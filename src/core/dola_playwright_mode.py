"""
Playwright dola mode — dola.com (ByteDance Seedance) video generation WITHOUT the
Chrome extension. This mirrors DolaModeManager's queue integration but drives the
Playwright engine in src/core/dola_api.py (DolaSession) with the orchestration
ported from tools/dola_run.py:

  • one dedicated automation profile per account (data/dola_profiles/<acct>),
  • staggered per-account launch (5s gap), N parallel tabs/account,
  • invisible headless via CloakBrowser (or a real off-screen window),
  • full error handling (limit / refusal / images / logged-out) + burn-recreate
    (delete dola account → same-Gmail relogin → fresh quota) on daily-limit,
  • proactive burn-recreate when dola reports 0 points left after a success.

queue_manager.py calls PlaywrightDolaModeManager.run() when
generation_mode == "playwright_dola".

Accounts = the profile folders under data/dola_profiles/ (set up once via
`python tools/dola_profiles.py login --as <acct>`), listed in registry.json.
Optional per-account proxy in data/dola_profiles/proxies.json.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from typing import Any, Dict, List, Optional

from playwright.async_api import async_playwright

from src.db.db_manager import (
    get_accounts, get_all_jobs, get_output_directory, get_setting,
    update_job_runtime_state, update_job_status,
)
from src.core.dola_api import (
    DolaSession, DailyLimitReached, GenerationRefused,
    NotLoggedIn, GotImagesNotVideo, DolaError,
)
# Reuse the exact same resolvers / filename helpers / allowed-value sets as the
# extension dola mode so both modes behave identically and share the UI settings.
from src.core.dola_mode import (
    _resolve_dola_ratio, _resolve_dola_duration, _resolve_dola_model,
    _queue_output_number, _safe_filename,
    _ALLOWED_RATIO, _ALLOWED_MODELS,
)
from src.core.cloakbrowser_support import load_cloakbrowser_api

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CHROME_EXES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]
MAX_RECREATE = 6          # safety: max burn-recreate cycles per account before retiring it
LAUNCH_STAGGER = 5.0      # seconds between account launches
GEN_TIMEOUT = 720         # dola render can take up to ~8 min


def _find_chrome() -> Optional[str]:
    for c in CHROME_EXES:
        if os.path.isfile(c):
            return c
    return None


def _proxy_dict(url):
    if not url:
        return None
    from urllib.parse import urlparse
    u = urlparse(url)
    pd = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username:
        pd["username"] = u.username
    if u.password:
        pd["password"] = u.password
    return pd


# ── browser launch (ported from tools/dola_run.py) ─────────────────────────────
async def _launch(profile_dir, proxy, headless):
    """Real Chrome persistent context. dola detects every headless mode, so for
    'invisible' we launch a genuine HEADED window moved OFF-SCREEN."""
    chrome = _find_chrome()
    p = await async_playwright().start()
    extra = ["--no-first-run", "--no-default-browser-check", "--disable-blink-features=AutomationControlled",
             "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows",
             "--disable-background-timer-throttling"]
    if headless:
        extra += ["--window-position=-32000,-32000", "--window-size=1280,800"]
    kwargs = dict(user_data_dir=profile_dir, executable_path=chrome, headless=False,
                  ignore_default_args=["--enable-automation"], args=extra,
                  viewport=None if headless else {"width": 1280, "height": 800})
    pd = _proxy_dict(proxy)
    if pd:
        kwargs["proxy"] = pd
    ctx = await p.chromium.launch_persistent_context(**kwargs)
    try:
        from playwright_stealth import Stealth
        payload = Stealth().script_payload
        if callable(payload):
            payload = payload()
        if payload:
            await ctx.add_init_script(script=payload)
    except Exception:
        pass
    return p, ctx


async def _export_cookies(profile_dir):
    """Grab a dedicated profile's cookies (Google + dola) to seed CloakBrowser."""
    chrome = _find_chrome()
    p = await async_playwright().start()
    try:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=profile_dir, executable_path=chrome, headless=False,
            ignore_default_args=["--enable-automation"],
            args=["--no-first-run", "--no-default-browser-check",
                  "--window-position=-32000,-32000", "--window-size=1200,800"])
        try:
            return await asyncio.wait_for(ctx.cookies(), timeout=30)
        finally:
            try:
                await asyncio.wait_for(ctx.close(), timeout=15)
            except Exception:
                pass
    finally:
        try:
            await p.stop()
        except Exception:
            pass


