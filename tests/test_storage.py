import json
import tempfile
import unittest
from pathlib import Path

from model import WatchConfig
from storage import SettingsStore, load_telegram_secrets, save_telegram_secrets


class StorageTests(unittest.TestCase):
    def test_round_trip_keeps_senior_goal_and_multiple_times(self):
        with tempfile.TemporaryDirectory() as temp:
            store=SettingsStore(Path(temp))
            config=WatchConfig(time_specs=("08:00","08:20"))
            store.save(config)
            loaded,warning=store.load()
            self.assertIsNone(warning)
            self.assertEqual(loaded.counts,{"ADULT":2,"SENIOR":2,"TEEN":0,"CHILD":2})
            self.assertEqual(loaded.time_specs,("08:00","08:20"))

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
