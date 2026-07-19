"use strict";

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, txt) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt != null) e.textContent = txt;
  return e;
};

const state = {
  source: null,
  sources: [],
  task: null,
  episodes: [],
  episode: null,
  detail: null,
  eye: "left",
  tiles: [],        // { camera, video, onReady } for each grid cell with video
  master: null,     // the video element that drives the shared timeline
  duration: 0,      // seconds; max across tiles (falls back to robot duration)
  robotDuration: 0, // seconds from robot_data (for cursor mapping)
  plots: [],        // { cursor, ctx, W, H } overlay canvases to animate
  playing: false,
  raf: null,
  eeTraceOn: true,   // show the end-effector future-trace overlay
  filter: null,      // { records, coverage, active:{field->constraint}, scanning }
};

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) {
    let msg = `${r.status}`;
    try { msg = (await r.json()).error || (await r.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return r.json();
}

// All dataset endpoints are scoped to the active source.
function apiBase() {
  return `/api/sources/${encodeURIComponent(state.source)}`;
}

function toast(msg) {
  const t = el("div", "toast", msg);
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 4000);
}

/* ---------------- Sidebar: tasks + episodes ---------------- */

async function init() {
  try {
    const { sources } = await api("/api/sources");
    state.sources = sources;
    // Source selector in the sidebar.
    const ssel = $("#source-select");
    ssel.innerHTML = "";
    sources.forEach((s) => ssel.appendChild(new Option(s.label, s.id)));
    ssel.onchange = () => selectSource(ssel.value);

    // Hash is #source/task/episode for shareable links.
    const [hSrc, hTask, hEp] = decodeURIComponent(location.hash.slice(1)).split("/");
    const startSrc = sources.some((s) => s.id === hSrc) ? hSrc : sources[0]?.id;
    await selectSource(startSrc, hTask || null, hEp || null);
  } catch (e) {
    toast("Failed to load sources: " + e.message);
  }
  $("#task-select").onchange = (ev) => selectTask(ev.target.value);
  $("#episode-search").addEventListener("input", renderEpisodeList);
  $("#eye-toggle").addEventListener("click", (ev) => {
    const b = ev.target.closest("button");
    if (!b) return;
    state.eye = b.dataset.eye;
    document.querySelectorAll("#eye-toggle button").forEach((x) => x.classList.toggle("active", x === b));
    if (state.detail) buildCameraGrid(state.detail.cameras || []);  // reload all tiles
  });
  $("#calib-head").addEventListener("click", () => $(".calib-card").classList.toggle("collapsed"));
  $("#brand-home").addEventListener("click", showOverview);
  $("#play-btn").addEventListener("click", togglePlay);
  $("#scrubber").addEventListener("input", onScrub);
  $("#trace-toggle").addEventListener("click", (ev) => {
    state.eeTraceOn = !state.eeTraceOn;
    ev.target.classList.toggle("active", state.eeTraceOn);
    drawAllTraces(currentTime());  // redraw (or clear) immediately
  });
}

/* ---------------- Source + overview ---------------- */

async function selectSource(sid, autoTask = null, autoEpisode = null) {
  state.source = sid;
  state.episode = null;
  $("#source-select").value = sid;
  try {
    const { tasks } = await api(`${apiBase()}/tasks`);
    const sel = $("#task-select");
    sel.innerHTML = "";
    tasks.forEach((t) => sel.appendChild(new Option(t, t)));
    const startTask = tasks.includes(autoTask) ? autoTask : tasks[0];
    if (startTask) await selectTask(startTask, autoEpisode);
    if (!autoEpisode) showOverview();
  } catch (e) {
    toast("Failed to load source: " + e.message);
  }
}

function updateHash() {
  const parts = [state.source, state.task, state.episode].filter(Boolean);
  location.hash = parts.map(encodeURIComponent).join("/");
}

function showOverview() {
  stopPlayback();
  state.episode = null;
  updateHash();
  renderEpisodeList();
  $("#episode-view").classList.add("hidden");
  $("#overview-view").classList.remove("hidden");
  renderOverview();
}

async function renderOverview() {
  try {
    const ov = await api(`${apiBase()}/overview`);
    $("#s3-root").textContent = `s3://${ov.bucket}/${ov.prefix}`;
    $("#ov-path").innerHTML = "";
    $("#ov-path").appendChild(el("div", "ov-uri", `s3://${ov.bucket}/${ov.prefix}`));
    $("#ov-path").appendChild(el("div", "ov-region", `region ${ov.region}`));

    const stats = $("#ov-stats");
    stats.innerHTML = "";
    const cards = [
      [ov.num_tasks, "Tasks"],
      [ov.num_episodes, "Episodes"],
      [ov.stations.length, ov.stations.length === 1 ? "Station" : "Stations"],
    ];
    cards.forEach(([num, lbl]) => {
      const c = el("div", "ov-stat");
      c.appendChild(el("div", "num", String(num)));
      c.appendChild(el("div", "lbl", lbl));
      stats.appendChild(c);
    });
    // Hours-of-data card — filled in once /api/stats (with per-episode durations)
    // loads in renderAnalytics; shows "…" until then.
    const hcard = el("div", "ov-stat");
    hcard.appendChild(el("div", "num", "…"));
    hcard.querySelector(".num").id = "ov-hours-num";
    const hlbl = el("div", "lbl", "Hours");
    hlbl.id = "ov-hours-lbl";
    hcard.appendChild(hlbl);
    stats.appendChild(hcard);
    if (ov.stations.length) {
      const c = el("div", "ov-stat");
      c.appendChild(el("div", "num", "🖥"));
      c.appendChild(el("div", "lbl wrap", ov.stations.join(", ")));
      stats.appendChild(c);
    }

    const maxEp = Math.max(1, ...ov.tasks.map((t) => t.episodes));
    const list = $("#ov-task-list");
    list.innerHTML = "";
    $("#ov-task-hint").textContent = `${ov.num_tasks} total`;
    ov.tasks.forEach((t) => {
      const row = el("div", "ov-task-row");
      row.appendChild(el("div", "t-name", t.task));
      const bar = el("div", "t-bar");
      const fill = el("i");
      fill.style.width = `${(t.episodes / maxEp) * 100}%`;
      bar.appendChild(fill);
      row.appendChild(bar);
      row.appendChild(el("div", "t-count", `${t.episodes} ep`));
      // Per-task hours — filled in by updateHoursCard once /api/stats loads.
      const hrs = el("div", "t-hours", "…");
      hrs.dataset.task = t.task;
      row.appendChild(hrs);
      const latest = t.latest ? parseEpisodeName(t.latest).when || "" : "";
      row.appendChild(el("div", "t-latest", latest ? latest.split(" · ")[0] : ""));
      row.onclick = () => selectTask(t.task);
      list.appendChild(row);
    });

    state.overviewTasks = ov.tasks;  // per-task totals, for extrapolating hours
    renderAnalytics(ov.tasks.map((t) => t.task));
  } catch (e) {
    toast("Failed to load overview: " + e.message);
  }
}

/* ---------------- Overview analytics charts ---------------- */

// Stable per-task color for the scatter, keyed by task order.
function taskColors(tasks) {
  const map = {};
  tasks.forEach((t, i) => { map[t] = PALETTE[i % PALETTE.length]; });
  return map;
}

async function renderAnalytics(taskOrder) {
  // Charts load after a separate stats fetch (can be slower on huge datasets).
  $("#hist-hint").textContent = "loading…";
  $("#scatter-hint").textContent = "loading…";
  const forSource = state.source;
  let stats;
  try {
    stats = await api(`${apiBase()}/stats`);
  } catch (e) {
    $("#hist-hint").textContent = "";
    $("#scatter-hint").textContent = "";
    toast("Failed to load stats: " + e.message);
    return;
  }
  if (forSource !== state.source) return;  // user switched away; drop stale result
  const eps = (stats.episodes || []).filter((e) => e.duration_s != null);
  const colors = taskColors(taskOrder || []);

  updateHoursCard(eps, stats);
  drawHistogram(eps);
  drawScatter(eps, colors);

  // Seed the episode filter from the same records the charts use. On small
  // sources this sample IS every episode; on large ones it's a sample until the
  // user runs a full scan (the "Scan all" button).
  initFilter(stats.episodes || [], {
    total: stats.total_episodes, scanned: stats.scanned ?? (stats.episodes || []).length,
    sampled: !!stats.sampled, full: false,
  });

  // Honestly label sampling: if the source subsampled, say so.
  const suffix = stats.sampled ? ` (sampled of ${stats.total_episodes.toLocaleString()})` : "";
  $("#hist-hint").textContent = `${eps.length} episodes${suffix}`;
  const withTime = eps.filter((e) => e.timestamp).length;
  $("#scatter-hint").textContent = withTime ? `${withTime} episodes${suffix}` : "no timestamps";

  // Legend for the scatter (one chip per task).
  const legend = $("#scatter-legend");
  legend.innerHTML = "";
  (taskOrder || []).forEach((t) => {
    const s = el("span");
    const i = el("i");
    i.style.background = colors[t];
    s.appendChild(i);
    s.appendChild(el("span", null, t));
    legend.appendChild(s);
  });
}

// Sum episode durations into the "Hours" overview card. When stats were sampled
// (huge sources), scale the sampled mean up to the true episode count and mark it
// an estimate, rather than under-reporting.
function updateHoursCard(eps, stats) {
  const numEl = $("#ov-hours-num");
  const lblEl = $("#ov-hours-lbl");
  if (!numEl) return;
  const durs = eps.map((e) => e.duration_s).filter((d) => d > 0);
  if (!durs.length) {
    numEl.textContent = "—";
    lblEl.textContent = "Hours";
    return;
  }
  const sumSecs = durs.reduce((a, b) => a + b, 0);
  let totalSecs = sumSecs;
  let estimated = false;
  if (stats.sampled && stats.total_episodes) {
    // Extrapolate: mean sampled duration × all episodes.
    totalSecs = (sumSecs / durs.length) * stats.total_episodes;
    estimated = true;
  }
  const hours = totalSecs / 3600;
  numEl.textContent = (estimated ? "~" : "") + (hours >= 10 ? hours.toFixed(0) : hours.toFixed(1));
  lblEl.textContent = estimated ? "Hours (est.)" : "Hours";
  numEl.title = estimated
    ? `Estimated from ${durs.length} sampled episodes (mean ${(sumSecs / durs.length).toFixed(1)}s) × ${stats.total_episodes.toLocaleString()} episodes`
    : `Sum of ${durs.length} episode durations`;

  updatePerTaskHours(eps, stats, estimated);
}

// Fill the per-task hours cells. For sampled sources, scale each task's own
// sampled mean by its full episode count (from the overview's per-task totals).
function updatePerTaskHours(eps, stats, estimated) {
  const byTask = {};  // task -> {sum, n}
  eps.forEach((e) => {
    const b = byTask[e.task] || (byTask[e.task] = { sum: 0, n: 0 });
    b.sum += e.duration_s; b.n += 1;
  });
  // Total episode count per task (for extrapolation) from the overview payload.
  const totalByTask = {};
  (state.overviewTasks || []).forEach((t) => { totalByTask[t.task] = t.episodes; });

  document.querySelectorAll(".t-hours").forEach((cell) => {
    const task = cell.dataset.task;
    const b = byTask[task];
    if (!b || !b.n) { cell.textContent = "—"; return; }
    let secs = b.sum;
    if (estimated && totalByTask[task]) secs = (b.sum / b.n) * totalByTask[task];
    const h = secs / 3600;
    const txt = h >= 10 ? h.toFixed(0) : h.toFixed(1);
    cell.textContent = (estimated ? "~" : "") + txt + "h";
  });
}

/* ---------------- Episode filter ---------------- */

// Filterable attributes, in display order. Each facet declares its kind and how to
// read its value from an episode stat record. A facet only appears if at least one
// scanned episode carries a non-null value for it — otherwise it renders disabled
// as "not available for this dataset" (consistent with the metadata empty states).
const FILTER_FACETS = [
  { field: "task", label: "Task", kind: "enum", get: (e) => e.task },
  { field: "duration_s", label: "Duration (s)", kind: "range", get: (e) => e.duration_s },
  { field: "status", label: "Status", kind: "enum", get: (e) => e.status },
  { field: "station", label: "Station", kind: "enum", get: (e) => e.station },
  { field: "num_cameras", label: "Cameras", kind: "range", get: (e) => e.num_cameras, int: true },
  { field: "robot_frames", label: "Robot frames", kind: "range", get: (e) => e.robot_frames, int: true },
  { field: "has_annotations", label: "Annotations", kind: "bool", get: (e) => e.has_annotations },
];

function initFilter(records, coverage) {
  state.filter = { records, coverage, active: {}, scanning: false };
  buildFilterFacets();
  applyFilter();
  const btn = $("#filter-scan-btn");
  btn.onclick = startFullScan;
  btn.disabled = false;  // a prior source's aborted scan may have left it disabled
  // Only offer "Scan all" when the current records are an incomplete sample.
  btn.classList.toggle("hidden", !coverage.sampled);
}

// A facet is "available" if some record has a non-null value for its field.
function facetAvailable(f) {
  return state.filter.records.some((e) => {
    const v = f.get(e);
    return v !== null && v !== undefined && v !== "";
  });
}

function buildFilterFacets() {
  const wrap = $("#filter-controls");
  wrap.innerHTML = "";
  FILTER_FACETS.forEach((f) => {
    const box = el("div", "facet");
    box.appendChild(el("div", "facet-label", f.label));
    if (!facetAvailable(f)) {
      box.classList.add("facet-disabled");
      box.appendChild(el("div", "facet-na subtle", "not available for this dataset"));
      wrap.appendChild(box);
      return;
    }
    if (f.kind === "range") buildRangeFacet(box, f);
    else if (f.kind === "enum") buildEnumFacet(box, f);
    else if (f.kind === "bool") buildBoolFacet(box, f);
    wrap.appendChild(box);
  });
}

function buildRangeFacet(box, f) {
  const vals = state.filter.records.map(f.get).filter((v) => v != null);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (f.int) { lo = Math.floor(lo); hi = Math.ceil(hi); }
  const row = el("div", "facet-range");
  const minIn = Object.assign(document.createElement("input"),
    { type: "number", value: f.int ? lo : Math.floor(lo), min: lo, max: hi, className: "facet-num" });
  const maxIn = Object.assign(document.createElement("input"),
    { type: "number", value: f.int ? hi : Math.ceil(hi), min: lo, max: hi, className: "facet-num" });
  const sync = () => {
    // An empty/invalid box means "no bound on that side" — treat as ±∞ rather than
    // NaN (every comparison against NaN is false, which would hide all episodes).
    const a = parseFloat(minIn.value), b = parseFloat(maxIn.value);
    const loB = Number.isNaN(a) ? -Infinity : a;
    const hiB = Number.isNaN(b) ? Infinity : b;
    state.filter.active[f.field] = (e) => {
      const v = f.get(e);
      return v != null && v >= loB && v <= hiB;
    };
    applyFilter();
  };
  minIn.oninput = sync; maxIn.oninput = sync;
  row.appendChild(minIn);
  row.appendChild(el("span", "facet-dash", "–"));
  row.appendChild(maxIn);
  box.appendChild(row);
}

function buildEnumFacet(box, f) {
  const seen = [...new Set(state.filter.records.map(f.get).filter((v) => v != null && v !== ""))].sort();
  const row = el("div", "facet-chips");
  const chosen = new Set();
  seen.forEach((val) => {
    const chip = el("button", "facet-chip", String(val));
    chip.onclick = () => {
      if (chosen.has(val)) { chosen.delete(val); chip.classList.remove("on"); }
      else { chosen.add(val); chip.classList.add("on"); }
      state.filter.active[f.field] = chosen.size
        ? (e) => chosen.has(f.get(e))
        : null;
      applyFilter();
    };
    row.appendChild(chip);
  });
  box.appendChild(row);
}

function buildBoolFacet(box, f) {
  const row = el("div", "facet-chips");
  const opts = [["any", null], ["yes", true], ["no", false]];
  opts.forEach(([lbl, want], i) => {
    const chip = el("button", "facet-chip" + (i === 0 ? " on" : ""), lbl);
    chip.onclick = () => {
      row.querySelectorAll(".facet-chip").forEach((c) => c.classList.remove("on"));
      chip.classList.add("on");
      state.filter.active[f.field] = want === null ? null : (e) => f.get(e) === want;
      applyFilter();
    };
    row.appendChild(chip);
  });
  box.appendChild(row);
}

// Run every active predicate over the records (AND semantics) and render matches.
function applyFilter() {
  const { records, active } = state.filter;
  const preds = Object.values(active).filter(Boolean);
  const matches = records.filter((e) => preds.every((p) => p(e)));
  renderFilterResults(matches);
  updateFilterCoverage(matches.length);
}

function updateFilterCoverage(matchCount) {
  const { coverage, records } = state.filter;
  const scanned = records.length;
  const parts = [];
  if (matchCount != null) parts.push(`${matchCount.toLocaleString()} matching`);
  parts.push(coverage.sampled && !coverage.full
    ? `of ${scanned.toLocaleString()} sampled (dataset has ${coverage.total.toLocaleString()})`
    : `of ${scanned.toLocaleString()} scanned`);
  $("#filter-coverage").textContent = parts.join(" ");
}

const FILTER_RESULT_CAP = 200;

function renderFilterResults(matches) {
  const wrap = $("#filter-results");
  wrap.innerHTML = "";
  if (!matches.length) {
    wrap.appendChild(el("div", "subtle empty-note", "No episodes match the current filters."));
    return;
  }
  const shown = matches.slice(0, FILTER_RESULT_CAP);
  shown.forEach((e) => {
    const row = el("div", "fr-row");
    row.appendChild(el("div", "fr-task", e.task));
    row.appendChild(el("div", "fr-ep mono", parseEpisodeName(e.episode).name));
    row.appendChild(el("div", "fr-dur mono", e.duration_s != null ? `${e.duration_s.toFixed(1)}s` : "—"));
    const tags = el("div", "fr-tags");
    if (e.status) tags.appendChild(el("span", "fr-tag " + e.status, e.status));
    if (e.has_annotations) tags.appendChild(el("span", "fr-tag ann", `${e.n_annotations ?? "?"} subtasks`));
    if (e.num_cameras) tags.appendChild(el("span", "fr-tag", `${e.num_cameras} cam`));
    row.appendChild(tags);
    row.onclick = () => { selectTask(e.task, e.episode); };
    wrap.appendChild(row);
  });
  if (matches.length > shown.length) {
    wrap.appendChild(el("div", "fr-more subtle",
      `+${(matches.length - shown.length).toLocaleString()} more — narrow the filters`));
  }
}

// Upgrade from the sampled seed to full coverage: kick off the background scan and
// poll, refreshing the filter as records stream in. Cached, so re-runs are fast.
async function startFullScan() {
  if (state.filter.scanning) return;
  state.filter.scanning = true;
  const forSource = state.source;
  const btn = $("#filter-scan-btn");
  btn.disabled = true;
  try {
    await fetch(`${apiBase()}/scan`, { method: "POST" });
    while (true) {
      const snap = await api(`${apiBase()}/scan`);
      if (forSource !== state.source) return;  // user switched away
      // Refresh records live but DON'T rebuild facets mid-scan (that would reset
      // the inputs the user is touching); just re-apply their predicates. Rebuild
      // once at the end so newly-seen values widen ranges / reveal chips.
      state.filter.records = snap.episodes;
      state.filter.coverage = { total: snap.total_episodes, scanned: snap.scanned,
                                sampled: true, full: snap.done };
      $("#filter-scan-status").textContent =
        `scanning ${snap.scanned.toLocaleString()} / ${snap.total_episodes.toLocaleString()}…`;
      applyFilter();
      if (snap.done) {
        // Rebuild facets so newly-seen values widen ranges / reveal chips — but
        // ONLY if the user hasn't set any filter yet. Rebuilding resets controls to
        // their default (unfiltered) display, which would desync from still-active
        // predicates; when filters are active we keep the current controls as-is.
        if (!Object.values(state.filter.active).some(Boolean)) buildFilterFacets();
        applyFilter();
        $("#filter-scan-status").textContent = `scanned all ${snap.scanned.toLocaleString()}`;
        btn.classList.add("hidden");
        break;
      }
      await new Promise((r) => setTimeout(r, 1500));
    }
  } catch (e) {
    $("#filter-scan-status").textContent = "scan failed: " + e.message;
  } finally {
    state.filter.scanning = false;
    btn.disabled = false;  // always recover the button (abort, error, or success)
  }
}

function setupCanvas(id) {
  const canvas = $("#" + id);
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.clientWidth, H = canvas.clientHeight;
  canvas.width = W * dpr; canvas.height = H * dpr;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  return { ctx, W, H };
}

function niceDuration(s) {
  return s >= 60 ? `${(s / 60).toFixed(1)}m` : `${s.toFixed(0)}s`;
}

// Histogram of episode duration (seconds).
function drawHistogram(eps) {
  const { ctx, W, H } = setupCanvas("hist-canvas");
  $("#hist-axis").innerHTML = "";
  if (!eps.length) return;

  const durs = eps.map((e) => e.duration_s);
  const lo = 0;
  const hi = Math.max(...durs);
  const nBins = Math.min(20, Math.max(6, Math.round(Math.sqrt(eps.length) * 2)));
  const binW = (hi - lo) / nBins || 1;
  const bins = new Array(nBins).fill(0);
  durs.forEach((d) => {
    let b = Math.floor((d - lo) / binW);
    if (b >= nBins) b = nBins - 1;
    if (b < 0) b = 0;
    bins[b]++;
  });
  const maxCount = Math.max(...bins);

  const pad = { l: 30, r: 8, t: 10, b: 8 };
  const plotW = W - pad.l - pad.r, plotH = H - pad.t - pad.b;

  // y gridlines + labels (counts)
  ctx.font = "10px 'JetBrains Mono', monospace";
  ctx.fillStyle = "#626875";
  ctx.textAlign = "right";
  const yTicks = niceTicks(0, maxCount, 4);
  yTicks.forEach((v) => {
    const y = pad.t + plotH * (1 - v / (maxCount || 1));
    ctx.strokeStyle = "rgba(255,255,255,0.06)";
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
    ctx.fillText(String(v), pad.l - 5, y + 3);
  });

  // bars
  const gap = 2;
  const bw = plotW / nBins;
  for (let i = 0; i < nBins; i++) {
    if (!bins[i]) continue;
    const h = plotH * (bins[i] / maxCount);
    const x = pad.l + i * bw;
    const y = pad.t + plotH - h;
    ctx.fillStyle = "#6ea8fe";
    ctx.fillRect(x + gap / 2, y, bw - gap, h);
  }

  // x-axis labels (min / mid / max duration)
  const axis = $("#hist-axis");
  [lo, lo + (hi - lo) / 2, hi].forEach((v) => axis.appendChild(el("span", null, niceDuration(v))));
}

// Scatter: episode duration (y) vs recorded wallclock time (x), colored by task.
function drawScatter(eps, colors) {
  const { ctx, W, H } = setupCanvas("scatter-canvas");
  $("#scatter-axis").innerHTML = "";
  const pts = eps
    .filter((e) => e.timestamp)
    .map((e) => ({ t: Date.parse(e.timestamp), y: e.duration_s, task: e.task, ep: e.episode }))
    .filter((p) => !isNaN(p.t));
  if (!pts.length) return;

  const tMin = Math.min(...pts.map((p) => p.t));
  const tMax = Math.max(...pts.map((p) => p.t));
  const yMax = Math.max(...pts.map((p) => p.y));
  const tSpan = tMax - tMin || 1;

  const pad = { l: 30, r: 8, t: 10, b: 8 };
  const plotW = W - pad.l - pad.r, plotH = H - pad.t - pad.b;
  const X = (t) => pad.l + ((t - tMin) / tSpan) * plotW;
  const Y = (y) => pad.t + plotH * (1 - y / (yMax || 1));

  // y gridlines (duration)
  ctx.font = "10px 'JetBrains Mono', monospace";
  ctx.textAlign = "right";
  niceTicks(0, yMax, 4).forEach((v) => {
    const y = Y(v);
    ctx.strokeStyle = "rgba(255,255,255,0.06)";
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
    ctx.fillStyle = "#626875";
    ctx.fillText(niceDuration(v), pad.l - 5, y + 3);
  });

  // points
  pts.forEach((p) => {
    ctx.fillStyle = colors[p.task] || "#6ea8fe";
    ctx.beginPath();
    ctx.arc(X(p.t), Y(p.y), 4, 0, Math.PI * 2);
    ctx.fill();
  });

  // x-axis labels (dates)
  const fmt = (ms) => {
    const d = new Date(ms);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  };
  const axis = $("#scatter-axis");
  [tMin, tMin + tSpan / 2, tMax].forEach((t) => axis.appendChild(el("span", null, fmt(t))));
}

// Produce up to `count` "nice" round tick values between lo and hi.
function niceTicks(lo, hi, count) {
  if (hi <= lo) return [0];
  const raw = (hi - lo) / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = (norm >= 5 ? 5 : norm >= 2 ? 2 : 1) * mag;
  const ticks = [];
  for (let v = 0; v <= hi + 1e-9; v += step) ticks.push(Math.round(v * 100) / 100);
  return ticks;
}

async function selectTask(task, autoEpisode = null) {
  state.task = task;
  $("#task-select").value = task;
  try {
    const { episodes } = await api(`${apiBase()}/tasks/${encodeURIComponent(task)}/episodes`);
    state.episodes = episodes;
    renderEpisodeList();
    if (autoEpisode && episodes.includes(autoEpisode)) {
      await selectEpisode(autoEpisode);
    }
  } catch (e) {
    toast("Failed to load episodes: " + e.message);
  }
}

// Episode lists can be very large (YAM tasks have >1000). Cap the rendered rows
// so the sidebar stays responsive; the search box narrows within the full list.
const EPISODE_RENDER_CAP = 300;

function renderEpisodeList() {
  const filter = $("#episode-search").value.toLowerCase();
  const list = $("#episode-list");
  list.innerHTML = "";
  const matched = state.episodes.filter((e) => e.toLowerCase().includes(filter));
  const shown = matched.slice(0, EPISODE_RENDER_CAP);
  $("#episode-count").textContent = matched.length;
  shown.forEach((ep) => {
    const li = el("li");
    li.classList.toggle("active", ep === state.episode);
    const parts = parseEpisodeName(ep);
    li.appendChild(el("div", "ep-li-name", parts.name));
    if (parts.when) li.appendChild(el("div", "ep-li-meta", parts.when));
    li.onclick = () => selectEpisode(ep);
    list.appendChild(li);
  });
  if (matched.length > shown.length) {
    const more = el("li", "ep-li-more", `+${matched.length - shown.length} more — refine search`);
    more.style.pointerEvents = "none";
    list.appendChild(more);
  }
}

// Episode names are either "station_2026-06-30T17-19-12..." (raiden) or
// "episode_<uuid>" (YAM). Show a readable label + timestamp when present.
function parseEpisodeName(ep) {
  const m = ep.match(/^(.*?)_(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})/);
  if (m) return { name: m[1], when: `${m[2]} · ${m[3]}:${m[4]}:${m[5]}` };
  const u = ep.match(/^episode_([0-9a-f]{8})/);
  if (u) return { name: `episode ${u[1]}`, when: null };
  return { name: ep, when: null };
}

