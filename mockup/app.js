/* Audio Normalizer — UI mock-up
 * Front-end prototype only: no backend, no file access. Library, run progress and
 * Home Assistant values are simulated in the browser so the final UX can be reviewed. */
(function () {
  'use strict';

  /* ------------------------------------------------------------------ utilities */
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fmt = (n) => Number(n).toLocaleString('en-US');
  const pad2 = (n) => String(n).padStart(2, '0');
  const fmtDb = (n) => (n >= 0 ? '+' : '−') + Math.abs(n).toFixed(1) + ' dB';
  const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const ROOT = '/data/library';
  const AUDIO_EXT = ['mp3', 'mp3', 'mp3', 'flac', 'm4a', 'wav', 'ogg', 'aac', 'wma'];
  const VIDEO_EXT = ['avi', 'mkv', 'mp4'];
  const LOSSLESS = ['flac', 'wav'];
  const TITLES = ['Intro', 'Morning Light', 'Cold Front', 'Blue Hour', 'Night Train', 'Echoes', 'Riverside', 'Long Way Home', 'Static', 'Finale'];
  const STATUS_LABEL = { pending: 'to do', ok: 'at target', changed: 'changed', parked: 'parked', skipped: 'skipped', error: 'error' };

  function rng(seed) {
    let s = seed >>> 0;
    return function () { s = (Math.imul(s, 1664525) + 1013904223) >>> 0; return s / 4294967296; };
  }
  const rand = rng(20261021);
  const pick = (arr) => arr[Math.floor(rand() * arr.length)];

  function toast(msg) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.classList.add('show');
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => t.classList.remove('show'), 2800);
  }

  /* ------------------------------------------------------------ mock library */
  const TREE_SPEC = [
    { name: 'Music', open: true, children: [
      { name: 'Rock', files: 42 },
      { name: 'Jazz', files: 6, children: [{ name: 'Live 1998', files: 12 }, { name: 'Blue Note', files: 9 }] },
      { name: 'Classical', files: 25 },
      { name: 'Compilations', files: 7 } ] },
    { name: 'Podcasts', open: true, children: [
      { name: 'Tech Weekly', files: 130 }, { name: 'History Hour', files: 88 }, { name: 'Interviews', files: 16 } ] },
    { name: 'Audiobooks', children: [{ name: 'Fantasy', files: 35 }, { name: 'Non-fiction', files: 21 }] },
    { name: 'Home videos', files: 4, video: true, children: [
      { name: '2019', files: 22 }, { name: '2020', files: 19 }, { name: '2021', files: 11 } ] },
    { name: 'Old archive (do not touch)', enabled: false, files: 60, children: [{ name: 'Recordings', files: 40 }] },
  ];

  function makeFile(node, k) {
    const ext = pick(node.isVideo ? VIDEO_EXT : AUDIO_EXT);
    const lufs = Math.round((-26 + rand() * 17) * 10) / 10;   // measured integrated loudness
    const r = rand();
    const status = r < 0.5 ? 'pending' : r < 0.72 ? 'ok' : r < 0.87 ? 'changed' : r < 0.95 ? 'parked' : r < 0.96 ? 'error' : 'skipped';
    const title = TITLES[k % TITLES.length] + ' ' + (Math.floor(k / TITLES.length) + 1);
    return { id: node.id + '#' + k, name: pad2(k + 1) + ' ' + title + '.' + ext, ext, lufs, status };
  }

  function buildNodes(specs, parentId, parentVideo) {
    return specs.map((spec) => {
      const id = parentId ? parentId + '/' + spec.name : spec.name;
      const node = {
        id, name: spec.name, enabled: spec.enabled !== false, open: !!spec.open,
        isVideo: !!spec.video || parentVideo, children: [], files: [],
      };
      node.children = buildNodes(spec.children || [], id, node.isVideo);
      for (let k = 0; k < (spec.files || 0); k++) node.files.push(makeFile(node, k));
      return node;
    });
  }

  const SENSOR_DEFS = [
    { key: 'status', name: 'Normalizer status', unit: '', deviceClass: '', icon: 'mdi:waveform', enabled: true },
    { key: 'progress_percent', name: 'Progress', unit: '%', deviceClass: '', icon: 'mdi:progress-check', enabled: true },
    { key: 'directories_enabled', name: 'Directories enabled', unit: 'dirs', deviceClass: '', icon: 'mdi:folder-multiple', enabled: true },
    { key: 'files_total', name: 'Files in scope', unit: 'files', deviceClass: '', icon: 'mdi:file-music', enabled: true },
    { key: 'files_pending', name: 'Files to do', unit: 'files', deviceClass: '', icon: 'mdi:file-clock', enabled: true },
    { key: 'files_checked', name: 'Files checked', unit: 'files', deviceClass: '', icon: 'mdi:file-check', enabled: true },
    { key: 'files_changed', name: 'Files changed', unit: 'files', deviceClass: '', icon: 'mdi:volume-plus', enabled: true },
    { key: 'files_parked', name: 'Files parked (review)', unit: 'files', deviceClass: '', icon: 'mdi:file-alert', enabled: true },
    { key: 'errors', name: 'Errors', unit: 'errors', deviceClass: '', icon: 'mdi:alert-circle', enabled: true },
    { key: 'current_file', name: 'Current file', unit: '', deviceClass: '', icon: 'mdi:file-music-outline', enabled: false },
    { key: 'last_run_finished', name: 'Last run finished', unit: '', deviceClass: 'timestamp', icon: 'mdi:history', enabled: false },
    { key: 'next_schedule_change', name: 'Next schedule change', unit: '', deviceClass: 'timestamp', icon: 'mdi:calendar-clock', enabled: false },
    { key: 'target_lufs', name: 'Target loudness', unit: 'LUFS', deviceClass: '', icon: 'mdi:tune-vertical', enabled: false },
  ];

  const DEFAULT_SETTINGS = {
    target: -16, truePeak: -1, threshold: 6, override: false,
    lossy: 'gain', videos: true, backup: true, dryRun: false,
  };

  function defaultSchedule() {
    // nights 22:00–06:00 every day, plus weekends all day
    return DAYS.map((_, d) => Array.from({ length: 24 }, (_, h) => (h >= 22 || h < 6) || d >= 5));
  }

  const state = {
    tree: buildNodes(TREE_SPEC, '', false),
    selectedDir: 'Music',
    run: { status: 'stopped', current: null, lastRun: new Date(Date.now() - 19 * 3600e3) },
    settings: Object.assign({}, DEFAULT_SETTINGS),
    schedule: defaultSchedule(),
    scanEvery: 15,
    onClose: 'finish',
    ha: {
      enabled: true, url: 'http://homeassistant.local:8123', token: 'demo-token-not-real',
      showToken: false, testMsg: '', sensors: SENSOR_DEFS.map((s) => Object.assign({}, s)),
    },
    log: [],
  };

  function log(level, msg) {
    state.log.unshift({ t: new Date(), level, msg });
    state.log = state.log.slice(0, 80);
  }
  log('info', 'Mock-up loaded — simulated library with ' + fmt(allEntries().length) + ' files');
  log('info', 'Scan: 2 new files found in Podcasts/Tech Weekly — queued for analysis');

  /* ------------------------------------------------------------- tree helpers */
  function walk(nodes, fn) { nodes.forEach((n) => { fn(n); walk(n.children, fn); }); }
  function allNodes() { const out = []; walk(state.tree, (n) => out.push(n)); return out; }
  function subtreeNodes(node) { const out = [node]; walk(node.children, (n) => out.push(n)); return out; }
  function findNode(id) { return allNodes().find((n) => n.id === id); }
  function subtreeState(node) {
    const flags = subtreeNodes(node).map((n) => n.enabled);
    return flags.every(Boolean) ? 'on' : flags.some(Boolean) ? 'mixed' : 'off';
  }
  function allEntries() {
    const out = [];
    allNodes().forEach((node) => node.files.forEach((file) => out.push({ node, file })));
    return out;
  }
  function scopeEntries() {
    const out = [];
    allNodes().forEach((node) => { if (node.enabled) node.files.forEach((file) => out.push({ node, file })); });
    return out;
  }

  /* -------------------------------------------------------------- statistics */
  function stats() {
    const scope = scopeEntries();
    const count = (st) => scope.filter((e) => e.file.status === st).length;
    const total = scope.length;
    const pending = count('pending');
    const checked = total - pending;
    const nodes = allNodes();
    return {
      dirsTotal: nodes.length,
      dirsEnabled: nodes.filter((n) => n.enabled).length,
      filesAll: allEntries().length,
      filesTotal: total,
      pending,
      checked,
      ok: count('ok'),
      changed: count('changed'),
      skipped: count('skipped'),
      errors: count('error'),
      parked: allEntries().filter((e) => e.file.status === 'parked').length,
      percent: total ? Math.round((checked / total) * 1000) / 10 : 0,
    };
  }

  /* ---------------------------------------------------- normalization model */
  const gainFor = (file) => state.settings.target - file.lufs;
  const isLossy = (file, node) => !(node.isVideo || LOSSLESS.includes(file.ext));

  function methodFor(file, node) {
    if (node.isVideo) return 'audio re-encode · video copied';
    if (LOSSLESS.includes(file.ext)) return 'lossless gain';
    if (state.settings.lossy === 'gain') return 'gain only (no re-encode)';
    if (state.settings.lossy === 'reencode') return 're-encode, same bitrate';
    return 'skipped (lossy format)';
  }

  /* ------------------------------------------------------------- schedule */
  const isAllowed = (d) => state.schedule[(d.getDay() + 6) % 7][d.getHours()];

  function nextChange(from) {
    const base = new Date(from);
    base.setMinutes(0, 0, 0);
    const inside = isAllowed(from);
    for (let i = 1; i <= 24 * 8; i++) {
      const t = new Date(base.getTime() + i * 3600e3);
      if (isAllowed(t) !== inside) return t;
    }
    return null;
  }

  const whenLabel = (t) => (t ? DAYS[(t.getDay() + 6) % 7] + ' ' + pad2(t.getHours()) + ':00' : '—');

  function scheduleInfo() {
    const now = new Date();
    const inside = isAllowed(now);
    const anyOn = state.schedule.some((d) => d.some(Boolean));
    const allOn = state.schedule.every((d) => d.every(Boolean));
    if (allOn) return { inside: true, text: 'Schedule: always active' };
    if (!anyOn) return { inside: false, text: 'Schedule: no active hours (manual runs only)' };
    const t = nextChange(now);
    return inside
      ? { inside, text: 'Inside schedule window · closes ' + whenLabel(t) }
      : { inside, text: 'Outside schedule window · opens ' + whenLabel(t) };
  }

  function dayRanges(di) {
    const row = state.schedule[di];
    const out = [];
    let start = null;
    for (let h = 0; h <= 24; h++) {
      const on = h < 24 && row[h];
      if (on && start === null) start = h;
      if (!on && start !== null) { out.push(pad2(start) + ':00–' + pad2(h) + ':00'); start = null; }
    }
    return out.length ? out.join(', ') : 'off';
  }

  const PRESETS = {
    nights: (d, h) => h >= 22 || h < 6,
    weekends: (d) => d >= 5,
    weeknights: (d, h) => d < 5 && (h >= 22 || h < 6),
    always: () => true,
    never: () => false,
  };

  /* ------------------------------------------------------- run simulation */
  function finishRun() {
    state.run.status = 'stopped';
    state.run.current = null;
    state.run.lastRun = new Date();
    log('ok', 'Run finished — all files in scope have been checked');
    toast('Run finished: all files in scope checked');
  }

  function tick() {
    if (state.run.status !== 'running') return;
    const todo = scopeEntries().filter((e) => e.file.status === 'pending');
    if (!todo.length) { finishRun(); refreshLive(); return; }
    todo.slice(0, 2).forEach(({ node, file }) => {
      const rel = node.id + '/' + file.name;
      const gain = gainFor(file);
      const abs = Math.abs(gain);
      state.run.current = rel;
      if (state.settings.lossy === 'skip-lossy' && isLossy(file, node)) {
        file.status = 'skipped';
      } else if (rand() < 0.015) {
        file.status = 'error';
        log('err', 'Could not decode ' + rel + ' — skipped, see log');
      } else if (abs < 0.5) {
        file.status = 'ok';
      } else if (abs > state.settings.threshold && !state.settings.override) {
        file.status = 'parked';
        log('warn', 'Parked ' + rel + ' — change ' + fmtDb(gain) + ' exceeds threshold, review later');
      } else {
        file.status = 'changed';
        log('ok', (state.settings.dryRun ? 'Would normalize ' : 'Normalized ') + rel + ' (' + fmtDb(gain) + ')');
      }
    });
    refreshLive();
  }

  // pages that refresh while a run is active (the directory page is refreshed on navigation only)
