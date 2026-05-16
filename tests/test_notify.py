"""Tests for the failure-alert email helper and the alert-on-failure wrapper."""

from __future__ import annotations

from datetime import date, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import ClassVar

import pytest
from typer.testing import CliRunner

from cite import _notify, _renew
from cite._notify import send_apply_success_email, send_failure_email
from cite._renew import LicenseInfo
from cite.cli import app

runner = CliRunner()


class _FakeSMTP:
    """Records the last sent message; mimics smtplib.SMTP enough for tests."""

    instances: ClassVar[list[_FakeSMTP]] = []

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.starttls_called = False
        self.login_args: tuple[str, str] | None = None
        self.sent: EmailMessage | None = None
        _FakeSMTP.instances.append(self)

    def __enter__(self) -> _FakeSMTP:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def starttls(self) -> None:
        self.starttls_called = True

    def login(self, user: str, password: str) -> None:
        self.login_args = (user, password)

    def send_message(self, msg: EmailMessage) -> None:
        self.sent = msg


@pytest.fixture()
def fake_smtp(monkeypatch):
    _FakeSMTP.instances = []
    monkeypatch.setattr(_notify.smtplib, "SMTP", _FakeSMTP)
    return _FakeSMTP


def _set_creds(monkeypatch, **overrides: str) -> None:
    base = {
        "CITE_ALERT_SMTP_USER": "sender@gmail.com",
        "CITE_ALERT_SMTP_PASSWORD": "app-password",
        "CITE_ALERT_TO": "ops@example.com",
    }
    base.update(overrides)
    for k, v in base.items():
        monkeypatch.setenv(k, v)


# --- send_failure_email ---


def test_send_failure_email_noop_when_unconfigured(fake_smtp) -> None:
    assert send_failure_email("renew", RuntimeError("boom")) is False
    assert fake_smtp.instances == []


def test_send_failure_email_noop_when_partial_config(fake_smtp, monkeypatch) -> None:
    monkeypatch.setenv("CITE_ALERT_SMTP_USER", "sender@gmail.com")
    # Password and recipient list missing → not configured.
    assert send_failure_email("renew", RuntimeError("boom")) is False
    assert fake_smtp.instances == []


def test_send_failure_email_sends_when_configured(fake_smtp, monkeypatch) -> None:
    _set_creds(monkeypatch)
    err = RuntimeError("the dongle exploded")
    assert send_failure_email("renew", err) is True

    assert len(fake_smtp.instances) == 1
    sent = fake_smtp.instances[0]
    assert sent.host == "smtp.gmail.com"
    assert sent.port == 587
    assert sent.starttls_called is True
    assert sent.login_args == ("sender@gmail.com", "app-password")

    assert sent.sent is not None
    assert sent.sent["From"] == "sender@gmail.com"
    assert sent.sent["To"] == "ops@example.com"
    assert "renew failed" in sent.sent["Subject"]
    body = sent.sent.get_content()
    assert "RuntimeError: the dongle exploded" in body
    assert "Command: cite renew" in body


def test_send_failure_email_honours_overrides(fake_smtp, monkeypatch) -> None:
    _set_creds(
        monkeypatch,
        CITE_ALERT_SMTP_HOST="smtp.example.org",
        CITE_ALERT_SMTP_PORT="2525",
        CITE_ALERT_FROM="alerts@example.org",
    )
    assert send_failure_email("clean", RuntimeError("x")) is True
    sent = fake_smtp.instances[0]
    assert (sent.host, sent.port) == ("smtp.example.org", 2525)
    assert sent.sent is not None
    assert sent.sent["From"] == "alerts@example.org"


def test_send_failure_email_swallows_smtp_errors(monkeypatch) -> None:
    _set_creds(monkeypatch)

    class _Broken:
        def __init__(self, *a: object, **k: object) -> None:
            raise OSError("connection refused")

    monkeypatch.setattr(_notify.smtplib, "SMTP", _Broken)
    # Must not raise — caller relies on this to never mask the real error.
    assert send_failure_email("renew", RuntimeError("x")) is False


# --- alert-on-failure CLI integration ---


