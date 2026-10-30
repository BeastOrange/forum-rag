from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Any

from wq_forum_rag.embeddings import EmbeddingBackend, build_embedding_backend
from wq_forum_rag.search import ForumSearcher
from wq_forum_rag.search_cache import CachedEmbeddingBackend

# Upper bound on knowledge candidates so a large page count cannot
# turn a top_k lookup into a full-index scan.
_MAX_KNOWLEDGE_CANDIDATES = 2_000


def search_forum_records(
    conn: sqlite3.Connection,
    query: str,
    *,
    top_k: int = 5,
    embedding_backend: EmbeddingBackend | None = None,
) -> list[dict[str, Any]]:
    if top_k <= 0 or not query.strip():
        return []
    rows = _forum_candidate_rows(conn, query, top_k=max(top_k * 20, 80))
    if not rows or top_k <= 0:
        return []

    backend = _backend(conn, embedding_backend)
    hits = ForumSearcher(rows, embedding_backend=backend).search(
        query=query, top_k=len(rows)
    )
    meta = {row["topic_id"]: row for row in rows}
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for hit in hits:
        if hit.topic_id in seen:
            continue
        seen.add(hit.topic_id)
        results.append(_hit_payload(hit, meta[hit.topic_id]))
        if len(results) >= top_k:
            break
    return results


def search_knowledge_records(
    conn: sqlite3.Connection,
    query: str,
    *,
    top_k: int = 5,
    include_drafts: bool = False,
    embedding_backend: EmbeddingBackend | None = None,
) -> list[dict[str, Any]]:
    if top_k <= 0 or not query.strip():
        return []
    from wq_forum_rag.knowledge_quality import eligible_knowledge_slugs

    eligible = None if include_drafts else eligible_knowledge_slugs(conn)
    if eligible == set():
        return []
    pages = _knowledge_candidate_rows(
        conn,
        query,
        top_k=max(top_k * 20, 80),
        include_drafts=include_drafts,
        eligible=eligible,
    )
    if not pages or top_k <= 0:
        return []

    chunks = [
        {
            "topic_id": page["slug"],
            "chunk_id": page["slug"],
            "community": "knowledge",
            "title": page["title"],
            "content": f"{page['summary']}\n{page['body']}",
        }
        for page in pages
    ]
    hits = ForumSearcher(
        chunks,
        embedding_backend=_backend(conn, embedding_backend),
        dense_weight=0.45,
        lexical_weight=0.55,
    ).search(query=query, top_k=len(pages))
    return _knowledge_results(hits, pages, _knowledge_link_counts(conn), top_k)


def _doc_title_key(value: str) -> str:
    # Match lexical search's Unicode lowercase handling and normalize spacing.
    return " ".join((value or "").lower().split())


def search_doc_records(
    conn: sqlite3.Connection,
    query: str,
    *,
    top_k: int = 5,
    embedding_backend: EmbeddingBackend | None = None,
) -> list[dict[str, Any]]:
    if top_k <= 0 or not query.strip():
        return []
    rows = _doc_candidate_rows(conn, query, top_k=max(top_k * 20, 80))
    if not rows or top_k <= 0:
        return []

    chunks = [
        {
            "topic_id": row["slug"],
            "chunk_id": row["chunk_id"],
            "community": "docs",
            "title": row["title"],
            "content": row["content"],
        }
        for row in rows
    ]
    hits = ForumSearcher(
        chunks,
        embedding_backend=_backend(conn, embedding_backend),
        dense_weight=0.55,
        lexical_weight=0.45,
    ).search(query=query, top_k=len(rows))
    title_key = _doc_title_key(query)
    # A named-document lookup has direct evidence stronger than hash similarity.
    # Keep the existing score/order inside each tier instead of inventing weights.
    hits.sort(key=lambda hit: _doc_title_key(hit.title) != title_key)
    meta = {row["slug"]: row for row in rows}
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for hit in hits:
        if hit.topic_id in seen:
            continue
        seen.add(hit.topic_id)
        row = meta[hit.topic_id]
        results.append(
            {
                "slug": row["slug"],
                "title": row["title"],
                "source_path": row["source_path"],
                "snippet": hit.snippet,
                "score": hit.score,
                "exact_title_match": _doc_title_key(row["title"]) == title_key,
            }
        )
        if len(results) >= top_k:
            break
    return results


