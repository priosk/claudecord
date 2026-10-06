"""Tests for src/telegram_bot.py: a condensed Telegram counterpart of
test_main.py, covering the pieces that differ (chat_id-keyed dispatch, the
Telegram ack/edit_text plumbing) rather than re-proving every scenario
test_main.py already covers against the identical orchestrator/runner code."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import telegram_bot as bot_module
from src.parser import Command
from src.sessions import SessionState
from src.telegram_status import QUEUED_MESSAGE, WORKING_MESSAGE
from src.timing import SPAN_TOTAL


class FakeAck:
    def __init__(self):
        self.edits = []

    async def edit_text(self, content):
        self.edits.append(content)


class FakeChat:
    def __init__(self, chat_id: int = 111):
        self.id = chat_id


class FakeMessage:
    def __init__(self, text: str, chat: FakeChat, ack: FakeAck):
        self.text = text
        self.chat = chat
        self._ack = ack
        self.reply_calls = []

    async def reply_text(self, content):
        self.reply_calls.append(content)
        return self._ack


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, *, chat_id, text):
        self.sent.append({"chat_id": chat_id, "text": text})


class FakeContext:
    def __init__(self):
        self.bot = FakeBot()


def _make_update(message: FakeMessage, chat: FakeChat):
    update = mock.Mock()
    update.effective_message = message
    update.effective_chat = chat
    return update


class ShutdownAndSessionHelpersTests(unittest.TestCase):
    def test_shutdown_command_matches_exact_korean_exit_word(self):
        self.assertTrue(bot_module._is_shutdown_command("종료"))
        self.assertTrue(bot_module._is_shutdown_command("  종료\n"))
        self.assertFalse(bot_module._is_shutdown_command("종료해줘"))

    def test_shutdown_clears_all_sessions_and_reports_processes(self):
        async def scenario():
            class Summary:
                requested = 2
                terminated = 2
                killed = 1
                still_running = 0

            with (
                mock.patch.object(
                    bot_module,
                    "terminate_active_claude_processes",
                    mock.AsyncMock(return_value=Summary()),
                ) as terminate,
                mock.patch.object(bot_module, "clear_all_sessions", return_value=3) as clear_all,
            ):
                actual_summary, cleared_sessions = await bot_module._shutdown_claude_sessions()
            return terminate, clear_all, actual_summary, cleared_sessions

        terminate, clear_all, summary, cleared_sessions = asyncio.run(scenario())
        reply = bot_module._format_shutdown_reply(summary, cleared_sessions=cleared_sessions)

        terminate.assert_awaited_once()
        clear_all.assert_called_once_with()
        self.assertIn("2/2개 종료", reply)
        self.assertIn("강제 종료 1개", reply)
        self.assertIn("3개 초기화", reply)

    def test_resolve_resume_and_workdir_uses_recent_chat_session(self):
        with mock.patch.object(
            bot_module,
            "get_session_state",
            return_value=SessionState("sess-1", workdir="/tmp/project"),
        ):
            resume_id, workdir, system_hint, explicit_session = bot_module._resolve_resume_and_workdir(
                Command(prompt="계속"), 123
            )

        self.assertEqual(resume_id, "sess-1")
        self.assertEqual(workdir, "/tmp/project")
        self.assertIsNone(system_hint)
        self.assertFalse(explicit_session)

    def test_resolve_resume_and_workdir_ignores_cached_state_for_explicit_session(self):
        with mock.patch.object(bot_module, "get_session_state") as get_session_state:
            resume_id, _workdir, _system_hint, explicit_session = bot_module._resolve_resume_and_workdir(
                Command(prompt="계속", session_id="sess-manual"), 123
            )

        get_session_state.assert_not_called()
        self.assertEqual(resume_id, "sess-manual")
        self.assertTrue(explicit_session)

    def test_missing_conversation_error_is_detected(self):
        self.assertTrue(
            bot_module._is_missing_conversation_error(
                {"type": "error", "text": "No conversation found with session ID: stale"}
            )
        )
        self.assertFalse(
            bot_module._is_missing_conversation_error(
                {"type": "result", "text": "No conversation found with session ID: stale"}
            )
        )

    def test_auto_resume_missing_conversation_clears_and_retries_fresh(self):
        async def scenario():
            calls = []

            async def fake_run_job(job_dir, resume=None, *, on_event=None, timings=None, scope=None):
                calls.append(resume)
                if len(calls) == 1:
                    return {"type": "error", "text": "No conversation found with session ID: stale"}
                return {"type": "result", "session_id": "fresh"}

            with (
                mock.patch.object(bot_module, "run_job", fake_run_job),
                mock.patch.object(bot_module, "clear_session") as clear_session,
            ):
                meta = await bot_module._run_job_with_session_recovery(
                    Path("job"), resume_id="stale", chat_id=123, explicit_session=False
                )
            return calls, clear_session, meta

        calls, clear_session, meta = asyncio.run(scenario())

        self.assertEqual(calls, ["stale", None])
        clear_session.assert_called_once_with(123)
        self.assertEqual(meta["session_id"], "fresh")


class JobLimitConfigTests(unittest.TestCase):
    def test_job_timeout_defaults_to_ten_minutes(self):
        for value in ({}, {"JOB_TIMEOUT_SECONDS": ""}, {"JOB_TIMEOUT_SECONDS": "잘못된값"}):
            with mock.patch.dict(os.environ, value, clear=True):
                self.assertEqual(bot_module._job_timeout_seconds(), 600.0)

    def test_max_concurrent_jobs_defaults_to_two_and_never_drops_below_one(self):
        for value, expected in (
            ({}, 2),
            ({"MAX_CONCURRENT_JOBS": "4"}, 4),
            ({"MAX_CONCURRENT_JOBS": "0"}, 1),
        ):
            with mock.patch.dict(os.environ, value, clear=True):
                self.assertEqual(bot_module._max_concurrent_jobs(), expected)


class HandleMessageFlowTests(unittest.TestCase):
    def test_handle_message_acks_and_forwards_text_body(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp) / "job-abc123"
                (job_dir / "logs").mkdir(parents=True)

                ack = FakeAck()
                chat = FakeChat()
                message = FakeMessage("테스트 요청", chat, ack)
                update = _make_update(message, chat)
                context = FakeContext()

                async def fake_run_job(passed_job_dir, resume=None, *, on_event=None, timings=None, scope=None):
                    return {
                        "type": "result",
                        "session_id": "sess-1",
                        "workdir": tmp,
                        "text_body": "실제 최종 답변",
                    }

                with (
                    mock.patch.object(bot_module, "is_telegram_authorized", return_value=True),
                    mock.patch.object(bot_module, "allocate_job", return_value=job_dir),
                    mock.patch.object(bot_module, "run_job", fake_run_job),
                    mock.patch.object(bot_module, "get_session_state", return_value=None),
                    mock.patch.object(bot_module, "set_session") as set_session,
                    mock.patch.object(bot_module, "send_outputs_telegram", mock.AsyncMock()) as send_outputs,
                ):
                    await bot_module.handle_message(update, context)

                return message, ack, set_session, send_outputs

        message, ack, set_session, send_outputs = asyncio.run(scenario())

        self.assertEqual(len(message.reply_calls), 1)
        self.assertIn(WORKING_MESSAGE, message.reply_calls[0])

        self.assertTrue(ack.edits)
        self.assertIn("작업 완료", ack.edits[-1])

        set_session.assert_called_once_with(111, "sess-1", workdir=mock.ANY, system_hint=None)

        send_outputs.assert_awaited_once()
        _, kwargs = send_outputs.call_args
        self.assertEqual(kwargs.get("body_text"), "실제 최종 답변")

    def test_clear_command_resets_session_without_running_a_job(self):
        async def scenario():
            ack = FakeAck()
            chat = FakeChat()
            message = FakeMessage("/clear", chat, ack)
            update = _make_update(message, chat)
            context = FakeContext()

            with (
                mock.patch.object(bot_module, "is_telegram_authorized", return_value=True),
                mock.patch.object(bot_module, "get_session_state", return_value=None),
                mock.patch.object(bot_module, "clear_session") as clear_session,
                mock.patch.object(bot_module, "allocate_job") as allocate_job,
            ):
                await bot_module.handle_message(update, context)

            return message, clear_session, allocate_job

        message, clear_session, allocate_job = asyncio.run(scenario())

        clear_session.assert_called_once_with(111)
        allocate_job.assert_not_called()
        self.assertIn("세션을 초기화했습니다", message.reply_calls[0])

    def test_shutdown_command_terminates_sessions_without_running_a_job(self):
        async def scenario():
            ack = FakeAck()
            chat = FakeChat()
            message = FakeMessage("종료", chat, ack)
            update = _make_update(message, chat)
            context = FakeContext()

            class Summary:
                requested = 1
                terminated = 1
                killed = 0
                still_running = 0

            with (
                mock.patch.object(bot_module, "is_telegram_authorized", return_value=True),
                mock.patch.object(
                    bot_module, "terminate_active_claude_processes", mock.AsyncMock(return_value=Summary())
                ),
                mock.patch.object(bot_module, "clear_all_sessions", return_value=1),
                mock.patch.object(bot_module, "allocate_job") as allocate_job,
            ):
                await bot_module.handle_message(update, context)

            return message, allocate_job

        message, allocate_job = asyncio.run(scenario())

        allocate_job.assert_not_called()
        self.assertIn("Claude 세션 종료 완료", message.reply_calls[0])

    def test_unauthorized_sender_is_ignored(self):
        async def scenario():
            ack = FakeAck()
            chat = FakeChat()
            message = FakeMessage("아무 말", chat, ack)
            update = _make_update(message, chat)
            context = FakeContext()

            with (
                mock.patch.object(bot_module, "is_telegram_authorized", return_value=False),
                mock.patch.object(bot_module, "allocate_job") as allocate_job,
            ):
                await bot_module.handle_message(update, context)

            return message, allocate_job

        message, allocate_job = asyncio.run(scenario())

        allocate_job.assert_not_called()
        self.assertEqual(message.reply_calls, [])


class HandleMessageConcurrencyAndTimeoutTests(unittest.TestCase):
    def test_a_job_over_the_limit_acks_as_queued_then_switches_to_working(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                first = Path(tmp) / "job-first"
                second = Path(tmp) / "job-second"
                for path in (first, second):
                    (path / "logs").mkdir(parents=True)

                first_started = asyncio.Event()
                release_first = asyncio.Event()

                async def fake_run_job(job_dir, resume=None, *, on_event=None, timings=None, scope=None):
                    if job_dir == first:
                        first_started.set()
                        await release_first.wait()
                    return {"type": "result", "session_id": "sess-q", "workdir": tmp, "text_body": "답변"}

                chat = FakeChat()
                ack_one, ack_two = FakeAck(), FakeAck()
                message_one = FakeMessage("첫 요청", chat, ack_one)
                message_two = FakeMessage("두 번째 요청", chat, ack_two)
                update_one = _make_update(message_one, chat)
                update_two = _make_update(message_two, chat)
                context = FakeContext()

                with (
                    mock.patch.object(bot_module, "is_telegram_authorized", return_value=True),
                    mock.patch.object(bot_module, "allocate_job", side_effect=[first, second]),
                    mock.patch.object(bot_module, "run_job", fake_run_job),
                    mock.patch.object(bot_module, "get_session_state", return_value=None),
                    mock.patch.object(bot_module, "set_session"),
                    mock.patch.object(bot_module, "send_outputs_telegram", mock.AsyncMock()),
                    mock.patch.dict(os.environ, {"MAX_CONCURRENT_JOBS": "1"}, clear=False),
                ):
                    task_one = asyncio.create_task(bot_module.handle_message(update_one, context))
                    await asyncio.wait_for(first_started.wait(), timeout=2)

                    task_two = asyncio.create_task(bot_module.handle_message(update_two, context))
                    deadline = asyncio.get_running_loop().time() + 2
                    while not message_two.reply_calls and asyncio.get_running_loop().time() < deadline:
                        await asyncio.sleep(0.001)

                    queued_ack = message_two.reply_calls[0]
                    edits_while_queued = list(ack_two.edits)

                    release_first.set()
                    await asyncio.wait_for(asyncio.gather(task_one, task_two), timeout=5)

                return queued_ack, edits_while_queued, ack_two.edits

        queued_ack, edits_while_queued, edits = asyncio.run(scenario())

        self.assertIn(QUEUED_MESSAGE, queued_ack)
        self.assertIn("앞에 1건", queued_ack)
        self.assertEqual(edits_while_queued, [])
        self.assertIn(WORKING_MESSAGE, edits[0])
        self.assertIn("작업 완료", edits[-1])

    def test_timed_out_job_reports_the_timeout_and_still_delivers_partial_output(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp) / "job-timeout1"
                (job_dir / "logs").mkdir(parents=True)
                (job_dir / "output.md").write_text("여기까지 진행된 부분 결과", encoding="utf-8")

                ack = FakeAck()
                chat = FakeChat()
                message = FakeMessage("오래 걸리는 요청", chat, ack)
                update = _make_update(message, chat)
                context = FakeContext()

                async def hanging_run_job(passed_job_dir, resume=None, *, on_event=None, timings=None, scope=None):
                    await asyncio.sleep(30)
                    raise AssertionError("run_job should have been cancelled")

                class Summary:
                    requested = 1
                    terminated = 1
                    killed = 0
                    still_running = 0

                with (
                    mock.patch.object(bot_module, "is_telegram_authorized", return_value=True),
                    mock.patch.object(bot_module, "allocate_job", return_value=job_dir),
                    mock.patch.object(bot_module, "run_job", hanging_run_job),
                    mock.patch.object(bot_module, "get_session_state", return_value=None),
                    mock.patch.object(bot_module, "set_session") as set_session,
                    mock.patch.object(
                        bot_module, "terminate_job_processes", mock.AsyncMock(return_value=Summary())
                    ) as terminate,
                    mock.patch.dict(os.environ, {"JOB_TIMEOUT_SECONDS": "0.05"}, clear=False),
                ):
                    await bot_module.handle_message(update, context)

                recorded = json.loads((job_dir / "timings.json").read_text(encoding="utf-8"))
                return ack, context.bot, terminate, set_session, job_dir.name, recorded

        ack, fake_bot, terminate, set_session, job_name, recorded = asyncio.run(scenario())

        self.assertTrue(ack.edits)
        self.assertIn("작업 시간 초과", ack.edits[-1])
        self.assertNotIn(WORKING_MESSAGE, ack.edits[-1])

        terminate.assert_awaited_once()
        set_session.assert_not_called()

        sent_texts = [entry["text"] for entry in fake_bot.sent]
        self.assertTrue(any("여기까지 진행된 부분 결과" in t for t in sent_texts))

        self.assertEqual(recorded["job_id"], job_name)
        self.assertIn(SPAN_TOTAL, recorded["spans_ms"])
        self.assertTrue(recorded["meta"].get("timed_out"))


if __name__ == "__main__":
    unittest.main()
