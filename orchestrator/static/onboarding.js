(() => {
  function showError(message) {
    const banner = document.getElementById('onboarding-error-banner');
    if (banner) {
      banner.hidden = false;
      banner.textContent = `Onboarding script error: ${message}. Form actions may not work — check the browser console.`;
    }
    console.error('[onboarding]', message);
  }

  let state = {};
  let summary = {};
  try {
    const dataScript = document.getElementById('onboarding-data');
    state = dataScript ? JSON.parse(dataScript.textContent || '{}') : {};
    summary = state.summary || {};
  } catch (parseErr) {
    showError(`failed to parse onboarding-data JSON: ${parseErr.message}`);
  }
  const savedRoutineKeys = new Set();
  let activeHabit = null;

  function requestJSON(url, options = {}) {
    const method = options.method || 'GET';
    if (method === 'GET') return apiGet(url);
    let body;
    if (typeof options.body === 'string') {
      try { body = JSON.parse(options.body); } catch (_err) { body = options.body; }
    } else body = options.body;
    return apiPost(url, body, { method });
  }

  function csv(value) { return String(value || '').split(',').map((item) => item.trim()).filter(Boolean); }

  function memberPayload(form) {
    const formData = new FormData(form);
    const payload = {
      name: String(formData.get('name') || '').trim(),
      role: String(formData.get('role') || 'adult'),
      telegram_chat_id: String(formData.get('telegram_chat_id') || '').trim() || null,
      allergies: csv(formData.get('allergies')),
      dietary_restrictions: csv(formData.get('dietary_restrictions')),
      sleep_time: String(formData.get('sleep_time') || '').trim() || null,
      wake_time: String(formData.get('wake_time') || '').trim() || null,
    };
    const id = String(formData.get('id') || '').trim();
    if (id) payload.id = Number(id);
    return payload;
  }

  function renderRoutineFields() {
    const container = document.getElementById('routine-fields');
    if (!container) return;
    const missing = summary.missing_profile_keys || [];
    const labels = { wake_time: 'Usual wake time', sleep_time: 'Usual sleep time', work_hours: 'Work hours' };
    const hints = { wake_time: 'Example: 07:00', sleep_time: 'Example: 22:30', work_hours: 'Example: weekdays 09:00-17:00' };
    container.innerHTML = '';
    if (!missing.length) {
      const done = document.createElement('div');
      done.className = 'empty-state compact';
      done.innerHTML = '<div class="empty-state-icon">✅</div><h3>Routine keys complete</h3><p>All routine keys are filled.</p>';
      container.appendChild(done);
      return;
    }
    for (const key of missing) {
      const card = document.createElement('article');
      card.className = 'field-card card-hover';
      card.dataset.key = key;
      const label = document.createElement('label');
      label.className = 'label';
      label.textContent = labels[key] || key;
      const input = document.createElement('input');
      input.className = 'input';
      input.name = key;
      input.type = key.endsWith('_time') ? 'time' : 'text';
      input.placeholder = hints[key] || '';
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'btn btn-primary save-routine-key';
      button.textContent = 'Save';
      label.appendChild(input);
      card.append(label, button);
      container.appendChild(card);
    }
  }

  function finishIfRoutineReady() {
    const missing = summary.missing_profile_keys || [];
    if (savedRoutineKeys.size >= missing.length) window.location.reload();
    else Toast.show('Save each missing routine field first.', 'warning');
  }

  async function submitMemberForm(form) {
    if (!(form instanceof HTMLFormElement)) return;
    const button = form.querySelector('button[type="submit"]');
    try {
      if (button) button.setAttribute('disabled', 'disabled');
      await requestJSON('/admin/household/upsert', { method: 'POST', body: JSON.stringify(memberPayload(form)) });
      Toast.show('Household member saved', 'success');
      setTimeout(() => window.location.reload(), 650);
    } catch (_err) { if (button) button.removeAttribute('disabled'); }
  }

  document.addEventListener('submit', (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || form.id !== 'member-form') return;
    event.preventDefault();
    submitMemberForm(form);
  });

  const memberForm = document.getElementById('member-form');
  if (memberForm instanceof HTMLFormElement) {
    memberForm.addEventListener('submit', (event) => { event.preventDefault(); submitMemberForm(memberForm); });
    const saveBtn = memberForm.querySelector('button[type="submit"]');
    if (saveBtn instanceof HTMLButtonElement) {
      saveBtn.addEventListener('click', (event) => {
        if (typeof memberForm.requestSubmit === 'function') { event.preventDefault(); memberForm.requestSubmit(); }
      });
    }
  }

  document.addEventListener('click', async (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;
    try {
      if (target.id === 'stage1-next') { await requestJSON('/admin/onboarding/stage'); Toast.show('Moved to the next stage', 'success'); setTimeout(() => window.location.reload(), 650); return; }
      if (target.classList.contains('save-routine-key')) {
        const card = target.closest('.field-card');
        const input = card ? card.querySelector('input') : null;
        const key = card instanceof HTMLElement ? card.dataset.key || '' : '';
        const value = input instanceof HTMLInputElement ? input.value.trim() : '';
        if (!key || !value) { Toast.show('Enter a value first.', 'warning'); return; }
        target.setAttribute('disabled', 'disabled');
        await requestJSON('/admin/profile/upsert', { method: 'POST', body: JSON.stringify({ key, value, source: 'onboarding_wizard' }) });
        savedRoutineKeys.add(key);
        target.textContent = 'Saved';
        Toast.show(`${key} saved`, 'success');
        return;
      }
      if (target.id === 'finish-routines') { finishIfRoutineReady(); return; }
      if (target.id === 'clear-member-form') {
        const form = document.getElementById('member-form');
        if (form instanceof HTMLFormElement) form.reset();
        const id = form ? form.querySelector('input[name="id"]') : null;
        if (id instanceof HTMLInputElement) id.value = '';
        Toast.show('Member form cleared', 'info');
        return;
      }
      if (target.classList.contains('edit-member')) {
        const card = target.closest('.member-card');
        const script = card ? card.querySelector('.member-json') : null;
        const form = document.getElementById('member-form');
        if (!(script instanceof HTMLScriptElement) || !(form instanceof HTMLFormElement)) return;
        const member = JSON.parse(script.textContent || '{}');
        for (const field of form.elements) {
          if (!(field instanceof HTMLInputElement || field instanceof HTMLSelectElement)) continue;
          const value = member[field.name];
          if (Array.isArray(value)) field.value = value.join(', ');
          else field.value = value == null ? '' : String(value);
        }
        form.scrollIntoView({ behavior: 'smooth', block: 'center' });
        Toast.show('Member loaded for editing', 'info');
        return;
      }
      if (target.classList.contains('forget-member')) {
        const card = target.closest('.member-card');
        const id = card instanceof HTMLElement ? Number(card.dataset.memberId) : 0;
        if (!id || !window.confirm('Remove this household member?')) return;
        await requestJSON('/admin/household/forget', { method: 'POST', body: JSON.stringify({ id }) });
        Toast.show('Household member removed', 'success');
        setTimeout(() => window.location.reload(), 650);
        return;
      }
      if (target.id === 'finish-household') {
        if ((summary.members || []).length > 0) window.location.reload();
        else Toast.show('Add at least one household member first.', 'warning');
        return;
      }
      const habitCard = target.closest('.habit-card');
      if (target.classList.contains('confirm-habit') && habitCard instanceof HTMLElement) {
        await requestJSON('/admin/knowledge/confirm', { method: 'POST', body: JSON.stringify({ table: 'habits', id: habitCard.dataset.id }) });
        Toast.show('Habit confirmed', 'success');
        setTimeout(() => window.location.reload(), 650);
        return;
      }
      if (target.classList.contains('skip-habit') && habitCard instanceof HTMLElement) {
        if (!window.confirm('Skip this inferred habit?')) return;
        await requestJSON('/admin/knowledge/forget', { method: 'POST', body: JSON.stringify({ table: 'habits', id: habitCard.dataset.id }) });
        Toast.show('Habit skipped', 'success');
        setTimeout(() => window.location.reload(), 650);
        return;
      }
      if (target.classList.contains('edit-habit') && habitCard instanceof HTMLElement) {
        const script = habitCard.querySelector('.habit-json');
        const modal = document.getElementById('habit-edit-modal');
        const textarea = document.getElementById('habit-edit-json');
        if (!(script instanceof HTMLScriptElement) || !(textarea instanceof HTMLTextAreaElement)) return;
        const habit = JSON.parse(script.textContent || '{}');
        activeHabit = { id: habitCard.dataset.id, table: 'habits' };
        textarea.value = JSON.stringify({ subject: habit.subject, pattern: habit.pattern || {}, frequency: habit.frequency || null, confidence: habit.confidence || 0, source: habit.source || 'onboarding_wizard' }, null, 2);
        Modal.open(modal);
        return;
      }
      if (target.id === 'cancel-habit-edit') { Modal.close(document.getElementById('habit-edit-modal')); return; }
      if (target.id === 'save-habit-edit' && activeHabit) {
        const textarea = document.getElementById('habit-edit-json');
        const error = document.getElementById('habit-edit-error');
        if (!(textarea instanceof HTMLTextAreaElement)) return;
        try {
          if (error) error.textContent = '';
          const payload = JSON.parse(textarea.value || '{}');
          await requestJSON(`/admin/knowledge/habits/${encodeURIComponent(activeHabit.id)}`, { method: 'PATCH', body: JSON.stringify(payload) });
          Toast.show('Habit saved', 'success');
          setTimeout(() => window.location.reload(), 650);
        } catch (err) { if (error) error.textContent = err.message || String(err); }
        return;
      }
      if (target.id === 'finish-onboarding') {
        const confirmed = Number(target.dataset.confirmedHabits || '0');
        if (confirmed < 1) { Toast.show('Confirm at least one habit first, or wait for morning briefs to surface one.', 'warning'); return; }
        await requestJSON('/admin/onboarding/complete', { method: 'POST', body: '{}' });
        Toast.show('Onboarding complete', 'success');
        setTimeout(() => window.location.reload(), 650);
      }
    } catch (_err) { target.removeAttribute('disabled'); }
  });

  try { renderRoutineFields(); } catch (renderErr) { showError(`renderRoutineFields failed: ${renderErr.message}`); }
})();