def _cookie_cache(session_path):
    # cache the exported cookies inside the account's own session dir
    return os.path.join(session_path, "_dola_cloak_cookies.json")


async def _cookies_for(session_path):
    cf = _cookie_cache(session_path)
    if os.path.isfile(cf):
        try:
            return json.load(open(cf, encoding="utf-8"))
        except Exception:
            pass
    ck = await _export_cookies(session_path)
    try:
        json.dump(ck, open(cf, "w", encoding="utf-8"))
    except Exception:
        pass
    return ck


def _save_cookies_file(session_path, cookies):
    try:
        json.dump(cookies, open(_cookie_cache(session_path), "w", encoding="utf-8"))
    except Exception:
        pass


async def _save_cookies(session_path, ctx):
    try:
        _save_cookies_file(session_path, await ctx.cookies())
    except Exception:
        pass


async def _launch_cloak(cookies, proxy, headless):
    api = load_cloakbrowser_api()
    persistent = api.get("persistent_async")
    if not api.get("available") or persistent is None:
        raise RuntimeError("CloakBrowser not available")
    session_path = tempfile.mkdtemp(prefix="dola_cloak_")
    ctx = await persistent(
        session_path, headless=headless,
        args=["--no-first-run", "--no-default-browser-check"],
        proxy=proxy or None, humanize={"preset": "careful"})
    try:
        await ctx.add_cookies(cookies)
    except Exception:
        pass
    return None, ctx


def _job_ref(job) -> str:
    for k in ("start_image_path", "ref_path", "ref_paths", "image_path"):
        v = str(job.get(k) or "").strip()
        if v:
            return v
    return ""


