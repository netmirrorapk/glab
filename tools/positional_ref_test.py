"""End-to-end test for POSITIONAL inline reference markers.

Exercises the real generation path: ExtensionBridge -> Chrome extension ->
flow.google.com. Uploads a reference image (maseQ), builds the ogiZ0b request
with a POSITIONAL prompt slot [8] (text + inline reference marker), sends it,
and verifies both the on-wire structure and that Flow returns an image.

REQUIRES: the G-Labs Studio Helper extension active on a flow.google.com
PROJECT tab in Chrome, and the desktop app NOT running (so this harness can
bind bridge port 18924 and the extension connects here).

Run:  python tools/positional_ref_test.py
"""

import asyncio
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.extension_bridge import ExtensionBridge
from src.core.extension_mode import ExtensionWorker


def log(msg):
    print(msg, flush=True)


async def main():
    bridge = ExtensionBridge(log)
    await bridge.start()
    log("Bridge started. Waiting for the extension (open a flow.google.com "
        "PROJECT tab in Chrome)…")

    accounts = {}
    t0 = time.time()
    while not accounts:
        if time.time() - t0 > 90:
            log("TIMEOUT: no account from extension in 90s. Is the G-Labs "
                "extension active on a Flow tab?")
            return
        await asyncio.sleep(2)
        accounts = bridge.get_connected_accounts()

    email = list(accounts.keys())[0]
    log(f"✓ Connected account: {email}")
    await asyncio.sleep(2)  # let the project id report in

    worker = ExtensionWorker("postest#e1", email, bridge, log)

    # Instrument: capture the ogiZ0b payload the app actually sends.
    orig_be = bridge.request_batchexecute
    captured = []

    async def wrapped_be(*args, **kwargs):
        if kwargs.get("rpc_id") == "ogiZ0b":
            captured.append(kwargs.get("payload_template"))
        return await orig_be(*args, **kwargs)

    bridge.request_batchexecute = wrapped_be

    ref = os.path.abspath(os.path.join("data", "refs", "kai.jpg"))
    log(f"Reference image: {ref}  exists={os.path.exists(ref)}")

    tail = (" is standing in front of a grand ancient castle at sunset, "
            "anime art style, cinematic 16:9, dramatic lighting, no text")
    segments = [
        {"type": "text", "text": "The main character "},
        {"type": "ref", "path": ref, "name": "kai.jpg"},
        {"type": "text", "text": tail},
    ]
    clean = "The main character" + tail

    log("\nGenerating with POSITIONAL inline reference…")
    result, error = await worker.generate_image(
        clean, "Nano Banana", "16:9",
        ref_paths=[ref], prompt_segments=segments,
    )

    # ── Verify the on-wire prompt slot ─────────────────────────────
    log("\n================ WIRE VERIFICATION ================")
    if captured:
        payload = json.loads(captured[-1])
        slot8 = payload[1][0][8]
        ref_slot = payload[1][0][2]
        segs_out = slot8[0] if (isinstance(slot8, list) and slot8) else []
        is_positional = any(
            isinstance(s, list) and len(s) == 2 and s[0] is None
            for s in segs_out
        )
        # redact media_id/filename lengths but show the shape
        def _shape(o):
            if isinstance(o, str):
                return o if len(o) <= 40 else f"<str:{len(o)}>"
            if isinstance(o, list):
                return [_shape(x) for x in o]
            return o
        log("prompt slot [8] structure:")
        log("  " + json.dumps(_shape(slot8))[:500])
        log(f"POSITIONAL inline reference present in [8]? {is_positional}")
        log(f"ref_slot [2] media count: "
            f"{len(ref_slot) if isinstance(ref_slot, list) else 0}")
    else:
        log("No ogiZ0b payload captured (upload/generation failed earlier).")

    # ── Result ─────────────────────────────────────────────────────
    log("\n================ RESULT ================")
    if error:
        log(f"FAILED: {str(error)[:400]}")
    else:
        if isinstance(result, dict):
            keys = list(result.keys())
            url = result.get("url") or result.get("image_url") or ""
            log(f"SUCCESS. result keys: {keys}")
            log(f"  image url received: {bool(url)}")
        else:
            log(f"SUCCESS. result: {str(result)[:120]}")

    try:
        await bridge.stop()
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(main())
