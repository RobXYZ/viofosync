# Changelog

## v2.6 — 2026-08-14

### Fixed

- **Archive delete now tells the truth about what it deleted.** A failed unlink — typically a `PUID`/share permission mismatch on a NAS mount — still removed the clip from the index: the tile vanished, "Deleted N" was reported, no space was freed, and the clip reappeared on the next rescan. Failed deletes now keep their rows and are reported as failed. Retention gets the same treatment plus a wedge guard, so the disk-pressure loop can no longer spin re-selecting the same un-deletable clip while crediting space it never freed.
- **Deleting protected clips is no longer a silent no-op.** Selecting locked clips reported "Deleted 0, skipped 0" with no explanation and no way through. The toast now counts protected and failed clips, and offers a confirm-through that force-deletes exactly the refused ones. Both fixes contributed by [@Anonymouse6661](https://github.com/Anonymouse6661) (#30).
- **Long stalls between downloads on slow cameras.** Refreshing the file listing after every download kept the queue current, but a camera slow to report its listing could spend longer listing than downloading, stalling the drain between short clips. The mid-drain refresh is now throttled adaptively — 10× the measured cost of the last refresh, floored at 30 s and capped at 600 s — while the cycle-start listing is unthrottled. Per-phase timings were added to the log so a single Logs-tab paste attributes any remaining gap. (#31)
- **Downloads refused on valid group-writable directories.** The writability pre-flight misreported NFS shares; download failures now leave a trace in the app log instead of failing silently. (#26)
- **Clips were wrongly written off as gone.** An empty file listing is a failed camera response far more often than a wiped card, and *gone* was terminal — retry only touches failed clips, so one lost this way was never re-queued. An empty listing no longer condemns the queue, and a clip that reappears on the camera is revived. (#26)
- Day cards' clip counts and sizes refresh after a delete or skip instead of going stale until a manual reload.
- A persistently rejected request no longer retries forever — the session-token refresh retries once, then surfaces the error.

### Changed

- **Faster archive browsing.** The clip lookups behind a day view no longer touch the recordings mount from the event loop, so a spun-down array can't freeze every other request and the live progress updates while a day loads. Page and API responses are gzipped — the app's JavaScript and the per-day payloads land 5–8× smaller on the wire. Contributed by [@Anonymouse6661](https://github.com/Anonymouse6661) (#30).
- **Cheaper redraws during a sync.** Day cards are only rebuilt when their contents actually changed, and the redraw is throttled rather than debounced, so the archive no longer rebuilds itself — Leaflet maps included — on every queue transition while footage streams in.
- **Filmstrip hover-scrub waits its turn.** Sweeping the cursor across a day grid used to queue an ffmpeg sprite job per tile crossed, saturating the browser's connections and stalling every other request behind sprite generation. A preview now needs 350 ms of hover and is abandoned on mouse-out.
- **Clear read-only** joins Mark read-only in the archive Actions menu.

### Added

#### Instance Name

A new **Instance name** setting labels one install so several are tellable apart — one per car, or a test instance alongside a live one. The label shows on the login page, in the header, and in the browser tab title. Leaving it at the default `viofosync` shows no label and keeps existing export filenames unchanged. (#29)

#### Support Bundle

**Settings → System → Download debug bundle** produces a single Markdown diagnostic report — runtime, settings, queue and database state, a log tail, and a live cross-check of the camera's reported file sizes — masked for public sharing, so it can be attached straight to an issue without hand-scrubbing. The running build version now shows at the foot of the Settings sidebar too, so a report can say exactly which build it came from.

## v2.5 — 2026-07-10

### Added

#### GPS Triage

viofosync can now fetch a clip's **GPS track before downloading the recording itself** — a skeleton trace pulled in seconds over Wi-Fi, rather than minutes of transferring the full MP4. With triage on, every queued clip is scouted for its route first, so the archive's journey maps, stops, and place names appear ahead of the footage arriving, and the download queue can be organised around where you actually drove. Off by default — enable **GPS triage** in Settings.

#### Sync Filtering

GPS-driven controls over what actually syncs — more sync policies to come:

- **Read-only-only sync** — restrict syncing to locked (event) clips, so only footage the camera has protected is pulled.
- **Location skip** — define **named locations** (Home by default) with a dwell radius; with GPS triage on, clips that dwell at a location flagged *exclude recordings* are auto-skipped, so parking-mode footage recorded on your own driveway never downloads. Location names also label journey and stop endpoints on the archive maps.

#### Clip Locking & Delete-from-Camera

You can now manually **mark a clip read-only** to retain it indefinitely — locked clips are protected from retention pruning and manual delete.

#### Journeys & Archive UX

- A **per-journey completion pie** shows at a glance how much of each detected trip has finished downloading.
- Journey grouping is **padded past the GPS stop radius** so a trip takes in its pull-away and pull-in clips, with the trace, start, and end markers all unified to that same padded window.
- Unified **clip actions dropdown** (queue / export / delete) replaces the scattered per-clip buttons.
- Archive navigation now **preserves tab state, restores map views, and adds FLIP animations**, so the view no longer resets as clips index in.

### Changed

- Single-channel captures get **compact filenames** — no redundant camera suffix when only one lens is present — and a downloaded clip's sidecar GPS is used as a fallback source for its track. Thanks to @nittanygeek (#24).
- Redundant **Extract GPS** button was removed now that triage handles extraction.

### Fixed

- The dashcam lists and serves the clip it is *still recording* and reports a stale size for it. An **active-recording guard** now holds every lens of the newest capture group out of triage and the download drain until the camera's `cmd=3014` record flag reports stopped, freeing superseded captures as new ones arrive and deferring any clip that keeps growing mid-download. (#26)
- The sync worker now **reports its state on start**, so the status badge no longer shows a false "paused" before the first cycle.
- `RangeReader` now raises `TruncatedRead` on a short range response instead of silently accepting a truncated GPS read.

## v2.4 — 2026-06-18

### Added

#### Camera Control

A new **Camera** tab reads the dashcam's current settings and lets you adjust the safely-changeable ones over Wi-Fi — on/off toggles, drop-downs, and a recording indicator — each validated against the camera's own option list and read back to confirm it stuck. Destructive commands (format SD, factory reset, firmware update, delete, reboot) are hard-blocked and never shown, and the few record-only settings auto-pause then resume recording. Settings for 29 Viofo models are mapped from the official app's command database; the A329S is validated on hardware. Contributed by [@droomurray](https://github.com/droomurray) (#21).

#### Three-Camera Support (Telephoto / Interior)

Telephoto (`T`) and interior/cabin (`I`) clips are now first-class alongside front and rear. They sync, index, and pair into the same capture group, so a three-camera day shows a third thumbnail in the archive and a third track on the timeline. New exports cover them: **Join Tele** / **Join Interior**, plus picture-in-picture with the third camera fullscreen and the front camera as the inset (the front clip stays the audio source, so the microphone track is preserved). The clip viewer's camera key cycles through every camera present at a timestamp. Two-camera setups are visually unchanged. Contributed by [@jusii](https://github.com/jusii) (#18).

#### Background Thumbnails & Filmstrips

Thumbnails and timeline filmstrips are now produced by a background worker as clips download — and existing clips are backfilled — so the archive and timeline populate as soon as footage arrives instead of after a sync cycle finishes. A new **Thumbnails** settings section controls it: thumbnail pre-generation is on by default, while the heavier filmstrip pre-generation is opt-in and otherwise falls back to generating on demand the first time a clip is viewed.

#### Per-Segment Picture-in-Picture in the Editor

The timeline editor's switched-camera cut can now carry a picture-in-picture inset on a per-segment basis. With a segment selected, press the PiP button (or **P**) to cycle the inset through your other cameras — it skips the segment's own camera — and a green placeholder shows where it will sit. The choice is remembered per segment and composited into the export, in the corner set by the global picture-in-picture position setting. A segment whose chosen camera has no overlapping footage simply exports without the inset.

#### Skip Downloads

You can now skip clips you don't want to sync. Select them in the download queue and choose **Skip** from the **Actions** menu; skipped clips get their own badge and are never downloaded. **Clear skip** returns them to the queue with a fresh set of retry attempts. Queue selection now spans pending, failed, and skipped clips, so one Actions menu — Download next / Skip / Clear skip / Retry failed — drives the whole list.

### Changed

- On/off controls across the app are now toggle switches, with one tooltip style used app-wide that works on hover, keyboard focus, and tap.
- The sync **pause** state is remembered across restarts instead of resetting to running.
- The download queue's per-action buttons are now a single **Actions** menu with **Apply**; selection-based **Retry failed** replaces the old retry-all button, while **Download recent hours next** is unchanged.
- Archive thumbnails animate on hover, scrubbing the clip's filmstrip — the same preview the Export Jobs list already offered. They fall back to the static thumbnail when no filmstrip is available, and respect reduced-motion.

### Fixed

- Locking a clip on the dashcam between sync cycles moves it into the camera's `/RO` folder; the download queue now refreshes the clip's source path when the camera re-reports it there, instead of exhausting its retry budget against the stale path and never syncing the clip. The dashcam-delete lock guard benefits too, since it keys off the same refreshed `/RO` path. Contributed by [@jusii](https://github.com/jusii) (#17).
- The Logs view now shows the date alongside the time and stays readable on a phone — long lines wrap rather than being clipped off-screen.
- Tapping a drop-down or text field no longer zooms the page in on mobile, and the interface no longer pinch-zooms — it behaves like a fixed app viewport.
- Auto-detected journeys on the timeline no longer miss the start of a drive or cut short on arrival — the journey window is padded past the GPS stop radius to take in the pull-away and pull-in clips, bounded by the surrounding parking footage.

## v2.3 — 2026-06-10

### Added

#### Timeline Video Editor

A multi-camera timeline editor for the archive: scrub the front/rear channels on a shared playhead with per-clip filmstrips, set in/out trim points, and cut between cameras to export a single switched-angle video. Includes frame-accurate keyboard shortcuts. Join, picture-in-picture, and switched exports are now hardware-accelerated via Intel QuickSync where available, with VAAPI and software fallbacks.

#### Export Jobs

- Animated filmstrip preview on each job — hover to scrub through the finished video.
- Click a job's thumbnail to play the export in the viewer.
- Output **Length** and **Size** columns in the jobs table.
- Switched-camera exports now carry one continuous front-camera audio track, removing the audible jump at each camera switch.

### Changed

- Base image moved to Debian + jellyfin-ffmpeg to unlock QuickSync on Intel iGPUs; VAAPI and software remain as fallbacks, so a host without a working iGPU degrades transparently.
- Archive updates live on a clip-indexed push instead of per-client polling, and the per-day GPS route aggregation is cached.
- UI polish: background dither, clearer labels, and archive view state that persists across navigation.

### Fixed

A broad reliability and security hardening pass (full per-item detail in `CLAUDE.md`):

- **Worker lifecycle** — sync and export workers now shut down within a bounded timeout, and an in-flight or paused ffmpeg export is no longer left running after stop. Changing the dashcam address or toggling scheduled sync starts and stops the worker at runtime, without a restart.
- **Responsiveness** — NAS directory walks, SQLite transactions, and the quota disk-usage scan run off the event loop, so a slow or busy recordings volume no longer freezes the UI and live updates.
- **Data safety** — manual-import staging recovers completed clips after a crash instead of deleting them; downloads that fail their size check are rejected rather than archived truncated; corrupt or truncated MP4s no longer spin a worker at 100% CPU; and partial thumbnails, filmstrips, or exports can't be served from cache or left to count against the quota.
- **Disk full** — a full recordings volume raises a sticky "disk full" sync error and pauses the queue, instead of marking every clip failed.
- **MQTT** — reconnect backoff resets after a stable connection, retained discovery configs are cleaned up on the first node-id/prefix change, and timed-out probes are reaped.
- **Security** — clip filenames and geocoded place names are HTML-escaped before display; the live-events WebSocket rejects cross-origin handshakes; the MQTT broker password is no longer returned by the settings API; and retention will not delete a clip while an export is reading it.
- Queue counters update immediately after the Prioritise and Retry actions.

## v2.2 — 2026-06-06

### Added

#### Home Assistant MQTT Support

Auto-discovered sensors and action buttons over MQTT, set up from a new Settings panel.

#### Manual Import

Add clips to the archive without Wi-Fi sync — by browser upload or a folder/USB drop path.

#### Alternative Camera Address

An optional second address for the same camera, used automatically when the primary is unreachable (e.g. reaching the dashcam over a mobile VPN).

#### Quota-Bound Retention

Measure retention and disk thresholds against a declared quota (`RECORDINGS_QUOTA_GB`), for recordings on a NAS share or ZFS dataset.

#### Sync Error Reporting

Sync now surfaces a sticky `error` state — missing config, unwritable path, camera auth failure, or disk full — in both the UI and Home Assistant.

#### Download Manager Improvements

Session speed and ETA while syncing, one-click retry of failed downloads, and live disk usage in Settings.

#### Export Improvements

Meaningful download filenames, direct download of the original front/rear clips, and a new rear-main picture-in-picture variant.

### Changed

- Sync status simplified to four states (`downloading` / `waiting` / `paused` / `error`); update any Home Assistant automations that matched the old `idle` / `stopped` strings.
- Export jobs panel redesigned.
- Downloads are now grouped by hour.
- UI polish: header alignment, unified status colours, and minor label tidy-ups.

### Fixed

- Archive retention caps now enforced on a periodic loop, not only after a download.
- Join exports no longer fail when clip paths are stored relative.
- Settings storage-usage card no longer renders near-invisible on the dark theme.

## v2.1 — 2026-05-16

### Fixed

- DB now lives in the `/config` volume rather than the recordings  
mount for better performance when the recordings are on  
slower storage.
- The startup retention sweep runs in the background rather than  
blocking the UI when there's a large delete backlog.

### Changed

- Retention sweep logs progress: a header when the time-phase has  
work, then a line every 10 deletions. Previously silent until the  
end-of-sweep summary.

### Migration

- An existing DB at `${RECORDINGS}/.viofosync.db` is copied to  
`${CONFIG_DIR}/viofosync.db` on first boot under v2.1. The legacy  
file is renamed to `.viofosync.db.migrated` on the recordings  
volume as a recoverable fallback.

## v2.0 — 2026-05

Major rewrite. Web UI replaces the cron CLI.

### Added

- Web UI on port 8080 with archive browser, download manager, GPX  
journey map, and ffmpeg picture-in-picture exports.
- First-run setup wizard at `/setup`.
- Settings page (UI-driven config, hot-reloaded for runtime values,  
restart-required for `WEB_HOST`/`WEB_PORT`).
- JSON config at `/config/config.json` replaces `viofosync.env`.

### Changed

- Docker image is webapp-only; cron CLI is no longer the primary path.
- Required env vars reduced to `PUID` / `PGID` / `TZ`.

### Migration

- Existing `viofosync.env` files are migrated to `config.json` on first  
boot. The old file is preserved as a one-shot rollback path.

## v1.x

- Cron-driven CLI version. See git history.
