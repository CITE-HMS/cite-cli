"""Tests for `cite apply-update` and its helpers."""

from __future__ import annotations

import subprocess
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import ClassVar

import pytest
import requests
from typer.testing import CliRunner

from cite import _email, _notify, _renew
from cite._email import find_candidate_emails
from cite._notify import send_urgency_alert
from cite._renew import (
    LicenseInfo,
    RenewState,
    apply_l2c,
    download_l2c,
    extract_haspid_from_l2c_content,
    extract_haspid_from_l2c_filename,
    load_checked_emails,
    save_checked_emails,
    save_renew_state,
)
from cite.cli import app

runner = CliRunner()


# --- Shared helpers / fixtures --------------------------------------------


def _make_email(
    *,
    message_id: str,
    date_hdr: str,
    body: str,
    sender: str = "ahus@lim.cz",
    html: bool = False,
) -> bytes:
    msg = EmailMessage()
    msg["Message-ID"] = message_id
    msg["Date"] = date_hdr
    msg["From"] = sender
    msg["To"] = "citeathms@gmail.com"
    msg["Subject"] = "License update"
    if html:
        msg.set_content("Plaintext fallback (no link here).")
        msg.add_alternative(body, subtype="html")
    else:
        msg.set_content(body)
    return msg.as_bytes()


class _FakeIMAP:
    """In-memory IMAP4_SSL replacement. Set `.mailboxes` (dict of name ->
    list[bytes]) before invoking find_candidate_emails."""

    instances: ClassVar[list[_FakeIMAP]] = []
    next_mailboxes: ClassVar[dict[str, list[bytes]]] = {}
    login_should_fail: ClassVar[bool] = False

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.logged_out = False
        self.mailboxes = dict(_FakeIMAP.next_mailboxes)
        self._current: list[bytes] = []
        _FakeIMAP.instances.append(self)

    def login(self, user: str, password: str) -> None:
        if _FakeIMAP.login_should_fail:
            import imaplib

            raise imaplib.IMAP4.error("authentication failed")
        self.user = user
        self.password = password

    def select(self, mailbox, readonly=False):  # type: ignore[no-untyped-def]
        # IMAP mailbox names come quoted in our SEARCH_MAILBOXES tuple
        # (e.g. '"[Gmail]/All Mail"'); strip surrounding quotes for lookup.
        key = mailbox.strip('"')
        if key in self.mailboxes:
            self._current = self.mailboxes[key]
            return "OK", [b""]
        return "NO", [b"no such mailbox"]

    def search(self, charset, *criteria):  # type: ignore[no-untyped-def]
        # Return every message UID in the currently-selected mailbox.
        uids = b" ".join(str(i + 1).encode() for i in range(len(self._current)))
        return "OK", [uids]

    def fetch(self, uid, what):  # type: ignore[no-untyped-def]
        idx = int(uid) - 1
        if 0 <= idx < len(self._current):
            raw = self._current[idx]
            return "OK", [(b"1 (RFC822 {%d}" % len(raw), raw)]
        return "NO", [b""]

    def logout(self) -> None:
        self.logged_out = True


@pytest.fixture()
def fake_imap(monkeypatch):
    _FakeIMAP.instances = []
    _FakeIMAP.next_mailboxes = {}
    _FakeIMAP.login_should_fail = False
    monkeypatch.setattr(_email.imaplib, "IMAP4_SSL", _FakeIMAP)
    return _FakeIMAP


@pytest.fixture()
def alert_creds(monkeypatch):
    """Set alert/IMAP credentials; tests can still override _FakeIMAP behavior."""
    monkeypatch.setenv("CITE_ALERT_SMTP_USER", "ops@example.com")
    monkeypatch.setenv("CITE_ALERT_SMTP_PASSWORD", "app-password")
    monkeypatch.setenv("CITE_ALERT_TO", "ops@example.com")


@pytest.fixture()
def tmp_checked_emails(tmp_path: Path, monkeypatch) -> Path:
    p = tmp_path / "checked_emails.json"
    monkeypatch.setattr(_renew, "CHECKED_EMAILS_PATH", p)
    return p


