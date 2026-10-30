import sqlite3
from contextlib import closing, contextmanager

import pytest

from wq_forum_rag import batch_search, search_index


@contextmanager
def connection(*args, **kwargs):
    with closing(sqlite3.connect(*args, **kwargs)) as conn, conn:
        yield conn


@pytest.fixture(params=["legacy", "fallback", "chinese"])
def indexed_db(tmp_path, request, monkeypatch):
    path = tmp_path / "forum.sqlite3"
    with connection(path) as conn:
        conn.executescript("""CREATE TABLE topics (topic_id TEXT PRIMARY KEY, title TEXT, community_title TEXT, url TEXT, content_hash TEXT);
        CREATE TABLE chunks (chunk_id TEXT PRIMARY KEY, topic_id TEXT, chunk_index INTEGER, chunk_level INTEGER, start_offset INTEGER, end_offset INTEGER, content TEXT, content_hash TEXT);""")
        for i, content in enumerate(
            [
                "missing 缺失 xalpha101",
                "missing 缺失 alpha101x",
                "missing 缺失 xalpha101 ALPHA101",
            ]
        ):
            ident = str(i)
            conn.execute(
                "INSERT INTO topics VALUES (?,?,?,?,?)",
                (ident, "missing 缺失", "", "", ""),
            )
            conn.execute(
                "INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?)",
                (ident, ident, 0, 1, 0, len(content), content, ""),
            )
        if request.param != "fallback":
            conn.row_factory = sqlite3.Row
            search_index.rebuild_search_index(conn, chinese=request.param == "chinese")
    monkeypatch.setenv(
        "FORUM_RAG_CHINESE_INDEX", "1" if request.param == "chinese" else "0"
    )
    return path, request.param


def test_constraints_before_limit_for_each_route(indexed_db, monkeypatch):
    path, mode = indexed_db
    monkeypatch.setattr(batch_search, "_CANDIDATES", 4)
    route = "缺失" if mode == "chinese" else "missing"
    result = batch_search.search_forum_batch(
        path, "missing alpha101", query_variants=[route]
    )
    assert "2" in {hit["topic_id"] for hit in result["results"]}
    assert route in next(
        hit["matched_queries"] for hit in result["results"] if hit["topic_id"] == "2"
    )


def test_constraint_boundaries_and_field_scope(indexed_db):
    path, mode = indexed_db
    with connection(path) as conn:
        conn.row_factory = sqlite3.Row
        # Direct route exercises SQL independently of ranking or result-side filtering.
        route = "缺失" if mode == "chinese" else "missing"
        ids = search_index.candidate_ids(
            conn, route, kind="forum_chunk", limit=2, constraints=("alpha101",)
        )
        assert ids == ["2"]


def test_ascii_case_boundary_rejects_unicode_case_equivalents():
    # Python re.I without ASCII admits Unicode case equivalents such as Kelvin sign.
    assert not batch_search._matches("K123", ("k123",))


def test_multiple_constraints_title_chunk_and_no_cross_chunk(indexed_db):
    path, mode = indexed_db
    with connection(path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO topics VALUES ('split', 'missing 缺失 TS_BACKFILL', '', '', '')"
        )
        for ident, index, content in [
            ("split0", 0, "missing 缺失 ALPHA101"),
            ("split1", 1, "missing 缺失 xalpha101"),
            ("split2", 2, "missing 缺失 alpha101_"),
        ]:
            conn.execute(
                "INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?)",
                (ident, "split", index, 1, 0, len(content), content, ""),
            )
        conn.execute(
            "INSERT INTO topics VALUES ('separate', 'missing 缺失', '', '', '')"
        )
        for ident, index, content in [
            ("sep0", 0, "missing 缺失 ts_backfill"),
            ("sep1", 1, "missing 缺失 alpha101"),
        ]:
            conn.execute(
                "INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?)",
                (ident, "separate", index, 1, 0, len(content), content, ""),
            )
        if mode != "fallback":
            search_index.rebuild_search_index(conn, chinese=mode == "chinese")
        route = "缺失" if mode == "chinese" else "missing"
        ids = search_index.candidate_ids(
            conn,
            route,
            kind="forum_chunk",
            limit=20,
            constraints=("ts_backfill", "alpha101"),
        )
        assert ids == ["split0"]


