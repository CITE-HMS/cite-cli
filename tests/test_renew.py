import hashlib
import json
import re
import socket
import threading
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cite import _renew
from cite._renew import (
    MOCK_C2L_PATH,
    URL_ALIASES,
    LicenseInfo,
    RenewState,
    RenewTarget,
    get_license_info,
    load_renew_state,
    resolve_c2l_file,
    resolve_url,
    save_renew_state,
    should_renew,
    submit_license_form,
)
from cite.cli import app

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


_MONTHS = {
    1: "Jan",
    2: "Feb",
    3: "Mar",
    4: "Apr",
    5: "May",
    6: "Jun",
    7: "Jul",
    8: "Aug",
    9: "Sep",
    10: "Oct",
    11: "Nov",
    12: "Dec",
}


def _lic_exp(d: date) -> str:
    """Build a `lic` value mimicking ACC's real output for an expiration date."""
    return (
        f"Expiration Date<br><nobr>&nbsp;"
        f"Mon {_MONTHS[d.month]} {d.day}, {d.year} 19:55</nobr>"
    )


def _acc_feed(*features: dict[str, str]) -> bytes:
    """Build a pseudo-JSON features-feed body the way ACC returns it."""
    body = ["/*JSON:features*/", ""]
    body.extend(json.dumps(f) + "," for f in features)
    body.append('{"fhaspid":"0","ffea":"0","cnt":"' + str(len(features)) + '"}')
    body.append("/* <admin_status><code>0</code></admin_status> */")
    return "\n".join(body).encode("utf-8")


class _ACCHandler(BaseHTTPRequestHandler):
    """Serve a canned XML body for /_int_/devices.html. Body set per-fixture."""

    xml_body: bytes = b"<root/>"
    status: int = 200

    def do_GET(self) -> None:
        self.send_response(self.status)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(self.xml_body)))
        self.end_headers()
        self.wfile.write(self.xml_body)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture()
def mock_acc(monkeypatch):
    """Spin up a stub ACC; tests set `_ACCHandler.xml_body` to control the response."""
    _ACCHandler.xml_body = b"<root/>"
    _ACCHandler.status = 200
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ACCHandler)
    host, port = server.server_address[:2]
    url = f"http://{host}:{port}/_int_/tab_feat.html"
    monkeypatch.setattr(_renew, "ACC_URL", url)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _ACCHandler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# --- should_renew unit tests ---


@pytest.mark.parametrize(
    "delta_days, expected",
    [
        (-1, False),  # already expired
        (0, True),  # expires today
        (1, True),  # tomorrow
        (14, True),  # boundary
        (15, False),  # one day past window
        (30, False),  # far future
    ],
)
def test_should_renew(delta_days: int, expected: bool) -> None:
    exp = date.today() + timedelta(days=delta_days)
    assert should_renew(exp, days_before=14) is expected


def test_resolve_c2l_file_mock_sentinel() -> None:
    assert resolve_c2l_file("mock") == MOCK_C2L_PATH
    assert MOCK_C2L_PATH.is_file()


def test_resolve_c2l_file_real_path(c2l_file: Path) -> None:
    assert resolve_c2l_file(c2l_file) == c2l_file.resolve()
    assert resolve_c2l_file(str(c2l_file)) == c2l_file.resolve()


def test_resolve_c2l_file_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        resolve_c2l_file(tmp_path / "nope.c2l")


def test_resolve_url_aliases() -> None:
    assert resolve_url("test") == "http://127.0.0.1:8765/"
    assert resolve_url("nikon") == "https://nis-e-update.nikon-instruments.jp/dealers/"
    assert resolve_url(RenewTarget.test) == "http://127.0.0.1:8765/"
    assert resolve_url(RenewTarget.nikon) == (
        "https://nis-e-update.nikon-instruments.jp/dealers/"
    )


# --- submit_license_form end-to-end against the mock ---


def test_submit_license_form_posts_all_fields(mock_server, c2l_file: Path) -> None:
    resp = submit_license_form(
        url=mock_server["url"],
        email="alice@example.com",
        full_name="Alice Example",
        c2l_file=c2l_file,
        note="hello from test",
    )
    assert resp.status_code == 200
    assert "Submission received" in resp.text

    log = mock_server["log_path"].read_text(encoding="utf-8")
    expected_sha = hashlib.sha256(c2l_file.read_bytes()).hexdigest()
    assert "alice@example.com" in log
    assert "Alice Example" in log
    assert "hello from test" in log
    assert "'Send'" in log  # the sendMe field
    assert expected_sha in log
    assert "license_request.c2l" in log


