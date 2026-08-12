"""Resolve the ffmpeg / ffprobe executables to ABSOLUTE paths.

Why this exists: on macOS (and sometimes Linux) a GUI-launched app — double-clicked,
started from the Dock, or run as a bundled .app — does NOT inherit the interactive
shell's PATH. Homebrew installs ffmpeg to /opt/homebrew/bin (Apple-Silicon) or
/usr/local/bin (Intel), neither of which is on a GUI process's default PATH. So a bare
subprocess call to "ffmpeg" fails with FileNotFoundError even though `ffmpeg -version`
works fine in Terminal — and the watermark step then silently no-ops.

Resolving to an absolute path (checking the common install locations, plus the bundled
imageio-ffmpeg binary as a last resort) makes ffmpeg work regardless of how the app was
launched. Falls back to the bare name so an environment that DOES have it on PATH still
works unchanged.
"""
import os
import shutil

_CACHE = {}

# Common absolute locations, in priority order (macOS Homebrew, MacPorts, Linux, etc.)
_COMMON_DIRS = (
    "/opt/homebrew/bin",   # macOS, Apple Silicon Homebrew
    "/usr/local/bin",      # macOS Intel Homebrew / manual installs
    "/opt/local/bin",      # MacPorts
    "/usr/bin",
    "/bin",
    "/snap/bin",
)


def resolve(tool):
    """Return an absolute path to `tool` ("ffmpeg"/"ffprobe"), or the bare name if it
    genuinely can't be located (so a working PATH still succeeds). Result is cached."""
    if tool in _CACHE:
        return _CACHE[tool]

    # 1) Honour PATH if the GUI process happens to have it.
    found = shutil.which(tool)

    # 2) Probe the usual absolute install locations.
    if not found:
        names = [tool + ".exe", tool] if os.name == "nt" else [tool]
        for d in _COMMON_DIRS:
            for n in names:
                cand = os.path.join(d, n)
                if os.path.isfile(cand) and os.access(cand, os.X_OK):
                    found = cand
                    break
            if found:
                break

    # 3) Last resort: the ffmpeg binary bundled with the imageio-ffmpeg wheel, if present.
    if not found and tool == "ffmpeg":
        try:
            import imageio_ffmpeg
            found = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pass

    found = found or tool
    _CACHE[tool] = found
    return found


def ffmpeg_exe():
    return resolve("ffmpeg")


def ffprobe_exe():
    return resolve("ffprobe")
