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
from src.core.dola_api import DolaSession, DailyLimitReached, GenerationRefused, DolaError
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


async def _launch_cloak(cookies, proxy, headless):
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
    try:
        await ctx.add_cookies(cookies)
    except Exception:
        pass
    return None, ctx


async def _account_recreate(acct, main_session, state):
    """Coordinated burn-recreate for ALL tabs of an account: pause every tab, wait
    for in-flight generations to finish, delete + re-login on the main tab (fresh
    quota), then resume. Only ONE tab actually performs it (the lock); others that
    also hit the limit just return once the account is healthy again."""
    async with state["lock"]:
        if state["healthy"].is_set():
            # already recovered by whoever got here first
            if state["alive"]:
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
        log(acct, "✅ fresh account ready — resuming all tabs")
        state["healthy"].set()                   # resume every tab


async def _tab_loop(acct, tab_i, session, main_session, queue, ratio, stats, state):
    tag = f"{acct}#t{tab_i+1}"
    try:
        await session._ensure_base()
    except Exception:
        pass
    attempts = {}
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
            await session.generate_one(prompt, out, ratio=ratio, timeout=720)
            state["busy"] -= 1
            n = os.path.getsize(out) if os.path.exists(out) else 0
            log(tag, f"✅ #{idx} saved ({n} bytes)")
            stats["done"] += 1
            attempts.pop(idx, None)
        except DailyLimitReached:
            state["busy"] -= 1
            await queue.put((idx, prompt))       # re-run on the fresh account
            log(tag, f"#{idx} daily limit → burn-recreate this account")
            await _account_recreate(acct, main_session, state)
        except GenerationRefused:
            state["busy"] -= 1
            log(tag, f"🚫 #{idx} refused (prompt) — skipping")
            stats["refused"] += 1
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
                 cloak=False, parallel=1, launch_delay=0.0):
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
            p, ctx = await _launch_cloak(cookies, cproxy, headless)
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
            log(acct, "login failed — retiring account"); return
        await main_session._ensure_base()
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
                 "lock": asyncio.Lock(), "alive": True}
        state["healthy"].set()
        log(acct, f"{len(sessions)} tab(s) ready — generating")
        await asyncio.gather(*[
            _tab_loop(acct, i, s, main_session, queue, ratio, stats, state)
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
    print(f"[run] parallel tabs/account={parallel}, launch stagger={stagger}s between accounts")
    await asyncio.gather(*[
        worker(a, proxies.get(a), queue, args.ratio, args.headless, stats,
               cloak=getattr(args, "cloak", False), parallel=parallel,
               launch_delay=i * stagger)                     # staggered: 0s, 5s, 10s, …
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
    ap.add_argument("--stagger", type=float, default=5.0, help="seconds between opening each account's profile (default 5)")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
