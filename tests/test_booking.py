"""Booking rehearsals stop at payment readiness and use the shared GUI worker."""
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.common.by import By

import gui
from gui import AGE_NAMES, DEFAULT_URL, WatchConfig
from test_booking_selection import SelectionClock, SelectionDriver


class BookingElement:
    def __init__(self, text, *, attributes=None, enabled=True, children=None, on_click=None):
        self.text = text
        self.attributes = attributes or {}
        self.enabled = enabled
        self.children = children or {}
        self.on_click = on_click
        self.clicks = 0
        self.selected = False

    def get_attribute(self, name):
        return self.attributes.get(name)

    def is_enabled(self):
        return self.enabled and 'disabled' not in self.attributes

    def find_elements(self, by, selector):
        return self.children.get((by, selector), [])

    def is_displayed(self):
        return True

    def is_selected(self):
        return self.selected

    def click(self):
        self.clicks += 1
        self.selected = not self.selected
        if self.on_click:
            self.on_click()


class BookingSwitchTo:
    def __init__(self, driver):
        self.driver = driver

    def window(self, handle):
        if handle not in self.driver.window_handles:
            raise ValueError('Unknown fake browser window')
        self.driver.current_window_handle = handle
        self.driver.current_url = self.driver.window_urls[handle]


class BookingDriver:
    """A closed set of page controls; unexpected selectors return no match."""
    current_url = DEFAULT_URL

    def __init__(self):
        self.window_handles = ['product-tab']
        self.current_window_handle = 'product-tab'
        self.window_urls = {'product-tab': DEFAULT_URL}
        self.switch_to = BookingSwitchTo(self)
        self.time = BookingElement('08:00 선택 가능', attributes={'aria-label': '08:00 선택'},
                                   children={(By.TAG_NAME, 'p'): [BookingElement('08:00')]})
        self.age = BookingElement('입장권 경로')
        self.price = BookingElement('일반 9,000원')
        self.buy = BookingElement('구매하기')
        self.money = BookingElement('NOL 머니')
        self.agree = BookingElement('전체 동의', attributes={'aria-label': '전체 동의'})
        self.other_checkbox = BookingElement('선택 정보 수신')
        self.pay = BookingElement('9,000원 결제하기', on_click=self.open_payment_tab)
        self.has_pay = True
        self.has_agree = True
        self.actions = []
        self.service = Mock()
        self.selection = SelectionDriver(SelectionClock())
        self.selection.selected['date'] = WatchConfig().date
        self.selection.expanded = None

    def open_payment_tab(self):
        self.window_handles.append('payment-tab')
        self.window_urls['payment-tab'] = 'https://nol-money.example.invalid/checkout'

    def find_elements(self, by, selector):
        self.actions.append(('find', by, selector))
        selected = self.selection.find_elements(by, selector)
        if selected:
            return selected
        # Never expose a PIN screen: the fake flow must stop before settings are read.
        if '#mtk_pinCodeInput' in selector:
            return []
        if '결제하기' in selector:
            if "not(@aria-disabled='true')" in selector and self.pay.get_attribute('aria-disabled') == 'true':
                return []
            if "not(@data-state='loading')" in selector and self.pay.get_attribute('data-state') == 'loading':
                return []
            return [self.pay] if self.has_pay else []
        if '구매하기' in selector:
            return [self.buy]
        if 'NOL 머니' in selector:
            return [self.money]
        if 'checkbox' in selector:
            if by == By.CSS_SELECTOR:
                return [self.agree, self.other_checkbox] if self.has_agree else [self.other_checkbox]
            return [self.agree] if self.has_agree else []
        if '전체' in selector or '동의' in selector:
            return [self.agree] if self.has_agree else []
        if '입장권' in selector or '경로' in selector:
            return [self.age]
        if '일반' in selector:
            return [self.price]
        if by == By.CSS_SELECTOR and 'role="option"' in selector:
            return [self.time]
        return []

    def find_element(self, by, selector):
        values = self.find_elements(by, selector)
        if not values:
            raise NoSuchElementException(selector)
        return values[0]

    def execute_script(self, script, element):
        self.actions.append(('script', script))
        if '.click()' in script:
            element.click()

    def get(self, url):
        self.actions.append(('navigate', url))
        self.current_url = url
        self.window_urls[self.current_window_handle] = url


