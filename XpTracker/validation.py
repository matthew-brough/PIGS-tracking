"""
Request validation and sanitisation for XP report payloads.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------

MAX_BODY_BYTES = 2_048  # 2 KB – a report payload is ~200 bytes

ACCEPTED_FIELDS = frozenset(
    {
        "player_id",
        "player_name",
        "tier",
        "hunting_xp",
        "business_xp",
        "player_xp",
        "heist_streak",
        "player_count",
        "login",
        "token",
    }
)

TIER_RANGE = (1, 7)
XP_RANGE = (0, 1_000_000_000)
STREAK_RANGE = (0, 1_001)
PLAYER_COUNT_RANGE = (1, 600)

_PLAYER_ID_RE = re.compile(r"^\d{1,7}$")
_PLAYER_NAME_MAX = 64

# Per-player rate limiting – client polls every ~10 s; allow headroom
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX = 12  # reports per window (~1 every 5 s)

_rate_buckets: dict[str, list[float]] = defaultdict(list)


# ---------------------------------------------------------------------------
# Validated report
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ValidatedReport:
    """Immutable container for a fully validated XP report."""

    player_id: str
    player_name: str
    tier: int | None
    hunting_xp: int | None
    business_xp: int | None
    player_xp: int | None
    heist_streak: int
    player_count: int | None
    login: bool


class ValidationError(Exception):
    """Raised when a report payload fails validation."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def clean_str(value: Any, max_len: int) -> str | None:
    """Return a trimmed, printable string or ``None``."""
    if not isinstance(value, str):
        return None
    cleaned = "".join(ch for ch in value if ch == " " or ch.isprintable())
    return cleaned.strip()[:max_len] or None


def int_in_range(value: Any, lo: int, hi: int) -> int | None:
    """Coerce *value* to ``int`` if within [*lo*, *hi*].

    Returns ``None`` when *value* is ``None``.
    Raises :class:`ValueError` otherwise.
    """
    if value is None:
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"not an integer: {value!r}")
    if not lo <= n <= hi:
        raise ValueError(f"{n} outside [{lo}, {hi}]")
    return n


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def is_rate_limited(player_id: str) -> bool:
    """Return ``True`` if *player_id* has exceeded the report rate limit."""
    now = time.monotonic()
    bucket = _rate_buckets[player_id]
    cutoff = now - RATE_LIMIT_WINDOW
    # Prune expired entries from front of list
    while bucket and bucket[0] < cutoff:
        bucket.pop(0)
    if len(bucket) >= RATE_LIMIT_MAX:
        return True
    bucket.append(now)
    return False


# ---------------------------------------------------------------------------
# Top-level validation entry point
# ---------------------------------------------------------------------------


def validate_report(data: Mapping[str, Any]) -> ValidatedReport:
    """Validate and sanitise a raw report dict.

    Returns a :class:`ValidatedReport` on success.
    Raises :class:`ValidationError` on any invalid input.
    """
    if not isinstance(data, Mapping):
        raise ValidationError("expected JSON object")

    # Reject unexpected keys
    unknown = set(data.keys()) - ACCEPTED_FIELDS
    if unknown:
        raise ValidationError(f"unknown fields: {sorted(unknown)}")

    # player_id
    raw_id = data.get("player_id")
    if not isinstance(raw_id, (str, int)):
        raise ValidationError("player_id is required")
    player_id = str(raw_id).strip()
    if not _PLAYER_ID_RE.match(player_id):
        raise ValidationError("player_id must be numeric")

    # player_name
    player_name = clean_str(data.get("player_name"), _PLAYER_NAME_MAX) or "Unknown"

    # Numeric fields
    try:
        tier = int_in_range(data.get("tier"), *TIER_RANGE)
        hunting_xp = int_in_range(data.get("hunting_xp"), *XP_RANGE)
        business_xp = int_in_range(data.get("business_xp"), *XP_RANGE)
        player_xp = int_in_range(data.get("player_xp"), *XP_RANGE)
        heist_streak = int_in_range(data.get("heist_streak"), *STREAK_RANGE) or 0
        player_count = int_in_range(data.get("player_count"), *PLAYER_COUNT_RANGE)
    except ValueError as exc:
        raise ValidationError(f"invalid field: {exc}", status_code=422)

    login = data.get("login") is True

    return ValidatedReport(
        player_id=player_id,
        player_name=player_name,
        tier=tier,
        hunting_xp=hunting_xp,
        business_xp=business_xp,
        player_xp=player_xp,
        heist_streak=heist_streak,
        player_count=player_count,
        login=login,
    )
