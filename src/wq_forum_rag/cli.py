# Author: konrad
# WQ_ID: OB53521
# Encoding: utf-8

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from wq_forum_rag.documents import DocumentStore
from wq_forum_rag.documents import ingest_documents as ingest_documents_impl
from wq_forum_rag.evolution_cli import register_evolution_commands
from wq_forum_rag.indexer import (
    ForumDatabaseBusyError,
    ForumIndexService,
    raise_if_database_locked,
)
from wq_forum_rag.manifest import SourceManifestService
from wq_forum_rag.search_cache import CachedEmbeddingBackend
from wq_forum_rag.search_index import rebuild_search_index
from wq_forum_rag.search_records import search_doc_records
from wq_forum_rag.source_cli import register_source_commands

DEFAULT_DB_PATH = Path(".cache/forum.sqlite3")
app = typer.Typer(
    no_args_is_help=True, help="Offline lightweight RAG for forum exports"
)
console = Console()


def _print_busy(exc: ForumDatabaseBusyError, **extra: Any) -> None:
    console.print_json(data={**extra, **exc.payload})


def _render_search(results: list[dict[str, Any]]) -> None:
    table = Table(title=f"Top {len(results)}")
    for name in ("topic_id", "community_title", "title", "author", "score"):
        table.add_column(name)
    for item in results:
        table.add_row(
            item["topic_id"],
            item["community_title"],
            item["title"],
            item["author"],
            f"{item['score']:.3f}",
        )
    console.print(table)


@app.command("doctor")
def doctor_command(
    db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db", dir_okay=False),
) -> None:
    """Check dependencies and an existing index without writing or loading models."""
    import json

    from .diagnostics import diagnose

    typer.echo(json.dumps(diagnose(db_path), ensure_ascii=False, indent=2))


@app.command("index")
def index_command(
    json_path: Path = typer.Option(
        ..., "--json", exists=True, dir_okay=False, readable=True
    ),
    db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db", dir_okay=False),
    rebuild: bool = typer.Option(False, "--rebuild", help="Force full rebuild"),
    prune: bool = typer.Option(
        True,
        "--prune/--no-prune",
        help="Delete topics present in DB but absent from new JSON (skipped on --rebuild)",
    ),
) -> None:
    console.print_json(
        data=ForumIndexService(db_path).index_from_json(
            json_path=json_path, rebuild=rebuild, prune=prune
        )
    )


@app.command("refresh")
def refresh_command(
    json_path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db", dir_okay=False),
    rebuild: bool = typer.Option(False, "--rebuild", help="Force full rebuild"),
    prune: bool = typer.Option(
        True,
        "--prune/--no-prune",
        help="Delete topics present in DB but absent from new JSON (skipped on --rebuild)",
    ),
) -> None:
    """Index forum JSON and commit source manifest in one step."""
    index_result = ForumIndexService(db_path).index_from_json(
        json_path=json_path, rebuild=rebuild, prune=prune
    )
    manifest_result = SourceManifestService(db_path).source_ingest_plan(
        json_path, commit=True
    )
    console.print_json(data={"index": index_result, "manifest": manifest_result})


@app.command("search")
def search_command(
    query: str = typer.Argument(..., help="Full-text query"),
    db_path: Path = typer.Option(
        DEFAULT_DB_PATH, "--db", exists=True, dir_okay=False, readable=True
    ),
    top_k: int = typer.Option(5, "--top-k", min=1, max=20),
) -> None:
    try:
        _render_search(ForumIndexService(db_path).search(query=query, top_k=top_k))
    except ForumDatabaseBusyError as exc:
        _print_busy(exc, query=query, results=[])


@app.command("show")
def show_command(
    topic_id: str = typer.Argument(..., help="Forum topic id"),
    db_path: Path = typer.Option(
        DEFAULT_DB_PATH, "--db", exists=True, dir_okay=False, readable=True
    ),
) -> None:
    try:
        post = ForumIndexService(db_path).get_post(topic_id)
    except ForumDatabaseBusyError as exc:
        _print_busy(exc, topic_id=topic_id, post=None)
        return
    if post is None:
        raise typer.Exit(code=1)
    console.print_json(data=post)


@app.command("search-reindex")
def search_reindex_command(
    db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db", exists=True, dir_okay=False),
    chinese: bool = typer.Option(
        False, "--chinese", help="Build the optional Chinese candidate index"
    ),
) -> None:
    try:
        console.print_json(data=rebuild_search_index(db_path, chinese=chinese))
    except sqlite3.OperationalError as exc:
        try:
            raise_if_database_locked(exc, db_path)
        except ForumDatabaseBusyError as busy:
            _print_busy(
                busy,
                fts_available=False,
                forum_docs=0,
                knowledge_docs=0,
                doc_chunks=0,
                indexed_documents=0,
            )
            return
        raise
    except ForumDatabaseBusyError as exc:
        _print_busy(
            exc,
            fts_available=False,
            forum_docs=0,
            knowledge_docs=0,
            doc_chunks=0,
            indexed_documents=0,
        )


