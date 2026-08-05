// Viofosync SPA — vanilla JS, no bundler.
// Two tabs (archive, downloads) hash-routed, one modal, one WebSocket.
//
// All mutating calls send X-CSRF-Token. The token is returned by
// /api/auth/login and refreshed via /api/auth/csrf on 403.

// Camera registry — mirror of viofosync_lib/cameras.py; keep the two
// in sync. Order = display/cycle order. ``letter`` is the filename
// suffix (and data-camera attribute), ``channel`` the API key (pair
// slots, export type suffixes, element id suffixes), ``label`` the
// human string. front+rear always exist; a 3-channel model adds one
// of the others.
const CAMERAS = [
  { letter: "F", channel: "front",    label: "Front" },
  { letter: "R", channel: "rear",     label: "Rear" },
  { letter: "T", channel: "tele",     label: "Tele" },
  { letter: "I", channel: "interior", label: "Interior" },
];
const CAMERA_LABELS = Object.fromEntries(
  CAMERAS.map((c) => [c.letter, c.label]),
);
// The third-camera slots whose UI (thumbs, action groups) only
// appears when the data contains them.
const EXTRA_CAMERAS = CAMERAS.filter(
  (c) => c.channel !== "front" && c.channel !== "rear",
);

const state = {
  csrf: null,
  modalClip: null,         // { id, camera, dayEl, timelines }
  camera: null,            // cached { info, cat } from the last good fetch
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
  filters: { driving: true, parking: true, ro: true, location: "",
             geofenced: localStorage.getItem("vfs.showGeofenced") === "1" },
  showMaps: localStorage.getItem("vfs.showMaps") !== "0",
  archiveSelected: new Map(),  // pair_id → { ts, front, rear, tele, interior }
  archiveExpanded: new Set(),  // open archive day keys ("YYYY-MM-DD"); persists
                               // open days across in-app navigation (re-render)
  archiveStale: false,         // a change event arrived while the archive was
                               // hidden; reload the day list on next tab entry
  journeyExpanded: new Set(),  // open journey/stop card keys ("date|kind|ts");
                               // restores expansion across day-body re-renders
  journeyMapViews: new Map(),  // card key → {center, zoom}; restores a map's
                               // view across re-renders instead of re-running
                               // the animated fitBounds (a visible zoom jump)
  map: null,
  routeLayer: null,
  ws: null,
  syncRunning: false,
  syncPaused: false,
  triage: { active: false },   // GPS triage progress: {active, triaged, total, eta_s}
  currentFilename: null,
  // Mirrored from /api/settings on login + on Save so display
  // helpers (fmtDistance) don't need to read from settingsState
  // (which is only loaded when the Settings tab is visited).
  distanceUnits: "km",
  locations: [],           // named locations mirrored from /api/settings, for the
                           // archive location filter dropdown
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

// Escape a string for interpolation into innerHTML templates —
// element body OR attribute context (quotes included). Clip
// filenames are external data: the Viofo regex allows any
// characters in the camera segment, and geocode labels come from
// Nominatim. Anything not produced by this file must pass through
// here before reaching an HTML template.
function escHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------- API helpers ----------

async function api(path, opts = {}, _retriedCsrf = false) {
  const headers = { "content-type": "application/json", ...(opts.headers || {}) };
  if (state.csrf && opts.method && opts.method !== "GET") {
    headers["x-csrf-token"] = state.csrf;
  }
  const r = await fetch(path, { ...opts, headers, credentials: "same-origin" });
  if (r.status === 401) {
    showLogin();
    throw new Error("unauthorised");
  }
  if (r.status === 403 && state.csrf && !_retriedCsrf) {
    // Refresh CSRF once and retry; a second 403 falls through to the
    // throw below. Without the guard a persistently-403ing POST (proxy
    // caching the csrf GET, session reissued between calls) recurses
    // forever with no error surfaced — the button just looks dead.
    const cr = await fetch("/api/auth/csrf", { credentials: "same-origin" });
    if (cr.ok) {
      state.csrf = (await cr.json()).csrf;
      return api(path, opts, true);
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
  api("/api/auth/me")
    .then((j) => {
      const el = document.getElementById("settings-version");
      if (el && j.version) el.textContent = j.version;
    })
    .catch(() => {});
  routeTo(location.hash || "#/archive");
  openSocket();
  try {
    const s = await api("/api/sync/status");
    state.syncRunning = s.running;
    state.syncPaused = s.paused;
    // The WS snapshot will deliver the server-computed sync_status
    // shortly; no direct updateSyncState call needed here.
  } catch {}
}

async function refreshDisplayPrefs() {
  try {
    const body = await api("/api/settings");
    state.distanceUnits = body.editable.DISTANCE_UNITS || "km";
    state.locations = body.editable.LOCATIONS || [];
    buildLocationFilter();
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

// Format a seconds-remaining estimate for the triage badge.
function fmtTriageEta(s) {
  if (s == null || !isFinite(s) || s < 0) return "";
  if (s < 60) return `~${Math.round(s)}s`;
  const m = Math.round(s / 60);
  if (m < 60) return `~${m} min`;
  const h = Math.floor(m / 60);
  return `~${h}h ${m % 60}m`;
}

// Build the "Triaging GPS 40/876 · ~12 min left" badge text from state.triage.
function triageBadgeText() {
  const t = state.triage || {};
  let s = "Triaging GPS";
  if (t.total) s += ` ${t.triaged || 0}/${t.total}`;
  const eta = fmtTriageEta(t.eta_s);
  if (eta) s += ` · ${eta} left`;
  return s;
}

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
  } else if (status === "triaging") {
    show = iconSync; title = "Pause"; klass = "triaging";
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
  btn.classList.remove("active", "paused", "waiting", "error", "triaging");
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
  const cameraView = document.getElementById("view-camera");
  if (cameraView) cameraView.hidden = tab !== "camera";
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
    // Rebuild the day list only when something actually changed while the
    // view was hidden (archiveStale, set by the WS handlers below) or it was
    // never rendered. Skipping the reload preserves open days, expanded
    // journey cards and their live Leaflet maps — switching to the timeline
    // and back should feel instant, not re-fetch the world.
    const days = document.getElementById("days");
    if (state.archiveStale || !days.children.length) loadDays();
    refreshExportJobs();
  }
  if (tab === "downloads") loadQueue();
  if (tab === "camera") loadCamera();
  if (tab === "logs") loadLogs();
  if (tab === "settings") loadSettings();
  if (tab === "timeline") {
    // "#/timeline/<date>/<journeyIdx?>" — segments after the tab.
    const segs = stripped.split("/");
    const date = segs[1] || "";
    const n = segs[2] != null && segs[2] !== "" ? Number(segs[2]) : null;
    const journey = Number.isInteger(n) && n >= 0 ? n : null;
    if (window.Timeline && date) window.Timeline.open(date, journey);
  }
}

// Refresh the day list when the server re-indexes the archive. The
// backend already broadcasts a `clip_indexed` WS event on every scan
// (sync-worker download, startup scan, manual rescan, import), so we
// reload on that push instead of polling. Guarded so we don't disrupt
// the user: skip while the archive view is hidden, and skip while a day
// is expanded — re-rendering would collapse the open card, reset its
// map, and drop any unsubmitted selections.
//
// This replaces an earlier 30s client-side poll that issued a full
// `/api/archive/rescan` (walking the whole recordings tree) from every
// open tab — so N open archive clients meant N full rescans per tick.
// The work is the server's to do once; the browser just reacts to it.
// Trailing debounce: the derive worker emits `clip_derived` per clip, so a
// boot backfill of a large archive would otherwise fire one reload per clip
// across every open tab. Coalesce a burst into a single reload shortly after
// it settles. Guards are evaluated at fire time (state may have changed).
let _archiveRefreshTimer = null;
function refreshArchiveOnIndexChange() {
  if (_archiveRefreshTimer) clearTimeout(_archiveRefreshTimer);
  _archiveRefreshTimer = setTimeout(() => {
    _archiveRefreshTimer = null;
    if (document.getElementById("view-archive").hidden) {
      // Don't drop the event: remember the list is out of date so the
      // next visit to the tab reloads it (routeTo checks this flag).
      state.archiveStale = true;
      return;
    }
    if (document.querySelector("#days .day .day-body:not([hidden])")) return;
    loadDays();
  }, 400);
}

// ---- FLIP animation for day-body re-renders --------------------------
// A day-body refresh (skip, download-next, WS queue_changed) rebuilds its
// DOM, which reads as an abrupt snap: tiles vanish and neighbours teleport.
// FLIP smooths it: capture each keyed element's position before the swap,
// then animate survivors from their old position to the new one; elements
// that only exist in the new DOM fade in. Keys live in data-flip-key
// (clip pairs, journey/stop/ungrouped cards). Positions are stored relative
// to the day body so a page-scroll shift between capture and play can't
// skew the deltas. Disabled under prefers-reduced-motion.
const _REDUCED_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)");
const FLIP_MS = 220;

function flipCapture(body) {
  if (_REDUCED_MOTION.matches) return null;
  const ref = body.getBoundingClientRect();
  const rects = new Map();
  for (const el of body.querySelectorAll("[data-flip-key]")) {
    const r = el.getBoundingClientRect();
    rects.set(el.dataset.flipKey,
      { x: r.left - ref.left, y: r.top - ref.top });
  }
  return rects.size ? rects : null;   // null = first render, nothing to play
}

function flipPlay(body, prev) {
  if (!prev) return;
  const ref = body.getBoundingClientRect();
  for (const el of body.querySelectorAll("[data-flip-key]")) {
    const r = el.getBoundingClientRect();
    const was = prev.get(el.dataset.flipKey);
    if (!was) {
      // Newcomer (e.g. a clip that just finished downloading).
      el.animate(
        [{ opacity: 0, transform: "scale(0.97)" },
         { opacity: 1, transform: "none" }],
        { duration: FLIP_MS, easing: "ease-out" });
      continue;
    }
    const dx = was.x - (r.left - ref.left);
    const dy = was.y - (r.top - ref.top);
    if (dx || dy) {
      el.animate(
        [{ transform: `translate(${dx}px, ${dy}px)` },
         { transform: "none" }],
        { duration: FLIP_MS, easing: "ease-in-out" });
    }
  }
}

// Fade the selected clip tiles out before an action that removes them from
// the archive (skip, delete), so the removal reads as intentional rather
// than a glitch. Resolves when the fade completes; the follow-up re-render
// then FLIPs the surviving neighbours into the freed space.
function animateOutSelectedPairs() {
  if (_REDUCED_MOTION.matches) return Promise.resolve();
  const anims = [];
  for (const pairId of state.archiveSelected.keys()) {
    document
      .querySelectorAll(`.clip-pair[data-pair-id="${CSS.escape(pairId)}"]`)
      .forEach((el) => {
        anims.push(
          el.animate(
            [{ opacity: 1, transform: "none" },
             { opacity: 0, transform: "scale(0.95)" }],
            { duration: 160, easing: "ease-in", fill: "forwards" }
          ).finished.catch(() => {})
        );
      });
  }
  return Promise.all(anims);
}

// Re-render any currently-open archive day so per-clip status icons reflect
// the latest queue state. Unlike refreshArchiveOnIndexChange (which skips open
// days to avoid disrupting browsing), this targets the open day directly —
// used after a Download Action and on queue_changed.
let _openDayRefreshTimer = null;
function refreshOpenArchiveDays() {
  document.querySelectorAll("#days .day").forEach((dayEl) => {
    const body = dayEl.querySelector(".day-body");
    if (body && !body.hidden) renderDayBody(body, dayEl.dataset.day);
  });
}
const OPEN_DAY_REFRESH_MS = 300;
const OPEN_DAY_REFRESH_SYNC_MS = 2000;
function scheduleOpenArchiveRefresh() {
  // Coalescing throttle, not a resetting debounce: queue_changed fires on
  // every item transition during a download session, and re-arming the
  // timer each time both starved the refresh under a steady stream and,
  // once idle, re-rendered every open day per transition. While a sync is
  // active, widen the window — one refresh per ~2 s is plenty for status
  // icons, and renderDayBody skips unchanged days anyway.
  if (_openDayRefreshTimer) return;
  const delay = state.syncRunning ? OPEN_DAY_REFRESH_SYNC_MS : OPEN_DAY_REFRESH_MS;
  _openDayRefreshTimer = setTimeout(() => {
    _openDayRefreshTimer = null;
    if (document.getElementById("view-archive").hidden) {
      state.archiveStale = true;   // reload on next tab entry instead
      return;
    }
    refreshOpenArchiveDays();
  }, delay);
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

// "GPS-excluded": reveal clips auto-skipped by the home geofence and
// weave their recovered skeleton tracks into the day's journeys.
// Persisted — it's a mode, like the maps toggle, not a per-visit filter.
(() => {
  const cb = document.getElementById("f-geofenced");
  cb.checked = state.filters.geofenced;
  cb.addEventListener("change", (e) => {
    state.filters.geofenced = e.target.checked;
    localStorage.setItem("vfs.showGeofenced", e.target.checked ? "1" : "0");
    state.page = 1;
    loadDays();
  });
})();

// Location filter: client-side, narrows the open day timeline to journeys
// (start or end) and stops at the selected named location. Populated from
// state.locations (mirrored from /api/settings); hidden when none are defined.
function buildLocationFilter() {
  const wrap = document.getElementById("f-location-wrap");
  const sel = document.getElementById("f-location");
  if (!wrap || !sel) return;
  const locs = state.locations || [];
  if (!locs.length) {
    wrap.hidden = true;
    state.filters.location = "";
    sel.value = "";
    return;
  }
  // Home first, then alphabetical; name doubles as the option value (filtering
  // matches by place name).
  const ordered = [...locs].sort((a, b) =>
    (b.is_home ? 1 : 0) - (a.is_home ? 1 : 0) ||
    (a.name || "").localeCompare(b.name || ""));
  const prev = state.filters.location;
  const names = ordered.map((l) => l.name);
  sel.innerHTML =
    '<option value="">All locations</option>' +
    ordered.map((l) =>
      `<option value="${escHtml(l.name)}">${escHtml(l.name)}</option>`).join("");
  if (prev && names.includes(prev)) {
    sel.value = prev;
  } else {
    sel.value = "";
    state.filters.location = "";
  }
  wrap.hidden = false;
}

document.getElementById("f-location").addEventListener("change", (e) => {
  state.filters.location = e.target.value;
  refreshOpenArchiveDays();
});

// "GPS Journey Splits" is a view option, not a filter: it gates the
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


function archiveKindParams(q) {
  q.set("driving", state.filters.driving ? "true" : "false");
  q.set("parking", state.filters.parking ? "true" : "false");
  q.set("ro", state.filters.ro ? "true" : "false");
  if (state.filters.geofenced) q.set("show_geofenced", "true");
}

// Tear down Leaflet instances under `root` before its innerHTML is
// wiped — Leaflet registers document-level listeners per map, so
// dropping the DOM nodes without map.remove() leaks every instance
// (the timeline view already does this; the archive didn't).
function destroyJourneyMaps(root) {
  for (const div of root.querySelectorAll(".journey-map")) {
    const bundle = div._journeyMap;
    if (bundle && bundle.map) {
      try { bundle.map.remove(); } catch {}
    }
    div._journeyMap = null;
  }
}

function destroyLocationMaps(root) {
  for (const div of root.querySelectorAll(".loc-map")) {
    const m = div._locMap;
    if (m) { try { m.remove(); } catch {} }
    div._locMap = null;
  }
}

async function loadDays() {
  // Stale-response guard (same token pattern as loadQueue): WS
  // pushes, filter changes, and pagination can race; only the most
  // recently requested page may render.
  const reqId = (state.daysRequestId = (state.daysRequestId || 0) + 1);
  const q = new URLSearchParams({
    page: state.page, per_page: state.perPage,
    sort: "desc",
  });
  archiveKindParams(q);

  const data = await api("/api/archive/days?" + q);
  if (reqId !== state.daysRequestId) return; // superseded
  state.archiveStale = false;                // rendering fresh data
  const container = document.getElementById("days");
  destroyJourneyMaps(container);
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
  // Open days persist in state.archiveExpanded so they survive a re-render
  // (e.g. navigating to the timeline and back, which rebuilds #days).
  const open = state.archiveExpanded.has(d.day);
  el.innerHTML = `
    <div class="day-header">
      <input type="checkbox" class="day-check"
             title="Select all clips in this day" />
      <h3>${d.day}</h3>
      <div class="meta">
        ${d.clip_count} clips${
          [
            d.driving_count ? `${d.driving_count} driving` : null,
            d.parking_count ? `${d.parking_count} parking` : null,
            d.ro_count ? `${d.ro_count} read-only` : null,
          ].filter(Boolean).map((s) => ` · ${s}`).join("")
        } · ${fmtBytes(d.total_bytes)}${d.gpx_count ? " · GPS" : ""}${
          d.remote_count ? ` · ${d.remote_count} on camera` : ""
        }${
          d.geofence_skipped_count
            ? ` · ${d.geofence_skipped_count} GPS-excluded`
            : ""
        }
      </div>
    </div>
    <div class="day-body" ${open ? "" : "hidden"}></div>
  `;
  const body = el.querySelector(".day-body");
  el.querySelector(".day-header").addEventListener("click", async () => {
    if (!body.hidden) {
      body.hidden = true;
      state.archiveExpanded.delete(d.day);
      return;
    }
    body.hidden = false;
    state.archiveExpanded.add(d.day);
    await loadDayBody(el);
  });
  wireDayCheck(el);
  // Restore a remembered-open day: populate its body immediately. Async; the
  // card is returned synchronously and fills in when the fetch resolves.
  if (open) {
    loadDayBody(el);
  }
  return el;
}

// Load (or reload) a day's body, then sync its select-all checkbox. Centralised
// so the day-header toggle, a remembered-open day, and the day-level select-all
// all populate the body the same way and mark it loaded for refreshDayCheck.
async function loadDayBody(dayEl) {
  const body = dayEl.querySelector(".day-body");
  // Only show the placeholder on a body that has never rendered; a refresh
  // of an already-populated day keeps the old content on screen until the
  // new payload arrives (renderDayBody swaps it in one go).
  if (!body.dataset.loaded) body.innerHTML = "<p>Loading…</p>";
  await renderDayBody(body, dayEl.dataset.day);
  body.dataset.loaded = "1";
  refreshDayCheck(dayEl);
}

// A pair carries GPS if it's a remote tile (already gated to gps_points>0) or
// any downloaded camera slot has a sidecar. Used to decide whether a clip that
// snaps to no journey/stop is worth an Ungrouped card or should be dropped.
function pairHasGps(pair) {
  if (pair.remote) return true;
  return CAMERAS.some((c) => pair[c.channel] && pair[c.channel].has_gpx);
}

// True if a timeline event involves the named location: a journey whose start
// OR end is that place, or a stop at that place. Matches on the server's
// `named` flag + the place name, so a coincidental geocoded label can't match.
function eventMatchesLocation(ev, name) {
  if (ev.kind === "journey") {
    const j = ev.data;
    return (j.start_named && j.start_label === name) ||
           (j.end_named && j.end_label === name);
  }
  const s = ev.data;
  return !!(s.named && s.label === name);
}

// Placeholder shown in an expanded day that has nothing to/from the filtered
// location (uses textContent — `name` is user-supplied).
function renderNoLocationActivity(name) {
  const note = document.createElement("p");
  note.style.cssText = "color:var(--muted);font-size:12px";
  note.textContent = `No activity to/from ${name} on this day.`;
  return note;
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
        api(`/api/archive/day/${date}/route`
            + (state.filters.geofenced ? "?show_geofenced=true" : ""))
          .catch((e) => { console.warn("route failed", e); return null; }),
      );
    } else {
      promises.push(Promise.resolve(null));
    }
    [data, route] = await Promise.all(promises);
  } catch (e) {
    destroyJourneyMaps(body);
    body.innerHTML = `<p style="color:var(--err)">Failed to load: ${e}</p>`;
    body._renderKey = null;   // error is on screen — force a real rebuild next time
    return;
  }

  // Skip-unchanged diff: key the render on the payload plus everything else
  // that shapes it. A sync-time refresh hits every open day, but usually only
  // the day being downloaded into actually changed — the rest bail here,
  // keeping their DOM (and any open Leaflet maps) untouched instead of a
  // wipe-and-rebuild of ~hundreds of tiles per queue_changed.
  const renderKey = JSON.stringify(
    { q: q.toString(), maps: state.showMaps, loc: state.filters.location, data, route });
  if (body._renderKey === renderKey) return;
  body._renderKey = renderKey;

  // FLIP: snapshot keyed positions before the swap, animate after (below).
  const flipPrev = flipCapture(body);

  destroyJourneyMaps(body);
  body.innerHTML = "";

  const journeys = (route && route.journeys) || [];
  const stops = (route && route.stops) || [];
  const hasGps = journeys.length > 0 || stops.length > 0;
  const locFilter = state.filters.location;
  if (!hasGps) {
    // With a location filter active, a day with no journeys/stops has nothing
    // to/from that place — show the note rather than an unfiltered flat grid.
    if (locFilter) {
      body.appendChild(renderNoLocationActivity(locFilter));
      return;
    }
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
    flipPlay(body, flipPrev);
    return;
  }

  // Build a timeline of all events (journeys + stops) ordered
  // by start time. Each event has its own clip bucket. Clips
  // whose timestamp falls inside an event's [start, end] land
  // there directly; the rest snap to whichever event is
  // closest in time, so everything is anchored to *some*
  // visible context instead of a flat "other" pile.
  // Journeys group with a PADDED window (group_start_ts/group_end_ts from the
  // server — the same _expand_journey_window the timeline uses) so the pull-away
  // and pull-in clips that sit just outside the raw GPS journey (the stop
  // boundary lands ~50 m inside the real drive) land on the journey card. Fall
  // back to the raw window for older payloads.
  const journeyEvents = journeys.map((j, idx) => ({
    kind: "journey", data: j, clips: [], idx,
    start: j.group_start_ts ?? j.start_ts,
    end: j.group_end_ts ?? j.end_ts,
  }));
  const stopEvents = stops.map((s, idx) => ({
    kind: "stop", data: s, clips: [], idx,
    start: s.start_ts, end: s.end_ts,
  }));
  // Newest first — most recent activity at the top of the day. Render order and
  // the snap fallback below scan this combined list.
  const events = [...journeyEvents, ...stopEvents].sort((a, b) => b.start - a.start);

  // A loose clip (no event contains its timestamp) snaps to the nearest event
  // only if it's within this gap; otherwise it collects in an "Ungrouped" card
  // so a not-yet-skipped home clip can't drag across the whole day onto a journey.
  const SNAP_MAX_GAP_S = 1800; // 30 min — matches the GPS session-gap splitter
  const ungrouped = [];

  for (const pair of data.clips) {
    const ts = pair.timestamp;
    // Journeys win over an overlapping stop at the boundary: a pull-in clip that
    // falls inside both the padded journey and the following stop belongs to the
    // drive, not the dwell.
    let target =
      journeyEvents.find((e) => ts >= e.start && ts <= e.end) ||
      stopEvents.find((e) => ts >= e.start && ts <= e.end);
    if (!target) {
      let best = null, bestGap = Infinity;
      for (const e of events) {
        const gap = ts < e.start ? e.start - ts : ts - e.end;
        if (gap < bestGap) { bestGap = gap; best = e; }
      }
      target = bestGap <= SNAP_MAX_GAP_S ? best : null;
    }
    if (target) target.clips.push(pair);
    else if (pairHasGps(pair)) ungrouped.push(pair);
    // A no-GPS pair with nothing within snap range has no place in the
    // journey-organised view (its real context — e.g. a home dwell built from
    // skipped clips — is invisible here), so drop it rather than float it in an
    // Ungrouped card. Extends the trace-gate principle to downloaded clips. A
    // no-GPS clip that DID snap to a journey/stop is kept (handled above).
  }

  const shown = locFilter
    ? events.filter((ev) => eventMatchesLocation(ev, locFilter))
    : events;

  for (const ev of shown) {
    if (ev.kind === "journey") {
      body.appendChild(renderJourneyCard(
        ev.data, ev.clips, ev.idx, date,
        (route && route.geofenced_tracks) || [],
      ));
    } else {
      body.appendChild(renderStopCard(ev.data, ev.clips, ev.idx, date));
    }
  }

  if (locFilter) {
    // The Ungrouped pile isn't tied to a place, so it's hidden while filtering.
    if (!shown.length) body.appendChild(renderNoLocationActivity(locFilter));
  } else if (ungrouped.length) {
    body.appendChild(renderUngroupedCard(ungrouped, date));
  }

  wireClipPairMapClicks(body);
  flipPlay(body, flipPrev);
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

// Clock-style H:MM:SS / M:SS — for short media lengths (exports) where
// fmtDuration's minute rounding ("0 min" for a 40s clip) is too coarse.
function fmtClock(seconds) {
  if (seconds == null || !isFinite(seconds) || seconds < 0) return "—";
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const ss = String(s).padStart(2, "0");
  if (h) return `${h}:${String(m).padStart(2, "0")}:${ss}`;
  return `${m}:${ss}`;
}

// ETA wants sub-minute precision; fmtDuration rounds those to "0 min".
function fmtEta(seconds) {
  if (seconds == null) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  return fmtDuration(seconds);
}

function renderStopCard(stop, clips, idx, date) {
  const startT = new Date(stop.start_time).toLocaleTimeString();
  const endT = new Date(stop.end_time).toLocaleTimeString();
  const hasClips = clips && clips.length > 0;
  const fallback = `${stop.lat.toFixed(3)}, ${stop.lon.toFixed(3)}`;
  const placeLabel = stop.label || fallback;

  // Empty stop → compact banner line.
  if (!hasClips) {
    const el = document.createElement("div");
    el.className = "stop-banner";
    el.dataset.flipKey = `${date}|sb|${stop.start_ts}`;
    el.innerHTML = `
      <span class="stop-icon" aria-hidden="true">⏸</span>
      <span>Stopped for <strong>${fmtDuration(stop.duration_s)}</strong>
        at <span class="stop-label">${placeGlyph(stop.home, stop.named)}${escHtml(placeLabel)}</span></span>
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
      <strong class="journey-title">
        <span class="stop-label">${placeGlyph(stop.home, stop.named)}${escHtml(placeLabel)}</span>
      </strong>
      <span class="journey-meta">
        ${fmtDuration(stop.duration_s)} ·
        ${clips.length} clip${clips.length === 1 ? "" : "s"}
      </span>
      <span class="stop-icon" aria-hidden="true">⏸</span>
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

  const cardKey = `${date}|s|${stop.start_ts}`;
  el.dataset.flipKey = cardKey;
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
    // Restore the pre-re-render view (see the journey card's initMap for
    // why the save listener is attached before the initial view is set).
    map.on("moveend zoomend", () => {
      state.journeyMapViews.set(cardKey,
        { center: map.getCenter(), zoom: map.getZoom() });
    });
    const saved = state.journeyMapViews.get(cardKey);
    if (saved) map.setView(saved.center, saved.zoom, { animate: false });
    else map.setView(center, 16, { animate: false });
    mapDiv._journeyMap = { map };
  };

  wireJourneyToggle(el, initMap, cardKey);
  wireJourneyCheck(el);
  return el;
}

function renderUngroupedCard(clips, date) {
  const el = document.createElement("div");
  el.className = "journey-card stop-card collapsible";
  el.dataset.flipKey = `${date}|u`;
  el.innerHTML = `
    <div class="journey-header">
      <span class="caret">▸</span>
      <input type="checkbox" class="journey-check"
             title="Select all clips in this group" />
      <strong class="journey-title">Ungrouped</strong>
      <span class="journey-meta">
        ${clips.length} clip${clips.length === 1 ? "" : "s"}
      </span>
    </div>
    <div class="journey-body" hidden>
      <div class="clip-grid"></div>
    </div>
  `;
  const grid = el.querySelector(".clip-grid");
  for (const pair of clips) grid.appendChild(renderClipPair(pair));
  wireJourneyToggle(el, () => {}, `${date}|u`);
  wireJourneyCheck(el);
  return el;
}

function renderCompletionPie(j) {
  if (j.completion == null) return "";
  const p = Math.round(j.completion * 100);
  const done = p >= 100;
  const d = j.completion_detail || {};
  const dlMin = Math.round((d.downloaded_s || 0) / 60);
  const totMin = Math.round((d.total_s || 0) / 60);
  const tip = done
    ? `Journey complete — ${totMin} min · ${d.total_clips || 0} clips downloaded`
    : `${dlMin} of ${totMin} min · ${d.downloaded_clips || 0} of ${d.total_clips || 0} clips downloaded`;
  return `<span class="journey-pie${done ? " is-complete" : ""}" role="img" style="--pp:${p}" title="${escHtml(tip)}" aria-label="${escHtml(tip)}"></span>`;
}

function renderJourneyCard(j, clips, idx, date, extraTracks) {
  const el = document.createElement("div");
  el.className = "journey-card collapsible drive";
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
        <span class="start-label" data-lat="${j.start_lat}" data-lon="${j.start_lon}">${placeGlyph(j.start_home, j.start_named)}${escHtml(startLabel)}</span>
        <span class="journey-arrow">→</span>
        <span class="end-label" data-lat="${j.end_lat}" data-lon="${j.end_lon}">${placeGlyph(j.end_home, j.end_named)}${escHtml(endLabel)}</span>
      </strong>
      <span class="journey-meta">
        ${renderCompletionPie(j)}${fmtDuration(j.duration_s)} · ${distance} · ${clips.length} clip${clips.length === 1 ? "" : "s"}
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
  const cardKey = `${date}|j|${j.start_ts}`;
  el.dataset.flipKey = cardKey;
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
    // Recovered (geofence-skipped) stretches: dashed and muted so they read
    // apart from downloaded track. extraTracks is the WHOLE day's list, so
    // each is clipped to THIS journey's padded window (same window that
    // buckets clips to the card) via its per-point times — otherwise every
    // journey map would draw every day's recovered stretch.
    const gStart = j.group_start_ts ?? j.start_ts;
    const gEnd = j.group_end_ts ?? j.end_ts;
    for (const t of extraTracks || []) {
      const tt = t.times || [];
      const g = t.geojson.geometry.coordinates
        .map(([lon, lat], i) => [lat, lon, tt[i]])
        .filter(([, , ts]) => ts == null || (ts >= gStart && ts <= gEnd))
        .map(([lat, lon]) => [lat, lon]);
      if (g.length < 2) continue;
      L.polyline(g, {
        color: cssVar("--muted"), weight: 3,
        dashArray: "6 6", opacity: 0.85,
      }).addTo(map);
    }
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

    // Restore the view a re-render tore down (day-body refresh after a
    // queue action) — an animated re-fit reads as an unprompted zoom jump
    // and loses the user's pan/zoom. Fresh cards fit the trace as before.
    // The save listener is attached BEFORE the initial view is set:
    // fitBounds fires moveend synchronously, and capturing that first fit
    // is what lets an untouched map survive a re-render (otherwise the
    // fallback re-fit runs against a not-yet-laid-out container and lands
    // on a garbage zoom).
    map.on("moveend zoomend", () => {
      state.journeyMapViews.set(cardKey,
        { center: map.getCenter(), zoom: map.getZoom() });
    });
    const saved = state.journeyMapViews.get(cardKey);
    if (saved) {
      map.setView(saved.center, saved.zoom, { animate: false });
    } else {
      map.fitBounds(line.getBounds(), { padding: [20, 20], animate: false });
    }

    mapDiv._journeyMap = {
      map, coords, times, showPin,
      scrollIntoView: () =>
        mapDiv.scrollIntoView({ behavior: "smooth", block: "nearest" }),
    };
  };

  wireJourneyToggle(el, initMap, cardKey);
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
// ``key`` (optional) persists the expansion in state.journeyExpanded so a
// day-body re-render (queue refresh, stale reload on tab return) restores
// the card instead of collapsing it.
function wireJourneyToggle(el, initMap, key) {
  const header = el.querySelector(".journey-header");
  const body = el.querySelector(".journey-body");
  const caret = el.querySelector(".caret");
  const setOpen = (opening) => {
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
  };
  header.addEventListener("click", (e) => {
    if (e.target.closest("input, a, button")) return;
    const opening = body.hidden;
    if (key) {
      if (opening) state.journeyExpanded.add(key);
      else state.journeyExpanded.delete(key);
    }
    setOpen(opening);
  });
  // Restore an expansion remembered across re-renders. Unhide immediately
  // (no visual pop-in), but defer the map init a frame: this runs while the
  // card is still detached (the renderer hasn't appended it yet), and
  // Leaflet reading a zero-size container computes a garbage zoom.
  if (key && state.journeyExpanded.has(key)) {
    body.hidden = false;
    caret.textContent = "▾";
    requestAnimationFrame(() => setOpen(true));
  }
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

// House glyph (SVG, no emoji) for the Home location — settings marker + labels.
const HOME_GLYPH =
  '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M2 7l6-4 6 4M4 6.5V13h8V6.5"/></svg>';

// Generic map-pin glyph (SVG, no emoji) for non-Home named places.
const PIN_GLYPH =
  '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M8 14s4.5-4.2 4.5-7.5a4.5 4.5 0 0 0-9 0C3.5 9.8 8 14 8 14z"/><circle cx="8" cy="6.5" r="1.5"/></svg>';

// Label glyph: house for the designated Home, pin for any other named place,
// nothing for a plain geocoded label.
function placeGlyph(isHome, isNamed) {
  return isHome ? HOME_GLYPH : (isNamed ? PIN_GLYPH : "");
}

// Small inline SVG status icons for not-yet-downloaded clips (no emoji).
const TRIAGE_STATE_ICON = {
  pending: '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M8 2v8M5 7l3 3 3-3M3 13h10"/></svg>',
  downloading: '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" class="spin"><path d="M8 2a6 6 0 1 1-6 6"/></svg>',
  failed: '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M8 1.5 15 14H1zM8 6v3.5M8 11.5h.01"/></svg>',
  skipped: '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="8" cy="8" r="6"/><path d="M4 4l8 8"/></svg>',
  geofence: '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M2 7l6-4 6 4M4 6.5V13h8V6.5"/></svg>',
};
const TRIAGE_STATE_TITLE = {
  pending: "Queued for download",
  downloading: "Downloading",
  failed: "Download failed",
  skipped: "Skipped (excluded from download)",
  geofence: "Skipped: parked at home",
};

function renderClipPair(pair) {
  const el = document.createElement("div");
  el.className = "clip-pair";
  el.dataset.pairId = `${pair.timestamp}_${pair.sequence}`;
  el.dataset.flipKey = `p|${el.dataset.pairId}`;
  el.dataset.ts = pair.timestamp;
  const time = new Date(pair.iso).toLocaleTimeString();
  const thumb = (c, cam) => {
    if (c && c.remote) {
      const st = c.state || "pending";
      // A geofence auto-skip gets a distinct house glyph + accent so it
      // reads apart from a manual skip.
      const geo = st === "skipped" && c.skip_reason === "geofence";
      const key = geo ? "geofence" : st;
      const icon = TRIAGE_STATE_ICON[key] || TRIAGE_STATE_ICON.pending;
      const title = TRIAGE_STATE_TITLE[key] || TRIAGE_STATE_TITLE.pending;
      const unskipBtn = geo
        ? `<button class="unskip-btn" data-filename="${escHtml(c.filename)}"
              title="Un-skip: queue this clip for download">Un-skip</button>`
        : "";
      return `<div class="thumb remote state-${st}${geo ? " geofence" : ""}" data-camera="${cam}">
        <div class="remote-badge" title="${title}">${icon}</div>
        <div class="label" title="${escHtml(c.basename)}">${escHtml(c.basename)}</div>
        ${unskipBtn}
      </div>`;
    }
    return c ? `<div class="thumb" data-camera="${cam}"
               data-clip-id="${c.id}" data-ts="${pair.timestamp}">
        <img src="/api/archive/clip/${c.id}/thumb" data-id="${c.id}"
             alt="" loading="lazy" decoding="async" />
        <div class="film-scrub" aria-hidden="true"></div>
        <div class="label" title="${escHtml(c.basename)}">${escHtml(c.basename)}</div>
      </div>` : `<div class="thumb empty"><div class="label">—</div></div>`;
  };
  // Kind badge: shown for parking / read-only pairs only. Driving
  // pairs are the common case so we leave them un-badged to keep
  // the grid quiet — absence of a chip reads as "driving".
  const kindBadge = (() => {
    if (pair.event_type === "parking") {
      return `<span class="kind-badge kind-parking">Parking</span>`;
    }
    if (pair.event_type === "ro" || pair.locked) {
      // Dashcam-locked (ro) OR user-pinned (locked) both read as "retained".
      return `<span class="kind-badge kind-ro">Read-only</span>`;
    }
    return "";
  })();
  el.innerHTML = `
    <div class="time">
      <label><input type="checkbox" data-pair="${pair.timestamp}_${pair.sequence}" /> ${time}</label>
      ${kindBadge}
    </div>
    <div class="thumbs">${CAMERAS.map((c) =>
      // front/rear always render (placeholder when missing); a
      // third camera renders only when present so 2-camera days
      // keep their familiar two columns.
      c.channel === "front" || c.channel === "rear"
        ? thumb(pair[c.channel], c.letter)
        : pair[c.channel] ? thumb(pair[c.channel], c.letter) : ""
    ).join("")}</div>
  `;
  el.querySelectorAll(".unskip-btn").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      btn.disabled = true;
      // On success the day re-renders (this button is gone); on failure
      // runQueueAction swallows the error, so re-enable to allow a retry.
      try { await runQueueAction("retry", [btn.dataset.filename]); }
      finally { btn.disabled = false; }
    });
  });
  el.querySelectorAll(".thumb img").forEach((img) => {
    img.addEventListener("click", (e) => {
      const thumbEl = e.currentTarget.closest(".thumb");
      const cam = thumbEl ? thumbEl.dataset.camera : "F";
      openVideo(Number(img.dataset.id), cam, thumbEl);
    });
  });

  // Lazy hover-scrub: load each clip's filmstrip the first time its tile is
  // hovered, then scrub the overlay. The metadata endpoint generates the
  // sprite on demand (cached), so this only does work for tiles the user
  // actually points at. Single-frame clips (frames < 2) stay static.
  el.querySelectorAll(".thumb[data-clip-id]").forEach((thumbEl) => {
    const id = thumbEl.dataset.clipId;
    const overlay = thumbEl.querySelector(".film-scrub");
    if (!overlay) return;
    wireLazyFilmstripScrub(
      overlay, thumbEl,
      (signal) => api(`/api/archive/clip/${id}/filmstrip`, { signal }));
  });

  // Selection checkbox → export set. Preserve selected state
  // across re-renders (e.g. auto-refresh).
  const cb = el.querySelector('input[type="checkbox"]');
  const pairId = el.dataset.pairId;
  if (state.archiveSelected.has(pairId)) cb.checked = true;
  cb.addEventListener("change", () => {
    if (cb.checked) {
      const clipId = (cam) => {
        const t = el.querySelector(`.thumb[data-camera="${cam}"]`);
        return t && t.dataset.clipId ? Number(t.dataset.clipId) : null;
      };
      state.archiveSelected.set(pairId, {
        ts: Number(el.dataset.ts),
        filenames: CAMERAS
          .map((c) => pair[c.channel] && pair[c.channel].basename)
          .filter(Boolean),
        ...Object.fromEntries(
          CAMERAS.map((c) => [c.channel, clipId(c.letter)]),
        ),
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
    // Also reflect up to the day-level "select all" header checkbox.
    const dayEl = el.closest(".day");
    if (dayEl) refreshDayCheck(dayEl);
  });

  return el;
}

// ---- Journey-level "select all" ----

function setCardSelection(cardEl, select) {
  cardEl.querySelectorAll('.clip-pair input[type="checkbox"]')
    .forEach((cb) => {
      if (cb.disabled) return;            // skip any explicitly-disabled checkbox
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
  const pairs = cardEl.querySelectorAll(
    '.clip-pair input[type="checkbox"]:not([disabled])');
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

// ---- Day-level "select all" ----

function refreshDayCheck(dayEl) {
  const cb = dayEl.querySelector(".day-check");
  if (!cb) return;
  const body = dayEl.querySelector(".day-body");
  // A collapsed / not-yet-loaded day keeps its checkbox actionable — clicking
  // it loads the body and selects everything. We just can't reflect counts yet.
  if (!body || body.dataset.loaded !== "1" || body.hidden) {
    cb.disabled = false;
    cb.indeterminate = false;
    return;
  }
  const pairs = dayEl.querySelectorAll(
    '.day-body .clip-pair input[type="checkbox"]:not([disabled])');
  const total = pairs.length;
  if (total === 0) {          // loaded but nothing selectable (e.g. remote-only)
    cb.disabled = true;
    cb.checked = false;
    cb.indeterminate = false;
    return;
  }
  let checked = 0;
  pairs.forEach((p) => { if (p.checked) checked++; });
  cb.disabled = false;
  cb.checked = checked === total;
  cb.indeterminate = checked > 0 && checked < total;
}

function wireDayCheck(dayEl) {
  const cb = dayEl.querySelector(".day-check");
  if (!cb) return;
  // Keep the tick from also toggling the day's expand/collapse (same header).
  cb.addEventListener("click", (e) => e.stopPropagation());
  cb.addEventListener("change", async (e) => {
    const body = dayEl.querySelector(".day-body");
    if (e.target.checked) {
      // Selecting a whole day needs its clips in the DOM, so expand + load
      // first if it hasn't been opened yet, then tick every clip.
      if (body.hidden) {
        body.hidden = false;
        state.archiveExpanded.add(dayEl.dataset.day);
      }
      if (body.dataset.loaded !== "1") await loadDayBody(dayEl);
      setCardSelection(body, true);
    } else {
      setCardSelection(body, false);
    }
    refreshDayCheck(dayEl);
  });
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
  document.getElementById("clip-action-apply").disabled = n === 0;
  document.getElementById("clip-action-clear").disabled = n === 0;
  rebuildClipActionMenu();
}

function clearSelection() {
  state.archiveSelected.clear();
  document.querySelectorAll(
    '#view-archive .clip-pair input[type="checkbox"]',
  ).forEach((cb) => { cb.checked = false; });
  // Reset every journey/stop card's "select all" header
  // checkbox so it goes back to unchecked too.
  document.querySelectorAll("#view-archive .journey-card")
    .forEach(refreshCardCheck);
  // And every day-level "select all" header checkbox.
  document.querySelectorAll("#view-archive .day-check").forEach((cb) => {
    cb.checked = false;
    cb.indeterminate = false;
  });
  updateArchiveActions();
}

function downloadOriginals(slot) {
  // slot: "front" | "rear" | "tele" | "interior". Download each
  // selected original clip
  // as its own file (no ZIP). The clip stream endpoint sends
  // Content-Disposition: attachment with the dashcam basename, so
  // a same-origin anchor click triggers a named download. Stagger
  // the clicks (150ms) so the browser queues them instead of
  // dropping all but the last, and shows its one "allow multiple
  // downloads" prompt once — small enough that a big selection
  // doesn't take ages to fire.
  const ids = [];
  for (const v of state.archiveSelected.values()) {
    if (v[slot]) ids.push(v[slot]);
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
  // join_<channel> takes that channel's clips; the pip family takes
  // front + the partner channel ("pip" is the legacy front-main +
  // rear name, "pip_<channel>" makes the partner fullscreen).
  const joinCh = type.startsWith("join_") ? type.slice(5) : null;
  const pipCh =
    type === "pip" ? "rear"
    : type.startsWith("pip_") ? type.slice(4)
    : null;
  const ids = [];
  for (const v of state.archiveSelected.values()) {
    if (joinCh && v[joinCh]) ids.push(v[joinCh]);
    else if (pipCh) {
      if (v.front) ids.push(v.front);
      if (v[pipCh]) ids.push(v[pipCh]);
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

// Used by the timeline editor's "View export jobs" toast action: surface the
// Archive tab and expand the (collapsible) export-jobs panel.
function viewExportJobs() {
  location.hash = "#/archive";
  setExportsPanelOpen(true);
  // Defer the scroll: the hashchange -> routeTo that un-hides #view-archive
  // runs after this call stack, so scrolling now (while the archive view is
  // still hidden behind the timeline tab) would be a no-op.
  requestAnimationFrame(() => {
    const panel = document.getElementById("exports-panel");
    if (panel) panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  });
}

document.getElementById("exports-toggle").addEventListener("click", () => {
  const open = document.getElementById("exports-toggle")
    .getAttribute("aria-expanded") === "true";
  setExportsPanelOpen(!open);
});

// ---- Consolidated "Clip actions" control (sticky toolbar) ----

const CAMERA_LABEL_BY_CHANNEL = Object.fromEntries(CAMERAS.map((c) => [c.channel, c.label]));
const EXPORT_CAMERAS_ALWAYS = ["front", "rear"];

// Rebuild the Export optgroup: Front/Rear always; Tele/Interior only when the
// current selection contains such clips. Originals first, then Joined (the order
// the user reads top-to-bottom). Preserves the current selection if still valid.
function rebuildClipActionMenu() {
  const sel = document.getElementById("clip-action");
  const grp = document.getElementById("clip-action-export");
  if (!sel || !grp) return;
  const present = new Set();
  for (const v of state.archiveSelected.values()) {
    for (const c of EXTRA_CAMERAS) if (v[c.channel]) present.add(c.channel);
  }
  const cams = [
    ...EXPORT_CAMERAS_ALWAYS,
    ...EXTRA_CAMERAS.filter((c) => present.has(c.channel)).map((c) => c.channel),
  ];
  // Only rewrite the optgroup when the camera set actually changed — avoids
  // churning the DOM (and closing an open dropdown) on every checkbox tick.
  const newValues = cams.flatMap((cam) => [`originals-${cam}`, `join-${cam}`]);
  // grp is an <optgroup> (no .options — that's a <select>-only property); its
  // <option>s are its direct children.
  const oldValues = [...grp.children].map((o) => o.value);
  if (newValues.join() === oldValues.join()) return;
  const prev = sel.value;
  grp.innerHTML =
    cams.map((cam) =>
      `<option value="originals-${cam}">Originals · ${CAMERA_LABEL_BY_CHANNEL[cam]}</option>`).join("") +
    cams.map((cam) =>
      `<option value="join-${cam}">Joined · ${CAMERA_LABEL_BY_CHANNEL[cam]}</option>`).join("");
  // If the previously-selected option vanished (a tele/interior option that's no
  // longer offered), fall back to the first Queue action.
  if ([...sel.options].some((o) => o.value === prev)) sel.value = prev;
  else sel.value = "download-next";
}

function selectionHasCamera(cam) {
  for (const v of state.archiveSelected.values()) if (v[cam]) return true;
  return false;
}

const QUEUE_ENDPOINT = {
  "download-next": "/api/queue/download-next",
  "skip": "/api/queue/skip",
};

async function runQueueAction(action, filenames) {
  try {
    // Skipping removes the tiles from the archive — fade them out first so
    // the removal reads as intentional; the re-render below then FLIPs the
    // surviving neighbours into the freed space.
    if (action === "skip") await animateOutSelectedPairs();
    let res;
    if (action === "retry") {
      // Retry also clears any user/geofence skip on the selection first.
      await api("/api/queue/unskip", { method: "POST", body: JSON.stringify({ filenames }) });
      res = await api("/api/queue/retry", { method: "POST", body: JSON.stringify({ filenames }) });
    } else {
      const endpoint = QUEUE_ENDPOINT[action];
      if (!endpoint) { toast(`Unknown action: ${action}`, { type: "error" }); return; }
      res = await api(endpoint, { method: "POST", body: JSON.stringify({ filenames }) });
    }
    toast(`${action.replaceAll("-", " ")}: ${res.updated} updated`);
    clearSelection();
    // Skip removes tiles from the archive, so the day cards' totals
    // changed too — anything else only flips per-clip status icons.
    if (action === "skip") loadDays();
    else refreshOpenArchiveDays();
  } catch (e) {
    toast(`Action failed: ${e.message || e}`, { type: "error" });
  }
}

function deleteResultToast(res) {
  const bits = [`Deleted ${res.deleted}`];
  if (res.skipped) bits.push(`${res.skipped} won't re-download`);
  if (res.protected) bits.push(`${res.protected} read-only/locked kept`);
  if (res.failed) bits.push(`${res.failed} failed — file could not be removed, see Logs`);
  toast(bits.join(", "), { type: res.failed ? "error" : "success" });
}

async function runQueueDelete(filenames) {
  try {
    await animateOutSelectedPairs();
    const res = await api("/api/queue/delete", { method: "POST", body: JSON.stringify({ filenames }) });
    // Confirm-through for protected clips: the first call already deleted
    // everything deletable, so ask once more and force-delete exactly the
    // refused names. Declining keeps them — the reload below restores
    // their faded-out tiles.
    if (res.protected > 0 && (res.protected_names || []).length) {
      const ok = confirm(
        `${res.protected} of the selected clip(s) are read-only or locked ` +
        `(dashcam event recordings, or clips marked read-only). Delete them anyway?`,
      );
      if (ok) {
        const forced = await api("/api/queue/delete", {
          method: "POST",
          body: JSON.stringify({ filenames: res.protected_names, force: true }),
        });
        res.deleted += forced.deleted;
        res.skipped += forced.skipped;
        res.failed += forced.failed;
        res.protected = 0;
      }
    }
    deleteResultToast(res);
    clearSelection();
    // Full reload, not just open bodies: the day cards' clip counts and
    // GB totals changed too.
    loadDays();
  } catch (e) {
    toast(`Delete failed: ${e.message || e}`, { type: "error" });
  }
}

async function applyClipAction() {
  if (state.archiveSelected.size === 0) {
    toast("Select some clips first.", { type: "error" });
    return;
  }
  const action = document.getElementById("clip-action").value;

  // Export actions operate on per-camera clip ids.
  if (action.startsWith("originals-") || action.startsWith("join-")) {
    const cam = action.slice(action.indexOf("-") + 1);
    if (!selectionHasCamera(cam)) {
      toast(`No ${CAMERA_LABEL_BY_CHANNEL[cam] || cam} clips selected.`, { type: "error" });
      return;
    }
    if (action.startsWith("originals-")) downloadOriginals(cam);
    else submitExport(`join_${cam}`);
    return;
  }

  // Queue actions operate on filenames.
  const filenames = [
    ...new Set([...state.archiveSelected.values()].flatMap((v) => v.filenames || [])),
  ];
  if (!filenames.length) {
    toast("Select some clips first.", { type: "error" });
    return;
  }
  if (action === "mark-ro" || action === "clear-ro") {
    const lock = action === "mark-ro";
    try {
      const res = await api(lock ? "/api/queue/lock" : "/api/queue/unlock",
        { method: "POST", body: JSON.stringify({ filenames }) });
      toast(lock ? `Marked ${res.updated} read-only`
                 : `Cleared read-only on ${res.updated}`);
      clearSelection();
      refreshOpenArchiveDays();
    } catch (e) {
      toast(`Failed: ${e.message || e}`, { type: "error" });
    }
    return;
  }
  if (action === "delete") {
    if (!confirm(
      `Delete ${filenames.length} downloaded clip(s) from storage and skip them ` +
      `so they won't re-download? Files on the camera aren't affected.`,
    )) return;
    await runQueueDelete(filenames);
    return;
  }
  await runQueueAction(action, filenames);
}

document.getElementById("clip-action-apply").addEventListener("click", applyClipAction);
document.getElementById("clip-action-clear").addEventListener("click", clearSelection);

// Prime the toolbar's disabled state + camera-aware menu on load. Placed here
// (not at updateArchiveActions's definition) so the consts above are initialised.
updateArchiveActions();

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

// App-wide transient notification. `type` is "success" (default) or "error".
// Optional { actionLabel, onAction } renders a single inline action button.
// Auto-dismisses after `duration` ms (errors linger longer); also closable.
function toast(message, opts = {}) {
  const { type = "success", actionLabel, onAction, duration } = opts;
  const host = document.getElementById("toast-container");
  if (!host) return;
  const card = document.createElement("div");
  card.className = `toast toast--${type === "error" ? "error" : "success"}`;
  card.setAttribute("role", type === "error" ? "alert" : "status");

  const msg = document.createElement("span");
  msg.className = "toast__msg";
  msg.textContent = message;
  card.appendChild(msg);

  if (actionLabel && typeof onAction === "function") {
    const act = document.createElement("button");
    act.type = "button";
    act.className = "toast__action";
    act.textContent = actionLabel;
    act.addEventListener("click", () => { dismiss(); onAction(); });
    card.appendChild(act);
  }

  const close = document.createElement("button");
  close.type = "button";
  close.className = "toast__close";
  close.setAttribute("aria-label", "Dismiss");
  close.textContent = "×";
  close.addEventListener("click", dismiss);
  card.appendChild(close);

  host.appendChild(card);
  // Trigger the enter transition on the next frame.
  requestAnimationFrame(() => card.classList.add("toast--in"));

  let timer = setTimeout(dismiss, duration || (type === "error" ? 7000 : 5000));
  let gone = false;
  function dismiss() {
    if (gone) return;
    gone = true;
    clearTimeout(timer);
    card.classList.remove("toast--in");
    card.addEventListener("transitionend", () => card.remove(), { once: true });
    // Fallback removal in case the transitionend never fires.
    setTimeout(() => card.remove(), 400);
  }
}

// Human-readable export type labels. These echo the toolbar
// buttons: Join F/R and the PiP Fr/Rf (front-main / rear-main).
// Display labels only. The keys are the load-bearing internal type ids used
// in the API/DB/routing (the timeline-cut type is "timeline" on the wire);
// this map just controls what the badge reads. Missing key -> raw id shown.
const EXPORT_TYPE_LABELS = {
  join_front: "Join Front",
  join_rear: "Join Rear",
  join_tele: "Join Tele",
  join_interior: "Join Interior",
  pip: "PiP Fr",
  pip_rear: "PiP Rf",
  pip_tele: "PiP Tf",
  pip_interior: "PiP If",
  timeline: "Timeline",
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

const EXPORT_ICON_PAUSE =
  '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" ' +
  'aria-hidden="true"><path d="M9 4.5H6.75A.75.75 0 0 0 6 5.25v13.5c0 ' +
  '.414.336.75.75.75H9a.75.75 0 0 0 .75-.75V5.25A.75.75 0 0 0 9 4.5Zm8.25 ' +
  '0H15a.75.75 0 0 0-.75.75v13.5c0 .414.336.75.75.75h2.25a.75.75 0 0 0 ' +
  '.75-.75V5.25a.75.75 0 0 0-.75-.75Z"/></svg>';

const EXPORT_ICON_RESUME =
  '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" ' +
  'aria-hidden="true"><path d="M5.25 5.653c0-1.426 1.529-2.33 2.779-1.643l' +
  '11.54 6.348c1.295.712 1.295 2.573 0 3.285L8.029 19.99c-1.25.687-2.779-' +
  '.217-2.779-1.643V5.653Z"/></svg>';

function escapeExportText(s) {
  // Delegates to escHtml — the DOM-based version this replaced did
  // not escape quotes, so it was unsafe in attribute contexts.
  return escHtml(s);
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

// ---------- Filmstrip hover-scrub (shared) ----------
//
// Turn `el` into a ready-to-scrub layer for an N-frame sprite. The :hover CSS
// rule deliberately omits animation-timing-function so the per-element step
// count set here (which CSS can't express via a custom property) wins. Returns
// false (a no-op) when there's nothing to scrub: no sprite, or a single tile.
function applyFilmstripScrub(el, spriteUrl, frames) {
  if (!el || !spriteUrl || !(frames >= 2)) return false;
  el.style.backgroundImage = `url("${spriteUrl}")`;
  el.style.setProperty("--frames", String(frames));
  el.style.animationTimingFunction = `steps(${frames}, jump-none)`;
  el.classList.add("is-ready");
  return true;
}

// Lazily load a filmstrip once `hoverEl` has been hovered for a beat, then
// wire `el` to scrub. `load(signal)` returns a promise of the filmstrip
// metadata ({ sprite_url, frames }). The dwell requirement matters: each cold
// request spawns ffmpeg on the server, and firing on raw mouseenter meant a
// fast sweep across a day grid queued one sprite job per tile crossed —
// saturating the browser's ~6 connections per origin and blocking every other
// API call behind sprite generation. Leaving the tile cancels the timer and
// aborts any in-flight request. A thrown error (network/5xx/abort) re-arms so
// a later hover retries; a clip that can't be rendered (204, no sprite_url)
// is left as a permanent no-op.
const FILMSTRIP_HOVER_DWELL_MS = 350;
function wireLazyFilmstripScrub(el, hoverEl, load) {
  let started = false;
  let dwellTimer = null;
  let ctrl = null;
  hoverEl.addEventListener("mouseenter", () => {
    if (started || dwellTimer) return;
    dwellTimer = setTimeout(() => {
      dwellTimer = null;
      started = true;
      ctrl = new AbortController();
      Promise.resolve(ctrl.signal)
        .then(load)
        .then((meta) => {
          if (meta && meta.sprite_url) {
            applyFilmstripScrub(el, meta.sprite_url, meta.frames);
          }
        })
        .catch(() => { started = false; })
        .finally(() => { ctrl = null; });
    }, FILMSTRIP_HOVER_DWELL_MS);
  });
  hoverEl.addEventListener("mouseleave", () => {
    if (dwellTimer) { clearTimeout(dwellTimer); dwellTimer = null; }
    if (ctrl) ctrl.abort();
  });
}

// Export sprites are a fixed-length strip (mirrors export_preview.N_FRAMES).
const EXPORT_FILMSTRIP_FRAMES = 10;

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
      <th class="export-preview-col"></th>
      <th>Type</th><th>Status</th><th>Footage</th>
      <th>Length</th><th>Size</th>
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
    if (j.state === "running" || j.state === "paused") {
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
    // Pause (while rendering) / Resume (while paused). Empty slot otherwise
    // so the row's action columns stay aligned.
    let ctrl =
      `<span class="export-action export-action--empty" aria-hidden="true">` +
      `</span>`;
    if (j.state === "running") {
      ctrl =
        `<button type="button" class="export-action export-pause" ` +
        `data-id="${j.id}" data-act="pause" title="Pause" ` +
        `aria-label="Pause export">${EXPORT_ICON_PAUSE}</button>`;
    } else if (j.state === "paused") {
      ctrl =
        `<button type="button" class="export-action export-resume" ` +
        `data-id="${j.id}" data-act="resume" title="Resume" ` +
        `aria-label="Resume export">${EXPORT_ICON_RESUME}</button>`;
    }
    const del =
      `<button type="button" class="export-action export-delete" ` +
      `data-id="${j.id}" title="Delete" aria-label="Delete export">` +
      `${EXPORT_ICON_TRASH}</button>`;

    // Filmstrip preview. A finished job whose strip is cached shows the
    // static first frame and scrubs through the export on hover via .film-scrub.
    // A finished job whose strip is still generating shows a shimmer
    // placeholder — still click-to-play, since the output already exists; it
    // swaps to the real strip on the export_preview_ready event. Non-done rows
    // get an empty cell of the same size so the columns stay aligned.
    let previewCell;
    if (j.state === "done" && j.has_preview) {
      previewCell =
        `<div class="export-thumb film-scrub" data-job-id="${j.id}" ` +
        `title="Play export"></div>`;
    } else if (j.state === "done") {
      previewCell =
        `<div class="export-thumb export-thumb--loading" data-job-id="${j.id}" ` +
        `title="Play export" aria-label="Preview generating"></div>`;
    } else {
      previewCell =
        `<div class="export-thumb export-thumb--empty" aria-hidden="true"></div>`;
    }

    // Output length + size, snapshotted on the row at finish. Only finished
    // jobs have them; everything else shows an em-dash.
    const lengthCell = j.output_duration_s != null
      ? fmtClock(j.output_duration_s)
      : "—";
    const sizeCell = j.output_size != null
      ? fmtBytes(j.output_size)
      : "—";

    tr.innerHTML = `
      <td class="export-preview">${previewCell}</td>
      <td>${typeCell}</td>
      <td class="export-status">${statusCell}</td>
      <td class="export-footage">${footageCell}</td>
      <td class="export-length">${lengthCell}</td>
      <td class="export-size">${sizeCell}</td>
      <td class="export-actions">${dl}${ctrl}${del}</td>
    `;
    tbody.appendChild(tr);
  }
  el.appendChild(table);
  el.querySelectorAll(".export-thumb[data-job-id]").forEach((thumb) => {
    thumb.addEventListener("click",
      () => openExportVideo(Number(thumb.dataset.jobId)));
  });
  el.querySelectorAll(".export-thumb.film-scrub[data-job-id]").forEach((thumb) => {
    applyFilmstripScrub(
      thumb,
      `/api/exports/${thumb.dataset.jobId}/filmstrip.jpg`,
      EXPORT_FILMSTRIP_FRAMES);
  });
  el.querySelectorAll(".export-delete").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm("Delete this export job and its output?")) return;
      await api(`/api/exports/${btn.dataset.id}`, { method: "DELETE" });
      refreshExportJobs();
    });
  });
  el.querySelectorAll(".export-pause, .export-resume").forEach((btn) => {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        await api(`/api/exports/${btn.dataset.id}/${btn.dataset.act}`,
                  { method: "POST" });
      } catch (err) {
        // 409 if the job moved on (finished/failed) between render and click.
      }
      refreshExportJobs();
    });
  });
}

