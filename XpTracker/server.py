"""
PIGS XpTracker – Starlette/uvicorn server

Serves the NUI userapp and persists player XP reports to PostgreSQL.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from contextlib import asynccontextmanager

import asyncpg
import uvicorn
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from discord_logging import DiscordWebhookHandler
from validation import (
    MAX_BODY_BYTES,
    ValidationError,
    is_rate_limited,
    validate_report,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USERAPP_DIR = os.path.join(BASE_DIR, "userapp")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("xptracker")
logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATABASE_URL = os.environ["DATABASE_URL"]
DISCORD_WEBHOOK_URL: str | None = os.environ.get("DISCORD_WEBHOOK_URL")

SECRET_KEY: str = secrets.token_hex(32)
_serializer = URLSafeTimedSerializer(SECRET_KEY)
TOKEN_MAX_AGE = 24 * 60 * 60  # 24 hours

_discord_handler: DiscordWebhookHandler | None = None

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

DDL = """\
CREATE TABLE IF NOT EXISTS players (
    player_id   TEXT        PRIMARY KEY,
    player_name TEXT,
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS pigs_reports (
    id              BIGSERIAL    PRIMARY KEY,
    player_id       TEXT         NOT NULL REFERENCES players (player_id),
    tier            SMALLINT,
    hunting_xp      BIGINT,
    business_xp     BIGINT,
    player_xp       BIGINT,
    heist_streak    SMALLINT,
    reported_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    new_session     BOOLEAN      NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS pigs_reports_player_idx
    ON pigs_reports (player_id, reported_at DESC);
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_discord_logging() -> None:
    """Attach a :class:`DiscordWebhookHandler` to the root logger."""
    global _discord_handler
    if not DISCORD_WEBHOOK_URL:
        logger.warning("DISCORD_WEBHOOK_URL not set — Discord logging disabled.")
        return
    handler = DiscordWebhookHandler(DISCORD_WEBHOOK_URL, level=logging.INFO)
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S"),
    )
    # Attach to root logger (catches most things)
    logging.getLogger().addHandler(handler)
    # Uvicorn sets propagate=False on its loggers, so attach directly
    external_loggers = ("uvicorn",)
    for name in external_loggers:
        logging.getLogger(name).addHandler(handler)
    _discord_handler = handler


def _json(data: object, status_code: int = 200) -> Response:
    return Response(
        content=json.dumps(data, default=str),
        status_code=status_code,
        media_type="application/json",
    )


def _verify_token(raw_token: object) -> str | None:
    """Return an error message if the token is invalid, else ``None``."""
    if not isinstance(raw_token, str) or not raw_token:
        return "missing or invalid token"
    try:
        _serializer.loads(raw_token, max_age=TOKEN_MAX_AGE)
    except SignatureExpired:
        return "token expired – reload the page"
    except BadSignature:
        return "invalid token"
    return None


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

_UPSERT_PLAYER = """\
INSERT INTO players (player_id, player_name, last_seen)
VALUES ($1, $2, NOW())
ON CONFLICT (player_id) DO UPDATE
    SET player_name = EXCLUDED.player_name,
        last_seen   = NOW()
"""

_INSERT_REPORT = """\
INSERT INTO pigs_reports
    (player_id, tier, hunting_xp, business_xp, player_xp, heist_streak, new_session)
VALUES ($1, $2, $3, $4, $5, $6, $7)
"""

_LAST_REPORT = """\
SELECT tier, hunting_xp, business_xp, player_xp, heist_streak
  FROM pigs_reports
 WHERE player_id = $1
 ORDER BY reported_at DESC
 LIMIT 1
"""


async def _persist_report(pool: asyncpg.Pool, rpt) -> None:
    """Write the validated report to the database.

    Adds a new row to pigs_reports. If the new report is a duplicate of the most recent report for the same player, sets new_session=True on the inserted row, otherwise False.
    """
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(_UPSERT_PLAYER, rpt.player_id, rpt.player_name)

        last = await conn.fetchrow(_LAST_REPORT, rpt.player_id)
        is_duplicate = (
            last is not None
            and last["tier"] == rpt.tier
            and last["hunting_xp"] == rpt.hunting_xp
            and last["business_xp"] == rpt.business_xp
            and last["player_xp"] == rpt.player_xp
            and last["heist_streak"] == rpt.heist_streak
        )

        await conn.execute(
            _INSERT_REPORT,
            rpt.player_id,
            rpt.tier,
            rpt.hunting_xp,
            rpt.business_xp,
            rpt.player_xp,
            rpt.heist_streak,
            is_duplicate,
        )


# ---------------------------------------------------------------------------
# Lifespan (startup / shutdown)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: Starlette):
    _setup_discord_logging()
    if _discord_handler is not None:
        _discord_handler.start()
        logger.info("Discord webhook logging started.")

    logger.info("Connecting to database…")
    pool: asyncpg.Pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with pool.acquire() as conn:
        await conn.execute(DDL)
    logger.info("Database schema ready.")
    app.state.pool = pool

    try:
        yield
    finally:
        await pool.close()
        logger.info("Database pool closed.")
        if _discord_handler is not None:
            await _discord_handler.stop()


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

# Maps token-verification errors → HTTP status code
_TOKEN_ERROR_STATUS = {
    "missing or invalid token": 401,
    "token expired – reload the page": 401,
    "invalid token": 403,
}


async def report(request: Request) -> Response:
    """Receive a timestamped XP snapshot from the PIGS NUI overlay."""
    assert request.client
    logger.debug("Received report request from %s", request.client.host)
    # ── Body size guard ───────────────────────────────────────────────
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        return _json({"error": "payload too large"}, 413)

    try:
        data: dict = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return _json({"error": "invalid JSON"}, 400)

    # ── Token ─────────────────────────────────────────────────────────
    token_err = _verify_token(data.get("token"))
    if token_err:
        return _json({"error": token_err}, _TOKEN_ERROR_STATUS.get(token_err, 401))

    # ── Validate & sanitise ───────────────────────────────────────────
    try:
        rpt = validate_report(data)
    except ValidationError as exc:
        return _json({"error": str(exc)}, exc.status_code)

    # ── Rate limit ────────────────────────────────────────────────────
    if is_rate_limited(rpt.player_id):
        return _json({"error": "rate limited"}, 429)

    # ── Persist ───────────────────────────────────────────────────────
    await _persist_report(request.app.state.pool, rpt)
    logger.info(
        "report | player=%s tier=%s streak=%d hunt=%s biz=%s player=%s",
        rpt.player_id,
        rpt.tier,
        rpt.heist_streak,
        rpt.hunting_xp,
        rpt.business_xp,
        rpt.player_xp,
    )
    return _json({"status": "ok"})


async def index(request: Request) -> HTMLResponse:
    """Serve the main userapp page with an embedded session token."""
    token = _serializer.dumps({"nonce": secrets.token_hex(16)})
    with open(os.path.join(USERAPP_DIR, "index.html")) as f:
        html = f.read()
    token_script = f'<script>window.__XP_TOKEN__="{token}";</script>'
    html = html.replace("</head>", f"{token_script}\n</head>", 1)
    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

routes = [
    Route("/", index, methods=["GET"]),
    Route("/report", report, methods=["POST"]),
    Mount("/static", StaticFiles(directory=USERAPP_DIR)),
]

middleware = [
    Middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    ),
]

app = Starlette(routes=routes, middleware=middleware, lifespan=lifespan)

# ---------------------------------------------------------------------------
# Entry point (development)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=int(os.environ.get("APP_PORT", 8000)),
    )
