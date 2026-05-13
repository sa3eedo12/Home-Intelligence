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
})();
