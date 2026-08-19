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
# The newer "Skills" / general-agent route: `/creative-video <prompt>` invokes the
# creative-video skill (a MoA agent that rewrites the prompt cinematically, then
# asks to confirm before generating). Same /chat/completion endpoint + same polling;
# only the request shape and the extra "yes" confirm differ from the ability route.
CREATIVE_VIDEO_SKILL_ID = "294222337297"
CREATIVE_VIDEO_SKILL = "creative-video"
LOGIN_COOKIES = {"sessionid", "sid_tt", "sid_guard", "uid_tt", "sessionid_ss",
                 "passport_csrf_token", "sid_ucp_v1"}
SIGN_PARAMS = {"msToken", "a_bogus", "X-Bogus", "_signature", "x-signature"}

# Options exposed by /samantha/skill/pack (skill_type 17). Kept as sane fallbacks; the
# live list can be refreshed at runtime via DolaSession.fetch_capabilities().
DEFAULT_MODELS = {
    "seedance_v2.0": "Seedance 2.0 Fast",
    "ic_mini": "Seedance 1.0 Fast",
}
DEFAULT_RATIOS = ["1:1", "3:4", "4:3", "9:16", "16:9", "21:9"]
DEFAULT_DURATIONS = ["5", "10", "15"]   # 15s = creative-video skill route only


class DolaError(Exception):
    pass


class DailyLimitReached(DolaError):
    """dola returned 'You've reached the daily limit for video generation.'"""


class GenerationRefused(DolaError):
    """dola refused/moderated the prompt (won't produce a video)."""


class NotLoggedIn(DolaError):
    """dola shows the guest/logged-out state mid-generation — cannot generate
    until re-login. Distinct from exhaustion: the fix is re-login, NOT burn."""


class GotImagesNotVideo(DolaError):
    """dola produced IMAGES instead of a video (wrong intent). Fail THIS job; the
    account is fine (do NOT burn-recreate)."""


class HighDemand(DolaError):
    """dola is under high demand / servers busy — a TRANSIENT condition, NOT
    account exhaustion. The fix is BACK OFF + retry the same prompt on the same
    account (re-login if it logged out). Burning the account does NOT help — the
    whole service is busy, not the account."""


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
# Transient "servers busy / high demand" — NOT exhaustion. Back off + retry the
# SAME account (re-login if it logged out). Do NOT burn.
_HIGH_DEMAND_MARKERS = (
    # exact dola toast: "We are experiencing high demand right now. Please try again later."
    "high demand", "experiencing high", "highdemand", "high_demand",
    "sendmsg_fail_highdemand", "senddemand",
    "servers are busy", "server is busy", "service is busy", "currently busy",
    "too many requests", "try again later", "try again in a",
    "please try again shortly", "please try again later",
    "system is busy", "under heavy load", "overloaded", "rate limit", "rate_limit",
)
# Logged-out / guest — dola accepts the message but generates nothing (extension
# dola.js:892-897). Re-login (NOT burn) is the fix.
_NOTLOGGEDIN_MARKERS = (
    "not available for guests", "log in to start creating",
    "login required", "please log in", "please sign in",
)
# POSITIVE proof a video is actually being produced (extension dola.js:899-903).
_GENVIDEO_MARKERS = (
    "generating video", "video_block", '"creation_block"', "creation_loading_block",
)


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
    # Transient server-busy — MUST win over the None→exhaustion fallthrough so the
    # caller retries instead of burning a perfectly good account.
    if any(m in low for m in _HIGH_DEMAND_MARKERS):
        return "busy"
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


def _skill_option(local_msg_id: str, *, need_create: bool, selected: bool) -> dict:
    """`option` block for the creative-video SKILL route. Mirrors _full_option but adds
    the general-agent fields dola's web UI sends: Deep-Think mode (4), agent_mode, and
    general_task_param carrying the selected skill. On the confirm ("yes") turn the skill
    is NOT re-selected (selected=False) and no new conversation is created."""
    o = _full_option()
    o["need_deep_think"] = 4
    o["agent_mode"] = 1
    o["need_create_conversation"] = need_create
    o["general_task_param"] = {
        "action": 0,
        "thread_local_message_id": [local_msg_id],
        "selected_skills": [CREATIVE_VIDEO_SKILL] if selected else [],
        "skill_selections": ([{"name": CREATIVE_VIDEO_SKILL,
                               "skill_id": CREATIVE_VIDEO_SKILL_ID, "skill_type": 2}]
                             if selected else []),
    }
    o["model_config"] = {"model_item_key": "", "model_extra_params": {}}
    o["aggregate_params"] = {"model_item_key": "", "provider_id": ""}
    return o


def _max_conv_index(raw: str) -> int:
    """Highest message index present in a pulled conversation chain (0 if none)."""
    idx = [int(x) for x in re.findall(r'"index_in_conv":\s*\\*"?(\d+)', raw or "")]
    return max(idx) if idx else 0


