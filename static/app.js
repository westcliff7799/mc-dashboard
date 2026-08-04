'use strict';

const $ = (id) => document.getElementById(id);

let capabilities = { rcon: false, agent: false, agent_configured: false };
let socket = null;
let backoff = 1000;
let currentDir = '';
let currentEntries = [];
let openFilePath = null;
let agentWritable = null;
const selection = new Set();

function toast(message, bad = false) {
  const el = $('toast');
  el.textContent = message;
  el.classList.toggle('bad', bad);
  el.classList.add('show');
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => el.classList.remove('show'), 4000);
}

function bytes(n) {
  if (!Number.isFinite(n)) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n < 10 && i > 0 ? n.toFixed(1) : Math.round(n)} ${units[i]}`;
}

function clock(ts) {
  return ts ? new Date(ts * 1000).toLocaleTimeString() : '';
}

function count(n) {
  if (!Number.isFinite(n)) return '—';
  return n >= 100000
    ? new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(n)
    : n.toLocaleString();
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (response.status === 401) { window.location.href = '/login'; throw new Error('auth'); }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
  return data;
}

document.querySelectorAll('.tabs button').forEach((button) => {
  button.addEventListener('click', () => {
    document.querySelectorAll('.tabs button')
      .forEach((b) => b.setAttribute('aria-selected', String(b === button)));
    document.querySelectorAll('.panel')
      .forEach((p) => p.classList.toggle('active', p.id === `tab-${button.dataset.tab}`));
    if (button.dataset.tab === 'files' && capabilities.agent) {
      loadFiles(currentDir);
      loadBackups();
    }
    if (button.dataset.tab === 'debug') {
      if (can('debug.pm2')) loadPm2();
      if (capabilities.role === 'admin') loadUsers();
    }
  });
});

const STATUS_LOOK = {
  online: ['good', '✓', 'Online'],
  unreachable: ['warning', '!', 'Unreachable'],
  stopped: ['critical', '✕', 'Stopped'],
  offline: ['critical', '✕', 'Offline'],
};

function describeFault(status, state) {
  const where = `${status.host}:${status.port}`;
  if (state === 'unreachable') {
    return `The agent reports the server running, but ${where} did not answer`
      + `${status.error ? ` (${status.error})` : ''}. `
      + 'That address is probably stale — check the tunnel on the host machine.';
  }
  if (state === 'stopped') return 'The agent reports the server process is stopped.';
  return status.error || '';
}

function renderStatus(status) {
  if (!status) return;

  const state = status.state || (status.online ? 'online' : 'offline');
  const [tone, icon, label] = STATUS_LOOK[state] || ['unknown', '•', 'Checking…'];
  const pill = $('status-pill');
  pill.className = `pill ${tone}`;
  $('status-icon').textContent = icon;
  $('status-text').textContent = label;

  $('checked-at').textContent = status.checked_at ? `checked ${clock(status.checked_at)}` : '';
  $('motd').textContent = status.online ? (status.motd || '') : describeFault(status, state);
  $('host').textContent = status.host
    ? `${status.host}:${status.port}${status.address_source === 'agent' ? ' · via agent' : ''}`
    : '—';

  $('t-players').textContent = status.online ? count(status.players_online) : '—';
  $('t-players-sub').textContent = status.online && status.players_max
    ? `of ${count(status.players_max)} slots` : '';
  $('t-version').textContent = status.version || '—';
  $('t-latency').textContent = status.latency_ms != null ? `${Math.round(status.latency_ms)} ms` : '—';

  const players = $('players');
  const names = status.players_sample || [];
  if (!status.online) {
    players.innerHTML = state === 'unreachable'
      ? '<span class="empty">Roster needs a working address — the server itself is up.</span>'
      : '<span class="empty">Server is offline.</span>';
  } else if (names.length === 0) {
    players.innerHTML = status.players_online > 0
      ? '<span class="empty">Names hidden — enable RCON for the full roster.</span>'
      : '<span class="empty">Nobody online.</span>';
  } else {
    players.innerHTML = '';
    names.forEach((name) => {
      const span = document.createElement('span');
      span.className = 'player';
      span.textContent = name;
      players.appendChild(span);
    });
  }

  if (status.agent_state) renderAgentState(status.agent_state);
}

function renderAgentState(state) {
  if (!state || !Object.keys(state).length) {
    agentWritable = null;
    $('t-process').textContent = '—';
    $('t-process-sub').textContent = capabilities.agent ? '' : 'agent offline';
    updateFileControls();
    return;
  }
  if (state.writable !== undefined) {
    agentWritable = Boolean(state.writable);
    updateFileControls();
  }
  $('t-process').textContent = state.running ? 'Running' : 'Stopped';
  const bits = [];
  if (state.mode) bits.push(state.mode);
  if (state.cpu) bits.push(`cpu ${state.cpu}`);
  if (state.memory) bits.push(state.memory);
  if (state.pid && state.pid !== '0') bits.push(`pid ${state.pid}`);
  $('t-process-sub').textContent = bits.join(' · ');
}

function can(permission) {
  return (capabilities.permissions || []).includes(permission);
}

function setTabVisible(name, visible) {
  document.querySelectorAll(`.tabs [data-tab="${name}"]`).forEach((tab) => { tab.hidden = !visible; });
  const panel = $(`tab-${name}`);
  if (panel && !visible) { panel.hidden = true; panel.classList.remove('active'); }
  else if (panel) { panel.hidden = false; }
}

function applyCapabilities() {
  const agent = capabilities.agent;

  const seesConsole = can('console.view') || can('console.command');
  const seesFiles = can('files.browse') || can('files.read') || can('files.write')
    || can('files.delete') || can('backup.list') || can('backup.create');
  const seesDebug = can('debug.pm2') || capabilities.role === 'admin';
  setTabVisible('console', seesConsole);
  setTabVisible('files', seesFiles);
  setTabVisible('debug', seesDebug);

  const userCard = $('user-list') && $('user-list').closest('.card');
  if (userCard) userCard.hidden = capabilities.role !== 'admin';

  if (!document.querySelector('.panel.active:not([hidden])')) {
    document.querySelectorAll('.tabs button').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === 'overview')));
    const overview = $('tab-overview');
    if (overview) { overview.hidden = false; overview.classList.add('active'); }
  }

  const power = $('power-card') || $('btn-start').closest('.card');
  const anyPower = can('power.start') || can('power.stop') || can('power.restart');
  if (power) power.hidden = !anyPower && !can('backup.create');

  $('btn-start').disabled = !agent || !can('power.start');
  $('btn-stop').disabled = !agent || !can('power.stop');
  $('btn-restart').disabled = !agent || !can('power.restart');
  $('btn-backup').disabled = !agent || !can('backup.create');
  $('power-unavailable').hidden = agent;
  $('files-notice').hidden = agent;

  const notice = $('console-notice');
  if (!capabilities.rcon && !agent) {
    notice.hidden = false;
    notice.innerHTML = '<span aria-hidden="true">⚠</span><span><strong>Read-only.</strong> ' +
      'Enable RCON to send commands, or connect the agent to stream the live console.</span>';
  } else if (!capabilities.rcon) {
    notice.hidden = false;
    notice.innerHTML = '<span aria-hidden="true">⚠</span><span><strong>RCON is off.</strong> ' +
      'The console below is live, but commands cannot be sent until RCON is enabled.</span>';
  } else if (!agent) {
    notice.hidden = false;
    notice.innerHTML = '<span aria-hidden="true">⚠</span><span><strong>Agent offline.</strong> ' +
      'Commands work over RCON, but the live console feed needs the agent.</span>';
  } else {
    notice.hidden = true;
  }

  $('cmd').disabled = !capabilities.rcon || !can('console.command');
  $('cmd-send').disabled = !capabilities.rcon || !can('console.command');

  $('btn-file-upload').hidden = !can('files.write');
  $('btn-file-upload-folder').hidden = !can('files.write');
  $('btn-file-folder').hidden = !can('files.write');
  $('btn-file-new').hidden = !can('files.write');
  $('btn-file-rename').hidden = !can('files.write');
  $('btn-file-delete').hidden = !can('files.delete');
  $('btn-file-unzip').hidden = !can('files.write');
  $('btn-file-download').hidden = !can('files.read');
  updateFileControls();

  if (!agent) renderAgentState(null);
}

const consoleEl = $('console');

function appendLine(entry) {
  const pinned = consoleEl.scrollHeight - consoleEl.scrollTop - consoleEl.clientHeight < 40;

  const line = document.createElement('span');
  let kind = entry.kind || '';
  if (!kind) {
    if (/\bERROR\b|Exception|\bFATAL\b/.test(entry.line)) kind = 'err';
    else if (/\bWARN\b/.test(entry.line)) kind = 'warn';
  }
  line.className = `l ${kind}`.trim();
  line.textContent = entry.line;
  consoleEl.appendChild(line);

  while (consoleEl.childElementCount > 1000) consoleEl.removeChild(consoleEl.firstChild);
  if (pinned) consoleEl.scrollTop = consoleEl.scrollHeight;
}

$('cmdform').addEventListener('submit', async (event) => {
  event.preventDefault();
  const input = $('cmd');
  const command = input.value.trim();
  if (!command) return;
  input.value = '';
  try {
    await api('/api/command', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command }),
    });
  } catch (error) {
    appendLine({ line: `! ${error.message}`, kind: 'err' });
  }
});

async function power(action) {
  if (action !== 'start' &&
      !confirm(`${action === 'stop' ? 'Stop' : 'Restart'} the server? Players will be disconnected.`)) {
    return;
  }
  const buttons = ['btn-start', 'btn-restart', 'btn-stop'];
  buttons.forEach((id) => { $(id).disabled = true; });
  try {
    const result = await api('/api/power', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action }),
    });
    toast(result.ok ? `${action}: ${result.detail || 'done'}` : `Failed: ${result.error || result.detail}`,
          !result.ok);
    if (result.state) renderAgentState(result.state);
  } catch (error) {
    toast(error.message, true);
  } finally {
    buttons.forEach((id) => { $(id).disabled = !capabilities.agent; });
  }
}

$('btn-start').addEventListener('click', () => power('start'));
$('btn-stop').addEventListener('click', () => power('stop'));
$('btn-restart').addEventListener('click', () => power('restart'));

const JSON_HEADERS = { 'Content-Type': 'application/json' };

function post(path, body) {
  return api(path, { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(body) });
}

function mayWriteFiles() {
  return can('files.write') && capabilities.agent && agentWritable !== false;
}

function mayDeleteFiles() {
  return can('files.delete') && capabilities.agent && agentWritable !== false;
}

function selectable() {
  return can('files.write') || can('files.delete') || can('files.read');
}

function selectedEntries() {
  return currentEntries.filter((entry) => selection.has(entry.path));
}

async function loadFiles(path) {
  if (path !== currentDir) closeEditor();
  const table = $('file-table');
  try {
    const data = await api(`/api/files?path=${encodeURIComponent(path)}`);
    currentDir = data.path || '';
    currentEntries = data.entries || [];
    selection.clear();
    renderCrumbs(currentDir);
    $('disk-free').textContent = data.disk_free ? `${bytes(data.disk_free)} free` : '';
    renderFileTable();
  } catch (error) {
    table.textContent = '';
    currentEntries = [];
    selection.clear();
    updateFileControls();
    if (capabilities.agent) toast(error.message, true);
  }
}

function syncSelectAll() {
  const all = $('select-all');
  if (all) all.checked = currentEntries.length > 0 && selection.size === currentEntries.length;
}

function fileHeader() {
  const tr = document.createElement('tr');
  if (selectable()) {
    const cell = document.createElement('th');
    cell.className = 'sel';
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.id = 'select-all';
    box.setAttribute('aria-label', 'Select everything in this folder');
    box.addEventListener('change', () => {
      selection.clear();
      if (box.checked) currentEntries.forEach((entry) => selection.add(entry.path));
      renderFileTable();
    });
    cell.appendChild(box);
    tr.appendChild(cell);
  }
  ['Name', 'Size', 'Modified'].forEach((label, index) => {
    const cell = document.createElement('th');
    cell.textContent = label;
    if (index > 0) cell.className = 'num';
    tr.appendChild(cell);
  });
  return tr;
}

function fileRow(entry) {
  const tr = document.createElement('tr');
  tr.classList.toggle('selected', selection.has(entry.path));

  if (selectable()) {
    const cell = document.createElement('td');
    cell.className = 'sel';
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.checked = selection.has(entry.path);
    box.setAttribute('aria-label', `Select ${entry.name}`);
    box.addEventListener('change', () => {
      if (box.checked) selection.add(entry.path);
      else selection.delete(entry.path);
      tr.classList.toggle('selected', box.checked);
      syncSelectAll();
      updateFileControls();
    });
    cell.appendChild(box);
    tr.appendChild(cell);
  }

  const name = document.createElement('td');
  name.className = 'name';
  name.textContent = `${entry.dir ? '📁' : '📄'} ${entry.name}`;
  if (entry.link) {
    const marker = document.createElement('span');
    marker.className = 'link';
    marker.textContent = ' (link)';
    name.appendChild(marker);
  }
  if (entry.dir || can('files.read')) {
    name.classList.add('open');
    name.addEventListener('click', () => (entry.dir ? loadFiles(entry.path) : openFile(entry.path)));
  }

  const size = document.createElement('td');
  size.className = 'num';
  size.textContent = entry.dir ? '' : bytes(entry.size);

  const modified = document.createElement('td');
  modified.className = 'num';
  modified.textContent = new Date(entry.modified * 1000).toLocaleString();

  tr.append(name, size, modified);
  return tr;
}

function renderFileTable() {
  const table = $('file-table');
  const columns = selectable() ? 4 : 3;
  table.textContent = '';
  table.appendChild(fileHeader());

  if (currentDir) {
    const up = document.createElement('tr');
    up.className = 'clickable';
    const cell = document.createElement('td');
    cell.className = 'name';
    cell.colSpan = columns;
    cell.textContent = '📁 ..';
    up.appendChild(cell);
    up.addEventListener('click', () => loadFiles(currentDir.split('/').slice(0, -1).join('/')));
    table.appendChild(up);
  }

  if (!currentEntries.length) {
    const empty = document.createElement('tr');
    const cell = document.createElement('td');
    cell.className = 'empty';
    cell.colSpan = columns;
    cell.textContent = 'This folder is empty.';
    empty.appendChild(cell);
    table.appendChild(empty);
  }

  currentEntries.forEach((entry) => table.appendChild(fileRow(entry)));
  syncSelectAll();
  updateFileControls();
}

function updateFileControls() {
  const agent = capabilities.agent;
  const write = mayWriteFiles();
  const chosen = selectedEntries();
  const single = chosen.length === 1 ? chosen[0] : null;

  $('btn-file-refresh').disabled = !agent;
  $('btn-file-upload').disabled = !write;
  $('btn-file-upload-folder').disabled = !write;
  $('btn-file-folder').disabled = !write;
  $('btn-file-new').disabled = !write;
  $('btn-file-rename').disabled = !write || !single;
  $('btn-file-delete').disabled = !mayDeleteFiles() || chosen.length === 0;
  $('btn-file-unzip').disabled = !write || !isArchive(single);
  $('btn-file-download').disabled = !can('files.read') || !agent || !single || single.dir;

  const blocked = can('files.write') || can('files.delete');
  $('files-readonly').hidden = !(agent && agentWritable === false && blocked);
}

function renderCrumbs(path) {
  const crumbs = $('crumbs');
  crumbs.textContent = '';
  const root = document.createElement('a');
  root.textContent = 'server';
  root.addEventListener('click', () => loadFiles(''));
  crumbs.appendChild(root);

  let accumulated = '';
  (path ? path.split('/') : []).forEach((part) => {
    accumulated = accumulated ? `${accumulated}/${part}` : part;
    const target = accumulated;
    crumbs.append(' / ');
    const link = document.createElement('a');
    link.textContent = part;
    link.addEventListener('click', () => loadFiles(target));
    crumbs.appendChild(link);
  });
}

function closeEditor() {
  openFilePath = null;
  $('editor').hidden = true;
  $('editor-text').value = '';
}

async function openFile(path) {
  try {
    const data = await api(`/api/files/read?path=${encodeURIComponent(path)}`);
    openFilePath = data.path || path;
    const text = $('editor-text');
    text.value = data.content;
    text.readOnly = !mayWriteFiles();
    $('editor-path').textContent = openFilePath;
    $('editor-note').textContent = text.readOnly ? 'read-only' : bytes(data.size);
    $('btn-editor-save').hidden = text.readOnly;
    $('editor').hidden = false;
    $('editor').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } catch (error) {
    toast(error.message, true);
  }
}

async function saveFile() {
  if (!openFilePath) return;
  const button = $('btn-editor-save');
  button.disabled = true;
  try {
    const result = await post('/api/files/write', {
      path: openFilePath,
      content: $('editor-text').value,
    });
    $('editor-note').textContent = bytes(result.size);
    toast(`Saved ${result.path}`);
    loadFiles(currentDir);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function newFolder() {
  const name = (prompt('Name for the new folder') || '').trim();
  if (!name) return;
  try {
    await post('/api/files/folder', { path: currentDir, name });
    toast(`Created ${name}`);
    loadFiles(currentDir);
  } catch (error) {
    toast(error.message, true);
  }
}

async function newFile() {
  const name = (prompt('Name for the new file') || '').trim();
  if (!name) return;
  const target = currentDir ? `${currentDir}/${name}` : name;
  try {
    await post('/api/files/write', { path: target, content: '', overwrite: false });
    await loadFiles(currentDir);
    if (can('files.read')) openFile(target);
  } catch (error) {
    toast(error.message, true);
  }
}

async function renameSelected() {
  const [entry] = selectedEntries();
  if (!entry) return;
  const next = (prompt(`Rename ${entry.name} to`, entry.name) || '').trim();
  if (!next || next === entry.name) return;
  try {
    const result = await post('/api/files/rename', { path: entry.path, to: next });
    toast(`Renamed to ${result.path}`);
    if (openFilePath === entry.path) closeEditor();
    loadFiles(currentDir);
  } catch (error) {
    toast(error.message, true);
  }
}

async function deleteSelected() {
  const chosen = selectedEntries();
  if (!chosen.length) return;
  const label = chosen.length === 1 ? chosen[0].name : `${chosen.length} items`;
  const folders = chosen.some((entry) => entry.dir);
  const warning = folders ? '\n\nFolders go with everything inside them.' : '';
  if (!confirm(`Delete ${label}?${warning}\n\nThis cannot be undone.`)) return;

  try {
    const result = await post('/api/files/delete', { paths: chosen.map((entry) => entry.path) });
    const failed = result.failed || [];
    if (failed.length) toast(`${failed[0].path}: ${failed[0].error}`, true);
    else toast(`Deleted ${label}`);
    if (chosen.some((entry) => entry.path === openFilePath)) closeEditor();
    loadFiles(currentDir);
  } catch (error) {
    toast(error.message, true);
  }
}

function isArchive(entry) {
  return Boolean(entry) && !entry.dir && /\.zip$/i.test(entry.name);
}

async function unzipSelected() {
  const [entry] = selectedEntries();
  if (!isArchive(entry)) return;

  const stem = entry.name.replace(/\.zip$/i, '');
  const name = (prompt('Unzip into which folder?', stem) || '').trim();
  if (!name) return;

  const button = $('btn-file-unzip');
  button.disabled = true;
  try {
    const result = await post('/api/files/extract', {
      path: entry.path,
      to: joinPath(currentDir, name),
    });
    toast(`Unzipped ${result.files} files into ${result.path}`);
    loadFiles(currentDir);
  } catch (error) {
    toast(error.message, true);
  } finally {
    updateFileControls();
  }
}

function downloadSelected() {
  const [entry] = selectedEntries();
  if (!entry || entry.dir) return;
  const link = document.createElement('a');
  link.href = `/api/files/download?path=${encodeURIComponent(entry.path)}`;
  link.download = entry.name;
  document.body.appendChild(link);
  link.click();
  link.remove();
}

const MAX_FOLDER_DEPTH = 32;

function joinPath(base, extra) {
  if (!extra) return base;
  return base ? `${base}/${extra}` : extra;
}

function writeBlocker() {
  if (!can('files.write')) return 'You do not have permission to change files.';
  if (!capabilities.agent) return 'The agent is not connected.';
  if (agentWritable === false) return 'The agent is running read-only.';
  return '';
}

function readBatch(reader) {
  return new Promise((resolve, reject) => reader.readEntries(resolve, reject));
}

function readEntryFile(entry) {
  return new Promise((resolve, reject) => entry.file(resolve, reject));
}

async function walkEntry(entry, prefix, found) {
  if (entry.isFile) {
    found.files.push({ file: await readEntryFile(entry), dir: prefix });
    return;
  }
  if (!entry.isDirectory) return;

  const dir = joinPath(prefix, entry.name);
  if (dir.split('/').length > MAX_FOLDER_DEPTH) throw new Error(`${dir}: nested too deeply`);
  found.dirs.add(dir);

  const reader = entry.createReader();
  for (;;) {
    const batch = await readBatch(reader);
    if (!batch.length) break;
    for (const child of batch) await walkEntry(child, dir, found);
  }
}

async function droppedItems(transfer) {
  const roots = Array.from(transfer.items || [])
    .map((item) => (item.webkitGetAsEntry ? item.webkitGetAsEntry() : null))
    .filter(Boolean);

  const found = { files: [], dirs: new Set() };
  if (!roots.length) {
    Array.from(transfer.files || []).forEach((file) => found.files.push({ file, dir: '' }));
    return found;
  }
  for (const root of roots) await walkEntry(root, '', found);
  return found;
}

function pickedItems(list) {
  const found = { files: [], dirs: new Set() };
  Array.from(list || []).forEach((file) => {
    const relative = (file.webkitRelativePath || '').replace(/\\/g, '/');
    const cut = relative.lastIndexOf('/');
    found.files.push({ file, dir: cut > 0 ? relative.slice(0, cut) : '' });
  });
  return found;
}

const queue = { running: false, paused: false, stopped: false, request: null, wake: null };

function renderQueueControls() {
  $('btn-upload-pause').textContent = queue.paused ? 'Resume' : 'Pause';
  $('btn-upload-pause').disabled = queue.stopped;
  $('btn-upload-stop').disabled = queue.stopped;
}

function pauseUploads() {
  if (!queue.running || queue.stopped) return;
  queue.paused = !queue.paused;
  if (!queue.paused && queue.wake) {
    const wake = queue.wake;
    queue.wake = null;
    wake();
  }
  renderQueueControls();
}

function stopUploads() {
  if (!queue.running || queue.stopped) return;
  queue.stopped = true;
  queue.paused = false;
  if (queue.request) queue.request.abort();
  if (queue.wake) {
    const wake = queue.wake;
    queue.wake = null;
    wake();
  }
  renderQueueControls();
}

async function waitWhilePaused(remaining) {
  while (queue.paused && !queue.stopped) {
    $('upload-name').textContent = `Paused — ${remaining} left`;
    await new Promise((resolve) => { queue.wake = resolve; });
  }
}

function uploadOne(item, overwrite) {
  return new Promise((resolve, reject) => {
    const label = joinPath(item.dir, item.file.name);
    const form = new FormData();
    form.append('path', joinPath(currentDir, item.dir));
    form.append('overwrite', overwrite ? 'true' : 'false');
    form.append('upload', item.file, item.file.name);

    const request = new XMLHttpRequest();
    queue.request = request;
    request.open('POST', '/api/files/upload');
    request.upload.addEventListener('progress', (event) => {
      if (event.lengthComputable) $('upload-bar').value = (event.loaded / event.total) * 100;
    });
    request.addEventListener('load', () => {
      let data = {};
      try { data = JSON.parse(request.responseText); } catch { }
      if (request.status === 401) { window.location.href = '/login'; return; }
      if (request.status >= 200 && request.status < 300) resolve(data);
      else reject(new Error(`${label}: ${data.detail || `HTTP ${request.status}`}`));
    });
    request.addEventListener('error', () => reject(new Error(`${label}: the upload failed`)));
    request.addEventListener('abort', () => reject(new Error(`${label}: upload cancelled`)));
    request.send(form);
  }).finally(() => { queue.request = null; });
}

async function uploadAll(found) {
  const items = found.files;
  const folders = new Set(found.dirs);
  items.forEach((item) => { if (item.dir) folders.add(item.dir); });
  if (!items.length && !folders.size) return;

  const blocker = writeBlocker();
  if (blocker) { toast(blocker, true); return; }
  if (queue.running) { toast('An upload is already running.', true); return; }

  const here = new Set(currentEntries.map((entry) => entry.name));
  const top = (item) => (item.dir || item.file.name).split('/')[0];
  const roots = new Set([...items.map(top), ...[...folders].map((dir) => dir.split('/')[0])]);
  const clashes = [...roots].filter((name) => here.has(name));
  if (clashes.length
    && !confirm(`${clashes.join(', ')} already exists here.\n\nContinue? Matching files are replaced.`)) {
    return;
  }
  const replacing = new Set(clashes);

  queue.running = true;
  queue.paused = false;
  queue.stopped = false;
  queue.request = null;
  queue.wake = null;
  renderQueueControls();

  let done = 0;
  $('upload-row').hidden = false;
  try {
    for (const folder of [...folders].sort()) {
      if (queue.stopped) break;
      await waitWhilePaused(`${folders.size} folders`);
      if (queue.stopped) break;
      $('upload-name').textContent = folder;
      $('upload-bar').value = 0;
      await post('/api/files/folders', { path: joinPath(currentDir, folder) });
    }
    for (const [index, item] of items.entries()) {
      if (queue.stopped) break;
      await waitWhilePaused(`${items.length - index} of ${items.length}`);
      if (queue.stopped) break;

      const label = joinPath(item.dir, item.file.name);
      $('upload-name').textContent = items.length > 1
        ? `${label} — ${index + 1} of ${items.length}`
        : label;
      $('upload-bar').value = 0;
      await uploadOne(item, replacing.has(top(item)));
      done += 1;
    }

    if (queue.stopped) toast(`Stopped — ${done} of ${items.length} files uploaded`);
    else if (items.length === 1) toast(`Uploaded ${joinPath(items[0].dir, items[0].file.name)}`);
    else if (items.length) toast(`Uploaded ${items.length} files`);
    else toast(`Created ${[...folders].sort()[0]}`);
  } catch (error) {
    if (queue.stopped) toast(`Stopped — ${done} of ${items.length} files uploaded`);
    else toast(error.message, true);
  } finally {
    queue.running = false;
    queue.paused = false;
    queue.request = null;
    queue.wake = null;
    $('upload-row').hidden = true;
    $('file-input').value = '';
    $('folder-input').value = '';
    loadFiles(currentDir);
  }
}

$('btn-file-refresh').addEventListener('click', () => loadFiles(currentDir));
$('btn-file-upload').addEventListener('click', () => $('file-input').click());
$('btn-file-upload-folder').addEventListener('click', () => $('folder-input').click());
$('file-input').addEventListener('change', (event) => uploadAll(pickedItems(event.target.files)));
$('folder-input').addEventListener('change', (event) => uploadAll(pickedItems(event.target.files)));
$('btn-file-folder').addEventListener('click', newFolder);
$('btn-file-new').addEventListener('click', newFile);
$('btn-file-rename').addEventListener('click', renameSelected);
$('btn-file-delete').addEventListener('click', deleteSelected);
$('btn-upload-pause').addEventListener('click', pauseUploads);
$('btn-upload-stop').addEventListener('click', stopUploads);
$('btn-file-unzip').addEventListener('click', unzipSelected);
$('btn-file-download').addEventListener('click', downloadSelected);
$('btn-editor-close').addEventListener('click', closeEditor);
$('btn-editor-save').addEventListener('click', saveFile);

$('editor-text').addEventListener('keydown', (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === 's') {
    event.preventDefault();
    if (mayWriteFiles()) saveFile();
  }
});

const dropzone = $('dropzone');

dropzone.addEventListener('dragover', (event) => {
  if (!mayWriteFiles()) return;
  event.preventDefault();
  dropzone.classList.add('over');
});

dropzone.addEventListener('dragleave', (event) => {
  if (!dropzone.contains(event.relatedTarget)) dropzone.classList.remove('over');
});

dropzone.addEventListener('drop', (event) => {
  dropzone.classList.remove('over');
  if (!mayWriteFiles()) return;
  event.preventDefault();
  droppedItems(event.dataTransfer)
    .then(uploadAll)
    .catch((error) => toast(error.message, true));
});

async function loadBackups() {
  const table = $('backup-table');
  try {
    const data = await api('/api/backups');
    table.innerHTML = '<tr><th>Archive</th><th class="num">Size</th><th class="num">Created</th></tr>';
    if (!(data.backups || []).length) {
      table.innerHTML += '<tr><td colspan="3" class="empty">No backups yet.</td></tr>';
      return;
    }
    data.backups.forEach((backup) => {
      const tr = document.createElement('tr');
      const name = document.createElement('td');
      name.className = 'name';
      name.textContent = backup.name;
      const size = document.createElement('td');
      size.className = 'num';
      size.textContent = bytes(backup.size);
      const time = document.createElement('td');
      time.className = 'num';
      time.textContent = new Date(backup.modified * 1000).toLocaleString();
      tr.append(name, size, time);
      table.appendChild(tr);
    });
  } catch {
    table.innerHTML = '';
  }
}

$('btn-backup').addEventListener('click', async () => {
  const button = $('btn-backup');
  button.disabled = true;
  button.textContent = 'Backing up…';
  try {
    const result = await api('/api/backup', { method: 'POST' });
    toast(result.ok ? `Saved ${result.archive} (${bytes(result.size)})` : `Failed: ${result.error}`,
          !result.ok);
    loadBackups();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = !capabilities.agent;
    button.textContent = 'Back up world now';
  }
});

function connect() {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  socket = new WebSocket(`${scheme}://${location.host}/ws`);

  socket.addEventListener('open', () => {
    backoff = 1000;
    $('link-state').textContent = 'live';
    clearInterval(connect._ping);
    connect._ping = setInterval(() => {
      if (socket.readyState === WebSocket.OPEN) socket.send('ping');
    }, 25000);
  });

  socket.addEventListener('message', (event) => {
    const message = JSON.parse(event.data);
    switch (message.t) {
      case 'status':  renderStatus(message.status); break;
      case 'state':   renderAgentState(message.state); break;
      case 'log':     appendLine(message); break;
      case 'backlog':
        consoleEl.innerHTML = '';
        (message.lines || []).forEach(appendLine);
        break;
      case 'agent':
        capabilities.agent = message.connected;
        applyCapabilities();
        toast(message.connected ? 'Agent connected' : 'Agent disconnected', !message.connected);
        break;
    }
  });

  socket.addEventListener('close', () => {
    $('link-state').textContent = 'reconnecting…';
    clearInterval(connect._ping);
    setTimeout(connect, backoff);
    backoff = Math.min(backoff * 2, 30000);
  });

  socket.addEventListener('error', () => socket.close());
}