@pytest.fixture()
def tmp_incoming(tmp_path: Path, monkeypatch) -> Path:
    incoming = tmp_path / "incoming"
    applied = tmp_path / "applied"
    received = tmp_path / "received_update.l2c"
    monkeypatch.setattr(_renew, "INCOMING_DIR", incoming)
    monkeypatch.setattr(_renew, "APPLIED_L2C_DIR", applied)
    monkeypatch.setattr(_renew, "RECEIVED_L2C_PATH", received)
    return incoming


# --- _email.find_candidate_emails ------------------------------------------


_VALID_BODY = (
    "Hello,\nYour update is ready:\n"
    "https://nis-e-update.nikon-instruments.jp/dealers/download.php?"
    "request=e556d5faf993ece4b7eaaa56fa5be2ad\n"
    "Best regards.\n"
)


def test_find_candidate_emails_returns_match(fake_imap, alert_creds) -> None:
    fake_imap.next_mailboxes = {
        "INBOX": [
            _make_email(
                message_id="<abc@example>",
                date_hdr="Fri, 15 May 2026 10:00:00 +0000",
                body=_VALID_BODY,
            )
        ]
    }
    since = datetime(2026, 5, 14, tzinfo=timezone.utc)
    results = find_candidate_emails(since)
    assert len(results) == 1
    assert results[0].message_id == "<abc@example>"
    assert results[0].request_token == "e556d5faf993ece4b7eaaa56fa5be2ad"
    assert "download.php?request=" in results[0].download_url


def test_find_candidate_emails_dedups_across_mailboxes(fake_imap, alert_creds) -> None:
    same = _make_email(
        message_id="<dup@example>",
        date_hdr="Fri, 15 May 2026 10:00:00 +0000",
        body=_VALID_BODY,
    )
    fake_imap.next_mailboxes = {
        "INBOX": [same],
        "[Gmail]/All Mail": [same],
    }
    since = datetime(2026, 5, 14, tzinfo=timezone.utc)
    results = find_candidate_emails(since)
    assert len(results) == 1
    assert results[0].message_id == "<dup@example>"


def test_find_candidate_emails_empty_when_no_match(fake_imap, alert_creds) -> None:
    fake_imap.next_mailboxes = {
        "INBOX": [
            _make_email(
                message_id="<noise@example>",
                date_hdr="Fri, 15 May 2026 10:00:00 +0000",
                body="Just a regular email. No download link.",
            )
        ]
    }
    since = datetime(2026, 5, 14, tzinfo=timezone.utc)
    assert find_candidate_emails(since) == []


def test_find_candidate_emails_unconfigured_raises(fake_imap) -> None:
    # No alert_creds fixture — env vars absent (autouse _strip_alert_env).
    with pytest.raises(RuntimeError, match="CITE_ALERT_SMTP_USER"):
        find_candidate_emails(datetime(2026, 5, 14, tzinfo=timezone.utc))


def test_find_candidate_emails_login_failure_raises(fake_imap, alert_creds) -> None:
    fake_imap.login_should_fail = True
    with pytest.raises(RuntimeError, match="IMAP login"):
        find_candidate_emails(datetime(2026, 5, 14, tzinfo=timezone.utc))


def test_find_candidate_emails_returns_newest_first(fake_imap, alert_creds) -> None:
    fake_imap.next_mailboxes = {
        "INBOX": [
            _make_email(
                message_id="<older@example>",
                date_hdr="Wed, 13 May 2026 10:00:00 +0000",
                body=_VALID_BODY.replace(
                    "e556d5faf993ece4b7eaaa56fa5be2ad",
                    "aaaa" * 8,
                ),
            ),
            _make_email(
                message_id="<newer@example>",
                date_hdr="Fri, 15 May 2026 10:00:00 +0000",
                body=_VALID_BODY,
            ),
        ]
    }
    since = datetime(2026, 5, 12, tzinfo=timezone.utc)
    results = find_candidate_emails(since)
    assert [r.message_id for r in results] == ["<newer@example>", "<older@example>"]


# --- download_l2c ---------------------------------------------------------


