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
    if (errText) wrap.appendChild(el("div", { class: "err" }, errText));
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
    for (const c of state.list.campaigns) {
      const row = el("div", { class: "campaign-row" });
      row.appendChild(el("div", { class: "name" }, c.name));
      const badge = el(
        "span",
        { class: `status-badge status-${c.status}` },
        c.status
      );
      row.appendChild(badge);
      row.appendChild(
        el(
          "div",
          { class: "created" },
          formatTs(c.created_at, { date: true })
        )
      );
      list.appendChild(row);
    }
    wrap.appendChild(list);
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
