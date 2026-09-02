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
  facts: {},        // episode -> { timestamp, status } for the browse list labels
  detail: null,
  overviewTasks: [], // per-task rows from /overview (counts + collection span)
  taskSort: "episodes",  // Tasks-card sort: episodes | collected | name
  taskWho: "all",        // Tasks-card filter: all | teachers (robot teachers only)
  taskTeachers: null,    // { supported, building, tasks: {task: {teacher: {...}}}, robot_teachers }
  hoursInputs: null,     // last /stats pass, so a re-sort can refill the hours cells
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

    // Hash is #source/task/episode for shareable links. With no (or empty) hash,
    // land on the catalog gallery instead of jumping into a source.
    const [hSrc, hTask, hEp] = decodeURIComponent(location.hash.slice(1)).split("/");
    if (hSrc && sources.some((s) => s.id === hSrc)) {
      await selectSource(hSrc, hTask || null, hEp || null);
    } else {
      showCatalog();
    }
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
  $("#brand-home").addEventListener("click", showCatalog);
  // Catalog comparison-chart metric toggle (Episodes / Hours / Tasks).
  $("#cat-metric-toggle").addEventListener("click", (ev) => {
    const b = ev.target.closest("button");
    if (!b) return;
    document.querySelectorAll("#cat-metric-toggle button").forEach((x) => x.classList.toggle("active", x === b));
    drawCatalogBars(b.dataset.metric);
  });
  // Upload contribution-calendar metric toggle (Data / Episodes / Files).
  $("#contrib-metric-toggle").addEventListener("click", (ev) => {
    const b = ev.target.closest("button");
    if (!b) return;
    document.querySelectorAll("#contrib-metric-toggle button").forEach((x) => x.classList.toggle("active", x === b));
    drawContrib(b.dataset.metric);
  });
  // Raiden teleop-per-day: teacher picker + metric (episodes / minutes) toggle.
  $("#teacher-select").addEventListener("change", (ev) => {
    state.teacherFilter = ev.target.value;
    drawTeachers(state.teacherMetric || "episodes");
  });
  $("#teacher-metric-toggle").addEventListener("click", (ev) => {
    const b = ev.target.closest("button");
    if (!b) return;
    document.querySelectorAll("#teacher-metric-toggle button").forEach((x) => x.classList.toggle("active", x === b));
    drawTeachers(b.dataset.metric);
  });
  // Tasks-card sort (Episodes / Collected / Name) — re-sorts the loaded rows, no refetch.
  $("#ov-task-sort").addEventListener("click", (ev) => {
    const b = ev.target.closest("button");
    if (!b) return;
    document.querySelectorAll("#ov-task-sort button").forEach((x) => x.classList.toggle("active", x === b));
    state.taskSort = b.dataset.sort;
    renderTaskList();
  });
  // Tasks-card scope (All / Robot teachers) — same loaded rows, narrowed.
  $("#ov-task-who").addEventListener("click", (ev) => {
    const b = ev.target.closest("button");
    if (!b) return;
    document.querySelectorAll("#ov-task-who button").forEach((x) => x.classList.toggle("active", x === b));
    state.taskWho = b.dataset.who;
    renderTaskList();
  });
  // Episode navigation: prev/next buttons, slider scrub, and ←/→ arrow keys.
  $("#ep-prev").addEventListener("click", () => stepEpisode(-1));
  $("#ep-next").addEventListener("click", () => stepEpisode(1));
  $("#ep-slider").addEventListener("input", (ev) => {
    const ep = state.episodes[parseInt(ev.target.value, 10)];
    if (ep && ep !== state.episode) selectEpisode(ep);
  });
  document.addEventListener("keydown", (ev) => {
    // Only when viewing an episode, and not while typing in the search/filter inputs.
    if (state.episode == null) return;
    const t = ev.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT")) return;
    if (ev.key === "ArrowLeft") { ev.preventDefault(); stepEpisode(-1); }
    else if (ev.key === "ArrowRight") { ev.preventDefault(); stepEpisode(1); }
  });
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
  state.hoursInputs = null;   // the previous dataset's per-task hours don't apply here
  state.taskTeachers = null;  // nor its task→teacher rollup
  clearTimeout(state._catTimer);   // stop catalog polling once we enter a source
  clearTimeout(state._taskTeacherTimer);
  document.body.classList.remove("catalog-mode");  // reveal the episode-browser sidebar
  $("#catalog-view").classList.add("hidden");
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
  $("#catalog-view").classList.add("hidden");
  $("#episode-view").classList.add("hidden");
  $("#overview-view").classList.remove("hidden");
  renderOverview();
}

/* ---------------- Catalog (landing gallery) ---------------- */

function showCatalog() {
  stopPlayback();
  state.source = null;
  state.episode = null;
  location.hash = "";
  // Catalog mode: the sidebar's episode-browsing controls (dataset/task/episode
  // pickers) are meaningless here, so collapse them (CSS keys off this class).
  document.body.classList.add("catalog-mode");
  $("#overview-view").classList.add("hidden");
  $("#episode-view").classList.add("hidden");
  $("#catalog-view").classList.remove("hidden");
  renderCatalog();
}

const CAT_KIND_LABEL = {
  raiden: "ZED .svo2", yam: "MCAP", lerobot: "LeRobot v3.0",
  lerobot_single: "LeRobot v3.0",
};

// Minimal HTML escape — card templates go through innerHTML, and a card's error
// string is server-supplied text (an S3 /botocore message), not markup.
function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (ch) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
  ));
}

