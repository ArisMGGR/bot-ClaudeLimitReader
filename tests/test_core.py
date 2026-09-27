"""Tests for parsing and pace math. Run with: python -m unittest discover tests"""

import datetime as dt
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clr.config import PaceConfig  # noqa: E402
from clr.pace import analyze  # noqa: E402
from clr.render import (  # noqa: E402
    COLOR_CRIT,
    COLOR_OK,
    COLOR_WARN,
    WARN_THRESHOLD,
    _severity_icon,
    bar,
    overall_color,
    pace_block,
    render_plain,
)
from clr.usage import parse_usage  # noqa: E402

UTC = dt.timezone.utc


def session_of(report):
    return next(w for w in report.windows if w.key in ("session", "five_hour"))

# Trimmed copy of a real response: Pro account, most windows null.
LIVE_PAYLOAD = {
    "five_hour": {"utilization": 34.0, "resets_at": "2026-09-03T23:30:00.299383+00:00"},
    "seven_day": {"utilization": 33.0, "resets_at": "2026-09-09T10:00:00.299407+00:00"},
    "seven_day_opus": None,
    "seven_day_sonnet": None,
    "extra_usage": {"is_enabled": False},
    "limits": [
        {
            "kind": "session",
            "group": "session",
            "percent": 34,
            "severity": "normal",
            "resets_at": "2026-09-03T23:30:00.299383+00:00",
            "is_active": True,
        },
        {
            "kind": "weekly_all",
            "group": "weekly",
            "percent": 33,
            "severity": "normal",
            "resets_at": "2026-09-09T10:00:00.299407+00:00",
            "is_active": False,
        },
    ],
}


class TestParse(unittest.TestCase):
    def test_parses_limits_array(self):
        report = parse_usage(LIVE_PAYLOAD)
        self.assertEqual([w.key for w in report.windows], ["session", "weekly_all"])
        self.assertEqual(report.primary_weekly.key, "weekly_all")
        self.assertEqual(session_of(report).percent, 34)

    def test_null_windows_are_skipped(self):
        report = parse_usage(LIVE_PAYLOAD)
        self.assertNotIn("seven_day_opus", [w.key for w in report.windows])

    def test_falls_back_to_named_windows(self):
        payload = {k: v for k, v in LIVE_PAYLOAD.items() if k != "limits"}
        report = parse_usage(payload)
        self.assertEqual({w.key for w in report.windows}, {"five_hour", "seven_day"})
        self.assertEqual(report.primary_weekly.key, "seven_day")

    def test_no_double_count_between_sources(self):
        # limits[] and the named blocks describe the same two windows.
        self.assertEqual(len(parse_usage(LIVE_PAYLOAD).windows), 2)

    def test_unknown_window_still_renders(self):
        payload = {
            "limits": [
                {"kind": "weekly_experimental", "group": "weekly", "percent": 10,
                 "resets_at": "2026-09-09T10:00:00+00:00", "is_active": True}
            ]
        }
        report = parse_usage(payload)
        self.assertEqual(report.windows[0].label, "Weekly experimental")

    def test_model_windows_reported_both_ways_appear_once(self):
        payload = {"limits": [], "seven_day_opus": {"utilization": 12.0, "resets_at": None},
                   "seven_day_sonnet": {"utilization": 4.0, "resets_at": None}}
        for kind in ("weekly_opus", "weekly_sonnet"):
            payload["limits"].append({"kind": kind, "group": "weekly", "percent": 12,
                                      "resets_at": "2026-09-09T10:00:00+00:00"})
        labels = [w.label for w in parse_usage(payload).windows]
        self.assertEqual(sorted(labels), ["Weekly (Opus)", "Weekly (Sonnet)"])

    def test_unreadable_entries_are_skipped(self):
        payload = {
            "limits": [{"kind": "session", "percent": "n/a"}, "junk"],
            "five_hour": {"utilization": 20.0, "resets_at": None},
            "seven_day": {"utilization": "?", "resets_at": None},
        }
        self.assertEqual([w.key for w in parse_usage(payload).windows], ["five_hour"])

    def test_non_finite_percent_is_skipped(self):
        # json.loads accepts NaN/Infinity; NaN used to crash the bar (F-031).
        payload = {"limits": [{"kind": "session", "percent": float("nan")},
                              {"kind": "weekly_all", "percent": "inf"}],
                   "five_hour": {"utilization": 20.0, "resets_at": None}}
        report = parse_usage(payload)
        self.assertEqual([w.key for w in report.windows], ["five_hour"])
        self.assertIn("5-hour session", render_plain(report))

    def test_empty_payload(self):
        report = parse_usage({})
        self.assertEqual(report.windows, [])
        self.assertIsNone(report.primary_weekly)
        self.assertIn("No usage windows", render_plain(report))


