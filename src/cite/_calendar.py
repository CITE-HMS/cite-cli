"""Google-Calendar reminders via iCalendar email invites.

When a license renewal is detected, `send_reminder_invites` emails a single
.ics invite (METHOD:REQUEST) to CITE_ALERT_TO containing one recurring all-day
event. Its three weekly occurrences fall 14 days before, 7 days before, and on
the new expiration date. Gmail can add recognized invites to Google Calendar
without a Google API or OAuth. Reuses the same SMTP env vars as `cite._notify`
(see that module's docstring).

All-day events are used (rather than VALARMs) because Google Calendar
ignores custom VALARMs on incoming invites; the account's default event
notifications still apply. The event UID is deterministic per (HASP ID,
expiry), so a re-send updates the existing series instead of duplicating it.
"""

from __future__ import annotations

import os
import smtplib
import socket
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage

from cite._notify import _is_configured

_REMINDER_OFFSETS: tuple[tuple[int, str], ...] = (
    (14, "14 days before expiration"),
    (7, "7 days before expiration"),
    (0, "expiration day"),
)


def _fold(line: str) -> str:
    """Fold a content line at 74 UTF-8 octets per RFC 5545 section 3.1."""
    parts: list[str] = []
    current: list[str] = []
    current_octets = 0

    for char in line:
        char_octets = len(char.encode("utf-8"))
        limit = 74 if not parts else 73  # continuation line starts with a space
        if current and current_octets + char_octets > limit:
            parts.append("".join(current))
            current = []
            current_octets = 0
        current.append(char)
        current_octets += char_octets

    parts.append("".join(current))
    return "\r\n ".join(parts)


def _display_date(value: date) -> str:
    """Format a date naturally without platform-specific strftime flags."""
    return f"{value.strftime('%B')} {value.day}, {value.year}"


def _escape_text(value: str) -> str:
    """Escape an iCalendar TEXT value per RFC 5545 section 3.3.11."""
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def build_reminder_ics(
    station: str | None,
    hasp_hex: str,
    expiry: date,
    organizer: str,
    attendees: list[str],
) -> str:
    """Return a VCALENDAR request with one three-occurrence reminder series.

    Pure function — no I/O. CRLF line endings per RFC 5545.
    """
    where = station or "unknown station"
    dtstamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    first_reminder = expiry - timedelta(days=14)
    summary = f"NIS-Elements license renewal reminder — {where}"
    description = (
        f"NIS-Elements license on {where} (HASP: {hasp_hex}) expires "
        f"{_display_date(expiry)}. This reminder series occurs 14 days before, "
        "7 days before, and on the expiration date."
    )

    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "PRODID:-//CITE-HMS//cite-cli//EN",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:cite-{hasp_hex}-{expiry.isoformat()}-reminders@cite-hms",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART;VALUE=DATE:{first_reminder.strftime('%Y%m%d')}",
        f"DTEND;VALUE=DATE:{(first_reminder + timedelta(days=1)).strftime('%Y%m%d')}",
        "RRULE:FREQ=WEEKLY;COUNT=3",
        f"SUMMARY:{_escape_text(summary)}",
        f"DESCRIPTION:{_escape_text(description)}",
        f"ORGANIZER:mailto:{organizer}",
        *(f"ATTENDEE;RSVP=FALSE:mailto:{a}" for a in attendees),
        "STATUS:CONFIRMED",
        "TRANSP:TRANSPARENT",
        "SEQUENCE:0",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(_fold(ln) for ln in lines) + "\r\n"


def send_reminder_invites(station: str | None, hasp_hex: str, expiry: date) -> bool:
    """Email the .ics reminder invite to CITE_ALERT_TO.

    Returns True if delivered, False if not configured or SMTP failed.
    Never raises.
    """
    if not _is_configured():
        return False

    user = os.environ["CITE_ALERT_SMTP_USER"]
    password = os.environ["CITE_ALERT_SMTP_PASSWORD"]
    to_addrs = os.environ["CITE_ALERT_TO"]
    host = os.environ.get("CITE_ALERT_SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("CITE_ALERT_SMTP_PORT", "587"))
    from_addr = os.environ.get("CITE_ALERT_FROM", user)

    where = station or socket.gethostname()
    attendees = [a.strip() for a in to_addrs.split(",") if a.strip()]
    ics = build_reminder_ics(
        station=where,
        hasp_hex=hasp_hex,
        expiry=expiry,
        organizer=from_addr,
        attendees=attendees,
    )

    msg = EmailMessage()
    msg["Subject"] = (
        f"[cite-cli] Calendar reminders: NIS-Elements license expires "
        f"{expiry.isoformat()} on {where}"
    )
    msg["From"] = from_addr
    msg["To"] = to_addrs
    reminder_lines = "\n".join(
        f"- {_display_date(expiry - timedelta(days=offset))} — {label}"
        for offset, label in _REMINDER_OFFSETS
    )
    msg.set_content(
        f"Calendar reminders have been created for the NIS-Elements license "
        f"on {where}.\n\n"
        f"HASP: {hasp_hex}\n"
        f"Expiration date: {_display_date(expiry)}\n\n"
        f"The attached calendar invitation contains three weekly all-day "
        f"reminders:\n\n"
        f"{reminder_lines}\n"
    )
    msg.add_alternative(ics, subtype="calendar", params={"method": "REQUEST"})

    try:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(user, password)
            s.send_message(msg)
        return True
    except Exception:
        return False
