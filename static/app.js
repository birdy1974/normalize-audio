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
  const MODE_LABEL = { off: 'Off', bulk: 'Bulk', force: 'Force', auto: 'Automatic' };
  const MODE_SHORT = {
    off: 'nothing is scanned or normalized',
    bulk: 'a run normalizes the files that are here',
    force: 'a run checks and normalizes all enabled files again',
    auto: 'new files are normalized by themselves',
  };
  const PARK_SORTS = {
    order: 'File order',
    change_asc: 'Change needed, lowest first',
    change_desc: 'Change needed, highest first',
    size_desc: 'Size of change, largest first',
  };
  const DEFAULTS = {
    general: {
      dryRun: false, preserveMtime: true, mode: 'bulk', theme: 'light',
      trimSilence: false, silenceLevelPct: 2.0, silencePeakDb: -40.0,
      silenceMinDuration: 5.0, silenceFadeOut: 0.5, silenceFadeIn: 0.5,
      coverAuto: false, coverSources: 'both', discogsToken: '', useLocalCover: true,
    },
    audio: { target: -16, truePeak: -1, hysteresis: 0.5, threshold: 6, override: false, lossy: 'gain', writeMode: 'inplace', outputDir: '/data/normalized-audio' },
    video: { target: -16, truePeak: -1, hysteresis: 0.5, threshold: 6, override: false, writeMode: 'inplace', outputDir: '/data/normalized-video' },
  };

  /* ----------------------------------------------------------------- theme */
  const THEME_KEY = 'audio-normalizer-theme';
  const VALID_THEMES = ['light', 'dark', 'system'];
  let themeHydratedFromLocal = false;
  let themePref = (() => {
    try {
      const raw = localStorage.getItem(THEME_KEY);
      if (raw && VALID_THEMES.includes(raw)) {
        themeHydratedFromLocal = true;
        return raw;
      }
    } catch (_e) { /* private mode */ }
    return 'light';
  })();

  function effectiveTheme(pref) {
    const p = pref || themePref || 'light';
    if (p === 'dark') return 'dark';
    if (p === 'system') {
      return (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light';
    }
    return 'light';
  }

  function syncThemeControls() {
    const resolved = effectiveTheme(themePref);
    const toggleBtn = document.getElementById('theme-toggle');
    if (toggleBtn) {
      const nextLbl = resolved === 'dark' ? 'Switch to light mode' : 'Switch to dark mode';
      toggleBtn.setAttribute('aria-label', nextLbl);
      toggleBtn.title = nextLbl;
      toggleBtn.setAttribute('aria-pressed', String(resolved === 'dark'));
    }
    document.querySelectorAll('#sidebar-theme [data-theme]').forEach((btn) => {
      const on = btn.dataset.theme === themePref;
      btn.classList.toggle('on', on);
      btn.setAttribute('aria-pressed', String(on));
    });
  }

  function applyTheme(pref, opts) {
    const o = opts || {};
    const next = VALID_THEMES.includes(pref) ? pref : 'light';
    themePref = next;
    const resolved = effectiveTheme(next);
    document.documentElement.setAttribute('data-theme', resolved);
    document.documentElement.setAttribute('data-theme-pref', next);
    const metaTheme = document.getElementById('meta-theme-color');
    if (metaTheme) metaTheme.setAttribute('content', resolved === 'dark' ? '#141615' : '#ebebe9');
    if (o.saveLocal !== false) {
      themeHydratedFromLocal = true;
      try { localStorage.setItem(THEME_KEY, next); } catch (_e) { /* private mode */ }
    }
    if (state && state.settings && state.settings.general) state.settings.general.theme = next;
    if (state && state.draft && state.draft.general) state.draft.general.theme = next;
    if (state && state.dash) state.dash.theme = next;
    syncThemeControls();
    if (typeof EDIT !== 'undefined' && EDIT.open && typeof drawEditor === 'function') {
      drawEditor();
    }
    if (o.saveServer) {
      api('/settings', { method: 'PUT', body: { general: { theme: next } } }).catch(() => {});
    }
  }

  if (window.matchMedia) {
    const darkMq = window.matchMedia('(prefers-color-scheme: dark)');
    const onMqChange = () => {
      if (themePref === 'system') applyTheme('system', { saveLocal: false, saveServer: false });
    };
    if (darkMq.addEventListener) darkMq.addEventListener('change', onMqChange);
    else if (darkMq.addListener) darkMq.addListener(onMqChange);
  }
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
  let fastBusyTimer = null;
  let fastBusyInFlight = false;
  function ensureFastBusyPoll() {
    const d = state.dash;
    const active = Object.keys(state.busy).length > 0
      || state.metaChecking
      || state.scanning.audio || state.scanning.video
      || !!(d && (
        d.status === 'running'
        || d.scanning
        || d.meta_checking
        || d.approved_pending > 0
        || (d.action_progress && d.action_progress.active)
      ));
    if (active && !fastBusyTimer) {
      fastBusyTimer = setInterval(() => {
        if (fastBusyInFlight) return;
        fastBusyInFlight = true;
        refreshDash().catch(() => {}).finally(() => { fastBusyInFlight = false; });
      }, 600);
    } else if (!active && fastBusyTimer) {
      clearInterval(fastBusyTimer);
      fastBusyTimer = null;
    }
  }

  function startBusy(text, totalHint) {
    const id = 'b' + (++busySeq);
    const tot = Math.max(1, Number(totalHint) || 1);
    state.busy[id] = { text, secs: null, startedAt: Date.now(), done: 0, total: tot };
    showActivity();
    ensureFastBusyPoll();
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

  function endBusy(id, minMs = 0) {
    if (state.busy[id] === undefined) return;
    state.busy[id].done = state.busy[id].total;
    showActivity();
    if (minMs > 0 && state.busy[id].startedAt) {
      const remain = minMs - (Date.now() - state.busy[id].startedAt);
      if (remain > 0) {
        setTimeout(() => endBusy(id, 0), remain);
        return;
      }
    }
    delete state.busy[id];
    showActivity();
    ensureFastBusyPoll();
  }

  function serverWork() {
    const d = state.dash;
    if (!d) return [];
    const out = [];
    const auto = d.mode === 'auto';
    const ap = d.action_progress || {};
    if (d.approved_pending > 0) {
      const fileSub = d.current_file ? folderTail(d.current_file) : 'applying parked file';
      const sub = d.approved_pending > 1 ? fileSub + ' · ' + d.approved_pending + ' left' : fileSub;
      const tot = Math.max(1, Number(d.approved_total || d.approved_pending) || 1);
      const dn = Math.max(0, Math.min(tot, Number(d.approved_done || 0)));
      out.push({ busy: true, text: 'Applying parked changes', sub: sub, done: dn, total: tot });
    }
    if (d.meta_checking) {
      const mp = d.meta_progress || (state.metadata && state.metadata.meta_progress) || {};
      const tot = Math.max(1, Number(mp.total || 0) || 1);
      const dn = Math.max(0, Math.min(tot, Number(mp.done || 0)));
      const progPart = mp.total > 0 ? dn + ' / ' + tot + ' files' : 'preparing files';
      const curPart = mp.current_file ? ' · ' + folderTail(mp.current_file) : '';
      out.push({ busy: true, text: 'Checking metadata', sub: progPart + curPart, done: dn, total: tot });
    }
    if (ap && ap.active) {
      const tot = Math.max(1, Number(ap.total || 1));
      const dn = Math.max(0, Math.min(tot, Number(ap.done || 0)));
      out.push({
        busy: true,
        text: ap.label || 'Working',
        sub: ap.current_file ? folderTail(ap.current_file) : (dn + ' / ' + tot),
        done: dn,
        total: tot,
      });
    }
    if (d.status === 'running') {
      const tot = Math.max(1, Number(d.progress_total || 1));
      const dn = Math.max(0, Math.min(tot, Number(d.progress_done || 0)));
      out.push({
        busy: true,
        text: 'Normalizing',
        sub: d.current_file ? folderTail(d.current_file) : 'waiting for the next file',
        done: dn,
        total: tot,
      });
    } else if (d.status === 'paused') {
      out.push({ busy: false, text: 'Paused', sub: auto ? 'automatic mode is paused' : 'the run is paused' });
    } else if (d.status === 'waiting_window') {
      const tot = Math.max(1, Number(d.progress_total || 1));
      const dn = Math.max(0, Math.min(tot, Number(d.progress_done || 0)));
      out.push({ busy: true, text: 'Waiting for schedule', sub: 'the run continues in the next window', done: dn, total: tot });
    } else if (d.scanning) {
      const tot = ap && ap.total ? Math.max(1, Number(ap.total)) : 1;
      const dn = ap && ap.total ? Math.max(0, Math.min(tot, Number(ap.done || 0))) : 0;
      out.push({ busy: true, text: 'Scanning folders', sub: 'looking for new, changed and removed files', done: dn, total: tot });
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
    const d = state.dash;
    const ap = d && d.action_progress && d.action_progress.active ? d.action_progress : null;
    if (ids.length) {
      const first = state.busy[ids[0]];
      const more = ids.length > 1 ? '+' + (ids.length - 1) + ' more in this browser' : '';
      const tot = ap ? Math.max(1, Number(ap.total || first.total || 1)) : Math.max(1, Number(first.total || 1));
      const dn = ap ? Math.max(0, Math.min(tot, Number(ap.done || 0))) : Math.max(0, Math.min(tot, Number(first.done || 0)));
      const pct = Math.min(100, Math.max(0, Math.round((dn / tot) * 100)));
      const curFile = ap && ap.current_file ? folderTail(ap.current_file) : '';
      return {
        busy: true,
        title: first.text,
        sub: [curFile, first.secs === null ? '' : first.secs + ' s', more].filter(Boolean).join(' · '),
        done: dn,
        total: tot,
        percent: pct,
      };
    }
    if (server.length) {
      const s0 = server[0];
      const tot = Math.max(1, Number(s0.total || 1));
      const dn = Math.max(0, Math.min(tot, Number(s0.done || 0)));
      const pct = Math.min(100, Math.max(0, Math.round((dn / tot) * 100)));
      return { busy: s0.busy, title: s0.text, sub: s0.sub, done: dn, total: tot, percent: pct };
    }
    if (!state.dash) return { busy: true, title: 'Loading', sub: 'reading the status', done: 0, total: 1, percent: 0 };
    return { busy: false, title: 'Idle', sub: 'nothing to do right now', done: 0, total: 0, percent: 0 };
  }

  let stoppingAction = false;
  const activeApiControllers = new Set();

  function isActionStoppable() {
    if (stoppingAction) return false;
    if (Object.keys(state.busy).length > 0) return true;
    if (state.metaChecking || state.scanning.audio || state.scanning.video) return true;
    const d = state.dash;
    if (!d) return false;
    if (d.approved_pending > 0 || d.meta_checking || d.scanning) return true;
    if (d.action_progress && d.action_progress.active) return true;
    if (d.status === 'running' || d.status === 'waiting_window') return true;
    if (d.status === 'paused' && d.mode !== 'auto') return true;
    return false;
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
    box.title = now.busy
      ? now.title + ' · ' + fmt(now.done) + ' / ' + fmt(now.total) + ' (' + now.percent + '%)' + (now.sub ? ' · ' + now.sub : '')
      : now.title + (now.sub ? ' · ' + now.sub : '');

    const progWrap = document.getElementById('activity-progress-wrap');
    const progCount = document.getElementById('activity-progress-count');
    const progPct = document.getElementById('activity-progress-pct');
    const progBar = document.getElementById('activity-progress-bar');
    const progFill = document.getElementById('activity-progress-fill');
    if (progWrap) {
      progWrap.hidden = !now.busy;
      if (now.busy) {
        const countTxt = fmt(now.done) + ' / ' + fmt(now.total);
        const pctTxt = now.percent + '%';
        if (progCount && progCount.textContent !== countTxt) progCount.textContent = countTxt;
        if (progPct && progPct.textContent !== pctTxt) progPct.textContent = pctTxt;
        if (progBar) progBar.setAttribute('aria-valuenow', String(now.percent));
        if (progFill) {
          const fillW = (now.done > 0 ? Math.max(3, now.percent) : Math.max(0, now.percent)) + '%';
          if (progFill.style.width !== fillW) progFill.style.width = fillW;
        }
      }
    }

    const stopBtn = document.getElementById('activity-stop');
    if (stopBtn) {
      const canStop = isActionStoppable();
      stopBtn.disabled = !canStop;
      stopBtn.classList.toggle('is-active', canStop);
      const lblEl = stopBtn.querySelector('.activity-stop-label');
      if (lblEl) {
        const nextLbl = stoppingAction ? 'Stopping…' : 'Stop';
        if (lblEl.textContent !== nextLbl) lblEl.textContent = nextLbl;
      }
      stopBtn.title = stoppingAction
        ? 'Stopping current action…'
        : (canStop ? 'Stop / kill current action (' + now.title + ')' : 'Nothing running right now');
    }
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
  const EDIT_COLORS_LIGHT = {
    bg: '#fbfbfa', grid: '#d9d9d5', gridText: '#9d9e99', text: '#6c6d69',
    wave: '#2f7a6b', waveDim: '#8fc0b5',
    sel: 'rgba(47,122,107,.20)', cut: 'rgba(160,63,54,.20)', cutLine: '#a03f36',
    fade: 'rgba(47,122,107,.14)', playhead: '#2f3130',
  };
  const EDIT_COLORS_DARK = {
    bg: '#181b1a', grid: '#2e3331', gridText: '#6c736e', text: '#9ca39e',
    wave: '#3ea38e', waveDim: '#266155',
    sel: 'rgba(62,163,142,.24)', cut: 'rgba(224,108,98,.22)', cutLine: '#e06c62',
    fade: 'rgba(62,163,142,.18)', playhead: '#eceee9',
  };
  const editColors = () => (effectiveTheme() === 'dark' ? EDIT_COLORS_DARK : EDIT_COLORS_LIGHT);

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
    const EDIT_COLORS = editColors();
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
    if (!COVER.open && !META_MODAL.open) document.body.classList.remove('has-modal');
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
    const method = (opts.method || 'GET').toUpperCase();
    const init = { method: method, headers: {} };
    const trackAbort = path !== '/actions/stop' && path !== '/metadata/stop' && path !== '/run/stop'
      && (method !== 'GET' || path.startsWith('/files/waveform') || path.startsWith('/files/info'));
    let ctrl = null;
    if (trackAbort) {
      ctrl = new AbortController();
      activeApiControllers.add(ctrl);
      if (opts.signal) {
        if (opts.signal.aborted) ctrl.abort();
        else opts.signal.addEventListener('abort', () => ctrl.abort(), { once: true });
      }
      init.signal = ctrl.signal;
    } else if (opts.signal) {
      init.signal = opts.signal;
    }
    if (opts.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
    try {
      const res = await fetch('/api' + path, init);
      let data = null;
      try { data = await res.json(); } catch (err) { data = null; }
      if (res.status === 499) {
        throw Object.assign(new Error('Action stopped'), { name: 'AbortError', stopped: true });
      }
      if (!res.ok) {
        const d = data && data.detail;
        let msg = 'Request failed (HTTP ' + res.status + ')';
        if (typeof d === 'string') msg = d;
        else if (Array.isArray(d)) msg = d.map((x) => x.msg).join('; ');
        throw new Error(msg);
      }
      return data;
    } finally {
      if (ctrl) activeApiControllers.delete(ctrl);
    }
  }

  const META_VIS_KEY = 'audio-normalizer-meta-vis-fields';
  const META_COLS_KEY = 'audio-normalizer-meta-cols-v3';
  const DEFAULT_META_COLS = {
    pick: 38,
    file: 270,
    custom: 255,
    field: 118,
    path: 190,
    discogs: 205,
    musicbrainz: 220,
    open: 220,
  };
  const ALL_META_FIELD_KEYS = ['artist', 'title', 'album', 'album_artist', 'year', 'track', 'genre', 'cover'];
  let metaCheckAbort = null;

  function loadStoredMetaVis() {
    try {
      const raw = JSON.parse(localStorage.getItem(META_VIS_KEY));
      if (Array.isArray(raw) && raw.length) {
        const clean = raw.filter((k) => ALL_META_FIELD_KEYS.includes(k));
        if (clean.length) return clean;
      }
    } catch (_err) { /* ignore */ }
    return ALL_META_FIELD_KEYS.slice();
  }

  function saveStoredMetaVis(list) {
    try { localStorage.setItem(META_VIS_KEY, JSON.stringify(list)); } catch (_err) { /* ignore */ }
  }

  function loadStoredMetaCols() {
    try {
      const raw = JSON.parse(localStorage.getItem(META_COLS_KEY));
      if (raw && typeof raw === 'object') {
        const out = Object.assign({}, DEFAULT_META_COLS);
        Object.keys(DEFAULT_META_COLS).forEach((k) => {
          const n = Number(raw[k]);
          if (isFinite(n) && n >= 32 && n <= 1200) out[k] = Math.round(n);
        });
        return out;
      }
    } catch (_err) { /* ignore */ }
    return Object.assign({}, DEFAULT_META_COLS);
  }

  function saveStoredMetaCols(cols) {
    try { localStorage.setItem(META_COLS_KEY, JSON.stringify(cols)); } catch (_err) { /* ignore */ }
  }

  let metaUiSaveTimer = null;
  function persistMetaUiPrefs() {
    clearTimeout(metaUiSaveTimer);
    metaUiSaveTimer = setTimeout(() => {
      api('/metadata/options', {
        method: 'PUT',
        body: {
          visible_fields: state.metaVisibleFields,
          only_diff_rows: !!state.metaOnlyDiffRows,
          col_widths: state.metaColWidths,
        },
      }).catch(() => { /* best-effort DB sync */ });
    }, 220);
  }

  /* ---------------------------------------------------------------- state */
  const state = {
    media: 'audio',                 // media tab shown on media-specific pages
    dash: null,                     // GET /api/dashboard
    dashKey: '',
    dashStructKey: '',
    lastFsSeq: null,
    settings: null,                 // saved settings
    draft: null,                    // settings being edited on the Settings page
    sched: null,                    // GET /api/schedule
    ha: null,                       // GET /api/ha
    haDraft: { url: '', token: '' },
    haTest: '',
    trees: { audio: null, video: null },   // { roots, map, rootFiles } built from /api/folders/tree
    selected: { audio: null, video: null },// selected folder (relpath) per media type
    treeScroll: { audio: { top: 0, left: 0 }, video: { top: 0, left: 0 } },
    filesScroll: { audio: { rel: null, top: 0, left: 0 }, video: { rel: null, top: 0, left: 0 } },
    browse: { media: null, rel: null, rows: null, loading: false },
    fileSel: [],                    // selected file ids in the Directories files pane
    scanning: { audio: false, video: false },  // a rescan of the base location is running
    scanMode: { audio: null, video: null },    // 'quick' | 'full' | 'folder' | null
    scanTarget: { audio: null, video: null },  // targeted folder relpath when scanMode === 'folder'
    busy: {},                       // what this browser is doing now: id -> { text, secs }
    parked: { audio: null, video: null },  // GET /api/parked
    coverParked: null,              // GET /api/covers/parked (separate cover art pane)
    coverParkMode: 'album',         // 'album' | 'file' in the cover art parked pane
    coverBust: Date.now(),          // cache-buster bumped after any cover or metadata write
    discogsTest: '',
    metadata: null,                 // GET /api/metadata
    metaChecking: false,            // true while a metadata check run is active
    metaSel: [],                    // selected file ids on the Meta data tab
    metaFilterStatus: 'diff',       // 'diff' | 'path_diff' | 'missing' | 'all'
    metaFilterField: 'all',         // 'all' or field key
    metaFilterFolder: 'all',        // 'all' or folder relpath
    metaSearch: '',                 // search query on the Meta data tab
    metaSort: 'folder',             // 'folder' | 'name' | 'diffs_desc' | 'path_first'
    metaVisibleFields: loadStoredMetaVis(), // which parameters are shown directly in Pane 2
    metaForceShowFields: [],        // bulk-edited parameters guaranteed visible even when Only diff rows is on
    metaOnlyDiffRows: false,        // when true, hide non-differing parameter rows
    metaColWidths: loadStoredMetaCols(),    // resizable column widths (px) for Pane 2 table
    metaPrefsHydrated: false,       // hydrated once from persistent SQLite metadata_options
    metaTableScroll: { top: 0, left: 0 },   // preserved horizontal/vertical scroll during live checks
    metaBulkOpen: false,            // whether the Bulk specific-parameter editor is expanded
    metaBulkFields: {},             // { [fieldKey]: { enabled: bool, mode: string, value: string, dirty: bool } }
    metaEditing: {},                // { "fileId:field": true } for inline custom input
    metaTouchedFiles: new Set(),    // file ids edited in this view so they never vanish or jump mid-selection
    parkSel: [],                    // selected parked file ids
    parkSort: 'order',
    measure: null,                  // last file level check (baseline)
    baselineFrom: null,             // media type when opened from Settings > Level
    loaded: {}, pending: {}, errors: {},
    dragging: false,                // a slider is being dragged: do not re-render parked rows
  };

  function bumpCoverCache() {
    state.coverBust = Date.now();
  }

  function fileCoverUrl(fileId, versionHint) {
    const v = String(versionHint || 0) + '-' + String(state.coverBust);
    return '/api/files/' + fileId + '/cover?v=' + encodeURIComponent(v);
  }

  function candThumbUrl(fileId, rawUrl, versionHint) {
    const u = String(rawUrl || '');
    if (!u) return '';
    if (u.startsWith('local://')) {
      const v = String(versionHint || 0) + '-' + String(state.coverBust);
      return '/api/files/' + fileId + '/local-cover?v=' + encodeURIComponent(v);
    }
    return u;
  }

  /* ----------------------------------------------------------- data loading */
  function keysFor(route) {
    const t = state.media;
    switch (route) {
      case 'dashboard': return ['dashboard', 'settings'];
      case 'directories': return ['dir:' + t, 'settings', 'dashboard'];   // folder counts come from the dashboard
      case 'parked': return t === 'audio' ? ['parked:' + t, 'coverParked', 'settings'] : ['parked:' + t, 'settings'];
      case 'metadata': return ['metadata', 'dashboard', 'settings'];
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
      if (!themeHydratedFromLocal && state.dash && state.dash.theme) {
        applyTheme(state.dash.theme, { saveLocal: false, saveServer: false });
      }
    } else if (key === 'settings') {
      state.settings = await api('/settings');
      if (themeHydratedFromLocal && state.settings && state.settings.general) {
        state.settings.general.theme = themePref;
      } else if (state.settings && state.settings.general && state.settings.general.theme) {
        applyTheme(state.settings.general.theme, { saveLocal: false, saveServer: false });
      }
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
      if (!tree.roots.length && !data.root_files && !state.scanning[t] && (!state.dash || !state.dash.last_scan)) {
        scanDirectories(t, false);
      }
    } else if (parts[0] === 'parked') {
      state.parked[parts[1]] = await loadParkedData(parts[1]);
    } else if (key === 'coverParked') {
      state.coverParked = await api('/covers/parked');
    } else if (key === 'metadata') {
      const snapSeq = metaMutationSeq;
      const incoming = await api('/metadata');
      if (snapSeq !== metaMutationSeq && state.metadata && Array.isArray(state.metadata.files) && Array.isArray(incoming.files)) {
        const localById = new Map(state.metadata.files.map((x) => [x.id, x]));
        incoming.files = incoming.files.map((f) => {
          if (state.metaTouchedFiles.has(f.id) && localById.has(f.id)) {
            return localById.get(f.id);
          }
          return f;
        });
      }
      state.metadata = incoming;
      const opts = (state.metadata && state.metadata.options) || {};
      if (!state.metaPrefsHydrated) {
        state.metaPrefsHydrated = true;
        if (Array.isArray(opts.visible_fields) && opts.visible_fields.length) {
          state.metaVisibleFields = opts.visible_fields.filter((k) => ALL_META_FIELD_KEYS.includes(k));
          saveStoredMetaVis(state.metaVisibleFields);
        }
        if (typeof opts.only_diff_rows === 'boolean') {
          state.metaOnlyDiffRows = opts.only_diff_rows;
        }
        if (opts.col_widths && typeof opts.col_widths === 'object' && Object.keys(opts.col_widths).length) {
          const hydrated = Object.assign({}, DEFAULT_META_COLS);
          const isLegacy9Col = Object.prototype.hasOwnProperty.call(opts.col_widths, 'current');
          Object.keys(DEFAULT_META_COLS).forEach((k) => {
            const n = Number(opts.col_widths[k]);
            if (isFinite(n) && n >= 32 && n <= 1200) hydrated[k] = Math.round(n);
          });
          if (isLegacy9Col) {
            hydrated.file = Math.max(hydrated.file, DEFAULT_META_COLS.file);
            hydrated.custom = Math.max(hydrated.custom, DEFAULT_META_COLS.custom);
          }
          state.metaColWidths = hydrated;
          saveStoredMetaCols(state.metaColWidths);
          if (isLegacy9Col) persistMetaUiPrefs();
        }
      }
      if (state.metadata && state.metadata.meta_checking) {
        state.metaChecking = true;
        watchMetaCheck();
      }
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

  function dashStructureSignature(d) {
    if (!d) return '';
    return JSON.stringify({
      status: d.status,
      mode: d.mode,
      run_mode: d.run_mode,
      paused: d.paused,
      types: d.types,
      last_finished: d.last_finished,
      last_scan: d.last_scan,
      fs_seq: d.fs_seq,
      watcher_active: d.watcher_active,
      approved_pending: d.approved_pending,
      dry_run: d.dry_run,
      media: d.media,
    });
  }

  let dashSeq = 0;
  async function refreshDash(opts) {
    const o = opts || {};
    const seq = ++dashSeq;
    const d = await api('/dashboard');
    if (seq < dashSeq) return;
    const prevFsSeq = state.lastFsSeq;
    const nextFsSeq = typeof d.fs_seq === 'number' ? d.fs_seq : null;
    state.lastFsSeq = nextFsSeq;
    const fsChanged = prevFsSeq !== null && nextFsSeq !== null && nextFsSeq !== prevFsSeq;
    const key = JSON.stringify(d);
    const structKey = dashStructureSignature(d);
    const structChanged = structKey !== state.dashStructKey;
    if (key !== state.dashKey) {
      state.dash = d;
      state.dashKey = key;
      state.dashStructKey = structKey;
      state.loaded.dashboard = true;
      const route = currentRoute();
      if (route === 'dashboard') {
        state.browse.stale = true;
        render();
      } else if (route === 'directories') {
        const t = state.media;
        const scanActive = !!(d.scanning || state.scanning[t]);
        if (!o.skipDirReload && !scanActive && (fsChanged || structChanged)) {
          state.browse.stale = true;
          invalidate('dir:' + t);
          const selRel = state.selected[t];
          await Promise.all([
            loadKey('dir:' + t),
            selRel ? loadBrowse(selRel, true) : Promise.resolve(),
          ]);
          render();
        } else if (!scanActive && structChanged) {
          render();
        }
      } else {
        if (structChanged) state.browse.stale = true;
        updateNavBadge();
      }
    }
    if (d.status === 'running' || d.scanning || d.approved_pending > 0) {
      watchApprovedWork();
    }
    if (d.meta_checking) {
      state.metaChecking = true;
      watchMetaCheck();
    }
    ensureFastBusyPoll();
    showActivity();                 // the run state lives in the dashboard data
  }

  let metaMutationSeq = 0;
  let metaPendingMutations = 0;
  let metaSelectChain = Promise.resolve();

  function applyServerMetaUpdates(filesUpdated) {
    if (!filesUpdated || !state.metadata || !Array.isArray(state.metadata.files)) return;
    let diffCount = 0;
    let pathDiffCount = 0;
    state.metadata.files.forEach((f) => {
      const u = filesUpdated[String(f.id)];
      if (u) {
        f.status = u.status;
        f.diffs = u.diffs;
        f.diff_count = u.diff_count;
        f.path_diff = u.path_diff;
        f.path_diff_count = u.path_diff_count;
        f.path_mismatches = u.path_mismatches;
        f.selected = u.selected;
        f.fields = u.fields;
      }
      if (f.status === 'diff' || f.diff_count > 0) diffCount += 1;
      if (f.path_diff || f.path_diff_count > 0) pathDiffCount += 1;
    });
    state.metadata.diff_count = diffCount;
    state.metadata.path_diff_count = pathDiffCount;
    if (state.metadata.counts) {
      state.metadata.counts.diff = diffCount;
      state.metadata.counts.path_diff = pathDiffCount;
    }
    updateNavBadge();
  }

  function queueMetaSelect(body, opts) {
    const o = opts || {};
    const mySeq = ++metaMutationSeq;
    metaPendingMutations += 1;
    if (Array.isArray(body.ids)) {
      body.ids.forEach((id) => state.metaTouchedFiles.add(Number(id)));
    }
    const run = metaSelectChain.then(async () => {
      const res = await api('/metadata/select', { method: 'POST', body: body });
      if (res && res.files_updated && mySeq === metaMutationSeq) {
        applyServerMetaUpdates(res.files_updated);
        if (!o.skipRender && currentRoute() === 'metadata') {
          renderPreservingMetaScroll();
        }
      }
      return res;
    });
    metaSelectChain = run.catch(() => {});
    return run.finally(() => {
      metaPendingMutations = Math.max(0, metaPendingMutations - 1);
    });
  }

  function currentSearchFrom() {
    if (state.metadata && state.metadata.options) {
      const v = state.metadata.options.search_from || state.metadata.options.searchFrom;
      if (v) return v;
    }
    if (state.dash && state.dash.search_from) return state.dash.search_from;
    return 'auto';
  }

  let metaWatchTimer = null;
  let metaWatchInFlight = false;
  let lastMetaRenderSig = '';
  let metaDeferredRender = false;

  function isAppControlFocused() {
    const ae = document.activeElement;
    if (!ae || ae === document.body) return false;
    const appEl = document.getElementById('app');
    if (!appEl || !appEl.contains(ae)) return false;
    return /^(INPUT|SELECT|TEXTAREA)$/.test(ae.tagName) && ae.type !== 'checkbox' && ae.type !== 'radio';
  }

  function patchLiveMetaProgressDom(mp) {
    const txtEl = document.getElementById('meta-live-progress-text');
    const fillEl = document.getElementById('meta-live-progress-fill');
    if (!txtEl && !fillEl) return;
    const done = Number(mp.done || 0);
    const total = Number(mp.total || 0);
    const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
    if (txtEl) {
      txtEl.innerHTML = '<span class="working" aria-hidden="true"></span> '
        + '<strong>' + (mp.force ? 'Re-checking all metadata' : 'Checking metadata') + ':</strong> '
        + (total > 0 ? fmt(done) + ' / ' + fmt(total) + ' files (' + pct + '%)' : 'starting…')
        + (done > 0 ? ' · <strong>' + fmt(mp.diff || 0) + '</strong> with differences · ' + fmt(mp.ok || 0) + ' matching' : '')
        + (mp.current_file ? ' · <span class="muted">Current: ' + esc(mp.current_file) + '</span>' : '');
    }
    if (fillEl) {
      fillEl.style.width = (total > 0 ? Math.max(2, pct) : 8) + '%';
    }
  }

  function renderPreservingMetaScroll() {
    const winY = window.scrollY;
    const winX = window.scrollX;
    const tw = document.getElementById('meta-table-wrap');
    if (tw) {
      state.metaTableScroll = { top: tw.scrollTop, left: tw.scrollLeft };
    }
    render();
    window.scrollTo(winX, winY);
  }

  function watchMetaCheck() {
    if (metaWatchTimer) return;
    metaWatchTimer = setInterval(async () => {
      if (metaWatchInFlight) return;
      metaWatchInFlight = true;
      try {
        const snapSeq = metaMutationSeq;
        const data = await api('/metadata');
        if ((snapSeq !== metaMutationSeq || metaPendingMutations > 0)
            && state.metadata && Array.isArray(state.metadata.files) && Array.isArray(data.files)) {
          const localById = new Map(state.metadata.files.map((x) => [x.id, x]));
          data.files = data.files.map((f) => (state.metaTouchedFiles.has(f.id) && localById.has(f.id) ? localById.get(f.id) : f));
        }
        state.metadata = data;
        state.loaded.metadata = true;
        const mp = data.meta_progress || {};
        const stillRunning = !!(data.meta_checking || mp.running);
        if (state.dash) {
          state.dash.meta_checking = stillRunning;
          state.dash.meta_progress = mp;
        }
        showActivity();
        updateNavBadge();

        const sig = [stillRunning ? 1 : 0, mp.seq || 0, data.checked_count || 0, data.diff_count || 0].join(':');
        if (currentRoute() === 'metadata') {
          if (sig !== lastMetaRenderSig || metaDeferredRender) {
            if ((isAppControlFocused() || colResizeState) && stillRunning) {
              patchLiveMetaProgressDom(mp);
              metaDeferredRender = true;
            } else {
              lastMetaRenderSig = sig;
              metaDeferredRender = false;
              renderPreservingMetaScroll();
            }
          } else if (stillRunning) {
            patchLiveMetaProgressDom(mp);
          }
        }

        if (!stillRunning) {
          clearInterval(metaWatchTimer);
          metaWatchTimer = null;
          state.metaChecking = false;
          metaDeferredRender = false;
          await refreshDash().catch(() => {});
          if (currentRoute() === 'directories' && state.browse && state.browse.rel) {
            await loadBrowse(state.browse.rel, true).catch(() => {});
          }
          if (currentRoute() === 'metadata') {
            renderPreservingMetaScroll();
          } else {
            render();
          }
          if (mp.stopped) {
            toast('Metadata check stopped (' + fmt(mp.done || 0) + ' file' + ((mp.done || 0) === 1 ? '' : 's') + ' checked · ' + fmt(mp.diff || 0) + ' with differences)');
          } else if ((mp.total || 0) > 0 || (mp.done || 0) > 0) {
            toast('Metadata check complete (' + fmt(mp.done || 0) + ' file' + ((mp.done || 0) === 1 ? '' : 's') + '): ' + fmt(mp.diff || 0) + ' with differences · ' + fmt(mp.ok || 0) + ' matching');
          }
        }
      } catch (_err) {
        /* transient network hiccup: keep polling */
      } finally {
        metaWatchInFlight = false;
      }
    }, 1100);
  }

  let browseSeq = 0;
  async function loadBrowse(rel, quiet) {
    const t = state.media;
    const seq = ++browseSeq;
    const sameFolder = state.browse.media === t && state.browse.rel === rel && Array.isArray(state.browse.rows);
    state.browse = {
      media: t,
      rel: rel,
      rows: quiet && sameFolder ? state.browse.rows : null,
      loading: !(quiet && sameFolder),
      stale: false,
    };
    try {
      const d = await api('/files?media=' + t + '&dir=' + encodeURIComponent(rel));
      if (seq === browseSeq && (state.selected[t] === rel || state.browse.rel === rel) && state.media === t) {
        state.browse = { media: t, rel: rel, rows: d.files, loading: false, stale: false };
      }
    } catch (err) {
      if (seq === browseSeq && state.browse.rel === rel) {
        state.browse = { media: t, rel: rel, rows: state.browse.rows || [], loading: false, stale: false };
      }
      if (!quiet) toast(err.message);
    }
    if (currentRoute() === 'directories') render();
  }

  /* Look for new, changed and removed files and folders on disk. One scan per media type at a
     time; the tree and the open folder are read again when it finishes. */
  const scans = { audio: null, video: null };     // in-flight rescan per media type

  function scanSummary(t, r, opts) {
    const o = opts || {};
    const prefix = o.dir ? ('Rescan (' + o.dir + ')') : (o.full ? 'Full rescan' : 'Rescan');
    if (!r) return prefix + ' finished';
    if (r.available === false) {
      return MEDIA[t].label + ' base location ' + MEDIA[t].root + ' is not available. Check the mount in docker-compose.yml.';
    }
    const found = [];
    if (r.new) found.push(r.new + ' new');
    if (r.removed) found.push(r.removed + ' removed');
    const n = r.new + r.removed;
    const files = n ? found.join(', ') + ' file' + (n === 1 ? '' : 's') : 'no new or removed files';
    return prefix + ': ' + files + ' · ' + fmt(r.folders) + ' folders';
  }

  function scanDirectories(t, notify, opts) {
    if (scans[t]) return scans[t];
    const o = opts || {};
    const full = !!o.full;
    const targetDir = o.dir ? String(o.dir) : null;
    state.scanning[t] = true;
    state.scanMode[t] = targetDir ? 'folder' : (full ? 'full' : 'quick');
    state.scanTarget[t] = targetDir;
    const busyLabel = targetDir
      ? ('Scanning folder ' + targetDir)
      : ((full ? 'Full rescanning ' : 'Scanning ') + MEDIA[t].label.toLowerCase() + ' folders');
    const busy = startBusy(busyLabel);
    const body = { media: t };
    if (full) body.full = true;
    if (targetDir) body.dir = targetDir;
    scans[t] = api('/scan', { method: 'POST', body: body })
      .then((r) => {
        const res = (r && r[t]) || null;
        if (notify || (res && res.available === false)) toast(scanSummary(t, res, o));
        return res;
      })
      .catch((err) => {
        if (!err || err.name !== 'AbortError') toast('Rescan failed: ' + err.message);
        return null;
      })
      .finally(() => {
        state.scanning[t] = false;
        state.scanMode[t] = null;
        state.scanTarget[t] = null;
        scans[t] = null;
        endBusy(busy);
        invalidate('dir:' + t);
        const onDirs = currentRoute() === 'directories' && state.media === t;
        const prevSel = state.selected[t];
        const tasks = [refreshDash({ skipDirReload: true })];
        if (onDirs) {
          tasks.push(loadKey('dir:' + t));
          if (prevSel) tasks.push(loadBrowse(prevSel, true));
        } else if (state.browse.media === t) {
          state.browse = { media: null, rel: null, rows: null, loading: false, stale: true };
        }
        return Promise.all(tasks).finally(() => {
          if (currentRoute() === 'directories') {
            const selNow = state.selected[t];
            if (selNow && selNow !== prevSel) loadBrowse(selNow, true);
            render();
          }
        });
      });
    return scans[t];
  }

  function openDirectories() {
    const t = state.media;
    invalidate('dir:' + t);                 // read the tree from the database; use Rescan to scan the disk
    if (state.selected[t]) {
      loadBrowse(state.selected[t], true);
    } else {
      state.browse = { media: null, rel: null, rows: null, loading: false, stale: true };
    }
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
    if (d.mode === 'off') return 'Automatic discovery is off. Rescan and opening Directories still work.';
    if (d.mode === 'bulk' || d.mode === 'force') {
      const text = d.mode === 'force'
        ? 'Force checks and normalizes all files in enabled folders again; no periodic scan. Use Rescan to find new files'
        : 'Bulk processes enabled folders only; no periodic scan. Use Rescan to find new files';
      return text + ' · last scan ' + (d.last_scan ? dayAndClock(d.last_scan) : 'not yet');
    }
    const every = d.scan_every ? 'every ' + d.scan_every + ' min' : 'periodically';
    return 'Looking for new files ' + every
      + ' · last ' + (d.last_scan ? dayAndClock(d.last_scan) : 'not yet')
      + ' · next ' + (d.next_scan ? dayAndClock(d.next_scan) : '—');
  }

  function scheduleText(t) {
    const m = dashMedia(t);
    if (!m) return '';
    if (!m.next_change) return m.in_window ? 'Always on' : 'No active hours';
    const when = whenLabel(new Date(m.next_change));
    return m.in_window ? 'Window open until ' + when : 'Window opens ' + when;
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

  function pageHead(title, desc, actions, withSeg, extraCls) {
    return '<header class="page-head' + (extraCls ? ' ' + extraCls : '') + '"><div><h1>' + title + '</h1>'
      + (desc ? '<p class="desc">' + desc + '</p>' : '') + '</div>'
      + '<div class="head-actions">' + (withSeg ? segControl() : '') + (actions || '') + '</div></header>';
  }

  /* ------------------------------------------------------------- dashboard */
  function modeButtons(mode) {
    return '<div class="seg mode-buttons" role="group" aria-label="Mode">'
      + ['off', 'bulk', 'force', 'auto'].map((value) => '<button type="button" data-action="set-mode" data-mode="' + value + '"'
        + ' class="' + (mode === value ? 'on' : '') + '" aria-pressed="' + (mode === value) + '">'
        + MODE_LABEL[value] + '</button>').join('')
      + '</div>';
  }

  function mediaPanel(t) {
    const m = dashMedia(t);
    return '<section class="card">'
      + '<div class="card-head"><div><h2>' + MEDIA[t].label + '</h2>'
      + '<div class="mono sub">' + MEDIA[t].root + '</div></div>'
      + '<span class="chip' + (m.in_window ? '' : ' is-out') + '">' + (m.in_window ? 'In window' : 'Outside window') + '</span></div>'
      + '<div class="stats">'
      + stat('1 · Folders / files in scope', m.directories_enabled + ' / ' + fmt(m.files_total), 'of ' + m.directories_total + ' folders')
      + stat('2 · To do', fmt(m.files_pending), 'waiting for analysis or normalization')
      + stat('3 · Checked', fmt(m.files_checked), 'files with a measured level')
      + stat('4 · Actually changed', fmt(m.files_changed), 'level set to ' + Number(m.target).toFixed(1) + ' LUFS')
      + stat('5 · Parked', fmt(m.files_parked), '<a href="#/parked">waiting for your decision</a>')
      + stat('6 · Empty sections changed', fmt(m.files_empty_changed || 0), fmt(m.files_empty_sections || 0) + ' detected with empty sections')
      + (t === 'audio'
        ? stat('7 · Cover art', fmt(m.files_cover_present || 0) + ' with cover',
               fmt(m.files_cover_embedded || 0) + ' embedded · ' + fmt(m.files_cover_missing || 0) + ' missing'
               + (m.files_cover_parked ? ' · <a href="#/parked">' + fmt(m.files_cover_parked) + ' waiting</a>' : ''))
        : '')
      + '</div>'
      + '<p class="small muted" style="margin-top:12px">' + esc(scheduleText(t)) + '</p>'
      + '</section>';
  }

  function runCard(d, checked, total, pct, noTypes) {
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
      buttons = '';
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
        : mode === 'force'
          ? 'Force: press Start to check and normalize all files in the enabled folders again (also if they have been checked before), within the schedule.'
          : 'Bulk: press Start to check and normalize the files that are in the library now, within the schedule and enabled folders. Files that appear later wait for the next Start.';
    const typesLocked = !isOff && (mode === 'bulk' || mode === 'force') && status !== 'stopped';   // no switches during a bulk or force run
    return '<section class="card">'
      + '<div class="dashboard-mode"><span class="muted">Mode</span>' + modeButtons(mode) + '</div>'
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
      + '</section>';
  }

  function renderDashboard() {
    const d = state.dash;
    const s = state.settings;
    const mode = MODE_LABEL[d.mode] ? d.mode : 'bulk';
    const types = enabledTypes();
    const noTypes = types.length === 0;
    const total = types.reduce((a, t) => a + dashMedia(t).files_total, 0);
    const checked = types.reduce((a, t) => a + dashMedia(t).files_checked, 0);
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
      + runCard(d, checked, total, pct, noTypes)
      + '<div class="split-2">' + mediaPanel('audio') + mediaPanel('video') + '</div>'
      + '<div class="split-2">'
      + '<section class="card"><h2 style="margin-bottom:12px">Needs attention</h2><dl class="rows">'
      + '<div><dt>Mode</dt><dd>' + esc(MODE_LABEL[mode]) + ' <span class="muted">· ' + esc(MODE_SHORT[mode]) + '</span>'
      + ' · <a href="#/settings">change</a></dd></div>'
      + '<div><dt>Look for new files</dt><dd>' + (mode === 'auto'
        ? 'every ' + fmt(d.scan_every) + ' min · last ' + esc(d.last_scan ? dayAndClock(d.last_scan) : 'not yet')
          + ' · next ' + esc(d.next_scan ? dayAndClock(d.next_scan) : '—')
        : '<span class="muted">manual only (Rescan or opening Directories)</span>') + '</dd></div>'
      + '<div><dt>Parked for review</dt><dd><a href="#/parked">Audio ' + dashMedia('audio').files_parked + ' · Video ' + dashMedia('video').files_parked + '</a>'
      + (parkedTotal ? '' : ' <span class="muted">(none)</span>') + '</dd></div>'
      + '<div><dt>Cover art (audio)</dt><dd><a href="#/parked">' + fmt(dashMedia('audio').files_cover_parked || 0) + ' waiting</a>'
      + ' <span class="muted">(' + fmt(dashMedia('audio').files_cover_present || 0) + ' with cover · '
      + fmt(dashMedia('audio').files_cover_missing || 0) + ' missing)</span>'
      + ' · <button class="btn btn-sm" data-action="cover-check-bulk">Check covers</button></dd></div>'
      + '<div><dt>Empty sections changed</dt><dd>Audio ' + fmt(dashMedia('audio').files_empty_changed || 0)
      + ' <span class="muted">(' + fmt(dashMedia('audio').files_empty_sections || 0) + ' detected)</span></dd></div>'
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
      const chg = m.gain_db !== undefined ? m.gain_db : round1(target - m.lufs);
      const emp = m.silent_sections ? m.silent_sections + ' (' + Number(m.silent_duration || 0).toFixed(1) + ' s)' : 'none';
      html += '<div class="measure">'
        + '<div><div class="stat-label">Checked file</div><strong>' + esc(m.name) + '</strong></div>'
        + '<div><div class="stat-label">Integrated loudness</div><strong>' + m.lufs.toFixed(1) + ' LUFS</strong></div>'
        + '<div><div class="stat-label">True peak</div><strong>' + Number(m.tp).toFixed(1) + ' dBTP</strong></div>'
        + '<div><div class="stat-label">Change needed at target ' + Number(target).toFixed(1) + '</div><strong>' + fmtDb(round1(chg)) + '</strong></div>'
        + '<div><div class="stat-label">Empty sections</div><strong>' + esc(emp) + '</strong></div>'
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

  function emptyLabel(f) {
    if (f.silence_trimmed) {
      return '<span class="st st-changed">removed (' + Number(f.silent_duration || 0).toFixed(1) + ' s)</span>';
    }
    if (f.silent_sections > 0) {
      return '<span class="st st-parked">' + f.silent_sections + ' (' + Number(f.silent_duration || 0).toFixed(1) + ' s)</span>';
    }
    if (f.measured_at) return '<span class="dim">none</span>';
    return '—';
  }

  function coverLabel(f, i) {
    if (f.has_cover) {
      const emb = f.cover_status === 'embedded';
      const covUrl = fileCoverUrl(f.id, f.cover_updated_at || f.changed_at || f.measured_at || 1);
      return '<button type="button" class="cover-chip-btn' + (emb ? ' is-embedded' : '') + '" data-action="open-cover-modal" data-idx="' + i + '"'
        + ' data-cover-zoom="' + esc(covUrl) + '" data-cover-caption="' + esc(f.name + ' · Embedded cover') + '" title="Click to view or change cover art">'
        + '<img class="cover-mini-thumb" src="' + esc(covUrl) + '" alt="" loading="lazy">'
        + '<span>' + (emb ? 'embedded' : 'present') + '</span></button>';
    }
    if (f.cover_status === 'choices') {
      const cnt = f.cover_choices_count || 'multiple';
      return '<button type="button" class="cover-chip-btn is-choices" data-action="open-cover-modal" data-idx="' + i + '" title="Multiple pictures found — click to choose">'
        + esc(cnt) + ' choices</button>';
    }
    if (f.cover_status === 'not_found') {
      return '<button type="button" class="cover-chip-btn dim" data-action="open-cover-modal" data-idx="' + i + '" title="No cover found automatically — click to search">not found</button>';
    }
    if (f.cover_status === 'skipped') {
      return '<button type="button" class="cover-chip-btn dim" data-action="open-cover-modal" data-idx="' + i + '" title="Cover skipped — click to search">skipped</button>';
    }
    if (f.has_cover === 0) {
      return '<button type="button" class="cover-chip-btn dim" data-action="open-cover-modal" data-idx="' + i + '" title="No embedded cover picture — click to search Discogs / open sources">missing</button>';
    }
    return '<button type="button" class="cover-chip-btn dim" data-action="open-cover-modal" data-idx="' + i + '">check</button>';
  }

  function renderFilesPaneSearchSourceBar() {
    const sf = currentSearchFrom();
    return '<div class="files-pane-src-row">'
      + '<span class="muted">Take Artist / Title from:</span>'
      + '<div class="seg seg-sm" role="group" aria-label="Take Artist and Title from">'
      + '<button type="button" class="' + (sf === 'auto' ? 'on' : '') + '" data-action="meta-opt-search-from" data-mode="auto" aria-pressed="' + (sf === 'auto') + '" title="Smart automatic: use metadata tags unless missing or totally different from folder/file name">Auto</button>'
      + '<button type="button" class="' + (sf === 'file' ? 'on' : '') + '" data-action="meta-opt-search-from" data-mode="file" aria-pressed="' + (sf === 'file') + '" title="Take Artist / Title from folder / file name">From file</button>'
      + '<button type="button" class="' + (sf === 'metadata' ? 'on' : '') + '" data-action="meta-opt-search-from" data-mode="metadata" aria-pressed="' + (sf === 'metadata') + '" title="Take Artist / Title from embedded file metadata tags">From meta data</button>'
      + '</div>'
      + '<span class="small muted">General setting for cover &amp; metadata lookups</span>'
      + '</div>';
  }

  function renderBrowse(node) {
    const t = node.type;
    const b = state.browse;
    const rowIds = b.rows ? b.rows.map((f) => f.id) : [];
    const sel = state.fileSel.filter((id) => rowIds.includes(id));
    state.fileSel = sel;
    const hasRows = !!(b.rows && b.rows.length);
    const selSuffix = sel.length ? ' (' + sel.length + ')' : '';
    const coverBtns = hasRows && t === 'audio'
      ? '<button class="btn btn-sm" data-action="check-covers-dir" title="Check and propose/embed missing cover art for ' + (sel.length ? 'selected files' : 'all files in this folder') + '">Check covers' + selSuffix + '</button>'
        + '<button class="btn btn-sm" data-action="open-album-cover" title="Search and set cover art for this album / folder">Album cover' + selSuffix + '</button>'
        + '<button class="btn btn-sm" data-action="check-meta-dir" title="Check metadata tags for ' + (sel.length ? 'selected files' : 'all files in this folder') + '">Check meta' + selSuffix + '</button>'
      : '';
    const scanning = !!state.scanning[t];
    const folderScanning = scanning && state.scanMode[t] === 'folder' && state.scanTarget[t] === node.rel;
    const rescanFolderBtn = '<button class="btn btn-sm' + (folderScanning ? ' is-busy' : '') + '" data-action="rescan-folder" data-rel="' + esc(node.rel) + '"'
      + (scanning ? ' disabled' : '') + ' title="Rescan only ' + esc(node.rel) + ' and its sub-folders">'
      + (folderScanning ? 'Scanning…' : 'Rescan folder') + '</button>';
    const batchBtns = hasRows
      ? coverBtns
        + '<button class="btn btn-sm" data-action="check-all-files" title="Check level and empty sections for ' + (sel.length ? 'selected files' : 'all files in this folder') + '">Check all' + selSuffix + '</button>'
        + '<button class="btn btn-sm btn-primary" data-action="execute-files" title="Execute normalization for ' + (sel.length ? 'selected files' : 'all files in this folder') + '">Execute' + selSuffix + '</button>'
      : '';
    const srcBar = t === 'audio' ? renderFilesPaneSearchSourceBar() : '';
    const head = '<div class="card-head" style="margin-bottom:8px"><div><h2>' + esc(MEDIA[t].root + '/' + node.rel) + '</h2>'
      + '<div class="sub">' + node.directFiles + ' files directly in this folder · ' + (subtree(node).length - 1) + ' sub-folders'
      + ' · Play listens to the original · Edit fades or cuts a part</div></div>'
      + '<div class="btns">' + rescanFolderBtn + batchBtns
      + '<span class="chip' + (node.enabled ? '' : ' is-out') + '">' + (node.enabled ? 'Included' : 'Excluded') + '</span></div></div>'
      + srcBar;
    if (b.loading || b.media !== t || b.rel !== node.rel || !b.rows) {
      return head + '<p class="muted">Loading files…</p>';
    }
    if (!b.rows.length) {
      return head + '<p class="muted">No files directly in this folder. Open a sub-folder in the tree.</p>';
    }
    const allOn = b.rows.length > 0 && b.rows.every((f) => sel.includes(f.id));
    const rows = b.rows.map((f, i) => {
      const ext = String(f.name).split('.').pop();
      const on = sel.includes(f.id);
      const checking = !!(state.measure && state.measure.relpath === f.relpath && state.measure.state === 'measuring');
      const playing = PLAY.media === t && PLAY.relpath === f.relpath && PLAY.playing;
      const editing = t === 'audio' && !!f.playable
        ? '<button class="btn btn-sm" data-action="edit-file" data-idx="' + i + '"'
          + (f.status === 'processing'
            ? ' disabled title="This file is being processed right now"'
            : ' title="Edit ' + esc(f.name) + ': fades and cut"') + '>Edit</button>'
        : '';
      const coverBtn = t === 'audio'
        ? '<button class="btn btn-sm" data-action="open-cover-modal" data-idx="' + i + '" title="Search or choose cover art for ' + esc(f.name) + '">Cover</button>'
        : '';
      const metaBtn = t === 'audio'
        ? '<button class="btn btn-sm" data-action="open-meta-modal" data-idx="' + i + '" title="Check and update metadata tags for ' + esc(f.name) + '">Meta</button>'
        : '';
      const listen = !f.playable
        ? '<button class="btn btn-sm" disabled title="This format cannot be played in the browser">Play</button>'
        : '<button class="btn btn-sm' + (playing ? ' btn-primary' : '') + '" data-action="preview" data-idx="' + i
          + '" aria-pressed="' + playing + '" title="' + esc((playing ? 'Pause ' : 'Listen to ') + f.name) + '">'
          + (playing ? 'Pause' : 'Play') + '</button>';
      const check = checking
        ? '<span class="row-busy"><span class="working" aria-hidden="true"></span>Checking…</span>'
        : '<button class="btn btn-sm" data-action="check-level" data-idx="' + i + '">Check level</button>';
      const cls = [checking ? 'is-busy-row' : playing ? 'is-playing-row' : '', on ? 'row-sel' : ''].filter(Boolean).join(' ');
      return '<tr' + (cls ? ' class="' + cls + '"' : '') + '>'
        + '<td class="pick"><input type="checkbox" data-file-check="' + f.id + '" aria-label="Select ' + esc(f.name) + '"' + (on ? ' checked' : '') + '></td>'
        + '<td class="file-cell">' + esc(f.name) + '</td><td class="mono">' + esc(ext) + '</td>'
        + (t === 'audio' ? '<td class="cover-cell">' + coverLabel(f, i) + '</td>' : '')
        + '<td class="num">' + fmtLufs(f.lufs) + '</td>'
        + '<td class="num">' + fmtDbN(f.gain_db) + '</td>'
        + '<td>' + emptyLabel(f) + '</td>'
        + '<td>' + esc(METHOD_LABEL[f.method] || f.method || '—') + '</td>'
        + '<td>' + statusLabel(f.status) + '</td>'
        + '<td class="actions-cell">' + listen + editing + coverBtn + metaBtn + check + '</td></tr>';
    }).join('');
    return head
      + '<div class="table-wrap files-table-wrap"><table class="files-table"><thead><tr>'
      + '<th class="pick"><input type="checkbox" data-file-all aria-label="Select all files in this folder"' + (allOn ? ' checked' : '') + '></th>'
      + '<th>File</th><th>Format</th>'
      + (t === 'audio'
        ? '<th>Cover' + tip('Embedded cover picture in the audio metadata. Click a cover badge or Cover button to view, pick between found pictures, or search Discogs and open sources (per file or per album).') + '</th>'
        : '')
      + '<th class="num">Level'
      + tip('Integrated loudness of the whole file (EBU R128), measured without changing the file. Check level measures one file again.')
      + '</th><th class="num">Change</th><th>Empty'
      + tip('Long silent ("no sound") sections detected in the file based on the thresholds in Settings.')
      + '</th><th>Method</th><th>Status</th><th></th></tr></thead><tbody>' + rows + '</tbody></table></div>'
      + (b.rows.length >= 500 ? '<p class="small muted" style="margin-top:10px">Showing the first 500 files.</p>' : '');
  }

  function captureDirScroll() {
    const root = app();
    if (!root) return;
    const treeEl = root.querySelector('.split-dirs > .tree');
    if (treeEl) {
      const m = treeEl.dataset.media || state.media;
      state.treeScroll[m] = { top: treeEl.scrollTop, left: treeEl.scrollLeft };
    }
    const filesEl = root.querySelector('.split-dirs > .files-pane');
    if (filesEl) {
      const m = filesEl.dataset.media || state.media;
      state.filesScroll[m] = {
        rel: filesEl.dataset.rel || null,
        top: filesEl.scrollTop,
        left: filesEl.scrollLeft,
      };
    }
  }

  function restoreDirScroll(t, selRel) {
    const root = app();
    if (!root) return;
    const treeEl = root.querySelector('.split-dirs > .tree');
    const savedTree = state.treeScroll[t];
    if (treeEl && savedTree) {
      treeEl.scrollTop = savedTree.top;
      treeEl.scrollLeft = savedTree.left;
    }
    const filesEl = root.querySelector('.split-dirs > .files-pane');
    const savedFiles = state.filesScroll[t];
    if (filesEl && savedFiles && savedFiles.rel === (selRel || '')) {
      filesEl.scrollTop = savedFiles.top;
      filesEl.scrollLeft = savedFiles.left;
    }
  }

  function renderDirectories() {
    const t = state.media;
    const tr = tree(t);
    const m = dashMedia(t);
    const selRel = state.selected[t];
    const sel = selRel && tr.map.get(selRel);
    const scanning = !!state.scanning[t];
    captureDirScroll();
    if (sel && (state.browse.stale || state.browse.media !== t || state.browse.rel !== sel.rel) && !state.browse.loading) {
      loadBrowse(sel.rel, state.browse.media === t && state.browse.rel === sel.rel);
    }
    const dirs = !tr.roots.length
      ? '<p class="muted">No folders found under the base location. Check the mount in docker-compose.yml, then use Rescan.</p>'
      : '';
    const quickBusy = scanning && state.scanMode[t] === 'quick';
    const fullBusy = scanning && state.scanMode[t] === 'full';
    const watchActive = !!(state.dash && state.dash.watcher_active);
    app().innerHTML = pageHead('Directories',
      'Base location <span class="mono">' + MEDIA[t].root + '</span>, mounted from <span class="mono">' + MEDIA[t].host
      + '</span> in docker-compose.yml. Untick a folder to exclude it. Ticking or unticking applies to its sub-folders too.',
      '<button class="btn btn-sm' + (quickBusy ? ' is-busy' : '') + '" data-action="rescan"' + (scanning ? ' disabled' : '') + '>'
      + (quickBusy ? 'Scanning…' : 'Rescan') + '</button>'
      + '<button class="btn btn-sm' + (fullBusy ? ' is-busy' : '') + '" data-action="rescan-full"' + (scanning ? ' disabled' : '')
      + ' title="Walk every folder on disk from scratch, ignoring cached folder timestamps">'
      + (fullBusy ? 'Full rescan…' : 'Full rescan') + '</button>'
      + tip('Rescan quickly checks only folders whose timestamps changed since the last scan, without resetting already-scanned files. Use Full rescan to walk every folder on disk from scratch.')
      + '<button class="btn btn-sm" data-action="select-all">Select all</button>'
      + '<button class="btn btn-sm" data-action="invert-dirs" title="Invert the enabled/disabled selection of all folders">Invert</button>'
      + '<button class="btn btn-sm" data-action="select-none">Select none</button>'
      + '<button class="btn btn-sm" data-action="expand-all">Expand all</button>'
      + '<button class="btn btn-sm" data-action="collapse-all">Collapse all</button>', true)
      + '<p class="muted" style="margin-bottom:0"><strong style="color:var(--text)">' + m.directories_enabled + ' of ' + m.directories_total
      + '</strong> folders included · ' + fmt(m.files_total) + ' files in scope'
      + (watchActive ? ' · <span class="dim" title="Live filesystem watcher (inotify) automatically detects added or removed folders">live watch active</span>' : '')
      + (scanning ? ' · <span class="scan-note">looking for new files and folders…</span>' : '') + '</p>'
      + baselinePanel(t)
      + dirs
      + '<div class="split-dirs">'
      + '<section class="card tree" data-media="' + t + '" aria-label="Folder tree"><ul>' + renderTree(tr.roots) + '</ul></section>'
      + '<section class="card files-pane" data-media="' + t + '" data-rel="' + esc(selRel || '') + '">'
      + (sel ? renderBrowse(sel) : (t === 'audio' ? renderFilesPaneSearchSourceBar() : '') + '<p class="muted">Select a folder in the tree.</p>') + '</section>'
      + '</div>';
    app().querySelectorAll('input[data-check]').forEach((cb) => {
      const n = tr.map.get(cb.dataset.check);
      cb.indeterminate = !!n && subtreeState(n) === 'mixed';
    });
    restoreDirScroll(t, selRel);
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

  function renderCoverParkedPane() {
    const cp = state.coverParked || { albums: [], choices_files: [], not_found_files: [], counts: { albums: 0, choices: 0, not_found: 0, total: 0 } };
    const mode = state.coverParkMode || 'album';
    const choicesCount = cp.choices_files.length;
    const nfCount = cp.not_found_files.length;
    const modeSeg = '<div class="seg" role="group" aria-label="Cover update grouping">'
      + '<button type="button" class="' + (mode === 'album' ? 'on' : '') + '" data-action="cover-park-mode" data-mode="album">Per album (' + cp.albums.length + ')</button>'
      + '<button type="button" class="' + (mode === 'file' ? 'on' : '') + '" data-action="cover-park-mode" data-mode="file">Per file (' + choicesCount + ')</button>'
      + '</div>';

    let bodyHtml = '';
    if (!choicesCount && !nfCount) {
      bodyHtml = '<p class="muted">No audio files are waiting for cover art selection. Use <strong>Check missing covers</strong> to scan enabled folders for missing pictures.</p>';
    } else {
      if (choicesCount > 0) {
        if (mode === 'album') {
          bodyHtml += cp.albums.map((g) => {
            const albumTitle = [g.artist, g.album].filter(Boolean).join(' — ') || folderLabel(g.dir);
            const overrideBadge = g.query_source === 'path_override'
              ? '<span class="cover-override-tag" title="ID3 tag was totally different from the folder/file name, so the folder/file name was used">Folder/file name used (ID3 differed)</span>'
              : '';
            const idsStr = g.file_ids.join(',');
            const candsHtml = g.candidates.map((c) => {
              const thumb = candThumbUrl(g.primary_file_id, c.thumb_url || c.image_url || '');
              const fullImg = candThumbUrl(g.primary_file_id, c.image_url || c.thumb_url || '');
              const cap = [c.title, c.source, c.year].filter(Boolean).join(' · ');
              return '<div class="cover-cand">'
                + '<div class="cover-cand-img-wrap"' + (fullImg ? ' data-cover-zoom="' + esc(fullImg) + '" data-cover-caption="' + esc(cap) + '"' : '') + '>'
                + (thumb ? '<img class="cover-cand-img" src="' + esc(thumb) + '" alt="' + esc(c.title) + '" loading="lazy">' : '<span class="muted small">No preview</span>')
                + '</div>'
                + '<div class="cover-cand-title">' + esc(c.title || 'Cover') + '</div>'
                + '<div class="cover-cand-meta"><span class="tag" style="margin:0">' + esc(c.source || 'Source') + '</span>'
                + '<span>' + esc(c.year || '') + '</span></div>'
                + '<div class="cover-cand-actions">'
                + '<button type="button" class="btn btn-sm btn-primary" data-action="cover-apply-cand" data-file="' + g.primary_file_id + '" data-scope="file" data-url="' + esc(c.image_url) + '" title="Apply only to ' + esc(g.files[0].name) + '">1 file</button>'
                + '<button type="button" class="btn btn-sm btn-album" data-action="cover-apply-cand" data-file="' + g.primary_file_id + '" data-scope="album" data-ids="' + esc(idsStr) + '" data-url="' + esc(c.image_url) + '">Use for album (' + g.file_ids.length + ')</button>'
                + '</div></div>';
            }).join('');
            const fileNames = g.files.slice(0, 6).map((f) => esc(f.name)).join(' · ')
              + (g.files.length > 6 ? ' · +' + (g.files.length - 6) + ' more' : '');
            return '<div class="cover-album-group">'
              + '<div class="cover-album-head"><div>'
              + '<strong>' + esc(albumTitle) + '</strong>' + overrideBadge
              + '<div class="small muted mono">' + esc(g.dir) + ' · ' + g.file_ids.length + ' file' + (g.file_ids.length === 1 ? '' : 's') + '</div>'
              + '<div class="small muted" style="margin-top:2px">' + fileNames + '</div>'
              + '</div>'
              + '<div class="btns">'
              + '<button type="button" class="btn btn-sm" data-action="open-cover-by-id" data-file="' + g.primary_file_id + '" data-scope="album" data-ids="' + esc(idsStr) + '">Search / Edit query</button>'
              + '<button type="button" class="btn btn-sm" data-action="cover-skip" data-ids="' + esc(idsStr) + '">Skip album</button>'
              + '</div></div>'
              + '<div class="cover-grid">' + candsHtml + '</div>'
              + '</div>';
          }).join('');
        } else {
          bodyHtml += cp.choices_files.map((f) => {
            const queryLabel = [f.artist, f.album, f.title].filter(Boolean).join(' · ') || f.name;
            const overrideBadge = f.query_source === 'path_override'
              ? '<span class="cover-override-tag" title="ID3 tag was totally different from the folder/file name, so the folder/file name was used">Folder/file name used (ID3 differed)</span>'
              : '';
            const candsHtml = f.candidates.map((c) => {
              const thumb = candThumbUrl(f.id, c.thumb_url || c.image_url || '');
              const fullImg = candThumbUrl(f.id, c.image_url || c.thumb_url || '');
              const cap = [c.title, c.source, c.year].filter(Boolean).join(' · ');
              return '<div class="cover-cand">'
                + '<div class="cover-cand-img-wrap"' + (fullImg ? ' data-cover-zoom="' + esc(fullImg) + '" data-cover-caption="' + esc(cap) + '"' : '') + '>'
                + (thumb ? '<img class="cover-cand-img" src="' + esc(thumb) + '" alt="' + esc(c.title) + '" loading="lazy">' : '<span class="muted small">No preview</span>')
                + '</div>'
                + '<div class="cover-cand-title">' + esc(c.title || 'Cover') + '</div>'
                + '<div class="cover-cand-meta"><span class="tag" style="margin:0">' + esc(c.source || 'Source') + '</span>'
                + '<span>' + esc(c.year || '') + '</span></div>'
                + '<div class="cover-cand-actions">'
                + '<button type="button" class="btn btn-sm btn-primary" data-action="cover-apply-cand" data-file="' + f.id + '" data-scope="file" data-url="' + esc(c.image_url) + '">Use for file</button>'
                + '<button type="button" class="btn btn-sm btn-album" data-action="cover-apply-cand" data-file="' + f.id + '" data-scope="album" data-url="' + esc(c.image_url) + '">Album</button>'
                + '</div></div>';
            }).join('');
            return '<div class="cover-album-group">'
              + '<div class="cover-album-head"><div>'
              + '<strong>' + esc(f.name) + '</strong>' + overrideBadge
              + '<div class="small muted">' + esc(queryLabel) + ' · <span class="mono">' + esc(f.dir) + '</span></div>'
              + '</div>'
              + '<div class="btns">'
              + '<button type="button" class="btn btn-sm" data-action="open-cover-by-id" data-file="' + f.id + '" data-scope="file">Search / Edit query</button>'
              + '<button type="button" class="btn btn-sm" data-action="cover-skip" data-ids="' + f.id + '">Skip</button>'
              + '</div></div>'
              + '<div class="cover-grid">' + candsHtml + '</div>'
              + '</div>';
          }).join('');
        }
      }

      if (nfCount > 0) {
        const nfRows = cp.not_found_files.map((f) => {
          const q = [f.artist, f.album, f.title].filter(Boolean).join(' · ') || '—';
          const overrideBadge = f.query_source === 'path_override'
            ? '<span class="cover-override-tag">Folder/file name used</span>'
            : '';
          return '<tr>'
            + '<td class="file-cell">' + esc(f.name) + '</td>'
            + '<td class="dim mono folder-cell">' + esc(folderLabel(f.dir)) + '</td>'
            + '<td>' + esc(q) + overrideBadge + '</td>'
            + '<td class="actions">'
            + '<button type="button" class="btn btn-sm btn-primary" data-action="open-cover-by-id" data-file="' + f.id + '" data-scope="album">Search cover</button>'
            + '<button type="button" class="btn btn-sm" data-action="cover-skip" data-ids="' + f.id + '">Skip</button>'
            + '</td></tr>';
        }).join('');
        bodyHtml += '<div style="margin-top:' + (choicesCount ? '18px' : '0') + '">'
          + '<h2 style="margin-bottom:8px">No picture found automatically (' + nfCount + ')</h2>'
          + '<p class="small muted" style="margin-bottom:10px">Use Search cover to adjust the Artist / Album / Title query and pick a picture for the file or album.</p>'
          + '<div class="table-wrap"><table><thead><tr><th>File</th><th>Folder</th><th>Query tried</th><th></th></tr></thead>'
          + '<tbody>' + nfRows + '</tbody></table></div></div>';
      }
    }

    return '<section class="card cover-parked-pane" id="cover-parked-pane">'
      + '<div class="card-head"><div><h2>Cover art — picture selection &amp; missing covers</h2>'
      + '<div class="sub">Separate pane for audio files without embedded pictures. Choose between multiple found pictures per album or per file, or bulk-update with the best match.</div></div>'
      + '<div class="btns">' + modeSeg
      + '<button type="button" class="btn btn-sm" data-action="cover-check-bulk">Check missing covers</button>'
      + '<button type="button" class="btn btn-sm btn-primary" data-action="cover-apply-first-all"' + (choicesCount ? '' : ' disabled') + '>Use first picture for all (' + choicesCount + ')</button>'
      + '</div></div>'
      + bodyHtml
      + '</section>';
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
    const coverSection = t === 'audio' ? renderCoverParkedPane() : '';
    app().innerHTML = pageHead('Parked files',
      'Files that need a decision are listed here. Audio normalization parked files and missing/multi-choice cover art have separate panes.',
      '<a class="btn" href="#/settings">Threshold</a>', true)
      + (t === 'audio' ? largeSection + lossySection + coverSection : largeSection);
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
      + '<span><span class="swatch" style="background:var(--track)"></span>paused</span>'
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
      + tip('Higher numbers (closer to 0, such as −14 or −10 LUFS) make the music louder; lower numbers (more negative, such as −23 or −30 LUFS) make it quieter. −14 LUFS suits music streaming, −16 suits podcasts and speech, and −23 is the TV broadcast standard.') + '</label>'
      + '<div class="slider-row"><input id="target-' + t + '" type="range" min="-30" max="-10" step="0.5" value="' + cfg.target + '" data-scope="' + t + '" data-setting="target">'
      + '<div class="val"><span data-out="' + t + '.target">' + Number(cfg.target).toFixed(1) + '</span> LUFS</div></div>'
      + '<p class="baseline-link-row"><a class="inline-link" href="#/directories" data-action="baseline-start" data-media="' + t + '">Set the baseline from a file: check its level in Directories →</a></p>'
      + '</div>'
      + '<div class="field"><label class="lbl" for="tp-' + t + '">True-peak ceiling'
      + tip('Prevents distortion (clipping) when boosting quiet files. Higher numbers (closer to 0, such as −1.0 dBTP) allow louder peaks so quiet music can be turned up further; lower numbers (more negative, such as −6.0 dBTP) leave more headroom and keep boosted files quieter.') + '</label>'
      + '<div class="slider-row"><input id="tp-' + t + '" type="range" min="-6" max="-1" step="0.1" value="' + cfg.truePeak + '" data-scope="' + t + '" data-setting="truePeak">'
      + '<div class="val"><span data-out="' + t + '.truePeak">' + Number(cfg.truePeak).toFixed(1) + '</span> dBTP</div></div>'
      + '</div>'
      + '<div class="field"><label class="lbl" for="hys-' + t + '">Hysteresis (no-change bandwidth)'
      + tip('If a file\'s loudness is already within ± this bandwidth around the target, its audio level is not changed. Higher numbers (up to 6.0 dB) leave files that are already fairly close to the target untouched; lower numbers (down to 0.0 dB) adjust even small volume differences so files match the target more tightly.') + '</label>'
      + '<div class="slider-row"><input id="hys-' + t + '" type="range" min="0" max="6" step="0.1" value="' + (cfg.hysteresis ?? 0.5) + '" data-scope="' + t + '" data-setting="hysteresis">'
      + '<div class="val">±<span data-out="' + t + '.hysteresis">' + Number(cfg.hysteresis ?? 0.5).toFixed(1) + '</span> dB</div></div>'
      + '</div>'
      + '<div class="field"><label class="lbl" for="th-' + t + '">Large-change threshold'
      + tip('Changes bigger than this are parked for review instead of being applied automatically. A higher number (up to 12 dB) lets the app make larger volume jumps (much louder or quieter) on its own; a lower number (down to 1 dB) only applies small tweaks automatically and parks bigger changes.') + '</label>'
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
      ['force', 'Force', 'Nothing happens until you press Start on the dashboard: then all files in the enabled folders are checked and normalized again, also if they have been checked before.'],
      ['off', 'Off', 'Nothing is scanned and nothing is normalized. Rescan, Check level and the audio editor still work when you use them.'],
    ].map(([v, title, d]) => '<label class="opt"><input type="radio" name="app-mode" value="' + v + '"'
      + ' data-scope="general" data-setting="mode"' + (mode === v ? ' checked' : '') + '>'
      + '<span><strong style="font-weight:500">' + title + '</strong>'
      + (v === 'bulk' ? '<span class="tag">default</span>' : '') + tip(d) + '</span></label>').join('');
    return '<section class="card"><div class="card-head"><div><h2>Mode</h2>'
      + '<div class="sub">Off, Bulk (you start it), Force (check all enabled files again) or Automatic (new files are normalized for you)</div></div></div>'
      + '<div class="field" style="margin:0"><span class="lbl">Mode' + tip('Switches the whole app: Off, Bulk (start a run yourself), Force (check and normalize all files in enabled folders again) or Automatic (new files are found and normalized for you).') + '</span>'
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
      + tip('Used only by Automatic mode: the folders are scanned this often and new or changed files are normalized. Bulk and Force have no periodic scan. A manual Rescan also counts, so the next automatic scan starts a full interval later.') + '</label>'
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

  function silenceCard() {
    const g = state.draft.general;
    const pct = g.silenceLevelPct ?? 2.0;
    const peak = g.silencePeakDb ?? -40.0;
    const minDur = g.silenceMinDuration ?? 5.0;
    const fadeOut = g.silenceFadeOut ?? 0.5;
    const fadeIn = g.silenceFadeIn ?? 0.5;
    return '<section class="card" style="margin-top:16px"><div class="card-head"><div><h2>Empty sections (no sound)</h2>'
      + '<div class="sub">Automatically detect and delete long silent sections in audio files, applying fade-out and fade-in instead of a hard cut.</div></div></div>'
      + '<label class="check" style="padding-top:0;border-bottom:1px solid var(--line);margin-bottom:16px">'
      + '<input class="switch" type="checkbox" data-scope="general" data-setting="trimSilence"' + (g.trimSilence ? ' checked' : '') + '>'
      + '<span><strong>Remove large empty sections of no sound</strong>'
      + tip('General option: when enabled, audio sections that meet the "no sound" criteria and are longer than the allowed time are automatically deleted during a run.')
      + '</span></label>'
      + '<div class="split-2" style="margin-top:0">'
      + '<div>'
      + '<div class="field"><label class="lbl" for="sil-pct">No-sound level (below % of file average)'
      + tip('A section is considered "no sound" when its RMS level stays below this percentage of the file\'s average loudness. Higher numbers catch slightly louder background hiss; lower numbers only match near-total silence.') + '</label>'
      + '<div class="slider-row"><input id="sil-pct" type="range" min="0.5" max="20" step="0.5" value="' + pct + '" data-scope="general" data-setting="silenceLevelPct">'
      + '<div class="val"><span data-out="general.silenceLevelPct">' + Number(pct).toFixed(1) + '</span> %</div></div></div>'
      + '<div class="field"><label class="lbl" for="sil-peak">Maximum peak level during "no sound"'
      + tip('Peaks inside a "no sound" section may not be higher than this ceiling. Higher numbers (closer to 0, such as −25 dB) allow louder background noise to still count as no sound; lower numbers (more negative, such as −60 dB) require stricter silence.') + '</label>'
      + '<div class="slider-row"><input id="sil-peak" type="range" min="-70" max="-20" step="1" value="' + peak + '" data-scope="general" data-setting="silencePeakDb">'
      + '<div class="val"><span data-out="general.silencePeakDb">' + Number(peak).toFixed(1) + '</span> dB</div></div></div>'
      + '<div class="field" style="margin-bottom:0"><label class="lbl" for="sil-dur">Allowed "no sound" time (only longer sections are deleted)'
      + tip('Normal pauses up to this duration are kept. Only continuous "no sound" sections strictly longer than this time are deleted.') + '</label>'
      + '<div class="slider-row"><input id="sil-dur" type="range" min="1" max="60" step="0.5" value="' + minDur + '" data-scope="general" data-setting="silenceMinDuration">'
      + '<div class="val"><span data-out="general.silenceMinDuration">' + Number(minDur).toFixed(1) + '</span> s</div></div></div>'
      + '</div>'
      + '<div>'
      + '<div class="field"><label class="lbl" for="sil-fo">Fade-out time before cut'
      + tip('Instead of a sudden hard cut at the start of a deleted silent section, the audio fades out smoothly over this time (0.0 s = clear cut).') + '</label>'
      + '<div class="slider-row"><input id="sil-fo" type="range" min="0" max="10" step="0.1" value="' + fadeOut + '" data-scope="general" data-setting="silenceFadeOut">'
      + '<div class="val"><span data-out="general.silenceFadeOut">' + Number(fadeOut).toFixed(1) + '</span> s</div></div></div>'
      + '<div class="field" style="margin-bottom:0"><label class="lbl" for="sil-fi">Fade-in time after cut'
      + tip('Instead of a sudden hard cut after a deleted silent section, the audio fades back in smoothly over this time (0.0 s = clear cut).') + '</label>'
      + '<div class="slider-row"><input id="sil-fi" type="range" min="0" max="10" step="0.1" value="' + fadeIn + '" data-scope="general" data-setting="silenceFadeIn">'
      + '<div class="val"><span data-out="general.silenceFadeIn">' + Number(fadeIn).toFixed(1) + '</span> s</div></div></div>'
      + '</div>'
      + '</div></section>';
  }

  function coverCard() {
    const g = state.draft.general;
    const srcMode = g.coverSources || 'both';
    const srcOpts = [
      ['both', 'Both: Discogs API + Open sources (MusicBrainz / Cover Art Archive / iTunes)', 'Queries Discogs and open sources to propose cover pictures. Single matches are embedded automatically; multiple matches are parked in the Cover art pane for you to choose.', true],
      ['discogs', 'Discogs native API only', 'Searches only the Discogs database. Add a Personal Access Token below for full rate limits (60 requests/min).', false],
      ['open', 'Open sources only (no API key needed)', 'Searches MusicBrainz / Cover Art Archive and iTunes Search API without requiring an account or token.', false],
    ].map(([v, title, d, def]) => '<label class="opt"><input type="radio" name="cover-sources" value="' + v + '"'
      + ' data-scope="general" data-setting="coverSources"' + (srcMode === v ? ' checked' : '') + '>'
      + '<span><strong style="font-weight:500">' + title + '</strong>'
      + (def ? '<span class="tag">default</span>' : '') + tip(d) + '</span></label>').join('');

    return '<section class="card" style="margin-top:16px"><div class="card-head"><div><h2>Cover art (audio metadata)</h2>'
      + '<div class="sub">Detect missing cover pictures in audio files, compare ID3 tags against folder/file names (using the folder/file name when totally different), and propose pictures from Discogs and open sources.</div></div></div>'
      + '<label class="check" style="padding-top:0">'
      + '<input class="switch" type="checkbox" data-scope="general" data-setting="coverAuto"' + (g.coverAuto ? ' checked' : '') + '>'
      + '<span><strong>Check and embed missing cover art during normalization runs</strong>'
      + tip('When enabled, audio files without an embedded picture are checked during runs. If 1 picture is found it is embedded automatically; if multiple pictures are found the file is listed in the separate Cover art pane on Parked files.')
      + '</span></label>'
      + '<label class="check" style="margin-bottom:16px;border-bottom:1px solid var(--line)">'
      + '<input class="switch" type="checkbox" data-scope="general" data-setting="useLocalCover"' + ((g.useLocalCover ?? true) ? ' checked' : '') + '>'
      + '<span><strong>Use local folder image (cover.jpg / folder.jpg) when present</strong>'
      + tip('If the audio file\'s folder already contains cover.jpg, folder.jpg, front.jpg, or album.jpg, embed that picture before querying online sources.')
      + '</span></label>'
      + '<div class="split-2" style="margin-top:0">'
      + '<div class="field" style="margin:0"><span class="lbl">Cover art sources</span>' + srcOpts + '</div>'
      + '<div><div class="field"><label class="lbl" for="discogs-token">Discogs Personal Access Token (optional)'
      + tip('Generate a free Personal Access Token in your Discogs account under Settings → Developers. Increases the Discogs rate limit to 60 requests/minute and unlocks full release image search.') + '</label>'
      + '<input class="input" id="discogs-token" type="password" autocomplete="off" data-scope="general" data-setting="discogsToken" placeholder="Paste Discogs Personal Access Token" value="' + esc(g.discogsToken || '') + '">'
      + '</div>'
      + '<div class="btns"><button type="button" class="btn btn-sm" data-action="discogs-test">Test Discogs</button>'
      + '<span class="muted small">' + esc(state.discogsTest || '') + '</span></div>'
      + '</div></div></section>';
  }

  function appearanceCard() {
    const cur = (state.draft && state.draft.general && state.draft.general.theme) || themePref || 'light';
    const opts = [
      ['light', 'Light', 'Crisp warm-grey and paper surfaces for bright environments.', true],
      ['dark', 'Dark', 'Low-glare charcoal surfaces with balanced contrast for dim rooms and late sessions.', false],
      ['system', 'Automatic (system)', 'Follows your operating system or browser light/dark preference automatically.', false],
    ].map(([v, title, d, def]) => '<label class="opt"><input type="radio" name="app-theme" value="' + v + '"'
      + ' data-scope="general" data-setting="theme"' + (cur === v ? ' checked' : '') + '>'
      + '<span><strong style="font-weight:500">' + title + '</strong>'
      + (def ? '<span class="tag">default</span>' : '') + tip(d) + '</span></label>').join('');
    return '<section class="card" style="margin-bottom:16px"><div class="card-head"><div><h2>Appearance</h2>'
      + '<div class="sub">Switch the GUI between Light mode, Dark mode, or Automatic system preference. Also available anytime in the left menu.</div></div></div>'
      + '<div class="field" style="margin:0"><span class="lbl">Theme</span>' + opts + '</div></section>';
  }

  function renderSettings() {
    const t = state.media;
    const g = state.draft.general;
    app().innerHTML = pageHead('Settings',
      'Level and output are set separately for audio and video. Changes take effect when you press Save.',
      '<button class="btn" data-action="settings-reset">Reset ' + MEDIA[t].label.toLowerCase() + '</button>'
      + '<button class="btn btn-primary" data-action="settings-save">Save</button>', true)
      + appearanceCard()
      + modeCard()
      + '<section class="card"><div class="check-list">'
      + '<label class="check" style="padding-top:0"><input class="switch" type="checkbox" data-scope="general" data-setting="dryRun"' + (g.dryRun ? ' checked' : '') + '>'
      + '<span><strong>Dry run</strong>' + tip('Analyse and report only. Nothing is written. Recommended for the first run on a real library.') + '</span></label>'
      + '<p class="small muted" style="padding-top:12px;border-top:1px solid var(--line)">Web access has no login (local network only, as chosen). '
      + 'Do not forward the port to the internet. The Home Assistant API still requires its access token.</p>'
      + '</div></section>'
      + silenceCard()
      + coverCard()
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

  /* ------------------------------------------------------------- metadata */
  const META_FIELDS = [
    ['artist', 'Artist'],
    ['title', 'Title (Track)'],
    ['album', 'Album'],
    ['album_artist', 'Album Artist'],
    ['year', 'Year'],
    ['track', 'Track No.'],
    ['genre', 'Genre'],
    ['cover', 'Cover picture'],
  ];
  const META_FIELD_MAP = Object.fromEntries(META_FIELDS);
  const META_SOURCES = [
    ['discogs', 'Discogs'],
    ['musicbrainz', 'MusicBrainz'],
    ['open', 'Open (iTunes)'],
  ];

  const META_COL_SPECS = [
    ['pick', '', 34, false],
    ['file', 'File & folder', 140, true],
    ['custom', 'Current tag / Selected (✎)', 155, true],
    ['field', 'Field', 75, true],
    ['path', 'Folder / file name', 80, true],
    ['discogs', 'Discogs', 135, true],
    ['musicbrainz', 'MusicBrainz', 145, true],
    ['open', 'Open (iTunes)', 145, true],
  ];

  function makeDragAttr(fileId, fieldKey, source, val, isModal) {
    if (!val || (fieldKey === 'cover' && val === 'present')) return '';
    const payload = JSON.stringify({
      fileId: fileId,
      field: fieldKey,
      source: source,
      value: String(val),
      modal: !!isModal,
    });
    return ' draggable="true" data-drag-meta="' + esc(payload) + '"';
  }

  function renderOnlineSourceCell(fileItem, fieldKey, srcKey, srcLabel, rawVal, chosen, pickAct, isModal) {
    const val = String(rawVal || '').trim();
    const dropAttrs = ' data-drop-file="' + fileItem.id + '" data-drop-field="' + fieldKey + '"'
      + (isModal ? ' data-drop-modal="1"' : '');
    if (!val) {
      return '<td class="meta-choice-cell"' + dropAttrs + '><span class="muted small">—</span></td>';
    }
    const dragAttr = makeDragAttr(fileItem.id, fieldKey, srcKey, val, isModal);
    if (fieldKey === 'cover') {
      const thumb = candThumbUrl(fileItem.id, val, fileItem.cover_updated_at || fileItem.checked_at);
      return '<td class="meta-choice-cell"' + dropAttrs + '>'
        + '<button type="button" class="meta-pill' + (chosen ? ' is-chosen' : '') + '"'
        + ' data-action="' + pickAct + '" data-file="' + fileItem.id + '" data-field="' + fieldKey + '" data-choice="' + srcKey + '"'
        + ' data-cover-zoom="' + esc(thumb) + '" data-cover-caption="' + esc(srcLabel + ' cover') + '"'
        + dragAttr + ' title="Use ' + esc(srcLabel) + ' cover (' + esc(val) + ') · drag to copy">'
        + '<span class="meta-drag-grip" aria-hidden="true">⋮⋮</span>'
        + '<span class="meta-radio-dot"></span>'
        + '<img class="meta-cover-mini" src="' + esc(thumb) + '" alt="" loading="lazy">'
        + '<span class="meta-pill-text">' + esc(srcLabel) + ' cover</span></button></td>';
    }
    return '<td class="meta-choice-cell"' + dropAttrs + '>'
      + '<button type="button" class="meta-pill' + (chosen ? ' is-chosen' : '') + '"'
      + ' data-action="' + pickAct + '" data-file="' + fileItem.id + '" data-field="' + fieldKey + '" data-choice="' + srcKey + '"'
      + dragAttr + ' title="' + esc(srcLabel + ': ' + val + ' · click to select or drag to another parameter') + '">'
      + '<span class="meta-drag-grip" aria-hidden="true">⋮⋮</span>'
      + '<span class="meta-radio-dot"></span>'
      + '<span class="meta-pill-text">' + esc(val) + '</span></button></td>';
  }

  function resolveFinalMetaValue(fieldKey, fieldObj, isDiscogsChosen, isMbChosen, isOpenChosen) {
    const choice = (fieldObj && fieldObj.choice) || 'current';
    const curVal = (fieldObj && fieldObj.current) || '';
    const pathVal = (fieldObj && fieldObj.path) || '';
    const discogsVal = (fieldObj && fieldObj.discogs) || '';
    const mbVal = (fieldObj && fieldObj.musicbrainz) || '';
    const openVal = (fieldObj && fieldObj.open) || '';
    const onlineVal = (fieldObj && fieldObj.online) || discogsVal || mbVal || openVal || '';
    const customVal = (fieldObj && fieldObj.custom_value) || '';
    if (choice === 'custom') return { val: customVal, srcLabel: 'Custom', isOverride: true };
    if (isDiscogsChosen) return { val: discogsVal, srcLabel: 'Discogs', isOverride: false };
    if (isMbChosen) return { val: mbVal, srcLabel: 'MusicBrainz', isOverride: false };
    if (isOpenChosen) return { val: openVal, srcLabel: 'Open', isOverride: false };
    if (choice === 'online') return { val: onlineVal, srcLabel: 'Online', isOverride: false };
    if (choice === 'path') return { val: pathVal, srcLabel: 'File', isOverride: false };
    return { val: curVal, srcLabel: 'Tag', isOverride: false };
  }

  function renderMetaFieldCells(fileItem, fieldKey, fieldObj, isModal) {
    const choice = (fieldObj && fieldObj.choice) || 'current';
    const curVal = (fieldObj && fieldObj.current) || '';
    const pathVal = (fieldObj && fieldObj.path) || '';
    const discogsVal = (fieldObj && fieldObj.discogs) || '';
    const mbVal = (fieldObj && fieldObj.musicbrainz) || '';
    const openVal = (fieldObj && fieldObj.open) || '';
    const onlineVal = (fieldObj && fieldObj.online) || discogsVal || mbVal || openVal || '';
    const customVal = (fieldObj && fieldObj.custom_value) || '';
    const pathWarn = !!(fieldObj && fieldObj.path_vs_tag_diff);
    const pickAct = isModal ? 'meta-modal-pick' : 'meta-pick';
    const editToggleAct = isModal ? 'meta-modal-edit-toggle' : 'meta-edit-toggle';
    const saveCustomAct = isModal ? 'meta-modal-save-custom' : 'meta-save-custom';
    const editKey = (isModal ? 'modal:' : '') + fileItem.id + ':' + fieldKey;
    const isEditing = !!state.metaEditing[editKey];
    const fieldLabel = META_FIELD_MAP[fieldKey] || fieldKey;
    const dropAttrs = ' data-drop-file="' + fileItem.id + '" data-drop-field="' + fieldKey + '"'
      + (isModal ? ' data-drop-modal="1"' : '');

    // If legacy 'online' choice is active, highlight whichever specific source matches onlineVal
    const isDiscogsChosen = choice === 'discogs' || (choice === 'online' && !!discogsVal && discogsVal === onlineVal);
    const isMbChosen = choice === 'musicbrainz' || (choice === 'online' && !isDiscogsChosen && !!mbVal && mbVal === onlineVal);
    const isOpenChosen = choice === 'open' || (choice === 'online' && !isDiscogsChosen && !isMbChosen && !!openVal);

    // Col 2 (right after File & folder): unified Current tag / Selected (✎) — shows current tag by default,
    // or the chosen/custom value when modified, with inline override (✎) and 1-click revert (↺) to current tag
    const finalInfo = resolveFinalMetaValue(fieldKey, fieldObj, isDiscogsChosen, isMbChosen, isOpenChosen);
    const finalVal = finalInfo.val || '';
    const willChange = choice !== 'current' && (fieldKey === 'cover' ? !!finalVal : finalVal !== curVal);
    const editSeed = choice === 'custom'
      ? customVal
      : (fieldKey === 'cover' && choice === 'current' ? (onlineVal || pathVal || '') : finalVal);

    let customInner = '';
    if (isEditing) {
      customInner = '<span class="meta-edit-wrap">'
        + '<input type="text" class="meta-custom-input" data-meta-custom-input="' + editKey + '" value="' + esc(editSeed) + '" placeholder="' + esc(fieldKey === 'cover' ? 'Cover image URL…' : 'Override ' + fieldLabel) + '">'
        + '<button type="button" class="btn btn-sm btn-primary" data-action="' + saveCustomAct + '" data-file="' + fileItem.id + '" data-field="' + fieldKey + '">Set</button>'
        + '<button type="button" class="meta-edit-btn is-active" data-action="' + editToggleAct + '" data-file="' + fileItem.id + '" data-field="' + fieldKey + '" title="Cancel override edit">✕</button>'
        + '</span>';
    } else if (fieldKey === 'cover') {
      const hasCov = curVal === 'present' || !!fileItem.has_cover;
      let covUrl = '';
      let covText = '';
      if (choice === 'current') {
        covUrl = hasCov ? fileCoverUrl(fileItem.id, fileItem.cover_updated_at || fileItem.checked_at || 1) : '';
        covText = hasCov ? 'Present' : 'Missing';
      } else if (finalVal) {
        covUrl = candThumbUrl(fileItem.id, finalVal, fileItem.cover_updated_at || fileItem.checked_at);
        covText = choice === 'custom' ? 'Custom cover' : (finalInfo.srcLabel + ' cover');
      } else {
        covText = choice === 'custom' ? '(clear)' : 'Missing';
      }
      const cDrag = (choice !== 'current' && finalVal) ? makeDragAttr(fileItem.id, fieldKey, choice, finalVal, isModal) : '';
      const zoomCaption = choice === 'current'
        ? (fileItem.name + ' · Current embedded cover')
        : (fileItem.name + ' · Selected cover (' + finalInfo.srcLabel + ')');
      const zoomAttrs = covUrl ? ' data-cover-zoom="' + esc(covUrl) + '" data-cover-caption="' + esc(zoomCaption) + '"' : '';
      const covTip = choice === 'current'
        ? ((hasCov ? 'Current embedded cover: Present' : 'No cover currently embedded in file') + ' · click or press ✎ to override')
        : ('Selected cover (' + finalInfo.srcLabel + ') · Current in file: ' + (hasCov ? 'Present' : 'Missing') + ' · click or press ✎ to override');
      const revertBtn = choice !== 'current'
        ? '<button type="button" class="meta-edit-btn" data-action="' + pickAct + '" data-file="' + fileItem.id + '" data-field="' + fieldKey + '" data-choice="current" title="' + esc('Keep current file cover (' + (hasCov ? 'Present' : 'Missing') + ')') + '">↺</button>'
        : '';
      customInner = '<span class="meta-edit-wrap">'
        + '<button type="button" class="meta-pill' + (willChange || choice === 'custom' ? ' is-chosen' : '') + '"'
        + ' data-action="' + editToggleAct + '" data-file="' + fileItem.id + '" data-field="' + fieldKey + '"'
        + zoomAttrs + cDrag + ' title="' + esc(covTip) + '">'
        + (covUrl ? '<img class="meta-cover-mini" src="' + esc(covUrl) + '" alt="" loading="lazy">' : '')
        + '<span class="meta-pill-text">' + esc(covText) + '</span>'
        + '<span class="meta-final-src">' + esc(finalInfo.srcLabel) + '</span>'
        + '</button>'
        + revertBtn
        + '<button type="button" class="meta-edit-btn" data-action="' + editToggleAct + '" data-file="' + fileItem.id + '" data-field="' + fieldKey + '" title="Override ' + esc(fieldLabel) + ' value">✎</button>'
        + '</span>';
    } else {
      const fDrag = finalVal ? makeDragAttr(fileItem.id, fieldKey, choice, finalVal, isModal) : '';
      const dispHtml = finalVal
        ? esc(finalVal)
        : (choice === 'custom' ? '<em class="muted">(clear)</em>' : '<em class="muted">(empty)</em>');
      const showWarn = choice === 'current' && pathWarn;
      const pillCls = 'meta-pill'
        + (willChange || choice === 'custom' ? ' is-chosen' : '')
        + (showWarn ? ' is-warn' : '');
      const pillTip = choice === 'current'
        ? ((curVal ? 'Current tag: ' + curVal : 'Current tag is empty')
          + (pathWarn ? ' (differs from folder/file name)' : '')
          + ' · click or press ✎ to edit'
          + (curVal ? ', or drag to another parameter' : ''))
        : ('Selected ' + fieldLabel + ' (' + finalInfo.srcLabel + '): ' + (finalVal || '(empty)')
          + ' · Current tag in file: ' + (curVal || '(empty)')
          + ' · click or press ✎ to edit');
      const revertBtn = choice !== 'current'
        ? '<button type="button" class="meta-edit-btn" data-action="' + pickAct + '" data-file="' + fileItem.id + '" data-field="' + fieldKey + '" data-choice="current" title="' + esc('Revert to current file tag: ' + (curVal || '(empty)')) + '">↺</button>'
        : '';
      customInner = '<span class="meta-edit-wrap">'
        + '<button type="button" class="' + pillCls + '"'
        + ' data-action="' + editToggleAct + '" data-file="' + fileItem.id + '" data-field="' + fieldKey + '"'
        + fDrag + ' title="' + esc(pillTip) + '">'
        + (finalVal ? '<span class="meta-drag-grip" aria-hidden="true">⋮⋮</span>' : '')
        + '<span class="meta-pill-text">' + dispHtml + '</span>'
        + (showWarn ? '<span class="cover-override-tag" title="Current tag differs from folder/file name">⚠️</span>' : '')
        + '<span class="meta-final-src">' + esc(finalInfo.srcLabel) + '</span>'
        + '</button>'
        + revertBtn
        + '<button type="button" class="meta-edit-btn" data-action="' + editToggleAct + '" data-file="' + fileItem.id + '" data-field="' + fieldKey + '" title="Override ' + esc(fieldLabel) + ' value (or drop a dragged value here)">✎</button>'
        + '</span>';
    }
    const customCell = '<td class="meta-choice-cell"' + dropAttrs + '><div class="meta-custom-cell-row">' + customInner + '</div></td>';

    // Col 3: Field (also a drop target!)
    const fieldCell = '<td class="meta-field-name"' + dropAttrs
      + ' title="' + esc(fieldLabel + ' — drop any dragged value onto this row to set ' + fieldLabel) + '">'
      + esc(fieldLabel) + '</td>';

    // Col 4: Folder / file name
    let pathInner = '<span class="muted small">—</span>';
    if (fieldKey === 'cover') {
      if (pathVal) {
        const dragAttr = makeDragAttr(fileItem.id, fieldKey, 'path', pathVal, isModal);
        const lThumb = candThumbUrl(fileItem.id, pathVal, fileItem.cover_updated_at || fileItem.checked_at);
        pathInner = '<button type="button" class="meta-pill' + (choice === 'path' ? ' is-chosen' : '') + '"'
          + ' data-action="' + pickAct + '" data-file="' + fileItem.id + '" data-field="' + fieldKey + '" data-choice="path"'
          + ' data-cover-zoom="' + esc(lThumb) + '" data-cover-caption="' + esc(fileItem.name + ' · Local folder cover') + '"'
          + dragAttr + ' title="Use local folder cover image · drag to copy">'
          + '<span class="meta-drag-grip" aria-hidden="true">⋮⋮</span>'
          + '<span class="meta-radio-dot"></span>'
          + '<img class="meta-cover-mini" src="' + esc(lThumb) + '" alt="" loading="lazy">'
          + '<span class="meta-pill-text">Local cover</span></button>';
      }
    } else if (pathVal) {
      const dragAttr = makeDragAttr(fileItem.id, fieldKey, 'path', pathVal, isModal);
      pathInner = '<button type="button" class="meta-pill' + (choice === 'path' ? ' is-chosen' : '') + '"'
        + ' data-action="' + pickAct + '" data-file="' + fileItem.id + '" data-field="' + fieldKey + '" data-choice="path"'
        + dragAttr + ' title="' + esc('Folder/file name: ' + pathVal + ' · click to select or drag to another parameter') + '">'
        + '<span class="meta-drag-grip" aria-hidden="true">⋮⋮</span>'
        + '<span class="meta-radio-dot"></span>'
        + '<span class="meta-pill-text">' + esc(pathVal) + '</span></button>';
    }
    const pathCell = '<td class="meta-choice-cell"' + dropAttrs + '>' + pathInner + '</td>';

    // Cols 5, 6, 7: Discogs, MusicBrainz, Open (iTunes)
    const discogsCell = renderOnlineSourceCell(fileItem, fieldKey, 'discogs', 'Discogs', discogsVal, isDiscogsChosen, pickAct, isModal);
    const mbCell = renderOnlineSourceCell(fileItem, fieldKey, 'musicbrainz', 'MusicBrainz', mbVal, isMbChosen, pickAct, isModal);
    const openCell = renderOnlineSourceCell(fileItem, fieldKey, 'open', 'Open (iTunes)', openVal, isOpenChosen, pickAct, isModal);

    return customCell + fieldCell + pathCell + discogsCell + mbCell + openCell;
  }

  function filterAndSortMetaFiles(files, opts) {
    const enabledFields = (opts && opts.fields) || META_FIELDS.map((x) => x[0]);
    const stFilter = state.metaFilterStatus || 'diff';
    const fldFilter = state.metaFilterField || 'all';
    const dirFilter = state.metaFilterFolder || 'all';
    const q = (state.metaSearch || '').trim().toLowerCase();

    const filtered = (files || []).filter((f) => {
      if (dirFilter !== 'all' && f.dir !== dirFilter) return false;
      const touched = state.metaTouchedFiles && state.metaTouchedFiles.has(f.id);
      if (stFilter === 'diff' && !f.diff_count && f.status !== 'diff' && !touched) return false;
      if (stFilter === 'path_diff' && !f.path_diff_count && !touched) return false;
      if (stFilter === 'missing') {
        const hasMissing = enabledFields.some((k) => f.fields && f.fields[k] && f.fields[k].missing);
        if (!hasMissing && !touched) return false;
      }
      if (fldFilter !== 'all') {
        const fo = f.fields && f.fields[fldFilter];
        if (!fo || (!fo.diff && stFilter !== 'all' && !touched)) return false;
      }
      if (q) {
        const hay = [
          f.name, f.relpath, f.dir,
          f.fields && f.fields.artist && (f.fields.artist.current || f.fields.artist.path || f.fields.artist.online),
          f.fields && f.fields.album && (f.fields.album.current || f.fields.album.path || f.fields.album.online),
          f.fields && f.fields.title && (f.fields.title.current || f.fields.title.path || f.fields.title.online),
        ].filter(Boolean).join(' ').toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });

    const sortMode = state.metaSort || 'folder';
    filtered.sort((a, b) => {
      if (sortMode === 'name') return String(a.name).localeCompare(String(b.name), 'en');
      if (sortMode === 'diffs_desc') return (b.diff_count - a.diff_count) || String(a.relpath).localeCompare(String(b.relpath), 'en');
      if (sortMode === 'path_first') return (b.path_diff_count - a.path_diff_count) || (b.diff_count - a.diff_count) || String(a.relpath).localeCompare(String(b.relpath), 'en');
      return String(a.relpath).localeCompare(String(b.relpath), 'en');
    });
    return filtered;
  }

  function getBulkFieldDraft(k) {
    if (!state.metaBulkFields[k]) {
      state.metaBulkFields[k] = { enabled: false, mode: 'custom', value: '', dirty: false };
    }
    return state.metaBulkFields[k];
  }

  function getEnabledVisibleMetaFields() {
    const opts = (state.metadata && state.metadata.options) || {};
    const enabled = Array.isArray(opts.fields) && opts.fields.length ? opts.fields : ALL_META_FIELD_KEYS;
    let keys = ALL_META_FIELD_KEYS.filter((k) => enabled.includes(k) && state.metaVisibleFields.includes(k));
    if (!keys.length) keys = ALL_META_FIELD_KEYS.filter((k) => state.metaVisibleFields.includes(k));
    if (!keys.length) keys = ALL_META_FIELD_KEYS.slice();
    if (state.metaFilterField && state.metaFilterField !== 'all') {
      keys = keys.filter((k) => k === state.metaFilterField);
    }
    return keys;
  }

  function ensureMetaFieldsVisible(fieldKeys) {
    if (!fieldKeys || !fieldKeys.length) return;
    let visChanged = false;
    fieldKeys.forEach((k) => {
      if (!state.metaVisibleFields.includes(k)) {
        state.metaVisibleFields.push(k);
        visChanged = true;
      }
      if (!state.metaForceShowFields.includes(k)) {
        state.metaForceShowFields.push(k);
      }
    });
    if (visChanged) {
      state.metaVisibleFields = ALL_META_FIELD_KEYS.filter((k) => state.metaVisibleFields.includes(k));
      saveStoredMetaVis(state.metaVisibleFields);
    }
    if (state.metaFilterField !== 'all' && !fieldKeys.includes(state.metaFilterField)) {
      state.metaFilterField = 'all';
    }
  }

  function buildBulkValuesMap(keys) {
    const out = {};
    keys.forEach((k) => {
      const bf = getBulkFieldDraft(k);
      out[k] = {
        choice: bf.mode || 'custom',
        value: bf.mode === 'custom' ? (bf.value || '') : '',
      };
    });
    return out;
  }

  function syncBulkPanelDom() {
    const enabledCount = META_FIELDS.filter(([k]) => getBulkFieldDraft(k).enabled).length;
    const selCount = state.metaSel.length;
    META_FIELDS.forEach(([k]) => {
      const bf = getBulkFieldDraft(k);
      const chk = document.querySelector('input[data-meta-bulk-enable="' + k + '"]');
      if (chk && chk.checked !== !!bf.enabled) chk.checked = !!bf.enabled;
      const item = chk && chk.closest('.meta-bulk-item');
      if (item) item.classList.toggle('is-enabled', !!bf.enabled);
    });
    const stageBtn = document.querySelector('[data-action="meta-bulk-stage"]');
    if (stageBtn) {
      stageBtn.disabled = !selCount || !enabledCount;
      stageBtn.textContent = 'Apply bulk edit (' + selCount + ')';
    }
    const resetBtn = document.querySelector('[data-action="meta-bulk-reset"]');
    if (resetBtn) resetBtn.disabled = !enabledCount;
  }

  function renderMetaBulkPanel(selIds, visibleFiles, isChecking) {
    const targetIds = selIds.length ? selIds : [];
    const enabledCount = META_FIELDS.filter(([k]) => getBulkFieldDraft(k).enabled).length;
    const rowsHtml = META_FIELDS.map(([k, lbl]) => {
      const bf = getBulkFieldDraft(k);
      const modeOpts = [
        ['custom', 'Custom value'],
        ['discogs', 'From Discogs'],
        ['musicbrainz', 'From MusicBrainz'],
        ['open', 'From Open (iTunes)'],
        ['path', 'From Folder / File'],
        ['current', 'Keep Current tag'],
      ].map(([mk, ml]) => '<option value="' + mk + '"' + (bf.mode === mk ? ' selected' : '') + '>' + ml + '</option>').join('');
      const inpDisabled = bf.mode !== 'custom';
      return '<div class="meta-bulk-item' + (bf.enabled ? ' is-enabled' : '') + '">'
        + '<label class="inline" style="min-width:0;overflow:hidden"><input type="checkbox" data-meta-bulk-enable="' + k + '"' + (bf.enabled ? ' checked' : '') + '> '
        + '<span class="meta-one-line" title="' + esc(lbl) + '">' + esc(lbl) + '</span></label>'
        + '<select class="input" data-meta-bulk-mode="' + k + '" aria-label="Source for ' + esc(lbl) + '">' + modeOpts + '</select>'
        + '<input type="text" class="input" data-meta-bulk-val="' + k + '" value="' + esc(bf.value) + '"'
        + (inpDisabled ? ' disabled placeholder="Uses ' + esc(bf.mode) + '"' : ' placeholder="' + esc(k === 'cover' ? 'Image URL (or drop pill)…' : 'Set ' + lbl + ' (or drop pill)…') + '"')
        + '>'
        + '</div>';
    }).join('');

    return '<div class="meta-bulk-panel">'
      + '<div class="meta-bulk-head">'
      + '<div><strong>Bulk edit parameters</strong>'
      + ' <span class="muted small">· Edit parameters for <strong>'
      + (targetIds.length ? targetIds.length + ' selected file' + (targetIds.length === 1 ? '' : 's') : '0 selected files (tick files in column 1 or click Select all)')
      + '</strong>: press <strong>Apply bulk edit (' + targetIds.length + ')</strong> to select the updated parameter(s) in the list, then press <strong>Update Selected (' + targetIds.length + ')</strong> to write ID3 tags into the actual file(s) (preserving timestamps).</span></div>'
      + '<div class="btns">'
      + (targetIds.length < visibleFiles.length && visibleFiles.length
        ? '<button type="button" class="btn btn-sm" data-action="meta-select-all-shown">Select all (' + visibleFiles.length + ')</button>'
        : '')
      + '<button type="button" class="btn btn-sm btn-primary" data-action="meta-bulk-stage"'
      + ((!targetIds.length || !enabledCount) ? ' disabled' : '')
      + ' title="Apply the checked parameter edits and select the updated parameter values in the list below">'
      + 'Apply bulk edit (' + targetIds.length + ')</button>'
      + '<button type="button" class="btn btn-sm btn-primary" data-action="meta-apply-selected"'
      + (!targetIds.length ? ' disabled' : '')
      + ' title="Write selected ID3 tags and cover art into the actual audio files on disk (file date/time stamp stays the same)">'
      + 'Update Selected (' + targetIds.length + ')</button>'
      + '<button type="button" class="btn btn-sm" data-action="meta-bulk-reset"'
      + (!enabledCount ? ' disabled' : '')
      + ' title="Clear the bulk edit parameter fields">Reset bulk</button>'
      + '<button type="button" class="btn btn-sm" data-action="meta-bulk-toggle">Hide</button>'
      + '</div></div>'
      + '<div class="meta-bulk-grid">' + rowsHtml + '</div>'
      + '</div>';
  }

  function renderMetadata() {
    const m = state.metadata || {
      options: { fields: META_FIELDS.map((x) => x[0]), check_path_vs_tags: true, sources: ['discogs', 'musicbrainz', 'open'] },
      meta_checking: false,
      enabled_folders: 0, total_folders: 0, total_files_in_scope: 0,
      checked_count: 0, diff_count: 0, path_diff_count: 0, folders: [], files: [],
    };
    const opts = m.options || { fields: META_FIELDS.map((x) => x[0]), check_path_vs_tags: true, sources: ['discogs', 'musicbrainz', 'open'] };
    const enabledFields = opts.fields || META_FIELDS.map((x) => x[0]);
    const enabledSources = opts.sources || ['discogs', 'musicbrainz', 'open'];
    const searchFrom = opts.search_from || opts.searchFrom || 'auto';
    const isChecking = !!(state.metaChecking || m.meta_checking || (state.dash && state.dash.meta_checking));
    const mp = m.meta_progress || (state.dash && state.dash.meta_progress) || {};
    const mpDone = Number(mp.done || 0);
    const mpTotal = Number(mp.total || 0);
    const mpPct = mpTotal > 0 ? Math.min(100, Math.round((mpDone / mpTotal) * 100)) : 0;

    const visibleFiles = filterAndSortMetaFiles(m.files || [], opts);
    const visIds = visibleFiles.map((f) => f.id);
    const sel = state.metaSel.filter((id) => visIds.includes(id));
    state.metaSel = sel;

    const headBtns = '<div class="btns">'
      + '<button class="btn' + (isChecking ? ' is-busy' : '') + '" data-action="meta-check-run"' + (isChecking ? ' disabled' : '')
      + ' title="Check metadata for audio files in enabled folders">'
      + (isChecking ? 'Checking…' : 'Check meta data') + '</button>'
      + '<button class="btn" data-action="meta-check-force"' + (isChecking ? ' disabled' : '')
      + ' title="Re-check all audio files in enabled folders against online sources">'
      + 'Re-check all</button>'
      + '<button class="btn' + (isChecking ? ' btn-danger' : '') + '" data-action="meta-check-stop"' + (isChecking ? '' : ' disabled')
      + ' title="Stop the running metadata check immediately">'
      + 'Stop</button>'
      + '<button class="btn btn-primary" data-action="meta-apply-selected"' + (sel.length ? '' : ' disabled')
      + ' title="Update ID3 tags and cover art in the selected audio files on disk (file date/time stamp stays the same)">'
      + 'Update Selected (' + sel.length + ')</button>'
      + '</div>';

    const fieldChecks = META_FIELDS.map(([k, lbl]) => {
      const on = enabledFields.includes(k);
      return '<label class="inline"><input type="checkbox" data-meta-opt-field="' + k + '"' + (on ? ' checked' : '') + '> ' + esc(lbl) + '</label>';
    }).join('');

    const sourceChecks = META_SOURCES.map(([k, lbl]) => {
      const on = enabledSources.includes(k);
      return '<label class="inline"><input type="checkbox" data-meta-opt-source="' + k + '"' + (on ? ' checked' : '') + '> ' + esc(lbl) + '</label>';
    }).join('');

    const liveProgressBanner = isChecking
      ? '<div class="meta-live-progress" id="meta-live-progress" role="status" aria-live="polite">'
        + '<div class="meta-live-progress-top">'
        + '<span id="meta-live-progress-text"><span class="working" aria-hidden="true"></span> '
        + '<strong>' + (mp.force ? 'Re-checking all metadata' : 'Checking metadata') + ':</strong> '
        + (mpTotal > 0 ? fmt(mpDone) + ' / ' + fmt(mpTotal) + ' files (' + mpPct + '%)' : 'starting…')
        + (mpDone > 0 ? ' · <strong>' + fmt(mp.diff || 0) + '</strong> with differences · ' + fmt(mp.ok || 0) + ' matching' : '')
        + (mp.current_file ? ' · <span class="muted">Current: ' + esc(mp.current_file) + '</span>' : '')
        + '</span>'
        + '<span class="small muted">Intermediate results appear live in section 2 below</span>'
        + '</div>'
        + '<div class="meta-live-progress-track"><div class="meta-live-progress-fill" id="meta-live-progress-fill" style="width:' + (mpTotal > 0 ? Math.max(2, mpPct) : 8) + '%"></div></div>'
        + '</div>'
      : '';

    const optionsCard = '<section class="card" style="margin-bottom:16px">'
      + '<div class="card-head"><div><h2>1 · Scope &amp; check options'
      + tip('Checks audio files inside the folders enabled on the Directories tab. Updating metadata preserves the original file modification timestamp (mtime).')
      + '</h2>'
      + '<div class="sub">'
      + fmt(m.enabled_folders) + ' of ' + fmt(m.total_folders) + ' audio folders enabled (<a href="#/directories">Directories tab</a>) · '
      + fmt(m.total_files_in_scope) + ' audio files in scope · '
      + fmt(m.checked_count) + ' checked · '
      + '<strong>' + fmt(m.diff_count) + '</strong> with differences'
      + (m.path_diff_count ? ' · <span class="warn-text">' + fmt(m.path_diff_count) + ' folder/file name mismatch' + (m.path_diff_count === 1 ? '' : 'es') + '</span>' : '')
      + (isChecking ? ' · <span class="scan-note">checking metadata… (intermediate results shown live below)</span>' : '')
      + '</div></div></div>'
      + '<div class="meta-options-row"><span class="muted small" style="min-width:120px">Fields to check</span>'
      + fieldChecks + '</div>'
      + '<div class="meta-options-row"><span class="muted small" style="min-width:120px">Folder / file check</span>'
      + '<label class="inline"><input class="switch" type="checkbox" data-meta-opt-path' + (opts.check_path_vs_tags ? ' checked' : '') + '> '
      + 'Compare <strong>Artist</strong>, <strong>Title</strong> and <strong>Album</strong> in file tags against the folder / file name (flag differences and use folder/file name for online search when totally different)</label>'
      + '</div>'
      + '<div class="meta-options-row"><span class="muted small" style="min-width:120px">Artist / Title from</span>'
      + '<div class="seg" role="group" aria-label="Take Artist and Title from">'
      + '<button type="button" class="' + (searchFrom === 'auto' ? 'on' : '') + '" data-action="meta-opt-search-from" data-mode="auto" aria-pressed="' + (searchFrom === 'auto') + '" title="Smart automatic: use metadata tags unless missing or totally different from folder/file name">Auto (smart)</button>'
      + '<button type="button" class="' + (searchFrom === 'file' ? 'on' : '') + '" data-action="meta-opt-search-from" data-mode="file" aria-pressed="' + (searchFrom === 'file') + '" title="Always take Artist / Title (and Album) from the folder / file name when searching online">From file (folder / file name)</button>'
      + '<button type="button" class="' + (searchFrom === 'metadata' ? 'on' : '') + '" data-action="meta-opt-search-from" data-mode="metadata" aria-pressed="' + (searchFrom === 'metadata') + '" title="Always take Artist / Title (and Album) from the file embedded metadata tags when searching online">From meta data (file tags)</button>'
      + '</div>'
      + '<span class="small muted">Controls where online lookups take <strong>Artist / Title</strong> from.</span>'
      + '</div>'
      + '<div class="meta-options-row"><span class="muted small" style="min-width:120px">Internet sources</span>'
      + sourceChecks
      + '<span class="small muted">Configure your Discogs token in <a href="#/settings">Settings → General</a>.</span>'
      + '</div>'
      + liveProgressBanner
      + '</section>';

    const statusOpts = [
      ['diff', 'With differences (' + fmt(m.diff_count) + ')'],
      ['path_diff', 'Tag vs. Folder/File mismatch ⚠️ (' + fmt(m.path_diff_count) + ')'],
      ['missing', 'Missing tags'],
      ['all', 'All checked files (' + fmt(m.checked_count) + ')'],
    ].map(([k, lbl]) => '<option value="' + k + '"' + (state.metaFilterStatus === k ? ' selected' : '') + '>' + lbl + '</option>').join('');

    const fieldFilterOpts = [['all', 'All checked fields']].concat(META_FIELDS)
      .map(([k, lbl]) => '<option value="' + k + '"' + (state.metaFilterField === k ? ' selected' : '') + '>' + esc(lbl) + '</option>').join('');

    const folderFilterOpts = [['all', 'All enabled folders']]
      .concat((m.folders || []).map((d) => [d, folderLabel(d)]))
      .map(([k, lbl]) => '<option value="' + esc(k) + '"' + (state.metaFilterFolder === k ? ' selected' : '') + '>' + esc(lbl) + '</option>').join('');

    const sortOpts = [
      ['folder', 'Folder / Album order'],
      ['name', 'File name (A–Z)'],
      ['diffs_desc', 'Most differences first'],
      ['path_first', 'Folder/file mismatch first'],
    ].map(([k, lbl]) => '<option value="' + k + '"' + (state.metaSort === k ? ' selected' : '') + '>' + lbl + '</option>').join('');

    const filterBar = '<div class="meta-filter-bar">'
      + '<div class="field"><label class="lbl" for="meta-flt-status">Show</label>'
      + '<select class="input" id="meta-flt-status" data-meta-filter="status">' + statusOpts + '</select></div>'
      + '<div class="field"><label class="lbl" for="meta-flt-field">Field</label>'
      + '<select class="input" id="meta-flt-field" data-meta-filter="field">' + fieldFilterOpts + '</select></div>'
      + '<div class="field"><label class="lbl" for="meta-flt-folder">Folder</label>'
      + '<select class="input" id="meta-flt-folder" data-meta-filter="folder">' + folderFilterOpts + '</select></div>'
      + '<div class="field"><label class="lbl" for="meta-flt-search">Filter text</label>'
      + '<input class="input" id="meta-flt-search" data-meta-filter="search" value="' + esc(state.metaSearch) + '" placeholder="Artist, album, title or file…"></div>'
      + '<div class="field"><label class="lbl" for="meta-flt-sort">Sort by</label>'
      + '<select class="input" id="meta-flt-sort" data-meta-filter="sort">' + sortOpts + '</select></div>'
      + '</div>';

    const visChecks = META_FIELDS.map(([k, lbl]) => {
      const on = state.metaVisibleFields.includes(k);
      return '<label class="inline"><input type="checkbox" data-meta-vis-field="' + k + '"' + (on ? ' checked' : '') + '> ' + esc(lbl) + '</label>';
    }).join('');

    const visBar = '<div class="meta-vis-bar">'
      + '<span class="meta-vis-title">Parameters shown in list:</span>'
      + visChecks
      + '<span class="grow"></span>'
      + '<label class="inline small muted" title="When enabled, only parameter rows with detected differences are shown for each file"><input type="checkbox" data-meta-only-diff' + (state.metaOnlyDiffRows ? ' checked' : '') + '> Only diff rows</label>'
      + '<button type="button" class="btn btn-sm" data-action="meta-vis-all" title="Show all parameters in the list">All parameters</button>'
      + '<button type="button" class="btn btn-sm" data-action="meta-cols-reset" title="Reset column widths to default">Reset column widths</button>'
      + '</div>';

    const selCount = sel.length;
    const bulkSrcDisabled = !selCount ? ' disabled' : '';
    const bulkBar = '<div class="park-bar" style="margin-bottom:12px">'
      + '<span><strong>' + selCount + ' of ' + visibleFiles.length + ' selected</strong>'
      + ' · Select source for enabled (visible) parameters on selected files:</span>'
      + '<div class="btns">'
      + '<button class="btn btn-sm" data-action="meta-bulk-choice" data-choice="path"' + bulkSrcDisabled + ' title="Use Folder / File values for enabled (visible) parameters on the ' + selCount + ' selected file(s)">Folder / File (' + selCount + ')</button>'
      + '<button class="btn btn-sm" data-action="meta-bulk-choice" data-choice="current"' + bulkSrcDisabled + ' title="Use Current tag values for enabled (visible) parameters on the ' + selCount + ' selected file(s)">Current tag (' + selCount + ')</button>'
      + '<button class="btn btn-sm" data-action="meta-bulk-artist-title" data-choice="path"' + bulkSrcDisabled + ' title="Take Artist and Track Title from the folder / file name on the ' + selCount + ' selected file(s)">Artist/Title ← File (' + selCount + ')</button>'
      + '<button class="btn btn-sm" data-action="meta-bulk-artist-title" data-choice="current"' + bulkSrcDisabled + ' title="Keep Artist and Track Title from the file meta data tags on the ' + selCount + ' selected file(s)">Artist/Title ← Meta data (' + selCount + ')</button>'
      + '<button class="btn btn-sm' + (state.metaBulkOpen ? ' btn-primary' : '') + '" data-action="meta-bulk-toggle" aria-expanded="' + state.metaBulkOpen + '">'
      + 'Bulk edit parameters (' + selCount + ')</button>'
      + '<button class="btn btn-sm btn-primary" data-action="meta-apply-selected"' + bulkSrcDisabled
      + ' title="Update ID3 tags and cover art in the actual audio file(s) on disk (time/date stamp stays the same)">'
      + 'Update Selected (' + selCount + ')</button>'
      + (selCount < visibleFiles.length && visibleFiles.length
        ? '<button class="btn btn-sm" data-action="meta-select-all-shown">Select all (' + visibleFiles.length + ')</button>'
        : '')
      + (selCount
        ? '<button class="btn btn-sm" data-action="meta-clear-sel">Clear selection</button>'
        : '')
      + '</div></div>';

    const bulkPanelHtml = state.metaBulkOpen
      ? renderMetaBulkPanel(sel, visibleFiles, isChecking)
      : '';

    let tableHtml = '';
    if (!m.checked_count) {
      tableHtml = isChecking
        ? '<p class="muted"><span class="working" aria-hidden="true"></span> Checking first audio file… intermediate results will appear here automatically as each file finishes.</p>'
        : '<p class="muted">No files have been checked for metadata differences yet. Press <strong>Check meta data</strong> above to compare your enabled audio folders against folder/file names and internet sources.</p>';
    } else if (!visibleFiles.length) {
      tableHtml = isChecking
        ? '<p class="muted"><span class="working" aria-hidden="true"></span> ' + fmt(m.checked_count) + ' file' + (m.checked_count === 1 ? '' : 's') + ' checked so far — none match the current filter yet (switch <strong>Show</strong> to <em>All checked files</em> to watch every checked file live).</p>'
        : '<p class="muted">No files match the current filter. Change the <strong>Show</strong> filter above to <em>All checked files</em> or press <strong>Check meta data</strong>.</p>';
    } else {
      const allOn = visibleFiles.length > 0 && visibleFiles.every((f) => sel.includes(f.id));
      const allowedListFields = META_FIELDS.map((x) => x[0]).filter((k) => state.metaVisibleFields.includes(k));
      const activeListFields = allowedListFields.length ? allowedListFields : META_FIELDS.map((x) => x[0]);

      const bodyRows = [];
      visibleFiles.forEach((f) => {
        const on = sel.includes(f.id);
        const fFields = f.fields || {};
        let rowFields = activeListFields.filter((k) => {
          if (state.metaFilterField !== 'all' && k !== state.metaFilterField) return false;
          if (state.metaForceShowFields.includes(k)) return true;
          const fo = fFields[k];
          if (!fo) return true;
          if (!state.metaOnlyDiffRows) return true;
          if (state.metaFilterStatus === 'all') return true;
          if (state.metaFilterStatus === 'path_diff') return !!fo.path_vs_tag_diff || !!fo.diff;
          return !!fo.diff;
        });
        if (!rowFields.length) {
          rowFields = activeListFields.filter((k) => state.metaFilterField === 'all' || k === state.metaFilterField);
        }
        if (!rowFields.length) rowFields = [activeListFields[0]];

        const span = Math.max(1, rowFields.length);
        rowFields.forEach((k, idx) => {
          const fo = fFields[k] || { current: '', path: '', discogs: '', musicbrainz: '', open: '', online: '', choice: 'current', custom_value: '' };
          const firstRow = idx === 0;
          const trCls = [firstRow ? 'meta-file-group-start' : '', on ? 'row-sel' : ''].filter(Boolean).join(' ');
          const srcTag = f.online_source
            ? '<span class="tag" style="margin-left:0">' + esc(f.online_source) + (f.online_score ? ' · ' + Math.round(f.online_score * 100) + '%' : '') + '</span>'
            : '<span class="muted small">No online match</span>';
          const overrideBadge = f.query_source === 'path_override'
            ? '<span class="cover-override-tag" title="Tags were totally different from folder/file name; folder/file name was used">Folder/file used</span>'
            : '';
          const folderStr = folderLabel(f.dir);
          const fileCols = firstRow
            ? '<td class="pick meta-pick-col" rowspan="' + span + '"><input type="checkbox" data-meta-check="' + f.id + '" aria-label="Select ' + esc(f.name) + '"' + (on ? ' checked' : '') + '></td>'
              + '<td class="meta-file-col" rowspan="' + span + '">'
              + '<div class="meta-file-cell-inner">'
              + '<strong class="meta-file-name" title="' + esc(f.relpath) + '">' + esc(f.name) + '</strong>'
              + '<span class="small muted mono meta-folder-name" title="' + esc(f.dir) + '">' + esc(folderStr) + '</span>'
              + '<div class="meta-badges-line">' + srcTag + overrideBadge + '</div>'
              + '<div class="meta-row-actions">'
              + '<button type="button" class="btn btn-sm btn-primary" data-action="meta-apply-one" data-file="' + f.id + '" title="Write selected metadata to this file without changing its timestamp">Update</button>'
              + '<button type="button" class="btn btn-sm" data-action="open-meta-by-id" data-file="' + f.id + '" title="Open full metadata popup for this file">Popup</button>'
              + '</div>'
              + '</div>'
              + '</td>'
            : '';
          bodyRows.push(
            '<tr' + (trCls ? ' class="' + trCls + '"' : '') + ' data-meta-row-file="' + f.id + '" data-meta-row-field="' + k + '">'
            + fileCols
            + renderMetaFieldCells(f, k, fo, false)
            + '</tr>'
          );
        });
      });

      const colsTotalWidth = META_COL_SPECS.reduce((acc, [ck]) => acc + (state.metaColWidths[ck] || DEFAULT_META_COLS[ck]), 0);
      const colGroupHtml = '<colgroup>' + META_COL_SPECS.map(([ck]) => {
        const w = state.metaColWidths[ck] || DEFAULT_META_COLS[ck];
        return '<col data-col-key="' + ck + '" style="width:' + w + 'px">';
      }).join('') + '</colgroup>';

      const resizer = (ck) => '<span class="col-resizer" data-resize-col="' + ck + '" title="Drag to resize column (double-click to reset)"></span>';

      tableHtml = '<div class="table-wrap" id="meta-table-wrap"><table class="meta-table meta-table-resizable" id="meta-diff-table" style="width:' + colsTotalWidth + 'px;min-width:100%">'
        + colGroupHtml
        + '<thead><tr>'
        + '<th class="pick"><input type="checkbox" data-meta-all aria-label="Select all visible files"' + (allOn ? ' checked' : '') + '></th>'
        + '<th class="is-resizable" title="File &amp; folder">File &amp; folder' + resizer('file') + '</th>'
        + '<th class="is-resizable" title="Current tag / Selected (✎)">Current tag / Selected (✎)' + tip('Shows the current file tag by default, or the selected/custom value to write. Click the value or ✎ to edit, press ↺ to revert to the current tag, or drop a dragged value here.') + resizer('custom') + '</th>'
        + '<th class="is-resizable" title="Field">Field' + tip('Metadata parameter name. Drop any dragged value pill onto a row to assign it to that parameter.') + resizer('field') + '</th>'
        + '<th class="is-resizable" title="Folder / file name">Folder / file name' + tip('Value parsed from the folder and file name on disk. Click to select, or drag onto another parameter.') + resizer('path') + '</th>'
        + '<th class="is-resizable" title="Discogs"><div class="meta-th-source"><span class="meta-th-title">Discogs' + tip('Match found on Discogs. Click the button to use Discogs values for enabled (visible) parameters on selected files.') + '</span><button type="button" class="btn btn-sm meta-th-btn" data-action="meta-bulk-choice" data-choice="discogs"' + bulkSrcDisabled + ' title="Use Discogs column values for enabled (visible) parameters on the ' + selCount + ' selected file(s)">Discogs (' + selCount + ')</button></div>' + resizer('discogs') + '</th>'
        + '<th class="is-resizable" title="MusicBrainz"><div class="meta-th-source"><span class="meta-th-title">MusicBrainz' + tip('Match found on MusicBrainz / Cover Art Archive. Click the button to use MusicBrainz values for enabled (visible) parameters on selected files.') + '</span><button type="button" class="btn btn-sm meta-th-btn" data-action="meta-bulk-choice" data-choice="musicbrainz"' + bulkSrcDisabled + ' title="Use MusicBrainz column values for enabled (visible) parameters on the ' + selCount + ' selected file(s)">MusicBrainz (' + selCount + ')</button></div>' + resizer('musicbrainz') + '</th>'
        + '<th class="is-resizable" title="Open (iTunes)"><div class="meta-th-source"><span class="meta-th-title">Open (iTunes)' + tip('Match found on Open (iTunes). Click the button to use Open (iTunes) values for enabled (visible) parameters on selected files.') + '</span><button type="button" class="btn btn-sm meta-th-btn" data-action="meta-bulk-choice" data-choice="open"' + bulkSrcDisabled + ' title="Use Open (iTunes) column values for enabled (visible) parameters on the ' + selCount + ' selected file(s)">Open (iTunes) (' + selCount + ')</button></div>' + resizer('open') + '</th>'
        + '</tr></thead><tbody>' + bodyRows.join('') + '</tbody></table></div>';
    }

    const diffCard = '<section class="card">'
      + '<div class="card-head"><div><h2>2 · Metadata differences &amp; selection'
      + tip('Click any pill to select which value to use, drag any value pill onto another parameter row to copy it there, resize columns via header dividers, or use Bulk edit parameters for selected files.')
      + '</h2>'
      + '<div class="sub">Click a pill to choose it · drag <strong>⋮⋮</strong> pills to copy values across parameters · drag column borders to resize · <strong>Update</strong> writes without changing file timestamps.</div></div></div>'
      + filterBar
      + visBar
      + bulkBar
      + bulkPanelHtml
      + tableHtml
      + '</section>';

    app().innerHTML = pageHead(
      'Meta data',
      'Check and compare audio file tags in enabled folders against folder/file names, Discogs, MusicBrainz and open sources. File timestamps are preserved.',
      headBtns,
      false,
      'page-head-static'
    ) + optionsCard + diffCard;
    const tw = document.getElementById('meta-table-wrap');
    if (tw && state.metaTableScroll) {
      tw.scrollTop = state.metaTableScroll.top || 0;
      tw.scrollLeft = state.metaTableScroll.left || 0;
    }
    syncMetaStickyHeader();
  }

  /* ------------------------------------------------ per-file metadata modal */
  const META_MODAL = {
    open: false,
    fileId: null,
    name: '',
    relpath: '',
    dir: '',
    albumFileCount: 1,
    queryMode: 'auto',
    artist: '',
    album: '',
    title: '',
    item: null,
    loading: false,
    busy: false,
    error: '',
  };

  function renderMetaModal() {
    const modal = document.getElementById('metadata-modal');
    if (!modal || !META_MODAL.open) return;
    const titleEl = document.getElementById('metadata-modal-title');
    const subEl = document.getElementById('metadata-modal-sub');
    const bodyEl = document.getElementById('metadata-modal-body');
    const btnFile = document.getElementById('meta-modal-apply-file');
    const btnAlbum = document.getElementById('meta-modal-apply-album');

    titleEl.textContent = 'Meta data check — ' + (META_MODAL.name || 'File');
    subEl.textContent = MEDIA.audio.root + '/' + (META_MODAL.relpath || '');

    const mf = (META_MODAL.item && META_MODAL.item.fields) || {};
    const mPathPreview = [(mf.artist && mf.artist.path) || '', (mf.title && mf.title.path) || ''].filter(Boolean).join(' — ') || 'none';
    const mTagPreview = [(mf.artist && mf.artist.current) || '', (mf.title && mf.title.current) || ''].filter(Boolean).join(' — ') || 'none';
    const qm = META_MODAL.queryMode || 'auto';
    const querySrcBar = '<div class="query-src-bar">'
      + '<span class="muted small">Take Artist / Title from:</span>'
      + '<div class="seg" role="group" aria-label="Take Artist and Title from">'
      + '<button type="button" class="' + (qm === 'auto' ? 'on' : '') + '" data-action="meta-modal-query-mode" data-mode="auto"' + (META_MODAL.loading ? ' disabled' : '') + '>Auto</button>'
      + '<button type="button" class="' + (qm === 'file' ? 'on' : '') + '" data-action="meta-modal-query-mode" data-mode="file"' + (META_MODAL.loading ? ' disabled' : '') + ' title="File: ' + esc(mPathPreview) + '">From file</button>'
      + '<button type="button" class="' + (qm === 'metadata' ? 'on' : '') + '" data-action="meta-modal-query-mode" data-mode="metadata"' + (META_MODAL.loading ? ' disabled' : '') + ' title="Meta data: ' + esc(mTagPreview) + '">From meta data</button>'
      + '</div>'
      + (META_MODAL.item
        ? '<span class="small muted query-src-preview"><span><strong>File:</strong> ' + esc(mPathPreview) + '</span><span>·</span><span><strong>Meta data:</strong> ' + esc(mTagPreview) + '</span></span>'
        : '')
      + '</div>';

    const searchHtml = querySrcBar + '<div class="cover-search-grid" style="grid-template-columns: repeat(3, minmax(0, 1fr)) auto">'
      + '<div class="field"><label class="lbl" for="meta-q-artist">Search Artist</label>'
      + '<input class="input" id="meta-q-artist" data-meta-modal-input="artist" value="' + esc(META_MODAL.artist) + '" placeholder="Artist"></div>'
      + '<div class="field"><label class="lbl" for="meta-q-album">Search Album</label>'
      + '<input class="input" id="meta-q-album" data-meta-modal-input="album" value="' + esc(META_MODAL.album) + '" placeholder="Album"></div>'
      + '<div class="field"><label class="lbl" for="meta-q-title">Search Track Title</label>'
      + '<input class="input" id="meta-q-title" data-meta-modal-input="title" value="' + esc(META_MODAL.title) + '" placeholder="Track title"></div>'
      + '<button type="button" class="btn' + (META_MODAL.loading ? ' is-busy' : '') + '" data-action="meta-modal-search"' + (META_MODAL.loading ? ' disabled' : '') + '>'
      + (META_MODAL.loading ? 'Checking…' : 'Re-check online') + '</button>'
      + '</div>';

    let contentHtml = '';
    if (META_MODAL.loading) {
      contentHtml = '<p class="muted"><span class="working" aria-hidden="true"></span> Checking file tags, folder/file name, and online metadata databases…</p>';
    } else if (META_MODAL.error) {
      contentHtml = '<p class="warn-text">' + esc(META_MODAL.error) + '</p>';
    } else if (META_MODAL.item) {
      const it = META_MODAL.item;
      const fields = it.fields || {};
      const overrideBanner = it.query_source === 'path_override'
        ? '<div class="notice" style="margin:0"><strong>Folder/file name used for search:</strong> the file tags were totally different from the folder/file name, so the folder/file name was used to look up metadata.</div>'
        : '';
      const srcLine = '<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">'
        + '<div><span class="small muted">Online match: </span>'
        + (it.online_source
          ? '<span class="tag">' + esc(it.online_source) + (it.online_score ? ' · ' + Math.round(it.online_score * 100) + '%' : '') + '</span>'
          : '<span class="small muted">none found</span>')
        + ' <span class="small muted">· Tip: drag ⋮⋮ pills onto another row to copy values across parameters</span>'
        + '</div>'
        + '<div class="btns">'
        + '<button type="button" class="btn btn-sm" data-action="meta-modal-bulk" data-choice="path">Folder / File (1)</button>'
        + '<button type="button" class="btn btn-sm" data-action="meta-modal-bulk" data-choice="current">Current tag (1)</button>'
        + '</div></div>';

      const rows = META_FIELDS.map(([k]) => {
        const fo = fields[k] || { current: '', path: '', discogs: '', musicbrainz: '', open: '', online: '', choice: 'current', custom_value: '' };
        return '<tr data-meta-row-file="' + it.id + '" data-meta-row-field="' + k + '" data-drop-modal="1">'
          + renderMetaFieldCells(it, k, fo, true)
          + '</tr>';
      }).join('');

      contentHtml = overrideBanner + srcLine
        + '<div class="table-wrap"><table class="meta-table"><thead><tr>'
        + '<th>Current tag / Selected (✎)</th>'
        + '<th>Field</th>'
        + '<th>Folder / file name</th>'
        + '<th><div class="meta-th-source"><span class="meta-th-title">Discogs</span><button type="button" class="btn btn-sm meta-th-btn" data-action="meta-modal-bulk" data-choice="discogs">Discogs (1)</button></div></th>'
        + '<th><div class="meta-th-source"><span class="meta-th-title">MusicBrainz</span><button type="button" class="btn btn-sm meta-th-btn" data-action="meta-modal-bulk" data-choice="musicbrainz">MusicBrainz (1)</button></div></th>'
        + '<th><div class="meta-th-source"><span class="meta-th-title">Open (iTunes)</span><button type="button" class="btn btn-sm meta-th-btn" data-action="meta-modal-bulk" data-choice="open">Open (iTunes) (1)</button></div></th>'
        + '</tr></thead><tbody>' + rows + '</tbody></table></div>';
    }

    bodyEl.innerHTML = searchHtml + contentHtml;
    const disabled = META_MODAL.loading || META_MODAL.busy || !META_MODAL.item;
    if (btnFile) btnFile.disabled = disabled;
    if (btnAlbum) {
      btnAlbum.disabled = disabled;
      btnAlbum.textContent = 'Update album (' + (META_MODAL.albumFileCount || 1) + ' file' + ((META_MODAL.albumFileCount || 1) === 1 ? '' : 's') + ')';
    }
  }

  async function openMetaModal(fileId, opts) {
    const o = opts || {};
    META_MODAL.open = true;
    META_MODAL.fileId = fileId;
    META_MODAL.name = o.name || '';
    META_MODAL.relpath = o.relpath || '';
    META_MODAL.dir = o.dir || '';
    META_MODAL.albumFileCount = 1;
    META_MODAL.queryMode = currentSearchFrom();
    META_MODAL.artist = '';
    META_MODAL.album = '';
    META_MODAL.title = '';
    META_MODAL.item = null;
    META_MODAL.loading = true;
    META_MODAL.busy = false;
    META_MODAL.error = '';

    const modal = document.getElementById('metadata-modal');
    if (modal) modal.hidden = false;
    document.body.classList.add('has-modal');
    renderMetaModal();
    await runMetaModalCheck(false);
  }

  async function runMetaModalCheck(force, explicitMode) {
    if (!META_MODAL.fileId) return;
    if (explicitMode) META_MODAL.queryMode = explicitMode;
    META_MODAL.loading = true;
    META_MODAL.error = '';
    renderMetaModal();
    const busy = startBusy('Checking file metadata · ' + (META_MODAL.name || 'file'), 1);
    const elapsed = tickBusy(busy);
    try {
      const qm = META_MODAL.queryMode || 'auto';
      const body = { force: !!force, query_mode: qm };
      if (force && !explicitMode) {
        body.artist = META_MODAL.artist;
        body.album = META_MODAL.album;
        body.title = META_MODAL.title;
      }
      const d = await api('/files/' + META_MODAL.fileId + '/metadata/check', { method: 'POST', body });
      META_MODAL.item = d;
      META_MODAL.name = d.name || META_MODAL.name;
      META_MODAL.relpath = d.relpath || META_MODAL.relpath;
      META_MODAL.dir = d.dir || META_MODAL.dir;
      META_MODAL.albumFileCount = d.album_file_count || 1;
      const f = d.fields || {};
      if (qm === 'file') {
        META_MODAL.artist = (f.artist && f.artist.path) || '';
        META_MODAL.album = (f.album && f.album.path) || '';
        META_MODAL.title = (f.title && f.title.path) || '';
      } else if (qm === 'metadata') {
        META_MODAL.artist = (f.artist && f.artist.current) || '';
        META_MODAL.album = (f.album && f.album.current) || '';
        META_MODAL.title = (f.title && f.title.current) || '';
      } else {
        META_MODAL.artist = (f.artist && (f.artist.online || f.artist.current || f.artist.path)) || '';
        META_MODAL.album = (f.album && (f.album.online || f.album.current || f.album.path)) || '';
        META_MODAL.title = (f.title && (f.title.online || f.title.current || f.title.path)) || '';
      }
      invalidate('metadata');
    } catch (err) {
      META_MODAL.error = err.message;
    } finally {
      elapsed.stop();
      endBusy(busy);
      META_MODAL.loading = false;
      renderMetaModal();
    }
  }

  function closeMetaModal() {
    if (!META_MODAL.open) return;
    META_MODAL.open = false;
    const modal = document.getElementById('metadata-modal');
    if (modal) modal.hidden = true;
    if (!EDIT.open && !COVER.open) document.body.classList.remove('has-modal');
  }

  async function applyMetaFromModal(scope) {
    if (!META_MODAL.open || !META_MODAL.fileId || !META_MODAL.item || META_MODAL.busy) return;
    META_MODAL.busy = true;
    renderMetaModal();
    const label = scope === 'album' ? 'album' : META_MODAL.name;
    const totalHint = scope === 'album' ? (META_MODAL.albumFileCount || 1) : 1;
    const busy = startBusy('Updating metadata (preserving timestamp) · ' + label, totalHint);
    const elapsed = tickBusy(busy);
    try {
      const f = META_MODAL.item.fields || {};
      const overrides = {};
      const sharedAlbumFields = ['artist', 'album', 'album_artist', 'year', 'genre', 'cover'];
      META_FIELDS.forEach(([k]) => {
        if (scope === 'album' && !sharedAlbumFields.includes(k)) return;
        const fo = f[k];
        if (!fo) return;
        const ch = fo.choice || 'current';
        if (ch === 'custom') overrides[k] = { choice: 'custom', value: fo.custom_value || '' };
        else if (ch === 'discogs' && fo.discogs) overrides[k] = { choice: 'discogs', value: fo.discogs };
        else if (ch === 'musicbrainz' && fo.musicbrainz) overrides[k] = { choice: 'musicbrainz', value: fo.musicbrainz };
        else if (ch === 'open' && fo.open) overrides[k] = { choice: 'open', value: fo.open };
        else if (ch === 'online' && fo.online) overrides[k] = { choice: 'online', value: fo.online };
        else if (ch === 'path' && fo.path) overrides[k] = { choice: 'path', value: fo.path };
        else if (ch === 'current') overrides[k] = { choice: 'current', value: fo.current || '' };
      });
      const r = await api('/metadata/apply', {
        method: 'POST',
        body: { ids: [META_MODAL.fileId], scope: scope, overrides: overrides },
      });
      bumpCoverCache();
      closeMetaModal();
      invalidate('metadata');
      invalidate('coverParked');
      state.browse.stale = true;
      await Promise.all([
        refreshDash(),
        currentRoute() === 'metadata' ? loadKey('metadata') : Promise.resolve(),
        currentRoute() === 'directories' && state.browse.rel ? loadBrowse(state.browse.rel, true) : Promise.resolve(),
        currentRoute() === 'parked' ? loadKey('coverParked') : Promise.resolve(),
      ]);
      const warnNote = r.errors && r.errors.length ? ' · Note: ' + r.errors[0] : '';
      toast('Updated metadata for ' + r.updated + ' file' + (r.updated === 1 ? '' : 's') + ' (timestamp preserved)' + warnNote);
      render();
    } catch (err) {
      META_MODAL.error = err.message;
      toast(err.message);
    } finally {
      elapsed.stop();
      endBusy(busy);
      META_MODAL.busy = false;
      if (META_MODAL.open) renderMetaModal();
    }
  }

  /* -------------------------------------------------------------- routing */
  const ROUTES = {
    dashboard: renderDashboard,
    directories: renderDirectories,
    parked: renderParked,
    metadata: renderMetadata,
    schedule: renderSchedule,
    settings: renderSettings,
    homeassistant: renderHA,
  };
  const TITLES = {
    dashboard: 'Dashboard',
    directories: 'Directories',
    parked: 'Parked files',
    metadata: 'Meta data',
    schedule: 'Schedule',
    settings: 'Settings',
    homeassistant: 'Home Assistant',
  };

  function currentRoute() {
    const r = location.hash.replace(/^#\/?/, '') || 'dashboard';
    return ROUTES[r] ? r : 'dashboard';
  }

  function updateNavBadge() {
    const dirsEl = document.getElementById('dirsBadge');
    const dirsLink = document.querySelector('#nav a[data-route="directories"]');
    if (dirsEl && state.dash) {
      const aMed = dashMedia('audio') || {};
      const vMed = dashMedia('video') || {};
      const selDirs = (aMed.directories_enabled || 0) + (vMed.directories_enabled || 0);
      const totDirs = (aMed.directories_total || 0) + (vMed.directories_total || 0);
      const selFiles = (aMed.files_total || 0) + (vMed.files_total || 0);
      dirsEl.textContent = fmt(selDirs) + ' / ' + fmt(selFiles);
      const tipText = (vMed.directories_total || vMed.files_total)
        ? 'Directories — ' + fmt(selDirs) + ' of ' + fmt(totDirs) + ' folders · ' + fmt(selFiles) + ' files selected'
          + ' (Audio: ' + fmt(aMed.directories_enabled || 0) + ' folders, ' + fmt(aMed.files_total || 0) + ' files'
          + ' · Video: ' + fmt(vMed.directories_enabled || 0) + ' folders, ' + fmt(vMed.files_total || 0) + ' files)'
        : 'Directories — ' + fmt(selDirs) + ' of ' + fmt(totDirs) + ' folders · ' + fmt(selFiles) + ' files selected';
      dirsEl.title = tipText;
      if (dirsLink) dirsLink.title = tipText;
    }
    const el = document.getElementById('parkedBadge');
    if (el && state.dash) {
      const audioParked = ALL.reduce((a, t) => a + dashMedia(t).files_parked, 0);
      const coverParked = (dashMedia('audio') && dashMedia('audio').files_cover_parked) || 0;
      el.textContent = audioParked + coverParked;
    }
    const metaEl = document.getElementById('metaBadge');
    if (metaEl && state.dash) {
      const metaDiff = (state.metadata && typeof state.metadata.diff_count === 'number')
        ? state.metadata.diff_count
        : ((dashMedia('audio') && dashMedia('audio').files_meta_diff) || 0);
      metaEl.textContent = metaDiff;
    }
  }

  let approvedWatchTimer = null;
  function watchApprovedWork() {
    if (approvedWatchTimer) return;
    approvedWatchTimer = setInterval(async () => {
      try {
        await refreshDash();
        if (currentRoute() === 'parked' && !state.dragging) {
          const t = state.media;
          const d = await loadParkedData(t);
          if (JSON.stringify(d) !== JSON.stringify(state.parked[t]) && currentRoute() === 'parked' && !state.dragging) {
            state.parked[t] = d;
            render();
          }
        }
      } catch (_err) {
        /* ignore transient errors */
      }
      const d = state.dash;
      if (!d || (!d.approved_pending && d.status !== 'running' && !d.scanning)) {
        clearInterval(approvedWatchTimer);
        approvedWatchTimer = null;
      }
    }, 800);
  }

  function hasCachedRouteData(route) {
    const t = state.media;
    if (route === 'directories') return !!(state.trees[t] && state.settings && state.dash);
    if (route === 'parked') return !!(state.parked[t] && state.settings && (t !== 'audio' || state.coverParked));
    if (route === 'metadata') return !!(state.metadata && state.dash && state.settings);
    return false;
  }

  /* ----------------------------------------------------------- cover modal */
  const COVER = {
    open: false,
    fileId: null,
    name: '',
    relpath: '',
    dir: '',
    hasCover: false,
    artist: '',
    album: '',
    title: '',
    source: 'both',
    queryMode: 'auto',
    querySource: 'tags',
    tagArtist: '',
    tagAlbum: '',
    tagTitle: '',
    pathArtist: '',
    pathAlbum: '',
    pathTitle: '',
    albumFileCount: 1,
    targetIds: null,
    defaultScope: 'file',
    candidates: [],
    selectedUrl: '',
    loading: false,
    busy: false,
    error: '',
  };

  function renderCoverModal() {
    const modal = document.getElementById('cover-modal');
    if (!modal || !COVER.open) return;
    const titleEl = document.getElementById('cover-modal-title');
    const subEl = document.getElementById('cover-modal-sub');
    const bodyEl = document.getElementById('cover-modal-body');
    const noteEl = document.getElementById('cover-modal-note');
    const btnFile = document.getElementById('cover-apply-file');
    const btnAlbum = document.getElementById('cover-apply-album');

    titleEl.textContent = COVER.defaultScope === 'album'
      ? 'Album / folder cover art — ' + (COVER.album || folderLabel(COVER.dir))
      : 'Cover art — ' + COVER.name;
    subEl.textContent = MEDIA.audio.root + '/' + COVER.relpath;

    const curCovUrl = COVER.hasCover ? fileCoverUrl(COVER.fileId, Date.now()) : '';
    const currentHtml = COVER.hasCover
      ? '<div class="cover-current-row">'
        + '<img class="cover-current-img" src="' + esc(curCovUrl) + '" data-cover-zoom="' + esc(curCovUrl) + '" data-cover-caption="' + esc(COVER.name + ' · Current embedded cover') + '" alt="Current cover">'
        + '<div><strong>Embedded cover art is present</strong><div class="small muted">Select a new picture below to replace it for this file or the whole album.</div></div>'
        + '</div>'
      : '';

    const overrideBanner = COVER.querySource === 'path_override'
      ? '<div class="notice" style="margin:0"><strong>Folder/file name used for search:</strong> the ID3 tags ('
        + esc([COVER.tagArtist, COVER.tagAlbum, COVER.tagTitle].filter(Boolean).join(' / ') || 'none')
        + ') were totally different from the folder/file name ('
        + esc([COVER.pathArtist, COVER.pathAlbum, COVER.pathTitle].filter(Boolean).join(' / '))
        + '), so the folder/file name was taken.</div>'
      : '';

    const srcOpts = [
      ['both', 'Both (Discogs + Open)'],
      ['discogs', 'Discogs only'],
      ['open', 'Open sources only'],
    ].map(([k, lbl]) => '<option value="' + k + '"' + (COVER.source === k ? ' selected' : '') + '>' + lbl + '</option>').join('');

    const pathPreview = [COVER.pathArtist, COVER.pathTitle].filter(Boolean).join(' — ') || [COVER.pathArtist, COVER.pathAlbum].filter(Boolean).join(' — ') || 'none';
    const tagPreview = [COVER.tagArtist, COVER.tagTitle].filter(Boolean).join(' — ') || [COVER.tagArtist, COVER.tagAlbum].filter(Boolean).join(' — ') || 'none';
    const cqm = COVER.queryMode || 'auto';
    const coverQuerySrcBar = '<div class="query-src-bar">'
      + '<span class="muted small">Take Artist / Title from:</span>'
      + '<div class="seg" role="group" aria-label="Take Artist and Title from">'
      + '<button type="button" class="' + (cqm === 'auto' ? 'on' : '') + '" data-action="cover-query-mode" data-mode="auto"' + (COVER.loading ? ' disabled' : '') + ' title="Smart automatic: use metadata tags unless missing or totally different from folder/file name">Auto</button>'
      + '<button type="button" class="' + (cqm === 'file' ? 'on' : '') + '" data-action="cover-query-mode" data-mode="file"' + (COVER.loading ? ' disabled' : '') + ' title="File: ' + esc(pathPreview) + '">From file</button>'
      + '<button type="button" class="' + (cqm === 'metadata' ? 'on' : '') + '" data-action="cover-query-mode" data-mode="metadata"' + (COVER.loading ? ' disabled' : '') + ' title="Meta data: ' + esc(tagPreview) + '">From meta data</button>'
      + '</div>'
      + ((!COVER.loading || COVER.pathArtist || COVER.tagArtist || COVER.pathTitle || COVER.tagTitle)
        ? '<span class="small muted query-src-preview"><span><strong>File:</strong> ' + esc(pathPreview) + '</span><span>·</span><span><strong>Meta data:</strong> ' + esc(tagPreview) + '</span></span>'
        : '')
      + '</div>';

    const searchHtml = coverQuerySrcBar + '<div class="cover-search-grid">'
      + '<div class="field"><label class="lbl" for="cover-q-artist">Artist</label>'
      + '<input class="input" id="cover-q-artist" data-cover-input="artist" value="' + esc(COVER.artist) + '" placeholder="Artist"></div>'
      + '<div class="field"><label class="lbl" for="cover-q-album">Album</label>'
      + '<input class="input" id="cover-q-album" data-cover-input="album" value="' + esc(COVER.album) + '" placeholder="Album"></div>'
      + '<div class="field"><label class="lbl" for="cover-q-title">Track title</label>'
      + '<input class="input" id="cover-q-title" data-cover-input="title" value="' + esc(COVER.title) + '" placeholder="Track title"></div>'
      + '<div class="field"><label class="lbl" for="cover-q-source">Source</label>'
      + '<select class="input" id="cover-q-source" data-cover-input="source">' + srcOpts + '</select></div>'
      + '<button type="button" class="btn' + (COVER.loading ? ' is-busy' : '') + '" data-action="cover-search"' + (COVER.loading ? ' disabled' : '') + '>'
      + (COVER.loading ? 'Searching…' : 'Search') + '</button>'
      + '</div>';

    let gridHtml = '';
    if (COVER.loading) {
      gridHtml = '<p class="muted"><span class="working" aria-hidden="true"></span> Searching Discogs and open cover databases…</p>';
    } else if (COVER.error) {
      gridHtml = '<p class="warn-text">' + esc(COVER.error) + '</p>';
    } else if (!COVER.candidates.length) {
      gridHtml = '<p class="muted">No cover pictures found for this query. Adjust the Artist, Album or Track title above and press <strong>Search</strong>.</p>';
    } else {
      gridHtml = '<div class="cover-grid">' + COVER.candidates.map((c, idx) => {
        const sel = COVER.selectedUrl === c.image_url;
        const thumb = candThumbUrl(COVER.fileId, c.thumb_url || c.image_url || '');
        const fullImg = candThumbUrl(COVER.fileId, c.image_url || c.thumb_url || '');
        const cap = [c.title, c.source, c.year].filter(Boolean).join(' · ');
        return '<div class="cover-cand' + (sel ? ' is-selected' : '') + '" data-action="cover-select-cand" data-idx="' + idx + '" tabindex="0" role="button" aria-pressed="' + sel + '">'
          + '<div class="cover-cand-img-wrap"' + (fullImg ? ' data-cover-zoom="' + esc(fullImg) + '" data-cover-caption="' + esc(cap) + '"' : '') + '>'
          + (thumb ? '<img class="cover-cand-img" src="' + esc(thumb) + '" alt="' + esc(c.title) + '" loading="lazy">' : '<span class="muted small">No preview</span>')
          + '</div>'
          + '<div class="cover-cand-title">' + esc(c.title || 'Cover') + '</div>'
          + '<div class="cover-cand-meta"><span class="tag" style="margin:0">' + esc(c.source || 'Source') + '</span>'
          + '<span>' + esc(c.year || '') + '</span></div>'
          + '</div>';
      }).join('') + '</div>';
    }

    bodyEl.innerHTML = currentHtml + overrideBanner + searchHtml + gridHtml;
    const targetCount = (COVER.targetIds && COVER.targetIds.length) || COVER.albumFileCount || 1;
    btnFile.disabled = !COVER.selectedUrl || COVER.busy || COVER.loading;
    btnAlbum.disabled = !COVER.selectedUrl || COVER.busy || COVER.loading;
    btnAlbum.textContent = COVER.targetIds && COVER.targetIds.length
      ? 'Use for selected (' + COVER.targetIds.length + ')'
      : 'Use for album (' + targetCount + ')';
    noteEl.textContent = COVER.candidates.length
      ? COVER.candidates.length + ' picture' + (COVER.candidates.length === 1 ? '' : 's') + ' found · click a picture, then apply to 1 file or the album'
      : '';
  }

  async function openCoverModal(fileId, opts) {
    const o = opts || {};
    COVER.open = true;
    COVER.fileId = Number(fileId);
    COVER.name = o.name || '';
    COVER.relpath = o.relpath || '';
    COVER.dir = o.dir || '';
    COVER.hasCover = !!o.hasCover;
    COVER.artist = '';
    COVER.album = '';
    COVER.title = '';
    COVER.source = (state.settings && state.settings.general && state.settings.general.coverSources) || 'both';
    COVER.queryMode = currentSearchFrom();
    COVER.querySource = 'tags';
    COVER.albumFileCount = 1;
    COVER.targetIds = o.targetIds || null;
    COVER.defaultScope = o.scope || 'file';
    COVER.candidates = [];
    COVER.selectedUrl = '';
    COVER.loading = true;
    COVER.busy = false;
    COVER.error = '';

    const modal = document.getElementById('cover-modal');
    if (modal) {
      modal.hidden = false;
      document.body.classList.add('has-modal');
    }
    renderCoverModal();
    await runCoverModalSearch(false);
  }

  async function runCoverModalSearch(force, explicitMode) {
    if (!COVER.open || !COVER.fileId) return;
    if (explicitMode) COVER.queryMode = explicitMode;
    COVER.loading = true;
    COVER.error = '';
    renderCoverModal();
    const busy = startBusy('Searching cover art · ' + (COVER.name || 'file'), 1);
    const elapsed = tickBusy(busy);
    try {
      const qm = COVER.queryMode || 'auto';
      const body = { source: COVER.source, query_mode: qm, force: !!force };
      if (force && !explicitMode) {
        body.artist = COVER.artist;
        body.album = COVER.album;
        body.title = COVER.title;
      }
      const d = await api('/files/' + COVER.fileId + '/cover/search', { method: 'POST', body });
      COVER.name = d.name || COVER.name;
      COVER.relpath = d.relpath || COVER.relpath;
      COVER.dir = d.dir || COVER.dir;
      COVER.hasCover = !!d.has_cover;
      COVER.artist = d.artist || '';
      COVER.album = d.album || '';
      COVER.title = d.title || '';
      COVER.queryMode = d.query_mode || qm;
      COVER.querySource = d.query_source || 'tags';
      COVER.tagArtist = d.tag_artist || '';
      COVER.tagAlbum = d.tag_album || '';
      COVER.tagTitle = d.tag_title || '';
      COVER.pathArtist = d.path_artist || '';
      COVER.pathAlbum = d.path_album || '';
      COVER.pathTitle = d.path_title || '';
      COVER.albumFileCount = d.album_file_count || 1;
      COVER.candidates = d.candidates || [];
      COVER.selectedUrl = COVER.candidates[0] ? COVER.candidates[0].image_url : '';
    } catch (err) {
      COVER.error = err.message;
    } finally {
      elapsed.stop();
      endBusy(busy);
      COVER.loading = false;
      renderCoverModal();
    }
  }

  function closeCoverModal() {
    if (!COVER.open) return;
    COVER.open = false;
    const modal = document.getElementById('cover-modal');
    if (modal) modal.hidden = true;
    if (!EDIT.open && !META_MODAL.open) document.body.classList.remove('has-modal');
  }

  async function applyCoverFromModal(scope) {
    if (!COVER.open || !COVER.fileId || !COVER.selectedUrl || COVER.busy) return;
    COVER.busy = true;
    renderCoverModal();
    const label = scope === 'album' ? 'album' : COVER.name;
    const totalHint = scope === 'album'
      ? ((COVER.targetIds && COVER.targetIds.length) || COVER.albumFileCount || 1)
      : 1;
    const busy = startBusy('Embedding cover art · ' + label, totalHint);
    const elapsed = tickBusy(busy);
    try {
      const body = { image_url: COVER.selectedUrl, scope: scope };
      if (scope === 'album' && COVER.targetIds && COVER.targetIds.length) {
        body.target_ids = COVER.targetIds;
      }
      const r = await api('/files/' + COVER.fileId + '/cover/apply', { method: 'POST', body });
      bumpCoverCache();
      closeCoverModal();
      invalidate('coverParked');
      invalidate('metadata');
      state.browse.stale = true;
      await Promise.all([
        refreshDash(),
        currentRoute() === 'parked' ? loadKey('coverParked') : Promise.resolve(),
        currentRoute() === 'metadata' ? loadKey('metadata') : Promise.resolve(),
        currentRoute() === 'directories' && state.browse.rel ? loadBrowse(state.browse.rel, true) : Promise.resolve(),
      ]);
      toast('Embedded cover art into ' + r.updated + ' file' + (r.updated === 1 ? '' : 's'));
      render();
    } catch (err) {
      COVER.error = err.message;
      toast(err.message);
    } finally {
      elapsed.stop();
      endBusy(busy);
      COVER.busy = false;
      if (COVER.open) renderCoverModal();
    }
  }

  function render() {
    const route = currentRoute();
    captureDirScroll();
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
      missing.forEach((k) => {
        if (!state.pending[k]) loadKey(k).then(render);
      });
      if (!hasCachedRouteData(route)) {
        app().innerHTML = pageHead(TITLES[route], '', '') + '<p class="muted">Loading…</p>';
        return;
      }
    }
    ROUTES[route]();
    document.querySelectorAll('input[type=range]').forEach(paintRange);
  }

  /* --------------------------------------------------------- interaction */
  const SAVE_ERR = (err) => {
    if (err && err.name === 'AbortError') return;
    toast(err.message);
  };

  async function onAction(el) {
    const a = el.dataset.action;
    const t = state.media;

    if (a === 'stop-current-action') {
      if (stoppingAction) return;
      stoppingAction = true;
      showActivity();
      try {
        Array.from(activeApiControllers).forEach((c) => {
          try { c.abort(); } catch (_err) { /* ignore */ }
        });
        activeApiControllers.clear();
        if (metaCheckAbort) {
          try { metaCheckAbort.abort(); } catch (_err) { /* ignore */ }
          metaCheckAbort = null;
        }
        if (metaWatchTimer) {
          clearInterval(metaWatchTimer);
          metaWatchTimer = null;
        }
        const res = await api('/actions/stop', { method: 'POST' }).catch(() => ({ ok: true }));
        state.busy = {};
        state.metaChecking = false;
        state.scanning = { audio: false, video: false };
        state.scanMode = { audio: null, video: null };
        state.scanTarget = { audio: null, video: null };
        if (state.metadata) state.metadata.meta_checking = false;
        if (state.dash) {
          state.dash.meta_checking = false;
          state.dash.scanning = false;
          state.dash.approved_pending = 0;
        }
        COVER.loading = false;
        COVER.busy = false;
        META_MODAL.loading = false;
        META_MODAL.busy = false;
        invalidate('metadata');
        invalidate('dir:' + state.media);
        invalidate('parked:' + state.media);
        invalidate('coverParked');
        state.browse.stale = true;
        await refreshDash().catch(() => {});
        if (currentRoute() === 'metadata') {
          await loadKey('metadata').catch(() => {});
        } else if (currentRoute() === 'directories' && state.browse && state.browse.rel) {
          await loadBrowse(state.browse.rel, true).catch(() => {});
        }
        const stoppedList = (res && Array.isArray(res.stopped) && res.stopped.length)
          ? ' (' + res.stopped.join(', ') + ')'
          : '';
        toast('Stopped current action' + stoppedList);
      } finally {
        stoppingAction = false;
        showActivity();
        render();
      }
      return;
    }

    if (a === 'media') {
      state.media = el.dataset.media;
      state.browse = { media: null, rel: null, rows: null, loading: false };
      closePreview();
      render();
    } else if (a === 'run') {
      const action = el.dataset.path;
      const mode = state.dash ? state.dash.mode : 'bulk';
      const label = action === 'start'
        ? (mode === 'force' ? 'Starting force run' : 'Starting bulk run')
        : action === 'stop'
          ? 'Stopping run'
          : (state.dash && state.dash.paused ? 'Resuming run' : 'Pausing run');
      const busy = startBusy(label);
      const elapsed = tickBusy(busy);
      if (state.dash) {
        if (action === 'start') {
          const inWin = ALL.some((m) => state.dash.types && state.dash.types[m] && state.dash.media && state.dash.media[m] && state.dash.media[m].in_window);
          state.dash.run_mode = 'active';
          state.dash.paused = false;
          state.dash.status = inWin ? 'running' : 'waiting_window';
        } else if (action === 'stop') {
          state.dash.run_mode = 'stopped';
          state.dash.paused = false;
          state.dash.status = 'stopped';
        } else if (action === 'pause') {
          state.dash.paused = !state.dash.paused;
          state.dash.status = state.dash.paused ? 'paused' : 'running';
        }
        state.dashKey = JSON.stringify(state.dash);
        render();
      }
      try {
        await api('/run/' + action, { method: 'POST' });
        await refreshDash();
        if (action === 'start' || (state.dash && state.dash.status === 'running')) {
          watchApprovedWork();
        }
      } finally {
        elapsed.stop();
        endBusy(busy, 600);
      }
      render();
    } else if (a === 'set-mode') {
      const mode = el.dataset.mode;
      if (mode === state.dash.mode) return;
      const busy = startBusy('Changing the mode');
      try {
        state.settings = await api('/settings', { method: 'PUT', body: { general: { mode } } });
        state.draft = clone(state.settings);
        await refreshDash();
        toast(MODE_LABEL[mode] + ' mode selected');
      } finally {
        endBusy(busy);
      }
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
      scanDirectories(t, true);       // incremental quick scan (Option A + B + D)
      render();
    } else if (a === 'rescan-full') {
      scanDirectories(t, true, { full: true });
      render();
    } else if (a === 'rescan-folder') {
      const rel = el.dataset.rel || state.selected[t];
      if (rel) {
        scanDirectories(t, true, { dir: rel });
        render();
      }
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
        invalidate('metadata');
        invalidate('parked:' + t);
        invalidate('coverParked');
        await refreshDash();
      } finally {
        endBusy(busy);
      }
      render();
    } else if (a === 'invert-dirs') {
      const busy = startBusy('Inverting folder selection');
      try {
        await api('/folders/invert', { method: 'POST', body: { media: t } });
        invalidate('dir:' + t);
        invalidate('metadata');
        invalidate('parked:' + t);
        invalidate('coverParked');
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
      const f = ((state.parked[t] && state.parked[t].files) || []).find((x) => String(x.id) === String(el.dataset.file));
      const label = f ? f.name : 'parked file';
      const busy = startBusy((a === 'approve' ? 'Applying parked change · ' : 'Skipping · ') + label);
      const elapsed = tickBusy(busy);
      try {
        await api('/parked/' + el.dataset.file + '/' + path, { method: 'POST' });
        state.parkSel = state.parkSel.filter((id) => String(id) !== el.dataset.file);
        invalidate('parked:' + t);
        await Promise.all([loadKey('parked:' + t), refreshDash()]);
        if (a === 'approve') watchApprovedWork();
        toast(a === 'approve' ? 'Applying change to ' + label : 'Skipped ' + label);
      } finally {
        elapsed.stop();
        endBusy(busy, 800);
      }
      render();
    } else if (a === 'approve-all') {
      const busy = startBusy('Applying all parked ' + MEDIA[t].label.toLowerCase() + ' files');
      const elapsed = tickBusy(busy);
      try {
        const r = await api('/parked/approve-all', { method: 'POST', body: { media: t, reason: el.dataset.reason } });
        state.parkSel = [];
        invalidate('parked:' + t);
        await Promise.all([loadKey('parked:' + t), refreshDash()]);
        if (r.done) watchApprovedWork();
        toast('Applying ' + r.done + ' parked file' + (r.done === 1 ? '' : 's'));
      } finally {
        elapsed.stop();
        endBusy(busy, 800);
      }
      render();
    } else if (a === 'adjust-file') {
      const f = ((state.parked[t] && state.parked[t].files) || []).find((x) => String(x.id) === String(el.dataset.file));
      const busy = startBusy('Adjusting change · ' + (f ? f.name : 'parked file'));
      try {
        await api('/parked/' + el.dataset.file + '/adjust', { method: 'POST', body: { delta: Number(el.dataset.step) } });
        invalidate('parked:' + t);
        await loadKey('parked:' + t);
      } finally {
        endBusy(busy, 500);
      }
      render();
    } else if (a === 'bulk-adjust') {
      const count = state.parkSel.length;
      const busy = startBusy('Adjusting ' + count + ' selected parked file' + (count === 1 ? '' : 's'), count);
      try {
        await api('/parked/bulk', { method: 'POST', body: { ids: state.parkSel, action: 'adjust', delta: Number(el.dataset.step) } });
        invalidate('parked:' + t);
        await loadKey('parked:' + t);
      } finally {
        endBusy(busy, 500);
      }
      render();
    } else if (a === 'apply-selected') {
      const count = state.parkSel.length;
      const busy = startBusy('Applying ' + count + ' selected parked file' + (count === 1 ? '' : 's'), count);
      const elapsed = tickBusy(busy);
      try {
        const r = await api('/parked/bulk', { method: 'POST', body: { ids: state.parkSel, action: 'approve' } });
        state.parkSel = [];
        invalidate('parked:' + t);
        await Promise.all([loadKey('parked:' + t), refreshDash()]);
        if (r.done) watchApprovedWork();
        toast('Applying ' + r.done + ' selected file' + (r.done === 1 ? '' : 's'));
      } finally {
        elapsed.stop();
        endBusy(busy, 800);
      }
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
      const busy = startBusy('Checking the level of ' + f.name, 1);
      const elapsed = tickBusy(busy);
      render();
      try {
        const r = await api('/files/measure', { method: 'POST', body: { media: t, relpath: f.relpath } });
        state.measure = {
          media: t, relpath: f.relpath, name: r.name, state: 'done',
          lufs: r.lufs, tp: r.true_peak, gain_db: r.gain_db,
          silent_sections: r.silent_sections, silent_duration: r.silent_duration,
        };
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
    } else if (a === 'check-all-files') {
      const b = state.browse;
      if (!b || !b.rows || !b.rows.length) return;
      const ids = state.fileSel.length ? state.fileSel.slice() : b.rows.map((f) => f.id);
      const busy = startBusy('Checking ' + ids.length + ' file' + (ids.length === 1 ? '' : 's'), ids.length);
      const elapsed = tickBusy(busy);
      render();
      try {
        const r = await api('/files/check-batch', { method: 'POST', body: { media: t, ids: ids, dir: b.rel } });
        toast('Checked ' + r.checked + ' file' + (r.checked === 1 ? '' : 's')
              + (r.empty_sections ? ' · ' + r.empty_sections + ' with empty sections' : ''));
        await refreshDash();
      } catch (err) {
        toast(err.message);
      } finally {
        elapsed.stop();
        endBusy(busy);
      }
      if (b.rel) await loadBrowse(b.rel);
      else render();
    } else if (a === 'execute-files') {
      const b = state.browse;
      if (!b || !b.rows || !b.rows.length) return;
      const ids = state.fileSel.length ? state.fileSel.slice() : b.rows.map((f) => f.id);
      const busy = startBusy('Executing ' + ids.length + ' file' + (ids.length === 1 ? '' : 's'), ids.length);
      const elapsed = tickBusy(busy);
      render();
      try {
        const r = await api('/files/execute-batch', { method: 'POST', body: { media: t, ids: ids, dir: b.rel } });
        toast('Executed ' + r.executed + ' file' + (r.executed === 1 ? '' : 's') + ' (' + r.changed + ' changed)');
        await refreshDash();
      } catch (err) {
        toast(err.message);
      } finally {
        elapsed.stop();
        endBusy(busy);
      }
      if (b.rel) await loadBrowse(b.rel);
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
    } else if (a === 'theme-toggle') {
      const next = effectiveTheme() === 'dark' ? 'light' : 'dark';
      applyTheme(next, { saveLocal: true, saveServer: true });
      if (currentRoute() === 'settings') render();
    } else if (a === 'set-theme') {
      const next = el.dataset.theme || 'light';
      applyTheme(next, { saveLocal: true, saveServer: true });
      if (currentRoute() === 'settings') render();
    } else if (a === 'settings-save') {
      const d = state.draft;
      const busy = startBusy('Saving the settings');
      render();
      try {
        state.settings = await api('/settings', { method: 'PUT', body: { general: d.general, audio: d.audio, video: d.video } });
        state.draft = clone(state.settings);
        if (state.settings && state.settings.general && state.settings.general.theme) {
          applyTheme(state.settings.general.theme, { saveLocal: true, saveServer: false });
        }
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
    } else if (a === 'open-cover-modal') {
      const f = state.browse.rows && state.browse.rows[Number(el.dataset.idx)];
      if (f) {
        await openCoverModal(f.id, { name: f.name, relpath: f.relpath, dir: state.browse.rel || '', hasCover: f.has_cover, scope: 'file' });
      }
    } else if (a === 'open-album-cover') {
      const b = state.browse;
      if (!b || !b.rows || !b.rows.length) return;
      const ids = state.fileSel.length ? state.fileSel.slice() : b.rows.map((x) => x.id);
      const first = b.rows.find((x) => x.id === ids[0]) || b.rows[0];
      await openCoverModal(first.id, {
        name: first.name,
        relpath: first.relpath,
        dir: b.rel || '',
        hasCover: first.has_cover,
        targetIds: state.fileSel.length ? ids : null,
        scope: 'album',
      });
    } else if (a === 'open-cover-by-id') {
      const fid = Number(el.dataset.file);
      const ids = el.dataset.ids ? el.dataset.ids.split(',').map(Number).filter(Boolean) : null;
      await openCoverModal(fid, { scope: el.dataset.scope || 'file', targetIds: ids });
    } else if (a === 'cover-close') {
      closeCoverModal();
    } else if (a === 'cover-query-mode') {
      const mode = el.dataset.mode || 'auto';
      COVER.queryMode = mode;
      if (mode === 'file') {
        COVER.artist = COVER.pathArtist || '';
        COVER.album = COVER.pathAlbum || '';
        COVER.title = COVER.pathTitle || '';
      } else if (mode === 'metadata') {
        COVER.artist = COVER.tagArtist || '';
        COVER.album = COVER.tagAlbum || '';
        COVER.title = COVER.tagTitle || '';
      }
      await runCoverModalSearch(false, mode);
    } else if (a === 'cover-search') {
      await runCoverModalSearch(true);
    } else if (a === 'cover-select-cand') {
      const cand = COVER.candidates[Number(el.dataset.idx)];
      if (cand) {
        COVER.selectedUrl = cand.image_url;
        renderCoverModal();
      }
    } else if (a === 'cover-apply') {
      await applyCoverFromModal(el.dataset.scope || 'file');
    } else if (a === 'cover-apply-cand') {
      const fid = Number(el.dataset.file);
      const scope = el.dataset.scope || 'file';
      const url = el.dataset.url;
      const ids = el.dataset.ids ? el.dataset.ids.split(',').map(Number).filter(Boolean) : [];
      const busy = startBusy('Embedding cover picture (' + scope + ')', ids.length || 1);
      const elapsed = tickBusy(busy);
      try {
        const r = await api('/files/' + fid + '/cover/apply', {
          method: 'POST',
          body: { image_url: url, scope: scope, target_ids: ids },
        });
        bumpCoverCache();
        invalidate('coverParked');
        invalidate('metadata');
        state.browse.stale = true;
        await Promise.all([loadKey('coverParked'), refreshDash()]);
        toast('Embedded cover art into ' + r.updated + ' file' + (r.updated === 1 ? '' : 's'));
      } finally {
        elapsed.stop();
        endBusy(busy);
      }
      render();
    } else if (a === 'cover-skip') {
      const ids = (el.dataset.ids || '').split(',').map(Number).filter(Boolean);
      if (!ids.length) return;
      const busy = startBusy('Skipping cover art for ' + ids.length + ' file' + (ids.length === 1 ? '' : 's'), ids.length);
      try {
        await api('/covers/skip', { method: 'POST', body: { ids: ids } });
        invalidate('coverParked');
        await Promise.all([loadKey('coverParked'), refreshDash()]);
        toast('Skipped cover art for ' + ids.length + ' file' + (ids.length === 1 ? '' : 's'));
      } finally {
        endBusy(busy);
      }
      render();
    } else if (a === 'check-covers-dir') {
      const b = state.browse;
      if (!b || !b.rows || !b.rows.length) return;
      const ids = state.fileSel.length ? state.fileSel.slice() : b.rows.map((f) => f.id);
      const busy = startBusy('Checking cover art for ' + ids.length + ' file' + (ids.length === 1 ? '' : 's'), ids.length);
      const elapsed = tickBusy(busy);
      render();
      try {
        const r = await api('/covers/check', { method: 'POST', body: { ids: ids, dir: b.rel } });
        bumpCoverCache();
        invalidate('coverParked');
        invalidate('metadata');
        await refreshDash();
        const parts = [];
        if (r.embedded) parts.push(r.embedded + ' embedded');
        if (r.choices) parts.push(r.choices + ' parked with multiple choices');
        if (r.present) parts.push(r.present + ' already had cover');
        if (r.not_found) parts.push(r.not_found + ' not found');
        toast('Cover check: ' + (parts.length ? parts.join(' · ') : r.checked + ' checked'));
      } catch (err) {
        toast(err.message);
      } finally {
        elapsed.stop();
        endBusy(busy);
      }
      if (b.rel) await loadBrowse(b.rel);
      else render();
    } else if (a === 'cover-check-bulk') {
      const busy = startBusy('Checking missing cover art in enabled audio folders');
      const elapsed = tickBusy(busy);
      render();
      try {
        const r = await api('/covers/check', { method: 'POST', body: { enabled_only: true } });
        bumpCoverCache();
        invalidate('coverParked');
        invalidate('metadata');
        state.browse.stale = true;
        await Promise.all([
          refreshDash(),
          currentRoute() === 'parked' ? loadKey('coverParked') : Promise.resolve(),
        ]);
        const parts = [];
        if (r.embedded) parts.push(r.embedded + ' embedded');
        if (r.choices) parts.push(r.choices + ' parked with multiple choices');
        if (r.not_found) parts.push(r.not_found + ' not found');
        toast('Bulk cover check (' + r.checked + ' files): ' + (parts.length ? parts.join(' · ') : 'all checked'));
      } finally {
        elapsed.stop();
        endBusy(busy);
      }
      render();
    } else if (a === 'cover-apply-first-all') {
      const busy = startBusy('Embedding first cover picture for all waiting files');
      const elapsed = tickBusy(busy);
      try {
        const r = await api('/covers/apply-first-all', { method: 'POST', body: {} });
        bumpCoverCache();
        invalidate('coverParked');
        invalidate('metadata');
        state.browse.stale = true;
        await Promise.all([loadKey('coverParked'), refreshDash()]);
        toast('Embedded cover art into ' + r.updated + ' file' + (r.updated === 1 ? '' : 's'));
      } finally {
        elapsed.stop();
        endBusy(busy);
      }
      render();
    } else if (a === 'cover-park-mode') {
      state.coverParkMode = el.dataset.mode === 'file' ? 'file' : 'album';
      render();
    } else if (a === 'discogs-test') {
      const token = (state.draft && state.draft.general && state.draft.general.discogsToken) || '';
      const busy = startBusy('Testing Discogs API connection');
      const elapsed = tickBusy(busy);
      try {
        const r = await api('/covers/test-discogs', { method: 'POST', body: { token: token } });
        state.discogsTest = r.message || '';
      } finally {
        elapsed.stop();
        endBusy(busy);
      }
      render();
    } else if (a === 'jump-top') {
      const reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      window.scrollTo({ top: 0, left: 0, behavior: reduced ? 'auto' : 'smooth' });
      document.querySelectorAll('.split-dirs > .tree, .split-dirs > .files-pane').forEach((pane) => {
        pane.scrollTo({ top: 0, left: 0, behavior: reduced ? 'auto' : 'smooth' });
      });
    } else if (a === 'meta-check-run' || a === 'meta-check-force') {
      const force = a === 'meta-check-force';
      if (state.metaChecking) return;
      state.metaTouchedFiles.clear();
      state.metaChecking = true;
      if (state.metadata) {
        state.metadata.meta_checking = true;
        state.metadata.meta_progress = { running: true, force: force, total: 0, done: 0, diff: 0, ok: 0, current_file: '' };
      }
      if (state.dash) state.dash.meta_checking = true;
      ensureFastBusyPoll();
      renderPreservingMetaScroll();
      try {
        const r = await api('/metadata/check', {
          method: 'POST',
          body: { enabled_only: true, force: force, background: true },
        });
        if (state.metadata && r) state.metadata.meta_progress = r;
        watchMetaCheck();
        toast(force ? 'Re-checking all metadata — intermediate results appear live below' : 'Checking metadata — intermediate results appear live below');
      } catch (err) {
        state.metaChecking = false;
        if (state.metadata) state.metadata.meta_checking = false;
        if (state.dash) state.dash.meta_checking = false;
        renderPreservingMetaScroll();
        throw err;
      }
    } else if (a === 'meta-check-stop') {
      try {
        await api('/metadata/stop', { method: 'POST' });
      } catch (_err) { /* ignore */ }
      if (metaCheckAbort) {
        metaCheckAbort.abort();
        metaCheckAbort = null;
      }
      if (metaWatchTimer) {
        clearInterval(metaWatchTimer);
        metaWatchTimer = null;
      }
      state.metaChecking = false;
      if (state.metadata) state.metadata.meta_checking = false;
      if (state.dash) state.dash.meta_checking = false;
      invalidate('metadata');
      await Promise.all([loadKey('metadata'), refreshDash()]).catch(() => {});
      toast('Metadata check stopped');
      renderPreservingMetaScroll();
    } else if (a === 'check-meta-dir') {
      const b = state.browse;
      if (!b || !b.rows || !b.rows.length) return;
      const ids = state.fileSel.length ? state.fileSel.slice() : b.rows.map((f) => f.id);
      state.metaChecking = true;
      if (state.dash) {
        state.dash.meta_checking = true;
        state.dash.meta_progress = { running: true, total: ids.length, done: 0, diff: 0, ok: 0, current_file: '' };
      }
      ensureFastBusyPoll();
      render();
      try {
        await api('/metadata/check', {
          method: 'POST',
          body: { ids: ids, dir: b.rel, force: true, background: true },
        });
        watchMetaCheck();
        toast('Checking metadata for ' + ids.length + ' file' + (ids.length === 1 ? '' : 's') + ' (open Meta data tab for live intermediate results)');
      } catch (err) {
        state.metaChecking = false;
        if (state.dash) state.dash.meta_checking = false;
        render();
        toast(err.message);
      }
    } else if (a === 'meta-vis-all') {
      state.metaVisibleFields = ALL_META_FIELD_KEYS.slice();
      saveStoredMetaVis(state.metaVisibleFields);
      persistMetaUiPrefs();
      render();
    } else if (a === 'meta-cols-reset') {
      state.metaColWidths = Object.assign({}, DEFAULT_META_COLS);
      saveStoredMetaCols(state.metaColWidths);
      persistMetaUiPrefs();
      toast('Column widths reset');
      render();
    } else if (a === 'meta-bulk-toggle') {
      state.metaBulkOpen = !state.metaBulkOpen;
      render();
    } else if (a === 'meta-select-all-shown') {
      const m = state.metadata || {};
      const visible = filterAndSortMetaFiles(m.files || [], m.options || {});
      state.metaSel = visible.map((f) => f.id);
      render();
    } else if (a === 'meta-bulk-reset') {
      state.metaBulkFields = {};
      render();
    } else if (a === 'meta-bulk-stage' || a === 'meta-bulk-apply-now') {
      const ids = state.metaSel.slice();
      const enabledKeys = META_FIELDS.map((x) => x[0]).filter((k) => getBulkFieldDraft(k).enabled);
      if (!ids.length || !enabledKeys.length) return;
      ensureMetaFieldsVisible(enabledKeys);
      const valuesMap = buildBulkValuesMap(enabledKeys);
      if (a === 'meta-bulk-stage') {
        ids.forEach((id) => state.metaTouchedFiles.add(id));
        await queueMetaSelect({ ids: ids, values: valuesMap });
        enabledKeys.forEach((k) => { getBulkFieldDraft(k).dirty = false; });
        const mNow = state.metadata || {};
        const visAfter = filterAndSortMetaFiles(mNow.files || [], mNow.options || {});
        const visSet = new Set(visAfter.map((f) => f.id));
        if (!ids.some((id) => visSet.has(id)) && state.metaFilterStatus !== 'all') {
          state.metaFilterStatus = 'all';
        }
        state.metaSel = ids.slice();
        const paramLabels = enabledKeys.map((k) => META_FIELD_MAP[k] || k).join(', ');
        toast('Selected updated ' + paramLabels + ' in list for ' + ids.length + ' file' + (ids.length === 1 ? '' : 's') + ' · press "Update Selected (' + ids.length + ')" to write ID3 tags');
        renderPreservingMetaScroll();
      } else {
        const busy = startBusy('Writing ' + enabledKeys.length + ' parameter(s) to ' + ids.length + ' selected file(s)', ids.length);
        const elapsed = tickBusy(busy);
        try {
          await queueMetaSelect({ ids: ids, values: valuesMap });
          enabledKeys.forEach((k) => { getBulkFieldDraft(k).dirty = false; });
          const r = await api('/metadata/apply', {
            method: 'POST',
            body: { ids: ids, scope: 'file', overrides: valuesMap, fields: enabledKeys },
          });
          ids.forEach((id) => state.metaTouchedFiles.delete(id));
          bumpCoverCache();
          invalidate('metadata');
          invalidate('coverParked');
          state.browse.stale = true;
          await Promise.all([
            loadKey('metadata'),
            refreshDash(),
            state.browse.rel ? loadBrowse(state.browse.rel, true) : Promise.resolve(),
          ]);
          const warnNote = r.errors && r.errors.length ? ' · Note: ' + r.errors[0] : '';
          toast('Updated ID3 tags in ' + r.updated + ' file' + (r.updated === 1 ? '' : 's') + ' (date/time stamp preserved)' + warnNote);
        } finally {
          elapsed.stop();
          endBusy(busy);
        }
        renderPreservingMetaScroll();
      }
    } else if (a === 'meta-pick') {
      const fid = Number(el.dataset.file);
      const field = el.dataset.field;
      const choice = el.dataset.choice;
      const editKey = fid + ':' + field;
      delete state.metaEditing[editKey];
      state.metaTouchedFiles.add(fid);
      const item = state.metadata && state.metadata.files && state.metadata.files.find((x) => x.id === fid);
      const fo = item && item.fields && item.fields[field];
      if (fo) {
        if (choice === 'discogs' && !fo.discogs) return;
        if (choice === 'musicbrainz' && !fo.musicbrainz) return;
        if (choice === 'open' && !fo.open) return;
        if (choice === 'path' && !fo.path) return;
        fo.choice = choice;
      }
      renderPreservingMetaScroll();
      await queueMetaSelect({ ids: [fid], field: field, choice: choice });
      renderPreservingMetaScroll();
    } else if (a === 'meta-edit-toggle') {
      const fid = Number(el.dataset.file);
      const field = el.dataset.field;
      const editKey = fid + ':' + field;
      if (state.metaEditing[editKey]) delete state.metaEditing[editKey];
      else state.metaEditing[editKey] = true;
      renderPreservingMetaScroll();
      const inp = document.querySelector('[data-meta-custom-input="' + editKey + '"]');
      if (inp) { inp.focus(); inp.select(); }
    } else if (a === 'meta-save-custom') {
      const fid = Number(el.dataset.file);
      const field = el.dataset.field;
      const editKey = fid + ':' + field;
      const inp = document.querySelector('[data-meta-custom-input="' + editKey + '"]');
      const val = inp ? inp.value.trim() : '';
      delete state.metaEditing[editKey];
      state.metaTouchedFiles.add(fid);
      const item = state.metadata && state.metadata.files && state.metadata.files.find((x) => x.id === fid);
      if (item && item.fields && item.fields[field]) {
        item.fields[field].choice = 'custom';
        item.fields[field].custom_value = val;
      }
      renderPreservingMetaScroll();
      await queueMetaSelect({ ids: [fid], field: field, choice: 'custom', custom_value: val });
      renderPreservingMetaScroll();
    } else if (a === 'meta-opt-search-from') {
      const mode = el.dataset.mode || 'auto';
      const saved = await api('/metadata/options', { method: 'PUT', body: { searchFrom: mode } });
      if (state.metadata) state.metadata.options = saved;
      if (state.dash) state.dash.search_from = (saved && saved.search_from) || mode;
      const modeLabels = { auto: 'Auto (smart)', file: 'From file (folder / file name)', metadata: 'From meta data (file tags)' };
      toast('Artist / Title search source set to: ' + (modeLabels[mode] || mode));
      if (currentRoute() === 'metadata') renderPreservingMetaScroll();
      else render();
    } else if (a === 'meta-bulk-artist-title') {
      const choice = el.dataset.choice || 'path';
      const ids = state.metaSel.slice();
      if (!ids.length) {
        toast('Select one or more files in column 1 first');
        return;
      }
      ensureMetaFieldsVisible(['artist', 'title']);
      ids.forEach((id) => state.metaTouchedFiles.add(id));
      await queueMetaSelect({
        ids: ids,
        values: {
          artist: { choice: choice, value: '' },
          title: { choice: choice, value: '' },
        },
      });
      const mNow = state.metadata || {};
      const visAfter = filterAndSortMetaFiles(mNow.files || [], mNow.options || {});
      const visSet = new Set(visAfter.map((f) => f.id));
      if (!ids.some((id) => visSet.has(id)) && state.metaFilterStatus !== 'all') {
        state.metaFilterStatus = 'all';
      }
      state.metaSel = ids.slice();
      toast('Selected Artist & Title from ' + (choice === 'path' ? 'File (folder / file name)' : 'Meta data (current tags)') + ' on ' + ids.length + ' file' + (ids.length === 1 ? '' : 's') + ' · press "Update Selected (' + ids.length + ')" to write ID3 tags');
      renderPreservingMetaScroll();
    } else if (a === 'meta-bulk-choice') {
      const choice = el.dataset.choice || 'online';
      const ids = state.metaSel.slice();
      if (!ids.length) {
        toast('Select one or more files in column 1 first');
        return;
      }
      const activeFields = getEnabledVisibleMetaFields();
      const valuesPayload = {};
      activeFields.forEach((k) => {
        valuesPayload[k] = { choice: choice, value: '' };
      });
      ids.forEach((id) => state.metaTouchedFiles.add(id));
      if (state.metadata && Array.isArray(state.metadata.files)) {
        const idSet = new Set(ids);
        state.metadata.files.forEach((item) => {
          if (!idSet.has(item.id) || !item.fields) return;
          activeFields.forEach((k) => {
            const fo = item.fields[k];
            if (!fo) return;
            if (choice === 'discogs' && fo.discogs) fo.choice = 'discogs';
            else if (choice === 'musicbrainz' && fo.musicbrainz) fo.choice = 'musicbrainz';
            else if (choice === 'open' && fo.open) fo.choice = 'open';
            else if (choice === 'online' && fo.online) fo.choice = 'online';
            else if (choice === 'path' && fo.path) fo.choice = 'path';
            else if (choice === 'current') fo.choice = 'current';
          });
        });
      }
      renderPreservingMetaScroll();
      await queueMetaSelect({ ids: ids, values: valuesPayload });
      const mNow = state.metadata || {};
      const visAfter = filterAndSortMetaFiles(mNow.files || [], mNow.options || {});
      const visSet = new Set(visAfter.map((f) => f.id));
      if (!ids.some((id) => visSet.has(id)) && state.metaFilterStatus !== 'all') {
        state.metaFilterStatus = 'all';
      }
      state.metaSel = ids.slice();
      const srcNames = { discogs: 'Discogs', musicbrainz: 'MusicBrainz', open: 'Open (iTunes)', path: 'Folder / File', current: 'Current tag' };
      toast('Selected ' + (srcNames[choice] || choice) + ' for ' + activeFields.length + ' enabled parameter(s) on ' + ids.length + ' file' + (ids.length === 1 ? '' : 's') + ' · press "Update Selected (' + ids.length + ')" to write ID3 tags');
      renderPreservingMetaScroll();
    } else if (a === 'meta-clear-sel') {
      state.metaSel = [];
      renderPreservingMetaScroll();
    } else if (a === 'meta-apply-one' || a === 'meta-apply-selected' || a === 'meta-apply-visible') {
      let ids = [];
      if (a === 'meta-apply-one') {
        ids = [Number(el.dataset.file)];
      } else if (a === 'meta-apply-selected') {
        ids = state.metaSel.slice();
      } else {
        const m = state.metadata || {};
        ids = filterAndSortMetaFiles(m.files || [], m.options || {}).map((f) => f.id);
      }
      if (!ids.length) return;
      const busy = startBusy('Writing ID3 tags to ' + ids.length + ' file' + (ids.length === 1 ? '' : 's') + ' (preserving file date/time stamp)', ids.length);
      const elapsed = tickBusy(busy);
      try {
        // If user edited fields in Bulk edit parameters and directly clicked "Update Selected ()", apply those bulk edits too
        if (a === 'meta-apply-selected' && state.metaBulkOpen) {
          const dirtyBulkKeys = META_FIELDS.map((x) => x[0]).filter((k) => {
            const bf = getBulkFieldDraft(k);
            return bf.enabled && bf.dirty;
          });
          if (dirtyBulkKeys.length) {
            ensureMetaFieldsVisible(dirtyBulkKeys);
            const valuesMap = buildBulkValuesMap(dirtyBulkKeys);
            await queueMetaSelect({ ids: ids, values: valuesMap });
            dirtyBulkKeys.forEach((k) => { getBulkFieldDraft(k).dirty = false; });
          }
        }
        const r = await api('/metadata/apply', { method: 'POST', body: { ids: ids, scope: 'file' } });
        ids.forEach((id) => state.metaTouchedFiles.delete(id));
        bumpCoverCache();
        invalidate('metadata');
        invalidate('coverParked');
        state.browse.stale = true;
        await Promise.all([
          loadKey('metadata'),
          refreshDash(),
          state.browse.rel ? loadBrowse(state.browse.rel, true) : Promise.resolve(),
        ]);
        const warnNote = r.errors && r.errors.length ? ' · Note: ' + r.errors[0] : '';
        if (r.updated > 0) {
          toast('Updated ID3 tags in ' + r.updated + ' file' + (r.updated === 1 ? '' : 's') + ' (file date/time stamp kept unchanged)' + warnNote);
        } else {
          toast('All selected ID3 tags already match the file(s) on disk' + warnNote);
        }
      } finally {
        elapsed.stop();
        endBusy(busy);
      }
      renderPreservingMetaScroll();
    } else if (a === 'open-meta-modal') {
      const f = state.browse.rows && state.browse.rows[Number(el.dataset.idx)];
      if (f) {
        await openMetaModal(f.id, { name: f.name, relpath: f.relpath, dir: state.browse.rel || '' });
      }
    } else if (a === 'open-meta-by-id') {
      const fid = Number(el.dataset.file);
      const f = state.metadata && state.metadata.files && state.metadata.files.find((x) => x.id === fid);
      await openMetaModal(fid, f ? { name: f.name, relpath: f.relpath, dir: f.dir } : {});
    } else if (a === 'meta-modal-close') {
      closeMetaModal();
    } else if (a === 'meta-modal-query-mode') {
      const mode = el.dataset.mode || 'auto';
      META_MODAL.queryMode = mode;
      const f = (META_MODAL.item && META_MODAL.item.fields) || {};
      if (mode === 'file') {
        META_MODAL.artist = (f.artist && f.artist.path) || '';
        META_MODAL.album = (f.album && f.album.path) || '';
        META_MODAL.title = (f.title && f.title.path) || '';
      } else if (mode === 'metadata') {
        META_MODAL.artist = (f.artist && f.artist.current) || '';
        META_MODAL.album = (f.album && f.album.current) || '';
        META_MODAL.title = (f.title && f.title.current) || '';
      }
      await runMetaModalCheck(false, mode);
    } else if (a === 'meta-modal-search') {
      await runMetaModalCheck(true);
    } else if (a === 'meta-modal-pick') {
      const fid = Number(el.dataset.file);
      const field = el.dataset.field;
      const choice = el.dataset.choice;
      const editKey = 'modal:' + fid + ':' + field;
      delete state.metaEditing[editKey];
      state.metaTouchedFiles.add(fid);
      const fo = META_MODAL.item && META_MODAL.item.fields && META_MODAL.item.fields[field];
      if (fo) {
        if (choice === 'discogs' && !fo.discogs) return;
        if (choice === 'musicbrainz' && !fo.musicbrainz) return;
        if (choice === 'open' && !fo.open) return;
        if (choice === 'path' && !fo.path) return;
        fo.choice = choice;
      }
      renderMetaModal();
      await queueMetaSelect({ ids: [fid], field: field, choice: choice }, { updateModal: true });
      renderMetaModal();
      if (currentRoute() === 'metadata') renderPreservingMetaScroll();
    } else if (a === 'meta-modal-edit-toggle') {
      const fid = Number(el.dataset.file);
      const field = el.dataset.field;
      const editKey = 'modal:' + fid + ':' + field;
      if (state.metaEditing[editKey]) delete state.metaEditing[editKey];
      else state.metaEditing[editKey] = true;
      renderMetaModal();
      const inp = document.querySelector('[data-meta-custom-input="' + editKey + '"]');
      if (inp) { inp.focus(); inp.select(); }
    } else if (a === 'meta-modal-save-custom') {
      const fid = Number(el.dataset.file);
      const field = el.dataset.field;
      const editKey = 'modal:' + fid + ':' + field;
      const inp = document.querySelector('[data-meta-custom-input="' + editKey + '"]');
      const val = inp ? inp.value.trim() : '';
      delete state.metaEditing[editKey];
      state.metaTouchedFiles.add(fid);
      if (META_MODAL.item && META_MODAL.item.fields && META_MODAL.item.fields[field]) {
        META_MODAL.item.fields[field].choice = 'custom';
        META_MODAL.item.fields[field].custom_value = val;
      }
      renderMetaModal();
      await queueMetaSelect({ ids: [fid], field: field, choice: 'custom', custom_value: val }, { updateModal: true });
      renderMetaModal();
      if (currentRoute() === 'metadata') renderPreservingMetaScroll();
    } else if (a === 'meta-modal-bulk') {
      const choice = el.dataset.choice || 'online';
      if (META_MODAL.item && META_MODAL.item.fields) {
        state.metaTouchedFiles.add(META_MODAL.fileId);
        const opts = (state.metadata && state.metadata.options) || {};
        const enabledKeys = Array.isArray(opts.fields) && opts.fields.length ? opts.fields : ALL_META_FIELD_KEYS;
        const valuesPayload = {};
        enabledKeys.forEach((k) => {
          valuesPayload[k] = { choice: choice, value: '' };
          const fo = META_MODAL.item.fields[k];
          if (!fo) return;
          if (choice === 'discogs' && fo.discogs) fo.choice = 'discogs';
          else if (choice === 'musicbrainz' && fo.musicbrainz) fo.choice = 'musicbrainz';
          else if (choice === 'open' && fo.open) fo.choice = 'open';
          else if (choice === 'online' && fo.online) fo.choice = 'online';
          else if (choice === 'path' && fo.path) fo.choice = 'path';
          else if (choice === 'current') fo.choice = 'current';
        });
        renderMetaModal();
        await queueMetaSelect({ ids: [META_MODAL.fileId], values: valuesPayload }, { updateModal: true });
        renderMetaModal();
        if (currentRoute() === 'metadata') renderPreservingMetaScroll();
      }
    } else if (a === 'meta-modal-apply') {
      await applyMetaFromModal(el.dataset.scope || 'file');
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
        invalidate('metadata');
        invalidate('parked:' + t);
        invalidate('coverParked');
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
      const t = state.media;
      const f = ((state.parked[t] && state.parked[t].files) || []).find((x) => String(x.id) === String(el.dataset.parkSlider));
      const busy = startBusy('Adjusting change · ' + (f ? f.name : 'parked file'));
      try {
        await api('/parked/' + el.dataset.parkSlider + '/adjust', { method: 'POST', body: { value: Number(el.value) } });
        state.dragging = false;
        invalidate('parked:' + t);
        await loadKey('parked:' + t);
      } finally {
        endBusy(busy, 500);
      }
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

    if (el.dataset.fileCheck !== undefined || el.dataset.fileAll !== undefined) {
      if (!committed) return;
      const list = (state.browse && state.browse.rows) || [];
      if (el.dataset.fileAll !== undefined) {
        state.fileSel = el.checked ? list.map((f) => f.id) : [];
      } else {
        const id = Number(el.dataset.fileCheck);
        state.fileSel = el.checked ? state.fileSel.concat(id) : state.fileSel.filter((x) => x !== id);
      }
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
      if (el.dataset.scope === 'general' && key === 'theme' && committed) {
        applyTheme(el.value, { saveLocal: true, saveServer: true });
      }
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
      return;
    }

    if (el.dataset.coverInput !== undefined) {
      const k = el.dataset.coverInput;
      if (k === 'artist') COVER.artist = el.value;
      else if (k === 'album') COVER.album = el.value;
      else if (k === 'title') COVER.title = el.value;
      else if (k === 'source') COVER.source = el.value;
      return;
    }

    if (el.dataset.metaModalInput !== undefined) {
      const k = el.dataset.metaModalInput;
      if (k === 'artist') META_MODAL.artist = el.value;
      else if (k === 'album') META_MODAL.album = el.value;
      else if (k === 'title') META_MODAL.title = el.value;
      return;
    }

    if (el.dataset.metaCheck !== undefined || el.dataset.metaAll !== undefined) {
      if (!committed) return;
      const m = state.metadata || {};
      const visible = filterAndSortMetaFiles(m.files || [], m.options || {});
      if (el.dataset.metaAll !== undefined) {
        state.metaSel = el.checked ? visible.map((f) => f.id) : [];
      } else {
        const id = Number(el.dataset.metaCheck);
        state.metaSel = el.checked ? state.metaSel.concat(id) : state.metaSel.filter((x) => x !== id);
      }
      render();
      return;
    }

    if (el.dataset.metaFilter !== undefined) {
      const k = el.dataset.metaFilter;
      if (k === 'search') {
        state.metaSearch = el.value;
        render();
        const inp = document.getElementById('meta-flt-search');
        if (inp) { inp.focus(); inp.setSelectionRange(inp.value.length, inp.value.length); }
        return;
      }
      if (!committed) return;
      if (k === 'status') {
        state.metaFilterStatus = el.value;
        state.metaTouchedFiles.clear();
      } else if (k === 'field') {
        state.metaFilterField = el.value;
        state.metaTouchedFiles.clear();
      } else if (k === 'folder') {
        state.metaFilterFolder = el.value;
        state.metaTouchedFiles.clear();
      } else if (k === 'sort') state.metaSort = el.value;
      render();
      return;
    }

    if (el.dataset.metaOptField !== undefined || el.dataset.metaOptSource !== undefined || el.dataset.metaOptPath !== undefined) {
      if (!committed) return;
      const m = state.metadata || {};
      const opts = Object.assign({
        fields: META_FIELDS.map((x) => x[0]),
        check_path_vs_tags: true,
        sources: ['discogs', 'musicbrainz', 'open'],
      }, m.options || {});
      if (el.dataset.metaOptField !== undefined) {
        const fld = el.dataset.metaOptField;
        const next = el.checked
          ? Array.from(new Set(opts.fields.concat(fld)))
          : opts.fields.filter((x) => x !== fld);
        if (next.length) opts.fields = next;
      } else if (el.dataset.metaOptSource !== undefined) {
        const src = el.dataset.metaOptSource;
        const next = el.checked
          ? Array.from(new Set(opts.sources.concat(src)))
          : opts.sources.filter((x) => x !== src);
        if (next.length) opts.sources = next;
      } else if (el.dataset.metaOptPath !== undefined) {
        opts.check_path_vs_tags = !!el.checked;
      }
      const saved = await api('/metadata/options', { method: 'PUT', body: opts });
      if (state.metadata) state.metadata.options = saved;
      await loadKey('metadata');
      render();
      return;
    }

    if (el.dataset.metaVisField !== undefined) {
      if (!committed) return;
      const fld = el.dataset.metaVisField;
      const next = el.checked
        ? ALL_META_FIELD_KEYS.filter((k) => state.metaVisibleFields.includes(k) || k === fld)
        : state.metaVisibleFields.filter((k) => k !== fld);
      if (!next.length) {
        el.checked = true;
        toast('Keep at least one parameter visible in the list');
        return;
      }
      if (!el.checked) {
        state.metaForceShowFields = state.metaForceShowFields.filter((k) => k !== fld);
      }
      state.metaVisibleFields = next;
      saveStoredMetaVis(next);
      persistMetaUiPrefs();
      render();
      return;
    }

    if (el.dataset.metaOnlyDiff !== undefined) {
      if (!committed) return;
      state.metaOnlyDiffRows = !!el.checked;
      persistMetaUiPrefs();
      render();
      return;
    }

    if (el.dataset.metaBulkEnable !== undefined) {
      if (!committed) return;
      const bf = getBulkFieldDraft(el.dataset.metaBulkEnable);
      bf.enabled = !!el.checked;
      bf.dirty = !!el.checked;
      render();
      return;
    }

    if (el.dataset.metaBulkMode !== undefined) {
      if (!committed) return;
      const bf = getBulkFieldDraft(el.dataset.metaBulkMode);
      bf.mode = el.value;
      bf.enabled = true;
      bf.dirty = true;
      render();
      return;
    }

    if (el.dataset.metaBulkVal !== undefined) {
      const k = el.dataset.metaBulkVal;
      const bf = getBulkFieldDraft(k);
      bf.value = el.value;
      bf.dirty = true;
      if (el.value.trim()) {
        bf.enabled = true;
      }
      syncBulkPanelDom();
      return;
    }
  }

  document.addEventListener('scroll', (e) => {
    const el = e.target;
    if (!el || !el.classList || !el.matches) return;
    if (el.matches('.split-dirs > .tree')) {
      const m = el.dataset.media || state.media;
      state.treeScroll[m] = { top: el.scrollTop, left: el.scrollLeft };
    } else if (el.matches('.split-dirs > .files-pane')) {
      const m = el.dataset.media || state.media;
      state.filesScroll[m] = {
        rel: el.dataset.rel || null,
        top: el.scrollTop,
        left: el.scrollLeft,
      };
    } else if (el.id === 'meta-table-wrap') {
      state.metaTableScroll = { top: el.scrollTop, left: el.scrollLeft };
      syncMetaStickyHeader();
    }
  }, true);

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

  function positionTip(tipEl) {
    if (!tipEl) return;
    const body = tipEl.querySelector('.tip-body');
    if (!body) return;
    const rect = tipEl.getBoundingClientRect();
    const inTh = !!tipEl.closest('th, .page-head');
    const txHost = tipEl.closest('thead.is-stuck');
    const hostRect = txHost ? txHost.getBoundingClientRect() : { left: 0, top: 0 };
    body.style.position = 'fixed';
    body.style.transform = 'none';
    body.style.zIndex = '200';
    const w = Math.min(320, Math.max(180, body.offsetWidth || 260));
    const left = Math.max(12, Math.min(window.innerWidth - w - 12, Math.round(rect.left + rect.width / 2 - w / 2)));
    body.style.left = (left - Math.round(hostRect.left)) + 'px';
    if (inTh || rect.top < 100) {
      body.style.top = (Math.round(rect.bottom + 8) - Math.round(hostRect.top)) + 'px';
      body.style.bottom = 'auto';
    } else {
      body.style.bottom = Math.round(window.innerHeight - rect.top + 8) + 'px';
      body.style.top = 'auto';
    }
  }

  document.addEventListener('mouseover', (e) => {
    const tipEl = e.target.closest && e.target.closest('.tip');
    if (tipEl) positionTip(tipEl);
  });
  document.addEventListener('focusin', (e) => {
    const tipEl = e.target.closest && e.target.closest('.tip');
    if (tipEl) positionTip(tipEl);
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

  // Popup modals (#cover-modal, #metadata-modal, #editor) intentionally stay open on backdrop click.

  /* ------------------------------------------------ cover hover zoom preview */
  const coverZoomTip = document.getElementById('cover-zoom-tip');
  const coverZoomImg = document.getElementById('cover-zoom-img');
  const coverZoomCaption = document.getElementById('cover-zoom-caption');
  let activeCoverZoomEl = null;

  function findCoverZoomTarget(target) {
    if (!target || !target.closest) return null;
    const explicit = target.closest('[data-cover-zoom]');
    if (explicit && explicit.dataset.coverZoom) return explicit;
    const img = target.closest('img.cover-mini-thumb, img.meta-cover-mini, img.cover-cand-img, img.cover-current-img');
    if (img && img.getAttribute('src')) return img;
    return null;
  }

  function positionCoverZoom(clientX, clientY) {
    if (!coverZoomTip || coverZoomTip.hidden) return;
    const pad = 16;
    const boxW = coverZoomTip.offsetWidth || 308;
    const boxH = coverZoomTip.offsetHeight || 320;
    let left = clientX + 18;
    let top = clientY - Math.round(boxH / 2);
    if (left + boxW + pad > window.innerWidth) {
      left = Math.max(pad, clientX - boxW - 18);
    }
    top = Math.max(pad, Math.min(window.innerHeight - boxH - pad, top));
    coverZoomTip.style.left = Math.round(left) + 'px';
    coverZoomTip.style.top = Math.round(top) + 'px';
  }

  function hideCoverZoom() {
    activeCoverZoomEl = null;
    if (coverZoomTip) coverZoomTip.hidden = true;
  }

  document.addEventListener('mouseover', (ev) => {
    const el = findCoverZoomTarget(ev.target);
    if (!el || !coverZoomTip || !coverZoomImg) {
      if (activeCoverZoomEl && (!ev.target || !activeCoverZoomEl.contains(ev.target))) hideCoverZoom();
      return;
    }
    if (el === activeCoverZoomEl && !coverZoomTip.hidden) return;
    const src = el.dataset.coverZoom || el.getAttribute('src') || '';
    if (!src) { hideCoverZoom(); return; }
    activeCoverZoomEl = el;
    if (coverZoomImg.getAttribute('src') !== src) coverZoomImg.setAttribute('src', src);
    const cap = el.dataset.coverCaption || el.getAttribute('alt') || '';
    if (coverZoomCaption) {
      coverZoomCaption.textContent = cap;
      coverZoomCaption.hidden = !cap;
    }
    coverZoomTip.hidden = false;
    positionCoverZoom(ev.clientX, ev.clientY);
  });

  document.addEventListener('mousemove', (ev) => {
    if (!activeCoverZoomEl || !coverZoomTip || coverZoomTip.hidden) return;
    if (!document.body.contains(activeCoverZoomEl)) {
      hideCoverZoom();
      return;
    }
    positionCoverZoom(ev.clientX, ev.clientY);
  });

  document.addEventListener('mouseout', (ev) => {
    if (!activeCoverZoomEl) return;
    const rel = ev.relatedTarget;
    if (!rel || !activeCoverZoomEl.contains(rel)) {
      const next = findCoverZoomTarget(rel);
      if (!next) hideCoverZoom();
    }
  });

  window.addEventListener('scroll', hideCoverZoom, true);
  document.addEventListener('pointerdown', hideCoverZoom);

  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && ev.target && ev.target.dataset && ev.target.dataset.metaBulkVal) {
      ev.preventDefault();
      const stageBtn = document.querySelector('[data-action="meta-bulk-stage"]');
      if (stageBtn && !stageBtn.disabled) stageBtn.click();
      return;
    }
    if (ev.key === 'Enter' && ev.target && ev.target.dataset && ev.target.dataset.metaCustomInput) {
      ev.preventDefault();
      const editKey = ev.target.dataset.metaCustomInput;
      const isModal = editKey.startsWith('modal:');
      const clean = isModal ? editKey.slice(6) : editKey;
      const parts = clean.split(':');
      const fid = Number(parts[0]);
      const field = parts[1];
      const btn = document.querySelector('[data-action="' + (isModal ? 'meta-modal-save-custom' : 'meta-save-custom') + '"][data-file="' + fid + '"][data-field="' + field + '"]');
      if (btn) btn.click();
      return;
    }
    if (META_MODAL.open) {
      if (ev.key === 'Escape') {
        ev.preventDefault();
        closeMetaModal();
      } else if (ev.key === 'Enter' && ev.target && ev.target.dataset && ev.target.dataset.metaModalInput) {
        ev.preventDefault();
        runMetaModalCheck(true);
      }
      return;
    }
    if (!COVER.open) return;
    if (ev.key === 'Escape') {
      ev.preventDefault();
      closeCoverModal();
    } else if (ev.key === 'Enter' && ev.target && ev.target.dataset && ev.target.dataset.coverInput) {
      ev.preventDefault();
      runCoverModalSearch(true);
    }
  });

  /* ------------------------------------------------ metadata drag-and-drop & column resizing */
  let activeMetaDrag = null;

  function clearMetaDropHighlights() {
    document.querySelectorAll('.is-drop-row').forEach((el) => el.classList.remove('is-drop-row'));
    document.querySelectorAll('.is-drop-cell').forEach((el) => el.classList.remove('is-drop-cell'));
  }

  document.addEventListener('dragstart', (ev) => {
    const dragEl = ev.target && ev.target.closest && ev.target.closest('[data-drag-meta]');
    if (!dragEl) return;
    try {
      const payload = JSON.parse(dragEl.dataset.dragMeta);
      activeMetaDrag = payload;
      if (ev.dataTransfer) {
        ev.dataTransfer.effectAllowed = 'copy';
        ev.dataTransfer.setData('application/x-audio-norm-meta', dragEl.dataset.dragMeta);
        ev.dataTransfer.setData('text/plain', String(payload.value || ''));
      }
    } catch (_err) {
      activeMetaDrag = null;
    }
  });

  document.addEventListener('dragover', (ev) => {
    const bulkInp = ev.target && ev.target.closest && ev.target.closest('input[data-meta-bulk-val], input[data-meta-custom-input]');
    if (bulkInp) {
      ev.preventDefault();
      if (ev.dataTransfer) ev.dataTransfer.dropEffect = 'copy';
      return;
    }
    const cell = ev.target && ev.target.closest && ev.target.closest('td[data-drop-field]');
    const row = ev.target && ev.target.closest && ev.target.closest('tr[data-meta-row-field]');
    if (!cell && !row) return;
    ev.preventDefault();
    if (ev.dataTransfer) ev.dataTransfer.dropEffect = 'copy';
    clearMetaDropHighlights();
    if (row) row.classList.add('is-drop-row');
    if (cell) cell.classList.add('is-drop-cell');
  });

  document.addEventListener('dragleave', (ev) => {
    const related = ev.relatedTarget;
    if (!related || !(related.closest && (related.closest('td[data-drop-field]') || related.closest('tr[data-meta-row-field]')))) {
      clearMetaDropHighlights();
    }
  });

  document.addEventListener('dragend', () => {
    activeMetaDrag = null;
    clearMetaDropHighlights();
  });

  document.addEventListener('drop', (ev) => {
    clearMetaDropHighlights();
    const bulkInp = ev.target && ev.target.closest && ev.target.closest('input[data-meta-bulk-val]');
    if (bulkInp) {
      let val = '';
      if (ev.dataTransfer) {
        const raw = ev.dataTransfer.getData('application/x-audio-norm-meta');
        if (raw) {
          try { val = JSON.parse(raw).value || ''; } catch (_e) { /* ignore */ }
        }
        if (!val) val = ev.dataTransfer.getData('text/plain') || '';
      } else if (activeMetaDrag) {
        val = activeMetaDrag.value || '';
      }
      if (val) {
        ev.preventDefault();
        const k = bulkInp.dataset.metaBulkVal;
        const bf = getBulkFieldDraft(k);
        bf.mode = 'custom';
        bf.value = val;
        bf.enabled = true;
        bf.dirty = true;
        render();
        toast('Dropped value into bulk ' + (META_FIELD_MAP[k] || k));
      }
      activeMetaDrag = null;
      return;
    }

    const cell = ev.target && ev.target.closest && ev.target.closest('td[data-drop-field]');
    const row = ev.target && ev.target.closest && ev.target.closest('tr[data-meta-row-field]');
    if (!cell && !row) return;

    let payload = activeMetaDrag;
    if (ev.dataTransfer) {
      const raw = ev.dataTransfer.getData('application/x-audio-norm-meta');
      if (raw) {
        try { payload = JSON.parse(raw); } catch (_e) { /* ignore */ }
      }
      if (!payload) {
        const txt = ev.dataTransfer.getData('text/plain');
        if (txt) payload = { value: txt };
      }
    }
    activeMetaDrag = null;
    if (!payload || payload.value === undefined) return;
    ev.preventDefault();

    const targetFileId = Number((cell && cell.dataset.dropFile) || (row && row.dataset.metaRowFile));
    const targetField = (cell && cell.dataset.dropField) || (row && row.dataset.metaRowField);
    const inModal = !!((cell && cell.dataset.dropModal) || (row && row.dataset.dropModal));
    const draggedVal = String(payload.value || '').trim();
    if (!targetFileId || !targetField) return;

    if (targetField === 'cover' && draggedVal && !/^(https?:\/\/|local:\/\/|data:)/i.test(draggedVal)) {
      toast('Cover picture requires an image URL');
      return;
    }

    if (inModal && META_MODAL.open && META_MODAL.item && META_MODAL.item.id === targetFileId) {
      state.metaTouchedFiles.add(targetFileId);
      if (META_MODAL.item.fields && META_MODAL.item.fields[targetField]) {
        META_MODAL.item.fields[targetField].choice = 'custom';
        META_MODAL.item.fields[targetField].custom_value = draggedVal;
      }
      delete state.metaEditing['modal:' + targetFileId + ':' + targetField];
      renderMetaModal();
      queueMetaSelect(
        { ids: [targetFileId], field: targetField, choice: 'custom', custom_value: draggedVal },
        { updateModal: true }
      )
        .then(() => {
          renderMetaModal();
          if (currentRoute() === 'metadata') renderPreservingMetaScroll();
        })
        .catch(SAVE_ERR);
      toast('Assigned to ' + (META_FIELD_MAP[targetField] || targetField));
      return;
    }

    delete state.metaEditing[targetFileId + ':' + targetField];
    state.metaTouchedFiles.add(targetFileId);
    const item = state.metadata && state.metadata.files && state.metadata.files.find((x) => x.id === targetFileId);
    if (item && item.fields && item.fields[targetField]) {
      item.fields[targetField].choice = 'custom';
      item.fields[targetField].custom_value = draggedVal;
    }
    renderPreservingMetaScroll();
    queueMetaSelect({
      ids: [targetFileId],
      field: targetField,
      choice: 'custom',
      custom_value: draggedVal,
    })
      .then(() => renderPreservingMetaScroll())
      .catch(SAVE_ERR);
    toast('Assigned "' + (draggedVal.length > 36 ? draggedVal.slice(0, 36) + '…' : draggedVal) + '" to ' + (META_FIELD_MAP[targetField] || targetField));
  });

  // Column resizing in 2 · Metadata differences & selection
  let colResizeState = null;
  document.addEventListener('pointerdown', (ev) => {
    const grip = ev.target && ev.target.closest && ev.target.closest('.col-resizer[data-resize-col]');
    if (!grip) return;
    ev.preventDefault();
    ev.stopPropagation();
    const colKey = grip.dataset.resizeCol;
    const spec = META_COL_SPECS.find((s) => s[0] === colKey);
    const minW = spec ? spec[2] : 60;
    const startW = state.metaColWidths[colKey] || DEFAULT_META_COLS[colKey] || 150;
    grip.classList.add('is-active');
    document.body.classList.add('is-col-resizing');
    colResizeState = { colKey, minW, startX: ev.clientX, startW, grip };
  });

  window.addEventListener('pointermove', (ev) => {
    if (!colResizeState) return;
    const dx = ev.clientX - colResizeState.startX;
    const nextW = Math.max(colResizeState.minW, Math.min(960, Math.round(colResizeState.startW + dx)));
    state.metaColWidths[colResizeState.colKey] = nextW;
    const tbl = document.getElementById('meta-diff-table');
    if (tbl) {
      const colEl = tbl.querySelector('col[data-col-key="' + colResizeState.colKey + '"]');
      if (colEl) colEl.style.width = nextW + 'px';
      const total = META_COL_SPECS.reduce((acc, [ck]) => acc + (state.metaColWidths[ck] || DEFAULT_META_COLS[ck]), 0);
      tbl.style.width = total + 'px';
    }
  });

  window.addEventListener('pointerup', () => {
    if (!colResizeState) return;
    if (colResizeState.grip) colResizeState.grip.classList.remove('is-active');
    document.body.classList.remove('is-col-resizing');
    saveStoredMetaCols(state.metaColWidths);
    persistMetaUiPrefs();
    colResizeState = null;
  });

  document.addEventListener('dblclick', (ev) => {
    const grip = ev.target && ev.target.closest && ev.target.closest('.col-resizer[data-resize-col]');
    if (!grip) return;
    const colKey = grip.dataset.resizeCol;
    state.metaColWidths[colKey] = DEFAULT_META_COLS[colKey] || 180;
    saveStoredMetaCols(state.metaColWidths);
    persistMetaUiPrefs();
    render();
  });

  function syncMetaStickyHeader() {
    const tbl = document.getElementById('meta-diff-table');
    if (!tbl) return;
    const thead = tbl.querySelector('thead');
    const tw = document.getElementById('meta-table-wrap');
    if (!thead || !tw) return;
    const rect = tw.getBoundingClientRect();
    const headH = thead.offsetHeight || 40;
    const maxOffset = Math.max(0, rect.height - headH);
    const winPush = rect.top < 0 ? Math.round(-rect.top) : 0;
    const y = Math.min(maxOffset, winPush + Math.round(tw.scrollTop || 0));
    if (y > 0) {
      thead.style.setProperty('--meta-head-y', y + 'px');
      thead.classList.add('is-stuck');
    } else {
      thead.style.removeProperty('--meta-head-y');
      thead.classList.remove('is-stuck');
    }
  }

  function syncJumpTop() {
    const btn = document.getElementById('jump-top-btn');
    if (btn) btn.classList.toggle('is-scrolled', window.scrollY > 80);
    syncMetaStickyHeader();
  }
  window.addEventListener('scroll', syncJumpTop, { passive: true });
  window.addEventListener('resize', syncMetaStickyHeader, { passive: true });

  /* ---------------------------------------------------------------- start */
  window.addEventListener('hashchange', () => {
    const route = currentRoute();
    if (route !== 'directories') state.baselineFrom = null;      // the baseline banner lasts while on Directories
    if (route !== 'settings' && state.settings) state.draft = clone(state.settings);   // unsaved edits are dropped
    if (route === 'parked') {
      invalidate('parked:' + state.media);  // fresh list each time the page is opened
      invalidate('coverParked');
    }
    if (route === 'metadata') {
      invalidate('metadata');
    }
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
    if (state.metaChecking || (state.dash && state.dash.meta_checking)) {
      watchMetaCheck();
    }
  }, 2500);

  applyTheme(themePref, { saveLocal: false, saveServer: false });
  applyMenu();
  initPlayer();
  initEditor();
  syncJumpTop();
  if (currentRoute() === 'directories') openDirectories();   // also when the app opens on this page
  render();
  if (!state.loaded.dashboard && !state.pending.dashboard) {
    refreshDash().catch(() => {});
  }
})();
