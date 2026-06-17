"""
Extension Mode — Chrome Extension + Direct API calls.

Architecture:
  Chrome Extension → reCAPTCHA token + auth session
  Python (aiohttp) → Direct API calls to Google Labs
  NO browser launched by Python. Zero CDP. Real Chrome.

RAM: ~50MB (just Python HTTP calls, no browser process)
Speed: Same as HTTP Shared (~20-40 threads possible)
reCAPTCHA: Best possible (real Chrome, world: "MAIN", zero detection)
"""

import asyncio
import base64
import json
import mimetypes
import os
import random
import time
import uuid
from typing import Optional, Dict, Any, List

try:
    import aiohttp
except ImportError:
    aiohttp = None


# ─────────────────────────────────────────────────────────────────
# SSL context for aiohttp calls
#
# PyInstaller-bundled Python on Windows often ships without a working
# CA trust store, so aiohttp's default SSL verification fails against
# Google's certs with:
#   SSLCertVerificationError: unable to get local issuer certificate
#
# We build a shared SSL context from certifi (Mozilla's CA bundle —
# always available because aiohttp already depends on it transitively)
# and reuse it across every ClientSession in this module. Fallback to
# the system default context if certifi ever disappears.
# ─────────────────────────────────────────────────────────────────
_SSL_CONTEXT = None


def _get_ssl_context():
    """Return a process-wide SSL context that trusts certifi's CA bundle.
    Cached on first use. Safe to call even when `ssl`/`certifi` fail to
    import — falls back to None (which lets aiohttp use its default).
    """
    global _SSL_CONTEXT
    if _SSL_CONTEXT is not None:
        return _SSL_CONTEXT
    try:
        import ssl as _ssl
        try:
            import certifi
            _SSL_CONTEXT = _ssl.create_default_context(cafile=certifi.where())
        except Exception:
            _SSL_CONTEXT = _ssl.create_default_context()
    except Exception:
        _SSL_CONTEXT = None
    return _SSL_CONTEXT


def _make_aiohttp_session(**kwargs):
    """Build an aiohttp.ClientSession with our certifi-backed SSL context.
    Drop-in replacement for bare `aiohttp.ClientSession()` — extra kwargs
    forward through to the real constructor.
    """
    ctx = _get_ssl_context()
    if ctx is None:
        return aiohttp.ClientSession(**kwargs)
    connector = aiohttp.TCPConnector(ssl=ctx)
    return aiohttp.ClientSession(connector=connector, **kwargs)


from src.core.extension_bridge import ExtensionBridge
from src.db.db_manager import (
    get_accounts,
    get_all_jobs,
    get_bool_setting,
    get_setting,
    get_int_setting,
    get_output_directory,
    set_setting,
    update_job_status,
    update_job_runtime_state,
    get_cached_media_id,
    set_cached_media_id,
)

# Google Labs API endpoints
IMAGE_API_URL = "https://aisandbox-pa.googleapis.com/v1/projects/{project_id}/flowMedia:batchGenerateImages"
VIDEO_API_URL = "https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoText"
AUTH_SESSION_URL = "https://labs.google/fx/api/auth/session"
PROJECT_CREATE_URL = "https://aisandbox-pa.googleapis.com/v1/projects"
UPLOAD_IMAGE_URL = "https://aisandbox-pa.googleapis.com/v1/flow/uploadImage"
VIDEO_REFERENCE_URL = "https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoReferenceImages"
VIDEO_START_IMAGE_URL = "https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoStartImage"
VIDEO_POLL_URL = "https://aisandbox-pa.googleapis.com/v1/video:batchCheckAsyncVideoGenerationStatus"


def _parse_api_error(status_code: int, resp_text: str) -> str:
    """Parse Google Labs API error response into a clear, human-readable message."""
    text_lower = (resp_text or "").lower()

    # Try to extract structured error message from JSON
    detail = ""
    try:
        err_json = json.loads(resp_text)
        if isinstance(err_json, dict):
            err_obj = err_json.get("error", err_json)
            detail = (
                err_obj.get("message", "")
                or err_obj.get("details", "")
                or err_obj.get("status", "")
            )
            if isinstance(detail, list) and detail:
                detail = str(detail[0])
            detail = str(detail).strip()
    except (json.JSONDecodeError, TypeError, KeyError):
        detail = resp_text[:200].strip()

    detail_lower = detail.lower() if detail else text_lower

    # ── 403 Errors ──
    if status_code == 403:
        if "recaptcha" in text_lower:
            return "⛔ reCAPTCHA Score Too Low — Google rejected the token (score below threshold). Tab may need reload."
        if "quota" in text_lower or "rate" in text_lower:
            return "⛔ Rate Limited (403) — Account quota exceeded or too many requests."
        # MODEL_ACCESS_DENIED → account is logged in but doesn't have access
        # to this specific model (typically Veo video on a free-tier account).
        # Prefix is matched downstream to fail-fast (no retry, no reCAPTCHA
        # burn) and skip remaining jobs of the same model on this account.
        if "model_access_denied" in text_lower or "model access denied" in text_lower:
            return "MODEL_ACCESS_DENIED: ⛔ Account lacks access to this model — Veo video usually requires Google AI Premium / paid plan. Free accounts only get image generation."
        if "permission" in text_lower or "forbidden" in text_lower:
            return f"⛔ Access Denied (403) — Account doesn't have permission. {detail[:100]}"
        return f"⛔ Forbidden (403) — {detail[:150] or 'Google rejected the request.'}"

    # ── 401 Errors ──
    if status_code == 401:
        return "🔑 Access Token Expired (401) — Session expired, need fresh auth from extension."

    # ── 400 Errors (most common, multiple causes) ──
    if status_code == 400:
        if "recaptcha" in text_lower:
            return "⚠️ reCAPTCHA Token Expired/Invalid (400) — Token was stale or malformed by the time API received it."
        if any(k in text_lower for k in ("expired", "token_expired", "invalid_token")):
            return "🔑 Access Token Expired (400) — Auth session needs refresh."
        if any(k in text_lower for k in ("project", "project_id", "project not found")):
            return "📁 Invalid Project ID (400) — Project not found or was deleted. Will auto-create on retry."
        if any(k in text_lower for k in (
            "safety", "blocked", "policy", "filter", "harmful",
            "inappropriate", "violat", "content_filter", "responsible_ai",
        )):
            return f"🚫 Prompt Blocked by Content Filter (400) — Google's safety filter rejected this prompt."
        if any(k in text_lower for k in ("invalid", "malformed", "parse", "field")):
            return f"❌ Malformed Request (400) — {detail[:150]}"
        # Generic 400 with detail
        return f"⚠️ Bad Request (400) — {detail[:200] or 'Unknown cause.'}"

    # ── 429 Rate Limit ──
    if status_code == 429:
        # Surface whatever detail Google sends alongside the 429 so we can
        # tell apart: "burst rate limit" (recovers in ~60s), "quota used
        # up" (recovers at midnight PT), "account flagged" (persistent),
        # and "reCAPTCHA-derived throttle" (need score recovery). Without
        # the detail every 429 looks the same and we chase the wrong fix.
        body_preview = resp_text[:300] if resp_text else ""
        detail_preview = detail[:200] if detail else ""
        shown = detail_preview or body_preview or "no body"
        return f"🕐 Rate Limited (429) — {shown}"

    # ── 500+ Server Errors ──
    if status_code >= 500:
        return f"🔧 Google Server Error ({status_code}) — Temporary issue, will retry."

    # ── Fallback ──
    return f"HTTP {status_code}: {detail[:200] or resp_text[:200]}"


def _resolve_image_model(model_name):
    """Map UI model name to API model identifier."""
    lower = str(model_name or "").strip().lower()
    # Nano Banana models (must check "pro" first — it also contains "nano banana")
    if "nano banana pro" in lower:
        return "GEM_PIX_2"
    if "nano banana" in lower:
        return "NARWHAL"
    # ALL Imagen models (including Imagen 4) map to NARWHAL — same as http_mode
    if "imagen" in lower:
        return "NARWHAL"
    # Pass through already-resolved uppercase identifiers
    if model_name and model_name == model_name.upper():
        return model_name
    return "NARWHAL"


def _resolve_image_ratio(ratio_name):
    """Map UI ratio to API ratio identifier."""
    raw = str(ratio_name or "").strip()
    # Pass through already-resolved identifiers
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
    """Map UI video quality name to API video model key."""
    source = str(video_model or model or "").strip().lower()
    if not source:
        return "veo_3_1_t2v_fast"
    if "lite" in source:
        return "veo_3_1_t2v_lite"
    if "lower pri" in source or "relaxed" in source:
        return "veo_3_1_t2v_fast_relaxed"
    if "quality" in source:
        return "veo_3_1_t2v"
    # Pass through already-resolved keys (contain underscores)
    if "_" in source:
        return source
    return "veo_3_1_t2v_fast"


def _resolve_video_ratio(ratio_name):
    """Map UI ratio to API video ratio identifier."""
    raw = str(ratio_name or "").strip()
    # Pass through already-resolved identifiers
    if raw.startswith("VIDEO_ASPECT_RATIO_"):
        return raw
    # Convert IMAGE_ prefix to VIDEO_
    if raw.startswith("IMAGE_ASPECT_RATIO_"):
        return raw.replace("IMAGE_", "VIDEO_", 1)
    lower = raw.lower()
    if "portrait" in lower or "9:16" in lower:
        return "VIDEO_ASPECT_RATIO_PORTRAIT"
    if "square" in lower or "1:1" in lower:
        return "VIDEO_ASPECT_RATIO_SQUARE"
    return "VIDEO_ASPECT_RATIO_LANDSCAPE"


def _normalize_video_sub_mode(video_sub_mode="", ref_path=None, start_image_path=None, end_image_path=None):
    """Determine video sub-mode from explicit value or infer from provided paths."""
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


