# audio-normalizer — architecture proposal

Status: **proposal, awaiting confirmation.** No backend code has been written yet.
The UI mock-up lives in [`mockup/`](../mockup/README.md).

Target: Synology DS918+ (Intel Celeron J3455, Apollo Lake, Intel HD Graphics 500, x86_64) running Docker / Container Manager.

---

## 1. Key findings that shape the design

### 1.1 VAAPI cannot accelerate the audio normalization itself

VAAPI (and Quick Sync) accelerate **video** encode/decode only. Loudness analysis and gain changes are digital signal processing on the CPU (ffmpeg / libebur128), so the GPU does not help with the core task.

What the Apollo Lake GPU does handle, per the Intel VAAPI support matrix and the FFmpeg QuickSync wiki:

| Function | DS918+ (Gen9 / Apollo Lake) |
|---|---|
| H.264 decode / encode | Yes (8-bit) |
| HEVC 8-bit and 10-bit decode | Yes |
| HEVC encode | Limited (8-bit 4:2:0 on Gen9) |
| VP9 / AV1 | Decode of VP9 8-bit only; no AV1 |

**Consequence:** For AVI / MKV / MP4 files, the video stream can be **copied** (`-c:v copy`) and only the audio track is re-encoded. The video is never decoded or re-encoded, so VAAPI has nothing to speed up. Recommendation: **no VAAPI in v1.** Keep the container GPU-ready (`/dev/dri` passthrough, optional) for a future "video re-encode" feature, and run a hardware probe (`vainfo`) in diagnostics. This is the honest answer to "if possible use VAAPI": it is not possible for this task, but it is also not needed.

### 1.2 Performance is the real constraint on this hardware

The J3455 is a 4-core, low-power CPU (1.5 GHz base, 2.3 GHz burst). The first full scan of a large library (EBU R128 analysis of every file) will be CPU-bound and take a long time. The design therefore:

- runs a configurable **concurrency limit** (default 1 file at a time, max 2);
- processes only inside the **schedule windows**;
- caches analysis results so that each file is analysed once per version.

Real throughput must be measured on the DS918+ during the prototype phase. No speed numbers are assumed here.

### 1.3 "Audio quality must not change" needs a precise definition

Changing the level and changing the quality are different things:

- **Pure gain** keeps the audio data and dynamics intact. A lossless container (FLAC, WAV, ALAC) can be re-written at exactly the same quality.
- **Lossy formats** (MP3, AAC, OGG, WMA) can only be changed without quality loss by editing the codec's global gain field (`mp3gain`, `aacgain`), in steps of about 1.5 dB. Re-encoding a lossy file always adds a small generation loss.
- **`loudnorm` in dynamic mode** changes dynamics. For this app the linear mode (`loudnorm=linear=true`) or a plain gain (`volume=…dB`) must be used instead.

This is the main product decision — see question Q1 below.

---

## 2. Proposed structure

```
audio-normalizer/
├── docker-compose.yml          # NAS-ready: library mount, config volume, port, optional /dev/dri
├── Dockerfile                  # python:3.12-slim + ffmpeg + mp3gain/aacgain (amd64)
├── README.md                   # install on DS918+ (Container Manager), first-run guide
├── requirements.md             # the original brief
├── docs/ARCHITECTURE_PROPOSAL.md
├── mockup/                     # static UI prototype (done, no backend)
├── app/
│   ├── main.py                 # FastAPI app: REST API + serves the web UI
│   ├── api/                    # web UI routes, HA endpoint, auth
│   ├── core/
│   │   ├── scanner.py          # walks enabled directories, detects new/changed files
│   │   ├── analyzer.py         # ffmpeg ebur128: integrated LUFS + true peak
│   │   ├── normalizer.py       # gain decision, parking, lossless/lossy strategy, atomic replace
│   │   ├── scheduler.py        # weekly time windows, pause/resume
│   │   └── runner.py           # job queue, worker pool, start / pause / stop state machine
│   ├── store/                  # SQLite (file state, run history, settings)
│   ├── ha/                     # Home Assistant payload + optional MQTT discovery
│   └── web/                    # front-end (vanilla JS, same approach as the mock-up)
└── tests/                      # unit tests with generated sample audio, no real library needed
```

### 2.1 Components

| Component | Choice | Reason |
|---|---|---|
| Backend | **Python 3.12 + FastAPI** | Good ffmpeg / audio tooling, one process, easy to test. |
| Audio engine | **ffmpeg** (`ebur128`, `volume`, copy-video mapping) | Handles every container requested (mp3, flac, avi, mkv, m4a, ogg, wav, wma). |
| Lossy gain | **mp3gain / aacgain** (only if Q1 = gain-only) | Lossless gain change for MP3/AAC. |
| State | **SQLite** in `/config` | Survives restarts; stores per-file fingerprint (path, size, mtime, hash), status, gain, history. |
| Scheduler | Own weekly-window logic (not cron) | Simple "days + hours" grid in the UI; no external daemon. |
| Frontend | Vanilla JS (as in the mock-up) | No build step, small image, easy to maintain on a NAS. |
| HA integration | REST endpoint `/api/v1/ha/status` (bearer token) | Works with HA's built-in RESTful sensors without custom code. |

### 2.2 Processing pipeline (per file)