class PlaywrightDolaModeManager:
    """Dola automation via Playwright dedicated profiles (no extension)."""

    def __init__(self, queue_manager):
        self.qm = queue_manager
        self._log = lambda m: queue_manager.signals.log_msg.emit(m)
        self._job_q: asyncio.Queue = asyncio.Queue()
        self._seen: set = set()                # job_ids currently enqueued / in-flight
        self._states: List[Dict[str, Any]] = []  # per-account state (completion monitor)
        self._alive = True
        self._ref_warned = False
        self._n_accounts = 0
        self._workers_done = 0
        # settings snapshot (loaded in run())
        self._model = "seedance_v2.0"
        self._ratio = "9:16"
        self._duration = 10
        self._slots = 1
        self._cloak = True
        self._headless = True
        self._auto_delete = True

    # ── lifecycle helpers ──────────────────────────────────────────────────────
    def _running(self) -> bool:
        return (self._alive
                and getattr(self.qm, "is_running", False)
                and not getattr(self.qm, "stop_requested", False)
                and not getattr(self.qm, "force_stop_requested", False))

    @staticmethod
    def _bool_setting(key, default="1") -> bool:
        return str(get_setting(key, default) or default).strip().lower() in ("1", "true", "on", "yes")

    def _enumerate_accounts(self) -> List[Dict[str, str]]:
        """The app's Account Manager accounts (db) with a valid Playwright session
        dir. Each account = {name, session_path, proxy}; session_path is the
        user-data-dir holding the Google/dola login (created by 'Login for dola')."""
        out: List[Dict[str, str]] = []
        try:
            for a in (get_accounts() or []):
                sp = str(a.get("session_path") or "").strip()
                if sp and os.path.isdir(sp):
                    out.append({"name": str(a.get("name") or sp),
                                "session_path": sp,
                                "proxy": str(a.get("proxy") or "").strip()})
        except Exception:
            pass
        return out

    def _out_path(self, job) -> str:
        out_dir = get_output_directory() or os.getcwd()
        os.makedirs(out_dir, exist_ok=True)
        qno = _queue_output_number(job)
        fname = f"{qno}.mp4" if qno is not None else _safe_filename(job["id"], 1)
        if not fname.lower().endswith(".mp4"):
            fname += ".mp4"
        return os.path.join(out_dir, fname)

    def _settle(self, job_id):
        """Terminal state reached (completed/failed) — allow the feeder to forget it."""
        self._seen.discard(job_id)

    async def _requeue(self, job):
        """Return a job to the queue (DB pending) WITHOUT letting the feeder re-add
        it (it stays in _seen and back in our internal queue)."""
        try:
            update_job_status(job["id"], "pending", account="")
        except Exception:
            pass
        await self._job_q.put(job)

    # ── main entry ─────────────────────────────────────────────────────────────
    async def run(self) -> None:
        self._log("[DolaPW] Starting Playwright dola mode (dedicated profiles, no extension)…")
        self._model = _resolve_dola_model(get_setting("dola_model", "seedance_v2.0"))
        self._ratio = _resolve_dola_ratio(get_setting("dola_ratio", "9:16"))
        self._duration = _resolve_dola_duration(get_setting("dola_duration", "10"))
        try:
            self._slots = max(1, min(8, int(str(get_setting("slots_per_account", "1") or "1"))))
        except Exception:
            self._slots = 1
        # Honor the app's "Browser & Stealth" settings so the visible choices drive
        # this mode too: Browser Mode = CloakBrowser → cloak; Cloak Display = Headless
        # → invisible. Real-Chrome modes run OFF-SCREEN when 'headless'.
        bmode = str(get_setting("browser_mode", "cloakbrowser") or "cloakbrowser").strip().lower()
        cdisp = str(get_setting("cloak_display", "headless") or "headless").strip().lower()
        self._cloak = (bmode == "cloakbrowser")
        self._headless = (cdisp == "headless") if self._cloak else (bmode == "headless")
        self._auto_delete = self._bool_setting("dola_auto_delete", "1")
        # If cloak is requested but the CloakBrowser binary isn't available, fall
        # back to real Chrome (off-screen when headless) so the mode still runs.
        if self._cloak:
            try:
                if not (load_cloakbrowser_api() or {}).get("available"):
                    self._cloak = False
                    self._log("[DolaPW] CloakBrowser not available → using real Chrome (off-screen).")
            except Exception:
                self._cloak = False

        accounts = self._enumerate_accounts()
        if not accounts:
            self._log("[DolaPW] No accounts found in Account Manager.\n"
                      "  Add one:  Account Manager → 'Login for dola (Google)'.")
            return
        self._n_accounts = len(accounts)
        self._log(f"[DolaPW] accounts={[a['name'] for a in accounts]} | cloak={self._cloak} "
                  f"headless={self._headless} | tabs/account={self._slots} | model={self._model} "
                  f"ratio={self._ratio} duration={self._duration}s | auto_delete={self._auto_delete}")

        tasks = [asyncio.create_task(self._feeder()),
                 asyncio.create_task(self._monitor())]
        for i, acct in enumerate(accounts):
            tasks.append(asyncio.create_task(
                self._account_worker(acct, launch_delay=i * LAUNCH_STAGGER)))
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._alive = False
        self._log("[DolaPW] dola mode finished.")

    # ── feeder: DB pending jobs → internal queue ───────────────────────────────
    async def _feeder(self) -> None:
        while self._running():
            try:
                jobs = get_all_jobs() or []
            except Exception:
                jobs = []
            for j in jobs:
                if (j.get("status") == "pending"
                        and str(j.get("job_type") or "").lower() == "video"
                        and j["id"] not in self._seen):
                    self._seen.add(j["id"])
                    await self._job_q.put(j)
            await asyncio.sleep(max(1, getattr(self.qm, "scheduler_poll_seconds", 2)))

    # ── completion monitor ─────────────────────────────────────────────────────
    async def _monitor(self) -> None:
        poll = max(1, getattr(self.qm, "scheduler_poll_seconds", 2))
        await asyncio.sleep(poll)
        stable = 0
        while self._running():
            await asyncio.sleep(poll)
            # every account worker exited (all logins failed / crashed) → can't proceed
            if self._workers_done >= self._n_accounts and self._n_accounts > 0:
                self._log("[DolaPW] All account workers ended — stopping "
                          "(remaining jobs left pending).")
                self._alive = False
                return
            try:
                jobs = get_all_jobs() or []
            except Exception:
                jobs = None
            if jobs is None:
                continue
            pend = any(j.get("status") == "pending" and str(j.get("job_type") or "").lower() == "video" for j in jobs)
            run = any(j.get("status") == "running" and str(j.get("job_type") or "").lower() == "video" for j in jobs)
            inflight = self._job_q.qsize() + sum(s.get("busy", 0) for s in self._states)
            if not pend and not run and inflight == 0:
                stable += 1
                if stable >= 2:
                    self._log("[DolaPW] All dola jobs completed. Stopping mode.")
                    self._alive = False
                    return
            else:
                stable = 0

    # ── per-account worker ─────────────────────────────────────────────────────
    async def _account_worker(self, acct, launch_delay=0.0) -> None:
        if launch_delay:
            await asyncio.sleep(launch_delay)
        if not self._running():
            self._workers_done += 1
            return
        name = acct["name"]
        session_path = acct["session_path"]
        proxy = acct.get("proxy") or ""
        alog = lambda *a: self._log(f"[DolaPW][{name}] " + " ".join(str(x) for x in a))
        p = ctx = None
        try:
            alog(f"opening account ({'CloakBrowser' if self._cloak else 'Chrome'})…")
            if self._cloak:
                cookies = await _cookies_for(session_path)
                p, ctx = await _launch_cloak(cookies, _proxy_dict(proxy), self._headless)
            else:
                p, ctx = await _launch(session_path, proxy, self._headless)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            main_session = DolaSession(ctx, page, logger=alog)

            alog("checking dola login…")
            if not await main_session.login_via_google(timeout=90):
                if self._cloak:
                    alog("login failed — refreshing cookies from the account session & retrying…")
                    try:
                        fresh = await _export_cookies(session_path)
                        await ctx.add_cookies(fresh)
                        _save_cookies_file(session_path, fresh)
                    except Exception as e:
                        alog("cookie refresh failed:", str(e)[:80])
                if not await main_session.login_via_google(timeout=90):
                    alog("login failed — retiring account (re-login it in Account Manager → "
                         "'Login for dola (Google)')")
                    return
            await main_session._ensure_base()
            if self._cloak:
                await _save_cookies(session_path, ctx)

            # open `slots` parallel tabs (each tab = 1 concurrent generation)
            sessions = [main_session]
            for _ in range(self._slots - 1):
                pg = await ctx.new_page()
                try:
                    await pg.goto("https://www.dola.com/chat/create-video", wait_until="domcontentloaded")
                except Exception:
                    pass
                sessions.append(DolaSession(ctx, pg, logger=alog))

            state = {"healthy": asyncio.Event(), "busy": 0, "recreated": 0,
                     "lock": asyncio.Lock(), "alive": True, "cloak": self._cloak,
                     "ctx": ctx, "acct": name, "session_path": session_path}
            state["healthy"].set()
            self._states.append(state)
            alog(f"logged in ✅ — {len(sessions)} tab(s) generating")
            await asyncio.gather(*[
                self._tab_loop(name, i, s, main_session, state)
                for i, s in enumerate(sessions)])
        except Exception as e:
            alog("worker crashed:", str(e)[:120])
        finally:
            self._workers_done += 1
            try:
                if ctx:
                    await ctx.close()
            except Exception:
                pass
            try:
                if p:
                    await p.stop()
            except Exception:
                pass

    # ── per-tab generation loop ────────────────────────────────────────────────
    async def _tab_loop(self, acct, tab_i, session, main_session, state) -> None:
        tag = f"{acct}#t{tab_i + 1}"
        try:
            await session._ensure_base()
        except Exception:
            pass
        attempts: Dict[str, int] = {}
        limit_hits: Dict[str, int] = {}
        while self._running() and state["alive"]:
            await state["healthy"].wait()
            if not (self._running() and state["alive"]):
                return
            if getattr(self.qm, "pause_requested", False):
                await asyncio.sleep(1)
                continue
            try:
                job = await asyncio.wait_for(self._job_q.get(), timeout=3)
            except asyncio.TimeoutError:
                if not self._alive:
                    return
                continue
            job_id = job["id"]
            try:
                prompt = str(job.get("video_prompt") or job.get("prompt") or "").strip()
                if not prompt:
                    update_job_status(job_id, "failed", account=acct, error="empty_prompt")
                    self.qm.signals.job_updated.emit(job_id, "failed", acct, "empty_prompt")
                    self._settle(job_id)
                    continue
                if _job_ref(job) and not self._ref_warned:
                    self._ref_warned = True
                    self._log("[DolaPW] Note: reference-image→video is not supported in "
                              "Playwright mode yet — generating text-only for ref jobs.")
                ratio = job.get("video_ratio") if job.get("video_ratio") in _ALLOWED_RATIO else self._ratio
                model = job.get("video_model") if job.get("video_model") in _ALLOWED_MODELS else self._model

                # login-check before every submit (guest/deleted → real login)
                try:
                    if not await session.logged_in_for_real():
                        alog = lambda *a: self._log(f"[DolaPW][{tag}] " + " ".join(str(x) for x in a))
                        alog("not logged in/guest → login…")
                        if await session.login_via_google(timeout=90):
                            await session._ensure_base()
                except Exception:
                    pass

                out_path = self._out_path(job)
                update_job_status(job_id, "running", account=acct)
                self.qm.signals.job_updated.emit(job_id, "running", acct, "")
                state["busy"] += 1
                try:
                    await session.generate_one(prompt, out_path, model=model, ratio=ratio,
                                               duration=self._duration, timeout=GEN_TIMEOUT)
                    state["busy"] -= 1
                    update_job_runtime_state(job_id, output_path=out_path)
                    update_job_status(job_id, "completed", account=acct)
                    self.qm.signals.job_updated.emit(job_id, "completed", acct, "")
                    self._log(f"[DolaPW][{tag}] ✅ saved {os.path.basename(out_path)}")
                    attempts.pop(job_id, None)
                    limit_hits.pop(job_id, None)
                    self._settle(job_id)
                    if self._auto_delete and session.last_points_left == 0:
                        self._log(f"[DolaPW][{tag}] dola reports 0 points left → burn-recreate")
                        await self._safe_recreate(acct, main_session, state)
                except DailyLimitReached:
                    state["busy"] -= 1
                    limit_hits[job_id] = limit_hits.get(job_id, 0) + 1
                    if limit_hits[job_id] > 2:
                        self._log(f"[DolaPW][{tag}] job signalled 'exhausted' "
                                  f"{limit_hits[job_id]}x across fresh accounts — poison, failing")
                        update_job_status(job_id, "failed", account=acct, error="no_video_queued")
                        self.qm.signals.job_updated.emit(job_id, "failed", acct, "no_video_queued")
                        self._settle(job_id)
                    else:
                        await self._requeue(job)
                        self.qm.signals.job_updated.emit(job_id, "pending", "", "daily_limit_requeued")
                        self._log(f"[DolaPW][{tag}] daily limit → burn-recreate this account")
                        if self._auto_delete:
                            await self._safe_recreate(acct, main_session, state)
                except GenerationRefused:
                    state["busy"] -= 1
                    update_job_status(job_id, "failed", account=acct, error="prompt_refused")
                    self.qm.signals.job_updated.emit(job_id, "failed", acct, "prompt_refused")
                    self._log(f"[DolaPW][{tag}] 🚫 refused (prompt) — skipping")
                    self._settle(job_id)
                except GotImagesNotVideo:
                    state["busy"] -= 1
                    update_job_status(job_id, "failed", account=acct, error="got_images_not_video")
                    self.qm.signals.job_updated.emit(job_id, "failed", acct, "got_images_not_video")
                    self._log(f"[DolaPW][{tag}] 🚫 produced images not video — skipping (account OK)")
                    self._settle(job_id)
                except NotLoggedIn:
                    state["busy"] -= 1
                    attempts[job_id] = attempts.get(job_id, 0) + 1
                    if attempts[job_id] > 4:
                        update_job_status(job_id, "failed", account=acct, error="not_logged_in")
                        self.qm.signals.job_updated.emit(job_id, "failed", acct, "not_logged_in")
                        self._settle(job_id)
                    else:
                        self._log(f"[DolaPW][{tag}] session dropped (guest) → re-login + requeue")
                        try:
                            if await session.login_via_google(timeout=90):
                                await session._ensure_base()
                        except Exception:
                            pass
                        await self._requeue(job)
                        self.qm.signals.job_updated.emit(job_id, "pending", "", "not_logged_in_requeued")
                except DolaError as e:
                    state["busy"] -= 1
                    attempts[job_id] = attempts.get(job_id, 0) + 1
                    if attempts[job_id] > 4:
                        update_job_status(job_id, "failed", account=acct, error=str(e)[:100])
                        self.qm.signals.job_updated.emit(job_id, "failed", acct, str(e)[:100])
                        self._log(f"[DolaPW][{tag}] failed {attempts[job_id]}x ({str(e)[:45]}) — giving up")
                        self._settle(job_id)
                    else:
                        await self._requeue(job)
                        self.qm.signals.job_updated.emit(job_id, "pending", "", "error_requeued")
                        await asyncio.sleep(6)
            finally:
                self._job_q.task_done()

    # ── coordinated burn-recreate (delete dola account → relogin → fresh quota) ─
    async def _safe_recreate(self, acct, main_session, state) -> None:
        """Wrapper so a burn-recreate crash retires ONE account cleanly instead of
        propagating out of the tab loop and killing the whole worker."""
        try:
            await self._safe_recreate(acct, main_session, state)
        except Exception as e:
            self._log(f"[DolaPW][{acct}] recreate crashed ({str(e)[:60]}) → retiring account")
            state["alive"] = False
            try:
                state["healthy"].set()
            except Exception:
                pass

    async def _account_recreate(self, acct, main_session, state) -> None:
        seen = state["recreated"]
        alog = lambda *a: self._log(f"[DolaPW][{acct}] " + " ".join(str(x) for x in a))
        async with state["lock"]:
            if state["recreated"] != seen:      # another tab already recreated
                return
            if not state["alive"]:
                return
            if state["recreated"] >= MAX_RECREATE:
                alog(f"recreate cap ({MAX_RECREATE}) reached — retiring account")
                state["alive"] = False
                state["healthy"].set()
                return
            state["healthy"].clear()            # pause every tab of this account
            alog("⛔ burn-recreate: waiting for other tabs' gens to finish…")
            for _ in range(150):                # up to ~5 min for in-flight gens
                if state["busy"] <= 0 or not self._running():
                    break
                await asyncio.sleep(2)
            alog(f"burn-recreate #{state['recreated'] + 1}: delete → re-login…")
            try:
                ok, detail = await main_session.delete_account(timeout=90, log=alog)
            except Exception as e:
                ok, detail = False, str(e)[:80]
            if not ok:
                alog("delete failed → retiring:", detail)
                state["alive"] = False
                state["healthy"].set()
                return
            # Re-login on the fresh account. Wrap EVERYTHING so a browser crash under
            # load ("Execution context was destroyed" / "Target ... closed") retires
            # this ONE account cleanly instead of throwing out of the tab loop and
            # killing the whole worker (which loses the account + spams errors).
            try:
                await asyncio.sleep(3)
                main_session._base = {}
                if not await main_session.login_via_google(timeout=90):
                    alog("re-login failed → retiring")
                    state["alive"] = False
                    state["healthy"].set()
                    return
                await main_session._ensure_base()
                for _ in range(5):              # settle + confirm the fresh session
                    await asyncio.sleep(2)
                    try:
                        if await main_session.logged_in_for_real():
                            break
                    except Exception:
                        pass
                state["recreated"] += 1
                if state.get("cloak") and state.get("ctx") and state.get("session_path"):
                    await _save_cookies(state["session_path"], state["ctx"])
                alog("✅ fresh account ready — resuming all tabs")
                state["healthy"].set()
            except Exception as e:
                alog(f"re-login crashed ({str(e)[:60]}) → retiring account")
                state["alive"] = False
                state["healthy"].set()
                return
