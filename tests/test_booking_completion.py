"""Offline completion checks: closed payment windows must not hide confirmed orders."""
from pathlib import Path
import logging
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

from selenium.common.exceptions import (InvalidSessionIdException, NoSuchElementException,
                                        NoSuchWindowException, StaleElementReferenceException,
                                        WebDriverException)
from selenium.webdriver.common.by import By

import gui
import test_booking as booking_fixtures
from test_booking_selection import SelectionClock


COMPLETE_URL = 'https://platform-site.yanolja.com/domestic/order/complete/offline-order'
PENDING_URL = 'https://platform-site.yanolja.com/domestic/order/payment'
SUCCESS_HEADING = '예약이 완료되었어요'


class CompletionHeading:
    def __init__(self, text=SUCCESS_HEADING, *, visible=True, stale_reads=0):
        self.value = text
        self.visible = visible
        self.stale_reads = stale_reads

    @property
    def text(self):
        if self.stale_reads:
            self.stale_reads -= 1
            raise StaleElementReferenceException('Offline heading was replaced during navigation')
        return self.value

    def is_displayed(self):
        return self.visible


class CompletionSwitch:
    def __init__(self, driver):
        self.driver = driver

    def window(self, handle):
        self.driver.visited.append(handle)
        if handle not in self.driver.pages:
            raise NoSuchWindowException('Offline window already closed')
        self.driver.current = handle


class CompletionDriver:
    """Only browser reads and switching are supported; checkout actions do not exist."""
    def __init__(self, pages=None, *, current='payment-tab'):
        self.pages = pages or {}
        self.current = current
        self.switch_to = CompletionSwitch(self)
        self.visited = []
        self.reads = []
        self.url_errors = []
        self.session_error = None

    @property
    def window_handles(self):
        if self.session_error:
            raise self.session_error
        return list(self.pages)

    @property
    def current_window_handle(self):
        self._page()
        return self.current

    def _page(self):
        if self.session_error:
            raise self.session_error
        if self.current not in self.pages:
            raise NoSuchWindowException('Offline current payment window has closed')
        return self.pages[self.current]

    @property
    def current_url(self):
        self.reads.append(self.current)
        if self.url_errors:
            raise self.url_errors.pop(0)
        return self._page()[0]

    def find_elements(self, by, selector):
        self.reads.append(self.current)
        headings = self._page()[1]
        if (by == By.TAG_NAME and selector == 'h1') or (
                by in (By.CSS_SELECTOR, By.XPATH) and 'h1' in selector):
            return headings
        return []

    def find_element(self, by, selector):
        elements = self.find_elements(by, selector)
        if not elements:
            raise NoSuchElementException('Offline selector has no matching heading')
        return elements[0]


