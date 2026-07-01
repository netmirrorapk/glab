"""
HTTP Mode: Shared Browser Fetch — reCAPTCHA + API in ONE browser context

Architecture:
  1 CloakBrowser per account  -> page.evaluate(fetch()) for ALL API calls
  N BrowserFetchWorkers       -> queue fetch() calls to the shared browser
  reCAPTCHA + fetch in SAME evaluate() call = same browser context = valid

RAM usage:
  1 browser per account = ~300 MB (fixed)
  Each worker = ~0 MB (just a Python coroutine)
  20 workers on 1 account = ~300 MB total vs ~6 GB with browser-per-slot
"""

import asyncio
import json
import os
import re
import time
import uuid
import random
import platform
from typing import Optional

try:
    import aiohttp
except ImportError:
    aiohttp = None

try:
    import aiofiles
except ImportError:
    aiofiles = None

from src.core.app_paths import get_sessions_dir
from src.core.process_tracker import cleanup_session_locks
from src.core.cloakbrowser_support import load_cloakbrowser_api
from src.core.recaptcha_mainworld import get_recaptcha_token_mainworld
from src.db.db_manager import (
    get_accounts,
    get_all_jobs,
    get_setting,
    get_int_setting,
    get_bool_setting,
    get_output_directory,
    update_job_status,
    update_job_runtime_state,
)

DATA_DIR = str(get_sessions_dir())


def _parse_api_error_string(error_str: str) -> str:
    """Parse JS-side 'HTTP {status}: {body}' error into a clear message."""
    import re as _re
    m = _re.match(r"HTTP\s+(\d+):\s*(.*)", error_str, _re.DOTALL)
    if not m:
        return error_str  # not an HTTP error, return as-is

    status = int(m.group(1))
    body = m.group(2).strip()
    text_lower = body.lower()

    if status == 403:
        if "recaptcha" in text_lower:
            return "⛔ reCAPTCHA Score Too Low — Google rejected the token (score below threshold). Tab may need reload."
        if "quota" in text_lower or "rate" in text_lower:
            return "⛔ Rate Limited (403) — Account quota exceeded or too many requests."
        return f"⛔ Forbidden (403) — {body[:150] or 'Google rejected the request.'}"

    if status == 401:
        return "🔑 Access Token Expired (401) — Session expired, need fresh auth."

    if status == 400:
        if "recaptcha" in text_lower:
            return "⚠️ reCAPTCHA Token Expired/Invalid (400) — Token was stale or malformed."
        if any(k in text_lower for k in ("expired", "token_expired", "invalid_token")):
            return "🔑 Access Token Expired (400) — Auth session needs refresh."
        if any(k in text_lower for k in ("project", "project_id", "project not found")):
            return "📁 Invalid Project ID (400) — Project not found or was deleted."
        if any(k in text_lower for k in (
            "safety", "blocked", "policy", "filter", "harmful",
            "inappropriate", "violat", "content_filter", "responsible_ai",
        )):
            return "🚫 Prompt Blocked by Content Filter (400) — Google's safety filter rejected this prompt."
        if any(k in text_lower for k in ("invalid", "malformed", "parse", "field")):
            return f"❌ Malformed Request (400) — {body[:150]}"
        return f"⚠️ Bad Request (400) — {body[:200] or 'Unknown cause.'}"

    if status == 429:
        return "🕐 Rate Limited (429) — Too many requests. Account needs cooldown."

    if status >= 500:
        return f"🔧 Google Server Error ({status}) — Temporary issue, will retry."

    return error_str


# ═══════════════════════════════════════════════════════════════════════════
# Model / aspect ratio resolvers — maps display names to API enum values
# ═══════════════════════════════════════════════════════════════════════════

def _resolve_image_model(model):
    lower = str(model or "").lower()
    # HARBOR_SEAL = Nano Banana 2 Lite (added 2026-07-01 from HAR capture).
    # "lite" check must come before generic "nano banana" match below.
    if "lite" in lower and "nano banana" in lower:
        return "HARBOR_SEAL"
    if "nano banana pro" in lower:
        return "GEM_PIX_2"
    if "nano banana" in lower:
        return "NARWHAL"
    # ALL Imagen models (including Imagen 4) map to NARWHAL — same as bot_engine
    if "imagen" in lower:
        return "NARWHAL"
    if model and model == model.upper():
        return model
    return "NARWHAL"


def _resolve_image_ratio(ratio):
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


def _resolve_video_model(model, video_model=""):
    source = str(video_model or model or "").strip().lower()
    if not source:
        return "veo_3_1_t2v_fast_ultra"
    if "lite" in source:
        return "veo_3_1_t2v_lite"
    if "lower pri" in source or "relaxed" in source:
        return "veo_3_1_t2v_fast_ultra_relaxed"
    if "quality" in source:
        return "veo_3_1_t2v"
    if "_" in source:
        return source
    return "veo_3_1_t2v_fast_ultra"


def _resolve_video_ratio(ratio):
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


# ═══════════════════════════════════════════════════════════════════════════
# SharedBrowser — 1 browser per account, shared by all workers
# ═══════════════════════════════════════════════════════════════════════════

