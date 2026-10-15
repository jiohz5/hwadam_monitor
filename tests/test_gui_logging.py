from pathlib import Path
import tempfile
import threading
import tkinter as tk
import unittest

from gui import App
from model import Availability, Snapshot, WatchConfig


class GuiLoggingTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.base = Path(self.folder.name)
        self.root = tk.Tk(); self.root.withdraw()
        self.app = App(self.root, self.base)
        self.root.update()

    def tearDown(self):
        if not self.app.closed: self.app.close()
        cleanup = getattr(self.app, 'cleanup_thread', None)
        if cleanup: cleanup.join(3)
        self.folder.cleanup()

    def logs(self):
        return '\n'.join(path.read_text(encoding='utf-8') for path in (self.base/'logs').glob('hwadam-*.log'))

    def test_worker_failure_is_saved_before_poll_and_survives_stale_response(self):
        self.app._submit(lambda: (_ for _ in ()).throw(RuntimeError('목록 조회 실패 TEST_FAILURE')), 'loaded', ticket=self.app.guard.ticket())
        self.app.executor.shutdown(wait=True)
        self.assertIn('TEST_FAILURE', self.logs())
        self.assertIn('operation=loaded', self.logs())
        self.app.guard.changed()
        self.app._poll()
        self.assertEqual(self.logs().count('TEST_FAILURE'), 1)
        self.assertIn('이전 조회 결과', self.app.home_log_text.get('1.0', 'end'))

    def test_snapshot_logs_monorail_stock_and_error_before_gui_renders_once(self):
        snapshot = Snapshot(WatchConfig(include_monorail=True),
            (Availability('08:00', 'entry', 1, ('SENIOR',), False),),
            (Availability('08:25', 'monorail', 0, (), False, '품절', entry_time='08:00'),),
            monorail_error='모노레일 일부 확인 실패')
        self.assertTrue(callable(getattr(self.app, '_emit', None)))
        worker = threading.Thread(target=self.app._emit, args=({'kind':'snapshot', 'snapshot':snapshot, 'checks':7},))
        worker.start(); worker.join(2)
        log = self.logs()
        self.assertIn('08:25', log)
        self.assertIn('입장 기준=08:00', log)
        self.assertIn('모노레일 일부 확인 실패', log)
        self.assertIn('checks=7', log)
        self.app._poll()
        self.assertEqual(self.logs().count('event=MONITOR_CHECK'), 1)
        self.assertIn('조회 완료', self.app.home_log_text.get('1.0', 'end'))

    def test_start_context_is_useful_without_private_order_link(self):
        config = WatchConfig(addon_url='https://example.invalid/private-order-123?token=secret',
                             include_monorail=True, mono_relative=True)
        self.assertTrue(callable(getattr(self.app, '_emit', None)))
        self.app._emit({'kind':'started', 'config':config})
        log = self.logs()
        for value in ('2026-10-25', '08:00~08:20', '성인 2장', '경로 2장', '어린이 2장', '+20분 ±15분', 'interval=10'):
            self.assertIn(value, log)
        self.assertNotIn('private-order-123', log)
        self.assertNotIn('secret', log)

    def test_status_only_actions_are_saved_but_normal_connection_ticks_are_coalesced(self):
        self.app.status.set('목록 확보 중지 요청 TEST_STATUS')
        self.app.session_status.set('NOL 연결 정상 · 08:00:00')
        self.app.session_status.set('NOL 연결 정상 · 08:00:10')
        self.assertIn('TEST_STATUS', self.logs())
        self.assertEqual(self.logs().count('NOL 연결 정상'), 1)

    def test_close_allows_inflight_job_to_finish_logging_without_tk_access(self):
        entered = threading.Event(); release = threading.Event()
        def job():
            entered.set(); release.wait(2)
            raise RuntimeError('종료 직전 오류 LATE_FAILURE')
        self.app._submit(job, 'loaded')
        self.assertTrue(entered.wait(2))
        self.app.close()
        release.set()
        cleanup = getattr(self.app, 'cleanup_thread', None)
        self.assertIsNotNone(cleanup)
        cleanup.join(3)
        self.assertFalse(cleanup.is_alive())
        log = self.logs()
        self.assertIn('LATE_FAILURE', log)
        self.assertIn('event=APP_CLOSED', log)

    def test_tk_callback_failure_records_location_without_raw_exception_secret(self):
        def broken_button():
            raise ValueError('raw-cookie-private-value')
        self.root.after(0, broken_button)
        self.root.update()
        log = self.logs()
        self.assertIn('event=GUI_CALLBACK_FAILED', log)
        self.assertIn('ValueError', log)
        self.assertIn('broken_button', log)
        self.assertNotIn('raw-cookie-private-value', log)

    def test_slow_monitor_keeps_handler_open_until_final_event(self):
        app = self.app
        class Runner:
            running = True
            calls = []
            def stop(self): pass
            def join(self, timeout=None):
                self.calls.append(timeout)
                if timeout is None:
                    app._emit({'kind':'finished','message':'느린 조회 종료 FINAL_AFTER_WAIT','checks':9,'reason':'user_stop'})
                    self.running = False
        runner = Runner(); runner.thread = runner
        app.runner = runner
        app.close(); app.cleanup_thread.join(3)
        self.assertFalse(app.cleanup_thread.is_alive())
        self.assertEqual(runner.calls, [35, None])
        log = self.logs()
        self.assertIn('event=SHUTDOWN_WAITING', log)
        self.assertLess(log.index('FINAL_AFTER_WAIT'), log.index('event=APP_CLOSED'))


if __name__ == '__main__': unittest.main()
