"""The package version is the one declared in ``pyproject.toml``."""

import tomllib
from pathlib import Path

import workbench


def test_version_matches_pyproject() -> None:
    """``workbench.__version__`` equals ``[project].version``."""
    data = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert workbench.__version__ == data["project"]["version"]
