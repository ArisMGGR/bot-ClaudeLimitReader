"""Fetch and normalize the Claude subscription usage report."""

from __future__ import annotations

import datetime as dt
import http.client
import json
import math
import urllib.error
import urllib.request
from dataclasses import dataclass

from .auth import force_refresh, get_access_token, load_tokens
from .config import API_BASE, USER_AGENT

USAGE_URL = f"{API_BASE}/api/oauth/usage"

# Human labels for the window keys the endpoint reports. Unknown keys fall back
# to a prettified version of the key itself, so a new window type still renders.
WINDOW_LABELS = {
    "session": "5-hour session",
    "five_hour": "5-hour session",
    "weekly_all": "Weekly (all models)",
    "seven_day": "Weekly (all models)",
    "weekly_opus": "Weekly (Opus)",
    "seven_day_opus": "Weekly (Opus)",
    "weekly_sonnet": "Weekly (Sonnet)",
    "seven_day_sonnet": "Weekly (Sonnet)",
    "seven_day_oauth_apps": "Weekly (OAuth apps)",
    "seven_day_cowork": "Weekly (Cowork)",
}

# Named windows to read when the normalized `limits` array is absent.
FALLBACK_WINDOW_KEYS = (
    "five_hour",
    "seven_day",
    "seven_day_opus",
    "seven_day_sonnet",
    "seven_day_oauth_apps",
    "seven_day_cowork",
)

# The `limits` kind that describes the same window as a named fallback block,
# so a window reported both ways is rendered once.
FALLBACK_ALIASES = {
    "five_hour": "session",
    "seven_day": "weekly_all",
    "seven_day_opus": "weekly_opus",
    "seven_day_sonnet": "weekly_sonnet",
}

# Nominal window durations, used to locate the start of a window from its reset
# time so pace can be computed without any stored history.
WINDOW_DURATIONS = {
    "session": dt.timedelta(hours=5),
    "five_hour": dt.timedelta(hours=5),
}
DEFAULT_WINDOW_DURATION = dt.timedelta(days=7)


class UsageError(Exception):
    """The usage endpoint could not be read."""


@dataclass
class Window:
    key: str
    label: str
    percent: float
    resets_at: dt.datetime | None
    severity: str = "normal"
    group: str = ""

    @property
    def duration(self) -> dt.timedelta:
        return WINDOW_DURATIONS.get(self.key, DEFAULT_WINDOW_DURATION)

    @property
    def is_weekly(self) -> bool:
        return self.group == "weekly" or self.duration >= dt.timedelta(days=1)


@dataclass
class UsageReport:
    windows: list[Window]
    fetched_at: dt.datetime
    account_email: str = ""

    @property
    def primary_weekly(self) -> Window | None:
        """The window the pace tracker runs on: the all-models weekly cap."""
        for key in ("weekly_all", "seven_day"):
            for w in self.windows:
                if w.key == key:
                    return w
        weeklies = [w for w in self.windows if w.is_weekly]
        return max(weeklies, key=lambda w: w.percent) if weeklies else None


def _parse_ts(value) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        # A time at the edge of the range can overflow converting to UTC.
        return parsed.astimezone(dt.timezone.utc)
    except (ValueError, OverflowError):
        return None


def _label_for(key: str) -> str:
    return WINDOW_LABELS.get(key, key.replace("_", " ").capitalize())


def _percent(value) -> float | None:
    """A finite number, or None; NaN would break the bar and the pace math."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def parse_usage(payload: dict) -> UsageReport:
    """Normalize the raw response into a list of windows.

    Prefers the `limits` array, which carries a server-computed severity, and
    falls back to the individually named window objects. Entries it cannot
    read are skipped rather than failing the whole report.
    """
    windows: list[Window] = []
    seen: set[str] = set()

    limits = payload.get("limits")
    for entry in limits if isinstance(limits, list) else []:
        if not isinstance(entry, dict):
            continue
        key = entry.get("kind") or entry.get("group") or ""
        percent = _percent(entry.get("percent"))
        if not isinstance(key, str) or not key or percent is None:
            continue
        windows.append(
            Window(
                key=key,
                label=_label_for(key),
                percent=percent,
                resets_at=_parse_ts(entry.get("resets_at")),
                severity=entry.get("severity") or "normal",
                group=entry.get("group") or "",
            )
        )
        seen.add(key)

    for key in FALLBACK_WINDOW_KEYS:
        block = payload.get(key)
        if not isinstance(block, dict):
            continue
        percent = _percent(block.get("utilization"))
        if percent is None:
            continue
        # Skip anything the limits array already described.
        alias = FALLBACK_ALIASES.get(key)
        if key in seen or (alias and alias in seen):
            continue
        windows.append(
            Window(
                key=key,
                label=_label_for(key),
                percent=percent,
                resets_at=_parse_ts(block.get("resets_at")),
                group="session" if key == "five_hour" else "weekly",
            )
        )
        seen.add(key)

    # Sessions first, then weeklies, each by descending consumption.
    windows.sort(key=lambda w: (0 if w.group == "session" else 1, -w.percent))

    return UsageReport(windows=windows, fetched_at=dt.datetime.now(dt.timezone.utc))


def _decode(raw: bytes) -> dict | None:
    """Parse a JSON object body, or None when it is not one."""
    try:
        body = json.loads(raw.decode(errors="replace") or "{}")
    except json.JSONDecodeError:
        return None
    return body if isinstance(body, dict) else None


def _get(url: str, token: str, timeout: int = 30) -> tuple[int, dict]:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "anthropic-beta": "oauth-2025-04-20",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = _decode(resp.read())
            if body is None:
                raise UsageError("Usage endpoint returned a response that is not a JSON object")
            return resp.status, body
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
        except (OSError, http.client.HTTPException) as read_error:
            # The status still counts, so a 401 still triggers the refresh.
            return e.code, {"error": f"HTTP {e.code}; the body could not be read: {read_error}"}
        body = _decode(raw)
        return e.code, body if body is not None else {"error": raw.decode(errors="replace")[:500]}
    except urllib.error.URLError as e:
        raise UsageError(f"Could not reach the usage endpoint: {e.reason}") from e
    except (OSError, http.client.HTTPException) as e:
        # Timeouts and resets mid-response; a truncated body (IncompleteRead)
        # is an HTTPException, not an OSError.
        raise UsageError(f"Could not reach the usage endpoint: {e}") from e


def fetch_usage() -> UsageReport:
    """Fetch current usage, refreshing the access token once on a 401."""
    token = get_access_token()
    status, body = _get(USAGE_URL, token)

    if status == 401:
        # Token rejected despite looking current; force one refresh and retry.
        token = force_refresh(token).access_token
        status, body = _get(USAGE_URL, token)

    if status != 200:
        raise UsageError(
            f"Usage endpoint returned HTTP {status}: "
            f"{body.get('error_description') or body.get('error') or body}"
        )

    report = parse_usage(body)
    stored = load_tokens()
    if stored:
        report.account_email = stored.account_email
    return report