// ---------- Video modal + nav ----------

// Modal camera cycle follows registry order. Only F+R plus one of
// the extras exist on any single device, but keeping them all costs
// nothing — absent cameras have empty timelines and the cycle
// skips them.
const MODAL_CAMERAS = CAMERAS.map((c) => c.letter);

function buildCameraTimelines(scopeEl) {
  const root = scopeEl || document.getElementById("view-archive");
  const timelines = Object.fromEntries(
    MODAL_CAMERAS.map((cam) => [cam, []]),
  );
  root.querySelectorAll(".thumb[data-clip-id]").forEach((el) => {
    const entry = {
      id: Number(el.dataset.clipId),
      ts: Number(el.dataset.ts),
      el,
    };
    (timelines[el.dataset.camera] || timelines.F).push(entry);
  });
  for (const cam of MODAL_CAMERAS) {
    timelines[cam].sort((a, b) => a.ts - b.ts);
  }
  return timelines;
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
  document.querySelector(".modal-nav").hidden = false;  // clip mode: show nav
  document.getElementById("modal").hidden = false;
  updateModalNav();
}

// Play a finished export in the same modal chrome (overlay, ×, Esc) as the
// clip player, minus the clip nav — an export is a single standalone video,
// so prev/next/camera-toggle don't apply. modalClip=null leaves the (null-safe)
// nav handlers and arrow/F keys as no-ops.
function openExportVideo(jobId) {
  state.modalClip = null;
  document.querySelector(".modal-nav").hidden = true;
  document.getElementById("modal-body").innerHTML =
    `<video src="/api/exports/${jobId}/video" controls autoplay></video>`;
  document.getElementById("modal").hidden = false;
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

  const curr = list[i];
  const next_ = curr ? nextCameraClip(mc, curr.ts) : null;
  toggle.disabled = !next_;
  if (next_) {
    toggle.textContent = `${CAMERA_LABELS[next_.camera]} view`;
  }
}

