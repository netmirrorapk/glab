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
    NotLoggedIn, GotImagesNotVideo, HighDemand, DolaError, DOLA_ORIGIN,
)
# Reuse the exact same resolvers / filename helpers / allowed-value sets as the
# extension dola mode so both modes behave identically and share the UI settings.
from src.core.dola_mode import (
    _resolve_dola_ratio, _resolve_dola_duration, _resolve_dola_model,
    _queue_output_number, _safe_filename,
    _ALLOWED_RATIO, _ALLOWED_MODELS,
)
from src.core.cloakbrowser_support import load_cloakbrowser_api
from src.core.ffmpeg_path import ffmpeg_exe, ffprobe_exe

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


async def _add_cookies_robust(ctx, cookies, log=None):
    """Inject cookies into a context ONE-BY-ONE with sanitisation, so a single malformed
    cookie (bad sameSite, a __Host-/__Secure- prefix with a domain, a partitionKey field,
    etc.) can't make the WHOLE add_cookies batch throw and leave the session logged out
    (the old `try: add_cookies(all) except: pass` dropped every cookie on one bad one).
    Returns (ok, total, google_ok) — google_ok = how many *.google.com cookies landed
    (those are what the delete-page OAuth re-auth needs)."""
    ok = google_ok = 0
    total = len(cookies or [])
    for c in (cookies or []):
        try:
            cc = {k: c[k] for k in c if k not in
                  ("partitionKey", "priority", "sameParty", "sourceScheme", "sourcePort", "size")}
            ss = str(cc.get("sameSite", "")).lower()
            cc["sameSite"] = {"lax": "Lax", "strict": "Strict", "none": "None",
                              "no_restriction": "None"}.get(ss, "Lax")
            name = str(cc.get("name", ""))
            if name.startswith("__Host-"):
                cc.pop("domain", None); cc["path"] = "/"; cc["secure"] = True
            elif name.startswith("__Secure-"):
                cc["secure"] = True
            dom = str(c.get("domain", "") or "")
            try:
                await ctx.add_cookies([cc])
            except Exception:
                # retry via url= (some engines reject a leading-dot domain)
                cc2 = {k: cc[k] for k in cc if k not in ("domain", "path")}
                host = dom.lstrip(".") or "www.dola.com"
                cc2["url"] = "https://" + host + "/"
                await ctx.add_cookies([cc2])
            ok += 1
            if "google.com" in dom:
                google_ok += 1
        except Exception:
            pass
    if log:
        log(f"cookies injected: {ok}/{total} (google={google_ok})")
    return ok, total, google_ok


async def _launch_cloak(cookies, proxy, headless, log=None):
    api = load_cloakbrowser_api()
    persistent = api.get("persistent_async")
    if not api.get("available") or persistent is None:
        raise RuntimeError("CloakBrowser not available")
    session_path = tempfile.mkdtemp(prefix="dola_cloak_")
    ctx = await persistent(
        session_path, headless=headless,
        args=["--no-first-run", "--no-default-browser-check"],
        proxy=proxy or None, humanize={"preset": "careful"})
    await _add_cookies_robust(ctx, cookies, log=log)
    return None, ctx


def _job_ref(job) -> str:
    for k in ("start_image_path", "ref_path", "ref_paths", "image_path"):
        v = str(job.get(k) or "").strip()
        if v:
            return v
    return ""


# Watermark box as FRACTIONS of the frame (measured on dola's 1280x720 output:
# the "Dola AI" logo sits bottom-right). Fractions → scales to any 16:9 size.
_WM = {"x": 0.867, "y": 0.910, "w": 0.123, "h": 0.075}


