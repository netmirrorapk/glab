"""Verify prompts go to Flow correctly, end-to-end.

For each test prompt: resolve @tags (real Reference Library) -> generate_image
-> capture the actual ogiZ0b payload sent to Flow -> reconstruct a human-
readable "prompt-as-sent" showing exactly where each reference image lands.

REQUIRES the G-Labs extension on a flow.google.com PROJECT tab + the desktop
app NOT running (so this harness binds bridge port 18924).
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
from src.core.reference_resolver import resolve_prompt_references


def log(m):
    print(m, flush=True)


TEST_PROMPTS = [
    "@kai sprinting through the burning @market carrying an injured @rin in his arms, "
    "embers everywhere, dynamic motion, anime action panel, cinematic 16:9 widescreen, "
    "dramatic lighting, no text",
    "@kai and @zek locked in a mid-air clash, blades crossed with an explosion of energy "
    "between them, epic, dynamic angle, anime action panel, cinematic 16:9 widescreen, "
    "dramatic lighting, no text",
]


def reconstruct(slot8):
    """Turn the ogiZ0b prompt slot [8] into a readable 'prompt-as-sent'."""
    parts = slot8[0] if (isinstance(slot8, list) and slot8) else []
    out = []
    for seg in parts:
        if isinstance(seg, list) and len(seg) == 1 and isinstance(seg[0], str):
            out.append(seg[0])
        elif isinstance(seg, list) and len(seg) == 2 and seg[0] is None:
            try:
                fname = seg[1][0][1]
            except Exception:
                fname = "?"
            out.append(f"[IMG:{fname}]")
    return "".join(out)


async def main():
    bridge = ExtensionBridge(log)
    await bridge.start()
    log("Bridge up. Waiting for extension (flow.google.com PROJECT tab)…")
    accounts = {}
    t0 = time.time()
    while not accounts:
        if time.time() - t0 > 90:
            log("TIMEOUT: extension not connected.")
            return
        await asyncio.sleep(2)
        accounts = bridge.get_connected_accounts()
    email = list(accounts.keys())[0]
    log(f"✓ account: {email}\n")
    await asyncio.sleep(2)
    worker = ExtensionWorker("chk#e1", email, bridge, log)

    orig_be = bridge.request_batchexecute
    captured = []

    async def wrapped(*a, **k):
        if k.get("rpc_id") == "ogiZ0b":
            captured.append(k.get("payload_template"))
        return await orig_be(*a, **k)

    bridge.request_batchexecute = wrapped

    for idx, raw in enumerate(TEST_PROMPTS, 1):
        log("=" * 70)
        log(f"PROMPT {idx} (as typed):\n  {raw}")
        resolved = resolve_prompt_references(raw)
        log(f"\nResolver output:")
        log(f"  clean_prompt : {resolved['clean_prompt']}")
        log(f"  ref_paths    : {[os.path.basename(p) for p in resolved['ref_paths']]}")
        log(f"  missing_tags : {resolved['missing_tags']}")

        captured.clear()
        result, error = await worker.generate_image(
            resolved["clean_prompt"], "Nano Banana", "16:9",
            ref_paths=resolved["ref_paths"] or None,
            prompt_segments=resolved.get("segments"),
        )

        if captured:
            payload = json.loads(captured[-1])
            slot8 = payload[1][0][8]
            ref_slot = payload[1][0][2]
            log("\n>>> WHAT FLOW ACTUALLY RECEIVED <<<")
            log(f"  prompt-as-sent : {reconstruct(slot8)}")
            log(f"  reference count in [2]: "
                f"{len(ref_slot) if isinstance(ref_slot, list) else 0}")
        else:
            log("  (no ogiZ0b payload captured)")

        log(f"  generation: {'FAILED — ' + str(error)[:150] if error else 'SUCCESS ✓'}")
        log("")

    try:
        await bridge.stop()
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(main())
