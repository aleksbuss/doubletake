"""Streaming review clients for doubletake.

Three backends, all pure-stdlib (no third-party deps, no SDK, no local binary):

* **Code Assist (default):** reuses the user's existing Antigravity / Gemini-CLI
  OAuth session at ``~/.gemini/oauth_creds.json`` to call
  ``cloudcode-pa.googleapis.com`` — i.e. the user's *subscription*. No API key.
* **Gemini Developer API (fallback):** used only when there is no OAuth session
  but ``GEMINI_API_KEY`` is set.
* **Claude Code (``DOUBLETAKE_BACKEND=claude``):** reuses the user's Claude Code
  OAuth session from the macOS keychain (``Claude Code-credentials``) to call
  ``api.anthropic.com`` — i.e. the user's Claude subscription. No API key.

All backends perform a single, non-agentic ``streamGenerateContent`` /
``/v1/messages`` call: no tools, no file access, no shell — read-only by
construction.
"""

from __future__ import annotations  # PEP 604 unions on Python 3.9

import http.client
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Callable, Iterator

# ---------------------------------------------------------------------------
# Gemini / Code Assist credentials
# ---------------------------------------------------------------------------

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
_DEFAULT_CODE_ASSIST_MODEL = "gemini-3.1-pro-preview"
_DEFAULT_GEMINI_API_MODEL = "gemini-2.5-pro"

# Automatic fallback chains on 429. Tried in order; first success wins.
# DOUBLETAKE_MODEL overrides the entire chain (single model, no fallback).
_CODE_ASSIST_FALLBACK_MODELS = ["gemini-3.1-pro-preview", "gemini-3-pro-preview", "gemini-2.5-pro"]
_CLAUDE_FALLBACK_MODELS = ["claude-opus-4-8", "claude-sonnet-4-6"]

_OAUTH_CREDS_PATH = os.path.expanduser(
    os.environ.get("DOUBLETAKE_OAUTH_CREDS", "~/.gemini/oauth_creds.json")
)

# ---------------------------------------------------------------------------
# Claude Code credentials (macOS keychain)
# ---------------------------------------------------------------------------

# Claude Code stores OAuth tokens in the macOS keychain under this service name.
# Credentials format: {"claudeAiOauth": {"accessToken": "...", "refreshToken":
# "...", "expiresAt": <ms epoch>, "scopes": [...], "subscriptionType": "...",
# "rateLimitTier": "..."}}
_CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"

# OAuth client for Claude Code — public (listed at
# https://claude.ai/oauth/claude-code-client-metadata). No client secret:
# token_endpoint_auth_method is "none" (public client).
_CLAUDE_OAUTH_CLIENT_ID = "https://claude.ai/oauth/claude-code-client-metadata"
_CLAUDE_OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
_CLAUDE_API_BASE = "https://api.anthropic.com/v1"
_CLAUDE_ANTHROPIC_VERSION = "2023-06-01"

_DEFAULT_CLAUDE_MODEL = "claude-opus-4-8"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class AuthError(RuntimeError):
    """No usable credential, or the stored OAuth session could not be refreshed."""


class BackendError(RuntimeError):
    """The model backend returned an error (HTTP status, quota, bad model, …)."""


class RateLimitError(BackendError):
    """429 from the backend — quota or capacity exhausted for this model."""


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Gemini / Code Assist auth
# ---------------------------------------------------------------------------