// The next camera (cycling F→R→T→I→F) that has a clip at ``ts``,
// or null when the current camera is the only one — the cycle
// skips cameras with no same-timestamp clip, so a 2-cam day
// behaves exactly like the old binary toggle.
function nextCameraClip(mc, ts) {
  const start = MODAL_CAMERAS.indexOf(mc.camera);
  for (let step = 1; step < MODAL_CAMERAS.length; step++) {
    const cam = MODAL_CAMERAS[(start + step) % MODAL_CAMERAS.length];
    const match = mc.timelines[cam].find((e) => e.ts === ts);
    if (match) return { camera: cam, clip: match };
  }
  return null;
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
  const curr = mc.timelines[mc.camera].find((e) => e.id === mc.id);
  if (!curr) return;
  const next = nextCameraClip(mc, curr.ts);
  if (!next) return;
  // Preserve current playback position and pause state so the
  // other camera picks up where this one was.
  const video = document.querySelector("#modal-body video");
  const seekTo = video ? video.currentTime : 0;
  const autoplay = video ? !video.paused : true;
  openVideo(next.clip.id, next.camera, next.clip.el, { seekTo, autoplay });
}

function closeModal() {
  document.getElementById("modal").hidden = true;
  document.getElementById("modal-body").innerHTML = "";
  document.querySelector(".modal-nav").hidden = false;  // restore default
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
    pending: 0, downloading: 0, done: 0, failed: 0, gone: 0, skipped: 0,
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
  // hasOwn guard: cam comes from API data, and a plain-object
  // lookup would resolve prototype keys ("constructor", …) to
  // functions instead of "?".
  const camLabel = Object.hasOwn(CAMERA_LABELS, cam)
    ? CAMERA_LABELS[cam] : "?";
  const parts = [`<span class="kind-badge kind-${escHtml(cam)}">${camLabel}</span>`];
  if (evt === "parking") {
    parts.push(`<span class="kind-badge kind-parking">Parking</span>`);
  } else if (evt === "event") {
    parts.push(`<span class="kind-badge kind-event">Event</span>`);
  }
  if (it.kind_ro || it.locked) {
    // Dashcam-locked (kind_ro) OR user-pinned (locked) — both retained.
    const title = it.locked ? "Read-only — retained" : "Read-only";
    parts.push(`<span class="kind-badge kind-ro" title="${title}">RO</span>`);
  }
  return parts.join(" ");
}

