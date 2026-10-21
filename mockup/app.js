/* Audio Normalizer — UI mock-up (v2: audio / video split)
 * Front-end prototype only: no backend, no file access. Every value is simulated.
 *
 * Audio and video are first-class media types. Each has its own library root,
 * folder selection, schedule, settings, counters, parked queue and Home Assistant sensors.
 * The run engine is shared; the "Process" switches on the dashboard choose the types. */
(function () {
  'use strict';

  /* ------------------------------------------------------------ utilities */
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fmt = (n) => Number(n).toLocaleString('en-US');
  const pad2 = (n) => String(n).padStart(2, '0');
  const round1 = (n) => Math.round(n * 10) / 10;
  const fmtDb = (n) => (n >= 0 ? '+' : '−') + Math.abs(n).toFixed(1) + ' dB';
  const clone = (o) => JSON.parse(JSON.stringify(o));
  const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const ALL = ['audio', 'video'];

  const MEDIA = {
    audio: {
      label: 'Audio', root: '/data/audio', host: '/volume1/music',
      exts: ['mp3', 'mp3', 'mp3', 'flac', 'm4a', 'wav', 'ogg', 'aac', 'wma'],
      lossless: ['flac', 'wav'],
    },
    video: {
      label: 'Video', root: '/data/video', host: '/volume1/video',
      exts: ['avi', 'mkv', 'mp4'],
      lossless: [],
    },
  };

  const STATUS_LABEL = { pending: 'to do', ok: 'at target', changed: 'changed', parked: 'parked', skipped: 'skipped', error: 'error' };
  const TITLES = ['Intro', 'Morning Light', 'Cold Front', 'Blue Hour', 'Night Train', 'Echoes', 'Riverside', 'Long Way Home', 'Static', 'Finale'];

  function rng(seed) {
    let s = seed >>> 0;
    return function () { s = (Math.imul(s, 1664525) + 1013904223) >>> 0; return s / 4294967296; };
  }
  const rand = rng(20261021);
  const pick = (arr) => arr[Math.floor(rand() * arr.length)];

  const INFO_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 7.6v.4"/></svg>';
  function tip(text) {
    return '<span class="tip" tabindex="0" aria-label="' + esc(text) + '">' + INFO_ICON
      + '<span class="tip-body" role="tooltip">' + esc(text) + '</span></span>';
  }

  /* green fill up to the thumb: used by every range slider */
  function paintRange(el) {
    const min = Number(el.min), max = Number(el.max), v = Number(el.value);
    const pct = max > min ? ((v - min) / (max - min)) * 100 : 0;
    el.style.setProperty('--pct', pct.toFixed(2) + '%');
  }

  function toast(msg) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.classList.add('show');
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => t.classList.remove('show'), 2800);
  }

  /* ---------------------------------------------------------- mock library */
  const SPEC = {
    audio: [
      { name: 'Music', open: true, children: [
        { name: 'Rock', files: 42 },
        { name: 'Jazz', files: 6, children: [{ name: 'Live 1998', files: 12 }, { name: 'Blue Note', files: 9 }] },
        { name: 'Classical', files: 25 },
        { name: 'Compilations', files: 7 } ] },
      { name: 'Podcasts', open: true, children: [
        { name: 'Tech Weekly', files: 130 }, { name: 'History Hour', files: 88 }, { name: 'Interviews', files: 16 } ] },
      { name: 'Audiobooks', children: [{ name: 'Fantasy', files: 35 }, { name: 'Non-fiction', files: 21 }] },
      { name: 'Old archive (do not touch)', enabled: false, files: 60, children: [{ name: 'Recordings', files: 40 }] },
    ],
    video: [
      { name: 'Home videos', open: true, children: [
        { name: '2019', files: 22 }, { name: '2020', files: 19 }, { name: '2021', files: 11 } ] },
      { name: 'Phone clips', files: 9 },
      { name: 'Old tapes', enabled: false, files: 30 },
    ],
  };

  function makeFile(node, k) {
    const ext = pick(MEDIA[node.type].exts);
    const lufs = Math.round((-26 + rand() * 17) * 10) / 10;   // measured integrated loudness of the audio
    const r = rand();
    const status = r < 0.5 ? 'pending' : r < 0.72 ? 'ok' : r < 0.87 ? 'changed' : r < 0.95 ? 'parked' : r < 0.96 ? 'error' : 'skipped';
    const title = TITLES[k % TITLES.length] + ' ' + (Math.floor(k / TITLES.length) + 1);
    return { id: node.id + '#' + k, name: pad2(k + 1) + ' ' + title + '.' + ext, ext, lufs, status, adjust: 0 };
  }

  function buildNodes(type, specs, parentRel) {
    return specs.map((spec) => {
      const rel = parentRel ? parentRel + '/' + spec.name : spec.name;
      const node = {
        type, id: type + ':' + rel, rel, name: spec.name,
        enabled: spec.enabled !== false, open: !!spec.open, children: [], files: [],
      };
      node.children = buildNodes(type, spec.children || [], rel);
      for (let k = 0; k < (spec.files || 0); k++) node.files.push(makeFile(node, k));
      return node;
    });
  }

  /* -------------------------------------------------------------- defaults */
  const DEFAULTS = {
    general: { dryRun: false },
    audio: { target: -16, truePeak: -1, threshold: 6, override: false, lossy: 'gain', writeMode: 'inplace', outputDir: '/data/normalized-audio' },
    video: { target: -16, truePeak: -1, threshold: 6, override: false, writeMode: 'inplace', outputDir: '/data/normalized-video' },
  };

  const PRESETS = {
    nights: (d, h) => h >= 22 || h < 6,
    weeknights: (d, h) => d < 5 && (h >= 22 || h < 6),
    weekends: (d) => d >= 5,
    always: () => true,
    never: () => false,
  };
  const scheduleFrom = (fn) => DAYS.map((_, d) => Array.from({ length: 24 }, (_, h) => !!fn(d, h)));

  const SENSOR_DEFS = [
    { path: 'status', name: 'Status', unit: '', deviceClass: '', icon: 'mdi:waveform', enabled: true, group: 'Overall' },
    { path: 'progress_percent', name: 'Progress', unit: '%', deviceClass: '', icon: 'mdi:progress-check', enabled: true, group: 'Overall' },
    { path: 'errors', name: 'Errors', unit: 'errors', deviceClass: '', icon: 'mdi:alert-circle', enabled: true, group: 'Overall' },
    { path: 'current_file', name: 'Current file', unit: '', deviceClass: '', icon: 'mdi:file-music-outline', enabled: false, group: 'Overall' },
    { path: 'last_run_finished', name: 'Last run finished', unit: '', deviceClass: 'timestamp', icon: 'mdi:history', enabled: false, group: 'Overall' },
  ];
  ALL.forEach((t) => {
    const L = MEDIA[t].label;
    SENSOR_DEFS.push(
      { path: t + '.directories_enabled', name: L + ' folders enabled', unit: 'folders', deviceClass: '', icon: 'mdi:folder-multiple', enabled: true, group: L },
      { path: t + '.files_total', name: L + ' files in scope', unit: 'files', deviceClass: '', icon: 'mdi:file-multiple', enabled: true, group: L },
      { path: t + '.files_pending', name: L + ' files to do', unit: 'files', deviceClass: '', icon: 'mdi:file-clock', enabled: true, group: L },
      { path: t + '.files_checked', name: L + ' files checked', unit: 'files', deviceClass: '', icon: 'mdi:file-check', enabled: true, group: L },
      { path: t + '.files_changed', name: L + ' files changed', unit: 'files', deviceClass: '', icon: 'mdi:volume-plus', enabled: true, group: L },
      { path: t + '.files_parked', name: L + ' files parked', unit: 'files', deviceClass: '', icon: 'mdi:file-alert', enabled: true, group: L },
      { path: t + '.target_lufs', name: L + ' target loudness', unit: 'LUFS', deviceClass: '', icon: 'mdi:tune-vertical', enabled: false, group: L },
      { path: t + '.next_schedule_change', name: L + ' next schedule change', unit: '', deviceClass: 'timestamp', icon: 'mdi:calendar-clock', enabled: false, group: L },
    );
  });

  /* ----------------------------------------------------------------- state */
  const state = {
    trees: { audio: buildNodes('audio', SPEC.audio, ''), video: buildNodes('video', SPEC.video, '') },
    media: 'audio',                                   // media tab shown on media-specific pages
    selected: { audio: 'audio:Music', video: 'video:Home videos' },
    run: {
      status: 'stopped', current: null,
      lastRun: new Date(Date.now() - 19 * 3600e3),
      types: { audio: true, video: true },            // "Process" switches on the dashboard
    },
    settings: clone(DEFAULTS),
    schedule: {
      audio: scheduleFrom((d, h) => (h >= 22 || h < 6) || d >= 5),   // nights + weekends
      video: scheduleFrom((d) => d >= 5),                              // weekends only (heavier work)
    },
    scanEvery: 15,
    onClose: 'finish',
    ha: {
      enabled: true, url: 'http://homeassistant.local:8123', token: 'demo-token-not-real',
      showToken: false, testMsg: '', sensors: SENSOR_DEFS.map((s) => Object.assign({}, s)),
    },
    log: [],
    parkSel: [],                                     // selected parked file ids
    parkSort: 'order',                               // sort of the parked list
    measure: null,                                   // last file level check (baseline)
    baselineFrom: null,                              // media type when opened from Settings > Level
  };

  function log(level, type, msg) {
    state.log.unshift({ t: new Date(), level, type, msg });
    state.log = state.log.slice(0, 80);
  }
  log('info', null, 'Mock-up loaded with ' + fmt(entriesOf(ALL).length) + ' files across both libraries');
  log('info', 'audio', 'Scan: 2 new files in /data/audio/Podcasts/Tech Weekly, queued');
  log('info', 'video', 'Scan: 1 new file in /data/video/Home videos/2021, queued');

  /* ------------------------------------------------------------ tree helpers */
  function walk(nodes, fn) { nodes.forEach((n) => { fn(n); walk(n.children, fn); }); }
  function nodesOf(types) { const out = []; types.forEach((t) => walk(state.trees[t], (n) => out.push(n))); return out; }
  function subtreeNodes(node) { const out = [node]; walk(node.children, (n) => out.push(n)); return out; }
  function findNode(id) { return nodesOf(ALL).find((n) => n.id === id); }
  function subtreeState(node) {
    const flags = subtreeNodes(node).map((n) => n.enabled);
    return flags.every(Boolean) ? 'on' : flags.some(Boolean) ? 'mixed' : 'off';
  }
  function entriesOf(types) {
    const out = [];
    nodesOf(types).forEach((node) => node.files.forEach((file) => out.push({ node, file })));
    return out;
  }
  const pathOf = (node, file) => MEDIA[node.type].root + '/' + node.rel + '/' + file.name;

  /* -------------------------------------------------------------- statistics */
  function stats(t) {
    const all = entriesOf([t]);
    const scope = all.filter((e) => e.node.enabled);
    const c = (st) => scope.filter((e) => e.file.status === st).length;
    const total = scope.length;
    const pending = c('pending');
    const checked = total - pending;
    const nodes = nodesOf([t]);
    return {
      dirsTotal: nodes.length,
      dirsEnabled: nodes.filter((n) => n.enabled).length,
      filesAll: all.length,
      filesTotal: total,
      pending,
      checked,
      ok: c('ok'),
      changed: c('changed'),
      skipped: c('skipped'),
      errors: c('error'),
      parked: all.filter((e) => e.file.status === 'parked').length,
      percent: total ? round1((checked / total) * 100) : 0,
    };
  }

  function runStats() {
    const types = ALL.filter((t) => state.run.types[t]);
    const s = types.map(stats);
    const total = s.reduce((a, x) => a + x.filesTotal, 0);
    const checked = s.reduce((a, x) => a + x.checked, 0);
    return { total, checked, percent: total ? round1((checked / total) * 100) : 0 };
  }

  /* ------------------------------------------------------ normalization model */
  const gainFor = (t, file) => state.settings[t].target - file.lufs;
  const isLossy = (t, file) => t === 'audio' && !MEDIA.audio.lossless.includes(file.ext);

  function methodFor(t, file) {
    if (t === 'video') return 'audio re-encode · video copied';
    if (MEDIA.audio.lossless.includes(file.ext)) return 'lossless gain';
    const m = state.settings.audio.lossy;
    return m === 'gain' ? 'gain only (no re-encode)' : m === 'reencode' ? 're-encode, same bitrate' : 'skipped (lossy format)';
  }

  /* ------------------------------------------------------------- schedule */
  const isAllowed = (t, d) => state.schedule[t][(d.getDay() + 6) % 7][d.getHours()];

  function nextChange(t, from) {
    const base = new Date(from);
    base.setMinutes(0, 0, 0);
    const inside = isAllowed(t, from);
    for (let i = 1; i <= 24 * 8; i++) {
      const x = new Date(base.getTime() + i * 3600e3);
      if (isAllowed(t, x) !== inside) return x;
    }
    return null;
  }

  const whenLabel = (x) => (x ? DAYS[(x.getDay() + 6) % 7] + ' ' + pad2(x.getHours()) + ':00' : '—');

  function scheduleInfo(t) {
    const sc = state.schedule[t];
    const now = new Date();
    const inside = isAllowed(t, now);
    if (sc.every((d) => d.every(Boolean))) return { inside: true, text: 'Always on' };
    if (!sc.some((d) => d.some(Boolean))) return { inside: false, text: 'No active hours (manual runs only)' };
    const next = nextChange(t, now);
    return inside
      ? { inside, text: 'Window open until ' + whenLabel(next) }
      : { inside, text: 'Window opens ' + whenLabel(next) };
  }

  function dayRanges(t, di) {
    const row = state.schedule[t][di];
    const out = [];
    let start = null;
    for (let h = 0; h <= 24; h++) {
      const on = h < 24 && row[h];
      if (on && start === null) start = h;
      if (!on && start !== null) { out.push(pad2(start) + ':00–' + pad2(h) + ':00'); start = null; }
    }
    return out.length ? out.join(', ') : 'off';
  }

  /* ---------------------------------------------------------- run engine */
  function finishRun() {
    state.run.status = 'stopped';
    state.run.current = null;
    state.run.lastRun = new Date();
    log('ok', null, 'Run finished: all files in the selected types have been checked');
    toast('Run finished: all files in scope checked');
  }

  function processFile(node, file) {
    const t = node.type;
    const cfg = state.settings[t];
    const path = pathOf(node, file);
    const gain = gainFor(t, file);
    const abs = Math.abs(gain);
    state.run.current = path;

    if (t === 'audio' && cfg.lossy === 'skip-lossy' && isLossy(t, file)) {
      file.status = 'skipped';
    } else if (rand() < 0.012) {
      file.status = 'error';
      log('err', t, 'Could not decode ' + path + ', skipped');
    } else if (abs < 0.5) {
      file.status = 'ok';
    } else if (abs > cfg.threshold && !cfg.override) {
      file.status = 'parked';
      log('warn', t, 'Parked ' + file.name + ': change ' + fmtDb(gain) + ' is over the threshold');
    } else {
      file.status = 'changed';
      log('ok', t, (state.settings.general.dryRun ? 'Would normalize ' : 'Normalized ') + file.name + ' (' + fmtDb(gain) + ')');
    }
  }

  function tick() {
    if (state.run.status !== 'running') return;
    const types = ALL.filter((t) => state.run.types[t]);
    const todo = entriesOf(types).filter((e) => e.node.enabled && e.file.status === 'pending');
    if (!todo.length) { finishRun(); refreshLive(); return; }
    todo.slice(0, 2).forEach(({ node, file }) => processFile(node, file));
    refreshLive();
  }

  const LIVE_ROUTES = ['dashboard', 'parked'];
  function refreshLive() {
    if (LIVE_ROUTES.includes(currentRoute())) render();
    else updateNavBadge();
  }

  /* -------------------------------------------------------- Home Assistant */
  function typePayload(t) {
    const s = stats(t);
    const next = nextChange(t, new Date());
    return {
      in_schedule_window: scheduleInfo(t).inside,
      directories_total: s.dirsTotal,
      directories_enabled: s.dirsEnabled,
      files_total: s.filesTotal,
      files_pending: s.pending,
      files_checked: s.checked,
      files_changed: s.changed,
      files_parked: s.parked,
      target_lufs: state.settings[t].target,
      next_schedule_change: next ? next.toISOString() : null,
    };
  }

  function apiPayload() {
    const rs = runStats();
    return {
      status: state.run.status,
      progress_percent: rs.percent,
      errors: stats('audio').errors + stats('video').errors,
      current_file: state.run.current,
      last_run_finished: state.run.lastRun ? state.run.lastRun.toISOString() : null,
      audio: typePayload('audio'),
      video: typePayload('video'),
    };
  }

  const MAIN_ATTRS = ['progress_percent', 'errors', 'current_file', 'last_run_finished', 'audio', 'video'];
  const MAIN_NAME = 'Audio Normalizer';

  function haYaml() {
    const en = state.ha.sensors.filter((x) => x.enabled);
    let y = 'rest:\n'
      + '  - resource: "http://<NAS-IP>:8080/api/v1/ha/status"\n'
      + '    headers:\n'
      + '      Authorization: "Bearer <YOUR_TOKEN>"\n'
      + '    scan_interval: 60\n'
      + '    sensor:\n'
      + '      # main entity: state = run status, attributes = full payload (used by the Lovelace card)\n'
      + '      - name: "' + MAIN_NAME + '"\n'
      + '        unique_id: audio_normalizer_main\n'
      + '        value_template: "{{ value_json.status }}"\n'
      + '        json_attributes:\n'
      + MAIN_ATTRS.map((k) => '          - ' + k + '\n').join('')
      + '        icon: mdi:waveform\n';
    en.forEach((x) => {
      const uid = 'audio_normalizer_' + x.path.replace(/\./g, '_');
      y += '      # optional single sensor\n'
        + '      - name: "' + x.name.replace(/"/g, '') + '"\n'
        + '        unique_id: ' + uid + '\n'
        + '        value_template: "{{ value_json.' + x.path + ' }}"\n';
      if (x.unit) y += '        unit_of_measurement: "' + x.unit + '"\n';
      if (x.deviceClass) y += '        device_class: ' + x.deviceClass + '\n';
      if (x.icon) y += '        icon: ' + x.icon + '\n';
    });
    return y;
  }

  function enabledPaths() { return state.ha.sensors.filter((x) => x.enabled).map((x) => x.path); }
  function cardConfig() {
    return { entity: 'sensor.audio_normalizer', title: 'Audio Normalizer', show_audio: true, show_video: true, show_details: true, fields: enabledPaths() };
  }
  function cardYaml() {
    const f = enabledPaths();
    return 'type: custom:audio-normalizer-card\nentity: sensor.audio_normalizer\ntitle: Audio Normalizer\nshow_audio: true\nshow_video: true\nshow_details: true\n'
      + (f.length ? 'fields:\n' + f.map((x) => '  - ' + x + '\n').join('') : 'fields: []\n');
  }

  function refreshHaPreview() {
    const j = document.getElementById('ha-json');
    if (j) j.textContent = JSON.stringify(apiPayload(), null, 2);
    const y = document.getElementById('ha-yaml');
    if (y) y.textContent = haYaml();
    const cy = document.getElementById('ha-card-yaml');
    if (cy) cy.textContent = cardYaml();
    const card = document.getElementById('card-preview');
    if (card && typeof card.setConfig === 'function') {
      card.setConfig(cardConfig());
      const p = apiPayload();
      card.hass = { states: { 'sensor.audio_normalizer': { state: p.status, attributes: p } } };
    }
  }

  /* ----------------------------------------------------------- components */
  const app = () => document.getElementById('app');

  function segControl() {
    return '<div class="seg" role="tablist" aria-label="Media type">'
      + ALL.map((t) => '<button type="button" role="tab" aria-selected="' + (state.media === t) + '"'
        + ' class="' + (state.media === t ? 'on' : '') + '" data-action="media" data-media="' + t + '">'
        + MEDIA[t].label + '</button>').join('')
      + '</div>';
  }

  function pageHead(title, desc, actions, withSeg) {
    return '<header class="page-head"><div><h1>' + title + '</h1>'
      + (desc ? '<p class="desc">' + desc + '</p>' : '') + '</div>'
      + '<div class="head-actions">' + (withSeg ? segControl() : '') + (actions || '') + '</div></header>';
  }

  function statusLabel(st) { return '<span class="st st-' + st + '">' + STATUS_LABEL[st] + '</span>'; }

  function stat(label, value, sub) {
    return '<div class="stat"><div class="stat-label">' + label + '</div>'
      + '<div class="stat-value">' + value + '</div><div class="stat-sub">' + sub + '</div></div>';
  }

  /* ------------------------------------------------------------- dashboard */
  function mediaPanel(t) {
    const s = stats(t);
    const info = scheduleInfo(t);
    return '<section class="card">'
      + '<div class="card-head"><div><h2>' + MEDIA[t].label + '</h2>'
      + '<div class="mono sub">' + MEDIA[t].root + '</div></div>'
      + '<span class="chip' + (info.inside ? '' : ' is-out') + '">' + (info.inside ? 'In window' : 'Outside window') + '</span></div>'
      + '<div class="stats">'
      + stat('1 · Folders / files in scope', s.dirsEnabled + ' / ' + fmt(s.filesTotal), 'of ' + s.dirsTotal + ' folders · ' + fmt(s.filesAll) + ' files')
      + stat('2 · To do', fmt(s.pending), 'waiting for analysis or normalization')
      + stat('3 · Checked', fmt(s.checked), fmt(s.ok) + ' already at target')
      + stat('4 · Actually changed', fmt(s.changed), 'level set to ' + state.settings[t].target.toFixed(1) + ' LUFS')
      + '</div>'
      + '<p class="small muted" style="margin-top:12px">' + esc(info.text) + '</p>'
      + '</section>';
  }

  function renderDashboard() {
    const r = state.run;
    const rs = runStats();
    const label = { running: 'Running', paused: 'Paused', stopped: 'Stopped' }[r.status];
    const noTypes = !ALL.some((t) => r.types[t]);
    const outside = r.status === 'running' ? ALL.filter((t) => r.types[t] && !scheduleInfo(t).inside) : [];
    const sa = stats('audio');
    const sv = stats('video');
    const onoff = (b) => (b ? 'on' : 'off');
    const writeLabel = (t) => {
      const cfg = state.settings[t];
      if (cfg.writeMode === 'backup') return 'in place, backup kept';
      if (cfg.writeMode === 'output') return 'output folder';
      return 'in place, no backup';
    };
    const logs = state.log.slice(0, 14).map((e) =>
      '<li><time>' + pad2(e.t.getHours()) + ':' + pad2(e.t.getMinutes()) + ':' + pad2(e.t.getSeconds()) + '</time>'
      + '<span class="tag-type">' + (e.type ? MEDIA[e.type].label.slice(0, 3).toUpperCase() : '—') + '</span>'
      + '<span class="lv-' + e.level + '">' + esc(e.msg) + '</span></li>').join('');

    app().innerHTML = pageHead('Dashboard', 'Audio and video are tracked separately: each has its own folders, schedule, settings and counters.', '')
      + '<section class="card">'
      + '<div class="run-top"><div class="run-state"><span class="dot dot-' + r.status + '"></span>'
      + '<strong>' + label + '</strong>'
      + (r.status === 'stopped' && rs.total && rs.checked === rs.total ? '<span class="muted">· all files checked</span>' : '')
      + '</div><div class="btns">'
      + '<button class="btn btn-primary" data-action="start"' + (r.status !== 'stopped' || noTypes ? ' disabled' : '') + '>Start</button>'
      + '<button class="btn" data-action="pause"' + (r.status === 'stopped' ? ' disabled' : '') + '>' + (r.status === 'paused' ? 'Resume' : 'Pause') + '</button>'
      + '<button class="btn" data-action="stop"' + (r.status === 'stopped' ? ' disabled' : '') + '>Stop</button>'
      + '<a class="btn" href="#/directories">Browse folders</a>'
      + '</div></div>'
      + '<div class="progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' + rs.percent + '"><div style="width:' + rs.percent + '%"></div></div>'
      + '<div class="run-meta"><span><strong style="color:var(--text)">' + rs.percent + '%</strong> complete</span>'
      + '<span>' + fmt(rs.checked) + ' of ' + fmt(rs.total) + ' files checked</span>'
      + '<span class="mono">current: ' + (r.current ? esc(r.current) : '—') + '</span></div>'
      + '<div class="run-types"><span class="muted">Process</span>' + tip('Switches can be changed while the run is stopped. A switched-off type is not processed.')
      + ALL.map((t) => '<label class="inline"><input class="switch" type="checkbox" data-run-type="' + t + '"'
        + (r.types[t] ? ' checked' : '') + (r.status !== 'stopped' ? ' disabled' : '') + '> ' + MEDIA[t].label + '</label>').join('')
      + (noTypes ? '<span class="warn-text">Select at least one type to start.</span>' : '')
      + '</div>'
      + (outside.length ? '<div class="notice">Running outside the schedule of ' + outside.map((t) => MEDIA[t].label).join(' and ')
        + ' (started manually). Stop the run to return to the schedule.</div>' : '')
      + '</section>'
      + '<div class="split-2">' + mediaPanel('audio') + mediaPanel('video') + '</div>'
      + '<div class="split-2">'
      + '<section class="card"><h2 style="margin-bottom:12px">Needs attention</h2><dl class="rows">'
      + '<div><dt>Parked for review</dt><dd><a href="#/parked">Audio ' + sa.parked + ' · Video ' + sv.parked + '</a></dd></div>'
      + '<div><dt>Errors</dt><dd>' + (sa.errors + sv.errors) + '</dd></div>'
      + '<div><dt>Last run finished</dt><dd>' + (r.lastRun ? r.lastRun.toLocaleString('en-GB') : 'never') + '</dd></div>'
      + '<div><dt>Write mode</dt><dd>Audio: ' + writeLabel('audio') + ' · Video: ' + writeLabel('video') + '</dd></div>'
      + '<div><dt>Large-change override</dt><dd>Audio ' + onoff(state.settings.audio.override) + ' · Video ' + onoff(state.settings.video.override) + '</dd></div>'
      + '<div><dt>Dry run</dt><dd>' + (state.settings.general.dryRun ? 'on, nothing is written' : 'off') + '</dd></div>'
      + '</dl></section>'
      + '<section class="card"><div class="card-head"><h2>Activity</h2>'
      + '<button class="btn btn-sm" data-action="clear-log"' + (state.log.length ? '' : ' disabled') + '>Clear list</button></div><ul class="log">'
      + (logs || '<li><span class="muted">No activity yet.</span></li>') + '</ul></section>'
      + '</div>';
  }

  /* baseline: check the level of one file and use it as the target */
  const baselineValue = (lufs) => Math.max(-30, Math.min(-10, Math.round(lufs * 2) / 2));
  function baselinePanel(t) {
    const m = state.measure && state.measure.media === t ? state.measure : null;
    const target = state.settings[t].target;
    let html = '';
    if (state.baselineFrom === t) {
      html += '<div class="notice info baseline-banner"><span><strong>Setting the baseline for Settings, Level.</strong> '
        + 'Check the level of a representative file below, then use it as the target. The target is now '
        + '<strong>' + target.toFixed(1) + ' LUFS</strong>.</span>'
        + '<a class="btn btn-sm" href="#/settings" data-action="baseline-done">Back to Settings</a></div>';
    }
    if (m && m.state === 'measuring') {
      html += '<div class="measure"><span>Measuring <strong>' + esc(m.name) + '</strong> … (integrated loudness, EBU R128, file not changed)</span></div>';
    } else if (m) {
      const r = Math.round(m.lufs * 2) / 2;
      const v = baselineValue(m.lufs);
      html += '<div class="measure">'
        + '<div><div class="stat-label">Checked file</div><strong>' + esc(m.name) + '</strong></div>'
        + '<div><div class="stat-label">Integrated loudness</div><strong>' + m.lufs.toFixed(1) + ' LUFS</strong></div>'
        + '<div><div class="stat-label">True peak</div><strong>' + m.tp.toFixed(1) + ' dBTP</strong></div>'
        + '<div><div class="stat-label">Change needed at target ' + target.toFixed(1) + '</div><strong>' + fmtDb(target - m.lufs) + '</strong></div>'
        + '<span class="grow"></span>'
        + '<button class="btn btn-primary" data-action="use-baseline">Use ' + v.toFixed(1) + ' LUFS as target' + (v !== r ? ' (slider limit)' : '') + '</button>'
        + '<button class="btn" data-action="measure-close">Dismiss</button></div>';
    }
    return html;
  }

  /* ----------------------------------------------------------- directories */
  function renderTree(nodes) {
    return nodes.map((n) => {
      const st = subtreeState(n);
      const total = subtreeNodes(n).reduce((a, x) => a + x.files.length, 0);
      const hasKids = n.children.length > 0;
      const active = state.selected[n.type] === n.id;
      return '<li class="' + (n.enabled ? '' : 'disabled-node') + '">'
        + '<div class="tree-row">'
        + '<button class="caret' + (hasKids ? '' : ' leaf') + '" data-action="tree-toggle" data-id="' + esc(n.id) + '" aria-label="Expand or collapse ' + esc(n.name) + '">'
        + (n.open ? '▾' : '▸') + '</button>'
        + '<input type="checkbox" data-check="' + esc(n.id) + '" aria-label="Include ' + esc(n.name) + '"' + (st === 'on' ? ' checked' : '') + '>'
        + '<button class="folder' + (active ? ' active' : '') + '" data-action="tree-browse" data-id="' + esc(n.id) + '">'
        + esc(n.name) + '</button>'
        + '<span class="count">' + fmt(total) + ' files</span>'
        + '</div>'
        + (hasKids && n.open ? '<ul>' + renderTree(n.children) + '</ul>' : '')
        + '</li>';
    }).join('');
  }

  function renderBrowse(node) {
    const t = node.type;
    const rows = node.files.slice(0, 80).map((f) => {
      const g = gainFor(t, f);
      return '<tr><td>' + esc(f.name) + '</td><td class="mono">' + f.ext + '</td>'
        + '<td class="num">' + f.lufs.toFixed(1) + ' LUFS</td>'
        + '<td class="num">' + fmtDb(g) + '</td>'
        + '<td class="dim">' + esc(methodFor(t, f)) + '</td>'
        + '<td>' + statusLabel(f.status) + '</td>'
        + '<td class="actions-cell"><button class="btn btn-sm" data-action="check-level" data-file="' + esc(f.id) + '">Check level</button></td></tr>';
    }).join('');
    const subs = subtreeNodes(node).length - 1;
    return '<div class="card-head" style="margin-bottom:8px"><div><h2>' + esc(MEDIA[t].root + '/' + node.rel) + '</h2>'
      + '<div class="sub">' + node.files.length + ' files directly in this folder · ' + subs + ' sub-folders</div></div>'
      + '<span class="chip' + (node.enabled ? '' : ' is-out') + '">' + (node.enabled ? 'Included' : 'Excluded') + '</span></div>'
      + (node.files.length
        ? '<div class="table-wrap"><table><thead><tr><th>File</th><th>Format</th><th class="num">Level' + tip('Integrated loudness of the whole file (EBU R128), measured without changing the file. Check level measures one file again.') + '</th>'
          + '<th class="num">Change</th><th>Method</th><th>Status</th><th></th></tr></thead><tbody>' + rows + '</tbody></table></div>'
          + (node.files.length > 80 ? '<p class="small muted" style="margin-top:10px">… and ' + (node.files.length - 80) + ' more files</p>' : '')
        : '<p class="muted">No files directly in this folder. Open a sub-folder in the tree.</p>');
  }

  function renderDirectories() {
    const t = state.media;
    const s = stats(t);
    const sel = findNode(state.selected[t]) || state.trees[t][0];
    app().innerHTML = pageHead('Directories',
      'Base location <span class="mono">' + MEDIA[t].root + '</span>, mounted from <span class="mono">' + MEDIA[t].host
      + '</span> in docker-compose.yml. Untick a folder to exclude it. Ticking or unticking applies to its sub-folders too.',
      '<button class="btn btn-sm" data-action="select-all">Select all</button>'
      + '<button class="btn btn-sm" data-action="select-none">Select none</button>'
      + '<button class="btn btn-sm" data-action="expand-all">Expand all</button>'
      + '<button class="btn btn-sm" data-action="collapse-all">Collapse all</button>', true)
      + '<p class="muted" style="margin-bottom:0"><strong style="color:var(--text)">' + s.dirsEnabled + ' of ' + s.dirsTotal
      + '</strong> folders included · ' + fmt(s.filesTotal) + ' files in scope</p>'
      + baselinePanel(t)
      + '<div class="split-dirs">'
      + '<section class="card tree" aria-label="Folder tree"><ul>' + renderTree(state.trees[t]) + '</ul></section>'
      + '<section class="card">' + renderBrowse(sel) + '</section>'
      + '</div>';
    app().querySelectorAll('input[data-check]').forEach((cb) => {
      const n = findNode(cb.dataset.check);
      cb.indeterminate = !!n && subtreeState(n) === 'mixed';
    });
  }

  /* ---------------------------------------------------------------- parked */
  function parkedIds(t) {
    return entriesOf([t]).filter((e) => e.file.status === 'parked').map((e) => e.file.id);
  }
  const PARK_RANGE = 12;                                   // dB, both ways
  const tenth = (v) => Math.round(v * 10) / 10;
  const effGain = (t, file) => tenth(gainFor(t, file) + (file.adjust || 0));
  function setNewValue(t, file, v) {
    const nv = Math.max(-PARK_RANGE, Math.min(PARK_RANGE, tenth(v)));
    file.adjust = tenth(nv - gainFor(t, file));
  }

  const PARK_SORTS = {
    order: { label: 'File order', cmp: null },
    asc: { label: 'Change needed, lowest first', cmp: (x, y) => x - y },
    desc: { label: 'Change needed, highest first', cmp: (x, y) => y - x },
    size: { label: 'Size of change, largest first', cmp: (x, y) => Math.abs(y) - Math.abs(x) },
  };
  function sortParked(list, t) {
    const s = PARK_SORTS[state.parkSort];
    if (!s || !s.cmp) return;
    list.sort((a, b) => s.cmp(effGain(t, a.file), effGain(t, b.file)));
  }

  function renderParked() {
    const t = state.media;
    const th = state.settings[t].threshold;
    const list = entriesOf([t]).filter((e) => e.file.status === 'parked');
    sortParked(list, t);
    const ids = list.map((e) => e.file.id);
    state.parkSel = state.parkSel.filter((id) => ids.includes(id));
    const sel = state.parkSel;
    const rows = list.map(({ node, file }) => {
      const orig = gainFor(t, file);
      const g = effGain(t, file);
      const on = sel.includes(file.id);
      const fid = esc(file.id);
      const segs = node.rel.split('/').filter(Boolean);
      const folder = segs.slice(-2).join('/') || node.name;
      const full = MEDIA[t].root + '/' + node.rel;
      const warn = Math.abs(g) > th;
      return '<tr' + (on ? ' class="row-sel"' : '') + '>'
        + '<td class="pick"><input type="checkbox" data-park-check="' + fid + '" aria-label="Select ' + esc(file.name) + '"' + (on ? ' checked' : '') + '></td>'
        + '<td class="file-cell">' + esc(file.name) + '</td>'
        + '<td class="dim mono folder-cell" title="' + esc(full) + '">' + esc(folder) + '</td>'
        + '<td class="num">' + file.lufs.toFixed(1) + ' LUFS</td>'
        + '<td class="chg"><div class="chg-vals">'
        + '<span class="chg-orig" title="Original change">' + fmtDb(orig) + '</span>'
        + '<span class="chg-arrow" aria-hidden="true">→</span>'
        + '<strong class="chg-new' + (warn ? ' warn' : '') + '" data-park-new="' + fid + '">' + fmtDb(g) + '</strong></div>'
        + '<div class="chg-ctl">'
        + '<button class="btn btn-sm" data-action="adjust-file" data-file="' + fid + '" data-step="-0.1" aria-label="Decrease change by 0.1 dB">−</button>'
        + '<input class="mini" type="range" min="-12" max="12" step="0.1" value="' + g + '" data-park-slider="' + fid + '" aria-label="Change for ' + esc(file.name) + '">'
        + '<button class="btn btn-sm" data-action="adjust-file" data-file="' + fid + '" data-step="0.1" aria-label="Increase change by 0.1 dB">+</button>'
        + '</div></td>'
        + '<td class="actions"><button class="btn btn-sm btn-primary" data-action="approve" data-file="' + fid + '">Apply change</button>'
        + '<button class="btn btn-sm" data-action="skip" data-file="' + fid + '">Skip</button></td></tr>';
    }).join('');
    const allOn = list.length > 0 && sel.length === list.length;
    const bulk = sel.length
      ? '<div class="bulk-bar"><span><strong>' + sel.length + '</strong> selected</span>'
        + '<span class="bulk-steps"><button class="btn btn-sm" data-action="bulk-adjust" data-step="-0.1">− 0.1 dB</button>'
        + '<button class="btn btn-sm" data-action="bulk-adjust" data-step="0.1">+ 0.1 dB</button></span>'
        + '<span class="grow"></span>'
        + '<button class="btn btn-sm btn-primary" data-action="apply-selected">Apply selected</button>'
        + '<button class="btn btn-sm" data-action="clear-selection">Clear selection</button></div>'
      : '';
    const sortOpts = Object.keys(PARK_SORTS).map((k) => '<option value="' + k + '"' + (state.parkSort === k ? ' selected' : '') + '>' + PARK_SORTS[k].label + '</option>').join('');
    const sortBar = '<div class="card-tools"><label for="park-sort">Sort by</label><select id="park-sort" class="input" data-park-sort>' + sortOpts + '</select></div>';
    app().innerHTML = pageHead('Parked files',
      MEDIA[t].label + ' changes larger than <strong>' + th + ' dB</strong> are not applied. The run continued with the next file. Adjust a change, then apply it.',
      '<button class="btn btn-primary" data-action="approve-all"' + (list.length ? '' : ' disabled') + '>Apply all (' + list.length + ')</button>'
      + '<a class="btn" href="#/settings">Threshold</a>', true)
      + '<section class="card">'
      + (list.length
        ? sortBar + bulk + '<div class="table-wrap"><table class="parked"><colgroup>'
          + '<col class="c-pick"><col class="c-file"><col class="c-folder"><col class="c-level"><col class="c-change"><col class="c-act">'
          + '</colgroup><thead><tr>'
          + '<th class="pick"><input type="checkbox" data-park-all aria-label="Select all parked files"' + (allOn ? ' checked' : '') + '></th>'
          + '<th>File</th><th>Folder</th><th class="num">Level</th><th>Change needed <span class="dim">(original → new)</span></th><th></th></tr></thead><tbody>' + rows + '</tbody></table></div>'
        : '<p class="muted">Nothing is parked in ' + MEDIA[t].label.toLowerCase() + '. Every checked file is within the threshold.</p>')
      + '</section>';
  }

  /* -------------------------------------------------------------- schedule */
  function renderSchedule() {
    const t = state.media;
    const sc = state.schedule[t];
    const hours = Array.from({ length: 24 }, (_, h) => h);
    const head = '<tr><th></th>' + hours.map((h) => '<th>' + pad2(h) + '</th>').join('') + '</tr>';
    const body = DAYS.map((d, di) => '<tr><td class="dayname">' + d + '</td>'
      + hours.map((h) => '<td class="cell' + (sc[di][h] ? ' on' : '') + '" data-d="' + di + '" data-h="' + h
        + '" title="' + d + ' ' + pad2(h) + ':00"></td>').join('') + '</tr>').join('');
    app().innerHTML = pageHead('Schedule',
      MEDIA[t].label + ' is processed only inside the green windows. Click or drag over the grid to change hours. Times are local (Europe/Amsterdam).',
      '', true)
      + '<section class="card">'
      + '<div class="btns" style="margin-bottom:14px">'
      + '<span class="muted small" style="margin-right:4px">Presets</span>'
      + '<button class="btn btn-sm" data-action="preset" data-preset="nights">Nights 22–06</button>'
      + '<button class="btn btn-sm" data-action="preset" data-preset="weeknights">Weeknights</button>'
      + '<button class="btn btn-sm" data-action="preset" data-preset="weekends">Weekends</button>'
      + '<button class="btn btn-sm" data-action="preset" data-preset="always">Always</button>'
      + '<button class="btn btn-sm" data-action="preset" data-preset="never">Never</button>'
      + '</div>'
      + '<div class="sched-wrap"><table class="sched"><thead>' + head + '</thead><tbody>' + body + '</tbody></table></div>'
      + '<div class="legend"><span><span class="swatch" style="background:var(--accent)"></span>processing allowed</span>'
      + '<span><span class="swatch" style="background:#dcdcd8"></span>paused</span>'
      + '<span id="sched-now"></span></div>'
      + '</section>'
      + '<section class="card"><h2 style="margin-bottom:10px">Windows per day</h2><ul class="sched-list" id="sched-summary"></ul></section>';
    updateScheduleInfo();
  }

  function updateScheduleInfo() {
    const t = state.media;
    const list = document.getElementById('sched-summary');
    if (list) {
      list.innerHTML = DAYS.map((d, i) => '<li><span class="muted">' + d + '</span><span>' + dayRanges(t, i) + '</span></li>').join('');
    }
    const now = document.getElementById('sched-now');
    if (now) now.textContent = scheduleInfo(t).text;
  }

  /* ---------------------------------------------------------- normalization */
  function writeOptions(t) {
    const cfg = state.settings[t];
    const opts = [
      ['inplace', 'Replace in place (default)', 'After verification the file is replaced. No copy of the original is kept.'],
      ['backup', 'Replace in place, keep a backup', 'The original is copied to /config/backups first. Needs extra disk space.'],
      ['output', 'Write to a separate output folder', 'The library is never touched. Normalized copies go to the folder below.'],
    ].map(([v, title, d]) => '<label class="opt"><input type="radio" name="writeMode-' + t + '" value="' + v + '"'
      + ' data-scope="' + t + '" data-setting="writeMode"' + (cfg.writeMode === v ? ' checked' : '') + '>'
      + '<span><strong style="font-weight:500">' + title + '</strong>' + tip(d) + '</span></label>').join('');
    const outField = cfg.writeMode === 'output'
      ? '<div class="field" style="margin:4px 0 0 28px"><label class="lbl" for="out-' + t + '">Output folder (inside the container)' + tip('Mount it in docker-compose.yml, for example /volume1/' + t + '-normalized:' + cfg.outputDir) + '</label>'
        + '<input class="input" id="out-' + t + '" data-scope="' + t + '" data-setting="outputDir" value="' + esc(cfg.outputDir) + '">'
        + '</div>'
      : '';
    return '<div class="field"><span class="lbl">How changed files are written</span>' + opts + outField + '</div>';
  }

  function levelCard(t) {
    const cfg = state.settings[t];
    return '<section class="card"><div class="card-head"><div><h2>Level</h2><div class="sub">Applies to ' + MEDIA[t].label.toLowerCase() + ' files</div></div></div>'
      + '<div class="field"><label class="lbl" for="target-' + t + '">Target loudness (integrated, EBU R128)' + tip('−16 LUFS suits podcasts and speech, −14 suits music streaming, −23 is the EBU broadcast standard.') + '</label>'
      + '<div class="slider-row"><input id="target-' + t + '" type="range" min="-30" max="-10" step="0.5" value="' + cfg.target + '" data-scope="' + t + '" data-setting="target">'
      + '<div class="val"><span data-out="' + t + '.target">' + cfg.target.toFixed(1) + '</span> LUFS</div></div>'
      + '<p class="baseline-link-row"><a class="inline-link" href="#/directories" data-action="baseline-start" data-media="' + t + '">Set the baseline from a file: check its level in Directories →</a></p>'
      + '</div>'
      + '<div class="field"><label class="lbl" for="tp-' + t + '">True-peak ceiling' + tip('The gain is capped so that no sample exceeds this level, which prevents clipping.') + '</label>'
      + '<div class="slider-row"><input id="tp-' + t + '" type="range" min="-6" max="-1" step="0.1" value="' + cfg.truePeak + '" data-scope="' + t + '" data-setting="truePeak">'
      + '<div class="val"><span data-out="' + t + '.truePeak">' + cfg.truePeak.toFixed(1) + '</span> dBTP</div></div>'
      + '</div>'
      + '<div class="field"><label class="lbl" for="th-' + t + '">Large-change threshold' + tip('Changes bigger than this are parked for review instead of being applied.') + '</label>'
      + '<div class="slider-row"><input id="th-' + t + '" type="range" min="1" max="12" step="0.5" value="' + cfg.threshold + '" data-scope="' + t + '" data-setting="threshold">'
      + '<div class="val"><span data-out="' + t + '.threshold">' + cfg.threshold.toFixed(1) + '</span> dB</div></div>'
      + '</div>'
      + '<label class="check"><input class="switch" type="checkbox" data-scope="' + t + '" data-setting="override"' + (cfg.override ? ' checked' : '') + '>'
      + '<span><strong>Override the confirmation for large changes</strong>' + tip('Apply changes above the threshold automatically, without parking them.') + '</span></label>'
      + (cfg.override ? '<div class="notice">Large ' + MEDIA[t].label.toLowerCase() + ' changes will be applied without review.</div>' : '')
      + '</section>';
  }

  function outputCard(t) {
    if (t === 'audio') {
      const cfg = state.settings.audio;
      const lossy = [
        ['gain', 'Gain only, no re-encode', 'MP3 via mp3gain, AAC via aacgain. The audio data is untouched; level steps are about 1.5 dB, so the target is hit within ±0.75 dB.', true],
        ['reencode', 'Re-encode at the same codec and bitrate', 'Exact target level, but each re-encode adds a small generation loss.', false],
        ['skip-lossy', 'Skip lossy files', 'Only lossless files (FLAC, WAV) are normalized. MP3, AAC, WMA and OGG are left alone.', false],
      ].map(([v, title, d, def]) => '<label class="opt"><input type="radio" name="lossy" value="' + v + '" data-scope="audio" data-setting="lossy"'
        + (cfg.lossy === v ? ' checked' : '') + '><span><strong style="font-weight:500">' + title + '</strong>'
        + (def ? '<span class="tag">default</span>' : '') + tip(d) + '</span></label>').join('');
      return '<section class="card"><div class="card-head"><div><h2>Output</h2><div class="sub">Audio files</div></div></div>'
        + '<div class="field"><span class="lbl">Lossy formats (MP3, AAC, WMA, OGG, M4A)</span>' + lossy + '</div>'
        + writeOptions('audio') + '</section>';
    }
    const planned = '<label class="opt"><input type="radio" name="video-stream" checked disabled>'
      + '<span><strong style="font-weight:500">Copy the video stream</strong>' + tip('Used in this version. The video is never decoded or re-encoded.') + '</span></label>'
      + '<label class="opt"><input type="radio" name="video-stream" disabled>'
      + '<span><strong style="font-weight:500">Re-encode video on the GPU (VAAPI)</strong><span class="tag">planned</span>' + tip('Possible later, as a separate option. Not available yet.') + '</span></label>';
    return '<section class="card"><div class="card-head"><div><h2>Output</h2><div class="sub">Video files (AVI, MKV, MP4)</div></div></div>'
      + '<div class="field"><span class="lbl">Video stream</span>' + planned + '</div>'
      + '<div class="notice info" style="margin:0 0 20px">The audio track is re-encoded with its original codec and bitrate, so the video is never touched.</div>'
      + writeOptions('video') + '</section>';
  }

  function scheduleOptions() {
    return '<section class="card" style="margin-top:16px"><div class="card-head"><div><h2>Scheduling</h2>'
      + '<div class="sub">Applies to audio and video. The time windows are set on the Schedule tab.</div></div></div>'
      + '<div class="split-2" style="margin-top:0">'
      + '<div class="field" style="margin:0"><label class="lbl" for="scan-every">Look for new or changed files every' + tip('New files are found automatically, also after a restart of the app.') + '</label>'
      + '<div class="input-row" style="max-width:300px"><input class="input" id="scan-every" type="number" min="1" max="1440" data-sched="scanEvery" value="' + state.scanEvery + '">'
      + '<span class="muted small">minutes</span></div>'
      + '</div>'
      + '<div class="field" style="margin:0"><span class="lbl">When a window closes during a file</span>'
      + '<label class="opt"><input type="radio" name="onclose" value="finish" data-sched="onClose"' + (state.onClose === 'finish' ? ' checked' : '') + '>'
      + '<span><strong style="font-weight:500">Finish the current file, then pause</strong>' + tip('Recommended. No half-written files.') + '</span></label>'
      + '<label class="opt"><input type="radio" name="onclose" value="now" data-sched="onClose"' + (state.onClose === 'now' ? ' checked' : '') + '>'
      + '<span><strong style="font-weight:500">Pause immediately</strong>' + tip('The temporary file is discarded and retried in the next window.') + '</span></label>'
      + '</div></div></section>';
  }

  function renderSettings() {
    const t = state.media;
    const g = state.settings.general;
    app().innerHTML = pageHead('Settings',
      'Settings for each media type. Level and output are separate for audio and video. Save applies to the config volume (not persisted in this mock-up).',
      '<button class="btn" data-action="settings-reset">Reset ' + MEDIA[t].label.toLowerCase() + '</button>'
      + '<button class="btn btn-primary" data-action="settings-save">Save</button>', true)
      + '<section class="card"><div class="check-list">'
      + '<label class="check" style="padding-top:0"><input class="switch" type="checkbox" data-scope="general" data-setting="dryRun"' + (g.dryRun ? ' checked' : '') + '>'
      + '<span><strong>Dry run</strong>' + tip('Analyse and report only. Nothing is written. Recommended for the first run on a real library.') + '</span></label>'
      + '<p class="small muted" style="padding-top:12px;border-top:1px solid var(--line)">Web access has no login (local network only, as chosen). '
      + 'Do not forward the port to the internet. The Home Assistant API still requires its access token.</p>'
      + '</div></section>'
      + scheduleOptions()
      + '<div class="split-2">' + levelCard(t) + outputCard(t) + '</div>';
  }

  /* ------------------------------------------------------------- Home Assistant */
  function renderHA() {
    const h = state.ha;
    let lastGroup = null;
    const sensorRows = h.sensors.map((x, i) => {
      const head = x.group !== lastGroup ? '<tr><td colspan="5" class="group-row">' + esc(x.group) + '</td></tr>' : '';
      lastGroup = x.group;
      return head + '<tr><td><input type="checkbox" data-sensor="' + i + '" data-field="enabled"' + (x.enabled ? ' checked' : '') + ' aria-label="Enable ' + esc(x.name) + '"></td>'
        + '<td><input class="input" data-sensor="' + i + '" data-field="name" value="' + esc(x.name) + '"></td>'
        + '<td class="mono dim">' + x.path + '</td>'
        + '<td>' + (x.unit ? esc(x.unit) : '<span class="dim">—</span>') + '</td>'
        + '<td>' + (x.deviceClass || '<span class="dim">—</span>') + '</td></tr>';
    }).join('');

    app().innerHTML = pageHead('Home Assistant',
      'Publishes the normalizer status as a local, token-protected API. Home Assistant reads it with RESTful sensors, and the Lovelace card shows every value.',
      '<button class="btn btn-primary" data-action="ha-save">Save</button>')
      + '<div class="split-2" style="margin-top:0">'
      + '<section class="card"><h2 style="margin-bottom:14px">Connection</h2>'
      + '<label class="check" style="padding-top:0"><input class="switch" type="checkbox" data-ha="enabled"' + (h.enabled ? ' checked' : '') + '>'
      + '<span><strong>Enable the Home Assistant API</strong>' + tip('Requests without a valid token are refused.') + '</span></label>'
      + '<div class="field" style="margin-top:14px"><label class="lbl" for="ha-url">Home Assistant URL</label>'
      + '<input class="input" id="ha-url" data-ha="url" value="' + esc(h.url) + '"></div>'
      + '<div class="field"><label class="lbl" for="ha-token">Long-lived access token' + tip('Create it in Home Assistant under Profile, then Security, then Long-lived access tokens. Stored in the config volume and never written to logs.') + '</label>'
      + '<div class="input-row"><input class="input" id="ha-token" data-ha="token" autocomplete="off" type="' + (h.showToken ? 'text' : 'password') + '" value="' + esc(h.token) + '">'
      + '<button class="btn" data-action="ha-show">' + (h.showToken ? 'Hide' : 'Show') + '</button></div>'
      + '</div>'
      + '<div class="btns"><button class="btn" data-action="ha-test">Test connection</button>'
      + '<span class="muted small">' + esc(h.testMsg) + '</span></div>'
      + '</section>'
      + '<section class="card"><h2 style="margin-bottom:14px">Local API endpoint</h2>'
      + '<pre>GET http://&lt;NAS-IP&gt;:8080/api/v1/ha/status\nAuthorization: Bearer &lt;token&gt;</pre>'
      + '<h2 style="margin:18px 0 10px">Current payload</h2><pre id="ha-json"></pre></section>'
      + '</div>'

      + '<section class="card" style="margin-top:16px"><div class="card-head"><div><h2>Lovelace card</h2>'
      + '<div class="sub">One file that shows every parameter on a Home Assistant dashboard. No extra software.</div></div>'
      + '<a class="btn btn-primary" href="lovelace/audio-normalizer-card.js" download="audio-normalizer-card.js">Download card (.js)</a></div>'
      + '<div class="lovelace-wrap">'
      + '<div><h2 style="margin-bottom:10px">Install in 3 steps' + tip('Also on GitHub: lovelace/audio-normalizer-card.js. Details and troubleshooting: lovelace/README.md.') + '</h2>'
      + '<ol class="steps">'
      + '<li>Copy the file to <span class="mono">/config/www/audio-normalizer-card.js</span></li>'
      + '<li>Settings → Dashboards → Resources → add <span class="mono">/local/audio-normalizer-card.js</span>, type <em>JavaScript module</em></li>'
      + '<li>Edit a dashboard → add card → Audio Normalizer, or paste this YAML:</li>'
      + '</ol>'
      + '<pre id="ha-card-yaml" style="margin-top:10px"></pre>'
      + '</div>'
      + '<div><h2 style="margin-bottom:10px">Preview (shows the enabled sensors)' + tip('The preview uses the same file you download. It shows only the sensors enabled below and updates when you change them.') + '</h2>'
      + '<div class="preview-stage"><audio-normalizer-card id="card-preview"></audio-normalizer-card></div>'
      + '</div>'
      + '</div></section>'

      + '<section class="card" style="margin-top:16px"><div class="card-head"><div><h2>Sensors</h2>'
      + '<div class="sub">Optional single sensors in addition to the main entity. The main entity is always created and feeds the card.</div></div></div>'
      + '<div class="table-wrap"><table><thead><tr><th>On</th><th>Sensor name</th><th>Source in payload</th><th>Unit</th><th>Device class</th></tr></thead>'
      + '<tbody>' + sensorRows + '</tbody></table></div></section>'
      + '<section class="card" style="margin-top:16px"><h2 style="margin-bottom:12px">Generated configuration.yaml</h2>'
      + '<pre id="ha-yaml"></pre></section>';
    refreshHaPreview();
  }

  /* -------------------------------------------------------------- routing */
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
    document.getElementById('parkedBadge').textContent = stats('audio').parked + stats('video').parked;
  }

  function render() {
    const route = currentRoute();
    document.querySelectorAll('#nav a').forEach((a) => {
      if (a.dataset.route === route) a.setAttribute('aria-current', 'page');
      else a.removeAttribute('aria-current');
    });
    updateNavBadge();
    ROUTES[route]();
    document.querySelectorAll('input[type=range]').forEach(paintRange);
  }

  /* --------------------------------------------------------- interaction */
  document.addEventListener('click', (e) => {
    const el = e.target.closest('[data-action]');
    if (!el || el.disabled) return;
    const a = el.dataset.action;
    const r = state.run;
    const t = state.media;

    if (a === 'media') {
      state.media = el.dataset.media;
    } else if (a === 'start') {
      if (r.status === 'stopped' && ALL.some((x) => r.types[x])) {
        r.status = 'running';
        log('info', null, 'Run started manually');
      }
    } else if (a === 'pause') {
      r.status = r.status === 'running' ? 'paused' : 'running';
      log('info', null, r.status === 'paused' ? 'Run paused' : 'Run resumed');
    } else if (a === 'stop') {
      r.status = 'stopped';
      r.current = null;
      log('info', null, 'Run stopped by user');
    } else if (a === 'tree-toggle') {
      const n = findNode(el.dataset.id); if (n) n.open = !n.open;
    } else if (a === 'tree-browse') {
      const n = findNode(el.dataset.id);
      if (n) state.selected[n.type] = n.id;
    } else if (a === 'select-all' || a === 'select-none') {
      nodesOf([t]).forEach((n) => { n.enabled = a === 'select-all'; });
    } else if (a === 'expand-all' || a === 'collapse-all') {
      nodesOf([t]).forEach((n) => { n.open = a === 'expand-all'; });
    } else if (a === 'approve' || a === 'skip') {
      const entry = entriesOf(ALL).find((x) => x.file.id === el.dataset.file);
      if (entry) {
        entry.file.status = a === 'approve' ? 'changed' : 'skipped';
        log(a === 'approve' ? 'ok' : 'info', entry.node.type, (a === 'approve' ? 'Change applied: ' : 'Skipped: ') + entry.file.name + (a === 'approve' ? ' (' + fmtDb(effGain(entry.node.type, entry.file)) + ')' : ''));
      }
    } else if (a === 'approve-all') {
      entriesOf([t]).forEach((x) => { if (x.file.status === 'parked') x.file.status = 'changed'; });
      log('ok', t, 'All parked ' + MEDIA[t].label.toLowerCase() + ' files approved and applied');
    } else if (a === 'preset') {
      const fn = PRESETS[el.dataset.preset];
      state.schedule[t] = scheduleFrom(fn);
    } else if (a === 'check-level') {
      const entry = entriesOf(ALL).find((x) => x.file.id === el.dataset.file);
      if (entry) {
        const f = entry.file;
        const mm = { id: f.id, media: entry.node.type, name: f.name, state: 'measuring' };
        state.measure = mm;
        setTimeout(() => {
          if (state.measure !== mm) return;                     // dismissed or replaced meanwhile
          mm.state = 'done';
          mm.lufs = f.lufs;
          mm.tp = Math.min(-0.1, Math.round((f.lufs + 9 + (f.name.length % 7) * 0.4) * 10) / 10);
          log('info', mm.media, 'Level checked: ' + f.name + ' ' + f.lufs.toFixed(1) + ' LUFS');
          render();
        }, 900);
      }
    } else if (a === 'measure-close') {
      state.measure = null;
    } else if (a === 'use-baseline') {
      const m = state.measure;
      if (m && m.state === 'done') {
        const v = baselineValue(m.lufs);
        state.settings[m.media].target = v;
        log('ok', m.media, 'Target loudness set to ' + v.toFixed(1) + ' LUFS from ' + m.name);
        toast('Target loudness set to ' + v.toFixed(1) + ' LUFS');
      }
    } else if (a === 'baseline-start') {
      state.media = el.dataset.media;
      state.baselineFrom = el.dataset.media;
    } else if (a === 'baseline-done') {
      state.baselineFrom = null;
    } else if (a === 'clear-log') {
      state.log = [];
    } else if (a === 'adjust-file') {
      const entry = entriesOf(ALL).find((x) => x.file.id === el.dataset.file);
      if (entry) setNewValue(entry.node.type, entry.file, effGain(entry.node.type, entry.file) + Number(el.dataset.step));
    } else if (a === 'bulk-adjust') {
      entriesOf(ALL).forEach((x) => {
        if (state.parkSel.includes(x.file.id)) setNewValue(x.node.type, x.file, effGain(x.node.type, x.file) + Number(el.dataset.step));
      });
    } else if (a === 'apply-selected') {
      let n = 0;
      entriesOf(ALL).forEach((x) => {
        if (state.parkSel.includes(x.file.id) && x.file.status === 'parked') { x.file.status = 'changed'; n += 1; }
      });
      state.parkSel = [];
      if (n) log('ok', t, 'Applied ' + n + ' selected ' + MEDIA[t].label.toLowerCase() + ' file' + (n === 1 ? '' : 's'));
    } else if (a === 'clear-selection') {
      state.parkSel = [];
    } else if (a === 'settings-save') {
      toast('Settings saved (mock-up: not persisted)');
      return;
    } else if (a === 'settings-reset') {
      state.settings[t] = clone(DEFAULTS[t]);
    } else if (a === 'ha-show') {
      state.ha.showToken = !state.ha.showToken;
    } else if (a === 'ha-test') {
      state.ha.testMsg = 'Connected to Home Assistant 2026.10 (simulated)';
    } else if (a === 'ha-save') {
      toast('Home Assistant settings saved (mock-up: not persisted)');
      return;
    }
    render();
  });

  function onField(e) {
    const el = e.target;
    if (el.type === 'range') paintRange(el);

    if (el.dataset.check !== undefined) {
      const n = findNode(el.dataset.check);
      if (n) subtreeNodes(n).forEach((x) => { x.enabled = el.checked; });
      if (e.type === 'change') render();
      return;
    }

    if (el.dataset.parkSlider !== undefined) {
      const entry = entriesOf(ALL).find((x) => x.file.id === el.dataset.parkSlider);
      if (entry) {
        setNewValue(entry.node.type, entry.file, Number(el.value));
        const out = document.querySelector('[data-park-new="' + el.dataset.parkSlider + '"]');
        if (out) out.textContent = fmtDb(effGain(entry.node.type, entry.file));
      }
      if (e.type === 'change') render();
      return;
    }

    if (el.dataset.parkSort !== undefined) {
      if (e.type === 'change') { state.parkSort = el.value; render(); }
      return;
    }

    if (el.dataset.parkCheck !== undefined || el.dataset.parkAll !== undefined) {
      if (e.type === 'change') {
        if (el.dataset.parkAll !== undefined) {
          state.parkSel = el.checked ? parkedIds(state.media) : [];
        } else {
          const id = el.dataset.parkCheck;
          state.parkSel = el.checked ? state.parkSel.concat(id) : state.parkSel.filter((x) => x !== id);
        }
        render();
      }
      return;
    }

    if (el.dataset.runType) {
      if (e.type === 'change') { state.run.types[el.dataset.runType] = el.checked; render(); }
      return;
    }

    if (el.dataset.setting) {
      if (el.type === 'radio' && !el.checked) return;
      const bucket = state.settings[el.dataset.scope];
      const key = el.dataset.setting;
      if (el.type === 'range') {
        const v = parseFloat(el.value);
        bucket[key] = v;
        const out = document.querySelector('[data-out="' + el.dataset.scope + '.' + key + '"]');
        if (out) out.textContent = v.toFixed(1);
        return;
      }
      bucket[key] = el.type === 'checkbox' ? el.checked : el.value;
      if (e.type === 'change') render();
      return;
    }

    if (el.dataset.ha) {
      state.ha[el.dataset.ha] = el.type === 'checkbox' ? el.checked : el.value;
      refreshHaPreview();
      return;
    }

    if (el.dataset.sensor !== undefined) {
      const x = state.ha.sensors[Number(el.dataset.sensor)];
      if (x) x[el.dataset.field] = el.type === 'checkbox' ? el.checked : el.value;
      refreshHaPreview();
      return;
    }

    if (el.dataset.sched) {
      if (el.type === 'radio' && !el.checked) return;
      if (el.dataset.sched === 'scanEvery') {
        state.scanEvery = Math.min(1440, Math.max(1, Math.round(Number(el.value)) || 1));
      } else {
        state.onClose = el.value;
      }
    }
  }
  document.addEventListener('input', onField);
  document.addEventListener('change', onField);

  /* schedule grid: click or drag to paint */
  let painting = null;
  function paintCell(cell) {
    const d = Number(cell.dataset.d), h = Number(cell.dataset.h);
    state.schedule[state.media][d][h] = painting;
    cell.classList.toggle('on', painting);
  }
  app().addEventListener('pointerdown', (e) => {
    const cell = e.target.closest('.cell');
    if (!cell) return;
    e.preventDefault();
    painting = !state.schedule[state.media][Number(cell.dataset.d)][Number(cell.dataset.h)];
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

  /* the info icon must not toggle the checkbox or radio of its label */
  document.addEventListener('click', (e) => {
    if (e.target.closest && e.target.closest('.tip')) e.preventDefault();
  });

  /* ------------------------------------------------------------------- menu */
  const MENU_KEY = 'audio-normalizer-menu';
  const menu = (() => {
    try { return Object.assign({ collapsed: false, pinned: true }, JSON.parse(localStorage.getItem(MENU_KEY)) || {}); }
    catch (err) { return { collapsed: false, pinned: true }; }
  })();
  function applyMenu() {
    const shell = document.querySelector('.app');
    shell.classList.toggle('is-rail', menu.collapsed);
    shell.classList.toggle('auto-rail', !menu.pinned);
    const c = document.getElementById('menu-collapse');
    const p = document.getElementById('menu-pin');
    c.setAttribute('aria-label', menu.collapsed ? 'Expand menu' : 'Collapse menu');
    c.title = menu.collapsed ? 'Expand menu' : 'Collapse menu';
    p.setAttribute('aria-pressed', String(menu.pinned));
    p.setAttribute('aria-label', menu.pinned ? 'Unpin menu' : 'Pin menu');
    p.title = menu.pinned ? 'Pinned: menu stays as it is. Click to unpin.' : 'Unpinned: menu shrinks to icons and opens on hover. Click to pin.';
    try { localStorage.setItem(MENU_KEY, JSON.stringify(menu)); } catch (err) { /* private mode */ }
  }
  document.addEventListener('click', (e) => {
    const b = e.target.closest('#menu-collapse, #menu-pin');
    if (!b) return;
    if (b.id === 'menu-collapse') menu.collapsed = !menu.collapsed;
    else menu.pinned = !menu.pinned;
    applyMenu();
  });

  /* ---------------------------------------------------------------- start */
  window.addEventListener('hashchange', () => {
    if (currentRoute() !== 'directories') state.baselineFrom = null;   // the baseline banner lasts while on Directories
    render();
  });
  setInterval(tick, 900);
  applyMenu();
  render();
})();