/* ---------------- Episode detail ---------------- */

async function selectEpisode(ep) {
  stopPlayback();
  state.episode = ep;
  updateHash();
  renderEpisodeList();
  $("#overview-view").classList.add("hidden");
  $("#episode-view").classList.remove("hidden");
  $("#ep-instruction").textContent = "Loading…";
  try {
    const detail = await api(
      `${apiBase()}/tasks/${encodeURIComponent(state.task)}/episodes/${encodeURIComponent(ep)}`
    );
    state.detail = detail;
    renderDetail(detail);
  } catch (e) {
    toast("Failed to load episode: " + e.message);
    $("#ep-instruction").textContent = "Error loading episode";
  }
}

function renderDetail(d) {
  const md = d.metadata || {};
  // instruction is a top-level field now (both sources); fall back to metadata.
  $("#ep-instruction").textContent =
    d.instruction || md.task_instruction || md.task_name || d.episode;
  $("#ep-task").textContent = d.task;
  $("#ep-name").textContent = d.episode;

  const status = (d.status || "").toLowerCase();
  const badge = $("#ep-status");
  if (d.status) {
    badge.textContent = d.status;
    badge.className = "status-badge " + (status === "success" ? "success" : "failure");
    badge.classList.remove("hidden");
  } else {
    badge.classList.add("hidden");  // YAM episodes carry no status
  }

  // Eye toggle only applies to stereo (raiden). Hide it when cameras are single-eye.
  const hasStereo = (d.cameras || []).some((c) => (c.eyes || []).length > 1);
  $("#eye-toggle").classList.toggle("hidden", !hasStereo);

  // EE-trace toggle only when the source provides projectable traces.
  const hasTraces = !!(d.ee_traces && d.ee_traces.cameras &&
                       Object.keys(d.ee_traces.cameras).length && d.ee_traces.arms.length);
  $("#trace-toggle").classList.toggle("hidden", !hasTraces);

  buildCameraGrid(d.cameras || []);
  renderMeta(md, d);
  renderPlots(d.robot);
  renderCalibration(d.calibration, d.cameras || []);
  renderAnnotations(d.annotations || []);
}

