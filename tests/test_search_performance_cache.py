from __future__ import annotations

import sqlite3

from test_search_index import _index_fixture

from wq_forum_rag.search_cache import CachedEmbeddingBackend


def test_readonly_hash_cache_reuses_pure_vectors_without_exposing_mutable_cache(
    tmp_path, monkeypatch
):
    import wq_forum_rag.search_cache as cache

    cache._hash_vector.cache_clear()
    db = _index_fixture(tmp_path)
    # Legacy test_search loads a second embeddings module during collection.
    # Instrument the class actually used by this cache, not the other module.
    backend_type = cache.HashEmbeddingBackend
    original = backend_type.embed_query
    calls = []

    def counted(self, text):
        calls.append((self.seed, self.dims, text))
        return original(self, text)

    monkeypatch.setattr(backend_type, "embed_query", counted)
    backend = CachedEmbeddingBackend(db, writable=False)
    first = backend.embed_documents(["cache probe unseen text"])
    expected = first[0].copy()
    first[0][0] += 100
    assert backend.embed_documents(["cache probe unseen text"])[0] == expected
    assert len(calls) == 1
    backend.embed_documents(["different cache probe text"])
    assert len(calls) == 2
    another = CachedEmbeddingBackend(db, backend=backend_type(dims=16), writable=False)
    assert len(another.embed_query("cache probe unseen text")) == 16
    assert len(calls) == 3
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM embedding_cache").fetchone()[0] == 0


def test_candidate_cache_changes_with_wal_and_does_not_override_read_snapshot(tmp_path):
    from wq_forum_rag.search_index import candidate_ids

    db = _index_fixture(tmp_path)
    with sqlite3.connect(db) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        with sqlite3.connect(db) as reader:
            assert candidate_ids(reader, "neutralization", kind="forum_chunk", limit=10)
            reader.execute("BEGIN")
            reader.execute("SELECT COUNT(*) FROM document_fts").fetchone()
            writer.execute("DELETE FROM document_fts WHERE kind='forum_chunk'")
            writer.commit()
            assert candidate_ids(reader, "neutralization", kind="forum_chunk", limit=10)
            reader.rollback()
            assert (
                candidate_ids(reader, "neutralization", kind="forum_chunk", limit=10)
                == []
            )


def test_candidate_cache_reuses_unchanged_ids_and_invalidates_commits(
    tmp_path, monkeypatch
):
    import wq_forum_rag.search_index as index

    db = _index_fixture(tmp_path)
    calls = []
    original = index._query_fts

    def counted(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(index, "_query_fts", counted)
    with sqlite3.connect(db) as conn:
        expected = index.fts_candidates(
            db, "neutralization", kind="forum_chunk", limit=10
        )
        count = len(calls)
        returned = index.fts_candidates(
            db, "neutralization", kind="forum_chunk", limit=10
        )
        returned.clear()
        assert (
            index.fts_candidates(db, "neutralization", kind="forum_chunk", limit=10)
            == expected
        )
        assert len(calls) == count
        conn.execute("DELETE FROM document_fts WHERE kind='forum_chunk'")
        conn.commit()
        assert (
            index.fts_candidates(db, "neutralization", kind="forum_chunk", limit=10)
            == []
        )
