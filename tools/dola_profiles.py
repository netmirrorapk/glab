"""
Dola automation — dedicated-profile manager.

Each dola automation account lives in its OWN Playwright-owned profile under
  data/dola_profiles/<name>/
so it is independent of your daily Chrome (daily Chrome can stay open) and its
cookies are NOT subject to the copy/App-Bound-Encryption problem.

You can populate an automation profile two ways:

  1) IMPORT the Google login from one of your REAL Chrome profiles (no re-typing
     the password). Because modern Chrome App-Bound-encrypts cookies on disk, we
     can't just copy files — instead we open the REAL profile once (Chrome must be
     closed), export its live Google session (storage_state), and inject it into
     the dedicated profile. One-time; after that the dedicated profile is stable.

         python tools/dola_profiles.py list
         python tools/dola_profiles.py import --from "Profile 18" --as acct1

  2) Fresh DIRECT login (you log into Google yourself in the opened window):

         python tools/dola_profiles.py login --as acct2

Duplicate guard: every automation profile records which Gmail it holds. If you try
to import/login a Gmail that's ALREADY in another automation profile, it refuses:
"already logged in as X in profile Y — use another gmail/profile".

List what you've set up:
         python tools/dola_profiles.py registry
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import glob

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8"); sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from playwright.async_api import async_playwright

CHROME_USER_DATA = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser(r"~\AppData\Local")),
    "Google", "Chrome", "User Data")
CHROME_EXES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]
PROFILES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "dola_profiles")
REGISTRY = os.path.join(PROFILES_DIR, "registry.json")
GOOGLE_LOGIN_COOKIES = {"SAPISID", "__Secure-1PSID", "__Secure-3PSID", "SSID", "SID", "HSID"}


def log(*a):
    print(*a, flush=True)


def find_chrome():
    for c in CHROME_EXES:
        if os.path.isfile(c):
            return c
    return None


def chrome_running():
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq chrome.exe"],
                             capture_output=True, text=True).stdout.lower()
        return "chrome.exe" in out
    except Exception:
        return False


def real_profile_gmail(profile_dir_name):
    pref = os.path.join(CHROME_USER_DATA, profile_dir_name, "Preferences")
    if not os.path.isfile(pref):
        return None
    try:
        d = json.load(open(pref, encoding="utf-8"))
        ai = d.get("account_info") or []
        for a in ai:
            if a.get("email"):
                return a["email"].lower()
    except Exception:
        pass
    return None


def load_registry():
    try:
        return json.load(open(REGISTRY, encoding="utf-8"))
    except Exception:
        return {}


def save_registry(reg):
    os.makedirs(PROFILES_DIR, exist_ok=True)
    json.dump(reg, open(REGISTRY, "w", encoding="utf-8"), indent=2)


def gmail_already_used(reg, gmail, exclude=None):
    gmail = (gmail or "").lower()
    for name, info in reg.items():
        if name == exclude:
            continue
        if (info.get("gmail") or "").lower() == gmail and gmail:
            return name
    return None


def cmd_list(_args):
    log("Real Chrome profiles + their signed-in Gmail:\n")
    rows = []
    for p in sorted(glob.glob(os.path.join(CHROME_USER_DATA, "Profile *"))) + [os.path.join(CHROME_USER_DATA, "Default")]:
        if not os.path.isdir(p):
            continue
        name = os.path.basename(p)
        g = real_profile_gmail(name)
        if g:
            rows.append((name, g))
    for name, g in rows:
        log(f"  {name:12}  {g}")
    log(f"\n{len(rows)} profiles with a Gmail. Import one with:")
    log('  python tools/dola_profiles.py import --from "Profile 18" --as acct1')


def cmd_registry(_args):
    reg = load_registry()
    if not reg:
        log("No automation profiles set up yet.")
        return
    log("Automation profiles (data/dola_profiles/):\n")
    for name, info in reg.items():
        log(f"  {name:12}  gmail={info.get('gmail')}  from={info.get('imported_from','(direct login)')}")


_FAST_ARGS = [
    "--no-first-run", "--no-default-browser-check",
    "--disable-blink-features=AutomationControlled",
    "--disable-extensions", "--disable-background-networking",
    "--disable-sync", "--disable-component-update", "--no-service-autorun",
    "--disable-features=Translate,OptimizationHints",
]


async def _export_google_state(from_profile):
    """Open the REAL profile (Chrome must be closed) and export its live cookies.
    Uses ctx.cookies() (light) — NOT storage_state() (which can hang on a big
    real profile). Everything is wrapped in timeouts so it can't hang forever."""
    chrome = find_chrome()
    # Real Chrome profiles (extensions/state) often HANG or crash under headless.
    # Launch a genuine HEADED window moved OFF-SCREEN instead — reliable, invisible.
    args = [f"--profile-directory={from_profile}",
            "--window-position=-32000,-32000", "--window-size=1200,800",
            "--disable-session-crashed-bubble", "--hide-crash-restore-bubble",
            "--disable-features=Translate,OptimizationHints,MediaRouter"] + _FAST_ARGS
    async with async_playwright() as p:
        log("  launching real profile (headed, off-screen; big profiles are slow)…")
        try:
            ctx = await asyncio.wait_for(
                p.chromium.launch_persistent_context(
                    user_data_dir=CHROME_USER_DATA, executable_path=chrome,
                    headless=False, ignore_default_args=["--enable-automation"], args=args),
                timeout=75)
        except asyncio.TimeoutError:
            log("  ⛔ launch timed out. Chrome 136+ BLOCKS automation (DevTools/CDP) on your")
            log("     REAL Chrome profile ('DevTools remote debugging requires a non-default")
            log("     data directory') — so import can't read its cookies. This is a Google")
            log("     security change, not fixable here.")
            log("     → Use direct login instead:  python tools/dola_profiles.py login --as <name>")
            log("       (or the app's '➕ Add Dola Account' button). One sign-in per Gmail.")
            return False, None, None
        try:
            log("  reading cookies…")
            cookies = await asyncio.wait_for(ctx.cookies(), timeout=30)   # ALL cookies (light)
            gnames = {c["name"] for c in cookies if "google" in (c.get("domain") or "")}
            gmail = real_profile_gmail(from_profile)
            logged = bool(gnames & GOOGLE_LOGIN_COOKIES)
            log(f"  got {len(cookies)} cookies; google-login={logged}")
            return logged, gmail, {"cookies": cookies}
        finally:
            try:
                await asyncio.wait_for(ctx.close(), timeout=20)
            except Exception:
                pass


