// Home-Intelligence dashboard live updater
// - Subscribes to /dashboard/stream (SSE) for live activity events
// - Updates agent tiles, sparklines, and the activity feed in place
// - Wires button clicks (quiet, run job, unmute, reload) to /admin endpoints
// - No frameworks, no build step

(() => {
  const grid = document.getElementById('agent-grid');
  const feed = document.getElementById('activity-feed');
  const curator = document.getElementById('curator-narrative');
  const curatorAlerts = document.getElementById('curator-alert-narrative');
  const connState = document.getElementById('connection-state');

  const WINDOW_MINUTES = 5;
  const SPARK_BUCKETS = WINDOW_MINUTES;
  const FEED_MAX = 80;
  const sparkData = new Map(); // agent -> array of per-minute counts

  function setConn(state, label) {
    if (!connState) return;
    connState.classList.remove('online', 'offline', 'connecting');
    connState.classList.add(state);
    connState.textContent = label;
  }

  function tileFor(agent) {
    return grid && grid.querySelector(`.agent-tile[data-agent="${CSS.escape(agent)}"]`);
  }

  function ensureTile(agent) {
    let tile = tileFor(agent);
    if (tile || !grid) return tile;
    tile = document.createElement('article');
    tile.className = 'agent-tile';
    tile.dataset.agent = agent;
    tile.dataset.state = 'idle';
    tile.innerHTML = `
      <header><span class="state-pill"></span><strong class="agent-name">${agent}</strong></header>
      <div class="current-task"><em>idle</em></div>
      <div class="metrics">
        <span class="ok-count">ok: 0</span>
        <span class="err-count">err: 0</span>
        <span class="avg-ms">0 ms</span>
      </div>
      <svg class="sparkline" viewBox="0 0 60 14" preserveAspectRatio="none"></svg>
    `;
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
    const points = values
      .map((v, i) => `${(i * step).toFixed(2)},${(h - (v / max) * (h - 1)).toFixed(2)}`)
      .join(' ');
    svg.innerHTML = `<polyline fill="none" stroke="#58a6ff" stroke-width="1" points="${points}" />`;
  }

  function applyTileSnapshot(row) {
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
      if (okEl) {
        const n = parseInt((okEl.textContent.split(':')[1] || '0').trim(), 10) + 1;
        okEl.textContent = `ok: ${n}`;
      }
      const avgEl = tile.querySelector('.avg-ms');
      if (avgEl) avgEl.textContent = fmtMs(event.duration_ms);
      // bump sparkline last bucket
      const buckets = sparkData.get(event.agent) || new Array(SPARK_BUCKETS).fill(0);
      buckets[buckets.length - 1] += 1;
      sparkData.set(event.agent, buckets);
      renderSparkline(tile.querySelector('.sparkline'), buckets);
      setTimeout(() => {
        if (tile.dataset.state === 'ok') tile.dataset.state = 'idle';
      }, 1500);
    } else if (event.status === 'error') {
      tile.dataset.state = 'error';
      const errEl = tile.querySelector('.err-count');
      if (errEl) {
        const n = parseInt((errEl.textContent.split(':')[1] || '0').trim(), 10) + 1;
        errEl.textContent = `err: ${n}`;
      }
    }
    prependFeedItem(event);
  }

  function prependFeedItem(event) {
    if (!feed) return;
    const li = document.createElement('li');
    li.className = `activity-item status-${event.status}`;
    li.dataset.agent = event.agent;
    li.innerHTML = `
      <span class="ts">${(event.ts || '').replace('T', ' ').slice(11, 19)}</span>
      <span class="agent">${event.agent}</span>
      <span class="cap">${event.capability}</span>
      <span class="status-tag">${event.status}</span>
      ${event.duration_ms ? `<span class="dur">${Math.round(event.duration_ms)}ms</span>` : ''}
    `;
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
        target.innerHTML = `<h3>⚠️ Alert briefing</h3><pre></pre><small></small>`;
        target.querySelector('pre').textContent = text;
        target.querySelector('small').textContent = `updated ${record.generated_at || ''}`;
      }
    } else {
      target.innerHTML = `<pre></pre><small></small>`;
      target.querySelector('pre').textContent = text;
      target.querySelector('small').textContent = `updated ${record.generated_at || ''}`;
    }
  }

  // === SSE connection ======================================================
  let es = null;
  function connect() {
    setConn('connecting', 'connecting…');
    es = new EventSource('/dashboard/stream');
    es.addEventListener('open', () => setConn('online', 'live'));
    es.addEventListener('error', () => {
      setConn('offline', 'reconnecting…');
      // EventSource auto-reconnects; don't manually close.
    });
    es.addEventListener('snapshot', (msg) => {
      try {
        const snapshot = JSON.parse(msg.data);
        for (const row of snapshot.agents || []) applyTileSnapshot(row);
      } catch (_e) {
        /* ignore */
      }
    });
    es.addEventListener('activity', (msg) => {
      try {
        applyActivityEvent(JSON.parse(msg.data));
      } catch (_e) {
        /* ignore */
      }
    });
    es.addEventListener('curator', (msg) => {
      try {
        applyCurator(JSON.parse(msg.data));
      } catch (_e) {
        /* ignore */
      }
    });
    es.addEventListener('heartbeat', () => setConn('online', 'live'));
  }
  connect();

  // === Buttons =============================================================
  async function postJSON(url, body) {
    const resp = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!resp.ok) {
      const text = await resp.text().catch(() => resp.statusText);
      throw new Error(`${resp.status} ${text}`);
    }
    return resp.json();
  }

  document.addEventListener('click', async (ev) => {
    const target = ev.target;
    if (!(target instanceof HTMLElement)) return;

    const quiet = target.dataset.quiet;
    if (quiet) {
      try { await postJSON(`/admin/quiet/${quiet}`); } catch (e) { console.error(e); }
      return;
    }
    if (target.classList.contains('run-btn') && target.dataset.job) {
      try { await postJSON(`/admin/run-job/${target.dataset.job}`); } catch (e) { console.error(e); }
      return;
    }
    if (target.classList.contains('unmute-btn') && target.dataset.key) {
      try {
        await postJSON('/admin/unmute', { key: target.dataset.key });
        target.closest('li')?.remove();
      } catch (e) { console.error(e); }
      return;
    }
    if (target.id === 'reload-btn') {
      try { await postJSON('/admin/reload-policies'); } catch (e) { console.error(e); }
      return;
    }
  });
})();