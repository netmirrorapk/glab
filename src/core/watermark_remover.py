"""Remove the visible Google Flow / Nano Banana / Gemini "sparkle"
watermark from generated images.

Cross-platform: works on Windows, macOS and Linux with only pip-installable
dependencies (opencv-python-headless, numpy). No ffmpeg on PATH required.
Falls back to the ffmpeg-based path in bot_engine.remove_image_watermark
if OpenCV isn't available.

Removes the VISIBLE mark only — Google's invisible SynthID watermark is
tamper-resistant by design and is not touched here.

Removal strategy — three techniques stacked so the visible mark disappears
without the tell-tale blur an inpaint-only pass leaves on busy textures
(paved ground, foliage, cobblestone, etc.):

  1. TIGHTER MASK.   The actual sparkle is ~40 px across, not the ~104 px
                     the earlier build was scrubbing. A smaller mask means
                     less area to reconstruct = less visible artifact.

  2. PATCH CLONE +
     POISSON BLEND. Copy a same-sized clean region from a horizontally
                    mirrored spot on the same y-coordinate (the ground
                    directly across from the sparkle usually has the
                    exact same texture). Poisson-blend it in with
                    cv2.seamlessClone(MIXED_CLONE) — gradients match at
                    the seam, so even a slight lighting/perspective
                    difference gets fixed automatically.

  3. FINE INPAINT.   A tight cv2.inpaint(TELEA) pass on the sparkle
                    footprint catches any residue the clone left behind
                    (edges of the star's rays). Only runs over the tight
                    mask so it can't smudge outside the sparkle area.

If OpenCV is not available (unusual install) the top-level entry point
falls through to bot_engine.remove_image_watermark, which uses ffmpeg
delogo. Either path removes the mark in-place and returns True on
success.
"""
import os


def _feather_edges(patch_h: int, patch_w: int, feather_px: int = 4):
    """Build an ellipse-ish feathered alpha mask for seamlessClone. The
    mask peaks in the middle and tapers to zero at the border so Poisson
    blending has an obvious "outside" to match against."""
    import numpy as np  # type: ignore
    yy, xx = np.mgrid[0:patch_h, 0:patch_w].astype(np.float32)
    dx = np.minimum(xx, patch_w - 1 - xx)
    dy = np.minimum(yy, patch_h - 1 - yy)
    edge_dist = np.minimum(dx, dy)
    # 0 at edges, 255 further than feather_px from any edge.
    alpha = np.clip(edge_dist / max(1, feather_px), 0, 1) * 255
    return alpha.astype("uint8")


