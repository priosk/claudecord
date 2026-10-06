import os
from functools import lru_cache

from telegram import Update


def _owner_ids(name: str) -> frozenset[int]:
    # Same comma-separated shape as ALLOWED_TELEGRAM_CHAT_IDS, mirroring
    # src.auth's OWNER_DISCORD_ID handling (#28 there).
    raw = os.environ.get(name)
    if raw is None:
        raise RuntimeError(f"환경변수 {name}이 필요합니다.")
    owners = set()
    for value in raw.split(","):
        value = value.strip()
        if value:
            try:
                owners.add(int(value))
            except ValueError as exc:
                raise RuntimeError(f"환경변수 {name}은 쉼표로 구분한 정수 Telegram ID여야 합니다.") from exc
    if not owners:
        raise RuntimeError(f"환경변수 {name}이 필요합니다.")
    return frozenset(owners)


def _allowed_chats() -> frozenset[int]:
    raw = os.environ.get("ALLOWED_TELEGRAM_CHAT_IDS", "")
    chats = set()
    for value in raw.split(","):
        value = value.strip()
        if value:
            try:
                chats.add(int(value))
            except ValueError as exc:
                raise RuntimeError("ALLOWED_TELEGRAM_CHAT_IDS는 쉼표로 구분한 정수 Telegram ID여야 합니다.") from exc
    return frozenset(chats)


# Mirrors src.auth._config: read lazily (not at import time) so importing
# this module never requires Telegram env vars, and tests can vary them
# freely via _config.cache_clear().
@lru_cache(maxsize=1)
def _config() -> tuple[frozenset[int], frozenset[int]]:
    return _owner_ids("OWNER_TELEGRAM_ID"), _allowed_chats()


def ensure_telegram_configured() -> None:
    """Validate auth-related env vars now, so bad config fails at startup, not on the first message."""
    _config()


def is_telegram_authorized(update: Update) -> bool:
    owner_ids, allowed_chats = _config()
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return False
    if user.is_bot or user.id not in owner_ids:
        return False
    if chat.type == "private":
        return True
    return chat.id in allowed_chats
