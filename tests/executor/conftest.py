"""Fixtures for the executor tests: async tests run on asyncio, real child processes."""

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def anyio_backend() -> str:
    """Run ``anyio``-marked tests on asyncio, the loop the executor's subprocess helpers use."""
    return "asyncio"


@pytest.fixture
def public_tmp() -> Iterator[Path]:
    """A fresh folder under ``/tmp`` that every uid may enter, unlike pytest's own ``0700`` temporary root.

    Root-only tests put a data volume here, so the api's uid and the slot uids can reach what they own
    in it; the folder is removed afterwards. Its path is short, which also suits Unix sockets.
    """
    folder = Path(tempfile.mkdtemp(prefix="wb-", dir="/tmp"))
    folder.chmod(0o755)
    try:
        yield folder
    finally:
        shutil.rmtree(folder, ignore_errors=True)
