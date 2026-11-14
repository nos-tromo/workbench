"""The runner of ``run_python``: the model's code, run in the job process, which is already the sandbox.

The job runs under a slot uid with its limits and no network, in a private copy of the chat's files, so the
code runs right here: compiled and ``exec``-ed as ``__main__`` in a fresh module, in the job's working folder,
with ``matplotlib``'s ``Agg`` backend set and a time limit below the job's wall time. Its standard output and
error go to buffers that keep the start and the end of each, half of the result limit apiece, however much it
prints. Whatever the code raises, ``SystemExit`` and ``KeyboardInterrupt`` included, becomes its traceback in
``stderr``, so the answer is always ``{"stdout", "stderr", "truncated"}`` and, when it applies, ``hint``. The
files it writes into ``out/`` are the job's outputs, which the supervisor collects; the runner does not list them.

The answer never names the job's real folder. ``<data>/jobs/<id>/workspace/<chat>`` is written as
``/workspace/<chat>``, the path the code itself can use (the executor image links ``/workspace`` to the parent of
the working folder), the rest of the job folder as ``<job>``, and any other path on the data volume as ``<data>``.
"""

import builtins
import collections
import io
import json
import linecache
import os
import re
import sys
import threading
import time
import traceback
import types
from collections.abc import Callable, Iterator
from contextlib import suppress
from pathlib import Path
from typing import Any, Final, override

from workbench.executor.context import JobContext
from workbench.executor.timer import TimeLimit, limit_until, uninterrupted

#: The file name the code is compiled under; tracebacks show it, with the code's own lines.
FILENAME: Final = "<code>"

#: What a virtual path starts with.
VIRTUAL_ROOT: Final = "/workspace"

_DEFAULT_BUDGET_S: Final = 60.0

#: The longest path Linux takes. Each stream keeps this much more than it shows on each side of a gap, so that
#: every path that reaches into what is shown is whole where it is rewritten.
_PATH_MAX: Final = 4096

#: A character that can go on a folder name here: a longer name is another folder.
_NAME_GOES_ON: Final = r"(?![\w.:-])"

_QUOTED_PATH: Final = re.compile(r"'(/[^']*)'")

#: The folder of the ``workbench`` package; frames of its code are the runner's or the timer's, not the code's.
_PACKAGE: Final = str(Path(__file__).resolve().parents[2]) + os.sep


class CodeTimeout(TimeLimit):
    """The code ran past its share of the job's wall time."""


class _Capture(io.TextIOBase):
    """A text stream that keeps the first and the last ``keep`` characters written to it and counts the rest.

    Writing costs at most two slices of what is written, whatever its size, so code that prints without end
    uses no more memory than the one string it prints. Code may write from many threads at once: each write is
    whole. The code's time limit waits for the end of the bookkeeping (:func:`~workbench.executor.timer.uninterrupted`).
    The lock is reentrant, so a signal handler of the code's that prints while the thread it interrupted is writing
    cannot deadlock it, and what the handler prints is taken on by the interrupted write once that is whole, after its
    own text. A handler of the code's own that raises (``signal.alarm(n)`` with ``raise TimeoutError``) does end a
    write half done, as the code asked, and some of that write's text may be lost; the sizes the stream counts never
    exceed what it holds, so no later write fails over it.

    Attributes:
        encoding: The stream's encoding, for code that asks before it writes.
        written: Characters written in all.
    """

    encoding: str = "utf-8"

    def __init__(self, keep: int) -> None:
        """Start empty.

        Args:
            keep: Characters kept at the start, and as many at the end.
        """
        super().__init__()
        self._lock = threading.RLock()
        self._keep = keep
        self._head: list[str] = []
        self._head_size = 0
        self._tail: collections.deque[str] = collections.deque()
        self._tail_size = 0
        self._writing = False
        self._later: list[str] = []
        self.written = 0

    @override
    def writable(self) -> bool:
        """Tell that the stream takes writes.

        Returns:
            ``True``.
        """
        return True

    @override
    def write(self, s: str) -> int:
        """Keep what is written within the head and the tail.

        Args:
            s: The text.

        Returns:
            Its length, as a file's ``write`` does.

        Raises:
            TypeError: It is not text.
            ValueError: The stream was closed.
        """
        if not isinstance(s, str):
            raise TypeError(f"write() argument must be str, not {type(s).__name__}")
        if self.closed:
            raise ValueError("I/O operation on closed file.")
        with self._lock:
            if self._writing:
                self._later.append(s)  # a signal handler's: the write it interrupted takes it on
            else:
                self._writing = True
                try:
                    self._take(s)
                finally:
                    self._writing = False
        return len(s)

    @uninterrupted
    def _take(self, s: str) -> None:
        """Keep a text, then whatever a signal handler wrote meanwhile, within the head and the tail.

        Args:
            s: The text.
        """
        while True:
            size = len(s)
            self.written += size
            start = 0
            room = self._keep - self._head_size
            if room > 0 and size:
                piece = s[:room]
                self._head.append(piece)
                self._head_size += len(piece)
                start = len(piece)
            if start < size:
                piece = s[max(start, size - self._keep) :]
                self._tail.append(piece)
                self._tail_size += len(piece)
                while self._tail_size - len(self._tail[0]) >= self._keep:
                    # The size goes down before the piece goes: an error that lands between the two leaves the size
                    # short of the pieces, never over them, and the loop never runs out of pieces.
                    self._tail_size -= len(self._tail[0])
                    self._tail.popleft()
            if not self._later:
                return
            s = self._later.pop(0)

    def parts(self) -> tuple[str, str, int]:
        """Tell what is kept.

        Returns:
            The head, the tail, and how many characters written between them are not kept.
        """
        with self._lock:
            head = "".join(self._head)
            tail = "".join(self._tail)[-self._keep :] if self._tail else ""
            return head, tail, self.written - len(head) - len(tail)


