// Viofosync SPA — vanilla JS, no bundler.
// Two tabs (archive, downloads) hash-routed, one modal, one WebSocket.
//
// All mutating calls send X-CSRF-Token. The token is returned by
// /api/auth/login and refreshed via /api/auth/csrf on 403.

const state = {
  csrf: null,
  modalClip: null,         // { id, camera, dayEl, timelines }
  autoAdvance: localStorage.getItem("vfs.autoAdvance") === "1",
  page: 1,
  perPage: 20,
  queueKinds: { driving: true, parking: true, ro: true },
  queueRequestId: 0,
  queueDays: [],           // list of day summaries from /api/queue/days
  queueDayItems: {},       // { 'YYYY-MM-DD': [items] } for expanded days
  queueExpanded: new Set(),
  queueHoursExpanded: new Set(), // keys: "YYYY-MM-DD HH" (HH may be "??")
  queueSelected: new Set(),// filenames ticked
  filters: { driving: true, parking: true, ro: true },
  showMaps: localStorage.getItem("vfs.showMaps") !== "0",
  archiveSelected: new Map(),  // pair_id → { ts, front, rear }
  archiveRefreshTimer: null,
  map: null,
  routeLayer: null,
  ws: null,
  syncRunning: false,
  syncPaused: false,
  currentFilename: null,
  // Mirrored from /api/settings on login + on Save so display
  // helpers (fmtDistance) don't need to read from settingsState
  // (which is only loaded when the Settings tab is visited).
  distanceUnits: "km",
  logsFilter: null,        // { level, logger, q } currently shown in Logs tab
  logsOldestId: null,      // smallest id loaded, for "Load older" pagination
};

// ---------- CSS variable bridge ----------
// Map markers and polylines need actual colour strings, but the
// source of truth for the palette lives in styles.css. cssVar()
// reads from :root once per call so a theme swap propagates without
// touching this file.

function cssVar(name) {
  return getComputedStyle(document.documentElement)
    .getPropertyValue(name)
    .trim();
}

// ---------- API helpers ----------

async function api(path, opts = {}) {
  const headers = { "content-type": "application/json", ...(opts.headers || {}) };
  if (state.csrf && opts.method && opts.method !== "GET") {
    headers["x-csrf-token"] = state.csrf;
  }
  const r = await fetch(path, { ...opts, headers, credentials: "same-origin" });
  if (r.status === 401) {
    showLogin();
    throw new Error("unauthorised");
  }
  if (r.status === 403 && state.csrf) {
    // refresh CSRF once and retry
    const cr = await fetch("/api/auth/csrf", { credentials: "same-origin" });
    if (cr.ok) {
      state.csrf = (await cr.json()).csrf;
      return api(path, opts);
    }
  }
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  const ct = r.headers.get("content-type") || "";
  return ct.includes("json") ? r.json() : r;
}

// ---------- Auth + routing ----------

function showLogin() {
  document.getElementById("login").hidden = false;
  document.getElementById("app").hidden = true;
}
async function showApp() {
  document.getElementById("login").hidden = true;
  document.getElementById("app").hidden = false;
  // Read display preferences before painting day cards so
  // distance formatting picks up the user's choice on first
  // render (rather than re-rendering a tick later).
  await refreshDisplayPrefs();
  routeTo(location.hash || "#/archive");
  openSocket();
  try {
    const s = await api("/api/sync/status");
    state.syncRunning = s.running;
    state.syncPaused = s.paused;
    // The WS snapshot will deliver the server-computed sync_status
    // shortly; no direct updateSyncState call needed here.
  } catch {}
  try {
    const gs = await api("/api/archive/extract-gps/status");
    setExtractButton(gs);
  } catch {}
}

async function refreshDisplayPrefs() {
  try {
    const body = await api("/api/settings");
    state.distanceUnits = body.editable.DISTANCE_UNITS || "km";
  } catch { /* keep defaults */ }
}

// ---- Display formatters ----

function fmtDistance(meters) {
  if (state.distanceUnits === "miles") {
    return `${(meters / 1609.344).toFixed(2)} mi`;
  }
  return `${(meters / 1000).toFixed(2)} km`;
}

function fmtBytes(bytes) {
  // Auto-scale to the largest unit that keeps the integer part
  // below 1024. Lower units use 0-1 decimal, GB+ use 2 so the
  // user sees enough precision on a multi-GB archive.
  if (!bytes || bytes < 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = bytes;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  const dec = i >= 3 ? 2 : (i >= 1 ? 1 : 0);
  return `${v.toFixed(dec)} ${units[i]}`;
}

document.getElementById("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const pw = document.getElementById("pw").value;
  const err = document.getElementById("login-error");
  err.textContent = "";
  try {
    const r = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ password: pw }),
      credentials: "same-origin",
    });
    if (!r.ok) { err.textContent = "Wrong password"; return; }
    const j = await r.json();
    state.csrf = j.csrf;
    showApp();
  } catch (e) { err.textContent = String(e); }
});

document.getElementById("logout").addEventListener("click", async () => {
  await api("/api/auth/logout", { method: "POST" });
  state.csrf = null;
  showLogin();
});

// ---------- Sync toggle button ----------

document.getElementById("sync-toggle").addEventListener("click", async () => {
  if (!state.syncRunning) {
    await api("/api/sync/start", { method: "POST" });
  } else if (!state.syncPaused) {
    await api("/api/sync/pause", { method: "POST" });
  } else {
    await api("/api/sync/resume", { method: "POST" });
  }
  const s = await api("/api/sync/status");
  state.syncRunning = s.running;
  state.syncPaused = s.paused;
  // The WS sync_status event will follow shortly; no direct call to
  // updateSyncState here — the server-computed status is the truth.
});

// state.syncStatus is one of: "downloading", "waiting", "paused", "error", null
// state.syncStatusReason is a short human-readable string (only set in error)
function updateSyncState(status) {
  state.syncStatus = status;
  // The toggle button's pause/resume behaviour still depends on
  // syncRunning/syncPaused, which are derived from sync_state events
  // (see handleEvent). Status is what drives the *visual* badge + icon.
  const btn = document.getElementById("sync-toggle");
  const setVisible = (el, visible) => {
    if (visible) el.removeAttribute("hidden");
    else el.setAttribute("hidden", "");
  };
  const iconPlay = document.getElementById("sync-icon-play");
  const iconPause = document.getElementById("sync-icon-pause");
  const iconSync = document.getElementById("sync-icon-sync");
  const iconWarn = document.getElementById("sync-icon-warning");

  let show, title, klass;
  if (status === "downloading") {
    show = iconSync; title = "Pause downloading"; klass = "active";
  } else if (status === "waiting") {
    show = iconSync; title = "Pause downloading"; klass = "waiting";
  } else if (status === "paused") {
    show = iconPause; title = "Resume downloading"; klass = "paused";
  } else if (status === "error") {
    // Surface the reason on the button tooltip too — users hovering
    // the icon (not the badge) should still see why we're in error.
    show = iconWarn;
    title = state.syncStatusReason
      ? "Error: " + state.syncStatusReason
      : "Error";
    klass = "error";
  } else {
    // Unknown / initial — fall back to play icon, no class.
    show = iconPlay; title = "Start downloading"; klass = null;
  }

  setVisible(iconPlay, show === iconPlay);
  setVisible(iconPause, show === iconPause);
  setVisible(iconSync, show === iconSync);
  setVisible(iconWarn, show === iconWarn);
  btn.classList.remove("active", "paused", "waiting", "error");
  if (klass) btn.classList.add(klass);
  btn.title = title;
}

async function skipCurrentDownload() {
  await api("/api/sync/skip", { method: "POST" });
}

window.addEventListener("hashchange", () => routeTo(location.hash));

