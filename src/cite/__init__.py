"""Command line tools for CITE@HMS."""

from importlib.metadata import PackageNotFoundError, version

from ._cleanup import iter_empty_dirs, iter_old_files

try:
    __version__ = version("cite-cli")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0"

__all__ = [
    "__version__",
    "iter_empty_dirs",
    "iter_old_files",
]
