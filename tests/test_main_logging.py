"""CLI와 시작 실패가 안전한 날짜별 실행 기록으로 이어지는지 검증한다."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import main
from model import Availability, Catalog, Snapshot


class OfflineBrowser:
    def __init__(self, base, log):
        self.log = log
        self.log('브라우저 연결 orderGroupId=PRIVATE_ORDER bot=123456:abcdefghijk')

    def detach(self):
        self.log('브라우저 연결 해제')


class OfflineProbeClient:
    def __init__(self, browser):
        self.browser = browser

    def prepare(self, config):
        return Catalog(10367229, '화담숲 입장권', {})

    def schedules(self, config):
        return [{'date': '2026-10-25'}]

    def rounds(self, config, **kwargs):
        return [{'time': '08:00'}, {'time': '08:20'}]

    def check(self, config, stop):
        return Snapshot(config, (Availability('08:00', 'entry', 2, ('ADULT',), True),))


class MainLoggingTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.stdout = StringIO()
        self.stderr = StringIO()
        self.addCleanup(patch.stopall)
        patch.object(main, 'BASE', self.base).start()

    def run_main(self, *options):
        with patch('sys.argv', ['main.py', *options]), redirect_stdout(self.stdout), redirect_stderr(self.stderr):
            return main.main()

    def daily_log(self):
        paths = list((self.base / 'logs').glob('hwadam-*.log'))
        self.assertEqual(len(paths), 1, 'CLI와 시작 실패는 날짜별 실행 파일에 기록해야 한다')
        return paths[0].read_text(encoding='utf-8')

    def test_probe_persists_browser_progress_and_result_without_changing_json_output(self):
        # CLI가 콘솔에만 기록하거나 JSON 결과를 버리면 이 계약이 깨진다.
        with patch('browser.BrowserSession', OfflineBrowser), patch('api.LeisureClient', OfflineProbeClient):
            self.assertEqual(self.run_main('--probe'), 0)

        text = self.daily_log()
        for value in ('mode=probe', '상품 10367229 화담숲 입장권', '날짜 1 회차 2',
                      '브라우저 연결', '브라우저 연결 해제', 'event=PROBE_RESULT',
                      'date=2026-10-25', 'rounds=1', 'hits=1', 'event=CLI_COMPLETE'):
            self.assertIn(value, text)
        self.assertEqual(len(set(re.findall(r'run=([0-9a-f]{8})', text))), 1)
        self.assertNotIn('PRIVATE_ORDER', text + self.stdout.getvalue())
        self.assertNotIn('123456:abcdefghijk', text + self.stdout.getvalue())
        output = self.stdout.getvalue()
        data, _ = json.JSONDecoder().raw_decode(output[output.index('{'):])
        self.assertEqual(data['date'], '2026-10-25(일)')
        self.assertEqual(data['rounds'][0]['quantity'], 2)

    def test_sync_catalog_persists_progress_and_summary_with_sanitized_console_json(self):
        def sync(client, cache, stop, emit, *, resume):
            self.assertTrue(resume)
            emit({'kind': 'catalog_progress', 'message': '2026-10-25 저장 완료'})
            emit({'kind': 'catalog_error', 'message': '확인 실패 orderGroupId=PRIVATE_ORDER'})
            return {'days': 1, 'errors': ['확인 실패 orderGroupId=PRIVATE_ORDER'], 'cancelled': False}

        with patch('browser.BrowserSession', OfflineBrowser), patch('api.LeisureClient', OfflineProbeClient), \
                patch('catalog.sync_catalogues', sync):
            self.assertEqual(self.run_main('--sync-catalog', '--resume'), 0)

        text = self.daily_log()
        for value in ('mode=sync-catalog', '2026-10-25 저장 완료', '확인 실패',
                      'event=CATALOG_RESULT', 'days=1', 'errors=1', 'cancelled=0', 'event=CLI_COMPLETE'):
            self.assertIn(value, text)
        self.assertNotIn('PRIVATE_ORDER', text + self.stdout.getvalue())
        json_line = next(line for line in self.stdout.getvalue().splitlines() if line.startswith('{'))
        data = json.loads(json_line)
        self.assertEqual(data['days'], 1)
        self.assertFalse(data['cancelled'])
        self.assertEqual(data['errors'], ['확인 실패 orderGroupId=<주문번호 숨김>'])

    def test_probe_persists_stock_times_quantities_and_monorail_error_reason(self):
        class DetailedClient(OfflineProbeClient):
            def check(self, config, stop):
                return Snapshot(config, (
                    Availability('08:00', 'entry', 2, ('ADULT',), True),
                    Availability('08:20', 'entry', 0, (), False, reason='품절'),
                ), (Availability('08:30', 'monorail', 1, ('CHILD',), False,
                                 section='MONORAIL_SEGMENT_2', entry_time='08:00'),),
                    '일부 회차 조회 시간 초과 orderGroupId=PRIVATE_ORDER')

        with patch('browser.BrowserSession', OfflineBrowser), patch('api.LeisureClient', DetailedClient):
            self.assertEqual(self.run_main('--probe', '--monorail'), 0)

        text = self.daily_log()
        for value in ('event=MONITOR_CHECK', '08:00 공통 잔여 2장', '가능권종=성인', '08:20 품절',
                      'event=MONORAIL_CHECK', '08:30 공통 잔여 1장', '입장 기준=08:00',
                      'event=MONORAIL_CHECK_FAILED', '일부 회차 조회 시간 초과',
                      'orderGroupId=<주문번호 숨김>'):
            self.assertIn(value, text)
        self.assertNotIn('PRIVATE_ORDER', text)

    def test_stdout_failure_preserves_the_message_that_could_not_be_printed(self):
        class BrokenOutput(StringIO):
            def write(self, text):
                if text.startswith('상품 '): raise BrokenPipeError('console closed')
                return super().write(text)

        self.stdout = BrokenOutput()
        with patch('browser.BrowserSession', OfflineBrowser), patch('api.LeisureClient', OfflineProbeClient):
            self.assertEqual(self.run_main('--probe'), 2)

        text = self.daily_log()
        self.assertIn('상품 10367229 화담숲 입장권', text)
        self.assertIn('error_type=BrokenPipeError', text)

    def test_json_stdout_failure_preserves_the_fetched_snapshot(self):
        class BrokenOutput(StringIO):
            def write(self, text):
                if text.startswith('{'): raise BrokenPipeError('console closed')
                return super().write(text)

        self.stdout = BrokenOutput()
        with patch('browser.BrowserSession', OfflineBrowser), patch('api.LeisureClient', OfflineProbeClient):
            self.assertEqual(self.run_main('--probe'), 2)

        text = self.daily_log()
        self.assertIn('event=MONITOR_CHECK', text)
        self.assertIn('08:00 공통 잔여 2장', text)
        self.assertIn('error_type=BrokenPipeError', text)

    def test_cli_failure_keeps_start_and_safe_error_in_the_same_run(self):
        class BrokenClient(OfflineProbeClient):
            def prepare(self, config):
                raise RuntimeError('PRIVATE_COOKIE PRIVATE_HEADER 123456:abcdefghijk')

        with patch('browser.BrowserSession', OfflineBrowser), patch('api.LeisureClient', BrokenClient):
            self.assertEqual(self.run_main('--probe'), 2)

        text = self.daily_log()
        self.assertIn('event=CLI_START', text)
        self.assertIn('event=APP_ERROR', text)
        self.assertIn('error_type=RuntimeError', text)
        self.assertRegex(text, r'test_main_logging\.py:\d+:prepare')
        self.assertIn('브라우저 연결 해제', text)
        self.assertNotIn('event=CLI_COMPLETE', text)
        self.assertEqual(len(set(re.findall(r'run=([0-9a-f]{8})', text))), 1)
        for secret in ('PRIVATE_COOKIE', 'PRIVATE_HEADER', '123456:abcdefghijk', 'Traceback'):
            self.assertNotIn(secret, text)

    def test_repeated_gui_startup_errors_append_safe_locations_and_preserve_old_log(self):
        directory = self.base / 'logs'
        directory.mkdir()
        legacy = directory / 'startup_error.log'
        legacy.write_text('기존 시작 오류 기록', encoding='utf-8')

        def fail_gui(smoke):
            private_local = 'PRIVATE_LOCAL_VALUE'
            raise ValueError('PRIVATE_EXCEPTION_BODY ' + private_local)

        with patch.object(main, 'gui', fail_gui):
            self.assertEqual(self.run_main('--gui-smoke'), 2)
            self.assertEqual(self.run_main('--gui-smoke'), 2)

        text = self.daily_log()
        self.assertEqual(text.count('event=APP_ERROR'), 2)
        self.assertEqual(len(set(re.findall(r'run=([0-9a-f]{8})', text))), 2)
        self.assertIn('mode=gui-smoke', text)
        self.assertIn('error_type=ValueError', text)
        self.assertRegex(text, r'test_main_logging\.py:\d+:fail_gui')
        for value in ('PRIVATE_EXCEPTION_BODY', 'PRIVATE_LOCAL_VALUE', 'raise ValueError', str(self.base)):
            self.assertNotIn(value, text)
        self.assertEqual(legacy.read_text(encoding='utf-8'), '기존 시작 오류 기록')


if __name__ == '__main__':
    unittest.main()
