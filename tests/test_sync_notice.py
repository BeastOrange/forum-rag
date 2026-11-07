"""Forum search tools add ``sync_notice`` only when the data is stale."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from wq_forum_rag import forum_sync, mcp_server, user_config
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


def _db(tmp_path: Path, age_days: float | None = 10, **meta: str) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
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
                )
            ]
        )
    rebuild_search_index(db)
    if age_days is not None:
        stamp = datetime.now(UTC) - timedelta(days=age_days)
        meta["sync_last_success_at"] = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    with sqlite3.connect(db) as conn:
        conn.executemany(
            "INSERT INTO schema_meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            list(meta.items()),
        )
    conn.close()
    return db


def _notice(db: Path) -> dict | None:
    return mcp_server.search_forum("neutralization", db=str(db), top_k=3).get(
        "sync_notice"
    )


class FakeService:
    def __init__(self, db_path, *, keep_backups=None, result=None) -> None:
        self.real = forum_sync.ForumSyncService(db_path)
        self.result = result or {"status": "synced"}

    def watermark(self):
        return self.real.watermark()

    def sync(self, *, full=False):
        return self.result


def _join_sync_threads() -> None:
    for thread in threading.enumerate():
        if thread.name == "forum-rag-sync":
            thread.join(timeout=5)


def test_fresh_data_has_no_notice(tmp_path: Path) -> None:
    db = _db(tmp_path, age_days=1)
    payload = mcp_server.search_forum("neutralization", db=str(db), top_k=3)
    assert payload["results"]
    assert "sync_notice" not in payload


def test_stale_data_with_auto_sync_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(user_config.AUTO_SYNC_ENV)
    db = _db(tmp_path)

    notice = _notice(db)

    assert notice is not None
    assert notice["reason"] == "auto_sync_off"
    assert notice["interval_days"] == 3.0
    assert notice["age_days"] >= 9.9
    assert notice["last_update"].endswith("Z")
    assert "forum-rag sync" in notice["message"]
    assert "forum-rag login" in notice["message"]
    assert "password in chat" in notice["message"]


def test_startup_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(user_config.AUTO_SYNC_ENV, "1")
    db = _db(tmp_path)

    assert forum_sync.maybe_start_background_sync(db)["status"] == "no_credentials"
    assert _notice(db)["reason"] == "no_credentials"


def test_startup_disabled_is_auto_sync_off(tmp_path: Path) -> None:
    db = _db(tmp_path)
    assert forum_sync.maybe_start_background_sync(db)["status"] == "disabled"
    assert _notice(db)["reason"] == "auto_sync_off"


def test_startup_login_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(user_config.AUTO_SYNC_ENV, "1")
    monkeypatch.setenv("WQ_BRAIN_USERNAME", "fake-user")
    monkeypatch.setenv("WQ_BRAIN_PASSWORD", "fake-pass")
    db = _db(tmp_path)

    def factory(path, *, keep_backups=None):
        return FakeService(path, result={"status": "auth_failed", "reason": "401"})

    status = forum_sync.maybe_start_background_sync(db, service_factory=factory)
    assert status["status"] == "started"
    _join_sync_threads()

    notice = _notice(db)
    assert notice["reason"] == "login_failed"
    assert "forum-rag login" in notice["message"]
    assert "again" in notice["message"]


def test_running_sync_has_no_advice(tmp_path: Path) -> None:
    db = _db(tmp_path)
    forum_sync.record_startup_status(db, "started", interval_days=3.0)

    notice = _notice(db)

    assert notice["reason"] == "sync_running"
    assert "forum-rag login" not in notice["message"]


def test_failed_startup_sync(tmp_path: Path) -> None:
    db = _db(tmp_path)
    forum_sync.record_startup_status(db, "network_error", reason="timeout")
    notice = _notice(db)
    assert notice["reason"] == "sync_failed"
    assert "network_error" in notice["message"]


def test_startup_state_for_another_db_is_ignored(tmp_path: Path) -> None:
    db = _db(tmp_path)
    forum_sync.record_startup_status(tmp_path / "other.sqlite3", "auth_failed")
    assert _notice(db)["reason"] == "auto_sync_off"


def test_meta_login_failure_without_startup_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(user_config.AUTO_SYNC_ENV, "1")
    db = _db(tmp_path, sync_last_status="auth_failed")
    assert _notice(db)["reason"] == "login_failed"

    other = _db(tmp_path / "b", sync_last_status="no_credentials")
    assert _notice(other)["reason"] == "no_credentials"

    due = _db(tmp_path / "c", sync_last_status="synced")
    assert _notice(due)["reason"] == "sync_due"


def test_no_watermark_has_no_notice(tmp_path: Path) -> None:
    db = _db(tmp_path, age_days=None)
    assert forum_sync.sync_notice(db) is None
    assert "sync_notice" not in mcp_server.search_forum("neutralization", db=str(db))
    assert forum_sync.sync_notice(tmp_path / "missing.sqlite3") is None
    assert not (tmp_path / "missing.sqlite3").exists()


def test_unreadable_db_has_no_notice(tmp_path: Path) -> None:
    db = tmp_path / "broken.sqlite3"
    db.write_bytes(b"not a database" * 64)
    assert forum_sync.sync_notice(db) is None


def test_find_by_exact_and_batch_carry_the_notice(tmp_path: Path) -> None:
    db = _db(tmp_path)

    exact = mcp_server.find_by_exact("Neutralization", db=str(db))
    batch = asyncio.run(mcp_server.search_forum_batch("neutralization", db=str(db)))

    assert exact["sync_notice"]["reason"] == "auto_sync_off"
    assert batch["sync_notice"]["reason"] == "auto_sync_off"


def test_busy_payloads_get_no_notice(tmp_path: Path) -> None:
    db = _db(tmp_path)
    for status in ("busy", "timeout", "locked"):
        payload = mcp_server._with_sync_notice({"status": status}, db)
        assert "sync_notice" not in payload


def test_startup_interval_override_suppresses_the_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sync_logger
) -> None:
    db = _db(tmp_path)
    monkeypatch.setenv("FORUM_RAG_DB", str(db))
    monkeypatch.setenv(user_config.AUTO_SYNC_ENV, "1")
    monkeypatch.setenv("WQ_BRAIN_USERNAME", "fake-user")
    monkeypatch.setenv("WQ_BRAIN_PASSWORD", "fake-pass")

    status = mcp_server._start_background_sync(["--sync-interval-days", "20"])

    assert status["status"] == "fresh"
    assert _notice(db) is None


def test_startup_exception_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sync_logger
) -> None:
    db = _db(tmp_path)
    monkeypatch.setenv("FORUM_RAG_DB", str(db))

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(forum_sync, "maybe_start_background_sync", boom)

    assert mcp_server._start_background_sync([])["status"] == "error"
    assert _notice(db)["reason"] == "sync_failed"
