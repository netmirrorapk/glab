"""Remove the visible Google Flow / Nano Banana / Gemini "sparkle" watermark
from generated images.

Cross-platform: works on Windows, macOS and Linux with only pip-installable
dependencies (opencv-python-headless, numpy). No ffmpeg on PATH required.
Falls back to the ffmpeg-based path in bot_engine.remove_image_watermark
if OpenCV isn't available, so upgrade partial installs still function.

Removes the VISIBLE mark only — Google's invisible SynthID watermark is
tamper-resistant by design and is not touched here.

The sparkle sits a FIXED ~100 px in from the bottom-right corner at
Flow's stock resolutions (768x1376, 896x1200, 1200x896). We compute the
mask from that fixed pixel inset with a proportional safety net for
larger (upscaled) outputs, then inpaint the region using the Telea
algorithm which reconstructs the covered pixels from neighbouring
content — no smear, no visible seam on typical natural backgrounds.
"""
import os


def remove_flow_watermark_cv(path: str) -> bool:
    """OpenCV-based inpaint of the sparkle watermark. Returns True on
    success (file was rewritten in place), False on any failure or
    when OpenCV isn't installed (caller should try the ffmpeg path)."""
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except ImportError:
        return False

    if not os.path.isfile(path):
        return False

    # cv2.imread returns None on any decode failure (unsupported format,
    # truncated file, etc.). Explicit check keeps the caller from
    # accidentally writing a zero-byte "cleaned" file.
    img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if img is None or img.size == 0:
        return False

    h, w = img.shape[:2]
    if w < 200 or h < 200:
        # Too small to safely mask — leave alone.
        return False

    short = min(w, h)
    # Sparkle centre offset in FIXED pixels from the bottom-right corner —
    # this matches Google's placement at all stock resolutions. The 0.075
    # proportional term only kicks in when the shorter side is unusually
    # large (upscaled or oversized outputs), giving the mask a safety net.
    off = max(103, int(short * 0.075))
    half = max(52, int(short * 0.038))
    cx, cy = w - off, h - off

    # Clamp the mask box to stay strictly inside the image bounds.
    x1 = max(0, cx - half)
    y1 = max(0, cy - half)
    x2 = min(w, cx + half)
    y2 = min(h, cy + half)
    if x2 - x1 < 4 or y2 - y1 < 4:
        return False

    mask = np.zeros((h, w), dtype=np.uint8)
    mask[y1:y2, x1:x2] = 255

    # If the input carries an alpha channel drop it before inpainting —
    # cv2.inpaint only accepts 1- or 3-channel images. Re-attach it
    # (unchanged) after so PNG transparency is preserved.
    alpha = None
    if img.ndim == 3 and img.shape[2] == 4:
        alpha = img[:, :, 3].copy()
        bgr = img[:, :, :3]
    else:
        bgr = img

    # Telea inpainting with a 3-pixel neighbourhood radius. Fast and
    # produces cleaner results than Navier-Stokes on the small, sharp-
    # edged sparkle icon.
    result = cv2.inpaint(bgr, mask, 3, cv2.INPAINT_TELEA)
    if alpha is not None:
        # Merge alpha back in — same alpha as the input (the sparkle
        # region was opaque in the source anyway).
        result = cv2.merge([result[:, :, 0], result[:, :, 1], result[:, :, 2], alpha])

    ext = os.path.splitext(path)[1].lower()
    encode_params = []
    if ext in (".jpg", ".jpeg"):
        # High quality to avoid re-compression artifacts around the fix.
        encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), 95]
    elif ext == ".png":
        encode_params = [int(cv2.IMWRITE_PNG_COMPRESSION), 3]
    elif ext == ".webp":
        encode_params = [int(cv2.IMWRITE_WEBP_QUALITY), 95]

    # Encode to bytes first, then atomic-replace. cv2.imwrite can silently
    # truncate on write errors (network shares, disk-full); encode-first
    # gives us a full-buffer check before we touch the original file.
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
    """Public entry point. Tries OpenCV first (no ffmpeg required, works
    cross-platform), falls back to the ffmpeg-delogo path shipped in
    bot_engine.remove_image_watermark. Returns True if either succeeded."""
    if remove_flow_watermark_cv(path):
        return True
    try:
        # Import lazily — bot_engine has heavy Playwright imports we don't
        # want to pay for at watermark-removal time if OpenCV was enough.
        from src.core.bot_engine import remove_image_watermark
        return bool(remove_image_watermark(path))
    except Exception:
        return False
