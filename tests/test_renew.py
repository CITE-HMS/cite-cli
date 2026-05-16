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
    discover_rus_exe,
    generate_c2l,
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
        (-1, True),  # already expired — renewal is overdue
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


def test_submit_license_form_empty_body_raises(tmp_path: Path, monkeypatch) -> None:
    """An HTTP 200 with an empty body must raise — it signals a silent failure."""
    import requests as _req

    c2l = tmp_path / "req.c2l"
    c2l.write_bytes(b"dummy c2l")

    class _EmptyResp:
        status_code = 200
        content = b""

        def raise_for_status(self) -> None:
            pass

    monkeypatch.setattr(_req, "post", lambda *a, **kw: _EmptyResp())
    with pytest.raises(RuntimeError, match="empty response body"):
        submit_license_form(
            url="http://127.0.0.1:1/",
            email="x@y.z",
            full_name="x",
            c2l_file=c2l,
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


# --- discover_rus_exe / generate_c2l ---


def test_discover_rus_exe_honours_env_override(tmp_path: Path, monkeypatch) -> None:
    fake_exe = tmp_path / "nis_hasp_update.exe"
    fake_exe.write_bytes(b"")
    monkeypatch.setenv("CITE_RUS_EXE", str(fake_exe))
    assert discover_rus_exe() == fake_exe


def test_discover_rus_exe_returns_none_for_bad_override(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CITE_RUS_EXE", str(tmp_path / "does_not_exist.exe"))
    # Don't accidentally match a real install on the dev box:
    monkeypatch.setattr(_renew, "RUS_EXE_GLOB_PATTERNS", ())
    assert discover_rus_exe() is None


def test_generate_c2l_writes_file(tmp_path: Path, monkeypatch) -> None:
    """Successful path: subprocess writes the file, returncode 0."""
    import subprocess

    fake_exe = tmp_path / "nis_hasp_update.exe"
    fake_exe.write_bytes(b"")
    output = tmp_path / "out.c2l"
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        Path(cmd[-1]).write_bytes(b"fake c2l content")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_renew.subprocess, "run", fake_run)

    result = generate_c2l(output, rus_exe=fake_exe)
    assert result == output
    assert output.read_bytes() == b"fake c2l content"
    assert captured["cmd"] == [str(fake_exe), "-r", str(output)]


def test_generate_c2l_no_rus_found(monkeypatch) -> None:
    monkeypatch.delenv("CITE_RUS_EXE", raising=False)
    monkeypatch.setattr(_renew, "RUS_EXE_GLOB_PATTERNS", ())
    with pytest.raises(RuntimeError, match="Could not locate nis_hasp_update"):
        generate_c2l(Path("/tmp/whatever.c2l"))


def test_generate_c2l_nonzero_exit_raises(tmp_path: Path, monkeypatch) -> None:
    fake_exe = tmp_path / "nis_hasp_update.exe"
    fake_exe.write_bytes(b"")

    def fake_run(cmd, **kwargs):
        import subprocess

        return subprocess.CompletedProcess(
            cmd, returncode=2, stdout="", stderr="oh no it broke"
        )

    monkeypatch.setattr(_renew.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="oh no it broke"):
        generate_c2l(tmp_path / "out.c2l", rus_exe=fake_exe)


def test_generate_c2l_exit_zero_but_no_file_raises(tmp_path: Path, monkeypatch) -> None:
    """The real bug from manual testing: RUS exits 0 without writing the file
    when the target dir doesn't exist. We must NOT trust the exit code alone."""
    fake_exe = tmp_path / "nis_hasp_update.exe"
    fake_exe.write_bytes(b"")

    def fake_run(cmd, **kwargs):
        import subprocess

        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_renew.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="did not create"):
        generate_c2l(tmp_path / "out.c2l", rus_exe=fake_exe)


def test_generate_c2l_overwrites_stale_file(tmp_path: Path, monkeypatch) -> None:
    """If a previous .c2l exists at the target path, it must be replaced
    (otherwise a failed run could leave us submitting stale data)."""
    fake_exe = tmp_path / "nis_hasp_update.exe"
    fake_exe.write_bytes(b"")
    output = tmp_path / "out.c2l"
    output.write_bytes(b"OLD STALE CONTENT")

    def fake_run(cmd, **kwargs):
        import subprocess

        Path(cmd[-1]).write_bytes(b"FRESH CONTENT")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_renew.subprocess, "run", fake_run)

    generate_c2l(output, rus_exe=fake_exe)
    assert output.read_bytes() == b"FRESH CONTENT"


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


# --- cite request-file + auto-generation in renew ---


