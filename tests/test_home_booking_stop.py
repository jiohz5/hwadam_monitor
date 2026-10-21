"""Home browser tests can be started and cancelled without opening management."""
import threading
import time
import unittest
from unittest.mock import patch

import gui
import test_booking as booking_helpers


class HomeBookingStopTests(booking_helpers.PatchedTestCase):
    """Reuse the temporary-root GUI fixture without inheriting its test cases."""

    close_app = booking_helpers.BookingGuiTests.close_app
    capture_jobs = booking_helpers.BookingGuiTests.capture_jobs
    finish_job = booking_helpers.BookingGuiTests.finish_job

    def setUp(self):
        booking_helpers.BookingGuiTests.setUp(self)
        self.panel.toggle_admin()

    def home_button(self):
        self.assertTrue(hasattr(self.app, 'browser_test_button'),
                        'Home ticket exploration needs its own browser test button')
        return self.app.browser_test_button

    def start_home(self):
        self.home_button().invoke()

    def assert_home_ready(self):
        self.assertFalse(self.app.busy)
        self.assertFalse(self.app.booking_active)
        self.assertFalse(self.panel.admin_enabled)
        self.assertEqual(str(self.home_button().cget('state')), 'normal')
        self.assertEqual(str(self.app.start_button.cget('state')), 'normal')
        self.assertEqual(str(self.app.stop_button.cget('state')), 'disabled')
        self.assertEqual(str(self.panel.test_pay_button.cget('state')), 'disabled')
        for widget, initial in self.condition_states.items():
            self.assertEqual(str(widget.cget('state')), initial)

    def assert_cancelled(self):
        self.assertIn('중지', self.panel.test_pay_status.get())
        self.assertNotIn('실패', self.panel.test_pay_status.get())
        self.assertNotIn('테스트 완료', self.panel.test_pay_status.get())
        self.assertIn('중지', self.app.status.get())
        self.assertIn('중지', self.app.home_log_text.get('1.0', 'end'))
        logs = '\n'.join(path.read_text(encoding='utf-8')
                         for path in (self.app.base / 'logs').glob('*.log'))
        self.assertIn('event=BOOKING_TEST_CANCELLED', logs)
        self.assertNotIn('event=JOB_FAILED', logs)
        self.assert_home_ready()

    def test_home_browser_test_is_immediately_right_of_stop(self):
        button = self.home_button()
        self.assertIs(button.master, self.app.stop_button.master)
        siblings = button.master.pack_slaves()
        self.assertEqual(siblings.index(button), siblings.index(self.app.stop_button) + 1)
        self.assertEqual(button.pack_info()['side'], 'left')
        self.assertEqual(self.app.stop_button.pack_info()['side'], 'left')
        self.assertIn('테스트', button.cget('text'))

    def test_home_actions_fit_regular_and_minimum_windows(self):
        button = self.home_button()
        self.root.attributes('-alpha', 0)
        self.root.deiconify()
        for size in ('1360x920', '1160x740'):
            with self.subTest(size=size):
                self.root.geometry(size + '+0+0')
                self.root.update()
                for action in (self.app.start_button, self.app.stop_button, button):
                    self.assertTrue(action.winfo_ismapped())
                    self.assertGreaterEqual(action.winfo_width(), action.winfo_reqwidth(),
                                            'Home action labels must not be clipped')
                self.assertGreaterEqual(button.winfo_x(),
                                        self.app.stop_button.winfo_x() + self.app.stop_button.winfo_width())
                self.assertLessEqual(button.winfo_x() + button.winfo_width(),
                                     button.master.winfo_width())

    def test_home_starts_with_management_closed_and_restores_home_controls(self):
        jobs = self.capture_jobs()
        with patch.object(gui, 'run_test_booking', return_value=True):
            self.start_home()
            self.assertTrue(self.app.busy)
            self.assertTrue(self.app.booking_active)
            self.assertFalse(self.panel.admin_enabled)
            self.assertEqual(str(self.panel.test_pay_button.cget('state')), 'disabled')
            self.assertEqual(str(self.app.stop_button.cget('state')), 'normal')
            self.assertEqual(str(self.home_button().cget('state')), 'disabled')
            self.finish_job(jobs)
        self.assertIn('완료', self.panel.test_pay_status.get())
        self.assert_home_ready()

    def test_management_browser_test_still_requires_management_mode(self):
        jobs = self.capture_jobs()
        self.panel.start_test_booking()
        self.assertEqual(jobs, [])
        self.assertFalse(self.app.busy)
        self.assertFalse(self.panel.admin_enabled)
        self.assertEqual(str(self.panel.test_pay_button.cget('state')), 'disabled')

    def test_stop_before_worker_starts_skips_every_browser_action(self):
        jobs = self.capture_jobs()
        with patch.object(gui, 'run_test_booking') as booking, \
             patch.object(self.app.browser, '_select') as select:
            self.start_home()
            self.app._start_browser_test()
            self.assertEqual(len(jobs), 1, 'Repeated starts must share one active booking')
            self.app.stop_button.invoke()
            self.assertTrue(self.app.booking_stop.is_set())
            self.assertTrue(self.app.busy, 'Stop must wait for the worker to acknowledge cancellation')
            self.assertTrue(self.app.booking_active)
            for widget in self.condition_states:
                self.assertEqual(str(widget.cget('state')), 'disabled')
            self.app._buttons()
            self.assertEqual(str(self.app.stop_button.cget('state')), 'disabled')
            self.app._stop()
            self.app._start_browser_test()
            self.assertEqual(len(jobs), 1, 'A stopping booking must continue blocking a second start')
            jobs.pop()()
            event = self.app.events.get_nowait()
            self.assertEqual(event['kind'], 'test_pay_cancelled')
            self.app._handle_event(event)
            select.assert_not_called()
            booking.assert_not_called()
        self.assertEqual(self.driver.actions, [])
        self.assert_cancelled()

    def test_stop_reaches_active_worker_and_waits_for_its_acknowledgement(self):
        entered = threading.Event()
        resume = threading.Event()
        checks = []
        completed_actions = []

        def booking(driver, config, **kwargs):
            checks.append(kwargs['should_stop']())
            entered.set()
            if not resume.wait(5):
                raise RuntimeError('Test did not release the booking worker')
            checks.append(kwargs['should_stop']())
            if checks[-1]:
                raise gui.BookingCancelled('브라우저 테스트 중지')
            completed_actions.append('continued booking')
            return True

        with patch.object(gui, 'run_test_booking', side_effect=booking):
            try:
                self.start_home()
                self.assertTrue(entered.wait(3), 'The real shared executor must start the booking')
                self.app.stop_button.invoke()
                self.assertTrue(self.app.busy)
                self.assertTrue(self.app.booking_active)
                self.assertEqual(str(self.app.start_button.cget('state')), 'disabled')
                self.assertEqual(str(self.home_button().cget('state')), 'disabled')
                resume.set()
                deadline = time.monotonic() + 3
                while self.app.busy and time.monotonic() < deadline:
                    self.root.update()
                    time.sleep(.01)
            finally:
                resume.set()
        self.assertEqual(checks, [False, True])
        self.assertEqual(completed_actions, [])
        self.assert_cancelled()

    def test_restart_after_cancellation_clears_the_previous_stop_request(self):
        jobs = self.capture_jobs()
        self.start_home()
        self.app.stop_button.invoke()
        self.finish_job(jobs)
        self.assert_cancelled()
        checks = []

        def booking(driver, config, **kwargs):
            checks.append(kwargs['should_stop']())
            return True

        self.app.booking_stop.set()
        with patch.object(gui, 'run_test_booking', side_effect=booking):
            self.start_home()
            self.assertFalse(self.app.booking_stop.is_set())
            self.finish_job(jobs)
        self.assertEqual(checks, [False])
        self.assertIn('완료', self.panel.test_pay_status.get())
        self.assert_home_ready()

    def test_home_failure_releases_booking_and_restores_controls(self):
        jobs = self.capture_jobs()
        with patch.object(gui, 'run_test_booking', side_effect=gui.BookingTestError('테스트 대상 없음')):
            self.start_home()
            self.finish_job(jobs)
        self.assertIn('실패', self.panel.test_pay_status.get())
        self.assert_home_ready()

    def test_management_started_booking_uses_the_same_home_stop(self):
        self.home_button()
        self.panel.toggle_admin()
        jobs = self.capture_jobs()
        with patch.object(gui, 'run_test_booking') as booking:
            self.panel.test_pay_button.invoke()
            self.assertEqual(str(self.app.stop_button.cget('state')), 'normal')
            self.app.stop_button.invoke()
            self.finish_job(jobs)
            booking.assert_not_called()
        self.assertFalse(self.app.busy)
        self.assertFalse(self.app.booking_active)
        self.assertTrue(self.panel.admin_enabled)
        self.assertEqual(str(self.panel.test_pay_button.cget('state')), 'normal')
        self.assertIn('중지', self.panel.test_pay_status.get())

    def test_home_stop_also_ends_an_active_local_management_preview(self):
        self.panel.toggle_admin()
        self.panel.run_button.invoke()
        self.assertTrue(self.panel.rehearsing)
        self.assertEqual(str(self.app.stop_button.cget('state')), 'normal')
        self.app.stop_button.invoke()
        self.assertFalse(self.panel.rehearsing)
        self.assertEqual(str(self.app.stop_button.cget('state')), 'disabled')
        self.assertEqual(str(self.panel.run_button.cget('state')), 'normal')

    def test_existing_monitor_stop_still_signals_the_monitor(self):
        stop_event = self.app.runner.stop_event
        thread = threading.Thread(target=lambda: stop_event.wait(5), daemon=True)
        self.app.runner.thread = thread
        thread.start()
        try:
            self.app._buttons()
            self.assertEqual(str(self.app.stop_button.cget('state')), 'normal')
            self.app.stop_button.invoke()
            self.assertTrue(stop_event.is_set())
            self.assertEqual(str(self.app.stop_button.cget('state')), 'disabled')
        finally:
            stop_event.set()
            thread.join(timeout=2)
        self.app._handle_event({'kind': 'finished', 'message': '감시 종료'})
        self.assertFalse(self.app.runner.running)
        self.assertEqual(str(self.app.start_button.cget('state')), 'normal')

    def test_queued_monitor_booking_attempt_is_ignored_after_stop(self):
        stop_event = self.app.runner.stop_event
        thread = threading.Thread(target=lambda: stop_event.wait(5), daemon=True)
        self.app.runner.thread = thread
        thread.start()
        try:
            self.app._buttons()
            self.app.stop_button.invoke()
            self.assertTrue(stop_event.is_set())
            thread.join(timeout=2)
            self.assertFalse(self.app.runner.running)
            with patch.object(self.app, '_start_auto_booking', create=True) as start_booking:
                self.app._handle_event({'kind': 'booking_attempt',
                                        'message': '가용 회차 발견 · 대기 중이던 예약 요청',
                                        'snapshot': object()})
                start_booking.assert_not_called()
        finally:
            stop_event.set()
            thread.join(timeout=2)
        self.assertEqual(self.driver.actions, [])


