import json
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from api import ApiError, LoginRequired
from urllib3.exceptions import ProtocolError
from selenium.common.exceptions import NoSuchWindowException
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


class LoginDriver(CookieDriver):
    """복원된 계정 쿠키를 공식 계정 방문 시 NOL 로그인으로 교환한다."""
    def __init__(self,logged=False,cookies=None):
        super().__init__(logged)
        self.cookies=list(cookies or [])
        self.visited=[]

    def get(self,url):
        self.visited.append(url)
        self.current_url=url
        if url.startswith('https://accounts.yanolja.com?'):
            self.logged=self.logged or any(
                cookie.get('name') in ('yanolja_sid','kakao-test-auth')
                for cookie in self.cookies)
            if self.logged:
                self.cookies=[cookie for cookie in self.cookies if cookie.get('name')!='yanolja_sid']
                self.cookies.append({'name':'yanolja_sid','value':'fresh-test-sid',
                    'domain':'.yanolja.com','path':'/','secure':True,'httpOnly':True,
                    'expires':time.time()+10800,'sameSite':'Lax'})

    def execute_script(self,script,*args):
        if '__hcHeaders || {}' in script:
            return {'Access-Token':'test-header'} if self.logged else {}
        return self.logged

    def execute_cdp_cmd(self,command,values):
        result=super().execute_cdp_cmd(command,values)
        if command=='Network.setCookie':
            self.cookies=[cookie for cookie in self.cookies
                if (cookie.get('name'),cookie.get('domain'),cookie.get('path')) !=
                   (values.get('name'),values.get('domain'),values.get('path'))]
            self.cookies.append(dict(values))
        return result


