// Routine card actions — confirm / dismiss / override.
// Each click POSTs /admin/routines/{id}/{action} then re-renders by
// reloading. Optimistic UI would be nicer but we want the server's
// canonical lifecycle state visible after every change.

(function () {
  "use strict";

  function activateTab(status) {
    document.querySelectorAll(".status-pill").forEach(function (btn) {
      btn.classList.toggle("is-active", btn.dataset.status === status);
    });
    document.querySelectorAll(".routines-group").forEach(function (sec) {
      sec.hidden = sec.dataset.status !== status;
    });
  }

  document.querySelectorAll(".status-pill").forEach(function (btn) {
    btn.addEventListener("click", function () {
      activateTab(btn.dataset.status);
    });
  });

  async function postAction(routineId, action, btn) {
    btn.disabled = true;
    var originalText = btn.textContent;
    btn.textContent = "...";
    try {
      var res = await fetch(
        "/admin/routines/" + encodeURIComponent(routineId) + "/" +
          encodeURIComponent(action),
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ source: "dashboard" }),
        }
      );
      if (!res.ok) {
        throw new Error("HTTP " + res.status);
      }
      // Full reload so all three buckets reflect the latest state.
      window.location.reload();
    } catch (err) {
      console.warn("routine_action_failed", err);
      btn.disabled = false;
      btn.textContent = originalText;
      alert("Action failed: " + (err && err.message ? err.message : err));
    }
  }

  document.body.addEventListener("click", function (ev) {
    var target = ev.target;
    if (!target || !target.classList) return;
    var card = target.closest(".routine-card");
    if (!card) return;
    var routineId = card.dataset.routineId;
    if (!routineId) return;
    if (target.classList.contains("routine-confirm")) {
      postAction(routineId, "confirm", target);
    } else if (target.classList.contains("routine-dismiss")) {
      postAction(routineId, "dismiss", target);
    } else if (target.classList.contains("routine-override")) {
      postAction(routineId, "override", target);
    }
  });
})();
