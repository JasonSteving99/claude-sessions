// ── Host daemon ──────────────────────────────────────────────────────────────
// The dashboard is served by the sandbox; port-management lives on the host.
// The browser is the only party that talks to BOTH — the sandbox can never
// reach the host daemon. Build the URL from location.hostname so this works
// for both 127.0.0.1 and (eventually) tailnet access.
const HOST_DAEMON_PORT = document.querySelector('meta[name=host-daemon-port]')?.content || '33001';
const HOST_DAEMON = `${location.protocol}//${location.hostname}:${HOST_DAEMON_PORT}`;

async function hdFetch(path, opts = {}) {
  return fetch(HOST_DAEMON + path, { cache: 'no-store', ...opts });
}

// ── Destroy project modal ────────────────────────────────────────────────────
function confirmDestroy(btn) {
  const project = btn.dataset.project;
  const dir = btn.dataset.dir;
  const count = btn.dataset.count;
  document.getElementById('m-count').textContent = count;
  document.getElementById('m-dir').textContent = dir;
  const form = document.getElementById('m-form');
  form.action = '/projects/' + encodeURIComponent(project) + '/destroy';
  form.dataset.project = project;
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
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  closeModal();
  closePortModal();
  closeApp();
});

// ── Full-screen app overlay ───────────────────────────────────────────────────
function openApp(url) {
  document.getElementById('app-frame').src = url;
  document.getElementById('app-overlay').classList.add('visible');
  history.pushState({ appOverlay: true }, '');
}
function closeApp() {
  const overlay = document.getElementById('app-overlay');
  if (!overlay.classList.contains('visible')) return;
  overlay.classList.remove('visible');
  document.getElementById('app-frame').src = '';
}
// Android back gesture / browser back button closes the overlay
window.addEventListener('popstate', () => closeApp());

// Intercept project-destroy submit: tell the host daemon to drop all of the
// project's exposed ports BEFORE the sandbox tears down the project itself.
// If the daemon call fails the destroy still goes through — orphan ports are
// recoverable (just unpublish manually); a stuck destroy is worse.
document.getElementById('m-form').addEventListener('submit', async (e) => {
  const project = e.target.dataset.project;
  if (!project) return;
  e.preventDefault();
  try {
    await hdFetch(`/projects/${encodeURIComponent(project)}/ports`, { method: 'DELETE' });
  } catch (_) { /* best-effort */ }
  try {
    await fetch(e.target.action, { method: 'POST', body: new FormData(e.target) });
  } catch (_) {}
  closeModal();
  await refreshGroups();
});

// ── Add-port modal ───────────────────────────────────────────────────────────
function openAddPort(btn) {
  const project = btn.dataset.project;
  document.getElementById('p-project').textContent = project;
  document.getElementById('p-form').dataset.project = project;
  const inp = document.getElementById('p-input');
  inp.value = '';
  document.getElementById('p-go').disabled = true;
  document.getElementById('p-error').style.display = 'none';
  document.getElementById('port-modal').style.display = 'flex';
  setTimeout(() => inp.focus(), 0);
}
function closePortModal() {
  document.getElementById('port-modal').style.display = 'none';
}
function onPortInput() {
  const v = parseInt(document.getElementById('p-input').value, 10);
  document.getElementById('p-go').disabled = !(Number.isInteger(v) && v >= 1 && v <= 65535);
  document.getElementById('p-error').style.display = 'none';
}
async function submitAddPort(e) {
  e.preventDefault();
  const project = e.target.dataset.project;
  const port = parseInt(document.getElementById('p-input').value, 10);
  const err = document.getElementById('p-error');
  const go = document.getElementById('p-go');
  go.disabled = true;
  err.style.display = 'none';
  try {
    const r = await hdFetch('/ports', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project, port, protocol: 'tcp' }),
    });
    if (!r.ok) {
      let detail = `HTTP ${r.status}`;
      try { detail = (await r.json()).detail || detail; } catch (_) {}
      err.textContent = detail;
      err.style.display = 'block';
      go.disabled = false;
      return;
    }
    closePortModal();
    hydratePorts();  // immediate refresh — don't wait for the 5s poll
  } catch (e) {
    err.textContent = 'host daemon unreachable — is `just up` running?';
    err.style.display = 'block';
    go.disabled = false;
  }
}

