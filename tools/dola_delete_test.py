"""
Isolated LIVE test of the dola account-DELETE (burn) step on ONE account, so we can
debug why 'Delete Now' isn't found without running the whole queue.

SAFE by default (--dry-run): opens /delete-account, completes the Google re-auth,
locates the 'Delete Now' button but does NOT click it — and DUMPS every visible
clickable element so we can see the real button markup. Nothing gets deleted.

    # safe: find the button + dump the page (NO deletion)
    python tools/dola_delete_test.py --account acct1 --dry-run

    # REAL: actually delete the account (then it can be re-created by logging in)
    python tools/dola_delete_test.py --account acct1

    # regular Chrome instead of CloakBrowser
    python tools/dola_delete_test.py --account acct1 --dry-run --no-cloak

List accounts:  python tools/dola_delete_test.py --list
"""
import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8"); sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import dola_run as R
from src.core.dola_api import DolaSession, DolaError


def _accounts():
    if os.path.isfile(R.REGISTRY):
        try:
            return list(json.load(open(R.REGISTRY, encoding="utf-8")).keys())
        except Exception:
            pass
    out = []
    if os.path.isdir(R.PROFILES_DIR):
        for n in os.listdir(R.PROFILES_DIR):
            if os.path.isdir(os.path.join(R.PROFILES_DIR, n)):
                out.append(n)
    return out


async def run(args):
    accts = _accounts()
    acct = args.account or (accts[0] if accts else None)
    if not acct:
        print("no accounts found"); return
    profile_dir = os.path.join(R.PROFILES_DIR, acct)
    if not os.path.isdir(profile_dir):
        print(f"profile missing for '{acct}'"); return
    proxies = json.load(open(R.PROXIES, encoding="utf-8")) if os.path.isfile(R.PROXIES) else {}
    proxy = proxies.get(acct)
    cloak = not args.no_cloak

    def log(*a):
        R.log(acct, *a)

    print("=" * 70)
    print(f"[delete-test] account={acct}  mode={'DRY-RUN (no deletion)' if args.dry_run else 'REAL DELETE'}  "
          f"cloak={cloak}")
    print("=" * 70)

    p = ctx = None
    try:
        if cloak:
            cookies = await R._cookies_for(acct, profile_dir)
            cproxy = R._proxy_dict(proxy) if isinstance(proxy, str) else proxy
            p, ctx = await R._launch_cloak(cookies, cproxy, False)   # headed so we can watch
        else:
            p, ctx = await R._launch(profile_dir, proxy, False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        session = DolaSession(ctx, page, logger=log)
        log("checking dola login…")
        if not await session.login_via_google(timeout=90):
            if cloak:
                try:
                    fresh = await R._export_cookies(profile_dir)
                    await ctx.add_cookies(fresh)
                except Exception:
                    pass
            if not await session.login_via_google(timeout=90):
                log("login failed — aborting"); return
        await session._ensure_base()
        log("logged in ✅ — starting delete flow…")

        ok, detail = await session.delete_account(timeout=90, log=log, dry_run=args.dry_run)
        print("\n" + "=" * 70)
        print(f"[delete-test] result: ok={ok}  detail={detail}")
        print("=" * 70)
        if ok and not args.dry_run:
            log("account deleted — re-logging in to create a FRESH account (recreate)…")
            session._base = {}
            if await session.login_via_google(timeout=90):
                await session._ensure_base()
                rl = await session.check_rate_limit()
                log(f"fresh account ready ✅ (send-rate limited={rl.get('is_limit')})")
            else:
                log("re-login after delete failed")
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
    ap.add_argument("--account", default=None)
    ap.add_argument("--dry-run", action="store_true", help="find the button + dump the page, do NOT delete")
    ap.add_argument("--no-cloak", action="store_true", help="use regular Chrome instead of CloakBrowser")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    if args.list:
        print("accounts:", _accounts() or "(none)"); return
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
