"""Tests for src/telegram_auth.py: the Telegram counterpart of test_auth.py."""

import sys
from datetime import datetime

import pytest
from telegram import Chat, Message, Update, User

from src import telegram_auth as auth


@pytest.fixture(autouse=True)
def _clear_config_cache():
    auth._config.cache_clear()
    yield
    auth._config.cache_clear()


def _make_update(user_id: int, *, is_bot: bool = False, chat_id: int | None = None, chat_type: str = "private") -> Update:
    user = User(id=user_id, first_name="tester", is_bot=is_bot)
    chat = Chat(id=chat_id if chat_id is not None else user_id, type=chat_type)
    message = Message(message_id=1, date=datetime.now(), chat=chat, from_user=user, text="hi")
    return Update(update_id=1, message=message)


def test_import_succeeds_without_env_vars(monkeypatch):
    monkeypatch.delenv("OWNER_TELEGRAM_ID", raising=False)
    monkeypatch.delenv("ALLOWED_TELEGRAM_CHAT_IDS", raising=False)
    sys.modules.pop("src.telegram_auth", None)
    import src.telegram_auth as reimported  # noqa: F401 -- just verifying it doesn't raise


def test_ensure_configured_raises_without_owner_env(monkeypatch):
    monkeypatch.delenv("OWNER_TELEGRAM_ID", raising=False)
    with pytest.raises(RuntimeError, match="OWNER_TELEGRAM_ID"):
        auth.ensure_telegram_configured()


def test_ensure_configured_raises_on_blank_owner_env(monkeypatch):
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "   ")
    with pytest.raises(RuntimeError, match="OWNER_TELEGRAM_ID"):
        auth.ensure_telegram_configured()


def test_ensure_configured_raises_on_invalid_owner_id(monkeypatch):
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "not-an-int")
    with pytest.raises(RuntimeError, match="정수 Telegram ID"):
        auth.ensure_telegram_configured()


def test_ensure_configured_ok_with_valid_env(monkeypatch):
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "111")
    auth.ensure_telegram_configured()


def test_single_owner_id_backward_compatible(monkeypatch):
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "111")
    monkeypatch.delenv("ALLOWED_TELEGRAM_CHAT_IDS", raising=False)

    assert auth.is_telegram_authorized(_make_update(111)) is True
    assert auth.is_telegram_authorized(_make_update(222)) is False


def test_multiple_owner_ids_comma_separated(monkeypatch):
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "111, 222,333")
    monkeypatch.delenv("ALLOWED_TELEGRAM_CHAT_IDS", raising=False)

    assert auth.is_telegram_authorized(_make_update(111)) is True
    assert auth.is_telegram_authorized(_make_update(222)) is True
    assert auth.is_telegram_authorized(_make_update(333)) is True
    assert auth.is_telegram_authorized(_make_update(444)) is False


def test_bot_sender_is_never_authorized(monkeypatch):
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "111")
    assert auth.is_telegram_authorized(_make_update(111, is_bot=True)) is False


def test_private_chat_bypasses_chat_whitelist(monkeypatch):
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "111")
    monkeypatch.setenv("ALLOWED_TELEGRAM_CHAT_IDS", "999")

    update = _make_update(111, chat_id=555, chat_type="private")
    assert auth.is_telegram_authorized(update) is True


def test_group_chat_requires_whitelist(monkeypatch):
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "111")
    monkeypatch.setenv("ALLOWED_TELEGRAM_CHAT_IDS", "999")

    allowed = _make_update(111, chat_id=999, chat_type="group")
    assert auth.is_telegram_authorized(allowed) is True

    denied = _make_update(111, chat_id=42, chat_type="group")
    assert auth.is_telegram_authorized(denied) is False


def test_group_chat_denied_without_any_whitelist(monkeypatch):
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "111")
    monkeypatch.delenv("ALLOWED_TELEGRAM_CHAT_IDS", raising=False)

    update = _make_update(111, chat_id=999, chat_type="group")
    assert auth.is_telegram_authorized(update) is False


def test_invalid_allowed_chat_ids_raises(monkeypatch):
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "111")
    monkeypatch.setenv("ALLOWED_TELEGRAM_CHAT_IDS", "not-an-int")

    with pytest.raises(RuntimeError, match="ALLOWED_TELEGRAM_CHAT_IDS"):
        auth.is_telegram_authorized(_make_update(111, chat_id=999, chat_type="group"))
