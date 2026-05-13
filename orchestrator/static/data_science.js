const pageStatus = document.getElementById('ds-status');
const jobStatus = document.getElementById('job-status');
function setPageStatus(text, className = '') {
  if (!pageStatus) return;
  pageStatus.className = `status-pill badge badge-muted ${className}`.trim();
  pageStatus.textContent = text;
}
async function runJob(button) {
  const job = button.dataset.job;
  if (!job) return;
  const original = button.textContent;
  button.disabled = true;
  button.textContent = 'Starting…';
  setPageStatus(`starting ${job}`, 'running');
  try {
    const payload = await apiPost(`/admin/data-science/run/${job}`);
    button.textContent = payload.started ? 'Started' : 'Already running';
    Toast.show(payload.started ? `Started ${job}` : `${job} is already running`, 'success');
    await pollStatus();
  } catch (error) {
    console.error('data science job failed', error);
    button.textContent = 'Failed';
    setPageStatus(String(error.message || error), 'error');
  } finally {
    setTimeout(() => { button.disabled = false; button.textContent = original; }, 1800);
  }
}
function renderStatus(payload) {
  if (!jobStatus) return;
  const jobs = payload.jobs || [];
  jobStatus.innerHTML = '';
  if (!jobs.length) {
    const empty = document.createElement('div');
    empty.className = 'empty-state compact';
    empty.innerHTML = '<div class="empty-state-icon">📊</div><h3>No job status yet</h3><p>Run a job to populate live status.</p>';
    jobStatus.appendChild(empty);
  }
  for (const job of jobs) {
    const card = document.createElement('article');
    card.className = 'job-card';
    card.innerHTML = '<strong></strong><span></span><small></small>';
    card.querySelector('strong').textContent = job.name;
    card.querySelector('span').textContent = job.last_status || 'never';
    card.querySelector('small').textContent = `${job.last_run_at || 'not run'} · ${job.last_summary || ''}`;
    jobStatus.appendChild(card);
  }
  const stale = payload.embedding?.stale_event_count ?? 0;
  setPageStatus(`${stale} stale embeddings`, stale ? 'running' : 'ok');
}
async function pollStatus() {
  try { renderStatus(await apiGet('/admin/data-science/status', { toastErrors: false })); }
  catch (error) { console.warn('data science status poll failed', error); setPageStatus('status unavailable', 'error'); }
}
document.addEventListener('click', (event) => {
  const button = event.target.closest('button[data-job]');
  if (button) runJob(button);
});
pollStatus();
setInterval(pollStatus, 5000);
