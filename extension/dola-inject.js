/**
 * dola-inject.js — MAIN-world content script, injected at document_start (BEFORE
 * dola's own JS runs). It patches window.open so that when dola opens its Google
 * OAuth popup (built with the real closure csrfToken), we record the exact URL.
 * The Dola module then reads window.__dolaOAuthUrl and drives it in the main tab
 * (the popup itself is blocked for extension-triggered clicks). The original
 * window.open is still called so manual login keeps working.
 */
(function () {
  try {
    if (window.__dolaOpenHooked) return;
    window.__dolaOpenHooked = true;
    window.__dolaOAuthUrl = "";
    const orig = window.open;
    window.open = function (url) {
      try {
        const u = String(url || "");
        if (u.indexOf("accounts.google.com") !== -1 || u.indexOf("o/oauth2") !== -1 ||
            (u.indexOf("oauth") !== -1 && u.indexOf("google") !== -1)) {
          window.__dolaOAuthUrl = u;
        }
      } catch (e) {}
      try { return orig.apply(this, arguments); } catch (e) { return null; }
    };
  } catch (e) {}
})();
