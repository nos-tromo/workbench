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
from workbench.executor.sandbox import JOB_OOM_SCORE_ADJ, SlotUse, slots_use

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
    """From arch/x86/entry/syscalls/syscall_64.tbl and include/uapi/asm-generic/unistd.h, and audit.h.

    Checked against linux-libc-dev 6.12.117 (asm/unistd_64.h of x86_64-linux-gnu, asm/unistd.h of aarch64-linux-gnu):
    mmap 9 and 222 (``__NR3264_mmap``), memfd_secret 447 on both.
    """
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
    assert (x86.memfd_secret, x86.mmap, arm.memfd_secret, arm.mmap) == (447, 9, 447, 222)
    assert (x86.audit, arm.audit) == (62 | 0xC0000000, 183 | 0xC0000000)


def run_filter(raw: bytes, arch: int, nr: int, *args: int) -> int:
    """Run a classic BPF seccomp program on one call, as the kernel would, and give its return value.

    Only the instructions ``seccomp.program`` uses are known; any other fails the test. ``struct seccomp_data`` is
    ``nr`` (u32), ``arch`` (u32), ``instruction_pointer`` (u64) and six u64 arguments, little-endian.
    """
    data = struct.pack("<IIQ6Q", nr, arch, 0, *(list(args) + [0] * (6 - len(args))))
    code = instructions(raw)
    accumulator, at = 0, 0
    while True:
        op, jt, jf, k = code[at]
        if op == 0x20:  # LD W ABS
            (accumulator,) = struct.unpack_from("<I", data, k)
        elif op == 0x54:  # ALU AND K
            accumulator &= k
        elif op == 0x15:  # JEQ K
            at += jt if accumulator == k else jf
        elif op == 0x35:  # JGE K
            at += jt if accumulator >= k else jf
        elif op == _RET:
            return k
        else:
            raise AssertionError(f"unknown instruction {op:#x}")
        at += 1


MAP_SHARED, MAP_PRIVATE, MAP_SHARED_VALIDATE, MAP_ANONYMOUS = 0x01, 0x02, 0x03, 0x20


@pytest.mark.parametrize("machine", sorted(seccomp.ARCHES))
def test_the_program_refuses_shared_anonymous_maps_and_memfds_and_nothing_else_of_mmap(machine: str) -> None:
    """R85, both machines' bytes, simulated: two bits in the low word of mmap's flags decide; the rest is allowed."""
    arch = seccomp.ARCHES[machine]
    raw = seccomp.program(arch)

    def mmap_with(flags: int) -> int:
        return run_filter(raw, arch.audit, arch.mmap, 0, 4096, 3, flags, 2**64 - 1, 0)

    assert mmap_with(MAP_SHARED | MAP_ANONYMOUS) == _EACCES
    assert mmap_with(MAP_SHARED_VALIDATE | MAP_ANONYMOUS) == _EACCES
    assert mmap_with(MAP_SHARED | MAP_ANONYMOUS | 0x4000) == _EACCES  # with MAP_NORESERVE
    assert mmap_with((1 << 32) | MAP_SHARED | MAP_ANONYMOUS) == _EACCES
    assert mmap_with(MAP_PRIVATE | MAP_ANONYMOUS) == _ALLOW
    assert mmap_with(MAP_SHARED) == _ALLOW  # a file
    assert mmap_with(MAP_PRIVATE) == _ALLOW
    assert mmap_with((1 << 32) | MAP_PRIVATE | MAP_ANONYMOUS) == _ALLOW
    assert run_filter(raw, arch.audit, arch.memfd_secret, 0) == _EACCES
    assert run_filter(raw, arch.audit, arch.memfd_create, 0, 0) == _EACCES
    assert run_filter(raw, arch.audit, arch.socket, 1, 1, 0) == _EACCES
    assert run_filter(raw, arch.audit, arch.socketpair, 1, 1, 0) == _ALLOW
    assert run_filter(raw, arch.audit, arch.socketpair, 2, 1, 0) == _EACCES
    assert run_filter(raw, arch.audit, arch.setsockopt, 3, 1, 16) == _EACCES
    assert run_filter(raw, arch.audit, arch.setsockopt, 3, 1, 7) == _ALLOW
    for other in (0, 1, 63, 64, 220, 221, 223, 446, 448):  # read, write, … and mmap's neighbours on both machines
        if other not in (arch.socket, arch.socketpair, arch.bind, arch.setsockopt, arch.mmap):
            assert run_filter(raw, arch.audit, other, 0, 0, 0, MAP_SHARED | MAP_ANONYMOUS) == _ALLOW, other
    foreign = seccomp.ARCHES["aarch64" if machine == "x86_64" else "x86_64"].audit
    assert run_filter(raw, foreign, arch.mmap, 0, 4096, 3, MAP_PRIVATE | MAP_ANONYMOUS) == _EACCES


