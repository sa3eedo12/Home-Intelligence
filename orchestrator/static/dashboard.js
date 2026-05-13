// Home-Intelligence dashboard live updater
(() => {
  const grid = document.getElementById('agent-grid');
  const feed = document.getElementById('activity-feed');
  const curator = document.getElementById('curator-narrative');
  const curatorAlerts = document.getElementById('curator-alert-narrative');
  const connState = document.getElementById('connection-state');
  const currentTime = document.getElementById('current-time');
  const lastUpdated = document.getElementById('last-updated');
  const SPARK_BUCKETS = 5;
  const FEED_MAX = 80;
  const sparkData = new Map();

  function updateClock() {
    if (!currentTime) return;
    currentTime.textContent = new Intl.DateTimeFormat([], { hour: 'numeric', minute: '2-digit' }).format(new Date());
  }

  function refreshActivityTimes() {
    document.querySelectorAll('.activity-item .ts[data-ts]').forEach((node) => {
      node.textContent = window.formatTimeAgo(node.dataset.ts);
    });
  }

  function setConn(state, label) {
    if (!connState) return;
    connState.classList.remove('online', 'offline', 'connecting', 'badge-success', 'badge-danger', 'badge-warning');
    connState.classList.add(state);
    connState.classList.add(state === 'online' ? 'badge-success' : state === 'offline' ? 'badge-danger' : 'badge-warning');
    connState.textContent = label;
  }

  function clearSkeletons() { grid?.querySelectorAll('.skeleton-card').forEach((node) => node.remove()); }

  function tileFor(agent) { return grid && grid.querySelector(`.agent-tile[data-agent="${CSS.escape(agent)}"]`); }

  function ensureTile(agent) {
    let tile = tileFor(agent);
    if (tile || !grid) return tile;
    clearSkeletons();
    tile = document.createElement('article');
    tile.className = 'agent-tile card-hover';
    tile.dataset.agent = agent;
    tile.dataset.state = 'idle';
    tile.innerHTML = `
      <header><span class="state-pill" aria-hidden="true"></span><strong class="agent-name"></strong></header>
      <div class="current-task"><em>idle</em></div>
      <div class="metrics"><span class="ok-count">ok: 0</span><span class="err-count">err: 0</span><span class="avg-ms">0 ms</span></div>
      <svg class="sparkline" viewBox="0 0 60 14" preserveAspectRatio="none" aria-hidden="true"></svg>
    `;
    tile.querySelector('.agent-name').textContent = agent;
    grid.appendChild(tile);
    return tile;
  }

  function fmtMs(ms) {
    if (!ms || ms <= 0) return '0 ms';
    if (ms < 1000) return `${Math.round(ms)} ms`;
    return `${(ms / 1000).toFixed(1)} s`;
  }

  function renderSparkline(svg, values) {
    if (!svg) return;
    const max = Math.max(1, ...values);
    const w = 60;
    const h = 14;
    const step = w / Math.max(1, values.length - 1);
    const points = values.map((v, i) => `${(i * step).toFixed(2)},${(h - (v / max) * (h - 1)).toFixed(2)}`).join(' ');
    svg.innerHTML = `<polyline fill="none" stroke="currentColor" stroke-width="1.5" points="${points}" />`;
  }

  function applyTileSnapshot(row) {
    clearSkeletons();
    const tile = ensureTile(row.agent);
    if (!tile) return;
    tile.dataset.state = row.state || 'idle';
    const cap = row.current && row.current.capability;
    const taskEl = tile.querySelector('.current-task');
    if (taskEl) taskEl.innerHTML = cap ? cap : '<em>idle</em>';
    const okEl = tile.querySelector('.ok-count');
    const errEl = tile.querySelector('.err-count');
    const avgEl = tile.querySelector('.avg-ms');
    if (okEl) okEl.textContent = `ok: ${row.ok}`;
    if (errEl) errEl.textContent = `err: ${row.errors}`;
    if (avgEl) avgEl.textContent = fmtMs(row.avg_ms);
    sparkData.set(row.agent, row.sparkline || new Array(SPARK_BUCKETS).fill(0));
    renderSparkline(tile.querySelector('.sparkline'), sparkData.get(row.agent));
  }

  function applyActivityEvent(event) {
    const tile = ensureTile(event.agent);
    if (!tile) return;
    if (event.status === 'started') {
      tile.dataset.state = 'working';
      const taskEl = tile.querySelector('.current-task');
      if (taskEl) taskEl.textContent = event.capability;
    } else if (event.status === 'ok') {
      tile.dataset.state = 'ok';
      const okEl = tile.querySelector('.ok-count');
      if (okEl) okEl.textContent = `ok: ${parseInt((okEl.textContent.split(':')[1] || '0').trim(), 10) + 1}`;
      const avgEl = tile.querySelector('.avg-ms');
      if (avgEl) avgEl.textContent = fmtMs(event.duration_ms);
      const buckets = sparkData.get(event.agent) || new Array(SPARK_BUCKETS).fill(0);
      buckets[buckets.length - 1] += 1;
      sparkData.set(event.agent, buckets);
      renderSparkline(tile.querySelector('.sparkline'), buckets);
      setTimeout(() => { if (tile.dataset.state === 'ok') tile.dataset.state = 'idle'; }, 1500);
    } else if (event.status === 'error') {
      tile.dataset.state = 'error';
      const errEl = tile.querySelector('.err-count');
      if (errEl) errEl.textContent = `err: ${parseInt((errEl.textContent.split(':')[1] || '0').trim(), 10) + 1}`;
      Toast.show(`${event.agent} failed ${event.capability}`, 'danger');
    }
    prependFeedItem(event);
  }

  function prependFeedItem(event) {
    if (!feed) return;
    feed.querySelector('.activity-empty')?.remove();
    const li = document.createElement('li');
    li.className = `activity-item status-${event.status}`;
    li.dataset.agent = event.agent;
    li.innerHTML = `<span class="ts" data-ts="${event.ts || new Date().toISOString()}">${formatTimeAgo(event.ts || new Date())}</span><span class="agent"></span><span class="cap"></span><span class="status-tag"></span>${event.duration_ms ? `<span class="dur">${Math.round(event.duration_ms)}ms</span>` : ''}`;
    li.querySelector('.agent').textContent = event.agent;
    li.querySelector('.cap').textContent = event.capability;
    li.querySelector('.status-tag').textContent = event.status;
    feed.insertBefore(li, feed.firstChild);
    while (feed.children.length > FEED_MAX) feed.removeChild(feed.lastChild);
  }

  function applyCurator(event) {
    const target = event.key === 'dashboard:alert_narrative' ? curatorAlerts : curator;
    if (!target) return;
    const record = event.record || {};
    const text = record.narrative || '';
    if (event.key === 'dashboard:alert_narrative') {
      target.hidden = !text;
      if (text) {
        target.innerHTML = '<h3>⚠️ Alert briefing</h3><pre></pre><small></small>';
        target.querySelector('pre').textContent = text;
        target.querySelector('small').textContent = `updated ${record.generated_at || ''}`;
      }
    } else {
      target.innerHTML = '<pre></pre><small></small>';
      target.querySelector('pre').textContent = text || 'No curator narrative yet.';
      target.querySelector('small').textContent = `updated ${record.generated_at || ''}`;
    }
    if (lastUpdated && record.generated_at) lastUpdated.textContent = record.generated_at;
  }

  function connect() {
    setConn('connecting', 'connecting…');
    const es = new EventSource('/dashboard/stream');
    es.addEventListener('open', () => setConn('online', 'live'));
    es.addEventListener('error', () => setConn('offline', 'reconnecting…'));
    es.addEventListener('snapshot', (msg) => { try { (JSON.parse(msg.data).agents || []).forEach(applyTileSnapshot); } catch (_e) { /* ignore */ } });
    es.addEventListener('activity', (msg) => { try { applyActivityEvent(JSON.parse(msg.data)); } catch (_e) { /* ignore */ } });
    es.addEventListener('curator', (msg) => { try { applyCurator(JSON.parse(msg.data)); } catch (_e) { /* ignore */ } });
    es.addEventListener('heartbeat', () => setConn('online', 'live'));
  }

  const navToggle = document.getElementById('nav-toggle');
  const navToggleButton = document.querySelector('.nav-toggle-button');
  function syncNavToggle() {
    if (navToggleButton && navToggle instanceof HTMLInputElement) {
      navToggleButton.setAttribute('aria-expanded', String(navToggle.checked));
    }
  }
  navToggle?.addEventListener('change', syncNavToggle);
  navToggleButton?.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      if (navToggle instanceof HTMLInputElement) {
        navToggle.checked = !navToggle.checked;
        syncNavToggle();
      }
    }
  });

  document.addEventListener('click', async (ev) => {
    const target = ev.target.closest('button');
    if (!(target instanceof HTMLElement)) return;
    const quiet = target.dataset.quiet;
    if (quiet) {
      await apiPost(`/admin/quiet/${quiet}`, undefined).then(() => Toast.show(`Quiet ${quiet}`, 'success')).catch(() => {});
      return;
    }
    if (target.classList.contains('run-btn') && target.dataset.job) {
      await apiPost(`/admin/run-job/${target.dataset.job}`, undefined).then(() => Toast.show(`Started ${target.dataset.job}`, 'success')).catch(() => {});
      return;
    }
    if (target.classList.contains('unmute-btn') && target.dataset.key) {
      await apiPost('/admin/unmute', { key: target.dataset.key }).then(() => {
        Toast.show(`Unmuted ${target.dataset.key}`, 'success');
        target.closest('li')?.remove();
      }).catch(() => {});
      return;
    }
    if (target.id === 'reload-btn') {
      await apiPost('/admin/reload-policies', undefined).then(() => Toast.show('Configuration reloaded', 'success')).catch(() => {});
    }
  });

  updateClock();
  refreshActivityTimes();
  document.querySelectorAll('.agent-tile:not(.skeleton-card)').forEach((tile) => renderSparkline(tile.querySelector('.sparkline'), new Array(SPARK_BUCKETS).fill(0)));
  setInterval(updateClock, 15000);
  setInterval(refreshActivityTimes, 30000);
  connect();
})();
