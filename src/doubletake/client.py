"""Streaming review clients for doubletake.

Two backends, both pure-stdlib (no third-party deps, no SDK, no local binary):

* **Code Assist (default):** reuses the user's existing Antigravity / Gemini-CLI
  OAuth session at ``~/.gemini/oauth_creds.json`` to call
  ``cloudcode-pa.googleapis.com`` — i.e. the user's *subscription*. No API key.
* **Gemini Developer API (fallback):** used only when there is no OAuth session
  but ``GEMINI_API_KEY`` is set.

Both perform a single, non-agentic ``streamGenerateContent`` call: there are no
tools, no file access and no shell, so the reviewer can only read the piped-in
artifact and emit text — read-only by construction.
"""

from __future__ import annotations  # PEP 604 unions on Python 3.9

import http.client
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Iterator

# Public OAuth client of the Gemini CLI / Code Assist app. These are not secrets
# (they ship in the open-source gemini-cli); they only identify the OAuth app for
# the refresh-token exchange. The actual authorization lives in the user's
# locally-stored refresh token.
_OAUTH_CLIENT_ID = (
    "681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com"
)
_OAUTH_CLIENT_SECRET = "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"
_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"

_CODE_ASSIST_BASE = "https://cloudcode-pa.googleapis.com/v1internal"
_GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

# Latest Pro model served by Code Assist. NOTE: the wire id is
# "gemini-3.1-pro-preview" — bare "gemini-3.1-pro" and "gemini-3-pro" both 404.
# Standard-tier rate-limits this model tightly; override to "gemini-3-pro-preview"
# or "gemini-2.5-pro" if you hit frequent 429s.
_DEFAULT_CODE_ASSIST_MODEL = "gemini-3.1-pro-preview"
_DEFAULT_GEMINI_API_MODEL = "gemini-2.5-pro"

_OAUTH_CREDS_PATH = os.path.expanduser(
    os.environ.get("DOUBLETAKE_OAUTH_CREDS", "~/.gemini/oauth_creds.json")
)


class AuthError(RuntimeError):
    """No usable credential, or the stored OAuth session could not be refreshed."""


