# Author: konrad
# WQ_ID: OB53521
# Encoding: utf-8

from __future__ import annotations

import sqlite3
from pathlib import Path

import typer
from rich.console import Console

from wq_forum_rag.indexer import ForumDatabaseBusyError, raise_if_database_locked
from wq_forum_rag.manifest import SourceManifestService

DEFAULT_DB_PATH = Path(".cache/forum.sqlite3")
console = Console()


def _print_busy(exc: ForumDatabaseBusyError, **extra: object) -> None:
    console.print_json(data={**extra, **exc.payload})


def register_source_commands(app: typer.Typer) -> None:
    @app.command("source-status")
    def source_status_command(
        source: Path = typer.Argument(..., exists=True, readable=True),
        db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db", dir_okay=False),
    ) -> None:
        try:
            console.print_json(
                data=SourceManifestService(db_path).source_status(source)
            )
        except sqlite3.OperationalError as exc:
            try:
                raise_if_database_locked(exc, db_path)
            except ForumDatabaseBusyError as busy:
                _print_busy(busy, source=str(source), counts={}, files={})
                return
            raise

    @app.command("source-ingest-plan")
    def source_ingest_plan_command(
        source: Path = typer.Argument(..., exists=True, readable=True),
        db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db", dir_okay=False),
        commit: bool = typer.Option(False, "--commit"),
    ) -> None:
        try:
            console.print_json(
                data=SourceManifestService(db_path).source_ingest_plan(
                    source, commit=commit
                )
            )
        except sqlite3.OperationalError as exc:
            try:
                raise_if_database_locked(exc, db_path)
            except ForumDatabaseBusyError as busy:
                _print_busy(busy, source=str(source), ingest_plan=[], delete_plan=[])
                return
            raise
