"""
Genspark Bridge Server — local HTTP bridge for Chrome Extension <-> Python.

Architecture (mirrors extension_bridge.py but for genspark.ai):
  Python App <-> Bridge (localhost:18925) <-> Chrome Extension

Differences from Flow bridge (extension_bridge.py):
  - No Bearer token dance — Genspark uses cookies only
  - Unlimited generation on Plus/Pro plans (no per-day quota dance)
  - Session-based rate limits (5-hour windows) instead of daily
  - SSE parsing needed for /api/agent/ask_proxy response
  - JSON polling via /api/spark/image_generation_task_detail
  - Recaptcha Enterprise site key: 6LfYyWcsAAAAAK8DUr6Oo1wHl2CJ5kKbO0AK3LIM

Endpoints (served to extension):
  GET  /genspark/poll          — extension polls for pending work
  POST /genspark/work-result   — extension sends generation result back
  POST /genspark/accounts      — extension reports logged-in Genspark accounts
  GET  /genspark/status        — app checks bridge status
"""

import asyncio
import base64
import io
import json
import logging
import mimetypes
import os
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from aiohttp import web

# ─── Reference-image cache (path → base64-encoded data URL payload) ───
# Pick-once-reuse-forever: app reads & encodes each ref file ONCE, then keeps
# the base64 payload in memory keyed by (path, mtime, size). Subsequent jobs
# that point at the same file reuse the cached payload without disk I/O or
# re-encoding. Cache is reset on app restart.
_REF_CACHE: Dict[str, Dict[str, Any]] = {}
_REF_CACHE_MAX_ITEMS = 32              # ~32 refs * ~3MB each = ~100MB max RAM
_REF_MAX_BYTES_BEFORE_RESIZE = 2 * 1024 * 1024   # 2MB → resize to fit Genspark body
_REF_RESIZE_MAX_DIM = 1536             # px — keeps quality good, body < 3MB after b64

def _encode_ref_file(path: str) -> Optional[Dict[str, str]]:
    """Read a reference image from disk, return {mime, data_url, b64}.

    Auto-resizes images >2MB to fit Genspark's ~5MB body cap. Uses an
    (path, mtime, size) keyed in-memory cache so the same file is encoded
    only once per app session — multiple jobs reusing the same reference
    image pay zero disk + zero CPU cost after the first read.
    """
    try:
        if not path or not os.path.exists(path):
            return None
        st = os.stat(path)
        cache_key = f"{path}|{st.st_mtime_ns}|{st.st_size}"
        hit = _REF_CACHE.get(cache_key)
        if hit:
            return hit

        mime = mimetypes.guess_type(path)[0] or "image/png"
        if mime not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
            mime = "image/png"

        with open(path, "rb") as fh:
            raw = fh.read()

        # Resize if too large — keeps the multipart payload reasonable
        if len(raw) > _REF_MAX_BYTES_BEFORE_RESIZE:
            try:
                from PIL import Image
                img = Image.open(io.BytesIO(raw))
                img.thumbnail((_REF_RESIZE_MAX_DIM, _REF_RESIZE_MAX_DIM), Image.LANCZOS)
                out = io.BytesIO()
                save_fmt = "JPEG" if mime == "image/jpeg" else "PNG"
                if save_fmt == "JPEG" and img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                img.save(out, format=save_fmt, quality=85, optimize=True)
                raw = out.getvalue()
                mime = "image/jpeg" if save_fmt == "JPEG" else "image/png"
            except Exception:
                # PIL not available — send original anyway; Genspark might reject it
                pass

        b64 = base64.b64encode(raw).decode("ascii")
        data_url = f"data:{mime};base64,{b64}"
        entry = {"mime": mime, "data_url": data_url, "b64": b64,
                 "size_bytes": str(len(raw))}

        # Evict oldest if cache full (simple FIFO)
        if len(_REF_CACHE) >= _REF_CACHE_MAX_ITEMS:
            try:
                _REF_CACHE.pop(next(iter(_REF_CACHE)))
            except StopIteration:
                pass
        _REF_CACHE[cache_key] = entry
        return entry
    except Exception:
        return None

log = logging.getLogger(__name__)

GENSPARK_BRIDGE_PORT = 18925
GENSPARK_BRIDGE_HOST = "127.0.0.1"

# Genspark's reCAPTCHA Enterprise site key (observed from production traffic)
GENSPARK_RECAPTCHA_SITE_KEY = "6LfYyWcsAAAAAK8DUr6Oo1wHl2CJ5kKbO0AK3LIM"