class _Resp:
    """Minimal stand-in for a requests.Response."""

    def __init__(self, content: bytes = b"data", filename: str | None = None) -> None:
        self.content = content
        self.headers: dict[str, str] = {}
        if filename:
            self.headers["Content-Disposition"] = f'attachment; filename="{filename}"'

    def raise_for_status(self) -> None:
        return None


def test_download_l2c_uses_filename_from_header(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: _Resp(content=b"fake l2c bytes", filename="520D66C9.l2c"),
    )
    path, name = download_l2c("http://example/x?request=abc", tmp_path)
    assert name == "520D66C9.l2c"
    assert path == tmp_path / "520D66C9.l2c"
    assert path.read_bytes() == b"fake l2c bytes"


def test_download_l2c_falls_back_to_url_token(tmp_path: Path, monkeypatch) -> None:
    """When Content-Disposition is missing, derive a name from the URL token."""
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: _Resp(content=b"bytes"),  # no filename
    )
    path, name = download_l2c("http://example/x?request=deadbeefcafebabe", tmp_path)
    assert name.endswith(".l2c")
    assert path.exists()


def test_download_l2c_http_error_raises(tmp_path: Path, monkeypatch) -> None:
    def _boom(*a, **k):
        raise requests.RequestException("nope")

    monkeypatch.setattr(requests, "get", _boom)
    with pytest.raises(RuntimeError, match="Failed to download"):
        download_l2c("http://example/x?request=z", tmp_path)


def test_download_l2c_empty_body_raises(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(content=b""))
    with pytest.raises(RuntimeError, match="empty"):
        download_l2c("http://example/x?request=z", tmp_path)


# --- extract_haspid_from_l2c_filename --------------------------------------


def test_extract_haspid_from_filename_hex() -> None:
    """The primary path: Nikon names files <HASPID_hex>.l2c."""
    # 520D66C9 hex == 1376609993 decimal (matches the user's sample file).
    assert extract_haspid_from_l2c_filename("520D66C9.l2c") == "1376609993"


def test_extract_haspid_from_filename_lowercase() -> None:
    assert extract_haspid_from_l2c_filename("09882a98.l2c") == "159918744"


def test_extract_haspid_from_filename_strips_dir() -> None:
    assert extract_haspid_from_l2c_filename("/some/path/520D66C9.l2c") == "1376609993"


def test_extract_haspid_from_filename_rejects_non_l2c() -> None:
    with pytest.raises(RuntimeError, match="Cannot parse HASP ID"):
        extract_haspid_from_l2c_filename("notanlc2file.txt")


def test_extract_haspid_from_filename_rejects_wrong_length() -> None:
    with pytest.raises(RuntimeError, match="Cannot parse HASP ID"):
        extract_haspid_from_l2c_filename("1234.l2c")


# --- extract_haspid_from_l2c_content (fallback parser) ---------------------


# Nikon's real .l2c format (from a sample file), used as the canonical fixture.
_REAL_L2C_FORMAT = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<variant version="1.0">\n'
    b'    <HASPUpdate_1.0 runtype="CLxListVariant">\n'
    b'        <Key0 runtype="CLxListVariant">\n'
    b'            <HASPID runtype="CLxStringW" value="{haspid}"/>\n'
    b'            <Update0 runtype="CLxStringW" value="..."/>\n'
    b"        </Key0>\n"
    b"    </HASPUpdate_1.0>\n"
    b"</variant>\n"
)


def test_extract_haspid_real_nikon_format(tmp_path: Path) -> None:
    """The actual format Nikon's renewal server produces (from a real sample)."""
    p = tmp_path / "test.l2c"
    p.write_bytes(_REAL_L2C_FORMAT.replace(b"{haspid}", b"520D66C9"))
    # 520D66C9 hex == 1376609993 decimal
    assert extract_haspid_from_l2c_content(p) == "1376609993"


def test_extract_haspid_element_form(tmp_path: Path) -> None:
    blob = b"<v2c><hasp_id>123456789</hasp_id></v2c>"
    p = tmp_path / "test.l2c"
    p.write_bytes(blob)
    assert extract_haspid_from_l2c_content(p) == "123456789"