class SharedBrowser:
    """
    Single shared browser per account. Workers call page.evaluate(fetch())
    to make API requests — reCAPTCHA + fetch in the SAME browser context.
    No separate HTTP clients needed.
    """

    def __init__(self, account_name, session_path, cookies_json_path, log_fn, project_id=None, headless=True, stealth_visible=False, proxy=None):
        self.account_name = account_name
        self._session_path = session_path
        self._cookies_json = cookies_json_path
        self._log = log_fn
        self._headless = headless
        self._stealth_visible = stealth_visible
        self._proxy = str(proxy or "").strip()
        self._context = None
        self._page = None
        self._project_id = project_id
        self._jobs_since_reload = 0
        self._last_page_reload = 0
        self._bearer_token = None
        self._bearer_token_time = 0

    async def start(self, playwright_instance):
        """Start shared browser and navigate to Flow page."""
        self._log(f"[SharedBrowser:{self.account_name}] Starting...")

        try:
            success = await self._launch_browser(playwright_instance)
            if not success:
                return False

            pages = list(getattr(self._context, "pages", []) or [])
            self._page = pages[0] if pages else await self._context.new_page()

            # Intercept requests to capture Bearer token
            self._setup_request_interception()

            await self._page.goto(
                "https://labs.google/fx/tools/flow",
                wait_until="load",
                timeout=30000,
            )
            await asyncio.sleep(5)

            # Warmup: humanized scroll to improve reCAPTCHA score
            await self._warmup_page()

            # Extract project ID
            if self._project_id:
                self._log(f"[SharedBrowser:{self.account_name}] Using cached project ID: {self._project_id}")
            else:
                self._project_id = await self._extract_project_id()

            if not self._project_id:
                self._log(f"[SharedBrowser:{self.account_name}] No project ID found. Check login.")
                return False

            # Navigate to the project URL so reCAPTCHA runs in correct context
            current_url = self._page.url
            if self._project_id and f"/project/{self._project_id}" not in current_url:
                try:
                    await self._page.goto(
                        f"https://labs.google/fx/tools/flow/project/{self._project_id}",
                        wait_until="load", timeout=15000,
                    )
                    await asyncio.sleep(3)
                    self._log(f"[SharedBrowser:{self.account_name}] Navigated to project URL.")
                except Exception as e:
                    self._log(f"[SharedBrowser:{self.account_name}] Project URL nav warn: {str(e)[:60]}")

            # Verify reCAPTCHA is loaded
            recap_ok = await self._check_recaptcha()
            if not recap_ok:
                self._log(f"[SharedBrowser:{self.account_name}] reCAPTCHA not loaded on page.")

            # Capture Bearer token by triggering an API call
            if not self._bearer_token:
                await self._trigger_bearer_capture()

            self._log(
                f"[SharedBrowser:{self.account_name}] Ready! "
                f"Project: {self._project_id}, reCAPTCHA: {'yes' if recap_ok else 'NO'}, "
                f"Bearer: {'yes' if self._bearer_token else 'NO'}"
            )
            return True

        except Exception as e:
            self._log(f"[SharedBrowser:{self.account_name}] Start failed: {str(e)[:80]}")
            return False

    async def _launch_browser(self, playwright_instance):
        """Launch CloakBrowser (preferred) or Playwright persistent context."""
        # Debug: log cookie file status
        self._log(f"[SharedBrowser:{self.account_name}] Session path: {self._session_path}")
        self._log(
            f"[SharedBrowser:{self.account_name}] Cookies JSON: {self._cookies_json} "
            f"(exists: {os.path.exists(self._cookies_json)})"
        )

        cookies = self._load_exported_cookies()
        self._log(f"[SharedBrowser:{self.account_name}] Loaded {len(cookies)} cookies from JSON file.")

        # Try CloakBrowser
        try:
            cloak_api = load_cloakbrowser_api()
            cloak_persistent = cloak_api.get("persistent_async")

            if cloak_api.get("available") and cloak_persistent:
                seed = random.randint(10000, 99999)
                # Use separate profile to avoid lock conflicts with main browser
                profile = self._session_path + "_shared_browser"
                os.makedirs(profile, exist_ok=True)
                self._clean_locks(profile)

                # Copy trust data from main profile (reCAPTCHA score, localStorage)
                self._copy_profile_trust_data(self._session_path, profile)

                self._log(f"[SharedBrowser:{self.account_name}] Profile path: {profile}")

                self._log(f"[SharedBrowser:{self.account_name}] Headless: {self._headless}, Stealth: {self._stealth_visible}")
                cloak_args = [f"--fingerprint={seed}"]
                if self._stealth_visible:
                    cloak_args.extend(["--window-position=-3000,-3000", "--window-size=800,600"])

                # Proxy support — resolve SOCKS5 auth via bridge if needed
                resolved_proxy = self._resolve_proxy()
                if resolved_proxy:
                    cloak_args.append(f"--proxy-server={resolved_proxy}")
                    self._log(f"[SharedBrowser:{self.account_name}] Proxy: {resolved_proxy}")

                self._context = await cloak_persistent(
                    profile, headless=self._headless,
                    args=cloak_args, humanize=True,
                )

                # Import cookies — CRITICAL for login session
                cookies_imported = await self._import_cookies(cookies)

                # Check actual cookie count in browser
                try:
                    actual = await self._context.cookies()
                    actual_count = len(actual)
                except Exception:
                    actual_count = cookies_imported

                self._log(
                    f"[SharedBrowser:{self.account_name}] CloakBrowser started. "
                    f"Imported: {cookies_imported}, Browser has: {actual_count} cookies."
                )

                if actual_count == 0:
                    self._log(
                        f"[SharedBrowser:{self.account_name}] "
                        "NO cookies in browser! Login session missing. "
                        f"Check: {self._cookies_json}"
                    )

                return True
        except Exception as e:
            self._log(f"[SharedBrowser:{self.account_name}] CloakBrowser not available: {str(e)[:120]}")

        # Fallback: Playwright
        try:
            self._clean_locks(self._session_path)
            self._log(f"[SharedBrowser:{self.account_name}] Profile path: {self._session_path}")

            pw_args = ["--disable-gpu", "--no-sandbox", "--disable-blink-features=AutomationControlled"]
            resolved_proxy = self._resolve_proxy()
            if resolved_proxy:
                pw_args.append(f"--proxy-server={resolved_proxy}")
                self._log(f"[SharedBrowser:{self.account_name}] Proxy: {resolved_proxy}")

            self._context = await playwright_instance.chromium.launch_persistent_context(
                self._session_path, headless=self._headless,
                args=pw_args,
                ignore_default_args=["--enable-automation"],
            )

            cookies_imported = await self._import_cookies(cookies)

            try:
                actual = await self._context.cookies()
                actual_count = len(actual)
            except Exception:
                actual_count = cookies_imported

            self._log(
                f"[SharedBrowser:{self.account_name}] Playwright started. "
                f"Imported: {cookies_imported}, Browser has: {actual_count} cookies."
            )
            return True
        except Exception as e:
            self._log(f"[SharedBrowser:{self.account_name}] Browser launch failed: {str(e)[:60]}")
            return False

    def _resolve_proxy(self):
        """Resolve proxy URL — handle SOCKS5 auth via local bridge if needed."""
        if not self._proxy:
            return None
        try:
            from src.core.proxy_bridge import get_or_create_bridge
            resolved = get_or_create_bridge(self._proxy)
            return resolved or None
        except ImportError:
            return self._proxy
        except Exception:
            return self._proxy

    def _setup_request_interception(self):
        """Intercept browser requests to capture Authorization Bearer token."""
        def _on_request(request):
            if "aisandbox-pa.googleapis.com" in request.url and request.method == "POST":
                auth = request.headers.get("authorization", "")
                if auth.startswith("Bearer "):
                    self._bearer_token = auth
                    self._bearer_token_time = time.time()
                    self._log(f"[SharedBrowser:{self.account_name}] Bearer token captured (len={len(auth)})")
        self._page.on("request", _on_request)

    async def _trigger_bearer_capture(self):
        """Trigger a fetch() from the browser to capture the Bearer token."""
        self._log(f"[SharedBrowser:{self.account_name}] Triggering API request to capture Bearer token...")
        try:
            status = await self._page.evaluate("""
                async () => {
                    try {
                        const r = await fetch(
                            'https://aisandbox-pa.googleapis.com/v1/credits',
                            { method: 'GET', credentials: 'include' }
                        );
                        return r.status;
                    } catch(e) { return e.message; }
                }
            """)
            self._log(f"[SharedBrowser:{self.account_name}] Credits API: {status}")
            await asyncio.sleep(2)
        except Exception as e:
            self._log(f"[SharedBrowser:{self.account_name}] Credits call error: {str(e)[:50]}")

        if self._bearer_token:
            self._log(f"[SharedBrowser:{self.account_name}] Bearer token ready!")
            return

        # Fallback: dummy generation POST
        try:
            # Try main-world reCAPTCHA first
            recap = None
            try:
                recap = await get_recaptcha_token_mainworld(
                    self._page, "generate", self._log
                )
            except Exception:
                pass
            # Fall back to inline evaluate if main-world failed
            if not recap:
                recap = await self._page.evaluate("""
                    async () => {
                        try {
                            const scripts = document.querySelectorAll("script[src*='recaptcha'][src*='render=']");
                            let sk = null;
                            for (const s of scripts) {
                                const m = s.src.match(/render=([^&]+)/);
                                if (m && m[1] !== 'explicit') { sk = m[1]; break; }
                            }
                            if (!sk || !grecaptcha || !grecaptcha.enterprise) return null;
                            return await grecaptcha.enterprise.execute(sk, {action: 'generate'});
                        } catch(e) { return null; }
                    }
                """)
            if recap and self._project_id:
                await self._page.evaluate(
                    """async ([pid, tok]) => {
                        try {
                            await fetch(
                                `https://aisandbox-pa.googleapis.com/v1/projects/${pid}/flowMedia:batchGenerateImages`,
                                {method:'POST', credentials:'include',
                                 headers:{'Content-Type':'text/plain;charset=UTF-8'},
                                 body:JSON.stringify({clientContext:{recaptchaContext:{token:tok,applicationType:'RECAPTCHA_APPLICATION_TYPE_WEB'},projectId:pid,tool:'PINHOLE',sessionId:';'+Date.now()},mediaGenerationContext:{batchId:crypto.randomUUID()},useNewMedia:true,requests:[{imageModelName:'NARWHAL',imageAspectRatio:'IMAGE_ASPECT_RATIO_SQUARE',structuredPrompt:{parts:[{text:'bearer capture test'}]},seed:12345,imageInputs:[]}]})}
                            );
                        } catch(e) {}
                    }""",
                    [self._project_id, recap],
                )
                await asyncio.sleep(2)
        except Exception:
            pass

        if self._bearer_token:
            self._log(f"[SharedBrowser:{self.account_name}] Bearer token ready!")
        else:
            self._log(f"[SharedBrowser:{self.account_name}] Bearer token NOT captured.")

    async def get_bearer_token(self):
        """Get Bearer token. Refresh if expired (>30 min)."""
        if self._bearer_token and (time.time() - self._bearer_token_time) < 1800:
            return self._bearer_token
        self._log(f"[SharedBrowser:{self.account_name}] Bearer token expired. Refreshing...")
        self._bearer_token = None
        await self._trigger_bearer_capture()
        return self._bearer_token

    def _extract_project_id_from_url(self, url):
        """Extract project ID from a URL string."""
        match = re.search(r"/project/([a-z0-9-]{16,})", url, re.IGNORECASE)
        if match:
            return match.group(1)
        match = re.search(r"/flow/([a-f0-9-]{36})", url)
        if match:
            return match.group(1)
        return None

    async def _extract_project_id_from_dom(self):
        """Extract project ID from DOM hints."""
        try:
            return await self._page.evaluate("""
                () => {
                    const regex = /\\/project\\/([a-z0-9-]{16,})/i;
                    const candidates = [String(window.location.href || "")];
                    const links = document.querySelectorAll("a[href*='/project/']");
                    for (const n of links) candidates.push(String(n.href || ""));
                    const scripts = document.querySelectorAll("script");
                    for (const s of scripts) {
                        const t = String(s.textContent || "");
                        if (t.includes("/project/")) candidates.push(t.slice(0, 6000));
                    }
                    for (const item of candidates) {
                        const m = item.match(regex);
                        if (m && m[1]) return m[1];
                    }
                    const body = document.body ? document.body.innerHTML.slice(0, 20000) : '';
                    const m2 = body.match(/"projectId"\\s*:\\s*"([a-f0-9-]{16,})"/);
                    if (m2) return m2[1];
                    return null;
                }
            """)
        except Exception:
            return None

    async def _click_new_project(self):
        """Click 'New project' button using multiple strategies (mirrors bot_engine)."""
        # Strategy 1: XPath text match
        try:
            btn = self._page.locator(
                "//*[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                " 'abcdefghijklmnopqrstuvwxyz'), 'new project')]"
            ).first
            if await btn.is_visible(timeout=3000):
                await btn.click(force=True)
                self._log(f"[SharedBrowser:{self.account_name}] Clicked 'New project' (text match).")
                return True
        except Exception:
            pass

        # Strategy 2: Heuristic button search (same as bot_engine)
        try:
            result = await self._page.evaluate("""
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
                        const txt = String(b.textContent || "").toLowerCase().replace(/\\s+/g, " ").trim();
                        const aria = String(b.getAttribute("aria-label") || "").toLowerCase();
                        const title = String(b.getAttribute("title") || "").toLowerCase();
                        const icon = String(
                            b.querySelector("i, mat-icon, .material-symbols, .material-symbols-outlined")?.textContent || ""
                        ).toLowerCase().trim();
                        const looksCreate =
                            txt.includes("new project") ||
                            (txt.includes("new") && txt.includes("project")) ||
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
                        score += (r.right || 0) * 0.1;
                        score -= (r.top || 0) * 0.08;
                        if (txt.includes("new project") || aria.includes("new project")) score += 500;
                        if (txt === "+" || icon === "add" || icon === "add_2") score += 100;
                        if (score > bestScore) { bestScore = score; best = b; }
                    }
                    if (!best) return { ok: false };
                    try { best.click(); } catch {
                        best.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                    }
                    return { ok: true };
                }
            """)
            if result and result.get("ok"):
                self._log(f"[SharedBrowser:{self.account_name}] Clicked 'New project' (heuristic).")
                return True
        except Exception:
            pass

        return False

    async def _extract_project_id(self):
        """Extract Flow project ID — robust multi-strategy approach (mirrors bot_engine)."""
        if not self._page:
            return None

        url = self._page.url
        self._log(f"[SharedBrowser:{self.account_name}] Current URL: {url}")

        if "accounts.google.com" in url or "signin" in url.lower():
            self._log(f"[SharedBrowser:{self.account_name}] Redirected to sign-in — cookies not working!")
            return None

        # 1. bot_engine shared cache — try navigating to cached project directly
        cached_pid = None
        try:
            from src.core.bot_engine import GoogleLabsBot
            cached_pid = GoogleLabsBot._shared_flow_project_id_by_account.get(self.account_name)
        except Exception:
            pass

        if cached_pid:
            self._log(f"[SharedBrowser:{self.account_name}] Trying cached project: {cached_pid}")
            try:
                await self._page.goto(
                    f"https://labs.google/fx/tools/flow/project/{cached_pid}",
                    wait_until="domcontentloaded", timeout=15000,
                )
                await asyncio.sleep(2)
                pid = self._extract_project_id_from_url(self._page.url)
                if pid:
                    self._log(f"[SharedBrowser:{self.account_name}] Project ID from cache nav: {pid}")
                    return pid
            except Exception:
                pass

        # 2. Check current URL
        pid = self._extract_project_id_from_url(self._page.url)
        if pid:
            self._log(f"[SharedBrowser:{self.account_name}] Project ID from URL: {pid}")
            return pid

        # 3. DOM hints
        pid = await self._extract_project_id_from_dom()
        if pid:
            self._log(f"[SharedBrowser:{self.account_name}] Project ID from DOM: {pid}")
            return pid

        # 4. Click "New project" with retries + polling (like bot_engine)
        for attempt in range(1, 4):
            self._log(f"[SharedBrowser:{self.account_name}] New project attempt {attempt}/3...")
            clicked = await self._click_new_project()
            if clicked:
                await asyncio.sleep(1.5)
            else:
                self._log(f"[SharedBrowser:{self.account_name}] 'New project' button not found.")
                await asyncio.sleep(1)

            # Poll for project ID in URL or DOM (14 checks × 0.5s = ~7s)
            for poll in range(14):
                pid = self._extract_project_id_from_url(self._page.url)
                if pid:
                    self._log(f"[SharedBrowser:{self.account_name}] Project ID from URL (poll {poll}): {pid}")
                    return pid
                pid = await self._extract_project_id_from_dom()
                if pid:
                    self._log(f"[SharedBrowser:{self.account_name}] Project ID from DOM (poll {poll}): {pid}")
                    return pid
                await asyncio.sleep(0.5)

            # Reload flow page between attempts
            if attempt < 3:
                try:
                    await self._page.goto(
                        "https://labs.google/fx/tools/flow",
                        wait_until="domcontentloaded", timeout=15000,
                    )
                    await asyncio.sleep(2)
                except Exception:
                    pass

        self._log(f"[SharedBrowser:{self.account_name}] No project ID found after 3 attempts. URL: {self._page.url}")
        return None

    async def _check_recaptcha(self):
        """Check if reCAPTCHA enterprise is loaded on the page."""
        try:
            return await self._page.evaluate(
                "() => typeof grecaptcha !== 'undefined' && !!grecaptcha.enterprise"
            )
        except Exception:
            return False

    async def _warmup_page(self):
        """Humanized scroll warmup to improve reCAPTCHA score."""
        if not self._page:
            return
        self._log(f"[SharedBrowser:{self.account_name}] Warming up page (humanized scroll)...")
        try:
            direction = 1
            for _ in range(8):
                delta = random.randint(90, 260) * direction
                await self._page.mouse.wheel(0, delta)
                if random.random() < 0.3:
                    direction *= -1
                await asyncio.sleep(random.uniform(0.3, 0.6))
            # Random mouse movements
            for _ in range(3):
                x = random.randint(200, 800)
                y = random.randint(200, 600)
                await self._page.mouse.move(x, y)
                await asyncio.sleep(random.uniform(0.2, 0.5))
        except Exception:
            pass

    def get_page(self):
        return self._page

    def get_project_id(self):
        return self._project_id

    async def maybe_reload(self):
        """Reload page every 40 jobs to keep reCAPTCHA fresh."""
        self._jobs_since_reload += 1
        if self._jobs_since_reload >= 40 and (time.time() - self._last_page_reload) > 60:
            self._log(f"[SharedBrowser:{self.account_name}] Refreshing page (every 100 jobs)...")
            try:
                await self._page.goto(
                    "https://labs.google/fx/tools/flow",
                    wait_until="domcontentloaded", timeout=15000,
                )
                await asyncio.sleep(3)
                pid = await self._extract_project_id()
                if pid:
                    self._project_id = pid
                self._last_page_reload = time.time()
                self._jobs_since_reload = 0
            except Exception:
                pass

    def _load_exported_cookies(self):
        if not os.path.exists(self._cookies_json):
            self._log(f"[SharedBrowser:{self.account_name}] exported_cookies.json NOT FOUND at {self._cookies_json}")
            return []
        try:
            with open(self._cookies_json, "r", encoding="utf-8") as f:
                cookies = json.load(f)
            valid = [c for c in cookies if c.get("name") and c.get("value") and c.get("domain")]
            if len(valid) != len(cookies):
                self._log(
                    f"[SharedBrowser:{self.account_name}] "
                    f"Filtered {len(cookies) - len(valid)} invalid cookies."
                )
            return valid
        except Exception as e:
            self._log(f"[SharedBrowser:{self.account_name}] Cookie file read error: {str(e)[:50]}")
            return []

    async def _import_cookies(self, cookies):
        """Import cookies into browser context. Returns count imported."""
        if not cookies or not self._context:
            return 0
        try:
            await self._context.add_cookies(cookies)
            return len(cookies)
        except Exception as e:
            self._log(f"[SharedBrowser:{self.account_name}] add_cookies error: {str(e)[:80]}")
            # Try importing one by one to skip bad cookies
            imported = 0
            for c in cookies:
                try:
                    await self._context.add_cookies([c])
                    imported += 1
                except Exception:
                    pass
            if imported > 0:
                self._log(f"[SharedBrowser:{self.account_name}] Imported {imported}/{len(cookies)} cookies (some failed).")
            return imported

    def _copy_profile_trust_data(self, src_profile, dst_profile):
        """Copy reCAPTCHA trust data (Local Storage, IndexedDB, Cookies) from main profile to shared profile."""
        import shutil
        # Browser profile data is inside Default/ subdirectory
        src_default = os.path.join(src_profile, "Default")
        dst_default = os.path.join(dst_profile, "Default")
        if not os.path.isdir(src_default):
            src_default = src_profile
        os.makedirs(dst_default, exist_ok=True)

        dirs_to_copy = ["Local Storage", "IndexedDB", "Session Storage"]
        files_to_copy = ["Cookies", "Cookies-journal"]
        copied = []

        for d in dirs_to_copy:
            src = os.path.join(src_default, d)
            dst = os.path.join(dst_default, d)
            if os.path.isdir(src):
                try:
                    if os.path.isdir(dst):
                        shutil.rmtree(dst, ignore_errors=True)
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                    copied.append(d)
                except Exception:
                    pass

        for f in files_to_copy:
            src = os.path.join(src_default, f)
            dst = os.path.join(dst_default, f)
            if os.path.isfile(src):
                try:
                    shutil.copy2(src, dst)
                    copied.append(f)
                except Exception:
                    pass

        if copied:
            self._log(f"[SharedBrowser:{self.account_name}] Copied trust data: {', '.join(copied)}")
        else:
            self._log(f"[SharedBrowser:{self.account_name}] No trust data found to copy from main profile.")

    def _clean_locks(self, path):
        for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
            lp = os.path.join(path, lock)
            try:
                if os.path.exists(lp) or os.path.islink(lp):
                    os.unlink(lp)
            except Exception:
                pass

    async def stop(self):
        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass
        self._context = None
        self._page = None
        self._log(f"[SharedBrowser:{self.account_name}] Stopped.")


