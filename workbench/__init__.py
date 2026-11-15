"""workbench: the federation's shared tool tier, a FastAPI tool API plus a sandboxed executor."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("workbench")
except PackageNotFoundError:  # running from source without an installed dist
    __version__ = "0+unknown"