def test_extract_haspid_hasp_element(tmp_path: Path) -> None:
    blob = b'<hasp id="09882A98"><features/></hasp>'
    p = tmp_path / "test.l2c"
    p.write_bytes(blob)
    assert extract_haspid_from_l2c_content(p) == "159918744"


def test_extract_haspid_utf16_encoding(tmp_path: Path) -> None:
    """Defense-in-depth: handle UTF-16 in case Nikon ever changes encodings."""
    text = '<HASPID runtype="CLxStringW" value="09882A98"/>'
    p = tmp_path / "test.l2c"
    p.write_bytes(text.encode("utf-16-le"))
    assert extract_haspid_from_l2c_content(p) == "159918744"


def test_extract_haspid_missing_raises(tmp_path: Path) -> None:
    p = tmp_path / "test.l2c"
    p.write_bytes(b"<not a hasp file/>")
    with pytest.raises(RuntimeError, match="Could not locate HASPID"):
        extract_haspid_from_l2c_content(p)


# --- checked_emails cache --------------------------------------------------


def test_checked_emails_roundtrip(tmp_checked_emails: Path) -> None:
    now = datetime.now(timezone.utc)
    cache = {
        "<a@x>": {"haspid": "111", "checked_at": now.isoformat()},
        "<b@x>": {"haspid": "222", "checked_at": now.isoformat()},
    }
    save_checked_emails(cache)
    loaded = load_checked_emails()
    assert set(loaded.keys()) == {"<a@x>", "<b@x>"}
    assert loaded["<a@x>"]["haspid"] == "111"


def test_checked_emails_prunes_old(tmp_checked_emails: Path) -> None:
    now = datetime.now(timezone.utc)
    very_old = (now - timedelta(days=200)).isoformat()
    fresh = now.isoformat()
    cache = {
        "<old@x>": {"haspid": "111", "checked_at": very_old},
        "<new@x>": {"haspid": "222", "checked_at": fresh},
    }
    save_checked_emails(cache, now=now)
    loaded = load_checked_emails()
    assert set(loaded.keys()) == {"<new@x>"}


def test_checked_emails_corrupt_returns_empty(tmp_checked_emails: Path) -> None:
    tmp_checked_emails.write_text("{not json", encoding="utf-8")
    assert load_checked_emails() == {}


def test_checked_emails_missing_returns_empty(tmp_checked_emails: Path) -> None:
    assert load_checked_emails() == {}


# --- apply_l2c -------------------------------------------------------------


def _make_l2c(p: Path, haspid: str = "09882A98") -> Path:
    p.write_bytes(_REAL_L2C_FORMAT.replace(b"{haspid}", haspid.encode("ascii")))
    return p


def test_apply_l2c_success(tmp_path: Path, monkeypatch) -> None:
    fake_exe = tmp_path / "nis_hasp_update.exe"
    fake_exe.write_bytes(b"")
    l2c = _make_l2c(tmp_path / "in.l2c")

    before = LicenseInfo(expiration_date=date(2026, 6, 5), hasp_id="159918744")
    after = LicenseInfo(expiration_date=date(2026, 9, 5), hasp_id="159918744")

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_renew.subprocess, "run", fake_run)
    monkeypatch.setattr(_renew, "get_license_info", lambda: after)

    result = apply_l2c(l2c, rus_exe=fake_exe, before=before)
    assert result == after
    assert captured["cmd"] == [str(fake_exe), "-a", str(l2c)]


def test_apply_l2c_rejection_marker_detected(tmp_path: Path, monkeypatch) -> None:
    fake_exe = tmp_path / "nis_hasp_update.exe"
    fake_exe.write_bytes(b"")
    l2c = _make_l2c(tmp_path / "in.l2c")
    before = LicenseInfo(expiration_date=date(2026, 6, 5), hasp_id="159918744")

    rejection = "Failed to apply a v2c update due to HL key type mismatch"

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            returncode=0,
            stdout="",
            stderr=rejection,
        )

    monkeypatch.setattr(_renew.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="HL key type mismatch"):
        apply_l2c(l2c, rus_exe=fake_exe, before=before)


