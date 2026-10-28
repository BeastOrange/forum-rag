# -*- coding: utf-8 -*-
# Author: konrad
# WQ_ID: OB53521
# Encoding: utf-8

from __future__ import annotations

import re
import sqlite3
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from wq_forum_rag.search import tokenize_text

_candidate_cache: OrderedDict[tuple, tuple[str, ...]] = OrderedDict()
_candidate_lock = threading.RLock()


def _candidate_state(conn: sqlite3.Connection):
    # A caller's explicit read transaction may still see an older WAL snapshot.
    # Never use a process-wide current-file cache for that transaction.
    if conn.in_transaction:
        return None
    row = conn.execute('PRAGMA database_list').fetchone()
    if not row or not row[2]:
        return None
    path = Path(row[2]).resolve()
    values = [str(path)]
    for suffix in ('', '-wal', '-journal'):
        try:
            stat = Path(str(path) + suffix).stat()
            values.append((stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
        except FileNotFoundError:
            values.append(None)
    # A hot rollback journal can change the visible snapshot without rewriting
    # the database itself. Disable caching while a nonempty journal exists.
    if values[-1] is not None and values[-1][2]:
        return None
    return tuple(values)


def init_search_schema(conn: sqlite3.Connection) -> bool:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS embedding_cache (
            backend_id TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            vector_json TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(backend_id, content_hash)
        )
        """
    )
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS document_fts USING fts5(
                kind UNINDEXED, record_id UNINDEXED, topic_id UNINDEXED,
                title, community, content
            )
            """
        )
        return True
    except sqlite3.OperationalError:
        return False


def rebuild_search_index(db_or_conn: str | Path | sqlite3.Connection) -> dict[str, Any]:
    if isinstance(db_or_conn, sqlite3.Connection):
        return _rebuild_with_conn(db_or_conn)
    with sqlite3.connect(db_or_conn) as conn:
        conn.row_factory = sqlite3.Row
        return _rebuild_with_conn(conn)


def fts_candidates(
    db_path: str | Path,
    query: str,
    *,
    kind: str = "forum_chunk",
    limit: int = 200,
    include_drafts: bool = False,
) -> list[str]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return _cached_candidate_ids(conn, query, kind=kind, limit=limit, include_drafts=include_drafts)


def _cached_candidate_ids(conn: sqlite3.Connection, query: str, *, kind: str, limit: int,
                          include_drafts: bool = False) -> list[str]:
    # Only fts_candidates' freshly opened, privately owned connection uses this
    # cache. Arbitrary caller connections can hold implicit SELECT snapshots.
    state = _candidate_state(conn)
    key = (state, query, kind, limit, include_drafts)
    if state is not None:
        with _candidate_lock:
            if key in _candidate_cache:
                result = _candidate_cache.pop(key)
                _candidate_cache[key] = result
                if _candidate_state(conn) == state:
                    return list(result)
    result = _candidate_ids_uncached(conn, query, kind=kind, limit=limit,
                                     include_drafts=include_drafts)
    if state is not None and _candidate_state(conn) == state:
        with _candidate_lock:
            _candidate_cache[key] = tuple(result)
            while len(_candidate_cache) > 128:
                _candidate_cache.popitem(last=False)
    return result


def candidate_ids(conn: sqlite3.Connection, query: str, *, kind: str, limit: int,
                  include_drafts: bool = False) -> list[str]:
    return _candidate_ids_uncached(conn, query, kind=kind, limit=limit,
                                   include_drafts=include_drafts)


def _candidate_ids_uncached(conn: sqlite3.Connection, query: str, *, kind: str, limit: int,
                            include_drafts: bool = False) -> list[str]:
    """Bound candidates; old unicode61 indexes need literal Chinese substring lookup."""
    if limit <= 0 or not query.strip():
        return []
    match_query = _fts_query(query)
    if not match_query:
        return []
    required = {"forum_chunk": ("chunks", "topics"), "doc_chunk": ("doc_chunks", "documents"), "knowledge_page": ("knowledge_pages",)}
    if kind in required and not all(_table_exists(conn, table) for table in required[kind]):
        return []
    available = _table_exists(conn, "document_fts")
    chinese = re.findall(r"[\u4e00-\u9fff]+", query)
    fts_limit = max(1, limit // 2) if chinese and limit > 1 else limit
    ids = _query_fts(conn, kind, match_query, fts_limit, include_drafts) if available else []
    # unicode61 indexes an entire contiguous Chinese run as one token. Search the
    # original records for literal runs, also when an older index is in use.
    terms = chinese.copy()
    if not available:
        terms += re.findall(r"[A-Za-z0-9_]+", query)
    if not terms or len(ids) >= limit:
        return ids
    sources = {
        "forum_chunk": ("chunks c JOIN topics t ON t.topic_id=c.topic_id", "c.chunk_id", "c.chunk_level=1", ("t.title", "t.community_title", "c.content")),
        "doc_chunk": ("doc_chunks c JOIN documents d ON d.slug=c.slug", "c.chunk_id", "1", ("d.title", "c.content")),
        "knowledge_page": ("knowledge_pages p", "p.slug", "1" if include_drafts else "p.status='published'", ("p.title", "p.summary", "p.body")),
    }
    if kind not in sources:
        return ids
    tables, ident, condition, columns = sources[kind]
    required = {"forum_chunk": ("chunks", "topics"), "doc_chunk": ("doc_chunks", "documents"), "knowledge_page": ("knowledge_pages",)}[kind]
    if not all(_table_exists(conn, table) for table in required):
        return ids
    clauses = []
    params = []
    for term in dict.fromkeys(terms):
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        for column in columns:
            clauses.append(f"{column} LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped}%")
    rows = conn.execute(f"SELECT {ident} FROM {tables} WHERE {condition} AND ({' OR '.join(clauses)}) LIMIT ?", (*params, limit)).fetchall()
    seen = set(ids)
    for row in rows:
        value = str(row[0])
        if value not in seen:
            ids.append(value)
            seen.add(value)
            if len(ids) >= limit:
                break
    return ids


def _rebuild_with_conn(conn: sqlite3.Connection) -> dict[str, Any]:
    if not init_search_schema(conn):
        return {
            "fts_available": False,
            "forum_docs": 0,
            "knowledge_docs": 0,
            "doc_chunks": 0,
            "indexed_documents": 0,
        }
    rows = _forum_rows(conn) + _knowledge_rows(conn) + _doc_rows(conn)
    conn.execute("DELETE FROM document_fts")
    conn.executemany(
        """
        INSERT INTO document_fts(kind, record_id, topic_id, title, community, content)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    forum_docs = sum(1 for row in rows if row[0] == "forum_chunk")
    knowledge_docs = sum(1 for row in rows if row[0] == "knowledge_page")
    doc_chunks = sum(1 for row in rows if row[0] == "doc_chunk")
    return {
        "fts_available": True,
        "forum_docs": forum_docs,
        "knowledge_docs": knowledge_docs,
        "doc_chunks": doc_chunks,
        "indexed_documents": len(rows),
    }


def _query_fts(conn: sqlite3.Connection, kind: str, match_query: str, limit: int, include_drafts: bool):
    if kind == "knowledge_page":
        rows = conn.execute(
            """
            SELECT document_fts.record_id
            FROM document_fts
            JOIN knowledge_pages ON knowledge_pages.slug = document_fts.record_id
            WHERE kind = ? AND document_fts MATCH ?
              AND (? OR knowledge_pages.status = 'published')
            ORDER BY rank
            LIMIT ?
            """,
            (kind, match_query, int(include_drafts), limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT record_id FROM document_fts
            WHERE kind = ? AND document_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (kind, match_query, limit),
        ).fetchall()
    return [str(row[0]) for row in rows]


def _forum_rows(conn: sqlite3.Connection) -> list[tuple[str, str, str, str, str, str]]:
    if not _table_exists(conn, "chunks") or not _table_exists(conn, "topics"):
        return []
    rows = conn.execute(
        """
        SELECT c.chunk_id, c.topic_id, c.content, t.title, t.community_title
        FROM chunks AS c
        JOIN topics AS t ON t.topic_id = c.topic_id
        WHERE c.chunk_level = 1
        """
    ).fetchall()
    return [
        ("forum_chunk", row["chunk_id"], row["topic_id"], row["title"], row["community_title"], row["content"])
        for row in rows
    ]


def _knowledge_rows(conn: sqlite3.Connection) -> list[tuple[str, str, str, str, str, str]]:
    if not _table_exists(conn, "knowledge_pages"):
        return []
    rows = conn.execute("SELECT slug, title, summary, body, status FROM knowledge_pages").fetchall()
    return [
        ("knowledge_page", row["slug"], row["slug"], row["title"], row["status"], f"{row['summary']}\n{row['body']}")
        for row in rows
    ]


def _doc_rows(conn: sqlite3.Connection) -> list[tuple[str, str, str, str, str, str]]:
    if not _table_exists(conn, "doc_chunks") or not _table_exists(conn, "documents"):
        return []
    rows = conn.execute(
        """
        SELECT c.chunk_id, c.slug, c.content, d.title
        FROM doc_chunks AS c
        JOIN documents AS d ON d.slug = c.slug
        ORDER BY c.slug, c.chunk_index
        """
    ).fetchall()
    return [
        ("doc_chunk", row["chunk_id"], row["slug"], row["title"], "docs", row["content"])
        for row in rows
    ]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE name = ? LIMIT 1", (name,)).fetchone()
    return row is not None


def _fts_query(query: str) -> str:
    tokens = []
    for token in tokenize_text(query):
        clean = token.replace('"', '""')
        if clean and clean.replace("_", "").isalnum():
            tokens.append(clean)
    return " OR ".join(f'"{token}"' for token in tokens[:12])
