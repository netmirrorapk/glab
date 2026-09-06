// G-Labs Studio Helper — Popup UI

// Show the actual extension version from the manifest
try {
  const m = chrome.runtime.getManifest();
  const el = document.getElementById("versionLabel");
  if (el && m?.version) el.textContent = "v" + m.version;
} catch {}


function updateStatus(status) {
  const badge = document.getElementById("statusBadge");
  const dot = document.getElementById("statusDot");
  const text = document.getElementById("statusText");

  if (status.connected) {
    badge.className = "status-badge connected";
    dot.className = "dot green";
    text.textContent = "Connected to Studio";
  } else {
    badge.className = "status-badge disconnected";
    dot.className = "dot red";
    text.textContent = status.lastError
      ? `Disconnected (${status.lastError})`
      : "Waiting for Studio...";
  }

  // Token count
  document.getElementById("tokenCount").textContent = status.tokenCount || 0;

  // Accounts list
  const container = document.getElementById("accountsList");
  if (status.accounts && status.accounts.length > 0) {
    container.innerHTML = status.accounts
      .map((acc) => {
        const heldSec = (status.ecosystem?.heldAccounts || {})[acc.email];
        const count = (status.ecosystem?.todayCounts || {})[acc.email] || 0;
        let badge = "";
        let actionBtn = "";
        if (heldSec && heldSec > 0) {
          const hrs = Math.floor(heldSec / 3600);
          const mins = Math.floor((heldSec % 3600) / 60);
          badge = `<div class="name" style="color:#ef5350">🔒 Held (${hrs}h ${mins}m left)</div>`;
          actionBtn = `
            <div style="margin-top:6px;display:flex;gap:4px;">
              <button class="btn btn-secondary force-btn"
                data-email="${acc.email}"
                style="margin:0;padding:4px 8px;font-size:10px;flex:1;">
                ⚠ Force Enable
              </button>
              <button class="btn btn-secondary release-btn"
                data-email="${acc.email}"
                style="margin:0;padding:4px 8px;font-size:10px;flex:1;">
                Release
              </button>
            </div>`;
        } else if (count > 0) {
          badge = `<div class="name" style="color:#66bb6a">🌱 ${count} activities today</div>`;
        }
        return `
      <div class="account-card" style="flex-direction:column;align-items:stretch;">
        <div style="display:flex;align-items:center;gap:8px;">
          <div class="icon">${(acc.email || "?")[0].toUpperCase()}</div>
          <div class="info">
            <div class="email">${acc.email || "Unknown"}</div>
            ${badge || `<div class="name">${acc.name || ""}</div>`}
          </div>
        </div>
        ${actionBtn}
        <button class="btn btn-secondary export-cookies-btn"
          data-email="${acc.email}"
          style="margin-top:6px;padding:6px 8px;font-size:11px;">
          📤 Export Cookies (for proxy / CloakBrowser)
        </button>
      </div>`;
      })
      .join("");

    // Wire action buttons
    document.querySelectorAll(".force-btn").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        const email = e.target.dataset.email;
        if (!confirm(
          `⚠ Warning: Account '${email}' was flagged by reCAPTCHA.\n\n` +
          `Force-enabling it may cause permanent flagging.\n\n` +
          `Continue?`
        )) return;
        fetch("http://127.0.0.1:18924/ecosystem", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ force_enable_account: email, enable: true }),
        }).then(() => setTimeout(refresh, 300));
      });
    });
    document.querySelectorAll(".release-btn").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        const email = e.target.dataset.email;
        chrome.runtime.sendMessage(
          { type: "ecosystemReleaseAccount", account: email },
          () => setTimeout(refresh, 300)
        );
      });
    });

    // Export cookies -> app writes exported_cookies.json for CloakBrowser + proxy
    document.querySelectorAll(".export-cookies-btn").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        const b = e.currentTarget;
        const email = b.dataset.email;
        const orig = b.textContent;
        b.textContent = "Exporting…";
        b.disabled = true;
        chrome.runtime.sendMessage({ type: "exportCookies", email }, (resp) => {
          if (resp && resp.ok) {
            b.textContent = `✅ ${resp.count} cookies saved${resp.registered ? " + account added" : ""}`;
          } else {
            b.textContent = `⚠ ${(resp && resp.error) || "failed (is the app running?)"}`;
          }
          setTimeout(() => { b.textContent = orig; b.disabled = false; }, 3500);
        });
      });
    });
  } else {
    container.innerHTML = '<div class="no-accounts">No accounts detected.<br>Open flow.google.com and log in.</div>';
  }

  // Ecosystem state — toggle reflects LOCAL enabled flag, not just directive.
  // This way the toggle stays consistent with what the user chose, even if
  // the bridge (app) is offline.
  const eco = status.ecosystem || { directive: "disabled", running: false, enabled: false };
  const ecoToggle = document.getElementById("ecoToggle");
  const ecoDot = document.getElementById("ecoDot");
  const ecoText = document.getElementById("ecoStatusText");
  const isOn = !!eco.enabled;
  ecoToggle.classList.toggle("on", isOn);

  if (!eco.bridgeOnline && isOn) {
    ecoDot.className = "eco-dot active";
    ecoText.textContent = eco.running
      ? `Active (standalone) — ${eco.currentSite}`
      : "Active (standalone) — app not needed for warmup";
  } else if (eco.directive === "active") {
    ecoDot.className = "eco-dot active";
    ecoText.textContent = eco.running
      ? `Active — ${eco.currentSite} (${eco.currentAccount?.split("@")[0] || ""})`
      : "Active — idle, next activity coming";
  } else if (eco.directive === "paused") {
    ecoDot.className = "eco-dot paused";
    ecoText.textContent = "Paused — generation running";
  } else {
    ecoDot.className = "eco-dot disabled";
    ecoText.textContent = "Disabled — toggle ON to build trust";
  }

  // Progress bar + ETA (while an activity is running)
  const progWrap = document.getElementById("ecoProgress");
  const progFill = document.getElementById("ecoProgressFill");
  const nextEl = document.getElementById("ecoNext");
  if (eco.running && eco.currentStartedAt && eco.currentDurationMs) {
    const elapsed = Date.now() - eco.currentStartedAt;
    const pct = Math.min(100, Math.round((elapsed / eco.currentDurationMs) * 100));
    const remainSec = Math.max(0, Math.round((eco.currentDurationMs - elapsed) / 1000));
    progWrap.style.display = "block";
    progFill.style.width = pct + "%";
    nextEl.textContent = `⏱ ${remainSec}s remaining on ${eco.currentSite}`;
  } else if (isOn && eco.nextActivityAt && eco.nextActivityAt > Date.now()) {
    const wait = Math.round((eco.nextActivityAt - Date.now()) / 1000);
    const mins = Math.floor(wait / 60), secs = wait % 60;
    progWrap.style.display = "none";
    const reasonMap = {
      user_navigating: " (you're navigating — waiting for you)",
      low_battery: " (low battery — conserving)",
      battery_conserve: " (battery save mode)",
    };
    const reasonText = reasonMap[eco.deferReason] || "";
    nextEl.textContent = `Next activity in ~${mins}m ${secs}s${reasonText}`;
  } else {
    progWrap.style.display = "none";
    nextEl.textContent = "";
  }

  // Live activity feed (last 15 events)
  const feedCard = document.getElementById("feedCard");
  const feedList = document.getElementById("feedList");
  const logs = (eco.log || []);
  if (isOn && logs.length) {
    feedCard.style.display = "block";
    feedList.innerHTML = logs.slice().reverse().map((e) => {
      const t = new Date(e.ts);
      const hh = String(t.getHours()).padStart(2, "0");
      const mm = String(t.getMinutes()).padStart(2, "0");
      const ss = String(t.getSeconds()).padStart(2, "0");
      const timeStr = `${hh}:${mm}:${ss}`;
      const user = (e.account || "").split("@")[0] || "";
      let text = "";
      if (e.kind === "start") {
        text = `🌱 ${user} opened <b>${e.site}</b> (~${e.duration_sec}s)`;
      } else if (e.kind === "end") {
        text = `✅ ${user} finished <b>${e.site}</b> (${e.duration_sec}s)`;
      } else if (e.kind === "abort") {
        text = `⏸ ${user} paused <b>${e.site}</b>`;
      } else if (e.kind === "error") {
        text = `⚠ ${user} error on <b>${e.site}</b>`;
      } else {
        text = `${e.kind}`;
      }
      return `<div class="feed-entry ${e.kind}">
        <span class="time">${timeStr}</span>${text}
      </div>`;
    }).join("");
  } else {
    feedCard.style.display = "none";
  }

  // ─── Genspark section ───
  const gs = status.genspark || { connected: false, accounts: [], lastError: "" };
  const gsSection = document.getElementById("gensparkSection");
  const gsStatusLine = document.getElementById("gensparkStatusLine");
  const gsList = document.getElementById("gensparkAccountsList");
  if (gsSection && gsList) {
    // Always show the section so users know the Genspark feature exists
    gsSection.style.display = "block";
    if (gs.connected) {
      gsStatusLine.textContent = "Bridge: ✅ Connected (port 18925)";
      gsStatusLine.style.color = "#66bb6a";
    } else if (gs.lastError) {
      gsStatusLine.textContent = `Bridge: ⚠ ${gs.lastError} (start the app in Genspark mode)`;
      gsStatusLine.style.color = "#ffa726";
    } else {
      gsStatusLine.textContent = "Bridge: offline (start app in Genspark mode)";
      gsStatusLine.style.color = "#778";
    }

    if (gs.accounts && gs.accounts.length > 0) {
      gsList.innerHTML = gs.accounts.map((a) => {
        const plan = (a.plan || "free").toLowerCase();
        const planColor = plan.includes("pro") ? "#c88aff"
          : plan.includes("plus") ? "#66bb6a"
          : "#888";
        return `
          <div class="account-card">
            <div class="icon" style="background:#2d1a4d;">🍌</div>
            <div class="info">
              <div class="email">${a.email}</div>
              <div class="name" style="color:${planColor}">
                ${plan === "free" ? "⚠ Free plan (limited)" : `✓ ${plan.toUpperCase()} — unlimited`}
              </div>
            </div>
          </div>`;
      }).join("");
    } else {
      gsList.innerHTML = '<div class="no-accounts" style="font-size:11px;">No accounts detected.<br>Open genspark.ai/ai_image and log in.</div>';
    }
  }
}

// Get status from background
function refresh() {
  chrome.runtime.sendMessage({ type: "getStatus" }, (response) => {
    if (response) updateStatus(response);
  });
}

// Open Labs tab
document.getElementById("btnOpenLabs").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "openLabsTab" });
});

// Refresh accounts
document.getElementById("btnRefresh").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "detectAccounts" }, (response) => {
    refresh();
  });
});

// Ecosystem toggle
document.getElementById("ecoToggle").addEventListener("click", () => {
  const toggle = document.getElementById("ecoToggle");
  const newState = !toggle.classList.contains("on");
  chrome.runtime.sendMessage(
    { type: "ecosystemToggle", enabled: newState },
    () => setTimeout(refresh, 500)
  );
});

// Initial load
refresh();

// Auto-refresh every 3s while popup is open
setInterval(refresh, 1000);
