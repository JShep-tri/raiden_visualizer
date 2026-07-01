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
  camera: null,
  eye: "left",
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
  } catch (e) {
    toast("Failed to load tasks: " + e.message);
  }
  $("#episode-search").addEventListener("input", renderEpisodeList);
  $("#eye-toggle").addEventListener("click", (ev) => {
    const b = ev.target.closest("button");
    if (!b) return;
    state.eye = b.dataset.eye;
    document.querySelectorAll("#eye-toggle button").forEach((x) => x.classList.toggle("active", x === b));
    if (state.camera) loadVideo();
  });
  $("#calib-head").addEventListener("click", () => $(".calib-card").classList.toggle("collapsed"));
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
  state.episode = ep;
  location.hash = encodeURIComponent(`${state.task}/${ep}`);
  renderEpisodeList();
  $("#empty-state").classList.add("hidden");
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

  renderCameras(d.cameras || []);
  renderMeta(md, d);
  renderPlots(d.robot);
  renderCalibration(d.calibration, d.cameras || []);
}

function renderCameras(cameras) {
  const tabs = $("#camera-tabs");
  tabs.innerHTML = "";
  const usable = cameras.filter((c) => c.has_video);
  // Prefer a scene/ego camera as the default view.
  const preferred = usable.find((c) => /scene|ego/.test(c.name)) || usable[0];
  state.camera = preferred ? preferred.name : null;

  cameras.forEach((c) => {
    const b = el("button", c.name === state.camera ? "active" : "", prettyCam(c.name));
    b.disabled = !c.has_video;
    b.title = c.has_video ? `${c.size_mb} MB` : "No recorded video (stub file)";
    b.onclick = () => {
      state.camera = c.name;
      document.querySelectorAll("#camera-tabs button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      loadVideo();
    };
    tabs.appendChild(b);
  });

  if (state.camera) loadVideo();
  else showVideoOverlay("No camera video available for this episode.");
}

function prettyCam(name) {
  return name.replace(/_camera$/, "").replace(/_/g, " ");
}

function showVideoOverlay(msg, spinner = false) {
  const ov = $("#video-overlay");
  ov.innerHTML = "";
  if (spinner) ov.appendChild(el("div", "spinner"));
  ov.appendChild(el("div", null, msg));
  ov.classList.remove("hidden");
}
function hideVideoOverlay() { $("#video-overlay").classList.add("hidden"); }

function loadVideo() {
  const v = $("#player");
  const url =
    `/api/tasks/${encodeURIComponent(state.task)}/episodes/${encodeURIComponent(state.episode)}` +
    `/video?camera=${encodeURIComponent(state.camera)}&eye=${state.eye}`;
  showVideoOverlay("Decoding video… first load transcodes on the server.", true);
  $("#video-caption").textContent = `${state.camera} · ${state.eye} eye · decoding .svo2 → mp4`;
  const onReady = () => {
    hideVideoOverlay();
    $("#video-caption").textContent =
      `${state.camera} · ${state.eye} eye · ${v.videoWidth}×${v.videoHeight}`;
  };
  // With +faststart MP4 the moov atom is up front, so the clip is ready to
  // scrub/play as soon as metadata loads — hide the overlay at that point.
  v.onloadedmetadata = onReady;
  v.oncanplay = onReady;
  v.onloadeddata = onReady;
  v.onplaying = onReady;
  v.onerror = () => showVideoOverlay("Could not decode this camera stream.");
  v.src = url;
  v.load();
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
  if (!robot || !robot.signals) {
    $("#plot-summary").textContent = "";
    wrap.appendChild(el("div", "subtle", "No robot_data.npz for this episode."));
    return;
  }
  const s = robot.summary || {};
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
    const canvas = el("canvas");
    block.appendChild(canvas);
    if (sig.dims > 1) block.appendChild(makeLegend(sig.dims));
    wrap.appendChild(block);
    // defer draw so canvas has layout dimensions
    requestAnimationFrame(() => drawSeries(canvas, t, sig));
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
