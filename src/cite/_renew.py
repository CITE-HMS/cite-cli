"""NIS-Elements Time-DEMO license renewal."""

from datetime import date
from enum import Enum
from pathlib import Path

import requests

DEFAULT_URL = "https://nis-e-update.nikon-instruments.jp/dealers/"
DEFAULT_DAYS_BEFORE = 14

MOCK_C2L_SENTINEL = "mock"
MOCK_C2L_PATH = Path(__file__).parent / "mock_renew" / "mock.c2l"


class RenewTarget(str, Enum):
    nikon = "nikon"
    test = "test"


URL_ALIASES: dict[str, str] = {
    RenewTarget.nikon.value: DEFAULT_URL,
    RenewTarget.test.value: "http://127.0.0.1:8765/",
}


def resolve_url(target: RenewTarget | str) -> str:
    key = target.value if isinstance(target, RenewTarget) else target
    return URL_ALIASES[key]


def resolve_c2l_file(value: str | Path) -> Path:
    """Resolve a `--c2l-file` argument.

    If the value is the literal 'mock' sentinel, return the bundled mock.c2l.
    Otherwise treat as a filesystem path and validate existence.
    """
    if str(value) == MOCK_C2L_SENTINEL:
        return MOCK_C2L_PATH
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"C2L file not found: {path}")
    return path


def get_license_expiration_date() -> date:
    # TODO: replace with real detection from the NIS-Elements license file.
    return date(2026, 6, 1)


def should_renew(expiration_date: date, days_before: int) -> bool:
    days_left = (expiration_date - date.today()).days
    return 0 <= days_left <= days_before


def submit_license_form(
    *,
    url: str,
    email: str,
    full_name: str,
    c2l_file: Path,
    note: str,
    timeout: float = 30.0,
) -> requests.Response:
    if not c2l_file.is_file():
        raise FileNotFoundError(f"C2L file not found: {c2l_file}")

    with c2l_file.open("rb") as fh:
        files = {"c2l": (c2l_file.name, fh, "application/octet-stream")}
        data = {
            "email": email,
            "name": full_name,
            "note": note,
            "sendMe": "Send",
        }
        resp = requests.post(url, data=data, files=files, timeout=timeout)
    resp.raise_for_status()
    return resp
