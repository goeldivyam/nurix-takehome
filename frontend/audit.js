/*
 * Audit tab — sticky filter bar, dense table, in-memory click-to-pin strip,
 * cursor-based pagination, and a polling gate that only fires when the
 * audit tab is visible, active, and sitting on the latest page (null
 * cursor). Pin state is module-level by design (see IMPLEMENTATION_PLAN.md
 * P4: "no localStorage"): reload wipes it.
 */

(function () {
  "use strict";

  const host = document.getElementById("audit-tab");

  const EVENT_TYPES = [
    { group: "Dispatch", color: "accent", items: ["DISPATCH", "CLAIMED"] },
    { group: "Retry", color: "info", items: ["RETRY_DUE"] },
    {
      group: "Skips & stale",
      color: "warning",
      items: [
        "SKIP_CONCURRENCY",
        "SKIP_BUSINESS_HOUR",
        "WEBHOOK_IGNORED_STALE",
        "DEBUG_AGE_DIALING",
      ],
    },
    {
      group: "Reclaim",
      color: "danger",
      items: ["RECLAIM_EXECUTED", "RECLAIM_SKIPPED_TERMINAL"],
    },
    {
      group: "Lifecycle",
      color: "success",
      items: ["CAMPAIGN_COMPLETED", "CAMPAIGN_PROMOTED_ACTIVE"],
    },
    { group: "State", color: "fg", items: ["TRANSITION"] },
  ];

  const TIME_RANGES = [
    { key: "15m", label: "15m", seconds: 15 * 60 },
    { key: "1h", label: "1h", seconds: 60 * 60 },
    { key: "6h", label: "6h", seconds: 6 * 60 * 60 },
    { key: "24h", label: "24h", seconds: 24 * 60 * 60 },
    { key: "custom", label: "Custom", seconds: null },
  ];

  const state = {
    filters: {
      campaigns: [],
      event_types: [],
      range: "1h",
      from_custom: "",
      to_custom: "",
      reason_contains: "",
      phone: "",
      call_id: "",
      cursor: null,
    },
    events: [],
    nextCursor: null,
    total: null,
    loading: false,
    error: null,
    pins: [],
    popoverOpen: null, // "campaign" | "event" | null
    pollTimer: null,
    fetchToken: 0,
    reasonDebounceTimer: null,
    allCampaigns: [],
    campaignSearch: "",
    expandedReasons: new Set(),
  };

  /* ---------------- URL <-> filter state ---------------- */

  function readFiltersFromUrl() {
    const p = window.App.readParams();
    state.filters.campaigns = split(p.campaign_id);
    state.filters.event_types = split(p.event_type);
    state.filters.range = TIME_RANGES.find((r) => r.key === p.range) ? p.range : "1h";
    state.filters.from_custom = p.from_ts || "";
    state.filters.to_custom = p.to_ts || "";
    state.filters.reason_contains = p.reason || "";
    // Phone URL param is digit-normalized on the backend; store whatever
    // the share URL carried so the input can round-trip the typed shape
    // if it's already digits, or just the sanitized digits otherwise.
    state.filters.phone = p.phone ? String(p.phone) : "";
    state.filters.call_id = p.call_id ? String(p.call_id) : "";
    state.filters.cursor = p.cursor || null;
  }

  function writeFiltersToUrl() {
    window.App.writeParams({
      campaign_id: state.filters.campaigns,
      event_type: state.filters.event_types,
      range: state.filters.range,
      from_ts: state.filters.range === "custom" ? state.filters.from_custom : "",
      to_ts: state.filters.range === "custom" ? state.filters.to_custom : "",
      reason: state.filters.reason_contains,
      phone: state.filters.phone,
      call_id: state.filters.call_id,
      cursor: state.filters.cursor || "",
    });
  }

  function split(v) {
    if (!v) return [];
    return String(v).split(",").filter(Boolean);
  }

  /* ---------------- API query ---------------- */

  function timeRangeWindow() {
    const f = state.filters;
    if (f.range === "custom") {
      return {
        from_ts: f.from_custom ? toIso(f.from_custom) : null,
        to_ts: f.to_custom ? toIso(f.to_custom) : null,
      };
    }
    const r = TIME_RANGES.find((x) => x.key === f.range);
    if (!r || r.seconds == null) return { from_ts: null, to_ts: null };
    const now = new Date();
    const from = new Date(now.getTime() - r.seconds * 1000);
    return { from_ts: from.toISOString(), to_ts: null };
  }

  function toIso(local) {
    if (!local) return null;
    const d = new Date(local);
    if (Number.isNaN(d.getTime())) return null;
    return d.toISOString();
  }

  async function fetchEvents() {
    const token = ++state.fetchToken;
    state.loading = true;
    state.error = null;
    render();
    const { from_ts, to_ts } = timeRangeWindow();
    const params = {
      limit: 100,
      cursor: state.filters.cursor || undefined,
      campaign_id: state.filters.campaigns.length === 1 ? state.filters.campaigns[0] : undefined,
      call_id: state.filters.call_id || undefined,
      event_type: state.filters.event_types.length ? state.filters.event_types : undefined,
      from_ts: from_ts || undefined,
      to_ts: to_ts || undefined,
      reason_contains: state.filters.reason_contains || undefined,
      phone: state.filters.phone || undefined,
    };
    // The /audit endpoint currently accepts a single campaign_id. When the
    // operator selects multiple, we OR client-side after the fetch. A
    // multi-id API filter is a natural follow-up but not in P4 scope.
    try {
      const res = await window.App.api.get("/audit", params);
      if (token !== state.fetchToken) return;
      let events = res.events || [];
      if (state.filters.campaigns.length > 1) {
        const set = new Set(state.filters.campaigns);
        events = events.filter((e) => e.campaign_id && set.has(e.campaign_id));
      }
      state.events = events;
      state.nextCursor = res.next_cursor || null;
      state.total = events.length;
      state.loading = false;
      // Ensure every campaign_id referenced by the fresh events has a known
      // name; if any are missing (new campaign created since we last synced),
      // re-fetch before we render — otherwise chips render as UUID slices.
      const ids = Array.from(
        new Set(events.map((e) => e.campaign_id).filter((x) => x)),
      );
      await ensureCampaignsLoaded(ids);
      render();
    } catch (err) {
      if (token !== state.fetchToken) return;
      state.loading = false;
      state.error = err.message || "Request failed";
      state.events = [];
      state.nextCursor = null;
      render();
    }
  }

  async function ensureCampaignsLoaded(requiredIds) {
    // Previous behavior short-circuited whenever `allCampaigns.length > 0`,
    // so a later POST /campaigns never propagated here and every new
    // campaign's rows rendered as 8-char UUID slices. The contract is now
    // "make sure we have a name for every id we're about to render" —
    // if ANY required id is missing, re-fetch.
    const haveIds = new Set(state.allCampaigns.map((c) => c.id));
    const needFetch =
      state.allCampaigns.length === 0 ||
      (Array.isArray(requiredIds) && requiredIds.some((id) => id && !haveIds.has(id)));
    if (!needFetch) return;
    // Adopt the campaigns-tab cache if it's ahead of ours.
    const fromTab = window.App.campaignsCache;
    if (Array.isArray(fromTab) && fromTab.length >= state.allCampaigns.length) {
      state.allCampaigns = fromTab.slice();
      const updatedIds = new Set(state.allCampaigns.map((c) => c.id));
      if (Array.isArray(requiredIds) && requiredIds.every((id) => !id || updatedIds.has(id))) {
        return;
      }
    }
    try {
      const res = await window.App.api.get("/campaigns", { limit: 200 });
      state.allCampaigns = res.campaigns || [];
      window.App.campaignsCache = state.allCampaigns;
    } catch (_) {
      // Leave allCampaigns as-is; labels fall back to short-id.
    }
  }

  /* ---------------- Polling ---------------- */

  function shouldPoll() {
    return (
      document.visibilityState === "visible" &&
      window.App.activeTab === "audit" &&
      !state.filters.cursor
    );
  }

  function reevaluatePolling() {
    if (shouldPoll()) {
      if (!state.pollTimer) {
        state.pollTimer = setInterval(() => {
          if (shouldPoll()) fetchEvents();
        }, 5000);
      }
    } else if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }

  /* ---------------- Render ---------------- */

  function render() {
    host.innerHTML = "";
    host.appendChild(buildFilterBar());
    host.appendChild(buildLoadingBar());
    if (state.error) host.appendChild(buildErrorBanner());
    if (state.pins.length > 0) host.appendChild(buildPinStrip());
    // Mirror the pager above the table so an operator reviewing a long
    // page doesn't need to scroll 4300px to reach the next-page control.
    // Only render when there's state to page over. The top copy is marked
    // aria-hidden so screen readers read the control pair exactly once —
    // the bottom pager carries the canonical a11y tree.
    if (state.filters.cursor || state.nextCursor) {
      const top = buildPagination();
      top.setAttribute("aria-hidden", "true");
      host.appendChild(top);
    }
    host.appendChild(buildTable());
    host.appendChild(buildPagination());
  }

  function buildFilterBar() {
    const bar = el("div", { class: "filter-bar", role: "toolbar", "aria-label": "Audit filters" });

    // Campaign multi-select popover
    bar.appendChild(buildCampaignPopover());

    // Event-type popover
    bar.appendChild(buildEventPopover());

    // Time range segmented
    bar.appendChild(buildTimeRange());

    // Phone filter (left of reason: identity filters before content filters).
    // Input accepts any shape; backend digit-normalises and requires ≥3
    // digits before firing. Sub-threshold inputs are a silent no-op.
    const phoneInput = el("input", {
      id: "audit-phone",
      type: "search",
      class: "mono",
      placeholder: "phone digits...",
      value: state.filters.phone,
      "aria-label": "Filter by phone (digits, minimum 3)",
    });
    phoneInput.addEventListener("input", () => {
      if (state.phoneDebounceTimer) clearTimeout(state.phoneDebounceTimer);
      state.phoneDebounceTimer = setTimeout(() => {
        state.filters.phone = phoneInput.value;
        state.filters.cursor = null;
        writeFiltersToUrl();
        fetchEvents();
      }, 250);
    });
    bar.appendChild(phoneInput);

    // Reason filter
    const reasonInput = el("input", {
      id: "audit-reason",
      type: "search",
      class: "mono",
      placeholder: "reason contains...",
      value: state.filters.reason_contains,
      "aria-label": "Filter by reason text",
    });
    reasonInput.addEventListener("input", () => {
      if (state.reasonDebounceTimer) clearTimeout(state.reasonDebounceTimer);
      state.reasonDebounceTimer = setTimeout(() => {
        state.filters.reason_contains = reasonInput.value;
        state.filters.cursor = null;
        writeFiltersToUrl();
        fetchEvents();
      }, 250);
    });
    bar.appendChild(reasonInput);

    // Active call_id filter — rendered as a dismissable chip so the
    // operator always knows they're narrowed to one call's lifecycle
    // (entered by clicking a call_id in any row). Dismissing returns to
    // the broader view without touching the other filters.
    if (state.filters.call_id) {
      const callChip = el(
        "button",
        {
          type: "button",
          class: "filter-chip",
          title: `Clear call filter (${state.filters.call_id})`,
        },
      );
      callChip.appendChild(
        el("span", { class: "filter-chip-label" }, "call:"),
      );
      callChip.appendChild(
        el("span", { class: "filter-chip-value mono" }, state.filters.call_id.slice(0, 8) + "…"),
      );
      callChip.appendChild(el("span", { class: "filter-chip-x" }, "×"));
      callChip.addEventListener("click", () => {
        state.filters.call_id = "";
        state.filters.cursor = null;
        writeFiltersToUrl();
        fetchEvents();
      });
      bar.appendChild(callChip);
    }

    bar.appendChild(el("div", { class: "spacer" }));

    // Result count pill
    const pill = el("span", { class: "count-pill", id: "audit-count" });
    pill.textContent =
      state.total == null
        ? "— events"
        : `${state.total.toLocaleString()} event${state.total === 1 ? "" : "s"}`;
    bar.appendChild(pill);

    return bar;
  }

  function buildCampaignPopover() {
    const selected = state.filters.campaigns;
    const wrap = el("div", { class: "popover-host" });
    const btn = el(
      "button",
      {
        type: "button",
        "aria-haspopup": "dialog",
        "aria-expanded": state.popoverOpen === "campaign" ? "true" : "false",
      },
      selected.length === 0
        ? "All campaigns"
        : `Campaigns · ${selected.length}`
    );
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      state.popoverOpen = state.popoverOpen === "campaign" ? null : "campaign";
      ensureCampaignsLoaded().then(() => render());
    });
    wrap.appendChild(btn);

    const pop = el("div", {
      class: "popover",
      role: "dialog",
      "data-open": state.popoverOpen === "campaign" ? "true" : "false",
    });
    pop.addEventListener("click", (e) => e.stopPropagation());

    const search = el("input", {
      class: "popover-search",
      type: "search",
      placeholder: "Search campaigns...",
      value: state.campaignSearch,
    });
    search.addEventListener("input", () => {
      state.campaignSearch = search.value;
      renderCampaignList(list);
    });
    pop.appendChild(search);

    const list = el("div");
    renderCampaignList(list);
    pop.appendChild(list);
    wrap.appendChild(pop);

    return wrap;
  }

  function renderCampaignList(container) {
    container.innerHTML = "";
    const q = state.campaignSearch.trim().toLowerCase();
    const matches = state.allCampaigns.filter((c) =>
      q ? c.name.toLowerCase().includes(q) || c.id.startsWith(q) : true
    );
    if (matches.length === 0) {
      container.appendChild(
        el("div", { class: "filter-summary", style: "padding:8px" }, "No campaigns match")
      );
      return;
    }
    for (const c of matches) {
      const row = el("label");
      const cb = el("input", { type: "checkbox", value: c.id });
      if (state.filters.campaigns.includes(c.id)) cb.checked = true;
      cb.addEventListener("change", () => {
        if (cb.checked) {
          state.filters.campaigns = [...state.filters.campaigns, c.id];
        } else {
          state.filters.campaigns = state.filters.campaigns.filter((x) => x !== c.id);
        }
        state.filters.cursor = null;
        writeFiltersToUrl();
        fetchEvents();
      });
      row.appendChild(cb);
      row.appendChild(el("span", {}, c.name));
      row.appendChild(
        el(
          "span",
          { class: "mono", style: "color:var(--fg-subtle); margin-left:auto; font-size:11px" },
          c.id.slice(0, 8)
        )
      );
      container.appendChild(row);
    }
  }

  function buildEventPopover() {
    const selected = state.filters.event_types;
    const wrap = el("div", { class: "popover-host" });
    const btn = el(
      "button",
      {
        id: "audit-filter-btn",
        type: "button",
        "aria-haspopup": "dialog",
        "aria-expanded": state.popoverOpen === "event" ? "true" : "false",
      },
      selected.length === 0 ? "All events" : `Events · ${selected.length}`
    );
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      state.popoverOpen = state.popoverOpen === "event" ? null : "event";
      render();
    });
    wrap.appendChild(btn);

    const pop = el("div", {
      class: "popover",
      role: "dialog",
      "data-open": state.popoverOpen === "event" ? "true" : "false",
    });
    pop.addEventListener("click", (e) => e.stopPropagation());

    for (const grp of EVENT_TYPES) {
      const section = el("div", { class: "popover-section" });
      section.appendChild(
        el(
          "div",
          {
            class: "micro-caps",
            style: `color: var(--${grp.color === "fg" || grp.color === "fg-muted" ? grp.color : grp.color}); margin-bottom:4px`,
          },
          grp.group
        )
      );
      for (const name of grp.items) {
        const row = el("label");
        const cb = el("input", { type: "checkbox", value: name });
        if (selected.includes(name)) cb.checked = true;
        cb.addEventListener("change", () => {
          if (cb.checked) state.filters.event_types = [...state.filters.event_types, name];
          else
            state.filters.event_types = state.filters.event_types.filter((x) => x !== name);
          state.filters.cursor = null;
          writeFiltersToUrl();
          fetchEvents();
        });
        row.appendChild(cb);
        row.appendChild(el("span", { class: "mono", style: "font-size:11px" }, name));
        section.appendChild(row);
      }
      pop.appendChild(section);
    }
    wrap.appendChild(pop);
    return wrap;
  }

  function buildTimeRange() {
    const seg = el("div", { class: "seg", role: "group", "aria-label": "Time range" });
    for (const r of TIME_RANGES) {
      const b = el(
        "button",
        {
          type: "button",
          "aria-pressed": state.filters.range === r.key ? "true" : "false",
        },
        r.label
      );
      b.addEventListener("click", () => {
        state.filters.range = r.key;
        state.filters.cursor = null;
        writeFiltersToUrl();
        fetchEvents();
      });
      seg.appendChild(b);
    }
    if (state.filters.range === "custom") {
      const from = el("input", {
        type: "datetime-local",
        value: state.filters.from_custom,
        "aria-label": "Custom range start",
      });
      const to = el("input", {
        type: "datetime-local",
        value: state.filters.to_custom,
        "aria-label": "Custom range end",
      });
      from.addEventListener("change", () => {
        state.filters.from_custom = from.value;
        state.filters.cursor = null;
        writeFiltersToUrl();
        fetchEvents();
      });
      to.addEventListener("change", () => {
        state.filters.to_custom = to.value;
        state.filters.cursor = null;
        writeFiltersToUrl();
        fetchEvents();
      });
      const wrap = el("div", { style: "display:inline-flex; gap:6px; align-items:center" });
      wrap.appendChild(seg);
      wrap.appendChild(from);
      wrap.appendChild(el("span", { class: "mono", style: "color:var(--fg-subtle)" }, "→"));
      wrap.appendChild(to);
      return wrap;
    }
    return seg;
  }

  function buildLoadingBar() {
    return el("div", {
      class: "loading-bar",
      "data-active": state.loading ? "true" : "false",
      role: "status",
    });
  }

  function buildErrorBanner() {
    const banner = el("div", { class: "error-banner", role: "alert" });
    banner.appendChild(el("div", {}, "Request failed"));
    banner.appendChild(el("div", { class: "factual" }, state.error));
    const retry = el("button", { type: "button", class: "ghost" }, "Retry");
    retry.addEventListener("click", () => fetchEvents());
    banner.appendChild(retry);
    return banner;
  }

  function buildPinStrip() {
    // Pinned rows share the exact column widths / cell classes of the live
    // audit table, so the pin strip reads as a sibling row stream rather
    // than a floating snippet. Wrapping the rows in a real <table>/<tbody>
    // keeps the <tr> + <td> markup semantically valid (raw <tr> in a <div>
    // collapses cells into inline text in every browser).
    const wrap = el("div", {
      class: "pinned-strip",
      role: "region",
      "aria-label": "Pinned events",
    });
    wrap.appendChild(
      el(
        "div",
        { class: "micro-caps", style: "margin-bottom:6px" },
        `Pinned · ${state.pins.length}/5`,
      ),
    );
    const table = el("table", { class: "table pinned-table" });
    const tbody = el("tbody");
    for (const ev of state.pins) tbody.appendChild(renderRow(ev, { pinned: true }));
    table.appendChild(tbody);
    wrap.appendChild(table);
    return wrap;
  }

  function buildTable() {
    if (state.loading && state.events.length === 0) {
      const wrap = el("div");
      for (let i = 0; i < 8; i++) {
        const row = el("div", { class: "skeleton-row" });
        row.appendChild(el("div", { class: "skeleton-cell", style: "width:96px" }));
        row.appendChild(el("div", { class: "skeleton-cell", style: "width:140px" }));
        row.appendChild(el("div", { class: "skeleton-cell", style: "width:100px" }));
        row.appendChild(el("div", { class: "skeleton-cell", style: "width:140px" }));
        row.appendChild(el("div", { class: "skeleton-cell", style: "flex:1" }));
        wrap.appendChild(row);
      }
      return wrap;
    }
    if (!state.error && state.events.length === 0) {
      const empty = el("div", { class: "empty-state" });
      empty.appendChild(el("div", {}, "No events match the current filters"));
      empty.appendChild(
        el("div", { class: "filter-summary" }, describeFilters())
      );
      const clear = el("button", { type: "button", class: "ghost" }, "Clear filters");
      clear.addEventListener("click", () => {
        state.filters.campaigns = [];
        state.filters.event_types = [];
        state.filters.range = "1h";
        state.filters.from_custom = "";
        state.filters.to_custom = "";
        state.filters.reason_contains = "";
        state.filters.phone = "";
        state.filters.call_id = "";
        state.filters.cursor = null;
        writeFiltersToUrl();
        fetchEvents();
      });
      empty.appendChild(clear);
      return empty;
    }

    const table = el("table", { class: "table", role: "table" });
    const thead = el("thead");
    const hr = el("tr");
    const cols = [
      ["ts", "cell-ts"],
      ["campaign", "cell-campaign"],
      ["call_id", "cell-call"],
      ["event", "cell-event"],
      ["reason", "cell-reason"],
    ];
    for (const [c, cls] of cols) hr.appendChild(el("th", { class: cls }, c));
    thead.appendChild(hr);
    table.appendChild(thead);
    const tbody = el("tbody");
    for (const ev of state.events) tbody.appendChild(renderRow(ev, { pinned: false }));
    table.appendChild(tbody);
    return table;
  }

  function renderRow(ev, { pinned }) {
    const tr = el("tr");
    const tsCell = el("td", { class: "cell-ts cell-mono" }, formatTs(ev.ts));
    const campaignCell = el("td", { class: "cell-campaign" });
    if (ev.campaign_id) {
      const chip = el(
        "span",
        { class: "campaign-chip", title: ev.campaign_id },
        campaignLabel(ev.campaign_id)
      );
      campaignCell.appendChild(chip);
    } else {
      campaignCell.appendChild(el("span", { class: "mono", style: "color:var(--fg-subtle)" }, "—"));
    }
    // Call cell: phone (primary, clickable → filters audit to that phone)
    // stacked above the short call_id (secondary, clickable → filters
    // audit to that call_id's full lifecycle). Both click affordances
    // are URL-backed so share-URLs round-trip. If the attempt_epoch is
    // populated (call-scoped events), a subdued `attempt N` badge sits
    // below the call_id. Campaign-level events render a single em-dash.
    const callCell = el("td", { class: "cell-call cell-mono" });
    if (ev.phone || ev.call_id) {
      const stack = el("div", { class: "call-stack" });
      if (ev.phone) {
        const phoneLink = el(
          "button",
          {
            type: "button",
            class: "call-ident call-ident-primary",
            title: `Filter audit by ${ev.phone}`,
          },
          ev.phone,
        );
        phoneLink.addEventListener("click", () => {
          state.filters.phone = ev.phone;
          state.filters.cursor = null;
          writeFiltersToUrl();
          fetchEvents();
        });
        stack.appendChild(phoneLink);
      }
      if (ev.call_id) {
        const idLink = el(
          "button",
          {
            type: "button",
            class: "call-ident call-ident-secondary",
            title: `Filter audit by call ${ev.call_id}`,
          },
          ev.call_id.slice(0, 8) + "…",
        );
        idLink.addEventListener("click", () => {
          state.filters.call_id = ev.call_id;
          state.filters.cursor = null;
          writeFiltersToUrl();
          fetchEvents();
        });
        stack.appendChild(idLink);
      }
      if (ev.attempt_epoch != null) {
        stack.appendChild(
          el("span", { class: "call-attempt" }, `attempt ${ev.attempt_epoch}`),
        );
      }
      callCell.appendChild(stack);
    } else {
      callCell.appendChild(
        el("span", { class: "mono", style: "color:var(--fg-subtle)" }, "—"),
      );
    }
    const eventCell = el("td", { class: "cell-event" });
    eventCell.appendChild(
      el(
        "span",
        { class: `chip chip-${ev.event_type} chip-default` },
        ev.event_type
      )
    );
    const reasonCell = el("td", { class: "cell-reason" });
    const expanded = state.expandedReasons.has(rowKey(ev));
    const reason = el(
      "div",
      {
        class: "reason",
        "data-expanded": expanded ? "true" : "false",
        tabindex: "0",
        role: "button",
        "aria-expanded": expanded ? "true" : "false",
      },
      ev.reason || ""
    );
    reason.addEventListener("click", () => toggleReason(ev));
    reason.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        toggleReason(ev);
      }
    });
    reasonCell.appendChild(reason);
    // Pin / unpin control rides inside the reason cell as an absolutely
    // positioned overlay so it shares column space with the reason payload
    // rather than consuming a dedicated 6th column (which at table-layout:
    // fixed would claim ~50% of the reason column's width for an element
    // invisible until hover).
    if (pinned) {
      const dismiss = el(
        "button",
        { class: "row-action dismiss", type: "button", "aria-label": "Unpin" },
        "×",
      );
      dismiss.addEventListener("click", () => {
        state.pins = state.pins.filter((p) => p.id !== ev.id);
        render();
      });
      reasonCell.appendChild(dismiss);
    } else {
      const pinBtn = el(
        "button",
        {
          class: "row-action row-pin-btn",
          type: "button",
          "aria-label": "Pin event",
          title: "Pin",
        },
        "Pin",
      );
      pinBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        pinEvent(ev);
      });
      reasonCell.appendChild(pinBtn);
    }

    tr.appendChild(tsCell);
    tr.appendChild(campaignCell);
    tr.appendChild(callCell);
    tr.appendChild(eventCell);
    tr.appendChild(reasonCell);
    return tr;
  }

  function toggleReason(ev) {
    const key = rowKey(ev);
    if (state.expandedReasons.has(key)) state.expandedReasons.delete(key);
    else state.expandedReasons.add(key);
    render();
  }

  function rowKey(ev) {
    return String(ev.id);
  }

  function pinEvent(ev) {
    if (state.pins.some((p) => p.id === ev.id)) return;
    if (state.pins.length >= 5) return;
    state.pins = [...state.pins, ev];
    render();
  }

  function buildPagination() {
    const wrap = el("div", { class: "pagination" });
    if (state.filters.cursor) {
      const back = el("button", { type: "button", class: "ghost" }, "Back to latest");
      back.addEventListener("click", () => {
        state.filters.cursor = null;
        writeFiltersToUrl();
        fetchEvents();
        reevaluatePolling();
      });
      wrap.appendChild(back);
    }
    if (state.nextCursor) {
      const next = el("button", { type: "button" }, "Older →");
      next.addEventListener("click", () => {
        state.filters.cursor = state.nextCursor;
        writeFiltersToUrl();
        fetchEvents();
        reevaluatePolling();
      });
      wrap.appendChild(next);
    }
    return wrap;
  }

  function campaignLabel(id) {
    const cached = state.allCampaigns.find((c) => c.id === id);
    if (cached) return cached.name;
    return id.slice(0, 8);
  }

  function describeFilters() {
    const bits = [];
    if (state.filters.campaigns.length > 0)
      bits.push(`${state.filters.campaigns.length} campaign(s)`);
    if (state.filters.call_id)
      bits.push(`call ${state.filters.call_id.slice(0, 8)}…`);
    if (state.filters.event_types.length > 0)
      bits.push(`events: ${state.filters.event_types.join(",")}`);
    bits.push(`range: ${state.filters.range}`);
    if (state.filters.reason_contains)
      bits.push(`reason~"${state.filters.reason_contains}"`);
    if (state.filters.phone)
      bits.push(`phone~"${state.filters.phone}"`);
    return bits.join(" · ");
  }

  function formatTs(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    const ss = String(d.getSeconds()).padStart(2, "0");
    const ms = String(d.getMilliseconds()).padStart(3, "0");
    return `${hh}:${mm}:${ss}.${ms}`;
  }

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v === null || v === undefined || v === false) continue;
        if (k === "class") node.className = v;
        else node.setAttribute(k, v === true ? "" : v);
      }
    }
    if (children !== undefined) {
      if (Array.isArray(children)) {
        for (const c of children) appendChild(node, c);
      } else {
        appendChild(node, children);
      }
    }
    return node;
  }

  function appendChild(parent, c) {
    if (c === null || c === undefined || c === false) return;
    if (typeof c === "string" || typeof c === "number") {
      parent.appendChild(document.createTextNode(String(c)));
    } else {
      parent.appendChild(c);
    }
  }

  /* ---------------- Wiring ---------------- */

  document.addEventListener("click", (e) => {
    // Dismiss open popover on outside click.
    if (state.popoverOpen && !e.target.closest(".popover-host")) {
      state.popoverOpen = null;
      render();
    }
  });

  window.addEventListener("dismiss-popovers", () => {
    if (state.popoverOpen) {
      state.popoverOpen = null;
      render();
    }
    if (state.expandedReasons.size > 0) {
      state.expandedReasons.clear();
      render();
    }
  });

  window.addEventListener("audit-focus-search", () => {
    const input = document.getElementById("audit-reason");
    if (input) input.focus();
  });

  window.addEventListener("audit-open-filter", () => {
    state.popoverOpen = "event";
    render();
  });

  window.addEventListener("audit-reevaluate-polling", reevaluatePolling);

  window.addEventListener("tabchange", (e) => {
    if (e.detail && e.detail.tab === "audit") {
      readFiltersFromUrl();
      ensureCampaignsLoaded().then(() => {
        fetchEvents();
        reevaluatePolling();
      });
    } else {
      reevaluatePolling();
    }
  });

  document.addEventListener("DOMContentLoaded", () => {
    readFiltersFromUrl();
    if (window.App.activeTab === "audit") {
      ensureCampaignsLoaded().then(() => {
        fetchEvents();
        reevaluatePolling();
      });
    }
  });
})();
