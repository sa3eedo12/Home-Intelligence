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

  // ====================== grouping + search + filtering ====================
  const container = document.getElementById('entity-groups');
  const groupBy = document.getElementById('group-by');
  const search = document.getElementById('search');
  const summaryLine = document.getElementById('summary-line');
  const allCards = container ? Array.from(container.querySelectorAll('.entity-card')) : [];

  function groupKey(card, mode) {
    if (mode === 'area') return card.dataset.area || 'Unassigned';
    if (mode === 'domain') return card.dataset.domain || 'unknown';
    if (mode === 'none') return 'All entities';
    // default: by suggested type, with a friendly bucket label
    const t = card.dataset.suggestedType || 'other';
    if (t.startsWith('appliance.')) return 'Appliances';
    if (t.startsWith('device.')) return 'Devices (TV / monitor / phone / …)';
    if (t.startsWith('vehicle.')) return 'Vehicles';
    if (t.startsWith('person.')) return 'People';
    if (t.startsWith('pet.')) return 'Pets';
    if (t === 'light') return 'Lights';
    if (t === 'sensor') return 'Sensors';
    if (t === 'media_player') return 'Media players (uncategorised)';
    if (t === 'room') return 'Rooms';
    return 'Other';
  }

  function render() {
    if (!container) return;
    const mode = groupBy ? groupBy.value : 'type';
    const q = (search?.value || '').toLowerCase().trim();

    // Reset: detach all cards from their current parent
    allCards.forEach((card) => card.remove());

    // Match + bucket
    const buckets = new Map();
    let visibleCount = 0;
    for (const card of allCards) {
      const haystack = card.dataset.search || '';
      if (q && !haystack.includes(q)) continue;
      visibleCount += 1;
      const key = groupKey(card, mode);
      if (!buckets.has(key)) buckets.set(key, []);
      buckets.get(key).push(card);
    }

    // Clear out any old groups + render new ones
    container.querySelectorAll('.group-section').forEach((s) => s.remove());
    const sortedKeys = Array.from(buckets.keys()).sort();
    for (const key of sortedKeys) {
      const cards = buckets.get(key);
      const section = document.createElement('section');
      section.className = 'group-section';
      const header = document.createElement('header');
      header.className = 'group-header';
      header.innerHTML = `<h3>${key}</h3><span>${cards.length}</span>`;
      section.appendChild(header);
      const grid = document.createElement('div');
      grid.className = 'entity-grid';
      cards.forEach((c) => grid.appendChild(c));
      section.appendChild(grid);
      container.appendChild(section);
    }

    if (summaryLine) {
      const total = allCards.length;
      summaryLine.textContent = q
        ? `Showing ${visibleCount} matching “${q}” of ${total} unidentified entities.`
        : `Showing ${visibleCount} unidentified entities, grouped by ${mode === 'type' ? 'suggested type' : mode}.`;
    }
  }

  if (groupBy) groupBy.addEventListener('change', render);
  if (search) search.addEventListener('input', render);
  render();

  // ====================== adopt / ignore / bulk ============================
  document.addEventListener('click', async (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;

    if (target.id === 'bulk-adopt-area') {
      const cards = container ? Array.from(container.querySelectorAll('.entity-card')) : [];
      if (!cards.length) return;
      if (!window.confirm(`Adopt all ${cards.length} visible entities using their currently-selected type?`)) {
        return;
      }
      target.setAttribute('disabled', 'disabled');
      let ok = 0;
      let failed = 0;
      for (const card of cards) {
        const entityId = card.dataset.entityId || '';
        if (!entityId) continue;
        try {
          await postJSON('/admin/discovery/adopt', {
            entity_id: entityId,
            type: value(card, '.type-select'),
            friendly_name: value(card, '.friendly-name') || entityId,
          });
          ok += 1;
        } catch (err) {
          console.error('bulk adopt failed for', entityId, err);
          failed += 1;
        }
      }
      window.alert(`Adopted ${ok} entity(ies). ${failed ? failed + ' failed.' : ''}`);
      window.location.reload();
      return;
    }

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
