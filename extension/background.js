/**
 * G-Labs Studio Helper — Chrome Extension Background Service Worker
 *
 * Architecture:
 *   Extension polls local Bridge Server (Python app) every 1.5s
 *   When work available → inject reCAPTCHA script into Labs tab (world: "MAIN")
 *   Send token + auth back to Bridge → Python makes direct API call
 *
 * Zero CDP, zero automation markers, real Chrome context.
 */

// Load persona system (ecosystem/warmup query pools)
try { importScripts("personas.js"); } catch (e) { console.warn("personas.js not loaded:", e); }
// Load Genspark module (standalone — uses port 18925 separately from Flow)
try { importScripts("genspark.js"); } catch (e) { console.warn("genspark.js not loaded:", e); }
// Load Grok module (standalone — uses port 18926 separately from Flow/Genspark)
try { importScripts("grok.js"); } catch (e) { console.warn("grok.js not loaded:", e); }

const BRIDGE_URL = "http://127.0.0.1:18924";
const POLL_INTERVAL = 1500;
const LABS_ORIGIN = "https://labs.google";
const ACCOUNT_DETECT_INTERVAL = 10000;

// ─── State ───
let bridgeConnected = false;
let connectedAccounts = {};  // tabId → { email, name, access_token, project_id }
let tokenCount = 0;
let lastPollError = "";

// Per-tab in-flight counter — each tab's scripting channel can only run
// one `chrome.scripting.executeScript` at a time, so we track how many
// work items are currently routing through each tab. findLabsTab() uses
// this to pick the least-busy matching tab for the target account,
// enabling true parallel dispatch when the user opens multiple labs
// tabs for the same account.
const _tabInFlight = {};      // tabId → number of in-flight work items

function _incTabInFlight(tabId) {
  if (!tabId) return;
  _tabInFlight[tabId] = (_tabInFlight[tabId] || 0) + 1;
}

function _decTabInFlight(tabId) {
  if (!tabId) return;
  _tabInFlight[tabId] = Math.max(0, (_tabInFlight[tabId] || 0) - 1);
}

// ─── reCAPTCHA readiness cache (30s validity) ───
const _recaptchaCache = {};  // tabId → { valid: bool, ts: timestamp }
const _RECAPTCHA_CACHE_TTL = 30000;  // 30 seconds

// ─── Ecosystem / Auto Warmup Mode state ───
// Local toggle persisted in chrome.storage so it survives SW restarts AND
// works without bridge being online. Bridge is notified when reachable, but
// is not required for the extension to know its own toggle state.
let ecosystemEnabledLocal = false;   // user's toggle choice (persisted)
let ecosystemGenerationRunning = false; // from bridge /poll
let ecosystemHeldAccounts = {};  // email -> seconds_remaining (from bridge)

// Derived directive — what the scheduler uses each tick.
function ecosystemComputeDirective() {
  if (!ecosystemEnabledLocal) return "disabled";
  if (ecosystemGenerationRunning) return "paused";
  return "active";
}

// Convenience getter — exposed to callers that used the old variable name.
Object.defineProperty(self, "ecosystemDirective", {
  get: ecosystemComputeDirective,
});

// Load persisted toggle on startup (guarded — works even if storage API unavailable)
try {
  if (chrome.storage && chrome.storage.local) {
    chrome.storage.local.get(["ecosystemEnabledLocal"], (result) => {
      ecosystemEnabledLocal = !!(result && result.ecosystemEnabledLocal);
      console.log(`[Ecosystem] Restored local toggle: ${ecosystemEnabledLocal ? "ON" : "OFF"}`);
    });
  }
} catch (e) {
  console.warn("[Ecosystem] storage.local unavailable:", e.message);
}
const ecosystemState = {
  running: false,       // an activity is currently executing
  currentAccount: "",   // which account's tab is active
  currentSite: "",      // e.g. "youtube"
  currentTabId: null,
  currentStartedAt: 0,  // when current activity started (timestamp)
  currentDurationMs: 0, // how long current activity will run
  nextActivityAt: 0,    // timestamp when next activity should fire
  deferReason: "",      // why we're deferring ("user_navigating", etc.)
  todayCounts: {},      // email -> int (reset daily locally)
  lastReset: 0,         // date tracker
  log: [],              // ring buffer of recent events (see logEcosystem)
};

// Keep last 50 events so the popup can show a live activity feed.
function logEcosystem(kind, data) {
  const entry = { ts: Date.now(), kind, ...data };
  ecosystemState.log.push(entry);
  if (ecosystemState.log.length > 50) ecosystemState.log.shift();
}
const ECOSYSTEM_MIN_GAP_MS = 15 * 60 * 1000;   // 15 min
const ECOSYSTEM_MAX_GAP_MS = 40 * 60 * 1000;   // 40 min
const ECOSYSTEM_MAX_PER_DAY = 35;              // safety cap per account per day

// ═══════════════════════════════════════════════════════════════════
// Bridge Communication
// ═══════════════════════════════════════════════════════════════════

async function pollBridge() {
  try {
    // Tell bridge which accounts THIS extension instance has
    // so it only gives us work we can handle (multi-profile support)
    const myAccounts = Object.values(connectedAccounts)
      .filter((a) => a.logged_in && a.email)
      .map((a) => a.email);
    const accountsParam = myAccounts.length
      ? `?accounts=${encodeURIComponent(myAccounts.join(","))}`
      : "";

    const resp = await fetch(`${BRIDGE_URL}/poll${accountsParam}`, {
      method: "GET",
      headers: { "Accept": "application/json" },
    });

    if (!resp.ok) {
      bridgeConnected = false;
      lastPollError = `HTTP ${resp.status}`;
      return;
    }

    // Detect reconnect — if bridge just came back online, push local toggle
    const wasDisconnected = !bridgeConnected;
    bridgeConnected = true;
    lastPollError = "";
    const data = await resp.json();

    if (wasDisconnected) {
      // Sync local ecosystem state to bridge so it knows our toggle
      fetch(`${BRIDGE_URL}/ecosystem`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: ecosystemEnabledLocal }),
      }).catch(() => {});
    }

    // Handle pending work — fire-and-forget so multiple polls can
    // dispatch different items in parallel. Per-tab serialization is
    // enforced naturally by Chrome's scripting channel; findLabsTab()
    // uses _tabInFlight to route each item to the least-busy matching
    // tab so load spreads across tabs instead of piling on tab #1.
    // A single tab account still behaves exactly as before (all items
    // queue onto that one tab's channel).
    if (data.work) {
      handleWork(data.work).catch((e) => {
        console.error("[handleWork]", e);
      });
    }

    // Handle commands (cookie clear, tab reload, etc.)
    if (data.command) {
      await handleCommand(data.command);
    }

    // ─── Ecosystem signals from bridge ───
    // Bridge tells us: is generation running? which accounts are held?
    // Our LOCAL toggle (ecosystemEnabledLocal) is the source of truth for
    // enable/disable; bridge only affects pause/held state.
    if (data.ecosystem) {
      const prevDirective = ecosystemComputeDirective();
      // Only take generation_running + held_accounts from bridge.
      // If bridge says "disabled" but we're locally enabled, prefer local.
      const bridgeDir = data.ecosystem.directive || "disabled";
      ecosystemGenerationRunning = (bridgeDir === "paused");
      ecosystemHeldAccounts = data.ecosystem.held_accounts || {};

      const newDirective = ecosystemComputeDirective();
      if (prevDirective !== newDirective) {
        console.log(`[Ecosystem] Directive: ${prevDirective} → ${newDirective}`);
      }

      // If we were running an activity and directive is now paused/disabled,
      // abort it IMMEDIATELY so generation never shares bandwidth/CPU.
      if (ecosystemState.running && newDirective !== "active") {
        await ecosystemAbortCurrent("directive_changed:" + newDirective);
      }
    }
  } catch (e) {
    bridgeConnected = false;
    lastPollError = e.message || "fetch failed";
  }
}

// ═══════════════════════════════════════════════════════════════════
// Token Generation — world: "MAIN"
// ═══════════════════════════════════════════════════════════════════

