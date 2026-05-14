(() => {
  const editModal = document.getElementById('edit-modal');
  const editTextarea = document.getElementById('edit-json');
  const editError = document.getElementById('edit-error');
  const evidenceModal = document.getElementById('evidence-modal');
  const evidenceList = document.getElementById('evidence-list');
  let activeEdit = null;

  const editableFields = {
    things: ['type', 'friendly_name', 'attributes', 'ha_entity_ids', 'photo_path', 'confidence', 'source'],
    habits: ['subject', 'pattern', 'frequency', 'confidence', 'last_observed_at', 'source'],
    preferences: ['value', 'confidence', 'source'],
    routines: ['name', 'steps', 'schedule', 'last_run_at', 'source'],
  };

  function reloadSoon() { setTimeout(() => window.location.reload(), 650); }

  function cardPayload(card) {
    const script = card.querySelector('.card-json');
    if (!script) return {};
    try { return JSON.parse(script.textContent || '{}'); } catch (_err) { return {}; }
  }

  function editablePayload(table, payload) {
    const result = {};
    for (const field of editableFields[table] || []) {
      if (Object.prototype.hasOwnProperty.call(payload, field)) result[field] = payload[field];
    }
    return result;
  }

  function renderEvidence(items) {
    if (!evidenceList) return;
    evidenceList.innerHTML = '';
    if (!items.length) {
      const empty = document.createElement('div');
      empty.className = 'empty-state compact';
      empty.innerHTML = '<div class="empty-state-icon">🔎</div><h3>No evidence found</h3><p>No matching event_log evidence found.</p>';
      evidenceList.appendChild(empty);
      return;
    }
    for (const item of items) {
      const article = document.createElement('article');
      article.className = 'evidence-item';
      const title = document.createElement('strong');
      title.textContent = `${item.agent || 'unknown'}.${item.capability || 'event'}`;
      const summary = document.createElement('div');
      summary.textContent = item.summary || '';
      const meta = document.createElement('small');
      meta.textContent = item.ts || '';
      article.append(title, summary, meta);
      evidenceList.appendChild(article);
    }
  }

  document.addEventListener('click', async (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;
    const card = target.closest('.knowledge-card');

    if (target.id === 'cancel-edit') { Modal.close(editModal); return; }
    if (target.id === 'close-evidence') { Modal.close(evidenceModal); return; }
    if (target.id === 'save-edit' && activeEdit) {
      try {
        if (editError) editError.textContent = '';
        const payload = JSON.parse(editTextarea.value || '{}');
        await apiPost(`/admin/knowledge/${activeEdit.table}/${encodeURIComponent(activeEdit.id)}`, payload, { method: 'PATCH' });
        Toast.show('Saved learned fact', 'success');
        reloadSoon();
      } catch (err) {
        if (editError) editError.textContent = err.message;
      }
      return;
    }
    if (!card) return;

    const table = card.dataset.table;
    const id = card.dataset.id;
    if (!table || !id) return;

    if (target.classList.contains('edit-btn')) {
      activeEdit = { table, id };
      const payload = editablePayload(table, cardPayload(card));
      if (editTextarea) editTextarea.value = JSON.stringify(payload, null, 2);
      if (editError) editError.textContent = '';
      Modal.open(editModal);
      return;
    }

    if (target.classList.contains('confirm-btn')) {
      await apiPost('/admin/knowledge/confirm', { table, id }).then(() => {
        Toast.show('Fact confirmed', 'success');
        reloadSoon();
      }).catch(() => {});
      return;
    }

    if (target.classList.contains('forget-btn')) {
      if (!window.confirm('Forget this learned fact? This cannot be undone.')) return;
      await apiPost('/admin/knowledge/forget', { table, id }).then(() => {
        Toast.show('Fact forgotten', 'success');
        reloadSoon();
      }).catch(() => {});
      return;
    }

    if (target.classList.contains('why-btn')) {
      try {
        renderEvidence([]);
        Modal.open(evidenceModal);
        const data = await apiGet(`/admin/knowledge/evidence?table=${encodeURIComponent(table)}&id=${encodeURIComponent(id)}`);
        renderEvidence(data.items || []);
      } catch (err) {
        renderEvidence([{ agent: 'dashboard', capability: 'evidence', summary: err.message }]);
      }
    }
  });

  // ── Tabs ────────────────────────────────────────────────────────────────
  document.querySelectorAll('.ay-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      const target = tab.dataset.tab;
      document.querySelectorAll('.ay-tab').forEach(t => t.classList.toggle('is-active', t === tab));
      document.querySelectorAll('.ay-pane').forEach(p => {
        const match = p.dataset.pane === target;
        p.classList.toggle('is-active', match);
        p.hidden = !match;
      });
    });
  });

  // ── Member switcher (?member=<id>) ──────────────────────────────────────
  const memberSelect = document.getElementById('member-select');
  if (memberSelect) {
    memberSelect.addEventListener('change', () => {
      const url = new URL(window.location.href);
      url.searchParams.set('member', memberSelect.value);
      window.location.href = url.toString();
    });
  }

  // ── Personal device drill-down ──────────────────────────────────────────
  // Reuses the same /admin/devices/{id} contract + dom ids as devices.js:
  //   #dev-detail, #dd-name, #dd-meta, #dd-entity-count, #dd-entities,
  //   #dev-detail-close
  function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }
  function relTime(iso) {
    const t = new Date(iso).getTime();
    if (Number.isNaN(t)) return iso;
    const sec = Math.floor((Date.now() - t) / 1000);
    if (sec < 60) return `${sec}s ago`;
    const min = Math.floor(sec / 60);
    if (min < 60) return `${min}m ago`;
    const hr = Math.floor(min / 60);
    if (hr < 48) return `${hr}h ago`;
    return `${Math.floor(hr / 24)}d ago`;
  }
  function propertyRow(e) {
    const value = (e.value == null || e.value === '') ? '—' : e.value;
    const ts = e.last_changed ? `<span class="prop-when muted">${escapeHtml(relTime(e.last_changed))}</span>` : '';
    const eid = `<span class="prop-eid muted" hidden>${escapeHtml(e.entity_id)}</span>`;
    return `
      <div class="property-row">
        <div class="property-label"><span>${escapeHtml(e.property_label || e.entity_id)}</span>${ts}</div>
        <div class="property-value">${escapeHtml(String(value))}</div>
        ${eid}
      </div>`;
  }
  async function openDeviceDetail(thingId) {
    const panel = document.getElementById('dev-detail');
    if (!panel) return;
    panel.hidden = false;
    document.getElementById('dd-name').textContent = 'Loading…';
    document.getElementById('dd-meta').textContent = '';
    document.getElementById('dd-entities').innerHTML = '';
    document.getElementById('dd-entity-count').textContent = '';
    try {
      const resp = await fetch(`/admin/devices/${thingId}`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const d = await resp.json();
      document.getElementById('dd-name').textContent = d.friendly_name || 'Device';
      const meta = [
        d.area ? `📍 ${d.area}` : null,
        d.manufacturer || null,
        d.model || null,
        d.primary_state ? `Currently: ${d.primary_state}` : null,
      ].filter(Boolean).join(' · ');
      document.getElementById('dd-meta').textContent = meta;
      document.getElementById('dd-entity-count').textContent = `(${d.entity_count})`;
      const ul = document.getElementById('dd-entities');
      const groups = d.grouped_entities || [];
      if (groups.length === 0) {
        ul.innerHTML = `<li class="muted">No properties to show.</li>`;
        return;
      }
      ul.innerHTML = groups.map(g => `
        <li class="category-block">
          <div class="category-header">
            <span class="category-icon">${g.icon}</span>
            <h4>${escapeHtml(g.name)}</h4>
            <span class="muted">${g.entities.length}</span>
          </div>
          <div class="property-grid">${g.entities.map(propertyRow).join('')}</div>
        </li>`).join('') + `
        <li class="tech-toggle-row">
          <button type="button" id="dd-tech-toggle" class="btn btn-ghost btn-sm linklike" data-shown="no">Show technical details</button>
        </li>`;
      document.getElementById('dd-tech-toggle')?.addEventListener('click', evt => {
        const btn = evt.currentTarget;
        const shown = btn.dataset.shown === 'yes';
        document.querySelectorAll('.prop-eid').forEach(el => { el.hidden = shown; });
        btn.dataset.shown = shown ? 'no' : 'yes';
        btn.textContent = shown ? 'Show technical details' : 'Hide technical details';
      });
    } catch (err) {
      document.getElementById('dd-name').textContent = 'Failed to load';
      document.getElementById('dd-meta').textContent = err.message;
    }
  }
  document.querySelectorAll('.device-card').forEach(card => {
    card.addEventListener('click', () => openDeviceDetail(parseInt(card.dataset.thingId, 10)));
    card.addEventListener('keydown', evt => {
      if (evt.key === 'Enter' || evt.key === ' ') {
        evt.preventDefault();
        openDeviceDetail(parseInt(card.dataset.thingId, 10));
      }
    });
  });
  document.getElementById('dev-detail-close')?.addEventListener('click', () => {
    document.getElementById('dev-detail').hidden = true;
  });
})();