// GPS triage indicator for a queue row. gps_state comes from the API:
// "ok" = fetched, "none" = no GPS / unreadable, "pending" = awaiting triage.
const GPS_BADGE = {
  ok:      { sym: "✓", cls: "gps-ok",      title: "GPS fetched" },
  none:    { sym: "✗", cls: "gps-none",    title: "No GPS / unreadable" },
  pending: { sym: "–", cls: "gps-pending", title: "Awaiting triage" },
};
function renderGpsBadge(it) {
  // GPS is a capture-level fact carried by one lens (the front). The server
  // (driven by the camera registry) derives gps_state from that lens's own
  // triage columns and *implies* the same state onto its sibling lenses, so
  // every clip of a capture shows the badge; only an orphan non-GPS clip
  // (no front at its timestamp) stays blank — no camera logic needed here.
  if (!it.gps_state) return "";
  const b = GPS_BADGE[it.gps_state] || GPS_BADGE.pending;
  return `<span class="gps-badge ${b.cls}" title="${b.title}">${b.sym}</span>`;
}

// Kept as a thin alias so existing call sites still resolve;
// fmtBytes is the canonical helper and auto-scales beyond MB/GB.
const fmtMB = fmtBytes;

function renderQueueDayCard(d) {
  const el = document.createElement("div");
  const hasPending = d.pending_count > 0;
  const hasSelectable = daySelectableCount(d) > 0;
  const isStale = !hasPending && d.downloading_count === 0;
  el.className = "day queue-day" + (isStale ? " queue-day-stale" : "");
  el.dataset.day = d.day;

  const pieces = [];
  if (d.downloading_count) pieces.push(`<span class="state-downloading">${d.downloading_count} downloading</span>`);
  if (d.pending_count)     pieces.push(`<span class="state-pending">${d.pending_count} pending</span>`);
  if (d.done_count)        pieces.push(`<span class="state-done">${d.done_count} done</span>`);
  if (d.failed_count)      pieces.push(`<span class="state-failed">${d.failed_count} failed</span>`);
  if (d.gone_count)        pieces.push(`<span class="state-gone">${d.gone_count} gone</span>`);
  if (d.skipped_count)     pieces.push(`<span class="state-skipped">${d.skipped_count} skipped</span>`);

  const expanded = state.queueExpanded.has(d.day);
  const selected = countSelectedInDay(d.day);
  const checkState = dayCheckState(d, selected);

  el.innerHTML = `
    <div class="day-header queue-day-header">
      <span class="caret">${expanded ? "▾" : "▸"}</span>
      <input type="checkbox" class="qd-check"
             ${checkState === "checked" ? "checked" : ""}
             ${!hasSelectable ? "disabled title='Nothing selectable'" : ""} />
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
  const hasSelectable = s.pending + s.failed + (s.skipped || 0) > 0;
  const label = hh === "??" ? "Unknown time" : `${hh}:00–${hh}:59`;

  const pieces = [];
  if (s.downloading) pieces.push(`<span class="state-downloading">${s.downloading} downloading</span>`);
  if (s.pending)     pieces.push(`<span class="state-pending">${s.pending} pending</span>`);
  if (s.done)        pieces.push(`<span class="state-done">${s.done} done</span>`);
  if (s.failed)      pieces.push(`<span class="state-failed">${s.failed} failed</span>`);
  if (s.gone)        pieces.push(`<span class="state-gone">${s.gone} gone</span>`);
  if (s.skipped)     pieces.push(`<span class="state-skipped">${s.skipped} skipped</span>`);

  el.innerHTML = `
    <div class="queue-hour-header">
      <span class="caret">${expanded ? "▾" : "▸"}</span>
      <input type="checkbox" class="qh-check" data-hour="${hh}"
             ${checkState === "checked" ? "checked" : ""}
             ${!hasSelectable ? "disabled title='Nothing selectable'" : ""} />
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
      <th>GPS</th>
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
    const selectable = isSelectable(it.state);
    const checked = state.queueSelected.has(it.filename);
    const kind = renderKindBadge(it);
    tr.innerHTML = `
      <td><input type="checkbox" class="qi-check" value="${escHtml(it.filename)}"
            ${selectable ? "" : "disabled"}
            ${checked ? "checked" : ""} /></td>
      <td>${ts}</td>
      <td>${kind}</td>
      <td class="gps-cell">${renderGpsBadge(it)}</td>
      <td>${escHtml(it.filename)}</td>
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

// Which queue states can be selected for a bulk action: pending (download
// next / skip), failed (skip / retry), skipped (clear skip).
function isSelectable(stateStr) {
  return stateStr === "pending" || stateStr === "failed"
      || stateStr === "skipped";
}

function countSelectedInDay(day) {
  const items = state.queueDayItems[day];
  if (!items) return 0;
  let n = 0;
  for (const it of items) {
    if (isSelectable(it.state) && state.queueSelected.has(it.filename)) n++;
  }
  return n;
}

function daySelectableCount(daySummary) {
  return (daySummary.pending_count || 0) + (daySummary.failed_count || 0)
       + (daySummary.skipped_count || 0);
}

function dayCheckState(daySummary, selectedCount) {
  const selectable = daySelectableCount(daySummary);
  if (!selectable) return "unchecked";
  // Can't know the per-day breakdown until the items are cached.
  if (!state.queueDayItems[daySummary.day]) return "unchecked";
  if (selectedCount === 0) return "unchecked";
  if (selectedCount >= selectable) return "checked";
  return "indeterminate";
}

function toggleDaySelection(day, select) {
  const items = state.queueDayItems[day] || [];
  for (const it of items) {
    if (!isSelectable(it.state)) continue;
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

function hourSelectableCount(day, hh) {
  let n = 0;
  for (const it of itemsInHour(day, hh)) if (isSelectable(it.state)) n++;
  return n;
}

function countSelectedInHour(day, hh) {
  let n = 0;
  for (const it of itemsInHour(day, hh)) {
    if (isSelectable(it.state) && state.queueSelected.has(it.filename)) n++;
  }
  return n;
}

function hourCheckState(day, hh) {
  const selectable = hourSelectableCount(day, hh);
  if (!selectable) return "unchecked";
  const sel = countSelectedInHour(day, hh);
  if (sel === 0) return "unchecked";
  if (sel >= selectable) return "checked";
  return "indeterminate";
}

function toggleHourSelection(day, hh, select) {
  for (const it of itemsInHour(day, hh)) {
    if (!isSelectable(it.state)) continue;
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

// Bulk actions on the current queue selection. Each posts the full selection;
// the backend WHERE clause filters to the applicable source state, so a mixed
// selection is fine and `updated` reports how many actually changed.
const QUEUE_ACTIONS = {
  "download-next": { url: "/api/queue/prioritize",
                     body: (f) => ({ filenames: f, position: "top" }),
                     label: "moved to front" },
  "skip":          { url: "/api/queue/skip",
                     body: (f) => ({ filenames: f }), label: "skipped" },
  "clear-skip":    { url: "/api/queue/unskip",
                     body: (f) => ({ filenames: f }), label: "un-skipped" },
  "retry-failed":  { url: "/api/queue/retry",
                     body: (f) => ({ filenames: f }), label: "re-queued" },
  "mark-ro":       { url: "/api/queue/lock",
                     body: (f) => ({ filenames: f }), label: "marked read-only" },
  "delete-from-camera": {
    url: "/api/queue/delete-from-camera",
    body: (f) => ({ filenames: f }),
    confirm: (n) =>
      `Delete ${n} clip(s) from the dashcam SD card? This cannot be undone.`,
    // The route returns {deleted, skipped, errors} (not {updated}), and
    // {ok:false,error} when no camera is configured.
    toast: (r) => r.ok === false
      ? `Failed: ${r.error || "no dashcam configured"}`
      : `Deleted ${r.deleted} from camera`
        + (r.skipped ? `, ${r.skipped} protected` : "")
        + (r.errors ? `, ${r.errors} error(s)` : ""),
    toastType: (r) => (r.ok === false || r.errors) ? "error" : "success",
  },
};

async function applyQueueAction() {
  const sel = document.getElementById("q-action");
  const spec = QUEUE_ACTIONS[sel.value];
  if (!spec) { toast("Choose an action first.", { type: "error" }); return; }
  const filenames = [...state.queueSelected];
  if (!filenames.length) {
    toast("Select some files first.", { type: "error" });
    return;
  }
  if (spec.confirm && !confirm(spec.confirm(filenames.length))) return;
  const btn = document.getElementById("q-apply");
  btn.disabled = true;
  try {
    const r = await api(spec.url, {
      method: "POST", body: JSON.stringify(spec.body(filenames)),
    });
    if (spec.toast) {
      toast(spec.toast(r),
        { type: spec.toastType ? spec.toastType(r) : "success" });
    } else {
      const n = r.updated ?? 0;
      toast(n
        ? `${n} file${n === 1 ? "" : "s"} ${spec.label}`
        : "No applicable items in selection.",
            { type: n ? "success" : "error" });
    }
    state.queueSelected.clear();
    await loadQueue();
  } catch (e) {
    toast(`Action failed: ${e.message || e}`, { type: "error" });
  } finally {
    btn.disabled = false;
  }
}

// Clear the queue tick selection in place (mirrors the archive "Clear"),
// unchecking every row/hour/day checkbox without a re-fetch.
function clearQueueSelection() {
  state.queueSelected.clear();
  document.querySelectorAll('#queue-days input[type="checkbox"]')
    .forEach((cb) => { cb.checked = false; cb.indeterminate = false; });
  renderQueueMeta();
}
document.getElementById("q-apply").addEventListener("click", applyQueueAction);
document.getElementById("q-clear").addEventListener("click", clearQueueSelection);

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
  let skipped = 0;
  for (const d of state.queueDays) {
    total += d.clip_count;
    pending += d.pending_count;
    skipped += d.skipped_count || 0;
  }
  const sel = state.queueSelected.size;
  let text = `${total} files across ${state.queueDays.length} days · ${pending} pending`;
  if (skipped) text += ` · ${skipped} skipped`;
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
  const when = new Date(e.ts * 1000);
  ts.textContent = `${when.toLocaleDateString()} ${when.toLocaleTimeString()}`;

  // Expand caret, immediately right of the date/time. The glyph only
  // appears on rows carrying an exception (.has-exc, set below), but
  // the column is always rendered so levels line up across rows.
  const caret = document.createElement("span");
  caret.className = "log-caret";

  const lvl = document.createElement("span");
  lvl.className = "log-level";
  lvl.textContent = e.level;

  const logger = document.createElement("span");
  logger.className = "log-logger";
  logger.textContent = e.logger;

  const msg = document.createElement("span");
  msg.className = "log-msg";
  msg.textContent = e.message;

  head.append(ts, caret, lvl, logger, msg);
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

let wsRetryDelayMs = 3000;

function openSocket() {
  if (state.ws) {
    // Mark the old socket so its close handler doesn't schedule a
    // second reconnect loop alongside the new socket's.
    state.ws._replaced = true;
    try { state.ws.close(); } catch {}
  }
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/api/progress`);
  state.ws = ws;
  ws.addEventListener("open", () => {
    wsRetryDelayMs = 3000; // healthy again — reset the backoff ladder
    if (state.wsHadConnection) {
      // The server's snapshot covers sync/session state but not list
      // contents — clip_indexed / queue / export events missed while
      // disconnected would otherwise leave these views stale.
      refreshArchiveOnIndexChange();
      refreshQueueIfVisible();
      refreshExportJobs().catch(() => {});
    }
    state.wsHadConnection = true;
  });
  ws.addEventListener("message", (e) => {
    try { handleEvent(JSON.parse(e.data)); } catch {}
  });
  ws.addEventListener("close", () => {
    if (ws._replaced) return;
    // Exponential backoff with jitter: a fixed 3s retry from every
    // open tab hammers a restarting server in lockstep.
    const delay = wsRetryDelayMs + Math.random() * 1000;
    wsRetryDelayMs = Math.min(wsRetryDelayMs * 2, 30000);
    setTimeout(openSocket, delay);
  });
}

