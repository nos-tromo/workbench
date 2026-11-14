"""The seccomp filter that keeps jobs from sockets, and the watchdog's measures of a job's folders and memory."""

import errno
import os
import socket
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from workbench.executor import seccomp
from workbench.executor.fs import measure
from workbench.executor.sandbox import slot_memory

LINUX = sys.platform == "linux"
KNOWN = seccomp.for_this_machine() is not None

_RET = 0x06
_ALLOW = 0x7FFF0000
_EACCES = 0x00050000 | errno.EACCES


def instructions(raw: bytes) -> list[tuple[int, int, int, int]]:
    """Decode a filter into ``(code, jt, jf, k)``."""
    return [struct.unpack("<HBBI", raw[at : at + 8]) for at in range(0, len(raw), 8)]


@pytest.mark.parametrize("machine", sorted(seccomp.ARCHES))
def test_the_program_checks_the_architecture_first_and_only_jumps_forward_to_a_return(machine: str) -> None:
    """A call of another architecture is refused; every jump lands inside the program; every path ends in a return."""
    arch = seccomp.ARCHES[machine]
    code = instructions(seccomp.program(arch))
    assert code[0] == (0x20, 0, 0, 4)
    assert code[1][0] == 0x15 and code[1][3] == arch.audit
    assert code[1 + 1 + code[1][2]] == (_RET, 0, 0, _EACCES)
    for at, (op, jt, jf, _) in enumerate(code):
        if op & 0x07 == 0x05:
            assert at + 1 + max(jt, jf) < len(code)
    assert code[-1] == (_RET, 0, 0, _EACCES)
    assert {k for op, _, _, k in code if op == _RET} == {_ALLOW, _EACCES}


def test_x86_64_refuses_x32_calls_and_aarch64_has_none() -> None:
    """x32 calls carry bit 30 in their number on x86_64; aarch64 has no such second table."""
    assert any(
        op == 0x35 and k == 0x40000000 for op, _, _, k in instructions(seccomp.program(seccomp.ARCHES["x86_64"]))
    )
    assert not any(op == 0x35 for op, _, _, _ in instructions(seccomp.program(seccomp.ARCHES["aarch64"])))


def test_the_numbers_are_the_kernels() -> None:
    """From arch/x86/entry/syscalls/syscall_64.tbl and include/uapi/asm-generic/unistd.h, and audit.h."""
    x86, arm = seccomp.ARCHES["x86_64"], seccomp.ARCHES["aarch64"]
    assert (x86.socket, x86.socketpair, x86.bind, x86.setsockopt, x86.memfd_create, x86.io_uring_setup) == (
        41,
        53,
        49,
        54,
        319,
        425,
    )
    assert (arm.socket, arm.socketpair, arm.bind, arm.setsockopt, arm.memfd_create, arm.io_uring_setup) == (
        198,
        199,
        200,
        208,
        279,
        425,
    )
    assert (x86.audit, arm.audit) == (62 | 0xC0000000, 183 | 0xC0000000)


def test_another_machine_gets_no_filter_and_the_supervisor_says_why() -> None:
    """The executor refuses to start where it cannot keep jobs from sockets."""
    assert seccomp.for_this_machine("riscv64") is None
    problem = seccomp.problem(lambda: None)
    assert problem is not None and problem.startswith("jobs are kept from sockets only on Linux on x86_64 or aarch64")


CHILD = """
import asyncio, errno, os, socket, sys
def attempt(action):
    try:
        action()
        return "ok"
    except OSError as exc:
        return errno.errorcode.get(exc.errno, str(exc.errno))
def bound():
    one, two = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    one.bind(b"\\0workbench-test")
def passcred():
    one, two = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    one.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
def other_option():
    one, two = socket.socketpair()
    one.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
async def nothing():
    return "ok"
print(",".join([
    attempt(lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM)),
    attempt(lambda: socket.socket(socket.AF_INET6, socket.SOCK_STREAM)),
    attempt(lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)),
    attempt(bound),
    attempt(passcred),
    attempt(lambda: os.memfd_create("x")),
    attempt(lambda: socket.socketpair()),
    attempt(other_option),
    asyncio.run(nothing()),
]))
"""


