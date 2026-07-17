"""Tests for cite._calendar (ICS building + reminder-invite emails)."""

from __future__ import annotations

from datetime import date
from typing import ClassVar

import pytest

from cite import _calendar
from cite._calendar import build_reminder_ics, send_reminder_invites


class _FakeSMTP:
    instances: ClassVar[list[_FakeSMTP]] = []

    def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
        self.host = host
        self.port = port
        self.sent = None
        _FakeSMTP.instances.append(self)

    def __enter__(self) -> _FakeSMTP:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def starttls(self) -> None:
        pass

    def login(self, user: str, password: str) -> None:
        pass

    def send_message(self, msg) -> None:  # type: ignore[no-untyped-def]
        self.sent = msg


@pytest.fixture
def fake_smtp(monkeypatch):
    _FakeSMTP.instances = []
    monkeypatch.setattr(_calendar.smtplib, "SMTP", _FakeSMTP)
    return _FakeSMTP


def _set_creds(monkeypatch) -> None:
    monkeypatch.setenv("CITE_ALERT_SMTP_USER", "sender@example.com")
    monkeypatch.setenv("CITE_ALERT_SMTP_PASSWORD", "app-password")
    monkeypatch.setenv("CITE_ALERT_TO", "a@example.com, b@example.com")


# --- build_reminder_ics ------------------------------------------------------


def test_ics_has_three_all_day_events_on_correct_days() -> None:
    ics = build_reminder_ics(
        station="Station 5 (Dongle 202136)",
        hasp_hex="4B92F5FA",
        expiry=date(2027, 8, 1),
        organizer="sender@example.com",
        attendees=["a@example.com"],
    )
    assert ics.count("BEGIN:VEVENT") == 3
    assert "METHOD:REQUEST" in ics
    # 14 days before, 7 days before, day-of (with DTEND the next day).
    assert "DTSTART;VALUE=DATE:20270718" in ics
    assert "DTSTART;VALUE=DATE:20270725" in ics
    assert "DTSTART;VALUE=DATE:20270801" in ics
    assert "DTEND;VALUE=DATE:20270802" in ics


def test_ics_titles_follow_apps_script_convention() -> None:
    ics = build_reminder_ics(
        station="Station 5 (Dongle 202136)",
        hasp_hex="4B92F5FA",
        expiry=date(2027, 8, 1),
        organizer="sender@example.com",
        attendees=["a@example.com"],
    )
    unfolded = ics.replace("\r\n ", "")
    assert "license expires in 14 days — HASP 4B92F5FA" in unfolded
    assert "license expires in 7 days — HASP 4B92F5FA" in unfolded
    assert "license EXPIRES TODAY — HASP 4B92F5FA" in unfolded
    assert "Station 5 (Dongle 202136)" in unfolded


def test_ics_uids_are_deterministic() -> None:
    kwargs = dict(
        station="Station 5",
        hasp_hex="4B92F5FA",
        expiry=date(2027, 8, 1),
        organizer="sender@example.com",
        attendees=["a@example.com"],
    )
    a, b = build_reminder_ics(**kwargs), build_reminder_ics(**kwargs)  # type: ignore[arg-type]
    for offset in ("14d", "7d", "0d"):
        uid = f"UID:cite-4B92F5FA-2027-08-01-{offset}@cite-hms"
        assert uid in a
        assert uid in b


def test_ics_uses_crlf_and_folds_long_lines() -> None:
    ics = build_reminder_ics(
        station="Station 5 (Dongle 202136)",
        hasp_hex="4B92F5FA",
        expiry=date(2027, 8, 1),
        organizer="sender@example.com",
        attendees=["a@example.com"],
    )
    assert "\n" not in ics.replace("\r\n", "")
    for line in ics.split("\r\n"):
        assert len(line) <= 74


def test_ics_lists_all_attendees() -> None:
    ics = build_reminder_ics(
        station=None,
        hasp_hex="4B92F5FA",
        expiry=date(2027, 8, 1),
        organizer="sender@example.com",
        attendees=["a@example.com", "b@example.com"],
    )
    assert ics.count("ATTENDEE;RSVP=FALSE:mailto:a@example.com") == 3
    assert ics.count("ATTENDEE;RSVP=FALSE:mailto:b@example.com") == 3


# --- send_reminder_invites ---------------------------------------------------


def test_send_noop_when_unconfigured(fake_smtp) -> None:
    assert send_reminder_invites("Station 5", "4B92F5FA", date(2027, 8, 1)) is False
    assert fake_smtp.instances == []


def test_send_delivers_calendar_part(fake_smtp, monkeypatch) -> None:
    _set_creds(monkeypatch)
    ok = send_reminder_invites("Station 5", "4B92F5FA", date(2027, 8, 1))
    assert ok is True
    msg = fake_smtp.instances[0].sent
    assert msg is not None
    assert "Calendar reminders" in msg["Subject"]
    assert "2027-08-01" in msg["Subject"]
    assert msg["To"] == "a@example.com, b@example.com"
    raw = msg.as_string()
    assert 'method="REQUEST"' in raw or "method=REQUEST" in raw
    assert "text/calendar" in raw


def test_send_swallows_smtp_errors(fake_smtp, monkeypatch) -> None:
    _set_creds(monkeypatch)

    def boom(self, msg) -> None:  # type: ignore[no-untyped-def]
        raise OSError("connection reset")

    monkeypatch.setattr(_FakeSMTP, "send_message", boom)
    assert send_reminder_invites("Station 5", "4B92F5FA", date(2027, 8, 1)) is False
