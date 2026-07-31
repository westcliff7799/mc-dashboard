'use strict';

const $ = (id) => document.getElementById(id);

let capabilities = { rcon: false, agent: false, agent_configured: false };
let socket = null;
let backoff = 1000;
let currentDir = '';

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
      loadPm2();
      loadUsers();
    }
  });
});

function renderStatus(status) {
  if (!status) return;

  const pill = $('status-pill');
  pill.className = `pill ${status.online ? 'good' : 'critical'}`;
  $('status-icon').textContent = status.online ? '✓' : '✕';
  $('status-text').textContent = status.online ? 'Online' : 'Offline';

  $('checked-at').textContent = status.checked_at ? `checked ${clock(status.checked_at)}` : '';
  $('motd').textContent = status.online ? (status.motd || '') : (status.error || '');
  $('host').textContent = `${status.host}:${status.port}`;

  $('t-players').textContent = status.online ? count(status.players_online) : '—';
  $('t-players-sub').textContent = status.online && status.players_max
    ? `of ${count(status.players_max)} slots` : '';
  $('t-version').textContent = status.version || '—';
  $('t-latency').textContent = status.latency_ms != null ? `${Math.round(status.latency_ms)} ms` : '—';

  const players = $('players');
  const names = status.players_sample || [];
  if (!status.online) {
    players.innerHTML = '<span class="empty">Server is offline.</span>';
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
    $('t-process').textContent = '—';
    $('t-process-sub').textContent = capabilities.agent ? '' : 'agent offline';
    return;
  }
  $('t-process').textContent = state.running ? 'Running' : 'Stopped';
  const bits = [];
  if (state.mode) bits.push(state.mode);
  if (state.cpu) bits.push(`cpu ${state.cpu}`);
  if (state.memory) bits.push(state.memory);
  if (state.pid && state.pid !== '0') bits.push(`pid ${state.pid}`);
  $('t-process-sub').textContent = bits.join(' · ');
}

function applyCapabilities() {
  const agent = capabilities.agent;

  if (capabilities.role === 'guest') {
    document.querySelectorAll('.tabs [data-tab="console"], .tabs [data-tab="files"], .tabs [data-tab="debug"]')
      .forEach((tab) => { tab.hidden = true; });
    ['tab-console', 'tab-files', 'tab-debug'].forEach((id) => {
      const panel = $(id);
      if (panel) { panel.hidden = true; panel.classList.remove('active'); }
    });
    const power = $('power-card') || $('btn-start').closest('.card');
    if (power) power.hidden = true;
    return;
  }

  ['btn-start', 'btn-restart', 'btn-stop', 'btn-backup'].forEach((id) => { $(id).disabled = !agent; });
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

  $('cmd').disabled = !capabilities.rcon;
  $('cmd-send').disabled = !capabilities.rcon;
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

async function loadFiles(path) {
  const table = $('file-table');
  try {
    const data = await api(`/api/files?path=${encodeURIComponent(path)}`);
    currentDir = data.path || '';
    renderCrumbs(currentDir);
    $('viewer').hidden = true;

    table.innerHTML = '<tr><th>Name</th><th class="num">Size</th><th class="num">Modified</th></tr>';
    if (currentDir) {
      const up = currentDir.split('/').slice(0, -1).join('/');
      table.appendChild(row('📁 ..', '', '', () => loadFiles(up)));
    }
    (data.entries || []).forEach((entry) => {
      table.appendChild(row(
        `${entry.dir ? '📁' : '📄'} ${entry.name}`,
        entry.dir ? '' : bytes(entry.size),
        new Date(entry.modified * 1000).toLocaleString(),
        entry.dir ? () => loadFiles(entry.path) : () => viewFile(entry.path),
      ));
    });
  } catch (error) {
    table.innerHTML = '';
    if (capabilities.agent) toast(error.message, true);
  }
}

function row(name, size, modified, onClick) {
  const tr = document.createElement('tr');
  tr.className = 'clickable';
  const nameCell = document.createElement('td');
  nameCell.className = 'name';
  nameCell.textContent = name;
  const sizeCell = document.createElement('td');
  sizeCell.className = 'num';
  sizeCell.textContent = size;
  const timeCell = document.createElement('td');
  timeCell.className = 'num';
  timeCell.textContent = modified;
  tr.append(nameCell, sizeCell, timeCell);
  tr.addEventListener('click', onClick);
  return tr;
}

function renderCrumbs(path) {
  const crumbs = $('crumbs');
  crumbs.innerHTML = '';
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

async function viewFile(path) {
  const viewer = $('viewer');
  try {
    const data = await api(`/api/files/read?path=${encodeURIComponent(path)}`);
    viewer.textContent = data.content;
    viewer.hidden = false;
    viewer.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } catch (error) {
    toast(error.message, true);
  }
}

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

function row(cells) {
  const tr = document.createElement('tr');
  cells.forEach(([text, className]) => {
    const td = document.createElement('td');
    if (className) td.className = className;
    td.textContent = text;
    tr.appendChild(td);
  });
  return tr;
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

async function loadUsers() {
  const table = $('user-table');
  try {
    const data = await api('/api/admin/users');
    table.innerHTML = '<tr><th>User</th><th>Access</th><th></th></tr>';
    table.appendChild(row([[data.admin, 'name'], ['admin — full access', ''], ['', '']]));

    if (!(data.users || []).length) {
      const empty = document.createElement('tr');
      empty.innerHTML = '<td colspan="3" class="empty">No read-only users yet.</td>';
      table.appendChild(empty);
      return;
    }
    data.users.forEach((user) => {
      const tr = row([[user.username, 'name'], ['read-only', ''], ['', '']]);
      const button = document.createElement('button');
      button.className = 'action danger';
      button.textContent = 'Remove';
      button.addEventListener('click', async () => {
        if (!confirm(`Remove ${user.username}?`)) return;
        try {
          await api(`/api/admin/users/${encodeURIComponent(user.username)}`, { method: 'DELETE' });
          toast(`Removed ${user.username}`);
          loadUsers();
        } catch (error) {
          toast(error.message, true);
        }
      });
      tr.lastChild.appendChild(button);
      table.appendChild(tr);
    });
  } catch (error) {
    toast(error.message, true);
  }
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
      body: JSON.stringify({ username, password }),
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
  } catch { /* the 401 path already redirected */ }
  applyCapabilities();
  renderStatus(await api('/api/status').catch(() => null));
  connect();
})();
