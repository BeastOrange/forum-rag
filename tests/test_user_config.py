from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

import keyring.backends.null
import pytest

from wq_forum_rag import user_config
from wq_forum_rag.support_client import SupportCredentials, SupportCredentialsMissing

PASSWORD = "dummy-not-a-password"


def _creds(name: str = "user@example.com") -> SupportCredentials:
    return SupportCredentials(username=name, password=PASSWORD)


def test_lookup_order_env_then_keyring_then_file(
    monkeypatch: pytest.MonkeyPatch, memory_keyring
) -> None:
    assert user_config.resolve_credentials() is None
    assert user_config.credentials_source() is None

    user_config.save_to_file(_creds("file@example.com"))
    found = user_config.resolve_credentials()
    assert found is not None
    assert found[1] == "file"
    assert found[0].username == "file@example.com"

    assert user_config.save_to_keyring(_creds("ring@example.com")) is True
    found = user_config.resolve_credentials()
    assert found is not None
    assert (found[0].username, found[1]) == ("ring@example.com", "keyring")

    monkeypatch.setenv("WQ_BRAIN_USERNAME", "  env@example.com ")
    assert user_config.credentials_source() == "keyring"  # env needs both vars
    monkeypatch.setenv("WQ_BRAIN_PASSWORD", PASSWORD)
    found = user_config.resolve_credentials()
    assert found is not None
    assert (found[0].username, found[1]) == ("env@example.com", "env")

    monkeypatch.setenv("WQ_BRAIN_PASSWORD", "")
    assert user_config.credentials_source() == "keyring"


def test_keyring_round_trip_and_delete(memory_keyring) -> None:
    assert user_config.keyring_available() is True
    assert user_config.save_to_keyring(_creds()) is True
    stored = json.loads(memory_keyring.entries[("wq-forum-rag", "brain")])
    assert stored == {"username": "user@example.com", "password": PASSWORD}
    loaded = user_config.load_keyring_credentials()
    assert loaded == _creds()
    assert user_config.delete_keyring_credentials() is True
    assert user_config.delete_keyring_credentials() is False
    assert user_config.load_keyring_credentials() is None


def test_invalid_keyring_entry_counts_as_missing(memory_keyring) -> None:
    memory_keyring.entries[("wq-forum-rag", "brain")] = "not json"
    assert user_config.load_keyring_credentials() is None


def test_fail_backend_is_unavailable() -> None:
    assert user_config.keyring_available() is False
    assert user_config.save_to_keyring(_creds()) is False
    assert user_config.load_keyring_credentials() is None
    assert user_config.delete_keyring_credentials() is False
    assert user_config.keyring_backend_name() == "keyring.backends.fail.Keyring"


def test_null_backend_save_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        user_config, "_keyring_backend", lambda: keyring.backends.null.Keyring()
    )
    assert user_config.keyring_available() is False
    assert user_config.save_to_keyring(_creds()) is False


def test_backend_that_drops_writes_is_rejected(
    monkeypatch: pytest.MonkeyPatch, memory_keyring
) -> None:
    monkeypatch.setattr(memory_keyring, "set_password", lambda *args: None)
    assert user_config.save_to_keyring(_creds()) is False


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_save_to_file_is_private(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "new" / "wq-forum-rag"
    monkeypatch.setenv(user_config.CONFIG_DIR_ENV, str(config))
    path = user_config.save_to_file(_creds())
    assert path == config / "credentials"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(config.stat().st_mode) == 0o700
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "username": "user@example.com",
        "password": PASSWORD,
    }
    assert sorted(item.name for item in config.iterdir()) == ["credentials"]
    assert user_config.load_file_credentials() == _creds()
    assert user_config.delete_file_credentials() is True
    assert user_config.delete_file_credentials() is False


def test_invalid_credentials_file_is_ignored_without_leaking(
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = user_config.credentials_path()
    path.write_text(f'{{"username": "u", "password": "{PASSWORD}"', encoding="utf-8")
    caplog.set_level(logging.DEBUG, logger="wq_forum_rag.sync")
    assert user_config.load_file_credentials() is None
    assert "invalid credentials file" in caplog.text
    assert PASSWORD not in caplog.text


@pytest.mark.parametrize(
    ("env", "settings", "expected"),
    [
        (None, None, (False, "default")),
        (None, True, (True, "settings")),
        (None, False, (False, "settings")),
        ("0", True, (False, "env")),
        ("off", True, (False, "env")),
        ("1", None, (True, "env")),
        ("yes", False, (True, "env")),
        ("", True, (True, "settings")),
    ],
)
def test_auto_sync_setting_precedence(
    monkeypatch: pytest.MonkeyPatch, env, settings, expected
) -> None:
    if env is None:
        monkeypatch.delenv(user_config.AUTO_SYNC_ENV, raising=False)
    else:
        monkeypatch.setenv(user_config.AUTO_SYNC_ENV, env)
    if settings is not None:
        user_config.set_auto_sync(settings)
    assert user_config.auto_sync_setting() == expected


def test_set_auto_sync_keeps_other_settings() -> None:
    user_config.write_settings(other="kept")
    user_config.set_auto_sync(True)
    assert user_config.read_settings() == {"auto_sync": True, "other": "kept"}
    user_config.set_auto_sync(False)
    assert user_config.read_settings() == {"auto_sync": False, "other": "kept"}


def test_unreadable_settings_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(user_config.AUTO_SYNC_ENV, raising=False)
    user_config.settings_path().write_text("{not json", encoding="utf-8")
    assert user_config.read_settings() == {}
    assert user_config.auto_sync_setting() == (False, "default")


def test_require_credentials_message() -> None:
    with pytest.raises(SupportCredentialsMissing) as info:
        user_config.require_credentials()
    message = str(info.value)
    assert "forum-rag login" in message
    assert "forum-rag sync" in message
    assert "WQ_BRAIN_PASSWORD" in message
    assert "forum-rag login" in user_config.NO_TERMINAL_MESSAGE
    assert "WQ_BRAIN_PASSWORD" in user_config.NO_TERMINAL_MESSAGE


def test_is_interactive_false_under_pytest() -> None:
    assert user_config.is_interactive() is False


def test_secrets_never_in_repr_or_logs(
    caplog: pytest.LogCaptureFixture, memory_keyring
) -> None:
    caplog.set_level(logging.DEBUG, logger="wq_forum_rag.sync")
    credentials = _creds()
    user_config.save_to_keyring(credentials)
    user_config.save_to_file(credentials)
    found = user_config.resolve_credentials()
    assert PASSWORD not in repr(found)
    assert PASSWORD not in str(credentials)
    assert PASSWORD not in caplog.text
