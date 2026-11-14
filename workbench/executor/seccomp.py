"""A seccomp filter that takes sockets away from jobs: no channel to another job's process through the kernel's net.

The executor has no network, but its loopback, its abstract Unix socket names and its netlink families are shared by
every slot. Two jobs that run at once, of two users, could pass a user's files from one to the other through them. The
filter closes every way to such a socket; it is installed in each job's process, between ``fork`` and ``exec``, before
the uid drop, and every process the job starts inherits it:

- ``socket()`` fails with ``EACCES``, whatever the family: Internet, Unix, netlink (``NETLINK_USERSOCK`` lets any
  process of the namespace send to any other) and the rest;
- ``socketpair()`` works for ``AF_UNIX``, which asyncio's self-pipe uses; any other family fails;
- ``bind()`` fails, so no socket gets a name another process could reach;
- ``setsockopt(SOL_SOCKET, SO_PASSCRED | SO_PASSPIDFD | SO_PASSPIDFD_THREAD)`` fails: with one of them, a Unix socket
  that sends or connects names itself with an abstract address (autobind), which another job could reach;
- ``io_uring_setup()`` fails: an io_uring can make and bind sockets past the filter (Docker's default profile refuses it
  too, since Docker 25);
- ``memfd_create()`` fails: memory held in such a file is in no process's resident set, where the supervisor's
  watchdog sums a job's memory.

A connected pair from ``socketpair()`` reaches only the process that made it and its children. The one way to unpair a
datagram end (its peer closes) leaves it nameless, and nobody can send to a nameless socket.

The filter is classic BPF, built here with ``ctypes`` (nothing is installed) and installed with
``prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, …)``. It checks the architecture first: x86_64 and aarch64 are known, with
their own syscall numbers; a call of another architecture (an i386 or x32 call on x86_64, an arm32 call on aarch64)
fails the same way, and the supervisor refuses to start on any other machine (:func:`problem`).
"""

import ctypes
import errno
import platform
import struct
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

# Classic BPF (linux/filter.h, linux/bpf_common.h).
_LD_W_ABS: Final = 0x20  # BPF_LD | BPF_W | BPF_ABS
_JEQ_K: Final = 0x15  # BPF_JMP | BPF_JEQ | BPF_K
_JGE_K: Final = 0x35  # BPF_JMP | BPF_JGE | BPF_K
_RET_K: Final = 0x06  # BPF_RET | BPF_K

# Seccomp (linux/seccomp.h) and prctl (linux/prctl.h).
_RET_ALLOW: Final = 0x7FFF0000
_RET_ERRNO: Final = 0x00050000
_PR_SET_NO_NEW_PRIVS: Final = 38
_PR_SET_SECCOMP: Final = 22
_SECCOMP_MODE_FILTER: Final = 2

# struct seccomp_data: nr (u32) at 0, arch (u32) at 4, args (u64 each) from 16; the low word comes first on both
# architectures, which are little-endian. Every argument checked here is an int, so its low word is all of it.
_NR: Final = 0
_ARCH: Final = 4


def _arg(n: int) -> int:
    """Offset of the low word of syscall argument ``n``."""
    return 16 + 8 * n


_AF_UNIX: Final = 1
_SOL_SOCKET: Final = 1
#: SO_PASSCRED, SO_PASSPIDFD, SO_PASSPIDFD_THREAD (asm-generic/socket.h, the same on both architectures).
_AUTOBINDS: Final = (16, 76, 86)
#: x32 calls on x86_64 carry this bit in their number.
_X32_BIT: Final = 0x40000000

#: The error every refused call gets.
REFUSED: Final = errno.EACCES


@dataclass(frozen=True)
class _Arch:
    """One architecture's numbers.

    Attributes:
        audit: Its ``AUDIT_ARCH_*`` value, as ``seccomp_data.arch`` carries it.
        x32: Whether calls with the x32 bit must be refused (x86_64).
        socket: ``socket``.
        socketpair: ``socketpair``.
        bind: ``bind``.
        setsockopt: ``setsockopt``.
        io_uring_setup: ``io_uring_setup``.
        memfd_create: ``memfd_create``.
    """

    audit: int
    x32: bool
    socket: int
    socketpair: int
    bind: int
    setsockopt: int
    io_uring_setup: int
    memfd_create: int


#: The architectures the executor runs on, by ``platform.machine()``: arch/x86/entry/syscalls/syscall_64.tbl and
#: include/uapi/asm-generic/unistd.h; AUDIT_ARCH_* = EM_* | __AUDIT_ARCH_64BIT | __AUDIT_ARCH_LE.
ARCHES: Final = {
    "x86_64": _Arch(0xC000003E, True, 41, 53, 49, 54, 425, 319),
    "aarch64": _Arch(0xC00000B7, False, 198, 199, 200, 208, 425, 279),
}