class ImmediateWait:
    def __init__(self, driver, timeout, *args, **kwargs):
        self.driver = driver

    def until(self, predicate):
        try:
            value = predicate(self.driver)
        except NoSuchElementException:
            value = False
        if not value:
            raise TimeoutException('Test page has no matching enabled element')
        return value


class PatchedTestCase(unittest.TestCase):
    def keep_patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result


class BookingFlowTests(PatchedTestCase):
    def setUp(self):
        self.assertTrue(callable(getattr(gui, 'run_test_booking', None)),
                        'The merged GUI must expose the booking flow')
        self.assertTrue(hasattr(gui, 'BookingTestError'))
        self.driver = BookingDriver()
        self.config = WatchConfig(time_specs=('08:00',), counts={'SENIOR': 1})
        self.keep_patch(patch.object(gui, 'WebDriverWait', ImmediateWait))
        self.keep_patch(patch('time.sleep'))

    def run_booking(self, **kwargs):
        return gui.run_test_booking(self.driver, self.config,
                                    age_label='경로', log=lambda message: None, **kwargs)

    def test_ready_checkout_never_clicks_final_payment(self):
        self.assertTrue(self.run_booking())
        self.assertEqual(self.driver.selection.clicks['option:time'], 1)
        self.assertEqual(self.driver.selection.clicks['option:age'], 1)
        self.assertEqual(self.driver.buy.clicks, 1)
        self.assertEqual(self.driver.money.clicks, 1)
        self.assertTrue(self.driver.agree.selected)
        self.assertEqual(self.driver.other_checkbox.clicks, 0)
        self.assertEqual(self.driver.pay.clicks, 0)

    def test_disabling_payment_stop_is_rejected_before_any_browser_action(self):
        with self.assertRaises(gui.BookingTestError):
            self.run_booking(stop_before_pay=False)
        self.assertEqual(self.driver.actions, [])
        self.assertEqual(self.driver.pay.clicks, 0)

    def test_missing_final_payment_is_failure(self):
        self.driver.has_pay = False
        with self.assertRaises(gui.BookingTestError):
            self.run_booking()
        self.assertEqual(self.driver.pay.clicks, 0)

    def test_hidden_final_payment_is_not_reported_ready(self):
        self.driver.pay.is_displayed = lambda: False
        with self.assertRaises(gui.BookingTestError):
            self.run_booking()
        self.assertEqual(self.driver.pay.clicks, 0)

    def test_disabled_final_payment_is_failure(self):
        for attributes, enabled in (({'aria-disabled': 'true'}, True),
                                    ({'disabled': 'true'}, True), ({}, False)):
            with self.subTest(attributes=attributes, enabled=enabled):
                self.driver = BookingDriver()
                self.driver.pay.attributes = attributes
                self.driver.pay.enabled = enabled
                with self.assertRaises(gui.BookingTestError):
                    self.run_booking()
                self.assertEqual(self.driver.pay.clicks, 0)

    def test_missing_explicit_all_agreement_is_failure(self):
        self.driver.has_agree = False
        with self.assertRaises(gui.BookingTestError):
            self.run_booking()
        self.assertEqual(self.driver.pay.clicks, 0)
        self.assertEqual(self.driver.other_checkbox.clicks, 0)

    def test_closed_app_cancels_before_browser_actions(self):
        with self.assertRaises(gui.BookingTestError):
            self.run_booking(should_stop=lambda: True)
        self.assertEqual(self.driver.actions, [])


