"""The runners of ``read_document`` and ``search_files``, which read the chat's files inside the sandboxed job.

The job's working folder is the chat folder: the input files are in it under the paths the api gave,
relative to it. The runners answer in the model's terms, so every path in an answer is the virtual
``/workspace/<chat>/<name>`` path and never the job's own, and every failure is a one-key
``{"error": "<code>: <message>"}``, which the executor reports as a failed tool and the api turns into
an answer for the model. A message never holds a parser's own text, which can hold a path.
"""

import json
import os
import re
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext, suppress
from pathlib import Path
from typing import Any, Final

from workbench import protocol
from workbench.executor import readers
from workbench.executor.context import JobContext
from workbench.executor.timer import Meter, TimeLimit, limit_spending, shielded

#: Bytes of the answer kept back for what is not a part of the text: the path, the kind, the hint.
RESERVE: Final = 2048

#: Seconds in all that a regular expression may spend matching (and compiling), in one search; the reading of
#: the files in between does not count.
REGEX_BUDGET_S: Final = 2.0

#: Most hits one page, cell, paragraph or line gives, so one piece cannot use up all the hits.
HITS_PER_UNIT: Final = 3

MAX_HITS: Final = 200

#: Hits may take this many times ``max_chars``, counted as the job writes them (non-ASCII escaped); the
#: executor reads a result of up to four times it.
HITS_ROOM: Final = 3
SNIPPET: Final = 200
SKIPPED_LISTED: Final = 20
_LISTED_NUMBERS: Final = 20
_DEFAULT_BUDGET_S: Final = 60.0
_READ_BUDGET_S: Final = 60.0

#: One file may use this share of a search's time; a file that takes longer is given up on and named.
FILE_SHARE: Final = 3


class RegexTimeout(TimeLimit):
    """A regular expression has spent its budget."""


def error(code: str, message: str) -> dict[str, Any]:
    """Answer with a failure the api understands.

    Args:
        code: A short name such as ``unsupported_format``.
        message: What the model should know, without a path of the job.

    Returns:
        A one-key result, which the executor reports as a failed tool.
    """
    return {"error": f"{code}: {message}"}


def _numbers(noun: str, numbers: list[int]) -> str:
    """List page or slide numbers for a sentence.

    Args:
        noun: ``page`` or ``slide``.
        numbers: The numbers.

    Returns:
        For example ``page 2``, ``pages 2, 5, 9`` or ``pages 1, 2, 3 and 40 more``.
    """
    shown = ", ".join(str(n) for n in numbers[:_LISTED_NUMBERS])
    more = len(numbers) - _LISTED_NUMBERS
    if more > 0:
        shown += f" and {more} more"
    return f"{noun} {shown}" if len(numbers) == 1 else f"{noun}s {shown}"


def _hint(doc: readers.DocumentText) -> str | None:
    """Say what the model should tell the user about what a read left out.

    Args:
        doc: What the reader returned.

    Returns:
        A hint, or ``None`` if there is nothing to add.
    """
    notes: list[str] = []
    if doc.pages_without_text:
        if doc.kind == "pptx":
            noun, what = "slide", "hold no text (pictures only?)"
            notes.append(f"{_numbers(noun, doc.pages_without_text)} {what}; read_document does no OCR.")
        else:
            noun = "page"
            notes.append(
                f"{_numbers(noun, doc.pages_without_text)} of those looked at have no text layer (scans or "
                "pictures). read_document does no OCR: the file needs OCR elsewhere before they can be read. "
                "Tell the user; do not treat them as blank."
            )
    if doc.cut is not None:
        notes.append(f"{doc.cut} is longer than the result limit and is cut; its rest cannot be read by paging.")
    if not doc.parts and not doc.pages_without_text:
        notes.append("There is nothing to show here: the document or the range is empty.")
    return " ".join(notes) or None


