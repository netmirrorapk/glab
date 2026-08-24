"""
LIVE end-to-end test on ONE GUI account (production path): login → generate a video →
prove the download is the DIRECT high-quality UNWATERMARKED master (the qAAB fallback_api
master, ~20-30 MB, NOT an ffmpeg-delogo'd stream) → optionally burn-recreate → regenerate.

    # one 30s Seedance-2.5 video, report HD/size/duration
    python tools/dola_generate_test.py --account megagaurienterprises --prompt "a cat walking in a sunny room" --model seedance_v2.5 --duration 30

    # ALSO test the loop: generate → burn-recreate → regenerate
    python tools/dola_generate_test.py --account megagaurienterprises --prompt "a cat walking" --duration 30 --burn
"""
import argparse, asyncio, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8"); sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from src.db.db_manager import get_accounts
from src.core.dola_playwright_mode import _cookies_for, _launch_cloak, _proxy_dict, _save_cookies
from src.core.dola_api import (DolaSession, DOLA_ORIGIN, DolaError, DailyLimitReached,
                               GenerationRefused, HighDemand, NotLoggedIn)

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "outputs", "dola_generate_test")


def log(*a):
    print("[gen-test]", *a, flush=True)


def _probe_duration(path):
    try:
        import subprocess
        from src.core.ffmpeg_path import ffprobe_exe
        out = subprocess.run([ffprobe_exe(), "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", path], capture_output=True, text=True, timeout=20)
        return out.stdout.strip()
    except Exception:
        return "?"


async def _login(session, session_path):
    if await session.confirm_logged_in():
        return True
    log("logging in (cloak)…")
    return await session.login_via_google(timeout=120)


async def _generate(session, prompt, model, ratio, duration, tag=""):
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, f"gen_{tag}{int(time.time())}.mp4")
    t0 = time.time()
    log(f"generating {tag}(model={model}, {duration}s, ratio={ratio}): {prompt[:50]}…")
    await session.generate_one(prompt, out, model=model, ratio=ratio, duration=duration,
                               timeout=900, use_skill=False)
    dt = int(time.time() - t0)
    n = os.path.getsize(out) if os.path.exists(out) else 0
    mb = n / 1024 / 1024
    hd = getattr(session, "last_was_hd", False)
    dur = _probe_duration(out)
    print("-" * 78)
    log(f"SAVED in {dt}s → {mb:.1f} MB ({n} bytes)")
    log(f"  UNWATERMARKED HD master (direct, no ffmpeg): {'YES ✅' if hd else 'NO — fell back to watermarked stream'}")
    log(f"  actual duration: {dur}s (asked {duration}s) | file: {out}")
    ok_quality = hd and mb >= 8
    log(f"  → {'✅ high-quality clean master' if ok_quality else '⚠ not the big clean master (check model/account tier)'}")
    print("-" * 78)
    return out, mb, hd, dur


async def run(args):
    acc = next((a for a in get_accounts() if args.account.lower() in (a["name"] or "").lower()), None)
    if not acc:
        print("account not found. available:", [a["name"] for a in get_accounts()]); return
    session_path = acc["session_path"]
    proxy = _proxy_dict(acc["proxy"]) if acc.get("proxy") else None
    print("=" * 78)
    print(f"account : {acc['name']}  | model={args.model} duration={args.duration}s burn={args.burn}")
    print("=" * 78)

    p = ctx = None
    try:
        cookies = await _cookies_for(session_path, log=log)
        p, ctx = await _launch_cloak(cookies, proxy, False, log=log)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        session = DolaSession(ctx, page, logger=log)
        await page.goto(f"{DOLA_ORIGIN}/chat/create-video", wait_until="domcontentloaded")
        if not await _login(session, session_path):
            log("login failed — aborting"); return
        await session._ensure_base()
        await _save_cookies(session_path, ctx)
        log(f"LOGGED IN — user_id={(await session.account_info()).get('user_id')}")

        # 1) first generation
        try:
            await _generate(session, args.prompt, args.model, args.ratio, args.duration, tag="1_")
        except DailyLimitReached:
            log("⚠ account already out of credits on the FIRST gen — going straight to burn-recreate")
            args.burn = True

        if not args.burn:
            print("\n[gen-test] DONE (no burn requested).")
            return

        # 2) burn-recreate (simulate the credit-0 → delete → relogin flow)
        log("BURN-RECREATE: delete → recreate (cloak) …")
        ok, detail = await session.delete_account(timeout=90, log=log)
        log(f"delete: ok={ok} {detail}")
        if not ok:
            log("delete failed — stopping"); return
        session._base = {}
        await session.clear_dola_cookies()
        if not await session.login_via_google(timeout=120):
            log("recreate login failed — stopping"); return
        await session._ensure_base()
        await _save_cookies(session_path, ctx)
        new_uid = (await session.account_info()).get("user_id")
        log(f"RECREATED — fresh user_id={new_uid}")

        # 3) regenerate on the fresh account
        await _generate(session, args.prompt, args.model, args.ratio, args.duration, tag="2_afterburn_")
        print("\n[gen-test] ✅ DONE — generate → burn-recreate → regenerate all worked.")

    except HighDemand as e:
        log("HIGH DEMAND (transient):", str(e)[:120])
    except GenerationRefused as e:
        log("prompt refused:", str(e)[:120])
    except NotLoggedIn as e:
        log("logged out mid-gen:", str(e)[:120])
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
    ap.add_argument("--account", required=True)
    ap.add_argument("--prompt", default="a cute cat walking gracefully across a sunny room")
    ap.add_argument("--model", default="seedance_v2.5", help="seedance_v2.5 / seedance_v2.0 / ic_mini")
    ap.add_argument("--ratio", default="9:16")
    ap.add_argument("--duration", type=int, default=30)
    ap.add_argument("--burn", action="store_true", help="also test burn-recreate + regenerate")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
