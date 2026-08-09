from __future__ import annotations
import asyncio
import json
import os
import re
import time
import uuid
import urllib.parse

DOLA_ORIGIN = "https://www.dola.com"
START_URL = "https://www.dola.com/chat/create-image"
BOT_ID = "7339470689562525703"                 # Dola assistant bot id (constant)
VIDEO_ABILITY_TYPE = 17                          # skill_type 17 = video_generation
LOGIN_COOKIES = {"sessionid", "sid_tt", "sid_guard", "uid_tt", "sessionid_ss"}
SIGN_PARAMS = {"msToken", "a_bogus", "X-Bogus", "_signature", "x-signature"}

# Options exposed by /samantha/skill/pack (skill_type 17). Kept as sane fallbacks; the
# live list can be refreshed at runtime via DolaSession.fetch_capabilities().
DEFAULT_MODELS = {
    "seedance_v2.0": "Seedance 2.0 Fast",
    "ic_mini": "Seedance 1.0 Fast",
}
DEFAULT_RATIOS = ["1:1", "3:4", "4:3", "9:16", "16:9", "21:9"]
DEFAULT_DURATIONS = ["5", "10"]


class DolaError(Exception):
    pass


class DailyLimitReached(DolaError):
    """dola returned 'You've reached the daily limit for video generation.'"""


class GenerationRefused(DolaError):
    """dola refused/moderated the prompt (won't produce a video)."""


# Terminal messages dola streams into the assistant reply. Detecting these lets us
# stop immediately instead of blindly polling until timeout.
# Find a visible element by text/aria and return its CENTER for a trusted click.
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

# ── FULL verdict markers, ported from the extension (dola.js + dola_mode.py) ──
# Definitive daily-limit / out-of-quota (account can't generate now).
_LIMIT_MARKERS = (
    "reached the daily limit for video generation",
    "daily limit for video generation",
    "reached the daily limit",
    "try again tomorrow",
    # cap prompts dola shows instead of generating — they never yield a vid
    "do you want to continue generating",
    "longer than 10 seconds is not supported",
)
# Last-points edge case: dola OPTIMISTICALLY says "generating" then corrects with
# "I can't generate the video. No points were used." → account is out of quota.
_CANTGEN_MARKERS = (
    "can't generate the video", "cannot generate the video",
    "couldn't generate the video", "could not generate the video",
    "unable to generate the video", "no points were used",
)
# Content-moderation refusal — THIS prompt is bad; the ACCOUNT is fine (do NOT
# exhaust it, just fail this one job).
_CONTENT_REFUSAL_MARKERS = (
    "generate the requested content", "create the requested content",
    "try something else", "against our content policy",
)
_REFUSAL_MARKERS = (
    "temporarily unable to generate", "unable to generate a video",
    "please try entering other requirements", "violates", "not able to create",
) + _CONTENT_REFUSAL_MARKERS
# Backend-authoritative "a video task was actually queued" flag.
_HAS_VIDEO_GEN = '"has_video_gen":"1"'


def _points_left(text: str):
    """Backend-authoritative remaining video points, e.g. 'You still have 0 points
    left today.' — 0 means retire the account after the current gen. None if absent."""
    m = (re.search(r"have\s+(\d+)\s+(?:video\s+)?points?\s+left\s+today", text, re.I)
         or re.search(r"only\s+have\s+(\d+)\s+left\s+today", text, re.I)
         or re.search(r"(\d+)\s+points?\s+left\s+today", text, re.I))
    return int(m.group(1)) if m else None


def _classify_reply(raw: str):
    """Classify a dola reply (submit SSE OR poll chain). Returns one of:
        'refused'  — content moderation (fail job, account OK)
        'limit'    — daily-limit / no-points (exhaust / burn-recreate)
        'gen'      — a video was queued / is being produced
        None       — nothing decisive yet
    Priority mirrors the extension: content-refusal and hard limits WIN over the
    optimistic 'the video will be generated' text (dola prints that even when it
    then refuses)."""
    low = raw.lower()
    if any(m in low for m in _CONTENT_REFUSAL_MARKERS):
        return "refused"
    if any(m in low for m in _LIMIT_MARKERS):
        return "limit"
    if any(m in low for m in _CANTGEN_MARKERS):
        return "limit"
    if "left today" in low and ("video point" in low or "only have" in low or "insufficient" in low):
        return "limit"
    if any(m in low for m in _REFUSAL_MARKERS):
        return "refused"
    if _HAS_VIDEO_GEN in raw or "the video will be generated" in low or "will be ready in" in low:
        return "gen"
    return None


