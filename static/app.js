'use strict';

// ── State ──────────────────────────────────────────────────────────────────
let selectedCtx = '';
let selectedNs  = '';
let selectedPod = '';
let ws          = null;
let allLines    = [];   // every raw line received this session
let filter      = '';   // current search text
const MAX_DOM   = 5000; // max <div> lines kept in the DOM
let authToken   = sessionStorage.getItem('k8s_token') || '';

// ── DOM shortcuts ──────────────────────────────────────────────────────────
const $    = id => document.getElementById(id);
const logEl = () => $('log-output');

// ── String helpers ─────────────────────────────────────────────────────────
function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function stripAnsi(s) {
  return s.replace(/\x1b\[[0-9;]*[mGKHFJ]/g, '');
}

function escRe(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

// ── Log-line rendering ─────────────────────────────────────────────────────
function renderLine(raw, term) {
  const clean = esc(stripAnsi(raw));
  if (!term) return clean;
  try {
    return clean.replace(new RegExp(escRe(esc(term)), 'gi'),
      m => `<mark class="hl">${m}</mark>`);
  } catch {
    return clean;
  }
}

function levelClass(line) {
  const u = line.toUpperCase();
  if (/\b(FATAL|CRITICAL)\b/.test(u)) return 'll-fatal';
  if (/\b(ERROR|ERR)\b/.test(u))       return 'll-error';
  if (/\b(WARN(?:ING)?)\b/.test(u))    return 'll-warn';
  if (/\bINFO\b/.test(u))              return 'll-info';
  if (/\b(DEBUG|DEBU)\b/.test(u))      return 'll-debug';
  if (/\b(TRACE|TRAC)\b/.test(u))      return 'll-trace';
  return 'll-default';
}

// ── Status management ──────────────────────────────────────────────────────
function setStatus(s) {
  const labels = {
    idle:       '● idle',
    connecting: '● connecting…',
    streaming:  '● streaming',
    stopped:    '● stopped',
    done:       '● done',
    error:      '● error',
  };
  const badge = $('status-badge');
  badge.textContent = labels[s] || s;
  badge.className   = `status-badge ${s}`;

  const live = s === 'streaming';
  $('btn-connect').disabled = live || s === 'connecting';
  $('btn-stop').disabled    = !live;
}

// ── Line counter ───────────────────────────────────────────────────────────
function updateCounter() {
  const total = allLines.length;
  const vis   = logEl().querySelectorAll('.ll').length;
  $('line-counter').textContent = filter
    ? `${vis} / ${total} lines`
    : `${total} lines`;
}

// ── DOM log operations ─────────────────────────────────────────────────────
function makeLine(raw) {
  const el = document.createElement('div');
  el.className = `ll ${levelClass(raw)}`;
  el.innerHTML = renderLine(raw, filter);
  return el;
}

function appendLine(raw) {
  const out = logEl();
  out.appendChild(makeLine(raw));
  // Keep DOM lean
  while (out.children.length > MAX_DOM) out.removeChild(out.firstChild);
  if ($('autoscroll-chk').checked) out.scrollTop = out.scrollHeight;
}

function addSysLine(msg) {
  const el = document.createElement('div');
  el.className = 'll ll-system';
  el.textContent = `── ${msg} ──`;
  const out = logEl();
  out.appendChild(el);
  if ($('autoscroll-chk').checked) out.scrollTop = out.scrollHeight;
}

function rerender() {
  const term     = filter.toLowerCase();
  const filtered = term
    ? allLines.filter(l => l.toLowerCase().includes(term))
    : allLines;
  const slice = filtered.slice(-MAX_DOM);

  const frag = document.createDocumentFragment();
  for (const l of slice) frag.appendChild(makeLine(l));

  const out = logEl();
  out.innerHTML = '';
  out.appendChild(frag);
  updateCounter();
  if ($('autoscroll-chk').checked) out.scrollTop = out.scrollHeight;
}

function clearLogs() {
  allLines = [];
  logEl().innerHTML = '';
  updateCounter();
}

function applyFilter() {
  filter = $('search-inp').value;
  rerender();
}

// ── Auth ───────────────────────────────────────────────────────────────────
function initAuth() {
  if (authToken) {
    $('login-overlay').classList.add('hidden');
    loadContexts();
  }
  $('login-pass').addEventListener('keydown', e => {
    if (e.key === 'Enter') doLogin();
  });
  $('login-user').addEventListener('keydown', e => {
    if (e.key === 'Enter') $('login-pass').focus();
  });
}

async function doLogin() {
  const user  = $('login-user').value.trim();
  const pass  = $('login-pass').value;
  const errEl = $('login-error');
  const btn   = $('login-btn');
  errEl.style.display = 'none';
  btn.disabled = true;
  btn.textContent = 'Signing in…';
  try {
    const r = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: user, password: pass }),
    });
    if (!r.ok) throw new Error('Invalid username or password');
    const { token } = await r.json();
    authToken = token;
    sessionStorage.setItem('k8s_token', token);
    $('login-overlay').classList.add('hidden');
    loadContexts();
  } catch (e) {
    errEl.textContent = e.message;
    errEl.style.display = 'block';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Sign in';
  }
}

