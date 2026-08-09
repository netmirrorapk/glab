"""
Phase 1 — Playwright dola driver proof.

Launches ONE real Chrome profile (reusing its existing Google login) via a
persistent context, optionally through a proxy, and generates ONE dola.com video
end-to-end using the existing DolaSession (page.evaluate signing — same mechanism
the extension used, no crypto to replicate).

This validates the whole Playwright architecture: real-profile login reuse +
per-context proxy + in-page signed API. If this makes a video, the full migration
(multi-context launcher + dola_mode rewire) is straightforward.

USAGE (the chosen Chrome profile MUST be closed first — Chrome locks a profile to
one browser at a time):

    python tools/dola_playwright_test.py --profile "Profile 2"
    python tools/dola_playwright_test.py --profile "Profile 2" --proxy http://user:pass@host:port
    python tools/dola_playwright_test.py --profile "Profile 2" --prompt "a calm ocean at sunset" --ratio 16:9

Outputs the mp4 to ./outputs/pw_test.mp4
"""
import argparse
import asyncio
import os
import sys
import shutil
import tempfile

# make src importable when run from repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.async_api import async_playwright
from src.core.dola_api import DolaSession, DailyLimitReached, GenerationRefused, DolaError

CHROME_USER_DATA = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser(r"~\AppData\Local")),
    "Google", "Chrome", "User Data",
)
PROFILES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "dola_profiles")
DEDICATED = False   # set from --dedicated in main()

# Explicit real Google Chrome binary — NOT channel="chrome" (which can resolve to
# a different chromium-family browser if one hijacked the registration).
CHROME_EXE_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]


def find_chrome():
    for c in CHROME_EXE_CANDIDATES:
        if os.path.isfile(c):
            return c
    return None


def make_profile_copy(root: str, profile_dir: str) -> str:
    """Copy the login-relevant parts of a real Chrome profile into a fresh temp
    user-data-dir (the profile mapped to 'Default'). This lets Playwright own a
    private copy — so the user's daily Chrome can STAY OPEN (no singleton lock),
    and it's the same pattern production will use (dedicated per-account profiles).
    Cookies stay decryptable because we also copy root/Local State (the key) and
    launch the SAME chrome.exe as the SAME OS user."""
    tmp = tempfile.mkdtemp(prefix="dola_pw_profile_")
    # root-level Local State holds the (DPAPI/App-Bound) cookie-encryption key
    src_ls = os.path.join(root, "Local State")
    if os.path.isfile(src_ls):
        shutil.copy2(src_ls, os.path.join(tmp, "Local State"))
    src = os.path.join(root, profile_dir)
    dst = os.path.join(tmp, "Default")
    os.makedirs(dst, exist_ok=True)
    # Only the files that carry the Google + dola session — skip the huge caches.
    for rel in ["Network", "Local Storage", "Session Storage", "IndexedDB",
                "Cookies", "Cookies-journal", "Preferences", "Secure Preferences",
                "Login Data", "Login Data For Account", "Web Data", "Trust Tokens"]:
        s = os.path.join(src, rel)
        d = os.path.join(dst, rel)
        try:
            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True)
            elif os.path.isfile(s):
                shutil.copy2(s, d)
        except Exception:
            pass
    return tmp


try:
    sys.stdout.reconfigure(encoding="utf-8")   # Windows console is cp1252 by default
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


def log(*a):
    try:
        print("[pw]", *a, flush=True)
    except Exception:
        print("[pw]", *[str(x).encode("ascii", "replace").decode() for x in a], flush=True)


