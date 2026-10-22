/*!
 * Audio Normalizer — Lovelace card  (v1.2.0)
 *
 * Shows the values published by the audio-normalizer status entity:
 * run status, app mode (off / bulk / automatic) with the last and next file
 * scan, progress, audio and video counters, parked files, errors,
 * target loudness, next schedule change, current file and last run.
 *
 * It reads ONE entity (default sensor.audio_normalizer). Its attributes hold
 * the payload. The optional `fields` list (generated from the enabled sensors on
 * the Home Assistant page) limits the card to the values you chose.
 *
 * Usage in a dashboard (raw YAML):
 *   type: custom:audio-normalizer-card
 *   entity: sensor.audio_normalizer
 *   title: Audio Normalizer
 *   show_audio: true
 *   show_video: true
 *   show_details: true
 *   fields:                       # optional; omit to show everything
 *     - audio.files_total
 *     - progress_percent
 *
 * Install: see lovelace/README.md
 * No external dependencies. Follows the Home Assistant theme variables.
 */
(function () {
  'use strict';

  const CARD_VERSION = '1.2.0';
  const CARD_TAG = 'audio-normalizer-card';
  const MODE_NAMES = { off: 'Off', bulk: 'Bulk', auto: 'Automatic' };

  const DEFAULTS = {
    entity: 'sensor.audio_normalizer',
    title: 'Audio Normalizer',
    show_audio: true,
    show_video: true,
    show_details: true,
    fields: null,
  };

  const esc = (s) => String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
  const num = (n) => (n === null || n === undefined || n === '' || isNaN(Number(n)) ? '—' : Number(n).toLocaleString());
  const lufs = (n) => (n === null || n === undefined || isNaN(Number(n)) ? '—' : Number(n).toFixed(1) + ' LUFS');
  const when = (iso) => {
    if (!iso) return '—';
    const d = new Date(iso);
    return isNaN(d.getTime()) ? esc(iso) : d.toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short' });
  };
  const cap = (s) => (s ? String(s).charAt(0).toUpperCase() + String(s).slice(1) : '—');

  const STYLE = `
    :host { display: block; }
    .card {
      background: var(--ha-card-background, var(--card-background-color, #fbfbfa));
      color: var(--primary-text-color, #1c1d1c);
      border: 1px solid var(--divider-color, #d9d9d5);
      border-radius: 6px;
      padding: 16px;
      font-family: var(--paper-font-body1_-_font-family, system-ui, -apple-system, Segoe UI, Roboto, sans-serif);
      font-size: 14px;
      line-height: 1.45;
      box-sizing: border-box;
    }
    *, *::before, *::after { box-sizing: border-box; }
    .head { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 14px; flex-wrap: wrap; }
    .title { font-size: 16px; font-weight: 600; }
    .status { display: inline-flex; align-items: center; gap: 7px; font-size: 12px; text-transform: uppercase; letter-spacing: .06em; color: var(--secondary-text-color, #6c6d69); }
    .status::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: #9d9e99; }
    .status.s-running { color: #2f7a6b; }
    .status.s-running::before { background: #2f7a6b; }
    .status.s-paused { color: #8a5d10; }
    .status.s-paused::before { background: #8a5d10; }
    .status.s-idle { color: #2f7a6b; }
    .status.s-idle::before { background: #2f7a6b; }
    .status.s-off { color: #9d9e99; }
    .status.s-off::before { background: #9d9e99; }
    .bar { height: 6px; background: var(--divider-color, #dcdcd8); border-radius: 999px; overflow: hidden; }
    .bar > div { height: 100%; background: #2f7a6b; border-radius: 999px; transition: width .5s linear; }
    .meta { display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; font-size: 12px; color: var(--secondary-text-color, #6c6d69); margin: 8px 0 16px; }
    .meta b { color: var(--primary-text-color, #1c1d1c); font-weight: 600; }
    .blocks { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 12px; }
    .block { border: 1px solid var(--divider-color, #d9d9d5); border-radius: 4px; padding: 12px; min-width: 0; }
    .block-head { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; margin-bottom: 10px; flex-wrap: wrap; }
    .block-title { font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: .05em; }
    .win { font-size: 12px; color: var(--secondary-text-color, #6c6d69); }
    .tiles { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); border-top: 1px solid var(--divider-color, #d9d9d5); border-left: 1px solid var(--divider-color, #d9d9d5); border-radius: 4px; overflow: hidden; }
    .tile { padding: 8px; border-right: 1px solid var(--divider-color, #d9d9d5); border-bottom: 1px solid var(--divider-color, #d9d9d5); min-width: 0; }
    .tl { font-size: 11px; color: var(--secondary-text-color, #6c6d69); line-height: 1.3; overflow-wrap: anywhere; }
    .tv { font-size: 18px; line-height: 1.25; font-weight: 600; font-variant-numeric: tabular-nums; }
    .rows { margin: 10px 0 0; }
    .rows > div { display: flex; justify-content: space-between; gap: 12px; padding: 6px 0; border-top: 1px solid var(--divider-color, #d9d9d5); font-size: 13px; }
    .rows dt { color: var(--secondary-text-color, #6c6d69); }
    .rows dd { margin: 0; text-align: right; font-variant-numeric: tabular-nums; }
    .foot { margin-top: 14px; display: grid; gap: 6px; font-size: 13px; border-top: 1px solid var(--divider-color, #d9d9d5); padding-top: 12px; }
    .foot > div { display: flex; justify-content: space-between; gap: 12px; }
    .foot dt { color: var(--secondary-text-color, #6c6d69); }
    .foot dd { margin: 0; text-align: right; min-width: 0; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .msg { padding: 12px; border: 1px dashed var(--divider-color, #b9b9b4); border-radius: 4px; font-size: 13px; line-height: 1.5; }
    code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
  `;

  class AudioNormalizerCard extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: 'open' });
      this._config = null;
      this._hass = null;
    }

    setConfig(config) {
      this._config = Object.assign({}, DEFAULTS, config || {});
      if (this._config.fields !== null && !Array.isArray(this._config.fields)) this._config.fields = null;
      this._render();
    }

    set hass(hass) {
      this._hass = hass;
      this._render();
    }

    getCardSize() { return 9; }

    static getStubConfig() { return Object.assign({}, DEFAULTS); }

    /* true if the value is shown: everything when `fields` is not set */
    _want(c, key) {
      return !c.fields || c.fields.indexOf(key) !== -1;
    }

    _tile(label, value) {
      return '<div class="tile"><div class="tl">' + esc(label) + '</div><div class="tv">' + value + '</div></div>';
    }

    _block(title, t, d, c) {
      const w = (k) => this._want(c, t + '.' + k);
      const tiles = [];
      if (w('directories_enabled') || w('directories_total')) {
        tiles.push(this._tile('Folders on', num(d.directories_enabled) + ' / ' + num(d.directories_total)));
      }
      if (w('files_total')) tiles.push(this._tile('Files in scope', num(d.files_total)));
      if (w('files_pending')) tiles.push(this._tile('To do', num(d.files_pending)));
      if (w('files_checked')) tiles.push(this._tile('Checked', num(d.files_checked)));
      if (w('files_changed')) tiles.push(this._tile('Changed', num(d.files_changed)));
      if (w('files_parked')) tiles.push(this._tile('Parked', num(d.files_parked)));

      const rows = [];
      if (c.show_details && w('target_lufs')) rows.push(['Target loudness', lufs(d.target_lufs)]);
      if (c.show_details && w('next_schedule_change')) rows.push(['Next schedule change', when(d.next_schedule_change)]);

      const showWindow = w('in_schedule_window');
      if (!tiles.length && !rows.length && !showWindow) return '';

      return '<section class="block">'
        + '<div class="block-head"><span class="block-title">' + esc(title) + '</span>'
        + (showWindow ? '<span class="win">' + (d.in_schedule_window ? 'In window' : 'Outside window') + '</span>' : '')
        + '</div>'
        + (tiles.length ? '<div class="tiles">' + tiles.join('') + '</div>' : '')
        + (rows.length
          ? '<dl class="rows">' + rows.map((r) => '<div><dt>' + esc(r[0]) + '</dt><dd>' + r[1] + '</dd></div>').join('') + '</dl>'
          : '')
        + '</section>';
    }

    _body(st, c) {
      const a = st.attributes || {};
      const top = (k) => this._want(c, k);
      const audio = a.audio || {};
      const video = a.video || {};
      const status = String(st.state || '');
      const pct = Math.min(100, Math.max(0, Number(a.progress_percent) || 0));

      const blocks = [];
      if (c.show_audio) { const b = this._block('Audio', 'audio', audio, c); if (b) blocks.push(b); }
      if (c.show_video) { const b = this._block('Video', 'video', video, c); if (b) blocks.push(b); }

      const meta = [];
      if (top('progress_percent')) meta.push('<span><b>' + pct.toFixed(1) + '%</b> complete</span>');
      if (top('errors')) meta.push('<span>Errors <b>' + num(a.errors) + '</b></span>');

      const foot = [];
      if (top('mode')) {
        foot.push('<div><dt>Mode</dt><dd>' + esc(MODE_NAMES[a.mode] || a.mode || '—') + '</dd></div>');
      }
      if (c.show_details && top('last_scan')) {
        foot.push('<div><dt>Last scan</dt><dd>' + when(a.last_scan) + '</dd></div>');
      }
      if (c.show_details && top('next_scan')) {
        foot.push('<div><dt>Next scan</dt><dd>' + when(a.next_scan) + '</dd></div>');
      }
      if (c.show_details && top('current_file')) {
        foot.push('<div><dt>Current file</dt><dd class="mono" title="' + esc(a.current_file || '') + '">' + esc(a.current_file || '—') + '</dd></div>');
      }
      if (c.show_details && top('last_run_finished')) {
        foot.push('<div><dt>Last run finished</dt><dd>' + when(a.last_run_finished) + '</dd></div>');
      }

      return '<div class="head"><div class="title">' + esc(c.title) + '</div>'
        + (top('status') ? '<div class="status s-' + esc(status) + '">' + esc(cap(status)) + '</div>' : '')
        + '</div>'
        + (top('progress_percent')
          ? '<div class="bar" role="progressbar" aria-valuenow="' + pct + '" aria-valuemin="0" aria-valuemax="100"><div style="width:' + pct + '%"></div></div>'
          : '')
        + (meta.length ? '<div class="meta">' + meta.join('') + '</div>' : '<div style="height:8px"></div>')
        + (blocks.length ? '<div class="blocks">' + blocks.join('') + '</div>' : '')
        + (foot.length ? '<dl class="foot">' + foot.join('') + '</dl>' : '');
    }

    _render() {
      if (!this._config) return;
      const c = this._config;
      let body;
      if (!this._hass) {
        body = '<div class="msg">Waiting for Home Assistant…</div>';
      } else {
        const st = this._hass.states && this._hass.states[c.entity];
        if (!st) {
          body = '<div class="msg">Entity <code>' + esc(c.entity) + '</code> was not found. Add the REST sensor from the Audio Normalizer web page (Home Assistant page, YAML snippet) and reload.</div>';
        } else if (st.state === 'unavailable' || st.state === 'unknown') {
          body = '<div class="msg">The entity is <b>' + esc(st.state) + '</b>. Check that the audio-normalizer container is running and that the token is valid.</div>';
        } else {
          body = this._body(st, c);
        }
      }
      this.shadowRoot.innerHTML = '<style>' + STYLE + '</style><div class="card">' + body + '</div>';
    }
  }

  if (!customElements.get(CARD_TAG)) {
    customElements.define(CARD_TAG, AudioNormalizerCard);
  }

  window.customCards = window.customCards || [];
  if (!window.customCards.some((x) => x.type === CARD_TAG)) {
    window.customCards.push({
      type: CARD_TAG,
      name: 'Audio Normalizer',
      description: 'Run status, progress, audio and video counters and parked files from the audio-normalizer sensor.',
      preview: false,
      documentationURL: 'https://github.com/birdy1974/normalize-audio',
    });
  }

  console.info('%c AUDIO-NORMALIZER-CARD %c v' + CARD_VERSION + ' ', 'color:#fff;background:#2f7a6b', 'color:#2f3130;background:#ebebe9');
})();
