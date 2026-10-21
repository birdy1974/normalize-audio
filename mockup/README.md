# audio-normalizer — UI mock-up

A static, clickable prototype of the web interface. It has **no backend**: the library,
run progress and Home Assistant values are simulated in the browser, and nothing is read
from or written to disk.

## Run it

Either open `index.html` directly in a browser, or serve the folder:

```bash
cd mockup
python3 -m http.server 8080
# then open http://localhost:8080
```

## Pages

| Page | What it shows |
|---|---|
| Dashboard | Run status (running / paused / stopped), % complete, Start / Pause / Stop, Browse, and the four counters: directories/files in scope, files to do, files checked, files actually changed. |
| Directories | Tree of the base location with per-folder checkboxes, tri-state parents, Select all / Select none, expand / collapse, and a file browser for the selected folder. |
| Parked files | Files whose level change is above the threshold. They are not changed; the run continued. Apply or skip each one. |
| Schedule | Weekly 7×24 grid (click or drag), presets, the resulting windows per day, scan interval and behaviour at window close. |
| Normalization | Target loudness slider (LUFS), true-peak ceiling, large-change threshold, override for large changes, lossy-format strategy (gain-only default, re-encode, skip lossy), write mode (in place default, in place with backup, separate output folder), video audio and dry-run. |
| Home Assistant | Connection and access token, local API endpoint with a live JSON payload, sensor definitions, and generated `configuration.yaml` snippet. |

## Notes

- Library contents, statuses and timings are random but deterministic (seeded).
- "Run" advances about two files per second while it is running.
- Save buttons only show a message; settings are not persisted in the mock-up.
- The mock-up is for reviewing layout and behaviour. See `../docs/ARCHITECTURE_PROPOSAL.md` for the planned architecture.
