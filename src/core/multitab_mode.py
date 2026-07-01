"""
Multi-Tab Mode: 1 Browser per account, N tabs for parallel generation.

Architecture:
  1 CloakBrowser per account (persistent context)
  N pages (tabs) in that browser — each runs independently
  Each tab: navigates to Flow -> generates reCAPTCHA -> calls API via fetch()

RAM: 300MB base + ~30MB per tab
  30 tabs = ~1.2GB total (vs ~9GB with 30 separate browsers)
"""

import asyncio
import json
import os
import re
import time
import random
import uuid
import platform
import base64

from src.core.app_paths import get_sessions_dir
from src.core.process_tracker import cleanup_session_locks
from src.core.cloakbrowser_support import load_cloakbrowser_api
from src.db.db_manager import (
    get_accounts,
    get_all_jobs,
    get_setting,
    get_int_setting,
    get_bool_setting,
    update_job_status,
)

DATA_DIR = str(get_sessions_dir())

FLOW_URL = "https://labs.google/fx/tools/flow"


# ═══════════════════════════════════════════════════════════════════════════
# Model / ratio resolvers — display names to API enums
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


def _clean_lock_files(path):
    """Remove browser lock files."""
    if not path or not os.path.exists(path):
        return
    for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
        lp = os.path.join(path, lock)
        try:
            if os.path.exists(lp) or os.path.islink(lp):
                os.unlink(lp)
        except Exception:
            pass


def _load_cookies_from_json(path, log_fn=None, label=""):
    """Load and validate cookies from exported_cookies.json."""
    if not os.path.exists(path):
        if log_fn:
            log_fn(f"[{label}] exported_cookies.json NOT FOUND at {path}")
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            cookies = json.load(f)
        valid = [c for c in cookies if c.get("name") and c.get("value") and c.get("domain")]
        return valid
    except Exception as e:
        if log_fn:
            log_fn(f"[{label}] Cookie file error: {str(e)[:50]}")
        return []


# ═══════════════════════════════════════════════════════════════════════════
# TabWorker — one tab in the shared browser
# ═══════════════════════════════════════════════════════════════════════════

