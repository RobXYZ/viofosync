# viofosync

![CI](https://github.com/RobXYZ/viofosync/actions/workflows/ci.yml/badge.svg) ![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg) ![Docker](https://img.shields.io/docker/pulls/robxyz/viofosync)

Self-hosted web app for syncing, browsing, and exporting recordings from a Viofo dashcam (tested with the A229 Pro) over Wi-Fi. Runs as a single Docker container on a NAS or any always-on host on the same network as the dashcam.

> **v2 is a full rewrite.** v1 was a cron-driven CLI based on [BlackVueSync](https://github.com/acolomba/BlackVueSync). v2 uses the same dashcam protocol but ships a web UI, journey-detected GPS maps, a timeline video editor, ffmpeg exports, JSON-backed settings, a first-run setup wizard, and a UI-driven download manager. The v1 cron CLI is preserved on the `main` branch.

## Features

- **Automatic Wi-Fi sync** - clips copy from the dashcam in your car when it joins your home wi-fi.
- **Download control** - skip clips you don't want, retry failed ones, and prioritise recent footage, all from the download queue.
- **GPS triage** - fetches each clip's GPS trace in seconds without downloading the full recording, so journeys, stops, and place names appear before the footage does — and the download queue can be organised around where you actually drove.
- **Sync filtering** - restrict syncing to locked (event) clips only, or auto-skip anything recorded while parked at a named location such as home. GPS-driven, and the groundwork for richer download/delete ordering to come.
- **Archive browser** - clips grouped by day, played in your browser; hover a clip to scrub a quick preview. Nothing to install on your phone or laptop.
- **Journey maps** - automatic journey detection with each trip shown on a map with stops detected and place names looked up automatically.
- **Clip retention control** - mark any clip read-only to keep it indefinitely (protected from pruning and delete), or delete a clip straight off the camera's SD card once it's safely saved.
- **Video editor** - trim clips, cut between cameras, and add a picture-in-picture inset to any segment, then export a single video.
- **Flexible exports** - original clips, joined front/rear, picture-in-picture, or edited cuts; hardware-accelerated where your system supports it.
- **Storage management** - set a size or age limit and the oldest footage is pruned to fit; optional auto-delete clears the camera's SD card once a clip is safely saved.
- **Camera control** - read and adjust the dashcam's own settings (resolution-adjacent options, parking mode, watermarks, HDR, LEDs, GPS…) from a Camera tab, with destructive actions hard-blocked.
- **Easy browser-based setup** - a first-run wizard, then a settings page.
- **Home Assistant support** - over MQTT, with sync status, alerts, and action buttons.

### Coming Soon

- **GPS triage** - fetch GPS traces from the camera ahead of downloading any recordings. View journeys, set rules and organise your download queue based on the GPS.
- **Advanced sync policies** - prioritise or skip recordings by type and location: fetch locked event clips first, deprioritise parking-mode footage, or skip anything recorded at home.

![Timeline video editor](https://raw.githubusercontent.com/RobXYZ/viofosync/main/screenshots/timeline_editor.webp)![Download manager](https://raw.githubusercontent.com/RobXYZ/viofosync/main/screenshots/download_manager.webp)

## Contents

- [Features](#features)
- [Getting started](#getting-started)
- [Configuration](#configuration)
- [Home Assistant](#home-assistant-via-mqtt)
- [Camera control](#camera-control)
- [Reference](#reference)
- [About](#about)

## Getting started

### Requirements

> [!NOTE]
> #### Most users will need
> - **Viofo Wi-Fi dashcam** connected to your LAN in station mode
> - **Viofo special firmware** to keep station mode always-on (supplied by Viofo support on request)
> - **Hardwire kit** (Viofo HK4) to keep the camera powered when parked - a dedicated dashcam battery is recommended for extended downloads
> - **Reserved IP** for the dashcam on your router, so it doesn't change
> - **NAS or always-on host** with large storage that can run Docker
> - **Optional: hardware video encoder + fast LAN** - recommended for the video editing features

### Tested cameras

viofosync targets any Viofo Wi-Fi dashcam that uses the standard `…F` / `…R` / `…T` / `…I` recording filenames, so models beyond those listed below should work — reports of other cameras are welcome.

| Camera         | 
| -------------- | 
| Viofo A229 Pro | 
| Viofo A329     | 

Three-camera models are supported with either a telephoto (`T`) or interior/cabin (`I`) third lens. The telephoto channel was validated live on the A329; the interior channel was validated against real cabin-cam footage contributed alongside that work.

### Quick start

```bash
docker run -d \
  --name viofosync \
  -p 8080:8080 \
  -e PUID=$(id -u) \
  -e PGID=$(id -g) \
  -e TZ=Europe/London \
  -v /path/to/config:/config \
  -v /path/to/recordings:/recordings \
  --restart unless-stopped \
  robxyz/viofosync
```

Or use the included `[docker-compose.yml](docker-compose.yml)`, which has the same settings plus a commented-out GPU passthrough block (see below).

Open `http://<host>:8080` and the first boot redirects you to a one-screen setup wizard at `/setup`. Enter the dashcam IP and an admin password (12+ characters) to finish. The wizard writes `/config/config.json` with a freshly-generated `SESSION_SECRET` and a bcrypt hash of the password — neither is held in env vars or the image.

After setup, every other setting lives on the **Settings** page in the UI.

> ⚠ **Setup window safety.** Until the wizard is submitted there is no auth on the container — the wizard self-disables after first submission and the route returns 404 thereafter. Don't expose the container to the public internet during this window.

### Hardware-accelerated exports

Exports (join, picture-in-picture, switched) and thumbnails use ffmpeg. At startup the app probes the host's encoders — QuickSync (Intel iGPU), VAAPI, NVENC, VideoToolbox — and falls back to software (libx264) if none work, so exports always run.

To use an Intel iGPU, pass the render node through:

```bash
docker run ... --device /dev/dri:/dev/dri robxyz/viofosync
```

The entrypoint auto-detects the render node's group and adds the app user to it. Some hosts (notably Synology DSM) need the group granted explicitly — find the GID and add it:

```bash
docker exec <container> sh -c 'stat -c %g /dev/dri/renderD128'   # often 937 on Synology
# docker run ... --group-add 937   (or group_add: ["937"] in docker-compose.yml)
```

Confirm it engaged with `docker exec <container> vainfo`, and check the startup log for `export encoder available: … qsv …`.

> [!NOTE]
> On arm64 hosts QuickSync won't probe-pass; exports degrade to VAAPI or software automatically.

## Configuration

The only Docker-level env vars are:

| Variable        | Description                                      | Default      |
| --------------- | ------------------------------------------------ | ------------ |
| `PUID` / `PGID` | Owner of `/config` and `/recordings` on the host | host UID/GID |
| `TZ`            | Timezone for log timestamps                      | UTC          |

App-level settings (sync interval, dashcam IP, encoder, geocoding email, web port, retention, password, auto-delete, etc.) are editable on the **Settings** page. Advanced users can hand-edit `/config/config.json` between restarts; the schema lives in `[web/settings_schema.py](web/settings_schema.py)`.

### Importing without Wi-Fi

Use **Import manually** in the web UI to ingest clips you already have on disk or the SD card. Two modes:

- **Upload** — pick a folder in your browser; clips upload one at a time and slot straight into the archive. On a quota-bound archive it makes room as it goes, evicting the oldest clips (never anything newer than what you're importing).
- **Folder** — copy clips into the `import` folder inside your recordings share, then **Scan** → **Ingest**. By default this is `recordings/import`; for a one-off import from a different path, type it in the Import dialog's Folder tab, or set a persistent default via the advanced `IMPORT_PATH` key in `/config/config.json`.

**From a USB drive / card reader:** bind-mount it into the container and set the import path to the mount, e.g.:

```bash
docker run ... -v /mnt/usb:/import robxyz/viofosync
# then type /import in the Import dialog, or set IMPORT_PATH=/import in /config/config.json
```

The source is only ever **read** — originals on the card/USB are never deleted. If you plug the drive in *after* the container starts, either restart the container or use shared mount propagation (`-v /mnt:/mnt:rshared`, with the host mount also shared) so the container sees it.

Imported clips are recognised by Viofo naming (`YYYY_MMDD_HHMMSS_NNNN[event][cam].MP4`); locked clips under an `RO/` folder keep their protected status. Non-matching files are left untouched.

### Alternative camera address

You can set an optional **Alternative address** (Settings → Dashcam) for the **same** dashcam.

This can be useful for reaching the camera on a second network:

- A Raspberry Pi running a VPN hotspot in the car, so you can reach the dashcam remotely.
- A site-to-site VPN to a second location the car is regularly parked at, where the camera sits on a different subnet/IP.

## Home Assistant via MQTT

viofosync can publish state and accept actions over MQTT, with full Home Assistant auto-discovery.

Enable on the Settings page → MQTT. You'll need:

- A reachable MQTT broker (Mosquitto, HA's built-in broker, EMQX, etc.).
- Broker host + port. Optional username, password, and TLS.
- A `Node ID` (default `viofosync`) — used as the topic prefix and as the `node_id` slot in HA discovery topics. Letters, digits, and `_` only. Set a distinct value per instance if you run more than one.

When MQTT is on, viofosync publishes:

- **Discovery configs** under `homeassistant/{component}/{node_id}/{object_id}/config` (retained) so HA picks them up immediately.
- **State** under `{node_id}/{object_id}/state` (retained, event-driven, no idle traffic).
- **Availability** to `{node_id}/availability` (`online` / `offline`), with LWT so HA marks every entity Unavailable within ~45s of an unclean disconnect.

### Sensors and buttons

Enabled by default in HA: dashcam connectivity, dashcam connection (`primary` / `alternative` / `offline`, with the live address as an `address` attribute), sync status (`downloading` / `waiting` / `paused` / `error`), queue pending, last downloaded clip, disk used, and six action buttons (start/pause/skip/refresh/retry-failed/rescan).

Disabled-by-default (still created — enable per-entity in HA): queue failed, queue downloading, current filename, current progress, total clips.

### Parameterised command

For "prioritize the last N hours", publish to `{node_id}/cmd/prioritize_recent` with payload `{"hours": 0.5}` (HA's `mqtt.publish` service works). `hours` must be in (0, 168].

### Security notes

- The MQTT password is stored in `config.json` in plaintext, alongside the bcrypt hash of the admin password and the session secret. The same access controls already apply to that file.

## Camera control

The **Camera** tab reads the dashcam's current settings and lets you change them over Wi-Fi — parking mode, watermarks, HDR, LEDs, GPS, beeps, time/date, loop length, bitrate, and so on. On/off settings are toggles; multi-choice settings are drop-downs populated with the camera's own option labels. Each change is validated, sent, and read back to confirm it applied.

![The Camera tab](https://raw.githubusercontent.com/RobXYZ/viofosync/main/screenshots/camera_control.webp)

This drives the undocumented Novatek **netapp** HTTP interface (`http://<cam>/?custom=1&cmd=<id>&par=<value>`). Because that protocol has no schema, the option labels and value enumerations come from a derived per-model command map (`viofosync_lib/data/command_map.json`); see [Command map data](#command-map-data).

**Safety model.** On this protocol a bare command is *not* always a harmless read — some ids are destructive actions. The control layer:

- **Hard-blocks destructive ids** (format SD, factory reset, firmware update, delete file, reboot, restart-Wi-Fi, SSD format/delete) — they are refused before any request is built and are never shown in the UI.
- **Allow-lists writes** to enumerated settings only, validates the value against the camera's option list, and **verifies** by reading the setting back.
- **Is gentle**: one request at a time with a short timeout, so it doesn't overrun the camera's single-threaded daemon.
- **Auto-pauses recording** for the few settings the camera only accepts when stopped (loop length, bitrate), then resumes — flagged in the UI as "paused recording".

**What it won't change.** Settings the camera refuses over Wi-Fi are shown read-only with the reason — e.g. *recording resolution* (changeable on the camera, not in station mode) and *exposure*. Settings for a lens that isn't attached (rear/interior HDR, video-merge, …) are read-only with "needs the rear/interior camera" and light up automatically once that lens is connected (detected via the live sensor count).

Only the **A329S** has been validated against real hardware; other models are mapped from the app data but untested.

### Command map data

`viofosync_lib/data/command_map.json` is a plain reformatting of the *factual API data* (command ids, English keys, descriptions, and option enumerations) found in the official VIOFO Android app's `device-cmd-manager.db` asset. No app code or resources are included, and the original `.db` is not redistributed. Regenerate or extend it with `scripts/build_command_map.py` (see the script header for how to pull the asset from the APK).

## Reference

### Reverse geocoding

Journey and stop cards display their start/end as *"Street, Town"* via Nominatim (OpenStreetMap). Lookups are rate-limited to 1/second per [Nominatim's usage policy](https://operations.osmfoundation.org/policies/nominatim/) and cached in the `geocode_cache` table (coords rounded to 3 d.p., ≈110 m). Set **Nominatim email** in Settings → GPS & Geocoding to identify your install per OSM's terms; toggle the **GPS maps** filter off on the Archive page to skip the Leaflet + Nominatim machinery entirely for low-bandwidth browsing.

### XML vs HTML listing

By default the app scrapes the dashcam's HTML directory listings (`/DCIM/Movie`, `/DCIM/Movie/Parking`, `/DCIM/Movie/RO`), which is noticeably faster on some firmware than the XML API (`/?custom=1&cmd=3015&par=1`). Toggle off **Use HTML directory listing** in Settings → Dashcam to fall back to XML.

### Migrating from v1

Existing installs with a `viofosync.env` file are migrated automatically on first boot of the v2 image:

- Settings land in `/config/config.json`.
- The original `viofosync.env` is preserved with a deprecation header — safe to delete.
- The cron-style entry point is no longer the primary path; the web app's sync worker covers the same ground with live progress and queue control.

`PUID` / `PGID` / `TZ` env vars work the same as v1.

### Running without Docker

For development or for hosts that don't have Docker:

```bash
pip install -r requirements.txt
CONFIG_DIR=/path/to/config RECORDINGS=/path/to/archive \
  python3 -m web.launcher
```

`web.launcher` reads `WEB_HOST` / `WEB_PORT` from `config.json` (defaults `0.0.0.0:8080`) and re-execs into uvicorn. On first run, browse to `http://localhost:8080/setup`. `ffmpeg` must be on `$PATH` for thumbnails and exports.

## About

### AI Code

viofosync is an open-source project built with substantial AI assistance, intended for personal use on a home network. Its security model assumes a trusted LAN - a single password over plain HTTP - so keep it behind your network or a VPN rather than exposing it directly to the public internet.

### Credits

Camera control — reading and safely adjusting dashcam settings over Wi-Fi — was contributed by [@droomurray](https://github.com/droomurray) (#21).

Three-camera support (telephoto and interior lenses), the single-source camera registry and other improvements were contributed by [@jusii](https://github.com/jusii) (#17, #18, #20).

Single channel camera work by [@nittanygeek](https://github.com/nittanygeek) (#24).

The GPX extraction logic uses the method described at [https://sergei.nz/extracting-gps-data-from-viofo-a119-and-other-novatek-powered-cameras/](https://sergei.nz/extracting-gps-data-from-viofo-a119-and-other-novatek-powered-cameras/).

This software is unaffiliated with Viofo or any other vendor.

### License

MIT — see [LICENSE](LICENSE).