@app.command("ingest-docs")
def ingest_docs_command(
    directory: Path = typer.Argument(
        ..., exists=True, file_okay=False, dir_okay=True, readable=True
    ),
    db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db", dir_okay=False),
    rebuild: bool = typer.Option(False, "--rebuild", help="Force full rebuild"),
    prune: bool = typer.Option(
        True,
        "--prune/--no-prune",
        help="Delete documents present in DB but absent from directory (skipped on --rebuild)",
    ),
) -> None:
    """Ingest a directory of markdown documents into the database."""
    try:
        ingest_result = ingest_documents_impl(
            directory, db_path, rebuild=rebuild, prune=prune
        )
        search_index = rebuild_search_index(db_path)
        console.print_json(data={**ingest_result, "search_index": search_index})
    except sqlite3.OperationalError as exc:
        try:
            raise_if_database_locked(exc, db_path)
        except ForumDatabaseBusyError as busy:
            _print_busy(
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
            return
        raise
    except ForumDatabaseBusyError as exc:
        _print_busy(
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


@app.command("search-docs")
def search_docs_command(
    query: str = typer.Argument(..., help="Full-text query"),
    db_path: Path = typer.Option(
        DEFAULT_DB_PATH, "--db", exists=True, dir_okay=False, readable=True
    ),
    top_k: int = typer.Option(5, "--top-k", min=1, max=20),
) -> None:
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            results = search_doc_records(
                conn,
                query=query,
                top_k=top_k,
                embedding_backend=CachedEmbeddingBackend(db_path, writable=False),
            )
    except ForumDatabaseBusyError as exc:
        _print_busy(exc, query=query, results=[])
        return
    except sqlite3.OperationalError as exc:
        try:
            raise_if_database_locked(exc, db_path)
        except ForumDatabaseBusyError as busy:
            _print_busy(busy, query=query, results=[])
            return
        raise
    table = Table(title=f"Top {len(results)}")
    for name in ("slug", "title", "score"):
        table.add_column(name)
    for item in results:
        table.add_row(item["slug"], item["title"], f"{item['score']:.3f}")
    console.print(table)


@app.command("show-doc")
def show_doc_command(
    slug: str = typer.Argument(..., help="Document slug"),
    db_path: Path = typer.Option(
        DEFAULT_DB_PATH, "--db", exists=True, dir_okay=False, readable=True
    ),
) -> None:
    try:
        with DocumentStore(db_path, initialize=False) as store:
            document = store.get_document(slug)
    except ForumDatabaseBusyError as exc:
        _print_busy(exc, slug=slug, document=None)
        return
    except sqlite3.OperationalError as exc:
        try:
            raise_if_database_locked(exc, db_path)
        except ForumDatabaseBusyError as busy:
            _print_busy(busy, slug=slug, document=None)
            return
        raise
    if document is None:
        raise typer.Exit(code=1)
    console.print_json(data=document)


@app.command("search-batch")
def search_batch_command(
    query: str = typer.Argument(...),
    query_variants: list[str] | None = typer.Option(
        None,
        "--variant",
        help="Repeat for caller-provided Chinese/English keywords; at most three",
    ),
    db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db", exists=True, dir_okay=False),
    top_k: int = typer.Option(6, "--top-k", min=1, max=20),
    evidence_chars: int = typer.Option(600, "--evidence-chars", min=100, max=2000),
    max_evidence_chars: int = typer.Option(
        6000, "--max-evidence-chars", min=200, max=20000
    ),
    compact: bool = typer.Option(
        False, "--compact", help="Omit the duplicate snippet field"
    ),
) -> None:
    """Batch local queries and return compact, traceable evidence without a model."""
    from .batch_search import search_forum_batch

    try:
        result = search_forum_batch(
            db_path,
            query,
            query_variants=query_variants,
            top_k=top_k,
            evidence_chars=evidence_chars,
            max_evidence_chars=max_evidence_chars,
            **({"compact": compact} if compact else {}),
        )
    except TimeoutError as exc:
        console.print_json(
            data={
                "status": "timeout",
                "query": query,
                "results": [],
                "reason": str(exc),
            }
        )
        return
    except sqlite3.OperationalError as exc:
        try:
            raise_if_database_locked(exc, db_path)
        except ForumDatabaseBusyError as busy:
            _print_busy(busy, query=query, results=[])
            return
        raise
    console.print_json(data=result)


@app.command("forum-evidence")
def forum_evidence_command(
    chunk_id: str = typer.Argument(...),
    db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db", exists=True, dir_okay=False),
    before: int = typer.Option(1, "--before", min=0, max=2),
    after: int = typer.Option(1, "--after", min=0, max=2),
    max_chars: int = typer.Option(6000, "--max-chars", min=100, max=20000),
    expected_content_hash: str | None = typer.Option(None, "--expected-content-hash"),
) -> None:
    """Read a bounded evidence window from one thread."""
    from .batch_search import get_forum_evidence

    try:
        result = get_forum_evidence(
            db_path,
            chunk_id,
            before=before,
            after=after,
            max_chars=max_chars,
            expected_content_hash=expected_content_hash,
        )
    except TimeoutError as exc:
        console.print_json(
            data={
                "status": "timeout",
                "chunk_id": chunk_id,
                "evidence": [],
                "reason": str(exc),
            }
        )
        return
    except sqlite3.OperationalError as exc:
        try:
            raise_if_database_locked(exc, db_path)
        except ForumDatabaseBusyError as busy:
            _print_busy(busy, chunk_id=chunk_id, evidence=[])
            return
        raise
    console.print_json(data=result)


def main() -> None:
    app()


register_evolution_commands(app)
register_source_commands(app)
