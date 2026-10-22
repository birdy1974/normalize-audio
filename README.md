# audio-normalizer

Self-hosted web app that brings the loudness of audio files (MP3, FLAC, M4A, WAV, OGG, WMA, and the audio
track of AVI, MKV and MP4 files) in a chosen folder tree to one common level, without changing the audio quality.
Designed for Docker on a Synology DS918+.

**Status: version 0.1.0, implemented.** Checked in a Linux sandbox: 76 automated tests (decision rules, pipeline with
real ffmpeg, REST API), a live run on a demo library, and the web UI in headless Chromium.
**Not yet verified:** the Docker image build, the `aacgain` package in Debian, the mp3gain/aacgain command-line
options on a real file, a run on the NAS, and the Home Assistant integration. See "Verification status" below.

- [`requirements.md`](requirements.md): the original brief.
- [`docs/ARCHITECTURE_PROPOSAL.md`](docs/ARCHITECTURE_PROPOSAL.md): structure, findings and the decisions Q1–Q8.
- [`mockup/`](mockup/README.md): the clickable UI prototype the implementation follows.
- [`lovelace/`](lovelace/README.md): the Home Assistant Lovelace card (also served by the app at `/static/`).

## What it does

- Three modes, selected with buttons on the dashboard or in Settings (Mode card), and shown in Home Assistant:
  - **Automatic** — every *Look for new or changed files every* N minutes the folders are scanned and new or changed
    files are checked and normalized by themselves, within the schedule and the folders that are included.
  - **Bulk** (default) — there is no periodic scan and nothing is normalized until you press Start. A bulk run works
    through the pending files in enabled folders, within their schedule, and stops by itself; files that appear later
    wait for a manual scan and the next Start.
  - **Off** — nothing is scanned and nothing is normalized. The app only shows what it already knows; Rescan,
    Check level and the audio editor still work when you use them, and Start is refused.
- Scans two base locations (audio and video) including all sub-folders. Automatic mode finds new and changed files on
  the interval above. In every mode, opening the Directories tab or using its Rescan button scans manually (the state
  is kept in SQLite). A manual Rescan also resets the automatic interval, so the next automatic scan starts a full
  interval later.
- Measures integrated loudness (EBU R128) and true peak with ffmpeg, then changes the level to the target
  (default −16 LUFS, −1 dBTP ceiling).
- Lossless files (FLAC, WAV) use ffmpeg `volume` at the original sample format. Lossy MP3 and AAC use
  mp3gain/aacgain (1.5 dB steps, no re-encode). Other lossy files are parked for a decision (re-encode at the
  same bitrate, or skip).
- Video files keep the video stream; only the audio track is changed.
- Changes above the threshold (default 6 dB) are parked, not applied. The run continues. Approve, skip or
  adjust them by ±0.1 dB in the web UI. An override switch applies them automatically.
- Each change writes a temporary file, verifies it, and replaces the original atomically. The original
  modification time is restored. Write modes: in place (default), in place with a backup, or a separate output folder.
- Dry run: analyse and report only.
- Schedule: a 7×24 grid per media type. When a window closes during a file, the file is finished and the run pauses
  (or pauses at once, selectable).
- Home Assistant: a token-protected REST endpoint (`/api/v1/ha/status`) for RESTful sensors, with the YAML and the
  Lovelace card configuration generated in the UI. The payload carries `mode` (off / bulk / auto), `scan_every`,
  `last_scan` and `next_scan` next to the run status and the counters; the new *Mode*, *Last file scan* and
  *Next file scan* sensors can be enabled with one click.
- An activity indicator in the menu shows on every page what the app is doing right now: scanning the folders,
  checking the level of a file, saving, a running normalization (with the current file) or idle.
- Pre-listen to the files: the Directories tab has a Play button per file and one player at the bottom of the page.
  The file is streamed unchanged, so what you hear is exactly what will be normalized; the player supports seeking.
  Formats the browser cannot decode (WMA, AVI, MKV, …) are shown with the button disabled.