function routeTo(hash) {
  // Hash forms: "#/archive", "#/downloads", "#/settings",
  // "#/settings/dashcam", etc. The first path segment is the
  // top-level tab; further segments are tab-internal routing.
  const stripped = hash.replace(/^#\//, "");
  const tab = (stripped.split("/")[0]) || "archive";
  // The Settings cog lives outside <nav> (top-right), so select
  // by data-tab anywhere in the header.
  document.querySelectorAll("header a[data-tab]").forEach((a) => {
    a.classList.toggle("active", a.dataset.tab === tab);
  });
  document.getElementById("view-archive").hidden = tab !== "archive";
  document.getElementById("view-downloads").hidden = tab !== "downloads";
  const logsView = document.getElementById("view-logs");
  if (logsView) logsView.hidden = tab !== "logs";
  const settingsView = document.getElementById("view-settings");
  if (settingsView) settingsView.hidden = tab !== "settings";
  const timelineView = document.getElementById("view-timeline");
  if (timelineView) {
    timelineView.hidden = tab !== "timeline";
    // Stop timeline playback when navigating away (a hidden <video>
    // keeps playing audio otherwise).
    if (tab !== "timeline" && window.Timeline && window.Timeline.close) {
      window.Timeline.close();
    }
  }
  if (tab === "archive") {
    loadDays();
    refreshExportJobs();
    startArchiveAutoRefresh();
  } else {
    stopArchiveAutoRefresh();
  }
  if (tab === "downloads") loadQueue();
  if (tab === "logs") loadLogs();
  if (tab === "settings") loadSettings();
  if (tab === "timeline") {
    stopArchiveAutoRefresh();
    // "#/timeline/<date>/<journeyIdx?>" — segments after the tab.
    const segs = stripped.split("/");
    const date = segs[1] || "";
    const n = segs[2] != null && segs[2] !== "" ? Number(segs[2]) : null;
    const journey = Number.isInteger(n) && n >= 0 ? n : null;
    if (window.Timeline && date) window.Timeline.open(date, journey);
  }
}

// Periodic rescan + reload so freshly downloaded clips appear
// without a manual refresh. Rescan is cheap (UPSERT per file).
async function autoRefreshArchive() {
  if (document.getElementById("view-archive").hidden) return;
  // Skip while the user has a day expanded — re-rendering would
  // collapse the card, reset the map, and drop any unsubmitted
  // selections.
  const expanded = document.querySelector(
    "#days .day .day-body:not([hidden])",
  );
  if (expanded) return;
  try { await api("/api/archive/rescan", { method: "POST" }); }
  catch { /* non-fatal */ }
  await loadDays();
}

function startArchiveAutoRefresh() {
  stopArchiveAutoRefresh();
  state.archiveRefreshTimer = setInterval(autoRefreshArchive, 30000);
}
function stopArchiveAutoRefresh() {
  if (state.archiveRefreshTimer) {
    clearInterval(state.archiveRefreshTimer);
    state.archiveRefreshTimer = null;
  }
}

// ---------- Archive ----------

function wireArchiveFilter(id, key) {
  document.getElementById(id).addEventListener("change", (e) => {
    state.filters[key] = e.target.checked;
    state.page = 1;
    loadDays();
  });
}
wireArchiveFilter("f-driving", "driving");
wireArchiveFilter("f-parking", "parking");
wireArchiveFilter("f-ro", "ro");

// "GPS maps" is a view option, not a filter: it gates the
// journey machinery (Leaflet, route fetch, reverse-geocode) on
// expansion but doesn't change what's fetched. Persisted to
// localStorage.
(() => {
  const cb = document.getElementById("f-show-maps");
  cb.checked = state.showMaps;
  cb.addEventListener("change", (e) => {
    state.showMaps = e.target.checked;
    localStorage.setItem("vfs.showMaps", state.showMaps ? "1" : "0");
    // Re-render any day bodies that are currently expanded so the
    // change takes effect immediately without a full page reload.
    document.querySelectorAll(
      "#view-archive .day .day-body:not([hidden])",
    ).forEach((body) => {
      const day = body.closest(".day").dataset.day
        || body.parentElement.querySelector(".day-header h3").textContent.trim();
      body.innerHTML = "<p>Loading…</p>";
      renderDayBody(body, day);
    });
  });
})();

document.getElementById("rescan").addEventListener("click", async () => {
  const btn = document.getElementById("rescan");
  btn.disabled = true; btn.textContent = "Scanning…";
  try {
    await api("/api/archive/rescan", { method: "POST" });
    await loadDays();
  } finally {
    btn.disabled = false; btn.textContent = "Rescan";
  }
});

function setExtractButton({ running, done, total, extracted, empty, errors }) {
  const btn = document.getElementById("extract-gps");
  if (running) {
    btn.disabled = true;
    btn.textContent = total
      ? `Extracting GPS ${done}/${total}…`
      : "Extracting GPS…";
  } else {
    btn.disabled = false;
    btn.textContent = "Extract GPS";
  }
  if (!running && total > 0 && (extracted != null)) {
    btn.title = `Last run: ${extracted} extracted · ${empty} empty · ${errors} errors`;
  }
}

document.getElementById("extract-gps").addEventListener("click", async (e) => {
  // Shift+click forces re-extraction of clips that already
  // have a sidecar — use this after tweaking the spike filter.
  const force = e.shiftKey;
  if (force && !confirm(
    "Re-extract GPS for every clip in the archive? This overwrites existing .gpx sidecars."
  )) return;
  try {
    const url = force ? "/api/archive/extract-gps?force=true" : "/api/archive/extract-gps";
    const r = await api(url, { method: "POST" });
    if (!r.started) {
      const btn = document.getElementById("extract-gps");
      btn.textContent = r.total === 0 ? "No clips need GPS" : "Extract GPS";
      setTimeout(() => { btn.textContent = "Extract GPS"; }, 2000);
      return;
    }
    setExtractButton({ running: true, done: 0, total: r.total });
  } catch (e) {
    console.warn("extract-gps failed", e);
  }
});

function archiveKindParams(q) {
  q.set("driving", state.filters.driving ? "true" : "false");
  q.set("parking", state.filters.parking ? "true" : "false");
  q.set("ro", state.filters.ro ? "true" : "false");
}

async function loadDays() {
  const q = new URLSearchParams({
    page: state.page, per_page: state.perPage,
    sort: "desc",
  });
  archiveKindParams(q);

  const data = await api("/api/archive/days?" + q);
  const container = document.getElementById("days");
  container.innerHTML = "";
  if (!data.days.length) {
    container.innerHTML = `<p style="text-align:center;color:var(--muted)">
      No recordings found. Click Rescan or wait for the next sync.</p>`;
    return;
  }
  for (const d of data.days) {
    container.appendChild(renderDayCard(d));
  }
  renderPagination(data.total);
}

function renderDayCard(d) {
  const el = document.createElement("div");
  el.className = "day";
  el.dataset.day = d.day;
  el.innerHTML = `
    <div class="day-header">
      <h3>${d.day}</h3>
      <div class="meta">
        ${d.clip_count} clips${
          [
            d.driving_count ? `${d.driving_count} driving` : null,
            d.parking_count ? `${d.parking_count} parking` : null,
            d.ro_count ? `${d.ro_count} read-only` : null,
          ].filter(Boolean).map((s) => ` · ${s}`).join("")
        } · ${fmtBytes(d.total_bytes)}${d.gpx_count ? " · GPS" : ""}
      </div>
    </div>
    <div class="day-body" hidden></div>
  `;
  el.querySelector(".day-header").addEventListener("click", async () => {
    const body = el.querySelector(".day-body");
    if (!body.hidden) { body.hidden = true; return; }
    body.hidden = false;
    body.innerHTML = "<p>Loading…</p>";
    await renderDayBody(body, d.day);
  });
  return el;
}

async function renderDayBody(body, date) {
  const q = new URLSearchParams();
  archiveKindParams(q);

  // Fetch clips. The route (journeys + stops + GPS line) is a
  // heavier fetch — it loads Leaflet, hits Nominatim for the
  // start/end labels, and decodes every clip's GPX — so we skip
  // it entirely when the user has the GPS-maps toggle off.
  let data, route;
  try {
    const promises = [api(`/api/archive/day/${date}?` + q)];
    if (state.showMaps) {
      promises.push(
        api(`/api/archive/day/${date}/route`)
          .catch((e) => { console.warn("route failed", e); return null; }),
      );
    } else {
      promises.push(Promise.resolve(null));
    }
    [data, route] = await Promise.all(promises);
  } catch (e) {
    body.innerHTML = `<p style="color:var(--err)">Failed to load: ${e}</p>`;
    return;
  }

  body.innerHTML = "";

  const journeys = (route && route.journeys) || [];
  const stops = (route && route.stops) || [];
  const hasGps = journeys.length > 0 || stops.length > 0;
  if (!hasGps) {
    // No journeys and no stops — fall back to a flat grid.
    const grid = document.createElement("div");
    grid.className = "clip-grid";
    for (const pair of data.clips) grid.appendChild(renderClipPair(pair));
    body.appendChild(grid);
    if (route && route.point_count === 0) {
      const note = document.createElement("p");
      note.style.cssText = "color:var(--muted);font-size:12px";
      note.textContent = "No GPS data for this day.";
      body.appendChild(note);
    }
    return;
  }

  // Build a timeline of all events (journeys + stops) ordered
  // by start time. Each event has its own clip bucket. Clips
  // whose timestamp falls inside an event's [start, end] land
  // there directly; the rest snap to whichever event is
  // closest in time, so everything is anchored to *some*
  // visible context instead of a flat "other" pile.
  const events = [
    ...journeys.map((j, idx) => ({
      kind: "journey", data: j, clips: [], idx,
      start: j.start_ts, end: j.end_ts,
    })),
    ...stops.map((s, idx) => ({
      kind: "stop", data: s, clips: [], idx,
      start: s.start_ts, end: s.end_ts,
    })),
  ];
  // Newest first — most recent activity at the top of the day.
  events.sort((a, b) => b.start - a.start);

  for (const pair of data.clips) {
    const ts = pair.timestamp;
    let target = events.find((e) => ts >= e.start && ts <= e.end);
    if (!target) {
      // Snap to the closest event by time gap.
      let best = null, bestGap = Infinity;
      for (const e of events) {
        const gap = ts < e.start ? e.start - ts : ts - e.end;
        if (gap < bestGap) { bestGap = gap; best = e; }
      }
      target = best;
    }
    if (target) target.clips.push(pair);
  }

  for (const ev of events) {
    if (ev.kind === "journey") {
      body.appendChild(renderJourneyCard(ev.data, ev.clips, ev.idx, date));
    } else {
      body.appendChild(renderStopCard(ev.data, ev.clips, ev.idx));
    }
  }

  wireClipPairMapClicks(body);
}

// Clicking anywhere on a clip-pair (except the thumbnail image,
// the checkbox, or its label) drops a pin on that pair's
// location in the enclosing journey map.
function wireClipPairMapClicks(root) {
  root.addEventListener("click", (e) => {
    if (e.target.closest("img, input, label")) return;
    const pairEl = e.target.closest(".clip-pair");
    if (!pairEl) return;
    const card = pairEl.closest(".journey-card");
    if (!card) return;
    const mapDiv = card.querySelector(".journey-map");
    const bundle = mapDiv && mapDiv._journeyMap;
    if (!bundle || !bundle.times.length) return;
    const t = Number(pairEl.dataset.ts);
    const idx = nearestTimeIdx(bundle.times, t);
    bundle.showPin(idx, { pan: true });
    bundle.scrollIntoView();
  });
}

function nearestTimeIdx(times, t) {
  let best = 0, bestD = Infinity;
  for (let i = 0; i < times.length; i++) {
    const d = Math.abs(times[i] - t);
    if (d < bestD) { bestD = d; best = i; }
  }
  return best;
}

// Reverse-geocode cache. Rounds keys to 3dp (~110 m) so
// start/end points of different journeys at the same
// location hit the same entry; same granularity the server
// uses. Dedupes concurrent requests for the same coord.
const _geocodePromises = new Map();
const _geocodeCache = new Map();
function _gkey(lat, lon) {
  return `${lat.toFixed(3)},${lon.toFixed(3)}`;
}
async function resolveGeocode(lat, lon) {
  const k = _gkey(lat, lon);
  if (_geocodeCache.has(k)) return _geocodeCache.get(k);
  if (_geocodePromises.has(k)) return _geocodePromises.get(k);
  const p = (async () => {
    try {
      const r = await api(
        `/api/archive/geocode?lat=${lat}&lon=${lon}`,
      );
      _geocodeCache.set(k, r.label || null);
      return r.label || null;
    } catch {
      return null;
    } finally {
      _geocodePromises.delete(k);
    }
  })();
  _geocodePromises.set(k, p);
  return p;
}

function fmtDuration(seconds) {
  const m = Math.round(seconds / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  const rem = m % 60;
  return rem ? `${h}h ${rem}m` : `${h}h`;
}

// ETA wants sub-minute precision; fmtDuration rounds those to "0 min".
function fmtEta(seconds) {
  if (seconds == null) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  return fmtDuration(seconds);
}

function renderStopCard(stop, clips, idx) {
  const startT = new Date(stop.start_time).toLocaleTimeString();
  const endT = new Date(stop.end_time).toLocaleTimeString();
  const hasClips = clips && clips.length > 0;
  const fallback = `${stop.lat.toFixed(3)}, ${stop.lon.toFixed(3)}`;
  const placeLabel = stop.label || fallback;

  // Empty stop → compact banner line.
  if (!hasClips) {
    const el = document.createElement("div");
    el.className = "stop-banner";
    el.innerHTML = `
      <span class="stop-icon" aria-hidden="true">⏸</span>
      <span>Stopped for <strong>${fmtDuration(stop.duration_s)}</strong>
        at <span class="stop-label">${placeLabel}</span></span>
      <span class="stop-when">${startT} – ${endT}</span>
    `;
    if (!stop.label) {
      resolveGeocode(stop.lat, stop.lon).then((label) => {
        if (label) el.querySelector(".stop-label").textContent = label;
      });
    }
    return el;
  }

  // Stop with clips → full card (static point).
  const el = document.createElement("div");
  el.className = "journey-card stop-card collapsible";
  const mapId = `stop-map-${stop.start_ts}-${idx}`;
  el.innerHTML = `
    <div class="journey-header">
      <span class="caret">▸</span>
      <input type="checkbox" class="journey-check"
             title="Select all clips in this stop" />
      <span class="journey-times">${startT} – ${endT}</span>
      <span class="stop-icon" aria-hidden="true">⏸</span>
      <strong class="journey-title">
        <span class="stop-label">${placeLabel}</span>
      </strong>
      <span class="journey-meta">
        ${fmtDuration(stop.duration_s)} ·
        ${clips.length} clip${clips.length === 1 ? "" : "s"}
      </span>
    </div>
    <div class="journey-body" hidden>
      <div id="${mapId}" class="journey-map stop-map"></div>
      <div class="clip-grid"></div>
    </div>
  `;
  if (!stop.label) {
    resolveGeocode(stop.lat, stop.lon).then((label) => {
      if (label) el.querySelector(".stop-label").textContent = label;
    });
  }
  const grid = el.querySelector(".clip-grid");
  for (const pair of clips) grid.appendChild(renderClipPair(pair));

  let mapInited = false;
  const initMap = () => {
    if (mapInited) return;
    mapInited = true;
    const mapDiv = el.querySelector(".journey-map");
    if (!mapDiv) return;
    const map = L.map(mapDiv);
    L.tileLayer(
      "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
      { attribution: "© OpenStreetMap" }
    ).addTo(map);
    const center = [stop.lat, stop.lon];
    const warn = cssVar("--warn");
    L.circleMarker(center, {
      radius: 10, color: warn,
      fillColor: warn, fillOpacity: 0.5,
    }).addTo(map).bindTooltip(
      `Parked ${fmtDuration(stop.duration_s)}`
    );
    map.setView(center, 16);
    mapDiv._journeyMap = { map };
  };

  wireJourneyToggle(el, initMap);
  wireJourneyCheck(el);
  return el;
}

function renderJourneyCard(j, clips, idx, date) {
  const el = document.createElement("div");
  el.className = "journey-card collapsible";
  const mapId = `journey-map-${j.start_ts}-${idx}`;
  const distance = fmtDistance(j.distance_m);
  const startT = new Date(j.start_time).toLocaleTimeString();
  const endT = new Date(j.end_time).toLocaleTimeString();
  const fallback = (lat, lon) =>
    `${lat.toFixed(3)}, ${lon.toFixed(3)}`;
  const startLabel = j.start_label
    || fallback(j.start_lat, j.start_lon);
  const endLabel = j.end_label
    || fallback(j.end_lat, j.end_lon);
  el.innerHTML = `
    <div class="journey-header">
      <span class="caret">▸</span>
      <input type="checkbox" class="journey-check"
             title="Select all clips in this journey" />
      <span class="journey-times">${startT} – ${endT}</span>
      <strong class="journey-title">
        <span class="start-label" data-lat="${j.start_lat}" data-lon="${j.start_lon}">${startLabel}</span>
        <span class="journey-arrow">→</span>
        <span class="end-label" data-lat="${j.end_lat}" data-lon="${j.end_lon}">${endLabel}</span>
      </strong>
      <span class="journey-meta">
        ${fmtDuration(j.duration_s)} · ${distance} · ${clips.length} clip${clips.length === 1 ? "" : "s"}
      </span>
      <button type="button" class="journey-open-tl"
              title="Open this journey in the timeline view">Timeline</button>
    </div>
    <div class="journey-body" hidden>
      <div id="${mapId}" class="journey-map"></div>
      <div class="clip-grid"></div>
    </div>
  `;

  // Kick off lazy geocoding for any endpoint we don't already
  // have a cached label for. Runs even when collapsed because the
  // header itself shows the resolved labels.
  if (!j.start_label) {
    resolveGeocode(j.start_lat, j.start_lon).then((label) => {
      if (label) {
        el.querySelector(".start-label").textContent = label;
      }
    });
  }
  if (!j.end_label) {
    resolveGeocode(j.end_lat, j.end_lon).then((label) => {
      if (label) {
        el.querySelector(".end-label").textContent = label;
      }
    });
  }
  const grid = el.querySelector(".clip-grid");
  for (const pair of clips) grid.appendChild(renderClipPair(pair));
  if (!clips.length) {
    const note = document.createElement("p");
    note.style.cssText = "color:var(--muted);font-size:12px;padding:8px";
    note.textContent = "No clips recorded for this journey.";
    grid.replaceWith(note);
  }

  // Lazy Leaflet init: defer until the journey is first opened.
  // With many journeys per day this saves mounting a dozen maps
  // upfront.
  let mapInited = false;
  const initMap = () => {
    if (mapInited) return;
    mapInited = true;
    const mapDiv = el.querySelector(".journey-map");
    if (!mapDiv) return;
    const map = L.map(mapDiv);
    L.tileLayer(
      "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
      { attribution: "© OpenStreetMap" }
    ).addTo(map);
    const coords = j.geojson.geometry.coordinates.map(
      ([lon, lat]) => [lat, lon]
    );
    const times = j.times || [];
    const accent = cssVar("--accent");
    const ok = cssVar("--ok");
    const err = cssVar("--err");
    const errText = cssVar("--err-text");
    const line = L.polyline(coords, { color: accent, weight: 5 }).addTo(map);
    L.circleMarker([j.start_lat, j.start_lon], {
      radius: 6, color: ok, fillColor: ok, fillOpacity: 1,
    }).addTo(map).bindTooltip("Start");
    L.circleMarker([j.end_lat, j.end_lon], {
      radius: 6, color: err, fillColor: err, fillOpacity: 1,
    }).addTo(map).bindTooltip("End");

    let marker = null;
    const showPin = (idx, { pan = false } = {}) => {
      if (idx < 0 || idx >= coords.length) return;
      if (marker) marker.remove();
      marker = L.circleMarker(coords[idx], {
        radius: 7, color: errText,
        fillColor: errText, fillOpacity: 1,
      }).addTo(map);
      const t = times[idx];
      if (t != null) {
        const label = new Date(t * 1000).toLocaleTimeString();
        marker.bindTooltip(label, { permanent: false }).openTooltip();
      }
      if (pan) map.panTo(coords[idx]);
    };

    line.on("click", (ev) => {
      if (!times.length) return;
      const idx = nearestCoordIdx(coords, ev.latlng);
      showPin(idx);
      const pairEl = findClipPairByTime(el, times[idx]);
      if (pairEl) flashClip(pairEl);
    });

    map.fitBounds(line.getBounds(), { padding: [20, 20] });

    mapDiv._journeyMap = {
      map, coords, times, showPin,
      scrollIntoView: () =>
        mapDiv.scrollIntoView({ behavior: "smooth", block: "nearest" }),
    };
  };

  wireJourneyToggle(el, initMap);
  wireJourneyCheck(el);
  const tlBtn = el.querySelector(".journey-open-tl");
  if (tlBtn) {
    tlBtn.addEventListener("click", (e) => {
      e.stopPropagation();              // don't toggle the card
      location.hash = `#/timeline/${date}/${idx}`;
    });
  }
  return el;
}

// Toggle handler shared by journey-card + stop-card-with-clips.
// First expansion triggers Leaflet init; subsequent expansions
// call invalidateSize() so the map re-measures after the body
// goes from display:none back to visible.
function wireJourneyToggle(el, initMap) {
  const header = el.querySelector(".journey-header");
  const body = el.querySelector(".journey-body");
  const caret = el.querySelector(".caret");
  header.addEventListener("click", (e) => {
    if (e.target.closest("input, a, button")) return;
    const opening = body.hidden;
    body.hidden = !opening;
    caret.textContent = opening ? "▾" : "▸";
    if (opening) {
      initMap();
      // Re-measure on subsequent re-expansions; harmless on first.
      const mapDiv = el.querySelector(".journey-map");
      const bundle = mapDiv && mapDiv._journeyMap;
      if (bundle && bundle.map) {
        requestAnimationFrame(() => bundle.map.invalidateSize());
      }
    }
  });
}

function nearestCoordIdx(coords, latlng) {
  // Squared-euclidean in degrees is fine — we only need the
  // argmin, and journeys span tens of km at most.
  let best = 0, bestD = Infinity;
  for (let i = 0; i < coords.length; i++) {
    const dx = coords[i][0] - latlng.lat;
    const dy = coords[i][1] - latlng.lng;
    const d = dx * dx + dy * dy;
    if (d < bestD) { bestD = d; best = i; }
  }
  return best;
}

// Clip pairs are ~60 s each; a click-time falls in the pair
// whose start is the latest one ≤ t, provided the gap is
// plausibly within one clip duration.
function findClipPairByTime(scopeEl, t) {
  const pairs = Array.from(
    scopeEl.querySelectorAll(".clip-pair[data-ts]")
  ).map((el) => ({ el, ts: Number(el.dataset.ts) }))
    .sort((a, b) => a.ts - b.ts);
  if (!pairs.length) return null;
  let candidate = null;
  for (const p of pairs) {
    if (p.ts <= t) candidate = p;
    else break;
  }
  if (!candidate) candidate = pairs[0];
  // Clip is typically 60 s; allow a little slack.
  if (Math.abs(t - candidate.ts) > 120) {
    // Fall back to the closest pair regardless of window —
    // the click was in a GPS gap, better to highlight the
    // nearest clip than nothing.
    let best = pairs[0], bestD = Infinity;
    for (const p of pairs) {
      const d = Math.abs(p.ts - t);
      if (d < bestD) { bestD = d; best = p; }
    }
    return best.el;
  }
  return candidate.el;
}

function flashClip(el) {
  el.scrollIntoView({ behavior: "smooth", block: "center" });
  el.classList.remove("flash");
  // Next frame so the animation restarts even on repeat clicks.
  requestAnimationFrame(() => {
    el.classList.add("flash");
    setTimeout(() => el.classList.remove("flash"), 1600);
  });
}

function renderClipPair(pair) {
  const el = document.createElement("div");
  el.className = "clip-pair";
  el.dataset.pairId = `${pair.timestamp}_${pair.sequence}`;
  el.dataset.ts = pair.timestamp;
  const time = new Date(pair.iso).toLocaleTimeString();
  const thumb = (c, cam) =>
    c ? `<div class="thumb" data-camera="${cam}"
               data-clip-id="${c.id}" data-ts="${pair.timestamp}">
        <img src="/api/archive/clip/${c.id}/thumb" data-id="${c.id}"
             alt="" loading="lazy" decoding="async" />
        <div class="label" title="${c.basename}">${c.basename}</div>
      </div>` : `<div class="thumb empty"><div class="label">—</div></div>`;
  // Kind badge: shown for parking / read-only pairs only. Driving
  // pairs are the common case so we leave them un-badged to keep
  // the grid quiet — absence of a chip reads as "driving".
  const kindBadge = (() => {
    if (pair.event_type === "parking") {
      return `<span class="kind-badge kind-parking">Parking</span>`;
    }
    if (pair.event_type === "ro") {
      return `<span class="kind-badge kind-ro">Read-only</span>`;
    }
    return "";
  })();
  el.innerHTML = `
    <div class="time">
      <label><input type="checkbox" data-pair="${pair.timestamp}_${pair.sequence}" /> ${time}</label>
      ${kindBadge}
    </div>
    <div class="thumbs">${thumb(pair.front, "F")}${thumb(pair.rear, "R")}</div>
  `;
  el.querySelectorAll(".thumb img").forEach((img) => {
    img.addEventListener("click", (e) => {
      const thumbEl = e.currentTarget.closest(".thumb");
      const cam = thumbEl ? thumbEl.dataset.camera : "F";
      openVideo(Number(img.dataset.id), cam, thumbEl);
    });
  });

  // Selection checkbox → export set. Preserve selected state
  // across re-renders (e.g. auto-refresh).
  const cb = el.querySelector('input[type="checkbox"]');
  const pairId = el.dataset.pairId;
  if (state.archiveSelected.has(pairId)) cb.checked = true;
  cb.addEventListener("change", () => {
    if (cb.checked) {
      const front = el.querySelector('.thumb[data-camera="F"]');
      const rear = el.querySelector('.thumb[data-camera="R"]');
      state.archiveSelected.set(pairId, {
        ts: Number(el.dataset.ts),
        front: front && front.dataset.clipId
          ? Number(front.dataset.clipId) : null,
        rear: rear && rear.dataset.clipId
          ? Number(rear.dataset.clipId) : null,
      });
    } else {
      state.archiveSelected.delete(pairId);
    }
    updateArchiveActions();
    // Reflect the new state back up to the journey/stop card's
    // "select all" checkbox so it goes checked / indeterminate /
    // unchecked as appropriate.
    const card = el.closest(".journey-card");
    if (card) refreshCardCheck(card);
  });

  return el;
}

// ---- Journey-level "select all" ----

function setCardSelection(cardEl, select) {
  cardEl.querySelectorAll('.clip-pair input[type="checkbox"]')
    .forEach((cb) => {
      if (cb.checked !== select) {
        cb.checked = select;
        // Reuse the per-clip change handler to update
        // state.archiveSelected and the action bar; the bubbles
        // arg lets this fire even when the input is detached
        // from the wider DOM (it's not, but be explicit).
        cb.dispatchEvent(new Event("change", { bubbles: true }));
      }
    });
}

function refreshCardCheck(cardEl) {
  const headerCb = cardEl.querySelector(".journey-check");
  if (!headerCb) return;
  const pairs = cardEl.querySelectorAll('.clip-pair input[type="checkbox"]');
  const total = pairs.length;
  if (total === 0) {
    headerCb.disabled = true;
    headerCb.checked = false;
    headerCb.indeterminate = false;
    return;
  }
  let checked = 0;
  pairs.forEach((p) => { if (p.checked) checked++; });
  headerCb.disabled = false;
  headerCb.checked = checked === total;
  headerCb.indeterminate = checked > 0 && checked < total;
}

function wireJourneyCheck(cardEl) {
  const cb = cardEl.querySelector(".journey-check");
  if (!cb) return;
  // Click stops here — the card-toggle handler also lives on
  // the header, and we don't want a tick to expand/collapse
  // the body. (wireJourneyToggle already short-circuits on
  // input clicks, but stopPropagation makes it explicit.)
  cb.addEventListener("click", (e) => e.stopPropagation());
  cb.addEventListener("change", (e) => {
    setCardSelection(cardEl, e.target.checked);
  });
  // Set initial state from any pre-existing per-clip selections
  // (e.g. a re-render after auto-refresh).
  refreshCardCheck(cardEl);
}

function updateArchiveActions() {
  const n = state.archiveSelected.size;
  const label = document.getElementById("selection-count");
  const bar = document.getElementById("archive-actions");
  if (n === 0) {
    label.textContent = "";
    bar.classList.remove("has-selection");
  } else {
    label.textContent = `${n} selected`;
    bar.classList.add("has-selection");
  }
  let fronts = 0, rears = 0, both = 0;
  for (const v of state.archiveSelected.values()) {
    if (v.front) fronts++;
    if (v.rear) rears++;
    if (v.front && v.rear) both++;
  }
  const hasFront = fronts > 0, hasRear = rears > 0, hasPair = both > 0;
  document.getElementById("dl-orig-front").disabled = !hasFront;
  document.getElementById("dl-orig-rear").disabled = !hasRear;
  document.getElementById("export-join-front").disabled = !hasFront;
  document.getElementById("export-join-rear").disabled = !hasRear;
  document.getElementById("export-pip-front").disabled = !hasPair;
  document.getElementById("export-pip-rear").disabled = !hasPair;
  document.getElementById("clear-selection").disabled = n === 0;
}

// Call once on load so the disabled buttons render correctly.
updateArchiveActions();

function clearSelection() {
  state.archiveSelected.clear();
  document.querySelectorAll(
    '#view-archive .clip-pair input[type="checkbox"]',
  ).forEach((cb) => { cb.checked = false; });
  // Reset every journey/stop card's "select all" header
  // checkbox so it goes back to unchecked too.
  document.querySelectorAll("#view-archive .journey-card")
    .forEach(refreshCardCheck);
  updateArchiveActions();
}

function downloadOriginals(slot) {
  // slot: "front" | "rear". Download each selected original clip
  // as its own file (no ZIP). The clip stream endpoint sends
  // Content-Disposition: attachment with the dashcam basename, so
  // a same-origin anchor click triggers a named download. Stagger
  // the clicks (150ms) so the browser queues them instead of
  // dropping all but the last, and shows its one "allow multiple
  // downloads" prompt once — small enough that a big selection
  // doesn't take ages to fire.
  const ids = [];
  for (const v of state.archiveSelected.values()) {
    if (slot === "front" && v.front) ids.push(v.front);
    else if (slot === "rear" && v.rear) ids.push(v.rear);
  }
  if (!ids.length) return;
  ids.forEach((id, i) => {
    setTimeout(() => {
      const a = document.createElement("a");
      a.href = `/api/archive/clip/${id}/video`;
      // No download attr: rely on the server's Content-Disposition
      // filename (the original basename) rather than the URL tail.
      document.body.appendChild(a);
      a.click();
      a.remove();
    }, i * 150);
  });
}

async function submitExport(type) {
  const ids = [];
  for (const v of state.archiveSelected.values()) {
    if (type === "join_front" && v.front) ids.push(v.front);
    else if (type === "join_rear" && v.rear) ids.push(v.rear);
    else if (type === "pip" || type === "pip_rear") {
      if (v.front) ids.push(v.front);
      if (v.rear) ids.push(v.rear);
    }
  }
  if (!ids.length) return;
  try {
    // Encoder is no longer chosen per-export — the backend uses
    // EXPORT_ENCODER from settings (defaults to "auto" which
    // probes for the best working hardware option at boot).
    await api("/api/exports", {
      method: "POST",
      body: JSON.stringify({ type, clip_ids: ids }),
    });
    // Selection is intentionally NOT cleared — it's common to
    // want front + rear + PiP of the same selection in
    // succession. The Clear button is right there if needed.
    setExportsPanelOpen(true);
    await refreshExportJobs();
    // Briefly highlight the panel so the user sees their job
    // land. No scrollIntoView here — the panel is now inline
    // with the action bar, already in view.
    const panel = document.getElementById("exports-panel");
    panel.classList.remove("just-submitted");
    requestAnimationFrame(() => {
      panel.classList.add("just-submitted");
      setTimeout(
        () => panel.classList.remove("just-submitted"),
        1600,
      );
    });
  } catch (e) {
    alert("Export failed: " + e);
  }
}

function setExportsPanelOpen(open) {
  const panel = document.getElementById("exports-panel");
  const toggle = document.getElementById("exports-toggle");
  panel.hidden = !open;
  toggle.setAttribute("aria-expanded", String(open));
  toggle.querySelector(".caret").textContent = open ? "▾" : "▸";
}

document.getElementById("exports-toggle").addEventListener("click", () => {
  const open = document.getElementById("exports-toggle")
    .getAttribute("aria-expanded") === "true";
  setExportsPanelOpen(!open);
});

document.getElementById("dl-orig-front")
  .addEventListener("click", () => downloadOriginals("front"));
document.getElementById("dl-orig-rear")
  .addEventListener("click", () => downloadOriginals("rear"));
document.getElementById("export-join-front")
  .addEventListener("click", () => submitExport("join_front"));
document.getElementById("export-join-rear")
  .addEventListener("click", () => submitExport("join_rear"));
document.getElementById("export-pip-front")
  .addEventListener("click", () => submitExport("pip"));
document.getElementById("export-pip-rear")
  .addEventListener("click", () => submitExport("pip_rear"));
document.getElementById("clear-selection")
  .addEventListener("click", clearSelection);

async function refreshExportJobs() {
  try {
    const r = await api("/api/exports");
    renderExportJobs(r.jobs || []);
  } catch (e) { /* non-fatal */ }
}

function updateExportsSummary(jobs) {
  // Mirror state into the <details> summary so the user can
  // see at a glance whether anything's happening, even when
  // the panel is collapsed.
  const el = document.getElementById("exports-summary");
  if (!el) return;
  if (!jobs.length) {
    el.textContent = "Export jobs";
    return;
  }
  const counts = { running: 0, queued: 0, done: 0, failed: 0 };
  for (const j of jobs) {
    if (j.state in counts) counts[j.state]++;
  }
  const parts = [];
  if (counts.running) parts.push(`${counts.running} running`);
  if (counts.queued)  parts.push(`${counts.queued} queued`);
  if (counts.done)    parts.push(`${counts.done} done`);
  if (counts.failed)  parts.push(`${counts.failed} failed`);
  el.textContent = parts.length
    ? `Export jobs · ${parts.join(" · ")}`
    : "Export jobs";
}

// Human-readable export type labels. These echo the toolbar
// buttons: Join F/R and the PiP Fr/Rf (front-main / rear-main).
const EXPORT_TYPE_LABELS = {
  join_front: "Join Front",
  join_rear: "Join Rear",
  pip: "PiP Fr",
  pip_rear: "PiP Rf",
};

// Heroicons solid (MIT) — arrow-down-tray (download) + trash (delete),
// matching the inline-SVG / currentColor pattern used in the header.
const EXPORT_ICON_DOWNLOAD =
  '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" ' +
  'aria-hidden="true"><path fill-rule="evenodd" d="M12 2.25a.75.75 0 0 1 ' +
  '.75.75v11.69l3.22-3.22a.75.75 0 1 1 1.06 1.06l-4.5 4.5a.75.75 0 0 1-1.06 ' +
  '0l-4.5-4.5a.75.75 0 1 1 1.06-1.06l3.22 3.22V3a.75.75 0 0 1 .75-.75Zm-9 ' +
  '13.5a.75.75 0 0 1 .75.75v2.25a1.5 1.5 0 0 0 1.5 1.5h13.5a1.5 1.5 0 0 0 ' +
  '1.5-1.5V16.5a.75.75 0 0 1 1.5 0v2.25a3 3 0 0 1-3 3H5.25a3 3 0 0 1-3-3V' +
  '16.5a.75.75 0 0 1 .75-.75Z" clip-rule="evenodd"/></svg>';

const EXPORT_ICON_TRASH =
  '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" ' +
  'aria-hidden="true"><path fill-rule="evenodd" d="M16.5 4.478v.227a48.816 ' +
  '48.816 0 0 1 3.878.512.75.75 0 1 1-.256 1.478l-.209-.035-1.005 13.07a3 ' +
  '3 0 0 1-2.991 2.77H8.084a3 3 0 0 1-2.991-2.77L4.087 6.66l-.209.035a.75' +
  '.75 0 0 1-.256-1.478A48.567 48.567 0 0 1 7.5 4.705v-.227c0-1.564 1.213-' +
  '2.9 2.816-2.951a52.662 52.662 0 0 1 3.369 0c1.603.051 2.815 1.387 2.815 ' +
  '2.951Zm-6.136-1.452a51.196 51.196 0 0 1 3.273 0C14.39 3.05 15 3.684 15 ' +
  '4.478v.113a49.488 49.488 0 0 0-6 0v-.113c0-.794.609-1.428 1.364-1.452Z' +
  'm-.355 5.945a.75.75 0 1 0-1.5.058l.347 9a.75.75 0 1 0 1.499-.058l-.346-' +
  '9Zm5.48.058a.75.75 0 1 0-1.498-.058l-.347 9a.75.75 0 0 0 1.5.058l.345-' +
  '9Z" clip-rule="evenodd"/></svg>';

function escapeExportText(s) {
  const d = document.createElement("div");
  d.textContent = String(s);
  return d.innerHTML;
}

// "15 Mar 14:30–15:02" (same day), "15 Mar – 17 Mar" (spans days),
// or "—" when no range was captured (jobs predating the feature, or
// clips that couldn't be resolved at enqueue time).
function formatExportRange(start, end) {
  if (!start) return "—";
  const s = new Date(start * 1000);
  const e = new Date((end || start) * 1000);
  const dOpts = { day: "numeric", month: "short" };
  const tOpts = { hour: "2-digit", minute: "2-digit", hour12: false };
  if (s.toDateString() === e.toDateString()) {
    const day = s.toLocaleDateString([], dOpts);
    const st = s.toLocaleTimeString([], tOpts);
    const et = e.toLocaleTimeString([], tOpts);
    return st === et ? `${day} ${st}` : `${day} ${st}–${et}`;
  }
  return `${s.toLocaleDateString([], dOpts)} – ` +
    `${e.toLocaleDateString([], dOpts)}`;
}

function renderExportJobs(jobs) {
  updateExportsSummary(jobs);
  const el = document.getElementById("exports-list");
  el.innerHTML = "";
  if (!jobs.length) {
    el.innerHTML = `<p style="color:var(--muted);padding:8px">
      No export jobs yet.</p>`;
    return;
  }
  const table = document.createElement("table");
  table.className = "exports-table";
  table.innerHTML = `
    <thead><tr>
      <th>Type</th><th>Status</th><th>Footage</th>
      <th class="exports-actions-col"></th>
    </tr></thead>
    <tbody></tbody>
  `;
  const tbody = table.querySelector("tbody");
  const live = state.exportProgress || {};
  for (const j of jobs) {
    const tr = document.createElement("tr");
    // Running jobs: prefer the live progress stream if we have
    // one; the DB row is only updated at finish.
    const liveHit = live[j.id];
    const progVal = liveHit && liveHit.progress != null
      ? liveHit.progress
      : j.progress;

    // Type badge.
    const label = EXPORT_TYPE_LABELS[j.type] || j.type;
    const typeCell =
      `<span class="export-type">${escapeExportText(label)}</span>`;

    // Status: state text, plus an inline progress bar while running
    // and the error message on failure.
    let statusCell = `<span class="state-${j.state}">${j.state}</span>`;
    if (j.state === "failed" && j.error) {
      statusCell +=
        `<span class="export-err"> · ${escapeExportText(j.error)}</span>`;
    }
    if (j.state === "running") {
      const pct = progVal != null ? Math.round(progVal * 100) : 0;
      const stage = liveHit && liveHit.stage ? ` · ${liveHit.stage}` : "";
      statusCell +=
        `<div class="export-progress" role="progressbar" ` +
        `aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100">` +
        `<div class="export-progress-fill" style="width:${pct}%"></div>` +
        `</div><span class="export-stage">${pct}%` +
        `${escapeExportText(stage)}</span>`;
    }

    // Footage: captured date range + clip count.
    const range = formatExportRange(j.clip_start, j.clip_end);
    const n = j.clip_count || 0;
    const footageCell = range === "—"
      ? '<span class="export-count">—</span>'
      : `${escapeExportText(range)}` +
        (n ? `<span class="export-count"> · ${n} ` +
          `clip${n === 1 ? "" : "s"}</span>` : "");

    // Actions: download (when ready) + delete. The download slot is
    // always reserved — an invisible placeholder when there's no
    // download — so the bin stays pinned to the right and never
    // jumps across as jobs finish.
    const dl = j.state === "done"
      ? `<a class="export-action" href="/api/exports/${j.id}/download" ` +
        `download title="Download" aria-label="Download export">` +
        `${EXPORT_ICON_DOWNLOAD}</a>`
      : `<span class="export-action export-action--empty" ` +
        `aria-hidden="true"></span>`;
    const del =
      `<button type="button" class="export-action export-delete" ` +
      `data-id="${j.id}" title="Delete" aria-label="Delete export">` +
      `${EXPORT_ICON_TRASH}</button>`;

    tr.innerHTML = `
      <td>${typeCell}</td>
      <td class="export-status">${statusCell}</td>
      <td class="export-footage">${footageCell}</td>
      <td class="export-actions">${dl}${del}</td>
    `;
    tbody.appendChild(tr);
  }
  el.appendChild(table);
  el.querySelectorAll(".export-delete").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm("Delete this export job and its output?")) return;
      await api(`/api/exports/${btn.dataset.id}`, { method: "DELETE" });
      refreshExportJobs();
    });
  });
}

