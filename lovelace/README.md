# Audio Normalizer — Lovelace card

A native Home Assistant dashboard card that shows every value of the audio normalizer:
run status and progress, audio and video counters (folders, files in scope, to do,
checked, changed, parked), target loudness, next schedule change, errors, current file
and last run.

The card is one self-contained file: `audio-normalizer-card.js`. It has no dependencies,
and it follows your Home Assistant theme (light and dark).

## 1. Get the file

Either of these works:

- **From the web page:** Home Assistant page → *Lovelace card* → **Download card**.
- **From GitHub:** download `lovelace/audio-normalizer-card.js` from this repository.

## 2. Copy it into Home Assistant

Copy the file to the `www` folder of your Home Assistant configuration:

```
/config/www/audio-normalizer-card.js
```

Home Assistant serves this as `/local/audio-normalizer-card.js`.

> Use the downloaded file, not a `raw.githubusercontent.com` link. GitHub serves raw files
> with a MIME type that Home Assistant refuses to load as a module.

## 3. Register it as a resource

*Settings → Dashboards → ⋮ (top right) → Resources → Add resource*

| Field | Value |
|---|---|
| URL | `/local/audio-normalizer-card.js` |
| Resource type | JavaScript module |

Reload the browser (or clear the cache) afterwards.

## 4. Make sure the sensor exists

The card reads the entity `sensor.audio_normalizer`. It is created by the REST sensor
snippet on the Audio Normalizer web page (Home Assistant page → *Generated configuration.yaml*).
Its attributes hold the full payload, so the card needs no further mapping.

If you named the entity differently, set `entity:` in the card configuration.

## 5. Add the card

Edit a dashboard → *Add card* → search for **Audio Normalizer**, or use raw YAML:

```yaml
type: custom:audio-normalizer-card
entity: sensor.audio_normalizer
title: Audio Normalizer
show_audio: true     # audio counters
show_video: true     # video counters
show_details: true   # target loudness, next schedule change, current file, last run
```

Full-width layout tip: put the card in a *Panel* or a *Grid* section with one column.

## Options

| Option | Default | Meaning |
|---|---|---|
| `entity` | `sensor.audio_normalizer` | Status entity created by the REST sensor |
| `title` | `Audio Normalizer` | Card title |
| `show_audio` | `true` | Show the audio block |
| `show_video` | `true` | Show the video block |
| `show_details` | `true` | Show target, next schedule change, current file and last run |

## Troubleshooting

- **"Entity was not found"**: the REST sensor is missing or has a different entity name. Check *Developer tools → States* for `audio_normalizer`.
- **"The entity is unavailable"**: the container cannot be reached, or the token is wrong. Check the REST sensor under *Settings → Devices & services → REST*.
- **Card does not appear in the picker**: clear the browser cache after adding the resource.