def _full_option() -> dict:
    """The complete `option` block dola's web UI sends. The full set is required —
    a trimmed version makes dola reuse a stale conversation and generate an image."""
    now = int(time.time())
    return {
        "send_message_scene": "", "create_time_ms": now * 1000, "collect_id": "", "is_audio": False,
        "answer_with_suggest": False, "tts_switch": False, "need_deep_think": 0,
        "click_clear_context": False, "from_suggest": False, "is_regen": False, "is_replace": False,
        "is_from_click_option": False, "is_from_click_softlink": False, "disable_sse_cache": False,
        "select_text_action": "", "is_select_text": False, "resend_for_regen": False, "scene_type": 0,
        "unique_key": str(uuid.uuid4()), "start_seq": 0, "need_create_conversation": True,
        "conversation_init_option": {"need_ack_conversation": True}, "regen_query_id": [],
        "edit_query_id": [], "regen_instruction": "", "no_replace_for_regen": False, "message_from": 0,
        "shared_app_name": "", "shared_app_id": "", "sse_recv_event_options": {"support_chunk_delta": True},
        "is_ai_playground": False, "is_old_user": False,
        "recovery_option": {"is_recovery": False, "req_create_time_sec": now, "append_sse_event_scene": 0},
        "message_storage_type": 0, "related_deleted_message_ids": {},
    }


