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
// Server URL is derived from the page origin so it works with any tunnel URL
// automatically. When running locally for testing, set SERVER_ORIGIN manually.
const SERVER_ORIGIN = window.location.origin !== 'null'
    ? window.location.origin
    : '';          // empty string → relative paths (same host)

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const state = {
    playerId:    null,
    playerName:  'Unknown',
    job:         null,
    tier:        0,
    heistStreak: 0,
    wasInHeist:  false,
    wasInParty:  false,
    // Current XP readings from the game client
    xp: {
        hunting:  null,
        business: null,
        player:   null,
    },
    // Last values successfully sent to the server (undefined = never reported)
    reported: {
        hunting:  undefined,
        business: undefined,
        player:   undefined,
    },
    lastReportAt: null,
    lastStatus:   'Waiting…',
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function setXP(group, rawValue) {
    const value = Number(rawValue);
    if (Number.isFinite(value)) state.xp[group] = value;
}

/** True if any XP value differs from what was last successfully reported. */
function xpChanged() {
    return state.xp.hunting  !== state.reported.hunting  ||
           state.xp.business !== state.reported.business ||
           state.xp.player   !== state.reported.player;
}

// ---------------------------------------------------------------------------
// Message handler – receives data from the Transport Tycoon game client
// ---------------------------------------------------------------------------

window.addEventListener('message', (event) => {
    const msg = event.data;
    // The game wraps all key-value pairs inside a `data` property
    if (!msg || typeof msg.data !== 'object' || msg.data === null) return;

    const d = msg.data;

    // ── Identity ──────────────────────────────────────────────────────────
    if ('name'    in d) state.playerName = d.name;
    // user_id is numeric, stable and server-assigned – use it as the canonical id
    if ('user_id' in d) state.playerId   = String(d.user_id);
    if ('job'     in d) state.job        = d.job;

    // ── XP ────────────────────────────────────────────────────────────────
    if ('exp_hunting_skill'     in d) setXP('hunting',  d.exp_hunting_skill);
    if ('exp_business_business' in d) setXP('business', d.exp_business_business);
    if ('exp_player_player'     in d) setXP('player',   d.exp_player_player);

    // ── Tier (top-level shortcut) ──────────────────────────────────────────
    if ('PartyTier' in d) state.tier = Number(d.PartyTier) || 0;

    // ── PIGS client state ─────────────────────────────────────────────────
    if ('pigs_client_state' in d) {
        const ps = d.pigs_client_state;
        if (ps && typeof ps === 'object') {
            // Tier (partyData takes precedence over PartyTier shortcut)
            if (ps.partyData && ps.partyData.tier != null) {
                state.tier = Number(ps.partyData.tier) || 0;
            }

            // Heist streak: increment on inHeist true → false transition
            const nowInHeist = Boolean(ps.inHeist);
            const nowInParty = Boolean(ps.inParty);

            if (state.wasInHeist && !nowInHeist) {
                // Heist just ended – count as a completed run
                state.heistStreak++;
            }
            // Reset streak if player has left the party entirely
            if (state.wasInParty && !nowInParty) {
                state.heistStreak = 0;
            }

            state.wasInHeist = nowInHeist;
            state.wasInParty = nowInParty;
        }
    }

    // ── Trigger a report whenever XP changes while on the PIGS job ────────
    if (state.job === PIGS_JOB && state.playerId && xpChanged()) {
        sendReport();
    }

    renderUI();
});

// ---------------------------------------------------------------------------
// Report (fires on XP change, not on a fixed interval)
// ---------------------------------------------------------------------------

async function sendReport() {
    const payload = {
        player_id:    state.playerId,
        player_name:  state.playerName,
        tier:         state.tier,
        hunting_xp:   state.xp.hunting,
        business_xp:  state.xp.business,
        player_xp:    state.xp.player,
        heist_streak: state.heistStreak,
    };

    try {
        const res = await fetch(`${SERVER_ORIGIN}/report`, {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify(payload),
        });

        if (res.ok) {
            // Only update the snapshot on success so a network blip causes a retry
            state.reported.hunting  = state.xp.hunting;
            state.reported.business = state.xp.business;
            state.reported.player   = state.xp.player;
            state.lastReportAt = new Date();
            setStatus('OK');
        } else {
            setStatus(`Error ${res.status}`);
        }
    } catch (err) {
        console.error('[XpTracker] POST /report failed:', err);
        setStatus('Network error');
    }

    renderUI();
}

function setStatus(msg) {
    state.lastStatus = msg;
}

// ---------------------------------------------------------------------------
// UI rendering
// ---------------------------------------------------------------------------

function renderUI() {
    const isPigs = state.job === PIGS_JOB;

    // Job badge
    const badge = document.getElementById('job-badge');
    badge.textContent  = state.job || '--';
    badge.className    = 'badge ' + (isPigs ? 'badge-active' : 'badge-inactive');

    // Player info
    setText('player-name',  state.playerName);
    setText('tier-value',   state.tier  || '--');
    setText('streak-value', state.heistStreak);

    // XP rows
    renderXP('hunt-xp',   state.xp.hunting);
    renderXP('biz-xp',    state.xp.business);
    renderXP('player-xp', state.xp.player);

    // Footer
    setText('last-report',
        state.lastReportAt ? state.lastReportAt.toLocaleTimeString() : 'Never');
    setText('report-status', state.lastStatus);
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

/** Keys this app actually consumes – used for targeted cache requests. */
const WATCHED_KEYS = [
    'job',
    'name',
    'user_id',
    'exp_hunting_skill',
    'exp_business_business',
    'exp_player_player',
    'PartyTier',
    'pigs_client_state',
];

/** Request only the keys we care about to avoid triggering full-cache lag. */
function requestData() {
    window.parent.postMessage({ type: 'getNamedData', keys: WATCHED_KEYS }, '*');
}

// Flush the relevant cache keys on startup (triggers initial report if already on job)
requestData();

// Periodically re-request data so we catch any values we might have missed.
// Reports are sent by the message handler whenever XP changes, not here.
setInterval(requestData, 10_000);
