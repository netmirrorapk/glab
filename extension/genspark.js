/**
 * G-Labs Studio Helper — Genspark Module
 *
 * Standalone module loaded via importScripts() in background.js.
 * Talks to GensparkBridge at http://127.0.0.1:18925 — completely separate
 * from the Flow bridge (18924). Flow and Genspark can run side-by-side
 * without interfering with each other.
 *
 * Responsibilities:
 *   1. Detect logged-in Genspark accounts (via /api/user/me)
 *   2. Periodically report accounts to the Genspark bridge
 *   3. Poll the bridge for pending image-generation work
 *   4. Execute each work item:
 *        a. Fetch a fresh reCAPTCHA Enterprise token
 *        b. POST /api/agent/ask_proxy with prompt + token
 *        c. Parse SSE stream to extract task_id
 *        d. Poll /api/spark/image_generation_task_detail until COMPLETED
 *        e. Fetch the image bytes from the returned URL
 *        f. Send result (base64 image + metadata) back to the bridge
 *
 * Runs only when the bridge is reachable on port 18925. Silent otherwise.
 */

const GENSPARK_BRIDGE_URL = "http://127.0.0.1:18925";
const GENSPARK_POLL_INTERVAL = 1500;            // ms between poll calls
const GENSPARK_ACCOUNT_DETECT_INTERVAL = 15000; // ms between account detection
const GENSPARK_ORIGIN = "https://www.genspark.ai";

// ─── State ───
let gensparkBridgeConnected = false;
let gensparkAccounts = {};  // email -> { email, plan_type, tab_id, ... }
let gensparkLastPollError = "";
// Parallel concurrency control — how many image generations may run
// concurrently inside this extension. The Python side (genspark_mode.py)
// already gates on the UI "Parallel" dropdown (1..40); this cap is a
// defence-in-depth ceiling so a runaway Python side can't blow past what
// Genspark itself can sustain. 40 matches the UI maximum. Plus-plan users
// who pick a high value may still hit the 5-hour session rate limit —
// genspark_mode.py logs a warning when that risk applies.
const GENSPARK_MAX_PARALLEL = 40;
let gensparkActiveCount = 0;

// ═══════════════════════════════════════════════════════════════════
// Account detection
// ═══════════════════════════════════════════════════════════════════