// ---------- Video modal + nav ----------

function buildCameraTimelines(scopeEl) {
  const root = scopeEl || document.getElementById("view-archive");
  const F = [], R = [];
  root.querySelectorAll(".thumb[data-clip-id]").forEach((el) => {
    const entry = {
      id: Number(el.dataset.clipId),
      ts: Number(el.dataset.ts),
      el,
    };
    (el.dataset.camera === "F" ? F : R).push(entry);
  });
  F.sort((a, b) => a.ts - b.ts);
  R.sort((a, b) => a.ts - b.ts);
  return { F, R };
}

function openVideo(clipId, camera, sourceEl, opts = {}) {
  const { seekTo = 0, autoplay = true } = opts;
  const dayEl = sourceEl ? sourceEl.closest(".day") : null;
  const timelines = buildCameraTimelines(dayEl);
  state.modalClip = { id: clipId, camera, dayEl, timelines };

  const body = document.getElementById("modal-body");
  body.innerHTML = `<video src="/api/archive/clip/${clipId}/video"
                           controls ${autoplay ? "autoplay" : ""}></video>`;
  const video = body.querySelector("video");
  if (seekTo > 0 && video) {
    // ``currentTime`` needs metadata to be loaded before the
    // browser will honour a seek; belt-and-braces it.
    const seek = () => { video.currentTime = seekTo; };
    video.addEventListener("loadedmetadata", seek, { once: true });
  }
  if (video) {
    video.addEventListener("ended", () => {
      if (state.autoAdvance) stepVideo(+1);
    });
  }
  document.getElementById("modal").hidden = false;
  updateModalNav();
}

