"""IMAP polling for Nikon's renewal-reply emails.

Used by `cite apply-update` to find emails containing the Nikon
`dealers/download.php?request=...` link. Reuses the same Gmail App
Password as the outbound failure-alert flow (`CITE_ALERT_SMTP_USER` /
`CITE_ALERT_SMTP_PASSWORD`), so no new env vars to configure.

Stdlib only — no IMAP library beyond `imaplib`/`email`.
"""

from __future__ import annotations

import email
import email.utils
import imaplib
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import Message

LINK_RE = re.compile(
    r"https?://nis-e-update\.nikon-instruments\.jp/dealers/download\.php"
    r"\?request=([0-9a-fA-F]{32})"
)

DEFAULT_IMAP_HOST = "imap.gmail.com"
DEFAULT_IMAP_PORT = 993

# Mailboxes to search. Order matters only for dedup logging — we collect
# everything and dedup by Message-ID at the end.
SEARCH_MAILBOXES = ("INBOX", '"[Gmail]/All Mail"', '"[Gmail]/Spam"')


@dataclass(frozen=True)
class CandidateEmail:
    message_id: str  # RFC-822 Message-ID, used as the cache key
    sent_at: datetime
    sender: str
    download_url: str
    request_token: str  # the 32-hex blob from the URL


def _imap_credentials() -> tuple[str, str]:
    user = os.environ.get("CITE_ALERT_SMTP_USER")
    password = os.environ.get("CITE_ALERT_SMTP_PASSWORD")
    if not (user and password):
        raise RuntimeError(
            "IMAP polling requires CITE_ALERT_SMTP_USER and "
            "CITE_ALERT_SMTP_PASSWORD to be set (same Gmail App Password "
            "used for outbound failure alerts)."
        )
    return user, password


def _extract_link_from_msg(msg: Message) -> tuple[str, str] | None:
    """Return (download_url, request_token) if the message body contains
    the Nikon download link, else None.
    """
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        body: str | None = None
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            charset = part.get_content_charset() or "utf-8"
            try:
                body = payload.decode(charset, errors="replace")
            except LookupError:
                body = payload.decode("utf-8", errors="replace")
        if body is None:
            continue
        m = LINK_RE.search(body)
        if m:
            return m.group(0), m.group(1).lower()
    return None


def _parse_date(value: str | None) -> datetime:
    if not value:
        return datetime.now(tz=timezone.utc)
    try:
        dt: datetime = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return datetime.now(tz=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def find_candidate_emails(
    since: datetime,
    *,
    host: str | None = None,
    port: int | None = None,
) -> list[CandidateEmail]:
    """Return every email matching the Nikon download-link regex since
    `since`, across INBOX / All Mail / Spam, deduplicated by Message-ID.

    Raises RuntimeError if credentials aren't configured or IMAP login
    fails. Returns an empty list if no matches.
    """
    user, password = _imap_credentials()
    host = host or os.environ.get("CITE_GMAIL_IMAP_HOST", DEFAULT_IMAP_HOST)
    port = int(
        port
        if port is not None
        else os.environ.get("CITE_GMAIL_IMAP_PORT", DEFAULT_IMAP_PORT)
    )

    try:
        client = imaplib.IMAP4_SSL(host, port)
    except OSError as e:
        raise RuntimeError(f"Could not reach IMAP server {host}:{port}: {e}") from e

    try:
        try:
            client.login(user, password)
        except imaplib.IMAP4.error as e:
            raise RuntimeError(
                f"IMAP login as {user!r} failed: {e}. Check your Gmail App Password."
            ) from e

        # RFC 3501 SINCE has day granularity; widen by one day to be safe.
        since_for_imap = since.astimezone(timezone.utc) - timedelta(days=1)
        since_imap_str = since_for_imap.strftime("%d-%b-%Y")

        seen: dict[str, CandidateEmail] = {}
        for mailbox in SEARCH_MAILBOXES:
            try:
                status, _ = client.select(mailbox, readonly=True)
            except imaplib.IMAP4.error:
                continue
            if status != "OK":
                continue
            status, data = client.search(None, "SINCE", since_imap_str)
            if status != "OK" or not data or not data[0]:
                continue
            for uid in data[0].split():
                status, fetched = client.fetch(uid, "(RFC822)")
                if status != "OK" or not fetched:
                    continue
                raw = _extract_rfc822_bytes(fetched)
                if raw is None:
                    continue
                msg = email.message_from_bytes(raw)
                link = _extract_link_from_msg(msg)
                if link is None:
                    continue
                url, token = link
                sent_at = _parse_date(msg.get("Date"))
                if sent_at < since:
                    continue
                message_id = (msg.get("Message-ID") or "").strip()
                if not message_id:
                    message_id = f"<no-id-{token}-{uid.decode()}>"
                if message_id in seen:
                    continue
                seen[message_id] = CandidateEmail(
                    message_id=message_id,
                    sent_at=sent_at,
                    sender=(msg.get("From") or "").strip(),
                    download_url=url,
                    request_token=token,
                )
    finally:
        try:
            client.logout()
        except Exception:
            pass

    return sorted(seen.values(), key=lambda c: c.sent_at, reverse=True)


def _extract_rfc822_bytes(fetched: object) -> bytes | None:
    """Pull the RFC822 body bytes out of an imaplib fetch() result.

    imaplib returns a list of mixed (tuple, bytes) entries; the RFC822
    payload is the bytes element of a 2-tuple.
    """
    if not isinstance(fetched, list):
        return None
    for entry in fetched:
        if isinstance(entry, tuple) and len(entry) >= 2:
            body = entry[1]
            if isinstance(body, bytes):
                return body
    return None
