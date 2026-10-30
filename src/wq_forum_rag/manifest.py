from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

IGNORED_DIR_NAMES = {".git", ".cache", ".venv", ".pytest_cache", "__pycache__"}
IGNORED_SUFFIXES = {
    ".7z",
    ".avi",
    ".bin",
    ".bmp",
    ".bz2",
    ".class",
    ".db",
    ".dll",
    ".doc",
    ".docx",
    ".dylib",
    ".epub",
    ".exe",
    ".flac",
    ".gif",
    ".gz",
    ".heic",
    ".ico",
    ".jar",
    ".jpeg",
    ".jpg",
    ".m4a",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".npy",
    ".npz",
    ".ogg",
    ".pdf",
    ".pickle",
    ".pkl",
    ".png",
    ".ppt",
    ".pptx",
    ".pyc",
    ".so",
    ".sqlite",
    ".sqlite3",
    ".tar",
    ".tif",
    ".tiff",
    ".wav",
    ".webm",
    ".webp",
    ".xls",
    ".xlsx",
    ".zip",
}
TEXT_SUFFIXES = {
    "",
    ".csv",
    ".json",
    ".log",
    ".markdown",
    ".md",
    ".mdown",
    ".mdx",
    ".rst",
    ".text",
    ".toml",
    ".tsv",
    ".txt",
    ".yaml",
    ".yml",
}


@dataclass(frozen=True)
class ManifestEntry:
    relative_path: str
    sha256: str
    mtime_ns: int
    size: int


class SourceManifestStore:
    def __init__(self, db_path: str | Path, *, initialize: bool = True) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        if initialize:
            self._init_schema()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> SourceManifestStore:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def load_entries(self, source_root: Path) -> dict[str, ManifestEntry]:
        if not self._table_exists("source_manifest"):
            return {}
        rows = self.conn.execute(
            """
            SELECT relative_path, sha256, mtime_ns, size
            FROM source_manifest
            WHERE source_root = ?
            ORDER BY relative_path
            """,
            (str(source_root),),
        ).fetchall()
        return {
            str(row["relative_path"]): ManifestEntry(
                relative_path=str(row["relative_path"]),
                sha256=str(row["sha256"]),
                mtime_ns=int(row["mtime_ns"]),
                size=int(row["size"]),
            )
            for row in rows
        }

    def replace_entries(self, source_root: Path, entries: list[ManifestEntry]) -> None:
        serialized = [
            (
                str(source_root),
                entry.relative_path,
                entry.sha256,
                entry.mtime_ns,
                entry.size,
            )
            for entry in entries
        ]
        with self.conn:
            self.conn.execute(
                "DELETE FROM source_manifest WHERE source_root = ?", (str(source_root),)
            )
            if serialized:
                self.conn.executemany(
                    """
                    INSERT INTO source_manifest (
                        source_root,
                        relative_path,
                        sha256,
                        mtime_ns,
                        size
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    serialized,
                )

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS source_manifest (
                source_root TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                mtime_ns INTEGER NOT NULL,
                size INTEGER NOT NULL,
                PRIMARY KEY(source_root, relative_path)
            );

            CREATE INDEX IF NOT EXISTS idx_source_manifest_root
                ON source_manifest(source_root);
            """
        )
        self.conn.commit()

    def _table_exists(self, name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (name,),
        ).fetchone()
        return row is not None


class SourceManifestService:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def source_status(self, source: str | Path) -> dict[str, Any]:
        return self._build_payload(source, commit=False, include_plan=False)

    def source_ingest_plan(
        self, source: str | Path, *, commit: bool = False
    ) -> dict[str, Any]:
        return self._build_payload(source, commit=commit, include_plan=True)

    def _build_payload(
        self,
        source: str | Path,
        *,
        commit: bool,
        include_plan: bool,
    ) -> dict[str, Any]:
        root = Path(source).expanduser().resolve()
        current_entries = scan_source_manifest(root)
        with SourceManifestStore(self.db_path, initialize=commit) as store:
            previous_entries = store.load_entries(root)
            delta = diff_manifest_entries(current_entries, previous_entries)
            if commit:
                store.replace_entries(root, current_entries)

        payload: dict[str, Any] = {
            "db": str(self.db_path),
            "source": str(root),
            "source_type": "directory" if root.is_dir() else "file",
            "dry_run": not commit,
            "committed": commit,
            "counts": {name: len(entries) for name, entries in delta.items()},
            "tracked_files": len(current_entries),
            "files": {
                name: [asdict(entry) for entry in entries]
                for name, entries in delta.items()
            },
        }
        if include_plan:
            payload["ingest_plan"] = [
                {"relative_path": entry.relative_path, "status": status}
                for status in ("new", "modified")
                for entry in delta[status]
            ]
            payload["delete_plan"] = [
                {"relative_path": entry.relative_path, "status": "deleted"}
                for entry in delta["deleted"]
            ]
        return payload


def scan_source_manifest(source: str | Path) -> list[ManifestEntry]:
    root = Path(source).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(root)
    if root.is_file():
        if not _should_track_file(root):
            return []
        return [_manifest_entry_for(root, base_dir=root.parent)]

    entries: list[ManifestEntry] = []
    for current_root, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in IGNORED_DIR_NAMES)
        base = Path(current_root)
        for filename in sorted(filenames):
            file_path = base / filename
            if not _should_track_file(file_path):
                continue
            entries.append(_manifest_entry_for(file_path, base_dir=root))
    return entries


def diff_manifest_entries(
    current_entries: list[ManifestEntry],
    previous_entries: dict[str, ManifestEntry],
) -> dict[str, list[ManifestEntry]]:
    current_map = {entry.relative_path: entry for entry in current_entries}
    delta = {name: [] for name in ("new", "modified", "unchanged", "deleted")}

    for relative_path in sorted(current_map):
        current = current_map[relative_path]
        previous = previous_entries.get(relative_path)
        if previous is None:
            delta["new"].append(current)
            continue
        if current == previous:
            delta["unchanged"].append(current)
            continue
        delta["modified"].append(current)

    for relative_path in sorted(set(previous_entries) - set(current_map)):
        delta["deleted"].append(previous_entries[relative_path])
    return delta


def _manifest_entry_for(file_path: Path, *, base_dir: Path) -> ManifestEntry:
    stat = file_path.stat()
    return ManifestEntry(
        relative_path=file_path.relative_to(base_dir).as_posix(),
        sha256=_sha256_for_file(file_path),
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
    )


def _should_track_file(file_path: Path) -> bool:
    suffix = file_path.suffix.lower()
    if suffix in IGNORED_SUFFIXES:
        return False
    if suffix in TEXT_SUFFIXES:
        return True
    return _looks_like_text(file_path)


def _looks_like_text(file_path: Path) -> bool:
    try:
        sample = file_path.read_bytes()[:2048]
    except OSError:
        return False
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _sha256_for_file(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        while True:
            chunk = handle.read(65_536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()