class _Rewriter:
    """Writes the job's real paths as the code and the model see them, also for a piece of a longer text.

    The working folder is ``<data>/jobs/<id>/workspace/<chat>`` (the supervisor's layout). The rules are one
    pattern, tried in order at each place: the chat folder, the job's ``workspace``, the job folder, then any path
    on the data volume. A rewritten path is always kept whole, so where a text is cut a path is either in it whole
    and rewritten or not in it at all.
    """

    def __init__(self, work_dir: Path, chat_id: str) -> None:
        """Build the rules for one job.

        Args:
            work_dir: The job's working folder.
            chat_id: The chat it holds.
        """
        job = work_dir.parent.parent
        data = job.parent.parent
        rules = [
            (re.escape(str(work_dir)) + _NAME_GOES_ON, f"{VIRTUAL_ROOT}/{chat_id}"),
            (re.escape(str(work_dir.parent)) + _NAME_GOES_ON, VIRTUAL_ROOT),
            (re.escape(str(job)) + _NAME_GOES_ON, "<job>"),
        ]
        if data != data.parent:
            rules.append((r"(?<![\w.:/-])" + re.escape(str(data)) + _NAME_GOES_ON + r"[^\s'\"]*", "<data>"))
        self._pattern = re.compile("|".join(f"({pattern})" for pattern, _ in rules))
        self._virtual = [virtual for _, virtual in rules]

    def _pieces(self, text: str) -> Iterator[tuple[int, int, str, bool]]:
        """Split a text into the stretches between real paths and the real paths, each with what it becomes.

        Args:
            text: The text.

        Yields:
            ``(start, end, output, is_path)`` in order; ``output`` is the stretch itself or the path rewritten.
        """
        at = 0
        for match in self._pattern.finditer(text):
            if match.start() > at:
                yield at, match.start(), text[at : match.start()], False
            yield match.start(), match.end(), self._virtual[(match.lastindex or 1) - 1], True
            at = match.end()
        if at < len(text):
            yield at, len(text), text[at:], False

    def __call__(self, text: str) -> str:
        """Rewrite a whole text.

        Args:
            text: The text.

        Returns:
            It with every real path rewritten.
        """
        return "".join(output for _, _, output, _ in self._pieces(text))

    def before(self, text: str, end: int) -> tuple[str, int]:
        """Rewrite the start of a text, up to ``end``, keeping a path that begins before ``end`` whole.

        Args:
            text: The text, with every path that begins before ``end`` whole in it.
            end: Where the start ends, in ``text``.

        Returns:
            The start rewritten, and where in ``text`` it ends.
        """
        kept: list[str] = []
        reached = 0
        for start, stop, output, is_path in self._pieces(text):
            if start >= end:
                break
            kept.append(output if is_path else output[: end - start])
            reached = stop if is_path else min(stop, end)
        return "".join(kept), reached

    def after(self, text: str, begin: int) -> tuple[str, int]:
        """Rewrite the end of a text, from ``begin`` on, keeping a path that ends after ``begin`` whole.

        Args:
            text: The text, with every path that ends after ``begin`` whole in it.
            begin: Where the end begins, in ``text``.

        Returns:
            The end rewritten, and where in ``text`` it begins.
        """
        kept: list[str] = []
        begun = len(text)
        for start, stop, output, is_path in self._pieces(text):
            if stop <= begin:
                continue
            first = start if is_path else max(start, begin)
            kept.append(output if is_path else output[first - start :])
            begun = min(begun, first)
        return "".join(kept), begun


