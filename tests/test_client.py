"""Tests for doubletake.client — auth, SSE parsing, transport, backend selection."""

from __future__ import annotations

import http.client
import io
import json
import socket
import subprocess
import time
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

import pytest

from doubletake.client import (
    AuthError,
    BackendError,
    RateLimitError,
    _access_token,
    _claude_access_token,
    _claude_refresh_token,
    _discover_project,
    _emit_claude_text,
    _emit_gemini_text,
    _iter_sse_text,
    _read_claude_keychain,
    _stream,
    stream_review,
    _DEFAULT_CODE_ASSIST_MODEL,
    _DEFAULT_GEMINI_API_MODEL,
    _DEFAULT_CLAUDE_MODEL,
    _CLAUDE_FALLBACK_MODELS,
    _CODE_ASSIST_FALLBACK_MODELS,
    _GEMINI_API_FALLBACK_MODELS,
)


# ============================================================================
# Helpers
# ============================================================================

def _make_creds(
    access_token: str = "tok",
    expiry_ms: int | None = None,
    refresh_token: str = "ref",
) -> dict:
    if expiry_ms is None:
        expiry_ms = int((time.time() + 3600) * 1000)
    return {
        "access_token": access_token,
        "expiry_date": expiry_ms,
        "refresh_token": refresh_token,
    }


class _FakeSSEResp:
    """Iterable HTTP response mock for _stream tests."""
    def __init__(self, lines: list[bytes], raise_after: BaseException | None = None):
        self._lines = lines
        self._raise_after = raise_after

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def __iter__(self):
        yield from self._lines
        if self._raise_after is not None:
            raise self._raise_after


def _sse_lines(*payloads: str) -> list[bytes]:
    """Build SSE byte lines from JSON payloads (blank-line terminated)."""
    out: list[bytes] = []
    for p in payloads:
        out.append(f"data: {p}\n".encode())
        out.append(b"\n")
    return out


def _gemini_chunk(text: str, unwrap: bool = False) -> str:
    inner = {"candidates": [{"content": {"parts": [{"text": text}]}}]}
    return json.dumps({"response": inner} if unwrap else inner)


# ============================================================================
# _emit_gemini_text
# ============================================================================

class TestEmitGeminiText:
    def _call(self, obj: object, unwrap: bool = False) -> list[str]:
        return list(_emit_gemini_text(json.dumps(obj), unwrap=unwrap))

    def test_basic_text_no_wrap(self):
        obj = {"candidates": [{"content": {"parts": [{"text": "hello"}]}}]}
        assert self._call(obj) == ["hello"]

    def test_basic_text_with_wrap(self):
        obj = {"response": {"candidates": [{"content": {"parts": [{"text": "hi"}]}}]}}
        assert self._call(obj, unwrap=True) == ["hi"]

    def test_multiple_parts(self):
        obj = {"candidates": [{"content": {"parts": [
            {"text": "a"}, {"text": "b"}, {"text": "c"}
        ]}}]}
        assert self._call(obj) == ["a", "b", "c"]

    def test_multiple_candidates(self):
        obj = {"candidates": [
            {"content": {"parts": [{"text": "x"}]}},
            {"content": {"parts": [{"text": "y"}]}},
        ]}
        assert self._call(obj) == ["x", "y"]

    def test_skips_thought_parts(self):
        obj = {"candidates": [{"content": {"parts": [
            {"text": "thought", "thought": True},
            {"text": "real"},
        ]}}]}
        assert self._call(obj) == ["real"]

    def test_skips_empty_string_text(self):
        obj = {"candidates": [{"content": {"parts": [{"text": ""}]}}]}
        assert self._call(obj) == []

    def test_skips_null_text(self):
        obj = {"candidates": [{"content": {"parts": [{"text": None}]}}]}
        assert self._call(obj) == []

    def test_skips_non_string_text(self):
        obj = {"candidates": [{"content": {"parts": [{"text": 123}]}}]}
        assert self._call(obj) == []

    def test_null_response_wrapper(self):
        assert self._call({"response": None}, unwrap=True) == []

    def test_null_content_safety_block(self):
        obj = {"candidates": [{"finishReason": "SAFETY"}]}
        assert self._call(obj) == []

    def test_explicit_null_content(self):
        obj = {"candidates": [{"content": None}]}
        assert self._call(obj) == []

    def test_missing_candidates(self):
        assert self._call({"response": {}}, unwrap=True) == []

    def test_null_candidates(self):
        assert self._call({"candidates": None}) == []

    def test_non_dict_candidate_skipped(self):
        obj = {"candidates": ["not-a-dict"]}
        assert self._call(obj) == []

    def test_non_dict_part_skipped(self):
        obj = {"candidates": [{"content": {"parts": ["not-a-dict", {"text": "ok"}]}}]}
        assert self._call(obj) == ["ok"]

    def test_invalid_json_silently_dropped(self):
        assert list(_emit_gemini_text("not json", unwrap=False)) == []

    def test_non_dict_json_silently_dropped(self):
        assert list(_emit_gemini_text("[1,2,3]", unwrap=False)) == []

    def test_wrap_missing_response_key(self):
        assert self._call({"something_else": {}}, unwrap=True) == []


