"""Passive session recovery and Kakao fallback share one bounded offline path."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from api import LoginRequired
from browser import BrowserSession
from gui import DEFAULT_URL
from test_browser import LoginDriver


class KakaoRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.session = self.new_session("default")

    def new_session(self, name):
        base = Path(self.folder.name) / name / "hwadam_codex"
        base.mkdir(parents=True)
        session = BrowserSession(base)
        session.driver = LoginDriver()
        session.tab = "own-tab"
        return session

    def ensure(self, *, manual=False):
        ensure = getattr(self.session, "_ensure_login", None)
        self.assertTrue(callable(ensure), "BrowserSession needs shared _ensure_login recovery")
        return ensure(DEFAULT_URL, manual=manual)

    @staticmethod
    def authenticate(session):
        # Only the external Kakao UI is replaced; session checks and saving stay real.
        session.driver.logged = True
        session.driver.cookies = [{"name": "yanolja_sid", "value": "OFFLINE_PRIVATE_SID",
            "domain": ".yanolja.com", "path": "/", "expires": time.time() + 3600}]
        return True

    def test_live_authentication_succeeds_without_kakao_even_if_previously_blocked(self):
        # A previous manual-required state must not override newly valid passive auth.
        self.session.driver.logged = True
        self.session.session_maintenance_blocked = True
        with patch.object(self.session, "_try_kakao_login",
                          side_effect=AssertionError("valid live login must bypass Kakao UI")):
            self.assertIs(self.ensure(), True)

        self.assertFalse(self.session.session_maintenance_blocked)
        self.assertGreater(self.session.session_authenticated_at, 0)
        self.assertFalse(self.session.cookie_file.exists())

    def test_new_passive_failure_still_gets_one_kakao_attempt_and_resets_auth_age(self):
        # Passive recovery latches blocked=True on failure; the pre-attempt state matters.
        attempts = []

        def kakao(url):
            self.assertTrue(self.session.session_maintenance_blocked)
            self.assertTrue(self.session.driver.current_url.startswith("https://accounts.yanolja.com?"))
            attempts.append(url)
            return self.authenticate(self.session)

        with patch.object(self.session, "_try_kakao_login", side_effect=kakao):
            self.assertIs(self.ensure(), True)

        self.assertEqual(attempts, [DEFAULT_URL])
        self.assertFalse(self.session.session_maintenance_blocked)
        self.assertGreater(self.session.session_authenticated_at, 0)
        self.assertGreater(self.session.session_authenticated_wall_at, 0)

    def test_existing_manual_required_state_does_not_repeat_kakao_fallback(self):
        self.session.session_maintenance_blocked = True
        with patch.object(self.session, "_try_kakao_login",
                          side_effect=AssertionError("blocked automatic recovery must not reopen Kakao")):
            self.assertIs(self.ensure(), False)
            self.assertIs(self.ensure(), False)

        self.assertTrue(self.session.session_maintenance_blocked)
        self.assertEqual(self.session.session_authenticated_at, 0)

    def test_manual_retry_can_recover_a_previously_blocked_session(self):
        self.session.session_maintenance_blocked = True
        attempts = []

        def kakao(url):
            attempts.append(url)
            return self.authenticate(self.session)

        with patch.object(self.session, "_try_kakao_login", side_effect=kakao):
            self.assertIs(self.ensure(manual=True), True)

        self.assertEqual(attempts, [DEFAULT_URL])
        self.assertFalse(self.session.session_maintenance_blocked)
        self.assertGreater(self.session.session_authenticated_at, 0)

    def test_failed_kakao_recovery_preserves_saved_cookies_and_blocks_repeated_ui(self):
        original = json.dumps([{"saved": "OFFLINE_PRIVATE_PREVIOUS_SESSION"}])
        self.session.cookie_file.write_text(original, encoding="utf-8")
        attempts = []

        def kakao(url):
            attempts.append(url)
            return False

        with patch.object(self.session, "_try_kakao_login", side_effect=kakao):
            self.assertIs(self.ensure(), False)
            self.assertIs(self.ensure(), False)

        self.assertEqual(attempts, [DEFAULT_URL])
        self.assertTrue(self.session.session_maintenance_blocked)
        self.assertEqual(self.session.session_authenticated_at, 0)
        self.assertEqual(self.session.cookie_file.read_text(encoding="utf-8"), original)

    def test_login_connect_and_idle_maintenance_use_shared_recovery_then_save(self):
        cases = (("open_login", True), ("connect", False), ("maintain_session", False))
        for entrypoint, want_manual in cases:
            with self.subTest(entrypoint=entrypoint):
                session = self.new_session(entrypoint)
                attempts = []

                def ensure(url, *, manual=False):
                    attempts.append((url, manual))
                    return self.authenticate(session)

                # create=True makes the missing integration fail through real public behavior.
                with patch.object(session, "_ensure_login", side_effect=ensure, create=True), \
                     patch.object(session, "_try_kakao_login", return_value=False):
                    try:
                        result = getattr(session, entrypoint)(DEFAULT_URL)
                    except LoginRequired:
                        self.fail(entrypoint + " bypassed successful shared authentication")

                self.assertEqual(attempts, [(DEFAULT_URL, want_manual)])
                if entrypoint == "open_login":
                    self.assertIs(result, True)
                elif entrypoint == "maintain_session":
                    self.assertEqual(result["state"], "renewed")
                else:
                    self.assertEqual(session.ready_url, DEFAULT_URL)
                    self.assertTrue(session.headers)
                saved = json.loads(session.cookie_file.read_text(encoding="utf-8"))
                self.assertIn("yanolja_sid", {cookie["name"] for cookie in saved})


if __name__ == "__main__":
    unittest.main()
