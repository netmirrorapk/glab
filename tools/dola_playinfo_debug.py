"""
Debug the play-info endpoints for an EXISTING vid (no new generation / no point spent).
Dumps the RAW response of both /samantha/video/get_play_info and
/samantha/media/get_play_info so we can see exactly what shape the unwatermarked-master
data comes in and fix get_play_info_hd / _best_master_url accordingly.

    python tools/dola_playinfo_debug.py --account acct1 --vid v186a3gm000cda2oqjfog65vcu2krt30
"""
import argparse, asyncio, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8"); sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import dola_run as R
from src.core.dola_api import DolaSession, DOLA_ORIGIN


async def run(args):
    acct = args.account
    profile_dir = os.path.join(R.PROFILES_DIR, acct)
    proxies = json.load(open(R.PROXIES, encoding="utf-8")) if os.path.isfile(R.PROXIES) else {}
    p, ctx = await R._launch(profile_dir, proxies.get(acct), False)
    try:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        s = DolaSession(ctx, page, logger=lambda *a: print("[dbg]", *a))
        if not await s.login_via_google(timeout=90):
            print("login failed"); return
        await s._ensure_base()
        try:
            if "dola.com" not in str(page.url or ""):
                await page.goto(f"{DOLA_ORIGIN}/chat/create-video", wait_until="domcontentloaded")
                await asyncio.sleep(1.5)
        except Exception:
            pass

        # The extension's exact query params for media/get_play_info
        ext_params = ("version_code=20800&language=en&device_platform=web&aid=497858"
                      "&real_aid=497858&pkg_type=release_version&samantha_web=1"
                      "&use-olympus-account=1")

        async def raw_post(full_url, body, origin):
            # ctx.request runs OUTSIDE the page → bypasses CORS (like the extension's
            # host_permissions), so we see the REAL server response, not a CORS error.
            try:
                resp = await s.ctx.request.post(
                    full_url,
                    headers={"content-type": "application/json", "origin": origin},
                    data=json.dumps(body))
                return {"status": resp.status, "body": await resp.text()}
            except Exception as e:
                return {"status": -1, "body": "ERR:" + str(e)[:150]}

        tests = [
            ("dola media (in-page/pf)", "pf", "/samantha/media/get_play_info", {"key": args.vid}),
            ("dola video (in-page/pf)", "pf", "/samantha/video/get_play_info", {"vid": args.vid}),
            ("dola media (ctx.request, no-CORS)", "raw",
             f"https://www.dola.com/samantha/media/get_play_info?{ext_params}",
             {"key": args.vid}),
            ("DOUBAO media (ctx.request, no-CORS)", "raw",
             f"https://www.doubao.com/samantha/media/get_play_info?{ext_params}",
             {"key": args.vid}),
        ]
        for name, kind, path, body in tests:
            print("\n" + "=" * 78)
            print(name, "->", path[:70], "  body:", json.dumps(body))
            try:
                if kind == "pf":
                    r = await s.pf(path, body)
                else:
                    r = await raw_post(path, body, path.split("/samantha")[0])
                print("status:", r.get("status"))
                b = r.get("body") or ""
                print("len:", len(b))
                try:
                    j = json.loads(b)
                    print(json.dumps(j, ensure_ascii=False, indent=1)[:2500])
                except Exception:
                    print(b[:1500])
            except Exception as e:
                print("call error:", str(e)[:160])
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
    ap.add_argument("--account", default="acct1")
    ap.add_argument("--vid", required=True)
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