class BookingCancellationFlowTests(unittest.TestCase):
    def test_cancellation_has_its_own_error_type_and_prevents_initial_browser_action(self):
        self.assertTrue(hasattr(gui, 'BookingCancelled'),
                        'User cancellation must be distinguishable from a failed booking')
        self.assertTrue(issubclass(gui.BookingCancelled, gui.BookingTestError))
        driver = booking_helpers.BookingDriver()
        with self.assertRaises(gui.BookingCancelled):
            gui.run_test_booking(driver, gui.WatchConfig(), should_stop=lambda: True)
        self.assertEqual(driver.actions, [])

    def test_cancellation_after_purchase_navigation_prevents_payment_controls(self):
        self.assertTrue(hasattr(gui, 'BookingCancelled'),
                        'User cancellation must be distinguishable from a failed booking')
        driver = booking_helpers.BookingDriver()
        stop = threading.Event()
        driver.buy.on_click = stop.set
        config = gui.WatchConfig(time_specs=('08:00',), counts={'SENIOR': 1})
        with patch.object(gui, 'WebDriverWait', booking_helpers.ImmediateWait), \
             patch('time.sleep'), self.assertRaises(gui.BookingCancelled):
            gui.run_test_booking(driver, config, age_label='경로',
                                 should_stop=stop.is_set, log=lambda message: None)
        self.assertEqual(driver.buy.clicks, 1)
        self.assertEqual(driver.money.clicks, 0)
        self.assertEqual(driver.agree.clicks, 0)
        self.assertEqual(driver.pay.clicks, 0)


if __name__ == '__main__':
    unittest.main()
