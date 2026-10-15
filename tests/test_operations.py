import json
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

from gui import App


class OperationsGuiTests(unittest.TestCase):
    def test_invalid_preferences_are_logged_without_values(self):
        with tempfile.TemporaryDirectory() as folder:
            root=tk.Tk();root.withdraw();app=App(root,Path(folder));root.update()
            try:
                panel=app.operations
                panel.vars['method'].set('PRIVATE_INVALID_VALUE')
                panel.save()
                text=app.log_text.get('1.0','end')
                self.assertIn('OPERATIONS_SETTINGS_INVALID',text)
                self.assertNotIn('PRIVATE_INVALID_VALUE',text)
            finally: app.close()

    def test_preference_write_failure_is_logged(self):
        with tempfile.TemporaryDirectory() as folder:
            root=tk.Tk();root.withdraw();app=App(root,Path(folder));root.update()
            try:
                with patch('ui_operations.atomic_write',side_effect=OSError('접근 거부')):
                    app.operations.save()
                text=app.log_text.get('1.0','end')
                self.assertIn('OPERATIONS_SETTINGS_SAVE_FAILED',text)
                self.assertIn('접근 거부',text)
            finally: app.close()

    def test_admin_mode_changes_are_logged(self):
        with tempfile.TemporaryDirectory() as folder:
            root=tk.Tk();root.withdraw();app=App(root,Path(folder));root.update()
            try:
                panel=app.operations
                panel.toggle_admin();panel.toggle_admin()
                text=app.log_text.get('1.0','end')
                self.assertIn('ADMIN_MODE_OPENED',text)
                self.assertIn('ADMIN_MODE_CLOSED',text)
            finally: app.close()

    def test_admin_rehearsal_has_no_browser_payment_or_message_side_effects(self):
        with tempfile.TemporaryDirectory() as folder:
            root=tk.Tk();root.withdraw();app=App(root,Path(folder));root.update()
            try:
                self.assertIn('payment',app.pages)
                self.assertIn('admin',app.pages)
                panel=app.operations
                self.assertEqual(str(panel.run_button.cget('state')),'disabled')
                with patch.object(app.browser,'connect') as connect, patch.object(app.browser,'request') as request, \
                     patch('webbrowser.open') as open_page, patch('telegram_bot.TelegramNotifier.send') as send:
                    panel.unlock_button.invoke()
                    panel.run_button.invoke()
                    self.assertTrue(panel.rehearsing)
                    for _ in range(8): panel.next_button.invoke()
                    self.assertFalse(panel.rehearsing)
                    self.assertIn('화면 점검 완료',panel.preview_status.get())
                    for boundary in (connect,request,open_page,send): boundary.assert_not_called()
                self.assertFalse(app.runner.running)
            finally: app.close()

    def test_payment_preferences_restore_without_copying_reference_pin_or_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            root=tk.Tk();root.withdraw();app=App(root,Path(folder));root.update()
            try:
                self.assertIn('payment',app.pages)
                panel=app.operations
                panel.vars['method'].set('Npay')
                panel.vars['stage'].set('전체 결제 (PIN까지)')
                panel.save_button.invoke()
                saved=json.loads((Path(folder)/'operation_settings.json').read_text(encoding='utf-8'))
                self.assertEqual(set(saved),{'method','kind','quantity','stage'})
            finally: app.close()
            root=tk.Tk();root.withdraw();app=App(root,Path(folder));root.update()
            try:
                self.assertEqual(app.operations.vars['method'].get(),'Npay')
                self.assertEqual(app.operations.vars['stage'].get(),'전체 결제 (PIN까지)')
                self.assertFalse(app.operations.admin_enabled)
                self.assertFalse(app.operations.rehearsing)
            finally: app.close()

    def test_admin_lock_stops_preview_and_keeps_watch_conditions_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            root=tk.Tk();root.withdraw();app=App(root,Path(folder));root.update()
            try:
                self.assertIn('admin',app.pages)
                before=app.read_config()
                panel=app.operations
                panel.unlock_button.invoke();panel.run_button.invoke()
                panel.unlock_button.invoke()
                self.assertFalse(panel.rehearsing)
                self.assertEqual(str(panel.run_button.cget('state')),'disabled')
                self.assertEqual(app.read_config(),before)
                panel.refresh_context()
                self.assertIn('성인 2',panel.target.get())
                self.assertIn('경로 2',panel.target.get())
            finally: app.close()