function handleEvent(ev) {
  const statusEl = document.getElementById("sync-status");
  const STATUS_LABEL = {
    downloading: "Downloading",
    triaging: "Triaging GPS",
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
    } else if (status === "triaging") {
      badgeText = triageBadgeText();
    }
    statusEl.textContent = badgeText;
    statusEl.className = "status " + (status || "");
    statusEl.title = reason || "";
    updateSyncState(status);
  };
  switch (ev.type) {
    case "clip_indexed":
    case "clip_derived":
      // Server re-indexed (download landed, manual rescan, import, or
      // startup scan), or the derive worker finished a clip's
      // thumbnail/filmstrip. Cheap read-only refresh — the work already
      // happened server-side, and this fires only on real changes, not
      // on a timer.
      refreshArchiveOnIndexChange();
      break;
    case "triage_progress":
      state.triage = ev.active
        ? { active: true, triaged: ev.triaged, total: ev.total, eta_s: ev.eta_s }
        : { active: false };
      if (ev.active) {
        if (state.syncStatus === "triaging") {
          document.getElementById("sync-status").textContent = triageBadgeText();
        }
        document.getElementById("current-download").textContent = triageBadgeText();
        refreshArchiveOnIndexChange();
      } else {
        document.getElementById("current-download").textContent = "";
        refreshArchiveOnIndexChange();
      }
      break;
    case "snapshot":
      if (ev.state.triage) {
        state.triage = ev.state.triage;
      }
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
    case "queue_changed":
      refreshQueueIfVisible();
      scheduleOpenArchiveRefresh();
      break;
    case "queue_reconciled":
    case "sync_done":
      refreshQueueIfVisible();
      break;
    case "gps_extract_done":
      // The Extract GPS toolbar button was removed; a force re-extract can
      // still be triggered via the API, so refresh the open archive when one
      // finishes (or mark it stale for the next visit).
      if (document.getElementById("view-archive").hidden) {
        state.archiveStale = true;
      } else {
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
    case "export_preview_ready":
      // Strip finished generating — re-render so the "generating" placeholder
      // becomes the real hover-scrub filmstrip (has_preview now true).
      if (!document.getElementById("view-archive").hidden) {
        refreshExportJobs();
      }
      break;
    case "export_state":   // pause/resume — reflect the new state live
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
      <strong>${escHtml(info.filename)}</strong>
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
    thumbnails: renderThumbnailsSection,
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
  destroyLocationMaps(pane);
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
  inp.className = "switch";
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
  renderField(pane, "GPS_TRIAGE",
              "GPS triage queued clips before download", checkbox("GPS_TRIAGE"));
  const tnote = document.createElement("p");
  tnote.className = "hint";
  tnote.textContent =
    "Reads each queued clip's GPS track straight off the camera (a few KB per " +
    "clip) before downloading it, so the journey map shows where clips were " +
    "recorded with placeholder thumbnails — letting you pick what to download.";
  pane.appendChild(tnote);
  renderField(pane, "GEOCODE_ENABLED", "Reverse-geocode journey endpoints", checkbox("GEOCODE_ENABLED"));
  renderField(pane, "NOMINATIM_EMAIL", "Contact email for Nominatim (optional)",
              textInput("NOMINATIM_EMAIL"));
  renderField(pane, "DISTANCE_UNITS", "Distance units",
              select("DISTANCE_UNITS", ["km", "miles"]));
  renderLocations(pane);
  renderGpsMaintenance(pane);
}

// Discreet maintenance action: flush stale geofence decisions + the journey
// (route) cache and re-evaluate from existing GPS data. No camera / re-triage.
function renderGpsMaintenance(pane) {
  const h = document.createElement("h3");
  h.textContent = "Maintenance";
  h.style.marginTop = "24px";
  pane.appendChild(h);

  const note = document.createElement("p");
  note.className = "hint";
  note.textContent =
    "Rebuilds location labels and journey grouping from existing GPS data; use " +
    "after changing locations. Nothing is re-downloaded, but clips no longer " +
    "excluded by a location may be queued for download.";
  pane.appendChild(note);

  const btn = document.createElement("button");
  btn.type = "button";
  btn.textContent = "Re-evaluate geofence & journeys";
  btn.addEventListener("click", async () => {
    if (!confirm(
      "Re-evaluate named locations and rebuild journey grouping from existing " +
      "GPS data?\n\nThis may queue some previously-skipped clips for download.",
    )) return;
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = "Re-evaluating…";
    try {
      const r = await api("/api/archive/rebuild-grouping", { method: "POST" });
      toast(`Re-evaluated: ${r.unskipped} un-skipped, ${r.reskipped} re-skipped.`);
    } catch (e) {
      toast(`Re-evaluate failed: ${e.message || e}`, { type: "error" });
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  });
  pane.appendChild(btn);
}

// Reusable single-location editor: map + name + radius + exclude + actions.
// Pure widget — edits flow out through callbacks; owns one Leaflet map that the
// host MUST tear down via the returned destroy() (mirrors destroyJourneyMaps).
// { location, gpsTriageOn, onChange(updated), onSetHome(), onDelete() } -> { el, destroy }
function createLocationEditor({ location, gpsTriageOn, onChange, onSetHome, onDelete }) {
  const loc = { ...location };
  const wrap = document.createElement("div");
  wrap.className = "loc-editor";

  const mapDiv = document.createElement("div");
  mapDiv.className = "loc-map";
  wrap.appendChild(mapDiv);

  // name
  const nameRow = document.createElement("div");
  nameRow.className = "form-row";
  const nameLbl = document.createElement("label");
  nameLbl.textContent = "Name";
  const nameInp = document.createElement("input");
  nameInp.type = "text";
  nameInp.value = loc.name || "";
  nameInp.addEventListener("input", () => {
    loc.name = nameInp.value;
    onChange({ ...loc });
  });
  nameRow.appendChild(nameLbl);
  nameRow.appendChild(nameInp);
  wrap.appendChild(nameRow);

  // radius slider with a live value in the label
  const radRow = document.createElement("div");
  radRow.className = "form-row";
  const radLbl = document.createElement("label");
  const radText = () => `Radius — ${loc.radius_m} m`;
  radLbl.textContent = radText();
  const radInp = document.createElement("input");
  radInp.type = "range";
  radInp.min = 5;
  radInp.max = 500;
  radInp.step = 5;
  radInp.value = loc.radius_m;
  radInp.addEventListener("input", () => {
    loc.radius_m = Number(radInp.value);
    radLbl.textContent = radText();
    if (circle) circle.setRadius(loc.radius_m);
    onChange({ ...loc });
  });
  radRow.appendChild(radLbl);
  radRow.appendChild(radInp);
  wrap.appendChild(radRow);

  // exclude toggle (requires GPS triage)
  if (gpsTriageOn) {
    const exRow = document.createElement("div");
    exRow.className = "form-row";
    const exLbl = document.createElement("label");
    exLbl.textContent = "Exclude recordings made here";
    const exInp = document.createElement("input");
    exInp.type = "checkbox";
    exInp.className = "switch";
    exInp.checked = !!loc.exclude_recordings;
    exInp.addEventListener("change", () => {
      loc.exclude_recordings = exInp.checked;
      onChange({ ...loc });
    });
    exRow.appendChild(exLbl);
    exRow.appendChild(exInp);
    wrap.appendChild(exRow);
  } else {
    const exNote = document.createElement("p");
    exNote.className = "hint";
    exNote.textContent =
      "Enable “GPS triage” above to auto-skip recordings made at this location.";
    wrap.appendChild(exNote);
  }

  // actions
  const actions = document.createElement("div");
  actions.className = "loc-actions";
  const homeBtn = document.createElement("button");
  homeBtn.type = "button";
  homeBtn.className = "btn-secondary loc-home-btn";
  homeBtn.textContent = loc.is_home ? "Current Home" : "Set as Home";
  homeBtn.disabled = !!loc.is_home;
  homeBtn.addEventListener("click", () => onSetHome());
  const delBtn = document.createElement("button");
  delBtn.type = "button";
  delBtn.className = "btn-secondary loc-del-btn";
  delBtn.textContent = "Delete";
  delBtn.addEventListener("click", () => onDelete());
  actions.appendChild(homeBtn);
  actions.appendChild(delBtn);
  wrap.appendChild(actions);

  // Leaflet map: click-to-set / draggable pin + radius circle
  const map = L.map(mapDiv).setView([loc.lat, loc.lon], 16);
  mapDiv._locMap = map;
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "© OpenStreetMap",
  }).addTo(map);
  setTimeout(() => map.invalidateSize(), 0);

  const icon = L.divIcon({
    html: `<span class="home-marker">${loc.is_home ? HOME_GLYPH : PIN_GLYPH}</span>`,
    className: "home-marker-wrap",
    iconSize: [24, 24],
    iconAnchor: [12, 12],
  });
  const marker = L.marker([loc.lat, loc.lon], { draggable: true, icon }).addTo(map);
  let circle = L.circle([loc.lat, loc.lon], {
    radius: loc.radius_m, color: "#3b82f6",
  }).addTo(map);

  function moveTo(lat, lon) {
    loc.lat = lat;
    loc.lon = lon;
    marker.setLatLng([lat, lon]);
    circle.setLatLng([lat, lon]);
    onChange({ ...loc });
  }
  marker.on("dragend", () => {
    const p = marker.getLatLng();
    moveTo(p.lat, p.lng);
  });
  map.on("click", (e) => moveTo(e.latlng.lat, e.latlng.lng));

  return {
    el: wrap,
    destroy() { map.remove(); mapDiv._locMap = null; },
  };
}

