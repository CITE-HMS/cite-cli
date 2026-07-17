"""NIS-Elements Time-DEMO license renewal."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from glob import glob
from pathlib import Path

import requests

DEFAULT_URL = "https://nis-e-update.nikon-instruments.jp/dealers/"

MOCK_C2L_SENTINEL = "mock"
MOCK_C2L_PATH = Path(__file__).parent / "mock_renew" / "mock.c2l"

NIKON_VENDOR_ID = "40094"
# Newer RTE (≥ HASP LM 22) 400s the bare URL and requires the `vendorid` +
# `featureid` query params. Older RTE accepts the bare URL. Try bare first
# (proven on the existing fleet), then the param'd URL as a fallback.
_ACC_BASE = "http://localhost:1947/_int_/tab_feat.html"
ACC_URLS: tuple[str, ...] = (
    _ACC_BASE,
    f"{_ACC_BASE}?vendorid={NIKON_VENDOR_ID}&featureid=-1",
)

_JSON_HEADER_RE = re.compile(rb"^/\*JSON:[^*]*\*/", re.DOTALL)
_JSON_TRAILER_RE = re.compile(rb"/\*.*?\*/\s*$", re.DOTALL)
_EXP_DATE_RE = re.compile(r"(\w{3})\s+(\d{1,2}),\s+(\d{4})")

RENEW_STATE_PATH = Path.home() / ".cite" / "renew_state.json"
GENERATED_C2L_PATH = Path.home() / ".cite" / "generated_request.c2l"
LAST_NOTIFIED_PATH = Path.home() / ".cite" / "last_notified_renewal.json"
LAST_URGENCY_PATH = Path.home() / ".cite" / "last_urgency_alert.json"
LAST_HASP_ID_PATH = Path.home() / ".cite" / "last_hasp_id.txt"

# Standard install locations for Nikon's HASP Update tool. The user can
# override via the CITE_RUS_EXE env var if their install is elsewhere.
RUS_EXE_GLOB_PATTERNS = (
    "C:/Program Files/NIS-Elements*/HASP/nis_hasp_update.exe",
    "C:/Program Files (x86)/NIS-Elements*/HASP/nis_hasp_update.exe",
)

HASP_ID_TO_STATIONS_MAP: dict[str, str] = {
    "57ABC02E": "Station 1 (Dongle 202140)",
    "09882A98": "Station 2 (Dongle 142841)",
    "3B8C0A7D": "Station 3 (Dongle 202137)",
    "4B92F5FA": "Station 5 (Dongle 202136)",
    "22B5229C": "Station 8 (Dongle 202141)",
    "42E55C92": "Station 9 (Dongle 202142)",
    "520D66C9": "Station 10 (Dongle 202140)",
    "7DCAF069": "Station 14 (Dongle 202134)",
    "1F5B4CB0": "Station 15 (Dongle 202138)",
    "45785A00": "Station 18 (Dongle )",
}


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


def fetch_acc_response() -> requests.Response:
    """Fetch the ACC features feed, trying each URL in ACC_URLS until one works.

    Newer RTE rejects the bare URL and requires query params; older RTE accepts
    the bare URL. Raises RuntimeError if none responds.
    """
    last_error: Exception | None = None
    for url in ACC_URLS:
        try:
            resp = requests.get(url, timeout=5, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            last_error = e
    raise RuntimeError(
        f"Could not reach Sentinel ACC at any of {ACC_URLS} — "
        f"is hasplms running? ({last_error})"
    ) from last_error


def get_license_info(hasp_id: str | None = None) -> LicenseInfo:
    """Return the earliest Nikon-vendor expiration date and HASP key ID via ACC.

    Queries the local Sentinel HASP Admin Control Center via `fetch_acc_response`
    (which tries `ACC_URLS` in order) and parses its pseudo-JSON features feed.
    Filters by Nikon's vendor ID (`40094`) client-side and skips
    perpetual features.

    If *hasp_id* (decimal string as reported by ACC) is given, only features
    for that specific key are considered — handles multi-key setups without
    false positives.  Without a filter, all Nikon keys are scanned and the
    entry with the earliest expiry is returned, so attaching a second dongle
    no longer raises an error.

    Raises RuntimeError if ACC is unreachable, no matching feature is found, or
    the response is malformed.
    """
    resp = fetch_acc_response()
    records = _parse_acc_features(resp.content)

    entries: list[tuple[date, str]] = []
    for rec in records:
        if rec.get("ven") != NIKON_VENDOR_ID:
            continue
        hid = rec.get("haspid", "").strip()
        if hasp_id is not None and hid != hasp_id:
            continue
        exp = _parse_exp_date(rec.get("lic", ""))
        if exp is None or not hid:
            continue
        entries.append((exp, hid))

    if not entries:
        raise RuntimeError(
            f"No time-bound features found for Nikon vendor {NIKON_VENDOR_ID}"
            + (f" on HASP key {hasp_id}" if hasp_id else "")
            + " via Sentinel ACC — is a Nikon dongle attached?"
        )

    entries.sort(key=lambda x: x[0])
    info = LicenseInfo(expiration_date=entries[0][0], hasp_id=entries[0][1])
    try:
        LAST_HASP_ID_PATH.parent.mkdir(parents=True, exist_ok=True)
        LAST_HASP_ID_PATH.write_text(info.hasp_id)
    except OSError:
        pass
    return info


def _atomic_replace(src: Path, dst: Path) -> None:
    """Rename src → dst atomically, retrying on Windows AV-induced PermissionError.

    On Windows, security software can briefly lock a newly-written file, causing
    os.replace to raise PermissionError. Three attempts with 100 ms gaps covers
    typical scan delays without meaningfully slowing the happy path.
    On non-Windows os.replace never raises PermissionError for this reason, so
    the retry loop is a no-op.
    """
    for attempt in range(3):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 2 or sys.platform != "win32":
                raise
            time.sleep(0.1)


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
    _atomic_replace(tmp, path)


def load_last_notified(path: Path | None = None) -> LicenseInfo | None:
    """Read the last-notified renewal record. Returns None if missing or malformed."""
    if path is None:
        path = LAST_NOTIFIED_PATH
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw)
        return LicenseInfo(
            expiration_date=date.fromisoformat(data["expiration_date"]),
            hasp_id=str(data["hasp_id"]),
        )
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None


def save_last_notified(info: LicenseInfo, path: Path | None = None) -> None:
    """Persist the last-notified renewal record atomically."""
    if path is None:
        path = LAST_NOTIFIED_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = {
        "hasp_id": info.hasp_id,
        "expiration_date": info.expiration_date.isoformat(),
        "notified_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _atomic_replace(tmp, path)


def load_last_urgency(path: Path | None = None) -> datetime | None:
    """Return the timestamp of the last urgency alert, or None if never sent."""
    if path is None:
        path = LAST_URGENCY_PATH
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw)
        return datetime.fromisoformat(data["sent_at"])
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None


def save_last_urgency(sent_at: datetime, path: Path | None = None) -> None:
    """Atomically persist the urgency-alert timestamp."""
    if path is None:
        path = LAST_URGENCY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps({"sent_at": sent_at.isoformat()}, indent=2), encoding="utf-8"
    )
    _atomic_replace(tmp, path)


def should_renew(expiration_date: date, days_before: int) -> bool:
    """Return True if a renewal should be submitted.

    Triggers when the license is within *days_before* days of expiry **or
    already expired** (days_left < 0).
    """
    days_left = (expiration_date - date.today()).days
    return days_left <= days_before


def hasp_id_to_hex(hasp_id: str) -> str:
    """Convert a decimal HASP ID string to 8-char uppercase hex.

    Returns the string unchanged if it is not a valid integer (e.g. already
    in hex or an unknown format), so callers never need a try/except.
    """
    try:
        return f"{int(hasp_id):08X}"
    except ValueError:
        return hasp_id


def hasp_id_to_station(hasp_id: str) -> str | None:
    """Return the station name for a HASP ID (decimal string), or None if unknown."""
    return HASP_ID_TO_STATIONS_MAP.get(hasp_id_to_hex(hasp_id))


def load_cached_hasp_id() -> str | None:
    """Return the last HASP ID seen via Sentinel, or None if no cache exists.

    Fallback for failure emails when the live Sentinel call is itself the
    thing that failed — see send_failure_email in _notify.py.
    """
    try:
        return LAST_HASP_ID_PATH.read_text().strip() or None
    except OSError:
        return None


def discover_rus_exe() -> Path | None:
    """Find nis_hasp_update.exe under standard NIS-Elements install locations.

    Honors the CITE_RUS_EXE env var as an override. Returns None if no
    candidate exists on disk.
    """
    override = os.environ.get("CITE_RUS_EXE")
    if override:
        p = Path(override)
        return p if p.is_file() else None
    for pattern in RUS_EXE_GLOB_PATTERNS:
        for match in glob(pattern):
            return Path(match)
    return None


def generate_c2l(output_path: Path, rus_exe: Path | None = None) -> Path:
    """Generate a fresh .c2l renewal request via nis_hasp_update.exe.

    Calls `nis_hasp_update.exe -r <output_path>` (the documented silent
    flag found in the binary's string table). Verifies the output file
    was actually created — the tool can exit 0 without writing the file
    when the target directory doesn't exist, so we never trust the exit
    code alone.

    Raises RuntimeError on any failure (RUS not found, subprocess error,
    or no output file produced).
    """
    if rus_exe is None:
        rus_exe = discover_rus_exe()
    if rus_exe is None or not rus_exe.is_file():
        raise RuntimeError(
            "Could not locate nis_hasp_update.exe. Set CITE_RUS_EXE to its "
            "full path, or confirm NIS-Elements is installed under "
            r"C:\Program Files\NIS-Elements*\HASP\."
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Write to a tmp path first so the previous .c2l survives if the tool fails.
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp_path.unlink(missing_ok=True)

    try:
        result = subprocess.run(
            [str(rus_exe), "-r", str(tmp_path)],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=60,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"{rus_exe.name} timed out after 60s; is a HASP dongle attached?"
        ) from e

    if result.returncode != 0:
        raise RuntimeError(
            f"{rus_exe.name} exited {result.returncode}: "
            f"{(result.stderr or result.stdout or '(no output)').strip()}"
        )

    if not tmp_path.is_file():
        raise RuntimeError(
            f"{rus_exe.name} reported success but did not create {output_path}. "
            "Check that the parent directory exists and is writable, and that "
            "a HASP dongle is attached."
        )

    _atomic_replace(tmp_path, output_path)
    return output_path


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
    if not resp.content:
        raise RuntimeError(
            f"Nikon returned an empty response body from {url}. "
            "The submission may not have been processed."
        )
    # Nikon's endpoint can return HTTP 200 with an HTML error page on bad
    # input (invalid email, malformed .c2l, etc.). Success also returns HTML,
    # so only reject when we see error-page title or server-error markers.
    _body = resp.content[:2048].lower()
    if b"<html" in _body or b"<!doctype" in _body:
        _error_titles = (
            b"<title>error",
            b"<title>400",
            b"<title>500",
            b"<title>bad request",
            b"<title>not found",
        )
        _error_body = (b"400 bad request", b"500 internal server error")
        if any(t in _body for t in _error_titles + _error_body):
            preview = resp.content[:300].decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"Nikon returned an HTML error page (HTTP {resp.status_code}); "
                "the submission was likely not processed. Check your email "
                f"address, .c2l file, and note.\nResponse preview: {preview!r}"
            )
    return resp