function updateModalNav() {
  const mc = state.modalClip;
  const prev = document.getElementById("modal-prev");
  const next = document.getElementById("modal-next");
  const toggle = document.getElementById("modal-toggle");
  if (!mc) {
    prev.disabled = next.disabled = toggle.disabled = true;
    return;
  }
  const list = mc.timelines[mc.camera];
  const i = list.findIndex((e) => e.id === mc.id);
  prev.disabled = i <= 0;
  next.disabled = i < 0 || i >= list.length - 1;

  const other = mc.camera === "F" ? "R" : "F";
  const curr = list[i];
  const match = curr
    ? mc.timelines[other].find((e) => e.ts === curr.ts)
    : null;
  toggle.disabled = !match;
  toggle.textContent = other === "R" ? "Rear view" : "Front view";
}

function stepVideo(delta) {
  const mc = state.modalClip;
  if (!mc) return;
  const list = mc.timelines[mc.camera];
  const i = list.findIndex((e) => e.id === mc.id);
  const target = list[i + delta];
  if (!target) return;
  openVideo(target.id, mc.camera, target.el);
}

function toggleVideoCamera() {
  const mc = state.modalClip;
  if (!mc) return;
  const other = mc.camera === "F" ? "R" : "F";
  const curr = mc.timelines[mc.camera].find((e) => e.id === mc.id);
  if (!curr) return;
  const match = mc.timelines[other].find((e) => e.ts === curr.ts);
  if (!match) return;
  // Preserve current playback position and pause state so the
  // other camera picks up where this one was.
  const video = document.querySelector("#modal-body video");
  const seekTo = video ? video.currentTime : 0;
  const autoplay = video ? !video.paused : true;
  openVideo(match.id, other, match.el, { seekTo, autoplay });
}

function closeModal() {
  document.getElementById("modal").hidden = true;
  document.getElementById("modal-body").innerHTML = "";
  state.modalClip = null;
}

document.getElementById("modal-close").addEventListener("click", closeModal);
document.getElementById("modal-prev").addEventListener("click", () => stepVideo(-1));
document.getElementById("modal-next").addEventListener("click", () => stepVideo(+1));
document.getElementById("modal-toggle").addEventListener("click", toggleVideoCamera);

const autoEl = document.getElementById("modal-autoplay");
autoEl.checked = state.autoAdvance;
autoEl.addEventListener("change", (e) => {
  state.autoAdvance = e.target.checked;
  localStorage.setItem("vfs.autoAdvance", state.autoAdvance ? "1" : "0");
});

document.addEventListener("keydown", (e) => {
  if (document.getElementById("modal").hidden) return;
  if (e.target.closest("input, textarea")) return;
  if (e.key === "ArrowLeft") { stepVideo(-1); e.preventDefault(); }
  else if (e.key === "ArrowRight") { stepVideo(+1); e.preventDefault(); }
  else if (e.key === "f" || e.key === "F") { toggleVideoCamera(); e.preventDefault(); }
  else if (e.key === "Escape") closeModal();
});

function renderPagination(total) {
  const pages = Math.max(1, Math.ceil(total / state.perPage));
  const el = document.getElementById("pagination");
  el.innerHTML = "";
  for (let p = 1; p <= pages; p++) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = p;
    if (p === state.page) b.style.background = "var(--accent)";
    b.addEventListener("click", () => { state.page = p; loadDays(); });
    el.appendChild(b);
  }
}

// ---------- Downloads ----------

// Two-digit hour ("00".."23") for a queue item, derived from the
// dashcam filename (YYYY_MMDD_HHMMSS_…). Filename-derived (not
// recorded_at) to stay timezone-stable and consistent with the
// server's day grouping (_day_expr in services/queue.py). Names that
// don't match bucket under "??" so they stay visible and sort last.
function hourKeyForItem(it) {
  const m = /^\d{4}_\d{4}_(\d{2})/.exec(it.filename || "");
  return m ? m[1] : "??";
}

// Bucket a day's items by hour. Returns [{ hour, items }] with hours
// newest-first and any "??" bucket last.
function groupItemsByHour(items) {
  const buckets = new Map();
  for (const it of items) {
    const hh = hourKeyForItem(it);
    if (!buckets.has(hh)) buckets.set(hh, []);
    buckets.get(hh).push(it);
  }
  const keys = [...buckets.keys()].sort((a, b) => {
    if (a === "??") return 1;
    if (b === "??") return -1;
    return Number(b) - Number(a);
  });
  return keys.map((hh) => ({ hour: hh, items: buckets.get(hh) }));
}

// Per-hour counts + summed bytes for the hour row, computed client-side
// from the bucket. State counts use bare keys (pending/downloading/done/
// failed/gone) and are consumed only by renderQueueHour — not the
// server-derived day summaries (which use *_count) — so they deliberately
// diverge from that naming.
function hourSummary(items) {
  const s = {
    clip_count: items.length, total_bytes: 0,
    pending: 0, downloading: 0, done: 0, failed: 0, gone: 0,
  };
  for (const it of items) {
    s.total_bytes += it.remote_size || 0;
    if (Object.prototype.hasOwnProperty.call(s, it.state)) s[it.state]++;
  }
  return s;
}

function queueKindParams(q) {
  q.set("driving", state.queueKinds.driving ? "true" : "false");
  q.set("parking", state.queueKinds.parking ? "true" : "false");
  q.set("ro", state.queueKinds.ro ? "true" : "false");
}

async function loadQueue() {
  const requestId = ++state.queueRequestId;
  const q = new URLSearchParams();
  queueKindParams(q);
  const data = await api("/api/queue/days?" + q);
  if (requestId !== state.queueRequestId) return;
  state.queueDays = data.days;
  // Prune selections and per-day caches for days that no longer exist.
  const liveDays = new Set(data.days.map((d) => d.day));
  for (const d of Object.keys(state.queueDayItems)) {
    if (!liveDays.has(d)) delete state.queueDayItems[d];
  }
  for (const key of [...state.queueHoursExpanded]) {
    if (!liveDays.has(key.split(" ")[0])) {
      state.queueHoursExpanded.delete(key);
    }
  }
  // Refresh items for any expanded days so live counts stay in sync.
  await Promise.all(
    [...state.queueExpanded]
      .filter((d) => liveDays.has(d))
      .map((d) => loadDayItems(d, { silent: true })),
  );
  renderQueue();
}

async function loadDayItems(day, { silent = false } = {}) {
  const q = new URLSearchParams();
  queueKindParams(q);
  const data = await api(`/api/queue/day/${day}?` + q);
  state.queueDayItems[day] = data.items;
  if (!silent) renderQueue();
}

function renderQueue() {
  const root = document.getElementById("queue-days");
  root.innerHTML = "";
  if (!state.queueDays.length) {
    root.innerHTML = `<p style="text-align:center;color:var(--muted);padding:24px">
      No queue items found.
    </p>`;
    renderQueueMeta();
    return;
  }
  for (const d of state.queueDays) {
    root.appendChild(renderQueueDayCard(d));
  }
  renderQueueMeta();
}

function renderKindBadge(it) {
  const cam = it.kind_camera || it.camera || "";
  const evt = it.kind_event || "";
  const camLabel = cam === "F" ? "Front" : cam === "R" ? "Rear" : "?";
  const parts = [`<span class="kind-badge kind-${cam}">${camLabel}</span>`];
  if (evt === "parking") {
    parts.push(`<span class="kind-badge kind-parking">Parking</span>`);
  } else if (evt === "event") {
    parts.push(`<span class="kind-badge kind-event">Event</span>`);
  }
  if (it.kind_ro) {
    parts.push(`<span class="kind-badge kind-ro">RO</span>`);
  }
  return parts.join(" ");
}

// Kept as a thin alias so existing call sites still resolve;
// fmtBytes is the canonical helper and auto-scales beyond MB/GB.
const fmtMB = fmtBytes;