async def _inject_into_dedicated(name, state):
    """Create/refresh the dedicated automation profile with the exported cookies."""
    chrome = find_chrome()
    dest = os.path.join(PROFILES_DIR, name)
    os.makedirs(dest, exist_ok=True)
    async with async_playwright() as p:
        log("  opening dedicated profile + injecting cookies…")
        ctx = await asyncio.wait_for(
            p.chromium.launch_persistent_context(
                user_data_dir=dest, executable_path=chrome, headless=False,
                ignore_default_args=["--enable-automation"],
                args=["--window-position=-32000,-32000", "--window-size=1200,800"] + _FAST_ARGS),
            timeout=90)
        try:
            await ctx.add_cookies(state.get("cookies", []))
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            try:
                await asyncio.wait_for(
                    page.goto("https://myaccount.google.com/", wait_until="domcontentloaded"),
                    timeout=30)
                await asyncio.sleep(3)
            except Exception:
                pass
            gck = await ctx.cookies("https://accounts.google.com")
            return bool({c["name"] for c in gck} & GOOGLE_LOGIN_COOKIES)
        finally:
            try:
                await asyncio.wait_for(ctx.close(), timeout=20)
            except Exception:
                pass


def cmd_import(args):
    if chrome_running():
        log("⚠️  Chrome is running. The REAL profile is locked while Chrome is open.")
        log("    Close ALL Chrome windows (and background chrome.exe), then re-run this import.")
        return
    reg = load_registry()
    gmail = real_profile_gmail(args.from_profile)
    if not gmail:
        log(f"Could not read a Gmail from '{args.from_profile}'. Is that profile signed into Google?")
        return
    dup = gmail_already_used(reg, gmail, exclude=args.as_name)
    if dup:
        log(f"⛔ {gmail} is ALREADY logged in automation profile '{dup}'. "
            f"Use another Gmail / another profile.")
        return
    log(f"importing Google login: {gmail}  ({args.from_profile} → {args.as_name})")
    logged, gmail2, state = asyncio.run(_export_google_state(args.from_profile))
    if not logged:
        log(f"⛔ '{args.from_profile}' has no active Google session to import "
            f"(Google signed out there). Sign it into Google first, or use `login`.")
        return
    ok = asyncio.run(_inject_into_dedicated(args.as_name, state))
    if not ok:
        log("⛔ import failed — Google session did not carry over. Try `login --as` for a fresh login.")
        return
    reg[args.as_name] = {"gmail": gmail, "imported_from": args.from_profile}
    save_registry(reg)
    log(f"✅ imported. Automation profile '{args.as_name}' now holds {gmail}.")
    log(f"   Generate with: python tools/dola_playwright_test.py --profile "
        f"\"{os.path.join(PROFILES_DIR, args.as_name)}\" --dedicated")


