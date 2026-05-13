(() => {
  const dataNode = document.getElementById('health-data');
  if (!dataNode) return;

  let snapshot = {};
  try {
    snapshot = JSON.parse(dataNode.textContent || '{}');
  } catch (_err) {
    snapshot = {};
  }

  const charts = snapshot.charts || {};
  const aggregateGrid = document.getElementById('health-aggregate-grid');
  const aggregateStatus = document.getElementById('health-aggregate-status');
  const recentBody = document.getElementById('health-recent-body');
  const recentCount = document.getElementById('health-recent-count');
  const testButton = document.getElementById('healthkit-test-btn');
  const tokenInput = document.getElementById('healthkit-token');
  const testStatus = document.getElementById('healthkit-test-status');
  const toast = window.Toast || { show: () => {} };
  const apiGet = window.apiGet || (async (url) => {
    const response = await fetch(url, { method: 'GET', cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  });
  const apiPost = window.apiPost || (async (url, body, options = {}) => {
    const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
    const response = await fetch(url, { method: 'POST', headers, body: JSON.stringify(body) });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  });

  function valuesFor(metric) {
    return (charts[metric] || [])
      .map((row) => ({
        day: row.day || row.date || '',
        value: Number(row.value ?? row.sum ?? row.avg ?? 0),
      }))
      .filter((row) => Number.isFinite(row.value));
  }

  function clear(svg) {
    while (svg.firstChild) svg.removeChild(svg.firstChild);
  }

  function append(svg, tag, attrs = {}) {
    const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, String(value)));
    svg.appendChild(node);
    return node;
  }

  function bounds(svg) {
    const box = svg.viewBox && svg.viewBox.baseVal;
    return { width: box && box.width ? box.width : 320, height: box && box.height ? box.height : 120 };
  }

  function empty(svg) {
    const { width, height } = bounds(svg);
    append(svg, 'text', { x: width / 2, y: height / 2, 'text-anchor': 'middle' }).textContent = 'No data yet';
  }

  function renderLine(svg, rows) {
    clear(svg);
    if (!rows.length) return empty(svg);
    const { width, height } = bounds(svg);
    const pad = 10;
    const min = Math.min(...rows.map((row) => row.value));
    const max = Math.max(...rows.map((row) => row.value));
    const range = max - min || 1;
    const points = rows.map((row, index) => {
      const x = pad + (index / Math.max(1, rows.length - 1)) * (width - pad * 2);
      const y = height - pad - ((row.value - min) / range) * (height - pad * 2);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    });
    append(svg, 'line', { class: 'axis', x1: pad, y1: height - pad, x2: width - pad, y2: height - pad });
    append(svg, 'path', { d: `M ${points.join(' L ')}` });
    const last = rows[rows.length - 1];
    append(svg, 'text', { x: width - pad, y: pad + 2, 'text-anchor': 'end' }).textContent = Math.round(last.value).toLocaleString();
  }

  function renderBars(svg, rows) {
    clear(svg);
    if (!rows.length) return empty(svg);
    const { width, height } = bounds(svg);
    const pad = 10;
    const max = Math.max(...rows.map((row) => row.value), 1);
    const barGap = 2;
    const barWidth = Math.max(2, (width - pad * 2) / rows.length - barGap);
    append(svg, 'line', { class: 'axis', x1: pad, y1: height - pad, x2: width - pad, y2: height - pad });
    rows.forEach((row, index) => {
      const barHeight = ((height - pad * 2) * row.value) / max;
      const x = pad + index * (barWidth + barGap);
      const y = height - pad - barHeight;
      append(svg, 'rect', { x, y, width: barWidth, height: Math.max(1, barHeight), rx: 2 });
    });
    append(svg, 'text', { x: width - pad, y: pad + 2, 'text-anchor': 'end' }).textContent = Math.round(max).toLocaleString();
  }

  function setBadge(node, label, kind) {
    if (!node) return;
    node.classList.remove('badge-success', 'badge-danger', 'badge-warning', 'badge-info', 'badge-muted');
    node.classList.add(`badge-${kind}`);
    node.textContent = label;
  }

  function aggregateNumber(row) {
    const value = Number(row.sum ?? row.value ?? row.avg ?? 0);
    return Number.isFinite(value) ? value : 0;
  }

  function formatCount(value) {
    const number = Number(value || 0);
    return Number.isFinite(number) ? Math.round(number).toLocaleString() : '—';
  }

  function formatDuration(minutes) {
    const value = Number(minutes || 0);
    if (!Number.isFinite(value) || value <= 0) return '—';
    const hours = Math.floor(value / 60);
    const mins = Math.round(value % 60);
    return hours ? `${hours}h ${mins}m` : `${mins}m`;
  }

  function addText(parent, tag, text) {
    const node = document.createElement(tag);
    node.textContent = text;
    parent.appendChild(node);
    return node;
  }

  function renderAggregateTile(def, rows) {
    const tile = document.createElement('article');
    tile.className = 'aggregate-tile';
    addText(tile, 'span', def.label);
    const values = rows.map(aggregateNumber);
    const total = values.reduce((sum, value) => sum + value, 0);
    const days = Math.max(rows.length, 1);
    if (!rows.length) {
      addText(tile, 'strong', '—');
      addText(tile, 'small', 'No data yet');
      return tile;
    }
    if (def.metric === 'sleep_asleep') {
      addText(tile, 'strong', formatDuration(total / days));
      addText(tile, 'small', `${formatDuration(total)} total over ${rows.length} day(s)`);
    } else if (def.metric === 'steps') {
      addText(tile, 'strong', formatCount(total));
      addText(tile, 'small', `${formatCount(total / days)} avg/day`);
    } else {
      const latest = rows[rows.length - 1] || {};
      const unit = latest.unit || 'kg';
      const latestValue = Number(latest.value ?? latest.avg ?? latest.sum ?? 0);
      const avg = values.reduce((sum, value) => sum + value, 0) / days;
      addText(tile, 'strong', Number.isFinite(latestValue) ? `${latestValue.toFixed(1)} ${unit}` : '—');
      addText(tile, 'small', `${avg.toFixed(1)} ${unit} 7-day avg`);
    }
    return tile;
  }

  async function loadAggregates() {
    if (!aggregateGrid) return;
    const defs = [
      { metric: 'sleep_asleep', label: 'Sleep' },
      { metric: 'steps', label: 'Steps' },
      { metric: 'weight', label: 'Weight' },
    ];
    try {
      const results = await Promise.all(defs.map(async (def) => ({
        def,
        data: await apiGet(`/admin/healthkit/aggregate?metric=${encodeURIComponent(def.metric)}&days=7`, { toastErrors: false }),
      })));
      aggregateGrid.replaceChildren();
      results.forEach(({ def, data }) => aggregateGrid.appendChild(renderAggregateTile(def, data.items || [])));
      setBadge(aggregateStatus, 'loaded', 'success');
    } catch (error) {
      setBadge(aggregateStatus, 'error', 'danger');
      aggregateGrid.replaceChildren();
      const tile = document.createElement('article');
      tile.className = 'aggregate-tile';
      addText(tile, 'strong', 'Unable to load aggregates');
      addText(tile, 'small', error.message || String(error));
      aggregateGrid.appendChild(tile);
    }
  }

  function tableCell(row, text, className) {
    const cell = document.createElement('td');
    if (className) cell.className = className;
    cell.textContent = text;
    row.appendChild(cell);
  }

  function metricValue(item) {
    const value = Number(item.value);
    if (!Number.isFinite(value)) return item.value == null ? '—' : String(item.value);
    if (Math.abs(value) >= 1000) return Math.round(value).toLocaleString();
    return Number.isInteger(value) ? String(value) : value.toFixed(1);
  }

  function renderRecent(items) {
    if (!recentBody) return;
    recentBody.replaceChildren();
    setBadge(recentCount, `${items.length} rows`, items.length ? 'info' : 'muted');
    if (!items.length) {
      const row = document.createElement('tr');
      tableCell(row, 'No HealthKit data yet — see setup instructions below', 'empty-cell');
      row.firstChild.colSpan = 5;
      recentBody.appendChild(row);
      return;
    }
    items.forEach((item) => {
      const row = document.createElement('tr');
      tableCell(row, item.metric || '—');
      tableCell(row, metricValue(item));
      tableCell(row, item.unit || '—');
      tableCell(row, item.started_at ? (window.formatTimeAgo ? window.formatTimeAgo(item.started_at) : item.started_at) : '—');
      tableCell(row, item.source || '—');
      recentBody.appendChild(row);
    });
  }

  async function loadRecentHealthKit() {
    if (!recentBody) return;
    try {
      const payload = await apiGet('/admin/healthkit/recent?limit=20', { toastErrors: false });
      renderRecent(payload.items || []);
    } catch (error) {
      setBadge(recentCount, 'error', 'danger');
      recentBody.replaceChildren();
      const row = document.createElement('tr');
      tableCell(row, error.message || String(error), 'empty-cell');
      row.firstChild.colSpan = 5;
      recentBody.appendChild(row);
    }
  }

  function setWebhookStatus(message, kind = '') {
    if (!testStatus) return;
    testStatus.classList.remove('success', 'error');
    if (kind) testStatus.classList.add(kind);
    testStatus.textContent = message;
  }

  async function testWebhook() {
    const token = (tokenInput?.value || '').trim();
    if (!token) {
      setWebhookStatus('Paste your X-Health-Token first, or use the curl snippet below.', 'error');
      toast.show('HealthKit token required for test webhook', 'warning');
      return;
    }
    const now = new Date().toISOString();
    const body = {
      data: {
        metrics: [
          {
            type: 'HKQuantityTypeIdentifierStepCount',
            unit: 'count',
            data: [{ date: now, qty: 1, source: 'dashboard_test' }],
          },
        ],
      },
    };
    testButton.disabled = true;
    setWebhookStatus('Sending test payload…');
    try {
      const result = await apiPost('/admin/healthkit/sync', body, {
        headers: { 'X-Health-Token': token },
        toastErrors: false,
      });
      setWebhookStatus(`Inserted ${result.inserted || 0}; skipped ${result.skipped || 0}.`, 'success');
      toast.show('HealthKit webhook accepted', 'success');
      await Promise.all([loadRecentHealthKit(), loadAggregates()]);
    } catch (error) {
      setWebhookStatus(error.message || String(error), 'error');
      toast.show(error.message || String(error), 'danger');
    } finally {
      testButton.disabled = false;
    }
  }

  document.querySelectorAll('svg[data-chart]').forEach((svg) => {
    const metric = svg.dataset.chart;
    const rows = valuesFor(metric);
    if (svg.classList.contains('sparkline')) {
      renderLine(svg, rows.slice(-14));
    } else if (metric === 'steps' || metric === 'sleep_asleep') {
      renderBars(svg, rows);
    } else {
      renderLine(svg, rows);
    }
  });

  testButton?.addEventListener('click', testWebhook);
  loadRecentHealthKit();
  loadAggregates();
})();