// Inline list of named locations (add / edit / set-home / delete). Replaces the
// single "Home location" block. All edits flow into settingsState.pending so the
// existing Save footer persists them; the existing LOCATIONS-change backfill
// (geofence + journey rebuild) runs server-side on save.
function renderLocations(pane) {
  const h = document.createElement("h3");
  h.textContent = "Locations";
  h.style.marginTop = "24px";
  pane.appendChild(h);

  const note = document.createElement("p");
  note.className = "hint";
  note.textContent =
    "Named places used to label and filter journeys and stops. With GPS Triage " +
    "on, a place can also auto-skip recordings made while parked there.";
  pane.appendChild(note);

  const listWrap = document.createElement("div");
  listWrap.className = "loc-list";
  pane.appendChild(listWrap);

  const addBtn = document.createElement("button");
  addBtn.type = "button";
  addBtn.className = "loc-add";
  addBtn.textContent = "+ Add location";
  pane.appendChild(addBtn);

  // Deep working copy; the backend stores it under LOCATIONS.
  let list = JSON.parse(JSON.stringify(valueOf("LOCATIONS") || []));
  let openIdx = -1;
  let editor = null;
  const gpsTriageOn = !!valueOf("GPS_TRIAGE");

  function persist() {
    setPending("LOCATIONS", JSON.parse(JSON.stringify(list)));
  }
  function closeEditor() {
    if (editor) { editor.destroy(); editor = null; }
  }
  function metaText(loc) {
    return `${Number(loc.lat).toFixed(4)}, ${Number(loc.lon).toFixed(4)} · ${loc.radius_m} m`;
  }

  function rerender() {
    closeEditor();
    listWrap.innerHTML = "";
    if (!list.length) {
      const empty = document.createElement("p");
      empty.className = "hint loc-empty";
      empty.textContent = "No locations yet. Add one to label journeys and stops.";
      listWrap.appendChild(empty);
    }
    list.forEach((loc, i) => {
      const row = document.createElement("div");
      row.className = "loc" + (i === openIdx ? " open" : "");

      const head = document.createElement("div");
      head.className = "loc-row";
      head.innerHTML = `
        <span class="loc-ico">${loc.is_home ? HOME_GLYPH : PIN_GLYPH}</span>
        <span class="loc-name">${escHtml(loc.name || "(unnamed)")}</span>
        ${loc.is_home ? '<span class="loc-badge home">Home</span>' : ""}
        ${loc.exclude_recordings ? '<span class="loc-badge excl">Excluded</span>' : ""}
        <span class="loc-meta">${metaText(loc)}</span>
        <span class="loc-caret">${i === openIdx ? "▴" : "▾"}</span>
      `;
      head.addEventListener("click", () => {
        openIdx = (openIdx === i) ? -1 : i;
        rerender();
      });
      row.appendChild(head);

      if (i === openIdx) {
        editor = createLocationEditor({
          location: loc,
          gpsTriageOn,
          onChange: (updated) => {
            list[i] = updated;
            persist();
            // keep the row's name/meta live without tearing down the open map
            head.querySelector(".loc-name").textContent = updated.name || "(unnamed)";
            head.querySelector(".loc-meta").textContent = metaText(updated);
          },
          onSetHome: () => {
            list.forEach((l, j) => { l.is_home = (j === i); });
            persist();
            rerender();
          },
          onDelete: () => {
            const wasHome = list[i].is_home;
            list.splice(i, 1);
            if (wasHome && list.length) list[0].is_home = true;
            openIdx = -1;
            persist();
            rerender();
          },
        });
        row.appendChild(editor.el);
      }
      listWrap.appendChild(row);
    });
  }

  addBtn.addEventListener("click", () => {
    const base = list[0] || { lat: 53.0, lon: -2.0 };
    list.push({
      name: "New location",
      lat: base.lat,
      lon: base.lon,
      radius_m: 30,
      exclude_recordings: false,
      is_home: list.length === 0,  // the very first place added is Home
    });
    openIdx = list.length - 1;
    persist();
    rerender();
  });

  rerender();
}