def test_submit_license_form_missing_c2l_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        submit_license_form(
            url="http://127.0.0.1:1",
            email="x@y.z",
            full_name="x",
            c2l_file=tmp_path / "does-not-exist.c2l",
            note="",
        )


# --- CLI tests ---


def _far_future() -> str:
    return (date.today() + timedelta(days=365)).strftime("%Y-%m-%d")


def _near() -> str:
    return (date.today() + timedelta(days=3)).strftime("%Y-%m-%d")


def test_cli_renew_requires_url(c2l_file: Path) -> None:
    result = runner.invoke(
        app,
        [
            "renew",
            "--email",
            "me@example.com",
            "--full-name",
            "Me",
            "--c2l-file",
            str(c2l_file),
            "--expires",
            _near(),
        ],
    )
    assert result.exit_code != 0
    # Strip ANSI escape codes: rich-formatted error output on some CI envs
    # wraps each character individually so the literal substring won't appear.
    assert "--url" in _ANSI_RE.sub("", result.output)


def test_cli_renew_rejects_unknown_url(c2l_file: Path) -> None:
    result = runner.invoke(
        app,
        [
            "renew",
            "--email",
            "me@example.com",
            "--full-name",
            "Me",
            "--c2l-file",
            str(c2l_file),
            "--url",
            "http://example.com/",
            "--expires",
            _near(),
        ],
    )
    assert result.exit_code != 0


