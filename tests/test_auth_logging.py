"""인증 로그의 진단 정보와 비밀정보 비노출 계약을 공개 경로에서 검증한다."""
import json
from pathlib import Path
import re
import tempfile
import time
import unittest
from unittest.mock import patch

from selenium.common.exceptions import NoSuchWindowException

from api import ApiError, LoginRequired
from browser import BrowserSession
from gui import DEFAULT_URL
from test_browser import LoginDriver


class ResponseDriver(LoginDriver):
    """실제 브라우저의 XHR 경계만 정해진 응답으로 대체한다."""

    def __init__(self, response):
        super().__init__(logged=True)
        self.response = response

    def execute_async_script(self, script, *args):
        return self.response


class AuthLoggingTests(unittest.TestCase):
    AUTH_LINE = re.compile(r"^\[AUTH trace=([0-9a-f]{8}) flag=([A-Z_]+)\](?: (.*))?$")

    def make_session(self, folder, driver):
        base = Path(folder) / "hwadam_codex"
        base.mkdir(exist_ok=True)
        logs = []
        session = BrowserSession(base, logs.append)
        session.driver = driver
        session.tab = "own-tab"
        return session, logs

    def auth_events(self, logs):
        events = []
        for line in logs:
            if not line.startswith("[AUTH"):
                continue
            match = self.AUTH_LINE.fullmatch(line)
            self.assertIsNotNone(match, "AUTH 로그는 추적 ID와 플래그를 가진 정보계약을 따라야 한다")
            trace, flag, raw_metadata = match.groups()
            metadata = {}
            for item in (raw_metadata or "").split():
                self.assertRegex(item, r"^[a-z_]+=[A-Za-z0-9_.:-]+$")
                key, value = item.split("=", 1)
                self.assertNotIn(key, metadata)
                metadata[key] = value
            events.append({"trace": trace, "flag": flag, "metadata": metadata})
        return events

    def event(self, logs, flag, **metadata):
        matches = [event for event in self.auth_events(logs)
                   if event["flag"] == flag
                   and all(event["metadata"].get(key) == str(value)
                           for key, value in metadata.items())]
        self.assertTrue(matches, f"필요한 AUTH 이벤트 없음: {flag} {metadata}")
        return matches[-1]

    def assert_no_secrets(self, logs, *secrets):
        text = "\n".join(logs)
        for secret in secrets:
            self.assertNotIn(secret, text)
        self.assertNotIn("https://", text)
        self.assertNotIn("http://", text)

    def test_expired_saved_sid_records_recovery_exhaustion_without_overwriting_file(self):
        # 만료 쿠키를 조용히 건너뛰거나 마지막 복원 실패를 숨기면 이 계약이 깨진다.
        cookies = [
            {"name": "yanolja_sid", "value": "SECRET_EXPIRED_SID",
             "domain": ".yanolja.com", "path": "/", "expires": time.time() - 3600},
            {"name": "analytics-only", "value": "SECRET_ANALYTICS_COOKIE",
             "domain": ".yanolja.com", "path": "/", "expires": time.time() + 3600},
        ]
        with tempfile.TemporaryDirectory() as folder:
            session, logs = self.make_session(folder, LoginDriver())
            original = json.dumps(cookies)
            session.cookie_file.write_text(original, encoding="utf-8")

            with self.assertRaises(LoginRequired):
                session.connect(DEFAULT_URL)

            self.assertEqual(session.cookie_file.read_text(encoding="utf-8"), original)
            self.event(logs, "AUTH_BEGIN", trigger="connect")
            self.event(logs, "SESSION_REFRESH_RESULT", source="live", logged_in=0)
            self.event(logs, "COOKIE_RESTORE_RESULT", source="saved", restored=1,
                       expired=1, rejected=0, sid_state="expired")
            self.event(logs, "SESSION_REFRESH_RESULT", source="saved", logged_in=0)
            self.event(logs, "COOKIE_FILE_MISSING", source="reference")
            self.event(logs, "LOGIN_REQUIRED", reason="recovery_exhausted")
            self.assertEqual(len({event["trace"] for event in self.auth_events(logs)}), 1)
            self.assert_no_secrets(logs, "SECRET_EXPIRED_SID", "SECRET_ANALYTICS_COOKIE")

    def test_live_login_reuse_logs_ready_and_cookie_counts_without_values(self):
        cookies = [
            {"name": "yanolja_sid", "value": "SECRET_LIVE_SID", "domain": ".yanolja.com",
             "path": "/", "expires": time.time() + 3600},
            {"name": "nol-account", "value": "SECRET_NOL_ACCOUNT",
             "domain": "accounts.yanolja.com", "path": "/", "session": True},
            {"name": "kakao-test-auth", "value": "SECRET_KAKAO_ACCOUNT",
             "domain": ".accounts.kakao.com", "path": "/", "session": True},
            {"name": "tracking", "value": "SECRET_TRACKING",
             "domain": "tiara.kakao.com", "path": "/", "session": True},
        ]
        with tempfile.TemporaryDirectory() as folder:
            session, logs = self.make_session(folder, LoginDriver(logged=True, cookies=cookies))

            self.assertIs(session.open_login(DEFAULT_URL), True)

            self.assertTrue(session.cookie_file.is_file())
            self.event(logs, "AUTH_BEGIN", trigger="open_login")
            self.event(logs, "SESSION_REFRESH_RESULT", source="live", logged_in=1)
            self.event(logs, "AUTH_READY", source="live")
            self.event(logs, "COOKIE_SAVED", nol_cookies=2, kakao_cookies=1)
            self.assertEqual(len({event["trace"] for event in self.auth_events(logs)}), 1)
            self.assert_no_secrets(logs, "SECRET_LIVE_SID", "SECRET_NOL_ACCOUNT",
                                   "SECRET_KAKAO_ACCOUNT", "SECRET_TRACKING", "fresh-test-sid")

    def test_invalid_saved_file_is_identified_and_preserved(self):
        for original in ("{SECRET_INVALID_JSON", '{"SECRET_WRONG_SHAPE": true}'):
            with self.subTest(original=original), tempfile.TemporaryDirectory() as folder:
                session, logs = self.make_session(folder, LoginDriver())
                session.cookie_file.write_text(original, encoding="utf-8")

                self.assertIs(session.open_login(DEFAULT_URL), False)

                self.assertEqual(session.cookie_file.read_text(encoding="utf-8"), original)
                self.event(logs, "COOKIE_FILE_INVALID", source="saved")
                self.event(logs, "COOKIE_FILE_MISSING", source="reference")
                self.event(logs, "LOGIN_REQUIRED", reason="recovery_exhausted")
                self.assert_no_secrets(logs, "SECRET_INVALID_JSON", "SECRET_WRONG_SHAPE")

    def test_api_auth_rejections_log_only_safe_status_code_and_endpoint(self):
        cases = [
            (401, "unknown-private-code", "/product/v1/products/10367229/option-groups/123/rounds?token=SECRET_QUERY", "rounds", "none"),
            (200, "leisure-web-api-0013", "/product/v1/products/10367229/option-groups/123/schedules?startDate=SECRET_QUERY", "schedules", "leisure-web-api-0013"),
            (200, "leisure-web-api-0015", "/product/v1/products/10367229", "product", "leisure-web-api-0015"),
            (200, "leisure-web-api-1802", "/order/v1/orders/group/SECRET_ORDER_ID/additional-purchase/verification", "addon", "leisure-web-api-1802"),
            (200, "leisure-web-api-1501", "/product/v1/private-endpoint?token=SECRET_QUERY", "other", "leisure-web-api-1501"),
        ]
        for status, code, path, endpoint, safe_code in cases:
            with self.subTest(status=status, endpoint=endpoint), tempfile.TemporaryDirectory() as folder:
                response = {"status": status, "body": json.dumps({"body": {
                    "code": code, "message": "SECRET_RESPONSE_BODY", "accessToken": "SECRET_RESPONSE_TOKEN"}})}
                session, logs = self.make_session(folder, ResponseDriver(response))
                session.connect(DEFAULT_URL)
                session.headers = {"Access-Token": "SECRET_HEADER_TOKEN", "Cookie": "SECRET_HEADER_COOKIE"}
                kwargs = {"method": "POST", "data": {"productId": "SECRET_REQUEST_DATA"}} if endpoint == "addon" else {}

                self.assertEqual(session.request(path, **kwargs), response)

                self.event(logs, "API_AUTH_REJECTED", http=status, code=safe_code, endpoint=endpoint)
                self.assert_no_secrets(logs, "SECRET_QUERY", "SECRET_ORDER_ID", "SECRET_RESPONSE_BODY",
                                       "SECRET_RESPONSE_TOKEN", "SECRET_HEADER_TOKEN", "SECRET_HEADER_COOKIE",
                                       "SECRET_REQUEST_DATA", "unknown-private-code", path)

    def test_no_such_window_logs_exception_type_and_previous_flag_without_message(self):
        class LostWindowDriver(ResponseDriver):
            def execute_async_script(self, script, *args):
                raise NoSuchWindowException("SECRET_EXCEPTION_MESSAGE https://private.example/?token=SECRET_TOKEN")

        with tempfile.TemporaryDirectory() as folder:
            session, logs = self.make_session(folder, LostWindowDriver({"status": 200, "body": "{}"}))
            session.connect(DEFAULT_URL)

            with self.assertRaises(ApiError):
                session.request("/product/v1/products/10367229")

            self.assertIsNone(session.ready_url)
            self.event(logs, "BROWSER_CONNECTION_LOST")
            error = self.event(logs, "AUTH_ERROR", error_type="NoSuchWindowException")
            events = self.auth_events(logs)
            error_index = events.index(error)
            self.assertGreater(error_index, 0)
            self.assertEqual(error["metadata"].get("last_flag"), events[error_index - 1]["flag"])
            self.assert_no_secrets(logs, "SECRET_EXCEPTION_MESSAGE", "SECRET_TOKEN")

    def test_cached_connect_and_successful_requests_do_not_add_auth_logs_per_poll(self):
        response = {"status": 200, "body": '{"body": []}'}
        with tempfile.TemporaryDirectory() as folder, patch("browser.time.monotonic", return_value=1000):
            session, logs = self.make_session(folder, ResponseDriver(response))
            session.connect(DEFAULT_URL)
            self.event(logs, "AUTH_READY", source="live")
            initial_events = self.auth_events(logs)

            for _ in range(5):
                session.connect(DEFAULT_URL)
                self.assertEqual(session.request("/product/v1/products/10367229/option-groups/123/rounds"), response)

            self.assertEqual(self.auth_events(logs), initial_events)

    def test_browser_auth_exception_is_safe_for_the_gui_to_log(self):
        class BrokenNavigationDriver(LoginDriver):
            def get(self, url):
                raise NoSuchWindowException('SECRET_EXCEPTION https://private.example/?token=SECRET_TOKEN')

        with tempfile.TemporaryDirectory() as folder:
            session, logs = self.make_session(folder, BrokenNavigationDriver())
            with self.assertRaises(Exception) as caught:
                session.open_login(DEFAULT_URL)
            self.event(logs, 'AUTH_ERROR', error_type='NoSuchWindowException')
            self.assertIsInstance(caught.exception, ApiError)
            self.assert_no_secrets([*logs, str(caught.exception)], 'SECRET_EXCEPTION', 'SECRET_TOKEN')

    def test_invalid_cookie_expiry_cannot_break_diagnostic_logging(self):
        for expiry in (float('nan'), float('inf')):
            with self.subTest(expiry=expiry), tempfile.TemporaryDirectory() as folder:
                session, logs = self.make_session(folder, LoginDriver())
                original = json.dumps([{'name':'yanolja_sid','value':'SECRET_COOKIE',
                    'domain':'.yanolja.com','path':'/','expires':expiry}])
                session.cookie_file.write_text(original, encoding='utf-8')
                try:
                    result = session.open_login(DEFAULT_URL)
                except Exception as error:
                    self.fail('잘못된 쿠키 만료 정보가 진단·복원 흐름을 중단함: '+type(error).__name__)
                self.assertIs(result, False)
                self.event(logs, 'COOKIE_RESTORE_RESULT', source='saved', restored=0, rejected=1)
                self.assertEqual(session.cookie_file.read_text(encoding='utf-8'), original)
                self.assert_no_secrets(logs, 'SECRET_COOKIE')

    def test_product_read_browser_error_is_safe_for_the_gui_to_log(self):
        with tempfile.TemporaryDirectory() as folder:
            session, logs = self.make_session(folder, LoginDriver(logged=True))
            session.connect(DEFAULT_URL)
            def lost_window(handle):
                raise NoSuchWindowException('SECRET_PRODUCT_COOKIE https://private.example/')
            session.driver.switch_to.window = lost_window
            with self.assertRaises(Exception) as caught:
                session.product()
            self.assertIsInstance(caught.exception, ApiError)
            self.event(logs, 'AUTH_ERROR', error_type='NoSuchWindowException')
            self.assert_no_secrets([*logs, str(caught.exception)], 'SECRET_PRODUCT_COOKIE')


if __name__ == "__main__":
    unittest.main()
