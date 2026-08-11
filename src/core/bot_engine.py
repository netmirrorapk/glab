import asyncio
import base64
import hashlib
import inspect
import mimetypes
import os
import platform
import random
import shutil
import subprocess
import time
import uuid
from urllib.parse import urlsplit
import requests
from src.core.runtime_stdio import ensure_std_streams
ensure_std_streams()

try:
    from playwright_stealth import stealth_async
except Exception:
    stealth_async = None

from src.core.app_paths import get_session_clones_dir
from src.core.cloakbrowser_support import (
    load_cloakbrowser_api,
)
from src.core.recaptcha_mainworld import get_recaptcha_token_mainworld
from src.core.fingerprint_generator import FingerprintGenerator
from src.core.process_tracker import process_tracker, cleanup_session_locks
from src.db.db_manager import get_output_directory, get_setting, update_job_runtime_state


def _image_size(path):
    """(w, h) of an image — PIL if available, else ffprobe. None on failure."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", path],
            capture_output=True, text=True, timeout=20).stdout.strip()
        w, h = out.split("x")[:2]
        return int(w), int(h)
    except Exception:
        return None


def remove_image_watermark(path):
    """Remove the Nano Banana / Gemini bottom-right 'sparkle' (✦) watermark IN-PLACE
    via ffmpeg's delogo (interpolates the region from surrounding pixels). The mark
    sits a fixed offset in from the bottom-right corner (~7.3% of the shorter side),
    so the box is computed from that corner and scales to any resolution. Graceful
    no-op (image kept as-is) if ffmpeg/size aren't available. NOTE: this removes the
    VISIBLE mark only — Google's invisible SynthID watermark is NOT affected."""
    sz = _image_size(path)
    if not sz:
        return False
    w, h = sz
    if w < 200 or h < 200:
        return False
    short = min(w, h)
    # The sparkle sits a ~FIXED ~112px in from the bottom-right corner (measured on
    # both 1376x768 and 2752x1536 output), NOT proportional — so use a fixed offset
    # (scaled up only for very large images) with a generous box.
    off = max(112, int(short * 0.06))     # sparkle centre offset from the corner
    half = max(70, int(short * 0.055))    # half box size (covers the sparkle + margin)
    cx, cy = w - off, h - off
    bx = max(1, cx - half)
    by = max(1, cy - half)
    bw = max(2, min(2 * half, w - bx - 2))
    bh = max(2, min(2 * half, h - by - 2))
    tmp = path + ".nw" + (os.path.splitext(path)[1] or ".png")
    try:
        rc = subprocess.run(
            ["ffmpeg", "-y", "-i", path,
             "-vf", f"delogo=x={bx}:y={by}:w={bw}:h={bh}", tmp],
            capture_output=True, timeout=60).returncode
        if rc == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 1000:
            os.replace(tmp, path)
            return True
    except Exception:
        pass
    try:
        if os.path.isfile(tmp):
            os.remove(tmp)
    except Exception:
        pass
    return False


