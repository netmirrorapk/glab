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

    log("exporting FRESH cookies from the dedicated profile (verifies Google session first)…")
    cookies = await _export_cookies(acc["session_path"], log=log)
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

        # NOTE: this tool NEVER navigates Google in cloak. Visiting accounts.google.com in
        # the cloak context rotates the shared Google __Secure-1PSIDTS and logs the profile's
        # Google out — that was the 'mail logout' bug. Cloak only ever touches dola.

        # 1) DETERMINISTIC dola screen detection (dola DOM only)
        try:
            scr = await s._dola_login_screen()
            log("dola login screen RIGHT NOW:", scr.get("state"))
        except Exception as e:
            log("screen detect error:", str(e)[:80])

        # 2) Cloak-safe login_direct — confirms the dola session INHERITED from the
        #    real-Chrome export; it never OAuths Google.
        log("--- login_direct (cloak-safe: inherited dola cookies, NO Google touch) ---")
        try:
            import time as _tt
            _t0 = _tt.perf_counter()
            dok = await s.login_direct(timeout=15)
            log(f"login_direct took {_tt.perf_counter()-_t0:.1f}s → {dok}")
            ck4 = await ctx.cookies(DOLA_ORIGIN)
            log("dola passport cookies:", sorted({c['name'] for c in ck4} & LOGIN_COOKIES) or "(NONE)")
        except Exception as e:
            import traceback
            log("login_direct error:", str(e)[:100]); traceback.print_exc()

        # --- FAST + ACCURATE login confirmation (the deterministic 'am I logged in?') ---
        import time as _t
        log("--- confirming login: instant cookie gate + one authenticated GET ---")
        t0 = _t.perf_counter()
        fast = await s.is_logged_in()
        t1 = _t.perf_counter()
        info = await s.account_info()
        t2 = _t.perf_counter()
        deep = await s.confirm_logged_in()
        t3 = _t.perf_counter()
        log(f"is_logged_in (cookie gate): {fast}  [{(t1-t0)*1e6:.0f} µs]")
        log(f"account_info (server truth): logged_in={info['logged_in']} user_id={info['user_id']} "
            f"email={info['email']!r}  [{(t2-t1)*1000:.0f} ms]")
        log(f"confirm_logged_in (gate+server): {deep}  [{(t3-t2)*1000:.0f} ms total]")

        log("AFTER: logged_in_for_real:", await s.logged_in_for_real(),
            "| URL:", str(page.url)[:70])
        print("\n>>> Done. (This tool never touches Google in cloak, so it won't log your")
        print(">>> Gmail out.) Window 20s me band ho jayega (ya Ctrl+C).")
        await asyncio.sleep(20)
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
