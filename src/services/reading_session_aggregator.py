"""Bounded estimates for progress-derived reading sessions (not player telemetry)."""

import math
import os

MAX_SESSION_SECONDS = 14400

# A closed session stops being retried once it has gone undelivered for this long,
# so a decommissioned destination cannot pin the queue or grow the buffer table without bound.
DELIVERY_RETRY_WINDOW_SECONDS = 7 * 86400


def _positive_number(value: object, default: float) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else default
    except (TypeError, ValueError):
        return default


# The poll-derived floor exists so sparse scheduled observations do not split one
# sitting, but it must not run away: an install driven by instant sync can set a
# sync period of hours, and twice that would merge a week of reading into one
# session. Past this ceiling the configured value stands on its own.
MAX_DERIVED_GAP_MINUTES = 30


def effective_session_gap_seconds() -> float:
    """Read the idle gap, allowing two scheduled observations before splitting."""
    configured = _positive_number(os.environ.get("READING_SESSION_MERGE_MINUTES"), 5)
    poll = _positive_number(os.environ.get("SYNC_PERIOD_MINS"), 5)
    derived = min(2 * poll, MAX_DERIVED_GAP_MINUTES)
    return max(1, configured, derived) * 60


def uncovered_fraction(start_progress: float, end_progress: float,
                       covered: list[tuple[float, float]]) -> float:
    """Fraction of a progress span no existing session already covers.

    An aggregated session spans far more of a book than the per-observation
    sessions this replaced, so a destination that logs its own reading may cover
    part of ours without covering the reading we are about to report. Returns 1.0
    when nothing overlaps and 0.0 when the span is fully covered.
    """
    low, high = sorted((float(start_progress), float(end_progress)))
    width = high - low
    if width <= 0:
        return 0.0
    clipped = sorted(
        (max(low, min(a, b)), min(high, max(a, b))) for a, b in covered
    )
    covered_width = 0.0
    reached = low
    for span_start, span_end in clipped:
        if span_end <= reached:
            continue
        covered_width += span_end - max(span_start, reached)
        reached = span_end
    return max(0.0, min(1.0, (width - covered_width) / width))


def movement_seconds(position_delta: float, now: float, previous_at: float | None,
                     gap_seconds: float) -> float:
    """Bound a positive movement by observed time; unknown first intervals are estimates.

    Position alone cannot distinguish seeks, playback speeds, or pauses within an
    observation interval. Slow playback is undercounted; a first observation with
    no baseline may overcount fast playback. Never manufacture a minimum duration.
    """
    if not math.isfinite(position_delta) or position_delta <= 0:
        return 0.0
    elapsed = gap_seconds if previous_at is None else max(0.0, now - previous_at)
    return min(position_delta, elapsed, gap_seconds, MAX_SESSION_SECONDS)