class GoogleLabsBot:
    """Core Playwright engine for Google Labs interactions."""
    FLOW_PAGE_URL = "https://labs.google/fx/tools/flow"
    FLOW_REFERER = "https://www.google.com"
    WEBDRIVER_OVERRIDE_SCRIPT = """
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined
        });
    """
    CHROME_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )
    PERSISTENT_CONTEXT_ARGS = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--window-size=1920,1080",
        "--lang=en-US",
    ]
    _shared_flow_project_id_by_account = {}
    _reference_upload_cache = {}
    _shared_reference_cache_locks = {}
    VIDEO_ENDPOINTS = {
        "text_to_video": "https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoText",
        "ingredients": "https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoReferenceImages",
        "frames_start": "https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoStartImage",
        "frames_start_end": "https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoStartAndEndImage",
    }
    VIDEO_MODEL_KEYS = {
        ("text_to_video", "fast"): "veo_3_1_t2v_fast_ultra",
        ("text_to_video", "lite"): "veo_3_1_t2v_lite",
        ("text_to_video", "lower_pri"): "veo_3_1_t2v_fast_ultra_relaxed",
        ("text_to_video", "quality"): "veo_3_1_t2v",
        ("ingredients", "fast"): "veo_3_1_r2v_fast_landscape_ultra",
        ("ingredients", "lite"): "veo_3_1_r2v_fast_landscape_ultra",
        ("ingredients", "lower_pri"): "veo_3_1_r2v_fast_landscape_ultra_relaxed",
        ("frames_start", "fast"): "veo_3_1_i2v_s_fast_ultra",
        ("frames_start", "lite"): "veo_3_1_i2v_s_fast_ultra",
        ("frames_start", "lower_pri"): "veo_3_1_i2v_s_fast_ultra_relaxed",
        ("frames_start", "quality"): "veo_3_1_i2v_s",
        ("frames_start_end", "fast"): "veo_3_1_i2v_s_fast_ultra_fl",
        ("frames_start_end", "lite"): "veo_3_1_i2v_s_fast_ultra_fl",
        ("frames_start_end", "lower_pri"): "veo_3_1_i2v_s_fast_fl_ultra_relaxed",
        ("frames_start_end", "quality"): "veo_3_1_i2v_s_fl",
    }
    NON_RETRYABLE_FAILURE_REASONS = (
        "PROMINENT_PERSON",
        "SAFETY_FILTER",
        "CONTENT_POLICY",
        "MODERATION",
        "BLOCKED",
        "HARMFUL",
        "SEXUALLY_EXPLICIT",
        "VIOLENCE",
        "HATE_SPEECH",
        "DANGEROUS",
        "TOXIC",
        "CHILD_SAFETY",
    )
    NON_RETRYABLE_ERROR_MESSAGES = (
        "PUBLIC_ERROR_PROMINENT_PEOPLE_FILTER_FAILED",
        "PUBLIC_ERROR_SAFETY_FILTER_FAILED",
        "PUBLIC_ERROR_CONTENT_POLICY",
        "PUBLIC_ERROR_MODERATION",
        "PUBLIC_ERROR_BLOCKED",
        "FILTER_FAILED",
    )

    @staticmethod
    def cleanup_browser_processes(log_callback=None, ports=None):
        logger = log_callback if callable(log_callback) else None
        try:
            killed = process_tracker.kill_all()
            if logger and killed:
                logger(f"[SYSTEM] Force-killed {killed} tracked browser process(es).")
        except Exception as exc:
            if logger:
                logger(f"[SYSTEM] Browser cleanup warning: {str(exc)[:100]}")

    def __init__(
        self,
        account_name,
        session_path,
        headless=True,
        proxy=None,
        browser_mode="headless",
        chrome_display="visible",
        cloak_display="headless",
        debug_port=None,
        random_fingerprint_enabled=True,
    ):
        self.account_name = account_name
        self.session_path = session_path
        self.headless = headless
        # Raw proxy URL (may be SOCKS5 with username/password).
        # Chromium/Playwright can't use SOCKS5 auth directly — route it
        # through a local HTTP-to-SOCKS5 bridge.
        raw_proxy = str(proxy or "").strip()
        self._raw_proxy = raw_proxy  # for logging
        self._bridged_proxy_used = False
        if raw_proxy:
            try:
                from src.core.proxy_bridge import get_or_create_bridge
                self.proxy = get_or_create_bridge(raw_proxy)
                if self.proxy != raw_proxy:
                    self._bridged_proxy_used = True
            except Exception:
                self.proxy = raw_proxy
        else:
            self.proxy = ""
        self.browser_mode = str(browser_mode or "headless").strip().lower()
        self.chrome_display = str(chrome_display or "visible").strip().lower()
        self.cloak_display = str(cloak_display or "headless").strip().lower()
        self.debug_port = int(debug_port or 0) if str(debug_port or "").strip() else 0
        self.random_fingerprint_enabled = bool(random_fingerprint_enabled)
        if self.browser_mode == "cloakbrowser":
            self.random_fingerprint_enabled = False
        self.browser = None
        self.chrome_process = None
        self.context = None
        self.page = None
        self._current_fingerprint = None
        self._fingerprint_init_script = ""
        self._fingerprint_context_script = ""
        self._last_project_resolve_error = ""
        self._cached_project_id = ""
        self._stealth_page_ids = set()
        self._warmed_page_ids = set()
        self._tracked_pids = set()

    async def _maybe_await(self, result):
        if inspect.isawaitable(result):
            return await result
        return result

    @staticmethod
    def _extract_context_process_pid(context):
        try:
            browser = getattr(context, "browser", None)
            process = getattr(browser, "process", None)
            pid = getattr(process, "pid", None)
            return int(pid) if pid else None
        except Exception:
            return None

    def _track_pid(self, pid):
        try:
            normalized = int(pid)
        except Exception:
            return None
        if normalized <= 0:
            return None
        self._tracked_pids.add(normalized)
        process_tracker.register(normalized)
        return normalized

    def _untrack_pid(self, pid):
        try:
            normalized = int(pid)
        except Exception:
            return
        self._tracked_pids.discard(normalized)
        process_tracker.unregister(normalized)

    def _register_context_pid(self, context):
        return self._track_pid(self._extract_context_process_pid(context))

    def _set_job_progress(self, job_id, *, step=None, poll_count=None, clear=False):
        if not job_id:
            return
        try:
            update_job_runtime_state(
                job_id,
                progress_step=step,
                progress_poll_count=poll_count,
                clear_progress=bool(clear),
            )
        except Exception:
            pass

    def _set_job_output_path(self, job_id, output_path):
        if not job_id or not output_path:
            return
        try:
            update_job_runtime_state(job_id, output_path=output_path)
        except Exception:
            pass

    def _account_root_key(self):
        name = str(self.account_name or "").strip()
        if "#s" in name:
            return name.split("#s", 1)[0].strip() or name
        return name

    def _build_reference_signature(self, ref_path):
        try:
            abs_path = os.path.abspath(ref_path)
            stat = os.stat(abs_path)
            return f"{abs_path}:{stat.st_size}:{int(stat.st_mtime)}"
        except Exception:
            return os.path.abspath(str(ref_path or ""))

    def _build_reference_cache_key(self, project_id, ref_path):
        return (
            str(project_id or "").strip(),
            self._build_reference_signature(ref_path),
        )

    def _cache_project_id(self, project_id):
        value = str(project_id or "").strip()
        if not value:
            return None
        self._cached_project_id = value
        self._shared_flow_project_id_by_account[self._account_root_key()] = value
        return value

    def clear_project_cache(self):
        account_key = self._account_root_key()
        existing = str(self._cached_project_id or "").strip()
        if existing:
            self.clear_reference_cache_for_project(existing)
        self._cached_project_id = ""
        self._last_project_resolve_error = ""
        self._shared_flow_project_id_by_account.pop(account_key, None)

    @classmethod
    def clear_account_project_cache(cls, account_name):
        account_key = str(account_name or "").strip()
        if not account_key:
            return
        project_id = str(cls._shared_flow_project_id_by_account.pop(account_key, "") or "").strip()
        if project_id:
            cls.clear_reference_cache_for_project(project_id)

    @classmethod
    def clear_reference_cache(cls):
        cls._reference_upload_cache.clear()
        cls._shared_reference_cache_locks.clear()

    @classmethod
    def clear_reference_cache_for_project(cls, project_id):
        project_id = str(project_id or "").strip()
        if not project_id:
            return
        keys_to_remove = [key for key in cls._reference_upload_cache if key and key[0] == project_id]
        for key in keys_to_remove:
            cls._reference_upload_cache.pop(key, None)
            cls._shared_reference_cache_locks.pop(key, None)

    @classmethod
    def _get_reference_cache_lock(cls, cache_key):
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        existing = cls._shared_reference_cache_locks.get(cache_key)
        if existing is not None:
            lock, lock_loop = existing
            if lock_loop is current_loop:
                return lock
        lock = asyncio.Lock()
        cls._shared_reference_cache_locks[cache_key] = (lock, current_loop)
        return lock

    def _get_float_setting(self, key, default, min_value, max_value):
        raw = str(get_setting(key, str(default)) or str(default)).strip()
        try:
            value = float(raw)
        except Exception:
            value = float(default)
        return max(float(min_value), min(float(max_value), value))

    def _get_api_humanized_delay_config(self):
        warmup_min = self._get_float_setting("api_humanized_warmup_min_seconds", 0.3, 0.0, 30.0)
        warmup_max = self._get_float_setting("api_humanized_warmup_max_seconds", 0.6, warmup_min, 40.0)
        ref_wait_min = self._get_float_setting("api_humanized_wait_ref_min_seconds", 0.3, 0.0, 20.0)
        ref_wait_max = self._get_float_setting("api_humanized_wait_ref_max_seconds", 0.8, ref_wait_min, 25.0)
        no_ref_wait_min = self._get_float_setting("api_humanized_wait_no_ref_min_seconds", 0.3, 0.0, 30.0)
        no_ref_wait_max = self._get_float_setting("api_humanized_wait_no_ref_max_seconds", 0.6, no_ref_wait_min, 35.0)
        return {
            "warmup_min": warmup_min,
            "warmup_max": warmup_max,
            "ref_wait_min": ref_wait_min,
            "ref_wait_max": ref_wait_max,
            "no_ref_wait_min": no_ref_wait_min,
            "no_ref_wait_max": no_ref_wait_max,
        }

    def _is_moderation_failure(self, error_detail):
        detail_upper = str(error_detail or "").upper()
        if not detail_upper:
            return False

        for reason in self.NON_RETRYABLE_FAILURE_REASONS:
            if reason in detail_upper:
                return True

        for error_text in self.NON_RETRYABLE_ERROR_MESSAGES:
            if error_text in detail_upper:
                return True

        return False

    def _build_cloak_fingerprint_seed(self):
        seed_base = str(self.account_name or "slot").strip() or "slot"
        return int(hashlib.md5(seed_base.encode("utf-8")).hexdigest()[:8], 16) % 99999

    @staticmethod
    def _build_cloak_launch_args(seed):
        """Build CloakBrowser launch args with Mac-specific compatibility fixes."""
        args = [f"--fingerprint={seed}"]
        if platform.system() == "Darwin":
            args.extend([
                "--disable-http2",
                "--fingerprint-storage-quota=5000",
                "--fingerprint-platform=windows",
                "--fingerprint-gpu-vendor=NVIDIA Corporation",
                "--fingerprint-gpu-renderer=NVIDIA GeForce RTX 3070",
            ])
        return args

    async def _goto_flow_page(self, page=None, target_url=None, wait_until="domcontentloaded", timeout=None):
        active_page = page or self.page
        if active_page is None:
            raise RuntimeError("Browser page is not available.")
        goto_kwargs = {"referer": self.FLOW_REFERER}
        if wait_until is not None:
            goto_kwargs["wait_until"] = wait_until
        if timeout is not None:
            goto_kwargs["timeout"] = timeout
        return await active_page.goto(target_url or self.FLOW_PAGE_URL, **goto_kwargs)

    def _cleanup_stale_profile_artifacts(self):
        profile_dir = os.path.abspath(str(self.session_path or ""))
        if not profile_dir or not os.path.isdir(profile_dir):
            return

        # Clean lock files (handles Mac symlinks via os.path.lexists).
        cleanup_session_locks(profile_dir)

        # Also remove stale Chrome runtime markers.
        for rel in ("DevToolsActivePort", "RunningChromeVersion"):
            target = os.path.join(profile_dir, rel)
            try:
                if os.path.lexists(target):
                    os.remove(target)
            except Exception:
                pass

    def _build_proxy_config(self):
        proxy_text = str(self.proxy or "").strip()
        if not proxy_text:
            return None

        parsed = urlsplit(proxy_text)
        if parsed.scheme and parsed.hostname and parsed.port:
            config = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
            if parsed.username:
                config["username"] = parsed.username
            if parsed.password:
                config["password"] = parsed.password
            return config
        return {"server": proxy_text}

    def _persistent_context_launch_options(self):
        fingerprint = self._current_fingerprint or {}
        screen = fingerprint.get("screen") or {"width": 1920, "height": 1080}
        languages = fingerprint.get("languages") or ["en-US", "en"]
        launch_options = {
            "headless": bool(self.headless),
            "ignore_default_args": ["--enable-automation"],
            "args": self._persistent_context_args(),
            "viewport": {
                "width": int(screen.get("width", 1920) or 1920),
                "height": int(screen.get("height", 1080) or 1080),
            },
            "user_agent": str(fingerprint.get("user_agent") or self.CHROME_USER_AGENT),
            "locale": str(languages[0] if languages else "en-US"),
        }
        timezone = str(fingerprint.get("timezone") or "").strip()
        if timezone:
            launch_options["timezone_id"] = timezone
        proxy_config = self._build_proxy_config()
        if proxy_config:
            launch_options["proxy"] = proxy_config
        return launch_options

    def _persistent_context_args(self):
        args = []
        fingerprint = self._current_fingerprint or {}
        screen = fingerprint.get("screen") or {"width": 1920, "height": 1080}
        languages = fingerprint.get("languages") or ["en-US", "en"]
        lang = str(languages[0] if languages else "en-US")
        window_size_arg = f"--window-size={int(screen.get('width', 1920) or 1920)},{int(screen.get('height', 1080) or 1080)}"
        lang_arg = f"--lang={lang}"
        for arg in self.PERSISTENT_CONTEXT_ARGS:
            if arg.startswith("--window-size=") or arg.startswith("--lang="):
                continue
            args.append(arg)
        args.extend([window_size_arg, lang_arg])
        return args

    def _find_chrome_path(self):
        system_name = platform.system()
        if system_name == "Windows":
            candidates = [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            ]
        elif system_name == "Darwin":
            candidates = [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            ]
        else:
            candidates = ["/usr/bin/google-chrome", "/usr/bin/chromium-browser", "/usr/bin/chromium"]

        for candidate in candidates:
            if candidate and os.path.exists(candidate):
                return candidate
        return None

    def _build_real_chrome_command(self, chrome_path):
        port = int(self.debug_port or 9222)
        fingerprint = self._current_fingerprint or {}
        screen = fingerprint.get("screen") or {"width": 1920, "height": 1080}
        languages = fingerprint.get("languages") or ["en-US", "en"]
        command = [
            chrome_path,
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={self.session_path}",
            "--no-first-run",
            "--disable-blink-features=AutomationControlled",
            f"--lang={str(languages[0] if languages else 'en-US')}",
            f"--window-size={int(screen.get('width', 1920) or 1920)},{int(screen.get('height', 1080) or 1080)}",
        ]
        user_agent = str(fingerprint.get("user_agent") or "").strip()
        if user_agent:
            command.append(f"--user-agent={user_agent}")
        if self.chrome_display == "headless":
            command.append("--headless=new")
        if self.proxy:
            command.append(f"--proxy-server={self.proxy}")
        return command

    def _resolve_browser_path(self, playwright_instance):
        browser_type = getattr(playwright_instance, "chromium", None)
        executable_path = getattr(browser_type, "executable_path", "")
        try:
            if callable(executable_path):
                executable_path = executable_path()
        except Exception:
            executable_path = ""
        return str(executable_path or "").strip()

    def _is_cdp_endpoint_live(self, port):
        try:
            response = requests.get(f"http://127.0.0.1:{int(port)}/json/version", timeout=2)
            return response.ok
        except Exception:
            return False

    def _kill_chrome_on_port(self, port, log_callback=None):
        port = int(port or 0)
        if port <= 0:
            return

        current_pid = None
        try:
            if self.chrome_process is not None and self.chrome_process.poll() is None:
                current_pid = getattr(self.chrome_process, "pid", None)
                self.chrome_process.terminate()
                self.chrome_process.wait(timeout=5)
        except Exception:
            try:
                if self.chrome_process is not None and self.chrome_process.poll() is None:
                    self.chrome_process.kill()
            except Exception:
                pass
        finally:
            if current_pid:
                self._untrack_pid(current_pid)
            self.chrome_process = None

        if platform.system() == "Windows":
            try:
                output = subprocess.check_output(
                    ["netstat", "-ano"],
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
                )
                target = f":{port}"
                pids = set()
                for line in output.splitlines():
                    if target not in line:
                        continue
                    parts = line.split()
                    if len(parts) >= 5 and parts[-1].isdigit():
                        pids.add(parts[-1])
                for pid in sorted(pids):
                    try:
                        normalized = int(pid)
                    except Exception:
                        continue
                    if not process_tracker.is_tracked(normalized):
                        continue
                    process_tracker.kill_pid(normalized)
                    self._tracked_pids.discard(normalized)
            except Exception as kill_error:
                if callable(log_callback):
                    log_callback(f"[{self.account_name}] stale Chrome cleanup warning: {kill_error}")

    async def _apply_browser_overrides(self, page, log_callback=None):
        if page is None:
            return
        try:
            if self.context is not None:
                await self._maybe_await(self.context.add_init_script(self.WEBDRIVER_OVERRIDE_SCRIPT))
        except Exception as override_error:
            if callable(log_callback):
                log_callback(f"[{self.account_name}] webdriver init-script warning: {override_error}")
        try:
            await self._maybe_await(page.add_init_script(self.WEBDRIVER_OVERRIDE_SCRIPT))
        except Exception as override_error:
            if callable(log_callback):
                log_callback(f"[{self.account_name}] webdriver page-script warning: {override_error}")
        try:
            await self._maybe_await(page.evaluate(
                "() => { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); }"
            ))
        except Exception:
            pass

    def _prepare_session_fingerprint(self, log_callback=None, label="Fingerprint"):
        if not self.random_fingerprint_enabled:
            self._current_fingerprint = None
            self._fingerprint_init_script = ""
            self._fingerprint_context_script = ""
            return None

        fingerprint = FingerprintGenerator.generate()
        self._current_fingerprint = fingerprint
        self._fingerprint_init_script = FingerprintGenerator.build_init_script(fingerprint)
        self._fingerprint_context_script = ""
        if callable(log_callback):
            log_callback(
                f"[{self.account_name}] {label}: "
                f"Chrome/{fingerprint['chrome_version']}, "
                f"Screen: {fingerprint['screen']['width']}x{fingerprint['screen']['height']}, "
                f"GPU: {fingerprint['gpu']['renderer']}"
            )
        return fingerprint

    async def _apply_fingerprint(self, page, log_callback=None):
        fingerprint = self._current_fingerprint
        script = self._fingerprint_init_script
        if page is None or not fingerprint or not script:
            return

        try:
            if self.context is not None and self._fingerprint_context_script != script:
                await self._maybe_await(self.context.add_init_script(script))
                self._fingerprint_context_script = script
        except Exception as fingerprint_error:
            if callable(log_callback):
                log_callback(f"[{self.account_name}] fingerprint init-script warning: {fingerprint_error}")

        try:
            await self._maybe_await(page.add_init_script(script))
        except Exception as fingerprint_error:
            if callable(log_callback):
                log_callback(f"[{self.account_name}] fingerprint page-script warning: {fingerprint_error}")

        try:
            await self._maybe_await(page.evaluate(script))
        except Exception:
            pass

    async def _emit_launch_debug(self, page, browser_path, log_callback=None):
        if page is None or not callable(log_callback):
            return
        try:
            ua = await self._maybe_await(page.evaluate("navigator.userAgent"))
        except Exception as ua_error:
            ua = f"<error: {ua_error}>"
        try:
            webdriver_flag = await self._maybe_await(page.evaluate("navigator.webdriver"))
        except Exception as wd_error:
            webdriver_flag = f"<error: {wd_error}>"
        try:
            timezone = await self._maybe_await(page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone"))
        except Exception as timezone_error:
            timezone = f"<error: {timezone_error}>"
        try:
            language = await self._maybe_await(page.evaluate("navigator.language"))
        except Exception as language_error:
            language = f"<error: {language_error}>"
        log_callback(f"[DEBUG] User-Agent: {ua}")
        log_callback(f"[DEBUG] navigator.webdriver: {webdriver_flag}")
        log_callback(f"[DEBUG] navigator.language: {language}")
        log_callback(f"[DEBUG] timezone: {timezone}")

    async def _apply_stealth_to_page(self, page, log_callback=None):
        if page is None or stealth_async is None:
            return

        page_marker = id(page)
        if page_marker in self._stealth_page_ids:
            return

        try:
            await stealth_async(page)
            self._stealth_page_ids.add(page_marker)
        except Exception as stealth_error:
            if callable(log_callback):
                log_callback(f"[{self.account_name}] Stealth setup warning: {stealth_error}")

    async def _warmup_page(self, page, log_callback=None):
        if page is None:
            return
        try:
            if page.is_closed():
                return
        except Exception:
            return

        page_marker = id(page)
        if page_marker in self._warmed_page_ids:
            return

        if callable(log_callback):
            log_callback(f"[{self.account_name}] Warm-up browsing on Flow page before submit...")

        try:
            await page.bring_to_front()
        except Exception:
            pass

        try:
            await page.evaluate("window.scrollTo(0, 300)")
            await asyncio.sleep(0.5)
            await page.evaluate("window.scrollTo(0, 0)")
            await asyncio.sleep(0.3)
        except Exception:
            pass

        try:
            await page.mouse.move(random.randint(100, 800), random.randint(100, 500))
            await asyncio.sleep(0.2)
            await page.mouse.move(random.randint(200, 600), random.randint(200, 400))
            await asyncio.sleep(0.3)
            await page.mouse.click(500, 300)
            await asyncio.sleep(0.5)
        except Exception:
            pass

        self._warmed_page_ids.add(page_marker)

    def _log_session_diagnostics(self, log_callback=None):
        if not callable(log_callback):
            return
        tag = f"[DEBUG:{self.account_name}]"
        sp = os.path.abspath(str(self.session_path or ""))
        log_callback(f"{tag} Session path: {sp}")
        log_callback(f"{tag} Session dir exists: {os.path.isdir(sp)}")
        if not os.path.isdir(sp):
            return
        top_items = sorted(os.listdir(sp))[:15]
        log_callback(f"{tag} Session root contents: {top_items}")
        default_dir = os.path.join(sp, "Default")
        if not os.path.isdir(default_dir):
            log_callback(f"{tag} Default/ dir NOT FOUND in session!")
            return
        default_items = sorted(os.listdir(default_dir))[:20]
        log_callback(f"{tag} Default/ contents: {default_items}")
        for cookie_rel in ("Default/Network/Cookies", "Default/Cookies", "Network/Cookies", "Cookies"):
            cp = os.path.join(sp, cookie_rel)
            if os.path.exists(cp):
                sz = os.path.getsize(cp)
                log_callback(f"{tag} Cookies FOUND: {cookie_rel} ({sz} bytes)")
                break
        else:
            log_callback(f"{tag} Cookies NOT FOUND in any location!")
        ls_dir = os.path.join(default_dir, "Local Storage")
        if os.path.isdir(ls_dir):
            log_callback(f"{tag} Local Storage: {len(os.listdir(ls_dir))} files")
        else:
            log_callback(f"{tag} Local Storage: NOT FOUND")
        for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            lp = os.path.join(sp, lock_name)
            log_callback(f"{tag} {lock_name}: exists={os.path.exists(lp)}, symlink={os.path.islink(lp)}")

    EXPORTED_COOKIES_FILENAME = "exported_cookies.json"

    async def _import_exported_cookies(self, log_callback=None):
        """Import cookies from exported_cookies.json into the live browser context.
        Fixes sameSite/secure mismatches that cause Playwright to silently reject cookies."""
        if not self.context:
            return 0
        cookies_json = os.path.join(os.path.abspath(str(self.session_path or "")), self.EXPORTED_COOKIES_FILENAME)
        if not os.path.exists(cookies_json):
            return 0
        try:
            import json as _json
            with open(cookies_json, "r", encoding="utf-8") as f:
                cookies = _json.load(f)
            if not cookies or not isinstance(cookies, list):
                return 0

            valid_cookies = []
            skipped = 0
            fixed = 0
            for c in cookies:
                name = c.get("name", "")
                value = c.get("value", "")
                domain = c.get("domain", "")
                if not name or not value or not domain:
                    skipped += 1
                    continue

                # Fix: sameSite=None requires secure=True — Playwright silently
                # rejects cookies that violate this. Convert to Lax to preserve them.
                same_site = c.get("sameSite", "")
                secure = c.get("secure", False)
                if same_site == "None" and not secure:
                    c["sameSite"] = "Lax"
                    fixed += 1

                # Validate sameSite value
                if c.get("sameSite", "") not in ("Strict", "Lax", "None", ""):
                    c["sameSite"] = "Lax"

                valid_cookies.append(c)

            if not valid_cookies:
                if callable(log_callback):
                    log_callback(f"[{self.account_name}] No valid cookies after filtering ({skipped} skipped).")
                return 0

            await self._maybe_await(self.context.add_cookies(valid_cookies))
            google_count = sum(1 for c in valid_cookies if "google" in (c.get("domain") or "").lower())
            if callable(log_callback):
                msg = f"[{self.account_name}] Imported {len(valid_cookies)} cookies ({google_count} Google)"
                if fixed:
                    msg += f", fixed {fixed} sameSite"
                if skipped:
                    msg += f", skipped {skipped} empty"
                msg += "."
                log_callback(msg)
            return len(valid_cookies)
        except Exception as exc:
            if callable(log_callback):
                log_callback(f"[{self.account_name}] Cookie import warning: {str(exc)[:80]}")
            return 0

    async def _export_cookies_to_json(self, log_callback=None):
        """Export cookies from the live browser context to exported_cookies.json."""
        if not self.context:
            return 0
        try:
            cookies = await self._maybe_await(self.context.cookies())
            if not cookies:
                return 0
            import json as _json
            cookies_json = os.path.join(os.path.abspath(str(self.session_path or "")), self.EXPORTED_COOKIES_FILENAME)
            with open(cookies_json, "w", encoding="utf-8") as f:
                _json.dump(cookies, f)
            if callable(log_callback):
                log_callback(f"[{self.account_name}] Exported {len(cookies)} cookies to JSON.")
            return len(cookies)
        except Exception as exc:
            if callable(log_callback):
                log_callback(f"[{self.account_name}] Cookie export warning: {str(exc)[:80]}")
            return 0

    async def initialize(self, playwright_instance, log_callback=None, fingerprint_label="Fingerprint"):
        if self.context is not None or self.browser is not None or self.chrome_process is not None or self.page is not None:
            try:
                await self.cleanup()
            except Exception:
                pass
        self._cleanup_stale_profile_artifacts()
        self._log_session_diagnostics(log_callback)
        self._prepare_session_fingerprint(log_callback, label=fingerprint_label)
        if self.browser_mode == "cloakbrowser":
            await self._initialize_cloakbrowser(playwright_instance, log_callback)
            return
        if self.browser_mode == "real_chrome":
            await self._initialize_real_chrome(playwright_instance, log_callback)
            return
        launch_options = self._persistent_context_launch_options()
        browser_path = self._resolve_browser_path(playwright_instance)
        if browser_path:
            launch_options["executable_path"] = browser_path
        if callable(log_callback):
            log_callback(f"[DEBUG] Using browser: {browser_path or '<default>'}")
            log_callback(f"[DEBUG] ignore_default_args = {['--enable-automation']}")
            log_callback(f"[DEBUG] headless = {bool(self.headless)}")
        self.context = await playwright_instance.chromium.launch_persistent_context(
            user_data_dir=self.session_path,
            **launch_options,
        )
        self._register_context_pid(self.context)
        pages = self.context.pages
        self.page = pages[0] if pages else await self.context.new_page()
        self.page.set_default_timeout(60000)
        await self._apply_browser_overrides(self.page, log_callback)
        await self._apply_fingerprint(self.page, log_callback)
        await self._apply_stealth_to_page(self.page, log_callback)
        await self._import_exported_cookies(log_callback)
        await self._emit_launch_debug(self.page, browser_path, log_callback)

    async def _initialize_cloakbrowser(self, playwright_instance, log_callback=None):
        cloak_api = load_cloakbrowser_api()
        cloak_binary_info = cloak_api.get("binary_info")
        cloak_ensure_binary = cloak_api.get("ensure_binary")
        cloak_persistent_async = cloak_api.get("persistent_async")

        if not cloak_api.get("available") or cloak_persistent_async is None:
            if callable(log_callback):
                log_callback(f"[{self.account_name}] CloakBrowser not installed. Falling back to Real Chrome CDP.")
            await self._initialize_real_chrome(playwright_instance, log_callback)
            return

        loop = asyncio.get_running_loop()
        try:
            installed = False
            if callable(cloak_binary_info):
                info = await loop.run_in_executor(None, cloak_binary_info)
                installed = bool((info or {}).get("installed"))
            if not installed:
                if callable(log_callback):
                    log_callback(
                        f"[{self.account_name}] CloakBrowser binary downloading (~200MB, first time only)..."
                    )
                if not callable(cloak_ensure_binary):
                    raise RuntimeError("CloakBrowser binary installer is unavailable.")
                await loop.run_in_executor(None, cloak_ensure_binary)
                if callable(log_callback):
                    log_callback(f"[{self.account_name}] CloakBrowser binary downloaded!")
        except Exception as exc:
            if callable(log_callback):
                log_callback(f"[{self.account_name}] CloakBrowser binary download failed: {str(exc)[:100]}")
                log_callback(f"[{self.account_name}] Falling back to Real Chrome CDP.")
            await self._initialize_real_chrome(playwright_instance, log_callback)
            return

        headless = self.cloak_display == "headless"
        stealth_visible = self.cloak_display == "stealth_visible"
        if stealth_visible:
            headless = False  # Launch as visible for reCAPTCHA trust
        seed = self._build_cloak_fingerprint_seed()
        cloak_args = self._build_cloak_launch_args(seed)
        if stealth_visible:
            cloak_args.extend(["--window-position=-3000,-3000", "--window-size=800,600"])

        # Login now uses pure Chrome on ALL platforms. Chrome's session files
        # are incompatible with CloakBrowser. Always use a fresh _cloak profile
        # and import cookies via exported_cookies.json.
        cloak_profile = os.path.abspath(str(self.session_path or "")) + "_cloak"
        os.makedirs(cloak_profile, exist_ok=True)
        cleanup_session_locks(cloak_profile)

        if callable(log_callback):
            mode_label = "fresh profile + cookie import"
            log_callback(
                f"[{self.account_name}] CloakBrowser mode: {mode_label} "
                f"(seed={seed}, headless={headless}, profile={cloak_profile})"
            )
            # Show proxy status so user can verify it's active
            if self._raw_proxy:
                if self._bridged_proxy_used:
                    log_callback(
                        f"[{self.account_name}] Proxy: {self._raw_proxy[:40]}... "
                        f"-> SOCKS5 bridge -> {self.proxy}"
                    )
                else:
                    log_callback(f"[{self.account_name}] Proxy: {self.proxy}")
            else:
                log_callback(f"[{self.account_name}] Proxy: none (direct)")

        last_error = None
        try:
            self.context = await cloak_persistent_async(
                cloak_profile,
                headless=headless,
                args=cloak_args,
                proxy=(self.proxy or None),
                humanize=True,
            )
            self._register_context_pid(self.context)
            pages = list(getattr(self.context, "pages", []) or [])
            self.page = pages[0] if pages else await self._maybe_await(self.context.new_page())
            self.page.set_default_timeout(60000)

            if stealth_visible and callable(log_callback):
                log_callback(f"[{self.account_name}] Stealth visible: window launched off-screen.")

            await self._apply_browser_overrides(self.page, log_callback)
            await self._apply_fingerprint(self.page, log_callback)
            await self._apply_stealth_to_page(self.page, log_callback)
            await self._emit_launch_debug(self.page, "cloakbrowser", log_callback)
            # Bug #6: Check cookie import result — warn if zero cookies
            cookie_count = await self._import_exported_cookies(log_callback)
            if cookie_count == 0 and callable(log_callback):
                log_callback(f"[{self.account_name}] WARNING: 0 cookies imported! Browser may not be authenticated.")
            if callable(log_callback):
                log_callback(f"[{self.account_name}] CloakBrowser ready! Score expected: 0.9")
            return
        except Exception as exc:
            last_error = exc
            try:
                await self.cleanup()
            except Exception:
                pass
        if callable(log_callback):
            log_callback(f"[{self.account_name}] CloakBrowser failed: {str(last_error)[:100]}")
            log_callback(f"[{self.account_name}] Falling back to Real Chrome CDP.")
        await self._initialize_real_chrome(playwright_instance, log_callback)

    async def _initialize_real_chrome(self, playwright_instance, log_callback=None):
        chrome_path = self._find_chrome_path()
        if not chrome_path:
            raise RuntimeError("Google Chrome not found for Real Chrome (CDP) mode.")

        port = int(self.debug_port or 9222)
        command = self._build_real_chrome_command(chrome_path)
        if callable(log_callback):
            log_callback(f"[DEBUG] Real Chrome executable: {chrome_path}")
            log_callback(f"[DEBUG] Real Chrome CDP port: {port}")
            log_callback(
                f"[DEBUG] Chrome display: {'headless (--headless=new)' if self.chrome_display == 'headless' else 'visible'}"
            )
            log_callback(f"[DEBUG] ignore_default_args = {['--enable-automation']} (CDP mode uses real Chrome subprocess)")
            if self.chrome_display == "headless":
                log_callback(f"[{self.account_name}] Real Chrome CDP (headless=new)")
            else:
                log_callback(f"[{self.account_name}] Real Chrome CDP (visible window)")

        creationflags = 0
        popen_kwargs = {}
        if platform.system() == "Windows":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            popen_kwargs["stdout"] = subprocess.DEVNULL
            popen_kwargs["stderr"] = subprocess.DEVNULL

        self.chrome_process = subprocess.Popen(
            command,
            creationflags=creationflags,
            **popen_kwargs,
        )
        self._track_pid(getattr(self.chrome_process, "pid", None))
        await asyncio.sleep(3)
        if not self._is_cdp_endpoint_live(port):
            if callable(log_callback):
                log_callback(f"[{self.account_name}] CDP endpoint on port {port} was not ready. Restarting Chrome...")
            self._kill_chrome_on_port(port, log_callback)
            self.chrome_process = subprocess.Popen(
                command,
                creationflags=creationflags,
                **popen_kwargs,
            )
            self._track_pid(getattr(self.chrome_process, "pid", None))
            await asyncio.sleep(3)
        if not self._is_cdp_endpoint_live(port):
            raise RuntimeError(f"Chrome CDP did not start on port {port}.")

        self.browser = await playwright_instance.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        contexts = list(self.browser.contexts)
        if not contexts:
            raise RuntimeError("Real Chrome CDP connected but no browser context was available.")
        self.context = contexts[0]
        pages = self.context.pages
        self.page = pages[0] if pages else await self.context.new_page()
        self.page.set_default_timeout(60000)
        await self._apply_browser_overrides(self.page, log_callback)
        await self._apply_fingerprint(self.page, log_callback)
        await self._apply_stealth_to_page(self.page, log_callback)
        await self._import_exported_cookies(log_callback)
        await self._emit_launch_debug(self.page, chrome_path, log_callback)

    async def is_session_alive(self):
        if not self.context:
            return False
        try:
            if self.page is None:
                return False
            return not self.page.is_closed()
        except Exception:
            return False

    async def ensure_active_page(self, log_callback=None):
        if not self.context:
            return False

        try:
            if self.page and not self.page.is_closed():
                return True
        except Exception:
            pass

        try:
            open_pages = [p for p in self.context.pages if not p.is_closed()]
            if open_pages:
                self.page = open_pages[-1]
                self.page.set_default_timeout(60000)
                await self._apply_browser_overrides(self.page, log_callback)
                await self._apply_fingerprint(self.page, log_callback)
                await self._apply_stealth_to_page(self.page, log_callback)
                if log_callback:
                    log_callback(f"[{self.account_name}] Re-attached to active browser tab.")
                return True
        except Exception:
            pass

        try:
            self.page = await self.context.new_page()
            self.page.set_default_timeout(60000)
            await self._apply_browser_overrides(self.page, log_callback)
            await self._apply_fingerprint(self.page, log_callback)
            await self._apply_stealth_to_page(self.page, log_callback)
            if log_callback:
                log_callback(f"[{self.account_name}] Created a new browser tab to continue.")
            return True
        except Exception:
            return False

    async def open_fresh_tab(self, log_callback=None):
        """Start a new tab for the next job and retire stale tabs."""
        if not self.context:
            return False

        previous_page = self.page
        try:
            new_page = await self.context.new_page()
            new_page.set_default_timeout(60000)
            await self._apply_browser_overrides(new_page, log_callback)
            await self._apply_fingerprint(new_page, log_callback)
            await self._apply_stealth_to_page(new_page, log_callback)
            self.page = new_page

            if previous_page and previous_page != new_page:
                try:
                    if not previous_page.is_closed():
                        await previous_page.close()
                except Exception:
                    pass

            # Keep context lightweight by closing any extra stale tabs.
            try:
                for page in self.context.pages:
                    if page == self.page:
                        continue
                    if not page.is_closed():
                        await page.close()
            except Exception:
                pass

            if log_callback:
                log_callback(f"[{self.account_name}] Opened fresh tab for isolated job execution.")
            return True
        except Exception:
            return await self.ensure_active_page(log_callback)

    async def refresh_flow_page(self, log_callback=None):
        if not self.context:
            return False

        if not await self.ensure_active_page(log_callback):
            return False

        try:
            current_url = str(self.page.url or "")
        except Exception:
            current_url = ""

        try:
            if "labs.google/fx/tools/flow" in current_url:
                await self.page.reload(wait_until="domcontentloaded", timeout=30000)
            else:
                await self._goto_flow_page(wait_until="domcontentloaded", timeout=30000)
            await self._apply_stealth_to_page(self.page, log_callback)
            self._warmed_page_ids.discard(id(self.page))
            await self._export_cookies_to_json(log_callback)
            await asyncio.sleep(2)
            return True
        except Exception as refresh_error:
            if callable(log_callback):
                log_callback(f"[{self.account_name}] Auto-refresh warning: {refresh_error}")
            return False

    async def _wait_for_visible(self, locator, timeout=3000):
        try:
            await locator.wait_for(state="visible", timeout=timeout)
            return True
        except Exception:
            return False

    async def _click_generate(self):
        generate_btn = self.page.locator(
            "//button[.//i[normalize-space(text())='arrow_forward'] "
            "or .//mat-icon[normalize-space(text())='arrow_forward']]"
        ).first

        try:
            if await generate_btn.count() > 0 and await self._wait_for_visible(generate_btn, timeout=3000):
                await generate_btn.click(force=True)
                return
        except Exception:
            pass

        # Fallback if selector changes.
        await self.page.keyboard.press("Enter")

    def _normalize_src(self, src):
        raw = str(src or "").strip()
        if not raw:
            return ""
        if raw.startswith("data:"):
            return raw[:128]
        return raw.split("#", 1)[0].split("?", 1)[0]

    async def _count_compose_ref_chips(self):
        try:
            return await self.page.evaluate(
                """
                () => {
                    const isVisible = (el) => {
                        if (!el || !el.isConnected) return false;
                        const style = window.getComputedStyle(el);
                        if (!style || style.display === "none" || style.visibility === "hidden" || style.opacity === "0") {
                            return false;
                        }
                        const r = el.getBoundingClientRect();
                        return r.width > 8 && r.height > 8;
                    };

                    const editor = document.querySelector("[data-slate-editor='true'], [role='textbox'][contenteditable='true'], textarea");
                    if (!editor) return 0;
                    const er = editor.getBoundingClientRect();

                    const nodes = Array.from(
                        document.querySelectorAll("button[data-card-open], [data-card-open], button, div")
                    );

                    let count = 0;
                    for (const el of nodes) {
                        if (!isVisible(el)) continue;
                        if (!el.querySelector("img")) continue;
                        if (el.closest("[data-radix-popper-content-wrapper], [role='dialog'], [role='menu']")) continue;

                        const r = el.getBoundingClientRect();
                        if (r.width < 24 || r.width > 110 || r.height < 24 || r.height > 110) continue;

                        const cx = r.left + r.width / 2;
                        const cy = r.top + r.height / 2;
                        if (cy < er.top - 260 || cy > er.bottom + 160 || cx < er.left - 180 || cx > er.right + 220) {
                            continue;
                        }
                        count += 1;
                    }
                    return count;
                }
                """
            )
        except Exception:
            return 0

    async def _open_asset_browser_from_compose(self):
        try:
            return await self.page.evaluate(
                """
                () => {
                    const isVisible = (el) => {
                        if (!el || !el.isConnected || el.disabled) return false;
                        const style = window.getComputedStyle(el);
                        if (!style || style.display === "none" || style.visibility === "hidden" || style.opacity === "0") {
                            return false;
                        }
                        const r = el.getBoundingClientRect();
                        return r.width > 10 && r.height > 10;
                    };

                    const iconTokens = (el) => {
                        const out = new Set();
                        if (!el || typeof el.querySelectorAll !== "function") return out;
                        const icons = el.querySelectorAll(
                            "i, mat-icon, .material-symbols, .material-symbols-outlined, .google-symbols, [class*='material-symbols']"
                        );
                        for (const node of icons) {
                            const token = String(node.textContent || "").trim().toLowerCase();
                            if (token) out.add(token);
                        }
                        return out;
                    };

                    const editor = document.querySelector("[data-slate-editor='true'], [role='textbox'][contenteditable='true'], textarea");
                    if (!editor) return { ok: false, reason: "editor_missing" };
                    const er = editor.getBoundingClientRect();

                    let best = null;
                    let bestScore = -Infinity;

                    for (const btn of document.querySelectorAll("button, [role='button']")) {
                        if (!isVisible(btn)) continue;
                        if (btn.closest("#af-bot-panel")) continue;

                        const txt = String(btn.textContent || "").toLowerCase().replace(/\\s+/g, " ").trim();
                        const aria = String(btn.getAttribute("aria-label") || "").toLowerCase();
                        const title = String(btn.getAttribute("title") || "").toLowerCase();
                        const icons = iconTokens(btn);

                        const isSubmit =
                            icons.has("arrow_forward") ||
                            icons.has("send") ||
                            txt.includes("create") ||
                            txt.includes("generate") ||
                            aria.includes("create") ||
                            aria.includes("generate");
                        if (isSubmit) continue;

                        const isAdd =
                            icons.has("add_2") ||
                            icons.has("add") ||
                            icons.has("add_photo_alternate") ||
                            txt === "+" ||
                            aria.includes("add") ||
                            aria.includes("upload") ||
                            title.includes("add") ||
                            title.includes("upload");
                        if (!isAdd) continue;

                        const r = btn.getBoundingClientRect();
                        const cx = r.left + r.width / 2;
                        const cy = r.top + r.height / 2;
                        const targetX = er.left + Math.min(90, er.width * 0.30);
                        const targetY = er.bottom - 8;
                        const dist = Math.hypot(cx - targetX, cy - targetY);

                        let score = 300 - dist * 0.25;
                        if (cy < er.top - 240 || cy > er.bottom + 180) score -= 180;
                        if (r.width > 220 || r.height > 140) score -= 80;
                        if (btn.closest("[data-radix-popper-content-wrapper], [role='dialog'], [role='menu']")) score -= 100;

                        if (score > bestScore) {
                            bestScore = score;
                            best = btn;
                        }
                    }

                    if (!best || bestScore < -50) return { ok: false, reason: "add_button_missing" };

                    try {
                        best.click();
                    } catch {
                        try {
                            best.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                        } catch {
                            return { ok: false, reason: "add_click_failed" };
                        }
                    }

                    return { ok: true };
                }
                """
            )
        except Exception:
            return {"ok": False, "reason": "asset_browser_open_exception"}

    async def _select_asset_row(self, ref_basename):
        try:
            return await self.page.evaluate(
                """
                (needle) => {
                    const normalize = (text) =>
                        String(text || "")
                            .toLowerCase()
                            .replace(/\\.[a-z0-9]{2,5}$/i, "")
                            .replace(/[_-]+/g, " ")
                            .replace(/\\s+/g, " ")
                            .trim();

                    const isVisible = (el) => {
                        if (!el || !el.isConnected) return false;
                        const style = window.getComputedStyle(el);
                        if (!style || style.display === "none" || style.visibility === "hidden" || style.opacity === "0") {
                            return false;
                        }
                        const r = el.getBoundingClientRect();
                        return r.width > 10 && r.height > 10;
                    };

                    const lowerNeedle = normalize(needle);
                    const search = document.querySelector(
                        "input[type='text'][placeholder*='Search'][placeholder*='Asset'], " +
                        "[data-radix-popper-content-wrapper] input[type='text'], [role='dialog'] input[type='text']"
                    );

                    if (search && isVisible(search) && lowerNeedle) {
                        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")?.set;
                        if (setter) setter.call(search, lowerNeedle);
                        else search.value = lowerNeedle;
                        search.dispatchEvent(new Event("input", { bubbles: true }));
                        search.dispatchEvent(new Event("change", { bubbles: true }));
                    }

                    const roots = [
                        document.querySelector("[data-radix-popper-content-wrapper]"),
                        document.querySelector("[role='dialog']"),
                        document.querySelector("[role='menu']"),
                    ].filter(Boolean);

                    const candidateRoots = roots.length ? roots : [document];
                    let rows = [];
                    for (const root of candidateRoots) {
                        const found = Array.from(root.querySelectorAll("button, [role='button'], [tabindex], div, li"))
                            .filter((el) => isVisible(el) && el.querySelector("img"))
                            .filter((el) => {
                                const txt = String(el.textContent || "").toLowerCase();
                                if (txt.includes("upload image") || txt.includes("search for assets")) return false;
                                const r = el.getBoundingClientRect();
                                return r.width >= 80 && r.height >= 30 && r.height <= 130;
                            });
                        if (found.length) {
                            rows = found;
                            break;
                        }
                    }

                    if (!rows.length) return { ok: false, reason: "asset_rows_missing" };

                    let target = null;
                    if (lowerNeedle) {
                        target = rows.find((row) => normalize(row.textContent).includes(lowerNeedle));
                    }
                    if (!target) target = rows[0];

                    try {
                        target.click();
                    } catch {
                        try {
                            target.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                        } catch {
                            return { ok: false, reason: "asset_row_click_failed" };
                        }
                    }

                    return {
                        ok: true,
                        rowText: String(target.textContent || "").replace(/\\s+/g, " ").trim().slice(0, 120),
                    };
                }
                """,
                ref_basename or "",
            )
        except Exception:
            return {"ok": False, "reason": "asset_row_select_exception"}

    async def _upload_reference_via_asset_browser(self, ref_path, log_callback):
        if not ref_path or not os.path.exists(ref_path):
            return False, "reference path missing"

        ref_basename = os.path.splitext(os.path.basename(ref_path))[0].strip()
        chips_before = await self._count_compose_ref_chips()
        ingredients_before = await self._count_store_ingredients()

        open_result = await self._open_asset_browser_from_compose()
        if not open_result.get("ok"):
            return False, f"asset browser open failed ({open_result.get('reason', 'unknown')})"

        browser_open = False
        for _ in range(20):
            try:
                browser_open = await self.page.evaluate(
                    """
                    () => !!(
                        document.querySelector("input[type='text'][placeholder*='Search'][placeholder*='Asset']") ||
                        document.querySelector("[data-radix-popper-content-wrapper], [role='dialog'], [role='menu']")
                    )
                    """
                )
            except Exception:
                browser_open = False
            if browser_open:
                break
            await asyncio.sleep(0.2)

        if not browser_open:
            return False, "asset browser did not open"

        upload_input = self.page.locator(
            "[data-radix-popper-content-wrapper] input[type='file'], "
            "[role='dialog'] input[type='file'], [role='menu'] input[type='file'], "
            "input[type='file']"
        ).last
        try:
            await upload_input.set_input_files(ref_path)
        except Exception as exc:
            return False, f"asset upload input failed ({exc})"

        try:
            await self.page.wait_for_selector(
                "//i[contains(text(), 'progress_activity')]",
                state="hidden",
                timeout=45000,
            )
        except Exception:
            await asyncio.sleep(1.2)

        # Click deterministic row (by filename match when possible).
        row_selected = None
        for _ in range(4):
            row_selected = await self._select_asset_row(ref_basename)
            if row_selected.get("ok"):
                break
            await asyncio.sleep(0.6)

        if not row_selected or not row_selected.get("ok"):
            return False, f"asset row selection failed ({row_selected.get('reason', 'unknown')})"

        chip_attached = False
        for _ in range(32):
            chips_now = await self._count_compose_ref_chips()
            ingredients_now = await self._count_store_ingredients()
            if chips_now > chips_before or ingredients_now > ingredients_before:
                chip_attached = True
                break
            await asyncio.sleep(0.25)

        try:
            await self.page.keyboard.press("Escape")
        except Exception:
            pass

        if not chip_attached:
            return False, "reference chip not attached after row click"

        log_callback(
            f"[{self.account_name}] Reference selected via asset browser: {row_selected.get('rowText', ref_basename)}"
        )
        return True, None

    async def _count_store_ingredients(self):
        try:
            count = await self.page.evaluate(
                """
                () => {
                    const editor = document.querySelector("[data-slate-editor='true']");
                    if (!editor) return 0;

                    const reactKeys = Object.keys(editor).filter((k) => k.startsWith("__react"));
                    for (const key of reactKeys) {
                        let node = editor[key];
                        for (let depth = 0; node && depth < 40; depth += 1, node = node.return) {
                            const store = node?.memoizedProps?.promptBoxStore;
                            if (store && typeof store.getState === "function") {
                                const state = store.getState() || {};
                                const list = Array.isArray(state.ingredients) ? state.ingredients : [];
                                return list.length;
                            }
                        }
                    }
                    return 0;
                }
                """
            )
            return max(0, int(count or 0))
        except Exception:
            return 0

    async def _capture_media_sources(self):
        try:
            sources = await self.page.evaluate(
                """
                () => {
                    const normalize = (src) => {
                        const raw = String(src || "").trim();
                        if (!raw) return "";
                        if (raw.startsWith("data:")) return raw.slice(0, 128);
                        return raw.split("#")[0].split("?")[0];
                    };

                    const nodes = document.querySelectorAll(
                        "div[data-index][data-item-index] img[src], div[data-index][data-item-index] video[src]"
                    );
                    return Array.from(nodes)
                        .map((el) => normalize(el.currentSrc || el.src || ""))
                        .filter(Boolean);
                }
                """
            )
            return set(sources or [])
        except Exception:
            return set()

    async def _select_generation_candidate(self, known_media_sources):
        known_sources = list(known_media_sources or [])
        try:
            return await self.page.evaluate(
                """
                (knownSources) => {
                    const normalize = (src) => {
                        const raw = String(src || "").trim();
                        if (!raw) return "";
                        if (raw.startsWith("data:")) return raw.slice(0, 128);
                        return raw.split("#")[0].split("?")[0];
                    };
                    const isVisible = (el) => {
                        if (!el || !el.isConnected) return false;
                        const style = window.getComputedStyle(el);
                        if (!style || style.display === "none" || style.visibility === "hidden" || style.opacity === "0") {
                            return false;
                        }
                        const r = el.getBoundingClientRect();
                        return r.width > 8 && r.height > 8;
                    };

                    const known = new Set((knownSources || []).map(normalize));
                    const cards = Array.from(document.querySelectorAll("div[data-index][data-item-index]"));

                    for (let i = cards.length - 1; i >= 0; i--) {
                        const card = cards[i];
                        const mediaNodes = card.querySelectorAll("img[src], video[src]");
                        if (!mediaNodes.length) continue;

                        const media = mediaNodes[mediaNodes.length - 1];
                        if (!isVisible(media)) continue;

                        const src = media.currentSrc || media.src || "";
                        const normSrc = normalize(src);
                        if (!normSrc || known.has(normSrc)) continue;

                        const r = media.getBoundingClientRect();
                        if (r.width < 120 || r.height < 120) continue;

                        const text = String(card.textContent || "").toLowerCase();
                        const looksProcessing =
                            text.includes("generating") ||
                            text.includes("queued") ||
                            text.includes("processing") ||
                            text.includes("progress");

                        return {
                            index: i,
                            src,
                            normSrc,
                            tag: String(media.tagName || "").toLowerCase(),
                            looksProcessing,
                        };
                    }
                    return null;
                }
                """,
                known_sources,
            )
        except Exception:
            return None

    async def _extract_latest_media_data(self, known_media_sources=None):
        known_sources = list(known_media_sources or [])
        return await self.page.evaluate(
            """
            async (knownSources) => {
                const normalize = (src) => {
                    const raw = String(src || "").trim();
                    if (!raw) return "";
                    if (raw.startsWith("data:")) return raw.slice(0, 128);
                    return raw.split("#")[0].split("?")[0];
                };

                const known = new Set((knownSources || []).map(normalize));
                const nodes = Array.from(
                    document.querySelectorAll(
                        "div[data-index][data-item-index] img[src], div[data-index][data-item-index] video[src]"
                    )
                );

                let media = null;
                for (let i = nodes.length - 1; i >= 0; i--) {
                    const node = nodes[i];
                    const src = node.currentSrc || node.src || "";
                    const norm = normalize(src);
                    if (!norm || known.has(norm)) continue;
                    const r = node.getBoundingClientRect();
                    if (r.width < 120 || r.height < 120) continue;
                    media = node;
                    break;
                }

                if (!media) media = nodes.length ? nodes[nodes.length - 1] : null;
                if (!media) return null;

                const src = media.currentSrc || media.src || "";
                const tag = (media.tagName || "").toLowerCase();
                if (!src) return null;

                if (src.startsWith("data:")) {
                    return { src, tag, dataUrl: src };
                }

                try {
                    const resp = await fetch(src);
                    const blob = await resp.blob();
                    const dataUrl = await new Promise((resolve, reject) => {
                        const reader = new FileReader();
                        reader.onload = () => resolve(reader.result);
                        reader.onerror = () => reject(reader.error);
                        reader.readAsDataURL(blob);
                    });
                    return { src, tag, dataUrl };
                } catch (error) {
                    return { src, tag, dataUrl: null, error: String(error) };
                }
            }
            """,
            known_sources,
        )

    async def _extract_media_data_from_container(self, container_index, known_media_sources=None):
        try:
            idx = int(container_index)
        except Exception:
            return None

        known_sources = list(known_media_sources or [])
        return await self.page.evaluate(
            """
            async (args) => {
                const index = Number(args?.index ?? -1);
                const knownSources = Array.isArray(args?.knownSources) ? args.knownSources : [];
                const cards = Array.from(document.querySelectorAll("div[data-index][data-item-index]"));
                if (!cards.length) return null;
                if (index < 0 || index >= cards.length) return null;

                const card = cards[index];
                const mediaNodes = Array.from(card.querySelectorAll("img[src], video[src]"));
                if (!mediaNodes.length) return null;

                const normalize = (src) => {
                    const raw = String(src || "").trim();
                    if (!raw) return "";
                    if (raw.startsWith("data:")) return raw.slice(0, 128);
                    return raw.split("#")[0].split("?")[0];
                };
                const known = new Set((knownSources || []).map(normalize));
                const pickBest = (nodes) => {
                    if (!nodes.length) return null;
                    return nodes
                        .map((node) => {
                            const src = node.currentSrc || node.src || "";
                            const rect = node.getBoundingClientRect();
                            return {
                                node,
                                src,
                                norm: normalize(src),
                                area: Math.max(0, rect.width || 0) * Math.max(0, rect.height || 0),
                            };
                        })
                        .filter((x) => !!x.src && x.area >= 120 * 120)
                        .sort((a, b) => b.area - a.area)[0] || null;
                };

                const freshNodes = mediaNodes.filter((node) => {
                    const src = node.currentSrc || node.src || "";
                    const norm = normalize(src);
                    return !!norm && !known.has(norm);
                });

                let picked = pickBest(freshNodes);
                if (!picked) picked = pickBest(mediaNodes);
                if (!picked) return null;
                const media = picked.node;
                const src = media.currentSrc || media.src || "";
                const tag = String(media.tagName || "").toLowerCase();
                if (!src) return null;

                if (src.startsWith("data:")) {
                    return { src, tag, dataUrl: src };
                }

                try {
                    const resp = await fetch(src);
                    const blob = await resp.blob();
                    const dataUrl = await new Promise((resolve, reject) => {
                        const reader = new FileReader();
                        reader.onload = () => resolve(reader.result);
                        reader.onerror = () => reject(reader.error);
                        reader.readAsDataURL(blob);
                    });
                    return { src, tag, dataUrl };
                } catch (error) {
                    return { src, tag, dataUrl: null, error: String(error) };
                }
            }
            """,
            {"index": idx, "knownSources": known_sources},
        )

    def _normalize_queue_no(self, queue_no):
        try:
            value = int(queue_no)
            if value > 0:
                return value
        except Exception:
            pass
        return None

    def _build_output_path(
        self,
        job_id,
        ext,
        suggested_filename=None,
        queue_no=None,
        output_index=1,
        output_count=1,
    ):
        out_dir = get_output_directory()
        os.makedirs(out_dir, exist_ok=True)

        safe_job = (job_id or "job").replace("-", "")[:8]
        normalized_queue_no = self._normalize_queue_no(queue_no)

        if suggested_filename:
            base_name, suggested_ext = os.path.splitext(suggested_filename)
            if suggested_ext:
                ext = suggested_ext
            base_name = base_name.strip().replace(" ", "_")[:32] or "generation"
        else:
            base_name = "generation"

        if normalized_queue_no is not None:
            try:
                idx = max(1, int(output_index or 1))
            except Exception:
                idx = 1
            try:
                count = max(1, int(output_count or 1))
            except Exception:
                count = 1

            if count > 1:
                filename = f"{normalized_queue_no}_{idx}{ext}"
            else:
                filename = f"{normalized_queue_no}{ext}"
        else:
            ts = int(time.time() * 1000)
            nonce = random.randint(1000, 9999)
            filename = f"{safe_job}_{ts}_{nonce}_{base_name}{ext}"
        return os.path.join(out_dir, filename)

    def _save_data_url(self, data_url, media_tag, job_id, queue_no=None, output_index=1, output_count=1):
        if not data_url or not data_url.startswith("data:"):
            return None

        header, encoded = data_url.split(",", 1)
        mime = "application/octet-stream"
        if ";base64" in header:
            mime = header.split(":", 1)[1].split(";", 1)[0]

        ext_map = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/webp": ".webp",
            "video/mp4": ".mp4",
            "video/webm": ".webm",
            "video/quicktime": ".mov",
        }
        ext = ext_map.get(mime)
        if not ext:
            ext = ".mp4" if media_tag == "video" else ".png"

        output_path = self._build_output_path(
            job_id,
            ext,
            queue_no=queue_no,
            output_index=output_index,
            output_count=output_count,
        )

        with open(output_path, "wb") as f:
            f.write(base64.b64decode(encoded))

        if ext in (".png", ".jpg", ".jpeg", ".webp") and str(
                get_setting("flow_remove_watermark", "1") or "1").strip().lower() in ("1", "true", "on", "yes"):
            try:
                remove_image_watermark(output_path)
            except Exception:
                pass
        return output_path

    def _extract_project_id(self, url):
        raw = str(url or "")
        marker = "/project/"
        idx = raw.find(marker)
        if idx < 0:
            return None
        tail = raw[idx + len(marker) :]
        project_id = tail.split("?", 1)[0].split("#", 1)[0].split("/", 1)[0].strip()
        return project_id or None

    async def _extract_project_id_from_dom_hints(self):
        try:
            hint = await self.page.evaluate(
                """
                () => {
                    const regex = /\\/project\\/([a-z0-9-]{16,})/i;
                    const candidates = [String(window.location.href || "")];

                    const linkNodes = document.querySelectorAll("a[href*='/project/']");
                    for (const node of linkNodes) {
                        candidates.push(String(node.href || node.getAttribute("href") || ""));
                    }

                    const scriptNodes = document.querySelectorAll("script");
                    for (const script of scriptNodes) {
                        const t = String(script.textContent || "");
                        if (t.includes("/project/")) {
                            candidates.push(t.slice(0, 6000));
                        }
                    }

                    for (const item of candidates) {
                        const match = item.match(regex);
                        if (match && match[1]) return match[1];
                    }
                    return "";
                }
                """
            )
            val = str(hint or "").strip()
            return val or None
        except Exception:
            return None

    async def _click_new_project_for_api(self):
        try:
            # Primary selector path.
            new_project_btn = self.page.locator(
                "//*[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'new project')]"
            ).first
            if await self._wait_for_visible(new_project_btn, timeout=3000):
                await new_project_btn.click(force=True)
                return True, "new_project_text"
        except Exception:
            pass

        try:
            # Broader heuristic for plus/create actions in top bar.
            result = await self.page.evaluate(
                """
                () => {
                    const isVisible = (el) => {
                        if (!el || !el.isConnected) return false;
                        const s = window.getComputedStyle(el);
                        if (!s || s.display === "none" || s.visibility === "hidden" || s.opacity === "0") return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 10 && r.height > 10;
                    };
                    const btns = Array.from(document.querySelectorAll("button, [role='button']"));
                    let best = null;
                    let bestScore = -1e9;
                    for (const b of btns) {
                        if (!isVisible(b)) continue;
                        if (b.closest("#af-bot-panel")) continue;
                        const txt = String(b.textContent || "").toLowerCase().replace(/\\s+/g, " ").trim();
                        const aria = String(b.getAttribute("aria-label") || "").toLowerCase();
                        const title = String(b.getAttribute("title") || "").toLowerCase();
                        const icon = String(
                            b.querySelector("i, mat-icon, .material-symbols, .material-symbols-outlined")?.textContent || ""
                        ).toLowerCase().trim();
                        const looksCreate =
                            txt.includes("new project") ||
                            txt.includes("new") && txt.includes("project") ||
                            txt.includes("create") ||
                            aria.includes("new project") ||
                            aria.includes("create project") ||
                            title.includes("new project") ||
                            txt === "+" ||
                            icon === "add" ||
                            icon === "add_2";
                        if (!looksCreate) continue;

                        const r = b.getBoundingClientRect();
                        let score = 0;
                        // Prefer top-right actions (Flow header behavior).
                        score += (r.right || 0) * 0.1;
                        score -= (r.top || 0) * 0.08;
                        if (txt.includes("new project") || aria.includes("new project")) score += 500;
                        if (txt === "+" || icon === "add" || icon === "add_2") score += 100;
                        if (score > bestScore) {
                            bestScore = score;
                            best = b;
                        }
                    }
                    if (!best) return { ok: false };
                    try {
                        best.click();
                    } catch {
                        best.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                    }
                    return { ok: true };
                }
                """
            )
            if result and result.get("ok"):
                return True, "new_project_heuristic"
        except Exception:
            pass

        return False, "new_project_not_found"

    async def _read_flow_page_diagnostic(self):
        url = str(self.page.url or "")
        title = ""
        snippet = ""
        try:
            title = await self.page.title()
        except Exception:
            title = ""
        try:
            snippet = await self.page.evaluate(
                "() => (document && document.body && document.body.innerText) ? document.body.innerText.slice(0, 260) : ''"
            )
        except Exception:
            snippet = ""
        return {
            "url": url,
            "title": str(title or ""),
            "snippet": str(snippet or "").replace("\\n", " ")[:260],
        }

    async def setup_project_via_ui(self, log_callback):
        """
        Warm a Flow project via UI before the first queued job on this slot.
        Returns True when a usable project id is resolved and cached.
        """
        try:
            log_callback(f"[{self.account_name}] Setting up Flow project via UI...")

            shared_project_id = str(
                self._shared_flow_project_id_by_account.get(self._account_root_key()) or ""
            ).strip()
            if shared_project_id:
                try:
                    await self._goto_flow_page(
                        target_url=f"{self.FLOW_PAGE_URL}/project/{shared_project_id}",
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                    await asyncio.sleep(1.5)
                    current_id = self._extract_project_id(self.page.url or "")
                    if current_id:
                        cached = self._cache_project_id(current_id)
                        log_callback(f"[{self.account_name}] Project ready from shared cache: {cached}")
                        return True
                except Exception:
                    pass

            await self._goto_flow_page(wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

            current_id = self._extract_project_id(self.page.url or "")
            if current_id:
                cached = self._cache_project_id(current_id)
                log_callback(f"[{self.account_name}] Project already loaded: {cached}")
                return True

            hinted_id = await self._extract_project_id_from_dom_hints()
            if hinted_id:
                cached = self._cache_project_id(hinted_id)
                log_callback(f"[{self.account_name}] Project ready from page hints: {cached}")
                return True

            shared_project_id = str(
                self._shared_flow_project_id_by_account.get(self._account_root_key()) or ""
            ).strip()
            if shared_project_id:
                try:
                    await self._goto_flow_page(
                        target_url=f"{self.FLOW_PAGE_URL}/project/{shared_project_id}",
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                    await asyncio.sleep(1.5)
                    current_id = self._extract_project_id(self.page.url or "")
                    if current_id:
                        cached = self._cache_project_id(current_id)
                        log_callback(f"[{self.account_name}] Project ready from shared cache: {cached}")
                        return True
                except Exception:
                    pass

            clicked, click_mode = await self._click_new_project_for_api()
            if clicked:
                log_callback(f"[{self.account_name}] Clicked 'New project' for setup ({click_mode}).")
            else:
                for selector in [
                    'button:has-text("New project")',
                    'button:has-text("New")',
                    '[aria-label="New project"]',
                    'button:has-text("+")',
                ]:
                    try:
                        btn = self.page.locator(selector).first
                        if await self._wait_for_visible(btn, timeout=2000):
                            await btn.click()
                            clicked = True
                            log_callback(f"[{self.account_name}] Clicked fallback project setup control ({selector}).")
                            break
                    except Exception:
                        continue

            for _ in range(30):
                await asyncio.sleep(0.5)
                current_id = self._extract_project_id(self.page.url or "")
                if current_id:
                    cached = self._cache_project_id(current_id)
                    log_callback(f"[{self.account_name}] Project ready: {cached}")
                    return True

                hinted_id = await self._extract_project_id_from_dom_hints()
                if hinted_id:
                    cached = self._cache_project_id(hinted_id)
                    log_callback(f"[{self.account_name}] Project ready from page hints: {cached}")
                    return True

                shared_project_id = str(
                    self._shared_flow_project_id_by_account.get(self._account_root_key()) or ""
                ).strip()
                if shared_project_id:
                    try:
                        await self._goto_flow_page(
                            target_url=f"{self.FLOW_PAGE_URL}/project/{shared_project_id}",
                            wait_until="domcontentloaded",
                            timeout=30000,
                        )
                        await asyncio.sleep(1.0)
                        current_id = self._extract_project_id(self.page.url or "")
                        if current_id:
                            cached = self._cache_project_id(current_id)
                            log_callback(f"[{self.account_name}] Project ready from shared cache: {cached}")
                            return True
                    except Exception:
                        pass

            page_url = self.page.url or ""
            log_callback(
                f"[{self.account_name}] Project ID not ready after UI setup. "
                f"Current URL: {page_url[:100]}. First job may fail and retry until project context is ready."
            )
            return False
        except Exception as exc:
            log_callback(f"[{self.account_name}] Project setup error: {exc}. First job may fail and retry.")
            return False

    def _resolve_image_model_name(self, model):
        lower = str(model or "").lower()
        # Flow image generation currently routes these image models through the
        # same Flow endpoint, using recovered model keys from HAR captures.
        # HARBOR_SEAL = Nano Banana 2 Lite (added 2026-07-01 from HAR capture).
        # "lite" check must come before generic "nano banana 2" match below.
        if "lite" in lower and "nano banana" in lower:
            return "HARBOR_SEAL"
        if "nano banana pro" in lower:
            return "GEM_PIX_2"
        if "nano banana 2" in lower:
            return "NARWHAL"
        if "imagen" in lower:
            return "NARWHAL"
        return None

    def _resolve_image_aspect_ratio(self, ratio):
        raw = str(ratio or "").strip()
        if raw.startswith("IMAGE_ASPECT_RATIO_"):
            return raw

        lower = raw.lower()
        if "4:3" in lower:
            return "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE"
        if "3:4" in lower:
            return "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR"
        if "portrait" in lower or "9:16" in lower:
            return "IMAGE_ASPECT_RATIO_PORTRAIT"
        if "square" in lower or "1:1" in lower:
            return "IMAGE_ASPECT_RATIO_SQUARE"
        return "IMAGE_ASPECT_RATIO_LANDSCAPE"

    def _normalize_video_sub_mode(self, video_sub_mode="", ref_path=None, start_image_path=None, end_image_path=None):
        raw = str(video_sub_mode or "").strip().lower()
        valid_modes = {"text_to_video", "ingredients", "frames_start", "frames_start_end"}
        if raw in valid_modes:
            return raw
        if end_image_path and start_image_path:
            return "frames_start_end"
        if start_image_path:
            return "frames_start"
        if ref_path:
            return "ingredients"
        return "text_to_video"

    def _resolve_video_model_tier(self, model, video_model=""):
        source = str(video_model or model or "").strip().lower()
        if not source:
            return "fast"
        if "lite" in source:
            return "lite"
        if "lower pri" in source or "lower priority" in source or "relaxed" in source:
            return "lower_pri"
        if "quality" in source:
            return "quality"
        if source in {"veo_3_1_t2v", "veo_3_1_i2v_s", "veo_3_1_i2v_s_fl"}:
            return "quality"
        return "fast"

    def _resolve_video_model_key(
        self,
        model,
        video_model="",
        has_reference=False,
        video_sub_mode="text_to_video",
        ref_path=None,
        start_image_path=None,
        end_image_path=None,
    ):
        if has_reference and not ref_path and not start_image_path and not end_image_path:
            ref_path = "__reference__"
        sub_mode = self._normalize_video_sub_mode(
            video_sub_mode=video_sub_mode,
            ref_path=ref_path,
            start_image_path=start_image_path,
            end_image_path=end_image_path,
        )
        tier = self._resolve_video_model_tier(model, video_model)
        return self.VIDEO_MODEL_KEYS.get((sub_mode, tier))

    def _resolve_video_endpoint(self, video_sub_mode="", ref_path=None, start_image_path=None, end_image_path=None):
        sub_mode = self._normalize_video_sub_mode(
            video_sub_mode=video_sub_mode,
            ref_path=ref_path,
            start_image_path=start_image_path,
            end_image_path=end_image_path,
        )
        return self.VIDEO_ENDPOINTS.get(sub_mode)

    def _resolve_video_reference_model_key(self, aspect_ratio):
        video_aspect = self._resolve_video_aspect_ratio(aspect_ratio)
        model_map = {
            "VIDEO_ASPECT_RATIO_LANDSCAPE": "veo_3_1_r2v_fast_landscape_ultra",
            "VIDEO_ASPECT_RATIO_PORTRAIT": "veo_3_1_r2v_fast_portrait_ultra",
            "VIDEO_ASPECT_RATIO_SQUARE": "veo_3_1_r2v_fast_square_ultra",
        }
        return model_map.get(video_aspect, "veo_3_1_r2v_fast_landscape_ultra")

    def _resolve_video_aspect_ratio(self, ratio):
        raw = str(ratio or "").strip()
        if raw.startswith("VIDEO_ASPECT_RATIO_"):
            return raw
        if raw.startswith("IMAGE_ASPECT_RATIO_"):
            return raw.replace("IMAGE_", "VIDEO_", 1)

        lower = raw.lower()
        if "portrait" in lower or "9:16" in lower:
            return "VIDEO_ASPECT_RATIO_PORTRAIT"
        if "square" in lower or "1:1" in lower:
            return "VIDEO_ASPECT_RATIO_SQUARE"
        return "VIDEO_ASPECT_RATIO_LANDSCAPE"

    def _resolve_video_upscale_config(self, upscale_value):
        raw = str(upscale_value or "none").strip().lower()
        if raw == "1080p":
            return ("VIDEO_RESOLUTION_1080P", "veo_3_1_upsampler_1080p")
        if raw == "4k":
            return ("VIDEO_RESOLUTION_4K", "veo_3_1_upsampler_4k")
        return (None, None)

    def _is_api_eligible_video_job(
        self,
        model,
        video_model="",
        ref_path=None,
        aspect_ratio="",
        video_sub_mode="text_to_video",
        start_image_path=None,
        end_image_path=None,
    ):
        _ = aspect_ratio
        sub_mode = self._normalize_video_sub_mode(
            video_sub_mode=video_sub_mode,
            ref_path=ref_path if ref_path and os.path.exists(ref_path) else None,
            start_image_path=start_image_path if start_image_path and os.path.exists(start_image_path) else None,
            end_image_path=end_image_path if end_image_path and os.path.exists(end_image_path) else None,
        )
        if sub_mode == "ingredients" and not (ref_path and os.path.exists(ref_path)):
            return False
        if sub_mode == "frames_start" and not (start_image_path and os.path.exists(start_image_path)):
            return False
        if sub_mode == "frames_start_end":
            if not (start_image_path and os.path.exists(start_image_path)):
                return False
            if not (end_image_path and os.path.exists(end_image_path)):
                return False
        return (
            self._resolve_video_model_key(
                model,
                video_model,
                video_sub_mode=sub_mode,
                ref_path=ref_path,
                start_image_path=start_image_path,
                end_image_path=end_image_path,
            )
            is not None
        )

    def _get_image_execution_mode(self):
        # Generation is API-only. Queue retries now handle transient failures
        # instead of falling back to UI automation.
        return "api_only"

    def _is_api_eligible_image_job(self, model, ref_path):
        # Reference-image jobs are API eligible; only unsupported/video models stay blocked.
        _ = ref_path
        lower = str(model or "").lower()
        is_flow_image_model = (
            ("nano banana 2" in lower)
            or ("nano banana pro" in lower)
            or ("nano banana" in lower and "lite" in lower)
            or ("imagen" in lower)
        )
        return is_flow_image_model and ("veo" not in lower)

    def _resolve_mime_type(self, file_path):
        guessed, _ = mimetypes.guess_type(file_path)
        return guessed or "image/jpeg"

    def _build_media_redirect_url(self, media_name):
        name = str(media_name or "").strip()
        if not name:
            return ""
        return f"https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?name={name}"

    def _build_video_redirect_url(self, media_name):
        return self._build_media_redirect_url(media_name)

    async def _upload_reference_image_api(self, project_id, file_path, log_callback=None):
        if not file_path or not os.path.exists(file_path):
            raise RuntimeError("reference path missing")

        try:
            with open(file_path, "rb") as f:
                image_bytes_b64 = base64.b64encode(f.read()).decode("utf-8")
        except Exception as exc:
            raise RuntimeError(f"reference read failed: {exc}") from exc

        file_name = os.path.basename(file_path)
        mime_type = self._resolve_mime_type(file_path)

        result = await self.page.evaluate(
            """
            async ({ projectId, fileName, mimeType, imageBytesB64 }) => {
                const getAuthSession = async () => {
                    try {
                        const resp = await fetch("https://labs.google/fx/api/auth/session", {
                            method: "GET",
                            credentials: "include",
                        });
                        if (!resp.ok) return null;
                        const data = await resp.json().catch(() => null);
                        if (!data || !data.access_token) return null;
                        return data;
                    } catch {
                        return null;
                    }
                };

                const authSession = await getAuthSession();
                if (!authSession || !authSession.access_token) {
                        return {
                            ok: false,
                            status: 0,
                            error: "missing auth session access token",
                        };
                }

                const body = {
                    clientContext: {
                        projectId,
                        tool: "PINHOLE",
                        sessionId: ";" + Date.now(),
                    },
                    fileName,
                    mimeType,
                    imageBytes: imageBytesB64,
                    isHidden: false,
                    isUserUploaded: true,
                };

                try {
                    const resp = await fetch("https://aisandbox-pa.googleapis.com/v1/flow/uploadImage", {
                        method: "POST",
                        credentials: "include",
                        headers: {
                            "content-type": "text/plain;charset=UTF-8",
                            "authorization": `Bearer ${authSession.access_token}`,
                        },
                        body: JSON.stringify(body),
                    });

                    const text = await resp.text();
                    let data = null;
                    try {
                        data = JSON.parse(text);
                    } catch {
                        data = null;
                    }

                    if (!resp.ok) {
                        return {
                            ok: false,
                            status: resp.status,
                            error:
                                (data && data.error && (data.error.message || data.error.status)) ||
                                text.slice(0, 500) ||
                                `HTTP ${resp.status}`,
                        };
                    }

                    const mediaName =
                        data?.name ||
                        data?.mediaName ||
                        data?.media?.name ||
                        (Array.isArray(data?.media) && data.media[0] ? data.media[0].name || "" : "") ||
                        "";
                    if (!mediaName) {
                        return {
                            ok: false,
                            status: resp.status,
                            error: "upload response missing media name",
                            raw: text.slice(0, 500),
                        };
                    }

                    return {
                        ok: true,
                        status: resp.status,
                        mediaName,
                    };
                } catch (error) {
                    return {
                        ok: false,
                        status: 0,
                        error: String(error),
                    };
                }
            }
            """,
            {
                "projectId": project_id,
                "fileName": file_name,
                "mimeType": mime_type,
                "imageBytesB64": image_bytes_b64,
            },
        )

        if not result or not result.get("ok"):
            error = result.get("error", "unknown") if result else "evaluate returned null"
            raise RuntimeError(f"Reference upload failed: {error}")
        return str(result.get("mediaName") or "").strip()

    async def _flow_api_upload_reference_image(self, project_id, ref_path):
        try:
            media_name = await self._upload_reference_image_api(project_id, ref_path)
            return {"ok": True, "status": 200, "mediaName": media_name}
        except Exception as exc:
            return {"ok": False, "status": 0, "error": str(exc)}

    async def _get_or_upload_reference_with_status(self, project_id, ref_path, log_callback):
        cache_key = self._build_reference_cache_key(project_id, ref_path)
        cache_lock = self._get_reference_cache_lock(cache_key)
        async with cache_lock:
            cached_media_name = str(self._reference_upload_cache.get(cache_key) or "").strip()
            if cached_media_name:
                log_callback(f"[{self.account_name}] Reference image cached, reusing: {cached_media_name}")
                return cached_media_name, True

            log_callback(f"[{self.account_name}] Uploading reference image: {os.path.basename(ref_path)}")
            media_name = await self._upload_reference_image_api(project_id, ref_path, log_callback)
            if not media_name:
                raise RuntimeError("reference upload returned empty media id")

            self._reference_upload_cache[cache_key] = media_name
            log_callback(f"[{self.account_name}] Reference uploaded and cached: {media_name}")
            return media_name, False

    async def _get_or_upload_reference(self, project_id, ref_path, log_callback):
        media_name, _was_cached = await self._get_or_upload_reference_with_status(
            project_id,
            ref_path,
            log_callback,
        )
        return media_name

    async def _upload_multiple_references(self, project_id, ref_paths, log_callback, include_stats=False):
        media_ids = []
        cache_hits = 0
        uploaded_count = 0
        seen_paths = set()
        for ref_path in list(ref_paths or []):
            normalized_path = os.path.abspath(str(ref_path or ""))
            if not normalized_path or normalized_path in seen_paths:
                continue
            seen_paths.add(normalized_path)
            media_id, was_cached = await self._get_or_upload_reference_with_status(
                project_id=project_id,
                ref_path=normalized_path,
                log_callback=log_callback,
            )
            media_ids.append(media_id)
            if was_cached:
                cache_hits += 1
            else:
                uploaded_count += 1
            log_callback(
                f"[{self.account_name}] Reference ready: {os.path.basename(normalized_path)} -> {media_id[:12]}..."
            )
        if include_stats:
            return media_ids, {"cache_hits": cache_hits, "uploaded_count": uploaded_count}
        return media_ids

    async def _ensure_flow_project_id(self, log_callback):
        self._last_project_resolve_error = ""
        account_key = self._account_root_key()

        cached_project_id = str(self._cached_project_id or "").strip()
        if cached_project_id:
            current_id = self._extract_project_id(self.page.url or "")
            if current_id == cached_project_id:
                return current_id
            try:
                await self._goto_flow_page(
                    target_url=f"{self.FLOW_PAGE_URL}/project/{cached_project_id}",
                    wait_until="domcontentloaded",
                )
                await asyncio.sleep(0.8)
                current_id = self._extract_project_id(self.page.url or "")
                if current_id == cached_project_id:
                    return self._cache_project_id(current_id)
            except Exception:
                pass

        shared_project_id = str(self._shared_flow_project_id_by_account.get(account_key) or "").strip()
        if shared_project_id:
            current_id = self._extract_project_id(self.page.url or "")
            if current_id == shared_project_id:
                return self._cache_project_id(current_id)
            try:
                await self._goto_flow_page(
                    target_url=f"{self.FLOW_PAGE_URL}/project/{shared_project_id}",
                    wait_until="domcontentloaded",
                )
                await asyncio.sleep(0.8)
                current_id = self._extract_project_id(self.page.url or "")
                if current_id == shared_project_id:
                    return self._cache_project_id(current_id)
            except Exception:
                pass

        current_id = self._extract_project_id(self.page.url or "")
        if current_id:
            return self._cache_project_id(current_id)

        hinted_id = await self._extract_project_id_from_dom_hints()
        if hinted_id:
            return self._cache_project_id(hinted_id)

        await self._goto_flow_page(wait_until="domcontentloaded")
        await asyncio.sleep(1)
        current_id = self._extract_project_id(self.page.url or "")
        if current_id:
            return self._cache_project_id(current_id)

        for attempt in range(1, 4):
            clicked, click_mode = await self._click_new_project_for_api()
            if clicked:
                log_callback(f"[{self.account_name}] Clicked 'New project' for API mode ({click_mode}).")
                await asyncio.sleep(1.1)
            else:
                await asyncio.sleep(0.6)

            for _ in range(14):
                current_id = self._extract_project_id(self.page.url or "")
                if current_id:
                    return self._cache_project_id(current_id)
                hinted_id = await self._extract_project_id_from_dom_hints()
                if hinted_id:
                    return self._cache_project_id(hinted_id)
                await asyncio.sleep(0.4)

                if attempt < 3:
                    try:
                        await self._goto_flow_page(wait_until="domcontentloaded")
                        await asyncio.sleep(0.8)
                    except Exception:
                        pass

        diag = await self._read_flow_page_diagnostic()
        low_url = diag["url"].lower()
        low_text = f"{diag['title']} {diag['snippet']}".lower()
        if "accounts.google.com" in low_url or "signin" in low_url or "log in" in low_text or "sign in" in low_text:
            self._last_project_resolve_error = "Google account session not signed in for Flow"
        elif "not available" in low_text or "unsupported" in low_text or "access denied" in low_text:
            self._last_project_resolve_error = "Flow access not available for this account"
        else:
            self._last_project_resolve_error = (
                f"could not create/open project page (url={diag['url']}, title={diag['title']})"
            )

        return None

    async def _api_humanized_scroll_warmup(self, log_callback, min_seconds=4.0, max_seconds=5.0):
        if not self.page or self.page.is_closed():
            return

        await self._warmup_page(self.page, log_callback)
        duration = random.uniform(min_seconds, max_seconds)
        log_callback(
            f"[{self.account_name}] Humanized warmup: browsing canvas for {duration:.1f}s before API submit..."
        )
        try:
            await self.page.bring_to_front()
        except Exception:
            pass

        end_time = time.time() + duration
        direction = 1
        while time.time() < end_time:
            delta = random.randint(90, 260) * direction
            try:
                await self.page.mouse.wheel(0, delta)
            except Exception:
                pass

            if random.random() < 0.25:
                direction *= -1

            await asyncio.sleep(random.uniform(0.22, 0.55))

    async def _api_humanized_pause(self, log_callback, min_seconds, max_seconds, reason):
        wait_for = random.uniform(min_seconds, max_seconds)
        log_callback(f"[{self.account_name}] Humanized wait ({reason}): {wait_for:.1f}s.")
        await asyncio.sleep(wait_for)

    async def _flow_api_generate_single_image(
        self,
        project_id,
        prompt,
        model_name,
        aspect_ratio,
        batch_id,
        seed,
        recaptcha_action,
        reference_media_ids=None,
    ):
        # Main-world reCAPTCHA: get token via <script> tag injection
        # (no CDP traces, like competitor's Chrome Extension approach)
        mainworld_token = None
        try:
            mainworld_token = await get_recaptcha_token_mainworld(
                self.page, str(recaptcha_action)
            )
        except Exception:
            pass

        return await self.page.evaluate(
            """
            async ({ projectId, prompt, modelName, aspectRatio, batchId, seed, referenceMediaIds, recaptchaAction, cachedToken }) => {
                const getAuthSession = async () => {
                    try {
                        const resp = await fetch("https://labs.google/fx/api/auth/session", {
                            method: "GET",
                            credentials: "include",
                        });
                        if (!resp.ok) return null;
                        const data = await resp.json().catch(() => null);
                        if (!data || !data.access_token) return null;
                        return data;
                    } catch {
                        return null;
                    }
                };

                const getRecaptchaContext = async () => {
                    try {
                        const enterprise = window.grecaptcha?.enterprise;
                        if (!enterprise || typeof enterprise.execute !== "function") return null;

                        let siteKey = null;
                        const scripts = Array.from(document.querySelectorAll("script[src*='recaptcha'][src*='render=']"));
                        for (const script of scripts) {
                            try {
                                const u = new URL(script.src);
                                const render = u.searchParams.get("render");
                                if (render && render !== "explicit") {
                                    siteKey = render;
                                    break;
                                }
                            } catch {
                                // keep searching
                            }
                        }
                        if (!siteKey) return null;

                        if (typeof enterprise.ready === "function") {
                            await new Promise((resolve) => enterprise.ready(resolve));
                        }

                        // Pre-warm reCAPTCHA Enterprise. Anti-detect browsers
                        // (CloakBrowser) score low on a COLD engine, so fire 3
                        // throwaway execute() calls to prime the score before
                        // minting the real token. Real Chrome is lenient enough
                        // to skip this (the extension does), but CloakBrowser
                        // needs the warmup even for image generation.
                        try {
                            for (let _w = 0; _w < 3; _w++) {
                                enterprise.execute(siteKey, { action: recaptchaAction }).catch(() => {});
                                await new Promise((r) => setTimeout(r, 80 + Math.random() * 120));
                            }
                        } catch (e) {}

                        const token = await enterprise.execute(siteKey, { action: recaptchaAction });
                        if (!token) return null;
                        return {
                            token,
                            applicationType: "RECAPTCHA_APPLICATION_TYPE_WEB",
                        };
                    } catch {
                        return null;
                    }
                };

                const authSession = await getAuthSession();
                if (!authSession || !authSession.access_token) {
                    return {
                        ok: false,
                        status: 0,
                        error: "missing auth session access token",
                    };
                }

                // Use main-world token if available, otherwise fall back to inline
                const recaptchaContext = cachedToken
                    ? { token: cachedToken, applicationType: "RECAPTCHA_APPLICATION_TYPE_WEB" }
                    : await getRecaptchaContext();
                const clientContext = {
                    projectId,
                    tool: "PINHOLE",
                    sessionId: ";" + Date.now(),
                };
                if (recaptchaContext) {
                    clientContext.recaptchaContext = recaptchaContext;
                }

                const body = {
                    clientContext,
                    mediaGenerationContext: { batchId },
                    useNewMedia: true,
                    requests: [
                        {
                            clientContext,
                            imageModelName: modelName,
                            imageAspectRatio: aspectRatio,
                            structuredPrompt: {
                                parts: [{ text: String(prompt || "").endsWith("\\n") ? String(prompt) : `${String(prompt || "")}\\n` }],
                            },
                            seed,
                            imageInputs: Array.isArray(referenceMediaIds) && referenceMediaIds.length > 0
                                ? referenceMediaIds.map((id) => ({
                                    imageInputType: "IMAGE_INPUT_TYPE_REFERENCE",
                                    name: id,
                                }))
                                : [],
                        },
                    ],
                };

                try {
                    const resp = await fetch(
                        `https://aisandbox-pa.googleapis.com/v1/projects/${projectId}/flowMedia:batchGenerateImages`,
                        {
                            method: "POST",
                            credentials: "include",
                            headers: {
                                "content-type": "text/plain;charset=UTF-8",
                                "authorization": `Bearer ${authSession.access_token}`,
                            },
                            body: JSON.stringify(body),
                        }
                    );

                    const text = await resp.text();
                    let data = null;
                    try {
                        data = JSON.parse(text);
                    } catch {
                        data = null;
                    }

                    if (!resp.ok) {
                        return {
                            ok: false,
                            status: resp.status,
                            error:
                                (data && data.error && (data.error.message || data.error.status)) ||
                                text.slice(0, 500) ||
                                `HTTP ${resp.status}`,
                        };
                    }

                    const mediaList = Array.isArray(data?.media) ? data.media : [];
                    const firstMedia = mediaList.length ? mediaList[0] : null;
                    const selectedMedia =
                        mediaList.find((item) => {
                            const itemName = item?.name || "";
                            const itemUrl = item?.image?.generatedImage?.fifeUrl || "";
                            if (Array.isArray(referenceMediaIds) && referenceMediaIds.includes(itemName)) {
                                return false;
                            }
                            return Boolean(itemUrl || itemName);
                        }) || firstMedia;
                    const workflowList = Array.isArray(data?.workflows) ? data.workflows : [];
                    const firstWorkflow = workflowList.length ? workflowList[0] : null;
                    const fifeUrl = selectedMedia?.image?.generatedImage?.fifeUrl || "";
                    const primaryMediaId = firstWorkflow?.metadata?.primaryMediaId || "";
                    const mediaName =
                        (Array.isArray(referenceMediaIds) && referenceMediaIds.includes(primaryMediaId) ? "" : primaryMediaId) ||
                        selectedMedia?.name ||
                        firstMedia?.name ||
                        "";

                    return {
                        ok: true,
                        status: resp.status,
                        fifeUrl,
                        mediaName,
                        workflowId:
                            firstMedia?.workflowId || (firstWorkflow ? firstWorkflow.name || "" : ""),
                    };
                } catch (error) {
                    return {
                        ok: false,
                        status: 0,
                        error: String(error),
                    };
                }
            }
            """,
            {
                "projectId": project_id,
                "prompt": prompt,
                "modelName": model_name,
                "aspectRatio": aspect_ratio,
                "batchId": batch_id,
                "seed": int(seed),
                "referenceMediaIds": list(reference_media_ids or []),
                "recaptchaAction": str(recaptcha_action),
                "cachedToken": mainworld_token,
            },
        )

    async def _download_binary_to_output(
        self,
        url,
        job_id,
        name_hint="api_image",
        queue_no=None,
        output_index=1,
        output_count=1,
    ):
        if not url:
            raise RuntimeError("empty download URL")

        request_ctx = self.context.request if self.context else None
        if request_ctx is None:
            raise RuntimeError("request context unavailable")

        resp = await request_ctx.get(url, timeout=90000)
        if not resp.ok:
            raise RuntimeError(f"download HTTP {resp.status}")

        content_type = str(resp.headers.get("content-type", "")).lower()
        ext_map = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/webp": ".webp",
            "video/mp4": ".mp4",
            "video/webm": ".webm",
            "video/quicktime": ".mov",
        }
        ext = ".jpg"
        for mime, candidate_ext in ext_map.items():
            if mime in content_type:
                ext = candidate_ext
                break

        output_path = self._build_output_path(
            job_id,
            ext,
            suggested_filename=f"{name_hint}{ext}",
            queue_no=queue_no,
            output_index=output_index,
            output_count=output_count,
        )
        data = await resp.body()
        with open(output_path, "wb") as f:
            f.write(data)
        if ext in (".png", ".jpg", ".jpeg", ".webp") and str(
                get_setting("flow_remove_watermark", "1") or "1").strip().lower() in ("1", "true", "on", "yes"):
            try:
                await asyncio.to_thread(remove_image_watermark, output_path)
            except Exception:
                pass
        return output_path

    async def _download_video_to_output(
        self,
        media_id,
        job_id,
        log_callback,
        queue_no=None,
        output_index=1,
        output_count=1,
    ):
        media_id = str(media_id or "").strip()
        if not media_id:
            raise RuntimeError("empty video media id")
        if self.page is None:
            raise RuntimeError("page unavailable for video download")

        output_path = self._build_output_path(
            job_id,
            ".mp4",
            suggested_filename=f"video_out{int(output_index or 1)}.mp4",
            queue_no=queue_no,
            output_index=output_index,
            output_count=output_count,
        )

        try:
            auth_data = await self.page.evaluate(
                """
                async () => {
                    try {
                        const resp = await fetch("https://labs.google/fx/api/auth/session", {
                            method: "GET",
                            credentials: "include",
                        });
                        if (!resp.ok) return null;
                        return await resp.json().catch(() => null);
                    } catch {
                        return null;
                    }
                }
                """
            )
            access_token = str((auth_data or {}).get("access_token") or "").strip()
            request_ctx = self.context.request if self.context else None
            if access_token and request_ctx is not None:
                candidate_urls = []
                for url in (
                    self._build_video_redirect_url(media_id),
                    self._build_media_redirect_url(media_id),
                ):
                    if url and url not in candidate_urls:
                        candidate_urls.append(url)
                for url in candidate_urls:
                    if not url:
                        continue
                    try:
                        resp = await request_ctx.get(
                            url,
                            headers={"authorization": f"Bearer {access_token}"},
                            timeout=90000,
                        )
                        if not resp.ok:
                            continue
                        data = await resp.body()
                        if not data or len(data) < 1000:
                            continue
                        # Bug #10: Write to .tmp first, rename on complete
                        tmp_path = output_path + ".tmp"
                        with open(tmp_path, "wb") as f:
                            f.write(data)
                        os.replace(tmp_path, output_path)
                        log_callback(f"[{self.account_name}] Video saved via auth redirect: {output_path}")
                        return output_path
                    except Exception:
                        continue
        except Exception as auth_err:
            log_callback(f"[{self.account_name}] Auth-backed video download failed: {auth_err}")

        browser_fetch_result = await self.page.evaluate(
            """
            async ({ mediaId }) => {
                const urls = [
                    `https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?name=${encodeURIComponent(mediaId)}`,
                ];

                const blobToBase64 = async (blob) => {
                    const reader = new FileReader();
                    return await new Promise((resolve, reject) => {
                        reader.onload = () => {
                            const result = String(reader.result || "");
                            const commaIdx = result.indexOf(",");
                            resolve(commaIdx >= 0 ? result.slice(commaIdx + 1) : "");
                        };
                        reader.onerror = () => reject(new Error("FileReader failed"));
                        reader.readAsDataURL(blob);
                    });
                };

                const attempts = [];
                for (const url of urls) {
                    try {
                        const resp = await fetch(url, {
                            method: "GET",
                            credentials: "include",
                            redirect: "follow",
                            cache: "no-store",
                        });
                        const finalUrl = resp.url || url;
                        const contentType = String(resp.headers.get("content-type") || "");
                        if (!resp.ok) {
                            attempts.push(`${url} -> HTTP ${resp.status}`);
                            continue;
                        }

                        const blob = await resp.blob();
                        if (!blob || !blob.size || blob.size < 1000) {
                            attempts.push(`${url} -> empty body`);
                            continue;
                        }

                        const loweredType = contentType.toLowerCase();
                        const looksDownloadable =
                            loweredType.includes("video/") ||
                            loweredType.includes("application/octet-stream") ||
                            loweredType.includes("binary/") ||
                            finalUrl.includes("googleusercontent.com") ||
                            finalUrl.includes("googleapis.com") ||
                            finalUrl.includes("videofx");
                        if (!looksDownloadable && loweredType.includes("text/html")) {
                            attempts.push(`${url} -> HTML response`);
                            continue;
                        }

                        const base64 = await blobToBase64(blob);
                        if (!base64) {
                            attempts.push(`${url} -> base64 encode failed`);
                            continue;
                        }

                        return {
                            ok: true,
                            base64,
                            contentType,
                            url,
                            finalUrl,
                            size: blob.size,
                        };
                    } catch (err) {
                        attempts.push(`${url} -> ${String(err)}`);
                    }
                }

                return {
                    ok: false,
                    error: attempts.join(" | ") || "all browser fetch attempts failed",
                };
            }
            """,
            {"mediaId": media_id},
        )

        if browser_fetch_result and browser_fetch_result.get("ok"):
            try:
                video_bytes = base64.b64decode(browser_fetch_result["base64"])
                with open(output_path, "wb") as f:
                    f.write(video_bytes)
                size_mb = len(video_bytes) / (1024 * 1024)
                log_callback(
                    f"[{self.account_name}] Video downloaded ({size_mb:.1f} MB) via browser fetch: {output_path}"
                )
                return output_path
            except Exception as decode_err:
                log_callback(f"[{self.account_name}] Browser fetch decode failed: {decode_err}")
        else:
            browser_error = ""
            if browser_fetch_result:
                browser_error = str(browser_fetch_result.get("error") or "").strip()
            if browser_error:
                log_callback(f"[{self.account_name}] Browser fetch download failed: {browser_error}")

        log_callback(f"[{self.account_name}] Trying browser download fallback for video media {media_id[:16]}...")
        try:
            download_url = self._build_video_redirect_url(media_id)
            async with self.page.expect_download(timeout=60000) as download_info:
                await self.page.evaluate(
                    """
                    (url) => {
                        const anchor = document.createElement("a");
                        anchor.href = url;
                        anchor.download = "video.mp4";
                        anchor.rel = "noopener";
                        document.body.appendChild(anchor);
                        anchor.click();
                        anchor.remove();
                    }
                    """,
                    download_url,
                )
            download = await download_info.value
            await download.save_as(output_path)
            log_callback(f"[{self.account_name}] Video saved via browser download: {output_path}")
            return output_path
        except Exception as browser_dl_err:
            log_callback(f"[{self.account_name}] Browser download fallback failed: {browser_dl_err}")

        raise RuntimeError(f"all video download methods failed for media {media_id}")

    async def _video_api_generate_single(
        self,
        project_id,
        prompt,
        aspect_ratio,
        video_model_key,
        endpoint=None,
        video_sub_mode="text_to_video",
        reference_media_id="",
        start_image_media_id="",
        end_image_media_id="",
    ):
        batch_id = str(uuid.uuid4())
        seed = random.randint(1, 2_147_483_647)
        video_aspect = self._resolve_video_aspect_ratio(aspect_ratio)
        endpoint = endpoint or self._resolve_video_endpoint(video_sub_mode=video_sub_mode)

        # Main-world reCAPTCHA token for video
        mainworld_token = None
        try:
            mainworld_token = await get_recaptcha_token_mainworld(
                self.page, "VIDEO_GENERATION"
            )
        except Exception:
            pass

        result = await self.page.evaluate(
            """
            async ({
                projectId,
                prompt,
                batchId,
                seed,
                videoAspect,
                videoModelKey,
                endpoint,
                subMode,
                referenceMediaId,
                startImageMediaId,
                endImageMediaId,
                recaptchaAction,
                cachedToken,
            }) => {
                try {
                    const authResp = await fetch("https://labs.google/fx/api/auth/session", {
                        method: "GET",
                        credentials: "include",
                    });
                    const authData = await authResp.json().catch(() => null);
                    if (!authData || !authData.access_token) {
                        return { ok: false, error: "No auth token" };
                    }

                    // Use main-world token if available, otherwise fall back to inline
                    let recaptchaContext = null;
                    if (cachedToken) {
                        recaptchaContext = {
                            token: cachedToken,
                            applicationType: "RECAPTCHA_APPLICATION_TYPE_WEB",
                        };
                    } else {
                        const enterprise = window.grecaptcha?.enterprise;
                        if (enterprise && typeof enterprise.execute === "function") {
                            const scripts = Array.from(document.querySelectorAll("script[src*='recaptcha'][src*='render=']"));
                            let siteKey = null;
                            for (const script of scripts) {
                                try {
                                    const u = new URL(script.src);
                                    const render = u.searchParams.get("render");
                                    if (render && render !== "explicit") {
                                        siteKey = render;
                                        break;
                                    }
                                } catch {
                                    // keep searching
                                }
                            }
                            if (siteKey) {
                                if (typeof enterprise.ready === "function") {
                                    await new Promise((resolve) => enterprise.ready(resolve));
                                }
                                // Pre-warm reCAPTCHA (CloakBrowser needs priming
                                // on a cold engine — 3 throwaway execute() calls).
                                try {
                                    for (let _w = 0; _w < 3; _w++) {
                                        enterprise.execute(siteKey, { action: recaptchaAction }).catch(() => {});
                                        await new Promise((r) => setTimeout(r, 80 + Math.random() * 120));
                                    }
                                } catch (e) {}
                                const token = await enterprise.execute(siteKey, { action: recaptchaAction });
                                if (token) {
                                    recaptchaContext = {
                                        token,
                                        applicationType: "RECAPTCHA_APPLICATION_TYPE_WEB",
                                    };
                                }
                            }
                        }
                    }

                    const clientContext = {
                        projectId,
                        tool: "PINHOLE",
                        userPaygateTier: "PAYGATE_TIER_TWO",
                        sessionId: ";" + Date.now(),
                    };
                    if (recaptchaContext) {
                        clientContext.recaptchaContext = recaptchaContext;
                    }

                    const request = {
                        aspectRatio: videoAspect,
                        seed,
                        textInput: {
                            structuredPrompt: {
                                parts: [{ text: String(prompt || "") }],
                            },
                        },
                        videoModelKey,
                        metadata: {},
                    };
                    if (subMode === "ingredients" && referenceMediaId) {
                        request.referenceImages = [
                            {
                                mediaId: referenceMediaId,
                                imageUsageType: "IMAGE_USAGE_TYPE_ASSET",
                            },
                        ];
                    }
                    if ((subMode === "frames_start" || subMode === "frames_start_end") && startImageMediaId) {
                        request.startImage = {
                            mediaId: startImageMediaId,
                            cropCoordinates: { top: 0, left: 0, bottom: 1, right: 1 },
                        };
                    }
                    if (subMode === "frames_start_end" && endImageMediaId) {
                        request.endImage = {
                            mediaId: endImageMediaId,
                            cropCoordinates: { top: 0, left: 0, bottom: 1, right: 1 },
                        };
                    }

                    const body = {
                        mediaGenerationContext: { batchId },
                        clientContext,
                        requests: [request],
                        useV2ModelConfig: true,
                    };

                    // HAR-validated: cookie-only auth matches real Chrome.
                    const resp = await fetch(endpoint, {
                        method: "POST",
                        credentials: "include",
                        headers: {
                            "content-type": "text/plain;charset=UTF-8",
                        },
                        body: JSON.stringify(body),
                    });

                    const text = await resp.text();
                    let data = null;
                    try {
                        data = JSON.parse(text);
                    } catch {
                        data = null;
                    }

                    if (!resp.ok) {
                        return {
                            ok: false,
                            error: (data && data.error && (data.error.message || data.error.status)) || text.slice(0, 300),
                        };
                    }

                    const mediaList = Array.isArray(data?.media) ? data.media : [];
                    const firstMedia = mediaList[0] || {};
                    const workflows = Array.isArray(data?.workflows) ? data.workflows : [];
                    const firstWorkflow = workflows[0] || {};

                    return {
                        ok: true,
                        media_id: firstMedia.name || firstWorkflow?.metadata?.primaryMediaId || "",
                        workflow_id: firstWorkflow.name || firstMedia.workflowId || "",
                        operation_name: data?.operations?.[0]?.operation?.name || "",
                        remaining_credits: data?.remainingCredits ?? null,
                    };
                } catch (err) {
                    return { ok: false, error: String(err) };
                }
            }
            """,
            {
                "projectId": project_id,
                "prompt": prompt,
                "batchId": batch_id,
                "seed": int(seed),
                "videoAspect": video_aspect,
                "videoModelKey": video_model_key,
                "endpoint": str(endpoint or ""),
                "subMode": str(video_sub_mode or "text_to_video"),
                "referenceMediaId": str(reference_media_id or ""),
                "startImageMediaId": str(start_image_media_id or ""),
                "endImageMediaId": str(end_image_media_id or ""),
                "recaptchaAction": "VIDEO_GENERATION",
                "cachedToken": mainworld_token,
            },
        )

        if not result or not result.get("ok"):
            error = result.get("error", "unknown") if result else "null result"
            raise RuntimeError(f"Video generation failed: {error}")

        media_id = str(result.get("media_id") or "").strip()
        workflow_id = str(result.get("workflow_id") or "").strip()
        if not media_id:
            raise RuntimeError("Video generation response missing media id")
        if not workflow_id:
            raise RuntimeError("Video generation response missing workflow id")
        return result

    async def _video_api_generate_single_with_reference(
        self,
        project_id,
        prompt,
        aspect_ratio,
        reference_media_id,
        video_model_key,
    ):
        return await self._video_api_generate_single(
            project_id=project_id,
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            video_model_key=video_model_key,
            endpoint=self._resolve_video_endpoint(video_sub_mode="ingredients"),
            video_sub_mode="ingredients",
            reference_media_id=reference_media_id,
        )

    async def _video_poll_until_complete(
        self,
        media_id,
        project_id,
        log_callback,
        job_id=None,
        progress_step="video",
        poll_interval=5,
        max_polls=60,
    ):
        for poll_num in range(1, max_polls + 1):
            await asyncio.sleep(poll_interval)
            self._set_job_progress(job_id, step=progress_step, poll_count=poll_num)
            result = await self.page.evaluate(
                """
                async ({ mediaId, projectId }) => {
                    try {
                        const authResp = await fetch("https://labs.google/fx/api/auth/session", {
                            method: "GET",
                            credentials: "include",
                        });
                        const authData = await authResp.json().catch(() => null);
                        if (!authData || !authData.access_token) {
                            return { ok: false, error: "No auth token" };
                        }

                        const resp = await fetch(
                            "https://aisandbox-pa.googleapis.com/v1/video:batchCheckAsyncVideoGenerationStatus",
                            {
                                method: "POST",
                                credentials: "include",
                                headers: {
                                    "content-type": "text/plain;charset=UTF-8",
                                    "authorization": "Bearer " + authData.access_token,
                                },
                                body: JSON.stringify({
                                    media: [{ name: mediaId, projectId }],
                                }),
                            }
                        );

                        const text = await resp.text();
                        let data = null;
                        try {
                            data = JSON.parse(text);
                        } catch {
                            data = null;
                        }
                        if (!resp.ok) {
                            return {
                                ok: false,
                                error: (data && data.error && (data.error.message || data.error.status)) || text.slice(0, 300),
                            };
                        }

                        const mediaList = Array.isArray(data?.media) ? data.media : [];
                        const mediaItem = mediaList[0] || {};
                        const mediaStatus = mediaItem?.mediaMetadata?.mediaStatus || {};
                        const status = mediaStatus?.mediaGenerationStatus || "UNKNOWN";
                        const failureReason = mediaStatus?.failureReason || "";
                        const moderationResult = mediaStatus?.moderationResult || "";
                        const errorMessage = mediaStatus?.errorMessage || "";
                        const safetyFilterResult = mediaItem?.mediaMetadata?.safetyFilterResult || "";
                        const rawStatus = JSON.stringify(mediaStatus || {}).slice(0, 500);
                        return {
                            ok: true,
                            status,
                            failureReason,
                            moderationResult,
                            errorMessage,
                            safetyFilterResult,
                            rawStatus,
                            remainingCredits: data?.remainingCredits ?? null,
                        };
                    } catch (err) {
                        return { ok: false, error: String(err) };
                    }
                }
                """,
                {"mediaId": media_id, "projectId": project_id},
            )

            if not result or not result.get("ok"):
                err = result.get("error", "unknown") if result else "null result"
                if poll_num % 3 == 0:
                    log_callback(f"[{self.account_name}] Poll {poll_num} error: {err}")
                continue

            status = str(result.get("status") or "UNKNOWN").strip().upper()
            if status == "MEDIA_GENERATION_STATUS_SUCCESSFUL":
                remaining_credits = result.get("remainingCredits")
                if remaining_credits is not None:
                    try:
                        log_callback(f"[CREDITS] Remaining: {int(remaining_credits)}")
                    except Exception:
                        log_callback(f"[CREDITS] Remaining: {remaining_credits}")
                log_callback(f"[{self.account_name}] Generation complete (poll {poll_num}).")
                return remaining_credits
            if status == "MEDIA_GENERATION_STATUS_FAILED":
                reason = str(result.get("failureReason") or "").strip()
                moderation = str(result.get("moderationResult") or "").strip()
                error_message = str(result.get("errorMessage") or "").strip()
                safety_filter = str(result.get("safetyFilterResult") or "").strip()
                raw_status = str(result.get("rawStatus") or "").strip()
                detail = reason or moderation or error_message or safety_filter or raw_status or "server returned FAILED"
                is_moderation = self._is_moderation_failure(detail)
                prefix = "MODERATION: " if is_moderation else ""
                log_callback(
                    f"[{self.account_name}] Video FAILED. "
                    f"{'Content blocked' if is_moderation else 'Server error'}: {detail}"
                )
                raise RuntimeError(f"{prefix}Video generation failed: {detail}")
            if poll_num % 3 == 0:
                log_callback(f"[{self.account_name}] Still generating... (poll {poll_num})")

        raise RuntimeError(f"Video generation timed out after {max_polls * poll_interval}s")

    async def _video_api_upscale(self, project_id, media_id, workflow_id, resolution, aspect_ratio):
        res_enum, model_key = self._resolve_video_upscale_config(resolution)
        if not res_enum or not model_key:
            raise RuntimeError(f"Unsupported video upscale target: {resolution}")

        batch_id = str(uuid.uuid4())
        seed = random.randint(1, 2_147_483_647)
        video_aspect = self._resolve_video_aspect_ratio(aspect_ratio)

        result = await self.page.evaluate(
            """
            async ({ projectId, mediaId, workflowId, batchId, seed, resEnum, modelKey, videoAspect, recaptchaAction }) => {
                try {
                    const authResp = await fetch("https://labs.google/fx/api/auth/session", {
                        method: "GET",
                        credentials: "include",
                    });
                    const authData = await authResp.json().catch(() => null);
                    if (!authData || !authData.access_token) {
                        return { ok: false, error: "No auth token" };
                    }

                    let recaptchaContext = null;
                    const enterprise = window.grecaptcha?.enterprise;
                    if (enterprise && typeof enterprise.execute === "function") {
                        const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
                        const scripts = Array.from(document.querySelectorAll("script[src*='recaptcha'][src*='render=']"));
                        let siteKey = null;
                        for (const script of scripts) {
                            try {
                                const u = new URL(script.src);
                                const render = u.searchParams.get("render");
                                if (render && render !== "explicit") {
                                    siteKey = render;
                                    break;
                                }
                            } catch {
                                // keep searching
                            }
                        }
                        if (siteKey) {
                            if (typeof enterprise.ready === "function") {
                                await new Promise((resolve) => enterprise.ready(resolve));
                            }
                            // Pre-warm reCAPTCHA (CloakBrowser needs priming on a
                            // cold engine — 3 throwaway execute() calls).
                            try {
                                for (let _w = 0; _w < 3; _w++) {
                                    enterprise.execute(siteKey, { action: recaptchaAction }).catch(() => {});
                                    await sleep(80 + Math.random() * 120);
                                }
                            } catch (e) {}
                            const token = await enterprise.execute(siteKey, { action: recaptchaAction });
                            if (token) {
                                recaptchaContext = {
                                    token,
                                    applicationType: "RECAPTCHA_APPLICATION_TYPE_WEB",
                                };
                            }
                        }
                    }

                    const clientContext = {
                        projectId,
                        tool: "PINHOLE",
                        userPaygateTier: "PAYGATE_TIER_TWO",
                        sessionId: ";" + Date.now(),
                    };
                    if (recaptchaContext) {
                        clientContext.recaptchaContext = recaptchaContext;
                    }

                    const body = {
                        mediaGenerationContext: { batchId },
                        clientContext,
                        requests: [
                            {
                                resolution: resEnum,
                                aspectRatio: videoAspect,
                                seed,
                                videoModelKey: modelKey,
                                metadata: { workflowId },
                                videoInput: { mediaId },
                            },
                        ],
                        useV2ModelConfig: true,
                    };

                    const resp = await fetch(
                        "https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoUpsampleVideo",
                        {
                            method: "POST",
                            credentials: "include",
                            headers: {
                                "content-type": "text/plain;charset=UTF-8",
                                "authorization": "Bearer " + authData.access_token,
                            },
                            body: JSON.stringify(body),
                        }
                    );

                    const text = await resp.text();
                    let data = null;
                    try {
                        data = JSON.parse(text);
                    } catch {
                        data = null;
                    }

                    if (!resp.ok) {
                        return {
                            ok: false,
                            error: (data && data.error && (data.error.message || data.error.status)) || text.slice(0, 300),
                        };
                    }

                    const mediaList = Array.isArray(data?.media) ? data.media : [];
                    const firstMedia = mediaList[0] || {};
                    return {
                        ok: true,
                        media_id: firstMedia.name || "",
                        remainingCredits: data?.remainingCredits ?? null,
                    };
                } catch (err) {
                    return { ok: false, error: String(err) };
                }
            }
            """,
            {
                "projectId": project_id,
                "mediaId": media_id,
                "workflowId": workflow_id,
                "batchId": batch_id,
                "seed": int(seed),
                "resEnum": res_enum,
                "modelKey": model_key,
                "videoAspect": video_aspect,
                "recaptchaAction": "VIDEO_GENERATION",
            },
        )

        if not result or not result.get("ok"):
            error = result.get("error", "unknown") if result else "null result"
            raise RuntimeError(f"Video upscale failed: {error}")

        upscaled_media_id = str(result.get("media_id") or "").strip()
        if not upscaled_media_id:
            raise RuntimeError("Video upscale response missing media id")
        return result

    async def _execute_video_job_via_flow_api(
        self,
        prompt,
        job_id,
        model,
        ratio,
        outs,
        video_upscale,
        video_model,
        video_sub_mode,
        ref_path,
        start_image_path,
        end_image_path,
        log_callback,
        queue_no=None,
    ):
        project_id = await self._ensure_flow_project_id(log_callback)
        if not project_id:
            detail = str(self._last_project_resolve_error or "").strip()
            if detail:
                return False, f"unable to resolve Flow project id: {detail}"
            return False, "unable to resolve Flow project id"

        delay_cfg = self._get_api_humanized_delay_config()
        await self._api_humanized_scroll_warmup(
            log_callback,
            min_seconds=delay_cfg["warmup_min"],
            max_seconds=delay_cfg["warmup_max"],
        )
        await self._api_humanized_pause(
            log_callback,
            min_seconds=delay_cfg["no_ref_wait_min"],
            max_seconds=delay_cfg["no_ref_wait_max"],
            reason="video pre-submit",
        )

        try:
            output_count = max(1, min(4, int(outs)))
        except Exception:
            output_count = 1

        sub_mode = self._normalize_video_sub_mode(
            video_sub_mode=video_sub_mode,
            ref_path=ref_path if ref_path and os.path.exists(ref_path) else None,
            start_image_path=start_image_path if start_image_path and os.path.exists(start_image_path) else None,
            end_image_path=end_image_path if end_image_path and os.path.exists(end_image_path) else None,
        )

        reference_media_id = ""
        if sub_mode == "ingredients":
            try:
                reference_media_id = await self._get_or_upload_reference(
                    project_id=project_id,
                    ref_path=ref_path,
                    log_callback=log_callback,
                )
                log_callback(f"[{self.account_name}] Reference for video: {reference_media_id}")
            except Exception as upload_err:
                log_callback(f"[{self.account_name}] Reference upload failed for video: {upload_err}")
                return False, f"Reference upload failed: {upload_err}"

        start_image_media_id = ""
        if sub_mode in ("frames_start", "frames_start_end"):
            try:
                start_image_media_id = await self._get_or_upload_reference(
                    project_id=project_id,
                    ref_path=start_image_path,
                    log_callback=log_callback,
                )
                log_callback(f"[{self.account_name}] Start image for video: {start_image_media_id}")
            except Exception as upload_err:
                log_callback(f"[{self.account_name}] Start image upload failed for video: {upload_err}")
                return False, f"Start image upload failed: {upload_err}"

        end_image_media_id = ""
        if sub_mode == "frames_start_end":
            try:
                end_image_media_id = await self._get_or_upload_reference(
                    project_id=project_id,
                    ref_path=end_image_path,
                    log_callback=log_callback,
                )
                log_callback(f"[{self.account_name}] End image for video: {end_image_media_id}")
            except Exception as upload_err:
                log_callback(f"[{self.account_name}] End image upload failed for video: {upload_err}")
                return False, f"End image upload failed: {upload_err}"

        selected_video_model = str(video_model or model or "").strip()
        if sub_mode == "ingredients" and "quality" in selected_video_model.lower():
            log_callback(
                f"[{self.account_name}] Quality mode does not support reference images. Switched to Fast."
            )
        model_key = self._resolve_video_model_key(
            model,
            video_model,
            video_sub_mode=sub_mode,
            ref_path=ref_path,
            start_image_path=start_image_path,
            end_image_path=end_image_path,
        )
        endpoint = self._resolve_video_endpoint(
            video_sub_mode=sub_mode,
            ref_path=ref_path,
            start_image_path=start_image_path,
            end_image_path=end_image_path,
        )
        if not model_key:
            return False, f"unsupported API video model mapping: {video_model or model}"
        if not endpoint:
            return False, f"unsupported API video endpoint mapping: {sub_mode}"

        upscale_target = str(video_upscale or "none").strip().lower() or "none"
        log_callback(
            f"[{self.account_name}] API video mode: model={model_key}, "
            f"ratio={self._resolve_video_aspect_ratio(ratio)}, outputs={output_count}, "
            f"upscale={upscale_target}, sub_mode={sub_mode}, "
            f"reference={'yes' if reference_media_id else 'no'}, "
            f"start={'yes' if start_image_media_id else 'no'}, end={'yes' if end_image_media_id else 'no'}"
        )
        saved_paths = []
        for idx in range(output_count):
            self._set_job_progress(job_id, step="video", poll_count=0)
            log_callback(f"[{self.account_name}] Generating video {idx + 1}/{output_count}...")
            gen_result = await self._video_api_generate_single(
                project_id=project_id,
                prompt=prompt,
                aspect_ratio=ratio,
                video_model_key=model_key,
                endpoint=endpoint,
                video_sub_mode=sub_mode,
                reference_media_id=reference_media_id,
                start_image_media_id=start_image_media_id,
                end_image_media_id=end_image_media_id,
            )

            media_id = str(gen_result.get("media_id") or "").strip()
            workflow_id = str(gen_result.get("workflow_id") or "").strip()
            log_callback(f"[{self.account_name}] Video submitted. Media: {media_id[:12]}... Polling...")
            await self._video_poll_until_complete(
                media_id=media_id,
                project_id=project_id,
                log_callback=log_callback,
                job_id=job_id,
                progress_step="video",
                poll_interval=5,
                max_polls=60,
            )
            log_callback(f"[{self.account_name}] Base video ready (720p).")

            download_media_id = media_id
            if upscale_target in ("1080p", "4k"):
                log_callback(f"[{self.account_name}] Upscaling to {upscale_target}...")
                upscale_result = await self._video_api_upscale(
                    project_id=project_id,
                    media_id=media_id,
                    workflow_id=workflow_id,
                    resolution=upscale_target,
                    aspect_ratio=ratio,
                )
                upscale_media_id = str(upscale_result.get("media_id") or "").strip()
                await self._video_poll_until_complete(
                    media_id=upscale_media_id,
                    project_id=project_id,
                    log_callback=log_callback,
                    job_id=job_id,
                    progress_step="video",
                    poll_interval=5,
                    max_polls=120 if upscale_target == "4k" else 60,
                )
                download_media_id = upscale_media_id
                log_callback(f"[{self.account_name}] Upscale complete ({upscale_target}).")

            if not download_media_id:
                return False, "Video generation returned no downloadable media id/url"

            try:
                self._set_job_progress(job_id, step="download", poll_count=0)
                output_path = await self._download_video_to_output(
                    download_media_id,
                    job_id,
                    log_callback,
                    queue_no=queue_no,
                    output_index=idx + 1,
                    output_count=output_count,
                )
                saved_paths.append(output_path)
                if idx == 0:
                    self._set_job_output_path(job_id, output_path)
                log_callback(f"[{self.account_name}] Video saved ({idx + 1}/{output_count}): {output_path}")
            except Exception as exc:
                return False, f"Video download failed: {exc}"

        if not saved_paths:
            return False, "Video API mode returned no saved files"
        log_callback(f"[{self.account_name}] Video generation complete ({len(saved_paths)} file(s)).")
        return True, None

    async def _execute_image_job_via_flow_api(
        self,
        prompt,
        job_id,
        model,
        ratio,
        outs,
        ref_path,
        ref_paths,
        log_callback,
        queue_no=None,
    ):
        model_name = self._resolve_image_model_name(model)
        if not model_name:
            return False, f"unsupported API model mapping: {model}"

        project_id = await self._ensure_flow_project_id(log_callback)
        if not project_id:
            detail = str(self._last_project_resolve_error or "").strip()
            if detail:
                return False, f"unable to resolve Flow project id: {detail}"
            return False, "unable to resolve Flow project id"

        delay_cfg = self._get_api_humanized_delay_config()
        await self._api_humanized_scroll_warmup(
            log_callback,
            min_seconds=delay_cfg["warmup_min"],
            max_seconds=delay_cfg["warmup_max"],
        )

        aspect_ratio = self._resolve_image_aspect_ratio(ratio)
        try:
            output_count = max(1, min(4, int(outs)))
        except Exception:
            output_count = 1

        batch_id = str(uuid.uuid4())
        reference_paths = []
        for candidate in list(ref_paths or []):
            candidate_path = str(candidate or "").strip()
            if candidate_path and os.path.exists(candidate_path) and candidate_path not in reference_paths:
                reference_paths.append(candidate_path)
        if not reference_paths and ref_path and os.path.exists(ref_path):
            reference_paths.append(str(ref_path).strip())

        has_reference = bool(reference_paths)
        reference_media_ids = []
        log_callback(
            f"[{self.account_name}] API mode: model={model_name}, ratio={aspect_ratio}, outputs={output_count}, references={len(reference_paths)}"
        )

        if has_reference:
            try:
                reference_media_ids, ref_upload_stats = await self._upload_multiple_references(
                    project_id=project_id,
                    ref_paths=reference_paths,
                    log_callback=log_callback,
                    include_stats=True,
                )
            except Exception as upload_err:
                log_callback(f"[{self.account_name}] Reference upload failed: {upload_err}")
                return False, f"Reference upload failed: {upload_err}"

            if int(ref_upload_stats.get("uploaded_count", 0) or 0) > 0:
                await self._api_humanized_pause(
                    log_callback,
                    min_seconds=delay_cfg["ref_wait_min"],
                    max_seconds=delay_cfg["ref_wait_max"],
                    reason="reference ready",
                )
            else:
                log_callback(f"[{self.account_name}] References came from cache. Skipping extra wait.")
        else:
            await self._api_humanized_pause(
                log_callback,
                min_seconds=delay_cfg["no_ref_wait_min"],
                max_seconds=delay_cfg["no_ref_wait_max"],
                reason="no-reference pre-submit",
            )

        saved_paths = []
        for idx in range(output_count):
            self._set_job_progress(job_id, step="image", poll_count=idx + 1)
            api_result = None
            for attempt in range(1, 3):
                seed = random.randint(1, 999999)
                api_result = await self._flow_api_generate_single_image(
                    project_id=project_id,
                    prompt=prompt,
                    model_name=model_name,
                    aspect_ratio=aspect_ratio,
                    batch_id=batch_id,
                    seed=seed,
                    reference_media_ids=reference_media_ids,
                    recaptcha_action="IMAGE_GENERATION",
                )
                if api_result and api_result.get("ok") and (api_result.get("fifeUrl") or api_result.get("mediaName")):
                    break
                await asyncio.sleep(1.5 * attempt)

            if not api_result or not api_result.get("ok"):
                error_text = api_result.get("error", "unknown") if api_result else "unknown"
                prefix = "MODERATION: " if self._is_moderation_failure(error_text) else ""
                return False, f"{prefix}API generation failed: {error_text}"
            media_name = str(api_result.get("mediaName") or "").strip()
            if reference_media_ids and media_name and media_name in set(reference_media_ids):
                log_callback(f"[{self.account_name}] Skipping reference image in results: {media_name}")
                continue
            download_url = api_result.get("fifeUrl") or self._build_media_redirect_url(api_result.get("mediaName"))
            if not download_url:
                return False, "API generation returned no downloadable media id/url"

            try:
                self._set_job_progress(job_id, step="download", poll_count=0)
                output_path = await self._download_binary_to_output(
                    download_url,
                    job_id,
                    name_hint=f"api_out{idx + 1}",
                    queue_no=queue_no,
                    output_index=idx + 1,
                    output_count=output_count,
                )
                saved_paths.append(output_path)
                if idx == 0:
                    self._set_job_output_path(job_id, output_path)
                log_callback(f"[{self.account_name}] API image saved ({idx + 1}/{output_count}): {output_path}")
            except Exception as exc:
                return False, f"API download failed: {exc}"

            await asyncio.sleep(0.4)

        if not saved_paths:
            return False, "API mode returned no saved files"
        log_callback(f"[{self.account_name}] API generation complete ({len(saved_paths)} file(s)).")
        return True, None

    async def _execute_pipeline_job_via_flow_api(
        self,
        prompt,
        video_prompt,
        job_id,
        model,
        ratio,
        ref_path,
        ref_paths,
        video_model,
        video_sub_mode,
        video_ratio,
        video_upscale,
        log_callback,
        queue_no=None,
    ):
        def is_audio_filter_error(error_detail):
            detail_upper = str(error_detail or "").upper()
            return (
                "AUDIO_FILTERED" in detail_upper
                or "AUDIO_GENERATION_FILTERED" in detail_upper
            )

        model_name = self._resolve_image_model_name(model)
        if not model_name:
            return False, f"unsupported API image model mapping: {model}"

        project_id = await self._ensure_flow_project_id(log_callback)
        if not project_id:
            detail = str(self._last_project_resolve_error or "").strip()
            if detail:
                return False, f"unable to resolve Flow project id: {detail}"
            return False, "unable to resolve Flow project id"

        delay_cfg = self._get_api_humanized_delay_config()
        await self._api_humanized_scroll_warmup(
            log_callback,
            min_seconds=delay_cfg["warmup_min"],
            max_seconds=delay_cfg["warmup_max"],
        )

        image_aspect_ratio = self._resolve_image_aspect_ratio(ratio)
        reference_paths = []
        for candidate in list(ref_paths or []):
            candidate_path = str(candidate or "").strip()
            if candidate_path and os.path.exists(candidate_path) and candidate_path not in reference_paths:
                reference_paths.append(candidate_path)
        if not reference_paths and ref_path and os.path.exists(ref_path):
            reference_paths.append(str(ref_path).strip())

        reference_media_ids = []
        log_callback(f"[{self.account_name}] Pipeline Step 1: Generating image...")
        self._set_job_progress(job_id, step="image", poll_count=1)
        if reference_paths:
            try:
                reference_media_ids, ref_upload_stats = await self._upload_multiple_references(
                    project_id=project_id,
                    ref_paths=reference_paths,
                    log_callback=log_callback,
                    include_stats=True,
                )
            except Exception as upload_err:
                return False, f"Pipeline Step 1 reference upload failed: {upload_err}"
            log_callback(f"[{self.account_name}] {len(reference_media_ids)} reference(s) uploaded/cached.")
            if int(ref_upload_stats.get("uploaded_count", 0) or 0) > 0:
                await self._api_humanized_pause(
                    log_callback,
                    min_seconds=delay_cfg["ref_wait_min"],
                    max_seconds=delay_cfg["ref_wait_max"],
                    reason="pipeline references ready",
                )
            else:
                log_callback(f"[{self.account_name}] Pipeline references came from cache. Skipping extra wait.")
        else:
            await self._api_humanized_pause(
                log_callback,
                min_seconds=delay_cfg["no_ref_wait_min"],
                max_seconds=delay_cfg["no_ref_wait_max"],
                reason="pipeline image pre-submit",
            )

        async def generate_pipeline_image(step_label):
            image_result = await self._flow_api_generate_single_image(
                project_id=project_id,
                prompt=prompt,
                model_name=model_name,
                aspect_ratio=image_aspect_ratio,
                batch_id=str(uuid.uuid4()),
                seed=random.randint(1, 999999),
                reference_media_ids=reference_media_ids,
                recaptcha_action="IMAGE_GENERATION",
            )

            if not image_result or not image_result.get("ok"):
                error_text = str(image_result.get("error") or "unknown") if image_result else "unknown"
                prefix = "MODERATION: " if self._is_moderation_failure(error_text) else ""
                raise RuntimeError(f"{prefix}{step_label} failed: {error_text}")

            generated_image_media_id = str(
                image_result.get("mediaName")
                or image_result.get("media_name")
                or image_result.get("media_id")
                or ""
            ).strip()
            if not generated_image_media_id:
                raise RuntimeError(f"{step_label} failed: generated image media id missing")

            return generated_image_media_id

        sub_mode = self._normalize_video_sub_mode(video_sub_mode=video_sub_mode)
        if sub_mode not in ("ingredients", "frames_start"):
            return False, f"unsupported pipeline video mode: {sub_mode}"

        model_key = self._resolve_video_model_key(
            video_model or model,
            video_model=video_model,
            video_sub_mode=sub_mode,
        )
        endpoint = self._resolve_video_endpoint(video_sub_mode=sub_mode)
        if not model_key:
            return False, f"unsupported pipeline video model mapping: {video_model or model}"
        if not endpoint:
            return False, f"unsupported pipeline video endpoint mapping: {sub_mode}"

        await self._api_humanized_pause(
            log_callback,
            min_seconds=delay_cfg["no_ref_wait_min"],
            max_seconds=delay_cfg["no_ref_wait_max"],
            reason="pipeline video pre-submit",
        )

        target_video_prompt = str(video_prompt or "animate").strip() or "animate"
        target_video_ratio = str(video_ratio or ratio or "Landscape (16:9)").strip()
        generated_image_media_id = ""
        download_media_id = ""
        upscale_target = str(video_upscale or "none").strip().lower() or "none"

        for pipeline_attempt in range(0, 4):
            if pipeline_attempt == 0:
                try:
                    self._set_job_progress(job_id, step="image", poll_count=1)
                    generated_image_media_id = await generate_pipeline_image("Pipeline Step 1")
                except RuntimeError as err:
                    return False, str(err)
                log_callback(
                    f"[{self.account_name}] Step 1 complete. Generated image: {generated_image_media_id[:12]}..."
                )
                step2_label = "Pipeline Step 2: Generating video from image..."
            else:
                log_callback(
                    f"[{self.account_name}] Pipeline retry {pipeline_attempt}/3 — generating new image..."
                )
                try:
                    self._set_job_progress(job_id, step="image", poll_count=pipeline_attempt + 1)
                    generated_image_media_id = await generate_pipeline_image("Pipeline retry image generation")
                except RuntimeError as err:
                    return False, str(err)
                log_callback(f"[{self.account_name}] New image: {generated_image_media_id[:12]}...")
                step2_label = "Pipeline Step 2: Generating video from new image..."

            try:
                self._set_job_progress(job_id, step="video", poll_count=0)
                log_callback(f"[{self.account_name}] {step2_label}")
                gen_result = await self._video_api_generate_single(
                    project_id=project_id,
                    prompt=target_video_prompt,
                    aspect_ratio=target_video_ratio,
                    video_model_key=model_key,
                    endpoint=endpoint,
                    video_sub_mode=sub_mode,
                    reference_media_id=generated_image_media_id if sub_mode == "ingredients" else "",
                    start_image_media_id=generated_image_media_id if sub_mode == "frames_start" else "",
                    end_image_media_id="",
                )

                media_id = str(gen_result.get("media_id") or "").strip()
                workflow_id = str(gen_result.get("workflow_id") or "").strip()
                log_callback(f"[{self.account_name}] Video submitted. Polling...")
                await self._video_poll_until_complete(
                    media_id=media_id,
                    project_id=project_id,
                    log_callback=log_callback,
                    job_id=job_id,
                    progress_step="video",
                    poll_interval=5,
                    max_polls=60,
                )
                log_callback(f"[{self.account_name}] Video generation complete.")

                download_media_id = media_id
                if upscale_target in ("1080p", "4k"):
                    log_callback(f"[{self.account_name}] Upscaling to {upscale_target}...")
                    upscale_result = await self._video_api_upscale(
                        project_id=project_id,
                        media_id=media_id,
                        workflow_id=workflow_id,
                        resolution=upscale_target,
                        aspect_ratio=target_video_ratio,
                    )
                    upscale_media_id = str(upscale_result.get("media_id") or "").strip()
                    await self._video_poll_until_complete(
                        media_id=upscale_media_id,
                        project_id=project_id,
                        log_callback=log_callback,
                        job_id=job_id,
                        progress_step="video",
                        poll_interval=5,
                        max_polls=120 if upscale_target == "4k" else 60,
                    )
                    download_media_id = upscale_media_id

                if pipeline_attempt > 0:
                    log_callback(f"[{self.account_name}] Retry {pipeline_attempt} succeeded!")
                break
            except RuntimeError as err:
                error_text = str(err or "").strip()
                if is_audio_filter_error(error_text) and pipeline_attempt < 3:
                    if pipeline_attempt == 0:
                        log_callback(
                            f"[{self.account_name}] Audio filter triggered. Regenerating image with new seed and retrying..."
                        )
                    else:
                        log_callback(
                            f"[{self.account_name}] Audio filter again on attempt {pipeline_attempt}. Trying new image..."
                        )
                    continue
                if is_audio_filter_error(error_text):
                    return False, "Audio filter persisted after 3 pipeline retries"
                return False, error_text

        if not download_media_id:
            return False, "Pipeline failed before final video download"

        try:
            self._set_job_progress(job_id, step="download", poll_count=0)
            output_path = await self._download_video_to_output(
                download_media_id,
                job_id,
                log_callback,
                queue_no=queue_no,
                output_index=1,
                output_count=1,
            )
        except Exception as exc:
            return False, f"Pipeline video download failed: {exc}"

        self._set_job_output_path(job_id, output_path)
        log_callback(f"[{self.account_name}] Pipeline complete! Video: {output_path}")
        return True, None

    async def _read_error_toast(self):
        try:
            return await self.page.evaluate(
                """
                () => {
                    const toasts = Array.from(document.querySelectorAll("li[data-sonner-toast]"));
                    if (!toasts.length) return { queueFull: false, policy: false, text: "" };

                    const latest = toasts[toasts.length - 1];
                    const text = (latest.textContent || "").trim().toLowerCase();
                    if (!text) return { queueFull: false, policy: false, text: "" };

                    const queueFull = text.includes("queue") || text.includes("limit") || text.includes("5");
                    const policy = text.includes("policy") || text.includes("not allowed") || text.includes("blocked");
                    return { queueFull, policy, text };
                }
                """
            )
        except Exception:
            return {"queueFull": False, "policy": False, "text": ""}

    async def _submit_with_queue_handling(self, log_callback):
        for attempt in range(1, 6):
            await self._click_generate()
            await asyncio.sleep(1.2)

            toast = await self._read_error_toast()
            if toast.get("policy"):
                raise Exception(f"Prompt policy blocked: {toast.get('text', 'policy error')}")

            if not toast.get("queueFull"):
                return

            wait_seconds = 8 * attempt
            log_callback(
                f"[{self.account_name}] Queue full detected, waiting {wait_seconds}s before retry ({attempt}/5)..."
            )
            await asyncio.sleep(wait_seconds)

        raise Exception("Queue remained full after retries.")

    async def execute_job(self, job_data, log_callback):
        """Executes a single generation job on Google Labs."""
        prompt = job_data["prompt"]
        job_id = job_data["id"]
        model = job_data["model"]
        model_lower = str(model or "").lower()
        ratio = job_data["aspect_ratio"]
        outs = job_data["output_count"]
        ref_path = job_data.get("ref_path")
        ref_paths = job_data.get("ref_paths") or []
        if isinstance(ref_paths, str):
            ref_paths = [ref_paths] if str(ref_paths).strip() else []
        ref_paths = [str(path).strip() for path in ref_paths if str(path or "").strip()]
        if not ref_paths and ref_path:
            ref_paths = [str(ref_path).strip()]
        if ref_paths and not ref_path:
            ref_path = ref_paths[0]
        job_type = str(job_data.get("job_type") or "").strip().lower()
        if job_type not in ("image", "video", "pipeline"):
            job_type = "video" if "veo" in model_lower else "image"
        is_video_job = job_type == "video"
        video_model = str(job_data.get("video_model") or (model if is_video_job else "")).strip()
        video_sub_mode = str(job_data.get("video_sub_mode") or "").strip()
        video_ratio = str(job_data.get("video_ratio") or ratio or "").strip()
        video_prompt = str(job_data.get("video_prompt") or "animate").strip() or "animate"
        video_upscale = str(job_data.get("video_upscale") or "none").strip().lower() or "none"
        video_output_count = job_data.get("video_output_count", outs)
        start_image_path = job_data.get("start_image_path")
        end_image_path = job_data.get("end_image_path")
        queue_no = job_data.get("queue_no")
        output_queue_no = job_data.get("output_index")
        if self._normalize_queue_no(output_queue_no) is None:
            output_queue_no = queue_no
        container_selector = "div[data-index][data-item-index]"
        download_path = None
        image_ref_paths = [path for path in ref_paths if path and os.path.exists(path)]
        if not image_ref_paths and ref_path and os.path.exists(ref_path):
            image_ref_paths = [ref_path]

        try:
            if not await self.ensure_active_page(log_callback):
                raise RuntimeError("Browser session is not active. Re-initialize account session.")

            if job_type == "pipeline":
                log_callback(f"[{self.account_name}] Trying API pipeline mode for image-to-video generation...")
                return await self._execute_pipeline_job_via_flow_api(
                    prompt=prompt,
                    video_prompt=video_prompt,
                    job_id=job_id,
                    model=model,
                    ratio=ratio,
                    ref_path=ref_path,
                    ref_paths=ref_paths,
                    video_model=video_model,
                    video_sub_mode=video_sub_mode,
                    video_ratio=video_ratio,
                    video_upscale=video_upscale,
                    log_callback=log_callback,
                    queue_no=output_queue_no,
                )

            execution_mode = self._get_image_execution_mode()
            allow_api = execution_mode == "api_only"
            api_eligible = (
                self._is_api_eligible_video_job(
                    model,
                    video_model,
                    ref_path=ref_path,
                    aspect_ratio=ratio,
                    video_sub_mode=video_sub_mode,
                    start_image_path=start_image_path,
                    end_image_path=end_image_path,
                )
                if is_video_job
                else self._is_api_eligible_image_job(model, ref_path)
            )
            has_reference = bool(
                any(path and os.path.exists(path) for path in ref_paths)
                or
                (ref_path and os.path.exists(ref_path))
                or (start_image_path and os.path.exists(start_image_path))
                or (end_image_path and os.path.exists(end_image_path))
            )

            if allow_api and api_eligible:
                if is_video_job:
                    log_callback(f"[{self.account_name}] Trying HAR-based API mode for video generation...")
                    try:
                        api_success, api_error = await self._execute_video_job_via_flow_api(
                            prompt=prompt,
                            job_id=job_id,
                            model=model,
                            ratio=ratio,
                            outs=video_output_count,
                            video_upscale=video_upscale,
                            video_model=video_model,
                            video_sub_mode=video_sub_mode,
                            ref_path=ref_path,
                            start_image_path=start_image_path,
                            end_image_path=end_image_path,
                            log_callback=log_callback,
                            queue_no=output_queue_no,
                        )
                    except Exception as api_exc:
                        api_success, api_error = False, str(api_exc)
                else:
                    log_callback(f"[{self.account_name}] Trying HAR-based API mode for image generation...")
                    api_success, api_error = await self._execute_image_job_via_flow_api(
                        prompt=prompt,
                        job_id=job_id,
                        model=model,
                        ratio=ratio,
                        outs=outs,
                        ref_path=ref_path,
                        ref_paths=ref_paths,
                        log_callback=log_callback,
                        queue_no=output_queue_no,
                    )
                if api_success:
                    return True, None
                api_error_text = str(api_error or "").strip()
                if not api_error_text:
                    api_error_text = "unknown API error"

                if self._is_moderation_failure(api_error_text):
                    normalized_error = api_error_text
                    if normalized_error.startswith("MODERATION:"):
                        normalized_error = normalized_error.split("MODERATION:", 1)[1].strip()
                    log_callback(
                        f"[{self.account_name}] Content blocked: {normalized_error}. "
                        "Skipping retry (same result guaranteed)."
                    )
                    return False, f"MODERATION: {normalized_error}"
                log_callback(
                    f"[{self.account_name}] API mode failed: {api_error_text}. Will retry via queue."
                )
                return False, f"API failed: {api_error_text or 'unknown API error'}"

            media_kind = "video" if is_video_job else "image"
            if not api_eligible:
                return False, f"API-only mode unsupported for selected {media_kind} model: {model}"
            return False, "API generation mode is unavailable."
            

        except Exception as exc:
            return False, f"Unexpected error: {exc}"

    async def cleanup(self):
        context_pid = self._extract_context_process_pid(self.context)
        chrome_pid = getattr(self.chrome_process, "pid", None) if self.chrome_process is not None else None
        context_closed = False
        if self.context is not None:
            try:
                await self._maybe_await(self.context.close())
                context_closed = True
            except Exception:
                pass
        if context_closed:
            await asyncio.sleep(2)
        if self.browser is not None:
            try:
                await self.browser.close()
            except Exception:
                pass
        if self.chrome_process is not None:
            try:
                self.chrome_process.terminate()
            except Exception:
                pass
            try:
                self.chrome_process.wait(timeout=5)
            except Exception:
                try:
                    self.chrome_process.kill()
                except Exception:
                    pass
            if chrome_pid:
                self._untrack_pid(chrome_pid)
            self.chrome_process = None
        if context_pid:
            self._untrack_pid(context_pid)
        for pid in list(self._tracked_pids):
            self._untrack_pid(pid)
        self.browser = None
        self.context = None
        self.page = None
        self._stealth_page_ids.clear()
        self._warmed_page_ids.clear()