# ═══════════════════════════════════════════════════════════════════════════
# BrowserFetchWorker — executes API calls via shared browser's fetch()
# ═══════════════════════════════════════════════════════════════════════════

class BrowserFetchWorker:
    """
    Worker that executes reCAPTCHA + API calls in ONE page.evaluate().
    No separate browser, no HTTP client — just queues JS fetch() calls.
    RAM: ~0 MB (shares the SharedBrowser's page).
    """

    def __init__(self, slot_id, shared_browser, log_fn):
        self.slot_id = slot_id
        self.account_name = shared_browser.account_name
        self._browser = shared_browser
        self._log = log_fn
        self.is_busy = False
        self.jobs_completed = 0

    async def generate_image(self, prompt, model, ratio, references=None):
        """Generate image: auth/session + reCAPTCHA + fetch in ONE evaluate (matches bot_engine)."""
        self.is_busy = True
        try:
            page = self._browser.get_page()
            project_id = self._browser.get_project_id()
            if not page or not project_id:
                return None, "No browser page or project ID"

            api_model = _resolve_image_model(model)
            api_ratio = _resolve_image_ratio(ratio)
            seed = random.randint(100000, 999999)
            batch_id = str(uuid.uuid4())
            prompt_text = prompt if prompt.endswith("\n") else f"{prompt}\n"

            self._log(f"[{self.slot_id}] Image: model={model} -> {api_model}, ratio={api_ratio}")

            # Main-world reCAPTCHA: get token via <script> tag injection
            # (no CDP traces, like competitor's Chrome Extension approach)
            mainworld_token = None
            try:
                mainworld_token = await get_recaptcha_token_mainworld(
                    page, "IMAGE_GENERATION", self._log
                )
            except Exception:
                pass

            result = await page.evaluate(
                """
                async ({ projectId, prompt, modelName, aspectRatio, batchId, seed, referenceMediaIds, cachedToken }) => {
                    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

                    // Step 1: Get fresh auth session (same as bot_engine)
                    const getAuthSession = async () => {
                        try {
                            const resp = await fetch("https://labs.google/fx/api/auth/session", {
                                method: "GET", credentials: "include"
                            });
                            if (!resp.ok) return null;
                            const data = await resp.json().catch(() => null);
                            if (!data || !data.access_token) return null;
                            return data;
                        } catch { return null; }
                    };

                    // Step 2: Get reCAPTCHA token — prefer main-world cached token,
                    // fall back to inline evaluate if not available
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
                                    if (render && render !== "explicit") { siteKey = render; break; }
                                } catch {}
                            }
                            if (!siteKey) return null;

                            if (typeof enterprise.ready === "function") {
                                await new Promise((resolve) => enterprise.ready(resolve));
                            }

                            const token = await enterprise.execute(siteKey, { action: "IMAGE_GENERATION" });
                            if (!token) return null;
                            return { token, applicationType: "RECAPTCHA_APPLICATION_TYPE_WEB" };
                        } catch { return null; }
                    };

                    try {
                        const authSession = await getAuthSession();
                        if (!authSession || !authSession.access_token) {
                            return { error: "missing auth session access token" };
                        }

                        // Use main-world token if available, otherwise fall back
                        const recaptchaContext = cachedToken
                            ? { token: cachedToken, applicationType: "RECAPTCHA_APPLICATION_TYPE_WEB" }
                            : await getRecaptchaContext();
                        const clientContext = {
                            projectId, tool: "PINHOLE", sessionId: ";" + Date.now()
                        };
                        if (recaptchaContext) {
                            clientContext.recaptchaContext = recaptchaContext;
                        }

                        const body = {
                            clientContext,
                            mediaGenerationContext: { batchId },
                            useNewMedia: true,
                            requests: [{
                                clientContext,
                                imageModelName: modelName,
                                imageAspectRatio: aspectRatio,
                                structuredPrompt: { parts: [{ text: prompt }] },
                                seed,
                                imageInputs: Array.isArray(referenceMediaIds) && referenceMediaIds.length > 0
                                    ? referenceMediaIds.map((id) => ({ imageInputType: "IMAGE_INPUT_TYPE_REFERENCE", name: id }))
                                    : []
                            }]
                        };

                        const resp = await fetch(
                            `https://aisandbox-pa.googleapis.com/v1/projects/${projectId}/flowMedia:batchGenerateImages`,
                            {
                                method: "POST", credentials: "include",
                                headers: {
                                    "content-type": "text/plain;charset=UTF-8",
                                    "authorization": `Bearer ${authSession.access_token}`
                                },
                                body: JSON.stringify(body)
                            }
                        );

                        if (!resp.ok) {
                            const errText = await resp.text();
                            return { error: `HTTP ${resp.status}: ${errText.substring(0, 300)}` };
                        }

                        return { success: true, data: await resp.json() };
                    } catch(e) {
                        return { error: e.message || String(e) };
                    }
                }
                """,
                {
                    "projectId": project_id, "prompt": prompt_text,
                    "modelName": api_model, "aspectRatio": api_ratio,
                    "batchId": batch_id, "seed": seed,
                    "referenceMediaIds": references or [],
                    "cachedToken": mainworld_token,
                },
            )

            await self._browser.maybe_reload()

            if not result:
                return None, "No response from browser evaluate"
            if result.get("error"):
                return None, _parse_api_error_string(result["error"])
            if result.get("success"):
                self.jobs_completed += 1
                return result["data"], None
            return None, "Unknown response"

        except Exception as e:
            return None, str(e)[:300]
        finally:
            self.is_busy = False

    async def generate_video(self, prompt, model, ratio):
        """Generate video: auth/session + reCAPTCHA + fetch in ONE evaluate (matches bot_engine)."""
        self.is_busy = True
        try:
            page = self._browser.get_page()
            project_id = self._browser.get_project_id()
            if not page or not project_id:
                return None, "No browser page or project ID"

            api_model = _resolve_video_model(model)
            api_ratio = _resolve_video_ratio(ratio)
            seed = random.randint(100000, 999999)
            batch_id = str(uuid.uuid4())

            self._log(f"[{self.slot_id}] Video: model={model} -> {api_model}, ratio={api_ratio}")

            # Main-world reCAPTCHA token for video
            mainworld_token = None
            try:
                mainworld_token = await get_recaptcha_token_mainworld(
                    page, "VIDEO_GENERATION", self._log
                )
            except Exception:
                pass

            result = await page.evaluate(
                """
                async ({ projectId, prompt, model, ratio, seed, batchId, cachedToken }) => {
                    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

                    const getAuthSession = async () => {
                        try {
                            const resp = await fetch("https://labs.google/fx/api/auth/session", {
                                method: "GET", credentials: "include"
                            });
                            if (!resp.ok) return null;
                            const data = await resp.json().catch(() => null);
                            if (!data || !data.access_token) return null;
                            return data;
                        } catch { return null; }
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
                                    if (render && render !== "explicit") { siteKey = render; break; }
                                } catch {}
                            }
                            if (!siteKey) return null;

                            if (typeof enterprise.ready === "function") {
                                await new Promise((resolve) => enterprise.ready(resolve));
                            }

                            const token = await enterprise.execute(siteKey, { action: "VIDEO_GENERATION" });
                            if (!token) return null;
                            return { token, applicationType: "RECAPTCHA_APPLICATION_TYPE_WEB" };
                        } catch { return null; }
                    };

                    try {
                        const authSession = await getAuthSession();
                        if (!authSession || !authSession.access_token) {
                            return { error: "missing auth session access token" };
                        }

                        // Use main-world token if available, otherwise fall back
                        const recaptchaContext = cachedToken
                            ? { token: cachedToken, applicationType: "RECAPTCHA_APPLICATION_TYPE_WEB" }
                            : await getRecaptchaContext();
                        const clientContext = {
                            projectId, tool: "PINHOLE",
                            userPaygateTier: "PAYGATE_TIER_TWO",
                            sessionId: ";" + Date.now()
                        };
                        if (recaptchaContext) {
                            clientContext.recaptchaContext = recaptchaContext;
                        }

                        const body = {
                            mediaGenerationContext: { batchId },
                            clientContext,
                            requests: [{
                                clientContext,
                                aspectRatio: ratio,
                                seed,
                                textInput: { structuredPrompt: { parts: [{ text: prompt }] } },
                                videoModelKey: model,
                                metadata: {}
                            }],
                            useV2ModelConfig: true
                        };

                        const resp = await fetch(
                            "https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoText",
                            {
                                method: "POST", credentials: "include",
                                headers: {
                                    "content-type": "text/plain;charset=UTF-8",
                                    "authorization": `Bearer ${authSession.access_token}`
                                },
                                body: JSON.stringify(body)
                            }
                        );

                        if (!resp.ok) {
                            const errText = await resp.text();
                            return { error: `HTTP ${resp.status}: ${errText.substring(0, 300)}` };
                        }

                        return { success: true, data: await resp.json() };
                    } catch(e) {
                        return { error: e.message || String(e) };
                    }
                }
                """,
                {
                    "projectId": project_id, "prompt": prompt,
                    "model": api_model, "ratio": api_ratio,
                    "seed": seed, "batchId": batch_id,
                    "cachedToken": mainworld_token,
                },
            )

            await self._browser.maybe_reload()

            if not result:
                return None, "No response from browser evaluate"
            if result.get("error"):
                return None, _parse_api_error_string(result["error"])
            if result.get("success"):
                self.jobs_completed += 1
                return result["data"], None
            return None, "Unknown response"

        except Exception as e:
            return None, str(e)[:300]
        finally:
            self.is_busy = False

    async def close(self):
        pass  # Nothing to close — no HTTP session


