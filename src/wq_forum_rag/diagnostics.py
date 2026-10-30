"""Small read-only installation/index checks; never initializes or repairs data."""

from __future__ import annotations

import os
import platform
import re
import sqlite3
import time
from contextlib import closing
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

_REQUIRED = {
    "topics": {"topic_id", "title", "url", "content_hash", "community_title"},
    "chunks": {
        "chunk_id",
        "topic_id",
        "chunk_index",
        "chunk_level",
        "start_offset",
        "end_offset",
        "content",
        "content_hash",
    },
}
# Columns read by search_docs, including the initial-chunk title lookup.
_DOCUMENT_REQUIRED = {
    "documents": {"slug", "title", "source_path"},
    "doc_chunks": {"chunk_id", "slug", "chunk_index", "content"},
}


def diagnose(db_path: str | Path) -> dict:
    """Report local readiness without model imports, writes, or a client probe."""
    path = Path(db_path).resolve()
    try:
        sdk = version("mcp")
    except PackageNotFoundError:
        sdk = None
    sdk_ok = bool(sdk and sdk.split(".")[0] == "2")
    result = {
        "status": "unavailable",
        "db": str(path),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "mcp_version": sdk,
        "mcp_compatible": sdk_ok,
        "client_connection": "unverified",
        "forum_tables": {},
        "source_status": {},
        "missing_columns": {},
        "fts_available": False,
        "actions": [],
    }
    if not sdk_ok:
        result["actions"].append("Run uv sync --locked, then restart the MCP client.")
    if not path.is_file():
        result["reason"] = "Database file is missing; no file was created."
        result["actions"].append(
            "Select an existing index with --db, or explicitly import forum or document sources first."
        )
        return result
    deadline = time.monotonic() + 1.0
    try:
        with closing(
            sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.25)
        ) as conn:
            conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            conn.execute("BEGIN")
            table_sql = dict(
                conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table'")
            )
            tables = set(table_sql)
            result["forum_tables"] = {name: name in tables for name in _REQUIRED}
            for source, required in (
                ("forum", _REQUIRED),
                ("documents", _DOCUMENT_REQUIRED),
            ):
                present = {name: name in tables for name in required}
                missing = {}
                if any(present.values()):
                    for name, columns in required.items():
                        actual = {
                            row[1] for row in conn.execute(f"PRAGMA table_info({name})")
                        }
                        if columns - actual:
                            missing[name] = sorted(columns - actual)
                status = (
                    "absent"
                    if not any(present.values())
                    else "incomplete"
                    if missing
                    else "available"
                )
                result["source_status"][source] = {
                    "status": status,
                    "tables": present,
                    "missing_columns": missing,
                }
                result["missing_columns"].update(missing)

            fts_invalid = False
            if "document_fts" in tables:
                columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(document_fts)")
                }
                missing = {
                    "kind",
                    "record_id",
                    "topic_id",
                    "title",
                    "community",
                    "content",
                } - columns
                if missing:
                    result["missing_columns"]["document_fts"] = sorted(missing)
                    fts_invalid = True
                elif not re.match(
                    r"CREATE\s+VIRTUAL\s+TABLE\b[\s\S]*?\bUSING\s+fts5\s*\(",
                    table_sql["document_fts"] or "",
                    re.IGNORECASE,
                ):
                    fts_invalid = True
                else:
                    # Compile a MATCH query without scanning index content.
                    conn.execute(
                        "EXPLAIN SELECT rowid FROM document_fts WHERE document_fts MATCH ? LIMIT 0",
                        ("probe",),
                    ).fetchall()
                    result["fts_available"] = True
            available = any(
                source["status"] == "available"
                for source in result["source_status"].values()
            )
            incomplete = [
                name
                for name, source in result["source_status"].items()
                if source["status"] == "incomplete"
            ]
            if incomplete:
                result["actions"].append(
                    "Check incomplete source schemas: "
                    + ", ".join(incomplete)
                    + "; explicitly re-import those sources using a compatible version if needed."
                )
            if fts_invalid:
                result["reason"] = (
                    "FTS table has incompatible columns or is not an FTS5 index."
                )
                result["actions"].append(
                    "Check the selected FTS schema; explicitly rebuild using a compatible version if needed."
                )
            elif not available:
                result["reason"] = (
                    "No complete forum or document source schema is available."
                )
                if not incomplete:
                    result["actions"].append(
                        "Select a database containing an imported source, or explicitly import forum or document sources."
                    )
            else:
                result["status"] = (
                    "ready"
                    if result["fts_available"] and sdk_ok and not incomplete
                    else "degraded"
                )
                if incomplete:
                    result["reason"] = (
                        "At least one source is available, but another source schema is incomplete."
                    )
                if not result["fts_available"]:
                    result["actions"].append(
                        "Run search-reindex explicitly to restore FTS; compatibility search may be slower."
                    )
            result["chinese_index_enabled"] = (
                os.environ.get("FORUM_RAG_CHINESE_INDEX") == "1"
            )
            result["chinese_index_present"] = "chinese_fts_v2" in tables
            if time.monotonic() >= deadline:
                raise TimeoutError("Diagnosis exceeded its one-second budget")
    except (sqlite3.Error, OSError) as exc:
        result.update(
            status="timeout" if time.monotonic() >= deadline else "error",
            reason=str(exc),
        )
        result["actions"].append(
            "Check database permissions, locks and SQLite compatibility; no automatic repair was attempted."
        )
    return result
