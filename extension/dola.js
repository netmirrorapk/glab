/**
 * G-Labs Studio Helper — Dola Module
 *
 * Standalone module loaded via importScripts() in background.js.
 * Talks to DolaBridge at http://127.0.0.1:18927 — completely separate from
 * Flow (18924), Genspark (18925) and Grok (18926). All modes coexist.
 *
 * dola.com (ByteDance / Doubao) video pipeline (reverse-engineered):
 *   1. POST /chat/completion            → conversation_id  (submit prompt)
 *   2. POST /im/chain/single (cmd 3100) → poll until a finished vid appears
 *        (content-type MUST be 'application/json; encoding=utf-8')
 *   3. POST /samantha/video/get_play_info?vid=..  → downloadable mp4 URL
 *   4. fetch the mp4 (tab cookies) → base64 → back to Python
 *
 * No reCAPTCHA — auth is cookie-based and dola's own page JS auto-attaches
 * msToken / a_bogus signing to every fetch made inside the dola.com tab. We
 * only need the base query params (aid, device_id, web_id, ...) which we
 * capture from dola's own network traffic via a fetch monkey-patch.
 *
 * Every fetch runs inside the dola.com tab via
 * chrome.scripting.executeScript(world:"MAIN") so cookies + signing apply.
 */

const DOLA_BRIDGE_URL = "http://127.0.0.1:18927";
const DOLA_POLL_INTERVAL = 1500;
const DOLA_ACCOUNT_DETECT_INTERVAL = 15000;
const DOLA_ORIGIN = "https://www.dola.com";
const DOLA_MAX_PARALLEL = 8;

// ─── State ───
let dolaBridgeConnected = false;
let dolaAccounts = {};              // email -> {email, userId, tab_ids, ...}
let dolaLastPollError = "";
let dolaActiveCount = 0;
let dolaDesiredTabs = 1;            // parallel target from the app (UI slider)
let dolaProfileAccount = null;     // stable {uid, email} for this Chrome profile
let dolaOpeningTabs = false;       // guard so we don't open tabs re-entrantly
let dolaReloginCooldownUntil = 0;  // skip re-login retries for a bit after a failure
let dolaReloginInProgress = false; // serialize re-login across parallel tabs
const dolaTabBusy = {};
const dolaInFlightRequestIds = new Set();

// ═══════════════════════════════════════════════════════════════════
// Base-param capture. dola signs requests with msToken/a_bogus (added by
// the page's own fetch override), but the non-signing base params (aid,
// device_id, web_id, tea_uuid, ...) live in the query string that dola's
// app code builds. We snapshot them from any real dola API request via a
// fetch monkey-patch installed into the tab's MAIN world. Idempotent.
// ═══════════════════════════════════════════════════════════════════
async function dolaInstallCapture(tabId) {
  try {
    return await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: () => {
        const SIGN = new Set(["msToken", "a_bogus", "X-Bogus", "_signature", "x-signature"]);
        const grab = (url) => {
          try {
            if (!url || url.indexOf("dola.com/") === -1 || url.indexOf("?") === -1) return null;
            const q = new URL(url, location.origin).searchParams;
            if (!q.get("device_id") || !q.get("aid")) return null;
            const base = {};
            for (const [k, v] of q.entries()) if (!SIGN.has(k)) base[k] = v;
            return base;
          } catch (e) {
            return null;
          }
        };
        // 1) Install a fetch patch once — captures base params from future requests.
        if (!window.__dolaCaptureInstalled) {
          window.__dolaCaptureInstalled = true;
          const orig = window.fetch;
          window.fetch = function (input) {
            try {
              const url = typeof input === "string" ? input : (input && input.url) || "";
              const b = grab(url);
              // Merge (union) so params like fp / tz_name / web_tab_id that only
              // appear on some endpoints still accumulate into the base set.
              if (b) window.__dolaBase = Object.assign(window.__dolaBase || {}, b);
            } catch (e) {}
            return orig.apply(this, arguments);
          };
        }
        // 2) Retroactive scan of already-made requests via the Performance API —
        //    works even when the page was loaded BEFORE the patch installed.
        if (!window.__dolaBase || !window.__dolaBase.device_id) {
          try {
            const entries = performance.getEntriesByType("resource") || [];
            for (let i = entries.length - 1; i >= 0; i--) {
              const b = grab(entries[i].name || "");
              // Merge across ALL matching entries to accumulate the full param set.
              if (b) window.__dolaBase = Object.assign(window.__dolaBase || {}, b);
            }
          } catch (e) {}
        }
        // 3) Last-resort reconstruction from device ids found in the page
        //    (localStorage / __NEXT_DATA__ / HTML), merged with dola web defaults.
        if (!window.__dolaBase || !window.__dolaBase.device_id) {
          try {
            let blob = "";
            try { for (let i = 0; i < localStorage.length; i++) blob += (localStorage.getItem(localStorage.key(i)) || ""); } catch (e) {}
            try { const nd = document.getElementById("__NEXT_DATA__"); if (nd) blob += nd.textContent || ""; } catch (e) {}
            try { blob += document.documentElement.innerHTML.slice(0, 200000); } catch (e) {}
            const pick = (re) => { const m = blob.match(re); return m ? m[1] : ""; };
            const device_id = pick(/"device_id"\s*:\s*"?(\d{15,25})"?/) || pick(/device_id[=:"']{1,3}(\d{15,25})/);
            const web_id = pick(/"web_id"\s*:\s*"?(\d{15,25})"?/) || pick(/"tea_uuid"\s*:\s*"?(\d{15,25})"?/) || device_id;
            if (device_id) {
              window.__dolaBase = {
                version_code: "20800", language: "en", device_platform: "web",
                doubao_device_platform: "web", aid: "495671", real_aid: "495671",
                pkg_type: "release_version", device_id: device_id,
                pc_version: "3.30.6", doubao_pc_version: "3.30.6",
                web_id: web_id || device_id, tea_uuid: web_id || device_id,
                region: "SG", sys_region: "SG", samantha_web: "1",
                web_platform: "browser", "use-olympus-account": "1",
              };
            }
          } catch (e) {}
        }
        const b = window.__dolaBase || {};
        return { ok: !!b.device_id, device_id: b.device_id || "", web_id: b.web_id || "", keys: Object.keys(b).length };
      },
    }).then((out) => (out && out[0] && out[0].result) || { ok: false });
  } catch (e) {
    return { ok: false, error: String(e && e.message) };
  }
}

// Run a function inside the dola.com tab's MAIN world (cookies + signing apply).
async function dolaExecInTab(tabId, fn, args) {
  try {
    const out = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: fn,
      args: args !== undefined ? [args] : [],
    });
    return out?.[0]?.result ?? null;
  } catch (e) {
    const m = (e && e.message) || "";
    // A closed/navigating tab is expected (e.g. after delete closes extra tabs) —
    // surface it as a sentinel so callers can bail instead of flooding the console.
    if (m.indexOf("No tab with id") !== -1 || m.indexOf("No frame with id") !== -1) return { __tabGone: true };
    console.warn("[Dola] exec in tab failed:", m);
    return null;
  }
}

// ═══════════════════════════════════════════════════════════════════
// Account detection
// ═══════════════════════════════════════════════════════════════════
// Is a Google account actively signed into THIS Chrome profile? These cookies
// exist only while a Google session is live — used to decide whether we can
// silently auto-login dola (dola login = Google OAuth) without any user click.
async function dolaHasGoogleSession() {
  try {
    const groups = await Promise.all([
      chrome.cookies.getAll({ domain: "google.com" }),
      chrome.cookies.getAll({ domain: ".google.com" }),
    ]);
    const names = new Set();
    for (const g of groups) for (const c of g || []) names.add(c.name);
    return ["SAPISID", "__Secure-1PSID", "__Secure-3PSID", "SSID", "SID", "HSID"].some((n) => names.has(n));
  } catch (e) {
    return false;
  }
}