# Find a visible element whose text/aria matches one of `wants` and return its
# CENTER RECT (so Python can do a TRUSTED page.mouse.click — a real user gesture,
# which GSI requires to open its Google popup; a scripted el.click() is ignored).
_RECT_JS = r"""(wants) => {
  const norm = s => (s||'').replace(/\s+/g,' ').trim().toLowerCase();
  const vis = el => { if(!el||!el.isConnected) return false; const st=getComputedStyle(el);
    if(!st||st.display==='none'||st.visibility==='hidden'||st.opacity==='0') return false;
    const r=el.getBoundingClientRect(); return r.width>4&&r.height>4; };
  const btns = Array.from(document.querySelectorAll("button,[role='button'],div,span,a,img")).filter(vis);
  let el = btns.find(b => wants.includes(norm(b.textContent)));
  if(!el){ el = btns.find(b => { const a=((b.getAttribute&&(b.getAttribute('aria-label')||b.getAttribute('alt')))||'').toLowerCase();
    return a.indexOf('google')!==-1 && getComputedStyle(b).cursor==='pointer'; }); }
  if(!el) return null;
  const r = el.getBoundingClientRect();
  return {x: r.left + r.width/2, y: r.top + r.height/2,
          label:(el.textContent||'').replace(/\s+/g,' ').trim().slice(0,30)};
}"""


async def _trusted_click(page, wants):
    """Find an element by text and click it with a REAL mouse gesture."""
    rect = await page.evaluate(_RECT_JS, wants)
    if not rect:
        return None
    try:
        await page.mouse.click(rect["x"], rect["y"])
        return rect["label"]
    except Exception:
        return None

_LOGIN_WANTS = ["log in", "login", "sign in", "log in / sign up", "sign up / log in"]
_GOOGLE_WANTS = ["continue with google", "sign in with google", "log in with google"]


async def dola_login_via_google(ctx, page, session, gmail=None, timeout=90):
    """Drive dola's own 'Continue with Google' login (like the extension did),
    completing the Google popup against the profile's active session."""
    import asyncio as _a
    await page.goto("https://www.dola.com/chat/create-video", wait_until="domcontentloaded")
    await _a.sleep(2.5)
    if await session.is_logged_in():
        return True
    # 1) open the login modal (trusted click)
    lab = await _trusted_click(page, _LOGIN_WANTS)
    log("  clicked 'Log In':", lab)
    await _a.sleep(1.8)
    # 2) TRUSTED click 'Continue with Google' — a real gesture, so GSI opens its
    #    Google popup (a scripted el.click() is silently ignored → no popup).
    popup = None
    try:
        async with ctx.expect_page(timeout=12000) as pi:
            lab2 = await _trusted_click(page, _GOOGLE_WANTS)
            log("  clicked 'Continue with Google':", lab2)
        popup = await pi.value
        log("  google popup opened:", (popup.url or "")[:70])
    except Exception:
        log("  (no popup — maybe FedCM/inline; will still poll for session)")
    # 3) if a popup with the account chooser is up, pick our account
    if popup:
        try:
            await popup.wait_for_load_state("domcontentloaded")
            await _a.sleep(2)
            # click the account row (by email if we know it, else the first one)
            picked = await popup.evaluate(
                r"""(email) => {
                    const rows = Array.from(document.querySelectorAll("div[data-identifier], li, div[role='link'], div"))
                      .filter(e => { const t=(e.innerText||'').toLowerCase();
                        return t.indexOf('@')!==-1 && e.getBoundingClientRect().height>20 && e.getBoundingClientRect().height<120; });
                    let el = email ? rows.find(e => (e.innerText||'').toLowerCase().indexOf(email)!==-1) : null;
                    if(!el) el = rows[0];
                    if(el){ el.click(); return (el.innerText||'').slice(0,40); }
                    return null;
                }""", (gmail or "").lower())
            log("  popup account picked:", picked)
        except Exception as e:
            log("  popup handling note:", str(e)[:80])
    # 4) poll for the dola session, dismissing the age modal if it appears
    deadline = __import__("time").time() + timeout
    while __import__("time").time() < deadline:
        await _a.sleep(2)
        try:
            await _trusted_click(page, ["i am 18", "confirm", "i'm 18", "yes", "continue"])  # age modal
        except Exception:
            pass
        if await session.is_logged_in():
            return True
    return await session.is_logged_in()


