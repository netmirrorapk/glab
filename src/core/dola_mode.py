"""
Dola automation mode — Chrome Extension + DolaBridge (port 18927).

Mirrors grok_mode.py / genspark_mode.py shape. queue_manager.py calls
DolaModeManager.run() when generation_mode == "chrome_extension_dola".

The whole dola pipeline (submit → poll → get_play_info → mp4 bytes) runs
inside the dola.com tab via the extension (dola.js). This mode just dispatches
prompts to the bridge and saves the returned video bytes. No browser is
launched by Python — it rides on the user's already-logged-in real Chrome.
"""

import asyncio
import base64
import os
import random
import time
from typing import Any, Dict, List, Optional

from src.core.dola_bridge import DolaBridge
from src.db.db_manager import (
    get_all_jobs,
    get_output_directory,
    get_setting,
    update_job_runtime_state,
    update_job_status,
)

# ─── Settings resolution helpers ───
_ALLOWED_RATIO = {"1:1", "3:4", "4:3", "9:16", "16:9", "21:9"}
_ALLOWED_DURATION = {5, 10}
_ALLOWED_MODELS = {"seedance_v2.5", "seedance_v2.0", "ic_mini"}


def _resolve_dola_ratio(ratio_name: str) -> str:
    """Resolve a UI ratio label to one of dola's accepted ratios. Default 9:16."""
    raw = str(ratio_name or "").strip()
    if raw in _ALLOWED_RATIO:
        return raw
    low = raw.lower()
    for token in ("9:16", "16:9", "21:9", "3:4", "4:3", "1:1"):
        if token in raw:
            return token
    if "portrait" in low:
        return "9:16"
    if "square" in low:
        return "1:1"
    if "landscape" in low:
        return "16:9"
    return "9:16"


def _resolve_dola_duration(length: Any) -> int:
    try:
        n = int(str(length).lower().replace("s", "").strip())
        if n in _ALLOWED_DURATION:
            return n
        # snap anything else to the nearest allowed value
        return 5 if n <= 7 else 10
    except Exception:
        return 10


def _resolve_dola_model(model_name: str) -> str:
    raw = str(model_name or "").strip()
    if raw in _ALLOWED_MODELS:
        return raw
    low = raw.lower()
    if "2.5" in low or "seedance_v2.5" in low:
        return "seedance_v2.5"
    if "1.0" in low or "ic_mini" in low or "seedance 1" in low:
        return "ic_mini"
    return "seedance_v2.0"


def _safe_filename(job_id: str, idx: int, ext: str = "mp4") -> str:
    safe = "".join(c for c in str(job_id) if c.isalnum() or c in "._-")[:40]
    return f"{safe or 'video'}_{idx}.{ext}"


def _queue_output_number(job: Dict[str, Any]) -> Optional[int]:
    """Stable 1-based queue number for a job (mirrors Flow/Grok naming)."""
    for key in ("output_index", "queue_no"):
        raw = job.get(key)
        if raw is None:
            continue
        try:
            val = int(raw)
        except (TypeError, ValueError):
            continue
        if val > 0:
            return val
    return None


class DolaWorker:
    """A worker slot for a single dola.com account/tab."""

    def __init__(self, slot_id: str, account_email: str, bridge: DolaBridge, log_fn):
        self.slot_id = slot_id
        self.account_email = account_email
        self._bridge = bridge
        self._log = log_fn
        self.is_busy = False