async function handleWork(work) {
  const { request_id, account, action } = work;

  // ─── EXECUTE_FETCH: run an HTTP request from inside the labs.google.com
  //     tab using native window.fetch(). Chrome auto-adds all browser
  //     fingerprint headers (sec-fetch-*, x-browser-validation,
  //     x-client-data) which Google's anti-abuse check requires — that's
  //     why a Python aiohttp request with a valid reCAPTCHA token still
  //     gets rejected with PUBLIC_ERROR_UNUSUAL_ACTIVITY. ───
  if (action === "EXECUTE_FETCH") {
    let selectedTabId = null;
    try {
      const tabId = await findLabsTab(account);
      if (!tabId) {
        await submitResult(request_id, { error: "no_labs_tab" });
        return;
      }
      // Reserve this tab's in-flight slot the moment we commit to it,
      // so a concurrent findLabsTab call for another job sees this tab
      // as busier and prefers a different one. The slot is released in
      // the outer finally block regardless of success/error/exception.
      selectedTabId = tabId;
      _incTabInFlight(tabId);

      const fetchUrl = work.fetch_url;
      const fetchMethod = work.fetch_method || "POST";
      const fetchBody = work.fetch_body || "";
      const fetchHeaders = work.fetch_headers || {};
      const recaptchaAction = work.recaptcha_action || null;
      const injectPath = work.inject_recaptcha_path || null;

      const result = await chrome.scripting.executeScript({
        target: { tabId },
        world: "MAIN",
        func: async (url, method, bodyStr, headers, captchaAction, injectPath) => {
          // 1. Optionally mint a fresh reCAPTCHA token + inject into body
          let bodyToSend = bodyStr;
          if (captchaAction && injectPath) {
            try {
              const enterprise = window.grecaptcha && window.grecaptcha.enterprise;
              if (!enterprise || typeof enterprise.execute !== "function") {
                return { error: "no_recaptcha_enterprise" };
              }
              // Resolve site key (same logic as token request handler)
              let siteKey = null;
              try {
                const clients = window.___grecaptcha_cfg && window.___grecaptcha_cfg.clients;
                if (clients) {
                  for (const id of Object.keys(clients)) {
                    const c = clients[id];
                    if (!c || typeof c !== "object") continue;
                    const walk = (obj, depth) => {
                      if (depth > 5 || !obj || typeof obj !== "object") return null;
                      for (const k of Object.keys(obj)) {
                        const v = obj[k];
                        if (typeof v === "string" && v.length >= 20 && v.length <= 50
                          && /^[A-Za-z0-9_-]+$/.test(v)) {
                          if (document.querySelector('script[src*="render=' + v + '"]')) return v;
                        }
                        if (typeof v === "object" && v !== null) {
                          const r = walk(v, depth + 1);
                          if (r) return r;
                        }
                      }
                      return null;
                    };
                    siteKey = walk(c, 0);
                    if (siteKey) break;
                  }
                }
              } catch {}
              if (!siteKey) {
                for (const s of document.querySelectorAll('script[src*="recaptcha"][src*="render="]')) {
                  try {
                    const r = new URL(s.src).searchParams.get("render");
                    if (r && r !== "explicit") { siteKey = r; break; }
                  } catch {}
                }
              }
              if (!siteKey) return { error: "no_sitekey" };
              if (typeof enterprise.ready === "function") {
                await new Promise((r) => enterprise.ready(r));
              }

              // Pre-warmup cluster for VIDEO_GENERATION (restored from
              // Apr 19 working build). 3 fire-and-forget execute()
              // calls before the real token mint keep the video
              // endpoint's stricter reCAPTCHA score check passing.
              // Image action skips the cluster — image endpoint is
              // lenient enough.
              if (captchaAction === "VIDEO_GENERATION") {
                const WARMUP_ACTIONS = ["IMAGE_GENERATION", "VIDEO_GENERATION"];
                for (let i = 0; i < 3; i++) {
                  try {
                    const wAction = WARMUP_ACTIONS[Math.floor(Math.random() * WARMUP_ACTIONS.length)];
                    enterprise.execute(siteKey, { action: wAction }).catch(() => {});
                    await new Promise((r) => setTimeout(r, 80 + Math.random() * 120));
                  } catch {}
                }
              }

              // Now mint the REAL token — fresh + with warm score
              const token = await enterprise.execute(siteKey, { action: captchaAction });
              if (!token) return { error: "recaptcha_returned_null" };

              // Inject the fresh token into the JSON body. Path can be a
              // single dotted path (e.g. "clientContext.recaptchaContext.token")
              // or multiple paths separated by ";" — needed for image
              // requests where Google expects the token both at top-level
              // clientContext.recaptchaContext.token AND duplicated inside
              // requests[].clientContext.recaptchaContext.token.
              try {
                const obj = JSON.parse(bodyStr);
                const setAtPath = (root, path) => {
                  const parts = String(path).split(".");
                  let cur = root;
                  for (let i = 0; i < parts.length - 1; i++) {
                    const key = parts[i];
                    // Numeric segment = array index
                    const isIdx = /^\d+$/.test(key);
                    const ref = isIdx ? Number(key) : key;
                    if (cur[ref] === undefined || cur[ref] === null) {
                      // Don't create if next path step doesn't exist —
                      // means the target sub-object isn't in this body.
                      // Skip silently (e.g. requests[0] doesn't have a
                      // clientContext for video bodies).
                      return;
                    }
                    cur = cur[ref];
                  }
                  const last = parts[parts.length - 1];
                  const lastIsIdx = /^\d+$/.test(last);
                  cur[lastIsIdx ? Number(last) : last] = token;
                };
                for (const p of String(injectPath).split(";")) {
                  if (p.trim()) setAtPath(obj, p.trim());
                }
                bodyToSend = JSON.stringify(obj);
              } catch (e) {
                return { error: "body_inject_failed: " + e.message };
              }
            } catch (e) {
              return { error: "recaptcha_failed: " + (e?.message || e) };
            }
          }

          // 2. Native fetch — Chrome adds the magic browser headers
          //    (sec-fetch-*, x-browser-validation, x-client-data) here.
          //    DON'T set them manually — let Chrome do its thing.
          try {
            const resp = await fetch(url, {
              method,
              credentials: "include",
              headers: headers || {},
              body: method === "GET" || method === "HEAD" ? undefined : bodyToSend,
            });
            const respBody = await resp.text();
            // Collect a few useful headers to return
            const respHeaders = {};
            try {
              for (const h of ["content-type", "x-goog-request-id"]) {
                const v = resp.headers.get(h);
                if (v) respHeaders[h] = v;
              }
            } catch {}
            return {
              status: resp.status,
              body: respBody,
              headers: respHeaders,
            };
          } catch (e) {
            return { error: "fetch_failed: " + (e?.message || e) };
          }
        },
        args: [fetchUrl, fetchMethod, fetchBody, fetchHeaders, recaptchaAction, injectPath],
      });

      const r = result?.[0]?.result;
      if (!r) {
        await submitResult(request_id, { error: "no_script_result" });
        return;
      }
      // Forward whatever the page returned (success or error)
      await submitResult(request_id, r);
      return;
    } catch (e) {
      await submitResult(request_id, { error: "execute_fetch_threw: " + (e?.message || e) });
      return;
    } finally {
      _decTabInFlight(selectedTabId);
    }
  }

  // ─── GET_COOKIES: return all cookies for labs.google (including .google.com parent) ───
  if (action === "GET_COOKIES") {
    try {
      const cookies = await chrome.cookies.getAll({ url: "https://labs.google/" });
      const cookieStr = cookies.map(c => `${c.name}=${c.value}`).join("; ");
      await submitResult(request_id, { cookies: cookieStr });
    } catch (e) {
      await submitResult(request_id, { error: e.message || "cookies_error" });
    }
    return;
  }

  // ─── DOWNLOAD_MEDIA: resolve redirect URL via webRequest + MAIN world fetch (fallback) ───
  if (action && action.startsWith("DOWNLOAD_MEDIA:")) {
    const mediaUrl = action.slice("DOWNLOAD_MEDIA:".length);
    const allLabsTabs = await chrome.tabs.query({ url: `${LABS_ORIGIN}/*` });
    const tabId = allLabsTabs.length > 0 ? allLabsTabs[0].id : null;
    if (!tabId) {
      await submitResult(request_id, { error: "no_labs_tab_for_download" });
      return;
    }
    try {
      // 1. Set up webRequest listener to capture the 307 redirect at network level
      //    (fires BEFORE CORS, so we get the Location even though JS fetch will fail)
      const redirectPromise = new Promise((resolve) => {
        let resolved = false;
        const listener = (details) => {
          if (!resolved && details.url.includes("getMediaUrlRedirect")) {
            resolved = true;
            chrome.webRequest.onBeforeRedirect.removeListener(listener);
            resolve(details.redirectUrl || null);
          }
        };
        chrome.webRequest.onBeforeRedirect.addListener(
          listener,
          { urls: ["https://labs.google/fx/api/trpc/media.getMediaUrlRedirect*"] }
        );
        // Timeout fallback
        setTimeout(() => {
          if (!resolved) {
            resolved = true;
            chrome.webRequest.onBeforeRedirect.removeListener(listener);
            resolve(null);
          }
        }, 15000);
      });

      // 2. Fire the request from MAIN world (has page cookies, same-origin)
      //    We don't care about the fetch result — webRequest captures the redirect
      await chrome.scripting.executeScript({
        target: { tabId },
        world: "MAIN",
        func: (url) => {
          fetch(url, { credentials: "include", redirect: "follow" }).catch(() => {});
        },
        args: [mediaUrl],
      });

      // 3. Wait for the redirect URL from webRequest listener
      const cdnUrl = await redirectPromise;
      if (cdnUrl) {
        await submitResult(request_id, { cdn_url: cdnUrl });
      } else {
        await submitResult(request_id, { error: "no_redirect_captured" });
      }
    } catch (e) {
      await submitResult(request_id, { error: e.message || "download_resolve_error" });
    }
    return;
  }

  // Check if any Labs tab exists at all (before reCAPTCHA validation)
  const allLabsTabs = await chrome.tabs.query({ url: `${LABS_ORIGIN}/*` });

  // Find a Labs tab with reCAPTCHA ready for this account
  const tabId = await findLabsTab(account);
  if (!tabId) {
    // Distinguish: no tab at all vs tab exists but reCAPTCHA not loaded
    if (allLabsTabs.length > 0) {
      await submitResult(request_id, {
        error: "no_recaptcha_enterprise",  // tab exists, reCAPTCHA not ready (auto-reloading)
      });
    } else {
      await submitResult(request_id, {
        error: `no_labs_tab_for_${account || "any"}`,
      });
    }
    return;
  }

  try {
    // Single injection: get auth + reCAPTCHA token + project ID in one shot
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: mainWorldExecute,
      args: [action],
    });

    const result = results?.[0]?.result;
    if (!result) {
      await submitResult(request_id, { error: "script_returned_null" });
      return;
    }

    if (result.token) {
      tokenCount++;
      // Update cached account info
      if (result.email) {
        connectedAccounts[tabId] = {
          email: result.email,
          name: result.name || "",
          access_token: result.access_token || "",
          project_id: result.project_id || "",
          logged_in: true,
        };
      }
    }

    await submitResult(request_id, result);
  } catch (e) {
    await submitResult(request_id, { error: e.message || "script_error" });
  }
}

/**
 * Resolve a media redirect URL using the page's cookies.
 * Fetches the URL with credentials, follows redirect, returns the final CDN URL.
 * Runs in world: "MAIN" so it has the page's cookies.
 */
async function resolveMediaUrl(mediaUrl) {
  try {
    // Use redirect: "manual" to capture the 307 Location header
    // (redirect: "follow" fails because GCS is cross-origin)
    const resp = await fetch(mediaUrl, {
      method: "GET",
      credentials: "include",
      redirect: "manual",
    });
    // 307 redirect — extract Location from the opaque-redirect response
    if (resp.type === "opaqueredirect" || resp.status === 0) {
      // Can't read Location from opaque redirect in fetch API.
      // Fall back to XMLHttpRequest which CAN read redirect headers.
      return await new Promise((resolve) => {
        const xhr = new XMLHttpRequest();
        xhr.open("GET", mediaUrl, true);
        xhr.withCredentials = true;
        // Prevent following redirect so we can read Location
        xhr.onreadystatechange = function () {
          if (xhr.readyState === 2) {  // HEADERS_RECEIVED
            const loc = xhr.getResponseHeader("Location");
            if (loc) {
              xhr.abort();
              resolve({ cdn_url: loc });
              return;
            }
          }
          if (xhr.readyState === 4) {
            if (xhr.status >= 200 && xhr.status < 400) {
              // Got actual data (no redirect)
              resolve({ cdn_url: xhr.responseURL || mediaUrl });
            } else {
              resolve({ error: `xhr_failed_${xhr.status}` });
            }
          }
        };
        xhr.onerror = () => resolve({ error: "xhr_network_error" });
        xhr.send();
      });
    }
    if (resp.status >= 300 && resp.status < 400) {
      const loc = resp.headers.get("Location");
      if (loc) return { cdn_url: loc };
    }
    if (resp.ok) {
      // resp.url contains the final URL after redirects
      if (resp.url && resp.url !== mediaUrl) {
        return { cdn_url: resp.url };
      }
    }
    return { error: `fetch_failed_${resp.status}` };
  } catch (e) {
    return { error: e.message || "resolve_fetch_error" };
  }
}

/**
 * This function runs in the page's TRUE main world (world: "MAIN").
 * No CDP traces. reCAPTCHA sees it as the page's own JavaScript.
 *
 * Returns: { token, access_token, email, name, project_id, error }
 */