class BookingCompletionTests(unittest.TestCase):
    def setUp(self):
        self.clock = SelectionClock()
        self.messages = []
        for patcher in (patch.object(gui.time, 'monotonic', self.clock),
                        patch.object(gui.time, 'time', self.clock),
                        patch.object(gui.time, 'sleep', self.clock.sleep),
                        patch.object(gui.Path, 'read_text', side_effect=AssertionError(
                            'Completion must never read settings or a payment PIN'))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def wait(self, driver, **kwargs):
        helper = getattr(gui, '_wait_booking_confirmation', None)
        self.assertTrue(callable(helper), 'Completion must recover from a closed payment window')
        return helper(driver, 'product-tab', 'payment-tab', step=self.messages.append,
                      timeout=.6, **kwargs)

    def test_closed_current_payment_window_recovers_confirmed_original_tab(self):
        driver = CompletionDriver({'product-tab': (COMPLETE_URL, [CompletionHeading()])})
        self.assertIs(self.wait(driver), True)
        self.assertEqual(driver.current_window_handle, 'product-tab')

    def test_still_open_payment_window_does_not_hide_original_tab_success(self):
        driver = CompletionDriver({'product-tab': (COMPLETE_URL, [CompletionHeading()]),
                                   'payment-tab': ('https://nol-money.example.invalid/pay', [])})
        self.assertIs(self.wait(driver), True)
        self.assertEqual(driver.current_window_handle, 'product-tab')

    def test_original_tab_can_redirect_after_payment_window_closes(self):
        driver = CompletionDriver({'product-tab': (PENDING_URL, [])})

        def redirect_after_poll(seconds):
            self.clock.sleep(seconds)
            driver.pages['product-tab'] = (COMPLETE_URL, [CompletionHeading()])

        with patch.object(gui.time, 'sleep', side_effect=redirect_after_poll):
            self.assertIs(self.wait(driver), True)

    def test_captured_payment_window_can_show_confirmation(self):
        driver = CompletionDriver({'product-tab': (PENDING_URL, []),
                                   'payment-tab': (COMPLETE_URL, [CompletionHeading()])})
        self.assertIs(self.wait(driver), True)

    def test_all_windows_closed_is_unconfirmed_instead_of_booking_failure(self):
        self.assertIs(self.wait(CompletionDriver()), False)

    def test_terminated_browser_session_is_unconfirmed_instead_of_booking_failure(self):
        driver = CompletionDriver()
        driver.session_error = InvalidSessionIdException('Offline browser was closed')
        self.assertIs(self.wait(driver), False)

    def test_unrelated_old_completed_order_is_never_used_as_current_success(self):
        driver = CompletionDriver({'product-tab': (PENDING_URL, []),
                                   'old-order-tab': (COMPLETE_URL, [CompletionHeading()])})
        self.assertIs(self.wait(driver), False)
        self.assertNotIn('old-order-tab', driver.visited)
        self.assertNotIn('old-order-tab', driver.reads)

    def test_completion_url_without_visible_success_heading_is_unconfirmed(self):
        for headings in ([], [CompletionHeading(visible=False)]):
            with self.subTest(headings=headings):
                driver = CompletionDriver({'product-tab': (COMPLETE_URL, headings)})
                self.assertIs(self.wait(driver), False)

    def test_success_heading_requires_the_trusted_complete_url(self):
        for url in (PENDING_URL,
                    'https://platform-site.yanolja.com.example.invalid/domestic/order/complete/123',
                    'http://platform-site.yanolja.com/domestic/order/complete/123',
                    'https://platform-site.yanolja.com/domestic/order/complete/'):
            with self.subTest(url=url):
                driver = CompletionDriver({'product-tab': (url, [CompletionHeading()])})
                self.assertIs(self.wait(driver), False)

    def test_negative_or_extra_heading_text_cannot_confirm_an_order(self):
        for text in ('예약이 완료되지 않았어요', '예약이 완료되었어요 취소됨', '예약이 완료'):
            with self.subTest(text=text):
                driver = CompletionDriver({'product-tab': (COMPLETE_URL, [CompletionHeading(text)])})
                self.assertIs(self.wait(driver), False)

    def test_stale_heading_during_redirect_is_retried(self):
        driver = CompletionDriver({'product-tab': (COMPLETE_URL, [CompletionHeading(stale_reads=1)])})
        self.assertIs(self.wait(driver), True)

    def test_transient_no_such_window_during_navigation_is_retried(self):
        driver = CompletionDriver({'product-tab': (COMPLETE_URL, [CompletionHeading()])})
        driver.url_errors.append(NoSuchWindowException('Offline transient navigation race'))
        self.assertIs(self.wait(driver), True)

    def test_driver_failure_after_payment_is_unconfirmed_without_leaking_raw_error(self):
        driver = CompletionDriver({'product-tab': (COMPLETE_URL, [CompletionHeading()])})
        driver.session_error = WebDriverException('private-order-url-and-session-marker')
        self.assertIs(self.wait(driver), False)
        self.assertNotIn('private-order-url-and-session-marker', '\n'.join(self.messages))

    def test_checkpoint_cancellation_escapes_before_browser_reads(self):
        driver = CompletionDriver({'product-tab': (COMPLETE_URL, [CompletionHeading()])})

        def cancelled():
            raise gui.BookingCancelled('Offline stop request')

        with self.assertRaises(gui.BookingCancelled):
            self.wait(driver, checkpoint=cancelled)
        self.assertEqual(driver.visited, [])
        self.assertEqual(driver.reads, [])


class BookingCompletionReportingTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = gui.App(self.root, Path(self.folder.name), auto_open_browser=False)
        self.addCleanup(self.close_app)
        self.root.update()

    def close_app(self):
        if not self.app.closed:
            self.app.close()
        cleanup = getattr(self.app, 'cleanup_thread', None)
        if cleanup:
            cleanup.join(3)

    def logs(self):
        return '\n'.join(path.read_text(encoding='utf-8')
                         for path in (Path(self.folder.name) / 'logs').glob('*.log'))

    def assert_reported(self, value, expected, forbidden, code, level=logging.INFO):
        from eventlog import event_records

        self.assertEqual(event_records({'kind': 'test_pay_done', 'value': value}),
                         [(level, code, expected)])
        self.app.booking_active = True
        self.app.test_booking_hold = True
        jobs = []
        with patch.object(self.app, '_background', side_effect=jobs.append):
            self.app._submit(lambda: value, 'test_pay_done')
        jobs.pop()()
        self.assertIn(expected, self.logs(), 'Worker must persist the real result before GUI polling')
        self.assertIn('event=' + code, self.logs())
        self.assertNotIn(forbidden, self.logs())
        self.app._poll()
        self.assertEqual(self.app.status.get(), expected)
        self.assertEqual(self.app.operations.test_pay_status.get(), expected)
        self.assertIn(expected, self.app.home_log_text.get('1.0', 'end'))
        self.assertFalse(self.app.busy)
        self.assertFalse(self.app.booking_active)
        self.assertFalse(self.app.test_booking_hold)

    def result(self, status):
        result_type = getattr(gui, 'BookingResult', None)
        self.assertTrue(callable(result_type), 'Booking outcomes must reach the GUI and persisted log')
        return result_type(status=status)

    def test_confirmed_result_reaches_gui_and_persisted_worker_log(self):
        self.assert_reported(self.result('confirmed'), '예약 성공 · 예약 완료 확인', '결제 결과 확인 필요',
                             'BOOKING_CONFIRMED')

    def test_unconfirmed_result_is_neither_success_nor_booking_failure(self):
        self.assert_reported(self.result('unconfirmed'),
                             '결제 결과 확인 필요 · 예약 완료 화면을 확인하지 못했습니다', '예약 성공',
                             'BOOKING_RESULT_UNCONFIRMED', logging.WARNING)
        self.assertNotIn('테스트 실패', self.app.status.get())

    def test_legacy_readiness_result_keeps_generic_completion_without_success_claim(self):
        self.assert_reported(True, '브라우저 테스트 완료 · 결과는 브라우저에서 확인하세요', '예약 성공',
                             'BOOKING_TEST_DONE')


class FinalConfirmationDriver(booking_fixtures.BookingDriver):
    """The entire synthetic checkout closes its payment tab on the final click."""
    def __init__(self, *, completed):
        super().__init__()
        self.completed = completed
        self.keypad = booking_fixtures.BookingElement('Offline keypad')
        self.digit = booking_fixtures.BookingElement('0', attributes={'aria-label': '0'})
        self.digit.rect = {'x': 20, 'y': 20}
        self.confirm = booking_fixtures.BookingElement('확인', on_click=self.close_payment)

    def close_payment(self):
        self.window_handles.remove('payment-tab')
        self.window_urls['product-tab'] = COMPLETE_URL if self.completed else PENDING_URL

    def find_elements(self, by, selector):
        if self.current_window_handle not in self.window_handles:
            raise NoSuchWindowException('Offline payment window closed after final confirm')
        if by == By.CSS_SELECTOR and 'aria-label="확인"' in selector:
            return [self.confirm]
        if by == By.CSS_SELECTOR and '#mtk_pinCodeInput' in selector:
            return [self.digit] if 'a[role="button"]' in selector else [self.keypad]
        if ((by == By.CSS_SELECTOR and 'h1' in selector) or (by == By.TAG_NAME and selector == 'h1')):
            return [CompletionHeading()] if self.completed and self.current_window_handle == 'product-tab' else []
        return super().find_elements(by, selector)


class BookingCompletionFlowTests(unittest.TestCase):
    def test_final_confirm_closes_payment_and_propagates_confirmed_or_unknown_outcome(self):
        for completed, expected_status in ((True, 'confirmed'), (False, 'unconfirmed')):
            with self.subTest(completed=completed):
                driver = FinalConfirmationDriver(completed=completed)
                clock = SelectionClock()
                messages = []
                with patch.object(gui, '_select_booking_options'), \
                     patch.object(gui, 'WebDriverWait', booking_fixtures.ImmediateWait), \
                     patch.object(gui.time, 'time', clock), \
                     patch.object(gui.time, 'monotonic', clock), \
                     patch.object(gui.time, 'sleep', clock.sleep), \
                     patch.object(gui.Path, 'exists', return_value=False), \
                     patch.object(gui.Path, 'read_text', side_effect=AssertionError(
                         'Synthetic flow must never read an actual settings file')), \
                     patch.dict(gui.os.environ, {'NOL_MON_PIN': '000000'}):
                    result = gui.run_test_booking(
                        driver, gui.WatchConfig(time_specs=('08:00',), counts={'SENIOR': 1}),
                        age_label='경로', stop_before_pay=False, log=messages.append)
                self.assertIsInstance(result, gui.BookingResult)
                self.assertEqual(result.status, expected_status)
                self.assertEqual(driver.confirm.clicks, 1, 'Completion probing must never submit again')
                self.assertEqual(driver.pay.clicks, 1)
                self.assertEqual(driver.current_window_handle, 'product-tab')
                self.assertNotIn('offline-order', '\n'.join(messages))


if __name__ == '__main__':
    unittest.main()
