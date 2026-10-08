"""Tkinter 설정 화면. 백그라운드 작업 결과는 큐로만 전달한다."""
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import logging
import json
import hashlib
from logging.handlers import RotatingFileHandler
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk
import webbrowser

from api import AuthLost, LeisureClient
from browser import BrowserSession
from catalog import Catalogue, PRODUCTS, product_id, sync_catalogues
from model import AGE_NAMES, DEFAULT_URL, REFERENCE_URL, SECTION_NAMES, WatchConfig, count_label, date_label, parse_time_specs, relative_monorail_specs, time_matches, validate_url
from monitor import MonitorRunner
from storage import SettingsStore, atomic_write, load_telegram_secrets, sanitize, save_telegram_secrets
from telegram_bot import TelegramNotifier
from ui_widgets import TimeGrid


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
        self.syncing=False
        self.catalog_stop=threading.Event()
        self.mutating=False
        self.events=queue.Queue()
        self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='hwadam-query')
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
        self.session_status=tk.StringVar(value='세션 저장 파일 있음 · 로그인 확인 필요' if (self.base/'session_cookies.json').is_file() else '로그인 세션 미저장 · 로그인 후 세션 저장하세요')
        self.detail=tk.StringVar(value='성인 2 · 경로 2 · 어린이 2 | 가용 1장부터 알림')
        self.primary_title=tk.StringVar(value='입장 회차 · 여러 개 선택 가능')
        self.product_title=tk.StringVar(value=(self.catalogue.product(product_id(config.product_url)) or {}).get('name','화담숲 입장권'))
        self.catalog_stamp=tk.StringVar(value='저장된 회차 목록을 불러오는 중')
        self.mono_stamp=tk.StringVar(value='선택 날짜의 모노레일 목록을 불러오는 중')
        self.date_summary=tk.StringVar(value=date_label(config.date))
        self.selection_count=tk.StringVar(value='선택한 회차')
        self.selection_summary=tk.StringVar(value='')
        self.order_label=tk.StringVar(value='추가구매는 입장권 주문에서 연 모노레일 주소를 붙여넣으세요.')
        self.logger=logging.getLogger(f'hwadam.{id(self)}')
        self.logger.setLevel(logging.INFO)
        self.logger.propagate=False
        (self.base/'logs').mkdir(parents=True,exist_ok=True)
        self.log_handler=RotatingFileHandler(self.base/'logs'/'hwadam.log',maxBytes=1024*1024,backupCount=3,encoding='utf-8')
        self.log_handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
        self.logger.addHandler(self.log_handler)
        self.browser=BrowserSession(self.base,lambda text:self.events.put({'kind':'log','message':text}))
        self.client=LeisureClient(self.browser)
        self.runner=MonitorRunner(self.client,self.events.put)
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
                return sync_catalogues(self.client,self.catalogue,self.catalog_stop,self.events.put,resume=True)
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
        build(self)

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

    def _submit(self,job,kind,*,ticket=None):
        self.busy=True; self._buttons()
        def work():
            try: event={'kind':kind,'value':job(),'ticket':ticket}
            except Exception as error: event={'kind':'job_error','message':sanitize(error),'ticket':ticket,'auth':isinstance(error,AuthLost)}
            self.events.put(event)
        self.executor.submit(work)

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
        while True:
            try: event=self.events.get_nowait()
            except queue.Empty: break
            kind=event['kind']
            if kind in ('loaded','tested','login_opened','session_saved','catalog_done','job_error'):
                self.busy=False
                if event.get('auth'): self.session_status.set('로그인 만료 · 전용 Edge에서 다시 로그인하세요')
                if kind in ('catalog_done','job_error'): self.syncing=False
                ticket=event.get('ticket')
                if ticket is not None and not self.guard.accepts(ticket):
                    self._log('조건이 변경되어 이전 조회 결과를 제외했습니다. 다시 읽어주세요.')
                    self.status.set('조건 변경 · 회차 새로고침 필요')
                elif kind=='loaded': self._show_loaded(event['value'])
                elif kind=='tested': self._log('텔레그램 테스트 알림 전송 완료')
                elif kind=='login_opened':
                    self.status.set('전용 Edge 로그인 후 로그인 세션 저장을 누르세요')
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
        self.poll_id=self.root.after(100,self._poll)

    def _log(self,text):
        text=sanitize(text); self.logger.info(text)
        stamp=dt.datetime.now().strftime('%H:%M:%S')
        for view in (self.home_log_text,self.log_text):
            at_bottom=not view.winfo_ismapped() or view.yview()[1]>=0.99
            view.configure(state='normal');view.insert('end',f'{stamp}  {text}\n')
            if int(view.index('end-1c').split('.')[0])>500: view.delete('1.0','100.0')
            if at_bottom: view.see('end')
            view.configure(state='disabled')

    def _problem(self,error):
        self._log(error); messagebox.showerror('화담숲 모니터',sanitize(error),parent=self.root)

    def _open_product(self):
        try: url=self._session_url()
        except ValueError as error: self._problem(error); return
        webbrowser.open(url)

    def close(self):
        if self.closed: return
        self.closed=True; self.runner.stop();self.catalog_stop.set(); self.root.after_cancel(self.poll_id)
        self._remember()
        try: atomic_write(self.choices_file,json.dumps(self.choices,ensure_ascii=False,indent=2))
        except OSError: pass
        self.executor.shutdown(wait=False,cancel_futures=True)
        threading.Thread(target=self.browser.detach,daemon=True).start()
        self.logger.removeHandler(self.log_handler); self.log_handler.close()
        self.root.destroy()
