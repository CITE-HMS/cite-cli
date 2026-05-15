"""Local mock HTTP server that mimics the Nikon dealer renewal form."""

from __future__ import annotations

import email
import hashlib
import re
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

INDEX_HTML = (Path(__file__).parent / "index.html").read_bytes()

SUCCESS_HTML = b"""<!DOCTYPE html>
<html><head><title>Submission received</title></head>
<body><h1>Submission received</h1>
<p>Your renewal request was logged by the local mock server.</p></body></html>
"""


def _parse_multipart(body: bytes, content_type: str) -> dict[str, dict[str, Any]]:
    """Parse multipart/form-data into {name: {value|filename+bytes}}.

    Uses email.message_from_bytes so we don't depend on the removed cgi module.
    """
    header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
    msg = email.message_from_bytes(header + body)
    out: dict[str, dict[str, Any]] = {}
    for part in msg.walk():
        if part.is_multipart():
            continue
        cd = part.get("Content-Disposition", "")
        m_name = re.search(r'name="([^"]+)"', cd)
        if not m_name:
            continue
        name = m_name.group(1)
        m_file = re.search(r'filename="([^"]*)"', cd)
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            payload = b""
        if m_file:
            out[name] = {"filename": m_file.group(1), "bytes": payload}
        else:
            out[name] = {"value": payload.decode("utf-8", errors="replace")}
    return out


class _Handler(BaseHTTPRequestHandler):
    log_path: Path  # set by run()

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(INDEX_HTML)))
            self.end_headers()
            self.wfile.write(INDEX_HTML)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("multipart/form-data"):
            self.send_error(400, "Expected multipart/form-data")
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        fields = _parse_multipart(body, ctype)

        summary_lines = [f"--- submission from {self.client_address[0]} ---"]
        for name, info in fields.items():
            if "filename" in info:
                data: bytes = info["bytes"]
                sha = hashlib.sha256(data).hexdigest()
                summary_lines.append(
                    f"{name}: file={info['filename']!r} size={len(data)} sha256={sha}"
                )
            else:
                summary_lines.append(f"{name}: {info['value']!r}")
        summary = "\n".join(summary_lines) + "\n"

        sys.stdout.write(summary)
        sys.stdout.flush()
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(summary)

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(SUCCESS_HTML)))
        self.end_headers()
        self.wfile.write(SUCCESS_HTML)

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"[mock] {self.address_string()} {fmt % args}\n")


@contextmanager
def serving(
    host: str = "127.0.0.1",
    port: int = 8765,
    log_dir: Path | None = None,
) -> Iterator[tuple[str, int]]:
    """Run the mock server in a background thread for the duration of the with-block."""
    log_dir = log_dir or Path.cwd()
    log_dir.mkdir(parents=True, exist_ok=True)
    _Handler.log_path = log_dir / "submissions.log"

    server = ThreadingHTTPServer((host, port), _Handler)
    raw_host, raw_port = server.server_address[:2]
    actual_host = str(raw_host)
    actual_port = int(raw_port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield actual_host, actual_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def run(
    host: str = "127.0.0.1",
    port: int = 8765,
    log_dir: Path | None = None,
) -> None:
    with serving(host=host, port=port, log_dir=log_dir) as (h, p):
        print(f"Mock renewal form listening on http://{h}:{p}/")
        print(f"Submissions logged to: {_Handler.log_path}")
        try:
            threading.Event().wait()  # block until Ctrl-C
        except KeyboardInterrupt:
            print("\nShutting down.")
