"""통합한 소스만 배포해도 다른 작업 폴더에서 정상 실행되는지 확인한다."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class DistributionTests(unittest.TestCase):
    def test_gui_runs_from_another_directory_with_only_consolidated_sources(self):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)/'설치 폴더'; base.mkdir()
            external = Path(folder)/'외부 작업 폴더'; external.mkdir()
            for name in ('main.py','gui.py','browser.py','api.py','model.py','monitor.py','storage.py',
                         'branding.py','catalog.py','eventlog.py','logfiles.py','telegram_bot.py','ui_layout.py','ui_widgets.py'):
                if (source/name).exists(): shutil.copy2(source/name,base/name)
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
