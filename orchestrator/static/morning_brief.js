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