class DolaSession:
    """Wraps one logged-in dola.com browser page and exposes the video-gen API."""

    def __init__(self, context, page, logger=None):
        self.ctx = context
        self.page = page
        self._log = logger or (lambda *a: None)
        self._base = {}                    # common query params (aid, device_id, ...)
        self.last_points_left = None       # backend remaining video points (0 ⇒ retire after this gen)
        self.last_was_hd = False           # True if the last save used the unwatermarked HD master
        self.page.on("request", self._on_request)

    # ---- signing / base params -------------------------------------------------
    def _on_request(self, req):
        try:
            if "www.dola.com/" not in req.url:
                return
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(req.url).query))
            # Merge/union across endpoints (extension dola.js:79): some params
            # (fp, tz_name, web_tab_id, …) only ride on certain requests, so we
            # accumulate them all. First value per key wins → device_id/aid stay
            # consistent. Only start once we've seen a fully-signed dola API call.
            if "device_id" in q and "aid" in q:
                for k, v in q.items():
                    if k not in SIGN_PARAMS and k not in self._base:
                        self._base[k] = v
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

    async def _page_is_guest(self) -> bool:
        """Look at the ACTUAL page, not just cookies. After a delete the dola
        cookies linger (stale) so the cookie check false-positives — but the page
        shows the guest state ('not available for guests' / a top-right 'Log In'
        button). Detecting that forces a real login (which recreates the account)."""
        try:
            return bool(await self.page.evaluate(r"""() => {
                const t = (document.body ? document.body.innerText : '').toLowerCase();
                if (t.indexOf('not available for guests') !== -1) return true;
                if (t.indexOf('log in to start creating') !== -1) return true;
                const w = window.innerWidth || 1280;
                const els = Array.from(document.querySelectorAll("button,[role='button'],a,div,span"));
                return els.some(b => {
                    const s = (b.textContent||'').replace(/\s+/g,' ').trim().toLowerCase();
                    if (s !== 'log in' && s !== 'login' && s !== 'sign in') return false;
                    const r = b.getBoundingClientRect();   // top-right corner = guest login button
                    return r.top < 140 && r.left > w*0.4 && r.width > 4 && r.height > 4;
                });
            }"""))
        except Exception:
            return False

    async def logged_in_for_real(self) -> bool:
        """Cookies present AND the page is NOT showing the guest state."""
        return (await self.is_logged_in()) and not (await self._page_is_guest())

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
        # REAL check (page state, not just stale cookies) — a deleted account keeps
        # its dola cookies but shows the guest page, so we must actually log in.
        if await self.logged_in_for_real():
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
            if await self.logged_in_for_real():
                return True
        return await self.logged_in_for_real()

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
        # 429 Too Many Requests / 503 Service Unavailable = dola throttling / high
        # demand (the web UI shows the 'experiencing high demand' toast). Transient.
        if r["status"] in (429, 503):
            raise HighDemand(f"submit HTTP {r['status']} — high demand / server busy")
        if r["status"] != 200:
            raise DolaError(f"submit failed: HTTP {r['status']}: {r['body'][:200]}")
        raw = r["body"] or ""
        # Backend-authoritative verdict from the submit SSE (full extension logic).
        verdict = _classify_reply(raw)
        if verdict == "refused":
            raise GenerationRefused("dola refused/moderated this prompt")
        if verdict == "limit":
            raise DailyLimitReached("daily video-generation limit reached / no points left")
        if verdict == "busy":
            raise HighDemand("dola under high demand at submit — transient, retry same account")
        conv = re.findall(r'"conversation_id":"(\d+)"', raw)
        if not conv:
            raise DolaError("submit ok but no conversation_id in SSE")
        # NOTE on the ambiguous "no_video_gen" case (conversation_id but no gen flag
        # / gen text): the extension deliberately did NOT treat this as exhaustion
        # (dola.js:807-814) — it's often just that has_video_gen hasn't streamed into
        # the SUBMIT response yet (esp. right after a fresh login/burn-recreate). If
        # we burn-recreated here we'd wrongly nuke a HEALTHY fresh account and
        # cascade. So we return the conversation_id and let wait_for_video decide:
        # a real gen shows up in the first few polls; a truly exhausted account
        # trips the 15s "generation never started" fast-fail there. Only EXPLICIT
        # limit/refusal text (handled above) fails instantly at submit.
        return conv[0]

    # ---- creative-video SKILL route (toggle: dola_use_skill_flow) --------------
    @staticmethod
    def _parse_prompt_duration(text: str):
        """Best-effort intended video length (seconds) stated in a prompt: an explicit
        'N sec(onds)', a '(Ns)' label like 'Prompt 1 (12s)', or the largest scene
        timestamp (e.g. '9-12s' -> 12). Returns an int in [3, 15] or None."""
        t = str(text or "")
        m = (re.search(r"(\d{1,2})\s*(?:seconds|second|secs|sec)\b", t, re.I)
             or re.search(r"\((\d{1,2})\s*s\)", t, re.I))
        if m:
            return max(3, min(15, int(m.group(1))))
        ts = [int(x) for x in re.findall(r"(\d{1,2})\s*s\b", t)]
        return max(3, min(15, max(ts))) if ts else None

    @staticmethod
    def _build_skill_text(prompt: str, ratio: str, duration: int,
                          prompt_duration: bool = False) -> str:
        """`/creative-video <prompt>[, <ratio>][, <duration>]`. The agent parses ratio and
        duration from natural language and rewrites the prompt cinematically.
        - Ratio: PROMPT-DRIVEN — only append the global ratio if the prompt doesn't state one.
        - Duration: if `prompt_duration` is True, use the EXACT length stated in the prompt
          (e.g. 'Prompt 1 (12s)' -> '12 seconds', '9-12s' timeline -> '12 seconds'), clamped
          to dola's 15s max; fall back to the global only when the prompt states nothing.
          If `prompt_duration` is False, append the global duration unless the prompt already
          states one (so an explicit '15 sec' still wins)."""
        p = str(prompt or "")
        while re.match(r"(?i)^\s*/?creative-video[:,\s]+", p):
            p = re.sub(r"(?i)^\s*/?creative-video[:,\s]+", "", p)
        while re.match(r"(?i)^\s*generated video:\s*", p):
            p = re.sub(r"(?i)^\s*generated video:\s*", "", p)
        p = p.strip()
        has_ratio = re.search(r"\b\d{1,2}\s*:\s*\d{1,2}\b", p) is not None
        tail = []
        r = str(ratio or "").strip()
        if r and not has_ratio:
            tail.append(r)
        try:
            d_global = int(duration)
        except (TypeError, ValueError):
            d_global = 0
        if prompt_duration:
            n = DolaSession._parse_prompt_duration(p)
            if n:
                tail.append(f"{n} seconds")          # exact per-prompt length, stated clearly
            elif d_global:
                tail.append(f"{d_global}s")          # no length in prompt -> global fallback
        else:
            has_dur = re.search(r"\b\d{1,3}\s*(?:s|sec|secs|second|seconds)\b", p, re.I) is not None
            if d_global and not has_dur:
                tail.append(f"{d_global}s")
        text = "/" + CREATIVE_VIDEO_SKILL + " " + p
        if tail:
            text += ", " + ", ".join(tail)
        return text

    def _text_message(self, text: str) -> tuple:
        """A single text content-block message; returns (message_dict, local_message_id)."""
        local_msg = str(uuid.uuid4())
        msg = {
            "local_message_id": local_msg,
            "content_block": [{
                "block_type": 10000,
                "content": {"text_block": {"text": text, "icon_url": "", "icon_url_dark": "", "summary": ""},
                            "pc_event_block": ""},
                "block_id": str(uuid.uuid4()), "parent_id": "", "meta_info": [], "append_fields": [],
            }],
            "message_status": 0,
        }
        return msg, local_msg

    def _attachment_message(self, att: dict) -> tuple:
        """An image-attachment message (block_type 10052) referencing an image already
        uploaded to dola's ImageX (att = {uri, name, width, height}). Returns
        (message_dict, local_message_id). Used for reference-image → video."""
        local_msg = str(uuid.uuid4())
        block = {
            "block_type": 10052,
            "content": {"attachment_block": {"attachments": [{
                "type": 1,
                "identifier": str(uuid.uuid4()),
                "image": {"name": att.get("name", ""), "uri": att["uri"],
                          "image_ori": {"url": "", "width": int(att.get("width") or 0),
                                        "height": int(att.get("height") or 0),
                                        "format": "", "url_formats": {}}},
                "parse_state": 0, "review_state": 1, "upload_status": 1, "progress": 100, "src": "",
            }]}, "pc_event_block": ""},
            "block_id": str(uuid.uuid4()), "parent_id": "", "meta_info": [], "append_fields": [],
        }
        return {"local_message_id": local_msg, "content_block": [block], "message_status": 0}, local_msg

    async def upload_reference_image(self, image_path: str, timeout: int = 60) -> dict:
        """Upload a LOCAL reference image through dola's own page uploader (which signs
        the ImageX Apply→PUT→Commit calls with the browser's STS creds — we can't sign
        those from Python). Returns {uri, name, width, height} for _attachment_message.
        Reliable because the web app does the signing exactly as a real user would."""
        if not image_path or not os.path.isfile(image_path):
            raise DolaError(f"reference image not found: {image_path}")
        # Make sure the composer (with its file <input>) is present.
        try:
            if "/chat" not in (self.page.url or ""):
                await self.page.goto(f"{DOLA_ORIGIN}/chat/create-video", wait_until="domcontentloaded")
                await asyncio.sleep(2)
        except Exception:
            pass
        inp = await self.page.query_selector('input[type="file"]')
        if inp is None:
            # reveal a hidden input by clicking an attach/upload control
            for sel in ('button[aria-label*="attach" i]', 'button[aria-label*="upload" i]',
                        'button[aria-label*="image" i]', '[data-testid*="upload"]',
                        '[class*="upload"] button', '[class*="attach"]'):
                try:
                    b = await self.page.query_selector(sel)
                    if b:
                        await b.click()
                        await asyncio.sleep(0.6)
                        inp = await self.page.query_selector('input[type="file"]')
                        if inp:
                            break
                except Exception:
                    pass
        if inp is None:
            raise DolaError("could not find dola's file-upload input for the reference image")
        # Arm the response waiter BEFORE setting the file so we don't miss the Commit.
        try:
            async with self.page.expect_response(
                    lambda r: "CommitImageUpload" in r.url, timeout=timeout * 1000) as rinfo:
                await inp.set_input_files(image_path)
            resp = await rinfo.value
            body = await resp.text()
        except Exception as e:
            raise DolaError(f"reference image upload failed / no Commit response: {str(e)[:120]}")
        m = re.search(r'"Uri":"(tos-[^"]+)"', body)
        if not m:
            raise DolaError("reference image upload: no Uri in CommitImageUpload response")
        uri = m.group(1)
        w = re.search(r'"ImageWidth":(\d+)', body)
        h = re.search(r'"ImageHeight":(\d+)', body)
        att = {"uri": uri, "name": os.path.basename(image_path),
               "width": int(w.group(1)) if w else 0, "height": int(h.group(1)) if h else 0}
        self._log(f"reference image uploaded → {uri} ({att['width']}x{att['height']})")
        return att

    async def submit_skill(self, prompt: str, ratio: str = "9:16", duration: int = 10,
                           attachment: dict | None = None,
                           prompt_duration: bool = False) -> tuple:
        """Submit a creative-video SKILL request. The agent replies asking to confirm
        (it does NOT generate yet). Returns (conversation_id, section_id, raw).
        If `attachment` is given (an uploaded reference image), it is prepended as a
        separate attachment message and the task is threaded off THAT message id
        (reference-image → video). `prompt_duration` makes the length come from the
        prompt itself (see _build_skill_text)."""
        base = prompt if str(prompt or "").strip() else ("animate the reference image" if attachment else prompt)
        text = self._build_skill_text(base, ratio, duration, prompt_duration=prompt_duration)
        text_msg, text_local = self._text_message(text)
        if attachment:
            att_msg, att_local = self._attachment_message(attachment)
            messages = [att_msg, text_msg]
            thread_id = att_local
            collection_id = str(uuid.uuid4())
        else:
            messages = [text_msg]
            thread_id = text_local
            collection_id = ""
        body = {
            "client_meta": {"local_conversation_id": f"local_{int(time.time() * 1000)}",
                            "conversation_id": "", "bot_id": BOT_ID,
                            "last_section_id": "", "last_message_index": None},
            "messages": messages,
            "option": _skill_option(thread_id, need_create=True, selected=True),
            "user_context": [],
            "ext": {"use_deep_think": "4", "sub_conv_firstmet_type": "1", "collection_id": collection_id,
                    "conversation_init_option": "{\"need_ack_conversation\":true}",
                    "commerce_credit_config_enable": "0"},
        }
        r = await self.pf("/chat/completion", body)
        if r["status"] in (429, 503):
            raise HighDemand(f"skill submit HTTP {r['status']} — high demand / server busy")
        if r["status"] != 200:
            raise DolaError(f"skill submit failed: HTTP {r['status']}: {r['body'][:200]}")
        raw = r["body"] or ""
        verdict = _classify_reply(raw)
        if verdict == "refused":
            raise GenerationRefused("dola refused/moderated this prompt")
        if verdict == "limit":
            raise DailyLimitReached("daily video-generation limit reached / no points left")
        if verdict == "busy":
            raise HighDemand("dola under high demand at skill submit — transient, retry same account")
        conv = re.findall(r'"conversation_id":"(\d+)"', raw)
        if not conv:
            raise DolaError("skill submit ok but no conversation_id in SSE")
        section = re.search(r'"section_id":"(\d+)"', raw)
        return conv[0], (section.group(1) if section else ""), raw

    async def _await_skill_ready(self, conv_id: str, query_idx: int = 1, timeout: int = 60) -> tuple:
        """Wait for the agent's confirm turn to settle (or for it to start generating
        outright). Returns (last_message_index, already_generating). Polls the chain and
        treats the turn as 'done' once the message index has grown past the user's query
        and stopped moving for two consecutive polls."""
        deadline = time.time() + timeout
        last_max, stable = query_idx, 0
        while time.time() < deadline:
            raw = await self._pull_single(conv_id)
            low = raw.lower()
            # already producing a video? (a real vid, or an ACTIVE loading block —
            # not the schema's null placeholder) → no confirm needed.
            if _extract_all_vids(raw) or 'creation_loading_block":{' in low or "generating video" in low:
                return _max_conv_index(raw) or last_max, True
            verdict = _classify_reply(raw)
            if verdict == "refused":
                raise GenerationRefused("dola refused/moderated this prompt")
            if verdict == "limit":
                raise DailyLimitReached("daily video-generation limit reached / no points left")
            if verdict == "busy":
                raise HighDemand("dola under high demand during skill confirm — transient, retry")
            mx = _max_conv_index(raw)
            if mx > query_idx:
                if mx == last_max:
                    stable += 1
                    if stable >= 2:
                        return mx, False
                else:
                    stable, last_max = 0, mx
            await asyncio.sleep(2.0)
        return last_max, False

    async def confirm_skill(self, conv_id: str, section_id: str, last_index: int,
                            text: str = "yes") -> str:
        """Send the confirm ("yes") turn that actually triggers generation. Returns raw."""
        msg, local_msg = self._text_message(text)
        body = {
            "client_meta": {"conversation_id": conv_id, "bot_id": BOT_ID,
                            "last_section_id": section_id or "", "last_message_index": last_index},
            "messages": [msg],
            "option": _skill_option(local_msg, need_create=False, selected=False),
            "user_context": [],
            "ext": {"use_deep_think": "4", "collection_id": "", "commerce_credit_config_enable": "0"},
        }
        r = await self.pf("/chat/completion", body)
        if r["status"] in (429, 503):
            raise HighDemand(f"skill confirm HTTP {r['status']} — high demand / server busy")
        if r["status"] != 200:
            raise DolaError(f"skill confirm failed: HTTP {r['status']}: {r['body'][:200]}")
        return r["body"] or ""

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

    async def check_rate_limit(self) -> dict:
        """Ask dola how long this account is send-throttled (the 'high demand'
        state). Returns {is_limit, limit_time, limit_tips, seconds} where `seconds`
        is how long to wait before it can send again (0 if not limited).
        Endpoint: POST /im/message/send_rate_limit (cmd 2260)."""
        body = {"cmd": 2260,
                "uplink_body": {"check_message_send_rate_limit_uplink_body": {}},
                "sequence_id": str(uuid.uuid4()), "channel": 2, "version": "1"}
        try:
            r = await self.pf("/im/message/send_rate_limit", body,
                              content_type="application/json; encoding=utf-8")
            d = (json.loads(r["body"] or "{}")
                 .get("downlink_body", {})
                 .get("check_message_send_rate_limit_downlink_body", {}))
        except Exception:
            return {"is_limit": False, "limit_time": 0, "limit_tips": "", "seconds": 0}
        is_limit = bool(d.get("is_limit"))
        lt = int(d.get("limit_time") or 0)
        tips = str(d.get("limit_tips") or "")
        secs = 0
        if is_limit and lt:
            # limit_time may be an absolute epoch (ms or s) or a duration in seconds.
            now = time.time()
            if lt > 1e12:      # epoch milliseconds
                secs = int(lt / 1000 - now)
            elif lt > 1e9:     # epoch seconds
                secs = int(lt - now)
            else:              # plain duration (seconds)
                secs = int(lt)
            secs = max(0, secs)
        return {"is_limit": is_limit, "limit_time": lt, "limit_tips": tips, "seconds": secs}

    async def snapshot_vids(self, conv_id: str) -> set:
        """Vids already present in a conversation (so we can ignore them and wait for the new one)."""
        try:
            return set(_extract_all_vids(await self._pull_single(conv_id)))
        except Exception:
            return set()

    async def wait_for_video(self, conv_id: str, timeout: int = 240,
                             poll_every: float = 3.0, exclude: set | None = None,
                             claim_set: set | None = None):
        """Poll until a NEW finished video appears (a vid not in `exclude`).
        Returns (vid, raw_message_text). `claim_set` (optional, shared across an account's
        tabs) is a hard DUPLICATE GUARD: a vid is atomically claimed the instant it's
        found (no await between check and add, so two parallel tabs can never take the
        same vid — the second one keeps waiting for its own). This prevents the same
        video being saved onto two different prompts."""
        exclude = exclude or set()
        deadline = time.time() + timeout
        start = time.time()
        last = ""
        self.last_points_left = None
        saw_gen = False
        cyc = 0
        while time.time() < deadline:
            await asyncio.sleep(poll_every)
            last = await self._pull_single(conv_id)
            cyc += 1
            low0 = last.lower()
            # 1) A NEW finished vid wins immediately. Skip any vid already excluded OR
            #    already claimed by another tab, then atomically claim this one.
            for vid in _extract_all_vids(last):
                if vid in exclude or (claim_set is not None and vid in claim_set):
                    continue
                if claim_set is not None:
                    claim_set.add(vid)          # atomic claim — no await before returning
                return vid, last
            # 2) Logged-out / guest mid-generation (extension dola.js:551) — the fix
            #    is RE-LOGIN, not burn. Distinct error so the runner re-logs in.
            if any(m in low0 for m in _NOTLOGGEDIN_MARKERS):
                raise NotLoggedIn("dola shows logged-out/guest state during generation")
            # 3) Backend points-left (authoritative) — 0 ⇒ retire after this gen.
            pl = _points_left(last)
            if pl is not None:
                self.last_points_left = pl
            # 4) FULL verdict classifier — catches dola showing "generating" then
            #    flipping to "can't generate / no points / daily limit / refused"
            #    mid-poll (a fake-poll would otherwise run to the full timeout).
            verdict = _classify_reply(last)
            if verdict == "refused":
                raise GenerationRefused("dola refused/moderated this prompt")
            if verdict == "limit":
                raise DailyLimitReached("daily video-generation limit reached / no points left")
            if verdict == "busy":
                raise HighDemand("dola under high demand during generation — transient, retry")
            # 5) POSITIVE proof a video is being produced (extension dola.js:556).
            if (_HAS_VIDEO_GEN in last or "generating" in low0
                    or "the video will be generated" in low0 or "will be ready" in low0
                    or any(m in low0 for m in _GENVIDEO_MARKERS)):
                saw_gen = True
            # 6) dola produced IMAGES instead of a video (extension dola.js:561) —
            #    wrong intent, fail THIS job; the account is FINE (do NOT burn).
            got_images = (("gen_image_block" in low0 or '"image_block":{' in low0)
                          and "generating video" not in low0)
            if not saw_gen and got_images and cyc >= 3:
                raise GotImagesNotVideo("dola produced images instead of a video")
            # 7) Silent exhaustion: submit accepted (conversation_id) but no gen ever
            #    starts (no gen text/flag, no vid, no explicit error) — almost always
            #    the account is out of points. A REAL gen confirms within ~6s; with 3s
            #    polling that's ~5 checks by 15s, so if nothing has started by then the
            #    account is exhausted → fail fast so the caller burn-recreates now.
            if not saw_gen and (time.time() - start) > 15:
                raise DailyLimitReached("generation never started — account exhausted (no points)")
            # 8) Periodic visibility: every ~15s log what dola is actually showing so
            #    a stuck "generating" or a NEW/unknown error is visible in the log
            #    (esp. useful when the window is off-screen/headless).
            if cyc % 5 == 1:
                m = re.findall(r'"text_block":\{"text":"((?:[^"\\]|\\.){0,140})', last)
                snippet = (m[-1] if m else "")[:120]
                elapsed = int(cyc * poll_every)
                self._log(f"  …still waiting ({elapsed}s): generating={saw_gen} "
                          f"images={got_images} points_left={self.last_points_left} "
                          f"| dola: {snippet!r}")
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
                # Match BOTH the web flow (/passport/web/cancel/confirm/) and the plain
                # variant (/passport/cancel/confirm/) — dola's passport SDK uses the web
                # one for the /delete-account page, but this stays correct if that changes.
                if "cancel/confirm/" in u and "/passport/" in u:
                    state["confirm_status"] = resp.status
                    if resp.status == 200:
                        state["confirm_ok"] = True
                elif "cancel/user_check/" in u and "/passport/" in u:
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
        try:
            return await self._delete_flow(delete_url, state, log, dry_run, timeout)
        finally:
            # Detach the per-delete listeners so repeated delete_account() calls
            # don't stack duplicate handlers on the page.
            for _ev, _fn in (("response", _on_resp), ("dialog", _on_dialog)):
                try:
                    self.page.remove_listener(_ev, _fn)
                except Exception:
                    pass

    async def _delete_flow(self, delete_url, state, log, dry_run, timeout) -> tuple:
        # 1) Open the delete-account page. dola's own login is auto_open: with an
        #    ACTIVE Google session it silently re-auths (prompt=none) and renders
        #    "Delete Now" — no pre-existing dola cookies needed. If Google is signed
        #    out, the page bounces to the Google account chooser (accounts.google.com).
        log("opening /delete-account (Google re-auth auto-completes if Google is logged in)...")
        await self.page.goto(delete_url, wait_until="domcontentloaded")
        await asyncio.sleep(1.5)   # let the page commit to bounce-or-render
        # Poll fast for a KNOWN state instead of a blanket 7s wait: either it bounced
        # to Google (re-auth) or the Delete button already rendered. Proceeds the
        # instant it's ready — same accuracy, no wasted seconds on a fast load.
        cur_url = ""
        for _ in range(24):   # up to ~12s, 0.5s granularity
            try:
                cur_url = str(self.page.url or "")
            except Exception:
                cur_url = ""
            if "accounts.google.com" in cur_url:
                break
            if await self._click_delete_control(log, do_click=False):
                break
            await asyncio.sleep(0.5)
        if "accounts.google.com" in cur_url:
            # The delete re-auth bounced to Google (chooser/consent for this
            # sensitive op). Google IS logged in — nudge the account row and WAIT
            # for the redirect back to dola.com/delete-account (where 'Delete Now'
            # renders). Poll the LIVE url so we don't act on a stale mid-redirect.
            log("delete re-auth bounced to Google — completing + waiting for return to dola...")
            back = False
            for i in range(40):   # ~20s, but returns the INSTANT we're back on dola
                if self.page.is_closed():   # browser context died (overload) — stop
                    return False, "browser context closed during delete re-auth"
                if i % 6 == 0:   # (re)click the account row every ~3s
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
                await asyncio.sleep(0.5)   # poll the url every 0.5s (fast return detect)
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

        # Let the page settle on the delete view before hunting the button — right
        # after the OAuth bounce it may still be mid-navigation, which makes an
        # evaluate throw "execution context was destroyed". The button-retry loop
        # below then grabs the button the instant it renders (no blanket wait).
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=6000)
        except Exception:
            pass
        await asyncio.sleep(1)
        # 3) Locate + click the danger 'Delete Now' control. The button renders
        #    LATE — the page keeps bouncing through the silent Google OAuth for a few
        #    seconds after we're "back on dola" before 'Delete Now' paints. So we RETRY
        #    for ~45s (verified live: the button shows a little while after the re-auth,
        #    not instantly) and break the instant it appears.
        clicked = False
        for i in range(45):   # ~45s, but breaks the instant the button appears
            if self.page.is_closed():   # browser context died (overload) — stop, don't spam
                return False, "browser context closed during delete"
            if await self._click_delete_control(log, do_click=not dry_run):
                clicked = True
                break
            # A logout redirect here means the delete already went through.
            try:
                u = str(self.page.url or "").lower()
            except Exception:
                u = ""
            if any(s in u for s in ("/login", "/passport/web/logout", "from_logout")):
                return True, "account deletion confirmed (logout redirect)"
            if i and i % 10 == 0 and log:
                log(f"  …still waiting for the 'Delete Now' button to render ({i}s)")
            await asyncio.sleep(1.0)
        if not clicked:
            # Distinguish the REAL cause: if we're stuck on Google's own login/account
            # chooser (accounts.google.com), the account's Google session is SIGNED OUT —
            # cookies alone couldn't re-auth it, so 'Delete Now' can never render. Report
            # that clearly (the fix is a full re-login) instead of the misleading
            # "button not found". Otherwise dump the page for inspection.
            try:
                cur = str(self.page.url or "").lower()
            except Exception:
                cur = ""
            if "accounts.google.com" in cur or "signin" in cur:
                return False, ("Google session SIGNED OUT for this account — re-auth "
                               "couldn't complete (run: dola_profiles.py login --as <acct>)")
            await self._dump_clickables(log)   # so we can see what the page actually shows
            return False, "could not find the 'Delete Now' button on /delete-account"
        if dry_run:
            return True, "dry run OK — 'Delete Now' button found, NOT clicked (no deletion)"
        log("clicked 'Delete Now' — waiting for server confirmation...")

        # 4) Wait for the cancel/confirm 200 (definitive success signal).
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.page.is_closed():   # browser context died (overload) — stop, don't spam
                return False, "browser context closed during delete"
            if state["confirm_ok"]:
                await asyncio.sleep(2)  # let the follow-up logout settle
                return True, "account deletion confirmed (passport cancel/confirm 200)"
            # A logout redirect is ALSO a definitive success signal (extension
            # dola.js:1623) — the delete drops the session and bounces to login.
            try:
                u = str(self.page.url or "").lower()
            except Exception:
                u = ""
            if any(s in u for s in ("/login", "/passport/web/logout", "from_logout")):
                return True, "account deletion confirmed (logout redirect)"
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

    async def _dump_clickables(self, log=None) -> None:
        """Diagnostic: log the visible clickable elements on the current page so we can
        see what the /delete-account page actually renders when the 'Delete Now' button
        isn't matched (dola occasionally changes the button text/markup)."""
        if not log:
            return
        try:
            items = await self.page.evaluate(r"""() => {
                const norm=(s)=>(s||'').replace(/\s+/g,' ').trim();
                const vis=(el)=>{const st=getComputedStyle(el);if(!st)return false;
                    const r=el.getBoundingClientRect();
                    return st.display!=='none'&&st.visibility!=='hidden'&&r.width>4&&r.height>4;};
                return Array.from(document.querySelectorAll("button,[role='button'],a,div,span"))
                  .filter(vis)
                  .map(e=>({t:norm(e.textContent).slice(0,45),
                            c:(e.className||'').toString().slice(0,60),
                            cur:getComputedStyle(e).cursor}))
                  .filter(x=>x.t && x.t.length>0 && x.t.length<=45)
                  .filter(x=>x.cur==='pointer' || /delete|confirm|account|remove|permanent/i.test(x.t))
                  .slice(0,30);
            }""")
            try:
                log(f"  [diag] delete page url: {str(self.page.url)[:80]}")
            except Exception:
                pass
            seen = set()
            for it in (items or []):
                key = (it.get("t"), it.get("c"))
                if key in seen:
                    continue
                seen.add(key)
                log(f"  [diag] clickable: '{it.get('t')}' | cursor={it.get('cur')} | class={it.get('c')}")
        except Exception as e:
            log(f"  [diag] dump failed: {str(e)[:80]}")

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
            const isBtn = (e) => e.tagName === "BUTTON" || e.getAttribute("role") === "button"
                || cls(e).includes("clickable") || getComputedStyle(e).cursor === "pointer";
            let el =
                nodes.find((e) => cls(e).includes("confirm-button") && cls(e).includes("type-danger")) ||
                nodes.find((e) => ["delete now", "delete account"].includes(norm(e.textContent)) && cls(e).includes("clickable")) ||
                nodes.find((e) => ["delete now", "delete account", "delete", "confirm"].includes(norm(e.textContent)) && getComputedStyle(e).cursor === "pointer") ||
                // contains-match: a short danger button whose label INCLUDES a delete phrase
                nodes.find((e) => { const t = norm(e.textContent);
                    return t.length <= 28 && isBtn(e)
                        && (t.includes("delete now") || t.includes("delete account")
                            || t.includes("permanently delete") || t.includes("delete my account")
                            || t.includes("confirm delete")); });
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

    async def get_play_info(self, vid: str, tries: int = 8, delay: float = 2.0) -> str:
        """Resolve a vid to a downloadable mp4 URL. dola sometimes returns the
        play-info BEFORE the CDN has a plain URL ready — only an ENCRYPTED main_url
        (base64, with encryption_method/gear_des_key) and no plain http…mp4. Poll a
        few times until the plain mp4 URL appears instead of failing (or worse,
        saving the JSON body as if it were a video)."""
        for i in range(tries):
            r = await self.pf("/samantha/video/get_play_info", {"vid": vid})
            body = r["body"] or ""
            # prefer the structured play_infos[].main (highest-res) — the CDN url has no
            # .mp4 path so _extract_mp4 alone can miss it — then fall back to a plain url.
            url, _def = _best_master_url(body)
            if url:
                return url
            urls = _extract_mp4(body)
            if urls:
                return urls[0]
            if i < tries - 1:
                await asyncio.sleep(delay)
        raise DolaError("get_play_info returned no plain mp4 url after retries")

    async def get_play_info_hd(self, vid: str, tries: int = 8, delay: float = 2.0):
        """Resolve a vid to the highest-quality UNWATERMARKED master mp4 via the MEDIA
        play-info endpoint — the source with NO burned-in watermark (the same one the
        zDola extension pulls; 1080p, ~20–30 MB), vs the low-res stream from
        /samantha/video/get_play_info. dola's /samantha/media/get_play_info takes
        {"key": vid} (NOT "vid"). The response shape varies, so parse BOTH known layouts:
          - data.original_media_info.main_url   (+ meta.definition)   ← the raw master
          - data.play_infos[].main              (+ definition)        ← per-resolution
        and return the URL with the largest frame (highest quality). Returns
        (url, definition) or raises."""
        for i in range(tries):
            r = await self.pf("/samantha/media/get_play_info", {"key": vid})
            url, definition = _best_master_url(r.get("body") or "")
            if url:
                return url, definition
            if i < tries - 1:
                await asyncio.sleep(delay)
        raise DolaError("media/get_play_info returned no master url (HD) after retries")

    async def download(self, url: str, out_path: str, tries: int = 3) -> int:
        """Download via the browser network stack (shares auth/cookies). Returns bytes
        written. Retries transient network blips (e.g. 'socket hang up' from the dola CDN)
        a few times so a flaky download does NOT force a whole re-generation — the video is
        already made; only the transfer failed."""
        last = None
        for i in range(max(1, tries)):
            try:
                resp = await self.ctx.request.get(url)
                data = await resp.body()
                os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
                with open(out_path, "wb") as f:
                    f.write(data)
                return len(data)
            except Exception as e:
                last = e
                if i < tries - 1:
                    await asyncio.sleep(2 * (i + 1))
        raise DolaError(f"download failed after {tries} tries: {str(last)[:100]}")

    # ---- high-level convenience ------------------------------------------------
    async def generate_one(self, prompt: str, out_path: str, *, model="seedance_v2.0",
                           ratio="9:16", duration=10, timeout=720, use_skill=False,
                           ref_image: str | None = None, prompt_duration: bool = False,
                           claimed_vids: set | None = None) -> str:
        """Full pipeline for a single prompt -> saved mp4. Returns out_path.
        use_skill=True routes through the creative-video SKILL (agent rewrites the prompt
        cinematically, then we auto-confirm) instead of the direct ability route.
        ref_image (a LOCAL path) turns this into reference-image → video: the image is
        uploaded via dola's page uploader and attached — this REQUIRES the skill route.
        claimed_vids: a set shared across this account's tabs — a hard DUPLICATE GUARD so
        two prompts can never save the same video (each tab claims its vid atomically)."""
        if use_skill or ref_image:
            attachment = None
            if ref_image:
                self._log(f"uploading reference image: {os.path.basename(str(ref_image))}…")
                attachment = await self.upload_reference_image(ref_image)
            self._log(f"submit (skill /creative-video{'+ref' if attachment else ''}): {str(prompt)[:50]}...")
            conv, section, _raw0 = await self.submit_skill(prompt, ratio=ratio, duration=duration,
                                                           attachment=attachment,
                                                           prompt_duration=prompt_duration)
            try:
                await self.page.goto(f"{DOLA_ORIGIN}/chat/{conv}", wait_until="domcontentloaded")
            except Exception:
                pass
            # Snapshot BEFORE the agent generates so wait_for_video only accepts the new vid.
            seen_before = await self.snapshot_vids(conv)
            last_index, already = await self._await_skill_ready(conv)
            if not already:
                self._log(f"conversation_id={conv}; auto-confirming (\"yes\") to start generation…")
                await self.confirm_skill(conv, section, last_index, "yes")
            else:
                self._log(f"conversation_id={conv}; agent generating directly (no confirm needed)…")
            self._log(f"{len(seen_before)} existing vid(s) ignored; waiting for new video...")
            vid, msg = await self.wait_for_video(conv, timeout=timeout, exclude=seen_before,
                                                 claim_set=claimed_vids)
            self._log(f"vid={vid}")
            return await self._fetch_and_save(vid, msg, out_path)

        self._log(f"submit: {prompt[:50]}...")
        # Land the generation in a fresh conversation so we never pick up an old video…
        seen_before = set()
        conv = await self.submit(prompt, model=model, ratio=ratio, duration=duration)
        # Bring the VISIBLE page to the conversation so you can watch the render
        # (the submit itself is an API call, so the page otherwise stays on the
        # idle create page). Harmless to polling — pf() fetches work on any dola page.
        try:
            await self.page.goto(f"{DOLA_ORIGIN}/chat/{conv}", wait_until="domcontentloaded")
        except Exception:
            pass
        # …and as a belt-and-braces guard, ignore any vids already in that conversation.
        seen_before = await self.snapshot_vids(conv)
        self._log(f"conversation_id={conv}; {len(seen_before)} existing vid(s) ignored; waiting for new video...")
        vid, msg = await self.wait_for_video(conv, timeout=timeout, exclude=seen_before,
                                             claim_set=claimed_vids)
        self._log(f"vid={vid}")
        return await self._fetch_and_save(vid, msg, out_path)

    async def _fetch_and_save(self, vid: str, msg: str, out_path: str) -> str:
        """Resolve a finished vid to an mp4 URL, download it, validate it's a real
        video, and return out_path. Shared by both the ability and skill routes.
        PREFERS the raw UNWATERMARKED 1080p master (media/get_play_info -> main_url);
        falls back to the older watermarked stream. Sets self.last_was_hd so the runner
        can SKIP the ffmpeg watermark-removal when the master (already clean) was used."""
        self.last_was_hd = False
        url = None
        definition = ""
        try:
            url, definition = await self.get_play_info_hd(vid)
            self.last_was_hd = True
        except DolaError:
            try:
                url = await self.get_play_info(vid)
            except DolaError:
                cand = _extract_mp4(msg)                # last resort: url embedded in message
                if not cand:
                    raise
                url = cand[0]
        n = await self.download(url, out_path)
        # Validate: a real dola mp4 is megabytes and starts with an ISO-BMFF box.
        # Guard against saving an error/JSON body (e.g. an expired or not-ready play
        # URL) as if it were a video and wrongly reporting success.
        if not _looks_like_video(out_path):
            try:
                os.remove(out_path)
            except OSError:
                pass
            raise DolaError(f"downloaded content is not a valid video ({n} bytes) — "
                            f"play URL expired/not ready")
        tag = f" [HD master {definition or '1080p'}, unwatermarked]" if self.last_was_hd else ""
        self._log(f"downloaded {n} bytes -> {out_path}{tag}")
        return out_path


