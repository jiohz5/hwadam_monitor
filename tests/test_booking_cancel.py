"""Offline cancellation checks for waits in the real browser booking flow."""
import threading
import unittest
from unittest.mock import patch

from selenium.common.exceptions import NoSuchElementException, TimeoutException

import gui
from test_booking import BookingDriver, PatchedTestCase
from test_booking_selection import SelectionClock


class WaitingBookingDriver(BookingDriver):
    """Keep one checkout control pending so cancellation occurs during its wait."""

    def __init__(self, pending):
        super().__init__()
        self.pending = pending
        self.pending_seen = False
        self.stop_requested = threading.Event()
        self.actions_at_stop = None
        if pending == 'terms_selected':
            self.agree.on_click = lambda: setattr(self.agree, 'selected', False)

    def find_elements(self, by, selector):
        elements = super().find_elements(by, selector)
        pending = (
            (self.pending == 'buy' and '구매하기' in selector)
            or (self.pending == 'money' and 'NOL 머니' in selector)
            or (self.pending == 'terms' and 'checkbox' in selector)
            or (self.pending == 'payment' and '결제하기' in selector)
            or (self.pending == 'keypad' and '#mtk_pinCodeInput' in selector)
        )
        if pending:
            self.pending_seen = True
            return []
        if self.pending == 'terms_selected' and self.agree.clicks and 'checkbox' in selector:
            self.pending_seen = True
        return elements

    def request_stop(self):
        self.actions_at_stop = list(self.actions)
        self.stop_requested.set()


class CancelBetweenPollsWait:
    """Request Stop between polls, when a Selenium wait normally sleeps."""

    def __init__(self, driver, timeout, *args, **kwargs):
        self.driver = driver

    def until(self, predicate):
        for _ in range(2):
            try:
                value = predicate(self.driver)
            except NoSuchElementException:
                value = False
            if value:
                return value
            if self.driver.pending_seen and not self.driver.stop_requested.is_set():
                self.driver.request_stop()
        raise TimeoutException('Offline pending control did not become ready')


class BookingCancellationTests(PatchedTestCase):
    def setUp(self):
        self.clock = SelectionClock()
        self.keep_patch(patch.object(gui, '_select_booking_options'))
        self.keep_patch(patch.object(gui, 'WebDriverWait', CancelBetweenPollsWait))
        self.keep_patch(patch.object(gui.time, 'monotonic', self.clock))
        self.keep_patch(patch.object(gui.time, 'sleep', self.clock.sleep))
        self.settings_read = self.keep_patch(patch.object(
            gui.Path, 'read_text', side_effect=AssertionError('Cancellation must precede settings access')))

    def run_booking(self, driver):
        return gui.run_test_booking(
            driver, gui.WatchConfig(time_specs=('08:00',), counts={'SENIOR': 1}),
            age_label='경로', log=lambda message: None,
            should_stop=driver.stop_requested.is_set)

    def assert_wait_cancellation(self, pending, expected_clicks):
        driver = WaitingBookingDriver(pending)
        with self.assertRaises(gui.BookingTestError) as caught:
            self.run_booking(driver)

        self.assertTrue(driver.stop_requested.is_set(), 'The requested checkout wait must be reached')
        self.assertEqual(driver.actions, driver.actions_at_stop,
                         'Stop must interrupt the next wait poll before another browser operation')
        self.assertEqual(tuple(control.clicks for control in
                               (driver.buy, driver.money, driver.agree, driver.pay)), expected_clicks)
        self.assertIsInstance(caught.exception, getattr(gui, 'BookingCancelled', ()),
                              'Cancellation must remain distinguishable from booking failures')
        self.settings_read.assert_not_called()

    def test_stop_while_waiting_for_buy_button_prevents_another_browser_poll(self):
        self.assert_wait_cancellation('buy', (0, 0, 0, 0))

    def test_stop_while_waiting_for_nol_money_prevents_retry_or_click(self):
        self.assert_wait_cancellation('money', (1, 0, 0, 0))

    def test_stop_while_waiting_for_terms_checkbox_prevents_payment(self):
        self.assert_wait_cancellation('terms', (1, 1, 0, 0))

    def test_stop_while_waiting_for_terms_selection_prevents_payment(self):
        self.assert_wait_cancellation('terms_selected', (1, 1, 1, 0))

    def test_stop_while_waiting_for_payment_button_prevents_click(self):
        self.assert_wait_cancellation('payment', (1, 1, 1, 0))

    def test_stop_while_waiting_for_keypad_prevents_settings_access(self):
        self.assert_wait_cancellation('keypad', (1, 1, 1, 1))

    def test_stop_requested_before_start_is_a_cancellation_without_browser_actions(self):
        driver = WaitingBookingDriver('buy')
        driver.request_stop()
        with self.assertRaises(gui.BookingTestError) as caught:
            self.run_booking(driver)
        self.assertEqual(driver.actions, [])
        self.assertIsInstance(caught.exception, getattr(gui, 'BookingCancelled', ()))
        self.settings_read.assert_not_called()

    def test_stop_during_payment_window_switch_is_not_swallowed_or_retried(self):
        driver = WaitingBookingDriver(None)
        switch_window = driver.switch_to.window

        def request_stop_on_switch(handle):
            driver.actions.append(('switch', handle))
            switch_window(handle)
            if not driver.stop_requested.is_set():
                driver.request_stop()

        with patch.object(driver.switch_to, 'window', side_effect=request_stop_on_switch), \
             patch.object(gui.time, 'time', self.clock):
            with self.assertRaises(gui.BookingTestError) as caught:
                self.run_booking(driver)

        self.assertTrue(driver.stop_requested.is_set())
        self.assertEqual(driver.current_window_handle, 'payment-tab')
        self.assertEqual(driver.actions, driver.actions_at_stop,
                         'Cancellation from the window-switch block must escape its retry handler')
        self.assertEqual(driver.pay.clicks, 1)
        self.assertIsInstance(caught.exception, getattr(gui, 'BookingCancelled', ()))
        self.settings_read.assert_not_called()

    def test_stop_during_after_buy_pause_does_not_wait_for_the_whole_pause(self):
        driver = WaitingBookingDriver(None)
        sleep_intervals = []

        def request_stop_during_sleep(seconds):
            sleep_intervals.append(seconds)
            self.clock.sleep(seconds)
            driver.request_stop()

        with patch.object(gui.time, 'sleep', side_effect=request_stop_during_sleep):
            with self.assertRaises(gui.BookingTestError) as caught:
                self.run_booking(driver)

        self.assertEqual(driver.buy.clicks, 1)
        self.assertEqual(driver.actions, driver.actions_at_stop)
        self.assertLessEqual(sum(sleep_intervals), .1,
                             'A stop during the pause must be checked within 100 ms')
        self.assertEqual((driver.money.clicks, driver.agree.clicks, driver.pay.clicks), (0, 0, 0))
        self.assertIsInstance(caught.exception, getattr(gui, 'BookingCancelled', ()))
        self.settings_read.assert_not_called()


if __name__ == '__main__':
    unittest.main()
