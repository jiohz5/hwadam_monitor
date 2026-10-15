"""화담숲 앱 제어·관리 화면·브라우저 예약 테스트 통합.

수정 위치: 1. 예약 테스트 / 2. 결제 준비·관리 화면 / 3. 폼 변환·앱 제어.
프로그램 실행은 기존 main.py 또는 run_hwadam.bat를 사용한다.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import logging
import json
import hashlib
from pathlib import Path
import queue
import threading
import traceback
import tkinter as tk
from tkinter import messagebox, ttk
import webbrowser

from api import AuthLost, LeisureClient
from browser import BrowserSession
from catalog import Catalogue, PRODUCTS, product_id, sync_catalogues
from eventlog import persist_event
from logfiles import create_app_logger
from model import AGE_NAMES, DEFAULT_URL, REFERENCE_URL, SECTION_NAMES, WatchConfig, count_label, date_label, parse_time_specs, relative_monorail_specs, time_matches, validate_url
from monitor import MonitorRunner
from storage import SettingsStore, atomic_write, load_telegram_secrets, sanitize, save_telegram_secrets
from telegram_bot import TelegramNotifier
from ui_widgets import BG, WHITE, GREEN, MUTED, card, TimeGrid
import os
import time
from typing import Callable
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException, ElementClickInterceptedException


# === 1. 브라우저 예약 테스트 (최종 결제 미실행) ===

LogFn = Callable[[str], None]


class BookingTestError(RuntimeError):
    pass


def run_test_booking(driver, config: WatchConfig, *, log: LogFn = print, age_label: str = "성인",
                     price_keyword: str = "일반", stop_before_pay: bool = True,
                     should_stop: Callable[[], bool] | None = None) -> bool:
    """브라우저에 선택된 날짜에서 첫 시각·한 권종을 시험하고 결제 직전에 끝낸다.

    날짜·수량은 사용자가 브라우저에서 먼저 설정한다. 최종 결제는 지원하지 않는다.
    """
    if not stop_before_pay:
        raise BookingTestError('예약 테스트에서는 최종 결제를 실행할 수 없습니다')

    def checkpoint():
        if should_stop is not None and should_stop():
            raise BookingTestError('프로그램 종료 요청으로 예약 테스트를 중지했습니다')

    def step(message):
        checkpoint()
        log('[TEST-BOOKING] '+message)

    def click(element):
        checkpoint()
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});",element)
        checkpoint()
        try: element.click()
        except ElementClickInterceptedException:
            checkpoint()
            driver.execute_script('arguments[0].click();',element)

    checkpoint()
    config.validate()
    if config.mode!='entry': raise BookingTestError('예약 테스트는 입장권 모드만 지원합니다')
    wait=WebDriverWait(driver,15)
    stage='시간 선택'
    target_time=config.time_specs[0].split('~')[0].strip()
    try:
        step(f'첫 시각 {target_time} · 권종 {age_label} · 날짜·수량은 브라우저의 선택값 사용')
        buttons=driver.find_elements(By.CSS_SELECTOR,'button[role="option"]')
        for button in buttons:
            label=button.get_attribute('aria-label') or ''
            text=button.text or ''
            if target_time not in label and target_time not in text: continue
            if ('품절' in text or '매진' in text or button.get_attribute('aria-disabled')=='true'
                    or not button.is_enabled()): continue
            click(button)
            break
        else: raise BookingTestError(f'구매 가능한 {target_time} 시간 버튼을 찾지 못했습니다')
        step(f'시간 선택 완료: {target_time}')
        time.sleep(.8)

        stage='권종 선택';step(f'권종 선택: 입장권 {age_label}')
        options=wait.until(EC.presence_of_all_elements_located(
            (By.XPATH,f'//button[contains(., "입장권 {age_label}") or contains(., "{age_label}")]')))
        for option in options:
            if age_label in (option.text or '') and option.is_enabled():
                click(option);break
        else: raise BookingTestError(f'권종 {age_label} 버튼을 찾지 못했습니다')
        time.sleep(.6)

        stage='요금 선택';step('요금 옵션 선택: '+price_keyword)
        prices=driver.find_elements(By.XPATH,f'//button[contains(., "{price_keyword}")]')
        for button in prices:
            text=button.text or ''
            if price_keyword in text and ('원' in text or '일반' in text) and button.is_enabled():
                click(button);break
        else: raise BookingTestError(f'{price_keyword} 요금 옵션을 찾지 못했습니다')
        time.sleep(.5)

        stage='구매 화면 이동';step('구매하기 버튼 클릭')
        buy=wait.until(EC.element_to_be_clickable((By.XPATH,'//button[contains(., "구매하기")]')))
        click(buy);time.sleep(2.5)

        stage='결제수단 선택';step('결제수단: NOL 머니 선택')
        money=wait.until(EC.element_to_be_clickable((By.XPATH,
            '//button[contains(., "NOL 머니")] | //label[contains(., "NOL 머니")] | //span[contains(text(), "NOL 머니")]')))
        click(money);time.sleep(.8)

        stage='전체 동의';step('전체 동의 항목 확인')
        # 페이지의 마지막 체크박스를 추측해서 누르지 않는다.
        agreements=driver.find_elements(By.XPATH,
            '//input[@type="checkbox" and contains(translate(@aria-label," ",""),"전체동의")]'
            ' | //label[contains(translate(normalize-space(.)," ",""),"전체동의")]//input[@type="checkbox"]'
            ' | //input[@type="checkbox" and @id=//label[contains(translate(normalize-space(.)," ",""),"전체동의")]/@for]')
        if not agreements: raise BookingTestError('전체 동의라고 표시된 체크박스를 찾지 못했습니다')
        checkbox=agreements[0]
        if not checkbox.is_selected(): click(checkbox)
        time.sleep(.6)

        stage='최종 결제 버튼 확인';step('최종 결제 버튼 활성화 확인 (클릭하지 않음)')
        pay=wait.until(EC.element_to_be_clickable((By.XPATH,'//button[contains(., "결제하기")]')))
        if not pay.is_enabled() or pay.get_attribute('aria-disabled')=='true' or pay.get_attribute('disabled'):
            raise BookingTestError('최종 결제 버튼이 비활성 상태입니다')
        step('최종 결제 버튼 활성화 확인 완료 · 최종 결제 미실행')
        return True
    except BookingTestError:
        raise
    except TimeoutException:
        raise BookingTestError(stage+' 단계의 화면 요소를 찾지 못했습니다') from None
    except Exception as error:
        # 원본 Selenium 오류/URL/응답 본문을 파일 로그에 남기지 않는다.
        raise BookingTestError(stage+' 실패 · '+type(error).__name__) from None


# === 2. 결제 준비·관리 화면 ===

OPTIONS={
    'method':('브라우저에서 직접 선택','Npay','신용/체크카드','기타'),
    'kind':('입장권','입장권 + 모노레일','모노레일 추가구매'),
    'quantity':('현재 목표 수량','1장 부분 가용'),
    'stage':('결제 직전','전체 결제 (PIN까지)'),
}


def scroll_page(parent):
    frame=ttk.Frame(parent);frame.pack(fill='both',expand=True)
    canvas=tk.Canvas(frame,bg=BG,highlightthickness=0)
    frame.scroll_canvas=canvas
    bar=ttk.Scrollbar(frame,orient='vertical',command=canvas.yview);bar.pack(side='right',fill='y')
    canvas.pack(side='left',fill='both',expand=True);canvas.configure(yscrollcommand=bar.set)
    body=ttk.Frame(canvas,padding=(0,0,8,12));window=canvas.create_window((0,0),window=body,anchor='nw')
    body.bind('<Configure>',lambda e:canvas.configure(scrollregion=canvas.bbox('all')))
    canvas.bind('<Configure>',lambda e:canvas.itemconfigure(window,width=e.width))
    return body


def title(parent,text,subtitle=''):
    ttk.Label(parent,text=text,style='Section.TLabel').pack(anchor='w')
    if subtitle:
        ttk.Label(parent,text=subtitle,style='Muted.Card.TLabel',wraplength=800).pack(anchor='w',pady=(5,12))


class OperationsUI:
    def __init__(self,app):
        self.app=app
        self.path=app.base/'operation_settings.json'
        self.admin_enabled=False;self.rehearsing=False;self.index=0;self.steps=[]
        self.admin_widgets=[]
        values=self._load()
        self.vars={key:tk.StringVar(value=values[key]) for key in OPTIONS}
        self.target=tk.StringVar()
        self.setup_status=tk.StringVar()
        self.preview_status=tk.StringVar(value='시나리오를 고른 뒤 GUI 테스트를 시작하세요.')
        self.payment_status=tk.StringVar(value='결제수단과 인증은 실제 브라우저 결제창에서 직접 선택합니다.')
        self.admin_status=tk.StringVar(value='관리 모드 닫힘 · 열기를 누르면 화면 테스트 도구가 활성화됩니다.')
        self._payment(app.pages['payment'])
        self._admin(app.pages['admin'])
        self.refresh_context();self._buttons()

    def _load(self):
        try:
            raw=json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(raw,dict): raw={}
        except (OSError,ValueError): raw={}
        return {key:raw[key] if raw.get(key) in choices else choices[0] for key,choices in OPTIONS.items()}

    def save(self):
        values={key:value.get() for key,value in self.vars.items()}
        if any(values[key] not in choices for key,choices in OPTIONS.items()):
            self.payment_status.set('목록에서 결제 준비 옵션을 선택하세요.')
            self.app._log('[OPERATIONS_SETTINGS_INVALID] 결제 준비 설정 확인 필요 · 목록에서 옵션을 선택하세요.')
            return
        try: atomic_write(self.path,json.dumps(values,ensure_ascii=False,indent=2))
        except OSError as error:
            self.payment_status.set('저장 실패 · '+sanitize(error))
            self.app._log('[OPERATIONS_SETTINGS_SAVE_FAILED] 결제 준비 설정 저장 실패 · '+sanitize(error))
            return
        self.payment_status.set('결제 준비·테스트 시나리오 저장 완료')
        self.app._log('결제 GUI 설정 저장 · 결제수단 선호 및 테스트 시나리오')

    def _payment(self,parent):
        body=scroll_page(parent)
        intro=card(body);intro.pack(fill='x',pady=(0,12))
        title(intro.content,'결제 준비','선택한 티켓을 확인하고 브라우저에서 결제를 직접 진행하는 작업 공간입니다.')
        ttk.Label(intro.content,textvariable=self.target,style='Card.TLabel',wraplength=850).pack(anchor='w',pady=(0,14))
        row=ttk.Frame(intro.content,style='Card.TFrame');row.pack(fill='x')
        ttk.Button(row,text='현재 선택 다시 가져오기',command=self.refresh_context).pack(side='left',padx=(0,8))
        ttk.Button(row,text='상품 페이지 열기 ↗',command=self.app._open_product).pack(side='left')
        form=card(body);form.pack(fill='x',pady=(0,12))
        title(form.content,'수동 결제 설정','실제 결제창에서 제공되는 수단을 직접 선택하세요. 아래 선택은 준비용 설정입니다.')
        row=ttk.Frame(form.content,style='Card.TFrame');row.pack(fill='x',pady=(0,12))
        ttk.Label(row,text='선호 결제수단',style='Card.TLabel').pack(side='left',padx=(0,12))
        ttk.Combobox(row,textvariable=self.vars['method'],values=OPTIONS['method'],state='readonly',width=25).pack(side='left')
        ttk.Label(form.content,text='카드정보 · 본인인증 · PIN',style='Card.TLabel').pack(anchor='w')
        ttk.Label(form.content,text='실제 브라우저 결제창에서 직접 입력',style='Muted.Card.TLabel').pack(anchor='w',pady=(5,14))
        self.save_button=ttk.Button(form.content,text='준비 설정 저장',command=self.save);self.save_button.pack(anchor='w')
        ttk.Label(form.content,textvariable=self.payment_status,style='Muted.Card.TLabel',wraplength=800).pack(anchor='w',pady=(10,0))
        checklist=card(body);checklist.pack(fill='x')
        title(checklist.content,'결제 전 직접 확인','확인란은 개인 점검용이며 실제 주문·결제 상태를 조회하지 않습니다.')
        self.checks=[]
        for text in ('상품 · 이용 날짜 · 입장 회차 확인','성인 · 경로 · 어린이 수량과 최종 금액 확인',
                     '모노레일 구간 · 탑승 시각 확인','할인 · 취소 조건 · 최종 결제 내용 확인'):
            value=tk.BooleanVar();self.checks.append(value)
            ttk.Checkbutton(checklist.content,text=text,variable=value).pack(anchor='w',pady=3)
        ttk.Button(checklist.content,text='관리 모드에서 테스트 준비 →',command=lambda:self.app._show_page('admin')).pack(anchor='w',pady=(12,0))

    def _admin(self,parent):
        body=scroll_page(parent)
        header=card(body);header.pack(fill='x',pady=(0,12))
        title(header.content,'관리 모드','초기셋업 점검과 결제 단계 GUI 테스트를 한곳에서 준비합니다.')
        row=ttk.Frame(header.content,style='Card.TFrame');row.pack(fill='x')
        self.unlock_button=ttk.Button(row,text='관리 모드 열기',command=self.toggle_admin);self.unlock_button.pack(side='left',padx=(0,12))
        ttk.Label(row,textvariable=self.admin_status,style='Muted.Card.TLabel',wraplength=650).pack(side='left')
        setup=card(body);setup.pack(fill='x',pady=(0,12))
        title(setup.content,'초기셋업 점검')
        ttk.Label(setup.content,textvariable=self.setup_status,style='Card.TLabel',wraplength=850).pack(anchor='w',pady=(8,12))
        row=ttk.Frame(setup.content,style='Card.TFrame');row.pack(fill='x')
        for text,command in (('초기셋업 점검',self.refresh_context),('로그인 · 세션 설정',lambda:self.app._show_page('settings')),('로그 폴더 열기',self.open_logs)):
            button=ttk.Button(row,text=text,command=command);button.pack(side='left',padx=(0,8));self.admin_widgets.append(button)
        scenario=card(body);scenario.pack(fill='x',pady=(0,12))
        title(scenario.content,'테스트 시나리오','티켓 종류·수량·결제 단계를 골라 확인할 화면 순서를 점검하세요. 테스트 수행은 이 화면에서만 진행합니다.')
        row=ttk.Frame(scenario.content,style='Card.TFrame');row.pack(fill='x')
        for index,(key,label) in enumerate((('kind','티켓 종류'),('quantity','수량 시나리오'),('stage','확인할 결제 단계'))):
            cell=ttk.Frame(row,style='Card.TFrame');cell.grid(row=0,column=index,sticky='ew',padx=(0,12));row.columnconfigure(index,weight=1)
            ttk.Label(cell,text=label,style='Muted.Card.TLabel').pack(anchor='w',pady=(0,5))
            widget=ttk.Combobox(cell,textvariable=self.vars[key],values=OPTIONS[key],state='readonly',width=22)
            widget.pack(fill='x');self.admin_widgets.append(widget)
        row=ttk.Frame(scenario.content,style='Card.TFrame');row.pack(fill='x',pady=(14,0))
        self.run_button=ttk.Button(row,text='테스트 수행 (GUI)',style='Accent.TButton',command=self.start_preview);self.run_button.pack(side='left',padx=(0,8))
        self.next_button=ttk.Button(row,text='다음 단계 확인',command=self.next_step);self.next_button.pack(side='left',padx=(0,8))
        self.stop_button=ttk.Button(row,text='테스트 종료',command=self.stop_preview);self.stop_button.pack(side='left',padx=(0,8))
        button=ttk.Button(row,text='시나리오 저장',command=self.save);button.pack(side='left');self.admin_widgets.append(button)
        # 로그인된 브라우저의 결제 직전 동선 테스트
        test_pay_row=ttk.Frame(scenario.content,style='Card.TFrame');test_pay_row.pack(fill='x',pady=(12,0))
        self.test_pay_button=ttk.Button(test_pay_row,text='테스트 결제 (브라우저)',style='Accent.TButton',command=self.start_test_booking)
        self.test_pay_button.pack(side='left',padx=(0,8))
        self.test_pay_status=tk.StringVar(value='첫 시각·첫 권종만 테스트 · 날짜·수량은 브라우저에서 먼저 선택 · 최종 결제 미실행')
        ttk.Label(test_pay_row,textvariable=self.test_pay_status,style='Muted.Card.TLabel',wraplength=700).pack(side='left')
        self.admin_widgets.append(self.test_pay_button)
        preview=card(body);preview.pack(fill='x')
        title(preview.content,'결제 동선 미리보기','실제 결제 테스트는 사용자가 브라우저에서 직접 진행합니다.')
        self.flow=ttk.Treeview(preview.content,columns=('step','state'),show='headings',height=6,selectmode='none')
        self.flow.heading('step',text='확인할 화면');self.flow.heading('state',text='GUI 확인 상태')
        self.flow.column('step',width=550);self.flow.column('state',width=180)
        self.flow.pack(fill='x')
        ttk.Label(preview.content,textvariable=self.preview_status,style='Card.TLabel',wraplength=850).pack(anchor='w',pady=(12,0))

    def refresh_context(self):
        app=self.app
        counts=' · '.join(f'{label} {app.counts[age].get()}장' for age,label in AGE_NAMES.items() if app.counts[age].get() not in ('','0'))
        lines=[app.product_title.get(),app.vars['date'].get()+'  |  '+counts]
        if app.vars['mode'].get()=='addon':
            lines.append('모노레일 추가구매 · '+app.vars['section'].get()+' · '+(app.vars['times'].get() or '회차 미선택'))
        else:
            lines.append('입장: '+(app.vars['times'].get() or '회차 미선택'))
            if app.vars['include_monorail'].get():
                timing='입장 +20분 ±15분' if app.vars['mono_relative'].get() else app.vars['mono_times'].get() or '가까운 후보 확인'
                lines.append('모노레일: '+app.vars['section'].get()+' · '+timing)
        self.target.set('\n'.join(lines))
        saved=sum(app.catalogue.summary(pid)[0] for pid in app.product_stats)
        session='저장 파일 있음 · 실제 로그인은 연결 시 확인' if (app.base/'session_cookies.json').is_file() else '로그인 세션 저장 필요'
        telegram='입력됨 · 전송 여부는 알림 테스트로 확인' if app.vars['token'].get().strip() and app.vars['chat'].get().strip() else '봇 토큰 · 채팅 ID 설정 필요'
        self.setup_status.set(f'NOL 세션: {session}\n상품 목록: {saved}일 저장  |  텔레그램: {telegram}')

    def toggle_admin(self):
        self.admin_enabled=not self.admin_enabled
        if not self.admin_enabled and self.rehearsing: self.stop_preview()
        self.unlock_button.configure(text='관리 모드 닫기' if self.admin_enabled else '관리 모드 열기')
        self.admin_status.set('관리 도구 활성화 · 로컬 화면 테스트' if self.admin_enabled else '관리 모드 닫힘')
        self.app._log('[ADMIN_MODE_OPENED] 관리 모드 열림' if self.admin_enabled else '[ADMIN_MODE_CLOSED] 관리 모드 닫힘')
        self._buttons()

    def _buttons(self):
        working=self.app.busy or self.app.runner.running
        for widget in self.admin_widgets:
            enabled=self.admin_enabled and not self.rehearsing and not working
            widget.configure(state=('readonly' if isinstance(widget,ttk.Combobox) else 'normal') if enabled else 'disabled')
        self.run_button.configure(state='normal' if self.admin_enabled and not self.rehearsing and not working else 'disabled')
        for widget in (self.next_button,self.stop_button): widget.configure(state='normal' if self.rehearsing else 'disabled')

    def start_test_booking(self):
        """첫 시각·첫 권종의 브라우저 동선을 공유 작업 큐에서 시험한다."""
        app=self.app
        if not self.admin_enabled:
            app._log('관리 모드를 먼저 열어주세요');return
        if app.closed or app.busy or app.session_pending or app.runner.running or self.rehearsing:
            app._log('다른 작업이 실행 중입니다');return
        try:
            config=app.read_config()
            if config.mode!='entry': raise ValueError('브라우저 테스트는 입장권 모드만 지원합니다')
        except ValueError as error:
            app._log('예약 테스트 설정 오류 · '+sanitize(error))
            self.test_pay_status.set('설정 확인 필요');return
        age_label=AGE_NAMES[config.selected_ages[0]]
        self.test_pay_status.set('브라우저 테스트 진행 중 · 최종 결제 미실행')
        app._log(f'[TEST-BOOKING] 시작 · 첫 시각 {config.time_specs[0].split("~")[0]} · 권종 {age_label} · 날짜·수량은 브라우저 선택값')
        app._lock_conditions(True)

        def job():
            browser=app.browser
            try:
                with app.client.lock, browser.lock:
                    if app.closed: raise BookingTestError('프로그램 종료 요청으로 예약 테스트를 중지했습니다')
                    if browser.driver is None:
                        raise BookingTestError('브라우저가 연결되지 않았습니다. 먼저 로그인 브라우저를 열어주세요')
                    browser._select()
                    actual=(browser.driver.current_url or '').split('?')[0].rstrip('/')
                    expected=config.url.split('?')[0].rstrip('/')
                    if actual!=expected:
                        raise BookingTestError('대상 상품 페이지에서 날짜와 수량을 먼저 선택하세요')
                    browser.headers={};browser.ready_url=None
                    return run_test_booking(browser.driver,config,age_label=age_label,stop_before_pay=True,
                        should_stop=lambda:app.closed,
                        log=lambda message:app._emit({'kind':'log','message':message}))
            except BookingTestError: raise
            except Exception as error:
                raise BookingTestError('브라우저 테스트 실패 · '+type(error).__name__) from None

        app._submit(job,'test_pay_done')

    def start_preview(self):
        if not self.admin_enabled or self.rehearsing or self.app.busy or self.app.runner.running: return
        self.refresh_context()
        self.steps=['상품 · 날짜 · 권종 수량 확인']
        if self.vars['kind'].get()!='입장권': self.steps.append('모노레일 구간 · 탑승 시각 확인')
        self.steps.extend(['주문 내용 · 최종 금액 확인','결제수단 선택 · 결제 직전'])
        if self.vars['stage'].get()=='전체 결제 (PIN까지)':
            self.steps.extend(['약관 · 결제 내용 확인','본인인증 · PIN 직접 입력','브라우저에서 결제 결과 확인'])
        self.flow.delete(*self.flow.get_children())
        for index,step in enumerate(self.steps): self.flow.insert('','end',iid=str(index),values=(step,'확인 중' if index==0 else '대기'))
        self.index=0;self.rehearsing=True
        self.preview_status.set('GUI 점검 중 · '+self.vars['kind'].get()+' / '+self.vars['quantity'].get()+' / '+self.vars['stage'].get())
        self.app._log('관리모드 GUI 점검 시작 · 브라우저 동작 없음');self._buttons()

    def next_step(self):
        if not self.rehearsing: return
        self.flow.item(str(self.index),values=(self.steps[self.index],'UI 확인'))
        self.index+=1
        if self.index==len(self.steps):
            self.rehearsing=False;self.preview_status.set('화면 점검 완료 · 실제 결제 테스트는 브라우저에서 직접 진행하세요.')
            self.app._log('관리모드 화면 점검 완료')
        else:
            self.flow.item(str(self.index),values=(self.steps[self.index],'확인 중'));self.flow.see(str(self.index))
            self.preview_status.set('GUI 점검 중 · '+self.steps[self.index])
        self._buttons()

    def stop_preview(self):
        if not self.rehearsing: return
        self.flow.item(str(self.index),values=(self.steps[self.index],'중지'))
        self.rehearsing=False;self.preview_status.set('GUI 테스트 종료 · 외부 주문·결제 상태와 무관합니다.')
        self.app._log('관리모드 GUI 점검 종료');self._buttons()

    def open_logs(self):
        try: os.startfile(str(self.app.base/'logs'))
        except OSError as error: self.app._problem(error)


# === 3. 설정 변환·앱 제어 ===

class QueryGuard:
    def __init__(self): self.revision=0
    def changed(self): self.revision+=1
    def ticket(self): return self.revision
    def accepts(self,ticket): return ticket==self.revision


def config_from_form(form):
    try:
        return WatchConfig(mode=form['mode'],product_url=form['product_url'].strip(),addon_url=form.get('addon_url','').strip(),
            date=form['date'].strip()[:10],time_specs=parse_time_specs(form['times']),
            counts={age:int(form['counts'].get(age,0)) for age in AGE_NAMES},
            section=form.get('section','MONORAIL_SEGMENT_2'),include_monorail=bool(form.get('include_monorail',False)),
            mono_specs=parse_time_specs(form.get('mono_times','')),mono_relative=bool(form.get('mono_relative',False)),interval=float(form.get('interval',10)),
            repeat_seconds=float(form.get('repeat',0))).validate()
    except (ValueError,TypeError) as error:
        raise ValueError('설정을 확인하세요: '+str(error)) from None


class App:
    def __init__(self,root,base_dir):
        self.root=root
        self.base=Path(base_dir)
        self.closed=False
        self.busy=False
        self.session_pending=False
        self.syncing=False
        self.catalog_stop=threading.Event()
        self.mutating=False
        self.events=queue.Queue()
        self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='hwadam-query')
        self.pending_jobs=set()
        self.jobs_lock=threading.Lock()
        self._event_persisted=False
        self._status_keys={}
        self.guard=QueryGuard()
        self.store=SettingsStore(self.base)
        config,warning=self.store.load()
        self.catalogue=Catalogue(self.base)
        self.choices_file=self.base/'browse_choices.json'
        try:
            self.choices=json.loads(self.choices_file.read_text(encoding='utf-8'))
            if not isinstance(self.choices.get('times'),dict) or not isinstance(self.choices.get('dates'),dict): raise ValueError()
            self.choices['times']={key:value for key,value in self.choices['times'].items()
                if isinstance(value,dict) and all(isinstance(value.get(field,''),str) for field in ('times','mono_times'))}
        except (OSError,ValueError,TypeError,AttributeError): self.choices={'times':{},'dates':{}}
        token,chat=load_telegram_secrets(self.base)
        self.vars={
            'mode':tk.StringVar(value=config.mode),'product_url':tk.StringVar(value=config.product_url),
            'addon_url':tk.StringVar(value=config.addon_url),'date':tk.StringVar(value=date_label(config.date)),
            'times':tk.StringVar(value=', '.join(config.time_specs)),'section':tk.StringVar(value=SECTION_NAMES[config.section]),
            'include_monorail':tk.BooleanVar(value=config.include_monorail),'mono_times':tk.StringVar(value=', '.join(config.mono_specs)),
            'mono_relative':tk.BooleanVar(value=config.mono_relative),
            'interval':tk.StringVar(value=f'{config.interval:g}'),'repeat':tk.StringVar(value=f'{config.repeat_seconds:g}'),
            'token':tk.StringVar(value=token),'chat':tk.StringVar(value=chat)}
        self.counts={age:tk.StringVar(value=str(config.counts.get(age,0))) for age in AGE_NAMES}
        self.primary_times=[]
        self.secondary_times=[]
        self.condition_widgets=[]
        self.widget_states={}
        self.status=tk.StringVar(value='대기 · 조건을 확인하고 브라우저를 연결하세요')
        self.session_status=tk.StringVar(value='저장된 세션 있음 · 연결 시 자동 확인' if (self.base/'session_cookies.json').is_file() else '로그인 세션 미저장 · 로그인 후 세션 저장하세요')
        self.detail=tk.StringVar(value='성인 2 · 경로 2 · 어린이 2 | 가용 1장부터 알림')
        self.primary_title=tk.StringVar(value='입장 회차 · 여러 개 선택 가능')
        self.product_title=tk.StringVar(value=(self.catalogue.product(product_id(config.product_url)) or {}).get('name','화담숲 입장권'))
        self.catalog_stamp=tk.StringVar(value='저장된 회차 목록을 불러오는 중')
        self.mono_stamp=tk.StringVar(value='선택 날짜의 모노레일 목록을 불러오는 중')
        self.date_summary=tk.StringVar(value=date_label(config.date))
        self.selection_count=tk.StringVar(value='선택한 회차')
        self.selection_summary=tk.StringVar(value='')
        self.order_label=tk.StringVar(value='추가구매는 입장권 주문에서 연 모노레일 주소를 붙여넣으세요.')
        self.logger,self.log_handler=create_app_logger(self.base)
        self.root.report_callback_exception=self._callback_exception
        self.logger.info('프로그램 시작 · GUI',extra={'event':'APP_STARTED'})
        for variable,code in ((self.status,'UI_STATUS'),(self.session_status,'SESSION_STATUS')):
            variable.trace_add('write',lambda *_,value=variable,event_code=code:self._status_changed(value,event_code))
        self.browser=BrowserSession(self.base,lambda text:self._emit({'kind':'log','message':text}))
        self.client=LeisureClient(self.browser)
        self.runner=MonitorRunner(self.client,self._emit)
        self.browse_key=self._choice_key()
        previous=self.choices['times'].get(self.browse_key)
        if previous:
            self.vars['times'].set(previous.get('times',''))
            self.vars['mono_times'].set(previous.get('mono_times',''))
        self._build()
        for key,value in self.vars.items():
            if key not in ('token','chat'):
                value.trace_add('write',lambda *_,field=key:self._condition_changed(field))
        for value in self.counts.values(): value.trace_add('write',lambda *_:self._condition_changed('counts'))
        self._mode_changed()
        self.root.protocol('WM_DELETE_WINDOW',self.close)
        self.poll_id=self.root.after(100,self._poll)
        self.keepalive_id=self.root.after(30000,self._keep_session_alive)
        self._log(warning or self.catalogue.warning or '준비 완료 · 상품을 선택하고 날짜와 회차를 체크하세요')

    def _show_page(self,name):
        if name in ('payment','admin') and hasattr(self,'operations'): self.operations.refresh_context()
        for key,page in self.pages.items():
            if key==name: page.grid()
            else: page.grid_remove()
        for key,button in self.nav_buttons.items():
            button.configure(bg='#315642' if key==name else '#173A30')

    def _choice_key(self):
        source=product_id(self.vars['product_url'].get())
        if self.vars['mode'].get()=='addon':
            source=hashlib.sha256(self.vars['addon_url'].get().strip().encode('utf-8')).hexdigest()
        return '|'.join((source,self.vars['mode'].get(),
                         self.vars['date'].get()[:10],self.vars['section'].get()))

    def _remember(self):
        self.choices['times'][self.browse_key]={'times':self.vars['times'].get(),'mono_times':self.vars['mono_times'].get()}
        self.choices['dates'][product_id(self.vars['product_url'].get())]=self.vars['date'].get()[:10]

    def _condition_changed(self,key):
        self.guard.changed()
        if self.mutating: return
        if key in ('product_url','date','mode','section','addon_url'):
            self._remember()
            self.browse_key=self._choice_key()
            previous=self.choices['times'].get(self.browse_key,{})
            self.mutating=True
            if key!='section' or self.vars['mode'].get()!='entry':
                self.vars['times'].set(previous.get('times',''))
            self.vars['mono_times'].set(previous.get('mono_times',''))
            self.mutating=False
            self._clear_times()
            self._cached_view()
        else:
            self._remember()
            if key=='mono_relative':
                if self.vars['mono_relative'].get(): self.vars['include_monorail'].set(True)
                self._mode_changed(clear=False)
            if key in ('times','mono_times','mono_relative'):
                self._sync_checks()
        self._update_summary()

    def _choose_product(self,pid):
        if self.runner.running: return
        self._remember()
        product=self.catalogue.product(pid)
        if not product: return
        date=self.choices['dates'].get(pid) or product.get('default_date') or product['start']
        if not isinstance(date,str) or not product['start']<=date<=product['end']: date=product['start']
        self.mutating=True
        self.vars['product_url'].set(product['url']);self.vars['date'].set(date_label(date))
        self.browse_key=self._choice_key()
        previous=self.choices['times'].get(self.browse_key,{})
        self.vars['times'].set(previous.get('times',''))
        self.vars['mono_times'].set(previous.get('mono_times',''))
        self.mutating=False
        self._clear_times();self._cached_view();self._update_summary();self._show_page('explore')

    def _update_product_stats(self):
        current=product_id(self.vars['product_url'].get())
        for pid,var in self.product_stats.items():
            saved,total=self.catalogue.summary(pid)
            var.set(f'{saved}일 회차 저장'+(f' / 예약 {total}일' if total else '') if saved else '날짜별 회차 목록 미확보')
            self.product_buttons[pid].configure(bg='#3C634A' if pid==current else '#214638')

    def _cached_view(self):
        pid=product_id(self.vars['product_url'].get());date=self.vars['date'].get()[:10]
        product=self.catalogue.product(pid) or {'name':'화담숲 입장권','start':'','end':'','dates':[],'days':{}}
        self.product_title.set(product['name'])
        self.calendar.set_context(product,date)
        values=product.get('dates') or []
        self.date_combo.configure(values=[date_label(value) for value in values])
        self._update_product_stats()
        if self.vars['mode'].get()=='addon':
            self.catalog_stamp.set('주문 링크로 연결하면 추가구매 가능한 실제 회차를 표시합니다.')
            self._update_summary();return
        record=self.catalogue.rounds(pid,date)
        if record is not None:
            self._fill_list(self.primary_list,record['rows'],parse_time_specs(self.vars['times'].get()),'primary')
            stamp=record['observed_at'].replace('T',' ')[:16]
            self.catalog_stamp.set(f"저장된 목록 · {stamp} 확인 · {len(record['rows'])}회차\n현재 재고는 감시 시작 후 확인합니다.")
        else:
            self.catalog_stamp.set('회차 목록 미확보 · 「두 상품 목록 확보」 또는 「이 날짜 새로고침」을 눌러주세요.')
        section=next((code for code,label in SECTION_NAMES.items() if label==self.vars['section'].get()),'')
        mono=self.catalogue.rounds(pid,date,section=section) if section else None
        if mono is not None:
            self._fill_list(self.secondary_list,mono['rows'],parse_time_specs(self.vars['mono_times'].get()),'secondary')
            self.mono_stamp.set(f"{date_label(date)} · {len(mono['rows'])}회차\n{mono['parent_time'] or '당일 첫'} 입장 기준 저장 목록")
        else: self.mono_stamp.set('이 날짜의 모노레일 목록 미확보\n「이 날짜 새로고침」으로 읽어주세요.')
        self._sync_checks()
        self._update_summary()

    def _display_mono_specs(self):
        if self.vars['mono_relative'].get() and self.vars['mode'].get()=='entry':
            selected=[time for time in self.primary_times if time_matches(time,parse_time_specs(self.vars['times'].get()))]
            return tuple(spec for time in selected for spec in relative_monorail_specs(time))
        return parse_time_specs(self.vars['mono_times'].get())

    def _sync_checks(self):
        for widget,key in ((self.primary_list,'times'),(self.secondary_list,'mono_times')):
            specs=self._display_mono_specs() if key=='mono_times' else parse_time_specs(self.vars[key].get())
            widget.selected={time for time,_ in widget.items if time_matches(time,specs)}
            widget.render()

    def _update_summary(self):
        value=self.vars['date'].get()[:10]
        try: self.date_summary.set(date_label(value))
        except ValueError: self.date_summary.set('날짜를 선택하세요')
        selected=[time for time in self.primary_times if time_matches(time,parse_time_specs(self.vars['times'].get()))]
        mono=[time for time in self.secondary_times if time_matches(time,self._display_mono_specs())]
        def label(times,fallback):
            return (', '.join(times[:3])+(f' 외 {len(times)-3}개' if len(times)>3 else '')) if times else fallback
        text=label(selected,self.vars['times'].get() or '미선택')
        if self.vars['mode'].get()=='addon':
            self.selection_count.set(f'모노레일 {len(selected)}개 회차 선택')
            self.selection_summary.set(text)
        else:
            self.selection_count.set(f'입장 {len(selected)}개 · 모노레일 {len(mono)}개 선택')
            fallback='가까운 후보 확인' if self.vars['include_monorail'].get() else '미선택'
            mono_label='입장 +20분 ±15분' if self.vars['mono_relative'].get() else label(mono,fallback)
            self.selection_summary.set('입장: '+text+'\n모노레일: '+mono_label)

    def _sync_catalogues(self):
        if self.busy or self.runner.running: return
        self.syncing=True;self.catalog_stop.clear()
        self.status.set('두 상품의 날짜·회차 목록을 확보하는 중…')
        def job():
            with self.client.lock:
                return sync_catalogues(self.client,self.catalogue,self.catalog_stop,self._emit,resume=True)
        self._submit(job,'catalog_done')

    def _condition(self,widget):
        self.condition_widgets.append(widget)
        self.widget_states[widget]=str(widget.cget('state')) if 'state' in widget.keys() else 'normal'
        return widget

    def _entry(self,parent,key,**kwargs):
        widget=ttk.Entry(parent,textvariable=self.vars[key],**kwargs)
        self._condition(widget)
        return widget

    def _build(self):
        from ui_layout import build
        build(self,OperationsUI)

    def _wheel(self,event):
        widget=event.widget
        while widget is not None:
            scroller=getattr(widget,'scroll_canvas',None)
            if scroller is not None:
                scroller.yview_scroll(-int(event.delta/120),'units');return 'break'
            if isinstance(widget,TimeGrid):
                widget.scroll_units(-int(event.delta/120));return 'break'
            if widget==self.left_content or widget==self.canvas:
                self.canvas.yview_scroll(-int(event.delta/120),'units'); return 'break'
            widget=getattr(widget,'master',None)

    def read_config(self):
        values={key:value.get() for key,value in self.vars.items()}
        values['section']=next((code for code,label in SECTION_NAMES.items() if label==values['section']),'')
        values['counts']={age:value.get() for age,value in self.counts.items()}
        return config_from_form(values)

    def _mode_changed(self,clear=True):
        addon=self.vars['mode'].get()=='addon'
        self.primary_title.set('모노레일 추가구매 회차' if addon else '입장 회차')
        if not self.runner.running:
            self.addon_entry.configure(state='normal' if addon else 'disabled')
            self.mono_check.configure(state='disabled' if addon else 'normal')
            self.relative_check.configure(state='disabled' if addon else 'normal')
            enabled=not addon and not self.vars['mono_relative'].get()
            self.mono_entry.configure(state='normal' if enabled else 'disabled')
            self.secondary_list.configure(state='normal' if enabled else 'disabled')
            self.calendar.set_enabled(not addon)
        if addon:
            self.addon_card.pack(fill='x',pady=(0,12),before=self.date_card)
        else: self.addon_card.pack_forget()
        if addon:
            self.mono_card.grid_remove();self.primary_card.grid_configure(columnspan=2,padx=0)
        else:
            self.mono_card.grid();self.primary_card.grid_configure(columnspan=1,padx=(0,6))
        if clear: self._clear_times();self._cached_view()

    def _clear_times(self):
        for widget in (self.primary_list,self.secondary_list):
            state=widget.cget('state'); widget.configure(state='normal'); widget.delete(0,'end'); widget.configure(state=state)
        self.primary_times=[]; self.secondary_times=[]
        self.table.delete(*self.table.get_children())
        self.extra_label.set('조건 변경 후 회차 새로고침으로 실제 목록을 읽으세요.')

    def _quick_range(self):
        self.vars['times'].set('08:00~08:20')

    def _selected(self,kind):
        widget,times,key=(self.primary_list,self.primary_times,'times') if kind=='primary' else (self.secondary_list,self.secondary_times,'mono_times')
        if times:
            self.vars[key].set(', '.join(times[index] for index in widget.curselection()))
            if kind=='secondary' and widget.curselection(): self.vars['include_monorail'].set(True)

    @staticmethod
    def _round_label(row):
        status=row.get('roundStatusCode')
        return ('품절' if status=='SOLD_OUT' or row.get('inventoryQuantity')==0 else
                '판매 중' if status in ('IN_SALE','OPEN','AVAILABLE') else '판매 상태 확인 필요')

    def _emit(self,event):
        # Tk 큐가 지연되거나 창이 닫혀도 작업 결과는 발생 시점에 저장한다.
        persist_event(self.logger,event)
        if not self.closed: self.events.put({**event,'_persisted':True})

    def _callback_exception(self,error_type,error,tb):
        locations=';'.join(f'{Path(frame.f_code.co_filename).name}:{line}:{frame.f_code.co_name}'
                           for frame,line in traceback.walk_tb(tb))
        self.logger.error('화면 처리 오류 error_type=%s location=%s',error_type.__name__,locations,
                          extra={'event':'GUI_CALLBACK_FAILED'})

    def _background(self,work):
        future=self.executor.submit(work)
        with self.jobs_lock: self.pending_jobs.add(future)
        def completed(done):
            with self.jobs_lock: self.pending_jobs.discard(done)
        future.add_done_callback(completed)

    def _status_changed(self,variable,code):
        text=sanitize(variable.get())
        key='NOL 연결 정상' if text.startswith('NOL 연결 정상 · ') else text
        previous=self._status_keys.get(code)
        self._status_keys[code]=key
        if key!=previous and not self._event_persisted:
            self.logger.info(text,extra={'event':code})

    def _submit(self,job,kind,*,ticket=None):
        self.busy=True; self._buttons()
        def work():
            try: event={'kind':kind,'value':job(),'ticket':ticket}
            except Exception as error: event={'kind':'job_error','operation':kind,'message':sanitize(error),'ticket':ticket,'auth':isinstance(error,AuthLost)}
            self._emit(event)
        self._background(work)

    def _session_url(self):
        addon=self.vars['mode'].get()=='addon' and self.vars['addon_url'].get().strip()
        return validate_url(addon or self.vars['product_url'].get(),order=bool(addon))

    def _open_login(self):
        if self.busy or self.runner.running: return
        try: url=self._session_url()
        except ValueError as error: self._problem(error); return
        self.status.set('전용 로그인 브라우저를 여는 중…')
        self._submit(lambda:self.browser.open_login(url),'login_opened')

    def _save_session(self):
        if self.busy or self.runner.running: return
        try: url=self._session_url()
        except ValueError as error: self._problem(error); return
        self.status.set('로그인 확인 후 세션을 저장하는 중…')
        self._submit(lambda:self.browser.save_login_session(url),'session_saved')

    def _keep_session_alive(self):
        if self.closed: return
        self.root.after_cancel(self.keepalive_id)
        self.keepalive_id=self.root.after(60000,self._keep_session_alive)
        # 감시 루프는 자체 갱신을 수행한다. 로그인·탐색 작업 중에는 이동하지 않는다.
        if self.busy or self.runner.running or self.session_pending: return
        try: url=validate_url(self.vars['product_url'].get())
        except ValueError: url=DEFAULT_URL
        self.session_pending=True
        def work():
            event={'kind':'session_maintenance','value':{'state':'busy'}}
            if not self.closed and self.client.lock.acquire(blocking=False):
                try:
                    if not self.closed:
                        event['value']=self.browser.maintain_session(url)
                except Exception as error:
                    event={'kind':'session_maintenance','message':sanitize(error),'auth':isinstance(error,AuthLost)}
                finally:
                    self.client.lock.release()
            self._emit(event)
        self._background(work)

    def _show_session_maintenance(self,event):
        self.session_pending=False
        if 'message' in event:
            self.session_status.set('로그인 유지 중단 · 전용 Edge에서 다시 로그인하세요' if event.get('auth')
                                    else '로그인 유지 확인 지연 · 자동 재시도 대기')
            self._log('로그인 유지 실패 · '+event['message'])
            return
        result=event['value']
        if result['state'] not in ('checked','renewed'): return
        expiry=result.get('expires_at',0)
        suffix=('만료 예정 '+dt.datetime.fromtimestamp(expiry,dt.timezone(dt.timedelta(hours=9))).strftime('%m/%d %H:%M:%S')
                if expiry else '만료 시각 미공개')
        prefix='로그인 유지 확인' if result['state']=='renewed' else '세션 쿠키 점검 '+dt.datetime.now().strftime('%H:%M')
        self.session_status.set(prefix+' · '+suffix)

    def _browse_config(self):
        values={key:value.get() for key,value in self.vars.items()}
        values['section']=next((code for code,label in SECTION_NAMES.items() if label==values['section']),'')
        values['counts']=({age:value.get() for age,value in self.counts.items()} if values['mode']=='addon'
                          else {age:1 for age in AGE_NAMES})
        values.update(times='00:00~23:59',mono_times='',interval=10,repeat=0)
        return config_from_form(values)

    def _load(self,refresh):
        if self.busy or self.runner.running: return
        try: config=self._browse_config()
        except ValueError as error: self._problem(error); return
        self.guard.changed(); ticket=self.guard.ticket()
        self.status.set('상품·날짜·회차를 읽는 중…')
        def job():
            with self.client.lock:
                catalog=self.client.prepare(config,refresh=refresh)
                dates=self.client.schedules(config)
                mono=config.mode=='addon'
                parent=self.client._order.entry_time if mono else None
                primary=self.client.rounds(config,monorail=mono,entry_time=parent)
                secondary=[]; extra_error=''
                if not mono:
                    try:
                        parent=min(row['time'] for row in primary) if primary else None
                        if parent: secondary=self.client.rounds(config,monorail=True,entry_time=parent)
                    except AuthLost: raise
                    except Exception as error: extra_error=sanitize(error)
                if not mono:
                    self.catalogue.put_catalog(catalog,dates)
                    self.catalogue.put_rounds(catalog.id,config.date,primary)
                    if not extra_error:
                        self.catalogue.put_rounds(catalog.id,config.date,secondary,section=config.section,parent_time=parent or '',ages=config.selected_ages)
                return config,catalog,dates,primary,secondary,self.client._order,extra_error
        self._submit(job,'loaded',ticket=ticket)

    def _show_loaded(self,value):
        config,catalog,dates,primary,secondary,order,extra_error=value
        self.date_combo.configure(values=[date_label(row['date']) for row in dates if row.get('date')])
        self.product_title.set(catalog.name)
        self._fill_list(self.primary_list,primary,parse_time_specs(self.vars['times'].get()),'primary')
        self._fill_list(self.secondary_list,secondary,parse_time_specs(self.vars['mono_times'].get()),'secondary')
        self.status.set('연결 완료 · 감시 시작을 누르세요')
        self.session_status.set('로그인 확인 · 세션 자동 저장 완료')
        self.detail.set(catalog.name+' | '+date_label(config.date)+' | 회차 목록 조회 완료')
        self.order_label.set(('주문 확인: '+order.entry_time+' 입장 · '+count_label(order.counts)) if order else '추가구매는 입장권 주문에서 연 모노레일 주소를 붙여넣으세요.')
        matches=[row for row in primary if time_matches(row['time'],parse_time_specs(self.vars['times'].get()))]
        self.extra_label.set(extra_error or (f'목표 범위에 실제 {len(matches)}회차 · 권종 가용 여부는 감시 시작 후 확인' if matches else '목표 시간에 실제 회차가 없습니다. 범위 또는 날짜를 확인하세요.'))
        self._log(f'목록 읽기 완료 · 날짜 {len(dates)}개 · 회차 {len(primary)}개 · 목표 일치 {len(matches)}개')
        self.catalog_stamp.set(f"방금 조회한 목록 · {len(primary)}회차 · 품절 회차도 감시 대상으로 선택할 수 있습니다.")
        self.mono_stamp.set(extra_error or f'{date_label(config.date)} · {len(secondary)}회차\n당일 첫 입장 기준 · 방금 조회')
        product=self.catalogue.product(str(catalog.id))
        if product: self.calendar.set_context(product,config.date)
        self._sync_checks();self._update_product_stats();self._update_summary()

    def _fill_list(self,widget,rows,specs,kind):
        times=[row['time'] for row in rows]
        widget.set_rows(rows,[time for time in times if time_matches(time,specs)])
        if kind=='primary': self.primary_times=times
        else: self.secondary_times=times

    def _save(self,show=True):
        try:
            config=self.read_config(); self.store.save(config)
            save_telegram_secrets(self.base,self.vars['token'].get(),self.vars['chat'].get())
            self._remember();atomic_write(self.choices_file,json.dumps(self.choices,ensure_ascii=False,indent=2))
            if show: self._log('설정 저장 완료 · hwadam_codex 폴더')
            return config
        except (ValueError,OSError) as error:
            self._problem(error); return None

    def _start(self):
        if self.busy or self.runner.running: return
        try:
            notifier=TelegramNotifier(self.vars['token'].get(),self.vars['chat'].get()).validate()
            config=self.read_config()
        except (ValueError,RuntimeError) as error: self._problem(error); return
        if self._save(False) is None: return
        self.guard.changed()
        self.status.set('감시 준비 중…')
        self.detail.set(date_label(config.date)+' | '+', '.join(config.time_specs)+' | '+count_label(config.counts))
        self.runner.start(config,notifier)
        self._lock_conditions(True); self._buttons()

    def _stop(self):
        if self.syncing:
            self.catalog_stop.set();self.status.set('목록 확보 중지 요청 · 이미 읽은 목록은 저장됩니다')
            self.stop_button.configure(state='disabled');return
        self.runner.stop(); self.status.set('중지 요청 · 진행 중인 조회가 끝나면 종료됩니다')
        self.stop_button.configure(state='disabled')

    def _lock_conditions(self,locked):
        for widget in self.condition_widgets:
            widget.configure(state='disabled' if locked else self.widget_states[widget])
        if not locked: self._mode_changed(clear=False)
        else: self.calendar.set_enabled(False)

    def _buttons(self):
        running=self.runner.running
        state='disabled' if self.busy or running else 'normal'
        for widget in (self.catalog_button,self.login_button,self.session_button,self.connect_button,self.refresh_button,self.start_button,self.test_button,self.save_button): widget.configure(state=state)
        can_stop=running and not self.runner.stop_event.is_set() or self.syncing and not self.catalog_stop.is_set()
        self.stop_button.configure(state='normal' if can_stop else 'disabled')
        if hasattr(self,'operations'): self.operations._buttons()

    def _test_telegram(self):
        if self.busy or self.runner.running: return
        notifier=TelegramNotifier(self.vars['token'].get(),self.vars['chat'].get())
        self._submit(lambda:notifier.send('🌳 hwadam_codex 알림 테스트\n성인·경로·어린이 중 선택 권종이 1장이라도 가능하면 알립니다.'),'tested')

    def _show_snapshot(self,event):
        snapshot=event['snapshot']; self.table.delete(*self.table.get_children())
        for row in (*snapshot.rounds,*snapshot.monorail):
            stock=row.stock_label+(f' · {row.entry_time} 입장 기준' if row.entry_time else '')
            self.table.insert('','end',values=('모노레일' if row.category=='monorail' else '입장권',row.time,stock,
                  ', '.join(AGE_NAMES[age] for age in row.eligible_ages) or '—'))
        self.status.set('감시 중 · 구매 가능 발견' if snapshot.hits else '감시 중 · 가용 회차 대기')
        self.session_status.set('NOL 연결 정상 · '+snapshot.checked_at)
        self.detail.set(date_label(snapshot.config.date)+' | '+count_label(snapshot.config.counts)+f"\n조회 {event['checks']}회 · 마지막 {snapshot.checked_at} · 목표 {', '.join(snapshot.config.time_specs)}")
        self.extra_label.set(snapshot.monorail_error or ('목표에 일치하는 회차가 없습니다. 날짜와 시각을 확인하세요.' if not snapshot.rounds else '공통 재고는 권종별 합계가 아닙니다. 구매 시 실시간 상태를 확인하세요.'))
        summary=' / '.join(row.time+' '+row.stock_label for row in snapshot.rounds)
        self._log('조회 완료 · '+(summary or '목표 회차 없음'))

    def _poll(self):
        if self.closed: return
        self.root.after_cancel(self.poll_id)
        while True:
            try: event=self.events.get_nowait()
            except queue.Empty: break
            self._event_persisted=event.get('_persisted',False)
            try: self._handle_event(event)
            finally: self._event_persisted=False
        self.poll_id=self.root.after(100,self._poll)

    def _handle_event(self,event):
        kind=event['kind']
        if kind=='session_maintenance':
            self._show_session_maintenance(event)
        elif kind in ('loaded','tested','login_opened','session_saved','catalog_done','job_error','test_pay_done'):
            self.busy=False
            booking=kind=='test_pay_done' or event.get('operation')=='test_pay_done'
            if booking:
                self._lock_conditions(False)
                message=('테스트 완료 · 최종 결제 미실행' if kind=='test_pay_done' else
                         '테스트 실패 · '+event['message'])
                self.operations.test_pay_status.set(message)
            if event.get('auth'): self.session_status.set('로그인 만료 · 전용 Edge에서 다시 로그인하세요')
            if kind in ('catalog_done','job_error'): self.syncing=False
            ticket=event.get('ticket')
            if ticket is not None and not self.guard.accepts(ticket):
                self._log('조건이 변경되어 이전 조회 결과를 제외했습니다. 다시 읽어주세요.',persist=True,event='STALE_RESULT')
                self.status.set('조건 변경 · 회차 새로고침 필요')
            elif kind=='loaded': self._show_loaded(event['value'])
            elif kind=='tested': self._log('텔레그램 테스트 알림 전송 완료')
            elif kind=='test_pay_done': self._log('브라우저 예약 테스트 완료 · 최종 결제 미실행')
            elif kind=='login_opened':
                if event['value']:
                    self.status.set('기존 로그인으로 연결 완료 · 목록 읽기 또는 감시 시작이 가능합니다')
                    self.session_status.set('로그인 유지 확인 · 세션 자동 저장 완료')
                    self._log('기존 로그인 재사용 완료 · 다시 로그인할 필요가 없습니다')
                else:
                    self.status.set('전용 Edge 로그인 후 로그인 세션 저장을 누르세요')
                    self.session_status.set('로그인 필요 · 전용 Edge에서 로그인 후 세션 저장하세요')
                    self._log('로그인 브라우저 준비 완료 · 로그인 후 세션 저장 필요')
            elif kind=='session_saved':
                self.status.set('로그인 세션 저장 완료 · 브라우저 연결·목록 읽기를 누르세요')
                self.session_status.set('세션 저장 완료 · '+dt.datetime.now().strftime('%H:%M:%S'))
                self._log(f"로그인 확인 · 세션 쿠키 {event['value']}개 저장 · session_cookies.json")
            elif kind=='catalog_done':
                result=event['value'];self._cached_view()
                message=f"목록 {'확보 중지' if result['cancelled'] else '확보 완료'} · {result['days']}일 저장"
                if result['errors']: message+=f" · 확인 필요 {len(result['errors'])}건"
                self.status.set(message);self._log(message)
                for error in result['errors'][:5]: self._log(error)
            else: self.status.set(event['message']); self._log(event['message'])
            self._buttons()
        elif kind=='catalog_progress':
            self._update_product_stats();self.status.set(event['message']);self._log(event['message'])
        elif kind=='snapshot': self._show_snapshot(event)
        elif kind=='started': self._log('감시 시작 · '+date_label(event['config'].date))
        elif kind=='finished':
            if not self.status.get().startswith('NOL') and '로그인' not in self.status.get(): self.status.set('감시 종료 · 조건을 변경할 수 있습니다')
            self._lock_conditions(False); self._buttons(); self._log(event['message'])
        elif kind in ('auth','error'):
            if kind=='auth': self.session_status.set('로그인 만료 · 전용 Edge에서 다시 로그인하세요')
            self.status.set(event['message']); self._log(event['message'])
        else: self._log(event.get('message',''))

    def _log(self,text,*,persist=None,level=logging.INFO,event='GUI_MESSAGE'):
        text=sanitize(text)
        if persist is None: persist=not self._event_persisted
        if persist: self.logger.log(level,text,extra={'event':event})
        stamp=dt.datetime.now().strftime('%H:%M:%S')
        for view in (self.home_log_text,self.log_text):
            at_bottom=not view.winfo_ismapped() or view.yview()[1]>=0.99
            view.configure(state='normal');view.insert('end',f'{stamp}  {text}\n')
            if int(view.index('end-1c').split('.')[0])>500: view.delete('1.0','100.0')
            if at_bottom: view.see('end')
            view.configure(state='disabled')

    def _problem(self,error):
        self._log(error,level=logging.ERROR,event='GUI_ERROR'); messagebox.showerror('화담숲 모니터',sanitize(error),parent=self.root)

    def _open_product(self):
        try: url=self._session_url()
        except ValueError as error: self._problem(error); return
        webbrowser.open(url)

    def close(self):
        if self.closed: return
        self.logger.info('프로그램 종료 요청 · 감시/목록 작업 중지',extra={'event':'APP_CLOSING'})
        self.closed=True; self.runner.stop();self.catalog_stop.set(); self.root.after_cancel(self.poll_id)
        self.root.after_cancel(self.keepalive_id)
        self._remember()
        try: atomic_write(self.choices_file,json.dumps(self.choices,ensure_ascii=False,indent=2))
        except OSError as error:
            self.logger.error('종료 시 선택 저장 실패 · '+sanitize(error),extra={'event':'SETTINGS_SAVE_FAILED'})
        self.executor.shutdown(wait=False,cancel_futures=True)
        self.root.destroy()
        with self.jobs_lock: pending=any(not future.done() for future in self.pending_jobs)
        if pending or self.runner.running:
            self.cleanup_thread=threading.Thread(target=self._finish_close,name='hwadam-cleanup',daemon=False)
            self.cleanup_thread.start()
        else: self._finish_close()

    def _finish_close(self):
        # 실행 중인 조회의 마지막 결과까지 보존하고, Tk 객체는 건드리지 않는다.
        self.executor.shutdown(wait=True,cancel_futures=True)
        if self.runner.thread: self.runner.thread.join(timeout=35)
        if self.runner.running:
            self.logger.warning('감시 종료 대기 35초 경과 · 마지막 조회가 끝날 때까지 기록 유지',extra={'event':'SHUTDOWN_WAITING'})
            self.runner.thread.join()
        try: self.browser.detach()
        except Exception as error:
            self.logger.warning('브라우저 분리 실패 · '+type(error).__name__,extra={'event':'BROWSER_DETACH_FAILED'})
        self.logger.info('프로그램 종료 · 작업 정리 완료',extra={'event':'APP_CLOSED'})
        self.log_handler.close()