# ============================================================================
# _emit_claude_text
# ============================================================================

class TestEmitClaudeText:
    def _call(self, obj: object) -> list[str]:
        return list(_emit_claude_text(json.dumps(obj)))

    def test_basic_text_delta(self):
        obj = {"type": "content_block_delta", "index": 0,
               "delta": {"type": "text_delta", "text": "hello"}}
        assert self._call(obj) == ["hello"]

    def test_skips_message_start(self):
        assert self._call({"type": "message_start", "message": {}}) == []

    def test_skips_ping(self):
        assert self._call({"type": "ping"}) == []

    def test_skips_content_block_start(self):
        obj = {"type": "content_block_start", "index": 0,
               "content_block": {"type": "text", "text": ""}}
        assert self._call(obj) == []

    def test_skips_content_block_stop(self):
        assert self._call({"type": "content_block_stop", "index": 0}) == []

    def test_skips_message_delta(self):
        obj = {"type": "message_delta",
               "delta": {"stop_reason": "end_turn", "stop_sequence": None}}
        assert self._call(obj) == []

    def test_skips_message_stop(self):
        assert self._call({"type": "message_stop"}) == []

    def test_skips_input_json_delta(self):
        obj = {"type": "content_block_delta", "index": 0,
               "delta": {"type": "input_json_delta", "partial_json": "{}"}}
        assert self._call(obj) == []

    def test_skips_empty_text(self):
        obj = {"type": "content_block_delta", "index": 0,
               "delta": {"type": "text_delta", "text": ""}}
        assert self._call(obj) == []

    def test_skips_null_text(self):
        obj = {"type": "content_block_delta", "index": 0,
               "delta": {"type": "text_delta", "text": None}}
        assert self._call(obj) == []

    def test_skips_non_string_text(self):
        obj = {"type": "content_block_delta", "index": 0,
               "delta": {"type": "text_delta", "text": 42}}
        assert self._call(obj) == []

    def test_null_delta(self):
        obj = {"type": "content_block_delta", "index": 0, "delta": None}
        assert self._call(obj) == []

    def test_invalid_json(self):
        assert list(_emit_claude_text("not json")) == []

    def test_non_dict_json(self):
        assert list(_emit_claude_text("[1, 2]")) == []

    def test_no_type_field(self):
        assert list(_emit_claude_text('{"index": 0}')) == []


# ============================================================================
# _iter_sse_text
# ============================================================================

class TestIterSseText:
    def _run(self, lines: list[bytes], emit_fn=None) -> list[str]:
        if emit_fn is None:
            emit_fn = lambda c: _emit_gemini_text(c, unwrap=False)
        return list(_iter_sse_text(iter(lines), emit_fn))

    def test_single_event(self):
        assert self._run(_sse_lines(_gemini_chunk("hi"))) == ["hi"]

    def test_multiple_events(self):
        assert self._run(_sse_lines(_gemini_chunk("a"), _gemini_chunk("b"))) == ["a", "b"]

    def test_multiline_data_accumulated(self):
        # SSE joins multiple data: lines with "\n". Split at a JSON token boundary
        # so the concatenated string is still valid JSON.
        chunk = _gemini_chunk("multi")  # {"candidates": [...]}
        part1 = "{"
        part2 = chunk[1:]  # '"candidates": [...]}'
        lines = [
            f"data: {part1}\n".encode(),
            f"data: {part2}\n".encode(),
            b"\n",
        ]
        assert self._run(lines) == ["multi"]

    def test_sse_comment_ignored(self):
        lines = [b": comment\n", *_sse_lines(_gemini_chunk("x"))]
        assert self._run(lines) == ["x"]

    def test_event_field_ignored(self):
        lines = [b"event: foo\n", *_sse_lines(_gemini_chunk("y"))]
        assert self._run(lines) == ["y"]

    def test_id_and_retry_fields_ignored(self):
        lines = [b"id: 1\n", b"retry: 5000\n", *_sse_lines(_gemini_chunk("z"))]
        assert self._run(lines) == ["z"]

    def test_trailing_event_no_blank_line(self):
        lines = [f"data: {_gemini_chunk('trailing')}\n".encode()]
        assert self._run(lines) == ["trailing"]

    def test_data_leading_space_stripped(self):
        # SSE spec: one space after "data:" is stripped
        lines = [f"data: {_gemini_chunk('spaced')}\n".encode(), b"\n"]
        assert self._run(lines) == ["spaced"]

    def test_empty_stream(self):
        assert self._run([]) == []

    def test_blank_lines_only(self):
        assert self._run([b"\n", b"\n"]) == []

    def test_invalid_json_chunk_skipped(self):
        assert self._run([b"data: not-json\n", b"\n"]) == []

    def test_claude_emit_fn(self):
        delta = json.dumps({"type": "content_block_delta", "index": 0,
                            "delta": {"type": "text_delta", "text": "claude!"}})
        assert self._run(_sse_lines(delta), emit_fn=_emit_claude_text) == ["claude!"]

    def test_crlf_line_endings(self):
        chunk = _gemini_chunk("crlf")
        lines = [f"data: {chunk}\r\n".encode(), b"\r\n"]
        assert self._run(lines) == ["crlf"]


