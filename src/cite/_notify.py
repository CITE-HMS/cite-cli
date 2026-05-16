"""Failure-notification email helper.

Sends an alert email when wrapped CLI commands fail unattended (e.g. from
Windows Task Scheduler). Reads SMTP credentials from env vars; if any
required var is unset, sending is a silent no-op so dev/test runs are
unaffected.

Env vars (set these in the .bat wrapper that runs `cite renew` / `cite clean`):

    CITE_ALERT_SMTP_USER     — required (the Gmail account that sends the email)
    CITE_ALERT_SMTP_PASSWORD — required (a Gmail App Password, NOT your login)
    CITE_ALERT_TO            — required; comma-separated recipient list
    CITE_ALERT_SMTP_HOST     — optional, default smtp.gmail.com
    CITE_ALERT_SMTP_PORT     — optional, default 587 (STARTTLS)
    CITE_ALERT_FROM          — optional; defaults to CITE_ALERT_SMTP_USER
"""

from __future__ import annotations

import os
import smtplib
import socket
import traceback
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cite._renew import RenewState

URGENCY_DAYS = 4


def _is_configured() -> bool:
    return all(
        os.environ.get(k)
        for k in ("CITE_ALERT_SMTP_USER", "CITE_ALERT_SMTP_PASSWORD", "CITE_ALERT_TO")
    )


def send_failure_email(command: str, error: BaseException) -> bool:
    """Send a failure alert. Returns True if delivered, False otherwise.

    Never raises: SMTP errors are swallowed so alerting can't mask the
    underlying command failure.
    """
    if not _is_configured():
        return False

    user = os.environ["CITE_ALERT_SMTP_USER"]
    password = os.environ["CITE_ALERT_SMTP_PASSWORD"]
    to_addrs = os.environ["CITE_ALERT_TO"]
    host = os.environ.get("CITE_ALERT_SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("CITE_ALERT_SMTP_PORT", "587"))
    from_addr = os.environ.get("CITE_ALERT_FROM", user)

    hostname = socket.gethostname()
    tb = "".join(traceback.format_exception(error))

    msg = EmailMessage()
    msg["Subject"] = f"[cite-cli] {command} failed on {hostname}"
    msg["From"] = from_addr
    msg["To"] = to_addrs
    msg.set_content(
        f"Command: cite {command}\n"
        f"Host:    {hostname}\n"
        f"Error:   {type(error).__name__}: {error}\n\n"
        f"Traceback:\n{tb}"
    )

    try:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(user, password)
            s.send_message(msg)
        return True
    except Exception:
        return False


def _hasp_id_hex(decimal_str: str) -> str:
    """Convert ACC's decimal HASP ID to the hex form Nikon's tools use."""
    try:
        return f"{int(decimal_str):08X}"
    except ValueError:
        return decimal_str


def send_urgency_alert(state: RenewState, days_remaining: int) -> bool:
    """Send the 'deadline approaching, no Nikon reply yet' email.

    Reuses the same SMTP config as send_failure_email. Returns True on
    delivery, False if not configured or SMTP failed. Never raises.
    """
    if not _is_configured():
        return False

    user = os.environ["CITE_ALERT_SMTP_USER"]
    password = os.environ["CITE_ALERT_SMTP_PASSWORD"]
    to_addrs = os.environ["CITE_ALERT_TO"]
    host = os.environ.get("CITE_ALERT_SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("CITE_ALERT_SMTP_PORT", "587"))
    from_addr = os.environ.get("CITE_ALERT_FROM", user)

    hostname = socket.gethostname()
    hasp_hex = _hasp_id_hex(state.hasp_id)
    now = datetime.now(tz=timezone.utc)
    submitted_age = (now - state.submitted_at).days

    if days_remaining < 0:
        days_phrase = f"already expired {-days_remaining} day(s) ago"
    elif days_remaining == 0:
        days_phrase = "expiring TODAY"
    else:
        days_phrase = f"expiring in {days_remaining} day(s)"

    msg = EmailMessage()
    msg["Subject"] = (
        f"[cite-cli] URGENT: NIS-Elements license {days_phrase}, "
        f"no Nikon reply yet on {hostname}"
    )
    msg["From"] = from_addr
    msg["To"] = to_addrs
    msg.set_content(
        f"URGENT: a NIS-Elements license renewal is overdue.\n"
        f"\n"
        f"Host:            {hostname}\n"
        f"HASP ID:         {hasp_hex} (decimal {state.hasp_id})\n"
        f"Expiration date: {state.expiration_date.isoformat()} "
        f"({days_phrase})\n"
        f"Renewal submitted: {state.submitted_at.isoformat()} "
        f"({submitted_age} day(s) ago)\n"
        f"Renewal endpoint:  {state.url}\n"
        f"\n"
        f"Action required:\n"
        f"  1. Check the citeathms@gmail.com inbox for a reply from\n"
        f"     Nikon containing a dealers/download.php?request=... link.\n"
        f"  2. If found, save the .v2c file and apply it manually on\n"
        f"     {hostname} via HASP Update (or `cite apply-update`).\n"
        f"  3. If no reply has arrived, contact Nikon support and quote\n"
        f"     the HASP ID above.\n"
    )

    try:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(user, password)
            s.send_message(msg)
        return True
    except Exception:
        return False
