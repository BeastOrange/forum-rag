"""Small read-only installation/index checks; never initializes or repairs data."""
from __future__ import annotations

import os
import platform
import sqlite3
import time
from contextlib import closing
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

_REQUIRED = {
    'topics': {'topic_id', 'title', 'url', 'content_hash', 'community_title'},
    'chunks': {'chunk_id', 'topic_id', 'chunk_index', 'chunk_level', 'start_offset',
               'end_offset', 'content', 'content_hash'},
}


def diagnose(db_path: str | Path) -> dict:
    """Report local readiness without model imports, writes, or a client probe."""
    path = Path(db_path).resolve()
    try:
        sdk = version('mcp')
    except PackageNotFoundError:
        sdk = None
    sdk_ok = bool(sdk and sdk.split('.')[0] == '2')
    result = {'status': 'unavailable', 'db': str(path), 'python': platform.python_version(),
              'sqlite': sqlite3.sqlite_version, 'mcp_version': sdk,
              'mcp_compatible': sdk_ok, 'client_connection': 'unverified',
              'forum_tables': {}, 'missing_columns': {}, 'fts_available': False,
              'actions': []}
    if not sdk_ok:
        result['actions'].append('Run uv sync --locked, then restart the MCP client.')
    if not path.is_file():
        result['reason'] = 'Database file is missing; no file was created.'
        result['actions'].append('Select an existing index with --db, or explicitly import a forum export first.')
        return result
    deadline = time.monotonic() + 1.0
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0.25)) as conn:
            conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            conn.execute('BEGIN')
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            result['forum_tables'] = {name: name in tables for name in _REQUIRED}
            for name, columns in _REQUIRED.items():
                actual = {row[1] for row in conn.execute(f'PRAGMA table_info({name})')}
                if columns - actual:
                    result['missing_columns'][name] = sorted(columns - actual)
            if 'document_fts' in tables:
                columns = {row[1] for row in conn.execute('PRAGMA table_info(document_fts)')}
                missing = {'kind', 'record_id', 'topic_id', 'title', 'community', 'content'} - columns
                if missing:
                    result['missing_columns']['document_fts'] = sorted(missing)
            if result['missing_columns']:
                result['reason'] = 'Forum schema is missing required tables or columns.'
                result['actions'].append('Check the selected database; explicitly re-import using a compatible version if needed.')
            else:
                if 'document_fts' in tables:
                    # Compile a MATCH query without scanning index content.
                    conn.execute('EXPLAIN SELECT rowid FROM document_fts WHERE document_fts MATCH ? LIMIT 0', ('probe',)).fetchall()
                    result['fts_available'] = True
                result['status'] = 'ready' if result['fts_available'] and sdk_ok else 'degraded'
                if not result['fts_available']:
                    result['actions'].append('Run search-reindex explicitly to restore FTS; compatibility search may be slower.')
            result['chinese_index_enabled'] = os.environ.get('FORUM_RAG_CHINESE_INDEX') == '1'
            result['chinese_index_present'] = 'chinese_fts_v2' in tables
    except (sqlite3.Error, OSError) as exc:
        result.update(status='timeout' if time.monotonic() >= deadline else 'error', reason=str(exc))
        result['actions'].append('Check database permissions, locks and SQLite compatibility; no automatic repair was attempted.')
    return result