function renderQueueDayCard(d) {
  const el = document.createElement("div");
  const hasPending = d.pending_count > 0;
  const isStale = !hasPending && d.downloading_count === 0;
  el.className = "day queue-day" + (isStale ? " queue-day-stale" : "");
  el.dataset.day = d.day;

  const pieces = [];
  if (d.downloading_count) pieces.push(`<span class="state-downloading">${d.downloading_count} downloading</span>`);
  if (d.pending_count)     pieces.push(`<span class="state-pending">${d.pending_count} pending</span>`);
  if (d.done_count)        pieces.push(`<span class="state-done">${d.done_count} done</span>`);
  if (d.failed_count)      pieces.push(`<span class="state-failed">${d.failed_count} failed</span>`);
  if (d.gone_count)        pieces.push(`<span class="state-gone">${d.gone_count} gone</span>`);

  const expanded = state.queueExpanded.has(d.day);
  const selected = countSelectedInDay(d.day);
  const checkState = dayCheckState(d, selected);

  el.innerHTML = `
    <div class="day-header queue-day-header">
      <span class="caret">${expanded ? "▾" : "▸"}</span>
      <input type="checkbox" class="qd-check"
             ${checkState === "checked" ? "checked" : ""}
             ${!hasPending ? "disabled title='No pending clips'" : ""} />
      <h3>${d.day}</h3>
      <div class="meta">
        ${d.clip_count} clips${
          [
            d.driving_count ? `${d.driving_count} driving` : null,
            d.parking_count ? `${d.parking_count} parking` : null,
            d.ro_count ? `${d.ro_count} read-only` : null,
          ].filter(Boolean).map((s) => ` · ${s}`).join("")
        } · ${fmtMB(d.total_bytes)}${
          d.pending_count ? ` · ${fmtMB(d.pending_bytes)} to go` : ""
        }
      </div>
      <div class="state-breakdown">${pieces.join("")}</div>
    </div>
    <div class="day-body queue-day-body" ${expanded ? "" : "hidden"}></div>
  `;

  const checkbox = el.querySelector(".qd-check");
  if (checkState === "indeterminate") checkbox.indeterminate = true;

  checkbox.addEventListener("click", (e) => e.stopPropagation());
  checkbox.addEventListener("change", async (e) => {
    const shouldSelect = e.target.checked || checkState === "indeterminate";
    if (!state.queueDayItems[d.day]) {
      await loadDayItems(d.day, { silent: true });
    }
    toggleDaySelection(d.day, shouldSelect);
    renderQueue();
  });

  el.querySelector(".queue-day-header").addEventListener("click", async (e) => {
    if (e.target.closest(".qd-check")) return;
    if (state.queueExpanded.has(d.day)) {
      state.queueExpanded.delete(d.day);
      renderQueue();
      return;
    }
    state.queueExpanded.add(d.day);
    if (!state.queueDayItems[d.day]) {
      const body = el.querySelector(".queue-day-body");
      body.hidden = false;
      body.innerHTML = `<p style="color:var(--muted);padding:8px">Loading…</p>`;
      await loadDayItems(d.day);
    } else {
      renderQueue();
    }
  });

  if (expanded && state.queueDayItems[d.day]) {
    const body = el.querySelector(".queue-day-body");
    body.appendChild(renderDayHours(d.day, state.queueDayItems[d.day]));
  }

  return el;
}

function renderDayHours(day, items) {
  const wrap = document.createElement("div");
  wrap.className = "queue-hours";
  if (!items.length) {
    wrap.innerHTML = `<p style="color:var(--muted);padding:8px">
      No files match this filter.
    </p>`;
    return wrap;
  }
  for (const group of groupItemsByHour(items)) {
    wrap.appendChild(renderQueueHour(day, group.hour, group.items));
  }
  return wrap;
}

function renderQueueHour(day, hh, items) {
  const el = document.createElement("div");
  el.className = "queue-hour";
  el.dataset.hour = hh;
  const key = `${day} ${hh}`;
  const expanded = state.queueHoursExpanded.has(key);
  const s = hourSummary(items);
  const checkState = hourCheckState(day, hh);
  const hasPending = s.pending > 0;
  const label = hh === "??" ? "Unknown time" : `${hh}:00–${hh}:59`;

  const pieces = [];
  if (s.downloading) pieces.push(`<span class="state-downloading">${s.downloading} downloading</span>`);
  if (s.pending)     pieces.push(`<span class="state-pending">${s.pending} pending</span>`);
  if (s.done)        pieces.push(`<span class="state-done">${s.done} done</span>`);
  if (s.failed)      pieces.push(`<span class="state-failed">${s.failed} failed</span>`);
  if (s.gone)        pieces.push(`<span class="state-gone">${s.gone} gone</span>`);

  el.innerHTML = `
    <div class="queue-hour-header">
      <span class="caret">${expanded ? "▾" : "▸"}</span>
      <input type="checkbox" class="qh-check" data-hour="${hh}"
             ${checkState === "checked" ? "checked" : ""}
             ${!hasPending ? "disabled title='No pending clips'" : ""} />
      <span class="hour-label">${label}</span>
      <span class="meta">${s.clip_count} clips · ${fmtMB(s.total_bytes)}</span>
      <div class="state-breakdown">${pieces.join("")}</div>
    </div>
    <div class="queue-hour-body" ${expanded ? "" : "hidden"}></div>
  `;

  const checkbox = el.querySelector(".qh-check");
  if (checkState === "indeterminate") checkbox.indeterminate = true;
  checkbox.addEventListener("click", (e) => e.stopPropagation());
  checkbox.addEventListener("change", (e) => {
    // Recompute live: this row updates surgically (no full re-render),
    // so a captured checkState would go stale across repeated clicks and
    // break deselect on an hour that began indeterminate.
    const live = hourCheckState(day, hh);
    const shouldSelect = e.target.checked || live === "indeterminate";
    toggleHourSelection(day, hh, shouldSelect);
    // Reflect onto any rendered file rows + recompute the headers,
    // surgically — no full re-render, so scroll is preserved.
    el.querySelectorAll(".queue-hour-body .qi-check").forEach((cb) => {
      cb.checked = state.queueSelected.has(cb.value);
    });
    updateHourHeaderCheckbox(day, hh);
    updateDayHeaderCheckbox(day);
    renderQueueMeta();
  });

  const header = el.querySelector(".queue-hour-header");
  header.addEventListener("click", (e) => {
    if (e.target.closest(".qh-check")) return;
    const body = el.querySelector(".queue-hour-body");
    const caret = el.querySelector(".caret");
    if (state.queueHoursExpanded.has(key)) {
      state.queueHoursExpanded.delete(key);
      body.hidden = true;
      body.innerHTML = "";
      caret.textContent = "▸";
    } else {
      state.queueHoursExpanded.add(key);
      body.appendChild(renderHourBody(day, hh, items));
      body.hidden = false;
      caret.textContent = "▾";
    }
  });

  if (expanded) {
    el.querySelector(".queue-hour-body")
      .appendChild(renderHourBody(day, hh, items));
  }
  return el;
}

function renderHourBody(day, hh, items) {
  const wrap = document.createElement("div");
  const table = document.createElement("table");
  table.className = "queue-items";
  table.innerHTML = `
    <thead><tr>
      <th></th>
      <th>Time</th>
      <th>Kind</th>
      <th>File</th>
      <th>Size</th>
      <th>State</th>
      <th>Attempts</th>
      <th>Order</th>
    </tr></thead>
    <tbody></tbody>
  `;
  const tbody = table.querySelector("tbody");
  for (const it of items) {
    const tr = document.createElement("tr");
    const size = it.remote_size ? fmtMB(it.remote_size) : "—";
    const ts = it.recorded_at
      ? new Date(it.recorded_at * 1000).toLocaleTimeString() : "—";
    const pos = it.queue_position === 0 ? "▶" :
                it.queue_position != null ? String(it.queue_position) : "—";
    const isPending = it.state === "pending";
    const checked = state.queueSelected.has(it.filename);
    const kind = renderKindBadge(it);
    tr.innerHTML = `
      <td><input type="checkbox" class="qi-check" value="${it.filename}"
            ${isPending ? "" : "disabled"}
            ${checked ? "checked" : ""} /></td>
      <td>${ts}</td>
      <td>${kind}</td>
      <td>${it.filename}</td>
      <td>${size}</td>
      <td class="state-${it.state}">${it.state}</td>
      <td>${it.attempts}</td>
      <td class="order-cell">${pos}</td>
    `;
    tbody.appendChild(tr);
  }
  tbody.addEventListener("change", (e) => {
    const cb = e.target.closest(".qi-check");
    if (!cb) return;
    if (cb.checked) state.queueSelected.add(cb.value);
    else state.queueSelected.delete(cb.value);
    // Update this file's hour checkbox and the day checkbox in place,
    // preserving scroll position (no re-render).
    updateHourHeaderCheckbox(day, hh);
    updateDayHeaderCheckbox(day);
    renderQueueMeta();
  });
  wrap.appendChild(table);
  return wrap;
}

function countSelectedInDay(day) {
  const items = state.queueDayItems[day];
  if (!items) return 0;
  let n = 0;
  for (const it of items) {
    if (it.state === "pending" && state.queueSelected.has(it.filename)) n++;
  }
  return n;
}

function dayCheckState(daySummary, selectedCount) {
  const pending = daySummary.pending_count;
  if (!pending) return "unchecked";
  // If we don't yet have the items cached, we can't know if
  // selectedCount corresponds to *this* day's pendings. Treat
  // as unchecked until expansion.
  if (!state.queueDayItems[daySummary.day]) return "unchecked";
  if (selectedCount === 0) return "unchecked";
  if (selectedCount >= pending) return "checked";
  return "indeterminate";
}

function toggleDaySelection(day, select) {
  const items = state.queueDayItems[day] || [];
  for (const it of items) {
    if (it.state !== "pending") continue;
    if (select) state.queueSelected.add(it.filename);
    else state.queueSelected.delete(it.filename);
  }
}

function updateDayHeaderCheckbox(day) {
  const card = document.querySelector(`.queue-day[data-day="${day}"]`);
  if (!card) return;
  const summary = state.queueDays.find((d) => d.day === day);
  if (!summary) return;
  const cb = card.querySelector(".qd-check");
  const selected = countSelectedInDay(day);
  const st = dayCheckState(summary, selected);
  cb.indeterminate = st === "indeterminate";
  cb.checked = st === "checked";
}

// ---- Hour-level selection (twins of the day helpers above) ----

function itemsInHour(day, hh) {
  const items = state.queueDayItems[day] || [];
  return items.filter((it) => hourKeyForItem(it) === hh);
}

function hourPendingCount(day, hh) {
  let n = 0;
  for (const it of itemsInHour(day, hh)) if (it.state === "pending") n++;
  return n;
}

function countSelectedInHour(day, hh) {
  let n = 0;
  for (const it of itemsInHour(day, hh)) {
    if (it.state === "pending" && state.queueSelected.has(it.filename)) n++;
  }
  return n;
}

function hourCheckState(day, hh) {
  const pending = hourPendingCount(day, hh);
  if (!pending) return "unchecked";
  const sel = countSelectedInHour(day, hh);
  if (sel === 0) return "unchecked";
  if (sel >= pending) return "checked";
  return "indeterminate";
}

function toggleHourSelection(day, hh, select) {
  for (const it of itemsInHour(day, hh)) {
    if (it.state !== "pending") continue;
    if (select) state.queueSelected.add(it.filename);
    else state.queueSelected.delete(it.filename);
  }
}

function updateHourHeaderCheckbox(day, hh) {
  const card = document.querySelector(`.queue-day[data-day="${day}"]`);
  if (!card) return;
  const cb = card.querySelector(`.qh-check[data-hour="${hh}"]`);
  if (!cb) return;
  const st = hourCheckState(day, hh);
  cb.indeterminate = st === "indeterminate";
  cb.checked = st === "checked";
}

function wireKindCheckbox(id, key) {
  document.getElementById(id).addEventListener("change", (e) => {
    state.queueKinds[key] = e.target.checked;
    loadQueue();
  });
}
wireKindCheckbox("q-kind-driving", "driving");
wireKindCheckbox("q-kind-parking", "parking");
wireKindCheckbox("q-kind-ro", "ro");

async function prioritizeSelected(position) {
  const selected = [...state.queueSelected];
  if (!selected.length) return;
  await api("/api/queue/prioritize", {
    method: "POST",
    body: JSON.stringify({ filenames: selected, position }),
  });
  state.queueSelected.clear();
  await loadQueue();
}
document.getElementById("q-prio-top")
  .addEventListener("click", () => prioritizeSelected("top"));

// Download most recent X hours first
document.getElementById("q-prio-recent").addEventListener("click", async () => {
  const input = document.getElementById("q-recent-hours");
  const hours = parseFloat(input.value);
  if (!hours || hours <= 0) return;
  const btn = document.getElementById("q-prio-recent");
  btn.disabled = true;
  try {
    const r = await api("/api/queue/prioritize-recent", {
      method: "POST",
      body: JSON.stringify({ hours }),
    });
    btn.textContent = `Prioritised ${r.updated} files`;
    setTimeout(() => { btn.textContent = "Download recent hours next"; }, 2000);
    await loadQueue();
  } finally {
    btn.disabled = false;
  }
});

function renderQueueMeta() {
  let total = 0;
  let pending = 0;
  let failed = 0;
  for (const d of state.queueDays) {
    total += d.clip_count;
    pending += d.pending_count;
    failed += d.failed_count || 0;
  }
  const sel = state.queueSelected.size;
  let text = `${total} files across ${state.queueDays.length} days · ${pending} pending`;
  if (sel) text += ` · ${sel} selected`;
  document.getElementById("queue-meta").textContent = text;
  updateRetryFailedButton(failed);
}

function updateRetryFailedButton(failedCount) {
  const btn = document.getElementById("q-retry-failed");
  if (!btn) return;
  btn.hidden = failedCount === 0;
  btn.textContent = `Retry failed (${failedCount})`;
}

// Re-queue every failed file. Empty body => retry all (server-side).
document.getElementById("q-retry-failed").addEventListener("click", async () => {
  const btn = document.getElementById("q-retry-failed");
  if (!window.confirm(
    "Retry all failed files? They'll be reset and re-queued for download."
  )) return;
  btn.disabled = true;
  try {
    await api("/api/queue/retry", { method: "POST", body: JSON.stringify({}) });
    await loadQueue();  // refreshes counts; button hides itself when none remain
  } finally {
    btn.disabled = false;
  }
});

function isDownloadsTabActive() {
  return !document.getElementById("view-downloads").hidden;
}

function refreshQueueIfVisible() {
  if (isDownloadsTabActive()) {
    loadQueue();
  }
}

// ---------- Logs tab ----------

const LOG_LEVELNO = {
  DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50,
};
const LOGS_PAGE = 200;       // server page size for history fetches
const LOGS_MAX_ROWS = 1000;  // cap live-tail DOM growth over a long session

