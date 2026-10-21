"""Offline regressions for option selection; this fake has no checkout controls."""
import calendar
import datetime as dt
import re
import unittest
from collections import Counter
from unittest.mock import patch

from selenium.common.exceptions import (ElementNotInteractableException, NoSuchElementException,
                                        StaleElementReferenceException)
from selenium.webdriver.common.by import By

import gui


LABELS = {'date': '이용 날짜', 'time': '이용 시간', 'age': '이용자', 'price': '이용자 할인'}


class SelectionClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class SelectionElement:
    def __init__(self, driver, kind, field=None, value=None):
        self.driver, self.kind, self.field, self.value = driver, kind, field, value

    @property
    def text(self):
        d = self.driver
        if self.kind == 'header':
            value = d.selected[self.field]
            if self.field == 'date' and value:
                date = dt.date.fromisoformat(value)
                return date.strftime('%Y.%m.%d') + '(' + '월화수목금토일'[date.weekday()] + ')'
            return value or '선택해 주세요'
        if self.kind == 'section':
            return LABELS[self.field] + '\n' + SelectionElement(d, 'header', self.field).text
        if self.kind == 'option':
            if d.stale_reads[self.field]:
                d.stale_reads[self.field] -= 1
                raise StaleElementReferenceException('Option list rerendered')
            if self.field == 'time':
                return self.value + (' 품절' if d.sold_out else ' 34매')
            return self.value
        if self.kind in ('tile', 'abbr'):
            return str(dt.date.fromisoformat(self.value).day)
        if self.kind in ('calendar', 'month'):
            return f'{d.month[0]}.{d.month[1]:02d}'
        if self.kind == 'opener':
            return '옵션 선택하기'
        if self.kind == 'ticket' and d.stale_reads['ticket']:
            d.stale_reads['ticket'] -= 1
            raise StaleElementReferenceException('Selected ticket rerendered')
        if self.kind in ('ticket', 'text'):
            return self.value
        return ''

    def get_attribute(self, name):
        d = self.driver
        if self.kind == 'header':
            return {'aria-haspopup': 'listbox', 'aria-expanded': str(d.expanded == self.field).lower()}.get(name)
        if self.kind == 'option':
            return {'role': 'option', 'aria-label': self.text,
                    'aria-disabled': str(d.disabled and self.field == 'time').lower()}.get(name)
        if self.kind in ('tile', 'abbr') and name == 'aria-label':
            date = dt.date.fromisoformat(self.value)
            return f'{date.year}년 {date.month}월 {date.day}일'
        if self.kind == 'calendar' and name == 'class':
            return 'leisure-calendar'
        if self.kind == 'tile' and name == 'class':
            return 'react-calendar__tile'
        if self.kind in ('previous', 'next') and name == 'aria-label':
            return '이전 달로 이동' if self.kind == 'previous' else '다음 날짜로 이동'
        return None

    def is_displayed(self):
        if self.kind == 'opener':
            return not self.driver.panel_open
        if self.kind in ('calendar', 'month', 'previous', 'next', 'tile', 'abbr'):
            return self.driver.panel_open and self.driver.expanded == 'date'
        if self.kind == 'option':
            return self.driver.panel_open and self.driver.expanded == self.field
        return self.driver.panel_open

    def is_enabled(self):
        return not (self.kind == 'option' and self.field == 'time' and self.driver.disabled)

    def click(self):
        d = self.driver
        key = self.kind + ':' + (self.field or self.value or '')
        d.clicks[key] += 1
        if self.kind == 'option' and d.not_interactable_clicks[self.field]:
            d.not_interactable_clicks[self.field] -= 1
            raise ElementNotInteractableException('Option is still animating')
        if self.kind == 'opener':
            d.panel_open = True
            d.expanded = 'date'
        elif self.kind == 'header':
            d.expanded = self.field
        elif self.kind in ('next', 'previous'):
            if d.lost_clicks['month']:
                d.lost_clicks['month'] -= 1
                return
            year, month = d.month
            total = year * 12 + month - 1 + (1 if self.kind == 'next' else -1)
            d.month = total // 12, total % 12 + 1
        elif self.kind in ('tile', 'abbr'):
            d.selected['date'] = self.value
            d.selected.update(time='', age='', price='')
            d.expanded = 'time'
        elif self.kind == 'option':
            if d.lost_clicks[self.field]:
                d.lost_clicks[self.field] -= 1
                d.expanded = None
                return
            d.selected[self.field] = self.value
            if self.field == 'price':
                title = ' / '.join([d.selected['date'].replace('-', '.'),
                                    d.selected['time'], d.selected['age'], '일반'])
                (d.pending_tickets if d.clock() < d.ready_at['ticket'] else d.tickets).append(title)
                d.selected.update(age='', price='')
            d.expanded = {'time': 'age', 'age': 'price', 'price': None}[self.field]

    def find_elements(self, by, selector):
        return self.driver.lookup(by, selector, self)

    def find_element(self, by, selector):
        matches = self.find_elements(by, selector)
        if not matches:
            raise NoSuchElementException(selector)
        return matches[0]