// A card whose deep build threw. Without this the card is indistinguishable from a
// slow one: both render blank metrics, so a real failure looks like patience.
function failBadge(msg) {
  const short = String(msg ?? "build failed");
  return `<span class="cat-badge failed" title="${esc(short)}">⚠ build failed</span>`;
}

// How often to ask whether a clip has finished decoding. Decodes run minutes, so
// this only needs to be fast enough that a ready clip does not feel delayed.
const CLIP_POLL_MS = 1500;

function annBadge(a) {
  const map = {
    yes: ["ann-yes", "✓ annotations"],
    none: ["ann-none", "no annotations"],
    unsupported: ["ann-na", "n/a"],
    unknown: ["ann-na", "annotations ?"],
  };
  const [cls, txt] = map[a] || ["ann-na", "annotations ?"];
  return `<span class="cat-badge ${cls}">${txt}</span>`;
}

async function renderCatalog() {
  const grid = $("#cat-grid");
  const agg = $("#cat-agg");
  if (!grid.dataset.init) { grid.innerHTML = `<div class="subtle">Loading datasets…</div>`; grid.dataset.init = "1"; }
  let data;
  try { data = await api("/api/catalog"); }
  catch (e) { grid.innerHTML = `<div class="subtle">Failed to load catalog: ${e.message}</div>`; return; }
  if (state.source) return;  // user navigated away while awaiting

  const a = data.aggregate;
  agg.innerHTML = [
    ["Datasets", a.num_datasets],
    ["Episodes", (a.total_episodes || 0).toLocaleString()],
    ["Tasks", (a.total_tasks || 0).toLocaleString()],
    // Sums per-card hours, which are estimates for any sampled source — so the
    // total is an estimate too the moment one of them is.
    ["Hours", a.total_hours
      ? `${data.datasets.some((d) => d.sampled) ? "~" : ""}${a.total_hours.toLocaleString()}`
      : "—"],
    ["With annotations", a.with_annotations],
  ].map(([k, v]) => `<div class="cat-stat"><div class="cat-stat-v">${v}</div><div class="cat-stat-k">${k}</div></div>`).join("");

  grid.innerHTML = "";
  data.datasets.forEach((c) => {
    const card = document.createElement("div");
    card.className = "cat-card";
    const fmt = CAT_KIND_LABEL[c.kind] || c.kind || "";
    const building = c.building;
    const failed = c.built_ok === false;
    // A sampled source's hours are EXTRAPOLATED from the sampled mean, not summed,
    // so prefix them. Presenting an estimate as a measurement is the one thing the
    // sampling trade-off must not do.
    const hours = c.total_hours != null
      ? `${c.sampled ? "~" : ""}${c.total_hours} h`
      : (building ? "…" : "—");
    const hoursTitle = c.sampled
      ? ` title="Estimated: mean of ${(c.scanned || 0).toLocaleString()} sampled episodes, scaled to ${(c.num_episodes || 0).toLocaleString()}"`
      : "";
    const cams = (c.cameras && c.cameras.length) ? c.cameras.join(", ") : (building ? "…" : "—");
    card.innerHTML = `
      <div class="cat-card-head">
        <div class="cat-card-title">${c.label}</div>
        <span class="cat-fmt">${fmt}</span>
      </div>
      <div class="cat-metrics">
        <div><b>${(c.num_episodes ?? "—").toLocaleString?.() ?? c.num_episodes ?? "—"}</b><span>episodes</span></div>
        <div><b>${c.num_tasks ?? "—"}</b><span>tasks</span></div>
        <div${hoursTitle}><b>${hours}</b><span>duration</span></div>
      </div>
      <div class="cat-row">${building
        ? '<span class="cat-badge building">⟳ computing…</span>'
        : failed ? failBadge(c.error) : annBadge(c.annotations)}</div>
      <div class="cat-cams subtle mono">${cams}</div>
      ${c.bucket ? `<div class="cat-prefix subtle mono">s3://${esc(c.bucket)}/${esc(c.prefix ?? "")}</div>` : ""}`;
    card.addEventListener("click", () => selectSource(c.id));
    grid.appendChild(card);
  });

  // Stash the cards for the comparison bar chart (redrawn on metric toggle / resize).
  state.catalog = data.datasets;
  drawCatalogBars(state.catMetric || "episodes");
  loadContrib();   // upload-activity calendar (independent /api/contrib scan)
  loadTeachers();  // raiden teleop-per-day-by-teacher bars (/api/raiden_teachers)

  $("#cat-hint").textContent = a.building ? `${a.building} computing…` : `${a.num_datasets} datasets`;
  // Poll while any dataset's deep summary is still building.
  if (a.building > 0 && !state.source) {
    clearTimeout(state._catTimer);
    state._catTimer = setTimeout(() => { if (!state.source) renderCatalog(); }, 4000);
  }
}

// Horizontal bar chart comparing datasets by a chosen metric. Matches the app's
// existing canvas charts (setupCanvas/niceTicks, #6ea8fe bars, mono tick labels).
const CAT_METRICS = {
  episodes: { key: "num_episodes", label: "episodes", fmt: (v) => v.toLocaleString() },
  hours: { key: "total_hours", label: "hours", fmt: (v) => (v >= 100 ? Math.round(v) : v.toFixed(1)) },
  tasks: { key: "num_tasks", label: "tasks", fmt: (v) => String(v) },
};

