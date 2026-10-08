"""GUI 실행 및 알림을 보내지 않는 읽기 검증 도구."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
import threading
import traceback

from model import DEFAULT_URL, SECTION_NAMES, WatchConfig, date_label, parse_time_specs
from storage import sanitize


BASE=Path(__file__).resolve().parent


def arguments():
    parser=argparse.ArgumentParser(description='화담숲 GUI 감시 / 읽기 검증')
    modes=parser.add_mutually_exclusive_group()
    modes.add_argument('--gui-smoke',action='store_true',help='브라우저·알림 없이 GUI 초기화 검증')
    modes.add_argument('--probe',action='store_true',help='실제 조회 1회, 알림 전송 없음')
    modes.add_argument('--sync-catalog',action='store_true',help='두 상품의 실제 날짜·회차 목록 저장, 알림 전송 없음')
    parser.add_argument('--resume',action='store_true',help='목록 확보 시 이미 저장된 날짜를 건너뜀')
    parser.add_argument('--product-url',default=DEFAULT_URL)
    parser.add_argument('--addon-url',default='')
    parser.add_argument('--date',default='2026-10-25')
    parser.add_argument('--times',default='08:00~08:20')
    parser.add_argument('--monorail',action='store_true')
    parser.add_argument('--mono-times',default='')
    parser.add_argument('--section',choices=SECTION_NAMES,default='MONORAIL_SEGMENT_2')
    for age,default in [('adult',2),('senior',2),('teen',0),('child',2)]:
        parser.add_argument('--'+age,type=int,default=default)
    return parser.parse_args()


def probe(args):
    from api import LeisureClient
    from browser import BrowserSession
    config=WatchConfig(mode='addon' if args.addon_url else 'entry',product_url=args.product_url,
       addon_url=args.addon_url,date=args.date,time_specs=parse_time_specs(args.times),
       counts={age:getattr(args,age.lower()) for age in ('ADULT','SENIOR','TEEN','CHILD')},
       include_monorail=args.monorail,mono_specs=parse_time_specs(args.mono_times),section=args.section).validate()
    browser=BrowserSession(BASE,lambda text:print(sanitize(text),flush=True))
    try:
        client=LeisureClient(browser)
        catalog=client.prepare(config)
        print('상품',catalog.id,catalog.name,flush=True)
        dates=client.schedules(config)
        mono=config.mode=='addon'
        rounds=client.rounds(config,monorail=mono,entry_time=client._order.entry_time if mono else None)
        print('날짜',len(dates),'회차',len(rounds),flush=True)
        snapshot=client.check(config,threading.Event())
        print(json.dumps({'date':date_label(config.date),'rounds':[asdict(row) for row in snapshot.rounds],
                          'monorail':[asdict(row) for row in snapshot.monorail],
                          'monorail_error':sanitize(snapshot.monorail_error)},ensure_ascii=False,indent=2),flush=True)
    finally:
        browser.detach()


def gui(smoke=False):
    import tkinter as tk
    from gui import App
    root=tk.Tk()
    if smoke: root.withdraw()
    app=App(root,BASE)
    if smoke:
        try:
            root.deiconify(); root.update()
            config=app.read_config()
            assert config.counts['SENIOR']>=0 and not app.runner.running and app.browser.driver is None
            for size in ('1360x920','1160x740'):
                root.geometry(size);root.update()
                assert app.canvas.winfo_width()>300
                assert app.start_button.winfo_rooty()+app.start_button.winfo_height()<root.winfo_rooty()+root.winfo_height()
                assert app.save_button.winfo_rooty()+app.save_button.winfo_height()<root.winfo_rooty()+root.winfo_height()
                assert app.home_log_text.winfo_height()>=100
                for widget in (app.home_log_text,app.table):
                    assert widget.winfo_ismapped()
                    assert widget.winfo_rooty()+widget.winfo_height()<root.winfo_rooty()+root.winfo_height()
                print('GUI smoke OK',root.winfo_width(),root.winfo_height(),date_label(config.date),config.counts)
        finally: app.close()
    else: root.mainloop()


def sync_catalog(args):
    from api import LeisureClient
    from browser import BrowserSession
    from catalog import Catalogue, sync_catalogues
    browser=BrowserSession(BASE,lambda text:print(sanitize(text),flush=True))
    try:
        result=sync_catalogues(LeisureClient(browser),Catalogue(BASE),threading.Event(),
                              lambda event:print(sanitize(event['message']),flush=True),resume=args.resume)
        print(json.dumps(result,ensure_ascii=False),flush=True)
    finally: browser.detach()


def main():
    args=arguments()
    try:
        if args.probe: probe(args)
        elif args.sync_catalog: sync_catalog(args)
        else: gui(args.gui_smoke)
        return 0
    except Exception as error:
        message=sanitize(traceback.format_exc())
        (BASE/'logs').mkdir(parents=True,exist_ok=True)
        (BASE/'logs'/'startup_error.log').write_text(message,encoding='utf-8')
        if sys.stderr is not None: print(sanitize(error),file=sys.stderr)
        if not args.probe and not args.gui_smoke and not args.sync_catalog:
            try:
                from tkinter import messagebox
                messagebox.showerror('화담숲 모니터 실행 오류',sanitize(error)+'\nlogs/startup_error.log를 확인하세요.')
            except Exception: pass
        return 2


if __name__=='__main__': sys.exit(main())