async def run(profile: str, proxy: str | None, prompt: str, ratio: str,
              model: str, duration: int, out: str, headless: bool):
    # Chrome's User Data has ONE root; each account is a SUBFOLDER ("Profile 18").
    # Playwright must get the ROOT as user_data_dir and pick the profile via
    # --profile-directory — pointing user_data_dir straight at the subfolder makes
    # Chrome create a fresh empty "Default" (→ "not logged in"). If an absolute path
    # to a profile subfolder was given, split it into root + profile name.
    user_data_dir, profile_dir = CHROME_USER_DATA, profile
    if not DEDICATED:
        # resolving a REAL Chrome profile (to copy). Dedicated profiles are handled
        # below (they live in data/dola_profiles/<name>).
        if os.path.isabs(profile):
            user_data_dir = os.path.dirname(profile)
            profile_dir = os.path.basename(profile)
        if not os.path.isdir(os.path.join(user_data_dir, profile_dir)):
            log(f"ERROR: profile not found: {os.path.join(user_data_dir, profile_dir)}")
            return 2

    chrome_exe = find_chrome()
    if not chrome_exe:
        log("ERROR: real Google Chrome not found. Install it.")
        return 2
    log(f"chrome.exe: {chrome_exe}")

    if DEDICATED:
        # A dedicated automation profile (created by tools/dola_profiles.py) is
        # already Playwright-owned — use it DIRECTLY (no copy; copying would lose
        # its App-Bound-encrypted cookies again).
        work_dir = profile if os.path.isabs(profile) else os.path.join(PROFILES_DIR, profile)
        if not os.path.isdir(work_dir):
            log(f"ERROR: dedicated profile not found: {work_dir}")
            return 2
        log(f"using dedicated profile: {work_dir}")
    else:
        # Copy a REAL Chrome profile so Playwright owns a private user-data-dir →
        # the user's daily Chrome can stay open (no singleton/exitCode=21 lock).
        log(f"copying profile '{profile_dir}' login state (daily Chrome can stay open)...")
        work_dir = make_profile_copy(user_data_dir, profile_dir)
        log(f"profile copy: {work_dir}")

    launch_kwargs = dict(
        user_data_dir=work_dir,       # private COPY (mapped profile → Default)
        executable_path=chrome_exe,   # EXPLICIT real Chrome (no channel ambiguity)
        headless=headless,
        # Drop the automation flag + banner so Google/dola don't flag the browser
        # as "controlled by automated software".
        ignore_default_args=["--enable-automation"],
        args=[
            "--no-first-run", "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
        ],
        viewport={"width": 1280, "height": 800},
    )
    if proxy:
        # parse http://user:pass@host:port into Playwright's proxy dict
        from urllib.parse import urlparse
        u = urlparse(proxy)
        pd = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
        if u.username:
            pd["username"] = u.username
        if u.password:
            pd["password"] = u.password
        launch_kwargs["proxy"] = pd
        log(f"using proxy {pd['server']}")

    async with async_playwright() as p:
        log(f"launching real Chrome — root={user_data_dir} profile={profile_dir}")
        try:
            ctx = await p.chromium.launch_persistent_context(**launch_kwargs)
        except Exception as e:
            log(f"ERROR launching (is that Chrome profile still open? close it first): {e}")
            return 3

        # Stealth: hide navigator.webdriver + automation fingerprints so dola
        # (ByteDance — strong bot detection) initializes its app + fires the
        # signed API requests we need. Applied to the CONTEXT so every page/nav
        # gets it via an init script.
        try:
            from playwright_stealth import Stealth
            st = Stealth()
            payload = getattr(st, "script_payload", None)
            if callable(payload):
                payload = payload()
            if payload:
                await ctx.add_init_script(script=payload)
                log("stealth init-script applied")
            else:
                log("stealth: no script_payload; trying apply_stealth_async")
        except Exception as e:
            log("stealth setup failed (continuing):", e)

        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        session = DolaSession(ctx, page, logger=log)
        try:
            from playwright_stealth import Stealth
            await Stealth().apply_stealth_async(page)
        except Exception:
            pass

        # collect ALL dola.com request URLs for diagnostics
        seen_reqs = []
        page.on("request", lambda r: seen_reqs.append(r.url) if "dola.com/" in r.url else None)

        try:
            log("checking proxy IP (so you can confirm each account is on its own IP)...")
            try:
                ipr = await ctx.request.get("https://api.ipify.org?format=json")
                log("egress IP:", (await ipr.text()))
            except Exception as e:
                log("ip check skipped:", e)

            # First: is GOOGLE still logged in inside this copied profile? That's
            # the ONLY session we need stable — dola auto-logins off it (and we
            # delete+relogin dola anyway).
            gck = await ctx.cookies("https://accounts.google.com")
            gnames = {c["name"] for c in gck}
            google_in = bool(gnames & {"SAPISID", "__Secure-1PSID", "__Secure-3PSID", "SSID", "SID"})
            log(f"GOOGLE logged in (copied profile)? {google_in}  (google cookies: {len(gnames)})")

            log("opening dola.com — trying auto-login off the Google session...")
            await page.goto("https://www.dola.com/chat/create-video", wait_until="domcontentloaded")
            await asyncio.sleep(3)
            ok = await session.ensure_logged_in(timeout=12)
            if not ok:
                log("auto-login didn't fire — driving dola's 'Continue with Google' (extension-style)...")
                ok = await dola_login_via_google(ctx, page, session, gmail=None, timeout=90)
            if not ok:
                try:
                    cur = await page.evaluate("() => location.href")
                except Exception:
                    cur = "?"
                log(f"ERROR: dola not logged in. google_in={google_in}, page={cur}")
                if google_in:
                    log("  → Google IS logged in but dola didn't auto-login "
                        "(likely the rotating VPN IP — dola/Google reject the session from a new IP).")
                else:
                    log("  → Google login was NOT preserved in the copy → fix the profile copy / re-login Google.")
                return 4
            log("logged in ✓  capturing base params...")
            try:
                await session._ensure_base()
            except Exception as e:
                await asyncio.sleep(2)
                api = [u for u in seen_reqs if any(x in u for x in ("/alice", "/samantha", "/im/", "/chat/", "device_id"))]
                log(f"DEBUG total dola reqs={len(seen_reqs)}, api-ish={len(api)}")
                for u in api[:12]:
                    log("  req:", u[:160])
                if not api:
                    log("  (no API/signed requests fired — dola app JS likely not initializing)")
                    for u in seen_reqs[:12]:
                        log("  any:", u[:120])
                try:
                    st = await page.evaluate("() => ({url: location.href, title: document.title})")
                    log("  page:", st)
                except Exception:
                    pass
                raise
            log("base params captured ✓")

            log(f"generating: '{prompt}'  ratio={ratio} model={model}")
            os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
            await session.generate_one(prompt, out, model=model, ratio=ratio, duration=duration, timeout=720)
            size = os.path.getsize(out) if os.path.exists(out) else 0
            log(f"✅ DONE — saved {size} bytes -> {out}")
            return 0
        except DailyLimitReached:
            log("⛔ daily limit for this account — try another profile or after reset.")
            return 5
        except GenerationRefused:
            log("🚫 dola refused this prompt (content moderation). Try a different prompt.")
            return 6
        except DolaError as e:
            log(f"dola error: {e}")
            return 7
        finally:
            await asyncio.sleep(1)
            try:
                await ctx.close()
            except Exception:
                pass
            if not DEDICATED:   # keep dedicated profiles; only delete temp copies
                try:
                    shutil.rmtree(work_dir, ignore_errors=True)
                except Exception:
                    pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True, help='Chrome profile dir name (e.g. "Profile 2") or absolute path')
    ap.add_argument("--proxy", default=None, help="http://user:pass@host:port (optional)")
    ap.add_argument("--prompt", default="a calm ocean wave at golden sunset, cinematic")
    ap.add_argument("--ratio", default="16:9")
    ap.add_argument("--model", default="seedance_v2.0")
    ap.add_argument("--duration", type=int, default=10)
    ap.add_argument("--out", default=os.path.join("outputs", "pw_test.mp4"))
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--dedicated", action="store_true",
                    help="profile is a dedicated automation profile (data/dola_profiles/<name>) — use directly, no copy")
    args = ap.parse_args()
    global DEDICATED
    DEDICATED = bool(args.dedicated)
    rc = asyncio.run(run(args.profile, args.proxy, args.prompt, args.ratio,
                         args.model, args.duration, args.out, args.headless))
    sys.exit(rc)


if __name__ == "__main__":
    main()