def test_another_machine_gets_no_filter_and_the_supervisor_says_why() -> None:
    """The executor refuses to start where it cannot keep jobs from sockets."""
    assert seccomp.for_this_machine("riscv64") is None
    problem = seccomp.problem(lambda: None)
    assert problem is not None and problem.startswith("jobs are kept from sockets only on Linux on x86_64 or aarch64")


CHILD = """
import asyncio, ctypes, errno, mmap, os, socket, sys
libc = ctypes.CDLL(None, use_errno=True)
def secret():
    if libc.syscall(447, 0) < 0:
        raise OSError(ctypes.get_errno(), "memfd_secret")
def attempt(action):
    try:
        action()
        return "ok"
    except OSError as exc:
        return errno.errorcode.get(exc.errno, str(exc.errno))
def bound():
    one, two = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    one.bind(b"\\0workbench-test")
def shared_zero():
    fd = os.open("/dev/zero", os.O_RDONLY)
    mmap.mmap(fd, 4096, mmap.MAP_SHARED, mmap.PROT_READ)
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
    attempt(lambda: mmap.mmap(-1, 4096)),
    attempt(lambda: mmap.mmap(-1, 4096, flags=mmap.MAP_SHARED | mmap.MAP_ANONYMOUS | mmap.MAP_POPULATE)),
    attempt(secret),
    attempt(lambda: socket.socketpair()),
    attempt(other_option),
    attempt(lambda: mmap.mmap(-1, 4096, flags=mmap.MAP_PRIVATE)),
    attempt(shared_zero),
    asyncio.run(nothing()),
]))
"""


@pytest.mark.skipif(not (LINUX and KNOWN), reason="the filter is installed on Linux on x86_64 and aarch64")
def test_under_the_filter_a_process_has_no_socket_but_a_unix_pair_and_asyncio() -> None:
    """R76, R85, in a child of this process: no root needed, so CI's non-root run tries it on x86_64 too.

    Neither a shared anonymous map nor a secret memfd; a private anonymous map and a read-only shared map of
    ``/dev/zero`` (which holds no memory) work.
    """
    sockets = seccomp.for_this_machine()
    assert sockets is not None
    done = subprocess.run(
        [sys.executable, "-I", "-c", CHILD], preexec_fn=sockets.install, capture_output=True, text=True, check=True
    )
    assert done.stdout.strip().split(",") == ["EACCES"] * 9 + ["ok"] * 5


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


def _open_descriptors() -> int:
    """How many descriptors this process holds, as its own descriptor folder lists them."""
    return len(os.listdir("/proc/self/fd" if LINUX else "/dev/fd"))


def test_measure_closes_what_it_opened_when_a_folder_cannot_be_listed(tmp_path: Path) -> None:
    """R86 N3: a top that cannot be listed raises, and the walk's own copy of its descriptor is closed again."""
    plain = tmp_path / "not-a-folder"
    plain.write_bytes(b"x")
    fd = os.open(plain, os.O_RDONLY)
    try:
        before = _open_descriptors()
        for _ in range(3):
            with pytest.raises(NotADirectoryError):
                measure([fd], max_entries=10, max_depth=5)
        assert _open_descriptors() == before
    finally:
        os.close(fd)


