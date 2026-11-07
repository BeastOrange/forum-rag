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
