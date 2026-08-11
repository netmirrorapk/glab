"""
Watch a folder and remove the Nano Banana / Gemini bottom-right 'sparkle' (✦)
watermark from every image as it appears (in-place, ffmpeg delogo). Self-contained
— use it as a STOPGAP while generation runs on an app build that doesn't yet
auto-remove the watermark. Ctrl-C to stop. Safe: skips files still being written
and never re-processes an unchanged file.

    python tools/dewatermark_watch.py "C:\\Users\\PC\\Downloads\\Video US"

Needs ffmpeg on PATH.
"""
import os
import sys
import time
import glob
import subprocess

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

EXTS = ("*.jpg", "*.jpeg", "*.png", "*.webp")


def _size(path):
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", path],
            capture_output=True, text=True, timeout=20).stdout.strip()
        w, h = out.split("x")[:2]
        return int(w), int(h)
    except Exception:
        return None


def dewatermark(path):
    """Remove the fixed bottom-right sparkle (~112px in from the corner)."""
    sz = _size(path)
    if not sz:
        return False
    w, h = sz
    if w < 200 or h < 200:
        return False
    short = min(w, h)
    off = max(112, int(short * 0.06))
    half = max(70, int(short * 0.055))
    cx, cy = w - off, h - off
    bx = max(1, cx - half)
    by = max(1, cy - half)
    bw = max(2, min(2 * half, w - bx - 2))
    bh = max(2, min(2 * half, h - by - 2))
    tmp = path + ".nw" + (os.path.splitext(path)[1] or ".png")
    try:
        rc = subprocess.run(
            ["ffmpeg", "-y", "-i", path,
             "-vf", f"delogo=x={bx}:y={by}:w={bw}:h={bh}", tmp],
            capture_output=True, timeout=60).returncode
        if rc == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 1000:
            os.replace(tmp, path)
            return True
    except Exception:
        pass
    try:
        if os.path.isfile(tmp):
            os.remove(tmp)
    except Exception:
        pass
    return False


def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else "."
    if not os.path.isdir(folder):
        print("folder not found:", folder)
        return
    done = {}   # path -> mtime AFTER we processed it
    cleaned = 0
    print(f"[watch] watching {folder} — removing image watermarks (Ctrl-C to stop)", flush=True)
    while True:
        files = []
        for e in EXTS:
            files += glob.glob(os.path.join(folder, e))
        for f in sorted(files):
            try:
                mt = os.path.getmtime(f)
            except OSError:
                continue
            if done.get(f) == mt:
                continue                       # already cleaned at this version
            try:
                s1 = os.path.getsize(f)
            except OSError:
                continue
            time.sleep(0.6)                    # let a mid-write file settle
            try:
                if os.path.getsize(f) != s1:
                    continue                   # still being written — next pass
            except OSError:
                continue
            ok = dewatermark(f)
            try:
                done[f] = os.path.getmtime(f)  # record cleaned version so we skip it
            except OSError:
                pass
            if ok:
                cleaned += 1
                print(f"[watch] cleaned {os.path.basename(f)}  (total {cleaned})", flush=True)
        time.sleep(3)


if __name__ == "__main__":
    main()