@pytest.mark.skipif(not (LINUX and KNOWN), reason="the filter is installed on Linux on x86_64 and aarch64")
def test_under_the_filter_a_process_has_no_socket_but_a_unix_pair_and_asyncio() -> None:
    """R76, in a child of this process: no root needed, so CI's non-root run tries it on x86_64 too."""
    sockets = seccomp.for_this_machine()
    assert sockets is not None
    done = subprocess.run(
        [sys.executable, "-I", "-c", CHILD], preexec_fn=sockets.install, capture_output=True, text=True, check=True
    )
    assert done.stdout.strip().split(",") == ["EACCES"] * 6 + ["ok"] * 3


@pytest.mark.skipif(not (LINUX and KNOWN), reason="the filter is installed on Linux on x86_64 and aarch64")
def test_the_start_up_probe_finds_the_filter_holds() -> None:
    """The supervisor's own probe, on this host."""
    assert seccomp.problem() is None


@pytest.mark.skipif(not LINUX, reason="a socket outside the filter, to tell the filter's refusal from the host's")
def test_this_process_itself_is_not_under_the_filter() -> None:
    """The filter is the job's alone: the supervisor and its helpers keep their sockets (the api's own socket)."""
    socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).close()


# --- the watchdog's measures ---------------------------------------------------------------------------------------


def test_measure_counts_every_entry_and_its_blocks_and_follows_no_link(tmp_path: Path) -> None:
    """Files and folders count; a link counts as itself, and what it points at is never walked."""
    home, out, elsewhere = tmp_path / "home", tmp_path / "out", tmp_path / "elsewhere"
    for folder in (home, out, elsewhere):
        folder.mkdir()
    (home / "tmp").mkdir()
    (home / "tmp" / "a.bin").write_bytes(os.urandom(64 * 1024))
    (out / "r.csv").write_bytes(b"x" * 10)
    for n in range(50):
        (elsewhere / f"f{n}").write_bytes(os.urandom(8192))
    (home / "link").symlink_to(elsewhere)
    fds = [os.open(path, os.O_RDONLY | os.O_DIRECTORY) for path in (home, out)]
    try:
        use = measure(fds, max_entries=1000, max_depth=10)
    finally:
        for fd in fds:
            os.close(fd)
    assert use.entries == 4
    assert use.depth == 1
    assert 64 * 1024 <= use.bytes < 50 * 8192


def test_measure_stops_past_its_bounds(tmp_path: Path) -> None:
    """Past the entry count the walk stops; below the deepest allowed level it enters nothing more."""
    wide = tmp_path / "wide"
    wide.mkdir()
    for n in range(30):
        (wide / f"f{n}").touch()
    deep = tmp_path / "deep"
    here = deep
    for _ in range(12):
        here = here / "d"
    here.mkdir(parents=True)
    for top, kwargs, check in (
        (wide, {"max_entries": 10, "max_depth": 5}, lambda use: use.entries == 11),
        (deep, {"max_entries": 1000, "max_depth": 5}, lambda use: use.depth == 6),
    ):
        fd = os.open(top, os.O_RDONLY | os.O_DIRECTORY)
        try:
            assert check(measure([fd], **kwargs))
        finally:
            os.close(fd)


def test_slot_memory_adds_the_resident_memory_of_the_uids_processes_and_its_unattached_segments(tmp_path: Path) -> None:
    """From a ``/proc`` of three processes and a SysV table: other uids and attached segments are left out."""
    proc = tmp_path / "proc"
    for pid, uid, rss in (("10", 20001, 1000), ("11", 20001, 500), ("12", 20002, 9000)):
        (proc / pid).mkdir(parents=True)
        (proc / pid / "status").write_bytes(
            b"Name:\tpython\nUid:\t%d\t%d\t%d\t%d\nVmRSS:\t   %d kB\n" % (uid, uid, uid, uid, rss)
        )
    (proc / "13").mkdir()
    (proc / "13" / "status").write_bytes(b"Name:\tkthread\nUid:\t20001\t20001\t20001\t20001\n")
    (proc / "sysvipc").mkdir()
    (proc / "sysvipc" / "shm").write_bytes(
        b"       key      shmid perms                  size  cpid  lpid nattch   uid   gid  cuid  cgid      atime"
        b"      dtime      ctime                   rss                  swap\n"
        b"         0          1   600               1048576    10    10      0 20001 20001 20001 20001          0"
        b"          0          0                  4096                  8192\n"
        b"         0          2   600               1048576    10    10      1 20001 20001 20001 20001          0"
        b"          0          0               1048576                     0\n"
    )
    assert slot_memory(20001, proc=proc) == (1000 + 500) * 1024 + 4096 + 8192
