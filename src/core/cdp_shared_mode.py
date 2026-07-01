"""
CDP Shared Browser Mode — 1 CloakBrowser process per account, N contexts via CDP.

Each context = independent cookies, independent session, independent reCAPTCHA.
Uses SAME page.evaluate() API calls as proven bot_engine (not fetch/HTTP).

RAM: 300MB base + ~30MB per context
  20 slots = ~900MB total (vs ~6GB with browser-per-slot)
"""

import asyncio
import json
import os
import re
import time
import random
import platform
import subprocess

from src.core.app_paths import get_sessions_dir
from src.core.process_tracker import process_tracker, cleanup_session_locks
from src.core.cloakbrowser_support import load_cloakbrowser_api
from src.core.recaptcha_mainworld import get_recaptcha_token_mainworld
from src.db.db_manager import (
    get_accounts,
    get_all_jobs,
    get_int_setting,
    get_setting,
    update_job_status,
)

DATA_DIR = str(get_sessions_dir())


# ═══════════════════════════════════════════════════════════════════════════
# Model / ratio resolvers (same as bot_engine)
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


def _fix_cookies_for_import(cookies):
    """Fix sameSite/secure mismatch before importing."""
    fixed = []
    for c in cookies:
        if not c.get("name") or not c.get("value") or not c.get("domain"):
            continue
        same_site = c.get("sameSite", "Lax")
        secure = c.get("secure", False)
        if same_site == "None" and not secure:
            c["sameSite"] = "Lax"
        if same_site not in ("Strict", "Lax", "None"):
            c["sameSite"] = "Lax"
        fixed.append(c)
    return fixed


# ═══════════════════════════════════════════════════════════════════════════
# CDPSlotWorker — one context in the shared browser
# ═══════════════════════════════════════════════════════════════════════════

# The SAME JavaScript that bot_engine uses for image generation.
# Handles auth session, reCAPTCHA, and API call all in one evaluate().
_IMAGE_GENERATE_JS = """
async ({ projectId, prompt, modelName, aspectRatio, batchId, seed, recaptchaAction, referenceMediaIds, cachedToken }) => {
    const getAuthSession = async () => {
        try {
            const resp = await fetch("https://labs.google/fx/api/auth/session", {
                method: "GET", credentials: "include",
            });
            if (!resp.ok) return null;
            const data = await resp.json().catch(() => null);
            if (!data || !data.access_token) return null;
            return data;
        } catch { return null; }
    };

    // Fix 3: soft-reset reCAPTCHA state before executing — avoids
    // stale widget state from previous failed calls. Much faster than
    // reloading the page (~50ms vs ~5000ms).
    const softResetRecaptcha = () => {
        try {
            const enterprise = window.grecaptcha?.enterprise;
            if (enterprise && typeof enterprise.reset === "function") {
                enterprise.reset();
            }
        } catch {}
    };

    const getRecaptchaContext = async (useReset) => {
        try {
            const enterprise = window.grecaptcha?.enterprise;
            if (!enterprise || typeof enterprise.execute !== "function") return null;
            const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

            // Fix 3: on retry after error, reset reCAPTCHA first
            if (useReset) softResetRecaptcha();

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
                await new Promise((r) => enterprise.ready(r));
            }
            const token = await enterprise.execute(siteKey, { action: recaptchaAction });
            if (!token) return null;
            return { token, applicationType: "RECAPTCHA_APPLICATION_TYPE_WEB" };
        } catch { return null; }
    };

    const authSession = await getAuthSession();
    if (!authSession || !authSession.access_token) {
        return { ok: false, status: 0, error: "missing auth session access token" };
    }

    // Fix 5: Use cached token if provided by Python (pre-generated
    // in background) — saves ~200-500ms per job
    let recaptchaContext = null;
    if (cachedToken) {
        recaptchaContext = { token: cachedToken, applicationType: "RECAPTCHA_APPLICATION_TYPE_WEB" };
    } else {
        recaptchaContext = await getRecaptchaContext(false);
    }
    const clientContext = { projectId, tool: "PINHOLE", sessionId: ";" + Date.now() };
    if (recaptchaContext) clientContext.recaptchaContext = recaptchaContext;

    const refs = Array.isArray(referenceMediaIds) && referenceMediaIds.length > 0
        ? referenceMediaIds.map((id) => ({ imageInputType: "IMAGE_INPUT_TYPE_REFERENCE", name: id }))
        : [];

    const body = {
        clientContext,
        mediaGenerationContext: { batchId },
        useNewMedia: true,
        requests: [{
            clientContext,
            imageModelName: modelName,
            imageAspectRatio: aspectRatio,
            structuredPrompt: { parts: [{ text: String(prompt || "") }] },
            seed,
            imageInputs: refs,
        }],
    };

    try {
        const resp = await fetch(
            `https://aisandbox-pa.googleapis.com/v1/projects/${projectId}/flowMedia:batchGenerateImages`,
            {
                method: "POST", credentials: "include",
                headers: { "content-type": "text/plain;charset=UTF-8", "authorization": `Bearer ${authSession.access_token}` },
                body: JSON.stringify(body),
            }
        );
        const text = await resp.text();
        let data = null;
        try { data = JSON.parse(text); } catch {}
        if (!resp.ok) {
            return { ok: false, status: resp.status, error: (data?.error?.message || data?.error?.status || text.slice(0, 300) || `HTTP ${resp.status}`) };
        }

        // Extract fifeUrl + mediaName (same as bot_engine)
        const mediaList = Array.isArray(data?.media) ? data.media : [];
        const firstMedia = mediaList.length ? mediaList[0] : null;
        const selectedMedia = mediaList.find((item) => {
            const itemName = item?.name || "";
            const itemUrl = item?.image?.generatedImage?.fifeUrl || "";
            if (Array.isArray(referenceMediaIds) && referenceMediaIds.includes(itemName)) return false;
            return Boolean(itemUrl || itemName);
        }) || firstMedia;
        const workflowList = Array.isArray(data?.workflows) ? data.workflows : [];
        const firstWorkflow = workflowList.length ? workflowList[0] : null;
        const fifeUrl = selectedMedia?.image?.generatedImage?.fifeUrl || "";
        const primaryMediaId = firstWorkflow?.metadata?.primaryMediaId || "";
        const mediaName =
            (Array.isArray(referenceMediaIds) && referenceMediaIds.includes(primaryMediaId) ? "" : primaryMediaId) ||
            selectedMedia?.name || firstMedia?.name || "";

        return { ok: true, status: resp.status, fifeUrl, mediaName, data };
    } catch (e) {
        return { ok: false, status: 0, error: String(e) };
    }
}
"""

_VIDEO_GENERATE_JS = """
async ({ projectId, prompt, modelKey, aspectRatio, batchId, seed, recaptchaAction, cachedToken }) => {
    const getAuthSession = async () => {
        try {
            const resp = await fetch("https://labs.google/fx/api/auth/session", { method: "GET", credentials: "include" });
            if (!resp.ok) return null;
            const data = await resp.json().catch(() => null);
            return (data && data.access_token) ? data : null;
        } catch { return null; }
    };

    const softResetRecaptcha = () => {
        try {
            const e = window.grecaptcha?.enterprise;
            if (e && typeof e.reset === "function") e.reset();
        } catch {}
    };

    const getRecaptchaContext = async () => {
        try {
            const enterprise = window.grecaptcha?.enterprise;
            if (!enterprise) return null;
            const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
            let siteKey = null;
            for (const s of document.querySelectorAll("script[src*='recaptcha'][src*='render=']")) {
                try { const m = new URL(s.src).searchParams.get("render"); if (m && m !== "explicit") { siteKey = m; break; } } catch {}
            }
            if (!siteKey) return null;
            if (typeof enterprise.ready === "function") await new Promise((r) => enterprise.ready(r));
            const token = await enterprise.execute(siteKey, { action: recaptchaAction });
            if (!token) return null;
            return { token, applicationType: "RECAPTCHA_APPLICATION_TYPE_WEB" };
        } catch { return null; }
    };

    const auth = await getAuthSession();
    if (!auth) return { ok: false, error: "missing auth session access token" };

    // Fix 5: Use cached token if available
    let recap = null;
    if (cachedToken) {
        recap = { token: cachedToken, applicationType: "RECAPTCHA_APPLICATION_TYPE_WEB" };
    } else {
        recap = await getRecaptchaContext();
    }
    const ctx = { projectId, tool: "PINHOLE", userPaygateTier: "PAYGATE_TIER_TWO", sessionId: ";" + Date.now() };
    if (recap) ctx.recaptchaContext = recap;

    const body = {
        mediaGenerationContext: { batchId },
        clientContext: ctx,
        requests: [{ clientContext: ctx, aspectRatio, seed, textInput: { structuredPrompt: { parts: [{ text: prompt }] } }, videoModelKey: modelKey, metadata: {} }],
        useV2ModelConfig: true,
    };

    try {
        const resp = await fetch("https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoText", {
            method: "POST", credentials: "include",
            headers: { "content-type": "text/plain;charset=UTF-8", "authorization": `Bearer ${auth.access_token}` },
            body: JSON.stringify(body),
        });
        const text = await resp.text();
        let data = null; try { data = JSON.parse(text); } catch {}
        if (!resp.ok) return { ok: false, status: resp.status, error: (data?.error?.message || text.slice(0, 300)) };
        return { ok: true, status: resp.status, data };
    } catch (e) { return { ok: false, error: String(e) }; }
}
"""


