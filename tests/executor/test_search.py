"""The ``read_document`` and ``search_files`` runners: what the model is told, and how a search is bounded."""

import json
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import scaled
from tests.fixtures import make_fixtures as fx
from workbench.executor import readers, timer
from workbench.executor.runners import RUNNERS, JobContext
from workbench.executor.runners import documents as docs

CHAT = "chat-1"


@pytest.fixture
def work(tmp_path: Path) -> Path:
    """The job's working folder: the chat folder, with the job's own empty ``out/`` in it."""
    folder = tmp_path / "job" / "workspace" / CHAT
    (folder / "out").mkdir(parents=True)
    return folder


def context(work: Path, *, max_chars: int = 16384, file_mb: int = 50) -> JobContext:
    """The context the job entry point builds for a working folder."""
    return JobContext(work_dir=work, out_dir=work / "out", chat_id=CHAT, max_chars=max_chars, file_mb=file_mb)


def search(work: Path, query: str, **args: Any) -> dict[str, Any]:
    """Run the search runner on a working folder."""
    return RUNNERS["search_files"]({"query": query, **args}, context(work))


def read_doc(work: Path, path: str, part: str | None = None, **ctx: Any) -> dict[str, Any]:
    """Run the read runner on a working folder."""
    return RUNNERS["read_document"]({"path": path, "part": part}, context(work, **ctx))


def populate(work: Path) -> None:
    """Put a PDF, a workbook and a Word file that all mention the same word into the folder."""
    fx.make_pdf(work / "report.pdf", ["The harbour inspection is complete.", "Nothing else here."])
    fx.make_xlsx(work / "figures.xlsx", rows=3)
    fx.make_docx(work / "review.docx")


# --- search ----------------------------------------------------------------------------------------------------


def test_search_finds_text_across_formats(work: Path) -> None:
    """A PDF page, a spreadsheet cell and a Word paragraph, each with its own kind of location."""
    populate(work)
    result = search(work, "HARBOUR")
    hits = {(h["path"], h["location"]) for h in result["hits"]}
    assert hits == {
        (f"/workspace/{CHAT}/report.pdf", "page 1"),
        (f"/workspace/{CHAT}/figures.xlsx", "Summary!B1"),
        (f"/workspace/{CHAT}/review.docx", "paragraph 2"),
    }
    by_location = {h["location"]: h["snippet"] for h in result["hits"]}
    assert by_location["page 1"] == "The harbour inspection is complete."
    assert by_location["Summary!B1"] == "harbour budget"
    assert (result["truncated"], result["files_searched"], result["unsupported_files"]) == (False, 3, 0)
    assert "hint" not in result
    assert "skipped" not in result


def test_search_hits_are_in_file_order_then_document_order(work: Path) -> None:
    """Files by name, and within a file the order of the document."""
    (work / "b.txt").write_text("match one\nno\nmatch two\n", encoding="utf-8")
    (work / "a.txt").write_text("match zero\n", encoding="utf-8")
    result = search(work, "match")
    assert [(h["path"].rsplit("/", 1)[1], h["location"]) for h in result["hits"]] == [
        ("a.txt", "line 1"),
        ("b.txt", "line 1"),
        ("b.txt", "line 3"),
    ]


def test_search_looks_into_folders_but_not_the_jobs_out_folder(work: Path) -> None:
    """A file in a folder is found by its path; what is in ``out/`` is the job's, not the chat's."""
    (work / "reports").mkdir()
    (work / "reports" / "q1.txt").write_text("needle in a folder\n", encoding="utf-8")
    (work / "out" / "made.txt").write_text("needle in out\n", encoding="utf-8")
    result = search(work, "needle")
    assert [h["path"] for h in result["hits"]] == [f"/workspace/{CHAT}/reports/q1.txt"]


def test_a_plain_query_is_text_not_a_pattern(work: Path) -> None:
    """Dots and brackets in a plain query mean themselves."""
    (work / "t.txt").write_text("abc\na.c\n[x]\n", encoding="utf-8")
    assert [h["location"] for h in search(work, "a.c")["hits"]] == ["line 2"]
    assert [h["location"] for h in search(work, "[x]")["hits"]] == ["line 3"]


def test_a_regex_is_a_pattern_and_ignores_case(work: Path) -> None:
    """``regex`` makes the query a regular expression."""
    (work / "t.txt").write_text("Order 12\nOrder 7\nINVOICE 340\n", encoding="utf-8")
    hits = search(work, r"(order|invoice) \d{2,}", regex=True)["hits"]
    assert [h["snippet"] for h in hits] == ["Order 12", "INVOICE 340"]


