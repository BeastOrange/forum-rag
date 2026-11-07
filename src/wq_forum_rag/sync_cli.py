from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from wq_forum_rag import forum_sync, user_config
from wq_forum_rag.support_client import (
    PASSWORD_ENV,
    USERNAME_ENV,
    SupportCredentialsMissing,
)

DEFAULT_DB_PATH = Path(".cache/forum.sqlite3")
FAILURE_STATUSES = frozenset(
    {
        "needs_full",
        "no_credentials",
        "auth_failed",
        "blocked",
        "network_error",
        "needs_terminal",
    }
)
LOGIN_NEEDS_TERMINAL = (
    "Run forum-rag login yourself in a terminal; it never reads credentials "
    "from a pipe or an AI tool."
)
AUTO_SYNC_QUESTION = (
    "Enable auto sync? forum-rag-mcp will then log in with these credentials at "
    "startup when the forum data is older than the threshold"
)
console = Console()


def _progress(message: str) -> None:
    typer.echo(message, err=True)


def _finish(payload: dict[str, Any]) -> None:
    console.print_json(data=payload)
    if payload.get("status") in FAILURE_STATUSES:
        raise typer.Exit(code=1)


def _env_notes(*, after_logout: bool = False) -> list[str]:
    notes: list[str] = []
    if user_config.load_env_credentials() is not None:
        if after_logout:
            notes.append(
                f"{USERNAME_ENV}/{PASSWORD_ENV} are still set in this environment "
                "and will still be used; unset them to stop using them."
            )
        else:
            notes.append(
                f"{USERNAME_ENV}/{PASSWORD_ENV} are set and take precedence over "
                "the saved credentials."
            )
    if os.environ.get(user_config.AUTO_SYNC_ENV, "").strip():
        notes.append(
            f"{user_config.AUTO_SYNC_ENV} is set and overrides the saved auto sync "
            "choice."
        )
    return notes


def _plaintext_warning(path: Path) -> str:
    return (
        "WARNING: no OS credential store (Keychain, Credential Manager, Secret "
        "Service) is available. Your BRAIN username and password will be stored "
        f"in PLAINTEXT at {path} with 0600 permissions. Anything that runs as this "
        "user, including AI agents, can read that file. Run `forum-rag logout` to "
        "delete it."
    )


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
            db_path,
            client_factory=forum_sync.interactive_client_factory,
            keep_backups=keep_backups,
            progress=_progress,
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
        _finish(forum_sync.support_check(forum_sync.interactive_client_factory))

    @app.command("login")
    def login_command() -> None:
        """Save BRAIN credentials in the OS credential store; asks before enabling auto sync."""
        if not user_config.is_interactive():
            _finish({"status": "needs_terminal", "reason": LOGIN_NEEDS_TERMINAL})
            return
        try:
            credentials = user_config.prompt_credentials()
        except SupportCredentialsMissing as exc:
            _finish({"status": "no_credentials", "reason": str(exc)})
            return
        typer.echo("verifying login…", err=True)
        check = forum_sync.verify_login(credentials)
        if check.get("status") != "ok":
            _finish(check)
            return
        backend: str | None = None
        path: str | None = None
        if user_config.save_to_keyring(credentials):
            store = user_config.SOURCE_KEYRING
            backend = user_config.keyring_backend_name()
        else:
            typer.echo(_plaintext_warning(user_config.credentials_path()), err=True)
            store = user_config.SOURCE_FILE
            path = str(user_config.save_to_file(credentials))
        enable = typer.confirm(AUTO_SYNC_QUESTION, default=False, err=True)
        user_config.set_auto_sync(enable)
        enabled, source = user_config.auto_sync_setting()
        _finish(
            {
                "status": "saved",
                "store": store,
                "backend": backend,
                "path": path,
                "auto_sync_saved": enable,
                "auto_sync_enabled": enabled,
                "auto_sync_source": source,
                "notes": _env_notes(),
            }
        )

    @app.command("logout")
    def logout_command() -> None:
        """Delete saved BRAIN credentials and turn auto sync off."""
        removed: list[str] = []
        if user_config.delete_keyring_credentials():
            removed.append(user_config.SOURCE_KEYRING)
        if user_config.delete_file_credentials():
            removed.append(user_config.SOURCE_FILE)
        user_config.set_auto_sync(False)
        enabled, source = user_config.auto_sync_setting()
        _finish(
            {
                "status": "logged_out",
                "removed": removed,
                "auto_sync_enabled": enabled,
                "auto_sync_source": source,
                "notes": _env_notes(after_logout=True),
            }
        )
