const pageStatus = document.getElementById('ds-status');
const jobStatus = document.getElementById('job-status');

function setPageStatus(text, className = '') {
  if (!pageStatus) return;
  pageStatus.className = `status-pill ${className}`.trim();
  pageStatus.textContent = text;
}

async function readJson(response) {
  try {
    return await response.json();
  } catch (_error) {
    return {};
  }
}

async function runJob(button) {
  const job = button.dataset.job;
  if (!job) return;
  const original = button.textContent;
  button.disabled = true;
  button.textContent = 'Starting…';
  setPageStatus(`starting ${job}`, 'running');
  try {
    const response = await fetch(`/admin/data-science/run/${job}`, { method: 'POST' });
    const payload = await readJson(response);
    if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
    button.textContent = payload.started ? 'Started' : 'Already running';
    await pollStatus();
  } catch (error) {
    console.error('data science job failed', error);
    button.textContent = 'Failed';
    setPageStatus(String(error.message || error), 'error');
  } finally {
    setTimeout(() => {
      button.disabled = false;
      button.textContent = original;
    }, 1800);
  }
}

function renderStatus(payload) {
  if (!jobStatus) return;
  const jobs = payload.jobs || [];
  jobStatus.innerHTML = '';
  for (const job of jobs) {
    const card = document.createElement('article');
    card.className = 'job-card';
    card.innerHTML = `
      <strong>${job.name}</strong>
      <span>${job.last_status || 'never'}</span>
      <small>${job.last_run_at || 'not run'} · ${job.last_summary || ''}</small>
    `;
    jobStatus.appendChild(card);
  }
  const stale = payload.embedding?.stale_event_count ?? 0;
  setPageStatus(`${stale} stale embeddings`, stale ? 'running' : 'ok');
}

async function pollStatus() {
  try {
    const response = await fetch('/admin/data-science/status', { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    renderStatus(await response.json());
  } catch (error) {
    console.warn('data science status poll failed', error);
    setPageStatus('status unavailable', 'error');
  }
}

document.addEventListener('click', (event) => {
  const button = event.target.closest('button[data-job]');
  if (button) runJob(button);
});

pollStatus();
setInterval(pollStatus, 5000);
