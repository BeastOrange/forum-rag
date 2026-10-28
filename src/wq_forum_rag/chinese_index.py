"""Optional v2 Chinese candidate index; all mutations use the caller's transaction."""
from __future__ import annotations

import re
import sqlite3

_SEGMENTS = re.compile(r'[\u4e00-\u9fff]+')


def segments(text):
    return _SEGMENTS.findall(text)


def _char(c):
    return f'u{ord(c):04x}'


def _bigram(a, b):
    return f'b{ord(a):04x}x{ord(b):04x}'


def _encode(fields):
    runs = (run for field in fields for run in segments(field or ''))
    grams, chars = [], []
    for run in runs:
        grams.extend(_bigram(a, b) for a, b in zip(run, run[1:]))
        grams.append('boundary')
        chars.extend(_char(c) for c in run)
    return ' '.join(grams), ' '.join(chars)


def table_exists(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def complete(conn):
    if not table_exists(conn, 'chinese_index_meta'):
        return False
    return conn.execute('SELECT 1 FROM chinese_index_meta WHERE version=2 AND complete=1').fetchone() is not None


def create(conn):
    if sqlite3.sqlite_version_info < (3, 43, 0):
        raise sqlite3.OperationalError('Chinese index requires SQLite 3.43 or newer (contentless_delete)')
    conn.execute('CREATE TABLE IF NOT EXISTS chinese_index_meta (version INTEGER PRIMARY KEY, complete INTEGER NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS chinese_index_rows ('
                 'rowid INTEGER PRIMARY KEY, kind TEXT NOT NULL, record_id TEXT NOT NULL, UNIQUE(kind, record_id))')
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chinese_fts_v2 USING fts5("
                 "grams, chars, content='', contentless_delete=1)")


def insert_rows(conn, rows):
    encoded = []
    for kind, ident, _topic, title, community, content in rows:
        grams, chars = _encode((title, community, content))
        cursor = conn.execute('INSERT INTO chinese_index_rows(kind, record_id) VALUES (?, ?)', (kind, ident))
        encoded.append((cursor.lastrowid, grams, chars))
    conn.executemany('INSERT INTO chinese_fts_v2(rowid, grams, chars) VALUES (?, ?, ?)', encoded)


def sync_knowledge(conn, slug):
    if not table_exists(conn, 'chinese_fts_v2'):
        return
    row = conn.execute("SELECT rowid FROM chinese_index_rows WHERE kind='knowledge_page' AND record_id=?", (slug,)).fetchone()
    if row:
        conn.execute('DELETE FROM chinese_fts_v2 WHERE rowid=?', (row[0],))
        conn.execute('DELETE FROM chinese_index_rows WHERE rowid=?', (row[0],))
    rows = conn.execute("SELECT 'knowledge_page', slug, slug, title, status, summary || char(10) || body "
                        'FROM knowledge_pages WHERE slug=?', (slug,)).fetchall()
    insert_rows(conn, rows)


def candidates(conn, query, kind, limit, include_drafts):
    runs = list(dict.fromkeys(segments(query)))
    match = []
    for run in runs:
        if len(run) == 1:
            match.append(f'chars : "{_char(run)}"')
        else:
            phrase = ' '.join(_bigram(a, b) for a, b in zip(run, run[1:]))
            match.append(f'grams : "{phrase}"')
    sources = {
        'forum_chunk': (
            ' JOIN chunks c ON c.chunk_id=m.record_id JOIN topics t ON t.topic_id=c.topic_id',
            ('t.title', 't.community_title', 'c.content'), 'c.chunk_level=1'),
        'doc_chunk': (
            ' JOIN doc_chunks c ON c.chunk_id=m.record_id JOIN documents d ON d.slug=c.slug',
            ('d.title', "'docs'", 'c.content'), '1'),
        'knowledge_page': (
            ' JOIN knowledge_pages p ON p.slug=m.record_id',
            ('p.title', 'p.status', "(p.summary || char(10) || p.body)"),
            '1' if include_drafts else "p.status='published'"),
    }
    if kind not in sources:
        return []
    join, fields, condition = sources[kind]
    clauses = [condition]
    params = [kind, ' AND '.join(match)]
    for run in runs:
        clauses.append('(' + ' OR '.join(f'instr({field}, ?) > 0' for field in fields) + ')')
        params.extend((run,) * len(fields))
    # ASCII terms are literal substrings, not English stemming or semantics.
    for word in dict.fromkeys(re.findall(r'[A-Za-z0-9_]+', query.lower())):
        clauses.append('(' + ' OR '.join(f'instr(lower({field}), ?) > 0' for field in fields) + ')')
        params.extend((word,) * len(fields))
    params.append(limit)
    sql = ('SELECT m.record_id FROM chinese_fts_v2 f '
           'JOIN chinese_index_rows m ON m.rowid=f.rowid' + join +
           ' WHERE m.kind=? AND chinese_fts_v2 MATCH ? AND ' + ' AND '.join(clauses) +
           ' ORDER BY rank LIMIT ?')
    return [str(row[0]) for row in conn.execute(sql, params)]
