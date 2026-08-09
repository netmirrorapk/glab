"""Test launcher — wraps main.py and tees the in-app log to test_run.log.

Used for instrumented test runs so an external observer can read the
dispatcher log live from disk without the GUI open.
"""
import os
import sys
import time

# Resolve test_run.log next to this script
_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_run.log")

# Open tee log file
_tee = open(_LOG_PATH, "w", encoding="utf-8", buffering=1)  # line-buffered
_tee.write(f"[{time.strftime('%H:%M:%S')}] [TEST_LAUNCHER] Booting app with log tee active\n")
_tee.flush()

# Make sure sys.stdout/stderr are real (main.py redirects to devnull if None)
if sys.stdout is None:
    sys.stdout = _tee
if sys.stderr is None:
    sys.stderr = _tee

from src.core.runtime_stdio import ensure_std_streams
ensure_std_streams()

# Monkey-patch MainWindow.append_log BEFORE it's instantiated
from src.ui import main_window as _mw_mod
_orig_append_log = _mw_mod.MainWindow.append_log


def _patched_append_log(self, msg):
    try:
        text = str(msg or "")
        ts = time.strftime("%H:%M:%S")
        _tee.write(f"[{ts}] {text}\n")
        _tee.flush()
    except Exception:
        pass
    return _orig_append_log(self, msg)


_mw_mod.MainWindow.append_log = _patched_append_log

_tee.write(f"[{time.strftime('%H:%M:%S')}] [TEST_LAUNCHER] append_log patched — starting main()\n")
_tee.flush()

# Now hand off to the real main()
from main import main
main()
