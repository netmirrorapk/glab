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
    _export_cookies, _launch_cloak, _add_cookies_robust, _proxy_dict, _fresh_relogin,
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
        # ── 1) LOGIN (real-Chrome login inside export → cloak inherits the session) ──
        log("STEP 1: exporting FRESH cookies (real-Chrome dola login) …")
        cookies = await _export_cookies(session_path, log=log)
        p, ctx = await _launch_cloak(cookies, proxy, False, log=log)   # visible so you can watch
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        session = DolaSession(ctx, page, logger=log)
        await page.goto(f"{DOLA_ORIGIN}/chat/create-video", wait_until="domcontentloaded")

        if not await session.confirm_logged_in():
            log("not logged in from injected cookies → login_direct …")
            if not await session.login_direct(timeout=90):
                log("❌ login failed — aborting"); return
        info = await session.account_info()
        log(f"✅ LOGGED IN — user_id={info['user_id']} name={info['email']!r} "
            f"| passport={_passport(await ctx.cookies(DOLA_ORIGIN))}")
        await session._ensure_base()
        old_uid = info["user_id"]

        # ── 2) PRE-DELETE fresh cookie refresh (production does this before /delete) ──
        log("STEP 2: pre-delete FRESH cookie refresh (as the runner does) …")
        try:
            fresh = await _export_cookies(session_path, log=log)
            await _add_cookies_robust(ctx, fresh, log=log)
        except Exception as e:
            log("pre-delete refresh failed:", str(e)[:80])

        # ── 3) DELETE the dola account ──
        log(f"STEP 3: {'DRY-RUN delete (no click)' if dry else 'REAL delete'} …")
        ok, detail = await session.delete_account(timeout=90, log=log, dry_run=dry)
        log(f"delete result: ok={ok} detail={detail}")
        if dry:
            log("DRY-RUN done — nothing deleted, no recreate. Re-run with --yes for the real flow.")
            return
        if not ok:
            log("❌ delete failed — not recreating"); return

        # ── 4) RE-LOGIN → RECREATE a fresh dola account on the same Google ──
        log("STEP 4: re-login to RECREATE a fresh account …")
        session._base = {}
        t0 = time.time()
        ok2 = await _fresh_relogin(session, session_path, log=log, timeout=90)
        log(f"re-login took {time.time()-t0:.1f}s → {ok2}")
        if not ok2:
            log("❌ re-login after delete failed"); return
        await session._ensure_base()
        info2 = await session.account_info()
        rl = await session.check_rate_limit()
        log(f"✅ RECREATED — user_id={info2['user_id']} (old={old_uid}) "
            f"| new-account={info2['user_id'] != old_uid} | send-throttled={rl.get('is_limit')}")
        print("\n" + "=" * 78)
        print(f"[burn-test] SUCCESS — burn→recreate complete. "
              f"old_uid={old_uid} new_uid={info2['user_id']}")
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
