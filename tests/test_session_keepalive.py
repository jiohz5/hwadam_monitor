"""GUI 유휴 시간에도 실제 인증 쿠키를 갱신하고 실패 시 저장본을 지킨다."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from urllib3.exceptions import ProtocolError

from api import ApiError, LoginRequired
from browser import BrowserSession
from gui import DEFAULT_URL
from test_browser import LoginDriver


class IdleLoginDriver(LoginDriver):
    """공식 계정 방문에서만 SID 값과 만료 시각을 발급하는 브라우저 경계."""

    def __init__(self, wall):
        self.wall = wall
        self.allow_auth = True
        self.renewals = 0
        self.fail_cookie_read = False
        super().__init__(logged=True, cookies=[
            {'name': 'yanolja_sid', 'value': 'original-private-sid',
             'domain': '.yanolja.com', 'path': '/', 'secure': True,
             'httpOnly': True, 'expires': wall() + 10800, 'sameSite': 'Lax'},
            {'name': 'kakao-test-auth', 'value': 'private-kakao-auth',
             'domain': '.accounts.kakao.com', 'path': '/', 'secure': True,
             'httpOnly': True, 'expires': -1, 'session': True, 'sameSite': 'None'}])

    def get(self, url):
        self.visited.append(url)
        self.current_url = url
        if url.startswith('https://accounts.yanolja.com?'):
            self.logged = self.allow_auth
            if self.logged:
                self.renewals += 1
                self.cookies = [cookie for cookie in self.cookies
                                if cookie['name'] != 'yanolja_sid']
                self.cookies.append({
                    'name': 'yanolja_sid', 'value': f'renewed-private-sid-{self.renewals}',
                    'domain': '.yanolja.com', 'path': '/', 'secure': True,
                    'httpOnly': True, 'expires': self.wall() + 10800, 'sameSite': 'Lax'})
        else:
            self.logged = self.allow_auth and any(
                cookie['name'] == 'yanolja_sid'
                and cookie.get('expires', 0) > self.wall() for cookie in self.cookies)

    def execute_cdp_cmd(self, command, values):
        if command == 'Network.getAllCookies' and self.fail_cookie_read:
            self.fail_cookie_read = False
            raise ProtocolError('temporary connection reset with private transport detail')
        return super().execute_cdp_cmd(command, values)


class SessionKeepaliveTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.folder.cleanup)
        self.mono = 10000.0
        self.wall = 1800000000.0
        monotonic = patch('browser.time.monotonic', side_effect=lambda: self.mono)
        wall_time = patch('browser.time.time', side_effect=lambda: self.wall)
        monotonic.start()
        wall_time.start()
        self.addCleanup(monotonic.stop)
        self.addCleanup(wall_time.stop)
        self.base = Path(self.folder.name) / 'hwadam_codex'
        self.base.mkdir()
        self.logs = []
        self.session = BrowserSession(self.base, self.logs.append)
        self.driver = IdleLoginDriver(lambda: self.wall)

    def attach(self):
        self.session.driver = self.driver
        self.session.tab = 'own-tab'

    def advance(self, seconds):
        self.mono += seconds
        self.wall += seconds

    def maintain(self):
        method = getattr(self.session, 'maintain_session', None)
        self.assertTrue(callable(method), '유휴 로그인 만료를 막는 maintain_session이 필요하다')
        return method(DEFAULT_URL)

    def saved_sid(self):
        saved = json.loads(self.session.cookie_file.read_text(encoding='utf-8'))
        return next(cookie for cookie in saved if cookie['name'] == 'yanolja_sid')

    def test_unconfigured_session_does_not_launch_or_probe_browser(self):
        # A new GUI must not open Edge merely because its idle timer runs.
        with patch.object(self.session, '_launch', side_effect=AssertionError('unexpected launch')), \
                patch.object(self.session, '_debug', side_effect=AssertionError('unexpected browser probe')):
            self.assertEqual(self.maintain()['state'], 'not_configured')
        self.assertIsNone(self.session.driver)
        self.assertFalse(self.session.cookie_file.exists())

    def test_first_idle_check_renews_real_sid_and_saves_extended_expiry(self):
        # Rewriting a cookie file without visiting SSO cannot satisfy this test.
        self.attach()
        original_expiry = self.driver.cookies[0]['expires']
        self.advance(60)
        self.session.headers = {'Access-Token': 'old-private-header'}
        self.session.ready_url = DEFAULT_URL

        self.assertEqual(self.maintain()['state'], 'renewed')

        saved = self.saved_sid()
        self.assertEqual(saved['value'], 'renewed-private-sid-1')
        self.assertGreater(saved['expires'], original_expiry)
        self.assertEqual(saved['expires'], 1800010860.0)
        self.assertFalse(self.session.headers)
        self.assertIsNone(self.session.ready_url)

    def test_saved_session_without_attached_driver_can_reconnect_and_renew(self):
        # Saved credentials should remain useful after the GUI reconnects.
        self.session.cookie_file.write_text(json.dumps(self.driver.cookies), encoding='utf-8')
        self.driver.cookies = []
        self.driver.logged = False

        def attach_saved(url):
            self.attach()

        with patch.object(self.session, '_attach', side_effect=attach_saved):
            self.assertEqual(self.maintain()['state'], 'renewed')
        self.assertEqual(self.saved_sid()['value'], 'renewed-private-sid-1')
        self.assertGreater(self.saved_sid()['expires'], self.wall)

    def test_repeat_idle_ticks_wait_then_check_without_refreshing_early(self):
        # Excess timer ticks must not repeatedly navigate the user's browser.
        self.attach()
        self.assertEqual(self.maintain()['state'], 'renewed')
        visited = list(self.driver.visited)
        self.advance(299)
        self.assertEqual(self.maintain()['state'], 'waiting')
        self.assertEqual(self.driver.visited, visited)
        self.advance(1)
        self.assertEqual(self.maintain()['state'], 'checked')
        self.assertEqual(self.driver.visited, visited)
        self.assertEqual(self.driver.renewals, 1)

    def test_idle_session_renews_after_thirty_minutes_even_with_valid_sid(self):
        # A healthy-looking long-lived cookie still needs periodic SSO traffic.
        self.attach()
        self.maintain()
        first_expiry = self.saved_sid()['expires']
        self.advance(1800)

        self.assertEqual(self.maintain()['state'], 'renewed')

        self.assertEqual(self.driver.renewals, 2)
        self.assertGreater(self.saved_sid()['expires'], first_expiry)
        self.assertEqual(self.saved_sid()['expires'], 1800012600.0)

    def test_resume_after_wall_clock_gap_checks_expiry_when_monotonic_barely_moved(self):
        # Some Windows sleep intervals do not advance the monotonic clock equally.
        self.attach()
        self.maintain()
        first_expiry = self.saved_sid()['expires']
        self.wall += 10801
        self.mono += 1

        self.assertEqual(self.maintain()['state'], 'renewed')

        self.assertGreater(self.saved_sid()['expires'], first_expiry)
        self.assertEqual(self.saved_sid()['expires'], 1800021601.0)

    def test_sid_at_ten_minute_margin_renews_before_schedule_is_due(self):
        # Waiting only for the thirty-minute schedule would expire a short SID.
        self.attach()
        self.session.save_session()
        self.driver.cookies[0]['expires'] = self.wall + 600

        self.assertEqual(self.maintain()['state'], 'renewed')

        self.assertEqual(self.saved_sid()['value'], 'renewed-private-sid-1')
        self.assertEqual(self.saved_sid()['expires'], 1800010800.0)

    def test_missing_or_expired_sid_renews_before_schedule_is_due(self):
        for sid_state in ('missing', 'expired'):
            with self.subTest(sid_state=sid_state):
                self.session = BrowserSession(self.base, self.logs.append)
                self.driver = IdleLoginDriver(lambda: self.wall)
                self.attach()
                self.session.save_session()
                if sid_state == 'missing':
                    self.driver.cookies = [cookie for cookie in self.driver.cookies
                                           if cookie['name'] != 'yanolja_sid']
                else:
                    self.driver.cookies[0]['expires'] = self.wall - 1

                self.assertEqual(self.maintain()['state'], 'renewed')

                self.assertEqual(self.saved_sid()['value'], 'renewed-private-sid-1')
                self.assertGreater(self.saved_sid()['expires'], self.wall)

    def test_temporary_transport_error_waits_one_check_interval_then_retries(self):
        # Transient browser failures must neither busy-loop nor latch auth failure.
        self.attach()
        self.driver.fail_cookie_read = True
        with self.assertRaises(ApiError):
            self.maintain()
        self.advance(299)
        self.assertEqual(self.maintain()['state'], 'waiting')
        self.assertFalse(self.driver.visited)
        self.advance(1)

        self.assertEqual(self.maintain()['state'], 'renewed')

        self.assertGreater(self.saved_sid()['expires'], self.wall)

    def test_exhausted_auth_preserves_file_and_waits_for_successful_manual_save(self):
        # An unavailable SSO session must not overwrite the last good saved file.
        self.attach()
        original = json.dumps(self.driver.cookies)
        self.session.cookie_file.write_text(original, encoding='utf-8')
        self.driver.allow_auth = False
        self.driver.logged = False
        with patch.object(self.session, '_try_kakao_login', return_value=False), \
             self.assertRaises(LoginRequired):
            self.maintain()
        self.assertEqual(self.session.cookie_file.read_text(encoding='utf-8'), original)
        visited = list(self.driver.visited)
        self.advance(3600)
        self.assertEqual(self.maintain()['state'], 'blocked')
        self.assertEqual(self.driver.visited, visited)

        self.driver.allow_auth = True
        self.driver.logged = True
        self.session.save_login_session(DEFAULT_URL)
        after_manual_save = list(self.driver.visited)
        self.assertEqual(self.maintain()['state'], 'checked')
        self.assertEqual(self.driver.visited, after_manual_save)

    def test_successful_save_records_auth_age_for_scheduled_idle_renewal(self):
        # Manual saving must reset the renewal age instead of causing immediate SSO.
        self.attach()
        self.session.save_session()
        self.assertEqual(self.maintain()['state'], 'checked')
        self.assertFalse(self.driver.visited)
        self.advance(1799)
        self.assertEqual(self.maintain()['state'], 'checked')
        self.assertFalse(self.driver.visited)
        self.advance(300)
        self.assertEqual(self.maintain()['state'], 'renewed')
        self.assertEqual(self.saved_sid()['value'], 'renewed-private-sid-1')

    def test_recent_successful_account_refresh_does_not_repeat_sso_on_idle_check(self):
        # Header capture may fail after SSO; that successful auth still resets age.
        self.attach()
        self.assertTrue(self.session._refresh_session(DEFAULT_URL))
        visited = list(self.driver.visited)

        self.assertEqual(self.maintain()['state'], 'checked')

        self.assertEqual(self.driver.visited, visited)
        self.assertEqual(self.driver.renewals, 1)

    def test_idle_renewal_keeps_saved_login_valid_beyond_original_three_hour_expiry(self):
        # The original idle-logout bug appears only after the initial SID expires.
        self.attach()
        self.maintain()
        original_expiry = self.saved_sid()['expires']
        for _ in range(8):
            self.advance(1800)
            self.assertEqual(self.maintain()['state'], 'renewed')
            self.assertGreater(self.saved_sid()['expires'], self.wall)

        self.assertGreater(self.wall, original_expiry)
        self.assertEqual(self.saved_sid()['value'], 'renewed-private-sid-9')

    def test_keepalive_diagnostics_include_cookie_state_without_private_data(self):
        # Useful idle diagnostics must retain the existing auth log privacy contract.
        self.attach()
        self.advance(60)
        self.maintain()
        diagnostic = '\n'.join(self.logs)
        self.assertIn('flag=KEEPALIVE_CHECK', diagnostic)
        self.assertIn('trigger=keepalive', diagnostic)
        self.assertIn('sid_state=valid', diagnostic)
        self.assertIn('sid_remaining_s=', diagnostic)
        for private in ('original-private-sid', 'renewed-private-sid',
                        'private-kakao-auth', 'https://', DEFAULT_URL):
            self.assertNotIn(private, diagnostic)


if __name__ == '__main__':
    unittest.main()