class TestPace(unittest.TestCase):
    def setUp(self):
        self.cfg = PaceConfig()
        self.report = parse_usage(LIVE_PAYLOAD)
        self.weekly = self.report.primary_weekly
        # 33.3h into the 168h window, matching the captured sample.
        self.now = dt.datetime(2026, 9, 3, 19, 20, tzinfo=UTC)

    def test_overspending_detected(self):
        p = analyze(self.weekly, self.cfg, now=self.now)
        self.assertEqual(p.verdict, "critical")
        self.assertAlmostEqual(p.elapsed_pct, 19.8, delta=0.3)
        self.assertAlmostEqual(p.ratio, 1.67, delta=0.05)

    def test_projects_early_exhaustion(self):
        p = analyze(self.weekly, self.cfg, now=self.now)
        self.assertTrue(p.will_exhaust_early)
        self.assertEqual(p.exhausts_at.date(), dt.date(2026, 9, 6))
        self.assertIn("hit 100%", p.detail())

    def test_on_pace(self):
        w = parse_usage(LIVE_PAYLOAD).primary_weekly
        w.percent = 50.0
        # Exactly halfway through the week.
        p = analyze(w, self.cfg, now=dt.datetime(2026, 9, 5, 22, 0, tzinfo=UTC))
        self.assertEqual(p.verdict, "on_pace")

    def test_under_pace(self):
        w = parse_usage(LIVE_PAYLOAD).primary_weekly
        w.percent = 10.0
        p = analyze(w, self.cfg, now=dt.datetime(2026, 9, 5, 22, 0, tzinfo=UTC))
        self.assertEqual(p.verdict, "under")
        self.assertFalse(p.will_exhaust_early)

    def test_too_early_to_judge(self):
        w = parse_usage(LIVE_PAYLOAD).primary_weekly
        w.percent = 3.0
        # ~1h into the window: below the noise floor.
        p = analyze(w, self.cfg, now=dt.datetime(2026, 9, 2, 11, 0, tzinfo=UTC))
        self.assertEqual(p.verdict, "unknown")
        self.assertIsNone(p.ratio)
        self.assertEqual(p.detail(), "")

    def test_exhausted(self):
        w = parse_usage(LIVE_PAYLOAD).primary_weekly
        w.percent = 100.0
        p = analyze(w, self.cfg, now=self.now)
        self.assertEqual(p.verdict, "exhausted")
        self.assertIn("exhausted", p.headline().lower())

    def test_zero_usage_has_no_projection(self):
        w = parse_usage(LIVE_PAYLOAD).primary_weekly
        w.percent = 0.0
        p = analyze(w, self.cfg, now=self.now)
        self.assertIsNone(p.exhausts_at)
        self.assertFalse(p.will_exhaust_early)

    def test_missing_reset_time(self):
        w = parse_usage(LIVE_PAYLOAD).primary_weekly
        w.resets_at = None
        p = analyze(w, self.cfg, now=self.now)
        self.assertEqual(p.verdict, "unknown")

    def test_session_uses_five_hour_duration(self):
        p = analyze(session_of(self.report), self.cfg, now=self.now)
        self.assertEqual(p.window.duration, dt.timedelta(hours=5))

    def test_extreme_server_values_do_not_break_the_report(self):
        """A tiny percent or a year-1 reset overflowed the datetime maths (F-064)."""
        now = dt.datetime.now(UTC)
        for label, entry in (
            ("tiny percent", {"percent": 1e-4,
                              "resets_at": (now + dt.timedelta(days=1)).isoformat()}),
            ("year-1 reset", {"percent": 5, "resets_at": "0001-01-02T00:00:00Z"}),
            ("year-1 with offset", {"percent": 5, "resets_at": "0001-01-01T00:00:00+14:00"}),
        ):
            with self.subTest(label):
                report = parse_usage({"limits": [
                    {"kind": "weekly_all", "group": "weekly", **entry}
                ]})
                self.assertIn("Weekly (all models)", render_plain(report))
                overall_color(report)  # the Discord embed computes this first

    def test_a_rate_too_slow_to_project_has_no_exhaustion_time(self):
        w = parse_usage(LIVE_PAYLOAD).primary_weekly
        w.percent = 1e-6  # 33h in, 100% would land past year 9999
        p = analyze(w, self.cfg, now=self.now)
        self.assertIsNone(p.exhausts_at)
        self.assertEqual(p.verdict, "under")


