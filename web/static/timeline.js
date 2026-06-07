// Viofosync timeline view — read-only scrubbable journey browser.
// Self-contained module exposed as window.Timeline. Reuses the
// page-global api() and cssVar() helpers from app.js.
(() => {
  "use strict";

  const state = {
    date: null,
    journeyIdx: null,
    data: null,            // /api/archive/timeline payload
    channels: [],          // [{key,label}]
    clipsByChannel: {},    // key -> [{id,start_ts,duration_s}] sorted
    view: { pxPerSecond: 0, originTs: 0 },
    playheadTs: 0,
    onSeek: null,          // hook set by later features
    onPlayheadMove: null,
    previewChannel: null,  // which channel the preview shows
    playing: false,
    videoEl: null,
    videoChannel: null,    // channel the current preview <video> belongs to
    // ---- editor (Phase 3B) ----
    inTs: null,
    outTs: null,
    segments: [],          // [{start, end, camera}] tiling [inTs, outTs]
    disabledChannels: null,// Set of channel keys excluded from cut/export
  };
  let transportWired = false;
  let editorWired = false;

  function el(id) { return document.getElementById(id); }
  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

  // ---- data load ----
  async function open(date, journeyIdx) {
    state.date = date;
    state.journeyIdx = journeyIdx;
    state.view = { pxPerSecond: 0, originTs: 0 };
    const q = journeyIdx != null
      ? `?date=${date}&journey=${journeyIdx}`
      : `?date=${date}`;
    let data;
    try {
      data = await api(`/api/archive/timeline${q}`);
    } catch (err) {
      el("tl-title").textContent = "Timeline — failed to load";
      console.error("timeline load failed", err);
      return;
    }
    state.data = data;
    state.channels = data.channels || [];
    state.clipsByChannel = {};
    for (const ch of state.channels) state.clipsByChannel[ch.key] = [];
    for (const c of (data.clips || [])) {
      (state.clipsByChannel[c.channel] ||= []).push(c);
    }
    for (const k of Object.keys(state.clipsByChannel)) {
      state.clipsByChannel[k].sort((a, b) => a.start_ts - b.start_ts);
    }
    renderTitle();
    initSelection();
    render();
    wireGestures();
    wireTransport();
    wireEditor();
    state.previewChannel = null;
    state.playing = false;
    state.videoEl = null;
    state.onSeek = (ts) => {
      loadPreviewAt(ts);
      if (state._mapMove) state._mapMove(ts);
    };
    state.onPlayheadMove = (ts) => {
      if (state._mapMove) state._mapMove(ts);
    };
    loadPreviewAt(state.playheadTs);
    updateTransport();
    renderMap();
    wireMapToggle();
  }

  function renderTitle() {
    const d = state.data;
    let title = state.date;
    const j = d.gps && d.gps.journeys && d.journey != null
      ? d.gps.journeys[d.journey] : null;
    if (j) {
      const s = new Date(j.start_ts * 1000).toLocaleTimeString();
      const e = new Date(j.end_ts * 1000).toLocaleTimeString();
      const place = (j.start_label && j.end_label)
        ? ` · ${j.start_label} → ${j.end_label}` : "";
      title = `${state.date} · ${s} – ${e}${place}`;
    }
    el("tl-title").textContent = title;
  }

  // ---- pure time<->pixel model ----
  function timeToX(ts) {
    return (ts - state.view.originTs) * state.view.pxPerSecond;
  }
  function xToTime(x) {
    return state.view.originTs + x / state.view.pxPerSecond;
  }
  function bounds() {
    const b = (state.data && state.data.bounds) || {};
    return { start: b.start_ts ?? 0, end: b.end_ts ?? 0 };
  }
  // Which clip in a channel contains ts, plus the offset into it.
  function clipAt(channelKey, ts) {
    const list = state.clipsByChannel[channelKey] || [];
    for (const c of list) {
      if (ts >= c.start_ts && ts < c.start_ts + (c.duration_s || 0)) {
        return { clip: c, offset: ts - c.start_ts };
      }
    }
    return null;
  }
  function trackWidthPx() {
    const strip = document.querySelector("#tl-tracks .tl-strip");
    if (strip) return strip.clientWidth || 800;
    // Before tracks exist, estimate from the host minus the label gutter.
    return Math.max(200, (el("tl-tracks").clientWidth || 800) - 64);
  }
  // Fit the whole span to the track width as the default zoom.
  function resetZoom() {
    const { start, end } = bounds();
    const span = Math.max(1, end - start);
    state.view.originTs = start;
    state.view.pxPerSecond = trackWidthPx() / span;
    if (!state.playheadTs) state.playheadTs = start;
  }

  // ---- rendering ----
  function render() {
    if (!state.data) return;
    renderRuler();
    renderTracks();           // creates the strips
    if (!state.view.pxPerSecond) resetZoom();
    layoutClipBlocks();       // now measure against real strip width
    renderPlayhead();
    if (state.inTs != null) renderEditor();
  }

  function renderRuler() {
    const ruler = el("tl-ruler");
    ruler.innerHTML = "";
    const { start, end } = bounds();
    const span = Math.max(1, end - start);
    // Tick density scales with width so the HH:MM:SS labels never collide
    // (~110px per label) — ~3 on a phone, ~6 on desktop.
    const w = ruler.clientWidth || trackWidthPx();
    const n = Math.max(2, Math.min(6, Math.floor(w / 110)));
    for (let i = 0; i <= n; i++) {
      const ts = start + (span * i) / n;
      const tick = document.createElement("span");
      tick.className = "tl-tick";
      tick.style.left = `${(i / n) * 100}%`;
      tick.textContent = new Date(ts * 1000).toLocaleTimeString();
      ruler.appendChild(tick);
    }
  }

  function renderTracks() {
    const host = el("tl-tracks");
    // Preserve the playhead element across re-renders.
    const ph = el("tl-playhead");
    host.innerHTML = "";
    state.trackEls = {};
    if (!state.channels.length) {
      host.innerHTML = `<p class="tl-empty">No clips for this ${
        state.journeyIdx != null ? "journey" : "day"}.</p>`;
      return;
    }
    for (const ch of state.channels) {
      const row = document.createElement("div");
      row.className = "tl-track";
      const dis = (state.disabledChannels || new Set()).has(ch.key);
      row.innerHTML =
        `<div class="tl-track-head">` +
        `<span class="tl-track-toggle ${dis ? "" : "on"}" data-ch="${ch.key}"` +
        ` role="button" tabindex="0" title="Enable/disable this camera"></span>` +
        `<span class="tl-track-label">${ch.label}</span>` +
        `</div>` +
        `<div class="tl-strip" data-channel="${ch.key}"></div>`;
      const strip = row.querySelector(".tl-strip");
      for (const c of (state.clipsByChannel[ch.key] || [])) {
        strip.appendChild(renderClipBlock(c));
      }
      host.appendChild(row);
      state.trackEls[ch.key] = strip;
    }
    if (ph) host.appendChild(ph);
  }

  function renderClipBlock(c) {
    const block = document.createElement("div");
    block.className = "tl-clip";
    block.dataset.clipId = c.id;
    block.dataset.startTs = c.start_ts;
    block.dataset.durationS = c.duration_s || 0;
    block._loaded = false;
    return block;
  }

  // Position every clip block by time and lazy-load its filmstrip.
  function layoutClipBlocks() {
    document.querySelectorAll("#tl-tracks .tl-clip").forEach((block) => {
      const ts = Number(block.dataset.startTs);
      const dur = Number(block.dataset.durationS);
      const x = timeToX(ts);
      const w = Math.max(1, dur * state.view.pxPerSecond);
      block.style.transform = `translateX(${x}px)`;
      block.style.width = `${w}px`;
      maybeLoadFilmstrip(block, x, w);
    });
    if (state.inTs != null) { renderSelection(); renderShading(); } // keep aligned
  }

  async function maybeLoadFilmstrip(block, x, widthPx) {
    if (block._loaded || widthPx < 8) return;
    const vw = trackWidthPx();
    if (x > vw + 400 || x + widthPx < -400) return; // off-screen
    block._loaded = true;
    try {
      const meta = await api(
        `/api/archive/clip/${block.dataset.clipId}/filmstrip`);
      if (meta && meta.sprite_url) {
        // Stretch the whole N-frame sprite across the block so frame
        // position maps to time (scrub-accurate) and zooming in
        // spreads the frames out into a readable filmstrip.
        block.style.backgroundImage = `url(${meta.sprite_url})`;
        block.style.backgroundSize = "100% 100%";
        block.classList.add("has-strip");
      }
    } catch (err) {
      block._loaded = false; // allow retry on next layout
    }
  }

  function renderPlayhead() {
    let ph = el("tl-playhead");
    if (!ph) {
      ph = document.createElement("div");
      ph.className = "tl-playhead";
      ph.id = "tl-playhead";
      el("tl-tracks").appendChild(ph);
    }
    ph.style.transform = `translateX(${timeToX(state.playheadTs)}px)`;
  }

  // ---- seek (preview/map hooks attach in later features) ----
  function seekToTs(ts) {
    state.playheadTs = clamp(ts, bounds().start, bounds().end);
    renderPlayhead();
    if (state.onSeek) state.onSeek(state.playheadTs);
  }

  // ---- gestures (Pointer Events: one path for mouse + touch + pen) ----
  const pointers = new Map();   // pointerId -> clientX
  let drag = null;              // { mode:'scrub'|'pan', startX, lastX, moved }
  let pinchPrev = 0;
  let gesturesWired = false;

  function zoomLimits() {
    const { start, end } = bounds();
    return { min: trackWidthPx() / Math.max(1, end - start), max: 40 };
  }
  function clampOrigin() {
    const { start, end } = bounds();
    const visible = trackWidthPx() / state.view.pxPerSecond;
    state.view.originTs = clamp(
      state.view.originTs, start, Math.max(start, end - visible));
  }
  function setZoom(pps, focalTs) {
    const { min, max } = zoomLimits();
    const next = clamp(pps, min, max);
    const focalX = timeToX(focalTs);
    state.view.pxPerSecond = next;
    state.view.originTs = focalTs - focalX / next;
    clampOrigin();
    layoutClipBlocks();
    renderPlayhead();
  }
  function zoomBy(factor, focalTs) {
    setZoom(state.view.pxPerSecond * factor, focalTs);
  }
  function panByPx(dx) {
    state.view.originTs -= dx / state.view.pxPerSecond;
    clampOrigin();
    layoutClipBlocks();
    renderPlayhead();
  }
  // Client x -> time, using the strip's left edge as the origin.
  function localX(clientX) {
    const strip = document.querySelector("#tl-tracks .tl-strip");
    const rect = (strip || el("tl-tracks")).getBoundingClientRect();
    return clientX - rect.left;
  }

  function wireGestures() {
    if (gesturesWired) return;
    gesturesWired = true;
    const tracks = el("tl-tracks");
    tracks.addEventListener("pointerdown", (e) => {
      // Toggle gutter: do NOT capture the pointer — capture retargets the
      // subsequent click to #tl-tracks, so the toggle's own click never
      // fires. Returning here leaves the native click intact.
      if (e.target.closest(".tl-track-head")) return;
      const grip = e.target.closest(".tl-switch-grip");
      try { tracks.setPointerCapture(e.pointerId); } catch { /* non-active pointer */ }
      pointers.set(e.pointerId, e.clientX);
      if (pointers.size === 2) {
        const xs = [...pointers.values()];
        pinchPrev = Math.abs(xs[0] - xs[1]);
        drag = null;
        return;
      }
      if (grip) {                       // drag a switch marker's boundary
        drag = { mode: "switch", idx: Number(grip.parentElement.dataset.idx) };
        return;
      }
      const onPlayhead = e.target.id === "tl-playhead";
      drag = { mode: onPlayhead ? "scrub" : "pan",
               startX: e.clientX, lastX: e.clientX, moved: false };
      if (onPlayhead) seekToTs(xToTime(localX(e.clientX)));
    });
    tracks.addEventListener("pointermove", (e) => {
      if (!pointers.has(e.pointerId)) return;
      pointers.set(e.pointerId, e.clientX);
      if (pointers.size === 2) {
        const xs = [...pointers.values()];
        const dist = Math.abs(xs[0] - xs[1]) || 1;
        const mid = (xs[0] + xs[1]) / 2;
        if (pinchPrev > 0) zoomBy(dist / pinchPrev, xToTime(localX(mid)));
        pinchPrev = dist;
        return;
      }
      if (!drag) return;
      if (drag.mode === "switch") {
        const i = drag.idx;
        const lo = state.segments[i - 1].start + 0.2;
        const hi = state.segments[i].end - 0.2;
        const t = clamp(xToTime(localX(e.clientX)), lo, hi);
        state.segments[i - 1].end = t;
        state.segments[i].start = t;
        renderEditor();
        return;
      }
      if (Math.abs(e.clientX - drag.startX) > 3) drag.moved = true;
      if (drag.mode === "scrub") {
        seekToTs(xToTime(localX(e.clientX)));
      } else if (drag.moved) {
        panByPx(e.clientX - drag.lastX);
      }
      drag.lastX = e.clientX;
    });
    const up = (e) => {
      if (drag && drag.mode === "pan" && !drag.moved) {
        seekToTs(xToTime(localX(e.clientX)));   // a tap = seek
      }
      pointers.delete(e.pointerId);
      if (pointers.size < 2) pinchPrev = 0;
      if (pointers.size === 0) drag = null;
    };
    tracks.addEventListener("pointerup", up);
    tracks.addEventListener("pointercancel", up);
    tracks.addEventListener("wheel", (e) => {
      e.preventDefault();
      zoomBy(e.deltaY < 0 ? 1.15 : 1 / 1.15, xToTime(localX(e.clientX)));
    }, { passive: false });
    el("tl-zoom-in").addEventListener("click",
      () => zoomBy(1.4, state.playheadTs));
    el("tl-zoom-out").addEventListener("click",
      () => zoomBy(1 / 1.4, state.playheadTs));
  }

  // ---- preview playback + transport ----
  function previewChannelKey() {
    // Editor: the preview follows the active segment's camera.
    const a = activeChannelAt(state.playheadTs);
    if (a && (state.clipsByChannel[a] || []).length) return a;
    // Fallback: first enabled channel that has clips.
    const ch = enabledChannels().find(
      (c) => (state.clipsByChannel[c.key] || []).length)
      || state.channels.find((c) => (state.clipsByChannel[c.key] || []).length);
    return ch ? ch.key : null;
  }

  function loadPreviewAt(ts, { autoplay = false } = {}) {
    const key = previewChannelKey();
    const host = el("tl-preview");
    if (!key) return;
    state.videoChannel = key;
    const hit = clipAt(key, ts);
    if (!hit) {
      host.innerHTML =
        `<div class="tl-preview-empty">No ${key} footage here</div>`;
      state.videoEl = null;
      return;
    }
    const cur = state.videoEl;
    if (cur && Number(cur.dataset.clipId) === hit.clip.id) {
      cur.currentTime = hit.offset;
      if (autoplay) cur.play().catch(() => {});
      return;
    }
    host.innerHTML = "";
    const video = document.createElement("video");
    video.dataset.clipId = hit.clip.id;
    video.src = `/api/archive/clip/${hit.clip.id}/video`;
    video.controls = false;
    video.playsInline = true;
    // A fresh <video> emits a timeupdate at currentTime≈0 *before* its
    // seek runs; deriving the playhead from it lands at the clip's start
    // (maybe in another segment) and ping-pongs the camera reload. Mark
    // it not-ready until the seek-to-offset lands; onTimeUpdate ignores
    // updates until then. (offset≈0 needs no seek, so it's ready at once.)
    video._ready = hit.offset <= 0.1;
    const seek = () => {
      if (hit.offset > 0.1) {
        video.addEventListener("seeked",
          () => { video._ready = true; }, { once: true });
        video.currentTime = hit.offset;
      } else {
        video._ready = true;
      }
      // play() rejects on rapid reseek (AbortError) / autoplay policy;
      // swallow it so it doesn't surface as an uncaught rejection.
      if (autoplay) video.play().catch(() => {});
    };
    video.addEventListener("loadedmetadata", seek, { once: true });
    video.addEventListener("timeupdate", onTimeUpdate);
    video.addEventListener("ended", onClipEnded);
    host.appendChild(video);
    state.videoEl = video;
  }

  function onTimeUpdate() {
    const v = state.videoEl;
    if (!v || !v.dataset.clipId) return;
    // Ignore updates while the clip is still seeking to its target offset
    // (incl. the transient currentTime≈0 a fresh <video> fires before its
    // seek). Acting on those lands the playhead at the clip's start, which
    // may be in another segment, and ping-pongs the camera reload below.
    if (v.seeking || !v._ready) return;
    const ch = state.videoChannel;
    const clip = (state.clipsByChannel[ch] || [])
      .find((c) => c.id === Number(v.dataset.clipId));
    if (!clip) return;
    state.playheadTs = clip.start_ts + v.currentTime;
    // Bounded playback: stop at the selection end, never play past it.
    if (state.inTs != null && state.playheadTs >= state.outTs) {
      v.pause();
      state.playing = false;
      state.playheadTs = state.outTs;
      renderPlayhead();
      renderActive();
      updateTransport();
      if (state.onPlayheadMove) state.onPlayheadMove(state.playheadTs);
      return;
    }
    renderPlayhead();
    renderActive();
    updateTransport();
    if (state.onPlayheadMove) state.onPlayheadMove(state.playheadTs);
    // Crossing a switch marker during playback flips the camera.
    const active = activeChannelAt(state.playheadTs);
    if (active && active !== ch && (state.clipsByChannel[active] || []).length) {
      loadPreviewAt(state.playheadTs, { autoplay: state.playing });
    }
  }

  function onClipEnded() {
    const key = previewChannelKey();
    const list = state.clipsByChannel[key] || [];
    const v = state.videoEl;
    const i = list.findIndex((c) => c.id === Number(v && v.dataset.clipId));
    const next = list[i + 1];
    // Don't advance into a clip that begins beyond the selection end.
    if (next && state.playing &&
        (state.inTs == null || next.start_ts < state.outTs)) {
      loadPreviewAt(next.start_ts + 0.01, { autoplay: true });
    } else {
      state.playing = false;
      updateTransport();
    }
  }

  function togglePlay() {
    const v = state.videoEl;
    // Pausing.
    if (v && !v.paused) {
      v.pause();
      state.playing = false;
      updateTransport();
      return;
    }
    // Starting playback. Only ever play within the selection: if the
    // playhead sits outside [inTs, outTs], jump to the start first so we
    // never play footage outside the defined area. (The playhead can still
    // be scrubbed outside while paused, to set a new start/end there.)
    if (state.inTs != null &&
        (state.playheadTs < state.inTs || state.playheadTs >= state.outTs)) {
      state.playheadTs = state.inTs;
      renderPlayhead();
      if (state.onPlayheadMove) state.onPlayheadMove(state.playheadTs);
      state.playing = true;
      loadPreviewAt(state.playheadTs, { autoplay: true });
      updateTransport();
      return;
    }
    // Inside the selection: resume from the current position.
    state.playing = true;
    if (!v) {
      loadPreviewAt(state.playheadTs, { autoplay: true });
    } else {
      v.play().catch(() => {});
    }
    updateTransport();
  }

  function fmtClock(s) {
    s = Math.max(0, Math.floor(s));
    const m = Math.floor(s / 60);
    return `${String(m).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  }

  function updateTransport() {
    const { start, end } = bounds();
    el("tl-tc").textContent =
      `${fmtClock(state.playheadTs - start)} / ${fmtClock(end - start)}`;
    el("tl-play").textContent = state.playing ? "⏸" : "▶";
    // The camera button shows the active segment's camera and cycles it.
    const camBtn = el("tl-cam");
    const active = activeChannelAt(state.playheadTs);
    if (active && enabledChannels().length > 1) {
      camBtn.hidden = false;
      camBtn.textContent = "⟳ " +
        ((state.channels.find((c) => c.key === active) || {}).label || active);
    } else {
      camBtn.hidden = true;
    }
  }

  function wireTransport() {
    if (transportWired) return;
    transportWired = true;
    el("tl-play").addEventListener("click", togglePlay);
    el("tl-cam").addEventListener("click", () => {
      cycleSegmentCamera(segAt(state.playheadTs));
    });
  }

  // ---- journey map (beside the preview) ----
  let mapWired = false;

  function journeyForMap() {
    const d = state.data;
    if (!d.gps || !d.gps.journeys || !d.gps.journeys.length) return null;
    if (d.journey != null && d.gps.journeys[d.journey]) {
      return d.gps.journeys[d.journey];
    }
    return d.gps.journeys[0];
  }

  function renderMap() {
    const host = el("tl-map");
    const stage = el("tl-stage");
    state._mapMove = null;
    if (state._map) { state._map.remove(); state._map = null; }
    const j = journeyForMap();
    if (!j) { stage.classList.add("no-map"); host.innerHTML = ""; return; }
    stage.classList.remove("no-map");
    host.innerHTML = "";
    const map = L.map(host, { attributionControl: false });
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
      { attribution: "© OpenStreetMap" }).addTo(map);
    const coords = j.geojson.geometry.coordinates.map(([lon, lat]) => [lat, lon]);
    const times = j.times || [];
    const line = L.polyline(coords,
      { color: cssVar("--accent"), weight: 5 }).addTo(map);
    map.fitBounds(line.getBounds(), { padding: [18, 18] });

    let marker = null;
    state._mapMove = (ts) => {
      if (!times.length) return;
      let best = 0, bd = Infinity;
      for (let i = 0; i < times.length; i++) {
        const d = Math.abs(times[i] - ts);
        if (d < bd) { bd = d; best = i; }
      }
      if (marker) marker.remove();
      marker = L.circleMarker(coords[best], {
        radius: 7, color: cssVar("--err-text"),
        fillColor: cssVar("--err-text"), fillOpacity: 1,
      }).addTo(map);
    };
    line.on("click", (ev) => {
      let best = 0, bd = Infinity;
      for (let i = 0; i < coords.length; i++) {
        const dx = coords[i][0] - ev.latlng.lat;
        const dy = coords[i][1] - ev.latlng.lng;
        const d = dx * dx + dy * dy;
        if (d < bd) { bd = d; best = i; }
      }
      if (times[best] != null) seekToTs(times[best]);
    });
    state._map = map;
    state._mapMove(state.playheadTs);
    requestAnimationFrame(() => map.invalidateSize());
  }

  function wireMapToggle() {
    if (mapWired) return;
    mapWired = true;
    el("tl-map-toggle").addEventListener("click", () => {
      const stage = el("tl-stage");
      if (stage.classList.contains("no-map")) return; // nothing to toggle
      const hidden = stage.classList.toggle("map-hidden");
      el("tl-map-toggle").setAttribute("aria-pressed", String(!hidden));
      if (!hidden && state._map) {
        requestAnimationFrame(() => state._map.invalidateSize());
      }
    });
  }

  // ============ editor (Phase 3B): in/out, switches, cameras ============
  function enabledChannels() {
    const dis = state.disabledChannels || new Set();
    return state.channels.filter((c) => !dis.has(c.key));
  }
  function firstEnabledChannel() {
    const e = enabledChannels();
    return e.length ? e[0].key
      : (state.channels[0] && state.channels[0].key) || null;
  }
  function nextEnabledChannel(key) {
    const e = enabledChannels();
    if (!e.length) return key;
    const i = e.findIndex((c) => c.key === key);
    return e[(i + 1) % e.length].key;
  }

  function initSelection() {
    state.disabledChannels = new Set();
    const { start, end } = bounds();
    state.inTs = start;
    state.outTs = end;
    state.segments = [{ start, end, camera: firstEnabledChannel() }];
  }

  function segAt(ts) {
    const segs = state.segments;
    for (let i = 0; i < segs.length; i++) {
      if (ts >= segs[i].start && ts < segs[i].end) return i;
    }
    return ts >= state.outTs ? Math.max(0, segs.length - 1) : 0;
  }
  function activeChannelAt(ts) {
    if (!state.segments || !state.segments.length) return null;
    if (state.inTs == null || ts < state.inTs || ts > state.outTs) return null;
    return state.segments[segAt(ts)].camera;
  }

  function clampSegments() {
    const inT = state.inTs, outT = state.outTs;
    let segs = state.segments
      .filter((s) => s.end > inT && s.start < outT)
      .map((s) => ({
        start: Math.max(s.start, inT),
        end: Math.min(s.end, outT),
        camera: s.camera,
      }));
    if (!segs.length) {
      segs = [{ start: inT, end: outT, camera: firstEnabledChannel() }];
    }
    segs[0].start = inT;
    segs[segs.length - 1].end = outT;
    state.segments = segs;
  }

  function setIn() {
    state.inTs = clamp(state.playheadTs, bounds().start, state.outTs - 1);
    clampSegments();
    renderEditor();
  }
  function setOut() {
    state.outTs = clamp(state.playheadTs, state.inTs + 1, bounds().end);
    clampSegments();
    renderEditor();
  }
  function clearEditor() {
    if (!confirm("Clear the start/end selection and all camera switches?")) {
      return;
    }
    initSelection();
    renderEditor();
    loadPreviewAt(state.playheadTs);
  }
  function addSwitchAtPlayhead() {
    const t = state.playheadTs;
    if (t <= state.inTs || t >= state.outTs) return;
    const i = segAt(t);
    const seg = state.segments[i];
    if (t - seg.start < 0.2 || seg.end - t < 0.2) return;
    state.segments.splice(i, 1,
      { start: seg.start, end: t, camera: seg.camera },
      { start: t, end: seg.end, camera: nextEnabledChannel(seg.camera) });
    renderEditor();
    loadPreviewAt(t);
  }
  function cycleSegmentCamera(i) {
    const seg = state.segments[i];
    if (!seg || enabledChannels().length < 2) return;
    seg.camera = nextEnabledChannel(seg.camera);
    renderEditor();
    state.videoChannel = null;          // force reload onto the new camera
    loadPreviewAt(state.playheadTs);
  }
  function toggleChannel(key) {
    const dis = state.disabledChannels;
    if (dis.has(key)) {
      dis.delete(key);
    } else {
      if (enabledChannels().length <= 1) return;   // keep at least one
      dis.add(key);
      // reassign any segment using the now-disabled camera
      const fallback = firstEnabledChannel();
      for (const s of state.segments) {
        if (s.camera === key) s.camera = fallback;
      }
    }
    renderEditor();
    state.videoChannel = null;
    loadPreviewAt(state.playheadTs);
  }

  // ---- editor rendering ----
  function renderEditor() {
    renderSelection();
    renderShading();
    renderActive();
    updateSelInfo();
    updateTransport();
    syncExportButtons();
  }
  function renderSelection() {
    const host = el("tl-tracks");
    if (state.inTs == null || !host) return;
    let band = el("tl-sel");
    if (!band) {
      band = document.createElement("div");
      band.className = "tl-sel"; band.id = "tl-sel";
      host.appendChild(band);
    }
    const x = timeToX(state.inTs);
    band.style.transform = `translateX(${x}px)`;
    band.style.width = `${Math.max(0, timeToX(state.outTs) - x)}px`;
    host.querySelectorAll(".tl-switch").forEach((m) => m.remove());
    for (let i = 1; i < state.segments.length; i++) {
      const m = document.createElement("div");
      m.className = "tl-switch";
      m.dataset.idx = i;
      m.style.transform = `translateX(${timeToX(state.segments[i].start)}px)`;
      const grip = document.createElement("div");
      grip.className = "tl-switch-grip";
      grip.textContent = "⇄";
      m.appendChild(grip);
      host.appendChild(m);
    }
    const ph = el("tl-playhead"); if (ph) host.appendChild(ph);
  }
  function renderActive() {
    const active = activeChannelAt(state.playheadTs);
    for (const ch of state.channels) {
      const strip = state.trackEls && state.trackEls[ch.key];
      const row = strip && strip.closest(".tl-track");
      if (!row) continue;
      const dis = (state.disabledChannels || new Set()).has(ch.key);
      row.classList.toggle("tl-disabled", dis);
      // Highlight the label of the camera the preview is showing now.
      row.classList.toggle("tl-active", !dis && ch.key === active);
      const toggle = row.querySelector(".tl-track-toggle");
      if (toggle) toggle.classList.toggle("on", !dis);
    }
  }

  // Per-segment shading: on each track, the segments that DON'T use this
  // camera are dimmed, and everything outside [inTs,outTs] is darker — so
  // the bright regions read as exactly what the switched export will use.
  function renderShading() {
    if (state.inTs == null) return;
    const { start, end } = bounds();
    for (const ch of state.channels) {
      const strip = state.trackEls && state.trackEls[ch.key];
      if (!strip) continue;
      strip.querySelectorAll(".tl-ov").forEach((o) => o.remove());
      const addOv = (t0, t1, cls) => {
        if (t1 <= t0) return;
        const x = timeToX(t0);
        const ov = document.createElement("div");
        ov.className = "tl-ov " + cls;
        ov.style.left = `${x}px`;
        ov.style.width = `${timeToX(t1) - x}px`;
        strip.appendChild(ov);
      };
      addOv(start, state.inTs, "tl-ov-outside");   // before the selection
      addOv(state.outTs, end, "tl-ov-outside");     // after the selection
      for (const seg of state.segments) {           // non-chosen camera
        if (seg.camera !== ch.key) addOv(seg.start, seg.end, "tl-ov-dim");
      }
    }
  }
  function updateSelInfo() {
    if (state.inTs == null) return;
    const dur = Math.max(0, state.outTs - state.inTs);
    const m = Math.floor(dur / 60), s = Math.floor(dur % 60);
    const nSw = Math.max(0, state.segments.length - 1);
    el("tl-selinfo").textContent =
      `Selection ${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` +
      ` · ${nSw} switch${nSw === 1 ? "" : "es"}`;
  }

  // ---- export ----
  async function postExport(body, label) {
    const status = el("tl-exp-status");
    status.textContent = `${label}: queuing…`;
    try {
      const r = await api("/api/exports", {
        method: "POST", body: JSON.stringify(body),
      });
      status.textContent =
        `${label}: queued (job ${r.job_id}) — track it under Downloads ▸ Exports`;
    } catch (err) {
      status.textContent = `${label}: failed (${err.message || err})`;
    }
  }
  function exportSwitched() {
    const dis = state.disabledChannels || new Set();
    const segments = state.segments
      .filter((s) => !dis.has(s.camera))
      .map((s) => ({ channel: s.camera, start_ts: s.start, end_ts: s.end }));
    if (!segments.length) {
      el("tl-exp-status").textContent = "Nothing to export";
      return;
    }
    postExport({ type: "switched", segments }, "Switched");
  }
  function channelHasFootage(channel) {
    if ((state.disabledChannels || new Set()).has(channel)) return false;
    return (state.clipsByChannel[channel] || []).some(
      (c) => c.start_ts < state.outTs
        && c.start_ts + (c.duration_s || 0) > state.inTs);
  }
  function exportSingle(channel) {
    const label = (state.channels.find((c) => c.key === channel) || {}).label
      || channel;
    if (!channelHasFootage(channel)) {
      el("tl-exp-status").textContent = `No ${label} footage in selection`;
      return;
    }
    postExport({
      type: "switched",
      segments: [{ channel, start_ts: state.inTs, end_ts: state.outTs }],
    }, label);
  }
  // Hide per-camera export buttons for channels this journey doesn't have.
  function syncExportButtons() {
    const present = new Set(state.channels.map((c) => c.key));
    const f = el("tl-exp-front"), r = el("tl-exp-rear");
    if (f) f.hidden = !present.has("front");
    if (r) r.hidden = !present.has("rear");
  }

  function wireEditor() {
    if (editorWired) return;
    editorWired = true;
    el("tl-in").addEventListener("click", setIn);
    el("tl-out").addEventListener("click", setOut);
    el("tl-switch").addEventListener("click", addSwitchAtPlayhead);
    el("tl-clear").addEventListener("click", clearEditor);
    el("tl-exp-switched").addEventListener("click", exportSwitched);
    el("tl-exp-front").addEventListener("click", () => exportSingle("front"));
    el("tl-exp-rear").addEventListener("click", () => exportSingle("rear"));
    // Track on/off toggles (delegated — rows are rebuilt on every render).
    el("tl-tracks").addEventListener("click", (e) => {
      const tg = e.target.closest(".tl-track-toggle");
      if (tg && tg.dataset.ch) { e.stopPropagation(); toggleChannel(tg.dataset.ch); }
    });
    // Space toggles play/pause while the timeline is open.
    document.addEventListener("keydown", (e) => {
      if (el("view-timeline").hidden) return;
      const t = e.target;
      if (t && typeof t.closest === "function"
          && t.closest("input, textarea, select")) return;
      if (e.code === "Space") { e.preventDefault(); togglePlay(); }
    });
  }

  // Tear down playback when leaving the view — a hidden <video> keeps
  // playing audio in the background otherwise. Called by the router on
  // any navigation away from the timeline; idempotent.
  function close() {
    const v = state.videoEl;
    if (v) {
      try { v.pause(); } catch { /* ignore */ }
      v.removeAttribute("src");
      try { v.load(); } catch { /* ignore */ }  // release buffering/audio
      state.videoEl = null;
    }
    state.playing = false;
    state.videoChannel = null;
  }

  // Re-fit to the new width on resize, keeping the playhead time.
  window.addEventListener("resize", () => {
    if (!state.data || el("view-timeline").hidden) return;
    state.view.pxPerSecond = 0;
    render();
    if (state._map) requestAnimationFrame(() => state._map.invalidateSize());
  });

  // Expose a few internals so later features can layer on.
  window.Timeline = {
    open, close, _state: state, _seekToTs: seekToTs,
    _timeToX: timeToX, _xToTime: xToTime, _bounds: bounds,
    _clipAt: clipAt, _layout: layoutClipBlocks, _renderPlayhead: renderPlayhead,
    _trackWidthPx: trackWidthPx, _clamp: clamp, _el: el,
  };
})();
