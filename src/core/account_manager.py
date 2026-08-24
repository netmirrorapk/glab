import os
import platform
import random
import re
import subprocess
import time
import json
import shutil
import tempfile
import inspect
from urllib.parse import urlsplit
from playwright.async_api import async_playwright
import asyncio
import requests

from src.core.runtime_stdio import ensure_std_streams
ensure_std_streams()
from src.core.app_paths import get_sessions_dir
from src.core.cloakbrowser_support import (
    get_cloakbrowser_cache_dir,
    load_cloakbrowser_api,
)
from src.core.cloak_downloader import download_cloakbrowser_with_progress
from src.core.cookie_warmup import heavy_cookie_warmup
from src.core.process_tracker import process_tracker, cleanup_session_locks
from src.db.db_manager import get_account_flag, get_bool_setting, get_setting, set_account_flag

try:
    from playwright_stealth import stealth_async
except Exception:
    stealth_async = None

DATA_DIR = str(get_sessions_dir())
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

class AccountManager:
    """Manages spawning Playwright to handle Google Auth sessions."""
    FLOW_PAGE_URL = "https://labs.google/fx/tools/flow"
    FLOW_REFERER = "https://www.google.com"
    # Login start URLs per target. Both open Google login in real Chrome — this is
    # what reliably leaves an ACTIVE google.com session in the profile. dola's own
    # login is auto_open, so once Google is active, visiting dola.com (for generation
    # or the delete-account flow) silently OAuths and logs in with NO extra click.
    LOGIN_START_URLS = {
        "flow": "https://accounts.google.com",
        "dola": "https://accounts.google.com",
    }
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

    SESSION_CHECK_IGNORE_PATTERNS = (
        "Cache",
        "Code Cache",
        "GPUCache",
        "GrShaderCache",
        "ShaderCache",
        "DawnCache",
        "Crashpad",
        "Crash Reports",
        "Singleton*",
        "*.lock",
    )

    COOKIE_PATH_CANDIDATES = (
        os.path.join("Default", "Network", "Cookies"),
        os.path.join("Network", "Cookies"),
        os.path.join("Default", "Cookies"),
        "Cookies",
    )

    @staticmethod
    def _safe_session_dir_name(raw_name: str) -> str:
        clean = re.sub(r"[^A-Za-z0-9@._-]+", "_", str(raw_name or "")).strip("._-")
        return clean or f"account_{int(time.time())}"

    @staticmethod
    def _build_proxy_config(proxy_value):
        proxy_text = str(proxy_value or "").strip()
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

    @staticmethod
    def _persistent_context_launch_options(headless, proxy_value=None):
        launch_options = {
            "headless": bool(headless),
            "ignore_default_args": ["--enable-automation"],
            "args": list(AccountManager.PERSISTENT_CONTEXT_ARGS),
            "viewport": {"width": 1920, "height": 1080},
            "user_agent": AccountManager.CHROME_USER_AGENT,
            "locale": "en-US",
        }
        proxy_config = AccountManager._build_proxy_config(proxy_value)
        if proxy_config:
            launch_options["proxy"] = proxy_config
        return launch_options

    @staticmethod
    def _resolve_browser_path(playwright_instance):
        browser_type = getattr(playwright_instance, "chromium", None)
        executable_path = getattr(browser_type, "executable_path", "")
        try:
            if callable(executable_path):
                executable_path = executable_path()
        except Exception:
            executable_path = ""
        return str(executable_path or "").strip()

    @staticmethod
    def _find_chrome_path():
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

    @staticmethod
    def _is_cdp_endpoint_live(port):
        try:
            response = requests.get(f"http://127.0.0.1:{int(port)}/json/version", timeout=2)
            return response.ok
        except Exception:
            return False

    @staticmethod
    def _kill_chrome_on_port(port):
        port = int(port or 0)
        if port <= 0:
            return

        system_name = platform.system()

        if system_name == "Darwin":
            # Mac: kill any Chrome using this debug port
            try:
                subprocess.run(
                    ["pkill", "-f", f"remote-debugging-port={port}"],
                    capture_output=True, timeout=5,
                )
                time.sleep(1)
            except Exception:
                pass
            # Also try lsof to find PIDs on the port
            try:
                output = subprocess.check_output(
                    ["lsof", "-ti", f":{port}"],
                    text=True, timeout=5,
                ).strip()
                for pid_str in output.splitlines():
                    try:
                        os.kill(int(pid_str.strip()), 9)
                    except Exception:
                        pass
            except Exception:
                pass

        elif system_name == "Windows":
            try:
                _no_window = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
                output = subprocess.check_output(
                    ["netstat", "-ano"],
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    **_no_window,
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
                    if process_tracker.is_tracked(normalized):
                        process_tracker.kill_pid(normalized)
            except Exception:
                pass

        else:
            # Linux: similar to Mac
            try:
                subprocess.run(
                    ["pkill", "-f", f"remote-debugging-port={port}"],
                    capture_output=True, timeout=5,
                )
            except Exception:
                pass

    @staticmethod
    async def _maybe_await(result):
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

    @staticmethod
    def _register_context_process(context):
        pid = AccountManager._extract_context_process_pid(context)
        if pid:
            process_tracker.register(pid)
        return pid

    @staticmethod
    def _unregister_context_process(context):
        pid = AccountManager._extract_context_process_pid(context)
        if pid:
            process_tracker.unregister(pid)
        return pid

    @staticmethod
    async def _close_context_and_flush(context, flush_delay=2):
        if context is None:
            return
        try:
            await AccountManager._maybe_await(context.close())
        except Exception:
            pass  # Don't re-raise — let cleanup continue
        await asyncio.sleep(max(0, float(flush_delay or 0)))

    @staticmethod
    def _find_cookie_file(session_path):
        base_path = str(session_path or "").strip()
        if not base_path:
            return None
        for rel_path in AccountManager.COOKIE_PATH_CANDIDATES:
            candidate = os.path.join(base_path, rel_path)
            if os.path.exists(candidate):
                return candidate
        return None

    @staticmethod
    async def _goto_flow_page(page, timeout=30000):
        if page is None:
            raise RuntimeError("Browser page is not available.")
        return await AccountManager._maybe_await(
            page.goto(
                AccountManager.FLOW_PAGE_URL,
                referer=AccountManager.FLOW_REFERER,
                wait_until="domcontentloaded",
                timeout=timeout,
            )
        )

    @staticmethod
    async def _apply_browser_overrides(context, page):
        if context is None or page is None:
            return
        try:
            await AccountManager._maybe_await(context.add_init_script(AccountManager.WEBDRIVER_OVERRIDE_SCRIPT))
        except Exception:
            pass
        try:
            await AccountManager._maybe_await(page.add_init_script(AccountManager.WEBDRIVER_OVERRIDE_SCRIPT))
        except Exception:
            pass
        try:
            await AccountManager._maybe_await(page.evaluate(
                "() => { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); }"
            ))
        except Exception:
            pass

    @staticmethod
    async def _apply_stealth_to_page(page):
        if page is None or stealth_async is None:
            return
        try:
            await stealth_async(page)
        except Exception:
            pass

    @staticmethod
    def _pick_best_email(candidates):
        seen = []
        for candidate in candidates:
            email = str(candidate or "").strip().lower()
            if not email or email in seen:
                continue
            seen.append(email)

        if not seen:
            return None

        gmail = [e for e in seen if e.endswith("@gmail.com") or e.endswith("@googlemail.com")]
        if gmail:
            return gmail[0]
        return seen[0]

    @staticmethod
    def _extract_emails_from_text(text):
        if not text:
            return []
        return EMAIL_RE.findall(str(text))

    @staticmethod
    def _detect_email_from_json_file(file_path):
        if not os.path.isfile(file_path):
            return None

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return None

        found = []

        def walk(node):
            if isinstance(node, dict):
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
            elif isinstance(node, str):
                found.extend(AccountManager._extract_emails_from_text(node))

        walk(data)
        return AccountManager._pick_best_email(found)

    @staticmethod
    def detect_email_from_session_dir(session_dir):
        if not session_dir or not os.path.isdir(session_dir):
            return None

        candidates = []
        json_candidates = [
            os.path.join(session_dir, "Default", "Preferences"),
            os.path.join(session_dir, "Local State"),
        ]
        for file_path in json_candidates:
            email = AccountManager._detect_email_from_json_file(file_path)
            if email:
                candidates.append(email)

        # Very light fallback: filename itself might already be email based.
        base = os.path.basename(os.path.normpath(session_dir))
        if EMAIL_RE.search(base or ""):
            candidates.append(base)

        return AccountManager._pick_best_email(candidates)

    @staticmethod
    async def _detect_logged_in_email(context):
        found = []

        try:
            cookies = await AccountManager._maybe_await(context.cookies())
            for cookie in cookies:
                value = str(cookie.get("value") or "")
                found.extend(EMAIL_RE.findall(value))
        except Exception:
            pass

        try:
            pages = list(getattr(context, "pages", []) or [])
        except Exception:
            pages = []

        for page in pages:
            try:
                # Read limited text from current page to avoid heavy calls.
                text = await asyncio.wait_for(
                    AccountManager._maybe_await(page.evaluate(
                        "() => (document && document.body && document.body.innerText) ? document.body.innerText.slice(0, 8000) : ''"
                    )),
                    timeout=2,
                )
                found.extend(EMAIL_RE.findall(str(text or "")))
            except Exception:
                continue

        return AccountManager._pick_best_email(found)

    @staticmethod
    def _make_temp_status_session_copy(session_dir):
        if not session_dir or not os.path.isdir(session_dir):
            raise FileNotFoundError(f"Session folder not found: {session_dir}")

        temp_root = tempfile.mkdtemp(prefix="g_labs_login_check_")
        clone_dir = os.path.join(temp_root, "session")
        shutil.copytree(
            session_dir,
            clone_dir,
            ignore=shutil.ignore_patterns(*AccountManager.SESSION_CHECK_IGNORE_PATTERNS),
        )
        return temp_root, clone_dir

    @staticmethod
    async def check_account_login_status(session_path, proxy=None):
        """
        Load a saved browser profile in headless mode and verify the Labs auth session.
        Returns: {"logged_in": bool, "email": str, "expires": str, "error": str}
        """
        temp_root = None
        context = None
        try:
            temp_root, check_session_path = AccountManager._make_temp_status_session_copy(session_path)

            async with async_playwright() as p:
                launch_options = AccountManager._persistent_context_launch_options(headless=True, proxy_value=proxy)
                browser_path = AccountManager._resolve_browser_path(p)
                if browser_path:
                    launch_options["executable_path"] = browser_path
                context = await p.chromium.launch_persistent_context(
                    user_data_dir=check_session_path,
                    **launch_options,
                )

                page = context.pages[0] if context.pages else await context.new_page()
                await AccountManager._apply_browser_overrides(context, page)
                await AccountManager._apply_stealth_to_page(page)

                try:
                    await AccountManager._goto_flow_page(page, timeout=15000)
                except Exception:
                    # The auth endpoint below is the real source of truth, so keep going.
                    pass

                auth_result = await page.evaluate(
                    """
                    async () => {
                        try {
                            const resp = await fetch(
                                "https://labs.google/fx/api/auth/session",
                                {
                                    credentials: "include",
                                    redirect: "follow",
                                }
                            );
                            const contentType = (resp.headers.get("content-type") || "").toLowerCase();
                            let data = null;
                            let text = "";
                            if (contentType.includes("application/json")) {
                                data = await resp.json().catch(() => null);
                            } else {
                                text = await resp.text().catch(() => "");
                            }

                            if (data && data.access_token) {
                                return {
                                    logged_in: true,
                                    email: data.email || data.user?.email || "",
                                    expires: data.expires || "",
                                    error: "",
                                };
                            }

                            const errorText =
                                data?.error?.message ||
                                data?.message ||
                                text ||
                                `HTTP ${resp.status}`;
                            return {
                                logged_in: false,
                                email: data?.email || data?.user?.email || "",
                                expires: data?.expires || "",
                                error: String(errorText || "No access token").slice(0, 300),
                            };
                        } catch (err) {
                            return {
                                logged_in: false,
                                email: "",
                                expires: "",
                                error: String(err),
                            };
                        }
                    }
                    """
                )

                return auth_result or {
                    "logged_in": False,
                    "email": "",
                    "expires": "",
                    "error": "Login check returned no result",
                }
        except Exception as e:
            return {
                "logged_in": False,
                "email": "",
                "expires": "",
                "error": f"Browser launch failed: {e}",
            }
        finally:
            try:
                if context is not None:
                    await context.close()
            except Exception:
                pass
            if temp_root and os.path.isdir(temp_root):
                shutil.rmtree(temp_root, ignore_errors=True)

    @staticmethod
    async def delete_dola_account(session_path, proxy=None, update_log_callback=None, headless=True, dry_run=False):
        """Open the account's saved browser profile and delete the dola.com account.

        Deletes ONLY the dola.com (ByteDance passport) account. The linked Google
        login and its cookies are on a separate domain and are left untouched, so
        the profile stays reusable for Google. Returns {"ok": bool, "detail": str}.
        """
        from src.core.dola_api import DolaSession

        logger = update_log_callback if callable(update_log_callback) else (lambda _m: None)
        session_path = str(session_path or "").strip()
        if not session_path or not os.path.isdir(session_path):
            return {"ok": False, "detail": "session folder not found on disk"}

        # Run on a TEMP COPY of the profile (not the live one) so a warmup/lock on
        # the live profile can't crash the launch. The copy carries the login
        # cookies, and dola deletion is server-side, so nothing needs to persist back.
        temp_root = None
        context = None
        try:
            temp_root, work_session_path = AccountManager._make_temp_status_session_copy(session_path)
            cleanup_session_locks(work_session_path)

            async with async_playwright() as p:
                launch_options = AccountManager._persistent_context_launch_options(
                    headless=bool(headless), proxy_value=proxy
                )
                # The profile is written by the system's (newer) real Chrome. Playwright's
                # bundled Chromium can't open it (version mismatch -> instant crash), so
                # launch with real Chrome itself. Fall back to bundled Chromium only if
                # Chrome isn't installed.
                real_chrome = AccountManager._find_chrome_path()
                if real_chrome:
                    launch_options["executable_path"] = real_chrome
                else:
                    browser_path = AccountManager._resolve_browser_path(p)
                    if browser_path:
                        launch_options["executable_path"] = browser_path
                context = await p.chromium.launch_persistent_context(
                    user_data_dir=work_session_path,
                    **launch_options,
                )
                AccountManager._register_context_process(context)
                page = context.pages[0] if context.pages else await context.new_page()
                await AccountManager._apply_browser_overrides(context, page)
                await AccountManager._apply_stealth_to_page(page)

                # Some profiles (Mac CloakBrowser hybrid) keep login in exported_cookies.json.
                cookies_json = os.path.join(work_session_path, "exported_cookies.json")
                if os.path.exists(cookies_json):
                    try:
                        with open(cookies_json, "r", encoding="utf-8") as f:
                            saved = json.load(f)
                        if isinstance(saved, list) and saved:
                            await AccountManager._maybe_await(context.add_cookies(saved))
                    except Exception:
                        pass

                sess = DolaSession(context, page, logger=logger)
                ok, detail = await sess.delete_account(log=logger, dry_run=bool(dry_run))
                return {"ok": bool(ok), "detail": str(detail)}
        except Exception as exc:
            return {"ok": False, "detail": f"delete run failed: {str(exc)[:200]}"}
        finally:
            try:
                if context is not None:
                    await AccountManager._close_context_and_flush(context, flush_delay=1)
            except Exception:
                pass
            AccountManager._unregister_context_process(context)
            if temp_root and os.path.isdir(temp_root):
                try:
                    shutil.rmtree(temp_root, ignore_errors=True)
                except Exception:
                    pass

    @staticmethod
    async def _run_cookie_warmup_once(
        account_name,
        session_path,
        proxy=None,
        update_log_callback=None,
        forced_browser_mode=None,
        warmup_progress_callback=None,
        warmup_complete_callback=None,
    ):
        account_label = str(account_name or "").strip()
        if not account_label or not session_path or not os.path.isdir(session_path):
            return

        logger = update_log_callback if callable(update_log_callback) else (lambda _msg: None)
        if not get_bool_setting("cookie_warmup", True):
            logger(f"[{account_label}] Cookie warm-up disabled. Skipping.")
            return

        if str(get_account_flag(account_label, "warmup_done") or "").strip().lower() == "true":
            logger(f"[{account_label}] Cookie warm-up already done. Skipping.")
            return

        browser_mode = str(forced_browser_mode or get_setting("browser_mode", "real_chrome") or "real_chrome").strip().lower()
        if browser_mode == "playwright":
            browser_mode = "visible"
        # Mac hybrid: force Real Chrome for warmup — same as login, cookies save reliably.
        if browser_mode == "cloakbrowser" and platform.system() == "Darwin":
            logger(f"[{account_label}] Mac hybrid mode: using Real Chrome for warmup.")
            browser_mode = "real_chrome"
        cloak_display = str(get_setting("cloak_display", "headless") or "headless").strip().lower()

        logger(f"[{account_label}] Starting cookie warm-up (one-time)...")

        context = None
        browser = None
        chrome_process = None
        using_cloak = False
        page = None

        try:
            async with async_playwright() as p:
                if browser_mode == "cloakbrowser":
                    cloak_api = load_cloakbrowser_api()
                    cloak_persistent_async = cloak_api.get("persistent_async")
                    cloak_binary_info = cloak_api.get("binary_info")
                    cloak_ensure_binary = cloak_api.get("ensure_binary")
                    if not cloak_api.get("available") or cloak_persistent_async is None:
                        logger(f"[{account_label}] CloakBrowser missing for warm-up. Falling back to Real Chrome CDP.")
                        browser_mode = "real_chrome"
                    else:
                        using_cloak = True
                        headless = cloak_display == "headless"
                        loop = asyncio.get_running_loop()
                        info = await loop.run_in_executor(None, cloak_binary_info) if callable(cloak_binary_info) else {}
                        if not bool((info or {}).get("installed")):
                            logger(f"[{account_label}] Downloading CloakBrowser binary (~200MB, first time only)...")
                            if not callable(cloak_ensure_binary):
                                raise RuntimeError("CloakBrowser binary installer is unavailable.")
                            await loop.run_in_executor(None, cloak_ensure_binary)
                            logger(f"[{account_label}] CloakBrowser binary ready!")

                        import hashlib as _hashlib
                        _seed_base = str(account_label or "slot").strip() or "slot"
                        _seed = int(_hashlib.md5(_seed_base.encode("utf-8")).hexdigest()[:8], 16) % 99999
                        _cloak_args = AccountManager._build_cloak_launch_args(_seed)
                        logger(f"[{account_label}] CloakBrowser warmup (seed={_seed}, args={_cloak_args})")
                        # SOCKS5+auth → local HTTP bridge
                        _raw = str(proxy or "").strip()
                        if _raw:
                            try:
                                from src.core.proxy_bridge import get_or_create_bridge
                                _bridge_proxy = get_or_create_bridge(_raw)
                            except Exception:
                                _bridge_proxy = _raw
                        else:
                            _bridge_proxy = None
                        # Login/warmup uses "careful" humanize preset —
                        # slower but stronger reCAPTCHA score on fresh session.
                        # API generation flow (bot_engine.py) stays on default preset for speed.
                        context = await cloak_persistent_async(
                            session_path,
                            headless=headless,
                            args=_cloak_args,
                            proxy=_bridge_proxy,
                            humanize={"preset": "careful"},
                        )
                        AccountManager._register_context_process(context)
                        pages = list(getattr(context, "pages", []) or [])
                        page = pages[0] if pages else await AccountManager._maybe_await(context.new_page())

                if browser_mode == "real_chrome":
                    chrome_path = AccountManager._find_chrome_path()
                    if not chrome_path:
                        raise RuntimeError("Chrome not found! Install Google Chrome.")

                    chrome_args = [
                        chrome_path,
                        "--remote-debugging-port=9220",
                        "--remote-debugging-address=127.0.0.1",
                        f"--user-data-dir={session_path}",
                        "--headless=new",
                        "--no-first-run",
                        "--disable-blink-features=AutomationControlled",
                        "--window-size=1920,1080",
                    ]
                    if proxy:
                        try:
                            from src.core.proxy_bridge import get_or_create_bridge
                            _bridged = get_or_create_bridge(str(proxy))
                        except Exception:
                            _bridged = str(proxy)
                        chrome_args.append(f"--proxy-server={_bridged}")

                    creationflags = 0
                    popen_kwargs = {}
                    if platform.system() == "Windows":
                        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    else:
                        popen_kwargs["stdout"] = subprocess.DEVNULL
                        popen_kwargs["stderr"] = subprocess.DEVNULL

                    chrome_process = subprocess.Popen(
                        chrome_args,
                        creationflags=creationflags,
                        **popen_kwargs,
                    )
                    process_tracker.register(getattr(chrome_process, "pid", None))
                    await asyncio.sleep(3)
                    if not AccountManager._is_cdp_endpoint_live(9220):
                        try:
                            if chrome_process is not None and chrome_process.poll() is None:
                                chrome_process.terminate()
                                chrome_process.wait(timeout=5)
                        except Exception:
                            try:
                                if chrome_process is not None and chrome_process.poll() is None:
                                    chrome_process.kill()
                            except Exception:
                                pass
                        process_tracker.unregister(getattr(chrome_process, "pid", None))
                        AccountManager._kill_chrome_on_port(9220)
                        chrome_process = subprocess.Popen(
                            chrome_args,
                            creationflags=creationflags,
                            **popen_kwargs,
                        )
                        process_tracker.register(getattr(chrome_process, "pid", None))
                        await asyncio.sleep(3)
                    if not AccountManager._is_cdp_endpoint_live(9220):
                        raise RuntimeError("Chrome CDP did not start on port 9220.")

                    browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9220")
                    contexts = list(browser.contexts)
                    if not contexts:
                        raise RuntimeError("Real Chrome CDP connected but no browser context was available.")
                    context = contexts[0]
                    page = context.pages[0] if context.pages else await context.new_page()
                elif not using_cloak:
                    headless = browser_mode == "headless"
                    launch_options = AccountManager._persistent_context_launch_options(
                        headless=headless,
                        proxy_value=proxy,
                    )
                    browser_path = AccountManager._resolve_browser_path(p)
                    if browser_path:
                        launch_options["executable_path"] = browser_path
                    context = await p.chromium.launch_persistent_context(
                        user_data_dir=session_path,
                        **launch_options,
                    )
                    AccountManager._register_context_process(context)
                    page = context.pages[0] if context.pages else await context.new_page()

                await AccountManager._apply_browser_overrides(context, page)
                await AccountManager._apply_stealth_to_page(page)
                warmup_ok = await heavy_cookie_warmup(
                    page,
                    account_label,
                    logger,
                    browser_mode=browser_mode,
                    progress_fn=warmup_progress_callback,
                )
                if warmup_ok:
                    set_account_flag(account_label, "warmup_done", "True")
                    logger(f"[{account_label}] Cookie warm-up saved. Won't repeat next time.")
                    if callable(warmup_complete_callback):
                        warmup_complete_callback(account_label, True, "Cookie warm-up saved!")
                else:
                    logger(f"[{account_label}] Cookie warm-up failed. Generation will still work.")
                    if callable(warmup_complete_callback):
                        warmup_complete_callback(account_label, False, "Warm-up failed")
        except Exception as exc:
            if browser_mode == "cloakbrowser":
                logger(f"[{account_label}] CloakBrowser warm-up failed: {str(exc)[:80]}. Using CDP fallback.")
                await AccountManager._run_cookie_warmup_once(
                    account_label,
                    session_path,
                    proxy=proxy,
                    update_log_callback=update_log_callback,
                    forced_browser_mode="real_chrome",
                    warmup_progress_callback=warmup_progress_callback,
                    warmup_complete_callback=warmup_complete_callback,
                )
                return
            logger(f"[{account_label}] Cookie warm-up failed: {str(exc)[:100]}. Generation will still work.")
            if callable(warmup_complete_callback):
                warmup_complete_callback(account_label, False, "Warm-up failed")
        finally:
            # Export cookies BEFORE closing context (essential for Mac CloakBrowser).
            try:
                if context is not None:
                    await AccountManager._export_login_cookies(
                        context, session_path, account_label, logger=logger
                    )
            except Exception as cookie_export_exc:
                logger(f"[{account_label}] Cookie export warning: {str(cookie_export_exc)[:80]}")
            context_flushed = False
            try:
                if context is not None:
                    await AccountManager._close_context_and_flush(context, flush_delay=1)
                    context_flushed = True
                if browser is not None:
                    await browser.close()
            except Exception:
                pass

            # SQLite fallback after context close
            await AccountManager._post_close_sqlite_fallback(
                session_path, account_label, logger=logger
            )

            if context_flushed:
                logger(f"[{account_label}] Warm-up browser closed. Cookies updated.")
            AccountManager._unregister_context_process(context)
            locks = cleanup_session_locks(session_path)
            if locks and callable(update_log_callback):
                update_log_callback(f"[{account_label}] Cleaned {locks} lock file(s) after warmup.")

            if chrome_process is not None and chrome_process.poll() is None:
                try:
                    chrome_process.terminate()
                    chrome_process.wait(timeout=5)
                except Exception:
                    try:
                        chrome_process.kill()
                    except Exception:
                        pass
            process_tracker.unregister(getattr(chrome_process, "pid", None))

    # ═══════════════════════════════════════════════════════════════════════
    # Cookie export methods — 3-tier fallback for cross-platform reliability
    # ═══════════════════════════════════════════════════════════════════════

    # Critical Google auth cookie names used to verify export quality.
    _AUTH_COOKIE_NAMES = frozenset((
        "SID", "SSID", "HSID", "SAPISID", "APISID",
        "__Secure-1PSID", "__Secure-3PSID",
    ))
    # dola.com (ByteDance passport) session cookies — presence means dola is logged in.
    _DOLA_LOGIN_COOKIE_NAMES = frozenset((
        "sessionid", "sessionid_ss", "sid_tt", "sid_guard", "uid_tt",
    ))

    @staticmethod
    def _format_raw_cookies(raw_cookies):
        """Convert raw CDP/Playwright cookie dicts to a uniform export format.
        Fixes sameSite/secure mismatch that causes Playwright to reject cookies."""
        formatted = []
        for c in raw_cookies:
            same_site = c.get("sameSite", "Lax")
            secure = c.get("secure", False)
            # Fix: sameSite=None requires secure=True — otherwise Playwright rejects
            if same_site == "None" and not secure:
                same_site = "Lax"
            # Validate sameSite value
            if same_site not in ("Strict", "Lax", "None"):
                same_site = "Lax"
            cookie = {
                "name": c.get("name", ""),
                "value": c.get("value", ""),
                "domain": c.get("domain", ""),
                "path": c.get("path", "/"),
                "secure": secure,
                "httpOnly": c.get("httpOnly", False),
                "sameSite": same_site,
            }
            if c.get("expires", -1) > 0:
                cookie["expires"] = c["expires"]
            formatted.append(cookie)
        return formatted

    @staticmethod
    async def _cdp_get_all_cookies(context, logger=None, label=""):
        """
        Use Network.getAllCookies CDP command to get ALL cookies from the
        entire browser — not just cookies for visited pages.
        Falls back to context.cookies() if CDP session fails.
        """
        # Try CDP Network.getAllCookies first (gets ALL browser cookies)
        try:
            pages = list(getattr(context, "pages", []) or [])
            page = pages[0] if pages else await AccountManager._maybe_await(context.new_page())
            cdp_session = await page.context.new_cdp_session(page)
            result = await cdp_session.send("Network.getAllCookies")
            raw = result.get("cookies", [])
            await cdp_session.detach()
            if raw:
                if logger:
                    logger(f"[{label}] CDP getAllCookies: {len(raw)} cookies found.")
                return raw
        except Exception as e:
            if logger:
                logger(f"[{label}] CDP getAllCookies failed: {str(e)[:60]}, falling back to context.cookies()")

        # Fallback: context.cookies() (only returns visited-page cookies)
        raw = await AccountManager._maybe_await(context.cookies())
        return raw or []

    @staticmethod
    async def _export_cookies_method1_cdp(context, session_dir, label, logger=None):
        """
        Method 1 (BEST): Export ALL cookies via CDP Network.getAllCookies.
        Bypasses SQLite encryption and returns every cookie in the browser.
        MUST be called BEFORE context.close().
        """
        try:
            raw = await AccountManager._cdp_get_all_cookies(context, logger, label)

            if not raw or len(raw) < 5:
                if logger:
                    logger(f"[{label}] Method 1 (CDP): Only {len(raw)} cookies — too few.")
                return False

            formatted = AccountManager._format_raw_cookies(raw)

            cookies_json_path = os.path.join(session_dir, "exported_cookies.json")
            with open(cookies_json_path, "w", encoding="utf-8") as f:
                json.dump(formatted, f)

            google_cookies = [c for c in formatted if "google" in (c.get("domain") or "").lower()]
            auth_cookies = [c for c in formatted if c.get("name") in AccountManager._AUTH_COOKIE_NAMES]
            if logger:
                logger(f"[{label}] Method 1 (CDP): Exported {len(formatted)} cookies "
                       f"({len(google_cookies)} Google, {len(auth_cookies)} auth).")
            return len(auth_cookies) > 0
        except Exception as e:
            if logger:
                logger(f"[{label}] Method 1 (CDP) failed: {str(e)[:60]}")
            return False

    @staticmethod
    async def _export_cookies_method2_navigate(context, session_dir, label, logger=None):
        """
        Method 2 (FALLBACK): Navigate to key Google domains to trigger cookie
        setting, then use CDP Network.getAllCookies for complete dump.
        """
        try:
            pages = list(getattr(context, "pages", []) or [])
            page = pages[0] if pages else await AccountManager._maybe_await(context.new_page())

            critical_urls = [
                "https://accounts.google.com",
                "https://myaccount.google.com",
                "https://www.google.com",
                "https://labs.google.com",
                "https://labs.google/fx/tools/flow",
            ]
            for url in critical_urls:
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=10000)
                    await asyncio.sleep(1.5)
                except Exception:
                    pass

            # Use CDP getAllCookies for complete cookie dump
            raw = await AccountManager._cdp_get_all_cookies(context, logger, label)

            if not raw or len(raw) < 5:
                if logger:
                    logger(f"[{label}] Method 2 (Navigate): Only {len(raw)} cookies.")
                return False

            formatted = AccountManager._format_raw_cookies(raw)

            cookies_json_path = os.path.join(session_dir, "exported_cookies.json")
            with open(cookies_json_path, "w", encoding="utf-8") as f:
                json.dump(formatted, f)

            google_cookies = [c for c in formatted if "google" in (c.get("domain") or "").lower()]
            auth_cookies = [c for c in formatted if c.get("name") in AccountManager._AUTH_COOKIE_NAMES]
            if logger:
                logger(f"[{label}] Method 2 (Navigate+CDP): Exported {len(formatted)} cookies "
                       f"({len(google_cookies)} Google, {len(auth_cookies)} auth).")
            return len(auth_cookies) > 0
        except Exception as e:
            if logger:
                logger(f"[{label}] Method 2 (Navigate) failed: {str(e)[:60]}")
            return False

    @staticmethod
    async def _export_cookies_method3_sqlite(session_dir, label, logger=None):
        """
        Method 3 (LAST RESORT): Read Chrome's SQLite cookie DB directly.
        Fails on Macs where cookies are encrypted via Keychain.
        Called AFTER context.close() — doesn't need a live browser.
        """
        import sqlite3

        try:
            cookies_db = os.path.join(session_dir, "Default", "Network", "Cookies")
            if not os.path.exists(cookies_db):
                cookies_db = os.path.join(session_dir, "Default", "Cookies")
            if not os.path.exists(cookies_db):
                if logger:
                    logger(f"[{label}] Method 3 (SQLite): Cookies DB not found.")
                return False

            temp_db = cookies_db + ".export_temp"
            shutil.copy2(cookies_db, temp_db)

            conn = sqlite3.connect(temp_db)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT host_key, name, value, encrypted_value, path, "
                "expires_utc, is_secure, is_httponly, samesite FROM cookies"
            )

            cookies = []
            encrypted_count = 0
            for row in cursor.fetchall():
                host, name, value, encrypted_value = row[0], row[1], row[2], row[3]
                if not value and encrypted_value:
                    encrypted_count += 1
                    continue
                cookie = {
                    "name": name,
                    "value": value,
                    "domain": host,
                    "path": row[4] or "/",
                    "secure": bool(row[6]),
                    "httpOnly": bool(row[7]),
                    "sameSite": ["None", "Lax", "Strict"][row[8]] if isinstance(row[8], int) and row[8] < 3 else "None",
                }
                if row[5] and row[5] > 0:
                    chrome_epoch = 11644473600
                    expires_unix = (row[5] / 1000000) - chrome_epoch
                    if expires_unix > 0:
                        cookie["expires"] = expires_unix
                cookies.append(cookie)

            conn.close()
            try:
                os.remove(temp_db)
            except Exception:
                pass

            if encrypted_count > 0 and logger:
                logger(f"[{label}] Method 3: {encrypted_count} encrypted cookies skipped (Keychain protected).")

            if not cookies or len(cookies) < 3:
                if logger:
                    logger(f"[{label}] Method 3: Only {len(cookies)} readable cookies — encryption likely blocking.")
                return False

            cookies_json_path = os.path.join(session_dir, "exported_cookies.json")
            with open(cookies_json_path, "w", encoding="utf-8") as f:
                json.dump(cookies, f)

            google_cookies = [c for c in cookies if "google" in (c.get("domain") or "").lower()]
            if logger:
                logger(f"[{label}] Method 3 (SQLite): Exported {len(cookies)} cookies "
                       f"({len(google_cookies)} Google), skipped {encrypted_count} encrypted.")
            return True
        except Exception as e:
            if logger:
                logger(f"[{label}] Method 3 (SQLite) failed: {str(e)[:60]}")
            return False

    @staticmethod
    async def _export_login_cookies(context, session_dir, label, logger=None):
        """
        Master cookie export — tries CDP first, then Navigate+CDP.
        MUST be called BEFORE context.close().
        Method 3 (SQLite) is called separately after context close.
        """
        success = await AccountManager._export_cookies_method1_cdp(
            context, session_dir, label, logger
        )

        if not success:
            if logger:
                logger(f"[{label}] Trying Method 2 (navigate + export)...")
            success = await AccountManager._export_cookies_method2_navigate(
                context, session_dir, label, logger
            )

        # Verify auth cookie quality even if export "succeeded"
        cookies_json_path = os.path.join(session_dir, "exported_cookies.json")
        if success and os.path.exists(cookies_json_path):
            try:
                with open(cookies_json_path, "r", encoding="utf-8") as f:
                    exported = json.load(f)
                has_auth = any(
                    c.get("name") in AccountManager._AUTH_COOKIE_NAMES for c in exported
                )
                if not has_auth:
                    if logger:
                        logger(f"[{label}] No Google auth cookies (SID/SSID) found — trying Method 2 as backup...")
                    await AccountManager._export_cookies_method2_navigate(
                        context, session_dir, label, logger
                    )
                    # Re-check
                    with open(cookies_json_path, "r", encoding="utf-8") as f:
                        exported = json.load(f)
                    has_auth = any(
                        c.get("name") in AccountManager._AUTH_COOKIE_NAMES for c in exported
                    )
                    if has_auth and logger:
                        logger(f"[{label}] Auth cookies found after Method 2!")
                    elif not has_auth and logger:
                        logger(f"[{label}] Still no auth cookies — generation may require re-login.")
            except Exception:
                pass

        return success

    @staticmethod
    async def _post_close_sqlite_fallback(session_dir, label, logger=None):
        """
        Called AFTER context.close(). If no exported_cookies.json exists,
        attempts Method 3 (SQLite) as a last resort.
        """
        cookies_json_path = os.path.join(session_dir, "exported_cookies.json")
        if not os.path.exists(cookies_json_path):
            if logger:
                logger(f"[{label}] CDP export failed. Trying SQLite fallback...")
            await AccountManager._export_cookies_method3_sqlite(session_dir, label, logger)

        # Final verification
        if os.path.exists(cookies_json_path):
            try:
                with open(cookies_json_path, "r", encoding="utf-8") as f:
                    cookies = json.load(f)
                if logger:
                    logger(f"[{label}] exported_cookies.json: {len(cookies)} cookies on disk.")
            except Exception:
                pass
        else:
            if logger:
                logger(f"[{label}] NO exported cookies. Generation will rely on persistent context only.")

    # ═══════════════════════════════════════════════════════════════════════
    # Pure Chrome subprocess login — ZERO Playwright = ZERO automation detection
    # ═══════════════════════════════════════════════════════════════════════

    @staticmethod
    async def _detect_account_from_session(session_dir, logger=None):
        """Detect Google account email from Chrome session files on disk."""
        import sqlite3

        # Try Preferences JSON (most reliable — Chrome writes account_info here)
        prefs = os.path.join(session_dir, "Default", "Preferences")
        if os.path.exists(prefs):
            try:
                with open(prefs, "r", encoding="utf-8") as f:
                    data = json.load(f)
                account_info = data.get("account_info", [])
                if account_info and isinstance(account_info, list):
                    email = account_info[0].get("email", "")
                    if email and "@" in email:
                        return email
                signin = data.get("signin", {})
                allowed = signin.get("allowed")
                if allowed:
                    for key in ("account_id_migration_state",):
                        val = str(data.get(key, ""))
                        if "@" in val:
                            return val
            except Exception:
                pass

        # Try Login Data database
        login_db = os.path.join(session_dir, "Default", "Login Data")
        if os.path.exists(login_db):
            try:
                temp = login_db + ".detect_temp"
                shutil.copy2(login_db, temp)
                conn = sqlite3.connect(temp)
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT username_value FROM logins "
                    "WHERE origin_url LIKE '%google%' AND username_value LIKE '%@%' LIMIT 1"
                )
                row = cursor.fetchone()
                conn.close()
                try:
                    os.remove(temp)
                except Exception:
                    pass
                if row and row[0]:
                    return row[0]
            except Exception:
                pass

        # Try cookies for email patterns
        cookies_db = os.path.join(session_dir, "Default", "Network", "Cookies")
        if not os.path.exists(cookies_db):
            cookies_db = os.path.join(session_dir, "Default", "Cookies")
        if os.path.exists(cookies_db):
            try:
                temp = cookies_db + ".detect_temp"
                shutil.copy2(cookies_db, temp)
                conn = sqlite3.connect(temp)
                cursor = conn.cursor()
                cursor.execute("SELECT value FROM cookies WHERE host_key LIKE '%google%' AND value LIKE '%@%' LIMIT 10")
                for row in cursor.fetchall():
                    emails = EMAIL_RE.findall(str(row[0] or ""))
                    if emails:
                        conn.close()
                        try:
                            os.remove(temp)
                        except Exception:
                            pass
                        return AccountManager._pick_best_email(emails)
                conn.close()
                try:
                    os.remove(temp)
                except Exception:
                    pass
            except Exception:
                pass

        return None

    @staticmethod
    async def _get_cookies_via_live_cdp(cdp_port, logger=None, label=""):
        """
        Get ALL cookies from a LIVE Chrome process via Playwright connect_over_cdp.
        Cookies are decrypted in Chrome's memory — bypasses macOS Keychain encryption.
        Does NOT control the browser — only reads cookies.
        """
        try:
            if not AccountManager._is_cdp_endpoint_live(cdp_port):
                return []

            async with async_playwright() as pw:
                browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
                contexts = list(browser.contexts)
                if not contexts:
                    await browser.close()
                    return []

                context = contexts[0]
                cookies = []

                # Try CDP Network.getAllCookies (gets ALL browser cookies)
                try:
                    pages = list(getattr(context, "pages", []) or [])
                    page = pages[0] if pages else await AccountManager._maybe_await(context.new_page())
                    cdp_session = await page.context.new_cdp_session(page)

                    # Try Storage.getCookies first
                    try:
                        result = await cdp_session.send("Storage.getCookies")
                        cookies = result.get("cookies", [])
                    except Exception:
                        pass

                    # Fallback / supplement with Network.getAllCookies
                    if len(cookies) < 10:
                        try:
                            result = await cdp_session.send("Network.getAllCookies")
                            net_cookies = result.get("cookies", [])
                            if len(net_cookies) > len(cookies):
                                cookies = net_cookies
                        except Exception:
                            pass

                    await cdp_session.detach()
                except Exception as e:
                    if logger:
                        logger(f"[{label}] CDP cookie read error: {str(e)[:50]}")

                # Fallback to context.cookies()
                if len(cookies) < 10:
                    try:
                        ctx_cookies = await AccountManager._maybe_await(context.cookies())
                        if ctx_cookies and len(ctx_cookies) > len(cookies):
                            cookies = ctx_cookies
                    except Exception:
                        pass

                # Disconnect (does NOT close Chrome — we connected, not launched)
                try:
                    await browser.close()
                except Exception:
                    pass

                return cookies

        except Exception as e:
            if logger:
                logger(f"[{label}] Live CDP connection failed: {str(e)[:60]}")
            return []

    @staticmethod
    async def _navigate_chrome_via_cdp(cdp_port, url, logger=None, label=""):
        """Navigate a live Chrome tab to a URL via Playwright CDP connection."""
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
                contexts = list(browser.contexts)
                if contexts:
                    pages = list(getattr(contexts[0], "pages", []) or [])
                    page = pages[0] if pages else await contexts[0].new_page()
                    await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    if logger:
                        logger(f"[{label}] Navigated to {url}")
                try:
                    await browser.close()
                except Exception:
                    pass
                return True
        except Exception as e:
            if logger:
                logger(f"[{label}] CDP navigate error: {str(e)[:60]}")
            return False

    @staticmethod
    async def _monitor_chrome_and_extract_cookies(
        chrome_process, cdp_port, session_dir, label, logger=None, should_stop=None,
        login_target="flow",
    ):
        """
        Monitor Chrome process. Periodically extract cookies via live CDP.
        After Google login is detected, auto-navigates to the target site to set
        all cookies: labs.google/flow for "flow", or dola.com for "dola" (which
        auto-logs-in via the active Google session). Keeps updating until Chrome
        closes. Returns (cookies_exported: bool, detected_email: str or None).
        """
        is_dola = str(login_target or "flow").strip().lower() == "dola"
        post_login_url = "https://www.dola.com/" if is_dola else "https://labs.google/fx/tools/flow"
        site_label = "dola.com" if is_dola else "Flow"

        cookies_saved = False
        detected_email = None
        last_cookie_count = 0
        login_detected = False
        flow_visited = False

        # Wait for CDP to become available
        await asyncio.sleep(3)

        while chrome_process.poll() is None:
            # Check for app shutdown
            if callable(should_stop) and should_stop():
                if logger:
                    logger(f"[{label}] Login cancelled — terminating Chrome.")
                try:
                    chrome_process.terminate()
                    chrome_process.wait(timeout=5)
                except Exception:
                    try:
                        chrome_process.kill()
                    except Exception:
                        pass
                break

            # Extract cookies from live Chrome
            try:
                raw_cookies = await AccountManager._get_cookies_via_live_cdp(
                    cdp_port, logger=None, label=label
                )

                if raw_cookies:
                    auth_cookies = [
                        c for c in raw_cookies
                        if c.get("name", "") in AccountManager._AUTH_COOKIE_NAMES
                    ]
                    google_cookies = [
                        c for c in raw_cookies
                        if "google" in (c.get("domain") or "").lower()
                    ]
                    if is_dola:
                        site_cookies = [
                            c for c in raw_cookies
                            if "dola.com" in (c.get("domain") or "").lower()
                            and c.get("name", "") in AccountManager._DOLA_LOGIN_COOKIE_NAMES
                        ]
                    else:
                        site_cookies = [
                            c for c in raw_cookies
                            if "labs.google" in (c.get("domain") or "").lower()
                        ]

                    # Log only when cookie count changes
                    if len(raw_cookies) != last_cookie_count:
                        last_cookie_count = len(raw_cookies)
                        if logger:
                            logger(f"[{label}] Live CDP: {len(raw_cookies)} cookies "
                                   f"({len(google_cookies)} Google, {len(auth_cookies)} auth)")

                    # Try to detect email from cookie values
                    if not detected_email:
                        for c in raw_cookies:
                            val = str(c.get("value") or "")
                            emails = EMAIL_RE.findall(val)
                            if emails:
                                detected_email = AccountManager._pick_best_email(emails)
                                if detected_email and logger:
                                    logger(f"[AUTO] Detected logged-in Google account: {detected_email}")
                                break

                    # Login detected — auto-navigate to the target site
                    if len(auth_cookies) >= 2 and not login_detected:
                        login_detected = True
                        if logger:
                            logger(f"[{label}] Google login detected! "
                                   f"({len(auth_cookies)} auth cookies)")
                            logger(f"[{label}] Navigating to {site_label} to set all cookies"
                                   + (" (dola auto-logs-in via Google)..." if is_dola else "..."))
                        await AccountManager._navigate_chrome_via_cdp(
                            cdp_port, post_login_url,
                            logger=logger, label=label,
                        )
                        # dola auto-login runs its OAuth after landing — give it time.
                        await asyncio.sleep(8 if is_dola else 5)
                        # Re-fetch cookies after navigation
                        continue

                    # Check if the target-site login cookies appeared
                    if login_detected and not flow_visited and len(site_cookies) >= 1:
                        flow_visited = True
                        if logger:
                            logger(f"[{label}] {site_label} login cookies set! "
                                   f"({len(site_cookies)} {site_label} cookies)")
                            logger(f"[{label}] You can close Chrome now.")

                    # Save cookies every poll when we have auth
                    if len(auth_cookies) >= 2:
                        formatted = AccountManager._format_raw_cookies(raw_cookies)
                        cookies_json_path = os.path.join(session_dir, "exported_cookies.json")
                        with open(cookies_json_path, "w", encoding="utf-8") as f:
                            json.dump(formatted, f)

                        if not cookies_saved:
                            cookies_saved = True
                            if not flow_visited and logger:
                                logger(f"[{label}] Auth cookies saved. Waiting for Flow cookies...")
            except Exception:
                pass

            # Poll every 3 seconds
            await asyncio.sleep(3)

        # Chrome closed — final status
        if logger:
            if not login_detected:
                logger(f"[{label}] No Google login detected. Please try again.")
            elif not flow_visited:
                logger(f"[{label}] {site_label} page was not visited. Login may not persist for generation.")

        return cookies_saved, detected_email

    @staticmethod
    async def _export_cookies_via_playwright_headless(session_dir, label, logger=None):
        """
        Briefly launch Playwright persistent context HEADLESS to read ALL cookies
        via CDP Network.getAllCookies. Chrome is already closed, so no conflict.
        Closes Playwright immediately after reading.
        """
        try:
            chrome_path = AccountManager._find_chrome_path()
            if not chrome_path:
                if logger:
                    logger(f"[{label}] Chrome not found for headless cookie read.")
                return False

            cleanup_session_locks(session_dir)

            async with async_playwright() as pw:
                ctx = await pw.chromium.launch_persistent_context(
                    session_dir,
                    executable_path=chrome_path,
                    headless=True,
                    args=[
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--disable-blink-features=AutomationControlled",
                    ],
                    ignore_default_args=["--enable-automation"],
                )

                cookies = []
                try:
                    pages = list(getattr(ctx, "pages", []) or [])
                    page = pages[0] if pages else await AccountManager._maybe_await(ctx.new_page())
                    cdp_session = await page.context.new_cdp_session(page)
                    result = await cdp_session.send("Network.getAllCookies")
                    cdp_cookies = result.get("cookies", [])
                    await cdp_session.detach()
                    if cdp_cookies:
                        cookies = cdp_cookies
                        if logger:
                            logger(f"[{label}] Headless CDP getAllCookies: {len(cookies)} cookies.")
                except Exception as e:
                    if logger:
                        logger(f"[{label}] Headless CDP getAllCookies failed: {str(e)[:50]}")

                if len(cookies) < 5:
                    try:
                        ctx_cookies = await AccountManager._maybe_await(ctx.cookies())
                        if ctx_cookies and len(ctx_cookies) > len(cookies):
                            cookies = ctx_cookies
                    except Exception:
                        pass

                try:
                    await AccountManager._close_context_and_flush(ctx, flush_delay=1)
                except Exception:
                    pass

            cleanup_session_locks(session_dir)

            if not cookies or len(cookies) < 5:
                if logger:
                    logger(f"[{label}] Headless cookie read: only {len(cookies)} cookies.")
                return False

            formatted = AccountManager._format_raw_cookies(cookies)

            cookies_json_path = os.path.join(session_dir, "exported_cookies.json")
            with open(cookies_json_path, "w", encoding="utf-8") as f:
                json.dump(formatted, f)

            google_cookies = [c for c in formatted if "google" in (c.get("domain") or "").lower()]
            auth_cookies = [c for c in formatted if c.get("name") in AccountManager._AUTH_COOKIE_NAMES]
            if logger:
                logger(f"[{label}] Headless read: {len(formatted)} cookies "
                       f"({len(google_cookies)} Google, {len(auth_cookies)} auth).")

            return len(auth_cookies) > 0

        except Exception as e:
            if logger:
                logger(f"[{label}] Headless cookie read failed: {str(e)[:60]}")
            return False

    @staticmethod
    async def _pure_chrome_login(
        session_dir, account_hint, update_log_callback=None, should_stop=None, proxy=None,
        login_target="flow",
    ):
        """
        Launch Chrome as a PURE subprocess WITH --remote-debugging-port.
        ZERO Playwright for login = ZERO automation detection by Google.
        Cookies are extracted from Chrome's LIVE memory via CDP while user
        logs in — this bypasses macOS Keychain cookie encryption entirely.
        After Chrome closes, falls back to headless read / SQLite if needed.
        Returns detected_email or None.
        """
        chrome_path = AccountManager._find_chrome_path()
        if not chrome_path:
            raise RuntimeError("Google Chrome not found! Please install Chrome.")

        is_mac = platform.system() == "Darwin"
        label = account_hint or os.path.basename(session_dir)
        cdp_port = random.randint(9220, 9300)

        if update_log_callback:
            update_log_callback(f"[{label}] Chrome path: {chrome_path}")
            update_log_callback(f"[{label}] Session dir: {session_dir}")
            update_log_callback(f"[{label}] CDP port: {cdp_port}")

        # Clean lock files before launch
        cleanup_session_locks(session_dir)

        # Kill anything already on the CDP port (Mac + Windows)
        AccountManager._kill_chrome_on_port(cdp_port)
        if is_mac:
            await asyncio.sleep(2)  # Mac needs extra time after killing

        chrome_args = [
            chrome_path,
            f"--user-data-dir={session_dir}",
            f"--remote-debugging-port={cdp_port}",
            "--remote-debugging-address=127.0.0.1",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-default-apps",
            "--disable-extensions",
            "--disable-sync",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            # HIDE the automation flag — without this Google rejects the sign-in with
            # 'this browser or app may not be secure' (accounts.google.com/v3/signin/rejected).
            # This is the one flag that makes the manual Google login work.
            "--disable-blink-features=AutomationControlled",
            "--window-size=1920,1080",
            AccountManager.LOGIN_START_URLS.get(str(login_target or "flow"), "https://accounts.google.com"),
        ]
        if proxy:
            # Chromium can't use SOCKS5 with username/password — route
            # through local HTTP-to-SOCKS5 bridge if needed.
            try:
                from src.core.proxy_bridge import get_or_create_bridge
                proxy_for_chrome = get_or_create_bridge(proxy)
            except Exception:
                proxy_for_chrome = proxy
            chrome_args.append(f"--proxy-server={proxy_for_chrome}")
            if update_log_callback and proxy_for_chrome != proxy:
                update_log_callback(f"[{label}] SOCKS5 bridge active: {proxy_for_chrome}")

        if update_log_callback:
            update_log_callback(f"[{label}] Launching pure Chrome with CDP port {cdp_port}...")
            if str(login_target or "flow") == "dola":
                update_log_callback(f"[{label}] Log in to your Google account fully, then CLOSE Chrome. dola.com will auto-login later using this Google session.")
            else:
                update_log_callback(f"[{label}] Please log in to Google, then CLOSE Chrome when done.")

        creationflags = 0
        popen_kwargs = {}
        if platform.system() == "Windows":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            popen_kwargs["stdout"] = subprocess.DEVNULL
            popen_kwargs["stderr"] = subprocess.DEVNULL

        chrome_process = subprocess.Popen(
            chrome_args,
            creationflags=creationflags,
            **popen_kwargs,
        )
        process_tracker.register(getattr(chrome_process, "pid", None))

        if update_log_callback:
            update_log_callback(f"[{label}] Chrome opened (PID: {chrome_process.pid}).")

        # Wait for Chrome to start — Mac needs longer
        startup_wait = 5 if is_mac else 3
        await asyncio.sleep(startup_wait)

        # Verify CDP endpoint is alive with retries
        cdp_ready = False
        for attempt in range(1, 4):
            if AccountManager._is_cdp_endpoint_live(cdp_port):
                cdp_ready = True
                if update_log_callback:
                    update_log_callback(f"[{label}] CDP endpoint ready on port {cdp_port}.")
                break
            if update_log_callback:
                update_log_callback(f"[{label}] CDP connection attempt {attempt}/3 — waiting...")
            await asyncio.sleep(3)

        if not cdp_ready:
            if update_log_callback:
                update_log_callback(f"[{label}] CDP endpoint not responding on port {cdp_port}. "
                                    "Will try again during monitoring...")
            # Don't abort — Chrome is still running, CDP might come up later

        # Monitor Chrome and extract cookies from LIVE process via CDP
        cookies_exported, detected_email = await AccountManager._monitor_chrome_and_extract_cookies(
            chrome_process, cdp_port, session_dir, label,
            logger=update_log_callback,
            should_stop=should_stop,
            login_target=login_target,
        )

        process_tracker.unregister(getattr(chrome_process, "pid", None))

        if update_log_callback:
            update_log_callback(f"[{label}] Chrome closed by user.")

        # Give Chrome time to flush to disk
        await asyncio.sleep(2)
        cleanup_session_locks(session_dir)

        # Drop the stale CloakBrowser cookie cache so the runner re-seeds from THIS fresh
        # Google login (the dola runner is cache-first: _dola_cloak_cookies.json is reused
        # and never re-exported, so a fresh Google login must invalidate the old cache).
        try:
            _cc = os.path.join(session_dir, "_dola_cloak_cookies.json")
            if os.path.isfile(_cc):
                os.remove(_cc)
                if update_log_callback:
                    update_log_callback(f"[{label}] cleared stale dola cloak cookie cache")
        except Exception:
            pass

        # Try to detect account from session files if not detected from live cookies
        if not detected_email:
            detected_email = await AccountManager._detect_account_from_session(
                session_dir, logger=update_log_callback
            )
            if detected_email and update_log_callback:
                update_log_callback(f"[AUTO] Detected account from session files: {detected_email}")

        if not detected_email and update_log_callback:
            update_log_callback(f"[{label}] Could not auto-detect account. Make sure you logged into Google.")

        export_label = detected_email or label

        # If live CDP extraction failed, try post-close fallbacks
        if not cookies_exported:
            if update_log_callback:
                update_log_callback(f"[{export_label}] Live CDP export missed. Trying headless Playwright read...")

            exported = await AccountManager._export_cookies_via_playwright_headless(
                session_dir, export_label, logger=update_log_callback
            )
            if not exported:
                if update_log_callback:
                    update_log_callback(f"[{export_label}] Headless read failed. Trying SQLite...")
                await AccountManager._export_cookies_method3_sqlite(
                    session_dir, export_label, logger=update_log_callback
                )

        # Final verification
        cookies_json_path = os.path.join(session_dir, "exported_cookies.json")
        if os.path.exists(cookies_json_path):
            try:
                with open(cookies_json_path, "r", encoding="utf-8") as f:
                    cookies = json.load(f)
                auth = [c for c in cookies if c.get("name") in AccountManager._AUTH_COOKIE_NAMES]
                if update_log_callback:
                    update_log_callback(f"[{export_label}] exported_cookies.json: "
                                        f"{len(cookies)} cookies ({len(auth)} auth) on disk.")
            except Exception:
                pass
        else:
            if update_log_callback:
                update_log_callback(f"[{export_label}] NO exported cookies. Generation will rely on persistent context only.")

        return detected_email

    @staticmethod
    async def login_and_save_session(
        account_name: str,
        update_log_callback=None,
        download_progress_callback=None,
        download_complete_callback=None,
        session_saved_callback=None,
        warmup_progress_callback=None,
        warmup_complete_callback=None,
        should_stop=None,
        proxy=None,
        login_target="flow",
    ):
        """
        Launches a visible browser for the user to login.
        Saves the persistent browser context to the data directory so future
        headless runs can stay logged in.

        login_target: "flow" (Google/labs.google, default) or "dola" (dola.com —
        opens dola.com so the user signs in via Google; the profile ends up with
        both dola.com and google cookies).

        Mac / Real Chrome: Uses PURE subprocess Chrome (zero Playwright)
        to avoid Google detecting automation hooks.
        Windows CloakBrowser: Uses CloakBrowser persistent context.
        """
        account_hint = str(account_name or "").strip()
        temp_name = AccountManager._safe_session_dir_name(account_hint or f"account_{int(time.time())}")
        session_dir = os.path.join(DATA_DIR, temp_name)
        os.makedirs(session_dir, exist_ok=True)
        detected_email = None

        if update_log_callback:
            visible_name = account_hint or temp_name
            update_log_callback(f"[{visible_name}] Launching browser for manual login...")

        # ══════════════════════════════════════════════════════════════════
        # Login: ALWAYS pure Chrome subprocess on ALL platforms.
        # No CloakBrowser, no Playwright for login — zero automation detection.
        # Generation uses CloakBrowser (browser-per-slot or multi-tab).
        # ══════════════════════════════════════════════════════════════════
        if update_log_callback:
            update_log_callback(
                f"[{account_hint or temp_name}] Using pure Chrome for login "
                "(zero automation detection). CloakBrowser will be used for generation."
            )

        detected_email = await AccountManager._pure_chrome_login(
            session_dir, account_hint,
            update_log_callback=update_log_callback,
            should_stop=should_stop,
            proxy=proxy,
            login_target=login_target,
        )

        # ══════════════════════════════════════════════════════════════════
        # Post-login: rename session dir, diagnostics, warmup
        # ══════════════════════════════════════════════════════════════════
        final_name = str(detected_email or account_hint or temp_name).strip()
        final_name = final_name or temp_name

        target_dir_name = AccountManager._safe_session_dir_name(final_name)
        target_session_dir = os.path.join(DATA_DIR, target_dir_name)
        final_session_path = session_dir

        if os.path.abspath(target_session_dir) != os.path.abspath(session_dir):
            if not os.path.exists(target_session_dir):
                try:
                    os.rename(session_dir, target_session_dir)
                    final_session_path = target_session_dir
                except Exception:
                    final_session_path = session_dir
            else:
                suffix_path = f"{target_session_dir}_{int(time.time())}"
                try:
                    os.rename(session_dir, suffix_path)
                    final_session_path = suffix_path
                except Exception:
                    final_session_path = session_dir

        if update_log_callback:
            update_log_callback(f"[{final_name}] Login browser closed. Session saved to disk.")
            cookie_file = AccountManager._find_cookie_file(final_session_path)
            if cookie_file:
                normalized_cookie_file = os.path.normpath(cookie_file)
                default_network_cookie = os.path.normpath(
                    os.path.join(final_session_path, "Default", "Network", "Cookies")
                )
                if normalized_cookie_file == default_network_cookie:
                    update_log_callback(f"[{final_name}] Cookies file verified on disk.")
                else:
                    update_log_callback(f"[{final_name}] Cookies found at alternate path.")
            else:
                update_log_callback(
                    f"[{final_name}] Warning: no cookies file found after close. Login may not persist in headless mode."
                )
            update_log_callback(f"[{final_name}] Session saved to {final_session_path}")
            if os.path.isdir(final_session_path):
                top_items = sorted(os.listdir(final_session_path))[:15]
                update_log_callback(f"[DEBUG:{final_name}] Session root contents: {top_items}")
                default_dir = os.path.join(final_session_path, "Default")
                if os.path.isdir(default_dir):
                    default_items = sorted(os.listdir(default_dir))[:20]
                    update_log_callback(f"[DEBUG:{final_name}] Default/ contents: {default_items}")
                else:
                    update_log_callback(f"[DEBUG:{final_name}] Default/ dir NOT FOUND after login!")
        if callable(session_saved_callback):
            session_saved_callback(final_name, final_session_path, detected_email or "")
        if not (callable(should_stop) and should_stop()):
            await AccountManager._run_cookie_warmup_once(
                final_name,
                final_session_path,
                proxy=proxy,
                update_log_callback=update_log_callback,
                warmup_progress_callback=warmup_progress_callback,
                warmup_complete_callback=warmup_complete_callback,
            )
        return final_name, final_session_path, detected_email or ""