class DolaSession:
    """Wraps one logged-in dola.com browser page and exposes the video-gen API."""

    def __init__(self, context, page, logger=None):
        self.ctx = context
        self.page = page
        self._log = logger or (lambda *a: None)
        self._base = {}                    # common query params (aid, device_id, ...)
        self.last_points_left = None       # backend remaining video points (0 ⇒ retire after this gen)
        self.page.on("request", self._on_request)

    # ---- signing / base params -------------------------------------------------
    def _on_request(self, req):
        try:
            if "www.dola.com/" not in req.url:
                return
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(req.url).query))
            if "device_id" in q and "aid" in q and not self._base:
                self._base = {k: v for k, v in q.items() if k not in SIGN_PARAMS}
        except Exception:
            pass

    def _qs(self) -> str:
        return urllib.parse.urlencode(self._base)

    async def _perf_scan_base(self):
        """Robust fallback (esp. headless): read the page's OWN past requests via
        the Performance API and pull the base query params from any dola API URL
        that already carries device_id+aid. Works even when the live request
        sniffer misses them, because dola fires these during normal page load."""
        js = r"""() => {
            try {
                const sign = new Set(["msToken","a_bogus","X-Bogus","_signature","x-signature"]);
                const ents = performance.getEntriesByType("resource").map(e => e.name);
                for (const url of ents) {
                    if (url.indexOf("dola.com/") === -1) continue;
                    let u; try { u = new URL(url); } catch (e) { continue; }
                    const q = u.searchParams;
                    if (q.get("device_id") && q.get("aid")) {
                        const o = {};
                        for (const [k, v] of q.entries()) if (!sign.has(k)) o[k] = v;
                        return o;
                    }
                }
                return null;
            } catch (e) { return null; }
        }"""
        try:
            got = await self.page.evaluate(js)
            if got and got.get("device_id") and got.get("aid"):
                self._base = {k: v for k, v in got.items()}
                return True
        except Exception:
            pass
        return False

    async def _ensure_base(self):
        if self._base:
            return
        # 1) live sniffer: a reload triggers the site's own signed XHRs
        await self.page.reload(wait_until="domcontentloaded")
        for _ in range(16):
            if self._base:
                return
            # 2) fallback: scan already-fired requests via the Performance API
            if await self._perf_scan_base():
                return
            await asyncio.sleep(0.6)
        # 3) last try: navigate to the video page (fires more API calls) + scan
        try:
            await self.page.goto(START_URL, wait_until="domcontentloaded")
            for _ in range(10):
                if self._base or await self._perf_scan_base():
                    return
                await asyncio.sleep(0.6)
        except Exception:
            pass
        raise DolaError("Could not capture base query params from dola.com")

    async def pf(self, path: str, body, content_type: str = "application/json") -> dict:
        """In-page signed POST. Returns {'status': int, 'body': str}."""
        url = f"{DOLA_ORIGIN}{path}"
        if "?" not in url:
            url += "?" + self._qs()
        js = """async ({url, body, ct}) => {
            try {
                const o = {method:'POST', credentials:'include', headers:{'content-type':ct}};
                if (body !== null) o.body = JSON.stringify(body);
                const r = await fetch(url, o);
                const t = await r.text();
                return {status: r.status, body: t};
            } catch (e) { return {status: -1, body: 'ERR:' + e}; }
        }"""
        return await self.page.evaluate(js, {"url": url, "body": body, "ct": content_type})

    # ---- session ---------------------------------------------------------------
    async def open(self):
        await self.page.goto(START_URL, wait_until="domcontentloaded")
        await asyncio.sleep(3)
        await self._ensure_base()

    async def is_logged_in(self) -> bool:
        cks = await self.ctx.cookies(DOLA_ORIGIN)
        return bool({c["name"] for c in cks} & LOGIN_COOKIES)

    async def ensure_logged_in(self, timeout: int = 25) -> bool:
        """Ensure the dola.com session is live. dola's login is auto_open: with an
        ACTIVE Google session in the profile, visiting dola.com silently runs the
        Google OAuth (prompt=none) and calls /passport/web/auth/login — no click.
        Returns True if a dola session is established within `timeout` seconds.

        Requires the profile's Google session to be active (log the account into
        Google via real Chrome). If Google is signed out this returns False.
        """
        if await self.is_logged_in():
            return True
        await self.page.goto(START_URL, wait_until="domcontentloaded")
        deadline = time.time() + timeout
        while time.time() < deadline:
            await asyncio.sleep(2)
            if await self.is_logged_in():
                return True
            try:
                if "accounts.google.com" in str(self.page.url or ""):
                    return False  # Google signed out -> account chooser, can't auto-login
            except Exception:
                pass
        return await self.is_logged_in()

    async def _trusted_click(self, wants):
        """Find a visible element by text/aria and click it with a REAL mouse
        gesture (page.mouse.click). GSI's 'Continue with Google' only reacts to a
        genuine user gesture — a scripted el.click() is ignored (→ no popup)."""
        rect = await self.page.evaluate(_RECT_JS, wants)
        if not rect:
            return None
        try:
            await self.page.mouse.click(rect["x"], rect["y"])
            return rect["label"]
        except Exception:
            return None

    async def login_via_google(self, gmail: str = None, timeout: int = 90) -> bool:
        """Drive dola's own 'Continue with Google' login (extension-style) against
        the profile's active Google session. Establishes the dola session even when
        dola's silent auto-login doesn't fire. Used for first login AND for the
        re-login after a burn-recreate (delete)."""
        await self.page.goto(f"{DOLA_ORIGIN}/chat/create-video", wait_until="domcontentloaded")
        await asyncio.sleep(2.5)
        if await self.is_logged_in():
            return True
        await self._trusted_click(["log in", "login", "sign in", "log in / sign up", "sign up / log in"])
        await asyncio.sleep(1.8)
        popup = None
        try:
            async with self.ctx.expect_page(timeout=12000) as pi:
                await self._trusted_click(["continue with google", "sign in with google", "log in with google"])
            popup = await pi.value
        except Exception:
            popup = None
        if popup:
            try:
                await popup.wait_for_load_state("domcontentloaded")
                await asyncio.sleep(2)
                await popup.evaluate(
                    r"""(email) => {
                        const rows = Array.from(document.querySelectorAll("div[data-identifier], li, div[role='link'], div"))
                          .filter(e => { const t=(e.innerText||'').toLowerCase();
                            return t.indexOf('@')!==-1 && e.getBoundingClientRect().height>20 && e.getBoundingClientRect().height<120; });
                        let el = email ? rows.find(e => (e.innerText||'').toLowerCase().indexOf(email)!==-1) : null;
                        if(!el) el = rows[0];
                        if(el){ el.click(); return true; }
                        return false;
                    }""", (gmail or "").lower())
            except Exception:
                pass
        deadline = time.time() + timeout
        while time.time() < deadline:
            await asyncio.sleep(2)
            try:
                await self._trusted_click(["i am 18", "confirm", "i'm 18", "yes", "continue"])  # age modal
            except Exception:
                pass
            if await self.is_logged_in():
                return True
        return await self.is_logged_in()

    async def fetch_capabilities(self) -> dict:
        """Live model / ratio / duration options from /samantha/skill/pack."""
        r = await self.pf("/samantha/skill/pack", {"skill_type": VIDEO_ABILITY_TYPE})
        try:
            data = json.loads(r["body"])["data"]["video_generation"]["meta"]
        except Exception:
            return {"models": DEFAULT_MODELS, "ratios": DEFAULT_RATIOS, "durations": DEFAULT_DURATIONS}
        out = {"models": {}, "ratios": [], "durations": []}
        for opt in data.get("option_list", []):
            if opt.get("value") == "model":
                out["models"] = {o["value"]: o["show_name"] for o in opt["options"]}
            elif opt.get("value") == "ratio":
                out["ratios"] = [o["value"] for o in opt["options"]]
            elif opt.get("value") == "duration":
                out["durations"] = [o["value"] for o in opt["options"]]
        return out

    # ---- generation ------------------------------------------------------------
    @staticmethod
    def _build_prompt_text(prompt: str, ratio: str) -> str:
        """Mirror the extension's promptText: strip any leading 'Generated video:'
        prefixes + a trailing aspect ratio, then add exactly one prefix and append
        the ratio. The 'Generated video: ' prefix makes dola return a VIDEO (not
        images); the ', 16:9' suffix ENFORCES the aspect (ability_param.ratio alone
        is a weak hint the model sometimes ignores)."""
        p = str(prompt or "")
        while re.match(r"(?i)^\s*generated video:\s*", p):
            p = re.sub(r"(?i)^\s*generated video:\s*", "", p)
        p = re.sub(r"\s*,\s*\d{1,2}\s*:\s*\d{1,2}\s*$", "", p).strip()
        r = str(ratio or "").strip()
        return "Generated video: " + p + (", " + r if r else "")

    async def submit(self, prompt: str, model: str = "seedance_v2.0",
                     ratio: str = "9:16", duration: int = 10) -> str:
        """Submit a text->video generation. Returns conversation_id.
        Raises DailyLimitReached / GenerationRefused from the submit SSE itself."""
        prompt_text = self._build_prompt_text(prompt, ratio)
        local_conv = f"local_{int(time.time() * 1000)}"
        body = {
            "client_meta": {"local_conversation_id": local_conv, "conversation_id": "",
                            "bot_id": BOT_ID, "last_section_id": "", "last_message_index": None},
            "messages": [{
                "local_message_id": str(uuid.uuid4()),
                "content_block": [{
                    "block_type": 10000,
                    "content": {"text_block": {"text": prompt_text, "icon_url": "", "icon_url_dark": "", "summary": ""},
                                "pc_event_block": ""},
                    "block_id": str(uuid.uuid4()), "parent_id": "", "meta_info": [], "append_fields": []
                }],
                "message_status": 0,
            }],
            "option": _full_option(),
            "chat_ability": {"ability_type": VIDEO_ABILITY_TYPE,
                             "ability_param": json.dumps({"ratio": ratio, "model": model, "duration": duration})},
            "user_context": [],
            # ext.sub_conv_firstmet_type=1 forces a FRESH conversation; without the full
            # option/ext block dola reuses a stale conversation and falls back to IMAGE gen.
            "ext": {"answer_with_suggest": "0", "sub_conv_firstmet_type": "1", "collection_id": "",
                    "conversation_init_option": "{\"need_ack_conversation\":true}",
                    "commerce_credit_config_enable": "0"},
        }
        r = await self.pf("/chat/completion", body)
        if r["status"] != 200:
            raise DolaError(f"submit failed: HTTP {r['status']}: {r['body'][:200]}")
        raw = r["body"] or ""
        # Backend-authoritative verdict from the submit SSE (full extension logic).
        verdict = _classify_reply(raw)
        if verdict == "refused":
            raise GenerationRefused("dola refused/moderated this prompt")
        if verdict == "limit":
            raise DailyLimitReached("daily video-generation limit reached / no points left")
        conv = re.findall(r'"conversation_id":"(\d+)"', raw)
        if not conv:
            raise DolaError("submit ok but no conversation_id in SSE")
        return conv[0]

    async def _pull_single(self, conv_id: str) -> str:
        body = {"cmd": 3100, "uplink_body": {"pull_singe_chain_uplink_body": {
            "conversation_id": conv_id, "anchor_index": 9007199254740991, "conversation_type": 3,
            "direction": 1, "limit": 20,
            "ext": {"sync_scenario": "SendBot|flow.agent.creation",
                    "pull_single_chain_scene": "multi_device_red_dot_sync"},
            "filter": {"index_list": []}, "evaluate_ab_params": "", "evaluate_common_params": ""}},
            "sequence_id": str(uuid.uuid4()), "channel": 2, "version": "1"}
        # IMPORTANT: IM endpoints require this exact content-type or they return
        # error 712012002 ("unsupported encoding type").
        r = await self.pf("/im/chain/single", body, content_type="application/json; encoding=utf-8")
        return r.get("body", "") or ""

    async def snapshot_vids(self, conv_id: str) -> set:
        """Vids already present in a conversation (so we can ignore them and wait for the new one)."""
        try:
            return set(_extract_all_vids(await self._pull_single(conv_id)))
        except Exception:
            return set()

    async def wait_for_video(self, conv_id: str, timeout: int = 240,
                             poll_every: float = 6.0, exclude: set | None = None):
        """Poll until a NEW finished video appears (a vid not in `exclude`).
        Returns (vid, raw_message_text)."""
        exclude = exclude or set()
        deadline = time.time() + timeout
        last = ""
        self.last_points_left = None
        while time.time() < deadline:
            await asyncio.sleep(poll_every)
            last = await self._pull_single(conv_id)
            # A NEW finished vid wins immediately.
            for vid in _extract_all_vids(last):
                if vid not in exclude:
                    return vid, last
            # Otherwise apply the FULL verdict classifier — catches the case where
            # dola shows "generating" then flips to "can't generate / no points /
            # daily limit / refused" mid-poll (fake-poll would otherwise run to the
            # full timeout).
            pl = _points_left(last)
            if pl is not None:
                self.last_points_left = pl
            verdict = _classify_reply(last)
            if verdict == "refused":
                raise GenerationRefused("dola refused/moderated this prompt")
            if verdict == "limit":
                raise DailyLimitReached("daily video-generation limit reached / no points left")
        raise DolaError("video not ready within timeout")

    # ---- account deletion (dola.com only — Google login stays intact) ---------
    async def delete_account(self, *, timeout: int = 75, log=None, dry_run: bool = False) -> tuple:
        """Fully-automated dola.com account deletion via the /delete-account page.

        Returns (ok: bool, detail: str). This deletes ONLY the dola.com (ByteDance
        passport) account. The linked Google account/cookies live on a different
        domain (google.com) and are never touched — the page's Google re-auth is
        silent (prompt=none) and only reads a token, it does not sign Google out.

        The passport calls (/passport/cancel/user_check, /passport/web/auth/authorize,
        /passport/web/cancel/confirm) are signed client-side by dola's own passport
        JS-SDK, so we drive the real page UI rather than forging the requests.
        """
        log = log or self._log
        delete_url = f"{DOLA_ORIGIN}/delete-account"
        state = {"confirm_status": None, "confirm_ok": False, "user_check_status": None}

        def _on_resp(resp):
            try:
                u = resp.url
                if "/passport/web/cancel/confirm/" in u:
                    state["confirm_status"] = resp.status
                    if resp.status == 200:
                        state["confirm_ok"] = True
                elif "/passport/cancel/user_check/" in u:
                    state["user_check_status"] = resp.status
            except Exception:
                pass

        def _on_dialog(dialog):
            # Auto-accept any native confirm() popup the page might raise.
            try:
                asyncio.create_task(dialog.accept())
            except Exception:
                pass

        self.page.on("response", _on_resp)
        self.page.on("dialog", _on_dialog)

        # 1) Open the delete-account page. dola's own login is auto_open: with an
        #    ACTIVE Google session it silently re-auths (prompt=none) and renders
        #    "Delete Now" — no pre-existing dola cookies needed. If Google is signed
        #    out, the page bounces to the Google account chooser (accounts.google.com).
        log("opening /delete-account (Google re-auth auto-completes if Google is logged in)...")
        await self.page.goto(delete_url, wait_until="domcontentloaded")
        await asyncio.sleep(7)
        cur_url = ""
        try:
            cur_url = str(self.page.url or "")
        except Exception:
            cur_url = ""
        if "accounts.google.com" in cur_url:
            # The delete re-auth bounced to Google (chooser/consent for this
            # sensitive op). Google IS logged in — nudge the account row and WAIT
            # for the redirect back to dola.com/delete-account (where 'Delete Now'
            # renders). Poll the LIVE url so we don't act on a stale mid-redirect.
            log("delete re-auth bounced to Google — completing + waiting for return to dola...")
            back = False
            for _ in range(12):   # ~30s
                try:
                    await self.page.evaluate(
                        r"""() => {
                            const rows = Array.from(document.querySelectorAll("div[data-identifier], li, div[role='link'], div"))
                              .filter(e => { const t=(e.innerText||'').toLowerCase();
                                return t.indexOf('@')!==-1 && e.getBoundingClientRect().height>20 && e.getBoundingClientRect().height<130; });
                            if(rows[0]) rows[0].click();
                        }""")
                except Exception:
                    pass
                await asyncio.sleep(2.5)
                try:
                    cur_url = str(self.page.url or "")
                except Exception:
                    cur_url = ""
                if "dola.com" in cur_url:
                    back = True
                    break
            if not back:
                return False, f"Google re-auth for delete didn't return to dola (url={cur_url[:60]})."
            log("  re-auth complete — back on dola/delete-account")
            await asyncio.sleep(4)   # let /delete-account render the danger button

        # 3) Locate the danger 'Delete Now' control (and click it unless dry-run).
        clicked = await self._click_delete_control(log, do_click=not dry_run)
        if not clicked:
            return False, "could not find the 'Delete Now' button on /delete-account"
        if dry_run:
            return True, "dry run OK — 'Delete Now' button found, NOT clicked (no deletion)"
        log("clicked 'Delete Now' — waiting for server confirmation...")

        # 4) Wait for the cancel/confirm 200 (definitive success signal).
        deadline = time.time() + timeout
        while time.time() < deadline:
            if state["confirm_ok"]:
                await asyncio.sleep(2)  # let the follow-up logout settle
                return True, "account deletion confirmed (passport cancel/confirm 200)"
            if state["confirm_status"] and state["confirm_status"] != 200:
                return False, f"cancel/confirm returned HTTP {state['confirm_status']}"
            # A secondary confirm modal may appear on some accounts — click it too.
            await self._click_secondary_confirm()
            await asyncio.sleep(1.5)

        # 5) Fallback: if dola login cookies are gone, treat as deleted.
        if not await self.is_logged_in():
            return True, "account appears deleted (dola login cookies cleared)"
        detail = "deletion not confirmed within timeout"
        if state["user_check_status"] and state["user_check_status"] != 200:
            detail += f" (user_check HTTP {state['user_check_status']})"
        return False, detail

    async def _click_delete_control(self, log=None, do_click=True) -> bool:
        """Find the 'Delete Now' danger button (text + class fallbacks).
        Clicks it when do_click is True; otherwise only reports it was found."""
        js = r"""(doClick) => {
            const norm = (s) => (s || "").replace(/\s+/g, " ").trim().toLowerCase();
            const vis = (el) => {
                if (!el || !el.isConnected) return false;
                const st = getComputedStyle(el);
                if (!st || st.display === "none" || st.visibility === "hidden" || st.opacity === "0") return false;
                const r = el.getBoundingClientRect();
                return r.width > 4 && r.height > 4;
            };
            const nodes = Array.from(document.querySelectorAll("div,button,[role='button'],span,a")).filter(vis);
            const cls = (e) => (e.className || "").toString();
            let el =
                nodes.find((e) => cls(e).includes("confirm-button") && cls(e).includes("type-danger")) ||
                nodes.find((e) => ["delete now", "delete account"].includes(norm(e.textContent)) && cls(e).includes("clickable")) ||
                nodes.find((e) => ["delete now", "delete account", "delete", "confirm"].includes(norm(e.textContent)) && getComputedStyle(e).cursor === "pointer");
            if (!el) return { ok: false };
            const label = (el.textContent || "").replace(/\s+/g, " ").trim().slice(0, 40);
            if (!doClick) return { ok: true, label, clicked: false };
            try { el.click(); } catch (e) {
                try { el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window })); }
                catch (e2) { return { ok: false }; }
            }
            return { ok: true, label, clicked: true };
        }"""
        try:
            info = await self.page.evaluate(js, do_click)
        except Exception as exc:
            if log:
                log(f"delete-control click error: {str(exc)[:120]}")
            return False
        if not info or not info.get("ok"):
            return False
        if log:
            verb = "clicked" if info.get("clicked") else "found (not clicked)"
            log(f"delete control {verb}: '{info.get('label', 'Delete Now')}'")
        return True

    async def _click_secondary_confirm(self):
        """Click a follow-up confirmation button inside a modal, if one appears."""
        js = r"""() => {
            const norm = (s) => (s || "").replace(/\s+/g, " ").trim().toLowerCase();
            const vis = (el) => {
                if (!el || !el.isConnected) return false;
                const st = getComputedStyle(el);
                if (!st || st.display === "none" || st.visibility === "hidden" || st.opacity === "0") return false;
                const r = el.getBoundingClientRect();
                return r.width > 4 && r.height > 4;
            };
            const scopes = Array.from(document.querySelectorAll(
                "[role='dialog'],[class*='modal'],[class*='Modal'],[class*='semi-modal'],[class*='confirm']"
            ));
            const roots = scopes.length ? scopes : [document];
            for (const root of roots) {
                const btns = Array.from(root.querySelectorAll("button,[role='button'],div,span")).filter(vis);
                const hit = btns.find((b) => ["confirm", "delete", "delete now", "ok", "yes", "continue"].includes(norm(b.textContent)));
                if (hit) { try { hit.click(); return true; } catch (e) {} }
            }
            return false;
        }"""
        try:
            return bool(await self.page.evaluate(js))
        except Exception:
            return False

    async def get_play_info(self, vid: str) -> str:
        """Resolve a vid to a downloadable mp4 URL."""
        r = await self.pf("/samantha/video/get_play_info", {"vid": vid})
        urls = _extract_mp4(r["body"] or "")
        if not urls:
            raise DolaError("get_play_info returned no mp4 url")
        return urls[0]

    async def download(self, url: str, out_path: str) -> int:
        """Download via the browser network stack (shares auth/cookies). Returns bytes written."""
        resp = await self.ctx.request.get(url)
        data = await resp.body()
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(data)
        return len(data)

    # ---- high-level convenience ------------------------------------------------
    async def generate_one(self, prompt: str, out_path: str, *, model="seedance_v2.0",
                           ratio="9:16", duration=10, timeout=720) -> str:
        """Full pipeline for a single prompt -> saved mp4. Returns out_path."""
        self._log(f"submit: {prompt[:50]}...")
        # Land the generation in a fresh conversation so we never pick up an old video…
        seen_before = set()
        conv = await self.submit(prompt, model=model, ratio=ratio, duration=duration)
        # …and as a belt-and-braces guard, ignore any vids already in that conversation.
        seen_before = await self.snapshot_vids(conv)
        self._log(f"conversation_id={conv}; {len(seen_before)} existing vid(s) ignored; waiting for new video...")
        vid, msg = await self.wait_for_video(conv, timeout=timeout, exclude=seen_before)
        self._log(f"vid={vid}")
        try:
            url = await self.get_play_info(vid)
        except DolaError:
            cand = _extract_mp4(msg)                    # fallback: url embedded in message
            if not cand:
                raise
            url = cand[0]
        n = await self.download(url, out_path)
        self._log(f"downloaded {n} bytes -> {out_path}")
        return out_path


# ---- parsing helpers (tolerant of escaped JSON in the message stream) ----------
def _extract_vid(text: str):
    vids = _extract_all_vids(text)
    return vids[0] if vids else None


def _extract_all_vids(text: str):
    """All distinct video ids in a (possibly escaped) message blob, order-preserved."""
    if not text:
        return []
    found = re.findall(r'vid\\*"?\s*:\s*\\*"?(v[0-9a-z]{24,})', text)
    if not found:
        found = re.findall(r'\b(v[0-9a-z]{30,})\b', text)
    seen, out = set(), []
    for v in found:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _extract_mp4(text: str):
    if not text:
        return []
    t = text.replace("\\u0026", "&").replace("\\/", "/").replace('\\"', '"')
    urls = (re.findall(r'https?://[^\s"\\]+?\.mp4[^\s"\\]*', t)
            + re.findall(r'https?://v16-dola[^\s"\\]+', t)
            + re.findall(r'https?://vod-urls[^\s"\\]+', t))
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out
