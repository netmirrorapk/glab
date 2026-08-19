"""
Phase 2 — multi-account parallel dola runner (Playwright, no extension).

Runs a queue of prompts across MANY dedicated automation profiles in parallel.
Each account = its own dedicated profile (data/dola_profiles/<name>) + optional
per-account proxy (its own IP → no shared-IP rate-limit). Full behaviour ported
from the extension: daily_limit → burn-recreate (delete + re-login → fresh quota),
content-refusal → skip that prompt, ratio enforcement, verdict classifier.

Setup once (per account):
    python tools/dola_profiles.py login --as acct1     # Google login (one-time)
    python tools/dola_profiles.py login --as acct2
    # optional proxies: data/dola_profiles/proxies.json  {"acct1":"http://user:pass@host:port", ...}

Run:
    python tools/dola_run.py --prompts prompts.txt
    python tools/dola_run.py --prompts prompts.txt --accounts acct1,acct2 --ratio 16:9 --headless

Videos are saved to outputs/dola_run/<index>_<account>.mp4
"""
import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8"); sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import json as _json
import tempfile
from playwright.async_api import async_playwright
from src.core.dola_api import (DolaSession, DailyLimitReached, GenerationRefused,
                               NotLoggedIn, GotImagesNotVideo, DolaError)
from src.core.cloakbrowser_support import load_cloakbrowser_api

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILES_DIR = os.path.join(ROOT, "data", "dola_profiles")
REGISTRY = os.path.join(PROFILES_DIR, "registry.json")
PROXIES = os.path.join(PROFILES_DIR, "proxies.json")
OUT_DIR = os.path.join(ROOT, "outputs", "dola_run")
CHROME_EXES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]
MAX_RECREATE = 6   # safety: max burn-recreate cycles per account before retiring it


def find_chrome():
    for c in CHROME_EXES:
        if os.path.isfile(c):
            return c
    return None


def log(acct, *a):
    tag = f"[{acct}]"
    try:
        print(tag, *a, flush=True)
    except Exception:
        print(tag, *[str(x).encode("ascii", "replace").decode() for x in a], flush=True)


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