class GensparkBridge:
    """Local HTTP bridge between Python app and Chrome Extension — Genspark mode.

    Runs on a SEPARATE port (18925) from the Flow bridge (18924) so both can
    coexist if a user switches between modes without restarting the app.
    """

    def __init__(self, log_fn: Optional[Callable[[str], None]] = None):
        self._log: Callable[[str], None] = log_fn or (lambda msg: None)
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None

        # Pending generation requests: request_id -> { prompt, model_params,
        # future, created_at, account, ... }
        self._pending_requests: Dict[str, Dict[str, Any]] = {}
        self._request_counter = 0

        # Connected accounts reported by extension.
        # Shape: { email: {email, plan_type, tab_id, last_seen} }
        self._connected_accounts: Dict[str, Dict[str, Any]] = {}

        # Track which extension instance picked up which request.
        # Shape: request_id -> { ext_key: frozenset, ts: dispatch_timestamp }
        # Once a request is dispatched we stop re-dispatching it for
        # DISPATCH_LOCK_SECONDS — the extension is processing it in parallel,
        # we don't want to hand out the same work twice.
        self._dispatched_to: Dict[str, Dict[str, Any]] = {}
        self.DISPATCH_LOCK_SECONDS = 300  # 5 min; longer than typical generation

        # Stats
        self._images_generated = 0
        self._extension_last_seen = 0.0

    # ═══════════════════════════════════════════════════════════════
    # Lifecycle
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    @web.middleware
    async def _cors_middleware(request, handler):
        """CORS + Private Network Access headers for Chrome 130+ (PNA)."""
        cors_headers = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Requested-With",
            "Access-Control-Allow-Private-Network": "true",
            "Access-Control-Max-Age": "86400",
        }
        if request.method == "OPTIONS":
            return web.Response(status=204, headers=cors_headers)
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            for k, v in cors_headers.items():
                exc.headers[k] = v
            raise
        for k, v in cors_headers.items():
            response.headers[k] = v
        return response

    async def start(self) -> None:
        """Start the Genspark bridge HTTP server."""
        # Generated images are 2-5 MB base64-encoded. Default aiohttp limit
        # is 1 MB which silently 413s the submit_result POST. Bump to 50 MB
        # so we can comfortably handle 4K image base64 payloads.
        self._app = web.Application(
            client_max_size=50 * 1024 * 1024,
            middlewares=[self._cors_middleware],
        )
        self._app.router.add_route(
            "OPTIONS", "/{tail:.*}", lambda r: web.Response(status=204)
        )
        self._app.router.add_get("/genspark/poll", self._handle_poll)
        self._app.router.add_post("/genspark/work-result", self._handle_work_result)
        self._app.router.add_post("/genspark/accounts", self._handle_accounts)
        self._app.router.add_get("/genspark/status", self._handle_status)
        self._app.router.add_post("/genspark/progress", self._handle_progress)

        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()

        try:
            self._site = web.TCPSite(self._runner, GENSPARK_BRIDGE_HOST, GENSPARK_BRIDGE_PORT)
            await self._site.start()
            self._log(
                f"[GensparkBridge] Started on "
                f"http://{GENSPARK_BRIDGE_HOST}:{GENSPARK_BRIDGE_PORT}"
            )
        except OSError as e:
            self._log(f"[GensparkBridge] Port {GENSPARK_BRIDGE_PORT} busy: {e}. Trying +1...")
            self._site = web.TCPSite(self._runner, GENSPARK_BRIDGE_HOST, GENSPARK_BRIDGE_PORT + 1)
            await self._site.start()
            self._log(
                f"[GensparkBridge] Started on "
                f"http://{GENSPARK_BRIDGE_HOST}:{GENSPARK_BRIDGE_PORT + 1}"
            )

    async def stop(self) -> None:
        """Stop the bridge server and fail any pending futures."""
        for req_id, req in list(self._pending_requests.items()):
            fut = req.get("future")
            if fut and not fut.done():
                fut.set_result({"error": "bridge_stopped"})
        self._pending_requests.clear()
        self._dispatched_to.clear()

        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()
        self._log("[GensparkBridge] Stopped.")

    # ═══════════════════════════════════════════════════════════════
    # Public API — called by GensparkModeManager
    # ═══════════════════════════════════════════════════════════════

    async def generate_image(
        self,
        account: str,
        prompt: str,
        model: str = "nano-banana-2",
        aspect_ratio: str = "auto",
        style: str = "auto",
        image_size: str = "auto",
        # auto_prompt sends the user's prompt through Genspark's LLM agent
        # (ask_proxy) which rewrites/enriches it before generation. That
        # endpoint rate-limits hard (~10 req per 25s) and its SSE stream
        # was returning prompt-rewrite text instead of an image task_id —
        # both bugs evaporate when this is False, since we then bypass the
        # agent and hit the image endpoint directly with the raw prompt.
        auto_prompt: bool = False,
        background_mode: bool = True,
        timeout: float = 300.0,
        ref_paths: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Submit a generation request to the extension.

        The extension will:
          1. POST /api/agent/ask_proxy with our params + its own reCAPTCHA token
          2. Parse the SSE stream to extract task_id
          3. Poll /api/spark/image_generation_task_detail until COMPLETED
          4. Download the image via /api/files/s/{id}
          5. Return {image_bytes_b64, image_url, prompt_used, model_used, ...}
            OR {error: "..."}

        Timeout default 300s (5 min) — generation typically 30-60s + polling.
        """
        self._request_counter += 1
        request_id = f"gen_{self._request_counter}_{int(time.time())}"

        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()

        # Pre-encode reference images to data URLs. The cache means a queue
        # of N jobs that all point at the same file pays exactly 1 read + 1
        # encode in total (pick-once-reuse-forever, per user spec).
        ref_images_b64: List[Dict[str, str]] = []
        ref_paths = ref_paths or []
        for p in ref_paths:
            entry = _encode_ref_file(str(p))
            if not entry:
                self._log(f"[GensparkBridge] Skipping missing/unreadable ref: {p}")
                continue
            ref_images_b64.append({
                "mime": entry["mime"],
                "data_url": entry["data_url"],
            })
            # Hard cap at 3 — Genspark's body cap is ~5MB and we don't want
            # multiple full-res images stacked. Most use-cases are 1 ref.
            if len(ref_images_b64) >= 3:
                break
        if ref_images_b64:
            self._log(
                f"[GensparkBridge] Reference images attached: "
                f"{len(ref_images_b64)} (cached={len(_REF_CACHE)})"
            )

        self._pending_requests[request_id] = {
            "account": account,
            "action": "IMAGE_GENERATION",
            "prompt": prompt,
            "model_params": {
                "type": "image",
                "model": model,
                "aspect_ratio": aspect_ratio,
                "auto_prompt": auto_prompt,
                "style": style,
                "image_size": image_size,
                "background_mode": background_mode,
                "camera_control": None,
            },
            # ref_images_b64 is sent in a sibling field, NOT inside
            # model_params, because Genspark's server validates model_params
            # against a schema and rejects unknown keys. The extension
            # picks this up and weaves the data URLs into the messages array.
            "ref_images_b64": ref_images_b64,
            "future": future,
            "created": time.time(),
            "_failed_ext_keys": set(),
        }

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            self._pending_requests.pop(request_id, None)
            self._dispatched_to.pop(request_id, None)
            return {"error": "timeout"}

    def get_connected_accounts(self) -> Dict[str, Dict[str, Any]]:
        """Get currently-connected Genspark accounts (reported by extension)."""
        return dict(self._connected_accounts)

    @property
    def is_extension_connected(self) -> bool:
        """True if the extension polled us in the last 5 seconds."""
        return (time.time() - self._extension_last_seen) < 5

    # ═══════════════════════════════════════════════════════════════
    # HTTP Handlers — called by Chrome Extension
    # ═══════════════════════════════════════════════════════════════

    async def _handle_poll(self, request: web.Request) -> web.Response:
        """Extension polls for work. Same multi-profile pattern as Flow bridge —
        extension sends ?accounts=email1,email2 and we only give it work for
        those accounts.
        """
        self._extension_last_seen = time.time()

        ext_accounts_param = request.query.get("accounts", "")
        ext_accounts = set(
            e.strip() for e in ext_accounts_param.split(",") if e.strip()
        ) if ext_accounts_param else set()
        ext_key = frozenset(ext_accounts) if ext_accounts else None

        response_data: Dict[str, Any] = {"work": None}

        # Don't give work to an extension that hasn't detected its accounts yet
        if not ext_accounts:
            return web.json_response(response_data, headers=_cors())

        # Find a pending request targeted at one of this ext's accounts.
        # Skip any request already in flight (dispatched within DISPATCH_LOCK)
        # to support parallel processing without duplicating generations.
        now = time.time()
        for req_id, req in list(self._pending_requests.items()):
            fut = req.get("future")
            if not fut or fut.done():
                continue
            target = req.get("account", "")
            failed_ext_keys = req.get("_failed_ext_keys", set())
            if ext_key and ext_key in failed_ext_keys:
                continue

            dispatched = self._dispatched_to.get(req_id)
            if dispatched:
                ts = dispatched.get("ts", 0)
                # Still within lock window → don't re-dispatch
                if now - ts < self.DISPATCH_LOCK_SECONDS:
                    continue
                # Lock expired (extension likely crashed) → allow re-dispatch

            if target in ext_accounts or not target:
                response_data["work"] = {
                    "request_id": req_id,
                    "account": target,
                    "action": req["action"],
                    "prompt": req["prompt"],
                    "model_params": req["model_params"],
                    "ref_images_b64": req.get("ref_images_b64", []),
                    "recaptcha_site_key": GENSPARK_RECAPTCHA_SITE_KEY,
                }
                self._dispatched_to[req_id] = {"ext_key": ext_key, "ts": now}
                break

        return web.json_response(response_data, headers=_cors())

    async def _handle_work_result(self, request: web.Request) -> web.Response:
        """Extension submits the final result for a request_id.

        Expected body:
          {
            "request_id": "gen_...",
            "image_url": "https://www.genspark.ai/api/files/s/abc?...",
            "image_bytes_b64": "<base64 jpeg>",     // optional but preferred
            "prompt_used": "enriched prompt ...",
            "model_used": "GEMINI_FLASH_IMAGE_EDIT:nano-banana-2",
            "task_id": "396305a9-...",
            "project_id": "9fbedbc1-...",
            "error": null | "error message"
          }
        """
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False, "reason": "bad_json"}, status=400)

        request_id = str(data.get("request_id", ""))
        req = self._pending_requests.pop(request_id, None)
        self._dispatched_to.pop(request_id, None)
        if not req:
            # Possibly already timed out
            return web.json_response({"ok": True, "stale": True}, headers=_cors())

        fut = req.get("future")
        if not fut or fut.done():
            return web.json_response({"ok": True, "stale": True}, headers=_cors())

        if data.get("error"):
            dbg = data.get("debug")
            if dbg:
                try:
                    evt_summary = dbg.get("event_types") or {}
                    self._log(
                        f"[GensparkBridge] SSE debug — event types: "
                        f"{evt_summary} (project_id={dbg.get('project_id')})"
                    )
                    # The "rich" events (message_result, message_field, tool
                    # message_start) are far more useful than rolling deltas
                    # — they're where task_id actually lives. Log them in
                    # full so we can see what Genspark is returning.
                    rich = dbg.get("rich_events") or []
                    if rich:
                        self._log(
                            f"[GensparkBridge] SSE debug — {len(rich)} rich event(s):"
                        )
                        for i, e in enumerate(rich):
                            self._log(f"[GensparkBridge]   rich[{i}]: {e}")
                    else:
                        last_events = dbg.get("last_events") or []
                        for e in last_events[-5:]:
                            self._log(f"[GensparkBridge]   event: {e}")
                except Exception:
                    pass
            fut.set_result({"error": str(data["error"])})
        else:
            self._images_generated += 1
            fut.set_result({
                "image_url": data.get("image_url", ""),
                "image_bytes_b64": data.get("image_bytes_b64", ""),
                "image_urls_nowatermark": data.get("image_urls_nowatermark", []),
                "prompt_used": data.get("prompt_used", ""),
                "model_used": data.get("model_used", ""),
                "task_id": data.get("task_id", ""),
                "project_id": data.get("project_id", ""),
                "error": None,
            })

        return web.json_response({"ok": True}, headers=_cors())

    async def _handle_accounts(self, request: web.Request) -> web.Response:
        """Extension reports which Genspark accounts are logged in."""
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False}, status=400)

        accounts = data.get("accounts") or []
        now = time.time()
        seen_emails = set()
        for a in accounts:
            email = str(a.get("email", "")).strip()
            if not email:
                continue
            seen_emails.add(email)
            self._connected_accounts[email] = {
                "email": email,
                "plan_type": str(a.get("plan_type", "free")),
                "tab_id": int(a.get("tab_id", 0) or 0),
                "user_id": str(a.get("user_id", "")),
                "display_name": str(a.get("display_name", "")),
                "last_seen": now,
            }
        # Expire accounts we haven't heard about in 30s
        for email in list(self._connected_accounts.keys()):
            if email in seen_emails:
                continue
            if now - self._connected_accounts[email].get("last_seen", 0) > 30:
                self._connected_accounts.pop(email, None)

        return web.json_response({"ok": True}, headers=_cors())

    async def _handle_progress(self, request: web.Request) -> web.Response:
        """Extension posts per-step progress updates so the user can see
        exactly where each request is in the pipeline.
        """
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False}, status=400)

        rid = str(data.get("request_id", ""))[:40]
        step = str(data.get("step", ""))
        detail = str(data.get("detail", ""))[:200]
        if rid and step:
            msg = f"[Genspark-progress] {rid}: {step}"
            if detail:
                msg += f" — {detail}"
            self._log(msg)
        return web.json_response({"ok": True}, headers=_cors())

    async def _handle_status(self, request: web.Request) -> web.Response:
        """App checks bridge status."""
        return web.json_response({
            "running": True,
            "extension_connected": self.is_extension_connected,
            "connected_accounts": list(self._connected_accounts.keys()),
            "images_generated": self._images_generated,
            "pending_requests": len(self._pending_requests),
        }, headers=_cors())


def _cors() -> Dict[str, str]:
    return {"Access-Control-Allow-Origin": "*"}