class CDPSlotWorker:
    """One context in the shared CDP browser. Uses SAME API code as bot_engine."""

    def __init__(self, slot_id, context, page, project_id, log_fn):
        self.slot_id = slot_id
        self.account_name = slot_id.split("#")[0] if "#" in slot_id else slot_id
        self.context = context
        self._page = page
        self._project_id = project_id
        self._log = log_fn
        self.is_busy = False
        self.jobs_completed = 0

        # Health tracking (Fix 3 + Fix 7)
        self.consecutive_failures = 0
        self.quarantined_until = 0.0  # timestamp; slot skipped until this time
        self.last_success_at = 0.0
        self.last_failure_reason = ""

        # Pre-generated reCAPTCHA token cache (Fix 4)
        self._cached_token = None
        self._cached_token_expires = 0.0  # reCAPTCHA tokens expire in ~120s

    def is_healthy(self, now=None):
        """Check if slot can accept new jobs (not quarantined, not busy)."""
        if self.is_busy:
            return False
        if now is None:
            now = time.time()
        return self.quarantined_until <= now

    def note_failure(self, reason=""):
        """Record a failure. After 3 consecutive failures, quarantine for 60s."""
        self.consecutive_failures += 1
        self.last_failure_reason = reason[:80]
        if self.consecutive_failures >= 3:
            self.quarantined_until = time.time() + 60
            self._log(
                f"[{self.slot_id}] Quarantined for 60s after {self.consecutive_failures} "
                f"consecutive failures (last: {reason[:60]})"
            )
            self.consecutive_failures = 0  # reset after quarantine applied

    def note_success(self):
        """Reset failure counters on success."""
        self.consecutive_failures = 0
        self.last_success_at = time.time()
        self.quarantined_until = 0.0
        self.last_failure_reason = ""

    async def pre_generate_token(self, action="IMAGE_GENERATION"):
        """Fix 5: Pre-generate reCAPTCHA token in background.

        reCAPTCHA Enterprise tokens expire in ~120s. We can generate them
        in advance and use them when a job arrives, saving ~200-500ms
        per job since execute() doesn't run synchronously during the
        generation call.
        """
        if self.is_busy:
            return
        try:
            token = await self._page.evaluate(
                """
                async (action) => {
                    try {
                        const enterprise = window.grecaptcha?.enterprise;
                        if (!enterprise || typeof enterprise.execute !== "function") return null;
                        let siteKey = null;
                        for (const s of document.querySelectorAll("script[src*='recaptcha'][src*='render=']")) {
                            try {
                                const r = new URL(s.src).searchParams.get("render");
                                if (r && r !== "explicit") { siteKey = r; break; }
                            } catch {}
                        }
                        if (!siteKey) return null;
                        if (typeof enterprise.ready === "function") {
                            await new Promise((r) => enterprise.ready(r));
                        }
                        return await enterprise.execute(siteKey, { action });
                    } catch { return null; }
                }
                """,
                action,
            )
            if token:
                self._cached_token = token
                # reCAPTCHA Enterprise tokens expire after ~120s, cache
                # for 90s to have a safety margin
                self._cached_token_expires = time.time() + 90
        except Exception:
            pass

    def _consume_cached_token(self):
        """Return cached token if still valid, else None. Consumes on read."""
        if self._cached_token and time.time() < self._cached_token_expires:
            token = self._cached_token
            self._cached_token = None
            self._cached_token_expires = 0.0
            return token
        return None

    async def soft_reset_recaptcha(self):
        """Fix 3: Soft-reset reCAPTCHA without full page reload.
        ~50ms vs ~5000ms for full reload. Use after reCAPTCHA failures."""
        try:
            await self._page.evaluate(
                """
                () => {
                    try {
                        const e = window.grecaptcha?.enterprise;
                        if (e && typeof e.reset === "function") e.reset();
                    } catch {}
                }
                """
            )
        except Exception:
            pass

    async def generate_image(self, prompt, model, ratio, references=None):
        """Generate image using SAME page.evaluate JS as bot_engine.

        Includes inline retry for transient "Internal error" responses —
        Google's backend randomly returns these, retrying with a new seed
        within the same tab usually succeeds (80%+ pass rate).

        Returns the FULL JS result dict (with fifeUrl, mediaName, data).
        """
        self.is_busy = True
        try:
            api_model = _resolve_image_model(model)
            api_ratio = _resolve_image_ratio(ratio)

            # Inline retry for transient server errors (Fix 5)
            # 2 attempts with fresh seed each time, 2s gap
            INLINE_MAX_ATTEMPTS = 2
            INLINE_GAP_SECONDS = 2

            last_error = None
            for inline_attempt in range(1, INLINE_MAX_ATTEMPTS + 1):
                seed = random.randint(100000, 999999)
                batch_id = f"{random.getrandbits(128):032x}"

                if inline_attempt == 1:
                    self._log(f"[{self.slot_id}] Image: {api_model}, {api_ratio}")
                else:
                    self._log(
                        f"[{self.slot_id}] Inline retry #{inline_attempt}/{INLINE_MAX_ATTEMPTS} "
                        f"with new seed (prev: {last_error[:60] if last_error else '?'})"
                    )

                # Main-world reCAPTCHA: get token via <script> tag injection
                # (no CDP traces, like competitor's Chrome Extension approach)
                # Falls back to inline evaluate if main-world fails
                cached = None
                try:
                    cached = await get_recaptcha_token_mainworld(
                        self._page, "IMAGE_GENERATION", self._log
                    )
                except Exception:
                    pass
                # If main-world failed, try pre-generated cache on attempt 1
                if not cached and inline_attempt == 1:
                    cached = self._consume_cached_token()

                try:
                    result = await self._page.evaluate(
                        _IMAGE_GENERATE_JS,
                        {
                            "projectId": self._project_id,
                            "prompt": prompt,
                            "modelName": api_model,
                            "aspectRatio": api_ratio,
                            "batchId": batch_id,
                            "seed": seed,
                            "recaptchaAction": "IMAGE_GENERATION",
                            "referenceMediaIds": references or [],
                            "cachedToken": cached,
                        },
                    )
                except Exception as e:
                    last_error = f"page.evaluate exception: {str(e)[:150]}"
                    # On exception, try soft reset + next attempt
                    if inline_attempt < INLINE_MAX_ATTEMPTS:
                        await self.soft_reset_recaptcha()
                        await asyncio.sleep(INLINE_GAP_SECONDS)
                    continue

                if not result:
                    last_error = "No response from page.evaluate"
                    if inline_attempt < INLINE_MAX_ATTEMPTS:
                        await asyncio.sleep(INLINE_GAP_SECONDS)
                    continue

                if result.get("ok"):
                    self.jobs_completed += 1
                    self.note_success()  # clear failure counter
                    return result, None

                # Failure — decide if inline retry makes sense
                error_text = str(result.get("error", "Unknown error"))
                last_error = error_text
                error_lower = error_text.lower()

                # Fix 3: reCAPTCHA errors get soft-reset + inline retry
                is_recaptcha = (
                    "recaptcha" in error_lower
                    or "evaluation failed" in error_lower
                )

                # Retry inline for transient server errors OR reCAPTCHA errors
                transient = is_recaptcha or any(t in error_lower for t in (
                    "internal error", "backend error", "service unavailable",
                    "http 500", "http 502", "http 503", "http 504",
                    "rpc failed", "connection reset", "deadline exceeded",
                ))

                if not transient:
                    # Non-transient error — bail out, let queue_manager handle
                    self.note_failure(error_text)
                    return None, error_text

                # Transient — quick retry with new seed
                if inline_attempt < INLINE_MAX_ATTEMPTS:
                    if is_recaptcha:
                        # Soft-reset reCAPTCHA (Fix 3) — 50ms vs 5s full reload
                        self._log(f"[{self.slot_id}] reCAPTCHA error — soft reset + retry")
                        await self.soft_reset_recaptcha()
                    await asyncio.sleep(INLINE_GAP_SECONDS)
                    continue

            # All inline attempts exhausted
            self.note_failure(last_error or "inline retries exhausted")
            return None, last_error or "inline retries exhausted"

        except Exception as e:
            await self._try_reload()
            self.note_failure(str(e)[:80])
            return None, str(e)[:200]
        finally:
            self.is_busy = False
            await self._maybe_refresh()

    async def generate_video(self, prompt, model, ratio):
        """Generate video using SAME page.evaluate JS as bot_engine.

        Includes inline retry for transient Internal errors — same as image.
        Returns the FULL JS result dict so _save_generation_result can see
        whatever download fields the JS exposes.
        """
        self.is_busy = True
        try:
            api_model = _resolve_video_model(model)
            api_ratio = _resolve_video_ratio(ratio)

            INLINE_MAX_ATTEMPTS = 2
            INLINE_GAP_SECONDS = 2

            last_error = None
            for inline_attempt in range(1, INLINE_MAX_ATTEMPTS + 1):
                seed = random.randint(100000, 999999)
                batch_id = f"{random.getrandbits(128):032x}"

                if inline_attempt == 1:
                    self._log(f"[{self.slot_id}] Video: {api_model}, {api_ratio}")
                else:
                    self._log(
                        f"[{self.slot_id}] Video inline retry #{inline_attempt} "
                        f"with new seed (prev: {last_error[:60] if last_error else '?'})"
                    )

                # Main-world reCAPTCHA token (same approach as image)
                cached = None
                try:
                    cached = await get_recaptcha_token_mainworld(
                        self._page, "VIDEO_GENERATION", self._log
                    )
                except Exception:
                    pass
                if not cached and inline_attempt == 1:
                    cached = self._consume_cached_token()

                try:
                    result = await self._page.evaluate(
                        _VIDEO_GENERATE_JS,
                        {
                            "projectId": self._project_id,
                            "prompt": prompt,
                            "modelKey": api_model,
                            "aspectRatio": api_ratio,
                            "batchId": batch_id,
                            "seed": seed,
                            "recaptchaAction": "VIDEO_GENERATION",
                            "cachedToken": cached,
                        },
                    )
                except Exception as e:
                    last_error = f"page.evaluate exception: {str(e)[:150]}"
                    if inline_attempt < INLINE_MAX_ATTEMPTS:
                        await self.soft_reset_recaptcha()
                        await asyncio.sleep(INLINE_GAP_SECONDS)
                    continue

                if not result:
                    last_error = "No response"
                    if inline_attempt < INLINE_MAX_ATTEMPTS:
                        await asyncio.sleep(INLINE_GAP_SECONDS)
                    continue

                if result.get("ok"):
                    self.jobs_completed += 1
                    self.note_success()
                    return result, None

                error_text = str(result.get("error", "Unknown error"))
                last_error = error_text
                error_lower = error_text.lower()

                is_recaptcha = (
                    "recaptcha" in error_lower
                    or "evaluation failed" in error_lower
                )
                transient = is_recaptcha or any(t in error_lower for t in (
                    "internal error", "backend error", "service unavailable",
                    "http 500", "http 502", "http 503", "http 504",
                    "rpc failed", "deadline exceeded",
                ))

                if not transient:
                    self.note_failure(error_text)
                    return None, error_text

                if inline_attempt < INLINE_MAX_ATTEMPTS:
                    if is_recaptcha:
                        self._log(f"[{self.slot_id}] reCAPTCHA error — soft reset + retry")
                        await self.soft_reset_recaptcha()
                    await asyncio.sleep(INLINE_GAP_SECONDS)
                    continue

            self.note_failure(last_error or "inline retries exhausted")
            return None, last_error or "inline retries exhausted"

        except Exception as e:
            await self._try_reload()
            self.note_failure(str(e)[:80])
            return None, str(e)[:200]
        finally:
            self.is_busy = False
            await self._maybe_refresh()

    async def _save_generation_result(self, result, job, media_tag="image"):
        """
        Download + save generated media to disk.

        Tries multiple response formats (in order):
          1. Top-level fifeUrl / mediaName (extracted by our JS — same as bot_engine)
          2. Walk result["data"]["media"][].image.generatedImage.fifeUrl (raw API shape)
          3. Alternate formats: generatedMedias / responses[].generatedMedias /
             mediaGenerations / results — with encodedImage (base64) or uri/url
          4. Deep recursive walk as last resort: find ANY string that looks like a
             downloadable image URL or looks like a media id we can redirect on.

        Returns list of saved file paths.
        """
        import base64 as _b64

        saved_files = []
        try:
            from src.db.db_manager import get_output_directory
            output_dir = str(get_output_directory())
            os.makedirs(output_dir, exist_ok=True)

            if not isinstance(result, dict):
                self._log(f"[{self.slot_id}] Save: result is not dict ({type(result).__name__})")
                return saved_files

            # ── DEBUG: log response structure (first time per slot only) ──
            try:
                top_keys = list(result.keys())
                self._log(f"[{self.slot_id}] DEBUG response keys: {top_keys}")
                data_blob = result.get("data") or {}
                if isinstance(data_blob, dict):
                    self._log(f"[{self.slot_id}] DEBUG data keys: {list(data_blob.keys())}")
                # Short preview (avoid flooding logs)
                preview = json.dumps(result, default=str)[:400]
                self._log(f"[{self.slot_id}] DEBUG preview: {preview}")
            except Exception:
                pass

            request_ctx = self.context.request if self.context else None

            # Helper: download a URL via playwright request context (carries cookies)
            async def _download_and_save(url, media_tag_inner):
                if not url or request_ctx is None:
                    return None
                try:
                    resp = await request_ctx.get(url, timeout=90000)
                    if not resp.ok:
                        self._log(f"[{self.slot_id}] URL {url[:60]}... HTTP {resp.status}")
                        return None
                    content_type = str(resp.headers.get("content-type", "")).lower()
                    ext = ".mp4" if media_tag_inner == "video" else ".jpg"
                    if "png" in content_type:
                        ext = ".png"
                    elif "webp" in content_type:
                        ext = ".webp"
                    elif "jpeg" in content_type or "jpg" in content_type:
                        ext = ".jpg"
                    elif "mp4" in content_type:
                        ext = ".mp4"
                    elif "webm" in content_type:
                        ext = ".webm"
                    elif "quicktime" in content_type:
                        ext = ".mov"
                    save_path = self._build_save_path(output_dir, job, ext)
                    body = await resp.body()
                    if not body or len(body) < 100:
                        return None
                    tmp_path = save_path + ".tmp"
                    with open(tmp_path, "wb") as f:
                        f.write(body)
                    os.replace(tmp_path, save_path)
                    return save_path
                except Exception as e:
                    self._log(f"[{self.slot_id}] download error: {str(e)[:80]}")
                    return None

            # Helper: save base64 payload
            def _save_base64(b64_str, media_tag_inner):
                if not b64_str:
                    return None
                try:
                    ext = ".mp4" if media_tag_inner == "video" else ".jpg"
                    save_path = self._build_save_path(output_dir, job, ext)
                    img_bytes = _b64.b64decode(b64_str)
                    if not img_bytes or len(img_bytes) < 100:
                        return None
                    with open(save_path, "wb") as f:
                        f.write(img_bytes)
                    return save_path
                except Exception as e:
                    self._log(f"[{self.slot_id}] b64 decode failed: {str(e)[:60]}")
                    return None

            # Helper: build a media redirect URL from a media id
            def _redirect_url(media_id):
                if not media_id:
                    return ""
                return (
                    "https://labs.google/fx/api/trpc/media.getMediaUrlRedirect"
                    f"?name={media_id}"
                )

            # ── METHOD 1: Top-level fifeUrl / mediaName (extracted by our JS) ──
            fife_url = result.get("fifeUrl") or ""
            media_name = result.get("mediaName") or ""
            for attempt_url in (fife_url, _redirect_url(media_name)):
                if not attempt_url:
                    continue
                saved_path = await _download_and_save(attempt_url, media_tag)
                if saved_path:
                    saved_files.append(saved_path)
                    self._log(f"[{self.slot_id}] Saved: {saved_path}")
                    return saved_files

            # ── METHOD 2: Walk data.media[].image.generatedImage.fifeUrl ──
            # This is the raw Google API response shape (same as bot_engine reads)
            data_blob = result.get("data") or {}
            if isinstance(data_blob, dict):
                media_arr = data_blob.get("media") if isinstance(data_blob.get("media"), list) else []
                for media_item in media_arr:
                    if not isinstance(media_item, dict):
                        continue
                    img = media_item.get("image") or {}
                    gen = img.get("generatedImage") if isinstance(img, dict) else None
                    if not isinstance(gen, dict):
                        continue
                    candidate = gen.get("fifeUrl") or gen.get("url") or ""
                    if candidate:
                        saved_path = await _download_and_save(candidate, media_tag)
                        if saved_path:
                            saved_files.append(saved_path)
                            self._log(f"[{self.slot_id}] Saved (data.media): {saved_path}")
                            return saved_files
                    # Also try by mediaId redirect
                    mid = media_item.get("name") or media_item.get("mediaId") or ""
                    if mid:
                        saved_path = await _download_and_save(_redirect_url(mid), media_tag)
                        if saved_path:
                            saved_files.append(saved_path)
                            self._log(f"[{self.slot_id}] Saved (data.media redirect): {saved_path}")
                            return saved_files

            # ── METHOD 3: Alternate response shapes (generatedMedias / results etc.) ──
            candidate_lists = []
            if isinstance(data_blob, dict):
                for key in ("generatedMedias", "generated_medias", "mediaGenerations", "results"):
                    v = data_blob.get(key)
                    if isinstance(v, list) and v:
                        candidate_lists.append(v)
                # Nested under responses[]
                for resp_item in data_blob.get("responses") or []:
                    if isinstance(resp_item, dict):
                        for key in ("generatedMedias", "generated_medias", "mediaGenerations", "results"):
                            v = resp_item.get(key)
                            if isinstance(v, list) and v:
                                candidate_lists.append(v)
            # Also top-level result may be the list directly in some shapes
            for key in ("generatedMedias", "generated_medias", "mediaGenerations", "results"):
                v = result.get(key)
                if isinstance(v, list) and v:
                    candidate_lists.append(v)

            for media_list in candidate_lists:
                for media in media_list:
                    if not isinstance(media, dict):
                        continue
                    image_b64 = (
                        media.get("encodedImage")
                        or media.get("encoded_image")
                        or media.get("imageData")
                        or media.get("image_data")
                        or media.get("bytes")
                        or ""
                    )
                    image_url = (
                        media.get("uri")
                        or media.get("url")
                        or media.get("imageUri")
                        or media.get("image_uri")
                        or media.get("downloadUri")
                        or media.get("fifeUrl")
                        or ""
                    )
                    # Also peek into nested image.generatedImage.fifeUrl
                    if not image_url:
                        img = media.get("image") or {}
                        gen = img.get("generatedImage") if isinstance(img, dict) else None
                        if isinstance(gen, dict):
                            image_url = gen.get("fifeUrl") or gen.get("url") or ""

                    if image_b64:
                        saved_path = _save_base64(image_b64, media_tag)
                        if saved_path:
                            saved_files.append(saved_path)
                            self._log(f"[{self.slot_id}] Saved (b64): {saved_path}")
                            return saved_files

                    if image_url:
                        saved_path = await _download_and_save(image_url, media_tag)
                        if saved_path:
                            saved_files.append(saved_path)
                            self._log(f"[{self.slot_id}] Saved (url): {saved_path}")
                            return saved_files

                    # Try media id → redirect
                    media_id = (
                        media.get("name")
                        or media.get("mediaId")
                        or media.get("media_id")
                        or media.get("mediaGenerateId")
                        or ""
                    )
                    if media_id:
                        saved_path = await _download_and_save(_redirect_url(media_id), media_tag)
                        if saved_path:
                            saved_files.append(saved_path)
                            self._log(f"[{self.slot_id}] Saved (id redirect): {saved_path}")
                            return saved_files

            # ── METHOD 4: Last-resort deep recursive walk ──
            # Walk the entire result tree looking for any string that looks like
            # a fife URL or http(s) URL pointing to google image hosts.
            def _deep_find_url(node, depth=0):
                if depth > 10:
                    return None
                if isinstance(node, str):
                    s = node
                    if s.startswith("http") and (
                        "googleusercontent.com" in s
                        or "ggpht.com" in s
                        or "fife" in s.lower()
                        or "labs.google" in s
                    ):
                        return s
                    return None
                if isinstance(node, dict):
                    for v in node.values():
                        found = _deep_find_url(v, depth + 1)
                        if found:
                            return found
                elif isinstance(node, list):
                    for v in node:
                        found = _deep_find_url(v, depth + 1)
                        if found:
                            return found
                return None

            deep_url = _deep_find_url(result)
            if deep_url:
                self._log(f"[{self.slot_id}] DEBUG deep-found URL: {deep_url[:80]}...")
                saved_path = await _download_and_save(deep_url, media_tag)
                if saved_path:
                    saved_files.append(saved_path)
                    self._log(f"[{self.slot_id}] Saved (deep): {saved_path}")
                    return saved_files

            # Nothing worked — log full structure for debugging
            try:
                self._log(
                    f"[{self.slot_id}] No downloadable media found. "
                    f"Full response: {json.dumps(result, default=str)[:1500]}"
                )
            except Exception:
                pass

        except Exception as e:
            self._log(f"[{self.slot_id}] Save error: {str(e)[:120]}")

        return saved_files

    def _build_save_path(self, output_dir, job, ext):
        """Build save path — matches bot_engine's file naming logic.

        Retry handling: when a failed job is retried, db_manager.retry_failed_jobs_to_top
        sets queue_no to a new negative value (to push the job to the top of the queue)
        and sets output_index to the ORIGINAL queue_no (to preserve the filename).

        So the file number must be read as:
            output_index (if is_retry and valid) -> else queue_no -> else next-available

        This ensures "Retry all failed" writes back to the same filename slots the
        original jobs would have filled.
        """
        def _valid_positive_int(value):
            try:
                if value is None:
                    return None
                v = int(value)
                return v if v > 0 else None
            except Exception:
                return None

        file_num = None
        is_retry = bool(job.get("is_retry"))

        # Prefer output_index on retries (preserved original number)
        if is_retry:
            file_num = _valid_positive_int(job.get("output_index"))

        # Fall back to queue_no (primary for normal jobs; secondary on retries)
        if file_num is None:
            file_num = _valid_positive_int(job.get("queue_no"))

        # Last resort — never happens for DB-backed jobs but safe for manual calls
        if file_num is None:
            existing_nums = []
            try:
                for f in os.listdir(output_dir):
                    root, _ = os.path.splitext(f)
                    try:
                        existing_nums.append(int(root))
                    except Exception:
                        pass
            except Exception:
                pass
            file_num = (max(existing_nums) + 1) if existing_nums else 1

        filename = f"{file_num}{ext}"
        save_path = os.path.join(output_dir, filename)

        # Avoid overwrite on retry collisions — bot_engine will overwrite, but
        # CDP mode appends _1, _2 so partial-failure retries can coexist.
        if os.path.exists(save_path):
            base, e = os.path.splitext(save_path)
            k = 1
            while os.path.exists(f"{base}_{k}{e}"):
                k += 1
            save_path = f"{base}_{k}{e}"
        return save_path

    async def _try_reload(self):
        try:
            await self._page.goto(
                "https://labs.google/fx/tools/flow",
                wait_until="domcontentloaded", timeout=15000,
            )
            await asyncio.sleep(2)
        except Exception:
            pass

    async def _maybe_refresh(self):
        if self.jobs_completed > 0 and self.jobs_completed % 50 == 0:
            self._log(f"[{self.slot_id}] Auto-refresh (every 50 jobs)...")
            await self._try_reload()

    async def close(self):
        try:
            await self.context.close()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════
