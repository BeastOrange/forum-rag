"""MCP startup starts the silent background sync and keeps serving searches."""

from __future__ import annotations

import logging
import sqlite3
import sys
import threading
from datetime import UTC, datetime, timedelta

import pytest

from wq_forum_rag import forum_sync, mcp_server
from wq_forum_rag.models import Topic
from wq_forum_rag.search_index import rebuild_search_index
from wq_forum_rag.storage import ForumStore


@pytest.fixture
def sync_logger(monkeypatch: pytest.MonkeyPatch) -> logging.Logger:
    logger = logging.getLogger("wq_forum_rag.sync")
    monkeypatch.setattr(logger, "handlers", [])
    monkeypatch.setattr(logger, "level", logger.level)
    monkeypatch.setattr(logger, "propagate", logger.propagate)
    return logger


def test_main_starts_background_sync_with_cli_overrides(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], sync_logger
):
    captured: dict = {}
    runs: list[str] = []

    def fake_start(db_path, *, interval_days=None, keep_backups=None):
        captured.update(
            db=db_path, interval_days=interval_days, keep_backups=keep_backups
        )
        return {"db": str(db_path), "status": "fresh"}

    monkeypatch.setattr(forum_sync, "maybe_start_background_sync", fake_start)
    monkeypatch.setattr(mcp_server.mcp, "run", lambda transport: runs.append(transport))
    monkeypatch.setattr(
        sys,
        "argv",
        ["forum-rag-mcp", "--sync-interval-days", "0.5", "--keep-backups", "2"],
    )

    mcp_server.main()

    assert captured["interval_days"] == 0.5
    assert captured["keep_backups"] == 2
    assert runs == ["stdio"]
    assert sync_logger.handlers
    assert sync_logger.propagate is False
    assert capsys.readouterr().out == ""


def _fixture_db(tmp_path):
    db = tmp_path / "forum.sqlite3"
    with ForumStore(db) as store:
        store.upsert_topics(
            [
                Topic(
                    "1",
                    "c",
                    "Forum",
                    "Neutralization",
                    "Industry neutralization notes.",
                    "url/1",
                    "",
                ),
                Topic(
                    "2", "c", "Forum", "Turnover", "Lower turnover ideas.", "url/2", ""
                ),
            ]
        )
    rebuild_search_index(db)
    old = (datetime.now(UTC) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES ('sync_last_success_at', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (old,),
        )
    conn.close()
    return db


def test_search_works_while_background_sync_runs(
    tmp_path, monkeypatch: pytest.MonkeyPatch, sync_logger
):
    db = _fixture_db(tmp_path)
    monkeypatch.setenv("WQ_FORUM_RAG_AUTO_SYNC", "1")
    monkeypatch.setenv("WQ_FORUM_RAG_AUTO_REFRESH", "0")
    monkeypatch.setenv("WQ_BRAIN_USERNAME", "fake-user")
    monkeypatch.setenv("WQ_BRAIN_PASSWORD", "fake-pass")
    entered = threading.Event()
    release = threading.Event()

    class BlockingService:
        def __init__(self, db_path, *, keep_backups=None):
            self.real = forum_sync.ForumSyncService(db_path, keep_backups=keep_backups)

        def watermark(self):
            return self.real.watermark()

        def sync(self, *, full=False):
            assert full is False
            entered.set()
            release.wait(10)
            return {"status": "synced"}

    status = forum_sync.maybe_start_background_sync(db, service_factory=BlockingService)
    assert status["status"] == "started"
    thread = next(t for t in threading.enumerate() if t.name == "forum-rag-sync")
    try:
        assert entered.wait(5)
        assert thread.is_alive()
        payload = mcp_server.search_forum("neutralization", db=str(db), top_k=3)
        assert payload["results"]
        assert payload["results"][0]["topic_id"] == "1"
        assert thread.is_alive()
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()


def test_main_keeps_serving_when_db_file_is_not_sqlite(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys, sync_logger
):
    db = tmp_path / "forum.sqlite3"
    db.write_bytes(b"this is not a sqlite database" * 64)
    monkeypatch.setenv("FORUM_RAG_DB", str(db))
    monkeypatch.setenv("WQ_FORUM_RAG_AUTO_SYNC", "1")
    monkeypatch.setenv("WQ_BRAIN_USERNAME", "fake-user")
    monkeypatch.setenv("WQ_BRAIN_PASSWORD", "fake-pass")
    runs: list[str] = []
    monkeypatch.setattr(mcp_server.mcp, "run", lambda transport: runs.append(transport))
    monkeypatch.setattr(sys, "argv", ["forum-rag-mcp"])

    assert mcp_server._start_background_sync([])["status"] == "error"
    mcp_server.main()

    assert runs == ["stdio"]
    assert capsys.readouterr().out == ""


def _api_post(pid: int, title: str, body: str) -> dict:
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "id": pid,
        "topic_id": 100,
        "title": title,
        "details": f"<p>{body}</p>",
        "html_url": f"https://support.example/posts/{pid}",
        "created_at": stamp,
        "updated_at": stamp,
        "author_id": 7,
        "vote_sum": 1,
        "comment_count": 0,
    }


