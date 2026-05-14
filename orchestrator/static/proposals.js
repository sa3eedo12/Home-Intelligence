// Proposals dashboard — list with filters + bulk Accept / Dismiss.
// Pulls all proposals up-front from the server-rendered HTML, then
// filters client-side for instant interaction. Action calls hit
// /admin/proposals/* and update each row in place.
(() => {
  const list = document.getElementById('proposal-list');
  if (!list) return;

  const STATE = {
    statusFilter: 'pending',
    kindFilter: '',
  };

  const pillsBox = document.querySelector('.status-pills');
  const initialStatus = pillsBox?.dataset.initialStatus || 'pending';
  STATE.statusFilter = initialStatus;

  const kindFilter = document.getElementById('proposals-kind-filter');
  const selectAll = document.getElementById('select-all-checkbox');
  const bulkBar = document.getElementById('bulk-actions');
  const selectedCount = document.getElementById('selected-count');
  const filteredEmpty = document.getElementById('filtered-empty');

  function rows() {
    return Array.from(list.querySelectorAll('.proposal-row'));
  }

  function isRowVisible(row) {
    if (row.classList.contains('is-removed')) return false;
    if (STATE.statusFilter !== 'all' && row.dataset.status !== STATE.statusFilter) return false;
    if (STATE.kindFilter && row.dataset.kind !== STATE.kindFilter) return false;
    return true;
  }

  function applyFilters() {
    let visibleCount = 0;
    rows().forEach((row) => {
      const visible = isRowVisible(row);
      row.style.display = visible ? '' : 'none';
      if (visible) visibleCount += 1;
      if (!visible) {
        const cb = row.querySelector('.proposal-checkbox');
        if (cb) cb.checked = false;
      }
    });
    filteredEmpty.hidden = visibleCount > 0 || rows().length === 0;
    if (selectAll) selectAll.checked = false;
    refreshBulkBar();
  }

  function refreshPills() {
    document.querySelectorAll('.status-pill').forEach((pill) => {
      pill.classList.toggle('active', pill.dataset.status === STATE.statusFilter);
    });
  }

  function refreshBulkBar() {
    const checked = list.querySelectorAll('.proposal-checkbox:checked');
    selectedCount.textContent = String(checked.length);
    bulkBar.hidden = checked.length === 0;
  }

  document.querySelectorAll('.status-pill').forEach((pill) => {
    pill.addEventListener('click', () => {
      STATE.statusFilter = pill.dataset.status;
      refreshPills();
      applyFilters();
    });
  });

  if (kindFilter) {
    kindFilter.addEventListener('change', () => {
      STATE.kindFilter = kindFilter.value;
      applyFilters();
    });
  }

  list.addEventListener('change', (ev) => {
    if (ev.target && ev.target.classList.contains('proposal-checkbox')) refreshBulkBar();
  });

  if (selectAll) {
    selectAll.addEventListener('change', () => {
      rows().forEach((row) => {
        if (!isRowVisible(row)) return;
        const cb = row.querySelector('.proposal-checkbox');
        if (cb) cb.checked = selectAll.checked;
      });
      refreshBulkBar();
    });
  }

  document.getElementById('select-clear-btn')?.addEventListener('click', () => {
    list.querySelectorAll('.proposal-checkbox:checked').forEach((cb) => { cb.checked = false; });
    if (selectAll) selectAll.checked = false;
    refreshBulkBar();
  });

  function rowFor(id) {
    return list.querySelector(`.proposal-row[data-proposal-id="${id}"]`);
  }

  function markRowResolved(id, newStatus) {
    const row = rowFor(id);
    if (!row) return;
    row.dataset.status = newStatus;
    const badge = row.querySelector('.status-badge');
    if (badge) badge.textContent = newStatus;
    const actions = row.querySelector('.proposal-actions');
    if (actions) actions.innerHTML = '<span class="muted resolved-marker">Resolved</span>';
    const cb = row.querySelector('.proposal-checkbox');
    if (cb) cb.checked = false;
    row.classList.remove('is-resolving');
    if (STATE.statusFilter !== 'all' && newStatus !== STATE.statusFilter) {
      row.style.display = 'none';
    }
  }

  async function actOnSingle(id, action) {
    const row = rowFor(id);
    if (row) row.classList.add('is-resolving');
    try {
      const data = await window.apiPost(`/admin/proposals/${id}/${action}`);
      markRowResolved(id, data.status || (action === 'accept' ? 'accepted' : 'dismissed'));
      window.Toast?.show(`Proposal ${id} ${action}ed.`, 'success');
    } catch (err) {
      if (row) row.classList.remove('is-resolving');
      // Toast already shown by apiPost on error
    }
    refreshBulkBar();
  }

  list.addEventListener('click', (ev) => {
    const target = ev.target;
    if (!(target instanceof HTMLElement)) return;
    const row = target.closest('.proposal-row');
    if (!row) return;
    const id = row.dataset.proposalId;
    if (!id) return;
    if (target.classList.contains('proposal-accept')) actOnSingle(id, 'accept');
    if (target.classList.contains('proposal-dismiss')) actOnSingle(id, 'dismiss');
  });

  async function bulkAct(action, endpoint) {
    const ids = Array.from(list.querySelectorAll('.proposal-checkbox:checked'))
      .map((cb) => cb.closest('.proposal-row'))
      .filter(Boolean)
      .map((row) => parseInt(row.dataset.proposalId, 10))
      .filter(Number.isFinite);
    if (ids.length === 0) return;
    ids.forEach((id) => rowFor(id)?.classList.add('is-resolving'));
    try {
      const data = await window.apiPost(`/admin/proposals/${endpoint}`, { ids });
      const newStatus = action === 'accept' ? 'accepted' : 'dismissed';
      (data.results || []).forEach((res) => {
        if (res.ok) markRowResolved(res.id, newStatus);
        else rowFor(res.id)?.classList.remove('is-resolving');
      });
      const okCount = data.accepted ?? data.dismissed ?? ids.length;
      window.Toast?.show(`${action === 'accept' ? 'Accepted' : 'Dismissed'} ${okCount} proposal(s).`, 'success');
    } catch (err) {
      ids.forEach((id) => rowFor(id)?.classList.remove('is-resolving'));
    }
    refreshBulkBar();
  }

  document.getElementById('bulk-accept-btn')?.addEventListener('click', () => bulkAct('accept', 'bulk-accept'));
  document.getElementById('bulk-dismiss-btn')?.addEventListener('click', () => bulkAct('dismiss', 'bulk-dismiss'));

  // Initial render
  refreshPills();
  applyFilters();
})();
