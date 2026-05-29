// Chore card actions — "Mark done" posts to /admin/chores/{id}/complete
// and then reloads so the bucket recomputes from the canonical log.

(function () {
  "use strict";

  async function markDone(templateId, btn) {
    btn.disabled = true;
    var originalText = btn.textContent;
    btn.textContent = "Saving…";
    try {
      var res = await fetch(
        "/admin/chores/" + encodeURIComponent(templateId) + "/complete",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ source: "dashboard" }),
        }
      );
      if (!res.ok) {
        throw new Error("HTTP " + res.status);
      }
      window.location.reload();
    } catch (err) {
      console.warn("chore_complete_failed", err);
      btn.disabled = false;
      btn.textContent = originalText;
      alert("Could not save: " + (err && err.message ? err.message : err));
    }
  }

  document.body.addEventListener("click", function (ev) {
    var target = ev.target;
    if (!target || !target.dataset) return;
    if (target.dataset.choreAction !== "complete") return;
    var templateId = target.dataset.templateId;
    if (!templateId) return;
    markDone(templateId, target);
  });
})();
