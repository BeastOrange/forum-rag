from __future__ import annotations

import copy
from contextlib import closing
import math

import numpy as np

from .embeddings import get_semantic_model, model_identity
from .search import _build_snippet, lexical_score
from .search_index import candidate_ids
from .semantic_index import generation_signatures, generation_snapshot, readonly, source_state


def search_semantic_forum(db_path, query, *, index_dir, model_dir, top_k=5, min_score=0.5, mode='semantic') -> dict:
    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k <= 0:
        raise ValueError('top_k must be a positive integer')
    if mode not in ('semantic', 'hybrid'):
        raise ValueError('mode must be semantic or hybrid')
    if not isinstance(min_score, (int, float)) or not math.isfinite(min_score) or not -1 <= min_score <= 1:
        raise ValueError('min_score must be a finite cosine threshold between -1 and 1')
    if not query.strip():
        return {'results': [], 'index_status': 'not_queried', 'mode': mode,
                'min_score': min_score, 'threshold_note': 'uncalibrated cosine threshold'}
    state = source_state(db_path)
    identity = model_identity(model_dir)
    folder, meta, ids, matrix, signatures = generation_snapshot(index_dir, state, identity)
    query_vector = np.asarray(get_semantic_model(model_dir, identity).embed_query(query), dtype=np.float32)
    if query_vector.shape != (identity['dimension'],) or not np.isfinite(query_vector).all():
        raise RuntimeError('invalid query vector')
    norm = np.linalg.norm(query_vector)
    if not norm:
        raise RuntimeError('zero query vector')
    scores = matrix @ (query_vector / norm)
    if not np.isfinite(scores).all():
        raise RuntimeError('invalid semantic index vectors')
    limit = min(len(ids), max(top_k * 20, 80))
    if limit:
        selected = np.argpartition(-scores, limit - 1)[:limit]
        dense = sorted((int(i) for i in selected if scores[i] >= min_score), key=lambda i: (-float(scores[i]), ids[i]))
    else:
        dense = []
    dense_ranks = {ids[i]: rank for rank, i in enumerate(dense, 1)}
    dense_scores = {ids[i]: float(scores[i]) for i in dense}
    with closing(readonly(db_path)) as conn:
        conn.execute('BEGIN')
        conn.execute('SELECT 1 FROM sqlite_master LIMIT 1').fetchone()
        if source_state(db_path) != state:
            raise RuntimeError('semantic index stale: source changed during request')
        lexical = candidate_ids(conn, query, kind='forum_chunk', limit=limit) if mode == 'hybrid' and limit else []
        all_ids = list(dict.fromkeys([ids[i] for i in dense] + lexical))
        rows = {}
        # SQLite installations can have different variable limits.
        for start in range(0, len(all_ids), 500):
            part = all_ids[start:start + 500]
            marks = ','.join('?' for _ in part)
            for row in conn.execute(f'''SELECT c.chunk_id, c.topic_id, c.community_id, c.content, t.title,
                t.community_title, t.author, t.created_at, t.url, t.vote_num, t.comment_count
                FROM chunks c JOIN topics t ON t.topic_id=c.topic_id
                WHERE c.chunk_level=1 AND c.chunk_id IN ({marks})''', part):
                rows[row['chunk_id']] = dict(row)
        if any(i not in rows for i in all_ids):
            raise RuntimeError('semantic index stale: source records changed')
        lexical = sorted((i for i in lexical if i in rows and lexical_score(query, rows[i]) > 0),
                         key=lambda i: (-lexical_score(query, rows[i]), i))
        lexical_ranks = {item: rank for rank, item in enumerate(lexical, 1)}
        if source_state(db_path) != state:
            raise RuntimeError('semantic index stale: source changed during request')
    ranked = []
    for item in all_ids:
        if mode == 'semantic':
            score = dense_scores[item]
        else:
            score = (1 / (60 + dense_ranks[item]) if item in dense_ranks else 0) + (1 / (60 + lexical_ranks[item]) if item in lexical_ranks else 0)
            if not score:
                continue
        ranked.append((score, item))
    ranked.sort(key=lambda x: (-x[0], x[1]))
    results, seen = [], set()
    for score, item in ranked:
        row = rows[item]
        if row['topic_id'] in seen:
            continue
        seen.add(row['topic_id'])
        results.append({'topic_id': row['topic_id'], 'community_id': row['community_id'],
                        'community_title': row['community_title'], 'title': row['title'],
                        'author': row['author'], 'datetime': row['created_at'], 'url': row['url'],
                        'vote_num': row['vote_num'], 'comment_num': row['comment_count'],
                        'snippet': _build_snippet(query, row), 'score': score,
                        'semantic_score': dense_scores.get(item)})
        if len(results) == top_k:
            break
    if source_state(db_path) != state:
        raise RuntimeError('semantic index stale: source changed during request')
    if generation_signatures(folder) != signatures:
        raise RuntimeError('semantic index changed during request')
    if model_identity(model_dir) != identity:
        raise RuntimeError('model changed during request')
    return {'results': results, 'mode': mode, 'model': copy.deepcopy(identity), 'index_status': 'ready',
            'generation': folder.name, 'min_score': min_score,
            'threshold_note': 'uncalibrated cosine threshold'}
