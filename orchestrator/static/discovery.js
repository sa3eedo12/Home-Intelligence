(() => {
  async function postJSON(url, payload) {
    const resp = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!resp.ok) {
      const text = await resp.text().catch(() => resp.statusText);
      throw new Error(`${resp.status} ${text}`);
    }
    return resp.json();
  }

  function value(card, selector) {
    const field = card.querySelector(selector);
    return field instanceof HTMLInputElement || field instanceof HTMLSelectElement
      ? field.value.trim()
      : '';
  }

  document.addEventListener('click', async (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;
    const card = target.closest('.entity-card');
    if (!(card instanceof HTMLElement)) return;
    const entityId = card.dataset.entityId || '';
    if (!entityId) return;

    try {
      if (target.classList.contains('adopt-btn')) {
        target.setAttribute('disabled', 'disabled');
        const payload = {
          entity_id: entityId,
          type: value(card, '.type-select'),
          friendly_name: value(card, '.friendly-name') || entityId,
        };
        const photoPath = value(card, '.photo-path');
        if (photoPath) payload.photo_path = photoPath;
        await postJSON('/admin/discovery/adopt', payload);
        window.location.reload();
      }
      if (target.classList.contains('ignore-btn')) {
        target.setAttribute('disabled', 'disabled');
        await postJSON('/admin/discovery/ignore', { entity_id: entityId });
        window.location.reload();
      }
    } catch (err) {
      target.removeAttribute('disabled');
      console.error(err);
      window.alert(err.message || String(err));
    }
  });
})();
