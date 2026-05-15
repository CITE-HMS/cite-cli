"""NIS-Elements Time-DEMO license renewal."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path

import requests

DEFAULT_URL = "https://nis-e-update.nikon-instruments.jp/dealers/"
DEFAULT_DAYS_BEFORE = 14

MOCK_C2L_SENTINEL = "mock"
MOCK_C2L_PATH = Path(__file__).parent / "mock_renew" / "mock.c2l"

ACC_URL = "http://localhost:1947/_int_/tab_feat.html"
NIKON_VENDOR_ID = "40094"

_JSON_HEADER_RE = re.compile(rb"^/\*JSON:[^*]*\*/", re.DOTALL)
_JSON_TRAILER_RE = re.compile(rb"/\*.*?\*/\s*$", re.DOTALL)
_EXP_DATE_RE = re.compile(r"(\w{3})\s+(\d{1,2}),\s+(\d{4})")

RENEW_STATE_PATH = Path.home() / ".cite" / "renew_state.json"


class RenewTarget(str, Enum):
    nikon = "nikon"
    test = "test"


URL_ALIASES: dict[str, str] = {
    RenewTarget.nikon.value: DEFAULT_URL,
    RenewTarget.test.value: "http://127.0.0.1:8765/",
}


@dataclass(frozen=True)
class LicenseInfo:
    expiration_date: date
    hasp_id: str


@dataclass(frozen=True)
class RenewState:
    expiration_date: date
    hasp_id: str
    submitted_at: datetime
    url: str


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


def _parse_acc_features(body: bytes) -> list[dict[str, str]]:
    """Parse ACC's pseudo-JSON features feed into a list of records.

    The body looks like::

        /*JSON:features*/
        {"ndx":"1", ...},
        {"ndx":"2", ...},
        {"fhaspid":"0","ffea":"0","cnt":"5"}
        /* <admin_status>...</admin_status> */

    i.e. a leading comment, comma-separated objects with no enclosing
    brackets, and a trailing admin-status comment.
    """
    cleaned = _JSON_HEADER_RE.sub(b"", body, count=1).strip()
    cleaned = _JSON_TRAILER_RE.sub(b"", cleaned).strip()
    try:
        records: list[dict[str, str]] = json.loads(b"[" + cleaned + b"]")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"ACC returned unparsable features feed: {e}") from e
    return records


def _parse_exp_date(lic: str) -> date | None:
    """Extract the expiration date from a `lic` field like
    'Expiration Date<br><nobr>&nbsp;Fri Jun 5, 2026 19:55</nobr>'.
    Returns None for perpetual / unrecognised entries."""
    if "Expiration Date" not in lic:
        return None
    m = _EXP_DATE_RE.search(lic)
    if not m:
        return None
    month, day, year = m.group(1), m.group(2), m.group(3)
    return datetime.strptime(f"{month} {day} {year}", "%b %d %Y").date()


def get_license_info() -> LicenseInfo:
    """Return the earliest Nikon-vendor expiration date and HASP key ID via ACC.

    Queries the local Sentinel HASP Admin Control Center at
    `http://localhost:1947/_int_/tab_feat.html` (read-only) and parses its
    pseudo-JSON features feed. Filters by Nikon's vendor ID (`40094`) and
    skips perpetual features. Raises RuntimeError if ACC is unreachable,
    no matching feature is found, or the response is malformed.
    """
    try:
        resp = requests.get(ACC_URL, timeout=5)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(
            f"Could not reach Sentinel ACC at {ACC_URL} — is hasplms running? ({e})"
        ) from e

    records = _parse_acc_features(resp.content)

    dates: list[date] = []
    hasp_ids: set[str] = set()
    for rec in records:
        if rec.get("ven") != NIKON_VENDOR_ID:
            continue
        exp = _parse_exp_date(rec.get("lic", ""))
        if exp is None:
            continue
        dates.append(exp)
        hid = rec.get("haspid", "").strip()
        if hid:
            hasp_ids.add(hid)

    if not dates:
        raise RuntimeError(
            f"No time-bound features found for Nikon vendor {NIKON_VENDOR_ID} "
            "via Sentinel ACC — is a Nikon dongle attached?"
        )
    if len(hasp_ids) != 1:
        raise RuntimeError(
            f"Expected exactly one HASP key for Nikon features, "
            f"got {sorted(hasp_ids)!r}"
        )
    return LicenseInfo(expiration_date=min(dates), hasp_id=hasp_ids.pop())


def load_renew_state(path: Path | None = None) -> RenewState | None:
    """Read the dedup state file. Returns None if missing or unparsable."""
    if path is None:
        path = RENEW_STATE_PATH
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw)
        return RenewState(
            expiration_date=date.fromisoformat(data["expiration_date"]),
            hasp_id=str(data["hasp_id"]),
            submitted_at=datetime.fromisoformat(data["submitted_at"]),
            url=str(data["url"]),
        )
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None


def save_renew_state(state: RenewState, path: Path | None = None) -> None:
    """Persist the dedup state file atomically."""
    if path is None:
        path = RENEW_STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = {
        "expiration_date": state.expiration_date.isoformat(),
        "hasp_id": state.hasp_id,
        "submitted_at": state.submitted_at.isoformat(),
        "url": state.url,
    }
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


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
