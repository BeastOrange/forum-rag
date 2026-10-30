"""Public path-based index operations own and close their SQLite connections."""

import sqlite3
from contextlib import closing

import pytest

from wq_forum_rag import search_index


@pytest.mark.parametrize("operation", ["query", "rebuild", "failure"])
def test_path_operations_close_connections(tmp_path, monkeypatch, operation):
    db = tmp_path / "forum.sqlite3"
    with closing(sqlite3.connect(db)) as conn, conn:
        search_index.init_search_schema(conn)
    original = sqlite3.connect
    opened = []

    def connect(*args, **kwargs):
        conn = original(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(search_index.sqlite3, "connect", connect)
    if operation == "rebuild":
        assert search_index.rebuild_search_index(db)["fts_available"]
    elif operation == "failure":

        def fail(*args, **kwargs):
            raise sqlite3.OperationalError("query failed")

        monkeypatch.setattr(search_index, "_cached_candidate_ids", fail)
        with pytest.raises(sqlite3.OperationalError, match="query failed"):
            search_index.fts_candidates(db, "alpha")
    else:
        assert search_index.fts_candidates(db, "alpha") == []
    assert opened
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")


def test_rebuild_does_not_close_caller_connection(tmp_path):
    with closing(sqlite3.connect(tmp_path / "caller.sqlite3")) as conn:
        search_index.rebuild_search_index(conn)
        assert conn.in_transaction
        assert conn.execute("SELECT count(*) FROM document_fts").fetchone()[0] == 0
        conn.rollback()
