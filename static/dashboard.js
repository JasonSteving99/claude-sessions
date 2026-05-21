function confirmDestroy(btn) {
  const project = btn.dataset.project;
  const dir = btn.dataset.dir;
  const count = btn.dataset.count;
  document.getElementById('m-count').textContent = count;
  document.getElementById('m-dir').textContent = dir;
  document.getElementById('m-form').action = '/projects/' + encodeURIComponent(project) + '/destroy';
  const inp = document.getElementById('m-input');
  inp.value = '';
  document.getElementById('m-go').disabled = true;
  document.getElementById('destroy-modal').style.display = 'flex';
  setTimeout(() => inp.focus(), 0);
}
function closeModal() {
  document.getElementById('destroy-modal').style.display = 'none';
}
function onConfirmInput() {
  document.getElementById('m-go').disabled = document.getElementById('m-input').value !== 'DELETE';
}
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

// ── Live refresh ─────────────────────────────────────────────────────────────
// Poll the groups partial and swap it in place — no full-page reload. Skip the
// swap if the user is currently typing somewhere, or the destroy modal is open,
// so we never wipe out in-progress input.
const REFRESH_MS = 5000;
function shouldSkipRefresh() {
  // Block when a focused input has actual content — otherwise an empty-but-focused
  // field (e.g. after submitting "+ New Project" and reset()) would freeze polling.
  const a = document.activeElement;
  if (a && (a.tagName === 'INPUT' || a.tagName === 'TEXTAREA') && a.value && a.value.length > 0) return true;
  if (a && a.isContentEditable && a.textContent && a.textContent.length > 0) return true;
  const modal = document.getElementById('destroy-modal');
  if (modal && modal.style.display !== 'none') return true;
  if (document.hidden) return true;
  return false;
}
async function refreshGroups() {
  if (shouldSkipRefresh()) return;
  try {
    const r = await fetch('/partial/groups', { cache: 'no-store' });
    if (!r.ok) return;
    const html = await r.text();
    const target = document.getElementById('groups');
    if (target) target.innerHTML = html;
  } catch (_) { /* ignore transient network errors */ }
}
setInterval(refreshGroups, REFRESH_MS);
// Also refresh immediately when the tab regains focus
document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshGroups(); });

// Event-driven refresh when the user creates a new project/session.
// Both "+ New Project" and "+ Session" forms POST to /sessions and open in a
// new tab — the dashboard tab's JS keeps running, so we can react to the submit
// directly. Backend writes the DB row early but full setup takes a beat, so we
// poll at several delays to catch it as soon as it appears.
document.addEventListener('submit', (e) => {
  const action = e.target.getAttribute('action');
  if (action === '/sessions') {
    [400, 1200, 2500].forEach(ms => setTimeout(refreshGroups, ms));
  }
});