async function dolaDetectAccounts() {
  try {
    // Match both apex (dola.com) and www — Chrome hides "www." in the address
    // bar so the user may be on either host.
    const tabs = await chrome.tabs.query({
      url: [
        "https://www.dola.com/*",
        "https://dola.com/*",
        "http://www.dola.com/*",
        "http://dola.com/*",
      ],
    });
    if (!tabs.length) {
      dolaAccounts = {};
      return;
    }

    // Logged-in check via dola.com session cookies. Query by URL (most reliable
    // — returns cookies that would actually be sent to dola.com, incl. domain
    // cookies) across apex + www + domain filter, then dedupe.
    let loggedIn = false;
    let names = new Set();
    try {
      const groups = await Promise.all([
        chrome.cookies.getAll({ url: "https://www.dola.com/" }),
        chrome.cookies.getAll({ url: "https://dola.com/" }),
        chrome.cookies.getAll({ domain: "dola.com" }),
        chrome.cookies.getAll({ domain: ".dola.com" }),
      ]);
      for (const g of groups) for (const c of g || []) names.add(c.name);
      const LOGIN = ["sessionid", "sessionid_ss", "sid_guard", "sid_tt", "uid_tt", "passport_csrf_token"];
      loggedIn = LOGIN.some((n) => names.has(n));
    } catch (e) {
      console.warn("[Dola] cookie read error:", e && e.message);
    }
    console.log(`[Dola] dola.com cookies (${names.size}): ${[...names].join(", ") || "(none)"} | loggedIn=${loggedIn}`);

    if (!loggedIn) {
      console.log(`[Dola] ${tabs.length} dola tab(s) found but no login cookie matched. If you ARE logged in, tell me the cookie names above.`);
      dolaAccounts = {};
      // PROACTIVE AUTO-LOGIN — the piece that makes a fresh profile "just work":
      // a new Chrome profile has Gmail signed in but has NEVER logged into
      // dola.com, so there are no dola cookies → the account is never detected →
      // no job is dispatched → the job-triggered re-login never fires (a
      // chicken-and-egg deadlock). If a Google session IS active in this profile,
      // drive dola's own "Continue with Google" here (trusted debugger click) so
      // dola cookies appear; the next detect cycle then reports the account and
      // generation starts on its own. Serialized + cooldown-guarded like the
      // job-triggered path so parallel tabs / retries don't stampede.
      try {
        if (!dolaReloginInProgress && Date.now() >= dolaReloginCooldownUntil && (await dolaHasGoogleSession())) {
          dolaReloginInProgress = true;
          try {
            console.log("[Dola] guest dola tab + active Google session → proactive auto-login…");
            const ok = await dolaReLoginSameAccount(tabs[0].id);
            if (!ok) {
              dolaReloginCooldownUntil = Date.now() + 120000; // 2-min cooldown to avoid loops
              console.warn("[Dola] proactive auto-login didn't take — will retry after cooldown.");
            } else {
              dolaProfileAccount = null; // fresh session → re-detect identity next cycle
              console.log("[Dola] proactive auto-login done — account will be detected next cycle.");
            }
          } finally {
            dolaReloginInProgress = false;
          }
        } else if (!(await dolaHasGoogleSession())) {
          console.log("[Dola] guest dola tab but NO active Google session — sign into Google in this profile first.");
        }
      } catch (e) {
        console.warn("[Dola] proactive login error:", e && e.message);
      }
      return;
    }

    // One Chrome profile = one dola account (shared cookies). Group ALL dola.com
    // tabs under a single, stable identity so auto-opened tabs (still loading,
    // no base params yet) don't get split into a separate "dola_user" account.
    let acctUid = dolaProfileAccount ? dolaProfileAccount.uid : "";
    let acctEmail = dolaProfileAccount ? dolaProfileAccount.email : "";
    const tabIds = [];
    for (const tab of tabs) {
      tabIds.push(tab.id);
      try {
        const cap = await dolaInstallCapture(tab.id);
        if (!acctUid && cap && cap.ok) {
          const info = await dolaExecInTab(tab.id, () => {
            const base = window.__dolaBase || {};
            const uid = base.web_id || base.tea_uuid || base.device_id || "";
            let email = "";
            try {
              const em = (document.documentElement?.innerHTML || "").match(/"email"\s*:\s*"([^"<>\s]+@[^"<>\s]+)"/i);
              if (em) email = em[1];
            } catch (e) {}
            return { uid: String(uid || ""), email: email || "" };
          });
          if (info && info.uid) { acctUid = info.uid; acctEmail = info.email || ""; }
        }
      } catch (e) {}
    }
    if (acctUid && !dolaProfileAccount) dolaProfileAccount = { uid: acctUid, email: acctEmail };
    const email = acctEmail || (acctUid ? `dola_${String(acctUid).slice(0, 10)}` : "dola_user");

    const fresh = {};
    fresh[email] = {
      email, userId: acctUid, subscription: "",
      tab_ids: tabIds, tab_id: tabIds[0], last_seen: Date.now(),
    };

    // Prune busy flags for closed tabs.
    const liveTabIds = new Set(tabIds);
    for (const k of Object.keys(dolaTabBusy)) {
      if (!liveTabIds.has(Number(k))) delete dolaTabBusy[k];
    }
    dolaAccounts = fresh;
    console.log(
      `[Dola] detect: ${tabs.length} tab(s), account=${email}, ` +
      `desiredTabs=${dolaDesiredTabs}, bridgeConnected=${dolaBridgeConnected}`
    );

    if (dolaBridgeConnected) {
      try {
        await fetch(`${DOLA_BRIDGE_URL}/dola/accounts`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            accounts: [{
              email, userId: acctUid, subscription: "", tab_count: tabIds.length,
            }],
          }),
        });
      } catch (e) {}
      // Auto-open dola.com tabs to reach the parallel target (each tab = 1 slot).
      dolaEnsureTabs().catch(() => {});
    }
  } catch (e) {
    console.warn("[Dola] Account detection failed:", e.message);
  }
}

// Open background dola.com tabs until we reach the parallel target
// (dolaDesiredTabs). Self-queries the live tab count so it can run from either
// the poll loop (fast) or account detection. Each new tab shares the profile's
// login and becomes another concurrent worker.
async function dolaEnsureTabs() {
  try {
    if (!dolaBridgeConnected || dolaOpeningTabs) return;
    const target = Math.max(1, Math.min(dolaDesiredTabs || 1, DOLA_MAX_PARALLEL));
    if (target <= 1) return;
    const tabs = await chrome.tabs.query({
      url: ["https://www.dola.com/*", "https://dola.com/*", "http://www.dola.com/*", "http://dola.com/*"],
    });
    const have = tabs.length;
    if (have >= target) return;
    dolaOpeningTabs = true;
    const toOpen = target - have;
    for (let i = 0; i < toOpen; i++) {
      try { await chrome.tabs.create({ url: `${DOLA_ORIGIN}/`, active: false }); } catch (e) {}
      await new Promise((r) => setTimeout(r, 400));
    }
    console.log(`[Dola] auto-opened ${toOpen} tab(s) → parallel target ${target} (had ${have}).`);
  } catch (e) {
    console.warn("[Dola] ensure tabs failed:", e && e.message);
  } finally {
    dolaOpeningTabs = false;
  }
}

// ═══════════════════════════════════════════════════════════════════
// Bridge polling
// ═══════════════════════════════════════════════════════════════════
async function dolaPollBridge() {
  try {
    const emails = Object.keys(dolaAccounts);
    const accountsParam = emails.length
      ? `?accounts=${encodeURIComponent(emails.join(","))}`
      : "";
    const resp = await fetch(`${DOLA_BRIDGE_URL}/dola/poll${accountsParam}`, {
      method: "GET",
      headers: { Accept: "application/json" },
    });
    if (!resp.ok) {
      dolaBridgeConnected = false;
      dolaLastPollError = `HTTP ${resp.status}`;
      return;
    }
    dolaBridgeConnected = true;
    dolaLastPollError = "";
    const data = await resp.json();
    if (typeof data.desired_tabs === "number" && data.desired_tabs >= 1) {
      dolaDesiredTabs = data.desired_tabs;
      // Open extra tabs promptly (self-guarded) instead of waiting for the 15s
      // account-detect cycle — so parallel spins up fast.
      if (dolaDesiredTabs > 1) dolaEnsureTabs().catch(() => {});
    }

    if (data.work && dolaActiveCount < DOLA_MAX_PARALLEL) {
      const rid = data.work.request_id;
      if (dolaInFlightRequestIds.has(rid)) return;
      dolaInFlightRequestIds.add(rid);
      dolaActiveCount++;
      (async () => {
        try {
          await dolaHandleWork(data.work);
        } catch (e) {
          console.warn("[Dola] handleWork threw:", e.message);
          try {
            await dolaSubmitResult(rid, { error: `handler_crash: ${e.message}` });
          } catch (e2) {}
        } finally {
          dolaActiveCount--;
          dolaInFlightRequestIds.delete(rid);
        }
      })();
    }
  } catch (e) {
    dolaBridgeConnected = false;
    dolaLastPollError = e.message || "fetch failed";
  }
}

