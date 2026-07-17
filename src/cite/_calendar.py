"""Google-Calendar reminders via iCalendar email invites.

When a license renewal is detected, `send_reminder_invites` emails a single
.ics invite (METHOD:REQUEST) to CITE_ALERT_TO containing three all-day
events: 14 days before, 7 days before, and on the new expiration date.
Gmail automatically adds invites addressed to the account to its Google
Calendar — no Google API or OAuth needed. Reuses the same SMTP env vars as
`cite._notify` (see that module's docstring).

All-day events are used (rather than VALARMs) because Google Calendar
ignores custom VALARMs on incoming invites; the account's default event
notifications still apply. Event UIDs are deterministic per
(HASP ID, expiry, offset), so a re-send updates the existing events instead
of duplicating them.
"""

from __future__ import annotations

import os
import smtplib
import socket
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage

from cite._notify import _is_configured

# (days before expiry, summary phrase) — titles follow the convention of the
# Apps Script this replaces, so the calendar stays consistent.
_REMINDER_OFFSETS: tuple[tuple[int, str], ...] = (
    (14, "expires in 14 days"),
    (7, "expires in 7 days"),
    (0, "EXPIRES TODAY"),
)


def _fold(line: str) -> str:
    """Fold a content line at 74 chars per RFC 5545 §3.1 (continuation
    lines start with a single space)."""
    if len(line) <= 74:
        return line
    parts = [line[:74]]
    rest = line[74:]
    while rest:
        parts.append(" " + rest[:73])
        rest = rest[73:]
    return "\r\n".join(parts)


def build_reminder_ics(
    station: str | None,
    hasp_hex: str,
    expiry: date,
    organizer: str,
    attendees: list[str],
) -> str:
    """Return a VCALENDAR (METHOD:REQUEST) with three all-day reminder events.

    Pure function — no I/O. CRLF line endings per RFC 5545.
    """
    where = station or "unknown station"
    dtstamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "PRODID:-//CITE-HMS//cite-cli//EN",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "METHOD:REQUEST",
    ]
    for offset, phrase in _REMINDER_OFFSETS:
        event_day = expiry - timedelta(days=offset)
        summary = f"⚠️ NIS-Elements license {phrase} — HASP {hasp_hex} ({where})"
        description = (
            f"NIS-Elements license on {where} (HASP: {hasp_hex}) "
            f"expires on {expiry.isoformat()}."
        )
        lines += [
            "BEGIN:VEVENT",
            f"UID:cite-{hasp_hex}-{expiry.isoformat()}-{offset}d@cite-hms",
            f"DTSTAMP:{dtstamp}",
            f"DTSTART;VALUE=DATE:{event_day.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{(event_day + timedelta(days=1)).strftime('%Y%m%d')}",
            f"SUMMARY:{summary}",
            f"DESCRIPTION:{description}",
            f"ORGANIZER:mailto:{organizer}",
            *(f"ATTENDEE;RSVP=FALSE:mailto:{a}" for a in attendees),
            "STATUS:CONFIRMED",
            "TRANSP:TRANSPARENT",
            "SEQUENCE:0",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
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
        f"[cite-cli] Calendar reminders: license expires "
        f"{expiry.isoformat()} on {where}"
    )
    msg["From"] = from_addr
    msg["To"] = to_addrs
    msg.set_content(
        f"Calendar invite for the NIS-Elements license on {where} "
        f"(HASP: {hasp_hex}), expiring {expiry.isoformat()}.\n"
        f"\n"
        f"Reminder events (14 days before, 7 days before, and day-of) are\n"
        f"attached as a calendar invite; Gmail adds them to Google Calendar\n"
        f"automatically.\n"
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