class SessionTests(unittest.TestCase):
    def test_closed_target_tab_recovers_using_an_existing_live_window(self):
        class ReplacedTabDriver(LoginDriver):
            def __init__(self):
                super().__init__(logged=True)
                self.window_handles=['live-tab']
                self.current_window_handle='closed-tab'
                def select(handle):
                    self.current_window_handle=handle
                def create(kind):
                    if self.current_window_handle not in self.window_handles:
                        raise NoSuchWindowException('closed current tab')
                    self.window_handles.append('new-own-tab')
                    self.current_window_handle='new-own-tab'
                self.switch_to=SimpleNamespace(window=select,new_window=create)
            def get(self,url):
                if self.current_window_handle not in self.window_handles:
                    raise NoSuchWindowException('navigation on a closed tab')
                super().get(url)
                if url.startswith('https://accounts.yanolja.com?'):
                    # 계정 리디렉션이 원래 탭을 닫고 다른 탭으로 돌아오는 상황.
                    self.window_handles.remove(self.current_window_handle)
            def execute_cdp_cmd(self,command,values):
                if command=='Page.addScriptToEvaluateOnNewDocument': return {}
                return super().execute_cdp_cmd(command,values)
        with tempfile.TemporaryDirectory() as folder:
            session=BrowserSession(Path(folder));session.driver=ReplacedTabDriver();session.tab='closed-tab'
            try:
                reused=session.open_login(DEFAULT_URL)
            except ApiError as error:
                self.fail('살아 있는 다른 탭에서 인증을 복구해야 함: '+str(error))
            self.assertTrue(reused)
            self.assertEqual(session.tab,'new-own-tab')
            self.assertTrue(session.cookie_file.is_file())

    def test_nol_and_kakao_auth_cookies_round_trip_without_tracking_domains(self):
        allowed=[
            ('yanolja_sid','.yanolja.com'),
            ('nol-account','accounts.yanolja.com'),
            ('kakao-test-auth','.kakao.com'),
            ('kakao-accounts','.accounts.kakao.com'),
            ('kakao-accounts-host','accounts.kakao.com'),
            ('kakao-accounts-child','login.accounts.kakao.com'),
            ('kakao-oauth','kauth.kakao.com')]
        excluded=[
            ('kakao-tracking','tiara.kakao.com'),
            ('payment-account','.kakaopay.com'),
            ('kakao-payment','kakaopay.kakao.com'),
            ('kakao-unrelated','www.kakao.com'),
            ('unrelated','.example.com'),
            ('domain-lookalike','accounts.kakao.com.example.com'),
            ('nol-lookalike','fake-yanolja.com')]
        cookies=[{'name':name,'value':'test-cookie','domain':domain,'path':'/',
            'secure':True,'httpOnly':True,'sameSite':'None','session':True,'expires':-1}
            for name,domain in allowed+excluded]
        with tempfile.TemporaryDirectory() as folder:
            session=BrowserSession(Path(folder));session.driver=CookieDriver()
            session.driver.cookies=cookies
            session.save_session()
            stored=json.loads(session.cookie_file.read_text(encoding='utf-8'))
            self.assertEqual({cookie['name'] for cookie in stored},{name for name,_ in allowed})
            next_session=BrowserSession(Path(folder));next_session.driver=CookieDriver(logged=False)
            self.assertEqual(next_session._restore(),len(allowed))
            self.assertEqual({cookie['name'] for cookie in next_session.driver.restored},
                {name for name,_ in allowed})
            self.assertTrue(all('expires' not in cookie for cookie in next_session.driver.restored),
                '세션 쿠키에 만료 시각을 강제로 부여하지 않고 복원해야 한다')

    def test_open_login_reuses_authenticated_browser_without_manual_login(self):
        with tempfile.TemporaryDirectory() as folder:
            session=BrowserSession(Path(folder));session.driver=LoginDriver(logged=True)
            session.tab='own-tab'
            self.assertTrue(session.open_login(DEFAULT_URL),
                '현재 로그인이 유효하면 수동 로그인 대신 인증 성공을 반환해야 한다')
            self.assertEqual(session.driver.current_url,DEFAULT_URL)
            self.assertTrue(session.cookie_file.is_file())
            stored=json.loads(session.cookie_file.read_text(encoding='utf-8'))
            self.assertIn('yanolja_sid',{cookie['name'] for cookie in stored})

    def test_open_login_restores_saved_kakao_session_before_manual_login(self):
        cookies=[
            {'name':'yanolja_sid','value':'expired-test-sid','domain':'.yanolja.com',
             'path':'/','expires':time.time()-3600},
            {'name':'kakao-test-auth','value':'test-cookie','domain':'.accounts.kakao.com',
             'path':'/','secure':True,'httpOnly':True,'sameSite':'None',
             'session':True,'expires':-1}]
        with tempfile.TemporaryDirectory() as folder:
            session=BrowserSession(Path(folder));session.driver=LoginDriver();session.tab='own-tab'
            session.cookie_file.write_text(json.dumps(cookies),encoding='utf-8')
            self.assertTrue(session.open_login(DEFAULT_URL),
                'NOL SID가 만료되어도 저장된 카카오 세션으로 먼저 복원해야 한다')
            self.assertEqual(session.driver.current_url,DEFAULT_URL)
            self.assertEqual([cookie['name'] for cookie in session.driver.restored],['kakao-test-auth'])
            stored=json.loads(session.cookie_file.read_text(encoding='utf-8'))
            self.assertGreater(next(cookie['expires'] for cookie in stored
                if cookie['name']=='yanolja_sid'),time.time())

    def test_analytics_only_own_file_does_not_block_authenticated_reference_fallback(self):
        own=[{'name':'analytics-only','value':'test-cookie','domain':'.yanolja.com',
            'path':'/','expires':time.time()+3600}]
        reference=[{'name':'yanolja_sid','value':'reference-test-sid','domain':'.yanolja.com',
            'path':'/','secure':True,'httpOnly':True,'expires':time.time()+3600}]
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder)/'hwadam_codex';base.mkdir()
            reference_base=Path(folder)/'hwadam_claude';reference_base.mkdir()
            session=BrowserSession(base);session.driver=LoginDriver();session.tab='own-tab'
            session.cookie_file.write_text(json.dumps(own),encoding='utf-8')
            (reference_base/'session_cookies.json').write_text(json.dumps(reference),encoding='utf-8')
            try:
                session.connect(DEFAULT_URL)
            except LoginRequired as error:
                self.fail('비인증 쿠키만 복원되면 다음 저장 출처의 인증도 확인해야 한다: '+str(error))
            self.assertEqual(session.ready_url,DEFAULT_URL)
            self.assertTrue(session.headers)
            self.assertIn('yanolja_sid',{cookie['name'] for cookie in session.driver.restored})

    def test_open_login_requires_manual_login_and_preserves_unusable_saved_file(self):
        with tempfile.TemporaryDirectory() as folder:
            session=BrowserSession(Path(folder));session.driver=LoginDriver();session.tab='own-tab'
            original=json.dumps([{'name':'analytics-only','value':'test-cookie',
                'domain':'.yanolja.com','path':'/','expires':time.time()+3600}])
            session.cookie_file.write_text(original,encoding='utf-8')
            self.assertIs(session.open_login(DEFAULT_URL),False)
            self.assertTrue(session.driver.current_url.startswith('https://accounts.yanolja.com?'))
            self.assertEqual(session.cookie_file.read_text(encoding='utf-8'),original)

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
