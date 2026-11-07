from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import ClassVar

import pytest
from typer.testing import CliRunner

from wq_forum_rag import forum_sync
from wq_forum_rag.cli import app
from wq_forum_rag.storage import ForumStore


class RecordingService:
    instances: ClassVar[list[RecordingService]] = []
    result: ClassVar[dict] = {"status": "synced"}

    def __init__(self, db_path, *, keep_backups=None, progress=None, **kwargs) -> None:
        self.db_path = Path(db_path)
        self.keep_backups = keep_backups
        self.sync_calls: list[dict] = []
        RecordingService.instances.append(self)

    def status(self, interval_days=None) -> dict:
        return forum_sync.ForumSyncService(self.db_path).status(interval_days)

    def sync(self, **kwargs) -> dict:
        self.sync_calls.append(kwargs)
        return {"db": str(self.db_path), **RecordingService.result}


@pytest.fixture
def recording(monkeypatch: pytest.MonkeyPatch) -> type[RecordingService]:
    RecordingService.instances = []
    RecordingService.result = {"status": "synced"}
    monkeypatch.setattr(forum_sync, "ForumSyncService", RecordingService)
    return RecordingService


def test_sync_full_passes_options(
    tmp_path: Path, recording: type[RecordingService]
) -> None:
    db = tmp_path / "forum.sqlite3"
    result = CliRunner().invoke(
        app,
        [
            "sync",
            "--full",
            "--community",
            "123",
            "--max-pages",
            "2",
            "--keep-backups",
            "3",
            "--db",
            str(db),
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "synced"
    service = recording.instances[0]
    assert service.keep_backups == 3
    assert service.sync_calls == [{"full": True, "max_pages": 2, "community": "123"}]


def test_sync_community_requires_full(
    tmp_path: Path, recording: type[RecordingService]
) -> None:
    result = CliRunner().invoke(
        app, ["sync", "--community", "123", "--db", str(tmp_path / "forum.sqlite3")]
    )

    assert result.exit_code != 0
    assert "--community requires --full" in result.output
    assert recording.instances == []


def test_sync_if_stale_on_fresh_db_makes_no_client_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_client():
        raise AssertionError("no client expected")

    monkeypatch.setattr(forum_sync, "default_client_factory", no_client)
    db = tmp_path / "forum.sqlite3"
    with ForumStore(db) as store, store.conn:
        store.conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES ('sync_last_success_at', ?)",
            (forum_sync._iso(forum_sync.datetime.now(forum_sync.UTC)),),
        )

    result = CliRunner().invoke(app, ["sync", "--if-stale", "--db", str(db)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "fresh"
    assert payload["due_at"]


def test_sync_if_stale_without_watermark_is_needs_full(tmp_path: Path) -> None:
    db = tmp_path / "forum.sqlite3"
    ForumStore(db).close()

    result = CliRunner().invoke(app, ["sync", "--if-stale", "--db", str(db)])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["status"] == "needs_full"


def test_sync_status_reports_needs_full(tmp_path: Path) -> None:
    db = tmp_path / "forum.sqlite3"
    ForumStore(db).close()

    result = CliRunner().invoke(app, ["sync-status", "--db", str(db)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["needs_full"] is True
    assert payload["credentials_configured"] is False
    assert payload["backups"] == []
    with sqlite3.connect(db) as conn:
        keys = {row[0] for row in conn.execute("SELECT key FROM schema_meta")}
    assert not any(key.startswith("sync_") for key in keys)


def test_sync_status_on_missing_db(tmp_path: Path) -> None:
    db = tmp_path / "missing.sqlite3"

    result = CliRunner().invoke(app, ["sync-status", "--db", str(db)])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["needs_full"] is True
    assert not db.exists()


class FakeCheckClient:
    def login(self) -> dict:
        return {"login_status": 201, "hops": 3, "role": "end-user"}

    def get_json(self, path: str, params=None) -> dict:
        assert path == forum_sync.POSTS_PATH
        assert params == {"page[size]": 2}
        return {"posts": [{"id": 1}, {"id": 2}]}


def test_sync_check_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(forum_sync, "default_client_factory", FakeCheckClient)

    result = CliRunner().invoke(app, ["sync-check"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "status": "ok",
        "login_status": 201,
        "hops": 3,
        "role": "end-user",
        "posts_status": 200,
        "posts_returned": 2,
    }


def test_sync_check_without_credentials_exits_1() -> None:
    result = CliRunner().invoke(app, ["sync-check"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "no_credentials"
    assert "WQ_BRAIN_PASSWORD" in payload["reason"]


def test_sync_no_credentials_exits_1(tmp_path: Path) -> None:
    db = tmp_path / "forum.sqlite3"

    result = CliRunner().invoke(app, ["sync", "--full", "--db", str(db)])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["status"] == "no_credentials"
    assert not db.exists()


@pytest.mark.parametrize(
    ("status", "code"),
    [
        ("partial", 0),
        ("already_running", 0),
        ("locked", 0),
        ("auth_failed", 1),
        ("blocked", 1),
        ("network_error", 1),
    ],
)
def test_sync_exit_codes(
    tmp_path: Path, recording: type[RecordingService], status: str, code: int
) -> None:
    recording.result = {"status": status}

    result = CliRunner().invoke(
        app, ["sync", "--full", "--db", str(tmp_path / "forum.sqlite3")]
    )

    assert result.exit_code == code
    assert json.loads(result.stdout)["status"] == status


def test_help_lists_sync_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    for name in ("sync", "sync-status", "sync-check"):
        assert name in result.output
