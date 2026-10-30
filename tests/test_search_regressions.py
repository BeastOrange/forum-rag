from __future__ import annotations

import sqlite3

from test_search_index import _index_fixture

from wq_forum_rag.indexer import ForumIndexService
from wq_forum_rag.search import hybrid_search
from wq_forum_rag.search_index import rebuild_search_index
from wq_forum_rag.search_records import search_forum_records


def test_misses_and_empty_do_not_score_all_records(tmp_path, monkeypatch):
    db = _index_fixture(tmp_path)
    service = ForumIndexService(db, auto_refresh=False)
    monkeypatch.setattr(
        service,
        "_search_rows",
        lambda ids: (
            (_ for _ in ()).throw(AssertionError("unbounded scan")) if not ids else []
        ),
    )
    assert service.search("xyzzyunrelatedword") == []
    assert service.search("") == []
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        assert search_forum_records(conn, "xyzzyunrelatedword") == []
        assert search_forum_records(conn, "", top_k=0) == []


def test_old_fts_chinese_substring_and_mixed_query(tmp_path):
    db = _index_fixture(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "UPDATE topics SET title='数据中性化处理 Alpha' WHERE topic_id='t1'"
        )
        conn.execute(
            "UPDATE chunks SET content='数据中性化处理 Alpha' WHERE topic_id='t1'"
        )
        rebuild_search_index(conn)
        conn.commit()
        assert any(
            row["topic_id"] == "t1" for row in search_forum_records(conn, "中性")
        )
        assert any(
            row["topic_id"] == "t1" for row in search_forum_records(conn, "中性 Alpha")
        )
    assert any(
        row["topic_id"] == "t1"
        for row in ForumIndexService(db, auto_refresh=False).search("中性")
    )


def test_exact_literal_wildcards_and_zero_limit(tmp_path):
    db = _index_fixture(tmp_path)
    service = ForumIndexService(db, auto_refresh=False)
    assert service.find_by_exact("%") == []
    assert service.find_by_exact("_") == []
    assert service.find_by_exact("t1", top_k=0) == []
    assert service.find_by_exact("t1", community="missing") == []
    assert service.find_by_exact("t1")[0]["topic_id"] == "t1"


def test_hybrid_rejects_no_lexical_evidence():
    chunks = [{"topic_id": "1", "content": "alpha beta"}]
    assert hybrid_search("xyzzyunrelatedword", chunks) == []
    assert hybrid_search("", chunks) == []


def test_missing_fts_is_literal_bounded_fallback_and_errors_propagate(tmp_path):
    import pytest

    db = _index_fixture(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("DROP TABLE document_fts")
        conn.commit()
        assert search_forum_records(conn, "neutralization")
        assert search_forum_records(conn, "xyzzyunrelatedword") == []
        conn.execute("CREATE TABLE document_fts (record_id TEXT, kind TEXT)")
        conn.commit()
        with pytest.raises(sqlite3.OperationalError):
            search_forum_records(conn, "neutralization")


def test_dedup_returns_other_topics_after_many_chunks(tmp_path):
    db = _index_fixture(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        original = conn.execute(
            "SELECT * FROM chunks WHERE topic_id='t1' AND chunk_level=1 LIMIT 1"
        ).fetchone()
        columns = original.keys()
        for index in range(20):
            values = dict(original)
            values["chunk_id"] = f"extra-{index}"
            values["chunk_index"] = 100 + index
            conn.execute(
                f"INSERT INTO chunks ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                tuple(values[column] for column in columns),
            )
        rebuild_search_index(conn)
        conn.commit()
        results = search_forum_records(conn, "neutralization", top_k=2)
        assert {result["topic_id"] for result in results} == {"t1", "t2"}


def test_doc_and_knowledge_chinese_and_misses():
    from wq_forum_rag.search_index import init_search_schema
    from wq_forum_rag.search_records import search_doc_records, search_knowledge_records

    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE documents (slug TEXT, title TEXT, source_path TEXT);
            CREATE TABLE doc_chunks (chunk_id TEXT, slug TEXT, content TEXT, chunk_index INTEGER);
            CREATE TABLE knowledge_pages (slug TEXT, title TEXT, summary TEXT, body TEXT,
                status TEXT, confidence REAL, updated_at TEXT);
            INSERT INTO documents VALUES ('doc', '数据中性化处理', 'doc.md');
            INSERT INTO doc_chunks VALUES ('chunk', 'doc', 'Alpha 数据中性化处理', 0);
            INSERT INTO knowledge_pages VALUES ('page', '数据中性化处理', 'Alpha', '详情', 'published', 0.9, '2026');
        """)
        assert init_search_schema(conn)
        conn.executemany(
            "INSERT INTO document_fts VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    "doc_chunk",
                    "chunk",
                    "doc",
                    "数据中性化处理",
                    "docs",
                    "Alpha 数据中性化处理",
                ),
                (
                    "knowledge_page",
                    "page",
                    "page",
                    "数据中性化处理",
                    "published",
                    "Alpha 详情",
                ),
            ],
        )
        assert search_doc_records(conn, "中性")[0]["slug"] == "doc"
        # An old published flag alone does not establish reviewed evidence.
        assert search_knowledge_records(conn, "中性") == []
        assert (
            search_knowledge_records(conn, "中性", include_drafts=True)[0]["slug"]
            == "page"
        )
        assert search_doc_records(conn, "xyzzyunrelatedword") == []
        assert search_knowledge_records(conn, "xyzzyunrelatedword") == []
        assert search_doc_records(conn, "中性", top_k=0) == []
        assert search_knowledge_records(conn, "中性", top_k=0) == []


def test_search_does_not_write_old_database(tmp_path):
    db = _index_fixture(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE document_fts")
        conn.execute("DROP TABLE embedding_cache")
        conn.commit()
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        assert search_forum_records(conn, "neutralization")
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE name IN ('document_fts','embedding_cache')"
            ).fetchall()
            == []
        )
    assert ForumIndexService(db, auto_refresh=False).search("neutralization")
    with sqlite3.connect(db) as conn:
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE name IN ('document_fts','embedding_cache')"
            ).fetchall()
            == []
        )


def test_exact_fallback_keeps_numeric_and_literal_matches(tmp_path):
    db = _index_fixture(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE topics SET title='rate 50% and a_b 123456' WHERE topic_id='t2'"
        )
    service = ForumIndexService(db, auto_refresh=False)
    for query in ("50%", "a_b", "123456"):
        assert service.find_by_exact(query)[0]["topic_id"] == "t2"
