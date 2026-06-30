"""Tests for doubletake.cli — argument parsing, I/O handling, error propagation."""

from __future__ import annotations

import io
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

from doubletake.cli import (
    DEFAULT_IDLE_TIMEOUT,
    VERSION,
    _idle_timeout,
    _read_prompt,
    main,
)
from doubletake import client


# ============================================================================
# _idle_timeout
# ============================================================================

class TestIdleTimeout:
    def test_returns_default_when_unset(self):
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("DOUBLETAKE_TIMEOUT", None)
            assert _idle_timeout() == DEFAULT_IDLE_TIMEOUT

    def test_returns_custom_value(self):
        with patch.dict("os.environ", {"DOUBLETAKE_TIMEOUT": "60"}):
            assert _idle_timeout() == 60.0

    def test_returns_float_value(self):
        with patch.dict("os.environ", {"DOUBLETAKE_TIMEOUT": "45.5"}):
            assert _idle_timeout() == 45.5

    def test_invalid_string_warns_and_returns_default(self, capsys):
        with patch.dict("os.environ", {"DOUBLETAKE_TIMEOUT": "not-a-number"}):
            result = _idle_timeout()
        assert result == DEFAULT_IDLE_TIMEOUT
        assert "Ignoring invalid" in capsys.readouterr().err

    def test_zero_warns_and_returns_default(self, capsys):
        with patch.dict("os.environ", {"DOUBLETAKE_TIMEOUT": "0"}):
            result = _idle_timeout()
        assert result == DEFAULT_IDLE_TIMEOUT
        assert "must be > 0" in capsys.readouterr().err

    def test_negative_warns_and_returns_default(self, capsys):
        with patch.dict("os.environ", {"DOUBLETAKE_TIMEOUT": "-10"}):
            result = _idle_timeout()
        assert result == DEFAULT_IDLE_TIMEOUT
        assert "must be > 0" in capsys.readouterr().err

    def test_float_zero_warns_and_returns_default(self, capsys):
        with patch.dict("os.environ", {"DOUBLETAKE_TIMEOUT": "0.0"}):
            result = _idle_timeout()
        assert result == DEFAULT_IDLE_TIMEOUT
        assert "must be > 0" in capsys.readouterr().err

    def test_very_small_positive_accepted(self):
        with patch.dict("os.environ", {"DOUBLETAKE_TIMEOUT": "0.001"}):
            assert _idle_timeout() == 0.001

    def test_empty_string_warns_and_returns_default(self, capsys):
        with patch.dict("os.environ", {"DOUBLETAKE_TIMEOUT": ""}):
            result = _idle_timeout()
        assert result == DEFAULT_IDLE_TIMEOUT
        assert "Ignoring invalid" in capsys.readouterr().err


# ============================================================================
# _read_prompt
# ============================================================================

class TestReadPrompt:
    def test_reads_stdin(self):
        with patch("sys.stdin", io.StringIO("hello world")):
            with patch("sys.stdin.isatty", return_value=False):
                result = _read_prompt()
        assert result == "hello world"

    def test_tty_stdin_exits_1(self):
        with patch("sys.stdin.isatty", return_value=True):
            with pytest.raises(SystemExit) as exc_info:
                _read_prompt()
        assert exc_info.value.code == 1

    def test_empty_stdin_exits_1(self, capsys):
        with patch("sys.stdin", io.StringIO("   \n  \t  ")):
            with patch("sys.stdin.isatty", return_value=False):
                with pytest.raises(SystemExit) as exc_info:
                    _read_prompt()
        assert exc_info.value.code == 1
        assert "empty" in capsys.readouterr().err

    def test_unicode_decode_error_exits_1(self, capsys):
        mock_stdin = MagicMock()
        mock_stdin.isatty.return_value = False
        mock_stdin.read.side_effect = UnicodeDecodeError("utf-8", b"", 0, 1, "reason")
        with patch("sys.stdin", mock_stdin):
            with pytest.raises(SystemExit) as exc_info:
                _read_prompt()
        assert exc_info.value.code == 1
        assert "stdin" in capsys.readouterr().err

    def test_closed_stdin_value_error_exits_1(self, capsys):
        mock_stdin = MagicMock()
        mock_stdin.isatty.return_value = False
        mock_stdin.read.side_effect = ValueError("I/O operation on closed file")
        with patch("sys.stdin", mock_stdin):
            with pytest.raises(SystemExit) as exc_info:
                _read_prompt()
        assert exc_info.value.code == 1
        assert "stdin" in capsys.readouterr().err

    def test_whitespace_only_prompt_exits_1(self, capsys):
        with patch("sys.stdin", io.StringIO("\n\n\n")):
            with patch("sys.stdin.isatty", return_value=False):
                with pytest.raises(SystemExit) as exc_info:
                    _read_prompt()
        assert exc_info.value.code == 1

    def test_prompt_with_only_leading_whitespace_is_valid(self):
        with patch("sys.stdin", io.StringIO("   real content")):
            with patch("sys.stdin.isatty", return_value=False):
                result = _read_prompt()
        assert result == "   real content"


# ============================================================================
# main — flag handling
# ============================================================================

