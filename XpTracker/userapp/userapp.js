/**
 * PIGS XP Tracker – userapp.js
 *
 * Listens to Transport Tycoon userapp message events, extracts PIGS job
 * data and periodically POSTs XP deltas + heist streak to the server.
 *
 * Data keys consumed (from event.data.data):
 *   job                    – current job string; only report when "pigs"
 *   name                   – player display name
 *   user_id                – numeric user id (canonical player_id)
 *   exp_hunting_skill      – hunting XP total
 *   exp_business_business  – business XP total
 *   exp_player_player      – player XP total
 *   PartyTier              – party / heist tier (top-level shortcut)
 *   pigs_client_state      – full PIGS state object (inHeist, partyData…)
 */

'use strict';

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const PIGS_JOB            = 'pigs_job';
const MIN_REPORT_INTERVAL = 5_000;      // 5 s → server allows 12 per 60 s
const POLL_INTERVAL       = 10_000;     // ms between data re-requests

const SERVER_ORIGIN = window.location.origin !== 'null'
    ? window.location.origin
    : '';

const SESSION_TOKEN = window.__XP_TOKEN__ || '';

/** Keys this app actually consumes – used for targeted cache requests. */
const WATCHED_KEYS = [
    'job', 'name', 'user_id',
    'exp_hunting_skill', 'exp_business_business', 'exp_player_player',
    'PartyTier', 'pigs_client_state',
];

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const state = {
    playerId:    null,
    playerName:  'Unknown',
    job:         null,
    tier:        0,
    streak: 0,
    wasInHeist:  false,
    wasInParty:  false,

    /** Current XP readings from the game client. */
    xp:       { hunting: null, business: null, player: null },
    /** Last values successfully sent to the server. */
    reported: { hunting: undefined, business: undefined, player: undefined },

    lastReportAt:      null,
    pendingReport:     false,
    reportingDisabled: false,   // true after 401/403 – stops further reports
    lastStatus:        'Waiting…',
    statusType:        'neutral',
};

// ---------------------------------------------------------------------------
// XP helpers
// ---------------------------------------------------------------------------

function setXP(group, rawValue) {
    const value = Number(rawValue);
    if (Number.isFinite(value)) state.xp[group] = value;
}

/** True if any XP value differs from what was last successfully reported. */
function xpChanged() {
    return (
        state.xp.hunting  !== state.reported.hunting  ||
        state.xp.business !== state.reported.business ||
        state.xp.player   !== state.reported.player
    );
}

// ---------------------------------------------------------------------------
// Message parsing – extract game data into local state
// ---------------------------------------------------------------------------

/** Map of game-data key → handler that applies the value to state. */
const DATA_HANDLERS = {
    name:                   (v) => { state.playerName = v; },
    user_id:                (v) => { state.playerId = String(v); },
    job:                    (v) => { state.job = v; },
    exp_hunting_skill:      (v) => setXP('hunting', v),
    exp_business_business:  (v) => setXP('business', v),
    exp_player_player:      (v) => setXP('player', v),
    PartyTier:              (v) => { state.tier = Number(v) || 0; },
};

/**
 * Process the `pigs_client_state` sub-object: update tier, track heist
 * transitions and manage the streak counter.
 */
function applyPigsClientState(ps) {
    if (!ps || typeof ps !== 'object') return;

    // Tier (partyData takes precedence over PartyTier shortcut)
    if (ps.partyData?.tier != null) {
        state.tier = Number(ps.partyData.tier) || 0;
    }

    // Set streak from received data if present
    if (typeof ps.streak === 'number') {
        state.streak = ps.streak;
    }

    // Optionally, update wasInHeist/wasInParty if still needed elsewhere
    state.wasInHeist = Boolean(ps.inHeist);
    state.wasInParty = Boolean(ps.inParty);
}

/**
 * Top-level message listener – receives data from the Transport Tycoon
 * game client and delegates to the appropriate handlers.
 */
function handleMessage(event) {
    const msg = event.data;
    if (!msg || typeof msg.data !== 'object' || msg.data === null) return;

    const d = msg.data;

    // Apply simple key → state mappings
    for (const [key, handler] of Object.entries(DATA_HANDLERS)) {
        if (key in d) handler(d[key]);
    }

    // Complex sub-object
    if ('pigs_client_state' in d) applyPigsClientState(d.pigs_client_state);

    // Trigger a report whenever XP changes while on the PIGS job
    if (state.job === PIGS_JOB && state.playerId && xpChanged()) {
        scheduleReport();
    }

    renderUI();
}

