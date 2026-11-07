"""Saved BRAIN credentials and the per-user auto-sync setting.

Credentials are looked up in this order: the ``WQ_BRAIN_USERNAME`` /
``WQ_BRAIN_PASSWORD`` env vars, the OS credential store (via ``keyring``),
then a plaintext fallback file written with 0600 permissions. The auto-sync
flag lives in ``settings.json`` next to that file; a non-empty
``WQ_FORUM_RAG_AUTO_SYNC`` overrides it in both directions.

Secrets are never logged, printed or included in exception messages.
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import secrets
import sys
from pathlib import Path
from typing import Any

import typer

from wq_forum_rag.indexer import DISABLED_VALUES
from wq_forum_rag.support_client import (
    PASSWORD_ENV,
    USERNAME_ENV,
    SupportCredentials,
    SupportCredentialsMissing,
)

CONFIG_DIR_ENV = "WQ_FORUM_RAG_CONFIG_DIR"
AUTO_SYNC_ENV = "WQ_FORUM_RAG_AUTO_SYNC"
KEYRING_SERVICE = "wq-forum-rag"
KEYRING_ACCOUNT = "brain"
SETTINGS_FILE = "settings.json"
CREDENTIALS_FILE = "credentials"
SOURCE_ENV = "env"
SOURCE_KEYRING = "keyring"
SOURCE_FILE = "file"

MISSING_MESSAGE = (
    "No BRAIN credentials found. Run `forum-rag login` in your own terminal to "
    "save them, or `forum-rag sync` there to enter them for one run without "
    f"storing anything (or set {USERNAME_ENV} and {PASSWORD_ENV})."
)
NO_TERMINAL_MESSAGE = (
    "No BRAIN credentials found and no terminal to ask for them. Ask the user to "
    "run `forum-rag login` (saves the credentials) or `forum-rag sync` (one run, "
    f"nothing stored) in their own terminal, or to set {USERNAME_ENV} and "
    f"{PASSWORD_ENV}. Never ask for the password in chat."
)

logger = logging.getLogger("wq_forum_rag.sync")


# -- paths ----------------------------------------------------------------------


def config_dir() -> Path:
    override = os.environ.get(CONFIG_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "wq-forum-rag"


def credentials_path() -> Path:
    return config_dir() / CREDENTIALS_FILE


def settings_path() -> Path:
    return config_dir() / SETTINGS_FILE


def _write_private(path: Path, text: str) -> None:
    """Atomically write ``text`` to ``path`` with 0600 permissions."""
    directory = path.parent
    if not directory.is_dir():
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(directory, 0o700)
    temp = directory / f".{path.name}.{secrets.token_hex(6)}.tmp"
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    if os.name == "posix":
        os.chmod(path, 0o600)


def _credentials_json(credentials: SupportCredentials) -> str:
    return json.dumps(
        {"username": credentials.username, "password": credentials.password}
    )


def _parse_credentials(text: str | None) -> SupportCredentials | None:
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    username = data.get("username")
    password = data.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        return None
    username = username.strip()
    if not username or not password:
        return None
    return SupportCredentials(username=username, password=password)


# -- keyring --------------------------------------------------------------------


def _keyring_backend() -> Any:
    import keyring

    return keyring.get_keyring()


def _usable_backend() -> Any | None:
    try:
        backend = _keyring_backend()
    except Exception as exc:  # noqa: BLE001 - a broken keyring means "unavailable"
        logger.debug("keyring backend unavailable: %s", type(exc).__name__)
        return None
    from keyring.backends import fail, null

    if isinstance(backend, (fail.Keyring, null.Keyring)):
        return None
    return backend


def keyring_available() -> bool:
    return _usable_backend() is not None


def keyring_backend_name() -> str | None:
    try:
        backend = _keyring_backend()
    except Exception:  # noqa: BLE001 - only the name is reported
        return None
    return f"{type(backend).__module__}.{type(backend).__name__}"


def load_keyring_credentials(
    *, service: str = KEYRING_SERVICE
) -> SupportCredentials | None:
    backend = _usable_backend()
    if backend is None:
        return None
    try:
        text = backend.get_password(service, KEYRING_ACCOUNT)
    except Exception as exc:  # noqa: BLE001 - unreadable entry counts as missing
        logger.debug("keyring read failed: %s", type(exc).__name__)
        return None
    return _parse_credentials(text)


def save_to_keyring(
    credentials: SupportCredentials, *, service: str = KEYRING_SERVICE
) -> bool:
    """Store the credentials; False when no working credential store exists."""
    backend = _usable_backend()
    if backend is None:
        return False
    text = _credentials_json(credentials)
    try:
        backend.set_password(service, KEYRING_ACCOUNT, text)
        stored = backend.get_password(service, KEYRING_ACCOUNT)
    except Exception as exc:  # noqa: BLE001 - fall back to the file
        logger.debug("keyring write failed: %s", type(exc).__name__)
        return False
    return stored == text


def delete_keyring_credentials(*, service: str = KEYRING_SERVICE) -> bool:
    backend = _usable_backend()
    if backend is None:
        return False
    try:
        if backend.get_password(service, KEYRING_ACCOUNT) is None:
            return False
        backend.delete_password(service, KEYRING_ACCOUNT)
    except Exception as exc:  # noqa: BLE001 - nothing removed
        logger.debug("keyring delete failed: %s", type(exc).__name__)
        return False
    return True


# -- env and file ---------------------------------------------------------------


def load_env_credentials() -> SupportCredentials | None:
    username = os.environ.get(USERNAME_ENV, "").strip()
    password = os.environ.get(PASSWORD_ENV, "")
    if not username or not password:
        return None
    return SupportCredentials(username=username, password=password)


def load_file_credentials() -> SupportCredentials | None:
    path = credentials_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("could not read %s: %s", path, type(exc).__name__)
        return None
    credentials = _parse_credentials(text)
    if credentials is None:
        logger.warning("ignoring invalid credentials file %s", path)
    return credentials


def save_to_file(credentials: SupportCredentials) -> Path:
    path = credentials_path()
    _write_private(path, _credentials_json(credentials) + "\n")
    return path


def delete_file_credentials() -> bool:
    path = credentials_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


# -- lookup ---------------------------------------------------------------------


def resolve_credentials() -> tuple[SupportCredentials, str] | None:
    """Env vars, then the OS credential store, then the file; never prompts."""
    credentials = load_env_credentials()
    if credentials is not None:
        return credentials, SOURCE_ENV
    credentials = load_keyring_credentials()
    if credentials is not None:
        return credentials, SOURCE_KEYRING
    credentials = load_file_credentials()
    if credentials is not None:
        return credentials, SOURCE_FILE
    return None


def credentials_source() -> str | None:
    found = resolve_credentials()
    return found[1] if found else None


def require_credentials() -> SupportCredentials:
    found = resolve_credentials()
    if found is None:
        raise SupportCredentialsMissing(MISSING_MESSAGE)
    return found[0]


# -- settings -------------------------------------------------------------------


def read_settings() -> dict[str, Any]:
    path = settings_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("ignoring unreadable %s: %s", path, type(exc).__name__)
        return {}
    return data if isinstance(data, dict) else {}


def write_settings(**changes: Any) -> Path:
    settings = {**read_settings(), **changes}
    path = settings_path()
    _write_private(path, json.dumps(settings, indent=2, sort_keys=True) + "\n")
    return path


def set_auto_sync(enabled: bool) -> Path:
    return write_settings(auto_sync=bool(enabled))


def auto_sync_setting() -> tuple[bool, str]:
    """Return ``(enabled, source)`` with source ``env``, ``settings`` or ``default``."""
    raw = os.environ.get(AUTO_SYNC_ENV, "").strip().lower()
    if raw:
        return raw not in DISABLED_VALUES, "env"
    value = read_settings().get("auto_sync")
    if isinstance(value, bool):
        return value, "settings"
    return False, "default"


# -- prompts --------------------------------------------------------------------


def is_interactive() -> bool:
    stdin = sys.stdin
    if stdin is None:
        return False
    try:
        return bool(stdin.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def _read_password(prompt: str) -> str:
    return getpass.getpass(prompt)


def prompt_credentials(notice: str | None = None) -> SupportCredentials:
    """Ask for the BRAIN username and password; the password is never echoed."""
    if notice:
        typer.echo(notice, err=True)
    username = str(typer.prompt("BRAIN username (email)", err=True)).strip()
    if not username:
        raise SupportCredentialsMissing("No BRAIN username entered")
    password = _read_password("BRAIN password (not shown): ")
    if not password:
        raise SupportCredentialsMissing("No BRAIN password entered")
    return SupportCredentials(username=username, password=password)


# -- doctor ---------------------------------------------------------------------


def credentials_report() -> dict[str, Any]:
    """Where credentials would come from; never includes their values."""
    found = resolve_credentials()
    return {
        "available": found is not None,
        "source": found[1] if found else None,
        "keyring_available": keyring_available(),
        "keyring_backend": keyring_backend_name(),
        "credentials_file": str(credentials_path()),
    }
