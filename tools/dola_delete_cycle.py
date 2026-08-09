"""
Phase-1.5 — burn-recreate cycle proof (Playwright).

On a dedicated automation profile: login → DELETE the dola account → RE-LOGIN the
same Google account (dola recreates a fresh account) → generate ONE video.
Proves the whole auto-cycle works headless, exactly like the extension did.

    python tools/dola_delete_cycle.py --profile acct1
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8"); sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from playwright.async_api import async_playwright
from src.core.dola_api import DolaSession, DailyLimitReached, GenerationRefused, DolaError

PROFILES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "dola_profiles")
CHROME_EXES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]


def log(*a):
    try:
        print("[cycle]", *a, flush=True)
    except Exception:
        print("[cycle]", *[str(x).encode("ascii", "replace").decode() for x in a], flush=True)


def find_chrome():
    for c in CHROME_EXES:
        if os.path.isfile(c):
            return c
    return None


async def run(profile, prompt, ratio, out, headless):
    dest = profile if os.path.isabs(profile) else os.path.join(PROFILES_DIR, profile)
    if not os.path.isdir(dest):
        log("ERROR: dedicated profile not found:", dest); return 2
    chrome = find_chrome()

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=dest, executable_path=chrome, headless=headless,
            ignore_default_args=["--enable-automation"],
            args=["--no-first-run", "--no-default-browser-check", "--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 800})
        try:
            from playwright_stealth import Stealth
            payload = Stealth().script_payload
            if callable(payload): payload = payload()
            if payload: await ctx.add_init_script(script=payload)
        except Exception:
            pass
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        session = DolaSession(ctx, page, logger=log)
        try:
            ipr = await ctx.request.get("https://api.ipify.org?format=json")
            log("egress IP:", (await ipr.text()))
        except Exception:
            pass

        try:
            # STEP 1 — login
            log("STEP 1: login to dola...")
            if not await session.login_via_google(timeout=90):
                log("  ⛔ initial login failed"); return 4
            log("  ✅ logged in")

            # STEP 2 — DELETE the dola account
            log("STEP 2: DELETING dola account...")
            ok, detail = await session.delete_account(timeout=90, log=log)
            log(f"  delete result: ok={ok} detail={detail}")
            if not ok:
                log("  ⛔ delete did not confirm"); return 5

            # STEP 3 — RE-LOGIN (same Google → dola recreates fresh account)
            log("STEP 3: RE-LOGIN same Google account (fresh dola account)...")
            await asyncio.sleep(4)
            session._base = {}  # fresh session → recapture base params
            if not await session.login_via_google(timeout=90):
                log("  ⛔ re-login failed"); return 6
            log("  ✅ re-logged in (fresh account)")
            await session._ensure_base()
            log("  ✅ base params captured")

            # STEP 4 — generate ONE video on the fresh account
            log(f"STEP 4: generating a video on the fresh account...")
            os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
            await session.generate_one(prompt, out, ratio=ratio, timeout=720)
            size = os.path.getsize(out) if os.path.exists(out) else 0
            log(f"  ✅ DONE — burn-recreate + generate WORKS. Saved {size} bytes -> {out}")
            return 0
        except DailyLimitReached:
            log("  ⛔ daily limit (fresh account had no quota?)"); return 7
        except GenerationRefused:
            log("  🚫 prompt refused"); return 8
        except DolaError as e:
            log("  dola error:", e); return 9
        finally:
            await asyncio.sleep(1)
            try: await ctx.close()
            except Exception: pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True, help="dedicated profile name (e.g. acct1)")
    ap.add_argument("--prompt", default="a calm mountain lake at dawn, cinematic")
    ap.add_argument("--ratio", default="16:9")
    ap.add_argument("--out", default=os.path.join("outputs", "pw_cycle.mp4"))
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()
    sys.exit(asyncio.run(run(args.profile, args.prompt, args.ratio, args.out, args.headless)))


if __name__ == "__main__":
    main()