# ============================================================================
# _access_token (Gemini / Code Assist)
# ============================================================================

class TestAccessToken:
    def test_returns_valid_token_without_refresh(self, tmp_path):
        f = tmp_path / "creds.json"
        f.write_text(json.dumps(_make_creds(access_token="valid-tok")))
        with patch("doubletake.client._OAUTH_CREDS_PATH", str(f)):
            assert _access_token() == "valid-tok"

    def test_refreshes_expired_token(self, tmp_path):
        f = tmp_path / "creds.json"
        f.write_text(json.dumps(_make_creds(access_token="old", expiry_ms=0)))

        class FakeResp:
            def __enter__(self):
                return io.BytesIO(json.dumps({"access_token": "new-tok"}).encode())
            def __exit__(self, *a):
                pass

        with (
            patch("doubletake.client._OAUTH_CREDS_PATH", str(f)),
            patch("urllib.request.urlopen", return_value=FakeResp()),
        ):
            assert _access_token() == "new-tok"

    def test_missing_file_raises_autherror(self, tmp_path):
        with patch("doubletake.client._OAUTH_CREDS_PATH", str(tmp_path / "no-such.json")):
            with pytest.raises(AuthError, match="No Antigravity"):
                _access_token()

    def test_malformed_json_raises_autherror(self, tmp_path):
        f = tmp_path / "creds.json"
        f.write_text("not json")
        with patch("doubletake.client._OAUTH_CREDS_PATH", str(f)):
            with pytest.raises(AuthError, match="No Antigravity"):
                _access_token()

    def test_array_creds_raises_autherror(self, tmp_path):
        f = tmp_path / "creds.json"
        f.write_text("[1, 2, 3]")
        with patch("doubletake.client._OAUTH_CREDS_PATH", str(f)):
            with pytest.raises(AuthError, match="Malformed credentials"):
                _access_token()

    def test_missing_refresh_token_raises_autherror(self, tmp_path):
        f = tmp_path / "creds.json"
        f.write_text(json.dumps({"access_token": "old", "expiry_date": 0}))
        with patch("doubletake.client._OAUTH_CREDS_PATH", str(f)):
            with pytest.raises(AuthError, match="no refresh token"):
                _access_token()

    def test_null_refresh_token_raises_autherror(self, tmp_path):
        f = tmp_path / "creds.json"
        f.write_text(json.dumps({"access_token": "old", "expiry_date": 0, "refresh_token": None}))
        with patch("doubletake.client._OAUTH_CREDS_PATH", str(f)):
            with pytest.raises(AuthError, match="no refresh token"):
                _access_token()

    def test_string_expiry_treated_as_zero_forces_refresh(self, tmp_path):
        # int() on a non-numeric string raises ValueError → catches as expiry_ms=0 → refresh
        f = tmp_path / "creds.json"
        f.write_text(json.dumps({
            "access_token": "tok", "expiry_date": "not-a-number", "refresh_token": "r"
        }))
        err = urllib.error.HTTPError(
            url="", code=400, msg="",
            hdrs=None, fp=io.BytesIO(json.dumps({"error": "invalid_grant"}).encode()),  # type: ignore
        )
        with (
            patch("doubletake.client._OAUTH_CREDS_PATH", str(f)),
            patch("urllib.request.urlopen", side_effect=err),
        ):
            with pytest.raises(AuthError):
                _access_token()

    def test_null_expiry_treated_as_zero(self, tmp_path):
        f = tmp_path / "creds.json"
        f.write_text(json.dumps({"access_token": "tok", "expiry_date": None, "refresh_token": "r"}))
        err = urllib.error.HTTPError(
            url="", code=503, msg="", hdrs=None, fp=io.BytesIO(b"{}"),  # type: ignore
        )
        with (
            patch("doubletake.client._OAUTH_CREDS_PATH", str(f)),
            patch("urllib.request.urlopen", side_effect=err),
        ):
            with pytest.raises(AuthError):
                _access_token()

    def test_invalid_grant_gives_relogin_message(self, tmp_path):
        f = tmp_path / "creds.json"
        f.write_text(json.dumps(_make_creds(expiry_ms=0)))
        err = urllib.error.HTTPError(
            url="", code=400, msg="Bad Request",
            hdrs=None,
            fp=io.BytesIO(json.dumps({"error": "invalid_grant"}).encode()),  # type: ignore
        )
        with (
            patch("doubletake.client._OAUTH_CREDS_PATH", str(f)),
            patch("urllib.request.urlopen", side_effect=err),
        ):
            with pytest.raises(AuthError, match="gemini auth login"):
                _access_token()

    def test_other_http_error_gives_generic_message(self, tmp_path):
        f = tmp_path / "creds.json"
        f.write_text(json.dumps(_make_creds(expiry_ms=0)))
        err = urllib.error.HTTPError(
            url="", code=503, msg="Service Unavailable",
            hdrs=None, fp=io.BytesIO(b"{}"),  # type: ignore
        )
        with (
            patch("doubletake.client._OAUTH_CREDS_PATH", str(f)),
            patch("urllib.request.urlopen", side_effect=err),
        ):
            with pytest.raises(AuthError, match="HTTP 503"):
                _access_token()

    def test_network_error_raises_autherror(self, tmp_path):
        f = tmp_path / "creds.json"
        f.write_text(json.dumps(_make_creds(expiry_ms=0)))
        with (
            patch("doubletake.client._OAUTH_CREDS_PATH", str(f)),
            patch("urllib.request.urlopen", side_effect=OSError("refused")),
        ):
            with pytest.raises(AuthError, match="Failed to refresh"):
                _access_token()


