"""Shared rendering for both bot front-ends."""

from __future__ import annotations

from .config import PaceConfig
from .pace import Pace, analyze, format_countdown
from .usage import UsageReport, Window

BAR_FILLED = "█"
BAR_EMPTY = "░"
BAR_WIDTH = 20

# Percent-of-cap thresholds for the per-window status icon.
WARN_THRESHOLD = 75.0

# Discord embed colors, keyed by the worst window icon or weekly pace verdict.
COLOR_OK = 0x3BA55D
COLOR_WARN = 0xFAA61A
COLOR_CRIT = 0xED4245
COLOR_NEUTRAL = 0x5865F2

VERDICT_ICON = {
    "under": "🟢",
    "on_pace": "🟢",
    "ahead": "🟡",
    "critical": "🔴",
    "exhausted": "🔴",
    "unknown": "⚪",
}


def bar(percent: float, width: int = BAR_WIDTH) -> str:
    pct = min(max(percent, 0.0), 100.0)
    filled = int(round(pct / 100.0 * width))
    return BAR_FILLED * filled + BAR_EMPTY * (width - filled)


def _window_level(window: Window) -> str:
    """'critical', 'warning' or 'normal': how full a window is, or the server says it is.

    The window icon and the embed colour both use this, so they never disagree.
    """
    if window.percent >= 100 or window.severity in ("critical", "danger"):
        return "critical"
    if window.percent >= WARN_THRESHOLD or window.severity in ("warning", "warn"):
        return "warning"
    return "normal"


def _severity_icon(window: Window) -> str:
    """Status for a window, based on how full it is.

    Deliberately independent of the pace verdict: pace is about the weekly
    trend and is reported separately, whereas this icon answers "how close is
    this window to its cap right now".
    """
    return {"critical": "🔴", "warning": "🟡"}.get(_window_level(window), "🟢")


def window_line(window: Window, pace: Pace) -> str:
    """One monospace line: bar, percent, and reset countdown."""
    reset = format_countdown(pace.time_to_reset)
    return f"{bar(window.percent)}  {window.percent:>5.1f}%   resets in {reset}"


def build_paces(report: UsageReport, cfg: PaceConfig | None = None) -> list[tuple[Window, Pace]]:
    cfg = cfg or PaceConfig()
    return [(w, analyze(w, cfg)) for w in report.windows]


def overall_color(report: UsageReport, cfg: PaceConfig | None = None) -> int:
    """The worst of what the report shows: each window's icon and the weekly pace.

    Only the primary weekly pace is rendered, so a session's own pace verdict
    must not colour the embed; nothing on it would explain the colour.
    """
    if not report.windows:
        return COLOR_NEUTRAL
    levels = {_window_level(w) for w in report.windows}
    pace = primary_pace(report, cfg)
    verdict = pace.verdict if pace else None
    if "critical" in levels or verdict == "critical":
        return COLOR_CRIT
    if "warning" in levels or verdict == "ahead":
        return COLOR_WARN
    return COLOR_OK


def primary_pace(report: UsageReport, cfg: PaceConfig | None = None) -> Pace | None:
    weekly = report.primary_weekly
    return analyze(weekly, cfg or PaceConfig()) if weekly else None


def pace_block(report: UsageReport, cfg: PaceConfig | None = None) -> str:
    """The weekly burn-rate verdict, as a short paragraph."""
    pace = primary_pace(report, cfg)
    if pace is None:
        return "No weekly window reported."

    icon = VERDICT_ICON.get(pace.verdict, "⚪")
    lines = [f"{icon} {pace.headline()}"]

    if pace.verdict != "unknown":
        lines.append(
            f"Spent {pace.used_pct:.0f}% of the weekly cap "
            f"with {pace.elapsed_pct:.0f}% of the week elapsed "
            f"({pace.margin_points:+.0f} points)."
        )

    detail = pace.detail()
    if detail:
        lines.append(detail)

    return "\n".join(lines)


def render_plain(report: UsageReport, cfg: PaceConfig | None = None) -> str:
    """Full plain-text report, identical on every platform."""
    cfg = cfg or PaceConfig()

    if not report.windows:
        return "No usage windows reported for this account."

    out: list[str] = []
    for window, pace in build_paces(report, cfg):
        icon = _severity_icon(window)
        out.append(f"{icon} {window.label}")
        out.append(window_line(window, pace))
        out.append("")

    out.append(pace_block(report, cfg))
    out.append("")
    out.append(f"Checked {report.fetched_at:%Y-%m-%d %H:%M} UTC")
    return "\n".join(out).strip()
