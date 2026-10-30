from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from wq_forum_rag.cli import app
from wq_forum_rag.manifest import SourceManifestStore
from wq_forum_rag.mcp_server import source_ingest_plan, source_status


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_source_status_is_read_only_until_commit(tmp_path: Path) -> None:
    source_dir = tmp_path / "notes"
    db_path = tmp_path / "forum.sqlite3"
    _write_text(source_dir / "alpha.md", "# Alpha\n")
    _write_text(source_dir / "nested" / "beta.txt", "beta\n")

    runner = CliRunner()
    status_result = runner.invoke(
        app, ["source-status", str(source_dir), "--db", str(db_path)]
    )
    assert status_result.exit_code == 0
    status_payload = json.loads(status_result.stdout)
    assert status_payload["dry_run"] is True
    assert status_payload["counts"] == {
        "new": 2,
        "modified": 0,
        "unchanged": 0,
        "deleted": 0,
    }

    with SourceManifestStore(db_path) as store:
        assert store.load_entries(source_dir.resolve()) == {}

    dry_plan_result = runner.invoke(
        app, ["source-ingest-plan", str(source_dir), "--db", str(db_path)]
    )
    assert dry_plan_result.exit_code == 0
    dry_plan_payload = json.loads(dry_plan_result.stdout)
    assert dry_plan_payload["committed"] is False
    assert [item["relative_path"] for item in dry_plan_payload["ingest_plan"]] == [
        "alpha.md",
        "nested/beta.txt",
    ]

    commit_result = runner.invoke(
        app,
        ["source-ingest-plan", str(source_dir), "--db", str(db_path), "--commit"],
    )
    assert commit_result.exit_code == 0
    commit_payload = json.loads(commit_result.stdout)
    assert commit_payload["committed"] is True
    assert commit_payload["counts"]["new"] == 2

    with SourceManifestStore(db_path) as store:
        assert set(store.load_entries(source_dir.resolve())) == {
            "alpha.md",
            "nested/beta.txt",
        }

    unchanged_payload = source_status(str(source_dir), db=str(db_path))
    assert unchanged_payload["counts"] == {
        "new": 0,
        "modified": 0,
        "unchanged": 2,
        "deleted": 0,
    }


def test_source_manifest_detects_modified_deleted_and_new_files(tmp_path: Path) -> None:
    source_dir = tmp_path / "notes"
    db_path = tmp_path / "forum.sqlite3"
    alpha_path = source_dir / "alpha.md"
    beta_path = source_dir / "beta.md"
    _write_text(alpha_path, "alpha v1\n")
    _write_text(beta_path, "beta v1\n")

    first_commit = source_ingest_plan(str(source_dir), db=str(db_path), commit=True)
    assert first_commit["counts"] == {
        "new": 2,
        "modified": 0,
        "unchanged": 0,
        "deleted": 0,
    }

    _write_text(alpha_path, "alpha v2\n")
    beta_path.unlink()
    _write_text(source_dir / "gamma.md", "gamma v1\n")

    status_payload = source_status(str(source_dir), db=str(db_path))
    assert status_payload["counts"] == {
        "new": 1,
        "modified": 1,
        "unchanged": 0,
        "deleted": 1,
    }
    assert [item["relative_path"] for item in status_payload["files"]["new"]] == [
        "gamma.md"
    ]
    assert [item["relative_path"] for item in status_payload["files"]["modified"]] == [
        "alpha.md"
    ]
    assert [item["relative_path"] for item in status_payload["files"]["deleted"]] == [
        "beta.md"
    ]

    dry_plan = source_ingest_plan(str(source_dir), db=str(db_path), commit=False)
    assert dry_plan["ingest_plan"] == [
        {"relative_path": "gamma.md", "status": "new"},
        {"relative_path": "alpha.md", "status": "modified"},
    ]
    assert dry_plan["delete_plan"] == [
        {"relative_path": "beta.md", "status": "deleted"}
    ]

    second_commit = source_ingest_plan(str(source_dir), db=str(db_path), commit=True)
    assert second_commit["committed"] is True
    assert second_commit["counts"] == {
        "new": 1,
        "modified": 1,
        "unchanged": 0,
        "deleted": 1,
    }

    final_status = source_status(str(source_dir), db=str(db_path))
    assert final_status["counts"] == {
        "new": 0,
        "modified": 0,
        "unchanged": 2,
        "deleted": 0,
    }


def test_source_manifest_ignores_binary_and_cache_inputs(tmp_path: Path) -> None:
    source_dir = tmp_path / "mixed"
    db_path = tmp_path / "forum.sqlite3"
    _write_text(source_dir / "keep.md", "keep\n")
    _write_text(source_dir / ".git" / "ignored.md", "ignored\n")
    _write_text(source_dir / ".cache" / "ignored.md", "ignored\n")
    _write_text(source_dir / ".venv" / "ignored.md", "ignored\n")
    _write_text(source_dir / ".pytest_cache" / "ignored.md", "ignored\n")
    _write_text(source_dir / "__pycache__" / "ignored.md", "ignored\n")
    _write_bytes(source_dir / "figure.png", b"\x89PNG\r\n\x1a\n")
    _write_bytes(source_dir / "report.pdf", b"%PDF-1.7")
    _write_bytes(source_dir / "clip.mp4", b"\x00\x00\x00\x18ftyp")
    _write_bytes(source_dir / "audio.mp3", b"ID3")
    _write_bytes(source_dir / "forum.sqlite3", b"")

    payload = source_status(str(source_dir), db=str(db_path))
    assert payload["counts"] == {"new": 1, "modified": 0, "unchanged": 0, "deleted": 0}
    assert [item["relative_path"] for item in payload["files"]["new"]] == ["keep.md"]