def test_cli_renew_no_renewal_needed(c2l_file: Path) -> None:
    result = runner.invoke(
        app,
        [
            "renew",
            "--email",
            "me@example.com",
            "--full-name",
            "Me",
            "--c2l-file",
            str(c2l_file),
            "--url",
            "test",
            "--expires",
            _far_future(),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "No renewal needed" in result.output


def test_cli_renew_dry_run(c2l_file: Path) -> None:
    result = runner.invoke(
        app,
        [
            "renew",
            "--email",
            "me@example.com",
            "--full-name",
            "Me",
            "--c2l-file",
            str(c2l_file),
            "--url",
            "test",
            "--expires",
            _near(),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Would submit" in result.output
    assert "me@example.com" in result.output


def test_cli_renew_end_to_end(mock_server, c2l_file: Path) -> None:
    result = runner.invoke(
        app,
        [
            "renew",
            "--email",
            "cli@example.com",
            "--full-name",
            "CLI User",
            "--c2l-file",
            str(c2l_file),
            "--url",
            "test",
            "--expires",
            _near(),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Submitted. HTTP 200" in result.output

    log = mock_server["log_path"].read_text(encoding="utf-8")
    assert "cli@example.com" in log
    assert "CLI User" in log


def test_cli_renew_with_c2l_mock_sentinel(mock_server) -> None:
    result = runner.invoke(
        app,
        [
            "renew",
            "--email",
            "me@example.com",
            "--full-name",
            "Me",
            "--c2l-file",
            "mock",
            "--url",
            "test",
            "--expires",
            _near(),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Submitted. HTTP 200" in result.output

    log = mock_server["log_path"].read_text(encoding="utf-8")
    assert "mock.c2l" in log


def test_cli_renew_missing_c2l_exits_nonzero(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "renew",
            "--email",
            "me@example.com",
            "--full-name",
            "Me",
            "--c2l-file",
            str(tmp_path / "nope.c2l"),
            "--url",
            "test",
            "--expires",
            _near(),
        ],
    )
    assert result.exit_code != 0
    assert "C2L file not found" in result.output


def _free_port() -> int:
    """Find a free TCP port on 127.0.0.1 (small race window but fine for tests)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_cli_renew_auto_starts_mock_when_port_free(
    c2l_file: Path, monkeypatch, tmp_path: Path
) -> None:
    """With --url test and nothing listening, renew should spin up the mock itself."""
    port = _free_port()
    monkeypatch.setitem(
        URL_ALIASES, RenewTarget.test.value, f"http://127.0.0.1:{port}/"
    )
    monkeypatch.chdir(tmp_path)  # submissions.log lands in CWD

    result = runner.invoke(
        app,
        [
            "renew",
            "--email",
            "auto@example.com",
            "--full-name",
            "Auto User",
            "--c2l-file",
            str(c2l_file),
            "--url",
            "test",
            "--expires",
            _near(),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Auto-starting mock server" in result.output
    assert "Stopping auto-started mock server" in result.output
    assert "Submitted. HTTP 200" in result.output

    log = (tmp_path / "submissions.log").read_text(encoding="utf-8")
    assert "auto@example.com" in log

    # Server should be gone after the command exits.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        assert s.connect_ex(("127.0.0.1", port)) != 0


def test_cli_renew_reuses_running_mock_when_port_in_use(
    mock_server, c2l_file: Path
) -> None:
    """If a mock is already running, renew should reuse it (not auto-start)."""
    result = runner.invoke(
        app,
        [
            "renew",
            "--email",
            "reuse@example.com",
            "--full-name",
            "Reuse User",
            "--c2l-file",
            str(c2l_file),
            "--url",
            "test",
            "--expires",
            _near(),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Using existing mock server" in result.output
    assert "Auto-starting" not in result.output
    assert "Submitted. HTTP 200" in result.output


def test_safety_net_blocks_real_nikon_url(c2l_file: Path) -> None:
    """The autouse fixture in conftest.py must reject any call to the real URL."""
    with pytest.raises(RuntimeError, match="real Nikon endpoint"):
        submit_license_form(
            url="https://nis-e-update.nikon-instruments.jp/dealers/",
            email="me@example.com",
            full_name="Me",
            c2l_file=c2l_file,
            note="should never be sent",
        )


def test_cli_renew_force_overrides_window(mock_server, c2l_file: Path) -> None:
    result = runner.invoke(
        app,
        [
            "renew",
            "--email",
            "force@example.com",
            "--full-name",
            "Force User",
            "--c2l-file",
            str(c2l_file),
            "--url",
            "test",
            "--expires",
            _far_future(),
            "--force",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Submitted. HTTP 200" in result.output


# --- get_license_info (Sentinel ACC) ---


def _nikon_feat(exp: date, haspid: str = "159918744", **extra: str) -> dict[str, str]:
    rec = {"ven": "40094", "haspid": haspid, "lic": _lic_exp(exp)}
    rec.update(extra)
    return rec


def test_get_license_info_returns_earliest(mock_acc) -> None:
    mock_acc.xml_body = _acc_feed(
        # Perpetual master feature — must be ignored.
        {"ven": "40094", "haspid": "159918744", "lic": "Perpetual"},
        _nikon_feat(date(2026, 7, 27)),
        _nikon_feat(date(2026, 6, 5)),
        _nikon_feat(date(2026, 9, 1)),
        # Different vendor — must be ignored even if it expires sooner.
        {"ven": "99999", "haspid": "OTHER", "lic": _lic_exp(date(2025, 1, 1))},
    )
    info = get_license_info()
    assert info == LicenseInfo(expiration_date=date(2026, 6, 5), hasp_id="159918744")


def test_get_license_info_real_acc_payload_shape(mock_acc) -> None:
    """Exercise the exact pseudo-JSON shape ACC returns (comment header,
    comma-separated objects with no array brackets, terminal sentinel
    record, trailing admin_status comment)."""
    mock_acc.xml_body = (
        b"/*JSON:features*/\n\n"
        b'{"ndx":"1","ven":"40094","haspid":"159918744","fid":"0",'
        b'"lic":"Perpetual"},\n'
        b'{"ndx":"2","ven":"40094","haspid":"159918744","fid":"1",'
        b'"lic":"Expiration Date<br><nobr>&nbsp;Fri Jun 5, 2026 19:55</nobr>"},\n'
        b'{"fhaspid":"0","ffea":"0","cnt":"2"}\n'
        b"/*\n <admin_status>\n  <code>0</code>\n </admin_status>\n*/\n"
    )
    info = get_license_info()
    assert info == LicenseInfo(expiration_date=date(2026, 6, 5), hasp_id="159918744")


def test_get_license_info_connection_refused(monkeypatch) -> None:
    monkeypatch.setattr(_renew, "ACC_URL", "http://127.0.0.1:1/_int_/tab_feat.html")
    with pytest.raises(RuntimeError, match="Could not reach"):
        get_license_info()


def test_get_license_info_no_nikon_feature(mock_acc) -> None:
    mock_acc.xml_body = _acc_feed(
        {"ven": "99999", "haspid": "OTHER", "lic": _lic_exp(date(2030, 1, 1))},
    )
    with pytest.raises(RuntimeError, match="No time-bound features found"):
        get_license_info()


def test_get_license_info_all_perpetual(mock_acc) -> None:
    mock_acc.xml_body = _acc_feed(
        {"ven": "40094", "haspid": "159918744", "lic": "Perpetual"},
    )
    with pytest.raises(RuntimeError, match="No time-bound features found"):
        get_license_info()


def test_get_license_info_mismatched_hasp_ids(mock_acc) -> None:
    mock_acc.xml_body = _acc_feed(
        _nikon_feat(date(2026, 7, 27), haspid="AAAA1111"),
        _nikon_feat(date(2026, 7, 27), haspid="BBBB2222"),
    )
    with pytest.raises(RuntimeError, match="Expected exactly one HASP key"):
        get_license_info()


def test_get_license_info_unparsable_body(mock_acc) -> None:
    mock_acc.xml_body = b"/*JSON:features*/ this is not json at all"
    with pytest.raises(RuntimeError, match="unparsable features feed"):
        get_license_info()


# --- state file helpers ---


def test_renew_state_roundtrip(tmp_state_path: Path) -> None:
    state = RenewState(
        expiration_date=date(2026, 7, 27),
        hasp_id="4B92F5FA",
        submitted_at=datetime(2026, 5, 14, 12, 41, 33, tzinfo=timezone.utc),
        url="https://nis-e-update.nikon-instruments.jp/dealers/",
    )
    save_renew_state(state)
    loaded = load_renew_state()
    assert loaded == state


def test_load_renew_state_missing(tmp_state_path: Path) -> None:
    assert load_renew_state() is None


def test_load_renew_state_corrupt(tmp_state_path: Path) -> None:
    tmp_state_path.write_text("{not json", encoding="utf-8")
    assert load_renew_state() is None


def test_load_renew_state_missing_fields(tmp_state_path: Path) -> None:
    tmp_state_path.write_text(json.dumps({"hasp_id": "X"}), encoding="utf-8")
    assert load_renew_state() is None


def test_save_renew_state_atomic(tmp_state_path: Path) -> None:
    state = RenewState(
        expiration_date=date(2026, 7, 27),
        hasp_id="4B92F5FA",
        submitted_at=datetime(2026, 5, 14, tzinfo=timezone.utc),
        url="http://x",
    )
    save_renew_state(state)
    assert tmp_state_path.is_file()
    leftover = tmp_state_path.with_suffix(tmp_state_path.suffix + ".tmp")
    assert not leftover.exists()


# --- CLI dedup integration ---


def _info_factory(exp: date, hasp_id: str = "4B92F5FA"):
    return lambda: LicenseInfo(expiration_date=exp, hasp_id=hasp_id)


def _invoke_renew_no_expires(c2l_file: Path, email: str = "dedup@example.com"):
    return runner.invoke(
        app,
        [
            "renew",
            "--email",
            email,
            "--full-name",
            "Dedup User",
            "--c2l-file",
            str(c2l_file),
            "--url",
            "test",
        ],
    )


def test_cli_renew_writes_state_after_submit(
    mock_server, c2l_file: Path, tmp_state_path: Path, monkeypatch
) -> None:
    near = date.today() + timedelta(days=3)
    monkeypatch.setattr(_renew, "get_license_info", _info_factory(near))

    result = _invoke_renew_no_expires(c2l_file)
    assert result.exit_code == 0, result.output
    assert "Submitted. HTTP 200" in result.output

    state = load_renew_state()
    assert state is not None
    assert state.expiration_date == near
    assert state.hasp_id == "4B92F5FA"


def test_cli_renew_appends_hasp_id_to_note(
    mock_server, c2l_file: Path, tmp_state_path: Path, monkeypatch
) -> None:
    """The HASP ID (hex, uppercase, zero-padded) must be appended to the note
    so Nikon's renewal staff can identify the dongle in the submission."""
    near = date.today() + timedelta(days=3)
    # 159918744 decimal = 09882A98 hex (matches Nikon's filename convention).
    monkeypatch.setattr(
        _renew,
        "get_license_info",
        lambda: LicenseInfo(expiration_date=near, hasp_id="159918744"),
    )

    result = _invoke_renew_no_expires(c2l_file, email="hex@example.com")
    assert result.exit_code == 0, result.output

    log = mock_server["log_path"].read_text(encoding="utf-8")
    assert "[HASP ID: 09882A98]" in log


def test_cli_renew_does_not_append_hasp_id_when_expires_given(
    mock_server, c2l_file: Path, tmp_state_path: Path
) -> None:
    """When --expires bypasses ACC, the HASP ID is unknown and must NOT be
    appended (no fake value)."""
    near = (date.today() + timedelta(days=3)).strftime("%Y-%m-%d")
    result = runner.invoke(
        app,
        [
            "renew",
            "--email",
            "noacc@example.com",
            "--full-name",
            "No ACC",
            "--c2l-file",
            str(c2l_file),
            "--url",
            "test",
            "--expires",
            near,
        ],
    )
    assert result.exit_code == 0, result.output

    log = mock_server["log_path"].read_text(encoding="utf-8")
    assert "HASP ID" not in log


def test_cli_renew_dedup_skips_second_run(
    mock_server, c2l_file: Path, tmp_state_path: Path, monkeypatch
) -> None:
    near = date.today() + timedelta(days=3)
    monkeypatch.setattr(_renew, "get_license_info", _info_factory(near))

    first = _invoke_renew_no_expires(c2l_file)
    assert first.exit_code == 0, first.output
    assert "Submitted. HTTP 200" in first.output

    second = _invoke_renew_no_expires(c2l_file)
    assert second.exit_code == 0, second.output
    assert "Already submitted" in second.output
    assert "Submitted. HTTP" not in second.output

    # Exactly one POST hit the mock server.
    log = mock_server["log_path"].read_text(encoding="utf-8")
    assert log.count("dedup@example.com") == 1


def test_cli_renew_dedup_resubmits_when_exp_changes(
    mock_server, c2l_file: Path, tmp_state_path: Path, monkeypatch
) -> None:
    near = date.today() + timedelta(days=3)
    monkeypatch.setattr(_renew, "get_license_info", _info_factory(near))
    assert _invoke_renew_no_expires(c2l_file).exit_code == 0

    # Simulate Nikon's new .c2v being applied: ACC now reports a later date.
    new_exp = date.today() + timedelta(days=10)
    monkeypatch.setattr(_renew, "get_license_info", _info_factory(new_exp))

    result = _invoke_renew_no_expires(c2l_file, email="cycle2@example.com")
    assert result.exit_code == 0, result.output
    assert "Submitted. HTTP 200" in result.output

    state = load_renew_state()
    assert state is not None
    assert state.expiration_date == new_exp


def test_cli_renew_dedup_resubmits_when_hasp_id_changes(
    mock_server, c2l_file: Path, tmp_state_path: Path, monkeypatch
) -> None:
    near = date.today() + timedelta(days=3)
    monkeypatch.setattr(_renew, "get_license_info", _info_factory(near, "AAAA1111"))
    assert _invoke_renew_no_expires(c2l_file).exit_code == 0

    # Different dongle, same expiration: shouldn't be muted by the prior state.
    monkeypatch.setattr(_renew, "get_license_info", _info_factory(near, "BBBB2222"))
    result = _invoke_renew_no_expires(c2l_file, email="swap@example.com")
    assert result.exit_code == 0, result.output
    assert "Submitted. HTTP 200" in result.output


def test_cli_renew_force_bypasses_dedup(
    mock_server, c2l_file: Path, tmp_state_path: Path, monkeypatch
) -> None:
    near = date.today() + timedelta(days=3)
    monkeypatch.setattr(_renew, "get_license_info", _info_factory(near))
    assert _invoke_renew_no_expires(c2l_file).exit_code == 0

    result = runner.invoke(
        app,
        [
            "renew",
            "--email",
            "force-dedup@example.com",
            "--full-name",
            "Force",
            "--c2l-file",
            str(c2l_file),
            "--url",
            "test",
            "--force",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Submitted. HTTP 200" in result.output
    assert "Already submitted" not in result.output


def test_cli_renew_acc_error_exits_nonzero(
    c2l_file: Path, tmp_state_path: Path, monkeypatch
) -> None:
    def _boom() -> LicenseInfo:
        raise RuntimeError("Could not reach Sentinel ACC at http://localhost:1947")

    monkeypatch.setattr(_renew, "get_license_info", _boom)
    result = _invoke_renew_no_expires(c2l_file)
    assert result.exit_code == 1
    assert "Could not reach Sentinel ACC" in result.output
