"""Offline latency regressions; the DOM fixture has no purchase/payment controls."""
import re
import unittest
from collections import Counter
from unittest.mock import patch

import gui
from test_booking import BookingDriver
from test_booking_selection import SelectionClock, SelectionDriver, SelectionElement


class SpeedClock(SelectionClock):
    def __init__(self):
        super().__init__()
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        super().sleep(seconds)


class CountedOption(SelectionElement):
    @property
    def text(self):
        self.driver.option_text_reads[self.value] += 1
        return super().text

    def click(self):
        self.driver.option_click_times[self.field].append(self.driver.clock())
        super().click()


class SpeedDriver(SelectionDriver):
    def __init__(self, clock):
        super().__init__(clock)
        self.month = (2026, 11)
        self.option_text_reads = Counter()
        self.unrelated_options = []
        self.lookups = []
        self.option_click_times = {'time': [], 'age': [], 'price': []}

    def options(self, field):
        options = super().options(field)
        if options and field == 'time':
            values = self.unrelated_options + [option.value for option in options]
            return [CountedOption(self, 'option', field, value) for value in values]
        return options

    def lookup(self, by, selector, scope=None):
        self.lookups.append((self.clock(), scope.kind if scope else None, by, selector))
        return super().lookup(by, selector, scope)


