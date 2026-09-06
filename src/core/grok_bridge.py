"""
Grok Bridge Server — local HTTP bridge for Chrome Extension <-> Python.

Architecture (mirrors genspark_bridge.py but for grok.com):
  Python App <-> Bridge (localhost:18926) <-> Chrome Extension

Key differences vs Flow/Genspark:
  - No Bearer token, no reCAPTCHA — Grok uses cookies + Statsig headers
    which are auto-attached when fetch runs inside grok.com tab's MAIN world
  - All three endpoints (upload, create, animate) are executed inside the
    extension; the bridge only hands off work items and collects the final
    video bytes as base64
  - Streaming NDJSON response is consumed inside the tab by grok.js — the
    bridge just receives the final videoUrl + downloaded bytes

Endpoints (served to extension):
  GET  /grok/poll          — extension polls for pending work
  POST /grok/work-result   — extension sends generation result back
  POST /grok/progress      — optional progress breadcrumbs
  POST /grok/accounts      — extension reports logged-in Grok accounts
  GET  /grok/status        — app checks bridge status
"""

import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from aiohttp import web

log = logging.getLogger(__name__)

GROK_BRIDGE_PORT = 18926
GROK_BRIDGE_HOST = "127.0.0.1"


class GrokBridge:
    """Local HTTP bridge between Python app and Chrome Extension — Grok mode.

    Runs on port 18926 (Flow = 18924, Genspark = 18925). Independent — all
    three can coexist without interfering.
    """

    # Dispatch lock: once a request is handed to the extension, don't
    # re-dispatch it for this many seconds. Grok video gen takes up to
    # ~3 minutes including upload + streaming.
    DISPATCH_LOCK_SECONDS = 360  # 6 min

    def __init__(self, log_fn: Optional[Callable[[str], None]] = None):
        self._log: Callable[[str], None] = log_fn or (lambda msg: None)
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None

        # Pending generation requests: request_id -> {
        #   prompt, account, settings..., future, created_at
        # }
        self._pending_requests: Dict[str, Dict[str, Any]] = {}
        self._request_counter = 0

        # Last time we saw ANY activity from the extension for a given
        # request_id (progress event, poll-for-work hit, or dispatch).
        # Used by wait_for_result to distinguish "extension is working
        # on it, just slow" from "extension went silent, bail out".
        self._last_activity: Dict[str, float] = {}

        # Accounts reported by extension.
        # Shape: { email: {email, userId, subscription, last_seen} }
        self._connected_accounts: Dict[str, Dict[str, Any]] = {}

        # Dispatch tracking — once a request_id is given to the extension,
        # don't hand it out again until the lock expires or result arrives.
        self._dispatched: Dict[str, float] = {}  # request_id -> ts

        self._videos_generated = 0
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
        """Start the Grok bridge HTTP server."""
        # Videos can be 5-30 MB base64-encoded. Be generous on client size.
        self._app = web.Application(
            client_max_size=200 * 1024 * 1024,
            middlewares=[self._cors_middleware],
        )
        self._app.router.add_route(
            "OPTIONS", "/{tail:.*}", lambda r: web.Response(status=204)
        )
        self._app.router.add_get("/grok/poll", self._handle_poll)
        self._app.router.add_post("/grok/work-result", self._handle_work_result)
        self._app.router.add_post("/grok/progress", self._handle_progress)
        self._app.router.add_post("/grok/accounts", self._handle_accounts)
        self._app.router.add_get("/grok/status", self._handle_status)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, GROK_BRIDGE_HOST, GROK_BRIDGE_PORT)
        try:
            await self._site.start()
            self._log(f"[GrokBridge] Started on http://{GROK_BRIDGE_HOST}:{GROK_BRIDGE_PORT}")
        except OSError as e:
            self._log(f"[GrokBridge] Port {GROK_BRIDGE_PORT} unavailable: {e}")
            raise

    async def stop(self) -> None:
        """Stop the bridge cleanly."""
        try:
            if self._site:
                await self._site.stop()
            if self._runner:
                await self._runner.cleanup()
        except Exception as e:
            log.warning("GrokBridge stop error: %s", e)
        self._log("[GrokBridge] Stopped.")

    # ═══════════════════════════════════════════════════════════════
    # Public API — queue_manager / grok_mode call these
    # ═══════════════════════════════════════════════════════════════

    def submit_request(
        self,
        account: str,
        prompt: str,
        *,
        aspect_ratio: str = "16:9",
        video_length: int = 10,
        resolution: str = "720p",
        mode: str = "custom",
        reference_image_base64: str = "",
        reference_image_filename: str = "",
        reference_image_mime: str = "image/jpeg",
    ) -> "tuple[str, asyncio.Future[Dict[str, Any]]]":
        """Queue a Grok video generation request. Returns (request_id, future).
        The future resolves when the extension reports the result
        (success or error). The request_id can be passed to
        `wait_for_result` / `time_since_last_activity` for smarter waits."""
        self._request_counter += 1
        rid = f"grok-{int(time.time())}-{self._request_counter}"
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()

        now = time.time()
        self._pending_requests[rid] = {
            "request_id": rid,
            "account": account,
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "video_length": video_length,
            "resolution": resolution,
            "mode": mode,
            "reference_image_base64": reference_image_base64,
            "reference_image_filename": reference_image_filename,
            "reference_image_mime": reference_image_mime,
            "future": fut,
            "created_at": now,
        }
        self._last_activity[rid] = now
        return rid, fut

    def time_since_last_activity(self, rid: str) -> Optional[float]:
        """Seconds since the extension last reported ANY activity for
        this request (dispatch, progress event, etc.). Returns None if
        the request_id is unknown."""
        ts = self._last_activity.get(rid)
        if ts is None:
            return None
        return time.time() - ts

    async def wait_for_result(
        self,
        rid: str,
        fut: "asyncio.Future[Dict[str, Any]]",
        *,
        idle_timeout_s: float = 300.0,
        max_total_s: float = 1800.0,
    ) -> Dict[str, Any]:
        """Wait for a submitted request's result with an activity-based
        timeout.

        - Fails if the extension goes idle (no progress/dispatch events)
          for more than `idle_timeout_s` seconds.
        - Fails if total wait exceeds `max_total_s` as a hard safety cap.
        - Succeeds as soon as the extension posts a result.

        Why not a flat wait_for(future, 300)? The 300s countdown starts
        when Python submits, but the extension may spend minutes waiting
        for a free tab before it actually begins. Flat 300s causes false
        timeouts for the tail of a queue even though the extension
        successfully generates the video — Python abandons the future
        before the extension posts the result back.
        """
        started_at = time.time()
        while True:
            if fut.done():
                return fut.result()
            idle_for = self.time_since_last_activity(rid)
            if idle_for is not None and idle_for > idle_timeout_s:
                # No word from the extension in too long — assume stuck.
                self._pending_requests.pop(rid, None)
                self._dispatched.pop(rid, None)
                self._last_activity.pop(rid, None)
                if not fut.done():
                    fut.set_result({
                        "error": "grok_idle_timeout",
                        "detail": (
                            f"No activity from extension for "
                            f"{int(idle_for)}s. Chrome or the extension "
                            "may have been closed, or the tab got "
                            "stuck. Restart Chrome and try again."
                        ),
                    })
                return fut.result()
            if time.time() - started_at > max_total_s:
                self._pending_requests.pop(rid, None)
                self._dispatched.pop(rid, None)
                self._last_activity.pop(rid, None)
                if not fut.done():
                    fut.set_result({
                        "error": "grok_total_timeout",
                        "detail": (
                            f"Job exceeded total {int(max_total_s)}s "
                            "cap. Too many slots queued on too few "
                            "Grok tabs — open more grok.com/imagine "
                            "tabs or reduce slot count."
                        ),
                    })
                return fut.result()
            try:
                # Short wait so we can re-check idle/total budget
                # periodically. 5s is a good balance between
                # responsiveness and CPU cost.
                return await asyncio.wait_for(asyncio.shield(fut), timeout=5.0)
            except asyncio.TimeoutError:
                continue

    def get_accounts(self) -> List[Dict[str, Any]]:
        """Snapshot of currently-connected Grok accounts."""
        return list(self._connected_accounts.values())

    def is_extension_connected(self) -> bool:
        """Heuristic — extension polled the bridge recently (< 10s)."""
        return (time.time() - self._extension_last_seen) < 10.0

    def cancel_all_pending(self) -> None:
        """Fail all pending futures with a cancellation error.
        Called when the user stops the queue manager."""
        for rid, req in list(self._pending_requests.items()):
            fut: asyncio.Future = req["future"]
            if not fut.done():
                fut.set_result({"error": "cancelled_by_user"})
        self._pending_requests.clear()
        self._dispatched.clear()
        self._last_activity.clear()

    # ═══════════════════════════════════════════════════════════════
    # HTTP handlers
    # ═══════════════════════════════════════════════════════════════

    async def _handle_poll(self, request: web.Request) -> web.Response:
        """Extension polls here. If there's pending work for one of its
        accounts, return it — otherwise return {}."""
        self._extension_last_seen = time.time()
        # Expect ?accounts=email1,email2 from extension
        accounts_csv = request.query.get("accounts", "")
        ext_emails = set(e.strip() for e in accounts_csv.split(",") if e.strip())

        now = time.time()
        # Expire dispatch locks that are older than DISPATCH_LOCK_SECONDS
        for rid in list(self._dispatched.keys()):
            if now - self._dispatched[rid] > self.DISPATCH_LOCK_SECONDS:
                self._dispatched.pop(rid, None)

        # Find the oldest pending request for one of the extension's accounts
        chosen: Optional[Dict[str, Any]] = None
        for rid, req in sorted(
            self._pending_requests.items(), key=lambda kv: kv[1]["created_at"]
        ):
            if rid in self._dispatched:
                continue
            if ext_emails and req["account"] not in ext_emails:
                continue
            chosen = req
            break

        if not chosen:
            return web.json_response({})

        rid = chosen["request_id"]
        self._dispatched[rid] = now
        # Extension is about to work on this rid — count that as activity
        # so wait_for_result doesn't fire an idle timeout while the
        # extension was still queuing it internally.
        self._last_activity[rid] = now

        # Send everything the extension needs (minus the future)
        payload = {
            "work": {
                "request_id": rid,
                "account": chosen["account"],
                "prompt": chosen["prompt"],
                "aspect_ratio": chosen["aspect_ratio"],
                "video_length": chosen["video_length"],
                "resolution": chosen["resolution"],
                "mode": chosen["mode"],
                "reference_image_base64": chosen["reference_image_base64"],
                "reference_image_filename": chosen["reference_image_filename"],
                "reference_image_mime": chosen["reference_image_mime"],
            }
        }
        return web.json_response(payload)

    async def _handle_work_result(self, request: web.Request) -> web.Response:
        """Extension calls this when a job completes (success or error)."""
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400, text="invalid JSON")
        rid = data.get("request_id", "")
        if not rid:
            return web.Response(status=400, text="missing request_id")

        req = self._pending_requests.pop(rid, None)
        self._dispatched.pop(rid, None)
        self._last_activity.pop(rid, None)
        if not req:
            # Extension may have re-submitted — silent OK
            return web.json_response({"ok": True, "stale": True})

        fut: asyncio.Future = req["future"]
        if not fut.done():
            fut.set_result(dict(data))

        if data.get("success"):
            self._videos_generated += 1

        return web.json_response({"ok": True})

    async def _handle_progress(self, request: web.Request) -> web.Response:
        """Optional progress breadcrumbs — surface to app log for visibility."""
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400, text="invalid JSON")
        rid = data.get("request_id", "")
        stage = data.get("stage", "")
        detail = data.get("detail", "")
        if rid:
            # Any progress event (including tab_wait) proves the
            # extension is alive — bump last_activity so wait_for_result
            # doesn't fire a false idle timeout.
            self._last_activity[rid] = time.time()
        if rid and stage:
            # Keep log quiet for high-frequency stages
            if stage not in {"started"}:
                self._log(f"[GrokBridge] {rid[-8:]} → {stage}: {detail}")
        return web.json_response({"ok": True})

    async def _handle_accounts(self, request: web.Request) -> web.Response:
        """Extension reports logged-in Grok accounts."""
        self._extension_last_seen = time.time()
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400, text="invalid JSON")
        accounts = data.get("accounts", []) or []
        now = time.time()
        fresh: Dict[str, Dict[str, Any]] = {}
        for a in accounts:
            email = str(a.get("email") or "").strip()
            if not email:
                continue
            try:
                tab_count = max(1, int(a.get("tab_count") or 1))
            except (TypeError, ValueError):
                tab_count = 1
            fresh[email] = {
                "email": email,
                "userId": str(a.get("userId") or ""),
                "subscription": str(a.get("subscription") or ""),
                "tab_count": tab_count,
                "last_seen": now,
            }
        self._connected_accounts = fresh
        return web.json_response({"ok": True, "count": len(fresh)})

    async def _handle_status(self, request: web.Request) -> web.Response:
        return web.json_response({
            "connected": self.is_extension_connected(),
            "accounts": list(self._connected_accounts.values()),
            "pending_requests": len(self._pending_requests),
            "videos_generated": self._videos_generated,
            "uptime_s": int(time.time() - (self._extension_last_seen or time.time())),
        })