function logsFilterParams() {
  return {
    level: document.getElementById("logs-level").value,
    logger: document.getElementById("logs-logger").value.trim(),
    q: document.getElementById("logs-q").value.trim(),
  };
}

function logsQueryString(f, extra = {}) {
  const params = new URLSearchParams({ level: f.level, limit: String(LOGS_PAGE), ...extra });
  if (f.logger) params.set("logger", f.logger);
  if (f.q) params.set("q", f.q);
  return params.toString();
}

function renderLogRow(e) {
  const row = document.createElement("div");
  row.className = "log-line log-" + (e.level || "").toLowerCase();
  row.dataset.id = e.id;

  const head = document.createElement("div");
  head.className = "log-head";

  const ts = document.createElement("span");
  ts.className = "log-ts";
  ts.textContent = new Date(e.ts * 1000).toLocaleTimeString();

  const lvl = document.createElement("span");
  lvl.className = "log-level";
  lvl.textContent = e.level;

  const logger = document.createElement("span");
  logger.className = "log-logger";
  logger.textContent = e.logger;

  const msg = document.createElement("span");
  msg.className = "log-msg";
  msg.textContent = e.message;

  head.append(ts, lvl, logger, msg);
  row.appendChild(head);

  if (e.exc_text) {
    head.classList.add("has-exc");
    head.addEventListener("click", () => row.classList.toggle("open"));
    const pre = document.createElement("pre");
    pre.className = "log-exc";
    pre.textContent = e.exc_text;
    row.appendChild(pre);
  }
  return row;
}

async function loadLogs() {
  const f = logsFilterParams();
  state.logsFilter = f;
  const list = document.getElementById("logs-list");
  const older = document.getElementById("logs-older");
  list.innerHTML = "";
  let entries = [];
  try {
    entries = (await api(`/api/logs?${logsQueryString(f)}`)).entries;
  } catch { return; }
  if (!entries.length) {
    const empty = document.createElement("div");
    empty.className = "logs-empty";
    empty.textContent = "No log entries match the current filter.";
    list.appendChild(empty);
  } else {
    for (const e of entries) list.appendChild(renderLogRow(e));
  }
  state.logsOldestId = entries.length ? entries[entries.length - 1].id : null;
  // A short page means there is no older history behind it.
  if (older) {
    const more = entries.length >= LOGS_PAGE;
    older.disabled = !more;
    older.textContent = more ? "Load older" : "No older entries";
  }
}

async function loadOlderLogs() {
  if (!state.logsOldestId) return;
  const f = state.logsFilter || logsFilterParams();
  const qs = logsQueryString(f, { before: String(state.logsOldestId) });
  const older = document.getElementById("logs-older");
  let entries = [];
  try { entries = (await api(`/api/logs?${qs}`)).entries; }
  catch { return; }
  const list = document.getElementById("logs-list");
  for (const e of entries) list.appendChild(renderLogRow(e));
  if (entries.length) state.logsOldestId = entries[entries.length - 1].id;
  if (older && entries.length < LOGS_PAGE) {
    older.disabled = true;
    older.textContent = "No older entries";
  }
}

function logMatchesFilter(e, f) {
  const min = LOG_LEVELNO[f.level] || 30;
  const lvl = e.levelno || LOG_LEVELNO[e.level] || 0;
  if (lvl < min) return false;
  if (f.logger && !(e.logger || "").includes(f.logger)) return false;
  if (f.q && !(e.message || "").toLowerCase().includes(f.q.toLowerCase())) {
    return false;
  }
  return true;
}

function logsLive(e) {
  const view = document.getElementById("view-logs");
  const list = document.getElementById("logs-list");
  if (!list || !view || view.hidden) return;
  // NB: state.logsFilter only resyncs when loadLogs() runs (on a toolbar
  // `change` or Refresh), so for a few seconds after editing a search box
  // (which commits on blur/Enter) live rows are matched against the prior
  // filter; loadLogs() re-renders cleanly on commit.
  const f = state.logsFilter || logsFilterParams();
  if (!logMatchesFilter(e, f)) return;
  const placeholder = list.querySelector(".logs-empty");
  if (placeholder) placeholder.remove();
  const atTop = list.scrollTop <= 4;
  list.insertBefore(renderLogRow(e), list.firstChild);
  if (atTop) list.scrollTop = 0;
  // Bound live-tail growth (the old event-log panel capped at 200). Trim the
  // oldest rows and keep logsOldestId at the oldest visible id so paging stays
  // contiguous.
  if (list.children.length > LOGS_MAX_ROWS) {
    while (list.children.length > LOGS_MAX_ROWS) list.removeChild(list.lastChild);
    if (list.lastChild) state.logsOldestId = Number(list.lastChild.dataset.id);
  }
}

document.getElementById("logs-refresh").addEventListener("click", loadLogs);
document.getElementById("logs-older").addEventListener("click", loadOlderLogs);
for (const id of ["logs-level", "logs-logger", "logs-q"]) {
  document.getElementById(id).addEventListener("change", loadLogs);
}

function openSocket() {
  if (state.ws) { try { state.ws.close(); } catch {} }
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  state.ws = new WebSocket(`${proto}//${location.host}/api/progress`);
  state.ws.addEventListener("message", (e) => {
    try { handleEvent(JSON.parse(e.data)); } catch {}
  });
  state.ws.addEventListener("close", () => {
    setTimeout(openSocket, 3000);
  });
}

function handleEvent(ev) {
  const statusEl = document.getElementById("sync-status");
  const STATUS_LABEL = {
    downloading: "Downloading",
    waiting: "Waiting",
    paused: "Paused",
    error: "Error",
  };
  const applyStatus = (status, reason) => {
    state.syncStatus = status;
    state.syncStatusReason = reason || null;
    // For error states, surface the reason directly in the badge text
    // rather than burying it in a hover-only tooltip — users won't
    // know to hover, and on touch devices the tooltip is invisible.
    let badgeText = STATUS_LABEL[status] || "";
    if (status === "error" && reason) {
      badgeText = "Error: " + reason;
    }
    statusEl.textContent = badgeText;
    statusEl.className = "status " + (status || "");
    statusEl.title = reason || "";
    updateSyncState(status);
  };
  switch (ev.type) {
    case "snapshot":
      if (ev.state.sync_status) {
        applyStatus(ev.state.sync_status, ev.state.sync_status_reason);
      }
      if (ev.state.current_item) updateCurrent(ev.state.current_item);
      if (ev.state.session) updateSessionStats(ev.state.session);
      if (ev.state.sync_state) {
        state.syncRunning = ev.state.sync_state.running;
        state.syncPaused = ev.state.sync_state.paused;
      }
      state.dashcamSource = ev.state.dashcam_source || "primary";
      updateConnectionChip();
      break;
    case "sync_status":
      applyStatus(ev.status, ev.reason);
      break;
    case "sync_state":
      state.syncRunning = ev.running;
      state.syncPaused = ev.paused;
      // Status follow-up will arrive separately; don't drive the badge here.
      break;
    case "dashcam_online":
      state.dashcamSource = ev.source || "primary";
      updateConnectionChip();
      break;
    case "dashcam_offline":
      // Keep state.dashcamSource (last known) so the chip persists.
      updateConnectionChip();
      break;
    case "item_started":
      updateCurrent({ filename: ev.filename, total: ev.total, bytes: 0 });
      refreshQueueIfVisible();
      break;
    case "item_progress":
      updateCurrent(ev);
      break;
    case "item_finished":
      document.getElementById("current-download").innerHTML = "";
      state.currentFilename = null;
      refreshQueueIfVisible();
      break;
    case "session_stats":
      updateSessionStats(ev);
      break;
    case "queue_reconciled":
    case "sync_done":
      refreshQueueIfVisible();
      break;
    case "gps_extract_started":
    case "gps_extract_progress":
      setExtractButton({
        running: true, done: ev.done || 0, total: ev.total || 0,
      });
      break;
    case "gps_extract_done":
      setExtractButton({
        running: false,
        done: ev.done, total: ev.total,
        extracted: ev.extracted, empty: ev.empty, errors: ev.errors,
      });
      if (!document.getElementById("view-archive").hidden) {
        loadDays();
      }
      break;
    case "export_progress":
      state.exportProgress = state.exportProgress || {};
      state.exportProgress[ev.job_id] = {
        progress: ev.progress, stage: ev.stage,
      };
      if (!document.getElementById("view-archive").hidden) {
        refreshExportJobs();
      }
      break;
    case "export_finished":
      if (state.exportProgress) delete state.exportProgress[ev.job_id];
      if (!document.getElementById("view-archive").hidden) {
        refreshExportJobs();
      }
      break;
    case "import_started":
    case "import_progress":
    case "import_done":
      if (window.__importOnEvent) window.__importOnEvent(ev);
      break;
    case "log":
      logsLive(ev);
      break;
  }
}

// Event delegation for skip button — avoids race with innerHTML replacement
document.getElementById("current-download").addEventListener("click", (e) => {
  if (e.target.closest(".cancel-btn")) {
    skipCurrentDownload();
  }
});

function updateCurrent(info) {
  const el = document.getElementById("current-download");
  const pct = info.total ? (100 * info.bytes / info.total).toFixed(1) : 0;
  const done = fmtBytes(info.bytes);
  const total = info.total ? fmtBytes(info.total) : "?";
  const speed = info.speed ? `${fmtBytes(info.speed)}/s` : "";
  el.innerHTML = `
    <div class="current-header">
      <strong>${info.filename}</strong>
      <span class="spacer"></span>
      <button type="button" class="cancel-btn"
              title="Skip this file" aria-label="Skip this file">&times;</button>
    </div>
    <div style="color:var(--muted);font-size:12px">
      ${done} / ${total} · ${pct}% · ${speed}
    </div>
    <div class="bar"><div style="width:${pct}%"></div></div>
  `;
  state.currentFilename = info.filename;
}