def test_an_invalid_regex_is_an_error_with_a_reason(work: Path) -> None:
    """The model is told what is wrong with its expression, in the re module's own words."""
    (work / "t.txt").write_text("x\n", encoding="utf-8")
    result = search(work, "(unclosed", regex=True)
    assert set(result) == {"error"}
    assert result["error"].startswith("bad_regex: missing ), unterminated subpattern at position 0")


def regex_spent(result: dict[str, Any]) -> bool:
    """Tell whether a search ended because its regular expression spent its budget, and said so as it should."""
    hint = result.get("hint", "")
    return (
        "error" not in result
        and result["truncated"] is True
        and "regex budget exhausted; use a simpler expression" in hint
        and "2 s matching in all" in hint
    )


def test_a_catastrophic_regex_stops_after_two_seconds(work: Path) -> None:
    """Backtracking that would take minutes is cut off: no error, the note, and the timer is disarmed."""
    (work / "t.txt").write_text("a" * 40 + "b\n", encoding="utf-8")
    began = time.monotonic()
    result = search(work, "(a+)+$", regex=True)
    assert 1.5 < time.monotonic() - began < scaled(6)
    assert regex_spent(result)
    assert result["hits"] == []
    # The module's handler stays installed after its first limit; only the timer is off.
    assert signal.getsignal(signal.SIGALRM) is timer._fire
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert timer._limits == []


def test_a_catastrophic_regex_keeps_the_hits_found_before_it(work: Path) -> None:
    """The lines before the one that backtracks are hits; they come back with the note, not as an error."""
    (work / "a.txt").write_text("aaaa\n" * 3 + "a" * 40 + "b\n" + "aaa\n", encoding="utf-8")
    (work / "b.txt").write_text("aaaa\n", encoding="utf-8")
    began = time.monotonic()
    result = search(work, "(a+)+$", regex=True)
    assert 1.5 < time.monotonic() - began < scaled(6)
    assert regex_spent(result)
    assert [(h["path"], h["location"]) for h in result["hits"]] == [
        (f"/workspace/{CHAT}/a.txt", f"line {n}") for n in (1, 2, 3)
    ]
    assert "2 files were not searched to the end" in result["hint"]


def test_the_two_seconds_are_for_the_whole_search_not_for_each_line(work: Path) -> None:
    """Each line takes about a quarter of a second, 200 of them would take a minute: the budget is spent once."""
    (work / "t.txt").write_text(("a" * 22 + "b\n") * 200, encoding="utf-8")
    began = time.monotonic()
    result = search(work, "(a+)+$", regex=True)
    assert 1.5 < time.monotonic() - began < scaled(4)
    assert regex_spent(result)


