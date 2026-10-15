"""통합한 소스만 배포해도 다른 작업 폴더에서 정상 실행되는지 확인한다."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SOURCES = ('main.py','gui.py','browser.py','api.py','storage.py','branding.py','catalog.py',
           'eventlog.py','logfiles.py','telegram_bot.py','ui_layout.py','ui_widgets.py')


class DistributionTests(unittest.TestCase):
    def copy_sources(self, base):
        source = Path(__file__).resolve().parents[1]
        for name in SOURCES:
            shutil.copy2(source/name,base/name)
        for name in ('model.py','monitor.py'):
            self.assertFalse((base/name).exists(), f'{name} must not be required by the distribution')
        return source

    def test_consolidated_sources_import_in_any_entry_order_without_services(self):
        code = '''
import importlib
from pathlib import Path
import platform
import sys
import tkinter as tk

def blocked(*args, **kwargs):
    raise AssertionError('Import must not start a GUI, browser, or network connection')

def audit(event, args):
    if event in {'socket.connect', 'socket.bind', 'socket.getaddrinfo',
                 'socket.gethostbyname', 'socket.gethostbyaddr', 'subprocess.Popen',
                 'os.system', 'os.startfile', 'os.startfile/2'}:
        blocked()

tk.Tk = blocked
# Selenium reads this metadata; Windows may invoke `ver` on its first lookup.
platform.uname()
sys.addaudithook(audit)
base = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(base))
for name in (sys.argv[2], *sys.argv[3:]):
    module = importlib.import_module(name)
    assert Path(module.__file__).resolve().parent == base, name
gui = importlib.import_module('gui')
for name in ('Availability', 'Catalog', 'Snapshot', 'WatchConfig', 'AlertTracker', 'MonitorRunner'):
    assert getattr(gui, name).__module__ == 'gui', name
assert 'model' not in sys.modules and 'monitor' not in sys.modules
print('Consolidated imports OK', sys.argv[2])
'''
        names = tuple(Path(name).stem for name in SOURCES)
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)/'설치 폴더'; base.mkdir()
            external = Path(folder)/'외부 작업 폴더'; external.mkdir()
            self.copy_sources(base)
            for first in names:
                with self.subTest(first=first):
                    result = subprocess.run([sys.executable,'-I','-B','-X','utf8','-c',code,
                                             str(base),first,*names], cwd=external,
                                            capture_output=True,text=True,encoding='utf-8',timeout=30)
                    self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)
                    self.assertIn('Consolidated imports OK '+first,result.stdout)
            self.assertEqual(list(external.iterdir()),[])

    def test_gui_runs_from_another_directory_with_only_consolidated_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)/'설치 폴더'; base.mkdir()
            external = Path(folder)/'외부 작업 폴더'; external.mkdir()
            source = self.copy_sources(base)
            for name in ('assets','data'):
                shutil.copytree(source/name,base/name)
            result = subprocess.run([sys.executable,'-X','utf8',str(base/'main.py'),'--gui-smoke'],
                                    cwd=external,capture_output=True,text=True,encoding='utf-8',timeout=30)
            self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)
            self.assertIn('GUI smoke OK 1360 920',result.stdout)
            self.assertIn('GUI smoke OK 1160 740',result.stdout)
            logs = list((base/'logs').glob('hwadam-*.log'))
            self.assertTrue(logs)
            self.assertIn('event=APP_CLOSED','\n'.join(path.read_text(encoding='utf-8') for path in logs))
            self.assertTrue((base/'browse_choices.json').is_file())
            self.assertEqual(list(external.iterdir()),[])


if __name__ == '__main__': unittest.main()
