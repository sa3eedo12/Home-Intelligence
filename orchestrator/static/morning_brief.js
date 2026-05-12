async function copyProposalPrompt(button) {
  const proposalId = button.dataset.proposalId;
  if (!proposalId) return;
  const original = button.textContent;
  button.disabled = true;
  button.textContent = 'Copying…';
  try {
    const response = await fetch(`/admin/proposals/${proposalId}/format`, { method: 'POST' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    await navigator.clipboard.writeText(payload.markdown || '');
    button.textContent = 'Copied';
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

document.addEventListener('click', (event) => {
  const button = event.target.closest('.copy-prompt');
  if (button) copyProposalPrompt(button);
});

// === Reflection-in-progress indicator =====================================
// Polls /admin/reflection/status while a run is happening. Auto-refreshes
// the page once the run finishes so the new brief appears.

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
    const res = await fetch('/admin/reflection/status', { cache: 'no-store' });
    if (!res.ok) return;
    const status = await res.json();
    const running = !!status.running;

    if (banner) {
      banner.classList.toggle('visible', running);
      banner.dataset.running = running ? '1' : '0';
      if (phaseEl) phaseEl.textContent = status.phase || (running ? 'starting' : '');
      if (elapsedEl) elapsedEl.textContent = running
        ? `· ${fmtElapsed(status.elapsed_seconds)}`
        : '';
    }

    if (lastSeenRunning && !running) {
      // Just finished — refresh so the new brief shows up.
      window.location.reload();
      return;
    }
    lastSeenRunning = running;

    if (running && pollHandle == null) {
      pollHandle = setInterval(tickReflectionStatus, 3000);
    } else if (!running && pollHandle != null) {
      clearInterval(pollHandle);
      pollHandle = null;
    }
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
    const res = await fetch('/admin/reflection/run', { method: 'POST' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    runNowBtn.textContent = 'Started';
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

// Always poll once on load — covers the case where a nightly run kicked off
// while no one was looking at the dashboard.
tickReflectionStatus();
// If the page loaded with running=true (server-rendered), kick off polling now.
if (lastSeenRunning && pollHandle == null) {
  pollHandle = setInterval(tickReflectionStatus, 3000);
}


// === Profile question answer/skip ==========================================
// Each "Question for you" card carries a data-profile-key attribute. Save
// posts to /admin/profile/upsert; Skip posts to /admin/profile/skip so the
// reflector deprioritises that gap. After either, fade the card out.

async function handleQuestionForm(form, action) {
  const card = form.closest('.question-card');
  const key = card?.dataset.profileKey;
  if (!key) return;
  const input = form.querySelector('.question-answer-input');
  const status = form.querySelector('.question-status');
  const buttons = form.querySelectorAll('button');
  const value = (input?.value || '').trim();

  if (action === 'save' && !value) {
    if (status) {
      status.hidden = false;
      status.textContent = 'Type an answer first.';
    }
    return;
  }

  buttons.forEach((b) => (b.disabled = true));
  if (status) {
    status.hidden = false;
    status.textContent = action === 'save' ? 'Saving…' : 'Skipping…';
  }

  try {
    const url = action === 'save' ? '/admin/profile/upsert' : '/admin/profile/skip';
    const body = action === 'save' ? { key, value, source: 'morning_brief' } : { key };
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    if (status) status.textContent = action === 'save' ? '✓ Saved' : '✓ Skipped';
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