async function mainWorldExecute(action) {
  const result = { token: null, access_token: null, email: null, name: null, project_id: null, error: null };

  try {
    // ─── 1. Auth Session ───
    try {
      const authResp = await fetch("https://labs.google/fx/api/auth/session", {
        method: "GET",
        credentials: "include",
      });
      if (authResp.ok) {
        const auth = await authResp.json().catch(() => null);
        if (auth) {
          result.access_token = auth.access_token || null;
          result.email = auth.email || (auth.user && auth.user.email) || null;
          result.name = auth.name || (auth.user && auth.user.name) || null;
        }
      }
    } catch {}

    if (!result.access_token) {
      result.error = "no_auth_session";
      return result;
    }

    // ─── 2. Project ID (from URL → DOM → API fallback) ───
    try {
      const urlMatch = window.location.href.match(/\/project\/([a-z0-9-]{16,})/i);
      if (urlMatch) {
        result.project_id = urlMatch[1];
      } else {
        // Try DOM scrape
        const links = document.querySelectorAll("a[href*='/project/']");
        for (const link of links) {
          const m = (link.href || "").match(/\/project\/([a-z0-9-]{16,})/i);
          if (m) { result.project_id = m[1]; break; }
        }
      }
      // API fallback: fetch projects list if still no project_id
      if (!result.project_id && result.access_token) {
        try {
          const projResp = await fetch(
            "https://aisandbox-pa.googleapis.com/v1/projects",
            { method: "GET", credentials: "include",
              headers: { "authorization": "Bearer " + result.access_token } }
          );
          if (projResp.ok) {
            const projData = await projResp.json().catch(() => null);
            // Response: { projects: [{ name: "projects/abc-123", ... }] }
            const projects = projData && (projData.projects || projData.project || []);
            if (Array.isArray(projects) && projects.length > 0) {
              const pName = projects[0].name || projects[0].projectId || "";
              const pMatch = pName.match(/([a-z0-9-]{16,})/i);
              if (pMatch) result.project_id = pMatch[1];
            }
          }
        } catch {}
      }
    } catch {}

    // ─── 3. reCAPTCHA Token ───
    try {
      const enterprise = window.grecaptcha && window.grecaptcha.enterprise;
      if (!enterprise || typeof enterprise.execute !== "function") {
        result.error = "no_recaptcha_enterprise";
        return result;
      }

      // Primary: ___grecaptcha_cfg.clients (internal config object)
      let siteKey = null;
      try {
        const clients = window.___grecaptcha_cfg && window.___grecaptcha_cfg.clients;
        if (clients) {
          for (const id of Object.keys(clients)) {
            const client = clients[id];
            if (!client || typeof client !== "object") continue;
            const walk = (obj, depth) => {
              if (depth > 5 || !obj || typeof obj !== "object") return null;
              for (const key of Object.keys(obj)) {
                const val = obj[key];
                if (typeof val === "string" && val.length >= 20 && val.length <= 50
                  && /^[A-Za-z0-9_-]+$/.test(val)) {
                  const check = document.querySelector('script[src*="render=' + val + '"]');
                  if (check) return val;
                }
                if (typeof val === "object" && val !== null) {
                  const found = walk(val, depth + 1);
                  if (found) return found;
                }
              }
              return null;
            };
            siteKey = walk(client, 0);
            if (siteKey) break;
          }
        }
      } catch {}

      // Fallback: script tag parsing
      if (!siteKey) {
        for (const s of document.querySelectorAll('script[src*="recaptcha"][src*="render="]')) {
          try {
            const render = new URL(s.src).searchParams.get("render");
            if (render && render !== "explicit") { siteKey = render; break; }
          } catch {}
        }
      }

      if (!siteKey) {
        result.error = "no_sitekey";
        return result;
      }

      // Ready + Execute — zero delays, no cluster.
      // Matches the working older build's simple pattern: any extra
      // execute() calls bumps the tab's per-minute rate and Google's
      // reCAPTCHA Enterprise flags the tab.
      if (typeof enterprise.ready === "function") {
        await new Promise((r) => enterprise.ready(r));
      }

      const token = await enterprise.execute(siteKey, { action });
      if (token) {
        result.token = token;
      } else {
        result.error = "execute_returned_null";
      }
    } catch (e) {
      result.error = e.message || "recaptcha_error";
    }

    return result;
  } catch (e) {
    result.error = e.message || "main_world_error";
    return result;
  }
}

// ═══════════════════════════════════════════════════════════════════
// Submit Result to Bridge
// ═══════════════════════════════════════════════════════════════════

async function submitResult(requestId, data) {
  try {
    await fetch(`${BRIDGE_URL}/token`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        request_id: requestId,
        ...data,
      }),
    });
  } catch {}
}

// ═══════════════════════════════════════════════════════════════════
// Tab Management
// ═══════════════════════════════════════════════════════════════════

async function checkRecaptchaReady(tabId) {
  // Check cache first (valid for 30s)
  const cached = _recaptchaCache[tabId];
  if (cached && (Date.now() - cached.ts) < _RECAPTCHA_CACHE_TTL) {
    return cached.valid;
  }

  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: () => {
        return typeof grecaptcha !== "undefined" && !!grecaptcha.enterprise
          && typeof grecaptcha.enterprise.execute === "function";
      },
    });
    const valid = !!(results?.[0]?.result);
    _recaptchaCache[tabId] = { valid, ts: Date.now() };
    return valid;
  } catch {
    _recaptchaCache[tabId] = { valid: false, ts: Date.now() };
    return false;
  }
}

function invalidateRecaptchaCache(tabId) {
  if (tabId) delete _recaptchaCache[tabId];
  else Object.keys(_recaptchaCache).forEach(k => delete _recaptchaCache[k]);
}

async function findLabsTab(targetAccount) {
  const tabs = await chrome.tabs.query({ url: `${LABS_ORIGIN}/*` });

  if (!tabs.length) return null;

  // If specific account requested, find matching tab with reCAPTCHA ready
  if (targetAccount) {
    // Collect ALL tabs matching this account with reCAPTCHA ready, then
    // pick the one with the fewest in-flight work items. Enables parallel
    // dispatch when the user has opened multiple labs.google.com tabs for
    // the same account — each tab's scripting channel runs independently,
    // so 3 tabs ≈ 3x throughput without touching the same-tab serialization.
    const ready = [];
    for (const tab of tabs) {
      const cached = connectedAccounts[tab.id];
      if (cached && cached.email === targetAccount) {
        if (await checkRecaptchaReady(tab.id)) {
          ready.push(tab.id);
        }
      }
    }
    if (ready.length) {
      ready.sort((a, b) => (_tabInFlight[a] || 0) - (_tabInFlight[b] || 0));
      return ready[0];
    }

    // Cache miss — check each tab via script
    for (const tab of tabs) {
      try {
        const results = await chrome.scripting.executeScript({
          target: { tabId: tab.id },
          world: "MAIN",
          func: async () => {
            try {
              const resp = await fetch("https://labs.google/fx/api/auth/session", {
                method: "GET", credentials: "include",
              });
              const data = await resp.json().catch(() => null);
              if (!data) return null;
              return {
                email: data.email || (data.user && data.user.email) || "",
                name: data.name || (data.user && data.user.name) || "",
                logged_in: !!data.access_token,
              };
            } catch { return null; }
          },
        });

        const auth = results?.[0]?.result;
        if (auth && auth.email === targetAccount) {
          connectedAccounts[tab.id] = { ...auth, logged_in: true };
          if (await checkRecaptchaReady(tab.id)) {
            return tab.id;
          }
        }
      } catch {}
    }

    // No tab has reCAPTCHA ready — auto-reload first matching account tab
    for (const tab of tabs) {
      const cached = connectedAccounts[tab.id];
      if (cached && cached.email === targetAccount) {
        try {
          await chrome.tabs.reload(tab.id);
          invalidateRecaptchaCache(tab.id);
        } catch {}
        // Return null — caller will get no_recaptcha, Python side will wait & retry
        return null;
      }
    }

    return null;
  }

  // No specific account — return first Labs tab with reCAPTCHA ready
  for (const tab of tabs) {
    if (await checkRecaptchaReady(tab.id)) {
      return tab.id;
    }
  }

  // None ready — reload first tab
  try {
    await chrome.tabs.reload(tabs[0].id);
    invalidateRecaptchaCache(tabs[0].id);
  } catch {}
  return null;
}

// ═══════════════════════════════════════════════════════════════════
// Account Detection
// ═══════════════════════════════════════════════════════════════════

async function detectAccounts() {
  const tabs = await chrome.tabs.query({ url: `${LABS_ORIGIN}/*` });
  const accounts = [];

  for (const tab of tabs) {
    try {
      const results = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        world: "MAIN",
        func: async () => {
          try {
            const resp = await fetch("https://labs.google/fx/api/auth/session", {
              method: "GET", credentials: "include",
            });
            if (!resp.ok) return null;
            const data = await resp.json().catch(() => null);
            if (!data || !data.access_token) return null;

            // Also try to get project ID from URL
            let projectId = null;
            const urlMatch = window.location.href.match(/\/project\/([a-z0-9-]{16,})/i);
            if (urlMatch) projectId = urlMatch[1];

            return {
              email: data.email || (data.user && data.user.email) || "",
              name: data.name || (data.user && data.user.name) || "",
              access_token: data.access_token,
              project_id: projectId,
              logged_in: true,
            };
          } catch { return null; }
        },
      });

      const auth = results?.[0]?.result;
      if (auth && auth.logged_in) {
        connectedAccounts[tab.id] = auth;
        accounts.push({
          email: auth.email,
          name: auth.name,
          tab_id: tab.id,
          project_id: auth.project_id,
        });
      }
    } catch {}
  }

  // Report to bridge
  try {
    await fetch(`${BRIDGE_URL}/accounts`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ accounts }),
    });
  } catch {}

  return accounts;
}

// ═══════════════════════════════════════════════════════════════════
// Commands from Bridge
// ═══════════════════════════════════════════════════════════════════

async function handleCommand(cmd) {
  const { type, account } = cmd;
  const tabId = await findLabsTab(account);

  switch (type) {
    case "clear_cookies":
      try {
        const cookies = await chrome.cookies.getAll({ domain: ".google.com" });
        for (const c of cookies) {
          if (c.domain.includes("labs.google")) {
            await chrome.cookies.remove({
              url: `https://labs.google${c.path}`,
              name: c.name,
            });
          }
        }
      } catch {}
      break;

    case "reload_tab":
      if (tabId) {
        try { await chrome.tabs.reload(tabId); } catch {}
      }
      break;

    case "clean_tracking":
      // Remove Service Workers + IndexedDB for labs.google — preserves login cookies
      try {
        await chrome.browsingData.remove(
          { origins: ["https://labs.google"] },
          {
            serviceWorkers: true,
            indexedDB: true,
            cacheStorage: true,
          }
        );
        console.log(`[G-Labs Helper] Tracking data cleaned for ${account || "all accounts"}`);
      } catch (e) {
        console.warn("[G-Labs Helper] clean_tracking failed:", e.message);
      }
      // Reload tab after cleanup so reCAPTCHA re-initializes fresh
      if (tabId) {
        try { await chrome.tabs.reload(tabId); } catch {}
      }
      break;

    case "clean_recaptcha_cookie":
      // Delete _GRECAPTCHA cookie only — keeps login session intact
      try {
        const allCookies = await chrome.cookies.getAll({ domain: ".google.com" });
        let deleted = 0;
        for (const c of allCookies) {
          if (c.name === "_GRECAPTCHA" || c.name.startsWith("_GRECAPTCHA")) {
            const protocol = c.secure ? "https" : "http";
            await chrome.cookies.remove({
              url: `${protocol}://${c.domain.replace(/^\./, "")}${c.path}`,
              name: c.name,
            });
            deleted++;
          }
        }
        console.log(`[G-Labs Helper] Removed ${deleted} _GRECAPTCHA cookie(s)`);
      } catch (e) {
        console.warn("[G-Labs Helper] clean_recaptcha_cookie failed:", e.message);
      }
      break;

    case "new_project":
      if (tabId) {
        try {
          const results = await chrome.scripting.executeScript({
            target: { tabId },
            world: "MAIN",
            func: async () => {
              // Click "New project" button
              const btn = document.querySelector('[data-testid="new-project"], button');
              const allBtns = Array.from(document.querySelectorAll("button"));
              const newProj = allBtns.find((b) => b.textContent?.trim() === "New project");
              if (newProj) {
                newProj.click();
                await new Promise((r) => setTimeout(r, 3000));
                const m = window.location.href.match(/\/project\/([a-z0-9-]{16,})/i);
                return m ? m[1] : null;
              }
              return null;
            },
          });
          const pid = results?.[0]?.result;
          if (pid) {
            await fetch(`${BRIDGE_URL}/project`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ account, project_id: pid }),
            });
          }
        } catch {}
      }
      break;
  }
}