class OrderedLock:
    def __init__(self, name, events):
        self.name = name
        self.events = events
        self.depth = 0
        self.lock = threading.RLock()

    def __enter__(self):
        self.lock.acquire()
        self.depth += 1
        self.events.append(self.name)
        return self

    def __exit__(self, *exc):
        self.depth -= 1
        self.lock.release()


class BookingGuiTests(PatchedTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = gui.App.__new__(gui.App)
        self.addCleanup(self.close_app)
        gui.App.__init__(self.app, self.root, Path(self.folder.name), auto_open_browser=False)
        self.root.update()
        self.panel = self.app.operations
        self.app.vars['times'].set('08:00')
        for age in AGE_NAMES:
            self.app.counts[age].set('2' if age == 'SENIOR' else '0')
        self.driver = BookingDriver()
        self.app.browser.driver = self.driver
        self.keep_patch(patch.object(self.app.browser, '_select'))
        self.keep_patch(patch.object(self.app, '_problem'))
        self.panel.toggle_admin()
        self.condition_states = {widget: str(widget.cget('state'))
                                 for widget in self.app.condition_widgets}

    def close_app(self):
        if hasattr(self.app, 'poll_id'):
            self.app.close()
            cleanup = getattr(self.app, 'cleanup_thread', None)
            if cleanup:
                cleanup.join(timeout=5)
        else:
            # Startup failure still needs to release the real log and executor.
            if hasattr(self.app, 'executor'):
                self.app.executor.shutdown(wait=True, cancel_futures=True)
            if hasattr(self.app, 'log_handler'):
                self.app.log_handler.close()
            self.root.destroy()

    def capture_jobs(self):
        jobs = []
        self.keep_patch(patch.object(self.app, '_background', side_effect=jobs.append))
        return jobs

    def finish_job(self, jobs):
        self.assertEqual(len(jobs), 1, 'Booking must use the existing shared worker')
        jobs.pop()()
        self.app._poll()

    def assert_unlocked(self):
        self.assertFalse(self.app.busy)
        self.assertEqual(str(self.panel.test_pay_button.cget('state')), 'normal')
        self.assertEqual(str(self.app.start_button.cget('state')), 'normal')
        for widget, initial in self.condition_states.items():
            self.assertEqual(str(widget.cget('state')), initial)

    def test_operations_page_can_start_with_both_rehearsal_controls(self):
        self.panel.toggle_admin()
        self.assertEqual(str(self.panel.test_pay_button.cget('state')), 'disabled')
        self.panel.toggle_admin()
        self.panel.run_button.invoke()
        self.assertTrue(self.panel.rehearsing)
        self.panel.stop_preview()
        self.assertFalse(self.panel.rehearsing)
        self.assertEqual(str(self.panel.test_pay_button.cget('state')), 'normal')

    def test_senior_booking_uses_normalized_config_and_persists_progress(self):
        self.app.counts['CHILD'].set('1')
        jobs = self.capture_jobs()
        lock_events = []
        self.app.client.lock = OrderedLock('client', lock_events)
        self.app.browser.lock = OrderedLock('browser', lock_events)
        captured = []

        def booking(driver, config, **kwargs):
            self.assertIs(driver, self.driver)
            self.assertEqual(config.section, 'MONORAIL_SEGMENT_2')
            self.assertEqual(config.selected_ages, ('SENIOR', 'CHILD'))
            self.assertEqual(config.counts['SENIOR'], 2)
            self.assertEqual(kwargs['age_label'], '경로')
            self.assertIs(kwargs['stop_before_pay'], True)
            self.assertEqual((self.app.client.lock.depth, self.app.browser.lock.depth), (1, 1))
            kwargs['log']('[TEST-BOOKING] regression-progress-marker')
            captured.append(config)
            return True

        with patch.object(gui, 'run_test_booking', side_effect=booking):
            self.panel.start_test_booking()
            self.assertTrue(self.app.busy)
            self.assertEqual(str(self.panel.test_pay_button.cget('state')), 'disabled')
            self.assertFalse(self.panel.rehearsing)
            self.assertEqual(len(jobs), 1)
            jobs.pop()()
            logs = '\n'.join(path.read_text(encoding='utf-8')
                             for path in (self.app.base / 'logs').glob('*.log'))
            self.assertIn('regression-progress-marker', logs,
                          'Worker progress must be persisted before the GUI consumes its queue')
            self.app._poll()
        self.assertEqual(len(captured), 1)
        self.assertEqual(lock_events[:2], ['client', 'browser'])
        self.assertIn('완료', self.panel.test_pay_status.get())
        self.assert_unlocked()

    def test_running_booking_blocks_duplicate_monitor_and_session_work(self):
        jobs = self.capture_jobs()
        with patch.object(gui, 'run_test_booking', return_value=True), \
             patch.object(self.app.runner, 'start') as monitor, \
             patch.object(self.app.browser, 'save_login_session') as save_session:
            self.app.session_pending = True
            self.panel.start_test_booking()
            self.assertEqual(jobs, [], 'Wait for an existing session renewal before using the selected browser page')
            self.app.session_pending = False
            self.panel.start_test_booking()
            self.panel.start_test_booking()
            self.app._start()
            self.app._save_session()
            self.app._keep_session_alive()
            self.assertEqual(len(jobs), 1)
            self.assertFalse(self.app.session_pending)
            monitor.assert_not_called()
            save_session.assert_not_called()
            self.assertEqual(str(self.app.start_button.cget('state')), 'disabled')
            self.assertEqual(str(self.app.session_button.cget('state')), 'disabled')
            self.finish_job(jobs)
        self.assert_unlocked()

    def test_booking_failure_restores_controls_and_reports_failure(self):
        jobs = self.capture_jobs()
        with patch.object(gui, 'run_test_booking', side_effect=gui.BookingTestError('결제 버튼 없음')):
            self.panel.start_test_booking()
            self.finish_job(jobs)
        self.assertIn('실패', self.panel.test_pay_status.get())
        self.assertIn('결제 버튼 없음', self.app.log_text.get('1.0', 'end'))
        self.assert_unlocked()

    def test_disconnected_browser_reports_failure_and_restores_controls(self):
        self.app.browser.driver = None
        jobs = self.capture_jobs()
        with patch.object(gui, 'run_test_booking') as booking:
            self.panel.start_test_booking()
            if jobs:
                self.finish_job(jobs)
            booking.assert_not_called()
        self.assertIn('실패', self.panel.test_pay_status.get())
        self.assert_unlocked()

    def test_wrong_product_fails_without_navigating_away_from_selected_date(self):
        self.driver.current_url = 'https://leisure-web.yanolja.com/leisure/10323843'
        jobs = self.capture_jobs()
        with patch.object(gui, 'run_test_booking') as booking:
            self.panel.start_test_booking()
            self.finish_job(jobs)
            booking.assert_not_called()
        self.assertFalse(any(action[0] == 'navigate' for action in self.driver.actions))
        self.assertIn('실패', self.panel.test_pay_status.get())
        self.assert_unlocked()

    def test_browser_booking_runs_on_existing_executor_thread(self):
        threads = []

        def booking(*args, **kwargs):
            threads.append(threading.current_thread().name)
            return True

        with patch.object(gui, 'run_test_booking', side_effect=booking):
            self.panel.start_test_booking()
            deadline = time.monotonic() + 3
            while self.app.busy and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.01)
        self.assertEqual(len(threads), 1)
        self.assertTrue(threads[0].startswith('hwadam-query'), threads)
        self.assertIn('완료', self.panel.test_pay_status.get())
        self.assert_unlocked()


if __name__ == '__main__':
    unittest.main()
