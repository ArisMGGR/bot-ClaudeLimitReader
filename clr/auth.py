"""OAuth login, token storage, and refresh.

The bot holds its own refresh-token lineage. Refresh tokens rotate on every use,
so a copied Claude Code credential would be invalidated by whichever client
refreshed last; performing a separate authorization avoids that entirely.

Login uses authorization-code + PKCE against the manual redirect, which shows
the code on screen instead of calling back to a local port -- so it works on a
headless host. (The device-code grant exists server-side but rejects this
client_id with `unauthorized_client`.)
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .config import (
    API_BASE,
    CLIENT_ID,
    REFRESH_SKEW_SECONDS,
    SCOPES,
    TOKEN_PATH,
    USER_AGENT,
    ensure_app_dir,
    write_private,
)

TOKEN_URL = f"{API_BASE}/v1/oauth/token"
AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"

# Redirect target that renders the authorization code on screen for the user to
# copy, instead of calling back to a local port. This is what makes the login
# work on a headless host.
MANUAL_REDIRECT_URL = "https://platform.claude.com/oauth/code/callback"


class AuthError(Exception):
    """Login or refresh failed in a way the user must act on."""


def _b64url(raw: bytes) -> str:
    """Base64url without padding, as PKCE requires."""
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: float  # epoch seconds
    scope: str = ""
    account_email: str = ""

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at - REFRESH_SKEW_SECONDS

    def to_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "scope": self.scope,
            "account_email": self.account_email,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Tokens":
        return cls(
            access_token=d["access_token"],
            refresh_token=d["refresh_token"],
            expires_at=float(d.get("expires_at", 0)),
            scope=d.get("scope", ""),
            account_email=d.get("account_email", ""),
        )

    @classmethod
    def from_response(cls, r: dict, fallback_refresh: str = "") -> "Tokens":
        account = r.get("account") or {}
        return cls(
            access_token=r["access_token"],
            refresh_token=r.get("refresh_token") or fallback_refresh,
            expires_at=time.time() + float(r.get("expires_in", 28800)),
            scope=r.get("scope", ""),
            account_email=account.get("email_address", "") if isinstance(account, dict) else "",
        )


def _decode(raw: bytes) -> dict:
    """Parse a JSON object body; anything else becomes an error payload."""
    text = raw.decode(errors="replace")
    try:
        body = json.loads(text or "{}")
    except json.JSONDecodeError:
        return {"error": "http_error", "error_description": text[:500]}
    if not isinstance(body, dict):
        return {"error": "http_error", "error_description": text[:500]}
    return body


def _post(url: str, payload: dict, timeout: int = 30) -> tuple[int, dict]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _decode(resp.read())
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
        except (OSError, http.client.HTTPException) as read_error:
            # An error status whose body is cut off is still that status.
            return e.code, {
                "error": "http_error",
                "error_description": f"the body could not be read: {read_error}",
            }
        return e.code, _decode(raw)
    except urllib.error.URLError as e:
        raise AuthError(f"Could not reach {url}: {e.reason}") from e
    except (OSError, http.client.HTTPException) as e:
        # Timeouts and resets mid-response; a truncated body (IncompleteRead)
        # is an HTTPException, not an OSError.
        raise AuthError(f"Could not reach {url}: {e}") from e


# ---------------------------------------------------------------- token store

# Refresh tokens rotate on every use, so two threads refreshing at once would
# spend the same token twice. Every read-modify-write of tokens.json holds this.
_token_lock = threading.RLock()


def save_tokens(tokens: Tokens) -> None:
    try:
        ensure_app_dir()
        with _token_lock:
            write_private(TOKEN_PATH, json.dumps(tokens.to_dict(), indent=2).encode())
    except OSError as e:
        # The new tokens are lost, and after a refresh the stored refresh token
        # is already spent: once the folder is writable, only a login helps.
        raise AuthError(
            f"cannot write {TOKEN_PATH}: {e}; make {TOKEN_PATH.parent} writable, "
            "then run `python run.py login` again"
        ) from e


def load_tokens() -> Tokens | None:
    if not TOKEN_PATH.exists():
        return None
    try:
        # Held while reading too: Windows refuses to replace a file that is open.
        with _token_lock, open(TOKEN_PATH, encoding="utf-8") as f:
            return Tokens.from_dict(json.load(f))
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        # ValueError covers bad JSON, bad UTF-8 and a non-numeric expires_at.
        raise AuthError(
            f"{TOKEN_PATH} is corrupt ({e}); run `python run.py login` again"
        ) from e
    except OSError as e:  # a directory, or owned by another user (e.g. root)
        raise AuthError(
            f"cannot read {TOKEN_PATH}: {e}; fix its permissions or run "
            "`python run.py login` again"
        ) from e


# ----------------------------------------------------------------- login


@dataclass
class PendingLogin:
    """An authorization request awaiting the code the user pastes back."""

    url: str
    state: str
    code_verifier: str


def start_login() -> PendingLogin:
    """Build the authorization URL for an authorization-code + PKCE login.

    Completing a fresh authorization mints a refresh token independent of any
    other client's, so the bot never competes with a local Claude Code install
    for the same rotating token.
    """
    code_verifier = _b64url(os.urandom(32))
    challenge = _b64url(hashlib.sha256(code_verifier.encode()).digest())
    state = _b64url(os.urandom(24))

    params = {
        "code": "true",  # render the code for manual copy rather than redirecting
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": MANUAL_REDIRECT_URL,
        "scope": SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return PendingLogin(
        url=f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}",
        state=state,
        code_verifier=code_verifier,
    )


def finish_login(pending: PendingLogin, pasted: str) -> Tokens:
    """Exchange the pasted authorization code for tokens."""
    pasted = pasted.strip()
    if not pasted:
        raise AuthError("No code entered.")

    # The success page shows the code as `code#state`; accept either form.
    code, _, returned_state = pasted.partition("#")
    code = code.strip()
    returned_state = returned_state.strip()

    if returned_state and returned_state != pending.state:
        raise AuthError(
            "The state value in the pasted code does not match this login "
            "attempt. Start over with `python run.py login`."
        )

    status, body = _post(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": MANUAL_REDIRECT_URL,
            "client_id": CLIENT_ID,
            "code_verifier": pending.code_verifier,
            "state": pending.state,
        },
    )
    if status != 200 or "access_token" not in body:
        err = body.get("error_description") or body.get("error") or body
        raise AuthError(f"Could not exchange the code (HTTP {status}): {err}")

    return Tokens.from_response(body)


def import_local_credentials(path: Path | None = None) -> Tokens:
    """Fallback: adopt the refresh token from a local Claude Code install.

    Note this takes over that token's lineage — the local CLI will need to log
    in again once the bot performs its next refresh.
    """
    path = path or Path.home() / ".claude" / ".credentials.json"
    if not path.exists():
        raise AuthError(f"No Claude Code credential file at {path}")

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise AuthError(f"Could not read {path}: {e}") from e
    oauth = (data.get("claudeAiOauth") if isinstance(data, dict) else None) or {}
    refresh = oauth.get("refreshToken")
    if not refresh:
        raise AuthError(f"{path} has no refreshToken")

    tokens = Tokens(
        access_token=oauth.get("accessToken", ""),
        refresh_token=refresh,
        expires_at=float(oauth.get("expiresAt", 0)) / 1000.0,
        scope=" ".join(oauth.get("scopes", [])),
    )
    return refresh_tokens(tokens)


# ------------------------------------------------------------------- refresh


def refresh_tokens(tokens: Tokens) -> Tokens:
    """Exchange the refresh token for a new access token and persist the result."""
    status, body = _post(
        TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
            "client_id": CLIENT_ID,
        },
    )
    if status != 200 or "access_token" not in body:
        err = body.get("error_description") or body.get("error") or body
        raise AuthError(
            f"Token refresh failed (HTTP {status}): {err}\n"
            "The refresh token is stale or was rotated by another client. "
            "Run `python run.py login` to re-authorize."
        )

    fresh = Tokens.from_response(body, fallback_refresh=tokens.refresh_token)
    if not fresh.account_email:
        fresh.account_email = tokens.account_email
    save_tokens(fresh)
    return fresh


NOT_LOGGED_IN = (
    "Not logged in. Run `python run.py login` on this host to authorize the bot."
)


def _refresh_if(stale) -> Tokens:
    """Re-read the stored tokens under the lock and refresh only if still stale.

    A caller that waited on the lock usually finds another thread has already
    refreshed, and must use that result instead of spending the rotated token.
    """
    with _token_lock:
        current = load_tokens()
        if current is None:
            raise AuthError(NOT_LOGGED_IN)
        return refresh_tokens(current) if stale(current) else current


def get_access_token() -> str:
    """Return a valid access token, refreshing on demand."""
    tokens = load_tokens()
    if tokens is None:
        raise AuthError(NOT_LOGGED_IN)
    if tokens.expired:
        tokens = _refresh_if(lambda t: t.expired)
    return tokens.access_token


def force_refresh(rejected_access_token: str) -> Tokens:
    """Refresh after the server rejected a token, unless someone already did."""
    return _refresh_if(lambda t: t.expired or t.access_token == rejected_access_token)