async function gensparkDetectAccounts() {
  try {
    const tabs = await chrome.tabs.query({ url: `${GENSPARK_ORIGIN}/*` });
    if (!tabs.length) {
      gensparkAccounts = {};
      return;
    }

    const fresh = {};
    for (const tab of tabs) {
      try {
        const result = await chrome.scripting.executeScript({
          target: { tabId: tab.id },
          world: "MAIN",
          func: async () => {
            // Try multiple endpoints — Genspark's actual user endpoint
            // name isn't documented so we probe a few common patterns.
            const endpoints = [
              "/api/user/me",
              "/api/user/info",
              "/api/user",
              "/api/me",
              "/api/auth/session",
              "/api/auth/me",
              "/api/account/info",
              "/api/account/me",
              "/api/v1/user",
              "/api/v1/me",
            ];
            let userInfo = null;
            let hitEndpoint = "";
            let lastStatus = 0;
            for (const ep of endpoints) {
              try {
                const r = await fetch(ep, {
                  method: "GET",
                  credentials: "include",
                  headers: { "Accept": "application/json" },
                });
                lastStatus = r.status;
                if (!r.ok) continue;
                const ctype = r.headers.get("content-type") || "";
                if (!ctype.includes("json")) continue;
                const data = await r.json().catch(() => null);
                if (!data) continue;
                // Genspark wraps responses in {status, message, data}
                const u = data?.data || data?.user || data;
                const email =
                  u?.email || u?.user_email || u?.userEmail ||
                  u?.account?.email || u?.profile?.email || "";
                if (!email) continue;
                userInfo = {
                  email,
                  plan_type: String(
                    u?.subscription?.plan || u?.subscription?.tier ||
                    u?.subscription_plan || u?.plan_type || u?.plan ||
                    u?.subscription?.type || u?.membership_type ||
                    u?.membership?.plan || "free"
                  ).toLowerCase(),
                  user_id: String(u?.id || u?.user_id || u?.uid || ""),
                  display_name: u?.display_name || u?.name ||
                                u?.username || u?.nickname || "",
                };
                hitEndpoint = ep;
                break;
              } catch (_e) {
                // Try next endpoint
              }
            }

            // Fallback 1: DOM-based detection — if the page shows the
            // user's profile icon with initial or email in accessible
            // attributes, treat as logged in even without an API hit.
            if (!userInfo) {
              // Look for user email in common DOM patterns
              const emailFromDom = (() => {
                // Data attributes
                const el = document.querySelector(
                  "[data-user-email], [data-email], [aria-label*='@']"
                );
                if (el) {
                  const attrs = ["data-user-email", "data-email", "aria-label"];
                  for (const a of attrs) {
                    const v = el.getAttribute(a) || "";
                    const m = v.match(/[\w.+-]+@[\w-]+\.[\w.-]+/);
                    if (m) return m[0];
                  }
                }
                // Inline script / Nuxt state often has the email
                const scripts = document.querySelectorAll("script");
                for (const s of scripts) {
                  const t = s.textContent || "";
                  const m = t.match(/"email":"([\w.+-]+@[\w-]+\.[\w.-]+)"/);
                  if (m) return m[1];
                }
                return "";
              })();
              if (emailFromDom) {
                userInfo = {
                  email: emailFromDom,
                  plan_type: "unknown",
                  user_id: "",
                  display_name: "",
                };
                hitEndpoint = "dom_scrape";
              }
            }

            // Fallback 2: cookie-only detection — if the user has any
            // auth-looking cookie, accept them as logged in with a
            // synthetic email so work can still be dispatched.
            if (!userInfo) {
              const cookieNames = (document.cookie || "")
                .split(";").map(c => c.trim().split("=")[0]);
              const authLike = cookieNames.some(n =>
                /session|token|auth|sso|logged/i.test(n)
              );
              if (authLike) {
                userInfo = {
                  email: "logged_in_user@genspark.ai",  // placeholder
                  plan_type: "unknown",
                  user_id: "",
                  display_name: "Genspark user",
                };
                hitEndpoint = "cookie_only";
              }
            }

            return {
              logged_in: !!userInfo,
              ...(userInfo || {}),
              _probe_endpoint: hitEndpoint,
              _last_status: lastStatus,
            };
          },
        });
        const info = result?.[0]?.result;
        if (info?.logged_in && info.email) {
          fresh[info.email] = {
            email: info.email,
            plan_type: info.plan_type,
            tab_id: tab.id,
            user_id: info.user_id,
            display_name: info.display_name,
          };
          if (info._probe_endpoint && !fresh.__logged_endpoint_once) {
            console.log(
              `[Genspark] Account detected via ${info._probe_endpoint}: ${info.email} (${info.plan_type})`
            );
            fresh.__logged_endpoint_once = true;
          }
        } else if (info) {
          console.log(
            `[Genspark] No account on tab ${tab.id} — ` +
            `probe endpoint: ${info._probe_endpoint || "none matched"}, ` +
            `last status: ${info._last_status}`
          );
        }
      } catch (e) {
        console.warn("[Genspark] detection error on tab", tab.id, e.message);
      }
    }
    // Remove internal marker before publishing
    delete fresh.__logged_endpoint_once;
    gensparkAccounts = fresh;

    // Report to bridge
    if (Object.keys(fresh).length) {
      try {
        await fetch(`${GENSPARK_BRIDGE_URL}/genspark/accounts`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ accounts: Object.values(fresh) }),
        });
      } catch {}
    }
  } catch (e) {
    // Bridge offline or no genspark tabs — silent
  }
}

// ═══════════════════════════════════════════════════════════════════
// Bridge polling
// ═══════════════════════════════════════════════════════════════════

