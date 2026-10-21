"""기존 헬퍼와 분리된 Edge 세션 및 인증된 페이지 내부 조회."""
import io
from contextlib import contextmanager
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from urllib.parse import quote, urlsplit

from selenium import webdriver
from selenium.common.exceptions import NoSuchWindowException, StaleElementReferenceException, TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.service import Service
from urllib3.exceptions import HTTPError

from api import AUTH_CODES, ApiError, LoginRequired
from gui import KST, validate_url
from storage import atomic_write


API_BASE = "https://leisure-web-api.yanolja.com"
DEBUG_PORT = 9444
SESSION_REFRESH_SECONDS = 1800
SESSION_CHECK_SECONDS = 300
SESSION_EXPIRY_MARGIN_SECONDS = 600
COOKIE_KEYS = ("name","value","domain","path","secure","httpOnly","sameSite","expires")


def _login_cookie(cookie):
    """NOL과 카카오 계정 쿠키만 보관한다. 다른 카카오 서비스는 제외한다."""
    domain = cookie.get("domain","")
    if not isinstance(domain,str):
        return False
    domain = domain.lower().lstrip(".")
    return (domain == "yanolja.com" or domain.endswith(".yanolja.com")
            or domain in ("kakao.com","accounts.kakao.com","kauth.kakao.com")
            or domain.endswith(".accounts.kakao.com"))


def _cookie_summary(cookies):
    """값을 읽지 않고 인증 관련 쿠키 개수와 SID 유효기간만 요약한다."""
    cookies = [cookie for cookie in cookies if isinstance(cookie,dict) and _login_cookie(cookie)]
    nol = [cookie for cookie in cookies if cookie['domain'].lower().lstrip('.') == 'yanolja.com'
           or cookie['domain'].lower().endswith('.yanolja.com')]
    sid = [cookie for cookie in nol if cookie.get('name') == 'yanolja_sid'
           and cookie['domain'].lower().lstrip('.') == 'yanolja.com']
    expiry = max((cookie['expires'] for cookie in sid
                  if isinstance(cookie.get('expires'),(int,float)) and math.isfinite(cookie['expires'])
                  and cookie['expires']>0),default=0)
    session = any(cookie.get('session') or cookie.get('expires',-1) in (-1,0) for cookie in sid)
    state = ('missing' if not sid else 'valid' if expiry>time.time() else
             'session' if session else 'expired' if expiry else 'unknown')
    return {'nol_cookies':len(nol),'kakao_cookies':len(cookies)-len(nol),
            'sid_state':state,'sid_remaining_s':int(expiry-time.time()) if expiry else 0}


HOOK_JS = r"""
(() => {
  if (window.__hcHooked) return;
  window.__hcHooked = true;
  window.__hcHeaders = {};
  const api = u => String(u).includes('leisure-web-api.yanolja.com');
  const open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(m,u) { this.__hcUrl=String(u); return open.apply(this,arguments); };
  const set = XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.setRequestHeader = function(k,v) {
    if (api(this.__hcUrl)) window.__hcHeaders[k]=String(v);
    return set.apply(this,arguments);
  };
  const fetch = window.fetch;
  window.fetch = function(input,init) {
    const u = typeof input === 'string' ? input : input.url;
    if (api(u)) {
      const headers = new Headers((init && init.headers) || (input && input.headers) || {});
      headers.forEach((v,k) => { window.__hcHeaders[k]=v; });
    }
    return fetch.apply(this,arguments);
  };
})();
"""
REQUEST_JS = r"""
const done=arguments[arguments.length-1];
const xhr=new XMLHttpRequest();
xhr.open(arguments[1],arguments[0]); xhr.withCredentials=true; xhr.timeout=12000;
Object.entries(arguments[2] || {}).forEach(([k,v]) => xhr.setRequestHeader(k,v));
xhr.onload=() => done({status:xhr.status,body:xhr.responseText});
xhr.onerror=() => done({status:-1,body:''});
xhr.ontimeout=() => done({status:-2,body:''});
if (arguments[3] !== null) xhr.setRequestHeader('Content-Type','application/json');
xhr.send(arguments[3] === null ? null : JSON.stringify(arguments[3]));
"""


