import json
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from api import ApiError, LoginRequired
from urllib3.exceptions import ProtocolError
from browser import BrowserSession
from model import DEFAULT_URL


class CookieDriver:
    """외부 Edge/CDP 경계만 대체하고 실제 저장·복원 코드는 실행한다."""
    def __init__(self,logged=True):
        self.logged=logged
        self.restored=[]
        self.window_handles=['own-tab']
        self.current_url=DEFAULT_URL
        self.switch_to=SimpleNamespace(window=lambda handle:None)
        self.service=SimpleNamespace(stop=lambda:None)
        self.cookies=[
            {'name':'yanolja_sid','value':'test-session','domain':'.yanolja.com','path':'/',
             'secure':True,'httpOnly':True,'expires':time.time()+3600,'sameSite':'Lax'},
            {'name':'unrelated','value':'excluded','domain':'.example.com','path':'/'}]

    def execute_script(self,script,*args): return self.logged

    def get(self,url): self.current_url=url

    def execute_cdp_cmd(self,command,values):
        if command=='Emulation.setFocusEmulationEnabled': return {}
        if command=='Network.getAllCookies': return {'cookies':self.cookies}
        if command=='Network.setCookie':
            self.restored.append(values)
            return {'success':True}
        raise AssertionError('예상하지 못한 CDP 요청: '+command)


class SessionTests(unittest.TestCase):
    def test_long_watch_renews_session_before_original_three_hour_expiry(self):
        clock=[time.time()]
        class RenewingDriver(CookieDriver):
            def __init__(self):
                super().__init__();self.cookies[0]['expires']=clock[0]+10800
            def get(self,url):
                self.current_url=url
                if url.startswith('https://accounts.yanolja.com?'):
                    self.cookies[0]['expires']=clock[0]+10800
                self.logged=clock[0]<self.cookies[0]['expires']
            def execute_script(self,script,*args):
                if '__hcHeaders || {}' in script: return {'Access-Token':'test-header'}
                return self.logged
        with tempfile.TemporaryDirectory() as folder,patch('browser.time.monotonic',side_effect=lambda:clock[0]):
            session=BrowserSession(Path(folder));session.driver=RenewingDriver();session.tab='own-tab'
            session.connect(DEFAULT_URL);first_expiry=session.driver.cookies[0]['expires']
            clock[0]+=1801;session.connect(DEFAULT_URL)
            self.assertGreater(session.driver.cookies[0]['expires'],first_expiry)
            for _ in range(7):
                clock[0]+=1801;session.connect(DEFAULT_URL)
            self.assertGreater(clock[0],first_expiry)
            stored=json.loads(session.cookie_file.read_text(encoding='utf-8'))
            self.assertGreater(stored[0]['expires'],clock[0])
            self.assertTrue(session.headers)

    def test_sso_recovers_live_browser_before_old_cookie_file_is_restored(self):
        class SsoDriver(CookieDriver):
            def get(self,url):
                self.current_url=url
                if url.startswith('https://accounts.yanolja.com?'): self.logged=True
            def execute_script(self,script,*args):
                if '__hcHeaders || {}' in script: return {'Access-Token':'fresh-header'}
                return self.logged
        with tempfile.TemporaryDirectory() as folder:
            session=BrowserSession(Path(folder));session.driver=SsoDriver(logged=False);session.tab='own-tab'
            old=dict(session.driver.cookies[0],value='old-session')
            session.cookie_file.write_text(json.dumps([old]),encoding='utf-8')
            session.connect(DEFAULT_URL)
            self.assertEqual(session.driver.restored,[])
            self.assertEqual(json.loads(session.cookie_file.read_text(encoding='utf-8'))[0]['value'],'test-session')

    def test_failed_session_renewal_does_not_replace_saved_login(self):
        with tempfile.TemporaryDirectory() as folder:
            session=BrowserSession(Path(folder));session.driver=CookieDriver(logged=False);session.tab='own-tab'
            session.cookie_file.write_text('[{"saved":"keep"}]',encoding='utf-8')
            with self.assertRaises(LoginRequired): session.connect(DEFAULT_URL)
            self.assertEqual(json.loads(session.cookie_file.read_text(encoding='utf-8')),[{'saved':'keep'}])

    def test_driver_connection_reset_is_recoverable_api_error(self):
        class ResetDriver(CookieDriver):
            def execute_async_script(self,*args): raise ProtocolError('connection reset')
        with tempfile.TemporaryDirectory() as folder:
            session=BrowserSession(Path(folder));session.driver=ResetDriver();session.tab='own-tab'
            session.headers={'Access-Token':'test'};session.ready_url=DEFAULT_URL
            with self.assertRaises(ApiError): session.request('/product/v1/products/10367229')
            self.assertIsNone(session.ready_url)

    def test_logged_out_save_preserves_existing_good_file(self):
        with tempfile.TemporaryDirectory() as folder:
            session=BrowserSession(Path(folder))
            session.cookie_file.write_text('[{"saved":"keep"}]',encoding='utf-8')
            session.driver=CookieDriver(logged=False)
            with self.assertRaises(LoginRequired): session.save_session()
            self.assertEqual(json.loads(session.cookie_file.read_text(encoding='utf-8')),[{'saved':'keep'}])

    def test_save_without_browser_reports_login_required(self):
        with tempfile.TemporaryDirectory() as folder:
            session=BrowserSession(Path(folder))
            with self.assertRaises(LoginRequired): session.save_session()
            self.assertFalse(session.cookie_file.exists())

    def test_saved_login_is_restored_by_next_session(self):
        with tempfile.TemporaryDirectory() as folder:
            session=BrowserSession(Path(folder)); session.driver=CookieDriver()
            session.save_session()
            stored=json.loads(session.cookie_file.read_text(encoding='utf-8'))
            self.assertEqual([cookie['name'] for cookie in stored],['yanolja_sid'])
            next_session=BrowserSession(Path(folder));next_session.driver=CookieDriver(logged=False)
            self.assertEqual(next_session._restore(),1)
            self.assertEqual(next_session.driver.restored[0]['value'],'test-session')

    def test_manual_save_checks_product_then_persists_current_login(self):
        with tempfile.TemporaryDirectory() as folder:
            session=BrowserSession(Path(folder));session.driver=CookieDriver();session.tab='own-tab'
            session.driver.current_url='https://accounts.yanolja.com/'
            session.headers={'Access-Token':'old-test'};session.ready_url=DEFAULT_URL
            session.save_login_session(DEFAULT_URL)
            self.assertEqual(session.driver.current_url,DEFAULT_URL)
            self.assertTrue(session.cookie_file.is_file())
            self.assertFalse(session.headers)
            self.assertIsNone(session.ready_url)
