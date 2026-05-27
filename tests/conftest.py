"""Test-wide safety nets and shared fixtures."""

import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import requests

from cite import _log, _renew
from cite._log import close_logging
from cite._renew import URL_ALIASES, RenewTarget
from cite.mock_renew.server import _Handler

_REAL_NIKON_DOMAIN = "nikon-instruments.jp"
_ALERT_ENV_VARS = (
    "CITE_ALERT_SMTP_USER",
    "CITE_ALERT_SMTP_PASSWORD",
    "CITE_ALERT_TO",
    "CITE_ALERT_SMTP_HOST",
    "CITE_ALERT_SMTP_PORT",
    "CITE_ALERT_FROM",
)


@pytest.fixture(autouse=True)
def _isolate_logging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect cite logging to tmp_path and close the handler after each test.

    This prevents ResourceWarning from unclosed file handles when CLI commands
    trigger init_logging() during tests.
    """
    logs_dir = tmp_path / "logs"
    cite_log = logs_dir / "cite.log"
    monkeypatch.setattr(_log, "LOGS_DIR", logs_dir)
    monkeypatch.setattr(_log, "CITE_LOG", cite_log)
    yield
    close_logging()


@pytest.fixture(autouse=True)
def _block_real_nikon_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard-fail any test that tries to POST to the real Nikon endpoint."""
    real_post = requests.post

    def guarded_post(url, *args, **kwargs):  # type: ignore[no-untyped-def]
        if _REAL_NIKON_DOMAIN in str(url):
            raise RuntimeError(
                f"Test attempted to hit the real Nikon endpoint: {url!r}. "
                "Use '--url test' (the mock_server fixture remaps it to a "
                "local mock)."
            )
        return real_post(url, *args, **kwargs)

    monkeypatch.setattr(requests, "post", guarded_post)


@pytest.fixture(autouse=True)
def _strip_alert_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear CITE_ALERT_* env vars so tests can never fire a real email."""
    for var in _ALERT_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def mock_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Spin up the renewal mock server on a free port; remap 'test' alias to it."""
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


@pytest.fixture()
def tmp_state_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the renew-state file to a tmp path so tests never touch $HOME."""
    p = tmp_path / "renew_state.json"
    monkeypatch.setattr(_renew, "RENEW_STATE_PATH", p)
    return p


@pytest.fixture()
def tmp_last_notified_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the last-notified file to a tmp path so tests never touch $HOME."""
    p = tmp_path / "last_notified_renewal.json"
    monkeypatch.setattr(_renew, "LAST_NOTIFIED_PATH", p)
    return p


@pytest.fixture(autouse=True)
def _isolate_urgency_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect LAST_URGENCY_PATH so tests never touch .cite/last_urgency_alert.json."""
    monkeypatch.setattr(
        _renew, "LAST_URGENCY_PATH", tmp_path / "last_urgency_alert.json"
    )


@pytest.fixture(autouse=True)
def _isolate_last_hasp_id_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect LAST_HASP_ID_PATH so tests never touch ~/.cite/last_hasp_id.txt."""
    monkeypatch.setattr(_renew, "LAST_HASP_ID_PATH", tmp_path / "last_hasp_id.txt")