def test_a_plain_query_is_never_timed(work: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A plain query has no budget; a regex query enters its limit once to compile and once for the whole search.

    The limit used to be entered around every piece, at five microseconds of system calls each, all charged to
    the budget: a 400 000-line file ran out of it with nothing matched.
    """
    spent: list[docs.Meter] = []
    original = docs.limit_spending

    def spy(meter: docs.Meter, error: type[docs.TimeLimit]) -> Any:
        spent.append(meter)
        return original(meter, error)

    monkeypatch.setattr(docs, "limit_spending", spy)
    (work / "t.txt").write_text("needle\nneedle two\n", encoding="utf-8")
    search(work, "needle")
    assert spent == []
    search(work, "needle", regex=True)
    assert len(spent) == 2
    (work / "u.txt").write_text("needle\n" * 1000, encoding="utf-8")
    search(work, "needle", regex=True)
    assert len(spent) == 4


def test_the_regex_budget_counts_matching_not_reading(work: Path) -> None:
    """A search of a 600 000-line file reads for about a second but matches for far less: it completes.

    It ran out of its two seconds at 400 000 lines when every line's limit was charged to the budget.
    """
    (work / "t.txt").write_text("nothing to see here\n" * 600_000 + "needle 7\n", encoding="utf-8")
    result = search(work, r"need(le)? \d", regex=True)
    assert "hint" not in result
    assert result["truncated"] is False
    assert [h["location"] for h in result["hits"]] == ["line 600001"]


def test_the_regex_budget_works_inside_a_search_that_has_a_reader_deadline(work: Path) -> None:
    """The reader's timer is in force while the matching runs; the two limits do not clobber each other."""
    (work / "t.txt").write_text("a" * 40 + "b\n", encoding="utf-8")
    began = time.monotonic()
    result = search(work, "(a+)+$", regex=True, budget_s=30)
    assert 1.5 < time.monotonic() - began < scaled(4)
    assert regex_spent(result)
    assert signal.getsignal(signal.SIGALRM) is timer._fire
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert readers_deadline_is_off()


def test_the_reader_deadline_still_fires_after_a_regex_budget_was_spent_inside_it(work: Path, big_xlsx: Path) -> None:
    """A slow file after a regex that was cheap: the file's limit is armed again when the regex's ends."""
    (work / "a.txt").write_text("needle 1\n" * 5, encoding="utf-8")
    shutil.copy(big_xlsx, work / "b-ledger.xlsx")
    began = time.monotonic()
    result = search(work, r"needle \d", regex=True, budget_s=3)
    assert time.monotonic() - began < scaled(6)
    assert len(result["hits"]) == 5
    assert result["skipped"] == [{"path": f"/workspace/{CHAT}/b-ledger.xlsx", "reason": "reading took longer than 1 s"}]


def test_a_files_deadline_cuts_a_catastrophic_match_short_and_the_search_goes_on(work: Path) -> None:
    """The file's own deadline (1 s) comes before the regex budget (2 s) is spent: it stops the match mid-way.

    The file is named, its hits before that line are kept, and the next file is searched with what is left.
    """
    (work / "a.txt").write_text("aaaa\n" + "a" * 40 + "b\n", encoding="utf-8")
    (work / "b.txt").write_text("aaaa\n", encoding="utf-8")
    began = time.monotonic()
    result = search(work, "(a+)+$", regex=True, budget_s=3)
    assert 0.9 < time.monotonic() - began < scaled(4)
    assert [(h["path"].rsplit("/", 1)[1], h["location"]) for h in result["hits"]] == [
        ("a.txt", "line 1"),
        ("b.txt", "line 1"),
    ]
    assert result["skipped"] == [{"path": f"/workspace/{CHAT}/a.txt", "reason": "reading took longer than 1 s"}]
    assert "hint" not in result
    assert result["truncated"] is False
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert timer._limits == []


def slow_to_read(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    """Make the text reader take ``seconds`` before the lines of a file whose name ends in ``-slow.txt``."""
    from workbench.executor.readers import text

    original = text.units

    def units(path: Path) -> Iterator[readers.Part]:
        if path.name.endswith("-slow.txt"):
            time.sleep(seconds)
        yield from original(path)

    monkeypatch.setattr(text, "units", units)


def wait_for_a_time_limit() -> None:
    """Run pure Python until a time limit's error lands in it; fail if none does within five seconds."""
    end = time.monotonic() + 5
    while time.monotonic() < end:
        pass
    pytest.fail("no time limit landed")


def hold_the_first_match_until_its_files_deadline(monkeypatch: pytest.MonkeyPatch, where: str) -> None:
    """Keep the first match of a file inside ``Meter.stop`` until the file's own deadline lands there.

    ``begins``: as ``stop()`` begins; ``clock``: just after its clock read. Those are where a file's
    ``ReadTimeout`` was found to land during a short match, in about one file of four that ran out of time.
    """
    held: list[bool] = []

    def hold() -> bool:
        if held or readers._deadline is None:  # the compile runs the meter too, with no file being read
            return False
        held.append(True)
        return True

    if where == "begins":
        original = timer.Meter.stop

        def stop(self: timer.Meter) -> None:
            if hold():
                wait_for_a_time_limit()
            original(self)

        monkeypatch.setattr(timer.Meter, "stop", stop)
        return
    code = timer.Meter.stop.__code__
    real = time.monotonic

    class Clock:
        """The timer module's clock, which waits in ``stop()`` right after it has been read."""

        @staticmethod
        def monotonic() -> float:
            now = real()
            if sys._getframe(1).f_code is code and hold():
                wait_for_a_time_limit()
            return now

    monkeypatch.setattr(timer, "time", Clock)


def spend_without_stopping_meters(limit: timer._Limit) -> type[timer.TimeLimit]:
    """The timer's ``_spend`` as it was, which left a meter that was running alone: only the search stops it."""
    limit.fired = True
    if limit.meter is not None:
        limit.meter.left = 0.0
        limit.meter.until = None
    return limit.error


@pytest.mark.parametrize(("where", "timer_stops_it"), [("begins", True), ("clock", True), ("begins", False)])
def test_a_files_deadline_that_lands_as_a_match_ends_does_not_spend_the_regex_budget_later(
    work: Path, monkeypatch: pytest.MonkeyPatch, where: str, timer_stops_it: bool
) -> None:
    """The first file's deadline lands in ``Meter.stop``, and the slow file after it is still searched, and the next.

    The second file takes longer to read than the regex budget had left. The meter used to be left running with
    its end near: that reading then raised ``RegexTimeout``, and the search answered "regex budget exhausted" with
    no hits and the last two files not searched. The timer stops the meter before it raises; with that taken
    away (``timer_stops_it`` false), the search's own stop at the end of each file still does.
    """
    monkeypatch.setattr(docs, "REGEX_BUDGET_S", 1.25)
    if not timer_stops_it:
        monkeypatch.setattr(timer, "_spend", spend_without_stopping_meters)
    hold_the_first_match_until_its_files_deadline(monkeypatch, where)
    slow_to_read(monkeypatch, 0.5)
    (work / "a.txt").write_text("nothing to see here\n" * 3, encoding="utf-8")
    (work / "b-slow.txt").write_text("needle 2\n", encoding="utf-8")
    (work / "c.txt").write_text("needle 3\n", encoding="utf-8")
    began = time.monotonic()
    result = search(work, r"need(le)? \d", regex=True, budget_s=3)
    elapsed = time.monotonic() - began
    assert "hint" not in result
    assert result["truncated"] is False
    assert [(h["path"].rsplit("/", 1)[1], h["location"]) for h in result["hits"]] == [
        ("b-slow.txt", "line 1"),
        ("c.txt", "line 1"),
    ]
    assert result["skipped"] == [{"path": f"/workspace/{CHAT}/a.txt", "reason": "reading took longer than 1 s"}]
    assert 1.4 < elapsed < scaled(4)
    assert timer._limits == []


def test_snippets_are_at_most_200_characters_around_the_match(work: Path) -> None:
    """The match is in the middle, with context on both sides, whatever the length of the line."""
    (work / "t.txt").write_text("x " * 300 + "NEEDLE" + " y" * 300 + "\n", encoding="utf-8")
    (hit,) = search(work, "needle")["hits"]
    assert len(hit["snippet"]) <= 200
    assert "NEEDLE" in hit["snippet"]
    assert hit["snippet"].startswith("…")
    assert hit["snippet"].endswith("…")
    assert hit["snippet"].index("NEEDLE") in range(80, 120)


def test_snippets_at_the_edges_and_for_long_matches() -> None:
    """A match at the start has no leading ellipsis; a match longer than the limit is cut."""
    text = "Start " + "." * 500
    match = re.search("Start", text)
    assert match is not None
    start = docs.snippet(text, match)
    assert start.startswith("Start ") and start.endswith("…") and len(start) <= 200
    tail = "." * 500 + " End"
    end_match = re.search("End", tail)
    assert end_match is not None
    end = docs.snippet(tail, end_match)
    assert end.startswith("…") and end.endswith(" End") and len(end) <= 200
    long = "b" * 500
    long_match = re.search("b+", long)
    assert long_match is not None
    assert len(docs.snippet(long, long_match)) <= 200
    short = "all of it"
    short_match = re.search("of", short)
    assert short_match is not None
    assert docs.snippet(short, short_match) == "all of it"


def test_max_hits_caps_the_answer_and_says_so(work: Path) -> None:
    """One hit more than allowed is found, to be able to say there are more."""
    (work / "t.txt").write_text("match\n" * 60, encoding="utf-8")
    result = search(work, "match", max_hits=10)
    assert len(result["hits"]) == 10
    assert result["truncated"] is True
    assert "max_hits" in result["hint"]
    assert len(search(work, "match", max_hits=60)["hits"]) == 60
    assert search(work, "match", max_hits=60)["truncated"] is False


def test_max_hits_is_at_most_200_and_at_least_1(work: Path) -> None:
    """Whatever the model asks for, the number is clamped."""
    (work / "t.txt").write_text("match\n" * 300, encoding="utf-8")
    assert len(search(work, "match", max_hits=5000)["hits"]) == 200
    assert len(search(work, "match", max_hits=0)["hits"]) == 1


def test_one_page_gives_a_few_hits_not_all(work: Path) -> None:
    """A page that says the word thirty times does not use up the hits of the other files."""
    (work / "a.txt").write_text("hit hit hit hit hit hit hit hit\n", encoding="utf-8")
    (work / "b.txt").write_text("hit\n", encoding="utf-8")
    result = search(work, "hit")
    assert [(h["path"].rsplit("/", 1)[1]) for h in result["hits"]] == ["a.txt"] * docs.HITS_PER_UNIT + ["b.txt"]


def test_empty_matches_are_not_hits(work: Path) -> None:
    """A pattern that matches the empty string everywhere finds nothing."""
    (work / "t.txt").write_text("abc\n", encoding="utf-8")
    assert search(work, "x*", regex=True)["hits"] == []


def test_the_hits_of_a_search_are_bounded_in_size_too(work: Path) -> None:
    """Long paths and snippets cannot make an answer the executor refuses to read: hits stop at 3 x ``max_chars``."""
    (work / ("d" * 150)).mkdir()
    for n in range(40):
        (work / ("d" * 150) / f"{'n' * 100}{n:02d}.txt").write_text(("x " * 150 + "needle") + "\n", encoding="utf-8")
    result = RUNNERS["search_files"]({"query": "needle", "max_hits": 200}, context(work, max_chars=2000))
    assert len(json.dumps(result["hits"])) <= 3 * 2000
    assert 1 < len(result["hits"]) < 40
    assert result["truncated"] is True
    assert "fit in an answer" in result["hint"]
    assert "raise max_hits" not in result["hint"]


def test_files_that_cannot_be_searched_are_named_with_the_reason(work: Path) -> None:
    """Encrypted, scanned and broken files are listed; other types are only counted."""
    populate(work)
    fx.make_pdf(work / "locked.pdf", ["harbour"], password="synthetic-password")
    fx.make_pdf(work / "scan.pdf", ["harbour on page one", None, None])
    fx.make_truncated(work / "review.docx", work / "cut.docx")
    (work / "chart.png").write_bytes(b"\x89PNG")
    (work / "mail.msg").write_bytes(b"x")
    result = search(work, "harbour")
    skipped = {s["path"].rsplit("/", 1)[1]: s["reason"] for s in result["skipped"]}
    assert skipped == {
        "locked.pdf": "the PDF is password-protected (encrypted)",
        "scan.pdf": "no text on pages 2, 3",
        "cut.docx": "the file is damaged or truncated, or is not what its extension says",
    }
    assert result["unsupported_files"] == 2
    assert result["files_searched"] == 6
    assert "harbour on page one" in [h["snippet"] for h in result["hits"]]


def test_a_search_with_no_files_finds_nothing(work: Path) -> None:
    """An empty chat is not an error."""
    assert search(work, "anything") == {"hits": [], "truncated": False, "files_searched": 0, "unsupported_files": 0}


def test_the_search_stops_opening_files_at_its_time_budget(work: Path) -> None:
    """A search over many heavy files returns what it has, and says how many files it did not reach."""
    for n in range(5):
        (work / f"f{n}.txt").write_text("needle\n", encoding="utf-8")
    result = search(work, "needle", budget_s=0)
    assert result["truncated"] is True
    assert "stopped after 0 s" in result["hint"]
    assert "5 files were not searched" in result["hint"]


def test_skipped_files_are_listed_up_to_a_limit(work: Path) -> None:
    """Thirty broken files do not make a thirty-line answer."""
    for n in range(30):
        (work / f"broken{n:02d}.docx").write_bytes(b"not a zip")
    result = search(work, "x")
    assert len(result["skipped"]) == docs.SKIPPED_LISTED == 20
    assert "10 more files could not be searched" in result["hint"]


def test_results_never_hold_the_jobs_real_path(work: Path) -> None:
    """Every path is virtual, in hits, skips and errors alike."""
    populate(work)
    fx.make_ole(work / "locked.docx")
    result = search(work, "harbour")
    assert str(work.parent.parent) not in json.dumps(result)
    assert all(h["path"].startswith(f"/workspace/{CHAT}/") for h in result["hits"])


@pytest.fixture(scope="module")
def big_xlsx(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A workbook of 100 000 rows: a search over it takes several seconds, built once for the module."""
    return fx.make_xlsx(tmp_path_factory.mktemp("big") / "ledger.xlsx", rows=100_000)


def test_a_slow_file_is_given_up_on_and_the_search_goes_on(work: Path, big_xlsx: Path) -> None:
    """One file may use a third of the time; it is named, what was found elsewhere is returned, no timeout."""
    (work / "a.txt").write_text("needle in a\n", encoding="utf-8")
    shutil.copy(big_xlsx, work / "b-ledger.xlsx")
    (work / "c.txt").write_text("needle in c\n", encoding="utf-8")
    began = time.monotonic()
    result = search(work, "needle", budget_s=3)
    assert time.monotonic() - began < scaled(6)
    assert "error" not in result
    assert {h["path"].rsplit("/", 1)[1] for h in result["hits"]} == {"a.txt", "c.txt"}
    assert result["skipped"] == [{"path": f"/workspace/{CHAT}/b-ledger.xlsx", "reason": "reading took longer than 1 s"}]
    assert result["truncated"] is False
    assert result["files_searched"] == 3


def test_when_the_whole_time_is_used_up_inside_a_file_the_search_stops_with_a_note(work: Path, big_xlsx: Path) -> None:
    """The partial hits are returned, with how many files were not searched; the job is not left to be killed."""
    (work / "a.txt").write_text("needle in a\n", encoding="utf-8")
    shutil.copy(big_xlsx, work / "b-ledger.xlsx")
    (work / "c.txt").write_text("needle in c\n", encoding="utf-8")
    began = time.monotonic()
    result = search(work, "needle", budget_s=1)
    assert time.monotonic() - began < scaled(5)
    assert [h["path"].rsplit("/", 1)[1] for h in result["hits"]] == ["a.txt"]
    assert result["truncated"] is True
    assert "stopped after 1 s" in result["hint"]
    assert "2 files were not searched" in result["hint"]


def test_a_workbook_that_is_slow_to_load_is_given_up_on_and_the_search_goes_on(work: Path) -> None:
    """900 000 shared strings pass the count and take nine seconds to load: the timer ends that at the file's share."""
    (work / "a.txt").write_text("needle in a\n", encoding="utf-8")
    fx.make_shared_strings_xlsx(work / "b-strings.xlsx", 900_000)
    (work / "c.txt").write_text("needle in c\n", encoding="utf-8")
    began = time.monotonic()
    result = search(work, "needle", budget_s=3)
    assert time.monotonic() - began < scaled(5)
    assert "error" not in result
    assert [h["path"].rsplit("/", 1)[1] for h in result["hits"]] == ["a.txt", "c.txt"]
    assert result["skipped"] == [
        {"path": f"/workspace/{CHAT}/b-strings.xlsx", "reason": "reading took longer than 1 s"}
    ]
    assert result["truncated"] is False


def test_a_regex_search_over_many_lines_still_stops_at_its_time(work: Path) -> None:
    """The regex budget's meter runs for every line; the search's own deadline still fires among them."""
    (work / "t.txt").write_text("nothing to see here\n" * 1_000_000, encoding="utf-8")
    began = time.monotonic()
    result = search(work, r"need(le)? \d", regex=True, budget_s=0.2)
    assert time.monotonic() - began < 4
    assert result["truncated"] is True
    assert "stopped after 0.2 s" in result["hint"]
    assert "regex budget" not in result["hint"]
    assert result["hits"] == []
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_a_regex_over_the_100000_row_workbook_finds_its_ten_hits(work: Path, big_xlsx: Path) -> None:
    """The brief's large fixture: the cells of rows 99 990 to 99 999 match, all ten are found, no budget runs out.

    With the limit entered around each of its 600 000 cells the budget ran out first, with no hits and an error.
    """
    shutil.copy(big_xlsx, work / "ledger.xlsx")
    result = search(work, r"item 9999\d\b", regex=True)
    assert "hint" not in result
    assert result["truncated"] is False
    assert [h["location"] for h in result["hits"]] == [f"Sheet1!B{n}" for n in range(99_990, 100_000)]


@pytest.mark.parametrize("kind", ["tag", "default", "noslash", "dslash"])
def test_a_workbook_with_a_loophole_is_named_in_a_search_at_once(work: Path, kind: str) -> None:
    """The sheet entries that openpyxl would open one by one are counted the way openpyxl finds them."""
    (work / "a.txt").write_text("needle in a\n", encoding="utf-8")
    fx.make_bypass_xlsx(work / "b-loophole.xlsx", kind, 20_000)
    (work / "c.txt").write_text("needle in c\n", encoding="utf-8")
    cpu = time.process_time()
    result = search(work, "needle", budget_s=3)
    assert time.process_time() - cpu < 2
    assert [h["path"].rsplit("/", 1)[1] for h in result["hits"]] == ["a.txt", "c.txt"]
    assert result["skipped"] == [
        {"path": f"/workspace/{CHAT}/b-loophole.xlsx", "reason": "the workbook has too many sheets"}
    ]


def test_workbooks_with_20000_sheet_entries_and_2_million_strings_are_named_at_once(work: Path) -> None:
    """Counted before they are loaded: refused in moments, and the rest of the chat is searched."""
    (work / "a.txt").write_text("needle in a\n", encoding="utf-8")
    fx.make_many_entries_xlsx(work / "b-entries.xlsx", 20_000)
    fx.make_shared_strings_xlsx(work / "c-strings.xlsx", 2_000_000)
    (work / "d.txt").write_text("needle in d\n", encoding="utf-8")
    cpu = time.process_time()
    result = search(work, "needle", budget_s=round(scaled(3)))
    assert time.process_time() - cpu < scaled(3)
    assert [h["path"].rsplit("/", 1)[1] for h in result["hits"]] == ["a.txt", "d.txt"]
    reasons = {s["path"].rsplit("/", 1)[1]: s["reason"] for s in result["skipped"]}
    assert reasons == {
        "b-entries.xlsx": "the workbook has too many sheets",
        "c-strings.xlsx": "the workbook holds too many strings",
    }


def test_hostile_files_are_named_and_the_search_goes_on(work: Path) -> None:
    """A row beyond the last, 20 000 sheets, 20 000 blocks: refused or read in moments; other files are searched."""
    (work / "a.txt").write_text("needle\n", encoding="utf-8")
    fx.make_sparse_xlsx(work / "sparse.xlsx", row=2_000_000_000)
    fx.make_many_sheets_ods(work / "sheets.ods", 20_000)
    fx.make_many_blocks_html(work / "blocks.html", 20_000)
    began = time.monotonic()
    result = search(work, "needle")
    assert time.monotonic() - began < 5
    skipped = {s["path"].rsplit("/", 1)[1]: s["reason"] for s in result["skipped"]}
    assert set(skipped) == {"sparse.xlsx", "sheets.ods"}
    assert [h["path"].rsplit("/", 1)[1] for h in result["hits"]] == ["a.txt"]


# --- read_document ---------------------------------------------------------------------------------------------


def test_read_document_answers_with_the_virtual_path_and_the_parts(work: Path) -> None:
    """The answer is the reader's, with the path the model knows."""
    fx.make_docx(work / "review.docx")
    result = read_doc(work, "review.docx")
    assert result["path"] == f"/workspace/{CHAT}/review.docx"
    assert result["kind"] == "docx"
    assert [p["label"] for p in result["parts"]] == [
        "paragraph 1",
        "paragraph 2",
        "paragraph 3",
        "table 1, rows 1-3",
        "paragraph 4",
    ]
    assert (result["total_parts"], result["pages_without_text"], result["truncated"]) == (5, [], False)
    assert "next_part" not in result
    assert "hint" not in result


def test_a_file_in_a_folder_keeps_its_folder_in_the_path(work: Path) -> None:
    """The virtual path of ``reports/q1.txt`` is below ``/workspace/<chat>/reports``."""
    (work / "reports").mkdir()
    (work / "reports" / "q1.txt").write_text("hello\n", encoding="utf-8")
    assert read_doc(work, "reports/q1.txt")["path"] == f"/workspace/{CHAT}/reports/q1.txt"


def test_a_pdf_with_a_scanned_page_carries_a_hint_about_ocr(work: Path) -> None:
    """The page is listed, never returned as blank, and the model is told what to say."""
    fx.make_pdf(work / "scan.pdf", ["text on page one", None])
    result = read_doc(work, "scan.pdf")
    assert [p["label"] for p in result["parts"]] == ["page 1"]
    assert result["pages_without_text"] == [2]
    assert "page 2" in result["hint"]
    assert "OCR" in result["hint"]
    assert "do not treat them as blank" in result["hint"]


def test_a_deck_with_a_picture_slide_says_slides_not_pages(work: Path) -> None:
    """The same report for slides, in the right word."""
    from pptx import Presentation

    deck = Presentation()
    deck.slides.add_slide(deck.slide_layouts[6])
    deck.slides.add_slide(deck.slide_layouts[5]).shapes.title.text = "Only this one"  # pyrefly: ignore
    deck.save(str(work / "pictures.pptx"))
    result = read_doc(work, "pictures.pptx")
    assert result["pages_without_text"] == [1]
    assert result["hint"].startswith("slide 1 hold no text")


def test_a_long_list_of_empty_pages_is_shortened_in_the_hint(work: Path) -> None:
    """Forty scanned pages are listed in the field, and summarised in the sentence."""
    fx.make_pdf(work / "scan.pdf", [None] * 40 + ["finally text"])
    result = read_doc(work, "scan.pdf")
    assert len(result["pages_without_text"]) == 40
    assert "and 20 more" in result["hint"]


def test_paging_through_the_runner(work: Path) -> None:
    """``next_part`` of one answer is ``part`` of the next."""
    (work / "long.txt").write_text("\n".join(f"line number {n} " + "w" * 80 for n in range(1, 400)), encoding="utf-8")
    first = read_doc(work, "long.txt", max_chars=4096)
    assert first["truncated"] is True
    second = read_doc(work, "long.txt", first["next_part"], max_chars=4096)
    assert second["parts"][0]["label"].startswith(f"lines {first['next_part'].split()[1]}-")


def test_the_answer_leaves_room_for_what_is_not_text(work: Path) -> None:
    """The parts are budgeted below ``max_chars``, so the path, the hint and the rest fit too."""
    fx.make_pdf(work / "scan.pdf", ["word " * 3000])
    result = read_doc(work, "scan.pdf", max_chars=4096)
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= 4096


@pytest.mark.parametrize(
    ("make", "code", "text"),
    [
        (lambda w: (w / "mail.msg").write_bytes(b"x"), "unsupported_format", ".eml or PDF"),
        (lambda w: (w / "pic.png").write_bytes(b"x"), "unsupported_format", "readable are PDF"),
        (lambda w: fx.make_pdf(w / "locked.pdf", ["x"], password="synthetic-password"), "unreadable_file", "password"),
        (lambda w: fx.make_ole(w / "locked.xlsx"), "unreadable_file", "password-protected"),
    ],
)
def test_failures_are_one_key_with_a_code_and_a_message(work: Path, make: Any, code: str, text: str) -> None:
    """The api turns ``code: message`` into the model's answer; the job reports the tool as failed."""
    make(work)
    name = next(p.name for p in work.iterdir() if p.is_file())
    result = read_doc(work, name)
    assert set(result) == {"error"}
    assert result["error"].startswith(f"{code}: ")
    assert text in result["error"]
    assert str(work.parent.parent) not in result["error"]


def test_a_missing_file_and_a_missing_part_are_errors(work: Path) -> None:
    """The file may have been deleted since the api looked; a part may not exist."""
    assert (
        read_doc(work, "gone.docx")["error"]
        == f"file_not_found: /workspace/{CHAT}/gone.docx is not in this conversation's workspace"
    )
    fx.make_pdf(work / "two.pdf", ["a", "b"])
    result = read_doc(work, "two.pdf", "page 9")
    assert result["error"] == "no_such_part: this document has 2 pages"
    assert read_doc(work, "out/anything.txt")["error"].startswith("file_not_found: ")


@pytest.mark.parametrize("path", ["../secret.txt", "/etc/passwd", "a//b.txt", ""])
def test_a_path_that_is_not_inside_the_chat_folder_is_refused(work: Path, path: str) -> None:
    """The api only sends names it got from the store; the runner does not trust that."""
    with pytest.raises(ValueError):
        read_doc(work, path)


def test_the_zip_bomb_guard_uses_the_jobs_file_limit(work: Path) -> None:
    """With a 1 MB file limit, 25 MB unpacked is refused through the runner as well."""
    fx.make_zip_bomb(work / "bomb.docx", mb=25)
    began = time.monotonic()
    refused = read_doc(work, "bomb.docx", file_mb=1)
    assert time.monotonic() - began < 5
    assert refused["error"].startswith("unreadable_file: the file unpacks to more than 20 MB")
    # Under a 2 MB limit the zip check lets it through; the XML parser's own text limit still stops it.
    assert "unpacks to more than" not in read_doc(work, "bomb.docx", file_mb=2).get("error", "")


def test_a_read_that_runs_out_of_time_is_an_error_with_a_code(work: Path, big_xlsx: Path) -> None:
    """The runner stops at its own budget, well before the executor would kill the job."""
    shutil.copy(big_xlsx, work / "ledger.xlsx")
    began = time.monotonic()
    result = RUNNERS["read_document"]({"path": "ledger.xlsx", "part": "Sheet1!A99000", "budget_s": 0.5}, context(work))
    assert time.monotonic() - began < 4
    assert result == {"error": "read_timeout: reading the file took longer than 0.5 s; it is too large or too complex"}
    assert readers_deadline_is_off()


def test_a_workbook_that_is_slow_to_load_ends_a_read_at_its_budget(work: Path) -> None:
    """``read_document`` on it answers ``read_timeout`` at its budget, not at the executor's wall time."""
    fx.make_shared_strings_xlsx(work / "strings.xlsx", 900_000)
    began = time.monotonic()
    result = RUNNERS["read_document"]({"path": "strings.xlsx", "part": None, "budget_s": 1}, context(work))
    assert 0.9 < time.monotonic() - began < scaled(3)
    assert result == {"error": "read_timeout: reading the file took longer than 1 s; it is too large or too complex"}


def readers_deadline_is_off() -> bool:
    """Tell whether no read deadline is left set: a later read is not stopped by an earlier one."""
    from workbench.executor import readers

    return readers._deadline is None


# --- through the job entry point -------------------------------------------------------------------------------


def run_job(work: Path, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run a tool exactly as the supervisor does: a spec next to the working folder, in isolated mode."""
    job = work.parent.parent
    spec = job / "spec.json"
    spec.write_text(json.dumps({"tool": tool, "args": args, "chat_id": CHAT, "max_chars": 16384, "file_mb": 50}))
    done = subprocess.run(
        [sys.executable, "-I", "-m", "workbench.executor.job", str(spec)],
        cwd=work,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads((job / "result.json").read_text())  # type: ignore[no-any-return]


def test_read_document_runs_as_a_job(work: Path) -> None:
    """The registered runner works in a fresh interpreter in isolated mode, with the real path nowhere in the answer."""
    fx.make_docx(work / "review.docx")
    result = run_job(work, "read_document", {"path": "review.docx", "part": "table 1"})
    assert result["path"] == f"/workspace/{CHAT}/review.docx"
    assert [p["label"] for p in result["parts"]] == ["table 1, rows 1-3", "paragraph 4"]
    assert str(work.parent.parent) not in json.dumps(result)


def test_search_files_runs_as_a_job_and_a_failure_is_an_error_result(work: Path) -> None:
    """A search finds its hit; an unsupported file read fails with the code the api maps."""
    populate(work)
    hits = run_job(work, "search_files", {"query": "harbour", "regex": False, "max_hits": 50, "budget_s": 30})["hits"]
    assert len(hits) == 3
    (work / "mail.msg").write_bytes(b"x")
    failed = run_job(work, "read_document", {"path": "mail.msg", "part": None})
    assert failed["error"].startswith("unsupported_format: ")
    assert ".eml or PDF" in failed["error"]