# ═══════════════════════════════════════════════════════════════════════════
# HttpModeManager — Orchestrates shared browsers + fetch workers
# ═══════════════════════════════════════════════════════════════════════════

class HttpModeManager:
    """
    Manages shared browsers and BrowserFetchWorkers for all accounts.
    Receives the AsyncQueueManager instance to access signals, settings, etc.
    """

    def __init__(self, queue_manager):
        self.qm = queue_manager
        self._log = lambda msg: queue_manager.signals.log_msg.emit(msg)
        self._shared_browsers = {}   # account_name -> SharedBrowser
        self._workers = {}           # account_name -> [BrowserFetchWorker, ...]
        self._active_tasks = []
        self._account_info = {}      # account_name -> {session_path, cookies_json}
        # reCAPTCHA fail tracking per account
        self._recaptcha_fails = {}   # account_name -> count
        self._recaptcha_fail_max = get_int_setting("auto_restart_recaptcha_fails", 3)
        self._restart_cooldown = get_int_setting("restart_cooldown_seconds", 30)
        self._last_restart_time = {}  # account_name -> timestamp
        self._restart_pending = set()  # account names needing restart
        # Per-slot logic (same as browser-per-slot mode)
        self._account_disabled = {}        # account_name -> True (on hold)
        self._account_hold_until = {}      # account_name -> timestamp
        self._account_hold_reason = {}     # account_name -> reason string
        self._consecutive_failures = {}    # slot_id -> count
        self._slot_disabled_until = {}     # slot_id -> timestamp
        self._recaptcha_streak = {}        # account_name -> streak count
        self._last_account_dispatch = {}   # account_name -> timestamp
        self._same_account_stagger = float(getattr(queue_manager, 'same_account_stagger_seconds', 1.0))
        # Read display setting
        cloak_display = str(get_setting("cloak_display", "headless") or "headless").strip().lower()
        self._stealth_visible = cloak_display == "stealth_visible"
        self._headless = cloak_display == "headless"  # stealth_visible = visible (not headless)

    async def run(self):
        """Main entry — start shared browsers, dispatch jobs."""
        from playwright.async_api import async_playwright

        all_accs = get_accounts()
        if not all_accs:
            self._log("[HTTP] No accounts configured.")
            return

        slots_per_account = max(1, min(40, get_int_setting("slots_per_account", 5)))
        self._log(
            f"[HTTP] Starting: {len(all_accs)} account(s), "
            f"{slots_per_account} worker(s) each."
        )

        async with async_playwright() as p:
            try:
                # Start shared browsers (1 per account)
                for acc in all_accs:
                    name = acc.get("name", "unknown")
                    session_path = acc.get("session_path", os.path.join(DATA_DIR, name))
                    cookies_json = os.path.join(session_path, "exported_cookies.json")

                    # Try cached project ID
                    cached_pid = None
                    try:
                        from src.core.bot_engine import GoogleLabsBot
                        cached_pid = GoogleLabsBot._shared_flow_project_id_by_account.get(name)
                    except Exception:
                        pass

                    account_proxy = str(acc.get("proxy") or "").strip()

                    # Store account info for restart
                    self._account_info[name] = {
                        "session_path": session_path,
                        "cookies_json": cookies_json,
                        "proxy": account_proxy,
                    }
                    self._recaptcha_fails[name] = 0

                    browser = SharedBrowser(
                        name, session_path, cookies_json, self._log,
                        project_id=cached_pid, headless=self._headless,
                        stealth_visible=self._stealth_visible, proxy=account_proxy,
                    )
                    success = await browser.start(p)

                    if not success:
                        self._log(f"[HTTP] {name}: Browser failed! Account skipped.")
                        continue

                    self._shared_browsers[name] = browser

                    # Create fetch workers
                    account_workers = []
                    for idx in range(1, slots_per_account + 1):
                        slot_id = f"{name}#h{idx}"
                        worker = BrowserFetchWorker(slot_id, browser, self._log)
                        account_workers.append(worker)

                    self._workers[name] = account_workers
                    self._log(f"[HTTP] {name}: 1 shared browser + {len(account_workers)} fetch workers ready.")

                total_workers = sum(len(w) for w in self._workers.values())
                total_browsers = len(self._shared_browsers)

                if total_workers == 0:
                    self._log("[HTTP] No workers started. Cannot proceed.")
                    return

                self._log(
                    f"[HTTP] Total: {total_browsers} browser(s), "
                    f"{total_workers} fetch worker(s). "
                    f"Est. RAM: ~{total_browsers * 300} MB."
                )

                # Main dispatch loop
                while self.qm.is_running:
                    if self.qm.stop_requested or self.qm.force_stop_requested:
                        break
                    if self.qm.pause_requested:
                        await asyncio.sleep(1)
                        continue

                    # Check account holds (same as per-slot mode)
                    self._check_account_holds()

                    # Check for browser restarts needed
                    if self._restart_pending:
                        await self._process_restarts(p, slots_per_account)

                    self._active_tasks = [t for t in self._active_tasks if not t.done()]

                    jobs = get_all_jobs()
                    pending = [j for j in jobs if j["status"] == "pending"]

                    if not pending:
                        if not self._active_tasks:
                            still_active = any(
                                j["status"] in ("pending", "running") for j in get_all_jobs()
                            )
                            if not still_active:
                                self._log("[HTTP] All jobs completed.")
                                break
                        await asyncio.sleep(self.qm.scheduler_poll_seconds)
                        continue

                    busy_slots = {t.get_name() for t in self._active_tasks if hasattr(t, "get_name")}

                    now = time.time()
                    dispatched = 0
                    for job in pending:
                        # Re-check stop inside dispatch loop
                        if self.qm.stop_requested or self.qm.force_stop_requested:
                            break

                        # Check retry_after (smart backoff from per-slot logic)
                        retry_after = self.qm.job_retry_after.get(job["id"], 0)
                        if retry_after > now:
                            continue

                        worker = self._get_available_worker(busy_slots)
                        if not worker:
                            break

                        job_id = job["id"]
                        update_job_status(job_id, "running", account=worker.account_name)
                        self.qm.signals.job_updated.emit(job_id, "running", worker.account_name, "")

                        # Track same-account stagger (per-slot style)
                        self._last_account_dispatch[worker.account_name] = time.time()

                        task = asyncio.create_task(
                            self._run_job(worker, job), name=worker.slot_id,
                        )
                        self._active_tasks.append(task)
                        busy_slots.add(worker.slot_id)
                        dispatched += 1

                        stagger = random.uniform(
                            self.qm.global_stagger_min_seconds,
                            self.qm.global_stagger_max_seconds,
                        )
                        if stagger > 0:
                            await asyncio.sleep(stagger)

                    if dispatched == 0:
                        await asyncio.sleep(self.qm.scheduler_poll_seconds)

                if self._active_tasks:
                    if self.qm.stop_requested or self.qm.force_stop_requested:
                        # Cancel all active tasks immediately on stop
                        self._log(f"[HTTP] Cancelling {len(self._active_tasks)} active job(s)...")
                        for t in self._active_tasks:
                            if not t.done():
                                t.cancel()
                        # Short timeout — don't wait forever
                        try:
                            await asyncio.wait_for(
                                asyncio.gather(*self._active_tasks, return_exceptions=True),
                                timeout=3.0,
                            )
                        except asyncio.TimeoutError:
                            self._log("[HTTP] Some tasks didn't cancel in 3s — continuing.")
                    else:
                        self._log(f"[HTTP] Waiting for {len(self._active_tasks)} active job(s)...")
                        await asyncio.gather(*self._active_tasks, return_exceptions=True)

            finally:
                for w_list in self._workers.values():
                    for w in w_list:
                        await w.close()
                for browser in self._shared_browsers.values():
                    await browser.stop()
                self._workers.clear()
                self._shared_browsers.clear()
                self._log("[HTTP] All browsers and workers stopped.")

    def _get_available_worker(self, busy_slots):
        now = time.time()
        for account_name, workers in self._workers.items():
            # Skip accounts pending restart
            if account_name in self._restart_pending:
                continue
            # Skip accounts on hold (rate-limited, auth expired, etc.)
            if self._account_disabled.get(account_name):
                continue
            # Same-account stagger (per-slot style)
            last_dispatch = self._last_account_dispatch.get(account_name, 0)
            if now - last_dispatch < self._same_account_stagger:
                continue
            for worker in workers:
                # Skip slots in cooldown
                if self._slot_disabled_until.get(worker.slot_id, 0) > now:
                    continue
                if worker.slot_id not in busy_slots and not worker.is_busy:
                    return worker
        return None

    def _report_recaptcha_fail(self, account_name, slot_id):
        """Track reCAPTCHA failure with streak-based cooldown (per-slot style)."""
        # If restart already queued for this account, don't keep counting
        if account_name in self._restart_pending:
            return
        self._recaptcha_fails[account_name] = self._recaptcha_fails.get(account_name, 0) + 1
        count = self._recaptcha_fails[account_name]

        # Streak-based slot cooldown (same as per-slot mode)
        streak = self._recaptcha_streak.get(account_name, 0) + 1
        self._recaptcha_streak[account_name] = streak
        cooldown_seconds = self.qm.recaptcha_account_cooldown_seconds * min(streak, 3)
        self._slot_disabled_until[slot_id] = time.time() + cooldown_seconds
        self._log(
            f"[HTTP] {slot_id}: reCAPTCHA fail #{count} — slot cooldown "
            f"{cooldown_seconds}s (streak {streak}). Other slots continue."
        )

        if count >= self._recaptcha_fail_max:
            # Check cooldown before restart
            last_restart = self._last_restart_time.get(account_name, 0)
            if (time.time() - last_restart) >= self._restart_cooldown:
                self._restart_pending.add(account_name)
                self._log(
                    f"[HTTP] {account_name}: {count} reCAPTCHA fails — browser restart queued."
                )
            else:
                remaining = int(self._restart_cooldown - (time.time() - last_restart))
                self._log(
                    f"[HTTP] {account_name}: restart cooldown ({remaining}s remaining)."
                )

    def _report_recaptcha_success(self, account_name):
        """Reset reCAPTCHA fail counter and streak on success."""
        if self._recaptcha_fails.get(account_name, 0) > 0:
            self._recaptcha_fails[account_name] = 0
        if self._recaptcha_streak.get(account_name, 0) > 0:
            self._recaptcha_streak[account_name] = 0

    def _put_account_on_hold(self, account_name, reason, hold_seconds):
        """Put account on hold — stop dispatching jobs to it (same as per-slot)."""
        self._account_hold_until[account_name] = time.time() + hold_seconds
        self._account_hold_reason[account_name] = reason
        self._account_disabled[account_name] = True
        mins = hold_seconds // 60
        secs = hold_seconds % 60
        self._log(
            f"[HTTP] Account {account_name} ON HOLD: {reason} "
            f"(resume in {mins}m {secs}s)"
        )
        # Reassign pending jobs from this account
        try:
            from src.db.db_manager import reassign_account_jobs
            count = reassign_account_jobs(account_name)
            if count > 0:
                self._log(f"[HTTP] Reassigned {count} job(s) from {account_name} to other accounts.")
        except Exception:
            pass

    def _check_account_holds(self):
        """Re-enable accounts whose hold has expired."""
        now = time.time()
        for account_name in list(self._account_hold_until.keys()):
            if self._account_hold_until[account_name] <= now:
                self._account_disabled.pop(account_name, None)
                reason = self._account_hold_reason.pop(account_name, "")
                self._account_hold_until.pop(account_name, None)
                self._log(f"[HTTP] Account {account_name} hold expired ({reason}). Reactivated.")

    async def _process_restarts(self, playwright_instance, slots_per_account):
        """Restart browsers for accounts that hit reCAPTCHA fail threshold."""
        import shutil
        for account_name in list(self._restart_pending):
            # Wait for active tasks on this account to finish
            account_tasks = [
                t for t in self._active_tasks
                if hasattr(t, "get_name") and t.get_name().startswith(account_name) and not t.done()
            ]
            if account_tasks:
                self._log(f"[HTTP] {account_name}: waiting for {len(account_tasks)} active task(s) before restart...")
                await asyncio.gather(*account_tasks, return_exceptions=True)

            self._log(f"[HTTP] {account_name}: Restarting browser (reCAPTCHA refresh)...")

            # Stop old browser
            old_browser = self._shared_browsers.pop(account_name, None)
            if old_browser:
                await old_browser.stop()

            # Remove old workers
            old_workers = self._workers.pop(account_name, [])
            for w in old_workers:
                await w.close()

            # Delete shared browser profile for fresh start
            info = self._account_info.get(account_name, {})
            session_path = info.get("session_path", "")
            shared_profile = session_path + "_shared_browser"
            if os.path.isdir(shared_profile):
                try:
                    shutil.rmtree(shared_profile, ignore_errors=True)
                    self._log(f"[HTTP] {account_name}: Deleted old shared profile.")
                except Exception:
                    pass

            # Cooldown before restart
            self._log(f"[HTTP] {account_name}: Cooling down {self._restart_cooldown}s...")
            await asyncio.sleep(self._restart_cooldown)

            # Start fresh browser
            cookies_json = info.get("cookies_json", "")
            account_proxy = info.get("proxy", "")
            new_browser = SharedBrowser(
                account_name, session_path, cookies_json, self._log,
                headless=self._headless, stealth_visible=self._stealth_visible,
                proxy=account_proxy,
            )
            success = await new_browser.start(playwright_instance)

            if success:
                self._shared_browsers[account_name] = new_browser
                # Create new workers
                new_workers = []
                for idx in range(1, slots_per_account + 1):
                    slot_id = f"{account_name}#h{idx}"
                    worker = BrowserFetchWorker(slot_id, new_browser, self._log)
                    new_workers.append(worker)
                self._workers[account_name] = new_workers
                self._recaptcha_fails[account_name] = 0
                self._last_restart_time[account_name] = time.time()
                self._log(
                    f"[HTTP] {account_name}: Browser restarted! "
                    f"{len(new_workers)} workers ready."
                )
            else:
                self._log(f"[HTTP] {account_name}: Restart FAILED! Account offline.")

            self._restart_pending.discard(account_name)

    async def _download_and_save(self, worker, job_id, api_data, queue_no=None):
        """Download generated image/video from API response and save to output directory."""
        browser = worker._browser
        context = browser._context
        if not context:
            return None, "No browser context for download"

        # Extract media URL from API response
        media_list = api_data.get("media", []) if isinstance(api_data, dict) else []
        fife_url = None
        media_name = None

        for item in media_list:
            url = (item.get("image", {}).get("generatedImage", {}).get("fifeUrl", "")
                   if isinstance(item, dict) else "")
            name = item.get("name", "") if isinstance(item, dict) else ""
            if url:
                fife_url = url
                media_name = name
                break
            if name and not fife_url:
                media_name = name

        if not fife_url and media_name:
            fife_url = f"https://labs.google/fx/api/trpc/backbone.redirect?input=%7B%22name%22%3A%22{media_name}%22%7D"

        if not fife_url:
            # Try workflows fallback
            workflows = api_data.get("workflows", []) if isinstance(api_data, dict) else []
            if workflows:
                primary_id = workflows[0].get("metadata", {}).get("primaryMediaId", "")
                if primary_id:
                    fife_url = f"https://labs.google/fx/api/trpc/backbone.redirect?input=%7B%22name%22%3A%22{primary_id}%22%7D"

        if not fife_url:
            self._log(f"[{worker.slot_id}] API response keys: {list(api_data.keys()) if isinstance(api_data, dict) else 'not dict'}")
            return None, "No downloadable media in API response"

        self._log(f"[{worker.slot_id}] Downloading from: {fife_url[:100]}...")

        # Download via Playwright request context (avoids CORS — same as bot_engine)
        try:
            request_ctx = context.request
            resp = await request_ctx.get(fife_url, timeout=90000)

            if not resp.ok:
                return None, f"Download HTTP {resp.status}"

            content_type = str(resp.headers.get("content-type", "")).lower()
            ext_map = {
                "image/png": ".png",
                "image/jpeg": ".jpg",
                "image/webp": ".webp",
                "video/mp4": ".mp4",
                "video/webm": ".webm",
            }
            ext = ".jpg"
            for mime, candidate_ext in ext_map.items():
                if mime in content_type:
                    ext = candidate_ext
                    break

            data = await resp.body()
            if not data:
                return None, "Downloaded empty file"

            # Build output path (same naming as bot_engine)
            output_dir = get_output_directory()
            os.makedirs(output_dir, exist_ok=True)

            normalized_qno = None
            try:
                val = int(queue_no)
                if val > 0:
                    normalized_qno = val
            except Exception:
                pass

            if normalized_qno is not None:
                filename = f"{normalized_qno}{ext}"
            else:
                safe_job = (job_id or "job").replace("-", "")[:8]
                ts = int(time.time() * 1000)
                nonce = random.randint(1000, 9999)
                filename = f"{safe_job}_{ts}_{nonce}_generation{ext}"

            output_path = os.path.join(output_dir, filename)

            with open(output_path, "wb") as f:
                f.write(data)

            # Update job runtime state with output path
            try:
                update_job_runtime_state(job_id, output_path=output_path)
            except Exception:
                pass

            self._log(f"[{worker.slot_id}] Saved: {filename} ({len(data)} bytes)")
            return output_path, None

        except Exception as e:
            return None, f"Download exception: {str(e)[:200]}"

    async def _run_job(self, worker, job):
        """Execute a single job — per-slot style error handling with smart retry."""
        job_id = job["id"]
        job_type = job.get("job_type", "image")
        prompt = job.get("prompt", "")
        model = job.get("model", "")
        queue_no = job.get("output_index") or job.get("queue_no")
        slot_id = worker.slot_id
        account_name = worker.account_name

        self._log(f"[{slot_id}] Processing job {job_id[:6]}...: {prompt[:40]}...")

        # Stop check
        if self.qm.stop_requested or self.qm.force_stop_requested:
            update_job_status(job_id, "pending", account="")
            self.qm.signals.job_updated.emit(job_id, "pending", "", "")
            return

        # Account on hold / restart pending — re-queue
        if self._account_disabled.get(account_name) or account_name in self._restart_pending:
            update_job_status(job_id, "pending", account="")
            self.qm.signals.job_updated.emit(job_id, "pending", "", "")
            self._log(f"[{slot_id}] Job {job_id[:6]}... re-queued (account unavailable).")
            return

        try:
            if "video" in job_type:
                video_model = job.get("video_model") or model
                ratio = job.get("aspect_ratio", "ASPECT_RATIO_16_9")
                result, error = await worker.generate_video(prompt, video_model, ratio)
            else:
                ratio = job.get("aspect_ratio", "IMAGE_ASPECT_RATIO_LANDSCAPE")
                result, error = await worker.generate_image(prompt, model, ratio)

            if result and not error:
                # Success — reset counters
                self._report_recaptcha_success(account_name)
                self._consecutive_failures[slot_id] = 0

                # Download and save
                output_path, dl_error = await self._download_and_save(
                    worker, job_id, result, queue_no=queue_no
                )
                if dl_error:
                    # Download failed — treat as retryable error
                    await self._handle_job_failure(
                        slot_id, account_name, job_id, f"Download failed: {dl_error}"
                    )
                    return

                update_job_status(job_id, "completed", account=account_name)
                self.qm.signals.job_updated.emit(job_id, "completed", account_name, "")
                self._log(f"[{slot_id}] Job {job_id[:6]}... completed! Saved: {output_path}")
                return

            # Generation failed
            error = error or "Unknown error"
            await self._handle_job_failure(slot_id, account_name, job_id, error)

        except asyncio.CancelledError:
            # Task was cancelled by stop — re-queue job to pending
            update_job_status(job_id, "pending", account="")
            self.qm.signals.job_updated.emit(job_id, "pending", "", "")
            return

        except Exception as e:
            await self._handle_job_failure(
                slot_id, account_name, job_id, f"Exception: {str(e)[:300]}"
            )

    async def _handle_job_failure(self, slot_id, account_name, job_id, error_msg):
        """Handle job failure — same logic as per-slot mode in queue_manager."""
        category = self.qm._classify_error(error_msg)
        retryable = self.qm._is_retryable_error(error_msg)
        max_retries = self.qm.max_auto_retries_per_job + (
            1 if self.qm._is_high_priority_retry_error(error_msg) else 0
        )

        self._log(
            f"[{slot_id}] Failure: category={category}, retryable={'yes' if retryable else 'no'}."
        )

        # Account hold for auth/rate-limit errors (same as per-slot)
        msg_lower = (error_msg or "").lower()
        if category in ("auth_missing", "project_resolution_failed"):
            self._put_account_on_hold(account_name, f"session expired ({category})", 300)
        elif "session not signed in" in msg_lower or "not signed in" in msg_lower:
            self._put_account_on_hold(account_name, "session not signed in", 300)
        elif any(p in msg_lower for p in (
            "rate limit", "429", "quota exceeded", "resource exhausted",
            "too many requests", "quota_exceeded", "rate_limit",
        )):
            self._put_account_on_hold(account_name, "rate limited", 300)
        elif "access denied" in msg_lower or "account suspended" in msg_lower:
            self._put_account_on_hold(account_name, "access denied", 1800)

        # Moderated — non-retryable, mark failed immediately
        if category == "moderated":
            self.qm.job_retry_counts.pop(job_id, None)
            self.qm.job_retry_after.pop(job_id, None)
            self._consecutive_failures[slot_id] = 0
            normalized_error = str(error_msg or "").strip()
            if normalized_error and not normalized_error.startswith("MODERATION:"):
                normalized_error = f"MODERATION: {normalized_error}"
            final_error = f"[moderated] {normalized_error or 'Content blocked by policy filter'}"
            update_job_status(job_id, "failed", account=account_name, error=final_error)
            self.qm.signals.job_updated.emit(job_id, "failed", account_name, final_error)
            self._log(f"[{slot_id}] Prompt blocked by content filter. Marked as failed (no retry).")
            return

        # reCAPTCHA failure — streak-based slot cooldown
        if "recaptcha" in msg_lower or "captcha" in msg_lower:
            self._report_recaptcha_fail(account_name, slot_id)

        # Force Bearer refresh on auth errors
        if "401" in error_msg or "auth" in msg_lower:
            self._log(f"[{slot_id}] Auth failure — refreshing Bearer token...")
            try:
                worker_browser = None
                for w_list in self._workers.values():
                    for w in w_list:
                        if w.slot_id == slot_id:
                            worker_browser = w._browser
                            break
                if worker_browser:
                    worker_browser._bearer_token = None
            except Exception:
                pass

        # Consecutive slot failures — disable slot temporarily
        if self.qm._should_penalize_slot(error_msg):
            self._consecutive_failures[slot_id] = self._consecutive_failures.get(slot_id, 0) + 1
            max_consec = getattr(self.qm, 'max_consecutive_slot_failures', 2)
            cooldown_secs = getattr(self.qm, 'slot_cooldown_seconds', 45)
            if self._consecutive_failures[slot_id] >= max_consec:
                self._slot_disabled_until[slot_id] = time.time() + cooldown_secs
                self._consecutive_failures[slot_id] = 0
                self._log(
                    f"[{slot_id}] Slot disabled for {cooldown_secs}s after repeated failures."
                )
        else:
            self._consecutive_failures[slot_id] = 0

        # Smart retry — re-queue to pending (same as per-slot mode)
        retry_count = self.qm.job_retry_counts.get(job_id, 0)
        if max_retries > 0 and retryable:
            if retry_count < max_retries:
                retry_count += 1
                self.qm.job_retry_counts[job_id] = retry_count
                retry_delay = self.qm._get_retry_delay_seconds(error_msg, retry_count)
                if retry_delay > 0:
                    self.qm.job_retry_after[job_id] = time.time() + retry_delay
                    retry_note = (
                        f"Auto-retry scheduled ({retry_count}/{max_retries}) "
                        f"after {retry_delay}s [{category}]: {error_msg}"
                    )
                else:
                    self.qm.job_retry_after.pop(job_id, None)
                    retry_note = (
                        f"Auto-retry scheduled ({retry_count}/{max_retries}) "
                        f"immediately [{category}]: {error_msg}"
                    )
                update_job_status(job_id, "pending", account="", error=retry_note)
                self.qm.signals.job_updated.emit(job_id, "pending", "", retry_note)
                self._log(f"[{slot_id}] {retry_note}")
                return

        # All retries exhausted — mark failed
        self.qm.job_retry_counts.pop(job_id, None)
        self.qm.job_retry_after.pop(job_id, None)
        final_error = f"[{category}] {error_msg}"
        update_job_status(job_id, "failed", account=account_name, error=final_error)
        self.qm.signals.job_updated.emit(job_id, "failed", account_name, final_error)
        self._log(f"[{slot_id}] Job {job_id[:6]}... FAILED: {error_msg[:200]}")