def _backend(conn: sqlite3.Connection, embedding_backend: EmbeddingBackend | None):
    if isinstance(embedding_backend, CachedEmbeddingBackend):
        return embedding_backend
    from wq_forum_rag.search_index import _table_exists

    db_path = _db_path(conn)
    if db_path and _table_exists(conn, "document_fts"):
        return CachedEmbeddingBackend(
            db_path, backend=embedding_backend, writable=embedding_backend is not None
        )
    return embedding_backend or build_embedding_backend()


def _forum_detail_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    from wq_forum_rag.search_index import _table_exists

    if not _table_exists(conn, "chunks") or not _table_exists(conn, "topics"):
        return []
    return conn.execute(
        """
        SELECT c.chunk_id, c.topic_id, c.community_id, c.content, t.title,
               t.community_title, t.author, t.created_at, t.url, t.vote_num, t.comment_count
        FROM chunks AS c
        JOIN topics AS t ON t.topic_id = c.topic_id
        WHERE c.chunk_level = 1
        ORDER BY c.topic_id, c.chunk_index
        """
    ).fetchall()


def _forum_candidate_rows(
    conn: sqlite3.Connection, query: str, *, top_k: int
) -> list[dict[str, Any]]:
    from wq_forum_rag.search_index import candidate_ids

    ids = candidate_ids(conn, query, kind="forum_chunk", limit=top_k)
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT c.chunk_id, c.topic_id, c.community_id, c.content, t.title,
               t.community_title, t.author, t.created_at, t.url, t.vote_num, t.comment_count
        FROM chunks c JOIN topics t ON t.topic_id = c.topic_id
        WHERE c.chunk_level = 1 AND c.chunk_id IN ({placeholders})
    """,
        ids,
    ).fetchall()
    return [dict(row) for row in rows]


def _knowledge_detail_rows(
    conn: sqlite3.Connection, *, include_drafts: bool
) -> list[dict[str, Any]]:
    from wq_forum_rag.search_index import _table_exists

    if not _table_exists(conn, "knowledge_pages"):
        return []
    where = "" if include_drafts else "WHERE status = 'published'"
    rows = conn.execute(
        f"""
        SELECT slug, title, summary, body, status, confidence
        FROM knowledge_pages
        {where}
        ORDER BY status DESC, confidence DESC, updated_at DESC, slug
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _knowledge_candidate_rows(
    conn: sqlite3.Connection,
    query: str,
    *,
    top_k: int,
    include_drafts: bool,
    eligible: set[str] | None = None,
):
    from wq_forum_rag.search_index import candidate_ids

    # Filter trust before truncating: many stale/conflicted high-ranking pages
    # must not crowd an eligible page out of the candidate pool. Only identifiers
    # are over-fetched; text scoring remains bounded by top_k.
    # candidate_ids reserves half its budget for Chinese literal matches. Over-fetch
    # published identifiers so trust filtering cannot crowd an eligible page out,
    # but keep the total bounded for very large knowledge bases.
    limit = (
        top_k
        if eligible is None
        else min(
            2
            * conn.execute(
                "SELECT COUNT(*) FROM knowledge_pages WHERE status='published'"
            ).fetchone()[0],
            _MAX_KNOWLEDGE_CANDIDATES,
        )
    )
    ids = candidate_ids(
        conn, query, kind="knowledge_page", limit=limit, include_drafts=include_drafts
    )
    if eligible is not None:
        ids = [slug for slug in ids if slug in eligible][:top_k]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT slug, title, summary, body, status, confidence FROM knowledge_pages
        WHERE slug IN ({placeholders}) AND (? OR status = 'published')
    """,
        (*ids, int(include_drafts)),
    ).fetchall()
    return [dict(row) for row in rows]


def _doc_detail_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    from wq_forum_rag.search_index import _table_exists

    if not _table_exists(conn, "doc_chunks") or not _table_exists(conn, "documents"):
        return []
    rows = conn.execute(
        """
        SELECT c.chunk_id, c.slug, c.content, d.title, d.source_path
        FROM doc_chunks AS c
        JOIN documents AS d ON d.slug = c.slug
        ORDER BY c.slug, c.chunk_index
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _doc_candidate_rows(
    conn: sqlite3.Connection, query: str, *, top_k: int
) -> list[dict[str, Any]]:
    from wq_forum_rag.search_index import _table_exists, candidate_ids

    if not _table_exists(conn, "documents") or not _table_exists(conn, "doc_chunks"):
        return []
    title_key = _doc_title_key(query)
    exact_slugs = []
    # Stream title metadata only; registering a SQLite function on a caller's
    # connection would fail if it already has an active cursor.
    with closing(
        conn.execute("SELECT slug, title FROM documents ORDER BY slug")
    ) as titles:
        for slug, title in titles:
            if _doc_title_key(title) == title_key:
                exact_slugs.append(slug)
                if len(exact_slugs) >= top_k:
                    break
    # Reserve a bounded candidate slot for each exact title, even if the FTS
    # budget omitted it. Fetch one initial chunk per document, never its body.
    exact = []
    if exact_slugs:
        marks = ",".join("?" for _ in exact_slugs)
        exact = conn.execute(
            f"""
        SELECT c.chunk_id
        FROM documents d JOIN doc_chunks c ON c.chunk_id = (
            SELECT first.chunk_id FROM doc_chunks first WHERE first.slug=d.slug
            ORDER BY first.chunk_index, first.chunk_id LIMIT 1
        )
        WHERE d.slug IN ({marks})
        ORDER BY d.slug LIMIT ?
        """,
            (*exact_slugs, top_k),
        ).fetchall()
    ids = list(
        dict.fromkeys(
            [str(row[0]) for row in exact]
            + candidate_ids(conn, query, kind="doc_chunk", limit=top_k)
        )
    )[:top_k]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT c.chunk_id, c.slug, c.content, d.title, d.source_path
        FROM doc_chunks c JOIN documents d ON d.slug = c.slug
        WHERE c.chunk_id IN ({placeholders})
    """,
        ids,
    ).fetchall()
    return [dict(row) for row in rows]


def _knowledge_results(
    hits: Any, pages: list[dict[str, Any]], link_counts: dict[str, int], top_k: int
):
    pages_by_slug = {page["slug"]: page for page in pages}
    results = []
    for hit in hits:
        page = pages_by_slug[hit.topic_id]
        boost = min(link_counts.get(hit.topic_id, 0) * 0.04, 0.24)
        results.append(
            {
                **{
                    k: page[k]
                    for k in ("slug", "title", "summary", "status", "confidence")
                },
                "score": hit.score + boost,
                "backlink_boost": boost,
                "snippet": hit.snippet,
            }
        )
    results.sort(
        key=lambda item: (item["score"], item["confidence"], item["slug"]), reverse=True
    )
    return results[:top_k]


def _knowledge_link_counts(conn: sqlite3.Connection) -> dict[str, int]:
    from wq_forum_rag.search_index import _table_exists

    if not _table_exists(conn, "knowledge_links"):
        return {}
    rows = conn.execute(
        "SELECT target_slug, COUNT(*) AS link_count FROM knowledge_links "
        "WHERE relation_type IN ('supports', 'refines') GROUP BY target_slug"
    ).fetchall()
    return {str(row["target_slug"]): int(row["link_count"]) for row in rows}


def _db_path(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("PRAGMA database_list").fetchone()
    return None if row is None or not row[2] else str(row[2])


def _hit_payload(hit: Any, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "topic_id": hit.topic_id,
        "community_id": row["community_id"],
        "community_title": hit.community,
        "title": hit.title,
        "author": row["author"],
        "datetime": row["created_at"],
        "url": row["url"],
        "vote_num": row["vote_num"],
        "comment_num": row["comment_count"],
        "snippet": hit.snippet,
        "score": hit.score,
    }
