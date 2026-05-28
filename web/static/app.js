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
    updateSyncState(s.running, s.paused);
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
  // Fetch fresh status — the WS event will follow shortly but
  // a direct response avoids a visible lag on the button icon.
  const s = await api("/api/sync/status");
  updateSyncState(s.running, s.paused);
});

function updateSyncState(running, paused) {
  state.syncRunning = running;
  state.syncPaused = paused;

  const btn = document.getElementById("sync-toggle");
  // Use explicit setAttribute/removeAttribute on the SVG icons:
  // some browsers don't propagate the `.hidden` IDL property
  // setter onto SVGElement reliably, which has bitten us with
  // the icon staying visible when JS said it should be hidden.
  const setVisible = (el, visible) => {
    if (visible) el.removeAttribute("hidden");
    else el.setAttribute("hidden", "");
  };
  const iconPlay = document.getElementById("sync-icon-play");
  const iconPause = document.getElementById("sync-icon-pause");
  const iconSync = document.getElementById("sync-icon-sync");

  let show, title, klass;
  if (!running) {
    show = iconPlay;
    title = "Start downloading";
    klass = null;
  } else if (paused) {
    show = iconPause;
    title = "Resume downloading";
    klass = "paused";
  } else {
    show = iconSync;
    title = "Pause downloading";
    klass = "active";
  }

  setVisible(iconPlay, show === iconPlay);
  setVisible(iconPause, show === iconPause);
  setVisible(iconSync, show === iconSync);
  btn.classList.remove("active", "paused");
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
  const settingsView = document.getElementById("view-settings");
  if (settingsView) settingsView.hidden = tab !== "settings";
  if (tab === "archive") {
    loadDays();
    refreshExportJobs();
    startArchiveAutoRefresh();
  } else {
    stopArchiveAutoRefresh();
  }
  if (tab === "downloads") loadQueue();
  if (tab === "settings") loadSettings();
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
      body.appendChild(renderJourneyCard(ev.data, ev.clips, ev.idx));
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

function renderJourneyCard(j, clips, idx) {
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
  document.getElementById("export-join-front").disabled = fronts === 0;
  document.getElementById("export-join-rear").disabled = rears === 0;
  document.getElementById("export-pip").disabled = both === 0;
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

async function submitExport(type) {
  const ids = [];
  for (const v of state.archiveSelected.values()) {
    if (type === "join_front" && v.front) ids.push(v.front);
    else if (type === "join_rear" && v.rear) ids.push(v.rear);
    else if (type === "pip") {
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

document.getElementById("export-join-front")
  .addEventListener("click", () => submitExport("join_front"));
document.getElementById("export-join-rear")
  .addEventListener("click", () => submitExport("join_rear"));
document.getElementById("export-pip")
  .addEventListener("click", () => submitExport("pip"));
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
      <th>ID</th><th>Type</th><th>State</th><th>Progress</th>
      <th>Created</th><th></th>
    </tr></thead>
    <tbody></tbody>
  `;
  const tbody = table.querySelector("tbody");
  const live = state.exportProgress || {};
  for (const j of jobs) {
    const tr = document.createElement("tr");
    const created = j.created_at
      ? new Date(j.created_at * 1000).toLocaleString() : "—";
    // Running jobs: prefer the live progress stream if we have
    // one; the DB row is only updated at finish.
    const liveHit = live[j.id];
    const progVal = liveHit && liveHit.progress != null
      ? liveHit.progress
      : j.progress;
    const terminal = ["done", "failed", "cancelled"].includes(j.state);
    let pct;
    if (j.state === "done") pct = "100%";
    else if (terminal) pct = "—";
    else pct = progVal != null ? Math.round(progVal * 100) + "%" : "—";
    const stage = liveHit && liveHit.stage && !terminal
      ? ` · ${liveHit.stage}` : "";
    const actions = [];
    if (j.state === "done") {
      actions.push(
        `<a href="/api/exports/${j.id}/download" download>Download</a>`,
      );
    }
    actions.push(`<button type="button" class="export-delete" data-id="${j.id}">Delete</button>`);
    tr.innerHTML = `
      <td>${j.id}</td>
      <td>${j.type}</td>
      <td class="state-${j.state}">${j.state}${j.error ? " · " + j.error : ""}</td>
      <td>${pct}${stage}</td>
      <td>${created}</td>
      <td>${actions.join(" · ")}</td>
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
    body.appendChild(renderDayItemsTable(d.day, state.queueDayItems[d.day]));
  }

  return el;
}

function renderDayItemsTable(day, items) {
  const wrap = document.createElement("div");
  if (!items.length) {
    wrap.innerHTML = `<p style="color:var(--muted);padding:8px">
      No files match this filter.
    </p>`;
    return wrap;
  }
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
    const size = it.remote_size
      ? fmtMB(it.remote_size) : "—";
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
    // Update just this day's header checkbox without a full re-render,
    // so the user's scroll position isn't lost.
    updateDayHeaderCheckbox(day);
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
  for (const d of state.queueDays) {
    total += d.clip_count;
    pending += d.pending_count;
  }
  const sel = state.queueSelected.size;
  let text = `${total} files across ${state.queueDays.length} days · ${pending} pending`;
  if (sel) text += ` · ${sel} selected`;
  document.getElementById("queue-meta").textContent = text;
}

function isDownloadsTabActive() {
  return !document.getElementById("view-downloads").hidden;
}

function refreshQueueIfVisible() {
  if (isDownloadsTabActive()) {
    loadQueue();
  }
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

const MAX_LOG_ENTRIES = 200;

function appendLog(ev) {
  const container = document.getElementById("log-entries");
  if (!container) return;
  // Per-chunk progress is already shown live in the progress bar
  // above (downloads) or the Export jobs table (exports); mirroring
  // it in the log buries everything else.
  if (ev.type === "item_progress" || ev.type === "export_progress") return;
  const line = document.createElement("div");
  line.className = "log-line";
  const ts = new Date().toLocaleTimeString();
  let detail = ev.type;
  if (ev.type === "item_started") detail = `item_started: ${ev.filename}`;
  else if (ev.type === "item_finished") detail = `item_finished: ${ev.filename} ${ev.ok ? "ok" : ev.error || "failed"}`;
  else if (ev.type === "sync_state") detail = `sync_state: running=${ev.running} paused=${ev.paused}`;
  else if (ev.type === "queue_reconciled") detail = `queue_reconciled: +${ev.added || 0} added, ${ev.marked_gone || 0} gone`;
  else if (ev.type === "dashcam_delete") {
    if (ev.ok) detail = `dashcam_delete: ${ev.filename} ok`;
    else if (ev.reason === "size_mismatch"
             && ev.local_size != null && ev.remote_size != null) {
      const delta = ev.local_size - ev.remote_size;
      const sign = delta >= 0 ? "+" : "";
      detail = `dashcam_delete: ${ev.filename} skipped `
        + `(size_mismatch: local=${ev.local_size} dashcam=${ev.remote_size}, ${sign}${delta})`;
    }
    else detail = `dashcam_delete: ${ev.filename} skipped (${ev.reason || "unknown"})`;
  }
  else if (ev.type === "retention_deleted") {
    detail = `retention_deleted: ${ev.filename} (${ev.reason})`;
  }
  else if (ev.type === "snapshot") detail = "snapshot (initial state)";
  line.innerHTML = `<span class="log-ts">${ts}</span> <span class="log-msg">${detail}</span>`;
  container.appendChild(line);
  while (container.children.length > MAX_LOG_ENTRIES) {
    container.removeChild(container.firstChild);
  }
  container.scrollTop = container.scrollHeight;
}

function handleEvent(ev) {
  appendLog(ev);
  const statusEl = document.getElementById("dashcam-status");
  switch (ev.type) {
    case "snapshot":
      if (ev.state.dashcam_online === true) {
        statusEl.textContent = "Dashcam online";
        statusEl.className = "status online";
      } else if (ev.state.dashcam_online === false) {
        statusEl.textContent = "Dashcam offline";
        statusEl.className = "status offline";
      }
      if (ev.state.current_item) updateCurrent(ev.state.current_item);
      if (ev.state.sync_state) {
        updateSyncState(ev.state.sync_state.running, ev.state.sync_state.paused);
      }
      break;
    case "sync_state":
      updateSyncState(ev.running, ev.paused);
      break;
    case "dashcam_online":
      statusEl.textContent = "Dashcam online";
      statusEl.className = "status online";
      break;
    case "dashcam_offline":
      statusEl.textContent = "Dashcam offline — retrying…";
      statusEl.className = "status offline";
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
  };
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
    <div class="form-row"><label>New password (min 8)</label><input type="password" id="pw-new" autocomplete="new-password" /></div>
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
