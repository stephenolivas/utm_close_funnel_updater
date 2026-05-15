"""Small helpers for normalization and date filtering."""
from datetime import datetime, timedelta, timezone


def normalize(s) -> str:
    """Lowercase + strip. Returns empty string for None or non-string falsy values."""
    if s is None:
        return ""
    return str(s).strip().lower()


def is_recent(date_str: str, days: int) -> bool:
    """Return True if the ISO-8601 datetime string is within the last `days` days."""
    if not date_str:
        return False
    try:
        # Close returns ISO 8601 with trailing Z; fromisoformat needs +00:00 instead.
        dt = datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return dt >= cutoff