def test_apply_l2c_exit_zero_no_advance_raises(tmp_path: Path, monkeypatch) -> None:
    fake_exe = tmp_path / "nis_hasp_update.exe"
    fake_exe.write_bytes(b"")
    l2c = _make_l2c(tmp_path / "in.l2c")
    same = LicenseInfo(expiration_date=date(2026, 6, 5), hasp_id="159918744")

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_renew.subprocess, "run", fake_run)
    monkeypatch.setattr(_renew, "get_license_info", lambda: same)

    with pytest.raises(RuntimeError, match=r"expiration.*did not advance"):
        apply_l2c(l2c, rus_exe=fake_exe, before=same)


def test_apply_l2c_no_rus_raises(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(_renew, "RUS_EXE_GLOB_PATTERNS", ())
    monkeypatch.delenv("CITE_RUS_EXE", raising=False)
    l2c = _make_l2c(tmp_path / "in.l2c")
    with pytest.raises(RuntimeError, match="Could not locate nis_hasp_update"):
        apply_l2c(l2c)


# --- send_urgency_alert ----------------------------------------------------


def _make_state(days_until_exp: int) -> RenewState:
    return RenewState(
        expiration_date=date.today() + timedelta(days=days_until_exp),
        hasp_id="159918744",
        submitted_at=datetime.now(timezone.utc) - timedelta(days=12),
        url="https://nis-e-update.nikon-instruments.jp/dealers/",
    )


class _FakeSMTP:
    instances: ClassVar[list[_FakeSMTP]] = []

    def __init__(self, host, port, timeout):  # type: ignore[no-untyped-def]
        self.sent: EmailMessage | None = None
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def starttls(self) -> None:
        pass

    def login(self, u, p) -> None:
        pass

    def send_message(self, msg) -> None:  # type: ignore[no-untyped-def]
        self.sent = msg


@pytest.fixture()
def fake_smtp(monkeypatch):
    _FakeSMTP.instances = []
    monkeypatch.setattr(_notify.smtplib, "SMTP", _FakeSMTP)
    return _FakeSMTP


def test_send_urgency_alert_sends(fake_smtp, alert_creds) -> None:
    state = _make_state(days_until_exp=3)
    assert send_urgency_alert(state, days_remaining=3) is True
    sent = fake_smtp.instances[0].sent
    assert sent is not None
    assert "URGENT" in sent["Subject"]
    body = sent.get_content()
    assert "09882A98" in body  # hex form
    assert "159918744" in body  # decimal form too


def test_send_urgency_alert_expired(fake_smtp, alert_creds) -> None:
    state = _make_state(days_until_exp=-2)
    assert send_urgency_alert(state, days_remaining=-2) is True
    sent = fake_smtp.instances[0].sent
    assert sent is not None
    text = sent["Subject"] + "\n" + sent.get_content()
    assert "already expired" in text


def test_send_urgency_alert_unconfigured_noop(fake_smtp) -> None:
    state = _make_state(days_until_exp=3)
    assert send_urgency_alert(state, days_remaining=3) is False
    assert fake_smtp.instances == []


# --- CLI integration -------------------------------------------------------


def _invoke_apply_update():
    return runner.invoke(app, ["apply-update"])


def _setup_pending_state(
    monkeypatch,
    tmp_state_path: Path,
    days_until_exp: int = 10,
) -> RenewState:
    state = RenewState(
        expiration_date=date.today() + timedelta(days=days_until_exp),
        hasp_id="159918744",
        submitted_at=datetime.now(timezone.utc) - timedelta(days=5),
        url="https://nis-e-update.nikon-instruments.jp/dealers/",
    )
    save_renew_state(state)
    return state


def test_cli_apply_update_no_state(fake_imap, tmp_state_path: Path) -> None:
    """With no state file, command exits 0 cleanly and never opens IMAP."""
    result = _invoke_apply_update()
    assert result.exit_code == 0, result.output
    assert "Nothing to apply" in result.output
    assert fake_imap.instances == []


def test_cli_apply_update_no_candidates(
    fake_imap,
    alert_creds,
    monkeypatch,
    tmp_state_path: Path,
    tmp_checked_emails: Path,
    tmp_incoming: Path,
) -> None:
    _setup_pending_state(monkeypatch, tmp_state_path, days_until_exp=10)
    fake_imap.next_mailboxes = {"INBOX": []}

    result = _invoke_apply_update()
    assert result.exit_code == 0, result.output
    assert "No Nikon reply" in result.output


def _fake_response(haspid_hex: str) -> _Resp:
    """Build a fake .l2c response: filename header drives HASP ID."""
    return _Resp(
        content=_REAL_L2C_FORMAT.replace(b"{haspid}", haspid_hex.encode("ascii")),
        filename=f"{haspid_hex}.l2c",
    )


def test_cli_apply_update_matches_and_applies(
    fake_imap,
    fake_smtp,
    alert_creds,
    monkeypatch,
    tmp_state_path: Path,
    tmp_checked_emails: Path,
    tmp_incoming: Path,
) -> None:
    """One matching email out of two; the right .l2c is applied, state cleaned up."""
    state = _setup_pending_state(monkeypatch, tmp_state_path, days_until_exp=10)

    fake_imap.next_mailboxes = {
        "INBOX": [
            _make_email(
                message_id="<other-pc-1@x>",
                date_hdr="Thu, 14 May 2026 10:00:00 +0000",
                body=_VALID_BODY.replace(
                    "e556d5faf993ece4b7eaaa56fa5be2ad",
                    "1" * 32,
                ),
            ),
            _make_email(
                message_id="<ours@x>",
                date_hdr="Fri, 15 May 2026 10:00:00 +0000",
                body=_VALID_BODY.replace(
                    "e556d5faf993ece4b7eaaa56fa5be2ad",
                    "2" * 32,
                ),
            ),
        ]
    }

    # Map each request_token to a fake .l2c whose filename carries the HASPID.
    haspid_hex_by_token = {
        "1" * 32: "AAAA1111",  # decimal 2863316753 — NOT ours
        "2" * 32: "09882A98",  # decimal 159918744 — OURS
    }

    def fake_get(url, **kwargs):
        token = url.rsplit("=", 1)[-1]
        return _fake_response(haspid_hex_by_token[token])

    monkeypatch.setattr(requests, "get", fake_get)

    # Mock subprocess apply.
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_renew.subprocess, "run", fake_run)

    # Stub get_license_info: before = state's exp, after = +90 days.
    before = LicenseInfo(expiration_date=state.expiration_date, hasp_id=state.hasp_id)
    after = LicenseInfo(
        expiration_date=state.expiration_date + timedelta(days=90),
        hasp_id=state.hasp_id,
    )
    calls = {"n": 0}

    def fake_info():
        calls["n"] += 1
        return before if calls["n"] == 1 else after

    monkeypatch.setattr(_renew, "get_license_info", fake_info)

    # Need a discoverable RUS exe.
    fake_exe = tmp_state_path.parent / "nis_hasp_update.exe"
    fake_exe.write_bytes(b"")
    monkeypatch.setenv("CITE_RUS_EXE", str(fake_exe))

    result = _invoke_apply_update()
    assert result.exit_code == 0, result.output
    assert "Applied. Expiration" in result.output
    assert "Cycle complete" in result.output

    # State file must be gone.
    assert not tmp_state_path.exists()

    # The applied .l2c was archived.
    archives = list((_renew.APPLIED_L2C_DIR).glob("09882A98_*.l2c"))
    assert len(archives) == 1

    # The cache records both Message-IDs (so we won't redownload).
    cache = load_checked_emails()
    assert "<ours@x>" in cache
    assert "<other-pc-1@x>" in cache
    assert cache["<other-pc-1@x>"]["haspid"] != state.hasp_id