# ---- parsing helpers (tolerant of escaped JSON in the message stream) ----------
def _best_master_url(body: str):
    """Pick the highest-quality master mp4 URL + its definition from a get_play_info
    body. Handles both dola layouts: data.original_media_info.main_url and
    data.play_infos[].main (choose the entry with the largest width*height). Returns
    (url, definition) or ('', '')."""
    if not body:
        return "", ""
    try:
        data = (json.loads(body) or {}).get("data", {}) or {}
    except Exception:
        return "", ""
    best_url, best_def, best_area = "", "", -1
    # 1) the raw master (media endpoint)
    omi = data.get("original_media_info") or {}
    u = str(omi.get("main_url") or "")
    if u.startswith("http"):
        meta = omi.get("meta") or {}
        w = int(omi.get("width") or meta.get("width") or 0)
        h = int(omi.get("height") or meta.get("height") or 0)
        best_url, best_def, best_area = u, str(meta.get("definition") or ""), (w * h or 1)
    # 2) per-resolution play_infos[].main — keep the biggest frame
    for pi in (data.get("play_infos") or []):
        u = str(pi.get("main") or pi.get("main_url") or "")
        if not u.startswith("http"):
            continue
        area = int(pi.get("width") or 0) * int(pi.get("height") or 0)
        if area >= best_area:
            best_url, best_def, best_area = u, str(pi.get("definition") or ""), area
    return best_url, best_def


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


def _looks_like_video(path: str) -> bool:
    """True only for a plausible real dola mp4: megabyte-scale and starting with an
    ISO-BMFF box. Rejects tiny/JSON/HTML bodies (expired or not-ready play URLs)."""
    try:
        if os.path.getsize(path) < 50_000:   # real videos are >1MB; this small = error body
            return False
        with open(path, "rb") as f:
            head = f.read(64)
    except OSError:
        return False
    if head[:1] in (b"{", b"<"):             # JSON / HTML error page
        return False
    return b"ftyp" in head or b"moov" in head or b"mdat" in head


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
