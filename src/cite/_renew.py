"""NIS-Elements Time-DEMO license renewal."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from glob import glob
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
GENERATED_C2L_PATH = Path.home() / ".cite" / "generated_request.c2l"
INCOMING_DIR = Path.home() / ".cite" / "incoming"
APPLIED_L2C_DIR = Path.home() / ".cite" / "applied"
RECEIVED_L2C_PATH = Path.home() / ".cite" / "received_update.l2c"
CHECKED_EMAILS_PATH = Path.home() / ".cite" / "checked_emails.json"

CHECKED_EMAILS_TTL_DAYS = 90

RUS_APPLY_FLAG = "-a"

# Standard install locations for Nikon's HASP Update tool. The user can
# override via the CITE_RUS_EXE env var if their install is elsewhere.
RUS_EXE_GLOB_PATTERNS = (
    "C:/Program Files/NIS-Elements*/HASP/nis_hasp_update.exe",
    "C:/Program Files (x86)/NIS-Elements*/HASP/nis_hasp_update.exe",
)


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
    if output_path.exists():
        output_path.unlink()

    try:
        result = subprocess.run(
            [str(rus_exe), "-r", str(output_path)],
            capture_output=True,
            text=True,
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

    if not output_path.is_file():
        raise RuntimeError(
            f"{rus_exe.name} reported success but did not create {output_path}. "
            "Check that the parent directory exists and is writable, and that "
            "a HASP dongle is attached."
        )

    return output_path


_CONTENT_DISP_FILENAME_RE = re.compile(r'filename\s*=\s*"?([^";\s]+)"?', re.IGNORECASE)
_L2C_FILENAME_RE = re.compile(r"^([0-9A-Fa-f]{8})\.l2c$", re.IGNORECASE)


def download_l2c(url: str, output_dir: Path, timeout: float = 30.0) -> tuple[Path, str]:
    """Download the .l2c bytes from Nikon's dealer endpoint.

    Nikon's flow is two-step: a GET returns an HTML confirmation page
    with a form (button labelled "Click to download the update"), and a
    POST with `downloadNow=true&sendMe=Click to download the update` to
    the same URL returns the actual .l2c bytes. We replicate both steps
    via a Session so any session cookies set on the GET carry through.

    Returns (saved_path, original_filename). The original filename is
    extracted from the POST response's Content-Disposition header
    (Nikon serves `<HASPID_hex>.l2c`); falls back to the URL's request
    token if missing.

    Raises RuntimeError on HTTP failure, empty body, or if Nikon serves
    HTML instead of the binary .l2c (which usually means the request
    token is invalid or expired).
    """
    try:
        with requests.Session() as session:
            # Step 1: GET the confirmation page (also primes any session cookies).
            initial = session.get(url, timeout=timeout)
            initial.raise_for_status()
            # If the GET unexpectedly returned the binary file directly (some
            # endpoint versions might), use it. Otherwise complete the
            # two-step download by POSTing the form.
            if _looks_like_l2c_payload(initial):
                resp = initial
            else:
                resp = session.post(
                    url,
                    data={
                        "downloadNow": "true",
                        "sendMe": "Click to download the update",
                    },
                    timeout=timeout,
                )
                resp.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"Failed to download .l2c from {url}: {e}") from e

    if not resp.content:
        raise RuntimeError(f"Downloaded .l2c from {url} was empty.")
    if not _looks_like_l2c_payload(resp):
        # Nikon returned another HTML page (likely because the token is
        # expired/invalid). Surface a clear error and include the first
        # 200 bytes for diagnostics.
        preview = resp.content[:200].decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"Nikon returned HTML instead of a .l2c file from {url} — "
            f"the request token is probably invalid or expired. "
            f"Response preview: {preview!r}"
        )

    filename = ""
    cd = resp.headers.get("Content-Disposition", "")
    if cd:
        m = _CONTENT_DISP_FILENAME_RE.search(cd)
        if m:
            filename = m.group(1).strip()
    if not filename:
        # Fallback: derive a filename from the URL's request token.
        token = url.rsplit("=", 1)[-1][:16]
        filename = f"unknown_{token}.l2c"

    output_dir.mkdir(parents=True, exist_ok=True)
    saved = output_dir / filename
    saved.write_bytes(resp.content)
    return saved, filename


def _looks_like_l2c_payload(resp: requests.Response) -> bool:
    """Heuristic: is this response the binary .l2c or an HTML page?"""
    content_type = resp.headers.get("Content-Type", "").lower()
    if "text/html" in content_type:
        return False
    cd = resp.headers.get("Content-Disposition", "")
    if ".l2c" in cd.lower():
        return True
    # Nikon's .l2c starts with the XML declaration; HTML pages start with
    # <!DOCTYPE html> or <html>.
    head = resp.content[:512]
    if b"<html" in head.lower() or b"<!doctype html" in head.lower():
        return False
    if head.lstrip().startswith(b"<?xml") and b"HASPUpdate" in resp.content[:2048]:
        return True
    # Final fallback: reasonable-sized binary body with no HTML markers.
    return len(resp.content) > 1024 and not content_type.startswith("text/")


def extract_haspid_from_l2c_filename(filename: str) -> str:
    """Parse the HASP ID from a Nikon .l2c filename like '520D66C9.l2c'.

    Nikon's renewal server names each update file by the HASP ID in hex
    (8 uppercase hex chars, e.g. `09882A98.l2c` for HASPID 159918744).
    Returns the HASP ID in **decimal** to match what ACC reports and
    what state.hasp_id stores.

    Raises RuntimeError if the filename doesn't match the expected
    pattern.
    """
    base = Path(filename).name
    m = _L2C_FILENAME_RE.match(base)
    if not m:
        raise RuntimeError(
            f"Cannot parse HASP ID from filename {base!r}: expected "
            "format '<HEX_HASPID>.l2c' (e.g. '09882A98.l2c'). "
            "Did Nikon change their naming convention?"
        )
    return str(int(m.group(1), 16))


# Defense-in-depth fallback: parse the file contents looking for HASPID
# patterns. Nikon's .l2c wraps Sentinel's <v2c>/<v2cp> XML internally
# (per the strings dump). If the filename-based extractor ever fails
# (e.g. filename mangled by an email-client save), this catches it.
_HASPID_PATTERNS = (
    re.compile(rb'<HASPID[^>]*\svalue="([0-9A-Fa-f]+)"'),
    re.compile(rb"<hasp_id[^>]*>([0-9A-Fa-f]+)</hasp_id>"),
    re.compile(rb"<haspid[^>]*>([0-9A-Fa-f]+)</haspid>"),
    re.compile(rb'<hasp\s+id="([0-9A-Fa-f]+)"'),
)


def extract_haspid_from_l2c_content(l2c_path: Path) -> str:
    """Fallback HASPID parser that inspects the file bytes.

    Returns the HASP ID in **decimal**. Raises RuntimeError if no
    HASPID can be located.
    """
    blob = l2c_path.read_bytes()
    # Strip UTF-16 nulls so a single ASCII regex finds matches regardless
    # of encoding.
    flat = blob.replace(b"\x00", b"")
    for pat in _HASPID_PATTERNS:
        m = pat.search(flat)
        if not m:
            continue
        raw = m.group(1).decode("ascii")
        if len(raw) == 8 or any(c in "abcdefABCDEF" for c in raw):
            return str(int(raw, 16))
        return str(int(raw))
    raise RuntimeError(
        f"Could not locate HASPID in {l2c_path}. The .l2c format may "
        "have changed; please share the file so the parser can be "
        "updated."
    )


def load_checked_emails(
    path: Path | None = None,
) -> dict[str, dict[str, str]]:
    """Read the message-id -> {haspid, checked_at} cache.

    Returns an empty dict if the file is missing, unreadable, or has the
    wrong shape — never raises, so a corrupted cache just causes
    re-downloads, not failure.
    """
    if path is None:
        path = CHECKED_EMAILS_PATH
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    cache: dict[str, dict[str, str]] = {}
    for k, v in data.items():
        if (
            isinstance(k, str)
            and isinstance(v, dict)
            and isinstance(v.get("haspid"), str)
            and isinstance(v.get("checked_at"), str)
        ):
            cache[k] = {"haspid": v["haspid"], "checked_at": v["checked_at"]}
    return cache


def save_checked_emails(
    cache: dict[str, dict[str, str]],
    path: Path | None = None,
    now: datetime | None = None,
) -> None:
    """Atomically write the cache, pruning entries older than the TTL."""
    if path is None:
        path = CHECKED_EMAILS_PATH
    if now is None:
        now = datetime.now().astimezone()

    pruned: dict[str, dict[str, str]] = {}
    cutoff = now - timedelta(days=CHECKED_EMAILS_TTL_DAYS)
    for k, v in cache.items():
        try:
            checked_at = datetime.fromisoformat(v["checked_at"])
        except (KeyError, ValueError):
            continue
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=now.tzinfo)
        if checked_at >= cutoff:
            pruned[k] = v

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(pruned, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def apply_l2c(
    l2c_path: Path,
    rus_exe: Path | None = None,
    *,
    before: LicenseInfo | None = None,
) -> LicenseInfo:
    """Apply `l2c_path` to the local HASP dongle via nis_hasp_update.exe.

    Validates both the exit code AND that the dongle's reported
    expiration actually advanced (re-reads via `get_license_info`).
    Returns the new LicenseInfo on success.

    Raises RuntimeError on any failure. Scans stdout/stderr for known
    RUS rejection strings to produce clearer error messages.
    """
    if not l2c_path.is_file():
        raise RuntimeError(f"l2c file not found: {l2c_path}")
    if rus_exe is None:
        rus_exe = discover_rus_exe()
    if rus_exe is None or not rus_exe.is_file():
        raise RuntimeError(
            "Could not locate nis_hasp_update.exe. Set CITE_RUS_EXE to its "
            "full path, or confirm NIS-Elements is installed under "
            r"C:\Program Files\NIS-Elements*\HASP\."
        )

    if before is None:
        before = get_license_info()

    try:
        result = subprocess.run(
            [str(rus_exe), RUS_APPLY_FLAG, str(l2c_path)],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"{rus_exe.name} timed out after 120s applying {l2c_path.name}."
        ) from e

    combined = (result.stdout or "") + (result.stderr or "")
    rejection_markers = (
        "HL key type mismatch",
        "clone detected",
        "hardware modified",
        "secure storage ID mismatch",
        "missing VLIB",
        "Update is too old",
        "not v2c license file",  # RUS's internal terminology, still relevant
        "HASP_UPDATE_BLOCKED",
        "Update cannot be applied",
    )
    for marker in rejection_markers:
        if marker.lower() in combined.lower():
            raise RuntimeError(
                f"{rus_exe.name} rejected {l2c_path.name}: {marker}. "
                f"Full output: {combined.strip()}"
            )

    if result.returncode != 0:
        raise RuntimeError(
            f"{rus_exe.name} exited {result.returncode} applying "
            f"{l2c_path.name}: {combined.strip() or '(no output)'}"
        )

    after = get_license_info()
    if after.expiration_date <= before.expiration_date:
        raise RuntimeError(
            f"{rus_exe.name} reported success but the dongle's expiration "
            f"did not advance (was {before.expiration_date.isoformat()}, "
            f"now {after.expiration_date.isoformat()}). The .l2c may have "
            "been a no-op or for a different feature."
        )
    return after


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