function prettyCam(name) {
  return name.replace(/_camera$/, "").replace(/_/g, " ");
}

/* ---------------- Camera grid ---------------- */

function buildCameraGrid(cameras) {
  stopPlayback();
  const grid = $("#camera-grid");
  grid.innerHTML = "";
  state.tiles = [];
  state.master = null;
  state.duration = 0;

  if (!cameras.length) {
    grid.appendChild(makeCamTile(null, "No cameras recorded for this episode."));
    return;
  }

  // One tile per camera, in a stable order. Stub cameras (no video) render a
  // graceful placeholder rather than a broken player.
  cameras.forEach((c) => {
    if (c.has_video) {
      grid.appendChild(makeVideoTile(c));
    } else {
      grid.appendChild(makeCamTile(c.name, "No recorded video", "stub file — header only"));
    }
  });
}

// A static (non-video) tile: missing camera or an error placeholder.
function makeCamTile(name, msg, sub, isError = false) {
  const tile = el("div", "cam-tile");
  if (name) tile.appendChild(camLabel(name));
  const ov = el("div", "cam-overlay" + (isError ? " err" : ""));
  ov.appendChild(el("div", "cam-icon"));
  ov.appendChild(el("div", "cam-msg", msg));
  if (sub) ov.appendChild(el("div", "cam-sub", sub));
  tile.appendChild(ov);
  return tile;
}

