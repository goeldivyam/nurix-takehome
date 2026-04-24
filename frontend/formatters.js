/*
 * Shared display formatters — import-free (attached to window.App.format).
 *
 * The backend's /campaigns responses use the external vocabulary the
 * assignment specifies (pending / in_progress / completed / failed).
 * The audit log's state_before / state_after columns remain INTERNAL
 * vocabulary (PENDING / ACTIVE / COMPLETED / FAILED) because the audit
 * stream is forensic truth — an engineer grepping `scheduler_audit`
 * should see the same enum the state-machine CAS ran against, not a
 * UI-friendly rename that could drift from the DB.
 *
 * The audit tab does not currently render state_before / state_after
 * values in the main table (they are only visible inside the expanded
 * per-row JSON blob, which is intentionally raw forensic data), so this
 * formatter is consumed only by the Campaigns tab today. It stays in a
 * shared module so that if a future audit affordance chooses to surface
 * campaign-level state transitions in a chip, the translation for the
 * campaign-level statuses is already a single well-named function.
 */

(function () {
  "use strict";

  const CAMPAIGN_STATUS_DISPLAY = {
    pending: { external: "pending", label: "Pending" },
    in_progress: { external: "in_progress", label: "In progress" },
    completed: { external: "completed", label: "Completed" },
    failed: { external: "failed", label: "Failed" },
  };

  function formatCampaignStatus(raw) {
    const hit = CAMPAIGN_STATUS_DISPLAY[raw];
    if (hit) {
      return {
        external: hit.external,
        label: hit.label,
        cssClass: `status-${hit.external}`,
      };
    }
    // Fallback for unknown values — render raw so a schema drift is
    // visible rather than silently collapsed into a default chip.
    const safe = String(raw || "unknown");
    return {
      external: safe,
      label: safe,
      cssClass: `status-${safe.toLowerCase()}`,
    };
  }

  const App = (window.App = window.App || {});
  App.format = App.format || {};
  App.format.campaignStatus = formatCampaignStatus;
})();