def _marked(head: str, tail: str, cut: int) -> str:
    """Join the two ends of a text around a mark that says how much is left out.

    Args:
        head: The start.
        tail: The end.
        cut: Characters left out.

    Returns:
        The text with the mark.
    """
    return f"{head}\n[… {cut} characters cut …]\n{tail}"


def _encodable(text: str) -> str:
    """Make a text that UTF-8 can hold: surrogate halves written apart are joined, a lone one is replaced.

    Code can print a lone surrogate (a name decoded with ``surrogateescape``, say); the job would write it, but the
    supervisor could not send the result on, and the run's output and files would be lost.

    Args:
        text: What the code printed.

    Returns:
        The text, with ``U+FFFD`` for each surrogate that has no partner.
    """
    return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def _kept(capture: _Capture, rewrite: _Rewriter, limit: int) -> tuple[str, bool]:
    """Turn what a stream kept into the text of the answer, within ``limit`` as the job writes it.

    The stream keeps ``_PATH_MAX`` characters more than ``limit`` at each end. Nothing written was left out: the
    text is rewritten whole, so no path is ever split where the two ends meet. Some was: the first ``limit``
    characters of the head and the last ``limit`` of the tail are rewritten from the ends as kept, which hold every
    path that reaches into them whole; the margin beyond is left out. Only then is the rewritten text cut, in the
    middle, to the limit.

    Args:
        capture: The stream; it keeps ``limit + _PATH_MAX`` characters at each end.
        rewrite: Writes real paths as virtual ones.
        limit: Most characters of the text's JSON, escapes included (the job writes ASCII JSON).

    Returns:
        The text, and whether some of it was cut.
    """
    head, tail, gap = capture.parts()
    if not gap:
        whole = rewrite(_encodable(head + tail))
        if len(json.dumps(whole)) <= limit:
            return whole, False
        middle = len(whole) // 2
        head, tail = whole[:middle], whole[middle:]
    else:
        raw_head, raw_tail = _encodable(head), _encodable(tail)
        head, reached = rewrite.before(raw_head, max(0, len(raw_head) - _PATH_MAX))
        tail, begun = rewrite.after(raw_tail, min(len(raw_tail), _PATH_MAX))
        gap += len(raw_head) - reached + begun
    best = _marked("", "", gap + len(head) + len(tail))
    low, high = 0, max(len(head), len(tail))
    while low <= high:
        keep = (low + high) // 2
        front, back = head[:keep], tail[max(0, len(tail) - keep) :]
        candidate = _marked(front, back, gap + len(head) - len(front) + len(tail) - len(back))
        if len(json.dumps(candidate)) <= limit:
            best, low = candidate, keep + 1
        else:
            high = keep - 1
    return best, True


def _use_agg() -> None:
    """Load matplotlib with its non-interactive backend, before the code and before its output is captured.

    Loading it here sends what matplotlib logs while it sets up (a cache folder it had to make) to the job's own
    stderr, which nobody reads, instead of into the answer.
    """
    import matplotlib

    matplotlib.use("Agg")


def _run(code: str, work_dir: Path, budget: float, out: _Capture, err: _Capture) -> BaseException | None:
    """Run the code as ``__main__`` in a module of its own, with its output captured, and put the process back.

    Args:
        code: The code.
        work_dir: The working folder, the code's cwd.
        budget: Seconds the code may run.
        out: Its standard output.
        err: Its standard error.

    Returns:
        What the code raised, or ``None`` if it ran to its end.
    """
    main = types.ModuleType("__main__")
    main.__dict__["__builtins__"] = builtins
    saved = sys.stdout, sys.stderr, sys.modules.get("__main__"), sys.argv
    linecache.cache[FILENAME] = (len(code), None, code.splitlines(keepends=True), FILENAME)
    os.chdir(work_dir)
    sys.stdout, sys.stderr, sys.modules["__main__"], sys.argv = out, err, main, [FILENAME]
    try:
        compiled = compile(code, FILENAME, "exec", dont_inherit=True)
        with limit_until(time.monotonic() + budget, CodeTimeout):
            exec(compiled, main.__dict__)
    except BaseException as exc:  # everything the code raises, SystemExit and the time limit too, is its answer
        return exc
    finally:
        sys.stdout, sys.stderr, previous, sys.argv = saved
        if previous is None:
            sys.modules.pop("__main__", None)
        else:
            sys.modules["__main__"] = previous
        with suppress(OSError):
            os.chdir(work_dir)
    return None