async def _launch(profile_dir, proxy, headless):
    chrome = find_chrome()
    p = await async_playwright().start()
    # dola (ByteDance) DETECTS every headless mode (old AND new) and throttles it
    # ("no conversation_id"). Only a REAL visible window passes. So for "invisible"
    # we launch a genuine HEADED window but move it OFF-SCREEN — dola sees a real
    # headed browser, you just don't see the window. Keep it non-backgrounded so
    # the off-screen tab keeps rendering.
    extra = ["--no-first-run", "--no-default-browser-check", "--disable-blink-features=AutomationControlled",
             "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows",
             "--disable-background-timer-throttling"]
    if headless:
        extra += ["--window-position=-32000,-32000", "--window-size=1280,800"]
    kwargs = dict(
        user_data_dir=profile_dir, executable_path=chrome, headless=False,
        ignore_default_args=["--enable-automation"],
        args=extra,
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
    """Grab a dedicated profile's cookies (Google + dola) so CloakBrowser — which
    doesn't persist a Google login — can be seeded with a logged-in session."""
    chrome = find_chrome()
    async with async_playwright() as p:
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


async def _cookies_for(acct, profile_dir):
    """Cached cookies (data/dola_profiles/<acct>_cookies.json) or a fresh export."""
    cf = os.path.join(PROFILES_DIR, f"{acct}_cookies.json")
    if os.path.isfile(cf):
        try:
            return _json.load(open(cf, encoding="utf-8"))
        except Exception:
            pass
    ck = await _export_cookies(profile_dir)
    try:
        _json.dump(ck, open(cf, "w", encoding="utf-8"))
    except Exception:
        pass
    return ck


async def _save_cookies(acct, ctx):
    """Refresh the cached cookies with the CURRENT live session (Google + the
    FRESH dola account after a login/burn-recreate) so the next run never seeds a
    stale/burned account. Google cookies are stable; only the dola ones rotate."""
    try:
        ck = await ctx.cookies()
        _json.dump(ck, open(os.path.join(PROFILES_DIR, f"{acct}_cookies.json"), "w", encoding="utf-8"))
    except Exception:
        pass


async def _add_cookies_robust(ctx, cookies, log=None):
    """Inject cookies ONE-BY-ONE with sanitisation so a single malformed cookie can't make
    the whole add_cookies batch throw and leave the session logged out. Returns (ok, total,
    google_ok)."""
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
                cc2 = {k: cc[k] for k in cc if k not in ("domain", "path")}
                cc2["url"] = "https://" + (dom.lstrip(".") or "www.dola.com") + "/"
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
    """Launch anti-detect CloakBrowser (passes dola's headless check) + inject the
    logged-in cookies. Returns (None, ctx) — no separate playwright object."""
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


async def _account_recreate(acct, main_session, state):
    """Coordinated burn-recreate for ALL tabs of an account: pause every tab, wait
    for in-flight generations to finish, delete + re-login on the main tab (fresh
    quota), then resume. Only ONE tab actually performs it (the lock); others that
    also hit the limit just return once the account is healthy again."""
    seen = state["recreated"]                    # snapshot BEFORE we queue on the lock
    async with state["lock"]:
        # If another tab completed a burn-recreate while we waited for the lock, the
        # account is already fresh → nothing to do. NOTE: healthy.is_set() alone is
        # NOT a valid guard (it's set during normal operation too, so the FIRST tab
        # to hit the limit would wrongly bail and no burn would ever happen — that
        # was a real bug). The recreated counter reliably tells us who's first.
        if state["recreated"] != seen:
            return
        if not state["alive"]:
            return
        if state["recreated"] >= MAX_RECREATE:
            log(acct, f"recreate cap ({MAX_RECREATE}) reached — retiring account")
            state["alive"] = False
            state["healthy"].set()
            return
        state["healthy"].clear()                 # pause every tab
        log(acct, "⛔ daily limit → burn-recreate: waiting for other tabs' gens to finish…")
        for _ in range(150):                     # up to ~5 min for in-flight gens
            if state["busy"] <= 0:
                break
            await asyncio.sleep(2)
        state["recreated"] += 1
        log(acct, f"burn-recreate #{state['recreated']}: delete → re-login…")
        try:
            ok, detail = await main_session.delete_account(timeout=90, log=lambda *a: log(acct, *a))
        except Exception as e:
            ok, detail = False, str(e)[:80]
        if not ok:
            log(acct, "delete failed → retiring:", detail)
            state["alive"] = False
            state["healthy"].set()
            return
        await asyncio.sleep(4)
        main_session._base = {}
        if not await main_session.login_via_google(timeout=90):
            log(acct, "re-login failed → retiring")
            state["alive"] = False
            state["healthy"].set()
            return
        await main_session._ensure_base()
        # Let the fresh session fully settle + CONFIRM it's really logged in before
        # resuming, so the first submit doesn't briefly see the guest state (which
        # would needlessly trip the re-login + requeue path).
        for _ in range(5):                       # up to ~10s
            await asyncio.sleep(2)
            try:
                if await main_session.logged_in_for_real():
                    break
            except Exception:
                pass
        if state.get("cloak") and state.get("ctx"):
            await _save_cookies(state.get("acct", acct), state["ctx"])   # cache fresh account
        log(acct, "✅ fresh account ready — resuming all tabs")
        state["healthy"].set()                   # resume every tab


async def _tab_loop(acct, tab_i, session, main_session, queue, ratio, stats, state,
                    use_skill=False, duration=10, tab_stagger=0.0):
    tag = f"{acct}#t{tab_i+1}"
    # stagger each tab's first submit so N tabs don't burst N sends at once
    if tab_i and tab_stagger:
        await asyncio.sleep(tab_i * tab_stagger)
    try:
        await session._ensure_base()
    except Exception:
        pass
    attempts = {}
    limit_hits = {}   # per-prompt "exhausted" count — guards against a poison prompt
    while state["alive"]:
        await state["healthy"].wait()            # block while the account recreates
        if not state["alive"]:
            return
        try:
            idx, prompt = queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        # login-check BEFORE every submit (guest/deleted → real login)
        try:
            if not await session.logged_in_for_real():
                log(tag, "not logged in/guest → login…")
                if await session.login_via_google(timeout=90):
                    await session._ensure_base()
        except Exception:
            pass
        out = os.path.join(OUT_DIR, f"{idx:04d}_{acct}.mp4")
        state["busy"] += 1
        try:
            await session.generate_one(prompt, out, ratio=ratio, duration=duration,
                                       timeout=720, use_skill=use_skill,
                                       claimed_vids=state.get("claimed_vids"))
            state["busy"] -= 1
            n = os.path.getsize(out) if os.path.exists(out) else 0
            log(tag, f"✅ #{idx} saved ({n} bytes)")
            stats["done"] += 1
            attempts.pop(idx, None)
            limit_hits.pop(idx, None)
            # dola told us EXACTLY how many points remain — 0 means the NEXT submit
            # will hit the limit. Burn-recreate NOW (authoritative signal) instead of
            # wasting a submit to discover it. The counter guard makes this idempotent.
            if session.last_points_left == 0:
                log(tag, f"#{idx} done but dola reports 0 points left → burn-recreate now")
                await _account_recreate(acct, main_session, state)
        except DailyLimitReached:
            state["busy"] -= 1
            # A REAL exhausted account, once burn-recreated, generates this prompt
            # fine — so a given prompt should trigger "exhausted" at most a couple of
            # times total. If the SAME prompt keeps signalling exhausted across
            # multiple FRESH accounts, it isn't a quota problem — it's a poison
            # prompt (dola silently returns no video for it). Skip it instead of
            # burning account after account on it.
            limit_hits[idx] = limit_hits.get(idx, 0) + 1
            if limit_hits[idx] > 2:
                log(tag, f"🚫 #{idx} signalled 'exhausted' {limit_hits[idx]}x across fresh "
                         f"accounts — poison prompt, skipping (not burning)")
                stats["refused"] += 1
            else:
                await queue.put((idx, prompt))   # re-run on the fresh account
                log(tag, f"#{idx} daily limit → burn-recreate this account")
                await _account_recreate(acct, main_session, state)
        except GenerationRefused:
            state["busy"] -= 1
            log(tag, f"🚫 #{idx} refused (prompt) — skipping")
            stats["refused"] += 1
        except GotImagesNotVideo:
            # dola made images, not a video — the account is FINE, only this job
            # failed. Skip it (do NOT burn-recreate a healthy account).
            state["busy"] -= 1
            log(tag, f"🚫 #{idx} produced images not video — skipping (account OK)")
            stats["refused"] += 1
        except NotLoggedIn:
            # Session dropped to guest mid-gen — re-login (NOT burn) and requeue.
            state["busy"] -= 1
            attempts[idx] = attempts.get(idx, 0) + 1
            if attempts[idx] > 4:
                log(tag, f"#{idx} still logged-out after {attempts[idx]} tries — giving up")
                stats["errors"] += 1
            else:
                log(tag, f"#{idx} session dropped (guest) → re-login + requeue")
                try:
                    if await session.login_via_google(timeout=90):
                        await session._ensure_base()
                except Exception:
                    pass
                await queue.put((idx, prompt))
        except DolaError as e:
            state["busy"] -= 1
            attempts[idx] = attempts.get(idx, 0) + 1
            if attempts[idx] > 4:
                log(tag, f"#{idx} failed {attempts[idx]}x ({str(e)[:45]}) — giving up")
                stats["errors"] += 1
            else:
                await queue.put((idx, prompt))
                await asyncio.sleep(8)
        finally:
            queue.task_done()


async def worker(acct, proxy, queue: asyncio.Queue, ratio, headless, stats,
                 cloak=False, parallel=1, launch_delay=0.0,
                 use_skill=False, duration=10, tab_stagger=0.0):
    if launch_delay:
        await asyncio.sleep(launch_delay)        # staggered launch (Ns gap between accounts)
    profile_dir = os.path.join(PROFILES_DIR, acct)
    if not os.path.isdir(profile_dir):
        log(acct, "profile missing — skipping"); return
    p = ctx = None
    try:
        log(acct, f"opening profile ({'CloakBrowser' if cloak else 'Chrome'})…")
        if cloak:
            cookies = await _cookies_for(acct, profile_dir)
            cproxy = _proxy_dict(proxy) if isinstance(proxy, str) else proxy
            p, ctx = await _launch_cloak(cookies, cproxy, headless, log=lambda *a: log(acct, *a))
        else:
            p, ctx = await _launch(profile_dir, proxy, headless)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        main_session = DolaSession(ctx, page, logger=lambda *a: log(acct, *a))
        try:
            ipr = await ctx.request.get("https://api.ipify.org?format=json")
            log(acct, "IP", (await ipr.text()).strip())
        except Exception:
            pass
        # login-check → login if needed
        log(acct, "checking dola login…")
        if not await main_session.login_via_google(timeout=90):
            # In cloak mode the session is seeded from a CACHED cookie file that can
            # go stale (Google rotates cookies). Re-export the LIVE cookies from the
            # dedicated profile (which holds the real Google login) and retry once.
            if cloak:
                log(acct, "login failed — refreshing cookies from the dedicated profile & retrying…")
                try:
                    fresh = await _export_cookies(profile_dir)
                    await _add_cookies_robust(ctx, fresh, log=lambda *a: log(acct, *a))
                    _json.dump(fresh, open(os.path.join(PROFILES_DIR, f"{acct}_cookies.json"), "w", encoding="utf-8"))
                    log(acct, f"refreshed {len(fresh)} cookies")
                except Exception as e:
                    log(acct, "cookie refresh failed:", str(e)[:80])
            if not await main_session.login_via_google(timeout=90):
                log(acct, "login failed — retiring account "
                          "(dedicated profile's Google may be logged out → re-run "
                          f"`python tools/dola_profiles.py login --as {acct}`)"); return
        await main_session._ensure_base()
        if cloak:
            await _save_cookies(acct, ctx)     # cache the FRESH session (not the stale seed)
        # open `parallel` tabs (concurrent generations per account); tab 0 = main
        nslots = max(1, int(parallel or 1))
        log(acct, f"logged in ✅ — opening {nslots} tab(s) for parallel generation…")
        sessions = [main_session]
        for _ in range(nslots - 1):
            pg = await ctx.new_page()
            try:
                await pg.goto("https://www.dola.com/chat/create-video", wait_until="domcontentloaded")
            except Exception:
                pass
            sessions.append(DolaSession(ctx, pg, logger=lambda *a: log(acct, *a)))
        state = {"healthy": asyncio.Event(), "busy": 0, "recreated": 0,
                 "lock": asyncio.Lock(), "alive": True,
                 "cloak": cloak, "ctx": ctx, "acct": acct,
                 "claimed_vids": set()}   # hard duplicate guard (shared across tabs)
        state["healthy"].set()
        log(acct, f"{len(sessions)} tab(s) ready — generating")
        await asyncio.gather(*[
            _tab_loop(acct, i, s, main_session, queue, ratio, stats, state,
                      use_skill=use_skill, duration=duration, tab_stagger=tab_stagger)
            for i, s in enumerate(sessions)])
        log(acct, "all tabs done")
    except Exception as e:
        log(acct, "worker crashed:", str(e)[:120])
    finally:
        # closes tabs + profile for THIS account (only after its tabs finished).
        # If the whole app/process is killed, Playwright tears every context down too.
        try:
            if ctx: await ctx.close()
        except Exception:
            pass
        try:
            if p: await p.stop()
        except Exception:
            pass


async def main_async(args):
    os.makedirs(OUT_DIR, exist_ok=True)
    prompts = [l.strip() for l in open(args.prompts, encoding="utf-8").read().splitlines() if l.strip()]
    if not prompts:
        print("no prompts"); return
    # accounts
    if args.accounts:
        accounts = [a.strip() for a in args.accounts.split(",") if a.strip()]
    else:
        reg = json.load(open(REGISTRY, encoding="utf-8")) if os.path.isfile(REGISTRY) else {}
        accounts = list(reg.keys())
    if not accounts:
        print("no accounts — set up with tools/dola_profiles.py login --as <name>"); return
    proxies = json.load(open(PROXIES, encoding="utf-8")) if os.path.isfile(PROXIES) else {}

    queue = asyncio.Queue()
    for i, pr in enumerate(prompts, 1):
        queue.put_nowait((i, pr))
    stats = {"done": 0, "refused": 0, "errors": 0}
    print(f"[run] {len(prompts)} prompts across {len(accounts)} account(s): {accounts}")
    print(f"[run] proxies: {list(proxies.keys()) or '(none — all on this machine IP)'}")

    if getattr(args, "cloak", False):
        print("[run] CloakBrowser mode — truly invisible headless (passes dola's detection)")
    stagger = float(getattr(args, "stagger", 5.0) or 0)
    parallel = int(getattr(args, "parallel", 1) or 1)
    use_skill = bool(getattr(args, "skill", False))
    duration = int(getattr(args, "duration", 10) or 10)
    tab_stagger = float(getattr(args, "tab_stagger", 2.0) or 0)
    print(f"[run] parallel tabs/account={parallel}, launch stagger={stagger}s between accounts")
    print(f"[run] route={'SKILL /creative-video' if use_skill else 'DIRECT ability'}, "
          f"duration={duration}s, tab_stagger={tab_stagger}s")
    await asyncio.gather(*[
        worker(a, proxies.get(a), queue, args.ratio, args.headless, stats,
               cloak=getattr(args, "cloak", False), parallel=parallel,
               launch_delay=i * stagger,                     # staggered: 0s, 5s, 10s, …
               use_skill=use_skill, duration=duration, tab_stagger=tab_stagger)
        for i, a in enumerate(accounts)
    ])
    print(f"[run] FINISHED — done={stats['done']} refused={stats['refused']} "
          f"errors={stats['errors']} | videos in {OUT_DIR}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompts", required=True, help="text file, one prompt per line")
    ap.add_argument("--accounts", default=None, help="comma list of profile names (default: all in registry)")
    ap.add_argument("--ratio", default="16:9")
    ap.add_argument("--headless", action="store_true", help="invisible: off-screen headed (regular Chrome) — dola passes this")
    ap.add_argument("--cloak", action="store_true", help="use anti-detect CloakBrowser in TRUE headless (truly invisible; cookies auto-injected from the dedicated profile)")
    ap.add_argument("--parallel", type=int, default=1, help="parallel tabs (concurrent generations) PER account")
    ap.add_argument("--skill", action="store_true", help="use the creative-video SKILL route (/creative-video + auto-yes) instead of the direct ability route")
    ap.add_argument("--duration", type=int, default=10, help="video duration in seconds (default 10)")
    ap.add_argument("--tab-stagger", type=float, default=2.0, dest="tab_stagger", help="seconds between each tab's first submit (avoids high-demand burst; default 2)")
    ap.add_argument("--stagger", type=float, default=5.0, help="seconds between opening each account's profile (default 5)")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
