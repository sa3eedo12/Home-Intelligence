async function copyProposalPrompt(button) {
  const proposalId = button.dataset.proposalId;
  if (!proposalId) return;
  const original = button.textContent;
  button.disabled = true;
  button.textContent = 'Copying…';
  try {
    const payload = await apiPost(`/admin/proposals/${proposalId}/format`);
    await navigator.clipboard.writeText(payload.markdown || '');
    button.textContent = 'Copied';
    Toast.show('Proposal prompt copied', 'success');
  } catch (error) {
    console.error('copy proposal failed', error);
    button.textContent = 'Failed';
  } finally {
    setTimeout(() => {
      button.disabled = false;
      button.textContent = original || 'Copy prompt';
    }, 1600);
  }
}

function deliveryBoxFor(button) {
  const card = button.closest('.proposal-card');
  let box = card?.querySelector('.proposal-delivery');
  if (!box && card) {
    box = document.createElement('div');
    box.className = 'proposal-delivery';
    box.setAttribute('aria-live', 'polite');
    card.querySelector('.proposal-actions')?.appendChild(box);
  }
  return box;
}
function showDeliveryStatus(button, message, className = 'delivery-status') {
  const box = deliveryBoxFor(button);
  if (!box) return;
  let status = box.querySelector(`.${className}`);
  if (!status) {
    status = document.createElement('small');
    status.className = className;
    box.appendChild(status);
  }
  status.textContent = message;
}
function showDeliveryLink(button, url, label, className) {
  const box = deliveryBoxFor(button);
  if (!box || !url) return;
  let link = box.querySelector(`a.${className}`);
  if (!link) {
    link = document.createElement('a');
    link.className = className;
    link.target = '_blank';
    link.rel = 'noreferrer';
    box.appendChild(link);
  }
  link.href = url;
  link.textContent = label;
}
function configuredHelpText(payload) {
  const detail = String(payload.detail || payload.message || '').toLowerCase();
  if (!detail.includes('not configured')) return null;
  return 'Set GITHUB_REPO_TOKEN and GITHUB_REPO in your TrueNAS .env to enable this.';
}
async function postProposalDelivery(button, endpoint, options) {
  const proposalId = button.dataset.proposalId;
  if (!proposalId) return;
  const original = button.textContent;
  button.disabled = true;
  button.textContent = options.pendingText;
  try {
    const payload = await apiPost(`/admin/proposals/${proposalId}/${endpoint}`, undefined, { toastErrors: false });
    const url = payload[options.urlKey] || payload.url || payload.pr_url || payload.issue_url;
    showDeliveryLink(button, url, options.linkLabel, options.linkClass);
    if (payload.message) showDeliveryStatus(button, payload.message);
    button.textContent = payload.already_dispatched ? 'Already sent' : 'Sent';
    Toast.show(payload.message || `${options.linkLabel} ready`, 'success');
  } catch (error) {
    console.error(`${endpoint} failed`, error);
    const help = configuredHelpText({ detail: error.message || String(error) });
    if (help) showDeliveryStatus(button, help, 'delivery-help');
    else showDeliveryStatus(button, `Failed: ${error.message || error}`, 'delivery-error');
    Toast.show(error.message || String(error), 'danger');
    button.disabled = false;
    button.textContent = 'Failed';
    setTimeout(() => { button.textContent = original; }, 2200);
    return;
  }
  button.disabled = true;
}

document.addEventListener('click', (event) => {
  const copyButton = event.target.closest('.copy-prompt');
  if (copyButton) { copyProposalPrompt(copyButton); return; }
  const issueButton = event.target.closest('.open-issue');
  if (issueButton) {
    postProposalDelivery(issueButton, 'github-issue', { pendingText: 'Opening…', urlKey: 'url', linkLabel: 'GitHub issue', linkClass: 'github-issue-link' });
    return;
  }
  const copilotButton = event.target.closest('.copilot-dispatch');
  if (copilotButton) {
    postProposalDelivery(copilotButton, 'copilot-dispatch', { pendingText: 'Dispatching…', urlKey: 'issue_url', linkLabel: 'Copilot issue', linkClass: 'copilot-issue-link' });
  }
});

