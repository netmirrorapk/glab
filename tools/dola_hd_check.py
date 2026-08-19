"""
Live-test the UNWATERMARKED HD download on an EXISTING conversation (no new point).
Re-logs in, pulls the chain, finds the finished vid + its vod fallback_api, decrypts
the master, and downloads it — reporting HD status + size.

    python tools/dola_hd_check.py --account acct2 --conv 38416216949588497
"""
import argparse, asyncio, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8"); sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import dola_run as R
from src.core.dola_api import DolaSession, DOLA_ORIGIN, _extract_fallback_api, _extract_all_vids

OUT = os.path.join(R.ROOT, "outputs", "dola_hd_check")


async def run(args):
    os.makedirs(OUT, exist_ok=True)
    profile_dir = os.path.join(R.PROFILES_DIR, args.account)
    proxies = json.load(open(R.PROXIES, encoding="utf-8")) if os.path.isfile(R.PROXIES) else {}
    p, ctx = await R._launch(profile_dir, proxies.get(args.account), False)
    try:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        s = DolaSession(ctx, page, logger=lambda *a: print(f"[{args.account}]", *a))
        if not await s.login_via_google(timeout=90):
            print("login failed — run: python tools/dola_profiles.py login --as", args.account); return
        await s._ensure_base()
        try:
            if "dola.com" not in str(page.url or ""):
                await page.goto(f"{DOLA_ORIGIN}/chat/create-video", wait_until="domcontentloaded")
                await asyncio.sleep(1.5)
        except Exception:
            pass
        print("logged in ✅ — pulling conversation", args.conv)
        msg = await s._pull_single(args.conv)
        vids = _extract_all_vids(msg)
        api = _extract_fallback_api(msg)
        print("vids in chain:", vids[:3])
        print("fallback_api present:", bool(api), "| unwatermarked:", "logo_type=unwatermarked" in api)
        if not vids:
            print("no finished vid in this conversation yet (still generating?) — try again in a minute")
            return
        vid = vids[-1]
        out = os.path.join(OUT, f"hdcheck_{args.account}_{vid[:12]}.mp4")
        print("\n▶ running full _fetch_and_save (prefers unwatermarked master)…")
        await s._fetch_and_save(vid, msg, out)
        n = os.path.getsize(out) if os.path.exists(out) else 0
        print("\n" + "=" * 66)
        print(f"RESULT: HD unwatermarked = {getattr(s, 'last_was_hd', False)} | "
              f"size = {n/1024/1024:.1f} MB ({n} bytes)")
        print("file:", out)
        print("=" * 66)
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
    ap.add_argument("--account", required=True)
    ap.add_argument("--conv", required=True)
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
