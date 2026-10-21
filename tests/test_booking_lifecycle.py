"""Offline regressions for GUI recovery, monitor navigation, and thread shutdown."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import gui
import test_booking as booking_helpers


class BookingLifecycleTests(booking_helpers.PatchedTestCase):
    close_app = booking_helpers.BookingGuiTests.close_app
    capture_jobs = booking_helpers.BookingGuiTests.capture_jobs

    def setUp(self):
        self.keep_patch(patch.object(tempfile, 'tempdir', str(Path(__file__).resolve().parents[1])))
        booking_helpers.BookingGuiTests.setUp(self)
        self.panel.toggle_admin()

    def assert_explore_page(self):
        self.assertEqual(self.app.pages['explore'].winfo_manager(), 'grid')
        for name, page in self.app.pages.items():
            if name != 'explore':
                self.assertEqual(page.winfo_manager(), '', f'{name} must be hidden after starting monitoring')

    def test_bad_event_does_not_strand_cancellation_or_future_gui_ticks(self):
        jobs = self.capture_jobs()
        self.app._start_browser_test()
        self.app._stop()
        self.app.events.put({'kind': 'poison_for_regression'})
        jobs.pop()()
        handle_event = self.app._handle_event

        def fail_once(event):
            if event['kind'] == 'poison_for_regression':
                raise AttributeError('Offline broken GUI event handler')
            return handle_event(event)

        with patch.object(self.app, '_handle_event', side_effect=fail_once):
            try:
                self.app._poll()
            except Exception as error:
                self.fail(f'A single broken event must not terminate GUI polling: {type(error).__name__}')
        self.assertFalse(self.app.busy)
        self.assertFalse(self.app.booking_active)
        self.assertFalse(self.app.test_booking_hold)
        self.assertEqual(str(self.app.start_button.cget('state')), 'normal')
        self.assertEqual(str(self.app.browser_test_button.cget('state')), 'normal')
        self.assertEqual(str(self.app.stop_button.cget('state')), 'disabled')
        self.assertIn('중지', self.panel.test_pay_status.get())
        for widget, initial in self.condition_states.items():
            self.assertEqual(str(widget.cget('state')), initial)
        logs = '\n'.join(path.read_text(encoding='utf-8')
                         for path in (self.app.base / 'logs').glob('*.log'))
        self.assertIn('GUI_CALLBACK_FAILED', logs)
        self.assertIn('AttributeError', logs)

        marker = 'offline-poll-recovery-marker'
        self.app.events.put({'kind': 'log', 'message': marker})
        deadline = time.monotonic() + 1
        while marker not in self.app.home_log_text.get('1.0', 'end') and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.assertIn(marker, self.app.home_log_text.get('1.0', 'end'),
                      'The next scheduled tick must still consume queued events')
        self.assertEqual(self.driver.actions, [])

    def test_start_from_settings_or_admin_shows_actual_explore_page(self):
        with patch.object(gui.TelegramNotifier, 'validate', autospec=True, side_effect=lambda notifier: notifier), \
             patch.object(gui.TelegramNotifier, 'send'), \
             patch.object(self.app.runner, 'start') as start:
            for page in ('settings', 'admin'):
                with self.subTest(page=page):
                    self.app._show_page(page)
                    self.assertEqual(self.app.pages[page].winfo_manager(), 'grid')
                    self.app._start()
                    self.assert_explore_page()
                    self.app._lock_conditions(False)
            self.assertEqual(start.call_count, 2)

    def test_invalid_start_from_settings_or_admin_shows_conditions_to_fix(self):
        with patch.object(gui.TelegramNotifier, 'validate', autospec=True, side_effect=lambda notifier: notifier), \
             patch.object(gui.TelegramNotifier, 'send'), \
             patch.object(self.app.runner, 'start') as start:
            for page in ('settings', 'admin'):
                with self.subTest(page=page):
                    self.app._show_page(page)
                    self.app.vars['times'].set('invalid-time')
                    self.app._start()
                    self.assert_explore_page()
            start.assert_not_called()
            self.app._problem.assert_called()

    def test_finished_event_before_thread_exit_restores_buttons_on_later_poll(self):
        release = threading.Event()
        thread = threading.Thread(target=release.wait, daemon=True, name='offline-monitor-exit')
        self.app.runner.thread = thread
        thread.start()
        try:
            self.app._lock_conditions(True)
            self.app._buttons()
            self.app.events.put({'kind': 'finished', 'message': '감시 종료'})
            self.app._poll()
            self.assertTrue(self.app.runner.running)
            self.assertEqual(str(self.app.start_button.cget('state')), 'disabled')
            self.assertEqual(str(self.app.browser_test_button.cget('state')), 'disabled')

            release.set()
            thread.join(timeout=2)
            self.assertFalse(self.app.runner.running)
            self.assertTrue(self.app.events.empty())
            self.app._poll()
            self.assertEqual(str(self.app.start_button.cget('state')), 'normal')
            self.assertEqual(str(self.app.browser_test_button.cget('state')), 'normal')
            self.assertEqual(str(self.app.stop_button.cget('state')), 'disabled')
            for widget, initial in self.condition_states.items():
                self.assertEqual(str(widget.cget('state')), initial)
        finally:
            release.set()
            thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
