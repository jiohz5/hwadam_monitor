"""날짜별 로그와 원본을 보존하는 과거 로그 분리의 공개 계약."""
import datetime as dt
from contextlib import redirect_stderr
import io
import logging
import os
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest.mock import patch

import logfiles


class LogfileTests(unittest.TestCase):
    def setUp(self):
        self.module = logfiles
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.base = Path(self.folder.name)

    def logger(self):
        logger, handler = self.module.create_app_logger(self.base)
        self.addCleanup(handler.close)
        return logger, handler

    def record(self, logger, timestamp, message, *, event="APP", level=logging.INFO):
        record = logger.makeRecord(logger.name, level, __file__, 1, message, (), None,
                                   extra={"event": event})
        record.created = dt.datetime.fromisoformat(timestamp).timestamp()
        logger.handle(record)

    def read(self, name):
        return (self.base / "logs" / name).read_text(encoding="utf-8")

    def test_midnight_files_and_timestamps_use_kst_record_time(self):
        # UTC 날짜나 현재 처리 시각을 쓰면 자정 양쪽 기록이 잘못 배치된다.
        class UTCHostDateTime(dt.datetime):
            @classmethod
            def fromtimestamp(cls, timestamp, tz=None):
                # 명시한 시간대가 없으면 UTC인 운영체제의 시계를 재현한다.
                return super().fromtimestamp(timestamp, tz or dt.timezone.utc)

        logger, _ = self.logger()
        with patch.object(self.module.dt, "datetime", UTCHostDateTime):
            self.record(logger, "2026-10-14T14:59:59.125+00:00", "자정 전", event="WATCH_START")
            self.record(logger, "2026-10-14T15:00:00.250+00:00", "자정 후", event="WATCH_STOP")
        before = self.read("hwadam-2026-10-14.log")
        after = self.read("hwadam-2026-10-15.log")
        self.assertRegex(before, r"2026-10-14 23:59:59\.125\+09:00 INFO run=[0-9a-f]{8} event=WATCH_START 자정 전\n$")
        self.assertRegex(after, r"2026-10-15 00:00:00\.250\+09:00 INFO run=[0-9a-f]{8} event=WATCH_STOP 자정 후\n$")
        self.assertNotIn("자정 후", before)
        self.assertNotIn("자정 전", after)

    def test_late_old_day_record_returns_to_original_file_without_truncating(self):
        logger, _ = self.logger()
        self.record(logger, "2026-10-14T23:59:58+09:00", "old-first")
        self.record(logger, "2026-10-15T00:00:01+09:00", "new-day")
        self.record(logger, "2026-10-14T23:59:59+09:00", "old-delayed")
        self.assertEqual([line.rsplit(" ", 1)[1] for line in self.read("hwadam-2026-10-14.log").splitlines()],
                         ["old-first", "old-delayed"])
        self.assertEqual(len(self.read("hwadam-2026-10-15.log").splitlines()), 1)

    def test_restart_appends_and_distinguishes_runs(self):
        first, handler = self.logger()
        self.record(first, "2026-10-15T10:00:00+09:00", "first")
        handler.close()
        second, _ = self.logger()
        self.record(second, "2026-10-15T10:01:00+09:00", "second")
        text = self.read("hwadam-2026-10-15.log")
        self.assertEqual(len(text.splitlines()), 2)
        self.assertEqual(len(set(re.findall(r"run=([0-9a-f]{8})", text))), 2)

    def test_direct_logger_calls_redact_formatted_values_and_default_event(self):
        logger, _ = self.logger()
        logger.warning("bot=%s orderGroupId=%s", "123456:abcdefghijk", "PRIVATE_ORDER")
        paths = list((self.base / "logs").glob("hwadam-*.log"))
        self.assertEqual(len(paths), 1)
        text = paths[0].read_text(encoding="utf-8")
        self.assertIn(" WARNING ", text)
        self.assertIn(" event=APP ", text)
        self.assertIn("<봇 토큰 숨김>", text)
        self.assertIn("orderGroupId=<주문번호 숨김>", text)
        self.assertNotIn("123456:abcdefghijk", text)
        self.assertNotIn("PRIVATE_ORDER", text)

    def test_exception_logging_persists_type_without_exception_message_or_traceback(self):
        logger, _ = self.logger()
        try:
            raise ValueError("PRIVATE_EXCEPTION_TEXT")
        except ValueError:
            logger.exception("작업 실패", extra={"event": "WATCH_ERROR"}, stack_info=True)
        text = next((self.base / "logs").glob("hwadam-*.log")).read_text(encoding="utf-8")
        self.assertIn("error_type=ValueError", text)
        self.assertNotIn("PRIVATE_EXCEPTION_TEXT", text)
        self.assertNotIn("Traceback", text)
        self.assertNotIn("Stack (most recent", text)
        self.assertEqual(len(text.splitlines()), 1)

    def test_closed_handler_never_reopens_for_pending_record(self):
        logger, handler = self.logger()
        self.record(logger, "2026-10-14T23:59:59+09:00", "before-close")
        before = self.read("hwadam-2026-10-14.log")
        handler.close()
        self.record(logger, "2026-10-15T00:00:01+09:00", "pending-after-close")
        self.assertEqual(self.read("hwadam-2026-10-14.log"), before)
        self.assertFalse((self.base / "logs" / "hwadam-2026-10-15.log").exists())

    def test_app_loggers_do_not_duplicate_each_others_records(self):
        first, _ = self.logger()
        second, _ = self.logger()
        self.record(first, "2026-10-15T10:00:00+09:00", "one")
        self.record(second, "2026-10-15T10:00:01+09:00", "two")
        self.assertEqual(len(self.read("hwadam-2026-10-15.log").splitlines()), 2)

    def test_concurrent_writes_remain_complete_once_each(self):
        logger, _ = self.logger()
        errors = []
        def write(worker):
            try:
                for number in range(40):
                    self.record(logger, "2026-10-15T10:00:00+09:00", f"worker-{worker}-item-{number}")
            except Exception as error:
                errors.append(error)
        workers = [threading.Thread(target=write, args=(number,)) for number in range(6)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        lines = self.read("hwadam-2026-10-15.log").splitlines()
        expected = {f"worker-{worker}-item-{number}" for worker in range(6) for number in range(40)}
        self.assertEqual(len(lines), 240)
        self.assertEqual({line.rsplit(" ", 1)[1] for line in lines}, expected)

    def test_file_open_failure_does_not_interrupt_caller_and_both_dates_can_retry(self):
        # 실제 경로 충돌로 새 날짜 파일 열기를 실패시킨다.
        logger, _ = self.logger()
        self.record(logger, "2026-10-14T23:59:58+09:00", "first")
        blocked = self.base / "logs" / "hwadam-2026-10-15.log"
        blocked.mkdir()
        diagnostics = io.StringIO()
        with redirect_stderr(diagnostics):
            try:
                self.record(logger, "2026-10-15T00:00:00+09:00", "PRIVATE_RECORD 123456:abcdefghijk")
                self.record(logger, "2026-10-14T23:59:59+09:00", "old-retry")
            except OSError as error:
                self.fail(f"로그 파일 오류가 감시 호출자에게 전파됨: {type(error).__name__}")
        blocked.rmdir()
        self.record(logger, "2026-10-15T00:00:01+09:00", "new-retry")
        self.assertIn("first", self.read("hwadam-2026-10-14.log"))
        self.assertIn("old-retry", self.read("hwadam-2026-10-14.log"))
        self.assertIn("new-retry", self.read("hwadam-2026-10-15.log"))
        self.assertIn("error_type=PermissionError", diagnostics.getvalue())
        self.assertNotIn("PRIVATE_RECORD", diagnostics.getvalue())
        self.assertNotIn("123456:abcdefghijk", diagnostics.getvalue())
        self.assertNotIn("Traceback", diagnostics.getvalue())

    def test_real_write_failure_is_contained_and_next_record_reopens_file(self):
        logger, handler = self.logger()
        self.record(logger, "2026-10-15T10:00:00+09:00", "before-failure")
        # 소유한 파일 기술자를 OS에서 끊어 실제 write/flush OSError를 일으킨다.
        os.close(handler._stream.fileno())
        with redirect_stderr(io.StringIO()):
            try:
                self.record(logger, "2026-10-15T10:00:01+09:00", "failed-record")
            except OSError as error:
                self.fail(f"로그 쓰기 오류가 감시 호출자에게 전파됨: {type(error).__name__}")
        self.record(logger, "2026-10-15T10:00:02+09:00", "after-recovery")
        text = self.read("hwadam-2026-10-15.log")
        self.assertIn("before-failure", text)
        self.assertIn("after-recovery", text)
        self.assertNotIn("failed-record", text)

    def test_flush_io_failure_does_not_interrupt_shutdown_and_allows_recovery(self):
        logger, handler = self.logger()
        self.record(logger, "2026-10-15T10:00:00+09:00", "before-flush")
        handler._stream.write("pending-buffer")
        os.close(handler._stream.fileno())
        with redirect_stderr(io.StringIO()):
            try:
                handler.flush()
            except OSError as error:
                self.fail(f"로그 flush 오류가 종료 호출자에게 전파됨: {type(error).__name__}")
        self.record(logger, "2026-10-15T10:00:01+09:00", "after-flush-recovery")
        self.assertIn("after-flush-recovery", self.read("hwadam-2026-10-15.log"))

    def test_close_io_failure_still_closes_handler_without_reopening(self):
        logger, handler = self.logger()
        self.record(logger, "2026-10-15T10:00:00+09:00", "before-close")
        handler._stream.write("pending-buffer")
        os.close(handler._stream.fileno())
        with redirect_stderr(io.StringIO()):
            try:
                handler.close()
                handler.close()
            except OSError as error:
                self.fail(f"로그 close 오류가 종료 호출자에게 전파됨: {type(error).__name__}")
        self.record(logger, "2026-10-16T10:00:00+09:00", "after-close")
        self.assertFalse((self.base / "logs" / "hwadam-2026-10-16.log").exists())

    def seed_legacy(self, files):
        (self.base / "logs").mkdir()
        for name, text in files.items():
            (self.base / "logs" / name).write_text(text, encoding="utf-8")

    def test_split_legacy_preserves_records_continuations_and_numeric_rotation_order(self):
        files = {
            "hwadam.log.10": "leading fragment\n2026-10-13 23:59:59,999 oldest\n  trace first\n",
            "hwadam.log.2": "  trace continued\n2026-10-14 12:00:00,000 older\n",
            "hwadam.log.1": "2026-10-14 12:01:00,000 recent\n",
            "hwadam.log": "2026-10-15 00:00:00,000 newest\n  last trace\n",
            "hwadam.log.backup": "not a numbered rotation\n",
        }
        self.seed_legacy(files)
        summary = self.module.split_legacy_logs(self.base)
        self.assertEqual([path.name for path in summary.source_paths],
                         ["hwadam.log.10", "hwadam.log.2", "hwadam.log.1", "hwadam.log"])
        self.assertEqual({path.name for path in summary.output_paths},
                         {"undated.log", "2026-10-13.log", "2026-10-14.log", "2026-10-15.log"})
        self.assertEqual(summary.record_count, 4)
        self.assertEqual(summary.line_count, 8)
        self.assertEqual(self.read("legacy/undated.log"), "leading fragment\n")
        self.assertEqual(self.read("legacy/2026-10-13.log"),
                         "2026-10-13 23:59:59,999 oldest\n  trace first\n  trace continued\n")
        self.assertEqual(self.read("legacy/2026-10-14.log"),
                         "2026-10-14 12:00:00,000 older\n2026-10-14 12:01:00,000 recent\n")
        self.assertEqual(self.read("legacy/2026-10-15.log"),
                         "2026-10-15 00:00:00,000 newest\n  last trace\n")
        for name, text in files.items():
            self.assertEqual(self.read(name), text)

    def test_split_legacy_rerun_replaces_derived_snapshot_without_duplicates(self):
        self.seed_legacy({"hwadam.log": "2026-10-15 01:02:03,456 one\n"})
        self.module.split_legacy_logs(self.base)
        original = self.read("legacy/2026-10-15.log")
        second = self.module.split_legacy_logs(self.base)
        self.assertEqual(self.read("legacy/2026-10-15.log"), original)
        self.assertEqual(second.record_count, 1)
        with (self.base / "logs" / "hwadam.log").open("a", encoding="utf-8") as stream:
            stream.write("2026-10-15 01:02:04,567 two\n")
        third = self.module.split_legacy_logs(self.base)
        self.assertEqual(third.record_count, 2)
        self.assertEqual(self.read("legacy/2026-10-15.log"), original + "2026-10-15 01:02:04,567 two\n")
        self.assertFalse(list((self.base / "logs").glob("hwadam-*.log")))

    def test_split_legacy_failed_atomic_replace_keeps_previous_snapshot_and_original(self):
        self.seed_legacy({"hwadam.log": "2026-10-15 01:02:03,456 one\n"})
        self.module.split_legacy_logs(self.base)
        original = self.read("hwadam.log")
        with patch.object(Path, "replace", side_effect=OSError("replace denied")):
            with self.assertRaises(OSError):
                self.module.split_legacy_logs(self.base)
        self.assertEqual(self.read("hwadam.log"), original)
        self.assertEqual(self.read("legacy/2026-10-15.log"), original)
        self.assertFalse(list((self.base / "logs" / "legacy").glob("*.tmp")))

    def test_split_legacy_missing_sources_has_empty_summary(self):
        summary = self.module.split_legacy_logs(self.base)
        self.assertEqual(summary.source_paths, ())
        self.assertEqual(summary.output_paths, ())
        self.assertEqual(summary.record_count, 0)
        self.assertEqual(summary.line_count, 0)
        self.assertFalse((self.base / "logs" / "legacy").exists())


if __name__ == "__main__":
    unittest.main()
