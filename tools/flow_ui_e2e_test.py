"""End-to-end test for the UI-drive image generation path (with pipelining).

Starts a standalone ExtensionBridge on port 18924, waits for the (reloaded)
G-Labs Studio Helper extension in Chrome to connect, then fires N request_flow_ui
calls CONCURRENTLY. If pipelining works, all N submit into ONE Flow tab
back-to-back and generate in parallel on Flow's side, so total time is close to a
single generation (not N times it).

Run with the desktop app CLOSED (so the port is free) and a flow.google.com
project tab open in Chrome. Usage: python tools/flow_ui_e2e_test.py [N]
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.extension_bridge import ExtensionBridge  # noqa: E402

PROMPTS = [
    "a lone lighthouse on a rocky cliff at golden hour, dramatic clouds, cinematic wide shot",
    "a red vintage bicycle leaning on a blue wall, sunny street, sharp detail",
    "a bowl of ramen with steam rising, dark wooden table, top-down, cozy",
]
MODEL = "Nano Banana 2"
ASPECT = "16:9"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 2   # how many to run in parallel


async def _one(bridge, account, prompt, idx):
    t0 = time.time()
    print(f"[test #{idx}] submit: {prompt[:50]}...")
    res = await bridge.request_flow_ui(
        account=account, prompt=prompt, model=MODEL, aspect=ASPECT,
        source_path="/", timeout=240,
    )
    dt = time.time() - t0
    ok = bool(res.get("ok")) and bool(res.get("cdn_url"))
    print(f"[test #{idx}] {'PASS' if ok else 'FAIL'} after {dt:.1f}s -- "
          f"media_id={res.get('media_id')} error={res.get('error')}")
    return ok


async def main():
    bridge = ExtensionBridge(log_fn=lambda m: print(m))
    await bridge.start()
    print("\n[test] Bridge up on 127.0.0.1:18924 -- waiting for the extension to connect...")

    account = ""
    for _ in range(60):  # up to 30s
        accts = bridge.get_connected_accounts()
        if accts:
            account = next(iter(accts))
            print(f"[test] Extension connected. Accounts: {list(accts)}  -> using: {account!r}")
            break
        await asyncio.sleep(0.5)
    else:
        print("[test] No extension connected. Is Chrome open, extension reloaded (v2.8.3+), "
              "and bridge port free? Firing anyway with account='' ...")

    n = max(1, min(N, len(PROMPTS)))
    print(f"\n[test] Firing {n} request_flow_ui CONCURRENTLY (pipeline test) "
          f"(model={MODEL}, aspect={ASPECT})")
    t0 = time.time()
    results = await asyncio.gather(*[_one(bridge, account, PROMPTS[i], i + 1) for i in range(n)])
    dt = time.time() - t0
    passed = sum(1 for r in results if r)
    print(f"\n[test] ===== {passed}/{n} PASSED in {dt:.1f}s total =====")
    print("[test] (If pipelined, total time ~= one generation, not n x single-time.)")

    await bridge.stop()


if __name__ == "__main__":
    asyncio.run(main())
