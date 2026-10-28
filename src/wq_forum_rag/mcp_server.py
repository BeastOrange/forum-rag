# -*- coding: utf-8 -*-
# Author: konrad
# WQ_ID: OB53521
# Encoding: utf-8

from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path
from typing import Any

from .cli import DEFAULT_DB_PATH, ForumIndexService
from .documents import DocumentStore, ingest_documents
from .evolution import EvolutionService
from .indexer import ForumDatabaseBusyError, raise_if_database_locked
from .manifest import SourceManifestService
from .search_cache import CachedEmbeddingBackend
from .search_index import rebuild_search_index as rebuild_search_index_impl
from .search_records import search_doc_records, search_knowledge_records
from .storage import ForumStore

try:
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError:  # pragma: no cover
    class FastMCP:  # type: ignore[override]
        def __init__(self, name: str, **_: Any) -> None:
            self.name = name
            self.tools: dict[str, Any] = {}

        def tool(self, **_: Any):
            def decorator(func: Any) -> Any:
                self.tools[func.__name__] = func
                return func

            return decorator

        def run(self, transport: str = "stdio") -> None:
            raise RuntimeError(f"mcp is not installed; cannot run transport={transport}")


def _resolve_db_path(db: str | None) -> Path:
    configured_db = os.environ.get("FORUM_RAG_DB") or os.environ.get("WQ_FORUM_RAG_DB")
    return Path(db or configured_db or DEFAULT_DB_PATH)


def _busy_payload(exc: ForumDatabaseBusyError, **extra: Any) -> dict[str, Any]:
    return {**extra, **exc.payload}


def search_forum(query: str, db: str | None = None, top_k: int = 5) -> dict[str, Any]:
    db_path = _resolve_db_path(db)
    try:
        results = ForumIndexService(db_path).search(query=query, top_k=top_k)
    except ForumDatabaseBusyError as exc:
        return _busy_payload(exc, query=query, results=[])
    return {"query": query, "db": str(db_path), "results": results}


async def search_semantic_forum(
    query: str, db: str | None = None, top_k: int = 5,
    index_dir: str | None = None, model_dir: str | None = None,
    min_score: float = 0.5, mode: str = "semantic",
) -> dict[str, Any]:
    """Search a separately built local model index, or fuse it with lexical hits.

    Requires explicit offline model/index preparation. Raw cosine is not a
    confidence probability; min_score needs dataset-specific calibration.
    """
    db_path = _resolve_db_path(db)
    index_dir = index_dir or os.environ.get("FORUM_RAG_SEMANTIC_INDEX")
    model_dir = model_dir or os.environ.get("FORUM_RAG_MODEL_DIR")
    base = {"query": query, "db": str(db_path)}
    if not index_dir or not model_dir:
        return {**base, "status": "not_configured", "results": [],
                "reason": "Build a local semantic index and supply index_dir/model_dir."}
    from .semantic_retrieval import search_semantic_forum as retrieve

    try:
        result = await asyncio.to_thread(
            retrieve, db_path, query, index_dir=index_dir, model_dir=model_dir,
            top_k=top_k, min_score=min_score, mode=mode,
        )
    except sqlite3.OperationalError as exc:
        try:
            raise_if_database_locked(exc, db_path)
        except ForumDatabaseBusyError as busy:
            return _busy_payload(busy, **base, results=[])
        raise
    return {**base, **result}


def get_post(topic_id: str, db: str | None = None) -> dict[str, Any]:
    db_path = _resolve_db_path(db)
    try:
        post = ForumIndexService(db_path).get_post(topic_id)
    except ForumDatabaseBusyError as exc:
        return _busy_payload(exc, topic_id=topic_id, post=None)
    return {"topic_id": topic_id, "db": str(db_path), "post": post}


def find_by_exact(
    value: str,
    db: str | None = None,
    community: str | None = None,
    top_k: int = 5,
) -> dict[str, Any]:
    db_path = _resolve_db_path(db)
    try:
        results = ForumIndexService(db_path).find_by_exact(
            value,
            community=community,
            top_k=top_k,
        )
    except ForumDatabaseBusyError as exc:
        return _busy_payload(exc, value=value, results=[])
    return {"value": value, "db": str(db_path), "results": results}