class TestRender(unittest.TestCase):
    def test_bar_bounds(self):
        self.assertEqual(bar(0), "░" * 20)
        self.assertEqual(bar(100), "█" * 20)
        self.assertEqual(len(bar(37.5)), 20)
        # Out-of-range input must not produce a ragged bar.
        self.assertEqual(len(bar(-5)), 20)
        self.assertEqual(len(bar(140)), 20)

    def test_embed_color_and_icon_share_the_warning_threshold(self):
        report = parse_usage(LIVE_PAYLOAD)
        session = session_of(report)
        session.percent = WARN_THRESHOLD + 2  # e.g. 77%: yellow icon
        weekly = report.primary_weekly
        weekly.percent = 1.0  # keep the weekly pace out of it
        self.assertEqual(_severity_icon(session), "🟡")
        with mock.patch("clr.render.analyze") as analyze_:
            analyze_.return_value = mock.Mock(verdict="on_pace")
            self.assertEqual(overall_color(report), COLOR_WARN)

    def test_embed_color_follows_the_server_severity_like_the_icon(self):
        """A severity the icon shows must colour the embed too (F-048)."""
        for severity, icon, color in (("critical", "🔴", COLOR_CRIT), ("warning", "🟡", COLOR_WARN)):
            with self.subTest(severity):
                report = parse_usage({"limits": [
                    {"kind": "session", "group": "session", "percent": 40, "severity": severity}
                ]})
                self.assertEqual(_severity_icon(report.windows[0]), icon)
                self.assertEqual(overall_color(report), color)

    def burning_session(self, weekly_percent):
        """A session 30% spent 30 minutes in (pace 3x), beside a weekly half-way through."""
        now = dt.datetime.now(UTC)
        return parse_usage({"limits": [
            {"kind": "session", "group": "session", "percent": 30,
             "resets_at": (now + dt.timedelta(hours=4, minutes=30)).isoformat()},
            {"kind": "weekly_all", "group": "weekly", "percent": weekly_percent,
             "resets_at": (now + dt.timedelta(days=3, hours=12)).isoformat()},
        ]})

    def test_a_session_pace_the_report_never_shows_does_not_colour_it(self):
        """Green icons and a green weekly pace make a green embed (F-053)."""
        report = self.burning_session(40)
        self.assertEqual({_severity_icon(w) for w in report.windows}, {"🟢"})
        self.assertIn("Comfortably under", pace_block(report))
        self.assertEqual(overall_color(report), COLOR_OK)

    def test_the_weekly_pace_still_colours_the_embed(self):
        report = self.burning_session(70)  # 70% spent, 50% elapsed: 1.4x, ahead
        self.assertIn("Overspending", pace_block(report))
        self.assertEqual(overall_color(report), COLOR_WARN)

    def test_plain_report_contains_key_facts(self):
        text = render_plain(parse_usage(LIVE_PAYLOAD))
        self.assertIn("5-hour session", text)
        self.assertIn("Weekly (all models)", text)
        self.assertIn("resets in", text)
        self.assertIn("%", text)


if __name__ == "__main__":
    unittest.main()