SHM_HEAD = (
    b"       key      shmid perms                  size  cpid  lpid nattch   uid   gid  cuid  cgid      atime"
    b"      dtime      ctime                   rss                  swap\n"
)


def _shm_row(shmid: int, nattch: int, owner: int, maker: int, rss: int, swap: int) -> bytes:
    """One segment of ``/proc/sysvipc/shm``, its columns as the kernel writes them."""
    cells = (0, shmid, 600, 1048576, 10, 10, nattch, owner, owner, maker, maker, 0, 0, 0, rss, swap)
    return b" ".join(b"%d" % cell for cell in cells) + b"\n"


def _proc(tmp_path: Path, processes: list[tuple[str, int, int | None, int, int]], segments: bytes = b"") -> Path:
    """A ``/proc`` of processes ``(pid, uid, VmRSS kB or None, RssShmem kB, oom_score_adj)`` and a SysV table."""
    proc = tmp_path / "proc"
    for pid, uid, rss, shmem, score in processes:
        (proc / pid).mkdir(parents=True)
        lines = b"Name:\tpython\nUid:\t%d\t%d\t%d\t%d\n" % (uid, uid, uid, uid)
        if rss is not None:
            lines += b"VmRSS:\t   %d kB\nRssAnon:\t   %d kB\nRssShmem:\t   %d kB\n" % (rss, rss - shmem, shmem)
        (proc / pid / "status").write_bytes(lines)
        (proc / pid / "oom_score_adj").write_bytes(b"%d\n" % score)
    (proc / "sysvipc").mkdir()
    (proc / "sysvipc" / "shm").write_bytes(SHM_HEAD + segments)
    return proc


def test_slots_use_counts_each_process_without_its_shared_memory_and_every_segment_once(tmp_path: Path) -> None:
    """R85: a segment counts against the uid that made it, attached or not; ``RssShmem`` is not counted twice.

    Segment 2 is attached (``nattch`` 1), as when a process filled it and forked a child that keeps it while the parent
    detaches: its pages are in no resident set, and the watchdog before R85 left it out.
    """
    proc = _proc(
        tmp_path,
        [
            ("10", 20001, 1000, 0, 1000),
            ("11", 20001, 1500, 1000, 1000),
            ("12", 20002, 9000, 0, 1000),
            ("13", 20001, None, 0, 1000),
        ],
        _shm_row(1, 0, 20001, 20001, 4096, 8192) + _shm_row(2, 1, 20002, 20001, 1048576, 0),
    )

    use = slots_use([20001, 20003], proc=proc)

    assert use == {
        20001: SlotUse(memory=(1000 + 500) * 1024 + 4096 + 8192 + 1048576, lowered=False),
        20003: SlotUse(),
    }


def test_slots_use_looks_at_proc_once_for_every_slot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R86 N6: one listing of ``/proc`` and one read of the SysV table per pass, however many slots run."""
    proc = _proc(tmp_path, [("10", 20001, 100, 0, 1000), ("11", 20002, 200, 0, 1000)])
    listings: list[object] = []
    real = os.scandir

    def counted(path: object) -> object:
        listings.append(path)
        return real(path)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "scandir", counted)

    use = slots_use([20001, 20002, 20003], proc=proc)

    assert listings == [proc]
    assert (use[20001].memory, use[20002].memory, use[20003].memory) == (100 * 1024, 200 * 1024, 0)


def test_a_slot_process_below_the_jobs_oom_score_is_found(tmp_path: Path) -> None:
    """R86 N7: ``oom_score_adj`` is world-readable; a slot process below 1000 lowered it, and the look says so."""
    proc = _proc(
        tmp_path,
        [("10", 20001, 100, 0, JOB_OOM_SCORE_ADJ), ("11", 20001, 100, 0, 0), ("12", 20002, 100, 0, JOB_OOM_SCORE_ADJ)],
    )

    use = slots_use([20001, 20002], proc=proc)

    assert (use[20001].lowered, use[20002].lowered) == (True, False)