class TestMainFlags:
    def _run(self, args: list[str], stdin_content: str = "prompt"):
        with (
            patch("sys.argv", ["doubletake"] + args),
            patch("sys.stdin", io.StringIO(stdin_content)),
            patch("sys.stdin.isatty", return_value=False),
        ):
            return main

    def test_help_flag_prints_and_exits_0(self, capsys):
        with patch("sys.argv", ["doubletake", "--help"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code == 0
        assert "USAGE" in capsys.readouterr().out

    def test_help_short_flag(self, capsys):
        with patch("sys.argv", ["doubletake", "-h"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code == 0

    def test_version_flag_prints_and_exits_0(self, capsys):
        with patch("sys.argv", ["doubletake", "--version"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code == 0
        assert VERSION in capsys.readouterr().out

    def test_version_short_flag(self, capsys):
        with patch("sys.argv", ["doubletake", "-v"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code == 0
        assert VERSION in capsys.readouterr().out


# ============================================================================
# main — stream_review integration
# ============================================================================

class TestMainStreamIntegration:
    def _run_main(self, stdin: str = "test prompt", stream_tokens=("hello", " world")):
        with (
            patch("sys.argv", ["doubletake"]),
            patch("sys.stdin", io.StringIO(stdin)),
            patch("sys.stdin.isatty", return_value=False),
            patch("doubletake.cli.client.stream_review",
                  return_value=iter(stream_tokens)),
        ):
            main()

    def test_writes_tokens_to_stdout(self, capsys):
        self._run_main()
        out = capsys.readouterr().out
        assert "hello" in out
        assert " world" in out

    def test_adds_trailing_newline_when_missing(self, capsys):
        self._run_main(stream_tokens=("no newline",))
        out = capsys.readouterr().out
        assert out.endswith("\n")

    def test_does_not_double_newline_when_present(self, capsys):
        self._run_main(stream_tokens=("ends with newline\n",))
        out = capsys.readouterr().out
        assert not out.endswith("\n\n")

    def test_empty_response_exits_1(self, capsys):
        with (
            patch("sys.argv", ["doubletake"]),
            patch("sys.stdin", io.StringIO("prompt")),
            patch("sys.stdin.isatty", return_value=False),
            patch("doubletake.cli.client.stream_review", return_value=iter([])),
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code == 1
        assert "Empty" in capsys.readouterr().err

    def test_stream_of_empty_strings_exits_1(self, capsys):
        # Generator that yields only empty strings must also be treated as empty.
        with (
            patch("sys.argv", ["doubletake"]),
            patch("sys.stdin", io.StringIO("prompt")),
            patch("sys.stdin.isatty", return_value=False),
            patch("doubletake.cli.client.stream_review", return_value=iter(["", ""])),
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code == 1


# ============================================================================
# main — error handling
# ============================================================================

class TestMainErrorHandling:
    def _run_with_error(self, exc):
        with (
            patch("sys.argv", ["doubletake"]),
            patch("sys.stdin", io.StringIO("prompt")),
            patch("sys.stdin.isatty", return_value=False),
            patch("doubletake.cli.client.stream_review", side_effect=exc),
        ):
            main()

    def test_auth_error_exits_1(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            self._run_with_error(client.AuthError("bad creds"))
        assert exc_info.value.code == 1
        assert "bad creds" in capsys.readouterr().err

    def test_backend_error_exits_1(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            self._run_with_error(client.BackendError("rate limit"))
        assert exc_info.value.code == 1
        assert "rate limit" in capsys.readouterr().err

    def test_timeout_error_exits_1(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            self._run_with_error(TimeoutError("no output"))
        assert exc_info.value.code == 1
        assert "no output" in capsys.readouterr().err

    def test_keyboard_interrupt_exits_130(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            self._run_with_error(KeyboardInterrupt())
        assert exc_info.value.code == 130
        assert "Interrupted" in capsys.readouterr().err

    def test_oserror_on_stdout_write_exits_1(self, capsys):
        def _tokens():
            yield "token"

        with (
            patch("sys.argv", ["doubletake"]),
            patch("sys.stdin", io.StringIO("prompt")),
            patch("sys.stdin.isatty", return_value=False),
            patch("doubletake.cli.client.stream_review", return_value=_tokens()),
            patch("sys.stdout.write", side_effect=OSError("disk full")),
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code == 1

    def test_broken_pipe_exits_0(self):
        def _tokens():
            yield "token"

        with (
            patch("sys.argv", ["doubletake"]),
            patch("sys.stdin", io.StringIO("prompt")),
            patch("sys.stdin.isatty", return_value=False),
            patch("doubletake.cli.client.stream_review", return_value=_tokens()),
            patch("sys.stdout.write", side_effect=BrokenPipeError()),
            # Mock OS calls so we don't actually replace the real stdout fd.
            patch("os.open", return_value=99),
            patch("os.dup2"),
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code == 0

    def test_broken_pipe_handler_survives_fileno_failure(self):
        """fileno() may raise if stdout is a non-fd stream (e.g. StringIO)."""
        def _tokens():
            yield "token"

        with (
            patch("sys.argv", ["doubletake"]),
            patch("sys.stdin", io.StringIO("prompt")),
            patch("sys.stdin.isatty", return_value=False),
            patch("doubletake.cli.client.stream_review", return_value=_tokens()),
            patch("sys.stdout.write", side_effect=BrokenPipeError()),
            patch("sys.stdout.fileno", side_effect=io.UnsupportedOperation("fileno")),
            patch("os.open", return_value=99),
            patch("os.dup2"),
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code == 0