def _from_the_code(tb: types.TracebackType | None) -> types.TracebackType | None:
    """Skip the runner's own frames at the top of a traceback.

    Args:
        tb: The traceback of what the code raised.

    Returns:
        The traceback from the code's first frame on, or ``None`` if none of it is the code's.
    """
    while tb is not None and tb.tb_frame.f_globals is globals():
        tb = tb.tb_next
    return tb


def _describe(error: BaseException, budget: float) -> str:
    """Write what the code raised as Python would print it, without the runner's frames.

    Args:
        error: What it raised.
        budget: Seconds it was given.

    Returns:
        The traceback, ending with the error; ``SystemExit`` and a syntax error as their last line alone.
    """
    tb = _from_the_code(error.__traceback__)
    if isinstance(error, SystemExit) or (isinstance(error, SyntaxError) and tb is None):
        return "".join(traceback.format_exception_only(error))
    if isinstance(error, TimeLimit):
        frames = [frame for frame in traceback.extract_tb(tb) if not frame.filename.startswith(_PACKAGE)]
        trace = "Traceback (most recent call last):\n" + "".join(traceback.format_list(frames)) if frames else ""
        return f"{trace}timed out: the code was stopped after {budget:g} s, its share of the job's wall time\n"
    return "".join(traceback.format_exception(type(error), error, tb))


def _paths_of(error: OSError) -> list[str]:
    """Take the paths an error names.

    Args:
        error: The error.

    Returns:
        Its file names, or the quoted paths in its message if it carries none.
    """
    names: list[Any] = [name for name in (error.filename, error.filename2) if name is not None]
    if not names:
        names = list(_QUOTED_PATH.findall(str(error)))
    paths: list[str] = []
    for name in names:
        with suppress(TypeError):
            paths.append(os.fsdecode(name))
    return paths


def _names_another_chat(error: BaseException, chat_id: str, rewrite: Callable[[str], str]) -> bool:
    """Tell whether the code failed on a file of another chat, which is not in this job.

    Args:
        error: What the code raised; the errors it was raised from or during are looked at too.
        chat_id: This job's chat.
        rewrite: Writes real paths as virtual ones.

    Returns:
        Whether one of them is a ``FileNotFoundError`` for a path below ``/workspace/`` in another chat folder.
    """
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, FileNotFoundError):
            for path in map(rewrite, _paths_of(current)):
                parts = path.split("/")
                if path.startswith(f"{VIRTUAL_ROOT}/") and len(parts) > 2 and parts[2] not in (chat_id, ""):
                    return True
        current = current.__cause__ or current.__context__
    return False


def run_python(args: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
    """Run the model's code on the chat's files and answer with what it printed.

    Args:
        args: ``code``, the Python to run; ``budget_s``, the seconds it may run before it is stopped.
        ctx: The job's surroundings; ``max_chars`` caps the answer, half of it for each stream.

    Returns:
        ``stdout``, ``stderr`` (the traceback if the code raised, or a note that it timed out), ``truncated``
        and, when the code looked for a file of another chat, ``hint``.
    """
    code = str(args["code"])
    budget = float(args.get("budget_s", _DEFAULT_BUDGET_S))
    keep = max(1, ctx.max_chars // 2)
    rewrite = _Rewriter(ctx.work_dir, ctx.chat_id)
    _use_agg()
    out, err = _Capture(keep + _PATH_MAX), _Capture(keep + _PATH_MAX)
    pid = os.getpid()
    error = _run(code, ctx.work_dir, budget, out, err)
    if os.getpid() != pid:
        # A process the code forked has run the code to its end: only the job's own process answers.
        os._exit(0 if error is None else 1)
    hint: str | None = None
    if error is not None:
        err.write(_describe(error, budget))
        if _names_another_chat(error, ctx.chat_id, rewrite):
            hint = (
                f"Only this chat's files are available: the code finds them in its folder and under "
                f"{VIRTUAL_ROOT}/{ctx.chat_id}/. Files of the user's other chats are not given to it."
            )
    stdout, cut_out = _kept(out, rewrite, keep)
    stderr, cut_err = _kept(err, rewrite, keep)
    result: dict[str, Any] = {"stdout": stdout, "stderr": stderr, "truncated": cut_out or cut_err}
    if hint is not None:
        result["hint"] = hint
    return result
