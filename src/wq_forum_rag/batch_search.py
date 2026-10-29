"""Bounded lexical batch search and verbatim evidence retrieval."""
from __future__ import annotations

import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .search import lexical_score
from .search_index import candidate_ids

_CANDIDATES = 200
_TIMEOUT_SECONDS = 2.0
_LITERAL = re.compile(r'(?<![A-Za-z0-9_])(?:[A-Za-z0-9_]*_[A-Za-z0-9_]+|[A-Za-z0-9]+_|[A-Za-z]+[0-9]+[A-Za-z0-9_]*|[0-9]+[A-Za-z][A-Za-z0-9_]*|[0-9]{5,})(?![A-Za-z0-9_])')
_OFFSET_NOTE = 'Character offsets refer to Topic.document_text() export representation, including Title/Body/Comments wrappers, not the original body.'
_HASH_NOTE = 'Content hashes are derived version identifiers, not hashes of the returned excerpt.'
_LITERAL_NOTE = 'Except exact topic ID lookups, original underscore identifiers, mixed letter-digit codes and standalone numbers of at least five digits must all occur in the candidate title or same chunk; cross-chunk evidence may be missed.'


def _integer(name, value, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f'{name} must be an integer between {minimum} and {maximum}')


def _string(name, value, *, max_length=512):
    if not isinstance(value, str):
        raise TypeError(f'{name} must be a string')
    if len(value) > max_length:
        raise ValueError(f'{name} exceeds {max_length} characters')
    return value.strip()


@contextmanager
def _snapshot(db_path):
    path = Path(db_path).resolve(strict=True)
    deadline = time.monotonic() + _TIMEOUT_SECONDS
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    try:
        conn.execute('BEGIN')
        yield conn, deadline
        _check_deadline(deadline)
    except sqlite3.OperationalError as exc:
        if 'interrupted' in str(exc).lower() and time.monotonic() >= deadline:
            raise TimeoutError('SQLite request exceeded time budget') from exc
        raise
    finally:
        conn.close()


def _check_deadline(deadline):
    if time.monotonic() >= deadline:
        raise TimeoutError('SQLite request exceeded time budget')