1. **Detect** — periodic scan of enabled directories (default every 15 min). A file is "new/changed" when path, size or mtime differ from the database. inotify is **not** used as the primary mechanism: it is unreliable on Synology shares and bind mounts. (Optional inotify later.)
2. **Analyse** — ffmpeg `ebur128` → integrated loudness (LUFS) and true peak (dBTP). Result cached by file fingerprint.
3. **Decide** — `gain = target − measured`, limited so the true peak stays below the ceiling (default −1 dBTP).
   - |gain| < 0.5 dB → `ok` (no change).
   - |gain| > threshold (default 6 dB) and override off → **`parked`** (not changed; the run continues with the next file).
   - otherwise → `changed`.
4. **Apply** — write to a temporary file in the same folder, verify with ffprobe (duration, stream count, codec unchanged), copy timestamps and permissions, then atomic rename. Optional backup of the original first.
5. **Record** — update the database; the new fingerprint prevents re-processing after restart.

Crash safety: temp files are named with a fixed prefix and removed at startup; unfinished jobs are re-queued.

### 2.3 Format handling (default proposal)

| Input | Default method | Quality impact |
|---|---|---|
| FLAC, WAV, ALAC | Re-encode losslessly at the same bit depth | None (gain only) |
| MP3 | `mp3gain` (global gain field) | None (1.5 dB steps → target accuracy ±0.75 dB) |
| AAC / M4A | `aacgain` | None (same 1.5 dB steps) |
| OGG / Opus | Opus/Vorbis gain header (R128_TRACK_GAIN) | None — to be verified in the prototype |
| WMA | Re-encode at same bitrate | Small generation loss (alternative: skip, see Q1) |
| AVI / MKV / MP4 | Audio re-encoded, **video stream copied** | Audio only; video untouched |

The exact tool mapping is a prototype task; the table is the target behaviour.

---

## 3. Feature mapping to requirements

| Requirement | Solution |
|---|---|
| Normalize all files incl. sub-directories | Recursive scanner over enabled directories (tree in UI). |
| Same sound level for all files | Target loudness (LUFS) slider; every file is brought to it. |
| Quality not changed | Gain-only / lossless strategies (see §1.3 and Q1). |
| Schedule (days, weeks, times) | Weekly 7×24 grid with presets; "finish current file" or "pause now" at window end. |
| New files detected, persistent | Periodic scan + SQLite fingerprints; state survives restarts. |
| Warning + park large changes, no waiting | Files above the threshold get status `parked`; the run continues; review page. |
| Override confirmation for large changes | Toggle in settings (applies large changes automatically). |
| Home Assistant sensors | REST API: status, progress %, files in scope, pending, checked, changed, parked, errors, current file, last run, next schedule change, target LUFS. |
| Dashboard (4 numbers) | Directories/files in scope · files to do · files checked · files actually changed. |
| Start / stop / browse | Run controls on dashboard; tree browser on Directories page. |
| Base location set by user | Compose volume mount; shown in UI, not changeable in the browser (safety). |
| Docker on DS918+ | `docker-compose.yml` for Container Manager; `/dev/dri` optional. |

---

## 4. Home Assistant interface (proposed)

Endpoint: `GET http://<NAS-IP>:8080/api/v1/ha/status` with header `Authorization: Bearer <token>`.

Example payload (values come from the mock-up state):

```json
{
  "status": "running",
  "progress_percent": 42.5,
  "directories_total": 15,
  "directories_enabled": 9,
  "files_total": 547,
  "files_pending": 270,
  "files_checked": 277,
  "files_changed": 118,
  "files_parked": 14,
  "errors": 3,
  "current_file": "Podcasts/Tech Weekly/07 Blue Hour 1.mp3",
  "last_run_finished": "2026-10-20T06:00:12Z",
  "next_schedule_change": "2026-10-21T22:00:00Z",
  "target_lufs": -16.0,
  "in_schedule_window": true
}
```

Home Assistant side: RESTful sensors (generated YAML snippet is shown in the mock-up's settings page). Optional later: MQTT discovery or a HACS custom integration (see Q5).

---

## 5. Open questions

The mock-up assumes the defaults below. They are asked explicitly in the chat before implementation.

| # | Question | Default assumed |
|---|---|---|
| Q1 | How should lossy files (MP3, AAC, WMA, OGG) be changed? | Gain-only (no re-encode) |
| Q2 | Write mode: replace files in place, or write to a separate output folder? | In place, atomic replace, optional backup |
| Q3 | Video files (AVI/MKV/MP4): normalize their audio only? Use VAAPI? | Audio only, video copied, no VAAPI in v1 |
| Q4 | Home Assistant: REST polling only, or also MQTT discovery / custom integration? | REST |
| Q5 | Web UI access: LAN only without login, or username/password? | Username/password |
| Q6 | Schedule window end: finish the current file, or pause immediately? | Finish current file |
| Q7 | Preserve file timestamps (mtime) after changing a file? | Yes |
| Q8 | Backend language: Python (FastAPI) or Node.js? | Python |

Also to confirm: the 4th dashboard item "how many files are actually checked" is read as **files actually changed** (level adjusted). The 3rd item ("files checked") is read as **files analysed**.

---

## 6. Delivery plan (after confirmation)

1. **Prototype (on the DS918+):** measure ffmpeg analysis speed; check `/dev/dri` and `vainfo`; verify gain-only MP3/AAC path and lossless FLAC path with sample files. Output: short report.
2. **Core engine:** scanner, analyser, normalizer, scheduler, SQLite state, unit tests with generated audio.
3. **API + web UI:** build from the approved mock-up; authentication per Q5.
4. **Home Assistant endpoint** and sensor definitions.
5. **Docker image** (amd64) and `docker-compose.yml`; install guide for Container Manager.
6. **Hardening:** dry-run mode, logs, health check, backup/restore of the config volume.

Dry-run mode (analyse and report, never write) is recommended as the first thing to run on a real library.
