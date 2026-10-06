"""Telegram counterpart to src/outputs.py.

Deliberately not a thin wrapper around outputs.py: that module's send helpers
take a discord.abc.Messageable and build discord.File objects, so reusing them
would mean patching Discord types anyway. This module re-implements the same
job-output contract (meta.json/manifest.json entries, output.md-first body,
empty-result notice) against the Telegram Bot API instead.

Two simplifications versus the Discord side, both noted where they apply:
- No parse_mode is set, so the body is sent as plain text. That sidesteps
  MarkdownV2's escaping rules entirely, at the cost of code fences rendering
  as literal ``` characters instead of a monospace block.
- Inline SVG code blocks are not extracted into their own attachment (outputs.py
  does this for Discord); they are sent as plain text like the rest of the body.
"""

import asyncio
import io
import json
import logging
import os
from pathlib import Path
from typing import Any

import telegram

from src.errors import redact_paths

logger = logging.getLogger(__name__)

# Telegram's hard per-message character cap is 4096. TELEGRAM_CHUNK_LIMIT
# stays under it with headroom for the same reason DISCORD_CHUNK_LIMIT does
# in outputs.py: length-counting mismatches between Python codepoints and
# what Telegram counts (UTF-16 code units, same as Discord).
TELEGRAM_MESSAGE_LIMIT = 4096
TELEGRAM_CHUNK_LIMIT = 4000

# Telegram Bot API's own limit for bot-uploaded documents.
MAX_ATTACH_BYTES = 50 * 1024 * 1024

# Mirrors OUTPUT_INLINE_MAX_CHUNKS in outputs.py: the same knob (env var name
# shared on purpose) controls when a long reply switches from paginated
# messages to a single response.md attachment, on either platform.
OUTPUT_INLINE_MAX_CHUNKS_ENV_VAR = "OUTPUT_INLINE_MAX_CHUNKS"
DEFAULT_OUTPUT_INLINE_MAX_CHUNKS = 3

_PREVIEW_SUFFIX = "\n\n… (본문이 길어 response.md 파일로 전체를 첨부했습니다)"


async def send_outputs_telegram(
    bot: telegram.Bot,
    chat_id: int,
    job_dir: Path,
    *,
    body_text: str | None = None,
    warn_missing_manifest: bool = True,
) -> None:
    """Send a job's answer body and attachments to a Telegram chat.

    Same body/attachment precedence contract as outputs.send_outputs: see
    that function's docstring for the full rationale (output.md-first body,
    key-level meta.json/manifest.json fallback, "actually transmitted" empty
    check).
    """
    body_source = await _resolve_body_text(job_dir, body_text)
    if body_source:
        await _send_body_text(bot, chat_id, body_source)

    entries, manifest_error = await _resolve_manifest_entries(job_dir)
    sent_file_count = await _send_attachment_entries(bot, chat_id, job_dir, entries)

    if manifest_error:
        await bot.send_message(chat_id=chat_id, text=manifest_error)
        return

    if not body_source and sent_file_count == 0 and warn_missing_manifest:
        await bot.send_message(chat_id=chat_id, text="(전달할 결과가 없습니다.)")


async def _resolve_body_text(job_dir: Path, body_text: str | None) -> str | None:
    text = await asyncio.to_thread(_read_output_md, job_dir)
    if text:
        return text
    return body_text or None


def _read_output_md(job_dir: Path) -> str | None:
    output_md = job_dir / "output.md"
    if output_md.exists():
        try:
            text = output_md.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            text = None
        if text and text.strip():
            return text
    return None


async def _load_entries_from(path: Path) -> tuple[list[Any] | None, str | None]:
    return await asyncio.to_thread(_read_entries_from, path)


def _read_entries_from(path: Path) -> tuple[list[Any] | None, str | None]:
    if not path.exists():
        return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return None, f"{path.name}을 읽을 수 없습니다: {exc}"
    except OSError:
        logger.warning("Failed to read job bookkeeping file %s", path, exc_info=True)
        return None, f"{path.name}을 읽을 수 없습니다."
    if not isinstance(data, dict):
        return None, f"{path.name} 형식이 올바르지 않습니다."
    entries = data.get("files", [])
    if not isinstance(entries, list):
        return None, f"{path.name}의 files 항목이 배열이 아닙니다."
    return entries, None


