"""Small time helpers shared by the timer-throttle guards.

Several controllers gate their work on "has at least N seconds passed since the
ISO timestamp we stored in status". Centralising the parse here keeps the
malformed-timestamp handling explicit (return ``None`` → caller proceeds as if
no prior timestamp) instead of scattering bare ``except ValueError: pass``.
"""

from __future__ import annotations

import datetime as dt


def seconds_since(iso_timestamp: str | None) -> float | None:
    """Return seconds elapsed since ``iso_timestamp``.

    ``None`` is returned when the value is missing or unparseable, so callers
    can treat "no usable prior timestamp" uniformly (typically: don't throttle).
    """
    if not iso_timestamp:
        return None
    try:
        parsed = dt.datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return None
    return (dt.datetime.now(dt.UTC) - parsed).total_seconds()
