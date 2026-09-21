"""Telegram counterpart to src/main.py.

Reuses the platform-agnostic pipeline as-is (src.orchestrator, src.runner,
src.sessions, src.parser, src.timing, src.greetings -- none of them import
discord or telegram) and re-implements only the transport-specific glue that
main.py builds on top of discord.py: auth (telegram_auth.py), status text and
the ack-editing loader (telegram_status.py), and sending the answer back
(telegram_outputs.py). The job dispatch shape below -- ack, queue behind a
concurrency gate, resolve/prepare the job, run with stale-session recovery,
handle timeout -- mirrors main.py line for line; see that module's comments
for why each piece exists (issue numbers and all). Chat IDs stand in for
Discord channel IDs everywhere a channel_id is expected (src.sessions and the
per-process job gate are keyed generically by that int, not by platform).
"""

import asyncio
import logging
import os
from pathlib import Path

import telegram
from dotenv import load_dotenv
from telegram import Message, Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

from src.errors import redact_paths, safe_error_text
from src.greetings import direct_reply_for
from src.orchestrator import allocate_job, cleanup_old_runs, prepare_job, run_job
from src.parser import parse
from src.runner import (
    JobProcessScope,
    get_warm_pool,
    terminate_active_claude_processes,
    terminate_job_processes,
)
from src.sessions import clear_all_sessions, clear_session, get_session_state, set_session
from src.telegram_auth import ensure_telegram_configured, is_telegram_authorized
from src.telegram_outputs import send_outputs_telegram
from src.telegram_status import (
    JobProgress,
    format_queued_status,
    format_working_status,
    run_spinning_loader,
    stop_spinning_loader,
)
from src.timing import SPAN_ACK, SPAN_OUTPUTS, SPAN_TOTAL, JobTimings, debug_timing_enabled

logger = logging.getLogger(__name__)

MISSING_CONVERSATION_MARKER = "No conversation found with session ID"
SHUTDOWN_COMMAND = "종료"
ERROR_TEXT_KEYS = ("text", "result", "error")

JOB_TIMEOUT_ENV_VAR = "JOB_TIMEOUT_SECONDS"
DEFAULT_JOB_TIMEOUT_SECONDS = 600.0

MAX_CONCURRENT_JOBS_ENV_VAR = "MAX_CONCURRENT_JOBS"
DEFAULT_MAX_CONCURRENT_JOBS = 2