window.addEventListener('message', handleMessage);

// ---------------------------------------------------------------------------
// Reporting (throttled – at most once per MIN_REPORT_INTERVAL)
// ---------------------------------------------------------------------------

function scheduleReport() {
    if (state.pendingReport || state.reportingDisabled) return;

    const now  = Date.now();
    const last = state.lastReportAt ? state.lastReportAt.getTime() : 0;
    const wait = Math.max(0, MIN_REPORT_INTERVAL - (now - last));

    state.pendingReport = true;
    setTimeout(async () => {
        try {
            if (state.job === PIGS_JOB && state.playerId && xpChanged()) {
                await sendReport();
            }
        } finally {
            state.pendingReport = false;
        }
    }, wait);
}

/** HTTP status → { message, fatal } mapping for error responses. */
const ERROR_RESPONSES = {
    401: { message: 'Session expired – reload page', fatal: true },
    403: { message: 'Invalid session – reload page', fatal: true },
    429: { message: 'Rate limited – slowing down',   fatal: false },
    413: { message: 'Payload too large',              fatal: false },
};

async function sendReport() {
    const payload = {
        player_id:    state.playerId,
        player_name:  state.playerName,
        tier:         state.tier || null,
        hunting_xp:   state.xp.hunting,
        business_xp:  state.xp.business,
        player_xp:    state.xp.player,
        heist_streak: state.streak,
        token:        SESSION_TOKEN,
    };

    // Optimistically snapshot reported values so concurrent xpChanged()
    // checks see them immediately and don't trigger duplicate sends.
    const prevReported = { ...state.reported };
    Object.assign(state.reported, { ...state.xp });

    try {
        const res = await fetch(`${SERVER_ORIGIN}/report`, {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify(payload),
        });

        if (res.ok) {
            state.lastReportAt = new Date();
            setStatus('OK', 'ok');
        } else {
            // Roll back optimistic update so the data is retried.
            Object.assign(state.reported, prevReported);
            handleErrorResponse(res);
        }
    } catch (err) {
        Object.assign(state.reported, prevReported);
        console.error('[XpTracker] POST /report failed:', err);
        setStatus('Network error');
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
    let detail = '';
    try { detail = (await res.json()).error || ''; } catch (_) { /* noop */ }

    if (res.status === 422) {
        setStatus(`Bad data: ${detail}`);
    } else {
        setStatus(`Error ${res.status}: ${detail || 'unknown'}`);
    }
}

function setStatus(msg, type = 'error') {
    state.lastStatus = msg;
    state.statusType = type;
}

// ---------------------------------------------------------------------------
// UI rendering
// ---------------------------------------------------------------------------

/** DOM ID → state value mapping for plain text cells. */
function getTextBindings() {
    return {
        'player-name':  state.playerName,
        'tier-value':   state.tier || '--',
        'streak-value': state.streak,
        'last-report':  state.lastReportAt
                            ? state.lastReportAt.toLocaleTimeString()
                            : 'Never',
    };
}

function renderUI() {
    const isPigs = state.job === PIGS_JOB;

    // Job badge
    const badge = document.getElementById('job-badge');
    badge.textContent = state.job || '--';
    badge.className   = 'badge ' + (isPigs ? 'badge-active' : 'badge-inactive');

    // Simple text bindings
    for (const [id, value] of Object.entries(getTextBindings())) {
        setText(id, value);
    }

    // XP rows
    renderXP('hunt-xp',   state.xp.hunting);
    renderXP('biz-xp',    state.xp.business);
    renderXP('player-xp', state.xp.player);

    // Status
    const statusEl = document.getElementById('report-status');
    if (statusEl) {
        statusEl.textContent = state.lastStatus;
        statusEl.className   = 'value muted'
            + (state.statusType === 'error' ? ' status-error' : '')
            + (state.statusType === 'ok'    ? ' status-ok'    : '');
    }
}

function renderXP(id, value) {
    setText(id, value === null ? '--' : value.toLocaleString());
}

function setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = String(value);
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

function requestData() {
    window.parent.postMessage({ type: 'getNamedData', keys: WATCHED_KEYS }, '*');
}

requestData();
setInterval(requestData, POLL_INTERVAL);