function drawCatalogBars(metric) {
  state.catMetric = metric;
  const canvas = $("#cat-bar-canvas");
  if (!canvas || !state.catalog) return;
  const m = CAT_METRICS[metric] || CAT_METRICS.episodes;
  // Rows for datasets that have this metric (skip still-building/absent values).
  const rows = state.catalog
    .map((c) => ({ label: c.label, v: c[m.key] }))
    .filter((r) => typeof r.v === "number" && r.v > 0)
    .sort((a, b) => b.v - a.v);
  // Size the canvas: fixed row height so the card grows with dataset count.
  const rowH = 30, padTop = 8, padBot = 26;
  canvas.style.height = (rows.length * rowH + padTop + padBot) + "px";
  const { ctx, W, H } = setupCanvas("cat-bar-canvas");
  if (!rows.length) {
    ctx.fillStyle = "#626875"; ctx.font = "12px 'Inter', sans-serif"; ctx.textAlign = "left";
    ctx.fillText("computing…", 8, 20);
    return;
  }
  const maxV = Math.max(...rows.map((r) => r.v));
  const labelW = 168, valW = 92;
  const x0 = labelW, plotW = W - labelW - valW;
  // x gridlines + axis ticks. IMPORTANT: the axis max must be >= maxV, else the
  // largest bar (scaled by v/tickMax) overshoots past the plot area. niceTicks
  // stops at the last tick <= maxV, so extend it by one step to a nice ceiling.
  ctx.font = "10px 'JetBrains Mono', monospace";
  let ticks = niceTicks(0, maxV, 4);
  const step = ticks.length > 1 ? ticks[1] - ticks[0] : (maxV || 1);
  let tickMax = ticks[ticks.length - 1] || maxV;
  while (tickMax < maxV) { tickMax += step; ticks.push(tickMax); }
  ticks.forEach((v) => {
    const x = x0 + plotW * (v / tickMax);
    ctx.strokeStyle = "rgba(255,255,255,0.06)";
    ctx.beginPath(); ctx.moveTo(x, padTop); ctx.lineTo(x, H - padBot + 4); ctx.stroke();
    ctx.fillStyle = "#626875"; ctx.textAlign = "center";
    ctx.fillText(m.fmt(v), x, H - padBot + 18);
  });
  // bars + labels
  rows.forEach((r, i) => {
    const y = padTop + i * rowH;
    const bh = rowH - 10;
    const bw = Math.max(2, plotW * (r.v / tickMax));
    // dataset label (right-aligned, truncated by clip)
    ctx.fillStyle = "#e7e9ee"; ctx.font = "12px 'Inter', sans-serif"; ctx.textAlign = "right";
    ctx.fillText(r.label.length > 24 ? r.label.slice(0, 23) + "…" : r.label, labelW - 12, y + bh / 2 + 4);
    // bar
    ctx.fillStyle = "#6ea8fe";
    roundRect(ctx, x0, y + 2, bw, bh, 3); ctx.fill();
    // value at bar end
    ctx.fillStyle = "#9aa0ad"; ctx.font = "11px 'JetBrains Mono', monospace"; ctx.textAlign = "left";
    ctx.fillText(m.fmt(r.v), x0 + bw + 6, y + bh / 2 + 4);
  });
}