function camLabel(name, dims) {
  const lab = el("div", "cam-label", prettyCam(name));
  if (dims) lab.appendChild(el("span", "cam-dims", dims));
  return lab;
}

function makeVideoTile(c) {
  const tile = el("div", "cam-tile");
  const label = camLabel(c.name);
  const video = document.createElement("video");
  video.playsInline = true;
  video.preload = "auto";
  video.muted = true;              // required for programmatic play of many tiles
  const overlay = el("div", "cam-overlay");
  overlay.appendChild(el("div", "spinner"));
  overlay.appendChild(el("div", "cam-msg", "Decoding…"));
  overlay.appendChild(el("div", "cam-sub", "first load transcodes .svo2 → mp4"));

  tile.appendChild(video);
  // EE-trace overlay canvas (only meaningful for cameras with projection params).
  const traceCanvas = el("canvas", "cam-trace");
  tile.appendChild(traceCanvas);
  tile.appendChild(label);
  tile.appendChild(overlay);

  const url =
    `${apiBase()}/tasks/${encodeURIComponent(state.task)}/episodes/${encodeURIComponent(state.episode)}` +
    `/video?camera=${encodeURIComponent(c.name)}&eye=${state.eye}`;

  // Projection params for this camera, if the source provided EE traces.
  const proj = (state.detail && state.detail.ee_traces && state.detail.ee_traces.cameras)
    ? state.detail.ee_traces.cameras[c.name] : null;
  const tileState = { camera: c.name, video, ready: false, canvas: traceCanvas, proj };
  const onReady = () => {
    if (tileState.ready) return;
    tileState.ready = true;
    overlay.classList.add("hidden");
    label.innerHTML = "";
    label.appendChild(document.createTextNode(prettyCam(c.name)));
    label.appendChild(el("span", "cam-dims", `${video.videoWidth}×${video.videoHeight}`));
    // Track the longest clip as the master timeline driver.
    if (video.duration && video.duration > state.duration) {
      state.duration = video.duration;
      state.master = video;
    }
    if (!state.master) state.master = video;
    updateDurationUI();
    drawTrace(tileState, currentTime());  // show the trace from the current position
  };
  video.onloadedmetadata = onReady;
  video.oncanplay = onReady;
  video.onloadeddata = onReady;
  video.onerror = () => {
    overlay.className = "cam-overlay err";
    overlay.innerHTML = "";
    overlay.appendChild(el("div", "cam-icon"));
    overlay.appendChild(el("div", "cam-msg", "Could not decode this stream"));
    overlay.appendChild(el("div", "cam-sub", c.name));
  };
  video.src = url;
  video.load();

  state.tiles.push(tileState);
  return tile;
}

