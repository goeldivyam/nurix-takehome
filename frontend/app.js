/*
 * Shared lifecycle: tab routing, fetch helpers, URL querystring as source
 * of truth for filter state, and the "poll only when visible + latest page
 * + audit active" gate used by the audit view.
 *
 * Design-contract: no frameworks, no toasts, no history.push for filter
 * edits (they use replaceState so URL stays clean). Keyboard shortcuts are
 * surface-wide: `/` focuses audit search, `f` opens the filter popover,
 * `esc` dismisses.
 */

(function () {
  "use strict";

  history.scrollRestoration = "manual";

  const App = (window.App = {
    apiBase: "",
    activeTab: "campaigns",
    campaignsCache: null,
    audit: {
      pollTimer: null,
    },
  });

  /* -------- API helpers -------- */

  function buildQuery(params) {
    if (!params) return "";
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v === undefined || v === null || v === "") continue;
      if (Array.isArray(v)) {
        if (v.length === 0) continue;
        qs.set(k, v.join(","));
      } else {
        qs.set(k, String(v));
      }
    }
    const s = qs.toString();
    return s ? `?${s}` : "";
  }

  async function apiGet(path, params) {
    const res = await fetch(`${App.apiBase}${path}${buildQuery(params)}`, {
      method: "GET",
      credentials: "same-origin",
      headers: { "content-type": "application/json" },
    });
    const text = await res.text();
    let body = null;
    if (text) {
      try {
        body = JSON.parse(text);
      } catch (_) {
        body = text;
      }
    }
    if (!res.ok) {
      const err = new Error(`GET ${path} failed: ${res.status}`);
      err.status = res.status;
      err.body = body;
      throw err;
    }
    return body;
  }

  async function apiPost(path, body) {
    const res = await fetch(`${App.apiBase}${path}`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    const text = await res.text();
    let parsed = null;
    if (text) {
      try {
        parsed = JSON.parse(text);
      } catch (_) {
        parsed = text;
      }
    }
    if (!res.ok) {
      const err = new Error(`POST ${path} failed: ${res.status}`);
      err.status = res.status;
      err.body = parsed;
      throw err;
    }
    return parsed;
  }

  App.api = { get: apiGet, post: apiPost };

  /* -------- URL state -------- */

  App.readParams = function readParams() {
    // Accept both `/ui/?foo=bar#audit` (canonical) and `/ui/#audit?foo=bar`
    // (what operators sometimes type). The audit-view URLs demo scripts
    // emit use the canonical form; this tolerance keeps older links and
    // hand-typed fragments working without rewriting history.
    const search = new URLSearchParams(window.location.search);
    const hash = window.location.hash || "";
    const qIdx = hash.indexOf("?");
    if (qIdx !== -1) {
      const fromHash = new URLSearchParams(hash.slice(qIdx + 1));
      for (const [k, v] of fromHash.entries()) {
        if (!search.has(k)) search.set(k, v);
      }
    }
    const out = {};
    for (const [k, v] of search.entries()) out[k] = v;
    return out;
  };

  App.writeParams = function writeParams(next) {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(next)) {
      if (v === undefined || v === null || v === "") continue;
      if (Array.isArray(v)) {
        if (v.length === 0) continue;
        qs.set(k, v.join(","));
      } else {
        qs.set(k, String(v));
      }
    }
    const search = qs.toString();
    const hash = window.location.hash || "";
    const url = `${window.location.pathname}${search ? "?" + search : ""}${hash}`;
    history.replaceState(null, "", url);
  };

  /* -------- Tab routing -------- */

  function readTabFromHash() {
    // Hash may be `#audit` (canonical), `#campaigns`, or — for the
    // legacy/typed form — `#audit?event_type=...`. Strip any query part
    // before matching.
    const raw = (window.location.hash || "").replace(/^#/, "");
    const tabName = raw.split("?", 1)[0];
    if (tabName === "audit") return "audit";
    return "campaigns";
  }

  function setTab(name) {
    App.activeTab = name;
    const tabs = document.querySelectorAll("[role=tab]");
    tabs.forEach((t) => {
      const selected = t.dataset.tab === name;
      t.setAttribute("aria-selected", selected ? "true" : "false");
    });
    document.getElementById("campaigns-tab").hidden = name !== "campaigns";
    document.getElementById("audit-tab").hidden = name !== "audit";
    const nextHash = `#${name}`;
    if (window.location.hash !== nextHash) {
      // Fold any hash-embedded query (the legacy/typed form
      // `/ui/#audit?event_type=...`) into location.search before rewriting.
      // `readParams` tolerates both URL shapes for reads, but the history
      // rewrite below would otherwise drop the hash-query suffix BEFORE
      // the audit view's readFiltersFromUrl() runs via the tabchange event
      // — silently resetting filters on first load of a hash-query URL.
      const existingHash = window.location.hash || "";
      const qIdx = existingHash.indexOf("?");
      let nextSearch = window.location.search;
      if (qIdx !== -1) {
        const fromHash = new URLSearchParams(existingHash.slice(qIdx + 1));
        const combined = new URLSearchParams(nextSearch.replace(/^\?/, ""));
        for (const [k, v] of fromHash.entries()) {
          if (!combined.has(k)) combined.set(k, v);
        }
        const combinedStr = combined.toString();
        nextSearch = combinedStr ? `?${combinedStr}` : "";
      }
      const url = `${window.location.pathname}${nextSearch}${nextHash}`;
      history.replaceState(null, "", url);
    }
    // Signal views so they can start / stop work.
    window.dispatchEvent(new CustomEvent("tabchange", { detail: { tab: name } }));
  }

  App.setTab = setTab;

  document.addEventListener("click", (e) => {
    const t = e.target.closest("[role=tab]");
    if (!t) return;
    e.preventDefault();
    setTab(t.dataset.tab);
  });

  window.addEventListener("hashchange", () => {
    const t = readTabFromHash();
    // Also run setTab when the new hash carries a query-suffix (the legacy
    // `#audit?foo=bar` form), even if the tab didn't change — the query
    // fold in setTab needs to run or the URL will stay malformed and the
    // audit view's readFiltersFromUrl() will never see the filters. Without
    // this widening, a same-tab mid-session hash push (e.g. operator pastes
    // a share URL into an already-open tab) would silently drop its filters.
    const newHash = window.location.hash || "";
    const hasHashQuery = newHash.indexOf("?") !== -1;
    if (t !== App.activeTab || hasHashQuery) setTab(t);
  });

  /* -------- Keyboard shortcuts (surface-wide) -------- */

  function isTypingTarget(el) {
    if (!el) return false;
    const tag = el.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
    if (el.isContentEditable) return true;
    return false;
  }

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      window.dispatchEvent(new CustomEvent("dismiss-popovers"));
      return;
    }
    if (isTypingTarget(e.target)) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "/") {
      e.preventDefault();
      if (App.activeTab !== "audit") setTab("audit");
      window.dispatchEvent(new CustomEvent("audit-focus-search"));
      return;
    }
    if (e.key.toLowerCase() === "f") {
      if (App.activeTab !== "audit") return;
      e.preventDefault();
      window.dispatchEvent(new CustomEvent("audit-open-filter"));
    }
  });

  /* -------- Visibility + tab change → audit polling gate -------- */

  document.addEventListener("visibilitychange", () => {
    window.dispatchEvent(new CustomEvent("audit-reevaluate-polling"));
  });

  /* -------- Boot -------- */

  document.addEventListener("DOMContentLoaded", () => {
    setTab(readTabFromHash());
  });
})();
