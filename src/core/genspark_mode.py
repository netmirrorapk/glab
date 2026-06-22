"""
Genspark Mode — Chrome Extension + Genspark.ai direct API calls.

Parallel architecture to extension_mode.py (Flow), but for genspark.ai:
  - Separate HTTP bridge on port 18925
  - Cookies-only auth (no Bearer token dance)
  - Unlimited generation on Plus/Pro plans (so no per-day quota hustle)
  - LLM agent wraps prompts before calling Nano Banana

This file deliberately does NOT import from extension_mode.py — the two
modes are isolated so a change to Flow never breaks Genspark and vice versa.
The queue_manager picks which one to run based on the `generation_mode`
setting.
"""

import asyncio
import base64
import os
import random
import re
import time
import uuid
from typing import Any, Dict, List, Optional

from src.core.genspark_bridge import GensparkBridge
from src.db.db_manager import (
    get_all_jobs,
    get_bool_setting,
    get_output_directory,
    update_job_status,
)


# ─── Aspect-ratio mapping (app format → Genspark format) ───
# Genspark accepts "16:9", "9:16", "1:1", "4:3", "3:4", "3:2", "2:3",
# "5:4", "4:5", "21:9", or "auto".
def _resolve_aspect_ratio(ratio_name: str) -> str:
    raw = str(ratio_name or "").strip()
    low = raw.lower()
    if not raw:
        return "auto"
    if raw in {"auto", "16:9", "9:16", "1:1", "4:3", "3:4", "3:2", "2:3",
              "5:4", "4:5", "21:9"}:
        return raw
    # Map IMAGE_ASPECT_RATIO_* identifiers used internally
    if "portrait" in low or "9:16" in low:
        return "9:16"
    if "square" in low or "1:1" in low:
        return "1:1"
    if "4:3" in low:
        return "4:3"
    if "3:4" in low:
        return "3:4"
    if "landscape" in low:
        return "16:9"
    return "auto"


def _resolve_model(model_name: str) -> str:
    """Map UI model name → Genspark model key.

    Genspark currently exposes:
      - nano-banana-2          (2K, Plus plan unlimited)
      - nano-banana-pro        (4K, Pro plan unlimited)
    """
    low = str(model_name or "").strip().lower()
    if "pro" in low and "nano" in low:
        return "nano-banana-pro"
    if "nano" in low:
        return "nano-banana-2"
    # Pass through if already a Genspark model key
    if low in {"nano-banana-2", "nano-banana-pro"}:
        return low
    # Fallback to nano-banana-2 for anything else
    return "nano-banana-2"


def _resolve_image_size(size_name: str) -> str:
    """Map UI quality label → Genspark image_size string.

    Valid values: "auto", "0.5k", "1k", "2k", "4k"
    """
    raw = str(size_name or "").strip().lower().replace(" ", "")
    if raw in {"auto", "0.5k", "1k", "2k", "4k"}:
        return raw
    # Accept common variations
    if raw in {"0.5", "512", "512px", "halfk"}: return "0.5k"
    if raw in {"1", "1024", "1024px"}: return "1k"
    if raw in {"2", "2048", "2048px"}: return "2k"
    if raw in {"4", "4096", "4096px"}: return "4k"
    return "auto"


class GensparkWorker:
    """A worker slot for a single Genspark account. Multiple workers per
    account allow parallel generation (respecting session rate limits).
    """

    def __init__(self, slot_id: str, account_email: str, bridge: GensparkBridge, log_fn):
        self.slot_id = slot_id
        self.account_email = account_email
        self._bridge = bridge
        self._log = log_fn
        self.is_busy = False


