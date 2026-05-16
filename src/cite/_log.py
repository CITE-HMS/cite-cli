"""Internal rotating log file for cite-cli.

All stdout/stderr produced during a CLI run is tee'd to
~/.cite/logs/cite.log (rotating, 1 MB x 5 backups) in addition to the
terminal.  This lets `cite log` open the logs folder without needing any
external log-redirect in Task Scheduler.

The bootstrap log (the `>> bootstrap.log 2>&1` redirect in Task Scheduler)
covers the rare case where uvx itself fails before Python starts.  The
`cite log` command opens the whole logs directory so both files are visible.
"""

from __future__ import annotations

import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

# Matches ANSI CSI sequences (colours, bold, etc.) so we can strip them from
# the plain-text log file while keeping the terminal output intact.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

LOGS_DIR = Path.home() / ".cite" / "logs"
CITE_LOG = LOGS_DIR / "cite.log"

_LOG_MAX_BYTES = 1 * 1024 * 1024  # 1 MB per file
_LOG_BACKUP_COUNT = 5

_active_handler: RotatingFileHandler | None = None


class _Tee:
    """Write to *stream* and *log_file* simultaneously.

    Not a full TextIO implementation; just enough for sys.stdout/stderr usage.
    Everything else is delegated to the underlying stream via __getattr__.
    """

    def __init__(self, stream: Any, log_file: Any) -> None:
        self._stream: Any = stream
        self._log: Any = log_file

    def write(self, s: str) -> int:
        self._log.write(_ANSI_RE.sub("", s))
        return self._stream.write(s)  # type: ignore[no-any-return]

    def flush(self) -> None:
        self._log.flush()
        self._stream.flush()

    def fileno(self) -> int:
        return self._stream.fileno()  # type: ignore[no-any-return]

    def isatty(self) -> bool:
        return self._stream.isatty()  # type: ignore[no-any-return]

    @property
    def encoding(self) -> str:
        return self._stream.encoding  # type: ignore[no-any-return]

    @property
    def errors(self) -> str | None:
        return self._stream.errors  # type: ignore[no-any-return]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def init_logging() -> None:
    """Tee stdout/stderr to the rotating cite.log for this process.

    Safe to call multiple times (idempotent: won't double-wrap).
    """
    global _active_handler

    if isinstance(sys.stdout, _Tee):
        return  # already initialised

    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    handler = RotatingFileHandler(
        CITE_LOG,
        maxBytes=_LOG_MAX_BYTES,
        backupCount=_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )

    # Accessing handler.stream ensures the file is opened before we tee into it.
    log_stream = handler.stream
    _active_handler = handler

    sys.stdout = _Tee(sys.stdout, log_stream)
    sys.stderr = _Tee(sys.stderr, log_stream)


def close_logging() -> None:
    """Close the active log handler and restore sys.stdout/stderr.

    Intended for tests and orderly shutdown only. Calling this resets the
    module so init_logging() can be called again.
    """
    global _active_handler

    if isinstance(sys.stdout, _Tee):
        sys.stdout = sys.stdout._stream
    if isinstance(sys.stderr, _Tee):
        sys.stderr = sys.stderr._stream

    if _active_handler is not None:
        _active_handler.close()
        _active_handler = None


def open_logs_dir() -> None:
    """Open the ~/.cite/logs/ directory in the system file manager."""
    import subprocess

    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    if sys.platform == "win32":
        import os

        os.startfile(str(LOGS_DIR))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.run(["open", str(LOGS_DIR)], check=False)
    else:
        subprocess.run(["xdg-open", str(LOGS_DIR)], check=False)
