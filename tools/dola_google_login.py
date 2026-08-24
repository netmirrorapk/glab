"""
Open ONE account's real-Chrome profile in a VISIBLE window at Google sign-in so YOU can
log the Google account in by hand. This Google session is the ONE-TIME seed CloakBrowser
reuses.

Launches Chrome as a PLAIN subprocess with --disable-blink-features=AutomationControlled
and NO --no-sandbox / NO Playwright automation — that's what makes Google ACCEPT the
sign-in (a Playwright-launched Chrome is blocked with 'this browser or app may not be
secure'). The window stays open; YOU log in and then CLOSE the window yourself. The script
waits for you to close it, then reads + reports the Google cookies.

    python tools/dola_google_login.py --account megagaurienterprises

Steps: log in → (2FA if any) → then just CLOSE the Chrome window. The script does the rest.
"""
import argparse, asyncio, os, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8"); sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from playwright.async_api import async_playwright
from src.db.db_manager import get_accounts
from src.core.dola_playwright_mode import _find_chrome, _cookie_cache


async def _read_cookies(session_path):
    """Read the profile's cookies AFTER Chrome closed (brief, no navigation)."""
    p = await async_playwright().start()
    try:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=session_path, executable_path=_find_chrome(), headless=True,
            ignore_default_args=["--enable-automation"],
            args=["--no-first-run", "--no-default-browser-check"])
        try:
            return await asyncio.wait_for(ctx.cookies(), timeout=20)
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


async def run(args):
    acc = next((a for a in get_accounts() if args.account.lower() in (a["name"] or "").lower()), None)
    if not acc:
        print("account not found. available:", [a["name"] for a in get_accounts()]); return
    session_path = acc["session_path"]
    print("=" * 74)
    print(f"account : {acc['name']}")
    print("Ek Chrome window khulega. Google mein LOGIN karo (password + 2FA).")
    print(">>> Login ho jaye to Chrome window KHUD BAND kar do. <<<")
    print("Window band karte hi ye script cookies check + save karega.")
    print("=" * 74)

    # drop the stale cloak cache so the next run re-seeds from THIS fresh Google login
    try:
        cf = _cookie_cache(session_path)
        if os.path.isfile(cf):
            os.remove(cf); print("[login] cleared stale cloak cookie cache")
    except Exception:
        pass

    chrome = _find_chrome()
    # PLAIN subprocess (NOT Playwright) with automation hidden + NO --no-sandbox → Google
    # accepts the sign-in. A distinct --user-data-dir spawns its own instance (no handoff).
    chrome_args = [
        chrome,
        f"--user-data-dir={session_path}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--window-position=80,60",
        "--window-size=1150,850",
        "https://accounts.google.com/",
    ]
    print("[login] Chrome khol raha hoon…")
    t0 = time.time()
    proc = subprocess.Popen(chrome_args)
    proc.wait()   # blocks until YOU close the Chrome window
    elapsed = time.time() - t0
    if elapsed < 8:
        print(f"[login] ⚠ Chrome {elapsed:.0f}s me band ho gaya — shayad pehle se koi Chrome is "
              "profile ko use kar raha tha (handoff). SAARE Chrome window band karke dobara chalao.")
        return
    print(f"[login] window band hua ({elapsed:.0f}s baad) — cookies padh raha hoon…")

    try:
        ck = await _read_cookies(session_path)
    except Exception as e:
        print("[login] cookie read error:", str(e)[:80]); return
    g = {c["name"] for c in ck if "google.com" in str(c.get("domain", ""))}
    ok = "SID" in g and "__Secure-1PSID" in g
    ts = "__Secure-1PSIDTS" in g
    print(f"[login] google cookies={len(g)} | SID={'yes' if 'SID' in g else 'no'} | "
          f"1PSIDTS={'yes' if ts else 'MISSING'}")
    if ok:
        print("[login] ✅ Google signed in. Ab burn/generation test chala sakte ho:")
        print(f"        python tools/dola_burn_recreate_test.py --account {acc['name']} --yes")
    else:
        print("[login] ❌ Google session nahi bana (SID missing) — login adhoora tha? Dobara try karo.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", required=True)
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