- Edit an audio file in the Directories tab: the Edit button opens a pop-up with the waveform and its levels, a time
  ruler, fade in / fade out sliders and a cut. Drag over the waveform to select a part, then "Cut this part"; the
  preview shows the length the file will get. Zoom with the +/− buttons, Ctrl + wheel or the Fit button, scroll with
  the wheel or the scrollbar under the waveform, and listen to the result before applying it (Space plays, Esc closes).
  The edit is done by ffmpeg on the server in the file itself — with a backup when the write mode says so — and the file
  is measured again in the next run, because fades and cuts change its level. Editing is off in dry run and in the
  output-folder write mode, where it would not change the file you hear.

## Install on the NAS (Container Manager or docker compose)

1. Copy this repository to the NAS (for example `/volume1/docker/audio-normalizer/src`).
2. Edit `docker-compose.yml`: the volume paths, the `user:` (uid:gid of the share owner) and the time zone.
3. Build and start:
   ```bash
   cd /volume1/docker/audio-normalizer/src
   docker compose up -d --build
   ```
4. Open `http://<NAS-IP>:8080` in the browser on your home network. Set the Directories and Schedule first,
   then run a dry run.

**Security:** the web UI has no login, by design (LAN only). Do not forward port 8080 on the router.
The Home Assistant endpoint requires its bearer token; the long-lived Home Assistant access token is stored in the
config volume and is never sent back to the browser.

## Development

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q tests/          # uses ffmpeg from PATH, or the imageio-ffmpeg binary
python -m app                       # run the app on port 8080 (set AUDIO_ROOT, VIDEO_ROOT, CONFIG_DIR)
```

Environment variables: `AUDIO_ROOT`, `VIDEO_ROOT`, `CONFIG_DIR`, `FFMPEG_BIN`, `MP3GAIN_BIN`, `AACGAIN_BIN`,
`CONCURRENCY` (1–2), `TZ`, `PORT`.

## Verification status

| Item | Status |
|---|---|
| Decision rules (gain, park, skip, lossy) | Automated tests pass (13) |
| Pipeline with real ffmpeg: volume, re-encode, backup, output, dry run, silent, park/approve, restart, window, folders, off / bulk / automatic mode and the scan schedule | Automated tests pass (29) |
| REST API: HA auth and token masking, HA sensor names, folders and tree (including rescan and the scan state), file streaming, waveform and info, editing, app modes and the scan schedule, settings validation, parked actions | Automated tests pass (33) |
| Web UI against the live API (headless Chromium) | Checked: pages load, no script errors, folder toggle, parked adjust and sort, settings save |
| Directories rescan | REST endpoint tested; UI checked in jsdom (tab open starts a scan, Rescan button and result message, tree updates) |
| Activity indicator and folder tree layout | UI checked in jsdom (scan, level check with counter, run and idle states; one line per folder) |
| Audio preview player | Stream endpoint tested (bytes, Range/206, path checks); UI checked in jsdom (play, pause, switch file, close, disabled formats, survives re-render) |
| Audio editor (fades, cut, waveform, zoom) | Waveform, info and edit endpoints tested with real ffmpeg on FLAC/MP3/M4A/OGG/WAV/Opus, edges, write modes and path checks; UI checked in jsdom (open, selection, cut, fades, zoom, pan, play, apply, blocked write modes, close) |
| Audio editor on very long files | Waveform of a 30-minute FLAC takes about 1.4 s; shorter views are cached and fetched per window. **Not checked on a real library** |
| Automatic mode (new files normalized without Start) | Pipeline tests (off / bulk / auto, schedule, pause, restart, mode switch) and a live run with a 1-minute scan interval: a file dropped in the folder was found and normalized by itself; in bulk mode it waited for Start; in off mode no scan ran for 72 s. UI checked in jsdom (mode choice, dashboard state, buttons, activity indicator, HA page and card) |
| Docker image build | **Not verified** (no Docker in the sandbox) |
| `aacgain` in Debian bookworm | **Not verified**. Build falls back to no AAC gain if the package is missing |
| mp3gain and aacgain command-line options (`-q`, `-p`, `-g`) | **Not verified** on real files (tools not installed in the sandbox) |
| Run on the DS918+ with a real library | **Not verified** |
| Home Assistant REST sensors and the Lovelace card in a real Home Assistant | **Not verified** |
