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
import urllib.parse
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
    get_job_model,
    get_output_directory,
    set_setting,
    update_job_model,
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
    # Nano Banana models — order matters here. "lite" MUST be checked before
    # the generic "nano banana" match below, and "pro" before generic too.
    # HARBOR_SEAL is the API name for the new Nano Banana 2 Lite variant
    # (confirmed via HAR capture 2026-07-01).
    if "lite" in lower and "nano banana" in lower:
        return "HARBOR_SEAL"
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


def _resolve_image_model_ui(model_name):
    """Map a model (UI name OR resolved API enum) to the exact label Flow's
    model dropdown shows, for the UI-drive path. Verified live against Flow's
    'Select model family' menu: 'Nano Banana Pro', 'Nano Banana 2.1',
    'Nano Banana 2 Lite' (Flow renamed the standard model 'Nano Banana 2' ->
    'Nano Banana 2.1' on 2026-10-07). Empty string = leave Flow's current
    selection. Old 'Nano Banana 2' / NARWHAL inputs map to the current 2.1."""
    raw = str(model_name or "").strip()
    lower = raw.lower()
    # API enums (from _resolve_image_model)
    if raw in ("GEM_PIX_2",) or "nano banana pro" in lower:
        return "Nano Banana Pro"
    if raw in ("HARBOR_SEAL",) or ("lite" in lower and "nano banana" in lower):
        return "Nano Banana 2 Lite"
    if raw in ("NARWHAL",) or "nano banana" in lower:
        return "Nano Banana 2.1"
    # Imagen and anything unknown → default to the standard model.
    if "imagen" in lower:
        return "Nano Banana 2.1"
    return "Nano Banana 2.1"


def _resolve_image_ratio_ui(ratio_name):
    """Map a ratio (UI name or IMAGE_ASPECT_RATIO_* enum) to the aspect label
    Flow's settings popover shows: 16:9, 4:3, 1:1, 3:4, 9:16."""
    raw = str(ratio_name or "").strip()
    lower = raw.lower()
    if "four_three" in lower or "4:3" in lower:
        return "4:3"
    if "three_four" in lower or "3:4" in lower:
        return "3:4"
    if "portrait" in lower or "9:16" in lower:
        return "9:16"
    if "square" in lower or "1:1" in lower:
        return "1:1"
    return "16:9"


def _parse_prompt_segments(raw):
    """Parse a job's stored prompt_segments (JSON list of {type:text|ref}) into
    a Python list, or None. Used to build Flow's positional inline reference
    markers in the ogiZ0b prompt slot."""
    if not raw:
        return None
    if isinstance(raw, list):
        return raw or None
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return None
        try:
            val = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        return val if (isinstance(val, list) and val) else None
    return None


def _image_aspect_ratio_int(ratio_name):
    """Map an image aspect ratio (UI name or IMAGE_ASPECT_RATIO_* enum) to
    the integer Flow's batchexecute RPC expects at position [1][0][4] of
    the ogiZ0b payload.

    Observed from a real Landscape 16:9 request in the HAR: value = 3.
    The other integers are best-effort guesses aligned with Google's
    typical protobuf enum ordering (0 = UNSPECIFIED). If any of these
    turn out wrong on-server we'll see a 400 response with a hint we can
    correct against — safer than silently misgenerating.
    """
    raw = str(ratio_name or "").strip().lower()
    if "portrait" in raw or "9:16" in raw:
        return 2
    if "square" in raw or "1:1" in raw:
        return 1
    if "3:4" in raw:
        return 4
    if "4:3" in raw:
        return 5
    # Default = LANDSCAPE 16:9 (the confirmed-working value from the HAR)
    return 3


def _find_flow_content_url(obj):
    """Depth-first search for the first 'https://flow-content.google/...'
    URL inside a batchexecute result tree. The ogiZ0b response nests the
    signed CDN URL several levels deep and the array shape drifts between
    Google pushes, so a shape-agnostic scan is more resilient than
    indexing.
    """
    if isinstance(obj, str):
        return obj if obj.startswith("https://flow-content.google/") else None
    if isinstance(obj, dict):
        for v in obj.values():
            found = _find_flow_content_url(v)
            if found:
                return found
        return None
    if isinstance(obj, (list, tuple)):
        for v in obj:
            found = _find_flow_content_url(v)
            if found:
                return found
    return None


def _find_media_id_near_url(obj, url):
    """Extract the media UUID that owns `url`. In the ogiZ0b response the
    UUID appears as the first element of the tuple that also carries the
    URL. Falls back to parsing the UUID out of the URL path itself, which
    matches the pattern flow-content.google/image/<UUID>?...
    """
    try:
        import re as _re
        m = _re.search(r"/image/([0-9a-f-]{8,})", url or "")
        if m:
            return m.group(1)
    except Exception:
        pass
    return ""


