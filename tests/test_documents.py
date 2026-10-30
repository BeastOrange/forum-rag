from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from wq_forum_rag.cli import app
from wq_forum_rag.documents import (
    Document,
    DocumentStore,
    build_doc_chunks,
    ingest_documents,
    iter_documents,
)
from wq_forum_rag.mcp_server import get_doc, ingest_docs, search_docs


def _write_docs(root: Path) -> None:
    (root / "Operators.md").write_text(
        "# Operators\n\nUse `ts_rank` to rank values across time series.\n",
        encoding="utf-8",
    )
    (root / "Neutralization.md").write_text(
        "# Neutralization\n\nSubindustry neutralization removes sector exposures.\n",
        encoding="utf-8",
    )
    nested = root / "Datasets"
    nested.mkdir()
    (nested / "How to use Price-Volume Dataset.md").write_text(
        "# Price-Volume Dataset\n\nClose, volume, vwap for each ticker.\n",
        encoding="utf-8",
    )


def test_iter_documents_uses_h1_as_title(tmp_path: Path) -> None:
    _write_docs(tmp_path)
    documents = sorted(iter_documents(tmp_path), key=lambda doc: doc.slug)
    slugs = [doc.slug for doc in documents]
    assert slugs == ["how-to-use-price-volume-dataset", "neutralization", "operators"]
    titles = {doc.slug: doc.title for doc in documents}
    assert titles["operators"] == "Operators"
    assert titles["how-to-use-price-volume-dataset"] == "Price-Volume Dataset"


def test_build_doc_chunks_window_and_overlap(tmp_path: Path) -> None:
    body = "abcdefghij" * 30
    document = Document(
        slug="alpha-doc", title="Alpha", source_path="alpha.md", body=body
    )
    chunks = build_doc_chunks(document, window_size=80, overlap=20)
    assert len(chunks) > 1
    assert chunks[0].chunk_index == 0
    assert chunks[0].start_offset == 0
    assert chunks[1].start_offset == chunks[0].end_offset - 20
    assert all(chunk.slug == "alpha-doc" for chunk in chunks)


def test_document_store_skips_unchanged_and_prunes_missing(tmp_path: Path) -> None:
    _write_docs(tmp_path)
    db_path = tmp_path / "forum.sqlite3"

    with DocumentStore(db_path) as store:
        first_stats, first_seen = store.upsert_documents(iter_documents(tmp_path))
        second_stats, _ = store.upsert_documents(iter_documents(tmp_path))

    assert first_stats["seen"] == 3
    assert first_stats["inserted"] == 3
    assert first_stats["chunks_written"] >= 3
    assert first_seen == {
        "operators",
        "neutralization",
        "how-to-use-price-volume-dataset",
    }
    assert second_stats["skipped"] == 3
    assert second_stats["chunks_written"] == 0

    (tmp_path / "Operators.md").unlink()
    with DocumentStore(db_path) as store:
        _, seen = store.upsert_documents(iter_documents(tmp_path))
        pruned = store.prune_missing(seen)
        remaining = {doc["slug"] for doc in store.list_documents()}
    assert pruned == 1
    assert remaining == {"neutralization", "how-to-use-price-volume-dataset"}


def test_ingest_docs_cli_indexes_and_searches(tmp_path: Path) -> None:
    docs_root = tmp_path / "docs"
    docs_root.mkdir()
    _write_docs(docs_root)
    db_path = tmp_path / "forum.sqlite3"
    runner = CliRunner()

    ingest_result = runner.invoke(
        app, ["ingest-docs", str(docs_root), "--db", str(db_path)]
    )
    assert ingest_result.exit_code == 0
    payload = json.loads(ingest_result.stdout)
    assert payload["indexed_documents"] == 3
    assert payload["search_index"]["doc_chunks"] >= 3

    search_result = runner.invoke(
        app,
        [
            "search-docs",
            "subindustry neutralization",
            "--db",
            str(db_path),
            "--top-k",
            "3",
        ],
    )
    assert search_result.exit_code == 0
    assert "neutralization" in search_result.stdout

    show_result = runner.invoke(app, ["show-doc", "operators", "--db", str(db_path)])
    assert show_result.exit_code == 0
    assert '"slug": "operators"' in show_result.stdout


def test_mcp_doc_tools_round_trip(tmp_path: Path) -> None:
    docs_root = tmp_path / "docs"
    docs_root.mkdir()
    _write_docs(docs_root)
    db_path = tmp_path / "forum.sqlite3"

    ingest_payload = ingest_docs(str(docs_root), db=str(db_path))
    assert ingest_payload["indexed_documents"] == 3

    search_payload = search_docs("price volume", db=str(db_path), top_k=3)
    assert any(
        item["slug"] == "how-to-use-price-volume-dataset"
        for item in search_payload["results"]
    )

    get_payload = get_doc("operators", db=str(db_path))
    assert get_payload["document"] is not None
    assert get_payload["document"]["title"] == "Operators"

    conn = sqlite3.connect(db_path)
    try:
        kinds = {
            row[0]
            for row in conn.execute("SELECT DISTINCT kind FROM document_fts").fetchall()
        }
    finally:
        conn.close()
    assert "doc_chunk" in kinds


def test_ingest_documents_rebuild_clears_state(tmp_path: Path) -> None:
    docs_root = tmp_path / "docs"
    docs_root.mkdir()
    _write_docs(docs_root)
    db_path = tmp_path / "forum.sqlite3"

    first = ingest_documents(docs_root, db_path)
    assert first["indexed_documents"] == 3

    (docs_root / "Operators.md").unlink()
    second = ingest_documents(docs_root, db_path, rebuild=True)
    assert second["status"] == "rebuilt"
    assert second["indexed_documents"] == 2
    assert second["pruned"] == 0


def test_ingest_empty_directory_skips_prune(tmp_path: Path) -> None:
    docs_root = tmp_path / "docs"
    docs_root.mkdir()
    _write_docs(docs_root)
    db_path = tmp_path / "forum.sqlite3"
    assert ingest_documents(docs_root, db_path)["indexed_documents"] == 3

    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    result = ingest_documents(empty_root, db_path)

    assert result["seen"] == 0
    assert result["prune_skipped"] is True
    assert result["pruned"] == 0
    assert result["indexed_documents"] == 3
