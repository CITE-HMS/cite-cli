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
from email.message import EmailMessage


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
