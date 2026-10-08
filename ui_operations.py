"""수동 결제 준비와 관리모드 화면. 결제·인증·주문 API를 호출하지 않는다."""
import json
import os
import tkinter as tk
from tkinter import ttk

from model import AGE_NAMES
from storage import atomic_write, sanitize
from ui_widgets import BG, WHITE, GREEN, MUTED, card


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
            self.payment_status.set('목록에서 결제 준비 옵션을 선택하세요.');return
        try: atomic_write(self.path,json.dumps(values,ensure_ascii=False,indent=2))
        except OSError as error:
            self.payment_status.set('저장 실패 · '+sanitize(error));return
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
        self._buttons()

    def _buttons(self):
        for widget in self.admin_widgets:
            enabled=self.admin_enabled and not self.rehearsing
            widget.configure(state=('readonly' if isinstance(widget,ttk.Combobox) else 'normal') if enabled else 'disabled')
        self.run_button.configure(state='normal' if self.admin_enabled and not self.rehearsing else 'disabled')
        for widget in (self.next_button,self.stop_button): widget.configure(state='normal' if self.rehearsing else 'disabled')

    def start_preview(self):
        if not self.admin_enabled or self.rehearsing: return
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