function updateSessionStats(s) {
  const el = document.getElementById("session-stats");
  if (!el) return;
  if (!s || !s.active) {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  const parts = [];
  if (s.avg_speed_bps != null) {
    parts.push(`avg ${fmtBytes(s.avg_speed_bps)}/s`);
  }
  if (s.eta_seconds != null) {
    parts.push(`ETA ${fmtEta(s.eta_seconds)}`);
  }
  parts.push(`${fmtBytes(s.session_bytes)} this session`);
  el.textContent = "Session · " + parts.join(" · ");
  el.hidden = false;
}

function updateConnectionChip() {
  let chip = document.getElementById("conn-chip");
  const onAlt = state.dashcamSource === "alternative";
  if (!onAlt) {
    if (chip) chip.remove();
    return;
  }
  if (!chip) {
    chip = document.createElement("span");
    chip.id = "conn-chip";
    chip.className = "kind-badge";
    chip.title = "Connected to the camera via the alternative address";
    chip.textContent = "via alternative";
    const anchor = document.getElementById("sync-status");
    if (anchor) anchor.insertAdjacentElement("beforebegin", chip);
  }
}

// ---------- Settings ----------
//
// Section renderers paint into #settings-pane based on the hash
// route. Edits accumulate in settingsState.pending; Save PUTs the
// diff and reloads. CSRF is handled by api().

const settingsState = {
  current: null,
  pending: {},
  readonly: null,
  restart_required: [],
};

async function loadSettings() {
  const pane = document.getElementById("settings-pane");
  try {
    const body = await api("/api/settings");
    settingsState.current = body.editable;
    settingsState.readonly = body.readonly;
    settingsState.restart_required = body.restart_required_keys || [];
    settingsState.pending = {};
    renderSettingsSection(currentSettingsSection());
    updateSettingsFooter();
  } catch (e) {
    pane.innerHTML =
      `<p class="error">Failed to load settings: ${e}</p>`;
  }
}

function currentSettingsSection() {
  const m = window.location.hash.match(/^#\/settings\/(\w+)/);
  return m ? m[1] : "dashcam";
}

function renderSettingsSection(name) {
  document.querySelectorAll(".settings-nav-link").forEach((a) => {
    a.classList.toggle("active", a.dataset.section === name);
  });
  const pane = document.getElementById("settings-pane");
  if (!pane) return;
  const fns = {
    dashcam: renderDashcamSection,
    sync: renderSyncSection,
    gps: renderGpsSection,
    exports: renderExportsSection,
    archive: renderArchiveSection,
    web: renderWebSection,
    security: renderSecuritySection,
    system: renderSystemSection,
    mqtt: renderMqttSection,
  };
  // Clear MQTT status polling if navigating away from that section.
  if (name !== "mqtt" && _mqttStatusTimer) {
    clearInterval(_mqttStatusTimer);
    _mqttStatusTimer = null;
  }
  pane.innerHTML = "";
  (fns[name] || fns.dashcam)(pane);
}

function setPending(key, value) {
  if (settingsState.current && settingsState.current[key] === value) {
    delete settingsState.pending[key];
  } else {
    settingsState.pending[key] = value;
  }
  updateSettingsFooter();
}

function updateSettingsFooter() {
  const footer = document.getElementById("settings-footer");
  const summary = document.getElementById("settings-pending-summary");
  if (!footer || !summary) return;
  const n = Object.keys(settingsState.pending).length;
  footer.hidden = n === 0;
  summary.textContent = n === 0 ? "" : `${n} change${n === 1 ? "" : "s"}`;
}

// ---- Field helpers ----
//
// Each section renderer composes one of these per editable key. The
// helpers read from `settingsState.pending` if present, else from
// `settingsState.current`, so unsaved edits survive section switches
// within a single load cycle.

function renderField(pane, key, label, control) {
  const row = document.createElement("div");
  row.className = "form-row";
  const lbl = document.createElement("label");
  lbl.textContent = label;
  if (settingsState.restart_required.includes(key)) {
    const chip = document.createElement("span");
    chip.className = "restart-required-chip";
    chip.textContent = "restart required";
    lbl.appendChild(chip);
  }
  row.appendChild(lbl);
  row.appendChild(control);
  pane.appendChild(row);
}

function valueOf(key) {
  return key in settingsState.pending
    ? settingsState.pending[key]
    : settingsState.current[key];
}

function textInput(key, opts = {}) {
  const inp = document.createElement("input");
  inp.type = opts.type || "text";
  if (opts.min !== undefined) inp.min = opts.min;
  if (opts.max !== undefined) inp.max = opts.max;
  inp.value = valueOf(key);
  inp.addEventListener("input", () => {
    let v = inp.value;
    if (opts.type === "number") v = inp.value === "" ? "" : Number(inp.value);
    setPending(key, v);
  });
  return inp;
}

function checkbox(key) {
  const inp = document.createElement("input");
  inp.type = "checkbox";
  inp.checked = !!valueOf(key);
  inp.addEventListener("change", () => setPending(key, inp.checked));
  return inp;
}

function select(key, options) {
  // Accepts a flat ['a', 'b'] array OR a list of [value, label]
  // tuples for nicer display when the backend value isn't itself
  // user-friendly (e.g. snake_case enums).
  const sel = document.createElement("select");
  for (const o of options) {
    const opt = document.createElement("option");
    if (Array.isArray(o)) {
      opt.value = o[0]; opt.textContent = o[1];
    } else {
      opt.value = o; opt.textContent = o;
    }
    if (opt.value === valueOf(key)) opt.selected = true;
    sel.appendChild(opt);
  }
  sel.addEventListener("change", () => setPending(key, sel.value));
  return sel;
}

// ---- Section renderers ----
//
// api() throws on non-OK statuses, so failures land in catch
// rather than as `{ ok: false }` payloads.

function renderDashcamSection(pane) {
  const row = document.createElement("div");
  row.className = "form-row";
  const lbl = document.createElement("label");
  lbl.textContent = "Dashcam IP or hostname";
  row.appendChild(lbl);
  const wrap = document.createElement("div");
  wrap.style.display = "flex";
  wrap.style.gap = "8px";
  const inp = textInput("ADDRESS");
  wrap.appendChild(inp);
  const test = document.createElement("button");
  test.type = "button";
  test.textContent = "Test";
  const result = document.createElement("span");
  result.className = "hint";
  test.addEventListener("click", async () => {
    result.textContent = "Testing…";
    try {
      const j = await api("/api/settings/test-dashcam", {
        method: "POST",
        body: JSON.stringify({ address: inp.value }),
      });
      result.textContent = j.ok
        ? `Reachable (${j.latency_ms}ms)`
        : `Failed: ${j.error}`;
    } catch (e) {
      result.textContent = `Failed: ${e.message || e}`;
    }
  });
  wrap.appendChild(test);
  row.appendChild(wrap);
  row.appendChild(result);
  pane.appendChild(row);

  const altRow = document.createElement("div");
  altRow.className = "form-row";
  const altLbl = document.createElement("label");
  altLbl.textContent = "Alternative address";
  altRow.appendChild(altLbl);
  const altWrap = document.createElement("div");
  altWrap.style.display = "flex";
  altWrap.style.gap = "8px";
  const altInp = textInput("ADDRESS_FALLBACK");
  altWrap.appendChild(altInp);
  const altTest = document.createElement("button");
  altTest.type = "button";
  altTest.textContent = "Test";
  const altResult = document.createElement("span");
  altResult.className = "hint";
  altResult.style.margin = "0";
  altTest.addEventListener("click", async () => {
    altResult.textContent = "Testing…";
    try {
      const j = await api("/api/settings/test-dashcam", {
        method: "POST",
        body: JSON.stringify({ address: altInp.value }),
      });
      altResult.textContent = j.ok
        ? `Reachable (${j.latency_ms}ms)`
        : `Failed: ${j.error}`;
    } catch (e) {
      altResult.textContent = `Failed: ${e.message || e}`;
    }
  });
  altWrap.appendChild(altTest);
  altRow.appendChild(altWrap);

  // Help text lives inside the field's form-row (like the Test result
  // below it) so it's grouped with the field and constrained to the
  // field width, rather than dangling as a wider detached paragraph.
  // margin:0 lets the row's flex gap own the spacing.
  const altNote = document.createElement("p");
  altNote.className = "hint";
  altNote.style.margin = "0";
  altNote.textContent =
    "Optional second IP/host for the SAME camera, used only when the " +
    "primary is unreachable — for example downloading over a VPN when the " +
    "car is parked elsewhere. NOT for a second camera.";
  altRow.appendChild(altNote);

  altRow.appendChild(altResult);
  pane.appendChild(altRow);

  renderField(pane, "HTML", "Use HTML directory listing", checkbox("HTML"));
  const htmlNote = document.createElement("p");
  htmlNote.className = "hint";
  htmlNote.textContent =
    "Use this option if you experience problems with the XML " +
    "loading slowly or missing files.";
  pane.appendChild(htmlNote);
  renderField(pane, "TIMEOUT", "Socket timeout (seconds)",
              textInput("TIMEOUT", { type: "number", min: 1, max: 60 }));
}

function renderSyncSection(pane) {
  renderField(pane, "ENABLE_SCHEDULED_SYNC", "Run scheduled sync", checkbox("ENABLE_SCHEDULED_SYNC"));
  renderField(pane, "SYNC_INTERVAL", "Sync interval (seconds)",
              textInput("SYNC_INTERVAL", { type: "number", min: 60, max: 86400 }));
  renderField(pane, "DOWNLOAD_ATTEMPTS", "Per-cycle retry count",
              textInput("DOWNLOAD_ATTEMPTS", { type: "number", min: 1, max: 10 }));
  renderField(pane, "MAX_DOWNLOAD_ATTEMPTS", "Total retry budget",
              textInput("MAX_DOWNLOAD_ATTEMPTS", { type: "number", min: 1, max: 20 }));

  renderField(
    pane,
    "SYNC_RO_ONLY",
    "Sync read-only files only",
    checkbox("SYNC_RO_ONLY"),
  );
  const roNote = document.createElement("p");
  roNote.className = "hint";
  roNote.textContent =
    "Pulls only clips that you've locked / saved on the dashcam " +
    "(e.g. impact events). Useful when you want the local archive " +
    "to mirror your manually-protected clips and ignore everyday " +
    "driving footage. Toggling this off resumes any non-RO clips " +
    "that were already queued.";
  pane.appendChild(roNote);

  renderField(
    pane,
    "DELETE_AFTER_DOWNLOAD",
    "Delete clips from dashcam after download",
    checkbox("DELETE_AFTER_DOWNLOAD"),
  );
  const dnote = document.createElement("p");
  dnote.className = "hint";
  dnote.textContent =
    "Frees space on the dashcam SD card by removing each clip from the device " +
    "once it's safely downloaded and verified. Read-only / locked clips are never " +
    "deleted. This is irreversible — make sure your local archive is the master copy.";
  pane.appendChild(dnote);
}

function renderGpsSection(pane) {
  renderField(pane, "GPS_EXTRACT", "Extract GPX after each download", checkbox("GPS_EXTRACT"));
  renderField(pane, "GEOCODE_ENABLED", "Reverse-geocode journey endpoints", checkbox("GEOCODE_ENABLED"));
  renderField(pane, "NOMINATIM_EMAIL", "Contact email for Nominatim (optional)",
              textInput("NOMINATIM_EMAIL"));
  renderField(pane, "DISTANCE_UNITS", "Distance units",
              select("DISTANCE_UNITS", ["km", "miles"]));
}

function renderExportsSection(pane) {
  renderField(pane, "EXPORT_ENCODER", "H.264 encoder",
              select("EXPORT_ENCODER", ["auto", "software", "videotoolbox", "nvenc", "qsv", "vaapi"]));
  renderField(pane, "PIP_POSITION", "Picture-in-picture position",
              select("PIP_POSITION", [
                ["top_right",    "Top right"],
                ["top_left",     "Top left"],
                ["bottom_right", "Bottom right"],
                ["bottom_left",  "Bottom left"],
              ]));
}

function renderArchiveSection(pane) {
  renderField(pane, "GROUPING", "Folder layout",
              select("GROUPING", ["none", "daily", "weekly", "monthly", "yearly"]));

  const h = document.createElement("h3");
  h.textContent = "Archive Retention";
  h.style.marginTop = "24px";
  pane.appendChild(h);

  // Live usage card. Sits between the heading and the threshold input
  // so users can see what their threshold is being measured against.
  const usageCard = document.createElement("div");
  usageCard.className = "storage-usage";
  usageCard.innerHTML = `
    <div class="storage-usage-row">
      <span class="storage-usage-label">Current usage</span>
      <span class="storage-usage-value">…</span>
    </div>
    <div class="storage-usage-bar">
      <div class="storage-usage-fill" style="width:0%"></div>
      <div class="storage-usage-threshold" style="display:none"></div>
    </div>
    <p class="hint storage-usage-mode">…</p>
  `;
  pane.appendChild(usageCard);
  refreshStorageUsage(usageCard);

  renderField(
    pane,
    "RETENTION_MAX_DAYS",
    "Retain for N days (0 = unlimited)",
    textInput("RETENTION_MAX_DAYS", { type: "number", min: 0, max: 3650 }),
  );
  renderField(
    pane,
    "RETENTION_DISK_PCT",
    "Trigger cleanup at N% of filesystem (0 = disabled)",
    textInput("RETENTION_DISK_PCT", { type: "number", min: 0, max: 99 }),
  );
  renderField(
    pane,
    "RECORDINGS_QUOTA_GB",
    "Trigger cleanup at this many GiB of recordings (0 = disabled)",
    textInput("RECORDINGS_QUOTA_GB", { type: "number", min: 0, max: 1048576 }),
  );
  renderField(
    pane,
    "RETENTION_PROTECT_RO",
    "Never delete read-only clips",
    checkbox("RETENTION_PROTECT_RO"),
  );
  const rnote = document.createElement("p");
  rnote.className = "hint";
  rnote.textContent =
    "Cleanup runs after each sync cycle. Files older than the day cap are " +
    "always removed first. The two disk-pressure triggers below are " +
    "independent — either or both may be set. Filesystem % is the right " +
    "choice when recordings live on a dedicated volume. GiB quota is the " +
    "right choice when recordings sit inside a Synology share / ZFS " +
    "dataset / other quota-bound mount where the filesystem's reported " +
    "free space doesn't reflect the actual limit. If both are set, " +
    "cleanup runs whenever either is breached.";
  pane.appendChild(rnote);

  renderField(
    pane,
    "DISK_CRITICAL_PCT",
    "Flag an error at N% of filesystem full (0 = disabled)",
    textInput("DISK_CRITICAL_PCT", { type: "number", min: 0, max: 100 }),
  );
  const cnote = document.createElement("p");
  cnote.className = "hint";
  cnote.textContent =
    "When the filesystem reaches this level, sync stops and the status " +
    "flips to an error (“disk N% full”). Keep it at or above the " +
    "filesystem-% cleanup trigger above so retention gets a chance to free " +
    "space first.";
  pane.appendChild(cnote);
}

// ---- Storage usage card ----

function _fmtBytes(n) {
  if (!n || n <= 0) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return (n < 10 ? n.toFixed(1) : Math.round(n)) + " " + units[i];
}

async function refreshStorageUsage(card) {
  if (!card || !card.isConnected) return;
  let body;
  try {
    body = await api("/api/storage/usage");
  } catch (_) {
    return;
  }
  if (!card.isConnected) return;

  const value = card.querySelector(".storage-usage-value");
  const fill  = card.querySelector(".storage-usage-fill");
  const mark  = card.querySelector(".storage-usage-threshold");
  const mode  = card.querySelector(".storage-usage-mode");

  if (body.total_bytes <= 0 || body.used_pct === null) {
    value.textContent = "Unavailable";
    fill.style.width = "0%";
    mark.style.display = "none";
    mode.textContent = body.mode === "quota"
      ? "Quota set but path unreadable."
      : "Could not read filesystem stats.";
    return;
  }

  const pct = body.used_pct;
  value.textContent =
    `${pct.toFixed(1)}% — ${_fmtBytes(body.used_bytes)} of ${_fmtBytes(body.total_bytes)}`;
  fill.style.width = Math.min(100, pct) + "%";

  // Tint the fill if we're at or past the cleanup threshold.
  fill.classList.toggle("over-threshold",
    body.threshold_pct != null && pct >= body.threshold_pct);

  if (body.threshold_pct != null && body.threshold_pct > 0) {
    mark.style.display = "block";
    mark.style.left = Math.min(100, body.threshold_pct) + "%";
    mark.title = `Cleanup threshold: ${body.threshold_pct}%`;
  } else {
    mark.style.display = "none";
  }

  mode.textContent = body.mode === "quota"
    ? `Measured against your ${_fmtBytes(body.total_bytes)} quota.`
    : `Measured against the filesystem containing recordings.`;
}


function renderWebSection(pane) {
  renderField(pane, "WEB_HOST", "Bind host", textInput("WEB_HOST"));
  renderField(pane, "WEB_PORT", "Listen port",
              textInput("WEB_PORT", { type: "number", min: 1, max: 65535 }));
  const note = document.createElement("p");
  note.className = "hint";
  note.textContent =
    "Saving here writes to disk but takes effect after the container restarts. " +
    "If the new value is unbindable, the change is rejected.";
  pane.appendChild(note);
}

function renderSecuritySection(pane) {
  // SAFETY: this section uses innerHTML for layout. The interpolated values are
  // hardcoded strings only — no user-controlled data.
  pane.innerHTML = `
    <h3>Change password</h3>
    <div class="form-row"><label>Current password</label><input type="password" id="pw-current" autocomplete="current-password" /></div>
    <div class="form-row"><label>New password (min 8 characters)</label><input type="password" id="pw-new" autocomplete="new-password" /></div>
    <div class="form-row"><label>Confirm new password</label><input type="password" id="pw-confirm" autocomplete="new-password" /></div>
    <div class="form-row"><label><input type="checkbox" id="pw-logout-others" /> Log out other sessions</label></div>
    <button type="button" id="pw-save">Change password</button>
    <span id="pw-result" class="hint" aria-live="polite"></span>
    <h3>Session secret</h3>
    <p class="hint">Rotating the session secret logs out every session, including this one.</p>
    <button type="button" id="rotate-secret">Rotate session secret</button>
  `;
  document.getElementById("pw-save").addEventListener("click", async () => {
    const cur = document.getElementById("pw-current").value;
    const nw = document.getElementById("pw-new").value;
    const cf = document.getElementById("pw-confirm").value;
    const lo = document.getElementById("pw-logout-others").checked;
    const result = document.getElementById("pw-result");
    if (nw !== cf) { result.textContent = "Passwords don't match"; return; }
    try {
      await api("/api/settings/password", {
        method: "POST",
        body: JSON.stringify({ current: cur, new_password: nw, logout_others: lo }),
      });
      result.textContent = "Password updated";
    } catch (e) {
      result.textContent = `Failed: ${e.message || e}`;
    }
  });
  document.getElementById("rotate-secret").addEventListener("click", async () => {
    if (!confirm("Rotate the session secret? You will be logged out.")) return;
    await api("/api/auth/logout", { method: "POST" });
    window.location.reload();
  });
}

function renderSystemSection(pane) {
  const ro = settingsState.readonly || {};
  // SAFETY: ro.* values come from server config (PUID/PGID/TZ from Docker env, RECORDINGS from /recordings mount).
  // Server-side these are os.environ reads — but to be defensive, escape user-visible values.
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, c =>
      ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c]));
  }
  pane.innerHTML = `
    <h3>Container</h3>
    <dl class="readonly">
      <dt>PUID</dt><dd>${esc(ro.PUID) || "—"}</dd>
      <dt>PGID</dt><dd>${esc(ro.PGID) || "—"}</dd>
      <dt>TZ</dt><dd>${esc(ro.TZ) || "—"}</dd>
      <dt>Recordings folder</dt><dd>${esc(ro.RECORDINGS) || "—"}</dd>
      <dt>Config file</dt><dd>${esc(ro.CONFIG_FILE) || "—"}</dd>
    </dl>
    <p class="hint">These come from Docker environment and can't be changed at runtime.</p>
    <button type="button" id="restart-now">Restart container</button>
  `;
  document.getElementById("restart-now").addEventListener("click", async () => {
    if (!confirm("Restart the container now? Active downloads will be re-queued.")) return;
    await api("/api/settings/restart", { method: "POST" });
    document.body.innerHTML = "<h1>Restarting…</h1><p>The page will reload in 5 seconds.</p>";
    setTimeout(() => window.location.reload(), 5000);
  });
}