async function gensparkPollBridge() {
  try {
    const emails = Object.keys(gensparkAccounts);
    if (!emails.length) {
      // Silently skip — avoids 400s on the bridge when no accounts yet
      return;
    }
    const accountsParam = `?accounts=${encodeURIComponent(emails.join(","))}`;

    const resp = await fetch(`${GENSPARK_BRIDGE_URL}/genspark/poll${accountsParam}`, {
      method: "GET",
      headers: { "Accept": "application/json" },
    });
    if (!resp.ok) {
      gensparkBridgeConnected = false;
      gensparkLastPollError = `HTTP ${resp.status}`;
      return;
    }
    gensparkBridgeConnected = true;
    gensparkLastPollError = "";
    const data = await resp.json();

    if (data.work && gensparkActiveCount < GENSPARK_MAX_PARALLEL) {
      const rid = data.work.request_id;
      // Dedupe — if we're already processing or already submitted this id,
      // ignore this dispatch silently (the bridge should also skip re-
      // dispatching but belt-and-suspenders helps).
      if (submittedRequestIds.has(rid) || inFlightRequestIds.has(rid)) {
        // Silent skip — no refusal POST (used to cause the valid run to be
        // cancelled mid-flight).
        return;
      }
      inFlightRequestIds.add(rid);
      gensparkActiveCount++;
      // Fire-and-forget — we don't await this, so the poll loop can keep
      // picking up more work while a generation runs in parallel.
      (async () => {
        try {
          await gensparkHandleWork(data.work);
        } catch (e) {
          console.warn("[Genspark] handleWork threw:", e.message);
        } finally {
          gensparkActiveCount--;
          inFlightRequestIds.delete(rid);
        }
      })();
    }
  } catch (e) {
    gensparkBridgeConnected = false;
    gensparkLastPollError = e.message || "fetch failed";
  }
}

// ═══════════════════════════════════════════════════════════════════
// Work execution — image generation end-to-end
// ═══════════════════════════════════════════════════════════════════

