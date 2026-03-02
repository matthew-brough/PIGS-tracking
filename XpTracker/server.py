"""
PIGS XpTracker – Starlette/uvicorn server
Serves the NUI userapp and persists player XP reports to PostgreSQL.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime

import asyncpg
import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

logger = logging.getLogger("xptracker")
logging.basicConfig(level=logging.INFO)

DATABASE_URL = "postgresql://{user}:{password}@db:{port}/{db}".format(
    user=os.environ["POSTGRES_USER"],
    password=os.environ["POSTGRES_PASSWORD"],
    port=os.environ["POSTGRES_PORT"],
    db=os.environ["POSTGRES_DB"],
)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
DDL = """
CREATE TABLE IF NOT EXISTS players (
    player_id   TEXT        PRIMARY KEY,
    player_name TEXT,
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- One timestamped snapshot of a player's XP totals and session state.
-- Deltas and rates are derived at query time by comparing adjacent rows.
CREATE TABLE IF NOT EXISTS pigs_reports (
    id              BIGSERIAL    PRIMARY KEY,
    player_id       TEXT         NOT NULL REFERENCES players (player_id),
    tier            SMALLINT,
    hunting_xp      BIGINT,
    business_xp     BIGINT,
    player_xp       BIGINT,
    heist_streak    SMALLINT,
    reported_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS pigs_reports_player_idx
    ON pigs_reports (player_id, reported_at DESC);
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialize(record: asyncpg.Record) -> dict:
    """Convert an asyncpg Record to a JSON-safe dict."""
    out: dict = {}
    for key, value in record.items():
        if isinstance(value, datetime):
            out[key] = value.isoformat()
        else:
            out[key] = value
    return out


def _int_or_none(value) -> int | None:
    """Coerce value to int, returning None for falsy/non-numeric input."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def json_response(data, status_code: int = 200) -> Response:
    return Response(
        content=json.dumps(data, default=str),
        status_code=status_code,
        media_type="application/json",
    )


# ---------------------------------------------------------------------------
# Lifespan (startup / shutdown)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: Starlette):
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


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def health(request: Request) -> Response:
    return json_response({"status": "ok"})


async def report(request: Request) -> Response:
    """
    Receive a timestamped XP snapshot from the PIGS NUI overlay.
    Fires whenever any XP value changes (not on a fixed interval).

    Expected JSON body:
        {
            "player_id":    "1",          // numeric user_id as string – required
            "player_name":  "artemOP",
            "tier":         7,
            "hunting_xp":   12345,
            "business_xp":  8000,
            "player_xp":    4000,
            "heist_streak": 3
        }
    """
    try:
        data: dict = await request.json()
    except Exception:
        return json_response({"error": "invalid JSON"}, 400)

    player_id: str | None = data.get("player_id")
    if not player_id:
        return json_response({"error": "player_id is required"}, 400)

    player_name: str = data.get("player_name") or "Unknown"
    tier = _int_or_none(data.get("tier"))
    hunting_xp = _int_or_none(data.get("hunting_xp"))
    business_xp = _int_or_none(data.get("business_xp"))
    player_xp = _int_or_none(data.get("player_xp"))
    heist_streak = int(data.get("heist_streak") or 0)

    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO players (player_id, player_name, last_seen)
                VALUES ($1, $2, NOW())
                ON CONFLICT (player_id) DO UPDATE
                    SET player_name = EXCLUDED.player_name,
                        last_seen   = NOW()
                """,
                player_id,
                player_name,
            )
            await conn.execute(
                """
                INSERT INTO pigs_reports
                    (player_id, tier, hunting_xp, business_xp, player_xp, heist_streak)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                player_id,
                tier,
                hunting_xp,
                business_xp,
                player_xp,
                heist_streak,
            )

    logger.info(
        "report | player=%s tier=%s streak=%d hunt=%s biz=%s player=%s",
        player_id,
        tier,
        heist_streak,
        hunting_xp,
        business_xp,
        player_xp,
    )
    return json_response({"status": "ok"})


async def get_players(request: Request) -> Response:
    """Return all players with their latest tier and XP totals."""
    async with request.app.state.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                p.player_id,
                p.player_name,
                p.last_seen,
                r.tier,
                r.heist_streak,
                r.hunting_xp,
                r.business_xp,
                r.player_xp
            FROM players p
            LEFT JOIN LATERAL (
                SELECT tier, heist_streak, hunting_xp, business_xp, player_xp
                FROM pigs_reports
                WHERE player_id = p.player_id
                ORDER BY reported_at DESC
                LIMIT 1
            ) r ON true
            ORDER BY p.last_seen DESC
            """
        )
    return json_response([_serialize(r) for r in rows])


async def get_player_history(request: Request) -> Response:
    """Return the 100 most recent XP snapshots for a specific player."""
    player_id: str = request.path_params["player_id"]
    async with request.app.state.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, tier, hunting_xp, business_xp, player_xp,
                   heist_streak, reported_at
            FROM pigs_reports
            WHERE player_id = $1
            ORDER BY reported_at DESC
            LIMIT 100
            """,
            player_id,
        )
    return json_response([_serialize(r) for r in rows])


async def get_stats(request: Request) -> Response:
    """
    XP-per-hour rates for every player over the last 24 h, derived
    from adjacent snapshot rows using LAG() rather than stored deltas.
    """
    async with request.app.state.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH ordered AS (
                SELECT
                    player_id,
                    hunting_xp,
                    business_xp,
                    player_xp,
                    heist_streak,
                    tier,
                    reported_at,
                    LAG(hunting_xp)  OVER w AS prev_hunt,
                    LAG(business_xp) OVER w AS prev_biz,
                    LAG(player_xp)   OVER w AS prev_player,
                    LAG(reported_at) OVER w AS prev_at
                FROM pigs_reports
                WHERE reported_at > NOW() - INTERVAL '24 hours'
                WINDOW w AS (PARTITION BY player_id ORDER BY reported_at)
            ),
            deltas AS (
                SELECT
                    player_id,
                    heist_streak,
                    tier,
                    -- only count positive, non-NULL gains between adjacent rows
                    GREATEST(hunting_xp  - prev_hunt,   0) AS hunt_gain,
                    GREATEST(business_xp - prev_biz,    0) AS biz_gain,
                    GREATEST(player_xp   - prev_player, 0) AS player_gain,
                    EXTRACT(EPOCH FROM (reported_at - prev_at))  AS secs
                FROM ordered
                WHERE prev_at IS NOT NULL
            )
            SELECT
                p.player_id,
                p.player_name,
                ROUND(SUM(d.hunt_gain)   / NULLIF(SUM(d.secs), 0) * 3600)::int AS hunting_xph,
                ROUND(SUM(d.biz_gain)    / NULLIF(SUM(d.secs), 0) * 3600)::int AS business_xph,
                ROUND(SUM(d.player_gain) / NULLIF(SUM(d.secs), 0) * 3600)::int AS player_xph,
                MAX(d.heist_streak) AS best_streak,
                MAX(d.tier)         AS peak_tier,
                COUNT(*)            AS report_count
            FROM deltas d
            JOIN players p USING (player_id)
            GROUP BY p.player_id, p.player_name
            ORDER BY hunting_xph DESC NULLS LAST
            """
        )
    return json_response([_serialize(r) for r in rows])


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

routes = [
    Route("/health", health, methods=["GET"]),
    Route("/report", report, methods=["POST"]),
    Route("/players", get_players, methods=["GET"]),
    Route("/players/{player_id:str}/history", get_player_history, methods=["GET"]),
    Route("/stats", get_stats, methods=["GET"]),
    # Static files last – catches everything else including "/"
    Mount("/", StaticFiles(directory="userapp", html=True)),
]

middleware = [
    # NUI pages run on a different origin inside FiveM; allow all for simplicity.
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
        reload=True,
        log_level="info",
    )
