/*
 * Campaigns tab — create form + dense list of existing campaigns.
 *
 * Form shape matches CampaignCreate (app/api/schemas/campaigns.py): name,
 * timezone, schedule (7-day grid with zero-or-more [start, end] windows
 * per day), max_concurrent, retry_config (max_attempts + backoff_base_seconds),
 * phones (E.164). 422 responses from the API land as field-level errors;
 * `invalid_phones` highlights matching textarea line numbers.
 */

(function () {
  "use strict";

  const DAYS = [
    ["mon", "Mon"],
    ["tue", "Tue"],
    ["wed", "Wed"],
    ["thu", "Thu"],
    ["fri", "Fri"],
    ["sat", "Sat"],
    ["sun", "Sun"],
  ];

  const host = document.getElementById("campaigns-tab");

  let state = {
    schedule: emptySchedule(),
    errors: {},
    status: null,
    list: { campaigns: [], next_cursor: null, loading: false, error: null },
  };

  function emptySchedule() {
    const out = {};
    for (const [k] of DAYS) out[k] = [];
    return out;
  }

  function timezoneOptions() {
    try {
      return Intl.supportedValuesOf("timeZone");
    } catch (_) {
      // Extremely old browser fallback — still covers both assignment regions.
      return ["UTC", "America/New_York", "America/Los_Angeles", "Asia/Kolkata"];
    }
  }

  function render() {
    host.innerHTML = "";
    host.appendChild(buildForm());
    host.appendChild(buildList());
  }

  /* ------------ Form ------------ */

  function buildForm() {
    const form = el("form", { class: "form-grid", novalidate: "true" });

    // Name
    // Re-renders after a 422 rebuild every input; `state.formValues` carries
    // whatever the operator had typed so the form doesn't lose 50 pasted
    // phones just because 2 were invalid.
    const sv = state.formValues || {};
    const nameInput = el("input", {
      id: "f-name",
      name: "name",
      type: "text",
      required: "true",
      maxlength: "200",
      placeholder: "e.g. NYC retention outreach",
    });
    if (sv.name) nameInput.value = sv.name;
    form.appendChild(
      formField({
        label: "Name",
        id: "f-name",
        wide: true,
        control: nameInput,
        errorKey: "name",
      })
    );

    // Timezone
    const tzSelect = el("select", { id: "f-timezone", name: "timezone" });
    const tzList = timezoneOptions();
    const localTz = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
    const tzPreferred = sv.timezone || localTz;
    for (const tz of tzList) {
      const opt = el("option", { value: tz }, tz);
      if (tz === tzPreferred) opt.selected = true;
      tzSelect.appendChild(opt);
    }
    form.appendChild(
      formField({
        label: "Timezone",
        id: "f-timezone",
        control: tzSelect,
        errorKey: "timezone",
        hint: "Local time for business-hour gating",
      })
    );

    // Max concurrent
    const maxInput = el("input", {
      id: "f-max",
      name: "max_concurrent",
      type: "number",
      min: "1",
      max: "100",
      placeholder: "default from settings",
    });
    if (sv.max) maxInput.value = sv.max;
    form.appendChild(
      formField({
        label: "Max concurrent",
        id: "f-max",
        control: maxInput,
        errorKey: "max_concurrent",
        hint: "Optional · per-campaign cap on in-flight calls",
      })
    );

    // Retry config
    const retryMaxInput = el("input", {
      id: "f-retry-max",
      name: "max_attempts",
      type: "number",
      min: "0",
      max: "10",
      value: sv.retryMax || "3",
      required: "true",
    });
    form.appendChild(
      formField({
        label: "Retry — max attempts",
        id: "f-retry-max",
        control: retryMaxInput,
        errorKey: "retry_config.max_attempts",
      })
    );
    const retryBaseInput = el("input", {
      id: "f-retry-base",
      name: "backoff_base_seconds",
      type: "number",
      min: "1",
      max: "3600",
      value: sv.retryBase || "60",
      required: "true",
    });
    form.appendChild(
      formField({
        label: "Retry — backoff base (seconds)",
        id: "f-retry-base",
        control: retryBaseInput,
        errorKey: "retry_config.backoff_base_seconds",
        hint: "Actual delay = base × 2^attempt ± 20% jitter",
      })
    );

    // Phones
    const phonesTa = el("textarea", {
      id: "f-phones",
      name: "phones",
      class: "phones-textarea",
      placeholder: "+14155551234\n+919876543210",
      required: "true",
    });
    if (sv.phones) phonesTa.value = sv.phones;
    form.appendChild(
      formField({
        label: "Phones",
        id: "f-phones",
        wide: true,
        control: phonesTa,
        errorKey: "phones",
        hint: "E.164 required (e.g. +14155551234 or +919876543210) — one per line",
      })
    );

    // Schedule grid
    const scheduleWrap = el("div", { class: "form-field wide" });
    scheduleWrap.appendChild(el("label", {}, "Weekly schedule"));
    scheduleWrap.appendChild(
      el(
        "div",
        { class: "hint" },
        "Each day: zero or more [start, end] windows. Each window must have start < end."
      )
    );
    const grid = buildScheduleGrid();
    scheduleWrap.appendChild(grid);
    const scheduleErr = el("div", {
      class: "err",
      id: "err-schedule",
      "aria-live": "polite",
    });
    scheduleWrap.appendChild(scheduleErr);
    form.appendChild(scheduleWrap);

    // Footer
    const footer = el("div", { class: "form-footer" });
    // Render status from state so a success message survives the
    // render-loop that loadList() triggers after create. Without this the
    // "Campaign created" line was wiped ~50ms after it landed, leaving
    // the operator with no feedback for a successful write.
    const statusLine = el("div", {
      class: state.status?.kind === "ok" ? "status-line ok" : "status-line",
      id: "form-status",
      "aria-live": "polite",
    });
    if (state.status?.text) statusLine.textContent = state.status.text;
    footer.appendChild(statusLine);

    const resetBtn = el("button", { type: "reset", class: "ghost" }, "Clear");
    const submitBtn = el("button", { type: "submit", class: "primary" }, "Create campaign");
    footer.appendChild(resetBtn);
    footer.appendChild(submitBtn);
    form.appendChild(footer);

    // Submit
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      submit(form);
    });
    // The "Clear" button is the ONLY path that should wipe state.status —
    // a successful-submit flow that needs to preserve `state.status` for
    // its 8-second visibility window imperatively clears inputs instead
    // of firing `form.reset()` (which would bounce through this handler
    // and race-clear the success line). "Clear" still takes the fast
    // user-intent path.
    resetBtn.addEventListener("click", (e) => {
      e.preventDefault();
      for (const input of form.querySelectorAll("input, textarea, select")) {
        if (input instanceof HTMLInputElement || input instanceof HTMLTextAreaElement) {
          input.value = "";
        }
      }
      state.schedule = emptySchedule();
      state.errors = {};
      state.status = null;
      state.formValues = null;
      render();
    });
    form.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        state.errors = {};
        render();
      }
    });

    return form;
  }

  function formField({ label, id, control, wide, errorKey, hint }) {
    const wrap = el("div", { class: "form-field" + (wide ? " wide" : "") });
    wrap.appendChild(el("label", { for: id }, label));
    wrap.appendChild(control);
    if (hint) wrap.appendChild(el("div", { class: "hint" }, hint));
    const errText = state.errors && state.errors[errorKey];
    if (errText) {
      // role="alert" + aria-live="polite" so screen readers announce the
      // validation failure when it lands, and so the audit + form share
      // one error-disclosure vocabulary.
      wrap.appendChild(
        el(
          "div",
          {
            class: "err",
            role: "alert",
            "aria-live": "polite",
          },
          errText,
        ),
      );
    }
    return wrap;
  }

  function buildScheduleGrid() {
    const grid = el("div", { class: "schedule-grid", role: "group" });
    for (const [key, label] of DAYS) {
      const row = el("div", { class: "schedule-day" });
      row.appendChild(el("span", { class: "day-label" }, label));
      const windowsWrap = el("div", { class: "schedule-windows" });
      const day = state.schedule[key] || [];
      day.forEach((win, idx) => windowsWrap.appendChild(buildWindow(key, idx, win)));
      row.appendChild(windowsWrap);

      const addBtn = el("button", { type: "button", class: "ghost" }, "Add window");
      addBtn.addEventListener("click", () => {
        state.schedule[key] = [...day, { start: "09:00", end: "17:00" }];
        render();
      });
      row.appendChild(addBtn);
      grid.appendChild(row);
    }
    return grid;
  }

  function buildWindow(dayKey, idx, win) {
    const invalid = !(win.start && win.end && win.start < win.end);
    const wrap = el("div", {
      class: "schedule-window" + (invalid ? " invalid" : ""),
    });
    const startInput = el("input", {
      type: "time",
      value: win.start || "",
      "aria-label": `${dayKey} window ${idx + 1} start`,
    });
    const endInput = el("input", {
      type: "time",
      value: win.end || "",
      "aria-label": `${dayKey} window ${idx + 1} end`,
    });
    startInput.addEventListener("change", () => {
      state.schedule[dayKey][idx].start = startInput.value;
      render();
    });
    endInput.addEventListener("change", () => {
      state.schedule[dayKey][idx].end = endInput.value;
      render();
    });
    const dismiss = el("button", { type: "button", class: "dismiss", "aria-label": "Remove" }, "x");
    dismiss.addEventListener("click", () => {
      state.schedule[dayKey].splice(idx, 1);
      render();
    });
    wrap.appendChild(startInput);
    wrap.appendChild(el("span", { class: "mono" }, "→"));
    wrap.appendChild(endInput);
    wrap.appendChild(dismiss);
    return wrap;
  }

  /* ------------ Submit + error mapping ------------ */

  async function submit(form) {
    const statusNode = form.querySelector("#form-status");
    state.errors = {};
    state.status = null;

    const name = form.querySelector("#f-name").value.trim();
    const timezone = form.querySelector("#f-timezone").value;
    const maxRaw = form.querySelector("#f-max").value.trim();
    const maxConcurrent = maxRaw ? Number(maxRaw) : null;
    const maxAttempts = Number(form.querySelector("#f-retry-max").value);
    const backoffBase = Number(form.querySelector("#f-retry-base").value);
    const phonesRaw = form.querySelector("#f-phones").value;
    const phones = phonesRaw.split("\n").map((s) => s.trim()).filter(Boolean);

    // Snapshot the current field values BEFORE any render() call. On 422 we
    // re-render to surface inline errors, which rebuilds every input; without
    // this snapshot the operator loses typed state (e.g. 50 pasted phones
    // disappearing when 2 were invalid).
    state.formValues = {
      name,
      timezone,
      max: maxRaw,
      retryMax: String(maxAttempts),
      retryBase: String(backoffBase),
      phones: phonesRaw,
    };

    // Local schedule validation — catch start >= end inline before POST.
    const schedErrIdx = [];
    for (const [k] of DAYS) {
      (state.schedule[k] || []).forEach((w, i) => {
        if (!(w.start && w.end && w.start < w.end)) schedErrIdx.push(`${k}[${i}]`);
      });
    }
    if (schedErrIdx.length > 0) {
      state.errors["schedule"] = `Invalid windows: ${schedErrIdx.join(", ")}`;
      render();
      return;
    }

    const payload = {
      name,
      timezone,
      schedule: state.schedule,
      retry_config: {
        max_attempts: maxAttempts,
        backoff_base_seconds: backoffBase,
      },
      phones,
    };
    if (maxConcurrent !== null && !Number.isNaN(maxConcurrent)) {
      payload.max_concurrent = maxConcurrent;
    }

    statusNode.textContent = "Submitting...";
    statusNode.classList.remove("ok");
    try {
      const created = await window.App.api.post("/campaigns", payload);
      // Success: park status in state BEFORE any DOM clear so a downstream
      // render (e.g. loadList) finds it set. Clear inputs imperatively —
      // form.reset() would bounce through the reset-handler and race-clear
      // state.status ~10ms after this paints.
      state.status = {
        kind: "ok",
        text: `Campaign created (${created.id}) — see audit tab`,
      };
      state.schedule = emptySchedule();
      state.errors = {};
      state.formValues = null;
      render();
      setTimeout(() => {
        if (state.status?.kind === "ok") {
          state.status = null;
          render();
        }
      }, 8000);
      await loadList();
    } catch (err) {
      mapApiError(err);
      state.status = { kind: "err", text: "Create failed — see field errors above." };
      render();
    }
  }

  function mapApiError(err) {
    if (!err || !err.body) {
      state.errors["_root"] = err && err.message ? err.message : "Request failed";
      return;
    }
    const detail = err.body.detail;
    if (err.status === 422 && Array.isArray(detail)) {
      for (const item of detail) {
        const loc = Array.isArray(item.loc) ? item.loc.slice(1) : [];
        const key = loc.join(".");
        const msg = item.msg || "invalid";
        // phones: the validator raises PydanticCustomError with a
        // ctx.invalid_phones list. Pydantic v2 lands that list at
        // `item.ctx.invalid_phones` directly (no extra `.error` nesting),
        // so the render becomes a proper "Line N: reason" list rather than
        // Python repr of the ValueError payload.
        const ctx = item.ctx || {};
        const invalidPhones = Array.isArray(ctx.invalid_phones) ? ctx.invalid_phones : null;
        if (key.startsWith("phones") && invalidPhones) {
          const lines = invalidPhones
            .map((p) => `Line ${Number(p.index) + 1}: ${p.reason} (got: ${p.input})`)
            .join("\n");
          state.errors["phones"] = lines;
          continue;
        }
        // generic fallback
        state.errors[key] = msg;
      }
      return;
    }
    state.errors["_root"] = typeof detail === "string" ? detail : JSON.stringify(detail);
  }

  /* ------------ Campaign list ------------ */

  async function loadList() {
    state.list.loading = true;
    state.list.error = null;
    render();
    try {
      const res = await window.App.api.get("/campaigns", { limit: 50 });
      state.list.campaigns = res.campaigns;
      state.list.next_cursor = res.next_cursor;
    } catch (err) {
      state.list.error = err.message || "Failed to load campaigns";
    } finally {
      state.list.loading = false;
    }
    window.App.campaignsCache = state.list.campaigns;
    render();
  }

  function buildList() {
    const wrap = el("section");
    wrap.appendChild(el("h2", { class: "section-heading" }, "Existing campaigns"));
    if (state.list.loading) {
      wrap.appendChild(skeleton(4));
      return wrap;
    }
    if (state.list.error) {
      const banner = el("div", { class: "error-banner" });
      banner.appendChild(el("div", {}, "Failed to load campaigns"));
      banner.appendChild(el("div", { class: "factual" }, state.list.error));
      wrap.appendChild(banner);
      return wrap;
    }
    if (!state.list.campaigns || state.list.campaigns.length === 0) {
      const empty = el("div", { class: "empty-state" });
      empty.appendChild(el("div", {}, "No campaigns yet"));
      empty.appendChild(
        el("div", { class: "filter-summary" }, "Create one above to see it here.")
      );
      wrap.appendChild(empty);
      return wrap;
    }
    const list = el("div", { class: "campaign-list" });
    const formatStatus = window.App.format.campaignStatus;
    for (const c of state.list.campaigns) {
      // Row is a <button> so it's natively click+keyboard reachable;
      // `all: unset` in CSS strips the default chrome so it still reads
      // as a row. Clicking opens the read-only detail drawer with config
      // + stats + a live list of this campaign's calls.
      const row = el("button", {
        type: "button",
        class: "campaign-row",
        "aria-label": `Open campaign details for ${c.name}`,
      });
      row.appendChild(el("div", { class: "name" }, c.name));
      const { cssClass, label } = formatStatus(c.status);
      const badge = el(
        "span",
        { class: `status-badge ${cssClass}` },
        label
      );
      row.appendChild(badge);
      row.appendChild(
        el(
          "div",
          { class: "created" },
          formatTs(c.created_at, { date: true })
        )
      );
      row.addEventListener("click", () => openCampaignDrawer(c));
      list.appendChild(row);
    }
    wrap.appendChild(list);
    return wrap;
  }

  /* ------------ Campaign detail drawer (read-only) ------------ */

  async function openCampaignDrawer(campaign) {
    // Mount an aside-drawer that overlays the right side; close via X button
    // or click outside. Async-load stats + calls list after paint so the
    // drawer opens immediately with the static setup and fills in the rest.
    closeCampaignDrawer();
    const backdrop = el("div", {
      class: "drawer-backdrop",
      id: "campaign-drawer-backdrop",
    });
    backdrop.addEventListener("click", (e) => {
      if (e.target === backdrop) closeCampaignDrawer();
    });
    // Remember the trigger so focus can return on close (WCAG 2.4.3).
    const returnFocusTo = document.activeElement;
    // Unique id for `aria-labelledby` wiring to the drawer title.
    const titleId = `drawer-title-${campaign.id}`;
    const drawer = el("aside", {
      class: "campaign-drawer",
      role: "dialog",
      "aria-modal": "true",
      "aria-labelledby": titleId,
    });
    const close = el(
      "button",
      {
        type: "button",
        class: "drawer-close",
        "aria-label": "Close details",
      },
      "×",
    );
    close.addEventListener("click", closeCampaignDrawer);
    drawer.appendChild(close);

    drawer.appendChild(renderDrawerHeader(campaign, titleId));
    drawer.appendChild(renderDrawerSetup(campaign));

    const statsSlot = el("section", { class: "drawer-section" });
    statsSlot.appendChild(el("h3", { class: "drawer-heading" }, "Stats"));
    const statsBody = el("div", { class: "drawer-stats-body" }, "Loading…");
    statsSlot.appendChild(statsBody);
    drawer.appendChild(statsSlot);

    const callsSlot = el("section", { class: "drawer-section" });
    callsSlot.appendChild(el("h3", { class: "drawer-heading" }, "Calls"));
    const callsBody = el("div", { class: "drawer-calls-body" }, "Loading…");
    callsSlot.appendChild(callsBody);
    drawer.appendChild(callsSlot);

    backdrop.appendChild(drawer);
    document.body.appendChild(backdrop);
    document.body.classList.add("drawer-open");

    // Move focus into the dialog (WCAG 2.4.11 / ARIA dialog pattern).
    // The close button is the least-disruptive landing target: a keyboard
    // user can Tab forward into the drawer body or Escape out immediately.
    close.focus();

    // Escape to close + Tab focus-trap so a keyboard user can't silently
    // fall back to the page behind the backdrop. Scoped listener; the
    // closer removes it so repeated open/close doesn't leak handlers.
    const focusableSelector =
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
    const keyHandler = (e) => {
      if (e.key === "Escape") {
        closeCampaignDrawer();
        return;
      }
      if (e.key !== "Tab") return;
      const focusable = drawer.querySelectorAll(focusableSelector);
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", keyHandler);
    backdrop._keyHandler = keyHandler;
    backdrop._returnFocusTo = returnFocusTo;

    // Fire the two fetches in parallel so total drawer-open latency is one
    // round-trip, not two.
    Promise.all([
      window.App.api.get(`/campaigns/${campaign.id}/stats`).catch(() => null),
      window.App.api.get(`/campaigns/${campaign.id}/calls`, { limit: 50 }).catch(() => null),
    ]).then(([stats, calls]) => {
      statsBody.textContent = "";
      if (stats) statsBody.appendChild(renderDrawerStats(stats));
      else statsBody.textContent = "Failed to load stats.";
      callsBody.textContent = "";
      if (calls) callsBody.appendChild(renderDrawerCalls(calls, campaign));
      else callsBody.textContent = "Failed to load calls.";
    });
  }

  function closeCampaignDrawer() {
    const existing = document.getElementById("campaign-drawer-backdrop");
    if (!existing) return;
    if (existing._keyHandler) window.removeEventListener("keydown", existing._keyHandler);
    const returnTo = existing._returnFocusTo;
    existing.remove();
    document.body.classList.remove("drawer-open");
    // Return focus to the element that opened the drawer so keyboard
    // users don't get dumped at the top of the page.
    if (returnTo && typeof returnTo.focus === "function") {
      returnTo.focus();
    }
  }

  function renderDrawerHeader(c, titleId) {
    const header = el("header", { class: "drawer-header" });
    header.appendChild(el("h2", { class: "drawer-title", id: titleId }, c.name));
    const formatStatus = window.App.format.campaignStatus;
    const { cssClass, label } = formatStatus(c.status);
    header.appendChild(
      el("span", { class: `status-badge ${cssClass}` }, label),
    );
    return header;
  }

  function renderDrawerSetup(c) {
    const section = el("section", { class: "drawer-section" });
    section.appendChild(el("h3", { class: "drawer-heading" }, "Setup"));
    const dl = el("dl", { class: "drawer-dl" });
    const addRow = (label, value) => {
      dl.appendChild(el("dt", {}, label));
      dl.appendChild(el("dd", { class: "mono" }, value));
    };
    addRow("Timezone", c.timezone);
    addRow("Max concurrent", String(c.max_concurrent));
    addRow(
      "Retry policy",
      `${c.retry_config.max_attempts} attempt(s), base ${c.retry_config.backoff_base_seconds}s, ±20% jitter`,
    );
    addRow("Created", formatTs(c.created_at, { date: true }));
    addRow("Updated", formatTs(c.updated_at, { date: true }));
    section.appendChild(dl);

    // Weekly schedule: one line per day with its configured windows, or
    // an em-dash if that day is closed. Read-only rendering; no edits.
    const schedHeading = el("h4", { class: "drawer-subheading" }, "Weekly schedule");
    section.appendChild(schedHeading);
    const schedList = el("ul", { class: "drawer-schedule" });
    const days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
    for (const day of days) {
      const windows = c.schedule[day] || [];
      const line = el("li", {});
      line.appendChild(el("span", { class: "drawer-day" }, day.toUpperCase()));
      if (windows.length === 0) {
        line.appendChild(el("span", { class: "drawer-windows dim" }, "—"));
      } else {
        const text = windows.map((w) => `${w.start}–${w.end}`).join(", ");
        line.appendChild(el("span", { class: "drawer-windows mono" }, text));
      }
      schedList.appendChild(line);
    }
    section.appendChild(schedList);
    return section;
  }

  function renderDrawerStats(stats) {
    const grid = el("div", { class: "drawer-stats-grid" });
    const cell = (label, value) => {
      const c = el("div", { class: "drawer-stat" });
      c.appendChild(el("div", { class: "drawer-stat-value mono" }, String(value)));
      c.appendChild(el("div", { class: "drawer-stat-label" }, label));
      return c;
    };
    grid.appendChild(cell("Total", stats.total));
    grid.appendChild(cell("Completed", stats.completed));
    grid.appendChild(cell("Failed", stats.failed));
    grid.appendChild(cell("In progress", stats.in_progress));
    grid.appendChild(cell("Retries", stats.retries_attempted));
    return grid;
  }

  function renderDrawerCalls(payload, campaign) {
    const calls = payload.calls || [];
    const wrap = el("div");
    if (calls.length === 0) {
      wrap.appendChild(
        el("div", { class: "drawer-empty" }, "No calls in this campaign yet."),
      );
      return wrap;
    }
    const table = el("table", { class: "drawer-table" });
    const head = el("tr");
    ["phone", "status", "attempt", "updated", ""].forEach((h) =>
      head.appendChild(el("th", {}, h)),
    );
    const thead = el("thead");
    thead.appendChild(head);
    table.appendChild(thead);
    const tbody = el("tbody");
    for (const call of calls) {
      const tr = el("tr");
      tr.appendChild(el("td", { class: "mono" }, call.phone));
      tr.appendChild(
        el("td", { class: `call-status-cell call-status-${call.status}` }, call.status),
      );
      tr.appendChild(
        el("td", { class: "mono" }, String(call.attempt_epoch)),
      );
      tr.appendChild(
        el("td", { class: "mono dim" }, formatTs(call.updated_at, { date: true })),
      );
      // Deep-link: clicking "View" switches to the Audit tab with a
      // filter pre-applied to this call_id so the operator lands on
      // the call's full lifecycle in one click.
      const view = el(
        "button",
        {
          type: "button",
          class: "ghost drawer-call-view",
          title: `View this call's lifecycle in the audit tab`,
        },
        "View →",
      );
      view.addEventListener("click", () => {
        const params = new URLSearchParams();
        params.set("call_id", call.id);
        params.set("campaign_id", campaign.id);
        params.set("range", "24h");
        const nextUrl = `${window.location.pathname}?${params.toString()}#audit`;
        history.pushState(null, "", nextUrl);
        window.dispatchEvent(new HashChangeEvent("hashchange"));
        closeCampaignDrawer();
      });
      const actions = el("td", {});
      actions.appendChild(view);
      tr.appendChild(actions);
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    wrap.appendChild(table);
    if (payload.next_cursor) {
      wrap.appendChild(
        el(
          "div",
          { class: "drawer-pager-hint dim" },
          "Showing first 50 — use the Audit tab for older events.",
        ),
      );
    }
    return wrap;
  }

  function skeleton(n) {
    const wrap = el("div");
    for (let i = 0; i < n; i++) {
      const row = el("div", { class: "skeleton-row" });
      row.appendChild(el("div", { class: "skeleton-cell", style: "flex:2" }));
      row.appendChild(el("div", { class: "skeleton-cell", style: "flex:1" }));
      row.appendChild(el("div", { class: "skeleton-cell", style: "flex:1.5" }));
      wrap.appendChild(row);
    }
    return wrap;
  }

  /* ------------ Utilities ------------ */

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

  function formatTs(iso, opts) {
    if (!iso) return "";
    try {
      const d = new Date(iso);
      if (opts && opts.date) {
        return d.toISOString().slice(0, 16).replace("T", " ") + "Z";
      }
      return d.toISOString();
    } catch (_) {
      return String(iso);
    }
  }

  /* ------------ Boot ------------ */

  window.addEventListener("tabchange", (e) => {
    if (e.detail && e.detail.tab === "campaigns") {
      render();
      loadList();
    }
  });

  document.addEventListener("DOMContentLoaded", () => {
    render();
    loadList();
  });
})();
