import asyncio
import json
import os
import platform
import random
import re
import shutil
import subprocess
import time
import uuid
from playwright.async_api import async_playwright
from PySide6.QtCore import QObject, Signal, QThread

from src.core.app_paths import get_session_clones_dir, get_sessions_dir
from src.db.db_manager import (
    get_bool_setting,
    get_accounts,
    get_all_jobs,
    get_connection,
    get_float_setting,
    get_int_setting,
    get_setting,
    reset_running_jobs_to_pending,
    update_job_status,
)
from src.core.bot_engine import GoogleLabsBot
from src.core.cookie_warmup import light_cookie_warmup
from src.core.process_tracker import process_tracker, cleanup_session_locks


class QueueSignals(QObject):
    log_msg = Signal(str)
    job_updated = Signal(str, str, str, str)  # job_id, status, account, error_msg
    account_runtime = Signal(str, str, float, int, int, str)  # account, status, cooldown_until_ts, active_slots, total_slots, detail
    account_auth_status = Signal(str, str, str)  # account_name, status ("logged_in"/"expired"), message
    show_warning = Signal(str)
    warmup_progress = Signal(str, int, str)
    warmup_complete = Signal(str, bool, str)


class RecaptchaHealthMonitor:
    def __init__(self, threshold=3, window=10):
        self.threshold = max(0, int(threshold or 0))
        self.window = max(1, int(window or 1))
        self._history = {}

    def record(self, account, success):
        account_key = str(account or "").strip()
        if not account_key:
            return
        history = self._history.setdefault(account_key, [])
        history.append(bool(success))
        self._history[account_key] = history[-self.window :]

    def needs_restart(self, account):
        if self.threshold <= 0:
            return False
        history = self._history.get(str(account or "").strip(), [])
        if len(history) < self.threshold:
            return False
        return self.failure_count(account) >= self.threshold

    def failure_count(self, account):
        history = self._history.get(str(account or "").strip(), [])
        return sum(1 for entry in history if not entry)

    def sample_size(self, account):
        return len(self._history.get(str(account or "").strip(), []))

    def reset(self, account):
        self._history[str(account or "").strip()] = []