function renderThumbnailsSection(pane) {
  renderField(pane, "DERIVE_THUMBS_EAGER",
              "Pre-generate thumbnails", checkbox("DERIVE_THUMBS_EAGER"));
  renderField(pane, "DERIVE_FILMSTRIPS_EAGER",
              "Pre-generate timeline filmstrips", checkbox("DERIVE_FILMSTRIPS_EAGER"));
  const note = document.createElement("p");
  note.className = "hint";
  note.textContent =
    "When on, these are built in the background as clips download (and " +
    "existing clips are backfilled), so the archive and timeline feel instant. " +
    "Without, the filmstrips are generated on demand which can be slow on " +
    "low-power hosts.";
  pane.appendChild(note);
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
    <div class="form-row"><label><input type="checkbox" class="switch" id="pw-logout-others" /> Log out other sessions</label></div>
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
      if (body.editable && body.editable.LOCATIONS) {
        state.locations = body.editable.LOCATIONS;
        buildLocationFilter();
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

  // fetch with api()'s session/CSRF semantics but raw Response
  // semantics preserved (the import flow inspects r.ok / r.json()
  // per file). Without this, a session expiring mid-import surfaced
  // as opaque per-file "errors" with no login redirect, and a stale
  // CSRF token failed the whole import with no retry.
  async function ifetch(path, opts = {}) {
    const send = () => fetch(path, {
      ...opts,
      headers: { ...(opts.headers || {}), ...csrfH() },
      credentials: "same-origin",
    });
    let r = await send();
    if (r.status === 403) {
      const cr = await fetch("/api/auth/csrf", { credentials: "same-origin" });
      if (cr.ok) state.csrf = (await cr.json()).csrf;
      r = await send();
    }
    if (r.status === 401) {
      showLogin();
      throw new Error("session expired");
    }
    return r;
  }

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

    // Ask the server which clips are already in the archive and drop them
    // up front, so they're never re-uploaded.
    let queue = picked;
    try {
      $("import-status").textContent = "Checking for clips already imported…";
      const r = await ifetch("/api/import/present", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          files: picked.map((p) => ({ name: p.file.name, size: p.file.size })),
        }),
      });
      if (r.ok) {
        const body = await r.json();
        const present = new Set(body.present || []);
        const skipped = new Set(body.skipped || []);
        queue = picked.filter(
          (p) => !present.has(p.file.name) && !skipped.has(p.file.name),
        );
        if (present.size) tally.already_present = present.size;
        if (skipped.size) tally.skipped = skipped.size;
      }
    } catch (_) { /* fall back to uploading everything */ }

    for (let i = 0; i < queue.length; i++) {
      const { file, path } = queue[i];
      $("import-status").textContent = `Uploading ${file.name} (${i + 1}/${queue.length})`;
      $("import-bar").style.width = `${(i / queue.length) * 100}%`;
      let res;
      try {
        const r = await ifetch("/api/import/upload", {
          method: "POST",
          credentials: "same-origin",
          headers: {
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
    await ifetch("/api/archive/rescan", { method: "POST" });
    renderSummary(tally);
  });

  // --- Folder tab ---
  $("import-folder-scan").addEventListener("click", async () => {
    const path = $("import-folder-path").value.trim() || null;
    const r = await ifetch("/api/import/scan", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    if (!r.ok) {
      $("import-folder-manifest").textContent = `Error: ${(await r.json()).detail || r.status}`;
      hide($("import-folder-go"));
      return;
    }
    const m = await r.json();
    const newCount = m.recognised.length - (m.present_count || 0);
    const dupNote = m.present_count
      ? `${newCount} new, ${m.present_count} already in archive, `
      : `${m.recognised.length} clip(s), `;
    $("import-folder-manifest").textContent =
      `${dupNote}${m.skipped_count} skipped, ` +
      `${(m.total_bytes / 1e9).toFixed(2)} GB${m.cross_volume ? " (external — copy)" : ""}.`;
    $("import-folder-go").dataset.path = path || "";
    show($("import-folder-go"));
  });

  $("import-folder-go").addEventListener("click", async (e) => {
    const path = e.target.dataset.path || null;
    show($("import-progress")); hide($("import-summary"));
    $("import-status").textContent = "Starting…";
    const r = await ifetch("/api/import/ingest", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
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
      `skipped ${t.skipped || 0}, over quota ${t.over_quota_older || 0}, ` +
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

// ---------- Tooltips ----------
// Reusable, app-wide tooltip. Any element with a `data-tip="…"` attribute
// shows it on hover, keyboard focus, and tap/click — covering the gaps in
// native `title` (no click, nothing on touch, unreliable timing). One shared
// element lives on <body> and is fixed-positioned from the trigger's rect, so
// it escapes overflow:hidden containers and flips/clamps near the viewport
// edges. Wiring is delegated on `document`, so dynamically-rendered content
// works with no per-element setup.
(() => {
  let tip = null;
  let current = null;

  function el() {
    if (!tip) {
      tip = document.createElement("div");
      tip.className = "tooltip";
      tip.setAttribute("role", "tooltip");
      tip.hidden = true;
      document.body.appendChild(tip);
    }
    return tip;
  }

  function show(target) {
    const text = target.getAttribute("data-tip");
    if (!text) return;
    current = target;
    const t = el();
    t.textContent = text;
    t.hidden = false;
    position(target, t);
  }

  function position(target, t) {
    const r = target.getBoundingClientRect();
    const tr = t.getBoundingClientRect();
    const gap = 8;
    let placement = "top";
    let top = r.top - tr.height - gap;
    if (top < gap) { top = r.bottom + gap; placement = "bottom"; }
    let left = r.left + r.width / 2 - tr.width / 2;
    left = Math.max(gap, Math.min(left, window.innerWidth - tr.width - gap));
    t.style.left = `${Math.round(left)}px`;
    t.style.top = `${Math.round(top)}px`;
    t.dataset.placement = placement;
    // Keep the arrow pointing at the trigger even after horizontal clamping.
    const arrow = Math.max(12, Math.min(tr.width - 12,
      r.left + r.width / 2 - left));
    t.style.setProperty("--tip-arrow", `${Math.round(arrow)}px`);
  }

  function hide() {
    if (tip) tip.hidden = true;
    current = null;
  }

  document.addEventListener("mouseover", (e) => {
    const t = e.target.closest?.("[data-tip]");
    if (t) show(t);
  });
  document.addEventListener("mouseout", (e) => {
    const t = e.target.closest?.("[data-tip]");
    if (t && t === current && !t.contains(e.relatedTarget)) hide();
  });
  document.addEventListener("focusin", (e) => {
    const t = e.target.closest?.("[data-tip]");
    if (t) show(t);
  });
  document.addEventListener("focusout", hide);
  // Tap/click shows (re-shows) on the target, dismisses anywhere else. Not a
  // toggle, so a touch's synthetic mouseover+click doesn't flash-and-hide.
  document.addEventListener("click", (e) => {
    const t = e.target.closest?.("[data-tip]");
    if (t) show(t); else hide();
  });
  window.addEventListener("scroll", hide, true);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") hide();
  });
})();

// ---------- Camera control ----------
// Reads/writes live dashcam settings via /api/camera/*. The server enforces
// the destructive-command denylist and per-value validation; the UI only ever
// offers the safely-writable enumerated settings the catalog returns.

function cameraCategory(key) {
  if (!key) return "Other";
  if (/RECORD|MOVIE|HDR|EXPOSURE|BITRATE|RESOLUTION|G_SENSOR|TIME_LAPSE|LIVEVIEW|IMAGE_|MIRROR|ROTATION|FISH_EYE|VIDEO_SOURCE|VIDEO_MERGE/.test(key))
    return "Recording & Image";
  if (/PARKING/.test(key)) return "Parking";
  if (/WATERMARK|PLATE|STAMP/.test(key)) return "Watermark";
  if (/WIFI|FREQUENCY|STA_/.test(key)) return "Wi-Fi & Network";
  return "System";
}

function settingLabel(key) {
  if (!key) return "(unknown)";
  return key.replace(/^CMD_/, "").replace(/_/g, " ").toLowerCase()
            .replace(/\b\w/g, (c) => c.toUpperCase());
}

function cameraErr(e) {
  const m = String((e && e.message) || e || "");
  if (m.startsWith("409"))
    return "No camera address configured — set it in Settings, then Refresh.";
  if (m.startsWith("502"))
    return "Camera unreachable — make sure it's powered and on Wi-Fi, then Refresh.";
  if (m.startsWith("403")) return "Refused (protected setting).";
  return "Error: " + m;
}

// Disable/enable every live control in the settings table. Controls stay
// disabled until a fetch confirms the camera is actually reachable, so we
// never let the user write a setting against stale/unconfirmed state.
function setCamControlsDisabled(disabled) {
  document.querySelectorAll("#camera-settings [data-cam-control]")
    .forEach((c) => { c.disabled = disabled; });
}

// Opening the tab renders the last-known snapshot instantly (controls
// disabled) and refreshes in the background; controls re-enable only once
// the camera answers.
async function loadCamera() {
  const statusEl = document.getElementById("camera-status");
  const infoEl = document.getElementById("camera-info");
  const setEl = document.getElementById("camera-settings");
  if (!statusEl) return;
  if (state.camera) {
    renderCameraInfo(infoEl, state.camera.info, state.camera.cat);
    renderCameraSettings(setEl, state.camera.cat);
    setCamControlsDisabled(true);
    statusEl.className = "camera-status";
    statusEl.textContent = "Refreshing from camera…";
  } else {
    statusEl.className = "camera-status";
    statusEl.textContent = "Loading camera…";
    infoEl.innerHTML = "";
    setEl.innerHTML =
      `<div class="cam-loading">Reading settings from the camera…</div>`;
  }
  await refreshCamera();
}

async function refreshCamera() {
  const statusEl = document.getElementById("camera-status");
  const infoEl = document.getElementById("camera-info");
  const setEl = document.getElementById("camera-settings");
  if (!statusEl) return;
  const refreshBtn = document.getElementById("camera-refresh");
  if (refreshBtn) refreshBtn.disabled = true;
  setCamControlsDisabled(true);

  let info, cat;
  try {
    info = await api("/api/camera/info");
    cat = await api("/api/camera/settings/catalog");
  } catch (e) {
    // Keep showing the cached snapshot (controls stay disabled) rather than
    // blanking the page when the camera is briefly unreachable.
    statusEl.className = "camera-status error";
    statusEl.textContent = state.camera
      ? cameraErr(e) + " Showing last-known settings."
      : cameraErr(e);
    if (!state.camera) setEl.innerHTML = "";
    if (refreshBtn) refreshBtn.disabled = false;
    return;
  }
  state.camera = { info, cat };
  renderCameraInfo(infoEl, info, cat);
  const adjustable = cat.settings.filter((s) => s.supported !== false).length;
  statusEl.className = "camera-status";
  statusEl.textContent =
    `${cat.model} · ${adjustable} of ${cat.settings.length} settings adjustable now`;
  // A fresh render replaces the disabled cached controls with live ones.
  renderCameraSettings(setEl, cat);
  if (refreshBtn) refreshBtn.disabled = false;
}

function renderCameraInfo(el, info, cat) {
  const free = info.free_space_bytes != null
    ? (info.free_space_bytes / (1 << 30)).toFixed(1) + " GB free"
    : "—";
  const s = info.sensors || {};
  const lenses = s.total ? `${s.total} (${info.camera_tag || "?"})` : "—";
  const rec = cat ? cat.recording : null;
  const recHtml = rec == null ? "—"
    : `<span class="cam-pill ${rec ? "on" : "off"}">` +
      `${rec ? "● Recording" : "■ Stopped"}</span>`;
  el.innerHTML = `
    <div class="cam-facts">
      <div><span>Firmware</span><b>${escHtml(info.firmware || "—")}</b></div>
      <div><span>Status</span><b>${recHtml}</b></div>
      <div><span>SD card</span><b>${escHtml(info.card_status_label || "—")}</b></div>
      <div><span>Storage</span><b>${escHtml(free)}</b></div>
      <div><span>Lenses</span><b>${escHtml(lenses)}</b></div>
    </div>`;
}

// A 2-option OFF/ON setting → render a toggle. Returns {off,on} indices or null.
function camBool(s) {
  if (!s.options || s.options.length !== 2) return null;
  const off = s.options.find((o) => String(o.value).toUpperCase() === "OFF");
  const on = s.options.find((o) => String(o.value).toUpperCase() === "ON");
  return off && on ? { off: off.index, on: on.index } : null;
}

function renderCamRow(s) {
  const id = `cam-${s.cmd}`;
  const label = escHtml(settingLabel(s.key));
  const status = `<span class="cam-row-status" id="cam-status-${s.cmd}"></span>`;

  // Row order is label | status | control: the status sits right after the
  // label so the control is always the last column and right-aligned (no
  // empty gap on the right when the status is blank).
  if (s.supported === false) {
    const cur = s.current_label != null ? escHtml(s.current_label) : "—";
    const reason = escHtml(s.unsupported_reason || "Not adjustable now");
    // A small ⓘ marker after the label; the reason is revealed by the
    // data-tip tooltip (hover/focus/tap). The value stays right-aligned.
    return `<div class="cam-row disabled">
        <label>${label}</label>
        <span class="cam-note" tabindex="0" data-tip="${reason}"
              aria-label="${reason}">ⓘ</span>
        <span class="cam-current">${cur}</span>
      </div>`;
  }

  const b = camBool(s);
  if (b) {
    const on = s.current === b.on;
    return `<div class="cam-row">
        <label for="${id}">${label}</label>
        ${status}
        <input type="checkbox" class="switch" id="${id}" data-cam-control
               data-key="${escHtml(s.key)}" data-cmd="${s.cmd}"
               data-on="${b.on}" data-off="${b.off}"${on ? " checked" : ""} />
      </div>`;
  }

  const opts = s.options.map((o) =>
    `<option value="${o.index}"${o.index === s.current ? " selected" : ""}>` +
    `${escHtml(o.value)}</option>`).join("");
  return `<div class="cam-row">
      <label for="${id}">${label}</label>
      ${status}
      <select id="${id}" data-cam-control data-key="${escHtml(s.key)}" data-cmd="${s.cmd}">${opts}</select>
    </div>`;
}

function renderCameraSettings(el, cat) {
  const groups = {};
  for (const s of cat.settings) {
    const g = cameraCategory(s.key);
    (groups[g] = groups[g] || []).push(s);
  }
  const order = ["Recording & Image", "Parking", "Watermark",
                 "Wi-Fi & Network", "System", "Other"];
  let html = "";
  for (const g of order) {
    const items = groups[g];
    if (!items) continue;
    html += `<fieldset class="cam-group"><legend>${escHtml(g)}</legend>`;
    for (const s of items) html += renderCamRow(s);
    html += `</fieldset>`;
  }
  el.innerHTML = html || "<p>No adjustable settings reported.</p>";
  el.querySelectorAll("[data-cam-control]").forEach((ctrl) => {
    ctrl.dataset.prev = ctrl.type === "checkbox"
      ? (ctrl.checked ? "1" : "0") : ctrl.value;
    ctrl.addEventListener("change", onCameraSettingChange);
  });
}

function revertCamControl(ctrl) {
  if (ctrl.type === "checkbox") ctrl.checked = ctrl.dataset.prev === "1";
  else ctrl.value = ctrl.dataset.prev;
}

async function onCameraSettingChange(e) {
  const ctrl = e.target;
  const { key, cmd } = ctrl.dataset;
  const isToggle = ctrl.type === "checkbox";
  const value = isToggle
    ? (ctrl.checked ? ctrl.dataset.on : ctrl.dataset.off) : ctrl.value;
  const st = document.getElementById("cam-status-" + cmd);
  ctrl.disabled = true;
  st.className = "cam-row-status pending";
  st.textContent = "Applying…";
  try {
    const r = await api(`/api/camera/settings/${encodeURIComponent(key)}`, {
      method: "POST",
      body: JSON.stringify({ value }),
    });
    if (r.verified === true) {
      st.className = "cam-row-status ok";
      st.textContent = r.record_cycled ? "✓ Saved (paused recording)" : "✓ Saved";
      ctrl.dataset.prev = isToggle ? (ctrl.checked ? "1" : "0") : value;
      setTimeout(() => {
        if (st.classList.contains("ok")) st.textContent = "";
      }, 2500);
    } else {
      st.className = "cam-row-status error";
      st.textContent = "Camera didn't accept this";
      revertCamControl(ctrl);
    }
  } catch (err) {
    st.className = "cam-row-status error";
    st.textContent = cameraErr(err);
    revertCamControl(ctrl);
  } finally {
    ctrl.disabled = false;
  }
}

document.getElementById("camera-refresh")
  ?.addEventListener("click", refreshCamera);