LOG_LEVEL_ENV_VAR = "LOG_LEVEL"
DEFAULT_LOG_LEVEL = logging.INFO
LOG_LEVEL_NAMES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# (limit, loop, semaphore) for the gate currently in use, plus the number of
# jobs holding or waiting for a slot. Rebuilt lazily -- same reasoning as
# main.py's _resolve_job_gate. A separate process from the Discord bot (if
# that one also runs), so this gate is independent of it.
_job_gate_state: tuple[int, asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None
_jobs_pending = 0


def _resolve_log_level(raw: str) -> int | None:
    if not raw:
        return DEFAULT_LOG_LEVEL
    level = logging.getLevelName(raw.upper())
    return level if isinstance(level, int) else None


def _configure_logging() -> None:
    raw = os.environ.get(LOG_LEVEL_ENV_VAR, "").strip()
    level = _resolve_log_level(raw)
    logging.basicConfig(level=DEFAULT_LOG_LEVEL if level is None else level, format=LOG_FORMAT)
    if level is None:
        logger.warning(
            "%s=%r is not a log level name; using %s. Expected one of %s.",
            LOG_LEVEL_ENV_VAR,
            raw,
            logging.getLevelName(DEFAULT_LOG_LEVEL),
            ", ".join(LOG_LEVEL_NAMES),
        )


def _job_timeout_seconds() -> float | None:
    raw = os.environ.get(JOB_TIMEOUT_ENV_VAR, "")
    if not str(raw).strip():
        return DEFAULT_JOB_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_JOB_TIMEOUT_SECONDS
    return value if value > 0 else None


def _max_concurrent_jobs() -> int:
    raw = os.environ.get(MAX_CONCURRENT_JOBS_ENV_VAR, "")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_MAX_CONCURRENT_JOBS
    return max(1, value)


def _resolve_job_gate() -> asyncio.Semaphore:
    global _job_gate_state, _jobs_pending

    limit = _max_concurrent_jobs()
    loop = asyncio.get_running_loop()
    if (
        _job_gate_state is None
        or _job_gate_state[0] != limit
        or _job_gate_state[1] is not loop
    ):
        _job_gate_state = (limit, loop, asyncio.Semaphore(limit))
        _jobs_pending = 0
    return _job_gate_state[2]


def _pending_job_count() -> int:
    return _jobs_pending


def _is_missing_conversation_error(meta: dict) -> bool:
    if not (meta.get("type") == "error" or meta.get("is_error")):
        return False
    return any(
        meta.get(key) and MISSING_CONVERSATION_MARKER in str(meta[key])
        for key in ERROR_TEXT_KEYS
    )


async def _run_job_with_session_recovery(
    job_dir: Path,
    *,
    resume_id: str | None,
    chat_id: int,
    explicit_session: bool,
    on_event=None,
    timings: JobTimings | None = None,
    scope: JobProcessScope | None = None,
) -> dict:
    meta = await run_job(
        job_dir, resume=resume_id, on_event=on_event, timings=timings, scope=scope
    )

    if resume_id and not explicit_session and _is_missing_conversation_error(meta):
        clear_session(chat_id)
        if timings is not None:
            timings.start_attempt(reason="stale_session_retry")
        meta = await run_job(
            job_dir, resume=None, on_event=on_event, timings=timings, scope=scope
        )
        meta["retried_without_stale_session"] = True
        meta["stale_session_id"] = resume_id

    return meta


def _resolve_resume_and_workdir(
    cmd, chat_id: int
) -> tuple[str | None, str | None, str | None, bool]:
    explicit_session = cmd.session_id is not None
    if explicit_session:
        return cmd.session_id, cmd.workdir, cmd.system_hint, True

    session_state = get_session_state(chat_id)
    resume_id = session_state.session_id if session_state else None
    workdir = cmd.workdir or (session_state.workdir if session_state else None)
    system_hint = cmd.system_hint or (session_state.system_hint if session_state else None)
    return resume_id, workdir, system_hint, False


async def _release_channel_warm_process(session_id: str | None) -> int:
    if not session_id:
        return 0

    pool = get_warm_pool()
    released = 0
    for wp in pool.idle_processes():
        if wp.session_id != session_id:
            continue
        pool.forget(wp)
        wp.close_stdin()
        released += 1
    return released


def _is_shutdown_command(text: str) -> bool:
    return text.strip() == SHUTDOWN_COMMAND


def _format_shutdown_reply(summary, *, cleared_sessions: int) -> str:
    killed_note = f" (강제 종료 {summary.killed}개)" if summary.killed else ""
    return (
        "Claude 세션 종료 완료.\n"
        f"- 실행 중이던 Claude CLI 프로세스: {summary.terminated}/{summary.requested}개 종료"
        f"{killed_note}\n"
        f"- 저장된 대화 세션: {cleared_sessions}개 초기화"
    )


async def _shutdown_claude_sessions():
    summary = await terminate_active_claude_processes()
    cleared_sessions = clear_all_sessions()
    return summary, cleared_sessions


async def _safe_edit_ack(ack: Message, content: str) -> None:
    try:
        await ack.edit_text(content)
    except telegram.error.TelegramError:
        logger.warning("Failed to edit ack message with final status", exc_info=True)
    except OSError:
        logger.warning("Failed to edit ack message with final status", exc_info=True)


async def _complete_job(ack: Message, status_line: str, job_dir: Path, timings: JobTimings) -> None:
    timings.stop(SPAN_TOTAL)
    timings.write(job_dir)
    summary = timings.format_line()
    logger.info("job %s timings: %s", job_dir.name, summary)
    if debug_timing_enabled():
        await _safe_edit_ack(ack, f"{status_line}\n{summary}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_telegram_authorized(update):
        return
    message = update.effective_message
    if message is None or not (message.text or "").strip():
        return

    timings = JobTimings()
    timings.start(SPAN_TOTAL)
    timings.start(SPAN_ACK)

    text = message.text.strip()
    chat_id = update.effective_chat.id

    if text == "/clear":
        state = get_session_state(chat_id)
        clear_session(chat_id)
        await _release_channel_warm_process(state.session_id if state else None)
        await message.reply_text("세션을 초기화했습니다. 다음 메시지부터 새 대화로 시작합니다.")
        return

    if _is_shutdown_command(text):
        summary, cleared_sessions = await _shutdown_claude_sessions()
        await message.reply_text(_format_shutdown_reply(summary, cleared_sessions=cleared_sessions))
        return

    direct_reply = direct_reply_for(text)
    if direct_reply:
        await message.reply_text(direct_reply)
        return

    cmd = parse(text)
    job_dir = await asyncio.to_thread(allocate_job)
    timings.job_id = job_dir.name

    await _dispatch_job(context, message, chat_id, cmd, job_dir, timings)


async def _dispatch_job(
    context: ContextTypes.DEFAULT_TYPE,
    message: Message,
    chat_id: int,
    cmd,
    job_dir: Path,
    timings: JobTimings,
) -> None:
    global _jobs_pending

    gate = _resolve_job_gate()
    jobs_ahead = _pending_job_count()
    queued = gate.locked()
    _jobs_pending += 1
    try:
        if queued:
            ack = await message.reply_text(format_queued_status(job_dir.name, jobs_ahead))
        else:
            ack = await message.reply_text(format_working_status(job_dir.name))
        timings.stop(SPAN_ACK)

        async with gate:
            resolved = await _prepare_queued_job(context, chat_id, ack, cmd, job_dir, timings)
            if resolved is None:
                return
            resume_id, system_hint, explicit_session = resolved

            progress = JobProgress()
            if queued:
                await _safe_edit_ack(ack, format_working_status(job_dir.name, progress))
            await _execute_job(
                context,
                chat_id,
                ack,
                job_dir,
                progress,
                timings,
                resume_id=resume_id,
                system_hint=system_hint,
                explicit_session=explicit_session,
            )
    finally:
        _jobs_pending = max(0, _jobs_pending - 1)


async def _prepare_queued_job(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    ack: Message,
    cmd,
    job_dir: Path,
    timings: JobTimings,
) -> tuple[str | None, str | None, bool] | None:
    resume_id, workdir, system_hint, explicit_session = _resolve_resume_and_workdir(cmd, chat_id)
    try:
        await asyncio.to_thread(prepare_job, job_dir, cmd.prompt, workdir, system_hint)
    except ValueError as exc:
        if resume_id and workdir and cmd.workdir is None and not explicit_session:
            clear_session(chat_id)
            await asyncio.to_thread(prepare_job, job_dir, cmd.prompt, None, system_hint)
            return None, system_hint, explicit_session

        logger.warning("job %s could not be prepared: %s", job_dir.name, exc)
        status_line = f"작업 실패 · {job_dir.name}"
        await _safe_edit_ack(ack, status_line)
        await context.bot.send_message(chat_id=chat_id, text=f"실행 불가: {redact_paths(str(exc))}")
        await _complete_job(ack, status_line, job_dir, timings)
        return None

    return resume_id, system_hint, explicit_session


async def _execute_job(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    ack: Message,
    job_dir: Path,
    progress: JobProgress,
    timings: JobTimings,
    *,
    resume_id: str | None,
    system_hint: str | None,
    explicit_session: bool,
) -> None:
    loader_task = asyncio.create_task(run_spinning_loader(ack, job_dir.name, progress))
    timeout = _job_timeout_seconds()
    scope = JobProcessScope()

    try:
        meta = await asyncio.wait_for(
            _run_job_with_session_recovery(
                job_dir,
                resume_id=resume_id,
                chat_id=chat_id,
                explicit_session=explicit_session,
                on_event=progress.record_event,
                timings=timings,
                scope=scope,
            ),
            timeout=timeout,
        )
        timings.start(SPAN_OUTPUTS)
    except TimeoutError:
        await stop_spinning_loader(loader_task)
        await _handle_job_timeout(context, chat_id, ack, job_dir, timings, timeout, scope)
        return
    except Exception as exc:
        await stop_spinning_loader(loader_task)
        logger.exception("job %s raised while running", job_dir.name)
        status_line = f"작업 실패 · {job_dir.name}"
        await _safe_edit_ack(ack, status_line)
        await context.bot.send_message(
            chat_id=chat_id, text=f"내부 오류: {safe_error_text(exc)} ({job_dir.name})"
        )
        await _complete_job(ack, status_line, job_dir, timings)
        return
    finally:
        await stop_spinning_loader(loader_task)

    if meta.get("session_id"):
        set_session(
            chat_id,
            meta["session_id"],
            workdir=meta.get("workdir"),
            system_hint=system_hint,
        )

    failed = meta.get("type") == "error" or meta.get("is_error")
    status = "작업 실패" if failed else "작업 완료"
    status_line = f"{status} · {job_dir.name}"
    await _safe_edit_ack(ack, status_line)
    if failed and meta.get("text"):
        logger.warning("job %s failed: %s", job_dir.name, meta["text"])
        await context.bot.send_message(chat_id=chat_id, text=redact_paths(str(meta["text"]))[:3900])
    await send_outputs_telegram(
        context.bot,
        chat_id,
        job_dir,
        body_text=meta.get("text_body"),
        warn_missing_manifest=not failed,
    )
    timings.stop(SPAN_OUTPUTS)
    await _complete_job(ack, status_line, job_dir, timings)


async def _handle_job_timeout(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    ack: Message,
    job_dir: Path,
    timings: JobTimings,
    timeout: float | None,
    scope: JobProcessScope,
) -> None:
    logger.warning(
        "job %s exceeded JOB_TIMEOUT_SECONDS=%s; terminating its claude processes",
        job_dir.name,
        timeout,
    )
    summary = await terminate_job_processes(scope)
    logger.warning("job %s timeout cleanup: %s", job_dir.name, summary)

    timings.set_meta(timed_out=True, timeout_seconds=timeout)
    status_line = f"작업 시간 초과 · {job_dir.name}"

    timings.start(SPAN_OUTPUTS)
    await _safe_edit_ack(ack, status_line)
    await send_outputs_telegram(context.bot, chat_id, job_dir, body_text=None, warn_missing_manifest=False)
    timings.stop(SPAN_OUTPUTS)
    await _complete_job(ack, status_line, job_dir, timings)


async def _on_startup(application: Application) -> None:
    removed = await asyncio.to_thread(cleanup_old_runs)
    if removed:
        logger.info("removed %s stale run directories", removed)


def main() -> None:
    _configure_logging()
    ensure_telegram_configured()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("환경변수 TELEGRAM_BOT_TOKEN이 필요합니다.")

    application = Application.builder().token(token).post_init(_on_startup).build()
    # No ~filters.COMMAND exclusion: "/clear" is itself parsed as a Telegram
    # bot-command entity, and it has to reach handle_message's own text-based
    # dispatch (mirroring main.py's on_message, which has no such split
    # either) rather than being filtered out before it gets there.
    application.add_handler(MessageHandler(filters.TEXT, handle_message))
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
