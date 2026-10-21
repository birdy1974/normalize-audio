# audio-normalizer — architecture proposal

Status: **decisions Q1–Q6 recorded (see §5).** Next gate: review the UI mock-up. Backend implementation starts after your sign-off on the mock-up. Q7–Q8 defaults are assumed. No backend code has been written yet.
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

This is the main product decision — see Q1 in §5.

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
| Lossy gain | **mp3gain / aacgain** (default lossy strategy, Q1) | Lossless gain change for MP3/AAC. |
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
4. **Apply** — write to a temporary file, verify with ffprobe (duration, stream count, codec unchanged), copy timestamps and permissions, then atomic rename. Write mode is a user setting (Q2, confirmed), default **in place, no backup**:
   - *Replace in place* (default): temp file in the same folder, then atomic rename over the original.
   - *Replace in place, keep backup*: original copied to `/config/backups` first (needs extra disk space).
   - *Separate output folder*: normalized copy written to a second mounted folder; the library is never modified.
5. **Record** — update the database; the new fingerprint prevents re-processing after restart.

Crash safety: temp files are named with a fixed prefix and removed at startup; unfinished jobs are re-queued.

### 2.3 Format handling (default proposal)

| Input | Default method | Quality impact |
|---|---|---|
| FLAC, WAV, ALAC | Re-encode losslessly at the same bit depth | None (gain only) |
| MP3 | `mp3gain` (global gain field) — **default** | None (1.5 dB steps → target accuracy ±0.75 dB) |
| AAC / M4A | `aacgain` | None (same 1.5 dB steps) |
| OGG / Opus | Opus/Vorbis gain header (R128_TRACK_GAIN) | None — to be verified in the prototype |
| WMA | Re-encode at same bitrate | Small generation loss (or skip, see Q1) |
| AVI / MKV / MP4 | Audio re-encoded, **video stream copied** | Audio only; video untouched |

The exact tool mapping is a prototype task; the table is the target behaviour.

**Lossy strategy is a user setting** (Q1, confirmed): *Gain only* (default) · *Re-encode at same bitrate* · *Skip lossy files*. Lossless files are always handled by lossless gain.

---

### 2.4 Audio / video split (prepared in the mock-up)

Audio and video are a first-class dimension of the data model, not a side feature. Each media type has its own:

| Item | Audio | Video |
|---|---|---|
| Library mount (compose) | `/volume1/music` → `/data/audio` | `/volume1/video` → `/data/video` |
| Folder selection (tree) | own enable/disable state | own enable/disable state |
| Schedule (7×24 grid) | own windows | own windows |
| Settings | target, true peak, threshold, override, lossy strategy, write mode | target, true peak, threshold, override, write mode; video stream option (copy now, GPU later) |
| Counters and parked queue | own | own |
| Home Assistant sensors | `audio.*` | `video.*` |

Shared across both types: scanner, fingerprint database, job queue, run state machine (Start / Pause / Stop), scan interval, window-close behaviour, dry run, logs.

Code structure to match:

```
app/core/media/
├── base.py        # MediaType: mount, extensions, scanner settings, pipeline
├── audio.py       # pipeline: ebur128 analysis, gain-only / re-encode / skip strategies
└── video.py       # pipeline: audio-track normalization, video stream copied
```

Database: every file row carries `media_type` and the directory table carries `media_type`, so parked queues, counters and HA values are always filtered by type. A future VAAPI video re-encode is added as a new strategy in `video.py` without changing the rest of the app.

Compose sketch (two mounts, one container):

```yaml
volumes:
  - /volume1/music:/data/audio:rw
  - /volume1/video:/data/video:rw
  - /volume1/docker/audio-normalizer/config:/config:rw
```

## 3. Feature mapping to requirements

| Requirement | Solution |
|---|---|
| Normalize all files incl. sub-directories | Recursive scanner over enabled directories (tree in UI). |
| Same sound level for all files | Target loudness (LUFS) slider; every file is brought to it. |
| Quality not changed | Default gain-only strategy for lossy files; lossless gain for FLAC/WAV; user can choose re-encode or skip (see §1.3, Q1). |
| Schedule (days, weeks, times) | Weekly 7×24 grid with presets; "finish current file" or "pause now" at window end. |
| New files detected, persistent | Periodic scan + SQLite fingerprints; state survives restarts. |
| Warning + park large changes, no waiting | Files above the threshold get status `parked`; the run continues; review page. |
| Override confirmation for large changes | Toggle in settings (applies large changes automatically). |
| Home Assistant sensors | REST API, grouped by media type: status, progress %, errors, and per type: folders, files in scope, pending, checked, changed, parked, target LUFS, next schedule change. |
| Dashboard (4 numbers) | Per media type: folders/files in scope · files to do · files checked · files actually changed. |
| Start / stop / browse | Run controls on dashboard; tree browser on Directories page. |
| Base location set by user | Compose volume mount; shown in UI, not changeable in the browser (safety). |
| Write behaviour | Settings: in place (default) · in place with backup · separate output folder (Q2). |
| Docker on DS918+ | `docker-compose.yml` for Container Manager; `/dev/dri` optional. |

