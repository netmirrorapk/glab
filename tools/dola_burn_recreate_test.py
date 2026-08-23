"""
LIVE end-to-end test of the BURN-RECREATE flow on ONE GUI account, using the EXACT
production path (src/core/dola_playwright_mode helpers + get_accounts() from the app DB):

    login  →  (pre-delete fresh-cookie refresh)  →  delete dola account  →  re-login (recreate)  →  confirm

The dola.com (ByteDance passport) account is deleted; the linked GOOGLE account is
untouched, so re-login creates a FRESH dola account (fresh points) on the same Google —
that's the whole point of the burn.

SAFE default (--dry-run): logs in, opens /delete-account, finds the 'Delete Now' button
but does NOT click it, and does NOT recreate. Nothing is deleted.

    # safe — login + locate the delete button, NO deletion, NO recreate
    python tools/dola_burn_recreate_test.py --account megagaurienterprises --dry-run

    # REAL — actually delete this dola account, then recreate it by re-logging in
    python tools/dola_burn_recreate_test.py --account megagaurienterprises --yes

List accounts:  python tools/dola_burn_recreate_test.py --list
"""
import argparse, asyncio, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8"); sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from src.db.db_manager import get_accounts
from src.core.dola_playwright_mode import (
    _export_cookies, _launch_cloak, _add_cookies_robust, _proxy_dict,
    _burn_recreate_real_chrome,
)
from src.core.dola_api import DolaSession, DOLA_ORIGIN, LOGIN_COOKIES, DolaError


def log(*a):
    print("[burn-test]", *a, flush=True)


def _passport(ck):
    return sorted({c["name"] for c in ck} & LOGIN_COOKIES)


async def run(args):
    accts = get_accounts()
    if args.list:
        for a in accts:
            print(f"  {a['name']}  | session={a['session_path']}")
        return
    acc = next((a for a in accts if args.account.lower() in (a["name"] or "").lower()), None)
    if not acc:
        print("account not found. available:", [a["name"] for a in accts]); return

    session_path = acc["session_path"]
    proxy = _proxy_dict(acc["proxy"]) if acc.get("proxy") else None
    dry = not args.yes
    print("=" * 78)
    print(f"account : {acc['name']}")
    print(f"mode    : {'DRY-RUN (no deletion, no recreate)' if dry else '⚠ REAL DELETE + RECREATE'}")
    print("=" * 78)

    p = ctx = None
    try:
        # ── The WHOLE burn (delete + recreate) runs in REAL CHROME — the profile owner —
        #    so every Google touch rotates __Secure-1PSIDTS in the owner and never desyncs
        #    the session (no Gmail logout). CloakBrowser is used ONLY afterwards to prove the
        #    generation side inherits the fresh dola session. ──
        log("BURN in REAL CHROME (delete + recreate — Google-safe, cloak not involved)…")
        ok, detail, new_cookies = await _burn_recreate_real_chrome(session_path, log=log, dry_run=dry)
        log(f"result: ok={ok} | {detail}")
        if dry:
            log("DRY-RUN done — nothing deleted. Re-run with --yes for the real burn+recreate.")
            return
        print("\n" + "=" * 78)
        if not ok:
            print(f"[burn-test] ❌ FAILED — {detail}")
            print("=" * 78)
            return
        print(f"[burn-test] ✅ SUCCESS (real-chrome) — {detail}")
        print("=" * 78)

        # ── Verify the CloakBrowser generation side inherits the fresh dola session ──
        log("verifying CloakBrowser generation side with the fresh cookies…")
        g = sum(1 for c in new_cookies if "google.com" in str(c.get("domain", "")))
        d = sum(1 for c in new_cookies if "dola.com" in str(c.get("domain", "")))
        log(f"fresh cookies: google={g}, dola={d}")
        p, ctx = await _launch_cloak(new_cookies, proxy, False, log=log)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        session = DolaSession(ctx, page, logger=log)
        await page.goto(f"{DOLA_ORIGIN}/chat/create-video", wait_until="domcontentloaded")
        cloak_ok = await session.confirm_logged_in()
        info = await session.account_info()
        log(f"CloakBrowser generation login: {'OK' if cloak_ok else 'NOT logged in'} "
            f"| user_id={info['user_id']}")
        print("\n" + "=" * 78)
        print(f"[burn-test] cloak generation-side: {'READY' if cloak_ok else 'FAILED'} "
              f"(user_id={info['user_id']})")
        print("Ab dola_account_status.py se dekho ki Google (1PSIDTS) intact hai.")
        print("=" * 78)

    except DolaError as e:
        log("DolaError:", str(e)[:160])
    except Exception as e:
        import traceback
        log("crashed:", str(e)[:160]); traceback.print_exc()
    finally:
        await asyncio.sleep(2)
        try:
            if ctx: await ctx.close()
        except Exception:
            pass
        try:
            if p: await p.stop()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="")
    ap.add_argument("--yes", action="store_true", help="REALLY delete + recreate (default is dry-run)")
    ap.add_argument("--dry-run", action="store_true", help="explicit dry-run (default)")
    ap.add_argument("--list", action="store_true")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
