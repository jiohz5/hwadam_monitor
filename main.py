"""GUI 실행 및 알림을 보내지 않는 읽기 검증 도구."""
import argparse
from dataclasses import asdict
import json
import logging
from pathlib import Path
import sys
import threading
import traceback

from model import DEFAULT_URL, SECTION_NAMES, WatchConfig, date_label, parse_time_specs
from eventlog import persist_event
from logfiles import create_app_logger
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


def cli_output(logger, message, *, event='CLI_PROGRESS', level=logging.INFO):
    message=sanitize(message)
    if logger is not None: logger.log(level,message,extra={'event':event})
    print(message,flush=True)


def print_json(value, *, indent=None):
    def clean(item):
        if isinstance(item,str): return sanitize(item)
        if isinstance(item,dict): return {key:clean(value) for key,value in item.items()}
        if isinstance(item,(list,tuple)): return [clean(value) for value in item]
        return item
    print(json.dumps(clean(value),ensure_ascii=False,indent=indent),flush=True)


def probe(args, logger=None):
    from api import LeisureClient
    from browser import BrowserSession
    config=WatchConfig(mode='addon' if args.addon_url else 'entry',product_url=args.product_url,
       addon_url=args.addon_url,date=args.date,time_specs=parse_time_specs(args.times),
       counts={age:getattr(args,age.lower()) for age in ('ADULT','SENIOR','TEEN','CHILD')},
       include_monorail=args.monorail,mono_specs=parse_time_specs(args.mono_times),section=args.section).validate()
    browser=BrowserSession(BASE,lambda text:cli_output(logger,text,event='BROWSER'))
    try:
        client=LeisureClient(browser)
        catalog=client.prepare(config)
        cli_output(logger,f'상품 {catalog.id} {catalog.name}',event='PROBE_PRODUCT')
        dates=client.schedules(config)
        mono=config.mode=='addon'
        rounds=client.rounds(config,monorail=mono,entry_time=client._order.entry_time if mono else None)
        cli_output(logger,f'날짜 {len(dates)} 회차 {len(rounds)}',event='PROBE_CATALOG')
        snapshot=client.check(config,threading.Event())
        if logger is not None:
            logger.info('조회 결과 date=%s rounds=%s hits=%s monorail=%s monorail_error=%s',
                        config.date,len(snapshot.rounds),len(snapshot.hits),len(snapshot.monorail),
                        int(bool(snapshot.monorail_error)),extra={'event':'PROBE_RESULT'})
            persist_event(logger,{'kind':'snapshot','snapshot':snapshot,'checks':1})
        print_json({'date':date_label(config.date),'rounds':[asdict(row) for row in snapshot.rounds],
                          'monorail':[asdict(row) for row in snapshot.monorail],
                          'monorail_error':snapshot.monorail_error},indent=2)
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


def sync_catalog(args, logger=None):
    from api import LeisureClient
    from browser import BrowserSession
    from catalog import Catalogue, sync_catalogues
    browser=BrowserSession(BASE,lambda text:cli_output(logger,text,event='BROWSER'))
    def progress(event):
        failed=event.get('kind')=='catalog_error'
        cli_output(logger,event['message'],event='CATALOG_ERROR' if failed else 'CATALOG_PROGRESS',
                   level=logging.WARNING if failed else logging.INFO)
    try:
        result=sync_catalogues(LeisureClient(browser),Catalogue(BASE),threading.Event(),
                              progress,resume=args.resume)
        if logger is not None:
            logger.info('목록 확보 결과 days=%s errors=%s cancelled=%s',result['days'],
                        len(result['errors']),int(result['cancelled']),extra={'event':'CATALOG_RESULT'})
        print_json(result)
    finally: browser.detach()


def main():
    args=arguments()
    mode='probe' if args.probe else 'sync-catalog' if args.sync_catalog else 'gui-smoke' if args.gui_smoke else 'gui'
    logger=handler=None
    try:
        if args.probe or args.sync_catalog:
            logger,handler=create_app_logger(BASE)
            logger.info('CLI 실행 시작 mode=%s',mode,extra={'event':'CLI_START'})
        if args.probe: probe(args,logger)
        elif args.sync_catalog: sync_catalog(args,logger)
        else: gui(args.gui_smoke)
        if logger is not None: logger.info('CLI 실행 완료 mode=%s',mode,extra={'event':'CLI_COMPLETE'})
        return 0
    except Exception as error:
        if logger is None: logger,handler=create_app_logger(BASE)
        locations=';'.join(f'{Path(frame.f_code.co_filename).name}:{line}:{frame.f_code.co_name}'
                           for frame,line in traceback.walk_tb(error.__traceback__))
        logger.error('실행 오류 mode=%s error_type=%s location=%s',mode,type(error).__name__,locations,
                     extra={'event':'APP_ERROR'})
        if sys.stderr is not None: print(sanitize(error),file=sys.stderr)
        if not args.probe and not args.gui_smoke and not args.sync_catalog:
            try:
                from tkinter import messagebox
                messagebox.showerror('화담숲 모니터 실행 오류',sanitize(error)+'\nlogs 폴더의 오늘 날짜 hwadam 로그를 확인하세요.')
            except Exception: pass
        return 2
    finally:
        if handler is not None:
            logger.removeHandler(handler)
            handler.close()


if __name__=='__main__': sys.exit(main())
