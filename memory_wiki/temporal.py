"""Temporal validity and supersession helpers for structured claims."""

from __future__ import annotations

from datetime import date, datetime, timezone


TEMPORAL_STATES = frozenset(
    ("current", "unknown", "future", "expired", "superseded", "invalid"))


def parse_timestamp(value):
    """Parse an ISO date or datetime as an aware UTC datetime."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    try:
        if len(value) == 10:
            parsed = datetime.combine(
                date.fromisoformat(value), datetime.min.time())
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def coerce_as_of(value=None):
    """Coerce a date, datetime, or ISO string to UTC."""
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        parsed = parse_timestamp(value)
        if parsed is None:
            raise ValueError(f"as_of must be an ISO date or datetime: {value}")
        return parsed
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, datetime.min.time())
    if not isinstance(value, datetime):
        raise ValueError("as_of must be a date, datetime, or ISO string")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def claim_state(claim, as_of=None):
    """Classify a claim at one point in time.

    ``validUntil`` is an exclusive boundary. Claims without temporal bounds
    remain recallable and are labeled ``unknown`` rather than assumed current.
    """
    if not isinstance(claim, dict):
        return "invalid"
    if str(claim.get("status") or "").strip().lower() == "superseded":
        return "superseded"
    raw_start = claim.get("validFrom")
    raw_end = claim.get("validUntil")
    start = parse_timestamp(raw_start)
    end = parse_timestamp(raw_end)
    if raw_start not in (None, "") and start is None:
        return "invalid"
    if raw_end not in (None, "") and end is None:
        return "invalid"
    if start is not None and end is not None and start > end:
        return "invalid"
    if start is None and end is None:
        return "unknown"
    when = coerce_as_of(as_of)
    if start is not None and when < start:
        return "future"
    if end is not None and when >= end:
        return "expired"
    return "current"


def is_recallable(claim, as_of=None, include_historical=False):
    """Whether automatic recall may return a claim at ``as_of``."""
    state = claim_state(claim, as_of)
    if include_historical:
        return state != "invalid"
    return state in ("current", "unknown")


def interval_errors(claim, path="claim"):
    """Return deterministic validation errors for temporal claim fields."""
    errors = []
    parsed = {}
    for field in ("validFrom", "validUntil"):
        if field not in claim:
            continue
        value = claim[field]
        parsed[field] = parse_timestamp(value)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{path}.{field} must be a non-empty ISO timestamp")
        elif parsed[field] is None:
            errors.append(f"{path}.{field} must be an ISO date or datetime")
    if (parsed.get("validFrom") is not None
            and parsed.get("validUntil") is not None
            and parsed["validFrom"] > parsed["validUntil"]):
        errors.append(f"{path}.validFrom must not be after {path}.validUntil")
    return errors
