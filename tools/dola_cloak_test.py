"""
CloakBrowser HEADLESS test for dola.

dola detects normal Chrome headless. This tests whether the anti-detect
CloakBrowser passes dola's headless check. CloakBrowser does NOT persist a Google
login across sessions, so we solve that the way you described:

  1) take the Google (+dola) cookies from a dedicated profile that's ALREADY
     logged in (data/dola_profiles/<name>, set up via `dola_profiles.py login`),
  2) inject them into a fresh CloakBrowser session,
  3) log into dola + generate — all HEADLESS.

    python tools/dola_cloak_test.py --as acct1
    python tools/dola_cloak_test.py --as acct1 --visible     # watch it
    python tools/dola_cloak_test.py --as acct1 --proxy http://user:pass@host:port

Cookies are cached to data/dola_profiles/<name>_cookies.json so re-runs are fast
(and CloakBrowser never needs a fresh Gmail login).
"""
import argparse
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8"); sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from playwright.async_api import async_playwright
from src.core.dola_api import DolaSession, DailyLimitReached, GenerationRefused, DolaError
from src.core.cloakbrowser_support import load_cloakbrowser_api

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILES_DIR = os.path.join(ROOT, "data", "dola_profiles")
CHROME_EXES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]


def log(*a):
    try:
        print("[cloak]", *a, flush=True)
    except Exception:
        print("[cloak]", *[str(x).encode("ascii", "replace").decode() for x in a], flush=True)


def find_chrome():
    for c in CHROME_EXES:
        if os.path.isfile(c):
            return c
    return None


async def export_cookies(profile_dir):
    """Open the dedicated profile with real Chrome (off-screen) and export ALL its
    cookies (Google + dola) so we can inject them into CloakBrowser."""
    chrome = find_chrome()
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=profile_dir, executable_path=chrome, headless=False,
            ignore_default_args=["--enable-automation"],
            args=["--no-first-run", "--no-default-browser-check",
                  "--window-position=-32000,-32000", "--window-size=1200,800"])
        try:
            cookies = await asyncio.wait_for(ctx.cookies(), timeout=30)
            return cookies
        finally:
            try:
                await asyncio.wait_for(ctx.close(), timeout=15)
            except Exception:
                pass


async def cloak_run(cookies, prompt, ratio, proxy, headless):
    api = load_cloakbrowser_api()
    persistent = api.get("persistent_async")
    if not api.get("available") or persistent is None:
        log("CloakBrowser not available in this environment."); return 3
    session_path = tempfile.mkdtemp(prefix="dola_cloak_")
    log(f"launching CloakBrowser (headless={headless}) session={session_path}")
    ctx = await persistent(
        session_path,
        headless=headless,
        args=["--no-first-run", "--no-default-browser-check"],
        proxy=proxy or None,
        humanize={"preset": "careful"},
    )
    try:
        # inject the logged-in cookies
        try:
            await ctx.add_cookies(cookies)
            log(f"injected {len(cookies)} cookies into CloakBrowser")
        except Exception as e:
            log("cookie inject error:", str(e)[:120])
        pages = list(getattr(ctx, "pages", []) or [])
        page = pages[0] if pages else await ctx.new_page()
        session = DolaSession(ctx, page, logger=log)
        try:
            ipr = await ctx.request.get("https://api.ipify.org?format=json")
            log("egress IP:", (await ipr.text()).strip())
        except Exception:
            pass

        log("logging into dola (via injected Google cookies + trusted click if needed)...")
        if not await session.login_via_google(timeout=90):
            log("⛔ dola login failed in CloakBrowser"); return 4
        log("✅ logged in — capturing base params...")
        await session._ensure_base()
        log("✅ base params captured — submitting a video (this is the headless test)...")
        out = os.path.join(ROOT, "outputs", "cloak_test.mp4")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        await session.generate_one(prompt, out, ratio=ratio, timeout=720)
        n = os.path.getsize(out) if os.path.exists(out) else 0
        log(f"🎉 CloakBrowser HEADLESS WORKS — saved {n} bytes -> {out}")
        return 0
    except DailyLimitReached:
        log("⛔ daily limit (but login+submit WORKED in CloakBrowser headless — detection passed)"); return 5
    except GenerationRefused:
        log("🚫 prompt refused (login+submit WORKED — detection passed)"); return 6
    except DolaError as e:
        msg = str(e)
        if "no conversation_id" in msg:
            log("⛔ 'no conversation_id' — CloakBrowser headless STILL detected/throttled by dola.")
        else:
            log("dola error:", msg[:100])
        return 7
    finally:
        try:
            await ctx.close()
        except Exception:
            pass


async def main_async(args):
    profile_dir = os.path.join(PROFILES_DIR, args.as_name)
    if not os.path.isdir(profile_dir):
        log("dedicated profile not found:", profile_dir,
            "\n  set it up: python tools/dola_profiles.py login --as", args.as_name)
        return 2
    cookie_file = os.path.join(PROFILES_DIR, f"{args.as_name}_cookies.json")

    cookies = None
    if os.path.isfile(cookie_file) and not args.refresh:
        try:
            cookies = json.load(open(cookie_file, encoding="utf-8"))
            log(f"using cached cookies ({len(cookies)}) from {os.path.basename(cookie_file)} "
                f"(use --refresh to re-export)")
        except Exception:
            cookies = None
    if not cookies:
        log("exporting cookies from the dedicated profile (real Chrome)...")
        try:
            cookies = await export_cookies(profile_dir)
        except Exception as e:
            log("cookie export failed (is that profile open in another run? close it):", str(e)[:120])
            return 2
        try:
            json.dump(cookies, open(cookie_file, "w", encoding="utf-8"))
            log(f"cached {len(cookies)} cookies -> {os.path.basename(cookie_file)}")
        except Exception:
            pass

    from urllib.parse import urlparse
    proxy = None
    if args.proxy:
        u = urlparse(args.proxy)
        proxy = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
        if u.username: proxy["username"] = u.username
        if u.password: proxy["password"] = u.password

    return await cloak_run(cookies, args.prompt, args.ratio, proxy, headless=not args.visible)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--as", dest="as_name", required=True, help="dedicated profile to source cookies from (e.g. acct1)")
    ap.add_argument("--prompt", default="a calm ocean wave at golden sunset, cinematic")
    ap.add_argument("--ratio", default="16:9")
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--visible", action="store_true", help="show CloakBrowser (default: headless — that's the test)")
    ap.add_argument("--refresh", action="store_true", help="re-export cookies from the profile")
    args = ap.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
