/*!
 * Audio Normalizer — Lovelace card  (v1.0.0)
 *
 * Shows every value published by the audio-normalizer status sensor:
 * run status, progress, audio and video counters, parked files, errors,
 * current file, last run, target loudness and next schedule change.
 *
 * It reads ONE entity (default sensor.audio_normalizer). The entity is created
 * by the REST sensor snippet from the Audio Normalizer web page; its attributes
 * hold the complete payload.
 *
 * Usage in a dashboard (raw YAML):
 *   type: custom:audio-normalizer-card
 *   entity: sensor.audio_normalizer
 *   title: Audio Normalizer
 *   show_audio: true
 *   show_video: true
 *   show_details: true
 *
 * Install: see lovelace/README.md
 * No external dependencies. Follows the Home Assistant theme variables.
 */
(function () {
  'use strict';

  const CARD_VERSION = '1.0.0';
  const CARD_TAG = 'audio-normalizer-card';
  const DEFAULTS = {
    entity: 'sensor.audio_normalizer',
    title: 'Audio Normalizer',
    show_audio: true,
    show_video: true,
    show_details: true,
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
      color: var(--primary-text-color, #1b1b1b);
      border: 1px solid var(--divider-color, #d3d3cf);
      border-radius: 0;
      padding: 16px;
      font-family: var(--paper-font-body1_-_font-family, system-ui, -apple-system, Segoe UI, Roboto, sans-serif);
      font-size: 14px;
      line-height: 1.45;
      box-sizing: border-box;
    }
    *, *::before, *::after { border-radius: 0; box-sizing: border-box; }
    .head { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 14px; flex-wrap: wrap; }
    .title { font-size: 16px; font-weight: 600; }
    .status { display: inline-flex; align-items: center; gap: 7px; font-size: 12px; text-transform: uppercase; letter-spacing: .06em; color: var(--secondary-text-color, #6b6b67); }
    .status::before { content: ""; width: 8px; height: 8px; background: #9a9a95; }
    .status.s-running { color: var(--primary-text-color, #1b1b1b); }
    .status.s-running::before { background: var(--primary-text-color, #2e2e2e); }
    .status.s-paused::before { background: transparent; border: 1px solid currentColor; }
    .bar { height: 6px; background: var(--divider-color, #dcdcd8); }
    .bar > div { height: 100%; background: var(--primary-text-color, #2e2e2e); transition: width .5s linear; }
    .meta { display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; font-size: 12px; color: var(--secondary-text-color, #6b6b67); margin: 8px 0 16px; }
    .meta b { color: var(--primary-text-color, #1b1b1b); font-weight: 600; }
    .blocks { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 12px; }
    .block { border: 1px solid var(--divider-color, #d3d3cf); padding: 12px; }
    .block-head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 10px; }
    .block-title { font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: .05em; }
    .win { font-size: 12px; color: var(--secondary-text-color, #6b6b67); }
    .tiles { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); border-top: 1px solid var(--divider-color, #d3d3cf); border-left: 1px solid var(--divider-color, #d3d3cf); }
    .tile { padding: 8px; border-right: 1px solid var(--divider-color, #d3d3cf); border-bottom: 1px solid var(--divider-color, #d3d3cf); min-width: 0; }
    .tl { font-size: 11px; color: var(--secondary-text-color, #6b6b67); line-height: 1.3; overflow-wrap: anywhere; }
    .tv { font-size: 18px; line-height: 1.25; font-weight: 600; font-variant-numeric: tabular-nums; }
    .rows { margin: 10px 0 0; }
    .rows > div { display: flex; justify-content: space-between; gap: 12px; padding: 6px 0; border-top: 1px solid var(--divider-color, #d3d3cf); font-size: 13px; }
    .rows dt { color: var(--secondary-text-color, #6b6b67); }
    .rows dd { margin: 0; text-align: right; font-variant-numeric: tabular-nums; }
    .foot { margin-top: 14px; display: grid; gap: 6px; font-size: 13px; border-top: 1px solid var(--divider-color, #d3d3cf); padding-top: 12px; }
    .foot > div { display: flex; justify-content: space-between; gap: 12px; }
    .foot dt { color: var(--secondary-text-color, #6b6b67); }
    .foot dd { margin: 0; text-align: right; min-width: 0; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .msg { padding: 12px; border: 1px dashed var(--divider-color, #b4b4af); font-size: 13px; line-height: 1.5; }
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
      this._render();
    }

    set hass(hass) {
      this._hass = hass;
      this._render();
    }

    getCardSize() { return 9; }

    static getStubConfig() { return Object.assign({}, DEFAULTS); }

    _tile(label, value) {
      return '<div class="tile"><div class="tl">' + esc(label) + '</div><div class="tv">' + value + '</div></div>';
    }

    _block(title, d, c) {
      return '<section class="block">'
        + '<div class="block-head"><span class="block-title">' + esc(title) + '</span>'
        + '<span class="win">' + (d.in_schedule_window ? 'In window' : 'Outside window') + '</span></div>'
        + '<div class="tiles">'
        + this._tile('Folders on', num(d.directories_enabled) + ' / ' + num(d.directories_total))
        + this._tile('Files in scope', num(d.files_total))
        + this._tile('To do', num(d.files_pending))
        + this._tile('Checked', num(d.files_checked))
        + this._tile('Changed', num(d.files_changed))
        + this._tile('Parked', num(d.files_parked))
        + '</div>'
        + (c.show_details
          ? '<dl class="rows"><div><dt>Target loudness</dt><dd>' + lufs(d.target_lufs) + '</dd></div>'
            + '<div><dt>Next schedule change</dt><dd>' + when(d.next_schedule_change) + '</dd></div></dl>'
          : '')
        + '</section>';
    }

    _body(st, c) {
      const a = st.attributes || {};
      const audio = a.audio || {};
      const video = a.video || {};
      const status = String(st.state || '');
      const pct = Math.min(100, Math.max(0, Number(a.progress_percent) || 0));

      const blocks = [];
      if (c.show_audio) blocks.push(this._block('Audio', audio, c));
      if (c.show_video) blocks.push(this._block('Video', video, c));

      return '<div class="head"><div class="title">' + esc(c.title) + '</div>'
        + '<div class="status s-' + esc(status) + '">' + esc(cap(status)) + '</div></div>'
        + '<div class="bar" role="progressbar" aria-valuenow="' + pct + '" aria-valuemin="0" aria-valuemax="100"><div style="width:' + pct + '%"></div></div>'
        + '<div class="meta"><span><b>' + pct.toFixed(1) + '%</b> complete</span>'
        + '<span>Errors <b>' + num(a.errors) + '</b></span></div>'
        + '<div class="blocks">' + blocks.join('') + '</div>'
        + (c.show_details
          ? '<dl class="foot">'
            + '<div><dt>Current file</dt><dd class="mono" title="' + esc(a.current_file || '') + '">' + esc(a.current_file || '—') + '</dd></div>'
            + '<div><dt>Last run finished</dt><dd>' + when(a.last_run_finished) + '</dd></div>'
            + '</dl>'
          : '');
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

  console.info('%c AUDIO-NORMALIZER-CARD %c v' + CARD_VERSION + ' ', 'color:#fff;background:#2e2e2e', 'color:#2e2e2e;background:#e7e7e5');
})();
