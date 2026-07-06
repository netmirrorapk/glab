"""
Extension Bridge Server — local HTTP server for Chrome Extension communication.

Architecture:
  Python App ←→ Bridge (localhost:18924) ←→ Chrome Extension

Endpoints:
  GET  /poll      — Extension polls for pending work
  POST /token     — Extension sends reCAPTCHA token + auth back
  POST /accounts  — Extension reports connected accounts
  POST /project   — Extension reports project ID
  GET  /status    — App checks bridge status
  POST /command   — App sends commands to extension (cookie clear, reload)

The bridge runs as an asyncio background task inside the main app.
"""

import asyncio
import json
import time
import logging
from collections import deque
from typing import Optional, Dict, Any, Callable, List
from aiohttp import web

log = logging.getLogger(__name__)

BRIDGE_PORT = 18924
BRIDGE_HOST = "127.0.0.1"


class ExtensionBridge:
    """Local HTTP bridge between Python app and Chrome Extension."""

    def __init__(self, log_fn: Optional[Callable] = None):
        self._log = log_fn or (lambda msg: None)
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None

        # Token request queue: request_id -> { account, action, future, ... }
        self._pending_requests: Dict[str, Dict[str, Any]] = {}
        self._request_counter = 0

        # Connected accounts from extension
        self._connected_accounts: Dict[str, Dict[str, Any]] = {}  # email -> info

        # Project IDs from extension
        self._project_ids: Dict[str, str] = {}  # account_email -> project_id
        # Per-account dict of {project_id: burned_at_ts}. A project enters
        # this map when Flow returns 429 against a submission using it —
        # see burn_project() / get_project_id() for rotation behavior.
        self._burned_projects: Dict[str, Dict[str, float]] = {}
        # How long a burned project stays quarantined. Google's per-project
        # quota often refills faster than 30 minutes — especially when the
        # user rotates VPN externally (Google's 429 attribution is
        # account+project+IP; an IP change effectively releases the burn
        # server-side even though our local timer hasn't expired). Ten
        # minutes is aggressive enough to bring old projects back into
        # rotation quickly while still preventing rapid re-burn on the
        # exact project that just failed.
        self.PROJECT_BURN_COOLDOWN_S = 10 * 60

        # Pending commands for extension
        self._pending_commands = []

        # Track which request was last dispatched to which extension
        # request_id -> frozenset(ext_accounts) that currently has the work
        self._dispatched_to: Dict[str, frozenset] = {}

        # ─── Token Pool (pre-fetched reCAPTCHA tokens) ───
        # Keyed by (account, action) — reCAPTCHA Enterprise tokens carry the
        # action they were minted for, and Google rejects them with
        # PUBLIC_ERROR_UNUSUAL_ACTIVITY if the action doesn't match the
        # endpoint they're submitted to. So image and video jobs need
        # separate pools — serving an IMAGE_GENERATION token to the video
        # endpoint was the root cause of "videos fail, images succeed" on
        # the same warm account.
        self._token_pool: Dict[tuple, deque] = {}     # (account, action) -> deque of {token, access_token, project_id, ts}
        self._prefetch_requests: Dict[str, tuple] = {}  # prefetch_request_id -> (account, action)
        # Per-(account, action) last-used timestamp. Pre-fetch only refills
        # actions that have been used recently — keeping VIDEO pool warm
        # during an image-only batch (and vice versa) just leaks execute()
        # calls to Google's risk model while never consuming the tokens.
        # See PREFETCH_RECENCY_S below for the gating window.
        self._action_last_used: Dict[tuple, float] = {}  # (account, action) -> ts
        # Pool sizing — UPDATED May 2026.
        #
        # Was 10 (set Apr 19) to keep tokens hot for the old aiohttp-direct
        # submission path. That path was retired on 2026-05-11 in favor of
        # extension MAIN-world fresh-mint at submit time, so the pool is
        # now consulted only for cached access_token + project_id; its
        # reCAPTCHA token is discarded by callers. A high target therefore
        # forces background refills (one execute() per refill) for tokens
        # nobody will use — production logs from a 37-image run showed the
        # VIDEO pool refilling itself to ~90 entries (cycling through 90s
        # expiries) while zero video jobs ran. That waste is exactly the
        # "isolated execute() spam" signature reCAPTCHA Enterprise flags.
        #
        # Target=2 keeps a small just-in-time pool so first-request latency
        # stays low for either action, while letting unused-action pools
        # decay to zero between bursts.
        self.TOKEN_POOL_TARGET = 2
        self.TOKEN_MAX_AGE = 90      # seconds before a cached token is too old
        # Which actions are ELIGIBLE for pre-fetch — actual refill also
        # gated on per-(account, action) recency via _action_last_used.
        self.PREFETCH_ACTIONS = ("IMAGE_GENERATION", "VIDEO_GENERATION")
        # Pre-fetch is skipped for actions whose last-used timestamp is
        # older than this. 5 minutes is long enough that a mid-batch
        # cadence of image and video jobs keeps both pools alive, short
        # enough that a single-mode batch (image-only or video-only) lets
        # the unused pool go cold within a few minutes.
        self.PREFETCH_RECENCY_S = 300.0

        # Stats
        self._tokens_received = 0
        self._extension_last_seen = 0

        # ─── Auto Warmup Mode (Ecosystem Activity) ───
        # Controls whether the extension performs background ecosystem activity
        # (YouTube, Search, Maps, etc.) to keep reCAPTCHA scores healthy.
        self._ecosystem_enabled: bool = False     # toggle from extension popup or app
        self._generation_running: bool = False    # set True when any slot is running a job
        self._ecosystem_stats: Dict[str, Any] = {
            "accounts": {},       # email -> { "last_activity": ts, "today_count": n, "current_site": str }
            "last_state_change": 0,
            "state": "idle",      # idle | running | paused | disabled
        }
        # Per-account ecosystem hold (syncs with reCAPTCHA hold state).
        # Held accounts: warmup stops AND new generation jobs are blocked
        # from dispatching to them (bridge refuses tokens) until hold expires
        # OR user toggles force-enable.
        self._ecosystem_held_accounts: Dict[str, float] = {}  # email -> released_at_ts
        # Force-enable override: user can choose to use a held account at
        # their own risk. Generation allowed, warmup still blocked.
        self._force_enabled_accounts: set = set()

        # ─── EXECUTE_FETCH concurrency limiter ───────────────────────
        # Each EXECUTE_FETCH routes through the extension's MAIN-world
        # script injection + native fetch, which is serialized per
        # Chrome scripting channel — one channel per TAB. The extension
        # now routes each work item to the least-busy matching tab via
        # findLabsTab(), so if the user opens N labs.google.com tabs
        # for the same account they get ~N parallel channels instead of
        # one. This cap must be high enough to saturate a multi-tab
        # setup (3 tabs × 4 concurrent per tab ≈ 12) but low enough that
        # a single-tab account doesn't burst past what Chrome's one
        # scripting channel can drain. 12 is the sweet spot for 1-4 tabs.
        self._fetch_semaphores: Dict[str, asyncio.Semaphore] = {}
        self.FETCH_CONCURRENCY_PER_ACCOUNT = 12

    # ═══════════════════════════════════════════════════════════════
    # Lifecycle
    # ═══════════════════════════════════════════════════════════════

    async def start(self):
        """Start the bridge HTTP server."""
        self._app = web.Application()
        self._app.router.add_get("/poll", self._handle_poll)
        self._app.router.add_post("/token", self._handle_token)
        self._app.router.add_post("/accounts", self._handle_accounts)
        self._app.router.add_post("/project", self._handle_project)
        self._app.router.add_get("/status", self._handle_status)
        self._app.router.add_post("/command", self._handle_command_post)
        # Extension exports the account's Google cookies (already converted to
        # Playwright format) so CloakBrowser modes can log in on a proxy IP.
        self._app.router.add_post("/cookies", self._handle_cookies)
        # Ecosystem / Auto Warmup Mode endpoints
        self._app.router.add_get("/ecosystem", self._handle_ecosystem_status)
        self._app.router.add_post("/ecosystem", self._handle_ecosystem_update)
        self._app.router.add_post("/ecosystem/activity", self._handle_ecosystem_activity_report)

        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()

        try:
            self._site = web.TCPSite(self._runner, BRIDGE_HOST, BRIDGE_PORT)
            await self._site.start()
            self._log(f"[Bridge] Started on http://{BRIDGE_HOST}:{BRIDGE_PORT}")
        except OSError as e:
            self._log(f"[Bridge] Port {BRIDGE_PORT} busy: {e}. Trying alternative...")
            # Try alternative port
            self._site = web.TCPSite(self._runner, BRIDGE_HOST, BRIDGE_PORT + 1)
            await self._site.start()
            self._log(f"[Bridge] Started on http://{BRIDGE_HOST}:{BRIDGE_PORT + 1}")

    async def stop(self):
        """Stop the bridge server."""
        # Cancel all pending requests
        for req_id, req in self._pending_requests.items():
            fut = req.get("future")
            if fut and not fut.done():
                fut.set_result({"error": "bridge_stopped"})
        self._pending_requests.clear()
        self._dispatched_to.clear()
        self._token_pool.clear()
        self._prefetch_requests.clear()
        # Ecosystem state cleanup
        self._generation_running = False
        # Keep _ecosystem_enabled + _ecosystem_held_accounts across restarts —
        # they persist via bridge runtime (user's toggle choice survives).

        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()
        self._log("[Bridge] Stopped.")

    # ═══════════════════════════════════════════════════════════════
    # Public API — called by ExtensionModeManager
    # ═══════════════════════════════════════════════════════════════

    async def request_token(self, account: str, action: str, timeout: float = 30.0) -> Dict[str, Any]:
        """
        Request a reCAPTCHA token from the Chrome Extension.
        Checks the pre-fetched token pool first for instant response.

        Args:
            account: Email of the account to use
            action: reCAPTCHA action (e.g., "IMAGE_GENERATION")
            timeout: Max seconds to wait for token

        Returns:
            dict with keys: token, access_token, project_id, error
        """
        # ─── Block dispatch if account is held (Phase 7 emergency stop) ───
        if self.is_account_held(account):
            info = self.get_hold_info(account)
            return {
                "error": f"account_held:{info['seconds_remaining']}s_remaining",
                "held": True,
                "seconds_remaining": info["seconds_remaining"],
            }

        # Mark this (account, action) as recently active so the pre-fetch
        # loop in _handle_poll knows to keep its pool warm. Actions that
        # nobody asks for don't get pre-fetched and the unused-pool refill
        # cycle (which leaks execute() calls to Google for tokens we never
        # use) stops accumulating.
        self._action_last_used[(account, action)] = time.time()

        # ─── Check token pool first ───
        # Action-scoped lookup — IMAGE_GENERATION and VIDEO_GENERATION
        # tokens live in separate pools so we never serve the wrong type.
        pool = self._token_pool.get((account, action))
        if pool:
            now = time.time()
            while pool:
                cached = pool.popleft()
                age = now - cached["ts"]
                if age < self.TOKEN_MAX_AGE:
                    self._log(
                        f"[Bridge] Pool hit for {account} "
                        f"(action={action}, age {age:.0f}s, {len(pool)} left in pool)"
                    )
                    return {
                        "token": cached["token"],
                        "access_token": cached["access_token"],
                        "project_id": cached["project_id"],
                        "error": None,
                    }
                # Token too old, discard and check next

        # ─── No cached token — fall through to live request ───
        self._request_counter += 1
        request_id = f"req_{self._request_counter}_{int(time.time())}"

        loop = asyncio.get_event_loop()
        future = loop.create_future()

        self._pending_requests[request_id] = {
            "account": account,
            "action": action,
            "future": future,
            "created": time.time(),
            "_failed_ext_keys": set(),   # frozensets of ext accounts that failed
            "_reroute_count": 0,
        }

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            self._pending_requests.pop(request_id, None)
            self._dispatched_to.pop(request_id, None)
            return {"error": "timeout"}

    async def request_api_fetch(
        self,
        account: str,
        url: str,
        method: str = "POST",
        body: str = "",
        headers: Optional[Dict[str, str]] = None,
        recaptcha_action: Optional[str] = None,
        inject_recaptcha_path: Optional[str] = None,
        timeout: float = 180.0,
    ) -> Dict[str, Any]:
        """Have the Chrome extension execute an HTTP request from inside the
        labs.google.com tab using native window.fetch().

        Why bother routing through the extension instead of aiohttp? Google's
        anti-abuse system checks for cryptographically-signed Chrome headers
        (x-browser-validation, x-client-data) that aiohttp can't produce.
        Even with a perfectly-valid reCAPTCHA token, requests originating
        from outside Chrome get flagged with PUBLIC_ERROR_UNUSUAL_ACTIVITY.
        Native fetch() inside the page makes Chrome auto-add all the right
        headers.

        If recaptcha_action is set, the extension will mint a fresh
        reCAPTCHA token with that action and write it into the JSON body
        at inject_recaptcha_path (e.g. "clientContext.recaptchaContext.token").
        Pass None to send the body as-is (caller has already attached a
        token, or none is needed).

        Returns: {status: int, body: str, error: str | None}
        """
        if self.is_account_held(account):
            info = self.get_hold_info(account)
            return {
                "error": f"account_held:{info['seconds_remaining']}s_remaining",
                "held": True,
            }

        # Mark this (account, action) as recently active for the same
        # reason as in request_token() — but only when the fetch actually
        # involves a fresh-mint at submit time (recaptcha_action set).
        # Otherwise the call is just a passthrough fetch (e.g. video
        # status poll) that doesn't need pre-fetched pool tokens.
        if recaptcha_action:
            self._action_last_used[(account, str(recaptcha_action))] = time.time()

        # Acquire the per-account EXECUTE_FETCH slot — prevents burst
        # traffic from choking the single Chrome scripting channel per
        # tab. Extra callers block on the semaphore (no timeout here),
        # and the outer `timeout` below still bounds total wait time.
        semaphore = self._fetch_semaphores.get(account)
        if semaphore is None:
            semaphore = asyncio.Semaphore(self.FETCH_CONCURRENCY_PER_ACCOUNT)
            self._fetch_semaphores[account] = semaphore

        async with semaphore:
            self._request_counter += 1
            request_id = f"fetch_{self._request_counter}_{int(time.time())}"

            loop = asyncio.get_event_loop()
            future = loop.create_future()

            self._pending_requests[request_id] = {
                "account": account,
                "action": "EXECUTE_FETCH",
                "fetch_url": url,
                "fetch_method": method,
                "fetch_body": body,
                "fetch_headers": dict(headers or {}),
                "recaptcha_action": recaptcha_action,
                "inject_recaptcha_path": inject_recaptcha_path,
                "future": future,
                "created": time.time(),
                "_failed_ext_keys": set(),
                "_reroute_count": 0,
            }

            try:
                result = await asyncio.wait_for(future, timeout=timeout)
                return result
            except asyncio.TimeoutError:
                self._pending_requests.pop(request_id, None)
                self._dispatched_to.pop(request_id, None)
                return {"error": "timeout"}

    def send_command(self, command_type: str, account: str = "", data: Any = None):
        """Queue a command for the extension (cookie clear, reload, etc.)."""
        self._pending_commands.append({
            "type": command_type,
            "account": account,
            "data": data,
        })

    def get_connected_accounts(self):
        """Get list of accounts currently connected via extension."""
        return dict(self._connected_accounts)

    def get_project_id(self, account: str) -> Optional[str]:
        """Get cached project ID for an account, or None if the cached one
        is currently in 429-cooldown.

        Empirical finding (May 2026 user report): Google Labs Flow's
        rate limit is scoped to projectId, not to the user account.
        When a project hits ~30-150 successful submissions Google starts
        429-ing every subsequent request on THAT project regardless of
        score — but creating a fresh project on the same account
        immediately starts working again. burn_project() captures that
        behavior so the dispatch loop rotates projects automatically.
        """
        pid = self._project_ids.get(account)
        if not pid:
            return None
        burn_ts = self._burned_projects.get(account, {}).get(pid, 0.0)
        if burn_ts and (time.time() - burn_ts) < self.PROJECT_BURN_COOLDOWN_S:
            return None
        return pid

    def set_project_id(self, account: str, project_id: str):
        """Cache a project ID for an account."""
        self._project_ids[account] = project_id

    def burn_project(self, account: str, project_id: str):
        """Mark a project as rate-limit-exhausted for PROJECT_BURN_COOLDOWN_S.

        Called when a submission returns 429 — the project that's
        currently associated with this account gets quarantined so the
        next request_token / _resolve_project_id call picks a different
        one (or triggers extension to create a fresh project via the
        "new_project" command).
        """
        if not account or not project_id:
            return
        bucket = self._burned_projects.setdefault(account, {})
        bucket[project_id] = time.time()
        # Drop the cached pointer so the next get_project_id() returns
        # None and the caller routes through _resolve_project_id().
        if self._project_ids.get(account) == project_id:
            self._project_ids.pop(account, None)

    def is_project_burned(self, account: str, project_id: str) -> bool:
        """True if project_id is within its 429 cooldown window."""
        if not account or not project_id:
            return False
        burn_ts = self._burned_projects.get(account, {}).get(project_id, 0.0)
        return bool(burn_ts and (time.time() - burn_ts) < self.PROJECT_BURN_COOLDOWN_S)

    def get_oldest_burned_project(self, account: str) -> Optional[str]:
        """Return the project_id with the OLDEST burn timestamp for this
        account (i.e. the one whose 429 attribution is most likely to have
        expired on Google's side). Used as a last-resort fallback in
        _create_project when every rotation method has come up empty —
        especially valuable when the user has rotated their VPN/IP
        externally, since a burned project often works again on a fresh
        IP without waiting out the full local cooldown."""
        if not account:
            return None
        burns = self._burned_projects.get(account, {})
        if not burns:
            return None
        try:
            oldest_pid = min(burns.items(), key=lambda kv: kv[1])[0]
            return oldest_pid or None
        except Exception:
            return None

    @property
    def is_extension_connected(self) -> bool:
        """Check if extension has polled recently (within 5 seconds)."""
        return (time.time() - self._extension_last_seen) < 5

    # ═══════════════════════════════════════════════════════════════
    # HTTP Handlers — called by Chrome Extension
    # ═══════════════════════════════════════════════════════════════

    async def _handle_poll(self, request: web.Request) -> web.Response:
        """Extension polls for work. Only return work this extension can handle.

        The extension sends ?accounts=email1,email2 so the bridge knows which
        accounts this particular Chrome profile can serve. Multi-profile setups
        have multiple extensions, each with its own set of accounts.
        """
        self._extension_last_seen = time.time()

        # Parse which accounts this extension instance has
        ext_accounts_param = request.query.get("accounts", "")
        ext_accounts = set(
            e.strip() for e in ext_accounts_param.split(",") if e.strip()
        ) if ext_accounts_param else set()

        ext_key = frozenset(ext_accounts) if ext_accounts else None

        response_data = {"work": None, "command": None}

        # If extension hasn't detected its accounts yet (empty param),
        # DON'T give it any work — wait until it knows what it has.
        # This prevents the race condition where a fresh extension grabs
        # work it can't handle before detectAccounts() runs.
        if not ext_accounts:
            if self._pending_commands:
                response_data["command"] = self._pending_commands.pop(0)
            # Also share ecosystem directive here
            response_data["ecosystem"] = {
                "directive": "disabled" if not self._ecosystem_enabled
                             else ("paused" if self._generation_running else "active"),
                "held_accounts": {},
            }
            return web.json_response(response_data, headers={"Access-Control-Allow-Origin": "*"})

        # Check for pending token requests — match to this extension's accounts
        for req_id, req in list(self._pending_requests.items()):
            fut = req.get("future")
            if fut and not fut.done():
                target_account = req.get("account", "")

                # Skip if this extension already failed for this request
                failed_ext_keys = req.get("_failed_ext_keys", set())
                if ext_key and ext_key in failed_ext_keys:
                    continue

                # Skip if this request is currently dispatched to ANY extension
                # (waiting for response — don't give same work twice). The old
                # check was `dispatched_key != ext_key`, which let the SAME
                # extension re-receive the same work on every 500ms poll until
                # the response came back. The original code paired this with
                # an extension-side _workInProgress lock that silently dropped
                # the dupes; that lock was removed in 4a368a9 to enable
                # multi-tab parallelism, which exposed the bug. Result: a
                # single video request was dispatched 7–30 times during its
                # 15–30s generation window, and Google generated a video for
                # each duplicate POST. Fix: dedup at the bridge instead.
                if self._dispatched_to.get(req_id) is not None:
                    continue

                # Only give work if this extension has the target account
                if target_account in ext_accounts or not target_account:
                    work = {
                        "request_id": req_id,
                        "account": target_account,
                        "action": req["action"],
                    }
                    # EXECUTE_FETCH carries extra fetch parameters that
                    # the extension needs to actually perform the request
                    # (URL, body, headers, optional reCAPTCHA injection).
                    if req["action"] == "EXECUTE_FETCH":
                        work["fetch_url"] = req.get("fetch_url", "")
                        work["fetch_method"] = req.get("fetch_method", "POST")
                        work["fetch_body"] = req.get("fetch_body", "")
                        work["fetch_headers"] = req.get("fetch_headers", {})
                        work["recaptcha_action"] = req.get("recaptcha_action")
                        work["inject_recaptcha_path"] = req.get("inject_recaptcha_path")
                    response_data["work"] = work
                    # Track that this request is now dispatched to this extension
                    self._dispatched_to[req_id] = ext_key
                    break

        # ─── Token Pool Pre-fetch ───
        # If no real work pending, pre-fetch tokens to keep the pool topped
        # up — but ONLY for (account, action) pairs that have been used in
        # the last PREFETCH_RECENCY_S seconds. Without that gate the bridge
        # spends every quiet poll refilling pools that nobody consumes
        # (e.g. the VIDEO pool during an image-only batch), which leaks
        # one execute() call per refill cycle to Google's risk model for
        # tokens nobody will use. Each unused execute() is a load-bearing
        # bot signal — eliminating them dropped the pre-429 image budget
        # significantly in production testing.
        if response_data["work"] is None:
            now = time.time()
            picked = False
            for account in ext_accounts:
                if picked:
                    break
                for action in self.PREFETCH_ACTIONS:
                    last_used = self._action_last_used.get((account, action), 0.0)
                    if now - last_used > self.PREFETCH_RECENCY_S:
                        # Action hasn't been requested in a while — let its
                        # pool decay naturally. When the next real call to
                        # request_token() lands, _action_last_used is set
                        # and the very next poll resumes pre-fetch.
                        continue
                    pool = self._token_pool.get((account, action), deque())
                    valid_count = sum(
                        1 for t in pool if (now - t["ts"]) < self.TOKEN_MAX_AGE
                    )
                    in_flight = sum(
                        1 for v in self._prefetch_requests.values()
                        if v == (account, action)
                    )
                    if valid_count + in_flight < self.TOKEN_POOL_TARGET:
                        self._request_counter += 1
                        prefetch_id = f"prefetch_{self._request_counter}_{int(time.time())}"
                        self._prefetch_requests[prefetch_id] = (account, action)
                        response_data["work"] = {
                            "request_id": prefetch_id,
                            "account": account,
                            "action": action,
                        }
                        picked = True
                        break

        # Check for pending commands
        if self._pending_commands:
            response_data["command"] = self._pending_commands.pop(0)

        # ─── Piggyback ecosystem directive ───
        # Extension uses this to decide whether to run background activity.
        now = time.time()
        # Clean expired holds
        for a in [k for k, v in self._ecosystem_held_accounts.items() if v <= now]:
            self._ecosystem_held_accounts.pop(a, None)

        if not self._ecosystem_enabled:
            eco_directive = "disabled"
        elif self._generation_running:
            eco_directive = "paused"
        else:
            eco_directive = "active"

        response_data["ecosystem"] = {
            "directive": eco_directive,
            "held_accounts": {
                a: max(0, int(ts - now))
                for a, ts in self._ecosystem_held_accounts.items()
            },
        }

        return web.json_response(response_data, headers={"Access-Control-Allow-Origin": "*"})

    async def _handle_token(self, request: web.Request) -> web.Response:
        """Extension sends back token + auth data."""
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False}, status=400)

        request_id = data.get("request_id", "")
        error = data.get("error", "")

        # ─── Handle pre-fetch results (token pool) ───
        if request_id.startswith("prefetch_"):
            info = self._prefetch_requests.pop(request_id, None)
            if info and data.get("token") and not error:
                account, action = info
                pool = self._token_pool.setdefault((account, action), deque())
                pool.append({
                    "token": data["token"],
                    "access_token": data.get("access_token"),
                    "project_id": data.get("project_id"),
                    "ts": time.time(),
                })
                # Also cache project ID (account-scoped, action-independent)
                pid = data.get("project_id")
                if account and pid:
                    self._project_ids[account] = pid
                self._tokens_received += 1
                self._log(
                    f"[Bridge] Pool: cached token for {account} "
                    f"(action={action}, pool size: {len(pool)})"
                )
            return web.json_response({"ok": True}, headers={"Access-Control-Allow-Origin": "*"})

        # ─── Re-route on no_labs_tab errors ───
        # If extension couldn't find the tab for this account, don't fail the
        # request — put it back so a DIFFERENT extension instance can try.
        if error and "no_labs_tab" in str(error):
            req = self._pending_requests.get(request_id)
            if req and req.get("future") and not req["future"].done():
                # Mark which extension failed — use the dispatched_to tracking
                failed_ext_key = self._dispatched_to.pop(request_id, None)
                if failed_ext_key:
                    req.setdefault("_failed_ext_keys", set()).add(failed_ext_key)

                req["_reroute_count"] = req.get("_reroute_count", 0) + 1

                # If too many reroutes (all extensions tried), give up
                if req["_reroute_count"] >= 6:
                    self._pending_requests.pop(request_id, None)
                    # Shape the failure response based on what the caller
                    # was expecting — fetch caller wants status/body, token
                    # caller wants token/access_token.
                    if req.get("action") == "EXECUTE_FETCH":
                        req["future"].set_result({
                            "status": None, "body": "", "headers": {},
                            "error": f"all_extensions_failed: {error}",
                        })
                    else:
                        req["future"].set_result({
                            "token": None, "access_token": None,
                            "email": None, "project_id": None,
                            "error": f"all_extensions_failed: {error}",
                        })
                    self._log(f"[Bridge] Request {request_id} FAILED after {req['_reroute_count']} reroutes: {error}")
                else:
                    # Leave request in _pending_requests — DON'T pop, DON'T resolve
                    # Next poll from a DIFFERENT extension will pick it up
                    self._log(f"[Bridge] Re-queuing {request_id} (attempt #{req['_reroute_count']}): {error}")

                return web.json_response({"ok": True}, headers={"Access-Control-Allow-Origin": "*"})

        # ─── Normal result (success or non-routing error) ───
        req = self._pending_requests.pop(request_id, None)
        self._dispatched_to.pop(request_id, None)

        if req and req.get("future") and not req["future"].done():
            # For special actions, pass through relevant fields
            action = req.get("action", "")
            if action.startswith("DOWNLOAD_MEDIA:"):
                result = {
                    "cdn_url": data.get("cdn_url"),
                    "base64_data": data.get("base64_data"),
                    "content_type": data.get("content_type"),
                    "size": data.get("size"),
                    "error": data.get("error"),
                }
            elif action == "GET_COOKIES":
                result = {
                    "cookies": data.get("cookies"),
                    "error": data.get("error"),
                }
            elif action == "EXECUTE_FETCH":
                # Extension forwarded the labs.google API response back.
                # status / body / headers / error fields all flow through.
                result = {
                    "status": data.get("status"),
                    "body": data.get("body", ""),
                    "headers": data.get("headers", {}),
                    "error": data.get("error"),
                }
            else:
                result = {
                    "token": data.get("token"),
                    "access_token": data.get("access_token"),
                    "email": data.get("email"),
                    "project_id": data.get("project_id"),
                    "error": data.get("error"),
                }

                # Cache project ID if received
                email = data.get("email", "")
                pid = data.get("project_id")
                if email and pid:
                    self._project_ids[email] = pid

                if data.get("token"):
                    self._tokens_received += 1

            req["future"].set_result(result)

        return web.json_response({"ok": True}, headers={"Access-Control-Allow-Origin": "*"})

    async def _handle_accounts(self, request: web.Request) -> web.Response:
        """Extension reports connected accounts. MERGE (don't replace) into global dict."""
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False}, status=400)

        accounts = data.get("accounts", [])
        # MERGE — each Chrome profile reports its own accounts separately.
        # Don't clear() — that erases accounts from other profiles.
        for acc in accounts:
            email = acc.get("email", "")
            if email:
                self._connected_accounts[email] = {
                    "email": email,
                    "name": acc.get("name", ""),
                    "tab_id": acc.get("tab_id"),
                    "project_id": acc.get("project_id"),
                }
                if acc.get("project_id"):
                    self._project_ids[email] = acc["project_id"]

        if accounts:
            self._log(
                f"[Bridge] Extension reports {len(accounts)} account(s): "
                + ", ".join(a.get("email", "?") for a in accounts)
            )

        return web.json_response({"ok": True}, headers={"Access-Control-Allow-Origin": "*"})

    async def _handle_cookies(self, request: web.Request) -> web.Response:
        """Extension exports an account's Google cookies (Playwright format).

        Writes them to <sessions>/<email>/exported_cookies.json and registers
        the account in the DB (if new) so the CloakBrowser generation modes
        (HTTP Shared / Browser per slot / CDP) can log in with these cookies
        on the account's assigned proxy IP — no manual login needed.
        """
        _cors = {"Access-Control-Allow-Origin": "*"}
        try:
            data = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "error": "bad json"}, status=400, headers=_cors
            )

        email = str(data.get("email") or "").strip()
        cookies = data.get("cookies") or []
        if not email or not isinstance(cookies, list) or not cookies:
            return web.json_response(
                {"ok": False, "error": "missing email or cookies"},
                status=400, headers=_cors,
            )

        # Keep only well-formed cookies (name + value + domain required by
        # Playwright's add_cookies).
        clean = [
            c for c in cookies
            if isinstance(c, dict) and c.get("name") and c.get("domain")
            and c.get("value") is not None
        ]
        if not clean:
            return web.json_response(
                {"ok": False, "error": "no valid cookies after filtering"},
                status=400, headers=_cors,
            )

        try:
            from src.core.app_paths import get_sessions_dir
            session_dir = get_sessions_dir() / email
            session_dir.mkdir(parents=True, exist_ok=True)
            cookies_path = session_dir / "exported_cookies.json"
            with open(cookies_path, "w", encoding="utf-8") as f:
                json.dump(clean, f, indent=2)
        except Exception as e:
            self._log(f"[Bridge] Cookie export write failed for {email}: {e}")
            return web.json_response(
                {"ok": False, "error": f"write failed: {str(e)[:120]}"},
                status=500, headers=_cors,
            )

        # Register the account in the DB (idempotent) so it shows up in the
        # app's account list, ready for a CloakBrowser mode + proxy.
        registered = False
        try:
            from src.db.db_manager import get_accounts, add_account
            existing = [a for a in get_accounts() if a.get("name") == email]
            if not existing:
                add_account(email, str(session_dir), "")
                registered = True
        except Exception as e:
            # Non-fatal — cookies are saved; user can add the account manually.
            self._log(f"[Bridge] Cookie export: DB register skipped ({str(e)[:80]}).")

        self._log(
            f"[Bridge] 🍪 Cookies exported for {email}: {len(clean)} saved to "
            f"{cookies_path}"
            + (" (account registered — refresh account list)" if registered else "")
        )
        return web.json_response(
            {"ok": True, "saved": len(clean), "registered": registered,
             "path": str(cookies_path)},
            headers=_cors,
        )

    async def _handle_project(self, request: web.Request) -> web.Response:
        """Extension reports project ID for an account."""
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False}, status=400)

        account = data.get("account", "")
        project_id = data.get("project_id", "")
        if account and project_id:
            self._project_ids[account] = project_id
            self._log(f"[Bridge] Project for {account}: {project_id}")

        return web.json_response({"ok": True}, headers={"Access-Control-Allow-Origin": "*"})

    async def _handle_status(self, request: web.Request) -> web.Response:
        """App checks bridge status."""
        return web.json_response({
            "running": True,
            "extension_connected": self.is_extension_connected,
            "connected_accounts": list(self._connected_accounts.keys()),
            "tokens_received": self._tokens_received,
            "pending_requests": len(self._pending_requests),
        }, headers={"Access-Control-Allow-Origin": "*"})

    async def _handle_command_post(self, request: web.Request) -> web.Response:
        """App queues a command for the extension."""
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False}, status=400)

        self.send_command(
            data.get("type", ""),
            data.get("account", ""),
            data.get("data"),
        )
        return web.json_response({"ok": True}, headers={"Access-Control-Allow-Origin": "*"})

    # ═══════════════════════════════════════════════════════════════
    # Auto Warmup Mode (Ecosystem Activity) — Phase 1
    # ═══════════════════════════════════════════════════════════════

    async def _handle_ecosystem_status(self, request: web.Request) -> web.Response:
        """Extension/App reads current ecosystem state + directive.
        Extension polls this (alongside /poll) to know whether to run activity,
        pause, or stay disabled.
        """
        now = time.time()
        # Clean up expired holds
        expired = [a for a, ts in self._ecosystem_held_accounts.items() if ts <= now]
        for a in expired:
            self._ecosystem_held_accounts.pop(a, None)

        # Determine effective state
        if not self._ecosystem_enabled:
            directive = "disabled"
        elif self._generation_running:
            directive = "paused"
        else:
            directive = "active"

        held_list = {a: max(0, int(ts - now)) for a, ts in self._ecosystem_held_accounts.items()}

        return web.json_response({
            "directive": directive,
            "enabled": self._ecosystem_enabled,
            "generation_running": self._generation_running,
            "held_accounts": held_list,   # email -> seconds_remaining
            "stats": self._ecosystem_stats,
        }, headers={"Access-Control-Allow-Origin": "*"})

    async def _handle_ecosystem_update(self, request: web.Request) -> web.Response:
        """App or extension popup updates ecosystem state.
        Body: { "enabled": true/false } or { "generation_running": true/false }
               or { "hold_account": "email@gmail.com", "duration_seconds": 172800 }
               or { "release_account": "email@gmail.com" }
        """
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False}, status=400)

        changed = False
        if "enabled" in data:
            new_val = bool(data["enabled"])
            if new_val != self._ecosystem_enabled:
                self._ecosystem_enabled = new_val
                self._ecosystem_stats["last_state_change"] = time.time()
                self._log(
                    f"[Bridge] Ecosystem mode: {'ENABLED' if new_val else 'DISABLED'}"
                )
                # Persist to DB so it survives app restart
                try:
                    from src.db.db_manager import set_setting
                    set_setting("ecosystem_enabled", "1" if new_val else "0")
                except Exception:
                    pass
                changed = True

        if "generation_running" in data:
            new_val = bool(data["generation_running"])
            if new_val != self._generation_running:
                self._generation_running = new_val
                self._ecosystem_stats["last_state_change"] = time.time()
                self._log(
                    f"[Bridge] Ecosystem: generation {'STARTED' if new_val else 'ENDED'} "
                    f"— activity {'paused' if new_val else 'will resume'}"
                )
                changed = True

        if data.get("hold_account"):
            account = data["hold_account"]
            duration = int(data.get("duration_seconds", 172800))  # default 48h
            self._ecosystem_held_accounts[account] = time.time() + duration
            self._log(
                f"[Bridge] Ecosystem hold: {account} for {duration // 3600}h "
                f"(recaptcha flagged — activity stopped for this account)"
            )
            changed = True

        if data.get("release_account"):
            account = data["release_account"]
            if self._ecosystem_held_accounts.pop(account, None) is not None:
                self._log(f"[Bridge] Ecosystem hold released: {account}")
                changed = True
            self._force_enabled_accounts.discard(account)

        if "force_enable_account" in data:
            account = data["force_enable_account"]
            enable = bool(data.get("enable", True))
            self.force_enable_account(account, enable)
            status = "FORCE-ENABLED" if enable else "un-forced"
            self._log(
                f"[Bridge] {account} {status} by user "
                f"(hold still counting — generation allowed, warmup blocked)"
            )
            changed = True

        return web.json_response({
            "ok": True,
            "changed": changed,
            "enabled": self._ecosystem_enabled,
            "generation_running": self._generation_running,
        }, headers={"Access-Control-Allow-Origin": "*"})

    async def _handle_ecosystem_activity_report(self, request: web.Request) -> web.Response:
        """Extension reports an activity it just did (for logs + stats).
        Body: { "account": "email", "site": "youtube", "duration_sec": 180,
                "action": "start" | "end" }
        """
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False}, status=400)

        account = data.get("account", "")
        site = data.get("site", "")
        action = data.get("action", "start")
        duration = int(data.get("duration_sec", 0))

        if not account:
            return web.json_response({"ok": False}, status=400)

        accounts_stats = self._ecosystem_stats.setdefault("accounts", {})
        acct = accounts_stats.setdefault(account, {
            "last_activity": 0,
            "today_count": 0,
            "current_site": "",
            "today_date": "",
        })

        # Reset daily count on date change
        today = time.strftime("%Y-%m-%d")
        if acct.get("today_date") != today:
            acct["today_count"] = 0
            acct["today_date"] = today

        now = time.time()
        if action == "start":
            acct["current_site"] = site
            acct["last_activity"] = now
            self._log(f"[Ecosystem] {account}: started {site}")
        elif action == "end":
            acct["current_site"] = ""
            acct["last_activity"] = now
            acct["today_count"] = acct.get("today_count", 0) + 1
            self._log(
                f"[Ecosystem] {account}: finished {site} ({duration}s) "
                f"— today: {acct['today_count']} activities"
            )

        return web.json_response({"ok": True}, headers={"Access-Control-Allow-Origin": "*"})

    # ─── Python-side API for ExtensionModeManager ───

    def set_generation_running(self, running: bool):
        """Called from extension_mode.py to signal generation start/end."""
        if running != self._generation_running:
            self._generation_running = running
            self._ecosystem_stats["last_state_change"] = time.time()

    def hold_ecosystem_account(self, account: str, duration_seconds: int = 172800):
        """Hold ecosystem activity for an account (called when reCAPTCHA flags it).
        Default 48 hours. Also blocks token dispatch unless force-enabled."""
        self._ecosystem_held_accounts[account] = time.time() + duration_seconds
        # Clear any cached tokens (across all actions) for the held account
        # so stale ones aren't served once the hold expires.
        for key in [k for k in self._token_pool if k[0] == account]:
            self._token_pool.pop(key, None)
        # Reset force_enable when a new hold is applied (safety)
        self._force_enabled_accounts.discard(account)

    def release_ecosystem_account(self, account: str):
        """Manually release an account's ecosystem hold."""
        self._ecosystem_held_accounts.pop(account, None)

    def is_ecosystem_enabled(self) -> bool:
        return self._ecosystem_enabled

    def set_ecosystem_enabled(self, enabled: bool):
        self._ecosystem_enabled = bool(enabled)

    def is_account_held(self, account: str) -> bool:
        """True if the account is currently held (reCAPTCHA flagged) AND
        the user has NOT force-enabled it. Held accounts block both dispatch
        and ecosystem activity."""
        released = self._ecosystem_held_accounts.get(account, 0)
        if released <= 0 or released <= time.time():
            return False
        if account in self._force_enabled_accounts:
            return False  # user chose to override
        return True

    def force_enable_account(self, account: str, enable: bool = True):
        if enable:
            self._force_enabled_accounts.add(account)
        else:
            self._force_enabled_accounts.discard(account)

    def get_hold_info(self, account: str):
        released = self._ecosystem_held_accounts.get(account, 0)
        now = time.time()
        return {
            "held": released > now,
            "seconds_remaining": max(0, int(released - now)),
            "released_at": released,
            "force_enabled": account in self._force_enabled_accounts,
        }
