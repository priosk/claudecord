"""Tests for src/telegram_status.py: the Telegram counterpart of test_status.py.

JobProgress/record_event and stop_spinning_loader are re-exported unchanged
from src.status and are already covered there -- this file only exercises
what telegram_status.py actually rewrites: the (markdown-free) status text
and the editMessageText-based loader.
"""

import asyncio
import unittest
from itertools import pairwise
from unittest import mock

import telegram

from src.telegram_status import (
    EDIT_INTERVAL_GROWTH,
    EDIT_INTERVAL_SECONDS,
    MAX_EDIT_INTERVAL_SECONDS,
    QUEUED_MESSAGE,
    WORKING_MESSAGE,
    JobProgress,
    format_queued_status,
    format_working_status,
    run_spinning_loader,
    stop_spinning_loader,
)


class FakeMessage:
    """Fake telegram.Message-like target for run_spinning_loader."""

    def __init__(self, target_edits: int, progress: JobProgress | None = None):
        self.edits = []
        self.ready = asyncio.Event()
        self.target_edits = target_edits
        self.progress = progress

    async def edit_text(self, text):
        self.edits.append(text)
        if self.progress is not None and len(self.edits) == 1:
            self.progress.tool = "Bash"
        if len(self.edits) >= self.target_edits:
            self.ready.set()


def _record_sleeps(*, target_edits: int, **loader_kwargs) -> list[float]:
    async def scenario():
        message = FakeMessage(target_edits=target_edits)
        sleep_calls: list[float] = []
        real_sleep = asyncio.sleep

        async def spy_sleep(seconds):
            sleep_calls.append(seconds)
            await real_sleep(0)

        with mock.patch("src.telegram_status.asyncio.sleep", spy_sleep):
            task = asyncio.create_task(run_spinning_loader(message, "job-123", **loader_kwargs))
            await asyncio.wait_for(message.ready.wait(), timeout=5)
            await stop_spinning_loader(task)

        return sleep_calls

    return asyncio.run(scenario())


class TelegramStatusTextTests(unittest.TestCase):
    def test_format_working_status_has_no_markdown_markers(self):
        # No parse_mode is set when sending to Telegram (see telegram_outputs.py),
        # so a literal "**" would show up verbatim -- unlike format_working_status
        # in src.status, this one must not emit any.
        status_text = format_working_status("job-123")

        self.assertNotIn("**", status_text)
        self.assertIn(WORKING_MESSAGE, status_text)
        self.assertIn("경과 00:00", status_text)
        self.assertIn("job-123", status_text)

    def test_format_working_status_shows_tool_name_when_known(self):
        progress = JobProgress()
        progress.tool = "Bash"
        status_text = format_working_status("job-123", progress)

        self.assertIn("Bash", status_text)
        self.assertIn("도구 실행 중", status_text)

    def test_format_queued_status_has_no_markdown_markers(self):
        text = format_queued_status("job-abc123", 2)

        self.assertNotIn("**", text)
        self.assertEqual(QUEUED_MESSAGE, "대기 중")
        self.assertIn(f"{QUEUED_MESSAGE} (앞에 2건)", text)
        self.assertIn("job-abc123", text)
        self.assertNotIn(WORKING_MESSAGE, text)

    def test_format_queued_status_never_renders_a_negative_backlog(self):
        self.assertIn("앞에 0건", format_queued_status("job-abc123", -3))


class SpinningLoaderTests(unittest.TestCase):
    def test_the_first_edit_still_lands_at_the_base_interval(self):
        delays = _record_sleeps(target_edits=1)
        self.assertEqual(delays[0], EDIT_INTERVAL_SECONDS)

    def test_each_successive_wait_grows_by_the_configured_factor(self):
        delays = _record_sleeps(target_edits=5)
        expected = [EDIT_INTERVAL_SECONDS * EDIT_INTERVAL_GROWTH**i for i in range(5)]
        for actual, want in zip(delays[:5], expected, strict=True):
            self.assertAlmostEqual(actual, want, places=6)

    def test_the_interval_stops_growing_at_the_ceiling(self):
        delays = _record_sleeps(target_edits=40)
        self.assertLessEqual(max(delays), MAX_EDIT_INTERVAL_SECONDS)
        self.assertEqual(delays[-1], MAX_EDIT_INTERVAL_SECONDS)

    def test_spinning_loader_edits_message_and_reflects_progress_updates(self):
        async def scenario():
            progress = JobProgress()
            message = FakeMessage(target_edits=2, progress=progress)
            task = asyncio.create_task(run_spinning_loader(message, "job-123", progress, interval=0))
            await asyncio.wait_for(message.ready.wait(), timeout=1)
            await stop_spinning_loader(task)
            return message.edits

        edits = asyncio.run(scenario())

        self.assertGreaterEqual(len(edits), 2)
        self.assertTrue(all(WORKING_MESSAGE in edit for edit in edits))
        self.assertNotIn("Bash", edits[0])
        self.assertIn("Bash", edits[1])

    def test_spinning_loader_spaces_real_edits_by_at_least_the_given_interval(self):
        async def scenario():
            progress = JobProgress()
            interval = 0.05
            message = FakeMessage(target_edits=3, progress=progress)
            timestamps = []
            original_edit = message.edit_text

            async def timed_edit(text):
                timestamps.append(asyncio.get_running_loop().time())
                await original_edit(text)

            message.edit_text = timed_edit

            task = asyncio.create_task(run_spinning_loader(message, "job-123", progress, interval=interval))
            await asyncio.wait_for(message.ready.wait(), timeout=2)
            await stop_spinning_loader(task)
            return timestamps

        timestamps = asyncio.run(scenario())

        self.assertGreaterEqual(len(timestamps), 3)
        for a, b in pairwise(timestamps):
            self.assertGreaterEqual(b - a, 0.05 * 0.8)

    def test_loader_stops_quietly_on_telegram_error_without_propagating(self):
        async def scenario():
            class DeadMessage:
                async def edit_text(self, text):
                    raise telegram.error.TelegramError("message is not modified")

            task = asyncio.create_task(run_spinning_loader(DeadMessage(), "job-123", interval=0))
            for _ in range(50):
                await asyncio.sleep(0)
                if task.done():
                    break
            self.assertTrue(task.done())
            self.assertIsNone(task.exception())

        asyncio.run(scenario())

    def test_loader_stops_quietly_on_network_error_without_propagating(self):
        async def scenario():
            class FlakyMessage:
                async def edit_text(self, text):
                    raise OSError("simulated network blip")

            task = asyncio.create_task(run_spinning_loader(FlakyMessage(), "job-123", interval=0))
            for _ in range(50):
                await asyncio.sleep(0)
                if task.done():
                    break
            self.assertTrue(task.done())
            self.assertIsNone(task.exception())

        asyncio.run(scenario())

    def test_a_dead_loader_says_so_in_the_log(self):
        async def scenario():
            class DeadMessage:
                async def edit_text(self, text):
                    raise telegram.error.TelegramError("chat not found")

            task = asyncio.create_task(run_spinning_loader(DeadMessage(), "job-123", interval=0))
            for _ in range(50):
                await asyncio.sleep(0)
                if task.done():
                    break
            await stop_spinning_loader(task)

        with self.assertLogs("src.telegram_status", level="WARNING") as captured:
            asyncio.run(scenario())

        self.assertTrue(any("stopping loader" in line for line in captured.output))


if __name__ == "__main__":
    unittest.main()