def _find_first_uuid(obj):
    """Depth-first search for the first UUID-shaped string in a batchexecute
    result tree. Flow's `maseQ` (image upload) RPC returns the freshly-created
    media's id as a bare UUID nested a few levels into the response; the exact
    position drifts between pushes, so a shape-agnostic scan is more resilient
    than indexing. Returns the UUID string or "".
    """
    import re as _re
    _UUID_RE = _re.compile(
        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    )
    if isinstance(obj, str):
        return obj if _UUID_RE.match(obj) else ""
    if isinstance(obj, dict):
        for v in obj.values():
            found = _find_first_uuid(v)
            if found:
                return found
        return ""
    if isinstance(obj, (list, tuple)):
        for v in obj:
            found = _find_first_uuid(v)
            if found:
                return found
    return ""


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
        # Generate images by driving Flow's real UI (trusted Generate click)
        # instead of the direct batchexecute POST, which reCAPTCHA flags as
        # PUBLIC_ERROR_UNUSUAL_ACTIVITY. The UI path is what manual generation
        # uses, so it passes. (References are text-only in this mode.)
        self.use_ui_drive = True

    async def _upload_reference_image(self, project_id, file_path):
        """Upload a single reference image and return its media id (UUID).

        New Flow (flow.google.com) uploads through the `maseQ` batchexecute
        RPC — the old aisandbox-pa `flow/uploadImage` + Bearer path is dead
        (there's no access_token after the NextAuth→Angular migration). The
        payload shape was captured live from Flow's own upload request:

            [ [None,22,None,None,None, <projectId>, None,None,None,None,
               ["<RECAPTCHA_TOKEN>", 1]],   # clientContext
              "<base64 image bytes>",        # [1]
              "<mimeType>",                  # [2]
              1,                             # [3]
              None,None,None,None,           # [4..7]
              "<fileName>",                  # [8]
              None,                          # [9]
              "<uuid1>", "<uuid2>" ]         # [10..11]

        The extension mints a fresh reCAPTCHA token in the flow.google.com
        tab's MAIN world (cookie/SAPISIDHASH auth), substitutes it for the
        "<RECAPTCHA_TOKEN>" placeholder, and POSTs from inside the page so
        Chrome's signed browser headers are attached. Returns the new media
        UUID which the caller drops into the ogiZ0b reference slot.
        """
        if not file_path or not os.path.exists(file_path):
            raise RuntimeError(f"Reference file not found: {file_path}")

        with open(file_path, "rb") as f:
            image_bytes_b64 = base64.b64encode(f.read()).decode("utf-8")

        file_name = os.path.basename(file_path)
        mime_type, _ = mimetypes.guess_type(file_path)
        mime_type = mime_type or "image/png"

        payload = [
            [None, 22, None, None, None, project_id, None, None, None, None,
             ["<RECAPTCHA_TOKEN>", 1]],
            image_bytes_b64,
            mime_type,
            1,
            None, None, None, None,
            file_name,
            None,
            str(uuid.uuid4()).upper(),
            str(uuid.uuid4()).upper(),
        ]
        payload_template = json.dumps(payload)

        be_result = await self._bridge.request_batchexecute(
            account=self.account_email,
            rpc_id="maseQ",
            payload_template=payload_template,
            source_path=f"/project/{project_id}",
            recaptcha_action="IMAGE_GENERATION",
            timeout=120,
        )

        err = be_result.get("error")
        if err:
            raise RuntimeError(f"Upload failed (maseQ): {err}")

        media_id = _find_first_uuid(be_result.get("result"))
        if not media_id:
            raise RuntimeError(
                f"Upload response missing media id: "
                f"{str(be_result.get('result'))[:200]}"
            )

        self._log(f"[{self.slot_id}] Uploaded reference: {file_name} -> {media_id}")
        return media_id

    async def _upload_and_cache_reference(self, project_id, file_path):
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

            media_name = await self._upload_reference_image(project_id, file_path)
            # Store in both caches
            ExtensionWorker._reference_cache[cache_key] = media_name
            set_cached_media_id(project_id, abs_path, media_name)
            return media_name

    async def _upload_references(self, project_id, ref_paths):
        """Upload multiple reference images, return list of media IDs."""
        media_ids = []
        for path in ref_paths:
            path = str(path).strip()
            if not path:
                continue
            media_name = await self._upload_and_cache_reference(project_id, path)
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

    async def _generate_image_ui(self, prompt, model, ratio):
        """Generate one image by driving Flow's real UI via the extension.

        The extension types the prompt into Flow's composer, sets model/aspect
        in the settings popover, and clicks the actual "Start generation"
        button with a TRUSTED chrome.debugger click. Flow mints the reCAPTCHA
        token off that gesture, so it passes exactly like a manual generation
        (no PUBLIC_ERROR_UNUSUAL_ACTIVITY). Returns (data, None) on success or
        (None, error_message) — same contract as generate_image.
        """
        self.is_busy = True
        try:
            model_ui = _resolve_image_model_ui(model)
            aspect_ui = _resolve_image_ratio_ui(ratio)
            self._log(f"[{self.slot_id}] Image (UI-drive): {model_ui}, {aspect_ui}")

            # A project must be open in the tab for the composer to exist. Use a
            # cached project id if we have one; the extension also opens/creates
            # a project itself when the composer is missing.
            project_id = self._bridge.get_project_id(self.account_email)
            if not project_id:
                try:
                    project_id = await self._resolve_project_id("")
                except Exception:
                    project_id = None
            source_path = f"/project/{project_id}" if project_id else "/"

            res = await self._bridge.request_flow_ui(
                account=self.account_email,
                prompt=prompt,
                model=model_ui,
                aspect=aspect_ui,
                source_path=source_path,
                timeout=260,
            )

            # Diagnostic: when the extension returns neither a URL nor an error,
            # dump exactly what came back so we can see WHY (empty result, lost
            # media id, reroute shape, etc.).
            if not (res.get("cdn_url")):
                try:
                    _raw = json.dumps(res, default=str)[:600]
                except Exception:
                    _raw = str(res)[:600]
                self._log(f"[{self.slot_id}] UI-drive non-success res: {_raw}")

            err = res.get("error") or ""
            if err:
                el = err.lower()
                if "gen_error:" in el:
                    reason = err.split("gen_error:", 1)[-1].strip()
                    if "unusual_activity" in el:
                        # Rare on the UI path, but if Flow ever flags it, a short
                        # cool-off + single-slot spacing is the fix.
                        try:
                            self._bridge.hold_account(self.account_email, 300)
                        except Exception:
                            pass
                        return None, (
                            "🚫 reCAPTCHA flagged UNUSUAL ACTIVITY (rare on the UI path). "
                            "Paused 5 min. Keep the flow.google.com tab open/visible, use "
                            "1 parallel slot, and add a small stagger between images."
                        )
                    return None, f"⛔ Flow generation error: {reason}"
                if "no_labs_tab" in el:
                    return None, "No flow.google.com tab open — open Flow (and a project)."
                if "no_composer" in el:
                    return None, ("Flow project not open — open a project in the "
                                  "flow.google.com tab so the prompt box is visible.")
                if "generate_disabled" in el:
                    return None, "Flow's Generate button stayed disabled (still busy) — retrying."
                if "flow_did_not_accept" in el:
                    return None, "Flow didn't accept the prompt (Generate click didn't register) — retrying."
                if "no_result_timeout" in el or "timeout" in el:
                    return None, "No image came back within the time limit — retrying."
                return None, f"Bridge error: {err}"

            cdn_url = res.get("cdn_url")
            media_id = res.get("media_id")
            if not cdn_url:
                # An empty result (no url AND no error) means the extension that
                # served this account fell through to the old token-mint path —
                # i.e. it has NO EXECUTE_FLOW_UI handler, so it's running an OLD
                # version. Each Chrome profile/window loads the unpacked
                # extension independently: reloading it in one profile does NOT
                # update another. Reload it in THIS account's Chrome.
                return None, (
                    "⛔ The Chrome serving this account is running an OLD G-Labs "
                    "Studio Helper (no UI-drive support). Reload the extension in "
                    "THAT account's Chrome profile: chrome://extensions → G-Labs "
                    "Studio Helper → Reload (needs v2.8.1+), then retry."
                )

            # Same shape the download path expects (media[0].image.imageUrl).
            data = {
                "media": [{
                    "name": media_id,
                    "image": {"imageUrl": cdn_url},
                }],
            }
            self.jobs_completed += 1
            return data, None
        except Exception as e:
            return None, f"Exception (UI-drive): {str(e)[:300]}"
        finally:
            self.is_busy = False

    async def generate_image(self, prompt, model, ratio, references=None, ref_paths=None,
                             prompt_segments=None):
        """Generate image via direct API call (token from extension).

        `prompt_segments` (optional) is an ordered list of {type:text|ref} that
        places references INLINE at their exact position in the prompt (Flow's
        native format). When present the ogiZ0b prompt slot is built as
        interleaved text + reference markers; otherwise the flat single-string
        prompt is used with references appended to the reference slot only.
        """
        # UI-drive path: type the prompt + click Flow's real Generate button
        # (trusted). This is the reliable path that dodges the reCAPTCHA
        # UNUSUAL_ACTIVITY block the direct POST hits. References are dropped
        # here (text-only) — the self-contained prompts describe the scene.
        if getattr(self, "use_ui_drive", True):
            if ref_paths or prompt_segments:
                self._log(
                    f"[{self.slot_id}] ⚠ UI-drive is text-only — "
                    f"{len(ref_paths or [])} reference image(s) not attached "
                    f"(the prompt text carries the description)."
                )
            return await self._generate_image_ui(prompt, model, ratio)

        self.is_busy = True
        try:
            api_model = _resolve_image_model(model)
            api_ratio = _resolve_image_ratio(ratio)
            seed = random.randint(100000, 999999)
            batch_id = str(uuid.uuid4())
            prompt_text = prompt if prompt.endswith("\n") else f"{prompt}\n"

            self._log(f"[{self.slot_id}] Image: {api_model}, {api_ratio}")

            # Get project ID + XSRF from extension via bridge. Since Google
            # migrated Flow off the old NextAuth stack there's no
            # access_token anymore — batchexecute authenticates on the
            # SAPISIDHASH cookie the browser already has. The bridge still
            # returns the SNlM0e XSRF token in the `access_token` slot for
            # legacy field-name reasons; we accept either shape here.
            bridge_result = await self._bridge.request_token(
                self.account_email, "IMAGE_GENERATION", timeout=60
            )

            if bridge_result.get("error"):
                return None, f"Bridge error: {bridge_result['error']}"

            access_token = bridge_result.get("access_token")  # now = XSRF for batchexecute
            project_id = bridge_result.get("project_id") or self._bridge.get_project_id(self.account_email)
            self.last_access_token = access_token  # cache for download-path cookie warmup

            # If no project ID, resolve with lock (so only 1 worker creates per account).
            # _resolve_project_id() also handles the extension's get_project
            # command which reads the project ID straight from the open tab —
            # that path doesn't need access_token either.
            if not project_id:
                project_id = await self._resolve_project_id(access_token or "")

            if not project_id:
                return None, "No project ID available — open a project in flow.google.com"

            # Reference images (character / location consistency) upload
            # through the `maseQ` batchexecute RPC — cookie-auth via the
            # extension, no access_token needed. Each upload is cached per
            # (project, file) so a character photo only uploads once. The
            # returned media UUIDs are injected into the ogiZ0b reference
            # slot below.
            media_ids = list(references or [])
            if ref_paths:
                try:
                    uploaded = await self._upload_references(project_id, ref_paths)
                    media_ids.extend(uploaded)
                except Exception as e:
                    self._log(
                        f"[{self.slot_id}] ⚠ Reference upload failed "
                        f"({str(e)[:150]}) — continuing without them."
                    )

            # Positional inline references (Flow-native): map each segment's
            # file path to its uploaded media id (cache-hit — already uploaded
            # above via ref_paths). Enables interleaved text+reference prompt.
            seg_media_map = {}
            if prompt_segments:
                for seg in prompt_segments:
                    if not (isinstance(seg, dict) and seg.get("type") == "ref"):
                        continue
                    p = str(seg.get("path") or "").strip()
                    if p and p not in seg_media_map:
                        try:
                            seg_media_map[p] = await self._upload_and_cache_reference(project_id, p)
                        except Exception:
                            pass

            # NEW FLOW (Dec 2026): Google migrated Flow's image endpoint
            # from the labs.google tRPC + Bearer stack to their internal
            # Angular + batchexecute stack on flow.google.com. See
            # tests/flow-3-models.har for the field shape — the request
            # goes through RPC id "ogiZ0b" and the response embeds the
            # signed CDN URL synchronously (no polling needed).
            #
            # The reCAPTCHA token is embedded inside the payload string
            # at TWO positions, both wrapped as ["<TOKEN>", 1]. We use
            # the literal placeholder "<RECAPTCHA_TOKEN>" and the
            # extension substitutes a freshly-minted token in-place
            # before the POST fires.
            aspect_int = _image_aspect_ratio_int(ratio)
            uuid1 = str(uuid.uuid4()).upper()
            uuid2 = str(uuid.uuid4()).upper()
            uuid3 = str(uuid.uuid4()).upper()

            def _build_payload(pid: str) -> str:
                # This is the ogiZ0b payload as observed in the HAR,
                # rebuilt with parameterized project_id / model / aspect
                # / prompt / seed / UUIDs. Keep the field positions
                # identical to what Flow sends — a shifted array is
                # rejected server-side with an opaque parse error.
                #
                # Reference images (character / location consistency) live
                # at position [2] of the inner generation item as a list of
                # [media_id, None, None, None, 1] entries — None when there
                # are no references. Verified live against Flow's own
                # ogiZ0b request: attaching one entry keeps the character,
                # two entries (character + location) keep both consistent.
                ref_slot = (
                    [[mid, None, None, None, 1] for mid in media_ids]
                    if media_ids else None
                )

                # Prompt slot [8]: positional inline references (Flow-native)
                # when we have segments + their media ids — an ordered mix of
                # text runs ["text"] and reference markers
                # [None, [[media_id, filename]]]. Falls back to the flat single
                # string [[[prompt_text]]] for plain prompts / legacy jobs.
                prompt_slot = [[[prompt_text]]]
                if prompt_segments and seg_media_map:
                    parts = []
                    for seg in prompt_segments:
                        if not isinstance(seg, dict):
                            continue
                        if seg.get("type") == "text":
                            t = seg.get("text", "")
                            if t:
                                parts.append([t])
                        elif seg.get("type") == "ref":
                            p = str(seg.get("path") or "").strip()
                            mid = seg_media_map.get(p)
                            if mid:
                                parts.append([None, [[mid, seg.get("name")
                                                      or os.path.basename(p)]]])
                    if any(isinstance(x, list) and len(x) == 2 and x[0] is None
                           for x in parts):
                        # only use positional form if it actually placed a ref
                        prompt_slot = [parts]

                payload = [
                    None,
                    [[
                        None, None, ref_slot, seed, aspect_int, api_model, None,
                        [None, 22, None, None, None, pid, None, None, None, None,
                         ["<RECAPTCHA_TOKEN>", 1]],
                        prompt_slot,
                        None, None, None,
                        uuid1, uuid2,
                    ]],
                    1,
                    [None, 22, None, None, None, pid, None, None, None, None,
                     ["<RECAPTCHA_TOKEN>", 1]],
                    [uuid3],
                ]
                return json.dumps(payload)

            # Retry budget: initial try + one 429-burn rotation + one
            # transient-bridge-error retry.
            attempts_left = 3
            data = None
            err_msg = None
            payload_str = _build_payload(project_id)
            source_path = f"/project/{project_id}"

            while attempts_left > 0:
                attempts_left -= 1

                be_result = await self._bridge.request_batchexecute(
                    account=self.account_email,
                    rpc_id="ogiZ0b",
                    payload_template=payload_str,
                    source_path=source_path,
                    recaptcha_action="IMAGE_GENERATION",
                    timeout=120,
                )

                err = be_result.get("error") or ""
                if err:
                    err_lower = err.lower()
                    # Transient bridge failures (safe-to-retry) mirror
                    # the EXECUTE_FETCH classification list.
                    safe_to_retry = (
                        "fetch_failed" in err_lower
                        or "execute_batchexecute_threw" in err_lower
                        or "frame with id" in err_lower
                        or "no_recaptcha_enterprise" in err_lower
                        or "no_script_result" in err_lower
                        or "no_labs_tab" in err_lower
                    )
                    if safe_to_retry and attempts_left > 0:
                        self._log(
                            f"[{self.slot_id}] Transient bridge error "
                            f"({err[:100]}) — retrying in 2s..."
                        )
                        await asyncio.sleep(2)
                        continue

                    # Google embedded a generation error inside the wrb.fr row
                    # (bot/abuse flag, policy block, etc.). Retrying an
                    # UNUSUAL_ACTIVITY flag only deepens it — hold the account and
                    # surface a clear, actionable message instead.
                    if "gen_error:" in err_lower:
                        reason = err.split("gen_error:", 1)[-1].strip()
                        if "unusual_activity" in err_lower:
                            # NOT an account ban (manual generation still works) —
                            # it's reCAPTCHA bot-SCORING on the generation action,
                            # triggered by parallel/rapid automated requests. Brief
                            # cool-off (10 min) to break the bot-like burst, and tell
                            # the user the real fix: 1 slot + spacing.
                            try:
                                self._bridge.hold_account(self.account_email, 600)
                            except Exception:
                                pass
                            return None, (
                                "🚫 reCAPTCHA flagged UNUSUAL ACTIVITY on generation "
                                "(the account is fine — manual gen still works; it's the "
                                "automated burst that scores as a bot). Paused 10 min. FIX: "
                                "Settings → set Parallel slots = 1, add a 15–30s stagger, "
                                "run smaller batches, and keep the flow.google.com tab active "
                                "(generate one by hand occasionally). 5 parallel tabs on one "
                                "account is the main trigger."
                            )
                        return None, f"⛔ Flow generation error: {reason}"

                    # Server-side batchexecute error line (batchexecute_er:401
                    # etc.) means Flow rejected the RPC — treat 401 as an
                    # auth-lost signal so the outer recovery kicks in.
                    if "batchexecute_er:401" in err_lower:
                        return None, "🔑 Session expired (401) — reload the flow.google.com tab."
                    if "batchexecute_er:429" in err_lower or "batchexecute_er:8" in err_lower:
                        # 429 or RESOURCE_EXHAUSTED. Try a project rotate.
                        if attempts_left > 0:
                            self._log(
                                f"[{self.slot_id}] batchexecute 429/exhausted on "
                                f"project {project_id} — burning and rotating."
                            )
                            self._bridge.burn_project(self.account_email, project_id)
                            new_pid = await self._resolve_project_id(access_token or "")
                            if new_pid and new_pid != project_id:
                                project_id = new_pid
                                payload_str = _build_payload(project_id)
                                source_path = f"/project/{project_id}"
                                continue
                        return None, "⛔ Quota exhausted (batchexecute 429)"

                    return None, f"Bridge error: {err}"

                # Success path — extract the signed CDN URL from the
                # deeply-nested result tree.
                result_body = be_result.get("result")
                if result_body is None:
                    # Diagnostics: dump exactly what Flow returned so we can tell
                    # apart a quota/moderation block, a changed response shape, or
                    # a token problem. (status 200 + null result = Flow accepted
                    # but produced nothing; non-200 = server rejection.)
                    try:
                        _keys = list(be_result.keys())
                        _status = be_result.get("status")
                        _raw = json.dumps(be_result, default=str)
                    except Exception:
                        _keys, _status, _raw = "?", "?", str(be_result)
                    self._log(
                        f"[{self.slot_id}] EMPTY RESULT diag — status={_status} "
                        f"keys={_keys} refs={len(ref_paths or [])} "
                        f"promptlen={len(prompt or '')} raw={_raw[:1400]}"
                    )
                    err_msg = "Empty batchexecute response"
                    break

                cdn_url = _find_flow_content_url(result_body)
                if not cdn_url:
                    # Log the shape so we can eyeball what changed.
                    try:
                        shape = json.dumps(result_body, indent=2, default=str)[:1500]
                    except Exception:
                        shape = str(result_body)[:1500]
                    self._log(
                        f"[{self.slot_id}] No flow-content URL in ogiZ0b "
                        f"response — result shape:\n{shape}"
                    )
                    err_msg = "No downloadable media URL in batchexecute response"
                    break

                media_id = _find_media_id_near_url(result_body, cdn_url)

                # Shape the result to match what the download path expects:
                # api_data["media"][0]["image"]["imageUrl"] = <cdn_url>
                data = {
                    "media": [{
                        "name": media_id,
                        "image": {"imageUrl": cdn_url},
                    }],
                    # Keep the raw batchexecute result for anyone who
                    # wants to poke at it in logs.
                    "_batchexecute_raw": result_body,
                }
                err_msg = None
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
                        project_id, ref_path
                    )
                if start_image_path and sub_mode in ("frames_start", "frames_start_end"):
                    start_media_id = await self._upload_and_cache_reference(
                        project_id, start_image_path
                    )
                if end_image_path and sub_mode == "frames_start_end":
                    end_media_id = await self._upload_and_cache_reference(
                        project_id, end_image_path
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

        # ── Method 0: Use the project the user ALREADY has open ───────────
        # Before any API call or button click, ask the extension to read the
        # project ID straight from this account's open /project/<id> tab. If a
        # project is open (user opened one, or the account is onboarded), this
        # resolves instantly — NO Bearer token, NO navigation — sidestepping
        # Method 1/2 (which 401 on fresh accounts) and Method 3 (which is flaky
        # and, ironically, FAILS precisely when a project is already open,
        # because it navigates away to click "New project"). get_project_id()
        # filters burned projects, so during a burn+rotate this returns the
        # open (burned) project as None and we correctly fall through to create
        # a fresh one. Only genuinely project-less accounts reach Methods 1-4.
        try:
            self._bridge.send_command("get_project", self.account_email)
            for _ in range(6):
                await asyncio.sleep(1)
                pid = self._bridge.get_project_id(self.account_email)
                if pid:
                    self._log(
                        f"[{self.slot_id}] Method 0 OK: read open project "
                        f"{pid} from the tab URL — no API/click needed."
                    )
                    return pid
        except Exception as e:
            self._log(f"[{self.slot_id}] Method 0 exception: {str(e)[:120]}")

        # ── Method 1: List projects via tRPC (new endpoint, July 2026) ──
        # Google migrated Flow's project APIs to labs.google/fx/api/trpc.
        # The old aisandbox-pa endpoint (v1/projects) now returns 404.
        # New endpoint: project.searchUserProjects — tRPC-style GET with
        # url-encoded JSON input in the ?input= query param.
        try:
            input_json = (
                '{"json":{"pageSize":50,"toolName":"PINHOLE","cursor":null},'
                '"meta":{"values":{"cursor":["undefined"]}}}'
            )
            input_encoded = urllib.parse.quote(input_json, safe="")
            list_url = (
                "https://labs.google/fx/api/trpc/project.searchUserProjects"
                f"?input={input_encoded}"
            )
            async with _make_aiohttp_session() as session:
                async with session.get(
                    list_url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 401:
                        # A 401 here can mean the account never finished Flow
                        # onboarding (fresh Gmail) OR — far more common once an
                        # account has already generated — the cached Bearer
                        # token is stale / the account is momentarily reCAPTCHA-
                        # flagged. Previously we assumed "not onboarded" and
                        # gave up (skipped Methods 2-4), which stranded perfectly
                        # good onboarded accounts whose token had simply gone
                        # stale. DON'T give up: fall through to Method 3, which
                        # asks the EXTENSION to click "New project" using the
                        # browser's own logged-in session (no Bearer token
                        # needed) — that works for onboarded accounts even when
                        # this API 401s. A genuinely un-onboarded account will
                        # also fail Method 3, which is the correct outcome.
                        self._log(
                            f"[{self.slot_id}] Method 1 HTTP 401 (stale token or "
                            f"un-onboarded) — NOT giving up; trying the extension "
                            f"'New project' click (Method 3), which uses the "
                            f"browser session and works for onboarded accounts."
                        )
                    elif not resp.ok:
                        self._log(
                            f"[{self.slot_id}] Method 1 (searchUserProjects): "
                            f"HTTP {resp.status} — trying Method 2"
                        )
                    else:
                        data = await resp.json()
                        # Shape: result.data.json.result.projects[].projectId
                        projects = (
                            data.get("result", {})
                                .get("data", {})
                                .get("json", {})
                                .get("result", {})
                                .get("projects", [])
                        )
                        if not isinstance(projects, list) or not projects:
                            self._log(
                                f"[{self.slot_id}] Method 1: returned 0 projects "
                                f"— account has none, will create one via Method 2"
                            )
                        else:
                            candidates = [
                                str(p.get("projectId", "")) for p in projects
                            ]
                            valid = [c for c in candidates if c]
                            burned_count = sum(
                                1 for c in valid
                                if self._bridge.is_project_burned(self.account_email, c)
                            )
                            pid = _pick_unburned(candidates)
                            if pid:
                                self._log(
                                    f"[{self.slot_id}] Method 1 OK: {pid} "
                                    f"(of {len(valid)} listed, {burned_count} burned)"
                                )
                                return pid
                            self._log(
                                f"[{self.slot_id}] Method 1: all {len(valid)} "
                                f"listed project(s) in cooldown — trying Method 2"
                            )
        except Exception as e:
            self._log(f"[{self.slot_id}] Method 1 exception: {str(e)[:120]}")

        # ── Method 2: Create a fresh project via tRPC API (July 2026) ──
        # Direct POST to project.createProject. This replaces the fragile
        # extension button-click flow — creates a project server-side
        # without needing the tab to be on the dashboard or the "New
        # project" CTA to be clickable. Returns the new projectId directly.
        try:
            # Title is cosmetic; use a timestamp so multiple auto-created
            # projects on the same account remain distinguishable.
            title = time.strftime("Auto %b %d, %H:%M")
            create_body = {
                "json": {
                    "projectTitle": title,
                    "toolName": "PINHOLE",
                }
            }
            async with _make_aiohttp_session() as session:
                async with session.post(
                    "https://labs.google/fx/api/trpc/project.createProject",
                    headers={**headers, "Content-Type": "application/json"},
                    json=create_body,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if not resp.ok:
                        self._log(
                            f"[{self.slot_id}] Method 2 (createProject API): "
                            f"HTTP {resp.status} — trying Method 3 (extension)"
                        )
                    else:
                        data = await resp.json()
                        pid = (
                            data.get("result", {})
                                .get("data", {})
                                .get("json", {})
                                .get("result", {})
                                .get("projectId", "")
                        )
                        if pid:
                            self._log(
                                f"[{self.slot_id}] Method 2 OK: created fresh "
                                f"project {pid} via API (no button click needed)"
                            )
                            # Post it back to the bridge so other workers see it
                            self._bridge.set_project_id(self.account_email, pid)
                            return pid
                        self._log(
                            f"[{self.slot_id}] Method 2: response OK but no "
                            f"projectId in payload — trying Method 3"
                        )
        except Exception as e:
            self._log(f"[{self.slot_id}] Method 2 exception: {str(e)[:120]}")

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
        self._log(
            f"[{self.slot_id}] Method 3 (extension 'New project' click): "
            f"dispatching command, waiting up to 20s for new project ID..."
        )
        pre_existing = self._bridge.get_project_id(self.account_email) or ""
        self._bridge.send_command("new_project", self.account_email)
        for _ in range(20):
            await asyncio.sleep(1)
            current = self._bridge.get_project_id(self.account_email) or ""
            if current and current != pre_existing:
                self._log(f"[{self.slot_id}] Method 3 OK: {current}")
                return current

        after_new = self._bridge.get_project_id(self.account_email)
        if after_new:
            self._log(
                f"[{self.slot_id}] Method 3 late arrival: {after_new}"
            )
            return after_new
        self._log(
            f"[{self.slot_id}] Method 3 timed out — extension could not "
            f"create a new project (button not found, tab stuck in a "
            f"project URL, or rate-limit modal blocking click). Falling "
            f"through to Method 4."
        )

        # ── Method 4: Last-resort — oldest burned project ──
        # If Methods 1-3 all came up empty, fall back to the project whose
        # 429 attribution is most likely to have expired server-side. When
        # the user rotates IP externally (Surfshark, VPN cycle, etc.), a
        # burned project often works again on the fresh IP even though our
        # local burn timer hasn't run out. If it still returns 429, the
        # normal burn+rotate flow will just mark it again — no worse than
        # returning null and forcing a strike-cascade pause.
        oldest_burned = self._bridge.get_oldest_burned_project(self.account_email)
        if oldest_burned:
            self._log(
                f"[{self.slot_id}] Method 4 OK: retrying oldest burned "
                f"project {oldest_burned[:16]}… (IP may have changed; "
                f"server-side 429 attribution may have expired even "
                f"though local cooldown hasn't)."
            )
            return oldest_burned
        self._log(
            f"[{self.slot_id}] Method 4: no burned projects to retry — "
            f"account has NO projects at all. User must open "
            f"labs.google/fx/tools/flow and create one manually."
        )
        return None


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
        self.RECAPTCHA_HOLD_THRESHOLD = 3  # after this many consecutive failures, START recovery (not HOLD)
        # Auto-recovery tracking — mirrors the user's manual dance
        # (clear cache/history + reconnect VPN + reload Flow tab). We
        # try this AUTOMATICALLY before ever holding an account, and
        # only hold after MAX_RECOVERY_ATTEMPTS rounds all fail.
        self._recaptcha_recovery_attempts: Dict[str, int] = {}
        self.MAX_RECAPTCHA_RECOVERY_ATTEMPTS = 3
        self._recovery_in_progress: Dict[str, float] = {}  # account -> lock timestamp
        # Track last-success time per account so recovery can decide
        # whether to rotate the VPN (a global action that briefly
        # interrupts every account) or skip it because other accounts
        # are still generating fine — proving the IP is healthy and
        # the cascade is account-specific.
        self._account_last_success: Dict[str, float] = {}

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

        # Reset class-level asyncio locks. asyncio.Lock objects are bound to
        # the event loop that was current when they were created. When the
        # queue manager is stopped and restarted, a NEW event loop starts
        # but the class-level dicts still hold locks from the OLD loop —
        # every acquire then throws "bound to a different event loop" and
        # every job fails. Clearing here forces fresh locks in the current
        # loop on the next _resolve_project_id / _upload_reference call.
        ExtensionWorker._project_locks.clear()
        ExtensionWorker._reference_cache_locks.clear()

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

                # Open ONE Flow tab per slot so slots run in PARALLEL across tabs
                # (findLabsTab spreads each slot to the least-busy tab). For
                # UI-drive the extension drives each tab even in the background,
                # and auto-creates a project in any tab that opened on Flow home.
                if slots_per_account > 1:
                    try:
                        self._bridge.send_command(
                            "ensure_tabs", account_name, data=slots_per_account
                        )
                        self._log(
                            f"[ExtMode] Requested {slots_per_account} Flow tab(s) "
                            f"for {account_name} (1 per slot) — parallel across "
                            f"tabs; the extension drives them even in the background."
                        )
                    except Exception:
                        pass

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
            if self.qm.account_disabled.get(account_email):
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

    def _try_swap_image_model(self, job_id: str, account_email: str = ""):
        """Rotate a job's image model through Standard → Lite → Pro (or any
        starting model) when the current model hits a daily/per-model quota.
        Returns the new model name on success, or None if the account has
        exhausted every model.

        The three Flow image models have INDEPENDENT quotas on Google's side:
          - Nano Banana 2         → NARWHAL
          - Nano Banana 2 Lite    → HARBOR_SEAL
          - Nano Banana Pro       → GEM_PIX_2
        Google tracks quota PER ACCOUNT PER MODEL, so a Standard quota-out
        on Account A does not affect Account B, and Account B should keep
        using Standard until IT hits the limit.

        Tracking has two layers:
          * Per-JOB (_job_models_tried): stops a single job ping-ponging
            between models it's already tried this run.
          * Per-ACCOUNT (_account_exhausted_models): records which models
            have returned quota-exhausted for the account, with a 6h TTL
            (Google's per-model quota window). Peer accounts are NOT
            marked — they still use the model normally until their own
            quota hits.

        When every model is exhausted for the account, this method returns
        None and emits an "all 3 models exhausted, use a different account"
        warning; the calling flow HOLDs just that account, other accounts
        keep going.
        """
        # Lazy-init trackers
        if not hasattr(self, "_job_models_tried"):
            self._job_models_tried = {}
        if not hasattr(self, "_account_exhausted_models"):
            self._account_exhausted_models = {}  # account -> {model_lower: exhausted_at}

        EXHAUSTED_TTL_S = 6 * 60 * 60  # Google's per-model quota window (~6h)

        try:
            current = str(get_job_model(job_id) or "").strip()
        except Exception:
            return None
        if not current:
            return None

        # Classify the current model into a canonical bucket. Order matters:
        # "lite" before generic "nano" (Lite contains "nano banana"), and
        # "pro" before generic "nano".
        low = current.lower()
        if "lite" in low and "nano" in low:
            current_bucket = "lite"
        elif "pro" in low and "nano" in low:
            current_bucket = "pro"
        elif "nano" in low or "narwhal" in low:
            current_bucket = "standard"
        elif "imagen" in low:
            # Imagen-family currently resolves to NARWHAL server-side, so
            # treat it as the Standard bucket for rotation purposes.
            current_bucket = "standard"
        else:
            # Unknown starting model — can't safely rotate.
            return None

        bucket_to_name = {
            "standard": "Nano Banana 2.1",
            "lite": "Nano Banana 2 Lite",
            "pro": "Nano Banana Pro",
        }

        # Mark the current model as exhausted for THIS account only.
        # Peer accounts are not touched — they keep using this model
        # until their own quota fires.
        now = time.time()
        if account_email:
            acct_bucket = self._account_exhausted_models.setdefault(
                account_email, {}
            )
            # Clean expired entries (past 6h TTL) — Google's quota
            # windows roll over, so an old exhausted marker shouldn't
            # keep blocking the model forever.
            for m in list(acct_bucket.keys()):
                if now - acct_bucket[m] > EXHAUSTED_TTL_S:
                    acct_bucket.pop(m, None)
            acct_bucket[bucket_to_name[current_bucket].lower()] = now

        # Preferred rotation order per starting bucket. Same-family sibling
        # first, Pro (premium) last.
        rotation_map = {
            "standard": ["Nano Banana 2 Lite", "Nano Banana Pro"],
            "lite":     ["Nano Banana 2.1",    "Nano Banana Pro"],
            "pro":      ["Nano Banana 2.1",    "Nano Banana 2 Lite"],
        }
        candidates = rotation_map.get(current_bucket, [])

        # Per-JOB tracking (in-run ping-pong prevention)
        tried = self._job_models_tried.setdefault(job_id, set())
        tried.add(low)

        # Per-ACCOUNT tracking (persistent across jobs)
        acct_exhausted = (
            self._account_exhausted_models.get(account_email, {})
            if account_email else {}
        )

        # Pick the first candidate not tried by this job AND not
        # exhausted for this account.
        alternate = None
        for cand in candidates:
            cand_low = cand.lower()
            if cand_low in tried:
                continue
            if cand_low in acct_exhausted:
                continue
            alternate = cand
            break

        if alternate is None:
            # All 3 models exhausted for this account. Log the state
            # loudly and emit a user warning; the calling code will
            # HOLD this one account so healthy peers keep running.
            if account_email:
                self._log(
                    f"[ExtMode] ⛔ Account {account_email}: ALL 3 IMAGE "
                    f"MODELS have hit quota (Nano Banana 2.1, Nano Banana 2 "
                    f"Lite, Nano Banana Pro). Account is out for the current "
                    f"6-hour quota window. Use a different account; this "
                    f"one will become usable again automatically as Google's "
                    f"quota TTL expires."
                )
                try:
                    self.qm.signals.show_warning.emit(
                        f"Account '{account_email}' has exhausted ALL 3 image "
                        f"model quotas.\n\n"
                        f"• Nano Banana 2.1 — quota reached\n"
                        f"• Nano Banana 2 Lite — quota reached\n"
                        f"• Nano Banana Pro — quota reached\n\n"
                        f"Use a different account for now. Quotas will reset "
                        f"within ~6 hours on Google's side."
                    )
                except Exception:
                    pass
            return None

        tried.add(alternate.lower())
        try:
            update_job_model(job_id, alternate)
        except Exception:
            return None
        return alternate

    def _route_model_for_account(self, job_id: str, account_email: str,
                                 requested_model: str):
        """Proactive per-account image-model router — the READ side of the
        model-quota logic (the WRITE side is _try_swap_image_model).

        Called at job dispatch, BEFORE any API call. If this account has
        already hit a per-model quota on `requested_model` earlier this run
        (recorded in _account_exhausted_models within the 6h TTL), switch to
        the next live model in the responsive rotation order up-front — so the
        queue NEVER re-fires a model we already know is dead for this account,
        never wastes a reCAPTCHA token, and never 429s for nothing. Peer
        accounts are untouched (separate exhausted sets), so account B keeps
        using Nano Banana 2 while account A has already rotated off it.

        Returns:
          * requested_model unchanged — still live for this account
          * a new model name — requested one is dead but a sibling is live
            (the job's stored model is updated too, so logs/download match)
          * None — EVERY image model is exhausted for this account (caller
            should hold just this account so peers keep running)
        """
        if not account_email:
            return requested_model
        exhausted = getattr(self, "_account_exhausted_models", {}).get(
            account_email, {}
        )
        if not exhausted:
            return requested_model

        # Drop markers older than the 6h quota window so a model that has
        # since refilled on Google's side becomes usable again automatically.
        now = time.time()
        for m in list(exhausted.keys()):
            if now - exhausted[m] > 6 * 60 * 60:
                exhausted.pop(m, None)
        if not exhausted:
            return requested_model

        low = str(requested_model or "").lower()
        if "lite" in low and "nano" in low:
            bucket = "lite"
        elif "pro" in low and "nano" in low:
            bucket = "pro"
        elif "nano" in low or "narwhal" in low or "imagen" in low:
            bucket = "standard"
        else:
            # Unknown model — leave it alone.
            return requested_model

        bucket_to_name = {
            "standard": "Nano Banana 2.1",
            "lite": "Nano Banana 2 Lite",
            "pro": "Nano Banana Pro",
        }
        # Requested model still live for this account → use it as-is.
        if bucket_to_name[bucket].lower() not in exhausted:
            return requested_model

        # Requested model is a known-dead bucket for this account. Walk the
        # same responsive rotation order used by _try_swap_image_model and
        # pick the first sibling that is still live.
        rotation_map = {
            "standard": ["Nano Banana 2 Lite", "Nano Banana Pro"],
            "lite":     ["Nano Banana 2.1",    "Nano Banana Pro"],
            "pro":      ["Nano Banana 2.1",    "Nano Banana 2 Lite"],
        }
        for cand in rotation_map.get(bucket, []):
            if cand.lower() not in exhausted:
                try:
                    update_job_model(job_id, cand)
                except Exception:
                    pass
                self._log(
                    f"[ExtMode] {account_email}: '{requested_model}' quota "
                    f"already reached this run — routing this job to '{cand}' "
                    f"up-front (no wasted call on the dead model)."
                )
                return cand

        # Every model exhausted for this account.
        return None

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
                prompt_segments=_parse_prompt_segments(job.get("prompt_segments")),
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

        # Proactive per-account image-model routing (READ side; WRITE side is
        # _try_swap_image_model on failure). If this account already hit a
        # per-model quota on the job's model earlier this run, switch to the
        # next live model up-front — so we never waste a call (and a reCAPTCHA
        # token) re-firing a model we already know is dead for this account.
        # Video jobs use their own model and are skipped.
        if "video" not in job_type:
            _routed = self._route_model_for_account(
                job_id, worker.account_email, model
            )
            if _routed is None:
                # Every image model exhausted for this account — hold JUST this
                # account (peers keep running) and re-queue for a healthy one.
                # Fires the same banner + 6h auto-recovery as the failure path.
                if not self.qm.account_disabled.get(worker.account_email):
                    self.qm.account_disabled[worker.account_email] = True
                    try:
                        self.qm.account_hold_until[worker.account_email] = time.time() + 6 * 60 * 60
                        self.qm.account_hold_reason[worker.account_email] = "All image models hit quota (auto-resumes in ~6h)"
                    except Exception:
                        pass
                    self._log(
                        f"[{worker.slot_id}] ⛔ All image models already at quota "
                        f"for {worker.account_email} — holding this account, "
                        f"re-queuing job for a healthy account. Quotas reset ~6h."
                    )
                    try:
                        self.qm.signals.account_auth_status.emit(
                            worker.account_email, "quota_exhausted",
                            "All 3 image models exhausted"
                        )
                    except Exception:
                        pass
                    try:
                        from src.db.db_manager import reassign_account_jobs
                        reassign_account_jobs(worker.account_email)
                    except Exception:
                        pass
                update_job_status(job_id, "pending", account="")
                self.qm.signals.job_updated.emit(job_id, "pending", "", "")
                return
            model = _routed

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
                        prompt_segments=_parse_prompt_segments(job.get("prompt_segments")),
                    )

                if result and not error:
                    # The image is ALREADY generated on Google's side. If the
                    # DOWNLOAD fails, retry ONLY the download (reusing the same
                    # generation result) — NEVER loop back to generate_image(),
                    # because that burns quota and creates DUPLICATE images on
                    # Flow for a single prompt (the exact bug: one prompt got
                    # generated 3x because each download failure regenerated).
                    output_path = None
                    dl_error = None
                    for dl_attempt in range(4):
                        output_path, dl_error = await self._download_and_save(
                            worker, job_id, result, queue_no=queue_no,
                            access_token=worker.last_access_token,
                        )
                        if not dl_error:
                            break
                        # 403 = stale Bearer token / CDN throttle. Refresh the
                        # token and retry the DOWNLOAD of the already-generated
                        # image (NOT the generation).
                        if "403" in str(dl_error):
                            self._log(
                                f"[{worker.slot_id}] Download 403 (try "
                                f"{dl_attempt + 1}/4) — refreshing token, "
                                f"re-downloading the already-generated image."
                            )
                            worker.last_access_token = None
                            try:
                                _fresh = await self._bridge.request_token(
                                    worker.account_email, "IMAGE_GENERATION"
                                )
                                if _fresh and _fresh.get("access_token"):
                                    worker.last_access_token = _fresh["access_token"]
                            except Exception:
                                pass
                            if dl_attempt < 3:
                                await asyncio.sleep(15)
                        else:
                            self._log(
                                f"[{worker.slot_id}] Download failed (try "
                                f"{dl_attempt + 1}/4): {str(dl_error)[:150]} — "
                                f"retrying DOWNLOAD only (image already on Flow)."
                            )
                            if dl_attempt < 3:
                                await asyncio.sleep(5)
                    if dl_error:
                        # Every download retry failed. The image EXISTS on Flow
                        # but we couldn't fetch it. Fail WITHOUT regenerating —
                        # regenerating would waste quota and duplicate the image.
                        # The user can recover it from the Flow project gallery.
                        last_error = dl_error
                        self._log(
                            f"[{worker.slot_id}] Download failed after 4 tries "
                            f"({str(dl_error)[:120]}). Image is on Flow but not "
                            f"saved locally — NOT regenerating (avoids wasting "
                            f"quota + duplicates)."
                        )
                        update_job_status(
                            job_id, "failed", account=worker.account_email,
                            error=f"generated_but_download_failed: {str(dl_error)[:140]}",
                        )
                        self.qm.signals.job_updated.emit(
                            job_id, "failed", worker.account_email,
                            "Generated on Flow, download failed (not regenerated)",
                        )
                        return
                    else:
                        update_job_status(job_id, "completed", account=worker.account_email)
                        self.qm.signals.job_updated.emit(job_id, "completed", worker.account_email, "")
                        self.qm._record_throttle_success(worker.account_email)
                        # Reset reCAPTCHA streak on success
                        self._recaptcha_streak.pop(worker.account_email, None)
                        # Reset the recovery-attempt counter too — a
                        # successful generation proves the recovery
                        # worked, so the next cascade gets a fresh
                        # MAX_RECAPTCHA_RECOVERY_ATTEMPTS budget instead
                        # of jumping straight to HOLD.
                        self._recaptcha_recovery_attempts.pop(worker.account_email, None)
                        # Record last-success timestamp — a peer account's
                        # recovery uses this to decide whether the shared
                        # VPN IP is still healthy (skip rotation) or if
                        # everyone is failing (rotation warranted).
                        self._account_last_success[worker.account_email] = time.time()
                        # A successful generation proves the model this
                        # job used is NOT exhausted for this account —
                        # clear the exhausted marker so future jobs on
                        # this account can pick it again if the rotator
                        # brought us back to it after a 6h TTL wrap.
                        try:
                            used_model = str(get_job_model(job_id) or "").lower().strip()
                            if used_model and hasattr(self, "_account_exhausted_models"):
                                self._account_exhausted_models.get(
                                    worker.account_email, {}
                                ).pop(used_model, None)
                        except Exception:
                            pass
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
                # A 429 that is actually a reCAPTCHA reputation failure (Google
                # returns HTTP 429 with "reCAPTCHA evaluation failed" in the
                # body) must NOT go down the pause cascade below — pausing the
                # account 5min→15min→1h does nothing for reCAPTCHA reputation.
                # Route it to the reCAPTCHA-recovery flow instead (cache clean →
                # _GRECAPTCHA cookie drop → smart VPN rotation → tab reload),
                # which is the only path that actually clears the flag.
                # EXCEPTION: a per-model quota-exhaustion 429 still belongs in
                # the 429 block so the model-swap recovery can run.
                _is_recaptcha_429 = (
                    ("recaptcha" in err_lower
                     or "captcha" in err_lower
                     or "evaluation failed" in err_lower)
                    and not any(q in err_lower for q in (
                        "exhausted", "check quota", "daily limit", "different model"
                    ))
                )
                if (
                    any(p in err_lower for p in ("429", "rate limit", "too many requests"))
                    and not _is_recaptcha_429
                ):
                    # Recovery strategy 1 — Cache cleanup. Some 429s "stick"
                    # in client-side state (IndexedDB, service worker cache).
                    # Manually clearing browser data on labs.google often
                    # gets the account working again immediately. Fire-and-
                    # forget so the work item isn't blocked by it.
                    try:
                        self._bridge.send_command("clean_tracking", worker.account_email)
                        self._log(
                            f"[{worker.slot_id}] 🧹 Sent clean_tracking to "
                            f"refresh labs.google session state — sometimes "
                            f"clears the rate-limit without needing the pause."
                        )
                    except Exception:
                        pass

                    # Recovery strategy 2 — Model fallback on per-model
                    # quota exhaustion. Daily quota is PER model on the
                    # Flow side too: Nano Banana (NARWHAL) and Nano Banana
                    # Pro (GEM_PIX_2) have separate quotas. If the user has
                    # access to both, swapping models lets the queue keep
                    # moving on the alternate model instead of pausing the
                    # whole account.
                    quota_exhausted = (
                        "exhausted" in err_lower
                        or "check quota" in err_lower
                        or "daily limit" in err_lower
                        or "different model" in err_lower
                    )
                    if quota_exhausted:
                        swapped_to = self._try_swap_image_model(
                            job_id, worker.account_email
                        )
                        if swapped_to:
                            self._log(
                                f"[{worker.slot_id}] 🔄 Quota exhausted on "
                                f"current model — swapped to '{swapped_to}' "
                                f"and re-queuing. Account NOT paused; other "
                                f"workers can keep using the same account on "
                                f"the alternate model."
                            )
                            update_job_status(job_id, "pending", account="")
                            self.qm.signals.job_updated.emit(
                                job_id, "pending", "", ""
                            )
                            # IMPORTANT: skip the pause cascade below —
                            # the swap path is the recovery for this job.
                            return
                        else:
                            # _try_swap_image_model returned None → this
                            # account has exhausted every model. Hold JUST
                            # this account (peers keep running on their
                            # own quotas) and reassign pending jobs to
                            # accounts that still have a working model.
                            already_held = self.qm.account_disabled.get(
                                worker.account_email, False
                            )
                            self.qm.account_disabled[worker.account_email] = True
                            # Auto-recover after ~6h (Google's per-model quota
                            # window) so the account re-enables ITSELF once its
                            # quota resets — matches the 6h TTL on the per-model
                            # exhausted markers. _check_account_holds() clears
                            # account_disabled + emits logged_in when this fires,
                            # which also clears the "Quota Exhausted" banner.
                            try:
                                self.qm.account_hold_until[worker.account_email] = time.time() + 6 * 60 * 60
                                self.qm.account_hold_reason[worker.account_email] = "All image models hit quota (auto-resumes in ~6h)"
                            except Exception:
                                pass
                            if not already_held:
                                self._log(
                                    f"[ExtMode] ⛔ All 3 image models exhausted "
                                    f"for {worker.account_email} — holding this "
                                    f"account only. Peer accounts continue "
                                    f"normally. Quotas auto-reset within ~6h."
                                )
                                try:
                                    self.qm.signals.account_auth_status.emit(
                                        worker.account_email, "quota_exhausted",
                                        "All 3 image models exhausted"
                                    )
                                except Exception:
                                    pass
                                try:
                                    from src.db.db_manager import reassign_account_jobs
                                    count = reassign_account_jobs(worker.account_email)
                                    if count > 0:
                                        self._log(
                                            f"[ExtMode] Reassigned {count} job(s) "
                                            f"from {worker.account_email} to accounts "
                                            f"that still have working models."
                                        )
                                except Exception:
                                    pass
                            update_job_status(job_id, "pending", account="")
                            self.qm.signals.job_updated.emit(
                                job_id, "pending", "", ""
                            )
                            return

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

                # reCAPTCHA score/token failure → track streak, trigger
                # auto-recovery on cascade (mirrors user's manual flow of
                # clear cache + reconnect VPN + reload Flow + retry).
                # Only HOLD after MAX_RECAPTCHA_RECOVERY_ATTEMPTS rounds all fail.
                if (
                    "recaptcha" in err_lower
                    or "captcha" in err_lower
                    or "evaluation failed" in err_lower
                ):
                    streak = self._recaptcha_streak.get(worker.account_email, 0) + 1
                    self._recaptcha_streak[worker.account_email] = streak
                    self._log(
                        f"[{worker.slot_id}] reCAPTCHA failure #{streak} for {worker.account_email}"
                    )

                    if streak >= self.RECAPTCHA_HOLD_THRESHOLD:
                        recovery_count = self._recaptcha_recovery_attempts.get(
                            worker.account_email, 0
                        )
                        # Give up only if we've already tried recovery
                        # MAX_RECAPTCHA_RECOVERY_ATTEMPTS times and reCAPTCHA
                        # is STILL failing on that account.
                        if recovery_count >= self.MAX_RECAPTCHA_RECOVERY_ATTEMPTS:
                            already_held = self.qm.account_disabled.get(worker.account_email, False)
                            self.qm.account_disabled[worker.account_email] = True
                            try:
                                self._bridge.hold_ecosystem_account(
                                    worker.account_email, duration_seconds=172800
                                )
                            except Exception:
                                pass
                            if not already_held:
                                self._log(
                                    f"[ExtMode] ⛔ Account {worker.account_email} exhausted "
                                    f"{recovery_count} recovery attempts — HOLDING account. "
                                    f"reCAPTCHA reputation not recovering; account needs "
                                    f"24-48h rest."
                                )
                                self.qm.signals.account_auth_status.emit(
                                    worker.account_email, "expired",
                                    f"reCAPTCHA flagged (after {recovery_count} recoveries)"
                                )
                                self.qm.signals.show_warning.emit(
                                    f"Account '{worker.account_email}' failed to recover after "
                                    f"{recovery_count} auto-recovery attempts.\n"
                                    f"Google's reCAPTCHA reputation is not improving.\n"
                                    f"Rest this account 24-48h or use a different one."
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

                        # ── AUTO-RECOVERY (mirrors user's manual flow) ──
                        # Only one worker per account runs the recovery
                        # sequence at a time; other workers wait via the
                        # streak-check on the next iteration.
                        now = time.time()
                        lock_ts = self._recovery_in_progress.get(worker.account_email, 0)
                        if now - lock_ts < 60:
                            # Another worker started recovery <60s ago —
                            # just wait for it, then retry.
                            self._log(
                                f"[{worker.slot_id}] Recovery already in progress for "
                                f"{worker.account_email} — waiting 15s then retrying."
                            )
                            await asyncio.sleep(15)
                            if self.qm.stop_requested or self.qm.force_stop_requested:
                                update_job_status(job_id, "pending", account="")
                                self.qm.signals.job_updated.emit(job_id, "pending", "", "")
                                return
                            continue

                        self._recovery_in_progress[worker.account_email] = now
                        self._recaptcha_recovery_attempts[worker.account_email] = recovery_count + 1
                        self._log(
                            f"[ExtMode] 🔄 Auto-recovery #{recovery_count + 1}/"
                            f"{self.MAX_RECAPTCHA_RECOVERY_ATTEMPTS} for "
                            f"{worker.account_email} — starting sequence "
                            f"(clean cache → clean _GRECAPTCHA → reload tab → wait)."
                        )
                        try:
                            # Step 1: Clear IndexedDB / cache / SW for labs.google
                            # (equivalent to user's "clear browsing history").
                            self._bridge.send_command("clean_tracking", worker.account_email)
                            await asyncio.sleep(3)

                            # Step 2: Delete _GRECAPTCHA cookie so Google mints
                            # a fresh reCAPTCHA client on the next page load.
                            self._bridge.send_command(
                                "clean_recaptcha_cookie", worker.account_email
                            )
                            await asyncio.sleep(2)

                            # Step 3a: SMART VPN rotation — only trigger the
                            # Surfshark restart if peer accounts on the same
                            # PC are ALSO failing. If someone else generated
                            # successfully in the last 60 seconds, the
                            # shared IP is fine and this cascade is
                            # reputation-specific to the one account; a
                            # global adapter restart would just interrupt
                            # the healthy accounts for no benefit.
                            #
                            # EXCEPTION: on the LAST recovery attempt (i.e.
                            # attempts 1-2 already failed with cache clear
                            # alone), force the VPN rotation regardless of
                            # peer health. Two rounds of soft recovery
                            # didn't help — the account will be HELD if
                            # this one fails too, so it's worth the brief
                            # peer disruption to try the one thing that
                            # historically WORKS in this state (IP change).
                            peer_recent_success = False
                            now_check = time.time()
                            for peer_email, last_ok in self._account_last_success.items():
                                if peer_email == worker.account_email:
                                    continue
                                if now_check - last_ok < 60:
                                    peer_recent_success = True
                                    break

                            is_final_attempt = (
                                recovery_count + 1 >= self.MAX_RECAPTCHA_RECOVERY_ATTEMPTS
                            )

                            if peer_recent_success and not is_final_attempt:
                                self._log(
                                    f"[ExtMode] 🌐 Skipping VPN rotation — a peer "
                                    f"account generated successfully in the last "
                                    f"60s, so the shared IP is healthy. This "
                                    f"cascade is account-specific; cache clear + "
                                    f"reCAPTCHA cookie drop + tab reload will "
                                    f"handle it without interrupting the peers. "
                                    f"(VPN will be forced on the final recovery "
                                    f"attempt if this one doesn't succeed.)"
                                )
                            else:
                                if peer_recent_success and is_final_attempt:
                                    self._log(
                                        f"[ExtMode] 🌐 FINAL recovery attempt "
                                        f"({recovery_count + 1}/"
                                        f"{self.MAX_RECAPTCHA_RECOVERY_ATTEMPTS}) — "
                                        f"forcing VPN rotation even though a peer "
                                        f"account is healthy. Previous 2 attempts "
                                        f"didn't fix the reputation with cache "
                                        f"clear alone; brief peer interruption is "
                                        f"worth the chance of a fresh IP unlocking "
                                        f"this account before HOLD."
                                    )
                                # VPN rotation DISABLED. Restarting the network
                                # adapter mid-run tore down in-flight generation
                                # and download requests ("Failed to fetch"), and
                                # Surfshark's datacenter IPs don't pass Flow's
                                # reCAPTCHA anyway. No trigger file is written, so
                                # the Surfshark-Rotate script never restarts the
                                # adapter — the network stays stable and downloads
                                # complete in one shot.
                                self._log(
                                    f"[ExtMode] VPN rotation skipped (disabled to "
                                    f"keep the network stable). Recovery uses cache "
                                    f"+ reCAPTCHA cookie clear + tab reload only."
                                )

                            # Step 3a-2: PROJECT rotation. A reCAPTCHA cascade
                            # often rides along with a Flow project that Google
                            # has already started 429-ing. Burn the account's
                            # current project so the retry AFTER this recovery
                            # resolves a FRESH one (via the normal Methods 1-4
                            # in _resolve_project_id — same proven path the
                            # inline 429-burn uses). This is a cache-pointer
                            # drop only — no tab navigation here, so it cannot
                            # race the reload below. The actual switch lands on
                            # the next attempt, giving a clean fresh-IP +
                            # fresh-cache + fresh-project combination.
                            try:
                                current_pid = self._bridge.get_project_id(
                                    worker.account_email
                                )
                                if current_pid:
                                    self._bridge.burn_project(
                                        worker.account_email, current_pid
                                    )
                                    self._log(
                                        f"[ExtMode] 🗂 Burned project "
                                        f"{current_pid} for {worker.account_email} "
                                        f"— retry will resolve a fresh project on "
                                        f"top of the IP + cache reset."
                                    )
                                else:
                                    self._log(
                                        f"[ExtMode] 🗂 No live cached project for "
                                        f"{worker.account_email} — retry will "
                                        f"resolve a fresh one anyway."
                                    )
                            except Exception as _pe:
                                self._log(
                                    f"[ExtMode] Project burn during recovery "
                                    f"failed ({str(_pe)[:80]}) — recovery still "
                                    f"proceeds."
                                )

                            # Step 3b: Reload the Flow tab. Fresh page = fresh
                            # reCAPTCHA context, and the VPN trigger above
                            # should have handed us a new IP by the time
                            # the tab finishes loading.
                            self._bridge.send_command("reload_tab", worker.account_email)

                            # Step 4: Wait for the tab reload + reCAPTCHA to settle.
                            # Longer wait than the 5s the old code used because
                            # reCAPTCHA needs a bit of user-simulated activity
                            # before its score recovers.
                            await asyncio.sleep(45)

                            # Step 5: Reset the streak so this account gets a
                            # fresh chance. Also record success at the
                            # queue-manager level so throttles unwind.
                            self._recaptcha_streak.pop(worker.account_email, None)
                            self._log(
                                f"[ExtMode] ✓ Auto-recovery #{recovery_count + 1} "
                                f"complete for {worker.account_email}. Streak reset; "
                                f"account back in rotation."
                            )
                        finally:
                            self._recovery_in_progress.pop(worker.account_email, None)

                        # Re-queue this job so it retries with the fresh state.
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
                    # For a plain IMAGE job WITHOUT references, a network blip
                    # is worth retrying: a duplicate image is cheap, and losing
                    # the (possibly already-generated) image just wastes quota
                    # with nothing saved locally — exactly the "generation hoti
                    # hai, download fail, image lost" complaint. Only VIDEO or
                    # reference-based jobs stay fail-fast, because a duplicate
                    # video / duplicate reference upload is expensive.
                    _has_refs = bool(
                        job.get("reference_media_ids")
                        or str(job.get("ref_path") or "").strip()
                        or str(job.get("ref_paths") or "").strip()
                    )
                    if job_type == "image" and not _has_refs and attempt < max_retries:
                        self._log(
                            f"[{worker.slot_id}] Network blip on image job "
                            f"({last_error[:80]}) — retrying (duplicate image is "
                            f"cheap; better than losing the generation)."
                        )
                        await asyncio.sleep(3)
                        continue
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
        # Historical shapes we've seen from Google:
        #   media[].image.generatedImage.fifeUrl    (original)
        #   media[].image.imageUrl                  (new, mid-2026)
        #   media[].image.url                       (some variants)
        #   Plus name-based fallback via backbone.redirect
        if not fife_url:
            media_list = api_data.get("media", []) if isinstance(api_data, dict) else []
            media_name = None
            for item in media_list:
                if not isinstance(item, dict):
                    continue
                img = item.get("image", {}) if isinstance(item.get("image"), dict) else {}
                gen = img.get("generatedImage", {}) if isinstance(img.get("generatedImage"), dict) else {}
                # Try every known field name, in order of preference
                url = (
                    gen.get("fifeUrl", "")
                    or gen.get("imageUrl", "")
                    or gen.get("url", "")
                    or img.get("fifeUrl", "")
                    or img.get("imageUrl", "")
                    or img.get("url", "")
                    or item.get("fifeUrl", "")
                )
                name = item.get("name", "") or gen.get("name", "") or img.get("name", "")
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
            # Log the api_data shape so we can figure out what field name
            # Google is actually returning — the code above tries every
            # historical name we know but the response format changes.
            try:
                shape = json.dumps(api_data, indent=2, default=str)[:1500]
            except Exception:
                shape = str(api_data)[:1500]
            self._log(
                f"[{worker.slot_id}] No downloadable media URL in response — "
                f"api_data shape (first 1500 chars):\n{shape}"
            )
            return None, "No downloadable media in API response"

        # Log the resolved URL so we can see what's actually being fetched
        # when 403s hit — useful for spotting when Google changes the
        # CDN host or requires a new signature scheme.
        self._log(
            f"[{worker.slot_id}] Download URL resolved: {fife_url[:150]}"
            + ("…" if len(fife_url) > 150 else "")
        )

        dl_headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
            "referer": "https://labs.google/",
        }
        # Google migrated the image CDN to flow-content.google (July 2026)
        # and switched to signed URLs — the `Signature=…` query parameter
        # is the auth. Any Authorization header or cookie we attach makes
        # the CDN reject the request as "unexpected auth alongside signed
        # URL", which is exactly the 403 the user just hit. Only attach
        # Bearer / cookies for the OLD CDN hosts that expected them.
        is_signed_url = "Signature=" in fife_url or "flow-content.google" in fife_url
        if access_token and not is_signed_url:
            dl_headers["authorization"] = f"Bearer {access_token}"

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

        # ── IMAGE: download with browser cookies + fall back to extension ──
        # Google's fifeUrl / backbone.redirect endpoints require an
        # authenticated Google session cookie (mid-2026 change). The
        # Bearer token alone is not sufficient — the CDN validates the
        # SAPISID / __Secure-3PSID cookies too. We follow the same 2-tier
        # pattern the video path uses:
        #   Tier 1: fetch cookies via extension, do the fetch ourselves
        #   Tier 2: ask the extension to download via chrome.fetch (it
        #           runs inside the tab so cookies attach automatically)
        try:
            # Tier 1 — direct aiohttp. Only attach cookies for legacy CDN
            # hosts that expect a Google session; signed flow-content.google
            # URLs must be requested WITHOUT any auth headers so the CDN
            # accepts the Signature= param as the sole authorisation.
            if is_signed_url:
                headers_with_cookies = dict(dl_headers)
            else:
                try:
                    cookie_result = await worker._bridge.request_token(
                        worker.account_email, "GET_COOKIES", timeout=10.0,
                    )
                    cookie_str = str(cookie_result.get("cookies", "") or "")
                except Exception:
                    cookie_str = ""
                headers_with_cookies = dict(dl_headers)
                if cookie_str:
                    headers_with_cookies["cookie"] = cookie_str

            async with _make_aiohttp_session() as session:
                async with session.get(
                    fife_url,
                    headers=headers_with_cookies,
                    timeout=aiohttp.ClientTimeout(total=120),
                    allow_redirects=True,
                ) as resp:
                    if resp.ok:
                        content_type = str(resp.headers.get("content-type", "")).lower()
                        data = await resp.read()
                        if data:
                            return await self._save_media(
                                job_id, data, content_type, queue_no,
                                slot_id=worker.slot_id,
                            )
                    tier1_status = resp.status
                    # On non-2xx, capture the response body and a few
                    # diagnostic headers so we can see WHY Google's CDN
                    # rejected the request. Signed-URL 403s usually
                    # include an XML error body or a header like
                    # x-goog-error / server=UploadServer with hints
                    # about signature mismatch, expired timestamp, or
                    # IP-based throttling.
                    try:
                        body_preview = (await resp.text())[:500]
                    except Exception:
                        body_preview = "<body-read-failed>"
                    diag_headers = {
                        k: v for k, v in resp.headers.items()
                        if k.lower() in (
                            "server", "x-goog-error", "x-guploader-uploadid",
                            "www-authenticate", "content-type", "content-length",
                            "x-content-type-options", "x-frame-options",
                        )
                    }
                    now_ts = int(time.time())
                    # Extract Expires= from the URL so we can see if the
                    # signature has expired vs current time.
                    exp_match = None
                    try:
                        import re as _re
                        m = _re.search(r"Expires=(\d+)", fife_url)
                        if m:
                            exp_match = int(m.group(1))
                    except Exception:
                        pass
                    exp_note = ""
                    if exp_match:
                        delta = exp_match - now_ts
                        exp_note = (
                            f", Expires={exp_match} (now={now_ts}, "
                            f"{'expired' if delta < 0 else f'valid for {delta}s'})"
                        )
                    self._log(
                        f"[{worker.slot_id}] Tier-1 {tier1_status} — "
                        f"headers={diag_headers}{exp_note} — "
                        f"body[:500]={body_preview!r}"
                    )
        except Exception as e:
            tier1_status = f"exception: {str(e)[:80]}"

        # Tier 2 — extension-side download (fetches from inside the labs
        # tab so session cookies attach automatically). Slower than Tier 1
        # but reliable when Google's CDN 403s the aiohttp request.
        try:
            self._log(
                f"[{worker.slot_id}] Tier-1 download failed ({tier1_status}); "
                f"falling back to extension-side fetch."
            )
            bridge_result = await worker._bridge.request_token(
                worker.account_email,
                f"FETCH_MEDIA_BYTES:{fife_url}",
                timeout=60.0,
            )
            err = bridge_result.get("error", "")
            if err:
                return None, f"Download HTTP {tier1_status} (bridge: {err})"

            # Extension either hands us the raw bytes or a CDN URL that
            # will accept a follow-up unauthenticated request.
            b64 = bridge_result.get("data_b64", "")
            if b64:
                import base64 as _b64
                data = _b64.b64decode(b64)
                content_type = str(bridge_result.get("content_type", "image/jpeg"))
                return await self._save_media(
                    job_id, data, content_type, queue_no,
                    slot_id=worker.slot_id,
                )

            cdn_url = bridge_result.get("cdn_url", "")
            if cdn_url:
                async with _make_aiohttp_session() as session:
                    async with session.get(
                        cdn_url,
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
                return await self._save_media(
                    job_id, data, content_type, queue_no,
                    slot_id=worker.slot_id,
                )

            return None, f"Download HTTP {tier1_status} (bridge returned nothing)"
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

        # Post-process: strip Google Flow's visible sparkle watermark
        # from images when the toggle is on. Uses OpenCV inpainting
        # first (cross-platform, no ffmpeg needed on macOS/Windows),
        # then falls back to ffmpeg delogo. Video files (.mp4/.webm)
        # are left alone — the sparkle isn't burned into video frames
        # the same way and would need a stream filter path.
        if ext in (".png", ".jpg", ".jpeg", ".webp"):
            try:
                from src.db.db_manager import get_setting
                wm_enabled = str(
                    get_setting("flow_remove_watermark", "1") or "1"
                ).strip().lower() in ("1", "true", "on", "yes")
            except Exception:
                wm_enabled = True  # Default ON — matches UI toggle default.
            if wm_enabled:
                try:
                    from src.core.watermark_remover import remove_flow_watermark
                    # Do the inpaint on a worker thread — OpenCV holds the
                    # GIL during the C++ call and inpaint takes 50–200 ms
                    # on Flow-sized images, which would visibly stall the
                    # async event loop otherwise.
                    ok = await asyncio.to_thread(remove_flow_watermark, output_path)
                    if not ok:
                        # Silent no-op — image stays as-is. Not fatal; the
                        # watermark is a nice-to-remove, not blocking.
                        pass
                except Exception:
                    pass

        try:
            update_job_runtime_state(job_id, output_path=output_path)
        except Exception:
            pass

        self._log(f"[{slot_id}] Saved: {filename} ({len(data)} bytes)")
        return output_path, None