async def _dewatermark(path) -> bool:
    """Remove dola's bottom-right 'Dola AI' watermark IN-PLACE via ffmpeg delogo.
    Runs as an async subprocess so it doesn't block the event loop. Graceful no-op
    if ffmpeg/ffprobe aren't available or anything fails (original is left intact)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            ffprobe_exe(), "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
        w, h = (int(v) for v in out.decode().strip().split("x")[:2])
    except Exception:
        return False
    x = max(1, min(int(_WM["x"] * w), w - 3))
    y = max(1, min(int(_WM["y"] * h), h - 3))
    bw = max(2, min(int(_WM["w"] * w), w - x - 1))
    bh = max(2, min(int(_WM["h"] * h), h - y - 1))
    tmp = path + ".nw.mp4"
    try:
        proc = await asyncio.create_subprocess_exec(
            ffmpeg_exe(), "-y", "-i", path,
            "-vf", f"delogo=x={x}:y={y}:w={bw}:h={bh}",
            "-preset", "veryfast", "-c:a", "copy", tmp,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        rc = await proc.wait()
        if rc == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 50000:
            os.replace(tmp, path)
            return True
    except Exception:
        pass
    try:
        if os.path.isfile(tmp):
            os.remove(tmp)
    except Exception:
        pass
    return False


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
        self._remove_wm = True
        self._wm_warned = False

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
        # honor the Account Manager "Use for generation" ticks. Setting holds the
        # enabled account names joined by '||'; unset (None) = use ALL (backward-compat).
        sel = get_setting("dola_selected_accounts", None)
        enabled = None
        if sel is not None:
            enabled = set(x for x in str(sel).split("||") if x)
        out: List[Dict[str, str]] = []
        try:
            for a in (get_accounts() or []):
                name = str(a.get("name") or "")
                sp = str(a.get("session_path") or "").strip()
                if enabled is not None and name not in enabled:
                    continue                      # unticked in Account Manager → skip
                if sp and os.path.isdir(sp):
                    out.append({"name": name or sp,
                                "session_path": sp,
                                "proxy": str(a.get("proxy") or "").strip()})
        except Exception:
            pass
        # "how many accounts to use" cap (Account Manager). 0/unset = use all that
        # passed the Use-tick filter above; otherwise use only the first N of them.
        try:
            maxn = int(str(get_setting("dola_max_accounts", "0") or "0"))
        except Exception:
            maxn = 0
        if maxn > 0 and len(out) > maxn:
            self._log(f"[DolaPW] using {maxn} of {len(out)} eligible account(s) "
                      f"(dola_max_accounts={maxn})")
            out = out[:maxn]
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
            self._slots = max(1, min(16, int(str(get_setting("slots_per_account", "1") or "1"))))
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
        self._remove_wm = self._bool_setting("dola_remove_watermark", "1")
        # Seconds to stagger each tab's FIRST submit so N tabs on one account don't fire
        # N sends in one burst (the #1 trigger for dola's "high demand" throttle). With
        # 10 tabs @ 2s that spreads the first round over ~18s. 0 = all at once.
        try:
            self._tab_stagger = max(0.0, float(str(get_setting("dola_tab_stagger", "2") or "2")))
        except Exception:
            self._tab_stagger = 2.0
        # Toggle: route video gen through the newer creative-video SKILL (agent rewrites
        # the prompt cinematically, auto-confirmed) instead of the direct ability route.
        # OFF by default — the ability route is the proven fast one-shot path.
        self._use_skill = self._bool_setting("dola_use_skill_flow", "0")
        # Toggle (skill route only): take each video's length from the prompt itself
        # (e.g. 'Prompt 1 (12s)' -> 12s, '8 sec' -> 8s), clamped to dola's 15s max,
        # instead of the fixed Dur dropdown. OFF = use the Dur setting for every job.
        self._prompt_duration = self._bool_setting("dola_prompt_duration", "0")
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
                  f"ratio={self._ratio} duration={self._duration}s | auto_delete={self._auto_delete} "
                  f"| skill_flow={self._use_skill} prompt_duration={self._prompt_duration} "
                  f"tab_stagger={self._tab_stagger}s")

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
                p, ctx = await _launch_cloak(cookies, _proxy_dict(proxy), self._headless, log=alog)
            else:
                p, ctx = await _launch(session_path, proxy, self._headless)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            main_session = DolaSession(ctx, page, logger=alog)

            alog("checking dola login…")
            if not await main_session.login_via_google(timeout=90):
                if self._cloak:
                    alog("login failed — re-exporting FRESH cookies from the dedicated profile & retrying…")
                    try:
                        fresh = await _export_cookies(session_path)
                        await _add_cookies_robust(ctx, fresh, log=alog)
                        _save_cookies_file(session_path, fresh)
                    except Exception as e:
                        alog("cookie refresh failed:", str(e)[:80])
                if not await main_session.login_via_google(timeout=90):
                    alog("login failed — retiring account (re-login it in Account Manager → "
                         "'Login for dola (Google)')")
                    return
            # login_via_google only checks cookies, so a stale-cookie GUEST passes it —
            # but then dola fires no signed requests and _ensure_base can't capture the
            # base params (it would crash the worker). Verify a REAL login first and
            # retire cleanly with a clear message if the Google session is actually dead.
            try:
                if not await main_session.logged_in_for_real():
                    alog("account is GUEST / Google session dead (cookies present but page "
                         "shows guest) — retiring; re-login it in Account Manager → "
                         "'Login for dola (Google)'")
                    return
            except Exception:
                pass
            try:
                await main_session._ensure_base()
            except Exception as e:
                # one more try — cloak sessions sometimes need a beat before dola fires
                # its signed XHRs (esp. with a partial cookie export, low google=N).
                alog(f"session init slow ({str(e)[:50]}) — retrying after a nudge…")
                try:
                    await main_session.page.goto(f"{DOLA_ORIGIN}/chat/create-video",
                                                 wait_until="domcontentloaded")
                    await asyncio.sleep(3)
                    await main_session._ensure_base()
                except Exception as e2:
                    alog(f"could not initialise dola session ({str(e2)[:60]}). The account IS "
                         "logged in but the CloakBrowser cookie export looks incomplete "
                         "(low google cookie count) — re-login it via 'Login for dola (Google)', "
                         "or switch Browser Mode to real Chrome for this run. Retiring.")
                    return
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
                     "ctx": ctx, "acct": name, "session_path": session_path,
                     # shared across this account's tabs — hard duplicate guard so no two
                     # prompts ever save the same vid.
                     "claimed_vids": set()}
            state["healthy"].set()
            self._states.append(state)
            # Report send-throttle status up front so the user sees which accounts
            # are rate-limited and exactly how long until they recover.
            try:
                rl = await main_session.check_rate_limit()
                if rl.get("is_limit"):
                    alog(f"⏳ RATE-LIMITED — recovers in ~{rl['seconds']}s "
                         f"({rl.get('limit_tips', '')[:50]})")
                else:
                    alog("send-rate: OK (not limited)")
            except Exception:
                pass
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
        # Spread the tabs' first submits so N tabs don't burst N sends at once
        # (avoids tripping dola's "high demand" send-throttle).
        if tab_i and getattr(self, "_tab_stagger", 0):
            await asyncio.sleep(tab_i * self._tab_stagger)
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
                # reference-image → video: upload the local image via dola's page
                # uploader and attach it (forces the creative-video skill route). Only
                # local files work; a URL/missing file falls back to text-only.
                ref_path = _job_ref(job)
                if ref_path and not os.path.isfile(ref_path):
                    if not self._ref_warned:
                        self._ref_warned = True
                        self._log(f"[DolaPW] Note: reference image not a local file ({ref_path[:60]}) "
                                  "— generating text-only for that job.")
                    ref_path = ""
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
                                               duration=self._duration, timeout=GEN_TIMEOUT,
                                               use_skill=self._use_skill, ref_image=(ref_path or None),
                                               prompt_duration=self._prompt_duration,
                                               claimed_vids=state["claimed_vids"])
                    state["busy"] -= 1
                    # auto-remove the "Dola AI" watermark (in-place, async, ~<1s) — but
                    # SKIP it when we already downloaded the raw UNWATERMARKED HD master
                    # (media/get_play_info main_url): it's clean AND high-quality, so
                    # running delogo would only waste time / soften a clean video.
                    if self._remove_wm and not getattr(session, "last_was_hd", False):
                        ok_wm = await _dewatermark(out_path)
                        if not ok_wm and not self._wm_warned:
                            self._wm_warned = True
                            self._log("[DolaPW] Note: watermark not removed (ffmpeg not found "
                                      "on PATH?) — videos are saved WITH the watermark.")
                    update_job_runtime_state(job_id, output_path=out_path)
                    update_job_status(job_id, "completed", account=acct)
                    self.qm.signals.job_updated.emit(job_id, "completed", acct, "")
                    self._log(f"[DolaPW][{tag}] ✅ saved {os.path.basename(out_path)}")
                    attempts.pop(job_id, None)
                    limit_hits.pop(job_id, None)
                    state["hd_streak"] = 0      # a success clears the high-demand backoff
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
                except HighDemand:
                    # Transient 'servers busy / high demand' — NOT exhaustion. Ask dola
                    # EXACTLY how long this account is throttled (send_rate_limit →
                    # limit_time) and wait that long, then retry the SAME prompt on the
                    # SAME account. Do NOT burn (the whole service is busy, not the
                    # account). Fall back to a rising backoff if dola gives no time.
                    state["busy"] -= 1
                    info = {}
                    try:
                        info = await session.check_rate_limit()
                    except Exception:
                        info = {}
                    secs = int(info.get("seconds") or 0)
                    if secs > 0:
                        wait = min(secs + 5, 900)   # +5s cushion, cap 15 min
                        tip = (info.get("limit_tips") or "").strip()
                        src = f"dola says {secs}s" + (f" — {tip[:50]}" if tip else "")
                    else:
                        hd = state.setdefault("hd_streak", 0) + 1
                        state["hd_streak"] = hd
                        wait = min(15 + hd * 15, 120)   # 30s,45s,… capped 120s
                        src = f"backoff {wait}s (no exact time from dola)"
                    self._log(f"[DolaPW][{tag}] ⏳ high demand / rate-limited → wait ({src}) "
                              f"+ retry same account (no burn)")
                    try:
                        if not await session.logged_in_for_real():
                            if await session.login_via_google(timeout=90):
                                await session._ensure_base()
                    except Exception:
                        pass
                    await self._requeue(job)
                    self.qm.signals.job_updated.emit(job_id, "pending", "", "high_demand_retry")
                    await asyncio.sleep(wait)
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
                except Exception as e:
                    # ANY other error (e.g. a Playwright network 'socket hang up' during
                    # download, a CDP blip) must NOT propagate — it would crash the whole
                    # worker and take down all this account's tabs. Treat it like a transient
                    # DolaError: requeue + retry the SAME job, give up only after repeats.
                    state["busy"] -= 1
                    attempts[job_id] = attempts.get(job_id, 0) + 1
                    if attempts[job_id] > 4:
                        update_job_status(job_id, "failed", account=acct, error=str(e)[:100])
                        self.qm.signals.job_updated.emit(job_id, "failed", acct, str(e)[:100])
                        self._log(f"[DolaPW][{tag}] failed {attempts[job_id]}x ({str(e)[:60]}) — giving up")
                        self._settle(job_id)
                    else:
                        self._log(f"[DolaPW][{tag}] transient error ({str(e)[:60]}) → requeue + retry")
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
            await self._account_recreate(acct, main_session, state)
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
            # In cloak mode the injected Google cookies may have rotated during the run;
            # the /delete-account OAuth re-auth needs a FRESH Google session, so re-export
            # + robustly inject fresh cookies from the dedicated profile right before the
            # delete (this is exactly what made the delete succeed in dola_delete_test).
            if state.get("cloak") and state.get("ctx") and state.get("session_path"):
                try:
                    fresh = await _export_cookies(state["session_path"])
                    await _add_cookies_robust(state["ctx"], fresh, log=alog)
                    _save_cookies_file(state["session_path"], fresh)
                except Exception as e:
                    alog("pre-delete cookie refresh failed:", str(e)[:60])
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