// ---- MQTT section ----

let _mqttStatusTimer = null;

async function refreshMqttStatus() {
  const el = document.getElementById("mqtt-status");
  if (!el) { clearInterval(_mqttStatusTimer); _mqttStatusTimer = null; return; }
  try {
    const body = await api("/api/mqtt/status");
    const dot  = el.querySelector(".dot");
    const text = el.querySelector(".mqtt-status-text");
    dot.className = "dot " + ({
      connected:    "green",
      connecting:   "amber",
      reconnecting: "amber",
      error:        "red",
      disabled:     "grey",
      idle:         "grey",
    }[body.state] || "grey");
    text.textContent = ({
      connected:    `Connected (${body.detail || ""})`,
      connecting:   `Connecting (${body.detail || ""})`,
      reconnecting: `Reconnecting (${body.detail || ""})`,
      error:        `Error: ${body.detail || ""}`,
      disabled:     "Disabled",
      idle:         body.detail || "Not configured",
    }[body.state] || body.state);
  } catch (_) { /* silently ignore if panel navigated away */ }
}

function renderMqttSection(pane) {
  const hint = document.createElement("p");
  hint.className = "hint";
  hint.textContent =
    "Publish state and accept actions over MQTT, with Home Assistant " +
    "auto-discovery. See README for the topic structure.";
  pane.appendChild(hint);

  renderField(pane, "MQTT_ENABLED", "Enable MQTT", checkbox("MQTT_ENABLED"));
  renderField(pane, "MQTT_HOST", "Broker host", textInput("MQTT_HOST"));
  renderField(pane, "MQTT_PORT", "Port",
              textInput("MQTT_PORT", { type: "number", min: 1, max: 65535 }));
  renderField(pane, "MQTT_USERNAME", "Username", textInput("MQTT_USERNAME"));
  renderField(pane, "MQTT_PASSWORD", "Password",
              textInput("MQTT_PASSWORD", { type: "password" }));
  renderField(pane, "MQTT_TLS", "Use TLS", checkbox("MQTT_TLS"));
  renderField(pane, "MQTT_CLIENT_ID", "Client ID", textInput("MQTT_CLIENT_ID"));
  renderField(pane, "MQTT_NODE_ID", "Node ID", textInput("MQTT_NODE_ID"));
  renderField(pane, "MQTT_DISCOVERY_PREFIX", "Discovery prefix",
              textInput("MQTT_DISCOVERY_PREFIX"));
  renderField(pane, "MQTT_DISCOVERY_ENABLED", "Publish Home Assistant discovery",
              checkbox("MQTT_DISCOVERY_ENABLED"));
  renderField(pane, "MQTT_QOS", "QoS", select("MQTT_QOS", [0, 1, 2]));

  // Status indicator
  const statusEl = document.createElement("p");
  statusEl.id = "mqtt-status";
  statusEl.innerHTML = '<span class="dot grey"></span><span class="mqtt-status-text">Disabled</span>';
  pane.appendChild(statusEl);

  // Test connection button
  const testBtn = document.createElement("button");
  testBtn.type = "button";
  testBtn.id = "mqtt-test-btn";
  testBtn.textContent = "Test connection";
  testBtn.addEventListener("click", async () => {
    testBtn.disabled = true;
    testBtn.textContent = "Testing…";
    try {
      const body = {
        host:      valueOf("MQTT_HOST"),
        port:      Number(valueOf("MQTT_PORT") || 1883),
        username:  valueOf("MQTT_USERNAME"),
        password:  valueOf("MQTT_PASSWORD"),
        tls:       !!valueOf("MQTT_TLS"),
        client_id: valueOf("MQTT_CLIENT_ID"),
      };
      const result = await api("/api/mqtt/test", {
        method: "POST",
        body: JSON.stringify(body),
      });
      alert(result.detail);
    } catch (e) {
      alert("Test failed: " + (e.message || e));
    } finally {
      testBtn.disabled = false;
      testBtn.textContent = "Test connection";
    }
  });
  pane.appendChild(testBtn);

  // Initial status fetch + start polling (cleared when pane re-renders)
  refreshMqttStatus();
  if (_mqttStatusTimer) clearInterval(_mqttStatusTimer);
  _mqttStatusTimer = setInterval(refreshMqttStatus, 5000);
}

function showRestartBanner() {
  if (document.getElementById("banner-restart")) return;
  const b = document.createElement("div");
  b.id = "banner-restart";
  b.className = "banner-restart";
  b.textContent =
    "Container restart required to apply web server changes. " +
    "Visit Settings → System → Restart.";
  document.body.appendChild(b);
}

// `defer` guarantees the doc is parsed by the time we run, but
// guard the lookups anyway in case the markup is restructured.
const settingsDiscard = document.getElementById("settings-discard");
if (settingsDiscard) {
  settingsDiscard.addEventListener("click", () => {
    settingsState.pending = {};
    renderSettingsSection(currentSettingsSection());
    updateSettingsFooter();
  });
}

const settingsSave = document.getElementById("settings-save");
if (settingsSave) {
  settingsSave.addEventListener("click", async () => {
    try {
      const body = await api("/api/settings", {
        method: "PUT",
        body: JSON.stringify(settingsState.pending),
      });
      if ((body.restart_required_keys || []).length) {
        showRestartBanner();
      }
      // Mirror display prefs back into the cached state so the
      // Archive tab's distance/byte formatters pick up the
      // change immediately, before the user navigates away.
      if (body.editable && body.editable.DISTANCE_UNITS) {
        state.distanceUnits = body.editable.DISTANCE_UNITS;
      }
      await loadSettings();
    } catch (e) {
      alert(`Save failed: ${e}`);
    }
  });
}

window.addEventListener("hashchange", () => {
  if (window.location.hash.startsWith("#/settings")) {
    // The earlier hashchange listener already swapped the tab;
    // we just refresh the active sidebar link + pane.
    renderSettingsSection(currentSettingsSection());
  }
});

// ---------- Bootstrap ----------

(async () => {
  try {
    await fetch("/api/auth/me", { credentials: "same-origin" })
      .then((r) => { if (!r.ok) throw 0; });
    const cr = await fetch("/api/auth/csrf", { credentials: "same-origin" });
    state.csrf = (await cr.json()).csrf;
    showApp();
  } catch {
    showLogin();
  }
})();

// ---- Import modal -------------------------------------------------------
(function importModal() {
  const modal = document.getElementById("import-modal");
  if (!modal) return;
  const $ = (id) => document.getElementById(id);
  const show = (el) => el && el.classList.remove("hidden");
  const hide = (el) => el && el.classList.add("hidden");
  const csrfH = () => (state.csrf ? { "x-csrf-token": state.csrf } : {});

  $("import-btn").addEventListener("click", () => {
    hide($("import-summary")); hide($("import-progress"));
    show(modal);
  });
  $("import-close").addEventListener("click", () => hide(modal));

  modal.querySelectorAll(".import-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      modal.querySelectorAll(".import-tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      modal.querySelectorAll(".tab-pane").forEach((p) => hide(p));
      show(modal.querySelector(`[data-pane="${tab.dataset.tab}"]`));
    });
  });

  const RE = /^\d{4}_\d{4}_\d{6}_\d+.+\.MP4$/i;
  const tsOf = (n) => {
    const m = n.match(/^(\d{4})_(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})/);
    return m ? Number(m.slice(1).join("")) : 0;
  };

  // --- Upload tab ---
  // Each entry is { file, path } so the folder picker (relative path via
  // webkitRelativePath) and drag-dropped folders (path from the
  // FileSystemEntry walk) share one shape: the path drives RO detection
  // server-side, the basename drives the write location.
  let picked = [];
  const dz = modal.querySelector(".dropzone");
  const dzTitle = modal.querySelector(".dropzone-title");

  function applySelection(items) {
    picked = items
      .filter((it) => RE.test(it.file.name))
      .sort((a, b) => tsOf(b.file.name) - tsOf(a.file.name)); // newest-first
    const skipped = items.length - picked.length;
    dz.classList.toggle("has-files", picked.length > 0);
    dzTitle.textContent = picked.length
      ? `${picked.length} clip${picked.length === 1 ? "" : "s"} ready`
      : "Drag a folder here, or click to browse";
    $("import-upload-manifest").textContent = items.length
      ? (picked.length
          ? `${picked.length} recognised${skipped ? `, ${skipped} skipped` : ""} — newest first.`
          : "No Viofo clips found in that folder.")
      : "";
    $("import-upload-go").disabled = picked.length === 0;
  }

  $("import-files").addEventListener("change", (e) => {
    applySelection(
      [...e.target.files].map((f) => ({ file: f, path: f.webkitRelativePath || f.name })),
    );
  });

  // Drag-and-drop a folder onto the dropzone. The browser default is to
  // navigate to the dropped item, so every handler must preventDefault.
  const readEntries = (reader) =>
    new Promise((res, rej) => reader.readEntries(res, rej));
  async function walk(entry, prefix, out) {
    const here = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (entry.isFile) {
      out.push({ file: await new Promise((res, rej) => entry.file(res, rej)), path: here });
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      let batch;
      do {
        batch = await readEntries(reader);
        for (const child of batch) await walk(child, here, out);
      } while (batch.length);
    }
  }

  const stop = (e) => { e.preventDefault(); e.stopPropagation(); };
  ["dragenter", "dragover"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { stop(e); dz.classList.add("dragging"); }));
  dz.addEventListener("dragleave", (e) => {
    if (!dz.contains(e.relatedTarget)) dz.classList.remove("dragging");
  });
  dz.addEventListener("drop", async (e) => {
    stop(e);
    dz.classList.remove("dragging");
    const dt = e.dataTransfer;
    // webkitGetAsEntry must be read synchronously, before any await.
    const entries = dt.items
      ? [...dt.items]
          .filter((i) => i.kind === "file")
          .map((i) => (i.webkitGetAsEntry ? i.webkitGetAsEntry() : null))
          .filter(Boolean)
      : [];
    const plain = entries.length ? [] : [...dt.files];
    const out = [];
    for (const entry of entries) await walk(entry, "", out);
    for (const f of plain) out.push({ file: f, path: f.name });
    applySelection(out);
  });

  // Stop a near-miss drop (onto the card or backdrop) from navigating the
  // page away while the modal is open; real drops on the zone are handled
  // above. Only active while the modal is open, so app-wide DnD is untouched.
  const guardStrayDrop = (e) => { if (!modal.classList.contains("hidden")) e.preventDefault(); };
  window.addEventListener("dragover", guardStrayDrop);
  window.addEventListener("drop", guardStrayDrop);

  $("import-upload-go").addEventListener("click", async () => {
    show($("import-progress")); hide($("import-summary"));
    const tally = {};
    for (let i = 0; i < picked.length; i++) {
      const { file, path } = picked[i];
      $("import-status").textContent = `Uploading ${file.name} (${i + 1}/${picked.length})`;
      $("import-bar").style.width = `${(i / picked.length) * 100}%`;
      let res;
      try {
        const r = await fetch("/api/import/upload", {
          method: "POST",
          credentials: "same-origin",
          headers: {
            ...csrfH(),
            "X-Import-Path": path,
            "X-Import-Size": String(file.size),
            "Content-Type": "application/octet-stream",
          },
          body: file,
        });
        if (!r.ok) {
          let detail = r.status;
          try { detail = (await r.json()).detail || r.status; } catch (_) {}
          res = { status: "error", detail: String(detail) };
        } else {
          res = await r.json();
        }
      } catch (err) {
        res = { status: "error", detail: String(err) };
      }
      const key = res.status === "error" ? "errors" : res.status;
      tally[key] = (tally[key] || 0) + 1;
    }
    $("import-bar").style.width = "100%";
    await fetch("/api/archive/rescan", { method: "POST", credentials: "same-origin", headers: csrfH() });
    renderSummary(tally);
  });

  // --- Folder tab ---
  $("import-folder-scan").addEventListener("click", async () => {
    const path = $("import-folder-path").value.trim() || null;
    const r = await fetch("/api/import/scan", {
      method: "POST",
      credentials: "same-origin",
      headers: { ...csrfH(), "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    if (!r.ok) {
      $("import-folder-manifest").textContent = `Error: ${(await r.json()).detail || r.status}`;
      hide($("import-folder-go"));
      return;
    }
    const m = await r.json();
    $("import-folder-manifest").textContent =
      `${m.recognised.length} clip(s), ${m.skipped.length} skipped, ` +
      `${(m.total_bytes / 1e9).toFixed(2)} GB${m.cross_volume ? " (external — copy)" : ""}.`;
    $("import-folder-go").dataset.path = path || "";
    show($("import-folder-go"));
  });

  $("import-folder-go").addEventListener("click", async (e) => {
    const path = e.target.dataset.path || null;
    show($("import-progress")); hide($("import-summary"));
    $("import-status").textContent = "Starting…";
    const r = await fetch("/api/import/ingest", {
      method: "POST",
      credentials: "same-origin",
      headers: { ...csrfH(), "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    if (!r.ok) {
      hide($("import-progress"));
      let detail = r.status;
      try { detail = (await r.json()).detail || r.status; } catch (_) {}
      $("import-status").textContent = `Error: ${detail}`;
      show($("import-status"));
    }
    // Progress arrives over the WebSocket (import_* events).
  });

  function renderSummary(t) {
    hide($("import-progress"));
    const el = $("import-summary");
    el.textContent =
      `Imported ${t.imported || 0}, duplicate ${t.already_present || 0}, ` +
      `skipped (over quota) ${t.over_quota_older || 0}, ` +
      `unrecognised ${t.not_recognised || 0}, errors ${t.errors || 0}.`;
    show(el);
    if (!document.getElementById("view-archive").hidden) loadDays();
  }

  // Fold folder-mode WS events into the same UI.
  window.__importOnEvent = (ev) => {
    if (ev.type === "import_started") {
      show($("import-progress")); $("import-bar").style.width = "0%";
    } else if (ev.type === "import_progress") {
      $("import-bar").style.width = `${(ev.done / Math.max(ev.total, 1)) * 100}%`;
      $("import-status").textContent = `${ev.filename} (${ev.done}/${ev.total})`;
    } else if (ev.type === "import_done") {
      renderSummary(ev);
    }
  };
})();