/* ---------------- Master transport: sync all tiles + plot cursor ---------------- */

function currentTime() {
  return state.master ? state.master.currentTime : 0;
}

function timelineDuration() {
  // Prefer the video duration; fall back to the robot trajectory length.
  return state.duration || state.robotDuration || 0;
}

function togglePlay() {
  if (state.playing) stopPlayback();
  else startPlayback();
}

function startPlayback() {
  if (!state.tiles.length) return;
  state.playing = true;
  $("#play-btn").textContent = "❚❚";
  // If at (or past) the end, restart from 0.
  const dur = timelineDuration();
  if (state.master && dur && state.master.currentTime >= dur - 0.05) {
    seekAll(0);
  }
  state.tiles.forEach((t) => { if (t.ready) t.video.play().catch(() => {}); });
  tick();
}

function stopPlayback() {
  state.playing = false;
  $("#play-btn").textContent = "▶";
  state.tiles.forEach((t) => t.video.pause());
  if (state.raf) { cancelAnimationFrame(state.raf); state.raf = null; }
}

function seekAll(seconds) {
  state.tiles.forEach((t) => {
    if (t.ready) { try { t.video.currentTime = seconds; } catch (_) {} }
  });
}

function onScrub(ev) {
  const dur = timelineDuration();
  if (!dur) return;
  const wasPlaying = state.playing;
  if (wasPlaying) stopPlayback();
  const secs = (ev.target.value / 1000) * dur;
  seekAll(secs);
  updateTransportUI(secs);
  drawAllCursors(secs);
  drawAllTraces(secs);
  if (wasPlaying) startPlayback();
}

