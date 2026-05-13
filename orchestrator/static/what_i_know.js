// What I Know About You — light interactivity (auto-refresh, theme respect)
(function () {
  const REFRESH_MS = 60_000;

  function refreshIfStale() {
    // Only auto-reload if the user hasn't interacted in the last 30s
    const lastInteract = window.__wikLastInteract || 0;
    if (Date.now() - lastInteract > 30_000) {
      window.location.reload();
    }
  }

  ["click", "keydown", "scroll", "mousemove"].forEach((ev) => {
    document.addEventListener(ev, () => {
      window.__wikLastInteract = Date.now();
    }, { passive: true });
  });

  setInterval(refreshIfStale, REFRESH_MS);
})();
