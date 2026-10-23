"""Burn-rate analysis for a usage window.

Compares how much of a window's quota has been spent against how much of the
window has elapsed. `resets_at` plus the window's nominal duration gives the
window start, so no usage history needs to be stored.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .config import PaceConfig
from .usage import Window

# Below this much elapsed time the ratio is too noisy to judge.
MIN_ELAPSED_FRACTION = 0.02


@dataclass
class Pace:
    window: Window
    used_pct: float
    elapsed_pct: float
    ratio: float | None  # used / elapsed; None when too early to tell
    verdict: str  # "under" | "on_pace" | "ahead" | "critical" | "unknown" | "exhausted"
    exhausts_at: dt.datetime | None
    resets_at: dt.datetime | None
    time_to_reset: dt.timedelta | None

    @property
    def will_exhaust_early(self) -> bool:
        return (
            self.exhausts_at is not None
            and self.resets_at is not None
            and self.exhausts_at < self.resets_at
        )

    @property
    def margin_points(self) -> float:
        """Percentage points spent above (positive) or below the even pace."""
        return self.used_pct - self.elapsed_pct

    def headline(self) -> str:
        if self.verdict == "exhausted":
            return "Quota exhausted for this window"
        if self.verdict == "unknown":
            return "Too early in the window to judge pace"
        if self.verdict == "critical":
            return f"Overspending badly — {self.ratio:.2f}x the even pace"
        if self.verdict == "ahead":
            return f"Overspending — {self.ratio:.2f}x the even pace"
        if self.verdict == "under":
            return f"Comfortably under — {self.ratio:.2f}x the even pace"
        return "On pace"

    def detail(self) -> str:
        """A sentence explaining the consequence, or an empty string."""
        if self.verdict in ("unknown", "exhausted"):
            return ""
        if self.will_exhaust_early and self.exhausts_at and self.resets_at:
            early = self.resets_at - self.exhausts_at
            return (
                f"At this rate you hit 100% on "
                f"{self.exhausts_at:%a %d %b %H:%M} UTC, "
                f"{humanize(early)} before the window resets."
            )
        if self.verdict in ("on_pace", "under"):
            return "Current burn rate lasts through the full window."
        return ""


def humanize(delta: dt.timedelta) -> str:
    total = int(max(delta, dt.timedelta(0)).total_seconds())
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def format_countdown(delta: dt.timedelta | None) -> str:
    if delta is None:
        return "unknown"
    if delta.total_seconds() <= 0:
        return "now"
    return humanize(delta)


def analyze(
    window: Window,
    cfg: PaceConfig | None = None,
    now: dt.datetime | None = None,
) -> Pace:
    """Compute the pace verdict for a single window."""
    cfg = cfg or PaceConfig()
    now = now or dt.datetime.now(dt.timezone.utc)

    used = float(window.percent)
    resets_at = window.resets_at
    time_to_reset = (resets_at - now) if resets_at else None

    if resets_at is None:
        return Pace(window, used, 0.0, None, "unknown", None, None, None)

    duration = window.duration
    try:
        start = resets_at - duration
    except OverflowError:  # a reset time within a week of datetime.min
        return Pace(window, used, 0.0, None, "unknown", None, resets_at, time_to_reset)
    total_seconds = duration.total_seconds()
    elapsed_seconds = (now - start).total_seconds()
    elapsed_frac = min(max(elapsed_seconds / total_seconds, 0.0), 1.0)
    elapsed_pct = elapsed_frac * 100.0

    if used >= 100.0:
        return Pace(window, used, elapsed_pct, None, "exhausted", now, resets_at, time_to_reset)

    if elapsed_frac < MIN_ELAPSED_FRACTION:
        return Pace(window, used, elapsed_pct, None, "unknown", None, resets_at, time_to_reset)

    ratio = used / elapsed_pct if elapsed_pct > 0 else None

    # Project when the quota reaches 100% if consumption continues at the
    # average rate observed so far in this window.
    exhausts_at = None
    if used > 0 and elapsed_seconds > 0:
        rate_per_second = used / elapsed_seconds  # percent per second
        seconds_to_full = 100.0 / rate_per_second
        try:
            exhausts_at = start + dt.timedelta(seconds=seconds_to_full)
        except OverflowError:  # so slow a rate that 100% lies past year 9999
            exhausts_at = None

    margin = used - elapsed_pct
    if margin > cfg.tolerance_points:
        verdict = "critical" if (ratio or 0) >= cfg.critical_ratio else "ahead"
    elif margin < -cfg.tolerance_points:
        verdict = "under"
    else:
        verdict = "on_pace"

    return Pace(
        window=window,
        used_pct=used,
        elapsed_pct=elapsed_pct,
        ratio=ratio,
        verdict=verdict,
        exhausts_at=exhausts_at,
        resets_at=resets_at,
        time_to_reset=time_to_reset,
    )