def read_document(args: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
    """Read one file of the chat from ``part`` on.

    Args:
        args: ``path``, relative to the chat folder; ``part``, where to start or ``None``; and ``budget_s``, the
            seconds after which reading stops.
        ctx: The job's surroundings.

    Returns:
        ``path`` (virtual), ``kind``, ``parts``, ``total_parts``, ``pages_without_text``, ``truncated`` and,
        when they apply, ``next_part`` and ``hint``; or an ``error`` result.
    """
    name = protocol.check_input_path(str(args["path"]))
    part = args.get("part")
    virtual = f"/workspace/{ctx.chat_id}/{name}"
    real = ctx.work_dir / name
    if name.split("/")[0] == "out" or not real.is_file():
        return error("file_not_found", f"{virtual} is not in this conversation's workspace")
    budget = float(args.get("budget_s", _READ_BUDGET_S))
    readers.set_deadline(time.monotonic() + budget)
    try:
        doc = readers.read(
            real,
            part=None if part is None else str(part),
            max_chars=max(1, ctx.max_chars - RESERVE),
            file_mb=ctx.file_mb,
        )
    except readers.UnsupportedFormat as exc:
        return error("unsupported_format", str(exc))
    except readers.UnreadableFile as exc:
        return error("unreadable_file", str(exc))
    except readers.NoSuchPart as exc:
        return error("no_such_part", str(exc))
    except readers.ReadTimeout:
        return error("read_timeout", f"reading the file took longer than {budget:g} s; it is too large or too complex")
    finally:
        readers.set_deadline(None)
    result: dict[str, Any] = {
        "path": virtual,
        "kind": doc.kind,
        "parts": [{"label": p.label, "text": p.text} for p in doc.parts],
        "total_parts": doc.total_parts,
        "pages_without_text": doc.pages_without_text,
        "truncated": doc.truncated,
    }
    if doc.next_part is not None:
        result["next_part"] = doc.next_part
    hint = _hint(doc)
    if hint is not None:
        result["hint"] = hint
    return result


def snippet(text: str, match: re.Match[str]) -> str:
    """Cut a snippet of at most ``SNIPPET`` characters around a match.

    Args:
        text: The text the match is in.
        match: The match.

    Returns:
        The match with context on both sides, whitespace collapsed, marked with an ellipsis where it is cut.
    """
    start, end = match.span()
    room = SNIPPET - 2
    length = end - start
    if length >= room:
        return " ".join(text[start : start + room].split()) + "…"
    before = (room - length) // 2
    first = max(0, start - before)
    last = min(len(text), end + (room - length) - (start - first))
    first = max(0, min(first, last - room))
    piece = " ".join(text[first:last].split())
    return ("…" if first > 0 else "") + piece + ("…" if last < len(text) else "")


class Matcher:
    """Finds a query in text; a regular expression may spend ``REGEX_BUDGET_S`` seconds matching, in all.

    The budget is a :class:`~workbench.executor.timer.Meter` that runs only around each match, so the time spent
    reading the files in between does not count, and starting and stopping it costs two clock reads and no
    system call. It is made binding by one :func:`~workbench.executor.timer.limit_spending` block around the
    whole search (:meth:`limit`), in which the readers' own deadlines nest; Python's regular expression engine
    honours the timer while it matches, so one expression that backtracks without end is stopped too.
    """

    def __init__(self, query: str, *, regex: bool) -> None:
        """Compile the query; case is ignored.

        Args:
            query: The text, or the regular expression.
            regex: Whether ``query`` is a regular expression.

        Raises:
            re.error: The expression is not valid.
            RegexTimeout: Compiling it took more than the budget.
            RecursionError: The expression is nested too deeply.
        """
        self.budget: Meter | None = Meter(REGEX_BUDGET_S) if regex else None
        if regex:
            with self.limit():
                self._pattern = self._metered(lambda: re.compile(query, re.IGNORECASE))
        else:
            self._pattern = re.compile(re.escape(query), re.IGNORECASE)

    def limit(self) -> AbstractContextManager[None]:
        """Make the budget binding inside a block; a plain query has none.

        Returns:
            The block; :class:`RegexTimeout` is raised in it once the budget is spent.
        """
        return nullcontext() if self.budget is None else limit_spending(self.budget, RegexTimeout)

    def _metered[T](self, work: Callable[[], T]) -> T:
        """Run ``work`` with the budget's meter running.

        Args:
            work: A function of no arguments.

        Returns:
            What ``work`` returns.

        Raises:
            RegexTimeout: The budget is spent already, or ran out during ``work`` (inside :meth:`limit`).
        """
        budget = self.budget
        if budget is None:
            return work()
        if budget.spent:
            raise RegexTimeout
        budget.start()
        try:
            return work()
        finally:
            budget.stop()

    def _first(self, text: str) -> list[re.Match[str]]:
        """Find the first non-empty matches in a text.

        Args:
            text: The text.

        Returns:
            At most ``HITS_PER_UNIT`` matches, in order.
        """
        found: list[re.Match[str]] = []
        for match in self._pattern.finditer(text):
            if match.end() > match.start():
                found.append(match)
                if len(found) == HITS_PER_UNIT:
                    break
        return found

    def find(self, text: str) -> list[re.Match[str]]:
        """Find the first matches in a text, leaving out empty ones.

        Args:
            text: The text.

        Returns:
            At most ``HITS_PER_UNIT`` matches, in order.

        Raises:
            RegexTimeout: The budget is spent.
        """
        if self.budget is None:
            return self._first(text)
        return self._metered(lambda: self._first(text))


def _files(root: Path) -> list[str]:
    """List the input files in the working folder, below its own ``out/`` folder excepted.

    Args:
        root: The job's working folder.

    Returns:
        The files' paths, relative to ``root``, with ``/``, sorted.
    """
    found: list[str] = []
    for folder, subfolders, names in os.walk(root, followlinks=False):
        here = Path(folder)
        if here == root:
            subfolders[:] = [name for name in subfolders if name != "out"]
        subfolders.sort()
        found.extend((here / name).relative_to(root).as_posix() for name in sorted(names))
    return sorted(found)


class _Search:
    """The state of one search over the chat's files."""

    def __init__(self, ctx: JobContext, matcher: Matcher, max_hits: int, deadline: float) -> None:
        """Start with no hits.

        Args:
            ctx: The job's surroundings.
            matcher: The query.
            max_hits: How many hits the answer may hold.
            deadline: The monotonic time after which no further file is opened.
        """
        self.ctx = ctx
        self.matcher = matcher
        self.max_hits = max_hits
        self.deadline = deadline
        self.per_file = max((deadline - time.monotonic()) / FILE_SHARE, 1.0)
        self.room = ctx.max_chars * HITS_ROOM
        self.crowded = False
        self.hits: list[dict[str, str]] = []
        self.skipped: list[dict[str, str]] = []
        self.searched = 0
        self.unsupported = 0
        self.full = False
        self.timed_out = False
        self.regex_spent = False

    def file(self, name: str) -> None:
        """Search one file.

        Args:
            name: The file's path relative to the working folder.
        """
        real = self.ctx.work_dir / name
        virtual = f"/workspace/{self.ctx.chat_id}/{name}"
        if not readers.supported(real):
            self.unsupported += 1
            return
        self.searched += 1
        blank: list[str] = []
        readers.set_deadline(min(self.deadline, time.monotonic() + self.per_file))
        pieces = readers.units(real, file_mb=self.ctx.file_mb)
        try:
            for unit in pieces:
                if time.monotonic() > self.deadline:
                    self.timed_out = True
                    return
                if not unit.text.strip():
                    blank.append(unit.label)
                    continue
                for match in self.matcher.find(unit.text):
                    hit = {"path": virtual, "location": unit.label, "snippet": snippet(unit.text, match)}
                    self.room -= len(json.dumps(hit)) + 1
                    if len(self.hits) >= self.max_hits or self.room < 0:
                        self.full = len(self.hits) >= self.max_hits
                        self.crowded = not self.full
                        return
                    self.hits.append(hit)
        except (readers.UnreadableFile, readers.UnsupportedFormat) as exc:
            self.skipped.append({"path": virtual, "reason": str(exc)})
            return
        except RegexTimeout:
            self.regex_spent = True
            return
        except readers.ReadTimeout:
            if time.monotonic() > self.deadline:
                self.timed_out = True
            else:
                self.skipped.append({"path": virtual, "reason": f"reading took longer than {self.per_file:g} s"})
            return
        finally:
            # The generator is left suspended inside the reader's time limit when the loop stops early. Close it
            # here, with the signal held back, rather than leave it to be collected when the deadline chooses.
            with shielded(), suppress(readers.ReadTimeout):
                pieces.close()
            readers.set_deadline(None)
        if blank:
            noun = blank[0].split(" ", 1)[0]
            numbers = [int(label.rsplit(" ", 1)[1]) for label in blank]
            self.skipped.append({"path": virtual, "reason": f"no text on {_numbers(noun, numbers)}"})


def search_files(args: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
    """Search every file of the chat for a text or a regular expression.

    Args:
        args: ``query``; ``regex``, whether it is a regular expression; ``max_hits`` (at most 200); and
            ``budget_s``, the seconds after which no further file is opened.
        ctx: The job's surroundings.

    Returns:
        ``hits`` (``path``, ``location``, ``snippet``), ``truncated``, ``files_searched``, ``unsupported_files``
        and, when they apply, ``skipped`` (files that could not be searched, and why) and ``hint``; or an
        ``error`` result. A regular expression that spends its budget ends the search with the hits found until
        then, ``truncated`` and a hint, not with an error.
    """
    query = str(args["query"])
    regex = bool(args.get("regex", False))
    max_hits = max(1, min(int(args.get("max_hits", 50)), MAX_HITS))
    budget = float(args.get("budget_s", _DEFAULT_BUDGET_S))
    try:
        matcher = Matcher(query, regex=regex)
    except re.error as exc:
        return error("bad_regex", f"{exc.msg} at position {exc.pos}")
    except (RecursionError, OverflowError):
        return error("bad_regex", "the expression is too complex")
    except RegexTimeout:
        return error("regex_timeout", f"the expression took more than {REGEX_BUDGET_S:g} s to compile")
    names = _files(ctx.work_dir)
    search = _Search(ctx, matcher, max_hits, time.monotonic() + budget)
    finished = 0
    try:
        with matcher.limit():
            for name in names:
                search.file(name)
                if search.full or search.crowded or search.timed_out or search.regex_spent:
                    break
                finished += 1
    except RegexTimeout:
        # The budget ran out at a moment no file was being matched (a file's own matching is handled in it).
        search.regex_spent = True
    except readers.ReadTimeout:
        # The whole time ran out at a moment no file was being read (a file's own limit is handled in it).
        search.timed_out = True
    result: dict[str, Any] = {
        "hits": search.hits,
        "truncated": search.full or search.crowded or search.timed_out or search.regex_spent,
        "files_searched": search.searched,
        "unsupported_files": search.unsupported,
    }
    if search.skipped:
        result["skipped"] = search.skipped[:SKIPPED_LISTED]
    hints: list[str] = []
    if search.full:
        hints.append("There are more hits than the limit; narrow the query, or raise max_hits (at most 200).")
    if search.crowded:
        hints.append("There are more hits than fit in an answer; narrow the query.")
    if search.timed_out:
        hints.append(f"The search stopped after {budget:g} s; {len(names) - finished} files were not searched.")
    if search.regex_spent:
        hints.append(
            f"regex budget exhausted; use a simpler expression, or a plain text query. The expression may spend "
            f"{REGEX_BUDGET_S:g} s matching in all; these are the hits found until then, and "
            f"{len(names) - finished} files were not searched to the end."
        )
    if len(search.skipped) > SKIPPED_LISTED:
        hints.append(f"{len(search.skipped) - SKIPPED_LISTED} more files could not be searched.")
    if hints:
        result["hint"] = " ".join(hints)
    return result