// ═══════════════════════════════════════════════════════════════════
// Work execution — end-to-end dola video generation
// ═══════════════════════════════════════════════════════════════════
async function dolaHandleWork(work) {
  const {
    request_id, account, command, prompt, model, ratio, duration,
    reference_image_base64, reference_image_filename, reference_image_mime,
  } = work;

  const acc = dolaAccounts[account] || Object.values(dolaAccounts)[0];
  if (!acc || !acc.tab_ids || !acc.tab_ids.length) {
    await dolaSubmitResult(request_id, { error: "no_dola_tab" });
    return;
  }

  // Pick a free tab for this account.
  let tabId = null;
  for (const tid of acc.tab_ids) {
    if (!dolaTabBusy[tid]) {
      tabId = tid;
      break;
    }
  }
  if (tabId === null) {
    // All tabs busy — report progress so the bridge doesn't idle-timeout, then bail
    // to be re-dispatched on the next poll cycle.
    await dolaReportProgress(request_id, "tab_wait", "all dola tabs busy");
    return;
  }
  dolaTabBusy[tabId] = true;

  try {
    await dolaReportProgress(request_id, "started", (prompt || "").slice(0, 40));

    // Auto-delete command — drive dola's /delete-account page in this tab.
    if (command === "delete_account") {
      const delRes = await dolaDeleteInTab(request_id, tabId);
      await dolaSubmitResult(request_id, delRes);
      return;
    }

    // Login check FIRST (fast cookie check). If logged out, auto re-login the
    // SAME Google account (clicks Log In → Continue with Google) before doing
    // anything — this heals a session that expired or was left logged out by a
    // prior delete. Done before base-params since re-login navigates the tab.
    if (!(await dolaIsLoggedIn(tabId))) {
      // Serialize re-login: only ONE tab drives the Google login at a time.
      // Parallel jobs starting from a logged-out state would otherwise fire
      // competing debugger clicks that all fail. Others WAIT for the winner, then
      // reload their own tab to pick up the shared (now logged-in) session.
      if (dolaReloginInProgress) {
        await dolaReportProgress(request_id, "relogin", "waiting for another tab's login");
        for (let i = 0; i < 60 && dolaReloginInProgress; i++) await new Promise((r) => setTimeout(r, 1500));
        try { await chrome.tabs.reload(tabId); } catch (e) {}
        await new Promise((r) => setTimeout(r, 3000));
        if (!(await dolaIsLoggedIn(tabId))) {
          await dolaSubmitResult(request_id, { error: "not_logged_in (shared re-login didn't take on this tab)" });
          return;
        }
      } else if (Date.now() < dolaReloginCooldownUntil) {
        await dolaSubmitResult(request_id, { error: "not_logged_in (re-login recently failed — check Google session)" });
        return;
      } else {
        dolaReloginInProgress = true;
        try {
          await dolaReportProgress(request_id, "relogin", "logged out — auto re-logging in same account");
          const ok = await dolaReLoginSameAccount(tabId);
          if (!ok || !(await dolaIsLoggedIn(tabId))) {
            dolaReloginCooldownUntil = Date.now() + 120000; // 2-min cooldown to avoid loops
            await dolaSubmitResult(request_id, { error: "not_logged_in (auto re-login failed — check Google session)" });
            return;
          }
          dolaProfileAccount = null; // new session → re-detect identity next cycle
          try { await chrome.tabs.update(tabId, { url: `${DOLA_ORIGIN}/chat/create-video` }); } catch (e) {}
          // Longer settle: after a delete+re-login dola's backend needs a moment to
          // finish provisioning the FRESH account (and its reset daily quota).
          // Submitting too early lands on a half-provisioned session → a spurious
          // daily_limit. 5s is comfortably past that window.
          await new Promise((r) => setTimeout(r, 5000));
          console.log("[Dola] re-login done — session settling before submit.");
        } finally {
          dolaReloginInProgress = false;
        }
      }
    }

    // Ensure base params are captured before we build API calls. Install the
    // capture each loop (idempotent) so it re-captures after a re-login navigated
    // the tab (fresh page → fresh window → __dolaBase must be rebuilt).
    let haveBase = false;
    for (let i = 0; i < 30; i++) {
      const cap = await dolaInstallCapture(tabId);
      if (cap && cap.ok) { haveBase = true; break; }
      await new Promise((r) => setTimeout(r, 500));
    }
    if (!haveBase) {
      await dolaSubmitResult(request_id, { error: "no_base_params (open/refresh a logged-in dola.com tab)" });
      return;
    }

    // 0) Optional reference image → upload via dola's OWN uploader (its ImageX
    //    SDK does the signed upload), then capture the resulting uri.
    let refUri = "";
    if (reference_image_base64) {
      await dolaReportProgress(request_id, "uploading_ref", reference_image_filename || "");
      const up = await dolaExecInTab(tabId, dolaMainUploadRef, {
        base64: reference_image_base64,
        filename: reference_image_filename || "ref.png",
        mime: reference_image_mime || "image/png",
      });
      if (!up || !up.uri) {
        if (up && up.diag) console.log("[Dola] ref upload diag:", JSON.stringify(up.diag));
        const diagStr = up && up.diag ? ` | diag=${JSON.stringify(up.diag).slice(0, 300)}` : "";
        await dolaSubmitResult(request_id, { error: `ref_upload_failed: ${(up && up.error) || "no uri"}${diagStr}` });
        return;
      }
      refUri = up.uri;
      await dolaReportProgress(request_id, "ref_uploaded", refUri);
    }

    // Dismiss the age-confirm modal if dola is showing it (blocks generation).
    await dolaConfirmAge(tabId);

    // 1) Submit → conversation_id + backend verdict
    await dolaReportProgress(request_id, "submit", refUri ? "with ref" : "");
    const sub = await dolaExecInTab(tabId, dolaMainSubmit, {
      prompt, model, ratio, duration,
      refUri, refName: reference_image_filename || "",
    });
    // dolaMainSubmit now returns { convId, gen } on success, or { error }.
    // The daily-limit / no-points verdict is decided from the submit SSE
    // itself — so it fails here in ~2s instead of after a long poll.
    if (!sub || sub.error || !sub.convId) {
      await dolaSubmitResult(request_id, { error: (sub && sub.error) ? sub.error : "submit_failed: no conversation_id" });
      return;
    }
    const convId = sub.convId;
    // Backend already CONFIRMED a video is being produced ("the video will be
    // generated / ready in N minutes") — so the poll can trust it and must not
    // fast-fail with "not started" while dola renders.
    const genConfirmedAtSubmit = !!sub.gen;

    // 2) Poll → vid
    await dolaReportProgress(request_id, "polling", `conv=${convId}${genConfirmedAtSubmit ? " (gen confirmed)" : ""}`);
    let vid = "";
    let lastMsg = "";
    const deadline = Date.now() + 720000; // 12 min — dola's 3rd parallel video can render slowly; don't give up early
    let seen = new Set();
    // snapshot existing vids first so we only accept a NEW one
    const snap = await dolaExecInTab(tabId, dolaMainPull, { convId });
    if (snap && snap.vids) seen = new Set(snap.vids);
    let cyc = 0;
    let sawGenerating = genConfirmedAtSubmit;
    let lastPointsLeft = null;   // backend "N points left today" — 0 ⇒ retire account after this gen
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 3500));
      const pull = await dolaExecInTab(tabId, dolaMainPull, { convId });
      if (pull && pull.__tabGone) { await dolaSubmitResult(request_id, { error: "tab_closed_during_generation" }); return; }
      if (!pull) continue;
      lastMsg = pull.text || "";
      if (pull.pointsLeft !== null && pull.pointsLeft !== undefined) lastPointsLeft = pull.pointsLeft;
      // Definitive fast-fails — no more polling a job that will never produce a video.
      if (pull.notLoggedIn) { await dolaSubmitResult(request_id, { error: "not_logged_in (account logged out — cannot generate)" }); return; }
      if (pull.limit) { await dolaSubmitResult(request_id, { error: "daily_limit_reached" }); return; }
      if (pull.refused) { await dolaSubmitResult(request_id, { error: "generation_refused" }); return; }
      const fresh = (pull.vids || []).filter((v) => !seen.has(v));
      if (fresh.length) { vid = fresh[0]; break; }
      if (pull.genVideo) sawGenerating = true;  // POSITIVE proof a video is being produced
      cyc++;
      // Accurate: if a while has passed with NO "generating video" proof, the
      // submit didn't actually start a video → fail fast instead of 5-min wait.
      if (!sawGenerating) {
        if (pull.gotImages && cyc >= 3) {
          await dolaSubmitResult(request_id, { error: "got_images_not_video (submit produced images, not a video)" });
          return;
        }
        if (cyc >= 8 && (pull.text || "").length > 150) {
          await dolaSubmitResult(request_id, { error: "video_generation_not_started (no 'generating video' signal — login/error?)" });
          return;
        }
      }
      if (cyc % 5 === 1) {
        console.log(
          `[Dola] poll conv=${String(convId).slice(-8)}: len=${(pull.text || "").length}, ` +
          `vids=${(pull.vids || []).length}, genVideo=${sawGenerating}, ` +
          `images=${!!pull.gotImages}, notLoggedIn=${!!pull.notLoggedIn}, limit=${!!pull.limit}`
        );
      }
      await dolaReportProgress(request_id, "generating", sawGenerating ? "confirmed" : "waiting");
    }
    if (!vid) {
      await dolaSubmitResult(request_id, { error: "video_not_ready_timeout" });
      return;
    }

    // 3) get_play_info → mp4 url
    await dolaReportProgress(request_id, "resolving", `vid=${vid}`);
    let mp4 = await dolaExecInTab(tabId, dolaMainPlayInfo, { vid });
    if ((!mp4 || typeof mp4 !== "string") && lastMsg) {
      // fallback: url embedded in the message stream
      const cand = await dolaExecInTab(tabId, dolaMainExtractMp4, { text: lastMsg });
      if (cand && typeof cand === "string") mp4 = cand;
    }
    if (!mp4 || typeof mp4 !== "string") {
      await dolaSubmitResult(request_id, { error: "no_mp4_url" });
      return;
    }

    // 4) Download bytes in-tab (shares cookies) → base64
    await dolaReportProgress(request_id, "downloading", "");
    const dl = await dolaExecInTab(tabId, dolaMainDownload, { url: mp4 });
    if (dl && dl.base64) {
      await dolaSubmitResult(request_id, {
        success: true,
        vid,
        video_url: mp4,
        video_base64: dl.base64,
        video_size: dl.size || 0,
        content_type: dl.contentType || "video/mp4",
        points_left: lastPointsLeft,
      });
    } else {
      // Couldn't fetch bytes in-tab — hand the URL to Python to try.
      await dolaSubmitResult(request_id, { success: true, vid, video_url: mp4, points_left: lastPointsLeft });
    }
  } finally {
    dolaTabBusy[tabId] = false;
  }
}

// ─── MAIN-world pipeline functions (self-contained; run inside dola.com tab) ───