class SelectionDriver:
    """Small live-state DOM model, with selectors scoped to the actual NOL sections."""
    def __init__(self, clock):
        self.clock = clock
        self.panel_open = True
        self.expanded = 'date'
        self.month = (2026, 10)
        self.selected = {'date': '2026-11-12', 'time': '', 'age': '', 'price': ''}
        self.clicks = Counter()
        self.lost_clicks = Counter()
        self.stale_reads = Counter()
        self.not_interactable_clicks = Counter()
        self.ready_at = Counter()
        self.sold_out = False
        self.disabled = False
        self.missing_time = False
        self.hide_target_date = False
        self.tickets = []
        self.pending_tickets = []
        self.price_text = '일반\n11,000원'

    def options(self, field):
        if self.expanded != field or self.clock() < self.ready_at[field]:
            return []
        values = {'time': ['08:20'] if self.missing_time else ['08:00', '08:20'],
                  'age': ['입장권 성인', '입장권 경로', '입장권 어린이'],
                  'price': [self.price_text]}[field]
        return [SelectionElement(self, 'option', field, value) for value in values]

    def dates(self):
        year, month = self.month
        dates = [dt.date(year, month, day).isoformat()
                 for day in range(1, calendar.monthrange(year, month)[1] + 1)]
        return [date for date in dates if not (self.hide_target_date and date == '2026-11-12')]

    def lookup(self, by, selector, scope=None):
        # No buy/pay/PIN selector exists in this fixture.
        if scope and scope.kind == 'abbr' and ('..' == selector or 'ancestor::button' in selector or 'parent::button' in selector):
            return [SelectionElement(self, 'tile', value=scope.value)]
        if scope and scope.kind == 'tile':
            if 'abbr' in selector:
                return [SelectionElement(self, 'abbr', value=scope.value)]
            return []
        if scope and scope.kind == 'option':
            return [SelectionElement(self, 'text', value=scope.value)] if selector == 'p' else []
        if scope and scope.kind == 'section':
            if 'aria-haspopup' in selector or selector == 'button':
                return [SelectionElement(self, 'header', scope.field)]
            if 'role' in selector and 'option' in selector:
                options = self.options(scope.field) if scope.field != 'date' else []
                prefix = re.search(r"starts-with\(normalize-space\(\.\),\s*(['\"])(.*?)\1\)", selector)
                if prefix:
                    options = [option for option in options
                               if ' '.join(option.value.split()).startswith(prefix.group(2))]
                return options
            if 'leisure-calendar' in selector and scope.field == 'date' and self.expanded == 'date':
                return [SelectionElement(self, 'calendar')]
            return []
        if not scope:
            if selector == 'h3':
                if self.clock() >= self.ready_at['ticket']:
                    self.tickets.extend(self.pending_tickets)
                    self.pending_tickets.clear()
                return [SelectionElement(self, 'ticket', value=title) for title in self.tickets]
            if '옵션 선택하기' in selector:
                return [SelectionElement(self, 'opener')] if not self.panel_open else []
            for field, label in LABELS.items():
                if re.search(r"(?:normalize-space\((?:\.|text\(\))?\)|text\(\))\s*=\s*['\"]" + re.escape(label) + r"['\"]", selector):
                    return [SelectionElement(self, 'section', field)] if self.panel_open else []
            if 'leisure-calendar' in selector:
                return [SelectionElement(self, 'calendar')] if self.panel_open and self.expanded == 'date' else []
        if self.expanded == 'date' and (not scope or scope.kind == 'calendar'):
            if '이전 달로 이동' in selector or 'navigation__prev-button' in selector:
                return [SelectionElement(self, 'previous')]
            if '다음 날짜로 이동' in selector or 'navigation__next-button' in selector:
                return [SelectionElement(self, 'next')]
            if 'navigation__label' in selector:
                return [SelectionElement(self, 'month')]
            if 'abbr' in selector or 'react-calendar__tile' in selector:
                match = re.search(r'(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일', selector)
                wanted = dt.date(*map(int, match.groups())).isoformat() if match else None
                kind = 'tile' if 'button' in selector else 'abbr'
                return [SelectionElement(self, kind, value=date) for date in self.dates()
                        if wanted is None or date == wanted]
        return []

    def find_elements(self, by, selector):
        return self.lookup(by, selector)

    def find_element(self, by, selector):
        matches = self.find_elements(by, selector)
        if not matches:
            raise NoSuchElementException(selector)
        return matches[0]

    def execute_script(self, script, *args):
        if '.click()' in script:
            args[0].click()