# ============================================================================
# _discover_project
# ============================================================================

class TestDiscoverProject:
    class _JsonResp:
        def __init__(self, data: dict):
            self._data = data
        def __enter__(self):
            return io.BytesIO(json.dumps(self._data).encode())
        def __exit__(self, *a):
            pass

    def test_returns_project_from_response(self):
        with patch("doubletake.client._post",
                   return_value=self._JsonResp({"cloudaicompanionProject": "my-proj"})):
            assert _discover_project("tok") == "my-proj"

    def test_falls_back_to_env_on_oserror(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "env-proj")
        with patch("doubletake.client._post", side_effect=OSError("refused")):
            assert _discover_project("tok") == "env-proj"

    def test_falls_back_on_incomplete_read(self):
        with patch("doubletake.client._post",
                   side_effect=http.client.IncompleteRead(b"")):
            assert _discover_project("tok") is None

    def test_falls_back_on_json_error(self):
        class _BadResp:
            def __enter__(self): return io.BytesIO(b"not json")
            def __exit__(self, *a): pass

        with patch("doubletake.client._post", return_value=_BadResp()):
            assert _discover_project("tok") is None

    def test_returns_none_on_empty_response(self):
        with patch("doubletake.client._post", return_value=self._JsonResp({})):
            assert _discover_project("tok") is None


# ============================================================================
# _read_claude_keychain
# ============================================================================