async function gensparkHandleWork(work) {
  const {
    request_id, account, prompt, model_params, recaptcha_site_key,
    ref_images_b64,
  } = work;
  const refImages = Array.isArray(ref_images_b64) ? ref_images_b64 : [];
  if (submittedRequestIds.has(request_id)) {
    console.warn(`[Genspark] already submitted, skipping ${request_id}`);
    return;
  }
  await gensparkReportProgress(request_id, "received_work", `account=${account}`);

  const info = gensparkAccounts[account];
  if (!info) {
    await gensparkReportProgress(request_id, "error", "account_tab_not_found");
    await gensparkSubmitResult(request_id, { error: "account_tab_not_found" });
    return;
  }
  const tabId = info.tab_id;

  // Verify tab is still open
  try {
    await chrome.tabs.get(tabId);
  } catch {
    await gensparkReportProgress(request_id, "error", "tab_closed");
    await gensparkSubmitResult(request_id, { error: "tab_closed" });
    return;
  }
  await gensparkReportProgress(request_id, "tab_ready", `tabId=${tabId}`);

  // Step 1: Get a fresh reCAPTCHA Enterprise token (in MAIN world)
  await gensparkReportProgress(request_id, "recaptcha_start", `key=${recaptcha_site_key.slice(0, 12)}…`);
  let recaptchaToken = "";
  try {
    const tokenResult = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: async (siteKey) => {
        // grecaptcha.enterprise is injected by Genspark's page script
        let tries = 0;
        while (tries < 30) {
          if (window.grecaptcha && window.grecaptcha.enterprise &&
              typeof window.grecaptcha.enterprise.execute === "function") {
            break;
          }
          await new Promise((r) => setTimeout(r, 300));
          tries++;
        }
        if (!window.grecaptcha?.enterprise?.execute) {
          return { error: "no_recaptcha_enterprise" };
        }
        try {
          const tok = await window.grecaptcha.enterprise.execute(siteKey, {
            action: "agent_ask",  // observed action in HAR
          });
          return { token: tok };
        } catch (e) {
          return { error: "recaptcha_failed: " + (e?.message || e) };
        }
      },
      args: [recaptcha_site_key],
    });
    const r = tokenResult?.[0]?.result;
    if (r?.error) {
      await gensparkReportProgress(request_id, "error", "recaptcha: " + r.error);
      await gensparkSubmitResult(request_id, { error: r.error });
      return;
    }
    recaptchaToken = r?.token || "";
  } catch (e) {
    await gensparkReportProgress(request_id, "error", "recaptcha_injection_failed: " + e.message);
    await gensparkSubmitResult(request_id, { error: "recaptcha_injection_failed: " + e.message });
    return;
  }
  if (!recaptchaToken) {
    await gensparkReportProgress(request_id, "error", "empty_recaptcha_token");
    await gensparkSubmitResult(request_id, { error: "empty_recaptcha_token" });
    return;
  }
  await gensparkReportProgress(request_id, "recaptcha_ok", `token_len=${recaptchaToken.length}`);

  // Step 2: POST /api/agent/ask_proxy with prompt + token — SSE response.
  // We do this INSIDE the tab so cookies + same-origin rules apply correctly.
  await gensparkReportProgress(
    request_id,
    "ask_proxy_start",
    `prompt="${prompt.slice(0, 40)}" refs=${refImages.length}`
  );
  let taskInfo;
  try {
    const askResult = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: async (reqBody) => {
        try {
          const r = await fetch("/api/agent/ask_proxy", {
            method: "POST",
            credentials: "include",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(reqBody),
          });
          if (!r.ok) {
            const text = await r.text().catch(() => "");
            return { error: `ask_proxy_http_${r.status}`, body: text.slice(0, 500) };
          }

          // Parse SSE stream — aggressive task_id extraction with multiple
          // strategies. Genspark's format uses {type, field_name, field_value}
          // events + tool_call deltas. The task_id can appear in many places.
          const reader = r.body.getReader();
          const decoder = new TextDecoder();
          let buffer = "";
          let taskId = null;
          let projectId = null;
          let msgId = null;
          const allEvents = [];            // every parsed event for debug
          const eventTypeCounts = {};      // summary for debug
          const UUID_RE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi;
          const deadline = Date.now() + 120000;  // 2 min hard cap

          // Tool-call argument accumulator (arguments stream as deltas)
          const toolArgBuffer = {};  // tool_call_id -> { name, args_string }
          // High-value events (message_result, message_field, tool message_start)
          // — captured FULL (no truncation) so we can debug task_id extraction.
          // Deltas are noisy and excluded.
          const richEvents = [];

          function tryExtractTaskId(obj) {
            if (taskId) return;

            // Direct fields
            if (obj.task_id) { taskId = obj.task_id; return; }
            if (Array.isArray(obj.task_ids) && obj.task_ids[0]) {
              taskId = obj.task_ids[0]; return;
            }

            const fv = obj.field_value;
            if (fv && typeof fv === "object") {
              // Shape 1: array of tasks
              if (Array.isArray(fv)) {
                for (const item of fv) {
                  if (item && typeof item === "object" &&
                      item.id && (item.task_type || item.task_source)) {
                    taskId = item.id; return;
                  }
                }
              } else {
                // Shape 2: single task object
                if (fv.id && (fv.task_type || fv.task_source || fv.queue_name)) {
                  taskId = fv.id; return;
                }
                // Shape 3: { task_ids: [...] }
                if (Array.isArray(fv.task_ids) && fv.task_ids[0]) {
                  taskId = fv.task_ids[0]; return;
                }
                // Shape 4: { tasks: {task_id_string: {...}} }  (like ig_tasks_status)
                if (fv.tasks && typeof fv.tasks === "object") {
                  const keys = Object.keys(fv.tasks);
                  if (keys.length) {
                    // Prefer keys that look like UUIDs
                    const uuidKey = keys.find((k) => UUID_RE.test(k));
                    taskId = uuidKey || keys[0];
                    if (taskId) { UUID_RE.lastIndex = 0; return; }
                  }
                }
              }
            }

            // Shape 5: field_name mentions tasks/task_ids and field_value is string
            const fn = (obj.field_name || "").toLowerCase();
            if (!taskId && fv && typeof fv === "string" &&
                (fn.includes("task") || fn.includes("generation"))) {
              const m = fv.match(UUID_RE);
              UUID_RE.lastIndex = 0;
              if (m) {
                const candidates = m.filter((u) => u !== projectId && u !== msgId);
                if (candidates.length) {
                  taskId = candidates[candidates.length - 1];
                  return;
                }
              }
            }

            // Shape 6: tool_result messages for generate_images — content may
            // be a JSON string with a tasks array
            if (obj.type === "message_start" && obj.role === "tool" && obj.content) {
              try {
                const parsed = typeof obj.content === "string"
                  ? JSON.parse(obj.content) : obj.content;
                if (parsed?.tasks && Array.isArray(parsed.tasks) && parsed.tasks[0]?.id) {
                  taskId = parsed.tasks[0].id;
                  return;
                }
                if (Array.isArray(parsed?.task_ids) && parsed.task_ids[0]) {
                  taskId = parsed.task_ids[0]; return;
                }
              } catch {}
            }

            // Shape 7: message_result events. These carry tool execution
            // results — including the image-generation task_id. The shape
            // varies; we look in many candidate locations and also do a
            // recursive UUID scan as a final safety net.
            if (obj.type === "message_result" || obj.type === "message_end") {
              // Common direct fields
              const candidates = [
                obj.result, obj.tool_result, obj.content,
                obj.field_value, obj.value, obj.data,
              ];
              for (const c of candidates) {
                if (taskId) return;
                if (!c) continue;
                let parsed = c;
                if (typeof c === "string") {
                  // Quick UUID scan first (cheap)
                  const m = c.match(UUID_RE);
                  UUID_RE.lastIndex = 0;
                  if (m) {
                    const cand = m.filter(
                      (u) => u !== projectId && u !== msgId
                    );
                    if (cand.length) { taskId = cand[0]; return; }
                  }
                  try { parsed = JSON.parse(c); } catch { continue; }
                }
                if (parsed && typeof parsed === "object") {
                  if (parsed.task_id) { taskId = parsed.task_id; return; }
                  if (Array.isArray(parsed.task_ids) && parsed.task_ids[0]) {
                    taskId = parsed.task_ids[0]; return;
                  }
                  if (Array.isArray(parsed.tasks) && parsed.tasks[0]?.id) {
                    taskId = parsed.tasks[0].id; return;
                  }
                  // Recursive UUID hunt
                  const s = JSON.stringify(parsed);
                  const m = s.match(UUID_RE);
                  UUID_RE.lastIndex = 0;
                  if (m) {
                    const cand = m.filter(
                      (u) => u !== projectId && u !== msgId
                    );
                    if (cand.length) { taskId = cand[0]; return; }
                  }
                }
              }
            }
          }

          function tryExtractFromToolArgs(obj) {
            // Generate_images tool arguments accumulate as deltas; once the
            // full JSON is valid and contains task IDs, grab them.
            if (obj.type === "message_field_delta" &&
                (obj.field_name || "").startsWith("tool_calls[") &&
                (obj.field_name || "").includes("arguments")) {
              const msg = obj.message_id || "unknown";
              const buf = toolArgBuffer[msg] = toolArgBuffer[msg] || { args: "" };
              buf.args += (obj.delta || "");
              // Try parse (may fail until complete)
              try {
                const parsed = JSON.parse(buf.args);
                if (!taskId && parsed) {
                  // Try obvious places
                  if (Array.isArray(parsed.task_ids) && parsed.task_ids[0]) {
                    taskId = parsed.task_ids[0]; return;
                  }
                  if (Array.isArray(parsed.tasks) && parsed.tasks[0]?.id) {
                    taskId = parsed.tasks[0].id; return;
                  }
                }
              } catch {}
            }
            // Also: when a tool_call has a complete function.arguments string
            if (obj.type === "message_field" &&
                (obj.field_name || "").startsWith("tool_calls[")) {
              const fv = obj.field_value;
              if (fv?.function?.arguments && typeof fv.function.arguments === "string") {
                try {
                  const parsed = JSON.parse(fv.function.arguments);
                  if (!taskId) {
                    if (Array.isArray(parsed.task_ids) && parsed.task_ids[0]) {
                      taskId = parsed.task_ids[0];
                    } else if (Array.isArray(parsed.tasks) && parsed.tasks[0]?.id) {
                      taskId = parsed.tasks[0].id;
                    }
                  }
                } catch {}
              }
            }
          }

          while (Date.now() < deadline) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split("\n");
            buffer = lines.pop() || "";
            for (const line of lines) {
              if (!line.startsWith("data:")) continue;
              const payload = line.slice(5).trim();
              if (!payload) continue;
              try {
                const obj = JSON.parse(payload);
                if (!projectId && obj.project_id) projectId = obj.project_id;
                if (!msgId && obj.message_id) msgId = obj.message_id;
                const t = obj.type || "unknown";
                eventTypeCounts[t] = (eventTypeCounts[t] || 0) + 1;
                // Keep a lightweight snapshot for debugging
                if (allEvents.length < 200) {
                  const snap = { type: t };
                  if (obj.field_name) snap.field_name = obj.field_name;
                  if (obj.role) snap.role = obj.role;
                  if (obj.field_value !== undefined) {
                    snap.field_value =
                      typeof obj.field_value === "string"
                        ? obj.field_value.slice(0, 180)
                        : obj.field_value;
                  }
                  if (obj.delta) snap.delta = String(obj.delta).slice(0, 60);
                  allEvents.push(snap);
                }
                // Capture FULL content of high-value events. Deltas are
                // excluded (too noisy); message_result / message_field /
                // tool message_start carry the actual answers.
                const isRich = (
                  t === "message_result" ||
                  t === "message_end" ||
                  t === "message_field" ||
                  (t === "message_start" && obj.role === "tool")
                );
                if (isRich && richEvents.length < 30) {
                  // Deep clone via JSON to avoid leaking references
                  try { richEvents.push(JSON.parse(JSON.stringify(obj))); }
                  catch { richEvents.push({ type: t, _serialize_failed: true }); }
                }
                tryExtractTaskId(obj);
                tryExtractFromToolArgs(obj);
                if (taskId) break;
              } catch (_e) {}
            }
            if (taskId) break;
          }
          try { await reader.cancel(); } catch {}

          if (!taskId) {
            // Fallback A: re-run the rich-event extractor on the full
            // captured set (Shape 7 etc. — in case stream ended before
            // task_id was located in the streaming pass).
            for (const ev of richEvents) {
              if (taskId) break;
              tryExtractTaskId(ev);
            }
          }

          if (!taskId) {
            // Fallback B: any UUID in any captured event that isn't a
            // project/msg id. richEvents first (full content), then
            // allEvents (truncated lightweight snapshots).
            const scan = [...richEvents, ...allEvents];
            for (const ev of scan) {
              if (taskId) break;
              const s = JSON.stringify(ev);
              const m = s.match(UUID_RE);
              UUID_RE.lastIndex = 0;
              if (m) {
                const cand = m.filter(
                  (u) => u !== projectId && u !== msgId && u !== (ev.message_id || "")
                );
                if (cand.length) { taskId = cand[cand.length - 1]; break; }
              }
            }
          }

          if (!taskId) {
            return {
              error: "sse_no_task_id_found",
              project_id: projectId,
              msg_id: msgId,
              debug_event_types: eventTypeCounts,
              debug_last_events: allEvents.slice(-8),
              debug_rich_events: richEvents,
            };
          }
          return { task_id: taskId, project_id: projectId, msg_id: msgId };
        } catch (e) {
          return { error: "ask_proxy_exception: " + (e?.message || e) };
        }
      },
      args: [(() => {
        const userMsgId = _gensparkUuid();
        // Reference-image branch: build OpenAI-style multimodal content
        // array — captured from real genspark.ai POST when the user dropped
        // an image into the chat. Order matters: image parts first, text
        // part last. Otherwise stick with the leaner string-content shape.
        let userContent;
        if (refImages.length > 0) {
          const parts = [];
          for (const ref of refImages) {
            if (ref && ref.data_url) {
              parts.push({
                type: "image_url",
                image_url: { url: ref.data_url },
              });
            }
          }
          parts.push({ type: "text", text: prompt });
          userContent = parts;
        } else {
          userContent = prompt;
        }
        const messages = [{
          role: "user",
          id: userMsgId,
          content: userContent,
        }];
        // Body shape captured from a real genspark.ai POST (Nano Banana Pro).
        // Includes is_private + push_token + session_state which the newer
        // Pro endpoint seems to require. Nano Banana 2 worked without them
        // because the older API accepted a leaner payload.
        return {
          model_params: model_params,
          writingContent: null,
          type: "image_generation_agent",
          project_id: null,
          messages: messages,
          user_s_input: prompt,
          g_recaptcha_token: recaptchaToken,
          is_private: true,
          push_token: "",
          session_state: {
            steps: [],
            messages: messages,
          },
        };
      })()],
    });
    taskInfo = askResult?.[0]?.result;
    if (!taskInfo || taskInfo.error) {
      // Include debug context if we got the structured error
      const debugPayload = (taskInfo && taskInfo.debug_event_types)
        ? { event_types: taskInfo.debug_event_types,
            last_events: taskInfo.debug_last_events,
            rich_events: taskInfo.debug_rich_events,
            project_id: taskInfo.project_id,
            msg_id: taskInfo.msg_id }
        : null;
      if (debugPayload) {
        console.log("[Genspark] SSE debug:", JSON.stringify(debugPayload, null, 2));
      }
      await gensparkReportProgress(
        request_id, "error",
        `ask_proxy: ${taskInfo?.error || "no_result"}`
      );
      await gensparkSubmitResult(request_id, {
        error: taskInfo?.error || "ask_proxy_no_result",
        debug: debugPayload,
      });
      return;
    }
  } catch (e) {
    await gensparkReportProgress(request_id, "error", "ask_proxy_injection_failed: " + e.message);
    await gensparkSubmitResult(request_id, {
      error: "ask_proxy_injection_failed: " + e.message,
    });
    return;
  }

  const { task_id, project_id } = taskInfo;
  await gensparkReportProgress(request_id, "task_created", `task_id=${task_id}`);

  // Step 3: Poll /api/spark/image_generation_task_detail until COMPLETED
  // (using JSON endpoint for simplicity over SSE)
  let finalTask = null;
  try {
    const pollResult = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: async (taskId) => {
        const deadline = Date.now() + 180000;  // 3 min cap for generation
        let lastStatus = "";
        while (Date.now() < deadline) {
          try {
            const r = await fetch(
              `/api/spark/image_generation_task_detail?task_id=${encodeURIComponent(taskId)}`,
              { method: "GET", credentials: "include" }
            );
            if (!r.ok) {
              await new Promise((res) => setTimeout(res, 2500));
              continue;
            }
            const data = await r.json();
            const t = data?.data || {};
            lastStatus = t.status || "";
            if (lastStatus === "COMPLETED" || lastStatus === "SUCCESS") {
              return { ok: true, task: t };
            }
            if (lastStatus === "FAILED" || lastStatus === "ERROR") {
              // Capture the human-readable error text so the Python side
              // can detect daily-limit / different-model messages and
              // auto-fallback to the other model. Genspark surfaces these
              // in task.error_message OR task.message OR a nested field.
              const errMsg = (
                t.error_message ||
                t.message ||
                t.failed_reason ||
                t.error ||
                ""
              );
              return {
                ok: false,
                reason: `task_status_${lastStatus}`,
                error_message: String(errMsg || "").slice(0, 500),
                task: t,
              };
            }
            await new Promise((res) => setTimeout(res, 2500));
          } catch (e) {
            await new Promise((res) => setTimeout(res, 2500));
          }
        }
        return { ok: false, reason: "poll_timeout", last_status: lastStatus };
      },
      args: [task_id],
    });
    const r = pollResult?.[0]?.result;
    if (!r || !r.ok) {
      // Prefer the human-readable error_message (when FAILED) over the
      // generic reason code — that's how the Python side detects
      // "daily limit" / "different model" patterns for auto-fallback.
      const errPayload = r?.error_message
        ? `${r.reason}: ${r.error_message}`
        : (r?.reason || "poll_failed");
      await gensparkReportProgress(request_id, "error", "poll: " + errPayload);
      await gensparkSubmitResult(request_id, {
        error: errPayload,
      });
      return;
    }
    finalTask = r.task;
    await gensparkReportProgress(request_id, "task_complete", `status=${finalTask?.status}`);
  } catch (e) {
    await gensparkReportProgress(request_id, "error", "poll_injection_failed: " + e.message);
    await gensparkSubmitResult(request_id, {
      error: "poll_injection_failed: " + e.message,
    });
    return;
  }

  // Step 4: Fetch the actual image bytes (prefer watermark-free URL)
  const urls = finalTask?.image_urls_nowatermark || finalTask?.image_urls || [];
  const imageUrl = Array.isArray(urls) ? urls[0] : urls;
  if (!imageUrl) {
    await gensparkReportProgress(request_id, "error", "no_image_url_in_completed_task");
    await gensparkSubmitResult(request_id, {
      error: "no_image_url_in_completed_task",
      task_id, project_id,
    });
    return;
  }
  await gensparkReportProgress(request_id, "downloading", imageUrl.slice(0, 80));

  let imageBytesB64 = "";
  try {
    const dlResult = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: async (url) => {
        try {
          const r = await fetch(url, { method: "GET", credentials: "include" });
          if (!r.ok) return { error: `image_fetch_http_${r.status}` };
          const buf = await r.arrayBuffer();
          // Convert to base64 in chunks (large images blow the stack if done
          // naively via apply)
          const bytes = new Uint8Array(buf);
          let bin = "";
          const chunk = 0x8000;
          for (let i = 0; i < bytes.length; i += chunk) {
            bin += String.fromCharCode.apply(
              null, bytes.subarray(i, i + chunk)
            );
          }
          return { b64: btoa(bin) };
        } catch (e) {
          return { error: "image_fetch_exception: " + (e?.message || e) };
        }
      },
      args: [imageUrl],
    });
    const r = dlResult?.[0]?.result;
    if (r?.error) {
      await gensparkSubmitResult(request_id, { error: r.error, image_url: imageUrl });
      return;
    }
    imageBytesB64 = r?.b64 || "";
  } catch (e) {
    await gensparkSubmitResult(request_id, {
      error: "download_injection_failed: " + e.message,
      image_url: imageUrl,
    });
    return;
  }

  // Step 5: Submit result to bridge
  await gensparkReportProgress(request_id, "done", `bytes=${imageBytesB64.length}`);
  await gensparkSubmitResult(request_id, {
    image_url: imageUrl,
    image_bytes_b64: imageBytesB64,
    image_urls_nowatermark: finalTask?.image_urls_nowatermark || [],
    model_used: finalTask?.model || model_params?.model || "",
    task_id: task_id,
    project_id: project_id || finalTask?.project_id || "",
    error: null,
  });
}

