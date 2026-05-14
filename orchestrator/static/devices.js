// devices.js — Personal/Home tabs with area grouping + drill-down.

(function () {
  'use strict';

  const TYPE_ICONS = {
    'device.tv': '📺',
    'device.monitor': '🖥️',
    'device.climate': '🌡️',
    'device.light': '💡',
    'device.cover': '🪟',
    'device.lock': '🔒',
    'device.camera': '📷',
    'device.vacuum': '🤖',
    'device.doorbell': '🚪',
    'device.phone': '📱',
    'device.tablet': '📱',
    'device.laptop': '💻',
    'device.watch': '⌚',
    'appliance.washer': '🧺',
    'appliance.dryer': '👕',
    'appliance.dishwasher': '🍽️',
    'appliance.vacuum': '🤖',
    'sensor.motion': '🚶',
  };

  let allDevices = { home: [], personal: [] };
  let activeScope = 'home';
  let members = [];

  async function fetchDevices() {
    const resp = await fetch('/admin/devices');
    if (!resp.ok) throw new Error(`devices fetch failed: ${resp.status}`);
    const data = await resp.json();
    allDevices.home = data.home || [];
    allDevices.personal = data.personal || [];
    document.querySelector('[data-count="home"]').textContent = allDevices.home.length;
    document.querySelector('[data-count="personal"]').textContent = allDevices.personal.length;
  }

  async function fetchMembers() {
    // No dedicated /admin/members endpoint — devices endpoint already
    // resolved owner_name for us. For the dropdown we do need the list
    // though, so query the household graph via the about-you data path.
    try {
      const resp = await fetch('/admin/household/list');
      if (resp.ok) {
        const data = await resp.json();
        members = data.members || data || [];
        return;
      }
    } catch (_) { /* falls through */ }
    // Fallback: synthesize from owner names we already saw on devices.
    const seen = new Map();
    for (const d of [...allDevices.home, ...allDevices.personal]) {
      if (d.owner_member_id && d.owner_name && !seen.has(d.owner_member_id)) {
        seen.set(d.owner_member_id, d.owner_name);
      }
    }
    members = Array.from(seen, ([id, name]) => ({ id, name }));
  }

  function render() {
    const container = document.getElementById('dev-list');
    const filterText = document.getElementById('dev-filter').value.trim().toLowerCase();
    const list = (allDevices[activeScope] || []).filter(d => {
      if (!filterText) return true;
      const haystack = [
        d.friendly_name, d.area, d.manufacturer, d.model,
        d.primary_entity_id, d.type, d.owner_name,
      ].filter(Boolean).join(' ').toLowerCase();
      return haystack.includes(filterText);
    });

    if (list.length === 0) {
      container.innerHTML = `<p class="muted dev-empty">No ${activeScope} devices${filterText ? ' match the filter' : ' yet'}.</p>`;
      return;
    }

    // Group by area (or by owner for personal scope).
    const groupKey = activeScope === 'personal' ? 'owner_name' : 'area';
    const byGroup = new Map();
    for (const d of list) {
      const key = d[groupKey] || (activeScope === 'personal' ? 'Unassigned' : 'No area');
      if (!byGroup.has(key)) byGroup.set(key, []);
      byGroup.get(key).push(d);
    }

    const groups = Array.from(byGroup.entries()).sort(([a], [b]) => a.localeCompare(b));
    container.innerHTML = groups.map(([groupName, devs]) => `
      <section class="dev-area">
        <h3 class="dev-area-header">${escapeHtml(groupName)} <span class="muted">(${devs.length})</span></h3>
        <div class="dev-area-grid">
          ${devs.map(deviceCard).join('')}
        </div>
      </section>
    `).join('');

    container.querySelectorAll('.dev-card').forEach(card => {
      card.addEventListener('click', () => openDetail(parseInt(card.dataset.id, 10)));
    });
  }

  function deviceCard(d) {
    const icon = TYPE_ICONS[d.type] || '🔌';
    const meta = [];
    if (d.manufacturer) meta.push(d.manufacturer);
    if (d.model) meta.push(d.model);
    return `
      <div class="dev-card" data-id="${d.id}">
        <div class="dev-card-icon">${icon}</div>
        <p class="dev-card-name">${escapeHtml(d.friendly_name || d.primary_entity_id || 'Unnamed')}</p>
        <div class="dev-card-meta">
          <span class="dev-card-pill">${escapeHtml(d.type || '')}</span>
          ${d.entity_count ? `<span class="dev-card-pill">${d.entity_count} entities</span>` : ''}
          ${meta.length ? `<span class="dev-card-pill">${escapeHtml(meta.join(' '))}</span>` : ''}
          ${d.owner_name && activeScope === 'home' ? `<span class="dev-card-pill">👤 ${escapeHtml(d.owner_name)}</span>` : ''}
        </div>
      </div>
    `;
  }

  async function openDetail(thingId) {
    const panel = document.getElementById('dev-detail');
    panel.hidden = false;
    document.getElementById('dd-name').textContent = 'Loading…';
    document.getElementById('dd-meta').textContent = '';
    document.getElementById('dd-entities').innerHTML = '';
    document.getElementById('dd-entity-count').textContent = '';

    try {
      const resp = await fetch(`/admin/devices/${thingId}`);
      if (!resp.ok) throw new Error(`detail fetch failed: ${resp.status}`);
      const d = await resp.json();
      renderDetail(d);
    } catch (err) {
      document.getElementById('dd-name').textContent = 'Failed to load';
      document.getElementById('dd-meta').textContent = String(err);
    }
  }

  function renderDetail(d) {
    document.getElementById('dd-name').textContent = d.friendly_name || d.primary_entity_id || 'Device';
    const metaBits = [
      d.area ? `📍 ${d.area}` : null,
      d.scope === 'personal' ? '👤 Personal' : '🏠 Home',
      d.manufacturer || null,
      d.model || null,
      d.primary_state ? `Currently: ${d.primary_state}` : null,
    ].filter(Boolean);
    document.getElementById('dd-meta').textContent = metaBits.join(' · ');

    // Owner select.
    const select = document.getElementById('dd-owner-select');
    select.innerHTML = `<option value="">(no owner — Home device)</option>` +
      members.map(m => `
        <option value="${m.id}" ${parseInt(d.owner_member_id || 0, 10) === parseInt(m.id, 10) ? 'selected' : ''}>${escapeHtml(m.name)}</option>
      `).join('');
    select.dataset.thingId = d.id;
    document.getElementById('dd-owner-status').textContent = '';

    // Categorized properties.
    document.getElementById('dd-entity-count').textContent = `(${d.entity_count})`;
    const ul = document.getElementById('dd-entities');
    const groups = d.grouped_entities || [];
    if (groups.length === 0) {
      ul.innerHTML = `<li class="muted">No entities to show.</li>`;
      return;
    }
    ul.innerHTML = groups.map(group => `
      <li class="category-block">
        <div class="category-header">
          <span class="category-icon">${group.icon}</span>
          <h4>${escapeHtml(group.name)}</h4>
          <span class="muted">${group.entities.length}</span>
        </div>
        <div class="property-grid">
          ${group.entities.map(propertyRow).join('')}
        </div>
      </li>
    `).join('') + `
      <li class="tech-toggle-row">
        <button type="button" id="dd-tech-toggle" class="btn btn-ghost btn-sm linklike">Show technical details</button>
      </li>
    `;

    document.getElementById('dd-tech-toggle')?.addEventListener('click', toggleTechDetails);
  }

  function propertyRow(e) {
    const value = (e.value == null || e.value === '') ? '—' : e.value;
    const ts = e.last_changed ? `<span class="prop-when muted">${escapeHtml(formatRelative(e.last_changed))}</span>` : '';
    const eid = `<span class="prop-eid muted" hidden>${escapeHtml(e.entity_id)}</span>`;
    return `
      <div class="property-row">
        <div class="property-label">
          <span>${escapeHtml(e.property_label || e.entity_id)}</span>
          ${ts}
        </div>
        <div class="property-value">${escapeHtml(String(value))}</div>
        ${eid}
      </div>
    `;
  }

  function toggleTechDetails() {
    const btn = document.getElementById('dd-tech-toggle');
    const showing = btn.dataset.shown === 'yes';
    document.querySelectorAll('.prop-eid').forEach(e => { e.hidden = showing; });
    btn.dataset.shown = showing ? 'no' : 'yes';
    btn.textContent = showing ? 'Show technical details' : 'Hide technical details';
  }

  async function saveOwner() {
    const select = document.getElementById('dd-owner-select');
    const thingId = select.dataset.thingId;
    if (!thingId) return;
    const ownerVal = select.value;
    const status = document.getElementById('dd-owner-status');
    status.textContent = 'Saving…';
    try {
      const resp = await fetch(`/admin/devices/${thingId}/owner`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          owner_member_id: ownerVal === '' ? null : parseInt(ownerVal, 10),
        }),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      status.textContent = `Saved — scope: ${data.scope}`;
      // Re-fetch to pick up the new scope/owner_name.
      await fetchDevices();
      render();
    } catch (err) {
      status.textContent = `Error: ${err.message}`;
    }
  }

  function formatRelative(iso) {
    const t = new Date(iso).getTime();
    if (Number.isNaN(t)) return iso;
    const ageMs = Date.now() - t;
    const sec = Math.floor(ageMs / 1000);
    if (sec < 60) return `${sec}s ago`;
    const min = Math.floor(sec / 60);
    if (min < 60) return `${min}m ago`;
    const hr = Math.floor(min / 60);
    if (hr < 48) return `${hr}h ago`;
    return `${Math.floor(hr / 24)}d ago`;
  }

  function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function bindUI() {
    document.querySelectorAll('.dev-tab').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.dev-tab').forEach(b => b.classList.remove('is-active'));
        btn.classList.add('is-active');
        activeScope = btn.dataset.scope;
        render();
      });
    });
    document.getElementById('dev-filter').addEventListener('input', render);
    document.getElementById('dev-detail-close').addEventListener('click', () => {
      document.getElementById('dev-detail').hidden = true;
    });
    document.getElementById('dd-owner-save').addEventListener('click', saveOwner);
  }

  async function init() {
    bindUI();
    try {
      await fetchDevices();
      await fetchMembers();
      render();
    } catch (err) {
      document.getElementById('dev-list').innerHTML =
        `<p class="muted dev-empty">Failed to load devices: ${escapeHtml(err.message)}</p>`;
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