class DolaModeManager:
    """Dola automation mode — Chrome Extension + DolaBridge."""

    DISPATCH_STAGGER_MIN = 1.5
    DISPATCH_STAGGER_MAX = 3.0

    def __init__(self, queue_manager):
        self.qm = queue_manager
        self._log = lambda msg: queue_manager.signals.log_msg.emit(msg)
        self._bridge = DolaBridge(self._log)
        self._workers: Dict[str, List[DolaWorker]] = {}
        self._active_tasks: List[asyncio.Task] = []
        # Settings snapshot loaded at run() start
        self._model = "seedance_v2.0"
        self._ratio = "9:16"
        self._duration = 10
        # Auto-delete-after-quota state
        self._auto_delete = False
        self._success_count: Dict[str, int] = {}   # account -> successful gens
        self._deleted_accounts: set = set()          # accounts already deleted/burned
        self._deleting: set = set()                  # accounts with a delete in flight
        self._pending_delete: set = set()            # hit daily-limit; delete once all its gens finish
        self._dry_delete_cycles = 0                  # deletes with NO successful gen in between
        self._max_dry_delete_cycles = 20             # backstop only; a successful gen resets to 0. Raised
                                                     # from 3 so a batch of already-exhausted accounts at the
                                                     # start of a run doesn't false-trip the burn-recreate.
        self._timeout_retries: Dict[str, int] = {}   # job_id -> times re-queued after a slow/not-ready timeout
        self._max_timeout_retries = 2                # give a slow video this many extra chances before failing
        self._logged_out_until: Dict[str, float] = {}  # account -> ts until which to skip it (logged out / re-login didn't take)
        self._logged_out_cooldown = 90.0             # seconds to skip a logged-out account before retrying it
        self._exhausted_accounts: set = set()        # accounts that hit daily-limit this run → skip (no re-hammer)

    @staticmethod
    def _delete_threshold(model: str) -> int:
        """Per-model daily quota → how many gens before auto-delete.
        Seedance 2.5 = 2/day, 2.0 Fast = 4/day (user-observed)."""
        m = str(model or "").lower()
        if "2.5" in m:
            return 2
        if "2.0" in m or "seedance_v2.0" in m:
            return 4
        return 4

    # ═══════════════════════════════════════════════════════════════
    # Main loop
    # ═══════════════════════════════════════════════════════════════

    async def run(self) -> None:
        self._log("[DolaMode] Starting dola.com automation mode...")
        try:
            await self._bridge.start()
        except Exception as e:
            self._log(f"[DolaMode] Bridge failed to start: {e}")
            return

        try:
            self._model = _resolve_dola_model(get_setting("dola_model", "seedance_v2.0"))
            self._ratio = _resolve_dola_ratio(get_setting("dola_ratio", "9:16"))
            self._duration = _resolve_dola_duration(get_setting("dola_duration", "10"))
            # Parallel target per account = the UI "Parallel/account" slider. The
            # extension auto-opens this many dola.com tabs (each tab = 1 concurrent
            # generation), and work starts on each tab as soon as it connects.
            try:
                desired_tabs = int(str(get_setting("slots_per_account", "1") or "1"))
            except Exception:
                desired_tabs = 1
            desired_tabs = max(1, min(8, desired_tabs))
            self._bridge.set_desired_tabs(desired_tabs)
            self._auto_delete = str(get_setting("dola_auto_delete", "1") or "1").strip() in ("1", "true", "True", "on", "yes")
            self._log(
                f"[DolaMode] Settings → model={self._model}, ratio={self._ratio}, "
                f"duration={self._duration}s, parallel target={desired_tabs} tab(s)/account"
            )
            if self._auto_delete:
                self._log(
                    "[DolaMode] Auto-delete ON — account deleted ONLY when its real "
                    "'daily limit' message appears, and only after all its generations finish."
                )
            self._log(
                "[DolaMode] Waiting for Chrome Extension to connect...\n"
                "  Make sure Chrome is open with the G-Labs Helper extension\n"
                "  and a logged-in https://www.dola.com/ tab is open."
            )

            wait_start = time.time()
            while not self._bridge.is_extension_connected():
                if self.qm.stop_requested or self.qm.force_stop_requested:
                    return
                if time.time() - wait_start > 60:
                    self._log("[DolaMode] Extension did not connect. Aborting.")
                    return
                await asyncio.sleep(1)

            # Give the extension up to ~20s to report dola accounts.
            await asyncio.sleep(4)
            connected = self._bridge.get_accounts()
            stable = 0
            prev_count = len(connected)
            for _ in range(20):
                if self.qm.stop_requested or self.qm.force_stop_requested:
                    return
                await asyncio.sleep(1)
                connected = self._bridge.get_accounts()
                if len(connected) == prev_count and prev_count > 0:
                    stable += 1
                    if stable >= 4:
                        break
                else:
                    stable = 0
                    prev_count = len(connected)

            if not connected:
                self._log(
                    "[DolaMode] No dola.com accounts detected.\n"
                    "  Open https://www.dola.com/ and log in, then retry."
                )
                wait_start = time.time()
                while not connected:
                    if self.qm.stop_requested or self.qm.force_stop_requested:
                        return
                    if time.time() - wait_start > 60:
                        self._log("[DolaMode] No accounts found. Aborting.")
                        return
                    await asyncio.sleep(3)
                    connected = self._bridge.get_accounts()

            account_names = [a["email"] for a in connected]
            self._log(
                f"[DolaMode] Found {len(connected)} account(s): " + ", ".join(account_names)
            )

            for info in connected:
                email = info["email"]
                tab_count = max(1, int(info.get("tab_count", 1) or 1))
                workers = []
                for idx in range(1, tab_count + 1):
                    slot_id = f"{email}#dl{idx}"
                    workers.append(DolaWorker(slot_id, email, self._bridge, self._log))
                self._workers[email] = workers
                self._log(
                    f"[DolaMode] {email}: {len(workers)} worker(s) ready "
                    f"(matches {tab_count} open tab(s))."
                )

            total_workers = sum(len(w) for w in self._workers.values())
            if total_workers == 0:
                self._log("[DolaMode] No workers started.")
                return

            self._log(
                f"[DolaMode] Total: {total_workers} worker(s) across "
                f"{len(self._workers)} account(s). No browser launched by Python."
            )

            last_heartbeat = 0.0
            heartbeat_interval = 20.0

            while self.qm.is_running:
                if self.qm.stop_requested or self.qm.force_stop_requested:
                    break
                if getattr(self.qm, "pause_requested", False):
                    await asyncio.sleep(1)
                    continue

                # Dynamic account discovery + tab scaling — the extension auto-opens
                # tabs for the parallel target, so an existing account's tab_count
                # grows over the first ~15s. Add workers to match (each tab = 1 slot).
                current = self._bridge.get_accounts()
                for info in current:
                    email = info.get("email", "")
                    if not email or email in self._deleted_accounts:
                        continue  # burned account — don't (re-)add
                    tab_count = max(1, int(info.get("tab_count", 1) or 1))
                    if email not in self._workers:
                        workers = []
                        for idx in range(1, tab_count + 1):
                            slot_id = f"{email}#dl{idx}"
                            workers.append(DolaWorker(slot_id, email, self._bridge, self._log))
                        self._workers[email] = workers
                        self._log(
                            f"[DolaMode] New account: {email} — {len(workers)} worker(s) added."
                        )
                    else:
                        existing = self._workers.get(email, [])
                        if tab_count > len(existing):
                            for idx in range(len(existing) + 1, tab_count + 1):
                                slot_id = f"{email}#dl{idx}"
                                existing.append(DolaWorker(slot_id, email, self._bridge, self._log))
                            self._workers[email] = existing
                            self._log(
                                f"[DolaMode] {email}: scaled to {len(existing)} worker(s) "
                                f"({tab_count} tab(s) now open) — parallel ready."
                            )

                self._active_tasks = [t for t in self._active_tasks if not t.done()]

                # Auto-delete: an account is queued for deletion ONLY when its real
                # daily-limit message was seen. Delete strictly AFTER all of that
                # account's in-flight generations have finished (no prediction).
                if self._auto_delete and self._pending_delete:
                    for acct in list(self._pending_delete):
                        if acct in self._deleted_accounts:
                            self._pending_delete.discard(acct)
                            continue
                        if acct in self._deleting:
                            continue  # delete already in flight
                        workers = self._workers.get(acct, [])
                        # Trigger delete only when ALL of this account's tabs are idle
                        # (keep it in _pending_delete so dispatch stays blocked).
                        if workers and all(not w.is_busy for w in workers):
                            self._log(f"[DolaMode] {acct}: all generations finished → deleting now (daily limit).")
                            t = asyncio.create_task(self._maybe_delete_account(acct, "daily_limit (all gens done)"))
                            self._active_tasks.append(t)

                jobs = get_all_jobs() or []
                # Dola handles VIDEO jobs only.
                pending = [
                    j for j in jobs
                    if j.get("status") == "pending"
                    and str(j.get("job_type") or "").lower() == "video"
                ]

                now_ts = time.time()
                if now_ts - last_heartbeat > heartbeat_interval:
                    running_count = sum(1 for j in jobs if j.get("status") == "running")
                    failed_count = sum(1 for j in jobs if j.get("status") == "failed")
                    done_count = sum(1 for j in jobs if j.get("status") == "completed")
                    self._log(
                        f"[DolaMode] ⏱ waiting — pending={len(pending)}, "
                        f"running={running_count}, done={done_count}, "
                        f"failed={failed_count}, active_tasks={len(self._active_tasks)}"
                    )
                    last_heartbeat = now_ts

                if not pending:
                    if not self._active_tasks:
                        still_active = any(
                            j.get("status") in ("pending", "running")
                            for j in (get_all_jobs() or [])
                        )
                        if not still_active:
                            self._log(
                                "[DolaMode] All jobs completed (or failed). Stopping dola mode."
                            )
                            break
                    await asyncio.sleep(self.qm.scheduler_poll_seconds)
                    continue

                busy_slots = {
                    t.get_name() for t in self._active_tasks if hasattr(t, "get_name")
                }

                dispatched = 0
                for job in pending:
                    if self.qm.stop_requested or self.qm.force_stop_requested:
                        break
                    worker = self._get_available_worker(busy_slots)
                    if not worker:
                        break

                    job_id = job["id"]
                    prompt_preview = str(job.get("video_prompt") or job.get("prompt") or "")[:60]
                    self._log(
                        f"[DolaMode] Dispatching job {str(job_id)[:6]}... to "
                        f"{worker.slot_id} | prompt: \"{prompt_preview}\""
                    )
                    update_job_status(job_id, "running", account=worker.account_email)
                    self.qm.signals.job_updated.emit(job_id, "running", worker.account_email, "")

                    task = asyncio.create_task(
                        self._run_job(worker, job), name=worker.slot_id,
                    )
                    self._active_tasks.append(task)
                    busy_slots.add(worker.slot_id)
                    dispatched += 1

                    stagger = random.uniform(self.DISPATCH_STAGGER_MIN, self.DISPATCH_STAGGER_MAX)
                    if stagger > 0:
                        await asyncio.sleep(stagger)

                if dispatched == 0:
                    # If nothing could be dispatched, nothing is running, and EVERY
                    # known account is exhausted (daily limit) — the whole fleet is
                    # out of quota for the day. Don't spin forever leaving the queue
                    # "running": stop cleanly, leaving the remaining jobs PENDING for
                    # the next run (or after adding accounts / a daily reset).
                    if not self._active_tasks and self._workers:
                        usable = [
                            e for e in self._workers
                            if e not in self._exhausted_accounts
                            and e not in self._deleted_accounts
                            and e not in self._pending_delete
                            and e not in self._deleting
                            and time.time() >= self._logged_out_until.get(e, 0)
                        ]
                        if not usable:
                            self._log(
                                f"[DolaMode] ⛔ All {len(self._workers)} account(s) have hit their daily "
                                f"limit — {len(pending)} job(s) left PENDING. Add more accounts (each in its "
                                f"own Chrome profile) or resume after the daily quota resets. Stopping."
                            )
                            break
                    await asyncio.sleep(self.qm.scheduler_poll_seconds)

            # Drain remaining tasks
            if self._active_tasks:
                if self.qm.stop_requested or self.qm.force_stop_requested:
                    self._log(f"[DolaMode] Cancelling {len(self._active_tasks)} task(s)...")
                    for t in self._active_tasks:
                        if not t.done():
                            t.cancel()
                    try:
                        await asyncio.wait_for(
                            asyncio.gather(*self._active_tasks, return_exceptions=True),
                            timeout=5.0,
                        )
                    except asyncio.TimeoutError:
                        self._log("[DolaMode] Some tasks didn't cancel in 5s.")
                else:
                    self._log(f"[DolaMode] Waiting for {len(self._active_tasks)} task(s)...")
                    await asyncio.gather(*self._active_tasks, return_exceptions=True)

        finally:
            try:
                self._bridge.cancel_all_pending()
            except Exception:
                pass
            try:
                await self._bridge.stop()
            except Exception:
                pass
            self._workers.clear()
            self._log("[DolaMode] Dola mode stopped.")

    # ═══════════════════════════════════════════════════════════════
    # Worker picker
    # ═══════════════════════════════════════════════════════════════

    def _get_available_worker(self, busy_slots: set) -> Optional[DolaWorker]:
        if not self._workers:
            return None
        now = time.time()
        for email, workers in self._workers.items():
            # Skip accounts queued for deletion, mid-delete, or already deleted —
            # so no new jobs land on tabs that are about to be closed/re-logged-in.
            if email in self._pending_delete or email in self._deleting or email in self._deleted_accounts:
                continue
            # Account already hit its daily limit this run → its quota is gone for
            # the day. Skip it entirely so we don't re-dispatch (and re-hammer) it
            # dozens of times — that hammering is what makes dola rate-limit and
            # return "no conversation_id in SSE". Each account does its ~1 video,
            # then steps aside for the next account.
            if email in self._exhausted_accounts:
                continue
            # Logged-out account (re-login didn't take — e.g. it isn't the active
            # Google account in this single Chrome). Skip for a cooldown so its
            # jobs route to an account that IS logged in, instead of failing them
            # over and over. Retried automatically after the cooldown.
            if now < self._logged_out_until.get(email, 0):
                continue
            if getattr(self.qm, "account_disabled", {}).get(email):
                continue
            pause_until = getattr(self.qm, "account_pause_until", {}).get(email, 0)
            if pause_until and now < pause_until:
                continue
            for w in workers:
                if w.slot_id not in busy_slots and not w.is_busy:
                    return w
        return None

    # ═══════════════════════════════════════════════════════════════
    # Per-job execution
    # ═══════════════════════════════════════════════════════════════

    async def _maybe_delete_account(self, account: str, reason: str) -> None:
        """Auto-delete an account from dola.com via the extension (drives the
        /delete-account page in the tab). Marks it burned so dispatch skips it and
        the queue continues on other accounts. Opt-in (dola_auto_delete)."""
        if not self._auto_delete or not account:
            return
        if account in self._deleted_accounts or account in self._deleting:
            return
        self._deleting.add(account)
        self._log(f"[DolaMode] 🗑 Auto-delete triggered for {account} ({reason}). Deleting from dola.com...")
        try:
            rid, future = self._bridge.submit_request(
                account=account, prompt="", command="delete_account",
            )
            result = await self._bridge.wait_for_result(
                rid, future, idle_timeout_s=120.0, max_total_s=300.0,
            )
        except Exception as e:
            self._deleting.discard(account)
            self._log(f"[DolaMode] Auto-delete error for {account}: {str(e)[:150]}")
            return
        if result and result.get("success"):
            self._workers.pop(account, None)
            self._deleting.discard(account)
            self._pending_delete.discard(account)
            self._success_count.pop(account, None)
            self._dry_delete_cycles += 1
            if result.get("relogged"):
                # Same/new account is back and usable — do NOT permanently block it;
                # discovery will re-add its workers and the pending jobs resume.
                self._log(
                    f"[DolaMode] ✅ {account} deleted + auto re-logged in — "
                    f"account will be re-detected and reused for the pending jobs."
                )
            else:
                self._deleted_accounts.add(account)
                self._log(f"[DolaMode] ✅ {account} deleted. Continuing on other accounts.")
            # Safety: if we keep deleting without any successful generation in
            # between, re-login is NOT resetting the quota → stop the loop.
            if self._dry_delete_cycles >= self._max_dry_delete_cycles:
                self._auto_delete = False
                self._log(
                    f"[DolaMode] ⚠️ {self._dry_delete_cycles} deletes with no successful generation in between — "
                    f"re-login isn't resetting the daily quota. Auto-delete DISABLED for this run (safety)."
                )
        else:
            self._deleting.discard(account)
            err = (result or {}).get("error", "unknown")
            self._log(f"[DolaMode] ⚠️ Auto-delete failed for {account}: {err}")

    async def _run_job(self, worker: DolaWorker, job: Dict[str, Any]) -> None:
        worker.is_busy = True
        job_id = job.get("id", "")
        prompt = str(job.get("video_prompt") or job.get("prompt") or "").strip()

        # The dola-specific combos (run-wide settings) are the source of truth.
        # A per-job field only wins if it is ALREADY a valid dola value — the Veo
        # Quality/Ratio values that job creation writes are meaningless for dola,
        # so they fall through to the dola run-wide settings.
        raw_model = str(job.get("video_model") or "")
        job_model = raw_model if raw_model in _ALLOWED_MODELS else self._model
        raw_ratio = str(job.get("video_ratio") or "")
        job_ratio = raw_ratio if raw_ratio in _ALLOWED_RATIO else self._ratio
        raw_dur = str(job.get("video_length") or "").strip()
        job_duration = _resolve_dola_duration(raw_dur) if raw_dur in {"5", "10", "6"} else self._duration

        if not prompt:
            update_job_status(job_id, "failed", account=worker.account_email, error="empty_prompt")
            self.qm.signals.job_updated.emit(job_id, "failed", worker.account_email, "empty_prompt")
            worker.is_busy = False
            return

        # Reference image (Video + Ref) — read from any field job creation may set,
        # base64 it, and let the extension upload it via dola's own ImageX SDK.
        ref_b64 = ""
        ref_name = ""
        ref_mime = "image/png"
        ref_path = ""
        for field in ("start_image_path", "ref_path", "ref_paths", "image_path"):
            raw = job.get(field)
            if not raw:
                continue
            s = str(raw).strip()
            if s.startswith("["):
                try:
                    import json as _json
                    arr = _json.loads(s)
                    if isinstance(arr, list) and arr:
                        s = str(arr[0]).strip()
                except Exception:
                    pass
            for sep in (";", ",", "\n"):
                if sep in s:
                    s = s.split(sep, 1)[0].strip()
            if s:
                ref_path = s
                break
        if ref_path and os.path.isfile(ref_path):
            try:
                import mimetypes as _mt
                with open(ref_path, "rb") as fh:
                    raw_bytes = fh.read()
                ref_b64 = base64.b64encode(raw_bytes).decode("ascii")
                ref_name = os.path.basename(ref_path)
                ref_mime = _mt.guess_type(ref_path)[0] or "image/png"
                self._log(f"[{worker.slot_id}] Reference image: {ref_name} ({len(raw_bytes)} bytes)")
            except Exception as e:
                self._log(f"[{worker.slot_id}] Reference read failed: {e} — falling back to text-to-video")
                ref_b64 = ""

        self._log(
            f"[{worker.slot_id}] Dola config → model={job_model}, ratio={job_ratio}, "
            f"duration={job_duration}s, ref={'yes' if ref_b64 else 'no'}"
        )

        try:
            rid, future = self._bridge.submit_request(
                account=worker.account_email,
                prompt=prompt,
                model=job_model,
                ratio=job_ratio,
                duration=job_duration,
                reference_image_base64=ref_b64,
                reference_image_filename=ref_name,
                reference_image_mime=ref_mime,
            )
        except Exception as e:
            self._log(f"[{worker.slot_id}] Bridge submit failed: {e}")
            update_job_status(job_id, "failed", account=worker.account_email, error=f"bridge_submit: {e}"[:500])
            self.qm.signals.job_updated.emit(job_id, "failed", worker.account_email, "bridge_submit")
            worker.is_busy = False
            return

        self._log(f"[{worker.slot_id}] Dispatched to extension — waiting for result...")

        try:
            result = await self._bridge.wait_for_result(
                rid, future, idle_timeout_s=300.0, max_total_s=1800.0,
            )
        except asyncio.CancelledError:
            worker.is_busy = False
            raise
        except Exception as e:
            self._log(f"[{worker.slot_id}] Bridge wait failed: {e}")
            update_job_status(job_id, "failed", account=worker.account_email, error=str(e)[:500])
            self.qm.signals.job_updated.emit(job_id, "failed", worker.account_email, str(e)[:120])
            worker.is_busy = False
            return

        if not result or result.get("error"):
            err = (result or {}).get("error", "unknown_error")
            detail = (result or {}).get("detail", "")
            msg = f"{err}" + (f" — {detail[:200]}" if detail else "")
            self._log(f"[{worker.slot_id}] Dola returned error: {msg}")
            if "daily_limit" in str(err).lower():
                # Don't fail the job — re-queue it as PENDING so it runs after the
                # account is refreshed (delete+relogin) or on another account.
                update_job_status(job_id, "pending", account="", error="dola_daily_limit (re-queued)")
                self.qm.signals.job_updated.emit(job_id, "pending", "", "daily_limit_requeued")
                if self._auto_delete and worker.account_email not in self._deleted_accounts:
                    # BURN-RECREATE (user-verified this DOES reset the daily quota):
                    # queue the account for delete → same-Gmail re-login → fresh
                    # quota → reuse. Only _pending_delete here (NOT _exhausted) —
                    # _pending_delete blocks new dispatch until the delete finishes,
                    # and after re-login the account is re-detected and used again.
                    # Adding it to _exhausted_accounts would permanently skip it and
                    # defeat the whole burn-recreate cycle.
                    if worker.account_email not in self._pending_delete:
                        self._pending_delete.add(worker.account_email)
                        self._log(
                            f"[DolaMode] {worker.account_email}: DAILY LIMIT — queued for "
                            f"delete + re-login (fresh quota) once its generations finish."
                        )
                else:
                    # Auto-delete OFF → can't refresh quota, so just skip this
                    # account for the run (no re-hammering) and let jobs route to
                    # other accounts that still have quota.
                    if worker.account_email and worker.account_email not in self._exhausted_accounts:
                        self._exhausted_accounts.add(worker.account_email)
                        self._log(
                            f"[DolaMode] {worker.account_email}: daily limit — EXHAUSTED for this run, "
                            f"skipping (auto-delete OFF, so no quota refresh)."
                        )
                worker.is_busy = False
                return
            elif ("content_refused" in str(err).lower() or "moderat" in str(err).lower()
                  or "no_video_gen" in str(err).lower()):
                # dola REFUSED this specific prompt — either an explicit content
                # refusal, OR the ambiguous "no video queued + no gen text" case
                # (almost always a refusal with wording we didn't match). The
                # ACCOUNT is fine — do NOT mark it exhausted (that would wrongly
                # skip an account that still has quota) and do NOT requeue (it
                # would just be refused again). Fail only THIS job so the user can
                # fix the prompt; other prompts keep running on the same account.
                update_job_status(job_id, "failed", account=worker.account_email,
                                  error="dola_prompt_refused (rejected — account still OK)")
                self.qm.signals.job_updated.emit(job_id, "failed", worker.account_email, "prompt_refused")
                self._log(f"[{worker.slot_id}] Prompt refused/no-video by dola — job failed (account still usable).")
                worker.is_busy = False
                return
            elif "no conversation_id" in str(err).lower():
                # dola returned a 200 with no conversation_id — almost always
                # transient rate-limiting from too many rapid submits (the
                # hammering the exhausted-skip above now prevents). Re-queue so it
                # retries on another account instead of counting as a hard failure.
                update_job_status(job_id, "pending", account="", error="dola_no_conv (rate-limited, re-queued)")
                self.qm.signals.job_updated.emit(job_id, "pending", "", "no_conv_requeued")
                self._log(f"[{worker.slot_id}] No conversation_id (rate-limited) — re-queued to PENDING.")
                worker.is_busy = False
                return
            elif ("ref_upload_failed" in str(err).lower() or "no uri" in str(err).lower()
                  or "idle_timeout" in str(err).lower() or "no activity from extension" in str(err).lower()):
                # Reference-image upload didn't return a uri, OR the extension
                # went silent (idle_timeout) — both are transient/infra hiccups
                # (a stuck tab after heavy delete/relogin churn, a half-settled
                # session, or a dormant service worker), NOT a bad job. Re-queue
                # to PENDING (bounded) so it retries on a fresh tab/dispatch
                # instead of being thrown away.
                tries = self._timeout_retries.get(job_id, 0) + 1
                self._timeout_retries[job_id] = tries
                if tries <= self._max_timeout_retries:
                    update_job_status(job_id, "pending", account="",
                                      error=f"ref_upload_retry ({tries}/{self._max_timeout_retries})")
                    self.qm.signals.job_updated.emit(job_id, "pending", "", "ref_upload_requeued")
                    self._log(
                        f"[{worker.slot_id}] Reference upload didn't settle — re-queued to PENDING "
                        f"(retry {tries}/{self._max_timeout_retries})."
                    )
                    worker.is_busy = False
                    return
                self._log(f"[{worker.slot_id}] Reference upload failed {tries}× — marking failed.")
                update_job_status(job_id, "failed", account=worker.account_email, error=f"dola: {msg}"[:500])
                self.qm.signals.job_updated.emit(job_id, "failed", worker.account_email, str(err)[:120])
                worker.is_busy = False
                return
            elif ("not_ready_timeout" in str(err).lower()
                  or "not_started" in str(err).lower()
                  or "video_generation_not_started" in str(err).lower()):
                # The video was still generating (dola is slow, esp. the 3rd
                # parallel job) or the gen signal hadn't landed yet — this is
                # NOT a real failure. Re-queue as PENDING so it retries on a
                # free tab instead of throwing away a video that may finish.
                # Bounded so a genuinely stuck job can't loop forever.
                tries = self._timeout_retries.get(job_id, 0) + 1
                self._timeout_retries[job_id] = tries
                if tries <= self._max_timeout_retries:
                    update_job_status(job_id, "pending", account="",
                                      error=f"dola_slow_timeout (retry {tries}/{self._max_timeout_retries})")
                    self.qm.signals.job_updated.emit(job_id, "pending", "", "slow_timeout_requeued")
                    self._log(
                        f"[{worker.slot_id}] Video still rendering (slow) — re-queued to PENDING "
                        f"(retry {tries}/{self._max_timeout_retries})."
                    )
                    worker.is_busy = False
                    return
                self._log(
                    f"[{worker.slot_id}] Video timed out {tries}× — giving up (marking failed)."
                )
                update_job_status(job_id, "failed", account=worker.account_email, error=f"dola: {msg}"[:500])
            elif "not_logged_in" in str(err).lower() or "logged out" in str(err).lower():
                # The tab for this account is logged out and the shared re-login
                # didn't take. In a single real Chrome only ONE Google/dola
                # account can be active at a time — extra accounts share the same
                # Google session and can't be logged in simultaneously. NEVER
                # fail the job for this: put it back to PENDING and put the dead
                # account on cooldown so jobs route to the account that IS active.
                update_job_status(job_id, "pending", account="",
                                  error="not_logged_in (re-queued; account not active in this Chrome)")
                self.qm.signals.job_updated.emit(job_id, "pending", "", "not_logged_in_requeued")
                self._logged_out_until[worker.account_email] = time.time() + self._logged_out_cooldown
                self._log(
                    f"[{worker.slot_id}] Not logged in — job re-queued to PENDING; "
                    f"{worker.account_email} paused {int(self._logged_out_cooldown)}s "
                    f"(not the active Google account in this Chrome)."
                )
                worker.is_busy = False
                return
            elif "refused" in str(err).lower() or "moderat" in str(err).lower():
                update_job_status(job_id, "failed", account=worker.account_email, error=f"dola_moderated: {msg}"[:500])
            else:
                update_job_status(job_id, "failed", account=worker.account_email, error=f"dola: {msg}"[:500])
            self.qm.signals.job_updated.emit(job_id, "failed", worker.account_email, str(err)[:120])
            worker.is_busy = False
            return

        # Resolve output path.
        out_dir = get_output_directory() or os.getcwd()
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception:
            pass
        fname = job.get("output_filename", "")
        if not fname:
            qno = _queue_output_number(job)
            fname = f"{qno}.mp4" if qno is not None else _safe_filename(job_id, 1)
        if not fname.lower().endswith(".mp4"):
            fname += ".mp4"
        out_path = os.path.join(out_dir, fname)

        # Prefer the base64 bytes fetched in-tab; fall back to downloading the URL.
        b64 = result.get("video_base64", "")
        video_bytes = b""
        if b64:
            try:
                video_bytes = base64.b64decode(b64)
            except Exception as e:
                self._log(f"[{worker.slot_id}] base64 decode failed: {e}")
                video_bytes = b""
        if not video_bytes:
            url = result.get("video_url", "")
            if url:
                video_bytes = await self._download_url(url)

        if not video_bytes:
            self._log(f"[{worker.slot_id}] Success reported but no video bytes/URL usable")
            update_job_status(job_id, "failed", account=worker.account_email, error="dola_no_bytes")
            self.qm.signals.job_updated.emit(job_id, "failed", worker.account_email, "dola_no_bytes")
            worker.is_busy = False
            return

        try:
            with open(out_path, "wb") as fh:
                fh.write(video_bytes)
            self._log(f"[{worker.slot_id}] Saved: {fname} ({len(video_bytes)} bytes)")
            update_job_runtime_state(job_id, output_path=out_path)
            update_job_status(job_id, "completed", account=worker.account_email)
            self.qm.signals.job_updated.emit(job_id, "completed", worker.account_email, "")
            self._log(f"[{worker.slot_id}] Job {str(job_id)[:6]}… completed! ({out_path})")
            # A real generation succeeded → re-login IS giving fresh quota; reset
            # the dry-delete safety counter so burn-and-recreate can keep going.
            self._dry_delete_cycles = 0
            # This account is clearly logged in and working now — clear any
            # logged-out cooldown so it's fully available again.
            self._logged_out_until.pop(worker.account_email, None)
            # BACKEND-ACCURATE quota signal: dola's own reply told us how many
            # video points remain today ("You still have N points left today").
            # When it hits 0 the NEXT submit is guaranteed to hit the daily
            # limit — so retire this account right after its in-flight gens
            # finish, instead of burning a wasted submit+poll on the limit.
            # This is NOT a prediction/count (user requirement) — it's dola's
            # authoritative number. Deletion still happens only after all this
            # account's generations complete (same _pending_delete gate).
            try:
                pl = result.get("points_left", None)
            except Exception:
                pl = None
            if (self._auto_delete and pl is not None and int(pl) <= 0
                    and worker.account_email not in self._deleted_accounts
                    and worker.account_email not in self._pending_delete):
                self._pending_delete.add(worker.account_email)
                self._log(
                    f"[DolaMode] {worker.account_email}: backend says 0 points left today "
                    f"→ queued for deletion once its remaining generations finish."
                )
        except Exception as e:
            self._log(f"[{worker.slot_id}] Save failed: {e}")
            update_job_status(job_id, "failed", account=worker.account_email, error=f"save_error: {e}"[:500])
            self.qm.signals.job_updated.emit(job_id, "failed", worker.account_email, "save_error")
        finally:
            worker.is_busy = False

    async def _download_url(self, url: str) -> bytes:
        """Fallback: download the mp4 URL directly (used only if the extension
        couldn't return bytes). dola CDN URLs are usually token-signed/public."""
        try:
            import aiohttp
            async with aiohttp.ClientSession() as sess:
                async with sess.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                    if resp.status == 200:
                        return await resp.read()
        except Exception as e:
            self._log(f"[DolaMode] URL download failed: {str(e)[:100]}")
        return b""