def test_cli_apply_update_no_match_in_urgency_window(
    fake_imap,
    fake_smtp,
    alert_creds,
    monkeypatch,
    tmp_state_path: Path,
    tmp_checked_emails: Path,
    tmp_incoming: Path,
) -> None:
    """No matching candidate AND < 4 days to expiry → URGENT email dispatched."""
    _setup_pending_state(monkeypatch, tmp_state_path, days_until_exp=3)
    fake_imap.next_mailboxes = {
        "INBOX": [
            _make_email(
                message_id="<not-ours@x>",
                date_hdr="Fri, 15 May 2026 10:00:00 +0000",
                body=_VALID_BODY,
            )
        ]
    }

    def fake_get(url, **kwargs):
        return _fake_response("AAAA1111")  # different HASPID

    monkeypatch.setattr(requests, "get", fake_get)

    result = _invoke_apply_update()
    assert result.exit_code == 0, result.output
    assert "No reply for this dongle yet" in result.output
    assert "URGENT alert email sent" in result.output
    sent = fake_smtp.instances[0].sent
    assert sent is not None
    assert "URGENT" in sent["Subject"]


def test_cli_apply_update_no_match_outside_urgency_window(
    fake_imap,
    fake_smtp,
    alert_creds,
    monkeypatch,
    tmp_state_path: Path,
    tmp_checked_emails: Path,
    tmp_incoming: Path,
) -> None:
    """No matching candidate, plenty of days left → no URGENT email."""
    _setup_pending_state(monkeypatch, tmp_state_path, days_until_exp=20)
    fake_imap.next_mailboxes = {"INBOX": []}

    result = _invoke_apply_update()
    assert result.exit_code == 0, result.output
    assert "URGENT" not in result.output
    assert fake_smtp.instances == []