def _resolve_video_model_for_sub_mode(video_sub_mode, model="", video_model="", ratio="", plan="ultra"):
    """Pick the correct video model key based on sub-mode, quality tier, and plan.

    ┌─────────────────────────────────────────────────────────────────┐
    │ 📚 CANONICAL REFERENCE: docs/veo_model_mapping.md               │
    │                                                                 │
    │ All mapping decisions in this function are documented and       │
    │ live-verified there. If the API ever returns MODEL_NOT_FOUND    │
    │ or INVALID_MODEL, follow the "Quick re-verification ritual"     │
    │ section in that doc to capture a fresh model name and update    │
    │ both the doc AND this function in lock-step.                    │
    │                                                                 │
    │ Last full verification: 2026-04-20 (Ultra plan, Veo 3.1).       │
    └─────────────────────────────────────────────────────────────────┘

    Pro and Ultra accounts use DIFFERENT model name suffixes — verified
    against a real Ultra-account HAR capture (sku=WS_ULTRA, tier=
    PAYGATE_TIER_TWO). Sending a Pro-style model name on an Ultra account
    returns 403 PUBLIC_ERROR_MODEL_ACCESS_DENIED.

    plan: "ultra" → veo_3_1_*_ultra (PAYGATE_TIER_TWO)
          "pro"   → veo_3_1_*       (PAYGATE_TIER_ONE)
    """
    source = str(video_model or model or "").strip().lower()
    plan_lower = str(plan or "ultra").strip().lower()
    # Determine quality tier. labs.google.com exposes 5 distinct tiers
    # for Veo (verified against the Ultra UI screenshot):
    #   1. Fast                    → "fast"
    #   2. Lite                    → "lite"
    #   3. Quality                 → "quality"
    #   4. Fast [Lower Priority]   → "lower_pri"      (= fast + relaxed)
    #   5. Lite [Lower Priority]   → "lite_lower_pri" (= lite + relaxed)
    # Detection has to check the COMBINATION first, otherwise the bare
    # "lite" keyword would absorb option 5 and drop the lower-priority
    # bit. Same for "fast" + "lower" → option 4.
    has_lite = "lite" in source
    has_lower = ("lower pri" in source or "lower_pri" in source
                 or "relaxed" in source)
    if has_lite and has_lower:
        tier = "lite_lower_pri"
    elif has_lite:
        tier = "lite"
    elif has_lower:
        tier = "lower_pri"
    elif "quality" in source:
        tier = "quality"
    else:
        tier = "fast"

    # ════════════════════════════════════════════════════════════════
    # Model resolution rebuilt from real labs.google.com Ultra captures
    # (DevTools fetch wrapper, 2026-04). Big surprises that the old
    # static table got wrong:
    #
    #   1. Lite is its OWN model family in every sub-mode — it does NOT
    #      collapse to Fast. Pattern: veo_3_1_{family}_lite (no _ultra,
    #      no ratio, no _s on i2v).
    #   2. Lite [Lower Pri] uses suffix "_low_priority", NOT "_relaxed"
    #      like Fast LP. Confirmed in t2v + r2v + i2v + interpolation.
    #   3. frames_start_end is a DIFFERENT family ("interpolation"), not
    #      i2v_s with _fl suffix.
    #   4. The _s suffix on i2v only appears on the Fast variants
    #      (i2v_s_fast_ultra). Lite/Lite-LP drop it (just i2v_lite).
    #   5. _ultra suffix only on Fast/Fast-LP variants. Lite/Lite-LP/
    #      Quality skip it.
    #
    # Family prefix per sub-mode:
    #   text_to_video    → t2v
    #   ingredients      → r2v          (Fast keeps _s? not yet seen)
    #   frames_start     → i2v          (Fast has _s, Lite drops it)
    #   frames_start_end → interpolation (entirely new family!)
    # ════════════════════════════════════════════════════════════════
    is_ultra = (plan_lower == "ultra")
    ultra_suffix = "_ultra" if is_ultra else ""

    # ── Text-to-video ────────────────────────────────────────────────
    # All major combos verified via live captures; Square is inferred.
    if video_sub_mode == "text_to_video":
        if tier == "lite":
            return "veo_3_1_t2v_lite"                    # CONFIRMED
        if tier == "lite_lower_pri":
            return "veo_3_1_t2v_lite_low_priority"       # CONFIRMED
        if tier == "quality":
            return "veo_3_1_t2v"                         # CONFIRMED
        # Fast / Fast LP — ratio encoded for non-landscape.
        # Note: Flow does NOT support Square video ratio — UI only exposes
        # Landscape + Portrait. So no _square branch needed here.
        api_ratio = _resolve_video_ratio(ratio)
        ratio_part = "_portrait" if "PORTRAIT" in api_ratio else ""
        relaxed_part = "_relaxed" if tier == "lower_pri" else ""
        return f"veo_3_1_t2v_fast{ratio_part}{ultra_suffix}{relaxed_part}"

    # ── Ingredients (R2V) ────────────────────────────────────────────
    # Lite + Lite [LP] CONFIRMED — no ratio, no _ultra suffix.
    # Fast Landscape CONFIRMED — veo_3_1_r2v_fast_landscape_ultra.
    # Quality: Flow UI doesn't expose Quality for ingredients officially —
    # it falls back to Fast. We keep the same model for safety.
    if video_sub_mode == "ingredients":
        if tier == "lite":
            return "veo_3_1_r2v_lite"                    # CONFIRMED
        if tier == "lite_lower_pri":
            return "veo_3_1_r2v_lite_low_priority"       # CONFIRMED
        # Fast / Fast LP / Quality — ratio + _ultra + maybe _relaxed.
        # Square not supported by Flow for video; UI only allows L/P.
        api_ratio = _resolve_video_ratio(ratio)
        ratio_short = "portrait" if "PORTRAIT" in api_ratio else "landscape"
        relaxed_suffix = "_relaxed" if tier == "lower_pri" else ""
        # Quality collapses to Fast (no separate r2v_quality model exists).
        return f"veo_3_1_r2v_fast_{ratio_short}{ultra_suffix}{relaxed_suffix}"

    # ── Frames Start (single image → video, "i2v" family) ────────────
    # Lite + Lite LP CONFIRMED — drop the _s suffix entirely, no ratio,
    # no _ultra. Fast confirmed via HAR (i2v_s_fast_ultra). Other tiers
    # follow the inferred pattern.
    if video_sub_mode == "frames_start":
        if tier == "lite":
            return "veo_3_1_i2v_lite"                    # CONFIRMED
        if tier == "lite_lower_pri":
            return "veo_3_1_i2v_lite_low_priority"       # CONFIRMED
        if tier == "quality":
            return "veo_3_1_i2v_s"                       # CONFIRMED
        # Fast / Fast LP — keep _s, add _ultra, optional _relaxed
        relaxed_suffix = "_relaxed" if tier == "lower_pri" else ""
        return f"veo_3_1_i2v_s_fast{ultra_suffix}{relaxed_suffix}"

    # ── Frames Start-End (start + end → video) — HYBRID family ───────
    # Live captures revealed this is a SPLIT family, not pure-interpolation:
    #   • Fast / Fast LP / Quality → "i2v_s_*_fl" family (the OG suffix)
    #   • Lite / Lite [LP]         → "interpolation_*" family (newer)
    # Both endpoints share the same /batchAsyncGenerateVideoStartAndEndImage
    # endpoint — only the videoModelKey differs by tier.
    #
    # Quirk: Google's _fl suffix moves position depending on tier!
    #   Fast plain : veo_3_1_i2v_s_fast_ultra_fl       (_fl at END)
    #   Fast LP    : veo_3_1_i2v_s_fast_fl_ultra_relaxed (_fl in MIDDLE)
    # Both confirmed via live captures.
    if video_sub_mode == "frames_start_end":
        if tier == "lite":
            return "veo_3_1_interpolation_lite"                # CONFIRMED
        if tier == "lite_lower_pri":
            return "veo_3_1_interpolation_lite_low_priority"   # CONFIRMED
        if tier == "quality":
            return "veo_3_1_i2v_s_fl"                          # CONFIRMED
        if tier == "lower_pri":
            # Fast LP: _fl moves before _ultra, _relaxed at end
            return f"veo_3_1_i2v_s_fast_fl{ultra_suffix}_relaxed"  # CONFIRMED
        # Fast plain: _fl stays at the end
        return f"veo_3_1_i2v_s_fast{ultra_suffix}_fl"          # CONFIRMED

    # Last-resort fallback for unknown sub_mode/tier combos
    return "veo_3_1_t2v_fast_ultra" if is_ultra else "veo_3_1_t2v_fast"


def _paygate_tier_for_plan(plan):
    """Map plan name to API paygate tier string."""
    return "PAYGATE_TIER_TWO" if str(plan or "ultra").strip().lower() == "ultra" else "PAYGATE_TIER_ONE"


# Video endpoint lookup
VIDEO_ENDPOINTS = {
    "text_to_video": VIDEO_API_URL,
    "ingredients": VIDEO_REFERENCE_URL,
    "frames_start": VIDEO_START_IMAGE_URL,
    "frames_start_end": "https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoStartAndEndImage",
}


