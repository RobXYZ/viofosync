// First-run wizard logic — vanilla JS, no build step.
(() => {
  const form = document.getElementById("setup-form");
  const pw = document.getElementById("password");
  const confirm = document.getElementById("confirm");
  const strength = document.getElementById("strength");
  const match = document.getElementById("match");
  const errorBox = document.getElementById("error");
  const submit = document.getElementById("submit");
  const testBtn = document.getElementById("test-btn");
  const testResult = document.getElementById("test-result");
  const address = document.getElementById("address");

  function passwordsOk() {
    return pw.value.length >= 8 && confirm.value === pw.value;
  }

  function update() {
    const len = pw.value.length;
    if (len === 0) { strength.textContent = ""; strength.className = "hint"; }
    else if (len < 8) { strength.textContent = `${len}/8 characters`; strength.className = "hint bad"; }
    else { strength.textContent = "OK"; strength.className = "hint ok"; }

    if (confirm.value === "") { match.textContent = ""; match.className = "hint"; }
    else if (confirm.value !== pw.value) { match.textContent = "Passwords don't match"; match.className = "hint bad"; }
    else { match.textContent = "Match"; match.className = "hint ok"; }

    submit.disabled = !passwordsOk() || Number(diskPct.value) > DISK_CEILING;
  }

  pw.addEventListener("input", update);
  confirm.addEventListener("input", update);

  testBtn.addEventListener("click", async () => {
    const v = address.value.trim();
    if (!v) { testResult.textContent = "Enter an address first"; testResult.className = "hint bad"; return; }
    testResult.textContent = "Testing…"; testResult.className = "hint";
    try {
      const r = await fetch("/api/setup/test-dashcam", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ address: v }),
      });
      const j = await r.json();
      if (j.ok) { testResult.textContent = `Reachable (${j.latency_ms}ms)`; testResult.className = "hint ok"; }
      else { testResult.textContent = `Failed: ${j.error}`; testResult.className = "hint bad"; }
    } catch (e) {
      testResult.textContent = `Error: ${e.message}`;
      testResult.className = "hint bad";
    }
  });

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    errorBox.hidden = true;
    const fd = new FormData(form);
    const r = await fetch("/setup", { method: "POST", body: fd, redirect: "manual" });
    if (r.status === 303 || r.type === "opaqueredirect") {
      window.location.href = "/";
    } else {
      const text = await r.text();
      errorBox.hidden = false;
      errorBox.textContent = text || `Setup failed (${r.status})`;
    }
  });

  // Paint the selected scope row.
  const scopeRadios = document.querySelectorAll('input[name="scope"]');
  function paintScope() {
    scopeRadios.forEach((r) => {
      r.closest(".opt").classList.toggle("on", r.checked);
    });
  }
  scopeRadios.forEach((r) => r.addEventListener("change", paintScope));
  paintScope();

  // Mirrors the server guard: DISK_CRITICAL_PCT is not editable here.
  const DISK_CEILING = 95;
  const diskPct = document.getElementById("retention_disk_pct");
  const diskHint = document.getElementById("disk-hint");
  diskPct.addEventListener("input", () => {
    const v = Number(diskPct.value);
    const bad = v > DISK_CEILING;
    diskHint.textContent = bad ? `Must be ${DISK_CEILING}% or lower.` : "";
    diskHint.className = bad ? "hint bad" : "hint";
    submit.disabled = bad || !passwordsOk();
  });

  // Home picker. Empty coordinates are a valid submission.
  const latField = document.getElementById("home_lat");
  const lonField = document.getElementById("home_lon");
  const clearBtn = document.getElementById("clear-home");
  let marker = null;
  let circle = null;
  let map = null;

  function setHome(lat, lon) {
    latField.value = lat.toFixed(6);
    lonField.value = lon.toFixed(6);
    clearBtn.hidden = false;
    if (!map) return;
    const radius = Number(document.getElementById("home_radius").value) || 30;
    if (marker) { map.removeLayer(marker); map.removeLayer(circle); }
    marker = L.circleMarker([lat, lon], { radius: 6, color: "#4c8dff", fillOpacity: 1 }).addTo(map);
    circle = L.circle([lat, lon], { radius, color: "#4c8dff", fillOpacity: 0.18 }).addTo(map);
    map.setView([lat, lon], Math.max(map.getZoom(), 14));
  }

  function clearHome() {
    latField.value = "";
    lonField.value = "";
    clearBtn.hidden = true;
    if (map && marker) { map.removeLayer(marker); map.removeLayer(circle); marker = null; circle = null; }
  }

  clearBtn.addEventListener("click", clearHome);

  document.getElementById("home_radius").addEventListener("input", () => {
    if (latField.value) setHome(Number(latField.value), Number(lonField.value));
  });

  if (typeof L === "undefined") {
    // No Leaflet (no internet, or the CDN is blocked): type coordinates.
    document.getElementById("home-map").hidden = true;
    document.getElementById("home-fallback").hidden = false;
    const ml = document.getElementById("home_lat_manual");
    const mo = document.getElementById("home_lon_manual");
    const sync = () => {
      const lat = parseFloat(ml.value);
      const lon = parseFloat(mo.value);
      if (Number.isFinite(lat) && Number.isFinite(lon)) setHome(lat, lon);
      else clearHome();
    };
    ml.addEventListener("input", sync);
    mo.addEventListener("input", sync);
  } else {
    map = L.map("home-map", { attributionControl: false }).setView([54.0, -2.0], 5);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
    }).addTo(map);
    map.on("click", (e) => setHome(e.latlng.lat, e.latlng.lng));

    // geolocation needs a secure context, which plain HTTP on a LAN is not.
    if (window.isSecureContext && navigator.geolocation) {
      const locBtn = document.getElementById("use-location");
      locBtn.hidden = false;
      locBtn.addEventListener("click", () => {
        locBtn.disabled = true;
        navigator.geolocation.getCurrentPosition(
          (p) => { setHome(p.coords.latitude, p.coords.longitude); locBtn.disabled = false; },
          () => { locBtn.disabled = false; },
        );
      });
    }
  }

  update();
})();