// Per-frame loop while playing: advance scrubber + move plot cursors in lockstep.
function tick() {
  if (!state.playing) return;
  const dur = timelineDuration();
  const t = currentTime();
  updateTransportUI(t);
  drawAllCursors(t);
  drawAllTraces(t);
  // Master clip ended -> stop and pin at the end.
  if (state.master && dur && state.master.ended) {
    stopPlayback();
    updateTransportUI(dur);
    drawAllCursors(dur);
    return;
  }
  state.raf = requestAnimationFrame(tick);
}

function updateDurationUI() {
  updateTransportUI(currentTime());
}

/* ---------------- EE future-trace overlay on camera tiles ---------------- */

const EE_TRACE_STEPS = 8;      // how many future waypoints to draw
const EE_TRACE_HORIZON_S = 1.5;  // over what future time window

// Project a base-frame point (x,y,z) to pixel coords for a camera's params.
// Convention: X_cam = R^T (X_base - t); uv = K X_cam (verified against real frames).
function projectPoint(p, proj, W, H) {
  const R = proj.R, t = proj.t, K = proj.K;
  const d = [p[0] - t[0], p[1] - t[1], p[2] - t[2]];
  // camera coords = R^T d  (R rows dotted with d)
  const cx = R[0][0]*d[0] + R[1][0]*d[1] + R[2][0]*d[2];
  const cy = R[0][1]*d[0] + R[1][1]*d[1] + R[2][1]*d[2];
  const cz = R[0][2]*d[0] + R[1][2]*d[1] + R[2][2]*d[2];
  if (cz <= 0) return null;  // behind camera
  // scale K to the actual canvas size (image_size is the calibrated size)
  const [iw, ih] = proj.image_size || [W, H];
  const sx = W / iw, sy = H / ih;
  const u = (K[0][0]*cx/cz + K[0][2]) * sx;
  const v = (K[1][1]*cy/cz + K[1][2]) * sy;
  return [u, v];
}

// dark(now) -> bright(future) gradient, hue blue->cyan->green. Returns CSS rgb.
function traceColor(f) {
  const hue = 210 - 150 * f;         // 210(blue) -> 60(yellow-green)
  const light = 40 + 45 * f;          // dark -> bright
  return `hsl(${hue}, 90%, ${light}%)`;
}

function drawTrace(tileState, secs) {
  const { canvas, video, proj } = tileState;
  const ee = state.detail && state.detail.ee_traces;
  const cv = canvas.getContext("2d");
  // size canvas to displayed video box
  const W = canvas.clientWidth, H = canvas.clientHeight;
  if (canvas.width !== W || canvas.height !== H) { canvas.width = W; canvas.height = H; }
  cv.clearRect(0, 0, W, H);
  if (!proj || !ee || !state.eeTraceOn) return;

  const times = ee.time;
  if (!times || !times.length) return;
  // current index by playback time
  const dur = ee.duration_s || timelineDuration();
  let i0 = Math.round((secs / dur) * (times.length - 1));
  i0 = Math.max(0, Math.min(times.length - 1, i0));
  const span = Math.max(1, Math.round((EE_TRACE_HORIZON_S / dur) * (times.length - 1)));
  const step = Math.max(1, Math.round(span / EE_TRACE_STEPS));

  ee.arms.forEach((arm) => {
    const pts = [];
    for (let k = 0; k < EE_TRACE_STEPS; k++) {
      const i = Math.min(times.length - 1, i0 + k * step);
      const uv = projectPoint(arm.xyz[i], proj, W, H);
      if (uv) pts.push(uv);
    }
    if (pts.length < 1) return;
    // connecting line
    for (let k = 0; k < pts.length - 1; k++) {
      cv.strokeStyle = traceColor(k / (EE_TRACE_STEPS - 1));
      cv.lineWidth = 2.5;
      cv.beginPath(); cv.moveTo(pts[k][0], pts[k][1]); cv.lineTo(pts[k+1][0], pts[k+1][1]); cv.stroke();
    }
    // waypoint dots
    pts.forEach((p, k) => {
      cv.fillStyle = traceColor(k / (EE_TRACE_STEPS - 1));
      cv.beginPath(); cv.arc(p[0], p[1], k === 0 ? 5 : 3.5, 0, Math.PI * 2); cv.fill();
      cv.lineWidth = 1; cv.strokeStyle = "rgba(0,0,0,0.5)"; cv.stroke();
    });
  });
}