const LIVE_ROUTES = ['dashboard', 'parked'];
  function refreshLive() {
    if (LIVE_ROUTES.includes(currentRoute())) render();
    else updateNavBadge();
  }

  /* -------------------------------------------------------------- Home Assistant */
  function apiPayload() {
    const s = stats();
    const sched = scheduleInfo();
    const next = nextChange(new Date());
    return {
      status: state.run.status,
      progress_percent: s.percent,
      directories_total: s.dirsTotal,
      directories_enabled: s.dirsEnabled,
      files_total: s.filesTotal,
      files_pending: s.pending,
      files_checked: s.checked,
      files_changed: s.changed,
      files_parked: s.parked,
      errors: s.errors,
      current_file: state.run.current,
      last_run_finished: state.run.lastRun ? state.run.lastRun.toISOString() : null,
      next_schedule_change: next ? next.toISOString() : null,
      target_lufs: state.settings.target,
      in_schedule_window: sched.inside,
    };
  }

  function haYaml() {
    const enabled = state.ha.sensors.filter((x) => x.enabled);
    let y = 'rest:\n'
      + '  - resource: "http://<NAS-IP>:8080/api/v1/ha/status"\n'
      + '    headers:\n'
      + '      Authorization: "Bearer <YOUR_TOKEN>"\n'
      + '    scan_interval: 60\n'
      + '    sensor:\n';
    enabled.forEach((x) => {
      y += '      - name: "Audio normalizer ' + x.name.replace(/^Audio normalizer /i, '') + '"\n'
        + '        unique_id: audio_normalizer_' + x.key + '\n'
        + '        value_template: "{{ value_json.' + x.key + ' }}"\n';
      if (x.unit) y += '        unit_of_measurement: "' + x.unit + '"\n';
      if (x.deviceClass) y += '        device_class: ' + x.deviceClass + '\n';
      if (x.icon) y += '        icon: ' + x.icon + '\n';
    });
    if (!enabled.length) y += '      []\n';
    return y;
  }

  function refreshHaPreview() {
    const j = document.getElementById('ha-json');
    if (j) j.textContent = JSON.stringify(apiPayload(), null, 2);
    const y = document.getElementById('ha-yaml');
    if (y) y.textContent = haYaml();
  }

  /* ------------------------------------------------------------------ pages */
  const app = () => document.getElementById('app');

  function kpi(label, value, sub) {
    return '<div class="card kpi"><div class="label">' + label + '</div>'
      + '<div class="value">' + value + '</div><div class="sub">' + sub + '</div></div>';
  }

  function statusBadge(st) {
    return '<span class="st st-' + st + '">' + STATUS_LABEL[st] + '</span>';
  }

  function renderDashboard() {
    const s = stats();
    const r = state.run;
    const sched = scheduleInfo();
    const label = { running: 'Running', paused: 'Paused', stopped: 'Stopped' }[r.status];
    const finished = r.status === 'stopped' && s.filesTotal > 0 && s.pending === 0;
    const outside = r.status === 'running' && !sched.inside && state.schedule.some((d) => d.some(Boolean));
    const logs = state.log.slice(0, 12).map((e) =>
      '<li><time>' + pad2(e.t.getHours()) + ':' + pad2(e.t.getMinutes()) + ':' + pad2(e.t.getSeconds()) + '</time>'
      + '<span class="lv-' + (e.level === 'info' ? 'ok' : e.level) + '">' + esc(e.msg) + '</span></li>').join('');

    app().innerHTML = ''
      + '<header class="page-head"><div><h1>Dashboard</h1>'
      + '<p class="muted">Normalization of <code>' + ROOT + '</code> — every enabled folder, sub-folders included.</p></div>'
      + '<div class="btns">'
      + '<button class="btn btn-primary" data-action="start"' + (r.status !== 'stopped' ? ' disabled' : '') + '>▶ Start</button>'
      + '<button class="btn" data-action="pause"' + (r.status === 'stopped' ? ' disabled' : '') + '>'
      + (r.status === 'paused' ? '⏵ Resume' : '⏸ Pause') + '</button>'
      + '<button class="btn btn-danger" data-action="stop"' + (r.status === 'stopped' ? ' disabled' : '') + '>■ Stop</button>'
      + '<a class="btn" href="#/directories">📁 Browse folders</a>'
      + '</div></header>'

      + '<section class="card">'
      + '<div class="page-head" style="margin:0"><div>'
      + '<span class="badge b-' + r.status + '">' + label + '</span>'
      + (finished ? ' <span class="muted">· all files in scope checked</span>' : '')
      + '</div><div class="muted">' + esc(sched.text) + '</div></div>'
      + '<div class="progress" role="progressbar" aria-valuenow="' + s.percent + '" aria-valuemin="0" aria-valuemax="100">'
      + '<div style="width:' + s.percent + '%"></div></div>'
      + '<div class="muted"><strong style="color:var(--ink)">' + s.percent + '% complete</strong> · '
      + fmt(s.checked) + ' of ' + fmt(s.filesTotal) + ' files checked · current file: <code>'
      + (r.current ? esc(r.current) : '—') + '</code></div>'
      + (outside ? '<div class="warn-box">Running outside the schedule window — started manually. It stops when you press Stop.</div>' : '')
      + '</section>'

      + '<section class="kpis">'
      + kpi('1 · Directories / files in scope', s.dirsEnabled + ' / ' + fmt(s.filesTotal),
        'of ' + s.dirsTotal + ' directories and ' + fmt(s.filesAll) + ' files in the library')
      + kpi('2 · Files still to do', fmt(s.pending), 'waiting for analysis and normalization')
      + kpi('3 · Files checked', fmt(s.checked), fmt(s.ok) + ' already at target · ' + fmt(s.skipped) + ' skipped')
      + kpi('4 · Files actually changed', fmt(s.changed), 'level adjusted to ' + state.settings.target.toFixed(1) + ' LUFS')
      + '</section>'

      + '<section class="grid-2">'
      + '<div class="card"><h2>Needs attention</h2><ul class="attn">'
      + '<li><span>Parked files (waiting for your review)</span><span><a href="#/parked"><strong>' + s.parked + '</strong> open →</a></span></li>'
      + '<li><span>Errors in this run</span><span><strong>' + s.errors + '</strong></span></li>'
      + '<li><span>Last run finished</span><span>' + (r.lastRun ? r.lastRun.toLocaleString('en-GB') : 'never') + '</span></li>'
      + '<li><span>Large-change override</span><span>' + (state.settings.override ? '<strong>on</strong> — applied without review' : 'off') + '</span></li>'
      + '<li><span>Dry run</span><span>' + (state.settings.dryRun ? '<strong>on</strong> — nothing is written' : 'off') + '</span></li>'
      + '</ul></div>'
      + '<div class="card"><h2>Recent activity</h2><ul class="log">' + (logs || '<li><span class="muted">No activity yet.</span></li>') + '</ul></div>'
      + '</section>';
  }

  function renderTree(nodes) {
    return nodes.map((n) => {
      const st = subtreeState(n);
      const total = subtreeNodes(n).reduce((a, x) => a + x.files.length, 0);
      const hasKids = n.children.length > 0;
      return '<li class="' + (n.enabled ? '' : 'disabled-node') + '">'
        + '<div class="tree-row">'
        + '<button class="caret' + (hasKids ? '' : ' leaf') + '" data-action="tree-toggle" data-id="' + esc(n.id) + '" aria-label="expand or collapse">'
        + (n.open ? '▾' : '▸') + '</button>'
        + '<input type="checkbox" data-check="' + esc(n.id) + '" aria-label="include ' + esc(n.name) + '"' + (st === 'on' ? ' checked' : '') + '>'
        + '<button class="folder' + (state.selectedDir === n.id ? ' active' : '') + '" data-action="tree-browse" data-id="' + esc(n.id) + '">📁 ' + esc(n.name) + '</button>'
        + '<span class="count">' + fmt(total) + ' files</span>'
        + '</div>'
        + (hasKids && n.open ? '<ul>' + renderTree(n.children) + '</ul>' : '')
        + '</li>';
    }).join('');
  }

  function renderBrowse(node) {
    const shown = node.files.slice(0, 80);
    const rows = shown.map((f) => {
      const g = gainFor(f);
      return '<tr><td>' + esc(f.name) + '</td><td><code>' + f.ext + '</code></td>'
        + '<td class="num">' + f.lufs.toFixed(1) + ' LUFS</td>'
        + '<td class="num">' + fmtDb(g) + '</td>'
        + '<td class="dim">' + esc(methodFor(f, node)) + '</td>'
        + '<td>' + statusBadge(f.status) + '</td></tr>';
    }).join('');
    const subs = subtreeNodes(node).length - 1;
    return '<h2>📁 ' + esc(ROOT + '/' + node.id) + '</h2>'
      + '<p class="muted">' + node.files.length + ' files directly in this folder · ' + subs + ' sub-folders · '
      + (node.enabled ? '<span class="st st-ok">enabled</span>' : '<span class="st st-skipped">excluded</span>') + '</p>'
      + (node.files.length
        ? '<div class="table-wrap"><table><thead><tr><th>File</th><th>Format</th><th class="num">Level</th>'
          + '<th class="num">Change</th><th>Method</th><th>Status</th></tr></thead><tbody>' + rows + '</tbody></table></div>'
          + (node.files.length > 80 ? '<p class="muted">… and ' + (node.files.length - 80) + ' more files</p>' : '')
        : '<p class="muted">No audio files directly in this folder — open a sub-folder in the tree.</p>');
  }

  function renderDirectories() {
    const s = stats();
    const sel = findNode(state.selectedDir) || state.tree[0];
    app().innerHTML = ''
      + '<header class="page-head"><div><h1>Directories</h1>'
      + '<p class="muted">Base location: <code>' + ROOT + '</code> (mounted from a NAS folder in <code>docker-compose.yml</code>). '
      + 'Untick a folder to exclude it. Ticking or unticking a folder applies to all its sub-folders.</p></div>'
      + '<div class="btns">'
      + '<button class="btn" data-action="select-all">Select all</button>'
      + '<button class="btn" data-action="select-none">Select none</button>'
      + '<button class="btn" data-action="expand-all">Expand all</button>'
      + '<button class="btn" data-action="collapse-all">Collapse all</button>'
      + '</div></header>'
      + '<p class="muted"><strong style="color:var(--ink)">' + s.dirsEnabled + ' of ' + s.dirsTotal + '</strong> directories enabled · '
      + fmt(s.filesTotal) + ' files in scope</p>'
      + '<div class="split" style="margin-top:12px">'
      + '<section class="card tree" aria-label="Directory tree"><ul>' + renderTree(state.tree) + '</ul></section>'
      + '<section class="card">' + renderBrowse(sel) + '</section>'
      + '</div>';
    app().querySelectorAll('input[data-check]').forEach((cb) => {
      const n = findNode(cb.dataset.check);
      cb.indeterminate = !!n && subtreeState(n) === 'mixed';
    });
  }

  function renderParked() {
    const list = allEntries().filter((e) => e.file.status === 'parked');
    const th = state.settings.threshold;
    const rows = list.map(({ node, file }) => {
      const g = gainFor(file);
      return '<tr><td>' + esc(file.name) + '</td><td class="dim">' + esc(ROOT + '/' + node.id) + '</td>'
        + '<td class="num">' + file.lufs.toFixed(1) + ' LUFS</td>'
        + '<td class="num ' + (Math.abs(g) > th ? 'warn' : '') + '">' + fmtDb(g) + '</td>'
        + '<td class="btns"><button class="btn btn-sm btn-primary" data-action="approve" data-file="' + esc(file.id) + '">Apply change</button>'
        + '<button class="btn btn-sm" data-action="skip" data-file="' + esc(file.id) + '">Skip</button></td></tr>';
    }).join('');
    app().innerHTML = ''
      + '<header class="page-head"><div><h1>Parked files</h1>'
      + '<p class="muted">These files need a level change bigger than the <strong>' + th + ' dB</strong> threshold. '
      + 'They were <strong>not changed</strong>; the run simply continued with the next file. Review them when convenient.</p></div>'
      + '<div class="btns"><button class="btn btn-primary" data-action="approve-all"' + (list.length ? '' : ' disabled') + '>Apply all (' + list.length + ')</button>'
      + '<a class="btn" href="#/settings">Threshold &amp; override →</a></div></header>'
      + '<section class="card">'
      + (list.length
        ? '<div class="table-wrap"><table><thead><tr><th>File</th><th>Folder</th><th class="num">Level</th>'
          + '<th class="num">Change needed</th><th>Decision</th></tr></thead><tbody>' + rows + '</tbody></table></div>'
        : '<p class="muted">Nothing is parked. Every checked file is within the threshold.</p>')
      + '</section>';
  }

  function renderSchedule() {
    const sc = state.schedule;
    const hours = Array.from({ length: 24 }, (_, h) => h);
    const head = '<tr><th></th>' + hours.map((h) => '<th>' + pad2(h) + '</th>').join('') + '</tr>';
    const body = DAYS.map((d, di) => '<tr><td class="dayname">' + d + '</td>'
      + hours.map((h) => '<td class="cell' + (sc[di][h] ? ' on' : '') + '" data-d="' + di + '" data-h="' + h
        + '" title="' + d + ' ' + pad2(h) + ':00"></td>').join('') + '</tr>').join('');
    app().innerHTML = ''
      + '<header class="page-head"><div><h1>Schedule</h1>'
      + '<p class="muted">The normalizer only processes files inside the green windows. Click or drag over the grid to change hours. '
      + 'Times are local (Europe/Amsterdam).</p></div></header>'
      + '<section class="card">'
      + '<div class="btns" style="margin-bottom:12px">'
      + '<span class="muted" style="margin-right:4px">Presets:</span>'
      + '<button class="btn btn-sm" data-action="preset" data-preset="nights">Nights 22:00–06:00</button>'
      + '<button class="btn btn-sm" data-action="preset" data-preset="weeknights">Weeknights only</button>'
      + '<button class="btn btn-sm" data-action="preset" data-preset="weekends">Weekends all day</button>'
      + '<button class="btn btn-sm" data-action="preset" data-preset="always">Always</button>'
      + '<button class="btn btn-sm" data-action="preset" data-preset="never">Never</button>'
      + '</div>'
      + '<div class="sched-wrap"><table class="sched"><thead>' + head + '</thead><tbody>' + body + '</tbody></table></div>'
      + '<div class="legend"><span><span class="swatch" style="background:#3b82f6"></span>run allowed</span>'
      + '<span><span class="swatch" style="background:#eef2f7"></span>paused</span>'
      + '<span id="sched-now" class="muted"></span></div>'
      + '</section>'
      + '<div class="grid-2">'
      + '<section class="card"><h2>Active windows</h2><ul class="sched-list" id="sched-summary"></ul></section>'
      + '<section class="card"><h2>Scan &amp; window options</h2>'
      + '<div class="field"><label class="lbl" for="scan-every">Look for new or changed files every</label>'
      + '<div class="input-row" style="max-width:260px"><input class="input" id="scan-every" type="number" min="1" max="1440" data-sched="scanEvery" value="' + state.scanEvery + '"><span class="btn" style="cursor:default">minutes</span></div>'
      + '<p class="help">New files are detected automatically, also after a restart of the app.</p></div>'
      + '<div class="field"><span class="lbl">When the window closes while a file is being processed</span>'
      + '<label class="opt"><input type="radio" name="onclose" value="finish" data-sched="onClose"' + (state.onClose === 'finish' ? ' checked' : '') + '>'
      + '<span><strong>Finish the current file, then pause</strong><br><span class="muted">Recommended — no half-written files.</span></span></label>'
      + '<label class="opt"><input type="radio" name="onclose" value="now" data-sched="onClose"' + (state.onClose === 'now' ? ' checked' : '') + '>'
      + '<span><strong>Pause immediately</strong><br><span class="muted">The temporary file is discarded and retried later.</span></span></label></div>'
      + '</section></div>';
    updateScheduleInfo();
  }

  function updateScheduleInfo() {
    const list = document.getElementById('sched-summary');
    if (list) {
      list.innerHTML = DAYS.map((d, i) => '<li><strong>' + d + '</strong> &nbsp;' + dayRanges(i) + '</li>').join('');
    }
    const now = document.getElementById('sched-now');
    if (now) now.textContent = scheduleInfo().text;
  }

  function renderSettings() {
    const s = state.settings;
    const lossy = [
      ['gain', 'Gain only — change the level without re-encoding', 'MP3 via mp3gain, AAC via aacgain. Audio data is untouched. Level steps are about 1.5 dB, so the target is hit within ±0.75 dB.', true],
      ['reencode', 'Re-encode at the same codec and bitrate', 'Exact target level, but every re-encode adds a small generation loss.', false],
      ['skip-lossy', 'Skip lossy files', 'Only lossless files (FLAC, WAV) are normalized; MP3/AAC/WMA/OGG are left alone.', false],
    ].map(([v, t, d, rec]) => '<label class="opt"><input type="radio" name="lossy" value="' + v + '" data-setting="lossy"'
      + (s.lossy === v ? ' checked' : '') + '><span><strong>' + t + '</strong>' + (rec ? '<span class="tag">recommended</span>' : '')
      + '<br><span class="muted">' + d + '</span></span></label>').join('');

    app().innerHTML = ''
      + '<header class="page-head"><div><h1>Normalization settings</h1>'
      + '<p class="muted">Global defaults for every enabled folder. Changes are saved to the app\u2019s config volume.</p></div>'
      + '<div class="btns"><button class="btn" data-action="settings-reset">Reset to defaults</button>'
      + '<button class="btn btn-primary" data-action="settings-save">Save settings</button></div></header>'
      + '<div class="grid-2">'
      + '<section class="card"><h2>Output level</h2>'
      + '<div class="field"><label class="lbl" for="s-target">Target loudness (integrated, EBU R128)</label>'
      + '<div class="slider-row"><input id="s-target" type="range" min="-30" max="-10" step="0.5" value="' + s.target + '" data-setting="target">'
      + '<div class="val"><span data-out="target">' + s.target.toFixed(1) + '</span> LUFS</div></div>'
      + '<p class="help">−16 LUFS suits podcasts and speech, −14 suits music streaming, −23 is the EBU broadcast standard.</p></div>'
      + '<div class="field"><label class="lbl" for="s-tp">True-peak ceiling</label>'
      + '<div class="slider-row"><input id="s-tp" type="range" min="-6" max="-1" step="0.1" value="' + s.truePeak + '" data-setting="truePeak">'
      + '<div class="val"><span data-out="truePeak">' + s.truePeak.toFixed(1) + '</span> dBTP</div></div>'
      + '<p class="help">The gain is capped so that no sample goes above this level, which prevents clipping.</p></div>'
      + '<div class="field"><label class="lbl" for="s-th">Large-change threshold</label>'
      + '<div class="slider-row"><input id="s-th" type="range" min="1" max="12" step="0.5" value="' + s.threshold + '" data-setting="threshold">'
      + '<div class="val"><span data-out="threshold">' + s.threshold.toFixed(1) + '</span> dB</div></div>'
      + '<p class="help">Changes bigger than this are <em>parked</em> for review instead of being applied.</p></div>'
      + '<label class="check"><input type="checkbox" data-setting="override"' + (s.override ? ' checked' : '') + '>'
      + '<span><strong>Override the confirmation for large changes</strong><br><span class="muted">Apply changes above the threshold automatically, without parking the file.</span></span></label>'
      + (s.override ? '<div class="warn-box">⚠ Large changes will be applied without review. Use this only with backups enabled.</div>' : '')
      + '</section>'
      + '<section class="card"><h2>Quality &amp; file handling</h2>'
      + '<div class="field"><span class="lbl">Lossy formats (MP3, AAC, WMA, OGG, M4A)</span>' + lossy + '</div>'
      + '<label class="check"><input type="checkbox" data-setting="videos"' + (s.videos ? ' checked' : '') + '>'
      + '<span><strong>Normalize the audio track of video files</strong><br><span class="muted">AVI, MKV, MP4. The video stream is copied, never re-encoded.</span></span></label>'
      + '<label class="check"><input type="checkbox" data-setting="backup"' + (s.backup ? ' checked' : '') + '>'
      + '<span><strong>Keep a backup of the original file</strong><br><span class="muted">Stored in <code>/config/backups</code> before the file is replaced.</span></span></label>'
      + '<label class="check"><input type="checkbox" data-setting="dryRun"' + (s.dryRun ? ' checked' : '') + '>'
      + '<span><strong>Dry run</strong><br><span class="muted">Analyse and report only. Nothing is written. Recommended for the first run on a real library.</span></span></label>'
      + '</section></div>';
  }

  function renderHA() {
    const h = state.ha;
    app().innerHTML = ''
      + '<header class="page-head"><div><h1>Home Assistant</h1>'
      + '<p class="muted">Publishes the normalizer status as a local, token-protected API. Home Assistant reads it with RESTful sensors.</p></div>'
      + '<div class="btns"><button class="btn btn-primary" data-action="ha-save">Save</button></div></header>'
      + '<div class="grid-2" style="margin-top:0">'
      + '<section class="card"><h2>Connection</h2>'
      + '<label class="check"><input type="checkbox" data-ha="enabled"' + (h.enabled ? ' checked' : '') + '>'
      + '<span><strong>Enable the Home Assistant API</strong><br><span class="muted">Requests without a valid token are refused.</span></span></label>'
      + '<div class="field" style="margin-top:14px"><label class="lbl" for="ha-url">Home Assistant URL</label>'
      + '<input class="input" id="ha-url" data-ha="url" value="' + esc(h.url) + '"></div>'
      + '<div class="field"><label class="lbl" for="ha-token">Long-lived access token</label>'
      + '<div class="input-row"><input class="input" id="ha-token" data-ha="token" autocomplete="off" type="' + (h.showToken ? 'text' : 'password') + '" value="' + esc(h.token) + '">'
      + '<button class="btn" data-action="ha-show">' + (h.showToken ? 'Hide' : 'Show') + '</button></div>'
      + '<p class="help">Create it in Home Assistant: Profile → Security → Long-lived access tokens. Stored in the config volume, never shown in logs.</p></div>'
      + '<div class="btns"><button class="btn" data-action="ha-test">Test connection</button>'
      + '<span class="muted">' + esc(h.testMsg) + '</span></div>'
      + '</section>'
      + '<section class="card"><h2>Local API endpoint</h2>'
      + '<pre>GET http://&lt;NAS-IP&gt;:8080/api/v1/ha/status\nAuthorization: Bearer &lt;token&gt;</pre>'
      + '<h2 style="margin-top:16px">Current payload</h2><pre id="ha-json"></pre></section>'
      + '</div>'
      + '<section class="card" style="margin-top:16px"><h2>Sensor definitions</h2>'
      + '<p class="muted">Pick which values become sensors in Home Assistant and how they are named.</p>'
      + '<div class="table-wrap"><table><thead><tr><th>On</th><th>Sensor name</th><th>JSON key</th><th>Unit</th><th>Device class</th></tr></thead><tbody>'
      + h.sensors.map((x, i) => '<tr><td><input type="checkbox" data-sensor="' + i + '" data-field="enabled"' + (x.enabled ? ' checked' : '') + '></td>'
        + '<td><input class="input" data-sensor="' + i + '" data-field="name" value="' + esc(x.name) + '"></td>'
        + '<td><code>' + x.key + '</code></td><td>' + (x.unit ? esc(x.unit) : '<span class="dim">—</span>') + '</td>'
        + '<td>' + (x.deviceClass || '<span class="dim">—</span>') + '</td></tr>').join('')
      + '</tbody></table></div></section>'
      + '<section class="card" style="margin-top:16px"><h2>Generated <code>configuration.yaml</code> snippet</h2>'
      + '<pre id="ha-yaml"></pre></section>';
    refreshHaPreview();
  }

  /* ----------------------------------------------------------------- routing */
  const ROUTES = {
    dashboard: renderDashboard,
    directories: renderDirectories,
    parked: renderParked,
    schedule: renderSchedule,
    settings: renderSettings,
    homeassistant: renderHA,
  };

  function currentRoute() {
    const r = location.hash.replace(/^#\/?/, '') || 'dashboard';
    return ROUTES[r] ? r : 'dashboard';
  }

  function updateNavBadge() {
    document.getElementById('parkedBadge').textContent = stats().parked;
  }

  function render() {
    const route = currentRoute();
    document.querySelectorAll('#nav a').forEach((a) => {
      if (a.dataset.route === route) a.setAttribute('aria-current', 'page');
      else a.removeAttribute('aria-current');
    });
    updateNavBadge();
    ROUTES[route]();
  }

  /* ------------------------------------------------------------- interaction */
  document.addEventListener('click', (e) => {
    const el = e.target.closest('[data-action]');
    if (!el || el.disabled) return;
    const a = el.dataset.action;
    const r = state.run;

    if (a === 'start' && r.status === 'stopped') {
      r.status = 'running';
      log('info', 'Run started manually');
    } else if (a === 'pause') {
      r.status = r.status === 'running' ? 'paused' : 'running';
      log('info', r.status === 'paused' ? 'Run paused' : 'Run resumed');
    } else if (a === 'stop') {
      r.status = 'stopped';
      r.current = null;
      log('info', 'Run stopped by user');
    } else if (a === 'tree-toggle') {
      const n = findNode(el.dataset.id); if (n) n.open = !n.open;
    } else if (a === 'tree-browse') {
      state.selectedDir = el.dataset.id;
    } else if (a === 'select-all' || a === 'select-none') {
      allNodes().forEach((n) => { n.enabled = a === 'select-all'; });
    } else if (a === 'expand-all' || a === 'collapse-all') {
      allNodes().forEach((n) => { n.open = a === 'expand-all'; });
    } else if (a === 'approve' || a === 'skip') {
      const entry = allEntries().find((x) => x.file.id === el.dataset.file);
      if (entry) {
        entry.file.status = a === 'approve' ? 'changed' : 'skipped';
        log(a === 'approve' ? 'ok' : 'info', (a === 'approve' ? 'Change applied: ' : 'Skipped: ') + entry.node.id + '/' + entry.file.name);
      }
    } else if (a === 'approve-all') {
      allEntries().forEach((x) => { if (x.file.status === 'parked') x.file.status = 'changed'; });
      log('ok', 'All parked files approved and applied');
    } else if (a === 'preset') {
      const fn = PRESETS[el.dataset.preset];
      state.schedule = DAYS.map((_, d) => Array.from({ length: 24 }, (_, h) => !!fn(d, h)));
    } else if (a === 'settings-save') {
      toast('Settings saved (mock-up: not persisted)');
      return;
    } else if (a === 'settings-reset') {
      state.settings = Object.assign({}, DEFAULT_SETTINGS);
    } else if (a === 'ha-show') {
      state.ha.showToken = !state.ha.showToken;
    } else if (a === 'ha-test') {
      state.ha.testMsg = '✔ Connected to Home Assistant 2026.10 (simulated)';
    } else if (a === 'ha-save') {
      toast('Home Assistant settings saved (mock-up: not persisted)');
      return;
    }
    render();
  });

  function onField(e) {
    const t = e.target;

    if (t.dataset.check !== undefined) {
      const n = findNode(t.dataset.check);
      if (n) subtreeNodes(n).forEach((x) => { x.enabled = t.checked; });
      if (e.type === 'change') render();
      return;
    }

    if (t.dataset.setting) {
      const key = t.dataset.setting;
      if (t.type === 'radio' && !t.checked) return;
      if (t.type === 'range') {
        const v = parseFloat(t.value);
        state.settings[key] = v;
        const out = document.querySelector('[data-out="' + key + '"]');
        if (out) out.textContent = v.toFixed(1);
        return;
      }
      state.settings[key] = t.type === 'checkbox' ? t.checked : t.value;
      if (e.type === 'change') render();
      return;
    }

    if (t.dataset.ha) {
      state.ha[t.dataset.ha] = t.type === 'checkbox' ? t.checked : t.value;
      refreshHaPreview();
      return;
    }

    if (t.dataset.sensor !== undefined) {
      const x = state.ha.sensors[Number(t.dataset.sensor)];
      if (x) x[t.dataset.field] = t.type === 'checkbox' ? t.checked : t.value;
      refreshHaPreview();
      return;
    }

    if (t.dataset.sched) {
      if (t.type === 'radio' && !t.checked) return;
      if (t.dataset.sched === 'scanEvery') {
        state.scanEvery = Math.min(1440, Math.max(1, Math.round(Number(t.value)) || 1));
      } else {
        state.onClose = t.value;
      }
      return;
    }
  }
  document.addEventListener('input', onField);
  document.addEventListener('change', onField);

  /* schedule grid: click or drag to paint */
  let painting = null;
  function paintCell(cell) {
    const d = Number(cell.dataset.d), h = Number(cell.dataset.h);
    state.schedule[d][h] = painting;
    cell.classList.toggle('on', painting);
  }
  app().addEventListener('pointerdown', (e) => {
    const cell = e.target.closest('.cell');
    if (!cell) return;
    e.preventDefault();
    painting = !state.schedule[Number(cell.dataset.d)][Number(cell.dataset.h)];
    paintCell(cell);
  });
  app().addEventListener('pointerover', (e) => {
    if (painting === null) return;
    const cell = e.target.closest('.cell');
    if (cell) paintCell(cell);
  });
  window.addEventListener('pointerup', () => {
    if (painting === null) return;
    painting = null;
    updateScheduleInfo();
  });

  /* ------------------------------------------------------------------- start */
  window.addEventListener('hashchange', render);
  setInterval(tick, 900);
  render();
})();
