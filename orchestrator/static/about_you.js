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
})();