function duration(seconds) {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

const PM2_COLUMNS = [
  ['id', 3, 'right'],
  ['name', 24, 'left'],
  ['status', 9, 'left'],
  ['↺', 6, 'right'],
  ['cpu', 6, 'right'],
  ['memory', 10, 'right'],
  ['uptime', 8, 'right'],
];
const PM2_STATUS_INDEX = 2;

function fit(value, width, align) {
  let text = value == null ? '—' : String(value);
  if (text.length > width) text = `${text.slice(0, width - 1)}…`;
  return align === 'right' ? text.padStart(width) : text.padEnd(width);
}

function pm2Rule(left, middle, right) {
  return left + PM2_COLUMNS.map(([, width]) => '─'.repeat(width + 2)).join(middle) + right + '\n';
}

function pm2Row(target, values, colourStatus) {
  target.appendChild(document.createTextNode('│ '));
  PM2_COLUMNS.forEach(([, width, align], index) => {
    const text = fit(values[index], width, align);
    if (colourStatus && index === PM2_STATUS_INDEX) {
      const span = document.createElement('span');
      const status = String(values[index]);
      span.className = `st ${status === 'online' ? 'ok' : status === 'errored' ? 'bad' : 'busy'}`;
      span.textContent = text;
      target.appendChild(span);
    } else {
      target.appendChild(document.createTextNode(text));
    }
    target.appendChild(document.createTextNode(index === PM2_COLUMNS.length - 1 ? ' │\n' : ' │ '));
  });
}

async function loadPm2() {
  const out = $('pm2-out');
  let data;
  try {
    data = await api('/api/admin/pm2');
  } catch (error) {
    out.classList.add('err');
    out.textContent = `error: ${error.message}`;
    return;
  }
  out.classList.remove('err');
  out.textContent = '';

  const processes = data.processes || [];
  if (!processes.length) {
    out.textContent = 'pm2 is running but has no processes.';
    return;
  }

  out.appendChild(document.createTextNode(pm2Rule('┌', '┬', '┐')));
  pm2Row(out, PM2_COLUMNS.map(([label]) => label), false);
  out.appendChild(document.createTextNode(pm2Rule('├', '┼', '┤')));
  processes.forEach((process) => {
    pm2Row(out, [
      process.id,
      process.name,
      process.status,
      process.restarts,
      process.cpu == null ? null : `${process.cpu}%`,
      process.memory_mb == null ? null : `${process.memory_mb}mb`,
      process.uptime_seconds == null ? null : duration(process.uptime_seconds),
    ], true);
  });
  out.appendChild(document.createTextNode(pm2Rule('└', '┴', '┘')));
}

let catalogue = [];

function permGrid(container, held, onChange) {
  container.textContent = '';
  catalogue.forEach((group) => {
    const box = document.createElement('fieldset');
    box.className = 'perm-group';
    const legend = document.createElement('legend');
    legend.textContent = group.title;
    box.appendChild(legend);

    group.permissions.forEach((permission) => {
      const label = document.createElement('label');
      label.className = permission.danger ? 'perm danger' : 'perm';

      const input = document.createElement('input');
      input.type = 'checkbox';
      input.value = permission.key;
      input.checked = held.includes(permission.key);
      if (onChange) input.addEventListener('change', onChange);

      const text = document.createElement('span');
      const name = document.createElement('span');
      name.className = 'perm-label';
      name.textContent = permission.label;
      const detail = document.createElement('span');
      detail.className = 'perm-detail';
      detail.textContent = permission.detail;
      text.append(name, detail);

      label.append(input, text);
      box.appendChild(label);
    });
    container.appendChild(box);
  });
}

function readGrid(container) {
  return Array.from(container.querySelectorAll('input[type="checkbox"]:checked')).map((i) => i.value);
}

function userCard(user) {
  const card = document.createElement('div');
  card.className = 'user-card';

  const head = document.createElement('div');
  head.className = 'user-head';
  const name = document.createElement('span');
  name.className = 'name';
  name.textContent = user.username;
  const count = document.createElement('span');
  count.className = 'perm-count';

  const save = document.createElement('button');
  save.className = 'action primary';
  save.type = 'button';
  save.textContent = 'Save';
  save.disabled = true;

  const remove = document.createElement('button');
  remove.className = 'action danger';
  remove.type = 'button';
  remove.textContent = 'Remove';

  const spacer = document.createElement('span');
  spacer.style.flex = '1';
  head.append(name, count, spacer, save, remove);

  const grid = document.createElement('div');
  grid.className = 'perm-grid';

  const refreshCount = () => {
    const n = readGrid(grid).length;
    count.textContent = n === 0 ? 'overview only' : `${n} of ${totalPermissions()}`;
  };
  const markDirty = () => { save.disabled = false; refreshCount(); };

  permGrid(grid, user.permissions || [], markDirty);
  refreshCount();

  save.addEventListener('click', async () => {
    save.disabled = true;
    try {
      await api(`/api/admin/users/${encodeURIComponent(user.username)}/permissions`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ permissions: readGrid(grid) }),
      });
      toast(`Updated ${user.username}`);
      loadUsers();
    } catch (error) {
      toast(error.message, true);
      save.disabled = false;
    }
  });

  remove.addEventListener('click', async () => {
    if (!confirm(`Remove ${user.username}?`)) return;
    try {
      await api(`/api/admin/users/${encodeURIComponent(user.username)}`, { method: 'DELETE' });
      toast(`Removed ${user.username}`);
      loadUsers();
    } catch (error) {
      toast(error.message, true);
    }
  });

  card.append(head, grid);
  return card;
}