def _forum_tables_available(conn):
    tables = {row[0].lower() for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND lower(name) IN ('topics', 'chunks')")}
    return tables == {'topics', 'chunks'}


def _language(title, text):
    text = (title or '') + '\n' + (text or '')
    # Topic.document_text uses an inline "Title: actual title" wrapper.
    # Remove only labels, retaining the title/body text used for detection.
    text = re.sub(r'(?m)^[ \t]*(?:Title:|Body:|Comments:)[ \t]*', '', text)
    text = re.sub(r'(?m)^[ \t]*\[Comment \d+\][^\n]*', '', text)
    text = re.sub(r'```[\s\S]*?```|`[^`]*`|(?<![A-Za-z])[A-Z]{2,}(?![A-Za-z])', ' ', text)
    text = _LITERAL.sub(' ', text)
    zh = bool(re.search(r'[\u4e00-\u9fff]', text))
    en = bool(re.search(r'[A-Za-z]{2,}', text))
    return 'mixed' if zh and en else 'zh' if zh else 'en' if en else 'unknown'


def _constraints(query):
    return tuple(dict.fromkeys(m.group().lower() for m in _LITERAL.finditer(query)))


def _matches(text, constraints):
    return all(re.search(r'(?<![A-Za-z0-9_])' + re.escape(term) + r'(?![A-Za-z0-9_])', text, re.I | re.ASCII) for term in constraints)


def _excerpt(row, size, query=''):
    text = row['content'] or ''
    start = 0
    if len(text) > size and query:
        terms = re.findall(r'[A-Za-z0-9_]+|[\u4e00-\u9fff]+', query)
        positions = [text.lower().find(term.lower()) for term in terms]
        positions = [position for position in positions if position >= 0]
        if positions:
            start = min(max(min(positions) - size // 4, 0), len(text) - size)
    end = min(start + size, len(text))
    return {'chunk_id': row['chunk_id'], 'chunk_index': row['chunk_index'],
            'chunk_content_hash': row['chunk_hash'], 'topic_content_hash': row['topic_hash'],
            'excerpt': text[start:end], 'excerpt_start': start, 'excerpt_end': end,
            'document_start_offset': row['start_offset'] + start,
            'document_end_offset': row['start_offset'] + end,
            'chunk_document_start_offset': row['start_offset'],
            'chunk_document_end_offset': row['end_offset'],
            'truncated': start > 0 or end < len(text)}


_SELECT = '''SELECT c.chunk_id, c.topic_id, c.chunk_index, c.chunk_level, c.start_offset, c.end_offset,
 c.content, c.content_hash AS chunk_hash, t.content_hash AS topic_hash, t.title, t.url
 FROM chunks c JOIN topics t ON t.topic_id=c.topic_id'''


def search_forum_batch(db_path, query: str, *, query_variants: list[str] | None = None,
                       top_k=6, evidence_chars=600, max_evidence_chars=6000) -> dict:
    query = _string('query', query)
    _integer('top_k', top_k, 1, 20)
    _integer('evidence_chars', evidence_chars, 100, 2000)
    _integer('max_evidence_chars', max_evidence_chars, 200, 20000)
    if query_variants is not None and not isinstance(query_variants, list):
        raise TypeError('query_variants must be a list')
    if query_variants is not None and len(query_variants) > 3:
        raise ValueError('query_variants may contain at most three entries')
    supplied = [query] + [_string('query variant', x) for x in (query_variants or [])]
    routes, seen = [], set()
    for text in supplied:
        key = ' '.join(text.split()).casefold()
        if text and key not in seen:
            routes.append(text)
            seen.add(key)
    # An empty original query cannot be rescued by variants.
    if not query:
        routes = []
    id_lookup = bool(re.fullmatch(r'\d+', query))
    if id_lookup:
        routes = [query]
    literals = _constraints(query)
    ranked = []
    route_counts = []
    route_limits = []
    index_status = 'not_queried'
    with _snapshot(db_path) as (conn, deadline):
        if not _forum_tables_available(conn):
            index_status = 'unavailable'
        elif routes:
            if id_lookup:
                index_status = 'not_used'
                rows = conn.execute(_SELECT + ' WHERE t.topic_id=? AND c.chunk_level=1 ORDER BY c.chunk_index,c.chunk_id LIMIT 1', (query,)).fetchall()
                route_lists = [[row['chunk_id'] for row in rows]]
                route_limits = [1]
            else:
                route_lists = []
                index_status = ('ready' if conn.execute("SELECT 1 FROM sqlite_master WHERE name='document_fts' AND type='table'").fetchone() else 'degraded')
                limit = _CANDIDATES // len(routes)
                for route in routes:
                    route_limits.append(limit)
                    options = {'constraints': literals} if literals else {}
                    ids = candidate_ids(conn, route, kind='forum_chunk', limit=limit, **options)
                    route_lists.append(list(dict.fromkeys(ids))[:limit])
                    _check_deadline(deadline)
            route_counts = [len(items) for items in route_lists]
            ids = list(dict.fromkeys(item for items in route_lists for item in items))
            rows_by_id = {}
            if ids:
                marks = ','.join('?' for _ in ids)
                rows_by_id = {row['chunk_id']: dict(row) for row in conn.execute(_SELECT + f' WHERE c.chunk_level=1 AND c.chunk_id IN ({marks})', ids)}
            topics = {}
            for route, ids_for_route in zip(routes, route_lists):
                per_topic = {}
                for item in ids_for_route:
                    row = rows_by_id.get(item)
                    if (not row or (id_lookup and row['topic_id'] != query)
                            or (not id_lookup and not _matches((row['title'] or '') + ' ' + (row['content'] or ''), literals))):
                        continue
                    score = 1.0 if id_lookup else lexical_score(route, {'title': row['title'], 'content': row['content']})
                    if score <= 0:
                        continue
                    key = (-score, item)
                    topic = row['topic_id']
                    if topic not in per_topic or key < per_topic[topic][0]:
                        per_topic[topic] = (key, row)
                ordered = sorted(per_topic.items(), key=lambda pair: (pair[1][0], pair[0]))
                for rank, (topic, (key, row)) in enumerate(ordered, 1):
                    value = topics.setdefault(topic, {'score': 0.0, 'routes': [], 'best': (key, row, route)})
                    value['score'] += 1 / (60 + rank)
                    value['routes'].append(route)
                    if key < value['best'][0]:
                        value['best'] = (key, row, route)
                _check_deadline(deadline)
            ranked = sorted(topics.items(), key=lambda pair: (-pair[1]['score'], pair[0]))
    remaining = max_evidence_chars
    results = []
    for topic, data in ranked[:top_k]:
        if remaining <= 0:
            break
        _, row, route = data['best']
        evidence = _excerpt(row, min(evidence_chars, remaining), route)
        remaining -= len(evidence['excerpt'])
        results.append({'topic_id': topic, 'title': row['title'], 'url': row['url'],
                        'score': data['score'], 'snippet': evidence['excerpt'],
                        'language': _language(row['title'], evidence['excerpt']),
                        'matched_queries': data['routes'], 'evidence': [evidence]})
    used = sum(len(e['excerpt']) for hit in results for e in hit['evidence'])
    return {'query': query, 'executed_queries': routes, 'results': results,
            'language_groups': {lang: [r['topic_id'] for r in results if r['language'] == lang] for lang in ('zh', 'en', 'mixed', 'unknown')},
            'strategy': 'exact topic ID' if id_lookup else 'per-route topic lexical ranking with RRF',
            'score_note': 'RRF rank score, not a probability', 'offset_note': _OFFSET_NOTE,
            'hash_note': _HASH_NOTE, 'literal_note': _LITERAL_NOTE, 'index_status': index_status,
            'budget': {'candidate_limit': _CANDIDATES, 'route_candidate_counts': route_counts,
                       'route_candidate_limits': route_limits,
                       'evidence_chars_per_chunk': evidence_chars, 'max_evidence_chars': max_evidence_chars,
                       'returned_evidence_chars': used, 'evidence_budget_exhausted': used >= max_evidence_chars,
                       'results_truncated': len(ranked) > len(results),
                       'candidate_limit_reached': any(count >= limit for count, limit in zip(route_counts, route_limits)) if not id_lookup else False}}


def get_forum_evidence(db_path, chunk_id: str, *, before=1, after=1, max_chars=6000,
                       expected_content_hash: str | None = None) -> dict:
    chunk_id = _string('chunk_id', chunk_id)
    if not chunk_id:
        raise ValueError('chunk_id must not be empty')
    _integer('before', before, 0, 2)
    _integer('after', after, 0, 2)
    _integer('max_chars', max_chars, 1, 20000)
    if expected_content_hash is not None and not isinstance(expected_content_hash, str):
        raise TypeError('expected_content_hash must be a string or None')
    with _snapshot(db_path) as (conn, deadline):
        if not _forum_tables_available(conn):
            return {'status': 'unavailable', 'chunk_id': chunk_id, 'evidence': [], 'anchor_complete': False, 'offset_note': _OFFSET_NOTE, 'hash_note': _HASH_NOTE}
        anchor = conn.execute(_SELECT + ' WHERE c.chunk_id=?', (chunk_id,)).fetchone()
        if anchor is None:
            return {'status': 'not_found', 'chunk_id': chunk_id, 'evidence': [], 'anchor_complete': False, 'offset_note': _OFFSET_NOTE, 'hash_note': _HASH_NOTE}
        if anchor['chunk_level'] != 1:
            return {'status': 'invalid_level', 'chunk_id': chunk_id, 'evidence': [], 'anchor_complete': False, 'offset_note': _OFFSET_NOTE, 'hash_note': _HASH_NOTE}
        if expected_content_hash is not None and expected_content_hash != anchor['chunk_hash']:
            return {'status': 'stale', 'chunk_id': chunk_id, 'evidence': [], 'anchor_complete': False, 'offset_note': _OFFSET_NOTE, 'hash_note': _HASH_NOTE}
        previous = conn.execute(_SELECT + ''' WHERE c.topic_id=? AND c.chunk_level=1
            AND (c.chunk_index,c.chunk_id) < (?,?)
            ORDER BY c.chunk_index DESC,c.chunk_id DESC LIMIT ?''',
            (anchor['topic_id'], anchor['chunk_index'], chunk_id, before)).fetchall()
        following = conn.execute(_SELECT + ''' WHERE c.topic_id=? AND c.chunk_level=1
            AND (c.chunk_index,c.chunk_id) > (?,?)
            ORDER BY c.chunk_index,c.chunk_id LIMIT ?''',
            (anchor['topic_id'], anchor['chunk_index'], chunk_id, after)).fetchall()
        _check_deadline(deadline)
    rows = [anchor, *previous, *following]
    remaining = max_chars
    evidence = []
    for row in rows:
        if remaining <= 0:
            break
        item = _excerpt(row, remaining)
        item['overlaps_anchor'] = (row['chunk_id'] != chunk_id and
                                   row['start_offset'] < anchor['end_offset'] and
                                   anchor['start_offset'] < row['end_offset'])
        remaining -= len(item['excerpt'])
        evidence.append(item)
    return {'status': 'ok', 'chunk_id': chunk_id, 'topic_id': anchor['topic_id'], 'evidence': evidence,
            'anchor_complete': bool(evidence and not evidence[0]['truncated']),
            'budget': {'max_chars': max_chars, 'returned_evidence_chars': max_chars - remaining,
                       'truncated': len(evidence) < len(rows) or any(item['truncated'] for item in evidence)},
            'offset_note': _OFFSET_NOTE, 'hash_note': _HASH_NOTE,
            'neighbor_order': 'anchor first, preceding nearest first, following nearest first',
            'overlap_note': 'Neighbor chunks may overlap; offsets identify shared source text.'}
