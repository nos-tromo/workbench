"""Fixtures for the executor tests: async tests run on asyncio, real child processes."""

import os
import shutil
import stat
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest


def _ipc_shape() -> str | None:
    """Tell how this host sets up ``/dev/shm`` and POSIX message queues, for the root-only IPC tests.

    Returns:
        ``"production"`` when ``/dev/shm`` is root's and no one else may write it, and
        ``fs.mqueue.queues_max`` is 0, so no slot can leave anything in either (ruling R28);
        ``"shared"`` when every uid may write ``/dev/shm`` and make queues, as in Docker's default
        container, where the defence-in-depth cleanup of both is tested; ``None`` otherwise, or
        where there is no ``/dev/shm``.
    """
    try:
        shm = os.lstat("/dev/shm")
        queues_max = Path("/proc/sys/fs/mqueue/queues_max").read_text(encoding="ascii").strip()
    except OSError:
        return None
    if not stat.S_ISDIR(shm.st_mode):
        return None
    if shm.st_uid == 0 and shm.st_mode & 0o022 == 0 and queues_max == "0":
        return "production"
    if shm.st_mode & 0o002 and queues_max != "0":
        return "shared"
    return None


IPC_SHAPE = _ipc_shape()
SHARED_IPC_ONLY = pytest.mark.skipif(
    IPC_SHAPE != "shared",
    reason="the defence-in-depth cleanup of /dev/shm and /dev/mqueue needs a host where slots can write "
    "both (Docker's default); the production shape refuses that (R28)",
)


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