def program(arch: _Arch) -> bytes:
    """Build the filter for one architecture, as the bytes of its ``struct sock_filter`` array.

    Args:
        arch: The architecture's numbers.

    Returns:
        The instructions, eight bytes each.
    """
    code: list[tuple[int, int | str, int | str, int]] = []

    def add(op: int, k: int, jt: int | str = 0, jf: int | str = 0) -> None:
        code.append((op, jt, jf, k))

    add(_LD_W_ABS, _ARCH)
    add(_JEQ_K, arch.audit, 0, "deny")
    add(_LD_W_ABS, _NR)
    if arch.x32:
        add(_JGE_K, _X32_BIT, "deny", 0)
    for refused in (arch.socket, arch.bind, arch.io_uring_setup, arch.memfd_create):
        add(_JEQ_K, refused, "deny", 0)
    add(_JEQ_K, arch.socketpair, "pair", 0)
    add(_JEQ_K, arch.setsockopt, "option", 0)
    add(_RET_K, _RET_ALLOW)
    labels = {"pair": len(code)}
    add(_LD_W_ABS, _arg(0))
    add(_JEQ_K, _AF_UNIX, 0, "deny")
    add(_RET_K, _RET_ALLOW)
    labels["option"] = len(code)
    add(_LD_W_ABS, _arg(1))
    add(_JEQ_K, _SOL_SOCKET, 0, "other_option")
    add(_LD_W_ABS, _arg(2))
    for option in _AUTOBINDS:
        add(_JEQ_K, option, "deny", 0)
    labels["other_option"] = len(code)
    add(_RET_K, _RET_ALLOW)
    labels["deny"] = len(code)
    add(_RET_K, _RET_ERRNO | REFUSED)

    def offset(at: int, target: int | str) -> int:
        if isinstance(target, int):
            return target
        jump = labels[target] - at - 1
        if not 0 <= jump <= 255:
            raise ValueError("a jump of the filter is out of range")
        return jump

    return b"".join(
        struct.pack("<HBBI", op, offset(at, jt), offset(at, jf), k) for at, (op, jt, jf, k) in enumerate(code)
    )


class _SockFilter(ctypes.Structure):
    _fields_ = (("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte), ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint))


class _SockFprog(ctypes.Structure):
    _fields_ = (("len", ctypes.c_ushort), ("filter", ctypes.POINTER(_SockFilter)))


class Filter:
    """The filter for this machine, built in the parent so the child only has to call ``prctl``."""

    def __init__(self, arch: _Arch) -> None:
        """Build the program and keep it, with ``libc``, for the child.

        Args:
            arch: The machine's numbers.
        """
        raw = program(arch)
        count = len(raw) // 8
        self._instructions = (_SockFilter * count).from_buffer_copy(raw)
        self._program = _SockFprog(count, ctypes.cast(self._instructions, ctypes.POINTER(_SockFilter)))
        self._libc = ctypes.CDLL(None, use_errno=True)
        self._prctl = self._libc.prctl
        self._prctl.argtypes = (ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong)
        self._prctl.restype = ctypes.c_int

    def install(self) -> None:
        """Set ``no_new_privs`` and install the filter on the calling thread; it holds for every process it starts.

        Call it in the child between ``fork`` and ``exec``. ``no_new_privs`` is what lets a process without
        ``CAP_SYS_ADMIN`` install a filter; the container sets it already.

        Raises:
            OSError: ``prctl`` refused either step; the spawn must fail then.
        """
        if self._prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "no_new_privs could not be set")
        address = ctypes.addressof(self._program)
        if self._prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, address, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "the seccomp filter could not be installed")


def for_this_machine(machine: str | None = None) -> Filter | None:
    """Build the filter for the machine the process runs on.

    Args:
        machine: The machine, as ``platform.machine()`` names it; this one by default.

    Returns:
        The filter on Linux on x86_64 or aarch64; ``None`` anywhere else.
    """
    if sys.platform != "linux":
        return None
    arch = ARCHES.get(platform.machine() if machine is None else machine)
    return None if arch is None else Filter(arch)


#: The probe: under the filter, an Internet socket must be refused with EACCES, and a Unix pair must still be made.
_PROBE = """\
import _socket, sys
try:
    _socket.socket(2, 1)
except OSError as exc:
    if exc.errno != 13:
        sys.exit(4)
else:
    sys.exit(3)
a, b = _socket.socketpair(1, 1)
"""


def problem(make: Callable[[], Filter | None] = for_this_machine) -> str | None:
    """Find a reason the filter cannot keep jobs from sockets on this host, by trying it in a child.

    Args:
        make: Builds the filter.

    Returns:
        A short reason, or ``None`` when a child under the filter cannot open an ``AF_INET`` socket (it gets
        ``EACCES``) and can still make a Unix socket pair.
    """
    built = make()
    if built is None:
        return f"jobs are kept from sockets only on Linux on x86_64 or aarch64, not on {platform.machine() or '?'}"
    try:
        done = subprocess.run(
            [sys.executable, "-I", "-S", "-c", _PROBE],
            env={"PATH": "/usr/bin:/bin"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=built.install,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return "the seccomp filter that keeps jobs from sockets could not be installed"
    if done.returncode != 0:
        return "the seccomp filter does not keep a process from opening an Internet socket"
    return None
