"""Telegram counterpart to src/status.py.

``JobProgress`` and ``stop_spinning_loader`` are reused as-is from
src.status — neither touches a Discord type, they just track/cancel a
background asyncio.Task. Only the status text and the message-edit loop are
rewritten here: Telegram messages aren't rendered with Discord's ``**bold**``
markdown unless a ``parse_mode`` is set, and this bot deliberately sends
plain text (see telegram_outputs.py) to avoid MarkdownV2's escaping rules, so
the status text drops the ``**`` markers rather than showing them literally.
"""

import asyncio
import logging

import telegram

from src.status import (  # noqa: F401 -- re-exported for telegram_bot.py
    EDIT_INTERVAL_GROWTH,
    EDIT_INTERVAL_SECONDS,
    MAX_EDIT_INTERVAL_SECONDS,
    JobProgress,
    _activity_label,
    _format_elapsed,
    stop_spinning_loader,
)

logger = logging.getLogger(__name__)

WORKING_MESSAGE = "작업중입니다."
QUEUED_MESSAGE = "대기 중"


def format_working_status(job_name: str, progress: "JobProgress | None" = None) -> str:
    elapsed = progress.elapsed_seconds() if progress else 0.0
    return (
        f"⚙️ {WORKING_MESSAGE}\n"
        f"{_activity_label(progress)} · 경과 {_format_elapsed(elapsed)}\n"
        f"{job_name}"
    )


def format_queued_status(job_name: str, ahead: int) -> str:
    return (
        f"⏳ {QUEUED_MESSAGE} (앞에 {max(0, ahead)}건)\n"
        f"앞선 작업이 끝나면 바로 시작합니다\n"
        f"{job_name}"
    )


async def run_spinning_loader(
    message: "telegram.Message",
    job_name: str,
    progress: "JobProgress | None" = None,
    *,
    interval: float = EDIT_INTERVAL_SECONDS,
    growth: float = EDIT_INTERVAL_GROWTH,
    max_interval: float = MAX_EDIT_INTERVAL_SECONDS,
) -> None:
    """Same backoff curve as src.status.run_spinning_loader, edited for the
    Telegram Bot API's ``editMessageText`` instead of Discord's message edit.
    """
    delay = interval
    while True:
        await asyncio.sleep(delay)
        delay = min(delay * growth, max_interval)
        try:
            await message.edit_text(format_working_status(job_name, progress))
        except telegram.error.TelegramError:
            # Message deleted, "message is not modified", permission change,
            # etc. -- best-effort UI, so bail out quietly (see
            # src.status.run_spinning_loader for the same contract).
            logger.warning("run_spinning_loader: edit failed, stopping loader", exc_info=True)
            return
        except OSError:
            logger.warning("run_spinning_loader: edit failed, stopping loader", exc_info=True)
            return
