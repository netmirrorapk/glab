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

from playwright.async_api import async_playwright
from src.core.dola_api import DolaSession, DailyLimitReached, GenerationRefused, DolaError

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
    kwargs = dict(
        user_data_dir=profile_dir, executable_path=chrome, headless=headless,
        ignore_default_args=["--enable-automation"],
        args=["--no-first-run", "--no-default-browser-check", "--disable-blink-features=AutomationControlled"],
        viewport={"width": 1280, "height": 800})
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


async def worker(acct, proxy, queue: asyncio.Queue, ratio, headless, stats):
    profile_dir = os.path.join(PROFILES_DIR, acct)
    if not os.path.isdir(profile_dir):
        log(acct, "profile missing — skipping"); return
    p = ctx = None
    recreated = 0
    try:
        p, ctx = await _launch(profile_dir, proxy, headless)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        session = DolaSession(ctx, page, logger=lambda *a: log(acct, *a))
        try:
            ipr = await ctx.request.get("https://api.ipify.org?format=json")
            log(acct, "IP", (await ipr.text()).strip())
        except Exception:
            pass
        if not await session.login_via_google(timeout=90):
            log(acct, "login failed — retiring account"); return
        await session._ensure_base()
        log(acct, "ready ✅")

        attempts = {}          # idx -> how many times this prompt failed transiently
        no_conv_streak = 0     # consecutive rate-limit hits → back off harder
        while True:
            try:
                idx, prompt = queue.get_nowait()
            except asyncio.QueueEmpty:
                log(acct, "queue empty — done"); return

            # ── STEP 0: ALWAYS make sure dola is logged in BEFORE submitting ──
            try:
                if not await session.is_logged_in():
                    log(acct, "not logged in → logging into dola first…")
                    if not await session.login_via_google(timeout=90):
                        log(acct, "login failed → retiring"); return
                    await session._ensure_base()
                    log(acct, "logged in ✅ — resuming")
            except Exception as e:
                log(acct, "login-check error:", str(e)[:80])

            out = os.path.join(OUT_DIR, f"{idx:04d}_{acct}.mp4")
            try:
                await session.generate_one(prompt, out, ratio=ratio, timeout=720)
                n = os.path.getsize(out) if os.path.exists(out) else 0
                log(acct, f"✅ #{idx} saved ({n} bytes)")
                stats["done"] += 1
                no_conv_streak = 0
                attempts.pop(idx, None)
                await asyncio.sleep(2)     # gentle stagger between jobs
            except DailyLimitReached:
                # EXHAUST → burn-recreate: delete → re-login (fresh quota) → resume,
                # and put THIS prompt back so it runs on the fresh account.
                await queue.put((idx, prompt))
                if recreated >= MAX_RECREATE:
                    log(acct, f"daily limit; recreate cap ({MAX_RECREATE}) reached — retiring"); return
                recreated += 1
                log(acct, f"⛔ daily limit → burn-recreate #{recreated}: delete → re-login → resume…")
                ok, detail = await session.delete_account(timeout=90, log=lambda *a: log(acct, *a))
                if not ok:
                    log(acct, "delete failed → retiring:", detail); return
                await asyncio.sleep(4)
                session._base = {}
                if not await session.login_via_google(timeout=90):
                    log(acct, "re-login failed → retiring"); return
                await session._ensure_base()
                log(acct, "✅ fresh account ready — resuming")
                no_conv_streak = 0
            except GenerationRefused:
                log(acct, f"🚫 #{idx} refused (prompt) — skipping")
                stats["refused"] += 1
            except DolaError as e:
                msg = str(e)
                attempts[idx] = attempts.get(idx, 0) + 1
                if attempts[idx] > 4:
                    log(acct, f"#{idx} failed {attempts[idx]}x ({msg[:50]}) — giving up on this prompt")
                    stats["errors"] += 1
                    continue
                await queue.put((idx, prompt))
                if "no conversation_id" in msg or "rate" in msg.lower():
                    # RATE-LIMIT (esp. multiple accounts on ONE IP). Do NOT hammer —
                    # back off progressively; give dola time to cool down.
                    no_conv_streak += 1
                    cooldown = min(15 + no_conv_streak * 15, 90)
                    log(acct, f"#{idx} rate-limited (no conv) — cooling down {cooldown}s "
                              f"(streak {no_conv_streak}). Tip: give each account its own proxy IP.")
                    await asyncio.sleep(cooldown)
                else:
                    log(acct, f"#{idx} transient: {msg[:70]} — retry in 8s")
                    await asyncio.sleep(8)
            finally:
                queue.task_done()
    except Exception as e:
        log(acct, "worker crashed:", str(e)[:120])
    finally:
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

    await asyncio.gather(*[
        worker(a, proxies.get(a), queue, args.ratio, args.headless, stats) for a in accounts
    ])
    print(f"[run] FINISHED — done={stats['done']} refused={stats['refused']} "
          f"errors={stats['errors']} | videos in {OUT_DIR}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompts", required=True, help="text file, one prompt per line")
    ap.add_argument("--accounts", default=None, help="comma list of profile names (default: all in registry)")
    ap.add_argument("--ratio", default="16:9")
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