// ═══════════════════════════════════════════════════════════════════
// Auto Warmup Mode — Ecosystem Activity Engine (Phase 1)
// ═══════════════════════════════════════════════════════════════════

// ─── Phase 4: Account service profile cache ───
// For each account, track which Google services have content (Drive files,
// Photos, Gmail messages). Services with no content get weight=0 so we don't
// do weird "open empty Drive, scroll nothing" activity that looks like a bot.
// Cache TTL: 24 hours — services can be re-checked daily.
const accountServiceProfile = {};  // email -> { drive: bool, photos: bool, gmail: bool, checked_at: ts }
const SERVICE_PROBE_TTL_MS = 24 * 60 * 60 * 1000;

async function probeAccountServices(account) {
  const now = Date.now();
  const cached = accountServiceProfile[account];
  if (cached && now - cached.checked_at < SERVICE_PROBE_TTL_MS) return cached;

  const profile = {
    drive: true,    // default true — err on side of trying
    photos: true,
    gmail: true,
    checked_at: now,
  };

  // Probe Drive — fetch the main folder listing HTML, check for "Nothing in here"
  try {
    const resp = await fetch("https://drive.google.com/drive/my-drive", {
      method: "GET",
      credentials: "include",
      cache: "no-store",
    });
    if (resp.ok) {
      const html = await resp.text();
      // Drive returns SPA shell; we check for localized empty state markers
      const emptyMarkers = [
        "A place for all of your files",  // EN empty state
        "no files",
        "Nothing in here",
        "यहाँ कुछ नहीं है",
      ];
      const looksEmpty = emptyMarkers.some((m) => html.includes(m));
      profile.drive = !looksEmpty;
    }
  } catch {}

  // Probe Photos
  try {
    const resp = await fetch("https://photos.google.com/", {
      method: "GET",
      credentials: "include",
      cache: "no-store",
    });
    if (resp.ok) {
      const html = await resp.text();
      const emptyMarkers = [
        "You haven't added any photos yet",
        "No photos",
        "Add your photos",
      ];
      const looksEmpty = emptyMarkers.some((m) => html.includes(m));
      profile.photos = !looksEmpty;
    }
  } catch {}

  // Gmail — we assume almost always has emails, but still check basic
  try {
    const resp = await fetch("https://mail.google.com/mail/u/0/#inbox", {
      method: "GET",
      credentials: "include",
      cache: "no-store",
    });
    if (resp.ok) {
      const html = await resp.text();
      profile.gmail = !html.includes("No conversations selected");
    }
  } catch {}

  accountServiceProfile[account] = profile;
  console.log(
    `[Ecosystem] Probed ${account}: drive=${profile.drive}, photos=${profile.photos}, gmail=${profile.gmail}`
  );
  return profile;
}

// Given an activity + account profile, return the effective weight
// (0 if service empty for this account, else base weight).
function effectiveWeight(activity, profile) {
  if (!profile) return activity.weight;
  if (activity.name === "drive" && !profile.drive) return 0;
  if (activity.name === "photos" && !profile.photos) return 0;
  if (activity.name === "gmail" && !profile.gmail) return 0;
  return activity.weight;
}

// ─── Basic search query pool (Phase 3 will replace with per-persona pools) ───
const ECOSYSTEM_SEARCH_QUERIES = [
  "best pizza near me", "weather today", "cricket score", "bollywood news",
  "tech news today", "best mobile under 20000", "laptop deals",
  "recipe for pasta", "stock market today", "ipl schedule",
  "car reviews", "youtube trending", "movie reviews", "best smartphone 2026",
  "food near me", "chai recipe", "how to cook biryani", "shopping deals",
  "health tips", "morning workout", "news live", "cricket news today",
  "upcoming movies 2026", "travel destinations", "best restaurants in delhi",
  "electric car price", "iphone 17 features", "samsung new phone",
  "football news", "world news", "share market tips", "gold rate today",
];

const ECOSYSTEM_YT_QUERIES = [
  "funny cricket moments", "cooking at home", "car review", "phone unboxing",
  "bollywood songs", "motivational videos", "how to make pizza",
  "travel vlog india", "tech tips", "fitness workout", "ipl highlights",
  "movie trailer", "cricket highlights", "street food", "gaming stream",
];

const ECOSYSTEM_MAPS_QUERIES = [
  "restaurants near me", "coffee shops", "shopping mall", "petrol pump",
  "ATM nearby", "pharmacy near me", "hospital", "park", "gym near me",
  "metro station", "hotel near me", "movie theater", "grocery store",
];

// Activity pool — each activity has an executor function.
const ECOSYSTEM_ACTIVITIES = [
  { name: "youtube",  weight: 30, url: "https://www.youtube.com/",        duration: [120, 300], run: activityYouTube },
  { name: "gmail",    weight: 20, url: "https://mail.google.com/",        duration: [60, 120],  run: activityGmail },
  { name: "search",   weight: 15, url: "https://www.google.com/",         duration: [60, 180],  run: activitySearch },
  { name: "drive",    weight: 10, url: "https://drive.google.com/",       duration: [60, 120],  run: activityDrive },
  { name: "maps",     weight: 10, url: "https://www.google.com/maps",     duration: [60, 120],  run: activityMaps },
  { name: "news",     weight: 10, url: "https://news.google.com/",        duration: [60, 180],  run: activityNews },
  { name: "photos",   weight: 5,  url: "https://photos.google.com/",      duration: [60, 120],  run: activityPhotos },
];

// Helper: pick a random item from array
function rand(arr) { return arr[Math.floor(Math.random() * arr.length)]; }
function randInt(min, max) { return Math.floor(min + Math.random() * (max - min + 1)); }
function randFloat(min, max) { return min + Math.random() * (max - min); }

// Helper: sleep with early-abort checks
async function abortableSleep(ms, checkFn) {
  const step = 500;
  const start = Date.now();
  while (Date.now() - start < ms) {
    if (checkFn && checkFn()) throw new Error("aborted");
    await new Promise((r) => setTimeout(r, Math.min(step, ms - (Date.now() - start))));
  }
}

// Inject a function into the target tab's MAIN world and get result.
async function injectInTab(tabId, fn, args) {
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: fn,
      args: args || [],
    });
    return results?.[0]?.result;
  } catch (e) {
    return null;
  }
}

// Wait for tab to finish loading (polls webNavigation via tab status)
async function waitForTabLoad(tabId, maxMs) {
  const start = Date.now();
  while (Date.now() - start < (maxMs || 15000)) {
    try {
      const t = await chrome.tabs.get(tabId);
      if (t && t.status === "complete") return true;
    } catch { return false; }
    await new Promise((r) => setTimeout(r, 500));
  }
  return false;
}

// ═══════════════════════════════════════════════════════════════════
// Site-specific activity executors
// Each returns when activity is done. Throws on abort/error.
// All interactions run inside MAIN world so they look like real user events.
// ═══════════════════════════════════════════════════════════════════

// ─── Phase 5 (Upgraded): Human-grade natural behavior helpers ───
// Upgrades vs original:
//  1. Bell-curve velocity (ease-in-out: fast in middle, slow at start/end)
//  2. Overshoot + correct (20% chance of crossing target and coming back)
//  3. Scroll with logarithmic deceleration + overshoot
//  4. Smoothed noise (1D low-pass filter instead of pure Math.random jitter)

// Note: smoothNoise + easeInOutCubic are defined inline inside the MAIN-world
// injected functions below (they run in page context, can't see outer scope).
//
// Realistic scroll with bell-curve velocity, logarithmic deceleration,
// occasional overshoot + correction, and smoothed noise on step sizes.
async function naturalScroll(tabId, scrolls, checkAbort) {
  for (let i = 0; i < scrolls; i++) {
    if (checkAbort && checkAbort()) throw new Error("aborted");
    const direction = Math.random() < 0.85 ? 1 : -1;   // mostly down, sometimes up
    const total = randInt(200, 900) * direction;
    // 20% chance of overshoot on primary scroll (goes too far, corrects back)
    const overshoot = Math.random() < 0.20;
    const overshootAmount = overshoot
      ? Math.floor(total * (0.15 + Math.random() * 0.20))
      : 0;

    // Break big scroll into 5-9 increments with logarithmic deceleration
    // (faster start, slower end — like a real scroll wheel spin)
    const steps = randInt(5, 9);
    await injectInTab(tabId, async (total, overshootAmt, steps) => {
      // Smoothed noise for per-step timing
      let noisePrev = Math.random() * 2 - 1;
      const noise = () => { noisePrev = noisePrev * 0.6 + (Math.random() * 2 - 1) * 0.4; return noisePrev; };
      // Logarithmic distribution of step sizes: sum = total
      const weights = [];
      for (let k = 0; k < steps; k++) {
        // exp decay — first steps bigger, last steps smaller
        weights.push(Math.exp(-k * 0.4));
      }
      const weightSum = weights.reduce((a, b) => a + b, 0);
      // Primary scroll (possibly past target if overshoot)
      const primaryTarget = total + overshootAmt;
      for (let k = 0; k < steps; k++) {
        const stepSize = Math.round((weights[k] / weightSum) * primaryTarget);
        window.scrollBy({ top: stepSize, behavior: "smooth" });
        // Step delay: base 70-180ms + noise
        const base = 70 + Math.random() * 110;
        const jitter = noise() * 30;
        await new Promise((r) => setTimeout(r, Math.max(30, base + jitter)));
      }
      // If we overshot, correct back
      if (overshootAmt !== 0) {
        // Short pause (human notices overshoot)
        await new Promise((r) => setTimeout(r, 150 + Math.random() * 250));
        // Correct: scroll back the overshoot amount in 2-3 small steps
        const correctSteps = 2 + Math.floor(Math.random() * 2);
        for (let k = 0; k < correctSteps; k++) {
          window.scrollBy({ top: -overshootAmt / correctSteps, behavior: "smooth" });
          await new Promise((r) => setTimeout(r, 80 + Math.random() * 120));
        }
      }
    }, [total, overshootAmount, steps]);

    // Read pause — 3 levels of realism:
    // - Long read (30% chance): 3.5-7s
    // - Glance (50%): 1-3.5s
    // - Very quick flick (20%): 0.3-1s
    const r = Math.random();
    let readPause;
    if (r < 0.30) readPause = randInt(3500, 7000);
    else if (r < 0.80) readPause = randInt(1000, 3500);
    else readPause = randInt(300, 1000);
    await abortableSleep(readPause, checkAbort);
  }
}

