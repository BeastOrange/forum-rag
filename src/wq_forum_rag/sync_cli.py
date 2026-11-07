from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from wq_forum_rag import forum_sync

DEFAULT_DB_PATH = Path(".cache/forum.sqlite3")
FAILURE_STATUSES = frozenset(
    {"needs_full", "no_credentials", "auth_failed", "blocked", "network_error"}
)
console = Console()


def _progress(message: str) -> None:
    typer.echo(message, err=True)


def _finish(payload: dict[str, Any]) -> None:
    console.print_json(data=payload)
    if payload.get("status") in FAILURE_STATUSES:
        raise typer.Exit(code=1)


def register_sync_commands(app: typer.Typer) -> None:
    @app.command("sync")
    def sync_command(
        full: bool = typer.Option(
            False, "--full", help="Fetch every post, comment and article"
        ),
        if_stale: bool = typer.Option(
            False,
            "--if-stale",
            help="Only sync when the last sync is older than D days",
        ),
        sync_interval_days: float | None = typer.Option(
            None, "--sync-interval-days", min=0.0, help="Staleness threshold in days"
        ),
        keep_backups: int | None = typer.Option(
            None, "--keep-backups", min=0, help="Backups to keep (default 1)"
        ),
        max_pages: int | None = typer.Option(
            None, "--max-pages", min=1, help="Stop each listing after N pages"
        ),
        community: str | None = typer.Option(
            None, "--community", help="Only this community id (with --full)"
        ),
        db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db", dir_okay=False),
    ) -> None:
        """Sync forum posts and help-center articles from the Support API."""
        if community and not full:
            raise typer.BadParameter(
                "--community requires --full", param_hint="--community"
            )
        service = forum_sync.ForumSyncService(
            db_path, keep_backups=keep_backups, progress=_progress
        )
        if if_stale:
            state = service.status(sync_interval_days)
            if not state["needs_full"] and not state["stale"]:
                _finish(
                    {
                        "status": "fresh",
                        "db": str(db_path),
                        "mode": "full" if full else "incremental",
                        "watermark": state["watermark"],
                        "due_at": state["due_at"],
                    }
                )
                return
        _finish(service.sync(full=full, max_pages=max_pages, community=community))

    @app.command("sync-status")
    def sync_status_command(
        db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db", dir_okay=False),
        sync_interval_days: float | None = typer.Option(
            None, "--sync-interval-days", min=0.0, help="Staleness threshold in days"
        ),
    ) -> None:
        """Show the sync watermark, staleness and backups (no network)."""
        console.print_json(
            data=forum_sync.ForumSyncService(db_path).status(sync_interval_days)
        )

    @app.command("sync-check")
    def sync_check_command() -> None:
        """Check the Support API login without writing anything (read-only)."""
        _finish(forum_sync.support_check())
