"""Cache operations must release SQLite handles even while objects stay alive."""
from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

import wq_forum_rag.search_cache as cache


@pytest.mark.parametrize('operation', ['initialize', 'read', 'write', 'count', 'failure'])
def test_cache_closes_owned_connections(tmp_path, monkeypatch, operation):
    db = tmp_path / 'cache.sqlite3'
    with closing(sqlite3.connect(db)) as conn, conn:
        from wq_forum_rag.search_index import init_search_schema
        init_search_schema(conn)
    backend = cache.CachedEmbeddingBackend(db, writable=operation != 'read')
    opened = []
    connect = sqlite3.connect

    def tracked(*args, **kwargs):
        conn = connect(*args, **kwargs)
        opened.append(conn)  # Keep handles alive: GC is not a cleanup contract.
        return conn

    monkeypatch.setattr(cache.sqlite3, 'connect', tracked)
    if operation == 'initialize':
        cache.CachedEmbeddingBackend(db)
    elif operation == 'count':
        assert cache.embedding_cache_count(db) == 0
    elif operation == 'failure':
        def fail(_texts):
            raise RuntimeError('encoding failed')
        monkeypatch.setattr(backend.backend, 'embed_documents', fail)
        with pytest.raises(RuntimeError, match='encoding failed'):
            backend.embed_documents(['new input'])
    else:
        assert backend.embed_documents(['new input'])
        if operation == 'write':
            with closing(connect(db)) as conn:
                assert conn.execute('SELECT COUNT(*) FROM embedding_cache').fetchone()[0] == 1
    assert opened
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            conn.execute('SELECT 1')
