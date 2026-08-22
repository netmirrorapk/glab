"""
SAFE account status — reports each GUI account's Google + dola cookie health WITHOUT
navigating anywhere (no accounts.google.com, no dola). It just opens the profile, reads
the cookie jar, and closes. Because it never visits Google, it does NOT rotate
__Secure-1PSIDTS and CANNOT log the account out (unlike a myaccount.google.com check).

    python tools/dola_account_status.py            # all accounts
    python tools/dola_account_status.py --account megashoeb82
"""
import argparse, asyncio, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8"); sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from playwright.async_api import async_playwright
from src.db.db_manager import get_accounts
from src.core.dola_playwright_mode import _find_chrome

# Core Google auth cookies — SID being present is the practical "Google session exists".
GOOGLE_AUTH = {"SID", "HSID", "SSID", "__Secure-1PSID", "__Secure-3PSID",
               "APISID", "SAPISID", "LSID", "__Secure-1PSIDTS"}
DOLA_AUTH = {"sessionid", "sid_tt", "uid_tt"}


async def one(acc):
    p = await async_playwright().start()
    try:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=acc["session_path"], executable_path=_find_chrome(),
            headless=True, ignore_default_args=["--enable-automation"],
            args=["--no-first-run", "--no-default-browser-check"])
        try:
            ck = await ctx.cookies()          # NO navigation — safe, no rotation
        finally:
            try: await ctx.close()
            except Exception: pass
    finally:
        try: await p.stop()
        except Exception: pass
    gnames = {c["name"] for c in ck if "google.com" in str(c.get("domain", ""))}
    dnames = {c["name"] for c in ck if "dola.com" in str(c.get("domain", ""))}
    g_ok = "SID" in gnames
    d_ok = {"sessionid", "sid_tt"} <= dnames
    ts = "__Secure-1PSIDTS" in gnames
    return {
        "name": acc["name"], "google": len(gnames), "dola": len(dnames),
        "google_ok": g_ok, "psidts": ts, "dola_ok": d_ok,
        "missing_google_auth": sorted(GOOGLE_AUTH - gnames),
    }


async def run(args):
    accts = get_accounts()
    if args.account:
        accts = [a for a in accts if args.account.lower() in (a["name"] or "").lower()]
    print("=" * 92)
    print(f"{'account':38} {'google':>7} {'dola':>5}  {'Google':10} {'1PSIDTS':8} {'dola-sess'}")
    print("-" * 92)
    for a in accts:
        try:
            r = await one(a)
            print(f"{r['name']:38} {r['google']:>7} {r['dola']:>5}  "
                  f"{'LIVE' if r['google_ok'] else 'LOGGED-OUT':10} "
                  f"{'yes' if r['psidts'] else 'MISSING':8} {'yes' if r['dola_ok'] else 'no'}")
        except Exception as e:
            print(f"{a['name']:38} ERROR {str(e)[:50]}")
    print("=" * 92)
    print("Google 'LIVE' = SID cookie present. '1PSIDTS MISSING' = Google will likely force")
    print("a re-login (session-validation cookie gone). Re-login via 'Login for dola (Google)'.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
