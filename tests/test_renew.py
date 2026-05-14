import hashlib
import socket
import threading
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cite._renew import (
    MOCK_C2L_PATH,
    URL_ALIASES,
    RenewTarget,
    resolve_c2l_file,
    resolve_url,
    should_renew,
    submit_license_form,
)
from cite.cli import app
from cite.mock_renew.server import _Handler

runner = CliRunner()


@pytest.fixture()
def mock_server(tmp_path: Path, monkeypatch):
    """Spin up the mock server on a free port; remap 'test' alias to it."""
    _Handler.log_path = tmp_path / "submissions.log"
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    host, port = server.server_address[:2]
    url = f"http://{host}:{port}/"
    monkeypatch.setitem(URL_ALIASES, RenewTarget.test.value, url)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"url": url, "log_path": _Handler.log_path}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture()
def c2l_file(tmp_path: Path) -> Path:
    f = tmp_path / "license_request.c2l"
    f.write_bytes(b"fake c2l contents 12345")
    return f


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
    assert "--url" in result.output


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