class BackendError(RuntimeError):
    """The model backend returned an error (HTTP status, quota, bad model, …)."""


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def _post(url: str, headers: dict, body: dict, timeout: float):
    """POSTs ``body`` as JSON and returns the open response (caller closes it)."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=timeout)


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", "replace")[:400]
    except Exception:  # noqa: BLE001
        return ""


# --------------------------------------------------------------------------- #
# OAuth (Code Assist)
# --------------------------------------------------------------------------- #
def _access_token() -> str:
    """Returns a valid access token, refreshing the stored one if it has expired.

    The refresh happens in-memory only; the shared ``oauth_creds.json`` is never
    rewritten, so we do not race with the Antigravity app or gemini-cli.

    Raises:
        AuthError: If the creds file is missing/unreadable or the refresh fails.
    """
    try:
        with open(_OAUTH_CREDS_PATH, encoding="utf-8") as fh:
            creds = json.load(fh)
    except (OSError, ValueError) as exc:
        raise AuthError(
            "No Antigravity/Gemini login found at "
            f"{_OAUTH_CREDS_PATH}. Sign in via the Antigravity app (or the "
            "`gemini` CLI), or set GEMINI_API_KEY to use the Gemini API."
        ) from exc

    token = creds.get("access_token")
    # gemini-cli stores ms since epoch; tolerate a missing/null value.
    expiry_ms = creds.get("expiry_date") or 0
    # Refresh if missing or within 60s of expiry.
    if token and time.time() * 1000 < expiry_ms - 60_000:
        return token

    refresh_token = creds.get("refresh_token")
    if not refresh_token:
        raise AuthError(
            "Stored Antigravity login has no refresh token; sign in again."
        )
    form = urllib.parse.urlencode({
        "client_id": _OAUTH_CLIENT_ID,
        "client_secret": _OAUTH_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode("utf-8")
    req = urllib.request.Request(
        _OAUTH_TOKEN_URL, data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)["access_token"]
    except (OSError, KeyError, ValueError) as exc:
        # OSError covers HTTPError/URLError/socket.timeout; KeyError/ValueError
        # cover a missing token field or unparseable response body.
        raise AuthError(
            "Failed to refresh the Antigravity login. Sign in again via the "
            f"Antigravity app. ({exc})"
        ) from exc


def _discover_project(token: str) -> str | None:
    """Best-effort lookup of the Code Assist project for the signed-in user."""
    env_project = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv(
        "GOOGLE_CLOUD_PROJECT_ID"
    )
    body = {
        "cloudaicompanionProject": env_project,
        "metadata": {
            "ideType": "IDE_UNSPECIFIED",
            "platform": "PLATFORM_UNSPECIFIED",
            "pluginType": "GEMINI",
            "duetProject": env_project,
        },
    }
    try:
        with _post(
            f"{_CODE_ASSIST_BASE}:loadCodeAssist",
            {"Authorization": f"Bearer {token}"}, body, timeout=30,
        ) as resp:
            data = json.load(resp)
        return data.get("cloudaicompanionProject") or env_project
    except (OSError, ValueError):
        # Non-fatal (covers HTTP/URL/socket-timeout/JSON errors): fall back to
        # env (or None / a managed project).
        return env_project


# --------------------------------------------------------------------------- #
# SSE parsing
# --------------------------------------------------------------------------- #
def _emit_text(chunk: str, unwrap: bool) -> Iterator[str]:
    """Parses one JSON SSE payload and yields its non-thought text parts."""
    try:
        obj = json.loads(chunk)
    except ValueError:
        return
    # Code Assist wraps the payload in "response" (which may be null on
    # metadata-only chunks); the Gemini API does not wrap.
    root = (obj.get("response") or {}) if unwrap else obj
    for cand in (root or {}).get("candidates") or []:
        for part in cand.get("content", {}).get("parts", []):
            # Skip "thought" parts — we stream only the final review text.
            if part.get("thought"):
                continue
            text = part.get("text")
            if text:
                yield text


def _iter_sse_text(resp, unwrap: bool) -> Iterator[str]:
    """Yields non-thought text deltas from an ``alt=sse`` Gemini stream.

    Implements the SSE framing properly: consecutive ``data:`` fields are
    accumulated (joined with "\\n") and the event is dispatched on the blank-line
    boundary, so multi-line JSON payloads are not dropped.

    Args:
        resp: An open streaming HTTP response.
        unwrap: True for Code Assist (text under a top-level ``response``
            wrapper); False for the Gemini Developer API.
    """
    data_lines: list[str] = []
    for raw in resp:  # each socket read is bounded by the response's timeout
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if line == "":  # event boundary — dispatch accumulated data
            if data_lines:
                yield from _emit_text("\n".join(data_lines), unwrap)
                data_lines = []
            continue
        if line.startswith(":"):  # SSE comment
            continue
        if line.startswith("data:"):
            value = line[len("data:"):]
            if value.startswith(" "):  # strip one optional leading space
                value = value[1:]
            data_lines.append(value)
        # other SSE fields (event:, id:, retry:) are ignored
    if data_lines:  # flush a trailing event with no terminating blank line
        yield from _emit_text("\n".join(data_lines), unwrap)


def _stream(url: str, headers: dict, body: dict, idle_timeout: float,
            unwrap: bool, what: str) -> Iterator[str]:
    """Opens an SSE stream and yields text, mapping transport errors."""
    try:
        resp = _post(url, headers, body, timeout=idle_timeout)
    except urllib.error.HTTPError as exc:
        detail = _http_error_detail(exc)
        if exc.code in (401, 403):
            raise AuthError(
                f"{what} rejected the request ({exc.code}). Your subscription "
                f"session may have expired — sign in to Antigravity again. {detail}"
            ) from exc
        if exc.code == 404:
            raise BackendError(
                f"{what}: model not found (404). Try a different "
                f"DOUBLETAKE_MODEL. {detail}"
            ) from exc
        if exc.code == 429:
            raise BackendError(f"{what}: rate limit / quota exceeded (429). {detail}")
        raise BackendError(f"{what} error {exc.code}: {detail}") from exc
    except OSError as exc:
        # URLError, socket.timeout and other connect-time errors.
        raise BackendError(f"{what}: network error: {exc}") from exc

    try:
        with resp:
            yield from _iter_sse_text(resp, unwrap=unwrap)
    except socket.timeout as exc:
        raise TimeoutError(
            f"No output for {idle_timeout:.0f}s from {what}; aborting."
        ) from exc
    except (OSError, http.client.IncompleteRead) as exc:
        # Connection reset / dropped mid-stream (socket.timeout, handled above,
        # is also an OSError subclass — order matters).
        raise BackendError(
            f"{what}: connection dropped mid-stream: {exc}"
        ) from exc


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def stream_review(prompt: str, *, system_prompt: str, model: str | None,
                  idle_timeout: float) -> Iterator[str]:
    """Streams the reviewer's text, picking the best available backend.

    Preference order: the Antigravity *subscription* (Code Assist OAuth) when a
    login exists, otherwise the Gemini Developer API when ``GEMINI_API_KEY`` is
    set. Setting ``DOUBLETAKE_BACKEND=gemini_api`` forces the API-key path.

    Raises:
        AuthError: If no usable credential is available.
        BackendError: If the backend returns an error.
        TimeoutError: If no token arrives within ``idle_timeout`` seconds.
    """
    forced = os.getenv("DOUBLETAKE_BACKEND")
    have_login = os.path.exists(_OAUTH_CREDS_PATH)
    api_key = os.getenv("GEMINI_API_KEY")

    use_api_key = forced == "gemini_api" or (not have_login and api_key)

    if use_api_key:
        if not api_key:
            raise AuthError("DOUBLETAKE_BACKEND=gemini_api but GEMINI_API_KEY is unset.")
        model = model or _DEFAULT_GEMINI_API_MODEL
        url = (
            f"{_GEMINI_API_BASE}/models/{model}:streamGenerateContent"
            f"?alt=sse&key={urllib.parse.quote(api_key)}"
        )
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "systemInstruction": {"parts": [{"text": system_prompt}]},
        }
        yield from _stream(url, {}, body, idle_timeout, unwrap=False,
                           what="Gemini API")
        return

    # Default: Antigravity subscription via Code Assist OAuth.
    if not have_login:
        raise AuthError(
            f"No Antigravity login at {_OAUTH_CREDS_PATH} and no GEMINI_API_KEY. "
            "Sign in via the Antigravity app, or set GEMINI_API_KEY."
        )
    token = _access_token()
    project = _discover_project(token)
    model = model or _DEFAULT_CODE_ASSIST_MODEL
    body = {
        "model": model,
        "user_prompt_id": str(uuid.uuid4()),
        "request": {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "systemInstruction": {"parts": [{"text": system_prompt}]},
        },
    }
    if project:
        body["project"] = project
    yield from _stream(
        f"{_CODE_ASSIST_BASE}:streamGenerateContent?alt=sse",
        {"Authorization": f"Bearer {token}"}, body, idle_timeout,
        unwrap=True, what="Antigravity (Code Assist)",
    )
