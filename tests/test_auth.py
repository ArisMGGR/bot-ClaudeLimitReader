"""Tests for the token store, refresh serialization, and network error mapping."""

import http.client
import io
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clr import auth, usage  # noqa: E402
from clr.auth import AuthError, Tokens  # noqa: E402
from clr.usage import UsageError  # noqa: E402


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "tokens.json"
        patchers = [
            mock.patch.object(auth, "TOKEN_PATH", self.path),
            mock.patch.object(auth, "ensure_app_dir", lambda: self.path.parent),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def store(self, **overrides):
        data = {"access_token": "old-access", "refresh_token": "old-refresh",
                "expires_at": 0, **overrides}
        self.path.write_text(json.dumps(data), encoding="utf-8")


class LoadTokensTest(StoreTestCase):
    def test_corrupt_files_are_auth_errors(self):
        for label, raw in (
            ("not json", b"{nope"),
            ("a list", b"[1, 2]"),
            ("null", b"null"),
            ("bad expiry", b'{"access_token": "a", "refresh_token": "r", "expires_at": "soon"}'),
            ("bad utf-8", b"\xff\xfe\x00"),
        ):
            with self.subTest(label):
                self.path.write_bytes(raw)
                with self.assertRaises(AuthError):
                    auth.load_tokens()

    def test_unreadable_file_is_an_auth_error(self):
        """A tokens.json that cannot be opened is not a traceback (F-051)."""
        self.path.mkdir()  # opening a directory fails on every platform
        with self.assertRaises(AuthError) as ctx:
            auth.load_tokens()
        self.assertIn("cannot read", str(ctx.exception))

    def test_save_leaves_no_temp_files(self):
        auth.save_tokens(Tokens("a", "r", time.time() + 3600))
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["tokens.json"])

    def test_save_is_owner_only(self):
        if sys.platform == "win32":
            self.skipTest("POSIX permission bits only")
        auth.save_tokens(Tokens("a", "r", time.time() + 3600))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)


class RefreshSerializationTest(StoreTestCase):
    """Two threads with an expired token must spend the refresh token once (F-013)."""

    def test_concurrent_refreshes_hit_the_server_once(self):
        self.store()
        calls = []

        def post(url, payload, timeout=30):
            calls.append(payload["refresh_token"])
            time.sleep(0.05)  # widen the race window
            n = len(calls)
            return 200, {"access_token": f"new-access-{n}", "refresh_token": f"new-refresh-{n}",
                         "expires_in": 3600}

        results = []
        with mock.patch.object(auth, "_post", post):
            threads = [threading.Thread(target=lambda: results.append(auth.get_access_token()))
                       for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(calls, ["old-refresh"])
        self.assertEqual(results, ["new-access-1"] * 4)

    def test_force_refresh_reuses_a_token_another_thread_minted(self):
        self.store(access_token="fresh-from-elsewhere", expires_at=time.time() + 3600)
        with mock.patch.object(auth, "_post") as post:
            tokens = auth.force_refresh("the-rejected-one")
        post.assert_not_called()
        self.assertEqual(tokens.access_token, "fresh-from-elsewhere")

    def test_force_refresh_refreshes_the_rejected_token(self):
        self.store(access_token="rejected", expires_at=time.time() + 3600)
        reply = (200, {"access_token": "new", "expires_in": 3600})
        with mock.patch.object(auth, "_post", return_value=reply) as post:
            tokens = auth.force_refresh("rejected")
        post.assert_called_once()
        self.assertEqual(tokens.access_token, "new")


class NetworkErrorTest(unittest.TestCase):
    """Network failures surface as the domain errors callers handle (F-008)."""

    def test_token_endpoint_unreachable(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            with self.assertRaises(AuthError):
                auth._post("https://example.invalid/token", {})

    def test_token_endpoint_timeout(self):
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaises(AuthError):
                auth._post("https://example.invalid/token", {})

    def test_usage_endpoint_unreachable_or_slow(self):
        for error in (urllib.error.URLError("refused"), TimeoutError("timed out")):
            with self.subTest(type(error).__name__):
                with mock.patch("urllib.request.urlopen", side_effect=error):
                    with self.assertRaises(UsageError):
                        usage._get("https://example.invalid/usage", "token")

    def test_usage_endpoint_returns_html(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = b"<html>proxy login</html>"
        with mock.patch("urllib.request.urlopen", return_value=response):
            with self.assertRaises(UsageError):
                usage._get("https://example.invalid/usage", "token")

    def test_a_truncated_body_is_a_domain_error(self):
        """IncompleteRead is an HTTPException, not an OSError (F-060)."""
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.side_effect = http.client.IncompleteRead(b'{"five_', 100)
        for label, call, error in (
            ("usage", lambda: usage._get("https://example.invalid/usage", "token"), UsageError),
            ("token", lambda: auth._post("https://example.invalid/token", {}), AuthError),
        ):
            with self.subTest(label):
                with mock.patch("urllib.request.urlopen", return_value=response):
                    with self.assertRaises(error):
                        call()

    def test_an_unreadable_error_body_keeps_its_status(self):
        """The body of a 401 or 5xx can be cut off too; its status must survive (F-060)."""

        def unreadable(exc):
            class Body(io.BytesIO):
                def read(self, *args):
                    raise exc

            return Body()

        for exc in (http.client.IncompleteRead(b"x", 5), ConnectionResetError("reset")):
            for code in (401, 502):
                for label, call in (
                    ("usage", lambda: usage._get("https://example.invalid/usage", "token")),
                    ("token", lambda: auth._post("https://example.invalid/token", {})),
                ):
                    with self.subTest(label, error=type(exc).__name__, code=code):
                        error = urllib.error.HTTPError(
                            "https://example.invalid", code, "", {}, unreadable(exc)
                        )
                        with mock.patch("urllib.request.urlopen", side_effect=error):
                            status, body = call()
                        self.assertEqual(status, code)
                        self.assertIn("could not be read", str(body))


if __name__ == "__main__":
    unittest.main()