def test_renew_failure_dispatches_alert(
    fake_smtp, monkeypatch, c2l_file: Path, tmp_state_path: Path
) -> None:
    _set_creds(monkeypatch)

    def _boom() -> LicenseInfo:
        raise RuntimeError("Could not reach Sentinel ACC at http://localhost:1947")

    monkeypatch.setattr(_renew, "get_license_info", _boom)

    result = runner.invoke(
        app,
        [
            "renew",
            "--email",
            "x@example.com",
            "--full-name",
            "X",
            "--c2l-file",
            str(c2l_file),
            "--url",
            "test",
        ],
    )
    assert result.exit_code == 1
    assert "Could not reach Sentinel ACC" in result.output
    assert "Failure alert email sent." in result.output

    assert len(fake_smtp.instances) == 1
    sent = fake_smtp.instances[0].sent
    assert sent is not None
    # The underlying RuntimeError is surfaced via the __cause__ chain.
    body = sent.get_content()
    assert "RuntimeError" in body
    assert "Could not reach Sentinel ACC" in body


def test_renew_success_does_not_dispatch(
    fake_smtp, monkeypatch, mock_server, c2l_file: Path, tmp_state_path: Path
) -> None:
    _set_creds(monkeypatch)
    near = date.today() + timedelta(days=3)
    monkeypatch.setattr(
        _renew,
        "get_license_info",
        lambda: LicenseInfo(expiration_date=near, hasp_id="159918744"),
    )

    result = runner.invoke(
        app,
        [
            "renew",
            "--email",
            "x@example.com",
            "--full-name",
            "X",
            "--c2l-file",
            str(c2l_file),
            "--url",
            "test",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Submitted. HTTP 200" in result.output
    assert "Failure alert" not in result.output
    assert fake_smtp.instances == []


def test_renew_failure_silent_when_unconfigured(
    fake_smtp, monkeypatch, c2l_file: Path, tmp_state_path: Path
) -> None:
    # No CITE_ALERT_* vars set (the conftest fixture clears them).
    def _boom() -> LicenseInfo:
        raise RuntimeError("nope")

    monkeypatch.setattr(_renew, "get_license_info", _boom)

    result = runner.invoke(
        app,
        [
            "renew",
            "--email",
            "x@example.com",
            "--full-name",
            "X",
            "--c2l-file",
            str(c2l_file),
            "--url",
            "test",
        ],
    )
    assert result.exit_code == 1
    assert "Failure alert" not in result.output
    assert fake_smtp.instances == []


def test_clean_failure_dispatches_alert(fake_smtp, monkeypatch, tmp_path: Path) -> None:
    _set_creds(monkeypatch)
    # Force the "no default directories found" failure path.
    monkeypatch.setattr("cite.cli.DEFAULT_PATHS", [str(tmp_path / "does-not-exist")])

    result = runner.invoke(app, ["clean"])
    assert result.exit_code == 1
    assert "No default directories" in result.output
    assert "Failure alert email sent." in result.output

    assert len(fake_smtp.instances) == 1
    sent = fake_smtp.instances[0].sent
    assert sent is not None
    assert "clean failed" in sent["Subject"]


def test_clean_abort_does_not_dispatch(fake_smtp, monkeypatch, tmp_path: Path) -> None:
    """User-cancellation (typer.Abort) must NOT trigger an alert."""
    _set_creds(monkeypatch)

    import time

    from cite import _cleanup

    # Push `iter_old_files`'s reference time 100 days into the future so a
    # freshly-created file looks 100 days old (works around the fact that
    # os.utime cannot backdate ctime on POSIX).
    monkeypatch.setattr(_cleanup, "TIME", time.time() + 100 * 86400)
    (tmp_path / "old.txt").write_text("x")

    # Answering "n" to the confirm prompt raises typer.Abort.
    result = runner.invoke(app, ["clean", str(tmp_path), "--days", "30"], input="n\n")
    assert result.exit_code != 0
    assert "Failure alert" not in result.output
    assert fake_smtp.instances == []


# --- cite license command ---


def test_cli_license_prints_info(monkeypatch) -> None:
    monkeypatch.setattr(
        _renew,
        "get_license_info",
        lambda: LicenseInfo(expiration_date=date(2026, 6, 5), hasp_id="159918744"),
    )
    result = runner.invoke(app, ["license"])
    assert result.exit_code == 0, result.output
    assert "License expires 2026-06-05" in result.output
    assert "HASP ID: 159918744" in result.output


def test_cli_license_error_exits_nonzero(monkeypatch) -> None:
    def _boom() -> LicenseInfo:
        raise RuntimeError("ACC offline")

    monkeypatch.setattr(_renew, "get_license_info", _boom)
    result = runner.invoke(app, ["license"])
    assert result.exit_code == 1
    assert "ACC offline" in result.output


def test_cli_license_raw_dumps_response(monkeypatch) -> None:
    import requests

    class _FakeResp:
        status_code = 200
        text = '/*JSON:features*/\n{"ven":"40094"}'

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp())
    result = runner.invoke(app, ["license", "--raw"])
    assert result.exit_code == 0, result.output
    assert '"ven":"40094"' in result.output


# --- cite test-alert command ---


def test_cli_test_alert_unconfigured_errors_with_hint() -> None:
    """No alert env vars → exit 1 with setup instructions, no SMTP attempt."""
    result = runner.invoke(app, ["test-alert"])
    assert result.exit_code == 1
    assert "Alert env vars not set" in result.output
    assert "setx CITE_ALERT_SMTP_USER" in result.output


def test_cli_test_alert_configured_sends(fake_smtp, monkeypatch) -> None:
    _set_creds(monkeypatch)
    result = runner.invoke(app, ["test-alert"])
    assert result.exit_code == 0, result.output
    assert "Test alert sent to ops@example.com" in result.output

    assert len(fake_smtp.instances) == 1
    sent = fake_smtp.instances[0].sent
    assert sent is not None
    assert "test-alert failed" in sent["Subject"]
    body = sent.get_content()
    assert "Test alert from `cite test-alert`" in body


def test_cli_test_alert_smtp_failure_shows_troubleshooting(monkeypatch) -> None:
    _set_creds(monkeypatch)

    class _Broken:
        def __init__(self, *a: object, **k: object) -> None:
            raise OSError("connection refused")

    monkeypatch.setattr(_notify.smtplib, "SMTP", _Broken)
    result = runner.invoke(app, ["test-alert"])
    assert result.exit_code == 1
    assert "SMTP send failed" in result.output
    assert "App Password" in result.output


# --- send_apply_success_email ---


def _make_license(exp: date, hasp_id: str = "159918744") -> LicenseInfo:
    return LicenseInfo(expiration_date=exp, hasp_id=hasp_id)


def test_send_apply_success_email_noop_when_unconfigured(fake_smtp) -> None:
    before = _make_license(date(2026, 6, 5))
    after = _make_license(date(2026, 9, 5))
    assert send_apply_success_email(before, after) is False
    assert fake_smtp.instances == []


def test_send_apply_success_email_sends_when_configured(fake_smtp, monkeypatch) -> None:
    _set_creds(monkeypatch)
    before = _make_license(date(2026, 6, 5))
    after = _make_license(date(2026, 9, 5))

    assert send_apply_success_email(before, after) is True

    assert len(fake_smtp.instances) == 1
    sent = fake_smtp.instances[0]
    assert sent.starttls_called is True
    assert sent.login_args == ("sender@gmail.com", "app-password")
    assert sent.sent is not None

    msg = sent.sent
    assert msg["From"] == "sender@gmail.com"
    assert msg["To"] == "ops@example.com"
    assert "NIS-Elements license renewed" in msg["Subject"]

    body = msg.get_content()
    assert "09882A98" in body  # hex HASP ID (decimal 159918744)
    assert "159918744" in body  # decimal HASP ID
    assert "2026-06-05" in body  # old expiry
    assert "2026-09-05" in body  # new expiry
    assert "92" in body  # days gained (92 days between Jun 5 and Sep 5)


def test_send_apply_success_email_swallows_smtp_errors(
    monkeypatch,
) -> None:
    _set_creds(monkeypatch)

    class _Broken:
        def __init__(self, *a: object, **k: object) -> None:
            raise OSError("connection refused")

    monkeypatch.setattr(_notify.smtplib, "SMTP", _Broken)
    before = _make_license(date(2026, 6, 5))
    after = _make_license(date(2026, 9, 5))
    assert send_apply_success_email(before, after) is False
