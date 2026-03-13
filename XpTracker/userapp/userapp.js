"use strict";

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const DEV = false;

function log(...data) {
  if (DEV) {
    console.log(...data);
  }
}

function debug(...data) {
  if (DEV) {
    console.debug(...data);
  }
}

const PIGS_JOB = "pigs_job";
const MIN_REPORT_INTERVAL = 5 * 1000; // ms

const SERVER_ORIGIN =
  window.location.origin !== "null" ? window.location.origin : "";

const SESSION_TOKEN = window.__XP_TOKEN__ || "";

const ERROR_RESPONSES = {
  400: { message: "Invalid data sent", fatal: true },
  401: { message: "Session expired – reload page", fatal: true },
  403: { message: "Invalid session – reload page", fatal: true },
  413: { message: "Payload too large", fatal: false },
  429: { message: "Rate limited – slowing down", fatal: false },
};

const WATCHED_KEYS = [
  "job",
  "name",
  "user_id",
  "exp_hunting_skill",
  "exp_business_business",
  "exp_player_player",
  "PartyTier",
  "pigs_client_state_inParty",
  "pigs_client_state_memberCount",
  "pigs_client_state_partyData_memberCount",
  "pigs_client_state_streak",
];

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const state = {
  user_id: null,
  name: null,
  job: null,
  tier: 0,
  streak: 0,
  player_count: 0,
  xp: {
    hunting: null,
    business: null,
    player: null,
  },
  lastReportAt: null,
  pending_report: false,
  reportingDisabled: false,
  lastStatus: "Waiting...",
  statusType: "neutral",
  InParty: false,
};

// ---------------------------------------------------------------------------
// XP helpers
// ---------------------------------------------------------------------------

function setXP(group, rawValue) {
  const value = Number(rawValue);
  if (Number.isFinite(value)) state.xp[group] = value;
}

// ---------------------------------------------------------------------------
// Draging Logic
// ---------------------------------------------------------------------------

// Dragging logic for the whole window
let dragListenersAttached = false;
let isDragging = false;
let dragStartX = 0;
let dragStartY = 0;
let windowStartX = 0;
let windowStartY = 0;

function initializeDragging() {
  const tracker = document.getElementById("tracker");
  const header = tracker ? tracker.querySelector(".header") : null;

  if (!tracker || !header) return;

  const savedPosition = getSavedPosition();
  if (savedPosition) {
    tracker.style.left = Math.round(savedPosition.x) + "px";
    tracker.style.top = Math.round(savedPosition.y) + "px";
  }

  header.style.cursor = "move";

  // Drag event listeners
  if (!dragListenersAttached) {
    header.addEventListener("mousedown", startDragging);
    document.addEventListener("mousemove", drag);
    document.addEventListener("mouseup", stopDragging);
    dragListenersAttached = true;
  }
}

function startDragging(e) {
  isDragging = true;
  const tracker = document.getElementById("tracker");

  dragStartX = e.clientX;
  dragStartY = e.clientY;

  const rect = tracker.getBoundingClientRect();
  windowStartX = Math.round(rect.left);
  windowStartY = Math.round(rect.top);

  e.preventDefault();
}

function drag(e) {
  if (!isDragging) return;

  const tracker = document.getElementById("tracker");
  const deltaX = e.clientX - dragStartX;
  const deltaY = e.clientY - dragStartY;

  const newX = windowStartX + deltaX;
  const newY = windowStartY + deltaY;

  const maxX = window.innerWidth - tracker.offsetWidth;
  const maxY = window.innerHeight - tracker.offsetHeight;

  const boundedX = Math.round(Math.max(0, Math.min(newX, maxX)));
  const boundedY = Math.round(Math.max(0, Math.min(newY, maxY)));

  tracker.style.left = boundedX + "px";
  tracker.style.top = boundedY + "px";
}

function stopDragging() {
  if (isDragging) {
    isDragging = false;
    savePosition();
  }
}

function savePosition() {
  const tracker = document.getElementById("tracker");
  const rect = tracker.getBoundingClientRect();

  const position = {
    x: Math.round(rect.left),
    y: Math.round(rect.top),
  };

  localStorage.setItem("pigsTracker_position", JSON.stringify(position));
}

function getSavedPosition() {
  try {
    const saved = localStorage.getItem("pigsTracker_position");
    return saved ? JSON.parse(saved) : null;
  } catch {
    return null;
  }
}
// ---------------------------------------------------------------------------
// Message parsing – extract game data into local state
// ---------------------------------------------------------------------------

