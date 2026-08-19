"""
One-off LIVE test of the creative-video SKILL route (dola_use_skill_flow) on ONE
already-logged-in account. Reuses tools/dola_run.py's launch + cookie helpers so it
behaves exactly like the real runner, but generates a SINGLE video with verbose logs
so we can watch every step: skill submit -> auto "yes" -> vid -> download -> watermark.

Examples:
    # default = skill route, first account in registry, off-screen Chrome
    python tools/dola_skill_test.py --prompt "a cat walking"

    # pick account + ratio/duration, CloakBrowser headless (truly invisible)
    python tools/dola_skill_test.py --account acct1 --prompt "sunset over the sea" --ratio 9:16 --duration 10 --cloak

    # compare: OLD direct route (no skill)
    python tools/dola_skill_test.py --prompt "a cat walking" --no-skill

List available accounts:
    python tools/dola_skill_test.py --list
"""
import argparse
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8"); sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import dola_run as R                      # reuse launch/cookies/proxy helpers + paths
from src.core.dola_api import (DolaSession, DailyLimitReached, GenerationRefused,
                               NotLoggedIn, GotImagesNotVideo, HighDemand, DolaError)

OUT_DIR = os.path.join(R.ROOT, "outputs", "dola_skill_test")


def _accounts():
    if os.path.isfile(R.REGISTRY):
        try:
            return list(json.load(open(R.REGISTRY, encoding="utf-8")).keys())
        except Exception:
            pass
    # fall back to any dir under data/dola_profiles that looks like a profile
    out = []
    if os.path.isdir(R.PROFILES_DIR):
        for n in os.listdir(R.PROFILES_DIR):
            if os.path.isdir(os.path.join(R.PROFILES_DIR, n)):
                out.append(n)
    return out