class ExtensionWorker:
    """A lightweight worker that uses the bridge for tokens and makes direct API calls."""

    # Class-level lock per account: prevents 7 workers all trying to create project at once
    _project_locks: Dict[str, asyncio.Lock] = {}
    # Class-level reference upload cache: (project_id, file_path) -> media_name
    _reference_cache: Dict[tuple, str] = {}
    _reference_cache_locks: Dict[tuple, asyncio.Lock] = {}

    def __init__(self, slot_id: str, account_email: str, bridge: ExtensionBridge, log_fn):
        self.slot_id = slot_id
        self.account_email = account_email
        self._bridge = bridge
        self._log = log_fn
        self.is_busy = False
        self.last_access_token = None  # cached for download auth
        self.jobs_completed = 0

    async def _upload_reference_image(self, access_token, project_id, file_path):
        """Upload a single reference image via aiohttp, return media name."""
        if not file_path or not os.path.exists(file_path):
            raise RuntimeError(f"Reference file not found: {file_path}")

        with open(file_path, "rb") as f:
            image_bytes_b64 = base64.b64encode(f.read()).decode("utf-8")

        file_name = os.path.basename(file_path)
        mime_type, _ = mimetypes.guess_type(file_path)
        mime_type = mime_type or "image/jpeg"

        body = {
            "clientContext": {
                "projectId": project_id,
                "tool": "PINHOLE",
                "sessionId": f";{int(time.time() * 1000)}",
            },
            "fileName": file_name,
            "mimeType": mime_type,
            "imageBytes": image_bytes_b64,
            "isHidden": False,
            "isUserUploaded": True,
        }

        async with _make_aiohttp_session() as session:
            async with session.post(
                UPLOAD_IMAGE_URL,
                headers={
                    "content-type": "text/plain;charset=UTF-8",
                    "authorization": f"Bearer {access_token}",
                    "origin": "https://labs.google",
                    "referer": "https://labs.google/",
                },
                data=json.dumps(body),
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                resp_text = await resp.text()
                if not resp.ok:
                    raise RuntimeError(f"Upload failed: {_parse_api_error(resp.status, resp_text)}")

                try:
                    data = json.loads(resp_text)
                except json.JSONDecodeError:
                    raise RuntimeError(f"Upload response not JSON: {resp_text[:200]}")

                media_name = (
                    data.get("name")
                    or data.get("mediaName")
                    or (data.get("media", {}).get("name") if isinstance(data.get("media"), dict) else "")
                    or (data["media"][0].get("name", "") if isinstance(data.get("media"), list) and data["media"] else "")
                )
                if not media_name:
                    raise RuntimeError(f"Upload response missing media name: {resp_text[:200]}")

                self._log(f"[{self.slot_id}] Uploaded reference: {file_name} -> {media_name}")
                return media_name

    async def _upload_and_cache_reference(self, access_token, project_id, file_path):
        """Upload reference image with 2-level cache (memory + DB) to avoid duplicate uploads.

        Memory cache is fast but lost on restart.
        DB cache persists across restarts — no re-upload needed after app restart.
        """
        abs_path = os.path.abspath(file_path)
        cache_key = (project_id, abs_path)

        # Level 1: memory cache (fast path)
        cached = ExtensionWorker._reference_cache.get(cache_key)
        if cached:
            self._log(f"[{self.slot_id}] Reference cached: {os.path.basename(file_path)}")
            return cached

        # Level 2: DB cache (survives restart)
        db_cached = get_cached_media_id(project_id, abs_path)
        if db_cached:
            ExtensionWorker._reference_cache[cache_key] = db_cached
            self._log(f"[{self.slot_id}] Reference restored from DB: {os.path.basename(file_path)}")
            return db_cached

        # Get or create lock for this specific file+project
        if cache_key not in ExtensionWorker._reference_cache_locks:
            ExtensionWorker._reference_cache_locks[cache_key] = asyncio.Lock()
        lock = ExtensionWorker._reference_cache_locks[cache_key]

        async with lock:
            # Re-check after acquiring lock (another worker may have uploaded)
            cached = ExtensionWorker._reference_cache.get(cache_key)
            if cached:
                return cached

            media_name = await self._upload_reference_image(access_token, project_id, file_path)
            # Store in both caches
            ExtensionWorker._reference_cache[cache_key] = media_name
            set_cached_media_id(project_id, abs_path, media_name)
            return media_name

    async def _upload_references(self, access_token, project_id, ref_paths):
        """Upload multiple reference images, return list of media IDs."""
        media_ids = []
        for path in ref_paths:
            path = str(path).strip()
            if not path:
                continue
            media_name = await self._upload_and_cache_reference(access_token, project_id, path)
            media_ids.append(media_name)
        return media_ids

    # Moderation / safety keywords — non-retryable failures
    _MODERATION_KEYWORDS = (
        "PROMINENT_PERSON", "SAFETY_FILTER", "CONTENT_POLICY", "MODERATION",
        "BLOCKED", "HARMFUL", "SEXUALLY_EXPLICIT", "VIOLENCE", "HATE_SPEECH",
        "DANGEROUS", "TOXIC", "CHILD_SAFETY", "FILTER_FAILED",
        "PUBLIC_ERROR_PROMINENT_PEOPLE_FILTER_FAILED",
        "PUBLIC_ERROR_SAFETY_FILTER_FAILED",
        "PUBLIC_ERROR_CONTENT_POLICY",
    )

    @staticmethod
    def _is_moderation_failure(detail: str) -> bool:
        upper = str(detail or "").upper()
        return any(kw in upper for kw in ExtensionWorker._MODERATION_KEYWORDS)

    async def _poll_video_status(self, access_token, media_id, project_id, poll_interval=5, max_polls=60):
        """Poll video generation status until complete or failed.

        Returns (status, error) where status is 'completed', 'failed', 'moderation', or 'timeout'.
        """
        for poll_num in range(1, max_polls + 1):
            await asyncio.sleep(poll_interval)

            poll_body = {
                "media": [{"name": media_id, "projectId": project_id}],
            }

            try:
                async with _make_aiohttp_session() as session:
                    async with session.post(
                        VIDEO_POLL_URL,
                        headers={
                            "content-type": "text/plain;charset=UTF-8",
                            "authorization": f"Bearer {access_token}",
                            "origin": "https://labs.google",
                            "referer": "https://labs.google/",
                        },
                        data=json.dumps(poll_body),
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as resp:
                        resp_text = await resp.text()
                        if not resp.ok:
                            if poll_num % 3 == 0:
                                self._log(f"[{self.slot_id}] Poll {poll_num} HTTP {resp.status}")
                            continue

                        data = json.loads(resp_text)

                        # Track remaining credits
                        remaining_credits = data.get("remainingCredits")
                        if remaining_credits is not None and poll_num <= 1:
                            try:
                                self._log(f"[CREDITS] Remaining: {int(remaining_credits)}")
                            except Exception:
                                pass

                        media_list = data.get("media", [])
                        if not media_list:
                            continue

                        media_item = media_list[0] if isinstance(media_list, list) else {}
                        media_metadata = media_item.get("mediaMetadata", {})
                        media_status = media_metadata.get("mediaStatus", {})
                        status = media_status.get("mediaGenerationStatus", "UNKNOWN")

                        # Safety filter info
                        safety_filter = media_metadata.get("safetyFilterResult", "")

                        if status == "MEDIA_GENERATION_STATUS_SUCCESSFUL":
                            if remaining_credits is not None:
                                try:
                                    self._log(f"[CREDITS] Remaining: {int(remaining_credits)}")
                                except Exception:
                                    pass
                            item_name = media_item.get("name", "") if isinstance(media_item, dict) else ""
                            wf_id = media_item.get("workflowId", "") if isinstance(media_item, dict) else ""
                            self._log(f"[{self.slot_id}] Video complete (poll {poll_num})")
                            self._log(f"[{self.slot_id}] media name={item_name}, workflowId={wf_id}")
                            # Return media_item for download URL extraction
                            return "completed", media_item

                        if status == "MEDIA_GENERATION_STATUS_FAILED":
                            reason = str(
                                media_status.get("failureReason")
                                or media_status.get("moderationResult")
                                or media_status.get("errorMessage")
                                or safety_filter
                                or "server returned FAILED"
                            )
                            is_moderation = self._is_moderation_failure(reason)
                            label = "Content blocked" if is_moderation else "Server error"
                            self._log(f"[{self.slot_id}] Video FAILED. {label}: {reason}")
                            if safety_filter and safety_filter not in reason:
                                self._log(f"[{self.slot_id}] Safety filter: {safety_filter}")
                            return ("moderation" if is_moderation else "failed"), reason

                        if poll_num % 3 == 0:
                            self._log(f"[{self.slot_id}] Video generating... (poll {poll_num})")

            except Exception as e:
                if poll_num % 3 == 0:
                    self._log(f"[{self.slot_id}] Poll {poll_num} error: {str(e)[:100]}")

        return "timeout", f"Video timed out after {max_polls * poll_interval}s"

    async def _request_video_upscale(self, access_token, project_id, media_id,
                                      workflow_id, resolution, aspect_ratio):
        """Submit video upscale request. Returns (new_media_id, error)."""
        UPSCALE_URL = "https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoUpsampleVideo"
        res_config = {
            "1080p": ("VIDEO_RESOLUTION_1080P", "veo_3_1_upsampler_1080p"),
            "4k": ("VIDEO_RESOLUTION_4K", "veo_3_1_upsampler_4k"),
        }
        res_enum, model_key = res_config.get(resolution, (None, None))
        if not res_enum:
            return None, f"Unsupported upscale resolution: {resolution}"

        # Get fresh token for upscale request
        bridge_result = await self._bridge.request_token(
            self.account_email, "VIDEO_GENERATION", timeout=60
        )
        if bridge_result.get("error"):
            return None, f"Bridge error: {bridge_result['error']}"
        token = bridge_result.get("token")
        access_token = bridge_result.get("access_token") or access_token

        batch_id = str(uuid.uuid4())
        seed = random.randint(100000, 999999)

        # Token left empty — extension EXECUTE_FETCH mints fresh at
        # dispatch time. Routes through Chrome native fetch for the
        # signed browser fingerprint headers Google's anti-abuse demands.
        client_context = {
            "projectId": project_id,
            "tool": "PINHOLE",
            "userPaygateTier": _paygate_tier_for_plan(get_setting("flow_account_plan", "ultra")),
            "sessionId": f";{int(time.time() * 1000)}",
            "recaptchaContext": {
                "token": "",  # extension fills this in
                "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
            },
        }

        body = {
            "mediaGenerationContext": {"batchId": batch_id},
            "clientContext": client_context,
            "requests": [{
                "resolution": res_enum,
                "aspectRatio": aspect_ratio,
                "seed": seed,
                "videoModelKey": model_key,
                "metadata": {"workflowId": workflow_id},
                "videoInput": {"mediaId": media_id},
            }],
            "useV2ModelConfig": True,
        }

        try:
            fetch_result = await self._bridge.request_api_fetch(
                account=self.account_email,
                url=UPSCALE_URL,
                method="POST",
                body=json.dumps(body),
                headers={
                    "content-type": "text/plain;charset=UTF-8",
                    "authorization": f"Bearer {access_token}",
                },
                recaptcha_action="VIDEO_GENERATION",
                inject_recaptcha_path="clientContext.recaptchaContext.token",
                timeout=60,
            )
            if fetch_result.get("error"):
                return None, f"Upscale bridge error: {fetch_result['error']}"
            status = fetch_result.get("status") or 0
            resp_text = fetch_result.get("body", "")
            if status < 200 or status >= 300:
                return None, f"Upscale API {status}: {resp_text[:200]}"
            data = json.loads(resp_text)

            media_list = data.get("media", []) if isinstance(data, dict) else []
            if media_list and isinstance(media_list[0], dict):
                new_id = media_list[0].get("name", "")
                if new_id:
                    return new_id, None
            return None, "Upscale response missing media id"
        except Exception as e:
            return None, f"Upscale error: {str(e)[:200]}"

    async def generate_image(self, prompt, model, ratio, references=None, ref_paths=None):
        """Generate image via direct API call (token from extension)."""
        self.is_busy = True
        try:
            api_model = _resolve_image_model(model)
            api_ratio = _resolve_image_ratio(ratio)
            seed = random.randint(100000, 999999)
            batch_id = str(uuid.uuid4())
            prompt_text = prompt if prompt.endswith("\n") else f"{prompt}\n"

            self._log(f"[{self.slot_id}] Image: {api_model}, {api_ratio}")

            # Get token + auth from extension via bridge
            bridge_result = await self._bridge.request_token(
                self.account_email, "IMAGE_GENERATION", timeout=60
            )

            if bridge_result.get("error"):
                return None, f"Bridge error: {bridge_result['error']}"

            token = bridge_result.get("token")
            access_token = bridge_result.get("access_token")
            project_id = bridge_result.get("project_id") or self._bridge.get_project_id(self.account_email)
            self.last_access_token = access_token  # cache for download

            if not access_token:
                return None, "No access token from extension"

            # If no project ID, resolve with lock (so only 1 worker creates per account)
            if not project_id:
                project_id = await self._resolve_project_id(access_token)

            if not project_id:
                return None, "No project ID available — open a project in labs.google/fx/tools/flow"

            # Upload reference images if file paths provided
            media_ids = list(references or [])
            if ref_paths:
                try:
                    uploaded = await self._upload_references(access_token, project_id, ref_paths)
                    media_ids.extend(uploaded)
                except Exception as e:
                    return None, f"Reference upload failed: {str(e)[:200]}"

            # Image gen — May 2026: Google tightened reCAPTCHA score
            # threshold on the image endpoint to match video's level.
            # The old "aiohttp direct + cached-pool token" route now
            # gets uniformly rejected with "Score Too Low" because:
            #   1. Cached pool tokens are minted by the background
            #      warmup loop with no real DOM interaction signal,
            #      so Google scores them at the floor.
            #   2. aiohttp can't reproduce Chrome's signed headers
            #      (x-browser-validation, x-client-data, sec-fetch-*),
            #      so the server-side validator treats the request
            #      as bot-origin even if the token itself is valid.
            # Switching to the same EXECUTE_FETCH route the video
            # endpoint uses fixes both: extension MAIN world mints
            # a fresh token at submit time (high score, mirrors what
            # labs.google's own UI does on Submit click) and Chrome
            # auto-attaches all the right headers because the fetch
            # originates from inside the labs.google tab.
            #
            # Token placeholder is required at both injection paths —
            # the extension's setAtPath skips paths whose parent
            # object doesn't exist, so we pre-create recaptchaContext
            # at both root and requests[0] for the inject to land.
            recaptcha_placeholder = {
                "token": "",
                "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
            }
            client_context_root = {
                "projectId": project_id,
                "tool": "PINHOLE",
                "sessionId": f";{int(time.time() * 1000)}",
                "recaptchaContext": dict(recaptcha_placeholder),
            }
            client_context_req = {
                "projectId": project_id,
                "tool": "PINHOLE",
                "sessionId": client_context_root["sessionId"],
                "recaptchaContext": dict(recaptcha_placeholder),
            }

            body = {
                "clientContext": client_context_root,
                "mediaGenerationContext": {"batchId": batch_id},
                "useNewMedia": True,
                "requests": [{
                    "clientContext": client_context_req,
                    "imageModelName": api_model,
                    "imageAspectRatio": api_ratio,
                    "structuredPrompt": {"parts": [{"text": prompt_text}]},
                    "seed": seed,
                    "imageInputs": (
                        [{"imageInputType": "IMAGE_INPUT_TYPE_REFERENCE", "name": ref_id}
                         for ref_id in media_ids]
                        if media_ids else []
                    ),
                }],
            }

            # We submit through EXECUTE_FETCH and, on a 429, fall through
            # to a single in-place retry against a fresh project. The
            # outer queue's retry/backoff still wraps this call, but
            # transparent project rotation avoids surfacing the user-
            # visible "429 strike" cascade for the well-understood
            # case of "this project's quota is just exhausted".
            # 3 attempts so we have room for one 429-burn rotation AND one
            # transient-bridge-error retry on top of the initial try without
            # running out of budget. Each branch below consumes one attempt.
            attempts_left = 3
            data = None
            err_msg = None
            while attempts_left > 0:
                attempts_left -= 1

                # client_context_root + _req both already reference the
                # current project_id from outer scope. On retry after a
                # burn we update project_id below and rebuild the body
                # once before re-issuing the POST.

                url = IMAGE_API_URL.format(project_id=project_id)
                fetch_result = await self._bridge.request_api_fetch(
                    account=self.account_email,
                    url=url,
                    method="POST",
                    body=json.dumps(body),
                    headers={
                        "content-type": "text/plain;charset=UTF-8",
                        "authorization": f"Bearer {access_token}",
                    },
                    recaptcha_action="IMAGE_GENERATION",
                    inject_recaptcha_path=(
                        "clientContext.recaptchaContext.token;"
                        "requests.0.clientContext.recaptchaContext.token"
                    ),
                    # 120s — restored from the bumped 180s value (d09d835).
                    # The 180s bump was meant to absorb Chrome's background-
                    # tab JS throttling, but on production logs it instead
                    # masked silent-throttle behavior: every request hung
                    # for the full 180s instead of failing fast at 120s,
                    # stretching the time before the worker could move on
                    # to the next job. User asked to put the original value
                    # back so failures surface sooner and the queue keeps
                    # moving.
                    timeout=120,
                )
                err = fetch_result.get("error") or ""
                if err:
                    # Classify the bridge-side failure:
                    #   - "fetch_failed: ..."           — native fetch() in MAIN world threw
                    #     before the request ever left the browser. Google did NOT receive
                    #     the request — safe to retry on a different tab.
                    #   - "execute_fetch_threw: Frame with ID 0 was removed."
                    #                                  — the labs.google tab was discarded
                    #     by Chrome (energy saver / memory) or crashed mid-execute. The
                    #     extension's findLabsTab() will pick a different tab on the
                    #     next attempt. Definitely pre-Google failure — safe to retry.
                    #   - "no_recaptcha_enterprise"    — tab's grecaptcha global is gone
                    #     (page navigated). Will be present again on the alt tab.
                    #   - "no_script_result"           — chrome.scripting.executeScript
                    #     returned an empty results array, which happens when the injected
                    #     MAIN-world async function never resolved (frame removed mid-
                    #     execute, page navigated before the fetch resolved). Same class
                    #     as the above — Google never received the request.
                    #
                    # Anything else (timeout, account_held, no_sitekey, etc.) is treated
                    # as "Google may have received the request" and surfaced to the outer
                    # queue's retry/dedup logic — DON'T retry transparently because that
                    # risks duplicate generation.
                    err_lower = err.lower()
                    safe_to_retry = (
                        "fetch_failed" in err_lower
                        or "execute_fetch_threw" in err_lower
                        or "frame with id" in err_lower
                        or "no_recaptcha_enterprise" in err_lower
                        or "no_script_result" in err_lower
                    )
                    if safe_to_retry and attempts_left > 0:
                        self._log(
                            f"[{self.slot_id}] Transient bridge error "
                            f"({err[:100]}) — retrying in 2s..."
                        )
                        await asyncio.sleep(2)
                        continue
                    return None, f"Bridge error: {err}"

                status = fetch_result.get("status") or 0
                resp_text = fetch_result.get("body", "")

                if status == 429 and attempts_left > 0:
                    # Per-project quota exhausted. Burn this project so
                    # the rest of the worker pool stops using it, resolve
                    # a fresh project (skipping burned ones, or creating
                    # a new one if all are cool-down), rebuild the body
                    # with the new projectId, and retry once.
                    self._log(
                        f"[{self.slot_id}] 429 on project {project_id} — "
                        "burning and rotating to a fresh project."
                    )
                    self._bridge.burn_project(self.account_email, project_id)
                    new_project_id = await self._resolve_project_id(access_token)
                    if not new_project_id or new_project_id == project_id:
                        # Couldn't rotate — surface the 429 to outer retry
                        err_msg = _parse_api_error(status, resp_text)
                        break
                    project_id = new_project_id
                    # Update both clientContext copies to point at the
                    # fresh project. The body dict is otherwise unchanged
                    # so the fresh reCAPTCHA token slot stays in place.
                    client_context_root["projectId"] = project_id
                    client_context_req["projectId"] = project_id
                    body["requests"][0]["clientContext"] = client_context_req
                    continue

                if status < 200 or status >= 300:
                    err_msg = _parse_api_error(status, resp_text)
                    break

                try:
                    data = json.loads(resp_text)
                    err_msg = None
                    break
                except json.JSONDecodeError:
                    err_msg = f"Invalid JSON response: {resp_text[:200]}"
                    break

            if err_msg:
                return None, err_msg
            if data is None:
                return None, "Image submission returned no data after rotation."

            self.jobs_completed += 1
            return data, None

        except Exception as e:
            return None, f"Exception: {str(e)[:300]}"
        finally:
            self.is_busy = False

    async def generate_video(
        self, prompt, model, ratio,
        video_sub_mode="text_to_video",
        ref_path=None, start_image_path=None, end_image_path=None,
        upscale=None,
    ):
        """Generate video via direct API call. Supports text-to-video, reference, and image-to-video."""
        self.is_busy = True
        try:
            # Determine sub-mode from explicit param or inferred from paths
            sub_mode = _normalize_video_sub_mode(
                video_sub_mode=video_sub_mode,
                ref_path=ref_path,
                start_image_path=start_image_path,
                end_image_path=end_image_path,
            )

            api_ratio = _resolve_video_ratio(ratio)
            plan = get_setting("flow_account_plan", "ultra")
            api_model = _resolve_video_model_for_sub_mode(sub_mode, model, model, ratio, plan=plan)
            endpoint = VIDEO_ENDPOINTS.get(sub_mode, VIDEO_API_URL)
            seed = random.randint(100000, 999999)
            batch_id = str(uuid.uuid4())

            self._log(f"[{self.slot_id}] Video: {sub_mode}, {api_model}, {api_ratio}")

            # Get token + auth from extension
            bridge_result = await self._bridge.request_token(
                self.account_email, "VIDEO_GENERATION", timeout=60
            )

            if bridge_result.get("error"):
                return None, f"Bridge error: {bridge_result['error']}"

            token = bridge_result.get("token")
            access_token = bridge_result.get("access_token")
            project_id = bridge_result.get("project_id") or self._bridge.get_project_id(self.account_email)
            self.last_access_token = access_token  # cache for download

            if not access_token:
                return None, "No access token from extension"

            # If no project ID, resolve with lock
            if not project_id:
                project_id = await self._resolve_project_id(access_token)

            if not project_id:
                return None, "📁 No project ID available — open a project in labs.google/fx/tools/flow"

            # Upload reference/start/end images if file paths provided
            ref_media_id = None
            start_media_id = None
            end_media_id = None

            try:
                if ref_path and sub_mode == "ingredients":
                    ref_media_id = await self._upload_and_cache_reference(
                        access_token, project_id, ref_path
                    )
                if start_image_path and sub_mode in ("frames_start", "frames_start_end"):
                    start_media_id = await self._upload_and_cache_reference(
                        access_token, project_id, start_image_path
                    )
                if end_image_path and sub_mode == "frames_start_end":
                    end_media_id = await self._upload_and_cache_reference(
                        access_token, project_id, end_image_path
                    )
            except Exception as e:
                return None, f"Image upload failed: {str(e)[:200]}"

            # Build request body. Token left empty — extension mints a fresh
            # one and writes it into the path below at dispatch time. Doing
            # this inside Chrome (not aiohttp) is what keeps the browser
            # fingerprint headers Google's anti-abuse demands.
            client_context = {
                "projectId": project_id,
                "tool": "PINHOLE",
                "userPaygateTier": _paygate_tier_for_plan(get_setting("flow_account_plan", "ultra")),
                "sessionId": f";{int(time.time() * 1000)}",
                "recaptchaContext": {
                    "token": "",  # extension fills this in
                    "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                },
            }

            request_obj = {
                "aspectRatio": api_ratio,
                "seed": seed,
                "textInput": {
                    "structuredPrompt": {"parts": [{"text": prompt}]},
                },
                "videoModelKey": api_model,
                "metadata": {},
            }

            # Add reference/start/end image fields based on sub-mode
            if sub_mode == "ingredients" and ref_media_id:
                request_obj["referenceImages"] = [{
                    "mediaId": ref_media_id,
                    "imageUsageType": "IMAGE_USAGE_TYPE_ASSET",
                }]
            if sub_mode in ("frames_start", "frames_start_end") and start_media_id:
                request_obj["startImage"] = {
                    "mediaId": start_media_id,
                    "cropCoordinates": {"top": 0, "left": 0, "bottom": 1, "right": 1},
                }
            if sub_mode == "frames_start_end" and end_media_id:
                request_obj["endImage"] = {
                    "mediaId": end_media_id,
                    "cropCoordinates": {"top": 0, "left": 0, "bottom": 1, "right": 1},
                }

            # Loud log showing EXACTLY what's about to be submitted — one
            # line per POST so we can correlate tool output to Flow
            # dashboard 1:1. If user sees N videos in dashboard but only
            # N/15 of these lines in the log, that's server-side (pre-
            # existing Google queue) rather than tool-side duplication.
            self._log(
                f"[{self.slot_id}] 📤 POST #1 to video API — "
                f"batchId={batch_id[:8]}… seed={seed} "
                f"model={api_model} ref={ref_media_id or start_media_id or end_media_id or '(none)'} "
                f"prompt={prompt[:40]!r}"
            )

            body = {
                "mediaGenerationContext": {
                    "batchId": batch_id,
                    # Real labs.google requests carry this — Google's anti-
                    # abuse uses request shape as a fingerprint, so we match.
                    "audioFailurePreference": "BLOCK_SILENCED_VIDEOS",
                },
                "clientContext": client_context,
                "requests": [request_obj],
                "useV2ModelConfig": True,
            }

            # Route through extension's native fetch — Chrome auto-adds the
            # signed browser headers (x-browser-validation, x-client-data,
            # sec-fetch-*) that Google's anti-abuse demands. aiohttp can't
            # produce these, which is why a perfect reCAPTCHA token still
            # got rejected with PUBLIC_ERROR_UNUSUAL_ACTIVITY.
            fetch_result = await self._bridge.request_api_fetch(
                account=self.account_email,
                url=endpoint,
                method="POST",
                body=json.dumps(body),
                headers={
                    "content-type": "text/plain;charset=UTF-8",
                    "authorization": f"Bearer {access_token}",
                },
                recaptcha_action="VIDEO_GENERATION",
                inject_recaptcha_path="clientContext.recaptchaContext.token",
                timeout=180,
            )

            if fetch_result.get("error"):
                err = fetch_result["error"]
                self._log(f"[{self.slot_id}] Video API bridge error: {err}")
                return None, f"Bridge error: {err}"

            status = fetch_result.get("status") or 0
            resp_text = fetch_result.get("body", "")
            if status < 200 or status >= 300:
                self._log(f"[{self.slot_id}] Video API {status}: {resp_text[:300]}")
                return None, _parse_api_error(status, resp_text)

            try:
                data = json.loads(resp_text)
            except json.JSONDecodeError:
                return None, f"Invalid JSON: {resp_text[:200]}"

            # Extract media_id for polling
            media_list = data.get("media", []) if isinstance(data, dict) else []
            workflows = data.get("workflows", []) if isinstance(data, dict) else []
            operations = data.get("operations", []) if isinstance(data, dict) else []

            media_id = ""
            if media_list and isinstance(media_list, list):
                media_id = media_list[0].get("name", "") if isinstance(media_list[0], dict) else ""
            if not media_id and operations and isinstance(operations, list):
                op = operations[0] if isinstance(operations[0], dict) else {}
                media_id = op.get("operation", {}).get("name", "")
            if not media_id and workflows and isinstance(workflows, list):
                media_id = workflows[0].get("metadata", {}).get("primaryMediaId", "") if isinstance(workflows[0], dict) else ""

            if not media_id:
                # No media_id means immediate response (unlikely for video) or error
                self.jobs_completed += 1
                return data, None

            # Poll until base video is complete (720p)
            upscale_target = str(upscale or "none").strip().lower()
            if upscale_target in ("", "none"):
                upscale_target = "none"
            self._log(f"[{self.slot_id}] Video submitted, polling: {media_id[:30]}...")
            poll_status, poll_data = await self._poll_video_status(
                access_token, media_id, project_id,
                poll_interval=5, max_polls=60,
            )

            if poll_status == "completed":
                # poll_data is the media_item dict on success
                final_name = media_id
                workflow_id = ""
                if isinstance(poll_data, dict):
                    final_name = poll_data.get("name", media_id) or media_id
                    workflow_id = poll_data.get("workflowId", "")
                    data["_poll_media_item"] = poll_data

                self._log(f"[{self.slot_id}] Base video ready (720p)")

                # ── Upscale if requested (1080p or 4K) ──
                if upscale_target in ("1080p", "4k") and project_id:
                    self._log(f"[{self.slot_id}] Upscaling to {upscale_target}...")
                    up_media_id, up_error = await self._request_video_upscale(
                        access_token, project_id, final_name,
                        workflow_id, upscale_target, api_ratio,
                    )
                    if up_error:
                        self._log(f"[{self.slot_id}] Upscale request failed: {up_error[:120]}")
                        # Fall back to 720p — still usable
                    elif up_media_id:
                        # Poll upscaled video
                        up_max = 120 if upscale_target == "4k" else 60
                        up_status, up_data = await self._poll_video_status(
                            access_token, up_media_id, project_id,
                            poll_interval=5, max_polls=up_max,
                        )
                        if up_status == "completed":
                            final_name = up_media_id
                            if isinstance(up_data, dict):
                                workflow_id = up_data.get("workflowId", workflow_id)
                            self._log(f"[{self.slot_id}] Upscale complete ({upscale_target})")
                        else:
                            self._log(f"[{self.slot_id}] Upscale poll {up_status} — using 720p fallback")

                # Finalize workflow — PATCH primaryMediaId (required before download URL works)
                if workflow_id and project_id:
                    try:
                        patch_url = f"https://aisandbox-pa.googleapis.com/v1/flowWorkflows/{workflow_id}"
                        patch_body = {
                            "workflow": {
                                "name": workflow_id,
                                "projectId": project_id,
                                "metadata": {"primaryMediaId": final_name},
                            },
                            "updateMask": "metadata.primaryMediaId",
                        }
                        async with _make_aiohttp_session() as s:
                            async with s.patch(
                                patch_url,
                                headers={
                                    "content-type": "text/plain;charset=UTF-8",
                                    "authorization": f"Bearer {access_token}",
                                    "origin": "https://labs.google",
                                    "referer": "https://labs.google/",
                                },
                                data=json.dumps(patch_body),
                                timeout=aiohttp.ClientTimeout(total=15),
                            ) as patch_resp:
                                if patch_resp.ok:
                                    self._log(f"[{self.slot_id}] Workflow finalized")
                                else:
                                    self._log(f"[{self.slot_id}] Workflow PATCH {patch_resp.status} (non-fatal)")
                    except Exception as e:
                        self._log(f"[{self.slot_id}] Workflow PATCH error (non-fatal): {str(e)[:80]}")

                    # Brief pause after finalize — let redirect service register
                    await asyncio.sleep(2)

                data["_video_media_id"] = final_name
                self.jobs_completed += 1
                return data, None
            elif poll_status == "moderation":
                return None, f"MODERATION: {poll_data or 'content blocked'}"
            else:
                return None, f"Video {poll_status}: {poll_data or 'unknown'}"

        except Exception as e:
            return None, f"Exception: {str(e)[:300]}"
        finally:
            self.is_busy = False

    async def _resolve_project_id(self, access_token) -> Optional[str]:
        """Resolve project ID with per-account lock — only one worker fetches at a time."""
        # Check cache again (another worker may have resolved it while we waited)
        cached = self._bridge.get_project_id(self.account_email)
        if cached:
            return cached

        # Get or create lock for this account
        if self.account_email not in ExtensionWorker._project_locks:
            ExtensionWorker._project_locks[self.account_email] = asyncio.Lock()

        lock = ExtensionWorker._project_locks[self.account_email]

        async with lock:
            # Double-check cache after acquiring lock
            cached = self._bridge.get_project_id(self.account_email)
            if cached:
                return cached

            self._log(f"[{self.slot_id}] Resolving project ID for {self.account_email}...")
            project_id = await self._create_project(access_token)
            if project_id:
                self._bridge.set_project_id(self.account_email, project_id)
                self._log(f"[{self.slot_id}] ✓ Project ID resolved: {project_id}")
            else:
                self._log(f"[{self.slot_id}] ✗ Could not resolve project ID")
            return project_id

    async def _create_project(self, access_token):
        """Get or create a Labs project via multiple API fallbacks.

        IMPORTANT (May 2026): Flow's rate limit is per-PROJECT, not
        per-account. Once a project burns through its quota, every
        subsequent submission on it returns 429 — but the SAME account
        with a freshly created project starts working immediately.
        Users were doing this dance manually (close app → new project
        in UI → restart → another 30-100 images), so we automate it
        here: each candidate from the projects list is checked against
        bridge.is_project_burned(), and if all known projects are
        cooling down the extension is asked to create a fresh one.
        """
        import re as _re

        def _extract_pid(raw):
            if not raw:
                return ""
            m = _re.search(r"([a-z0-9-]{16,})", str(raw), _re.IGNORECASE)
            return m.group(1) if m else ""

        def _pick_unburned(pids):
            for pid in pids:
                if not pid:
                    continue
                if not self._bridge.is_project_burned(self.account_email, pid):
                    return pid
            return ""

        headers = {"authorization": f"Bearer {access_token}"}

        # ── Method 1: List projects via aisandbox API ──
        try:
            async with _make_aiohttp_session() as session:
                async with session.get(
                    "https://aisandbox-pa.googleapis.com/v1/projects",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.ok:
                        data = await resp.json()
                        projects = data.get("projects", data.get("project", []))
                        if isinstance(projects, list) and projects:
                            candidates = [
                                _extract_pid(p.get("name", "") or p.get("projectId", ""))
                                for p in projects
                            ]
                            pid = _pick_unburned(candidates)
                            if pid:
                                self._log(
                                    f"[{self.slot_id}] Project from API: {pid} "
                                    f"(skipped {sum(1 for c in candidates if c and c != pid and self._bridge.is_project_burned(self.account_email, c))} burned)"
                                )
                                return pid
                            if candidates:
                                self._log(
                                    f"[{self.slot_id}] All {len([c for c in candidates if c])} listed project(s) "
                                    "are in 429 cooldown — creating fresh project."
                                )
        except Exception as e:
            self._log(f"[{self.slot_id}] Projects API error: {str(e)[:80]}")

        # ── Method 2: listFlows via trpc ──
        try:
            async with _make_aiohttp_session() as session:
                async with session.get(
                    "https://labs.google/fx/api/trpc/backbone.listFlows",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.ok:
                        data = await resp.json()
                        flows = data.get("result", {}).get("data", {}).get("flows", [])
                        if flows:
                            candidates = [str(f.get("name", "")) for f in flows]
                            pid = _pick_unburned(candidates)
                            if pid:
                                self._log(f"[{self.slot_id}] Project from listFlows: {pid}")
                                return pid
        except Exception:
            pass

        # ── Method 3: Ask extension to click "New project" ──
        # Clear bridge's cached project_id so the post-back from extension
        # overwrites the stale (potentially burned) value cleanly.
        # Extension flow now navigates tab → dashboard, waits for hydration,
        # clicks, then waits for URL transition. Worst-case timing:
        #   ~1s extension poll wait + ~15s tab navigation + ~1.5s settle +
        #   up to 8s button-find poll + up to 8s URL transition wait
        #   = ~33s ceiling. We wait 20s and let the extension finish
        #   asynchronously; if the post-back lands later, the next
        #   _resolve_project_id pass will pick it up.
        pre_existing = self._bridge.get_project_id(self.account_email) or ""
        self._bridge.send_command("new_project", self.account_email)
        for _ in range(20):
            await asyncio.sleep(1)
            current = self._bridge.get_project_id(self.account_email) or ""
            if current and current != pre_existing:
                return current
        return self._bridge.get_project_id(self.account_email)


class ExtensionModeManager:
    """
    Manages Chrome Extension mode — same pattern as HttpModeManager.
    Receives AsyncQueueManager instance for signals, settings, etc.
    """

    def __init__(self, queue_manager):
        self.qm = queue_manager
        self._log = lambda msg: queue_manager.signals.log_msg.emit(msg)
        self._bridge = ExtensionBridge(self._log)
        self._workers: Dict[str, list] = {}  # account_email -> [ExtensionWorker, ...]
        self._active_tasks = []
        # Per-account dispatch stagger — tracks when we last fired a job
        # to each account so we can enforce a 0.8-1.5s minimum gap
        # between same-account dispatches. Prevents the "8 requests in
        # 1 second" burst pattern that Google rate-limits even when
        # every individual request is valid.
        self._last_dispatch_ts: Dict[str, float] = {}  # account -> last dispatch timestamp
        # reCAPTCHA streak tracking — auto-hold after consecutive failures
        self._recaptcha_streak: Dict[str, int] = {}  # account -> consecutive recaptcha failures
        self.RECAPTCHA_HOLD_THRESHOLD = 3  # hold account after this many consecutive failures

        # reCAPTCHA AUTO-RETRY cooldown — for users with rotating-IP VPNs
        # (Surfshark, NordVPN, etc.). When Google flags an account, the flag
        # is usually scoped to the current IP — once the VPN rotates to a
        # fresh IP, the next reCAPTCHA call typically scores fine again.
        # Old behavior was to PERMANENTLY disable the account on 3 strikes,
        # which wasted the natural recovery from VPN rotation. New flow:
        # set a short cooldown, auto-clear when it expires, retry. Track
        # consecutive cooldown cycles so a genuinely flagged account
        # (no VPN rotation helping) eventually falls back to the old
        # permanent-hold behavior instead of looping forever.
        self._recaptcha_cooldown_until: Dict[str, float] = {}  # account -> unix ts
        self._recaptcha_cooldown_cycles: Dict[str, int] = {}   # account -> consecutive cycles
        self.RECAPTCHA_COOLDOWN_SECONDS = 60        # 1-min cooldown between retries
        self.RECAPTCHA_MAX_COOLDOWN_CYCLES = 30     # ~30 min of retries before giving up

        # ─── Auto tracking cleanup — keeps reCAPTCHA score healthy ───
        self._account_gen_count: Dict[str, int] = {}   # account -> generations since last cleanup
        self._account_last_cleanup: Dict[str, float] = {}  # account -> timestamp of last cleanup
        self.CLEANUP_EVERY_N_GENS = 150      # clean tracking data every N generations per account
        self.CLEANUP_MIN_INTERVAL = 259200   # minimum 3 days (seconds) between cleanups

    async def run(self):
        """Main entry — start bridge, wait for extension, dispatch jobs."""
        if aiohttp is None:
            self._log("[ExtMode] ERROR: aiohttp not installed. Run: pip install aiohttp")
            return

        # Start bridge server
        await self._bridge.start()

        # Restore ecosystem toggle state from DB (persists across app restarts)
        try:
            saved = get_bool_setting("ecosystem_enabled", False)
            self._bridge.set_ecosystem_enabled(bool(saved))
            if saved:
                self._log("[ExtMode] Auto Warmup Mode restored: ENABLED")
        except Exception:
            pass

        try:
            slots_per_account = max(1, min(40, get_int_setting("slots_per_account", 5)))

            self._log(
                "[ExtMode] Chrome Extension mode — waiting for extension to connect...\n"
                "  Make sure Chrome is open with G-Labs Helper extension\n"
                "  and labs.google.com tabs are logged in."
            )

            # Wait for extension to connect (max 60 seconds)
            wait_start = time.time()
            while not self._bridge.is_extension_connected:
                if self.qm.stop_requested or self.qm.force_stop_requested:
                    return
                if time.time() - wait_start > 60:
                    self._log("[ExtMode] Extension not connected after 60s. Aborting.")
                    return
                await asyncio.sleep(1)

            self._log("[ExtMode] Extension connected!")

            # Wait for all account reports to arrive.
            # Each Chrome profile reports separately — wait until count stabilizes.
            await asyncio.sleep(4)
            connected = self._bridge.get_connected_accounts()
            prev_count = len(connected)

            # Wait up to 20s for more accounts to trickle in
            stable_rounds = 0
            for _ in range(20):
                if self.qm.stop_requested or self.qm.force_stop_requested:
                    return
                await asyncio.sleep(1)
                connected = self._bridge.get_connected_accounts()
                if len(connected) == prev_count and prev_count > 0:
                    stable_rounds += 1
                    if stable_rounds >= 4:
                        break  # count stable for 4s — all profiles reported
                else:
                    stable_rounds = 0
                    prev_count = len(connected)

            if not connected:
                self._log(
                    "[ExtMode] No accounts detected by extension.\n"
                    "  Open labs.google/fx/tools/flow in Chrome and login with Google account."
                )
                # Keep waiting for accounts (max 60 more seconds)
                wait_start = time.time()
                while not connected:
                    if self.qm.stop_requested or self.qm.force_stop_requested:
                        return
                    if time.time() - wait_start > 60:
                        self._log("[ExtMode] No accounts found. Aborting.")
                        return
                    await asyncio.sleep(3)
                    connected = self._bridge.get_connected_accounts()

            self._log(
                f"[ExtMode] Found {len(connected)} account(s) via extension: "
                + ", ".join(connected.keys())
            )

            # Create workers for each extension-detected account
            for email, info in connected.items():
                account_name = email or info.get("name", "unknown")

                workers = []
                for idx in range(1, slots_per_account + 1):
                    slot_id = f"{account_name}#e{idx}"
                    worker = ExtensionWorker(slot_id, account_name, self._bridge, self._log)
                    workers.append(worker)

                self._workers[account_name] = workers
                self._log(f"[ExtMode] {account_name}: {len(workers)} worker(s) ready.")

            total_workers = sum(len(w) for w in self._workers.values())
            if total_workers == 0:
                self._log("[ExtMode] No workers started. Ensure accounts are logged in via Chrome Extension.")
                return

            self._log(
                f"[ExtMode] Total: {total_workers} worker(s). "
                f"RAM: ~50MB (no browser launched). "
            )

            # Main dispatch loop (same pattern as HttpModeManager)
            while self.qm.is_running:
                if self.qm.stop_requested or self.qm.force_stop_requested:
                    break
                if self.qm.pause_requested:
                    await asyncio.sleep(1)
                    continue

                # ─── Dynamic account discovery ───
                # Check for new accounts that connected after initial worker creation
                current_accounts = self._bridge.get_connected_accounts()
                for email, info in current_accounts.items():
                    if email and email not in self._workers:
                        account_name = email or info.get("name", "unknown")
                        workers = []
                        for idx in range(1, slots_per_account + 1):
                            slot_id = f"{account_name}#e{idx}"
                            worker = ExtensionWorker(slot_id, account_name, self._bridge, self._log)
                            workers.append(worker)
                        self._workers[account_name] = workers
                        self._log(
                            f"[ExtMode] New account detected: {account_name} — "
                            f"{len(workers)} worker(s) added dynamically."
                        )

                self._active_tasks = [t for t in self._active_tasks if not t.done()]

                # Signal ecosystem: generation is running iff there are active tasks
                # Extension will pause background activity during generation.
                self._bridge.set_generation_running(len(self._active_tasks) > 0)

                jobs = get_all_jobs()
                pending = [j for j in jobs if j["status"] == "pending"]

                if not pending:
                    if not self._active_tasks:
                        still_active = any(
                            j["status"] in ("pending", "running") for j in get_all_jobs()
                        )
                        if not still_active:
                            self._log("[ExtMode] All jobs completed.")
                            break
                    await asyncio.sleep(self.qm.scheduler_poll_seconds)
                    continue

                busy_slots = {t.get_name() for t in self._active_tasks if hasattr(t, "get_name")}

                dispatched = 0
                for job in pending:
                    if self.qm.stop_requested or self.qm.force_stop_requested:
                        break

                    worker = self._get_available_worker(busy_slots)
                    if not worker:
                        break

                    # Per-account dispatch stagger — Google 429s a same-IP
                    # burst of 8-16 requests landing inside a 1-second
                    # window even when each request is individually clean
                    # (valid token + signed Chrome headers). Manual users
                    # can't click Generate 8 times per second; rate limit
                    # kicks in on the pattern alone. We enforce a 2-3s
                    # minimum gap between dispatches to the SAME account
                    # so the request stream stays well inside Google's
                    # ~40-60 req/min/account ceiling. Cross-account
                    # dispatches still fire in parallel — their sessions
                    # are independent.
                    last_ts = self._last_dispatch_ts.get(worker.account_email, 0.0)
                    elapsed = time.time() - last_ts
                    min_gap = 2.0 + random.random() * 1.0  # 2.0–3.0s jittered
                    if last_ts and elapsed < min_gap:
                        await asyncio.sleep(min_gap - elapsed)
                    self._last_dispatch_ts[worker.account_email] = time.time()

                    job_id = job["id"]
                    update_job_status(job_id, "running", account=worker.account_email)
                    self.qm.signals.job_updated.emit(job_id, "running", worker.account_email, "")

                    task = asyncio.create_task(
                        self._run_job(worker, job), name=worker.slot_id,
                    )
                    self._active_tasks.append(task)
                    busy_slots.add(worker.slot_id)
                    dispatched += 1

                    # Stagger between dispatches. Video jobs get a longer
                    # gap because labs.google.com's video endpoint (Veo)
                    # is much more abuse-sensitive than the image one —
                    # a burst of 5 video requests in <2s reliably trips
                    # PUBLIC_ERROR_UNUSUAL_ACTIVITY even on warm accounts.
                    # Image jobs keep the user's configured fast stagger.
                    if str(job.get("job_type") or "image").lower() == "video":
                        stagger = random.uniform(3.0, 5.0)
                    else:
                        stagger = random.uniform(
                            self.qm.global_stagger_min_seconds,
                            self.qm.global_stagger_max_seconds,
                        )
                    if stagger > 0:
                        await asyncio.sleep(stagger)

                if dispatched == 0:
                    await asyncio.sleep(self.qm.scheduler_poll_seconds)

            # Handle remaining tasks
            if self._active_tasks:
                if self.qm.stop_requested or self.qm.force_stop_requested:
                    self._log(f"[ExtMode] Cancelling {len(self._active_tasks)} active job(s)...")
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
                        self._log("[ExtMode] Some tasks didn't cancel in 3s — continuing.")
                else:
                    self._log(f"[ExtMode] Waiting for {len(self._active_tasks)} active job(s)...")
                    await asyncio.gather(*self._active_tasks, return_exceptions=True)

        finally:
            await self._bridge.stop()
            self._workers.clear()
            self._log("[ExtMode] Extension mode stopped.")

    def _get_available_worker(self, busy_slots):
        """Find an available worker across all accounts, respecting 429 throttle."""
        import time as _time
        now = _time.time()
        for account_email, workers in self._workers.items():
            # Check if account is 429-throttled — limit concurrent slots
            throttle_max = self.qm.account_throttle_max_slots.get(account_email)
            if throttle_max is not None and self.qm.account_throttle_until.get(account_email, 0) > now:
                busy_count = sum(1 for w in workers if w.slot_id in busy_slots or w.is_busy)
                if busy_count >= throttle_max:
                    continue  # This account is at its throttled limit

            # Hard 429 pause — Google flagged this account with
            # PUBLIC_ERROR_UNUSUAL_ACTIVITY_TOO_MUCH_TRAFFIC. Block ALL
            # workers (not just reduce slots) until cooldown expires.
            # Without this, the surviving slot keeps hammering and Google
            # extends the lock.
            if self.qm.is_account_429_paused(account_email):
                continue

            # Check account disabled (hard hold for auth errors etc.)
            # If account was reCAPTCHA-held AND user force-enabled it, bridge
            # returns is_account_held=False — allow dispatch despite qm flag.
            #
            # NEW (May 2026): if a reCAPTCHA cooldown was set and has now
            # expired, clear the disable flag + reset streak so this
            # dispatch goes through. Designed for VPN rotating-IP users
            # where the flag was IP-scoped and the new IP usually scores
            # fine. If the retry fails again, the 3-strike path below
            # will re-arm cooldown for another cycle (up to MAX_CYCLES).
            if self.qm.account_disabled.get(account_email):
                cooldown_until = self._recaptcha_cooldown_until.get(account_email, 0.0)
                if cooldown_until and time.time() >= cooldown_until:
                    cycles = self._recaptcha_cooldown_cycles.get(account_email, 0)
                    self._log(
                        f"[ExtMode] {account_email}: reCAPTCHA cooldown expired "
                        f"(cycle {cycles}/{self.RECAPTCHA_MAX_COOLDOWN_CYCLES}) — "
                        f"auto-retrying. If your VPN rotated IP, next call should succeed."
                    )
                    self._recaptcha_cooldown_until.pop(account_email, None)
                    self.qm.account_disabled[account_email] = False
                    self._recaptcha_streak[account_email] = 0
                    # Fall through to normal dispatch below.
                else:
                    try:
                        if not self._bridge.is_account_held(account_email):
                            # Check if this is a force-enable override
                            hold_info = self._bridge.get_hold_info(account_email)
                            if hold_info.get("force_enabled"):
                                pass  # user allowed it — fall through
                            else:
                                continue
                        else:
                            continue
                    except Exception:
                        continue
                    continue

            for worker in workers:
                if worker.slot_id not in busy_slots and not worker.is_busy:
                    return worker
        return None

    def _check_auto_cleanup(self, account_email: str):
        """Auto-clean tracking data (Service Workers, IndexedDB, _GRECAPTCHA cookie)
        every N generations or every 3 days — whichever comes first.
        Keeps reCAPTCHA score healthy by removing accumulated bot fingerprints."""
        now = time.time()
        count = self._account_gen_count.get(account_email, 0) + 1
        self._account_gen_count[account_email] = count
        last_cleanup = self._account_last_cleanup.get(account_email, now)

        # Initialize last_cleanup on first call
        if account_email not in self._account_last_cleanup:
            self._account_last_cleanup[account_email] = now
            return

        time_since = now - last_cleanup
        needs_cleanup = (
            count >= self.CLEANUP_EVERY_N_GENS
            or time_since >= self.CLEANUP_MIN_INTERVAL
        )

        if needs_cleanup:
            self._log(
                f"[ExtMode] Auto-cleanup for {account_email}: "
                f"{count} generations, {time_since / 3600:.1f}h since last cleanup. "
                f"Cleaning Service Workers + IndexedDB + _GRECAPTCHA cookie..."
            )
            # Send cleanup commands to extension
            self._bridge.send_command("clean_tracking", account_email)
            self._bridge.send_command("clean_recaptcha_cookie", account_email)
            # Reset counters
            self._account_gen_count[account_email] = 0
            self._account_last_cleanup[account_email] = now

    async def _run_pipeline_job(self, worker: ExtensionWorker, job: dict):
        """Execute a pipeline job: Step 1 = generate image, Step 2 = generate video from it."""
        job_id = job["id"]
        prompt = job.get("prompt", "")
        model = job.get("model", "")
        queue_no = job.get("output_index") or job.get("queue_no")
        video_prompt = str(job.get("video_prompt") or "animate").strip() or "animate"
        video_model = str(job.get("video_model") or "").strip()
        video_sub_mode = str(job.get("video_sub_mode") or "ingredients").strip()
        video_ratio = str(job.get("video_ratio") or job.get("aspect_ratio") or "").strip()
        ratio = job.get("aspect_ratio", "IMAGE_ASPECT_RATIO_LANDSCAPE")

        # Parse ref_paths for image step
        ref_paths = []
        ref_paths_raw = job.get("ref_paths") or ""
        if isinstance(ref_paths_raw, str) and ref_paths_raw.strip():
            try:
                parsed = json.loads(ref_paths_raw)
                if isinstance(parsed, list):
                    ref_paths = [str(p).strip() for p in parsed if str(p).strip()]
            except (json.JSONDecodeError, TypeError):
                ref_paths = [ref_paths_raw.strip()]
        single_ref = str(job.get("ref_path") or "").strip()
        if single_ref and single_ref not in ref_paths:
            ref_paths.insert(0, single_ref)

        sub_mode = _normalize_video_sub_mode(video_sub_mode=video_sub_mode)
        if sub_mode not in ("ingredients", "frames_start"):
            sub_mode = "ingredients"

        self._log(f"[{worker.slot_id}] Pipeline: image({model}) -> video({sub_mode})")

        try:
            # ── Step 1: Generate image ──
            self._log(f"[{worker.slot_id}] Pipeline Step 1: Generating image...")
            img_result, img_error = await worker.generate_image(
                prompt, model, ratio,
                ref_paths=ref_paths if ref_paths else None,
            )

            if img_error or not img_result:
                update_job_status(job_id, "failed", account=worker.account_email, error=img_error or "Image generation failed")
                self.qm.signals.job_updated.emit(job_id, "failed", worker.account_email, img_error or "Image generation failed")
                self._log(f"[{worker.slot_id}] Pipeline Step 1 FAILED: {(img_error or '')[:200]}")
                return

            # Extract media ID from generated image
            media_list = img_result.get("media", []) if isinstance(img_result, dict) else []
            generated_media_id = ""
            for item in media_list:
                if isinstance(item, dict):
                    generated_media_id = item.get("name", "")
                    if generated_media_id:
                        break

            if not generated_media_id:
                workflows = img_result.get("workflows", []) if isinstance(img_result, dict) else []
                if workflows and isinstance(workflows[0], dict):
                    generated_media_id = workflows[0].get("metadata", {}).get("primaryMediaId", "")

            if not generated_media_id:
                update_job_status(job_id, "failed", account=worker.account_email, error="Pipeline: no media ID from image generation")
                self.qm.signals.job_updated.emit(job_id, "failed", worker.account_email, "Pipeline: no media ID from image generation")
                return

            self._log(f"[{worker.slot_id}] Step 1 done: {generated_media_id[:20]}...")

            # ── Step 2: Generate video from image ──
            self._log(f"[{worker.slot_id}] Pipeline Step 2: Generating video ({sub_mode})...")

            # Use the generated image media ID as reference or start image
            # We pass it directly as a media ID (not file path) — need to build generate_video call manually
            api_ratio = _resolve_video_ratio(video_ratio)
            plan = get_setting("flow_account_plan", "ultra")
            api_model = _resolve_video_model_for_sub_mode(sub_mode, video_model, video_model, video_ratio, plan=plan)
            endpoint = VIDEO_ENDPOINTS.get(sub_mode, VIDEO_API_URL)
            seed = random.randint(100000, 999999)
            batch_id = str(uuid.uuid4())

            # Get fresh token for video step
            bridge_result = await worker._bridge.request_token(
                worker.account_email, "VIDEO_GENERATION", timeout=60
            )
            if bridge_result.get("error"):
                update_job_status(job_id, "failed", account=worker.account_email, error=f"Pipeline Step 2 bridge error: {bridge_result['error']}")
                self.qm.signals.job_updated.emit(job_id, "failed", worker.account_email, f"Pipeline Step 2 bridge error: {bridge_result['error']}")
                return

            token = bridge_result.get("token")
            access_token = bridge_result.get("access_token")
            project_id = bridge_result.get("project_id") or worker._bridge.get_project_id(worker.account_email)

            if not access_token:
                update_job_status(job_id, "failed", account=worker.account_email, error="Pipeline Step 2: no access token")
                self.qm.signals.job_updated.emit(job_id, "failed", worker.account_email, "Pipeline Step 2: no access token")
                return

            if not project_id:
                project_id = await worker._resolve_project_id(access_token)

            # Token left empty — extension EXECUTE_FETCH mints fresh at
            # dispatch time with 3-call pre-warmup. Routes through Chrome
            # native fetch for x-browser-validation / x-client-data headers.
            client_context = {
                "projectId": project_id or "",
                "tool": "PINHOLE",
                "userPaygateTier": _paygate_tier_for_plan(get_setting("flow_account_plan", "ultra")),
                "sessionId": f";{int(time.time() * 1000)}",
                "recaptchaContext": {
                    "token": "",  # extension fills this in
                    "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                },
            }

            request_obj = {
                "aspectRatio": api_ratio,
                "seed": seed,
                "textInput": {
                    "structuredPrompt": {"parts": [{"text": video_prompt}]},
                },
                "videoModelKey": api_model,
                "metadata": {},
            }

            if sub_mode == "ingredients":
                request_obj["referenceImages"] = [{
                    "mediaId": generated_media_id,
                    "imageUsageType": "IMAGE_USAGE_TYPE_ASSET",
                }]
            elif sub_mode == "frames_start":
                request_obj["startImage"] = {
                    "mediaId": generated_media_id,
                    "cropCoordinates": {"top": 0, "left": 0, "bottom": 1, "right": 1},
                }

            body = {
                "mediaGenerationContext": {
                    "batchId": batch_id,
                    "audioFailurePreference": "BLOCK_SILENCED_VIDEOS",
                },
                "clientContext": client_context,
                "requests": [request_obj],
                "useV2ModelConfig": True,
            }

            fetch_result = await worker._bridge.request_api_fetch(
                account=worker.account_email,
                url=endpoint,
                method="POST",
                body=json.dumps(body),
                headers={
                    "content-type": "text/plain;charset=UTF-8",
                    "authorization": f"Bearer {access_token}",
                },
                recaptcha_action="VIDEO_GENERATION",
                inject_recaptcha_path="clientContext.recaptchaContext.token",
                timeout=180,
            )
            if fetch_result.get("error"):
                err = f"Bridge error: {fetch_result['error']}"
                update_job_status(job_id, "failed", account=worker.account_email, error=f"Pipeline Step 2: {err}")
                self.qm.signals.job_updated.emit(job_id, "failed", worker.account_email, f"Pipeline Step 2: {err}")
                return
            status = fetch_result.get("status") or 0
            resp_text = fetch_result.get("body", "")
            if status < 200 or status >= 300:
                err = _parse_api_error(status, resp_text)
                update_job_status(job_id, "failed", account=worker.account_email, error=f"Pipeline Step 2: {err}")
                self.qm.signals.job_updated.emit(job_id, "failed", worker.account_email, f"Pipeline Step 2: {err}")
                return
            data = json.loads(resp_text)

            # Extract video media_id
            vid_media_list = data.get("media", []) if isinstance(data, dict) else []
            vid_media_id = ""
            if vid_media_list and isinstance(vid_media_list, list) and isinstance(vid_media_list[0], dict):
                vid_media_id = vid_media_list[0].get("name", "")
            if not vid_media_id:
                vid_workflows = data.get("workflows", []) if isinstance(data, dict) else []
                if vid_workflows and isinstance(vid_workflows[0], dict):
                    vid_media_id = vid_workflows[0].get("metadata", {}).get("primaryMediaId", "")

            if not vid_media_id:
                update_job_status(job_id, "failed", account=worker.account_email, error="Pipeline Step 2: no video media ID")
                self.qm.signals.job_updated.emit(job_id, "failed", worker.account_email, "Pipeline Step 2: no video media ID")
                return

            # Poll video
            self._log(f"[{worker.slot_id}] Video submitted, polling: {vid_media_id[:20]}...")
            poll_status, poll_data = await worker._poll_video_status(
                access_token, vid_media_id, project_id,
                poll_interval=5, max_polls=60,
            )

            if poll_status != "completed":
                err = f"Pipeline video {poll_status}: {poll_data or 'unknown'}"
                update_job_status(job_id, "failed", account=worker.account_email, error=err)
                self.qm.signals.job_updated.emit(job_id, "failed", worker.account_email, err)
                return

            # Use media name from poll response
            if isinstance(poll_data, dict):
                vid_media_id = poll_data.get("name", vid_media_id) or vid_media_id
                data["_poll_media_item"] = poll_data

            # Download video
            data["_video_media_id"] = vid_media_id
            output_path, dl_error = await self._download_and_save(
                worker, job_id, data, queue_no=queue_no,
                access_token=worker.last_access_token,
            )

            if dl_error:
                update_job_status(job_id, "failed", account=worker.account_email, error=f"Pipeline download: {dl_error}")
                self.qm.signals.job_updated.emit(job_id, "failed", worker.account_email, f"Pipeline download: {dl_error}")
                return

            update_job_status(job_id, "completed", account=worker.account_email)
            self.qm.signals.job_updated.emit(job_id, "completed", worker.account_email, "")
            self.qm._record_throttle_success(worker.account_email)
            self._log(f"[{worker.slot_id}] Pipeline job {job_id[:6]}... completed! ({output_path})")

        except asyncio.CancelledError:
            update_job_status(job_id, "pending", account="")
            self.qm.signals.job_updated.emit(job_id, "pending", "", "")
        except Exception as e:
            err = str(e)[:300]
            update_job_status(job_id, "failed", account=worker.account_email, error=err)
            self.qm.signals.job_updated.emit(job_id, "failed", worker.account_email, err)
            self._log(f"[{worker.slot_id}] Pipeline FAILED: {err}")

    async def _run_job(self, worker: ExtensionWorker, job: dict):
        """Execute a single job with retries."""
        job_id = job["id"]
        job_type = job.get("job_type", "image")
        prompt = job.get("prompt", "")
        model = job.get("model", "")
        queue_no = job.get("output_index") or job.get("queue_no")

        # Pipeline jobs have their own 2-step flow
        if job_type == "pipeline":
            return await self._run_pipeline_job(worker, job)

        self._log(f"[{worker.slot_id}] Job {job_id[:6]}...: {prompt[:40]}...")

        max_retries = max(1, get_int_setting("max_auto_retries_per_job", 3))
        last_error = ""

        for attempt in range(max_retries + 1):
            if self.qm.stop_requested or self.qm.force_stop_requested:
                update_job_status(job_id, "pending", account="")
                self.qm.signals.job_updated.emit(job_id, "pending", "", "")
                return

            # If account was put on hold (by another slot's failure), abort retries and re-queue
            if self.qm.account_disabled.get(worker.account_email):
                self._log(
                    f"[{worker.slot_id}] Account on hold — aborting retries, re-queuing job."
                )
                update_job_status(job_id, "pending", account="")
                self.qm.signals.job_updated.emit(job_id, "pending", "", "")
                return

            # If account is 429-throttled, check if THIS slot should yield
            # (too many busy slots on this account — let others finish first)
            import time as _time
            _throttle_max = self.qm.account_throttle_max_slots.get(worker.account_email)
            if _throttle_max is not None and self.qm.account_throttle_until.get(worker.account_email, 0) > _time.time():
                # Count how many workers on this account are busy
                _account_workers = self._workers.get(worker.account_email, [])
                _busy = sum(1 for w in _account_workers if w.is_busy)
                if _busy > _throttle_max and attempt > 0:
                    self._log(
                        f"[{worker.slot_id}] Account throttled ({_throttle_max} slots max) — yielding job."
                    )
                    update_job_status(job_id, "pending", account="")
                    self.qm.signals.job_updated.emit(job_id, "pending", "", "")
                    return

            try:
                if "video" in job_type:
                    video_model = job.get("video_model") or model
                    ratio = job.get("video_ratio") or job.get("aspect_ratio") or "VIDEO_ASPECT_RATIO_LANDSCAPE"
                    # Extract video-specific fields from job
                    video_sub_mode = str(job.get("video_sub_mode") or "").strip() or "text_to_video"
                    ref_path = str(job.get("ref_path") or "").strip() or None
                    start_image_path = str(job.get("start_image_path") or "").strip() or None
                    end_image_path = str(job.get("end_image_path") or "").strip() or None
                    # ref_paths may also contain the reference for video
                    if not ref_path:
                        ref_paths_raw = job.get("ref_paths") or ""
                        if isinstance(ref_paths_raw, str) and ref_paths_raw.strip():
                            try:
                                parsed = json.loads(ref_paths_raw)
                                if isinstance(parsed, list) and parsed:
                                    ref_path = str(parsed[0]).strip()
                            except (json.JSONDecodeError, TypeError):
                                ref_path = ref_paths_raw.strip()

                    # Log the exact pairing so the user can see at a glance
                    # which image is going with which prompt. Surfaces any
                    # accidental duplication (same file + same prompt across
                    # multiple jobs) the moment it actually dispatches.
                    _pair_ref = os.path.basename(ref_path or start_image_path or end_image_path or "") or "(no ref)"
                    _pair_prompt = (prompt or "")[:60]
                    self._log(
                        f"[{worker.slot_id}] ↪ pair: ref={_pair_ref}  prompt={_pair_prompt!r}"
                    )

                    result, error = await worker.generate_video(
                        prompt, video_model, ratio,
                        video_sub_mode=video_sub_mode,
                        ref_path=ref_path,
                        start_image_path=start_image_path,
                        end_image_path=end_image_path,
                        upscale=job.get("video_upscale"),
                    )
                else:
                    ratio = job.get("aspect_ratio", "IMAGE_ASPECT_RATIO_LANDSCAPE")
                    # Parse ref_paths from job (JSON list or single path)
                    ref_paths = []
                    ref_paths_raw = job.get("ref_paths") or ""
                    if isinstance(ref_paths_raw, str) and ref_paths_raw.strip():
                        try:
                            parsed = json.loads(ref_paths_raw)
                            if isinstance(parsed, list):
                                ref_paths = [str(p).strip() for p in parsed if str(p).strip()]
                        except (json.JSONDecodeError, TypeError):
                            ref_paths = [ref_paths_raw.strip()]
                    # Also check single ref_path
                    single_ref = str(job.get("ref_path") or "").strip()
                    if single_ref and single_ref not in ref_paths:
                        ref_paths.insert(0, single_ref)

                    result, error = await worker.generate_image(
                        prompt, model, ratio,
                        references=job.get("reference_media_ids"),
                        ref_paths=ref_paths if ref_paths else None,
                    )

                if result and not error:
                    # Download and save
                    output_path, dl_error = await self._download_and_save(
                        worker, job_id, result, queue_no=queue_no,
                        access_token=worker.last_access_token,
                    )
                    if dl_error:
                        last_error = dl_error
                        self._log(f"[{worker.slot_id}] Download failed: {dl_error[:200]}")
                        if attempt < max_retries:
                            await asyncio.sleep(5)
                            continue
                    else:
                        update_job_status(job_id, "completed", account=worker.account_email)
                        self.qm.signals.job_updated.emit(job_id, "completed", worker.account_email, "")
                        self.qm._record_throttle_success(worker.account_email)
                        # Reset reCAPTCHA streak on success
                        self._recaptcha_streak.pop(worker.account_email, None)
                        # Reset the auto-retry cooldown cycle counter too —
                        # a successful generation means whatever was failing
                        # has cleared (e.g. VPN rotated to a clean IP).
                        self._recaptcha_cooldown_cycles.pop(worker.account_email, None)
                        self._recaptcha_cooldown_until.pop(worker.account_email, None)
                        # Reset 429 streak — account is back to normal
                        self.qm.clear_429_streak(worker.account_email)
                        # Track generation count for auto cleanup
                        self._check_auto_cleanup(worker.account_email)
                        self._log(f"[{worker.slot_id}] Job {job_id[:6]}... completed! ({output_path})")
                        return

                last_error = error or "Unknown error"
                self._log(
                    f"[{worker.slot_id}] Attempt {attempt + 1}/{max_retries + 1} "
                    f"failed: {last_error[:200]}"
                )

                # no_recaptcha_enterprise → tab doesn't have reCAPTCHA loaded
                # Extension already auto-reloads the tab, just wait for it
                if "no_recaptcha_enterprise" in last_error:
                    self._log(f"[{worker.slot_id}] Tab has no reCAPTCHA — waiting for auto-reload...")
                    if self.qm.stop_requested or self.qm.force_stop_requested:
                        update_job_status(job_id, "pending", account="")
                        self.qm.signals.job_updated.emit(job_id, "pending", "", "")
                        return
                    await asyncio.sleep(8)  # wait for tab reload + reCAPTCHA init
                    continue

                # 429 Rate limit — Google's hard rate cap (PUBLIC_ERROR_
                # UNUSUAL_ACTIVITY_TOO_MUCH_TRAFFIC). The previous soft
                # throttle just lowered slot count but the surviving slot
                # kept hammering, producing 30+ retries per job in a tight
                # loop and amplifying Google's lock. Now we:
                #   1. HARD pause the whole account (exponential backoff:
                #      5min → 15min → 1h → 24h+HOLD)
                #   2. Cap per-job 429 attempts at 1 — same prompt won't
                #      re-queue forever, it gets marked failed instead so
                #      the queue moves on to other prompts.
                err_lower = last_error.lower()
                if any(p in err_lower for p in ("429", "rate limit", "too many requests")):
                    # Keep the legacy slot throttle running too — it provides
                    # the gradual ramp-up if the pause expires successfully.
                    self.qm._throttle_account_for_429(worker.account_email)
                    # Hard pause the account (exponential backoff per strike)
                    self.qm.pause_account_for_429(worker.account_email)
                    # Per-job 429 attempt cap — fail this job after 1 strike
                    job_429_attempts = self.qm.increment_job_429_attempts(job_id)
                    if job_429_attempts >= 1:
                        self._log(
                            f"[{worker.slot_id}] 429 — job {job_id[:6]}… already "
                            f"hit rate-limit; marking failed (account paused, "
                            f"queue moves on)."
                        )
                        update_job_status(
                            job_id, "failed", account=worker.account_email,
                            error="429 rate-limit (account paused, see logs)",
                        )
                        self.qm.signals.job_updated.emit(
                            job_id, "failed", worker.account_email,
                            "429 rate-limit (account paused)",
                        )
                        return
                    # First 429 on this job — re-queue once for a different
                    # account / after pause expires.
                    self._log(f"[{worker.slot_id}] 429 detected — re-queuing job once.")
                    update_job_status(job_id, "pending", account="")
                    self.qm.signals.job_updated.emit(job_id, "pending", "", "")
                    return

                # reCAPTCHA score/token failure → track streak, hold if persistent
                if "recaptcha" in err_lower or "captcha" in err_lower:
                    streak = self._recaptcha_streak.get(worker.account_email, 0) + 1
                    self._recaptcha_streak[worker.account_email] = streak
                    self._log(
                        f"[{worker.slot_id}] reCAPTCHA failure #{streak} for {worker.account_email}"
                    )

                    if streak >= self.RECAPTCHA_HOLD_THRESHOLD:
                        cycles = self._recaptcha_cooldown_cycles.get(worker.account_email, 0) + 1
                        self._recaptcha_cooldown_cycles[worker.account_email] = cycles

                        if cycles >= self.RECAPTCHA_MAX_COOLDOWN_CYCLES:
                            # Many cycles failed in a row — assume VPN rotation
                            # is NOT happening (or IP changes aren't helping).
                            # Fall back to the original permanent-hold behavior
                            # so the user gets a clear notification + jobs are
                            # reassigned to working accounts.
                            already_held = self.qm.account_disabled.get(worker.account_email, False)
                            self.qm.account_disabled[worker.account_email] = True
                            self._recaptcha_cooldown_until.pop(worker.account_email, None)
                            try:
                                self._bridge.hold_ecosystem_account(
                                    worker.account_email, duration_seconds=172800
                                )
                            except Exception:
                                pass
                            if not already_held:
                                self._log(
                                    f"[ExtMode] ⛔ Account {worker.account_email}: "
                                    f"{cycles} reCAPTCHA cooldown cycles all failed — "
                                    f"HOLDING permanently (VPN rotation not recovering this one)."
                                )
                                self.qm.signals.account_auth_status.emit(
                                    worker.account_email, "expired",
                                    f"reCAPTCHA flagged ({cycles} cycles)"
                                )
                                self.qm.signals.show_warning.emit(
                                    f"Account '{worker.account_email}' tried {cycles} reCAPTCHA "
                                    f"cooldown cycles without success.\n"
                                    f"Google has likely flagged this account.\n"
                                    f"Close this account's extension tab and use a different account."
                                )
                                try:
                                    from src.db.db_manager import reassign_account_jobs
                                    count = reassign_account_jobs(worker.account_email)
                                    if count > 0:
                                        self._log(
                                            f"[ExtMode] Reassigned {count} job(s) from "
                                            f"{worker.account_email} to other accounts."
                                        )
                                except Exception:
                                    pass
                            update_job_status(job_id, "pending", account="")
                            self.qm.signals.job_updated.emit(job_id, "pending", "", "")
                            return

                        # Normal cooldown path — disable account temporarily,
                        # set a wake-up timestamp, reset streak so the dispatch
                        # loop's cooldown-expiry check picks it up. On VPN
                        # rotating IPs the next attempt typically succeeds on
                        # the fresh IP and the cycle counter (line above) will
                        # be cleared by the success path.
                        self.qm.account_disabled[worker.account_email] = True
                        self._recaptcha_cooldown_until[worker.account_email] = (
                            time.time() + self.RECAPTCHA_COOLDOWN_SECONDS
                        )
                        self._recaptcha_streak[worker.account_email] = 0
                        self._log(
                            f"[ExtMode] {worker.account_email}: {streak} reCAPTCHA "
                            f"failures — cooldown {self.RECAPTCHA_COOLDOWN_SECONDS}s "
                            f"(cycle {cycles}/{self.RECAPTCHA_MAX_COOLDOWN_CYCLES}). "
                            f"Auto-retry after wait. If your VPN rotates IP soon, "
                            f"generation should resume on the new IP."
                        )
                        # Re-queue current job; worker picks it up after cooldown
                        update_job_status(job_id, "pending", account="")
                        self.qm.signals.job_updated.emit(job_id, "pending", "", "")
                        return

                    # Not at threshold yet — reload tab and retry
                    self._bridge.send_command("reload_tab", worker.account_email)
                    if self.qm.stop_requested or self.qm.force_stop_requested:
                        update_job_status(job_id, "pending", account="")
                        self.qm.signals.job_updated.emit(job_id, "pending", "", "")
                        return
                    await asyncio.sleep(5)
                    continue

                # Moderation / content blocked — don't retry, same prompt won't pass
                if last_error.startswith("MODERATION:"):
                    self._log(f"[{worker.slot_id}] Content blocked by moderation — not retrying.")
                    break

                # Network-level failure ("Bridge error: fetch_failed",
                # "timeout", "network error"). We CANNOT know whether
                # Google actually received the request — the fetch may
                # have completed server-side but our connection dropped
                # before the response came back. Retrying would re-upload
                # the reference image AND re-submit the video request,
                # creating a duplicate video on Google's side and a
                # duplicate reference asset in the Flow project. Fail
                # the job fast instead so the user can decide to retry
                # once they confirm Google didn't process the first try.
                if any(marker in last_error for marker in (
                    "fetch_failed",
                    "Bridge error: timeout",
                    "Failed to fetch",
                    "Bridge error: no_labs_tab",
                )):
                    self._log(
                        f"[{worker.slot_id}] Network-level failure — not retrying "
                        f"(Google may have received the first request). "
                        f"Marking failed; use Retry Failed to re-queue manually."
                    )
                    update_job_status(
                        job_id, "failed", account=worker.account_email,
                        error=f"Network error (unsafe to retry): {last_error[:150]}",
                    )
                    self.qm.signals.job_updated.emit(
                        job_id, "failed", worker.account_email,
                        f"Network error: {last_error[:100]}",
                    )
                    return

                # MODEL_ACCESS_DENIED — account just doesn't have access to
                # this model (e.g. Veo video on a free Gmail). Retrying is
                # pointless and burns reCAPTCHA score, which then trips
                # PUBLIC_ERROR_UNUSUAL_ACTIVITY on subsequent jobs from the
                # same account. Mark the account as no-access for this
                # model so we skip the rest of the queue's video jobs on it.
                if last_error.startswith("MODEL_ACCESS_DENIED"):
                    self._log(
                        f"[{worker.slot_id}] No access to model — not retrying. "
                        f"Skipping all remaining {job_type} jobs on {worker.account_email}."
                    )
                    # Disable account for future jobs of this type
                    self.qm.account_disabled[worker.account_email] = True
                    self.qm.signals.account_auth_status.emit(
                        worker.account_email, "expired",
                        f"No access to {job_type} model (likely needs paid plan)"
                    )
                    # Reassign account's pending jobs to other accounts
                    try:
                        from src.db.db_manager import reassign_account_jobs
                        count = reassign_account_jobs(worker.account_email)
                        if count > 0:
                            self._log(
                                f"[ExtMode] Reassigned {count} job(s) from "
                                f"{worker.account_email} (no model access)."
                            )
                    except Exception:
                        pass
                    break

                # 400 "invalid argument" — same prompt won't fix on retry, fail after 2 attempts
                if "400" in last_error and attempt >= 1:
                    self._log(f"[{worker.slot_id}] Same 400 error twice — skipping prompt.")
                    break  # exit retry loop → mark as failed

                if attempt < max_retries:
                    # Check stop before retry sleep
                    if self.qm.stop_requested or self.qm.force_stop_requested:
                        update_job_status(job_id, "pending", account="")
                        self.qm.signals.job_updated.emit(job_id, "pending", "", "")
                        return
                    await asyncio.sleep(min(10 * (attempt + 1), 15))  # cap at 15s

            except asyncio.CancelledError:
                # Task was cancelled by stop — re-queue job
                update_job_status(job_id, "pending", account="")
                self.qm.signals.job_updated.emit(job_id, "pending", "", "")
                return

            except Exception as e:
                last_error = str(e)[:300]
                self._log(f"[{worker.slot_id}] Attempt {attempt + 1} exception: {last_error}")
                if attempt < max_retries:
                    if self.qm.stop_requested or self.qm.force_stop_requested:
                        update_job_status(job_id, "pending", account="")
                        self.qm.signals.job_updated.emit(job_id, "pending", "", "")
                        return
                    await asyncio.sleep(10)

        update_job_status(job_id, "failed", account=worker.account_email, error=last_error)
        self.qm.signals.job_updated.emit(job_id, "failed", worker.account_email, last_error)
        self._log(f"[{worker.slot_id}] Job {job_id[:6]}... FAILED: {last_error[:200]}")

    async def _download_and_save(self, worker, job_id, api_data, queue_no=None, access_token=None):
        """Download generated media via direct HTTP and save to output directory."""
        fife_url = None
        is_video = False

        # ── VIDEO: check _video_media_id FIRST (set by generate_video after polling) ──
        video_media_id = api_data.get("_video_media_id", "") if isinstance(api_data, dict) else ""
        if video_media_id:
            fife_url = (
                f"https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?"
                f"name={video_media_id}"
            )
            is_video = True

        # ── IMAGE: extract fifeUrl or build backbone.redirect URL ──
        if not fife_url:
            media_list = api_data.get("media", []) if isinstance(api_data, dict) else []
            media_name = None
            for item in media_list:
                url = (item.get("image", {}).get("generatedImage", {}).get("fifeUrl", "")
                       if isinstance(item, dict) else "")
                name = item.get("name", "") if isinstance(item, dict) else ""
                if url:
                    fife_url = url
                    break
                if name and not fife_url:
                    media_name = name

            if not fife_url and media_name:
                fife_url = (
                    f"https://labs.google/fx/api/trpc/backbone.redirect?"
                    f"input=%7B%22name%22%3A%22{media_name}%22%7D"
                )

        if not fife_url:
            return None, "No downloadable media in API response"

        dl_headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
            "referer": "https://labs.google/",
        }

        # ── VIDEO: direct HTTP with browser cookies (fast) ──
        if is_video:
            try:
                # Get browser cookies via extension
                cookie_result = await worker._bridge.request_token(
                    worker.account_email, "GET_COOKIES", timeout=10.0,
                )
                cookie_str = cookie_result.get("cookies", "")
                if cookie_str:
                    dl_headers["cookie"] = cookie_str
                    async with _make_aiohttp_session() as session:
                        async with session.get(
                            fife_url,
                            headers=dl_headers,
                            timeout=aiohttp.ClientTimeout(total=120),
                            allow_redirects=True,
                        ) as resp:
                            if resp.ok:
                                content_type = str(resp.headers.get("content-type", "")).lower()
                                data = await resp.read()
                                if data:
                                    return await self._save_media(job_id, data, content_type, queue_no, slot_id=worker.slot_id)

                # Fallback: bridge webRequest method
                self._log(f"[{worker.slot_id}] Direct download failed, using bridge fallback...")
                bridge_result = await worker._bridge.request_token(
                    worker.account_email,
                    f"DOWNLOAD_MEDIA:{fife_url}",
                    timeout=60.0,
                )
                err = bridge_result.get("error", "")
                if err:
                    return None, f"Bridge download error: {err}"

                cdn_url = bridge_result.get("cdn_url", "")
                if cdn_url:
                    dl_headers.pop("cookie", None)
                    async with _make_aiohttp_session() as session:
                        async with session.get(
                            cdn_url,
                            headers=dl_headers,
                            timeout=aiohttp.ClientTimeout(total=120),
                            allow_redirects=True,
                        ) as resp:
                            if not resp.ok:
                                return None, f"CDN download HTTP {resp.status}"
                            content_type = str(resp.headers.get("content-type", "")).lower()
                            data = await resp.read()
                            if not data:
                                return None, "Downloaded empty file from CDN"
                    return await self._save_media(job_id, data, content_type, queue_no, slot_id=worker.slot_id)

                return None, "No download URL available"
            except Exception as e:
                return None, f"Download error: {str(e)[:200]}"

        # ── IMAGE: direct aiohttp download (no cookies needed) ──
        try:
            async with _make_aiohttp_session() as session:
                async with session.get(
                    fife_url,
                    headers=dl_headers,
                    timeout=aiohttp.ClientTimeout(total=120),
                    allow_redirects=True,
                ) as resp:
                    if not resp.ok:
                        return None, f"Download HTTP {resp.status}"

                    content_type = str(resp.headers.get("content-type", "")).lower()
                    data = await resp.read()
                    if not data:
                        return None, "Downloaded empty file"

            return await self._save_media(job_id, data, content_type, queue_no, slot_id=worker.slot_id)

        except Exception as e:
            return None, f"Download error: {str(e)[:200]}"

    async def _save_media(self, job_id, data, content_type, queue_no=None, slot_id="ext"):
        """Save downloaded media bytes to output directory. Returns (path, error)."""
        ext_map = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/webp": ".webp",
            "video/mp4": ".mp4",
            "video/webm": ".webm",
        }
        content_type = str(content_type or "").lower()
        ext = ".jpg"
        for mime, candidate_ext in ext_map.items():
            if mime in content_type:
                ext = candidate_ext
                break
        # Fallback: sniff from data magic bytes
        if ext == ".jpg" and data[:4] in (b'\x00\x00\x00\x18', b'\x00\x00\x00\x1c', b'\x00\x00\x00 '):
            ext = ".mp4"

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

        try:
            update_job_runtime_state(job_id, output_path=output_path)
        except Exception:
            pass

        self._log(f"[{slot_id}] Saved: {filename} ({len(data)} bytes)")
        return output_path, None
