"""Strict source-timeline timestamp parsing and interval validation."""

from __future__ import annotations

import re

MAX_SOURCE_MS = 60 * 60 * 1000
MIN_CLIP_MS = 1000
_MINUTE_SECONDS = re.compile(r"(\d{1,3}):([0-5]\d)(?:\.(\d{1,3}))?\Z")
_HOUR_SECONDS = re.compile(r"(\d{1,2}):([0-5]\d):([0-5]\d)(?:\.(\d{1,3}))?\Z")


def parse_source_time(value: str | None, *, name: str) -> int | None:
    """Parse M:SS[.mmm] or HH:MM:SS[.mmm] into integer milliseconds."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a timestamp string")
    match = _MINUTE_SECONDS.fullmatch(value)
    if match:
        minutes, seconds, fraction = match.groups()
        total_ms = (int(minutes) * 60 + int(seconds)) * 1000 + _milliseconds(fraction)
    else:
        match = _HOUR_SECONDS.fullmatch(value)
        if not match:
            raise ValueError(f"{name} must use M:SS[.mmm] or HH:MM:SS[.mmm]")
        hours, minutes, seconds, fraction = match.groups()
        if int(hours) > 1 or (
            int(hours) == 1 and (int(minutes) or int(seconds) or _milliseconds(fraction))
        ):
            raise ValueError(f"{name} exceeds the one-hour source limit")
        total_ms = (
            (int(hours) * 60 + int(minutes)) * 60 + int(seconds)
        ) * 1000 + _milliseconds(fraction)
    if total_ms > MAX_SOURCE_MS:
        raise ValueError(f"{name} exceeds the one-hour source limit")
    return total_ms


def parse_source_range(
    start_time: str | None, end_time: str | None,
) -> tuple[int | None, int | None]:
    """Parse and validate optional marks on the original source timeline."""
    start_ms = parse_source_time(start_time, name="start_time")
    end_ms = parse_source_time(end_time, name="end_time")
    if start_ms is None and end_ms is None:
        return None, None
    effective_start = start_ms if start_ms is not None else 0
    effective_end = end_ms if end_ms is not None else MAX_SOURCE_MS
    if effective_end <= effective_start:
        raise ValueError("end_time must be after start_time")
    if effective_end - effective_start < MIN_CLIP_MS:
        raise ValueError("Source clip must be at least one second")
    return start_ms, end_ms


def validate_source_range_ms(
    start_ms: int | None, end_ms: int | None,
) -> tuple[int | None, int | None]:
    """Validate persisted canonical bounds without converting through floats."""
    for name, value in (("start_ms", start_ms), ("end_ms", end_ms)):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > MAX_SOURCE_MS
        ):
            raise ValueError(f"{name} must be an integer within the one-hour source limit")
    if start_ms is None and end_ms is None:
        return None, None
    effective_start = start_ms if start_ms is not None else 0
    effective_end = end_ms if end_ms is not None else MAX_SOURCE_MS
    if effective_end <= effective_start:
        raise ValueError("end_ms must be after start_ms")
    if effective_end - effective_start < MIN_CLIP_MS:
        raise ValueError("Source clip must be at least one second")
    return start_ms, end_ms


def _milliseconds(fraction: str | None) -> int:
    return int(fraction.ljust(3, "0")) if fraction else 0