class BookingSpeedTests(unittest.TestCase):
    def setUp(self):
        self.clock = SpeedClock()
        self.driver = SpeedDriver(self.clock)
        self.config = gui.WatchConfig(date='2026-11-12', time_specs=('08:00',), counts={'ADULT': 1})
        self.messages = []
        for name, replacement in [('monotonic', self.clock), ('time', self.clock), ('sleep', self.clock.sleep)]:
            patcher = patch.object(gui.time, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def select(self, **kwargs):
        return gui._select_booking_options(self.driver, self.config,
                                           step=self.messages.append, timeout=3, **kwargs)

    def assert_selected_ticket(self):
        self.assertEqual(self.driver.tickets, ['2026.11.12 / 08:00 / 입장권 성인 / 일반'])

    def test_immediate_selection_is_verified_without_fixed_sleep(self):
        self.select()
        self.assert_selected_ticket()
        self.assertEqual(self.clock.sleeps, [], 'Immediate DOM transitions must be rechecked without a fixed sleep')

    def test_large_time_list_does_not_read_unrelated_option_text(self):
        self.driver.unrelated_options = [f'09:{minute:02d}' for minute in range(60)]
        self.select()
        self.assert_selected_ticket()
        unrelated_reads = sum(self.driver.option_text_reads[value] for value in self.driver.unrelated_options)
        self.assertEqual(unrelated_reads, 0, 'Target lookup must not fetch every unrelated option text')

    def test_short_list_delay_is_detected_promptly_without_busy_polling(self):
        self.driver.ready_at['time'] = self.clock() + .18
        self.select()
        self.assert_selected_ticket()
        self.assertGreaterEqual(self.clock(), 100.18)
        self.assertLessEqual(self.clock(), 100.35)
        self.assertGreater(len(self.clock.sleeps), 0)
        self.assertLessEqual(len(self.clock.sleeps), 7)
        self.assertTrue(all(.049 <= delay <= .3 for delay in self.clock.sleeps))

    def test_permanently_missing_list_has_bounded_polling_and_no_click(self):
        self.driver.ready_at['time'] = self.clock() + 100
        with self.assertRaises(gui.BookingTestError):
            self.select()
        self.assertAlmostEqual(self.clock(), 103.0)
        self.assertLessEqual(len(self.clock.sleeps), 40)
        self.assertTrue(all(.049 <= delay <= .3 for delay in self.clock.sleeps[:-1]))
        self.assertEqual(self.driver.clicks['option:time'], 0)

    def test_adaptive_wait_remains_cancellable_before_options_appear(self):
        self.driver.ready_at['time'] = self.clock() + 100

        def checkpoint():
            if self.clock() >= 100.18:
                raise gui.BookingTestError('선택 취소')

        with self.assertRaisesRegex(gui.BookingTestError, '선택 취소'):
            self.select(checkpoint=checkpoint)
        self.assertLessEqual(self.clock(), 100.35)
        self.assertEqual(self.driver.clicks['option:time'], 0)
        self.assertEqual(self.driver.tickets, [])

    def test_lost_option_click_is_rechecked_but_not_rapidly_repeated(self):
        self.driver.lost_clicks['time'] = 1
        self.select()
        self.assert_selected_ticket()
        first, second = self.driver.option_click_times['time']
        self.assertGreaterEqual(second - first, .8)

    def test_time_reset_during_age_loading_is_revalidated_on_next_poll(self):
        self.driver.ready_at['age'] = self.clock() + .2
        reset_done = False

        def sleep_and_reset(seconds):
            nonlocal reset_done
            self.clock.sleep(seconds)
            if not reset_done and self.clock() >= 100.1:
                self.driver.selected.update(time='08:20', age='', price='')
                self.driver.expanded = 'time'
                reset_done = True

        with patch.object(gui.time, 'sleep', sleep_and_reset):
            self.select()
        self.assertTrue(reset_done)
        self.assert_selected_ticket()
        self.assertEqual(self.driver.clicks['option:time'], 2)
        self.assertEqual(self.driver.clicks['option:price'], 1)

    def assert_timing_breakdown(self, message):
        for label in ('소요', '조회', '클릭', '대기', '누적'):
            self.assertRegex(message, rf'{label}=\d+(?:\.\d+)?초')

    def test_success_logs_include_each_stage_timing_and_attempts(self):
        self.driver.ready_at['time'] = self.clock() + .18
        self.select()
        completed = [message for message in self.messages if '확인 완료' in message]
        self.assertEqual(len(completed), 5)
        for message in completed:
            self.assert_timing_breakdown(message)
            self.assertRegex(message, r'확인 \d+회')
        totals = [float(re.search(r'누적=(\d+(?:\.\d+)?)초', message).group(1))
                  for message in completed]
        self.assertEqual(totals, sorted(totals))
        self.assertGreater(totals[-1], 0)

    def test_timeout_diagnostic_includes_elapsed_query_click_and_wait_timing(self):
        self.driver.ready_at['time'] = self.clock() + 100
        with self.assertRaises(gui.BookingTestError) as failure:
            self.select()
        diagnostics = [message for message in self.messages + [str(failure.exception)]
                       if '실패' in message or '시간 초과' in message]
        self.assertTrue(diagnostics)
        self.assert_timing_breakdown(diagnostics[-1])

    def test_buy_completion_timing_is_logged_after_click_before_payment_method(self):
        driver = BookingDriver()
        driver.selection = self.driver
        events = []

        def buy_clicked():
            events.append('buy-clicked')
            self.clock.now += .12

        def log(message):
            self.messages.append(message)
            if '구매하기 클릭 완료' in message:
                events.append('buy-completion-log')
                raise gui.BookingCancelled('Offline stop immediately after purchase navigation')

        driver.buy.on_click = buy_clicked
        with patch.object(gui.Path, 'read_text', side_effect=AssertionError('Settings must not be read')):
            with self.assertRaises(gui.BookingCancelled):
                gui.run_test_booking(driver, self.config, log=log)
        self.assertEqual(events, ['buy-clicked', 'buy-completion-log'])
        self.assertEqual((driver.buy.clicks, driver.money.clicks, driver.pay.clicks), (1, 0, 0))
        completion = next(message for message in self.messages if '구매하기 클릭 완료' in message)
        self.assertRegex(completion, r'소요=0\.120초')
        self.assertRegex(completion, r'버튼대기=\d+(?:\.\d+)?초')
        self.assertRegex(completion, r'클릭·화면전환=0\.120초')
        self.assertRegex(completion, r'누적=0\.120초')


if __name__ == '__main__':
    unittest.main()