class BookingSelectionTests(unittest.TestCase):
    def setUp(self):
        self.clock = SelectionClock()
        self.driver = SelectionDriver(self.clock)
        self.config = gui.WatchConfig(date='2026-11-12', time_specs=('08:00',), counts={'ADULT': 1})
        self.messages = []
        for name, replacement in [('monotonic', self.clock), ('time', self.clock), ('sleep', self.clock.sleep)]:
            patcher = patch.object(gui.time, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def select(self, **kwargs):
        helper = getattr(gui, '_select_booking_options', None)
        self.assertTrue(callable(helper), 'Booking option selection must have an independently testable helper')
        return helper(self.driver, self.config, step=self.messages.append, timeout=3, **kwargs)

    def assert_selection_complete(self):
        self.assertEqual(self.driver.selected,
                         {'date': '2026-11-12', 'time': '08:00', 'age': '', 'price': ''})
        self.assertIn('2026.11.12 / 08:00 / 입장권 성인 / 일반', self.driver.tickets)

    def test_matching_date_header_with_open_wrong_month_calendar_selects_actual_target_cell(self):
        self.select()
        self.assert_selection_complete()
        self.assertEqual(self.driver.clicks['tile:2026-10-12'], 0)
        self.assertEqual(self.driver.clicks['tile:2026-11-12'], 1)
        self.assertEqual(self.driver.clicks['header:date'], 0)

    def test_new_option_panel_opens_then_navigates_to_exact_date(self):
        self.driver.panel_open = False
        self.driver.expanded = None
        self.driver.selected['date'] = ''
        self.select()
        self.assert_selection_complete()
        self.assertEqual(self.driver.clicks['opener:'], 1)

    def test_wrong_selected_month_does_not_reuse_same_day_number(self):
        self.driver.selected['date'] = '2026-10-12'
        self.driver.expanded = None
        self.select()
        self.assert_selection_complete()
        self.assertEqual(self.driver.clicks['tile:2026-10-12'], 0)
        self.assertEqual(self.driver.clicks['tile:2026-11-12'], 1)

    def test_lost_month_navigation_click_is_retried_after_state_recheck(self):
        self.driver.lost_clicks['month'] = 1
        self.select()
        self.assert_selection_complete()
        self.assertEqual(self.driver.clicks['next:'], 2)

    def test_existing_matching_ticket_is_reused_without_adding_duplicate(self):
        self.driver.selected.update(time='08:00')
        self.driver.tickets = ['2026.11.12 / 08:00 / 입장권 성인 / 일반']
        self.driver.expanded = None
        self.select()
        self.assert_selection_complete()
        self.assertEqual(sum(self.driver.clicks.values()), 0)
        self.assertEqual(len(self.driver.tickets), 1)

    def test_stale_existing_ticket_heading_retries_without_click_or_duplicate(self):
        self.driver.selected.update(time='08:00')
        self.driver.tickets = ['2026.11.12 / 08:00 / 입장권 성인 / 일반']
        self.driver.expanded = None
        self.driver.stale_reads['ticket'] = 1
        self.select()
        self.assert_selection_complete()
        self.assertEqual(sum(self.driver.clicks.values()), 0)
        self.assertEqual(len(self.driver.tickets), 1)

    def test_different_ticket_does_not_satisfy_target_selection(self):
        self.driver.tickets = ['2026.10.12 / 08:00 / 입장권 성인 / 일반']
        self.select()
        self.assert_selection_complete()
        self.assertEqual(len(self.driver.tickets), 2)

    def test_price_selection_verifies_added_ticket_despite_reset_headers(self):
        self.select()
        self.assert_selection_complete()
        self.assertEqual(self.driver.clicks['option:price'], 1)
        self.assertEqual(len(self.driver.tickets), 1)

    def test_price_label_and_amount_can_share_one_line(self):
        self.driver.price_text = '일반 11,000원'
        self.select()
        self.assert_selection_complete()

    def test_delayed_ticket_row_does_not_trigger_duplicate_price_click(self):
        self.driver.ready_at['ticket'] = self.clock() + 1.6
        self.select()
        self.assert_selection_complete()
        self.assertEqual(self.driver.clicks['option:price'], 1)
        self.assertEqual(len(self.driver.tickets), 1)

    def test_unconfirmed_price_click_times_out_without_duplicate_addition(self):
        self.driver.ready_at['ticket'] = self.clock() + 100
        with self.assertRaises(gui.BookingTestError):
            self.select()
        self.assertEqual(self.driver.clicks['option:price'], 1)
        self.assertEqual(self.driver.tickets, [])

    def test_matching_date_with_collapsed_time_list_opens_time_only(self):
        self.driver.expanded = None
        self.select()
        self.assert_selection_complete()
        self.assertEqual(self.driver.clicks['header:date'], 0)
        self.assertEqual(self.driver.clicks['header:time'], 1)

    def test_delayed_time_options_are_requeried_until_present(self):
        self.driver.ready_at['time'] = self.clock() + 1.2
        self.select()
        self.assert_selection_complete()
        self.assertGreaterEqual(self.clock(), 101.2)

    def test_lost_time_click_reopens_list_and_verifies_header_before_advancing(self):
        self.driver.lost_clicks['time'] = 1
        self.select()
        self.assert_selection_complete()
        self.assertEqual(self.driver.clicks['option:time'], 2)
        self.assertGreaterEqual(self.driver.clicks['header:time'], 1)

    def test_stale_time_option_is_requeried(self):
        self.driver.stale_reads['time'] = 1
        self.select()
        self.assert_selection_complete()

    def test_transient_noninteractable_time_click_retries_after_animation(self):
        self.driver.not_interactable_clicks['time'] = 1
        self.select()
        self.assert_selection_complete()
        self.assertEqual(self.driver.clicks['option:time'], 2)

    def test_missing_date_stops_before_selecting_any_time(self):
        self.driver.hide_target_date = True
        with self.assertRaises(gui.BookingTestError):
            self.select()
        self.assertEqual(self.driver.clicks['option:time'], 0)

    def test_sold_out_time_is_not_clicked_and_is_diagnosed(self):
        self.driver.sold_out = True
        with self.assertRaises(gui.BookingTestError):
            self.select()
        self.assertEqual(self.driver.clicks['option:time'], 0)
        self.assertTrue(any('품절' in message or '매진' in message for message in self.messages))

    def test_missing_time_is_diagnosed_separately_from_sold_out(self):
        self.driver.missing_time = True
        with self.assertRaises(gui.BookingTestError):
            self.select()
        self.assertEqual(self.driver.clicks['option:time'], 0)
        self.assertTrue(any('미발견' in message or '없' in message for message in self.messages))

    def test_disabled_time_is_not_clicked(self):
        self.driver.disabled = True
        with self.assertRaises(gui.BookingTestError):
            self.select()
        self.assertEqual(self.driver.clicks['option:time'], 0)

    def test_cancellation_during_retry_propagates_without_more_clicks(self):
        self.driver.ready_at['time'] = self.clock() + 10
        def checkpoint():
            if self.clock() >= 100.8:
                raise gui.BookingTestError('선택 취소')
        with self.assertRaisesRegex(gui.BookingTestError, '선택 취소'):
            self.select(checkpoint=checkpoint)
        self.assertEqual(self.driver.clicks['option:time'], 0)


if __name__ == '__main__':
    unittest.main()
