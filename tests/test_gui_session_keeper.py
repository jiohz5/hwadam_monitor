from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest

from gui import App
from test_browser import LoginDriver


class GuiSessionKeeperTests(unittest.TestCase):
    def _require_keeper(self, app):
        self.assertTrue(callable(getattr(app, '_keep_session_alive', None)),
                        '감시 중지 상태에서도 실행되는 로그인 유지 작업이 필요합니다')

    def _finish_job(self, root, app):
        deadline = time.monotonic() + 3
        while app.session_pending and time.monotonic() < deadline:
            root.update()
            time.sleep(.01)
        self.assertFalse(app.session_pending, '로그인 유지 결과가 GUI에 전달되어야 합니다')

    def test_stopped_monitor_renews_session_without_valid_watch_conditions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = tk.Tk(); root.withdraw(); app = App(root, Path(folder), auto_open_browser=False); root.update()
            try:
                self._require_keeper(app)
                app.browser.driver = LoginDriver(logged=True)
                app.browser.tab = 'own-tab'
                app.vars['date'].set('날짜 미선택')
                app.vars['times'].set('')
                app._keep_session_alive()
                self.assertFalse(app.busy, '백그라운드 로그인 유지가 탐색 화면을 잠그면 안 됩니다')
                self._finish_job(root, app)
                self.assertTrue(app.browser.cookie_file.is_file())
                self.assertGreater(app.browser._session_expiry(), time.time() + 10000)
                self.assertFalse(app.runner.running)
                self.assertEqual(app.vars['date'].get(), '날짜 미선택')
                self.assertIn('로그인 유지', app.session_status.get())
                self.assertIn('KEEPALIVE', app.home_log_text.get('1.0', 'end'))
            finally:
                app.close()

    def test_auth_failure_reaches_home_without_starting_monitor_or_replacing_saved_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = tk.Tk(); root.withdraw(); app = App(root, Path(folder), auto_open_browser=False); root.update()
            try:
                self._require_keeper(app)
                app.browser.driver = LoginDriver(logged=False)
                app.browser.tab = 'own-tab'
                app.browser.cookie_file.write_text('[]', encoding='utf-8')
                app._keep_session_alive()
                self._finish_job(root, app)
                self.assertEqual(app.browser.cookie_file.read_text(encoding='utf-8'), '[]')
                self.assertIn('다시 로그인', app.session_status.get())
                self.assertIn('로그인 유지 실패', app.home_log_text.get('1.0', 'end'))
                self.assertFalse(app.runner.running)
                self.assertFalse(app.busy)
            finally:
                app.close()

    def test_foreground_login_work_defers_background_navigation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = tk.Tk(); root.withdraw(); app = App(root, Path(folder), auto_open_browser=False); root.update()
            try:
                self._require_keeper(app)
                driver = LoginDriver(logged=True)
                app.browser.driver = driver; app.browser.tab = 'own-tab'
                app.busy = True
                app._keep_session_alive()
                root.update()
                self.assertFalse(app.session_pending)
                self.assertEqual(driver.visited, [])
                self.assertFalse(app.browser.cookie_file.exists())
                self.assertTrue(app.keepalive_id)
            finally:
                app.close()

    def test_close_cancels_idle_session_timer(self):
        with tempfile.TemporaryDirectory() as folder:
            root = tk.Tk(); root.withdraw(); app = App(root, Path(folder), auto_open_browser=False); root.update()
            try:
                self._require_keeper(app)
                timer = app.keepalive_id
                self.assertIn(timer, root.tk.call('after', 'info'))
                app.close()
                self.assertNotIn(timer, root.tk.call('after', 'info'))
            finally:
                if not app.closed:
                    app.close()


if __name__ == '__main__':
    unittest.main()