// Realistic human typing (upgraded):
// - Variable per-character delay driven by smoothed noise (correlated —
//   real typists speed up in rhythm, slow down occasionally, not pure random)
// - 5% chance of typo with correction (backspace)
// - Bigrams that are common in English type faster, uncommon slower
// - Occasional pauses between words
// - Long pause 3% chance (thinking mid-query)
function humanTypeInto(selector, text) {
  return async function (sel, txt) {
    const box = document.querySelector(sel);
    if (!box) return false;
    box.focus();
    box.value = "";
    box.dispatchEvent(new Event("input", { bubbles: true }));
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

    // Smoothed 1D noise for typing rhythm
    let noise = Math.random() * 2 - 1;
    const nextNoise = () => { noise = noise * 0.7 + (Math.random() * 2 - 1) * 0.3; return noise; };

    // Common bigrams that real typists hit faster (just a shortlist)
    const fastBigrams = new Set([
      "th", "he", "in", "er", "an", "re", "on", "en", "at", "nd", "ed",
      "nt", "ha", "st", "or", "ou", "ng", "as", "is", "of", "it", "es"
    ]);

    for (let i = 0; i < txt.length; i++) {
      const ch = txt[i];
      // 5% typo chance on letter keys
      if (/[a-z]/i.test(ch) && Math.random() < 0.05) {
        const typoChars = "abcdefghijklmnopqrstuvwxyz";
        const wrong = typoChars[Math.floor(Math.random() * 26)];
        box.value += wrong;
        box.dispatchEvent(new Event("input", { bubbles: true }));
        await sleep(80 + Math.random() * 120);
        // Realize mistake — hesitation + backspace
        await sleep(120 + Math.random() * 180);
        box.value = box.value.slice(0, -1);
        box.dispatchEvent(new Event("input", { bubbles: true }));
        await sleep(80 + Math.random() * 120);
      }

      box.value += ch;
      box.dispatchEvent(new Event("input", { bubbles: true }));

      // Base delay — smoothed noise shifts the mean ±25%
      const baseMean = 110;
      const noiseShift = nextNoise() * 40;   // ±40ms from rhythm
      let delay = baseMean + noiseShift + Math.random() * 80;

      // Bigram speed-up
      if (i + 1 < txt.length) {
        const bi = (ch + txt[i + 1]).toLowerCase();
        if (fastBigrams.has(bi)) delay *= 0.75;
      }

      // Word-break pause
      if (ch === " " && Math.random() < 0.35) delay += 180 + Math.random() * 320;

      // 3% chance of "thinking" pause mid-query
      if (Math.random() < 0.03 && i > 2 && i < txt.length - 3) {
        delay += 500 + Math.random() * 1200;
      }

      await sleep(Math.max(35, delay));
    }
    return true;
  };
}

// Simulate a realistic mouse movement along a Bezier curve between two points.
// This fires mouseover/mousemove events so Google's behavioral sensors see
// non-robotic pointer paths, not just clicks. Runs entirely in-page.
function bezierMouseMoveFn(fromX, fromY, toX, toY, steps) {
  return async function (fx, fy, tx, ty, n) {
    // Control points with random jitter (Bezier cubic)
    const c1x = fx + (tx - fx) * (0.2 + Math.random() * 0.2) + (Math.random() - 0.5) * 40;
    const c1y = fy + (ty - fy) * (0.1 + Math.random() * 0.2) + (Math.random() - 0.5) * 40;
    const c2x = fx + (tx - fx) * (0.6 + Math.random() * 0.2) + (Math.random() - 0.5) * 40;
    const c2y = fy + (ty - fy) * (0.7 + Math.random() * 0.2) + (Math.random() - 0.5) * 40;
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    for (let i = 0; i <= n; i++) {
      const t = i / n;
      const u = 1 - t;
      const x = u*u*u*fx + 3*u*u*t*c1x + 3*u*t*t*c2x + t*t*t*tx;
      const y = u*u*u*fy + 3*u*u*t*c1y + 3*u*t*t*c2y + t*t*t*ty;
      const el = document.elementFromPoint(x, y);
      if (el) {
        const ev = new MouseEvent("mousemove", {
          bubbles: true, cancelable: true,
          clientX: x, clientY: y, view: window,
        });
        el.dispatchEvent(ev);
      }
      await sleep(10 + Math.random() * 15);  // ~60-90fps ish
    }
  };
}

// Human-grade mouse meander across viewport. Upgrades:
//  - Bezier cubic with randomized control points (shy-mouse style)
//  - Bell-curve velocity (ease-in-out cubic — fast middle, slow ends)
//  - 20% overshoot + correct at waypoints
//  - Smoothed noise for jitter (correlated, not chaotic)
//  - Fitts's-Law-inspired movement time: longer = slower per-pixel
//  - Variable polling interval 10-20ms (~60-100Hz)
//  - Occasional hesitation mid-path
async function idleMouseMeander(tabId) {
  await injectInTab(tabId, async () => {
    const w = window.innerWidth, h = window.innerHeight;
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

    // Smoothed 1D noise
    let nX = Math.random() * 2 - 1, nY = Math.random() * 2 - 1;
    const noiseX = () => { nX = nX * 0.65 + (Math.random() * 2 - 1) * 0.35; return nX; };
    const noiseY = () => { nY = nY * 0.65 + (Math.random() * 2 - 1) * 0.35; return nY; };

    // Ease-in-out cubic — bell-curve velocity profile
    const ease = (t) => (t < 0.5 ? 4*t*t*t : 1 - Math.pow(-2*t + 2, 3) / 2);

    // Fitts's Law-ish duration estimate: base + k * log2(distance / target_size)
    function fittsMs(dist) {
      // Assume effective target width ~ 80px. a=120ms, b=80ms/bit.
      const bits = Math.log2(dist / 80 + 1);
      return 120 + 80 * bits + Math.random() * 80;  // jitter
    }

    // Dispatch mousemove event at (px, py)
    function fire(px, py) {
      const el = document.elementFromPoint(px, py);
      if (el) {
        el.dispatchEvent(new MouseEvent("mousemove", {
          bubbles: true, cancelable: true, clientX: px, clientY: py, view: window,
        }));
      }
    }

    // Bezier cubic with ease-in-out velocity + overshoot-correct
    async function moveTo(fx, fy, tx, ty) {
      const dist = Math.hypot(tx - fx, ty - fy);
      const durationMs = fittsMs(dist);
      // Randomized control points (asymmetric so path isn't a perfect arc)
      const c1x = fx + (tx - fx) * (0.15 + Math.random() * 0.25) + noiseX() * 50;
      const c1y = fy + (ty - fy) * (0.10 + Math.random() * 0.20) + noiseY() * 50;
      const c2x = fx + (tx - fx) * (0.60 + Math.random() * 0.25) + noiseX() * 50;
      const c2y = fy + (ty - fy) * (0.70 + Math.random() * 0.20) + noiseY() * 50;
      // 20% overshoot — extend target by 8-15% then correct back
      const overshoot = Math.random() < 0.20;
      const overExt = overshoot ? 0.08 + Math.random() * 0.07 : 0;
      const dx = tx - fx, dy = ty - fy;
      const extX = tx + dx * overExt;
      const extY = ty + dy * overExt;
      const ux = overshoot ? extX : tx;
      const uy = overshoot ? extY : ty;

      const steps = Math.max(15, Math.floor(durationMs / 14));
      const stepMs = durationMs / steps;

      for (let i = 0; i <= steps; i++) {
        const raw = i / steps;
        const t = ease(raw);                     // bell-curve velocity
        const u = 1 - t;
        const x = u*u*u*fx + 3*u*u*t*c1x + 3*u*t*t*c2x + t*t*t*ux + noiseX() * 1.5;
        const y = u*u*u*fy + 3*u*u*t*c1y + 3*u*t*t*c2y + t*t*t*uy + noiseY() * 1.5;
        fire(x, y);
        // Variable polling ~60-100Hz
        await sleep(Math.max(8, stepMs + (Math.random() - 0.5) * 6));
        // 5% chance of mid-path micro-hesitation
        if (Math.random() < 0.05 && i > 3 && i < steps - 3) {
          await sleep(80 + Math.random() * 180);
        }
      }

      // Correct overshoot if needed (short reverse path)
      if (overshoot) {
        await sleep(100 + Math.random() * 200);
        const correctSteps = 5 + Math.floor(Math.random() * 4);
        for (let i = 1; i <= correctSteps; i++) {
          const ct = i / correctSteps;
          const cx = ux + (tx - ux) * ease(ct);
          const cy = uy + (ty - uy) * ease(ct);
          fire(cx, cy);
          await sleep(14 + Math.random() * 8);
        }
      }
    }

    // Meander across 3-5 waypoints
    let x = Math.random() * w, y = Math.random() * h;
    const waypoints = 3 + Math.floor(Math.random() * 3);
    for (let s = 0; s < waypoints; s++) {
      const nx = Math.random() * w, ny = Math.random() * h;
      await moveTo(x, y, nx, ny);
      x = nx; y = ny;
      // Pause at waypoint (human-like)
      await sleep(300 + Math.random() * 700);
    }
  });
}

