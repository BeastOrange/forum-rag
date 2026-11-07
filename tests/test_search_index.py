from __future__ import annotations

import dataclasses
import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from wq_forum_rag import chinese_index
from wq_forum_rag.cli import app
from wq_forum_rag.evolution import EvolutionService
from wq_forum_rag.search_cache import embedding_cache_count
from wq_forum_rag.search_index import (
    fts_candidates,
    rebuild_search_index,
    reindex_forum_topics,
)
from wq_forum_rag.search_records import search_forum_records
from wq_forum_rag.storage import ForumStore


def _write_fixture(json_path: Path) -> None:
    payload = {
        "byCommunity": {
            "c1": {
                "title": "Alpha Lab",
                "topics": {
                    "t1": {
                        "id": "t1",
                        "author": "alice",
                        "commentNum": 0,
                        "datetime": "2026-04-28T13:00:00Z",
                        "postContent": "<p>Alpha decay should be tested with neutralization settings.</p>",
                        "title": "Alpha decay neutralization",
                        "url": "https://example.com/t1",
                        "voteNum": 10,
                    },
                    "t2": {
                        "id": "t2",
                        "author": "bob",
                        "commentNum": 0,
                        "datetime": "2026-04-28T13:10:00Z",
                        "postContent": "<p>Subindustry neutralization can reduce sector exposure.</p>",
                        "title": "Subindustry neutralization",
                        "url": "https://example.com/t2",
                        "voteNum": 8,
                    },
                },
            }
        }
    }
    json_path.write_text(json.dumps(payload), encoding="utf-8")


def _index_fixture(tmp_path: Path) -> Path:
    json_path = tmp_path / "forum.json"
    db_path = tmp_path / "forum.sqlite3"
    _write_fixture(json_path)
    result = CliRunner().invoke(
        app,
        ["index", "--json", str(json_path), "--db", str(db_path), "--rebuild"],
    )
    assert result.exit_code == 0
    return db_path


class CountingBackend:
    def __init__(self) -> None:
        self.calls = 0

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [
            [float(len(text)), float(text.count("a") + text.count("A")), 1.0]
            for text in texts
        ]

    def embed_query(self, text: str) -> list[float]:
        raise AssertionError(
            "CachedEmbeddingBackend should route query embeddings through embed_documents"
        )


def test_fts_exact_match_supports_forum_and_knowledge(tmp_path: Path) -> None:
    db_path = _index_fixture(tmp_path)
    service = EvolutionService(db_path)
    service.propose_knowledge_page(
        slug="alpha/neutralization-guide",
        title="Neutralization guide",
        summary="Subindustry neutralization is useful when sector exposure needs explicit control.",
        body="Use forum evidence before changing the neutralization hierarchy.",
        source_topic_ids=["t1", "t2"],
        confidence=0.9,
    )

    payload = rebuild_search_index(db_path)
    assert payload["fts_available"] is True
    assert payload["forum_docs"] >= 2
    assert payload["knowledge_docs"] >= 1

    forum_candidates = fts_candidates(
        db_path, "subindustry neutralization", kind="forum_chunk"
    )
    knowledge_candidates = fts_candidates(
        db_path,
        "subindustry neutralization",
        kind="knowledge_page",
        include_drafts=True,
    )

    assert any(candidate.startswith("t2:chunk:") for candidate in forum_candidates)
    assert "alpha/neutralization-guide" in knowledge_candidates


def test_embedding_cache_reuses_vectors_across_repeated_searches(
    tmp_path: Path,
) -> None:
    db_path = _index_fixture(tmp_path)
    backend = CountingBackend()

    with ForumStore(db_path) as store:
        first = search_forum_records(
            store.conn,
            "neutralization",
            top_k=2,
            embedding_backend=backend,
        )
        second = search_forum_records(
            store.conn,
            "neutralization",
            top_k=2,
            embedding_backend=backend,
        )

    assert first
    assert second
    assert backend.calls == 2
    assert embedding_cache_count(db_path) >= 2


def test_fallback_without_fts_still_returns_results(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = _index_fixture(tmp_path)

    def fake_init_search_schema(conn: sqlite3.Connection) -> bool:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS embedding_cache (
                backend_id TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                vector_json TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(backend_id, content_hash)
            )
            """
        )
        return False

    monkeypatch.setattr(
        "wq_forum_rag.search_index.init_search_schema", fake_init_search_schema
    )

    backend = CountingBackend()
    with ForumStore(db_path) as store:
        results = search_forum_records(
            store.conn,
            "neutralization",
            top_k=1,
            embedding_backend=backend,
        )

    assert results[0]["topic_id"] in {"t1", "t2"}


def _replace_t1(db_path: Path, *, title: str, body: str) -> ForumStore:
    store = ForumStore(db_path)
    topic = store.get_topic("t1")
    assert topic is not None
    store.upsert_topics(
        [dataclasses.replace(topic, title=title, body_text=body, content_hash="")]
    )
    return store


def _fts_rows(conn: sqlite3.Connection, topic_id: str) -> list[tuple]:
    return conn.execute(
        "SELECT rowid, kind, record_id, title, community, content FROM document_fts "
        "WHERE topic_id=? ORDER BY rowid",
        (topic_id,),
    ).fetchall()


def test_reindex_forum_topics_refreshes_only_changed_topics(tmp_path: Path) -> None:
    db_path = _index_fixture(tmp_path)
    rebuild_search_index(db_path)
    assert "t1:chunk:0" in fts_candidates(db_path, "decay", kind="forum_chunk")

    store = _replace_t1(
        db_path, title="Zebracorn momentum", body="Zebracorn momentum signals."
    )
    try:
        t2_before = _fts_rows(store.conn, "t2")
        with store.conn:
            inserted = reindex_forum_topics(store.conn, ["t1"])
        t2_after = _fts_rows(store.conn, "t2")
        t1_rows = _fts_rows(store.conn, "t1")
    finally:
        store.close()

    assert inserted == len(t1_rows) >= 1
    assert t2_before == t2_after and t2_before
    assert fts_candidates(db_path, "zebracorn", kind="forum_chunk") == ["t1:chunk:0"]
    assert fts_candidates(db_path, "decay", kind="forum_chunk") == []


def test_reindex_forum_topics_without_fts_returns_zero(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    try:
        assert reindex_forum_topics(conn, ["t1"]) == 0
    finally:
        conn.close()


@pytest.mark.skipif(
    sqlite3.sqlite_version_info < (3, 43, 0),
    reason="Chinese index requires SQLite 3.43",
)
def test_reindex_forum_topics_updates_chinese_index(tmp_path: Path) -> None:
    db_path = _index_fixture(tmp_path)
    rebuild_search_index(db_path, chinese=True)

    store = _replace_t1(db_path, title="动量因子", body="动量因子需要中性化处理。")
    try:
        with store.conn:
            reindex_forum_topics(store.conn, ["t1"])
        conn = store.conn
        found = chinese_index.candidates(conn, "动量因子", "forum_chunk", 10, False)
        indexed = conn.execute(
            "SELECT count(*) FROM chinese_index_rows WHERE kind='forum_chunk'"
        ).fetchone()[0]
        fts_rows = conn.execute("SELECT count(*) FROM chinese_fts_v2").fetchone()[0]
        chunks = conn.execute(
            "SELECT count(*) FROM chunks WHERE chunk_level=1"
        ).fetchone()[0]
    finally:
        store.close()

    assert found == ["t1:chunk:0"]
    assert indexed == chunks
    assert fts_rows == indexed