// ── API helpers ────────────────────────────────────────────────────────────
async function apiFetch(path) {
  const r = await fetch(path, {
    headers: { 'Authorization': `Bearer ${authToken}` },
  });
  if (r.status === 401) {
    authToken = '';
    sessionStorage.removeItem('k8s_token');
    $('login-overlay').classList.remove('hidden');
    throw new Error('Session expired – please log in again');
  }
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

// ── Context ────────────────────────────────────────────────────────────────
async function loadContexts() {
  try {
    const { contexts, active } = await apiFetch('/api/contexts');
    const sel = $('ctx-select');
    sel.innerHTML = '';
    for (const c of contexts) {
      const opt = new Option(c, c, c === active, c === active);
      sel.appendChild(opt);
    }
    if (contexts.length) {
      selectedCtx = sel.value;
      await loadNamespaces();
    }
  } catch (e) {
    $('ctx-select').innerHTML = `<option value="">Error: ${e.message}</option>`;
  }
}

async function onContextChange() {
  selectedCtx = $('ctx-select').value;
  selectedNs  = '';
  resetPodPanel();
  const ns = $('ns-select');
  ns.disabled = true;
  ns.innerHTML = '<option>Loading…</option>';
  await loadNamespaces();
}

// ── Namespaces ─────────────────────────────────────────────────────────────
async function loadNamespaces() {
  if (!selectedCtx) return;
  const sel = $('ns-select');
  try {
    const { namespaces } = await apiFetch(
      `/api/namespaces?context=${encodeURIComponent(selectedCtx)}`);
    sel.innerHTML = '<option value="">— select —</option>';
    for (const ns of namespaces) sel.appendChild(new Option(ns, ns));
    sel.disabled = false;
  } catch (e) {
    sel.innerHTML = `<option value="">Error: ${e.message}</option>`;
  }
}

async function onNsChange() {
  selectedNs  = $('ns-select').value;
  selectedPod = '';
  if (!selectedNs) return;
  resetPodPanel();
  await loadPods();
}

// ── Pods ───────────────────────────────────────────────────────────────────
async function loadPods() {
  if (!selectedCtx || !selectedNs) return;
  $('pod-list').innerHTML = '<div class="empty-msg">Loading…</div>';
  try {
    const { pods } = await apiFetch(
      `/api/pods?context=${encodeURIComponent(selectedCtx)}&namespace=${encodeURIComponent(selectedNs)}`);
    renderPods(pods);
  } catch (e) {
    $('pod-list').innerHTML =
      `<div class="empty-msg" style="color:var(--red)">Error: ${e.message}</div>`;
  }
}

function refreshPods() { loadPods(); }

function renderPods(pods) {
  $('pod-count').textContent = pods.length;
  if (!pods.length) {
    $('pod-list').innerHTML = '<div class="empty-msg">No pods found</div>';
    return;
  }

  const frag = document.createDocumentFragment();
  for (const pod of pods) {
    const dotCls = {
      Running: 'dot-running',
      Pending: 'dot-pending',
      Failed:  'dot-failed',
    }[pod.status] || 'dot-unknown';

    const restartHtml = pod.restarts > 0
      ? `<span class="restart-badge" title="${pod.restarts} restarts">↻${pod.restarts}</span>`
      : '';

    const el = document.createElement('div');
    el.className  = 'pod-item';
    el.dataset.pod = pod.name;
    el.innerHTML  = `
      <div class="pod-dot ${dotCls}"></div>
      <div class="pod-name" title="${esc(pod.name)}">${esc(pod.name)}</div>
      <div class="pod-meta">${esc(pod.ready)}${restartHtml}</div>`;
    el.onclick = () => selectPod(pod);
    frag.appendChild(el);
  }

  $('pod-list').innerHTML = '';
  $('pod-list').appendChild(frag);

  // Restore active highlight if pod is still present
  if (selectedPod) {
    $('pod-list').querySelector(`[data-pod="${selectedPod}"]`)
      ?.classList.add('active');
  }
}

function selectPod(pod) {
  selectedPod = pod.name;

  $('pod-label').textContent = `${selectedNs} / ${pod.name}`;

  // Highlight in list
  document.querySelectorAll('.pod-item').forEach(el => el.classList.remove('active'));
  $('pod-list').querySelector(`[data-pod="${pod.name}"]`)?.classList.add('active');

  // Container selector – only shown for multi-container pods
  const ctrSel   = $('ctr-select');
  const ctrField = $('ctr-field');
  ctrSel.innerHTML = '';

  if (pod.containers.length > 1) {
    for (const c of pod.containers) ctrSel.appendChild(new Option(c, c));
    // Append init containers with a prefix so they're distinguishable
    for (const c of (pod.init_containers || [])) {
      ctrSel.appendChild(new Option(`[init] ${c}`, c));
    }
    ctrField.style.display = 'block';
  } else {
    // Single container – pass its name explicitly so multi-ctr pods don't error
    if (pod.containers.length === 1) {
      ctrSel.appendChild(new Option(pod.containers[0], pod.containers[0]));
    }
    ctrField.style.display = 'none';
  }

  $('btn-connect').disabled = false;
  $('btn-dl').disabled      = false;
}

function resetPodPanel() {
  selectedPod = '';
  $('pod-list').innerHTML = '<div class="empty-msg">Select a namespace</div>';
  $('pod-count').textContent = '0';
  $('pod-label').textContent = 'No pod selected';
  $('btn-connect').disabled = true;
  $('btn-stop').disabled    = true;
  $('btn-dl').disabled      = true;
  $('ctr-field').style.display = 'none';
}

// ── WebSocket streaming ────────────────────────────────────────────────────
function startStream() {
  if (!selectedPod) return;
  if (ws) { ws.close(); ws = null; }

  clearLogs();
  setStatus('connecting');

  const params = new URLSearchParams({
    context:    selectedCtx,
    namespace:  selectedNs,
    pod:        selectedPod,
    tail_lines: $('tail-select').value,
    previous:   $('prev-chk').checked ? 'true' : 'false',
  });

  // Always send container – required for multi-container pods
  const ctr = $('ctr-select').value;
  if (ctr) params.append('container', ctr);
  params.append('token', authToken);

  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/ws/logs?${params}`);

  ws.onopen = () => {
    setStatus('streaming');
    addSysLine(`Connected – ${selectedPod}`);
  };

  ws.onmessage = ({ data }) => {
    let msg;
    try { msg = JSON.parse(data); } catch { return; }

    if (msg.t === 'log') {
      allLines.push(msg.line);
      const term = filter.toLowerCase();
      if (!term || msg.line.toLowerCase().includes(term)) appendLine(msg.line);
      updateCounter();
    } else if (msg.t === 'err') {
      addSysLine(`Error: ${msg.msg}`);
      setStatus('error');
    } else if (msg.t === 'done') {
      addSysLine('Stream ended – pod stopped or completed');
      setStatus('done');
    }
    // ignore ping
  };

  ws.onclose = () => {
    const cur = $('status-badge').className;
    if (cur.includes('streaming') || cur.includes('connecting')) {
      setStatus('stopped');
      addSysLine('Disconnected');
    }
  };

  ws.onerror = () => {
    setStatus('error');
    addSysLine('WebSocket error – check server connection');
  };
}

function stopStream() {
  if (ws) { ws.close(); ws = null; }
  setStatus('stopped');
}

// ── Download ───────────────────────────────────────────────────────────────
function downloadLogs() {
  if (!selectedPod) return;
  const params = new URLSearchParams({
    context:    selectedCtx,
    namespace:  selectedNs,
    pod:        selectedPod,
    tail_lines: 5000,
  });
  const ctr = $('ctr-select').value;
  if (ctr) params.append('container', ctr);
  if ($('prev-chk').checked) params.append('previous', 'true');
  params.append('token', authToken);
  window.open(`/api/logs/download?${params}`, '_blank');
}

// ── Init ───────────────────────────────────────────────────────────────────
initAuth();