const banner = document.getElementById('reflection-banner');
const phaseEl = document.getElementById('reflection-phase');
const elapsedEl = document.getElementById('reflection-elapsed');
const runNowBtn = document.getElementById('run-now-btn');
let pollHandle = null;
let lastSeenRunning = banner ? banner.dataset.running === '1' : false;
function fmtElapsed(seconds) {
  if (seconds == null || isNaN(seconds)) return '';
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const r = s % 60;
  return r ? `${m}m ${r}s` : `${m}m`;
}
async function tickReflectionStatus() {
  try {
    const status = await apiGet('/admin/reflection/status', { toastErrors: false });
    const running = !!status.running;
    if (banner) {
      banner.classList.toggle('visible', running);
      banner.dataset.running = running ? '1' : '0';
      if (phaseEl) phaseEl.textContent = status.phase || (running ? 'starting' : '');
      if (elapsedEl) elapsedEl.textContent = running ? `· ${fmtElapsed(status.elapsed_seconds)}` : '';
    }
    if (lastSeenRunning && !running) { window.location.reload(); return; }
    lastSeenRunning = running;
    if (running && pollHandle == null) pollHandle = setInterval(tickReflectionStatus, 3000);
    else if (!running && pollHandle != null) { clearInterval(pollHandle); pollHandle = null; }
  } catch (error) {
    console.warn('reflection status poll failed', error);
  }
}
async function startReflectionNow() {
  if (!runNowBtn) return;
  const original = runNowBtn.textContent;
  runNowBtn.disabled = true;
  runNowBtn.textContent = 'Starting…';
  try {
    await apiPost('/admin/reflection/run');
    runNowBtn.textContent = 'Started';
    Toast.show('Reflection started', 'success');
    await tickReflectionStatus();
  } catch (error) {
    console.error('start reflection failed', error);
    runNowBtn.textContent = 'Failed';
  } finally {
    setTimeout(() => {
      runNowBtn.disabled = false;
      runNowBtn.textContent = original || 'Run reflection now';
    }, 2000);
  }
}
if (runNowBtn) runNowBtn.addEventListener('click', startReflectionNow);
tickReflectionStatus();
if (lastSeenRunning && pollHandle == null) pollHandle = setInterval(tickReflectionStatus, 3000);

async function handleQuestionForm(form, action) {
  const card = form.closest('.question-card');
  const key = card?.dataset.profileKey;
  if (!key) return;
  const input = form.querySelector('.question-answer-input');
  const status = form.querySelector('.question-status');
  const buttons = form.querySelectorAll('button');
  const value = (input?.value || '').trim();
  if (action === 'save' && !value) {
    Toast.show('Type an answer first.', 'warning');
    if (status) { status.hidden = false; status.textContent = 'Type an answer first.'; }
    return;
  }
  buttons.forEach((b) => (b.disabled = true));
  if (status) { status.hidden = false; status.textContent = action === 'save' ? 'Saving…' : 'Skipping…'; }
  try {
    const url = action === 'save' ? '/admin/profile/upsert' : '/admin/profile/skip';
    const body = action === 'save' ? { key, value, source: 'morning_brief' } : { key };
    await apiPost(url, body);
    if (status) status.textContent = action === 'save' ? '✓ Saved' : '✓ Skipped';
    Toast.show(action === 'save' ? 'Answer saved' : 'Question skipped', 'success');
    card.classList.add('question-answered');
    setTimeout(() => card.remove(), 800);
  } catch (error) {
    console.error('question save/skip failed', error);
    if (status) status.textContent = `Failed: ${error.message || error}`;
    buttons.forEach((b) => (b.disabled = false));
  }
}
document.addEventListener('submit', (event) => {
  const form = event.target.closest('.question-answer-form');
  if (!form) return;
  event.preventDefault();
  handleQuestionForm(form, 'save');
});
document.addEventListener('click', (event) => {
  const skip = event.target.closest('.skip-question');
  if (skip) {
    event.preventDefault();
    handleQuestionForm(skip.closest('.question-answer-form'), 'skip');
  }
});