function dolaMainSubmit(args) {
  const { prompt, model, ratio, duration, refUri, refName } = args;
  const base = window.__dolaBase || {};
  // dola's Video-mode UI prefixes the prompt with "Generated video: " — this is
  // the signal that makes the backend produce a VIDEO. Without it, dola treats
  // the prompt as an IMAGE request even with ability_type 17. (Confirmed across
  // all real video HARs.)
  // Idempotent + robust: strip ANY number of leading "Generated video:" prefixes
  // (and surrounding whitespace) first, then add exactly one. Guards against a
  // re-queued/echoed prompt arriving already-prefixed (which produced the
  // malformed "Generated video: Generated video: …" that dola then refused).
  let _p = String(prompt || "");
  while (/^\s*generated video:\s*/i.test(_p)) _p = _p.replace(/^\s*generated video:\s*/i, "");
  // ★ Append the aspect ratio to the PROMPT TEXT (e.g. "…, 16:9"). dola's web UI
  // does exactly this, and it is what actually ENFORCES the output aspect for
  // text-to-video. ability_param.ratio alone is a weak hint the Seedance model
  // sometimes ignores — inferring the aspect from the prompt content instead (a
  // "close-up of a palm" came out 9:16 despite ratio:16:9). Strip any trailing
  // ratio already present, then append the requested one exactly once.
  _p = _p.replace(/\s*,\s*\d{1,2}\s*:\s*\d{1,2}\s*$/, "").trim();
  const _ratio = String(ratio || "").trim();
  const promptText = "Generated video: " + _p + (_ratio ? ", " + _ratio : "");
  const uuid = () =>
    "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
      const r = (Math.random() * 16) | 0;
      return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
    });
  // FULL option + FULL ext — matches the real dola.com web VIDEO request
  // captured in www.dola.com.har (ability_type 17, seedance_v2.0). This is the
  // authoritative body for video generation.
  const now = Math.floor(Date.now() / 1000);
  const option = {
    send_message_scene: "", create_time_ms: now * 1000, collect_id: "", is_audio: false,
    answer_with_suggest: false, tts_switch: false, need_deep_think: 0,
    click_clear_context: false, from_suggest: false, is_regen: false, is_replace: false,
    is_from_click_option: false, is_from_click_softlink: false, disable_sse_cache: false,
    select_text_action: "", is_select_text: false, resend_for_regen: false, scene_type: 0,
    unique_key: uuid(), start_seq: 0, need_create_conversation: true,
    conversation_init_option: { need_ack_conversation: true }, regen_query_id: [],
    edit_query_id: [], regen_instruction: "", no_replace_for_regen: false, message_from: 0,
    shared_app_name: "", shared_app_id: "", sse_recv_event_options: { support_chunk_delta: true },
    is_ai_playground: false, is_old_user: false,
    recovery_option: { is_recovery: false, req_create_time_sec: now, append_sse_event_scene: 0 },
    message_storage_type: 0, related_deleted_message_ids: {},
  };
  const promptMsg = {
    local_message_id: uuid(),
    content_block: [{
      block_type: 10000,
      content: { text_block: { text: promptText, icon_url: "", icon_url_dark: "", summary: "" }, pc_event_block: "" },
      block_id: uuid(), parent_id: "", meta_info: [], append_fields: [],
    }],
    message_status: 0,
  };

  let messages, abilityParam, ext;
  if (refUri) {
    // Reference → video: an attachment message (block_type 10052 with the
    // uploaded image uri) followed by the prompt message. ratio is omitted from
    // ability_param (dola derives it from the image). Matches www.dola.comref 3.har.
    const attachMsg = {
      local_message_id: uuid(),
      content_block: [{
        block_type: 10052,
        content: {
          attachment_block: { attachments: [{
            type: 1, identifier: uuid(),
            image: { name: refName || "ref.png", uri: refUri,
              image_ori: { url: "", width: 0, height: 0, format: "", url_formats: {} } },
            parse_state: 0, review_state: 1, upload_status: 1, progress: 100, src: "",
          }] },
          pc_event_block: "",
        },
        block_id: uuid(), parent_id: "", meta_info: [], append_fields: [],
      }],
      message_status: 0,
    };
    messages = [attachMsg, promptMsg];
    // Include the requested ratio for reference video too — dola's web omits it
    // (defaults to the image's aspect), but sending it lets the user force a ratio
    // (e.g. 16:9 output from a portrait reference). Harmless if dola ignores it.
    abilityParam = JSON.stringify({ ratio, model, duration });
    ext = { answer_with_suggest: "0", sub_conv_firstmet_type: "1", collection_id: uuid(),
      conversation_init_option: '{"need_ack_conversation":true}', commerce_credit_config_enable: "0" };
  } else {
    messages = [promptMsg];
    abilityParam = JSON.stringify({ ratio, model, duration });
    ext = { answer_with_suggest: "0", sub_conv_firstmet_type: "1", collection_id: "",
      conversation_init_option: '{"need_ack_conversation":true}', commerce_credit_config_enable: "0" };
  }

  const body = {
    client_meta: { local_conversation_id: `local_${Date.now()}`, conversation_id: "",
      bot_id: "7339470689562525703", last_section_id: "", last_message_index: null },
    messages,
    option,
    chat_ability: { ability_type: 17, ability_param: abilityParam },
    user_context: [],
    ext,
  };
  const qs = new URLSearchParams(base).toString();
  return fetch(`https://www.dola.com/chat/completion?${qs}`, {
    method: "POST", credentials: "include",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  })
    .then((r) => r.text().then((t) => ({ status: r.status, body: t })))
    .then((res) => {
      if (res.status !== 200) return { error: `HTTP ${res.status}: ${(res.body || "").slice(0, 160)}` };
      const raw = res.body || "";
      const m = raw.match(/"conversation_id":"(\d+)"/);
      const convId = m ? m[1] : "";
      // ── BACKEND-AUTHORITATIVE VERDICT (from the submit SSE itself) ──
      // dola streams its FULL first reply in this /chat/completion response.
      // The assistant's text_block content is definitive — no need to poll and
      // race the "Generating…" loading_block against the real answer:
      //   • daily limit   → "reached the daily limit" / "try again tomorrow"
      //   • no points left → "only have N left today" / "video points" + "left"
      //   • real gen start → "the video will be generated" / "ready in ... minute"
      // We read this instantly so limit/points fail in ~2s instead of a 12-min
      // poll, and a confirmed gen skips the "not started" false-negative checks.
      const low = raw.toLowerCase();
      // ★ BACKEND-AUTHORITATIVE gen flag. dola's SSE sets ext.has_video_gen="1"
      // ONLY when it actually QUEUES a video task. Verified across HARs: present
      // in every real generation (incl. the "0 points left but still generated"
      // success), ABSENT in every daily-limit / no-video reply. This is far more
      // reliable than the optimistic "the video will be generated" TEXT, which
      // dola prints even when it then refuses ("I can't generate … no points
      // were used"). We key the verdict on this structured flag.
      const hasVideoGen = raw.indexOf('"has_video_gen":"1"') !== -1;
      const isLimit =
        low.indexOf("reached the daily limit") !== -1 ||
        low.indexOf("daily limit for video") !== -1 ||
        (low.indexOf("daily limit") !== -1 && low.indexOf("try again tomorrow") !== -1);
      const isPoints =
        (low.indexOf("left today") !== -1 &&
          (low.indexOf("video point") !== -1 || low.indexOf("only have") !== -1)) ||
        (low.indexOf("not enough") !== -1 && low.indexOf("point") !== -1) ||
        low.indexOf("insufficient") !== -1;
      // dola sometimes OPTIMISTICALLY streams "the video will be generated …"
      // and then CORRECTS itself with "I can't generate the video. No points
      // were used." (the last-points edge case). This refusal must WIN over the
      // optimistic gen text, else the poll trusts gen=confirmed and fake-polls
      // for the full 12 min while no video ever comes. Check it BEFORE genOk.
      const cantGen =
        low.indexOf("can't generate the video") !== -1 ||
        low.indexOf("cannot generate the video") !== -1 ||
        low.indexOf("couldn't generate the video") !== -1 ||
        low.indexOf("could not generate the video") !== -1 ||
        low.indexOf("unable to generate the video") !== -1 ||
        low.indexOf("no points were used") !== -1;
      const genOk =
        low.indexOf("the video will be generated") !== -1 ||
        low.indexOf("video will be generated") !== -1 ||
        low.indexOf("will be ready in") !== -1 ||
        low.indexOf("ready in 1-3") !== -1 ||
        low.indexOf("generating your video") !== -1 ||
        low.indexOf('"has_video_gen":"1"') !== -1;
      // Content-moderation refusal — dola REFUSED this specific prompt (e.g.
      // copyrighted characters, disallowed content): "I can't generate the
      // requested content. Try something else." This is NOT a quota problem —
      // the account is fine, only THIS prompt is bad. Must be a distinct error
      // so dola_mode fails just this job and does NOT mark the account exhausted.
      const contentRefused =
        low.indexOf("generate the requested content") !== -1 ||
        low.indexOf("create the requested content") !== -1 ||
        low.indexOf("try something else") !== -1 ||
        low.indexOf("can't generate the requested") !== -1 ||
        low.indexOf("cannot generate the requested") !== -1 ||
        low.indexOf("against our content policy") !== -1 ||
        low.indexOf("violates our") !== -1;
      // Priority is deliberate — structured flag beats optimistic text:
      // 0) content refusal → fail THIS job (definitive; account stays usable)
      if (contentRefused) return { error: "content_refused", detail: "submit_verdict:content_moderation" };
      // 1) explicit daily-limit text  → fail (definitive)
      if (isLimit) return { error: "daily_limit_reached", detail: "submit_verdict:daily_limit" };
      // 2) explicit refusal ("I can't generate the video / no points were used")
      //    → fail. This WINS over everything below, incl. any stray gen text.
      if (cantGen) return { error: "daily_limit_reached", detail: "submit_verdict:cant_generate" };
      if (!convId) return { error: "no conversation_id in SSE" };
      // 3) backend actually QUEUED a video (has_video_gen="1") → real generation,
      //    even if the reply also says "you only have N left today" (that text
      //    accompanies successful last-point gens too). Poll with full confidence.
      if (hasVideoGen) return { convId, gen: true, detail: "gen_confirmed(has_video_gen)" };
      // 4) no video queued + a points warning → account is out of quota → fail.
      if (isPoints) return { error: "daily_limit_reached", detail: "submit_verdict:no_points_left" };
      // 5) no video queued + no positive gen text at all → AMBIGUOUS. This is NOT
      //    a confirmed daily-limit (those always carry the "reached the daily
      //    limit / try again tomorrow" text, caught by isLimit above) — it's more
      //    likely a refusal whose exact wording we didn't match. Return a DISTINCT
      //    error so dola_mode fails just this job and does NOT mark the whole
      //    account exhausted (which would wrongly skip an account that still has
      //    quota but merely refused one prompt).
      if (!genOk) return { error: "no_video_gen", detail: "submit_verdict:no_video_queued" };
      // 6) optimistic gen TEXT but no backend flag → uncertain. Poll but with
      //    gen:false so the "not started" fast-check bails in ~28s if no vid comes
      //    (instead of a doomed 12-min poll).
      return { convId, gen: false, detail: "gen_text_only" };
    })
    .catch((e) => ({ error: "ERR:" + e }));
}

