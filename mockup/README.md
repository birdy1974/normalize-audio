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
| Dashboard | Run status (running / paused / stopped), % complete, Start / Pause / Stop, Process switches (Audio / Video), and per media type the four counters: folders/files in scope, files to do, checked, actually changed. The Activity list can be cleared. |
| Directories | Audio / Video tabs. Tree of each base location with per-folder checkboxes, tri-state parents, Select all / Select none, expand / collapse, and a file browser for the selected folder. |
| Parked files | Audio / Video tabs. Files whose level change is above the threshold. Adjust a file's change with − and + (0.5 dB steps), select several files and adjust or apply them together. The run continued; nothing is changed until you apply it. |
| Schedule | Audio / Video tabs, each with its own weekly 7×24 grid (click or drag), presets, and the resulting windows per day. |
| Settings | Audio / Video tabs. Global dry run, then per type: target loudness slider (LUFS), true-peak ceiling, large-change threshold, override for large changes, lossy-format strategy (gain-only default, re-encode, skip lossy), write mode (in place default, in place with backup, separate output folder), video audio and dry-run. Also the scheduling options: scan interval and behaviour when a window closes. |
| Home Assistant | Connection and access token, local API endpoint with a live JSON payload, the **Lovelace card** (download, 3 install steps, YAML, live preview of the same file), sensor definitions grouped by type, and generated `configuration.yaml` snippet with the main `sensor.audio_normalizer` entity. |

## Notes

- Style: greyscale only (no accent colour), square corners everywhere, custom square checkboxes, radios and sliders. The side menu stays on the left at every width; on narrow screens it becomes a narrow rail.
- `lovelace/` in the repository is linked here as `mockup/lovelace`, so the Download button and the live card preview use the same file as the repository.
- Menu: icons on every item. The chevron collapses the menu to an icon rail. The pin keeps the menu as it is; when unpinned, the rail opens on hover over the content. The state is remembered in the browser.
- Colours: greyscale base with one muted green accent for selected states and primary buttons, and light tints for notices and selected rows. Small corner radii (4px controls, 6px cards).
- Home Assistant preview: the card preview and the install YAML show only the sensors enabled on the page. Disabling a sensor removes it from both.
- `lovelace/` in the repository is linked here as `mockup/lovelace`, so the Download button and the card preview use the same file.
- Library contents, statuses and timings are random but deterministic (seeded).
- "Run" advances about two files per second while it is running, for the media types selected with the Process switches.
- Save buttons only show a message; settings are not persisted in the mock-up.
- The mock-up is for reviewing layout and behaviour. See `../docs/ARCHITECTURE_PROPOSAL.md` for the planned architecture.