async def run(args):
    os.makedirs(OUT_DIR, exist_ok=True)
    accts = _accounts()
    acct = args.account or (accts[0] if accts else None)
    if not acct:
        print("no accounts — set up with: python tools/dola_profiles.py login --as <name>")
        return
    profile_dir = os.path.join(R.PROFILES_DIR, acct)
    if not os.path.isdir(profile_dir):
        print(f"profile missing for '{acct}' at {profile_dir}")
        return
    proxies = json.load(open(R.PROXIES, encoding="utf-8")) if os.path.isfile(R.PROXIES) else {}
    proxy = proxies.get(acct)
    use_skill = not args.no_skill

    def log(*a):
        R.log(acct, *a)

    print("=" * 70)
    print(f"[test] account={acct}  route={'SKILL /creative-video' if use_skill else 'DIRECT ability'}  "
          f"ratio={args.ratio} duration={args.duration}s  cloak={args.cloak} headless={args.headless}")
    print(f"[test] prompt: {args.prompt!r}")
    print("=" * 70)

    p = ctx = None
    try:
        log(f"opening profile ({'CloakBrowser' if args.cloak else 'Chrome'})…")
        if args.cloak:
            cookies = await R._cookies_for(acct, profile_dir)
            cproxy = R._proxy_dict(proxy) if isinstance(proxy, str) else proxy
            p, ctx = await R._launch_cloak(cookies, cproxy, args.headless, log=log)
        else:
            p, ctx = await R._launch(profile_dir, proxy, args.headless)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        session = DolaSession(ctx, page, logger=log)
        try:
            ipr = await ctx.request.get("https://api.ipify.org?format=json")
            log("IP", (await ipr.text()).strip())
        except Exception:
            pass

        log("checking dola login…")
        if not await session.login_via_google(timeout=90):
            if args.cloak:
                log("login failed — refreshing cookies from the dedicated profile & retrying…")
                try:
                    fresh = await R._export_cookies(profile_dir)
                    await R._add_cookies_robust(ctx, fresh, log=log)
                except Exception as e:
                    log("cookie refresh failed:", str(e)[:80])
            if not await session.login_via_google(timeout=90):
                log(f"login failed — run: python tools/dola_profiles.py login --as {acct}")
                return
        await session._ensure_base()
        log("logged in ✅")

        # Make sure the page is ON dola.com before we submit — right after a Google
        # re-auth it can be left on an accounts.google.com/redirect page, and the
        # in-page fetch to dola.com from there fails cross-origin ("Failed to fetch").
        try:
            cur = str(session.page.url or "")
            if "dola.com" not in cur:
                await session.page.goto("https://www.dola.com/chat/create-video",
                                        wait_until="domcontentloaded")
                import asyncio as _a
                await _a.sleep(1.5)
        except Exception:
            pass

        # rate-limit visibility (so we know if the account is send-throttled first)
        try:
            rl = await session.check_rate_limit()
            if rl.get("is_limit"):
                log(f"⚠ account is send-throttled — recovers in ~{rl.get('seconds')}s ({rl.get('limit_tips')})")
            else:
                log("rate-limit: OK (not throttled)")
        except Exception:
            pass

        ref = args.ref or None
        if ref:
            if not os.path.isfile(ref):
                log(f"❌ reference image not found: {ref}")
                return
            log(f"reference image: {ref}")
        out = os.path.join(OUT_DIR, f"skilltest_{acct}_{int(time.time())}.mp4")
        t0 = time.time()
        log(f"▶ generating (use_skill={use_skill or bool(ref)}, ref={'yes' if ref else 'no'})…")
        await session.generate_one(args.prompt, out, model=args.model, ratio=args.ratio,
                                   duration=args.duration, timeout=args.timeout,
                                   use_skill=use_skill, ref_image=ref)
        dt = int(time.time() - t0)
        n = os.path.getsize(out) if os.path.exists(out) else 0
        hd = getattr(session, "last_was_hd", False)
        mb = n / 1024 / 1024
        log(f"✅ VIDEO SAVED in {dt}s — {mb:.1f} MB ({n} bytes) -> {out}")
        log(f"   HD unwatermarked master: {'YES ✅' if hd else 'no (fell back to watermarked stream)'}")
        try:
            import subprocess
            from src.core.ffmpeg_path import ffprobe_exe
            dur = subprocess.run([ffprobe_exe(), "-v", "error", "-show_entries",
                                  "format=duration", "-of", "csv=p=0", out],
                                 capture_output=True, text=True, timeout=20).stdout.strip()
            log(f"   actual duration: {dur}s (requested {args.duration}s, model {args.model})")
        except Exception:
            pass

        if not args.no_watermark:
            try:
                from src.core.dola_playwright_mode import _dewatermark
                ok = await _dewatermark(out)
                log(f"watermark removal: {'done ✅' if ok else 'skipped/failed (ffmpeg?)'}")
            except Exception as e:
                log("watermark step error:", str(e)[:80])

        print("\n" + "=" * 70)
        print(f"[test] SUCCESS — {out}")
        print("=" * 70)

    except HighDemand as e:
        log("❌ HIGH DEMAND (transient):", str(e)[:120])
    except DailyLimitReached as e:
        log("❌ account exhausted / daily limit:", str(e)[:120])
    except GenerationRefused as e:
        log("❌ prompt refused/moderated:", str(e)[:120])
    except GotImagesNotVideo as e:
        log("❌ dola produced images not video:", str(e)[:120])
    except NotLoggedIn as e:
        log("❌ logged out mid-gen:", str(e)[:120])
    except DolaError as e:
        log("❌ DolaError:", str(e)[:160])
    except Exception as e:
        import traceback
        log("❌ crashed:", str(e)[:160])
        traceback.print_exc()
    finally:
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
    ap.add_argument("--prompt", default="a cute cat walking gracefully across a sunny room")
    ap.add_argument("--ref", default=None, help="LOCAL reference image path → reference-image→video (forces skill route)")
    ap.add_argument("--account", default=None, help="profile name (default: first in registry)")
    ap.add_argument("--ratio", default="9:16")
    ap.add_argument("--duration", type=int, default=10)
    ap.add_argument("--model", default="seedance_v2.0", help="seedance_v2.5 / seedance_v2.0 / ic_mini (direct route only)")
    ap.add_argument("--timeout", type=int, default=720)
    ap.add_argument("--no-skill", action="store_true", help="use the OLD direct ability route instead")
    ap.add_argument("--cloak", action="store_true", help="use anti-detect CloakBrowser (true headless)")
    ap.add_argument("--headless", action="store_true", help="off-screen headed Chrome (invisible; dola passes)")
    ap.add_argument("--no-watermark", action="store_true", help="skip the ffmpeg watermark-removal step")
    ap.add_argument("--list", action="store_true", help="list available accounts and exit")
    args = ap.parse_args()
    if args.list:
        a = _accounts()
        print("accounts:", a or "(none)")
        return
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