function dolaMainPull(args) {
  const { convId } = args;
  const base = window.__dolaBase || {};
  const uuid = () =>
    "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
      const r = (Math.random() * 16) | 0;
      return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
    });
  const body = {
    cmd: 3100, uplink_body: { pull_singe_chain_uplink_body: {
      conversation_id: convId, anchor_index: 9007199254740991, conversation_type: 3,
      direction: 1, limit: 20,
      ext: { sync_scenario: "SendBot|flow.agent.creation", pull_single_chain_scene: "multi_device_red_dot_sync" },
      filter: { index_list: [] }, evaluate_ab_params: "", evaluate_common_params: "" } },
    sequence_id: uuid(), channel: 2, version: "1",
  };
  const qs = new URLSearchParams(base).toString();
  const extractVids = (text) => {
    if (!text) return [];
    let found = (text.match(/vid\\*"?\s*:\s*\\*"?(v[0-9a-z]{24,})/g) || [])
      .map((s) => (s.match(/(v[0-9a-z]{24,})/) || [])[1]).filter(Boolean);
    if (!found.length) found = (text.match(/\b(v[0-9a-z]{30,})\b/g) || []);
    const seen = new Set(); const out = [];
    for (const v of found) { if (!seen.has(v)) { seen.add(v); out.push(v); } }
    return out;
  };
  return fetch(`https://www.dola.com/im/chain/single?${qs}`, {
    method: "POST", credentials: "include",
    headers: { "content-type": "application/json; encoding=utf-8" },
    body: JSON.stringify(body),
  })
    .then((r) => r.text())
    .then((t) => {
      const low = (t || "").toLowerCase();
      // Backend tells us EXACTLY how many video points remain today, e.g.
      // "You still have 0 points left today." / "only have 2 left today".
      // This is authoritative (not a guess) — 0 means the next gen WILL fail,
      // so we can retire the account right after the current gen finishes
      // instead of wasting a submit that hits the daily limit.
      let pointsLeft = null;
      const pm = low.match(/have\s+(\d+)\s+(?:video\s+)?points?\s+left\s+today/) ||
                 low.match(/only\s+have\s+(\d+)\s+left\s+today/) ||
                 low.match(/(\d+)\s+points?\s+left\s+today/);
      if (pm) pointsLeft = parseInt(pm[1], 10);
      return {
        text: t || "",
        pointsLeft,
        vids: extractVids(t || ""),
        limit:
          low.indexOf("daily limit for video generation") !== -1 ||
          low.indexOf("reached the daily limit") !== -1 ||
          (low.indexOf("daily limit") !== -1 && low.indexOf("try again tomorrow") !== -1) ||
          // When the account is capped dola sometimes replies with a confirmation
          // prompt ("...longer than 10 seconds is not supported. Do you want to
          // continue generating a 10-second video?") instead of generating — it
          // never produces a vid, so treat it like the daily limit (fail fast).
          low.indexOf("do you want to continue generating") !== -1 ||
          low.indexOf("longer than 10 seconds is not supported") !== -1 ||
          // dola optimistically says "the video will be generated" then CORRECTS
          // with "I can't generate the video. No points were used." — if we only
          // gen-confirmed at submit, the poll would spin the full 12 min. Catch
          // the correction here too so a fake-poll dies immediately.
          low.indexOf("can't generate the video") !== -1 ||
          low.indexOf("cannot generate the video") !== -1 ||
          low.indexOf("couldn't generate the video") !== -1 ||
          low.indexOf("no points were used") !== -1 ||
          low.indexOf("unable to generate the video") !== -1,
        refused: ["temporarily unable to generate", "unable to generate a video", "please try entering other requirements", "not able to create", "generate the requested content", "create the requested content", "try something else", "against our content policy", "violates our"].some((m) => low.indexOf(m) !== -1),
        // Logged out / guest — dola accepts the message but generates nothing.
        notLoggedIn:
          low.indexOf("not available for guests") !== -1 ||
          low.indexOf("log in to start creating") !== -1 ||
          low.indexOf("login required") !== -1 ||
          low.indexOf("please log in") !== -1 ||
          low.indexOf("please sign in") !== -1,
        // POSITIVE proof a video is actually being produced.
        genVideo:
          low.indexOf("generating video") !== -1 ||
          low.indexOf("video_block") !== -1 ||
          low.indexOf('"creation_block"') !== -1 ||
          low.indexOf("creation_loading_block") !== -1,
        // dola produced IMAGES instead of a video (wrong intent).
        gotImages:
          (low.indexOf("gen_image_block") !== -1 || low.indexOf('"image_block":{') !== -1) &&
          low.indexOf("generating video") === -1,
      };
    })
    .catch(() => null);
}

