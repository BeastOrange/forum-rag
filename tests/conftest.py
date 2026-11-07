from __future__ import annotations

from pathlib import Path

import pytest

from wq_forum_rag import support_client

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
def _no_network_or_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(support_client.USERNAME_ENV, raising=False)
    monkeypatch.delenv(support_client.PASSWORD_ENV, raising=False)
    monkeypatch.setenv("WQ_FORUM_RAG_AUTO_SYNC", "0")
    monkeypatch.setattr(support_client, "_default_transport_factory", _network_disabled)
