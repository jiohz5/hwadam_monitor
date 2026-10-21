"""Offline automatic-booking lifecycle through the real GUI event dispatcher."""
import threading
import unittest
from unittest.mock import patch

import gui
import test_booking as booking_helpers


class AutoBookingLifecycleTests(booking_helpers.PatchedTestCase):
    """Reuse only fixture helpers, without inheriting unrelated GUI tests."""

    close_app = booking_helpers.BookingGuiTests.close_app
    capture_jobs = booking_helpers.BookingGuiTests.capture_jobs
    finish_job = booking_helpers.BookingGuiTests.finish_job

    def setUp(self):
        booking_helpers.BookingGuiTests.setUp(self)
        self.panel.toggle_admin()
        self.booking = self.keep_patch(patch.object(
            gui, 'run_test_booking', return_value=gui.BookingResult('confirmed')))
        self.jobs = self.capture_jobs()
        self.snapshot = gui.Snapshot(self.app.read_config(), (
            gui.Availability('08:00', 'entry', 4, ('SENIOR',), True),))
        self.start_monitor()

    def start_monitor(self):
        stop = threading.Event()
        thread = threading.Thread(target=stop.wait, daemon=True)
        self.app.runner.stop_event = stop
        self.app.runner.thread = thread
        thread.start()

        def cleanup_monitor():
            stop.set()
            thread.join(timeout=2)

        self.addCleanup(cleanup_monitor)
        self.app._lock_conditions(True)
        self.app._buttons()
        return stop

    def finish_monitor(self):
        stop = self.app.runner.stop_event
        stop.set()
        self.app.runner.thread.join(timeout=2)
        self.assertFalse(self.app.runner.running)
        self.app._handle_event({'kind': 'finished', 'message': '감시 종료',
                                'monitor_stop': stop})

    def request_booking(self, *, monitor_stop=None):
        event = {'kind': 'booking_attempt', 'message': '가용 회차 발견 · 예약 시도',
                 'snapshot': self.snapshot,
                 'monitor_stop': monitor_stop or self.app.runner.stop_event}
        try:
            self.app._handle_event(event)
        except AttributeError as error:
            self.fail('Automatic booking must dispatch to the shared GUI worker: ' + str(error))

    def assert_conditions_locked(self):
        for widget in self.condition_states:
            self.assertEqual(str(widget.cget('state')), 'disabled')
        self.assertEqual(str(self.app.start_button.cget('state')), 'disabled')
        self.assertEqual(str(self.app.browser_test_button.cget('state')), 'disabled')

    def assert_fully_idle(self):
        self.assertFalse(self.app.busy)
        self.assertFalse(self.app.booking_active)
        self.assertFalse(self.app.test_booking_hold)
        self.assertFalse(self.app.runner.running)
        for widget, initial in self.condition_states.items():
            self.assertEqual(str(widget.cget('state')), initial)
        self.assertEqual(str(self.app.start_button.cget('state')), 'normal')
        self.assertEqual(str(self.app.browser_test_button.cget('state')), 'normal')
        self.assertEqual(str(self.app.stop_button.cget('state')), 'disabled')

    def test_active_monitor_booking_attempt_starts_one_stoppable_shared_worker(self):
        self.request_booking()
        self.request_booking()
        self.assertEqual(len(self.jobs), 1)
        self.assertTrue(self.app.busy)
        self.assertTrue(self.app.booking_active)
        self.assertTrue(self.app.test_booking_hold)
        self.assertFalse(self.app.booking_stop.is_set())
        self.assertTrue(self.app.runner.running)
        self.assertFalse(self.panel.admin_enabled)
        self.assertEqual(str(self.app.stop_button.cget('state')), 'normal')
        self.assert_conditions_locked()

    def test_stop_before_queued_worker_skips_browser_and_unlocks_after_acknowledgement(self):
        self.request_booking()
        self.app.stop_button.invoke()
        self.assertTrue(self.app.booking_stop.is_set())
        self.assertTrue(self.app.runner.stop_event.is_set())
        self.assertTrue(self.app.busy)
        self.assertTrue(self.app.booking_active)
        self.assert_conditions_locked()
        self.finish_monitor()
        self.assert_conditions_locked()
        self.finish_job(self.jobs)
        self.booking.assert_not_called()
        self.app.browser._select.assert_not_called()
        self.assertEqual(self.driver.actions, [])
        self.assertIn('중지', self.panel.test_pay_status.get())
        self.assert_fully_idle()

    def test_booking_outcomes_keep_conditions_locked_until_monitor_also_finishes(self):
        outcomes = ((None, '완료'), (gui.BookingCancelled('예약 중지'), '중지'),
                    (RuntimeError('offline booking failure'), '실패'))
        for error, expected_status in outcomes:
            with self.subTest(outcome=expected_status):
                self.booking.side_effect = error
                self.request_booking()
                self.finish_job(self.jobs)
                self.assertFalse(self.app.busy)
                self.assertFalse(self.app.booking_active)
                self.assertFalse(self.app.test_booking_hold)
                self.assertTrue(self.app.runner.running)
                self.assertFalse(self.app.runner.stop_event.is_set())
                self.assertIn(expected_status, self.panel.test_pay_status.get())
                self.assertEqual(str(self.app.stop_button.cget('state')), 'normal')
                self.assert_conditions_locked()
        self.assertEqual(self.booking.call_count, 3)
        for call in self.booking.call_args_list:
            self.assertIs(call.args[0], self.driver)
            self.assertEqual(call.args[1], self.snapshot.config)
            self.assertIs(call.kwargs['stop_before_pay'], False)
            self.assertEqual(call.kwargs['age_label'], '경로')
            self.assertTrue(callable(call.kwargs['should_stop']))
        self.assertFalse(any(action[0] == 'navigate' for action in self.driver.actions),
                         'Worker completion must preserve the current browser page')
        self.finish_monitor()
        self.assert_fully_idle()

    def test_monitor_finished_during_booking_keeps_conditions_locked(self):
        self.request_booking()
        self.finish_monitor()
        self.assertTrue(self.app.busy)
        self.assertTrue(self.app.booking_active)
        self.assertTrue(self.app.test_booking_hold)
        self.assert_conditions_locked()
        self.assertEqual(str(self.app.stop_button.cget('state')), 'normal')
        self.finish_job(self.jobs)
        self.assert_fully_idle()

    def test_old_monitor_booking_attempt_cannot_start_booking_in_new_run(self):
        old_stop = self.app.runner.stop_event
        self.finish_monitor()
        current_stop = self.start_monitor()
        self.assertIsNot(old_stop, current_stop)
        self.request_booking(monitor_stop=old_stop)
        self.assertEqual(self.jobs, [])
        self.assertFalse(self.app.busy)
        self.assertFalse(self.app.booking_active)
        self.assertEqual(self.driver.actions, [])
        self.request_booking(monitor_stop=current_stop)
        self.assertEqual(len(self.jobs), 1, 'The current monitor run must still be able to book')


class MonitorBookingEventIdentityTests(unittest.TestCase):
    def test_booking_attempt_and_finished_identify_the_emitting_monitor_run(self):
        stop = threading.Event()
        config = gui.WatchConfig(time_specs=('08:00',), counts={'SENIOR': 1})
        snapshot = gui.Snapshot(config, (
            gui.Availability('08:00', 'entry', 4, ('SENIOR',), True),))
        events = []

        class Client:
            def check(self, selected_config, cancellation):
                return snapshot

        class Notifier:
            def send(self, message):
                pass

        def receive(event):
            events.append(event)
            if event['kind'] == 'booking_attempt':
                stop.set()

        gui.MonitorRunner(Client(), receive)._run(config, Notifier(), stop)
        for kind in ('booking_attempt', 'finished'):
            with self.subTest(kind=kind):
                matching = [event for event in events if event['kind'] == kind]
                self.assertEqual(len(matching), 1)
                self.assertIs(matching[0].get('monitor_stop'), stop)


if __name__ == '__main__':
    unittest.main()