function dolaMainPlayInfo(args) {
  const { vid } = args;
  const base = window.__dolaBase || {};
  const qs = new URLSearchParams(base).toString();
  const extractMp4 = (text) => {
    if (!text) return "";
    const t = text.replace(/\\u0026/g, "&").replace(/\\\//g, "/").replace(/\\"/g, '"');
    const urls = (t.match(/https?:\/\/[^\s"\\]+?\.mp4[^\s"\\]*/g) || [])
      .concat(t.match(/https?:\/\/v16-dola[^\s"\\]+/g) || [])
      .concat(t.match(/https?:\/\/vod-urls[^\s"\\]+/g) || []);
    return urls.length ? urls[0] : "";
  };
  return fetch(`https://www.dola.com/samantha/video/get_play_info?${qs}`, {
    method: "POST", credentials: "include",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ vid }),
  })
    .then((r) => r.text())
    .then((t) => extractMp4(t) || { error: "no mp4" })
    .catch((e) => ({ error: "ERR:" + e }));
}

function dolaMainExtractMp4(args) {
  const t0 = args.text || "";
  const t = t0.replace(/\\u0026/g, "&").replace(/\\\//g, "/").replace(/\\"/g, '"');
  const urls = (t.match(/https?:\/\/[^\s"\\]+?\.mp4[^\s"\\]*/g) || [])
    .concat(t.match(/https?:\/\/v16-dola[^\s"\\]+/g) || [])
    .concat(t.match(/https?:\/\/vod-urls[^\s"\\]+/g) || []);
  return urls.length ? urls[0] : "";
}

function dolaMainDownload(args) {
  return fetch(args.url, { credentials: "include" })
    .then((r) => r.blob().then((b) => ({ blob: b, ct: r.headers.get("content-type") || "video/mp4" })))
    .then(({ blob, ct }) =>
      new Promise((resolve) => {
        const fr = new FileReader();
        fr.onloadend = () => {
          const s = String(fr.result || "");
          const b64 = s.indexOf(",") !== -1 ? s.slice(s.indexOf(",") + 1) : s;
          resolve({ base64: b64, size: blob.size, contentType: ct });
        };
        fr.onerror = () => resolve(null);
        fr.readAsDataURL(blob);
      })
    )
    .catch(() => null);
}

// Upload a reference image using dola's OWN uploader (its ImageX SDK handles the
// signed prepare→apply→upload→commit flow — we don't reproduce the signing). We
// feed the file into dola's file input, then capture the final image `uri` from
// the CommitImageUpload response (patching both fetch and XHR since the SDK may
// use either). Returns { uri } or { error }.
function dolaMainUploadRef(args) {
  const { base64, filename, mime } = args;
  return new Promise(async (resolve) => {
    try {
      const grabUri = (t) => {
        if (!t) return "";
        const m = t.match(/"Uri"\s*:\s*"(tos-[^"]+)"/) || t.match(/"ImageUri"\s*:\s*"(tos-[^"]+)"/);
        return m ? m[1] : "";
      };
      // Patch fetch (once) to capture CommitImageUpload responses.
      if (!window.__dolaUpFetch) {
        window.__dolaUpFetch = true;
        const orig = window.fetch;
        window.fetch = function (input) {
          const url = typeof input === "string" ? input : (input && input.url) || "";
          const p = orig.apply(this, arguments);
          if (url.indexOf("CommitImageUpload") !== -1) {
            try {
              p.then((r) => { r.clone().text().then((t) => { const u = grabUri(t); if (u) window.__dolaUploadedUri = u; }).catch(() => {}); }).catch(() => {});
            } catch (e) {}
          }
          return p;
        };
      }
      // Patch XHR (once) too — ByteDance SDKs often use XMLHttpRequest.
      if (!window.__dolaUpXhr) {
        window.__dolaUpXhr = true;
        const OpenOrig = XMLHttpRequest.prototype.open;
        const SendOrig = XMLHttpRequest.prototype.send;
        XMLHttpRequest.prototype.open = function (m, u) { this.__dolaUrl = u; return OpenOrig.apply(this, arguments); };
        XMLHttpRequest.prototype.send = function () {
          try {
            if (this.__dolaUrl && String(this.__dolaUrl).indexOf("CommitImageUpload") !== -1) {
              this.addEventListener("load", function () { try { const u = grabUri(this.responseText || ""); if (u) window.__dolaUploadedUri = u; } catch (e) {} });
            }
          } catch (e) {}
          return SendOrig.apply(this, arguments);
        };
      }
      window.__dolaUploadedUri = "";

      // base64 → File
      const bin = atob(base64);
      const arr = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
      const file = new File([arr], filename || "ref.png", { type: mime || "image/png" });

      // Deep search for file inputs, traversing open shadow roots too.
      const deepFileInputs = () => {
        const out = [];
        const walk = (root) => {
          if (!root || !root.querySelectorAll) return;
          try { root.querySelectorAll('input[type="file"]').forEach((el) => out.push(el)); } catch (e) {}
          try { root.querySelectorAll("*").forEach((el) => { if (el.shadowRoot) walk(el.shadowRoot); }); } catch (e) {}
        };
        walk(document);
        return out;
      };
      const findInput = () => {
        const inputs = deepFileInputs();
        return inputs.find((i) => (i.accept || "").indexOf("image") !== -1) || inputs.find((i) => !i.accept) || inputs[0] || null;
      };
      // Candidate "reveal" controls (the compose "+" / add-image button).
      const revealControls = () => {
        const isVis = (el) => {
          try { const r = el.getBoundingClientRect(); return r.width > 6 && r.height > 6; } catch (e) { return false; }
        };
        return Array.from(document.querySelectorAll('button, [role="button"], label, [class*="material-symbols"], i, span'))
          .filter(isVis)
          .filter((b) => {
            const t = ((b.textContent || "") + " " +
              ((b.getAttribute && (b.getAttribute("aria-label") || b.getAttribute("title"))) || "")).toLowerCase().trim();
            return /add_photo|add_2|add_circle|attach_file|\bupload\b|\bimage\b|\battach\b|reference|^\+$|^add$/.test(t);
          });
      };

      let input = findInput();
      // Try to reveal a dynamically-created input by clicking add/upload controls,
      // re-querying for up to ~9s.
      if (!input) {
        const clicked = [];
        const ctrls = revealControls();
        for (const b of ctrls.slice(0, 6)) {
          try { b.click(); clicked.push(((b.textContent || "").trim() || b.getAttribute("aria-label") || "?").slice(0, 20)); } catch (e) {}
        }
        for (let i = 0; i < 12 && !input; i++) {
          await new Promise((r) => setTimeout(r, 750));
          input = findInput();
        }
        if (!input) {
          // Diagnostic dump so we can target the real control precisely.
          const diag = revealControls().slice(0, 18).map((b) =>
            (b.tagName || "") + ":" + (((b.textContent || "").trim() || b.getAttribute("aria-label") || b.getAttribute("title") || "").slice(0, 18))
          );
          resolve({
            error: "no_file_input_found",
            diag: { fileInputs: deepFileInputs().length, clickedControls: clicked, candidateControls: diag },
          });
          return;
        }
      }

      try {
        const dt = new DataTransfer();
        dt.items.add(file);
        input.files = dt.files;
      } catch (e) { resolve({ error: "set_files_failed: " + (e && e.message) }); return; }
      input.dispatchEvent(new Event("change", { bubbles: true }));
      input.dispatchEvent(new Event("input", { bubbles: true }));

      // Best-effort: remove the just-added attachment from dola's compose box so
      // images don't accumulate. Our API submit ignores the compose state anyway,
      // so this is hygiene only — a miss is harmless.
      const cleanupAttachment = () => {
        try {
          const candidates = Array.from(document.querySelectorAll(
            'button, [role="button"], i, span, svg, [class*="close"], [class*="remove"], [class*="delete"]'
          ));
          let clicked = 0;
          for (const b of candidates) {
            if (clicked >= 2) break;
            const label = (((b.getAttribute && (b.getAttribute("aria-label") || b.getAttribute("title"))) || "") + " " + (b.textContent || "")).toLowerCase().trim();
            const isRemove = /remove|delete|close|clear|cancel|×|✕|✖/.test(label) || label === "x";
            if (!isRemove) continue;
            let r; try { r = b.getBoundingClientRect(); } catch (e) { continue; }
            if (!r || r.width > 46 || r.height > 46) continue; // small chip control only
            const holder = b.closest && b.closest('[class*="attach"], [class*="upload"], [class*="image"], [class*="thumb"], [class*="ingredient"], [class*="file"]');
            if (!holder) continue;
            if (!(holder.querySelector && holder.querySelector("img"))) continue;
            try { b.click(); clicked++; } catch (e) {}
          }
        } catch (e) {}
      };

      const deadline = Date.now() + 90000;
      (function poll() {
        if (window.__dolaUploadedUri) {
          const uri = window.__dolaUploadedUri;
          setTimeout(cleanupAttachment, 300);
          resolve({ uri });
          return;
        }
        if (Date.now() > deadline) { resolve({ error: "ref_upload_timeout (uri not captured)" }); return; }
        setTimeout(poll, 800);
      })();
    } catch (e) {
      resolve({ error: "upload_ex: " + (e && e.message) });
    }
  });
}

// ═══════════════════════════════════════════════════════════════════
// Account deletion (drives dola's /delete-account page in the tab)
// ═══════════════════════════════════════════════════════════════════
async function dolaDeleteInTab(request_id, tabId) {
  try {
    await dolaReportProgress(request_id, "deleting", "opening /delete-account");
    await chrome.tabs.update(tabId, { url: `${DOLA_ORIGIN}/delete-account` });
    // Wait for load.
    for (let i = 0; i < 20; i++) {
      await new Promise((r) => setTimeout(r, 1000));
      let t; try { t = await chrome.tabs.get(tabId); } catch (e) { break; }
      if (t && t.status === "complete") break;
    }
    await new Promise((r) => setTimeout(r, 2500)); // let Google re-auth + page render

    // The page briefly bounces to accounts.google.com for silent OAuth, then
    // returns to /delete-account with the "Delete Now" button. Re-install the
    // capture each attempt (navigation clears MAIN world) and retry finding the
    // button for up to ~30s before deciding it's really signed out.
    let clicked = null;
    for (let i = 0; i < 20 && !(clicked && clicked.ok); i++) {
      await dolaExecInTab(tabId, dolaMainDeleteSetup);
      clicked = await dolaExecInTab(tabId, dolaMainClickDelete);
      if (clicked && clicked.ok) break;
      await new Promise((r) => setTimeout(r, 1500));
    }
    if (!clicked || !clicked.ok) {
      const cur = (await dolaExecInTab(tabId, () => location.href)) || "";
      if (String(cur).indexOf("accounts.google.com") !== -1) {
        return { error: "delete_google_signed_out (account chooser shown — re-login Google)" };
      }
      return { error: "delete_button_not_found" };
    }
    await dolaReportProgress(request_id, "delete_confirm", clicked.label || "");

    // Poll for the cancel/confirm 200 (definitive) or a logged-out state.
    const deadline = Date.now() + 45000;
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 1200));
      await dolaExecInTab(tabId, dolaMainClickSecondaryConfirm); // dismiss any modal
      const st = await dolaExecInTab(tabId, dolaMainDeleteStatus);
      if (st && (st.deleted || st.loggedOut)) {
        // Deleted. Close the extra parallel dola tabs and re-login in THIS single
        // tab (cleaner + the trusted click needs one focused tab). After login,
        // the auto-open-tabs logic re-creates the parallel tabs.
        dolaProfileAccount = null;
        await dolaCloseOtherDolaTabs(tabId);
        const relogged = await dolaReLoginSameAccount(tabId);
        // Let the freshly re-created account + its reset quota fully provision on
        // dola's backend before the queue starts dispatching to it again.
        if (relogged) await new Promise((r) => setTimeout(r, 5000));
        return { success: true, deleted: true, relogged: !!relogged };
      }
    }
    return { error: "delete_not_confirmed_timeout" };
  } catch (e) {
    return { error: "delete_ex: " + (e && e.message) };
  }
}

// MAIN-world: after clicking "Continue with Google", dola has generated + stored
// its login csrfToken (to validate on callback). Read it from web storage so our
// constructed OAuth URL carries dola's REAL csrf (else dola rejects the login).
function dolaMainReadLoginCsrf() {
  try {
    const all = {};
    try { for (let i = 0; i < sessionStorage.length; i++) { const k = sessionStorage.key(i); all["s:" + k] = sessionStorage.getItem(k) || ""; } } catch (e) {}
    try { for (let i = 0; i < localStorage.length; i++) { const k = localStorage.key(i); all["l:" + k] = localStorage.getItem(k) || ""; } } catch (e) {}
    // Any stored value that IS/contains the login state (has csrfToken + google/login).
    let fullState = "";
    for (const k in all) {
      const v = all[k];
      if (/csrfToken/i.test(v) && /"platform"\s*:\s*"google"|"type"\s*:\s*"login"|navigatePath/i.test(v)) {
        // Extract the JSON object that holds csrfToken.
        const mm = v.match(/\{[^{}]*csrfToken[^{}]*\}/i) || (v.trim().charAt(0) === "{" ? [v] : null);
        if (mm) { fullState = mm[0]; break; }
      }
    }
    const blob = JSON.stringify(all);
    const UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";
    // JSON-escaped tolerant: csrfToken <any quotes/backslashes/colon> uuid
    let csrf = "";
    let m = blob.match(new RegExp('csrf[_a-z]*token["\\\\\':=\\s]+(' + UUID + ')', 'i'));
    if (m) csrf = m[1];
    // Diagnostic: storage entries relevant to login.
    const relevant = {};
    for (const k in all) {
      if (/csrf|oauth|google|login|auth|gauth|state/i.test(k) || /csrfToken|platform.{0,4}google|type.{0,4}login/i.test(all[k])) {
        relevant[k] = String(all[k]).slice(0, 160);
      }
    }
    return { csrf, fullState: fullState.slice(0, 500), via: csrf ? "csrfToken" : "", relevant, keyCount: Object.keys(all).length };
  } catch (e) {
    return { csrf: "", err: String(e && e.message) };
  }
}

// MAIN-world: is the page in a logged-OUT (guest) state? Authoritative — reads
// the actual page instead of trusting cookies (which linger stale after logout).
function dolaMainIsGuest() {
  try {
    // ONLY a real "Log In" BUTTON in the top bar means logged-out. Do NOT scan
    // page/body text — the sidebar chat history has old titles like "Login
    // Required" / "Login for creation" that would false-positive as guest.
    const btns = Array.from(document.querySelectorAll("button,[role='button']"));
    for (const el of btns) {
      const t = (el.textContent || "").replace(/\s+/g, " ").trim().toLowerCase();
      if (t === "log in" || t === "login" || t === "sign in" || t === "log in / sign up") {
        const r = el.getBoundingClientRect();
        // Visible and in the top strip (the header login button), not sidebar.
        if (r.width > 4 && r.height > 4 && r.top < 140 && r.left > window.innerWidth * 0.4) return true;
      }
    }
    return false;
  } catch (e) {
    return false;
  }
}

// MAIN-world: after a fresh login dola shows a "Confirm Your Age (18+)" modal —
// click "Confirm" to proceed, else login stays blocked. Returns true if clicked.
function dolaMainConfirmAge() {
  try {
    const txt = (document.body ? (document.body.innerText || "") : "").toLowerCase();
    if (txt.indexOf("confirm your age") === -1 && txt.indexOf("at least 18") === -1) return false;
    for (const b of document.querySelectorAll("button,[role='button']")) {
      const t = (b.textContent || "").replace(/\s+/g, " ").trim().toLowerCase();
      if (t === "confirm" || t === "yes" || t === "i am" || t === "i'm 18 or older" || t === "continue") {
        try { b.click(); return true; } catch (e) {}
      }
    }
  } catch (e) {}
  return false;
}

// Dismiss the age modal in a tab (best-effort).
async function dolaConfirmAge(tabId) {
  try {
    const done = await dolaExecInTab(tabId, dolaMainConfirmAge);
    if (done) console.log("[Dola] confirmed age (18+) modal.");
    return done;
  } catch (e) { return false; }
}

// Accurate login check for a specific tab: page NOT in guest state. Falls back to
// the cookie check only if the in-tab read fails.
async function dolaIsLoggedIn(tabId) {
  try {
    // Must actually be ON a dola.com page — mid-OAuth (accounts.google.com) or a
    // blank/loading page is NOT a confirmed login (avoids false positives).
    let url = "";
    try { const t = await chrome.tabs.get(tabId); url = (t && t.url) || ""; } catch (e) {}
    if (url && url.indexOf("dola.com") === -1) return false;
    if (url && url.indexOf("/auth/callback") !== -1) return false; // transient — still processing
    const guest = await dolaExecInTab(tabId, dolaMainIsGuest);
    if (guest === true) return false;
    if (guest === false) return true;
  } catch (e) {}
  return await dolaCheckLoggedIn();
}

// Fast login check — cookie presence only, NO network call (~ms). Best-effort
// (can read stale cookies after logout — prefer dolaIsLoggedIn(tabId)).
async function dolaCheckLoggedIn() {
  try {
    const groups = await Promise.all([
      chrome.cookies.getAll({ url: "https://www.dola.com/" }),
      chrome.cookies.getAll({ url: "https://dola.com/" }),
    ]);
    const names = new Set();
    for (const g of groups) for (const c of g || []) names.add(c.name);
    return ["sessionid", "sessionid_ss", "sid_guard", "sid_tt", "uid_tt", "sid_ucp_v1"].some((n) => names.has(n));
  } catch (e) {
    return false;
  }
}

// Capture dola's real Google-OAuth URL (valid state/csrf) by observing the popup's
// request to accounts.google.com via webRequest. Returns the URL or "".
function dolaCaptureOAuthUrl(timeoutMs) {
  return new Promise((resolve) => {
    let done = false;
    const listener = (details) => {
      const u = details.url || "";
      if (u.indexOf("o/oauth2") !== -1 && (u.indexOf("dola.com") !== -1 || u.indexOf("client_id") !== -1)) {
        if (done) return;
        done = true;
        try { chrome.webRequest.onBeforeRequest.removeListener(listener); } catch (e) {}
        resolve(u);
      }
    };
    try {
      chrome.webRequest.onBeforeRequest.addListener(listener, {
        urls: ["*://accounts.google.com/o/oauth2/*", "*://accounts.google.com/signin/*"],
      });
    } catch (e) { resolve(""); return; }
    setTimeout(() => { if (!done) { done = true; try { chrome.webRequest.onBeforeRequest.removeListener(listener); } catch (e) {} resolve(""); } }, timeoutMs || 9000);
  });
}

async function dolaCloseGoogleAuthTabs(keepTabId) {
  try {
    const tabs = await chrome.tabs.query({ url: ["*://accounts.google.com/*"] });
    for (const t of tabs) if (t.id !== keepTabId) { try { await chrome.tabs.remove(t.id); } catch (e) {} }
  } catch (e) {}
}

// Close all dola.com tabs except the one we keep (for a clean single-tab login
// after a delete). dolaProfileAccount is reset so the extra parallel tabs get
// re-opened after re-login.
async function dolaCloseOtherDolaTabs(keepTabId) {
  try {
    const tabs = await chrome.tabs.query({
      url: ["https://www.dola.com/*", "https://dola.com/*", "http://www.dola.com/*", "http://dola.com/*"],
    });
    let closed = 0;
    for (const t of tabs) {
      if (t.id === keepTabId) continue;
      if (dolaTabBusy[t.id]) continue; // never close a tab with an in-flight job
      try { await chrome.tabs.remove(t.id); closed++; } catch (e) {}
    }
    for (const k of Object.keys(dolaTabBusy)) if (Number(k) !== keepTabId && !dolaTabBusy[k]) delete dolaTabBusy[k];
    if (closed) console.log(`[Dola] closed ${closed} extra dola tab(s) for a clean single-tab re-login.`);
  } catch (e) {}
}

// Build dola's Google-OAuth URL directly (constants from the login HAR). Driving
// this in the MAIN tab avoids the (blocked) popup entirely. Google (active
// session) redirects to dola's callback with the token → dola logs in.
function dolaBuildOAuthUrl(csrf, rawStateJson) {
  let state = null;
  if (rawStateJson) { try { state = JSON.parse(rawStateJson); } catch (e) { state = null; } }
  if (!state) {
    if (!csrf) {
      csrf = "dola-" + Date.now();
      try { if (self.crypto && crypto.randomUUID) csrf = crypto.randomUUID(); } catch (e) {}
    }
    state = {
      entry: "",
      trackMeta: { login_entrance: "auto_open", is_from_ug: "0", is_from_share: "0", trigger_platform: "web", trigger_app: "doubao" },
      autoClose: false, platform: "google", navigatePath: "/chat/", type: "login", csrfToken: csrf,
    };
  }
  let b64 = "";
  try { b64 = btoa(unescape(encodeURIComponent(JSON.stringify(state)))); } catch (e) { b64 = btoa(JSON.stringify(state)); }
  const params = new URLSearchParams({
    client_id: "742187162285-a21b1bgtsfa0srr9jhhb5ksc5ctb6p25.apps.googleusercontent.com",
    redirect_uri: "https://www.dola.com/auth/callback",
    response_type: "token",
    scope: "email profile",
    include_granted_scopes: "true",
    state: b64,
  });
  return "https://accounts.google.com/o/oauth2/v2/auth?" + params.toString();
}

// MAIN-world: viewport center of the "Continue with Google" button, verified
// with elementFromPoint (so we click a real, hittable target).
function dolaMainGoogleBtnRect() {
  try {
    const isVis = (el) => {
      const r = el.getBoundingClientRect();
      const s = getComputedStyle(el);
      return r.width > 20 && r.height > 10 && s.visibility !== "hidden" && s.display !== "none" &&
        r.top >= 0 && r.top < innerHeight && r.left >= 0;
    };
    let best = null;
    for (const el of document.querySelectorAll("button,[role='button'],div,a,span")) {
      const t = (el.textContent || "").replace(/\s+/g, " ").trim().toLowerCase();
      if ((t === "continue with google" || (t.indexOf("continue with google") !== -1 && t.length < 40)) && isVis(el)) {
        const w = el.getBoundingClientRect().width;
        if (!best || w < best.getBoundingClientRect().width) best = el; // smallest = the actual button
      }
    }
    if (!best) return null;
    const r = best.getBoundingClientRect();
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    let hit = "";
    try {
      const h = document.elementFromPoint(x, y);
      const inside = h && (h === best || best.contains(h) || h.contains(best));
      hit = (h ? h.tagName : "null") + (inside ? " (ok)" : " (BLOCKED)");
    } catch (e) {}
    return { x, y, hit };
  } catch (e) {
    return null;
  }
}

// Dispatch a TRUSTED click via chrome.debugger — this carries real user
// activation, so dola's GSI popup is allowed to open (unlike a scripted click).
async function dolaDebuggerClick(tabId, cssX, cssY, dpr) {
  const target = { tabId };
  const send = (method, params) => new Promise((res) =>
    chrome.debugger.sendCommand(target, method, params || {}, () => res(!chrome.runtime.lastError)));
  const detach = () => new Promise((res) => { try { chrome.debugger.detach(target, () => res(true)); } catch (e) { res(true); } });
  const attached = await new Promise((res) =>
    chrome.debugger.attach(target, "1.3", () => {
      if (chrome.runtime.lastError) { console.log("[Dola] debugger attach failed:", chrome.runtime.lastError.message); res(false); }
      else res(true);
    }));
  if (!attached) return false;
  try {
    // CDP Input coordinates are CSS pixels of the viewport (NOT device pixels) —
    // use them directly. (dpr param kept for logging only.)
    const x = Math.round(cssX);
    const y = Math.round(cssY);
    await send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
    await send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", buttons: 1, clickCount: 1 });
    await send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", buttons: 0, clickCount: 1 });
    await new Promise((r) => setTimeout(r, 1500)); // let the gesture open the popup
    return true;
  } finally {
    await detach();
  }
}

// After deletion / when logged out, re-login the SAME Google account. dola uses
// Google Identity Services (GSI) — a scripted click can't open its popup and the
// csrf is in a closure. So we dispatch a REAL trusted click on "Continue with
// Google" via chrome.debugger; dola then opens its own popup which completes
// silently against the active Google session and logs the main tab in.
async function dolaReLoginSameAccount(tabId) {
  try {
    await chrome.tabs.update(tabId, { url: `${DOLA_ORIGIN}/chat/create-video` });
    await new Promise((r) => setTimeout(r, 4500));
    const deadline = Date.now() + 90000;
    while (Date.now() < deadline) {
      if (await dolaIsLoggedIn(tabId)) {
        console.log("[Dola] re-logged in with same Google account.");
        return true;
      }
      // 1) Open the login modal (a scripted click is fine — no popup here).
      await dolaExecInTab(tabId, dolaMainClickLogin);
      // 2) Wait for "Continue with Google" to render, then get its rect.
      let rect = null;
      for (let k = 0; k < 12 && !rect; k++) {
        await new Promise((r0) => setTimeout(r0, 500));
        rect = await dolaExecInTab(tabId, dolaMainGoogleBtnRect);
      }
      if (!rect) {
        console.log("[Dola] 'Continue with Google' not found after opening modal. retrying...");
        await new Promise((r0) => setTimeout(r0, 2500));
        continue;
      }
      console.log(`[Dola] btn @ (${Math.round(rect.x)},${Math.round(rect.y)}) hit=${rect.hit}`);
      // Make the tab active + window focused so the trusted click lands correctly.
      try {
        await chrome.tabs.update(tabId, { active: true });
        await chrome.windows.update((await chrome.tabs.get(tabId)).windowId, { focused: true });
      } catch (e) {}
      await new Promise((r0) => setTimeout(r0, 400));
      const ok = await dolaDebuggerClick(tabId, rect.x, rect.y, 1);
      console.log(`[Dola] trusted click @ (${Math.round(rect.x)},${Math.round(rect.y)}) → ${ok ? "sent" : "FAILED"}. Checking for Google popup...`);
      // Diagnostic: did a Google popup open, and where is it?
      await new Promise((r0) => setTimeout(r0, 1500));
      try {
        const gt = await chrome.tabs.query({ url: ["*://accounts.google.com/*"] });
        if (gt.length) console.log(`[Dola] Google popup: ${(gt[0].url || "").slice(0, 95)}`);
        else console.log("[Dola] no Google popup visible now (either silent auth already completed & closed it, or the click missed — the login poll below is the real verdict).");
      } catch (e) {}
      // 3) The popup completes on its own if Google auto-selects. Poll main tab,
      //    dismissing the "Confirm Your Age" modal that appears on fresh logins.
      for (let i = 0; i < 34; i++) {
        await new Promise((r3) => setTimeout(r3, 1200));
        await dolaConfirmAge(tabId);
        const url = (await dolaExecInTab(tabId, () => location.href)) || "";
        // Count as logged-in only once we've left the transient /auth/callback page.
        if (url.indexOf("/auth/callback") === -1 && (await dolaIsLoggedIn(tabId))) {
          await dolaConfirmAge(tabId);
          console.log("[Dola] ✅ re-login complete via trusted click.");
          return true;
        }
      }
      await new Promise((r0) => setTimeout(r0, 1500));
    }
    console.log("[Dola] re-login did NOT complete (Google chooser/consent, or GSI needs interaction).");
    return false;
  } catch (e) {
    console.log("[Dola] re-login error:", e && e.message);
    return false;
  }
}

// MAIN-world: patch window.open so the Google OAuth URL is captured instead of
// opening a (blockable, undrivable) popup. Returns a harmless stub window.
function dolaMainInstallOpenCapture() {
  try {
    window.__dolaOAuthUrl = "";
    if (!window.__dolaOpenPatched) {
      window.__dolaOpenPatched = true;
      window.open = function (url) {
        try {
          const u = String(url || "");
          if (u.indexOf("accounts.google.com") !== -1 || u.indexOf("/auth/") !== -1 || u.indexOf("oauth") !== -1) {
            window.__dolaOAuthUrl = u;
          }
        } catch (e) {}
        return { closed: false, close() {}, focus() {}, blur() {}, postMessage() {}, location: {}, document: {} };
      };
    }
    return true;
  } catch (e) {
    return false;
  }
}

// MAIN-world: click dola's login controls. Prefers "Continue with Google".
function dolaMainClickLogin() {
  const norm = (s) => (s || "").replace(/\s+/g, " ").trim().toLowerCase();
  const vis = (el) => {
    if (!el || !el.isConnected) return false;
    const st = getComputedStyle(el);
    if (!st || st.display === "none" || st.visibility === "hidden" || st.opacity === "0") return false;
    const r = el.getBoundingClientRect();
    return r.width > 4 && r.height > 4;
  };
  const btns = Array.from(document.querySelectorAll("button,[role='button'],div,span,a,img")).filter(vis);
  const googleWants = ["continue with google", "sign in with google", "log in with google"];
  let el = btns.find((b) => googleWants.includes(norm(b.textContent)));
  // Google icon/button inside a login modal (aria-label / alt).
  if (!el) {
    el = btns.find((b) => {
      const a = (((b.getAttribute && (b.getAttribute("aria-label") || b.getAttribute("alt"))) || "")).toLowerCase();
      return a.indexOf("google") !== -1 && getComputedStyle(b).cursor === "pointer";
    });
  }
  // Otherwise open the login modal first.
  if (!el) el = btns.find((b) => ["log in", "login", "sign in", "log in / sign up"].includes(norm(b.textContent)));
  if (!el) return { clicked: false };
  try { el.click(); } catch (e) {
    try { el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window })); }
    catch (e2) { return { clicked: false }; }
  }
  return { clicked: true, label: (el.textContent || el.getAttribute("aria-label") || "google").replace(/\s+/g, " ").trim().slice(0, 30) };
}

// MAIN-world: patch fetch/XHR to flag the passport cancel/confirm 200, and
// auto-accept any native confirm() popup.
function dolaMainDeleteSetup() {
  try {
    if (!window.__dolaDelPatch) {
      window.__dolaDelPatch = true;
      window.__dolaCancelOk = false;
      const mark = (u, ok) => { if (u && u.indexOf("/passport/web/cancel/confirm/") !== -1 && ok) window.__dolaCancelOk = true; };
      const of = window.fetch;
      window.fetch = function (input) {
        const u = typeof input === "string" ? input : (input && input.url) || "";
        const p = of.apply(this, arguments);
        if (u.indexOf("cancel/confirm") !== -1) { try { p.then((r) => mark(u, r.ok || r.status === 200)).catch(() => {}); } catch (e) {} }
        return p;
      };
      const oo = XMLHttpRequest.prototype.open, os = XMLHttpRequest.prototype.send;
      XMLHttpRequest.prototype.open = function (m, u) { this.__u = u; return oo.apply(this, arguments); };
      XMLHttpRequest.prototype.send = function () {
        try { if (this.__u && String(this.__u).indexOf("cancel/confirm") !== -1) this.addEventListener("load", function () { mark(this.__u, this.status === 200); }); } catch (e) {}
        return os.apply(this, arguments);
      };
      try { window.confirm = () => true; } catch (e) {}
    }
    return true;
  } catch (e) { return false; }
}

function dolaMainClickDelete() {
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
  const el =
    nodes.find((e) => cls(e).includes("confirm-button") && cls(e).includes("type-danger")) ||
    nodes.find((e) => ["delete now", "delete account"].includes(norm(e.textContent)) && cls(e).includes("clickable")) ||
    nodes.find((e) => ["delete now", "delete account", "delete", "confirm"].includes(norm(e.textContent)) && getComputedStyle(e).cursor === "pointer");
  if (!el) return { ok: false };
  const label = (el.textContent || "").replace(/\s+/g, " ").trim().slice(0, 40);
  try { el.click(); } catch (e) {
    try { el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window })); }
    catch (e2) { return { ok: false }; }
  }
  return { ok: true, label, clicked: true };
}

function dolaMainClickSecondaryConfirm() {
  try {
    const norm = (s) => (s || "").replace(/\s+/g, " ").trim().toLowerCase();
    const btns = Array.from(document.querySelectorAll("button,[role='button'],div,span")).filter((el) => {
      if (!el.isConnected) return false;
      const r = el.getBoundingClientRect();
      return r.width > 4 && r.height > 4;
    });
    const el = btns.find((e) => ["confirm", "delete", "ok", "yes", "confirm deletion", "delete account"].includes(norm(e.textContent)) && getComputedStyle(e).cursor === "pointer");
    if (el) { try { el.click(); } catch (e) {} return true; }
  } catch (e) {}
  return false;
}

function dolaMainDeleteStatus() {
  let href = "";
  try { href = location.href || ""; } catch (e) {}
  const loggedOut = /\/login|\/passport\/web\/logout|from_logout/.test(href);
  return { deleted: !!window.__dolaCancelOk, loggedOut };
}

// ═══════════════════════════════════════════════════════════════════
// Bridge helpers
// ═══════════════════════════════════════════════════════════════════
async function dolaReportProgress(request_id, stage, detail) {
  try {
    await fetch(`${DOLA_BRIDGE_URL}/dola/progress`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request_id, stage, detail: String(detail || "") }),
    });
  } catch (e) {}
}

async function dolaSubmitResult(request_id, payload) {
  try {
    await fetch(`${DOLA_BRIDGE_URL}/dola/work-result`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request_id, ...payload }),
    });
  } catch (e) {
    console.warn("[Dola] submit result failed:", e.message);
  }
}

function dolaGetStatus() {
  return {
    connected: dolaBridgeConnected,
    accounts: Object.values(dolaAccounts).map((a) => ({ email: a.email, subscription: a.subscription || "" })),
    lastError: dolaLastPollError,
    active: dolaActiveCount,
  };
}

function dolaStart() {
  console.log("[Dola] Module starting — bridge:", DOLA_BRIDGE_URL);
  setInterval(dolaPollBridge, DOLA_POLL_INTERVAL);
  setInterval(dolaDetectAccounts, DOLA_ACCOUNT_DETECT_INTERVAL);
  setTimeout(dolaDetectAccounts, 2500);
}

self.dolaStart = dolaStart;
self.dolaGetStatus = dolaGetStatus;