function drawAllTraces(secs) {
  state.tiles.forEach((t) => { if (t.ready && t.proj) drawTrace(t, secs); });
}

function updateTransportUI(secs) {
  const dur = timelineDuration();
  const label = dur ? `${secs.toFixed(1)}s / ${dur.toFixed(1)}s` : `${secs.toFixed(1)}s`;
  $("#time-label").textContent = label;
  const sc = $("#scrubber");
  if (dur && document.activeElement !== sc) {
    sc.value = String(Math.round((secs / dur) * 1000));
  }
}

// Consistent empty-state across every metadata section: rather than hide a card
// or silently drop it, always render the section and say the data isn't available
// for this episode. Messages stay source-agnostic (no raiden-only filenames).
function notAvailable(container, msg) {
  container.innerHTML = "";
  container.appendChild(el("div", "subtle empty-note", msg || "Not available for this episode."));
}

function renderMeta(md, d) {
  const grid = $("#meta-grid");
  grid.innerHTML = "";
  const rs = (d.robot && d.robot.summary) || {};
  const rows = [
    ["Teacher", md.teacher_name],
    ["Station", md.station_name],
    ["Control", md.control],
    // Duration/frames/rate: prefer episode metadata (raiden), else robot summary (yam).
    ["Duration", md.duration_s != null ? `${md.duration_s.toFixed(2)} s`
                 : rs.duration_s != null ? `${rs.duration_s.toFixed(2)} s` : null],
    ["Robot frames", md.robot_frames != null ? md.robot_frames : rs.num_steps],
    ["Robot rate", md.robot_hz != null ? `${md.robot_hz} Hz`
                   : rs.hz != null ? `${rs.hz} Hz` : null],
    ["Control rate", md.control_hz != null ? `${md.control_hz} Hz` : null],
    ["Camera FPS", md.camera_fps],
    ["Arm", md.arm_type],
    ["Cameras", (d.cameras || []).length || null],
    ["Subtasks", md.num_annotations || null],
    ["Timestamp", md.timestamp ? md.timestamp.replace("T", " ").slice(0, 19) : null],
    ["Converted", md.converted != null ? String(md.converted) : null],
  ];
  let shown = 0;
  rows.forEach(([k, val]) => {
    if (val == null || val === "") return;
    const row = el("div", "meta-row");
    row.appendChild(el("div", "meta-key", k));
    const isMono = k === "Timestamp" || k === "Robot frames";
    row.appendChild(el("div", "meta-val" + (isMono ? " mono" : ""), String(val)));
    grid.appendChild(row);
    shown++;
  });
  if (!shown) notAvailable(grid, "No metadata available for this episode.");
}

// Subtask annotations (timestamped). The card is always shown; when an episode
// has none, it says so rather than vanishing — consistent with the other sections.
function renderAnnotations(anns) {
  const body = $("#annotations-body");
  if (!body) return;
  if (!anns.length) {
    notAvailable(body, "No subtask annotations for this episode.");
    return;
  }
  body.innerHTML = "";
  anns.forEach((a) => {
    const row = el("div", "ann-row");
    row.appendChild(el("span", "ann-t mono", a.t != null ? `${a.t.toFixed(1)}s` : "—"));
    row.appendChild(el("span", "ann-text", a.text || ""));
    body.appendChild(row);
  });
}

/* ---------------- Robot trajectory plots ---------------- */

const PALETTE = ["#6ea8fe", "#f472b6", "#4ade80", "#fbbf24", "#a78bfa", "#22d3ee", "#fb923c"];

function renderPlots(robot) {
  const wrap = $("#plots");
  wrap.innerHTML = "";
  state.plots = [];
  state.robotDuration = 0;
  if (!robot || !robot.signals || !Object.keys(robot.signals).length) {
    $("#plot-summary").textContent = "";
    notAvailable(wrap, "No robot trajectories available for this episode.");
    return;
  }
  const s = robot.summary || {};
  state.robotDuration = s.duration_s || 0;
  $("#plot-summary").textContent =
    [s.num_steps != null ? `${s.num_steps} steps` : null,
     s.duration_s != null ? `${s.duration_s}s` : null,
     s.hz != null ? `${s.hz} Hz` : null].filter(Boolean).join(" · ");

  const t = robot.time || [];
  // Show the most informative signals: position + gripper commands, both arms.
  const order = Object.keys(robot.signals).sort(plotPriority);
  order.forEach((key) => {
    const sig = robot.signals[key];
    if (!sig.series || !sig.series.length) return;
    const block = el("div", "plot-block");
    const title = el("div", "plot-title");
    title.appendChild(el("span", null, prettySignal(key)));
    title.appendChild(el("span", "range", `[${sig.min}, ${sig.max}]`));
    block.appendChild(title);

    // Two stacked canvases: static series underneath, thin playback cursor on top.
    const wrapC = el("div", "plot-canvas-wrap");
    const series = el("canvas", "plot-series");
    const cursor = el("canvas", "plot-cursor");
    wrapC.appendChild(series);
    wrapC.appendChild(cursor);
    block.appendChild(wrapC);
    if (sig.dims > 1) block.appendChild(makeLegend(sig.dims));
    wrap.appendChild(block);

    // defer draw so canvases have layout dimensions
    requestAnimationFrame(() => {
      drawSeries(series, t, sig);
      const c = sizeCanvas(cursor);
      state.plots.push(c);
    });
  });
}

// Size a canvas to its box (DPR-aware) and return a handle for cursor drawing.
function sizeCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.clientWidth, H = canvas.clientHeight;
  canvas.width = W * dpr; canvas.height = H * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  return { canvas, ctx, W, H };
}