class TabWorker:
    """
    One tab in the shared browser. Handles image + video generation.
    reCAPTCHA + fetch() happen in the SAME browser context.
    credentials: 'include' sends cookies + OAuth automatically.
    """

    def __init__(self, slot_id, page, project_id, log_fn, get_bearer=None):
        self.slot_id = slot_id
        self.account_name = slot_id.split("#")[0] if "#" in slot_id else slot_id
        self._page = page
        self._project_id = project_id
        self._log = log_fn
        self._get_bearer = get_bearer  # Function to get Bearer token from AccountBrowser
        self.is_busy = False
        self.jobs_completed = 0
        self._recaptcha_ready = False

    async def setup(self):
        """Verify reCAPTCHA is loaded on this tab."""
        try:
            self._recaptcha_ready = await self._page.evaluate(
                "() => typeof grecaptcha !== 'undefined' && !!grecaptcha.enterprise"
            )
        except Exception:
            self._recaptcha_ready = False
        if not self._recaptcha_ready:
            self._log(f"[{self.slot_id}] reCAPTCHA not ready on tab.")

    async def generate_image(self, prompt, model, ratio, references=None):
        """Generate image: reCAPTCHA + fetch with Bearer in this tab."""
        self.is_busy = True
        try:
            bearer = self._get_bearer() if self._get_bearer else None
            if not bearer:
                return None, "No Bearer token available"

            api_model = _resolve_image_model(model)
            api_ratio = _resolve_image_ratio(ratio)
            seed = random.randint(100000, 999999)
            session_id = f";{int(time.time() * 1000)}"
            batch_id = str(uuid.uuid4())

            self._log(f"[{self.slot_id}] Image: {api_model}, {api_ratio}")

            result = await self._page.evaluate(
                """
                async ([projectId, prompt, model, ratio, seed, sessionId, batchId, refInputs, bearerToken]) => {
                    try {
                        // reCAPTCHA token
                        let recaptchaToken = null;
                        try {
                            const scripts = document.querySelectorAll("script[src*='recaptcha'][src*='render=']");
                            let siteKey = null;
                            for (const s of scripts) {
                                const m = s.src.match(/render=([^&]+)/);
                                if (m && m[1] !== 'explicit') { siteKey = m[1]; break; }
                            }
                            if (siteKey && typeof grecaptcha !== 'undefined' && grecaptcha.enterprise) {
                                recaptchaToken = await grecaptcha.enterprise.execute(siteKey, {action: 'generate'});
                            }
                        } catch(e) {
                            return { error: 'reCAPTCHA failed: ' + e.message };
                        }

                        const clientContext = {
                            projectId: projectId,
                            tool: 'PINHOLE',
                            sessionId: sessionId
                        };
                        if (recaptchaToken) {
                            clientContext.recaptchaContext = {
                                token: recaptchaToken,
                                applicationType: 'RECAPTCHA_APPLICATION_TYPE_WEB'
                            };
                        }

                        const refs = typeof refInputs === 'string' ? JSON.parse(refInputs) : (refInputs || []);

                        const payload = {
                            clientContext: clientContext,
                            mediaGenerationContext: { batchId: batchId },
                            useNewMedia: true,
                            requests: [{
                                clientContext: clientContext,
                                imageModelName: model,
                                imageAspectRatio: ratio,
                                structuredPrompt: { parts: [{ text: prompt }] },
                                seed: seed,
                                imageInputs: refs
                            }]
                        };

                        const url = `https://aisandbox-pa.googleapis.com/v1/projects/${projectId}/flowMedia:batchGenerateImages`;
                        const resp = await fetch(url, {
                            method: 'POST',
                            credentials: 'include',
                            headers: {
                                'Content-Type': 'text/plain;charset=UTF-8',
                                'Authorization': bearerToken
                            },
                            body: JSON.stringify(payload)
                        });

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
                [self._project_id, prompt, api_model, api_ratio, seed, session_id, batch_id,
                 json.dumps(references) if references else "[]", bearer],
            )

            return self._handle_result(result)

        except Exception as e:
            return None, str(e)[:200]
        finally:
            self.is_busy = False
            await self._maybe_refresh()

    async def generate_video(self, prompt, model, ratio):
        """Generate video: reCAPTCHA + fetch with Bearer in this tab."""
        self.is_busy = True
        try:
            bearer = self._get_bearer() if self._get_bearer else None
            if not bearer:
                return None, "No Bearer token available"

            api_model = _resolve_video_model(model)
            api_ratio = _resolve_video_ratio(ratio)
            seed = random.randint(100000, 999999)
            session_id = f";{int(time.time() * 1000)}"
            batch_id = str(uuid.uuid4())

            self._log(f"[{self.slot_id}] Video: {api_model}, {api_ratio}")

            result = await self._page.evaluate(
                """
                async ([projectId, prompt, model, ratio, seed, sessionId, batchId, bearerToken]) => {
                    try {
                        let recaptchaToken = null;
                        try {
                            const scripts = document.querySelectorAll("script[src*='recaptcha'][src*='render=']");
                            let siteKey = null;
                            for (const s of scripts) {
                                const m = s.src.match(/render=([^&]+)/);
                                if (m && m[1] !== 'explicit') { siteKey = m[1]; break; }
                            }
                            if (siteKey && typeof grecaptcha !== 'undefined' && grecaptcha.enterprise) {
                                recaptchaToken = await grecaptcha.enterprise.execute(siteKey, {action: 'generate'});
                            }
                        } catch(e) {
                            return { error: 'reCAPTCHA failed: ' + e.message };
                        }

                        const clientContext = {
                            projectId: projectId,
                            tool: 'PINHOLE',
                            userPaygateTier: 'PAYGATE_TIER_TWO',
                            sessionId: sessionId
                        };
                        if (recaptchaToken) {
                            clientContext.recaptchaContext = {
                                token: recaptchaToken,
                                applicationType: 'RECAPTCHA_APPLICATION_TYPE_WEB'
                            };
                        }

                        const payload = {
                            mediaGenerationContext: { batchId: batchId },
                            clientContext: clientContext,
                            requests: [{
                                clientContext: clientContext,
                                aspectRatio: ratio,
                                seed: seed,
                                textInput: { structuredPrompt: { parts: [{ text: prompt }] } },
                                videoModelKey: model,
                                metadata: {}
                            }],
                            useV2ModelConfig: true
                        };

                        const resp = await fetch(
                            'https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoText',
                            {
                                method: 'POST',
                                credentials: 'include',
                                headers: {
                                    'Content-Type': 'text/plain;charset=UTF-8',
                                    'Authorization': bearerToken
                                },
                                body: JSON.stringify(payload)
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
                [self._project_id, prompt, api_model, api_ratio, seed, session_id, batch_id, bearer],
            )

            return self._handle_result(result)

        except Exception as e:
            return None, str(e)[:200]
        finally:
            self.is_busy = False
            await self._maybe_refresh()

    def _handle_result(self, result):
        if not result:
            return None, "No response from tab"
        if result.get("error"):
            return None, result["error"]
        if result.get("success"):
            self.jobs_completed += 1
            return result.get("data", result), None
        return None, "Unknown response format"

    async def reload(self):
        """Reload tab to refresh reCAPTCHA."""
        try:
            self._log(f"[{self.slot_id}] Refreshing tab (job #{self.jobs_completed})...")
            await self._page.goto(FLOW_URL, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(2)
            await self.setup()
        except Exception as e:
            self._log(f"[{self.slot_id}] Reload error: {str(e)[:50]}")

    async def _maybe_refresh(self):
        """Refresh every 50 jobs."""
        if self.jobs_completed > 0 and self.jobs_completed % 50 == 0:
            await self.reload()

    async def close(self):
        pass  # Tab is managed by SharedBrowser


# ═══════════════════════════════════════════════════════════════════════════
# AccountBrowser — one CloakBrowser per account with N tabs
# ═══════════════════════════════════════════════════════════════════════════

class AccountBrowser:
    """One CloakBrowser per account. Creates and manages N TabWorkers."""

    def __init__(self, account_name, session_path, cookies_json_path, log_fn):
        self.account_name = account_name
        self._session_path = session_path
        self._cookies_json = cookies_json_path
        self._log = log_fn
        self._context = None
        self._project_id = None
        self._tabs = []
        self._bearer_token = None
        self._bearer_token_time = 0

    async def start(self, num_tabs, playwright_instance):
        """Launch browser and create N tabs."""
        self._log(f"[MultiTab:{self.account_name}] Starting 1 browser + {num_tabs} tabs...")

        try:
            success = await self._launch_browser(playwright_instance)
            if not success:
                return False

            # Navigate first page to Flow
            pages = list(getattr(self._context, "pages", []) or [])
            first_page = pages[0] if pages else await self._context.new_page()

            # Intercept requests to capture Bearer token
            self._setup_request_interception(first_page)

            try:
                await first_page.goto(FLOW_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                self._log(f"[MultiTab:{self.account_name}] Navigation error: {str(e)[:50]}")
                return False

            await asyncio.sleep(3)

            # Check for sign-in redirect
            url = first_page.url
            self._log(f"[MultiTab:{self.account_name}] URL: {url}")
            if "accounts.google.com" in url or "signin" in url.lower():
                self._log(f"[MultiTab:{self.account_name}] Redirected to sign-in — session invalid!")
                return False

            # Extract project ID
            self._project_id = await self._extract_project_id(first_page)

            # Try bot_engine cache
            if not self._project_id:
                try:
                    from src.core.bot_engine import GoogleLabsBot
                    self._project_id = GoogleLabsBot._shared_flow_project_id_by_account.get(
                        self.account_name
                    )
                    if self._project_id:
                        self._log(f"[MultiTab:{self.account_name}] Project ID from cache: {self._project_id}")
                except Exception:
                    pass

            if not self._project_id:
                self._log(f"[MultiTab:{self.account_name}] No project ID found!")
                return False

            self._log(f"[MultiTab:{self.account_name}] Project ID: {self._project_id}")

            # Verify reCAPTCHA on first page
            recap_ok = False
            for _ in range(3):
                try:
                    recap_ok = await first_page.evaluate(
                        "() => typeof grecaptcha !== 'undefined' && !!grecaptcha.enterprise"
                    )
                except Exception:
                    pass
                if recap_ok:
                    break
                await asyncio.sleep(2)

            if not recap_ok:
                self._log(f"[MultiTab:{self.account_name}] reCAPTCHA not loaded!")
                return False

            # Capture Bearer token
            if not self._bearer_token:
                await self._trigger_bearer_capture(first_page)

            if self._bearer_token:
                self._log(f"[MultiTab:{self.account_name}] Bearer token ready.")
            else:
                self._log(f"[MultiTab:{self.account_name}] Bearer token NOT captured!")
                return False

            # Create Tab 1 from first page
            tab1 = TabWorker(
                f"{self.account_name}#t1", first_page, self._project_id,
                self._log, get_bearer=self.get_bearer_token,
            )
            await tab1.setup()
            self._tabs.append(tab1)

            # Create remaining tabs
            for i in range(2, num_tabs + 1):
                try:
                    page = await self._context.new_page()
                    self._setup_request_interception(page)
                    await page.goto(FLOW_URL, wait_until="domcontentloaded", timeout=20000)
                    await asyncio.sleep(2)
                    tab = TabWorker(
                        f"{self.account_name}#t{i}", page, self._project_id,
                        self._log, get_bearer=self.get_bearer_token,
                    )
                    await tab.setup()
                    self._tabs.append(tab)
                    await asyncio.sleep(0.5)
                except Exception as e:
                    self._log(f"[MultiTab:{self.account_name}] Tab {i} failed: {str(e)[:60]}")

            est_ram = 300 + len(self._tabs) * 30
            self._log(
                f"[MultiTab:{self.account_name}] {len(self._tabs)} tabs ready. "
                f"Bearer: yes, Est. RAM: ~{est_ram}MB"
            )
            return len(self._tabs) > 0

        except Exception as e:
            self._log(f"[MultiTab:{self.account_name}] Start failed: {str(e)[:80]}")
            return False

    async def _launch_browser(self, playwright_instance):
        """Launch CloakBrowser or Playwright for GENERATION only (login already done)."""
        label = f"MultiTab:{self.account_name}"

        # Read display setting — respect visible/headless choice
        cloak_display = str(get_setting("cloak_display", "headless") or "headless").strip().lower()
        is_headless = cloak_display != "visible"
        self._log(f"[{label}] Display: {'headless' if is_headless else 'visible'}")

        # Load cookies from login session
        cookies = _load_cookies_from_json(self._cookies_json, self._log, label)
        self._log(f"[{label}] Cookies JSON: {self._cookies_json} ({len(cookies)} cookies)")

        # Always use a SEPARATE profile for multi-tab generation.
        # Login uses pure Chrome which locks the session_path — using the
        # same path causes "Target page, context" errors from lock conflicts.
        # Fresh profile + imported cookies works on ALL platforms.
        profile = self._session_path + "_multitab"
        os.makedirs(profile, exist_ok=True)
        _clean_lock_files(profile)
        # Also clean the source session in case locks leaked
        _clean_lock_files(self._session_path)

        self._log(f"[{label}] Profile: {profile} (fresh + imported cookies)")

        # Try CloakBrowser
        try:
            cloak_api = load_cloakbrowser_api()
            cloak_persistent = cloak_api.get("persistent_async")

            if cloak_api.get("available") and cloak_persistent:
                seed = random.randint(10000, 99999)
                self._context = await cloak_persistent(
                    profile, headless=is_headless,
                    args=[
                        f"--fingerprint={seed}",
                        "--disable-gpu",
                        "--disable-software-rasterizer",
                        "--disable-dev-shm-usage",
                    ],
                    humanize=True,
                )

                # Import cookies (essential on Mac, harmless on Windows)
                imported = await self._import_cookies(cookies)
                try:
                    actual = await self._context.cookies()
                    actual_count = len(actual)
                except Exception:
                    actual_count = imported
                self._log(
                    f"[{label}] CloakBrowser started ({'visible' if not is_headless else 'headless'}). "
                    f"Imported: {imported}, Browser: {actual_count} cookies."
                )
                if actual_count == 0:
                    self._log(f"[{label}] NO cookies! Check: {self._cookies_json}")
                return True
        except Exception as e:
            self._log(f"[{label}] CloakBrowser failed: {str(e)[:120]}")

        # Fallback: Playwright persistent context
        try:
            _clean_lock_files(self._session_path)
            self._context = await playwright_instance.chromium.launch_persistent_context(
                self._session_path, headless=is_headless,
                args=["--disable-gpu", "--no-sandbox", "--disable-blink-features=AutomationControlled",
                      "--disable-dev-shm-usage"],
                ignore_default_args=["--enable-automation"],
            )
            imported = await self._import_cookies(cookies)
            self._log(f"[{label}] Playwright started ({imported} cookies imported).")
            return True
        except Exception as e:
            self._log(f"[{label}] Browser launch failed: {str(e)[:200]}")
            return False

    async def _import_cookies(self, cookies):
        """Import cookies into browser. Returns count imported."""
        if not cookies or not self._context:
            return 0
        try:
            await self._context.add_cookies(cookies)
            return len(cookies)
        except Exception:
            imported = 0
            for c in cookies:
                try:
                    await self._context.add_cookies([c])
                    imported += 1
                except Exception:
                    pass
            return imported

    def _setup_request_interception(self, page):
        """Intercept outgoing requests to capture Bearer token."""
        def _on_request(request):
            if "aisandbox-pa.googleapis.com" in request.url and request.method == "POST":
                auth = request.headers.get("authorization", "")
                if auth.startswith("Bearer "):
                    self._bearer_token = auth
                    self._bearer_token_time = time.time()
                    self._log(f"[MultiTab:{self.account_name}] Bearer token captured (len={len(auth)})")
        page.on("request", _on_request)

    async def _trigger_bearer_capture(self, page):
        """Trigger a fetch from the page to capture Chrome's OAuth Bearer token."""
        self._log(f"[MultiTab:{self.account_name}] Triggering Bearer capture...")
        # Method 1: credits endpoint (lightweight GET)
        try:
            await page.evaluate("""
                async () => {
                    try { await fetch('https://aisandbox-pa.googleapis.com/v1/credits',
                          {method:'GET', credentials:'include'}); } catch(e) {}
                }
            """)
            await asyncio.sleep(2)
        except Exception:
            pass

        if self._bearer_token:
            return

        # Method 2: dummy POST with reCAPTCHA
        try:
            await page.evaluate("""
                async () => {
                    try {
                        const scripts = document.querySelectorAll("script[src*='recaptcha'][src*='render=']");
                        let sk = null;
                        for (const s of scripts) {
                            const m = s.src.match(/render=([^&]+)/);
                            if (m && m[1] !== 'explicit') { sk = m[1]; break; }
                        }
                        if (!sk || !grecaptcha || !grecaptcha.enterprise) return;
                        const tok = await grecaptcha.enterprise.execute(sk, {action:'generate'});
                        await fetch('https://aisandbox-pa.googleapis.com/v1/credits',
                            {method:'POST', credentials:'include',
                             headers:{'Content-Type':'text/plain;charset=UTF-8'},
                             body:JSON.stringify({token:tok})});
                    } catch(e) {}
                }
            """)
            await asyncio.sleep(2)
        except Exception:
            pass

    def get_bearer_token(self):
        """Get captured Bearer token. Called by TabWorkers."""
        return self._bearer_token

    async def _extract_project_id(self, page):
        """Extract project ID from URL or DOM."""
        url = page.url

        # /project/{id} pattern (bot_engine style)
        match = re.search(r"/project/([a-z0-9-]{16,})", url, re.IGNORECASE)
        if match:
            return match.group(1)

        # /flow/{uuid} pattern
        match = re.search(r"/flow/([a-f0-9-]{36})", url)
        if match:
            return match.group(1)

        # DOM hints
        try:
            pid = await page.evaluate("""
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
            if pid:
                return pid
        except Exception:
            pass

        # Click "New project"
        try:
            btn = page.locator(
                "//*[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                " 'abcdefghijklmnopqrstuvwxyz'), 'new project')]"
            ).first
            if await btn.is_visible():
                await btn.click(force=True)
                await asyncio.sleep(5)
                url = page.url
                match = re.search(r"/project/([a-z0-9-]{16,})", url, re.IGNORECASE)
                if match:
                    return match.group(1)
        except Exception:
            pass

        return None

    def get_available_tab(self):
        for tab in self._tabs:
            if not tab.is_busy:
                return tab
        return None

    def get_all_tabs(self):
        return list(self._tabs)

    async def stop(self):
        for tab in self._tabs:
            tab.is_busy = False
        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass
        self._context = None
        total = sum(t.jobs_completed for t in self._tabs)
        self._log(f"[MultiTab:{self.account_name}] Stopped. {total} jobs across {len(self._tabs)} tabs.")


# ═══════════════════════════════════════════════════════════════════════════
# MultiTabManager — orchestrates account browsers + tab workers
# ═══════════════════════════════════════════════════════════════════════════

class MultiTabManager:
    """
    Manages AccountBrowsers and dispatches jobs to TabWorkers.
    Receives the AsyncQueueManager instance for signals and settings.
    """

    def __init__(self, queue_manager):
        self.qm = queue_manager
        self._log = lambda msg: queue_manager.signals.log_msg.emit(msg)
        self._browsers = {}   # account_name -> AccountBrowser
        self._active_tasks = []

    async def run(self):
        """Main entry — start browsers with tabs, dispatch jobs."""
        from playwright.async_api import async_playwright

        all_accs = get_accounts()
        if not all_accs:
            self._log("[MultiTab] No accounts configured.")
            return

        slots_per_account = max(1, min(40, get_int_setting("slots_per_account", 5)))
        self._log(
            f"[MultiTab] Starting: {len(all_accs)} account(s), "
            f"{slots_per_account} tab(s) each."
        )

        async with async_playwright() as p:
            try:
                # Start 1 browser per account with N tabs
                for acc in all_accs:
                    name = acc.get("name", "unknown")
                    session_path = acc.get("session_path", os.path.join(DATA_DIR, name))
                    cookies_json = os.path.join(session_path, "exported_cookies.json")

                    browser = AccountBrowser(name, session_path, cookies_json, self._log)
                    success = await browser.start(slots_per_account, p)

                    if success:
                        self._browsers[name] = browser
                    else:
                        self._log(f"[MultiTab] {name}: Failed! Account skipped.")

                total_tabs = sum(len(b.get_all_tabs()) for b in self._browsers.values())
                total_browsers = len(self._browsers)

                if total_tabs == 0:
                    self._log("[MultiTab] No tabs started. Cannot proceed.")
                    return

                est_ram = total_browsers * 300 + total_tabs * 30
                self._log(
                    f"[MultiTab] Total: {total_browsers} browser(s), "
                    f"{total_tabs} tab(s). Est. RAM: ~{est_ram}MB."
                )

                # Main dispatch loop
                while self.qm.is_running:
                    if self.qm.stop_requested or self.qm.force_stop_requested:
                        break
                    if self.qm.pause_requested:
                        await asyncio.sleep(1)
                        continue

                    # Prune done tasks
                    self._active_tasks = [t for t in self._active_tasks if not t.done()]

                    # Get pending jobs
                    jobs = get_all_jobs()
                    pending = [j for j in jobs if j["status"] == "pending"]

                    if not pending:
                        if not self._active_tasks:
                            still_active = any(
                                j["status"] in ("pending", "running") for j in get_all_jobs()
                            )
                            if not still_active:
                                self._log("[MultiTab] All jobs completed.")
                                break
                        await asyncio.sleep(self.qm.scheduler_poll_seconds)
                        continue

                    # Find busy slots
                    busy_slots = set()
                    for t in self._active_tasks:
                        n = t.get_name() if hasattr(t, "get_name") else ""
                        if n:
                            busy_slots.add(n)

                    dispatched = 0

                    # ── Same-account stagger: with multiple tabs on a
                    # single CloakBrowser, firing 5 jobs within 2 s
                    # triggers Google Flow's "Internal error encountered"
                    # burst rate-limit. The setting already exists
                    # (self.qm.same_account_stagger_seconds, default 1 s)
                    # but this dispatcher wasn't consulting it. Now it
                    # does — 5 jobs spread over ~5 s, zero burst errors.
                    acct_stagger = max(0.0, float(self.qm.same_account_stagger_seconds))
                    now_ts = time.time()
                    stagger_blocked = set()
                    if acct_stagger > 0:
                        for acc_name, last_at in self.qm.last_account_dispatch_at.items():
                            if now_ts - last_at < acct_stagger:
                                stagger_blocked.add(acc_name)

                    for job in pending:
                        tab = self._get_available_tab(busy_slots, stagger_blocked=stagger_blocked)
                        if not tab:
                            break

                        job_id = job["id"]
                        update_job_status(job_id, "running", account=tab.account_name)
                        self.qm.signals.job_updated.emit(job_id, "running", tab.account_name, "")

                        # Record dispatch BEFORE task creation so further
                        # iterations in this same poll cycle see the
                        # account as freshly-dispatched.
                        self.qm.last_account_dispatch_at[tab.account_name] = time.time()
                        if acct_stagger > 0:
                            stagger_blocked.add(tab.account_name)

                        task = asyncio.create_task(
                            self._run_job(tab, job), name=tab.slot_id,
                        )
                        self._active_tasks.append(task)
                        busy_slots.add(tab.slot_id)
                        dispatched += 1

                        stagger = random.uniform(
                            self.qm.global_stagger_min_seconds,
                            self.qm.global_stagger_max_seconds,
                        )
                        if stagger > 0:
                            await asyncio.sleep(stagger)

                    if dispatched == 0:
                        await asyncio.sleep(self.qm.scheduler_poll_seconds)

                # Wait for active tasks
                if self._active_tasks:
                    if self.qm.stop_requested or self.qm.force_stop_requested:
                        self._log(f"[MultiTab] Cancelling {len(self._active_tasks)} active job(s)...")
                        for t in self._active_tasks:
                            if not t.done():
                                t.cancel()
                        try:
                            await asyncio.wait_for(
                                asyncio.gather(*self._active_tasks, return_exceptions=True),
                                timeout=3.0,
                            )
                        except asyncio.TimeoutError:
                            self._log("[MultiTab] Some tasks didn't cancel in 3s — continuing.")
                    else:
                        self._log(f"[MultiTab] Waiting for {len(self._active_tasks)} active job(s)...")
                        await asyncio.gather(*self._active_tasks, return_exceptions=True)

            finally:
                for browser in self._browsers.values():
                    await browser.stop()
                self._browsers.clear()
                self._log("[MultiTab] All browsers and tabs stopped.")

    def _get_available_tab(self, busy_slots, stagger_blocked=None):
        """Return the next idle tab, skipping accounts whose
        same-account dispatch stagger hasn't elapsed yet.
        """
        blocked = stagger_blocked or set()
        for browser in self._browsers.values():
            if browser.account_name in blocked:
                continue
            for tab in browser.get_all_tabs():
                if tab.slot_id not in busy_slots and not tab.is_busy:
                    return tab
        return None

    async def _run_job(self, tab, job):
        """Execute a single job on a tab with retries."""
        job_id = job["id"]
        job_type = job.get("job_type", "image")
        prompt = job.get("prompt", "")
        model = job.get("model", "")

        self._log(f"[{tab.slot_id}] Processing job {job_id[:6]}...: {prompt[:40]}...")

        max_retries = max(1, get_int_setting("max_auto_retries_per_job", 3))
        last_error = ""

        for attempt in range(max_retries + 1):
            try:
                if "video" in job_type:
                    video_model = job.get("video_model") or model
                    ratio = job.get("aspect_ratio", "ASPECT_RATIO_16_9")
                    result, error = await tab.generate_video(prompt, video_model, ratio)
                else:
                    ratio = job.get("aspect_ratio", "IMAGE_ASPECT_RATIO_LANDSCAPE")
                    result, error = await tab.generate_image(prompt, model, ratio)

                if result and not error:
                    update_job_status(job_id, "completed", account=tab.account_name)
                    self.qm.signals.job_updated.emit(job_id, "completed", tab.account_name, "")
                    self._log(f"[{tab.slot_id}] Job {job_id[:6]}... completed!")
                    return

                last_error = error or "Unknown error"
                self._log(
                    f"[{tab.slot_id}] Attempt {attempt + 1}/{max_retries + 1} "
                    f"failed: {last_error[:200]}"
                )

                # Handle specific errors
                err_lower = last_error.lower()
                if "recaptcha" in err_lower:
                    self._log(f"[{tab.slot_id}] reCAPTCHA issue — reloading tab...")
                    await tab.reload()
                    await asyncio.sleep(3)
                elif "401" in last_error or "auth" in err_lower:
                    self._log(f"[{tab.slot_id}] Auth issue — refreshing Bearer + reloading tab...")
                    # Force Bearer refresh on the account browser
                    for browser in self._browsers.values():
                        if tab.account_name == browser.account_name:
                            browser._bearer_token = None
                            first_tab_page = browser.get_all_tabs()[0]._page if browser.get_all_tabs() else None
                            if first_tab_page:
                                await browser._trigger_bearer_capture(first_tab_page)
                            break
                    await tab.reload()
                    await asyncio.sleep(3)
                elif "internal error" in err_lower or "500" in last_error:
                    # Google Flow backend flakiness — reload the tab to
                    # rotate load-balancer session stickiness so the
                    # retry lands on a fresh backend instead of the same
                    # degraded one.
                    if attempt < max_retries:
                        self._log(
                            f"[{tab.slot_id}] Internal error — reloading tab to rotate backend..."
                        )
                        await tab.reload()
                        await asyncio.sleep(5 + attempt * 5)
                elif attempt < max_retries:
                    await asyncio.sleep(10 * (attempt + 1))

            except Exception as e:
                last_error = str(e)[:200]
                self._log(f"[{tab.slot_id}] Attempt {attempt + 1} exception: {last_error}")
                if attempt < max_retries:
                    await asyncio.sleep(10)

        # All retries exhausted
        update_job_status(job_id, "failed", account=tab.account_name, error=last_error)
        self.qm.signals.job_updated.emit(job_id, "failed", tab.account_name, last_error)
        self._log(f"[{tab.slot_id}] Job {job_id[:6]}... FAILED: {last_error[:200]}")
