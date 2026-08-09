"""
Remove the dola.com "Dola AI" watermark (bottom-right corner) from generated
videos using ffmpeg's delogo filter (interpolates the logo region from the
surrounding pixels — no visible mark left).

Usage:
    python tools/dola_dewatermark.py "C:\\Users\\PC\\Downloads\\Video US"
    python tools/dola_dewatermark.py "<folder>" --inplace     # overwrite originals
    python tools/dola_dewatermark.py "<one_video>.mp4"

By default, cleaned files go to a "no_watermark" subfolder; originals are kept.
Already-cleaned files are skipped, so you can re-run it as more videos arrive.

Needs ffmpeg on PATH (ffmpeg -version).
"""
import os
import subprocess
import sys
import json

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# Watermark box as FRACTIONS of the frame (measured on dola's 1280x720 output:
# "Dola AI" at x≈1110..1268, y≈655..707). Fractions so it scales to any 16:9 size.
WM = {"x": 0.867, "y": 0.910, "w": 0.123, "h": 0.075}


def ffprobe_size(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "json", path],
            capture_output=True, text=True).stdout
        s = json.loads(out)["streams"][0]
        return int(s["width"]), int(s["height"])
    except Exception:
        return None, None


def delogo_box(w, h):
    """Pixel box for the watermark. Landscape → bottom-right 'Dola AI'.
    (Portrait 9:16 output would need different coords; add if you use 9:16.)"""
    x = int(round(WM["x"] * w))
    y = int(round(WM["y"] * h))
    bw = int(round(WM["w"] * w))
    bh = int(round(WM["h"] * h))
    # delogo needs a 1px border inside the frame on every side
    x = max(1, min(x, w - 3))
    y = max(1, min(y, h - 3))
    bw = max(2, min(bw, w - x - 1))
    bh = max(2, min(bh, h - y - 1))
    return x, y, bw, bh


def clean_one(src, dst):
    w, h = ffprobe_size(src)
    if not w:
        print(f"  skip (not a video): {os.path.basename(src)}")
        return False
    x, y, bw, bh = delogo_box(w, h)
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", src,
         "-vf", f"delogo=x={x}:y={y}:w={bw}:h={bh}",
         "-c:a", "copy", dst],
        capture_output=True, text=True)
    ok = r.returncode == 0 and os.path.isfile(dst) and os.path.getsize(dst) > 50000
    print(f"  {'OK ' if ok else 'FAIL'} {os.path.basename(src)} "
          f"({w}x{h}, box {x},{y},{bw},{bh})")
    if not ok:
        print("     ", (r.stderr or "").strip().splitlines()[-1:] or "")
    return ok


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    target = sys.argv[1]
    inplace = "--inplace" in sys.argv[2:]

    if os.path.isfile(target):
        files = [target]
        base = os.path.dirname(os.path.abspath(target))
    elif os.path.isdir(target):
        base = target
        files = [os.path.join(base, f) for f in sorted(os.listdir(base))
                 if f.lower().endswith(".mp4")]
    else:
        print("Not found:", target)
        return

    out_dir = base if inplace else os.path.join(base, "no_watermark")
    done = 0
    for src in files:
        name = os.path.basename(src)
        if os.path.dirname(os.path.abspath(src)).endswith("no_watermark"):
            continue
        dst = src if inplace else os.path.join(out_dir, name)
        if not inplace and os.path.isfile(dst):
            continue  # already cleaned
        tmp = dst + ".tmp.mp4" if inplace else dst
        if clean_one(src, tmp):
            if inplace:
                os.replace(tmp, dst)
            done += 1
    print(f"\nDone. Cleaned {done} video(s) -> "
          f"{'in place' if inplace else out_dir}")


if __name__ == "__main__":
    main()