def _access_token() -> str:
    """Returns a valid Gemini access token, refreshing if expired.

    Refresh happens in-memory only; ``oauth_creds.json`` is never rewritten.

    Raises:
        AuthError: creds file missing/unreadable, malformed, or refresh failed.
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

    if not isinstance(creds, dict):
        raise AuthError(
            f"Malformed credentials file at {_OAUTH_CREDS_PATH} "
            "(expected a JSON object). Delete it and run `gemini auth login`."
        )

    token = creds.get("access_token")
    try:
        expiry_ms = int(creds.get("expiry_date") or 0)
    except (TypeError, ValueError):
        expiry_ms = 0

    if token and time.time() * 1000 < expiry_ms - 60_000:
        return token

    refresh_token = creds.get("refresh_token")
    if not refresh_token:
        raise AuthError(
            "Stored Antigravity login has no refresh token. "
            "Run `gemini auth login` to re-authenticate."
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
    except urllib.error.HTTPError as exc:
        try:
            err_body = json.loads(exc.read().decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            err_body = {}
        if isinstance(err_body, dict) and err_body.get("error") == "invalid_grant":
            raise AuthError(
                "Your Antigravity login has expired or been revoked.\n"
                "Re-authenticate by running:\n\n"
                "    gemini auth login\n\n"
                "or by signing in through the Antigravity app, "
                "then run doubletake again."
            ) from exc
        raise AuthError(
            f"Failed to refresh the Antigravity login (HTTP {exc.code}). "
            "Run `gemini auth login` to re-authenticate."
        ) from exc
    except (OSError, KeyError, ValueError) as exc:
        raise AuthError(
            "Failed to refresh the Antigravity login. "
            f"Run `gemini auth login` to re-authenticate. ({exc})"
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
    except (OSError, ValueError, http.client.IncompleteRead):
        return env_project


# ---------------------------------------------------------------------------
# Claude Code auth (macOS keychain)
# ---------------------------------------------------------------------------

def _read_claude_keychain() -> dict:
    """Reads Claude Code credentials from the macOS keychain.

    Returns the parsed ``claudeAiOauth`` dict.

    Raises:
        AuthError: keychain entry absent, not on macOS, or malformed JSON.
    """
    try:
        result = subprocess.run(
            ["security", "find-generic-password",
             "-s", _CLAUDE_KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        raise AuthError(
            "Claude backend requires macOS (`security` command not found). "
            "Set GEMINI_API_KEY or sign in to Antigravity instead."
        )
    except subprocess.TimeoutExpired:
        raise AuthError("Timed out reading Claude Code credentials from keychain.")

    if result.returncode != 0:
        raise AuthError(
            "No Claude Code login found in the macOS keychain. "
            "Sign in via the Claude desktop app or run `claude` once to authenticate."
        )

    try:
        outer = json.loads(result.stdout.strip())
    except (ValueError, AttributeError) as exc:
        raise AuthError(
            f"Claude Code keychain entry is not valid JSON: {exc}"
        ) from exc

    if not isinstance(outer, dict) or "claudeAiOauth" not in outer:
        raise AuthError(
            "Claude Code keychain entry has unexpected format "
            f"(keys: {list(outer.keys()) if isinstance(outer, dict) else type(outer).__name__}). "
            "Try re-authenticating via the Claude desktop app."
        )

    oauth = outer["claudeAiOauth"]
    if not isinstance(oauth, dict):
        raise AuthError("Claude Code keychain 'claudeAiOauth' is not a dict.")
    return oauth


def _claude_refresh_token(refresh_token: str) -> str:
    """Exchanges a Claude Code refresh token for a new access token (in-memory)."""
    form = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": _CLAUDE_OAUTH_CLIENT_ID,
    }).encode("utf-8")
    req = urllib.request.Request(
        _CLAUDE_OAUTH_TOKEN_URL, data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
            token = data.get("access_token")
            if not isinstance(token, str) or not token:
                raise ValueError(f"No access_token in response: {list(data.keys())}")
            return token
    except urllib.error.HTTPError as exc:
        try:
            err_body = json.loads(exc.read().decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            err_body = {}
        if isinstance(err_body, dict) and err_body.get("error") == "invalid_grant":
            raise AuthError(
                "Your Claude Code login has expired or been revoked.\n"
                "Re-authenticate via the Claude desktop app or run `claude` once,\n"
                "then run doubletake again."
            ) from exc
        raise AuthError(
            f"Failed to refresh Claude Code login (HTTP {exc.code}). "
            "Try re-authenticating via the Claude desktop app."
        ) from exc
    except (OSError, KeyError, ValueError) as exc:
        raise AuthError(
            f"Failed to refresh Claude Code login: {exc}. "
            "Try re-authenticating via the Claude desktop app."
        ) from exc


def _claude_access_token() -> str:
    """Returns a valid Claude Code access token, refreshing in-memory if needed.

    Reads from the macOS keychain. Never writes back to the keychain (avoids
    racing with the Claude desktop app / CLI).

    Raises:
        AuthError: keychain absent, not on macOS, or refresh failed.
    """
    oauth = _read_claude_keychain()

    token = oauth.get("accessToken")
    try:
        expiry_ms = int(oauth.get("expiresAt") or 0)
    except (TypeError, ValueError):
        expiry_ms = 0

    if token and time.time() * 1000 < expiry_ms - 60_000:
        return token

    refresh_token = oauth.get("refreshToken")
    if not refresh_token:
        raise AuthError(
            "Stored Claude Code login has no refresh token. "
            "Re-authenticate via the Claude desktop app."
        )
    return _claude_refresh_token(refresh_token)


# ---------------------------------------------------------------------------
# SSE parsing — shared infrastructure
# ---------------------------------------------------------------------------

def _emit_gemini_text(chunk: str, unwrap: bool) -> Iterator[str]:
    """Parses one Gemini/Code-Assist SSE payload and yields non-thought text."""
    try:
        obj = json.loads(chunk)
    except ValueError:
        return
    if not isinstance(obj, dict):
        return
    root = (obj.get("response") or {}) if unwrap else obj
    for cand in (root or {}).get("candidates") or []:
        if not isinstance(cand, dict):
            continue
        # content may be null when generation is blocked (safety filters).
        for part in (cand.get("content") or {}).get("parts") or []:
            if not isinstance(part, dict):
                continue
            if part.get("thought"):
                continue
            text = part.get("text")
            if isinstance(text, str) and text:
                yield text


def _emit_claude_text(chunk: str) -> Iterator[str]:
    """Parses one Claude API SSE payload and yields text delta content."""
    try:
        obj = json.loads(chunk)
    except ValueError:
        return
    if not isinstance(obj, dict):
        return
    # Only content_block_delta events carry text; all others are metadata.
    if obj.get("type") != "content_block_delta":
        return
    delta = obj.get("delta") or {}
    if not isinstance(delta, dict):
        return
    if delta.get("type") == "text_delta":
        text = delta.get("text")
        if isinstance(text, str) and text:
            yield text


def _iter_sse_text(
    resp,
    emit_fn: Callable[[str], Iterator[str]],
) -> Iterator[str]:
    """Yields text deltas from a generic ``alt=sse`` stream.

    Implements proper SSE framing: consecutive ``data:`` fields are accumulated
    (joined with "\\n") and the event is dispatched on the blank-line boundary,
    so multi-line JSON payloads are not dropped.

    Args:
        resp: An open streaming HTTP response (iterable of raw bytes per line).
        emit_fn: Callable that takes a raw data-field string and yields text.
    """
    data_lines: list[str] = []
    for raw in resp:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if line == "":  # event boundary — dispatch accumulated data
            if data_lines:
                yield from emit_fn("\n".join(data_lines))
                data_lines = []
            continue
        if line.startswith(":"):  # SSE comment
            continue
        if line.startswith("data:"):
            value = line[len("data:"):]
            if value.startswith(" "):
                value = value[1:]
            data_lines.append(value)
        # other SSE fields (event:, id:, retry:) are intentionally ignored
    if data_lines:  # flush trailing event with no terminating blank line
        yield from emit_fn("\n".join(data_lines))


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def _stream(
    url: str,
    headers: dict,
    body: dict,
    idle_timeout: float,
    emit_fn: Callable[[str], Iterator[str]],
    what: str,
) -> Iterator[str]:
    """Opens an SSE stream and yields text, mapping transport errors to typed exc."""
    try:
        resp = _post(url, headers, body, timeout=idle_timeout)
    except urllib.error.HTTPError as exc:
        detail = _http_error_detail(exc)
        if exc.code in (401, 403):
            raise AuthError(
                f"{what} rejected the request ({exc.code}). "
                f"Your session may have expired — re-authenticate. {detail}"
            ) from exc
        if exc.code == 404:
            raise BackendError(
                f"{what}: model not found (404). Try a different "
                f"DOUBLETAKE_MODEL. {detail}"
            ) from exc
        if exc.code == 429:
            raise RateLimitError(
                f"{what}: rate limit / quota exceeded (429). {detail}"
            ) from exc
        raise BackendError(f"{what} error {exc.code}: {detail}") from exc
    except OSError as exc:
        raise BackendError(f"{what}: network error: {exc}") from exc

    try:
        with resp:
            yield from _iter_sse_text(resp, emit_fn)
    except socket.timeout as exc:
        raise TimeoutError(
            f"No output for {idle_timeout:.0f}s from {what}; aborting."
        ) from exc
    except (OSError, http.client.IncompleteRead) as exc:
        # socket.timeout (handled above) is an OSError subclass — order matters.
        raise BackendError(
            f"{what}: connection dropped mid-stream: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def stream_review(
    prompt: str,
    *,
    system_prompt: str,
    model: str | None,
    idle_timeout: float,
) -> Iterator[str]:
    """Streams the reviewer's text, picking the best available backend.

    Preference order (when ``DOUBLETAKE_BACKEND`` is not set):
      1. Antigravity subscription (Code Assist OAuth) if login exists.
      2. Gemini Developer API if ``GEMINI_API_KEY`` is set.

    ``DOUBLETAKE_BACKEND`` overrides:
      ``gemini_api`` — force Gemini Developer API (``GEMINI_API_KEY`` required).
      ``claude``     — use Claude Code OAuth from macOS keychain (no API key).

    Raises:
        AuthError: No usable credential.
        BackendError: Backend returned an error.
        TimeoutError: No token within ``idle_timeout`` seconds.
    """
    forced = os.getenv("DOUBLETAKE_BACKEND")

    if forced and forced not in ("gemini_api", "claude"):
        sys.stderr.write(
            f"[doubletake] ⚠️ Unrecognized DOUBLETAKE_BACKEND={forced!r}. "
            "Supported values: 'gemini_api', 'claude'. "
            "Ignoring and using Antigravity.\n"
        )
        forced = None

    # ── Claude Code backend ───────────────────────────────────────────────
    if forced == "claude":
        token = _claude_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "anthropic-version": _CLAUDE_ANTHROPIC_VERSION,
        }
        # If the user pinned a model, use only that. Otherwise try the chain.
        models = [model] if model else _CLAUDE_FALLBACK_MODELS
        if not models:
            raise BackendError("No Claude models configured.")
        last_exc: RateLimitError | None = None
        for i, m in enumerate(models):
            if i > 0:
                sys.stderr.write(
                    f"[doubletake] ⚠️ {models[i - 1]} rate-limited;"
                    f" falling back to {m}.\n"
                )
            body = {
                "model": m,
                "max_tokens": 8192,
                "system": system_prompt,
                "messages": [{"role": "user", "content": prompt}],
                "stream": True,
            }
            try:
                yield from _stream(
                    f"{_CLAUDE_API_BASE}/messages",
                    headers, body, idle_timeout,
                    emit_fn=_emit_claude_text,
                    what="Claude Code",
                )
                return
            except RateLimitError as exc:
                last_exc = exc
        raise last_exc  # type: ignore[misc]  — loop ran (models non-empty), set on first 429

    # ── Gemini Developer API (forced or fallback) ─────────────────────────
    have_login = os.path.exists(_OAUTH_CREDS_PATH)
    api_key = os.getenv("GEMINI_API_KEY")

    if forced == "gemini_api" or (not have_login and api_key):
        if not api_key:
            raise AuthError(
                "DOUBLETAKE_BACKEND=gemini_api but GEMINI_API_KEY is unset."
            )
        model = model or _DEFAULT_GEMINI_API_MODEL
        url = f"{_GEMINI_API_BASE}/models/{model}:streamGenerateContent?alt=sse"
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "systemInstruction": {"parts": [{"text": system_prompt}]},
        }
        yield from _stream(
            url, {"x-goog-api-key": api_key}, body, idle_timeout,
            emit_fn=lambda c: _emit_gemini_text(c, unwrap=False),
            what="Gemini API",
        )
        return

    # ── Antigravity subscription (Code Assist OAuth) ──────────────────────
    if not have_login:
        raise AuthError(
            f"No Antigravity login at {_OAUTH_CREDS_PATH} and no GEMINI_API_KEY. "
            "Sign in via the Antigravity app, or set GEMINI_API_KEY."
        )
    token = _access_token()
    project = _discover_project(token)
    # If the user pinned a model, use only that. Otherwise try the chain.
    models = [model] if model else _CODE_ASSIST_FALLBACK_MODELS
    if not models:
        raise BackendError("No Code Assist models configured.")
    last_exc: RateLimitError | None = None
    for i, m in enumerate(models):
        if i > 0:
            sys.stderr.write(
                f"[doubletake] ⚠️ {models[i - 1]} rate-limited;"
                f" falling back to {m}.\n"
            )
        body = {
            "model": m,
            "user_prompt_id": str(uuid.uuid4()),
            "request": {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "systemInstruction": {"parts": [{"text": system_prompt}]},
            },
        }
        if project:
            body["project"] = project
        try:
            yield from _stream(
                f"{_CODE_ASSIST_BASE}:streamGenerateContent?alt=sse",
                {"Authorization": f"Bearer {token}"}, body, idle_timeout,
                emit_fn=lambda c: _emit_gemini_text(c, unwrap=True),
                what="Antigravity (Code Assist)",
            )
            return
        except RateLimitError as exc:
            last_exc = exc
    raise last_exc  # type: ignore[misc]  — loop ran (models non-empty), set on first 429