# CDPBrowserServer — 1 CloakBrowser process per account
# ═══════════════════════════════════════════════════════════════════════════

class CDPBrowserServer:
    """Manages 1 CloakBrowser process. Creates N contexts via CDP."""

    # Hard-clean cadence: every N completed jobs, do full browser restart to
    # reset reCAPTCHA telemetry, GPU caches, etc.
    HARD_CLEAN_EVERY_N_JOBS = 250

    def __init__(self, account_name, session_path, cookies_json_path, cdp_port, log_fn):
        self.account_name = account_name
        self._session_path = session_path
        self._cookies_json = cookies_json_path
        self._cdp_port = cdp_port
        self._log = log_fn
        self._process = None
        self._browser = None
        self._playwright = None
        self._project_id = None
        self._slots = []
        self._max_slots = 0  # Hard ceiling — can create more on demand up to this
        self._cookies_cache = []  # Cached cookies for on-demand slot creation
        self._slot_lock = None  # Initialized in start() to avoid event loop binding
        # Reusable launch config — populated by start(), reused by hard-clean restart.
        # Keeping the SAME port/profile/fingerprint is critical for reCAPTCHA reset.
        self._profile_path = None
        self._fingerprint_seed = None
        self._is_headless = True
        self._binary_path = None
        # Hard-clean tracking
        self._jobs_completed = 0
        self._is_cleaning = False
        self._clean_count = 0  # how many hard cleans have happened (for stats)
        # Clean markers — stored OUTSIDE the profile dir (sibling files) so
        # they survive full profile deletion. Populated in start() once
        # _profile_path is known.
        self._clean_marker_path = None  # sibling file: <profile>.last_clean
        self._jobs_marker_path = None   # sibling file: <profile>.jobs_count

    async def start(self, num_slots, total_jobs=0):
        """Start CloakBrowser process and create min(num_slots, total_jobs) contexts.

        Remaining contexts are created on-demand via get_or_create_slot().
        """
        self._max_slots = max(1, int(num_slots))
        if total_jobs and total_jobs > 0:
            actual_slots = min(self._max_slots, int(total_jobs))
        else:
            actual_slots = self._max_slots
        actual_slots = max(1, actual_slots)

        self._slot_lock = asyncio.Lock()
        self._log(
            f"[CDPServer:{self.account_name}] Starting on port {self._cdp_port} "
            f"({actual_slots} of {self._max_slots} slots, jobs: {total_jobs or 'unknown'})..."
        )

        cleanup_session_locks(self._session_path)

        # Find CloakBrowser binary (cached for reuse in hard-clean restart)
        binary = self._find_cloak_binary()
        if not binary:
            self._log(f"[CDPServer:{self.account_name}] CloakBrowser binary not found!")
            return False
        self._binary_path = binary

        # Generate seed + headless state ONCE and cache — restart must use SAME values
        self._fingerprint_seed = random.randint(10000, 99999)
        cloak_display = str(get_setting("cloak_display", "headless") or "headless").strip().lower()
        self._is_headless = cloak_display != "visible"

        # Use _cloak profile to avoid lock conflicts with login Chrome
        self._profile_path = self._session_path + "_cloak"
        # Marker files live NEXT TO the profile dir (not inside it) so a full
        # profile delete does not wipe the jobs_count / last_clean history.
        self._clean_marker_path = self._profile_path + ".last_clean"
        self._jobs_marker_path = self._profile_path + ".jobs_count"

        os.makedirs(self._profile_path, exist_ok=True)
        cleanup_session_locks(self._profile_path)

        self._log(f"[CDPServer:{self.account_name}] Profile: {self._profile_path}")
        self._log(f"[CDPServer:{self.account_name}] Display: {'headless' if self._is_headless else 'visible'}")

        # Startup profile check: delete profile if it's old/large/job-heavy
        # so we never start generation on a junk profile with degraded
        # reCAPTCHA telemetry. Runs BEFORE browser launch.
        await self._check_startup_clean()

        # Launch browser process (extracted so hard-clean can reuse)
        launched = self._launch_browser_process()
        if not launched:
            return False

        # Wait for CDP
        ready = await self._wait_for_cdp(timeout=15)
        if not ready:
            self._log(f"[CDPServer:{self.account_name}] CDP not ready after 15s!")
            return False

        self._log(f"[CDPServer:{self.account_name}] CDP ready!")

        # Connect Playwright
        from playwright.async_api import async_playwright
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{self._cdp_port}"
        )

        # Load + cache cookies for on-demand slot creation
        cookies = self._load_cookies()
        self._cookies_cache = cookies
        self._log(f"[CDPServer:{self.account_name}] Cookies loaded: {len(cookies)}")

        # Create first slot + extract project ID
        ctx1 = await self._browser.new_context()
        if cookies:
            try:
                await ctx1.add_cookies(cookies)
            except Exception:
                pass
        page1 = await ctx1.new_page()
        # Fix 1: Default timeout 120s — image gen takes 31-38s per HAR,
        # video gen can take 60-90s, default 30s causes premature timeouts
        page1.set_default_timeout(120000)
        await page1.goto("https://labs.google/fx/tools/flow", wait_until="domcontentloaded", timeout=30000)
        # Smart warmup replaces the old blind sleep. Primes reCAPTCHA
        # Enterprise so the first real job's execute() scores high
        # instead of failing with "reCAPTCHA evaluation failed".
        # Fast path ~200-500ms, worst case ~2500ms. Zero credit cost.
        warmed1 = await self._warmup_recaptcha(page1, timeout_s=2.5)
        if warmed1:
            self._log(
                f"[CDPServer:{self.account_name}] reCAPTCHA primed ✓ (slot 1)"
            )
        else:
            self._log(
                f"[CDPServer:{self.account_name}] reCAPTCHA warmup skipped (slot 1) — retry loop will cover"
            )

        # Check login
        if "accounts.google.com" in page1.url:
            self._log(f"[CDPServer:{self.account_name}] Redirected to login — session invalid!")
            return False

        # Extract project ID
        self._project_id = await self._extract_project_id(page1)
        if not self._project_id:
            # Try bot_engine cache
            try:
                from src.core.bot_engine import GoogleLabsBot
                self._project_id = GoogleLabsBot._shared_flow_project_id_by_account.get(self.account_name)
            except Exception:
                pass
        if not self._project_id:
            self._log(f"[CDPServer:{self.account_name}] No project ID!")
            return False

        self._log(f"[CDPServer:{self.account_name}] Project: {self._project_id}")

        # First slot
        slot1 = CDPSlotWorker(f"{self.account_name}#c1", ctx1, page1, self._project_id, self._log)
        self._slots.append(slot1)

        # PARALLEL SLOT CREATION — slots 2..N created concurrently using
        # asyncio.gather(). Previously sequential with 0.5s stagger, causing
        # 5 extra slots to take ~35s. Now ~5-8s total (bottleneck is the
        # per-slot page.goto which runs in parallel against the same browser).
        remaining = actual_slots - 1
        if remaining > 0:
            self._log(
                f"[CDPServer:{self.account_name}] Creating {remaining} additional slot(s) in parallel..."
            )

            async def _build_slot(slot_index):
                try:
                    return await self._create_new_slot(index=slot_index)
                except Exception as e:
                    self._log(
                        f"[CDPServer:{self.account_name}] Slot {slot_index} failed: {str(e)[:60]}"
                    )
                    return None

            parallel_slots = await asyncio.gather(
                *[_build_slot(i) for i in range(2, actual_slots + 1)],
                return_exceptions=False,
            )
            none_count = sum(1 for s in parallel_slots if s is None)
            if none_count:
                self._log(
                    f"[CDPServer:{self.account_name}] {none_count}/{remaining} additional slot(s) failed"
                )

        est_ram = 300 + len(self._slots) * 30
        self._log(
            f"[CDPServer:{self.account_name}] {len(self._slots)}/{self._max_slots} slots ready. "
            f"Est RAM: ~{est_ram}MB"
        )
        return len(self._slots) > 0

    async def _create_new_slot(self, index=None):
        """Create a new context slot. Returns the slot or None on failure.

        Used both for initial parallel slot creation in start() and for
        on-demand slot creation via get_or_create_slot().
        """
        if self._browser is None or self._project_id is None:
            return None
        try:
            ctx = await self._browser.new_context()
            if self._cookies_cache:
                try:
                    await ctx.add_cookies(self._cookies_cache)
                except Exception:
                    pass
            page = await ctx.new_page()
            page.set_default_timeout(120000)  # Fix 1: long image/video gen
            await page.goto(
                "https://labs.google/fx/tools/flow",
                wait_until="domcontentloaded",
                timeout=15000,
            )
            # Smart warmup (same as start() slot 1) — primes grecaptcha
            # so the first job on this parallel slot doesn't hit a
            # cold-start reCAPTCHA fail. No log per-slot to avoid spam
            # when 4-40 slots are created in parallel.
            await self._warmup_recaptcha(page, timeout_s=2.5)
            slot_index = index if index is not None else (len(self._slots) + 1)
            slot = CDPSlotWorker(
                f"{self.account_name}#c{slot_index}",
                ctx,
                page,
                self._project_id,
                self._log,
            )
            self._slots.append(slot)
            return slot
        except Exception as e:
            self._log(f"[CDPServer:{self.account_name}] _create_new_slot error: {str(e)[:80]}")
            return None

    async def get_or_create_slot(self):
        """Return an idle slot. Creates a new one on-demand if under max capacity."""
        if self._slot_lock is None:
            self._slot_lock = asyncio.Lock()
        async with self._slot_lock:
            # Reuse idle slot if any
            for slot in self._slots:
                if not slot.is_busy:
                    return slot
            # Create new slot if under limit
            if len(self._slots) < self._max_slots:
                new_slot = await self._create_new_slot()
                if new_slot is not None:
                    self._log(
                        f"[CDPServer:{self.account_name}] On-demand slot created "
                        f"({len(self._slots)}/{self._max_slots})"
                    )
                    return new_slot
            return None

    # ────────────────────────────────────────────────────────────────────
    # Browser launch (extracted for reuse in hard-clean restart)
    # ────────────────────────────────────────────────────────────────────
    def _launch_browser_process(self):
        """Launch the CloakBrowser subprocess using cached config.

        Uses the SAME port, profile, fingerprint, and display mode as the
        original start() call — critical for hard-clean restart to preserve
        session continuity (same cookies, same device identity).
        """
        if not self._binary_path or not self._profile_path:
            self._log(f"[CDPServer:{self.account_name}] _launch_browser_process: missing cached config")
            return False

        chrome_args = [
            self._binary_path,
            f"--remote-debugging-port={self._cdp_port}",
            f"--user-data-dir={self._profile_path}",
            f"--fingerprint={self._fingerprint_seed}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ]
        if self._is_headless:
            chrome_args.append("--headless=new")

        popen_kwargs = {}
        if platform.system() == "Windows":
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            popen_kwargs["stdout"] = subprocess.DEVNULL
            popen_kwargs["stderr"] = subprocess.DEVNULL

        try:
            self._process = subprocess.Popen(chrome_args, **popen_kwargs)
            process_tracker.register(self._process.pid)
            self._log(f"[CDPServer:{self.account_name}] PID: {self._process.pid} (port {self._cdp_port})")
            return True
        except Exception as e:
            self._log(f"[CDPServer:{self.account_name}] subprocess.Popen failed: {str(e)[:80]}")
            self._process = None
            return False

    # ────────────────────────────────────────────────────────────────────
    # HARD CLEAN — full browser restart every N jobs (reCAPTCHA reset)
    # ────────────────────────────────────────────────────────────────────
    def note_job_completed(self):
        """Increment completed-job counter.

        Called by CDPSharedManager AFTER a successful file save.
        Returns True if a hard clean should be triggered now.
        """
        self._jobs_completed += 1
        # Persist jobs count to disk so startup check can detect job-heavy
        # profiles after an app restart.
        self._update_jobs_count()
        return self._jobs_completed >= self.HARD_CLEAN_EVERY_N_JOBS and not self._is_cleaning

    # ──────────────────────────────────────────────────────────────────────
    # Startup profile check + clean markers
    # ──────────────────────────────────────────────────────────────────────

    async def _check_startup_clean(self):
        """Delete profile on startup if it's stale (job-heavy / old / bloated).

        Triggers:
          1. Jobs count since last clean >= 200
          2. Hours since last clean >= 24
          3. No marker AND Cache folder > 50 MB (junk legacy profile)

        Markers are stored OUTSIDE the profile dir (sibling files) so they
        survive a full profile delete.
        """
        if not self._profile_path:
            return

        needs_clean = False
        reason = ""

        # Check 1: jobs count from last session
        try:
            if self._jobs_marker_path and os.path.exists(self._jobs_marker_path):
                with open(self._jobs_marker_path, "r") as f:
                    last_count = int((f.read() or "0").strip() or "0")
                if last_count >= 200:
                    needs_clean = True
                    reason = f"{last_count} jobs since last clean"
        except Exception:
            pass

        # Check 2: time since last clean
        try:
            if (
                not needs_clean
                and self._clean_marker_path
                and os.path.exists(self._clean_marker_path)
            ):
                with open(self._clean_marker_path, "r") as f:
                    last_time = float((f.read() or "0").strip() or "0")
                hours_ago = (time.time() - last_time) / 3600
                if hours_ago >= 24:
                    needs_clean = True
                    reason = f"{hours_ago:.0f} hours since last clean"
        except Exception:
            pass

        # Check 3: no marker at all → look for legacy junk (Cache > 50 MB)
        try:
            if not needs_clean and self._clean_marker_path and not os.path.exists(self._clean_marker_path):
                cache_dir = os.path.join(self._profile_path, "Default", "Cache")
                if os.path.isdir(cache_dir):
                    cache_size = 0
                    for dp, _dn, fns in os.walk(cache_dir):
                        for fn in fns:
                            try:
                                cache_size += os.path.getsize(os.path.join(dp, fn))
                            except Exception:
                                pass
                    if cache_size > 50 * 1024 * 1024:  # 50 MB
                        needs_clean = True
                        reason = f"Cache is {cache_size / 1024 / 1024:.0f}MB (no clean marker)"
        except Exception:
            pass

        if not needs_clean:
            self._log(
                f"[CDPServer:{self.account_name}] Startup clean not needed — profile is fresh."
            )
            return

        self._log(
            f"[CDPServer:{self.account_name}] Startup clean needed: {reason} — deleting profile..."
        )

        # Delete full profile (same logic as hard clean Step 4)
        import shutil

        total_size = 0
        if os.path.isdir(self._profile_path):
            try:
                for dp, _dn, fns in os.walk(self._profile_path):
                    for fn in fns:
                        try:
                            total_size += os.path.getsize(os.path.join(dp, fn))
                        except Exception:
                            pass
            except Exception:
                pass

        deleted = False
        for attempt in range(5):
            try:
                shutil.rmtree(self._profile_path, ignore_errors=False)
                deleted = True
                break
            except Exception as e:
                if attempt < 4:
                    await asyncio.sleep(0.5)
                else:
                    self._log(
                        f"[CDPServer:{self.account_name}] Startup delete retry exhausted: "
                        f"{str(e)[:80]} — using ignore_errors fallback"
                    )
                    shutil.rmtree(self._profile_path, ignore_errors=True)
                    deleted = True

        try:
            os.makedirs(self._profile_path, exist_ok=True)
        except Exception as e:
            self._log(
                f"[CDPServer:{self.account_name}] Startup clean makedirs failed: {str(e)[:60]}"
            )
            return

        freed_mb = total_size / (1024 * 1024)
        self._log(
            f"[CDPServer:{self.account_name}] Startup clean done! Freed {freed_mb:.1f}MB. "
            f"Fresh profile will be initialized by Chromium on launch."
        )

        # Save markers immediately so the NEXT startup check sees a fresh clean.
        # Reset in-memory jobs counter too.
        self._jobs_completed = 0
        self._save_clean_markers()

    def _save_clean_markers(self):
        """Write .last_clean (unix ts) and .jobs_count (0) next to the profile dir.

        Called after every clean — both hard clean and startup clean. Files
        live OUTSIDE the profile dir so profile deletion does not erase them.
        """
        try:
            if self._clean_marker_path:
                with open(self._clean_marker_path, "w") as f:
                    f.write(str(time.time()))
            if self._jobs_marker_path:
                with open(self._jobs_marker_path, "w") as f:
                    f.write("0")
        except Exception as e:
            self._log(
                f"[CDPServer:{self.account_name}] save_clean_markers failed: {str(e)[:60]}"
            )

    def _update_jobs_count(self):
        """Increment .jobs_count on disk after a successful job.

        Keeps counter durable across app restarts so startup check can detect
        job-heavy profiles even if the app crashed without a clean shutdown.
        """
        try:
            if not self._jobs_marker_path:
                return
            count = 0
            if os.path.exists(self._jobs_marker_path):
                try:
                    with open(self._jobs_marker_path, "r") as f:
                        count = int((f.read() or "0").strip() or "0")
                except Exception:
                    count = 0
            count += 1
            with open(self._jobs_marker_path, "w") as f:
                f.write(str(count))
        except Exception:
            # Silent — disk marker is best-effort, never block the job flow.
            pass

    async def _warmup_recaptcha(self, page, timeout_s=2.5):
        """Prime reCAPTCHA Enterprise BEFORE the first real job runs.

        Replaces the blind ``asyncio.sleep(1)`` padding that used to follow
        ``page.goto(flow)``. That sleep caused ~20-40% of first-job
        ``grecaptcha.enterprise.execute()`` calls to fail with
        "reCAPTCHA evaluation failed" — the existing retry loop covered
        it, but at a cost of ~4s extra delay per affected job and noisy
        logs.

        Strategy (no credit cost — tokens are free, only API submissions
        cost):

          1. ``page.wait_for_function`` polls at 100 ms for
             ``window.grecaptcha.enterprise.execute`` to become callable.
             This is much tighter than a blind sleep — domcontentloaded
             fires BEFORE enterprise.js finishes loading, and the real
             readiness varies from 150 ms to 2 s depending on network.
          2. Inside ``page.evaluate``: wait for
             ``grecaptcha.enterprise.ready()`` callback to fire.
          3. Extract the sitekey from the loaded
             ``<script src="...enterprise.js?render=KEY">`` tag.
          4. Fire a single dummy
             ``grecaptcha.enterprise.execute(key, {action:'flow_init'})``.
             The returned token is discarded — we never submit it to the
             Flow API, so there is zero credit cost. But Google's
             reCAPTCHA backend now has a fingerprint + a scored
             interaction for this browser, and the next REAL execute
             (from the first job) gets a high score immediately.

        Timing:
          Happy path:  200-500 ms  (faster than the old 1 000 ms sleep)
          Medium:      600-1 200 ms
          Worst case:  ~2 500 ms   (still faster than a retry + 4 s
                                    backoff, and with zero first-job
                                    fails)

        On ANY failure this is a no-op — the existing per-job retry
        loop still catches late-loaded grecaptcha and retries. So this
        is pure upside: never regresses reliability, usually speeds
        things up.
        """
        try:
            # Step 1+2: wait for grecaptcha.enterprise.execute to exist.
            # page.wait_for_function polls every ~100 ms internally.
            await page.wait_for_function(
                """() => (typeof window.grecaptcha !== 'undefined')
                    && window.grecaptcha.enterprise
                    && (typeof window.grecaptcha.enterprise.execute === 'function')""",
                timeout=int(timeout_s * 1000),
            )

            # Step 3+4: grecaptcha.ready() + extract sitekey + dummy execute.
            # The evaluate runs fully inside the page's JS context and
            # returns a short status string we can log on.
            status = await page.evaluate(
                """async () => {
                    try {
                        // Find sitekey from the loaded enterprise.js <script>
                        let sitekey = null;
                        const scripts = Array.from(document.querySelectorAll('script'));
                        for (const s of scripts) {
                            const src = s.src || '';
                            if (src.indexOf('recaptcha') !== -1 && src.indexOf('enterprise') !== -1) {
                                const m = src.match(/[?&]render=([^&]+)/);
                                if (m && m[1] && m[1] !== 'explicit') {
                                    sitekey = decodeURIComponent(m[1]);
                                    break;
                                }
                            }
                        }
                        if (!sitekey) return 'no_sitekey';

                        // Wait for grecaptcha.enterprise to finish its own init
                        await new Promise((resolve) => {
                            try {
                                grecaptcha.enterprise.ready(() => resolve(true));
                                // Hard cap in case ready() never fires
                                setTimeout(() => resolve(false), 1500);
                            } catch (e) { resolve(false); }
                        });

                        // Fire the priming execute — token discarded, no API
                        // submission, no credit cost. This is what gives
                        // Google's backend enough signal to score the NEXT
                        // execute (the real one) at a passable level.
                        try {
                            const token = await grecaptcha.enterprise.execute(
                                sitekey, {action: 'flow_init'}
                            );
                            return token ? 'primed' : 'no_token';
                        } catch (e) {
                            return 'execute_err';
                        }
                    } catch (e) {
                        return 'outer_err';
                    }
                }"""
            )
            return status == "primed"
        except Exception:
            # Best-effort — if anything goes wrong, let the per-job retry
            # loop handle a cold first execute. No regression vs the old
            # blind sleep.
            return False

    async def _maybe_hard_clean(self):
        """Full browser restart after N completed jobs.

        Steps:
          1. Close all contexts (slots lose their page/context refs)
          2. Disconnect Playwright CDP
          3. Terminate browser process
          4. Clean profile sub-dirs (Cache/Code Cache/Service Worker/etc.)
             — NEVER deletes Cookies or Cookies-journal
          5. Wait 3s for filesystem to settle
          6. Relaunch browser process with SAME port/profile/fingerprint
          7. Wait for CDP endpoint
          8. Reconnect Playwright
          9. Re-extract project ID on a fresh context
         10. Recreate contexts for all existing slots
         11. Reset job counter

        Other accounts keep running — only this server pauses.
        Failure does NOT crash — just logs and marks cleaning complete.
        """
        if self._is_cleaning:
            return False
        self._is_cleaning = True
        clean_num = self._clean_count + 1
        start_ts = time.time()

        try:
            self._log(
                f"[CDPServer:{self.account_name}] HARD CLEAN #{clean_num} starting "
                f"({self._jobs_completed} jobs done)..."
            )

            # ── Step 0: Wait for in-flight jobs on this account to finish ──
            # _is_cleaning is already True so the dispatcher won't hand out NEW jobs
            # to this account's slots. We just need to let currently-running ones
            # drain. Cap the wait at 120s so a stuck slot doesn't block forever.
            wait_start = time.time()
            while time.time() - wait_start < 120:
                busy_slots = [s for s in self._slots if s.is_busy]
                if not busy_slots:
                    break
                self._log(
                    f"[CDPServer:{self.account_name}] Waiting for {len(busy_slots)} "
                    f"in-flight job(s) to finish before clean..."
                )
                await asyncio.sleep(2)

            # ── Step 1: Close all slot contexts ──
            self._log(f"[CDPServer:{self.account_name}] [1/11] Closing all contexts...")
            for slot in self._slots:
                try:
                    if slot.context is not None:
                        await slot.context.close()
                except Exception:
                    pass
                slot.context = None
                slot._page = None
                slot.is_busy = False

            # ── Step 2: Disconnect Playwright CDP ──
            self._log(f"[CDPServer:{self.account_name}] [2/11] Disconnecting CDP...")
            if self._browser is not None:
                try:
                    await self._browser.close()
                except Exception:
                    pass
                self._browser = None

            # ── Step 3: Terminate browser process ──
            self._log(f"[CDPServer:{self.account_name}] [3/11] Killing browser process...")
            if self._process is not None:
                try:
                    self._process.terminate()
                    for _ in range(10):
                        if self._process.poll() is not None:
                            break
                        await asyncio.sleep(0.2)
                    if self._process.poll() is None:
                        try:
                            self._process.kill()
                        except Exception:
                            pass
                    try:
                        process_tracker.unregister(self._process.pid)
                    except Exception:
                        pass
                except Exception as e:
                    self._log(f"[CDPServer:{self.account_name}] process kill error: {str(e)[:60]}")
                self._process = None

            # ── Step 4: DELETE ENTIRE profile and recreate it empty ──
            # Maximum reCAPTCHA reset: removes ALL hidden Google tracking data
            # (IndexedDB fingerprints, BrowsingTopics, Feature Engagement,
            # DIPS tracking, BudgetDatabase, etc.) that a partial cache clean
            # leaves behind. Cookies are reinjected via Playwright contexts
            # after restart — same method as initial startup.
            #
            # Score recovery: ~85-90% (full delete) vs ~60-70% (partial clean)
            self._log(
                f"[CDPServer:{self.account_name}] [4/11] Deleting FULL profile for max reCAPTCHA reset..."
            )
            import shutil

            profile = self._profile_path
            total_size = 0

            # Calculate size for logging
            if os.path.isdir(profile):
                try:
                    for dp, _dn, fns in os.walk(profile):
                        for fn in fns:
                            try:
                                total_size += os.path.getsize(os.path.join(dp, fn))
                            except Exception:
                                pass
                except Exception:
                    pass

            # Delete entire profile with Windows file-lock retry.
            # On Windows, even after process kill, file handles can linger
            # briefly. Retry up to 5 times with short delays before giving
            # up and falling back to ignore_errors=True.
            if os.path.isdir(profile):
                deleted = False
                for attempt in range(5):
                    try:
                        shutil.rmtree(profile, ignore_errors=False)
                        deleted = True
                        break
                    except Exception as e:
                        if attempt < 4:
                            await asyncio.sleep(0.5)
                        else:
                            self._log(
                                f"[CDPServer:{self.account_name}] Profile delete retry exhausted: "
                                f"{str(e)[:80]} — using ignore_errors fallback"
                            )
                            shutil.rmtree(profile, ignore_errors=True)
                            deleted = True
                if not deleted:
                    self._log(f"[CDPServer:{self.account_name}] Profile delete failed entirely")

            # Recreate empty profile dir — Chromium will init it fresh on relaunch
            try:
                os.makedirs(profile, exist_ok=True)
            except Exception as e:
                self._log(f"[CDPServer:{self.account_name}] makedirs failed: {str(e)[:60]}")
                return False

            freed_mb = total_size / (1024 * 1024)
            self._log(
                f"[CDPServer:{self.account_name}] [4/11] Full profile deleted! "
                f"Freed {freed_mb:.1f}MB"
            )

            # Reload cookies from disk so any updates written since startup
            # are picked up, AND so the in-memory cache stays fresh for
            # future on-demand slot creation.
            try:
                fresh_cookies = self._load_cookies()
                if fresh_cookies:
                    self._cookies_cache = fresh_cookies
                    self._log(
                        f"[CDPServer:{self.account_name}] Reloaded {len(fresh_cookies)} "
                        f"cookie(s) from disk for re-injection"
                    )
                else:
                    self._log(
                        f"[CDPServer:{self.account_name}] WARNING: cookie file empty/missing — "
                        f"using {len(self._cookies_cache)} cached cookies"
                    )
            except Exception as e:
                self._log(
                    f"[CDPServer:{self.account_name}] Cookie reload failed ({str(e)[:60]}) — "
                    f"using cached"
                )

            # ── Step 5: Wait for filesystem to settle ──
            await asyncio.sleep(3)

            # Extra safety: clean any stale session locks on the profile
            # (profile was just recreated so this is usually a no-op, but
            # it's cheap and catches edge cases)
            try:
                cleanup_session_locks(self._profile_path)
            except Exception:
                pass

            # ── Step 6: Relaunch browser process (same port/profile/fingerprint) ──
            self._log(
                f"[CDPServer:{self.account_name}] [6/11] Restarting browser on port {self._cdp_port}..."
            )
            launched = self._launch_browser_process()
            if not launched:
                self._log(f"[CDPServer:{self.account_name}] HARD CLEAN: browser relaunch FAILED")
                return False

            # ── Step 7: Wait for CDP endpoint ──
            self._log(f"[CDPServer:{self.account_name}] [7/11] Waiting for CDP...")
            ready = await self._wait_for_cdp(timeout=20)
            if not ready:
                self._log(f"[CDPServer:{self.account_name}] HARD CLEAN: CDP not ready after 20s")
                return False

            # ── Step 8: Reconnect Playwright ──
            self._log(f"[CDPServer:{self.account_name}] [8/11] Reconnecting Playwright...")
            try:
                if self._playwright is None:
                    from playwright.async_api import async_playwright
                    self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{self._cdp_port}"
                )
            except Exception as e:
                self._log(f"[CDPServer:{self.account_name}] HARD CLEAN: Playwright reconnect failed: {str(e)[:80]}")
                return False

            # ── Step 9: Re-extract project ID on a fresh page ──
            self._log(f"[CDPServer:{self.account_name}] [9/11] Re-verifying project ID...")
            try:
                probe_ctx = await self._browser.new_context()
                if self._cookies_cache:
                    try:
                        await probe_ctx.add_cookies(self._cookies_cache)
                    except Exception:
                        pass
                probe_page = await probe_ctx.new_page()
                await probe_page.goto(
                    "https://labs.google/fx/tools/flow",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                await asyncio.sleep(3)
                if "accounts.google.com" in probe_page.url:
                    self._log(
                        f"[CDPServer:{self.account_name}] HARD CLEAN: session invalid after restart "
                        f"(redirected to login)"
                    )
                    try:
                        await probe_ctx.close()
                    except Exception:
                        pass
                    return False
                new_pid = await self._extract_project_id(probe_page)
                if new_pid:
                    self._project_id = new_pid
                try:
                    await probe_ctx.close()
                except Exception:
                    pass
            except Exception as e:
                self._log(f"[CDPServer:{self.account_name}] HARD CLEAN: project ID probe failed: {str(e)[:80]}")
                # Keep existing project ID as fallback — don't fail the clean

            # ── Step 10: Recreate contexts for all existing slots ──
            self._log(f"[CDPServer:{self.account_name}] [10/11] Recreating {len(self._slots)} slot context(s)...")
            recreated = 0
            for slot in self._slots:
                try:
                    new_ctx = await self._browser.new_context()
                    if self._cookies_cache:
                        try:
                            await new_ctx.add_cookies(self._cookies_cache)
                        except Exception:
                            pass
                    new_page = await new_ctx.new_page()
                    new_page.set_default_timeout(120000)  # Fix 1
                    await new_page.goto(
                        "https://labs.google/fx/tools/flow",
                        wait_until="domcontentloaded",
                        timeout=15000,
                    )
                    # Smart warmup — recreated contexts after a hard
                    # clean have a completely fresh grecaptcha state,
                    # so priming here matters even more than at initial
                    # startup.
                    await self._warmup_recaptcha(new_page, timeout_s=2.5)
                    slot.context = new_ctx
                    slot._page = new_page
                    slot._project_id = self._project_id
                    slot.is_busy = False
                    recreated += 1
                except Exception as e:
                    self._log(
                        f"[CDPServer:{self.account_name}] slot {slot.slot_id} recreate failed: {str(e)[:60]}"
                    )

            # ── Step 11: Reset counter + persist clean markers ──
            self._jobs_completed = 0
            self._clean_count += 1
            # Persist .last_clean + reset .jobs_count on disk so the next
            # app startup sees this recent clean and skips startup-clean.
            self._save_clean_markers()
            elapsed = time.time() - start_ts
            self._log(
                f"[CDPServer:{self.account_name}] HARD CLEAN #{clean_num} DONE in {elapsed:.1f}s. "
                f"{recreated}/{len(self._slots)} slots ready, reCAPTCHA reset, resuming..."
            )
            return True
        except Exception as e:
            self._log(f"[CDPServer:{self.account_name}] HARD CLEAN error: {str(e)[:120]}")
            return False
        finally:
            self._is_cleaning = False

    def _find_cloak_binary(self):
        """Find CloakBrowser binary path.

        Order:
          1. ensure_binary() — downloads if missing (important on first-run Mac)
          2. binary_info() — fast path if already installed
          3. get_binary_path() — config lookup

        On Mac, if the returned path ends in .app (bundle), resolve to the actual
        executable inside Contents/MacOS/.
        """
        binary = None

        # Method 1: ensure_binary() FIRST — this also downloads the binary if missing
        # (critical on fresh Mac installs where binary_info() would return nothing)
        try:
            from cloakbrowser.download import ensure_binary
            binary = ensure_binary()
            if binary:
                self._log(f"[CDPServer:{self.account_name}] Binary ensured: {binary}")
        except Exception as e:
            self._log(f"[CDPServer:{self.account_name}] ensure_binary unavailable: {str(e)[:60]}")

        # Method 2: cloakbrowser.binary_info()
        if not binary:
            try:
                from cloakbrowser import binary_info
                info = binary_info() or {}
                if info.get("installed"):
                    binary = info.get("binary_path")
            except Exception:
                pass

        # Method 3: cloakbrowser.config.get_binary_path()
        if not binary:
            try:
                from cloakbrowser.config import get_binary_path
                binary = get_binary_path()
            except Exception:
                pass

        if not binary:
            return None

        binary_str = str(binary)

        # Mac: .app bundle → find the real executable inside
        if platform.system() == "Darwin" and binary_str.endswith(".app"):
            # Try known Chromium-based exec names
            candidates = [
                os.path.join(binary_str, "Contents", "MacOS", "Chromium"),
                os.path.join(binary_str, "Contents", "MacOS", "Google Chrome for Testing"),
                os.path.join(binary_str, "Contents", "MacOS", "CloakBrowser"),
                os.path.join(binary_str, "Contents", "MacOS", "Chrome"),
            ]
            resolved = None
            for cand in candidates:
                if os.path.exists(cand):
                    resolved = cand
                    break
            # Fallback: walk MacOS dir and take first regular file
            if not resolved:
                macos_dir = os.path.join(binary_str, "Contents", "MacOS")
                if os.path.isdir(macos_dir):
                    try:
                        for entry in sorted(os.listdir(macos_dir)):
                            full = os.path.join(macos_dir, entry)
                            if os.path.isfile(full):
                                resolved = full
                                break
                    except Exception:
                        pass
            if resolved:
                self._log(f"[CDPServer:{self.account_name}] Mac .app resolved: {resolved}")
                binary_str = resolved
            else:
                self._log(f"[CDPServer:{self.account_name}] Mac .app bundle has no MacOS executable: {binary_str}")
                return None

        if not os.path.exists(binary_str):
            self._log(f"[CDPServer:{self.account_name}] Binary path returned but file not found: {binary_str}")
            return None

        self._log(f"[CDPServer:{self.account_name}] Binary: {binary_str}")
        return binary_str

    async def _wait_for_cdp(self, timeout=15):
        """Wait for CDP endpoint to accept connections."""
        import requests as http_req
        for _ in range(timeout * 2):
            try:
                resp = http_req.get(f"http://127.0.0.1:{self._cdp_port}/json/version", timeout=2)
                if resp.ok:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.5)
        return False

    def _load_cookies(self):
        """Load and fix cookies from exported_cookies.json."""
        if not os.path.exists(self._cookies_json):
            return []
        try:
            with open(self._cookies_json, "r", encoding="utf-8") as f:
                raw = json.load(f)
            return _fix_cookies_for_import(raw)
        except Exception:
            return []

    async def _extract_project_id(self, page):
        """Extract project ID from Flow page — tries URL, DOM, New Project click, settings cache."""
        # ── Method 1: URL regex ──
        url = page.url
        match = re.search(r"/project/([a-z0-9-]{16,})", url, re.IGNORECASE)
        if match:
            return match.group(1)
        match = re.search(r"/flow/([a-f0-9-]{36})", url)
        if match:
            return match.group(1)

        # ── Method 2: DOM scrape ──
        try:
            pid = await page.evaluate("""
                () => {
                    const regex = /\\/project\\/([a-z0-9-]{16,})/i;
                    const candidates = [window.location.href];
                    for (const n of document.querySelectorAll("a[href*='/project/']"))
                        candidates.push(n.href || "");
                    for (const s of document.querySelectorAll("script")) {
                        const t = s.textContent || "";
                        if (t.includes("/project/")) candidates.push(t.slice(0, 6000));
                    }
                    for (const item of candidates) {
                        const m = item.match(regex);
                        if (m && m[1]) return m[1];
                    }
                    const body = document.body ? document.body.innerHTML.slice(0, 20000) : '';
                    const m2 = body.match(/"projectId"\\s*:\\s*"([a-f0-9-]{16,})"/);
                    return m2 ? m2[1] : null;
                }
            """)
            if pid:
                return pid
        except Exception:
            pass

        # ── Method 3: Click "New project" button ──
        try:
            new_proj = page.locator("text='New project'")
            count = await new_proj.count()
            if count > 0:
                self._log(f"[CDPServer:{self.account_name}] Clicking 'New project'...")
                await new_proj.first.click()
                await asyncio.sleep(5)
                new_url = page.url
                m = re.search(r"/project/([a-z0-9-]{16,})", new_url, re.IGNORECASE)
                if m:
                    return m.group(1)
                m = re.search(r"/flow/([a-f0-9-]{36})", new_url)
                if m:
                    return m.group(1)
        except Exception:
            pass

        # ── Method 4: Settings cache (from bot_engine old mode) ──
        try:
            # Try a few likely locations for the settings file
            candidates = [
                os.path.join(os.path.dirname(self._session_path), "settings.json"),
                os.path.join(os.path.dirname(self._session_path), "..", "settings.json"),
                os.path.join(DATA_DIR, "settings.json"),
            ]
            for settings_file in candidates:
                settings_file = os.path.abspath(settings_file)
                if os.path.exists(settings_file):
                    with open(settings_file, "r", encoding="utf-8") as f:
                        settings = json.load(f)
                    cached = settings.get("cached_project_ids", {}) or {}
                    pid = cached.get(self.account_name)
                    if pid:
                        self._log(f"[CDPServer:{self.account_name}] Project from settings cache: {pid}")
                        return pid
        except Exception:
            pass

        return None

    def get_available_slot(self):
        for slot in self._slots:
            if not slot.is_busy:
                return slot
        return None

    def get_all_slots(self):
        return list(self._slots)

    async def stop(self):
        """Stop all slots, browser, and process."""
        for slot in self._slots:
            await slot.close()

        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass

        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass

        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            process_tracker.unregister(self._process.pid)

        total = sum(s.jobs_completed for s in self._slots)
        self._log(f"[CDPServer:{self.account_name}] Stopped. {total} jobs across {len(self._slots)} slots.")