def _has_any_existing_file(job_dir: Path, entries: list[Any]) -> bool:
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        p = _safe_manifest_path(job_dir, entry.get("path"))
        if p is not None and p.exists():
            return True
    return False


async def _resolve_manifest_entries(job_dir: Path) -> tuple[list[Any], str | None]:
    meta_entries, meta_error = await _load_entries_from(job_dir / "meta.json")
    if meta_entries and await asyncio.to_thread(_has_any_existing_file, job_dir, meta_entries):
        return meta_entries, None

    manifest_entries, manifest_error = await _load_entries_from(job_dir / "manifest.json")
    if manifest_entries:
        return manifest_entries, None

    if meta_entries:
        return meta_entries, None

    return [], meta_error or manifest_error


def _safe_manifest_path(job_dir: Path, raw_path: Any) -> Path | None:
    if not isinstance(raw_path, str) or not raw_path:
        return None

    rel_path = Path(raw_path)
    if rel_path.is_absolute():
        return None

    root = job_dir.resolve()
    candidate = (root / rel_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


async def _send_attachment_entries(
    bot: telegram.Bot, chat_id: int, job_dir: Path, entries: list[Any]
) -> int:
    sent_count = 0
    sent_paths = set()

    for entry in entries:
        if not isinstance(entry, dict):
            await bot.send_message(chat_id=chat_id, text="manifest 파일 항목이 올바르지 않아 무시했습니다.")
            continue

        p = _safe_manifest_path(job_dir, entry.get("path"))
        if p is None:
            await bot.send_message(
                chat_id=chat_id,
                text=f"manifest 경로를 무시했습니다: {redact_paths(str(entry.get('path')))}",
            )
            continue
        if not p.exists():
            continue

        resolved = p.resolve()
        if resolved in sent_paths:
            continue
        sent_paths.add(resolved)

        if p.stat().st_size > MAX_ATTACH_BYTES:
            label = entry.get("label") or p.name
            limit_mb = MAX_ATTACH_BYTES // (1024 * 1024)
            await bot.send_message(
                chat_id=chat_id,
                text=f"파일 {label}이 {limit_mb}MB를 초과해 첨부 불가. 경로: {redact_paths(str(p))}",
            )
            continue

        await bot.send_document(chat_id=chat_id, document=p, filename=p.name)
        sent_count += 1

    return sent_count


def _effective_inline_max_chunks() -> int:
    raw = os.environ.get(OUTPUT_INLINE_MAX_CHUNKS_ENV_VAR)
    if raw is None or not raw.strip():
        return DEFAULT_OUTPUT_INLINE_MAX_CHUNKS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_OUTPUT_INLINE_MAX_CHUNKS
    if value <= 0:
        return DEFAULT_OUTPUT_INLINE_MAX_CHUNKS
    return value


async def _send_body_text(bot: telegram.Bot, chat_id: int, text: str) -> None:
    chunks = _chunk(text, TELEGRAM_CHUNK_LIMIT)
    if len(chunks) <= _effective_inline_max_chunks():
        for chunk in chunks:
            await bot.send_message(chat_id=chat_id, text=chunk)
        return

    preview_budget = max(TELEGRAM_CHUNK_LIMIT - len(_PREVIEW_SUFFIX), 200)
    preview_chunks = _chunk(text, preview_budget)
    preview = (preview_chunks[0].rstrip() if preview_chunks else "") + _PREVIEW_SUFFIX
    document = io.BytesIO(text.encode("utf-8"))
    await bot.send_document(chat_id=chat_id, document=document, filename="response.md", caption=preview[:1024])


def _chunk(text: str, limit: int = TELEGRAM_CHUNK_LIMIT) -> list[str]:
    """Split ``text`` into pieces of at most ``limit`` characters.

    No code-fence tracking here (compare outputs._chunk): without a
    parse_mode, Telegram never renders ``` as a fence in the first place, so
    there is nothing for a split to visually break.
    """
    if not text:
        return []
    return [text[i : i + limit] for i in range(0, len(text), limit)]
