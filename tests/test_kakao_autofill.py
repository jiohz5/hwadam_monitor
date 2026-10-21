"""Saved-password login through a fake browser, without accessing credentials."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from selenium.common.exceptions import StaleElementReferenceException

from browser import BrowserSession
from gui import DEFAULT_URL
from test_kakao_login import FakeElement, KakaoDriver


class PasswordElement(FakeElement):
    def __init__(self, driver):
        super().__init__(click=lambda: driver.actions.append('focus_password'))
        self.driver = driver

    def send_keys(self, *keys):
        raise AssertionError('Ordinary WebDriver keys do not reach the native popup')


class StayElement(FakeElement):
    def __init__(self, driver):
        super().__init__()
        self.driver = driver

    def is_selected(self):
        if self.driver.stale_keep and self.clicks:
            self.driver.stale_keep = False
            raise StaleElementReferenceException('fake checkbox rerender')
        return self.driver.keep_signed_in

    def click(self):
        super().click()
        self.driver.actions.append('keep_signed_in')
        if self.driver.keep_works:
            self.driver.keep_signed_in = not self.driver.keep_signed_in


class PasswordDriver(KakaoDriver):
    def __init__(self, *, autofill=True, filled=False, kept=False,
                 keep_works=True, accept=True, otp=False, password_host=None,
                 duplicate_password=False, duplicate_submit=False, popup=False,
                 account_present=True, account_changed=False, stale_keep=False,
                 stale_submit=False, stale_submit_click=False):
        super().__init__(popup=popup)
        self.autofill = autofill
        self.password_filled = filled
        self.keep_signed_in = kept
        self.keep_works = keep_works
        self.accept = accept
        self.otp = otp
        self.password_host = password_host or 'https://accounts.kakao.com/login/'
        self.duplicate_password = duplicate_password
        self.duplicate_submit = duplicate_submit
        self.account_present = account_present
        self.account_changed = account_changed
        self.stale_keep = stale_keep
        self.stale_submit = stale_submit
        self.stale_submit_click = stale_submit_click
        self.account_guard = False
        self.actions = []
        self.keys = []
        self.suggestion_selected = False
        self.password = PasswordElement(self)
        self.keep = StayElement(self)
        self.keep_label = FakeElement('로그인 상태 유지', click=self.keep.click)
        self.submit = FakeElement('로그인', click=self._submit)

    def _choose(self):
        self.windows[self.current_window_handle] = ('password', self.password_host)

    def _submit(self):
        if self.stale_submit_click:
            self.stale_submit_click = False
            raise StaleElementReferenceException('fake replaced button rejected before click')
        if not self.password_filled or not self.keep_signed_in:
            raise AssertionError('Submit requires a filled password and stay-signed-in')
        self.actions.append('submit')
        if self.accept:
            self.logged = True
            self.windows['main'] = ('product', DEFAULT_URL)
            if self.popup:
                del self.windows['auth-popup']

    def find_elements(self, by, query):
        if self.stage != 'password':
            return super().find_elements(by, query)
        if 'password' in query:
            fields = [self.password] * (2 if self.duplicate_password else 1)
            return fields + ([FakeElement()] if self.otp and 'one-time-code' in query else [])
        if 'one-time-code' in query:
            return [FakeElement()] if self.otp else []
        if 'label-staySignedIn' in query:
            return [self.keep_label]
        if 'staySignedIn' in query:
            return [self.keep]
        if 'submit' in query and '로그인' in query:
            if self.stale_submit:
                self.stale_submit = False
                raise StaleElementReferenceException('fake submit rerender')
            return [self.submit] * (2 if self.duplicate_submit else 1)
        return []

    def execute_script(self, script, *args):
        if '__hcKakaoAccountUnchanged = ' in script:
            self.account_guard = True
            return self.account_present
        if 'delete ' in script and '__hcKakaoAccountUnchanged' in script:
            self.account_guard = False
            return None
        if '__hcKakaoAccountUnchanged' in script:
            return (self.account_guard and not (self.account_changed and self.suggestion_selected)
                    and (self.password_filled if '.value' in script else True))
        if args and args[0] is self.password and 'Boolean(' in script and '.value' in script:
            # Only a boolean crosses the browser boundary; no password is present in Python.
            return self.password_filled
        return super().execute_script(script, *args)

    def execute_cdp_cmd(self, command, values):
        if command == 'Page.bringToFront': return {}
        if command == 'Input.dispatchKeyEvent':
            key = values.get('key')
            if key not in ('PageUp', 'Tab'):
                raise AssertionError('Only popup navigation and non-submit acceptance are allowed')
            expected = 33 if key == 'PageUp' else 9
            if values.get('nativeVirtualKeyCode') != expected:
                raise AssertionError('Native code is required for browser popup listeners')
            self.keys.append(dict(values))
            if values['type'] == 'rawKeyDown':
                if key == 'PageUp':
                    self.suggestion_selected = True
                elif self.suggestion_selected:
                    self.actions.append('select_saved_password')
                    if self.autofill:
                        self.password_filled = True
            return {}
        return super().execute_cdp_cmd(command, values)


class KakaoAutofillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.logs = []
        self.session = BrowserSession(Path(self.temp.name), self.logs.append)
        self.session.tab = 'main'
        self.elapsed = 0
        clock = patch('browser.time.monotonic', side_effect=self._clock)
        sleep = patch('browser.time.sleep', side_effect=self._sleep)
        clock.start()
        sleep.start()
        self.addCleanup(clock.stop)
        self.addCleanup(sleep.stop)

    def _clock(self):
        self.elapsed += .02
        return self.elapsed

    def _sleep(self, seconds): self.elapsed += seconds

    def run_login(self, **kwargs):
        self.driver = PasswordDriver(**kwargs)
        self.session.driver = self.driver
        with patch.object(self.session, '_recover_login', return_value=False):
            return self.session.open_login(DEFAULT_URL)

    def test_password_focus_selection_keep_and_submit_complete_verified_login(self):
        self.assertTrue(self.run_login())
        self.assertEqual(self.driver.actions,
                         ['focus_password', 'select_saved_password', 'keep_signed_in', 'submit'])
        self.assertEqual(self.driver.submit.clicks, 1)
        self.assertTrue(self.session.cookie_file.is_file())
        self.assertFalse(self.session.session_maintenance_blocked)
        self.assertGreater(self.driver.product_checks, 0)

    def test_already_checked_stay_signed_in_is_not_toggled_off(self):
        self.assertTrue(self.run_login(kept=True))
        self.assertNotIn('keep_signed_in', self.driver.actions)
        self.assertTrue(self.driver.keep_signed_in)

    def test_prefilled_password_needs_no_suggestion_selection(self):
        self.assertTrue(self.run_login(filled=True))
        self.assertEqual(self.driver.keys, [])
        self.assertEqual(self.driver.submit.clicks, 1)

    def test_missing_saved_password_never_submits_and_preserves_cookie_file(self):
        original = '[{"name":"old-session","value":"PRIVATE_OLD_COOKIE"}]'
        self.session.cookie_file.write_text(original, encoding='utf-8')
        self.assertFalse(self.run_login(autofill=False))
        self.assertEqual(self.driver.submit.clicks, 0)
        self.assertEqual(self.session.cookie_file.read_text(encoding='utf-8'), original)
        self.assertTrue(self.session.session_maintenance_blocked)
        self.assertIn('reason=autofill_unavailable', '\n'.join(self.logs))

    def test_failed_keep_checkbox_does_not_submit_or_toggle_repeatedly(self):
        self.assertFalse(self.run_login(keep_works=False))
        self.assertEqual(self.driver.keep.clicks, 1)
        self.assertEqual(self.driver.submit.clicks, 0)

    def test_unconfirmed_login_submits_only_once_and_does_not_save(self):
        self.assertFalse(self.run_login(accept=False))
        self.assertEqual(self.driver.submit.clicks, 1)
        self.assertEqual(self.driver.password.clicks, 1)
        self.assertFalse(self.session.cookie_file.exists())

    def test_otp_is_left_to_user_before_password_or_submit_click(self):
        self.assertFalse(self.run_login(otp=True))
        self.assertEqual(self.driver.actions, [])

    def test_password_selection_handles_auth_popup_return(self):
        self.assertTrue(self.run_login(popup=True))
        self.assertEqual(self.session.tab, 'main')
        self.assertEqual(self.driver.submit.clicks, 1)

    def test_non_kakao_password_form_is_not_used(self):
        for host in ('http://accounts.kakao.com/login/',
                     'https://accounts.kakao.com.example.invalid/login/',
                     'https://accounts.yanolja.com/login/'):
            with self.subTest(host=host):
                self.assertFalse(self.run_login(password_host=host))
                self.assertEqual(self.driver.actions, [])

    def test_ambiguous_password_fields_are_not_clicked(self):
        self.assertFalse(self.run_login(duplicate_password=True))
        self.assertEqual(self.driver.actions, [])

    def test_ambiguous_login_buttons_are_not_submitted(self):
        self.assertFalse(self.run_login(duplicate_submit=True))
        self.assertEqual(self.driver.submit.clicks, 0)

    def test_autofill_for_different_account_is_never_submitted(self):
        self.assertFalse(self.run_login(account_changed=True))
        self.assertTrue(self.driver.password_filled)
        self.assertEqual(self.driver.submit.clicks, 0)
        self.assertIn('reason=autofill_account_changed', '\n'.join(self.logs))

    def test_missing_current_account_does_not_select_arbitrary_saved_account(self):
        self.assertFalse(self.run_login(account_present=False))
        self.assertEqual(self.driver.keys, [])
        self.assertEqual(self.driver.submit.clicks, 0)

    def test_rerender_after_autofill_continues_without_reselecting_password(self):
        for failure in ('stale_keep', 'stale_submit'):
            with self.subTest(failure=failure):
                self.assertTrue(self.run_login(**{failure: True}))
                self.assertEqual(self.driver.actions.count('select_saved_password'), 1)
                self.assertEqual(self.driver.keep.clicks, 1)
                self.assertEqual(self.driver.submit.clicks, 1)
                self.assertFalse(self.driver.account_guard)

    def test_submit_replaced_before_click_can_be_retried_without_duplicate_submission(self):
        self.assertTrue(self.run_login(stale_submit_click=True))
        self.assertEqual(self.driver.submit.clicks, 2)
        self.assertEqual(self.driver.actions.count('submit'), 1)
        self.assertEqual(self.driver.actions.count('select_saved_password'), 1)
        self.assertTrue(self.session.cookie_file.exists())

    def test_diagnostics_report_steps_without_values_or_urls(self):
        self.assertTrue(self.run_login())
        logs = '\n'.join(self.logs)
        for flag in ('KAKAO_PASSWORD_FOCUSED', 'KAKAO_AUTOFILL_READY',
                     'KAKAO_KEEP_SIGNED_IN', 'KAKAO_LOGIN_SUBMITTED', 'KAKAO_LOGIN_READY'):
            self.assertIn('flag=' + flag + ']', logs)
        for secret in ('SECRET_', 'PRIVATE_', 'https://', '@example.invalid'):
            self.assertNotIn(secret, logs)


if __name__ == '__main__':
    unittest.main()