// YouTube: persona search, scroll, click a video, watch for most of duration
// ─── YouTube activity — 3 entry paths, verified video playback ───
//
// Entry path (random, weighted):
//   A. 40% DIRECT: already on youtube.com → browse homepage → click video
//   B. 30% YT_SEARCH: on youtube.com → type query → click result
//   C. 30% GOOGLE_PATH: navigate to Google → search "<topic> youtube" →
//                      click a YouTube result → video page loads
//
// After click:
//   - Wait for /watch URL to load
//   - Explicitly call video.play() (handles autoplay-blocked background tabs)
//   - Verify playback by checking currentTime advances (retry up to 3x)
//   - Mute + stay on 144p (set via API if possible)
//
// This is what makes YouTube actually record the view in watch history.
async function activityYouTube(tabId, duration, checkAbort, account) {
  await waitForTabLoad(tabId, 15000);
  await abortableSleep(randInt(2000, 4000), checkAbort);

  // Pick entry path
  const pathRoll = Math.random();
  let entryPath;
  if (pathRoll < 0.40) entryPath = "direct";
  else if (pathRoll < 0.70) entryPath = "yt_search";
  else entryPath = "google_path";

  console.log(`[Ecosystem] YouTube entry path: ${entryPath}`);

  const hasPersona = typeof personaForAccount === "function" && account;

  if (entryPath === "google_path") {
    // A. Navigate tab to Google first
    await chrome.tabs.update(tabId, { url: "https://www.google.com/" }).catch(() => {});
    await waitForTabLoad(tabId, 15000);
    await abortableSleep(randInt(1500, 3000), checkAbort);

    // B. Search "<topic> youtube" or "<topic> video"
    let baseQuery = hasPersona
      ? personaYoutubeQuery(personaForAccount(account))
      : "funny videos";
    const suffix = Math.random() < 0.6 ? " youtube" : " video";
    const query = baseQuery + suffix;

    await idleMouseMeander(tabId);
    await chrome.scripting.executeScript({
      target: { tabId }, world: "MAIN",
      func: humanTypeInto(),
      args: ["textarea[name='q'], input[name='q']", query],
    }).catch(() => {});
    await abortableSleep(randInt(400, 900), checkAbort);
    await injectInTab(tabId, () => {
      const box = document.querySelector("textarea[name='q'], input[name='q']");
      if (!box) return;
      const form = box.form || box.closest("form");
      if (form) form.submit();
      else box.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    });
    await abortableSleep(randInt(3000, 5000), checkAbort);
    await waitForTabLoad(tabId, 10000);

    // Scroll search results briefly
    await naturalScroll(tabId, randInt(1, 3), checkAbort);

    // C. Click a YouTube result link (prefer watch URL, fallback any youtube.com)
    const clickedYT = await injectInTab(tabId, () => {
      const results = Array.from(document.querySelectorAll("#search a, #rso a"));
      // Prefer video results (watch URLs)
      const watchLinks = results.filter((a) =>
        a.href && /youtube\.com\/watch\?v=/.test(a.href)
      );
      const ytLinks = watchLinks.length ? watchLinks
        : results.filter((a) => a.href && a.href.includes("youtube.com"));
      if (!ytLinks.length) return false;
      const pick = ytLinks[Math.floor(Math.random() * Math.min(5, ytLinks.length))];
      pick.click();
      return true;
    });

    if (!clickedYT) {
      // Fallback: navigate to youtube.com directly
      await chrome.tabs.update(tabId, { url: "https://www.youtube.com/" }).catch(() => {});
    }
    await abortableSleep(randInt(3000, 5000), checkAbort);
    await waitForTabLoad(tabId, 15000);
  }
  else if (entryPath === "yt_search" && hasPersona) {
    // Already on youtube.com → type a persona query
    const q = personaYoutubeQuery(personaForAccount(account));
    await idleMouseMeander(tabId);
    await chrome.scripting.executeScript({
      target: { tabId }, world: "MAIN",
      func: humanTypeInto(),
      args: ["input#search, input[name='search_query']", q],
    }).catch(() => {});
    await abortableSleep(randInt(400, 900), checkAbort);
    await injectInTab(tabId, () => {
      const box = document.querySelector("input#search, input[name='search_query']");
      if (!box) return;
      const form = box.form || box.closest("form");
      if (form) form.submit();
      else box.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    });
    await abortableSleep(randInt(3000, 5000), checkAbort);
    await waitForTabLoad(tabId, 10000);
  }
  // else "direct": just homepage browsing, no search

  // Browse whatever page we're on (homepage / search results / already-at-video)
  await naturalScroll(tabId, randInt(2, 4), checkAbort);

  // If we're NOT already on a /watch page, click a video thumbnail
  const isOnWatchPage = await injectInTab(tabId, () => /\/watch\?v=/.test(location.href));
  if (!isOnWatchPage) {
    const clickResult = await injectInTab(tabId, () => {
      // YouTube thumb selectors (DOM differs between homepage / results / shelf)
      const selectors = [
        "a#thumbnail[href*='watch']",
        "ytd-thumbnail a[href*='watch']",
        "a.ytd-thumbnail[href*='watch']",
        "ytd-rich-item-renderer a[href*='watch']",
      ];
      let thumbs = [];
      for (const sel of selectors) {
        thumbs = Array.from(document.querySelectorAll(sel));
        if (thumbs.length) break;
      }
      if (!thumbs.length) return { clicked: false, reason: "no_thumbs" };
      const pick = thumbs[Math.floor(Math.random() * Math.min(thumbs.length, 10))];
      pick.click();
      return { clicked: true, href: pick.href };
    });
    if (!clickResult?.clicked) {
      console.warn("[Ecosystem] YouTube thumb click failed:", clickResult?.reason);
    }
    // Wait for navigation to /watch
    await abortableSleep(randInt(2500, 4500), checkAbort);
    await waitForTabLoad(tabId, 15000);
  }

  // Force video to actually play — handles background-tab autoplay blocking.
  // Verify by checking currentTime advances; retry up to 3 times.
  const playResult = await injectInTab(tabId, async () => {
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    // Dismiss any "Continue watching?" / age-gate / ad-skip overlays briefly
    const dismissers = [
      "button[aria-label*='Skip']",
      "button.ytp-ad-skip-button",
      "tp-yt-paper-button.ytd-confirm-dialog-renderer",
    ];
    for (const sel of dismissers) {
      const btn = document.querySelector(sel);
      if (btn) { try { btn.click(); } catch {} }
    }
    // Find the video element
    let attempts = 0;
    let vid = null;
    while (attempts < 20 && !vid) {
      vid = document.querySelector("video");
      if (!vid) { await sleep(300); attempts++; }
    }
    if (!vid) return { ok: false, reason: "no_video_el" };
    // Mute & lower quality hint
    vid.muted = true;
    vid.volume = 0;
    // Try to start playback (retry up to 3 times over ~6 sec)
    const t0 = vid.currentTime;
    for (let i = 0; i < 3; i++) {
      try { await vid.play(); } catch {}
      await sleep(2000);
      if (vid.currentTime > t0 + 0.3) {
        return { ok: true, currentTime: vid.currentTime, duration: vid.duration };
      }
    }
    return { ok: false, reason: "playback_not_advancing", currentTime: vid.currentTime };
  });

  if (playResult?.ok) {
    console.log(`[Ecosystem] YouTube video playing (t=${playResult.currentTime?.toFixed(1)}s)`);
  } else {
    console.warn(`[Ecosystem] YouTube video not playing: ${playResult?.reason}`);
  }

  // Watch loop — during watch, occasionally scroll comments
  const watchTime = Math.max(30000, duration - 20000);
  const start = Date.now();
  while (Date.now() - start < watchTime) {
    if (checkAbort && checkAbort()) throw new Error("aborted");
    await abortableSleep(randInt(10000, 25000), checkAbort);
    // 40% chance: scroll comments
    if (Math.random() < 0.4) {
      await injectInTab(tabId, () => {
        window.scrollBy({ top: 400 + Math.random() * 500, behavior: "smooth" });
      });
    }
    // 15% chance: re-assert play if paused (YouTube may pause bg tab)
    if (Math.random() < 0.15) {
      await injectInTab(tabId, async () => {
        const v = document.querySelector("video");
        if (v && v.paused) {
          try { await v.play(); } catch {}
        }
      });
    }
  }
}

// Gmail: open inbox, scroll, hover/click emails briefly
async function activityGmail(tabId, duration, checkAbort) {
  await waitForTabLoad(tabId, 20000);
  await abortableSleep(randInt(3000, 6000), checkAbort);
  await naturalScroll(tabId, randInt(3, 6), checkAbort);

  // Try to click first email to read
  await injectInTab(tabId, () => {
    const row = document.querySelector("tr[role='row'], div[role='main'] tr");
    if (row) row.click();
  });
  await abortableSleep(randInt(5000, 12000), checkAbort);

  // Go back to inbox
  await injectInTab(tabId, () => {
    const back = document.querySelector("[aria-label='Back to Inbox'], [aria-label='Back to inbox']");
    if (back) back.click();
  });
  // Scroll rest
  await naturalScroll(tabId, randInt(2, 4), checkAbort);
}

// Google Search: type a query, submit, scroll results, click one
async function activitySearch(tabId, duration, checkAbort, account) {
  await waitForTabLoad(tabId, 15000);
  await abortableSleep(randInt(1500, 3500), checkAbort);

  // Prefer persona-driven query if available
  let query;
  if (typeof personaForAccount === "function" && account) {
    query = personaSearchQuery(personaForAccount(account));
  } else {
    query = rand(ECOSYSTEM_SEARCH_QUERIES);
  }

  // Mouse meander before typing — looks more realistic to Google's ML
  await idleMouseMeander(tabId);

  // Human-like typing with typos + natural delays (Phase 5)
  await chrome.scripting.executeScript({
    target: { tabId }, world: "MAIN",
    func: humanTypeInto(),
    args: ["textarea[name='q'], input[name='q']", query],
  }).catch(() => {});

  // Brief pause before submit (reading what we typed)
  await abortableSleep(randInt(300, 900), checkAbort);

  // Submit
  await injectInTab(tabId, () => {
    const box = document.querySelector("textarea[name='q'], input[name='q']");
    if (!box) return;
    const form = box.form || box.closest("form");
    if (form) form.submit();
    else box.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  });

  await abortableSleep(randInt(3000, 5000), checkAbort);
  await waitForTabLoad(tabId, 10000);
  await naturalScroll(tabId, randInt(2, 5), checkAbort);

  // Click a result (50% chance)
  if (Math.random() < 0.5) {
    await injectInTab(tabId, () => {
      const links = Array.from(document.querySelectorAll("#search a h3, #rso a h3"));
      if (links.length) {
        const pick = links[Math.floor(Math.random() * Math.min(5, links.length))];
        pick.click();
      }
    });
    await abortableSleep(randInt(5000, 15000), checkAbort);
    await naturalScroll(tabId, randInt(2, 4), checkAbort);
  }
}

// Google Maps: type a location, view results
async function activityMaps(tabId, duration, checkAbort, account) {
  await waitForTabLoad(tabId, 20000);
  await abortableSleep(randInt(3000, 6000), checkAbort);

  let q;
  if (typeof personaForAccount === "function" && account) {
    q = personaMapsQuery(personaForAccount(account));
  } else {
    q = rand(ECOSYSTEM_MAPS_QUERIES);
  }
  await idleMouseMeander(tabId);
  await chrome.scripting.executeScript({
    target: { tabId }, world: "MAIN",
    func: humanTypeInto(),
    args: ["input#searchboxinput, input[name='q'], input[placeholder]", q],
  }).catch(() => {});
  await abortableSleep(randInt(400, 900), checkAbort);
  await injectInTab(tabId, () => {
    const box = document.querySelector("input#searchboxinput, input[name='q'], input[placeholder]");
    if (box) box.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
  });

  await abortableSleep(randInt(5000, 10000), checkAbort);
  await naturalScroll(tabId, randInt(2, 4), checkAbort);
}

// Google Drive: scroll
async function activityDrive(tabId, duration, checkAbort) {
  await waitForTabLoad(tabId, 15000);
  await abortableSleep(randInt(3000, 6000), checkAbort);
  await naturalScroll(tabId, randInt(3, 6), checkAbort);
}

// Google News: scroll, click article
async function activityNews(tabId, duration, checkAbort) {
  await waitForTabLoad(tabId, 15000);
  await abortableSleep(randInt(2000, 4000), checkAbort);
  await naturalScroll(tabId, randInt(3, 5), checkAbort);

  if (Math.random() < 0.5) {
    await injectInTab(tabId, () => {
      const links = Array.from(document.querySelectorAll("article a, a[href*='/articles/']"));
      if (links.length) {
        const pick = links[Math.floor(Math.random() * Math.min(10, links.length))];
        pick.click();
      }
    });
    await abortableSleep(randInt(5000, 15000), checkAbort);
    await naturalScroll(tabId, randInt(2, 4), checkAbort);
  }
}

// Google Photos: scroll
async function activityPhotos(tabId, duration, checkAbort) {
  await waitForTabLoad(tabId, 15000);
  await abortableSleep(randInt(3000, 6000), checkAbort);
  await naturalScroll(tabId, randInt(3, 5), checkAbort);
}

function ecosystemPickActivity(profile) {
  // Apply smart check: services with no content get weight 0.
  const weighted = ECOSYSTEM_ACTIVITIES.map((a) => ({
    activity: a,
    weight: effectiveWeight(a, profile),
  })).filter((x) => x.weight > 0);
  if (!weighted.length) return ECOSYSTEM_ACTIVITIES[0];
  const totalWeight = weighted.reduce((s, x) => s + x.weight, 0);
  let r = Math.random() * totalWeight;
  for (const x of weighted) {
    r -= x.weight;
    if (r <= 0) return x.activity;
  }
  return weighted[0].activity;
}

