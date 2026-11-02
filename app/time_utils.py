from __future__ import annotations

from datetime import UTC, datetime


def as_utc(value: datetime, *, label: str = "timestamp") -> datetime:
    """Return an aware UTC datetime without guessing a timezone for naive values."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone offset")
    return value.astimezone(UTC)


def utc_iso(value: datetime, *, label: str = "timestamp") -> str:
    return as_utc(value, label=label).isoformat()


def parse_utc(value: str, *, label: str = "stored timestamp") -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is not a valid ISO 8601 timestamp") from exc
    return as_utc(parsed, label=label)
