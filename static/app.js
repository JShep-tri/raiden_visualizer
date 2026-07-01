"use strict";

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, txt) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt != null) e.textContent = txt;
  return e;
};

const state = {
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

function toast(msg) {
  const t = el("div", "toast", msg);
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 4000);
}

/* ---------------- Sidebar: tasks + episodes ---------------- */

async function init() {
  try {
    const health = await api("/api/health");
    $("#s3-root").textContent = `s3://${health.bucket}/${health.prefix}`;
    const { tasks } = await api("/api/tasks");
    const sel = $("#task-select");
    sel.innerHTML = "";
    tasks.forEach((t) => sel.appendChild(new Option(t, t)));
    sel.onchange = () => selectTask(sel.value);
    // Restore task/episode from the URL hash (#task/episode) for shareable links.
    const [hTask, hEp] = decodeURIComponent(location.hash.slice(1)).split("/");
    const startTask = tasks.includes(hTask) ? hTask : tasks[0];
    if (startTask) await selectTask(startTask, hEp || null);
    if (!hEp) renderOverview();  // land on the overview when no deep link
  } catch (e) {
    toast("Failed to load tasks: " + e.message);
  }
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
}

/* ---------------- Overview page ---------------- */

function showOverview() {
  stopPlayback();
  state.episode = null;
  location.hash = "";
  renderEpisodeList();
  $("#episode-view").classList.add("hidden");
  $("#overview-view").classList.remove("hidden");
  renderOverview();
}

async function renderOverview() {
  try {
    const ov = await api("/api/overview");
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
      const latest = t.latest ? parseEpisodeName(t.latest).when || "" : "";
      row.appendChild(el("div", "t-latest", latest ? latest.split(" · ")[0] : ""));
      row.onclick = () => selectTask(t.task);
      list.appendChild(row);
    });
  } catch (e) {
    toast("Failed to load overview: " + e.message);
  }
}

async function selectTask(task, autoEpisode = null) {
  state.task = task;
  $("#task-select").value = task;
  try {
    const { episodes } = await api(`/api/tasks/${encodeURIComponent(task)}/episodes`);
    state.episodes = episodes;
    renderEpisodeList();
    if (autoEpisode && episodes.includes(autoEpisode)) {
      await selectEpisode(autoEpisode);
    }
  } catch (e) {
    toast("Failed to load episodes: " + e.message);
  }
}

function renderEpisodeList() {
  const filter = $("#episode-search").value.toLowerCase();
  const list = $("#episode-list");
  list.innerHTML = "";
  const shown = state.episodes.filter((e) => e.toLowerCase().includes(filter));
  $("#episode-count").textContent = shown.length;
  shown.forEach((ep) => {
    const li = el("li");
    li.classList.toggle("active", ep === state.episode);
    const parts = parseEpisodeName(ep);
    const name = el("div", "ep-li-name", parts.name);
    li.appendChild(name);
    if (parts.when) li.appendChild(el("div", "ep-li-meta", parts.when));
    li.onclick = () => selectEpisode(ep);
    list.appendChild(li);
  });
}

// Episode folders look like "russet_2026-06-30T17-19-12.764258".
function parseEpisodeName(ep) {
  const m = ep.match(/^(.*?)_(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})/);
  if (!m) return { name: ep, when: null };
  return { name: m[1], when: `${m[2]} · ${m[3]}:${m[4]}:${m[5]}` };
}

/* ---------------- Episode detail ---------------- */