# ═══════════════════════════════════════════════════════════════════════════
# CDPSharedManager — orchestrates CDP servers for all accounts
# ═══════════════════════════════════════════════════════════════════════════

class CDPSharedManager:
    """Manages CDP servers and dispatches jobs to slot workers."""

    def __init__(self, queue_manager):
        self.qm = queue_manager
        self._log = lambda msg: queue_manager.signals.log_msg.emit(msg)
        self._servers = {}
        self._active_tasks = []
        self._rr_order = []  # Round-robin order of account names

    async def run(self):
        """Main entry — start servers, dispatch jobs."""
        all_accs = get_accounts()
        if not all_accs:
            self._log("[CDPShared] No accounts configured.")
            return

        slots_per_account = max(1, min(40, get_int_setting("slots_per_account", 5)))
        base_port = random.randint(9222, 9280)

        # Count pending jobs so start() can create only needed contexts
        try:
            pending_jobs_snapshot = [j for j in get_all_jobs() if j.get("status") == "pending"]
            total_pending = len(pending_jobs_snapshot)
        except Exception:
            total_pending = 0

        # Distribute jobs evenly across accounts (rough per-account estimate)
        num_accs = max(1, len(all_accs))
        jobs_per_account_hint = max(1, (total_pending + num_accs - 1) // num_accs) if total_pending else 0

        self._log(
            f"[CDPShared] Starting: {len(all_accs)} account(s), "
            f"{slots_per_account} context(s) each. "
            f"Pending jobs: {total_pending}"
        )

        try:
            # PARALLEL ACCOUNT STARTUP — all accounts start their browsers
            # simultaneously instead of one-by-one. 3 accounts × 60s sequential
            # → max(60s) ≈ 60s total. Each account is independent, so there's
            # no resource conflict (each has its own port + profile).
            pre_start_info = []  # [(name, server), ...]
            for i, acc in enumerate(all_accs):
                name = acc.get("name", "unknown")
                session_path = acc.get("session_path", os.path.join(DATA_DIR, name))
                cookies_json = os.path.join(session_path, "exported_cookies.json")
                port = base_port + i
                server = CDPBrowserServer(name, session_path, cookies_json, port, self._log)
                pre_start_info.append((name, server))

            self._log(
                f"[CDPShared] Launching {len(pre_start_info)} account(s) in parallel..."
            )

            async def _start_one(name, server):
                """Start one account; never raise so other accounts keep going."""
                try:
                    ok = await server.start(
                        num_slots=slots_per_account,
                        total_jobs=jobs_per_account_hint,
                    )
                    return name, server, ok, None
                except Exception as e:
                    return name, server, False, str(e)[:120]

            start_results = await asyncio.gather(
                *[_start_one(name, server) for name, server in pre_start_info],
                return_exceptions=False,
            )

            for name, server, ok, err in start_results:
                if ok:
                    self._servers[name] = server
                    self._rr_order.append(name)
                    self._log(f"[CDPShared] {name}: ready ✓")
                else:
                    if err:
                        self._log(f"[CDPShared] {name}: start() exception: {err}")
                    self._log(f"[CDPShared] {name}: Failed to start — skipped.")
                    try:
                        await server.stop()
                    except Exception:
                        pass

            total_slots = sum(len(s.get_all_slots()) for s in self._servers.values())
            total_servers = len(self._servers)

            if total_slots == 0:
                self._log("[CDPShared] No slots started on any account.")
                return

            est_ram = total_servers * 300 + total_slots * 30
            self._log(
                f"[CDPShared] Total: {total_servers} browser(s), "
                f"{total_slots} context(s). Est RAM: ~{est_ram}MB."
            )

            # Main dispatch loop
            while self.qm.is_running:
                if self.qm.stop_requested or self.qm.force_stop_requested:
                    break
                if self.qm.pause_requested:
                    await asyncio.sleep(1)
                    continue

                self._active_tasks = [t for t in self._active_tasks if not t.done()]

                # Expire any account holds whose duration has elapsed —
                # this re-enables dispatch to accounts that were paused
                # for auth / rate-limit / reCAPTCHA cooldowns.
                try:
                    self.qm._check_account_holds()
                except Exception as hold_err:
                    self._log(f"[CDPShared] _check_account_holds error: {hold_err}")

                jobs = get_all_jobs()
                pending = [j for j in jobs if j["status"] == "pending"]

                # Clean up stale retry tracking entries for jobs that are
                # no longer in the queue (deleted / moved / completed via
                # other mechanisms).
                now_ts = time.time()
                pending_ids = {j["id"] for j in jobs}
                for rid in list(self.qm.job_retry_after.keys()):
                    if rid not in pending_ids:
                        self.qm.job_retry_after.pop(rid, None)
                        self.qm.job_retry_counts.pop(rid, None)

                # Filter pending jobs by retry_after — jobs whose scheduled
                # retry time hasn't elapsed are skipped this cycle.
                pending_ready = [
                    j for j in pending
                    if self.qm.job_retry_after.get(j["id"], 0) <= now_ts
                ]

                if not pending_ready:
                    if not self._active_tasks and not pending:
                        still = any(
                            j["status"] in ("pending", "running")
                            for j in get_all_jobs()
                        )
                        if not still:
                            self._log("[CDPShared] All jobs completed.")
                            break
                    await asyncio.sleep(self.qm.scheduler_poll_seconds)
                    continue

                pending = pending_ready

                busy = {t.get_name() for t in self._active_tasks if hasattr(t, "get_name")}
                dispatched = 0

                # ── Same-account stagger: match the old browser-per-slot
                # dispatcher behaviour. Without this, 5 parallel slots on
                # a single account fire their jobs within ~2 seconds and
                # Google Flow's batchGenerateImages endpoint returns
                # "Internal error encountered" for ~40-60% of the burst.
                # With a 1.0 s minimum gap per account (default), 5 jobs
                # spread over ~5 s and the error rate collapses to ~0%.
                #
                # The setting already exists in queue_manager
                # (self.qm.same_account_stagger_seconds, default 1.0 s);
                # CDP mode just wasn't consulting it. multitab mode had
                # the same gap — fixed there too.
                acct_stagger = max(0.0, float(self.qm.same_account_stagger_seconds))
                now_ts = time.time()
                stagger_blocked = set()
                if acct_stagger > 0:
                    for acc_name, last_at in self.qm.last_account_dispatch_at.items():
                        if now_ts - last_at < acct_stagger:
                            stagger_blocked.add(acc_name)

                for job in pending:
                    slot = await self._get_available_slot_async(
                        busy, stagger_blocked=stagger_blocked
                    )
                    if not slot:
                        break

                    job_id = job["id"]
                    update_job_status(job_id, "running", account=slot.account_name)
                    self.qm.signals.job_updated.emit(job_id, "running", slot.account_name, "")

                    # Update dispatch-stagger tracker BEFORE task creation so
                    # subsequent iterations in this same poll cycle also see
                    # the account as freshly-dispatched and skip it.
                    self.qm.last_account_dispatch_at[slot.account_name] = time.time()
                    if acct_stagger > 0:
                        stagger_blocked.add(slot.account_name)

                    task = asyncio.create_task(self._run_job(slot, job), name=slot.slot_id)
                    self._active_tasks.append(task)
                    busy.add(slot.slot_id)
                    dispatched += 1

                    stagger = random.uniform(
                        self.qm.global_stagger_min_seconds,
                        self.qm.global_stagger_max_seconds,
                    )
                    if stagger > 0:
                        await asyncio.sleep(stagger)

                if dispatched == 0:
                    await asyncio.sleep(self.qm.scheduler_poll_seconds)

                # Fix 5: Pre-generate reCAPTCHA tokens on idle slots
                # Fire in background — no await, no blocking
                try:
                    for server in self._servers.values():
                        for slot in server.get_all_slots():
                            if (not slot.is_busy
                                    and slot._cached_token is None
                                    and slot.is_healthy(now_ts)):
                                # Schedule background token generation
                                asyncio.create_task(slot.pre_generate_token())
                except Exception:
                    pass

            if self._active_tasks:
                if self.qm.stop_requested or self.qm.force_stop_requested:
                    self._log(f"[CDPShared] Cancelling {len(self._active_tasks)} active job(s)...")
                    for t in self._active_tasks:
                        if not t.done():
                            t.cancel()
                    try:
                        await asyncio.wait_for(
                            asyncio.gather(*self._active_tasks, return_exceptions=True),
                            timeout=3.0,
                        )
                    except asyncio.TimeoutError:
                        self._log("[CDPShared] Some tasks didn't cancel in 3s — continuing.")
                else:
                    self._log(f"[CDPShared] Waiting for {len(self._active_tasks)} active job(s)...")
                    await asyncio.gather(*self._active_tasks, return_exceptions=True)

        finally:
            for server in self._servers.values():
                await server.stop()
            self._servers.clear()
            self._log("[CDPShared] All browsers and slots stopped.")

    async def _get_available_slot_async(self, busy_slots, stagger_blocked=None):
        """Round-robin across accounts with on-demand slot creation.

        Account-level skip conditions (old-mode parity):
          1. Hard-clean in progress (``server._is_cleaning``)
          2. Same-account dispatch stagger not yet elapsed (``stagger_blocked``)
          3. Account on hold via ``queue_manager._put_account_on_hold``
             (auth / rate limit / access denied). Checked via
             ``self.qm.account_disabled``.
          4. reCAPTCHA streak cooldown active
             (``self.qm.account_recaptcha_until[name] > now``). Other
             accounts keep running — only the cooldowning one pauses.

        Then:
          5. Pass 1 — reuse an idle slot on the first eligible account.
          6. Pass 2 — create a new slot on-demand on the first eligible
             account (up to that server's max).
          7. Rotate the winning account to the end of _rr_order so the
             next dispatch prefers the next account.
        """
        if not self._rr_order:
            return None

        blocked = stagger_blocked or set()
        now_ts = time.time()

        def _account_eligible(name, server):
            """All old-mode gates in one place."""
            if server is None:
                return False
            if getattr(server, "_is_cleaning", False):
                return False
            if name in blocked:
                return False
            # Account on hold (auth / rate limit / suspension)
            if self.qm.account_disabled.get(name):
                return False
            # reCAPTCHA streak cooldown
            recap_until = self.qm.account_recaptcha_until.get(name, 0)
            if recap_until and recap_until > now_ts:
                return False
            return True

        # Pass 1: reuse idle slot across accounts (round-robin)
        order = list(self._rr_order)
        for name in order:
            server = self._servers.get(name)
            if not _account_eligible(name, server):
                continue
            for slot in server.get_all_slots():
                # Skip quarantined slots (Fix 7 — health tracking)
                if slot.slot_id not in busy_slots and slot.is_healthy(now_ts):
                    # Move this account to end of rotation
                    try:
                        self._rr_order.remove(name)
                        self._rr_order.append(name)
                    except ValueError:
                        pass
                    return slot

        # Pass 2: create new slot on-demand (round-robin)
        for name in order:
            server = self._servers.get(name)
            if not _account_eligible(name, server):
                continue
            try:
                new_slot = await server.get_or_create_slot()
            except Exception as e:
                self._log(f"[CDPShared] {name}: get_or_create_slot error: {str(e)[:80]}")
                new_slot = None
            if new_slot is not None and new_slot.slot_id not in busy_slots and not new_slot.is_busy:
                try:
                    self._rr_order.remove(name)
                    self._rr_order.append(name)
                except ValueError:
                    pass
                return new_slot

        return None

    async def _run_job(self, slot, job):
        """Execute a single job attempt. On failure, delegate to
        ``_handle_cdp_job_failure`` which requeues the job via the shared
        ``queue_manager`` retry state — the NEXT dispatch cycle picks it
        up and routes it to whichever slot is currently free (possibly
        on a different account entirely).

        This mirrors the old browser-per-slot mode behaviour: single
        attempt per slot, requeue on failure, natural retry-to-different-
        slot rotation. That's what breaks Google Flow's load-balancer
        session stickiness — inline reload on the same context does NOT
        because Google's routing is keyed on account + request hash,
        not on browser session.
        """
        job_id = job["id"]

        # Load FULL job payload from DB — get_all_jobs() used by the dispatcher
        # omits aspect_ratio, output_index, is_retry, ref_paths, video_ratio, etc.
        # Without this, CDP mode silently defaults aspect_ratio and loses the
        # preserved output_index on retries (causing wrong filenames).
        try:
            full_payload = self.qm._load_job_payload(job_id)
            if isinstance(full_payload, dict) and full_payload:
                # Merge — full payload takes precedence over the dispatch summary
                job = {**job, **full_payload}
        except Exception as e:
            self._log(f"[{slot.slot_id}] Warning: _load_job_payload failed: {str(e)[:80]}")

        job_type = job.get("job_type", "image")
        prompt = job.get("prompt", "")
        model = job.get("model", "")

        self._log(f"[{slot.slot_id}] Job {job_id[:6]}...: {prompt[:40]}...")

        try:
            if "video" in job_type:
                video_model = job.get("video_model") or model
                ratio = job.get("aspect_ratio", "ASPECT_RATIO_16_9")
                result, error = await slot.generate_video(prompt, video_model, ratio)
                media_tag = "video"
            else:
                ratio = job.get("aspect_ratio", "IMAGE_ASPECT_RATIO_LANDSCAPE")
                result, error = await slot.generate_image(prompt, model, ratio)
                media_tag = "image"

            # ── Success path ──
            if result and not error:
                saved = await slot._save_generation_result(
                    result, job, media_tag=media_tag
                )
                if not saved:
                    await self._handle_cdp_job_failure(
                        slot,
                        job_id,
                        "Generation succeeded but file save failed (no downloadable media)",
                    )
                    return

                update_job_status(job_id, "completed", account=slot.account_name)
                self.qm.signals.job_updated.emit(
                    job_id, "completed", slot.account_name, ""
                )
                self.qm.signals.account_auth_status.emit(
                    slot.account_name, "logged_in", "Success"
                )
                self._log(
                    f"[{slot.slot_id}] Job {job_id[:6]}... completed! ({len(saved)} file(s))"
                )

                # Clear any retry state from previous failures on this job
                self.qm.job_retry_counts.pop(job_id, None)
                self.qm.job_retry_after.pop(job_id, None)
                # Reset account reCAPTCHA streak on successful generation
                self.qm.account_recaptcha_streak.pop(slot.account_name, None)

                # HARD CLEAN trigger — only on SUCCESSFUL save.
                try:
                    server = self._servers.get(slot.account_name)
                    if server is not None and server.note_job_completed():
                        asyncio.create_task(server._maybe_hard_clean())
                except Exception as e:
                    self._log(
                        f"[{slot.slot_id}] hard-clean trigger error: {str(e)[:80]}"
                    )
                return

            # ── Failure path ── (generate_* returned (None/empty, error))
            await self._handle_cdp_job_failure(
                slot, job_id, error or "Unknown error"
            )

        except Exception as e:
            # Unexpected exception during execution
            await self._handle_cdp_job_failure(slot, job_id, str(e)[:200])

    async def _handle_cdp_job_failure(self, slot, job_id, error_msg):
        """Port of ``queue_manager._handle_job_failure`` for CDP Shared mode.

        Uses all the old-mode helpers via ``self.qm.*``:

          * ``_classify_error``        — categorize the error
          * ``_is_retryable_error``    — decide retry vs fail
          * ``_is_high_priority_retry_error`` — +1 retry budget for
                                          timeouts / session drops /
                                          reCAPTCHA / download failures
          * ``_get_retry_delay_seconds`` — smart delay per error type
                                          (20 s base for Flow backend
                                          5xx, 30 s for timeouts, etc.)
          * ``_put_account_on_hold``   — 300 s hold for auth / rate limit,
                                          1800 s for access denied
          * ``account_recaptcha_streak`` / ``recaptcha_account_cooldown_seconds``
                                          — streak-based reCAPTCHA cooldown
          * ``job_retry_counts`` / ``job_retry_after``
                                          — per-job retry tracking

        On retryable failures the job is requeued to 'pending' with a
        ``retry_after`` timestamp. The NEXT dispatch cycle picks it up
        and routes it to whichever slot is free at that moment, which
        naturally rotates away from any degraded backend assignment
        (since Google Flow's load balancer hashes on request metadata,
        a request from a different slot/timestamp lands on a different
        backend).
        """
        account_name = slot.account_name
        label = slot.slot_id
        category = self.qm._classify_error(error_msg)
        retryable = self.qm._is_retryable_error(error_msg)
        max_retries_for_error = self.qm.max_auto_retries_per_job + (
            1 if self.qm._is_high_priority_retry_error(error_msg) else 0
        )

        self._log(
            f"[{label}] Failure: category={category}, "
            f"retryable={'yes' if retryable else 'no'}: {str(error_msg)[:120]}"
        )

        msg_lower = (error_msg or "").lower()

        # ── Account-level holds for serious errors (old-mode parity) ──
        if category in ("auth_missing", "project_resolution_failed"):
            self.qm._put_account_on_hold(
                account_name, f"session expired ({category})", 300
            )
        elif "session not signed in" in msg_lower or "not signed in" in msg_lower:
            self.qm._put_account_on_hold(account_name, "session not signed in", 300)
        elif any(p in msg_lower for p in (
            "rate limit", "429", "quota exceeded", "resource exhausted",
            "too many requests", "quota_exceeded", "rate_limit",
        )):
            self.qm._put_account_on_hold(account_name, "rate limited", 300)
        elif "access denied" in msg_lower or "account suspended" in msg_lower:
            self.qm._put_account_on_hold(account_name, "access denied", 1800)

        # ── Moderation → fail permanently, no retry ──
        if category == "moderated":
            self.qm.job_retry_counts.pop(job_id, None)
            self.qm.job_retry_after.pop(job_id, None)
            normalized = str(error_msg or "").strip()
            if normalized and not normalized.startswith("MODERATION:"):
                normalized = f"MODERATION: {normalized}"
            final_error = f"[moderated] {normalized or 'Content blocked by policy filter'}"
            update_job_status(job_id, "failed", account=account_name, error=final_error)
            self.qm.signals.job_updated.emit(job_id, "failed", account_name, final_error)
            self._log(
                f"[{label}] Prompt blocked by content filter. Marked as failed (no retry)."
            )
            return

        # ── reCAPTCHA: reload page + apply streak-based account cooldown ──
        if "recaptcha" in msg_lower:
            self._log(
                f"[{label}] reCAPTCHA issue — reloading page + applying account cooldown..."
            )
            try:
                await slot._try_reload()
            except Exception:
                pass
            streak = self.qm.account_recaptcha_streak.get(account_name, 0) + 1
            self.qm.account_recaptcha_streak[account_name] = streak
            cooldown_seconds = self.qm.recaptcha_account_cooldown_seconds * min(streak, 3)
            self.qm.account_recaptcha_until[account_name] = time.time() + cooldown_seconds
            self._log(
                f"[{label}] Account {account_name}: reCAPTCHA cooldown "
                f"{cooldown_seconds}s (streak {streak}). Other accounts continue."
            )

        # ── Session-drop detection: the context/page died on us ──
        if self.qm._is_session_drop_error(error_msg):
            self._log(
                f"[{label}] Browser session dropped — slot will re-create context on next use."
            )
            # CDP mode's slot context is tied to the browser; reload is
            # the best we can do without full hard-clean. The hard-clean
            # cycle will fully rebuild contexts eventually.
            try:
                await slot._try_reload()
            except Exception:
                pass

        # ── Retry decision ──
        retry_count = self.qm.job_retry_counts.get(job_id, 0)
        if max_retries_for_error > 0 and retryable:
            if retry_count < max_retries_for_error:
                retry_count += 1
                self.qm.job_retry_counts[job_id] = retry_count
                retry_delay = self.qm._get_retry_delay_seconds(error_msg, retry_count)
                if retry_delay > 0:
                    self.qm.job_retry_after[job_id] = time.time() + retry_delay
                    retry_note = (
                        f"Auto-retry scheduled ({retry_count}/{max_retries_for_error}) "
                        f"after {retry_delay}s [{category}]: {str(error_msg)[:120]}"
                    )
                else:
                    self.qm.job_retry_after.pop(job_id, None)
                    retry_note = (
                        f"Auto-retry scheduled ({retry_count}/{max_retries_for_error}) "
                        f"immediately [{category}]: {str(error_msg)[:120]}"
                    )
                # REQUEUE — put job back to 'pending' so the dispatcher
                # picks it up on whatever slot is free next. This is the
                # KEY difference from inline retry: the requeue naturally
                # rotates the retry to a different slot/context, which
                # breaks Google Flow's load-balancer session stickiness.
                update_job_status(job_id, "pending", account="", error=retry_note)
                self.qm.signals.job_updated.emit(job_id, "pending", "", retry_note)
                self._log(f"[{label}] {retry_note}")
                return

        # ── Retries exhausted OR non-retryable → mark failed permanently ──
        self.qm.job_retry_counts.pop(job_id, None)
        self.qm.job_retry_after.pop(job_id, None)
        final_error = f"[{category}] {error_msg}"
        update_job_status(job_id, "failed", account=account_name, error=final_error)
        self.qm.signals.job_updated.emit(job_id, "failed", account_name, final_error)
        self._log(
            f"[{label}] Job {job_id[:6]}... failed [{category}]: {str(error_msg)[:200]}"
        )