def test_cli_apply_update_cache_skips_known_other_pc(
    fake_imap,
    alert_creds,
    monkeypatch,
    tmp_state_path: Path,
    tmp_checked_emails: Path,
    tmp_incoming: Path,
) -> None:
    """A candidate whose Message-ID is already in the cache as another PC's
    HASPID must NOT be re-downloaded."""
    _setup_pending_state(monkeypatch, tmp_state_path, days_until_exp=20)
    # Pre-populate cache with a not-ours entry.
    save_checked_emails(
        {
            "<not-ours@x>": {
                "haspid": "AAAA1111",
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }
        }
    )
    fake_imap.next_mailboxes = {
        "INBOX": [
            _make_email(
                message_id="<not-ours@x>",
                date_hdr="Fri, 15 May 2026 10:00:00 +0000",
                body=_VALID_BODY,
            )
        ]
    }

    download_calls = {"n": 0}

    def boom_if_called(*a, **k):
        download_calls["n"] += 1
        raise AssertionError("should not have downloaded a cached candidate")

    monkeypatch.setattr(requests, "get", boom_if_called)

    result = _invoke_apply_update()
    assert result.exit_code == 0, result.output
    assert download_calls["n"] == 0
    assert "No reply for this dongle yet" in result.output


# --- cite apply-update --dry-run -------------------------------------------


def test_cli_apply_update_dry_run_no_state(
    fake_imap,
    alert_creds,
    monkeypatch,
    tmp_state_path: Path,
) -> None:
    """No state file is fine in dry-run — runs in pure-diagnostic mode."""
    fake_imap.next_mailboxes = {
        "INBOX": [
            _make_email(
                message_id="<x@y>",
                date_hdr="Fri, 15 May 2026 10:00:00 +0000",
                body=_VALID_BODY,
            )
        ]
    }
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: _fake_response("520D66C9"),
    )

    result = runner.invoke(app, ["apply-update", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "DRY RUN" in result.output
    assert "pure-diagnostic mode" in result.output
    assert "520D66C9.l2c" in result.output
    assert "1376609993" in result.output  # decimal HASP ID
    assert "DRY RUN complete" in result.output


def test_cli_apply_update_dry_run_marks_match(
    fake_imap,
    alert_creds,
    monkeypatch,
    tmp_state_path: Path,
) -> None:
    """With a state file, dry-run identifies the matching candidate."""
    state = _setup_pending_state(monkeypatch, tmp_state_path)
    # state.hasp_id is "159918744" decimal = 09882A98 hex.
    fake_imap.next_mailboxes = {
        "INBOX": [
            _make_email(
                message_id="<ours@x>",
                date_hdr="Fri, 15 May 2026 10:00:00 +0000",
                body=_VALID_BODY,
            )
        ]
    }
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: _fake_response("09882A98"),  # matches state.hasp_id
    )

    result = runner.invoke(app, ["apply-update", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Match:    YES" in result.output
    # State file must be untouched.
    assert tmp_state_path.exists()
    assert state.hasp_id == "159918744"  # sanity