async function removePort(btn) {
  const port = btn.dataset.port;
  const protocol = btn.dataset.protocol;
  btn.disabled = true;
  try {
    const r = await hdFetch(`/ports/${port}?protocol=${encodeURIComponent(protocol)}`, { method: 'DELETE' });
    // 204 has no body but is still ok=true
    if (!r.ok && r.status !== 404) {
      btn.disabled = false;
      return;
    }
  } catch (_) {
    btn.disabled = false;
    return;
  }
  hydratePorts();
}

// ── Port-panel hydration ─────────────────────────────────────────────────────
// Project groups are server-rendered; the .ports-panel inside each is filled
// in here from the host daemon. Re-runs after every refreshGroups() swap.
let _portCache = null;  // last successful fetch, used while a request is in-flight

// Single global ResizeObserver: scales each iframe so its rendered footprint
// fills the card. The iframe always renders at a 1280×800 virtual desktop
// viewport (so pages look like a real desktop view, not a cramped mobile
// rendering) and we shrink it via transform to fit whatever width the grid
// hands the card. Single RO instance to avoid per-card allocation churn.
const PREVIEW_VIEWPORT_W = 1280;
const _previewRO = new ResizeObserver(entries => {
  for (const entry of entries) {
    const w = entry.contentRect.width;
    if (w <= 0) continue;
    const iframe = entry.target.querySelector('.port-preview-iframe');
    if (iframe) iframe.style.transform = `scale(${w / PREVIEW_VIEWPORT_W})`;
  }
});
async function hydratePorts() {
  let entries;
  try {
    const r = await hdFetch('/ports');
    if (!r.ok) throw new Error('bad status');
    entries = await r.json();
    _portCache = entries;
  } catch (_) {
    // Daemon down or unreachable. Render an explicit "down" banner so users
    // don't think the feature is just broken. Tag the panel state so the
    // next successful fetch always re-renders (clearing this banner).
    document.querySelectorAll('.ports-panel').forEach(panel => {
      panel.innerHTML = `<div class="ports-empty ports-down">host daemon unreachable</div>`;
      panel.dataset.portsKey = '__down__';
    });
    return;
  }
  const byProject = {};
  for (const e of entries) {
    (byProject[e.project] = byProject[e.project] || []).push(e);
  }
  document.querySelectorAll('.ports-panel').forEach(panel => {
    const project = panel.dataset.project;
    const list = byProject[project] || [];
    list.sort((a, b) => a.port - b.port);

    // Skip the innerHTML swap (and the iframe teardown that comes with it) if
    // the visible port set is unchanged. Without this, every 5s the polling
    // refresh would nuke every iframe and force every previewed app to reload.
    const key = list.map(p => `${p.port}/${p.protocol}`).join(',') || '__empty__';
    if (panel.dataset.portsKey === key) return;
    panel.dataset.portsKey = key;

    if (list.length === 0) {
      panel.innerHTML = '';  // collapse to nothing when no ports — saves vertical space
      return;
    }

    panel.innerHTML = `
      <div class="ports-header"><span class="ports-label">exposed ports</span></div>
      <div class="ports-list">
        ${list.map(p => {
          const url = `${location.protocol}//${location.hostname}:${p.port}`;
          return `
          <div class="port-preview">
            <a class="port-preview-frame" href="${url}" onclick="openApp('${url}'); return false;"
               title="Open :${p.port}">
              <iframe class="port-preview-iframe" src="${url}" loading="lazy"
                      referrerpolicy="no-referrer" tabindex="-1"
                      aria-hidden="true"></iframe>
            </a>
            <div class="port-preview-footer">
              <a class="port-link" href="${url}" onclick="openApp('${url}'); return false;">:${p.port}</a>
              <span class="port-proto">${p.protocol}</span>
              <span class="port-spacer"></span>
              <button type="button" class="port-remove" data-port="${p.port}" data-protocol="${p.protocol}"
                      title="Unpublish ${p.port}/${p.protocol}" onclick="removePort(this)">×</button>
            </div>
          </div>`;
        }).join('')}
      </div>`;

    // Hook each new preview frame up to the resize observer so the iframe's
    // transform scales with the card's actual width (not just the 320px
    // minimum from the grid template).
    panel.querySelectorAll('.port-preview-frame').forEach(frame => {
      _previewRO.observe(frame);
    });
  });
}

// ── Live refresh ─────────────────────────────────────────────────────────────
// Poll the groups partial and swap it in place — no full-page reload. Skip the
// swap if the user is currently typing somewhere, or a modal is open, so we
// never wipe out in-progress input.
const REFRESH_MS = 5000;
function shouldSkipRefresh() {
  // Block when a focused input has actual content — otherwise an empty-but-focused
  // field (e.g. after submitting "+ New Project" and reset()) would freeze polling.
  const a = document.activeElement;
  if (a && (a.tagName === 'INPUT' || a.tagName === 'TEXTAREA') && a.value && a.value.length > 0) return true;
  if (a && a.isContentEditable && a.textContent && a.textContent.length > 0) return true;
  for (const id of ['destroy-modal', 'port-modal']) {
    const m = document.getElementById(id);
    if (m && m.style.display !== 'none') return true;
  }
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
    if (!target) return;

    // Surgical swap: replace .project-header and .project-sessions in each
    // group, but leave the existing .ports-panel DOM node alone. Naively
    // setting target.innerHTML would destroy every preview iframe on every
    // poll tick (5s) — the browsing context of an iframe is torn down when
    // it leaves the DOM, so each tick would force every previewed app to
    // reload from scratch.
    const tmp = document.createElement('div');
    tmp.innerHTML = html;

    const current = {};
    target.querySelectorAll(':scope > .project-group').forEach(g => {
      current[g.dataset.project] = g;
    });

    const seen = new Set();
    const newGroups = tmp.querySelectorAll(':scope > .project-group');
    newGroups.forEach(newG => {
      const project = newG.dataset.project;
      seen.add(project);
      const old = current[project];
      if (!old) {
        target.appendChild(newG);  // new project: insert wholesale (empty ports-panel)
        return;
      }
      const newHeader = newG.querySelector(':scope > .project-header');
      const oldHeader = old.querySelector(':scope > .project-header');
      if (newHeader && oldHeader) oldHeader.replaceWith(newHeader);

      const newSessions = newG.querySelector(':scope > .project-sessions');
      const oldSessions = old.querySelector(':scope > .project-sessions');
      if (newSessions && oldSessions) oldSessions.replaceWith(newSessions);
      // ports-panel intentionally left untouched
    });

    Object.entries(current).forEach(([project, g]) => {
      if (!seen.has(project)) g.remove();
    });

    hydratePorts();
  } catch (_) { /* ignore transient network errors */ }
}
setInterval(refreshGroups, REFRESH_MS);
// Also refresh immediately when the tab regains focus
document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshGroups(); });

// Handle all /sessions form submissions in JS.
// Native target="_blank" POST forms break in Android PWA standalone mode —
// Chrome opens a Custom Tab that makes a GET instead of following the POST
// redirect, resulting in 405. Doing the POST via fetch and opening the result
// with window.open (called synchronously before the await to stay within the
// user-gesture context) works correctly on both desktop and mobile PWA.
document.addEventListener('submit', async (e) => {
  const form = e.target;
  const action = form.getAttribute('action') || '';

  if (action === '/sessions') {
    // Create / +Session: open terminal in new tab
    e.preventDefault();
    const win = window.open('', '_blank');  // synchronous — must stay within user-gesture context
    const body = new FormData(form);
    form.reset();
    try {
      const r = await fetch('/sessions', { method: 'POST', body });
      if (r.ok && win) win.location.href = r.url;
      else if (win) win.close();
    } catch (_) { if (win) win.close(); }
    [400, 1200, 2500].forEach(ms => setTimeout(refreshGroups, ms));

  } else if (action.endsWith('/kill')) {
    // Kill session: fetch in place, refresh dashboard without page reload
    e.preventDefault();
    try { await fetch(action, { method: 'POST' }); } catch (_) {}
    await refreshGroups();
  }
});

// Initial hydration on page load — the server rendered empty .ports-panel divs.
hydratePorts();