// Track which request_ids we've already submitted — prevents re-dispatching
// the same work item (and wasting credits) if the submit POST fails for
// any reason and the bridge keeps handing us the same work on subsequent
// polls. Capped at 200 entries.
const submittedRequestIds = new Set();
const inFlightRequestIds = new Set();

async function gensparkSubmitResult(requestId, body) {
  // Retry up to 3 times — size errors are hard-fail, others transient
  let lastError = "";
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const resp = await fetch(`${GENSPARK_BRIDGE_URL}/genspark/work-result`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request_id: requestId, ...body }),
      });
      if (resp.ok) {
        submittedRequestIds.add(requestId);
        // Prune cache if it grows too large
        if (submittedRequestIds.size > 200) {
          const first = submittedRequestIds.values().next().value;
          submittedRequestIds.delete(first);
        }
        return true;
      }
      lastError = `HTTP ${resp.status}`;
      const txt = await resp.text().catch(() => "");
      console.warn(
        `[Genspark] submit_result attempt ${attempt} rejected (${lastError}): ${txt.slice(0, 200)}`
      );
      // 413 means we need to drop bytes — retry without them
      if (resp.status === 413 && body.image_bytes_b64) {
        console.warn("[Genspark] Dropping base64 bytes and retrying URL-only");
        delete body.image_bytes_b64;
        continue;
      }
    } catch (e) {
      lastError = e.message || "fetch failed";
      console.warn(`[Genspark] submit_result attempt ${attempt} threw:`, lastError);
    }
    await new Promise(r => setTimeout(r, 500 * attempt));
  }
  console.error(`[Genspark] submit_result FAILED after 3 attempts for ${requestId}: ${lastError}`);
  return false;
}