class TestReadClaudeKeychain:
    def _mock(self, returncode: int, stdout: str) -> MagicMock:
        m = MagicMock()
        m.returncode = returncode
        m.stdout = stdout
        return m

    def _valid(self) -> str:
        oauth = {
            "accessToken": "acc",
            "refreshToken": "ref",
            "expiresAt": int((time.time() + 3600) * 1000),
        }
        return json.dumps({"claudeAiOauth": oauth})

    def test_success(self):
        with patch("subprocess.run", return_value=self._mock(0, self._valid())):
            assert _read_claude_keychain()["accessToken"] == "acc"

    def test_security_not_found_raises_autherror(self):
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            with pytest.raises(AuthError, match="macOS"):
                _read_claude_keychain()

    def test_timeout_raises_autherror(self):
        with patch("subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="security", timeout=10)):
            with pytest.raises(AuthError, match="Timed out"):
                _read_claude_keychain()

    def test_nonzero_returncode_raises_autherror(self):
        with patch("subprocess.run", return_value=self._mock(1, "")):
            with pytest.raises(AuthError, match="No Claude Code login"):
                _read_claude_keychain()

    def test_invalid_json_raises_autherror(self):
        with patch("subprocess.run", return_value=self._mock(0, "not-json")):
            with pytest.raises(AuthError, match="not valid JSON"):
                _read_claude_keychain()

    def test_missing_claudeaioauth_key_raises_autherror(self):
        with patch("subprocess.run",
                   return_value=self._mock(0, json.dumps({"other": "stuff"}))):
            with pytest.raises(AuthError, match="unexpected format"):
                _read_claude_keychain()

    def test_array_json_raises_autherror(self):
        with patch("subprocess.run", return_value=self._mock(0, "[1, 2, 3]")):
            with pytest.raises(AuthError, match="unexpected format"):
                _read_claude_keychain()

    def test_claudeaioauth_not_dict_raises_autherror(self):
        with patch("subprocess.run",
                   return_value=self._mock(0, json.dumps({"claudeAiOauth": "a string"}))):
            with pytest.raises(AuthError, match="not a dict"):
                _read_claude_keychain()


# ============================================================================
# _claude_access_token
# ============================================================================

class TestClaudeAccessToken:
    def _oauth(self, *, expired: bool = False, no_refresh: bool = False) -> dict:
        expiry = int((time.time() - 60) * 1000) if expired else int((time.time() + 3600) * 1000)
        d: dict = {"accessToken": "acc", "expiresAt": expiry}
        if not no_refresh:
            d["refreshToken"] = "ref"
        return d

    def test_returns_valid_token(self):
        with patch("doubletake.client._read_claude_keychain", return_value=self._oauth()):
            assert _claude_access_token() == "acc"

    def test_refreshes_expired_token(self):
        with (
            patch("doubletake.client._read_claude_keychain", return_value=self._oauth(expired=True)),
            patch("doubletake.client._claude_refresh_token", return_value="new-acc") as mock_ref,
        ):
            assert _claude_access_token() == "new-acc"
        mock_ref.assert_called_once_with("ref")

    def test_expired_no_refresh_token_raises(self):
        with patch("doubletake.client._read_claude_keychain",
                   return_value=self._oauth(expired=True, no_refresh=True)):
            with pytest.raises(AuthError, match="no refresh token"):
                _claude_access_token()

    def test_null_expiry_forces_refresh(self):
        oauth = {"accessToken": "acc", "expiresAt": None, "refreshToken": "ref"}
        with (
            patch("doubletake.client._read_claude_keychain", return_value=oauth),
            patch("doubletake.client._claude_refresh_token", return_value="refreshed"),
        ):
            assert _claude_access_token() == "refreshed"

    def test_string_expiry_forces_refresh(self):
        oauth = {"accessToken": "acc", "expiresAt": "not-a-number", "refreshToken": "ref"}
        with (
            patch("doubletake.client._read_claude_keychain", return_value=oauth),
            patch("doubletake.client._claude_refresh_token", return_value="refreshed"),
        ):
            assert _claude_access_token() == "refreshed"


# ============================================================================
# _claude_refresh_token
# ============================================================================

class TestClaudeRefreshToken:
    class _FakeResp:
        def __init__(self, data: dict):
            self._data = data
        def __enter__(self):
            return io.BytesIO(json.dumps(self._data).encode())
        def __exit__(self, *a):
            pass

    def test_success(self):
        with patch("urllib.request.urlopen",
                   return_value=self._FakeResp({"access_token": "new-tok"})):
            assert _claude_refresh_token("refresh-tok") == "new-tok"

    def test_invalid_grant_raises_autherror(self):
        err = urllib.error.HTTPError(
            url="", code=400, msg="",
            hdrs=None,
            fp=io.BytesIO(json.dumps({"error": "invalid_grant"}).encode()),  # type: ignore
        )
        with patch("urllib.request.urlopen", side_effect=err):
            with pytest.raises(AuthError, match="expired or been revoked"):
                _claude_refresh_token("bad-refresh")

    def test_http_error_raises_autherror(self):
        err = urllib.error.HTTPError(
            url="", code=500, msg="Server Error",
            hdrs=None, fp=io.BytesIO(b"{}"),  # type: ignore
        )
        with patch("urllib.request.urlopen", side_effect=err):
            with pytest.raises(AuthError, match="HTTP 500"):
                _claude_refresh_token("ref")

    def test_network_error_raises_autherror(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            with pytest.raises(AuthError, match="Failed to refresh"):
                _claude_refresh_token("ref")

    def test_missing_access_token_in_response_raises(self):
        with patch("urllib.request.urlopen",
                   return_value=self._FakeResp({"token_type": "Bearer"})):
            with pytest.raises(AuthError, match="Failed to refresh"):
                _claude_refresh_token("ref")


# ============================================================================
# _stream
# ============================================================================

class TestStream:
    def _passthrough(self, chunk: str):
        yield chunk

    def _http_err(self, code: int, body: bytes = b"detail") -> urllib.error.HTTPError:
        return urllib.error.HTTPError(
            url="", code=code, msg="",
            hdrs=None, fp=io.BytesIO(body),  # type: ignore
        )

    def _run(self, resp=None, side_effect=None) -> list[str]:
        with patch("doubletake.client._post", return_value=resp, side_effect=side_effect):
            return list(_stream("url", {}, {}, 30.0, self._passthrough, "test"))

    def test_success_yields_text(self):
        assert self._run(_FakeSSEResp([b"data: hello\n", b"\n"])) == ["hello"]

    def test_empty_stream_yields_nothing(self):
        assert self._run(_FakeSSEResp([])) == []

    def test_multiple_chunks(self):
        resp = _FakeSSEResp([b"data: a\n", b"\n", b"data: b\n", b"\n"])
        assert self._run(resp) == ["a", "b"]

    def test_401_raises_autherror(self):
        with pytest.raises(AuthError, match="401"):
            self._run(side_effect=self._http_err(401))

    def test_403_raises_autherror(self):
        with pytest.raises(AuthError, match="403"):
            self._run(side_effect=self._http_err(403))

    def test_404_raises_backenderr(self):
        with pytest.raises(BackendError, match="model not found"):
            self._run(side_effect=self._http_err(404))

    def test_429_raises_ratelimiterror(self):
        with pytest.raises(RateLimitError, match="rate limit"):
            self._run(side_effect=self._http_err(429))

    def test_ratelimiterror_is_backenderr(self):
        with pytest.raises(BackendError):
            self._run(side_effect=self._http_err(429))

    def test_500_raises_backenderr(self):
        with pytest.raises(BackendError, match="500"):
            self._run(side_effect=self._http_err(500))

    def test_network_error_raises_backenderr(self):
        with pytest.raises(BackendError, match="network error"):
            self._run(side_effect=OSError("refused"))

    def test_socket_timeout_mid_stream_raises_timeouterror(self):
        resp = _FakeSSEResp([b"data: partial\n", b"\n"], raise_after=socket.timeout("timed out"))
        with pytest.raises(TimeoutError, match="No output"):
            self._run(resp)

    def test_incomplete_read_mid_stream_raises_backenderr(self):
        resp = _FakeSSEResp([b"data: partial\n", b"\n"],
                            raise_after=http.client.IncompleteRead(b""))
        with pytest.raises(BackendError, match="dropped mid-stream"):
            self._run(resp)

    def test_connection_reset_mid_stream_raises_backenderr(self):
        resp = _FakeSSEResp([b"data: start\n", b"\n"],
                            raise_after=ConnectionResetError("reset by peer"))
        with pytest.raises(BackendError, match="dropped mid-stream"):
            self._run(resp)


# ============================================================================
# stream_review — backend selection
# ============================================================================

class TestStreamReview:
    @pytest.fixture(autouse=True)
    def patch_stream(self):
        def _noop(*a, **kw):
            yield "review-text"

        with patch("doubletake.client._stream", side_effect=_noop) as m:
            self._stream_mock = m
            yield m

    def _call(self, **kw):
        return list(stream_review(
            kw.pop("prompt", "p"),
            system_prompt=kw.pop("system_prompt", "sys"),
            model=kw.pop("model", None),
            idle_timeout=kw.pop("idle_timeout", 30),
        ))

    def _what(self) -> str:
        return self._stream_mock.call_args.kwargs["what"]

    def _url(self) -> str:
        return self._stream_mock.call_args.args[0]

    def _body(self) -> dict:
        return self._stream_mock.call_args.args[2]

    def _headers(self) -> dict:
        return self._stream_mock.call_args.args[1]

    # ── Antigravity (Code Assist) ─────────────────────────────────────────

    def test_uses_code_assist_when_login_exists(self, tmp_path):
        creds = tmp_path / "oauth.json"
        creds.write_text(json.dumps(_make_creds()))
        with (
            patch("doubletake.client._OAUTH_CREDS_PATH", str(creds)),
            patch("doubletake.client._access_token", return_value="tok"),
            patch("doubletake.client._discover_project", return_value=None),
        ):
            assert self._call() == ["review-text"]
        assert "Code Assist" in self._what()

    def test_code_assist_includes_project(self, tmp_path):
        creds = tmp_path / "oauth.json"
        creds.write_text(json.dumps(_make_creds()))
        with (
            patch("doubletake.client._OAUTH_CREDS_PATH", str(creds)),
            patch("doubletake.client._access_token", return_value="tok"),
            patch("doubletake.client._discover_project", return_value="my-proj"),
        ):
            self._call()
        assert self._body().get("project") == "my-proj"

    def test_code_assist_omits_project_when_none(self, tmp_path):
        creds = tmp_path / "oauth.json"
        creds.write_text(json.dumps(_make_creds()))
        with (
            patch("doubletake.client._OAUTH_CREDS_PATH", str(creds)),
            patch("doubletake.client._access_token", return_value="tok"),
            patch("doubletake.client._discover_project", return_value=None),
        ):
            self._call()
        assert "project" not in self._body()

    def test_code_assist_uses_default_model(self, tmp_path):
        creds = tmp_path / "oauth.json"
        creds.write_text(json.dumps(_make_creds()))
        with (
            patch("doubletake.client._OAUTH_CREDS_PATH", str(creds)),
            patch("doubletake.client._access_token", return_value="tok"),
            patch("doubletake.client._discover_project", return_value=None),
        ):
            self._call()
        assert self._body()["model"] == _DEFAULT_CODE_ASSIST_MODEL

    def test_code_assist_respects_model_override(self, tmp_path):
        creds = tmp_path / "oauth.json"
        creds.write_text(json.dumps(_make_creds()))
        with (
            patch("doubletake.client._OAUTH_CREDS_PATH", str(creds)),
            patch("doubletake.client._access_token", return_value="tok"),
            patch("doubletake.client._discover_project", return_value=None),
        ):
            self._call(model="gemini-2.5-pro")
        assert self._body()["model"] == "gemini-2.5-pro"

    def test_code_assist_bearer_auth(self, tmp_path):
        creds = tmp_path / "oauth.json"
        creds.write_text(json.dumps(_make_creds()))
        with (
            patch("doubletake.client._OAUTH_CREDS_PATH", str(creds)),
            patch("doubletake.client._access_token", return_value="my-tok"),
            patch("doubletake.client._discover_project", return_value=None),
        ):
            self._call()
        assert self._headers()["Authorization"] == "Bearer my-tok"

    # ── Gemini API ────────────────────────────────────────────────────────

    def test_uses_gemini_api_when_no_login(self, tmp_path):
        with (
            patch("doubletake.client._OAUTH_CREDS_PATH", str(tmp_path / "no-such.json")),
            patch.dict("os.environ", {"GEMINI_API_KEY": "key123"}),
        ):
            self._call()
        assert self._what() == "Gemini API"

    def test_gemini_api_uses_default_model_in_url(self, tmp_path):
        with (
            patch("doubletake.client._OAUTH_CREDS_PATH", str(tmp_path / "no-such.json")),
            patch.dict("os.environ", {"GEMINI_API_KEY": "key123"}),
        ):
            self._call()
        assert _DEFAULT_GEMINI_API_MODEL in self._url()

    def test_gemini_api_key_in_header_not_url(self, tmp_path):
        with (
            patch("doubletake.client._OAUTH_CREDS_PATH", str(tmp_path / "no-such.json")),
            patch.dict("os.environ", {"GEMINI_API_KEY": "secret-key"}),
        ):
            self._call()
        assert "secret-key" not in self._url()
        assert self._headers().get("x-goog-api-key") == "secret-key"

    def test_forced_gemini_api_overrides_login(self, tmp_path):
        creds = tmp_path / "oauth.json"
        creds.write_text(json.dumps(_make_creds()))
        with (
            patch("doubletake.client._OAUTH_CREDS_PATH", str(creds)),
            patch.dict("os.environ", {
                "DOUBLETAKE_BACKEND": "gemini_api",
                "GEMINI_API_KEY": "forced-key",
            }),
        ):
            self._call()
        assert self._what() == "Gemini API"

    def test_forced_gemini_api_without_key_raises(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.setenv("DOUBLETAKE_BACKEND", "gemini_api")
        with patch("doubletake.client._OAUTH_CREDS_PATH", str(tmp_path / "no-such.json")):
            with pytest.raises(AuthError, match="GEMINI_API_KEY is unset"):
                self._call()

    # ── Claude backend ────────────────────────────────────────────────────

    def test_forced_claude_backend(self):
        with (
            patch.dict("os.environ", {"DOUBLETAKE_BACKEND": "claude"}),
            patch("doubletake.client._claude_access_token", return_value="claude-tok"),
        ):
            self._call()
        assert self._what() == "Claude Code"

    def test_claude_uses_default_model(self):
        with (
            patch.dict("os.environ", {"DOUBLETAKE_BACKEND": "claude"}),
            patch("doubletake.client._claude_access_token", return_value="tok"),
        ):
            self._call()
        assert self._body()["model"] == _DEFAULT_CLAUDE_MODEL

    def test_claude_respects_model_override(self):
        with (
            patch.dict("os.environ", {"DOUBLETAKE_BACKEND": "claude"}),
            patch("doubletake.client._claude_access_token", return_value="tok"),
        ):
            self._call(model="claude-opus-4-8")
        assert self._body()["model"] == "claude-opus-4-8"

    def test_claude_uses_bearer_auth(self):
        with (
            patch.dict("os.environ", {"DOUBLETAKE_BACKEND": "claude"}),
            patch("doubletake.client._claude_access_token", return_value="my-tok"),
        ):
            self._call()
        assert self._headers()["Authorization"] == "Bearer my-tok"

    def test_claude_body_structure(self):
        with (
            patch.dict("os.environ", {"DOUBLETAKE_BACKEND": "claude"}),
            patch("doubletake.client._claude_access_token", return_value="tok"),
        ):
            self._call(prompt="user-msg", system_prompt="sys-msg")
        body = self._body()
        assert body["system"] == "sys-msg"
        assert body["messages"] == [{"role": "user", "content": "user-msg"}]
        assert body["stream"] is True
        assert body["max_tokens"] == 8192

    def test_claude_sets_anthropic_version_header(self):
        with (
            patch.dict("os.environ", {"DOUBLETAKE_BACKEND": "claude"}),
            patch("doubletake.client._claude_access_token", return_value="tok"),
        ):
            self._call()
        assert "anthropic-version" in self._headers()

    # ── No credentials ────────────────────────────────────────────────────

    def test_no_login_no_key_raises_autherror(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("DOUBLETAKE_BACKEND", raising=False)
        with patch("doubletake.client._OAUTH_CREDS_PATH", str(tmp_path / "no-such.json")):
            with pytest.raises(AuthError, match="No Antigravity login"):
                self._call()

    # ── Unrecognized DOUBLETAKE_BACKEND ──────────────────────────────────

    def test_unrecognized_backend_warns(self, tmp_path, capsys):
        creds = tmp_path / "oauth.json"
        creds.write_text(json.dumps(_make_creds()))
        with (
            patch("doubletake.client._OAUTH_CREDS_PATH", str(creds)),
            patch("doubletake.client._access_token", return_value="tok"),
            patch("doubletake.client._discover_project", return_value=None),
            patch.dict("os.environ", {"DOUBLETAKE_BACKEND": "typo-value"}),
        ):
            self._call()
        err = capsys.readouterr().err
        assert "Unrecognized" in err
        assert "typo-value" in err

    def test_unrecognized_backend_falls_back_to_antigravity(self, tmp_path):
        creds = tmp_path / "oauth.json"
        creds.write_text(json.dumps(_make_creds()))
        with (
            patch("doubletake.client._OAUTH_CREDS_PATH", str(creds)),
            patch("doubletake.client._access_token", return_value="tok"),
            patch("doubletake.client._discover_project", return_value=None),
            patch.dict("os.environ", {"DOUBLETAKE_BACKEND": "typo-value"}),
        ):
            self._call()
        assert "Code Assist" in self._what()

    # ── Fallback on 429 ───────────────────────────────────────────────────

    def test_claude_falls_back_to_sonnet_on_429(self, capsys):
        tried_models: list[str] = []

        def _rate_limit_then_ok(*a, **kw):
            body = a[2]
            tried_models.append(body["model"])
            if len(tried_models) == 1:
                raise RateLimitError("opus 429")
            yield "review"

        with (
            patch.dict("os.environ", {"DOUBLETAKE_BACKEND": "claude"}),
            patch("doubletake.client._claude_access_token", return_value="tok"),
            patch("doubletake.client._stream", side_effect=_rate_limit_then_ok),
        ):
            result = self._call()

        assert result == ["review"]
        assert tried_models == ["claude-opus-4-8", "claude-sonnet-4-6"]
        err = capsys.readouterr().err
        assert "claude-opus-4-8" in err
        assert "falling back to claude-sonnet-4-6" in err

    def test_claude_raises_when_all_models_rate_limited(self):
        with (
            patch.dict("os.environ", {"DOUBLETAKE_BACKEND": "claude"}),
            patch("doubletake.client._claude_access_token", return_value="tok"),
            patch("doubletake.client._stream",
                  side_effect=RateLimitError("all rate limited")),
        ):
            with pytest.raises(RateLimitError):
                self._call()

    def test_claude_pinned_model_no_fallback(self):
        """DOUBLETAKE_MODEL pins to one model — no fallback attempted."""
        with (
            patch.dict("os.environ", {"DOUBLETAKE_BACKEND": "claude"}),
            patch("doubletake.client._claude_access_token", return_value="tok"),
            patch("doubletake.client._stream",
                  side_effect=RateLimitError("pinned 429")),
        ):
            with pytest.raises(RateLimitError):
                self._call(model="claude-opus-4-8")

    def test_code_assist_falls_back_on_429(self, tmp_path, capsys):
        creds = tmp_path / "oauth.json"
        creds.write_text(json.dumps(_make_creds()))
        tried_models: list[str] = []

        def _rate_limit_then_ok(*a, **kw):
            body = a[2]
            tried_models.append(body["model"])
            if len(tried_models) == 1:
                raise RateLimitError("3.1 capacity")
            yield "review"

        with (
            patch("doubletake.client._OAUTH_CREDS_PATH", str(creds)),
            patch("doubletake.client._access_token", return_value="tok"),
            patch("doubletake.client._discover_project", return_value=None),
            patch("doubletake.client._stream", side_effect=_rate_limit_then_ok),
        ):
            result = self._call()

        assert result == ["review"]
        assert tried_models == ["gemini-3.1-pro-preview", "gemini-3-pro-preview"]
        err = capsys.readouterr().err
        assert "gemini-3.1-pro-preview" in err
        assert "falling back to gemini-3-pro-preview" in err

    def test_code_assist_fallback_chain_exhausted_raises(self, tmp_path):
        creds = tmp_path / "oauth.json"
        creds.write_text(json.dumps(_make_creds()))
        with (
            patch("doubletake.client._OAUTH_CREDS_PATH", str(creds)),
            patch("doubletake.client._access_token", return_value="tok"),
            patch("doubletake.client._discover_project", return_value=None),
            patch("doubletake.client._stream",
                  side_effect=RateLimitError("all capacity gone")),
        ):
            with pytest.raises(RateLimitError):
                self._call()

    def test_gemini_api_falls_back_on_429(self, tmp_path, capsys):
        tried_models: list[str] = []

        def _rate_limit_then_ok(*a, **kw):
            # extract model from URL: .../models/<model>:streamGenerate...
            url = a[0]
            m = url.split("/models/")[1].split(":")[0]
            tried_models.append(m)
            if len(tried_models) == 1:
                raise RateLimitError("gemini-2.5-pro 429")
            yield "review"

        with (
            patch("doubletake.client._OAUTH_CREDS_PATH", str(tmp_path / "no.json")),
            patch.dict("os.environ", {"GEMINI_API_KEY": "key"}),
            patch("doubletake.client._stream", side_effect=_rate_limit_then_ok),
        ):
            result = self._call()

        assert result == ["review"]
        assert tried_models == _GEMINI_API_FALLBACK_MODELS
        err = capsys.readouterr().err
        assert "gemini-2.5-pro" in err
        assert "falling back to gemini-3-flash-preview" in err

    def test_code_assist_fallback_covers_full_chain(self, tmp_path, capsys):
        """All three models in the chain get tried before raising."""
        creds = tmp_path / "oauth.json"
        creds.write_text(json.dumps(_make_creds()))
        tried_models: list[str] = []

        def _capture_and_fail(*a, **kw):
            body = a[2]
            tried_models.append(body["model"])
            raise RateLimitError("capacity")

        with (
            patch("doubletake.client._OAUTH_CREDS_PATH", str(creds)),
            patch("doubletake.client._access_token", return_value="tok"),
            patch("doubletake.client._discover_project", return_value=None),
            patch("doubletake.client._stream", side_effect=_capture_and_fail),
        ):
            with pytest.raises(RateLimitError):
                self._call()

        assert tried_models == _CODE_ASSIST_FALLBACK_MODELS
