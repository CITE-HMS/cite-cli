"""Command line tools for NIC@HMS."""
from ._cleanup import iter_empty_dirs, iter_old_files

__version__ = "0.1.0"
__all__ = [
    "__version__",
    "iter_old_files",
    "iter_empty_dirs",
]
