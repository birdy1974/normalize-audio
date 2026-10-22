/* Audio Normalizer — web UI.
 * Vanilla JavaScript. Every value comes from the REST API under /api; nothing is simulated.
 * Layout and wording follow the approved mock-up (mockup/ in the repository, v4.2). */
(function () {
  'use strict';

  /* ------------------------------------------------------------ utilities */
  const esc = (s) => String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fmt = (n) => Number(n || 0).toLocaleString('en-US');
  const pad2 = (n) => String(n).padStart(2, '0');
  const round1 = (n) => Math.round(n * 10) / 10;
  const tenth = (v) => Math.round(v * 10) / 10;
  const fmtDb = (n) => (n >= 0 ? '+' : '−') + Math.abs(n).toFixed(1) + ' dB';
  const fmtDbN = (n) => (n === null || n === undefined ? '—' : fmtDb(n));
  const fmtLufs = (n) => (n === null || n === undefined ? '—' : Number(n).toFixed(1) + ' LUFS');
  const clone = (o) => JSON.parse(JSON.stringify(o));
  const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const ALL = ['audio', 'video'];
  const PARK_RANGE = 12;                                  // dB, both ways (the server clamps the same way)
  let busySeq = 0;                                        // ids for state.busy
  const folderTail = (path) => String(path || '').split('/').filter(Boolean).slice(-2).join('/');

  const MEDIA = {
    audio: { label: 'Audio', root: '/data/audio', host: '/volume1/music' },
    video: { label: 'Video', root: '/data/video', host: '/volume1/video' },
  };
  const STATUS_LABEL = {
    pending: 'to do', approved: 'approved', processing: 'working', ok: 'at target', changed: 'changed',
    parked: 'parked', skipped: 'skipped', error: 'error', dryrun: 'dry run',
  };
  const METHOD_LABEL = {
    volume: 'lossless gain', mp3gain: 'gain only (mp3gain)', aacgain: 'gain only (aacgain)',
    reencode: 're-encode, same bitrate',
  };
  const MODE_LABEL = { off: 'Off', bulk: 'Bulk', auto: 'Automatic' };
  const MODE_SHORT = {
    off: 'nothing is scanned or normalized',
    bulk: 'a run normalizes the files that are here',
    auto: 'new files are normalized by themselves',
  };
  const PARK_SORTS = {
    order: 'File order',
    change_asc: 'Change needed, lowest first',
    change_desc: 'Change needed, highest first',
    size_desc: 'Size of change, largest first',
  };
  const DEFAULTS = {
    general: { dryRun: false, preserveMtime: true, mode: 'bulk' },
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

  /* ------------------------------------------------------------- activity
     What the app is doing right now, shown on every page: work started from this
     browser (a level check, a rescan, a save) and work the server reports (a
     running normalization, the periodic scan). */
  function startBusy(text) {
    const id = 'b' + (++busySeq);
    state.busy[id] = { text, secs: null };
    showActivity();
    return id;
  }

  /* a live counter for actions that take a while, e.g. "Checking the level of x · 12 s" */
  function tickBusy(id) {
    const started = Date.now();
    const update = () => {
      const b = state.busy[id];
      if (!b) return false;
      b.secs = Math.max(0, Math.round((Date.now() - started) / 1000));
      showActivity();
      return true;
    };
    update();
    const timer = setInterval(update, 1000);
    return { stop: () => clearInterval(timer) };
  }

  function endBusy(id) {
    if (state.busy[id] === undefined) return;
    delete state.busy[id];
    showActivity();
  }

  function serverWork() {
    const d = state.dash;
    if (!d) return [];
    const out = [];
    const auto = d.mode === 'auto';
    if (d.status === 'running') {
      out.push({ busy: true, text: 'Normalizing', sub: d.current_file ? folderTail(d.current_file) : 'waiting for the next file' });
    } else if (d.status === 'paused') {
      out.push({ busy: false, text: 'Paused', sub: auto ? 'automatic mode is paused' : 'the run is paused' });
    } else if (d.status === 'waiting_window') {
      out.push({ busy: true, text: 'Waiting for schedule', sub: 'the run continues in the next window' });
    } else if (d.scanning) {
      out.push({ busy: true, text: 'Scanning folders', sub: 'looking for new, changed and removed files' });
    } else if (d.status === 'off') {
      out.push({ busy: false, text: 'Off', sub: 'not scanning and not normalizing' });
    } else if (d.status === 'idle' && auto) {
      out.push({ busy: false, text: 'Watching', sub: 'new files are normalized automatically' });
    }
    return out;
  }

  function activity() {
    const ids = Object.keys(state.busy);
    const server = serverWork();
    if (ids.length) {
      const first = state.busy[ids[0]];
      const more = ids.length > 1 ? '+' + (ids.length - 1) + ' more in this browser' : '';
      return {
        busy: true,
        title: first.text,
        sub: [first.secs === null ? '' : first.secs + ' s', more].filter(Boolean).join(' · '),
      };
    }
    if (server.length) return { busy: server[0].busy, title: server[0].text, sub: server[0].sub };
    if (!state.dash) return { busy: true, title: 'Loading', sub: 'reading the status' };
    return { busy: false, title: 'Idle', sub: 'nothing to do right now' };
  }

  function showActivity() {
    const box = document.getElementById('activity');
    if (!box) return;
    const now = activity();
    const main = document.getElementById('activity-main');
    const sub = document.getElementById('activity-sub');
    /* a running timer updates the label without re-rendering the page */
    if (main.textContent !== now.title) main.textContent = now.title;
    if (sub.textContent !== now.sub) sub.textContent = now.sub;
    box.classList.toggle('is-busy', now.busy);
    box.setAttribute('aria-busy', String(now.busy));
    box.title = now.title + (now.sub ? ' · ' + now.sub : '');
  }

  /* ----------------------------------------------------------- preview player
     One player for the whole app, outside #app so a re-render never stops it. A Play
     button in the Directories file list loads the file into it. The file is streamed
     unchanged (no transcoding): what you hear is what will be normalized. */
  const PLAY = { media: null, relpath: null, name: null, playing: false };
  const ICON_PLAY = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5.5v13l11-6.5z"/></svg>';
  const ICON_PAUSE = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5h3.2v14H8zM12.8 5H16v14h-3.2z"/></svg>';

  const previewAudio = () => document.getElementById('preview-audio');
  const streamUrl = (media, relpath) => '/api/files/stream?media=' + encodeURIComponent(media)
    + '&relpath=' + encodeURIComponent(relpath);

  function fmtTime(sec) {
    if (!isFinite(sec) || sec < 0) return '0:00';
    return Math.floor(sec / 60) + ':' + pad2(Math.floor(sec % 60));
  }

  function syncPlayer() {
    const bar = document.getElementById('player');
    const audio = previewAudio();
    if (!bar || !audio) return;
    bar.hidden = !PLAY.relpath;
    document.body.classList.toggle('has-player', !!PLAY.relpath);
    if (!PLAY.relpath) return;
    const duration = isFinite(audio.duration) ? audio.duration : 0;
    const toggle = document.getElementById('player-toggle');
    toggle.innerHTML = PLAY.playing ? ICON_PAUSE : ICON_PLAY;
    toggle.setAttribute('aria-label', PLAY.playing ? 'Pause' : 'Play');
    document.getElementById('player-name').textContent = PLAY.name || '';
    document.getElementById('player-sub').textContent =
      (PLAY.media ? MEDIA[PLAY.media].label : '') + ' · ' + folderTail(PLAY.relpath);
    const seek = document.getElementById('player-seek');
    seek.value = duration ? String(Math.round((audio.currentTime / duration) * 1000)) : '0';
    paintRange(seek);
    document.getElementById('player-time').textContent =
      fmtTime(audio.currentTime) + ' / ' + (duration ? fmtTime(duration) : '--:--');
    bar.classList.toggle('is-playing', PLAY.playing);
  }

  function playFile(media, file) {
    const audio = previewAudio();
    if (!audio || !file || !file.playable) return;
    if (PLAY.media === media && PLAY.relpath === file.relpath) { togglePreview(); return; }
    PLAY.media = media;
    PLAY.relpath = file.relpath;
    PLAY.name = file.name;
    PLAY.playing = false;
    audio.src = streamUrl(media, file.relpath);
    syncPlayer();
    audio.play().catch(() => toast('The browser blocked playback. Press play in the player.'));
  }

  function togglePreview() {
    const audio = previewAudio();
    if (!audio || !PLAY.relpath) return;
    if (PLAY.playing) audio.pause();
    else audio.play().catch(() => toast('The browser blocked playback. Press play again.'));
  }

  function closePreview() {
    const audio = previewAudio();
    const wasOpen = !!PLAY.relpath;
    PLAY.media = PLAY.relpath = PLAY.name = null;
    PLAY.playing = false;
    if (audio) {
      audio.pause();
      audio.removeAttribute('src');
      audio.load();
    }
    syncPlayer();
    if (wasOpen && currentRoute() === 'directories') render();
  }

  function initPlayer() {
    const audio = previewAudio();
    if (!audio) return;
    const repaint = () => {
      syncPlayer();
      if (currentRoute() === 'directories') render();      // the Play/Pause button of the row
    };
    audio.addEventListener('play', () => { PLAY.playing = true; repaint(); });
    audio.addEventListener('pause', () => { PLAY.playing = false; repaint(); });
    audio.addEventListener('ended', () => { PLAY.playing = false; repaint(); });
    audio.addEventListener('timeupdate', syncPlayer);
    audio.addEventListener('loadedmetadata', syncPlayer);
    audio.addEventListener('durationchange', syncPlayer);
    audio.addEventListener('error', () => {
      if (!PLAY.relpath) return;
      toast('This file cannot be played in the browser.');
      closePreview();
    });
    const seek = document.getElementById('player-seek');
    if (seek) seek.addEventListener('input', () => {
      if (!isFinite(audio.duration) || audio.duration <= 0) return;
      audio.currentTime = (Number(seek.value) / 1000) * audio.duration;
    });
    syncPlayer();
  }

  /* ------------------------------------------------------------ audio editor
     A window over one file: waveform with a dB scale and a time ruler, zoom, fades
     and a cut. The server applies everything in one pass (ffmpeg) on a temporary
     file and replaces the original atomically, like the normalization run does. */
  const EDIT = {
    open: false, media: 'audio', relpath: null, name: null, duration: 0,
    lufs: null, peak: null,
    view: { start: 0, dur: 1 }, base: null, window: null,
    sel: null, cut: null, fadeIn: 0, fadeOut: 0,
    loading: false, waveError: '', applyError: '', busy: false, playing: false,
    req: 0, session: 0, timer: null, raf: null, lastDraw: 0, dragging: null, lastFocus: null, viewTouched: false,
  };
  const EDIT_MIN_WINDOW = 0.25;          // seconds visible at the deepest zoom
  const ZOOM_STEP = 1.6;
  const EDIT_COLORS = {
    bg: '#fbfbfa', grid: '#d9d9d5', gridText: '#9d9e99', text: '#6c6d69',
    wave: '#2f7a6b', waveDim: '#8fc0b5',
    sel: 'rgba(47,122,107,.20)', cut: 'rgba(160,63,54,.20)', cutLine: '#a03f36',
    fade: 'rgba(47,122,107,.14)', playhead: '#2f3130',
  };

  function fmtClock(sec, decimals) {
    if (!isFinite(sec) || sec < 0) sec = 0;
    const m = Math.floor(sec / 60);
    const s = sec - m * 60;
    const text = decimals ? s.toFixed(1) : String(Math.round(s));
    return m + ':' + (Number(text) < 10 ? '0' : '') + text;
  }

  const editorAudio = () => document.getElementById('editor-audio');

  function editPlan() {
    const cutLen = EDIT.cut ? Math.max(0, EDIT.cut.end - EDIT.cut.start) : 0;
    return { cutLen, newDur: Math.max(0, EDIT.duration - cutLen) };
  }

  function editBlocked() {
    const gen = (state.settings && state.settings.general) || {};
    const audio = (state.settings && state.settings.audio) || {};
    if (gen.dryRun) return 'Dry run is on: turn it off in Settings to edit files.';
    if (audio.writeMode === 'output') {
      return 'Write mode is output folder: editing the original is switched off in Settings.';
    }
    return '';
  }

  function editProblem(plan) {
    if (!EDIT.duration) return '';
    if (EDIT.fadeIn <= 0 && EDIT.fadeOut <= 0 && !EDIT.cut) return 'Set a fade or select a part to cut.';
    if (EDIT.fadeIn > plan.newDur - 0.05 || EDIT.fadeOut > plan.newDur - 0.05) {
      return 'A fade is longer than the file will be after the cut.';
    }
    if (EDIT.fadeIn + EDIT.fadeOut > plan.newDur) return 'Fade in and fade out overlap. Shorten one of them.';
    return '';
  }

  function selectionHtml() {
    if (EDIT.cut) {
      const len = EDIT.cut.end - EDIT.cut.start;
      return '<span class="cut-chip">Remove ' + fmtClock(EDIT.cut.start, 1) + ' – ' + fmtClock(EDIT.cut.end, 1)
        + ' · ' + len.toFixed(1) + ' s</span>'
        + '<button type="button" class="btn btn-sm" data-action="editor-uncut">Undo cut</button>';
    }
    if (EDIT.sel) {
      const len = EDIT.sel.end - EDIT.sel.start;
      return '<span><strong>Selection</strong> ' + fmtClock(EDIT.sel.start, 1) + ' – ' + fmtClock(EDIT.sel.end, 1)
        + ' · ' + len.toFixed(1) + ' s</span>'
        + '<button type="button" class="btn btn-sm btn-primary" data-action="editor-cut">Cut this part</button>'
        + '<button type="button" class="btn btn-sm" data-action="editor-clear-sel">Clear</button>';
    }
    return '<span class="muted">Drag over the waveform to select a part to remove.</span>';
  }

  function summaryHtml(plan) {
    if (!EDIT.duration) return '';
    const bits = [];
    if (EDIT.cut) {
      bits.push('cut ' + fmtClock(EDIT.cut.start, 1) + ' – ' + fmtClock(EDIT.cut.end, 1));
    }
    if (EDIT.fadeIn > 0) bits.push('fade in ' + EDIT.fadeIn.toFixed(1) + ' s');
    if (EDIT.fadeOut > 0) bits.push('fade out ' + EDIT.fadeOut.toFixed(1) + ' s');
    if (!bits.length) return 'New length <strong>' + fmtClock(plan.newDur, 1) + '</strong> · nothing selected yet';
    return 'New length <strong>' + fmtClock(plan.newDur, 1) + '</strong> (now ' + fmtClock(EDIT.duration, 1) + ') · '
      + esc(bits.join(' · '));
  }

  function editorNoteHtml() {
    const mode = state.settings && state.settings.audio ? state.settings.audio.writeMode : 'inplace';
    if (mode === 'backup') return 'Saved over the original; a backup is kept in the config volume.';
    if (mode === 'output') return 'Write mode is output folder: editing the original is switched off in Settings.';
    return 'Saved over the original, without a backup. The file is measured again in the next run.';
  }

  function syncEditor() {
    if (!EDIT.open) return;
    const plan = editPlan();
    const audio = editorAudio();
    document.getElementById('editor-title').textContent = 'Edit ' + (EDIT.name || 'audio');
    const measured = EDIT.lufs === null ? 'not measured yet'
      : fmtLufs(EDIT.lufs) + (typeof EDIT.peak === 'number' ? ' · ' + EDIT.peak.toFixed(1) + ' dBTP' : '');
    const sub = [folderTail(EDIT.relpath), MEDIA[EDIT.media].label,
      EDIT.duration ? fmtClock(EDIT.duration, 0) + ' long' : '', measured].filter(Boolean).join(' · ');
    document.getElementById('editor-sub').textContent =
      sub + (EDIT.loading ? ' · reading the levels…' : '');
    const play = document.getElementById('editor-play');
    play.textContent = EDIT.playing ? 'Pause' : 'Play';
    play.disabled = !EDIT.duration || EDIT.loading;
    document.getElementById('editor-time').textContent =
      fmtClock(audio ? audio.currentTime : 0, 0) + ' / ' + fmtClock(EDIT.duration, 0);
    document.getElementById('editor-zoom').textContent = !EDIT.duration ? ''
      : EDIT.view.dur >= EDIT.duration - 0.01 ? 'whole file'
        : fmtClock(EDIT.view.dur, EDIT.view.dur < 10 ? 1 : 0) + ' visible';
    const pan = document.getElementById('editor-pan');
    const span = Math.max(0, EDIT.duration - EDIT.view.dur);
    pan.hidden = !(EDIT.duration && span > 0.02);
    if (!pan.hidden) {
      pan.value = String(Math.round((EDIT.view.start / span) * 1000));
      paintRange(pan);
    }
    document.getElementById('editor-sel').innerHTML = selectionHtml();
    const fadeIn = document.getElementById('editor-fade-in');
    const fadeOut = document.getElementById('editor-fade-out');
    const maxFade = Math.max(0.5, Math.min(30, plan.newDur || 30));
    if (EDIT.fadeIn > maxFade) EDIT.fadeIn = maxFade;
    if (EDIT.fadeOut > maxFade) EDIT.fadeOut = maxFade;
    fadeIn.max = fadeOut.max = String(maxFade);
    if (document.activeElement !== fadeIn) fadeIn.value = String(EDIT.fadeIn);
    if (document.activeElement !== fadeOut) fadeOut.value = String(EDIT.fadeOut);
    paintRange(fadeIn);
    paintRange(fadeOut);
    document.getElementById('editor-fade-in-out').textContent = EDIT.fadeIn > 0 ? EDIT.fadeIn.toFixed(1) + ' s' : 'off';
    document.getElementById('editor-fade-out-out').textContent = EDIT.fadeOut > 0 ? EDIT.fadeOut.toFixed(1) + ' s' : 'off';
    document.getElementById('editor-summary').innerHTML = summaryHtml(plan);
    const problem = editBlocked() || editProblem(plan);
    const warn = document.getElementById('editor-warn');
    const message = problem || EDIT.applyError;
    warn.hidden = !message;
    warn.textContent = message || '';
    const apply = document.getElementById('editor-apply');
    apply.disabled = EDIT.busy || EDIT.loading || !!EDIT.waveError || !EDIT.duration || !!problem;
    apply.textContent = EDIT.busy ? 'Applying…' : 'Apply edits';
    document.getElementById('editor-note').textContent = editorNoteHtml();
  }

  function drawEditor() {
    const canvas = document.getElementById('editor-canvas');
    if (!canvas || !EDIT.open) return;
    const ctx = canvas.getContext ? canvas.getContext('2d') : null;
    if (!ctx) return;                                  // no canvas support: the controls still work
    const W = Math.max(280, canvas.clientWidth || 680);
    const H = 190;
    const dpr = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(W * dpr) || canvas.height !== Math.round(H * dpr)) {
      canvas.width = Math.round(W * dpr);
      canvas.height = Math.round(H * dpr);
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = EDIT_COLORS.bg;
    ctx.fillRect(0, 0, W, H);
    if (!EDIT.duration) {                              // the length is on its way: never show the last file
      ctx.fillStyle = EDIT_COLORS.text;
      ctx.font = '12px system-ui, sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText(EDIT.waveError || 'Opening…', W / 2, H / 2);
      ctx.textAlign = 'left';
      return;
    }
    const ruler = 22, top = ruler + 8, bottom = H - 8;
    const mid = (top + bottom) / 2, half = (bottom - top) / 2;
    const view = EDIT.view;
    const xOf = (t) => ((t - view.start) / view.dur) * W;

    /* time ruler */
    const steps = [0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800];
    const target = view.dur / Math.max(2, W / 90);
    const step = steps.find((v) => v >= target) || steps[steps.length - 1];
    ctx.strokeStyle = EDIT_COLORS.grid;
    ctx.beginPath();
    ctx.moveTo(0, ruler + 0.5);
    ctx.lineTo(W, ruler + 0.5);
    ctx.stroke();
    ctx.fillStyle = EDIT_COLORS.text;
    ctx.font = '10px system-ui, -apple-system, Segoe UI, Roboto, sans-serif';
    ctx.textBaseline = 'alphabetic';
    const first = Math.ceil(view.start / step) * step;
    for (let t = first; t <= view.start + view.dur + 1e-6; t += step) {
      const x = Math.round(xOf(t)) + 0.5;
      if (x < 0 || x > W) continue;
      ctx.strokeStyle = EDIT_COLORS.grid;
      ctx.beginPath();
      ctx.moveTo(x, ruler);
      ctx.lineTo(x, ruler + 5);
      ctx.stroke();
      ctx.fillText(fmtClock(t, step < 1 ? 1 : 0), Math.max(1, Math.min(W - 36, x + 3)), 12);
    }

    /* level lines */
    ctx.font = '10px system-ui, -apple-system, Segoe UI, Roboto, sans-serif';
    ctx.textBaseline = 'middle';
    [[0, '0 dB'], [-6, '−6'], [-12, '−12'], [-24, '−24']].forEach(([db, label]) => {
      const y = mid - Math.pow(10, db / 20) * half;
      if (y < top - 1 || y > bottom + 1) return;
      ctx.strokeStyle = EDIT_COLORS.grid;
      ctx.setLineDash(db === 0 ? [] : [3, 3]);
      ctx.beginPath();
      ctx.moveTo(0, y + 0.5);
      ctx.lineTo(W, y + 0.5);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = EDIT_COLORS.gridText;
      ctx.fillText(label, 4, Math.max(top, y - 7));
    });

    /* waveform */
    const win = EDIT.window;
    if (!win || !win.min || !win.min.length) {
      ctx.fillStyle = EDIT_COLORS.text;
      ctx.font = '12px system-ui, sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText(EDIT.waveError || 'Reading the levels…', W / 2, mid);
      ctx.textAlign = 'left';
      return;
    }
    const n = win.min.length;
    const winEnd = win.start + win.duration;
    ctx.fillStyle = EDIT_COLORS.wave;
    for (let x = 0; x < W; x += 1) {
      const t0 = view.start + (x / W) * view.dur;
      const t1 = view.start + ((x + 1) / W) * view.dur;
      if (t1 < win.start || t0 > winEnd) continue;
      let i0 = Math.floor(((t0 - win.start) / win.duration) * n);
      let i1 = Math.ceil(((t1 - win.start) / win.duration) * n);
      i0 = Math.max(0, Math.min(n - 1, i0));
      i1 = Math.max(i0 + 1, Math.min(n, i1));
      let lo = 0;
      let hi = 0;
      for (let i = i0; i < i1; i += 1) {
        if (win.min[i] < lo) lo = win.min[i];
        if (win.max[i] > hi) hi = win.max[i];
      }
      const yTop = mid - hi * half;
      const yBot = mid - lo * half;
      ctx.fillRect(x, yTop, 1, Math.max(1, yBot - yTop));
    }

    /* fades: drawn on the timeline that remains after the cut */
    const plan = editPlan();
    const mapTime = (t) => {
      if (!EDIT.cut) return t;
      return t < EDIT.cut.start ? t : Math.min(EDIT.duration, t + plan.cutLen);
    };
    ctx.strokeStyle = EDIT_COLORS.waveDim;
    if (EDIT.fadeIn > 0) {
      const x0 = xOf(0);
      const x1 = xOf(Math.min(EDIT.fadeIn, plan.newDur));
      ctx.fillStyle = EDIT_COLORS.fade;
      ctx.fillRect(Math.max(0, x0), top, Math.max(0, x1 - Math.max(0, x0)), bottom - top);
      ctx.beginPath();
      ctx.moveTo(x0, bottom);
      ctx.lineTo(x1, top);
      ctx.stroke();
    }
    if (EDIT.fadeOut > 0) {
      const x0 = xOf(mapTime(Math.max(0, plan.newDur - EDIT.fadeOut)));
      const x1 = xOf(mapTime(plan.newDur));
      ctx.fillStyle = EDIT_COLORS.fade;
      ctx.fillRect(Math.max(0, x0), top, Math.min(W, x1) - Math.max(0, x0), bottom - top);
      ctx.beginPath();
      ctx.moveTo(x0, top);
      ctx.lineTo(x1, bottom);
      ctx.stroke();
    }

    /* the part that will be removed */
    if (EDIT.cut) {
      const x0 = Math.max(0, xOf(EDIT.cut.start));
      const x1 = Math.min(W, xOf(EDIT.cut.end));
      if (x1 > x0) {
        ctx.fillStyle = EDIT_COLORS.cut;
        ctx.fillRect(x0, top, x1 - x0, bottom - top);
        ctx.strokeStyle = EDIT_COLORS.cutLine;
        ctx.setLineDash([4, 3]);
        ctx.beginPath();
        ctx.moveTo(x0, top);
        ctx.lineTo(x0, bottom);
        ctx.moveTo(x1, top);
        ctx.lineTo(x1, bottom);
        ctx.stroke();
        ctx.setLineDash([]);
        const label = 'remove ' + (EDIT.cut.end - EDIT.cut.start).toFixed(1) + ' s';
        ctx.font = '11px system-ui, sans-serif';
        if (x1 - x0 > ctx.measureText(label).width + 10) {
          ctx.fillStyle = EDIT_COLORS.cutLine;
          ctx.textBaseline = 'top';
          ctx.fillText(label, x0 + 5, top + 4);
        }
      }
    }

    /* the selection */
    if (EDIT.sel) {
      const x0 = xOf(EDIT.sel.start);
      const x1 = xOf(EDIT.sel.end);
      ctx.fillStyle = EDIT_COLORS.sel;
      ctx.fillRect(Math.max(0, x0), top, Math.min(W, x1) - Math.max(0, x0), bottom - top);
      ctx.strokeStyle = EDIT_COLORS.wave;
      ctx.setLineDash([2, 2]);
      ctx.beginPath();
      ctx.moveTo(x0, top);
      ctx.lineTo(x0, bottom);
      ctx.moveTo(x1, top);
      ctx.lineTo(x1, bottom);
      ctx.stroke();
      ctx.setLineDash([]);
    }

    /* playhead */
    const audio = editorAudio();
    const now = audio ? audio.currentTime : 0;
    if (now >= view.start && now <= view.start + view.dur) {
      const x = xOf(now);
      ctx.strokeStyle = EDIT_COLORS.playhead;
      ctx.beginPath();
      ctx.moveTo(x, ruler);
      ctx.lineTo(x, bottom);
      ctx.stroke();
      ctx.fillStyle = EDIT_COLORS.playhead;
      ctx.beginPath();
      ctx.moveTo(x - 4, ruler);
      ctx.lineTo(x + 4, ruler);
      ctx.lineTo(x, ruler + 6);
      ctx.closePath();
      ctx.fill();
    }
  }

  function scheduleWindow() {
    clearTimeout(EDIT.timer);
    EDIT.timer = setTimeout(fetchViewWindow, 180);
  }

  function fetchViewWindow() {
    if (!EDIT.open || !EDIT.duration) return;
    const view = EDIT.view;
    if (EDIT.base && view.dur >= EDIT.duration - 0.01) {
      EDIT.window = EDIT.base;
      drawEditor();
      return;
    }
    const canvas = document.getElementById('editor-canvas');
    const buckets = Math.min(4000, Math.max(200, Math.round((canvas && canvas.clientWidth ? canvas.clientWidth : 800) * 1.5)));
    loadWaveform(view.start, view.dur, buckets, false);
  }

  function loadWaveform(start, duration, buckets, base) {
    if (!EDIT.open) return;
    const seq = ++EDIT.req;
    if (base) {
      EDIT.loading = true;
      EDIT.base = null;
      EDIT.window = null;
      EDIT.waveError = '';
      syncEditor();
    }
    let url = '/files/waveform?media=' + encodeURIComponent(EDIT.media)
      + '&relpath=' + encodeURIComponent(EDIT.relpath)
      + '&buckets=' + buckets + '&start=' + Number(start || 0).toFixed(3);
    if (duration) url += '&duration=' + Number(duration).toFixed(3);
    api(url).then((d) => {
      if (!EDIT.open || seq !== EDIT.req) return;
      if (base) {
        EDIT.base = d;
        EDIT.window = d;
        EDIT.duration = d.file_duration;
        if (!EDIT.viewTouched) EDIT.view = { start: 0, dur: d.file_duration };
        EDIT.loading = false;
        if (EDIT.viewTouched) scheduleWindow();
      } else {
        EDIT.window = d;
      }
      EDIT.waveError = '';
      syncEditor();
      drawEditor();
    }).catch((err) => {
      if (!EDIT.open || seq !== EDIT.req) return;
      EDIT.loading = false;
      EDIT.waveError = err.message;
      syncEditor();
      drawEditor();
    });
  }

  function zoomBy(factor, anchor) {
    if (!EDIT.duration) return;
    const view = EDIT.view;
    const minDur = Math.min(EDIT.duration, EDIT_MIN_WINDOW);
    const newDur = Math.max(minDur, Math.min(EDIT.duration, view.dur * factor));
    if (Math.abs(newDur - view.dur) < 1e-6) return;
    const at = anchor === undefined ? view.start + view.dur / 2 : anchor;
    const frac = view.dur > 0 ? (at - view.start) / view.dur : 0.5;
    let start = at - frac * newDur;
    start = Math.max(0, Math.min(EDIT.duration - newDur, start));
    EDIT.view = { start, dur: newDur };
    EDIT.viewTouched = true;
    scheduleWindow();
    syncEditor();
    drawEditor();
  }

  function panBy(delta) {
    if (!EDIT.duration) return;
    const span = Math.max(0, EDIT.duration - EDIT.view.dur);
    const start = Math.max(0, Math.min(span, EDIT.view.start + delta));
    if (Math.abs(start - EDIT.view.start) < 1e-9) return;
    EDIT.view.start = start;
    EDIT.viewTouched = true;
    scheduleWindow();
    syncEditor();
    drawEditor();
  }

  function fitEditor() {
    if (!EDIT.duration) return;
    EDIT.view = { start: 0, dur: EDIT.duration };
    EDIT.viewTouched = false;
    if (EDIT.base) EDIT.window = EDIT.base;
    clearTimeout(EDIT.timer);
    syncEditor();
    drawEditor();
  }

  function seekEditor(t) {
    const audio = editorAudio();
    if (!audio || !EDIT.duration) return;
    audio.currentTime = Math.max(0, Math.min(EDIT.duration, t));
    updateEditorTime();
    drawEditor();
  }

  function toggleEditorPlay() {
    const audio = editorAudio();
    if (!audio || !EDIT.duration) return;
    if (EDIT.playing) audio.pause();
    else audio.play().catch(() => toast('The browser blocked playback. Press Play again.'));
  }

  function updateEditorTime() {
    const audio = editorAudio();
    const time = document.getElementById('editor-time');
    if (time) time.textContent = fmtClock(audio ? audio.currentTime : 0, 0) + ' / ' + fmtClock(EDIT.duration, 0);
    const play = document.getElementById('editor-play');
    if (play) play.textContent = EDIT.playing ? 'Pause' : 'Play';
  }

  function editorTick() {
    EDIT.raf = null;
    if (!EDIT.open) return;
    const audio = editorAudio();
    const playing = EDIT.playing;              // the play/pause/ended events are the source
    updateEditorTime();
    if (playing && audio && EDIT.duration) {
      const t = audio.currentTime;
      /* keep the playhead in view while playing a zoomed part */
      if (EDIT.view.dur < EDIT.duration - 0.01
        && (t < EDIT.view.start + 0.02 || t > EDIT.view.start + EDIT.view.dur - 0.02)) {
        EDIT.view.start = Math.max(0, Math.min(EDIT.duration - EDIT.view.dur, t - EDIT.view.dur / 2));
        EDIT.viewTouched = true;
        scheduleWindow();
      }
    }
    const now = Date.now();
    if (!EDIT.lastDraw || now - EDIT.lastDraw > 30) {
      drawEditor();
      EDIT.lastDraw = now;
    }
    if (playing) EDIT.raf = requestAnimationFrame(editorTick);
    else { syncEditor(); drawEditor(); }
  }

  function editorAnchor() {
    const audio = editorAudio();
    const t = audio ? audio.currentTime : 0;
    return t >= EDIT.view.start && t <= EDIT.view.start + EDIT.view.dur
      ? t : EDIT.view.start + EDIT.view.dur / 2;
  }

  function editorTimeAt(ev) {
    const canvas = document.getElementById('editor-canvas');
    if (!canvas) return 0;
    const rect = canvas.getBoundingClientRect();
    const width = rect.width || canvas.clientWidth || 680;
    const x = Math.max(0, Math.min(width, ev.clientX - rect.left));
    return EDIT.view.start + (x / width) * EDIT.view.dur;
  }

  function openEditor(media, file) {
    if (!file) return;
    if (media !== 'audio') { toast('Editing is available for audio files only.'); return; }
    if (!file.playable) { toast('This format cannot be edited here.'); return; }
    closePreview();                                  // only one thing plays at a time
    EDIT.open = true;
    EDIT.media = media;
    EDIT.relpath = file.relpath;
    EDIT.name = file.name;
    EDIT.duration = 0;
    EDIT.lufs = typeof file.lufs === 'number' ? file.lufs : null;
    EDIT.peak = typeof file.true_peak === 'number' ? file.true_peak : null;
    EDIT.view = { start: 0, dur: 1 };
    EDIT.base = EDIT.window = null;
    EDIT.sel = EDIT.cut = null;
    EDIT.fadeIn = EDIT.fadeOut = 0;
    EDIT.loading = true;
    EDIT.waveError = '';
    EDIT.applyError = '';
    EDIT.busy = false;
    EDIT.playing = false;
    EDIT.viewTouched = false;
    EDIT.session += 1;
    EDIT.lastFocus = document.activeElement;
    drawEditor();                                    // drop the previous file's picture at once
    const audio = editorAudio();
    if (audio) {
      audio.pause();
      audio.src = streamUrl(media, file.relpath);
    }
    const modal = document.getElementById('editor');
    modal.hidden = false;
    document.body.classList.add('has-modal');
    syncEditor();
    const close = modal.querySelector('[data-action="editor-close"]');
    if (close) close.focus();
    loadEditInfo();
    loadWaveform(0, null, 1800, true);
  }

  function loadEditInfo() {
    const session = EDIT.session;
    api('/files/info?media=' + encodeURIComponent(EDIT.media)
      + '&relpath=' + encodeURIComponent(EDIT.relpath)).then((d) => {
      if (!EDIT.open || session !== EDIT.session || !d || !d.duration) return;
      if (!EDIT.duration) {
        EDIT.duration = d.duration;
        if (!EDIT.viewTouched) EDIT.view = { start: 0, dur: d.duration };
        syncEditor();
        drawEditor();
      }
    }).catch(() => { /* the waveform call reports the error */ });
  }

  function closeEditor() {
    if (!EDIT.open) return;
    EDIT.open = false;
    EDIT.req += 1;
    EDIT.session += 1;
    clearTimeout(EDIT.timer);
    if (EDIT.raf) cancelAnimationFrame(EDIT.raf);
    EDIT.raf = null;
    EDIT.playing = false;
    EDIT.sel = null;
    EDIT.cut = null;
    const audio = editorAudio();
    if (audio) {
      audio.pause();
      audio.removeAttribute('src');
      if (audio.load) audio.load();
    }
    const modal = document.getElementById('editor');
    if (modal) modal.hidden = true;
    document.body.classList.remove('has-modal');
    if (EDIT.lastFocus && EDIT.lastFocus.focus) EDIT.lastFocus.focus();
    EDIT.lastFocus = null;
  }

  async function applyEdit() {
    const plan = editPlan();
    if (EDIT.busy || editBlocked() || editProblem(plan)) return;
    EDIT.busy = true;
    EDIT.applyError = '';
    const busy = startBusy('Editing ' + EDIT.name);
    const tick = tickBusy(busy);
    syncEditor();
    let name = EDIT.name;
    try {
      const r = await api('/files/edit', {
        method: 'POST',
        body: {
          media: EDIT.media,
          relpath: EDIT.relpath,
          fade_in: EDIT.fadeIn,
          fade_out: EDIT.fadeOut,
          cut_start: EDIT.cut ? EDIT.cut.start : null,
          cut_end: EDIT.cut ? EDIT.cut.end : null,
        },
      });
      name = r.name;
      closeEditor();
      invalidate('dir:' + state.media);
      state.browse = { media: null, rel: null, rows: null, loading: false };
      await refreshDash();
      toast('Edited ' + name + ' · new length ' + fmtClock(r.duration, 1));
      render();
    } catch (err) {
      EDIT.applyError = err.message;
      toast(err.message);
      syncEditor();
    } finally {
      tick.stop();
      endBusy(busy);
      EDIT.busy = false;
      syncEditor();
    }
  }

  function initEditor() {
    const modal = document.getElementById('editor');
    const audio = editorAudio();
    const canvas = document.getElementById('editor-canvas');
    if (!modal || !audio || !canvas) return;

    audio.addEventListener('play', () => {
      EDIT.playing = true;
      syncEditor();
      if (!EDIT.raf) EDIT.raf = requestAnimationFrame(editorTick);
    });
    audio.addEventListener('pause', () => {
      EDIT.playing = false;
      syncEditor();
      drawEditor();
    });
    audio.addEventListener('ended', () => {
      EDIT.playing = false;
      syncEditor();
      drawEditor();
    });
    audio.addEventListener('timeupdate', () => {
      if (!EDIT.playing) {
        updateEditorTime();
        drawEditor();
      }
    });
    audio.addEventListener('loadedmetadata', drawEditor);
    audio.addEventListener('error', () => {
      if (!EDIT.open || !EDIT.relpath) return;
      EDIT.waveError = 'This file cannot be played in the browser.';
      syncEditor();
    });

    let drag = null;
    canvas.addEventListener('pointerdown', (ev) => {
      if (!EDIT.duration) return;
      ev.preventDefault();
      if (canvas.setPointerCapture) canvas.setPointerCapture(ev.pointerId);
      drag = { t0: editorTimeAt(ev), t: editorTimeAt(ev), moved: false };
      EDIT.dragging = drag;
    });
    canvas.addEventListener('pointermove', (ev) => {
      if (!drag) return;
      const t = editorTimeAt(ev);
      drag.t = t;
      const px = EDIT.view.dur / Math.max(1, canvas.clientWidth || 680);
      if (Math.abs(t - drag.t0) > px * 3) {
        drag.moved = true;
        EDIT.sel = { start: Math.min(drag.t0, t), end: Math.max(drag.t0, t) };
        syncEditor();
        drawEditor();
      }
    });
    const finishDrag = () => {
      if (!drag) return;
      if (!drag.moved) {
        EDIT.sel = null;
        seekEditor(drag.t0);
      }
      drag = null;
      EDIT.dragging = null;
      syncEditor();
      drawEditor();
    };
    canvas.addEventListener('pointerup', finishDrag);
    canvas.addEventListener('pointercancel', () => { drag = null; EDIT.dragging = null; });
    canvas.addEventListener('wheel', (ev) => {
      if (!EDIT.duration) return;
      ev.preventDefault();
      if (ev.ctrlKey || ev.metaKey) {
        zoomBy(ev.deltaY > 0 ? ZOOM_STEP : 1 / ZOOM_STEP, editorTimeAt(ev));
      } else {
        const delta = Math.abs(ev.deltaX) > Math.abs(ev.deltaY) ? ev.deltaX : ev.deltaY;
        panBy((delta / 400) * EDIT.view.dur);
      }
    }, { passive: false });

    const fadeIn = document.getElementById('editor-fade-in');
    const fadeOut = document.getElementById('editor-fade-out');
    const onFade = () => {
      EDIT.fadeIn = Number(fadeIn.value);
      EDIT.fadeOut = Number(fadeOut.value);
      syncEditor();
      drawEditor();
    };
    fadeIn.addEventListener('input', onFade);
    fadeOut.addEventListener('input', onFade);

    const pan = document.getElementById('editor-pan');
    pan.addEventListener('input', () => {
      const span = Math.max(0, EDIT.duration - EDIT.view.dur);
      EDIT.view.start = (Number(pan.value) / 1000) * span;
      EDIT.viewTouched = true;
      scheduleWindow();
      paintRange(pan);
      drawEditor();
    });

    modal.addEventListener('click', (ev) => {
      if (ev.target === modal) closeEditor();
    });
    document.addEventListener('keydown', (ev) => {
      if (!EDIT.open) return;
      if (ev.key === 'Escape') {
        ev.preventDefault();
        closeEditor();
        return;
      }
      const tag = (ev.target && ev.target.tagName) || '';
      if (ev.key === ' ' && !/^(INPUT|TEXTAREA|SELECT|BUTTON|A)$/.test(tag)) {
        ev.preventDefault();
        toggleEditorPlay();
      }
    });
    window.addEventListener('resize', () => { if (EDIT.open) drawEditor(); });
    paintRange(fadeIn);
    paintRange(fadeOut);
  }

  /* ------------------------------------------------------------------ API */
  async function api(path, options) {
    const opts = options || {};
    const init = { method: opts.method || 'GET', headers: {} };
    if (opts.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
    const res = await fetch('/api' + path, init);
    let data = null;
    try { data = await res.json(); } catch (err) { data = null; }
    if (!res.ok) {
      const d = data && data.detail;
      let msg = 'Request failed (HTTP ' + res.status + ')';
      if (typeof d === 'string') msg = d;
      else if (Array.isArray(d)) msg = d.map((x) => x.msg).join('; ');
      throw new Error(msg);
    }
    return data;
  }

  /* ---------------------------------------------------------------- state */
  const state = {
    media: 'audio',                 // media tab shown on media-specific pages
    dash: null,                     // GET /api/dashboard
    dashKey: '',
    settings: null,                 // saved settings
    draft: null,                    // settings being edited on the Settings page
    sched: null,                    // GET /api/schedule
    ha: null,                       // GET /api/ha
    haDraft: { url: '', token: '' },
    haTest: '',
    trees: { audio: null, video: null },   // { roots, map, rootFiles } built from /api/folders/tree
    selected: { audio: null, video: null },// selected folder (relpath) per media type
    browse: { media: null, rel: null, rows: null, loading: false },
    scanning: { audio: false, video: false },  // a rescan of the base location is running
    busy: {},                       // what this browser is doing now: id -> { text, secs }
    parked: { audio: null, video: null },  // GET /api/parked
    parkSel: [],                    // selected parked file ids
    parkSort: 'order',
    measure: null,                  // last file level check (baseline)
    baselineFrom: null,             // media type when opened from Settings > Level
    loaded: {}, pending: {}, errors: {},
    dragging: false,                // a slider is being dragged: do not re-render parked rows
  };

  /* ----------------------------------------------------------- data loading */
  function keysFor(route) {
    const t = state.media;
    switch (route) {
      case 'dashboard': return ['dashboard', 'settings'];
      case 'directories': return ['dir:' + t, 'settings', 'dashboard'];   // folder counts come from the dashboard
      case 'parked': return ['parked:' + t, 'settings'];
      case 'schedule': return ['schedule'];
      case 'settings': return ['settings', 'schedule'];
      case 'homeassistant': return ['ha'];
      default: return ['dashboard'];
    }
  }

  function buildTree(type, data, previous) {
    const map = new Map();
    data.dirs.forEach((d) => {
      const old = previous && previous.map.get(d.relpath);
      map.set(d.relpath, {
        type, rel: d.relpath, name: d.name, parent: d.parent, enabled: d.enabled,
        directFiles: d.direct_files, files: d.files, children: [],
        open: old ? old.open : false,
      });
    });
    const roots = [];
    map.forEach((n) => {
      const parent = n.parent !== '.' ? map.get(n.parent) : null;
      (parent ? parent.children : roots).push(n);
    });
    const sortAll = (list) => { list.sort((a, b) => a.name.localeCompare(b.name, 'en')); list.forEach((n) => sortAll(n.children)); };
    sortAll(roots);
    if (!previous) roots.forEach((n) => { n.open = true; });
    return { roots, map, rootFiles: data.root_files };
  }

  async function fetchKey(key) {
    const parts = key.split(':');
    if (key === 'dashboard') {
      state.dash = await api('/dashboard');
      state.dashKey = JSON.stringify(state.dash);
    } else if (key === 'settings') {
      state.settings = await api('/settings');
      state.draft = clone(state.settings);
    } else if (key === 'schedule') {
      state.sched = await api('/schedule');
    } else if (key === 'ha') {
      state.ha = await api('/ha');
      state.haDraft = { url: state.ha.url, token: '' };
    } else if (parts[0] === 'dir') {
      const t = parts[1];
      const data = await api('/folders/tree?media=' + t);
      state.trees[t] = buildTree(t, data, state.trees[t]);
      const tree = state.trees[t];
      if (!state.selected[t] || !tree.map.has(state.selected[t])) {
        state.selected[t] = tree.roots[0] ? tree.roots[0].rel : null;
      }
    } else if (parts[0] === 'parked') {
      state.parked[parts[1]] = await loadParkedData(parts[1]);
    }
  }

  function loadParkedData(t) {
    return api('/parked?media=' + t + '&sort=' + encodeURIComponent(state.parkSort));
  }

  function loadKey(key) {
    state.pending[key] = true;
    return fetchKey(key)
      .then(() => { state.loaded[key] = true; delete state.errors[key]; })
      .catch((err) => { state.errors[key] = err.message; })
      .finally(() => { delete state.pending[key]; });
  }

  function invalidate(key) { delete state.loaded[key]; }

  async function refreshDash() {
    const d = await api('/dashboard');
    const key = JSON.stringify(d);
    if (key !== state.dashKey) {
      state.dash = d;
      state.dashKey = key;
      state.loaded.dashboard = true;
      if (currentRoute() === 'dashboard') render();
      else updateNavBadge();
    }
    showActivity();                 // the run state lives in the dashboard data
  }

  async function loadBrowse(rel) {
    const t = state.media;
    state.browse = { media: t, rel: rel, rows: null, loading: true };
    try {
      const d = await api('/files?media=' + t + '&dir=' + encodeURIComponent(rel));
      if (state.browse.rel === rel && state.browse.media === t) state.browse = { media: t, rel: rel, rows: d.files, loading: false };
    } catch (err) {
      if (state.browse.rel === rel) state.browse = { media: t, rel: rel, rows: [], loading: false };
      toast(err.message);
    }
    render();
  }

  /* Look for new, changed and removed files and folders on disk. One scan per media type at a
     time; the tree and the open folder are read again when it finishes. */
  const scans = { audio: null, video: null };     // in-flight rescan per media type

  function scanSummary(t, r) {
    if (!r) return 'Rescan finished';
    if (r.available === false) {
      return MEDIA[t].label + ' base location ' + MEDIA[t].root + ' is not available. Check the mount in docker-compose.yml.';
    }
    const found = [];
    if (r.new) found.push(r.new + ' new');
    if (r.changed) found.push(r.changed + ' changed');
    if (r.removed) found.push(r.removed + ' removed');
    const n = r.new + r.changed + r.removed;
    const files = n ? found.join(', ') + ' file' + (n === 1 ? '' : 's') : 'no new or changed files';
    return 'Rescan: ' + files + ' · ' + fmt(r.folders) + ' folders';
  }

  function scanDirectories(t, notify) {
    if (scans[t]) return scans[t];
    state.scanning[t] = true;
    const busy = startBusy('Scanning ' + MEDIA[t].label.toLowerCase() + ' folders');
    scans[t] = api('/scan', { method: 'POST', body: { media: t } })
      .then((r) => {
        const res = (r && r[t]) || null;
        if (notify || (res && res.available === false)) toast(scanSummary(t, res));
        return res;
      })
      .catch((err) => {
        toast('Rescan failed: ' + err.message);
        return null;
      })
      .finally(() => {
        state.scanning[t] = false;
        scans[t] = null;
        endBusy(busy);
        invalidate('dir:' + t);
        if (state.browse.media === t) state.browse = { media: null, rel: null, rows: null, loading: false };
        const done = () => { if (currentRoute() === 'directories') render(); };
        refreshDash().then(done, done);
      });
    return scans[t];
  }

  function openDirectories() {
    const t = state.media;
    invalidate('dir:' + t);                 // read the tree again, also when the scan is slow
    scanDirectories(t, false);              // new files and folders become visible when it finishes
  }

  /* ----------------------------------------------------- derived helpers */
  const app = () => document.getElementById('app');
  const dashMedia = (t) => (state.dash ? state.dash.media[t] : null);
  const runState = () => (state.dash ? state.dash.status : 'stopped');
  const enabledTypes = () => ALL.filter((t) => state.dash && state.dash.types[t]);

  function whenLabel(x) {
    return DAYS[(x.getDay() + 6) % 7] + ' ' + pad2(x.getHours()) + ':00';
  }

  function dayAndClock(iso) {
    if (!iso) return 'never';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return 'never';
    const today = new Date();
    const sameDay = d.toDateString() === today.toDateString();
    return (sameDay ? '' : DAYS[(d.getDay() + 6) % 7] + ' ') + pad2(d.getHours()) + ':' + pad2(d.getMinutes());
  }

  /* last scan: "14:02" today, "Tue 22:15" otherwise */
  function scanLine(d) {
    if (d.mode === 'off') return 'Not scanning while the app is off.';
    const every = d.scan_every ? 'every ' + d.scan_every + ' min' : 'periodically';
    return 'Looking for new files ' + every
      + ' · last ' + (d.last_scan ? dayAndClock(d.last_scan) : 'not yet')
      + ' · next ' + (d.next_scan ? dayAndClock(d.next_scan) : '—');
  }

  function scheduleText(t) {
    const m = dashMedia(t);
    if (!m) return '';
    if (!m.next_change) return m.in_schedule_window ? 'Always on' : 'No active hours (manual runs only)';
    const when = whenLabel(new Date(m.next_change));
    return m.in_schedule_window ? 'Window open until ' + when : 'Window opens ' + when;
  }

  function tree(t) { return state.trees[t]; }
  function walk(nodes, fn) { nodes.forEach((n) => { fn(n); walk(n.children, fn); }); }
  function subtree(node) { const out = [node]; walk(node.children, (n) => out.push(n)); return out; }
  function subtreeState(node) {
    const flags = subtree(node).map((n) => n.enabled);
    return flags.every(Boolean) ? 'on' : flags.some(Boolean) ? 'mixed' : 'off';
  }

  function baselineValue(lufs) {
    return Math.max(-30, Math.min(-10, Math.round(lufs * 2) / 2));
  }

  function folderLabel(dir) {
    const segs = String(dir || '').split('/').filter((s) => s && s !== '.');
    return segs.slice(-2).join('/') || '(base folder)';
  }

  function statusLabel(st) {
    return '<span class="st st-' + esc(st) + '">' + esc(STATUS_LABEL[st] || st) + '</span>';
  }

  function stat(label, value, sub) {
    return '<div class="stat"><div class="stat-label">' + label + '</div>'
      + '<div class="stat-value">' + value + '</div><div class="stat-sub">' + sub + '</div></div>';
  }

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

  /* ------------------------------------------------------------- dashboard */
  function mediaPanel(t) {
    const m = dashMedia(t);
    return '<section class="card">'
      + '<div class="card-head"><div><h2>' + MEDIA[t].label + '</h2>'
      + '<div class="mono sub">' + MEDIA[t].root + '</div></div>'
      + '<span class="chip' + (m.in_schedule_window ? '' : ' is-out') + '">' + (m.in_schedule_window ? 'In window' : 'Outside window') + '</span></div>'
      + '<div class="stats">'
      + stat('1 · Folders / files in scope', m.directories_enabled + ' / ' + fmt(m.files_total), 'of ' + m.directories_total + ' folders')
      + stat('2 · To do', fmt(m.files_pending), 'waiting for analysis or normalization')
      + stat('3 · Checked', fmt(m.files_checked), 'files with a measured level')
      + stat('4 · Actually changed', fmt(m.files_changed), 'level set to ' + Number(m.target).toFixed(1) + ' LUFS')
      + stat('5 · Parked', fmt(m.files_parked), '<a href="#/parked">waiting for your decision</a>')
      + '</div>'
      + '<p class="small muted" style="margin-top:12px">' + esc(scheduleText(t)) + '</p>'
      + '</section>';
  }

  function runCard(d, checked, total, pct, noTypes, outside) {
    const mode = MODE_LABEL[d.mode] ? d.mode : 'bulk';
    const isOff = mode === 'off';
    const isAuto = mode === 'auto';
    const status = d.status;
    const dotClass = status === 'running' ? 'dot-running' : status === 'paused' ? 'dot-paused'
      : (isAuto && status === 'idle') ? 'dot-watching' : '';
    const stateText = isOff ? 'the app is not doing anything'
      : status === 'running' ? (isAuto ? 'normalizing a new file' : 'normalizing')
        : status === 'paused' ? 'paused by you'
          : status === 'waiting_window' ? 'waiting for the schedule window'
            : status === 'idle' ? (isAuto ? 'watching for new files' : 'idle')
              : (isAuto ? 'starting' : 'not started');
    const startOn = !isOff && !isAuto && status === 'stopped' && !noTypes;
    const pauseOn = !isOff && status !== 'stopped';
    let buttons = '';
    if (isOff) {
      buttons = '<a class="btn" href="#/settings">Change the mode in Settings</a>';
    } else if (isAuto) {
      buttons = '<button class="btn' + (status === 'paused' ? ' btn-primary' : '') + '" data-action="run" data-path="pause">'
        + (status === 'paused' ? 'Resume' : 'Pause') + '</button>';
    } else {
      buttons = '<button class="btn btn-primary" data-action="run" data-path="start"' + (startOn ? '' : ' disabled') + '>Start</button>'
        + '<button class="btn" data-action="run" data-path="pause"' + (pauseOn ? '' : ' disabled') + '>'
        + (status === 'paused' ? 'Resume' : 'Pause') + '</button>'
        + '<button class="btn" data-action="run" data-path="stop"' + (pauseOn ? '' : ' disabled') + '>Stop</button>';
    }
    buttons += '<a class="btn" href="#/directories">Browse folders</a>';
    const explanation = isOff
      ? 'The app is off: folders are not scanned and no file is normalized. Rescan and Check level still work when you use them.'
      : isAuto
        ? 'Automatic: new and changed files are found and normalized by themselves, within the schedule and the folders that are included here.'
        : 'Bulk: press Start to check and normalize the files that are in the library now. Files that appear later wait for the next Start.';
    const typesLocked = !isOff && mode === 'bulk' && status !== 'stopped';   // no switches during a bulk run
    return '<section class="card">'
      + '<div class="run-top"><div class="run-state"><span class="dot ' + dotClass + '"></span>'
      + '<strong>' + esc(MODE_LABEL[mode]) + '</strong>'
      + '<span class="muted">· ' + esc(stateText) + '</span>'
      + '</div><div class="btns">' + buttons + '</div></div>'
      + '<p class="small muted mode-line">' + esc(explanation) + '</p>'
      + '<p class="small muted mode-line">' + esc(scanLine(d)) + '</p>'
      + '<div class="progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' + pct + '"><div style="width:' + pct + '%"></div></div>'
      + '<div class="run-meta"><span><strong style="color:var(--text)">' + pct + '%</strong> complete</span>'
      + '<span>' + fmt(checked) + ' of ' + fmt(total) + ' files checked</span>'
      + '<span class="mono">current: ' + (d.current_file ? esc(d.current_file) : '—') + '</span></div>'
      + '<div class="run-types"><span class="muted">Process</span>'
      + tip(isAuto ? 'A switched-off type is left alone by automatic mode as well.'
        : 'Switches can be changed while the run is stopped. A switched-off type is not processed.')
      + ALL.map((t) => '<label class="inline"><input class="switch" type="checkbox" data-run-type="' + t + '"'
        + (d.types[t] ? ' checked' : '') + (typesLocked ? ' disabled' : '') + '> ' + MEDIA[t].label + '</label>').join('')
      + (noTypes ? '<span class="warn-text">Select at least one type to start.</span>' : '')
      + '</div>'
      + (d.dry_run ? '<div class="notice info">Dry run is on: files are checked, nothing is written.</div>' : '')
      + (outside.length ? '<div class="notice">Running outside the schedule of ' + outside.map((t) => MEDIA[t].label).join(' and ')
        + ' (started manually). Stop the run to return to the schedule.</div>' : '')
      + '</section>';
  }

  function renderDashboard() {
    const d = state.dash;
    const s = state.settings;
    const status = d.status;
    const mode = MODE_LABEL[d.mode] ? d.mode : 'bulk';
    const types = enabledTypes();
    const noTypes = types.length === 0;
    const total = types.reduce((a, t) => a + dashMedia(t).files_total, 0);
    const checked = types.reduce((a, t) => a + dashMedia(t).files_checked, 0);
    const outside = status === 'running' ? types.filter((t) => !dashMedia(t).in_schedule_window) : [];
    const writeLabel = (t) => {
      const cfg = s[t];
      if (cfg.writeMode === 'backup') return 'in place, backup kept';
      if (cfg.writeMode === 'output') return 'output folder';
      return 'in place, no backup';
    };
    const parkedTotal = ALL.reduce((a, t) => a + dashMedia(t).files_parked, 0);
    const errors = ALL.reduce((a, t) => a + (dashMedia(t).files_errors || 0), 0);
    const logs = d.log.slice(0, 14).map((e) => {
      const when = new Date(e.ts * 1000);
      const cls = e.level === 'error' ? 'lv-err' : e.level === 'warn' ? 'lv-warn' : '';
      return '<li><time>' + pad2(when.getHours()) + ':' + pad2(when.getMinutes()) + ':' + pad2(when.getSeconds()) + '</time>'
        + '<span class="tag-type">' + (e.media ? MEDIA[e.media].label.slice(0, 3).toUpperCase() : '—') + '</span>'
        + '<span class="' + cls + '">' + esc(e.msg) + '</span></li>';
    }).join('');
    const pct = d.progress_percent;

    app().innerHTML = pageHead('Dashboard', 'Audio and video are tracked separately: each has its own folders, schedule, settings and counters.', '')
      + runCard(d, checked, total, pct, noTypes, outside)
      + '<div class="split-2">' + mediaPanel('audio') + mediaPanel('video') + '</div>'
      + '<div class="split-2">'
      + '<section class="card"><h2 style="margin-bottom:12px">Needs attention</h2><dl class="rows">'
      + '<div><dt>Mode</dt><dd>' + esc(MODE_LABEL[mode]) + ' <span class="muted">· ' + esc(MODE_SHORT[mode]) + '</span>'
      + ' · <a href="#/settings">change</a></dd></div>'
      + '<div><dt>Look for new files</dt><dd>' + (mode === 'off' ? '<span class="muted">off</span>'
        : 'every ' + fmt(d.scan_every) + ' min · last ' + esc(d.last_scan ? dayAndClock(d.last_scan) : 'not yet')
          + ' · next ' + esc(d.next_scan ? dayAndClock(d.next_scan) : '—')) + '</dd></div>'
      + '<div><dt>Parked for review</dt><dd><a href="#/parked">Audio ' + dashMedia('audio').files_parked + ' · Video ' + dashMedia('video').files_parked + '</a>'
      + (parkedTotal ? '' : ' <span class="muted">(none)</span>') + '</dd></div>'
      + '<div><dt>Errors</dt><dd>' + errors + '</dd></div>'
      + '<div><dt>Last run finished</dt><dd>' + (d.last_finished ? new Date(d.last_finished).toLocaleString('en-GB') : 'never') + '</dd></div>'
      + '<div><dt>Write mode</dt><dd>Audio: ' + writeLabel('audio') + ' · Video: ' + writeLabel('video') + '</dd></div>'
      + '<div><dt>Large-change override</dt><dd>Audio ' + (s.audio.override ? 'on' : 'off') + ' · Video ' + (s.video.override ? 'on' : 'off') + '</dd></div>'
      + '<div><dt>Dry run</dt><dd>' + (s.general.dryRun ? 'on, nothing is written' : 'off') + '</dd></div>'
      + '</dl></section>'
      + '<section class="card"><div class="card-head"><h2>Activity</h2>'
      + '<button class="btn btn-sm" data-action="clear-log"' + (d.log.length ? '' : ' disabled') + '>Clear list</button></div><ul class="log">'
      + (logs || '<li><span class="muted">No activity yet.</span></li>') + '</ul></section>'
      + '</div>';
  }

  /* ----------------------------------------------------------- directories */
  function baselinePanel(t) {
    const m = state.measure && state.measure.media === t ? state.measure : null;
    const target = state.settings[t].target;
    let html = '';
    if (state.baselineFrom === t) {
      html += '<div class="notice info baseline-banner"><span><strong>Setting the baseline for Settings, Level.</strong> '
        + 'Check the level of a representative file below, then use it as the target. The target is now '
        + '<strong>' + Number(target).toFixed(1) + ' LUFS</strong>.</span>'
        + '<a class="btn btn-sm" href="#/settings" data-action="baseline-done">Back to Settings</a></div>';
    }
    if (m && m.state === 'measuring') {
      html += '<div class="measure is-busy"><span class="working" aria-hidden="true"></span>'
        + '<span>Measuring <strong>' + esc(m.name) + '</strong> … (integrated loudness, EBU R128, file not changed)</span></div>';
    } else if (m) {
      const v = baselineValue(m.lufs);
      html += '<div class="measure">'
        + '<div><div class="stat-label">Checked file</div><strong>' + esc(m.name) + '</strong></div>'
        + '<div><div class="stat-label">Integrated loudness</div><strong>' + m.lufs.toFixed(1) + ' LUFS</strong></div>'
        + '<div><div class="stat-label">True peak</div><strong>' + Number(m.tp).toFixed(1) + ' dBTP</strong></div>'
        + '<div><div class="stat-label">Change needed at target ' + Number(target).toFixed(1) + '</div><strong>' + fmtDb(round1(target - m.lufs)) + '</strong></div>'
        + '<span class="grow"></span>'
        + '<button class="btn btn-primary" data-action="use-baseline">Use ' + v.toFixed(1) + ' LUFS as target' + (v !== Math.round(m.lufs * 2) / 2 ? ' (slider limit)' : '') + '</button>'
        + '<button class="btn" data-action="measure-close">Dismiss</button></div>';
    }
    return html;
  }

  function renderTree(nodes) {
    return nodes.map((n) => {
      const st = subtreeState(n);
      const hasKids = n.children.length > 0;
      const active = state.selected[n.type] === n.rel;
      return '<li class="' + (n.enabled ? '' : 'disabled-node') + '">'
        + '<div class="tree-row">'
        + '<button class="caret' + (hasKids ? '' : ' leaf') + '" data-action="tree-toggle" data-rel="' + esc(n.rel) + '" aria-label="Expand or collapse ' + esc(n.name) + '">'
        + (n.open ? '▾' : '▸') + '</button>'
        + '<input type="checkbox" data-check="' + esc(n.rel) + '" aria-label="Include ' + esc(n.name) + '"' + (st === 'on' ? ' checked' : '') + '>'
        + '<button class="folder' + (active ? ' active' : '') + '" data-action="tree-browse" data-rel="' + esc(n.rel) + '" title="' + esc(n.rel) + '">'
        + '<span class="folder-name">' + esc(n.name) + '</span></button>'
        + '<span class="count">' + fmt(n.files) + ' files</span>'
        + '</div>'
        + (hasKids && n.open ? '<ul>' + renderTree(n.children) + '</ul>' : '')
        + '</li>';
    }).join('');
  }

  function renderBrowse(node) {
    const t = node.type;
    const b = state.browse;
    const head = '<div class="card-head" style="margin-bottom:8px"><div><h2>' + esc(MEDIA[t].root + '/' + node.rel) + '</h2>'
      + '<div class="sub">' + node.directFiles + ' files directly in this folder · ' + (subtree(node).length - 1) + ' sub-folders'
      + ' · Play listens to the original · Edit fades or cuts a part</div></div>'
      + '<span class="chip' + (node.enabled ? '' : ' is-out') + '">' + (node.enabled ? 'Included' : 'Excluded') + '</span></div>';
    if (b.loading || b.media !== t || b.rel !== node.rel || !b.rows) {
      return head + '<p class="muted">Loading files…</p>';
    }
    if (!b.rows.length) {
      return head + '<p class="muted">No files directly in this folder. Open a sub-folder in the tree.</p>';
    }
    const rows = b.rows.map((f, i) => {
      const ext = String(f.name).split('.').pop();
      const checking = !!(state.measure && state.measure.relpath === f.relpath && state.measure.state === 'measuring');
      const playing = PLAY.media === t && PLAY.relpath === f.relpath && PLAY.playing;
      const editing = t === 'audio' && !!f.playable
        ? '<button class="btn btn-sm" data-action="edit-file" data-idx="' + i + '"'
          + (f.status === 'processing'
            ? ' disabled title="This file is being processed right now"'
            : ' title="Edit ' + esc(f.name) + ': fades and cut"') + '>Edit</button>'
        : '';
      const listen = !f.playable
        ? '<button class="btn btn-sm" disabled title="This format cannot be played in the browser">Play</button>'
        : '<button class="btn btn-sm' + (playing ? ' btn-primary' : '') + '" data-action="preview" data-idx="' + i
          + '" aria-pressed="' + playing + '" title="' + esc((playing ? 'Pause ' : 'Listen to ') + f.name) + '">'
          + (playing ? 'Pause' : 'Play') + '</button>';
      const check = checking
        ? '<span class="row-busy"><span class="working" aria-hidden="true"></span>Checking…</span>'
        : '<button class="btn btn-sm" data-action="check-level" data-idx="' + i + '">Check level</button>';
      const cls = checking ? 'is-busy-row' : playing ? 'is-playing-row' : '';
      return '<tr' + (cls ? ' class="' + cls + '"' : '') + '>'
        + '<td>' + esc(f.name) + '</td><td class="mono">' + esc(ext) + '</td>'
        + '<td class="num">' + fmtLufs(f.lufs) + '</td>'
        + '<td class="num">' + fmtDbN(f.gain_db) + '</td>'
        + '<td>' + esc(METHOD_LABEL[f.method] || f.method || '—') + '</td>'
        + '<td>' + statusLabel(f.status) + '</td>'
        + '<td class="actions-cell">' + listen + editing + check + '</td></tr>';
    }).join('');
    return head
      + '<div class="table-wrap"><table><thead><tr><th>File</th><th>Format</th><th class="num">Level'
      + tip('Integrated loudness of the whole file (EBU R128), measured without changing the file. Check level measures one file again.')
      + '</th><th class="num">Change</th><th>Method</th><th>Status</th><th></th></tr></thead><tbody>' + rows + '</tbody></table></div>'
      + (b.rows.length >= 500 ? '<p class="small muted" style="margin-top:10px">Showing the first 500 files.</p>' : '');
  }

  function renderDirectories() {
    const t = state.media;
    const tr = tree(t);
    const m = dashMedia(t);
    const selRel = state.selected[t];
    const sel = selRel && tr.map.get(selRel);
    const scanning = !!state.scanning[t];
    if (sel && (state.browse.media !== t || state.browse.rel !== sel.rel) && !state.browse.loading) {
      loadBrowse(sel.rel);
    }
    const dirs = !tr.roots.length
      ? '<p class="muted">No folders found under the base location. Check the mount in docker-compose.yml, then use Rescan.</p>'
      : '';
    app().innerHTML = pageHead('Directories',
      'Base location <span class="mono">' + MEDIA[t].root + '</span>, mounted from <span class="mono">' + MEDIA[t].host
      + '</span> in docker-compose.yml. Untick a folder to exclude it. Ticking or unticking applies to its sub-folders too.',
      '<button class="btn btn-sm' + (scanning ? ' is-busy' : '') + '" data-action="rescan"' + (scanning ? ' disabled' : '') + '>'
      + (scanning ? 'Scanning…' : 'Rescan') + '</button>'
      + tip('Looks on disk for new, changed and removed files and folders, then updates this page. The same check runs when you open this tab.')
      + '<button class="btn btn-sm" data-action="select-all">Select all</button>'
      + '<button class="btn btn-sm" data-action="select-none">Select none</button>'
      + '<button class="btn btn-sm" data-action="expand-all">Expand all</button>'
      + '<button class="btn btn-sm" data-action="collapse-all">Collapse all</button>', true)
      + '<p class="muted" style="margin-bottom:0"><strong style="color:var(--text)">' + m.directories_enabled + ' of ' + m.directories_total
      + '</strong> folders included · ' + fmt(m.files_total) + ' files in scope'
      + (scanning ? ' · <span class="scan-note">looking for new files and folders…</span>' : '') + '</p>'
      + baselinePanel(t)
      + dirs
      + '<div class="split-dirs">'
      + '<section class="card tree" aria-label="Folder tree"><ul>' + renderTree(tr.roots) + '</ul></section>'
      + '<section class="card">' + (sel ? renderBrowse(sel) : '<p class="muted">Select a folder in the tree.</p>') + '</section>'
      + '</div>';
    app().querySelectorAll('input[data-check]').forEach((cb) => {
      const n = tr.map.get(cb.dataset.check);
      cb.indeterminate = !!n && subtreeState(n) === 'mixed';
    });
  }

  /* ---------------------------------------------------------------- parked */
  const effective = (f) => tenth(Number(f.effective));
  // same bounds as the server: +/-12 dB, widened when the measured change is larger
  const sliderMin = (f) => Math.min(-12, Math.floor(Number(f.gain_db)));
  const sliderMax = (f) => Math.max(12, Math.ceil(Number(f.gain_db)));

  function parkedRow(f, t, lossy, sel) {
    const id = String(f.id);
    const on = sel.includes(f.id);
    const folder = folderLabel(f.dir);
    const full = MEDIA[t].root + '/' + (f.dir && f.dir !== '.' ? f.dir : '');
    const check = '<td class="pick"><input type="checkbox" data-park-check="' + id + '" aria-label="Select ' + esc(f.name) + '"' + (on ? ' checked' : '') + '></td>';
    const fileCell = '<td class="file-cell">' + esc(f.name) + '</td>'
      + '<td class="dim mono folder-cell" title="' + esc(full) + '">' + esc(folder) + '</td>'
      + '<td class="num">' + fmtLufs(f.lufs) + '</td>';
    if (lossy) {
      return '<tr' + (on ? ' class="row-sel"' : '') + '>' + check + fileCell
        + '<td class="chg"><span class="chg-orig" title="Change needed">' + fmtDbN(f.gain_db) + '</span></td>'
        + '<td class="actions"><button class="btn btn-sm btn-primary" data-action="approve" data-file="' + id + '">Re-encode</button>'
        + '<button class="btn btn-sm" data-action="skip" data-file="' + id + '">Skip</button></td></tr>';
    }
    const warn = f.warn;
    return '<tr' + (on ? ' class="row-sel"' : '') + '>' + check + fileCell
      + '<td class="chg"><div class="chg-vals">'
      + '<span class="chg-orig" title="Original change">' + fmtDbN(f.gain_db) + '</span>'
      + '<span class="chg-arrow" aria-hidden="true">→</span>'
      + '<strong class="chg-new' + (warn ? ' warn' : '') + '" data-park-new="' + id + '">' + fmtDbN(effective(f)) + '</strong></div>'
      + '<div class="chg-ctl">'
      + '<button class="btn btn-sm" data-action="adjust-file" data-file="' + id + '" data-step="-0.1" aria-label="Decrease change by 0.1 dB">−</button>'
      + '<input class="mini" type="range" min="' + sliderMin(f) + '" max="' + sliderMax(f) + '" step="0.1" value="' + effective(f) + '" data-park-slider="' + id + '" aria-label="Change for ' + esc(f.name) + '">'
      + '<button class="btn btn-sm" data-action="adjust-file" data-file="' + id + '" data-step="0.1" aria-label="Increase change by 0.1 dB">+</button>'
      + '</div></td>'
      + '<td class="actions"><button class="btn btn-sm btn-primary" data-action="approve" data-file="' + id + '">Apply change</button>'
      + '<button class="btn btn-sm" data-action="skip" data-file="' + id + '">Skip</button></td></tr>';
  }

  function parkedTable(files, t, lossy, sel) {
    const allOn = files.length > 0 && files.every((f) => sel.includes(f.id));
    const head = '<thead><tr><th class="pick"><input type="checkbox" data-park-all aria-label="Select all in this list"' + (allOn ? ' checked' : '') + '></th>'
      + '<th>File</th><th>Folder</th><th class="num">Level</th>'
      + (lossy ? '<th>Change needed</th><th></th>' : '<th>Change needed <span class="dim">(original → new)</span></th><th></th>')
      + '</tr></thead>';
    const cols = lossy
      ? '<col class="c-pick"><col class="c-file"><col class="c-folder"><col class="c-level"><col class="c-change"><col class="c-act">'
      : '<col class="c-pick"><col class="c-file"><col class="c-folder"><col class="c-level"><col class="c-change"><col class="c-act">';
    return '<div class="table-wrap"><table class="parked"><colgroup>' + cols + '</colgroup>' + head
      + '<tbody>' + files.map((f) => parkedRow(f, t, lossy, sel)).join('') + '</tbody></table></div>';
  }

  function renderParked() {
    const t = state.media;
    const data = state.parked[t];
    const th = data.threshold;
    const large = data.files.filter((f) => f.reason !== 'lossy_no_gain_tool');
    const lossy = data.files.filter((f) => f.reason === 'lossy_no_gain_tool');
    const ids = data.files.map((f) => f.id);
    state.parkSel = state.parkSel.filter((id) => ids.includes(id));
    const sel = state.parkSel;
    const sortOpts = Object.keys(PARK_SORTS).map((k) => '<option value="' + k + '"' + (state.parkSort === k ? ' selected' : '') + '>' + PARK_SORTS[k] + '</option>').join('');
    const sortBar = '<div class="card-tools"><label for="park-sort">Sort by</label><select id="park-sort" class="input" data-park-sort>' + sortOpts + '</select></div>';
    const bulk = sel.length
      ? '<div class="bulk-bar"><span><strong>' + sel.length + '</strong> selected</span>'
        + '<span class="bulk-steps"><button class="btn btn-sm" data-action="bulk-adjust" data-step="-0.1">− 0.1 dB</button>'
        + '<button class="btn btn-sm" data-action="bulk-adjust" data-step="0.1">+ 0.1 dB</button></span>'
        + '<span class="grow"></span>'
        + '<button class="btn btn-sm btn-primary" data-action="apply-selected">Apply selected</button>'
        + '<button class="btn btn-sm" data-action="clear-selection">Clear selection</button></div>'
      : '';
    const largeSection = '<section class="card"><div class="card-head"><div><h2>Large changes</h2>'
      + '<div class="sub">' + MEDIA[t].label + ' changes larger than <strong>' + th + ' dB</strong> are not applied. The run continues with the next file.</div></div>'
      + '<button class="btn btn-primary" data-action="approve-all" data-reason="large_change"' + (large.length ? '' : ' disabled') + '>Apply all (' + large.length + ')</button></div>'
      + (large.length
        ? sortBar + bulk + parkedTable(large, t, false, sel)
        : '<p class="muted">Nothing is parked for a large change in ' + MEDIA[t].label.toLowerCase() + '.</p>')
      + '</section>';
    const lossySection = '<section class="card" style="margin-top:16px"><div class="card-head"><div><h2>Lossy files without a gain tool</h2>'
      + '<div class="sub">These files (WMA, OGG, or AAC/M4A without aacgain) can only be changed by re-encoding. Re-encode keeps the codec and bitrate; skip leaves the file as it is.</div></div>'
      + '<button class="btn btn-primary" data-action="approve-all" data-reason="lossy_no_gain_tool"' + (lossy.length ? '' : ' disabled') + '>Re-encode all (' + lossy.length + ')</button></div>'
      + (lossy.length ? parkedTable(lossy, t, true, sel) : '<p class="muted">No lossy files are waiting.</p>')
      + '</section>';
    app().innerHTML = pageHead('Parked files',
      'Files that need a decision are listed here. Each one is parked, not applied, until you approve it.',
      '<a class="btn" href="#/settings">Threshold</a>', true)
      + (t === 'audio' ? largeSection + lossySection : largeSection);
  }

  /* -------------------------------------------------------------- schedule */
  function renderSchedule() {
    const t = state.media;
    const sc = state.sched[t];
    const hours = Array.from({ length: 24 }, (_, h) => h);
    const head = '<tr><th></th>' + hours.map((h) => '<th>' + pad2(h) + '</th>').join('') + '</tr>';
    const body = DAYS.map((d, di) => '<tr><td class="dayname">' + d + '</td>'
      + hours.map((h) => '<td class="cell' + (sc[di][h] ? ' on' : '') + '" data-d="' + di + '" data-h="' + h
        + '" title="' + d + ' ' + pad2(h) + ':00"></td>').join('') + '</tr>').join('');
    app().innerHTML = pageHead('Schedule',
      MEDIA[t].label + ' is processed only inside the green windows. Click or drag over the grid to change hours. Times are local.',
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

  function dayRanges(t, di) {
    const row = state.sched[t][di];
    const out = [];
    let start = null;
    for (let h = 0; h <= 24; h++) {
      const on = h < 24 && row[h];
      if (on && start === null) start = h;
      if (!on && start !== null) { out.push(pad2(start) + ':00–' + pad2(h) + ':00'); start = null; }
    }
    return out.length ? out.join(', ') : 'off';
  }

  function updateScheduleInfo() {
    const t = state.media;
    const list = document.getElementById('sched-summary');
    if (list) list.innerHTML = DAYS.map((d, i) => '<li><span class="muted">' + d + '</span><span>' + dayRanges(t, i) + '</span></li>').join('');
    const now = document.getElementById('sched-now');
    if (now) now.textContent = scheduleText(t);
  }

  async function saveGrid(t) {
    state.sched = await api('/schedule', { method: 'PUT', body: { [t]: state.sched[t] } });
  }

  /* ------------------------------------------------------------- settings */
  function writeOptions(t) {
    const cfg = state.draft[t];
    const opts = [
      ['inplace', 'Replace in place (default)', 'After verification the file is replaced. No copy of the original is kept.'],
      ['backup', 'Replace in place, keep a backup', 'The original is copied to /config/backups first. Needs extra disk space.'],
      ['output', 'Write to a separate output folder', 'The library is never touched. Normalized copies go to the folder below.'],
    ].map(([v, title, d]) => '<label class="opt"><input type="radio" name="writeMode-' + t + '" value="' + v + '"'
      + ' data-scope="' + t + '" data-setting="writeMode"' + (cfg.writeMode === v ? ' checked' : '') + '>'
      + '<span><strong style="font-weight:500">' + title + '</strong>' + tip(d) + '</span></label>').join('');
    const outField = cfg.writeMode === 'output'
      ? '<div class="field" style="margin:4px 0 0 28px"><label class="lbl" for="out-' + t + '">Output folder (inside the container)'
        + tip('Mount it in docker-compose.yml, for example /volume1/' + t + '-normalized:' + cfg.outputDir) + '</label>'
        + '<input class="input" id="out-' + t + '" data-scope="' + t + '" data-setting="outputDir" value="' + esc(cfg.outputDir) + '">'
        + '</div>'
      : '';
    return '<div class="field"><span class="lbl">How changed files are written</span>' + opts + outField + '</div>';
  }

  function levelCard(t) {
    const cfg = state.draft[t];
    return '<section class="card"><div class="card-head"><div><h2>Level</h2><div class="sub">Applies to ' + MEDIA[t].label.toLowerCase() + ' files</div></div></div>'
      + '<div class="field"><label class="lbl" for="target-' + t + '">Target loudness (integrated, EBU R128)'
      + tip('−16 LUFS suits podcasts and speech, −14 suits music streaming, −23 is the EBU broadcast standard.') + '</label>'
      + '<div class="slider-row"><input id="target-' + t + '" type="range" min="-30" max="-10" step="0.5" value="' + cfg.target + '" data-scope="' + t + '" data-setting="target">'
      + '<div class="val"><span data-out="' + t + '.target">' + Number(cfg.target).toFixed(1) + '</span> LUFS</div></div>'
      + '<p class="baseline-link-row"><a class="inline-link" href="#/directories" data-action="baseline-start" data-media="' + t + '">Set the baseline from a file: check its level in Directories →</a></p>'
      + '</div>'
      + '<div class="field"><label class="lbl" for="tp-' + t + '">True-peak ceiling'
      + tip('The gain is capped so that no sample exceeds this level, which prevents clipping.') + '</label>'
      + '<div class="slider-row"><input id="tp-' + t + '" type="range" min="-6" max="-1" step="0.1" value="' + cfg.truePeak + '" data-scope="' + t + '" data-setting="truePeak">'
      + '<div class="val"><span data-out="' + t + '.truePeak">' + Number(cfg.truePeak).toFixed(1) + '</span> dBTP</div></div>'
      + '</div>'
      + '<div class="field"><label class="lbl" for="th-' + t + '">Large-change threshold'
      + tip('Changes bigger than this are parked for review instead of being applied.') + '</label>'
      + '<div class="slider-row"><input id="th-' + t + '" type="range" min="1" max="12" step="0.5" value="' + cfg.threshold + '" data-scope="' + t + '" data-setting="threshold">'
      + '<div class="val"><span data-out="' + t + '.threshold">' + Number(cfg.threshold).toFixed(1) + '</span> dB</div></div>'
      + '</div>'
      + '<label class="check"><input class="switch" type="checkbox" data-scope="' + t + '" data-setting="override"' + (cfg.override ? ' checked' : '') + '>'
      + '<span><strong>Override the confirmation for large changes</strong>' + tip('Apply changes above the threshold automatically, without parking them.') + '</span></label>'
      + (cfg.override ? '<div class="notice">Large ' + MEDIA[t].label.toLowerCase() + ' changes will be applied without review.</div>' : '')
      + '</section>';
  }

  function outputCard(t) {
    if (t === 'audio') {
      const cfg = state.draft.audio;
      const lossy = [
        ['gain', 'Gain only, no re-encode', 'MP3 via mp3gain, AAC via aacgain. The audio data is untouched; level steps are about 1.5 dB, so the target is hit within ±0.75 dB. Files with no gain tool are parked for a decision.', true],
        ['reencode', 'Re-encode at the same codec and bitrate', 'Exact target level, but each re-encode adds a small generation loss.', false],
        ['skip', 'Skip lossy files', 'Only lossless files (FLAC, WAV) are normalized. MP3, AAC, WMA and OGG are left alone.', false],
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

  function modeCard() {
    const g = state.draft.general;
    const every = state.sched ? state.sched.scanEvery : 15;
    const mode = g.mode || 'bulk';
    const options = [
      ['auto', 'Automatic', 'New and changed files are checked and normalized by themselves. The folders are looked at every ' + every + ' minutes, within the schedule and the folders that are included.'],
      ['bulk', 'Bulk', 'Nothing happens until you press Start on the dashboard: then the files that are in the library are checked and normalized. Files that appear later wait for the next Start.'],
      ['off', 'Off', 'Nothing is scanned and nothing is normalized. Rescan, Check level and the audio editor still work when you use them.'],
    ].map(([v, title, d, def]) => '<label class="opt"><input type="radio" name="app-mode" value="' + v + '"'
      + ' data-scope="general" data-setting="mode"' + (mode === v ? ' checked' : '') + '>'
      + '<span><strong style="font-weight:500">' + title + '</strong>'
      + (v === 'bulk' ? '<span class="tag">default</span>' : '') + tip(d) + '</span></label>').join('');
    return '<section class="card"><div class="card-head"><div><h2>Mode</h2>'
      + '<div class="sub">Off, Bulk (you start it) or Automatic (new files are normalized for you)</div></div></div>'
      + '<div class="field" style="margin:0"><span class="lbl">Mode' + tip('Switches the whole app: Off, Bulk (start a run yourself) or Automatic (new files are found and normalized for you).') + '</span>'
      + options
      + '<p class="small muted" style="margin-top:10px">Automatic mode uses Settings, Scheduling: new files are looked for every '
      + every + ' minutes.</p>'
      + '</div></section>';
  }

  function scheduleOptions() {
    const sc = state.sched;
    return '<section class="card" style="margin-top:16px"><div class="card-head"><div><h2>Scheduling</h2>'
      + '<div class="sub">Applies to audio and video. The time windows are set on the Schedule tab.</div></div></div>'
      + '<div class="split-2" style="margin-top:0">'
      + '<div class="field" style="margin:0"><label class="lbl" for="scan-every">Look for new or changed files every'
      + tip('Used by Automatic mode: the folders are scanned this often and new or changed files are normalized. A manual Rescan also counts, so the next scan starts a full interval later. Also runs in Bulk mode, so the list of files stays current.') + '</label>'
      + '<div class="input-row" style="max-width:300px"><input class="input" id="scan-every" type="number" min="1" max="1440" data-sched="scanEvery" value="' + sc.scanEvery + '">'
      + '<span class="muted small">minutes</span></div>'
      + '</div>'
      + '<div class="field" style="margin:0"><span class="lbl">When a window closes during a file</span>'
      + '<label class="opt"><input type="radio" name="onclose" value="finish" data-sched="onClose"' + (sc.onClose === 'finish' ? ' checked' : '') + '>'
      + '<span><strong style="font-weight:500">Finish the current file, then pause</strong>' + tip('Recommended. No half-written files.') + '</span></label>'
      + '<label class="opt"><input type="radio" name="onclose" value="pause" data-sched="onClose"' + (sc.onClose === 'pause' ? ' checked' : '') + '>'
      + '<span><strong style="font-weight:500">Pause immediately</strong>' + tip('The work on the current file is stopped and its temporary output discarded. The file is retried in the next window.') + '</span></label>'
      + '</div></div></section>';
  }

  function renderSettings() {
    const t = state.media;
    const g = state.draft.general;
    app().innerHTML = pageHead('Settings',
      'Level and output are set separately for audio and video. Changes take effect when you press Save.',
      '<button class="btn" data-action="settings-reset">Reset ' + MEDIA[t].label.toLowerCase() + '</button>'
      + '<button class="btn btn-primary" data-action="settings-save">Save</button>', true)
      + modeCard()
      + '<section class="card"><div class="check-list">'
      + '<label class="check" style="padding-top:0"><input class="switch" type="checkbox" data-scope="general" data-setting="dryRun"' + (g.dryRun ? ' checked' : '') + '>'
      + '<span><strong>Dry run</strong>' + tip('Analyse and report only. Nothing is written. Recommended for the first run on a real library.') + '</span></label>'
      + '<p class="small muted" style="padding-top:12px;border-top:1px solid var(--line)">Web access has no login (local network only, as chosen). '
      + 'Do not forward the port to the internet. The Home Assistant API still requires its access token.</p>'
      + '</div></section>'
      + scheduleOptions()
      + '<div class="split-2">' + levelCard(t) + outputCard(t) + '</div>';
  }

  /* ------------------------------------------------------- Home Assistant */
  /* the entity ID Home Assistant derives from a name when it first creates the sensor (preview only) */
  function entityIdFor(name) {
    const slug = String(name).toLowerCase().normalize('NFKD').replace(/[\u0300-\u036f]/g, '')
      .replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
    return 'sensor.' + (slug || 'sensor');
  }

  function sensorRows(list) {
    let lastGroup = null;
    return list.map((x) => {
      const head = x.group !== lastGroup ? '<tr><td colspan="6" class="group-row">' + esc(x.group) + '</td></tr>' : '';
      lastGroup = x.group;
      const reset = x.name !== x.default_name
        ? '<button class="btn btn-sm" data-action="sensor-reset" data-path="' + esc(x.path) + '">Default</button>' : '';
      return head + '<tr><td><input type="checkbox" data-sensor="' + esc(x.path) + '"' + (x.enabled ? ' checked' : '') + ' aria-label="Enable ' + esc(x.default_name) + '"></td>'
        + '<td><div class="input-row"><input class="input" data-sensor-name="' + esc(x.path) + '" value="' + esc(x.name) + '" maxlength="80"'
        + ' aria-label="Name of ' + esc(x.default_name) + ' in Home Assistant">' + reset + '</div></td>'
        + '<td class="mono dim">' + esc(x.path) + '</td>'
        + '<td class="mono">' + esc(entityIdFor(x.name)) + '</td>'
        + '<td>' + (x.unit ? esc(x.unit) : '<span class="dim">—</span>') + '</td>'
        + '<td>' + (x.deviceClass ? esc(x.deviceClass) : '<span class="dim">—</span>') + '</td></tr>';
    }).join('');
  }

  function renderHA() {
    const h = state.ha;
    const d = state.haDraft;
    app().innerHTML = pageHead('Home Assistant',
      'Publishes the normalizer status as a local, token-protected API. Home Assistant reads it with RESTful sensors, and the Lovelace card shows every value.',
      '<button class="btn btn-primary" data-action="ha-save">Save</button>')
      + '<div class="split-2" style="margin-top:0">'
      + '<section class="card"><h2 style="margin-bottom:14px">Connection</h2>'
      + '<div class="field"><label class="lbl" for="ha-url">Home Assistant URL</label>'
      + '<input class="input" id="ha-url" data-ha="url" value="' + esc(d.url) + '"></div>'
      + '<div class="field"><label class="lbl" for="ha-token">Long-lived access token' + tip('Create it in Home Assistant under Profile, then Security, then Long-lived access tokens. Stored in the config volume and never sent back to the browser.') + '</label>'
      + '<div class="input-row"><input class="input" id="ha-token" data-ha="token" autocomplete="off" type="password" placeholder="' + (h.token_set ? 'Saved: ' + esc(h.token) + ' (type to replace)' : 'Paste the token') + '" value="' + esc(d.token) + '">'
      + '</div></div>'
      + '<div class="btns"><button class="btn" data-action="ha-test">Test connection</button>'
      + '<span class="muted small" id="ha-test-msg">' + esc(state.haTest) + '</span></div>'
      + '</section>'
      + '<section class="card"><h2 style="margin-bottom:14px">Local API endpoint</h2>'
      + '<pre>GET http://&lt;NAS-IP&gt;:' + h.port + '/api/v1/ha/status\nAuthorization: Bearer &lt;endpoint token&gt;</pre>'
      + '<p class="small muted">The endpoint token is written into the configuration below. Rotating it stops the current sensors until you paste the new configuration.</p>'
      + '<div class="btns"><button class="btn btn-sm" data-action="ha-rotate">Rotate endpoint token</button></div>'
      + '<h2 style="margin:18px 0 10px">Current payload</h2><pre id="ha-json">Loading…</pre></section>'
      + '</div>'

      + '<section class="card" style="margin-top:16px"><div class="card-head"><div><h2>Lovelace card</h2>'
      + '<div class="sub">One file that shows every parameter on a Home Assistant dashboard. No extra software.</div></div>'
      + '<a class="btn btn-primary" href="/static/audio-normalizer-card.js" download="audio-normalizer-card.js">Download card (.js)</a></div>'
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
      + '<div class="sub">Optional single sensors in addition to the main entity, which is always created. Name them as they are in Home Assistant. The name sets the entity ID only when Home Assistant first creates a sensor; an existing entity keeps its ID. Changes are saved at once.</div></div></div>'
      + '<div class="table-wrap"><table><thead><tr><th>On</th><th>Name in Home Assistant</th><th>Source in payload</th><th>Entity ID</th><th>Unit</th><th>Device class</th></tr></thead>'
      + '<tbody>' + sensorRows(h.sensors) + '</tbody></table></div></section>'
      + '<section class="card" style="margin-top:16px"><h2 style="margin-bottom:12px">Generated configuration.yaml</h2>'
      + '<pre id="ha-yaml"></pre></section>';
    refreshHaPreview();
  }

  /* previews: YAML from the server, payload and card from the same endpoint Home Assistant calls */
  async function refreshHaPreview() {
    const h = state.ha;
    if (!h) return;
    const y = document.getElementById('ha-yaml');
    if (y) y.textContent = h.yaml;
    const cy = document.getElementById('ha-card-yaml');
    if (cy) cy.textContent = h.card_yaml;
    const j = document.getElementById('ha-json');
    try {
      const res = await fetch('/api/v1/ha/status', { headers: { Authorization: 'Bearer ' + h.endpointToken } });
      const payload = await res.json();
      if (j) j.textContent = JSON.stringify(payload, null, 2);
      const card = document.getElementById('card-preview');
      if (card && typeof card.setConfig === 'function') {
        card.setConfig({ entity: 'sensor.audio_normalizer', title: 'Audio Normalizer', show_audio: true, show_video: true, show_details: true, fields: h.enabled_paths });
        card.hass = { states: { 'sensor.audio_normalizer': { state: payload.status, attributes: payload } } };
      }
    } catch (err) {
      if (j) j.textContent = 'Payload not available: ' + err.message;
    }
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
  const TITLES = { dashboard: 'Dashboard', directories: 'Directories', parked: 'Parked files', schedule: 'Schedule', settings: 'Settings', homeassistant: 'Home Assistant' };

  function currentRoute() {
    const r = location.hash.replace(/^#\/?/, '') || 'dashboard';
    return ROUTES[r] ? r : 'dashboard';
  }

  function updateNavBadge() {
    const el = document.getElementById('parkedBadge');
    if (el && state.dash) el.textContent = ALL.reduce((a, t) => a + dashMedia(t).files_parked, 0);
  }

  function render() {
    const route = currentRoute();
    document.querySelectorAll('#nav a').forEach((a) => {
      if (a.dataset.route === route) a.setAttribute('aria-current', 'page');
      else a.removeAttribute('aria-current');
    });
    updateNavBadge();
    showActivity();
    const keys = keysFor(route);
    const failed = keys.map((k) => state.errors[k]).find(Boolean);
    if (failed) {
      app().innerHTML = pageHead(TITLES[route], '', '')
        + '<section class="card"><p class="warn-text">Could not load this page: ' + esc(failed) + '</p>'
        + '<div class="btns" style="margin-top:12px"><button class="btn" data-action="retry">Try again</button></div></section>';
      return;
    }
    const missing = keys.filter((k) => !state.loaded[k]);
    if (missing.length) {
      app().innerHTML = pageHead(TITLES[route], '', '') + '<p class="muted">Loading…</p>';
      missing.forEach((k) => {
        if (!state.pending[k]) loadKey(k).then(render);
      });
      return;
    }
    ROUTES[route]();
    document.querySelectorAll('input[type=range]').forEach(paintRange);
  }

  /* --------------------------------------------------------- interaction */
  const SAVE_ERR = (err) => toast(err.message);

  async function onAction(el) {
    const a = el.dataset.action;
    const t = state.media;

    if (a === 'media') {
      state.media = el.dataset.media;
      state.browse = { media: null, rel: null, rows: null, loading: false };
      closePreview();
      render();
    } else if (a === 'run') {
      await api('/run/' + el.dataset.path, { method: 'POST' });
      await refreshDash();
      render();
    } else if (a === 'clear-log') {
      await api('/log/clear', { method: 'POST' });
      await refreshDash();
      render();
    } else if (a === 'retry') {
      keysFor(currentRoute()).forEach((k) => { delete state.errors[k]; invalidate(k); });
      render();
    } else if (a === 'tree-toggle') {
      const n = tree(t).map.get(el.dataset.rel);
      if (n) n.open = !n.open;
      render();
    } else if (a === 'tree-browse') {
      state.selected[t] = el.dataset.rel;
      render();
    } else if (a === 'rescan') {
      scanDirectories(t, true);       // the button shows Scanning… until the fresh tree is read
      render();
    } else if (a === 'preview') {
      const f = state.browse.rows && state.browse.rows[Number(el.dataset.idx)];
      if (f) playFile(t, f);
    } else if (a === 'edit-file') {
      const f = state.browse.rows && state.browse.rows[Number(el.dataset.idx)];
      if (f) openEditor(t, f);
    } else if (a === 'editor-close') {
      closeEditor();
    } else if (a === 'editor-play') {
      toggleEditorPlay();
    } else if (a === 'editor-zoom-in') {
      zoomBy(1 / ZOOM_STEP, editorAnchor());
    } else if (a === 'editor-zoom-out') {
      zoomBy(ZOOM_STEP, editorAnchor());
    } else if (a === 'editor-fit') {
      fitEditor();
    } else if (a === 'editor-cut') {
      if (EDIT.sel && EDIT.sel.end - EDIT.sel.start >= 0.05) {
        EDIT.cut = { start: EDIT.sel.start, end: EDIT.sel.end };
        EDIT.sel = null;
        syncEditor();
        drawEditor();
      }
    } else if (a === 'editor-uncut') {
      EDIT.cut = null;
      syncEditor();
      drawEditor();
    } else if (a === 'editor-clear-sel') {
      EDIT.sel = null;
      syncEditor();
      drawEditor();
    } else if (a === 'editor-apply') {
      await applyEdit();
    } else if (a === 'player-toggle') {
      togglePreview();
    } else if (a === 'player-close') {
      closePreview();
    } else if (a === 'select-all' || a === 'select-none') {
      const busy = startBusy(a === 'select-all' ? 'Including all folders' : 'Excluding all folders');
      try {
        await api('/folders/all', { method: 'POST', body: { media: t, enabled: a === 'select-all' } });
        invalidate('dir:' + t);
        await refreshDash();
      } finally {
        endBusy(busy);
      }
      render();
    } else if (a === 'expand-all' || a === 'collapse-all') {
      walk(tree(t).roots, (n) => { n.open = a === 'expand-all'; });
      render();
    } else if (a === 'approve' || a === 'skip') {
      const path = a === 'approve' ? 'approve' : 'skip';
      await api('/parked/' + el.dataset.file + '/' + path, { method: 'POST' });
      state.parkSel = state.parkSel.filter((id) => String(id) !== el.dataset.file);
      invalidate('parked:' + t);
      await refreshDash();
      toast(a === 'approve' ? 'Queued: the change will be applied in the next run' : 'Skipped');
      render();
    } else if (a === 'approve-all') {
      const r = await api('/parked/approve-all', { method: 'POST', body: { media: t, reason: el.dataset.reason } });
      state.parkSel = [];
      invalidate('parked:' + t);
      await refreshDash();
      toast(r.done + ' file' + (r.done === 1 ? '' : 's') + ' queued');
      render();
    } else if (a === 'adjust-file') {
      await api('/parked/' + el.dataset.file + '/adjust', { method: 'POST', body: { delta: Number(el.dataset.step) } });
      invalidate('parked:' + t);
      render();
    } else if (a === 'bulk-adjust') {
      await api('/parked/bulk', { method: 'POST', body: { ids: state.parkSel, action: 'adjust', delta: Number(el.dataset.step) } });
      invalidate('parked:' + t);
      render();
    } else if (a === 'apply-selected') {
      const r = await api('/parked/bulk', { method: 'POST', body: { ids: state.parkSel, action: 'approve' } });
      state.parkSel = [];
      invalidate('parked:' + t);
      await refreshDash();
      toast(r.done + ' selected file' + (r.done === 1 ? '' : 's') + ' queued');
      render();
    } else if (a === 'clear-selection') {
      state.parkSel = [];
      render();
    } else if (a === 'preset') {
      const fn = PRESETS[el.dataset.preset];
      state.sched[t] = scheduleFrom(fn);
      await saveGrid(t);
      render();
    } else if (a === 'check-level') {
      const f = state.browse.rows[Number(el.dataset.idx)];
      if (!f) return;
      state.measure = { media: t, relpath: f.relpath, name: f.name, state: 'measuring' };
      const busy = startBusy('Checking the level of ' + f.name);
      const elapsed = tickBusy(busy);
      render();
      try {
        const r = await api('/files/measure', { method: 'POST', body: { media: t, relpath: f.relpath } });
        state.measure = { media: t, relpath: f.relpath, name: r.name, state: 'done', lufs: r.lufs, tp: r.true_peak };
      } catch (err) {
        state.measure = null;
        toast(err.message);
      } finally {
        elapsed.stop();
        endBusy(busy);
      }
      const node = tree(t) && tree(t).map.get(state.selected[t]);
      if (node) await loadBrowse(node.rel);
      else render();
    } else if (a === 'measure-close') {
      state.measure = null;
      render();
    } else if (a === 'use-baseline') {
      const m = state.measure;
      if (m && m.state === 'done') {
        const v = baselineValue(m.lufs);
        state.settings = await api('/settings', { method: 'PUT', body: { [m.media]: { target: v } } });
        state.draft = clone(state.settings);
        toast('Target loudness set to ' + v.toFixed(1) + ' LUFS');
        render();
      }
    } else if (a === 'baseline-start') {
      state.media = el.dataset.media;
      state.baselineFrom = el.dataset.media;
    } else if (a === 'baseline-done') {
      state.baselineFrom = null;
    } else if (a === 'settings-save') {
      const d = state.draft;
      const busy = startBusy('Saving the settings');
      render();
      try {
        state.settings = await api('/settings', { method: 'PUT', body: { general: d.general, audio: d.audio, video: d.video } });
        state.draft = clone(state.settings);
        toast('Settings saved');
        await refreshDash();
      } finally {
        endBusy(busy);
      }
      render();
    } else if (a === 'settings-reset') {
      state.draft[t] = clone(DEFAULTS[t]);
      render();
    } else if (a === 'ha-save') {
      const body = { url: state.haDraft.url };
      if (state.haDraft.token.trim()) body.token = state.haDraft.token.trim();
      const busy = startBusy('Saving the Home Assistant settings');
      render();
      try {
        state.ha = await api('/ha', { method: 'PUT', body: body });
        state.haDraft = { url: state.ha.url, token: '' };
        state.haTest = '';
        toast('Home Assistant settings saved');
      } finally {
        endBusy(busy);
      }
      render();
    } else if (a === 'ha-test') {
      const busy = startBusy('Testing the Home Assistant connection');
      const elapsed = tickBusy(busy);
      render();
      try {
        if (state.haDraft.token.trim()) {
          const body = { url: state.haDraft.url, token: state.haDraft.token.trim() };
          state.ha = await api('/ha', { method: 'PUT', body: body });
          state.haDraft = { url: state.ha.url, token: '' };
        }
        const r = await api('/ha/test', { method: 'POST' });
        state.haTest = r.message;
      } finally {
        elapsed.stop();
        endBusy(busy);
      }
      render();
    } else if (a === 'sensor-reset') {
      const sensor = state.ha.sensors.find((x) => x.path === el.dataset.path);
      if (sensor) {
        state.ha = await api('/ha', { method: 'PUT', body: { sensors: [{ path: sensor.path, name: sensor.default_name }] } });
        render();
      }
    } else if (a === 'ha-rotate') {
      if (!window.confirm('The current endpoint token stops working at once. Continue?')) return;
      const busy = startBusy('Creating a new endpoint token');
      render();
      try {
        state.ha = await api('/ha/rotate-token', { method: 'POST' });
        state.haTest = 'New endpoint token created. Paste the new configuration into Home Assistant.';
      } finally {
        endBusy(busy);
      }
      render();
    }
  }

  document.addEventListener('click', (e) => {
    const el = e.target.closest('[data-action]');
    if (!el || el.disabled) return;
    onAction(el).catch(SAVE_ERR);
  });

  /* input = while dragging; change = committed */
  async function onField(e) {
    const el = e.target;
    const committed = e.type === 'change';
    if (el.type === 'range') paintRange(el);
    if (e.type === 'pointerdown') return;

    if (el.dataset.check !== undefined) {
      if (!committed) return;
      const t = state.media;
      const busy = startBusy('Updating the folders');
      try {
        await api('/folders/toggle', { method: 'POST', body: { media: t, relpath: el.dataset.check, enabled: el.checked } });
        invalidate('dir:' + t);
        await refreshDash();
      } finally {
        endBusy(busy);
      }
      render();
      return;
    }

    if (el.dataset.parkSlider !== undefined) {
      const out = document.querySelector('[data-park-new="' + el.dataset.parkSlider + '"]');
      if (!committed) {
        if (out) out.textContent = fmtDb(tenth(Number(el.value)));
        return;
      }
      await api('/parked/' + el.dataset.parkSlider + '/adjust', { method: 'POST', body: { value: Number(el.value) } });
      state.dragging = false;
      invalidate('parked:' + state.media);
      render();
      return;
    }

    if (el.dataset.parkSort !== undefined) {
      if (!committed) return;
      state.parkSort = el.value;
      invalidate('parked:' + state.media);
      render();
      return;
    }

    if (el.dataset.parkCheck !== undefined || el.dataset.parkAll !== undefined) {
      if (!committed) return;
      const t = state.media;
      const list = state.parked[t].files;
      if (el.dataset.parkAll !== undefined) {
        state.parkSel = el.checked ? list.map((f) => f.id) : [];
      } else {
        const id = Number(el.dataset.parkCheck);
        state.parkSel = el.checked ? state.parkSel.concat(id) : state.parkSel.filter((x) => x !== id);
      }
      render();
      return;
    }

    if (el.dataset.runType) {
      if (!committed) return;
      await api('/run/types', { method: 'POST', body: { [el.dataset.runType]: el.checked } });
      await refreshDash();
      render();
      return;
    }

    if (el.dataset.setting) {
      if (el.type === 'radio' && !el.checked) return;
      const bucket = state.draft[el.dataset.scope];
      const key = el.dataset.setting;
      if (el.type === 'range') {
        const v = parseFloat(el.value);
        bucket[key] = v;
        const out = document.querySelector('[data-out="' + el.dataset.scope + '.' + key + '"]');
        if (out) out.textContent = v.toFixed(1);
        return;
      }
      bucket[key] = el.type === 'checkbox' ? el.checked : el.value;
      if (committed) render();
      return;
    }

    if (el.dataset.ha) {
      if (el.dataset.ha === 'url') state.haDraft.url = el.value;
      if (el.dataset.ha === 'token') state.haDraft.token = el.value;
      return;
    }

    if (el.dataset.sensorName !== undefined) {
      if (!committed) return;
      try {
        state.ha = await api('/ha', { method: 'PUT', body: { sensors: [{ path: el.dataset.sensorName, name: el.value }] } });
        toast('Sensor name saved');
      } catch (err) {
        toast(err.message);
      }
      render();
      return;
    }

    if (el.dataset.sensor !== undefined) {
      if (!committed) return;
      state.ha = await api('/ha', { method: 'PUT', body: { sensors: [{ path: el.dataset.sensor, enabled: el.checked }] } });
      refreshHaPreview();
      return;
    }

    if (el.dataset.sched) {
      if (el.type === 'radio' && !el.checked) return;
      if (!committed) return;
      const body = el.dataset.sched === 'scanEvery'
        ? { scanEvery: Math.min(1440, Math.max(1, Math.round(Number(el.value)) || 1)) }
        : { onClose: el.value };
      state.sched = await api('/schedule', { method: 'PUT', body: body });
      render();
    }
  }

  document.addEventListener('input', (e) => { onField(e).catch(SAVE_ERR); });
  document.addEventListener('change', (e) => { onField(e).catch(SAVE_ERR); });
  document.addEventListener('pointerdown', (e) => {
    if (e.target.matches && e.target.matches('input[type=range]')) state.dragging = true;
  });
  window.addEventListener('pointerup', () => { state.dragging = false; });

  /* schedule grid: click or drag to paint, saved when the pointer is released */
  let painting = null;
  function paintCell(cell) {
    const d = Number(cell.dataset.d), h = Number(cell.dataset.h);
    state.sched[state.media][d][h] = painting;
    cell.classList.toggle('on', painting);
  }
  document.getElementById('app').addEventListener('pointerdown', (e) => {
    const cell = e.target.closest('.cell');
    if (!cell) return;
    e.preventDefault();
    painting = !state.sched[state.media][Number(cell.dataset.d)][Number(cell.dataset.h)];
    paintCell(cell);
  });
  document.getElementById('app').addEventListener('pointerover', (e) => {
    if (painting === null) return;
    const cell = e.target.closest('.cell');
    if (cell) paintCell(cell);
  });
  window.addEventListener('pointerup', () => {
    if (painting === null) return;
    painting = null;
    const t = state.media;
    saveGrid(t).then(() => { updateScheduleInfo(); toast('Schedule saved'); }).catch(SAVE_ERR);
  });

  /* the info icon must not toggle the checkbox or radio of its label */
  document.addEventListener('click', (e) => {
    if (e.target.closest && e.target.closest('.tip')) e.preventDefault();
  });

  /* ------------------------------------------------------------------ menu */
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
    const route = currentRoute();
    if (route !== 'directories') state.baselineFrom = null;      // the baseline banner lasts while on Directories
    if (route !== 'settings' && state.settings) state.draft = clone(state.settings);   // unsaved edits are dropped
    if (route === 'parked') invalidate('parked:' + state.media);  // fresh list each time the page is opened
    if (route === 'directories') openDirectories();                // and look for new files and folders
    render();
  });

  // live status: the dashboard is refreshed every few seconds; parked rows only while no slider is being dragged
  setInterval(() => {
    refreshDash().catch(() => { /* offline: keep the last view */ });
    if (currentRoute() === 'parked' && !state.dragging && state.loaded['parked:' + state.media]) {
      const t = state.media;
      loadParkedData(t).then((d) => {
        if (JSON.stringify(d) !== JSON.stringify(state.parked[t]) && currentRoute() === 'parked' && !state.dragging) {
          state.parked[t] = d;
          render();
        }
      }).catch(() => { /* keep the last view */ });
    }
  }, 3000);

  applyMenu();
  initPlayer();
  initEditor();
  if (currentRoute() === 'directories') openDirectories();   // also when the app opens on this page
  render();
})();
