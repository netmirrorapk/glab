"""
Watch a CloakBrowser dola login step-by-step to see WHERE it gets stuck. Uses a GUI
account's own session (from the app DB), exports FRESH cookies, launches CloakBrowser
VISIBLE (not headless), navigates to dola, dumps the login/guest state, then drives
login_via_google — logging every step. Window stays open so you can inspect dola.

    python tools/dola_cloak_login_debug.py --account megagaurienterprises
    python tools/dola_cloak_login_debug.py --list
"""
import argparse, asyncio, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8"); sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from src.db.db_manager import get_accounts
from src.core.dola_playwright_mode import _export_cookies, _launch_cloak, _proxy_dict
from src.core.dola_api import DolaSession, DOLA_ORIGIN, LOGIN_COOKIES


def log(*a):
    print("[cloak-dbg]", *a, flush=True)


async def run(args):
    accts = get_accounts()
    if args.list:
        for a in accts:
            print(f"  {a['name']}  | proxy={a['proxy'] or '(none)'} | session={a['session_path']}")
        return
    acc = next((a for a in accts if args.account.lower() in (a["name"] or "").lower()), None)
    if not acc:
        print("account not found. available:", [a["name"] for a in accts]); return
    print("=" * 74)
    print(f"account : {acc['name']}")
    print(f"session : {acc['session_path']}")
    print(f"proxy   : {acc['proxy'] or '(none)'}")
    print("=" * 74)

    log("exporting FRESH cookies from the dedicated profile…")
    cookies = await _export_cookies(acc["session_path"])
    g = sum(1 for c in cookies if "google.com" in str(c.get("domain", "")))
    log(f"exported {len(cookies)} cookies (google={g})")

    proxy = _proxy_dict(acc["proxy"]) if acc["proxy"] else None
    log("launching CloakBrowser VISIBLE (headless=False) — dekho window…")
    p, ctx = await _launch_cloak(cookies, proxy, False, log=log)   # False = visible
    try:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        s = DolaSession(ctx, page, logger=log)

        try:
            ipr = await ctx.request.get("https://api.ipify.org?format=json")
            log("IP:", (await ipr.text()).strip())
        except Exception:
            pass

        log("navigating to dola /chat/create-video…")
        await page.goto(f"{DOLA_ORIGIN}/chat/create-video", wait_until="domcontentloaded")
        await asyncio.sleep(3)
        log("URL:", str(page.url)[:90])

        ck = await ctx.cookies(DOLA_ORIGIN)
        present = sorted({c["name"] for c in ck} & LOGIN_COOKIES)
        log("dola passport cookies present:", present or "(NONE — not authed to dola!)")
        log("is_logged_in(cookies):", await s.is_logged_in(),
            "| page_is_guest:", await s._page_is_guest())

        log("--- driving login_via_google (watch the window) ---")
        ok = await s.login_via_google(timeout=90)
        log("login_via_google returned:", ok)
        await asyncio.sleep(2)
        log("AFTER: is_logged_in:", await s.is_logged_in(),
            "| guest:", await s._page_is_guest(),
            "| logged_in_for_real:", await s.logged_in_for_real())
        log("URL now:", str(page.url)[:90])
        ck2 = await ctx.cookies(DOLA_ORIGIN)
        log("dola passport cookies now:", sorted({c["name"] for c in ck2} & LOGIN_COOKIES) or "(still NONE)")
        try:
            txt = await page.evaluate("() => (document.body?document.body.innerText:'').replace(/\\s+/g,' ').slice(0,220)")
            log("page text:", txt)
        except Exception:
            pass

        print("\n>>> Window khula hai — dekho dola kya dikha raha hai (login modal / guest / logged-in).")
        print(">>> 180s baad apne aap band ho jayega (ya Ctrl+C).")
        await asyncio.sleep(180)
    finally:
        try:
            await ctx.close()
        except Exception:
            pass
        try:
            if p: await p.stop()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="")
    ap.add_argument("--list", action="store_true")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