def test_cli_request_file_writes_to_output(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "out.c2l"

    def fake_generate(target: Path, rus_exe=None):
        target.write_bytes(b"hello c2l")
        return target

    monkeypatch.setattr(_renew, "generate_c2l", fake_generate)

    result = runner.invoke(app, ["request-file", "--output", str(out)])
    assert result.exit_code == 0, result.output
    assert "Wrote" in result.output
    assert str(out) in result.output
    assert out.read_bytes() == b"hello c2l"


def test_cli_request_file_error_exits_nonzero(tmp_path: Path, monkeypatch) -> None:
    def fake_generate(target: Path, rus_exe=None):
        raise RuntimeError("nis_hasp_update.exe not found")

    monkeypatch.setattr(_renew, "generate_c2l", fake_generate)
    result = runner.invoke(app, ["request-file", "--output", str(tmp_path / "x.c2l")])
    assert result.exit_code == 1
    assert "nis_hasp_update.exe not found" in result.output


def test_cli_renew_auto_generates_c2l_when_omitted(
    mock_server, tmp_state_path: Path, tmp_path: Path, monkeypatch
) -> None:
    """When --c2l-file is omitted, `cite renew` should call generate_c2l."""
    near = date.today() + timedelta(days=3)
    monkeypatch.setattr(_renew, "get_license_info", _info_factory(near))

    generated = tmp_path / "auto_generated.c2l"
    monkeypatch.setattr(_renew, "GENERATED_C2L_PATH", generated)

    captured: dict = {}

    def fake_generate(target: Path, rus_exe=None):
        captured["target"] = target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"auto-generated c2l bytes")
        return target

    monkeypatch.setattr(_renew, "generate_c2l", fake_generate)

    result = runner.invoke(
        app,
        [
            "renew",
            "--email",
            "auto@example.com",
            "--full-name",
            "Auto",
            "--url",
            "test",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Generating fresh .c2l" in result.output
    assert "Submitted. HTTP 200" in result.output
    assert captured["target"] == generated
    assert generated.is_file()

    # The auto-generated file actually got POSTed.
    log = mock_server["log_path"].read_text(encoding="utf-8")
    assert "auto_generated.c2l" in log


def test_cli_renew_dry_run_does_not_generate(
    tmp_state_path: Path, tmp_path: Path, monkeypatch
) -> None:
    """Dry-run with no --c2l-file should print the auto-generate intent
    but NOT actually invoke nis_hasp_update.exe."""
    near = date.today() + timedelta(days=3)
    monkeypatch.setattr(_renew, "get_license_info", _info_factory(near))

    called = {"yes": False}

    def fake_generate(target: Path, rus_exe=None):
        called["yes"] = True
        raise AssertionError("must not be called in dry-run")

    monkeypatch.setattr(_renew, "generate_c2l", fake_generate)

    result = runner.invoke(
        app,
        [
            "renew",
            "--email",
            "dry@example.com",
            "--full-name",
            "Dry",
            "--url",
            "test",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "auto-generate" in result.output
    assert called["yes"] is False


# --- combined renew (apply-first, then submit) -----------------------------


def test_cli_renew_runs_apply_phase_first(
    mock_server, c2l_file: Path, tmp_state_path: Path, monkeypatch
) -> None:
    """By default `cite renew` calls apply_update() before the submit phase."""
    from cite import cli

    call_order: list[str] = []
    real_apply = cli.apply_update

    def recording_apply(*args, **kwargs):
        call_order.append("apply")
        real_apply(*args, **kwargs)

    monkeypatch.setattr(cli, "apply_update", recording_apply)

    near = date.today() + timedelta(days=3)
    monkeypatch.setattr(_renew, "get_license_info", _info_factory(near))

    result = runner.invoke(
        app,
        [
            "renew",
            "--email",
            "combined@example.com",
            "--full-name",
            "Combined",
            "--c2l-file",
            str(c2l_file),
            "--url",
            "test",
        ],
    )
    assert result.exit_code == 0, result.output
    assert call_order == ["apply"]
    assert "Submitted. HTTP 200" in result.output


def test_cli_renew_no_apply_skips_apply_phase(
    mock_server, c2l_file: Path, tmp_state_path: Path, monkeypatch
) -> None:
    """`--no-apply` skips the apply phase entirely."""
    from cite import cli

    apply_called = {"yes": False}

    def boom_if_called(*args, **kwargs):
        apply_called["yes"] = True
        raise AssertionError("apply_update should not be called when --no-apply")

    monkeypatch.setattr(cli, "apply_update", boom_if_called)

    result = runner.invoke(
        app,
        [
            "renew",
            "--email",
            "noapply@example.com",
            "--full-name",
            "NoApply",
            "--c2l-file",
            str(c2l_file),
            "--url",
            "test",
            "--expires",
            _near(),
            "--no-apply",
        ],
    )
    assert result.exit_code == 0, result.output
    assert apply_called["yes"] is False
    assert "Submitted. HTTP 200" in result.output


def test_cli_renew_continues_when_apply_phase_fails(
    mock_server, c2l_file: Path, tmp_state_path: Path, monkeypatch
) -> None:
    """An apply-phase failure must NOT block the submit phase."""
    import typer as _typer

    from cite import cli

    def failing_apply(*args, **kwargs):
        # Simulate apply_update raising typer.Exit(1) after its own
        # _alert_on_failure wrapper already dispatched the alert.
        raise _typer.Exit(1)

    monkeypatch.setattr(cli, "apply_update", failing_apply)

    result = runner.invoke(
        app,
        [
            "renew",
            "--email",
            "resilient@example.com",
            "--full-name",
            "Resilient",
            "--c2l-file",
            str(c2l_file),
            "--url",
            "test",
            "--expires",
            _near(),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Apply phase failed" in result.output
    assert "continuing to submit phase" in result.output.lower()
    assert "Submitted. HTTP 200" in result.output