def related_posts(topic_id: str, db: str | None = None, top_k: int = 5) -> dict[str, Any]:
    db_path = _resolve_db_path(db)
    try:
        posts = ForumIndexService(db_path).related_posts(topic_id=topic_id, top_k=top_k)
    except ForumDatabaseBusyError as exc:
        return _busy_payload(exc, topic_id=topic_id, results=[])
    return {"topic_id": topic_id, "db": str(db_path), "results": posts}


def source_status(source: str, db: str | None = None) -> dict[str, Any]:
    db_path = _resolve_db_path(db)
    try:
        return SourceManifestService(db_path).source_status(source)
    except ForumDatabaseBusyError as exc:
        return _busy_payload(exc, source=source, counts={}, files={})
    except sqlite3.OperationalError as exc:
        try:
            raise_if_database_locked(exc, db_path)
        except ForumDatabaseBusyError as busy:
            return _busy_payload(busy, source=source, counts={}, files={})
        raise


def source_ingest_plan(
    source: str,
    db: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    db_path = _resolve_db_path(db)
    try:
        return SourceManifestService(db_path).source_ingest_plan(source, commit=commit)
    except ForumDatabaseBusyError as exc:
        return _busy_payload(exc, source=source, ingest_plan=[], delete_plan=[])
    except sqlite3.OperationalError as exc:
        try:
            raise_if_database_locked(exc, db_path)
        except ForumDatabaseBusyError as busy:
            return _busy_payload(busy, source=source, ingest_plan=[], delete_plan=[])
        raise


def build_evolution_context(query: str, db: str | None = None, top_k: int = 5) -> dict[str, Any]:
    db_path = _resolve_db_path(db)
    try:
        context = EvolutionService(db_path).build_evolution_context(
            query=query,
            top_k=top_k,
        )
    except ForumDatabaseBusyError as exc:
        return _busy_payload(exc, query=query, published_knowledge=[], forum_evidence=[])
    return {"query": query, "db": str(db_path), **context}


def propose_knowledge_page(
    slug: str,
    title: str,
    summary: str,
    body: str,
    source_topic_ids: list[str],
    confidence: float,
    db: str | None = None,
    links: list[dict[str, Any]] | None = None,
    auto_publish: bool = True,
    source_evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    db_path = _resolve_db_path(db)
    try:
        result = EvolutionService(db_path).propose_knowledge_page(
            slug=slug,
            title=title,
            summary=summary,
            body=body,
            source_topic_ids=source_topic_ids,
            confidence=confidence,
            links=links,
            auto_publish=auto_publish,
            source_evidence=source_evidence,
        )
    except ForumDatabaseBusyError as exc:
        return _busy_payload(exc, page=None, issues=[], auto_published=False)
    except sqlite3.OperationalError as exc:
        try:
            raise_if_database_locked(exc, db_path)
        except ForumDatabaseBusyError as busy:
            return _busy_payload(busy, page=None, issues=[], auto_published=False)
        raise
    return {"db": str(db_path), **result}


def search_knowledge(
    query: str,
    db: str | None = None,
    top_k: int = 5,
    include_drafts: bool = False,
) -> dict[str, Any]:
    db_path = _resolve_db_path(db)
    try:
        with ForumStore(db_path, initialize=False) as store:
            results = search_knowledge_records(
                store.conn,
                query=query,
                top_k=top_k,
                include_drafts=include_drafts,
                embedding_backend=CachedEmbeddingBackend(db_path, writable=False),
            )
    except ForumDatabaseBusyError as exc:
        return _busy_payload(exc, query=query, results=[])
    except sqlite3.OperationalError as exc:
        try:
            raise_if_database_locked(exc, db_path)
        except ForumDatabaseBusyError as busy:
            return _busy_payload(busy, query=query, results=[])
        raise
    return {"query": query, "db": str(db_path), "results": results}


def get_knowledge_page(slug: str, db: str | None = None) -> dict[str, Any]:
    db_path = _resolve_db_path(db)
    try:
        page = EvolutionService(db_path).get_knowledge_page(slug)
    except ForumDatabaseBusyError as exc:
        return _busy_payload(exc, slug=slug, page=None)
    except sqlite3.OperationalError as exc:
        try:
            raise_if_database_locked(exc, db_path)
        except ForumDatabaseBusyError as busy:
            return _busy_payload(busy, slug=slug, page=None)
        raise
    return {"slug": slug, "db": str(db_path), "page": page}


def link_knowledge_pages(
    source_slug: str,
    target_slug: str,
    relation_type: str,
    db: str | None = None,
    weight: float = 1.0,
    confidence: float = 0.8,
) -> dict[str, Any]:
    db_path = _resolve_db_path(db)
    try:
        link = EvolutionService(db_path).link_knowledge_pages(
            source_slug,
            target_slug,
            relation_type,
            weight=weight,
            confidence=confidence,
        )
    except ForumDatabaseBusyError as exc:
        return _busy_payload(exc, link=None)
    except sqlite3.OperationalError as exc:
        try:
            raise_if_database_locked(exc, db_path)
        except ForumDatabaseBusyError as busy:
            return _busy_payload(busy, link=None)
        raise
    return {"db": str(db_path), "link": link}


def lint_knowledge(db: str | None = None, slug: str | None = None) -> dict[str, Any]:
    db_path = _resolve_db_path(db)
    try:
        result = EvolutionService(db_path).lint_knowledge(slug=slug)
    except ForumDatabaseBusyError as exc:
        return _busy_payload(exc, slug=slug, issues=[], blocking_count=0, warning_count=0)
    except sqlite3.OperationalError as exc:
        try:
            raise_if_database_locked(exc, db_path)
        except ForumDatabaseBusyError as busy:
            return _busy_payload(busy, slug=slug, issues=[], blocking_count=0, warning_count=0)
        raise
    return {"db": str(db_path), **result}


def publish_knowledge_page(slug: str, db: str | None = None) -> dict[str, Any]:
    db_path = _resolve_db_path(db)
    try:
        result = EvolutionService(db_path).publish_knowledge_page(slug)
    except ForumDatabaseBusyError as exc:
        return _busy_payload(exc, published=False, page=None, issues=[])
    except sqlite3.OperationalError as exc:
        try:
            raise_if_database_locked(exc, db_path)
        except ForumDatabaseBusyError as busy:
            return _busy_payload(busy, published=False, page=None, issues=[])
        raise
    return {"db": str(db_path), **result}


def review_knowledge_page(
    slug: str, action: str, note: str, db: str | None = None, replacement_slug: str | None = None,
) -> dict[str, Any]:
    """Record a human/agent review, request review, or archive with a reason.

    A located excerpt is not proof of a claim; the reviewer assesses its support.
    """
    db_path = _resolve_db_path(db)
    try:
        page = EvolutionService(db_path).review_knowledge_page(
            slug, action, note=note, replacement_slug=replacement_slug)
    except sqlite3.OperationalError as exc:
        try:
            raise_if_database_locked(exc, db_path)
        except ForumDatabaseBusyError as busy:
            return _busy_payload(busy, page=None)
        raise
    return {"db": str(db_path), "page": page}


def graph_query(
    slug: str,
    db: str | None = None,
    depth: int = 1,
    relation_type: str | None = None,
) -> dict[str, Any]:
    db_path = _resolve_db_path(db)
    try:
        result = EvolutionService(db_path).graph_query(
            slug,
            depth=depth,
            relation_type=relation_type,
        )
    except ForumDatabaseBusyError as exc:
        return _busy_payload(exc, start=slug, depth=depth, nodes=[], edges=[])
    except sqlite3.OperationalError as exc:
        try:
            raise_if_database_locked(exc, db_path)
        except ForumDatabaseBusyError as busy:
            return _busy_payload(busy, start=slug, depth=depth, nodes=[], edges=[])
        raise
    return {"db": str(db_path), **result}


def export_knowledge_wiki(
    db: str | None = None,
    output_dir: str = ".cache/wiki",
    include_drafts: bool = False,
) -> dict[str, Any]:
    db_path = _resolve_db_path(db)
    try:
        result = EvolutionService(db_path).export_wiki(
            output_dir,
            include_drafts=include_drafts,
        )
    except ForumDatabaseBusyError as exc:
        return _busy_payload(exc, output_dir=str(output_dir), page_count=0, written=[])
    except sqlite3.OperationalError as exc:
        try:
            raise_if_database_locked(exc, db_path)
        except ForumDatabaseBusyError as busy:
            return _busy_payload(busy, output_dir=str(output_dir), page_count=0, written=[])
        raise
    return {"db": str(db_path), **result}


def rebuild_search_index(db: str | None = None) -> dict[str, Any]:
    db_path = _resolve_db_path(db)
    try:
        result = rebuild_search_index_impl(db_path)
    except ForumDatabaseBusyError as exc:
        return _busy_payload(
            exc,
            fts_available=False,
            forum_docs=0,
            knowledge_docs=0,
            doc_chunks=0,
            indexed_documents=0,
        )
    except sqlite3.OperationalError as exc:
        try:
            raise_if_database_locked(exc, db_path)
        except ForumDatabaseBusyError as busy:
            return _busy_payload(
                busy,
                fts_available=False,
                forum_docs=0,
                knowledge_docs=0,
                doc_chunks=0,
                indexed_documents=0,
            )
        raise
    return {"db": str(db_path), **result}


def search_docs(query: str, db: str | None = None, top_k: int = 5) -> dict[str, Any]:
    db_path = _resolve_db_path(db)
    try:
        with ForumStore(db_path, initialize=False) as store:
            results = search_doc_records(
                store.conn,
                query=query,
                top_k=top_k,
                embedding_backend=CachedEmbeddingBackend(db_path, writable=False),
            )
    except ForumDatabaseBusyError as exc:
        return _busy_payload(exc, query=query, results=[])
    except sqlite3.OperationalError as exc:
        try:
            raise_if_database_locked(exc, db_path)
        except ForumDatabaseBusyError as busy:
            return _busy_payload(busy, query=query, results=[])
        raise
    return {"query": query, "db": str(db_path), "results": results}


def get_doc(slug: str, db: str | None = None) -> dict[str, Any]:
    db_path = _resolve_db_path(db)
    try:
        with DocumentStore(db_path, initialize=False) as store:
            document = store.get_document(slug)
    except ForumDatabaseBusyError as exc:
        return _busy_payload(exc, slug=slug, document=None)
    except sqlite3.OperationalError as exc:
        try:
            raise_if_database_locked(exc, db_path)
        except ForumDatabaseBusyError as busy:
            return _busy_payload(busy, slug=slug, document=None)
        raise
    return {"slug": slug, "db": str(db_path), "document": document}


def ingest_docs(
    directory: str,
    db: str | None = None,
    rebuild: bool = False,
    prune: bool = True,
) -> dict[str, Any]:
    db_path = _resolve_db_path(db)
    try:
        result = ingest_documents(directory, db_path, rebuild=rebuild, prune=prune)
        search_index = rebuild_search_index_impl(db_path)
    except ForumDatabaseBusyError as exc:
        return _busy_payload(
            exc,
            directory=str(directory),
            indexed_documents=0,
            pruned=0,
            seen=0,
            inserted=0,
            updated=0,
            skipped=0,
            chunks_written=0,
            search_index={
                "fts_available": False,
                "forum_docs": 0,
                "knowledge_docs": 0,
                "doc_chunks": 0,
                "indexed_documents": 0,
            },
        )
    except sqlite3.OperationalError as exc:
        try:
            raise_if_database_locked(exc, db_path)
        except ForumDatabaseBusyError as busy:
            return _busy_payload(
                busy,
                directory=str(directory),
                indexed_documents=0,
                pruned=0,
                seen=0,
                inserted=0,
                updated=0,
                skipped=0,
                chunks_written=0,
                search_index={
                    "fts_available": False,
                    "forum_docs": 0,
                    "knowledge_docs": 0,
                    "doc_chunks": 0,
                    "indexed_documents": 0,
                },
            )
        raise
    return {**result, "search_index": search_index}


def build_mcp_server(default_db: str | Path | None = None) -> FastMCP:
    if default_db and not (
        os.environ.get("FORUM_RAG_DB") or os.environ.get("WQ_FORUM_RAG_DB")
    ):
        os.environ["FORUM_RAG_DB"] = str(default_db)
    server = FastMCP("forum-rag", json_response=True)
    server.tool()(search_forum)
    server.tool()(search_semantic_forum)
    server.tool()(get_post)
    server.tool()(find_by_exact)
    server.tool()(related_posts)
    server.tool()(source_status)
    server.tool()(source_ingest_plan)
    server.tool()(build_evolution_context)
    server.tool()(propose_knowledge_page)
    server.tool()(search_knowledge)
    server.tool()(get_knowledge_page)
    server.tool()(link_knowledge_pages)
    server.tool()(lint_knowledge)
    server.tool()(publish_knowledge_page)
    server.tool()(review_knowledge_page)
    server.tool()(graph_query)
    server.tool()(export_knowledge_wiki)
    server.tool()(rebuild_search_index)
    server.tool()(search_docs)
    server.tool()(get_doc)
    server.tool()(ingest_docs)
    return server


mcp = build_mcp_server()


def main() -> None:
    mcp.run(transport="stdio")