class BrowserSession:
    def __init__(self,base: Path,log=lambda text: None):
        self.base = Path(base)
        self.profile = self.base / "edge_profile"
        self.cookie_file = self.base / "session_cookies.json"
        self.marker_file = self.base / "browser_session.json"
        self.driver_file = self.base / "driver" / "msedgedriver.exe"
        self.log = log
        self.lock = threading.RLock()
        self.driver = None
        self.tab = None
        self._positioned_browser = None
        self.headers = {}
        self.ready_url = None
        self.headers_at = 0.0
        self.auth_trace = uuid.uuid4().hex[:8]
        self.auth_last_flag = 'NONE'
        self.session_checked_at = 0.0
        self.session_checked_wall_at = 0.0
        self.session_authenticated_at = 0.0
        self.session_authenticated_wall_at = 0.0
        self.session_maintenance_blocked = False

    def _diag(self,flag,**metadata):
        # 호출부는 고정된 분류·수치만 전달한다. URL, 비밀값, 예외 본문은 전달하지 않는다.
        fields = ' '.join(f'{key}={value}' for key,value in metadata.items())
        self.log(f'[AUTH trace={self.auth_trace} flag={flag}]'+(' '+fields if fields else ''))
        self.auth_last_flag = flag

    def _begin_auth(self,trigger):
        self.auth_trace = uuid.uuid4().hex[:8]
        self._diag('AUTH_BEGIN',trigger=trigger)

    @contextmanager
    def _auth_attempt(self,trigger,*,quiet=False):
        if not quiet:
            self._begin_auth(trigger)
        try:
            yield
        except Exception as error:
            self._diag('AUTH_ERROR',error_type=type(error).__name__,last_flag=self.auth_last_flag,operation=trigger)
            if isinstance(error,(WebDriverException,HTTPError,OSError)):
                # GUI가 예외 메시지를 따로 기록해도 원본 브라우저 메시지가 노출되지 않게 한다.
                raise ApiError(f'인증 연결 작업 실패 · {type(error).__name__} · 추적 ID {self.auth_trace}. 전용 Edge와 연결 상태를 확인하세요') from None
            raise

    @staticmethod
    def _url_kind(url):
        try:
            parts = urlsplit(url)
        except (ValueError,TypeError):
            return 'unavailable'
        if parts.hostname == 'leisure-web.yanolja.com':
            return 'product' if parts.path.startswith('/leisure/') else 'nol_service'
        return {'accounts.yanolja.com':'nol_account','accounts.kakao.com':'kakao_account',
                'kauth.kakao.com':'kakao_oauth'}.get(parts.hostname,'other')

    def _page_kind(self):
        try:
            return self._url_kind(self.driver.current_url)
        except (AttributeError,WebDriverException,HTTPError,OSError):
            return 'unavailable'

    def _debug(self):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json/version",timeout=2) as response:
                return json.load(response)
        except (OSError,ValueError):
            return {}

    @staticmethod
    def _version(path):
        if not Path(path).is_file():
            return ""
        try:
            flags = getattr(subprocess,"CREATE_NO_WINDOW",0)
            output = subprocess.check_output([str(path),"--version"],timeout=8,creationflags=flags).decode("utf-8","ignore")
            match = re.search(r"\d+\.\d+\.\d+\.\d+",output)
            return match.group(0) if match else ""
        except (OSError,subprocess.SubprocessError):
            return ""

    def _driver_path(self,browser_version):
        major = browser_version.split(".")[0]
        if self._version(self.driver_file).split(".")[0] == major:
            return self.driver_file
        self.driver_file.parent.mkdir(parents=True,exist_ok=True)
        for folder in ("hwadam_claude","cgv_grok_new"):
            candidate = self.base.parent / folder / "driver" / "msedgedriver.exe"
            if self._version(candidate).split(".")[0] == major:
                shutil.copyfile(candidate,self.driver_file)
                self.log(f"기존 Edge 드라이버 재사용 · 버전 {major}")
                return self.driver_file
        versions = [browser_version]
        try:
            with urllib.request.urlopen(f"https://msedgedriver.microsoft.com/LATEST_RELEASE_{major}",timeout=20) as response:
                raw = response.read()
            encoding = "utf-16" if raw.startswith((b"\xff\xfe",b"\xfe\xff")) else "utf-8"
            latest = re.sub(r"[^\d.]","",raw.decode(encoding))
            if latest and latest not in versions:
                versions.append(latest)
        except (OSError,UnicodeError):
            pass
        for version in versions:
            if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+",version):
                continue
            try:
                url = f"https://msedgedriver.microsoft.com/{version}/edgedriver_win64.zip"
                with urllib.request.urlopen(url,timeout=30) as response:
                    content = response.read()
                with zipfile.ZipFile(io.BytesIO(content)) as archive:
                    name = next(name for name in archive.namelist() if name.lower().endswith("msedgedriver.exe"))
                    self.driver_file.write_bytes(archive.read(name))
                self.log(f"Microsoft 공식 Edge 드라이버 준비 · {version}")
                return self.driver_file
            except (OSError,ValueError,zipfile.BadZipFile,StopIteration):
                continue
        raise ApiError("Edge 드라이버를 준비하지 못했습니다. 인터넷 연결과 Edge 버전을 확인하세요")

    def _launch(self,url):
        info = self._debug()
        if info:
            try:
                marker = json.loads(self.marker_file.read_text(encoding="utf-8"))
            except (OSError,ValueError):
                marker = {}
            if marker.get("browser") != info.get("webSocketDebuggerUrl"):
                self._diag('DEBUG_PORT_CONFLICT',port=DEBUG_PORT)
                raise ApiError(f"포트 {DEBUG_PORT}를 다른 브라우저가 사용 중입니다. hwadam_codex 전용 Edge를 확인하세요")
            self._diag('BROWSER_REUSED',port=DEBUG_PORT)
            return info
        candidates = [Path(os.environ.get("ProgramFiles(x86)",r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
                      Path(os.environ.get("ProgramFiles",r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe"]
        exe = next((path for path in candidates if path.is_file()),None)
        if exe is None:
            raise ApiError("Microsoft Edge 실행 파일을 찾지 못했습니다")
        self.profile.mkdir(parents=True,exist_ok=True)
        subprocess.Popen([str(exe),f"--user-data-dir={self.profile}",f"--remote-debugging-port={DEBUG_PORT}",
                          "--remote-debugging-address=127.0.0.1","--no-first-run","--no-default-browser-check",
                          "--window-size=1080,940",url],close_fds=True,
                         creationflags=getattr(subprocess,"DETACHED_PROCESS",0) | getattr(subprocess,"CREATE_NEW_PROCESS_GROUP",0))
        deadline = time.monotonic()+15
        while time.monotonic() < deadline:
            info = self._debug()
            if info:
                atomic_write(self.marker_file,json.dumps({"browser":info.get("webSocketDebuggerUrl")},indent=2))
                self.log("hwadam_codex 전용 Edge 열림")
                self._diag('BROWSER_LAUNCHED',port=DEBUG_PORT)
                return info
            time.sleep(0.25)
        raise ApiError("전용 Edge에 연결하지 못했습니다. 전용 프로필 창을 닫고 다시 브라우저를 열어주세요")

    def _place_window(self):
        """현재 모니터 작업 영역의 오른쪽 40%에 전용 브라우저를 배치한다."""
        available = self.driver.execute_script("""
            return {left: screen.availLeft ?? 0, top: screen.availTop ?? 0,
                    width: screen.availWidth, height: screen.availHeight};
        """)
        if not isinstance(available,dict) or any(
                not isinstance(available.get(key),(int,float)) or not math.isfinite(available[key])
                for key in ('left','top','width','height')):
            raise ValueError('모니터 작업 영역을 확인하지 못했습니다')
        if available['width']<=0 or available['height']<=0:
            raise ValueError('모니터 작업 영역이 비어 있습니다')
        width = max(1,round(available['width']*.40))
        bounds = {'left':round(available['left']+available['width'])-width,
                  'top':round(available['top']),'width':width,'height':round(available['height'])}
        window = self.driver.execute_cdp_cmd('Browser.getWindowForTarget',{})
        window_id = window['windowId']
        if window.get('bounds',{}).get('windowState')!='normal':
            self.driver.execute_cdp_cmd('Browser.setWindowBounds',
                                        {'windowId':window_id,'bounds':{'windowState':'normal'}})
        self.driver.execute_cdp_cmd('Browser.setWindowBounds',{'windowId':window_id,'bounds':bounds})
        self.log('[BROWSER_WINDOW_PLACED] 전용 Edge · 화면 오른쪽 40% · 작업 영역 전체 높이')

    def _attach(self,url):
        if self.driver is not None:
            try:
                _ = self.driver.window_handles
                return
            except (WebDriverException,HTTPError,OSError) as error:
                self._diag('BROWSER_CONNECTION_LOST',error_type=type(error).__name__)
                self.detach()
        info = self._launch(url)
        match = re.search(r"(?:Edg|Chrome)/(\d+\.\d+\.\d+\.\d+)",info.get("Browser",""))
        if match is None:
            raise ApiError("실행 중인 Edge 버전을 확인하지 못했습니다")
        options = webdriver.EdgeOptions()
        options.add_experimental_option("debuggerAddress",f"127.0.0.1:{DEBUG_PORT}")
        self.driver = webdriver.Edge(service=Service(str(self._driver_path(match.group(1)))),options=options)
        self.driver.set_page_load_timeout(25)
        self.driver.set_script_timeout(18)
        self.tab = self.driver.current_window_handle
        self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",{"source":HOOK_JS})
        self.headers = {}
        self.ready_url = None
        self._diag('BROWSER_ATTACHED',browser_major=match.group(1).split('.')[0])
        identity = info.get('webSocketDebuggerUrl')
        if not identity or self._positioned_browser!=identity:
            try:
                self._place_window()
            except (WebDriverException,HTTPError,OSError,ValueError,TypeError,KeyError) as error:
                self.log(f'[BROWSER_WINDOW_FAILED] 창 배치 실패 · {type(error).__name__}')
            else:
                self._positioned_browser = identity

    def _select(self):
        handles = self.driver.window_handles
        if self.tab not in handles:
            self._diag('TAB_RECREATED',open_tabs=len(handles))
            if handles:
                self.driver.switch_to.window(handles[0])
            self.driver.switch_to.new_window("tab")
            self.tab = self.driver.current_window_handle
            self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",{"source":HOOK_JS})
            self.ready_url = None
        self.driver.switch_to.window(self.tab)
        try:
            self.driver.execute_cdp_cmd("Emulation.setFocusEmulationEnabled",{"enabled":True})
        except WebDriverException:
            pass

    def _navigate(self,url):
        target = self._url_kind(url)
        self._diag('NAVIGATION_BEGIN',target=target)
        try:
            self._select()
            self.driver.get(url)
        except TimeoutException:
            self._diag('NAVIGATION_TIMEOUT',target=target)
            self.log("페이지 로딩 신호 지연 · 표시된 상품을 확인합니다")
        self._diag('NAVIGATION_DONE',target=target,page=self._page_kind())

    def _logged_in(self):
        deadline = time.monotonic()+8
        while time.monotonic()<deadline:
            state = self.driver.execute_script("return window.__NEXT_DATA__?.props?.pageProps?.isLoggedIn ?? null;")
            if state is not None:
                self._diag('LOGIN_STATE',state='authenticated' if state else 'anonymous',page=self._page_kind())
                return bool(state)
            time.sleep(0.2)
        self._diag('LOGIN_STATE',state='unavailable',page=self._page_kind())
        return False

    def _cookie_sources(self):
        return (self.cookie_file,self.base.parent / "hwadam_claude" / "session_cookies.json")

    def _restore(self,source=None):
        for source in ((Path(source),) if source is not None else self._cookie_sources()):
            label = 'saved' if source == self.cookie_file else 'reference'
            try:
                cookies = json.loads(source.read_text(encoding="utf-8"))
                file_age = max(0,int(time.time()-source.stat().st_mtime))
            except FileNotFoundError:
                self._diag('COOKIE_FILE_MISSING',source=label)
                continue
            except (OSError,ValueError) as error:
                self._diag('COOKIE_FILE_INVALID',source=label,error_type=type(error).__name__)
                continue
            if not isinstance(cookies,list):
                self._diag('COOKIE_FILE_INVALID',source=label,reason='not_list')
                continue
            restored = expired = rejected = 0
            for cookie in cookies:
                if not isinstance(cookie,dict) or not _login_cookie(cookie):
                    continue
                values = {key:cookie[key] for key in COOKIE_KEYS if key in cookie}
                if (not isinstance(values.get('expires',-1),(int,float))
                        or not math.isfinite(values.get('expires',-1))):
                    rejected += 1
                    continue
                if cookie.get("session") or values.get("expires",-1) <= 0:
                    values.pop("expires",None)
                elif values["expires"] < time.time():
                    expired += 1
                    continue
                try:
                    if self.driver.execute_cdp_cmd("Network.setCookie",values).get("success"):
                        restored += 1
                    else:
                        rejected += 1
                except WebDriverException:
                    rejected += 1
                    continue
            self._diag('COOKIE_RESTORE_RESULT',source=label,restored=restored,expired=expired,
                       rejected=rejected,file_age_s=file_age,**_cookie_summary(cookies))
            if restored:
                self.log("저장된 NOL·간편로그인 세션 복원 시도" + (" · 참고 폴더에서 읽음" if source != self.cookie_file else ""))
                return restored
        return 0

    def save_session(self):
        with self.lock:
            if self.driver is None:
                self._diag('COOKIE_SAVE_SKIPPED',reason='no_browser')
                raise LoginRequired('먼저 로그인 브라우저를 열고 NOL 로그인 후 세션을 저장하세요')
            if not self._logged_in():
                self.session_maintenance_blocked = True
                self._diag('COOKIE_SAVE_SKIPPED',reason='not_logged_in')
                raise LoginRequired('로그인이 확인되지 않아 세션을 저장하지 않았습니다. 전용 Edge에서 로그인 후 다시 저장하세요')
            cookies = self.driver.execute_cdp_cmd("Network.getAllCookies",{}).get("cookies",[])
            cookies = [cookie for cookie in cookies if _login_cookie(cookie)]
            if not cookies:
                self._diag('COOKIE_SAVE_SKIPPED',reason='no_login_cookies')
                raise ApiError('저장할 NOL 세션 쿠키를 찾지 못했습니다. 기존 저장 파일을 유지합니다')
            atomic_write(self.cookie_file,json.dumps(cookies,ensure_ascii=False,indent=1))
            self._session_verified()
            self._diag('COOKIE_SAVED',**_cookie_summary(cookies))
            return len(cookies)

    def open_login(self,url):
        """기존 세션 또는 저장된 카카오 계정으로 로그인하고 확인된 세션을 저장한다."""
        validate_url(url)
        with self.lock,self._auth_attempt('open_login'):
            self._attach(url)
            self._select()
            self.headers = {}
            self.ready_url = None
            if self._ensure_login(url,manual=True):
                self.save_session()
                self.log('NOL 로그인 확인 · NOL·간편로그인 세션 자동 저장 완료')
                return True
            self.log("전용 Edge에서 로그인 후 GUI의 '로그인 세션 저장'을 누르세요")
            return False

    def _try_kakao_password_autofill(self,password,timeout,*,preserve_guard=False):
        """성공하면 None, 실패하면 원인 flag. 비밀번호 값은 브라우저 밖으로 읽지 않는다."""
        deadline = time.monotonic()+timeout
        try:
            # 아이디 비교는 페이지 안에서만 수행한다. 다른 저장 계정으로 바뀌면 제출하지 않는다.
            identified = self.driver.execute_script("""
                const field = arguments[0];
                const account = (field.form || document).querySelector(
                    'input[name="loginId"], input[autocomplete="username"], input[type="email"], input[type="text"]');
                const original = account?.value;
                field.__hcKakaoAccountUnchanged = () => Boolean(
                    account?.isConnected && original?.trim() && account.value === original);
                return field.__hcKakaoAccountUnchanged();
            """,password)
            if identified is not True: return 'autofill_account_missing'
            self.driver.execute_cdp_cmd('Page.bringToFront',{})
            password.click()
            self._diag('KAKAO_PASSWORD_FOCUSED')
            self.log('카카오 비밀번호 칸 클릭 · Edge 저장 암호 선택 대기')

            def filled():
                return self.driver.execute_script('return Boolean(arguments[0].value);',password) is True

            def ready(source):
                unchanged = self.driver.execute_script(
                    'return Boolean(arguments[0].__hcKakaoAccountUnchanged?.());',password)
                if unchanged is not True: return 'autofill_account_changed'
                self._diag('KAKAO_AUTOFILL_READY',source=source)
                self.log('저장 암호 입력 확인 · 로그인 상태 유지 확인 중')
                return None

            if filled():
                return ready('already_filled')
            # 팝업 생성 직후의 오입력 방지 시간이 지난 뒤 첫 항목을 선택한다.
            time.sleep(min(.8,max(0,deadline-time.monotonic())))
            if not filled() and time.monotonic()<deadline:
                # WebDriver send_keys는 nativeVirtualKeyCode가 없어 브라우저 팝업의
                # 키 리스너를 건너뛴다. PageUp은 첫 항목 선택, Tab은 선택 확정이며
                # 팝업이 없을 때도 폼을 제출하지 않는다. 전역 Windows 입력은 쓰지 않는다.
                for key,code in (('PageUp',33),('Tab',9)):
                    for event in ('rawKeyDown','keyUp'):
                        self.driver.execute_cdp_cmd('Input.dispatchKeyEvent',{
                            'type':event,'key':key,'code':key,
                            'windowsVirtualKeyCode':code,'nativeVirtualKeyCode':code})
                self._diag('KAKAO_AUTOFILL_SELECTION_ATTEMPTED')
            while time.monotonic()<deadline:
                if filled(): return ready('browser')
                time.sleep(.1)
            self._diag('KAKAO_AUTOFILL_UNAVAILABLE')
            return 'autofill_unavailable'
        finally:
            if not preserve_guard:
                self._clear_kakao_password_guard(password)

    def _clear_kakao_password_guard(self,password):
        if password is None: return
        try:
            self.driver.execute_script('delete arguments[0].__hcKakaoAccountUnchanged;',password)
        except (WebDriverException,HTTPError,OSError):
            pass  # 이동·창 종료로 사라진 페이지의 임시 비교 함수는 함께 폐기된다.

    def _try_kakao_login(self,url,timeout=25):
        """기존 계정과 Edge 저장 암호로만 로그인한다. 추가 인증은 사용자에게 맡긴다."""
        origin = self.tab
        initial_handles = set(self.driver.window_handles)
        active = origin
        popup = None
        started = selected = password_attempted = submitted = False
        password_ready = False
        password_guard = None
        deadline = time.monotonic()+timeout
        self._diag('KAKAO_LOGIN_BEGIN')
        self.log('카카오 간편로그인 시도 · 저장된 계정 선택 대기')

        def pending(reason,**metadata):
            self._clear_kakao_password_guard(password_guard)
            self.session_maintenance_blocked = True
            self._diag('KAKAO_MANUAL_REQUIRED',reason=reason,**metadata)
            messages = {
                'multiple_accounts':'저장된 카카오 계정이 여러 개입니다. 사용할 계정을 직접 선택하세요',
                'no_saved_account':'저장된 카카오 계정이 없습니다. 전용 Edge에서 로그인하세요',
                'additional_auth':'추가 인증이 필요합니다. 전용 Edge에서 이어서 진행하세요',
                'autofill_unavailable':'저장 암호가 입력되지 않았습니다. Edge 암호 목록 또는 Windows 인증 창을 확인하세요',
                'autofill_account_missing':'자동완성할 카카오 아이디가 확인되지 않았습니다. 사용할 계정을 직접 선택하세요',
                'autofill_account_changed':'저장 암호 선택 중 카카오 아이디가 달라져 제출하지 않았습니다. 사용할 계정을 확인하세요',
                'ambiguous_password':'비밀번호 입력칸을 하나로 확인하지 못했습니다. 카카오 화면을 확인하세요',
                'keep_not_checked':'로그인 상태 유지 체크를 확인하지 못했습니다. 카카오 화면을 확인하세요',
                'submit_unavailable':'카카오 로그인 버튼을 하나로 확인하지 못했습니다. 카카오 화면을 확인하세요',
                'login_not_confirmed':'카카오 선택 후 NOL 로그인이 확인되지 않았습니다',
                'unexpected_page':'예상한 로그인 화면이 아닙니다. 전용 Edge 화면을 확인하세요',
                'multiple_popups':'로그인 창이 여러 개 열렸습니다. 전용 Edge에서 사용할 창을 확인하세요',
                'window_closed':'로그인 창이 닫혔습니다. 로그인 브라우저를 다시 열어주세요',
                'click_failed':'카카오 로그인 버튼을 누르지 못했습니다. 전용 Edge 화면을 확인하세요',
                'timeout':'카카오 자동 로그인 대기 시간이 지났습니다. 추가 인증·동의 또는 계정 선택 화면을 확인하세요',
            }
            self.log(messages[reason])
            return False

        def page():
            parts = urlsplit(self.driver.current_url)
            if parts.scheme!='https': return 'other'
            return self._url_kind(self.driver.current_url)

        def visible(by,selector):
            return [element for element in self.driver.find_elements(by,selector) if element.is_displayed()]

        def clickable(element):
            return element.is_enabled() and element.get_attribute('aria-disabled')!='true'

        while time.monotonic()<deadline:
            try:
                handles = self.driver.window_handles
                if started and popup is None:
                    candidates = []
                    for handle in handles:
                        if handle in initial_handles: continue
                        self.driver.switch_to.window(handle)
                        if page() in ('nol_account','kakao_account','kakao_oauth','product'):
                            candidates.append(handle)
                    if len(candidates)>1: return pending('multiple_popups')
                    if candidates:
                        popup = active = candidates[0]
                if active not in handles:
                    if origin not in handles: return pending('window_closed')
                    active = origin
                if selected and popup is not None and origin in handles and active!=origin:
                    # 일부 OAuth 흐름은 팝업을 닫지 않고 원래 탭만 상품으로 돌려놓는다.
                    self.driver.switch_to.window(origin)
                    if page()=='product': active = origin
                self.driver.switch_to.window(active)
                kind = page()
                if kind=='product':
                    self.tab = active
                    # 클릭·리디렉션만으로 성공을 판단하지 않고 상품의 실제 인증을 확인한다.
                    if self.driver.current_url!=url:
                        self._navigate(url)
                    if not self._logged_in(): return pending('login_not_confirmed')
                    self._clear_kakao_password_guard(password_guard)
                    self._diag('KAKAO_LOGIN_READY')
                    return True
                if kind not in ('nol_account','kakao_account','kakao_oauth','nol_service'):
                    return pending('unexpected_page')
                if kind in ('nol_account','kakao_account'):
                    if visible(By.CSS_SELECTOR,"input[autocomplete='one-time-code']"):
                        return pending('additional_auth')
                    passwords = visible(By.CSS_SELECTOR,"input[type='password']")
                    if passwords and not submitted:
                        if kind!='kakao_account': return pending('additional_auth')
                        if len(passwords)!=1 or not clickable(passwords[0]):
                            return pending('ambiguous_password')
                        if not password_ready:
                            if password_attempted: return pending('autofill_unavailable')
                            password_attempted = True
                            selected = True
                            password_guard = passwords[0]
                            reason = self._try_kakao_password_autofill(
                                passwords[0],min(5,max(0,deadline-time.monotonic())),preserve_guard=True)
                            if reason: return pending(reason)
                            password_ready = True
                        # 자동완성 도중 리디렉션되었다면 다른 페이지의 버튼을 누르지 않는다.
                        if page()!='kakao_account': continue
                        if self.driver.execute_script(
                            'return Boolean(arguments[0].value && arguments[0].__hcKakaoAccountUnchanged?.());',
                            passwords[0]) is not True:
                            return pending('autofill_account_changed')
                        keep = visible(By.CSS_SELECTOR,"input[type='checkbox'][name='staySignedIn']")
                        if len(keep)!=1: return pending('keep_not_checked')
                        if not keep[0].is_selected():
                            if not clickable(keep[0]): return pending('keep_not_checked')
                            labels = visible(By.CSS_SELECTOR,'label#label-staySignedIn')
                            (labels[0] if len(labels)==1 else keep[0]).click()
                        if not keep[0].is_selected(): return pending('keep_not_checked')
                        self._diag('KAKAO_KEEP_SIGNED_IN',checked=1)
                        buttons = visible(By.XPATH,"//button[@type='submit' and normalize-space(.)='로그인']")
                        if len(buttons)!=1 or not clickable(buttons[0]): return pending('submit_unavailable')
                        # 응답 지연이나 창 전환 중에도 로그인 제출은 한 번만 한다.
                        submitted = True
                        try:
                            buttons[0].click()
                        except StaleElementReferenceException:
                            # 클릭 전 교체된 요소로 거절된 경우만 새 버튼으로 재시도한다.
                            submitted = False
                            raise
                        self._diag('KAKAO_LOGIN_SUBMITTED')
                        self.log('로그인 상태 유지 체크 확인 · 카카오 로그인 제출 완료')
                if kind=='nol_account' and not started:
                    buttons = visible(By.XPATH,"//button[contains(normalize-space(.), '카카오로 시작하기')]")
                    if len(buttons)==1 and clickable(buttons[0]):
                        buttons[0].click()
                        started = True
                        self._diag('KAKAO_START_CLICKED')
                        self.log('카카오로 시작하기 클릭 완료')
                elif kind=='kakao_account' and not selected:
                    # 새로운 계정·계정 삭제 버튼과 구분되는 실제 저장 계정의 프로필만 선택한다.
                    accounts = [element for element in visible(By.CSS_SELECTOR,"a.wrap_profile[role='button']")
                                if element.find_elements(By.CSS_SELECTOR,'.info_profile')]
                    if len(accounts)>1: return pending('multiple_accounts',accounts=len(accounts))
                    if len(accounts)==1 and clickable(accounts[0]):
                        accounts[0].click()
                        selected = True
                        self._diag('KAKAO_ACCOUNT_CLICKED',accounts=1)
                        self.log('저장된 카카오 계정 선택 완료 · NOL 로그인 확인 중')
                    elif not accounts and visible(By.XPATH,"//h2[normalize-space(.)='로그인할 카카오계정 선택']"):
                        return pending('no_saved_account')
            except (StaleElementReferenceException,NoSuchWindowException):
                # 리디렉션으로 요소·팝업이 사라졌다면 다음 상태에서 다시 판정한다.
                pass
            except (WebDriverException,HTTPError,OSError) as error:
                return pending('click_failed',error_type=type(error).__name__)
            time.sleep(.2)
        return pending('login_not_confirmed' if submitted else 'timeout')

    def save_login_session(self,url):
        """현재 브라우저 로그인을 새 상품 응답으로 확인한 뒤 저장한다."""
        validate_url(url)
        with self.lock,self._auth_attempt('manual_save'):
            self._attach(url)
            self._select()
            self.headers = {}
            self.ready_url = None
            self._navigate(url)
            # 수동 저장은 이전 파일을 복원하지 않고 지금 로그인한 상태를 저장한다.
            return self.save_session()

    def _session_expiry(self,cookies=None):
        if cookies is None:
            cookies=self.driver.execute_cdp_cmd('Network.getAllCookies',{}).get('cookies',[])
        values=[cookie.get('expires',0) for cookie in cookies
                if cookie.get('name')=='yanolja_sid' and cookie.get('domain','').lstrip('.')=='yanolja.com'
                and isinstance(cookie.get('expires'),(int,float)) and math.isfinite(cookie['expires'])
                and cookie['expires']>0]
        return max(values,default=0)

    def _session_verified(self):
        self.session_authenticated_at = time.monotonic()
        self.session_authenticated_wall_at = time.time()
        self.session_maintenance_blocked = False

    def maintain_session(self,url):
        """감시 중지 중에도 실제 NOL 인증을 갱신한다. 쿠키 만료값은 수정하지 않는다."""
        validate_url(url)
        with self.lock:
            if self.session_maintenance_blocked:
                return {'state':'blocked'}
            if self.driver is None and not self.cookie_file.is_file():
                return {'state':'not_configured'}
            now = time.monotonic()
            wall = time.time()
            gap = now-self.session_checked_at if self.session_checked_at else 0
            wall_gap = wall-self.session_checked_wall_at if self.session_checked_wall_at else 0
            if self.session_checked_at and gap < SESSION_CHECK_SECONDS and wall_gap < SESSION_CHECK_SECONDS:
                return {'state':'waiting'}
            # 일시적인 연결 오류에도 다음 타이머마다 즉시 재시도하지 않는다.
            self.session_checked_at = now
            self.session_checked_wall_at = wall
            with self._auth_attempt('keepalive'):
                self._attach(url)
                self._select()
                cookies = self.driver.execute_cdp_cmd('Network.getAllCookies',{}).get('cookies',[])
                summary = _cookie_summary(cookies)
                expiry = self._session_expiry(cookies)
                self._diag('KEEPALIVE_CHECK',check_gap_s=max(0,int(gap)),
                           wall_gap_s=max(0,int(wall_gap)),**summary)
                auth_age = max(now-self.session_authenticated_at,wall-self.session_authenticated_wall_at)
                reason = ('initial' if not self.session_authenticated_at else
                          'sid_missing' if summary['sid_state']=='missing' else
                          'sid_expired' if summary['sid_state']=='expired' else
                          'expires_soon' if expiry and expiry-wall <= SESSION_EXPIRY_MARGIN_SECONDS else
                          'scheduled' if auth_age >= SESSION_REFRESH_SECONDS else '')
                if not reason:
                    return {'state':'checked','expires_at':expiry}
                self._diag('KEEPALIVE_REFRESH',reason=reason)
                # 계정·상품 이동으로 이전 조회 헤더가 낡을 수 있어 다음 조회에서 다시 확보한다.
                self.headers = {}
                self.ready_url = None
                if not self._ensure_login(url):
                    raise LoginRequired('자동 로그인 유지로 인증을 복원하지 못했습니다. 전용 Edge에서 다시 로그인한 뒤 로그인 세션을 저장하세요')
                self.save_session()
                cookies = self.driver.execute_cdp_cmd('Network.getAllCookies',{}).get('cookies',[])
                self._diag('KEEPALIVE_RESULT',state='renewed',**_cookie_summary(cookies))
                self.log('대기 중 로그인 세션 갱신·저장 완료')
                return {'state':'renewed','expires_at':self._session_expiry(cookies)}

    def _refresh_session(self,url,*,source='live'):
        """공식 계정의 기존 로그인으로 SID를 갱신한다. 로그인 입력은 하지 않는다."""
        started=time.monotonic()
        cookies=self.driver.execute_cdp_cmd('Network.getAllCookies',{}).get('cookies',[])
        before=self._session_expiry(cookies)
        self._diag('SESSION_REFRESH_BEGIN',source=source,**_cookie_summary(cookies))
        self._navigate('https://accounts.yanolja.com?service=yanolja&redirectUrl='+quote(url,safe=''))
        account_page=self._page_kind()
        self._navigate(url)
        logged_in=self._logged_in()
        cookies=self.driver.execute_cdp_cmd('Network.getAllCookies',{}).get('cookies',[])
        after=self._session_expiry(cookies)
        self._diag('SESSION_REFRESH_RESULT',source=source,logged_in=int(logged_in),
                   account_page=account_page,page=self._page_kind(),
                   elapsed_ms=int((time.monotonic()-started)*1000),**_cookie_summary(cookies))
        if not logged_in: return False
        self._session_verified()
        if after:
            expiry=dt.datetime.fromtimestamp(after,KST).strftime('%m/%d %H:%M:%S')
            action='NOL 세션 연장 완료' if after>before else 'NOL 로그인 확인 · 만료 시각 유지'
            self.log(action+' · 만료 예정 '+expiry+' (한국 시간)')
        else:
            self.log('NOL 로그인 확인 · 쿠키 만료 시각 미공개')
        return True

    def _recover_login(self,url):
        # 현재 브라우저 로그인을 오래된 저장 쿠키로 덮어쓰지 않는다.
        if self._refresh_session(url):
            self._diag('AUTH_READY',source='live')
            return True
        for source in self._cookie_sources():
            # 쿠키 설치 개수가 아니라 상품 페이지의 실제 로그인 상태로 판단한다.
            label = 'saved' if source == self.cookie_file else 'reference'
            if self._restore(source) and self._refresh_session(url,source=label):
                self._diag('AUTH_READY',source=label)
                return True
        self._diag('LOGIN_REQUIRED',reason='recovery_exhausted')
        self.session_maintenance_blocked = True
        return False

    def _ensure_login(self,url,*,manual=False):
        """세 진입점에서 쿠키 복원 후 같은 카카오 복구를 최대 한 번 사용한다."""
        was_blocked = self.session_maintenance_blocked
        if self._recover_login(url): return True
        if was_blocked and not manual:
            self._diag('KAKAO_RECOVERY_SKIPPED',reason='manual_action_pending')
            return False
        self._diag('KAKAO_RECOVERY_BEGIN',trigger='manual' if manual else 'session_expired')
        self._navigate('https://accounts.yanolja.com?service=yanolja&redirectUrl='+quote(url,safe=''))
        if self._try_kakao_login(url):
            self._session_verified()
            return True
        self.session_maintenance_blocked = True
        return False

    def connect(self,url, *, force=False):
        validate_url(url)
        with self.lock:
            cached = (not force and self.ready_url == url and self.headers
                      and time.monotonic()-self.headers_at < SESSION_REFRESH_SECONDS)
            trigger = 'force_connect' if force else 'connect'
            with self._auth_attempt(trigger,quiet=bool(cached)):
                self._attach(url)
                self._select()
                if (not force and self.ready_url == url and self.headers and time.monotonic()-self.headers_at < SESSION_REFRESH_SECONDS
                        and self.driver.current_url == url):
                    return
                if cached:
                    self._begin_auth(trigger)
                self.headers = {}
                self.ready_url = None
                if not self._ensure_login(url):
                    raise LoginRequired('NOL 세션 갱신과 저장 세션 복원으로 로그인을 확인하지 못했습니다. 전용 Edge에서 로그인 후 세션을 저장하고 감시를 다시 시작하세요')
                self.driver.execute_script(HOOK_JS)
                deadline = time.monotonic()+16
                clicked_at = 0.0
                while time.monotonic()<deadline:
                    headers = self.driver.execute_script("return window.__hcHeaders || {};")
                    if any(key.lower() == "access-token" and value for key,value in headers.items()):
                        self.headers = headers
                        self.headers_at = time.monotonic()
                        self.ready_url = url
                        self._diag('HEADERS_READY')
                        self.save_session()
                        return
                    if time.monotonic()-clicked_at>=3:
                        self.driver.execute_script("""
                          const bs=[...document.querySelectorAll('button')].filter(b =>
                            ['옵션 선택하기','옵션 변경하기'].includes((b.innerText || '').trim()) && b.offsetParent !== null);
                          if(bs.length) bs[bs.length-1].click();
                        """)
                        clicked_at = time.monotonic()
                    time.sleep(0.2)
                self._diag('HEADER_CAPTURE_TIMEOUT',page=self._page_kind())
                raise ApiError("옵션 조회 헤더를 받지 못했습니다. 로그인과 상품의 옵션 선택 화면을 확인하세요")

    def product(self):
        with self.lock,self._auth_attempt('product',quiet=True):
            self._select()
            return self.driver.execute_script("return window.__NEXT_DATA__?.props?.pageProps?.product || null;")

    def request(self,path, *, method="GET",data=None):
        if not path.startswith("/product/v1/") and not re.fullmatch(r"/order/v1/orders/group/[^/?#]+/additional-purchase/verification",path):
            raise ValueError("허용된 상품·추가구매 검증 조회 경로가 아닙니다")
        if method not in ("GET","POST") or method == "POST" and not path.endswith("/additional-purchase/verification"):
            raise ValueError("상품 재고 조회와 추가구매 자격 확인만 허용됩니다")
        with self.lock:
            if self.driver is None or not self.headers:
                raise ApiError("브라우저 연결이 준비되지 않았습니다")
            try:
                self._select()
                response = self.driver.execute_async_script(REQUEST_JS,API_BASE+path,method,self.headers,data)
            except (WebDriverException,HTTPError,OSError) as error:
                self._diag('BROWSER_CONNECTION_LOST',error_type=type(error).__name__)
                self._diag('AUTH_ERROR',error_type=type(error).__name__,last_flag=self.auth_last_flag)
                self.detach()
                raise ApiError("브라우저 조회 연결이 끊겼습니다. 연결을 다시 확인합니다") from None
            if response.get("status") in (-1,-2):
                self.ready_url = None
                self._diag('REQUEST_NETWORK_ERROR',status=response['status'])
            try:
                body=json.loads(response.get('body') or '{}').get('body')
                code=body.get('code') if isinstance(body,dict) else None
            except (ValueError,AttributeError,TypeError):
                code=None
            known_code=code if isinstance(code,str) and code in AUTH_CODES else 'none'
            if response.get('status') == 401 or known_code != 'none':
                endpoint=path.split('?',1)[0]
                kind=('addon' if endpoint.endswith('/additional-purchase/verification') else
                      'rounds' if endpoint.endswith('/rounds') else
                      'schedules' if endpoint.endswith('/schedules') else
                      'product' if re.fullmatch(r'/product/v1/products/\d+',endpoint) else 'other')
                status=response.get('status')
                self._diag('API_AUTH_REJECTED',http=status if isinstance(status,int) else 0,
                           code=known_code,endpoint=kind)
            return response

    def detach(self):
        with self.lock:
            if self.driver is not None:
                try:
                    self.driver.service.stop()
                except (OSError,WebDriverException,HTTPError):
                    pass
            self.driver = None
            self.headers = {}
            self.ready_url = None
