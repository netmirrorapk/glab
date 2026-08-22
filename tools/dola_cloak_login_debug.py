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

        # 1) Is the cloak session ACTUALLY signed into Google? (silent OAuth needs this)
        log("--- checking cloak's Google session (myaccount.google.com) ---")
        try:
            await page.goto("https://myaccount.google.com/", wait_until="domcontentloaded")
            await asyncio.sleep(3)
            gu = str(page.url or "")
            gtxt = await page.evaluate("() => (document.body?document.body.innerText:'').replace(/\\s+/g,' ').slice(0,160)")
            signed_in = ("myaccount.google.com" in gu and "signin" not in gu and "accountchooser" not in gu)
            log("google url:", gu[:80])
            log("GOOGLE signed-in in cloak:", signed_in, "| text:", gtxt[:120])
        except Exception as e:
            log("google check error:", str(e)[:80])

        # 1b) DETERMINISTIC screen detection — what login screen is dola showing NOW?
        try:
            scr = await s._dola_login_screen()
            log("dola login screen RIGHT NOW:", scr.get("state"),
                "| flags:", {k: scr.get(k) for k in ("googleBtn", "loginBtn", "ageGate", "googleSignin")})
        except Exception as e:
            log("screen detect error:", str(e)[:80])

        # 2) Try the DIRECT / SILENT login (dola auto_open OAuth, prompt=none, NO click)
        log("--- trying DIRECT silent login (ensure_logged_in, 25s, no UI click) ---")
        try:
            sok = await s.ensure_logged_in(timeout=3)
            ck3 = await ctx.cookies(DOLA_ORIGIN)
            log("ensure_logged_in (silent) returned:", sok,
                "| dola passport cookies now:", sorted({c['name'] for c in ck3} & LOGIN_COOKIES) or "(still NONE)")
        except Exception as e:
            log("ensure_logged_in error:", str(e)[:80])
        # 2b) The NEW direct login (navigate to Google OAuth authorize URL, no UI click)
        log("--- trying login_direct (Google OAuth redirect → dola callback, NO click) ---")
        try:
            import time as _tt
            _t0 = _tt.perf_counter()
            dok = await s.login_direct(timeout=30)
            _elapsed = _tt.perf_counter() - _t0
            log(f"⏱ login_direct took {_elapsed:.1f}s")
            ck4 = await ctx.cookies(DOLA_ORIGIN)
            log("login_direct returned:", dok,
                "| logged_in_for_real:", await s.logged_in_for_real(),
                "| dola passport cookies:", sorted({c['name'] for c in ck4} & LOGIN_COOKIES) or "(NONE)")
        except Exception as e:
            import traceback
            log("login_direct error:", str(e)[:100]); traceback.print_exc()
        if await s.logged_in_for_real():
            log("✅✅ DIRECT LOGIN WORKED — dola session established without any UI click!")
        # back to the create page for the click tests
        try:
            await page.goto(f"{DOLA_ORIGIN}/chat/create-video", wait_until="domcontentloaded")
            await asyncio.sleep(2)
        except Exception:
            pass

        # --- directly test clicking "Continue with Google" with several methods ---
        log("--- locating 'Continue with Google' button ---")
        rect = await page.evaluate(r"""() => {
            const els = Array.from(document.querySelectorAll("button,[role='button'],div,span,a"));
            for (const e of els) {
                const t = (e.textContent||'').replace(/\s+/g,' ').trim().toLowerCase();
                if (t === 'continue with google' || t === 'sign in with google' || t === 'log in with google') {
                    const r = e.getBoundingClientRect();
                    if (r.width>4 && r.height>4) return {x:r.x+r.width/2, y:r.y+r.height/2, w:r.width, h:r.height, t};
                }
            }
            return null;
        }""")
        log("button rect:", rect)

        async def try_click(method):
            try:
                async with ctx.expect_page(timeout=8000) as pi:
                    if method == "mouse" and rect:
                        await page.mouse.click(rect["x"], rect["y"])
                    elif method == "locator":
                        await page.get_by_text("Continue with Google", exact=False).first.click(timeout=5000)
                    elif method == "eval":
                        await page.evaluate("""() => {
                            const els=[...document.querySelectorAll("button,[role='button'],div,span,a")];
                            const b=els.find(e=>(e.textContent||'').trim().toLowerCase()==='continue with google');
                            if(b) b.click();
                        }""")
                pop = await pi.value
                log(f"  [{method}] → POPUP OPENED ✅  url={str(pop.url)[:70]}")
                return pop
            except Exception as e:
                log(f"  [{method}] → no popup ({str(e)[:50]})")
                return None

        log("--- testing click methods (watch which one opens the Google popup) ---")
        pop = await try_click("mouse")
        if not pop:
            pop = await try_click("locator")
        if not pop:
            pop = await try_click("eval")
        if pop:
            try:
                await pop.wait_for_load_state("domcontentloaded")
                await asyncio.sleep(2)
                txt = await pop.evaluate("() => (document.body?document.body.innerText:'').replace(/\\s+/g,' ').slice(0,200)")
                log("  popup text:", txt)
            except Exception:
                pass

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

        log("--- now driving the full login_via_google for comparison ---")
        ok = await s.login_via_google(timeout=60)
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