function totalPermissions() {
  return catalogue.reduce((n, group) => n + group.permissions.length, 0);
}

async function loadUsers() {
  const list = $('user-list');
  let data;
  try {
    data = await api('/api/admin/users');
  } catch (error) {
    toast(error.message, true);
    return;
  }

  catalogue = data.catalogue || [];
  permGrid($('new-user-perms'), []);
  list.textContent = '';

  const owner = document.createElement('div');
  owner.className = 'user-card owner';
  const ownerHead = document.createElement('div');
  ownerHead.className = 'user-head';
  const ownerName = document.createElement('span');
  ownerName.className = 'name';
  ownerName.textContent = data.admin;
  const ownerNote = document.createElement('span');
  ownerNote.className = 'perm-count';
  ownerNote.textContent = 'owner — everything, set in .env';
  ownerHead.append(ownerName, ownerNote);
  owner.appendChild(ownerHead);
  list.appendChild(owner);

  if (!(data.users || []).length) {
    const empty = document.createElement('p');
    empty.className = 'empty';
    empty.textContent = 'No other accounts yet.';
    list.appendChild(empty);
    return;
  }
  data.users.forEach((user) => list.appendChild(userCard(user)));
}

$('btn-pm2-refresh').addEventListener('click', loadPm2);

$('userform').addEventListener('submit', async (event) => {
  event.preventDefault();
  const username = $('new-username').value.trim();
  const password = $('new-password').value;
  try {
    await api('/api/admin/users', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password, permissions: readGrid($('new-user-perms')) }),
    });
    $('new-username').value = '';
    $('new-password').value = '';
    toast(`Added ${username}`);
    loadUsers();
  } catch (error) {
    toast(error.message, true);
  }
});

$('logout').addEventListener('click', async () => {
  await fetch('/api/logout', { method: 'POST' });
  window.location.href = '/login';
});

(async function init() {
  try {
    capabilities = await api('/api/capabilities');
    $('host').textContent = capabilities.host;
  } catch { }
  applyCapabilities();
  renderStatus(await api('/api/status').catch(() => null));
  connect();
})();