function roundRect(ctx, x, y, w, h, r) {
  r = Math.min(r, h / 2, w / 2);
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

/* ---------------- Upload contribution calendar (GitHub-style) ---------------- */
// Human-readable bytes for the "Data" metric + tooltips.
function fmtBytes(n) {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB", "PB"];
  let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)} ${u[i]}`;
}

const CONTRIB_METRICS = {
  bytes: { key: "bytes", label: "uploaded", fmt: fmtBytes },
  episodes: { key: "episodes", label: "episodes", fmt: (v) => v.toLocaleString() },
  files: { key: "files", label: "files", fmt: (v) => v.toLocaleString() },
};

// Fetch the merged upload calendar and (re)draw. Polls while any source is still
// scanning, mirroring the catalog's building/poll pattern.
async function loadContrib() {
  let data;
  try { data = await api("/api/contrib"); }
  catch (e) { return; }               // calendar is non-critical; leave silent on error
  if (state.source) return;           // navigated into a source meanwhile
  state.contrib = data;
  renderContribFilter();
  drawContrib(state.contribMetric || "bytes");
  if (data.building > 0 && !state.source) {
    clearTimeout(state._contribTimer);
    state._contribTimer = setTimeout(() => { if (!state.source) loadContrib(); }, 4000);
  }
}

// Dataset-subset chips above the calendar: "All" + one per built dataset. Clicking
// one scopes the graph to that dataset's uploads (re-merged client-side, no fetch).
function renderContribFilter() {
  const host = $("#contrib-filter");
  const data = state.contrib;
  if (!host || !data) return;
  const built = (data.datasets || []).filter((d) => !d.building && d.built_ok !== false);
  // "All" + each dataset, with an upload-day count as a hint.
  const cur = state.contribFilter || "all";
  const chips = [{ id: "all", label: "All datasets", n: (data.totals || {}).days_active }]
    .concat(built.map((d) => ({ id: d.id, label: d.label, n: Object.keys(d.days || {}).length })));
  host.innerHTML = chips.map((c) =>
    `<button class="contrib-chip ${c.id === cur ? "active" : ""}" data-cid="${c.id}">` +
    `${c.label}<span class="cc-n">${c.n != null ? c.n + "d" : ""}</span></button>`
  ).join("");
  // Bind once; delegate clicks.
  if (!host.dataset.bound) {
    host.dataset.bound = "1";
    host.addEventListener("click", (ev) => {
      const b = ev.target.closest(".contrib-chip");
      if (!b) return;
      state.contribFilter = b.dataset.cid;
      renderContribFilter();
      drawContrib(state.contribMetric || "bytes");
    });
  }
}

// Resolve the calendar view for the current dataset-subset filter: returns the
// day-rollup + span + totals for either everything (merged, filter="all") or one
// dataset. Keeps drawContrib agnostic to whether it's showing all or a subset.
function contribView() {
  const data = state.contrib || {};
  const f = state.contribFilter || "all";
  if (f === "all") {
    return { days: data.days || {}, span: data.span || {}, totals: data.totals || {},
             counts_episodes: true, building: data.building || 0, label: "all datasets" };
  }
  const ds = (data.datasets || []).find((d) => d.id === f);
  if (!ds || ds.building) return { days: {}, span: {}, totals: {}, counts_episodes: true, building: 1, label: f };
  const dk = Object.keys(ds.days || {}).sort();
  return {
    days: ds.days || {}, span: ds.span || {},
    totals: { ...(ds.totals || {}), days_active: dk.length },
    counts_episodes: ds.counts_episodes !== false, building: 0, label: ds.label,
  };
}

// Render the year-of-weeks heatmap into #contrib-cal for a chosen metric.
function drawContrib(metric) {
  state.contribMetric = metric;
  const host = $("#contrib-cal");
  if (!host || !state.contrib) return;
  const view = contribView();       // all-datasets or a single-dataset subset
  const m = CONTRIB_METRICS[metric] || CONTRIB_METRICS.bytes;

  const first = view.span && view.span.first, last = view.span && view.span.last;
  if (!first || !last) {
    host.innerHTML = `<div class="contrib-empty">${view.building ? "Scanning uploads…" : "No dated uploads found."}</div>`;
    $("#contrib-summary").textContent = view.building ? `${view.building} scanning…` : "";
    return;
  }

  // Build the day grid from the Sunday on/before `first` to the Saturday on/after
  // `last`, laid out as columns of weeks (GitHub style). Dates are handled in UTC
  // to match S3's LastModified day bucketing (avoids TZ off-by-one at week edges).
  const val = (day) => { const d = view.days[day]; return d ? (d[m.key] || 0) : 0; };
  const parse = (s) => new Date(s + "T00:00:00Z");
  const iso = (dt) => dt.toISOString().slice(0, 10);
  const start = parse(first); start.setUTCDate(start.getUTCDate() - start.getUTCDay());  // back to Sunday
  const end = parse(last); end.setUTCDate(end.getUTCDate() + (6 - end.getUTCDay()));      // fwd to Saturday

  // Intensity thresholds: quartiles of the NONZERO daily values so a few big days
  // don't wash everything into bucket 1 (linear scaling would).
  const nz = [];
  for (let dt = new Date(start); dt <= end; dt.setUTCDate(dt.getUTCDate() + 1)) {
    const v = val(iso(dt)); if (v > 0) nz.push(v);
  }
  nz.sort((a, b) => a - b);
  const q = (p) => nz.length ? nz[Math.min(nz.length - 1, Math.floor(p * nz.length))] : 0;
  const th = [q(0.25), q(0.5), q(0.75), q(0.9)];
  const bucket = (v) => {
    if (v <= 0) return 0;
    if (v <= th[0]) return 1;
    if (v <= th[1]) return 2;
    if (v <= th[2]) return 3;
    return 4;
  };

  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const weeks = [];       // array of {days:[{iso,v,inRange}], month:label|null}
  let cur = new Date(start), lastMonth = -1;
  while (cur <= end) {
    const wk = { days: [], month: null };
    for (let d = 0; d < 7; d++) {
      const key = iso(cur);
      const inRange = key >= first && key <= last;
      wk.days.push({ iso: key, v: val(key), inRange });
      cur.setUTCDate(cur.getUTCDate() + 1);
    }
    // Month label above a week when its first day starts a new month.
    const mo = parse(wk.days[0].iso).getUTCMonth();
    if (mo !== lastMonth) { wk.month = MONTHS[mo]; lastMonth = mo; }
    weeks.push(wk);
  }

  // Build DOM: month row + [day-of-week gutter | week columns].
  const monthRow = weeks.map((w) => `<span style="width:15px">${w.month || ""}</span>`).join("");
  const dow = ["", "Mon", "", "Wed", "", "Fri", ""].map((d) => `<span>${d}</span>`).join("");
  const cells = weeks.map((w) => {
    const col = w.days.map((d) => {
      if (!d.inRange) return `<i class="contrib-cell empty"></i>`;
      const lv = bucket(d.v);
      const title = d.v > 0 ? `${d.iso}: ${m.fmt(d.v)} ${m.label}` : `${d.iso}: no uploads`;
      return `<i class="contrib-cell contrib-day l${lv}" title="${title}"></i>`;
    }).join("");
    return `<div class="contrib-week">${col}</div>`;
  }).join("");

  host.innerHTML =
    `<div class="contrib-months">${monthRow}</div>` +
    `<div class="contrib-body"><div class="contrib-dow">${dow}</div><div class="contrib-weeks">${cells}</div></div>`;

  const t = view.totals || {};
  const totalForMetric = m.key === "bytes" ? fmtBytes(t.bytes) :
    (m.key === "episodes" ? (t.episodes || 0).toLocaleString() : (t.files || 0).toLocaleString());
  // Episode-count caveat: for "all", count packed datasets that aren't episode-
  // counted; for a single dataset, flag it directly if it's a packed format.
  const f = state.contribFilter || "all";
  let notEpNote = "";
  if (m.key === "episodes") {
    if (f === "all") {
      const n = (state.contrib.datasets || [])
        .filter((d) => d.built_ok !== false && d.counts_episodes === false).length;
      if (n) notEpNote = ` · ${n} packed dataset(s) not episode-counted`;
    } else if (!view.counts_episodes) {
      notEpNote = ` · packed format — episodes not counted (see Data/Files)`;
    }
  }
  const scope = f === "all" ? "" : ` — ${view.label}`;
  $("#contrib-summary").textContent =
    `${totalForMetric} ${m.label}${scope} across ${t.days_active || 0} active days` +
    (view.building ? ` · scanning…` : "") + notEpNote;
}

/* -------- Raiden teleop-per-day, toggled by teacher (bar chart) -------- */
// Fetch the teacher-by-day rollup and (re)draw. Polls while raiden sources scan.
async function loadTeachers() {
  let data;
  try { data = await api("/api/raiden_teachers"); }
  catch (e) { return; }
  if (state.source) return;
  state.teachers = data;
  // Populate the teacher <select> (All + each teacher, episode-sorted) once we
  // have data; preserve the current selection across polls.
  const sel = $("#teacher-select");
  if (sel) {
    const cur = state.teacherFilter || "all";
    const tot = data.totals_by_teacher || {};
    const opts = [`<option value="all">All teachers</option>`].concat(
      (data.teachers || []).map((t) =>
        `<option value="${t}">${t} (${(tot[t] || {}).episodes || 0})</option>`));
    sel.innerHTML = opts.join("");
    sel.value = (data.teachers || []).includes(cur) || cur === "all" ? cur : "all";
    state.teacherFilter = sel.value;
  }
  drawTeachers(state.teacherMetric || "episodes");
  if (data.building > 0 && !state.source) {
    clearTimeout(state._teacherTimer);
    state._teacherTimer = setTimeout(() => { if (!state.source) loadTeachers(); }, 4000);
  }
}

const TEACHER_METRICS = {
  episodes: { label: "episodes", fmt: (v) => v.toLocaleString(),
              pick: (rec) => rec.episodes || 0 },
  minutes: { label: "minutes", fmt: (v) => (v >= 100 ? Math.round(v) : v.toFixed(1)),
             pick: (rec) => (rec.seconds || 0) / 60 },
};

// Vertical per-day bars of raiden teleop volume, for the selected teacher (or all).
function drawTeachers(metric) {
  state.teacherMetric = metric;
  const data = state.teachers;
  const canvas = $("#teacher-canvas");
  if (!canvas || !data) return;
  const m = TEACHER_METRICS[metric] || TEACHER_METRICS.episodes;
  const who = state.teacherFilter || "all";

  const first = data.span && data.span.first, last = data.span && data.span.last;
  const summary = $("#teacher-summary");
  const { ctx, W, H } = setupCanvas("teacher-canvas");
  if (!first || !last) {
    ctx.fillStyle = "#626875"; ctx.font = "12px 'Inter', sans-serif"; ctx.textAlign = "left";
    ctx.fillText(data.building ? "Scanning raiden metadata…" : "No raiden teleop found.", 8, 22);
    if (summary) summary.textContent = data.building ? `${data.building} scanning…` : "";
    return;
  }

  // Build a continuous daily series first..last (gaps = 0), summing the chosen
  // metric for the selected teacher (or across all teachers).
  const parse = (s) => new Date(s + "T00:00:00Z");
  const iso = (dt) => dt.toISOString().slice(0, 10);
  const dayVal = (day) => {
    const per = data.days[day]; if (!per) return 0;
    if (who === "all") return Object.values(per).reduce((s, r) => s + m.pick(r), 0);
    return per[who] ? m.pick(per[who]) : 0;
  };
  const days = [];
  for (let dt = parse(first); dt <= parse(last); dt.setUTCDate(dt.getUTCDate() + 1)) {
    const k = iso(dt); days.push({ day: k, v: dayVal(k) });
  }
  const maxV = Math.max(1, ...days.map((d) => d.v));
  const total = days.reduce((s, d) => s + d.v, 0);
  const active = days.filter((d) => d.v > 0).length;

  // Plot: y-axis ticks on the left, one bar per day left→right.
  const padL = 40, padR = 8, padTop = 10, padBot = 26;
  const plotW = W - padL - padR, plotH = H - padTop - padBot;
  ctx.font = "10px 'JetBrains Mono', monospace";
  let ticks = niceTicks(0, maxV, 4);
  const step = ticks.length > 1 ? ticks[1] - ticks[0] : (maxV || 1);
  let tickMax = ticks[ticks.length - 1] || maxV;
  while (tickMax < maxV) { tickMax += step; ticks.push(tickMax); }
  ticks.forEach((v) => {
    const y = padTop + plotH * (1 - v / tickMax);
    ctx.strokeStyle = "rgba(255,255,255,0.06)";
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W - padR, y); ctx.stroke();
    ctx.fillStyle = "#626875"; ctx.textAlign = "right";
    ctx.fillText(m.fmt(v), padL - 6, y + 3);
  });
  // bars
  const n = days.length;
  const slot = plotW / n, bw = Math.max(1, Math.min(18, slot - 2));
  days.forEach((d, i) => {
    if (d.v <= 0) return;
    const x = padL + i * slot + (slot - bw) / 2;
    const bh = plotH * (d.v / tickMax);
    const y = padTop + plotH - bh;
    ctx.fillStyle = "#6ea8fe";
    roundRect(ctx, x, y, bw, bh, 2); ctx.fill();
  });
  // sparse x date labels (first, last, and mid) to avoid clutter
  ctx.fillStyle = "#626875"; ctx.font = "9px 'JetBrains Mono', monospace"; ctx.textAlign = "center";
  const idxs = n <= 1 ? [0] : [0, Math.floor((n - 1) / 2), n - 1];
  [...new Set(idxs)].forEach((i) => {
    const x = padL + i * slot + slot / 2;
    ctx.fillText(days[i].day.slice(5), x, H - padBot + 16);  // MM-DD
  });

  if (summary) {
    const label = who === "all" ? "all teachers" : who;
    summary.textContent = `${m.fmt(total)} ${m.label} — ${label} across ${active} active days` +
      (data.building ? ` · scanning…` : "");
  }
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

    state.overviewTasks = ov.tasks;  // per-task totals, for extrapolating hours
    state.numTasks = ov.num_tasks;
    renderTaskList();
    loadTaskTeachers();   // reveals the robot-teacher filter when its scan is ready
    renderAnalytics(ov.tasks.map((t) => t.task));
  } catch (e) {
    toast("Failed to load overview: " + e.message);
  }
}

/* ---------------- Overview: per-task breakdown ---------------- */

// Sort comparators for the Tasks card. "collected" is newest-collected first (the
// useful direction: what was recorded most recently); tasks whose format carries no
// timestamp sort last so they never displace dated ones.
const TASK_SORTS = {
  episodes: (a, b) => b.episodes - a.episodes,
  name: (a, b) => a.task.localeCompare(b.task),
  collected: (a, b) => {
    const av = a.collected_end || "", bv = b.collected_end || "";
    if (!av && !bv) return b.episodes - a.episodes;
    if (!av) return 1;
    if (!bv) return -1;
    return bv.localeCompare(av);   // ISO8601 sorts lexicographically
  },
};

function renderTaskList() {
  const all = state.overviewTasks || [];
  const list = $("#ov-task-list");
  list.innerHTML = "";
  const mode = state.taskSort || "episodes";
  // Hide the Collected sort where the format has no capture timestamps (LeRobot):
  // offering a sort that can't order anything would just look broken.
  const anyDated = all.some((t) => t.collected_end);
  $("#ov-task-sort").classList.toggle("hidden", !anyDated);
  // Same for the robot-teacher filter: only shown once the teacher scan for a
  // teacher-recording source has landed.
  const tt = state.taskTeachers;
  const canFilterByTeacher = !!(tt && tt.supported && !tt.building && Object.keys(tt.tasks).length);
  $("#ov-task-who").classList.toggle("hidden", !canFilterByTeacher);
  const teacherOnly = canFilterByTeacher && state.taskWho === "teachers";
  const tasks = teacherOnly ? all.filter((t) => taskTeacherNames(t.task).robot.length) : all;

  const sorted = tasks.slice().sort(TASK_SORTS[mode] || TASK_SORTS.episodes);
  $("#ov-task-hint").textContent = teacherOnly
    ? `${tasks.length} of ${state.numTasks ?? all.length}`
    : `${state.numTasks ?? all.length} total`;
  // Bar scale stays on the unfiltered max, so a task's bar means the same thing in
  // either view instead of rescaling when you toggle.
  const maxEp = Math.max(1, ...all.map((t) => t.episodes));
  sorted.forEach((t) => {
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
    row.appendChild(taskWhenCell(t));
    if (canFilterByTeacher) {
      const who = taskTeacherNames(t.task);
      if (who.all.length) row.title = `teleoperated by ${who.all.join(", ")}`;
    }
    row.onclick = () => selectTask(t.task);
    list.appendChild(row);
  });
  if (teacherOnly && !sorted.length) {
    list.appendChild(el("div", "subtle empty-note",
      "No tasks recorded by the robot teachers in this dataset."));
  }
  // Rows were rebuilt, so their hours cells are back to "…" — refill from the last
  // stats pass if it already landed (a re-sort must not lose them).
  const h = state.hoursInputs;
  if (h) updatePerTaskHours(h.eps, h.stats, h.estimated);
}

// Who teleoperated a task: every teacher with episodes in it (most episodes first),
// and which of them are robot teachers. "unknown" (episodes with no teacher_name
// recorded) is reported but never counts as a robot teacher.
function taskTeacherNames(task) {
  const tt = state.taskTeachers;
  const per = (tt && tt.tasks && tt.tasks[task]) || {};
  const roster = new Set((tt?.robot_teachers || []).map((t) => t.toLowerCase()));
  const names = Object.keys(per).sort((a, b) => per[b].episodes - per[a].episodes);
  return {
    all: names.map((n) => `${n} (${per[n].episodes})`),
    robot: names.filter((n) => roster.has(n.toLowerCase())),
  };
}

// Which tasks each teacher worked on — an exact rollup off the same scan that feeds
// the per-day teacher chart, so it isn't limited to the sampled stats pass. Polls
// while that scan is building; never fatal (the filter just stays hidden).
async function loadTaskTeachers() {
  const forSource = state.source;
  try {
    const r = await api(`${apiBase()}/task-teachers`);
    if (forSource !== state.source) return;   // switched away; drop stale result
    state.taskTeachers = r;
    renderTaskList();
    if (r.supported && r.building) {
      clearTimeout(state._taskTeacherTimer);
      state._taskTeacherTimer = setTimeout(() => {
        if (state.source === forSource) loadTaskTeachers();
      }, 5000);
    }
  } catch (_) { /* filter stays hidden */ }
}

// The date cell: the collection span from the backend (start–end, or a single date
// for a one-day task), falling back to the date parsed out of the latest episode's
// name for formats that report no timestamps.
function taskWhenCell(t) {
  const start = t.collected_start ? t.collected_start.slice(0, 10) : null;
  const end = t.collected_end ? t.collected_end.slice(0, 10) : null;
  if (!start && !end) {
    const parsed = t.latest ? parseEpisodeName(t.latest).when : null;
    return el("div", "t-latest", parsed ? parsed.split(" · ")[0] : "");
  }
  // Show a range only when both ends are known AND differ; a single known bound (or a
  // same-day task) shows one date rather than a half-empty arrow.
  const text = (start && end && start !== end) ? `${start} → ${end}` : (end || start);
  const cell = el("div", "t-latest", text);
  cell.title = `collected ${t.collected_start || "unknown"} → ${t.collected_end || "unknown"}`;
  return cell;
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
  // Stashed so re-sorting the Tasks card (which rebuilds the rows) can refill the
  // cells without refetching stats.
  state.hoursInputs = { eps, stats, estimated };
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
// No Task facet: a chip per task ran to 90 chips on the bigger datasets, dwarfing
// every other facet. Pick a task from the Tasks card or the sidebar instead — each
// result row still names its task.
const FILTER_FACETS = [
  { field: "duration_s", label: "Duration (s)", kind: "range", get: (e) => e.duration_s },
  { field: "status", label: "Status", kind: "enum", get: (e) => e.status },
  { field: "station", label: "Station", kind: "enum", get: (e) => e.station },
  { field: "teacher", label: "Robot teacher", kind: "enum", get: (e) => e.teacher },
  { field: "control", label: "Control type", kind: "enum", get: (e) => e.control },
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
  state.facts = {};
  $("#task-select").value = task;
  try {
    const { episodes } = await api(`${apiBase()}/tasks/${encodeURIComponent(task)}/episodes`);
    state.episodes = episodes;
    renderEpisodeList();
    loadEpisodeFacts(task);   // fills in timestamps/status when it lands
    if (autoEpisode && episodes.includes(autoEpisode)) {
      await selectEpisode(autoEpisode);
    }
  } catch (e) {
    toast("Failed to load episodes: " + e.message);
  }
}

// Timestamp + success/failure per episode, for the sidebar rows. Fetched after the
// list renders (it can take a few seconds on a big task) and never fatal: a failure
// or an unsupported source just leaves the rows showing indices only. Stamped with
// the task it was requested for so a fast task switch can't apply stale labels.
async function loadEpisodeFacts(task) {
  try {
    const r = await api(`${apiBase()}/tasks/${encodeURIComponent(task)}/episode-facts`);
    if (state.task !== task) return;
    state.facts = r.facts || {};
    renderEpisodeList();
  } catch (_) { /* labels are optional */ }
}

// Episode lists can be very large (YAM tasks have >1000). Cap the rendered rows
// so the sidebar stays responsive; the search box narrows within the full list.
const EPISODE_RENDER_CAP = 300;

function renderEpisodeList() {
  const filter = $("#episode-search").value.toLowerCase();
  const list = $("#episode-list");
  list.innerHTML = "";
  // Index is the episode's stable position in the task list — ZERO-based, oldest
  // first, so it matches the episode numbering raiden itself records on disk
  // (0000, 0001, ...). Computed off the full list so it doesn't renumber as the
  // search box narrows the visible rows.
  const matched = state.episodes
    .map((ep, i) => ({ ep, idx: i }))
    .filter(({ ep }) => ep.toLowerCase().includes(filter));
  const shown = matched.slice(0, EPISODE_RENDER_CAP);
  $("#episode-count").textContent = matched.length;
  const width = String(Math.max(0, state.episodes.length - 1)).length;
  shown.forEach(({ ep, idx }) => {
    const li = el("li");
    li.classList.toggle("active", ep === state.episode);
    const head = el("div", "ep-li-head");
    // Zero-padded to the task's widest index so the rows form a clean column.
    head.appendChild(el("div", "ep-li-idx mono", String(idx).padStart(width, "0")));
    const f = (state.facts || {})[ep];
    const st = (f && f.status ? f.status : "").toLowerCase();
    if (st) {
      const badge = el("div", "ep-li-status " + statusClass(st), statusMark(st));
      badge.title = f.status;
      head.appendChild(badge);
    }
    li.appendChild(head);
    // Timestamp: from the metadata when we have it, else parsed out of the
    // episode's own name (raiden dirs are station_<ISO timestamp>).
    const when = (f && f.timestamp) ? formatStamp(f.timestamp) : parseEpisodeName(ep).when;
    if (when) li.appendChild(el("div", "ep-li-when mono", when));
    li.onclick = () => selectEpisode(ep);
    list.appendChild(li);
  });
  if (matched.length > shown.length) {
    const more = el("li", "ep-li-more", `+${matched.length - shown.length} more — refine search`);
    more.style.pointerEvents = "none";
    list.appendChild(more);
  }
}

// success/failure/pending -> the status-badge palette already used on the detail page.
function statusClass(st) {
  if (st === "success") return "success";
  if (st === "failure" || st === "fail") return "failure";
  return "neutral";
}

// A compact glyph rather than the word, to fit the sidebar row (title carries the word).
function statusMark(st) {
  return st === "success" ? "✓" : (st === "failure" || st === "fail") ? "✕" : "•";
}

// "2026-08-04T17:19:47.275092" -> "2026-08-04 · 17:19:47" (no Date parsing: these are
// local wallclock stamps with no zone, and Date would re-interpret them).
function formatStamp(ts) {
  const m = String(ts).match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})/);
  return m ? `${m[1]} · ${m[2]}` : null;
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
  updateEpisodeNav();
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

/* ---------------- Episode navigation: slider + prev/next + arrow keys ---------- */

// Sync the header nav bar (slider position, counter, button disabled state) to the
// current episode's index within the task list.
function updateEpisodeNav() {
  const eps = state.episodes;
  const i = eps.indexOf(state.episode);
  const n = eps.length;
  const slider = $("#ep-slider");
  const pos = $("#ep-pos");
  const prev = $("#ep-prev");
  const next = $("#ep-next");
  if (i < 0 || !n) {
    pos.textContent = "— / —";
    slider.max = "0"; slider.value = "0"; slider.disabled = true;
    prev.disabled = next.disabled = true;
    return;
  }
  slider.disabled = false;
  slider.max = String(n - 1);
  // Slider left→right = first→last in the task list (which is oldest→newest).
  slider.value = String(i);
  // Zero-based index / highest index, zero-padded to match the sidebar labels (and
  // raiden's own numbering) — "ep" makes it read as an index, not an Nth-of-M count.
  const w = String(n - 1).length;
  pos.textContent = `ep ${String(i).padStart(w, "0")} / ${n - 1}`;
  prev.disabled = i === 0;
  next.disabled = i === n - 1;
}

// Step to the episode `delta` positions away in the task list (bounded).
function stepEpisode(delta) {
  const eps = state.episodes;
  const i = eps.indexOf(state.episode);
  if (i < 0) return;
  const j = Math.min(eps.length - 1, Math.max(0, i + delta));
  if (j !== i) selectEpisode(eps[j]);
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
  renderRollout(md);
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

  const clipBase =
    `${apiBase()}/tasks/${encodeURIComponent(state.task)}/episodes/${encodeURIComponent(state.episode)}`;
  const clipQuery = `camera=${encodeURIComponent(c.name)}&eye=${state.eye}`;
  const url = `${clipBase}/video?${clipQuery}`;
  const statusUrl = `${clipBase}/video/status?${clipQuery}`;
  // Which episode this tile was mounted for, so a poll that outlives a navigation
  // stops instead of loading a clip into a torn-down tile.
  const mountedTask = state.task, mountedEpisode = state.episode;

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

  const failWith = (msg) => {
    overlay.className = "cam-overlay err";
    overlay.innerHTML = "";
    overlay.appendChild(el("div", "cam-icon"));
    overlay.appendChild(el("div", "cam-msg", msg));
    overlay.appendChild(el("div", "cam-sub", c.name));
  };

  // Wait for the DECODE, then hand the clip to the element. Pointing video.src
  // straight at /video puts a multi-minute transcode (cross-region download plus
  // an ffmpeg pass) on the request the browser is blocked on, and the load
  // balancer's 60s idle timeout severs it — which the element reports through
  // onerror as "Could not decode this stream", a decode error for a timeout, while
  // the decode goes on to succeed server-side. See raiden_viz/clips.py.
  (async () => {
    for (;;) {
      if (state.task !== mountedTask || state.episode !== mountedEpisode) return;
      let r;
      try {
        r = await fetch(statusUrl);
      } catch (_) {
        failWith("Could not reach the server");
        return;
      }
      if (!r.ok) {
        // The server distinguishes a missing camera (404) from a stub file with no
        // video in it (422), so show what it said rather than a generic failure.
        let msg = `Could not decode this stream (${r.status})`;
        try { msg = (await r.json()).detail || msg; } catch (_) {}
        failWith(msg);
        return;
      }
      const st = await r.json();
      if (st.ready) {
        video.src = url;   // now a cache hit, so this returns promptly
        video.load();
        return;
      }
      await new Promise((done) => setTimeout(done, CLIP_POLL_MS));
    }
  })();

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

// Rollout / policy provenance. Only rfm_rl rollout episodes carry
// metadata.rollout_info; when it is absent the whole card is hidden (data-driven,
// not an empty-state note) since it is not a universal section. Reuses the
// Metadata row styling; the long checkpoint path is a full-width wrapping row.
function renderRollout(md) {
  const card = $("#rollout-card");
  const ri = md && md.rollout_info;
  if (!ri || typeof ri !== "object") { card.classList.add("hidden"); return; }
  card.classList.remove("hidden");
  $("#rollout-generator").textContent = md.generator || "";
  const body = $("#rollout-body");
  body.innerHTML = "";
  const rows = [
    ["Policy", ri.policy, false],
    ["Config", ri.config_name, true],
    ["Action space", ri.action_space, false],
    ["Control rate", ri.control_hz != null ? `${ri.control_hz} Hz` : null, false],
    ["Ticks requested", ri.n_ticks_requested, false],
    ["Actor / tick", ri.actor_per_tick, false],
    ["Ensemble coeff", ri.ensemble_coeff, false],
    ["Max joint delta", ri.max_joint_delta, false],
    ["Verdict", ri.verdict, false],
    ["GT episode", ri.gt_episode, false],
    ["Git commit", ri.git_commit ? String(ri.git_commit).slice(0, 10) : null, true],
  ];
  let shown = 0;
  rows.forEach(([k, val, mono]) => {
    if (val == null || val === "") return;
    const row = el("div", "meta-row");
    row.appendChild(el("div", "meta-key", k));
    row.appendChild(el("div", "meta-val" + (mono ? " mono" : ""), String(val)));
    body.appendChild(row);
    shown++;
  });
  if (ri.ckpt) {
    const row = el("div", "meta-row rollout-ckpt");
    row.appendChild(el("div", "meta-key", "Checkpoint"));
    row.appendChild(el("div", "meta-val mono", String(ri.ckpt)));
    body.appendChild(row);
    shown++;
  }
  if (!shown) notAvailable(body, "No rollout parameters recorded for this episode.");
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
