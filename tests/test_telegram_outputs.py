"""Tests for src/telegram_outputs.py: the Telegram counterpart of a subset of
test_outputs.py. SVG-preview rendering is intentionally not covered here --
telegram_outputs.py does not implement it (see its module docstring)."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import telegram_outputs
from src.telegram_outputs import send_outputs_telegram


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, *, chat_id, text):
        self.sent.append({"kind": "message", "chat_id": chat_id, "text": text})

    async def send_document(self, *, chat_id, document, filename=None, caption=None):
        content = document.read() if hasattr(document, "read") else None
        self.sent.append(
            {
                "kind": "document",
                "chat_id": chat_id,
                "path": document if not hasattr(document, "read") else None,
                "filename": filename,
                "caption": caption,
                "content": content,
            }
        )

    def texts(self):
        return [entry["text"] for entry in self.sent if entry["kind"] == "message"]


class SendOutputsTelegramTests(unittest.TestCase):
    def test_uses_body_text_when_no_output_md(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                bot = FakeBot()
                await send_outputs_telegram(bot, 42, Path(tmp), body_text="본문입니다")
                return bot

        bot = asyncio.run(scenario())
        self.assertIn("본문입니다", bot.texts())

    def test_prefers_existing_nonempty_output_md_over_body_text(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp)
                (job_dir / "output.md").write_text("파일 본문", encoding="utf-8")
                bot = FakeBot()
                await send_outputs_telegram(bot, 42, job_dir, body_text="폴백 본문")
                return bot

        bot = asyncio.run(scenario())
        self.assertIn("파일 본문", bot.texts())
        self.assertNotIn("폴백 본문", bot.texts())

    def test_non_utf8_output_md_falls_back_to_body_text(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp)
                (job_dir / "output.md").write_bytes(b"\xff\xfe\x00broken")
                bot = FakeBot()
                await send_outputs_telegram(bot, 42, job_dir, body_text="폴백 본문")
                return bot

        bot = asyncio.run(scenario())
        self.assertIn("폴백 본문", bot.texts())

    def test_sends_notice_for_genuinely_empty_result(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                bot = FakeBot()
                await send_outputs_telegram(bot, 42, Path(tmp))
                return bot

        bot = asyncio.run(scenario())
        self.assertIn("(전달할 결과가 없습니다.)", bot.texts())

    def test_can_suppress_empty_result_notice(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                bot = FakeBot()
                await send_outputs_telegram(bot, 42, Path(tmp), warn_missing_manifest=False)
                return bot

        bot = asyncio.run(scenario())
        self.assertEqual(bot.sent, [])

    def test_prefers_meta_json_over_manifest_json(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp)
                (job_dir / "meta.jpg.txt").write_text("x")  # unrelated file, sanity noise
                (job_dir / "meta_file.txt").write_text("meta content", encoding="utf-8")
                (job_dir / "manifest_file.txt").write_text("manifest content", encoding="utf-8")
                (job_dir / "meta.json").write_text(
                    json.dumps({"files": [{"path": "meta_file.txt"}]}), encoding="utf-8"
                )
                (job_dir / "manifest.json").write_text(
                    json.dumps({"files": [{"path": "manifest_file.txt"}]}), encoding="utf-8"
                )
                bot = FakeBot()
                await send_outputs_telegram(bot, 42, job_dir)
                return bot

        bot = asyncio.run(scenario())
        docs = [e for e in bot.sent if e["kind"] == "document"]
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["filename"], "meta_file.txt")

    def test_falls_back_to_manifest_json_when_meta_json_missing(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp)
                (job_dir / "manifest_file.txt").write_text("manifest content", encoding="utf-8")
                (job_dir / "manifest.json").write_text(
                    json.dumps({"files": [{"path": "manifest_file.txt"}]}), encoding="utf-8"
                )
                bot = FakeBot()
                await send_outputs_telegram(bot, 42, job_dir)
                return bot

        bot = asyncio.run(scenario())
        docs = [e for e in bot.sent if e["kind"] == "document"]
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["filename"], "manifest_file.txt")

    def test_meta_lists_only_missing_files_falls_back_to_manifest(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp)
                (job_dir / "manifest_file.txt").write_text("manifest content", encoding="utf-8")
                (job_dir / "meta.json").write_text(
                    json.dumps({"files": [{"path": "never-written.txt"}]}), encoding="utf-8"
                )
                (job_dir / "manifest.json").write_text(
                    json.dumps({"files": [{"path": "manifest_file.txt"}]}), encoding="utf-8"
                )
                bot = FakeBot()
                await send_outputs_telegram(bot, 42, job_dir)
                return bot

        bot = asyncio.run(scenario())
        docs = [e for e in bot.sent if e["kind"] == "document"]
        self.assertEqual([d["filename"] for d in docs], ["manifest_file.txt"])

    def test_rejects_manifest_path_traversal(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp)
                (job_dir / "manifest.json").write_text(
                    json.dumps({"files": [{"path": "../escape.txt"}]}), encoding="utf-8"
                )
                bot = FakeBot()
                await send_outputs_telegram(bot, 42, job_dir)
                return bot

        bot = asyncio.run(scenario())
        self.assertTrue(any("manifest 경로를 무시했습니다" in t for t in bot.texts()))

    def test_rejects_absolute_manifest_path(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp)
                (job_dir / "manifest.json").write_text(
                    json.dumps({"files": [{"path": "/etc/passwd"}]}), encoding="utf-8"
                )
                bot = FakeBot()
                await send_outputs_telegram(bot, 42, job_dir)
                return bot

        bot = asyncio.run(scenario())
        self.assertTrue(any("manifest 경로를 무시했습니다" in t for t in bot.texts()))

    def test_reports_malformed_manifest_without_raising(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp)
                (job_dir / "manifest.json").write_text("not json", encoding="utf-8")
                bot = FakeBot()
                await send_outputs_telegram(bot, 42, job_dir)
                return bot

        bot = asyncio.run(scenario())
        self.assertTrue(any("manifest.json을 읽을 수 없습니다" in t for t in bot.texts()))

    def test_attachment_over_limit_is_reported_and_not_sent(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp)
                big_file = job_dir / "big.bin"
                big_file.write_bytes(b"x" * 10)
                (job_dir / "manifest.json").write_text(
                    json.dumps({"files": [{"path": "big.bin"}]}), encoding="utf-8"
                )
                bot = FakeBot()
                with mock.patch.object(telegram_outputs, "MAX_ATTACH_BYTES", 5):
                    await send_outputs_telegram(bot, 42, job_dir)
                return bot

        bot = asyncio.run(scenario())
        self.assertEqual([e for e in bot.sent if e["kind"] == "document"], [])
        self.assertTrue(any("MB를 초과해 첨부 불가" in t for t in bot.texts()))

    def test_switches_to_attachment_when_too_many_chunks(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                bot = FakeBot()
                long_text = "x" * (telegram_outputs.TELEGRAM_CHUNK_LIMIT * 5)
                await send_outputs_telegram(bot, 42, Path(tmp), body_text=long_text)
                return bot, long_text

        bot, long_text = asyncio.run(scenario())
        docs = [e for e in bot.sent if e["kind"] == "document"]
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["filename"], "response.md")
        self.assertEqual(docs[0]["content"].decode("utf-8"), long_text)
        self.assertEqual([e for e in bot.sent if e["kind"] == "message"], [])

    def test_respects_output_inline_max_chunks_override(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                bot = FakeBot()
                short_text = "x" * (telegram_outputs.TELEGRAM_CHUNK_LIMIT * 2)
                with mock.patch.dict(os.environ, {"OUTPUT_INLINE_MAX_CHUNKS": "1"}):
                    await send_outputs_telegram(bot, 42, Path(tmp), body_text=short_text)
                return bot

        bot = asyncio.run(scenario())
        docs = [e for e in bot.sent if e["kind"] == "document"]
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["filename"], "response.md")

    def test_chunk_never_exceeds_the_requested_limit(self):
        text = "가" * 10000
        chunks = telegram_outputs._chunk(text, 100)
        self.assertTrue(all(len(c) <= 100 for c in chunks))
        self.assertEqual("".join(chunks), text)



if __name__ == "__main__":
    unittest.main()
