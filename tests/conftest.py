from __future__ import annotations

from pathlib import Path

import keyring.backend
import keyring.backends.fail
import keyring.errors
import pytest

from wq_forum_rag import forum_sync, support_client, user_config

ROOT = Path(__file__).resolve().parents[1]
ALPHAMACHINE_ROOT = ROOT / "AlphaMachine"
ALPHAMACHINE_MISSING_REASON = "AlphaMachine sources are not present in this checkout"


def pytest_ignore_collect(collection_path, config) -> bool:  # type: ignore[no-untyped-def]
    if ALPHAMACHINE_ROOT.exists():
        return False
    return collection_path.name.startswith("test_alphamachine_")


def _network_disabled(*args, **kwargs):  # type: ignore[no-untyped-def]
    raise RuntimeError("network disabled in tests")


@pytest.fixture(autouse=True)
def _no_network_or_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    monkeypatch.delenv(support_client.USERNAME_ENV, raising=False)
    monkeypatch.delenv(support_client.PASSWORD_ENV, raising=False)
    monkeypatch.setenv("WQ_FORUM_RAG_AUTO_SYNC", "0")
    monkeypatch.setattr(support_client, "_default_transport_factory", _network_disabled)
    # Never touch the real ~/.config/wq-forum-rag or the OS credential store.
    monkeypatch.setenv(
        user_config.CONFIG_DIR_ENV, str(tmp_path_factory.mktemp("wq-forum-rag-config"))
    )
    monkeypatch.setattr(
        user_config, "_keyring_backend", lambda: keyring.backends.fail.Keyring()
    )
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    forum_sync.reset_startup_status()


class MemoryKeyring(keyring.backend.KeyringBackend):
    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.entries: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.entries.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.entries[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        try:
            del self.entries[(service, username)]
        except KeyError as exc:
            raise keyring.errors.PasswordDeleteError(username) from exc


@pytest.fixture
def memory_keyring(monkeypatch: pytest.MonkeyPatch) -> MemoryKeyring:
    backend = MemoryKeyring()
    monkeypatch.setattr(user_config, "_keyring_backend", lambda: backend)
    return backend
