"""Tests for the shared throttle-timestamp helper."""

from __future__ import annotations

import datetime as dt

from vastai_operator.timeutils import seconds_since


def test_none_and_empty_return_none() -> None:
    assert seconds_since(None) is None
    assert seconds_since("") is None


def test_malformed_timestamp_returns_none() -> None:
    # The whole point: a corrupt status timestamp must not raise — callers
    # then proceed as if there were no prior timestamp (no throttle).
    assert seconds_since("not-a-timestamp") is None
    assert seconds_since("2026-13-99T99:99:99") is None


def test_elapsed_is_positive_for_past_timestamp() -> None:
    past = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=30)).isoformat()
    elapsed = seconds_since(past)
    assert elapsed is not None
    assert 29 <= elapsed <= 60


def test_future_timestamp_is_negative() -> None:
    future = (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=30)).isoformat()
    elapsed = seconds_since(future)
    assert elapsed is not None
    assert elapsed < 0
