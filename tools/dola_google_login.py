"""
Open ONE account's real-Chrome profile in a VISIBLE window at Google sign-in, so YOU can
log the Google account in by hand (enter the password yourself — automation can't and
shouldn't). The script watches the cookie jar and, the moment a real Google session
appears (SID cookie), it saves + closes. That fresh Google session is then the ONE-TIME
seed CloakBrowser reuses.

    python tools/dola_google_login.py --account megagaurienterprises

Log in in the window that opens; it closes itself once you're signed in (or after 5 min).
"""
import argparse, asyncio, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8"); sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from playwright.async_api import async_playwright
from src.db.db_manager import get_accounts
from src.core.dola_playwright_mode import _find_chrome, _cookie_cache


async def run(args):
    acc = next((a for a in get_accounts() if args.account.lower() in (a["name"] or "").lower()), None)
    if not acc:
        print("account not found. available:", [a["name"] for a in get_accounts()]); return
    session_path = acc["session_path"]
    print("=" * 74)
    print(f"account : {acc['name']}")
    print("A real-Chrome window will open. LOG IN to this Google account by hand.")
    print("It closes automatically once you're signed in (or after 5 minutes).")
    print("=" * 74)

    # drop the stale cloak cache so the next run re-seeds from THIS fresh Google login
    try:
        cf = _cookie_cache(session_path)
        if os.path.isfile(cf):
            os.remove(cf); print("[login] cleared stale cloak cookie cache")
    except Exception:
        pass

    p = await async_playwright().start()
    try:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=session_path, executable_path=_find_chrome(), headless=False,
            ignore_default_args=["--enable-automation"],
            args=["--no-first-run", "--no-default-browser-check",
                  "--window-position=80,60", "--window-size=1150,850"])
        try:
            pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
            try:
                await pg.bring_to_front()
            except Exception:
                pass
            await pg.goto("https://accounts.google.com/", wait_until="domcontentloaded")
            print("[login] waiting for you to sign in…")
            for i in range(150):          # up to ~5 min
                await asyncio.sleep(2)
                try:
                    names = {c["name"] for c in await ctx.cookies()
                             if "google.com" in str(c.get("domain", ""))}
                except Exception:
                    names = set()
                if {"SID", "__Secure-1PSID"} <= names:
                    # give Google a moment to also set the rotating validation cookie
                    for _ in range(8):
                        await asyncio.sleep(2)
                        names = {c["name"] for c in await ctx.cookies()
                                 if "google.com" in str(c.get("domain", ""))}
                        if "__Secure-1PSIDTS" in names:
                            break
                    ok_ts = "__Secure-1PSIDTS" in names
                    print(f"[login] Google signed in ✅ (google cookies={len(names)}, "
                          f"1PSIDTS={'yes' if ok_ts else 'not yet'})")
                    print("[login] saving session + closing…")
                    break
                if i and i % 15 == 0:
                    print(f"[login] …still waiting ({i*2}s)")
            else:
                print("[login] timed out — you didn't finish signing in.")
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
    print("[login] done. Ab burn/generation test chala sakte ho — cloak isi fresh Google session ko seed karega.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", required=True)
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
