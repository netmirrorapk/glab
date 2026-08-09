import sys
import os
import asyncio
import subprocess
import shutil
import uuid
import time
import re
import json
from pathlib import Path
from urllib.parse import urlparse
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout,
    QTextEdit, QPlainTextEdit, QPushButton, QComboBox, QLabel,
    QTableWidget, QTableWidgetItem, QTableView, QHeaderView, QAbstractItemView,
    QSplitter, QGroupBox, QLineEdit, QTabWidget, QScrollArea, QAbstractScrollArea,
    QMessageBox, QFileDialog, QSpinBox, QCheckBox, QFrame, QSizePolicy, QProgressBar,
    QProgressDialog, QDialog, QAbstractSpinBox,
    QGraphicsDropShadowEffect, QDoubleSpinBox, QInputDialog
)
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QSize, QObject, QRunnable, QThreadPool, QRectF, QEvent


# ══════════════════════════════════════════════════════════════════════════
# QFluentWidgets (Phase 2+ migration). Graceful fallback if unavailable —
# allows main_window to keep importing even when library is missing.
# ══════════════════════════════════════════════════════════════════════════
try:
    from qfluentwidgets import (
        NavigationInterface,
        NavigationItemPosition,
        FluentIcon,
        PrimaryPushButton,
        PushButton,
        TransparentPushButton,
        MessageBox as FluentMessageBox,
        InfoBar,
        InfoBarPosition,
        SpinBox as _FluentSpinBox,
        DoubleSpinBox as _FluentDoubleSpinBox,
        ComboBox as _FluentComboBox,
    )
    _FLUENT_UI_AVAILABLE = True
except Exception:
    NavigationInterface = None
    NavigationItemPosition = None
    FluentIcon = None
    PrimaryPushButton = None
    PushButton = None
    TransparentPushButton = None
    FluentMessageBox = None
    InfoBar = None
    InfoBarPosition = None
    _FluentSpinBox = None
    _FluentDoubleSpinBox = None
    _FluentComboBox = None
    _FLUENT_UI_AVAILABLE = False


# ══════════════════════════════════════════════════════════════════════════
# Module-level class rebinding: replace QSpinBox / QDoubleSpinBox with
# their Fluent variants so every `QSpinBox()` call in this file
# automatically uses the Fluent-styled widget. Fluent SpinBox and
# DoubleSpinBox ARE proper QSpinBox subclasses, so all existing code
# (isinstance checks, API calls, signal connections, wheel monkey-patch via
# inheritance) continues to work unchanged.
#
# NOTE: We do NOT rebind QComboBox. Fluent ComboBox is not a QComboBox
# subclass (it extends QPushButton) — while duck-type compatible at the
# method level, Qt's internal popup and isinstance-based logic breaks
# when we substitute it. Stock QComboBox is kept with a chevron QSS.
# ══════════════════════════════════════════════════════════════════════════
if _FLUENT_UI_AVAILABLE:
    if _FluentSpinBox is not None:
        QSpinBox = _FluentSpinBox
    if _FluentDoubleSpinBox is not None:
        QDoubleSpinBox = _FluentDoubleSpinBox

    # Re-apply wheel-block on Fluent variants. The earlier
    # _apply_wheel_block() call patched the stock QSpinBox class, but
    # Fluent SpinBox/DoubleSpinBox define their OWN wheelEvent which
    # shadows the inherited patched method. We need to patch Fluent
    # classes directly so page scrolling doesn't change values.
    for _cls in (_FluentSpinBox, _FluentDoubleSpinBox, _FluentComboBox):
        if _cls is not None:
            try:
                _cls.wheelEvent = _no_wheel_change
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════
# GLOBAL wheel-event block for dropdowns / spin boxes.
# Prevents accidental value changes when scrolling the page with cursor
# over a QComboBox/QSpinBox. Widget must be explicitly focused (clicked)
# before wheel scroll can change its value — same as modern web UIs.
# ══════════════════════════════════════════════════════════════════════════
def _no_wheel_change(self, event):
    """Legacy monkey-patch (still used as fallback). Ignore wheel events
    unless widget has focus."""
    if self.hasFocus():
        return type(self).__mro__[1].wheelEvent(self, event)
    event.ignore()


def _apply_wheel_block():
    """Monkey-patch wheelEvent on QComboBox / QSpinBox / QDoubleSpinBox.
    This is the first line of defense but PySide6's C++ event dispatch
    sometimes bypasses post-hoc Python overrides, so we ALSO install a
    global application-level event filter (_WheelEventFilter) as the
    authoritative solution."""
    for cls in (QComboBox, QSpinBox, QDoubleSpinBox):
        cls.wheelEvent = _no_wheel_change


_apply_wheel_block()


class _WheelEventFilter(QObject):
    """Per-widget event filter: block wheel events on any
    QAbstractSpinBox / QComboBox and manually forward them to the
    nearest QAbstractScrollArea ancestor so the page still scrolls.

    Why forwarding: simply returning True from the filter consumes
    the event entirely — the parent scroll area never sees it.
    We have to manually sendEvent() to the scroll area's viewport
    after consuming the original.
    """

    def eventFilter(self, obj, event):
        try:
            if event.type() == QEvent.Wheel:
                # Walk up the parent chain to find a scroll area.
                ancestor = obj.parent() if hasattr(obj, "parent") else None
                while ancestor is not None:
                    if isinstance(ancestor, QAbstractScrollArea):
                        # Forward the wheel event to the scroll area
                        # viewport so the page scrolls normally.
                        try:
                            QApplication.sendEvent(ancestor.viewport(), event)
                        except Exception:
                            pass
                        return True  # consume on the widget
                    parent_fn = getattr(ancestor, "parent", None)
                    ancestor = parent_fn() if callable(parent_fn) else None
                # No scroll area found — still consume so the widget
                # doesn't change its value.
                return True
        except Exception:
            pass
        return False


# ══════════════════════════════════════════════════════════════════════════
# Runtime-generated chevron SVG for QComboBox drop-down arrow. QSS can
# only reference arrows via `image: url(...)` with a real file path, and
# CSS border-triangle tricks don't render reliably in PySide6 subcontrols.
# This writes a tiny SVG to a known cache dir on first import so the QSS
# can point at it. The file is idempotent — written once per run.
# ══════════════════════════════════════════════════════════════════════════
_CHEVRON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 10">'
    '<path d="M2 2 L8 8 L14 2" stroke="#CBD5E1" stroke-width="2.2" '
    'fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
    '</svg>'
)


def _ensure_chevron_asset():
    """Write the chevron SVG to disk so QSS can reference it."""
    try:
        import tempfile
        asset_dir = Path(tempfile.gettempdir()) / "glabs_assets"
        asset_dir.mkdir(parents=True, exist_ok=True)
        path = asset_dir / "chevron_down.svg"
        if not path.exists():
            path.write_text(_CHEVRON_SVG, encoding="utf-8")
        # Qt QSS wants forward slashes even on Windows
        return str(path).replace("\\", "/")
    except Exception:
        return ""


_CHEVRON_PATH = _ensure_chevron_asset()


def _force_plusminus_symbols(root_widget):
    """Walk a widget tree and set PlusMinus button symbols on every
    QSpinBox / QDoubleSpinBox. QSS-styled arrows are unreliable in
    PySide6 (CSS triangle subcontrols don't render cleanly). Plus/minus
    glyphs are text, so they are always visible against any theme and
    any background color."""
    try:
        for sb in root_widget.findChildren(QAbstractSpinBox):
            try:
                sb.setButtonSymbols(QAbstractSpinBox.PlusMinus)
            except Exception:
                pass
    except Exception:
        pass
from PySide6.QtGui import QColor, QIcon, QPixmap, QFont, QPainter, QPen, QTextCursor

from src.db.db_manager import (
    get_accounts,
    add_account,
    remove_account,
    remove_account_by_id,
    update_account_name_by_id,
    update_account_proxy_by_id,
    update_account_session_by_id,
    add_jobs_bulk,
    get_all_jobs,
    get_failed_jobs,
    get_output_directory,
    get_float_setting,
    get_int_setting,
    get_bool_setting,
    get_setting,
    set_setting,
    set_account_flag,
    clear_account_flags,
    clear_failed_jobs,
    clear_completed_jobs,
    update_job_status,
    update_job_prompt,
    update_pending_jobs_generation_settings,
    retry_failed_jobs_to_top,
)
from src.core.account_manager import AccountManager
from src.core.app_paths import get_app_data_dir, get_outputs_dir, get_project_cache_path, get_session_clones_dir, get_sessions_dir
from src.core.bot_engine import GoogleLabsBot
from src.core.process_tracker import process_tracker
from src.core.queue_manager import AsyncQueueManager
from src.ui.queue_model import QueueTableModel

class LoginWorker(QThread):
    log_msg = Signal(str)
    download_progress = Signal(int, str)
    download_complete = Signal(bool, str)
    session_saved = Signal(str, str, str)
    warmup_progress = Signal(str, int, str)
    warmup_complete = Signal(str, bool, str)
    finished_login = Signal(str, str, str) # name, session_path, detected_email
    
    def __init__(self, account_name, proxy="", login_target="flow"):
        super().__init__()
        self.account_name = account_name
        self.proxy = str(proxy or "").strip()
        self.login_target = str(login_target or "flow").strip().lower()

    def stop(self):
        self.requestInterruption()

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            name, session_path, detected_email = loop.run_until_complete(
                AccountManager.login_and_save_session(
                    self.account_name,
                    lambda m: self.log_msg.emit(m),
                    login_target=self.login_target,
                    download_progress_callback=lambda percent, status: self.download_progress.emit(int(percent), str(status)),
                    download_complete_callback=lambda success, message: self.download_complete.emit(bool(success), str(message)),
                    session_saved_callback=lambda name, session_path, detected_email: self.session_saved.emit(
                        str(name),
                        str(session_path),
                        str(detected_email),
                    ),
                    warmup_progress_callback=lambda name, percent, status: self.warmup_progress.emit(
                        str(name),
                        int(percent),
                        str(status),
                    ),
                    warmup_complete_callback=lambda name, success, message: self.warmup_complete.emit(
                        str(name),
                        bool(success),
                        str(message),
                    ),
                    should_stop=lambda: self.isInterruptionRequested(),
                    proxy=self.proxy,
                )
            )
            if not self.isInterruptionRequested():
                self.finished_login.emit(name, session_path, detected_email)
        except Exception as e:
            if not self.isInterruptionRequested():
                label = self.account_name if str(self.account_name or "").strip() else "AUTO-GMAIL"
                self.log_msg.emit(f"[{label}] Error during login: {str(e)}")
        finally:
            loop.close()


class LoginCheckWorker(QThread):
    log_msg = Signal(str)
    single_result = Signal(int, dict)
    result_ready = Signal(dict)

    def __init__(self, accounts):
        super().__init__()
        self.accounts = list(accounts or [])

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        results = {}
        try:
            for account in self.accounts:
                account_id = int(account.get("id") or 0)
                account_name = str(account.get("name") or f"Account {account_id}")
                session_path = str(account.get("session_path") or "")
                proxy = str(account.get("proxy") or "")
                try:
                    result = loop.run_until_complete(
                        AccountManager.check_account_login_status(session_path, proxy=proxy)
                    )
                except Exception as e:
                    result = {
                        "logged_in": False,
                        "email": "",
                        "expires": "",
                        "error": str(e),
                    }

                results[account_id] = result
                self.single_result.emit(account_id, result)
                state_text = "logged in" if result.get("logged_in") else "logged out"
                self.log_msg.emit(f"[ACCOUNTS] {account_name}: {state_text}")
        finally:
            loop.close()

        self.result_ready.emit(results)


class CleanupThread(QThread):
    """Kept for backward compat but no longer used by closeEvent."""
    def __init__(self, queue_manager, parent=None):
        super().__init__(parent)
        self.queue_manager = queue_manager

    def run(self):
        try:
            if self.queue_manager and self.queue_manager.isRunning():
                try:
                    self.queue_manager.stop()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            process_tracker.kill_all()
        except Exception:
            pass


class _CloakDownloadLogHandler:
    """Captures cloakbrowser.download log records and emits parsed
    progress to a Qt signal. CloakBrowser logs progress internally via
    Python's logging module (lines like "Download progress: 30% (60/200
    MB)") but doesn't expose a callback API — this handler bridges
    that gap.
    """
    _DOWNLOAD_PCT_RE = re.compile(
        r"Download progress:\s*(\d+)%\s*\((\d+)/(\d+)\s*MB\)",
        re.IGNORECASE,
    )
    _DOWNLOAD_DONE_RE = re.compile(
        r"Download complete:\s*(\d+)\s*MB",
        re.IGNORECASE,
    )

    def __init__(self, progress_signal):
        self._progress_signal = progress_signal
        self.level = 0  # accept all levels

    def handle(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return
        m = self._DOWNLOAD_PCT_RE.search(msg)
        if m:
            try:
                pct = int(m.group(1))
                done = int(m.group(2))
                total = int(m.group(3))
                self._progress_signal.emit(pct, done, total)
            except Exception:
                pass
            return
        done = self._DOWNLOAD_DONE_RE.search(msg)
        if done:
            try:
                size = int(done.group(1))
                self._progress_signal.emit(100, size, size)
            except Exception:
                pass


class DolaDeleteWorker(QThread):
    """Runs the online dola.com account deletion for a single account off the UI thread."""
    log_msg = Signal(str)
    finished_delete = Signal(int, bool, str)  # account_id, ok, detail

    def __init__(self, account_id, session_path, proxy=""):
        super().__init__()
        self.account_id = int(account_id or 0)
        self.session_path = str(session_path or "")
        self.proxy = str(proxy or "").strip()

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                AccountManager.delete_dola_account(
                    self.session_path,
                    proxy=self.proxy,
                    update_log_callback=lambda m: self.log_msg.emit(m),
                )
            )
            self.finished_delete.emit(
                self.account_id,
                bool(result.get("ok")),
                str(result.get("detail") or ""),
            )
        except Exception as e:
            self.finished_delete.emit(self.account_id, False, str(e))
        finally:
            loop.close()


class CloakUpdateWorker(QThread):
    status_changed = Signal(str, str)
    # percent (0-100), downloaded MB, total MB
    progress_changed = Signal(int, int, int)
    finished = Signal(bool, str)

    def __init__(self, install_mode=False, parent=None):
        super().__init__(parent)
        self.install_mode = bool(install_mode)

    @staticmethod
    def _configure_cloak_env():
        if getattr(sys, "frozen", False):
            cache_dir = get_app_data_dir() / "cloakbrowser_cache"
        else:
            cache_dir = Path.home() / ".cloakbrowser"
        cache_dir.mkdir(parents=True, exist_ok=True)
        os.environ["CLOAKBROWSER_CACHE_DIR"] = str(cache_dir.resolve())

    def _install_download_log_handler(self):
        """Hook into cloakbrowser.download logger to capture progress."""
        import logging
        try:
            dl_logger = logging.getLogger("cloakbrowser.download")
            dl_logger.setLevel(logging.INFO)
            handler = _CloakDownloadLogHandler(self.progress_changed)
            # logging wants a real Handler subclass — wrap ours
            class _BridgeHandler(logging.Handler):
                def __init__(self, bridge):
                    super().__init__(level=logging.INFO)
                    self._bridge = bridge

                def emit(self, record):
                    self._bridge.handle(record)

            bridge = _BridgeHandler(handler)
            dl_logger.addHandler(bridge)
            return bridge
        except Exception:
            return None

    def _remove_download_log_handler(self, handler):
        import logging
        if handler is None:
            return
        try:
            logging.getLogger("cloakbrowser.download").removeHandler(handler)
        except Exception:
            pass

    def _get_pip_python(self):
        """Get the right Python for pip. In frozen builds, find system Python."""
        if not getattr(sys, "frozen", False) and not getattr(sys, "_MEIPASS", None):
            return sys.executable
        for cmd in ["python3", "python"]:
            try:
                r = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=5)
                if r.returncode == 0:
                    return cmd
            except Exception:
                continue
        return sys.executable

    def _pip_show_version(self):
        """Get real installed cloakbrowser version via pip show (no cache issues)."""
        python_exe = self._get_pip_python()
        try:
            _no_win = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)} if sys.platform.startswith("win") else {}
            result = subprocess.run(
                [python_exe, "-m", "pip", "show", "cloakbrowser"],
                capture_output=True, text=True, timeout=15, **_no_win,
            )
            if result.returncode == 0 and result.stdout:
                for line in result.stdout.splitlines():
                    if line.startswith("Version:"):
                        return line.split(":", 1)[1].strip()
        except Exception:
            pass
        return "unknown"

    def run(self):
        import importlib

        self._configure_cloak_env()

        try:
            is_frozen = getattr(sys, "frozen", False)

            # ── Get version BEFORE upgrade (via pip show — no cache) ──
            pkg_version_before = self._pip_show_version()

            self.status_changed.emit("⏳ Updating CloakBrowser package...", "#60A5FA")
            python_exe = self._get_pip_python()
            _no_win = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)} if sys.platform.startswith("win") else {}
            pip_success = False
            last_pip_error = ""

            # Try multiple pip strategies for cross-platform compatibility
            pip_strategies = [
                [python_exe, "-m", "pip", "install", "cloakbrowser", "--upgrade"],
                [python_exe, "-m", "pip", "install", "cloakbrowser", "--upgrade", "--break-system-packages"],
                [python_exe, "-m", "pip", "install", "cloakbrowser", "--upgrade", "--user"],
            ]
            for i, pip_cmd in enumerate(pip_strategies):
                try:
                    self.status_changed.emit(f"⏳ Trying pip strategy {i+1}/3...", "#60A5FA")
                    result = subprocess.run(
                        pip_cmd,
                        capture_output=True,
                        text=True,
                        timeout=120,
                        **_no_win,
                    )
                    if result.returncode == 0:
                        pip_success = True
                        # Show what pip actually did
                        pip_out = (result.stdout or "").strip()
                        if "already satisfied" in pip_out.lower():
                            self.status_changed.emit("✓ Already on latest pip version.", "#22C55E")
                        elif "Successfully installed" in pip_out:
                            self.status_changed.emit("✓ Package upgraded via pip!", "#22C55E")
                        else:
                            self.status_changed.emit("✓ pip command succeeded.", "#22C55E")
                        break
                    else:
                        last_pip_error = (result.stderr or result.stdout or "").strip()[:150]
                except subprocess.TimeoutExpired:
                    last_pip_error = "pip timed out (120s)"
                    continue
                except FileNotFoundError:
                    last_pip_error = f"Python not found: {python_exe}"
                    continue
                except Exception as e:
                    last_pip_error = str(e)[:100]
                    continue

            if not pip_success:
                self.status_changed.emit(f"⚠ pip upgrade failed: {last_pip_error[:80]}", "#F59E0B")

            self.status_changed.emit("⏳ Checking for binary updates...", "#60A5FA")

            # ── Get version AFTER pip upgrade (via pip show — no cache) ──
            pkg_version_after = self._pip_show_version()

            # Force clean reimport for binary operations
            importlib.invalidate_caches()
            for mod_name in list(sys.modules.keys()):
                if mod_name == "cloakbrowser" or mod_name.startswith("cloakbrowser."):
                    del sys.modules[mod_name]

            try:
                cloakbrowser = importlib.import_module("cloakbrowser")
            except ImportError:
                self.finished.emit(False, "CloakBrowser not installed. Install via pip first.")
                return

            try:
                binary_info = getattr(cloakbrowser, "binary_info")
                ensure_binary = importlib.import_module("cloakbrowser.download").ensure_binary
            except Exception as exc:
                self.finished.emit(False, f"CloakBrowser update components unavailable: {str(exc)[:100]}")
                return

            info_before = binary_info() or {}
            bin_version_before = str(info_before.get("version") or "none")

            self.status_changed.emit("⏳ Downloading latest binary if available...", "#60A5FA")
            _log_handler = self._install_download_log_handler()
            try:
                ensure_binary()
            finally:
                self._remove_download_log_handler(_log_handler)

            # Reimport for fresh binary_info after ensure_binary
            for mod_name in list(sys.modules.keys()):
                if mod_name == "cloakbrowser" or mod_name.startswith("cloakbrowser."):
                    del sys.modules[mod_name]
            importlib.invalidate_caches()
            cloakbrowser = importlib.import_module("cloakbrowser")
            binary_info = getattr(cloakbrowser, "binary_info")

            info_after = binary_info() or {}
            bin_version_after = str(info_after.get("version") or "none")
            installed = bool(info_after.get("installed"))

            if not installed:
                self.finished.emit(False, "Binary download failed.")
                return

            # Check if either pip package or binary was updated
            pkg_updated = pkg_version_before not in ("none", "unknown") and pkg_version_before != pkg_version_after
            bin_updated = bin_version_before != bin_version_after

            if pkg_updated or bin_updated:
                changes = []
                if pkg_updated:
                    changes.append(f"pkg {pkg_version_before} → {pkg_version_after}")
                if bin_updated:
                    changes.append(f"binary {bin_version_before} → {bin_version_after}")
                self.finished.emit(True, f"Updated! {', '.join(changes)}")
            elif not pip_success:
                # pip upgrade failed — don't say "up to date", be honest
                self.finished.emit(
                    False,
                    f"pip upgrade failed (v{pkg_version_after}). Run manually: pip install cloakbrowser --upgrade",
                )
            else:
                self.finished.emit(
                    True,
                    f"Already on latest version (v{pkg_version_after}, binary {bin_version_after})",
                )
        except Exception as exc:
            error_str = str(exc)[:100]
            lower_error = error_str.lower()
            if any(token in lower_error for token in ("connection", "urlerror", "getaddrinfo", "timed out", "network")):
                self.finished.emit(False, "No internet connection. Try again later.")
            else:
                self.finished.emit(False, f"Update failed: {error_str}")


class ProxyConfigDialog(QDialog):
    def __init__(self, account_name, current_proxy=None, parent=None):
        super().__init__(parent)
        self.account_name = str(account_name or "").strip()
        self.setWindowTitle(f"Proxy Settings - {self.account_name or 'Account'}")
        self.setMinimumWidth(450)
        self.setStyleSheet(
            """
            QDialog { background: #1E293B; color: #F8FAFC; }
            QLabel { color: #E2E8F0; }
            QLineEdit, QComboBox {
                background: #0F172A;
                color: #F8FAFC;
                border: 1px solid #334155;
                border-radius: 6px;
                padding: 8px 10px;
            }
            QCheckBox { color: #F8FAFC; }
            """
        )

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        self.chk_enable = QCheckBox("Enable Proxy for this account")
        self.chk_enable.setStyleSheet("font-weight: 600; font-size: 13px;")
        self.chk_enable.toggled.connect(self._toggle_fields)
        layout.addWidget(self.chk_enable)

        self.fields_widget = QWidget()
        form = QFormLayout(self.fields_widget)
        form.setSpacing(10)

        self.cmb_protocol = QComboBox()
        self.cmb_protocol.addItems(["HTTP", "HTTPS", "SOCKS4", "SOCKS5"])
        self.cmb_protocol.setCurrentText("SOCKS5")
        form.addRow("Protocol:", self.cmb_protocol)

        self.txt_host = QLineEdit()
        self.txt_host.setPlaceholderText("e.g. proxy.example.com or 45.xx.xx.xx")
        form.addRow("Host:", self.txt_host)

        self.txt_port = QLineEdit()
        self.txt_port.setPlaceholderText("e.g. 1080, 8080, 3128")
        self.txt_port.setMaximumWidth(120)
        form.addRow("Port:", self.txt_port)

        self.chk_auth = QCheckBox("Requires username/password")
        self.chk_auth.toggled.connect(self._toggle_auth)
        form.addRow("", self.chk_auth)

        self.txt_user = QLineEdit()
        self.txt_user.setPlaceholderText("Username")
        self.lbl_user = QLabel("Username:")
        self.txt_pass = QLineEdit()
        self.txt_pass.setPlaceholderText("Password")
        self.txt_pass.setEchoMode(QLineEdit.Password)
        self.lbl_pass = QLabel("Password:")
        self.chk_show_pass = QCheckBox("Show password")
        self.chk_show_pass.toggled.connect(
            lambda checked: self.txt_pass.setEchoMode(
                QLineEdit.Normal if checked else QLineEdit.Password
            )
        )

        form.addRow(self.lbl_user, self.txt_user)
        form.addRow(self.lbl_pass, self.txt_pass)
        form.addRow("", self.chk_show_pass)
        layout.addWidget(self.fields_widget)

        self.lbl_preview = QLabel("")
        self.lbl_preview.setStyleSheet(
            "color: #94A3B8; font-size: 11px; padding: 8px; background: #0F172A; border-radius: 4px;"
        )
        self.lbl_preview.setWordWrap(True)
        layout.addWidget(self.lbl_preview)

        self.btn_test = QPushButton("Test Proxy Connection")
        self.btn_test.setStyleSheet(
            """
            QPushButton {
                background: rgba(59, 130, 246, 0.15);
                color: #3B82F6;
                border: 1px solid #3B82F6;
                border-radius: 6px;
                padding: 8px;
                font-weight: 600;
            }
            """
        )
        self.btn_test.clicked.connect(self._test_proxy)
        layout.addWidget(self.btn_test)

        self.lbl_test_result = QLabel("")
        self.lbl_test_result.setWordWrap(True)
        layout.addWidget(self.lbl_test_result)

        btn_row = QHBoxLayout()
        self.btn_save = QPushButton("Save")
        self.btn_save.setStyleSheet(
            """
            QPushButton {
                background: #3B82F6;
                color: white;
                border-radius: 6px;
                padding: 10px 24px;
                font-weight: 700;
            }
            """
        )
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setStyleSheet(
            "color: #94A3B8; border: 1px solid #475569; border-radius: 6px; padding: 10px 24px;"
        )
        self.btn_save.clicked.connect(self.accept)
        self.btn_cancel.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(self.btn_cancel)
        btn_row.addWidget(self.btn_save)
        layout.addLayout(btn_row)

        for widget in (self.txt_host, self.txt_port, self.txt_user, self.txt_pass):
            widget.textChanged.connect(self._update_preview)
        self.cmb_protocol.currentTextChanged.connect(self._update_preview)

        if current_proxy:
            self._load_proxy(current_proxy)

        self._toggle_auth(False)
        self.fields_widget.setVisible(False)
        self._update_preview()

    def _toggle_fields(self, enabled):
        self.fields_widget.setVisible(bool(enabled))
        self.btn_test.setEnabled(bool(enabled))
        self._update_preview()

    def _toggle_auth(self, checked):
        visible = bool(checked)
        self.txt_user.setVisible(visible)
        self.lbl_user.setVisible(visible)
        self.txt_pass.setVisible(visible)
        self.lbl_pass.setVisible(visible)
        self.chk_show_pass.setVisible(visible)
        self._update_preview()

    def _update_preview(self):
        if not self.chk_enable.isChecked():
            self.lbl_preview.setText("Proxy: Disabled (direct connection)")
            return
        url = self.get_proxy_url()
        if url:
            self.lbl_preview.setText(f"Proxy URL: {url}")
        else:
            self.lbl_preview.setText("Fill host and port")

    def get_proxy_url(self):
        if not self.chk_enable.isChecked():
            return ""

        protocol = self.cmb_protocol.currentText().strip().lower()
        host = self.txt_host.text().strip()
        port = self.txt_port.text().strip()
        if not host or not port:
            return ""

        if self.chk_auth.isChecked():
            user = self.txt_user.text().strip()
            pwd = self.txt_pass.text().strip()
            if user and pwd:
                return f"{protocol}://{user}:{pwd}@{host}:{port}"

        return f"{protocol}://{host}:{port}"

    def _load_proxy(self, proxy_url):
        parsed = urlparse(str(proxy_url or "").strip())
        if not parsed.scheme:
            return
        self.chk_enable.setChecked(True)
        protocol = parsed.scheme.upper()
        if protocol in {"HTTP", "HTTPS", "SOCKS4", "SOCKS5"}:
            self.cmb_protocol.setCurrentText(protocol)
        self.txt_host.setText(parsed.hostname or "")
        self.txt_port.setText(str(parsed.port) if parsed.port else "")
        if parsed.username:
            self.chk_auth.setChecked(True)
            self.txt_user.setText(parsed.username)
            self.txt_pass.setText(parsed.password or "")

    def _test_proxy(self):
        url = self.get_proxy_url()
        if not url:
            self.lbl_test_result.setText("Fill proxy details first")
            self.lbl_test_result.setStyleSheet("color: #EF4444;")
            return

        self.lbl_test_result.setText("Testing...")
        self.lbl_test_result.setStyleSheet("color: #F59E0B;")
        QApplication.processEvents()

        try:
            import requests
        except Exception as exc:
            self.lbl_test_result.setText(f"Failed: requests not available ({exc})")
            self.lbl_test_result.setStyleSheet("color: #EF4444;")
            return

        try:
            proxies = {"http": url, "https": url}
            response = requests.get("https://httpbin.org/ip", proxies=proxies, timeout=10)
            if response.status_code == 200:
                ip_value = response.json().get("origin", "unknown")
                self.lbl_test_result.setText(f"Connected. Proxy IP: {ip_value}")
                self.lbl_test_result.setStyleSheet("color: #22C55E;")
            else:
                self.lbl_test_result.setText(f"HTTP {response.status_code}")
                self.lbl_test_result.setStyleSheet("color: #EF4444;")
        except Exception as exc:
            self.lbl_test_result.setText(f"Failed: {str(exc)[:120]}")
            self.lbl_test_result.setStyleSheet("color: #EF4444;")


class BulkQueueAddWorker(QThread):
    progress = Signal(int, int)
    completed = Signal(int)
    failed = Signal(str)

    def __init__(self, job_specs, parent=None):
        super().__init__(parent)
        self.job_specs = list(job_specs or [])

    def run(self):
        try:
            inserted = add_jobs_bulk(
                self.job_specs,
                progress_cb=lambda done, total: self.progress.emit(int(done), int(total)),
            )
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.completed.emit(int(inserted or 0))


class WorkerSignals(QObject):
    finished = Signal(object)
    error = Signal(str)


class BackgroundTask(QRunnable):
    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()

    def run(self):
        try:
            result = self.fn(*self.args, **self.kwargs)
        except Exception as exc:
            self.signals.error.emit(str(exc))
            return
        self.signals.finished.emit(result)


class UIUpdateThrottler(QObject):
    def __init__(self, parent=None, interval_ms=250):
        super().__init__(parent)
        self._pending = {}
        self._timer = QTimer(self)
        self._timer.setInterval(max(50, int(interval_ms)))
        self._timer.timeout.connect(self._flush)
        self._timer.start()

    def schedule(self, key, callback):
        if callable(callback):
            self._pending[str(key)] = callback

    def _flush(self):
        if not self._pending:
            return
        updates = list(self._pending.items())
        self._pending.clear()
        for _key, callback in updates:
            try:
                callback()
            except Exception:
                pass


class LogBuffer(QObject):
    def __init__(self, text_edit, parent=None, interval_ms=200):
        super().__init__(parent)
        self.text_edit = text_edit
        self._pending = []
        self._timer = QTimer(self)
        self._timer.setInterval(max(50, int(interval_ms)))
        self._timer.timeout.connect(self.flush)
        self._timer.start()

    def append(self, message):
        if message is None:
            return
        self._pending.append(str(message))

    def clear(self):
        self._pending.clear()
        if self.text_edit is not None:
            self.text_edit.clear()

    @staticmethod
    def _colorize_line(line):
        """Apply color to log line based on content keywords."""
        import html as _html
        escaped = _html.escape(line)
        # Determine line color based on keywords
        low = line.lower()
        if any(k in low for k in ("failed", "error", "exception", "traceback", "❌")):
            color = "#EF4444"  # red
        elif any(k in low for k in ("warning", "warn", "⚠")):
            color = "#F59E0B"  # yellow
        elif any(k in low for k in ("completed", "saved:", "done", "success", "✓", "✅")):
            color = "#22C55E"  # green
        elif any(k in low for k in ("[credits]", "remaining:", "estimated cost")):
            color = "#22C55E"  # green
        elif any(k in low for k in ("generating", "poll", "running", "uploading")):
            color = "#60A5FA"  # blue
        elif any(k in low for k in ("[bridge]", "[extmode]", "extension", "connected")):
            color = "#60A5FA"  # blue
        elif any(k in low for k in ("stopped", "stopping", "cancelled")):
            color = "#F59E0B"  # yellow
        else:
            color = "#94A3B8"  # default grey
        # Highlight bracketed tags like [e1], [Bridge], [CREDITS] etc.
        import re
        def _highlight_tag(m):
            tag = _html.escape(m.group(0))
            return f'<span style="color:#60A5FA;font-weight:600;">{tag}</span>'
        escaped = re.sub(r'\[[^\]]{1,20}\]', _highlight_tag, escaped)
        return f'<span style="color:{color};">{escaped}</span>'

    def flush(self):
        if not self._pending or self.text_edit is None:
            return
        lines = self._pending
        self._pending = []
        html_lines = [self._colorize_line(l) for l in lines]
        html_payload = "<br>".join(html_lines)
        cursor = self.text_edit.textCursor()
        cursor.movePosition(QTextCursor.End)
        if self.text_edit.document().blockCount() > 1:
            cursor.insertHtml("<br>" + html_payload)
        else:
            cursor.insertHtml(html_payload)
        self.text_edit.setTextCursor(cursor)
        scrollbar = self.text_edit.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())


class VirtualLiveGridWidget(QWidget):
    CARD_SIZES = {
        "small": (156, 210),
        "medium": (216, 286),
        "large": (280, 360),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._jobs = []
        self._card_size = "medium"
        self._columns = 1
        self._spacing = 12
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAutoFillBackground(False)

    def set_card_size(self, size_key):
        normalized = str(size_key or "medium").strip().lower()
        if normalized not in self.CARD_SIZES:
            normalized = "medium"
        if self._card_size != normalized:
            self._card_size = normalized
            self._reflow()

    def set_jobs(self, jobs):
        self._jobs = [dict(job or {}) for job in list(jobs or [])]
        self._reflow()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow()

    def sizeHint(self):
        width = max(320, self.width() or 320)
        height = max(220, self.minimumHeight() or 220)
        return QSize(width, height)

    def _reflow(self):
        card_w, card_h = self.CARD_SIZES.get(self._card_size, self.CARD_SIZES["medium"])
        available_width = max(320, self.width() or self.parentWidget().width() if self.parentWidget() else 320)
        self._columns = max(1, (available_width + self._spacing) // (card_w + self._spacing))
        rows = max(1, (len(self._jobs) + self._columns - 1) // self._columns)
        total_height = rows * (card_h + self._spacing) + self._spacing
        self.setMinimumHeight(total_height)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(event.rect(), QColor("#111827"))

        card_w, card_h = self.CARD_SIZES.get(self._card_size, self.CARD_SIZES["medium"])
        row_height = card_h + self._spacing
        first_row = max(0, event.rect().top() // max(1, row_height))
        last_row = max(first_row, (event.rect().bottom() // max(1, row_height)) + 1)

        status_colors = {
            "pending": QColor("#94A3B8"),
            "running": QColor("#3B82F6"),
            "completed": QColor("#22C55E"),
            "failed": QColor("#EF4444"),
            "moderated": QColor("#F59E0B"),
        }
        status_bg = {
            "pending": QColor("#1E293B"),
            "running": QColor("#0C2D5E"),
            "completed": QColor("#052E16"),
            "failed": QColor("#2A0F12"),
            "moderated": QColor("#2A1F08"),
        }
        status_labels = {
            "pending": "QUEUED",
            "running": "● LIVE",
            "completed": "✓ DONE",
            "failed": "✗ FAIL",
            "moderated": "⚠ FLAG",
        }

        prompt_font = QFont()
        prompt_font.setPointSize(10)
        prompt_font.setBold(True)
        meta_font = QFont()
        meta_font.setPointSize(8)
        header_font = QFont()
        header_font.setPointSize(9)
        header_font.setBold(True)
        badge_font = QFont()
        badge_font.setPointSize(8)
        badge_font.setBold(True)
        big_status_font = QFont()
        big_status_font.setPointSize(14)
        big_status_font.setBold(True)

        for row in range(first_row, last_row + 1):
            for col in range(self._columns):
                idx = row * self._columns + col
                if idx >= len(self._jobs):
                    break

                job = self._jobs[idx]
                x = self._spacing + col * (card_w + self._spacing)
                y = self._spacing + row * row_height
                rect = QRectF(x, y, card_w, card_h)

                status = str(job.get("status") or "pending").strip().lower()
                s_color = status_colors.get(status, QColor("#94A3B8"))
                s_bg = status_bg.get(status, QColor("#1E293B"))
                queue_no = job.get("output_index") if job.get("is_retry") else job.get("queue_no")
                if queue_no is None:
                    queue_no = idx + 1
                prompt = str(job.get("prompt") or "(No prompt)")
                meta = str(job.get("account") or "Waiting in queue")
                progress = str(job.get("progress") or "")

                # ── Card background ──
                is_retry = job.get("is_retry")
                card_bg = QColor("#162033") if is_retry else QColor("#1E293B")
                painter.setPen(Qt.NoPen)
                painter.setBrush(card_bg)
                painter.drawRoundedRect(rect, 12, 12)

                # Top accent line (3px colored bar)
                accent_rect = QRectF(x + 1, y + 1, card_w - 2, 3)
                painter.setBrush(s_color)
                painter.drawRoundedRect(accent_rect, 2, 2)

                # ── Header row: #N + status badge ──
                painter.setFont(header_font)
                painter.setPen(QColor("#CBD5E1"))
                painter.drawText(QRectF(x + 14, y + 14, card_w * 0.4, 20), Qt.AlignLeft | Qt.AlignVCenter, f"#{queue_no}")

                # Status badge (pill)
                badge_text = status_labels.get(status, status.upper())
                painter.setFont(badge_font)
                fm = painter.fontMetrics()
                badge_w = fm.horizontalAdvance(badge_text) + 16
                badge_h = 22
                badge_x = x + card_w - 14 - badge_w
                badge_y = y + 12
                painter.setPen(Qt.NoPen)
                painter.setBrush(s_bg)
                painter.drawRoundedRect(QRectF(badge_x, badge_y, badge_w, badge_h), 6, 6)
                painter.setPen(s_color)
                painter.drawText(QRectF(badge_x, badge_y, badge_w, badge_h), Qt.AlignCenter, badge_text)

                # ── Preview area ──
                preview_y = y + 42
                preview_h = max(72, int(card_h * 0.36))
                preview_rect = QRectF(x + 12, preview_y, card_w - 24, preview_h)
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor("#0F172A"))
                painter.drawRoundedRect(preview_rect, 10, 10)

                # Status icon in preview
                painter.setFont(big_status_font)
                painter.setPen(s_color)
                painter.drawText(preview_rect, Qt.AlignCenter, status_labels.get(status, status.upper()))

                # ── Prompt text ──
                prompt_y = preview_y + preview_h + 10
                painter.setFont(prompt_font)
                painter.setPen(QColor("#E2E8F0"))
                prompt_rect = QRectF(x + 14, prompt_y, card_w - 28, 40)
                prompt_text = fm.elidedText(prompt.replace("\n", " "), Qt.ElideRight, max(40, int(prompt_rect.width() * 2)))
                painter.drawText(prompt_rect, Qt.TextWordWrap, prompt_text)

                # ── Meta info (bottom) ──
                painter.setFont(meta_font)
                # Separator line
                sep_y = y + card_h - 48
                painter.setPen(QPen(QColor("#1E293B"), 1))
                painter.drawLine(int(x + 14), int(sep_y), int(x + card_w - 14), int(sep_y))

                painter.setPen(QColor("#64748B"))
                painter.drawText(QRectF(x + 14, y + card_h - 42, card_w - 28, 16), Qt.AlignLeft | Qt.AlignVCenter, meta[:42])
                type_text = progress[:42] if progress else str(job.get("job_type") or "image").title()
                painter.setPen(QColor("#475569"))
                painter.drawText(QRectF(x + 14, y + card_h - 24, card_w - 28, 16), Qt.AlignLeft | Qt.AlignVCenter, type_text)

                # Card border (draw last so it's on top)
                painter.setPen(QPen(QColor("#334155"), 1))
                painter.setBrush(Qt.NoBrush)
                painter.drawRoundedRect(rect, 12, 12)

        painter.end()


class ThrottledTableUpdater(QObject):
    def __init__(self, parent, flush_callback, interval_ms=200):
        super().__init__(parent)
        self.flush_callback = flush_callback
        self.pending_job_ids = set()
        self.timer = QTimer(self)
        self.timer.setInterval(max(50, int(interval_ms)))
        self.timer.timeout.connect(self._flush)
        self.timer.start()

    def queue_job(self, job_id):
        if job_id:
            self.pending_job_ids.add(str(job_id))

    def queue_many(self, job_ids):
        for job_id in list(job_ids or []):
            self.queue_job(job_id)

    def _flush(self):
        if not self.pending_job_ids:
            return
        pending = list(self.pending_job_ids)
        self.pending_job_ids.clear()
        self.flush_callback(pending)


class ThrottledStatsUpdater(QObject):
    def __init__(self, parent, update_callback, interval_ms=500):
        super().__init__(parent)
        self.update_callback = update_callback
        self._latest_jobs = None
        self._dirty = False
        self.timer = QTimer(self)
        self.timer.setInterval(max(100, int(interval_ms)))
        self.timer.timeout.connect(self._update)
        self.timer.start()

    def mark_dirty(self, jobs=None):
        if jobs is not None:
            self._latest_jobs = list(jobs)
        self._dirty = True

    def _update(self):
        if not self._dirty:
            return
        self._dirty = False
        self.update_callback(self._latest_jobs)


class BulkImageDropTable(QTableWidget):
    files_dropped = Signal(list)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        paths = [url.toLocalFile() for url in urls if url.isLocalFile()]
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()
            return
        super().dropEvent(event)


class LiveJobCard(QFrame):
    CARD_SIZES = {
        "small": (156, 210),
        "medium": (216, 286),
        "large": (280, 360),
    }

    def __init__(self, job_data, card_size="medium", parent=None):
        super().__init__(parent)
        self.job_data = dict(job_data or {})
        self.card_size = str(card_size or "medium")
        self.setObjectName("liveJobCard")
        self._build_ui()
        self._apply_card_size()
        self.update_job(job_data or {})

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        self.preview_label = QLabel()
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setObjectName("livePreview")
        layout.addWidget(self.preview_label)

        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(6)

        self.lbl_job_num = QLabel("#?")
        self.lbl_job_num.setObjectName("liveJobNumber")
        header_layout.addWidget(self.lbl_job_num)
        header_layout.addStretch()

        self.lbl_status = QLabel("")
        self.lbl_status.setObjectName("liveJobStatus")
        header_layout.addWidget(self.lbl_status)
        layout.addLayout(header_layout)

        self.job_progress = QProgressBar()
        self.job_progress.setRange(0, 100)
        self.job_progress.setTextVisible(False)
        self.job_progress.setFixedHeight(6)
        self.job_progress.setObjectName("liveJobProgress")
        self.job_progress.setVisible(False)
        layout.addWidget(self.job_progress)

        self.lbl_prompt = QLabel("")
        self.lbl_prompt.setWordWrap(True)
        self.lbl_prompt.setObjectName("liveJobPrompt")
        layout.addWidget(self.lbl_prompt)

        self.lbl_meta = QLabel("")
        self.lbl_meta.setWordWrap(True)
        self.lbl_meta.setObjectName("liveJobMeta")
        layout.addWidget(self.lbl_meta)
        layout.addStretch()

    def _apply_card_size(self):
        width, height = self.CARD_SIZES.get(self.card_size, self.CARD_SIZES["medium"])
        self.setFixedSize(width, height)
        preview_height = max(96, width - 28)
        if self.card_size == "large":
            preview_height = max(140, width - 34)
        self.preview_label.setFixedHeight(preview_height)

    def _set_preview_text(self, text, *, bg="#0F172A", color="#64748B", border="#334155"):
        self.preview_label.setPixmap(QPixmap())
        self.preview_label.setText(text)
        self.preview_label.setStyleSheet(
            f"background: {bg}; color: {color}; border: 1px solid {border}; "
            "border-radius: 8px; font-size: 30px; font-weight: 700;"
        )

    def _set_status_style(self, text, color):
        self.lbl_status.setText(text)
        self.lbl_status.setStyleSheet(
            f"color: {color}; font-size: 11px; font-weight: 700; background: transparent; border: none;"
        )

    def _estimate_progress(self):
        status = str(self.job_data.get("status") or "pending").strip().lower()
        if status == "completed":
            return 100
        if status != "running":
            return 0

        job_type = str(self.job_data.get("job_type") or "").strip().lower()
        step = str(self.job_data.get("progress_step") or "").strip().lower()
        poll_count = max(0, int(self.job_data.get("progress_poll_count") or 0))

        if job_type == "pipeline":
            if step == "image":
                return min(30, 10 + (poll_count * 8))
            if step == "download":
                return 95
            return 30 + min(65, poll_count * 7)

        if step == "image":
            return min(90, 20 + (poll_count * 25))
        if step == "download":
            return 95
        return min(95, max(8, poll_count * 10))

    @staticmethod
    def _extract_video_thumbnail(video_path):
        target = str(video_path or "").strip()
        if not target or not os.path.exists(target):
            return None
        thumb_path = f"{target}_thumb.jpg"
        if os.path.exists(thumb_path):
            return thumb_path
        try:
            _no_window = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)} if sys.platform.startswith("win") else {}
            subprocess.run(
                [
                    "ffmpeg",
                    "-i",
                    target,
                    "-vf",
                    "select=eq(n\\,0)",
                    "-frames:v",
                    "1",
                    "-y",
                    thumb_path,
                ],
                capture_output=True,
                timeout=8,
                check=False,
                **_no_window,
            )
        except Exception:
            return None
        return thumb_path if os.path.exists(thumb_path) else None

    def _load_preview(self, output_path):
        resolved = str(output_path or "").strip()
        if not resolved or not os.path.exists(resolved):
            self._set_preview_text("✅", bg="#112020", color="#22C55E", border="#22C55E")
            return

        preview_width = max(96, self.preview_label.width() - 8)
        preview_height = max(96, self.preview_label.height() - 8)
        source_path = resolved
        suffix = Path(resolved).suffix.lower()
        if suffix == ".mp4":
            thumb_path = self._extract_video_thumbnail(resolved)
            if thumb_path:
                source_path = thumb_path
            else:
                self._set_preview_text("🎬", bg="#112020", color="#22C55E", border="#22C55E")
                return

        pixmap = QPixmap(source_path)
        if pixmap.isNull():
            fallback = "🎬" if suffix == ".mp4" else "🖼"
            self._set_preview_text(fallback, bg="#112020", color="#22C55E", border="#22C55E")
            return

        scaled = pixmap.scaled(preview_width, preview_height, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.preview_label.setText("")
        self.preview_label.setStyleSheet("background: #0F172A; border: 1px solid #334155; border-radius: 8px;")
        self.preview_label.setPixmap(scaled)

    def update_job(self, job_data):
        self.job_data = dict(job_data or {})
        queue_no = self.job_data.get("queue_no") or self.job_data.get("index") or "?"
        self.lbl_job_num.setText(f"Job {queue_no}")

        prompt = str(self.job_data.get("prompt") or "").strip()
        prompt_snippet = prompt if len(prompt) <= 56 else (prompt[:53] + "...")
        self.lbl_prompt.setText(prompt_snippet or "(No prompt)")
        self.lbl_prompt.setToolTip(prompt or "(No prompt)")

        status = str(self.job_data.get("status") or "pending").strip().lower()
        output_path = str(self.job_data.get("output_path") or "").strip()
        error_text = str(self.job_data.get("error") or "").strip()
        progress_step = str(self.job_data.get("progress_step") or "").strip().lower()
        poll_count = max(0, int(self.job_data.get("progress_poll_count") or 0))

        self.job_progress.setVisible(False)
        self.job_progress.setValue(0)
        self.job_progress.setStyleSheet(
            "QProgressBar { background: #334155; border: none; border-radius: 3px; } "
            "QProgressBar::chunk { background: #3B82F6; border-radius: 3px; }"
        )

        if status == "completed":
            self._set_status_style("✅ Done", "#22C55E")
            self.lbl_meta.setText(os.path.basename(output_path) if output_path else "Output ready")
            self.lbl_meta.setToolTip(output_path or "")
            self.job_progress.setValue(100)
            self.job_progress.setStyleSheet(
                "QProgressBar { background: #334155; border: none; border-radius: 3px; } "
                "QProgressBar::chunk { background: #22C55E; border-radius: 3px; }"
            )
            self.job_progress.setVisible(True)
            self._load_preview(output_path)
            return

        if status == "failed":
            self._set_status_style("❌ Failed", "#EF4444")
            meta = error_text if len(error_text) <= 54 else (error_text[:51] + "...")
            self.lbl_meta.setText(meta or "Generation failed")
            self.lbl_meta.setToolTip(error_text or "Generation failed")
            self._set_preview_text("❌", bg="#1F1A2A", color="#EF4444", border="#EF4444")
            return

        if status == "running":
            progress_value = self._estimate_progress()
            if progress_step == "image":
                status_text = "🖼 Image"
                meta = "Generating source image..."
            elif progress_step == "download":
                status_text = "⬇ Download"
                meta = "Finalizing output..."
            else:
                status_text = f"🔄 {progress_value}%"
                meta = f"poll {poll_count}/10" if poll_count > 0 else "Submitting..."
                if str(self.job_data.get("job_type") or "").strip().lower() == "pipeline":
                    status_text = f"🎬 {progress_value}%"
            self._set_status_style(status_text, "#3B82F6")
            self.lbl_meta.setText(meta)
            self.lbl_meta.setToolTip(meta)
            self.job_progress.setValue(progress_value)
            self.job_progress.setVisible(True)
            self._set_preview_text("🔄", bg="#0F172A", color="#3B82F6", border="#334155")
            return

        self._set_status_style("⏳ Queued", "#94A3B8")
        self.lbl_meta.setText("Waiting in queue")
        self.lbl_meta.setToolTip("Waiting in queue")
        self._set_preview_text("⏳", bg="#0F172A", color="#475569", border="#334155")


class SidebarNav(QFrame):
    """
    Fluent-powered sidebar navigation.

    Public API (preserved from the old QFrame version — DO NOT change
    signatures, several call sites in MainWindow depend on these):
        - Signal: page_selected(str)
        - set_active(key)
        - update_stats(pending, running, done, failed, session_total)
        - setFixedWidth(width)

    Internal implementation uses qfluentwidgets.NavigationInterface for
    the nav buttons (with icons, hover states, smooth selection
    animations). The footer stats panel is a lightweight QWidget under
    the nav — still contains the clickable "Failed" button.
    """
    page_selected = Signal(str)

    NAV_ITEMS = [
        ("dashboard", "Image Generation"),
        ("video", "Video Generation"),
        ("accounts", "Account Manager"),
        ("live", "Live Generation"),
        ("failed", "Failed Jobs"),
        ("settings", "Settings"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("sidebarNav")
        self.setFixedWidth(220)  # slightly wider for Fluent nav labels

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Title header ──────────────────────────────────────────
        title_wrap = QWidget()
        title_wrap.setObjectName("sidebarTitleWrap")
        title_layout = QVBoxLayout(title_wrap)
        title_layout.setContentsMargins(18, 18, 18, 10)
        title_layout.setSpacing(0)
        self.lbl_title = QLabel("G-Labs\nAutomation")
        self.lbl_title.setObjectName("sidebarTitle")
        title_layout.addWidget(self.lbl_title)
        self.lbl_byline = QLabel("by MegaShoeb")
        self.lbl_byline.setObjectName("sidebarByline")
        title_layout.addWidget(self.lbl_byline)
        root.addWidget(title_wrap)

        # ── Fluent NavigationInterface ────────────────────────────
        if _FLUENT_UI_AVAILABLE and NavigationInterface is not None:
            self.nav = NavigationInterface(
                self, showMenuButton=False, showReturnButton=False
            )
            self.nav.setExpandWidth(220)
            # Map route keys to their icons
            nav_icons = {
                "dashboard": FluentIcon.PHOTO,
                "video": FluentIcon.VIDEO,
                "accounts": FluentIcon.PEOPLE,
                "live": FluentIcon.PLAY,
                "failed": FluentIcon.CANCEL,
                "settings": FluentIcon.SETTING,
            }
            for key, label in self.NAV_ITEMS:
                icon = nav_icons.get(key, FluentIcon.HOME)
                # NavigationWidget.clicked is Signal(bool) — the lambda's
                # first positional arg MUST accept the bool payload or it
                # overrides the captured route key default. Use an
                # explicit swallow arg to preserve the captured `k`.
                self.nav.addItem(
                    routeKey=key,
                    icon=icon,
                    text=label,
                    onClick=(lambda _checked=False, k=key: self._emit_nav(k)),
                    selectable=True,
                    position=NavigationItemPosition.TOP,
                )
            root.addWidget(self.nav, 1)
            self.nav_buttons = None  # not used in Fluent mode
        else:
            # Fallback: original QPushButton-based sidebar when Fluent
            # library is unavailable (shouldn't happen in normal runs).
            self.nav = None
            self.nav_buttons = {}
            fallback_layout = QVBoxLayout()
            fallback_layout.setContentsMargins(10, 6, 10, 6)
            fallback_layout.setSpacing(6)
            for key, label in self.NAV_ITEMS:
                btn = QPushButton(label)
                btn.setObjectName("sidebarNavButton")
                btn.setCheckable(True)
                btn.setCursor(Qt.PointingHandCursor)
                btn.clicked.connect(
                    lambda checked=False, nav_key=key: self._emit_nav(nav_key)
                )
                fallback_layout.addWidget(btn)
                self.nav_buttons[key] = btn
            fallback_layout.addStretch(1)
            fallback_wrap = QWidget()
            fallback_wrap.setLayout(fallback_layout)
            root.addWidget(fallback_wrap, 1)

        # ── Divider above stats ───────────────────────────────────
        bottom_divider = QFrame()
        bottom_divider.setFrameShape(QFrame.HLine)
        bottom_divider.setObjectName("sidebarDivider")
        root.addWidget(bottom_divider)

        # ── Stats footer panel — professional mini stat cards ────
        stats_wrap = QWidget()
        stats_wrap.setObjectName("sidebarStatsWrap")
        stats_layout = QVBoxLayout(stats_wrap)
        stats_layout.setContentsMargins(14, 14, 14, 14)
        stats_layout.setSpacing(6)

        # Section header
        stats_header = QLabel("QUEUE STATS")
        stats_header.setObjectName("sidebarStatsHeader")
        stats_layout.addWidget(stats_header)
        stats_layout.addSpacing(2)

        def _build_stat_row(key, label_text, color):
            """Build a mini stat row: colored dot + label + count.
            Returns (row_widget, count_label)."""
            row = QFrame()
            row.setObjectName("sidebarStatRow")
            row.setProperty("variant", key)
            rl = QHBoxLayout(row)
            rl.setContentsMargins(10, 6, 10, 6)
            rl.setSpacing(8)
            dot = QLabel("●")
            dot.setObjectName("sidebarStatDot")
            dot.setStyleSheet(f"color: {color}; font-size: 14px; background: transparent;")
            dot.setFixedWidth(10)
            lbl = QLabel(label_text)
            lbl.setObjectName("sidebarStatLabel")
            count = QLabel("0")
            count.setObjectName("sidebarStatCount")
            count.setProperty("variant", key)
            count.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            rl.addWidget(dot)
            rl.addWidget(lbl, 1)
            rl.addWidget(count)
            return row, count

        pending_row, self.lbl_pending = _build_stat_row("pending", "Pending", "#94A3B8")
        running_row, self.lbl_running = _build_stat_row("running", "Running", "#60A5FA")
        done_row, self.lbl_done = _build_stat_row("done", "Done", "#22C55E")
        stats_layout.addWidget(pending_row)
        stats_layout.addWidget(running_row)
        stats_layout.addWidget(done_row)

        # Failed: clickable row (navigates to failed jobs page)
        failed_row = QFrame()
        failed_row.setObjectName("sidebarStatRow")
        failed_row.setProperty("variant", "failed")
        failed_row.setCursor(Qt.PointingHandCursor)
        failed_layout_inner = QHBoxLayout(failed_row)
        failed_layout_inner.setContentsMargins(10, 6, 10, 6)
        failed_layout_inner.setSpacing(8)
        failed_dot = QLabel("●")
        failed_dot.setStyleSheet("color: #EF4444; font-size: 14px; background: transparent;")
        failed_dot.setFixedWidth(10)
        failed_lbl = QLabel("Failed")
        failed_lbl.setObjectName("sidebarStatLabel")
        # Store count label as btn_failed for backwards compat with update_stats
        # naming (even though it's now a QLabel, not a QPushButton). We
        # wire click via a mousePressEvent on the row frame.
        self.btn_failed = QLabel("0")
        self.btn_failed.setObjectName("sidebarStatCount")
        self.btn_failed.setProperty("variant", "failed")
        self.btn_failed.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        failed_layout_inner.addWidget(failed_dot)
        failed_layout_inner.addWidget(failed_lbl, 1)
        failed_layout_inner.addWidget(self.btn_failed)

        def _failed_row_mouse_press(event, self_ref=self):
            self_ref._emit_nav("failed")
        failed_row.mousePressEvent = _failed_row_mouse_press
        self._failed_row = failed_row
        stats_layout.addWidget(failed_row)

        # Session footer — subtle, below a divider
        session_divider = QFrame()
        session_divider.setObjectName("sidebarStatsInnerDivider")
        session_divider.setFrameShape(QFrame.HLine)
        session_divider.setFixedHeight(1)
        stats_layout.addSpacing(4)
        stats_layout.addWidget(session_divider)
        stats_layout.addSpacing(4)
        self.lbl_session = QLabel("Session: 0 images")
        self.lbl_session.setObjectName("sidebarSession")
        self.lbl_session.setAlignment(Qt.AlignCenter)
        stats_layout.addWidget(self.lbl_session)

        root.addWidget(stats_wrap)

        self._active_key = ""
        self.set_active("dashboard")

    def _emit_nav(self, key):
        self.set_active(key)
        self.page_selected.emit(str(key))

    def set_active(self, key):
        """Mark a nav item as selected. Called by MainWindow._sync_sidebar_selection."""
        key_str = str(key or "")
        if not key_str:
            return
        self._active_key = key_str
        if self.nav is not None:
            try:
                self.nav.setCurrentItem(key_str)
            except Exception:
                pass
        elif self.nav_buttons:
            for btn_key, button in self.nav_buttons.items():
                button.setChecked(btn_key == key_str)

    def update_stats(self, pending, running, done, failed, session_total):
        """Refresh the footer stats panel. In the new stat-card layout,
        labels are just the count (e.g. "5") not "Pending: 5" — the
        metric name is shown separately in each row."""
        self.lbl_pending.setText(str(int(pending or 0)))
        self.lbl_running.setText(str(int(running or 0)))
        self.lbl_done.setText(str(int(done or 0)))
        self.btn_failed.setText(str(int(failed or 0)))
        self.lbl_session.setText(f"Session: {int(session_total or 0)} images")
        has_failures = int(failed or 0) > 0
        self.btn_failed.setProperty("hasFailures", has_failures)
        self.btn_failed.style().unpolish(self.btn_failed)
        self.btn_failed.style().polish(self.btn_failed)
        # Also update the parent row style so it lights up red when
        # failures exist (CSS [hasFailures="true"] selector).
        if hasattr(self, "_failed_row"):
            self._failed_row.setProperty("hasFailures", has_failures)
            self._failed_row.style().unpolish(self._failed_row)
            self._failed_row.style().polish(self._failed_row)


class MainWindow(QMainWindow):
    warmup_progress_signal = Signal(str, int, str)
    warmup_complete_signal = Signal(str, bool, str)
    _ext_accounts_signal = Signal(object)  # carries dict or None from bg thread

    def __init__(self):
        super().__init__()
        self._kill_zombie_browsers(startup=True)
        self._cleanup_stale_locks()
        app = QApplication.instance()
        if app is not None and str(app.style().objectName()).lower() != "fusion":
            app.setStyle("Fusion")
        # Per-widget wheel filter: installed directly on each spin/combo
        # after setup_* methods complete. See _install_wheel_filters().
        self._wheel_filter = _WheelEventFilter(self)
        self.setObjectName("mainWindow")
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAutoFillBackground(True)
        self.setWindowTitle("G-Labs Multi-Account Automation App")
        self.resize(1280, 860)
        # Lower minimum so the window can fit on smaller laptops
        # (13"/14" screens at 1366x768 or scaled displays). Scroll
        # area handles overflow gracefully.
        self.setMinimumSize(900, 560)
        self.setWindowState(Qt.WindowMaximized)
        
        self.central_widget = QWidget()
        self.central_widget.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.central_widget.setAutoFillBackground(True)
        self.setCentralWidget(self.central_widget)
        self.main_layout = QHBoxLayout(self.central_widget)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("mainTabs")
        self.tabs.setDocumentMode(True)
        self.tabs.tabBar().setObjectName("mainAppTabBar")
        self.tabs.tabBar().setDrawBase(False)
        self.tabs.tabBar().hide()

        self.sidebar = SidebarNav(self)
        self.sidebar.page_selected.connect(self._on_sidebar_page_selected)
        self.main_layout.addWidget(self.sidebar)

        self.sidebar_divider = QFrame()
        self.sidebar_divider.setFrameShape(QFrame.VLine)
        self.sidebar_divider.setObjectName("sidebarShellDivider")
        self.sidebar_divider.setFixedWidth(1)
        self.main_layout.addWidget(self.sidebar_divider)

        self.main_layout.addWidget(self.tabs, 1)
        
        # Tabs
        self.tab_dashboard = QWidget()
        self.tab_accounts = QWidget()
        self.tab_live_generation = QWidget()
        self.tab_failed_jobs = QWidget()
        self.tab_settings = QWidget()
        
        self.tabs.addTab(self.tab_dashboard, "Dashboard")
        self.tabs.addTab(self.tab_accounts, "Account Manager")
        self.tabs.addTab(self.tab_live_generation, "Live Generation")
        self.tabs.addTab(self.tab_failed_jobs, "Failed Jobs")
        self.tabs.addTab(self.tab_settings, "Settings")
        
        self.queue_manager = None
        self.pending_clear_all = False
        self.queue_running = False
        self.queue_paused = False
        self.queue_stopping = False
        self._app_closing = False
        self._pending_settings_sync_ready = False
        self.account_runtime_state = {}
        self.account_login_state = {}
        self._runtime_auth_status = {}  # account_name -> "expired" (cleared on success)
        self.warmup_widgets = {}
        self.active_warmup_progress = {}
        self._pending_login_add = None
        self.failed_prompt_edits = {}
        self.login_worker = None
        self.login_check_worker = None
        self.dola_delete_worker = None
        self.relogin_worker = None
        self._completion_times = []
        self._generation_start_time = None
        self._terminal_job_states = {}
        self._account_status_auto_check_done = False
        self.bulk_queue_add_worker = None
        self.bulk_add_progress_dialog = None
        self._bulk_add_success_logs = []
        self._bulk_add_after_success = None
        self.thread_pool = QThreadPool.globalInstance()
        self._background_tasks = set()
        self._cleanup_thread = None
        self._cleanup_started = False
        self.ui_throttler = UIUpdateThrottler(self, interval_ms=250)
        self._queue_row_map = {}
        self._queue_job_order = []
        self._queue_row_snapshots = {}
        self._latest_queue_jobs = []
        self._latest_accounts = []
        self._queue_snapshot_inflight = False
        self._queue_snapshot_requested = False
        self._failed_jobs_dirty = True
        self._failed_jobs_inflight = False
        self._latest_failed_jobs = []
        self._live_tab_dirty = True
        self._grid_scrolling = False
        self._pending_live_jobs = None
        self._grid_scroll_resume_timer = QTimer(self)
        self._grid_scroll_resume_timer.setSingleShot(True)
        self._grid_scroll_resume_timer.timeout.connect(self._on_grid_scroll_idle)
        self.account_runtime_timer = QTimer(self)
        self.account_runtime_timer.setInterval(1000)
        self.account_runtime_timer.timeout.connect(self._on_account_runtime_tick)
        self.account_status_timer = QTimer(self)
        self.account_status_timer.setInterval(10000)
        self.account_status_timer.timeout.connect(self._refresh_login_statuses)
        self.warmup_progress_signal.connect(self._on_warmup_progress)
        self.warmup_complete_signal.connect(self._on_warmup_complete)
        
        self.setup_dashboard()
        self.setup_accounts()
        self.setup_live_generation()
        self.setup_failed_jobs()
        self.setup_settings()
        self.table_updater = ThrottledTableUpdater(self, self._flush_queue_table_updates, interval_ms=200)
        self.stats_updater = ThrottledStatsUpdater(self, self._refresh_dashboard_stats, interval_ms=500)
        self.queue_snapshot_timer = QTimer(self)
        self.queue_snapshot_timer.setInterval(1000)
        self.queue_snapshot_timer.timeout.connect(self._request_queue_snapshot)
        self.queue_snapshot_timer.start()
        self.failed_jobs_refresh_timer = QTimer(self)
        self.failed_jobs_refresh_timer.setSingleShot(True)
        self.failed_jobs_refresh_timer.setInterval(400)
        self.failed_jobs_refresh_timer.timeout.connect(lambda: self._request_failed_jobs_refresh(force=False))
        self._apply_modern_theme()
        # Install per-widget wheel filter on every spin/combo box in
        # the app. Must happen after setup_* methods have created the
        # widgets. The filter consumes wheel events and forwards them
        # to the parent scroll area.
        self._install_wheel_filters()
        self._update_runtime_badges()
        # Final sync now that both the video tabs AND the settings
        # sidebar's cmb_generation_mode exist — the earlier call at
        # setup_dashboard time ran before setup_settings so Grok mode
        # wouldn't apply to the video tabs on startup.
        self._sync_generation_mode_ui()
        self._pending_settings_sync_ready = True
        self.account_runtime_timer.start()
        self.account_status_timer.start()
        self.live_refresh_timer = QTimer(self)
        self.live_refresh_timer.setInterval(2000)
        self.live_refresh_timer.timeout.connect(self._schedule_live_refresh)
        self.live_refresh_timer.start()
        self.progress_timer = QTimer(self)
        self.progress_timer.setInterval(2000)
        self.progress_timer.timeout.connect(self._update_progress_display)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self._sync_sidebar_selection()
        
    def setup_dashboard(self):
        root_layout = QVBoxLayout(self.tab_dashboard)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.dashboard_content = QWidget()
        self.dashboard_content.setObjectName("dashboardContent")

        layout = QVBoxLayout(self.dashboard_content)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(8)

        hero = QFrame()
        hero.setObjectName("dashboardTopBar")
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(12, 6, 12, 6)
        hero_layout.setSpacing(10)

        # Hidden hero text (kept for compatibility but not shown in new layout)
        hero_text_layout = QVBoxLayout()
        hero_text_layout.setSpacing(0)
        self.lbl_hero_title = QLabel("")
        self.lbl_hero_title.setObjectName("heroTitle")
        self.lbl_hero_title.setVisible(False)
        self.lbl_hero_subtitle = QLabel("")
        self.lbl_hero_subtitle.setObjectName("heroSubtitle")
        self.lbl_hero_subtitle.setVisible(False)
        hero_text_layout.addWidget(self.lbl_hero_title)
        hero_text_layout.addWidget(self.lbl_hero_subtitle)

        hero_meta_wrap = QWidget()
        hero_meta_layout = QHBoxLayout(hero_meta_wrap)
        hero_meta_layout.setContentsMargins(0, 0, 0, 0)
        hero_meta_layout.setSpacing(6)
        self.lbl_runtime_mode = QLabel("Mode: Hybrid")
        self.lbl_runtime_mode.setObjectName("metaBadge")
        self.lbl_runtime_parallel = QLabel("Parallel: 1/account")
        self.lbl_runtime_parallel.setObjectName("metaBadge")
        self.lbl_queue_status = QLabel("Queue: STOPPED")
        self.lbl_queue_status.setObjectName("metaBadge")
        self.lbl_session_stats = QLabel("Session: 0 images generated")
        self.lbl_session_stats.setStyleSheet(
            "background-color: #1E293B; color: #94A3B8; padding: 4px 12px; "
            "border-radius: 4px; font-size: 12px;"
        )
        hero_meta_layout.addWidget(self.lbl_runtime_mode)
        hero_meta_layout.addWidget(self.lbl_runtime_parallel)
        hero_meta_layout.addWidget(self.lbl_queue_status)
        hero_meta_layout.addWidget(self.lbl_session_stats)

        self.toolbar_actions_host = QWidget()
        self.toolbar_actions_layout = QHBoxLayout(self.toolbar_actions_host)
        self.toolbar_actions_layout.setContentsMargins(0, 0, 0, 0)
        self.toolbar_actions_layout.setSpacing(8)

        hero_layout.addLayout(hero_text_layout, stretch=0)
        hero_layout.addWidget(hero_meta_wrap, 0)
        hero_layout.addStretch(1)
        hero_layout.addWidget(self.toolbar_actions_host, 0)
        layout.addWidget(hero)
        self._apply_card_shadow(hero, blur=28, y_offset=8)

        self.warning_container = QWidget()
        self.warning_container.setVisible(False)
        self.warning_container.setStyleSheet(
            "QWidget { background: #1A1520; border: 1px solid #F59E0B; border-radius: 8px; }"
        )
        warning_layout = QHBoxLayout(self.warning_container)
        warning_layout.setContentsMargins(12, 8, 12, 8)
        warning_layout.setSpacing(10)
        self.warning_banner = QLabel("")
        self.warning_banner.setWordWrap(True)
        self.warning_banner.setStyleSheet(
            "background: transparent; color: #F59E0B; border: none; "
            "padding: 0px; font-weight: 600; font-size: 13px;"
        )
        btn_dismiss_warning = QPushButton("✕")
        btn_dismiss_warning.setFixedSize(24, 24)
        btn_dismiss_warning.setCursor(Qt.PointingHandCursor)
        btn_dismiss_warning.setStyleSheet(
            "QPushButton { background: transparent; color: #F59E0B; border: none; font-size: 16px; font-weight: 700; }"
            "QPushButton:hover { color: #FCD34D; }"
        )
        btn_dismiss_warning.clicked.connect(lambda: self.warning_container.setVisible(False))
        warning_layout.addWidget(self.warning_banner, 1)
        warning_layout.addWidget(btn_dismiss_warning, 0, Qt.AlignTop)
        layout.addWidget(self.warning_container)

        stats_frame = QFrame()
        stats_frame.setObjectName("statsRow")
        stats_layout = QHBoxLayout(stats_frame)
        stats_layout.setContentsMargins(0, 0, 0, 0)
        stats_layout.setSpacing(12)

        def make_stat_card(title_text, accent, clickable=False):
            card = QFrame()
            card.setObjectName("statCard")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(14, 12, 14, 12)
            card_layout.setSpacing(4)
            if clickable:
                value = QPushButton("0")
                value.setCursor(Qt.PointingHandCursor)
                value.setToolTip("Click to view failed jobs")
                value.setStyleSheet(self._failed_stat_button_style(False))
                value.clicked.connect(self._go_to_failed_tab)
            else:
                value = QLabel("0")
                value.setObjectName("statValue")
                value.setStyleSheet(
                    f"color: {accent}; font-size: 28px; font-weight: 800; background: transparent; border: none;"
                )
            title = QLabel(title_text)
            title.setObjectName("statTitle")
            title.setStyleSheet(
                "color: #64748B; font-size: 11px; font-weight: 600; letter-spacing: 1px; "
                "text-transform: uppercase; background: transparent; border: none;"
            )
            if clickable:
                title.setStyleSheet(
                    "color: #EF4444; font-size: 12px; font-weight: 600; letter-spacing: 1px; "
                    "text-transform: uppercase; background: transparent; border: none;"
                )
            card_layout.addWidget(value)
            card_layout.addWidget(title)
            self._apply_card_shadow(card, blur=24, y_offset=8)
            return card, value

        pending_card, self.stat_pending = make_stat_card("Pending", "#94A3B8")
        running_card, self.stat_running = make_stat_card("Running", "#3B82F6")
        completed_card, self.stat_completed = make_stat_card("Done", "#22C55E")
        failed_card, self.btn_failed_count = make_stat_card("Failed", "#EF4444", clickable=True)
        self.stat_failed = self.btn_failed_count

        stats_layout.addWidget(pending_card, 1)
        stats_layout.addWidget(running_card, 1)
        stats_layout.addWidget(completed_card, 1)
        stats_layout.addWidget(failed_card, 1)
        stats_frame.setVisible(False)
        layout.addWidget(stats_frame)
        self.progress_widget = QWidget()
        progress_layout = QHBoxLayout(self.progress_widget)
        progress_layout.setContentsMargins(0, 0, 0, 2)
        progress_layout.setSpacing(8)
        self.overall_progress = QProgressBar()
        self.overall_progress.setRange(0, 100)
        self.overall_progress.setValue(0)
        self.overall_progress.setFixedHeight(6)
        self.overall_progress.setTextVisible(False)
        self.overall_progress.setStyleSheet(
            "QProgressBar { border: none; border-radius: 3px; background-color: #1E293B; } "
            "QProgressBar::chunk { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, "
            "stop:0 #2563EB, stop:1 #60A5FA); border-radius: 3px; }"
        )
        self.lbl_progress_text = QLabel("0/0 (0%)")
        self.lbl_progress_text.setStyleSheet("color: #94A3B8; font-size: 10px; min-width: 80px;")
        self.lbl_speed = QLabel("Speed: --")
        self.lbl_speed.setStyleSheet("color: #60A5FA; font-size: 10px; min-width: 90px;")
        self.lbl_eta = QLabel("ETA: --")
        self.lbl_eta.setStyleSheet("color: #F59E0B; font-size: 10px; min-width: 80px;")
        progress_layout.addWidget(self.overall_progress, 1)
        progress_layout.addWidget(self.lbl_progress_text)
        progress_layout.addWidget(self.lbl_speed)
        progress_layout.addWidget(self.lbl_eta)
        layout.addWidget(self.progress_widget)
        self.current_ref_path = None
        self.current_ref_paths = []
        self.current_start_image_path = None
        self.current_end_image_path = None
        self.current_pipe_ref_paths = []
        self.bulk_panels = {}

        saved_slots = max(1, min(40, get_int_setting("slots_per_account", 5)))
        self._mode_tab_scrolls = {}

        self.mode_tabs = QTabWidget()
        self.mode_tabs.setObjectName("modeTabs")
        self.mode_tabs.setDocumentMode(True)
        self.mode_tabs.tabBar().setDrawBase(False)
        self.mode_tabs.setMinimumHeight(120)
        self.mode_tabs.setMaximumHeight(16777215)
        self.mode_tabs.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.mode_tabs.currentChanged.connect(self._on_mode_tab_changed)

        self.mode_tab_image = QWidget()
        self.mode_tab_t2v = QWidget()
        self.mode_tab_ref = QWidget()
        self.mode_tab_frames = QWidget()
        self.mode_tab_pipeline = QWidget()
        self.mode_tabs.addTab(self.mode_tab_image, "Image")
        self.mode_tabs.addTab(self.mode_tab_t2v, "Video")
        self.mode_tabs.addTab(self.mode_tab_ref, "Video + Ref")
        self.mode_tabs.addTab(self.mode_tab_frames, "Video + Frames")
        self.mode_tabs.addTab(self.mode_tab_pipeline, "Image -> Video")

        self._setup_image_mode_tab(saved_slots)
        self._setup_video_t2v_tab(saved_slots)
        self._setup_video_ref_tab(saved_slots)
        self._setup_video_frames_tab(saved_slots)
        self._setup_pipeline_tab(saved_slots)
        self._remove_stray_mode_tab_buttons()
        self._adjust_mode_tabs_height()

        self.prompts_group = QGroupBox("")
        self.prompts_group.setObjectName("dashboardPanel")
        prompts_layout = QVBoxLayout(self.prompts_group)
        prompts_layout.setContentsMargins(10, 8, 10, 8)
        prompts_layout.setSpacing(6)
        prompts_header = QHBoxLayout()
        prompts_header.setContentsMargins(0, 0, 0, 0)
        prompts_header.setSpacing(8)
        self.lbl_prompts_title = QLabel("PROMPTS")
        self.lbl_prompts_title.setStyleSheet(
            "color: #60A5FA; font-size: 11px; font-weight: 700; letter-spacing: 1px;"
        )
        self.btn_import_txt = QPushButton("Import TXT")
        self.btn_import_txt.setFixedHeight(26)
        self.btn_import_txt.setStyleSheet(
            "QPushButton { background-color: #334155; color: #94A3B8; font-size: 11px; "
            "border: 1px solid #475569; border-radius: 4px; padding: 0 10px; } "
            "QPushButton:hover { background-color: #475569; color: white; }"
        )
        self.btn_import_txt.clicked.connect(self._import_prompts_txt)
        prompts_header.addWidget(self.lbl_prompts_title)
        prompts_header.addStretch(1)
        prompts_header.addWidget(self.btn_import_txt)
        prompts_layout.addLayout(prompts_header)
        self.prompts_input = QTextEdit()
        # Disable rich-text acceptance so pasted content from websites
        # or docs doesn't carry black text color / fonts / styling.
        # Qt strips formatting and inserts plain text only.
        self.prompts_input.setAcceptRichText(False)
        self.prompts_input.setPlaceholderText(
            "Paste your prompts here, one per line...\n\n"
            "Example:\n"
            "A serene mountain landscape at sunset with golden light\n"
            "A futuristic city skyline with neon lights and flying cars\n"
            "A cozy coffee shop interior with warm lighting\n\n"
            "Tips:\n"
            "• One prompt per line\n"
            "• No limit on number of prompts\n"
            "• Detailed prompts = better results"
        )
        self.prompts_input.setMinimumHeight(80)
        self.prompts_input.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.prompts_input.verticalScrollBar().setSingleStep(6)
        prompts_layout.addWidget(self.prompts_input, 1)
        self.prompts_input.setPlaceholderText(
            "Paste your prompts here, one per line...\n\n"
            "Example:\n"
            "A serene mountain landscape at sunset with golden light\n"
            "A futuristic city skyline with neon lights and flying cars\n"
            "A cozy coffee shop interior with warm lighting\n\n"
            "Tips:\n"
            "- One prompt per line\n"
            "- No limit on number of prompts\n"
            "- Detailed prompts = better results"
        )
        # Phase 3: Add to Queue → Fluent PrimaryPushButton with + icon
        _AddBtnCls = PrimaryPushButton if _FLUENT_UI_AVAILABLE else QPushButton
        self.btn_add_to_queue = _AddBtnCls()
        self.btn_add_to_queue.setText("Add to Queue")
        if _FLUENT_UI_AVAILABLE:
            try:
                self.btn_add_to_queue.setIcon(FluentIcon.ADD)
            except Exception:
                pass
        else:
            self.btn_add_to_queue.setProperty("role", "primaryGradient")
            self.btn_add_to_queue.setText("+  Add to Queue")
        self.btn_add_to_queue.setFixedHeight(34)
        self.btn_add_to_queue.clicked.connect(self.add_prompts_to_queue)
        prompts_layout.addWidget(self.btn_add_to_queue)
        self._apply_card_shadow(self.prompts_group, blur=24, y_offset=8)
        self.prompts_input_widget = self.prompts_group

        queue_group = QGroupBox("")
        queue_group.setObjectName("dashboardPanel")
        queue_layout = QVBoxLayout(queue_group)
        queue_layout.setContentsMargins(10, 8, 10, 8)
        queue_layout.setSpacing(6)
        queue_header_wrap = QHBoxLayout()
        queue_header_wrap.setContentsMargins(0, 0, 0, 0)
        queue_header_wrap.setSpacing(6)
        self.lbl_queue_title = QLabel("TASK QUEUE")
        self.lbl_queue_title.setStyleSheet(
            "color: #60A5FA; font-size: 11px; font-weight: 700; letter-spacing: 1px;"
        )
        queue_header_wrap.addWidget(self.lbl_queue_title)
        queue_header_wrap.addStretch(1)
        queue_layout.addLayout(queue_header_wrap)
        self.queue_model = QueueTableModel(self)
        self.queue_table = QTableView()
        assert isinstance(self.queue_table, QTableView), "MUST use QTableView for large queues"
        self.queue_table.setModel(self.queue_model)
        self.queue_table.verticalHeader().setVisible(False)
        self.queue_table.setAlternatingRowColors(False)
        self.queue_table.verticalHeader().setDefaultSectionSize(32)
        self.queue_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.queue_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.queue_table.setShowGrid(True)
        queue_header = self.queue_table.horizontalHeader()
        queue_header.setMinimumSectionSize(30)
        queue_header.setStretchLastSection(False)
        queue_header.setSectionResizeMode(0, QHeaderView.Fixed)    # #
        queue_header.setSectionResizeMode(1, QHeaderView.Stretch)  # Prompt
        queue_header.setSectionResizeMode(2, QHeaderView.Fixed)    # Type
        queue_header.setSectionResizeMode(3, QHeaderView.Fixed)    # Status
        self.queue_table.setColumnWidth(0, 30)
        self.queue_table.setColumnWidth(2, 60)
        self.queue_table.setColumnWidth(3, 80)
        self.queue_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.queue_table.customContextMenuRequested.connect(self.show_queue_context_menu)
        self._configure_table_scrolling(self.queue_table)
        self.queue_table.setStyleSheet(
            """
            QTableView {
                background: #1E293B;
                color: #F8FAFC;
                border: 1px solid #334155;
                border-radius: 8px;
                gridline-color: #334155;
                selection-background-color: #2a3a5c;
                selection-color: #F8FAFC;
            }
            QTableView::item {
                padding: 4px 6px;
            }
            QTableView::item:selected {
                background: #2a3a5c;
                color: #FFFFFF;
            }
            QHeaderView::section {
                background: #0F172A;
                color: #94A3B8;
                font-weight: 600;
                padding: 6px;
                border: none;
                border-bottom: 1px solid #334155;
            }
            """
        )
        queue_layout.addWidget(self.queue_table)
        self._apply_card_shadow(queue_group, blur=24, y_offset=8)
        self.task_queue_widget = queue_group

        # ── 2-COLUMN LAYOUT (mockup redesign) ─────────────────
        # Left: mode_tabs + prompts + Add to Queue
        # Right: task queue + logs (fixed width)
        self._left_panel = QWidget()
        self._left_panel.setObjectName("leftPanel")
        left_vlayout = QVBoxLayout(self._left_panel)
        left_vlayout.setContentsMargins(0, 0, 0, 0)
        left_vlayout.setSpacing(8)
        left_vlayout.addWidget(self.mode_tabs, 1)
        left_vlayout.addWidget(self.prompts_group, 1)

        self._right_panel = QWidget()
        self._right_panel.setObjectName("rightPanel")
        self._right_panel.setMinimumWidth(340)
        self._right_panel.setMaximumWidth(460)
        right_vlayout = QVBoxLayout(self._right_panel)
        right_vlayout.setContentsMargins(0, 0, 0, 0)
        right_vlayout.setSpacing(8)

        # ── Logs widget (placed in right panel) ───────────────
        self.logs_widget = QGroupBox("")
        self.logs_widget.setObjectName("dashboardPanel")
        logs_layout = QVBoxLayout(self.logs_widget)
        logs_layout.setContentsMargins(10, 8, 10, 8)
        logs_header = QHBoxLayout()
        self.lbl_logs_title = QLabel("Live Logs")
        self.lbl_logs_title.setStyleSheet("color: #94A3B8; font-size: 12px; font-weight: 700;")
        logs_header.addWidget(self.lbl_logs_title)
        logs_header.addStretch()
        _ClearLogCls = PushButton if _FLUENT_UI_AVAILABLE else QPushButton
        self.btn_clear_logs = _ClearLogCls()
        self.btn_clear_logs.setText("Clear")
        if _FLUENT_UI_AVAILABLE:
            try:
                self.btn_clear_logs.setIcon(FluentIcon.BROOM)
            except Exception:
                pass
        self.btn_clear_logs.setFixedHeight(24)
        self.btn_clear_logs.setCursor(Qt.PointingHandCursor)
        if not _FLUENT_UI_AVAILABLE:
            self.btn_clear_logs.setStyleSheet(
                "QPushButton { background: rgba(51,65,85,0.4); color: #64748B; font-size: 10px; "
                "border: 1px solid rgba(51,65,85,0.6); border-radius: 11px; padding: 0 12px; "
                "font-weight: 500; letter-spacing: 0.5px; } "
                "QPushButton:hover { background: rgba(96,165,250,0.15); color: #60A5FA; "
                "border-color: rgba(96,165,250,0.4); }"
            )
        self.btn_clear_logs.clicked.connect(self._clear_logs)
        logs_header.addWidget(self.btn_clear_logs)
        logs_layout.addLayout(logs_header)
        self.logs_output = QTextEdit()
        self.logs_output.setObjectName("logsOutput")
        self.logs_output.setReadOnly(True)
        self.logs_output.setUndoRedoEnabled(False)
        self.logs_output.setMinimumHeight(120)
        self.logs_output.document().setMaximumBlockCount(5000)
        self.logs_output.verticalScrollBar().setSingleStep(6)
        self.logs_output.setStyleSheet(
            "QTextEdit { background: #0B1120; border: 1px solid #1E293B; border-radius: 8px; "
            "font-family: 'Consolas', 'Courier New', monospace; font-size: 11px; color: #64748B; }"
        )
        logs_layout.addWidget(self.logs_output)
        self.log_buffer = LogBuffer(self.logs_output, self, interval_ms=180)

        # ── Toolbar action buttons ────────────────────────────────
        _StartBtnCls = PrimaryPushButton if _FLUENT_UI_AVAILABLE else QPushButton
        self.btn_start = _StartBtnCls()
        self.btn_start.setText("Start Automation")
        if _FLUENT_UI_AVAILABLE:
            try:
                self.btn_start.setIcon(FluentIcon.PLAY)
            except Exception:
                pass
        self.btn_start.setFixedHeight(34)
        self.btn_start.setMinimumWidth(140)
        if not _FLUENT_UI_AVAILABLE:
            self.btn_start.setStyleSheet(
                "QPushButton { background-color: #2563EB; color: white; font-size: 13px; font-weight: 700; "
                "border: none; border-radius: 7px; padding: 0 18px; } "
                "QPushButton:hover { background-color: #3B82F6; } "
                "QPushButton:disabled { background-color: #1E3A5F; color: #64748B; }"
            )
        self.btn_start.clicked.connect(self.start_queue_manager)

        _PauseBtnCls = PushButton if _FLUENT_UI_AVAILABLE else QPushButton
        self.btn_pause = _PauseBtnCls()
        self.btn_pause.setText("Pause")
        if _FLUENT_UI_AVAILABLE:
            try:
                self.btn_pause.setIcon(FluentIcon.PAUSE)
            except Exception:
                pass
        self.btn_pause.setFixedHeight(30)
        self.btn_pause.setFixedWidth(84)
        if not _FLUENT_UI_AVAILABLE:
            self.btn_pause.setStyleSheet(
                "QPushButton { background-color: #1E293B; color: #94A3B8; font-size: 12px; font-weight: 600; "
                "border: 1px solid #334155; border-radius: 6px; } "
                "QPushButton:hover { background-color: #334155; color: white; } "
                "QPushButton:disabled { background-color: #1E293B; color: #334155; }"
            )
        self.btn_pause.clicked.connect(self.pause_queue_manager)

        self.btn_resume = _StartBtnCls()
        self.btn_resume.setText("Resume")
        if _FLUENT_UI_AVAILABLE:
            try:
                self.btn_resume.setIcon(FluentIcon.PLAY)
            except Exception:
                pass
        self.btn_resume.setFixedHeight(30)
        self.btn_resume.setFixedWidth(94)
        if not _FLUENT_UI_AVAILABLE:
            self.btn_resume.setStyleSheet(
                "QPushButton { background-color: #1D4ED8; color: white; font-size: 12px; font-weight: 600; "
                "border: none; border-radius: 6px; } "
                "QPushButton:hover { background-color: #3B82F6; } "
                "QPushButton:disabled { background-color: #1E3A5F; color: #64748B; }"
            )
        self.btn_resume.clicked.connect(self.resume_queue_manager)

        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setFixedHeight(30)
        self.btn_stop.setFixedWidth(74)
        self.btn_stop.setStyleSheet(
            "QPushButton { background-color: #DC2626; color: white; font-size: 12px; font-weight: 600; "
            "border: none; border-radius: 6px; } "
            "QPushButton:hover { background-color: #EF4444; } "
            "QPushButton:disabled { background-color: #4A1A1A; color: #64748B; }"
        )
        self.btn_stop.clicked.connect(self.stop_queue_manager)

        _ClearBtnCls = PushButton if _FLUENT_UI_AVAILABLE else QPushButton
        self.btn_clear_queue = _ClearBtnCls()
        self.btn_clear_queue.setText("Clear Queue")
        if _FLUENT_UI_AVAILABLE:
            try:
                self.btn_clear_queue.setIcon(FluentIcon.DELETE)
            except Exception:
                pass
        self.btn_clear_queue.setFixedHeight(26)
        if not _FLUENT_UI_AVAILABLE:
            self.btn_clear_queue.setStyleSheet(
                "QPushButton { background-color: transparent; color: #94A3B8; font-size: 10px; "
                "border: 1px solid #334155; border-radius: 4px; padding: 0 8px; } "
                "QPushButton:hover { background-color: #334155; color: white; }"
            )
        self.btn_clear_queue.clicked.connect(self.clear_queue)

        self.btn_clear_done = _ClearBtnCls()
        self.btn_clear_done.setText("Clear Done")
        if _FLUENT_UI_AVAILABLE:
            try:
                self.btn_clear_done.setIcon(FluentIcon.ACCEPT)
            except Exception:
                pass
        self.btn_clear_done.setFixedHeight(26)
        if not _FLUENT_UI_AVAILABLE:
            self.btn_clear_done.setStyleSheet(
                "QPushButton { background-color: transparent; color: #94A3B8; font-size: 10px; "
                "border: 1px solid #334155; border-radius: 4px; padding: 0 8px; } "
                "QPushButton:hover { background-color: #334155; color: white; }"
            )
        self.btn_clear_done.clicked.connect(self.clear_completed_jobs_from_queue)

        # Place buttons in toolbar (top bar)
        self.toolbar_actions_layout.addWidget(self.btn_start)
        self.toolbar_actions_layout.addWidget(self.btn_pause)
        self.toolbar_actions_layout.addWidget(self.btn_resume)
        self.toolbar_actions_layout.addWidget(self.btn_stop)
        # Place clear buttons in queue header
        queue_header_wrap.addWidget(self.btn_clear_queue)
        queue_header_wrap.addWidget(self.btn_clear_done)

        # ── Assemble right panel: queue + logs ────────────────────
        right_vlayout.addWidget(queue_group, 1)
        right_vlayout.addWidget(self.logs_widget, 0)
        self.logs_widget.setMinimumHeight(180)
        self.logs_widget.setMaximumHeight(240)
        self.task_queue_widget = queue_group

        # ── Assemble 2-column content splitter ────────────────────
        self.content_splitter = QSplitter(Qt.Horizontal)
        self.content_splitter.setChildrenCollapsible(False)
        self.content_splitter.setHandleWidth(4)
        self.content_splitter.addWidget(self._left_panel)
        self.content_splitter.addWidget(self._right_panel)
        self.content_splitter.setStretchFactor(0, 1)
        self.content_splitter.setStretchFactor(1, 0)
        self.content_splitter.setSizes([700, 400])
        self.content_splitter.setStyleSheet(
            "QSplitter::handle:horizontal { background: #1E293B; width: 2px; } "
            "QSplitter::handle:horizontal:hover { background: #60A5FA; }"
        )
        layout.addWidget(self.content_splitter, 1)

        root_layout.addWidget(self.dashboard_content, 1)

        self._set_queue_controls_state("stopped")
        self._sync_generation_mode_ui()
        self._refresh_bulk_pairing_preview("ingredients")
        self._refresh_bulk_pairing_preview("frames_start")
        self.load_queue_table()
        QTimer.singleShot(0, self._scroll_active_mode_tab_to_top)

    def setup_live_generation(self):
        layout = QVBoxLayout(self.tab_live_generation)
        layout.setContentsMargins(20, 16, 20, 12)
        layout.setSpacing(14)

        # ── Header card: stats + progress bar in one panel ──
        header_card = QFrame()
        header_card.setObjectName("liveHeaderCard")
        header_card.setStyleSheet(
            "#liveHeaderCard {"
            "  background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #1E293B, stop:1 #172033);"
            "  border: 1px solid #334155; border-radius: 14px;"
            "}"
        )
        header_vbox = QVBoxLayout(header_card)
        header_vbox.setContentsMargins(18, 16, 18, 16)
        header_vbox.setSpacing(14)

        # Title row
        title_row = QHBoxLayout()
        live_icon = QLabel("⚡")
        live_icon.setStyleSheet("font-size: 18px; background: transparent; border: none;")
        live_title = QLabel("Live Generation")
        live_title.setStyleSheet(
            "color: #F8FAFC; font-size: 17px; font-weight: 800; letter-spacing: 0.5px;"
            " background: transparent; border: none;"
        )
        title_row.addWidget(live_icon)
        title_row.addWidget(live_title)
        title_row.addStretch()
        header_vbox.addLayout(title_row)

        # Stats row
        stats_bar = QHBoxLayout()
        stats_bar.setSpacing(10)
        self.live_stat_total = self._create_live_stat("0", "Total", "#94A3B8")
        self.live_stat_running = self._create_live_stat("0", "Running", "#3B82F6")
        self.live_stat_done = self._create_live_stat("0", "Done", "#22C55E")
        self.live_stat_failed = self._create_live_stat("0", "Failed", "#EF4444")
        self.live_stat_pending = self._create_live_stat("0", "Pending", "#F59E0B")
        for card in (
            self.live_stat_total,
            self.live_stat_running,
            self.live_stat_done,
            self.live_stat_failed,
            self.live_stat_pending,
        ):
            stats_bar.addWidget(card, 1)
        header_vbox.addLayout(stats_bar)

        # Progress bar
        progress_label = QLabel("Overall Progress")
        progress_label.setStyleSheet(
            "color: #94A3B8; font-size: 11px; font-weight: 700; letter-spacing: 1px;"
            " text-transform: uppercase; background: transparent; border: none;"
        )
        header_vbox.addWidget(progress_label)
        self.live_progress_bar = QProgressBar()
        self.live_progress_bar.setRange(0, 100)
        self.live_progress_bar.setValue(0)
        self.live_progress_bar.setTextVisible(True)
        self.live_progress_bar.setFormat("No jobs")
        self.live_progress_bar.setFixedHeight(30)
        self.live_progress_bar.setObjectName("liveOverallProgress")
        header_vbox.addWidget(self.live_progress_bar)

        layout.addWidget(header_card)
        self._apply_card_shadow(header_card, blur=28, y_offset=8)

        # ── Grid Section ──
        grid_wrap = QFrame()
        grid_wrap.setObjectName("liveGridPanel")
        grid_wrap.setStyleSheet(
            "#liveGridPanel {"
            "  background: #111827; border: 1px solid #1E293B;"
            "  border-top: 1px solid #334155; border-radius: 14px;"
            "}"
        )
        grid_layout = QVBoxLayout(grid_wrap)
        grid_layout.setContentsMargins(16, 14, 16, 14)
        grid_layout.setSpacing(10)

        # Grid header row
        grid_header = QHBoxLayout()
        grid_title_icon = QLabel("🎨")
        grid_title_icon.setStyleSheet("font-size: 15px; background: transparent; border: none;")
        grid_title = QLabel("Active Grid")
        grid_title.setStyleSheet(
            "color: #F8FAFC; font-size: 14px; font-weight: 700; background: transparent; border: none;"
        )
        grid_header.addWidget(grid_title_icon)
        grid_header.addWidget(grid_title)
        grid_header.addStretch()

        # Grid size selector inline in header
        sz_label = QLabel("Grid Size")
        sz_label.setStyleSheet(
            "color: #64748B; font-size: 11px; font-weight: 600; background: transparent; border: none;"
        )
        grid_header.addWidget(sz_label)
        self.cmb_grid_size = self._create_setting_combo(
            [("Small", "small"), ("Medium", "medium"), ("Large", "large")],
            current_data="medium",
            trigger_sync=False,
        )
        self.cmb_grid_size.setFixedWidth(100)
        self.cmb_grid_size.currentIndexChanged.connect(self._refresh_live_grid)
        grid_header.addWidget(self.cmb_grid_size)
        grid_layout.addLayout(grid_header)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("background: #1E293B; border: none; max-height: 1px;")
        grid_layout.addWidget(sep)

        self.grid_scroll = QScrollArea()
        self.grid_scroll.setWidgetResizable(True)
        self.grid_scroll.setFrameShape(QFrame.NoFrame)
        self.grid_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.grid_scroll.setObjectName("liveGridScroll")
        self.grid_scroll.verticalScrollBar().setSingleStep(6)
        self.grid_scroll.verticalScrollBar().valueChanged.connect(self._on_grid_scroll)
        self.live_grid_widget = VirtualLiveGridWidget()
        self.live_grid_widget.setObjectName("liveGridCanvas")
        self.grid_scroll.setWidget(self.live_grid_widget)
        grid_layout.addWidget(self.grid_scroll, 1)

        # Bottom bar
        bottom_bar = QHBoxLayout()
        bottom_bar.setSpacing(10)
        bottom_bar.addStretch()

        _BtnCls = PushButton if _FLUENT_UI_AVAILABLE else QPushButton
        self.btn_open_outputs = _BtnCls("📂  Open Outputs Folder")
        self.btn_open_outputs.setObjectName("liveOpenOutputsBtn")
        self.btn_open_outputs.setStyleSheet(
            "#liveOpenOutputsBtn {"
            "  background: #1E293B; color: #E2E8F0; border: 1px solid #334155;"
            "  border-radius: 8px; padding: 7px 18px; font-weight: 600; font-size: 12px;"
            "}"
            "#liveOpenOutputsBtn:hover { background: #263449; border-color: #3B82F6; }"
        )
        self.btn_open_outputs.clicked.connect(self._open_outputs_folder)
        bottom_bar.addWidget(self.btn_open_outputs)
        grid_layout.addLayout(bottom_bar)

        layout.addWidget(grid_wrap, 1)
        self._apply_card_shadow(grid_wrap, blur=24, y_offset=8)

        self._live_grid_size_key = str(self.cmb_grid_size.currentData() or "medium")
        self._refresh_live_grid()

    def _create_live_stat(self, count_text, label_text, color):
        card = QFrame()
        card.setObjectName("liveStatCard")
        card.setStyleSheet(
            f"QFrame#liveStatCard {{"
            f"  background: #0F172A; border: 1px solid #1E293B; border-radius: 12px;"
            f"  border-top: 3px solid {color};"
            f"}}"
            f"QFrame#liveStatCard:hover {{ background: #162033; border-color: #334155; border-top: 3px solid {color}; }}"
        )
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(14, 14, 14, 10)
        card_layout.setSpacing(2)

        count_label = QLabel(str(count_text))
        count_label.setStyleSheet(
            f"color: {color}; font-size: 28px; font-weight: 900; background: transparent; border: none;"
        )
        count_label.setAlignment(Qt.AlignCenter)
        label = QLabel(label_text.upper())
        label.setStyleSheet(
            "color: #64748B; font-size: 10px; font-weight: 700; letter-spacing: 1.5px; background: transparent; border: none;"
        )
        label.setAlignment(Qt.AlignCenter)
        card_layout.addWidget(count_label)
        card_layout.addWidget(label)
        card._count_label = count_label
        return card

    def _default_outputs_dir(self):
        return str(get_outputs_dir())

    def _outputs_dir(self):
        return str(get_output_directory())

    def _browse_output_directory(self):
        current_dir = self._outputs_dir()
        selected_dir = QFileDialog.getExistingDirectory(
            self,
            "Select Outputs Folder",
            current_dir,
        )
        if not selected_dir:
            return
        normalized = os.path.abspath(os.path.expanduser(str(selected_dir)))
        self.output_dir_input.setText(normalized)

    def _reset_output_directory(self):
        self.output_dir_input.setText(self._default_outputs_dir())

    def _open_outputs_folder(self):
        outputs_dir = self._outputs_dir()
        os.makedirs(outputs_dir, exist_ok=True)
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", outputs_dir])
            elif sys.platform.startswith("win"):
                subprocess.Popen(["explorer", outputs_dir])
            else:
                subprocess.Popen(["xdg-open", outputs_dir])
        except Exception as exc:
            QMessageBox.warning(self, "Open Outputs Failed", f"Could not open outputs folder:\n{exc}")

    def _refresh_live_grid(self):
        if not hasattr(self, "live_grid_widget"):
            return
        self._live_grid_size_key = str(self.cmb_grid_size.currentData() or "medium") if hasattr(self, "cmb_grid_size") else "medium"
        self.live_grid_widget.set_card_size(self._live_grid_size_key)
        self._apply_live_jobs(self._latest_queue_jobs)

    def _update_live_stats(self, jobs):
        if not hasattr(self, "live_progress_bar"):
            return

        total = len(jobs)
        running = sum(1 for job in jobs if str(job.get("status") or "").strip().lower() == "running")
        done = sum(1 for job in jobs if str(job.get("status") or "").strip().lower() == "completed")
        failed = sum(1 for job in jobs if str(job.get("status") or "").strip().lower() == "failed")
        pending = sum(1 for job in jobs if str(job.get("status") or "").strip().lower() == "pending")

        self.live_stat_total._count_label.setText(str(total))
        self.live_stat_running._count_label.setText(str(running))
        self.live_stat_done._count_label.setText(str(done))
        self.live_stat_failed._count_label.setText(str(failed))
        self.live_stat_pending._count_label.setText(str(pending))

        if total <= 0:
            self.live_progress_bar.setMaximum(100)
            self.live_progress_bar.setValue(0)
            self.live_progress_bar.setFormat("No jobs")
            return

        settled = done + failed
        percent = int((settled / total) * 100)
        self.live_progress_bar.setMaximum(total)
        self.live_progress_bar.setValue(settled)
        self.live_progress_bar.setFormat(f"{percent}% ({settled}/{total} complete)")

    def _apply_live_jobs(self, jobs):
        jobs = list(jobs or [])
        if not hasattr(self, "live_grid_widget"):
            return
        self._latest_queue_jobs = jobs
        if hasattr(self, "grid_scroll") and self.grid_scroll is not None:
            self.live_grid_widget.resize(max(320, self.grid_scroll.viewport().width()), self.live_grid_widget.height())
        self.live_grid_widget.set_jobs(jobs)
        self._update_live_stats(jobs)

    def _schedule_live_refresh(self):
        if not hasattr(self, "tab_live_generation"):
            return
        if self.tabs.currentWidget() is not self.tab_live_generation:
            return
        if self._grid_scrolling:
            return
        jobs = list(self._pending_live_jobs if self._pending_live_jobs is not None else self._latest_queue_jobs)
        self._pending_live_jobs = None
        self.ui_throttler.schedule("live_grid", lambda jobs=jobs: self._apply_live_jobs(jobs))

    def _on_grid_scroll(self, _value):
        self._grid_scrolling = True
        self._grid_scroll_resume_timer.start(500)

    def _on_grid_scroll_idle(self):
        self._grid_scrolling = False
        if self._pending_live_jobs is not None and self.tabs.currentWidget() is self.tab_live_generation:
            self.ui_throttler.schedule(
                "live_grid",
                lambda jobs=list(self._pending_live_jobs or []): self._apply_live_jobs(jobs),
            )
            self._pending_live_jobs = None

    def _clear_logs(self):
        if hasattr(self, "log_buffer"):
            self.log_buffer.clear()
            return
        if hasattr(self, "logs_output"):
            self.logs_output.clear()

    def _show_session_warning(self, message):
        if not hasattr(self, "warning_banner") or not hasattr(self, "warning_container"):
            return
        text = str(message or "").strip()
        if not text:
            self.warning_banner.clear()
            self.warning_container.setVisible(False)
            return
        self.warning_banner.setText(f"⚠ {text}")
        self.warning_container.setVisible(True)

        # Also show a popup dialog for critical warnings (reCAPTCHA flagged accounts)
        if "reCAPTCHA" in text:
            QMessageBox.warning(self, "⚠ Account Flagged — reCAPTCHA", text)

    def _create_setting_combo(self, items, current_data=None, *, trigger_sync=True, min_height=38):
        combo = QComboBox()
        combo.setObjectName("settingInput")
        combo.setMinimumHeight(min_height)
        for item in items:
            if isinstance(item, tuple):
                combo.addItem(item[0], item[1])
            else:
                combo.addItem(str(item), item)
        if current_data is not None:
            idx = combo.findData(current_data)
            if idx < 0 and isinstance(current_data, str):
                idx = combo.findText(current_data)
            if idx >= 0:
                combo.setCurrentIndex(idx)
        if trigger_sync:
            combo.currentIndexChanged.connect(lambda _=None: self._on_generation_settings_changed())
        return combo

    def _create_parallel_combo(self, saved_slots):
        combo = self._create_setting_combo([(str(i), i) for i in range(1, 41)], current_data=max(1, min(40, saved_slots)), trigger_sync=False)
        combo.currentIndexChanged.connect(lambda _=None: self._update_runtime_badges())
        return combo

    def _create_grok_res_dur_combos(self):
        """Build the (resolution, duration) combo pair for Grok mode.
        Grok Imagine caps at 720p and only accepts 6s or 10s clips —
        exposing 1080p/4K options would just map down silently and
        confuse the user. Saved values are stored under grok_* keys so
        non-Grok runs ignore them entirely. Defaults to 720p / 10s.

        Each combo's change signal triggers `_sync_grok_combos_across_tabs`
        so all 4 video sub-tabs keep the same grok resolution/duration
        choice — users almost never want different per-tab values, and
        a single global choice makes persistence straightforward."""
        saved_res = str(get_setting("grok_resolution", "720p") or "720p").lower()
        if saved_res not in ("480p", "720p"):
            saved_res = "720p"
        try:
            saved_dur = int(get_setting("grok_video_length", 10) or 10)
        except (TypeError, ValueError):
            saved_dur = 10
        if saved_dur not in (6, 10):
            saved_dur = 10
        res = self._create_setting_combo(
            [("480p", "480p"), ("720p", "720p")], current_data=saved_res,
        )
        dur = self._create_setting_combo(
            [("6s", 6), ("10s", 10)], current_data=saved_dur,
        )
        res.currentIndexChanged.connect(
            lambda _=None, src=res: self._sync_grok_combos_across_tabs("res", src)
        )
        dur.currentIndexChanged.connect(
            lambda _=None, src=dur: self._sync_grok_combos_across_tabs("dur", src)
        )
        return res, dur

    def _sync_grok_combos_across_tabs(self, kind: str, source):
        """Propagate a change in one tab's grok res/dur combo to the
        other tabs. Guarded by a reentrancy flag so the propagation
        doesn't re-fire and loop forever."""
        if getattr(self, "_syncing_grok_combos", False):
            return
        attr = "_cmb_grok_res" if kind == "res" else "_cmb_grok_dur"
        try:
            target_data = source.currentData()
        except Exception:
            return
        self._syncing_grok_combos = True
        try:
            for prefix in ("t2v", "ref", "frm", "pipe"):
                other = getattr(self, f"{prefix}{attr}", None)
                if other is None or other is source:
                    continue
                idx = other.findData(target_data)
                if idx >= 0 and other.currentIndex() != idx:
                    # blockSignals so the propagated setCurrentIndex
                    # doesn't re-fire _on_generation_settings_changed
                    # on every target (which would run the pending-job
                    # resync 3 extra times per user interaction).
                    other.blockSignals(True)
                    try:
                        other.setCurrentIndex(idx)
                    finally:
                        other.blockSignals(False)
        finally:
            self._syncing_grok_combos = False

    def _set_grok_row_visible(self, upscale_combo, grok_widgets, upscale_label, is_grok):
        """Toggle an upscale/grok row between Veo-style (upscale combo)
        and Grok-style (resolution + duration combos) layout. Keeps
        Parallel visible either way."""
        upscale_combo.setVisible(not is_grok)
        for w in grok_widgets:
            w.setVisible(is_grok)
        if upscale_label is not None:
            upscale_label.setText("Grok:" if is_grok else "Upscale:")

    def _make_setting_label(self, text):
        label = QLabel(text)
        label.setObjectName("settingLabel")
        label.setMinimumWidth(86)
        return label

    def _make_inline_row(self, *widgets):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        for widget in widgets:
            if widget is None:
                continue
            stretch = 1 if isinstance(widget, (QComboBox, QLineEdit, QTextEdit)) else 0
            layout.addWidget(widget, stretch)
        layout.addStretch()
        return row

    def _apply_card_shadow(self, widget, *, blur=28, y_offset=10, color=QColor(15, 23, 42, 90)):
        effect = QGraphicsDropShadowEffect(widget)
        effect.setBlurRadius(blur)
        effect.setOffset(0, y_offset)
        effect.setColor(color)
        widget.setGraphicsEffect(effect)

    def _configure_table_scrolling(self, table):
        table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        table.verticalScrollBar().setSingleStep(6)
        table.horizontalScrollBar().setSingleStep(6)
        table.setMouseTracking(False)

    def _start_background_task(self, fn, *args, on_finished=None, on_error=None, **kwargs):
        task = BackgroundTask(fn, *args, **kwargs)
        self._background_tasks.add(task)

        def _cleanup(*_args):
            self._background_tasks.discard(task)

        task.signals.finished.connect(_cleanup, Qt.QueuedConnection)
        task.signals.error.connect(_cleanup, Qt.QueuedConnection)
        if callable(on_finished):
            task.signals.finished.connect(on_finished, Qt.QueuedConnection)
        if callable(on_error):
            task.signals.error.connect(on_error, Qt.QueuedConnection)
        else:
            task.signals.error.connect(self._on_background_task_error, Qt.QueuedConnection)
        self.thread_pool.start(task)
        return task

    def _on_background_task_error(self, error_msg):
        message = str(error_msg or "").strip()
        if message:
            self.append_log(f"[ERROR] {message}")

    @staticmethod
    def _slim_jobs_for_ui(jobs):
        result = []
        for job in list(jobs or []):
            row = dict(job or {})
            row.pop("image_bytes", None)
            row.pop("binary", None)
            if str(row.get("status") or "").strip().lower() == "completed":
                row.pop("ref_paths", None)
                prompt = str(row.get("prompt") or "")
                if len(prompt) > 160:
                    row["prompt"] = prompt[:157] + "..."
            result.append(row)
        return result

    def _cached_pending_count(self):
        return sum(1 for job in self._latest_queue_jobs if str(job.get("status") or "").strip().lower() == "pending")

    def _queue_job_status_text(self, job):
        status_text = str(job.get("status") or "").strip().lower() or "pending"
        if status_text == "failed" and self._is_moderated_failed_error(job.get("error")):
            return "moderated"
        return status_text

    def _queue_job_progress_text(self, job):
        status_text = self._queue_job_status_text(job)
        if status_text == "completed":
            return "Done"
        if status_text in ("failed", "moderated"):
            return "Failed"
        if status_text == "running":
            progress_step = str(job.get("progress_step") or "").strip().lower()
            poll_count = max(0, int(job.get("progress_poll_count") or 0))
            if progress_step == "download":
                return "Downloading"
            if progress_step == "image":
                return "Rendering"
            if progress_step == "video":
                return f"Polling {poll_count}"
            return "Running"

        type_text = str(job.get("job_type") or "image").strip().lower()
        progress_value = job.get("video_output_count") if type_text in ("video", "pipeline") else job.get("output_count")
        try:
            return f"x{max(1, int(progress_value or 1))}"
        except Exception:
            return "--"

    def _build_queue_row_snapshot(self, job):
        type_text = str(job.get("job_type") or "image").strip().lower()
        if type_text == "pipeline":
            display_type = "Pipeline"
        elif type_text == "video":
            display_type = "Video"
        else:
            display_type = "Image"

        model_text = str(job.get("model") or "")
        if type_text == "pipeline":
            video_model = str(job.get("video_model") or "").strip()
            if video_model:
                model_text = f"{model_text} -> {video_model}"

        display_no = job.get("output_index") if job.get("is_retry") else job.get("queue_no")
        if display_no is None:
            display_no = str(job.get("id") or "")[:8]
        display_no_text = f"{display_no} (RETRY)" if job.get("is_retry") else str(display_no)

        return {
            "job_id": str(job.get("id") or ""),
            "queue_no": display_no_text,
            "prompt": str(job.get("prompt") or ""),
            "job_type_display": display_type,
            "model_display": model_text,
            "account": str(job.get("account") or ""),
            "status": self._queue_job_status_text(job),
            "progress": self._queue_job_progress_text(job),
            "is_retry": bool(job.get("is_retry")),
            "retry_source": str(job.get("retry_source") or ""),
        }

    def _flush_queue_table_updates(self, job_ids):
        if not hasattr(self, "queue_model") or self.queue_model.rowCount() <= 0:
            return
        pending_updates = {}
        for job_id in list(job_ids or []):
            row = self._queue_row_map.get(str(job_id))
            snapshot = self._queue_row_snapshots.get(str(job_id))
            if row is None or snapshot is None:
                continue
            pending_updates[row] = snapshot
        self.queue_model.bulk_update(pending_updates)

    def _schedule_failed_jobs_refresh(self):
        self._failed_jobs_dirty = True
        if hasattr(self, "failed_jobs_refresh_timer") and self.tabs.currentWidget() is self.tab_failed_jobs:
            self.failed_jobs_refresh_timer.start()

    def _poll_queue_snapshot(self):
        self._request_queue_snapshot()

    def _request_queue_snapshot(self):
        if not hasattr(self, "queue_model"):
            return
        if self._queue_snapshot_inflight:
            self._queue_snapshot_requested = True
            return
        self._queue_snapshot_inflight = True
        self._queue_snapshot_requested = False
        self._start_background_task(
            get_all_jobs,
            on_finished=self._on_queue_snapshot_loaded,
            on_error=self._on_queue_snapshot_failed,
        )

    def _on_queue_snapshot_failed(self, error_msg):
        self._queue_snapshot_inflight = False
        self._on_background_task_error(error_msg)

    def _on_queue_snapshot_loaded(self, jobs):
        self._queue_snapshot_inflight = False
        slim_jobs = self._slim_jobs_for_ui(jobs)
        if self._queue_snapshot_requested:
            self._queue_snapshot_requested = False
            self._request_queue_snapshot()
        self.ui_throttler.schedule("queue_snapshot", lambda jobs=slim_jobs: self._apply_queue_snapshot(jobs))

    def _apply_queue_snapshot(self, jobs):
        jobs = list(jobs or [])
        self._latest_queue_jobs = jobs
        job_order = [str(job.get("id") or "") for job in jobs]
        snapshots = {job_id: self._build_queue_row_snapshot(job) for job_id, job in zip(job_order, jobs)}

        if self.queue_model.rowCount() != len(jobs) or self._queue_job_order != job_order:
            self._queue_job_order = job_order
            self._queue_row_map = {job_id: idx for idx, job_id in enumerate(self._queue_job_order)}
            queue_rows = [snapshots[job_id] for job_id in job_order]
            self._queue_row_snapshots = dict(snapshots)
            self.queue_model.set_jobs(queue_rows)
        else:
            changed_rows = {}
            for job_id in job_order:
                snapshot = snapshots[job_id]
                if snapshot != self._queue_row_snapshots.get(job_id):
                    row = self._queue_row_map.get(job_id)
                    if row is not None:
                        changed_rows[row] = snapshot
            self._queue_row_snapshots = dict(snapshots)
            if changed_rows:
                self.queue_model.bulk_update(changed_rows)

        self._refresh_dashboard_stats(jobs)
        if self.tabs.currentWidget() is self.tab_live_generation:
            if self._grid_scrolling:
                self._pending_live_jobs = jobs
            else:
                self._apply_live_jobs(jobs)
        else:
            self._live_tab_dirty = True

    def _request_failed_jobs_refresh(self, force=False):
        self._failed_jobs_dirty = True
        if not force and self.tabs.currentWidget() is not self.tab_failed_jobs:
            return
        if self._failed_jobs_inflight:
            return
        self._failed_jobs_inflight = True
        self._start_background_task(
            get_failed_jobs,
            on_finished=self._on_failed_jobs_loaded,
            on_error=self._on_failed_jobs_failed,
        )

    def _on_failed_jobs_failed(self, error_msg):
        self._failed_jobs_inflight = False
        self._on_background_task_error(error_msg)

    def _on_failed_jobs_loaded(self, jobs):
        self._failed_jobs_inflight = False
        self._latest_failed_jobs = list(jobs or [])
        if self.tabs.currentWidget() is self.tab_failed_jobs:
            self.ui_throttler.schedule(
                "failed_jobs",
                lambda jobs=list(self._latest_failed_jobs): self._populate_failed_jobs_table(jobs),
            )

    def _start_bulk_queue_add(self, job_specs, *, success_logs=None, after_success=None, progress_title="Adding prompts to queue..."):
        if self.bulk_queue_add_worker and self.bulk_queue_add_worker.isRunning():
            QMessageBox.information(self, "Queue Add In Progress", "Please wait for the current bulk add to finish.")
            return

        specs = list(job_specs or [])
        if not specs:
            return

        self._bulk_add_success_logs = list(success_logs or [])
        self._bulk_add_after_success = after_success
        self.bulk_queue_add_worker = BulkQueueAddWorker(specs, self)
        self.bulk_queue_add_worker.progress.connect(self._on_bulk_queue_add_progress)
        self.bulk_queue_add_worker.completed.connect(self._on_bulk_queue_add_completed)
        self.bulk_queue_add_worker.failed.connect(self._on_bulk_queue_add_failed)

        if len(specs) >= 50:
            self.bulk_add_progress_dialog = QProgressDialog(progress_title, "", 0, len(specs), self)
            self.bulk_add_progress_dialog.setCancelButton(None)
            self.bulk_add_progress_dialog.setWindowModality(Qt.WindowModal)
            self.bulk_add_progress_dialog.setMinimumDuration(0)
            self.bulk_add_progress_dialog.setAutoClose(False)
            self.bulk_add_progress_dialog.setAutoReset(False)
            self.bulk_add_progress_dialog.setValue(0)
            self.bulk_add_progress_dialog.show()

        if hasattr(self, "btn_add_to_queue"):
            self.btn_add_to_queue.setEnabled(False)
        self.bulk_queue_add_worker.start()

    def _on_bulk_queue_add_progress(self, done, total):
        if self.bulk_add_progress_dialog is not None:
            self.bulk_add_progress_dialog.setMaximum(max(1, int(total or 1)))
            self.bulk_add_progress_dialog.setValue(int(done or 0))

    def _on_bulk_queue_add_completed(self, inserted_count):
        if self.bulk_add_progress_dialog is not None:
            self.bulk_add_progress_dialog.setValue(self.bulk_add_progress_dialog.maximum())
            self.bulk_add_progress_dialog.close()
            self.bulk_add_progress_dialog.deleteLater()
            self.bulk_add_progress_dialog = None

        if hasattr(self, "btn_add_to_queue"):
            self.btn_add_to_queue.setEnabled(True)

        after_success = self._bulk_add_after_success
        self._bulk_add_after_success = None
        if callable(after_success):
            after_success()

        for msg in self._bulk_add_success_logs:
            self.append_log(msg)
        self._bulk_add_success_logs = []

        self.load_queue_table()

        if self.bulk_queue_add_worker is not None:
            self.bulk_queue_add_worker.deleteLater()
            self.bulk_queue_add_worker = None

    def _on_bulk_queue_add_failed(self, error_msg):
        if self.bulk_add_progress_dialog is not None:
            self.bulk_add_progress_dialog.close()
            self.bulk_add_progress_dialog.deleteLater()
            self.bulk_add_progress_dialog = None
        if hasattr(self, "btn_add_to_queue"):
            self.btn_add_to_queue.setEnabled(True)
        QMessageBox.critical(self, "Queue Add Failed", str(error_msg or "Could not add prompts to queue."))
        self._bulk_add_success_logs = []
        self._bulk_add_after_success = None
        if self.bulk_queue_add_worker is not None:
            self.bulk_queue_add_worker.deleteLater()
            self.bulk_queue_add_worker = None

    def _make_status_badge(self, text, status_text):
        status = str(status_text or "").strip().lower()
        styles = {
            "pending": ("#94A3B8", "#1D2535", "#475569"),
            "running": ("#3B82F6", "#1A2744", "#3B82F6"),
            "completed": ("#22C55E", "#132432", "#22C55E"),
            "failed": ("#EF4444", "#1F1A2A", "#EF4444"),
            "moderated": ("#F59E0B", "#1C1E2A", "#F59E0B"),
        }
        fg, bg, border = styles.get(status, ("#94A3B8", "#1D2535", "#475569"))

        wrap = QWidget()
        layout = QHBoxLayout(wrap)
        layout.setContentsMargins(6, 2, 6, 2)
        layout.setSpacing(0)

        badge = QLabel(str(text or "").upper())
        badge.setAlignment(Qt.AlignCenter)
        badge.setStyleSheet(
            f"color: {fg}; background: {bg}; border: 1px solid {border}; "
            "border-radius: 6px; padding: 3px 8px; font-size: 11px; font-weight: 700;"
        )
        layout.addWidget(badge, alignment=Qt.AlignCenter)
        return wrap

    def _create_tab_scroll_content(self, tab_widget, max_h):
        outer_layout = QVBoxLayout(tab_widget)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)
        tab_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        tab_widget.setMinimumHeight(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        scroll.verticalScrollBar().setSingleStep(6)
        scroll.setProperty("preferredMaxHeight", int(max_h))
        scroll.setAlignment(Qt.AlignTop | Qt.AlignLeft)

        viewport = QWidget()
        viewport.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        viewport_layout = QVBoxLayout(viewport)
        viewport_layout.setContentsMargins(0, 0, 0, 0)
        viewport_layout.setSpacing(0)

        content = QWidget()
        content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        layout.setAlignment(Qt.AlignTop)
        viewport_layout.addWidget(content, 0, Qt.AlignTop)
        viewport_layout.addStretch(1)

        scroll.setWidget(viewport)
        outer_layout.addWidget(scroll, 1)
        self._mode_tab_scrolls[tab_widget] = scroll
        return layout, scroll

    def _create_tab_section_title(self, text):
        label = QLabel(text)
        label.setObjectName("tabSectionTitle")
        return label

    def _create_separator(self):
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setObjectName("tabSeparator")
        line.setFixedHeight(1)
        return line

    def _toggle_bulk_section(self, mode_key, checked):
        panel = self._bulk_panel(mode_key)
        if not panel:
            return
        panel["content"].setVisible(bool(checked))
        panel["toggle"].setText("📦 Bulk Image Matching  ▼" if checked else "📦 Bulk Image Matching  ▶")

    def _toggle_pipeline_bulk_section(self, checked):
        is_open = bool(checked)
        if hasattr(self, "pipe_bulk_content"):
            self.pipe_bulk_content.setVisible(is_open)
        if hasattr(self, "pipe_bulk_toggle"):
            self.pipe_bulk_toggle.setText("📦 Bulk Pipeline Prompts  ▼" if is_open else "📦 Bulk Pipeline Prompts  ▶")

    def _update_pipeline_count(self):
        if not hasattr(self, "pipe_lbl_count"):
            return

        img_lines = []
        vid_lines = []
        if hasattr(self, "pipe_txt_img_prompts"):
            img_lines = [line for line in self.pipe_txt_img_prompts.toPlainText().strip().split("\n") if line.strip()]
        if hasattr(self, "pipe_txt_vid_prompts"):
            vid_lines = [line for line in self.pipe_txt_vid_prompts.toPlainText().strip().split("\n") if line.strip()]

        img_count = len(img_lines)
        vid_count = len(vid_lines)

        if img_count == 0:
            self.pipe_lbl_count.setText("")
            self.pipe_lbl_count.setStyleSheet("color: #64748B; font-size: 12px; padding: 4px 0;")
        elif vid_count == 0:
            self.pipe_lbl_count.setText(f"Image Prompts: {img_count}  |  Video Prompts: 0 (all will use 'animate')")
            self.pipe_lbl_count.setStyleSheet("color: #F59E0B; font-size: 12px; padding: 4px 0;")
        elif img_count == vid_count:
            self.pipe_lbl_count.setText(f"Image Prompts: {img_count}  |  Video Prompts: {vid_count}  Matched")
            self.pipe_lbl_count.setStyleSheet("color: #22C55E; font-size: 12px; font-weight: 600; padding: 4px 0;")
        elif vid_count < img_count:
            diff = img_count - vid_count
            self.pipe_lbl_count.setText(
                f"Image Prompts: {img_count}  |  Video Prompts: {vid_count}  {diff} video prompt(s) missing (will use 'animate')"
            )
            self.pipe_lbl_count.setStyleSheet("color: #F59E0B; font-size: 12px; padding: 4px 0;")
        else:
            diff = vid_count - img_count
            self.pipe_lbl_count.setText(
                f"Image Prompts: {img_count}  |  Video Prompts: {vid_count}  {diff} extra video prompt(s) ignored"
            )
            self.pipe_lbl_count.setStyleSheet("color: #F59E0B; font-size: 12px; padding: 4px 0;")

    def _current_flow_plan(self):
        """Read the currently-selected Flow plan (ultra/pro). Safe to call
        before cmb_flow_plan exists — falls back to ultra (default).
        """
        combo = getattr(self, "cmb_flow_plan", None)
        if combo is None:
            return "ultra"
        try:
            value = combo.currentData()
        except Exception:
            value = None
        if not value:
            try:
                value = combo.currentText()
            except Exception:
                value = None
        return str(value or "ultra").strip().lower() or "ultra"

    def _veo_tier_options_for_plan(self, plan):
        """Return the list of (label, value) tier options Flow exposes
        for the given plan. Verified via live captures on labs.google.com.

        Ultra plan exposes all 5 tiers including the [Lower Pri] variants.
        Pro plan exposes ONLY 3 tiers — no [Lower Pri] options exist in
        Flow's official UI. Sending a _relaxed / _low_priority model on a
        Pro account returns PUBLIC_ERROR_MODEL_ACCESS_DENIED.
        """
        plan_lower = str(plan or "ultra").strip().lower()
        if plan_lower == "pro":
            return [
                ("Veo 3.1 - Fast", "Veo 3.1 - Fast"),
                ("Veo 3.1 - Lite", "Veo 3.1 - Lite"),
                ("Veo 3.1 - Quality", "Veo 3.1 - Quality"),
            ]
        # Ultra (default) — all 5 tiers
        return [
            ("Veo 3.1 - Fast", "Veo 3.1 - Fast"),
            ("Veo 3.1 - Lite", "Veo 3.1 - Lite"),
            ("Veo 3.1 - Quality", "Veo 3.1 - Quality"),
            ("Veo 3.1 - Fast [Lower Pri]", "Veo 3.1 - Fast [Lower Pri]"),
            ("Veo 3.1 - Lite [Lower Pri]", "Veo 3.1 - Lite [Lower Pri]"),
        ]

    def _repopulate_tier_combo(self, combo, items, default_value="Veo 3.1 - Fast"):
        """Clear+refill a tier QComboBox while preserving the user's
        selection if it's still valid under the new options. Falls back
        to `default_value` when the old selection no longer exists
        (e.g., Ultra user picks Fast [LP] then switches to Pro plan).
        """
        if combo is None:
            return
        current_value = ""
        try:
            current_value = str(combo.currentData() or combo.currentText() or "").strip()
        except Exception:
            current_value = ""
        combo.blockSignals(True)
        try:
            combo.clear()
            for label, value in items:
                combo.addItem(label, value)
            restored_idx = combo.findData(current_value)
            if restored_idx < 0:
                # Try matching by visible label (in case only labels stored)
                restored_idx = combo.findText(current_value)
            if restored_idx < 0:
                restored_idx = combo.findData(default_value)
            if restored_idx < 0:
                restored_idx = 0
            combo.setCurrentIndex(max(0, restored_idx))
        finally:
            combo.blockSignals(False)

    def _apply_plan_tier_filter(self):
        """Rebuild all 4 Veo quality dropdowns based on the current plan.
        Called once at startup and again whenever the user flips the
        Flow Account Plan dropdown.
        """
        plan = self._current_flow_plan()
        items = self._veo_tier_options_for_plan(plan)
        for attr in ("t2v_cmb_quality", "ref_cmb_quality", "frm_cmb_quality"):
            combo = getattr(self, attr, None)
            if combo is not None:
                self._repopulate_tier_combo(combo, items)
        # Pipeline dropdown has its own path (mode-aware) — re-run it so
        # it picks up the new plan as well.
        if hasattr(self, "pipe_cmb_vid_quality"):
            mode = "ingredients"
            if hasattr(self, "pipe_cmb_vid_mode"):
                try:
                    mode = str(self.pipe_cmb_vid_mode.currentData() or "ingredients")
                except Exception:
                    mode = "ingredients"
            self._update_pipeline_video_quality_options(mode)

    def _update_pipeline_video_quality_options(self, mode):
        if not hasattr(self, "pipe_cmb_vid_quality"):
            return
        normalized_mode = str(mode or "ingredients").strip().lower() or "ingredients"
        items = self._veo_tier_options_for_plan(self._current_flow_plan())
        self._repopulate_tier_combo(self.pipe_cmb_vid_quality, items)

    def _on_pipeline_video_mode_changed(self, _index):
        mode = str(self.pipe_cmb_vid_mode.currentData() or "ingredients")
        self._update_pipeline_video_quality_options(mode)
        self._on_generation_settings_changed()

    def _create_path_row(self, button_text, browse_handler, clear_handler, clear_label="Clear"):
        row = QFrame()
        row.setObjectName("referenceRow")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(8, 8, 8, 8)
        row_layout.setSpacing(10)
        browse_btn = QPushButton(button_text)
        browse_btn.setProperty("role", "browse")
        browse_btn.setMinimumHeight(38)
        browse_btn.clicked.connect(browse_handler)
        label = QLabel("None")
        label.setObjectName("refStatusLabel")
        clear_btn = QPushButton(clear_label)
        clear_btn.setObjectName("refClearButton")
        clear_btn.setProperty("role", "danger")
        clear_btn.setMinimumHeight(34)
        clear_btn.clicked.connect(clear_handler)
        clear_btn.setVisible(False)
        row_layout.addWidget(browse_btn)
        row_layout.addWidget(label, 1)
        row_layout.addWidget(clear_btn)
        return row, browse_btn, label, clear_btn

    def _setup_image_mode_tab(self, saved_slots):
        layout, self.img_tab_scroll = self._create_tab_scroll_content(self.mode_tab_image, 230)
        layout.addWidget(self._create_tab_section_title("Image Settings"))

        form = QFormLayout()
        form.setSpacing(12)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setFormAlignment(Qt.AlignTop)
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)

        self.img_cmb_model = self._create_setting_combo([
            ("Nano Banana 2", "Nano Banana 2"),
            ("Nano Banana 2 Lite", "Nano Banana 2 Lite"),
            ("Nano Banana Pro", "Nano Banana Pro"),
            ("Imagen 4", "Imagen 4"),
        ], current_data="Nano Banana 2")
        self.img_cmb_ratio = self._create_setting_combo([
            ("Landscape (16:9)", "Landscape (16:9)"),
            ("Standard (4:3)", "Standard (4:3)"),
            ("Square (1:1)", "Square (1:1)"),
            ("Portrait (3:4)", "Portrait (3:4)"),
            ("Tall Portrait (9:16)", "Tall Portrait (9:16)"),
        ], current_data="Landscape (16:9)")
        self.img_cmb_outputs = self._create_setting_combo([("x1", 1), ("x2", 2), ("x3", 3), ("x4", 4)], current_data=1)
        # Genspark-only: output resolution. Ignored in Flow mode.
        self.img_cmb_quality = self._create_setting_combo([
            ("Auto (default)", "auto"),
            ("0.5K (fast)", "0.5k"),
            ("1K (1024px)", "1k"),
            ("2K (2048px)", "2k"),
            ("4K (4096px)", "4k"),
        ], current_data="auto")
        self.img_cmb_quality.setToolTip(
            "Output resolution for Genspark mode.\n\n"
            "Per-job setting — every prompt added after changing this uses the\n"
            "new resolution. Ignored in Flow mode (Flow picks its own size).\n\n"
            "Plus plan: 2K max. Pro plan: unlimited 4K with Nano Banana Pro."
        )
        self.img_cmb_parallel = self._create_parallel_combo(saved_slots)

        # Genspark-only: Auto Prompt toggle. When OFF (default) the raw
        # prompt is sent straight to image generation, bypassing Genspark's
        # LLM agent (ask_proxy). The agent path was hitting 429 rate limits
        # and an SSE-parsing bug, so OFF is the working configuration.
        self.img_chk_auto_prompt = QCheckBox("Auto Prompt (let Genspark rewrite)")
        self.img_chk_auto_prompt.setChecked(get_bool_setting("genspark_auto_prompt", False))
        self.img_chk_auto_prompt.setToolTip(
            "When ON: Genspark's LLM agent rewrites your prompt before generating "
            "(adds 'no text/letters/symbols' style guards, etc).\n\n"
            "When OFF (default): your prompt goes directly to the image model.\n\n"
            "Recommended OFF — the agent path rate-limits hard (~10 requests "
            "before HTTP 429) and currently has an SSE-parsing bug. Genspark "
            "mode only."
        )

        form.addRow(self._make_setting_label("Model:"), self.img_cmb_model)
        form.addRow(
            self._make_setting_label("Ratio:"),
            self._make_inline_row(
                self.img_cmb_ratio,
                self._make_setting_label("Outputs:"),
                self.img_cmb_outputs,
            ),
        )
        form.addRow(self._make_setting_label("Quality:"), self.img_cmb_quality)
        form.addRow(self._make_setting_label("Parallel:"), self.img_cmb_parallel)
        form.addRow("", self.img_chk_auto_prompt)
        layout.addLayout(form)

        refs_header = QHBoxLayout()
        self.img_btn_add_refs = QPushButton("+ Add Reference Image(s)")
        self.img_btn_add_refs.setProperty("role", "browse")
        self.img_btn_add_refs.clicked.connect(self.select_reference_image)
        self.lbl_ref_status = QLabel("None")
        self.lbl_ref_status.setObjectName("settingHint")
        self.btn_clear_ref = QPushButton("Clear All")
        self.btn_clear_ref.setObjectName("refClearButton")
        self.btn_clear_ref.setProperty("role", "danger")
        self.btn_clear_ref.clicked.connect(self.clear_reference_image)
        self.btn_clear_ref.setVisible(False)
        refs_header.addWidget(self.img_btn_add_refs)
        refs_header.addWidget(self.lbl_ref_status, 1)
        refs_header.addWidget(self.btn_clear_ref)
        refs_widget = QWidget()
        refs_widget.setLayout(refs_header)
        form.addRow(self._make_setting_label("Reference:"), refs_widget)

        self.ref_items_container = QWidget()
        self.ref_items_layout = QVBoxLayout(self.ref_items_container)
        self.ref_items_layout.setContentsMargins(0, 0, 0, 0)
        self.ref_items_layout.setSpacing(6)
        self.ref_items_container.setVisible(False)
        layout.addWidget(self.ref_items_container)
        layout.addStretch()

    def _setup_video_t2v_tab(self, saved_slots):
        layout, self.t2v_tab_scroll = self._create_tab_scroll_content(self.mode_tab_t2v, 230)
        layout.addWidget(self._create_tab_section_title("Text-to-Video Settings"))

        form = QFormLayout()
        form.setSpacing(12)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setFormAlignment(Qt.AlignTop)
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)

        self.t2v_cmb_quality = self._create_setting_combo([
            ("Veo 3.1 - Fast", "Veo 3.1 - Fast"),
            ("Veo 3.1 - Lite", "Veo 3.1 - Lite"),
            ("Veo 3.1 - Fast [Lower Pri]", "Veo 3.1 - Fast [Lower Pri]"),
            ("Veo 3.1 - Lite [Lower Pri]", "Veo 3.1 - Lite [Lower Pri]"),
            ("Veo 3.1 - Quality", "Veo 3.1 - Quality"),
        ], current_data="Veo 3.1 - Fast")
        self.t2v_cmb_ratio = self._create_setting_combo([
            ("Landscape (16:9)", "Landscape (16:9)"),
            ("Portrait (9:16)", "Portrait (9:16)"),
        ], current_data="Landscape (16:9)")
        self.t2v_cmb_outputs = self._create_setting_combo([("x1", 1), ("x2", 2), ("x3", 3), ("x4", 4)], current_data=1)
        self.t2v_cmb_upscale = self._create_setting_combo([
            ("720p", "none"),
            ("1080p (Free)", "1080p"),
            ("4K (+50)", "4k"),
        ], current_data="none")
        # Grok-specific combos: visible only when generation_mode =
        # chrome_extension_grok (toggled in _sync_generation_mode_ui).
        # Grok caps at 720p and only supports 6s / 10s durations.
        self.t2v_cmb_grok_res, self.t2v_cmb_grok_dur = self._create_grok_res_dur_combos()
        self.t2v_lbl_grok_res = self._make_setting_label("Res:")
        self.t2v_lbl_grok_dur = self._make_setting_label("Dur:")
        self.t2v_lbl_upscale = self._make_setting_label("Upscale:")
        self.t2v_cmb_parallel = self._create_parallel_combo(saved_slots)

        form.addRow(self._make_setting_label("Quality:"), self.t2v_cmb_quality)
        form.addRow(
            self._make_setting_label("Ratio:"),
            self._make_inline_row(
                self.t2v_cmb_ratio,
                self._make_setting_label("Outputs:"),
                self.t2v_cmb_outputs,
            ),
        )
        form.addRow(
            self.t2v_lbl_upscale,
            self._make_inline_row(
                self.t2v_cmb_upscale,
                self.t2v_lbl_grok_res, self.t2v_cmb_grok_res,
                self.t2v_lbl_grok_dur, self.t2v_cmb_grok_dur,
                self._make_setting_label("Parallel:"),
                self.t2v_cmb_parallel,
            ),
        )
        self._set_grok_row_visible(
            self.t2v_cmb_upscale,
            (self.t2v_lbl_grok_res, self.t2v_cmb_grok_res,
             self.t2v_lbl_grok_dur, self.t2v_cmb_grok_dur),
            self.t2v_lbl_upscale,
            False,
        )

        # Dola-specific settings — shown only when generation mode is
        # "Chrome Extension — Dola" (toggled in _sync_generation_mode_ui).
        self._build_dola_settings_row(form)

        layout.addLayout(form)
        layout.addStretch()

    def _build_dola_settings_row(self, form):
        """Model / Ratio / Duration selectors for dola.com (Seedance) mode, added
        to each video sub-tab. They persist to the dola_model / dola_ratio /
        dola_duration settings (kept in sync across tabs) which dola_mode.py reads
        as the run-wide generation config. Hidden unless dola mode is active."""
        if not hasattr(self, "_dola_setting_rows"):
            self._dola_setting_rows = []
            self._dola_model_combos = []
            self._dola_ratio_combos = []
            self._dola_dur_combos = []
            self._dola_autodelete_checks = []
        try:
            cur_model = str(get_setting("dola_model", "seedance_v2.0") or "seedance_v2.0")
            cur_ratio = str(get_setting("dola_ratio", "9:16") or "9:16")
            try:
                cur_dur = int(str(get_setting("dola_duration", "10") or "10"))
            except Exception:
                cur_dur = 10

            m = self._create_setting_combo(
                [("Seedance 2.5 (Best)", "seedance_v2.5"),
                 ("Seedance 2.0 Fast", "seedance_v2.0"),
                 ("Seedance 1.0", "ic_mini")],
                current_data=cur_model, trigger_sync=False,
            )
            r = self._create_setting_combo(
                [(x, x) for x in ("9:16", "16:9", "1:1", "3:4", "4:3", "21:9")],
                current_data=cur_ratio, trigger_sync=False,
            )
            d = self._create_setting_combo(
                [("5s", 5), ("10s", 10)], current_data=cur_dur, trigger_sync=False,
            )
            self._dola_model_combos.append(m)
            self._dola_ratio_combos.append(r)
            self._dola_dur_combos.append(d)

            m.currentIndexChanged.connect(
                lambda _=None, c=m: self._on_dola_setting_changed("dola_model", c, self._dola_model_combos)
            )
            r.currentIndexChanged.connect(
                lambda _=None, c=r: self._on_dola_setting_changed("dola_ratio", c, self._dola_ratio_combos)
            )
            d.currentIndexChanged.connect(
                lambda _=None, c=d: self._on_dola_setting_changed("dola_duration", c, self._dola_dur_combos)
            )

            chk = QCheckBox("Auto-delete on daily limit")
            chk.setToolTip(
                "Opt-in: when dola's real 'daily limit for video generation' message "
                "appears for an account, delete that account from dola.com — but ONLY "
                "after all its running generations finish. Then continue on other "
                "accounts. No prediction. PERMANENT/irreversible."
            )
            chk.setChecked(str(get_setting("dola_auto_delete", "1") or "1").strip() in ("1", "true", "on", "yes"))
            self._dola_autodelete_checks.append(chk)
            chk.toggled.connect(lambda checked=False: self._on_dola_autodelete_toggled(checked))

            lbl = self._make_setting_label("Dola:")
            field = self._make_inline_row(
                m, self._make_setting_label("Ratio:"), r,
                self._make_setting_label("Dur:"), d, chk,
            )
            form.addRow(lbl, field)
            lbl.setVisible(False)
            field.setVisible(False)
            self._dola_setting_rows.append((lbl, field))
        except Exception as exc:
            try:
                self.append_log(f"[UI] Dola settings row build warning: {exc}")
            except Exception:
                pass

    def _on_dola_autodelete_toggled(self, checked):
        """Persist the dola auto-delete opt-in and mirror across tabs."""
        try:
            set_setting("dola_auto_delete", "1" if checked else "0")
            for c in getattr(self, "_dola_autodelete_checks", []):
                if c.isChecked() != bool(checked):
                    c.blockSignals(True)
                    c.setChecked(bool(checked))
                    c.blockSignals(False)
        except Exception:
            pass

    def _on_dola_setting_changed(self, key, combo, siblings):
        """Persist a dola setting and mirror the choice into the same combo on
        the other video sub-tabs so they don't drift."""
        try:
            data = combo.currentData()
            set_setting(key, str(data))
            for c in siblings:
                if c is combo:
                    continue
                idx = c.findData(data)
                if idx >= 0 and c.currentIndex() != idx:
                    c.blockSignals(True)
                    c.setCurrentIndex(idx)
                    c.blockSignals(False)
        except Exception:
            pass

    def _setup_video_ref_tab(self, saved_slots):
        layout, self.ref_tab_scroll = self._create_tab_scroll_content(self.mode_tab_ref, 260)
        layout.addWidget(self._create_tab_section_title("Video + Reference Settings"))

        form = QFormLayout()
        form.setSpacing(12)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setFormAlignment(Qt.AlignTop)
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)

        self.ref_cmb_quality = self._create_setting_combo([
            ("Veo 3.1 - Fast", "Veo 3.1 - Fast"),
            ("Veo 3.1 - Lite", "Veo 3.1 - Lite"),
            ("Veo 3.1 - Quality", "Veo 3.1 - Quality"),
            ("Veo 3.1 - Fast [Lower Pri]", "Veo 3.1 - Fast [Lower Pri]"),
            ("Veo 3.1 - Lite [Lower Pri]", "Veo 3.1 - Lite [Lower Pri]"),
        ], current_data="Veo 3.1 - Fast")
        self.ref_cmb_ratio = self._create_setting_combo([
            ("Landscape (16:9)", "Landscape (16:9)"),
            ("Portrait (9:16)", "Portrait (9:16)"),
        ], current_data="Landscape (16:9)")
        self.ref_cmb_outputs = self._create_setting_combo([("x1", 1), ("x2", 2), ("x3", 3), ("x4", 4)], current_data=1)
        self.ref_cmb_upscale = self._create_setting_combo([
            ("720p", "none"),
            ("1080p (Free)", "1080p"),
            ("4K (+50)", "4k"),
        ], current_data="none")
        self.ref_cmb_grok_res, self.ref_cmb_grok_dur = self._create_grok_res_dur_combos()
        self.ref_lbl_grok_res = self._make_setting_label("Res:")
        self.ref_lbl_grok_dur = self._make_setting_label("Dur:")
        self.ref_lbl_upscale = self._make_setting_label("Upscale:")
        self.ref_cmb_parallel = self._create_parallel_combo(saved_slots)

        form.addRow(self._make_setting_label("Quality:"), self.ref_cmb_quality)
        form.addRow(
            self._make_setting_label("Ratio:"),
            self._make_inline_row(
                self.ref_cmb_ratio,
                self._make_setting_label("Outputs:"),
                self.ref_cmb_outputs,
            ),
        )
        form.addRow(
            self.ref_lbl_upscale,
            self._make_inline_row(
                self.ref_cmb_upscale,
                self.ref_lbl_grok_res, self.ref_cmb_grok_res,
                self.ref_lbl_grok_dur, self.ref_cmb_grok_dur,
                self._make_setting_label("Parallel:"),
                self.ref_cmb_parallel,
            ),
        )
        self._set_grok_row_visible(
            self.ref_cmb_upscale,
            (self.ref_lbl_grok_res, self.ref_cmb_grok_res,
             self.ref_lbl_grok_dur, self.ref_cmb_grok_dur),
            self.ref_lbl_upscale,
            False,
        )

        self.ref_single_row, self.btn_ref_single_browse, self.lbl_ref_single, self.btn_ref_single_clear = self._create_path_row(
            "Browse Reference Image",
            self.select_single_reference_image,
            self.clear_single_reference_image,
        )
        form.addRow(self._make_setting_label("Reference:"), self.ref_single_row)
        self._build_dola_settings_row(form)
        layout.addLayout(form)

        layout.addWidget(self._create_separator())
        self.ref_bulk_group = self._create_bulk_panel("ingredients")
        layout.addWidget(self.ref_bulk_group)
        layout.addStretch()

    def _setup_video_frames_tab(self, saved_slots):
        layout, self.frames_tab_scroll = self._create_tab_scroll_content(self.mode_tab_frames, 280)
        layout.addWidget(self._create_tab_section_title("Frames Settings"))

        form = QFormLayout()
        form.setSpacing(12)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setFormAlignment(Qt.AlignTop)
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)

        self.frm_cmb_mode = self._create_setting_combo([
            ("Start Only", "frames_start"),
            ("Start + End", "frames_start_end"),
        ], current_data="frames_start")
        self.frm_cmb_mode.currentIndexChanged.connect(lambda _=None: self._sync_generation_mode_ui())
        self.frm_cmb_quality = self._create_setting_combo([
            ("Veo 3.1 - Fast", "Veo 3.1 - Fast"),
            ("Veo 3.1 - Lite", "Veo 3.1 - Lite"),
            ("Veo 3.1 - Fast [Lower Pri]", "Veo 3.1 - Fast [Lower Pri]"),
            ("Veo 3.1 - Lite [Lower Pri]", "Veo 3.1 - Lite [Lower Pri]"),
            ("Veo 3.1 - Quality", "Veo 3.1 - Quality"),
        ], current_data="Veo 3.1 - Fast")
        self.frm_cmb_ratio = self._create_setting_combo([
            ("Landscape (16:9)", "Landscape (16:9)"),
            ("Portrait (9:16)", "Portrait (9:16)"),
        ], current_data="Landscape (16:9)")
        self.frm_cmb_outputs = self._create_setting_combo([("x1", 1), ("x2", 2), ("x3", 3), ("x4", 4)], current_data=1)
        self.frm_cmb_upscale = self._create_setting_combo([
            ("720p", "none"),
            ("1080p (Free)", "1080p"),
            ("4K (+50)", "4k"),
        ], current_data="none")
        self.frm_cmb_grok_res, self.frm_cmb_grok_dur = self._create_grok_res_dur_combos()
        self.frm_lbl_grok_res = self._make_setting_label("Res:")
        self.frm_lbl_grok_dur = self._make_setting_label("Dur:")
        self.frm_lbl_upscale = self._make_setting_label("Upscale:")
        self.frm_cmb_parallel = self._create_parallel_combo(saved_slots)

        form.addRow(
            self._make_setting_label("Frame Mode:"),
            self._make_inline_row(
                self.frm_cmb_mode,
                self._make_setting_label("Quality:"),
                self.frm_cmb_quality,
            ),
        )
        form.addRow(
            self._make_setting_label("Ratio:"),
            self._make_inline_row(
                self.frm_cmb_ratio,
                self._make_setting_label("Outputs:"),
                self.frm_cmb_outputs,
            ),
        )
        form.addRow(
            self.frm_lbl_upscale,
            self._make_inline_row(
                self.frm_cmb_upscale,
                self.frm_lbl_grok_res, self.frm_cmb_grok_res,
                self.frm_lbl_grok_dur, self.frm_cmb_grok_dur,
                self._make_setting_label("Parallel:"),
                self.frm_cmb_parallel,
            ),
        )
        self._set_grok_row_visible(
            self.frm_cmb_upscale,
            (self.frm_lbl_grok_res, self.frm_cmb_grok_res,
             self.frm_lbl_grok_dur, self.frm_cmb_grok_dur),
            self.frm_lbl_upscale,
            False,
        )

        self.start_row, self.btn_start_image, self.lbl_start_image, self.btn_clear_start_image = self._create_path_row(
            "Browse Start Image",
            self.select_start_image,
            self.clear_start_image,
        )
        self.end_row, self.btn_end_image, self.lbl_end_image, self.btn_clear_end_image = self._create_path_row(
            "Browse End Image",
            self.select_end_image,
            self.clear_end_image,
        )
        form.addRow(self._make_setting_label("Start Image:"), self.start_row)
        form.addRow(self._make_setting_label("End Image:"), self.end_row)
        layout.addLayout(form)

        self.frm_bulk_separator = self._create_separator()
        layout.addWidget(self.frm_bulk_separator)
        self.frm_bulk_group = self._create_bulk_panel("frames_start")
        layout.addWidget(self.frm_bulk_group)
        layout.addStretch()

    def _setup_pipeline_tab(self, saved_slots):
        layout, self.pipeline_tab_scroll = self._create_tab_scroll_content(self.mode_tab_pipeline, 300)
        layout.addWidget(self._create_tab_section_title("Image -> Video Pipeline"))

        step1_title = self._create_tab_section_title("Step 1: Image Generation")
        layout.addWidget(step1_title)

        form1 = QFormLayout()
        form1.setSpacing(12)
        form1.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form1.setFormAlignment(Qt.AlignTop)
        form1.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)

        self.pipe_cmb_img_model = self._create_setting_combo([
            ("Imagen 4", "Imagen 4"),
            ("Nano Banana Pro", "Nano Banana Pro"),
            ("Nano Banana 2", "Nano Banana 2"),
            ("Nano Banana 2 Lite", "Nano Banana 2 Lite"),
        ], current_data="Imagen 4")
        self.pipe_cmb_img_ratio = self._create_setting_combo([
            ("Landscape (16:9)", "Landscape (16:9)"),
            ("Standard (4:3)", "Standard (4:3)"),
            ("Square (1:1)", "Square (1:1)"),
            ("Portrait (3:4)", "Portrait (3:4)"),
            ("Tall Portrait (9:16)", "Tall Portrait (9:16)"),
        ], current_data="Landscape (16:9)")
        # Outputs per pipeline prompt — mirrors the other tabs so user
        # can choose how many image variants to generate per Step 1.
        # Default x1 (save credits). Pipeline feeds the FIRST media_id
        # to Step 2 regardless of count, so x2+ gives more thumbnails
        # saved but only one proceeds to video.
        self.pipe_cmb_img_outputs = self._create_setting_combo(
            [("x1", 1), ("x2", 2), ("x3", 3), ("x4", 4)], current_data=1,
        )
        form1.addRow(self._make_setting_label("Image Model:"), self.pipe_cmb_img_model)
        form1.addRow(self._make_setting_label("Image Ratio:"), self.pipe_cmb_img_ratio)
        form1.addRow(self._make_setting_label("Image Outputs:"), self.pipe_cmb_img_outputs)

        pipe_ref_header = QHBoxLayout()
        self.pipe_btn_add_refs = QPushButton("+ Add Reference Image(s)")
        self.pipe_btn_add_refs.setProperty("role", "browse")
        self.pipe_btn_add_refs.clicked.connect(self.select_pipeline_reference_images)
        self.pipe_lbl_ref_status = QLabel("None")
        self.pipe_lbl_ref_status.setObjectName("settingHint")
        self.pipe_btn_clear_refs = QPushButton("Clear All")
        self.pipe_btn_clear_refs.setObjectName("refClearButton")
        self.pipe_btn_clear_refs.setProperty("role", "danger")
        self.pipe_btn_clear_refs.clicked.connect(self.clear_pipeline_reference_images)
        self.pipe_btn_clear_refs.setVisible(False)
        pipe_ref_header.addWidget(self.pipe_btn_add_refs)
        pipe_ref_header.addWidget(self.pipe_lbl_ref_status, 1)
        pipe_ref_header.addWidget(self.pipe_btn_clear_refs)
        pipe_ref_widget = QWidget()
        pipe_ref_widget.setLayout(pipe_ref_header)
        form1.addRow(self._make_setting_label("Reference:"), pipe_ref_widget)
        layout.addLayout(form1)

        self.pipe_ref_items_container = QWidget()
        self.pipe_ref_items_layout = QVBoxLayout(self.pipe_ref_items_container)
        self.pipe_ref_items_layout.setContentsMargins(0, 0, 0, 0)
        self.pipe_ref_items_layout.setSpacing(6)
        self.pipe_ref_items_container.setVisible(False)
        layout.addWidget(self.pipe_ref_items_container)

        layout.addWidget(self._create_separator())
        step2_title = self._create_tab_section_title("Step 2: Video Generation")
        layout.addWidget(step2_title)

        form2 = QFormLayout()
        form2.setSpacing(12)
        form2.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form2.setFormAlignment(Qt.AlignTop)
        form2.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)

        self.pipe_cmb_vid_mode = self._create_setting_combo([
            ("Ingredients (Reference Style)", "ingredients"),
            ("Frames - Start Image", "frames_start"),
        ], current_data="ingredients")
        self.pipe_cmb_vid_mode.currentIndexChanged.connect(self._on_pipeline_video_mode_changed)

        self.pipe_cmb_vid_quality = self._create_setting_combo([], trigger_sync=False)
        self.pipe_cmb_vid_quality.currentIndexChanged.connect(lambda _=None: self._on_generation_settings_changed())
        self._update_pipeline_video_quality_options("ingredients")

        self.pipe_cmb_vid_ratio = self._create_setting_combo([
            ("Landscape (16:9)", "Landscape (16:9)"),
            ("Portrait (9:16)", "Portrait (9:16)"),
        ], current_data="Landscape (16:9)")

        self.pipe_txt_vid_prompt = QLineEdit()
        self.pipe_txt_vid_prompt.setObjectName("settingInput")
        self.pipe_txt_vid_prompt.setMinimumHeight(38)
        self.pipe_txt_vid_prompt.setPlaceholderText('Optional - default: "animate"')
        self.pipe_txt_vid_prompt.textChanged.connect(lambda _=None: self._on_generation_settings_changed())

        self.pipe_cmb_upscale = self._create_setting_combo([
            ("720p", "none"),
            ("1080p (Free)", "1080p"),
            ("4K (+50)", "4k"),
        ], current_data="none")
        self.pipe_cmb_grok_res, self.pipe_cmb_grok_dur = self._create_grok_res_dur_combos()
        self.pipe_lbl_grok_res = self._make_setting_label("Res:")
        self.pipe_lbl_grok_dur = self._make_setting_label("Dur:")
        self.pipe_lbl_upscale = self._make_setting_label("Upscale:")
        # Outputs per video prompt — same semantics as the other tabs.
        # x1 by default to keep credit burn predictable. x2+ multiplies
        # video credits accordingly (estimate updates live).
        self.pipe_cmb_vid_outputs = self._create_setting_combo(
            [("x1", 1), ("x2", 2), ("x3", 3), ("x4", 4)], current_data=1,
        )
        self.pipe_cmb_parallel = self._create_parallel_combo(saved_slots)

        form2.addRow(self._make_setting_label("Video Mode:"), self.pipe_cmb_vid_mode)
        form2.addRow(self._make_setting_label("Video Quality:"), self.pipe_cmb_vid_quality)
        form2.addRow(self._make_setting_label("Video Ratio:"), self.pipe_cmb_vid_ratio)
        form2.addRow(self._make_setting_label("Video Outputs:"), self.pipe_cmb_vid_outputs)
        form2.addRow(self._make_setting_label("Video Prompt:"), self.pipe_txt_vid_prompt)
        form2.addRow(
            self.pipe_lbl_upscale,
            self._make_inline_row(
                self.pipe_cmb_upscale,
                self.pipe_lbl_grok_res, self.pipe_cmb_grok_res,
                self.pipe_lbl_grok_dur, self.pipe_cmb_grok_dur,
                self._make_setting_label("Parallel:"),
                self.pipe_cmb_parallel,
            ),
        )
        self._set_grok_row_visible(
            self.pipe_cmb_upscale,
            (self.pipe_lbl_grok_res, self.pipe_cmb_grok_res,
             self.pipe_lbl_grok_dur, self.pipe_cmb_grok_dur),
            self.pipe_lbl_upscale,
            False,
        )
        layout.addLayout(form2)

        layout.addWidget(self._create_separator())
        prompts_title = self._create_tab_section_title("Pipeline Prompts")
        layout.addWidget(prompts_title)

        prompts_split = QHBoxLayout()
        prompts_split.setSpacing(12)

        img_prompt_box = QVBoxLayout()
        img_prompt_box.setSpacing(6)
        img_prompt_label = QLabel("Image Prompts (one per line)")
        img_prompt_label.setStyleSheet("color: #F8FAFC; font-weight: 700;")
        self.pipe_txt_img_prompts = QPlainTextEdit()
        self.pipe_txt_img_prompts.setPlaceholderText("Enter image generation prompts here, one per line...")
        self.pipe_txt_img_prompts.setMinimumHeight(120)
        self.pipe_txt_img_prompts.verticalScrollBar().setSingleStep(6)
        self.pipe_txt_img_prompts.textChanged.connect(self._update_pipeline_count)
        img_prompt_box.addWidget(img_prompt_label)
        img_prompt_box.addWidget(self.pipe_txt_img_prompts)

        vid_prompt_box = QVBoxLayout()
        vid_prompt_box.setSpacing(6)
        vid_prompt_label = QLabel("Video Prompts (one per line)")
        vid_prompt_label.setStyleSheet("color: #F8FAFC; font-weight: 700;")
        self.pipe_txt_vid_prompts = QPlainTextEdit()
        self.pipe_txt_vid_prompts.setPlaceholderText('Video prompts - leave blank lines for "animate" default...')
        self.pipe_txt_vid_prompts.setMinimumHeight(120)
        self.pipe_txt_vid_prompts.verticalScrollBar().setSingleStep(6)
        self.pipe_txt_vid_prompts.textChanged.connect(self._update_pipeline_count)
        vid_prompt_box.addWidget(vid_prompt_label)
        vid_prompt_box.addWidget(self.pipe_txt_vid_prompts)

        prompts_split.addLayout(img_prompt_box, 1)
        prompts_split.addLayout(vid_prompt_box, 1)
        layout.addLayout(prompts_split)

        self.pipe_lbl_count = QLabel("")
        self.pipe_lbl_count.setObjectName("settingHint")
        layout.addWidget(self.pipe_lbl_count)

        pipe_actions = QHBoxLayout()
        pipe_actions.addStretch()
        _PipeAddCls = PrimaryPushButton if _FLUENT_UI_AVAILABLE else QPushButton
        self.pipe_btn_add_bulk = _PipeAddCls()
        self.pipe_btn_add_bulk.setText("Add All to Queue")
        if _FLUENT_UI_AVAILABLE:
            try:
                self.pipe_btn_add_bulk.setIcon(FluentIcon.ADD)
            except Exception:
                pass
        else:
            self.pipe_btn_add_bulk.setProperty("role", "primaryGradient")
        self.pipe_btn_add_bulk.setFixedHeight(36)
        self.pipe_btn_add_bulk.setMinimumWidth(180)
        self.pipe_btn_add_bulk.clicked.connect(self._add_pipeline_to_queue)
        pipe_actions.addWidget(self.pipe_btn_add_bulk)
        layout.addLayout(pipe_actions)
        self._update_pipeline_count()
        layout.addStretch()

    def _create_bulk_panel(self, mode_key):
        wrapper = QWidget()
        wrapper_layout = QVBoxLayout(wrapper)
        wrapper_layout.setContentsMargins(0, 0, 0, 0)
        wrapper_layout.setSpacing(8)

        bulk_toggle = QPushButton("📦 Bulk Image Matching  ▶")
        bulk_toggle.setCheckable(True)
        bulk_toggle.setChecked(False)
        bulk_toggle.setProperty("role", "bulkToggle")
        bulk_toggle.clicked.connect(lambda checked=False, key=mode_key: self._toggle_bulk_section(key, checked))
        wrapper_layout.addWidget(bulk_toggle)

        content = QFrame()
        content.setObjectName("bulkPanel")
        content.setVisible(False)
        panel_layout = QVBoxLayout(content)
        panel_layout.setContentsMargins(10, 10, 10, 10)
        panel_layout.setSpacing(10)

        browse_style = """
            QPushButton {
                background: #1A2744;
                color: #3B82F6;
                border: 1px dashed #3B82F6;
                border-radius: 6px;
                padding: 8px 16px;
                font-size: 13px;
                font-weight: 600;
                min-width: 100px;
            }
            QPushButton:hover {
                background: #1E2D4A;
            }
        """
        clear_style = """
            QPushButton {
                background: #1F1A2A;
                color: #EF4444;
                border: 1px solid #EF4444;
                border-radius: 6px;
                padding: 8px 16px;
                font-size: 13px;
                font-weight: 600;
                min-width: 90px;
            }
            QPushButton:hover {
                background: #2A1D2C;
            }
        """
        add_style = """
            QPushButton {
                background: #3B82F6;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 10px 18px;
                font-size: 13px;
                font-weight: 700;
                min-width: 150px;
            }
            QPushButton:hover {
                background: #2563EB;
            }
        """

        header = QHBoxLayout()
        btn_folder = QPushButton()
        btn_folder.setText("Browse Folder")
        btn_folder.setProperty("role", "browse")
        btn_folder.setStyleSheet(browse_style)
        btn_folder.clicked.connect(lambda _=False, key=mode_key: self.select_bulk_image_folder(key))
        btn_files = QPushButton()
        btn_files.setText("Browse Images")
        btn_files.setProperty("role", "browse")
        btn_files.setStyleSheet(browse_style)
        btn_files.clicked.connect(lambda _=False, key=mode_key: self.select_bulk_image_files(key))
        lbl_loaded = QLabel("0 image(s)")
        lbl_loaded.setObjectName("settingHint")
        btn_clear = QPushButton()
        btn_clear.setText("Clear")
        btn_clear.setProperty("role", "danger")
        btn_clear.setStyleSheet(clear_style)
        btn_clear.clicked.connect(lambda _=False, key=mode_key: self.clear_bulk_panel(key))
        header.addWidget(btn_folder)
        header.addWidget(btn_files)
        header.addWidget(lbl_loaded)
        header.addStretch()
        header.addWidget(btn_clear)
        panel_layout.addLayout(header)

        sort_row = QHBoxLayout()
        sort_label = self._make_setting_label("Sort:")
        sort_row.addWidget(sort_label)
        cmb_sort = self._create_setting_combo([
            ("Name A-Z", "name_asc"),
            ("Name Z-A", "name_desc"),
            ("Time Old-New", "time_old"),
            ("Time New-Old", "time_new"),
        ], current_data="name_asc", trigger_sync=False)
        cmb_sort.currentIndexChanged.connect(lambda _=None, key=mode_key: self._refresh_bulk_pairing_preview(key))
        sort_row.addWidget(cmb_sort)
        missing_label = self._make_setting_label("Missing Prompt:")
        sort_row.addWidget(missing_label)
        cmb_missing = self._create_setting_combo([
            ("Use filename", "filename"),
            ("Skip", "skip"),
        ], current_data="filename", trigger_sync=False)
        cmb_missing.currentIndexChanged.connect(lambda _=None, key=mode_key: self._refresh_bulk_pairing_preview(key))
        sort_row.addWidget(cmb_missing)
        sort_row.addStretch()
        panel_layout.addLayout(sort_row)

        tbl_images = BulkImageDropTable(0, 3)
        tbl_images.setHorizontalHeaderLabels(["#", "Image", "Filename"])
        tbl_images.verticalHeader().setVisible(False)
        tbl_images.setAlternatingRowColors(True)
        tbl_images.setSelectionMode(QTableWidget.NoSelection)
        tbl_images.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        tbl_images.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        tbl_images.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        tbl_images.setMinimumHeight(110)
        self._configure_table_scrolling(tbl_images)
        tbl_images.setVisible(False)
        tbl_images.files_dropped.connect(lambda paths, key=mode_key: self._load_bulk_images_from_paths(key, paths))
        panel_layout.addWidget(tbl_images)

        prompts_input = QTextEdit()
        prompts_input.setAcceptRichText(False)
        prompts_input.setPlaceholderText("Prompts, one per line. Line 1 pairs with image 1.")
        prompts_input.setMinimumHeight(84)
        prompts_input.verticalScrollBar().setSingleStep(6)
        # Debounced refresh: textChanged fires on every keystroke, and the
        # preview rebuild walks every image to draw thumbnails (~30ms each
        # on disk). 200 prompts × 200 images = the UI was hanging for 5-10s
        # mid-paste. Delay the actual rebuild by 300ms after the last change
        # so a paste of 400 lines triggers ONE refresh instead of 400.
        prompts_input.textChanged.connect(
            lambda key=mode_key: self._schedule_bulk_pairing_refresh(key)
        )
        panel_layout.addWidget(prompts_input)

        pairs_table = QTableWidget(0, 3)
        pairs_table.setHorizontalHeaderLabels(["#", "Image", "Paired Prompt"])
        pairs_table.verticalHeader().setVisible(False)
        pairs_table.setAlternatingRowColors(True)
        pairs_table.setSelectionMode(QTableWidget.NoSelection)
        pairs_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        pairs_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        pairs_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        pairs_table.setMinimumHeight(130)
        self._configure_table_scrolling(pairs_table)
        pairs_table.setVisible(False)
        panel_layout.addWidget(pairs_table)

        lbl_hint = QLabel("Drop images or browse, then add prompts to preview pairings.")
        lbl_hint.setObjectName("settingHint")
        lbl_hint.setWordWrap(True)
        panel_layout.addWidget(lbl_hint)

        _BulkAddCls = PrimaryPushButton if _FLUENT_UI_AVAILABLE else QPushButton
        btn_add = _BulkAddCls()
        btn_add.setText("Add All to Queue")
        if _FLUENT_UI_AVAILABLE:
            try:
                btn_add.setIcon(FluentIcon.ADD)
            except Exception:
                pass
        else:
            btn_add.setProperty("role", "primaryGradient")
            btn_add.setStyleSheet(add_style)
        btn_add.setFixedHeight(36)
        btn_add.setMinimumWidth(180)
        btn_add.clicked.connect(lambda _=False, key=mode_key: self.add_bulk_i2v_to_queue(key))
        panel_layout.addWidget(btn_add, alignment=Qt.AlignLeft)
        panel_layout.addStretch()
        wrapper_layout.addWidget(content)

        self.bulk_panels[mode_key] = {
            "group": wrapper,
            "wrapper": wrapper,
            "toggle": bulk_toggle,
            "content": content,
            "entries": [],
            "btn_folder": btn_folder,
            "btn_files": btn_files,
            "lbl_loaded": lbl_loaded,
            "btn_clear": btn_clear,
            "sort_selector": cmb_sort,
            "missing_selector": cmb_missing,
            "images_table": tbl_images,
            "prompts_input": prompts_input,
            "pairs_table": pairs_table,
            "hint_label": lbl_hint,
            "add_btn": btn_add,
        }
        return wrapper
        
    def setup_accounts(self):
        layout = QVBoxLayout(self.tab_accounts)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # ── Account Health Monitor overview card ──────────────────
        overview = QFrame()
        overview.setObjectName("accountOverviewCard")
        overview_layout = QHBoxLayout(overview)
        overview_layout.setContentsMargins(20, 14, 20, 14)
        overview_layout.setSpacing(10)
        overview_title = QLabel("Account Health Monitor")
        overview_title.setObjectName("accountOverviewTitle")
        overview_layout.addWidget(overview_title)
        overview_layout.addStretch()

        # Colored metric chips — each has its own variant via property
        # so the QSS can tint them individually (green/blue/orange/gray).
        def _mk_chip(text, variant):
            lbl = QLabel(text)
            lbl.setObjectName("accountMetricChip")
            lbl.setProperty("variant", variant)
            return lbl

        self.lbl_acc_total = _mk_chip("Total: 0", "neutral")
        self.lbl_acc_logged_in = _mk_chip("Logged In: 0", "success")
        self.lbl_acc_logged_out = _mk_chip("Logged Out: 0", "muted")
        self.lbl_acc_running = _mk_chip("Running: 0", "info")
        self.lbl_acc_cooldown = _mk_chip("Cooldown: 0", "warning")
        self.lbl_acc_ready = _mk_chip("Ready: 0", "success")

        overview_layout.addWidget(self.lbl_acc_total)
        overview_layout.addWidget(self.lbl_acc_logged_in)
        overview_layout.addWidget(self.lbl_acc_logged_out)
        overview_layout.addWidget(self.lbl_acc_running)
        overview_layout.addWidget(self.lbl_acc_cooldown)
        overview_layout.addWidget(self.lbl_acc_ready)
        layout.addWidget(overview)

        # ── Add New Google Account card ───────────────────────────
        add_group = QGroupBox("Add New Google Account Session")
        add_layout = QGridLayout()
        add_layout.setHorizontalSpacing(12)
        add_layout.setVerticalSpacing(10)
        add_layout.setContentsMargins(4, 4, 4, 4)

        lbl_acc_name_head = QLabel("Account Name")
        lbl_acc_name_head.setObjectName("settingLabel")
        lbl_acc_proxy_head = QLabel("Proxy")
        lbl_acc_proxy_head.setObjectName("settingLabel")

        self.acc_name_input = QLineEdit()
        self.acc_name_input.setPlaceholderText("Optional alias (leave blank for auto Gmail)")
        self.acc_name_input.setMinimumHeight(38)
        self.acc_proxy_input = QLineEdit()
        self.acc_proxy_input.setPlaceholderText("Optional proxy: socks5://user:pass@host:port")
        self.acc_proxy_input.setMinimumHeight(38)

        _LoginBtnCls = PrimaryPushButton if _FLUENT_UI_AVAILABLE else QPushButton
        self.btn_login = _LoginBtnCls()
        self.btn_login.setText("Login to Google (New Browser)")
        if _FLUENT_UI_AVAILABLE:
            try:
                self.btn_login.setIcon(FluentIcon.PEOPLE)
            except Exception:
                pass
        else:
            self.btn_login.setProperty("role", "primary")
        self.btn_login.setMinimumHeight(86)
        self.btn_login.setMinimumWidth(240)
        self.btn_login.clicked.connect(self.start_login)

        # Second login button — logs into dola.com (Continue with Google). The
        # saved profile ends up with dola.com session cookies (needed for dola
        # generation + the "Delete from dola.com" action).
        _LoginDolaCls = PrimaryPushButton if _FLUENT_UI_AVAILABLE else QPushButton
        self.btn_login_dola = _LoginDolaCls()
        self.btn_login_dola.setText("Login for dola (Google)")
        if not _FLUENT_UI_AVAILABLE:
            self.btn_login_dola.setProperty("role", "primary")
        self.btn_login_dola.setMinimumHeight(40)
        self.btn_login_dola.setMinimumWidth(240)
        self.btn_login_dola.setToolTip(
            "Opens real Chrome at Google login. Log into your Google account fully. "
            "dola.com then auto-logs-in on demand (generation + delete) — no extra click."
        )
        self.btn_login_dola.clicked.connect(lambda: self.start_login(login_target="dola"))

        add_layout.addWidget(lbl_acc_name_head, 0, 0)
        add_layout.addWidget(self.acc_name_input, 0, 1)
        add_layout.addWidget(lbl_acc_proxy_head, 1, 0)
        add_layout.addWidget(self.acc_proxy_input, 1, 1)
        add_layout.addWidget(self.btn_login, 0, 2, 2, 1)
        add_layout.addWidget(self.btn_login_dola, 2, 2)
        add_layout.setColumnStretch(1, 1)
        add_group.setLayout(add_layout)
        layout.addWidget(add_group)

        self.download_widget = QWidget()
        download_layout = QHBoxLayout(self.download_widget)
        download_layout.setContentsMargins(0, 4, 0, 4)
        download_layout.setSpacing(10)
        self.download_label = QLabel("Downloading CloakBrowser binary...")
        self.download_label.setStyleSheet("color: #60A5FA; font-weight: 600;")
        self.download_progress = QProgressBar()
        self.download_progress.setRange(0, 100)
        self.download_progress.setValue(0)
        self.download_progress.setFixedHeight(20)
        self.download_progress.setStyleSheet(
            """
            QProgressBar {
                border: 1px solid #334155;
                border-radius: 4px;
                background-color: #1E293B;
                text-align: center;
                color: white;
                font-weight: 600;
            }
            QProgressBar::chunk {
                background-color: #3B82F6;
                border-radius: 3px;
            }
            """
        )
        self.download_percent = QLabel("0%")
        self.download_percent.setStyleSheet("color: #60A5FA; font-weight: 600; min-width: 40px;")
        download_layout.addWidget(self.download_label)
        download_layout.addWidget(self.download_progress, 1)
        download_layout.addWidget(self.download_percent)
        self.download_widget.setVisible(False)
        layout.addWidget(self.download_widget)

        # ── Extension Accounts (Live from Chrome) ─────────────────
        self.ext_accounts_card = QFrame()
        self.ext_accounts_card.setObjectName("extAccountsCard")
        self.ext_accounts_card.setStyleSheet("""
            QFrame#extAccountsCard {
                background: #0F172A;
                border: 1px solid #1E293B;
                border-radius: 10px;
                padding: 0;
            }
        """)
        ext_card_layout = QVBoxLayout(self.ext_accounts_card)
        ext_card_layout.setContentsMargins(16, 12, 16, 12)
        ext_card_layout.setSpacing(8)

        # Header row
        ext_header = QHBoxLayout()
        ext_header.setSpacing(8)
        self.ext_status_dot = QLabel("●")
        self.ext_status_dot.setStyleSheet("color: #EF4444; font-size: 10px;")
        self.ext_status_dot.setFixedWidth(14)
        ext_title = QLabel("Chrome Extension Accounts")
        ext_title.setStyleSheet("color: #F1F5F9; font-size: 13px; font-weight: 700;")
        self.ext_status_label = QLabel("Extension not connected")
        self.ext_status_label.setStyleSheet("color: #64748B; font-size: 11px;")
        self.btn_ext_refresh = QPushButton("⟳")
        self.btn_ext_refresh.setFixedSize(28, 28)
        self.btn_ext_refresh.setToolTip("Refresh extension accounts")
        self.btn_ext_refresh.setStyleSheet("""
            QPushButton {
                background: #1E293B; border: 1px solid #334155;
                border-radius: 6px; color: #94A3B8; font-size: 14px;
            }
            QPushButton:hover { background: #334155; color: #F1F5F9; }
        """)
        self.btn_ext_refresh.clicked.connect(self._refresh_extension_accounts)
        # Standalone cookie-import bridge — start the bridge WITHOUT generation
        # so the extension can export cookies for CloakBrowser + proxy mode.
        self.btn_cookie_bridge = QPushButton("🍪 Connect (Cookie Import)")
        self.btn_cookie_bridge.setToolTip(
            "Starts the extension bridge WITHOUT running any generation.\n"
            "Then open the extension popup and click 'Export Cookies' for each\n"
            "account to set them up for HTTP Shared (CloakBrowser + proxy) mode."
        )
        self.btn_cookie_bridge.setStyleSheet("""
            QPushButton {
                background: #1E293B; border: 1px solid #334155;
                border-radius: 6px; color: #94A3B8; font-size: 11px;
                padding: 4px 10px;
            }
            QPushButton:hover { background: #334155; color: #F1F5F9; }
        """)
        self.btn_cookie_bridge.clicked.connect(self._toggle_cookie_bridge)
        ext_header.addWidget(self.ext_status_dot)
        ext_header.addWidget(ext_title)
        ext_header.addStretch()
        ext_header.addWidget(self.ext_status_label)
        ext_header.addWidget(self.btn_cookie_bridge)
        ext_header.addWidget(self.btn_ext_refresh)
        ext_card_layout.addLayout(ext_header)

        # Account list container
        self.ext_accounts_list = QVBoxLayout()
        self.ext_accounts_list.setSpacing(4)
        self.ext_no_accounts_label = QLabel("No accounts detected. Open labs.google in Chrome and log in.")
        self.ext_no_accounts_label.setStyleSheet("color: #475569; font-size: 12px; padding: 4px 0;")
        self.ext_accounts_list.addWidget(self.ext_no_accounts_label)
        ext_card_layout.addLayout(self.ext_accounts_list)

        layout.addWidget(self.ext_accounts_card)

        # Connect signal for thread-safe UI updates
        self._ext_accounts_signal.connect(self._apply_ext_accounts)

        # Timer to auto-refresh extension accounts every 5 seconds
        self._ext_accounts_timer = QTimer(self)
        self._ext_accounts_timer.setInterval(5000)
        self._ext_accounts_timer.timeout.connect(self._refresh_extension_accounts)
        self._ext_accounts_timer.start()
        # Initial fetch after 1 second
        QTimer.singleShot(1000, self._refresh_extension_accounts)

        self.acc_table = QTableWidget(0, 10)
        self.acc_table.setHorizontalHeaderLabels([
            "ID",
            "Account",
            "Proxy",
            "Status",
            "Session",
            "Runtime",
            "Cooldown",
            "Slots",
            "Details",
            "",
        ])
        self.acc_table.verticalHeader().setVisible(False)
        self.acc_table.verticalHeader().setDefaultSectionSize(56)  # Taller rows
        self.acc_table.setAlternatingRowColors(False)
        self.acc_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.acc_table.setSelectionMode(QTableWidget.SingleSelection)
        self.acc_table.setShowGrid(False)  # No grid lines — cleaner look
        self.acc_table.setFocusPolicy(Qt.NoFocus)
        self.acc_table.setStyleSheet(
            """
            QTableWidget {
                background: #0F172A;
                border: 1px solid #1E293B;
                border-radius: 10px;
                color: #F1F5F9;
                font-size: 13px;
                outline: none;
                gridline-color: transparent;
            }
            QTableWidget::viewport {
                background: #0F172A;
            }
            QHeaderView::section {
                background: #0F172A;
                color: #64748B;
                font-size: 11px;
                font-weight: 600;
                text-transform: uppercase;
                letter-spacing: 0.5px;
                border: none;
                border-bottom: 1px solid #1E293B;
                padding: 12px 14px;
            }
            QTableWidget::item {
                background: transparent;
                border: none;
                border-bottom: 1px solid #1E293B;
                color: #F1F5F9;
                padding: 0;
            }
            QTableWidget::item:selected {
                background: transparent;
                color: #F1F5F9;
            }
            QScrollBar:vertical {
                background: transparent;
                width: 10px;
                margin: 4px 2px 4px 0;
            }
            QScrollBar::handle:vertical {
                background: #334155;
                border-radius: 5px;
                min-height: 30px;
            }
            QScrollBar::handle:vertical:hover {
                background: #475569;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0;
            }
            """
        )
        acc_header = self.acc_table.horizontalHeader()
        acc_header.setStretchLastSection(False)
        acc_header.setSectionResizeMode(0, QHeaderView.Fixed)     # ID
        acc_header.setSectionResizeMode(1, QHeaderView.Stretch)   # Account — takes ALL remaining space
        acc_header.setSectionResizeMode(2, QHeaderView.Fixed)     # Proxy
        acc_header.setSectionResizeMode(3, QHeaderView.Fixed)     # Status
        acc_header.setSectionResizeMode(4, QHeaderView.Fixed)     # Session
        acc_header.setSectionResizeMode(5, QHeaderView.Fixed)     # Runtime
        acc_header.setSectionResizeMode(6, QHeaderView.Fixed)     # Cooldown
        acc_header.setSectionResizeMode(7, QHeaderView.Fixed)     # Slots
        acc_header.setSectionResizeMode(8, QHeaderView.Fixed)     # Details (fixed not stretch)
        acc_header.setSectionResizeMode(9, QHeaderView.Fixed)     # Actions
        # Account column gets MINIMUM 280px — enough for long emails like megagaurienterprises@gmail.com (~260px at 13px font)
        acc_header.setMinimumSectionSize(48)
        self.acc_table.setColumnWidth(0, 48)    # ID
        self.acc_table.setColumnWidth(1, 300)   # Account — initial 300 (stretch will expand)
        self.acc_table.setColumnWidth(2, 150)   # Proxy
        self.acc_table.setColumnWidth(3, 110)   # Status
        self.acc_table.setColumnWidth(4, 90)    # Session
        self.acc_table.setColumnWidth(5, 90)    # Runtime
        self.acc_table.setColumnWidth(6, 100)   # Cooldown
        self.acc_table.setColumnWidth(7, 70)    # Slots
        self.acc_table.setColumnWidth(8, 160)   # Details
        self.acc_table.setColumnWidth(9, 60)    # Actions
        # Prevent text eliding in the table itself (separate from cell widgets)
        self.acc_table.setTextElideMode(Qt.ElideNone)
        self._configure_table_scrolling(self.acc_table)
        layout.addWidget(self.acc_table)
        
        bottom_layout = QHBoxLayout()
        self.btn_refresh_accs = QPushButton("Refresh List")
        self.btn_refresh_accs.setProperty("role", "secondary")
        self.btn_refresh_accs.clicked.connect(self.refresh_accounts)

        self.btn_delete_acc = QPushButton("Delete Selected")
        self.btn_delete_acc.setProperty("role", "subtleDanger")
        self.btn_delete_acc.clicked.connect(self.delete_selected_account)
        
        bottom_layout.addWidget(self.btn_refresh_accs)
        bottom_layout.addStretch()
        bottom_layout.addWidget(self.btn_delete_acc)
        layout.addLayout(bottom_layout)
        
        self.load_accounts()
        
    def setup_failed_jobs(self):
        root = QVBoxLayout(self.tab_failed_jobs)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(14)

        # ── Page header card (matches Settings page style) ─────────
        header_wrap = QFrame()
        header_wrap.setObjectName("settingsHeader")
        header_layout = QHBoxLayout(header_wrap)
        header_layout.setContentsMargins(22, 18, 22, 18)
        header_layout.setSpacing(14)

        header_text_col = QVBoxLayout()
        header_text_col.setSpacing(4)
        header_title = QLabel("Failed Jobs")
        header_title.setObjectName("settingsPageTitle")
        header_subtitle = QLabel(
            "Review, edit, and retry jobs that failed during generation. "
            "Edit prompts directly in the table before retrying."
        )
        header_subtitle.setObjectName("settingsPageSubtitle")
        header_subtitle.setWordWrap(True)
        header_text_col.addWidget(header_title)
        header_text_col.addWidget(header_subtitle)
        header_layout.addLayout(header_text_col, 1)

        self.chk_select_all_failed = QCheckBox("Select All")
        self.chk_select_all_failed.clicked.connect(self._toggle_select_all_failed)
        header_layout.addWidget(self.chk_select_all_failed, 0, Qt.AlignRight)
        root.addWidget(header_wrap)

        # ── Table card ─────────────────────────────────────────────
        table_card = QFrame()
        table_card.setObjectName("failedJobsCard")
        table_card_layout = QVBoxLayout(table_card)
        table_card_layout.setContentsMargins(2, 2, 2, 2)
        table_card_layout.setSpacing(0)

        self.failed_table = QTableWidget(0, 7)
        self.failed_table.setObjectName("failedJobsTable")
        self.failed_table.setHorizontalHeaderLabels(
            ["✓", "#", "Prompt", "Type", "Error Reason", "Original Prompt", "Status"]
        )
        self.failed_table.verticalHeader().setVisible(False)
        self.failed_table.verticalHeader().setDefaultSectionSize(48)
        # Disable alternating row colors so per-item setBackground
        # (status badges, moderation tints) paints cleanly without
        # the QSS alternate-background fighting it.
        self.failed_table.setAlternatingRowColors(False)
        self.failed_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.failed_table.setSelectionMode(QTableWidget.ExtendedSelection)
        self.failed_table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.failed_table.setShowGrid(False)
        self.failed_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.failed_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.failed_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.failed_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.failed_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.failed_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self.failed_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeToContents)
        self.failed_table.horizontalHeader().setHighlightSections(False)
        self.failed_table.itemSelectionChanged.connect(self._update_failed_jobs_actions)
        self.failed_table.itemChanged.connect(self._on_failed_table_item_changed)
        self._configure_table_scrolling(self.failed_table)

        # Empty-state overlay — shown when table has 0 rows
        self.failed_empty_overlay = QLabel(
            "No failed jobs\n\nWhen a job fails, it'll show up here so you can edit and retry."
        )
        self.failed_empty_overlay.setObjectName("failedJobsEmpty")
        self.failed_empty_overlay.setAlignment(Qt.AlignCenter)
        self.failed_empty_overlay.setWordWrap(True)

        stack_wrap = QWidget()
        stack_layout = QVBoxLayout(stack_wrap)
        stack_layout.setContentsMargins(0, 0, 0, 0)
        stack_layout.setSpacing(0)
        stack_layout.addWidget(self.failed_table)
        stack_layout.addWidget(self.failed_empty_overlay)
        table_card_layout.addWidget(stack_wrap)
        root.addWidget(table_card, 1)

        # ── Bottom action bar ──────────────────────────────────────
        actions_wrap = QFrame()
        actions_wrap.setObjectName("failedJobsActionsBar")
        actions_layout = QHBoxLayout(actions_wrap)
        actions_layout.setContentsMargins(22, 14, 22, 14)
        actions_layout.setSpacing(10)

        _RefreshCls = PushButton if _FLUENT_UI_AVAILABLE else QPushButton
        self.btn_refresh_failed = _RefreshCls()
        self.btn_refresh_failed.setText("Refresh List")
        if _FLUENT_UI_AVAILABLE:
            try:
                self.btn_refresh_failed.setIcon(FluentIcon.SYNC)
            except Exception:
                pass
        else:
            self.btn_refresh_failed.setProperty("role", "secondary")
        self.btn_refresh_failed.setFixedHeight(36)
        # Wrap in lambda — otherwise clicked(bool) payload is passed
        # to load_failed_jobs as `jobs=False`, which wipes the table.
        self.btn_refresh_failed.clicked.connect(lambda: self.load_failed_jobs())

        _RetrySelCls = PrimaryPushButton if _FLUENT_UI_AVAILABLE else QPushButton
        self.btn_requeue_selected = _RetrySelCls()
        self.btn_requeue_selected.setText("Retry Selected")
        if _FLUENT_UI_AVAILABLE:
            try:
                self.btn_requeue_selected.setIcon(FluentIcon.PLAY)
            except Exception:
                pass
        else:
            self.btn_requeue_selected.setProperty("role", "warning")
        self.btn_requeue_selected.setFixedHeight(36)
        self.btn_requeue_selected.setMinimumWidth(150)
        self.btn_requeue_selected.clicked.connect(self._retry_selected_failed)

        _RetryAllCls = PushButton if _FLUENT_UI_AVAILABLE else QPushButton
        self.btn_retry_all_failed = _RetryAllCls()
        self.btn_retry_all_failed.setText("Retry All Failed")
        if _FLUENT_UI_AVAILABLE:
            try:
                self.btn_retry_all_failed.setIcon(FluentIcon.UPDATE)
            except Exception:
                pass
        else:
            self.btn_retry_all_failed.setProperty("role", "warning")
        self.btn_retry_all_failed.setFixedHeight(36)
        self.btn_retry_all_failed.setMinimumWidth(150)
        self.btn_retry_all_failed.clicked.connect(self._retry_all_failed)

        _CopyCls = PushButton if _FLUENT_UI_AVAILABLE else QPushButton
        self.btn_copy_failed = _CopyCls()
        self.btn_copy_failed.setText("Copy All Prompts")
        if _FLUENT_UI_AVAILABLE:
            try:
                self.btn_copy_failed.setIcon(FluentIcon.COPY)
            except Exception:
                pass
        else:
            self.btn_copy_failed.setProperty("role", "secondary")
        self.btn_copy_failed.setFixedHeight(36)
        self.btn_copy_failed.setToolTip(
            "Copy every failed prompt to clipboard (one per line).\n"
            "Paste into your AI tool, rewrite them, then use Paste Rewritten\n"
            "to apply the rewrites back in order."
        )
        self.btn_copy_failed.clicked.connect(self.copy_failed_prompts)

        _PasteCls = PushButton if _FLUENT_UI_AVAILABLE else QPushButton
        self.btn_paste_failed = _PasteCls()
        self.btn_paste_failed.setText("Paste Rewritten")
        if _FLUENT_UI_AVAILABLE:
            try:
                self.btn_paste_failed.setIcon(FluentIcon.PASTE)
            except Exception:
                pass
        else:
            self.btn_paste_failed.setProperty("role", "secondary")
        self.btn_paste_failed.setFixedHeight(36)
        self.btn_paste_failed.setToolTip(
            "Read rewritten prompts from clipboard (one per line) and\n"
            "apply them to failed jobs in order. The first line replaces\n"
            "the first failed job's prompt, the second line the second, etc.\n"
            "Count must match the number of failed jobs."
        )
        self.btn_paste_failed.clicked.connect(self.paste_rewritten_failed_prompts)

        # Clear Failed: stock QPushButton with red override (same
        # pattern as Pause/Stop/Clean Profiles — Fluent PushButton
        # conflicts with color overrides).
        self.btn_clear_failed = QPushButton("Clear Failed")
        self.btn_clear_failed.setStyleSheet(
            "QPushButton { background-color: #DC2626; color: white; padding: 8px 18px; "
            "border-radius: 7px; font-weight: 700; border: none; min-height: 36px; "
            "font-size: 12px; } "
            "QPushButton:hover { background-color: #EF4444; } "
            "QPushButton:pressed { background-color: #B91C1C; } "
            "QPushButton:disabled { background-color: #334155; color: #64748B; }"
        )
        self.btn_clear_failed.setCursor(Qt.PointingHandCursor)
        self.btn_clear_failed.clicked.connect(self.clear_failed_jobs_list)

        actions_layout.addWidget(self.btn_refresh_failed)
        actions_layout.addStretch()
        actions_layout.addWidget(self.btn_copy_failed)
        actions_layout.addWidget(self.btn_paste_failed)
        actions_layout.addWidget(self.btn_requeue_selected)
        actions_layout.addWidget(self.btn_retry_all_failed)
        actions_layout.addWidget(self.btn_clear_failed)
        root.addWidget(actions_wrap)

        self.load_failed_jobs()
        self._update_failed_empty_state()

    # ──────────────────────────────────────────────────────────────────
    # Settings page helpers — professional card/form layout building
    # ──────────────────────────────────────────────────────────────────
    def _settings_card(self, title, subtitle=""):
        """Create a titled card container for a group of related settings.
        Returns (card, form) where `form` is the QFormLayout to add rows to.
        """
        card = QGroupBox()
        card.setObjectName("settingsCard")
        outer = QVBoxLayout(card)
        outer.setContentsMargins(22, 18, 22, 18)
        outer.setSpacing(6)

        title_lbl = QLabel(str(title))
        title_lbl.setObjectName("settingsCardTitle")
        outer.addWidget(title_lbl)
        if subtitle:
            sub_lbl = QLabel(str(subtitle))
            sub_lbl.setObjectName("settingsCardSubtitle")
            sub_lbl.setWordWrap(True)
            outer.addWidget(sub_lbl)

        divider = QFrame()
        divider.setObjectName("settingsCardDivider")
        divider.setFrameShape(QFrame.HLine)
        divider.setFixedHeight(1)
        outer.addWidget(divider)
        outer.addSpacing(8)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(20)
        form.setVerticalSpacing(12)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        form.setFormAlignment(Qt.AlignTop | Qt.AlignLeft)
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        outer.addLayout(form)
        return card, form

    def _settings_label(self, text, hint=None):
        """Build a QLabel with settings-style formatting + optional tooltip."""
        lbl = QLabel(str(text))
        lbl.setObjectName("settingsRowLabel")
        if hint:
            lbl.setToolTip(str(hint))
        return lbl

    def _settings_range_row(self, spin_min, spin_max, suffix=""):
        """Wrap two spinboxes (min + 'to' + max) into a single row widget."""
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(8)
        row_layout.addWidget(spin_min)
        sep = QLabel("to")
        sep.setObjectName("settingsRowSeparator")
        row_layout.addWidget(sep)
        row_layout.addWidget(spin_max)
        if suffix:
            suffix_lbl = QLabel(str(suffix))
            suffix_lbl.setObjectName("settingsRowSeparator")
            row_layout.addWidget(suffix_lbl)
        row_layout.addStretch()
        return row

    def setup_settings(self):
        # Outer scroll wrapper so long settings lists scroll cleanly on
        # any screen size.
        root = QVBoxLayout(self.tab_settings)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        settings_scroll = QScrollArea()
        settings_scroll.setObjectName("settingsScrollArea")
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setFrameShape(QFrame.NoFrame)
        settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        scroll_content = QWidget()
        scroll_content.setObjectName("settingsScrollContent")
        layout = QVBoxLayout(scroll_content)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        # ── Page header ───────────────────────────────────────────
        header_wrap = QFrame()
        header_wrap.setObjectName("settingsHeader")
        header_layout = QVBoxLayout(header_wrap)
        header_layout.setContentsMargins(22, 18, 22, 18)
        header_layout.setSpacing(4)
        header_title = QLabel("Settings")
        header_title.setObjectName("settingsPageTitle")
        header_subtitle = QLabel(
            "Tune automation behavior, browser mode, stealth, and output location. "
            "Changes are saved when you click Save Settings below."
        )
        header_subtitle.setObjectName("settingsPageSubtitle")
        header_subtitle.setWordWrap(True)
        header_layout.addWidget(header_title)
        header_layout.addWidget(header_subtitle)
        layout.addWidget(header_wrap)

        # ═══════════════════════════════════════════════════════════
        # SECTION 1 — Performance & Parallelism
        # ═══════════════════════════════════════════════════════════
        perf_card, perf_form = self._settings_card(
            "Performance & Parallelism",
            "Control how many concurrent jobs run per account and how they're paced.",
        )

        self.spin_slots_per_account = QSpinBox()
        self.spin_slots_per_account.setRange(1, 40)
        self.spin_slots_per_account.setValue(max(1, min(40, get_int_setting("slots_per_account", 5))))
        self.spin_slots_per_account.setToolTip(
            "How many generation jobs run in parallel per account. More slots = more RAM and CPU usage."
        )
        perf_form.addRow(
            self._settings_label("Worker Slots Per Account"),
            self.spin_slots_per_account,
        )

        self.spin_same_account_stagger = QDoubleSpinBox()
        self.spin_same_account_stagger.setRange(0.0, 60.0)
        self.spin_same_account_stagger.setDecimals(1)
        self.spin_same_account_stagger.setSingleStep(0.1)
        self.spin_same_account_stagger.setValue(
            max(0.0, min(60.0, get_float_setting("same_account_stagger_seconds", 1.0)))
        )
        self.spin_same_account_stagger.setToolTip(
            "Minimum delay between consecutive dispatches on the same account. "
            "Prevents burst rate-limits when parallel slots are enabled."
        )
        perf_form.addRow(
            self._settings_label("Same-Account Stagger (sec)"),
            self.spin_same_account_stagger,
        )

        self.spin_global_stagger_min = QDoubleSpinBox()
        self.spin_global_stagger_min.setRange(0.0, 60.0)
        self.spin_global_stagger_min.setDecimals(1)
        self.spin_global_stagger_min.setSingleStep(0.1)
        self.spin_global_stagger_min.setValue(
            max(0.0, min(60.0, get_float_setting("global_stagger_min_seconds", 0.3)))
        )
        self.spin_global_stagger_max = QDoubleSpinBox()
        self.spin_global_stagger_max.setRange(0.0, 120.0)
        self.spin_global_stagger_max.setDecimals(1)
        self.spin_global_stagger_max.setSingleStep(0.1)
        saved_gmax = get_float_setting("global_stagger_max_seconds", 0.6)
        self.spin_global_stagger_max.setValue(
            max(self.spin_global_stagger_min.value(), min(120.0, saved_gmax))
        )
        perf_form.addRow(
            self._settings_label("Global Stagger (sec)"),
            self._settings_range_row(self.spin_global_stagger_min, self.spin_global_stagger_max),
        )

        self.cmb_speed_profile = QComboBox()
        self.cmb_speed_profile.addItem("Slow Stable", "stable")
        self.cmb_speed_profile.addItem("Fast", "fast")
        saved_speed_profile = str(get_setting("speed_profile", "fast") or "fast").strip().lower()
        speed_index = self.cmb_speed_profile.findData(
            saved_speed_profile if saved_speed_profile in ("stable", "fast") else "fast"
        )
        if speed_index < 0:
            speed_index = 0
        self.cmb_speed_profile.setCurrentIndex(speed_index)
        perf_form.addRow(self._settings_label("Speed Profile"), self.cmb_speed_profile)

        self.chk_profile_clone = QCheckBox(
            "Enable profile cloning for extra slots (required for same-account parallel)"
        )
        self.chk_profile_clone.setChecked(get_bool_setting("enable_profile_clones", True))
        perf_form.addRow("", self.chk_profile_clone)

        perf_note = QLabel(
            "Tip: More slots = more RAM / CPU. If your system gets slow, reduce slots to 1–2."
        )
        perf_note.setObjectName("settingsHint")
        perf_note.setWordWrap(True)
        perf_form.addRow("", perf_note)
        layout.addWidget(perf_card)

        # ═══════════════════════════════════════════════════════════
        # SECTION 2 — Retry & Auto-Recovery
        # ═══════════════════════════════════════════════════════════
        retry_card, retry_form = self._settings_card(
            "Retry & Auto-Recovery",
            "Configure how the queue retries failed jobs and when it auto-refreshes stuck accounts.",
        )

        self.spin_recaptcha_cooldown = QSpinBox()
        self.spin_recaptcha_cooldown.setRange(5, 600)
        self.spin_recaptcha_cooldown.setValue(
            max(5, min(600, get_int_setting("recaptcha_account_cooldown_seconds", 15)))
        )
        self.spin_recaptcha_cooldown.setToolTip(
            "How long a slot waits after a reCAPTCHA block before retrying."
        )
        retry_form.addRow(
            self._settings_label("ReCAPTCHA Slot Cooldown (sec)"),
            self.spin_recaptcha_cooldown,
        )

        self.spin_max_retries = QSpinBox()
        self.spin_max_retries.setRange(0, 5)
        self.spin_max_retries.setValue(
            max(0, min(5, get_int_setting("max_retries", get_int_setting("max_auto_retries_per_job", 3))))
        )
        self.spin_max_retries.setToolTip(
            "How many times the queue retries a failed job before marking it as failed."
        )
        retry_form.addRow(self._settings_label("Max Retries Per Job"), self.spin_max_retries)

        self.spin_retry_base_delay = QSpinBox()
        self.spin_retry_base_delay.setRange(5, 300)
        self.spin_retry_base_delay.setValue(
            max(5, min(300, get_int_setting("retry_base_delay_seconds", get_int_setting("auto_retry_base_delay_seconds", 10))))
        )
        self.spin_retry_base_delay.setToolTip(
            "Base delay between retries (smart backoff multiplies this)."
        )
        retry_form.addRow(
            self._settings_label("Retry Base Delay (sec)"),
            self.spin_retry_base_delay,
        )

        self.spin_auto_refresh_after_jobs = QSpinBox()
        self.spin_auto_refresh_after_jobs.setRange(0, 10000)
        self.spin_auto_refresh_after_jobs.setValue(
            max(0, min(10000, get_int_setting("auto_refresh_after_jobs", 150)))
        )
        self.spin_auto_refresh_after_jobs.setToolTip(
            "0 disables auto-refresh. Otherwise, refresh account slots after every N successful jobs."
        )
        retry_form.addRow(
            self._settings_label("Auto-refresh After N Jobs"),
            self.spin_auto_refresh_after_jobs,
        )

        self.spin_restart_threshold = QSpinBox()
        self.spin_restart_threshold.setRange(0, 20)
        self.spin_restart_threshold.setValue(
            max(0, min(20, get_int_setting("auto_restart_recap_fail_threshold", 3)))
        )
        self.spin_restart_window = QSpinBox()
        self.spin_restart_window.setRange(5, 50)
        self.spin_restart_window.setValue(
            max(5, min(50, get_int_setting("auto_restart_recap_fail_window", 10)))
        )
        restart_row = QWidget()
        restart_row_l = QHBoxLayout(restart_row)
        restart_row_l.setContentsMargins(0, 0, 0, 0)
        restart_row_l.setSpacing(8)
        restart_row_l.addWidget(self.spin_restart_threshold)
        _sep1 = QLabel("in last")
        _sep1.setObjectName("settingsRowSeparator")
        restart_row_l.addWidget(_sep1)
        restart_row_l.addWidget(self.spin_restart_window)
        _sep2 = QLabel("attempts")
        _sep2.setObjectName("settingsRowSeparator")
        restart_row_l.addWidget(_sep2)
        restart_row_l.addStretch()
        retry_form.addRow(
            self._settings_label("Auto-restart After N reCAPTCHA Fails"),
            restart_row,
        )

        self.spin_restart_cooldown = QSpinBox()
        self.spin_restart_cooldown.setRange(10, 120)
        self.spin_restart_cooldown.setValue(
            max(10, min(120, get_int_setting("auto_restart_recap_cooldown_seconds", 30)))
        )
        retry_form.addRow(
            self._settings_label("Cooldown Before Auto-restart (sec)"),
            self.spin_restart_cooldown,
        )

        self.chk_api_captcha_submit_lock = QCheckBox(
            "Legacy API submit lane (disabled; stagger controls now handle pacing)"
        )
        self.chk_api_captcha_submit_lock.setChecked(False)
        self.chk_api_captcha_submit_lock.setEnabled(False)
        self.chk_api_captcha_submit_lock.setToolTip(
            "Submit-lane throttling has been removed. Same-account and global stagger settings now control pacing."
        )
        retry_form.addRow("", self.chk_api_captcha_submit_lock)
        layout.addWidget(retry_card)

        # ═══════════════════════════════════════════════════════════
        # SECTION 3 — Browser & Stealth
        # ═══════════════════════════════════════════════════════════
        browser_card, browser_form = self._settings_card(
            "Browser & Stealth",
            "Choose the browser engine used for generation and how it's displayed.",
        )

        self.cmb_image_execution_mode = QComboBox()
        self.cmb_image_execution_mode.addItem("API only (queue retries on failure)", "api_only")
        self.cmb_image_execution_mode.setCurrentIndex(0)
        self.cmb_image_execution_mode.setEnabled(False)
        self.cmb_image_execution_mode.setToolTip(
            "Generation uses API-only mode. Transient failures retry through the queue."
        )
        self.cmb_image_execution_mode.currentIndexChanged.connect(
            lambda _=None: self._update_runtime_badges()
        )
        browser_form.addRow(
            self._settings_label("Image Execution Mode"),
            self.cmb_image_execution_mode,
        )

        self.cmb_browser_mode = QComboBox()
        self.cmb_browser_mode.addItem("Headless (Playwright)", "headless")
        self.cmb_browser_mode.addItem("Visible (Playwright)", "visible")
        self.cmb_browser_mode.addItem("Real Chrome (CDP)", "real_chrome")
        self.cmb_browser_mode.addItem("CloakBrowser (Best Stealth)", "cloakbrowser")
        self.cmb_browser_mode.currentIndexChanged.connect(self._on_browser_mode_changed)
        saved_browser_mode = str(get_setting("browser_mode", "real_chrome") or "real_chrome").strip().lower()
        if saved_browser_mode == "playwright":
            saved_browser_mode = "visible"
        browser_mode_index = self.cmb_browser_mode.findData(saved_browser_mode)
        if browser_mode_index < 0:
            browser_mode_index = self.cmb_browser_mode.findData("cloakbrowser")
        self.cmb_browser_mode.setCurrentIndex(browser_mode_index)
        browser_form.addRow(self._settings_label("Browser Mode"), self.cmb_browser_mode)

        # Chrome Display (visible only when Real Chrome selected)
        self.cmb_chrome_display = QComboBox()
        self.cmb_chrome_display.addItem("Visible (window dikhega)", "visible")
        self.cmb_chrome_display.addItem("Headless (background me chalega)", "headless")
        saved_chrome_display = str(get_setting("chrome_display", "headless") or "headless").strip().lower()
        chrome_display_index = self.cmb_chrome_display.findData(saved_chrome_display)
        if chrome_display_index < 0:
            chrome_display_index = self.cmb_chrome_display.findData("headless")
        self.cmb_chrome_display.setCurrentIndex(chrome_display_index)
        self.lbl_chrome_display = self._settings_label("Chrome Display")
        browser_form.addRow(self.lbl_chrome_display, self.cmb_chrome_display)

        # Cloak Display (visible only when CloakBrowser selected)
        self.cmb_cloak_display = QComboBox()
        self.cmb_cloak_display.addItem("Headless", "headless")
        self.cmb_cloak_display.addItem("Visible", "visible")
        self.cmb_cloak_display.addItem("Stealth Visible (hidden but not headless)", "stealth_visible")
        saved_cloak_display = str(get_setting("cloak_display", "headless") or "headless").strip().lower()
        cloak_display_index = self.cmb_cloak_display.findData(saved_cloak_display)
        if cloak_display_index < 0:
            cloak_display_index = self.cmb_cloak_display.findData("headless")
        self.cmb_cloak_display.setCurrentIndex(cloak_display_index)
        self.lbl_cloak_display = self._settings_label("Cloak Display")
        browser_form.addRow(self.lbl_cloak_display, self.cmb_cloak_display)

        self.cmb_generation_mode = QComboBox()
        self.cmb_generation_mode.setObjectName("settingInput")
        self.cmb_generation_mode.setMinimumHeight(38)
        self.cmb_generation_mode.addItem("Browser per slot (stable)", "browser_per_slot")
        self.cmb_generation_mode.addItem("HTTP Shared (lowest RAM)", "http_shared")
        self.cmb_generation_mode.addItem("CDP Shared (low RAM — experimental)", "cdp_shared")
        self.cmb_generation_mode.addItem("Chrome Extension (Flow — best reCAPTCHA)", "chrome_extension")
        self.cmb_generation_mode.addItem(
            "Chrome Extension — Genspark (Nano Banana, unlimited Plus/Pro)",
            "chrome_extension_genspark",
        )
        self.cmb_generation_mode.addItem(
            "Chrome Extension — Grok Imagine (video only, SuperGrok)",
            "chrome_extension_grok",
        )
        self.cmb_generation_mode.addItem(
            "Chrome Extension — Dola (dola.com Seedance video)",
            "chrome_extension_dola",
        )
        self.cmb_generation_mode.addItem(
            "Playwright — Dola (dola.com Seedance, dedicated profiles, invisible, auto burn-recreate)",
            "playwright_dola",
        )
        self.cmb_generation_mode.setToolTip(
            "Browser per slot: Each slot opens its own browser (~300MB each). Proven stable.\n\n"
            "HTTP Shared: 1 browser per account for reCAPTCHA only.\n"
            "All API calls via shared page.evaluate(fetch()). ~300MB per account total.\n"
            "5 slots = ~300MB vs ~1.5GB. Fastest and most RAM efficient.\n\n"
            "CDP Shared: 1 CloakBrowser process per account, N contexts via CDP.\n"
            "Each context = independent cookies + session (~30MB each).\n"
            "20 slots = ~900MB total vs ~6GB. EXPERIMENTAL.\n\n"
            "Chrome Extension (Flow): Uses your REAL Chrome browser with extension.\n"
            "Best reCAPTCHA scores on labs.google/Flow. Zero CDP, zero automation markers.\n"
            "~50MB RAM. Requires Chrome Extension installed + Chrome open.\n\n"
            "Chrome Extension — Genspark: Drives genspark.ai directly through your logged-in\n"
            "Chrome tab. Plus plan ($24.99) gives UNLIMITED Nano Banana Pro 2K; Pro plan\n"
            "($249.99) gives UNLIMITED Nano Banana Pro 4K. No per-day caps, simpler cookie-\n"
            "based auth. Runs on a separate bridge (port 18925) so Flow is untouched.\n\n"
            "Chrome Extension — Grok Imagine: Drives grok.com/imagine directly through your\n"
            "logged-in Chrome tab. Video generation only (text→video or image→video). No\n"
            "reCAPTCHA, cookie-based auth. SuperGrok ($30/mo) ~100 videos/day, Premium\n"
            "$8/mo ~50/day, Premium+ $40/mo ~100/day. Settings saved under grok_* keys;\n"
            "runs on separate bridge (port 18926) so Flow + Genspark are untouched."
        )
        saved_gen_mode = str(get_setting("generation_mode", "browser_per_slot") or "browser_per_slot").strip().lower()
        gen_mode_idx = self.cmb_generation_mode.findData(saved_gen_mode)
        if gen_mode_idx < 0:
            gen_mode_idx = 0
        self.cmb_generation_mode.setCurrentIndex(gen_mode_idx)
        # Initialize the "last mode" tracker so the Genspark-exit handler
        # can detect a switch away on the very first user action — even
        # after an app restart where Genspark was the active mode.
        self._last_generation_mode = saved_gen_mode
        # Re-sync the video sub-tabs whenever generation mode flips, so
        # the Upscale/Resolution/Duration combos swap without needing a
        # restart. Only bound if the combo isn't already wired (guards
        # against double-connect on settings dialog re-open).
        try:
            self.cmb_generation_mode.currentIndexChanged.connect(
                lambda _=None: self._on_generation_mode_changed()
            )
        except Exception:
            pass
        browser_form.addRow(self._settings_label("Generation Mode"), self.cmb_generation_mode)

        # Flow account plan — Pro vs Ultra use different Veo model names
        # AND different paygate tiers. Sending the wrong combo returns
        # 403 PUBLIC_ERROR_MODEL_ACCESS_DENIED. Default Ultra (latest plan).
        self.cmb_flow_plan = QComboBox()
        self.cmb_flow_plan.setObjectName("settingInput")
        self.cmb_flow_plan.setMinimumHeight(38)
        self.cmb_flow_plan.addItem("Ultra (Google AI Ultra — recommended)", "ultra")
        self.cmb_flow_plan.addItem("Pro (Google AI Pro — older plan)", "pro")
        self.cmb_flow_plan.setToolTip(
            "Which Google AI plan the labs.google.com account is on.\n\n"
            "Ultra: uses veo_3_1_*_ultra model keys + PAYGATE_TIER_TWO.\n"
            "Pro:   uses veo_3_1_* model keys + PAYGATE_TIER_ONE.\n\n"
            "Wrong setting → 403 PUBLIC_ERROR_MODEL_ACCESS_DENIED on every\n"
            "video request. Free Gmail accounts have NO Veo access (image\n"
            "only) regardless of this setting."
        )
        saved_plan = str(get_setting("flow_account_plan", "ultra") or "ultra").strip().lower()
        plan_idx = self.cmb_flow_plan.findData(saved_plan)
        if plan_idx < 0:
            plan_idx = 0
        self.cmb_flow_plan.setCurrentIndex(plan_idx)
        browser_form.addRow(self._settings_label("Flow Account Plan"), self.cmb_flow_plan)
        # Flow exposes DIFFERENT tier options per plan (verified live):
        #   • Ultra → 5 tiers (Fast, Lite, Quality, Fast LP, Lite LP)
        #   • Pro   → 3 tiers (Fast, Lite, Quality only — no LP variants)
        # Reapply the filter whenever the user flips the plan so that the
        # quality dropdowns throughout the app always mirror what Flow
        # actually accepts. Also run it once now so the saved plan takes
        # effect on the dropdowns created earlier in __init__.
        self.cmb_flow_plan.currentIndexChanged.connect(
            lambda _=None: self._apply_plan_tier_filter()
        )
        self._apply_plan_tier_filter()

        # Note: Genspark Model and Quality dropdowns live on the
        # Image Generation tab (per-job control) rather than here.

        self.chk_random_fingerprint = QCheckBox("Random fingerprint per session (like GoLogin)")
        self.chk_random_fingerprint.setChecked(get_bool_setting("random_fingerprint_per_session", False))
        browser_form.addRow("", self.chk_random_fingerprint)

        # CloakBrowser version row (inside Browser card)
        self.cloak_update_widget = QWidget()
        cloak_update_layout = QHBoxLayout(self.cloak_update_widget)
        cloak_update_layout.setContentsMargins(0, 0, 0, 0)
        cloak_update_layout.setSpacing(10)
        self.lbl_cloak_version = QLabel("CloakBrowser: checking...")
        self.lbl_cloak_version.setObjectName("settingsInlineInfo")
        _CloakUpdateCls = PushButton if _FLUENT_UI_AVAILABLE else QPushButton
        self.btn_cloak_update = _CloakUpdateCls()
        self.btn_cloak_update.setText("Check for Updates")
        if _FLUENT_UI_AVAILABLE:
            try:
                self.btn_cloak_update.setIcon(FluentIcon.SYNC)
            except Exception:
                pass
        self.btn_cloak_update.setFixedWidth(200)
        self.btn_cloak_update.setFixedHeight(34)
        if not _FLUENT_UI_AVAILABLE:
            self.btn_cloak_update.setStyleSheet(
                "QPushButton { background-color: #334155; color: white; "
                "border: 1px solid #475569; border-radius: 4px; font-size: 12px; "
                "padding: 4px 12px; } "
                "QPushButton:hover { background-color: #475569; } "
                "QPushButton:disabled { background-color: #1E293B; color: #64748B; }"
            )
        self.btn_cloak_update.clicked.connect(self._on_cloak_update_clicked)
        cloak_update_layout.addWidget(self.lbl_cloak_version, 1)
        cloak_update_layout.addWidget(self.btn_cloak_update)
        browser_form.addRow(self._settings_label("CloakBrowser"), self.cloak_update_widget)

        # Download progress bar — hidden until an update download starts.
        # Updated live via CloakUpdateWorker.progress_changed signal which
        # is driven by a logging handler hooked into cloakbrowser.download.
        self.cloak_progress_wrap = QWidget()
        cloak_progress_layout = QHBoxLayout(self.cloak_progress_wrap)
        cloak_progress_layout.setContentsMargins(0, 4, 0, 0)
        cloak_progress_layout.setSpacing(10)
        self.cloak_progress_bar = QProgressBar()
        self.cloak_progress_bar.setObjectName("cloakDownloadProgress")
        self.cloak_progress_bar.setRange(0, 100)
        self.cloak_progress_bar.setValue(0)
        self.cloak_progress_bar.setTextVisible(True)
        self.cloak_progress_bar.setFormat("%p%")
        self.cloak_progress_bar.setFixedHeight(18)
        self.lbl_cloak_progress_text = QLabel("0 / 0 MB")
        self.lbl_cloak_progress_text.setObjectName("settingsInlineInfo")
        self.lbl_cloak_progress_text.setFixedWidth(90)
        cloak_progress_layout.addWidget(self.cloak_progress_bar, 1)
        cloak_progress_layout.addWidget(self.lbl_cloak_progress_text)
        self.cloak_progress_wrap.setVisible(False)
        browser_form.addRow("", self.cloak_progress_wrap)

        self.lbl_cloak_update_status = QLabel("")
        self.lbl_cloak_update_status.setObjectName("settingsHint")
        self.lbl_cloak_update_status.setVisible(False)
        browser_form.addRow("", self.lbl_cloak_update_status)
        layout.addWidget(browser_card)

        # ═══════════════════════════════════════════════════════════
        # SECTION 4 — Humanization & Warm-up
        # ═══════════════════════════════════════════════════════════
        warmup_card, warmup_form = self._settings_card(
            "Humanization & Warm-up",
            "Human-like pacing delays and cookie warm-up behavior to improve reCAPTCHA scores.",
        )

        self.spin_warmup_min = QDoubleSpinBox()
        self.spin_warmup_min.setRange(0.0, 10.0)
        self.spin_warmup_min.setDecimals(1)
        self.spin_warmup_min.setSingleStep(0.1)
        self.spin_warmup_min.setValue(
            max(0.0, min(10.0, get_float_setting("api_humanized_warmup_min_seconds", 0.2)))
        )
        self.spin_warmup_max = QDoubleSpinBox()
        self.spin_warmup_max.setRange(0.0, 10.0)
        self.spin_warmup_max.setDecimals(1)
        self.spin_warmup_max.setSingleStep(0.1)
        saved_warmup_max = get_float_setting("api_humanized_warmup_max_seconds", 0.4)
        self.spin_warmup_max.setValue(
            max(self.spin_warmup_min.value(), min(10.0, saved_warmup_max))
        )
        warmup_form.addRow(
            self._settings_label("Warmup Delay (sec)"),
            self._settings_range_row(self.spin_warmup_min, self.spin_warmup_max),
        )

        self.chk_cookie_warmup = QCheckBox(
            "Cookie warm-up on first login (heavy — 2 searches + 3-4 site visits)"
        )
        self.chk_cookie_warmup.setChecked(get_bool_setting("cookie_warmup", True))
        self.chk_cookie_warmup.setToolTip(
            "When enabled, performs a full cookie warm-up after first login:\n"
            "2 Google Searches + click results + 3-4 random site visits.\n"
            "Takes ~4-5 minutes. Improves reCAPTCHA score significantly.\n"
            "Only runs ONCE per account (won't repeat after first login)."
        )
        warmup_form.addRow("", self.chk_cookie_warmup)

        self.chk_light_warmup = QCheckBox(
            "Quick warm-up before each run (light — 1 search + 0-1 site visit)"
        )
        self.chk_light_warmup.setChecked(get_bool_setting("light_warmup", True))
        self.chk_light_warmup.setToolTip(
            "When enabled, performs a quick cookie refresh before each generation run:\n"
            "1 Google Search + click result + maybe 1 random site.\n"
            "Takes ~30-45 seconds. Keeps cookies fresh between runs."
        )
        warmup_form.addRow("", self.chk_light_warmup)
        layout.addWidget(warmup_card)

        # ═══════════════════════════════════════════════════════════
        # SECTION 5 — Output Folder
        # ═══════════════════════════════════════════════════════════
        output_card, output_form = self._settings_card(
            "Output Folder",
            "Where the app saves generated images and videos.",
        )

        output_row = QWidget()
        output_row_l = QHBoxLayout(output_row)
        output_row_l.setContentsMargins(0, 0, 0, 0)
        output_row_l.setSpacing(8)
        self.output_dir_input = QLineEdit()
        self.output_dir_input.setReadOnly(True)
        self.output_dir_input.setPlaceholderText("Choose where generated files should be saved")
        self.output_dir_input.setText(self._outputs_dir())
        output_row_l.addWidget(self.output_dir_input, 1)

        _OutputBrowseCls = PushButton if _FLUENT_UI_AVAILABLE else QPushButton
        self.btn_browse_output_dir = _OutputBrowseCls()
        self.btn_browse_output_dir.setText("Browse…")
        if _FLUENT_UI_AVAILABLE:
            try:
                self.btn_browse_output_dir.setIcon(FluentIcon.FOLDER)
            except Exception:
                pass
        else:
            self.btn_browse_output_dir.setProperty("role", "browse")
        self.btn_browse_output_dir.setFixedHeight(34)
        self.btn_browse_output_dir.clicked.connect(self._browse_output_directory)
        output_row_l.addWidget(self.btn_browse_output_dir)

        self.btn_reset_output_dir = _OutputBrowseCls()
        self.btn_reset_output_dir.setText("Reset")
        if _FLUENT_UI_AVAILABLE:
            try:
                self.btn_reset_output_dir.setIcon(FluentIcon.CANCEL)
            except Exception:
                pass
        else:
            self.btn_reset_output_dir.setProperty("role", "secondary")
        self.btn_reset_output_dir.setFixedHeight(34)
        self.btn_reset_output_dir.clicked.connect(self._reset_output_directory)
        output_row_l.addWidget(self.btn_reset_output_dir)
        output_form.addRow(self._settings_label("Folder"), output_row)
        layout.addWidget(output_card)

        # ═══════════════════════════════════════════════════════════
        # SECTION 6 — Action buttons (Save + Clean Profiles)
        # ═══════════════════════════════════════════════════════════
        actions_wrap = QFrame()
        actions_wrap.setObjectName("settingsActionsBar")
        actions_layout = QVBoxLayout(actions_wrap)
        actions_layout.setContentsMargins(22, 18, 22, 18)
        actions_layout.setSpacing(12)

        actions_title = QLabel("Save & Maintenance")
        actions_title.setObjectName("settingsCardTitle")
        actions_layout.addWidget(actions_title)
        actions_subtitle = QLabel(
            "Apply your changes, or run maintenance. Clean Profiles removes cache but keeps login cookies."
        )
        actions_subtitle.setObjectName("settingsCardSubtitle")
        actions_subtitle.setWordWrap(True)
        actions_layout.addWidget(actions_subtitle)

        actions_divider = QFrame()
        actions_divider.setObjectName("settingsCardDivider")
        actions_divider.setFrameShape(QFrame.HLine)
        actions_divider.setFixedHeight(1)
        actions_layout.addWidget(actions_divider)
        actions_layout.addSpacing(6)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        _SaveSettingsCls = PrimaryPushButton if _FLUENT_UI_AVAILABLE else QPushButton
        self.btn_save_settings = _SaveSettingsCls()
        self.btn_save_settings.setText("Save Settings")
        if _FLUENT_UI_AVAILABLE:
            try:
                self.btn_save_settings.setIcon(FluentIcon.SAVE)
            except Exception:
                pass
        else:
            self.btn_save_settings.setProperty("role", "primary")
        self.btn_save_settings.setFixedHeight(44)
        self.btn_save_settings.setMinimumWidth(220)
        self.btn_save_settings.clicked.connect(self.save_settings)
        btn_row.addWidget(self.btn_save_settings, 2)

        # Clean Profiles stays as stock QPushButton with red color
        # override (Fluent PushButton conflicts with bg color overrides).
        self.btn_clean_profiles = QPushButton("Clean All Browser Profiles")
        self.btn_clean_profiles.setToolTip(
            "Removes accumulated cache, service workers, and tracking data.\n"
            "Preserves login cookies and session.\n"
            "Run this if reCAPTCHA errors are increasing.\n"
            "Same effect as reinstalling but without losing accounts."
        )
        self.btn_clean_profiles.setStyleSheet(
            "QPushButton { background-color: #DC2626; color: white; padding: 10px 18px; "
            "border-radius: 7px; font-weight: 700; border: none; min-height: 44px; "
            "font-size: 14px; } "
            "QPushButton:hover { background-color: #EF4444; } "
            "QPushButton:pressed { background-color: #B91C1C; }"
        )
        self.btn_clean_profiles.setCursor(Qt.PointingHandCursor)
        self.btn_clean_profiles.clicked.connect(lambda: self._on_clean_profiles())
        btn_row.addWidget(self.btn_clean_profiles, 1)
        actions_layout.addLayout(btn_row)
        layout.addWidget(actions_wrap)

        layout.addStretch(1)

        settings_scroll.setWidget(scroll_content)
        root.addWidget(settings_scroll)

        self._cloak_update_worker = None
        self._auto_check_cloak_on_startup()
        self._on_browser_mode_changed(self.cmb_browser_mode.currentIndex())

    # ──────────────────────────────────────────────────────────────────
    # Phase 4A: Fluent dialog helpers.
    # These wrap qfluentwidgets.MessageBox with a QMessageBox-like API
    # so existing call sites can migrate with minimal change. They fall
    # back cleanly to QMessageBox when Fluent is unavailable.
    # ──────────────────────────────────────────────────────────────────
    def _fluent_confirm(self, title, content, confirm_label="Yes", cancel_label="No"):
        """Show a Yes/No confirmation dialog. Returns True if user confirmed."""
        if _FLUENT_UI_AVAILABLE and FluentMessageBox is not None:
            try:
                box = FluentMessageBox(str(title), str(content), self)
                box.yesButton.setText(str(confirm_label))
                box.cancelButton.setText(str(cancel_label))
                return bool(box.exec())
            except Exception:
                pass
        reply = QMessageBox.question(
            self,
            str(title),
            str(content),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return reply == QMessageBox.Yes

    def _fluent_notify(self, title, content, cancel_only=True):
        """Show an informational dialog with a single OK button."""
        if _FLUENT_UI_AVAILABLE and FluentMessageBox is not None:
            try:
                box = FluentMessageBox(str(title), str(content), self)
                box.yesButton.setText("OK")
                if cancel_only:
                    box.cancelButton.hide()
                return box.exec()
            except Exception:
                pass
        QMessageBox.information(self, str(title), str(content))
        return True

    # ──────────────────────────────────────────────────────────────────
    # Phase 5: InfoBar toast notifications.
    # Slide-in from top-right, auto-dismiss, colored variants. Replace
    # call sites that currently use QMessageBox.information (simple OK
    # popups) with these — non-blocking, more modern.
    # ──────────────────────────────────────────────────────────────────
    def _toast_success(self, title, content="", duration=3000):
        if _FLUENT_UI_AVAILABLE and InfoBar is not None:
            try:
                InfoBar.success(
                    title=str(title),
                    content=str(content),
                    orient=Qt.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP_RIGHT,
                    duration=int(duration),
                    parent=self,
                )
                return
            except Exception:
                pass

    def _toast_info(self, title, content="", duration=3000):
        if _FLUENT_UI_AVAILABLE and InfoBar is not None:
            try:
                InfoBar.info(
                    title=str(title),
                    content=str(content),
                    orient=Qt.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP_RIGHT,
                    duration=int(duration),
                    parent=self,
                )
                return
            except Exception:
                pass

    def _toast_warning(self, title, content="", duration=4000):
        if _FLUENT_UI_AVAILABLE and InfoBar is not None:
            try:
                InfoBar.warning(
                    title=str(title),
                    content=str(content),
                    orient=Qt.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP_RIGHT,
                    duration=int(duration),
                    parent=self,
                )
                return
            except Exception:
                pass

    def _toast_error(self, title, content="", duration=5000):
        if _FLUENT_UI_AVAILABLE and InfoBar is not None:
            try:
                InfoBar.error(
                    title=str(title),
                    content=str(content),
                    orient=Qt.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP_RIGHT,
                    duration=int(duration),
                    parent=self,
                )
                return
            except Exception:
                pass

    def _install_wheel_filters(self):
        """Walk the widget tree and install our wheel event filter on
        every QAbstractSpinBox and QComboBox. Also install on the
        inner QLineEdit of each spinbox so wheel events hitting the
        edit field are also caught."""
        try:
            # Spin boxes (both Fluent and stock variants)
            for sb in self.findChildren(QAbstractSpinBox):
                try:
                    sb.installEventFilter(self._wheel_filter)
                    le = sb.lineEdit() if hasattr(sb, "lineEdit") else None
                    if le is not None:
                        le.installEventFilter(self._wheel_filter)
                except Exception:
                    pass
            # Combo boxes
            for cb in self.findChildren(QComboBox):
                try:
                    cb.installEventFilter(self._wheel_filter)
                    # ComboBox uses an internal view — install on the
                    # widget itself, Qt will filter before view gets it.
                except Exception:
                    pass
        except Exception:
            pass

    def _apply_modern_theme(self):
        stylesheet = """
            QMainWindow, QWidget {
                background-color: #0F172A;
                color: #F8FAFC;
                font-family: "SF Pro Display", "Segoe UI", "Inter", "Helvetica Neue", "Arial";
                font-size: 13px;
            }
            QMainWindow#mainWindow {
                background: #0F172A;
            }
            QFrame#sidebarNav {
                background: #0B1120;
                border: none;
            }
            /* Phase 6: Match title + stats wrap backgrounds to the
               sidebar frame so Fluent NavigationInterface sits flush
               between two uniformly-colored areas. */
            QWidget#sidebarTitleWrap {
                background: #0B1120;
            }
            QWidget#sidebarStatsWrap {
                background: #0B1120;
            }
            QLabel#sidebarTitle {
                color: #F8FAFC;
                font-size: 19px;
                font-weight: 800;
                padding: 4px 4px 2px 4px;
                background: transparent;
            }
            QLabel#sidebarByline {
                color: #64748B;
                font-size: 10px;
                font-weight: 500;
                font-style: italic;
                padding: 0px 4px 14px 6px;
                background: transparent;
                letter-spacing: 1px;
            }
            QFrame#sidebarDivider, QFrame#sidebarShellDivider {
                background: #1E293B;
                color: #1E293B;
                border: none;
            }
            QPushButton#sidebarNavButton {
                background: transparent;
                color: #94A3B8;
                text-align: left;
                font-size: 12px;
                font-weight: 600;
                padding: 9px 12px;
                border: none;
                border-radius: 8px;
            }
            QPushButton#sidebarNavButton:hover {
                background: #1E293B;
                color: #FFFFFF;
            }
            QPushButton#sidebarNavButton:checked {
                background: #2563EB;
                color: #FFFFFF;
            }
            /* Sidebar stats — mini card rows with colored dots */
            QLabel#sidebarStatsHeader {
                color: #64748B;
                font-size: 10px;
                font-weight: 800;
                letter-spacing: 1.5px;
                padding: 0 4px;
                background: transparent;
            }
            QFrame#sidebarStatRow {
                background: #0F172A;
                border: 1px solid #1E293B;
                border-radius: 8px;
            }
            QFrame#sidebarStatRow:hover {
                background: #1E293B;
                border: 1px solid #334155;
            }
            QFrame#sidebarStatRow[variant="failed"][hasFailures="true"] {
                background: #2A1020;
                border: 1px solid #7F1D1D;
            }
            QFrame#sidebarStatRow[variant="failed"][hasFailures="true"]:hover {
                background: #3A1525;
                border: 1px solid #B91C1C;
            }
            QLabel#sidebarStatLabel {
                color: #94A3B8;
                font-size: 11px;
                font-weight: 600;
                background: transparent;
            }
            QLabel#sidebarStatCount {
                color: #F8FAFC;
                font-size: 13px;
                font-weight: 800;
                background: transparent;
                min-width: 24px;
            }
            QLabel#sidebarStatCount[variant="pending"] { color: #CBD5E1; }
            QLabel#sidebarStatCount[variant="running"] { color: #60A5FA; }
            QLabel#sidebarStatCount[variant="done"] { color: #22C55E; }
            QLabel#sidebarStatCount[variant="failed"] { color: #94A3B8; }
            QLabel#sidebarStatCount[variant="failed"][hasFailures="true"] { color: #FCA5A5; }
            QFrame#sidebarStatsInnerDivider {
                background: #1E293B;
                color: #1E293B;
                border: none;
            }
            /* Legacy selectors kept for fallback (when Fluent unavailable) */
            QLabel#sidebarStat {
                color: #94A3B8;
                font-size: 11px;
                padding: 2px 4px;
                background: transparent;
            }
            QLabel#sidebarStatRunning {
                color: #60A5FA;
                font-size: 11px;
                padding: 2px 4px;
                background: transparent;
            }
            QLabel#sidebarStatDone {
                color: #22C55E;
                font-size: 11px;
                padding: 2px 4px;
                background: transparent;
            }
            QPushButton#sidebarFailedButton {
                background: transparent;
                color: #EF4444;
                text-align: left;
                font-size: 11px;
                font-weight: 600;
                padding: 4px;
                border: none;
                border-radius: 6px;
            }
            QPushButton#sidebarFailedButton:hover {
                background: #2D1B1B;
                color: #FCA5A5;
            }
            QPushButton#sidebarFailedButton[hasFailures="true"] {
                background: #2D1B1B;
                color: #FCA5A5;
            }
            QLabel#sidebarSession {
                color: #64748B;
                font-size: 10px;
                font-weight: 600;
                padding: 4px;
                background: transparent;
                letter-spacing: 0.3px;
            }
            QTabWidget#mainTabs {
                background: #0F172A;
            }
            QTabWidget#mainTabs::pane {
                border: none;
                background: #0F172A;
                margin-top: 0px;
            }
            QTabBar, QTabBar#mainAppTabBar {
                background: #0F172A;
            }
            QTabBar#mainAppTabBar {
                border: none;
            }
            QTabBar#mainAppTabBar::tab {
                background: #111827;
                color: #94A3B8;
                border: none;
                border-bottom: 2px solid transparent;
                padding: 10px 22px;
                margin-right: 4px;
                font-weight: 700;
                font-size: 13px;
            }
            QTabBar#mainAppTabBar::tab:selected {
                color: #3B82F6;
                background: #0F172A;
                border-bottom: 2px solid #3B82F6;
            }
            QTabBar#mainAppTabBar::tab:hover:!selected {
                color: #CBD5E1;
                background: #131C30;
            }
            QScrollArea, QScrollArea > QWidget > QWidget,
            QScrollArea#dashboardScroll, QWidget#dashboardContent {
                background: #0F172A;
                border: none;
            }
            QTabWidget::pane {
                border: 1px solid #1E293B;
                border-top: 1px solid #334155;
                border-radius: 10px;
                background: #111827;
                padding: 0px;
                margin-top: -1px;
            }
            /* Fluent Pivot-style tab bar — thicker underline, smooth hover.
               Active tab gets a prominent blue underline like Windows 11
               Settings navigation. */
            QTabBar::tab {
                padding: 12px 26px;
                font-weight: 700;
                font-size: 13px;
                color: #94A3B8;
                background: transparent;
                border: none;
                border-bottom: 3px solid transparent;
                margin-right: 6px;
            }
            QTabBar::tab:selected {
                color: #F8FAFC;
                border-bottom: 3px solid #2563EB;
            }
            QTabBar::tab:hover:!selected {
                color: #E2E8F0;
                border-bottom: 3px solid #1E3A5F;
            }
            QFrame#heroCard, QFrame#dashboardTopBar {
                background: #1E293B;
                border: 1px solid #334155;
                border-radius: 14px;
            }
            QLabel#heroTitle {
                color: #F8FAFC;
                font-size: 17px;
                font-weight: 800;
                letter-spacing: -0.4px;
            }
            QLabel#heroSubtitle {
                color: #94A3B8;
                font-size: 11px;
            }
            QLabel#metaBadge {
                background: #172033;
                color: #E2E8F0;
                border: 1px solid #334155;
                border-radius: 10px;
                padding: 7px 12px;
                font-weight: 700;
            }
            /* Fluent-style Account Health overview card */
            QFrame#accountOverviewCard {
                background: #111827;
                border: 1px solid #1E293B;
                border-top: 1px solid #334155;
                border-radius: 10px;
            }
            QFrame#statCard {
                background: #1E293B;
                border: 1px solid #334155;
                border-radius: 14px;
            }
            QFrame#statCard:hover {
                border: 1px solid #3B82F6;
                background: #243247;
            }
            QScrollArea#liveGridScroll, QWidget#liveGridContainer {
                background: transparent;
                border: none;
            }
            QFrame#liveJobCard {
                background: #1E293B;
                border: 1px solid #334155;
                border-radius: 12px;
            }
            QFrame#liveJobCard:hover {
                border-color: #3B82F6;
                background: #243247;
            }
            QLabel#liveJobNumber {
                color: #94A3B8;
                font-size: 11px;
                font-weight: 700;
            }
            QLabel#liveJobPrompt {
                color: #E2E8F0;
                font-size: 12px;
                font-weight: 600;
            }
            QLabel#liveJobMeta {
                color: #64748B;
                font-size: 11px;
            }
            QProgressBar#liveOverallProgress {
                background: #1E293B;
                border: 1px solid #334155;
                border-radius: 8px;
                text-align: center;
                color: #F8FAFC;
                font-weight: 700;
                padding: 1px;
            }
            QProgressBar#liveOverallProgress::chunk {
                background: #3B82F6;
                border-radius: 7px;
            }
            /* ─────────── Settings page — professional card layout ─────────── */
            QScrollArea#settingsScrollArea, QWidget#settingsScrollContent {
                background: #0F172A;
                border: none;
            }
            QFrame#settingsHeader {
                background: #111827;
                border: 1px solid #1E293B;
                border-left: 3px solid #2563EB;
                border-radius: 10px;
            }
            QLabel#settingsPageTitle {
                color: #F8FAFC;
                font-size: 22px;
                font-weight: 800;
                letter-spacing: -0.3px;
                background: transparent;
            }
            QLabel#settingsPageSubtitle {
                color: #94A3B8;
                font-size: 13px;
                font-weight: 500;
                background: transparent;
            }
            QGroupBox#settingsCard, QFrame#settingsActionsBar {
                background: #111827;
                border: 1px solid #1E293B;
                border-top: 1px solid #334155;
                border-radius: 10px;
                margin-top: 0px;
                padding: 0px;
            }
            QGroupBox#settingsCard::title {
                padding: 0px;
                margin: 0px;
                color: transparent;
                background: transparent;
            }
            QLabel#settingsCardTitle {
                color: #F8FAFC;
                font-size: 15px;
                font-weight: 800;
                letter-spacing: 0.2px;
                background: transparent;
            }
            QLabel#settingsCardSubtitle {
                color: #64748B;
                font-size: 12px;
                font-weight: 500;
                background: transparent;
            }
            QFrame#settingsCardDivider {
                background: #1E293B;
                color: #1E293B;
                border: none;
            }
            QLabel#settingsRowLabel {
                color: #CBD5E1;
                font-size: 13px;
                font-weight: 600;
                background: transparent;
                padding-right: 8px;
                min-width: 200px;
            }
            QLabel#settingsRowSeparator {
                color: #64748B;
                font-size: 12px;
                font-weight: 500;
                background: transparent;
                padding: 0 4px;
            }
            QLabel#settingsHint {
                color: #64748B;
                font-size: 11px;
                font-weight: 500;
                background: transparent;
                padding-top: 4px;
            }
            QLabel#settingsInlineInfo {
                color: #60A5FA;
                font-size: 12px;
                font-weight: 600;
                background: transparent;
            }
            /* ─────────── Failed Jobs page ─────────── */
            QFrame#failedJobsCard {
                background: #111827;
                border: 1px solid #1E293B;
                border-top: 1px solid #334155;
                border-radius: 10px;
            }
            QTableWidget#failedJobsTable {
                background: transparent;
                border: none;
                color: #E2E8F0;
                font-size: 12px;
                gridline-color: #1E293B;
                selection-background-color: #1E3A5F;
                selection-color: #F8FAFC;
            }
            QTableWidget#failedJobsTable::item {
                padding: 10px 8px;
                border-bottom: 1px solid #1E293B;
            }
            QTableWidget#failedJobsTable::item:selected {
                background: #1E3A5F;
                color: #F8FAFC;
            }
            QTableWidget#failedJobsTable QHeaderView::section {
                background: #0F172A;
                color: #64748B;
                font-size: 10px;
                font-weight: 800;
                text-transform: uppercase;
                letter-spacing: 0.8px;
                padding: 14px 10px;
                border: none;
                border-bottom: 2px solid #1E293B;
            }
            /* In-place cell editor — when user double-clicks a prompt
               cell to edit, a QLineEdit is created by Qt. Style it so
               the text is visible against the dark row background. */
            QTableWidget#failedJobsTable QLineEdit {
                background: #0B1220;
                color: #F8FAFC;
                border: 2px solid #3B82F6;
                border-radius: 4px;
                padding: 6px 8px;
                font-size: 12px;
                selection-background-color: #2563EB;
                selection-color: #FFFFFF;
            }
            QLabel#failedJobsEmpty {
                color: #475569;
                font-size: 14px;
                font-weight: 600;
                padding: 80px 40px;
                background: transparent;
            }
            QFrame#failedJobsActionsBar {
                background: #111827;
                border: 1px solid #1E293B;
                border-top: 1px solid #334155;
                border-radius: 10px;
            }
            /* CloakBrowser download progress bar — visible during updates */
            QProgressBar#cloakDownloadProgress {
                background: #0F172A;
                border: 1px solid #334155;
                border-radius: 4px;
                text-align: center;
                color: #F8FAFC;
                font-weight: 700;
                font-size: 10px;
                padding: 1px;
            }
            QProgressBar#cloakDownloadProgress::chunk {
                background: #2563EB;
                border-radius: 3px;
            }
            /* ─────────── Checkboxes — bigger, high-contrast, Fluent ───────────
               Visible in any card background. 20x20 indicator with
               clear borders. Checked state uses theme blue + white
               checkmark via unicode. */
            QCheckBox {
                color: #E2E8F0;
                font-size: 13px;
                font-weight: 500;
                spacing: 10px;
                padding: 6px 0;
                background: transparent;
            }
            QCheckBox:disabled {
                color: #64748B;
            }
            QCheckBox::indicator {
                width: 20px;
                height: 20px;
                border-radius: 5px;
            }
            QCheckBox::indicator:unchecked {
                background: #0F172A;
                border: 2px solid #475569;
            }
            QCheckBox::indicator:unchecked:hover {
                border: 2px solid #60A5FA;
                background: #1E293B;
            }
            QCheckBox::indicator:checked {
                background: #2563EB;
                border: 2px solid #60A5FA;
            }
            QCheckBox::indicator:checked:hover {
                background: #3B82F6;
                border: 2px solid #60A5FA;
            }
            QCheckBox::indicator:disabled {
                background: #1E293B;
                border: 2px solid #334155;
            }
            QLabel#accountOverviewTitle {
                color: #F8FAFC;
                font-size: 17px;
                font-weight: 800;
                letter-spacing: 0.2px;
                background: transparent;
            }
            /* Color-variant metric chips for Account Health panel.
               variant="neutral" / "muted" / "info" / "success" / "warning"
               Each chip gets a distinct accent for quick visual scanning. */
            QLabel#accountMetricChip {
                background: #0F172A;
                border: 1px solid #1E293B;
                border-radius: 8px;
                color: #CBD5E1;
                font-weight: 700;
                font-size: 12px;
                padding: 8px 14px;
            }
            QLabel#accountMetricChip[variant="neutral"] {
                color: #E2E8F0;
                border: 1px solid #334155;
            }
            QLabel#accountMetricChip[variant="muted"] {
                color: #64748B;
                border: 1px solid #1E293B;
            }
            QLabel#accountMetricChip[variant="info"] {
                color: #60A5FA;
                border: 1px solid #1E3A5F;
                background: #0C1A33;
            }
            QLabel#accountMetricChip[variant="success"] {
                color: #34D399;
                border: 1px solid #14532D;
                background: #0B1F18;
            }
            QLabel#accountMetricChip[variant="warning"] {
                color: #FBBF24;
                border: 1px solid #78350F;
                background: #1F1608;
            }
            QLabel#statValue {
                background: transparent;
                border: none;
            }
            QLabel#statTitle {
                background: transparent;
                border: none;
            }
            /* Fluent-style card panels — darker background for subtle
               lift, accent-tinted border, prominent title bar. */
            QGroupBox, QGroupBox#dashboardPanel, QFrame#dashboardPanel {
                font-weight: 700;
                font-size: 14px;
                color: #F8FAFC;
                border: 1px solid #1E293B;
                border-top: 1px solid #334155;
                border-radius: 10px;
                margin-top: 14px;
                padding: 22px 16px 16px 16px;
                background: #111827;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 18px;
                padding: 2px 10px;
                color: #E2E8F0;
                background: #111827;
                font-size: 13px;
                font-weight: 700;
                letter-spacing: 0.3px;
            }
            QLabel {
                color: #94A3B8;
                font-size: 13px;
                background: transparent;
            }
            QLabel#settingLabel {
                color: #94A3B8;
                font-size: 13px;
                font-weight: 600;
                padding-right: 6px;
            }
            QLabel#tabSectionTitle {
                color: #F8FAFC;
                font-size: 15px;
                font-weight: 700;
                padding-bottom: 6px;
                border-bottom: 1px solid #334155;
                margin-bottom: 8px;
            }
            QLabel#settingHint {
                color: #64748B;
                font-size: 12px;
                font-weight: 500;
            }
            /* Input fields — includes QPlainTextEdit so pipeline
               Image/Video prompt boxes have visible borders. The
               color rule applies uniformly even if pasted rich text
               tries to override (works for rich text too). */
            QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QTableWidget {
                background: #0F172A;
                color: #F8FAFC;
                border: 1px solid #334155;
                border-radius: 8px;
                padding: 8px 10px;
                selection-background-color: #1E2D4A;
                selection-color: #FFFFFF;
            }
            QTextEdit, QPlainTextEdit {
                /* Force text color — wins over pasted rich-text
                   embedded colors. Qt QTextEdit honors this for
                   plain-text-only mode (setAcceptRichText=False). */
                color: #F8FAFC;
            }
            QLineEdit:hover, QTextEdit:hover, QPlainTextEdit:hover {
                border-color: #475569;
            }
            QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QSpinBox:focus {
                border-color: #3B82F6;
            }
            /* Fluent SpinBox / DoubleSpinBox ship with their own native
               styling — we do NOT override them. QComboBox stays stock
               (Fluent ComboBox broke Qt internals) but gets custom QSS
               with a real SVG chevron for visible dropdown arrow. */
            QComboBox {
                padding: 10px 14px;
                padding-right: 36px;
                border: 1px solid #1E293B;
                border-bottom: 2px solid #334155;
                border-radius: 7px;
                background: #111827;
                color: #F8FAFC;
                font-size: 13px;
                min-height: 24px;
                min-width: 110px;
            }
            QComboBox:hover {
                background: #1E293B;
                border-bottom: 2px solid #2563EB;
            }
            QComboBox:focus, QComboBox:on {
                border: 1px solid #2563EB;
                border-bottom: 2px solid #3B82F6;
                background: #1E293B;
            }
            QComboBox::drop-down {
                border: none;
                width: 32px;
                subcontrol-origin: padding;
                subcontrol-position: center right;
                background: transparent;
            }
            QComboBox::down-arrow {
                image: url(%CHEVRON%);
                width: 14px;
                height: 14px;
                margin-right: 10px;
            }
            QComboBox::down-arrow:on {
                /* When popup is open, flip chevron isn't possible in
                   QSS — keep same image. */
                image: url(%CHEVRON%);
            }
            QComboBox QAbstractItemView {
                background: #0F172A;
                color: #F8FAFC;
                border: 1px solid #334155;
                border-radius: 7px;
                selection-background-color: #2563EB;
                selection-color: white;
                padding: 6px;
                outline: 0;
            }
            QComboBox QAbstractItemView::item {
                padding: 8px 12px;
                border-radius: 5px;
                min-height: 24px;
            }
            QComboBox QAbstractItemView::item:hover {
                background: #1E293B;
            }
            QPushButton {
                padding: 8px 16px;
                border-radius: 8px;
                font-weight: 600;
                font-size: 13px;
                border: 1px solid transparent;
                background: #1E293B;
                color: #F8FAFC;
            }
            QPushButton:disabled {
                color: #64748B;
                border-color: #334155;
                background: #253246;
            }
            /* Fluent primary button — matches theme accent, rounded
               corners, clean Fluent feel. Used for Save Settings and
               other big primary actions. */
            QPushButton[role="primary"], QPushButton[role="primaryGradient"], QPushButton[role="startGradient"] {
                color: white;
                border: none;
                border-radius: 7px;
                background: #2563EB;
                padding: 12px 24px;
                font-size: 14px;
                font-weight: 700;
            }
            QPushButton[role="primary"]:hover, QPushButton[role="primaryGradient"]:hover, QPushButton[role="startGradient"]:hover {
                background: #3B82F6;
            }
            QPushButton[role="primary"]:pressed, QPushButton[role="primaryGradient"]:pressed {
                background: #1D4ED8;
            }
            QPushButton[role="outlineWarning"], QPushButton[role="warning"] {
                background: transparent;
                color: #F59E0B;
                border: 1px solid #F59E0B;
                padding: 12px 20px;
            }
            QPushButton[role="outlineWarning"]:hover, QPushButton[role="warning"]:hover {
                background: #1C1E2A;
            }
            QPushButton[role="outlineInfo"] {
                background: transparent;
                color: #3B82F6;
                border: 1px solid #3B82F6;
                padding: 12px 20px;
            }
            QPushButton[role="outlineInfo"]:hover {
                background: #172240;
            }
            QPushButton[role="outlineDanger"], QPushButton[role="subtleDanger"] {
                background: transparent;
                color: #EF4444;
                border: 1px solid #EF4444;
                padding: 12px 20px;
            }
            QPushButton[role="outlineDanger"]:hover, QPushButton[role="subtleDanger"]:hover {
                background: #1F1A2A;
            }
            QPushButton[role="ghost"], QPushButton[role="secondary"] {
                background: transparent;
                color: #94A3B8;
                border: 1px solid #475569;
            }
            QPushButton[role="ghost"]:hover, QPushButton[role="secondary"]:hover {
                background: #334155;
                color: #F8FAFC;
            }
            /* Fluent-styled browse button — clean solid border, no
               dashed look. Used for reference image selectors, start
               image, end image pickers. */
            QPushButton[role="browse"] {
                background: #0F172A;
                color: #60A5FA;
                border: 1px solid #1E293B;
                border-bottom: 2px solid #2563EB;
                border-radius: 7px;
                padding: 10px 18px;
                font-weight: 600;
            }
            QPushButton[role="browse"]:hover {
                background: #1E293B;
                color: #93C5FD;
                border-bottom: 2px solid #3B82F6;
            }
            /* Fluent-styled danger button — red with subtle bg tint,
               proper radius, no harsh border. Used for Clean All
               Browser Profiles + Clear Reference buttons. */
            QPushButton[role="danger"], QPushButton#refClearButton {
                background: #DC2626;
                color: #FFFFFF;
                border: none;
                border-radius: 7px;
                padding: 10px 18px;
                font-weight: 700;
            }
            QPushButton[role="danger"]:hover, QPushButton#refClearButton:hover {
                background: #EF4444;
            }
            QPushButton#refClearButton {
                min-width: 34px;
                max-width: 34px;
                padding: 0px;
            }
            QLabel#refStatusLabel {
                background: #131C30;
                border: 1px solid #3B82F6;
                border-radius: 10px;
                padding: 7px 12px;
                color: #93C5FD;
                font-size: 13px;
                font-weight: 700;
                min-height: 24px;
            }
            QFrame#referenceRow, QFrame#bulkPanel {
                background: #172033;
                border: 1px solid #334155;
                border-radius: 12px;
            }
            QFrame#tabSeparator {
                background: #334155;
                border: none;
                max-height: 1px;
            }
            QTextEdit#logsOutput {
                background: #0B0E14;
                color: #22C55E;
                font-family: "SF Mono", "JetBrains Mono", "Fira Code", "Menlo", "Consolas", monospace;
                font-size: 12px;
                border: 1px solid #1E293B;
                border-radius: 10px;
                padding: 10px;
            }
            QTableWidget {
                background: #1E293B;
                color: #F8FAFC;
                border: 1px solid #334155;
                border-radius: 8px;
                gridline-color: #334155;
                selection-background-color: #1E2D4A;
                alternate-background-color: #243247;
            }
            QTableWidget::item {
                padding: 6px 8px;
                border-bottom: 1px solid #1E293B;
            }
            QHeaderView::section {
                background: #0F172A;
                color: #94A3B8;
                font-weight: 700;
                font-size: 11px;
                padding: 8px;
                border: none;
                border-bottom: 1px solid #334155;
            }
            QSplitter::handle {
                background: #1E293B;
                border-radius: 3px;
                border: 1px solid #334155;
            }
            QScrollBar:vertical {
                background: transparent;
                width: 8px;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical {
                background: #475569;
                border-radius: 4px;
                min-height: 30px;
            }
            QScrollBar::handle:vertical:hover {
                background: #64748B;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            QMenu {
                background: #1E293B;
                color: #F8FAFC;
                border: 1px solid #334155;
                border-radius: 8px;
                padding: 6px;
            }
            QMenu::item {
                padding: 8px 12px;
                border-radius: 6px;
            }
            QMenu::item:selected {
                background: #1A2744;
            }
            """
        # Inject runtime-generated chevron SVG path into QSS
        stylesheet = stylesheet.replace("%CHEVRON%", _CHEVRON_PATH or "")
        self.setStyleSheet(stylesheet)

    def _set_status_item_style(self, item, status_text):
        status = str(status_text or "").strip().lower()
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        if status == "completed":
            item.setForeground(QColor("#22C55E"))
            item.setBackground(QColor(10, 31, 20))
        elif status == "running":
            item.setForeground(QColor("#3B82F6"))
            item.setBackground(QColor(11, 25, 47))
        elif status == "moderated":
            item.setForeground(QColor("#F59E0B"))
            item.setBackground(QColor(39, 24, 5))
        elif status == "failed":
            item.setForeground(QColor("#EF4444"))
            item.setBackground(QColor(44, 14, 14))
        elif status == "pending":
            item.setForeground(QColor("#94A3B8"))
            item.setBackground(QColor(22, 31, 47))
        else:
            item.setForeground(QColor("#94A3B8"))
            item.setBackground(QColor(30, 41, 59))

    def _set_account_runtime_item_style(self, item, runtime_text):
        runtime = str(runtime_text or "").strip().lower()
        if runtime == "running":
            item.setForeground(QColor("#22C55E"))
            item.setBackground(QColor(10, 31, 20))
        elif runtime == "cooldown":
            item.setForeground(QColor("#EF4444"))
            item.setBackground(QColor(44, 14, 14))
        elif runtime == "slot_cooldown":
            item.setForeground(QColor("#F59E0B"))
            item.setBackground(QColor(39, 24, 5))
        elif runtime == "ready":
            item.setForeground(QColor("#3B82F6"))
            item.setBackground(QColor(11, 25, 47))
        else:
            item.setForeground(QColor("#94A3B8"))
            item.setBackground(QColor(30, 41, 59))

    def _format_remaining(self, seconds):
        total = max(0, int(seconds))
        hours, rem = divmod(total, 3600)
        minutes, secs = divmod(rem, 60)
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    def _get_or_create_table_item(self, table, row, col, text=None):
        item = table.item(row, col)
        if item is None:
            item = QTableWidgetItem("" if text is None else str(text))
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            table.setItem(row, col, item)
        elif text is not None:
            item.setText(str(text))
        return item

    def _normalize_login_status(self, status_data):
        status = dict(status_data or {})
        state = str(status.get("state") or "").strip().lower()
        if not state:
            if status.get("logged_in"):
                state = "logged_in"
            elif status:
                state = "logged_out"
            else:
                state = "unknown"
        status["state"] = state
        status["logged_in"] = bool(status.get("logged_in")) if state != "logged_out" else False
        return status

    def _resolve_account_session_dir(self, account):
        account = dict(account or {})
        session_path = Path(str(account.get("session_path") or "").strip()).expanduser()
        if session_path.is_dir():
            return session_path

        account_name = str(account.get("name") or "").strip()
        if account_name:
            fallback = get_sessions_dir() / account_name
            if fallback.is_dir():
                return fallback
        return session_path

    def _get_login_status(self, account):
        import time as _time

        session_dir = self._resolve_account_session_dir(account)
        if not session_dir.exists():
            return False, "❌ Logged Out", ""

        # Check if runtime auth status override exists (from generation errors)
        acc_name = str(account.get("name") or "").strip()
        runtime_status = getattr(self, "_runtime_auth_status", {}).get(acc_name)
        if runtime_status == "expired":
            return False, "⚠ Session Expired", "Re-login required — generation auth failed"
        if runtime_status == "quota_exhausted":
            # Account is logged in fine — it just ran out of quota on every
            # image model. Show a distinct banner so the user knows to switch
            # accounts; it auto-clears on the next successful generation.
            return True, "🚫 Quota Exhausted", (
                "All image models (Nano Banana 2 / Lite / Pro) hit their "
                "per-account quota. Resets in ~6h — use another account meanwhile."
            )

        # Check exported_cookies.json for auth cookie validity
        cookies_json = session_dir / "exported_cookies.json"
        if cookies_json.exists():
            try:
                import json as _json
                with open(str(cookies_json), "r", encoding="utf-8") as f:
                    cookies = _json.load(f)
                auth_names = {"SID", "SSID", "HSID", "SAPISID", "__Secure-1PSID"}
                auth_cookies = [c for c in cookies if c.get("name") in auth_names]
                if auth_cookies:
                    now = _time.time()
                    expired = [
                        c for c in auth_cookies
                        if c.get("expires", 0) > 0 and c["expires"] < now
                    ]
                    if len(expired) >= 2:
                        return False, "⚠ Cookies Expired", f"{len(expired)} auth cookies expired"
                    return True, "✅ Logged In", f"{len(auth_cookies)} auth cookies"
            except Exception:
                pass

        # Fallback: check session files
        local_storage_dir = session_dir / "Default" / "Local Storage"
        if local_storage_dir.exists():
            try:
                if local_storage_dir.is_dir() and any(local_storage_dir.iterdir()):
                    return True, "✅ Logged In", str(local_storage_dir)
            except Exception:
                pass

        cookie_candidates = [
            session_dir / "Default" / "Network" / "Cookies",
            session_dir / "Default" / "Cookies",
        ]
        for candidate in cookie_candidates:
            if candidate.exists():
                return True, "✅ Logged In", str(candidate)

        return False, "❌ Logged Out", ""

    def _check_login_status(self, account):
        is_logged_in, _, _ = self._get_login_status(account)
        return "logged_in" if is_logged_in else "logged_out"

    def _warmup_progress_stylesheet(self, chunk_color="#F59E0B"):
        return (
            "QProgressBar {"
            " border: 1px solid #334155;"
            " border-radius: 3px;"
            " background-color: #1E293B;"
            " text-align: center;"
            " color: white;"
            " font-size: 11px;"
            "} "
            f"QProgressBar::chunk {{ background-color: {chunk_color}; border-radius: 2px; }}"
        )

    def _build_warmup_progress(self):
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(6)

        progress_bar = QProgressBar()
        progress_bar.setRange(0, 100)
        progress_bar.setValue(0)
        progress_bar.setFixedHeight(16)
        progress_bar.setFixedWidth(120)
        progress_bar.setStyleSheet(self._warmup_progress_stylesheet())

        status_label = QLabel("")
        status_label.setStyleSheet("color: #F59E0B; font-size: 11px;")

        layout.addWidget(progress_bar)
        layout.addWidget(status_label, 1)
        widget.setVisible(False)
        return widget, progress_bar, status_label

    def _set_account_detail_cell(self, row, account_name, detail_text):
        detail_label = QLabel(str(detail_text or ""))
        detail_label.setStyleSheet("color: #CBD5E1; padding: 2px 4px;")
        detail_label.setWordWrap(True)
        detail_label.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)

        warmup_widget, warmup_bar, warmup_status = self._build_warmup_progress()
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(0)
        layout.addWidget(detail_label)
        layout.addWidget(warmup_widget)
        self.acc_table.setCellWidget(row, 8, container)
        self.warmup_widgets[account_name] = {
            "widget": warmup_widget,
            "progress_bar": warmup_bar,
            "status_label": warmup_status,
            "detail_label": detail_label,
            "row": row,
        }
        if account_name in self.active_warmup_progress:
            self._apply_warmup_state(account_name)

    def _set_account_detail_text(self, account_name, detail_text, row=None):
        warmup = self.warmup_widgets.get(account_name)
        if warmup:
            warmup["detail_label"].setText(str(detail_text or ""))
            if row is not None:
                warmup["row"] = row
            return
        if row is not None:
            self._get_or_create_table_item(self.acc_table, row, 8, detail_text)

    def _apply_warmup_state(self, account_name):
        warmup = self.warmup_widgets.get(account_name)
        state = self.active_warmup_progress.get(account_name)
        if not warmup or not state:
            return

        warmup["widget"].setVisible(True)
        warmup["detail_label"].setVisible(False)
        warmup["progress_bar"].setValue(int(state.get("percent", 0) or 0))
        warmup["status_label"].setText(str(state.get("status") or ""))
        success = state.get("success")
        if success is True:
            warmup["progress_bar"].setStyleSheet(self._warmup_progress_stylesheet("#22C55E"))
            warmup["status_label"].setStyleSheet("color: #22C55E; font-size: 11px;")
        elif success is False:
            warmup["progress_bar"].setStyleSheet(self._warmup_progress_stylesheet("#EF4444"))
            warmup["status_label"].setStyleSheet("color: #EF4444; font-size: 11px;")
        else:
            warmup["progress_bar"].setStyleSheet(self._warmup_progress_stylesheet("#F59E0B"))
            warmup["status_label"].setStyleSheet("color: #F59E0B; font-size: 11px;")

    def _on_warmup_progress(self, account_name, percent, status_text):
        account_key = str(account_name or "").strip()
        if not account_key:
            return
        self.active_warmup_progress[account_key] = {
            "percent": max(0, min(100, int(percent or 0))),
            "status": str(status_text or ""),
            "success": None,
        }
        self._apply_warmup_state(account_key)

    def _on_warmup_complete(self, account_name, success, message):
        account_key = str(account_name or "").strip()
        if not account_key:
            return
        self.active_warmup_progress[account_key] = {
            "percent": 100 if success else max(0, int(self.active_warmup_progress.get(account_key, {}).get("percent", 0))),
            "status": str(message or ""),
            "success": bool(success),
        }
        self._apply_warmup_state(account_key)
        QTimer.singleShot(3000, lambda name=account_key: self._restore_normal_detail(name))

    def _restore_normal_detail(self, account_name):
        warmup = self.warmup_widgets.get(account_name)
        if not warmup:
            self.active_warmup_progress.pop(account_name, None)
            return

        warmup["widget"].setVisible(False)
        warmup["detail_label"].setVisible(True)
        warmup["progress_bar"].setValue(0)
        warmup["progress_bar"].setStyleSheet(self._warmup_progress_stylesheet("#F59E0B"))
        warmup["status_label"].setText("")
        warmup["status_label"].setStyleSheet("color: #F59E0B; font-size: 11px;")
        self.active_warmup_progress.pop(account_name, None)

    def _clear_warmup_tracking(self, account_name):
        account_key = str(account_name or "").strip()
        if not account_key:
            return
        self.active_warmup_progress.pop(account_key, None)
        self.warmup_widgets.pop(account_key, None)

    def _set_account_name_cell(self, row, account_id, display_name, real_name):
        """Clean 2-line account cell: bold name + muted meta. No avatar, no boxes."""
        if not hasattr(self, "acc_table"):
            return

        full_name = real_name or display_name or "Unknown"

        widget = QWidget()
        widget.setStyleSheet("background: transparent; border: none;")

        layout = QVBoxLayout(widget)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(3)

        # Line 1: Bold name, full width
        name_label = QLabel(full_name)
        name_label.setStyleSheet(
            "color: #F1F5F9;"
            "font-size: 13px;"
            "font-weight: 600;"
            "background: transparent;"
            "border: none;"
        )
        name_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(name_label)

        # Line 2: muted ID
        meta_label = QLabel(f"Account ID: {account_id}")
        meta_label.setStyleSheet(
            "color: #64748B;"
            "font-size: 11px;"
            "font-weight: 400;"
            "background: transparent;"
            "border: none;"
        )
        layout.addWidget(meta_label)

        widget.setToolTip(full_name)
        self.acc_table.setCellWidget(row, 1, widget)

    def _set_account_saved_status_cell(self, row, account):
        if not hasattr(self, "acc_table"):
            return

        session_dir = self._resolve_account_session_dir(account)
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(6)

        if session_dir.exists():
            dot = QLabel("●")
            dot.setStyleSheet("color: #3B82F6; font-size: 14px; font-weight: 900;")
            label = QLabel("Saved")
            label.setStyleSheet("color: #3B82F6; font-weight: 600; font-size: 12px;")
            layout.addWidget(dot)
            layout.addWidget(label)
            widget.setToolTip(str(session_dir))
        else:
            label = QLabel("—")
            label.setStyleSheet("color: #64748B; font-weight: 600; font-size: 14px;")
            layout.addWidget(label)
        layout.addStretch()
        self.acc_table.setCellWidget(row, 4, widget)

    def _set_account_login_status_cell(self, row, account_id, status_data=None):
        """Render modern colored-dot status pill instead of emoji."""
        if not hasattr(self, "acc_table"):
            return

        status = self._normalize_login_status(status_data)
        state = status.get("state", "unknown")
        account = self._account_record_by_id(account_id) or {}
        is_logged_in, _status_text, detail_tip = self._get_login_status(account)

        # Map state → (label, color)
        if state == "logging_in":
            label_text, color = "Logging in", "#3B82F6"
        elif state == "checking":
            label_text, color = "Checking", "#94A3B8"
        elif is_logged_in:
            label_text, color = "Active", "#10B981"
            email = str(status.get("email") or "").strip()
            expires = str(status.get("expires") or "").strip()
            detail_bits = [bit for bit in (detail_tip, email, expires) if bit]
            detail_tip = "\n".join(detail_bits)
        else:
            label_text, color = "Offline", "#EF4444"
            error_text = str(status.get("error") or "").strip()
            detail_tip = "\n".join(bit for bit in (detail_tip, error_text) if bit)

        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(8)

        # Colored dot (no emoji — clean Linear/Notion style)
        dot = QLabel("●")
        dot.setStyleSheet(f"color: {color}; font-size: 14px; font-weight: 900;")
        layout.addWidget(dot)

        label = QLabel(label_text)
        label.setStyleSheet(f"color: {color}; font-weight: 600; font-size: 12px;")
        layout.addWidget(label)
        layout.addStretch()

        if detail_tip:
            widget.setToolTip(detail_tip)
        self.acc_table.setCellWidget(row, 3, widget)

    def _refresh_login_statuses(self):
        if not hasattr(self, "acc_table"):
            return

        for row in range(self.acc_table.rowCount()):
            id_item = self.acc_table.item(row, 0)
            if not id_item:
                continue
            account_id = int(id_item.data(Qt.UserRole) or 0)
            self._set_account_login_status_cell(row, account_id, self.account_login_state.get(account_id))
            account = self._account_record_by_id(account_id) or {}
            self._set_account_saved_status_cell(row, account)

        self._refresh_account_overview()

    def _find_account_row(self, account_id):
        target_id = int(account_id or 0)
        for row in range(self.acc_table.rowCount()):
            id_item = self.acc_table.item(row, 0)
            if id_item and int(id_item.data(Qt.UserRole) or 0) == target_id:
                return row
        return -1

    def _account_record_by_id(self, account_id):
        target_id = int(account_id or 0)
        for account in list(self._latest_accounts or []):
            if int(account.get("id") or 0) == target_id:
                return account
        return None

    def _sanitize_account_clone_prefix(self, account_name):
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(account_name or "account")).strip("._-")
        return safe or "account"

    def _make_account_action_button(self, text, color, hover_color):
        btn = QPushButton(text)
        btn.setFixedSize(60, 26)
        btn.setStyleSheet(
            f"""
            QPushButton {{
                background: transparent;
                color: {color};
                font-size: 11px;
                font-weight: 600;
                border: 1px solid {color};
                border-radius: 4px;
            }}
            QPushButton:hover {{
                background: {hover_color};
                color: white;
            }}
            """
        )
        return btn

    def _add_account_action_buttons(self, row, account_id, account_name):
        """Modern dropdown menu (⋯) replacing Reset/Delete buttons."""
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(4)
        layout.addStretch()

        # 3-dot dropdown menu button — Linear/Notion style
        menu_btn = QPushButton("⋯")
        menu_btn.setFixedSize(32, 32)
        menu_btn.setCursor(Qt.PointingHandCursor)
        menu_btn.setToolTip(f"Actions for {account_name}")
        menu_btn.setStyleSheet(
            "QPushButton {"
            " background: transparent;"
            " color: #94A3B8;"
            " border: 1px solid #334155;"
            " border-radius: 6px;"
            " font-size: 18px;"
            " font-weight: 700;"
            " padding: 0px;"
            "}"
            "QPushButton:hover {"
            " background: #1E293B;"
            " color: #F1F5F9;"
            " border-color: #475569;"
            "}"
            "QPushButton:pressed {"
            " background: #334155;"
            "}"
            "QPushButton::menu-indicator { image: none; width: 0px; }"
        )

        from PySide6.QtWidgets import QMenu
        menu = QMenu(menu_btn)
        menu.setStyleSheet(
            "QMenu {"
            " background: #1E293B;"
            " color: #F1F5F9;"
            " border: 1px solid #334155;"
            " border-radius: 8px;"
            " padding: 6px 0px;"
            " font-size: 13px;"
            "}"
            "QMenu::item {"
            " padding: 8px 20px 8px 16px;"
            " border-radius: 4px;"
            " margin: 2px 6px;"
            "}"
            "QMenu::item:selected {"
            " background: #334155;"
            "}"
            "QMenu::separator {"
            " height: 1px;"
            " background: #334155;"
            " margin: 4px 10px;"
            "}"
        )

        act_reset = menu.addAction("🔄  Reset Session")
        act_reset.setToolTip("Delete saved session and open a fresh login browser.")
        act_reset.triggered.connect(
            lambda _=False, target_id=account_id: self._reset_account_session(target_id)
        )

        act_relogin = menu.addAction("🔐  Re-Login")
        act_relogin.triggered.connect(
            lambda _=False, target_id=account_id: self._relogin_account(target_id)
        )

        menu.addSeparator()

        act_delete = menu.addAction("🗑  Delete Account")
        act_delete.setToolTip(f"Remove '{account_name}' from Account Manager.")
        act_delete.triggered.connect(
            lambda _=False, target_id=account_id: self._delete_account_by_id(target_id)
        )

        menu.addSeparator()

        act_del_dola = menu.addAction("❌  Delete from dola.com")
        act_del_dola.setToolTip(
            f"Permanently delete '{account_name}' from dola.com itself (irreversible).\n"
            "Fully automatic. The linked Google login/cookies are NOT touched."
        )
        act_del_dola.triggered.connect(
            lambda _=False, target_id=account_id: self._delete_dola_online(target_id)
        )

        menu_btn.setMenu(menu)
        layout.addWidget(menu_btn)

        self.acc_table.setCellWidget(row, 9, widget)

    def _proxy_status_text(self, proxy_value):
        proxy_text = str(proxy_value or "").strip()
        if not proxy_text:
            return "Direct"
        parsed = urlparse(proxy_text)
        if parsed.hostname and parsed.port:
            return f"{parsed.hostname}:{parsed.port}"
        return proxy_text

    def _set_account_proxy_cell(self, row, account_id, account_name, proxy_value):
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(6)

        proxy_text = str(proxy_value or "").strip()

        if proxy_text:
            # Colored dot + compact text
            dot = QLabel("●")
            dot.setStyleSheet("color: #10B981; font-size: 12px; font-weight: 900;")
            layout.addWidget(dot)
            label = QLabel(self._proxy_status_text(proxy_text))
            label.setStyleSheet("color: #10B981; font-weight: 600; font-size: 12px;")
            label.setToolTip(proxy_text)
            layout.addWidget(label, 1)
        else:
            label = QLabel("Direct")
            label.setStyleSheet("color: #64748B; font-weight: 500; font-size: 12px;")
            layout.addWidget(label, 1)

        # Icon-only settings button (cleaner)
        btn_proxy = QPushButton("\u2699")
        btn_proxy.setFixedSize(28, 28)
        btn_proxy.setCursor(Qt.PointingHandCursor)
        btn_proxy.setToolTip("Configure proxy")
        btn_proxy.setStyleSheet(
            "QPushButton {"
            " background: transparent;"
            " color: #94A3B8;"
            " border: 1px solid #334155;"
            " border-radius: 5px;"
            " font-size: 14px;"
            "}"
            "QPushButton:hover {"
            " background: #1E293B;"
            " color: #F1F5F9;"
            " border-color: #475569;"
            "}"
            "QPushButton:pressed {"
            " background: #334155;"
            "}"
        )
        btn_proxy.clicked.connect(
            lambda _=False, target_id=account_id: self._open_proxy_dialog(target_id)
        )
        layout.addWidget(btn_proxy)

        self.acc_table.setCellWidget(row, 2, widget)

    def _open_proxy_dialog(self, account_id):
        account = self._account_record_by_id(account_id)
        if not account:
            QMessageBox.warning(self, "Account Missing", "Could not find that account in the database.")
            return

        account_name = str(account.get("name") or "").strip()
        current_proxy = str(account.get("proxy") or "").strip()
        dialog = ProxyConfigDialog(account_name, current_proxy, self)
        if dialog.exec() != QDialog.Accepted:
            return

        proxy_value = dialog.get_proxy_url()
        self._start_background_task(
            update_account_proxy_by_id,
            int(account_id),
            proxy_value,
            on_finished=lambda _result, account_id=account_id, proxy_value=proxy_value: self._on_account_proxy_saved(
                account_id,
                proxy_value,
            ),
        )

    def _delete_account_session_artifacts(self, account):
        account_name = str(account.get("name") or "").strip()
        session_path = Path(str(account.get("session_path") or "").strip()).expanduser()
        removed_paths = []

        if session_path.is_dir():
            shutil.rmtree(session_path, ignore_errors=True)
            removed_paths.append(str(session_path))

        clone_root = get_session_clones_dir()
        clone_prefixes = {
            self._sanitize_account_clone_prefix(account_name),
            self._sanitize_account_clone_prefix(session_path.name),
        }
        if clone_root.is_dir():
            for child in clone_root.iterdir():
                if not child.is_dir():
                    continue
                if any(prefix and child.name.startswith(prefix) for prefix in clone_prefixes):
                    shutil.rmtree(child, ignore_errors=True)
                    removed_paths.append(str(child))

        return removed_paths

    def _start_account_session_refresh(self, account_id, action_label="Re-login"):
        account = self._account_record_by_id(account_id)
        if not account:
            QMessageBox.warning(self, "Account Missing", "Could not find that account in the database.")
            return False

        account_name = str(account.get("name") or "").strip()
        runtime = self.account_runtime_state.get(account_name, {})
        active_slots = int(runtime.get("active_slots", 0) or 0)
        if active_slots > 0:
            QMessageBox.warning(
                self,
                "Account Busy",
                f"'{account_name}' currently has {active_slots} active slot(s). Stop the queue or wait until it is idle before {action_label.lower()}.",
            )
            return False

        if self.relogin_worker and self.relogin_worker.isRunning():
            QMessageBox.information(
                self,
                "Login Already Running",
                "Another account login browser is already open. Finish that first, then try again.",
            )
            return False

        removed_paths = self._delete_account_session_artifacts(account)
        self.account_login_state[int(account_id)] = {"state": "logging_in"}
        row = self._find_account_row(account_id)
        if row >= 0:
            self._set_account_login_status_cell(row, account_id, self.account_login_state[int(account_id)])

        if removed_paths:
            self.append_log(
                f"[ACCOUNTS] {action_label} requested for {account_name}. Cleared {len(removed_paths)} session item(s)."
            )
        else:
            self.append_log(
                f"[ACCOUNTS] {action_label} requested for {account_name}. No prior session files found."
            )
        self.append_log(f"[ACCOUNTS] Opening fresh login browser for {account_name}...")

        worker = LoginWorker(account_name)
        worker.log_msg.connect(self.append_log, Qt.QueuedConnection)
        worker.warmup_progress.connect(self.warmup_progress_signal.emit, Qt.QueuedConnection)
        worker.warmup_complete.connect(self.warmup_complete_signal.emit, Qt.QueuedConnection)
        worker.finished_login.connect(
            lambda name, session_path, detected_email, target_id=account_id: self.on_relogin_finished(
                target_id, name, session_path, detected_email
            )
        )
        self.relogin_worker = worker
        worker.start()
        return True

    def _reset_account_session(self, account_id):
        account = self._account_record_by_id(account_id)
        if not account:
            QMessageBox.warning(self, "Account Missing", "Could not find that account in the database.")
            return

        account_name = str(account.get("name") or "").strip()
        if not self._fluent_confirm(
            "Reset Session",
            f"Reset session for '{account_name}'?\n\n"
            f"This deletes saved cookies/session clones and opens a fresh login browser.",
            confirm_label="Reset",
            cancel_label="Cancel",
        ):
            return

        self._start_account_session_refresh(account_id, action_label="Session reset")

    def _delete_account_by_id(self, account_id):
        account = self._account_record_by_id(account_id)
        if not account:
            QMessageBox.warning(self, "Account Missing", "Could not find that account in the database.")
            return

        account_name = str(account.get("name") or "").strip()

        # Confirmation dialog — prevent accidental deletion
        if not self._fluent_confirm(
            "Delete Account",
            f"Are you sure you want to delete '{account_name}'?\n\n"
            f"This will permanently remove:\n"
            f"  • Account from database\n"
            f"  • Saved login session and cookies\n"
            f"  • All pending/running jobs will be reassigned\n\n"
            f"This action CANNOT be undone.",
            confirm_label="Delete",
            cancel_label="Cancel",
        ):
            return

        self._start_background_task(
            self._delete_account_record,
            account_id,
            account_name,
            on_finished=lambda _result, account_id=account_id, account_name=account_name: self._on_account_deleted(
                account_id,
                account_name,
            ),
        )

    def _delete_dola_online(self, account_id):
        """Permanently delete the account from dola.com itself (irreversible).

        Fully automated: no manual clicking in the browser. Only the dola.com
        account is deleted — the linked Google login/cookies are left intact.
        """
        account = self._account_record_by_id(account_id)
        if not account:
            QMessageBox.warning(self, "Account Missing", "Could not find that account in the database.")
            return

        account_name = str(account.get("name") or "").strip()
        session_path = str(self._resolve_account_session_dir(account))
        proxy = str(account.get("proxy") or "").strip()

        # Guard: don't delete while the queue is actively using this account.
        runtime = getattr(self, "account_runtime_state", {}).get(account_name, {})
        if int(runtime.get("active_slots", 0) or 0) > 0:
            QMessageBox.warning(
                self,
                "Account Busy",
                f"'{account_name}' has active queue slot(s). Stop the queue before deleting it from dola.com.",
            )
            return

        if self.dola_delete_worker and self.dola_delete_worker.isRunning():
            QMessageBox.information(
                self,
                "Deletion In Progress",
                "Another dola.com account deletion is still running. Please wait for it to finish.",
            )
            return

        # One-click confirmation — a single lightweight guard against accidental
        # clicks on an irreversible action (no typing required).
        if not self._fluent_confirm(
            "Delete from dola.com",
            f"Delete '{account_name}' from dola.com now?\n\n"
            "One click, then fully automatic. The linked Google login is NOT affected "
            "(you can re-login with the same Gmail later).\n\n"
            "This action CANNOT be undone.",
            confirm_label="Delete",
            cancel_label="Cancel",
        ):
            return

        self.append_log(f"[ACCOUNTS] Starting dola.com deletion for '{account_name}' (automatic)...")
        worker = DolaDeleteWorker(account_id, session_path, proxy=proxy)
        worker.log_msg.connect(self.append_log, Qt.QueuedConnection)
        worker.finished_delete.connect(self._on_dola_delete_finished, Qt.QueuedConnection)
        self.dola_delete_worker = worker
        worker.start()

    def _on_dola_delete_finished(self, account_id, ok, detail):
        account = self._account_record_by_id(account_id)
        account_name = str((account or {}).get("name") or f"Account {account_id}")
        if ok:
            self.append_log(f"[ACCOUNTS] ✅ '{account_name}' deleted from dola.com. {detail}")
            QMessageBox.information(
                self,
                "Deleted from dola.com",
                f"'{account_name}' was deleted from dola.com.\n\n{detail}\n\n"
                "It is still listed here (kept as requested). The Google login is intact.",
            )
        else:
            self.append_log(f"[ACCOUNTS] ❌ dola.com deletion failed for '{account_name}': {detail}")
            QMessageBox.warning(
                self,
                "Deletion Failed",
                f"Could not delete '{account_name}' from dola.com.\n\n{detail}",
            )

    @staticmethod
    def _clear_account_project_cache_artifacts(account_name):
        normalized_name = str(account_name or "").strip()
        if not normalized_name:
            return

        try:
            GoogleLabsBot.clear_account_project_cache(normalized_name)
        except Exception:
            pass

        cache_file = get_project_cache_path()
        if not cache_file.exists():
            return

        try:
            with cache_file.open("r", encoding="utf-8") as handle:
                cache_data = json.load(handle)
            if isinstance(cache_data, dict):
                cache_data.pop(normalized_name, None)
                with cache_file.open("w", encoding="utf-8") as handle:
                    json.dump(cache_data, handle, indent=2, sort_keys=True)
            else:
                cache_file.unlink(missing_ok=True)
        except Exception:
            try:
                cache_file.unlink(missing_ok=True)
            except Exception:
                pass

    @staticmethod
    def _delete_account_record(account_id, account_name):
        # Reassign any pending/running jobs from this account before deleting
        try:
            from src.db.db_manager import reassign_account_jobs
            reassigned = reassign_account_jobs(account_name)
            if reassigned > 0:
                pass  # Logged in _on_account_deleted
        except Exception:
            reassigned = 0
        MainWindow._clear_account_project_cache_artifacts(account_name)
        clear_account_flags(account_name)
        if int(account_id or 0) > 0:
            remove_account_by_id(int(account_id))
        else:
            remove_account(account_name)
        return reassigned

    def _on_account_deleted(self, account_id, account_name):
        self.account_login_state.pop(int(account_id or 0), None)
        self._runtime_auth_status.pop(account_name, None)
        self._clear_warmup_tracking(account_name)
        self.append_log(f"Deleted account '{account_name}'. Pending jobs reassigned to other accounts.")
        self.refresh_accounts()

    def _sanitize_account_clone_prefix(self, account_name):
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(account_name or "account")).strip("._-")
        return safe or "account"

    def _add_account_action_buttons(self, row, account_id, account_name):
        """Modern dropdown menu (⋯) replacing Reset/Delete buttons."""
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(4)
        layout.addStretch()

        # 3-dot dropdown menu button — Linear/Notion style
        menu_btn = QPushButton("⋯")
        menu_btn.setFixedSize(32, 32)
        menu_btn.setCursor(Qt.PointingHandCursor)
        menu_btn.setToolTip(f"Actions for {account_name}")
        menu_btn.setStyleSheet(
            "QPushButton {"
            " background: transparent;"
            " color: #94A3B8;"
            " border: 1px solid #334155;"
            " border-radius: 6px;"
            " font-size: 18px;"
            " font-weight: 700;"
            " padding: 0px;"
            "}"
            "QPushButton:hover {"
            " background: #1E293B;"
            " color: #F1F5F9;"
            " border-color: #475569;"
            "}"
            "QPushButton:pressed {"
            " background: #334155;"
            "}"
            "QPushButton::menu-indicator { image: none; width: 0px; }"
        )

        from PySide6.QtWidgets import QMenu
        menu = QMenu(menu_btn)
        menu.setStyleSheet(
            "QMenu {"
            " background: #1E293B;"
            " color: #F1F5F9;"
            " border: 1px solid #334155;"
            " border-radius: 8px;"
            " padding: 6px 0px;"
            " font-size: 13px;"
            "}"
            "QMenu::item {"
            " padding: 8px 20px 8px 16px;"
            " border-radius: 4px;"
            " margin: 2px 6px;"
            "}"
            "QMenu::item:selected {"
            " background: #334155;"
            "}"
            "QMenu::separator {"
            " height: 1px;"
            " background: #334155;"
            " margin: 4px 10px;"
            "}"
        )

        act_reset = menu.addAction("🔄  Reset Session")
        act_reset.setToolTip("Delete saved session and open a fresh login browser.")
        act_reset.triggered.connect(
            lambda _=False, target_id=account_id: self._reset_account_session(target_id)
        )

        act_relogin = menu.addAction("🔐  Re-Login")
        act_relogin.triggered.connect(
            lambda _=False, target_id=account_id: self._relogin_account(target_id)
        )

        menu.addSeparator()

        act_delete = menu.addAction("🗑  Delete Account")
        act_delete.setToolTip(f"Remove '{account_name}' from Account Manager.")
        act_delete.triggered.connect(
            lambda _=False, target_id=account_id: self._delete_account_by_id(target_id)
        )

        menu.addSeparator()

        act_del_dola = menu.addAction("❌  Delete from dola.com")
        act_del_dola.setToolTip(
            f"Permanently delete '{account_name}' from dola.com itself (irreversible).\n"
            "Fully automatic. The linked Google login/cookies are NOT touched."
        )
        act_del_dola.triggered.connect(
            lambda _=False, target_id=account_id: self._delete_dola_online(target_id)
        )

        menu_btn.setMenu(menu)
        layout.addWidget(menu_btn)

        self.acc_table.setCellWidget(row, 9, widget)

    def _delete_account_session_artifacts(self, account):
        account_name = str(account.get("name") or "").strip()
        session_path = Path(str(account.get("session_path") or "").strip()).expanduser()
        removed_paths = []

        if account_name:
            set_account_flag(account_name, "warmup_done", "False")

        if session_path and session_path.is_dir():
            shutil.rmtree(session_path, ignore_errors=True)
            removed_paths.append(str(session_path))

        clone_root = get_session_clones_dir()
        clone_prefixes = {
            self._sanitize_account_clone_prefix(account_name),
            self._sanitize_account_clone_prefix(session_path.name if session_path else ""),
        }
        if clone_root.is_dir():
            for child in clone_root.iterdir():
                if not child.is_dir():
                    continue
                child_name = child.name
                if any(prefix and child_name.startswith(prefix) for prefix in clone_prefixes):
                    shutil.rmtree(child, ignore_errors=True)
                    removed_paths.append(str(child))

        return removed_paths

    def _start_account_session_refresh(self, account_id, action_label="Re-login"):
        account = self._account_record_by_id(account_id)
        if not account:
            QMessageBox.warning(self, "Account Missing", "Could not find that account in the database.")
            return False

        account_name = str(account.get("name") or "").strip()
        runtime = self.account_runtime_state.get(account_name, {})
        active_slots = int(runtime.get("active_slots", 0) or 0)
        if active_slots > 0:
            QMessageBox.warning(
                self,
                "Account Busy",
                f"'{account_name}' currently has {active_slots} active slot(s). Stop the queue or wait until it is idle before {action_label.lower()}.",
            )
            return False

        if self.relogin_worker and self.relogin_worker.isRunning():
            QMessageBox.information(
                self,
                "Login Already Running",
                "Another account login window is already open. Finish that first, then try again.",
            )
            return False

        removed_paths = self._delete_account_session_artifacts(account)
        self.account_login_state[int(account_id)] = {"state": "logging_in"}
        row = self._find_account_row(account_id)
        if row >= 0:
            self._set_account_login_status_cell(row, account_id, self.account_login_state[int(account_id)])

        if removed_paths:
            self.append_log(
                f"[ACCOUNTS] {action_label} requested for {account_name}. Cleared {len(removed_paths)} session item(s)."
            )
        else:
            self.append_log(
                f"[ACCOUNTS] {action_label} requested for {account_name}. No prior session files found."
            )
        self.append_log(f"[ACCOUNTS] Opening fresh login browser for {account_name}...")

        worker = LoginWorker(account_name, proxy=account.get("proxy"))
        worker.log_msg.connect(self.append_log, Qt.QueuedConnection)
        worker.warmup_progress.connect(self.warmup_progress_signal.emit, Qt.QueuedConnection)
        worker.warmup_complete.connect(self.warmup_complete_signal.emit, Qt.QueuedConnection)
        worker.finished_login.connect(
            lambda name, session_path, detected_email, target_id=account_id: self.on_relogin_finished(
                target_id, name, session_path, detected_email
            )
        )
        self.relogin_worker = worker
        worker.start()
        return True

    def _reset_account_session(self, account_id):
        account = self._account_record_by_id(account_id)
        if not account:
            QMessageBox.warning(self, "Account Missing", "Could not find that account in the database.")
            return

        account_name = str(account.get("name") or "").strip()
        if not self._fluent_confirm(
            "Reset Session",
            f"Reset session for '{account_name}'?\n\n"
            f"This deletes saved cookies/session clones and opens a fresh login browser.",
            confirm_label="Reset",
            cancel_label="Cancel",
        ):
            return

        self._start_account_session_refresh(account_id, action_label="Session reset")

    def _delete_account_by_id(self, account_id):
        account = self._account_record_by_id(account_id)
        if not account:
            QMessageBox.warning(self, "Account Missing", "Could not find that account in the database.")
            return

        account_name = str(account.get("name") or "").strip()

        # Confirmation dialog — prevent accidental deletion
        if not self._fluent_confirm(
            "Delete Account",
            f"Are you sure you want to delete '{account_name}'?\n\n"
            f"This will permanently remove:\n"
            f"  • Account from database\n"
            f"  • Saved login session and cookies\n"
            f"  • All pending/running jobs will be reassigned\n\n"
            f"This action CANNOT be undone.",
            confirm_label="Delete",
            cancel_label="Cancel",
        ):
            return

        self._start_background_task(
            self._delete_account_record,
            account_id,
            account_name,
            on_finished=lambda _result, account_id=account_id, account_name=account_name: self._on_account_deleted(
                account_id,
                account_name,
            ),
        )

    def _on_account_table_item_changed(self, item):
        return

    def _on_account_proxy_saved(self, account_id, proxy_value):
        for account in self._latest_accounts or []:
            if int(account.get("id") or 0) == int(account_id):
                account["proxy"] = proxy_value
                account_name = str(account.get("name") or "").strip()
                if proxy_value:
                    self.append_log(f"[ACCOUNTS] Proxy updated for {account_name}.")
                else:
                    self.append_log(f"[ACCOUNTS] Proxy cleared for {account_name}.")
                if self.queue_manager and self.queue_manager.isRunning():
                    self.append_log("[ACCOUNTS] Restart queue to apply updated proxy settings.")
                break
        self.refresh_accounts()

    def _refresh_account_overview(self):
        if not hasattr(self, "acc_table"):
            return
        total = self.acc_table.rowCount()
        logged_in = 0
        logged_out = 0
        running = 0
        cooldown = 0
        ready = 0
        for row in range(total):
            id_item = self.acc_table.item(row, 0)
            account_id = int(id_item.data(Qt.UserRole) or 0) if id_item else 0
            account = self._account_record_by_id(account_id) or {}
            if self._check_login_status(account) == "logged_in":
                logged_in += 1
            else:
                logged_out += 1

            runtime = str(self._get_or_create_table_item(self.acc_table, row, 5).text() or "").lower()
            if runtime == "running":
                running += 1
            elif runtime in ("cooldown", "slot cooldown", "slot_cooldown"):
                cooldown += 1
            elif runtime == "ready":
                ready += 1

        if hasattr(self, "lbl_acc_total"):
            self.lbl_acc_total.setText(f"Total: {total}")
        if hasattr(self, "lbl_acc_logged_in"):
            self.lbl_acc_logged_in.setText(f"Logged In: {logged_in}")
        if hasattr(self, "lbl_acc_logged_out"):
            self.lbl_acc_logged_out.setText(f"Logged Out: {logged_out}")
        if hasattr(self, "lbl_acc_running"):
            self.lbl_acc_running.setText(f"Running: {running}")
        if hasattr(self, "lbl_acc_cooldown"):
            self.lbl_acc_cooldown.setText(f"Cooldown: {cooldown}")
        if hasattr(self, "lbl_acc_ready"):
            self.lbl_acc_ready.setText(f"Ready: {ready}")

    def _refresh_account_runtime_cells(self):
        if not hasattr(self, "acc_table"):
            return

        now = time.time()
        for row in range(self.acc_table.rowCount()):
            name_item = self._get_or_create_table_item(self.acc_table, row, 1)
            account_name = str(name_item.data(Qt.UserRole) or name_item.text() or "").strip()
            runtime = self.account_runtime_state.get(account_name, {})

            runtime_status = str(runtime.get("status", "idle")).strip().lower()
            runtime_display = {
                "cooldown": "cooldown",
                "slot_cooldown": "slot cooldown",
                "running": "running",
                "ready": "ready",
                "idle": "idle",
            }.get(runtime_status, runtime_status or "idle")
            cooldown_until = float(runtime.get("cooldown_until", 0.0) or 0.0)
            active_slots = int(runtime.get("active_slots", 0) or 0)
            total_slots = int(runtime.get("total_slots", 1) or 1)
            detail = str(runtime.get("detail", "Queue stopped" if not self.queue_manager or not self.queue_manager.isRunning() else "Ready"))

            remaining = max(0, int(cooldown_until - now))
            cooldown_text = self._format_remaining(remaining) if remaining > 0 else "--"
            slots_text = f"{active_slots}/{max(1, total_slots)}"

            runtime_item = self._get_or_create_table_item(self.acc_table, row, 5, runtime_display)
            self._set_account_runtime_item_style(runtime_item, runtime_status)

            cooldown_item = self._get_or_create_table_item(self.acc_table, row, 6, cooldown_text)
            if remaining > 0:
                cooldown_item.setForeground(QColor("#b4233a"))
            else:
                cooldown_item.setForeground(QColor("#64748b"))
                if runtime_status == "cooldown":
                    runtime_item.setText("ready")
                    self._set_account_runtime_item_style(runtime_item, "ready")

            self._get_or_create_table_item(self.acc_table, row, 7, slots_text)
            self._set_account_detail_text(account_name, detail, row=row)

        self._refresh_account_overview()

    def _on_account_runtime_tick(self):
        self._refresh_account_runtime_cells()

    # ── Extension Accounts Live Refresh ───────────────────────────
    def _refresh_extension_accounts(self):
        """Query bridge server in background thread, then update UI via signal."""
        if not hasattr(self, "ext_accounts_card"):
            return
        # Avoid stacking requests
        if getattr(self, "_ext_fetch_running", False):
            return
        self._ext_fetch_running = True

        import threading

        def _fetch():
            import urllib.request
            try:
                req = urllib.request.Request(
                    "http://127.0.0.1:18924/status",
                    headers={"Accept": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=1) as resp:
                    import json as _json
                    result = _json.loads(resp.read().decode())
            except Exception:
                result = None
            # Emit signal — thread-safe, delivers to main thread event loop
            try:
                self._ext_accounts_signal.emit(result)
            except RuntimeError:
                pass  # widget destroyed

        threading.Thread(target=_fetch, daemon=True).start()

    def _toggle_cookie_bridge(self):
        """Start/stop a standalone extension bridge (no generation) so the
        extension can export cookies for CloakBrowser + proxy mode."""
        running = (
            getattr(self, "_cookie_bridge_thread", None) is not None
            and self._cookie_bridge_thread.is_alive()
        )
        if running:
            self._stop_cookie_bridge()
        else:
            self._start_cookie_bridge()

    def _start_cookie_bridge(self):
        import threading
        import asyncio
        if (
            getattr(self, "_cookie_bridge_thread", None) is not None
            and self._cookie_bridge_thread.is_alive()
        ):
            return
        self._cookie_bridge = None
        self._cookie_bridge_loop = None

        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                from src.core.extension_bridge import ExtensionBridge
                bridge = ExtensionBridge(lambda m: print(m))
                self._cookie_bridge = bridge
                self._cookie_bridge_loop = loop
                loop.run_until_complete(bridge.start())
                loop.run_forever()
            except Exception as e:
                print(f"[CookieBridge] failed: {e}")
            finally:
                try:
                    loop.close()
                except Exception:
                    pass

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        self._cookie_bridge_thread = t
        try:
            self.btn_cookie_bridge.setText("🍪 Stop Cookie Bridge")
        except Exception:
            pass
        try:
            QMessageBox.information(
                self, "Cookie Bridge Started",
                "Bridge is now running (no generation).\n\n"
                "1. Open the G-Labs Helper extension popup — the status should "
                "turn GREEN (Connected).\n"
                "2. Click 'Export Cookies' under each account.\n"
                "3. Click 'Refresh List' here — the account will appear.\n"
                "4. Set its proxy, then run in HTTP Shared mode.\n\n"
                "Click 'Stop Cookie Bridge' when done (before starting a normal "
                "generation run, so ports don't clash)."
            )
        except Exception:
            pass

    def _stop_cookie_bridge(self):
        import asyncio
        loop = getattr(self, "_cookie_bridge_loop", None)
        bridge = getattr(self, "_cookie_bridge", None)
        if loop is not None and bridge is not None:
            try:
                fut = asyncio.run_coroutine_threadsafe(bridge.stop(), loop)
                fut.result(timeout=3)
            except Exception:
                pass
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass
        self._cookie_bridge_thread = None
        self._cookie_bridge = None
        self._cookie_bridge_loop = None
        try:
            self.btn_cookie_bridge.setText("🍪 Connect (Cookie Import)")
        except Exception:
            pass

    def _apply_ext_accounts(self, data):
        """Update extension accounts UI (called on main thread)."""
        self._ext_fetch_running = False
        if not hasattr(self, "ext_accounts_card"):
            return

        if data is None:
            # Bridge not running
            self.ext_status_dot.setStyleSheet("color: #EF4444; font-size: 10px;")
            self.ext_status_label.setText("Bridge not running")
            self._clear_ext_account_rows()
            self.ext_no_accounts_label.setVisible(True)
            self.ext_no_accounts_label.setText(
                "Start generation in Chrome Extension mode to connect."
            )
            return

        ext_connected = data.get("extension_connected", False)
        accounts = data.get("connected_accounts", [])  # list of email strings

        # Update status
        if ext_connected:
            self.ext_status_dot.setStyleSheet("color: #22C55E; font-size: 10px;")
            self.ext_status_label.setText(
                f"Connected · {len(accounts)} account{'s' if len(accounts) != 1 else ''}"
            )
        else:
            self.ext_status_dot.setStyleSheet("color: #F59E0B; font-size: 10px;")
            self.ext_status_label.setText("Bridge running · Extension not connected")

        # Clear old account rows
        self._clear_ext_account_rows()

        if not accounts:
            self.ext_no_accounts_label.setVisible(True)
            if ext_connected:
                self.ext_no_accounts_label.setText(
                    "No accounts detected. Open labs.google in Chrome and log in."
                )
            else:
                self.ext_no_accounts_label.setText(
                    "Install the Chrome Extension and open labs.google to detect accounts."
                )
            return

        self.ext_no_accounts_label.setVisible(False)

        for email in accounts:
            row = QFrame()
            row.setObjectName("extAccountRow")
            row.setStyleSheet("""
                QFrame#extAccountRow {
                    background: #1E293B;
                    border: 1px solid #334155;
                    border-radius: 6px;
                    padding: 0;
                }
            """)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(12, 6, 12, 6)
            row_layout.setSpacing(8)

            dot = QLabel("●")
            dot.setStyleSheet("color: #22C55E; font-size: 9px;")
            dot.setFixedWidth(12)
            row_layout.addWidget(dot)

            email_lbl = QLabel(str(email))
            email_lbl.setStyleSheet(
                "color: #F1F5F9; font-size: 13px; font-weight: 600;"
            )
            row_layout.addWidget(email_lbl)

            row_layout.addStretch()

            badge = QLabel("Extension")
            badge.setStyleSheet("""
                color: #A78BFA; font-size: 10px; font-weight: 700;
                background: #2E1065; border: 1px solid #7C3AED;
                border-radius: 4px; padding: 2px 8px;
            """)
            row_layout.addWidget(badge)

            self.ext_accounts_list.addWidget(row)

    def _clear_ext_account_rows(self):
        """Remove all dynamically added extension account row widgets."""
        while self.ext_accounts_list.count() > 1:  # keep ext_no_accounts_label
            item = self.ext_accounts_list.takeAt(1)
            if item and item.widget():
                item.widget().deleteLater()

    def _on_tab_changed(self, index):
        if self.tabs.widget(index) is self.tab_accounts and not self._account_status_auto_check_done:
            self._account_status_auto_check_done = True
            QTimer.singleShot(350, self.refresh_accounts)
        elif self.tabs.widget(index) is self.tab_accounts:
            QTimer.singleShot(150, self._refresh_login_statuses)
        if self.tabs.widget(index) is self.tab_live_generation:
            self._live_tab_dirty = False
            QTimer.singleShot(100, self._refresh_live_grid)
            self._request_queue_snapshot()
        if self.tabs.widget(index) is self.tab_failed_jobs:
            QTimer.singleShot(100, lambda: self._request_failed_jobs_refresh(force=True))
        self._sync_sidebar_selection()

    def _on_sidebar_page_selected(self, page_key):
        key = str(page_key or "dashboard").strip().lower()
        if key == "dashboard":
            self.tabs.setCurrentWidget(self.tab_dashboard)
            if hasattr(self, "mode_tabs"):
                self.mode_tabs.setCurrentIndex(0)
            return
        if key == "video":
            self.tabs.setCurrentWidget(self.tab_dashboard)
            if hasattr(self, "mode_tabs"):
                target_index = 1 if self.mode_tabs.count() > 1 else 0
                self.mode_tabs.setCurrentIndex(target_index)
            return
        if key == "accounts":
            self.tabs.setCurrentWidget(self.tab_accounts)
            return
        if key == "live":
            self.tabs.setCurrentWidget(self.tab_live_generation)
            return
        if key == "failed":
            self.tabs.setCurrentWidget(self.tab_failed_jobs)
            return
        if key == "settings":
            self.tabs.setCurrentWidget(self.tab_settings)

    def _current_sidebar_key(self):
        current_widget = self.tabs.currentWidget() if hasattr(self, "tabs") else None
        if current_widget is self.tab_dashboard:
            if hasattr(self, "mode_tabs") and self.mode_tabs.currentIndex() > 0:
                return "video"
            return "dashboard"
        if current_widget is self.tab_accounts:
            return "accounts"
        if current_widget is self.tab_live_generation:
            return "live"
        if current_widget is self.tab_failed_jobs:
            return "failed"
        if current_widget is self.tab_settings:
            return "settings"
        return "dashboard"

    def _sync_sidebar_selection(self):
        if hasattr(self, "sidebar"):
            self.sidebar.set_active(self._current_sidebar_key())

    def _on_browser_mode_changed(self, _index):
        mode = self.cmb_browser_mode.currentData() or "headless"
        is_real_chrome = mode == "real_chrome"
        is_cloak = mode == "cloakbrowser"
        if hasattr(self, "lbl_chrome_display"):
            self.lbl_chrome_display.setVisible(is_real_chrome)
        if hasattr(self, "cmb_chrome_display"):
            self.cmb_chrome_display.setVisible(is_real_chrome)
        if hasattr(self, "lbl_cloak_display"):
            self.lbl_cloak_display.setVisible(is_cloak)
        if hasattr(self, "cmb_cloak_display"):
            self.cmb_cloak_display.setVisible(is_cloak)
        if hasattr(self, "cloak_update_widget"):
            self.cloak_update_widget.setVisible(is_cloak)
        if hasattr(self, "lbl_cloak_update_status"):
            self.lbl_cloak_update_status.setVisible(False)
        if is_cloak:
            self._refresh_cloak_version_display()
        if hasattr(self, "chk_random_fingerprint"):
            if is_cloak:
                self.chk_random_fingerprint.setChecked(False)
                self.chk_random_fingerprint.setEnabled(False)
                self.chk_random_fingerprint.setToolTip("CloakBrowser has built-in C++ fingerprinting")
            else:
                self.chk_random_fingerprint.setEnabled(True)
                self.chk_random_fingerprint.setToolTip("")

    def _get_pip_python(self):
        """Get the right Python executable for pip commands.
        In .app/.exe builds, sys.executable is the bundled binary — not Python.
        Fall back to system python3/python on PATH."""
        if not getattr(sys, "frozen", False) and not getattr(sys, "_MEIPASS", None):
            return sys.executable  # Normal dev mode — use current Python
        # Frozen build — find system Python
        for cmd in ["python3", "python"]:
            try:
                result = subprocess.run(
                    [cmd, "--version"], capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0:
                    return cmd
            except Exception:
                continue
        return sys.executable  # Last resort

    def _pip_show_cloak_version(self):
        """Get real installed cloakbrowser version via pip show."""
        python_exe = self._get_pip_python()
        try:
            _no_win = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)} if sys.platform.startswith("win") else {}
            result = subprocess.run(
                [python_exe, "-m", "pip", "show", "cloakbrowser"],
                capture_output=True, text=True, timeout=15, **_no_win,
            )
            if result.returncode == 0 and result.stdout:
                for line in result.stdout.splitlines():
                    if line.startswith("Version:"):
                        return line.split(":", 1)[1].strip()
        except Exception:
            pass
        return "unknown"

    def _refresh_cloak_version_display(self, reset_button=True):
        if not hasattr(self, "lbl_cloak_version") or not hasattr(self, "btn_cloak_update"):
            return

        # ── Get REAL installed version via pip show (subprocess) ──
        pkg_version = self._pip_show_cloak_version()

        # Fallback to importlib
        if pkg_version == "unknown":
            try:
                import importlib
                for _mod in list(sys.modules.keys()):
                    if _mod == "cloakbrowser" or _mod.startswith("cloakbrowser."):
                        del sys.modules[_mod]
                importlib.invalidate_caches()
                cb = importlib.import_module("cloakbrowser")
                pkg_version = str(getattr(cb, "__version__", "unknown"))
            except ImportError:
                self.lbl_cloak_version.setText("CloakBrowser: not installed")
                self.lbl_cloak_version.setStyleSheet("color: #EF4444; font-size: 12px;")
                if reset_button:
                    self.btn_cloak_update.setText("Install CloakBrowser")
                return
            except Exception:
                pass

        # ── Get binary info ──
        bin_version = "unknown"
        installed = False
        try:
            import importlib
            for _mod in list(sys.modules.keys()):
                if _mod == "cloakbrowser" or _mod.startswith("cloakbrowser."):
                    del sys.modules[_mod]
            importlib.invalidate_caches()
            cb = importlib.import_module("cloakbrowser")
            binary_info = getattr(cb, "binary_info", None)
            if callable(binary_info):
                info = binary_info() or {}
                bin_version = str(info.get("version") or "unknown")
                installed = bool(info.get("installed"))
        except Exception:
            pass

        if installed:
            self.lbl_cloak_version.setText(f"CloakBrowser v{pkg_version} | Binary: {bin_version}")
            self.lbl_cloak_version.setStyleSheet("color: #22C55E; font-size: 12px;")
        elif pkg_version != "unknown":
            self.lbl_cloak_version.setText(f"CloakBrowser v{pkg_version} | Binary: not downloaded")
            self.lbl_cloak_version.setStyleSheet("color: #F59E0B; font-size: 12px;")
        else:
            self.lbl_cloak_version.setText("CloakBrowser: not installed")
            self.lbl_cloak_version.setStyleSheet("color: #EF4444; font-size: 12px;")

        if reset_button:
            self.btn_cloak_update.setText("Check for Updates")

    def _auto_check_cloak_on_startup(self):
        self._refresh_cloak_version_display()

    def _on_cloak_update_clicked(self):
        if getattr(self, "_cloak_update_worker", None) is not None and self._cloak_update_worker.isRunning():
            return

        install_mode = False
        try:
            import importlib

            importlib.import_module("cloakbrowser")
            self.btn_cloak_update.setText("Check for Updates")
        except ImportError:
            install_mode = True

        self.btn_cloak_update.setEnabled(False)
        self.btn_cloak_update.setText("Updating...")
        self.lbl_cloak_update_status.setVisible(True)
        self.lbl_cloak_update_status.setText("Starting update...")
        self.lbl_cloak_update_status.setStyleSheet("color: #60A5FA; font-size: 11px;")
        # Reset + show progress bar (initially 0%)
        if hasattr(self, "cloak_progress_wrap"):
            self.cloak_progress_bar.setValue(0)
            self.lbl_cloak_progress_text.setText("0 / 0 MB")
            self.cloak_progress_wrap.setVisible(True)

        self._cloak_update_worker = CloakUpdateWorker(install_mode=install_mode, parent=self)
        self._cloak_update_worker.status_changed.connect(self._on_cloak_update_status)
        self._cloak_update_worker.progress_changed.connect(self._on_cloak_download_progress)
        self._cloak_update_worker.finished.connect(self._on_cloak_update_finished)
        self._cloak_update_worker.start()

    def _on_cloak_update_status(self, message, color):
        message = str(message or "").encode("ascii", "ignore").decode().strip() or str(message or "")
        self.lbl_cloak_update_status.setText(str(message or ""))
        self.lbl_cloak_update_status.setStyleSheet(f"color: {color}; font-size: 11px;")
        self.lbl_cloak_update_status.setVisible(True)

    def _on_cloak_download_progress(self, percent, done_mb, total_mb):
        """Live progress update from CloakUpdateWorker log hook."""
        if hasattr(self, "cloak_progress_bar"):
            try:
                self.cloak_progress_bar.setValue(int(percent))
            except Exception:
                pass
        if hasattr(self, "lbl_cloak_progress_text"):
            try:
                self.lbl_cloak_progress_text.setText(f"{int(done_mb)} / {int(total_mb)} MB")
            except Exception:
                pass

    def _on_cloak_update_finished(self, success, message):
        message = str(message or "").encode("ascii", "ignore").decode().strip() or str(message or "")
        self.btn_cloak_update.setEnabled(True)
        self._refresh_cloak_version_display()
        # Auto-hide progress bar after completion
        if hasattr(self, "cloak_progress_wrap"):
            QTimer.singleShot(2500, lambda: self.cloak_progress_wrap.setVisible(False))
        if success:
            self.btn_cloak_update.setText("✅ Up to Date")
            self.lbl_cloak_update_status.setText(f"✅ {message}")
            self.lbl_cloak_update_status.setStyleSheet("color: #22C55E; font-size: 11px;")
            self.btn_cloak_update.setText("Up to Date")
            self.lbl_cloak_update_status.setText(f"Success: {message}")
        else:
            self.btn_cloak_update.setText("❌ Update Failed")
            self.lbl_cloak_update_status.setText(f"❌ {message}")
            self.lbl_cloak_update_status.setStyleSheet("color: #EF4444; font-size: 11px;")
            self.btn_cloak_update.setText("Update Failed")
            self.lbl_cloak_update_status.setText(f"Failed: {message}")

        QTimer.singleShot(5000, self._reset_cloak_update_button)
        QTimer.singleShot(10000, lambda: self.lbl_cloak_update_status.setVisible(False))
        self._cloak_update_worker = None

    def _reset_cloak_update_button(self):
        if not hasattr(self, "btn_cloak_update") or not self.btn_cloak_update.isEnabled():
            return
        try:
            import importlib

            importlib.import_module("cloakbrowser")
            self.btn_cloak_update.setText("Check for Updates")
        except ImportError:
            self.btn_cloak_update.setText("Install CloakBrowser")
        return
        try:
            import importlib

            importlib.import_module("cloakbrowser")
            self.btn_cloak_update.setText("🔄 Check for Updates")
        except ImportError:
            self.btn_cloak_update.setText("📦 Install CloakBrowser")

    def _on_mode_tab_changed(self, _index):
        self._adjust_mode_tabs_height()
        self._sync_sidebar_selection()
        self._scroll_active_mode_tab_to_top()
        if not getattr(self, "_pending_settings_sync_ready", False):
            return
        self._sync_generation_mode_ui()

    def _scroll_active_mode_tab_to_top(self):
        if not hasattr(self, "mode_tabs"):
            return
        scroll = self._mode_tab_scrolls.get(self.mode_tabs.currentWidget())
        if scroll is None:
            return
        QTimer.singleShot(0, lambda sb=scroll.verticalScrollBar(): sb.setValue(0))

    def _remove_stray_mode_tab_buttons(self):
        if not hasattr(self, "mode_tabs"):
            return
        for tab_widget in (
            getattr(self, "mode_tab_image", None),
            getattr(self, "mode_tab_t2v", None),
            getattr(self, "mode_tab_ref", None),
            getattr(self, "mode_tab_frames", None),
            getattr(self, "mode_tab_pipeline", None),
        ):
            if tab_widget is None:
                continue
            for button in tab_widget.findChildren(QPushButton):
                text = " ".join(str(button.text() or "").strip().split()).lower()
                if text not in {"start", "start automation"}:
                    continue
                if hasattr(self, "btn_start") and button is self.btn_start:
                    continue
                parent = button.parentWidget()
                if parent is not None and parent.layout() is not None:
                    parent.layout().removeWidget(button)
                button.hide()
                button.setParent(None)
                button.deleteLater()

    def _failed_stat_button_style(self, has_failures):
        if has_failures:
            return (
                "QPushButton { background-color: #2D1B1B; color: #EF4444; font-size: 36px; font-weight: 700; "
                "border: 2px solid #EF4444; border-radius: 8px; text-align: left; padding: 16px; } "
                "QPushButton:hover { background-color: #3D2020; }"
            )
        return (
            "QPushButton { background-color: #1E293B; color: #EF4444; font-size: 36px; font-weight: 700; "
            "border: 2px solid #1E293B; border-radius: 8px; text-align: left; padding: 16px; } "
            "QPushButton:hover { border: 2px solid #EF4444; background-color: #2D1B1B; }"
        )

    def _go_to_failed_tab(self):
        if not hasattr(self, "tabs"):
            return
        for index in range(self.tabs.count()):
            if self.tabs.tabText(index) == "Failed Jobs":
                self.tabs.setCurrentIndex(index)
                break

    def _update_session_stats_label(self, generated_count=None):
        if not hasattr(self, "lbl_session_stats"):
            return
        try:
            count = max(0, int(generated_count if generated_count is not None else 0))
        except Exception:
            count = 0
        self.lbl_session_stats.setText(f"Session: {count} images generated")
        if hasattr(self, "sidebar"):
            pending = int(self.stat_pending.text()) if hasattr(self, "stat_pending") else 0
            running = int(self.stat_running.text()) if hasattr(self, "stat_running") else 0
            done = int(self.stat_completed.text()) if hasattr(self, "stat_completed") else count
            failed = int(self.stat_failed.text()) if hasattr(self, "stat_failed") else 0
            self.sidebar.update_stats(pending, running, done, failed, count)

    def _note_terminal_job(self, job_id, status):
        job_key = str(job_id or "").strip()
        status_text = str(status or "").strip().lower()
        if not job_key:
            return
        if status_text in ("completed", "failed"):
            previous = self._terminal_job_states.get(job_key)
            if previous != status_text:
                self._completion_times.append(time.time())
                if len(self._completion_times) > 30:
                    self._completion_times = self._completion_times[-30:]
            self._terminal_job_states[job_key] = status_text
        else:
            self._terminal_job_states.pop(job_key, None)

    def _on_generation_started(self):
        self._generation_start_time = time.time()
        self._completion_times = []
        self._terminal_job_states = {}
        self._update_progress_display()

    def _on_queue_stopped(self):
        if hasattr(self, "lbl_speed"):
            self.lbl_speed.setText("Speed: --")
        if hasattr(self, "lbl_eta"):
            self.lbl_eta.setText("ETA: --")
        self._completion_times = []
        self._generation_start_time = None
        self._terminal_job_states = {}

    def _update_progress_display(self, jobs=None):
        if not hasattr(self, "overall_progress"):
            return

        jobs = list(jobs if jobs is not None else (self._latest_queue_jobs or []))
        total = len(jobs)
        done = 0
        failed = 0
        current_terminal = {}

        for job in jobs:
            status = str(job.get("status") or "").strip().lower()
            job_id = str(job.get("id") or job.get("job_id") or "").strip()
            if status == "completed":
                done += 1
            elif status == "failed":
                failed += 1
            if job_id:
                current_terminal[job_id] = status
                self._note_terminal_job(job_id, status)

        stale_ids = [job_id for job_id in self._terminal_job_states if job_id not in current_terminal]
        for job_id in stale_ids:
            self._terminal_job_states.pop(job_id, None)

        completed = done + failed
        self._update_session_stats_label(done)

        if total <= 0:
            self.overall_progress.setValue(0)
            self.lbl_progress_text.setText("0/0 (0%)")
            self.lbl_speed.setText("Speed: --")
            self.lbl_eta.setText("ETA: --")
            return

        percent = int((completed / total) * 100) if total else 0
        self.overall_progress.setValue(percent)
        self.lbl_progress_text.setText(f"{done}/{total} ({percent}%)")

        if len(self._completion_times) >= 2:
            time_span = max(0.0, self._completion_times[-1] - self._completion_times[0])
            if time_span > 0:
                images_per_sec = (len(self._completion_times) - 1) / time_span
                images_per_min = images_per_sec * 60.0
                self.lbl_speed.setText(f"Speed: ~{images_per_min:.1f} img/min")
                remaining = max(0, total - completed)
                if images_per_sec > 0 and remaining > 0:
                    eta_seconds = remaining / images_per_sec
                    if eta_seconds < 60:
                        self.lbl_eta.setText(f"ETA: ~{int(eta_seconds)}s")
                    elif eta_seconds < 3600:
                        self.lbl_eta.setText(f"ETA: ~{int(eta_seconds / 60)}m")
                    else:
                        hours = int(eta_seconds / 3600)
                        mins = int((eta_seconds % 3600) / 60)
                        self.lbl_eta.setText(f"ETA: ~{hours}h {mins}m")
                elif remaining <= 0:
                    self.lbl_eta.setText("ETA: complete")
                else:
                    self.lbl_eta.setText("ETA: calculating...")
            else:
                self.lbl_speed.setText("Speed: calculating...")
                self.lbl_eta.setText("ETA: calculating...")
        elif self._generation_start_time and done > 0:
            elapsed = max(0.0, time.time() - self._generation_start_time)
            if elapsed > 0:
                images_per_min = (done / elapsed) * 60.0
                self.lbl_speed.setText(f"Speed: ~{images_per_min:.1f} img/min")
                remaining = max(0, total - completed)
                if remaining <= 0:
                    self.lbl_eta.setText("ETA: complete")
                else:
                    eta_seconds = (remaining / max(1, done)) * elapsed
                    if eta_seconds < 60:
                        self.lbl_eta.setText(f"ETA: ~{int(eta_seconds)}s")
                    elif eta_seconds < 3600:
                        self.lbl_eta.setText(f"ETA: ~{int(eta_seconds / 60)}m")
                    else:
                        hours = int(eta_seconds / 3600)
                        mins = int((eta_seconds % 3600) / 60)
                        self.lbl_eta.setText(f"ETA: ~{hours}h {mins}m")
            else:
                self.lbl_speed.setText("Speed: --")
                self.lbl_eta.setText("ETA: --")
        else:
            self.lbl_speed.setText("Speed: --")
            self.lbl_eta.setText("ETA: --")

    def _refresh_dashboard_stats(self, jobs=None):
        if jobs is None:
            jobs = self._latest_queue_jobs
        counts = {"pending": 0, "running": 0, "completed": 0, "failed": 0}
        for job in jobs:
            status = str(job.get("status") or "").strip().lower()
            if status in counts:
                counts[status] += 1

        if hasattr(self, "stat_pending"):
            self.stat_pending.setText(str(counts["pending"]))
        if hasattr(self, "stat_running"):
            self.stat_running.setText(str(counts["running"]))
        if hasattr(self, "stat_completed"):
            self.stat_completed.setText(str(counts["completed"]))
        if hasattr(self, "stat_failed"):
            self.stat_failed.setText(str(counts["failed"]))
        if hasattr(self, "btn_failed_count"):
            self.btn_failed_count.setStyleSheet(self._failed_stat_button_style(counts["failed"] > 0))
        self._update_session_stats_label(counts["completed"])
        self._update_progress_display(jobs)
        if hasattr(self, "sidebar"):
            self.sidebar.update_stats(
                counts["pending"],
                counts["running"],
                counts["completed"],
                counts["failed"],
                counts["completed"],
            )

    def _adjust_mode_tabs_height(self):
        if not hasattr(self, "mode_tabs"):
            return
        compact_screen = self.height() < 800
        preferred_top = 180 if compact_screen else 220
        self.mode_tabs.setMinimumHeight(preferred_top)
        self.mode_tabs.setMaximumHeight(16777215)
        if hasattr(self, "dashboard_body_splitter"):
            sizes = self.dashboard_body_splitter.sizes()
            if sizes and sizes[0] < preferred_top:
                total = max(sum(sizes), preferred_top + 260)
                self.dashboard_body_splitter.setSizes([preferred_top, max(260, total - preferred_top)])

    def _update_runtime_badges(self):
        if hasattr(self, "lbl_runtime_mode"):
            active_label = self.mode_tabs.tabText(self.mode_tabs.currentIndex()) if hasattr(self, "mode_tabs") else "Image"
            self.lbl_runtime_mode.setText(f"Mode: {active_label}")

        if hasattr(self, "lbl_runtime_parallel"):
            selected = self._current_parallel_slots()
            self.lbl_runtime_parallel.setText(f"Parallel: {int(selected or 1)}/account")

        self._update_session_stats_label(self.stat_completed.text() if hasattr(self, "stat_completed") else 0)

    def _update_queue_status_label(self):
        if not hasattr(self, "lbl_queue_status"):
            return

        if self.queue_stopping:
            text = "Queue: STOPPING"
            color = "#EF4444"
            background = "#1F1A2A"
            border = "#EF4444"
        elif self.queue_paused:
            text = "Queue: PAUSED"
            color = "#F59E0B"
            background = "#1C1E2A"
            border = "#F59E0B"
        elif self.queue_running:
            text = "Queue: RUNNING"
            color = "#22C55E"
            background = "#111F2E"
            border = "#22C55E"
        else:
            text = "Queue: STOPPED"
            color = "#64748B"
            background = "#1D2535"
            border = "#475569"

        self.lbl_queue_status.setText(text)
        self.lbl_queue_status.setStyleSheet(
            f"color: {color}; font-weight: 700; font-size: 12px; "
            f"padding: 6px 12px; background: {background}; border: 1px solid {border}; border-radius: 8px;"
        )
        self._sync_sidebar_selection()

    def _set_queue_controls_state(self, state):
        state = str(state or "stopped").strip().lower()
        is_running = state in ("running", "paused", "stopping")
        is_paused = state == "paused"
        is_stopping = state == "stopping"

        self.btn_start.setEnabled(not is_running)
        self.btn_pause.setVisible(not is_paused)
        self.btn_pause.setEnabled(state == "running")
        self.btn_resume.setVisible(is_paused)
        self.btn_resume.setEnabled(is_paused)
        self.btn_stop.setEnabled(state in ("running", "paused"))
        if hasattr(self, "btn_force_stop_clear"):
            self.btn_force_stop_clear.setEnabled(not is_stopping)

        self.queue_running = is_running
        self.queue_paused = is_paused
        self.queue_stopping = is_stopping
        self._update_queue_status_label()

    def _estimate_video_credits(self, prompt_count=1, output_count=1, upscale="none", video_model=""):
        model_text = str(video_model or "").strip().lower()
        if "lower pri" in model_text or "lower priority" in model_text or "relaxed" in model_text:
            base_credits = 0
        elif "quality" in model_text:
            base_credits = 100
        else:
            base_credits = 10

        per_video = base_credits + (50 if str(upscale or "none").strip().lower() == "4k" else 0)
        return max(1, int(prompt_count or 1)) * max(1, int(output_count or 1)) * per_video

    def _build_credits_estimate_text(self):
        current_settings = self._current_generation_settings()
        job_type = str(current_settings.get("job_type") or "image").strip().lower()
        if job_type == "image":
            return "Images: ~0 credits"

        output_count = max(1, int(current_settings.get("video_output_count") or current_settings.get("output_count") or 1))
        upscale = str(current_settings.get("video_upscale") or "none").strip().lower()
        video_model = str(current_settings.get("video_model") or current_settings.get("model") or "").strip()
        total = self._estimate_video_credits(
            prompt_count=1,
            output_count=output_count,
            upscale=upscale,
            video_model=video_model,
        )

        if job_type == "pipeline":
            return f"~{total} credits per prompt (image free + video {total})"

        if upscale == "1080p":
            suffix = " (1080p upscale free)"
        elif upscale == "4k":
            suffix = f" (incl. {50 * output_count} for 4K upscale)"
        else:
            suffix = ""
        return f"~{total} credits per prompt{suffix}"

    def _sync_pending_queue_jobs_to_current_settings(self):
        if not getattr(self, "_pending_settings_sync_ready", False):
            return 0

        current_settings = self._current_generation_settings()
        self._start_background_task(
            update_pending_jobs_generation_settings,
            current_settings["model"],
            current_settings["aspect_ratio"],
            current_settings["output_count"],
            current_settings["ref_path"],
            ref_paths=current_settings.get("ref_paths"),
            job_type=current_settings["job_type"],
            video_model=current_settings["video_model"],
            video_sub_mode=current_settings["video_sub_mode"],
            video_ratio=current_settings.get("video_ratio", ""),
            video_prompt=current_settings.get("video_prompt", ""),
            video_upscale=current_settings["video_upscale"],
            video_length=current_settings.get("video_length", 10),
            video_output_count=current_settings["video_output_count"],
            start_image_path=current_settings["start_image_path"],
            end_image_path=current_settings["end_image_path"],
            filter_job_type=current_settings["job_type"],
            filter_video_sub_mode=(
                current_settings["video_sub_mode"]
                if current_settings["job_type"] in ("video", "pipeline")
                else None
            ),
            on_finished=self._on_pending_settings_sync_finished,
        )
        return 0

    def _on_pending_settings_sync_finished(self, updated_count):
        try:
            updated_count = int(updated_count or 0)
        except Exception:
            updated_count = 0
        if updated_count > 0:
            self.load_queue_table()
        return updated_count

    def _on_generation_settings_changed(self):
        self._update_runtime_badges()
        self._sync_pending_queue_jobs_to_current_settings()

    def _set_remaining_credits(self, credits):
        _ = credits
        return

    def append_log(self, msg):
        text = str(msg or "")
        try:
            import os as _os
            _dbgdir = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))), "data")
            _os.makedirs(_dbgdir, exist_ok=True)
            with open(_os.path.join(_dbgdir, "app_debug.log"), "a", encoding="utf-8") as _fh:
                _fh.write(text + "\n")
        except Exception:
            pass
        marker = "[CREDITS] Remaining:"
        if marker in text:
            try:
                value_text = text.split(marker, 1)[1].strip().split()[0].replace(",", "")
                self._set_remaining_credits(value_text)
            except Exception:
                pass
        if hasattr(self, "log_buffer"):
            self.log_buffer.append(text)
            return
        self.logs_output.append(text)

    def _on_clean_profiles(self):
        """Clean all account browser profiles — remove junk, keep cookies."""
        from PySide6.QtWidgets import QMessageBox
        try:
            from src.core.profile_cleaner import clean_profile, clean_derived_profiles
            from src.core.app_paths import get_sessions_dir

            sessions_dir = str(get_sessions_dir())
            total_deleted = 0
            total_freed = 0

            if os.path.isdir(sessions_dir):
                for name in os.listdir(sessions_dir):
                    path = os.path.join(sessions_dir, name)
                    if os.path.isdir(path):
                        d, f = clean_profile(path)
                        total_deleted += d
                        total_freed += f
                        d2, f2 = clean_derived_profiles(path)
                        total_deleted += d2
                        total_freed += f2

            freed_mb = total_freed / (1024 * 1024)

            if total_deleted > 0:
                msg = f"Cleaned {total_deleted} items\nFreed {freed_mb:.1f} MB\n\nLogin sessions preserved."
                self.append_log(f"[CLEAN] {msg}")
            else:
                msg = "All browser profiles are already clean.\nNo junk data found to remove."
                self.append_log("[CLEAN] All profiles already clean.")

            # Show popup — use None parent to avoid widget hierarchy issues
            box = QMessageBox()
            box.setWindowTitle("Profile Cleaner")
            box.setText(msg)
            box.setIcon(QMessageBox.Information)
            box.exec()

        except Exception as e:
            self.append_log(f"[CLEAN] Error: {str(e)[:100]}")
            box = QMessageBox()
            box.setWindowTitle("Clean Error")
            box.setText(f"Error cleaning profiles:\n{str(e)[:200]}")
            box.setIcon(QMessageBox.Warning)
            box.exec()

    def save_settings(self):
        slots = int(self.spin_slots_per_account.value())
        stagger = round(float(self.spin_same_account_stagger.value()), 1)
        global_stagger_min = round(float(self.spin_global_stagger_min.value()), 1)
        global_stagger_max = round(float(self.spin_global_stagger_max.value()), 1)
        if global_stagger_max < global_stagger_min:
            global_stagger_max = global_stagger_min
            self.spin_global_stagger_max.setValue(global_stagger_max)
        recaptcha_cooldown = int(self.spin_recaptcha_cooldown.value())
        max_retries = int(self.spin_max_retries.value())
        retry_base_delay = int(self.spin_retry_base_delay.value())
        auto_refresh_after_jobs = int(self.spin_auto_refresh_after_jobs.value())
        auto_restart_fail_threshold = int(self.spin_restart_threshold.value())
        auto_restart_fail_window = int(self.spin_restart_window.value())
        auto_restart_cooldown = int(self.spin_restart_cooldown.value())
        profile_clone_enabled = self.chk_profile_clone.isChecked()
        image_execution_mode = "api_only"
        browser_mode = self.cmb_browser_mode.currentData() or "headless"
        chrome_display = self.cmb_chrome_display.currentData() or "visible"
        cloak_display = self.cmb_cloak_display.currentData() or "headless"
        random_fingerprint_enabled = self.chk_random_fingerprint.isChecked()
        cookie_warmup_enabled = self.chk_cookie_warmup.isChecked()
        light_warmup_enabled = self.chk_light_warmup.isChecked()
        speed_profile = self.cmb_speed_profile.currentData() or "fast"
        warmup_min = round(float(self.spin_warmup_min.value()), 1)
        warmup_max = round(float(self.spin_warmup_max.value()), 1)
        if warmup_max < warmup_min:
            warmup_max = warmup_min
            self.spin_warmup_max.setValue(warmup_max)

        if str(speed_profile).lower() == "fast":
            ref_wait_min, ref_wait_max = 0.3, 0.8
            no_ref_wait_min, no_ref_wait_max = warmup_min, warmup_max
        else:
            speed_profile = "stable"
            ref_wait_min, ref_wait_max = 0.4, 0.9
            no_ref_wait_min, no_ref_wait_max = max(0.3, warmup_min), max(0.6, warmup_max)

        default_output_dir = self._default_outputs_dir()
        selected_output_dir = os.path.abspath(os.path.expanduser(str(self.output_dir_input.text() or "").strip()))
        stored_output_dir = "" if selected_output_dir == default_output_dir else selected_output_dir
        self.output_dir_input.setText(selected_output_dir)
        self._update_runtime_badges()
        target_slots = max(1, min(40, slots))
        for selector_name in ("img_cmb_parallel", "t2v_cmb_parallel", "ref_cmb_parallel", "frm_cmb_parallel", "pipe_cmb_parallel"):
            selector = getattr(self, selector_name, None)
            if selector is None:
                continue
            idx = selector.findData(target_slots)
            if idx >= 0:
                selector.setCurrentIndex(idx)
        settings_payload = {
            "slots_per_account": str(slots),
            "same_account_stagger_seconds": str(stagger),
            "global_stagger_min_seconds": str(global_stagger_min),
            "global_stagger_max_seconds": str(global_stagger_max),
            "max_retries": str(max_retries),
            "max_auto_retries_per_job": str(max_retries),
            "retry_base_delay_seconds": str(retry_base_delay),
            "auto_retry_base_delay_seconds": str(retry_base_delay),
            "recaptcha_account_cooldown_seconds": str(recaptcha_cooldown),
            "auto_refresh_after_jobs": str(auto_refresh_after_jobs),
            "auto_restart_recap_fail_threshold": str(auto_restart_fail_threshold),
            "auto_restart_recap_fail_window": str(auto_restart_fail_window),
            "auto_restart_recap_cooldown_seconds": str(auto_restart_cooldown),
            "enable_profile_clones": "1" if profile_clone_enabled else "0",
            "api_captcha_submit_lock": "0",
            "image_execution_mode": str(image_execution_mode),
            "browser_mode": str(browser_mode),
            "chrome_display": str(chrome_display),
            "cloak_display": str(cloak_display),
            "random_fingerprint_per_session": "1" if random_fingerprint_enabled else "0",
            "cookie_warmup": "1" if cookie_warmup_enabled else "0",
            "light_warmup": "1" if light_warmup_enabled else "0",
            "speed_profile": str(speed_profile),
            "api_min_submit_gap_seconds": "0",
            "api_humanized_warmup_min_seconds": str(warmup_min),
            "api_humanized_warmup_max_seconds": str(warmup_max),
            "api_humanized_wait_ref_min_seconds": str(ref_wait_min),
            "api_humanized_wait_ref_max_seconds": str(ref_wait_max),
            "api_humanized_wait_no_ref_min_seconds": str(no_ref_wait_min),
            "api_humanized_wait_no_ref_max_seconds": str(no_ref_wait_max),
            "output_directory": stored_output_dir,
            "generation_mode": str(getattr(self, "cmb_generation_mode", None) and self.cmb_generation_mode.currentData() or "browser_per_slot"),
            "genspark_auto_prompt": "1" if (getattr(self, "img_chk_auto_prompt", None) and self.img_chk_auto_prompt.isChecked()) else "0",
            "flow_account_plan": str(getattr(self, "cmb_flow_plan", None) and self.cmb_flow_plan.currentData() or "ultra"),
        }
        # Persist Grok-specific resolution/duration from the t2v tab (used
        # as the shared default across all video sub-tabs). Per-dispatch
        # the UI still reads each tab's own combo, but the persisted
        # values keep the dropdowns consistent across restarts.
        grok_res_cmb = getattr(self, "t2v_cmb_grok_res", None)
        grok_dur_cmb = getattr(self, "t2v_cmb_grok_dur", None)
        if grok_res_cmb is not None:
            settings_payload["grok_resolution"] = str(grok_res_cmb.currentData() or "720p")
        if grok_dur_cmb is not None:
            try:
                settings_payload["grok_video_length"] = str(int(grok_dur_cmb.currentData() or 10))
            except (TypeError, ValueError):
                settings_payload["grok_video_length"] = "10"
        self._start_background_task(
            self._persist_settings_payload,
            settings_payload,
            on_finished=lambda _result: self._on_settings_saved(
                slots,
                stagger,
                global_stagger_min,
                global_stagger_max,
                max_retries,
                retry_base_delay,
                recaptcha_cooldown,
                auto_refresh_after_jobs,
                auto_restart_fail_threshold,
                auto_restart_fail_window,
                auto_restart_cooldown,
                profile_clone_enabled,
                browser_mode,
                chrome_display,
                cloak_display,
                random_fingerprint_enabled,
                cookie_warmup_enabled,
                light_warmup_enabled,
                speed_profile,
                selected_output_dir,
                warmup_min,
                warmup_max,
                ref_wait_min,
                ref_wait_max,
                no_ref_wait_min,
                no_ref_wait_max,
            ),
        )

    @staticmethod
    def _persist_settings_payload(settings_payload):
        for key, value in dict(settings_payload or {}).items():
            set_setting(str(key), str(value))
        return True

    def _on_settings_saved(
        self,
        slots,
        stagger,
        global_stagger_min,
        global_stagger_max,
        max_retries,
        retry_base_delay,
        recaptcha_cooldown,
        auto_refresh_after_jobs,
        auto_restart_fail_threshold,
        auto_restart_fail_window,
        auto_restart_cooldown,
        profile_clone_enabled,
        browser_mode,
        chrome_display,
        cloak_display,
        random_fingerprint_enabled,
        cookie_warmup_enabled,
        light_warmup_enabled,
        speed_profile,
        selected_output_dir,
        warmup_min,
        warmup_max,
        ref_wait_min,
        ref_wait_max,
        no_ref_wait_min,
        no_ref_wait_max,
    ):
        self.append_log(
            f"[SETTINGS] Saved: slots/account={slots}, stagger={stagger:.1f}s, "
            f"global stagger={global_stagger_min:.1f}s-{global_stagger_max:.1f}s, "
            f"retries={max_retries}, retry base delay={retry_base_delay}s, "
            f"reCAPTCHA cooldown={recaptcha_cooldown}s, "
            f"auto-refresh every {auto_refresh_after_jobs} job(s), "
            f"auto-restart after {auto_restart_fail_threshold} reCAPTCHA fail(s) in {auto_restart_fail_window}, "
            f"restart cooldown={auto_restart_cooldown}s, "
            f"profile cloning={'on' if profile_clone_enabled else 'off'}, "
            f"image mode=api_only, browser mode={browser_mode}, chrome display={chrome_display}, "
            f"cloak display={cloak_display}, random fingerprint={'on' if random_fingerprint_enabled else 'off'}, "
            f"cookie warm-up={'on' if cookie_warmup_enabled else 'off'}, "
            f"light warm-up={'on' if light_warmup_enabled else 'off'}, speed profile={speed_profile}, "
            f"output dir={selected_output_dir}."
        )
        self.append_log(
            f"[SETTINGS] Applied speed preset: warmup={warmup_min:.1f}-{warmup_max:.1f}s, "
            f"pre-submit(ref)={ref_wait_min:.1f}-{ref_wait_max:.1f}s, "
            f"pre-submit(no-ref)={no_ref_wait_min:.1f}-{no_ref_wait_max:.1f}s."
        )
        if self.queue_manager and self.queue_manager.isRunning():
            self.append_log("[SETTINGS] Restart Queue Manager to apply new slot settings.")
        self._toast_success("Settings Saved", "Automation settings saved successfully.")
        
    def start_login(self, checked=False, login_target="flow"):
        login_target = str(login_target or "flow").strip().lower()
        acc_name = self.acc_name_input.text().strip()
        proxy_value = self.acc_proxy_input.text().strip()
        log_target = acc_name if acc_name else "AUTO-GMAIL"
        site_label = "dola.com" if login_target == "dola" else "Google"
        self.append_log(f"Starting {site_label} login flow for {log_target}. A browser will open...")
        self._reset_download_progress_widget()
        self._pending_login_add = None
        self.btn_login.setEnabled(False)
        if hasattr(self, "btn_login_dola"):
            self.btn_login_dola.setEnabled(False)
        self.acc_name_input.setEnabled(False)
        self.acc_proxy_input.setEnabled(False)

        self.login_worker = LoginWorker(acc_name, proxy=proxy_value, login_target=login_target)
        self.login_worker.log_msg.connect(self.append_log, Qt.QueuedConnection)
        self.login_worker.download_progress.connect(self._on_download_progress, Qt.QueuedConnection)
        self.login_worker.download_complete.connect(self._on_download_complete, Qt.QueuedConnection)
        self.login_worker.session_saved.connect(self.on_login_session_saved, Qt.QueuedConnection)
        self.login_worker.warmup_progress.connect(self.warmup_progress_signal.emit, Qt.QueuedConnection)
        self.login_worker.warmup_complete.connect(self.warmup_complete_signal.emit, Qt.QueuedConnection)
        self.login_worker.finished_login.connect(self.on_login_finished)
        self.login_worker.start()

    def on_login_session_saved(self, name, session_path, detected_email):
        proxy_value = str(getattr(self.login_worker, "proxy", "") or "").strip() if self.login_worker else ""
        self._pending_login_add = (str(name), str(session_path))
        self._start_background_task(
            add_account,
            name,
            session_path,
            proxy_value,
            on_finished=lambda added, name=name, detected_email=detected_email: self._on_account_added(
                name,
                detected_email,
                added,
            ),
        )
        
    def on_login_finished(self, name, session_path, detected_email):
        proxy_value = str(getattr(self.login_worker, "proxy", "") or "").strip() if self.login_worker else ""
        self.login_worker = None
        self.acc_name_input.clear()
        self.acc_proxy_input.clear()
        self.btn_login.setEnabled(True)
        if hasattr(self, "btn_login_dola"):
            self.btn_login_dola.setEnabled(True)
        self.acc_name_input.setEnabled(True)
        self.acc_proxy_input.setEnabled(True)
        if self._pending_login_add == (str(name), str(session_path)):
            self._pending_login_add = None
            return
        self._start_background_task(
            add_account,
            name,
            session_path,
            proxy_value,
            on_finished=lambda added, name=name, detected_email=detected_email: self._on_account_added(
                name,
                detected_email,
                added,
            ),
        )

    def refresh_accounts(self):
        self.load_accounts(after_load=lambda: self.start_login_status_check())

    def _on_account_added(self, name, detected_email, added):
        if added:
            if detected_email:
                self.append_log(f"Account '{name}' auto-detected from Google login and added successfully.")
            else:
                self.append_log(f"Account '{name}' added successfully to database.")
        else:
            self.append_log(f"Account '{name}' already exists. Session was not added as duplicate.")
            QMessageBox.warning(
                self,
                "Duplicate Account",
                f"Account '{name}' already exists in Account Manager."
            )
        self.refresh_accounts()

    def _reset_download_progress_widget(self):
        if not hasattr(self, "download_widget"):
            return
        self.download_widget.setVisible(False)
        self.download_progress.setValue(0)
        self.download_percent.setText("0%")
        self.download_label.setText("Downloading CloakBrowser binary...")
        self.download_label.setStyleSheet("color: #60A5FA; font-weight: 600;")
        self.download_progress.setStyleSheet(
            """
            QProgressBar {
                border: 1px solid #334155;
                border-radius: 4px;
                background-color: #1E293B;
                text-align: center;
                color: white;
                font-weight: 600;
            }
            QProgressBar::chunk {
                background-color: #3B82F6;
                border-radius: 3px;
            }
            """
        )

    def _on_download_progress(self, percent, status_text):
        self.download_widget.setVisible(True)
        safe_percent = max(0, min(100, int(percent or 0)))
        self.download_progress.setValue(safe_percent)
        self.download_percent.setText(f"{safe_percent}%")
        self.download_label.setText(str(status_text or "Downloading CloakBrowser..."))
        self.btn_login.setEnabled(False)
        self.btn_login.setText("Downloading CloakBrowser...")

    def _on_download_complete(self, success, message):
        self.download_widget.setVisible(True)
        if success:
            self.download_label.setText(f"OK {message}")
            self.download_label.setStyleSheet("color: #22C55E; font-weight: 600;")
            self.download_progress.setValue(100)
            self.download_percent.setText("100%")
            self.download_progress.setStyleSheet(
                """
                QProgressBar {
                    border: 1px solid #334155;
                    border-radius: 4px;
                    background-color: #1E293B;
                    text-align: center;
                    color: white;
                    font-weight: 600;
                }
                QProgressBar::chunk {
                    background-color: #22C55E;
                    border-radius: 3px;
                }
                """
            )
            self.btn_login.setText("Waiting for Login Browser...")
            QTimer.singleShot(5000, lambda: self.download_widget.setVisible(False))
        else:
            self.download_label.setText(f"Failed {message}")
            self.download_label.setStyleSheet("color: #EF4444; font-weight: 600;")
            self.btn_login.setText("Login to Google (New Browser)")

    def load_accounts(self, after_load=None):
        self.acc_table.setRowCount(0)
        self._start_background_task(
            self._load_accounts_payload,
            on_finished=lambda payload, after_load=after_load: self._apply_accounts_payload(payload, after_load=after_load),
        )

    @staticmethod
    def _load_accounts_payload():
        accs = get_accounts()
        rename_count = 0
        display_map = {}
        for acc in accs:
            current_name = str(acc.get("name") or "").strip()
            if current_name and "@" in current_name:
                display_map[acc.get("id")] = current_name
                continue
            detected = AccountManager.detect_email_from_session_dir(acc.get("session_path"))
            if detected and detected != current_name:
                if update_account_name_by_id(acc.get("id"), detected):
                    acc["name"] = detected
                    display_map[acc.get("id")] = detected
                    rename_count += 1
                else:
                    alias = current_name or "unnamed"
                    display_map[acc.get("id")] = f"{detected} (alias: {alias})"
            else:
                display_map[acc.get("id")] = current_name

        if rename_count > 0:
            accs = get_accounts()
            for acc in accs:
                name = str(acc.get("name") or "").strip()
                if acc.get("id") not in display_map:
                    display_map[acc.get("id")] = name
        return {"accounts": accs, "display_map": display_map, "rename_count": rename_count}

    def _apply_accounts_payload(self, payload, after_load=None):
        self._loading_accounts_table = True
        self.acc_table.setRowCount(0)
        self.warmup_widgets = {}
        payload = dict(payload or {})
        accs = list(payload.get("accounts") or [])
        display_map = dict(payload.get("display_map") or {})
        rename_count = int(payload.get("rename_count") or 0)
        self._latest_accounts = accs
        if rename_count > 0:
            self.append_log(f"[ACCOUNTS] Auto-updated {rename_count} account name(s) from session Gmail.")

        live_ids = {int(acc.get("id") or 0) for acc in accs}
        self.account_login_state = {
            int(account_id): value
            for account_id, value in self.account_login_state.items()
            if int(account_id) in live_ids
        }

        try:
            for i, acc in enumerate(accs):
                self.acc_table.insertRow(i)
                db_id = int(acc.get("id") or 0)
                id_item = QTableWidgetItem(str(i + 1))
                id_item.setData(Qt.UserRole, db_id)
                id_item.setTextAlignment(Qt.AlignCenter)
                real_name = str(acc.get('name') or "")
                display_name = str(display_map.get(acc.get("id"), real_name) or "")
                # Keep the item for data storage ONLY (runtime reads from Qt.UserRole).
                # Text MUST be empty — actual display is done by setCellWidget below,
                # otherwise the item's text renders UNDER the widget (double text bug).
                name_item = QTableWidgetItem("")
                name_item.setData(Qt.UserRole, real_name)
                name_item.setData(Qt.UserRole + 1, str(acc.get("session_path") or ""))
                saved_status_item = QTableWidgetItem("")

                for item in (id_item, name_item, saved_status_item):
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)

                self.acc_table.setItem(i, 0, id_item)
                self.acc_table.setItem(i, 1, name_item)
                self.acc_table.setItem(i, 4, saved_status_item)

                # Professional 2-line account name widget (name + meta)
                self._set_account_name_cell(i, db_id, display_name, real_name)

                self._set_account_proxy_cell(i, db_id, real_name or display_name, acc.get("proxy"))
                self._set_account_login_status_cell(i, db_id, self.account_login_state.get(db_id))
                self._set_account_saved_status_cell(i, acc)
                self._get_or_create_table_item(self.acc_table, i, 5, "idle")
                self._get_or_create_table_item(self.acc_table, i, 6, "--")
                self._get_or_create_table_item(self.acc_table, i, 7, "0/1")
                self._set_account_detail_cell(i, real_name, "Queue stopped")
                self._add_account_action_buttons(i, db_id, real_name or display_name)
        finally:
            self._loading_accounts_table = False
        self._refresh_account_runtime_cells()
        self._refresh_login_statuses()
        if callable(after_load):
            after_load()

    def start_login_status_check(self, account_ids=None):
        accounts = list(self._latest_accounts or [])
        if account_ids is not None:
            wanted = {int(account_id) for account_id in account_ids}
            accounts = [acc for acc in accounts if int(acc.get("id") or 0) in wanted]

        if not accounts:
            self.btn_refresh_accs.setEnabled(True)
            self.btn_refresh_accs.setText("Refresh List")
            self._refresh_account_overview()
            return

        if self.login_check_worker and self.login_check_worker.isRunning():
            self.append_log("[ACCOUNTS] Login status check already running.")
            return

        self.btn_refresh_accs.setEnabled(False)
        self.btn_refresh_accs.setText("Checking...")

        for account in accounts:
            account_id = int(account.get("id") or 0)
            self.account_login_state[account_id] = {"state": "checking"}
            row = self._find_account_row(account_id)
            if row >= 0:
                self._set_account_login_status_cell(row, account_id, self.account_login_state[account_id])

        self._refresh_account_overview()
        self.login_check_worker = LoginCheckWorker(accounts)
        self.login_check_worker.single_result.connect(self._on_single_account_checked)
        self.login_check_worker.result_ready.connect(self._on_login_check_complete)
        self.login_check_worker.start()

    def _on_single_account_checked(self, account_id, status):
        normalized = self._normalize_login_status(status)
        self.account_login_state[int(account_id)] = normalized
        row = self._find_account_row(account_id)
        if row >= 0:
            self._set_account_login_status_cell(row, account_id, normalized)
        self._refresh_account_overview()

    def _on_login_check_complete(self, all_results):
        self.btn_refresh_accs.setEnabled(True)
        self.btn_refresh_accs.setText("Refresh List")
        self.login_check_worker = None

        logged_in = 0
        total = 0
        for account_id, status in dict(all_results or {}).items():
            total += 1
            normalized = self._normalize_login_status(status)
            self.account_login_state[int(account_id)] = normalized
            if normalized.get("logged_in"):
                logged_in += 1

        logged_out = max(0, total - logged_in)
        if total > 0:
            if logged_out > 0:
                self.append_log(
                    f"[ACCOUNTS] Login check: {logged_in}/{total} logged in, {logged_out} need re-login."
                )
            else:
                self.append_log(f"[ACCOUNTS] All {total} accounts logged in.")
        self._refresh_account_overview()

    def _relogin_account(self, account_id):
        self._start_account_session_refresh(account_id, action_label="Re-login")

    def on_relogin_finished(self, account_id, name, session_path, detected_email):
        self.relogin_worker = None
        self._start_background_task(
            self._persist_relogin_result,
            account_id,
            session_path,
            name,
            on_finished=lambda updated, account_id=account_id, name=name, detected_email=detected_email: self._on_relogin_saved(
                account_id,
                name,
                detected_email,
                updated,
            ),
        )

    @staticmethod
    def _persist_relogin_result(account_id, session_path, name):
        updated = update_account_session_by_id(account_id, session_path, name)
        if not updated:
            updated = update_account_session_by_id(account_id, session_path)
        return bool(updated)

    def _on_relogin_saved(self, account_id, name, detected_email, updated):
        if updated:
            if detected_email:
                self.append_log(f"[ACCOUNTS] Re-login complete for '{name}'. Session refreshed.")
            else:
                self.append_log(f"[ACCOUNTS] Re-login complete for '{name}'.")
        else:
            self.append_log(f"[ACCOUNTS] Session refreshed for '{name}', but the account name could not be updated.")
        self.load_accounts(after_load=lambda: self.start_login_status_check(account_ids=[account_id]))

    def delete_selected_account(self):
        selected = self.acc_table.selectedItems()
        if not selected:
            return
            
        row = selected[0].row()
        id_item = self.acc_table.item(row, 0)
        db_id = int(id_item.data(Qt.UserRole) or 0)
        self._delete_account_by_id(db_id)

    def _is_moderated_failed_error(self, error_text):
        text = str(error_text or "").strip()
        if not text:
            return False
        if text.startswith("[moderated]") or text.startswith("MODERATION:"):
            return True

        text_upper = text.upper()
        moderation_tokens = (
            "PROMINENT_PERSON",
            "SAFETY_FILTER",
            "CONTENT_POLICY",
            "MODERATION",
            "FILTER_FAILED",
            "BLOCKED",
            "SEXUALLY_EXPLICIT",
            "VIOLENCE",
            "HATE_SPEECH",
            "CHILD_SAFETY",
            "HARMFUL",
            "DANGEROUS",
            "TOXIC",
        )
        return any(token in text_upper for token in moderation_tokens)

    def _update_failed_jobs_actions(self):
        row_count = self.failed_table.rowCount()
        has_rows = row_count > 0
        checked_rows = self._checked_failed_rows()
        self.btn_requeue_selected.setEnabled(bool(checked_rows))
        self.btn_requeue_selected.setToolTip(
            "Retry checked failed jobs. Moderated jobs require an edited prompt."
        )
        self.btn_retry_all_failed.setEnabled(has_rows)
        self.btn_retry_all_failed.setToolTip(
            "Retry every failed job. Moderated rows will only retry if the prompt was edited."
        )
        self.btn_copy_failed.setEnabled(has_rows)
        self.btn_clear_failed.setEnabled(has_rows)
        self.chk_select_all_failed.setEnabled(has_rows)
        self._update_failed_empty_state()

    def _update_failed_empty_state(self):
        """Show the empty-state overlay when there are zero failed jobs."""
        if not hasattr(self, "failed_empty_overlay") or not hasattr(self, "failed_table"):
            return
        has_rows = self.failed_table.rowCount() > 0
        self.failed_empty_overlay.setVisible(not has_rows)
        self.failed_table.setVisible(has_rows)

    def _checked_failed_rows(self):
        rows = []
        for row in range(self.failed_table.rowCount()):
            item = self.failed_table.item(row, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                rows.append(row)
        return rows

    def _toggle_select_all_failed(self, checked):
        if getattr(self, "_loading_failed_table", False):
            return
        self._loading_failed_table = True
        target_state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        try:
            for row in range(self.failed_table.rowCount()):
                item = self.failed_table.item(row, 0)
                if item:
                    item.setCheckState(target_state)
        finally:
            self._loading_failed_table = False
        self._update_failed_jobs_actions()

    def _sync_failed_select_all_checkbox(self):
        if not hasattr(self, "chk_select_all_failed"):
            return
        row_count = self.failed_table.rowCount()
        self.chk_select_all_failed.blockSignals(True)
        self.chk_select_all_failed.setChecked(row_count > 0 and len(self._checked_failed_rows()) == row_count)
        self.chk_select_all_failed.blockSignals(False)

    def _failed_row_job_id(self, row):
        item = self.failed_table.item(row, 0)
        return item.data(Qt.UserRole) if item else None

    def _failed_row_is_moderated(self, row):
        item = self.failed_table.item(row, 0)
        return bool(item.data(Qt.UserRole + 1)) if item else False

    def _failed_original_prompt(self, row):
        item = self.failed_table.item(row, 0)
        return str(item.data(Qt.UserRole + 2) or "") if item else ""

    def _failed_row_edited_prompt(self, row):
        prompt_item = self.failed_table.item(row, 2)
        return str(prompt_item.text() or "").strip() if prompt_item else ""

    def _apply_failed_prompt_edit_style(self, row):
        prompt_item = self.failed_table.item(row, 2)
        if not prompt_item:
            return
        edited_prompt = str(prompt_item.text() or "").strip()
        original_prompt = self._failed_original_prompt(row)
        if edited_prompt and edited_prompt != original_prompt:
            prompt_item.setBackground(QColor("#1A2744"))
        else:
            prompt_item.setBackground(QColor())

    def _on_failed_table_item_changed(self, item):
        if item is None or getattr(self, "_loading_failed_table", False):
            return
        if item.column() == 0:
            self._sync_failed_select_all_checkbox()
            self._update_failed_jobs_actions()
            return
        if item.column() != 2:
            return

        row = item.row()
        job_id = self._failed_row_job_id(row)
        original_prompt = self._failed_original_prompt(row)
        edited_prompt = str(item.text() or "").strip()
        if job_id:
            if edited_prompt and edited_prompt != original_prompt:
                self.failed_prompt_edits[job_id] = edited_prompt
            else:
                self.failed_prompt_edits.pop(job_id, None)
        self._apply_failed_prompt_edit_style(row)

    def load_failed_jobs(self, jobs=None):
        # Defensive: Qt signals sometimes pass a bool (from clicked)
        # or other non-iterable. Only treat `jobs` as data if it's
        # an actual list/tuple of dicts.
        if isinstance(jobs, (list, tuple)):
            self._populate_failed_jobs_table(jobs)
            return
        self._request_failed_jobs_refresh(force=self.tabs.currentWidget() is self.tab_failed_jobs)

    def _populate_failed_jobs_table(self, jobs):
        self._loading_failed_table = True
        self.failed_table.setRowCount(0)
        jobs = list(jobs or [])
        live_ids = {str(job.get("id") or "") for job in jobs}
        self.failed_prompt_edits = {
            job_id: prompt
            for job_id, prompt in self.failed_prompt_edits.items()
            if job_id in live_ids
        }
        self._failed_jobs_dirty = False

        try:
            for i, j in enumerate(jobs):
                self.failed_table.insertRow(i)
                is_moderated = self._is_moderated_failed_error(j.get("error"))
                job_id = str(j.get("id") or "")
                original_prompt = str(j.get("prompt") or "")
                edited_prompt = self.failed_prompt_edits.get(job_id, original_prompt)

                check_item = QTableWidgetItem("")
                check_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
                check_item.setCheckState(Qt.CheckState.Unchecked)
                check_item.setData(Qt.UserRole, job_id)
                check_item.setData(Qt.UserRole + 1, is_moderated)
                check_item.setData(Qt.UserRole + 2, original_prompt)
                self.failed_table.setItem(i, 0, check_item)

                number_item = QTableWidgetItem(str(j.get("queue_no") or i + 1))
                number_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self.failed_table.setItem(i, 1, number_item)

                prompt_item = QTableWidgetItem(edited_prompt)
                prompt_item.setToolTip("Double-click to edit prompt before retrying.")
                prompt_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEditable)
                self.failed_table.setItem(i, 2, prompt_item)
                self._apply_failed_prompt_edit_style(i)

                type_item = QTableWidgetItem(str(j.get("job_type") or "image").title())
                type_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self.failed_table.setItem(i, 3, type_item)

                error_item = QTableWidgetItem(str(j.get("error") or ""))
                error_item.setToolTip(str(j.get("error") or ""))
                error_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self.failed_table.setItem(i, 4, error_item)

                original_item = QTableWidgetItem(original_prompt)
                original_item.setToolTip(original_prompt)
                original_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self.failed_table.setItem(i, 5, original_item)

                # Status badge: RETRYING / MODERATED / FAILED
                job_status = str(j.get("status") or "failed").strip().lower()
                is_retrying = job_status in ("pending", "running") and j.get("is_retry")

                if is_retrying:
                    badge_text = "🔄 RETRYING"
                    badge_tooltip = "Job is being retried. Will disappear when successful."
                elif is_moderated:
                    badge_text = "⚠ MODERATED"
                    badge_tooltip = "Blocked by Google content filter. Edit the prompt before retrying."
                else:
                    badge_text = "✕ FAILED"
                    badge_tooltip = f"{str(j.get('job_type') or 'image').title()} job failed after queue retries."

                badge_item = QTableWidgetItem(badge_text)
                badge_item.setToolTip(badge_tooltip)
                if is_retrying:
                    badge_item.setForeground(QColor("#60A5FA"))
                    badge_item.setBackground(QColor("#0C2D5E"))
                elif is_moderated:
                    badge_item.setForeground(QColor("#FBBF24"))
                    badge_item.setBackground(QColor("#2A1F08"))
                else:
                    badge_item.setForeground(QColor("#F87171"))
                    badge_item.setBackground(QColor("#2A0F12"))
                # Bold font for emphasis
                badge_font = QFont()
                badge_font.setBold(True)
                badge_font.setPointSize(10)
                badge_item.setFont(badge_font)
                badge_item.setTextAlignment(Qt.AlignCenter)
                badge_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self.failed_table.setItem(i, 6, badge_item)
        finally:
            self._loading_failed_table = False

        self._sync_failed_select_all_checkbox()
        self._update_failed_jobs_actions()

    def _retry_failed_rows(self, rows):
        if not rows:
            self._toast_warning("No Failed Jobs Selected", "Select at least one failed job to retry.")
            return

        retried = 0
        skipped = 0
        retry_updates = []
        for row in rows:
            job_id = self._failed_row_job_id(row)
            if not job_id:
                continue

            edited_prompt = self._failed_row_edited_prompt(row)
            original_prompt = self._failed_original_prompt(row)
            is_moderated = self._failed_row_is_moderated(row)

            if not edited_prompt:
                skipped += 1
                self.append_log(f"[RETRY] Skipping job {str(job_id)[:8]} — prompt is empty.")
                continue

            if is_moderated and edited_prompt == original_prompt:
                skipped += 1
                preview = edited_prompt[:40] + ("..." if len(edited_prompt) > 40 else "")
                self.append_log(f"[RETRY] Skipping moderated job — prompt not changed: '{preview}'")
                continue

            self.failed_prompt_edits.pop(job_id, None)
            retry_updates.append(
                {
                    "job_id": job_id,
                    "prompt": edited_prompt,
                    "retry_source": "failed_tab",
                }
            )

        if retry_updates:
            self._start_background_task(
                retry_failed_jobs_to_top,
                retry_updates,
                retry_source="failed_tab",
                on_finished=lambda retried_count, skipped=skipped: self._on_failed_retry_finished(retried_count, skipped),
            )
            return

        if retried <= 0:
            QMessageBox.information(
                self,
                "Nothing Re-queued",
                "No failed jobs were re-queued. Moderated jobs need an edited prompt before retrying.",
            )
        else:
            extra = f" Skipped {skipped} job(s)." if skipped else ""
            self.append_log(
                f"[RETRY] {retried} job(s) re-queued at top with original filenames preserved.{extra}"
            )

        self.load_failed_jobs()
        self.load_queue_table()

    def _on_failed_retry_finished(self, retried, skipped):
        retried = int(retried or 0)
        if retried <= 0:
            QMessageBox.information(
                self,
                "Nothing Re-queued",
                "No failed jobs were re-queued. Moderated jobs need an edited prompt before retrying.",
            )
        else:
            extra = f" Skipped {skipped} job(s)." if skipped else ""
            self.append_log(
                f"[RETRY] {retried} job(s) re-queued at top with original filenames preserved.{extra}"
            )
        self.load_failed_jobs()
        self.load_queue_table()

    def _retry_selected_failed(self):
        self._retry_failed_rows(self._checked_failed_rows())

    def _retry_all_failed(self):
        self._retry_failed_rows(list(range(self.failed_table.rowCount())))

    def requeue_failed_job(self):
        self._retry_selected_failed()

    def copy_failed_prompts(self):
        prompts = []
        for row in range(self.failed_table.rowCount()):
            prompt_item = self.failed_table.item(row, 2)
            prompt_text = str(prompt_item.text() or "").strip() if prompt_item else ""
            if prompt_text:
                prompts.append(prompt_text)
        if not prompts:
            self._toast_warning("No Failed Prompts", "There are no failed prompts to copy.")
            return

        QApplication.clipboard().setText("\n".join(prompts))
        self.append_log(f"Copied {len(prompts)} failed prompt(s) to clipboard.")
        self._toast_success(
            "Copied to Clipboard",
            f"{len(prompts)} failed prompt(s) copied. Rewrite them and use Paste Rewritten.",
        )

    def paste_rewritten_failed_prompts(self):
        """Bulk-replace failed job prompts from clipboard text.
        Each line in clipboard becomes the new prompt for the failed
        job at the same row index. Count must match row count."""
        row_count = self.failed_table.rowCount()
        if row_count <= 0:
            self._toast_warning("No Failed Jobs", "Nothing to paste into.")
            return

        clipboard_text = QApplication.clipboard().text() or ""
        # Split by newlines, keep only non-empty lines
        lines = [ln.strip() for ln in clipboard_text.splitlines() if ln.strip()]
        if not lines:
            self._toast_warning(
                "Clipboard Empty",
                "No text in clipboard. Copy your rewritten prompts first (one per line).",
            )
            return

        if len(lines) != row_count:
            # Count mismatch — ask the user how to proceed
            proceed = self._fluent_confirm(
                "Prompt Count Mismatch",
                f"Clipboard has {len(lines)} line(s) but there are {row_count} failed job(s).\n\n"
                f"Continue anyway? Extra clipboard lines will be ignored, "
                f"and any rows beyond the clipboard count will be left unchanged.",
                confirm_label="Paste Anyway",
                cancel_label="Cancel",
            )
            if not proceed:
                return

        # Apply rewrites — walk rows in order, overwrite prompt cell
        # text. The itemChanged handler auto-tracks edits and updates
        # self.failed_prompt_edits so Retry Selected/All picks them up.
        applied = 0
        for row in range(min(row_count, len(lines))):
            prompt_item = self.failed_table.item(row, 2)
            if prompt_item is None:
                continue
            new_text = lines[row]
            if not new_text:
                continue
            prompt_item.setText(new_text)
            applied += 1

        self._toast_success(
            "Prompts Pasted",
            f"Updated {applied} failed prompt(s). Click Retry Selected or Retry All to run them.",
        )
        self.append_log(f"Pasted {applied} rewritten prompt(s) from clipboard.")
        self._update_failed_jobs_actions()

    def clear_failed_jobs_list(self):
        if self.failed_table.rowCount() <= 0:
            return
        if not self._fluent_confirm(
            "Clear Failed Jobs",
            "Remove all failed jobs from the list?",
            confirm_label="Clear",
            cancel_label="Cancel",
        ):
            return

        self._start_background_task(
            clear_failed_jobs,
            on_finished=self._on_failed_jobs_cleared,
        )

    def _on_failed_jobs_cleared(self, deleted_count):
        self.append_log(f"Cleared {int(deleted_count or 0)} failed job(s) from the dashboard.")
        self.load_failed_jobs()
        self.load_queue_table()

    def _is_supported_bulk_image_file(self, file_path):
        suffix = Path(str(file_path or "")).suffix.lower()
        return suffix in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}

    def _bulk_panel(self, mode_key):
        return self.bulk_panels.get(str(mode_key or "").strip())

    def _load_bulk_images_from_paths(self, mode_key, paths):
        panel = self._bulk_panel(mode_key)
        if not panel:
            return

        entries = []
        seen_paths = set()
        for raw_path in paths or []:
            path = Path(str(raw_path or "")).expanduser()
            if not path.exists():
                continue
            if path.is_dir():
                candidates = [item for item in path.iterdir() if item.is_file() and self._is_supported_bulk_image_file(item)]
            elif path.is_file() and self._is_supported_bulk_image_file(path):
                candidates = [path]
            else:
                candidates = []

            for item in candidates:
                resolved = str(item.resolve())
                if resolved in seen_paths:
                    continue
                seen_paths.add(resolved)
                try:
                    modified_time = float(item.stat().st_mtime)
                except Exception:
                    modified_time = 0.0
                entries.append({"path": resolved, "filename": item.name, "modified_time": modified_time})

        panel["entries"] = entries
        # Debounced refresh — for 200+ image loads, the synchronous
        # thumbnail rebuild was freezing the UI for several seconds
        # right after the file dialog closed. The 50ms delay here lets
        # the dialog finish dismissing and the UI repaint before the
        # rebuild starts; subsequent rebuilds (typing in prompts) are
        # also coalesced via the same debounce.
        self._schedule_bulk_pairing_refresh(mode_key, delay_ms=50)

    def clear_bulk_panel(self, mode_key):
        panel = self._bulk_panel(mode_key)
        if not panel:
            return
        panel["entries"] = []
        panel["prompts_input"].clear()
        self._schedule_bulk_pairing_refresh(mode_key, delay_ms=50)

    def select_bulk_image_folder(self, mode_key):
        folder_path = QFileDialog.getExistingDirectory(self, "Select Folder With Images", "")
        if folder_path:
            self._load_bulk_images_from_paths(mode_key, [folder_path])

    def select_bulk_image_files(self, mode_key):
        file_paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Select Images",
            "",
            "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif)",
        )
        if file_paths:
            self._load_bulk_images_from_paths(mode_key, file_paths)

    def _get_sorted_bulk_images(self, mode_key):
        panel = self._bulk_panel(mode_key)
        if not panel:
            return []
        images = list(panel.get("entries") or [])
        sort_mode = str(panel["sort_selector"].currentData() or "name_asc")
        if sort_mode == "name_desc":
            images.sort(key=lambda item: self._natural_key(item.get("filename")), reverse=True)
        elif sort_mode == "time_old":
            images.sort(key=lambda item: float(item.get("modified_time") or 0.0))
        elif sort_mode == "time_new":
            images.sort(key=lambda item: float(item.get("modified_time") or 0.0), reverse=True)
        else:
            images.sort(key=lambda item: self._natural_key(item.get("filename")))
        return images

    def _build_thumbnail_icon(self, image_path, size=56):
        # Cached so the same icon isn't decoded from disk every time the
        # bulk pairing preview rebuilds (which happens on every keystroke
        # in the prompts box). Without this, 200 images × every textChanged
        # = 200 disk reads + decodes per keystroke.
        if not hasattr(self, "_thumbnail_cache"):
            self._thumbnail_cache = {}
        path_str = str(image_path or "")
        cache_key = (path_str, size)
        cached = self._thumbnail_cache.get(cache_key)
        if cached is not None:
            return cached

        pixmap = QPixmap(path_str)
        if pixmap.isNull():
            result = (QIcon(), QSize(size, size))
        else:
            scaled = pixmap.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            result = (QIcon(scaled), scaled.size())

        # Bound the cache so it can't grow unbounded across long sessions.
        # Most bulk runs use < 1000 images; older entries are evicted FIFO.
        if len(self._thumbnail_cache) > 2000:
            for k in list(self._thumbnail_cache.keys())[:500]:
                self._thumbnail_cache.pop(k, None)
        self._thumbnail_cache[cache_key] = result
        return result

    def _schedule_bulk_pairing_refresh(self, mode_key, delay_ms=300):
        """Coalesce rapid bulk-preview refresh requests into a single
        update after `delay_ms` of quiet. Without this, pasting 400
        prompt lines into the bulk text box fires textChanged 400 times,
        each rebuilding the thumbnail tables — UI hangs for seconds.
        """
        if not hasattr(self, "_bulk_refresh_timers"):
            self._bulk_refresh_timers = {}
        timer = self._bulk_refresh_timers.get(mode_key)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(
                lambda key=mode_key: self._refresh_bulk_pairing_preview(key)
            )
            self._bulk_refresh_timers[mode_key] = timer
        # Re-arming an active timer pushes the firing further out — that's
        # exactly the debounce we want.
        timer.start(delay_ms)

    _NATURAL_SORT_RE = re.compile(r"(\d+)")

    @classmethod
    def _natural_key(cls, value):
        """Sort key that orders '1, 2, 10, 100' instead of '1, 10, 100, 2'.

        Splits the string into runs of digits and non-digits; digit runs are
        compared as ints, the rest case-insensitively. Used for filename
        sort so '10.jpg' comes after '2.jpg', not before it.
        """
        parts = cls._NATURAL_SORT_RE.split(str(value or ""))
        return [int(p) if p.isdigit() else p.lower() for p in parts]

    def _filename_to_prompt(self, filename):
        name_no_ext = Path(str(filename or "")).stem
        return name_no_ext.replace("_", " ").replace("-", " ").strip()

    def _build_bulk_pairs(self, mode_key):
        panel = self._bulk_panel(mode_key)
        if not panel:
            return [], [], []
        images = self._get_sorted_bulk_images(mode_key)
        raw_lines = panel["prompts_input"].toPlainText().splitlines()
        prompts = [line.strip() for line in raw_lines if line.strip()]
        missing_action = str(panel["missing_selector"].currentData() or "filename")

        pairs = []
        for idx, image in enumerate(images):
            if idx < len(prompts):
                prompt = prompts[idx]
                paired = True
            elif missing_action == "filename":
                prompt = self._filename_to_prompt(image.get("filename"))
                paired = False
            else:
                continue

            pairs.append(
                {
                    "index": idx + 1,
                    "image_path": image.get("path"),
                    "filename": image.get("filename"),
                    "prompt": prompt,
                    "paired": paired,
                }
            )
        return images, prompts, pairs

    def _refresh_bulk_pairing_preview(self, mode_key):
        panel = self._bulk_panel(mode_key)
        if not panel:
            return
        images, prompts, pairs = self._build_bulk_pairs(mode_key)

        images_table = panel["images_table"]
        pairs_table = panel["pairs_table"]
        images_table.setRowCount(len(images))
        images_table.setVisible(bool(images))
        for row_idx, image in enumerate(images):
            images_table.setRowHeight(row_idx, 64)

            index_item = QTableWidgetItem(str(row_idx + 1))
            index_item.setForeground(QColor("#E2E8F0"))
            images_table.setItem(row_idx, 0, index_item)

            thumb_item = QTableWidgetItem("")
            icon, thumb_size = self._build_thumbnail_icon(image.get("path"))
            if not icon.isNull():
                thumb_item.setIcon(icon)
                images_table.setIconSize(thumb_size)
            thumb_item.setToolTip(str(image.get("path") or ""))
            images_table.setItem(row_idx, 1, thumb_item)

            file_item = QTableWidgetItem(str(image.get("filename") or ""))
            file_item.setToolTip(str(image.get("path") or ""))
            file_item.setForeground(QColor("#E2E8F0"))
            images_table.setItem(row_idx, 2, file_item)

        pairs_table.setRowCount(len(pairs))
        pairs_table.setVisible(bool(pairs))
        for row_idx, pair in enumerate(pairs):
            pairs_table.setRowHeight(row_idx, 64)

            index_item = QTableWidgetItem(str(pair.get("index") or row_idx + 1))
            index_item.setForeground(QColor("#E2E8F0"))
            pairs_table.setItem(row_idx, 0, index_item)

            image_item = QTableWidgetItem(str(pair.get("filename") or ""))
            icon, thumb_size = self._build_thumbnail_icon(pair.get("image_path"))
            if not icon.isNull():
                image_item.setIcon(icon)
                pairs_table.setIconSize(thumb_size)
            image_item.setToolTip(str(pair.get("image_path") or ""))
            image_item.setForeground(QColor("#E2E8F0"))
            pairs_table.setItem(row_idx, 1, image_item)

            prompt_item = QTableWidgetItem(str(pair.get("prompt") or ""))
            if not pair.get("paired", True):
                prompt_item.setForeground(QColor("#F59E0B"))
                prompt_item.setToolTip("Auto-generated from filename")
            else:
                prompt_item.setForeground(QColor("#E2E8F0"))
            pairs_table.setItem(row_idx, 2, prompt_item)

        panel["lbl_loaded"].setText(f"{len(images)} image(s)")
        missing_count = max(0, len(images) - len(prompts))
        if not images:
            hint = "Drop images or choose a folder, then add prompts to preview pairings."
        elif missing_count > 0:
            action_label = (
                "will use filename prompts"
                if str(panel["missing_selector"].currentData() or "filename") == "filename"
                else "will be skipped"
            )
            hint = f"{len(images)} images, {len(prompts)} prompts — {missing_count} images {action_label}."
        elif len(prompts) > len(images):
            hint = f"{len(prompts) - len(images)} extra prompt(s) have no matching image."
        else:
            hint = f"Ready: {len(pairs)} image/prompt pair(s)."
        panel["hint_label"].setText(hint)
        panel["add_btn"].setEnabled(bool(pairs))

    def add_bulk_i2v_to_queue(self, mode_key):
        images, prompts, pairs = self._build_bulk_pairs(mode_key)
        if not images:
            self._toast_warning("No Images", "Load images first for bulk image-to-video.")
            return
        if not pairs:
            self._toast_warning("No Pairs", "No image/prompt pairs are ready to add to the queue.")
            return

        bulk_sub_mode = str(mode_key or "")
        if bulk_sub_mode not in ("ingredients", "frames_start"):
            QMessageBox.information(
                self,
                "Bulk Mode Unavailable",
                "Bulk image matching is available only for Ingredients and Frames - Start Image modes.",
            )
            return

        current_settings = self._current_generation_settings()

        # Build the existing (image_path, prompt) set from pending/running
        # jobs so we can skip pairs already queued. Without this, clicking
        # "Add to Queue" twice silently double-queues every pair — which
        # manifests as "same image used twice in generation" downstream.
        existing_pairs = set()
        try:
            from src.db.db_manager import get_all_jobs
            for existing in get_all_jobs() or []:
                if existing.get("status") not in ("pending", "running"):
                    continue
                existing_pairs.add(
                    (
                        str(existing.get("ref_path") or existing.get("start_image_path") or "").strip(),
                        str(existing.get("prompt") or "").strip(),
                    )
                )
        except Exception:
            existing_pairs = set()

        auto_generated_count = 0
        skipped_duplicate_count = 0
        job_specs = []
        for pair in pairs:
            if not pair.get("paired", True):
                auto_generated_count += 1
            ref_path = pair["image_path"] if bulk_sub_mode == "ingredients" else None
            start_image_path = pair["image_path"] if bulk_sub_mode == "frames_start" else None
            dedup_key = (
                str(ref_path or start_image_path or "").strip(),
                str(pair["prompt"] or "").strip(),
            )
            if dedup_key in existing_pairs:
                skipped_duplicate_count += 1
                continue
            existing_pairs.add(dedup_key)
            job_specs.append({
                "job_id": str(uuid.uuid4()),
                "prompt": pair["prompt"],
                "model": current_settings["model"],
                "aspect_ratio": current_settings["aspect_ratio"],
                "output_count": current_settings["output_count"],
                "ref_path": ref_path,
                "ref_paths": [ref_path] if ref_path else [],
                "job_type": "video",
                "video_model": current_settings["video_model"],
                "video_sub_mode": current_settings["video_sub_mode"],
                "video_ratio": current_settings.get("video_ratio", current_settings["aspect_ratio"]),
                "video_prompt": current_settings.get("video_prompt", ""),
                "video_upscale": current_settings["video_upscale"],
                "video_length": current_settings.get("video_length", 10),
                "video_output_count": current_settings["video_output_count"],
                "start_image_path": start_image_path,
                "end_image_path": None,
            })

        try:
            _dbg_img = str((pairs[0].get("image_path") if pairs else "")) or "(none)"
            _dbg_ref = str((job_specs[0].get("ref_path") if job_specs else "")) or "(none)"
            self.append_log(
                f"[BULK-DEBUG] sub_mode={bulk_sub_mode}, pairs={len(pairs)}, specs={len(job_specs)}, "
                f"first_image_path={_dbg_img[:70]}, first_spec_ref={_dbg_ref[:70]}"
            )
        except Exception:
            pass

        extra_note = f" ({auto_generated_count} filename prompt(s))" if auto_generated_count else ""
        estimate = self._estimate_video_credits(
            prompt_count=len(pairs),
            output_count=current_settings["video_output_count"],
            upscale=current_settings["video_upscale"],
            video_model=current_settings["video_model"],
        )
        self._start_bulk_queue_add(
            job_specs,
            success_logs=[
                f"[CREDITS] Estimated cost: ~{estimate} credits for {len(pairs)} prompt(s) x {current_settings['video_output_count']} output(s)",
                f"Added {len(pairs)} bulk image-to-video task(s) in {bulk_sub_mode.replace('_', ' ')} mode{extra_note}.",
            ],
            progress_title=f"Adding {len(job_specs)} bulk video job(s)...",
        )

    def _sync_primary_reference_path(self):
        return self.current_ref_paths[0] if getattr(self, "current_ref_paths", None) else None

    @staticmethod
    def _make_ref_thumbnail(path, size=48):
        """Create a QLabel with a thumbnail preview of the image."""
        from PyQt5.QtGui import QPixmap
        thumb_label = QLabel()
        thumb_label.setFixedSize(size, size)
        thumb_label.setStyleSheet(
            f"background: #0F172A; border: 1px solid #334155; border-radius: 4px;"
        )
        thumb_label.setAlignment(Qt.AlignCenter)
        try:
            pixmap = QPixmap(path)
            if not pixmap.isNull():
                scaled = pixmap.scaled(size - 4, size - 4, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                thumb_label.setPixmap(scaled)
        except Exception:
            thumb_label.setText("?")
        thumb_label.setToolTip(path)
        return thumb_label

    def _rebuild_reference_image_rows(self):
        if not hasattr(self, "ref_items_layout"):
            return

        while self.ref_items_layout.count():
            item = self.ref_items_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        for path in list(getattr(self, "current_ref_paths", []) or []):
            row_widget = QWidget()
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 2, 0, 2)
            row_layout.setSpacing(8)

            thumb = self._make_ref_thumbnail(path, size=44)
            row_layout.addWidget(thumb)

            label = QLabel(os.path.basename(path))
            label.setObjectName("refStatusLabel")
            label.setToolTip(path)
            label.setStyleSheet("font-size: 12px; color: #CBD5E1;")

            btn_remove = QPushButton("X")
            btn_remove.setObjectName("refClearButton")
            btn_remove.setProperty("role", "danger")
            btn_remove.setFixedWidth(28)
            btn_remove.setMinimumHeight(28)
            btn_remove.setToolTip(f"Remove {os.path.basename(path)}")
            btn_remove.clicked.connect(lambda _=False, target_path=path: self._remove_reference_image(target_path))

            row_layout.addWidget(label, 1)
            row_layout.addWidget(btn_remove)
            self.ref_items_layout.addWidget(row_widget)

        self.ref_items_container.setVisible(bool(getattr(self, "current_ref_paths", [])))

    def _update_reference_image_ui(self):
        ref_count = len(getattr(self, "current_ref_paths", []) or [])

        if hasattr(self, "lbl_ref_status"):
            if ref_count <= 0:
                self.lbl_ref_status.setText("None")
            elif ref_count == 1:
                self.lbl_ref_status.setText(f"1 image selected: {os.path.basename(self.current_ref_paths[0])}")
            else:
                self.lbl_ref_status.setText(f"{ref_count} images selected")

        self._rebuild_reference_image_rows()
        self.btn_clear_ref.setVisible(ref_count > 0)
        self._on_generation_settings_changed()

    def _add_reference_image(self, path):
        normalized = str(path or "").strip()
        if not normalized:
            return False
        if not os.path.exists(normalized):
            return False
        if normalized in self.current_ref_paths:
            return False
        self.current_ref_paths.append(normalized)
        # Sort A-Z by filename (natural — '10.jpg' after '2.jpg', not before)
        self.current_ref_paths.sort(key=lambda p: self._natural_key(os.path.basename(p)))
        self._update_reference_image_ui()
        return True

    def _remove_reference_image(self, path):
        normalized = str(path or "").strip()
        self.current_ref_paths = [item for item in self.current_ref_paths if str(item or "").strip() != normalized]
        self._update_reference_image_ui()

    def clear_reference_image(self):
        self.current_ref_paths = []
        self._update_reference_image_ui()

    def select_reference_image(self):
        file_paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Select Reference Image(s)",
            "",
            "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif)",
        )
        if file_paths:
            added_any = False
            for file_path in file_paths:
                added_any = self._add_reference_image(file_path) or added_any
            if not added_any:
                self._update_reference_image_ui()
        elif not self.current_ref_paths:
            self._update_reference_image_ui()

    def _rebuild_pipeline_reference_rows(self):
        if not hasattr(self, "pipe_ref_items_layout"):
            return

        while self.pipe_ref_items_layout.count():
            item = self.pipe_ref_items_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        for path in list(getattr(self, "current_pipe_ref_paths", []) or []):
            row_widget = QWidget()
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 2, 0, 2)
            row_layout.setSpacing(8)

            thumb = self._make_ref_thumbnail(path, size=44)
            row_layout.addWidget(thumb)

            label = QLabel(os.path.basename(path))
            label.setObjectName("refStatusLabel")
            label.setToolTip(path)
            label.setStyleSheet("font-size: 12px; color: #CBD5E1;")

            btn_remove = QPushButton("X")
            btn_remove.setObjectName("refClearButton")
            btn_remove.setProperty("role", "danger")
            btn_remove.setFixedWidth(28)
            btn_remove.setMinimumHeight(28)
            btn_remove.setToolTip(f"Remove {os.path.basename(path)}")
            btn_remove.clicked.connect(lambda _=False, target_path=path: self._remove_pipeline_reference_image(target_path))

            row_layout.addWidget(label, 1)
            row_layout.addWidget(btn_remove)
            self.pipe_ref_items_layout.addWidget(row_widget)

        self.pipe_ref_items_container.setVisible(bool(getattr(self, "current_pipe_ref_paths", [])))

    def _update_pipeline_reference_ui(self):
        ref_count = len(getattr(self, "current_pipe_ref_paths", []) or [])

        if hasattr(self, "pipe_lbl_ref_status"):
            if ref_count <= 0:
                self.pipe_lbl_ref_status.setText("None")
            elif ref_count == 1:
                self.pipe_lbl_ref_status.setText(f"1 image selected: {os.path.basename(self.current_pipe_ref_paths[0])}")
            else:
                self.pipe_lbl_ref_status.setText(f"{ref_count} images selected")

        self._rebuild_pipeline_reference_rows()
        if hasattr(self, "pipe_btn_clear_refs"):
            self.pipe_btn_clear_refs.setVisible(ref_count > 0)
        self._on_generation_settings_changed()

    def _add_pipeline_reference_image(self, path):
        normalized = str(path or "").strip()
        if not normalized or not os.path.exists(normalized):
            return False
        if normalized in self.current_pipe_ref_paths:
            return False
        self.current_pipe_ref_paths.append(normalized)
        # Sort A-Z by filename (natural — '10.jpg' after '2.jpg', not before)
        self.current_pipe_ref_paths.sort(key=lambda p: self._natural_key(os.path.basename(p)))
        self._update_pipeline_reference_ui()
        return True

    def _remove_pipeline_reference_image(self, path):
        normalized = str(path or "").strip()
        self.current_pipe_ref_paths = [
            item for item in self.current_pipe_ref_paths if str(item or "").strip() != normalized
        ]
        self._update_pipeline_reference_ui()

    def clear_pipeline_reference_images(self):
        self.current_pipe_ref_paths = []
        self._update_pipeline_reference_ui()

    def select_pipeline_reference_images(self):
        file_paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Select Pipeline Reference Image(s)",
            "",
            "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif)",
        )
        if file_paths:
            added_any = False
            for file_path in file_paths:
                added_any = self._add_pipeline_reference_image(file_path) or added_any
            if not added_any:
                self._update_pipeline_reference_ui()
        elif not self.current_pipe_ref_paths:
            self._update_pipeline_reference_ui()

    def _update_single_reference_image_ui(self):
        has_ref = bool(self.current_ref_path)
        if hasattr(self, "lbl_ref_single"):
            self.lbl_ref_single.setText(os.path.basename(self.current_ref_path) if has_ref else "None")
        if hasattr(self, "btn_ref_single_clear"):
            self.btn_ref_single_clear.setVisible(has_ref)
        # Show/update thumbnail preview
        self._update_path_row_thumbnail(self.ref_single_row, self.current_ref_path if has_ref else None)
        self._on_generation_settings_changed()

    def _update_path_row_thumbnail(self, row_frame, path):
        """Add or update a thumbnail in a path row frame."""
        # Remove old thumbnail if exists
        old_thumb = row_frame.findChild(QLabel, "pathRowThumb")
        if old_thumb:
            old_thumb.deleteLater()
        if path and os.path.exists(path):
            from PyQt5.QtGui import QPixmap
            thumb = QLabel()
            thumb.setObjectName("pathRowThumb")
            thumb.setFixedSize(40, 40)
            thumb.setStyleSheet("background: #0F172A; border: 1px solid #334155; border-radius: 4px;")
            thumb.setAlignment(Qt.AlignCenter)
            try:
                pixmap = QPixmap(path)
                if not pixmap.isNull():
                    scaled = pixmap.scaled(36, 36, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    thumb.setPixmap(scaled)
            except Exception:
                pass
            thumb.setToolTip(path)
            row_frame.layout().insertWidget(0, thumb)

    def clear_single_reference_image(self):
        self.current_ref_path = None
        self._update_single_reference_image_ui()

    def select_single_reference_image(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Reference Image",
            "",
            "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif)",
        )
        if file_path:
            self.current_ref_path = file_path
        self._update_single_reference_image_ui()

    def clear_start_image(self):
        self.current_start_image_path = None
        if hasattr(self, "lbl_start_image"):
            self.lbl_start_image.setText("None")
        if hasattr(self, "btn_clear_start_image"):
            self.btn_clear_start_image.setVisible(False)
        self._update_path_row_thumbnail(self.start_row, None)
        self._on_generation_settings_changed()

    def select_start_image(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Start Image",
            "",
            "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif)",
        )
        if file_path:
            self.current_start_image_path = file_path
            self.lbl_start_image.setText(os.path.basename(file_path))
            self.btn_clear_start_image.setVisible(True)
            self._update_path_row_thumbnail(self.start_row, file_path)
            self._on_generation_settings_changed()
        elif not self.current_start_image_path and hasattr(self, "lbl_start_image"):
            self.lbl_start_image.setText("None")

    def clear_end_image(self):
        self.current_end_image_path = None
        if hasattr(self, "lbl_end_image"):
            self.lbl_end_image.setText("None")
        if hasattr(self, "btn_clear_end_image"):
            self.btn_clear_end_image.setVisible(False)
        self._update_path_row_thumbnail(self.end_row, None)
        self._on_generation_settings_changed()

    def select_end_image(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select End Image",
            "",
            "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif)",
        )
        if file_path:
            self.current_end_image_path = file_path
            self.lbl_end_image.setText(os.path.basename(file_path))
            self.btn_clear_end_image.setVisible(True)
            self._update_path_row_thumbnail(self.end_row, file_path)
            self._on_generation_settings_changed()
        elif not self.current_end_image_path and hasattr(self, "lbl_end_image"):
            self.lbl_end_image.setText("None")

    def _add_pipeline_to_queue(self):
        current_settings = self._current_generation_settings()
        if current_settings.get("job_type") != "pipeline":
            return

        text = self.pipe_txt_img_prompts.toPlainText().strip() if hasattr(self, "pipe_txt_img_prompts") else ""
        if not text:
            self._toast_warning("No Prompts", "Enter at least one image prompt.")
            return

        image_prompts = [line.strip() for line in text.splitlines() if line.strip()]
        raw_video_lines = []
        if hasattr(self, "pipe_txt_vid_prompts"):
            raw_video_lines = [line.strip() for line in self.pipe_txt_vid_prompts.toPlainText().splitlines()]

        custom_video_prompt_count = 0
        job_specs = []
        for idx, image_prompt in enumerate(image_prompts):
            target_video_prompt = (
                raw_video_lines[idx]
                if idx < len(raw_video_lines) and raw_video_lines[idx]
                else current_settings.get("video_prompt", "animate")
            )
            if idx < len(raw_video_lines) and raw_video_lines[idx]:
                custom_video_prompt_count += 1
            job_specs.append({
                "job_id": str(uuid.uuid4()),
                "prompt": image_prompt,
                "model": current_settings["model"],
                "aspect_ratio": current_settings["aspect_ratio"],
                "output_count": current_settings["output_count"],
                "ref_path": current_settings["ref_path"],
                "ref_paths": current_settings.get("ref_paths"),
                "job_type": "pipeline",
                "video_model": current_settings["video_model"],
                "video_sub_mode": current_settings["video_sub_mode"],
                "video_ratio": current_settings.get("video_ratio", current_settings["aspect_ratio"]),
                "video_prompt": target_video_prompt,
                "video_upscale": current_settings["video_upscale"],
                "video_length": current_settings.get("video_length", 10),
                "video_output_count": current_settings["video_output_count"],
                "start_image_path": None,
                "end_image_path": None,
            })

        animate_count = max(0, len(image_prompts) - custom_video_prompt_count)
        estimate = self._estimate_video_credits(
            prompt_count=len(image_prompts),
            output_count=1,
            upscale=current_settings["video_upscale"],
            video_model=current_settings["video_model"],
        )
        msg = f"[PIPELINE] Added {len(image_prompts)} pipeline jobs. {custom_video_prompt_count} with custom video prompts"
        if animate_count > 0:
            msg += f", {animate_count} with 'animate' default"
        self._start_bulk_queue_add(
            job_specs,
            success_logs=[
                f"[CREDITS] Estimated cost: ~{estimate} credits for {len(image_prompts)} pipeline prompt(s)",
                msg,
            ],
            after_success=lambda: self.pipe_txt_img_prompts.clear() if hasattr(self, "pipe_txt_img_prompts") else None,
            progress_title=f"Adding {len(job_specs)} pipeline job(s)...",
        )

    def _current_video_sub_mode(self):
        if not hasattr(self, "mode_tabs"):
            return "text_to_video"
        idx = self.mode_tabs.currentIndex()
        if idx == 1:
            return "text_to_video"
        if idx == 2:
            return "ingredients"
        if idx == 3 and hasattr(self, "frm_cmb_mode"):
            return str(self.frm_cmb_mode.currentData() or "frames_start")
        if idx == 4 and hasattr(self, "pipe_cmb_vid_mode"):
            return str(self.pipe_cmb_vid_mode.currentData() or "ingredients")
        return "text_to_video"

    def _validate_video_job_inputs(self, settings, *, bulk_mode_override=None):
        sub_mode = str(bulk_mode_override or settings.get("video_sub_mode") or "text_to_video")
        ref_path = str(settings.get("ref_path") or "").strip()
        start_image_path = str(settings.get("start_image_path") or "").strip()
        end_image_path = str(settings.get("end_image_path") or "").strip()

        if sub_mode == "ingredients" and not ref_path:
            self._toast_warning("Missing Reference Image", "Ingredients mode needs a reference image.")
            return False
        if sub_mode == "frames_start" and not start_image_path:
            self._toast_warning("Missing Start Image", "Frames - Start Image mode needs a start image.")
            return False
        if sub_mode == "frames_start_end":
            if not start_image_path:
                self._toast_warning("Missing Start Image", "Frames - Start + End mode needs a start image.")
                return False
            if not end_image_path:
                self._toast_warning("Missing End Image", "Frames - Start + End mode needs an end image.")
                return False
        return True

    def _current_job_type(self):
        if hasattr(self, "mode_tabs") and self.mode_tabs.currentIndex() == 4:
            return "pipeline"
        if hasattr(self, "mode_tabs") and self.mode_tabs.currentIndex() > 0:
            return "video"
        return "image"

    def _current_parallel_slots(self):
        if not hasattr(self, "mode_tabs"):
            return 1
        idx = self.mode_tabs.currentIndex()
        selector = {
            0: getattr(self, "img_cmb_parallel", None),
            1: getattr(self, "t2v_cmb_parallel", None),
            2: getattr(self, "ref_cmb_parallel", None),
            3: getattr(self, "frm_cmb_parallel", None),
            4: getattr(self, "pipe_cmb_parallel", None),
        }.get(idx)
        if selector is None:
            return 1
        return int(selector.currentData() or 1)

    def _current_generation_settings(self):
        required_controls = (
            "img_cmb_outputs",
            "t2v_cmb_outputs",
            "ref_cmb_outputs",
            "frm_cmb_outputs",
            "pipe_cmb_img_outputs",
            "pipe_cmb_vid_outputs",
            "pipe_cmb_vid_ratio",
        )
        if not hasattr(self, "mode_tabs") or not all(hasattr(self, name) for name in required_controls):
            return {
                "job_type": "image",
                "model": "Imagen 4",
                "aspect_ratio": "Landscape (16:9)",
                "output_count": 1,
                "ref_path": None,
                "ref_paths": [],
                "video_model": "",
                "video_sub_mode": "",
                "video_ratio": "",
                "video_prompt": "",
                "video_upscale": "none",
                "video_output_count": 1,
                "start_image_path": None,
                "end_image_path": None,
            }

        idx = self.mode_tabs.currentIndex()
        if idx == 0:
            output_count = int(self.img_cmb_outputs.currentData() or 1)
            ref_paths = list(self.current_ref_paths)
            model_text = str(self.img_cmb_model.currentText() or "Imagen 4")
            # Encode Genspark-only quality (if non-auto) into the model string
            # as "Model Name:<size>". Flow's model resolver ignores the suffix
            # and matches on the prefix, so this is a no-op for Flow jobs but
            # carries per-job resolution for Genspark.
            quality = "auto"
            try:
                quality = str(self.img_cmb_quality.currentData() or "auto").strip().lower()
            except Exception:
                pass
            encoded_model = f"{model_text}:{quality}" if quality and quality != "auto" else model_text
            return {
                "job_type": "image",
                "model": encoded_model,
                "aspect_ratio": str(self.img_cmb_ratio.currentData() or self.img_cmb_ratio.currentText() or "Landscape (16:9)"),
                "output_count": output_count,
                "ref_path": ref_paths[0] if ref_paths else None,
                "ref_paths": ref_paths,
                "video_model": "",
                "video_sub_mode": "",
                "video_ratio": "",
                "video_prompt": "",
                "video_upscale": "none",
                "video_output_count": 1,
                "start_image_path": None,
                "end_image_path": None,
            }

        if idx == 1:
            output_count = int(self.t2v_cmb_outputs.currentData() or 1)
            model = str(self.t2v_cmb_quality.currentText() or "Veo 3.1 - Fast")
            return {
                "job_type": "video",
                "model": model,
                "aspect_ratio": str(self.t2v_cmb_ratio.currentData() or self.t2v_cmb_ratio.currentText() or "Landscape (16:9)"),
                "output_count": output_count,
                "ref_path": None,
                "ref_paths": [],
                "video_model": model,
                "video_sub_mode": "text_to_video",
                "video_ratio": str(self.t2v_cmb_ratio.currentData() or self.t2v_cmb_ratio.currentText() or "Landscape (16:9)"),
                "video_prompt": "",
                "video_upscale": self._video_upscale_for_dispatch(
                    self.t2v_cmb_upscale, getattr(self, "t2v_cmb_grok_res", None),
                ),
                "video_length": self._video_length_for_dispatch(
                    getattr(self, "t2v_cmb_grok_dur", None),
                ),
                "video_output_count": output_count,
                "start_image_path": None,
                "end_image_path": None,
            }

        if idx == 2:
            output_count = int(self.ref_cmb_outputs.currentData() or 1)
            model = str(self.ref_cmb_quality.currentText() or "Veo 3.1 - Fast")
            ref_path = str(self.current_ref_path or "").strip() or None
            return {
                "job_type": "video",
                "model": model,
                "aspect_ratio": str(self.ref_cmb_ratio.currentData() or self.ref_cmb_ratio.currentText() or "Landscape (16:9)"),
                "output_count": output_count,
                "ref_path": ref_path,
                "ref_paths": [ref_path] if ref_path else [],
                "video_model": model,
                "video_sub_mode": "ingredients",
                "video_ratio": str(self.ref_cmb_ratio.currentData() or self.ref_cmb_ratio.currentText() or "Landscape (16:9)"),
                "video_prompt": "",
                "video_upscale": self._video_upscale_for_dispatch(
                    self.ref_cmb_upscale, getattr(self, "ref_cmb_grok_res", None),
                ),
                "video_length": self._video_length_for_dispatch(
                    getattr(self, "ref_cmb_grok_dur", None),
                ),
                "video_output_count": output_count,
                "start_image_path": None,
                "end_image_path": None,
            }

        if idx == 3:
            frame_mode = self._current_video_sub_mode()
            output_count = int(self.frm_cmb_outputs.currentData() or 1)
            model = str(self.frm_cmb_quality.currentText() or "Veo 3.1 - Fast")
            return {
                "job_type": "video",
                "model": model,
                "aspect_ratio": str(self.frm_cmb_ratio.currentData() or self.frm_cmb_ratio.currentText() or "Landscape (16:9)"),
                "output_count": output_count,
                "ref_path": None,
                "ref_paths": [],
                "video_model": model,
                "video_sub_mode": frame_mode,
                "video_ratio": str(self.frm_cmb_ratio.currentData() or self.frm_cmb_ratio.currentText() or "Landscape (16:9)"),
                "video_prompt": "",
                "video_upscale": self._video_upscale_for_dispatch(
                    self.frm_cmb_upscale, getattr(self, "frm_cmb_grok_res", None),
                ),
                "video_length": self._video_length_for_dispatch(
                    getattr(self, "frm_cmb_grok_dur", None),
                ),
                "video_output_count": output_count,
                "start_image_path": self.current_start_image_path,
                "end_image_path": self.current_end_image_path if frame_mode == "frames_start_end" else None,
            }

        ref_paths = list(getattr(self, "current_pipe_ref_paths", []) or [])
        image_model = str(self.pipe_cmb_img_model.currentText() or "Imagen 4")
        video_model = str(self.pipe_cmb_vid_quality.currentText() or "Veo 3.1 - Fast")
        # Per-prompt output counts — mirror the other tabs' dropdowns.
        # Fallback to 1 if the combo hasn't been created yet (early
        # init paths can hit this code before the widget exists).
        img_outputs_combo = getattr(self, "pipe_cmb_img_outputs", None)
        vid_outputs_combo = getattr(self, "pipe_cmb_vid_outputs", None)
        img_output_count = int(img_outputs_combo.currentData() or 1) if img_outputs_combo is not None else 1
        vid_output_count = int(vid_outputs_combo.currentData() or 1) if vid_outputs_combo is not None else 1
        return {
            "job_type": "pipeline",
            "model": image_model,
            "aspect_ratio": str(self.pipe_cmb_img_ratio.currentData() or self.pipe_cmb_img_ratio.currentText() or "Landscape (16:9)"),
            "output_count": img_output_count,
            "ref_path": ref_paths[0] if ref_paths else None,
            "ref_paths": ref_paths,
            "video_model": video_model,
            "video_sub_mode": str(self.pipe_cmb_vid_mode.currentData() or "ingredients"),
            "video_ratio": str(self.pipe_cmb_vid_ratio.currentData() or self.pipe_cmb_vid_ratio.currentText() or "Landscape (16:9)"),
            "video_prompt": str(self.pipe_txt_vid_prompt.text() or "").strip() or "animate",
            "video_upscale": self._video_upscale_for_dispatch(
                self.pipe_cmb_upscale, getattr(self, "pipe_cmb_grok_res", None),
            ),
            "video_length": self._video_length_for_dispatch(
                getattr(self, "pipe_cmb_grok_dur", None),
            ),
            "video_output_count": vid_output_count,
            "start_image_path": None,
            "end_image_path": None,
        }

    def _video_upscale_for_dispatch(self, upscale_combo, grok_res_combo):
        """Resolve the `video_upscale` job-dict field. In Grok mode this
        reads the 480p/720p grok-res combo; in every other mode it
        reads the Veo-style upscale combo (720p/1080p/4K). Keeps job
        dispatch agnostic of which backend will actually consume it —
        grok_mode maps 480p/720p literally, extension_mode ignores
        unknown values and falls back to "none"."""
        if self._is_grok_generation_mode() and grok_res_combo is not None:
            return str(grok_res_combo.currentData() or "720p")
        return str(upscale_combo.currentData() or "none") if upscale_combo is not None else "none"

    def _video_length_for_dispatch(self, grok_dur_combo):
        """Resolve the `video_length` job-dict field. Grok is the only
        backend that actually uses this (6s / 10s). Other modes ignore
        it — still safe to pass through."""
        if self._is_grok_generation_mode() and grok_dur_combo is not None:
            try:
                return int(grok_dur_combo.currentData() or 10)
            except (TypeError, ValueError):
                pass
        return 10

    def _is_grok_generation_mode(self) -> bool:
        """True when the app settings have Grok Imagine selected as the
        active generation mode. Used by the video tabs to swap their
        Upscale combo for a Resolution (480p/720p) + Duration (6s/10s)
        pair, and by the job-dict builders to feed those values as
        video_upscale / video_length on dispatched jobs."""
        cmb = getattr(self, "cmb_generation_mode", None)
        if cmb is None:
            return False
        try:
            return str(cmb.currentData() or "").lower() == "chrome_extension_grok"
        except Exception:
            return False

    def _is_dola_generation_mode(self) -> bool:
        """True when Dola (dola.com Seedance) is the selected Generation Mode."""
        cmb = getattr(self, "cmb_generation_mode", None)
        if cmb is None:
            return False
        try:
            return str(cmb.currentData() or "").lower() in ("chrome_extension_dola", "playwright_dola")
        except Exception:
            return False

    def _is_genspark_generation_mode(self) -> bool:
        """True when Genspark is the selected Generation Mode."""
        cmb = getattr(self, "cmb_generation_mode", None)
        if cmb is None:
            return False
        try:
            return str(cmb.currentData() or "").lower() == "chrome_extension_genspark"
        except Exception:
            return False

    def _snapshot_pre_genspark_settings(self):
        """Save current slots/stagger/speed values to persistent storage
        as a 'pre-Genspark' backup. Called right before applying the
        Genspark defaults so the user's previous Flow/Grok/Browser pacing
        can be restored when they switch away from Genspark."""
        try:
            if hasattr(self, "spin_slots_per_account"):
                set_setting("pre_genspark_slots_per_account",
                            str(int(self.spin_slots_per_account.value())))
            if hasattr(self, "spin_same_account_stagger"):
                set_setting("pre_genspark_same_stagger",
                            str(float(self.spin_same_account_stagger.value())))
            if hasattr(self, "spin_global_stagger_min"):
                set_setting("pre_genspark_global_min",
                            str(float(self.spin_global_stagger_min.value())))
            if hasattr(self, "spin_global_stagger_max"):
                set_setting("pre_genspark_global_max",
                            str(float(self.spin_global_stagger_max.value())))
            if hasattr(self, "cmb_speed_profile"):
                set_setting("pre_genspark_speed_profile",
                            str(self.cmb_speed_profile.currentData() or "fast"))
        except Exception:
            pass

    def _restore_pre_genspark_settings(self):
        """Restore the pacing settings that were active before Genspark
        defaults overwrote them. Called when the user switches away from
        Genspark Generation Mode."""
        applied = []
        try:
            saved_slots_str = str(get_setting("pre_genspark_slots_per_account", "") or "").strip()
            if saved_slots_str and hasattr(self, "spin_slots_per_account"):
                slots = max(1, min(40, int(float(saved_slots_str))))
                self.spin_slots_per_account.setValue(slots)
                set_setting("slots_per_account", str(slots))
                applied.append(f"parallel={slots}")

            saved_same_str = str(get_setting("pre_genspark_same_stagger", "") or "").strip()
            if saved_same_str and hasattr(self, "spin_same_account_stagger"):
                v = max(0.0, min(60.0, float(saved_same_str)))
                self.spin_same_account_stagger.setValue(v)
                set_setting("same_account_stagger_seconds", str(v))
                applied.append(f"per-account stagger={v}s")

            saved_gmin = str(get_setting("pre_genspark_global_min", "") or "").strip()
            saved_gmax = str(get_setting("pre_genspark_global_max", "") or "").strip()
            if saved_gmin and hasattr(self, "spin_global_stagger_min"):
                v = max(0.0, min(60.0, float(saved_gmin)))
                self.spin_global_stagger_min.setValue(v)
                set_setting("global_stagger_min_seconds", str(v))
            if saved_gmax and hasattr(self, "spin_global_stagger_max"):
                v = max(0.0, min(120.0, float(saved_gmax)))
                self.spin_global_stagger_max.setValue(v)
                set_setting("global_stagger_max_seconds", str(v))
            if saved_gmin and saved_gmax:
                applied.append(f"global stagger={saved_gmin}-{saved_gmax}s")

            saved_speed = str(get_setting("pre_genspark_speed_profile", "") or "").strip().lower()
            if saved_speed and hasattr(self, "cmb_speed_profile"):
                idx = self.cmb_speed_profile.findData(saved_speed)
                if idx >= 0:
                    self.cmb_speed_profile.setCurrentIndex(idx)
                    set_setting("speed_profile", saved_speed)
                    applied.append(f"speed={saved_speed}")

            if applied:
                self.append_log(
                    "[SETTINGS] Restored pre-Genspark pacing settings: "
                    + ", ".join(applied) + "."
                )
        except Exception as e:
            try:
                self.append_log(f"[SETTINGS] Could not restore pre-Genspark settings: {e}")
            except Exception:
                pass

    def _apply_genspark_recommended_settings(self):
        """Apply Genspark-optimized pacing defaults to prevent the 5-hour
        session limit from being hit too quickly. Triggered when the user
        switches Generation Mode TO Genspark — overwrites slots, stagger,
        and speed-profile settings in both the UI and persistent storage.

        Why these values: empirically tuned. With parallel=4 + fast stagger,
        Genspark Plus throttles around ~70 images. With these values, runs
        sustain 3-4 hours without throttle stalls.
        """
        SLOTS = 2
        SAME_ACC_STAGGER = 3.0
        GLOBAL_MIN = 2.5
        GLOBAL_MAX = 4.0
        SPEED_PROFILE = "stable"  # "Slow Stable" — closest to balanced
        applied = []
        try:
            if hasattr(self, "spin_slots_per_account"):
                self.spin_slots_per_account.setValue(SLOTS)
                set_setting("slots_per_account", str(SLOTS))
                applied.append(f"parallel={SLOTS}")
            if hasattr(self, "spin_same_account_stagger"):
                self.spin_same_account_stagger.setValue(SAME_ACC_STAGGER)
                set_setting("same_account_stagger_seconds", str(SAME_ACC_STAGGER))
                applied.append(f"per-account stagger={SAME_ACC_STAGGER}s")
            if hasattr(self, "spin_global_stagger_min"):
                self.spin_global_stagger_min.setValue(GLOBAL_MIN)
                set_setting("global_stagger_min_seconds", str(GLOBAL_MIN))
            if hasattr(self, "spin_global_stagger_max"):
                self.spin_global_stagger_max.setValue(GLOBAL_MAX)
                set_setting("global_stagger_max_seconds", str(GLOBAL_MAX))
                applied.append(f"global stagger={GLOBAL_MIN}-{GLOBAL_MAX}s")
            if hasattr(self, "cmb_speed_profile"):
                idx = self.cmb_speed_profile.findData(SPEED_PROFILE)
                if idx >= 0:
                    self.cmb_speed_profile.setCurrentIndex(idx)
                    set_setting("speed_profile", SPEED_PROFILE)
                    applied.append("speed=Slow Stable")
            if applied:
                self.append_log(
                    "[SETTINGS] Genspark recommended defaults applied: "
                    + ", ".join(applied) + ". "
                    "These pacing values prevent the 5-hour session-limit "
                    "from triggering on Plus plan. Switch back to another "
                    "mode to auto-restore your previous settings."
                )
        except Exception as e:
            try:
                self.append_log(f"[SETTINGS] Could not apply Genspark defaults: {e}")
            except Exception:
                pass

    def _on_generation_mode_changed(self):
        """Handle Generation Mode dropdown change. Runs UI sync and:
          - Entering Genspark from another mode → snapshot current pacing
            + apply Genspark defaults.
          - Leaving Genspark to another mode → restore pacing snapshot.
        """
        self._sync_generation_mode_ui()
        is_now_genspark = self._is_genspark_generation_mode()
        was_genspark = (
            str(getattr(self, "_last_generation_mode", "") or "").lower()
            == "chrome_extension_genspark"
        )
        current_mode = ""
        cmb = getattr(self, "cmb_generation_mode", None)
        if cmb is not None:
            try:
                current_mode = str(cmb.currentData() or "").lower()
            except Exception:
                current_mode = ""

        if is_now_genspark and not was_genspark:
            # Entering Genspark — snapshot then apply defaults
            self._snapshot_pre_genspark_settings()
            self._apply_genspark_recommended_settings()
        elif was_genspark and not is_now_genspark:
            # Leaving Genspark — restore previous pacing
            self._restore_pre_genspark_settings()

        self._last_generation_mode = current_mode

    def _sync_generation_mode_ui(self):
        frame_mode = self._current_video_sub_mode()
        pipeline_active = hasattr(self, "mode_tabs") and self.mode_tabs.currentIndex() == 4
        # Toggle each video sub-tab's Upscale row between Veo layout
        # (upscale combo) and Grok layout (resolution + duration).
        is_grok = self._is_grok_generation_mode()
        for prefix in ("t2v", "ref", "frm", "pipe"):
            upscale_cmb = getattr(self, f"{prefix}_cmb_upscale", None)
            res_cmb = getattr(self, f"{prefix}_cmb_grok_res", None)
            dur_cmb = getattr(self, f"{prefix}_cmb_grok_dur", None)
            res_lbl = getattr(self, f"{prefix}_lbl_grok_res", None)
            dur_lbl = getattr(self, f"{prefix}_lbl_grok_dur", None)
            up_lbl = getattr(self, f"{prefix}_lbl_upscale", None)
            if upscale_cmb is None or res_cmb is None or dur_cmb is None:
                continue  # this tab hasn't been built yet
            self._set_grok_row_visible(
                upscale_cmb,
                (res_lbl, res_cmb, dur_lbl, dur_cmb),
                up_lbl,
                is_grok,
            )
        # Dola settings rows (one per video sub-tab) — visible only in dola mode.
        is_dola = self._is_dola_generation_mode()
        for _lbl, _field in getattr(self, "_dola_setting_rows", []):
            _lbl.setVisible(is_dola)
            _field.setVisible(is_dola)

        if hasattr(self, "end_row"):
            self.end_row.setVisible(frame_mode == "frames_start_end")
        if hasattr(self, "frm_bulk_group"):
            self.frm_bulk_group.setVisible(frame_mode == "frames_start")
        if hasattr(self, "frm_bulk_separator"):
            self.frm_bulk_separator.setVisible(frame_mode == "frames_start")
        if hasattr(self, "btn_ref_single_clear"):
            self.btn_ref_single_clear.setVisible(bool(self.current_ref_path))
        if hasattr(self, "btn_clear_start_image"):
            self.btn_clear_start_image.setVisible(bool(self.current_start_image_path))
        if hasattr(self, "btn_clear_end_image"):
            self.btn_clear_end_image.setVisible(frame_mode == "frames_start_end" and bool(self.current_end_image_path))
        if hasattr(self, "prompts_group"):
            self.prompts_group.setEnabled(not pipeline_active)
        if hasattr(self, "lbl_prompts_title"):
            self.lbl_prompts_title.setText(
                "PROMPTS" if not pipeline_active else "PROMPTS (use Pipeline tab)"
            )
        if hasattr(self, "prompts_input"):
            self.prompts_input.setPlaceholderText(
                "Paste your prompts here, one per line..."
                if not pipeline_active
                else "Pipeline tab uses its own Image Prompts + Video Prompts editors."
            )
        if hasattr(self, "btn_add_to_queue"):
            self.btn_add_to_queue.setEnabled(not pipeline_active)
        self._update_runtime_badges()
        self._adjust_mode_tabs_height()

    def _import_prompts_txt(self):
        if not hasattr(self, "prompts_input"):
            return
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Import Prompts TXT",
            "",
            "Text Files (*.txt);;All Files (*.*)",
        )
        if not file_path:
            return
        try:
            with open(file_path, "r", encoding="utf-8") as handle:
                content = handle.read()
        except UnicodeDecodeError:
            with open(file_path, "r", encoding="latin-1") as handle:
                content = handle.read()
        except Exception as exc:
            self._toast_error("Import Failed", f"Unable to read file: {exc}")
            return

        self.prompts_input.setPlainText(content)

    def add_prompts_to_queue(self):
        if hasattr(self, "mode_tabs") and self.mode_tabs.currentIndex() == 4:
            self._toast_info("Use Pipeline Add Button", "Use the Pipeline tab's 'Add All to Queue' button.")
            return
        # On Video+Ref (tab 2) / Frames (tab 3): if bulk images are loaded, the user
        # wants image↔prompt pairs. Redirect to the bulk add so the reference image
        # actually attaches — otherwise this bottom button would create ref-less jobs
        # (a very common confusion vs the Bulk panel's own "Add All to Queue").
        if hasattr(self, "mode_tabs"):
            _bulk_key = {2: "ingredients", 3: "frames_start"}.get(self.mode_tabs.currentIndex())
            if _bulk_key:
                _panel = self._bulk_panel(_bulk_key)
                if _panel and (_panel.get("entries") or []):
                    self.append_log("[BULK] Bulk images loaded → adding image↔prompt pairs (reference attached).")
                    self.add_bulk_i2v_to_queue(_bulk_key)
                    return
        text = self.prompts_input.toPlainText().strip()
        if not text:
            self._toast_warning("No Prompts", "Please enter at least one prompt.")
            return

        current_settings = self._current_generation_settings()
        if current_settings["job_type"] == "video" and not self._validate_video_job_inputs(current_settings):
            return
        
        prompts = [p.strip() for p in text.split('\n') if p.strip()]
        job_specs = []
        for prompt_text in prompts:
            job_specs.append({
                "job_id": str(uuid.uuid4()),
                "prompt": prompt_text,
                "model": current_settings["model"],
                "aspect_ratio": current_settings["aspect_ratio"],
                "output_count": current_settings["output_count"],
                "ref_path": current_settings["ref_path"],
                "ref_paths": current_settings.get("ref_paths"),
                "job_type": current_settings["job_type"],
                "video_model": current_settings["video_model"],
                "video_sub_mode": current_settings["video_sub_mode"],
                "video_ratio": current_settings.get("video_ratio", current_settings["aspect_ratio"]),
                "video_prompt": current_settings.get("video_prompt", ""),
                "video_upscale": current_settings["video_upscale"],
                "video_length": current_settings.get("video_length", 10),
                "video_output_count": current_settings["video_output_count"],
                "start_image_path": current_settings["start_image_path"],
                "end_image_path": current_settings["end_image_path"],
            })

        success_logs = []
        if current_settings["job_type"] in ("video", "pipeline"):
            estimate = self._estimate_video_credits(
                prompt_count=len(prompts),
                output_count=current_settings["video_output_count"],
                upscale=current_settings["video_upscale"],
                video_model=current_settings["video_model"],
            )
            success_logs.append(
                f"[CREDITS] Estimated cost: ~{estimate} credits for {len(prompts)} prompt(s) x {current_settings['video_output_count']} output(s)"
            )
        success_logs.append(f"Added {len(prompts)} prompts to queue.")
        self._start_bulk_queue_add(
            job_specs,
            success_logs=success_logs,
            after_success=self.prompts_input.clear,
            progress_title=f"Adding {len(job_specs)} prompt(s) to queue...",
        )

    def clear_queue(self):
        if self._fluent_confirm(
            "Confirm Clear",
            "Clear all task queue history (pending, running, completed, failed) in one click?",
            confirm_label="Clear All",
            cancel_label="Cancel",
        ):
            if self.queue_manager and self.queue_manager.isRunning():
                self.pending_clear_all = True
                self.queue_manager.stop()
                self._set_queue_controls_state("stopping")
                self.append_log("Stop requested. Running jobs are being cancelled and the queue will auto-clear.")
                return

            from src.db.db_manager import clear_all_jobs
            self._start_background_task(
                clear_all_jobs,
                on_finished=self._on_clear_queue_finished,
            )

    def clear_completed_jobs_from_queue(self):
        self._start_background_task(
            clear_completed_jobs,
            on_finished=self._on_clear_completed_finished,
        )

    def load_queue_table(self, jobs=None):
        if jobs is None:
            self._request_queue_snapshot()
            return

        self._apply_queue_snapshot(self._slim_jobs_for_ui(jobs))

    def show_queue_context_menu(self, position):
        from PySide6.QtWidgets import QMenu
        menu = QMenu()
        remove_action = menu.addAction("Remove Selected Task")
        action = menu.exec(self.queue_table.viewport().mapToGlobal(position))
        
        if action == remove_action:
            index = self.queue_table.indexAt(position)
            if not index.isValid():
                selected_rows = self.queue_table.selectionModel().selectedRows()
                index = selected_rows[0] if selected_rows else None
            if index is not None and index.isValid():
                row = index.row()
                job_id = self.queue_model.job_id_at(row)
                from src.db.db_manager import delete_job
                self._start_background_task(
                    delete_job,
                    job_id,
                    on_finished=lambda _result, job_id=job_id: self._on_manual_job_removed(job_id),
                )

    def _on_manual_job_removed(self, job_id):
        self.append_log("Removed job manually.")
        self.load_queue_table()

    def _on_clear_queue_finished(self, deleted_count):
        self.append_log(f"Queue cleared manually. Removed {int(deleted_count or 0)} task(s).")
        self.load_queue_table()
        self.load_failed_jobs()

    def _on_clear_completed_finished(self, deleted_count):
        self.append_log(f"Cleared {int(deleted_count or 0)} completed job(s) from queue history.")
        self.load_queue_table()
        self.load_failed_jobs()

    def _on_force_clear_finished(self, deleted_count):
        self.append_log(f"[SYSTEM] Instant queue clear complete. Removed {int(deleted_count or 0)} task(s).")
        self.load_queue_table()
        self.load_failed_jobs()

    def _on_auto_clear_after_stop_finished(self, deleted_count):
        self.append_log(f"Queue auto-cleared after stop. Removed {int(deleted_count or 0)} task(s).")
        self.load_queue_table()
        self.load_failed_jobs()

    def start_queue_manager(self):
        if self.queue_manager and self.queue_manager.isRunning():
            return

        if self.queue_manager and not self.queue_manager.isRunning():
            try:
                self.queue_manager.deleteLater()
            except Exception:
                pass
            self.queue_manager = None

        self.pending_clear_all = False
        self.queue_paused = False
        self.queue_stopping = False

        self.account_runtime_state = {}
        self._refresh_account_runtime_cells()

        current_settings = self._current_generation_settings()
        pending_count = self._cached_pending_count()

        # On (re)start of the queue, propagate the CURRENT UI settings —
        # especially model + aspect ratio — to any pending jobs whose fields
        # were saved with older values. Previously the queue would honor
        # each job's saved model even if the user had since changed the
        # dropdown, so a stop → change model → start cycle appeared to do
        # nothing. This fires the same background sync that runs on live
        # settings changes so no per-job override survives a restart.
        if pending_count > 0:
            try:
                self._sync_pending_queue_jobs_to_current_settings()
                self.append_log(
                    f"[SETTINGS] Syncing {pending_count} pending job(s) to "
                    f"current UI: model={current_settings['model']}, "
                    f"ratio={current_settings['aspect_ratio']}."
                )
            except Exception as exc:
                self.append_log(f"[SETTINGS] Pending sync skipped: {exc}")

        self.append_log(
            f"[SETTINGS] Current panel default: {current_settings['job_type']}, "
            f"{current_settings['model']}, {current_settings['aspect_ratio']}, "
            f"x{current_settings['output_count']}, "
            f"references={len(current_settings.get('ref_paths') or [])}, "
            f"upscale={current_settings['video_upscale']}."
        )

        selected_slots = max(1, min(40, self._current_parallel_slots()))
        set_setting("slots_per_account", str(selected_slots))
        self.spin_slots_per_account.setValue(selected_slots)

        if selected_slots > 1:
            current_stagger = max(0.0, min(60.0, float(self.spin_same_account_stagger.value())))
            minimum_stagger = 1.0
            if current_stagger < minimum_stagger:
                current_stagger = minimum_stagger
                set_setting("same_account_stagger_seconds", str(current_stagger))
                self.spin_same_account_stagger.setValue(current_stagger)
                self.append_log(
                    f"[SETTINGS] Auto-applied minimum {minimum_stagger:.1f}s same-account stagger for parallel slots."
                )

        if selected_slots > 1 and not self.chk_profile_clone.isChecked():
            set_setting("enable_profile_clones", "1")
            self.chk_profile_clone.setChecked(True)
            self.append_log("[SETTINGS] Auto-enabled profile cloning for parallel slots.")

        self.append_log(f"[SETTINGS] Parallel tasks selected: {selected_slots} slot(s)/account.")
        self._update_runtime_badges()
             
        self.queue_manager = AsyncQueueManager()
        self.queue_manager.signals.log_msg.connect(self.append_log, Qt.QueuedConnection)
        self.queue_manager.signals.job_updated.connect(self.on_job_updated, Qt.QueuedConnection)
        self.queue_manager.signals.account_runtime.connect(self.on_account_runtime, Qt.QueuedConnection)
        self.queue_manager.signals.account_auth_status.connect(self._on_account_auth_status, Qt.QueuedConnection)
        self.queue_manager.signals.show_warning.connect(self._show_session_warning, Qt.QueuedConnection)
        self.queue_manager.signals.warmup_progress.connect(self.warmup_progress_signal.emit, Qt.QueuedConnection)
        self.queue_manager.signals.warmup_complete.connect(self.warmup_complete_signal.emit, Qt.QueuedConnection)
        self.queue_manager.finished.connect(self.on_queue_manager_finished, Qt.QueuedConnection)
        self._on_generation_started()
        if hasattr(self, "progress_timer"):
            self.progress_timer.start()
        self.queue_manager.start()

        self._set_queue_controls_state("running")
        self._show_session_warning("")
        self.append_log("Queue Manager Started. Dispatching bots...")
        self._toast_success(
            "Automation Started",
            f"{pending_count} pending job(s) • {selected_slots} slot(s)/account",
        )

    def pause_queue_manager(self):
        if not self.queue_manager or not self.queue_manager.isRunning():
            return
        if self.queue_paused or self.queue_stopping:
            return

        self.queue_manager.pause_dispatch()
        self._set_queue_controls_state("paused")
        running = sum(1 for runtime in self.account_runtime_state.values() if int(runtime.get("active_slots", 0) or 0) > 0)
        self.append_log(
            f"[SYSTEM] Queue paused. {running} account(s) still running will finish current jobs. No new jobs will dispatch until resumed."
        )
        self._toast_warning(
            "Queue Paused",
            f"{running} account(s) finishing current jobs. No new dispatch.",
        )

    def resume_queue_manager(self):
        if not self.queue_manager or not self.queue_manager.isRunning():
            return
        if not self.queue_paused or self.queue_stopping:
            return

        self.queue_manager.resume_dispatch()
        self._set_queue_controls_state("running")
        pending = self._cached_pending_count()
        self.append_log(f"[SYSTEM] Queue resumed. {pending} pending job(s) ready for dispatch.")
        self._toast_success("Queue Resumed", f"{pending} pending job(s) ready for dispatch.")

    def stop_queue_manager(self):
        if self.queue_manager and self.queue_manager.isRunning():
            self.queue_manager.stop()
            self._set_queue_controls_state("stopping")
            self.append_log("[SYSTEM] Stop requested — running jobs reset to pending instantly.")
            self._toast_info("Stopped", "Running jobs moved back to pending.")
            # Immediately refresh queue table to show jobs back as pending
            QTimer.singleShot(200, self.load_queue_table)

    def force_stop_and_clear_queue(self):
        if not self._fluent_confirm(
            "Force Stop + Instant Clear",
            "This will cancel active jobs immediately and clear the full queue now.\n"
            "Running tasks may be lost. Continue?",
            confirm_label="Force Stop",
            cancel_label="Cancel",
        ):
            return

        from src.db.db_manager import clear_all_jobs

        if self.queue_manager and self.queue_manager.isRunning():
            self.pending_clear_all = False
            self.queue_manager.force_stop()
            self._set_queue_controls_state("stopping")
            self.append_log("[SYSTEM] Force stop sent. Active jobs cancelled immediately.")

        self._start_background_task(
            clear_all_jobs,
            on_finished=self._on_force_clear_finished,
        )

    def on_queue_manager_finished(self):
        finished_manager = self.sender()
        if finished_manager is not None and self.queue_manager is not None and finished_manager is not self.queue_manager:
            try:
                finished_manager.deleteLater()
            except Exception:
                pass
            return

        if finished_manager is None:
            finished_manager = self.queue_manager

        self.queue_manager = None
        self._set_queue_controls_state("stopped")
        if hasattr(self, "progress_timer"):
            self.progress_timer.stop()
        self._on_queue_stopped()
        self.append_log("Queue Manager safely stopped and contexts closed.")
        self.load_queue_table()
        self.load_failed_jobs()
        self.account_runtime_state = {
            str(self.acc_table.item(row, 1).data(Qt.UserRole) or self.acc_table.item(row, 1).text() or ""): {
                "status": "idle",
                "cooldown_until": 0.0,
                "active_slots": 0,
                "total_slots": 1,
                "detail": "Queue stopped",
            }
            for row in range(self.acc_table.rowCount())
            if self.acc_table.item(row, 1) is not None
        }
        self._refresh_account_runtime_cells()
        if self.pending_clear_all:
            from src.db.db_manager import clear_all_jobs
            self.pending_clear_all = False
            self._start_background_task(
                clear_all_jobs,
                on_finished=self._on_auto_clear_after_stop_finished,
            )
        if finished_manager is not None:
            try:
                finished_manager.deleteLater()
            except Exception:
                pass

    def on_job_updated(self, job_id, status, account, error_msg):
        self._request_queue_snapshot()
        # Refresh failed tab on any status change — retrying jobs show there too
        if str(status or "").strip().lower() in ("failed", "pending", "completed", "running"):
            self._schedule_failed_jobs_refresh()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        width = self.width()
        height = self.height()

        if hasattr(self, "sidebar"):
            self.sidebar.setFixedWidth(140 if width < 1400 else 180)
        if hasattr(self, "progress_widget"):
            self.progress_widget.setFixedHeight(24 if height < 800 else 28)
        if hasattr(self, "dashboard_body_splitter") and hasattr(self, "mode_tabs"):
            self._adjust_mode_tabs_height()
        if hasattr(self, "tabs") and self.tabs.currentWidget() is getattr(self, "tab_live_generation", None):
            self.ui_throttler.schedule("live_grid_resize", self._refresh_live_grid)

    def _on_account_auth_status(self, account_name, status, message):
        """Handle auth status changes from queue manager
        (logged_in / expired / rate_limited)."""
        if not hasattr(self, "_runtime_auth_status"):
            self._runtime_auth_status = {}

        if status == "expired":
            self._runtime_auth_status[account_name] = "expired"
            self.append_log(f"[{account_name}] Session expired: {message}")
        elif status == "rate_limited":
            # 429 hard pause — surface as a distinct state so the user
            # knows it's a temporary cooldown, not a session expiry.
            # Live countdown is shown via the account_runtime detail cell.
            self._runtime_auth_status[account_name] = "rate_limited"
            self.append_log(f"[{account_name}] ⏸ Rate limited: {message}")
        elif status == "quota_exhausted":
            # All image models hit their per-account per-model quota. This
            # account is held (peers keep running); the row shows a banner and
            # we log a clear notification. Google's quotas reset within ~6h.
            self._runtime_auth_status[account_name] = "quota_exhausted"
            self.append_log(
                f"[{account_name}] 🚫 Quota exhausted — all image models "
                f"({message or 'Nano Banana 2 / Lite / Pro'}) hit their limit. "
                f"This account is paused; other accounts keep running. "
                f"Quotas reset within ~6h."
            )
        elif status == "logged_in":
            # Clear expired/rate_limited/quota_exhausted status on success
            self._runtime_auth_status.pop(account_name, None)

        # Trigger immediate status refresh
        self._refresh_login_statuses()

    def on_account_runtime(self, account_name, runtime_status, cooldown_until_ts, active_slots, total_slots, detail):
        self.account_runtime_state[str(account_name)] = {
            "status": str(runtime_status or "idle"),
            "cooldown_until": float(cooldown_until_ts or 0.0),
            "active_slots": int(active_slots or 0),
            "total_slots": int(total_slots or 1),
            "detail": str(detail or ""),
        }
        self._refresh_account_runtime_cells()

    def _shutdown_worker_thread(self, worker, timeout_ms=4000):
        if not worker:
            return
        try:
            if hasattr(worker, "stop"):
                worker.stop()
        except Exception:
            pass
        try:
            worker.requestInterruption()
        except Exception:
            pass
        try:
            worker.wait(max(0, int(timeout_ms)))
        except Exception:
            pass

    def closeEvent(self, event):
        """Immediate exit — kill browsers, force terminate. No crash dialog."""
        event.accept()
        try:
            process_tracker.kill_all()
        except Exception:
            pass
        os._exit(0)

    def _kill_zombie_browsers(self, startup=False):
        try:
            killed = process_tracker.load_and_kill_stale() if startup else process_tracker.kill_all()
            if killed:
                phase = "STARTUP" if startup else "CLEANUP"
                print(f"[{phase}] Killed {killed} app browser process(es).")
        except Exception:
            pass

    def _cleanup_stale_locks(self):
        for base_dir in (get_sessions_dir(), get_session_clones_dir()):
            try:
                if not base_dir.exists():
                    continue
            except Exception:
                continue

            for pattern in ("Singleton*", "lockfile"):
                try:
                    lock_paths = list(base_dir.rglob(pattern))
                except Exception:
                    continue

                for lock_path in lock_paths:
                    try:
                        if lock_path.is_file():
                            lock_path.unlink()
                    except Exception:
                        pass



