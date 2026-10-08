from pathlib import Path
import tempfile
import tkinter as tk
import unittest
import time
import json

from gui import App, QueryGuard, config_from_form
from model import DEFAULT_URL, SECTION_NAMES, parse_product
from test_api import FULL_PRODUCT
from api import AuthLost
from unittest.mock import Mock
import threading
from test_browser import CookieDriver
from catalog import Catalogue
from ui_widgets import DateCalendar


class FormTests(unittest.TestCase):
    def test_sunday_calendar_positions_select_the_correct_visit_date(self):
        root=tk.Tk();root.withdraw();picked=[]
        calendar=DateCalendar(root,picked.append)
        try:
            calendar.set_context({'start':'2026-10-01','end':'2026-10-31'},'2026-10-25')
            buttons={str(button.cget('text')).strip():button for button in calendar.grid_frame.winfo_children()
                     if isinstance(button,tk.Button)}
            self.assertEqual(int(buttons['25'].grid_info()['column']),0)
            self.assertEqual(int(buttons['31'].grid_info()['column']),6)
            buttons['25'].invoke()
            self.assertEqual(picked,['2026-10-25'])
        finally: root.destroy()

    def test_live_logs_arrive_on_home_and_expanded_view_without_switching_tabs(self):
        with tempfile.TemporaryDirectory() as folder:
            root=tk.Tk();root.withdraw();app=App(root,Path(folder));root.update()
            try:
                app._log('입장권 08:20 조회 완료')
                self.assertIn('입장권 08:20 조회 완료',app.home_log_text.get('1.0','end'))
                self.assertIn('입장권 08:20 조회 완료',app.log_text.get('1.0','end'))
                app._show_page('monitor')
                app._log('봇 123456:abcdefghijk 연결 오류')
                for view in (app.home_log_text,app.log_text):
                    self.assertIn('<봇 토큰 숨김>',view.get('1.0','end'))
                    self.assertNotIn('123456:abcdefghijk',view.get('1.0','end'))
                    self.assertEqual(str(view.cget('state')),'disabled')
            finally: app.close()

    def test_relative_monorail_window_follows_entry_checks_and_survives_save(self):
        with tempfile.TemporaryDirectory() as folder:
            cache=Catalogue(Path(folder))
            cache.put_rounds('10367229','2026-10-25',[{'time':'08:00'},{'time':'08:20'}])
            cache.put_rounds('10367229','2026-10-25',
                [{'time':value} for value in ('08:05','08:25','08:35','08:55','09:00')],
                section='MONORAIL_SEGMENT_2',parent_time='08:00')
            root=tk.Tk();root.withdraw();app=App(root,Path(folder));root.update()
            try:
                app.vars['mono_relative'].set(True)
                self.assertTrue(app.read_config().include_monorail)
                self.assertEqual(app.secondary_list.selected,{'08:05','08:25','08:35','08:55'})
                self.assertEqual(app.secondary_list.cget('state'),'disabled')
                app.primary_list.buttons['08:20'].invoke()
                self.assertEqual(app.secondary_list.selected,{'08:05','08:25','08:35'})
                self.assertEqual(app.read_config().monorail_specs_for('08:00'),('08:05~08:35',))
                app.store.save(app.read_config())
            finally: app.close()
            root=tk.Tk();root.withdraw();restored=App(root,Path(folder));root.update()
            try:
                self.assertTrue(restored.read_config().mono_relative)
                self.assertEqual(restored.secondary_list.selected,{'08:05','08:25','08:35'})
                restored.vars['mono_relative'].set(False)
                self.assertEqual(restored.secondary_list.cget('state'),'normal')
            finally: restored.close()

    def test_entry_and_monorail_checkboxes_are_visible_together_and_select_independently(self):
        with tempfile.TemporaryDirectory() as folder:
            cache=Catalogue(Path(folder))
            cache.put_rounds('10367229','2026-10-25',[{'time':'08:00'},{'time':'08:20'}])
            cache.put_rounds('10367229','2026-10-25',[{'time':'08:30'},{'time':'08:35'}],section='MONORAIL_SEGMENT_2',parent_time='08:00')
            cache.put_rounds('10367229','2026-10-26',[{'time':'09:30'}],section='MONORAIL_SEGMENT_2',parent_time='09:00')
            root=tk.Tk();root.withdraw();app=App(root,Path(folder));root.update()
            try:
                self.assertEqual(app.secondary_area.winfo_manager(),'pack','알림 포함 여부와 무관하게 모노레일 목록을 탐색할 수 있어야 한다')
                for grid in (app.primary_list,app.secondary_list):
                    self.assertTrue(all(isinstance(button,tk.Checkbutton) for button in grid.buttons.values()))
                app.secondary_list.buttons['08:30'].invoke()
                app.secondary_list.buttons['08:35'].invoke()
                self.assertEqual(app.read_config().mono_specs,('08:30','08:35'))
                self.assertTrue(app.read_config().include_monorail)
                self.assertEqual(app.read_config().time_specs,('08:00~08:20',))
                app.secondary_list.set_filter('오후');app.secondary_list.set_filter('전체')
                self.assertEqual(app.secondary_list.selected,{'08:30','08:35'})
                app.vars['date'].set('2026-10-26(월)')
                self.assertEqual(app.secondary_times,['09:30'])
                self.assertEqual(app.vars['mono_times'].get(),'')
                app.vars['date'].set('2026-10-25(일)')
                self.assertEqual(app.read_config().mono_specs,('08:30','08:35'))
                app.secondary_list.buttons['08:30'].invoke()
                self.assertEqual(app.read_config().mono_specs,('08:35',))
            finally: app.close()

    def test_weekday_display_and_senior_counts_become_valid_monitor_config(self):
        config=config_from_form({'mode':'entry','product_url':DEFAULT_URL,'date':'2026-10-25(일)',
          'times':'08:00, 08:20','counts':{'ADULT':'2','SENIOR':'2','TEEN':'0','CHILD':'2'},
          'interval':'10','repeat':'0'})
        self.assertEqual(config.date,'2026-10-25')
        self.assertEqual(config.time_specs,('08:00','08:20'))
        self.assertEqual(config.counts['SENIOR'],2)

    def test_old_async_response_is_rejected_after_date_or_product_changes(self):
        guard=QueryGuard()
        ticket=guard.ticket()
        self.assertTrue(guard.accepts(ticket))
        guard.changed()
        self.assertFalse(guard.accepts(ticket))

    def test_gui_build_does_not_start_monitor_and_preserves_default_target(self):
        with tempfile.TemporaryDirectory() as folder:
            root=tk.Tk()
            root.withdraw()
            app=App(root,Path(folder))
            try:
                root.update()
                self.assertEqual(app.read_config().counts,{'ADULT':2,'SENIOR':2,'TEEN':0,'CHILD':2})
                self.assertEqual(app.read_config().date,'2026-10-25')
                self.assertFalse(app.runner.running)
                self.assertIsNone(app.browser.driver)
                self.assertGreater(app.left_content.winfo_reqheight(),100)
                self.assertTrue(hasattr(app,'login_button'),'전용 로그인 브라우저를 열 수 있어야 한다')
                self.assertTrue(hasattr(app,'session_button'),'로그인 세션을 수동으로 저장할 수 있어야 한다')
            finally:
                app.close()

    def test_save_session_button_works_before_watch_conditions_are_valid(self):
        with tempfile.TemporaryDirectory() as folder:
            root=tk.Tk();root.withdraw();app=App(root,Path(folder))
            try:
                app.browser.driver=CookieDriver();app.browser.tab='own-tab'
                app.vars['date'].set('날짜 미선택')
                app.vars['times'].set('')
                app.session_button.invoke()
                deadline=time.monotonic()+2
                while app.busy and time.monotonic()<deadline:
                    root.update();time.sleep(.01)
                stored=json.loads((Path(folder)/'session_cookies.json').read_text(encoding='utf-8'))
                self.assertEqual([row['name'] for row in stored],['yanolja_sid'])
                self.assertIn('저장 완료',app.session_status.get())
                self.assertFalse(app.runner.running)
            finally: app.close()

    def test_checked_times_stay_with_their_product_when_browsing_between_products(self):
        with tempfile.TemporaryDirectory() as folder:
            cache=Catalogue(Path(folder))
            cache.put_rounds('10367229','2026-10-25',[{'time':'08:00','roundStatusCode':'SOLD_OUT'},
                                                    {'time':'08:20','roundStatusCode':'SOLD_OUT'}])
            cache.put_rounds('10323843','2026-10-03',[{'time':'09:00','roundStatusCode':'IN_SALE'}])
            root=tk.Tk();root.withdraw();app=App(root,Path(folder))
            try:
                root.update()
                app.primary_list.buttons['08:20'].invoke()
                self.assertEqual(app.read_config().time_specs,('08:00',))
                app._choose_product('10323843')
                root.update()
                self.assertIn('05.12-10.22',app.product_title.get())
                app.primary_list.buttons['09:00'].invoke()
                self.assertEqual(app.read_config().time_specs,('09:00',))
                app._choose_product('10367229')
                root.update()
                self.assertEqual(app.read_config().time_specs,('08:00',))
                self.assertFalse(app.runner.running)
                self.assertIsNone(app.browser.driver)
            finally: app.close()

    def test_browse_choices_survive_restart_and_monorail_section_change(self):
        with tempfile.TemporaryDirectory() as folder:
            root=tk.Tk();root.withdraw();app=App(root,Path(folder))
            try:
                root.update()
                app.vars['times'].set('08:20')
                app.vars['mono_times'].set('09:00')
                original=app.vars['section'].get()
                app.vars['section'].set(SECTION_NAMES['MONORAIL_SEGMENT_1'])
                self.assertEqual(app.vars['times'].get(),'08:20')
                self.assertEqual(app.vars['mono_times'].get(),'')
                app.vars['section'].set(original)
                self.assertEqual(app.vars['mono_times'].get(),'09:00')
            finally: app.close()
            root=tk.Tk();root.withdraw();restored=App(root,Path(folder));root.update()
            try: self.assertEqual(restored.vars['times'].get(),'08:20')
            finally: restored.close()

    def test_addon_browse_uses_only_requested_ages_and_isolates_orders(self):
        with tempfile.TemporaryDirectory() as folder:
            root=tk.Tk();root.withdraw();app=App(root,Path(folder));root.update()
            try:
                app.vars['mode'].set('addon')
                app.vars['addon_url'].set(DEFAULT_URL+'?orderGroupId=first')
                app.vars['times'].set('09:00')
                app.vars['interval'].set('')
                config=app._browse_config()
                self.assertEqual(config.counts['TEEN'],0)
                self.assertEqual(config.counts['SENIOR'],2)
                app.vars['addon_url'].set(DEFAULT_URL+'?orderGroupId=second')
                self.assertEqual(app.vars['times'].get(),'')
                app.vars['addon_url'].set(DEFAULT_URL+'?orderGroupId=first')
                self.assertEqual(app.vars['times'].get(),'09:00')
            finally: app.close()

    def test_browse_monorail_menu_uses_earliest_entry_even_when_target_is_later(self):
        with tempfile.TemporaryDirectory() as folder:
            root=tk.Tk();root.withdraw();app=App(root,Path(folder));root.update()
            try:
                app.vars['include_monorail'].set(False);app.vars['times'].set('14:00')
                client=Mock(lock=threading.RLock(),_order=None)
                client.prepare.return_value=parse_product(FULL_PRODUCT)
                client.schedules.return_value=[{'date':'2026-10-25'}]
                client.rounds.side_effect=[[{'time':'14:00'},{'time':'08:00'}],[{'time':'08:06'},{'time':'14:06'}]]
                app.client=client
                app._submit=lambda job,*args,**kwargs:job()
                app._load(False)
                self.assertEqual(client.rounds.call_args.kwargs['entry_time'],'08:00')
                self.assertEqual(len(app.catalogue.rounds('10367229','2026-10-25',section='MONORAIL_SEGMENT_2')['rows']),2)
            finally: app.close()

    def test_expired_auth_during_monorail_browse_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as folder:
            root=tk.Tk();root.withdraw();app=App(root,Path(folder))
            try:
                app.vars['include_monorail'].set(True)
                client=Mock(lock=threading.RLock(),_order=None)
                client.prepare.return_value=parse_product(FULL_PRODUCT)
                client.schedules.return_value=[{'date':'2026-10-25'}]
                client.rounds.side_effect=[[{'time':'08:00'}],AuthLost('NOL 로그인 만료')]
                app.client=client
                app._load(False)
                deadline=time.monotonic()+2
                while app.busy and time.monotonic()<deadline:
                    root.update();time.sleep(.01)
                self.assertIn('로그인',app.session_status.get())
                self.assertNotIn('완료',app.session_status.get())
                self.assertIn('만료',app.status.get())
            finally: app.close()


if __name__=='__main__': unittest.main()