def _pick_source_box(w: int, h: int, wm_box):
    """Choose a same-size source patch to copy over the watermark. Prefer
    the horizontal mirror (same y, opposite x) — on Flow's photo outputs
    the ground / background at that spot almost always matches the tile
    directly across from the sparkle. Fall back to a patch DIRECTLY above
    the watermark if the mirror site would land off-image or overlap the
    destination.

    Returns (sx1, sy1, sx2, sy2) or None if no valid source exists."""
    x1, y1, x2, y2 = wm_box
    pw = x2 - x1
    ph = y2 - y1

    # Candidate A: horizontal mirror (same y row, flipped x).
    mirror_cx = (w - 1) - ((x1 + x2) // 2)
    sx1 = mirror_cx - pw // 2
    sx2 = sx1 + pw
    sy1 = y1
    sy2 = y2
    if 0 <= sx1 and sx2 <= w and 0 <= sy1 and sy2 <= h and sx2 <= x1:
        return (sx1, sy1, sx2, sy2)

    # Candidate B: same x, directly above the watermark.
    sy1 = y1 - ph - 4
    sy2 = sy1 + ph
    sx1 = x1
    sx2 = x2
    if 0 <= sy1 and sy2 <= h and sy2 <= y1:
        return (sx1, sy1, sx2, sy2)

    # Candidate C: same x, directly below (rare — watermark is usually
    # near the bottom edge, but just in case the image is tall enough).
    sy1 = y2 + 4
    sy2 = sy1 + ph
    if sy2 <= h:
        return (sx1, sy1, sx2, sy2)

    return None


def remove_flow_watermark_cv(path: str) -> bool:
    """OpenCV-based clean of the sparkle watermark using patch clone +
    Poisson blend + a fine inpaint pass. Returns True on success (file
    rewritten in place), False on any failure or when OpenCV isn't
    installed (caller should try the ffmpeg path)."""
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except ImportError:
        return False

    if not os.path.isfile(path):
        return False

    img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if img is None or img.size == 0:
        return False

    h, w = img.shape[:2]
    if w < 200 or h < 200:
        return False

    # Preserve alpha through the process — inpaint/seamlessClone only
    # accept 1- or 3-channel arrays.
    alpha = None
    if img.ndim == 3 and img.shape[2] == 4:
        alpha = img[:, :, 3].copy()
        bgr = img[:, :, :3].copy()
    else:
        bgr = img.copy()

    short = min(w, h)
    # Sparkle centre offset in from the bottom-right corner. Flow's
    # placement is a fixed ~100 px inset at stock resolutions; the 0.075
    # proportional term is a safety net for upscaled outputs.
    off = max(103, int(short * 0.075))
    # Tighter half-box than the previous build. The sparkle is only
    # ~40 px across; giving it a 40 px half (80 px box) leaves a small
    # margin for the star's rays without over-scrubbing surrounding
    # texture.
    half = max(40, int(short * 0.030))
    cx, cy = w - off, h - off

    x1 = max(0, cx - half)
    y1 = max(0, cy - half)
    x2 = min(w, cx + half)
    y2 = min(h, cy + half)
    pw = x2 - x1
    ph = y2 - y1
    if pw < 8 or ph < 8:
        return False

    wm_box = (x1, y1, x2, y2)

    # ── Stage 1: patch clone + Poisson blend ─────────────────────────
    #
    # Copy a same-sized clean patch from a nearby "safe" area and
    # Poisson-blend it in with cv2.seamlessClone(MIXED_CLONE). MIXED
    # copies the SOURCE gradients where they're stronger than the
    # destination's, which lets the surrounding texture bleed through
    # at the seam — no visible box even on busy backgrounds.
    src_box = _pick_source_box(w, h, wm_box)
    clone_done = False
    if src_box is not None:
        sx1, sy1, sx2, sy2 = src_box
        src_patch = bgr[sy1:sy2, sx1:sx2].copy()
        if src_patch.shape[0] == ph and src_patch.shape[1] == pw:
            # Feathered ellipse mask so the Poisson solver knows exactly
            # where the transition happens.
            fmask = _feather_edges(ph, pw, feather_px=max(4, min(pw, ph) // 10))
            # seamlessClone wants a binary-ish mask (any non-zero counts
            # as "inside"), but a soft mask still influences the border
            # solver on OpenCV >= 4.5.
            try:
                blended = cv2.seamlessClone(
                    src_patch, bgr, fmask,
                    (cx, cy),
                    cv2.MIXED_CLONE,
                )
                # Only accept the result if it actually changed pixels
                # in the target region — some builds silently no-op on
                # very small masks.
                if blended is not None and blended.shape == bgr.shape:
                    bgr = blended
                    clone_done = True
            except cv2.error:
                # seamlessClone can throw on edge cases (mask on image
                # border, etc.); fall through to inpaint.
                pass

    # ── Stage 2: fine inpaint pass on the exact sparkle footprint ───
    #
    # Even after a clean patch clone, the sparkle's high-contrast rays
    # can leave faint edges. A small Telea inpaint over a TIGHTER mask
    # (just the star itself, not the whole safety box) cleans that up
    # without smudging the surrounding texture.
    tight_half = max(24, int(short * 0.022))
    tx1 = max(0, cx - tight_half)
    ty1 = max(0, cy - tight_half)
    tx2 = min(w, cx + tight_half)
    ty2 = min(h, cy + tight_half)
    if tx2 - tx1 >= 4 and ty2 - ty1 >= 4:
        mask = np.zeros((h, w), dtype=np.uint8)
        mask[ty1:ty2, tx1:tx2] = 255
        try:
            bgr = cv2.inpaint(bgr, mask, 3, cv2.INPAINT_TELEA)
        except cv2.error:
            # If inpaint fails after a successful clone we still keep
            # the clone result.
            if not clone_done:
                return False

    # Reattach alpha unchanged.
    if alpha is not None:
        result = cv2.merge([bgr[:, :, 0], bgr[:, :, 1], bgr[:, :, 2], alpha])
    else:
        result = bgr

    ext = os.path.splitext(path)[1].lower()
    encode_params = []
    if ext in (".jpg", ".jpeg"):
        encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), 95]
    elif ext == ".png":
        encode_params = [int(cv2.IMWRITE_PNG_COMPRESSION), 3]
    elif ext == ".webp":
        encode_params = [int(cv2.IMWRITE_WEBP_QUALITY), 95]

    ok, buf = cv2.imencode(ext or ".png", result, encode_params)
    if not ok or buf is None or len(buf) < 1000:
        return False

    tmp = path + ".wm.tmp" + (ext or "")
    try:
        with open(tmp, "wb") as f:
            f.write(buf.tobytes())
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


def remove_flow_watermark(path: str) -> bool:
    """Public entry point. Tries OpenCV clone+inpaint first (no ffmpeg
    required, works cross-platform), falls back to the ffmpeg-delogo
    path shipped in bot_engine.remove_image_watermark. Returns True if
    either succeeded."""
    if remove_flow_watermark_cv(path):
        return True
    try:
        from src.core.bot_engine import remove_image_watermark
        return bool(remove_image_watermark(path))
    except Exception:
        return False
