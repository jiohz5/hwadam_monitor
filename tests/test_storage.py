from dataclasses import replace
import json
import tempfile
import unittest
from pathlib import Path

from gui import WatchConfig
from storage import SettingsStore, load_telegram_secrets, save_telegram_secrets, sanitize


class StorageTests(unittest.TestCase):
    def test_redaction_remains_readable_across_gui_and_file_logging(self):
        text='주문 orderGroupId=private-order&next=1 · bot=123456:abcdefghijk'
        clean=sanitize(text)
        self.assertNotIn('private-order',clean)
        self.assertEqual(sanitize(sanitize(clean)),clean)

    def test_round_trip_keeps_senior_goal_and_multiple_times(self):
        with tempfile.TemporaryDirectory() as temp:
            store=SettingsStore(Path(temp))
            config=WatchConfig(time_specs=("08:00","08:20"))
            store.save(config)
            loaded,warning=store.load()
            self.assertIsNone(warning)
            self.assertEqual(loaded.counts,{"ADULT":2,"SENIOR":2,"TEEN":0,"CHILD":2})
            self.assertEqual(loaded.time_specs,("08:00","08:20"))
            self.assertNotIn('NOL_MON_PIN',json.loads(store.path.read_text(encoding='utf-8')))

    def test_save_preserves_latest_pin_without_putting_it_in_watch_config(self):
        with tempfile.TemporaryDirectory() as temp:
            store=SettingsStore(Path(temp))
            store.path.write_text('{"NOL_MON_PIN":"000001"}',encoding='utf-8-sig')
            config,warning=store.load()
            self.assertIsNone(warning)
            self.assertFalse(hasattr(config,'NOL_MON_PIN'))
            # 실행 중 파일에서 직접 바꾼 PIN도 다음 설정 저장에서 보존한다.
            store.path.write_text('{"NOL_MON_PIN":"001234"}',encoding='utf-8-sig')
            config=replace(config,interval=25)
            store.save(config)
            saved=json.loads(store.path.read_text(encoding='utf-8'))
            self.assertEqual(saved['NOL_MON_PIN'],'001234')
            self.assertEqual(saved['interval'],25)
            store.save(config)
            self.assertEqual(json.loads(store.path.read_text(encoding='utf-8'))['NOL_MON_PIN'],'001234')

    def test_invalid_existing_settings_are_not_overwritten(self):
        for original in (b'{"NOL_MON_PIN":"001234",',b'[]',b'null',b'\xff'):
            with self.subTest(original=original), tempfile.TemporaryDirectory() as temp:
                store=SettingsStore(Path(temp))
                store.path.write_bytes(original)
                with self.assertRaises(ValueError):
                    store.save(WatchConfig())
                self.assertEqual(store.path.read_bytes(),original)

    def test_corrupted_settings_fall_back_with_actionable_warning(self):
        with tempfile.TemporaryDirectory() as temp:
            Path(temp,'settings.json').write_text('{bad',encoding='utf-8')
            config,warning=SettingsStore(Path(temp)).load()
            self.assertTrue(warning)
            self.assertEqual(config.date,'2026-10-25')

    def test_secrets_do_not_enter_general_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp)
            save_telegram_secrets(base,'123456:abcdefghijk','987654321')
            SettingsStore(base).save(WatchConfig())
            general=json.loads((base/'settings.json').read_text(encoding='utf-8'))
            self.assertNotIn('token',general)
            self.assertEqual(load_telegram_secrets(base, environ={}),('123456:abcdefghijk','987654321'))


if __name__ == '__main__':
    unittest.main()