const PLOT_PAD = { l: 6, r: 6 };  // must match drawSeries horizontal padding

// Draw the vertical playback cursor on every plot at time `secs`.
function drawAllCursors(secs) {
  const dur = timelineDuration();
  const frac = dur > 0 ? Math.min(1, Math.max(0, secs / dur)) : 0;
  state.plots.forEach((p) => {
    p.ctx.clearRect(0, 0, p.W, p.H);
    const x = PLOT_PAD.l + frac * (p.W - PLOT_PAD.l - PLOT_PAD.r);
    p.ctx.strokeStyle = "rgba(248,113,113,0.95)";
    p.ctx.lineWidth = 1.5;
    p.ctx.beginPath();
    p.ctx.moveTo(x, 0);
    p.ctx.lineTo(x, p.H);
    p.ctx.stroke();
  });
}

function plotPriority(a, b) {
  const rank = (k) => {
    if (/gripper_pos/.test(k)) return 0;
    if (/joint_pos_7d/.test(k)) return 1;
    if (/joint_cmd/.test(k)) return 2;
    if (/joint_pos/.test(k)) return 3;
    if (/vel/.test(k)) return 5;
    if (/eff/.test(k)) return 6;
    return 4;
  };
  return rank(a) - rank(b) || a.localeCompare(b);
}

function prettySignal(k) {
  return k
    .replace(/^follower_/, "")
    .replace(/^l_/, "left ")
    .replace(/^r_/, "right ")
    .replace(/_/g, " ");
}

function makeLegend(dims) {
  const leg = el("div", "legend");
  for (let i = 0; i < dims; i++) {
    const s = el("span");
    const sw = el("i");
    sw.style.background = PALETTE[i % PALETTE.length];
    s.appendChild(sw);
    s.appendChild(el("span", null, `${i}`));
    leg.appendChild(s);
  }
  return leg;
}

function drawSeries(canvas, t, sig) {
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.clientWidth, H = canvas.clientHeight;
  canvas.width = W * dpr; canvas.height = H * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, W, H);

  const pad = { l: 6, r: 6, t: 8, b: 8 };
  const series = sig.series; // [n][dims]
  const n = series.length;
  if (!n) return;
  let lo = sig.min, hi = sig.max;
  if (hi - lo < 1e-9) { hi += 0.5; lo -= 0.5; }
  const pane = 0.05 * (hi - lo);
  lo -= pane; hi += pane;

  const x = (i) => pad.l + (i / (n - 1 || 1)) * (W - pad.l - pad.r);
  const y = (v) => pad.t + (1 - (v - lo) / (hi - lo)) * (H - pad.t - pad.b);

  // zero baseline
  if (lo < 0 && hi > 0) {
    ctx.strokeStyle = "rgba(255,255,255,0.08)";
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(pad.l, y(0)); ctx.lineTo(W - pad.r, y(0)); ctx.stroke();
  }

  const dims = sig.dims;
  for (let d = 0; d < dims; d++) {
    ctx.strokeStyle = PALETTE[d % PALETTE.length];
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    for (let i = 0; i < n; i++) {
      const v = series[i][d];
      const px = x(i), py = y(v);
      if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    }
    ctx.stroke();
  }
}

/* ---------------- Calibration ---------------- */

function renderCalibration(calib, cameras) {
  const body = $("#calib-body");
  body.innerHTML = "";
  const card = $(".calib-card");
  if (!calib || !calib.cameras || !Object.keys(calib.cameras).length) {
    card.classList.add("collapsed");
    notAvailable(body, "No camera calibration available for this episode.");
    return;
  }
  // The "Check alignment" hint only makes sense when some camera actually offers
  // that overlay (needs base-frame extrinsics — raiden only, not the xdof sidecar).
  const anyOverlay = Object.values(calib.cameras).some((c) => c.extrinsics);
  $(".calib-hint").classList.toggle("hidden", !anyOverlay);
  Object.entries(calib.cameras).forEach(([name, c]) => {
    const box = el("div", "calib-cam");
    const h = el("h4", null, prettyCam(name));
    if (c.type) h.appendChild(el("span", "tag", c.type));
    box.appendChild(h);
    const cm = c.intrinsics && c.intrinsics.camera_matrix;
    if (cm) {
      kv(box, "fx", cm[0][0].toFixed(1));
      kv(box, "fy", cm[1][1].toFixed(1));
      kv(box, "cx", cm[0][2].toFixed(1));
      kv(box, "cy", cm[1][2].toFixed(1));
    }
    if (c.intrinsics && c.intrinsics.image_size) {
      kv(box, "size", c.intrinsics.image_size.join("×"));
    }
    if (c.serial_number) kv(box, "serial", String(c.serial_number));
    // Distortion (xdof sidecar carries it; raiden's rectified calib does not).
    if (c.distortion && c.distortion.length) {
      kv(box, "distortion", c.distortion.map((x) => x.toFixed(4)).join(", "));
      if (c.distortion_model) kv(box, "model", c.distortion_model);
    }
    if (c.baseline_m) kv(box, "baseline", `${(c.baseline_m * 1000).toFixed(1)} mm`);
    // Calibration check: only scene-type cameras carry base-frame extrinsics we
    // can project. A button renders the arm-base axes onto a still frame.
    if (c.extrinsics) {
      const btn = el("button", "calib-check-btn", "Check alignment");
      const holder = el("div", "calib-overlay-holder");
      btn.onclick = () => loadCalibOverlay(name, btn, holder);
      box.appendChild(btn);
      box.appendChild(holder);
    }
    body.appendChild(box);
  });
}

// Load the calibration overlay image for one camera into its holder.
function loadCalibOverlay(camera, btn, holder) {
  btn.disabled = true;
  btn.textContent = "Rendering…";
  const url = `${apiBase()}/tasks/${encodeURIComponent(state.task)}` +
    `/episodes/${encodeURIComponent(state.episode)}/calib?camera=${encodeURIComponent(camera)}`;
  const img = new Image();
  img.className = "calib-overlay-img";
  img.onload = () => { holder.innerHTML = ""; holder.appendChild(img); btn.textContent = "Refresh"; btn.disabled = false; };
  img.onerror = () => { btn.textContent = "Check alignment"; btn.disabled = false; toast("Could not render overlay for " + camera); };
  img.src = url + `&_=${state.episode}`;  // cache-key stable per episode
}

function kv(parent, k, v) {
  const row = el("div", "kv");
  row.appendChild(el("span", null, k));
  row.appendChild(el("span", null, v));
  parent.appendChild(row);
}

init();