function isObject(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

const DATA_HANDLERS = {
  user_id: (v) => {
    state.user_id = v;
  },
  name: (v) => {
    state.name = v;
  },
  job: (v) => {
    state.job = v;
  },
  PartyTier: (v) => {
    state.tier = v;
  },
  pigs_client_state_streak: (v) => {
    state.streak = v;
  },
  pigs_client_state_memberCount: (v) => {
    state.player_count = v;
  },
  pigs_client_state_partyData_memberCount: (v) => {
    state.player_count = v;
  },
  exp_hunting_skill: (v) => {
    setXP("hunting", v);
  },
  exp_business_business: (v) => {
    setXP("business", v);
  },
  exp_player_player: (v) => {
    setXP("player", v);
  },
  pigs_client_state_inParty: (v) => {
    state.InParty = v;
  },
};

function handleMessage(event) {
  const msg = event.data;
  if (typeof msg !== "object" || msg === null) return;
  debug("Received message:", msg);
  const data = msg.data;
  if (!isObject(data)) return;

  for (const [key, value] of Object.entries(data)) {
    if (key.startsWith("pigs_client_state")) {
      debug(`Key: ${key}, Value: ${value}`);
    }
  }

  for (const key of WATCHED_KEYS) {
    if (!(key in data)) continue;

    if (!(key in DATA_HANDLERS)) {
      console.warn(`No handler for key: ${key}`);
      state.reportingDisabled = true;
      setStatus(`Error: No handler for key: ${key}`, "error");
      continue;
    }
    log(`Handling key: ${key} with value: ${data[key]}`);
    const handler = DATA_HANDLERS[key];
    const value = data[key];
    try {
      handler(value);
    } catch (err) {
      console.error(`Error handling key: ${key}`, err);
      state.reportingDisabled = true;
      setStatus(`Error handling key: ${key} - ${err.message}`, "error");
    }

    if (
      state.job === PIGS_JOB &&
      state.user_id &&
      key === "pigs_client_state_streak"
    ) {
      let ready = true;
      for (const [key, value] of Object.entries(state)) {
        if (
          (value === null || value === 0) &&
          key !== "streak" &&
          key !== "lastReportAt"
        ) {
          debug(`Not ready for reporting, key: ${key}, value: ${value}`);
          ready = false;
          break;
        }
      }
      if (ready) {
        scheduleReport();
      }
    }
  }

  renderUI();
}

window.addEventListener("message", handleMessage);

// ---------------------------------------------------------------------------
// Reporting (throttled – at most once per MIN_REPORT_INTERVAL)
// ---------------------------------------------------------------------------

function scheduleReport() {
  if (state.pending_report || state.reportingDisabled) {
    debug(
      "skipped, pending report: ",
      state.pending_report,
      "reporting disabled: ",
      state.reportingDisabled,
    );
    return;
  }

  const now = Date.now();
  const last = state.lastReportAt ? state.lastReportAt.getTime() : 0;
  const wait = Math.max(0, MIN_REPORT_INTERVAL - (now - last));

  state.pending_report = true;
  setTimeout(async () => {
    try {
      if (state.job === PIGS_JOB && state.user_id) {
        await sendReport();
      }
    } finally {
      state.pending_report = false;
    }
  }, wait);
}

async function sendReport() {
  const payload = {
    player_id: state.user_id,
    player_name: state.name,
    tier: state.tier,
    heist_streak: state.streak,
    player_count: state.player_count,
    hunting_xp: state.xp.hunting,
    business_xp: state.xp.business,
    player_xp: state.xp.player,
    token: SESSION_TOKEN,
    login: state.lastReportAt === null ? true : false,
  };
  debug("Sending report:", payload);
  try {
    const res = await fetch(`${SERVER_ORIGIN}/report`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (res.ok) {
      state.lastReportAt = new Date();
      setStatus("OK", "ok");
    } else {
      handleErrorResponse(res);
    }
  } catch (err) {
    console.error("[XpTracker] POST /report failed:", err);
    setStatus("Network error");
  }
  renderUI();
}

async function handleErrorResponse(res) {
  const known = ERROR_RESPONSES[res.status];
  if (known) {
    setStatus(known.message);
    if (known.fatal) state.reportingDisabled = true;
    return;
  }
  // Fallback – try to extract server error detail
  let detail = "";
  try {
    detail = (await res.json()).error || "";
  } catch (_) {
    /* noop */
  }

  if (res.status === 422) {
    setStatus(`Bad data: ${detail}`);
  } else {
    setStatus(`Error ${res.status}: ${detail || "unknown"}`);
  }
}

function setStatus(msg, type = "error") {
  state.lastStatus = msg;
  state.statusType = type;
}

// ---------------------------------------------------------------------------
// UI rendering
// ---------------------------------------------------------------------------

function getTextBindings() {
  return {
    "player-name": state.name,
    "tier-value": state.tier || "--",
    "streak-value": state.streak,
    "player-count": state.playerCount || "1",
    "last-report": state.lastReportAt
      ? state.lastReportAt.toLocaleTimeString()
      : "Never",
  };
}

function renderUI() {
  const isPigs = state.job === PIGS_JOB;

  // Job badge
  const badge = document.getElementById("job-badge");
  badge.textContent = state.job || "--";
  badge.className = "badge " + (isPigs ? "badge-active" : "badge-inactive");

  // Simple text bindings
  for (const [id, value] of Object.entries(getTextBindings())) {
    setText(id, value);
  }

  // XP rows
  renderXP("hunt-xp", state.xp.hunting);
  renderXP("biz-xp", state.xp.business);
  renderXP("player-xp", state.xp.player);

  // Status
  const statusEl = document.getElementById("report-status");
  if (statusEl) {
    statusEl.textContent = state.lastStatus;
    statusEl.className =
      "value muted" +
      (state.statusType === "error" ? " status-error" : "") +
      (state.statusType === "ok" ? " status-ok" : "");
  }
}

function renderXP(id, value) {
  setText(id, value === null ? "--" : value.toLocaleString());
}

function setText(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = String(value);
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  initializeDragging();
  window.parent.postMessage({ type: "getNamedData", keys: WATCHED_KEYS }, "*");
});
