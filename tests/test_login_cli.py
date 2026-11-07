from __future__ import annotations

import json
import os
import stat

import pytest
from typer.testing import CliRunner

from wq_forum_rag import forum_sync, user_config
from wq_forum_rag.cli import app
from wq_forum_rag.support_client import SupportAuthError, SupportCredentials

PASSWORD = "dummy-not-a-password"
KEY = (user_config.KEYRING_SERVICE, user_config.KEYRING_ACCOUNT)


class FakeLoginClient:
    def __init__(self, credentials: SupportCredentials, error=None) -> None:
        self.credentials = credentials
        self.error = error

    def login(self) -> dict:
        if self.error is not None:
            raise self.error
        return {"login_status": 201, "hops": 3, "role": "end-user"}


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch) -> list[SupportCredentials]:
    seen: list[SupportCredentials] = []

    def client_for(credentials):
        seen.append(credentials)
        return FakeLoginClient(credentials)

    monkeypatch.setattr(user_config, "is_interactive", lambda: True)
    monkeypatch.setattr(user_config, "_read_password", lambda prompt: PASSWORD)
    monkeypatch.setattr(forum_sync, "client_for", client_for)
    return seen


def _payload(result) -> dict:
    # CliRunner echoes typed input to stdout; the JSON payload follows it.
    return json.loads(result.stdout[result.stdout.index("{") :])


def _settings() -> dict:
    return json.loads(user_config.settings_path().read_text(encoding="utf-8"))


def test_login_saves_to_keyring_and_enables_auto_sync(
    monkeypatch: pytest.MonkeyPatch, terminal, memory_keyring
) -> None:
    monkeypatch.delenv(user_config.AUTO_SYNC_ENV)

    result = CliRunner().invoke(app, ["login"], input="user@example.com\ny\n")

    assert result.exit_code == 0, result.output
    payload = _payload(result)
    assert payload["status"] == "saved"
    assert payload["store"] == "keyring"
    assert payload["path"] is None
    assert payload["auto_sync_saved"] is True
    assert payload["auto_sync_enabled"] is True
    assert payload["auto_sync_source"] == "settings"
    assert payload["notes"] == []
    assert json.loads(memory_keyring.entries[KEY]) == {
        "username": "user@example.com",
        "password": PASSWORD,
    }
    assert terminal == [SupportCredentials("user@example.com", PASSWORD)]
    assert _settings()["auto_sync"] is True
    assert not user_config.credentials_path().exists()
    assert PASSWORD not in result.output
    assert "user@example.com" not in json.dumps(payload)


def test_login_answer_no_keeps_auto_sync_off(terminal, memory_keyring) -> None:
    result = CliRunner().invoke(app, ["login"], input="user@example.com\nn\n")

    assert result.exit_code == 0, result.output
    payload = _payload(result)
    assert payload["auto_sync_saved"] is False
    assert payload["auto_sync_enabled"] is False
    assert payload["auto_sync_source"] == "env"
    assert any(user_config.AUTO_SYNC_ENV in note for note in payload["notes"])
    assert _settings()["auto_sync"] is False
    assert KEY in memory_keyring.entries


def test_login_enter_defaults_to_no(terminal, memory_keyring) -> None:
    result = CliRunner().invoke(app, ["login"], input="user@example.com\n\n")

    assert result.exit_code == 0, result.output
    assert _settings()["auto_sync"] is False


def test_login_falls_back_to_plaintext_file(terminal) -> None:
    result = CliRunner().invoke(app, ["login"], input="user@example.com\nn\n")

    assert result.exit_code == 0, result.output
    assert "PLAINTEXT" in result.stderr
    payload = _payload(result)
    path = user_config.credentials_path()
    assert payload["store"] == "file"
    assert payload["path"] == str(path)
    assert path.is_file()
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert user_config.load_file_credentials() == SupportCredentials(
        "user@example.com", PASSWORD
    )
    assert PASSWORD not in result.output


def test_login_failure_stores_nothing(
    monkeypatch: pytest.MonkeyPatch, terminal, memory_keyring
) -> None:
    monkeypatch.setattr(
        forum_sync,
        "client_for",
        lambda credentials: FakeLoginClient(
            credentials, SupportAuthError("BRAIN login failed with HTTP 401")
        ),
    )

    result = CliRunner().invoke(app, ["login"], input="user@example.com\ny\n")

    assert result.exit_code == 1
    assert _payload(result)["status"] == "auth_failed"
    assert memory_keyring.entries == {}
    assert not user_config.credentials_path().exists()
    assert not user_config.settings_path().exists()


def test_login_without_terminal_never_reads_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("must not be called")

    monkeypatch.setattr(user_config, "_read_password", forbidden)
    monkeypatch.setattr(forum_sync, "client_for", forbidden)

    result = CliRunner().invoke(app, ["login"], input="user@example.com\ny\n")

    assert result.exit_code == 1
    payload = _payload(result)
    assert payload["status"] == "needs_terminal"
    assert "forum-rag login" in payload["reason"]
    assert not user_config.settings_path().exists()


def test_logout_removes_both_stores_and_disables_auto_sync(
    monkeypatch: pytest.MonkeyPatch, memory_keyring
) -> None:
    credentials = SupportCredentials("user@example.com", PASSWORD)
    assert user_config.save_to_keyring(credentials)
    user_config.save_to_file(credentials)
    user_config.set_auto_sync(True)
    monkeypatch.delenv(user_config.AUTO_SYNC_ENV)

    result = CliRunner().invoke(app, ["logout"])

    assert result.exit_code == 0, result.output
    payload = _payload(result)
    assert payload["status"] == "logged_out"
    assert payload["removed"] == ["keyring", "file"]
    assert payload["auto_sync_enabled"] is False
    assert payload["auto_sync_source"] == "settings"
    assert payload["notes"] == []
    assert memory_keyring.entries == {}
    assert not user_config.credentials_path().exists()
    assert _settings()["auto_sync"] is False

    again = CliRunner().invoke(app, ["logout"])
    assert _payload(again)["removed"] == []


def test_logout_notes_env_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WQ_BRAIN_USERNAME", "user@example.com")
    monkeypatch.setenv("WQ_BRAIN_PASSWORD", PASSWORD)

    result = CliRunner().invoke(app, ["logout"])

    assert result.exit_code == 0, result.output
    notes = _payload(result)["notes"]
    assert any("WQ_BRAIN_PASSWORD" in note for note in notes)
    assert PASSWORD not in result.output
    assert "user@example.com" not in result.output


def test_help_lists_login_and_logout() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "login" in result.output
    assert "logout" in result.output