class PausingClient:
    """Fake Support client that pauses after the first recent-posts page."""

    def __init__(self, paused: threading.Event, release: threading.Event) -> None:
        self.paused = paused
        self.release = release
        self.last_iteration_truncated = False

    def login(self) -> dict:
        return {"login_status": 201, "hops": 3, "role": "end-user"}

    def iter_pages(self, path, params=None, *, max_pages=None):
        self.last_iteration_truncated = False
        if path == forum_sync.TOPICS_PATH:
            yield {"topics": [{"id": 100, "name": "Forum"}]}
        elif path == forum_sync.POSTS_PATH:
            yield {"posts": [_api_post(301, "Orthogonal", "orthogonality first page")]}
            self.paused.set()
            assert self.release.wait(10)
            yield {"posts": [_api_post(302, "Crowding", "crowding second page")]}
        else:
            yield {}

    def map_concurrent(self, fn, items):
        return [(item, fn(item)) for item in items]


def test_search_reads_real_sync_page_commits_while_sync_is_paused(
    tmp_path, monkeypatch: pytest.MonkeyPatch, sync_logger
):
    db = _fixture_db(tmp_path)
    monkeypatch.setenv("WQ_FORUM_RAG_AUTO_SYNC", "1")
    monkeypatch.setenv("WQ_FORUM_RAG_AUTO_REFRESH", "0")
    monkeypatch.setenv("WQ_BRAIN_USERNAME", "fake-user")
    monkeypatch.setenv("WQ_BRAIN_PASSWORD", "fake-pass")
    paused = threading.Event()
    release = threading.Event()
    client = PausingClient(paused, release)

    def factory(db_path, *, keep_backups=None):
        return forum_sync.ForumSyncService(
            db_path, client_factory=lambda: client, keep_backups=keep_backups
        )

    status = forum_sync.maybe_start_background_sync(db, service_factory=factory)
    assert status["status"] == "started"
    thread = next(t for t in threading.enumerate() if t.name == "forum-rag-sync")
    try:
        assert paused.wait(10)
        payload = mcp_server.search_forum("orthogonality", db=str(db), top_k=3)
        assert payload.get("status") != "locked"
        assert [hit["topic_id"] for hit in payload["results"]] == ["301"]
        old = mcp_server.search_forum("neutralization", db=str(db), top_k=3)
        assert old["results"][0]["topic_id"] == "1"
        assert thread.is_alive()
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    payload = mcp_server.search_forum("crowding", db=str(db), top_k=3)
    assert [hit["topic_id"] for hit in payload["results"]] == ["302"]
    with sqlite3.connect(db) as conn:
        meta = dict(conn.execute("SELECT key, value FROM schema_meta").fetchall())
    conn.close()
    assert meta["sync_last_status"] == "synced"