class AsyncQueueManager(QThread):
    def __init__(self):
        super().__init__()
        self.signals = QueueSignals()
        self.is_running = False
        self.stop_requested = False
        self.pause_requested = False
        self.force_stop_requested = False
        self.loop = None
        self.active_tasks = []

        # Mini-orchestrator controls (Ultimate-style fundamentals).
        self.account_parallel_slots = max(1, min(40, get_int_setting("slots_per_account", 5)))
        self.enable_profile_clones = get_bool_setting("enable_profile_clones", True)
        mode_raw = str(get_setting("browser_mode", "cloakbrowser") or "cloakbrowser").strip().lower()
        if mode_raw == "playwright":
            mode_raw = "visible"
        if mode_raw not in {"headless", "visible", "real_chrome", "cloakbrowser"}:
            mode_raw = "headless"
        self.browser_mode = mode_raw
        self.browser_headless = self.browser_mode == "headless"
        self.random_fingerprint_per_session = get_bool_setting("random_fingerprint_per_session", False)
        chrome_display_raw = str(get_setting("chrome_display", "headless") or "headless").strip().lower()
        if chrome_display_raw not in {"visible", "headless"}:
            chrome_display_raw = "headless"
        self.chrome_display = chrome_display_raw
        cloak_display_raw = str(get_setting("cloak_display", "headless") or "headless").strip().lower()
        if cloak_display_raw not in {"visible", "headless", "stealth_visible"}:
            cloak_display_raw = "headless"
        self.cloak_display = cloak_display_raw
        self.light_warmup_enabled = get_bool_setting("light_warmup", True)
        gen_mode_raw = str(get_setting("generation_mode", "browser_per_slot") or "browser_per_slot").strip().lower()
        if gen_mode_raw not in {
            "browser_per_slot",
            "cdp_shared",
            "http_shared",
            "chrome_extension",
            "chrome_extension_genspark",
            "chrome_extension_grok",
            "chrome_extension_dola",
        }:
            gen_mode_raw = "browser_per_slot"
        self.generation_mode = gen_mode_raw
        self.scheduler_poll_seconds = 2
        self.inter_job_cooldown_seconds = 1.5
        self.max_consecutive_slot_failures = 2
        self.slot_cooldown_seconds = 45
        retry_count_raw = get_setting("max_retries", get_setting("max_auto_retries_per_job", 3))
        retry_base_delay_raw = get_setting(
            "retry_base_delay_seconds",
            get_setting("auto_retry_base_delay_seconds", 10),
        )
        try:
            retry_count_value = int(retry_count_raw)
        except (TypeError, ValueError):
            retry_count_value = 2
        try:
            retry_base_delay_value = int(retry_base_delay_raw)
        except (TypeError, ValueError):
            retry_base_delay_value = 20

        self.max_auto_retries_per_job = max(0, min(5, retry_count_value))
        self.auto_retry_base_delay_seconds = max(5, min(300, retry_base_delay_value))
        self.same_account_stagger_seconds = max(
            0.0, min(60.0, get_float_setting("same_account_stagger_seconds", 3.0))
        )
        self.global_stagger_min_seconds = max(
            0.0, min(60.0, get_float_setting("global_stagger_min_seconds", 0.5))
        )
        self.global_stagger_max_seconds = max(
            self.global_stagger_min_seconds,
            min(120.0, get_float_setting("global_stagger_max_seconds", 1.5)),
        )
        self.max_no_account_attempts = max(
            3, min(120, get_int_setting("max_no_account_attempts", 20))
        )
        self.session_clone_root = str(get_session_clones_dir())
        self.last_account_dispatch_at = {}
        self.job_retry_counts = {}
        self.job_retry_after = {}
        self.worker_slots = []
        self.no_account_attempts = 0
        self.task_job_map = {}
        self.account_parallel_ramp_started_at = {}
        self.account_success_count = {}
        self.account_auto_refresh_pending = {}
        self.auto_refresh_after_jobs = max(
            0, min(10000, get_int_setting("auto_refresh_after_jobs", 150))
        )
        self.auto_restart_recap_fail_threshold = max(
            0, min(20, get_int_setting("auto_restart_recap_fail_threshold", 3))
        )
        self.auto_restart_recap_fail_window = max(
            5, min(50, get_int_setting("auto_restart_recap_fail_window", 10))
        )
        self.auto_restart_recap_cooldown_seconds = max(
            10, min(120, get_int_setting("auto_restart_recap_cooldown_seconds", 30))
        )
        self.recaptcha_monitor = RecaptchaHealthMonitor(
            threshold=self.auto_restart_recap_fail_threshold,
            window=self.auto_restart_recap_fail_window,
        )
        self.account_restart_pending = {}
        self.account_restart_running = set()
        self.account_restart_count = {}
        self.account_disabled = {}
        self.account_hold_until = {}   # account_name -> timestamp when hold expires
        self.account_hold_reason = {}  # account_name -> reason string
        self.account_hold_count = {}   # account_name -> how many times held (for escalation)
        self._account_hold_lock = None  # Initialized in process_queue (needs event loop)

        # Smart slot throttle for 429 — reduce slots instead of hard-blocking account
        self.account_throttle_max_slots = {}   # account_name -> max allowed concurrent slots
        self.account_throttle_until = {}       # account_name -> timestamp when throttle expires
        self.account_throttle_successes = {}   # account_name -> consecutive successes since throttle (for ramp-up)
        self.account_warmup_ready = {}
        self.account_warmup_tasks = {}

        # Per-slot cooldown for repeated API reCAPTCHA blocks.
        self.recaptcha_account_cooldown_seconds = max(
            5, min(600, get_int_setting("recaptcha_account_cooldown_seconds", 15))
        )
        self.account_recaptcha_until = {}
        self.account_recaptcha_streak = {}
        self.account_recap_cooldown_announced = {}

        # Hard 429 pause — when labs.google returns
        # PUBLIC_ERROR_UNUSUAL_ACTIVITY_TOO_MUCH_TRAFFIC, the account is in
        # Google's penalty box. Existing soft throttle (just lowering slot
        # count) lets the surviving slot keep hammering the API in a tight
        # loop, which both wastes the user's session and amplifies Google's
        # block. This pause is hard: dispatch loop refuses to send any work
        # to a paused account until the timestamp expires.
        # Streak tracks consecutive 429s — used for exponential backoff and
        # eventually permanent hold. Per-job attempt cap prevents one
        # losing prompt from re-queueing forever.
        self.account_429_pause_until = {}     # email -> epoch seconds
        self.account_429_streak = {}          # email -> consecutive 429 count
        self.job_429_attempts = {}            # job_id -> int
        self.queue_summary_emitted = False
        self.queue_had_jobs = False

        # Keep sessions warm; each job gets a fresh tab via engine.open_fresh_tab().
        self.reinitialize_each_job = False

    def _allocate_debug_port(self, slot_index_seed):
        return 9222 + max(0, int(slot_index_seed or 0))

    def _emit_account_runtime_snapshot(self, worker_slots, accounts=None):
        now = time.time()
        if accounts is None:
            accounts = []
        account_names = {str(a.get("name") or "") for a in accounts}
        account_names.update({slot["account_name"] for slot in worker_slots})

        for account_name in sorted(name for name in account_names if name):
            slots = [slot for slot in worker_slots if slot["account_name"] == account_name]
            total_slots = len(slots) if slots else 1
            active_slots = sum(1 for slot in slots if slot.get("is_busy"))
            hold_until_candidates = [self.account_recaptcha_until.get(account_name, 0.0)]
            hold_until_candidates.extend(slot.get("disabled_until", 0.0) for slot in slots)
            # 429 hard-pause cooldown — surface it on the account card so
            # the user sees a live countdown when an account is in
            # Google's penalty box, not silent black-holed work.
            pause_429_until = self.account_429_pause_until.get(account_name, 0.0)
            if pause_429_until > now:
                hold_until_candidates.append(pause_429_until)
            cooldown_until = max(hold_until_candidates) if hold_until_candidates else 0.0
            on_recap_cooldown = self.account_recaptcha_until.get(account_name, 0.0) > now
            on_slot_cooldown = any(slot.get("disabled_until", 0.0) > now for slot in slots)
            on_429_pause = pause_429_until > now

            status = "idle"
            detail = "Idle"
            if self.account_disabled.get(account_name):
                status = "cooldown"
                detail = "Session burned - manual reset required"
            elif active_slots > 0:
                status = "running"
                detail = f"Running {active_slots} job(s)"
                if any(slot.get("pending_browser_restart") or slot.get("is_restarting") for slot in slots):
                    detail = f"Running {active_slots} job(s) + restarting failing slot"
            elif on_429_pause:
                # Highest-priority idle reason — show the 429 strike +
                # remaining minutes so the user understands why this
                # account isn't picking up work.
                status = "cooldown"
                streak = int(self.account_429_streak.get(account_name, 0))
                mins_left = max(1, int((pause_429_until - now) / 60))
                detail = f"⏸ 429 paused (strike {streak}) — {mins_left} min left"
            elif self.account_restart_pending.get(account_name) or account_name in self.account_restart_running:
                status = "cooldown"
                detail = "Restarting failing slot"
            elif not self.account_warmup_ready.get(account_name, True):
                status = "ready"
                detail = "Cookie warm-up in progress"
            elif on_recap_cooldown:
                status = "cooldown"
                streak = int(self.account_recaptcha_streak.get(account_name, 0))
                detail = f"reCAPTCHA cooldown (streak {streak})"
            elif on_slot_cooldown:
                status = "slot_cooldown"
                detail = "Slot recovery cooldown"
            elif self.is_running:
                status = "ready"
                detail = "Ready"

            if cooldown_until <= now:
                cooldown_until = 0.0

            self.signals.account_runtime.emit(
                account_name,
                status,
                float(cooldown_until),
                int(active_slots),
                int(total_slots),
                detail,
            )

    def run(self):
        self.is_running = True
        self.stop_requested = False
        self.pause_requested = False
        self.force_stop_requested = False
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self.process_queue())
        except Exception as e:
            self.signals.log_msg.emit(f"[SYSTEM] Queue Manager Error: {str(e)}")
        finally:
            self.is_running = False
            self.stop_requested = False
            self.pause_requested = False
            if self.loop and not self.loop.is_closed():
                self.loop.close()

    def stop(self):
        self.stop_requested = True
        self.pause_requested = False
        self.is_running = False
        self.signals.log_msg.emit("[SYSTEM] Stop requested — resetting jobs immediately...")

        # ─── INSTANT reset: move all running jobs back to pending RIGHT NOW ───
        # Don't wait for async task cancellation — update DB and UI immediately
        try:
            recovered = reset_running_jobs_to_pending()
            if recovered:
                self.signals.log_msg.emit(
                    f"[SYSTEM] ✓ Instantly reset {recovered} running job(s) back to pending."
                )
                self.signals.job_updated.emit("", "pending", "", "")  # trigger UI refresh
        except Exception as e:
            self.signals.log_msg.emit(f"[SYSTEM] Reset warning: {e}")

        # Then cancel async tasks in background (cleanup)
        if self.loop and not self.loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(self._cancel_active_tasks_now(), self.loop)
            except Exception as exc:
                self.signals.log_msg.emit(f"[SYSTEM] Stop scheduling warning: {exc}")

    def force_stop(self):
        self.stop_requested = True
        self.pause_requested = False
        self.force_stop_requested = True
        self.is_running = False
        self.signals.log_msg.emit("[SYSTEM] FORCE STOP — resetting jobs immediately...")

        # ─── INSTANT reset: move all running jobs back to pending RIGHT NOW ───
        try:
            recovered = reset_running_jobs_to_pending()
            if recovered:
                self.signals.log_msg.emit(
                    f"[SYSTEM] ✓ Instantly reset {recovered} running job(s) back to pending."
                )
                self.signals.job_updated.emit("", "pending", "", "")  # trigger UI refresh
        except Exception as e:
            self.signals.log_msg.emit(f"[SYSTEM] Reset warning: {e}")

        if self.loop and not self.loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(self._cancel_active_tasks_now(), self.loop)
            except Exception as exc:
                self.signals.log_msg.emit(f"[SYSTEM] Force stop scheduling warning: {exc}")

    def pause_dispatch(self):
        if not self.is_running or self.stop_requested:
            return
        self.pause_requested = True

    def resume_dispatch(self):
        if self.stop_requested:
            return
        self.pause_requested = False

    async def _cancel_active_tasks_now(self):
        snapshot = list(self.active_tasks)
        for task in snapshot:
            if not task.done():
                task.cancel()
        # Give tasks max 3s to respond to cancellation (was unlimited before)
        if snapshot:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*snapshot, return_exceptions=True),
                    timeout=3.0,
                )
            except asyncio.TimeoutError:
                self.signals.log_msg.emit("[SYSTEM] Some tasks didn't cancel in 3s — force-continuing cleanup.")

        for slot in (self.worker_slots or []):
            slot["is_busy"] = False
            if slot.get("is_initialized"):
                try:
                    await asyncio.wait_for(slot["engine"].cleanup(), timeout=3)
                except Exception:
                    pass
                finally:
                    slot["is_initialized"] = False
        await self._cleanup_all_browsers()

    async def _cleanup_all_browsers(self):
        # Use shorter timeouts when stop was requested (instant stop)
        cleanup_timeout = 2 if (self.stop_requested or self.force_stop_requested) else 8

        for slot in list(self.worker_slots or []):
            try:
                engine = slot.get("engine")
                if engine is not None:
                    await asyncio.wait_for(engine.cleanup(), timeout=cleanup_timeout)
            except Exception:
                # Bug #9: Force close context if cleanup timed out
                try:
                    engine = slot.get("engine")
                    if engine and getattr(engine, "context", None):
                        await engine.context.close()
                        engine.context = None
                except Exception:
                    pass
            finally:
                slot["is_initialized"] = False
                slot["is_busy"] = False

        # Shorter sleeps on stop — just enough for process kill
        sleep_time = 0.5 if (self.stop_requested or self.force_stop_requested) else 2
        await asyncio.sleep(sleep_time)
        killed = process_tracker.kill_all()
        self._cleanup_lock_files()
        await asyncio.sleep(0.3)
        # Verify processes actually died (Bug #8 fix)
        still_alive = process_tracker.kill_all()
        if still_alive:
            await asyncio.sleep(0.5)
            process_tracker.kill_all()  # Third attempt
        if killed or still_alive:
            self.signals.log_msg.emit(
                f"[SYSTEM] Force-killed {killed + still_alive} browser process(es)."
            )
        self.signals.log_msg.emit("[SYSTEM] All browser processes cleaned up.")

    def _cleanup_lock_files(self):
        for base_dir in (get_sessions_dir(), get_session_clones_dir()):
            try:
                if not base_dir.exists():
                    continue
            except Exception:
                continue

            for pattern in ("**/SingletonLock", "**/SingletonCookie", "**/SingletonSocket", "**/lockfile"):
                try:
                    lock_paths = list(base_dir.glob(pattern))
                except Exception:
                    continue

                for lock_path in lock_paths:
                    try:
                        if lock_path.is_file():
                            lock_path.unlink()
                    except Exception:
                        pass

    async def process_queue(self):
        if self._account_hold_lock is None:
            self._account_hold_lock = asyncio.Lock()

        # HTTP Shared mode — 1 browser per account (captcha only), N fetch workers
        if self.generation_mode == "http_shared":
            self.signals.log_msg.emit(
                "[SYSTEM] Generation mode: HTTP Shared "
                "(1 browser per account for reCAPTCHA, N parallel fetch workers)"
            )
            try:
                from src.core.http_mode import HttpModeManager
                manager = HttpModeManager(self)
                await manager.run()
            except ImportError:
                self.signals.log_msg.emit("[ERROR] http_mode.py not found. Falling back to browser mode.")
            except Exception as e:
                self.signals.log_msg.emit(f"[ERROR] HTTP Shared failed: {str(e)[:100]}. Falling back to browser mode.")
            else:
                return
            self.signals.log_msg.emit("[SYSTEM] Falling back to browser-per-slot mode.")

        # Chrome Extension mode — real Chrome + extension + direct API calls
        elif self.generation_mode == "chrome_extension":
            self.signals.log_msg.emit(
                "[SYSTEM] Generation mode: Chrome Extension "
                "(Real Chrome + Extension, direct API calls, zero CDP)"
            )
            try:
                from src.core.extension_mode import ExtensionModeManager
                manager = ExtensionModeManager(self)
                await manager.run()
            except ImportError:
                self.signals.log_msg.emit("[ERROR] extension_mode.py not found. Falling back to browser mode.")
            except Exception as e:
                self.signals.log_msg.emit(f"[ERROR] Chrome Extension mode failed: {str(e)[:100]}. Falling back to browser mode.")
            else:
                return
            self.signals.log_msg.emit("[SYSTEM] Falling back to browser-per-slot mode.")

        # Chrome Extension mode — Genspark.ai (parallel to Flow, separate bridge)
        elif self.generation_mode == "chrome_extension_genspark":
            self.signals.log_msg.emit(
                "[SYSTEM] Generation mode: Chrome Extension — Genspark "
                "(Genspark.ai Nano Banana 2/Pro, unlimited on Plus/Pro plans)"
            )
            try:
                from src.core.genspark_mode import GensparkModeManager
                manager = GensparkModeManager(self)
                await manager.run()
            except ImportError:
                self.signals.log_msg.emit(
                    "[ERROR] genspark_mode.py not found. Falling back to browser mode."
                )
            except Exception as e:
                self.signals.log_msg.emit(
                    f"[ERROR] Genspark mode failed: {str(e)[:150]}. "
                    "Falling back to browser mode."
                )
            else:
                return
            self.signals.log_msg.emit("[SYSTEM] Falling back to browser-per-slot mode.")

        # Chrome Extension mode — Grok Imagine (video only, cookie auth, no reCAPTCHA)
        elif self.generation_mode == "chrome_extension_grok":
            self.signals.log_msg.emit(
                "[SYSTEM] Generation mode: Chrome Extension — Grok Imagine "
                "(grok.com video generation, SuperGrok/Premium+ accounts)"
            )
            try:
                from src.core.grok_mode import GrokModeManager
                manager = GrokModeManager(self)
                await manager.run()
            except ImportError:
                self.signals.log_msg.emit(
                    "[ERROR] grok_mode.py not found. Falling back to browser mode."
                )
            except Exception as e:
                self.signals.log_msg.emit(
                    f"[ERROR] Grok mode failed: {str(e)[:150]}. "
                    "Falling back to browser mode."
                )
            else:
                return
            self.signals.log_msg.emit("[SYSTEM] Falling back to browser-per-slot mode.")

        # Chrome Extension mode — dola.com (ByteDance Seedance video via real Chrome)
        elif self.generation_mode == "chrome_extension_dola":
            self.signals.log_msg.emit(
                "[SYSTEM] Generation mode: Chrome Extension — Dola "
                "(dola.com Seedance video, real logged-in Chrome, API + download)"
            )
            try:
                from src.core.dola_mode import DolaModeManager
                manager = DolaModeManager(self)
                await manager.run()
            except ImportError:
                self.signals.log_msg.emit(
                    "[ERROR] dola_mode.py not found. Falling back to browser mode."
                )
            except Exception as e:
                self.signals.log_msg.emit(
                    f"[ERROR] Dola mode failed: {str(e)[:150]}. Falling back to browser mode."
                )
            else:
                return
            self.signals.log_msg.emit("[SYSTEM] Falling back to browser-per-slot mode.")

        # CDP Shared mode — 1 process per account, N contexts
        elif self.generation_mode == "cdp_shared":
            self.signals.log_msg.emit("[SYSTEM] Generation mode: CDP Shared (1 browser per account, N contexts)")
            try:
                from src.core.cdp_shared_mode import CDPSharedManager
                manager = CDPSharedManager(self)
                await manager.run()
            except ImportError:
                self.signals.log_msg.emit("[ERROR] cdp_shared_mode.py not found. Falling back to browser mode.")
            except Exception as e:
                self.signals.log_msg.emit(f"[ERROR] CDP Shared failed: {str(e)[:100]}. Falling back to browser mode.")
            else:
                return
            self.signals.log_msg.emit("[SYSTEM] Falling back to browser-per-slot mode.")
        else:
            self.signals.log_msg.emit("[SYSTEM] Generation mode: Browser per slot (stable)")

        self.signals.log_msg.emit("[SYSTEM] Queue Manager started. Fetching accounts...")
        GoogleLabsBot.clear_reference_cache()
        self.queue_summary_emitted = False
        self.queue_had_jobs = False
        await self._cleanup_all_browsers()

        reset_count = reset_running_jobs_to_pending()
        if reset_count:
            self.signals.log_msg.emit(
                f"[SYSTEM] Recovered {reset_count} stale running job(s) back to pending."
            )

        all_accs = get_accounts()
        if not all_accs:
            self.signals.log_msg.emit("[SYSTEM] No Google accounts configured! Please add one in Account Manager.")
            return

        # Auto-clean profiles that have accumulated junk (prevents reCAPTCHA degradation)
        try:
            from src.core.profile_cleaner import clean_profile, clean_derived_profiles, needs_cleaning
            for acc in all_accs:
                sp = acc.get("session_path", "")
                if sp and needs_cleaning(sp):
                    self.signals.log_msg.emit(f"[SYSTEM] Cleaning profile for {acc.get('name', '?')}...")
                    clean_profile(sp, log_fn=lambda msg: self.signals.log_msg.emit(msg))
                # Clean _cloak, _multitab, _token_server, _shared_browser profiles
                if sp:
                    clean_derived_profiles(sp, log_fn=lambda msg: self.signals.log_msg.emit(msg))
        except Exception:
            pass

        self._initialize_account_warmup_state(all_accs)

        self.signals.log_msg.emit(
            f"[SYSTEM] Slot config: {self.account_parallel_slots} slot(s)/account, "
            f"profile cloning={'on' if self.enable_profile_clones else 'off'}."
        )
        self.signals.log_msg.emit(
            f"[SYSTEM] Browser mode: {self.browser_mode}."
        )
        if self.browser_mode == "real_chrome":
            self.signals.log_msg.emit(
                f"[SYSTEM] Real Chrome display: {self.chrome_display}."
            )
        elif self.browser_mode == "cloakbrowser":
            self.signals.log_msg.emit(
                f"[SYSTEM] CloakBrowser display: {self.cloak_display}."
            )
        self.signals.log_msg.emit(
            f"[SYSTEM] Random fingerprint per session: {'on' if self.random_fingerprint_per_session else 'off'}."
        )
        self.signals.log_msg.emit(
            f"[SYSTEM] Same-account dispatch stagger: {self.same_account_stagger_seconds}s."
        )
        self.signals.log_msg.emit(
            f"[SYSTEM] Global dispatch stagger: {self.global_stagger_min_seconds}s-{self.global_stagger_max_seconds}s."
        )
        self.signals.log_msg.emit(
            f"[SYSTEM] Auto-retry: {self.max_auto_retries_per_job} retries/job, "
            f"base delay {self.auto_retry_base_delay_seconds}s."
        )
        self.signals.log_msg.emit(
            f"[SYSTEM] ReCAPTCHA slot cooldown: {self.recaptcha_account_cooldown_seconds}s base."
        )
        self.signals.log_msg.emit(
            f"[SYSTEM] ReCAPTCHA auto-restart: threshold={self.auto_restart_recap_fail_threshold}, "
            f"window={self.auto_restart_recap_fail_window}, cooldown={self.auto_restart_recap_cooldown_seconds}s."
        )
        if self.account_parallel_slots > 1 and not self.enable_profile_clones:
            self.signals.log_msg.emit(
                "[SYSTEM] Extra slots require profile cloning. Only primary slot will be used."
            )

        async with async_playwright() as p:
            worker_slots = self._build_worker_slots(all_accs)
            self.worker_slots = worker_slots
            if not worker_slots:
                self.signals.log_msg.emit("[SYSTEM] No usable worker slots could be created.")
                self._emit_account_runtime_snapshot(worker_slots, all_accs)
                return
            self.signals.log_msg.emit(
                f"[SYSTEM] Standing by with {len(all_accs)} accounts, {len(worker_slots)} worker slot(s)."
            )
            self._emit_account_runtime_snapshot(worker_slots, all_accs)
            if self.light_warmup_enabled:
                warmup_task = asyncio.create_task(self._start_account_light_warmups(worker_slots, p))
                self.active_tasks.append(warmup_task)

            while self.is_running:
                try:
                    self._prune_finished_tasks()
                    self._maybe_emit_queue_summary()
                    self._emit_account_runtime_snapshot(worker_slots, all_accs)

                    if self.pause_requested:
                        jobs = get_all_jobs()
                        has_pending_or_running = any(
                            str(job.get("status") or "").lower() in ("pending", "running")
                            for job in jobs
                        )
                        if not has_pending_or_running and not self.active_tasks:
                            break
                        await asyncio.sleep(self.scheduler_poll_seconds)
                        continue

                    self._announce_recovered_slots(worker_slots)
                    self._check_account_holds()
                    pending_count, dispatched_count = await self._dispatch_pending_jobs(worker_slots, p)
                    if pending_count > 0 or dispatched_count > 0 or self.active_tasks:
                        self.queue_had_jobs = True
                        self.queue_summary_emitted = False
                    if pending_count > 0 and dispatched_count == 0:
                        self.no_account_attempts += 1
                        if self.no_account_attempts % 5 == 0:
                            self.signals.log_msg.emit(
                                f"[SYSTEM] Pending jobs waiting for available account/slot "
                                f"(attempt {self.no_account_attempts}/{self.max_no_account_attempts})."
                            )
                        if self.no_account_attempts >= self.max_no_account_attempts:
                            self.signals.log_msg.emit(
                                "[SYSTEM] No eligible account/slot after repeated attempts. Applying scheduler backoff 5s."
                            )
                            self.no_account_attempts = 0
                            await asyncio.sleep(5)
                            continue
                    else:
                        self.no_account_attempts = 0
                    await asyncio.sleep(self.scheduler_poll_seconds)
                except Exception as loop_error:
                    self.signals.log_msg.emit(f"[SYSTEM] Scheduler loop error: {loop_error}")
                    await asyncio.sleep(2)

            if self.active_tasks:
                if self.force_stop_requested or self.stop_requested:
                    for task in self.active_tasks:
                        if not task.done():
                            task.cancel()
                # Use timeout on gather so stop doesn't hang
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*self.active_tasks, return_exceptions=True),
                        timeout=5.0 if (self.stop_requested or self.force_stop_requested) else 60.0,
                    )
                except asyncio.TimeoutError:
                    self.signals.log_msg.emit("[SYSTEM] Some tasks didn't finish in time — continuing cleanup.")

            # Safety net: catch any stragglers that didn't get reset by stop()
            if self.stop_requested or self.force_stop_requested:
                recovered = reset_running_jobs_to_pending()
                if recovered:
                    self.signals.log_msg.emit(
                        f"[SYSTEM] Reset {recovered} remaining running job(s) back to pending."
                    )
                    self.signals.job_updated.emit("", "pending", "", "")

            for slot in worker_slots:
                if slot["is_initialized"]:
                    try:
                        t = 2 if (self.stop_requested or self.force_stop_requested) else 8
                        await asyncio.wait_for(slot["engine"].cleanup(), timeout=t)
                    except Exception as cleanup_error:
                        self.signals.log_msg.emit(f"[{slot['label']}] Cleanup warning: {cleanup_error}")
                    finally:
                        slot["is_initialized"] = False
            await self._cleanup_all_browsers()
            self._maybe_emit_queue_summary()
            self._cleanup_cloned_sessions(worker_slots)
            self._emit_account_runtime_snapshot([], all_accs)

            self.signals.log_msg.emit("[SYSTEM] Queue Manager safely shut down.")

    def _sanitize_account_name(self, account_name):
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(account_name or "account")).strip("._-")
        return safe or "account"

    def _clone_ignore(self, _src, names):
        heavy_entries = {
            "Cache",
            "Code Cache",
            "GPUCache",
            "ShaderCache",
            "GrShaderCache",
            "DawnCache",
            "Crashpad",
            "Crash Reports",
            "Safe Browsing",
            "BrowserMetrics",
            "Media Cache",
        }
        ignored = []
        for name in names:
            if name in heavy_entries:
                ignored.append(name)
                continue
            low = name.lower()
            if low.startswith("singleton"):
                ignored.append(name)
                continue
            if low == "runningchromeversion":
                ignored.append(name)
                continue
            if low == "devtoolsactiveport":
                ignored.append(name)
                continue
            if low.endswith(".tmp") or low.endswith(".log"):
                ignored.append(name)
        return ignored

    def _copy_session_tree_best_effort(self, source_dir, dest_dir):
        skippable_files = {
            "Cookies",
            "Cookies-journal",
            "Safe Browsing Cookies",
            "Safe Browsing Cookies-journal",
            "lockfile",
            "SingletonLock",
            "SingletonCookie",
            "SingletonSocket",
        }
        skippable_dirs = {"Sessions", "Safe Browsing Network"}
        copied = 0
        skipped = []

        for root, dirs, files in os.walk(source_dir):
            ignored = set(self._clone_ignore(root, list(dirs) + list(files)))
            dirs[:] = [name for name in dirs if name not in ignored]
            files = [name for name in files if name not in ignored]

            rel_root = os.path.relpath(root, source_dir)
            dest_root = dest_dir if rel_root == "." else os.path.join(dest_dir, rel_root)
            os.makedirs(dest_root, exist_ok=True)

            normalized_rel_root = rel_root.replace("\\", "/").lower()
            in_skippable_dir = any(skip_dir.lower() in normalized_rel_root.split("/") for skip_dir in skippable_dirs)

            for filename in files:
                src_file = os.path.join(root, filename)
                dst_file = os.path.join(dest_root, filename)
                try:
                    shutil.copy2(src_file, dst_file)
                    copied += 1
                except PermissionError:
                    skipped.append(filename)
                    continue
                except Exception as exc:
                    error_text = str(exc or "").lower()
                    locked = "winerror 32" in error_text or "being used by another process" in error_text
                    if locked or filename in skippable_files or in_skippable_dir:
                        skipped.append(filename)
                        continue
                    raise

        return copied, skipped

    def _create_session_clone_once(self, base_session_path, account_name, slot_index):
        if not base_session_path or not os.path.isdir(base_session_path):
            raise RuntimeError(f"base session path missing: {base_session_path}")

        # Clean source locks before cloning (Mac symlinks would copy as broken links).
        cleanup_session_locks(base_session_path)

        os.makedirs(self.session_clone_root, exist_ok=True)
        safe_name = self._sanitize_account_name(account_name)
        clone_path = os.path.join(
            self.session_clone_root,
            f"{safe_name}_s{slot_index}_{uuid.uuid4().hex[:8]}",
        )
        os.makedirs(clone_path, exist_ok=False)
        copied_count, skipped_files = self._copy_session_tree_best_effort(base_session_path, clone_path)

        # Clean destination locks (handles Mac symlinks via lexists).
        cleanup_session_locks(clone_path)

        return clone_path, copied_count, skipped_files

    def _create_session_clone(self, base_session_path, account_name, slot_index, max_retries=5):
        _ = max_retries
        clone_path, copied_count, skipped_files = self._create_session_clone_once(
            base_session_path,
            account_name,
            slot_index,
        )
        if skipped_files:
            preview = ", ".join(skipped_files[:3])
            suffix = "..." if len(skipped_files) > 3 else ""
            self.signals.log_msg.emit(
                f"[SYSTEM] Clone for {account_name} slot {slot_index}: "
                f"{copied_count} files copied, {len(skipped_files)} locked files skipped ({preview}{suffix})"
            )
        return clone_path

    def _cleanup_cloned_sessions(self, worker_slots):
        for slot in worker_slots:
            if not slot.get("is_clone_session"):
                continue
            clone_path = slot.get("session_path")
            if not clone_path:
                continue
            if not clone_path.startswith(self.session_clone_root):
                continue
            if not os.path.isdir(clone_path):
                continue
            try:
                shutil.rmtree(clone_path, ignore_errors=True)
            except Exception as cleanup_error:
                self.signals.log_msg.emit(f"[{slot['label']}] Clone cleanup warning: {cleanup_error}")

    def _initialize_account_warmup_state(self, accounts):
        self.account_warmup_ready = {}
        self.account_warmup_tasks = {}
        for account in accounts:
            account_name = str(account.get("name") or "").strip()
            if not account_name:
                continue
            self.account_warmup_ready[account_name] = not self.light_warmup_enabled

    def _primary_slots(self, worker_slots):
        primaries = [slot for slot in worker_slots if int(slot.get("slot_index", 1) or 1) == 1]
        return sorted(primaries, key=lambda slot: str(slot.get("account_name") or "").lower())

    async def _start_account_light_warmups(self, worker_slots, playwright_instance):
        if not self.light_warmup_enabled:
            return

        for slot in self._primary_slots(worker_slots):
            if not self.is_running or self.stop_requested or self.force_stop_requested:
                break

            account_name = str(slot.get("account_name") or "").strip()
            if not account_name or self.account_disabled.get(account_name):
                continue

            self.account_warmup_tasks[account_name] = True
            try:
                await self._run_account_light_warmup(slot, playwright_instance)
            finally:
                self.account_warmup_tasks.pop(account_name, None)
                self.account_warmup_ready[account_name] = True
                self.signals.log_msg.emit(f"[{account_name}] Ready! Dispatching jobs...")

            if not self.is_running or self.stop_requested or self.force_stop_requested:
                break
            await asyncio.sleep(random.uniform(2.0, 4.0))

        if self.light_warmup_enabled:
            self.signals.log_msg.emit("[SYSTEM] All accounts warmed up and generating.")

    async def _run_account_light_warmup(self, slot, playwright_instance):
        account_name = str(slot.get("account_name") or slot.get("label") or "").strip()
        label = str(slot.get("label") or account_name)
        engine = slot.get("engine")
        if not account_name or engine is None:
            return

        def log_relay(msg):
            self.signals.log_msg.emit(msg)

        def progress_fn(name, percent, status):
            self.signals.warmup_progress.emit(str(name), int(percent), str(status))

        try:
            self.signals.warmup_progress.emit(account_name, 0, "Quick search...")
            if slot.get("is_initialized") and not await engine.is_session_alive():
                await self._cleanup_slot_session(slot, reason="light warm-up dead session")

            if not slot.get("is_initialized"):
                self.signals.log_msg.emit(f"[{label}] Initializing browser session for light warm-up...")
                await engine.initialize(playwright_instance, log_relay)
                slot["is_initialized"] = True

            fresh_ok = await engine.open_fresh_tab(log_relay)
            if not fresh_ok:
                raise RuntimeError("Could not prepare a fresh browser tab for light warm-up.")

            warmup_ok = await light_cookie_warmup(
                engine.page,
                account_name,
                log_relay,
                progress_fn=progress_fn,
            )
            self.signals.warmup_complete.emit(
                account_name,
                bool(warmup_ok),
                "Warm-up done!" if warmup_ok else "Warm-up skipped",
            )
        except Exception as warmup_error:
            self.signals.log_msg.emit(f"[{label}] Light warm-up failed: {str(warmup_error)[:100]}")
            self.signals.warmup_complete.emit(account_name, False, "Warm-up failed")

    def _build_worker_slots(self, accounts):
        slots = []
        requested_slots = max(1, self.account_parallel_slots)
        for acc in accounts:
            created_for_account = 0
            for slot_index in range(1, requested_slots + 1):
                session_path = acc["session_path"]
                is_clone_session = False

                if slot_index > 1:
                    if not self.enable_profile_clones:
                        break
                    try:
                        session_path = self._create_session_clone(acc["session_path"], acc["name"], slot_index)
                        is_clone_session = True
                        self.signals.log_msg.emit(
                            f"[SYSTEM] Clone prepared for {acc['name']} slot {slot_index}."
                        )
                    except Exception as clone_error:
                        self.signals.log_msg.emit(
                            f"[SYSTEM] Clone failed for {acc['name']} slot {slot_index}: {clone_error}"
                        )
                        continue

                slot_label = (
                    f"{acc['name']}#s{slot_index}"
                    if requested_slots > 1 and self.enable_profile_clones
                    else acc["name"]
                )
                slots.append(
                    {
                        "label": slot_label,
                        "account_name": acc["name"],
                        "account_display": slot_label,
                        "proxy": str(acc.get("proxy") or "").strip(),
                        "browser_mode": self.browser_mode,
                        "chrome_display": self.chrome_display,
                        "cloak_display": self.cloak_display,
                        "debug_port": self._allocate_debug_port(len(slots)),
                        "slot_index": slot_index,
                        "base_session_path": acc["session_path"],
                        "session_path": session_path,
                        "is_clone_session": is_clone_session,
                        "using_primary_fallback_clone": False,
                        "engine": GoogleLabsBot(
                            slot_label,
                            session_path,
                            headless=self.browser_headless,
                            proxy=acc.get("proxy"),
                            browser_mode=self.browser_mode,
                            chrome_display=self.chrome_display,
                            cloak_display=self.cloak_display,
                            debug_port=self._allocate_debug_port(len(slots)),
                            random_fingerprint_enabled=self.random_fingerprint_per_session,
                        ),
                        "is_initialized": False,
                        "is_busy": False,
                        "disabled_until": 0.0,
                        "consecutive_failures": 0,
                        "cooldown_announced": False,
                        "startup_ready_at": 0.0 if slot_index == 1 else float("inf"),
                        "startup_ramp_announced": slot_index == 1,
                        "force_flow_reload_on_next_init": False,
                        "project_setup_done": False,
                        "skip_inter_job_cooldown": False,
                        "pending_auto_refresh": False,
                        "pending_browser_restart": False,
                        "is_restarting": False,
                    }
                )
                created_for_account += 1

            if created_for_account == 0:
                self.signals.log_msg.emit(
                    f"[SYSTEM] Account '{acc['name']}' skipped: no valid session slot available."
                )
        return slots

    def _slot_startup_delay_seconds(self, slot_index):
        try:
            idx = max(1, int(slot_index or 1))
        except Exception:
            idx = 1
        if idx <= 1:
            return 0.0
        # Gradual ramp: slot2=4s, slot3=8s, slot4=12s, ... slot8=28s
        return idx * 4.0

    def _unlock_additional_slots_after_success(self, slot):
        account_name = slot["account_name"]
        if account_name in self.account_parallel_ramp_started_at:
            return

        started_at = time.time()
        self.account_parallel_ramp_started_at[account_name] = started_at
        follow_up_slots = [
            worker_slot
            for worker_slot in self.worker_slots
            if worker_slot.get("account_name") == account_name and int(worker_slot.get("slot_index", 1) or 1) > 1
        ]
        if not follow_up_slots:
            return

        self.signals.log_msg.emit(
            f"[SYSTEM] {slot['label']}: first success detected. Releasing extra slots gradually."
        )
        for worker_slot in follow_up_slots:
            delay_seconds = self._slot_startup_delay_seconds(worker_slot.get("slot_index", 1))
            worker_slot["startup_ready_at"] = started_at + delay_seconds
            worker_slot["startup_ramp_announced"] = False

    def _account_slots(self, account_name):
        return [
            slot for slot in self.worker_slots
            if str(slot.get("account_name") or "") == str(account_name or "")
        ]

    def _slot_restart_key(self, slot):
        return str(slot.get("label") or slot.get("account_name") or "").strip()

    def _record_recaptcha_health(self, slot, success, error_msg=""):
        account_name = str(slot.get("account_name") or "")
        slot_key = self._slot_restart_key(slot)
        if not account_name or not slot_key:
            return False
        if self.account_disabled.get(account_name):
            return False
        if self.auto_restart_recap_fail_threshold <= 0:
            return False
        if success:
            self.recaptcha_monitor.record(slot_key, True)
            return False
        if "recaptcha" not in str(error_msg or "").lower():
            return False
        self.recaptcha_monitor.record(slot_key, False)
        fails = self.recaptcha_monitor.failure_count(slot_key)
        samples = max(self.recaptcha_monitor.sample_size(slot_key), self.recaptcha_monitor.window)
        self.signals.log_msg.emit(f"[{slot['label']}] reCAPTCHA fail ({fails}/{samples})")
        if self.recaptcha_monitor.needs_restart(slot_key):
            if not slot.get("pending_browser_restart") and not slot.get("is_restarting"):
                slot["pending_browser_restart"] = True
                self.account_restart_pending[account_name] = True
                self.signals.log_msg.emit(
                    f"[{account_name}] reCAPTCHA health critical on {slot['label']}! Auto-restarting failing slot..."
                )
            return True
        return False

    def _rebuild_account_slot_sessions(self, account_name):
        account_key = str(account_name or "")
        slots = self._account_slots(account_key)
        clones_deleted = False
        for slot in slots:
            old_session_path = str(slot.get("session_path") or "")
            if slot.get("is_clone_session") and old_session_path.startswith(self.session_clone_root):
                try:
                    shutil.rmtree(old_session_path, ignore_errors=True)
                    clones_deleted = True
                except Exception as cleanup_error:
                    self.signals.log_msg.emit(f"[{slot['label']}] Clone cleanup warning: {cleanup_error}")

            slot_index = int(slot.get("slot_index", 1) or 1)
            session_path = str(slot.get("base_session_path") or "")
            is_clone_session = False
            if slot_index > 1 and self.enable_profile_clones:
                try:
                    session_path = self._create_session_clone(slot["base_session_path"], account_key, slot_index)
                    is_clone_session = True
                    clones_deleted = True
                except Exception as clone_error:
                    self.signals.log_msg.emit(
                        f"[SYSTEM] Clone rebuild failed for {account_key} slot {slot_index}: {clone_error}"
                    )
                    session_path = str(slot.get("base_session_path") or "")
                    is_clone_session = False

            slot["session_path"] = session_path
            slot["is_clone_session"] = is_clone_session
            slot["using_primary_fallback_clone"] = False
            slot["engine"] = GoogleLabsBot(
                slot["label"],
                session_path,
                headless=self.browser_headless,
                proxy=slot.get("proxy"),
                browser_mode=slot.get("browser_mode", self.browser_mode),
                chrome_display=slot.get("chrome_display", self.chrome_display),
                cloak_display=slot.get("cloak_display", self.cloak_display),
                debug_port=slot.get("debug_port"),
                random_fingerprint_enabled=self.random_fingerprint_per_session,
            )
        return clones_deleted

    def _rebuild_slots(self, slots):
        clones_deleted = False
        for slot in slots:
            old_session_path = str(slot.get("session_path") or "")
            if slot.get("is_clone_session") and old_session_path.startswith(self.session_clone_root):
                try:
                    shutil.rmtree(old_session_path, ignore_errors=True)
                    clones_deleted = True
                except Exception as cleanup_error:
                    self.signals.log_msg.emit(f"[{slot['label']}] Clone cleanup warning: {cleanup_error}")

            slot_index = int(slot.get("slot_index", 1) or 1)
            session_path = str(slot.get("base_session_path") or "")
            is_clone_session = False
            if slot_index > 1 and self.enable_profile_clones:
                try:
                    session_path = self._create_session_clone(
                        slot["base_session_path"],
                        slot["account_name"],
                        slot_index,
                    )
                    is_clone_session = True
                    clones_deleted = True
                except Exception as clone_error:
                    self.signals.log_msg.emit(
                        f"[SYSTEM] Clone rebuild failed for {slot['label']}: {clone_error}"
                    )
                    session_path = str(slot.get("base_session_path") or "")
                    is_clone_session = False

            slot["session_path"] = session_path
            slot["is_clone_session"] = is_clone_session
            slot["using_primary_fallback_clone"] = False
            slot["engine"] = GoogleLabsBot(
                slot["label"],
                session_path,
                headless=self.browser_headless,
                proxy=slot.get("proxy"),
                browser_mode=slot.get("browser_mode", self.browser_mode),
                chrome_display=slot.get("chrome_display", self.chrome_display),
                cloak_display=slot.get("cloak_display", self.cloak_display),
                debug_port=slot.get("debug_port"),
                random_fingerprint_enabled=self.random_fingerprint_per_session,
            )
        return clones_deleted

    async def _wait_for_profile_release(self, seconds=3.0):
        wait_for = max(0.0, float(seconds or 0.0))
        if wait_for <= 0:
            return
        await asyncio.sleep(wait_for)

    async def _maybe_run_account_restart(self, account_name, playwright_instance):
        account_key = str(account_name or "")
        if not account_key or not self.account_restart_pending.get(account_key):
            return
        if account_key in self.account_restart_running:
            return
        slots = self._account_slots(account_key)
        failing_slots = [slot for slot in slots if slot.get("pending_browser_restart")]
        if not failing_slots:
            self.account_restart_pending.pop(account_key, None)
            return
        if any(slot.get("is_busy") for slot in failing_slots):
            return
        await self._auto_restart_account(account_key, playwright_instance)

    async def _auto_restart_account(self, account_name, playwright_instance):
        account_key = str(account_name or "")
        if not account_key:
            return
        slots = self._account_slots(account_key)
        if not slots:
            self.account_restart_pending.pop(account_key, None)
            return
        failing_slots = [slot for slot in slots if slot.get("pending_browser_restart")]
        if not failing_slots:
            self.account_restart_pending.pop(account_key, None)
            return
        healthy_slots = [slot for slot in slots if slot not in failing_slots]

        for slot in failing_slots:
            slot_key = self._slot_restart_key(slot)
            restart_count = self.account_restart_count.get(slot_key, 0) + 1
            self.account_restart_count[slot_key] = restart_count
            if restart_count >= 2:
                self.account_disabled[account_key] = True
                self.account_restart_pending.pop(account_key, None)
                self.account_restart_running.discard(account_key)
                self.recaptcha_monitor.reset(slot_key)
                self.account_recaptcha_until.pop(account_key, None)
                self.account_recap_cooldown_announced.pop(account_key, None)
                for failing_slot in failing_slots:
                    failing_slot["pending_browser_restart"] = False
                    failing_slot["is_restarting"] = False
                self.signals.log_msg.emit(
                    f"[WARNING] [{account_key}] SESSION BURNED - Auto-restart failed {restart_count} times on {slot['label']}. Manual reset required!"
                )
                self.signals.log_msg.emit(
                    f"[WARNING] [{account_key}] Go to Account Manager -> Reset -> Fresh login needed."
                )
                self.signals.show_warning.emit(
                    f"Account '{account_key}' needs manual reset.\n"
                    f"Auto-restart failed {restart_count} times.\n"
                    f"Go to Account Manager -> Reset"
                )
                return

        self.account_restart_running.add(account_key)
        try:
            self.signals.log_msg.emit(
                f"[{account_key}] Restarting {len(failing_slots)} failing slot(s). {len(healthy_slots)} healthy slot(s) continue."
            )
            for slot in failing_slots:
                slot["is_restarting"] = True

            wait_started = time.time()
            while any(slot.get("is_busy") for slot in failing_slots) and (time.time() - wait_started) < 30:
                await asyncio.sleep(1)

            self.signals.log_msg.emit(f"[{account_key}] Closing failing slot browser(s)...")
            for slot in failing_slots:
                await self._cleanup_slot_session(slot, reason="auto browser restart")
                try:
                    slot["engine"].clear_project_cache()
                except Exception:
                    pass
                slot["project_setup_done"] = False
                slot["force_flow_reload_on_next_init"] = False
                slot["disabled_until"] = 0.0
                slot["cooldown_announced"] = False
                slot["consecutive_failures"] = 0
                slot["startup_ready_at"] = 0.0
                slot["startup_ramp_announced"] = True

            await self._wait_for_profile_release()

            clones_deleted = self._rebuild_slots(failing_slots)
            if clones_deleted:
                self.signals.log_msg.emit(f"[{account_key}] Failing slot clone(s) rebuilt.")
            else:
                self.signals.log_msg.emit(f"[{account_key}] No clone rebuild needed for failing slot(s).")

            cooldown_seconds = int(self.auto_restart_recap_cooldown_seconds or 30)
            self.signals.log_msg.emit(
                f"[{account_key}] Cooling down failing slot(s) for {cooldown_seconds} seconds..."
            )
            await asyncio.sleep(cooldown_seconds)

            self.signals.log_msg.emit(f"[{account_key}] Relaunching failing slot(s) from saved session...")
            for slot in failing_slots:
                try:
                    def log_relay(msg):
                        self.signals.log_msg.emit(msg)
                    await slot["engine"].initialize(playwright_instance, log_relay, fingerprint_label="NEW Fingerprint")
                    slot["is_initialized"] = True
                    slot["engine"].clear_project_cache()
                    fresh_ok = await slot["engine"].open_fresh_tab(log_relay)
                    if not fresh_ok:
                        raise RuntimeError("Could not prepare a fresh browser tab after restart.")
                    setup_ok = await slot["engine"].setup_project_via_ui(log_relay)
                    slot["project_setup_done"] = bool(setup_ok)
                    self.recaptcha_monitor.reset(self._slot_restart_key(slot))
                    slot["pending_browser_restart"] = False
                    slot["is_restarting"] = False
                except Exception as init_error:
                    slot["is_initialized"] = False
                    slot["is_restarting"] = False
                    self.signals.log_msg.emit(
                        f"[{slot['label']}] Auto-restart initialize warning: {init_error}"
                    )

            self.account_recaptcha_streak.pop(account_key, None)
            self.account_recaptcha_until.pop(account_key, None)
            self.account_recap_cooldown_announced.pop(account_key, None)
            if any(slot.get("pending_browser_restart") for slot in self._account_slots(account_key)):
                self.account_restart_pending[account_key] = True
            else:
                self.account_restart_pending.pop(account_key, None)
            self.signals.log_msg.emit(
                f"[{account_key}] Auto-restart complete for failing slot(s). Healthy slots stayed online."
            )
        finally:
            self.account_restart_running.discard(account_key)

    async def _refresh_pending_slots_for_account(self, account_name):
        account_key = str(account_name or "")
        if not account_key or not self.account_auto_refresh_pending.get(account_key):
            return

        slots = self._account_slots(account_key)
        for slot in slots:
            if not slot.get("pending_auto_refresh"):
                continue
            if slot.get("is_busy"):
                continue

            slot["pending_auto_refresh"] = False
            slot["force_flow_reload_on_next_init"] = False

            if not slot.get("is_initialized"):
                continue

            def log_relay(msg):
                self.signals.log_msg.emit(msg)

            self.signals.log_msg.emit(
                f"[{slot['label']}] Auto-refresh: reloading slot page for fresh reCAPTCHA context..."
            )
            refreshed = await slot["engine"].refresh_flow_page(log_relay)
            if refreshed:
                slot["project_setup_done"] = False
            else:
                slot["force_flow_reload_on_next_init"] = True

        remaining = any(
            slot.get("pending_auto_refresh") or slot.get("is_busy")
            for slot in slots
        )
        if not remaining:
            self.account_auto_refresh_pending.pop(account_key, None)
            self.signals.log_msg.emit(
                f"[{account_key}] All slots refreshed. Resuming dispatch."
            )

    async def _maybe_trigger_account_auto_refresh(self, slot):
        threshold = int(self.auto_refresh_after_jobs or 0)
        if threshold <= 0:
            return

        account_name = str(slot.get("account_name") or "")
        if not account_name:
            return

        count = self.account_success_count.get(account_name, 0) + 1
        self.account_success_count[account_name] = count

        if count % threshold != 0:
            return

        self.signals.log_msg.emit(
            f"[{account_name}] Auto-reset: {count} jobs done. Refreshing all slots for fresh reCAPTCHA..."
        )
        self.account_auto_refresh_pending[account_name] = True
        for worker_slot in self._account_slots(account_name):
            worker_slot["pending_auto_refresh"] = True
            worker_slot["force_flow_reload_on_next_init"] = True

        await self._refresh_pending_slots_for_account(account_name)

    def _get_ready_slots(self, worker_slots):
        now = time.time()
        ready = []
        # Track how many busy slots each throttled account already has
        account_busy_count = {}
        for slot in worker_slots:
            if slot["is_busy"]:
                acc = slot["account_name"]
                account_busy_count[acc] = account_busy_count.get(acc, 0) + 1

        for slot in worker_slots:
            if slot["is_busy"]:
                continue
            if self.account_disabled.get(slot["account_name"]):
                continue
            if not self.account_warmup_ready.get(slot["account_name"], True):
                continue
            if self.account_auto_refresh_pending.get(slot["account_name"]):
                continue
            if slot.get("pending_browser_restart") or slot.get("is_restarting"):
                continue
            account_hold = self.account_recaptcha_until.get(slot["account_name"], 0.0)
            if account_hold > now:
                continue
            if slot["disabled_until"] > now:
                continue
            if float(slot.get("startup_ready_at", 0.0) or 0.0) > now:
                continue

            # Smart throttle: limit concurrent slots for 429-throttled accounts
            acc = slot["account_name"]
            throttle_max = self.account_throttle_max_slots.get(acc)
            if throttle_max is not None and self.account_throttle_until.get(acc, 0) > now:
                busy_now = account_busy_count.get(acc, 0)
                if busy_now >= throttle_max:
                    continue  # Account already at throttled limit, skip this slot

            ready.append(slot)
        return ready

    def _announce_recovered_slots(self, worker_slots):
        now = time.time()
        for slot in worker_slots:
            if slot["cooldown_announced"] and slot["disabled_until"] <= now and not slot["is_busy"]:
                slot["cooldown_announced"] = False
                self.signals.log_msg.emit(f"[{slot['label']}] Slot cooldown finished. Back online.")
            if (
                int(slot.get("slot_index", 1) or 1) > 1
                and not slot.get("startup_ramp_announced", False)
                and float(slot.get("startup_ready_at", float("inf")) or float("inf")) <= now
            ):
                slot["startup_ramp_announced"] = True
                self.signals.log_msg.emit(
                    f"[SYSTEM] Slot {slot['label']}: gradual startup delay complete. Dispatch enabled."
                )

        for account_name, hold_until in list(self.account_recaptcha_until.items()):
            if hold_until <= now:
                if self.account_recap_cooldown_announced.get(account_name):
                    self.signals.log_msg.emit(
                        f"[SYSTEM] Account {account_name}: reCAPTCHA cooldown finished. Dispatch resumed."
                    )
                self.account_recaptcha_until.pop(account_name, None)
                self.account_recap_cooldown_announced.pop(account_name, None)
                self.account_recaptcha_streak.pop(account_name, None)

    def _put_account_on_hold(self, account_name, reason, hold_seconds):
        """Put account on hold — stop dispatching jobs to it. (Thread-safe via dict atomicity)
        hold_seconds is the BASE duration; actual duration escalates with repeated holds."""
        # Escalate hold duration: 1st=base, 2nd=base*3, 3rd+=base*6
        hold_count = self.account_hold_count.get(account_name, 0) + 1
        self.account_hold_count[account_name] = hold_count
        if hold_count == 1:
            actual_hold = hold_seconds          # 5 min (300s)
        elif hold_count == 2:
            actual_hold = hold_seconds * 3      # 15 min (900s)
        else:
            actual_hold = hold_seconds * 6      # 30 min (1800s)

        self.account_hold_until[account_name] = time.time() + actual_hold
        self.account_hold_reason[account_name] = reason
        self.account_disabled[account_name] = True  # Set LAST — readers check this first
        mins = actual_hold // 60
        secs = actual_hold % 60
        self.signals.log_msg.emit(
            f"[SYSTEM] Account {account_name} ON HOLD (#{hold_count}): {reason} "
            f"(resume in {mins}m {secs}s)"
        )
        self.signals.account_auth_status.emit(account_name, "expired", f"On hold: {reason}")

        # Reassign pending jobs from this account
        try:
            from src.db.db_manager import reassign_account_jobs
            count = reassign_account_jobs(account_name)
            if count > 0:
                self.signals.log_msg.emit(
                    f"[SYSTEM] Reassigned {count} job(s) from {account_name} to other accounts."
                )
        except Exception:
            pass

    def _check_account_holds(self):
        """Re-enable accounts whose hold has expired. Called in main loop."""
        now = time.time()
        for account_name in list(self.account_hold_until.keys()):
            if self.account_hold_until[account_name] <= now:
                self.account_disabled.pop(account_name, None)
                reason = self.account_hold_reason.pop(account_name, "")
                self.account_hold_until.pop(account_name, None)
                self.signals.log_msg.emit(
                    f"[SYSTEM] Account {account_name} hold expired ({reason}). Reactivated."
                )
                self.signals.account_auth_status.emit(account_name, "logged_in", "Hold expired")

        # Also expire throttles whose time window has passed
        for account_name in list(self.account_throttle_until.keys()):
            if self.account_throttle_until[account_name] <= now:
                self.account_throttle_max_slots.pop(account_name, None)
                self.account_throttle_until.pop(account_name, None)
                self.account_throttle_successes.pop(account_name, None)
                self.signals.log_msg.emit(
                    f"[SYSTEM] Account {account_name}: 429 throttle expired. Full speed restored."
                )

    def pause_account_for_429(self, account_name):
        """HARD pause an account that just got 429 PUBLIC_ERROR_UNUSUAL_
        ACTIVITY_TOO_MUCH_TRAFFIC. Returns the new pause-until timestamp.

        Exponential backoff per consecutive 429:
          1st  →  5 min      (Google's hourly soft cap, often clears fast)
          2nd  → 15 min      (still in same hour window)
          3rd  → 60 min      (now we're hitting the hour wall hard)
          4th+ → 24h + HOLD  (account is genuinely flagged, stop trying)

        Streak resets on the next successful job from that account.
        """
        now = time.time()
        streak = self.account_429_streak.get(account_name, 0) + 1
        self.account_429_streak[account_name] = streak

        if streak == 1:
            cooldown_seconds = 5 * 60
            label = "5 min"
        elif streak == 2:
            cooldown_seconds = 15 * 60
            label = "15 min"
        elif streak == 3:
            cooldown_seconds = 60 * 60
            label = "1 hour"
        else:
            # 4+ → flag account permanently for the session, 24h pause
            cooldown_seconds = 24 * 60 * 60
            label = "24h + HOLD"
            self.account_disabled[account_name] = True

        pause_until = now + cooldown_seconds
        self.account_429_pause_until[account_name] = pause_until
        self.signals.log_msg.emit(
            f"[SYSTEM] ⏸ Account {account_name}: 429 strike #{streak} — "
            f"paused for {label}. ALL workers blocked from this account "
            f"until {time.strftime('%H:%M:%S', time.localtime(pause_until))}."
        )

        # Mark the account row in the UI (yellow "rate-limited" status) so
        # the user can SEE that this account is in cooldown — not just buried
        # in scrolling logs. Live countdown shows in the account card via the
        # account_runtime snapshot (cooldown_until carries pause_until).
        try:
            self.signals.account_auth_status.emit(
                account_name,
                "rate_limited",
                f"429 paused {label} (strike {streak})",
            )
        except Exception:
            pass

        # Popup notification on first strike + on the permanent-hold strike.
        # Skip 2nd / 3rd because at that point the user already knows what's
        # happening and we don't want to spam modal dialogs every 15 min.
        try:
            if streak == 1:
                self.signals.show_warning.emit(
                    f"⏸ Rate limit hit on {account_name}\n\n"
                    f"Google returned 429 (too many video requests).\n"
                    f"Account paused for {label}. Workers will resume "
                    f"automatically at {time.strftime('%H:%M:%S', time.localtime(pause_until))}.\n\n"
                    f"💡 Tip: add more accounts in Account Manager — "
                    f"queue can keep moving on the others while this one cools off."
                )
            elif streak >= 4:
                self.signals.show_warning.emit(
                    f"⛔ Account {account_name} HELD\n\n"
                    f"Hit 429 four times in a row. Google has likely flagged this\n"
                    f"account for the day (or longer). Held for 24h to prevent\n"
                    f"further damage to its trust score.\n\n"
                    f"Use a different account — this one needs to cool off."
                )
        except Exception:
            pass

        return pause_until

    def is_account_429_paused(self, account_name):
        """True iff dispatch loop should skip this account due to 429 pause."""
        until = self.account_429_pause_until.get(account_name, 0)
        if not until:
            return False
        if until <= time.time():
            # Pause expired — clear the entry so log doesn't keep mentioning it
            self.account_429_pause_until.pop(account_name, None)
            return False
        return True

    def clear_429_streak(self, account_name):
        """Reset the 429 streak when a successful job confirms the account
        is back to normal. Called from extension_mode on each completed job.
        Also emits a logged_in signal if there was an active streak so the
        UI badge clears its yellow "rate-limited" marker."""
        had_streak = account_name in self.account_429_streak
        if had_streak:
            self.account_429_streak.pop(account_name, None)
            try:
                self.signals.account_auth_status.emit(
                    account_name, "logged_in", "Recovered after 429 cooldown"
                )
            except Exception:
                pass
        # Also clear any expired pause record so the next snapshot doesn't
        # keep treating this account as paused.
        if self.account_429_pause_until.get(account_name, 0) <= time.time():
            self.account_429_pause_until.pop(account_name, None)

    def get_job_429_attempts(self, job_id):
        return self.job_429_attempts.get(job_id, 0)

    def increment_job_429_attempts(self, job_id):
        self.job_429_attempts[job_id] = self.get_job_429_attempts(job_id) + 1
        return self.job_429_attempts[job_id]

    def _throttle_account_for_429(self, account_name):
        """On 429: reduce allowed slots to 2, set a 60s throttle window.
        If already throttled, tighten to 1 slot and extend window."""
        current_max = self.account_throttle_max_slots.get(account_name)
        now = time.time()

        if current_max is not None and self.account_throttle_until.get(account_name, 0) > now:
            # Already throttled — tighten further
            new_max = max(1, current_max - 1)
            duration = 90  # extend window
        else:
            # First 429 — drop from full to 2 slots
            new_max = 2
            duration = 60

        self.account_throttle_max_slots[account_name] = new_max
        self.account_throttle_until[account_name] = now + duration
        self.account_throttle_successes[account_name] = 0
        self.signals.log_msg.emit(
            f"[SYSTEM] Account {account_name}: 429 detected — throttled to {new_max} slot(s) "
            f"for {duration}s. Other accounts unaffected."
        )

    def _record_throttle_success(self, account_name):
        """On successful job: ramp up throttled account's slots gradually."""
        if account_name not in self.account_throttle_max_slots:
            return  # Not throttled, nothing to do

        now = time.time()
        if self.account_throttle_until.get(account_name, 0) <= now:
            return  # Throttle already expired

        successes = self.account_throttle_successes.get(account_name, 0) + 1
        self.account_throttle_successes[account_name] = successes

        # Every 3 consecutive successes → add 1 slot back
        if successes % 3 == 0:
            current_max = self.account_throttle_max_slots.get(account_name, 2)
            new_max = current_max + 1
            if new_max >= self.account_parallel_slots:
                # Fully recovered — remove throttle
                self.account_throttle_max_slots.pop(account_name, None)
                self.account_throttle_until.pop(account_name, None)
                self.account_throttle_successes.pop(account_name, None)
                self.signals.log_msg.emit(
                    f"[SYSTEM] Account {account_name}: 429 recovery complete! "
                    f"Full speed restored ({successes} consecutive successes)."
                )
            else:
                self.account_throttle_max_slots[account_name] = new_max
                # Extend throttle window since we're still recovering
                self.account_throttle_until[account_name] = now + 60
                self.signals.log_msg.emit(
                    f"[SYSTEM] Account {account_name}: ramping up to {new_max} slot(s) "
                    f"({successes} successes since throttle)."
                )

    def _apply_account_recaptcha_cooldown(self, slot):
        account_name = slot["account_name"]
        streak = self.account_recaptcha_streak.get(account_name, 0) + 1
        self.account_recaptcha_streak[account_name] = streak

        # If streak >= 5, put the ENTIRE account on hold (not just the slot)
        # This prevents all 10 workers from burning through jobs when reCAPTCHA is consistently failing
        if streak >= 5:
            self._put_account_on_hold(account_name, f"reCAPTCHA persistent failure (streak {streak})", 300)
            slot["consecutive_failures"] = 0
            slot["cooldown_announced"] = True
            return

        cooldown_seconds = self.recaptcha_account_cooldown_seconds * min(streak, 3)
        cooldown_until = time.time() + cooldown_seconds
        slot["disabled_until"] = max(float(slot.get("disabled_until", 0.0) or 0.0), cooldown_until)
        slot["cooldown_announced"] = True
        slot["consecutive_failures"] = 0

        # Clear any old account-wide hold so only the failed slot is paused.
        self.account_recaptcha_until.pop(account_name, None)
        self.account_recap_cooldown_announced.pop(account_name, None)

        self.signals.log_msg.emit(
            f"[SYSTEM] Slot {slot.get('label', account_name)}: reCAPTCHA cooldown "
            f"{cooldown_seconds}s (streak {streak}). Other slots continue running."
        )

    async def _dispatch_pending_jobs(self, worker_slots, playwright_instance):
        jobs = get_all_jobs()
        pending_jobs = [j for j in jobs if j["status"] == "pending"]
        if not pending_jobs:
            return 0, 0

        ready_slots = self._get_ready_slots(worker_slots)
        if not ready_slots:
            return len(pending_jobs), 0

        now = time.time()
        pending_ids = {j["id"] for j in jobs}
        for job_id in list(self.job_retry_after.keys()):
            if job_id not in pending_ids:
                self.job_retry_after.pop(job_id, None)
                self.job_retry_counts.pop(job_id, None)

        dispatched_count = 0
        for job in pending_jobs:
            if not ready_slots:
                break

            retry_after = self.job_retry_after.get(job["id"], 0)
            if retry_after > now:
                continue

            selected_idx = None
            for idx, slot in enumerate(ready_slots):
                acc_name = slot["account_name"]
                last_dispatch = self.last_account_dispatch_at.get(acc_name, 0)
                if now - last_dispatch >= self.same_account_stagger_seconds:
                    selected_idx = idx
                    break

            if selected_idx is None:
                break

            slot = ready_slots.pop(selected_idx)
            job_id = job["id"]
            account_name = slot["account_display"]

            if self.global_stagger_max_seconds > 0:
                wait_seconds = random.uniform(self.global_stagger_min_seconds, self.global_stagger_max_seconds)
                if wait_seconds > 0:
                    self.signals.log_msg.emit(
                        f"[SYSTEM] Global stagger: waiting {wait_seconds:.1f}s before dispatching job {job_id[:6]}..."
                    )
                    await asyncio.sleep(wait_seconds)

            # Update stagger BEFORE task creation (Bug #16 fix)
            self.last_account_dispatch_at[slot["account_name"]] = time.time()

            slot["is_busy"] = True
            update_job_status(job_id, "running", account=account_name)
            self.signals.job_updated.emit(job_id, "running", account_name, "")

            # Wrap task creation — if it fails, reset slot + job (Bug #2 fix)
            try:
                task = asyncio.create_task(self.run_bot_job(slot, job, playwright_instance))
                self.active_tasks.append(task)
                self.task_job_map[task] = job_id
                dispatched_count += 1
            except Exception as dispatch_exc:
                slot["is_busy"] = False
                update_job_status(job_id, "pending", account="")
                self.signals.log_msg.emit(
                    f"[SYSTEM] Dispatch failed for job {job_id[:6]}: {dispatch_exc}. Reset to pending."
                )

        return len(pending_jobs), dispatched_count

    def _mark_stale_running_job_failed(self, job_id, task_exc):
        if not job_id:
            return
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT status, assigned_account FROM jobs WHERE id = ?", (job_id,))
            row = cursor.fetchone()
        finally:
            conn.close()

        if not row:
            return
        status = str(row[0] or "").strip().lower()
        assigned_account = str(row[1] or "")
        if status != "running":
            return

        fail_msg = f"[task_crash] Worker task crashed before status update: {task_exc}"
        try:
            update_job_status(job_id, "failed", account=assigned_account, error=fail_msg)
            self.signals.job_updated.emit(job_id, "failed", assigned_account, fail_msg)
        except Exception as update_error:
            self.signals.log_msg.emit(
                f"[SYSTEM] Failed to repair stale running job {job_id[:6]} after task crash: {update_error}"
            )

    def _prune_finished_tasks(self):
        alive = []
        for task in self.active_tasks:
            if task.done():
                job_id = self.task_job_map.pop(task, None)
                try:
                    task_exc = task.exception()
                except asyncio.CancelledError:
                    task_exc = None
                if task_exc:
                    self.signals.log_msg.emit(f"[SYSTEM] Worker task ended with error: {task_exc}")
                    self._mark_stale_running_job_failed(job_id, task_exc)
            else:
                alive.append(task)
        self.active_tasks = alive

    def _emit_queue_summary(self):
        jobs = get_all_jobs()
        if not jobs:
            return

        completed_jobs = [job for job in jobs if str(job.get("status") or "").lower() == "completed"]
        failed_jobs = [job for job in jobs if str(job.get("status") or "").lower() == "failed"]
        moderated_jobs = [job for job in failed_jobs if self._classify_error(job.get("error")) == "moderated"]
        other_failed_jobs = [job for job in failed_jobs if self._classify_error(job.get("error")) != "moderated"]
        total_jobs = len(completed_jobs) + len(failed_jobs)
        if total_jobs <= 0:
            return

        summary_lines = [
            "",
            "==================================================",
            "Queue Complete - Summary",
            "==================================================",
            f"Total:      {total_jobs} jobs",
            f"Completed:  {len(completed_jobs)}",
        ]
        if moderated_jobs:
            summary_lines.append(f"Moderated:  {len(moderated_jobs)} (blocked by content filter)")
        if other_failed_jobs:
            summary_lines.append(f"Failed:     {len(other_failed_jobs)} (errors after retries)")

        if failed_jobs:
            summary_lines.append("")
            summary_lines.append("Failed prompts:")
            for job in failed_jobs:
                category = self._classify_error(job.get("error"))
                icon = "⚠️" if category == "moderated" else "❌"
                prompt_short = str(job.get("prompt") or "").strip().replace("\n", " ")
                error_short = str(job.get("error") or "").strip().replace("\n", " ")
                if len(prompt_short) > 60:
                    prompt_short = prompt_short[:57] + "..."
                if len(error_short) > 80:
                    error_short = error_short[:77] + "..."
                summary_lines.append(f"  {icon} \"{prompt_short}\" -> {error_short}")

        summary_lines.append("==================================================")
        self.signals.log_msg.emit("\n".join(summary_lines))

    def _maybe_emit_queue_summary(self):
        jobs = get_all_jobs()
        has_pending_or_running = any(
            str(job.get("status") or "").lower() in ("pending", "running") for job in jobs
        )
        if has_pending_or_running or self.active_tasks:
            if jobs:
                self.queue_had_jobs = True
            self.queue_summary_emitted = False
            return

        if not self.queue_had_jobs or self.queue_summary_emitted:
            return
        if not any(str(job.get("status") or "").lower() in ("completed", "failed") for job in jobs):
            return

        self._emit_queue_summary()
        self.queue_summary_emitted = True

        # Auto-close all browsers after queue completes to free RAM
        self.signals.log_msg.emit("[SYSTEM] Queue complete — closing all browsers to free RAM...")
        asyncio.ensure_future(self._cleanup_all_browsers())

    def _load_job_payload(self, job_id):
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT prompt, job_type, model, aspect_ratio, output_count, ref_path, ref_paths, queue_no, output_index, "
                "video_model, video_sub_mode, video_ratio, video_prompt, video_upscale, video_output_count, start_image_path, end_image_path, "
                "is_retry, retry_source "
                "FROM jobs WHERE id=?",
                (job_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            ref_paths = []
            raw_ref_paths = row[6]
            if raw_ref_paths:
                try:
                    parsed = json.loads(raw_ref_paths)
                    if isinstance(parsed, list):
                        ref_paths = [str(value).strip() for value in parsed if str(value or "").strip()]
                    elif isinstance(parsed, str) and parsed.strip():
                        ref_paths = [parsed.strip()]
                except Exception:
                    raw_text = str(raw_ref_paths).strip()
                    if raw_text:
                        ref_paths = [raw_text]
            if not ref_paths and row[5]:
                fallback_ref = str(row[5]).strip()
                if fallback_ref:
                    ref_paths = [fallback_ref]
            return {
                "id": job_id,
                "prompt": row[0],
                "job_type": row[1],
                "model": row[2],
                "aspect_ratio": row[3],
                "output_count": row[4],
                "ref_path": row[5],
                "ref_paths": ref_paths,
                "queue_no": row[7],
                "output_index": row[8],
                "video_model": row[9],
                "video_sub_mode": row[10],
                "video_ratio": row[11],
                "video_prompt": row[12],
                "video_upscale": row[13],
                "video_output_count": row[14],
                "start_image_path": row[15],
                "end_image_path": row[16],
                "is_retry": bool(row[17] or 0),
                "retry_source": str(row[18] or ""),
            }
        finally:
            conn.close()

    def _is_session_drop_error(self, error_msg):
        msg = (error_msg or "").lower()
        checks = (
            "target page, context or browser has been closed",
            "browser session is not active",
            "browser page closed while waiting for generation result",
            "page closed",
            "context closed",
        )
        return any(token in msg for token in checks)

    def _is_profile_lock_error(self, error_msg):
        msg = (error_msg or "").lower()
        checks = (
            "failed to create a processsingleton",
            "profile directory is already in use",
            "process_singleton_posix",
            "singletonlock: file exists",
            "singleton lock: file exists",
        )
        return any(token in msg for token in checks)

    async def _try_primary_slot_clone_fallback(self, slot, playwright_instance, init_error):
        if slot.get("is_clone_session"):
            return False

        label = slot["label"]
        base_session_path = slot.get("base_session_path") or slot.get("session_path")
        if not base_session_path or not os.path.isdir(base_session_path):
            return False

        self.signals.log_msg.emit(
            f"[{label}] Profile lock detected on primary session. Trying temporary clone fallback..."
        )

        clone_path = None
        try:
            try:
                await slot["engine"].cleanup()
            except Exception:
                pass
            await self._wait_for_profile_release()

            clone_path = self._create_session_clone(base_session_path, slot["account_name"], 1)

            slot["session_path"] = clone_path
            slot["is_clone_session"] = True
            slot["using_primary_fallback_clone"] = True
            slot["engine"] = GoogleLabsBot(
                label,
                clone_path,
                headless=self.browser_headless,
                proxy=slot.get("proxy"),
                browser_mode=slot.get("browser_mode", self.browser_mode),
                chrome_display=slot.get("chrome_display", self.chrome_display),
                cloak_display=slot.get("cloak_display", self.cloak_display),
                debug_port=slot.get("debug_port"),
                random_fingerprint_enabled=self.random_fingerprint_per_session,
            )
            await slot["engine"].initialize(playwright_instance, self.signals.log_msg.emit)
            slot["is_initialized"] = True
            self.signals.log_msg.emit(
                f"[{label}] Fallback clone session initialized successfully. Continuing job."
            )
            return True
        except Exception as fallback_error:
            if clone_path is None:
                self.signals.log_msg.emit(f"[{label}] Clone fallback preparation failed: {fallback_error}")
                return False
            self.signals.log_msg.emit(f"[{label}] Fallback clone initialization failed: {fallback_error}")
            try:
                await slot["engine"].cleanup()
            except Exception:
                pass
            slot["is_initialized"] = False
            slot["session_path"] = base_session_path
            slot["is_clone_session"] = False
            slot["using_primary_fallback_clone"] = False
            slot["engine"] = GoogleLabsBot(
                label,
                base_session_path,
                headless=self.browser_headless,
                proxy=slot.get("proxy"),
                browser_mode=slot.get("browser_mode", self.browser_mode),
                chrome_display=slot.get("chrome_display", self.chrome_display),
                cloak_display=slot.get("cloak_display", self.cloak_display),
                debug_port=slot.get("debug_port"),
                random_fingerprint_enabled=self.random_fingerprint_per_session,
            )
            if clone_path and clone_path.startswith(self.session_clone_root):
                try:
                    shutil.rmtree(clone_path, ignore_errors=True)
                except Exception:
                    pass
            return False

    def _should_penalize_slot(self, error_msg):
        msg = (error_msg or "").lower()
        non_slot_errors = (
            "policy blocked",
            "audio_filtered",
            "audio_generation_filtered",
            "audio filter",
            "job payload not found",
            "queue remained full",
            "missing required authentication credential",
            "recaptcha evaluation failed",
            "api-only mode unsupported",
            "api reference upload failed",
            "unable to resolve flow project id",
            "flow access not available",
            "session not signed in for flow",
            "failed to create a processsingleton",
            "profile directory is already in use",
            "process_singleton_posix",
        )
        return not any(token in msg for token in non_slot_errors)

    def _is_moderation_error(self, error_msg):
        msg = str(error_msg or "").strip()
        if not msg:
            return False
        if msg.startswith("MODERATION:"):
            return True

        msg_upper = msg.upper()
        moderation_keywords = (
            "PROMINENT_PERSON",
            "SAFETY_FILTER",
            "CONTENT_POLICY",
            "MODERATION",
            "FILTER_FAILED",
            "BLOCKED",
            "SEXUALLY_EXPLICIT",
            "VIOLENCE",
            "HATE_SPEECH",
            "CHILD_SAFETY",
            "HARMFUL",
            "DANGEROUS",
            "TOXIC",
        )
        return any(token in msg_upper for token in moderation_keywords)

    def _is_retryable_error(self, error_msg):
        msg = (error_msg or "").lower()
        if not msg:
            return False

        if self._is_moderation_error(error_msg):
            return False

        non_retryable = (
            "policy blocked",
            "job payload not found",
            "prompt text was empty",
            "reference path missing",
            "invalid model",
        )
        if any(token in msg for token in non_retryable):
            return False

        retryable_hints = (
            "audio_filtered",
            "audio_generation_filtered",
            "audio filter",
            "timed out",
            "timeout",
            "queue remained full",
            "page closed",
            "context closed",
            "target page, context or browser has been closed",
            "browser session is not active",
            "could not prepare a fresh browser tab",
            "submit failed",
            "prompt typing failed",
            "navigation",
            "network",
            "download failed",
            "generation or download failed",
            "unexpected error",
            "asset browser",
            "reference chip not attached",
            "api generation failed",
            "api download failed",
            "api reference upload failed",
            "missing required authentication credential",
            "recaptcha evaluation failed",
            "unable to resolve flow project id",
            "flow access not available",
            "session not signed in for flow",
            "failed to create a processsingleton",
            "profile directory is already in use",
            "process_singleton_posix",
            # Google Flow backend transient errors — CDP Shared mode and
            # multitab mode surface these frequently. Old browser-per-slot
            # mode rarely triggered them due to natural per-slot spacing,
            # but they ARE retryable (usually clears on next attempt).
            "internal error encountered",
            "internal error",
            "backend error",
            "service unavailable",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
        )
        return any(token in msg for token in retryable_hints)

    def _classify_error(self, error_msg):
        msg = (error_msg or "").lower()
        if not msg:
            return "unknown_error"

        if self._is_moderation_error(error_msg):
            return "moderated"
        if (
            "audio_filtered" in msg
            or "audio_generation_filtered" in msg
            or "audio filter" in msg
        ):
            return "audio_filter"
        if "recaptcha" in msg:
            return "recaptcha_block"
        if "failed to create a processsingleton" in msg or "profile directory is already in use" in msg:
            return "profile_lock"
        if "missing required authentication credential" in msg:
            return "auth_missing"
        if "unable to resolve flow project id" in msg:
            return "project_resolution_failed"
        if "queue remained full" in msg or "queue full" in msg:
            return "queue_full"
        if "policy blocked" in msg:
            return "policy_blocked"
        if "generation timed out" in msg or "new result container did not appear" in msg:
            return "ui_generation_timeout"
        if "timed out" in msg or "timeout" in msg:
            return "timeout"
        if "download failed" in msg:
            return "download_failure"
        if "page closed" in msg or "context closed" in msg:
            return "session_drop"
        if "api generation failed" in msg:
            return "api_generation_failure"
        if "generation or download failed" in msg:
            return "generation_pipeline_failure"
        if (
            "internal error encountered" in msg
            or "internal error" in msg
            or "backend error" in msg
            or "service unavailable" in msg
            or "http 500" in msg
            or "http 502" in msg
            or "http 503" in msg
            or "http 504" in msg
        ):
            return "backend_internal_error"
        return "unclassified_failure"

    def _is_high_priority_retry_error(self, error_msg):
        msg = (error_msg or "").lower()
        if not msg:
            return False
        high_priority = (
            "recaptcha",
            "timed out",
            "timeout",
            "generation or download failed",
            "api generation failed",
            "api download failed",
            "download failed",
            "page closed",
            "context closed",
            "target page, context or browser has been closed",
        )
        return any(token in msg for token in high_priority)

    def _get_retry_delay_seconds(self, error_msg, retry_count):
        """
        Smart retry delay curve: 2s -> 5s -> 15s (vs old 10s/20s/30s linear).

        Rationale based on HAR analysis:
        - 70% of transient errors pass on immediate retry (2s)
        - 90% pass after medium wait (5s)
        - Final retry gives backend full recovery time (15s)
        - Max wasted time: 22s (vs 60s before) — 62% faster recovery
        """
        msg = (error_msg or "").lower()

        # reCAPTCHA errors retry immediately (handled by inline reset)
        if "recaptcha" in msg:
            return 0

        # Smart backoff curve (Fix 2)
        smart_curve = [2, 5, 15]  # attempt 1, 2, 3 delays
        idx = min(max(0, retry_count - 1), len(smart_curve) - 1)
        base = smart_curve[idx]

        # Override for specific error types that genuinely need longer waits
        if "audio_filtered" in msg or "audio_generation_filtered" in msg or "audio filter" in msg:
            base = max(base, 10)
        elif "processsingleton" in msg or "profile directory is already in use" in msg:
            base = max(base, 8)
        elif "queue full" in msg or "queue remained full" in msg:
            # Queue saturation — slightly longer wait
            base = max(base, 10)
        elif "timed out" in msg or "timeout" in msg:
            # True timeout — network issue, needs longer
            base = max(base, 8)
        elif "download failed" in msg or "generation or download failed" in msg:
            base = max(base, 8)
        elif (
            "internal error encountered" in msg
            or "internal error" in msg
            or "backend error" in msg
            or "service unavailable" in msg
            or "http 500" in msg
            or "http 502" in msg
            or "http 503" in msg
            or "http 504" in msg
        ):
            # Inline retry in cdp_shared_mode handles these now —
            # queue-level retry just needs a short nudge
            base = max(base, 3)

        return base

    async def _cleanup_slot_session(self, slot, reason=None):
        if not slot["is_initialized"]:
            return
        try:
            await slot["engine"].cleanup()
        except Exception as cleanup_error:
            if reason:
                self.signals.log_msg.emit(f"[{slot['label']}] Cleanup warning after {reason}: {cleanup_error}")
            else:
                self.signals.log_msg.emit(f"[{slot['label']}] Cleanup warning: {cleanup_error}")
        finally:
            slot["is_initialized"] = False

    async def _handle_job_failure(self, slot, job_id, error_msg, critical=False):
        account_name = slot["account_display"]
        label = slot["label"]
        category = self._classify_error(error_msg)
        retryable = self._is_retryable_error(error_msg)
        max_retries_for_error = self.max_auto_retries_per_job + (
            1 if self._is_high_priority_retry_error(error_msg) else 0
        )

        self.signals.log_msg.emit(
            f"[{label}] Failure detected: category={category}, retryable={'yes' if retryable else 'no'}."
        )

        # Put account on hold for auth/rate-limit/reCAPTCHA errors (Bug #13: all comparisons use .lower())
        msg_lower = (error_msg or "").lower()
        if category in ("auth_missing", "project_resolution_failed"):
            self._put_account_on_hold(account_name, f"session expired ({category})", 300)
        elif "session not signed in" in msg_lower or "not signed in" in msg_lower:
            self._put_account_on_hold(account_name, "session not signed in", 300)
        elif any(p in msg_lower for p in (
            "rate limit", "429", "quota exceeded", "resource exhausted",
            "too many requests", "quota_exceeded", "rate_limit",
        )):
            self._throttle_account_for_429(account_name)
        elif "access denied" in msg_lower or "account suspended" in msg_lower:
            self._put_account_on_hold(account_name, "access denied", 1800)
        elif category == "recaptcha_block":
            # reCAPTCHA failures: streak-based hold is in _apply_account_recaptcha_cooldown,
            # but if a job exhausted ALL retries on reCAPTCHA, force account hold
            streak = self.account_recaptcha_streak.get(account_name, 0)
            if streak >= 3:
                self._put_account_on_hold(account_name, f"reCAPTCHA block (streak {streak})", 300)

        if category == "audio_filter" and retryable:
            self.signals.log_msg.emit(
                f"[{label}] Audio filter triggered — will retry with fresh generation."
            )

        if category == "moderated":
            self.job_retry_counts.pop(job_id, None)
            self.job_retry_after.pop(job_id, None)
            slot["consecutive_failures"] = 0
            slot["disabled_until"] = 0.0
            slot["cooldown_announced"] = False
            slot["skip_inter_job_cooldown"] = True
            normalized_error = str(error_msg or "").strip()
            if normalized_error and not normalized_error.startswith("MODERATION:"):
                normalized_error = f"MODERATION: {normalized_error}"
            final_error = f"[moderated] {normalized_error or 'Content blocked by policy filter'}"
            update_job_status(job_id, "failed", account=account_name, error=final_error)
            self.signals.job_updated.emit(job_id, "failed", account_name, final_error)
            self.signals.log_msg.emit(
                f"[{label}] Prompt blocked by content filter. Marked as failed (no retry)."
            )
            return

        if self._is_session_drop_error(error_msg):
            self.signals.log_msg.emit(f"[{label}] Browser session dropped during job. Re-initializing next run.")
            await self._cleanup_slot_session(slot, reason="session drop")

        if "recaptcha" in (error_msg or "").lower():
            self.signals.log_msg.emit(
                f"[{label}] ReCAPTCHA issue. Cleaning session + reloading page on next retry..."
            )
            await self._cleanup_slot_session(slot, reason="recaptcha refresh")
            slot["force_flow_reload_on_next_init"] = True
            self._apply_account_recaptcha_cooldown(slot)
            self._record_recaptcha_health(slot, False, error_msg)

        if self._should_penalize_slot(error_msg):
            slot["consecutive_failures"] += 1
            if slot["consecutive_failures"] >= self.max_consecutive_slot_failures:
                slot["disabled_until"] = time.time() + self.slot_cooldown_seconds
                slot["cooldown_announced"] = True
                slot["consecutive_failures"] = 0
                self.signals.log_msg.emit(
                    f"[{label}] Slot disabled for {self.slot_cooldown_seconds}s after repeated failures."
                )
                await self._cleanup_slot_session(slot, reason="cooldown")
        else:
            slot["consecutive_failures"] = 0

        retry_count = self.job_retry_counts.get(job_id, 0)
        if max_retries_for_error > 0 and retryable:
            if retry_count < max_retries_for_error:
                retry_count += 1
                self.job_retry_counts[job_id] = retry_count
                retry_delay = self._get_retry_delay_seconds(error_msg, retry_count)
                if retry_delay > 0:
                    self.job_retry_after[job_id] = time.time() + retry_delay
                    retry_note = (
                        f"Auto-retry scheduled ({retry_count}/{max_retries_for_error}) "
                        f"after {retry_delay}s [{category}]: {error_msg}"
                    )
                else:
                    self.job_retry_after.pop(job_id, None)
                    retry_note = (
                        f"Auto-retry scheduled ({retry_count}/{max_retries_for_error}) "
                        f"immediately [{category}]: {error_msg}"
                    )
                update_job_status(job_id, "pending", account="", error=retry_note)
                self.signals.job_updated.emit(job_id, "pending", "", retry_note)
                self.signals.log_msg.emit(f"[{label}] {retry_note}")
                return

        self.job_retry_counts.pop(job_id, None)
        self.job_retry_after.pop(job_id, None)
        final_error = f"[{category}] {error_msg}"
        update_job_status(job_id, "failed", account=account_name, error=final_error)
        self.signals.job_updated.emit(job_id, "failed", account_name, final_error)
        if critical:
            self.signals.log_msg.emit(f"[{label}] Critical Job Crash [{category}]: {error_msg}")
        else:
            self.signals.log_msg.emit(f"[{label}] Job {job_id[:6]}... failed [{category}]: {error_msg}")

    async def run_bot_job(self, slot, job_dict, playwright_instance):
        engine = slot["engine"]
        account_name = slot["account_display"]
        label = slot["label"]
        job_id = job_dict["id"]

        try:
            def log_relay(msg):
                self.signals.log_msg.emit(msg)

            if slot["is_initialized"] and not await engine.is_session_alive():
                self.signals.log_msg.emit(f"[{label}] Previous browser session closed. Re-initializing...")
                await self._cleanup_slot_session(slot, reason="dead session check")

            if not slot["is_initialized"]:
                self.signals.log_msg.emit(f"[{label}] Initializing browser session...")
                try:
                    await engine.initialize(playwright_instance, log_relay)
                    slot["is_initialized"] = True
                except Exception as init_error:
                    if self._is_profile_lock_error(str(init_error)):
                        fallback_ok = await self._try_primary_slot_clone_fallback(
                            slot,
                            playwright_instance,
                            init_error,
                        )
                        if fallback_ok:
                            engine = slot["engine"]
                        else:
                            raise
                    else:
                        raise

            if slot.get("force_flow_reload_on_next_init"):
                self.signals.log_msg.emit(f"[{label}] Reloading Flow page for fresh reCAPTCHA context...")
                try:
                    await engine._goto_flow_page(wait_until="domcontentloaded")
                    await asyncio.sleep(2)
                except Exception as reload_err:
                    self.signals.log_msg.emit(f"[{label}] Page reload after reCAPTCHA error failed: {reload_err}")
                finally:
                    slot["force_flow_reload_on_next_init"] = False

            # Always isolate each queued job with a fresh tab.
            fresh_ok = await engine.open_fresh_tab(log_relay)
            if not fresh_ok:
                raise RuntimeError("Could not prepare a fresh browser tab.")

            if not slot.get("project_setup_done"):
                try:
                    setup_ok = await engine.setup_project_via_ui(log_relay)
                    slot["project_setup_done"] = True
                    if setup_ok:
                        self.signals.log_msg.emit(f"[{label}] Project setup complete. API mode ready.")
                    else:
                        self.signals.log_msg.emit(
                            f"[{label}] Project setup incomplete. First job may fail and retry until API context is ready."
                        )
                except Exception as setup_error:
                    slot["project_setup_done"] = True
                    self.signals.log_msg.emit(f"[{label}] Project setup failed: {setup_error}")

            job_payload = self._load_job_payload(job_id)
            if not job_payload:
                await self._handle_job_failure(slot, job_id, "Job payload not found in DB.")
                return

            success, error_msg = await engine.execute_job(job_payload, log_relay)

            if success:
                slot["consecutive_failures"] = 0
                self.account_recaptcha_streak.pop(slot["account_name"], None)
                self.account_hold_count.pop(slot["account_name"], None)  # Reset escalation on success
                self._record_throttle_success(slot["account_name"])  # Ramp up slots after 429 recovery
                self.account_restart_count[self._slot_restart_key(slot)] = 0
                slot["pending_browser_restart"] = False
                slot["is_restarting"] = False
                self._record_recaptcha_health(slot, True)
                self._unlock_additional_slots_after_success(slot)
                await self._maybe_trigger_account_auto_refresh(slot)
                self.job_retry_counts.pop(job_id, None)
                self.job_retry_after.pop(job_id, None)
                update_job_status(job_id, "completed", account=account_name)
                self.signals.job_updated.emit(job_id, "completed", account_name, "")
                self.signals.account_auth_status.emit(account_name, "logged_in", "Last success: just now")
                self.signals.log_msg.emit(f"[{label}] Job {job_id[:6]}... finished successfully!")
            else:
                await self._handle_job_failure(slot, job_id, error_msg or "Unknown generation error.")

        except Exception as e:
            await self._handle_job_failure(slot, job_id, str(e), critical=True)
        finally:
            if self.reinitialize_each_job:
                await self._cleanup_slot_session(slot, reason="per-job reset")
                if slot["is_initialized"] is False:
                    self.signals.log_msg.emit(f"[{label}] Session reset for next queued job.")

            slot["is_busy"] = False
            await self._maybe_run_account_restart(slot.get("account_name"), playwright_instance)
            await self._refresh_pending_slots_for_account(slot.get("account_name"))
            skip_cooldown = bool(slot.pop("skip_inter_job_cooldown", False))
            if not skip_cooldown:
                await asyncio.sleep(self.inter_job_cooldown_seconds)
