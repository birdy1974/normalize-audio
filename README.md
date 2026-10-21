# audio-normalizer

Self-hosted web app that brings the loudness of audio files (MP3, FLAC, M4A, WAV, OGG, WMA, and the audio
track of AVI, MKV and MP4 files) in a chosen folder tree to one common level, without changing the audio quality.
Designed for Docker on a Synology DS918+.

**Status: version 0.1.0, implemented.** Checked in a Linux sandbox: 43 automated tests (decision rules, pipeline with
real ffmpeg, REST API), a live run on a demo library, and the web UI in headless Chromium.
**Not yet verified:** the Docker image build, the `aacgain` package in Debian, the mp3gain/aacgain command-line
options on a real file, a run on the NAS, and the Home Assistant integration. See "Verification status" below.

- [`requirements.md`](requirements.md): the original brief.
- [`docs/ARCHITECTURE_PROPOSAL.md`](docs/ARCHITECTURE_PROPOSAL.md): structure, findings and the decisions Q1–Q8.
- [`mockup/`](mockup/README.md): the clickable UI prototype the implementation follows.
- [`lovelace/`](lovelace/README.md): the Home Assistant Lovelace card (also served by the app at `/static/`).

## What it does

- Scans two base locations (audio and video) including all sub-folders. New and changed files are found
  periodically and after a restart (the state is kept in SQLite).
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
  Lovelace card configuration generated in the UI.

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
| Pipeline with real ffmpeg: volume, re-encode, backup, output, dry run, silent, park/approve, restart, window, folders | Automated tests pass (15) |
| REST API: HA auth and token masking, HA sensor names, folders and tree, settings validation, parked actions | Automated tests pass (17) |
| Web UI against the live API (headless Chromium) | Checked: pages load, no script errors, folder toggle, parked adjust and sort, settings save |
| Docker image build | **Not verified** (no Docker in the sandbox) |
| `aacgain` in Debian bookworm | **Not verified**. Build falls back to no AAC gain if the package is missing |
| mp3gain and aacgain command-line options (`-q`, `-p`, `-g`) | **Not verified** on real files (tools not installed in the sandbox) |
| Run on the DS918+ with a real library | **Not verified** |
| Home Assistant REST sensors and the Lovelace card in a real Home Assistant | **Not verified** |
