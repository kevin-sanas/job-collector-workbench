import os
import subprocess
import sys
import unittest

from app.config import ROOT


class LifecycleTests(unittest.TestCase):
    def test_check_mode_exits_without_native_crash(self):
        environment = os.environ.copy()
        environment['QT_QPA_PLATFORM'] = 'offscreen'
        # This only suppresses modal OS crash dialogs in each test child. The
        # return code and faulthandler output still expose a native crash.
        bootstrap = (
            "import sys; "
            "exec(\"if sys.platform == 'win32':\\n import ctypes; ctypes.windll.kernel32.SetErrorMode(2)\"); "
            "sys.argv=['main.py','--check']; import main; raise SystemExit(main.main())"
        )
        for attempt in range(5):
            with self.subTest(attempt=attempt):
                process = subprocess.run(
                    [sys.executable, '-X', 'utf8', '-X', 'faulthandler', '-c', bootstrap],
                    cwd=ROOT, env=environment, capture_output=True, timeout=20,
                )
                output = (process.stdout + process.stderr).decode('utf-8', errors='replace')
                self.assertEqual(process.returncode, 0, output)
                self.assertIn('UI_CHECK_OK', output)


if __name__ == '__main__':
    unittest.main()
