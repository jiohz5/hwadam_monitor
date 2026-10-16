"""Saved Kakao account login, exercised through an offline WebDriver boundary."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from selenium.common.exceptions import NoSuchWindowException, WebDriverException

from api import LoginRequired
from browser import BrowserSession
from gui import DEFAULT_URL


class FakeElement:
    def __init__(self, text='', *, click=None, saved=False, visible=True, enabled=True):
        self.text = text
        self._click = click
        self.saved = saved
        self.visible = visible
        self.enabled = enabled
        self.clicks = 0

    def is_displayed(self): return self.visible
    def is_enabled(self): return self.enabled

    def get_attribute(self, name):
        if name == 'value':
            raise AssertionError('Login automation must not read credentials')
        return 'false' if name == 'aria-disabled' else None

    def find_elements(self, by, query):
        if query == '.info_profile':
            return [FakeElement('SECRET_ACCOUNT@example.invalid')] if self.saved else []
        return []

    def click(self):
        self.clicks += 1
        if self._click:
            self._click()


class KakaoDriver:
    def __init__(self, *, accounts=1, popup=False, challenge=False,
                 authenticate=True, timeout=False, bad_host=None, fail_click=None,
                 direct=False, extra_popup=False):
        self.current_window_handle = 'main'
        self.windows = {'main': ('account', 'https://accounts.yanolja.com/')}
        self.popup = popup
        self.challenge = challenge
        self.authenticate = authenticate
        self.timeout = timeout
        self.bad_host = bad_host
        self.fail_click = fail_click
        self.direct = direct
        self.extra_popup = extra_popup
        self.logged = False
        self.navigations = []
        self.selections = []
        self.product_checks = 0
        self.cookies = [dict(name='yanolja_sid', value='SECRET_COOKIE_VALUE',
                             domain='.yanolja.com', path='/', expires=-1, session=True)]
        self.start = FakeElement('카카오로 시작하기', click=self._start)
        self.accounts = [FakeElement('SECRET_ACCOUNT@example.invalid',
                                    saved=True, click=self._choose) for _ in range(accounts)]
        self.new_account = FakeElement('직접 입력해서 로그인', click=self._unexpected_click)
        self.challenge_input = FakeElement('')
        self.switch_to = SimpleNamespace(window=self._switch)

    @property
    def window_handles(self): return list(self.windows)

    @property
    def current_url(self):
        if self.current_window_handle not in self.windows:
            raise NoSuchWindowException('closed fake popup')
        return self.windows[self.current_window_handle][1]

    @property
    def stage(self):
        if self.current_window_handle not in self.windows:
            raise NoSuchWindowException('closed fake popup')
        return self.windows[self.current_window_handle][0]

    def _switch(self, handle):
        if handle not in self.windows:
            raise NoSuchWindowException('closed fake popup')
        self.selections.append(handle)
        self.current_window_handle = handle

    def _start(self):
        if self.fail_click == 'start':
            raise WebDriverException('SECRET_EXCEPTION https://auth.invalid/?token=SECRET_TOKEN')
        if self.timeout:
            return
        if self.direct:
            self.logged = self.authenticate
            self.windows[self.current_window_handle] = ('product', DEFAULT_URL)
            return
        url = self.bad_host or 'https://accounts.kakao.com/login/simple/?token=SECRET_TOKEN'
        stage = 'challenge' if self.challenge else 'profiles'
        if self.popup:
            self.windows['auth-popup'] = (stage, url)
            if self.extra_popup:
                self.windows['other-auth-popup'] = (stage, url)
        else:
            self.windows[self.current_window_handle] = (stage, url)

    def _choose(self):
        if self.fail_click == 'account':
            raise WebDriverException('SECRET_EXCEPTION https://auth.invalid/?token=SECRET_TOKEN')
        self.logged = self.authenticate
        if self.popup:
            del self.windows['auth-popup']
        self.windows['main'] = ('product', DEFAULT_URL)

    def _unexpected_click(self):
        raise AssertionError('New-account login must never be clicked')

    def get(self, url):
        self.navigations.append(url)
        if url.startswith('https://leisure-web.yanolja.com/leisure/'):
            self.windows[self.current_window_handle] = ('product', url)
        elif url.startswith('https://accounts.yanolja.com'):
            self.windows[self.current_window_handle] = ('account', url)
        else:
            raise AssertionError('Unexpected navigation outside fake auth flow')

    def find_elements(self, by, query):
        if '카카오로 시작하기' in query:
            return [self.start] if self.stage == 'account' else []
        if query == "a.wrap_profile[role='button']":
            return self.accounts + [self.new_account] if self.stage == 'profiles' else []
        if '로그인할 카카오계정 선택' in query:
            return [FakeElement('로그인할 카카오계정 선택')] if self.stage == 'profiles' else []
        if 'password' in query or 'one-time-code' in query:
            return [self.challenge_input] if self.stage == 'challenge' else []
        return []

    def execute_script(self, script, *args):
        if '__NEXT_DATA__' in script:
            self.product_checks += 1
            return self.logged if self.stage == 'product' else None
        if 'scrollIntoView' in script:
            return None
        raise AssertionError('Unexpected browser script in offline login test')

    def execute_cdp_cmd(self, command, values):
        if command == 'Network.getAllCookies': return {'cookies': self.cookies}
        if command in ('Emulation.setFocusEmulationEnabled', 'Page.addScriptToEvaluateOnNewDocument'):
            return {}
        raise AssertionError('Unexpected CDP operation in offline login test: ' + command)


class KakaoLoginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.logs = []
        self.session = BrowserSession(Path(self.temp.name), self.logs.append)
        self.session.tab = 'main'
        self.elapsed = 0.0
        self.addCleanup(patch.stopall)
        patch('browser.time.monotonic', side_effect=self._clock).start()
        patch('browser.time.sleep', side_effect=self._sleep).start()

    def _clock(self):
        self.elapsed += 0.02
        return self.elapsed

    def _sleep(self, seconds): self.elapsed += seconds

    def driver(self, **kwargs):
        self.session.driver = KakaoDriver(**kwargs)
        return self.session.driver

    def open_login(self):
        with patch.object(self.session, '_recover_login', return_value=False):
            return self.session.open_login(DEFAULT_URL)

    def assert_private_logs(self):
        text = '\n'.join(self.logs)
        for secret in ('SECRET_ACCOUNT', 'SECRET_COOKIE', 'SECRET_TOKEN', 'SECRET_EXCEPTION',
                       'https://', '@example.invalid'):
            self.assertNotIn(secret, text)

    def test_open_login_clicks_saved_account_and_saves_only_verified_session(self):
        driver = self.driver()
        self.assertIs(self.open_login(), True)
        self.assertEqual(driver.start.clicks, 1)
        self.assertEqual(driver.accounts[0].clicks, 1)
        self.assertEqual(driver.new_account.clicks, 0)
        self.assertGreater(driver.product_checks, 0)
        self.assertEqual(driver.current_url, DEFAULT_URL)
        saved = json.loads(self.session.cookie_file.read_text(encoding='utf-8'))
        self.assertEqual(saved[0]['name'], 'yanolja_sid')
        for flag in ('KAKAO_LOGIN_BEGIN', 'KAKAO_START_CLICKED',
                     'KAKAO_ACCOUNT_CLICKED', 'KAKAO_LOGIN_READY', 'COOKIE_SAVED'):
            self.assertEqual(sum('flag=' + flag + ']' in line for line in self.logs), 1)
        self.assert_private_logs()

    def test_existing_authentication_bypasses_click_flow(self):
        driver = self.driver()
        driver.logged = True
        driver.windows['main'] = ('product', DEFAULT_URL)
        with patch.object(self.session, '_recover_login', return_value=True), \
             patch.object(self.session, '_try_kakao_login') as click_flow:
            self.assertIs(self.session.open_login(DEFAULT_URL), True)
        click_flow.assert_not_called()
        self.assertEqual(driver.start.clicks, 0)
        self.assertTrue(self.session.cookie_file.is_file())

    def test_existing_kakao_account_can_redirect_without_account_selection(self):
        driver = self.driver(direct=True)
        self.assertIs(self.open_login(), True)
        self.assertEqual(driver.start.clicks, 1)
        self.assertEqual(driver.accounts[0].clicks, 0)
        self.assertGreater(driver.product_checks, 0)

    def test_multiple_saved_accounts_are_left_for_user_selection(self):
        driver = self.driver(accounts=2)
        self.assertIs(self.open_login(), False)
        self.assertEqual(driver.start.clicks, 1)
        self.assertEqual([account.clicks for account in driver.accounts], [0, 0])
        self.assertEqual(driver.new_account.clicks, 0)
        self.assertFalse(self.session.cookie_file.exists())
        self.assert_private_logs()

    def test_no_saved_account_does_not_click_new_account_action(self):
        driver = self.driver(accounts=0)
        self.assertIs(self.open_login(), False)
        self.assertEqual(driver.new_account.clicks, 0)
        self.assertFalse(self.session.cookie_file.exists())

    def test_additional_authentication_is_left_for_user_without_credential_reads(self):
        driver = self.driver(challenge=True)
        self.assertIs(self.open_login(), False)
        self.assertEqual(driver.accounts[0].clicks, 0)
        self.assertEqual(driver.challenge_input.clicks, 0)
        self.assertFalse(self.session.cookie_file.exists())
        self.assert_private_logs()

    def test_timeout_is_bounded_and_start_button_is_not_clicked_repeatedly(self):
        driver = self.driver(timeout=True)
        self.assertIs(self.session._try_kakao_login(DEFAULT_URL, timeout=1), False)
        self.assertEqual(driver.start.clicks, 1)
        self.assertLess(self.elapsed, 3)
        self.assertFalse(self.session.cookie_file.exists())

    def test_lookalike_and_non_https_auth_pages_are_not_clicked(self):
        for url in ('https://accounts.kakao.com.example.invalid/login/simple/',
                    'http://accounts.kakao.com/login/simple/',
                    'https://unknown.example.invalid/login/simple/'):
            with self.subTest(url=url):
                driver = self.driver(bad_host=url)
                self.assertIs(self.open_login(), False)
                self.assertEqual(driver.accounts[0].clicks, 0)
                self.assertFalse(self.session.cookie_file.exists())
                self.assert_private_logs()

    def test_click_errors_are_private_and_preserve_saved_file(self):
        original = '[{"saved":"keep"}]'
        self.session.cookie_file.write_text(original, encoding='utf-8')
        for failing_step in ('start', 'account'):
            with self.subTest(failing_step=failing_step):
                driver = self.driver(fail_click=failing_step)
                self.assertIs(self.open_login(), False)
                self.assertEqual(self.session.cookie_file.read_text(encoding='utf-8'), original)
                self.assert_private_logs()
                if failing_step == 'start':
                    self.assertEqual(driver.accounts[0].clicks, 0)

    def test_account_click_without_authenticated_product_does_not_save(self):
        driver = self.driver(authenticate=False)
        original = '[{"saved":"keep"}]'
        self.session.cookie_file.write_text(original, encoding='utf-8')
        self.assertIs(self.open_login(), False)
        self.assertEqual(driver.accounts[0].clicks, 1)
        self.assertGreater(driver.product_checks, 0)
        self.assertEqual(self.session.cookie_file.read_text(encoding='utf-8'), original)
        self.assert_private_logs()

    def test_authentication_lost_before_save_keeps_old_cookie_file(self):
        self.driver()
        original = '[{"saved":"keep"}]'
        self.session.cookie_file.write_text(original, encoding='utf-8')
        with patch.object(self.session.driver, 'execute_script', side_effect=[True, False]):
            with self.assertRaises(LoginRequired):
                self.open_login()
        self.assertEqual(self.session.cookie_file.read_text(encoding='utf-8'), original)
        self.assertTrue(self.session.session_maintenance_blocked)
        self.assert_private_logs()

    def test_new_auth_popup_is_followed_and_closed_popup_returns_to_product_tab(self):
        driver = self.driver(popup=True)
        driver.windows['old-unrelated-tab'] = ('profiles', 'https://unrelated.example.invalid/')
        self.assertIs(self.open_login(), True)
        self.assertEqual(driver.accounts[0].clicks, 1)
        self.assertIn('auth-popup', driver.selections)
        self.assertNotIn('old-unrelated-tab', driver.selections)
        self.assertEqual(driver.current_window_handle, 'main')
        self.assertEqual(self.session.tab, 'main')
        self.assertTrue(self.session.cookie_file.is_file())

    def test_multiple_new_auth_popups_are_left_for_user(self):
        driver = self.driver(popup=True, extra_popup=True)
        self.assertIs(self.open_login(), False)
        self.assertEqual(driver.accounts[0].clicks, 0)
        self.assertFalse(self.session.cookie_file.exists())

    def test_auth_popup_can_stay_open_after_original_tab_authenticates(self):
        class KeptPopupDriver(KakaoDriver):
            def _choose(self):
                self.logged = True
                self.windows['main'] = ('product', DEFAULT_URL)
                self.windows['auth-popup'] = ('callback', 'https://kauth.kakao.com/callback')
        driver = self.session.driver = KeptPopupDriver(popup=True)
        self.assertIs(self.open_login(), True)
        self.assertEqual(driver.accounts[0].clicks, 1)
        self.assertEqual(driver.current_window_handle, 'main')
        self.assertEqual(self.session.tab, 'main')
        self.assertIn('auth-popup', driver.window_handles)
        self.assertTrue(self.session.cookie_file.is_file())

    def test_hidden_or_disabled_start_button_is_not_clicked(self):
        for attribute in ('visible', 'enabled'):
            with self.subTest(attribute=attribute):
                driver = self.driver()
                setattr(driver.start, attribute, False)
                self.assertIs(self.session._try_kakao_login(DEFAULT_URL, timeout=1), False)
                self.assertEqual(driver.start.clicks, 0)

    def test_hidden_saved_account_is_not_automatically_selected(self):
        driver = self.driver()
        driver.accounts[0].visible = False
        self.assertIs(self.open_login(), False)
        self.assertEqual(driver.accounts[0].clicks, 0)
        self.assertFalse(self.session.cookie_file.exists())


if __name__ == '__main__':
    unittest.main()