async function selectEpisode(ep) {
  stopPlayback();
  state.episode = ep;
  location.hash = encodeURIComponent(`${state.task}/${ep}`);
  renderEpisodeList();
  $("#overview-view").classList.add("hidden");
  $("#episode-view").classList.remove("hidden");
  $("#ep-instruction").textContent = "Loading…";
  try {
    const detail = await api(
      `/api/tasks/${encodeURIComponent(state.task)}/episodes/${encodeURIComponent(ep)}`
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
  $("#ep-instruction").textContent = md.task_instruction || md.task_name || d.episode;
  $("#ep-task").textContent = d.task;
  $("#ep-name").textContent = d.episode;

  const status = (md.status || "").toLowerCase();
  const badge = $("#ep-status");
  badge.textContent = md.status || "unknown";
  badge.className = "status-badge " + (status === "success" ? "success" : status ? "failure" : "neutral");

  buildCameraGrid(d.cameras || []);
  renderMeta(md, d);
  renderPlots(d.robot);
  renderCalibration(d.calibration, d.cameras || []);
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
  tile.appendChild(label);
  tile.appendChild(overlay);

  const url =
    `/api/tasks/${encodeURIComponent(state.task)}/episodes/${encodeURIComponent(state.episode)}` +
    `/video?camera=${encodeURIComponent(c.name)}&eye=${state.eye}`;

  const tileState = { camera: c.name, video, ready: false };
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
  if (wasPlaying) startPlayback();
}

// Per-frame loop while playing: advance scrubber + move plot cursors in lockstep.
function tick() {
  if (!state.playing) return;
  const dur = timelineDuration();
  const t = currentTime();
  updateTransportUI(t);
  drawAllCursors(t);
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

function updateTransportUI(secs) {
  const dur = timelineDuration();
  const label = dur ? `${secs.toFixed(1)}s / ${dur.toFixed(1)}s` : `${secs.toFixed(1)}s`;
  $("#time-label").textContent = label;
  const sc = $("#scrubber");
  if (dur && document.activeElement !== sc) {
    sc.value = String(Math.round((secs / dur) * 1000));
  }
}

function renderMeta(md, d) {
  const grid = $("#meta-grid");
  grid.innerHTML = "";
  const rows = [
    ["Teacher", md.teacher_name],
    ["Station", md.station_name],
    ["Control", md.control],
    ["Duration", md.duration_s != null ? `${md.duration_s.toFixed(2)} s` : null],
    ["Robot frames", md.robot_frames],
    ["Robot rate", md.robot_hz != null ? `${md.robot_hz} Hz` : null],
    ["Camera FPS", md.camera_fps],
    ["Cameras", (md.cameras || []).length ? md.cameras.length : null],
    ["Timestamp", md.timestamp ? md.timestamp.replace("T", " ").slice(0, 19) : null],
    ["Converted", md.converted != null ? String(md.converted) : null],
  ];
  rows.forEach(([k, val]) => {
    if (val == null || val === "") return;
    const row = el("div", "meta-row");
    row.appendChild(el("div", "meta-key", k));
    const isMono = k === "Timestamp" || k === "Robot frames";
    row.appendChild(el("div", "meta-val" + (isMono ? " mono" : ""), String(val)));
    grid.appendChild(row);
  });
}

/* ---------------- Robot trajectory plots ---------------- */

const PALETTE = ["#6ea8fe", "#f472b6", "#4ade80", "#fbbf24", "#a78bfa", "#22d3ee", "#fb923c"];

function renderPlots(robot) {
  const wrap = $("#plots");
  wrap.innerHTML = "";
  state.plots = [];
  state.robotDuration = 0;
  if (!robot || !robot.signals) {
    $("#plot-summary").textContent = "";
    wrap.appendChild(el("div", "subtle", "No robot_data.npz for this episode."));
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
    p.ctx.strokeStyle = "rgba(110,168,254,0.9)";
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
  if (!calib || !calib.cameras) {
    card.classList.add("collapsed");
    body.appendChild(el("div", "subtle", "No calibration_results.json for this episode."));
    return;
  }
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
    body.appendChild(box);
  });
}

function kv(parent, k, v) {
  const row = el("div", "kv");
  row.appendChild(el("span", null, k));
  row.appendChild(el("span", null, v));
  parent.appendChild(row);
}

init();