def _dedicated_gmail(dest):
    """Read the Gmail that got signed into a dedicated profile (dest/Default)."""
    pref = os.path.join(dest, "Default", "Preferences")
    if not os.path.isfile(pref):
        return None
    try:
        d = json.load(open(pref, encoding="utf-8"))
        for a in (d.get("account_info") or []):
            if a.get("email"):
                return a["email"].lower()
    except Exception:
        pass
    return None


def cmd_login(args):
    reg = load_registry()
    dest = os.path.join(PROFILES_DIR, args.as_name)
    os.makedirs(dest, exist_ok=True)
    chrome = find_chrome()
    if not chrome:
        log("Chrome not found."); return

    # Launch PLAIN Chrome (NO Playwright, NO automation flags) so Google does NOT
    # block the sign-in ("browser may not be secure"). Playwright only DRIVES the
    # profile later for dola — the one-time Google login must look human.
    log(f"Opening a normal Chrome window for '{args.as_name}'…")
    proc = subprocess.Popen([
        chrome, f"--user-data-dir={dest}", "--no-first-run",
        "--no-default-browser-check", "https://accounts.google.com/",
    ])
    log("")
    log("  👉 In that window: log into the Google account you want for this profile.")
    log("  👉 Then CLOSE the Chrome window completely.")
    log("  👉 Come back here and press ENTER.")
    try:
        input()
    except Exception:
        pass
    # give Chrome a moment to flush + fully exit so the profile isn't locked
    try:
        proc.wait(timeout=3)
    except Exception:
        pass
    import time as _t
    _t.sleep(2)

    gmail = _dedicated_gmail(dest)
    if not gmail:
        log("⛔ No Google account detected in the profile. Did the sign-in finish? "
            "Re-run and make sure you're fully logged in before closing Chrome.")
        return
    dup = gmail_already_used(reg, gmail, exclude=args.as_name)
    if dup:
        log(f"⛔ {gmail} is ALREADY in automation profile '{dup}'. Use another Gmail/profile.")
        return
    reg[args.as_name] = {"gmail": gmail, "imported_from": "(direct login)"}
    save_registry(reg)
    log(f"✅ logged in. Automation profile '{args.as_name}' holds {gmail}.")
    log(f"   Test it: python tools/dola_playwright_test.py --profile \"{dest}\" --dedicated")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(func=cmd_list)
    sub.add_parser("registry").set_defaults(func=cmd_registry)
    pi = sub.add_parser("import")
    pi.add_argument("--from", dest="from_profile", required=True, help='real profile e.g. "Profile 18"')
    pi.add_argument("--as", dest="as_name", required=True, help="automation profile name e.g. acct1")
    pi.set_defaults(func=cmd_import)
    pl = sub.add_parser("login")
    pl.add_argument("--as", dest="as_name", required=True)
    pl.set_defaults(func=cmd_login)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