async function gensparkReportProgress(requestId, step, detail) {
  console.log(`[Genspark] ${requestId.slice(0, 20)} → ${step}${detail ? ": " + detail : ""}`);
  try {
    await fetch(`${GENSPARK_BRIDGE_URL}/genspark/progress`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        request_id: requestId,
        step,
        detail: detail || "",
      }),
    });
  } catch {
    // Progress reporting is best-effort; failures don't abort work
  }
}

// ═══════════════════════════════════════════════════════════════════
// Helpers
// ═══════════════════════════════════════════════════════════════════

function _gensparkUuid() {
  // Simple v4-ish UUID — doesn't need to be crypto-strong, it's a message id
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

// ═══════════════════════════════════════════════════════════════════
// Lifecycle — started from background.js
// ═══════════════════════════════════════════════════════════════════

// Intervals are kicked off in background.js so the service worker can
// manage them alongside the Flow bridge's intervals.
function gensparkStart() {
  setInterval(gensparkPollBridge, GENSPARK_POLL_INTERVAL);
  setInterval(gensparkDetectAccounts, GENSPARK_ACCOUNT_DETECT_INTERVAL);
  // Initial account detection after 2s (tabs time to load)
  setTimeout(gensparkDetectAccounts, 2000);
  console.log("[Genspark] Module started — bridge:", GENSPARK_BRIDGE_URL);
}

// Expose to the service worker global scope
self.gensparkStart = gensparkStart;
self.gensparkGetStatus = function () {
  return {
    connected: gensparkBridgeConnected,
    accounts: Object.values(gensparkAccounts).map((a) => ({
      email: a.email,
      plan: a.plan_type,
    })),
    lastError: gensparkLastPollError,
  };
};