function ecosystemPickAccount() {
  // Pick a random logged-in account that isn't held and hasn't hit daily cap.
  const today = new Date().toISOString().slice(0, 10);
  if (ecosystemState.lastReset !== today) {
    ecosystemState.todayCounts = {};
    ecosystemState.lastReset = today;
  }
  const candidates = Object.values(connectedAccounts)
    .filter((a) => a.logged_in && a.email)
    .filter((a) => !(a.email in ecosystemHeldAccounts))
    .filter((a) => (ecosystemState.todayCounts[a.email] || 0) < ECOSYSTEM_MAX_PER_DAY);
  if (!candidates.length) return null;
  return candidates[Math.floor(Math.random() * candidates.length)];
}

// Gaussian-ish random gap (Box-Muller), time-of-day aware.
// Hours 1 AM - 7 AM: huge gaps (simulate sleep).
// Hours 11 PM - 1 AM and 7 AM - 9 AM: reduced activity.
function ecosystemNextGap() {
  let u = 0, v = 0;
  while (u === 0) u = Math.random();
  while (v === 0) v = Math.random();
  const z = Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
  const mean = (ECOSYSTEM_MIN_GAP_MS + ECOSYSTEM_MAX_GAP_MS) / 2;
  const std = (ECOSYSTEM_MAX_GAP_MS - ECOSYSTEM_MIN_GAP_MS) / 4;
  let gap = mean + z * std;
  gap = Math.max(ECOSYSTEM_MIN_GAP_MS, Math.min(ECOSYSTEM_MAX_GAP_MS, gap));

  // Time-of-day multiplier
  const hr = new Date().getHours();
  let mult = 1.0;
  if (hr >= 1 && hr < 7) mult = 4.0;       // deep sleep: 4x longer gaps
  else if (hr >= 23 || hr < 1) mult = 2.0; // late night: 2x
  else if (hr >= 7 && hr < 9) mult = 1.5;  // early morning: 1.5x
  return Math.floor(gap * mult);
}

async function ecosystemReportActivity(account, site, action, durationSec) {
  try {
    await fetch(`${BRIDGE_URL}/ecosystem/activity`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ account, site, action, duration_sec: durationSec || 0 }),
    });
  } catch {}
}

async function ecosystemAbortCurrent(reason) {
  if (!ecosystemState.running) return;
  const { currentAccount, currentSite, currentTabId } = ecosystemState;
  console.log(`[Ecosystem] Aborting ${currentSite} on ${currentAccount}: ${reason}`);
  ecosystemState.running = false;
  if (currentTabId) {
    try { await chrome.tabs.remove(currentTabId); } catch {}
  }
  ecosystemState.currentTabId = null;
  ecosystemState.currentAccount = "";
  ecosystemState.currentSite = "";
  ecosystemReportActivity(currentAccount, currentSite, "abort", 0);
}

async function ecosystemRunActivity() {
  if (ecosystemState.running) return;
  if (ecosystemDirective !== "active") return;

  const account = ecosystemPickAccount();
  if (!account) {
    console.log("[Ecosystem] No eligible accounts — waiting");
    ecosystemState.nextActivityAt = Date.now() + 5 * 60 * 1000;
    return;
  }
  // Probe services for this account (cached 24h) and pick activity weighted
  // by what content this account actually has.
  const profile = await probeAccountServices(account.email);
  const activity = ecosystemPickActivity(profile);
  const [minDur, maxDur] = activity.duration;
  const duration = Math.floor(minDur + Math.random() * (maxDur - minDur)) * 1000;

  ecosystemState.running = true;
  ecosystemState.currentAccount = account.email;
  ecosystemState.currentSite = activity.name;
  ecosystemState.currentStartedAt = Date.now();
  ecosystemState.currentDurationMs = duration;

  console.log(
    `[Ecosystem] ${account.email} → ${activity.name} for ${Math.round(duration / 1000)}s`
  );
  logEcosystem("start", {
    account: account.email,
    site: activity.name,
    duration_sec: Math.round(duration / 1000),
  });
  ecosystemReportActivity(account.email, activity.name, "start", 0);

  let tabId = null;
  const startedAtWall = Date.now();
  try {
    // Open background tab (invisible to user)
    const tab = await chrome.tabs.create({ url: activity.url, active: false });
    tabId = tab.id;
    ecosystemState.currentTabId = tabId;

    // Abort check function — fast path for directive changes or external aborts
    const checkAbort = () => {
      if (ecosystemDirective !== "active") return true;
      if (!ecosystemState.running) return true;
      return false;
    };

    // Site-specific executor handles the real interaction
    if (typeof activity.run === "function") {
      await activity.run(tabId, duration, checkAbort, account.email);
    } else {
      // Fallback: just wait (for activities without executor)
      await abortableSleep(duration, checkAbort);
    }

    // Activity completed — track count
    ecosystemState.todayCounts[account.email] =
      (ecosystemState.todayCounts[account.email] || 0) + 1;
    const elapsedSec = Math.round((Date.now() - startedAtWall) / 1000);
    logEcosystem("end", {
      account: account.email, site: activity.name, duration_sec: elapsedSec,
    });
    ecosystemReportActivity(account.email, activity.name, "end", elapsedSec);
  } catch (e) {
    const msg = e && e.message ? e.message : String(e);
    if (msg === "aborted" || msg.startsWith("directive_")) {
      console.log(`[Ecosystem] Activity stopped: ${msg}`);
      logEcosystem("abort", { account: account.email, site: activity.name, reason: msg });
    } else {
      console.warn(`[Ecosystem] Activity error: ${msg}`);
      logEcosystem("error", { account: account.email, site: activity.name, error: msg });
    }
    ecosystemReportActivity(account.email, activity.name, "error", 0);
  } finally {
    if (tabId) {
      try { await chrome.tabs.remove(tabId); } catch {}
    }
    ecosystemState.running = false;
    ecosystemState.currentTabId = null;
    ecosystemState.currentAccount = "";
    ecosystemState.currentSite = "";
    // Schedule next activity
    ecosystemState.nextActivityAt = Date.now() + ecosystemNextGap();
  }
}

// ─── Phase 6: Resource / safety guards ───
// Check battery via offscreen document (service workers can't access
// navigator.getBattery directly). Fallback: assume plugged in if unavailable.
let _lastBatteryCheck = 0;
let _batteryCache = { level: 1, charging: true };
async function checkBatterySafe() {
  const now = Date.now();
  if (now - _lastBatteryCheck < 60000) return _batteryCache;
  _lastBatteryCheck = now;
  try {
    // Run navigator.getBattery() inside a tab we already control
    // (we don't have one if idle, so we skip check and assume OK)
    const tabs = await chrome.tabs.query({ url: "https://*/*" });
    if (!tabs.length) return _batteryCache;
    const result = await chrome.scripting.executeScript({
      target: { tabId: tabs[0].id }, world: "MAIN",
      func: async () => {
        if (!navigator.getBattery) return { level: 1, charging: true };
        const b = await navigator.getBattery();
        return { level: b.level, charging: b.charging };
      },
    });
    if (result?.[0]?.result) _batteryCache = result[0].result;
  } catch {}
  return _batteryCache;
}

// Detect if user is actively typing/clicking in Chrome.
// Previously tracked tab-switch + focus changes too, but those trigger
// when user opens the popup — causing warmup to forever-defer while
// user tests the extension. Now we only count ACTUAL interaction
// (tab URL changes by user navigation, which excludes our own bg tabs).
let _lastUserActivity = 0;
chrome.tabs.onUpdated.addListener((tid, changeInfo, tab) => {
  // Only count navigation to a new URL (real user action), not load completion.
  // Also skip if this is one of our own ecosystem background tabs.
  if (changeInfo.url && ecosystemState.currentTabId !== tid) {
    _lastUserActivity = Date.now();
  }
});

function userRecentlyActive() {
  // 45s window, only counts real navigations. Less aggressive than before.
  return (Date.now() - _lastUserActivity) < 45 * 1000;
}

// Ecosystem scheduler tick — fires every 30s
async function ecosystemTick() {
  if (ecosystemDirective !== "active") return;
  if (ecosystemState.running) return;
  if (Date.now() < ecosystemState.nextActivityAt) return;

  // Schedule first run 1-5 min after becoming active
  if (ecosystemState.nextActivityAt === 0) {
    ecosystemState.nextActivityAt = Date.now() + (60 + Math.random() * 240) * 1000;
    return;
  }

  // Phase 6 safety guards

  // User is actively navigating Chrome? Back off briefly.
  if (userRecentlyActive()) {
    ecosystemState.deferReason = "user_navigating";
    ecosystemState.nextActivityAt = Date.now() + 90 * 1000;  // 90s, not 5 min
    return;
  }

  // Battery check
  const battery = await checkBatterySafe();
  if (!battery.charging && battery.level < 0.30) {
    console.log(`[Ecosystem] Battery ${Math.round(battery.level * 100)}% not charging — paused`);
    ecosystemState.deferReason = "low_battery";
    ecosystemState.nextActivityAt = Date.now() + 15 * 60 * 1000;
    return;
  }
  if (!battery.charging && battery.level < 0.50) {
    // Half the frequency on medium battery
    ecosystemState.nextActivityAt = Date.now() + ecosystemNextGap();
    if (Math.random() < 0.5) {
      ecosystemState.deferReason = "battery_conserve";
      return;
    }
  }

  ecosystemState.deferReason = "";
  await ecosystemRunActivity();
}

// ═══════════════════════════════════════════════════════════════════
// Lifecycle
// ═══════════════════════════════════════════════════════════════════

// ═══════════════════════════════════════════════════════════════════
// Flow reCAPTCHA Warmup
// ═══════════════════════════════════════════════════════════════════
//
// Real users on labs.google.com trigger grecaptcha.enterprise.execute()
// hundreds of times per minute (verified by wrapping execute in the
// console — labs.google.com itself fires ~200-1000 calls/min in the
// background, presumably to keep the v3 trust score high).
//
// Without this warmup, our extension only calls execute() ~once per
// dispatched job. That isolated/bursty pattern looked enough like a bot
// to Google that even a fresh paid Ultra account got rejected with
// PUBLIC_ERROR_UNUSUAL_ACTIVITY on every video request — while the same
// account succeeded instantly when generating manually from the page.
//
// The warmup installs a self-running loop INSIDE each labs.google.com
// tab (one-time injection per tab, marker on window) that calls
// execute() on a 1.5–3.5s jittered interval and discards the resulting
// tokens. Mimics the natural site behaviour so our "real" execute()
// calls land with a healthy score.