---

## 4. Home Assistant interface (proposed)

Endpoint: `GET http://<NAS-IP>:8080/api/v1/ha/status` with header `Authorization: Bearer <token>`.

Example payload (structure taken from the mock-up; values are sample numbers):

```json
{
  "status": "running",
  "progress_percent": 52.6,
  "errors": 8,
  "current_file": "/data/audio/Music/Rock/04 Cold Front 1.mp3",
  "last_run_finished": "2026-10-20T23:05:15Z",
  "audio": {
    "in_schedule_window": false,
    "directories_total": 16,
    "directories_enabled": 15,
    "files_total": 431,
    "files_pending": 201,
    "files_checked": 230,
    "files_changed": 74,
    "files_parked": 35,
    "target_lufs": -16.0,
    "next_schedule_change": "2026-10-21T22:00:00Z"
  },
  "video": {
    "in_schedule_window": false,
    "directories_total": 6,
    "directories_enabled": 5,
    "files_total": 61,
    "files_pending": 32,
    "files_checked": 29,
    "files_changed": 12,
    "files_parked": 8,
    "target_lufs": -16.0,
    "next_schedule_change": "2026-10-24T00:00:00Z"
  }
}
```

Sensors use dotted paths, for example `audio.files_total` → `{{ value_json.audio.files_total }}`. The mock-up's Home Assistant page groups sensors into Overall, Audio and Video.

Home Assistant side: RESTful sensors (generated YAML snippet is shown on the Home Assistant page of the mock-up). Decided for v1: **REST only** (Q4). MQTT discovery or a HACS custom integration are not planned for v1.

Security: the endpoint is protected by the bearer token. The web UI itself has no login (Q5), so keep the port on the local network and do not forward it to the internet.

---

## 5. Decisions

Answers from the confirmation round:

| # | Question | Decision |
|---|---|---|
| Q1 | Lossy files (MP3, AAC, WMA, OGG): how to change the level? | **User-selectable in Settings** — *Gain only* (default), *Re-encode at same bitrate*, *Skip lossy files*. |
| Q2 | Write mode? | **User-selectable in Settings** — *Replace in place* (**default, no backup**), *Replace in place with backup*, *Separate output folder*. |
| Q3 | Video files and VAAPI? | Audio track normalized, video copied. **No VAAPI in v1.** GPU video re-encoding is an optional later feature. |
| Q4 | Home Assistant connection? | **REST endpoint only** (bearer token, RESTful sensors). |
| Q5 | Web page access? | **No login — local network only.** The port must not be exposed to the internet. |
| Q6 | Schedule window closes during a file? | **Finish the current file, then pause** (default). |

Still open — defaults assumed unless you object:

| # | Question | Default assumed |
|---|---|---|
| Q7 | Keep the original file's modification time after normalizing? | Yes |
| Q8 | Backend language? | Python 3.12 + FastAPI |

Also interpreted as stated: dashboard item 3 "files checked" = files analysed; item 4 "files actually checked" = **files actually changed** (level adjusted).

## 6. Delivery plan (after confirmation)

1. **Prototype (on the DS918+):** measure ffmpeg analysis speed; check `/dev/dri` and `vainfo`; verify gain-only MP3/AAC path and lossless FLAC path with sample files. Output: short report.
2. **Core engine:** scanner, analyser, normalizer, scheduler, SQLite state with `media_type` on directories and files, unit tests with generated audio.
3. **API + web UI:** build from the approved mock-up. No login (Q5), so network exposure must be documented clearly in the install guide.
4. **Home Assistant endpoint** and sensor definitions.
5. **Docker image** (amd64) and `docker-compose.yml`; install guide for Container Manager.
6. **Hardening:** dry-run mode, logs, health check, backup/restore of the config volume.
7. **Later (optional):** GPU video re-encoding with VAAPI (Q3), once the audio engine is stable.

Dry-run mode (analyse and report, never write) is recommended as the first thing to run on a real library.
