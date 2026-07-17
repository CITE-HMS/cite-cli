"""Tests for the internal rotating-log helper and `cite log` command."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cite import _log
from cite._log import _Tee, init_logging
from cite.cli import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# _Tee
# ---------------------------------------------------------------------------


def test_tee_writes_to_both_streams(tmp_path: Path) -> None:
    log_file = tmp_path / "out.log"
    with log_file.open("w", encoding="utf-8") as lf:
        import io

        buf = io.StringIO()
        tee = _Tee(buf, lf)
        tee.write("hello tee\n")
        tee.flush()

    assert "hello tee" in buf.getvalue()
    assert "hello tee" in log_file.read_text()


def test_tee_strips_ansi_from_log_file(tmp_path: Path) -> None:
    """ANSI codes reach the terminal stream but are stripped from the log file."""
    import io

    log_file = tmp_path / "ansi.log"
    with log_file.open("w", encoding="utf-8") as lf:
        buf = io.StringIO()
        tee = _Tee(buf, lf)
        tee.write("\x1b[1;32mcoloured\x1b[0m plain\n")
        tee.flush()

    # Terminal gets raw ANSI
    assert "\x1b[1;32m" in buf.getvalue()
    # Log file has plain text only
    log_content = log_file.read_text()
    assert "\x1b" not in log_content
    assert "coloured plain" in log_content


def test_tee_handles_unicode_with_legacy_windows_encoding(tmp_path: Path) -> None:
    """Unicode status output must not crash a cp1252 scheduled task."""
    import io

    class Cp1252Stream:
        encoding = "cp1252"
        errors = "strict"

        def __init__(self) -> None:
            self.buffer = io.BytesIO()

        def write(self, text: str) -> int:
            self.buffer.write(text.encode(self.encoding, errors=self.errors))
            return len(text)

        def flush(self) -> None:
            pass

    log_file = tmp_path / "unicode.log"
    terminal = Cp1252Stream()
    with log_file.open("w", encoding="utf-8") as lf:
        tee = _Tee(terminal, lf)
        tee.write("Renewal detected: 2026-01-01 → 2027-01-01.\n")
        tee.flush()

    assert terminal.buffer.getvalue().decode("cp1252") == (
        "Renewal detected: 2026-01-01 ? 2027-01-01.\n"
    )
    assert "2026-01-01 → 2027-01-01" in log_file.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# init_logging
# ---------------------------------------------------------------------------


def test_init_logging_creates_log_file() -> None:
    log_path = _log.CITE_LOG
    assert not log_path.exists()
    init_logging()
    assert log_path.exists()


def test_init_logging_wraps_stdout() -> None:
    assert not isinstance(sys.stdout, _Tee)
    init_logging()
    assert isinstance(sys.stdout, _Tee)


def test_init_logging_is_idempotent() -> None:
    init_logging()
    stdout_after_first = sys.stdout
    init_logging()
    assert sys.stdout is stdout_after_first  # not double-wrapped


def test_init_logging_stdout_written_to_file() -> None:
    init_logging()
    sys.stdout.write("sentinel-line\n")
    sys.stdout.flush()
    content = _log.CITE_LOG.read_text(encoding="utf-8")
    assert "sentinel-line" in content


def test_init_logging_captures_typer_secho_output() -> None:
    """Regression test for the Windows bug where click's Windows-console
    fast path bypassed our Tee, leaving cite.log empty.

    On macOS/Linux this passes trivially; on Windows it would fail
    before the _patch_click_for_tee fix because click writes directly
    to the Windows Console handle instead of through sys.stdout.
    """
    import typer

    init_logging()
    typer.secho("typer-secho-marker", fg="green")
    typer.secho("typer-stderr-marker", fg="red", err=True)
    sys.stdout.flush()
    sys.stderr.flush()

    content = _log.CITE_LOG.read_text(encoding="utf-8")
    assert "typer-secho-marker" in content
    assert "typer-stderr-marker" in content


def test_init_logging_simulated_windows_console_path(monkeypatch) -> None:
    """Simulate the Windows-only failure mode by forcing click's
    Windows-console fast path to be active on this platform.

    Without `_patch_click_for_tee`'s neutralisation, click would write
    directly to the console-stream returned by `_get_windows_console_stream`,
    bypassing our Tee. With the fix, that selector returns None even on
    Windows (and during this simulated run), so click falls through to
    wrap sys.stdout (= our Tee) instead.

    This test would fail on Windows pre-fix; the simulation lets us
    catch a regression on any platform.
    """
    import io

    import typer
    from click import _compat

    # Build a sentinel "Windows console stream" that DOES NOT go through
    # sys.stdout. If click ever used this stream instead of going through
    # sys.stdout, our Tee would never see the writes and the assertion
    # below would catch it.
    bypass_buffer = io.StringIO()

    # Inject a Windows-console-stream selector BEFORE init_logging runs.
    # If the regression returns, click will use this bypass_buffer for
    # output and our log assertion below fails.
    monkeypatch.setattr(
        _compat,
        "_get_windows_console_stream",
        lambda *_a, **_kw: bypass_buffer,
    )

    init_logging()  # this should reinstall our None-returning override

    typer.secho("post-patch-marker", fg="green")
    sys.stdout.flush()

    # Our patch must have neutralised the injected bypass on every
    # platform — output should reach the log AND NOT leak to bypass_buffer.
    assert bypass_buffer.getvalue() == "", (
        "Click bypassed sys.stdout via the Windows-console stream — "
        "_patch_click_for_tee fix is not effective"
    )
    content = _log.CITE_LOG.read_text(encoding="utf-8")
    assert "post-patch-marker" in content


# ---------------------------------------------------------------------------
# cite log command
# ---------------------------------------------------------------------------


def test_cli_log_opens_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cite log always calls open_logs_dir() and prints the path."""
    logs_dir = _log.LOGS_DIR

    calls: list[str] = []
    monkeypatch.setattr(_log, "open_logs_dir", lambda: calls.append("opened"))

    result = runner.invoke(app, ["log"])
    assert result.exit_code == 0, result.output
    assert calls == ["opened"]
    assert str(logs_dir) in result.output


def test_cli_log_shows_cite_log_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When cite.log exists, its size is printed."""
    logs_dir = _log.LOGS_DIR
    logs_dir.mkdir(parents=True)
    (_log.CITE_LOG).write_text("x" * 2048, encoding="utf-8")

    monkeypatch.setattr(_log, "open_logs_dir", lambda: None)

    result = runner.invoke(app, ["log"])
    assert result.exit_code == 0, result.output
    assert "cite.log" in result.output