class GensparkModeManager:
    """
    Genspark automation mode — uses the Chrome Extension + a dedicated local
    bridge (GensparkBridge) to drive genspark.ai directly.

    Mirrors ExtensionModeManager's shape but is fully independent.
    """

    def __init__(self, queue_manager):
        self.qm = queue_manager
        self._log = lambda msg: queue_manager.signals.log_msg.emit(msg)
        self._bridge = GensparkBridge(self._log)
        self._workers: Dict[str, List[GensparkWorker]] = {}  # email -> [workers]
        self._active_tasks: List[asyncio.Task] = []
        # Settings snapshot loaded at run() start — applies globally to every
        # job dispatched in this Genspark session.
        self._default_model: str = "nano-banana-2"
        self._default_image_size: str = "auto"
        self._auto_prompt: bool = False

    # ═══════════════════════════════════════════════════════════════
    # Main loop
    # ═══════════════════════════════════════════════════════════════

    async def run(self) -> None:
        self._log("[GensparkMode] Starting Genspark automation mode...")
        await self._bridge.start()

        try:
            # Model + quality are per-job now (encoded into the job's model
            # field as "Name:size" by the Image Generation tab). The legacy
            # global "genspark_model" / "genspark_image_size" settings are no
            # longer written by the UI — reading them here would surface a
            # stale value from a previous app version, which confused users
            # whose DB still had "nano-banana-pro" left over.
            self._default_model = "nano-banana-2"
            self._default_image_size = "auto"
            self._log(
                f"[GensparkMode] Fallback defaults → model={self._default_model}, "
                f"image_size={self._default_image_size} (each job carries its own "
                "Model + Quality from the Image Generation tab)"
            )

            # Honor the UI "Parallel" dropdown (Image Generation tab) — same
            # value every other generation mode uses. Clamped to 1..40 to
            # match the dropdown's range. Note: Plus-plan users above ~5–10
            # parallel may hit the 5-hour session rate limit; the warning
            # below makes that explicit so it isn't surprising.
            slots_per_account = max(1, min(40, int(self.qm.account_parallel_slots or 5)))
            if slots_per_account > 10:
                self._log(
                    f"[GensparkMode] ⚠ Parallel={slots_per_account}/account is high — "
                    "Plus plan may hit the 5-hour session rate limit. Pro plan handles it better."
                )

            # Auto Prompt toggle from Image Generation tab. When OFF, raw
            # prompt goes straight to image generation — bypasses Genspark's
            # LLM agent (ask_proxy), which avoids both the 429 rate limit
            # and the SSE-parsing bug that the agent path triggers.
            self._auto_prompt = get_bool_setting("genspark_auto_prompt", False)
            self._log(
                f"[GensparkMode] Auto Prompt: {'ON (LLM agent rewrites)' if self._auto_prompt else 'OFF (raw prompt → image gen)'}"
            )

            # Dispatch stagger. With Auto Prompt OFF the request goes
            # straight to the image endpoint (much higher rate limit), so
            # a small 1.5–2.5s gap is plenty. With Auto Prompt ON the LLM
            # ask_proxy endpoint caps at ~0.4 req/s, so we space further.
            if self._auto_prompt:
                self._stagger_min = 5.0
                self._stagger_max = 7.0
            else:
                self._stagger_min = 1.5
                self._stagger_max = 2.5
            self._log(
                f"[GensparkMode] Dispatch stagger: "
                f"{self._stagger_min:.1f}s–{self._stagger_max:.1f}s"
            )

            self._log(
                "[GensparkMode] Waiting for Chrome Extension to connect...\n"
                "  Make sure Chrome is open with G-Labs Helper extension\n"
                "  and genspark.ai is logged in (Plus or Pro plan recommended)."
            )

            # Wait for first account detection (up to 60s)
            wait_start = time.time()
            while not self._bridge.is_extension_connected:
                if self.qm.stop_requested or self.qm.force_stop_requested:
                    return
                if time.time() - wait_start > 60:
                    self._log("[GensparkMode] Extension did not connect. Aborting.")
                    return
                await asyncio.sleep(1)

            # Give extension 4s to report accounts
            await asyncio.sleep(4)
            connected = self._bridge.get_connected_accounts()
            prev_count = len(connected)
            stable_rounds = 0
            for _ in range(20):
                if self.qm.stop_requested or self.qm.force_stop_requested:
                    return
                await asyncio.sleep(1)
                connected = self._bridge.get_connected_accounts()
                if len(connected) == prev_count and prev_count > 0:
                    stable_rounds += 1
                    if stable_rounds >= 4:
                        break
                else:
                    stable_rounds = 0
                    prev_count = len(connected)

            if not connected:
                self._log(
                    "[GensparkMode] No Genspark accounts detected.\n"
                    "  Open https://www.genspark.ai/ai_image and log in."
                )
                wait_start = time.time()
                while not connected:
                    if self.qm.stop_requested or self.qm.force_stop_requested:
                        return
                    if time.time() - wait_start > 60:
                        self._log("[GensparkMode] No accounts found. Aborting.")
                        return
                    await asyncio.sleep(3)
                    connected = self._bridge.get_connected_accounts()

            self._log(
                f"[GensparkMode] Found {len(connected)} account(s): "
                + ", ".join(connected.keys())
            )

            # Spin up workers
            for email, info in connected.items():
                account_name = email or info.get("display_name", "unknown")
                workers = []
                for idx in range(1, slots_per_account + 1):
                    slot_id = f"{account_name}#gs{idx}"
                    workers.append(GensparkWorker(slot_id, account_name, self._bridge, self._log))
                self._workers[account_name] = workers
                plan = info.get("plan_type", "free")
                self._log(
                    f"[GensparkMode] {account_name} [{plan}]: {len(workers)} worker(s) ready."
                )

            total_workers = sum(len(w) for w in self._workers.values())
            if total_workers == 0:
                self._log("[GensparkMode] No workers started.")
                return

            self._log(f"[GensparkMode] Total: {total_workers} worker(s) across "
                     f"{len(self._workers)} account(s).")

            # Snapshot of queue at start — helps user debug "nothing happens"
            try:
                initial_jobs = get_all_jobs() or []
                by_status: Dict[str, int] = {}
                for j in initial_jobs:
                    s = j.get("status", "unknown")
                    by_status[s] = by_status.get(s, 0) + 1
                self._log(
                    f"[GensparkMode] Queue snapshot: "
                    + ", ".join(f"{k}={v}" for k, v in by_status.items())
                    if by_status else "[GensparkMode] Queue is empty."
                )
                if not by_status.get("pending", 0):
                    self._log(
                        "[GensparkMode] No pending jobs. "
                        "If you re-ran after a failure, the old job is marked "
                        "'failed' — add a fresh prompt in the Prompts box and "
                        "click 'Add to Queue'."
                    )
            except Exception:
                pass

            # Idle heartbeat so logs don't go silent while we wait for jobs
            last_heartbeat = 0.0
            heartbeat_interval = 20.0  # seconds

            # Main dispatch loop
            while self.qm.is_running:
                if self.qm.stop_requested or self.qm.force_stop_requested:
                    break
                if self.qm.pause_requested:
                    await asyncio.sleep(1)
                    continue

                # Dynamic account discovery — add new accounts that connected
                # after initial setup
                current_accounts = self._bridge.get_connected_accounts()
                for email, info in current_accounts.items():
                    if email and email not in self._workers:
                        workers = []
                        for idx in range(1, slots_per_account + 1):
                            slot_id = f"{email}#gs{idx}"
                            workers.append(
                                GensparkWorker(slot_id, email, self._bridge, self._log)
                            )
                        self._workers[email] = workers
                        self._log(
                            f"[GensparkMode] New account: {email} — "
                            f"{len(workers)} worker(s) added."
                        )

                # Prune finished tasks
                self._active_tasks = [t for t in self._active_tasks if not t.done()]

                jobs = get_all_jobs()
                pending = [j for j in jobs if j["status"] == "pending"]

                # Periodic idle heartbeat so the user sees the app is alive
                now_ts = time.time()
                if now_ts - last_heartbeat > heartbeat_interval:
                    running_count = sum(1 for j in jobs if j["status"] == "running")
                    failed_count = sum(1 for j in jobs if j["status"] == "failed")
                    done_count = sum(1 for j in jobs if j["status"] == "completed")
                    self._log(
                        f"[GensparkMode] ⏱ waiting — "
                        f"pending={len(pending)}, running={running_count}, "
                        f"done={done_count}, failed={failed_count}, "
                        f"active_tasks={len(self._active_tasks)}"
                    )
                    last_heartbeat = now_ts

                if not pending:
                    if not self._active_tasks:
                        still_active = any(
                            j["status"] in ("pending", "running") for j in get_all_jobs()
                        )
                        if not still_active:
                            self._log(
                                "[GensparkMode] All jobs completed (or failed). "
                                "Stopping Genspark mode. Add fresh prompts and "
                                "click Start Automation to run more."
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
                        # No free workers — log once per tick so user knows why
                        self._log(
                            f"[GensparkMode] All {sum(len(w) for w in self._workers.values())} "
                            f"worker(s) busy, {len(pending)} job(s) waiting"
                        )
                        break

                    job_id = job["id"]
                    prompt_preview = str(job.get("prompt") or "")[:60]
                    self._log(
                        f"[GensparkMode] Dispatching job {job_id[:6]}... "
                        f"to {worker.slot_id} | prompt: \"{prompt_preview}\""
                    )
                    update_job_status(job_id, "running", account=worker.account_email)
                    self.qm.signals.job_updated.emit(
                        job_id, "running", worker.account_email, ""
                    )

                    task = asyncio.create_task(
                        self._run_job(worker, job), name=worker.slot_id,
                    )
                    self._active_tasks.append(task)
                    busy_slots.add(worker.slot_id)
                    dispatched += 1

                    stagger = random.uniform(self._stagger_min, self._stagger_max)
                    if stagger > 0:
                        await asyncio.sleep(stagger)

                if dispatched == 0:
                    await asyncio.sleep(self.qm.scheduler_poll_seconds)

            # Drain remaining tasks
            if self._active_tasks:
                if self.qm.stop_requested or self.qm.force_stop_requested:
                    self._log(
                        f"[GensparkMode] Cancelling {len(self._active_tasks)} task(s)..."
                    )
                    for t in self._active_tasks:
                        if not t.done():
                            t.cancel()
                    try:
                        await asyncio.wait_for(
                            asyncio.gather(*self._active_tasks, return_exceptions=True),
                            timeout=5.0,
                        )
                    except asyncio.TimeoutError:
                        self._log("[GensparkMode] Some tasks didn't cancel in 5s.")
                else:
                    self._log(
                        f"[GensparkMode] Waiting for {len(self._active_tasks)} task(s)..."
                    )
                    await asyncio.gather(*self._active_tasks, return_exceptions=True)

        finally:
            await self._bridge.stop()
            self._workers.clear()
            self._log("[GensparkMode] Genspark mode stopped.")

    # ═══════════════════════════════════════════════════════════════
    # Job execution
    # ═══════════════════════════════════════════════════════════════

    def _get_available_worker(self, busy_slots: set) -> Optional[GensparkWorker]:
        """Find a free worker, round-robin across accounts."""
        # Preserve insertion order but rotate starting account for fairness
        account_list = list(self._workers.keys())
        if not account_list:
            return None
        # Simple rotation
        for email, workers in self._workers.items():
            if self.qm.account_disabled.get(email):
                continue
            for w in workers:
                if w.slot_id not in busy_slots and not w.is_busy:
                    return w
        return None

    async def _run_job(self, worker: GensparkWorker, job: dict) -> None:
        """Execute a single image-generation job."""
        job_id = job["id"]
        prompt = str(job.get("prompt") or "").strip()
        if not prompt:
            update_job_status(job_id, "failed", account=worker.account_email,
                              error="empty_prompt")
            self.qm.signals.job_updated.emit(job_id, "failed", worker.account_email,
                                            "empty_prompt")
            return

        # Per-job model + quality parsing. The UI encodes quality into the
        # model field as "Model Name:<size>" when the user picks a non-auto
        # quality on the Image Generation tab. Flow's model resolver ignores
        # the suffix (it matches on the prefix), so this is safe for both
        # modes.
        raw_model_str = str(job.get("model") or "").strip()
        size_from_job = ""
        if ":" in raw_model_str:
            base, _, size_hint = raw_model_str.rpartition(":")
            candidate = size_hint.strip().lower()
            if candidate in {"auto", "0.5k", "1k", "2k", "4k"}:
                size_from_job = candidate
                raw_model_str = base.strip()

        # Resolve model — _resolve_model handles "Nano Banana 2",
        # "nano-banana-pro" etc. and falls back to the default when empty.
        low = raw_model_str.lower()
        if low in {"nano-banana-2", "nano-banana-pro"}:
            model = low
        elif "nano" in low:
            model = _resolve_model(raw_model_str)
        else:
            model = self._default_model

        ratio = _resolve_aspect_ratio(job.get("aspect_ratio") or "auto")
        image_size = size_from_job or self._default_image_size

        # Reference images — job stores them as either a list (modern path)
        # or a semicolon-separated string (legacy path, shared with Flow's
        # bulk-add code). Accept both. Files that no longer exist on disk
        # are filtered out silently.
        raw_refs = job.get("ref_paths")
        ref_paths_list: List[str] = []
        if isinstance(raw_refs, (list, tuple)):
            ref_paths_list = [str(p).strip() for p in raw_refs if str(p or "").strip()]
        elif isinstance(raw_refs, str) and raw_refs.strip():
            ref_paths_list = [p.strip() for p in raw_refs.split(";") if p.strip()]
        # Fallback to single `ref_path` field if list is empty
        if not ref_paths_list:
            single_ref = str(job.get("ref_path") or "").strip()
            if single_ref:
                ref_paths_list = [single_ref]
        # Existence filter — broken paths silently dropped
        ref_paths_list = [p for p in ref_paths_list if os.path.exists(p)]

        # When refs are attached, Pro+auto can produce 4k images which
        # combined with the inline base64 of the ref easily exceeds
        # Genspark's request body cap. The HAR captures showed Pro requests
        # with refs running at image_size="1k". Auto-downgrade to keep the
        # request small. User can still override by picking 2k/4k explicitly.
        if ref_paths_list and model == "nano-banana-pro" and image_size == "auto":
            image_size = "1k"
            self._log(
                f"[GensparkMode] Job {job_id[:6]}… auto-downgrading Pro size "
                f"to 1k because reference images are attached"
            )

        self._log(
            f"[GensparkMode] Job {job_id[:6]}… settings: "
            f"model={model}, ratio={ratio}, size={image_size}, "
            f"refs={len(ref_paths_list)}"
        )

        max_retries = int(self.qm.max_auto_retries_per_job or 2)
        last_error = ""
        worker.is_busy = True
        try:
            for attempt in range(max_retries + 1):
                if self.qm.stop_requested or self.qm.force_stop_requested:
                    update_job_status(job_id, "pending", account="")
                    self.qm.signals.job_updated.emit(job_id, "pending", "", "")
                    return
                try:
                    result = await self._bridge.generate_image(
                        account=worker.account_email,
                        prompt=prompt,
                        model=model,
                        aspect_ratio=ratio,
                        image_size=image_size,
                        auto_prompt=self._auto_prompt,
                        ref_paths=ref_paths_list,
                    )
                except Exception as e:
                    last_error = f"bridge_error: {e}"
                    self._log(f"[{worker.slot_id}] Bridge exception: {last_error}")
                    if attempt < max_retries:
                        await asyncio.sleep(10)
                        continue
                    break

                if result.get("error"):
                    last_error = result["error"]
                    self._log(
                        f"[{worker.slot_id}] Attempt {attempt + 1}/{max_retries + 1} "
                        f"failed: {last_error[:200]}"
                    )
                    # Common recoverable errors
                    err_lower = last_error.lower()
                    if "recaptcha" in err_lower or "captcha" in err_lower:
                        # Same as Flow: reload tab, retry
                        await asyncio.sleep(5)
                        continue
                    if "429" in err_lower or "rate" in err_lower:
                        self._log(
                            f"[{worker.slot_id}] 429 — backing off 60s (session limit)"
                        )
                        await asyncio.sleep(60)
                        continue
                    if "timeout" in err_lower:
                        await asyncio.sleep(5)
                        continue
                    # Unknown error — break after a couple of tries
                    if attempt < max_retries:
                        await asyncio.sleep(10)
                        continue
                    break

                # Success path — save the image to output dir.
                # Use queue_no / output_index for filename so it matches Flow
                # ("1.jpg", "2.jpg", ...) and preserves numbering across retries.
                queue_no = job.get("output_index") or job.get("queue_no")
                out_path = await self._save_image(
                    result, job_id, worker.account_email, queue_no=queue_no,
                    slot_id=worker.slot_id,
                )
                if out_path:
                    update_job_status(job_id, "completed",
                                      account=worker.account_email)
                    self.qm.signals.job_updated.emit(
                        job_id, "completed", worker.account_email, ""
                    )
                    self._log(f"[{worker.slot_id}] Job {job_id[:6]}... completed! "
                             f"({out_path})")
                    return
                else:
                    last_error = "save_failed"

            # Out of retries
            update_job_status(job_id, "failed", account=worker.account_email,
                              error=last_error or "unknown")
            self.qm.signals.job_updated.emit(
                job_id, "failed", worker.account_email, last_error or "unknown"
            )
            self._log(
                f"[{worker.slot_id}] Job {job_id[:6]}... FAILED: "
                f"{(last_error or 'unknown')[:200]}"
            )
        finally:
            worker.is_busy = False

    async def _save_image(
        self,
        result: Dict[str, Any],
        job_id: str,
        account: str,
        queue_no=None,
        slot_id: str = "gs",
    ) -> Optional[str]:
        """Save the returned image bytes to the output dir.

        Naming convention mirrors Flow / extension_mode._save_media:
          - If queue_no / output_index is set → filename = "{queue_no}.jpg"
            (e.g. "1.jpg", "2.jpg") — survives retries because output_index
            stays the same on re-dispatch.
          - Fallback → "{safe_job}_{ts}_{nonce}_generation.jpg"
        """
        try:
            out_dir = get_output_directory() or "outputs"
            os.makedirs(out_dir, exist_ok=True)

            # Prefer base64 payload (already downloaded by extension)
            b64 = result.get("image_bytes_b64") or ""
            if not b64:
                url = result.get("image_url", "")
                if not url:
                    return None
                # Extension didn't send bytes — save URL pointer so the user
                # can still recover the file.
                self._log(
                    f"[{slot_id}] No bytes returned, only URL: {url[:100]}"
                )
                data = (url + "\n").encode("utf-8")
                ext = ".url.txt"
            else:
                data = base64.b64decode(b64)
                # Sniff magic bytes — Genspark usually serves JPEG, sometimes
                # PNG. Fall back to .jpg if ambiguous.
                if data[:3] == b"\xff\xd8\xff":
                    ext = ".jpg"
                elif data[:8] == b"\x89PNG\r\n\x1a\n":
                    ext = ".png"
                elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
                    ext = ".webp"
                else:
                    ext = ".jpg"

            # Normalise queue_no → positive integer (same rule as Flow)
            normalized_qno = None
            try:
                val = int(queue_no) if queue_no is not None else None
                if val is not None and val > 0:
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

            output_path = os.path.join(out_dir, filename)

            # If a file with the numbered name already exists (manual retry
            # on a previously-saved job), overwrite — the new attempt is
            # the canonical version now.
            mode = "wb" if ext != ".url.txt" else "w"
            if mode == "wb":
                with open(output_path, "wb") as f:
                    f.write(data)
            else:
                with open(output_path, "w", encoding="utf-8") as f:
                    f.write(data.decode("utf-8"))

            try:
                from src.db.db_manager import update_job_runtime_state
                update_job_runtime_state(job_id, output_path=output_path)
            except Exception:
                pass

            self._log(
                f"[{slot_id}] Saved: {filename} ({len(data)} bytes)"
            )
            return output_path
        except Exception as e:
            self._log(f"[{slot_id}] Save failed: {e}")
            return None