async function installFlowRecaptchaWarmup() {
  let tabs;
  try {
    tabs = await chrome.tabs.query({ url: `${LABS_ORIGIN}/*` });
  } catch {
    return;
  }
  for (const tab of tabs) {
    try {
      await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        world: "MAIN",
        func: () => {
          // Idempotent — only one warmup loop per tab.
          if (window.__glabsRecaptchaWarmupActive) return;
          window.__glabsRecaptchaWarmupActive = true;

          const ACTIONS = ["IMAGE_GENERATION", "VIDEO_GENERATION"];
          // Slowed warmup cadence — UPDATED May 2026.
          //
          // Original cadence (1.5-3.5s) was chosen back when:
          //   1) Tokens went into a cached pool that submission code
          //      consumed via Python aiohttp, and
          //   2) The API call originated outside the labs.google tab
          //      so Google's score model rejected isolated/cold tokens.
          // In that world the loop kept the tab's per-session score
          // primed so cached pool tokens passed validation.
          //
          // Both preconditions are gone now. After the 2026-05-11 fix,
          // every real submission mints its own fresh reCAPTCHA token
          // at submit time INSIDE the labs.google tab via the same
          // EXECUTE_FETCH path the video endpoint uses, with full
          // Chrome-signed headers. So the warmup pool tokens are no
          // longer used by submission code at all.
          //
          // Meanwhile the high cadence is now actively harmful: ~25-40
          // execute() calls per minute per tab, with almost none of
          // those tokens consumed, looks like a bot signature to
          // Google's reCAPTCHA Enterprise model. Production logs from
          // a 41-image run showed pool sizes hitting 54 IMAGE + 140
          // VIDEO captures (≈194 wasted execute() calls vs 41 real
          // submissions) — a 5× ratio of unused-to-used token mints.
          // Right after that imbalance accumulated, Google flipped the
          // account into a "reCAPTCHA evaluation failed" 429 cooldown.
          //
          // 30-60s matches roughly how often a real labs.google user's
          // own page code naturally re-runs execute() during active
          // use (hovers, focus changes, model picks) — high enough to
          // signal "this tab is alive" but low enough that we're not
          // overwhelmingly out of distribution. Fresh-mint at submit
          // time handles the actual score requirement for the real
          // POST.
          const MIN_DELAY_MS = 30000;
          const MAX_DELAY_MS = 60000;

          // Resolve the site key the same way our token code does, but
          // lazy — don't crash if the page hasn't fully loaded yet.
          function findSiteKey() {
            try {
              const clients = window.___grecaptcha_cfg && window.___grecaptcha_cfg.clients;
              if (clients) {
                for (const id of Object.keys(clients)) {
                  const c = clients[id];
                  if (!c || typeof c !== "object") continue;
                  const walk = (obj, depth) => {
                    if (depth > 5 || !obj || typeof obj !== "object") return null;
                    for (const k of Object.keys(obj)) {
                      const v = obj[k];
                      if (typeof v === "string" && v.length >= 20 && v.length <= 50
                        && /^[A-Za-z0-9_-]+$/.test(v)) {
                        if (document.querySelector('script[src*="render=' + v + '"]')) return v;
                      }
                      if (typeof v === "object" && v !== null) {
                        const r = walk(v, depth + 1);
                        if (r) return r;
                      }
                    }
                    return null;
                  };
                  const k = walk(c, 0);
                  if (k) return k;
                }
              }
            } catch {}
            for (const s of document.querySelectorAll('script[src*="recaptcha"][src*="render="]')) {
              try {
                const r = new URL(s.src).searchParams.get("render");
                if (r && r !== "explicit") return r;
              } catch {}
            }
            return null;
          }

          let consecutiveFailures = 0;
          async function tick() {
            try {
              const enterprise = window.grecaptcha && window.grecaptcha.enterprise;
              if (enterprise && typeof enterprise.execute === "function") {
                const siteKey = findSiteKey();
                if (siteKey) {
                  const action = ACTIONS[Math.floor(Math.random() * ACTIONS.length)];
                  if (typeof enterprise.ready === "function") {
                    await new Promise((r) => enterprise.ready(r));
                  }
                  // Generate + discard. Goal is the score-keeping side
                  // effect, not the token itself.
                  await enterprise.execute(siteKey, { action });
                  consecutiveFailures = 0;
                }
              }
            } catch {
              consecutiveFailures++;
              // Back off on persistent failure (page broken / not ready)
              if (consecutiveFailures > 5) {
                window.__glabsRecaptchaWarmupActive = false;
                return;  // stop loop
              }
            }
            const delay = MIN_DELAY_MS + Math.random() * (MAX_DELAY_MS - MIN_DELAY_MS);
            setTimeout(tick, delay);
          }

          // First tick after a short randomized delay so multi-tab
          // warmups don't all fire on the same instant.
          setTimeout(tick, 500 + Math.random() * 1500);
        },
      });
    } catch {
      // Tab not scriptable (chrome:// URL, closed mid-call, etc.) — skip
    }
  }
}

// Re-run the installer every 30s so newly opened labs tabs pick up the
// warmup. Idempotent because of the window.__glabsRecaptchaWarmupActive
// flag — re-injecting an already-warm tab is a no-op.
// Background warmup loop — RESTORED from Apr 19 working build. Fires
// grecaptcha.enterprise.execute() at a jittered 1.5–3.5s cadence on
// every open labs tab to keep the tab's reCAPTCHA Enterprise score
// primed. The concurrent 150-video test run proved this is the right
// cadence — the tab-level score primes the real tokens and the real
// video POSTs pass. Disabling this on Apr 21 broke reCAPTCHA scoring
// for everyone — score decays between real requests without activity.
setInterval(installFlowRecaptchaWarmup, 30000);
// First run after 5s so Chrome finishes loading the tab + reCAPTCHA SDK
setTimeout(installFlowRecaptchaWarmup, 5000);

// Also install warmup right after a labs.google tab finishes loading
// (caught by the existing onUpdated listener below — see line ~1875).

// Start polling bridge — 500ms interval, but _workInProgress lock
// prevents concurrent handleWork calls. This ensures fast pickup
// of queued work (important when 6+ jobs are pending).
setInterval(pollBridge, 500);

// Ecosystem scheduler — checks every 30s whether to fire an activity.
// Heavy work is gated by directive + nextActivityAt, so idle cost is minimal.
setInterval(ecosystemTick, 30000);

// Detect accounts periodically
setInterval(detectAccounts, ACCOUNT_DETECT_INTERVAL);

// Initial detection after 2s (give Chrome time to load tabs)
setTimeout(detectAccounts, 2000);

// Listen for Labs tab changes
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === "complete" && tab.url && tab.url.startsWith(LABS_ORIGIN)) {
    invalidateRecaptchaCache(tabId);  // force fresh check after reload
    setTimeout(() => detectAccounts(), 3000);
    // Re-install warmup loop after reload (window flag was wiped).
    setTimeout(installFlowRecaptchaWarmup, 4000);
  }
});

// Clean up when tab closes
chrome.tabs.onRemoved.addListener((tabId) => {
  delete connectedAccounts[tabId];
  invalidateRecaptchaCache(tabId);
});

// Messages from popup
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "getStatus") {
    const accs = Object.values(connectedAccounts).filter((a) => a.logged_in);
    const gensparkStatus = (typeof self.gensparkGetStatus === "function")
      ? self.gensparkGetStatus()
      : { connected: false, accounts: [], lastError: "" };
    sendResponse({
      connected: bridgeConnected,
      accounts: accs.map((a) => ({ email: a.email, name: a.name })),
      tokenCount,
      lastError: lastPollError,
      genspark: gensparkStatus,
      ecosystem: {
        directive: ecosystemComputeDirective(),
        enabled: ecosystemEnabledLocal,
        bridgeOnline: bridgeConnected,
        running: ecosystemState.running,
        currentAccount: ecosystemState.currentAccount,
        currentSite: ecosystemState.currentSite,
        currentStartedAt: ecosystemState.currentStartedAt,
        currentDurationMs: ecosystemState.currentDurationMs,
        nextActivityAt: ecosystemState.nextActivityAt,
        deferReason: ecosystemState.deferReason || "",
        heldAccounts: ecosystemHeldAccounts,
        todayCounts: ecosystemState.todayCounts,
        log: ecosystemState.log.slice(-15),  // last 15 events
      },
    });
    return false;
  }

  if (msg.type === "ecosystemToggle") {
    // Apply locally IMMEDIATELY (works even if bridge offline)
    const enabled = !!msg.enabled;
    ecosystemEnabledLocal = enabled;
    try {
      if (chrome.storage && chrome.storage.local) {
        chrome.storage.local.set({ ecosystemEnabledLocal: enabled });
      }
    } catch (e) {
      console.warn("[Ecosystem] storage.set failed:", e.message);
    }
    console.log(`[Ecosystem] Local toggle: ${enabled ? "ON" : "OFF"}`);

    // Abort running activity if user turned OFF
    if (!enabled && ecosystemState.running) {
      ecosystemAbortCurrent("user_toggle_off");
    }

    // Also try to sync to bridge — fire and forget, don't block the UI
    fetch(`${BRIDGE_URL}/ecosystem`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    }).catch(() => {});

    // Respond immediately with local state
    sendResponse({ ok: true, enabled });
    return false;
  }

  if (msg.type === "ecosystemReleaseAccount") {
    fetch(`${BRIDGE_URL}/ecosystem`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ release_account: msg.account }),
    })
      .then((r) => r.json())
      .then((data) => sendResponse({ ok: true, ...data }))
      .catch((e) => sendResponse({ ok: false, error: e.message }));
    return true;
  }

  if (msg.type === "detectAccounts") {
    detectAccounts().then((accounts) => sendResponse({ accounts }));
    return true; // async response
  }

  if (msg.type === "openLabsTab") {
    chrome.tabs.create({ url: `${LABS_ORIGIN}/fx/tools/flow` });
    sendResponse({ ok: true });
    return false;
  }
});

// ─── MV3 Service Worker Keep-Alive ───
// Manifest V3 suspends background service workers after ~30s of inactivity.
// When suspended, the next chrome.scripting.executeScript call can fail with
// "fetch_failed: Failed to fetch" or "no_script_result" while the worker
// wakes up. To prevent this, we register a chrome.alarms alarm that fires
// every 24 seconds. The alarm event itself resets the idle timer, keeping
// the worker persistently alive during active generation runs.
//
// This is the Chrome-sanctioned workaround for MV3 worker suspension.
// Cost: ~0MB RAM, zero network, zero battery. Benefit: eliminates the
// intermittent "Bridge error: fetch_failed" / "no_script_result" errors
// that occur when the worker naps between parallel POSTs.
try {
  if (chrome.alarms && chrome.alarms.create) {
    chrome.alarms.create("gLabsKeepAlive", { periodInMinutes: 0.4 }); // 24s
    chrome.alarms.onAlarm.addListener((alarm) => {
      if (alarm.name === "gLabsKeepAlive") {
        // No-op — the act of firing this event wakes/keeps the worker alive.
      }
    });
    console.log("[G-Labs Helper] Keep-alive alarm registered (24s interval)");
  }
} catch (e) {
  console.warn("[G-Labs Helper] Keep-alive alarm failed:", e.message);
}

console.log("[G-Labs Helper] Extension started. Bridge:", BRIDGE_URL);

// Start Genspark module if it loaded successfully — independent of Flow.
// Silently stays idle when the Genspark bridge isn't running.
try {
  if (typeof self.gensparkStart === "function") {
    self.gensparkStart();
  }
} catch (e) {
  console.warn("[G-Labs Helper] Genspark module failed to start:", e);
}

// Start Grok module — independent of Flow/Genspark.
// Silently stays idle when the Grok bridge (port 18926) isn't running.
try {
  if (typeof self.grokStart === "function") {
    self.grokStart();
  }
} catch (e) {
  console.warn("[G-Labs Helper] Grok module failed to start:", e);
}
