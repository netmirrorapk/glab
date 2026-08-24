"""
Open ONE account's real-Chrome profile in a VISIBLE window at Google sign-in so YOU can
log the Google account in by hand. The Google session it establishes is the ONE-TIME seed
CloakBrowser reuses.

IMPORTANT — this launches Chrome the SAME anti-detect way the app does: a plain subprocess
with --remote-debugging-port + --disable-blink-features=AutomationControlled and NO
--no-sandbox / NO Playwright launch. If Chrome is started BY Playwright (automation flags),
Google blocks the sign-in with 'this browser or app may not be secure'. We attach over CDP
only to watch the cookies and save when you're signed in.

    python tools/dola_google_login.py --account megagaurienterprises

Log in in the window that opens; it closes itself once you're signed in (or after 5 min).
"""
import argparse, asyncio, json, os, subprocess, sys, time, urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8"); sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from playwright.async_api import async_playwright
from src.db.db_manager import get_accounts
from src.core.dola_playwright_mode import _find_chrome, _cookie_cache

PORT = 9223


def _cdp_live(port):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2)
        return True
    except Exception:
        return False


async def run(args):
    acc = next((a for a in get_accounts() if args.account.lower() in (a["name"] or "").lower()), None)
    if not acc:
        print("account not found. available:", [a["name"] for a in get_accounts()]); return
    session_path = acc["session_path"]
    print("=" * 74)
    print(f"account : {acc['name']}")
    print("A real-Chrome window will open at Google sign-in. LOG IN by hand.")
    print("It closes automatically once you're signed in (or after 5 minutes).")
    print("=" * 74)

    # drop the stale cloak cache so the next run re-seeds from THIS fresh Google login
    try:
        cf = _cookie_cache(session_path)
        if os.path.isfile(cf):
            os.remove(cf); print("[login] cleared stale cloak cookie cache")
    except Exception:
        pass

    chrome = _find_chrome()
    # Plain subprocess launch (NOT Playwright) with automation hidden — this is what makes
    # Google accept the sign-in. Visible window (no --headless) so you can log in.
    chrome_args = [
        chrome,
        f"--remote-debugging-port={PORT}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={session_path}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--window-position=80,60",
        "--window-size=1150,850",
        "https://accounts.google.com/",
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    proc = subprocess.Popen(chrome_args, creationflags=creationflags)
    try:
        for _ in range(25):
            if _cdp_live(PORT):
                break
            time.sleep(1)
        if not _cdp_live(PORT):
            print("[login] Chrome CDP didn't start — is another Chrome using this profile? Close it and retry.")
            return

        p = await async_playwright().start()
        try:
            browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            print("[login] window open — sign in now…")
            done = False
            for i in range(150):          # up to ~5 min
                await asyncio.sleep(2)
                try:
                    names = {c["name"] for c in await ctx.cookies()
                             if "google.com" in str(c.get("domain", ""))}
                except Exception:
                    names = set()
                if {"SID", "__Secure-1PSID"} <= names:
                    for _ in range(8):    # let the rotating validation cookie settle
                        await asyncio.sleep(2)
                        names = {c["name"] for c in await ctx.cookies()
                                 if "google.com" in str(c.get("domain", ""))}
                        if "__Secure-1PSIDTS" in names:
                            break
                    print(f"[login] Google signed in OK (google cookies={len(names)}, "
                          f"1PSIDTS={'yes' if '__Secure-1PSIDTS' in names else 'not yet'})")
                    done = True
                    break
                if i and i % 15 == 0:
                    print(f"[login] …still waiting ({i*2}s)")
            if not done:
                print("[login] timed out — sign-in not completed.")
            try:
                await browser.close()
            except Exception:
                pass
        finally:
            try:
                await p.stop()
            except Exception:
                pass
    finally:
        try:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=6)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    print("[login] done. Ab: python tools/dola_burn_recreate_test.py --account", acc["name"], "--yes")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", required=True)
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