def test_constraints_are_validated_and_bound(indexed_db):
    path, mode = indexed_db
    with connection(path) as conn:
        route = "缺失" if mode == "chinese" else "missing"
        with pytest.raises(ValueError, match="ASCII"):
            search_index.candidate_ids(
                conn,
                route,
                kind="forum_chunk",
                limit=2,
                constraints=("alpha101' OR 1=1 --",),
            )
        assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 3


def test_unconstrained_candidate_contract_and_exact_id(indexed_db):
    path, mode = indexed_db
    with connection(path) as conn:
        route = "缺失" if mode == "chinese" else "missing"
        assert (
            len(search_index.candidate_ids(conn, route, kind="forum_chunk", limit=2))
            == 2
        )
    assert (
        batch_search.search_forum_batch(path, "2", query_variants=["无关"])["results"][
            0
        ]["topic_id"]
        == "2"
    )


def test_sql_matches_ascii_case_without_unicode_case_folding(indexed_db):
    path, mode = indexed_db
    with connection(path) as conn:
        conn.row_factory = sqlite3.Row
        for ident, content in [
            ("kelvin", "missing 缺失 K123"),
            ("ascii", "missing 缺失 K123"),
        ]:
            conn.execute(
                "INSERT INTO topics VALUES (?,?,?,?,?)",
                (ident, "missing 缺失", "", "", ""),
            )
            conn.execute(
                "INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?)",
                (ident, ident, 0, 1, 0, len(content), content, ""),
            )
        if mode != "fallback":
            search_index.rebuild_search_index(conn, chinese=mode == "chinese")
        route = "缺失" if mode == "chinese" else "missing"
        assert search_index.candidate_ids(
            conn, route, kind="forum_chunk", limit=20, constraints=("k123",)
        ) == ["ascii"]


def test_constraint_after_embedded_nul_is_not_lost(indexed_db):
    path, mode = indexed_db
    with connection(path) as conn:
        conn.row_factory = sqlite3.Row
        content = "missing 缺失\x00xalpha101\x00ALPHA101"
        conn.execute("INSERT INTO topics VALUES ('nul', 'missing 缺失', '', '', '')")
        conn.execute(
            "INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?)",
            ("nul", "nul", 0, 1, 0, len(content), content, ""),
        )
        if mode != "fallback":
            search_index.rebuild_search_index(conn, chinese=mode == "chinese")
        route = "缺失" if mode == "chinese" else "missing"
        assert "nul" in search_index.candidate_ids(
            conn, route, kind="forum_chunk", limit=20, constraints=("alpha101",)
        )


@pytest.mark.parametrize("encoding", ["UTF-16le", "UTF-16be"])
def test_nul_constraint_on_utf16_database(tmp_path, monkeypatch, encoding):
    path = tmp_path / "utf16.sqlite3"
    monkeypatch.setenv("FORUM_RAG_CHINESE_INDEX", "0")
    with connection(path) as conn:
        conn.execute("PRAGMA encoding='" + encoding + "'")
        conn.executescript("""CREATE TABLE topics (topic_id TEXT PRIMARY KEY, title TEXT, community_title TEXT);
            CREATE TABLE chunks (chunk_id TEXT PRIMARY KEY, topic_id TEXT, chunk_level INTEGER, content TEXT);""")
        conn.execute("INSERT INTO topics VALUES ('hit', 'missing', '')")
        conn.execute(
            "INSERT INTO chunks VALUES ('hit', 'hit', 1, ?)", ("before\0ALPHA101",)
        )
        assert search_index.candidate_ids(
            conn, "missing", kind="forum_chunk", limit=2, constraints=("alpha101",)
        ) == ["hit"]
