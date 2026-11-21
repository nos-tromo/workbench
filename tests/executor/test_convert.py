"""``convert_office``: an Office file becomes one PDF in ``out/``, or an answer that says why not, and no more.

LibreOffice itself runs only in the executor image (``tests/executor/test_supervisor.py``); here the converter is
``fake_office`` (``tests/conftest.py``), or one the test writes. Every file is built at test time, all text invented.
"""

import dataclasses
import json
import os
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import FakeOffice
from tests.fixtures import make_fixtures, make_templates
from workbench.executor import readers
from workbench.executor.readers import UnreadableFile
from workbench.executor.runners import RUNNERS, JobContext
from workbench.executor.runners.convert import convert_office
from workbench.renders import CONVERTED_MAX_BYTES, ConvertAnswer

JOB_ID = "0123456789abcdef0123456789abcdef"
CHAT = "chat-1"
NOT_SUPPORTED = "unsupported_format: only Word, PowerPoint and OpenDocument text files are converted"
NO_SLIDES = "unsupported_format: the deck has no slides; inspect_template lists the layouts of a slide master"
NOT_CONVERTED = "unreadable_file: the file could not be converted to pages"


@dataclass(frozen=True)
class Job:
    """A job folder as the supervisor makes it, the runner's working folder its chat folder.

    Attributes:
        ctx: What the job entry point gives the runner.
    """

    ctx: JobContext

    def path(self, name: str) -> Path:
        """Where an input of that name lies."""
        return self.ctx.work_dir / name

    def convert(self, name: str, *, budget_s: float = 90, **extra: Any) -> dict[str, Any]:
        """Run ``convert_office`` on an input; ``extra`` is the runner's own keyword, ``convert``."""
        return convert_office({"path": name, "budget_s": budget_s}, self.ctx, **extra)

    def written(self) -> list[str]:
        """The names in ``out/``."""
        return sorted(path.name for path in self.ctx.out_dir.iterdir())


@pytest.fixture
def job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Job:
    """An empty job folder for ``chat-1``, whose ``TMPDIR`` is a folder of the job's own."""
    work = tmp_path / "data" / "jobs" / JOB_ID / "workspace" / CHAT
    (work / "out").mkdir(parents=True)
    scratch = tmp_path / "data" / "jobs" / JOB_ID / "home" / "tmp"
    scratch.mkdir(parents=True)
    monkeypatch.setenv("TMPDIR", str(scratch))
    monkeypatch.setattr("tempfile.tempdir", None)  # read TMPDIR again
    return Job(JobContext(work_dir=work, out_dir=work / "out", chat_id=CHAT, max_chars=16 * 1024, file_mb=50))


def _never(source: Path, target: Path, *, deck: bool, deadline: float) -> None:
    """A converter that must not be called."""
    raise AssertionError("LibreOffice was reached")


def _raising(error: BaseException) -> Any:
    """A converter that ends in ``error``."""

    def convert(source: Path, target: Path, *, deck: bool, deadline: float) -> None:
        raise error

    return convert


def _writing(data: bytes) -> Any:
    """A converter that writes ``data`` as the PDF."""

    def convert(source: Path, target: Path, *, deck: bool, deadline: float) -> None:
        target.write_bytes(data)

    return convert


def _leaving_nothing(source: Path, target: Path, *, deck: bool, deadline: float) -> None:
    """A converter that returns and writes no file."""


def _scratch() -> list[str]:
    """What the job's own temporary folder holds."""
    return sorted(path.name for path in Path(tempfile.gettempdir()).iterdir())


# --- what is converted ----------------------------------------------------------------------


def test_a_deck_is_converted_to_one_pdf_in_out(job: Job, fake_office: FakeOffice) -> None:
    """Three slides give three pages: the answer says so and names the file, which is alone in ``out/``."""
    make_fixtures.make_deck(job.path("harbour.pptx"), 3, chart_on=None)

    answer = job.convert("harbour.pptx")

    assert answer == {"name": "converted.pdf", "pages": 3, "slides": 3}
    assert ConvertAnswer.model_validate(answer).model_dump() == answer
    assert job.written() == ["converted.pdf"]
    assert (job.ctx.out_dir / "converted.pdf").read_bytes().startswith(b"%PDF-")
    assert (fake_office.calls, fake_office.decks) == (1, [True])


@pytest.mark.parametrize("suffix", [".pptx", ".potx"])
def test_a_deck_template_is_a_deck_too(job: Job, fake_office: FakeOffice, suffix: str) -> None:
    """A ``.potx`` is counted by its slides and converted as a deck."""
    deck = make_fixtures.make_deck(job.path("deck.pptx"), 2, chart_on=None)
    name = "deck" + suffix
    if suffix == ".potx":
        make_templates.as_potx(deck, job.path(name))

    answer = job.convert(name)

    assert answer == {"name": "converted.pdf", "pages": 2, "slides": 2}
    assert fake_office.decks == [True]


@pytest.mark.parametrize("kind", ["docx", "odt", "dotx"])
def test_a_word_file_is_converted_without_a_slide_count(job: Job, fake_office: FakeOffice, kind: str) -> None:
    """A document, an OpenDocument text and a Word template give ``slides`` ``None`` and are converted as no deck."""
    name = {"docx": "memo.docx", "odt": "survey.odt", "dotx": "memo.dotx"}[kind]
    if kind == "odt":
        make_fixtures.make_odt(job.path(name))
    else:
        docx = make_fixtures.make_docx(job.path("memo.docx"))
        if kind == "dotx":
            make_fixtures.as_template(docx, job.path(name))

    answer = job.convert(name)

    assert answer == {"name": "converted.pdf", "pages": 3, "slides": None}
    assert fake_office.decks == [False]
    assert job.written() == ["converted.pdf"]


def test_the_suffix_is_read_in_any_case(job: Job, fake_office: FakeOffice) -> None:
    """``DECK.PPTX`` is a deck, as ``kind_of`` and ``unit_of`` read it."""
    make_fixtures.make_deck(job.path("DECK.PPTX"), 2, chart_on=None)

    assert job.convert("DECK.PPTX") == {"name": "converted.pdf", "pages": 2, "slides": 2}
    assert fake_office.decks == [True]


def test_the_converter_is_looked_up_when_the_job_runs(job: Job, fake_office: FakeOffice) -> None:
    """The registered runner calls ``office.convert`` as it is then, so a test (and this fixture) can put its own."""
    make_fixtures.make_docx(job.path("memo.docx"))
    fake_office.pages = 5

    answer = RUNNERS["convert_office"]({"path": "memo.docx"}, job.ctx)

    assert answer == {"name": "converted.pdf", "pages": 5, "slides": None}


def test_a_converter_gets_a_deck_flag_and_a_deadline_and_no_test_seam(job: Job, tmp_path: Path) -> None:
    """Production defaults only: the runner passes ``deck`` and ``deadline``, never ``office.convert``'s test seams.

    The spy takes exactly those two keywords, so any other one would be a ``TypeError``.
    """
    seen: list[dict[str, Any]] = []
    pdf = make_fixtures.make_pdf(tmp_path / "two.pdf", ["one", "two"])

    def convert(source: Path, target: Path, *, deck: bool, deadline: float) -> None:
        seen.append({"deck": deck, "left": deadline - time.monotonic(), "source": source.name})
        target.write_bytes(pdf.read_bytes())

    make_fixtures.make_deck(job.path("deck.pptx"), 2, chart_on=None)

    answer = job.convert("deck.pptx", budget_s=90, convert=convert)

    assert answer == {"name": "converted.pdf", "pages": 2, "slides": 2}
    ((call),) = seen
    assert call["deck"] is True
    assert call["source"] == "deck.pptx"
    assert 80 < call["left"] <= 90


# --- what is refused before LibreOffice ---------------------------------------------------------


@pytest.mark.parametrize("which", ["ole docx", "ole pptx", "encrypted odt"])
def test_encrypted_files_never_reach_libreoffice(job: Job, which: str) -> None:
    """A password-protected file is told so, before any child is forked, and ``out/`` stays empty."""
    if which == "encrypted odt":
        make_fixtures.make_encrypted_odt(job.path("locked.odt"))
        name = "locked.odt"
    else:
        name = "locked." + which.split()[1]
        make_fixtures.make_ole(job.path(name))

    answer = job.convert(name, convert=_never)

    assert set(answer) == {"error"}
    assert answer["error"].startswith("unreadable_file: ")
    assert "password-protected" in answer["error"]
    assert job.written() == []


def test_the_odt_check_is_the_encrypted_manifest_alone(job: Job, fake_office: FakeOffice) -> None:
    """An OpenDocument text with no encryption data in its manifest is converted."""
    make_fixtures.make_odt(job.path("survey.odt"))

    assert job.convert("survey.odt")["pages"] == 3


def test_the_manifest_is_read_to_a_mebibyte_and_no_more(job: Job, fake_office: FakeOffice) -> None:
    """The check is a courtesy before LibreOffice, which refuses a locked file itself: it reads a mebibyte at most."""
    plain = make_fixtures.make_odt(job.path("survey.odt"))
    with zipfile.ZipFile(plain) as src, zipfile.ZipFile(job.path("long.odt"), "w") as out:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "META-INF/manifest.xml":
                data = data.replace(
                    b"</manifest:manifest>", b" " * (1024 * 1024 + 8) + b"encryption-data</manifest:manifest>"
                )
            out.writestr(item, data)

    assert job.convert("long.odt")["pages"] == 3


def test_a_zip_bomb_never_reaches_libreoffice(job: Job) -> None:
    """A file of a few kilobytes that unpacks past the job's bound is refused before the converter."""
    make_fixtures.make_zip_bomb(job.path("bomb.pptx"), mb=25)
    ctx = dataclasses.replace(job.ctx, file_mb=1)

    answer = convert_office({"path": "bomb.pptx"}, ctx, convert=_never)

    assert answer["error"].startswith("unreadable_file: ")
    assert "more than 20 MB" in answer["error"]
    assert job.written() == []


def test_a_damaged_file_is_unreadable_before_libreoffice(job: Job) -> None:
    """Half a document is no zip."""
    make_fixtures.make_truncated(make_fixtures.make_docx(job.path("memo.docx")), job.path("half.docx"))

    answer = job.convert("half.docx", convert=_never)

    assert answer == {"error": f"unreadable_file: {readers.DAMAGED}"}
    assert job.written() == []


def test_a_deck_without_slides_is_unsupported(job: Job, fake_office: FakeOffice) -> None:
    """A slide master with layouts and no slides is told so at once, with no conversion."""
    master = make_templates.make_master(job.path("master.pptx"), examples=0)
    make_templates.as_potx(master, job.path("master.potx"))

    answer = job.convert("master.potx")

    assert answer == {"error": NO_SLIDES}
    assert fake_office.calls == 0
    assert job.written() == []


def test_a_macro_enabled_deck_is_unsupported(job: Job, fake_office: FakeOffice) -> None:
    """A ``.pptx`` whose main part is that of a macro-enabled presentation is told so, with no conversion."""
    deck = make_fixtures.make_deck(job.path("deck.pptx"), 2, chart_on=None)
    make_templates.as_macro_pptx(deck, job.path("macro.pptx"))

    answer = job.convert("macro.pptx")

    assert set(answer) == {"error"}
    assert answer["error"].startswith("unsupported_format: a macro-enabled presentation")
    assert fake_office.calls == 0
    assert job.written() == []


def test_a_spreadsheet_is_unsupported(job: Job) -> None:
    """Spreadsheets, old binary formats, ODF slides, a PDF, a picture and a file with no extension are not converted."""
    make_fixtures.make_xlsx(job.path("sheet.xlsx"))
    for name in ("sheet.ods", "old.doc", "slides.ppt", "show.odp", "memo.rtf", "book.pdf", "photo.png", "README"):
        job.path(name).write_bytes(b"x")

    for name in (
        "sheet.xlsx",
        "sheet.ods",
        "old.doc",
        "slides.ppt",
        "show.odp",
        "memo.rtf",
        "book.pdf",
        "photo.png",
        "README",
    ):
        assert job.convert(name, convert=_never) == {"error": NOT_SUPPORTED}, name
    assert job.written() == []


def test_a_missing_file_is_not_found(job: Job) -> None:
    """A path that is no file of the chat, or lies in the job's own ``out/``, is not found; the answer names no path."""
    (job.ctx.out_dir / "converted.pdf").write_bytes(b"x")

    for name in ("none.docx", "out/converted.pdf", "out/none.pptx"):
        answer = job.convert(name, convert=_never)
        assert set(answer) == {"error"}
        assert answer["error"].startswith("file_not_found: ")
        assert name not in answer["error"]
        assert "/workspace" not in answer["error"]


def test_a_path_that_is_no_input_path_is_the_callers_mistake(job: Job) -> None:
    """An absolute path or a ``..`` is refused as ``prepare_file`` refuses it."""
    for name in ("/etc/passwd", "../memo.docx"):
        with pytest.raises(ValueError, match=r"relative|file name"):
            job.convert(name, convert=_never)


# --- what the converter may do --------------------------------------------------------------


def test_a_pdf_over_32_mib_is_refused(job: Job) -> None:
    """The cap is the answer's: a PDF past it is ``unreadable_file`` naming 32 MB, and nothing reaches ``out/``."""
    make_fixtures.make_docx(job.path("memo.docx"))

    answer = job.convert("memo.docx", convert=_writing(b"%PDF-" + bytes(CONVERTED_MAX_BYTES)))

    assert answer == {"error": "unreadable_file: the converted file is larger than 32 MB"}
    assert job.written() == []


def test_a_file_of_exactly_32_mib_is_not_too_large(job: Job) -> None:
    """The cap is a limit, not an exclusion: 32 MiB passes the size check, and is judged as a PDF (zeros are none)."""
    make_fixtures.make_docx(job.path("memo.docx"))

    answer = job.convert("memo.docx", convert=_writing(b"%PDF-" + bytes(CONVERTED_MAX_BYTES - 5)))

    assert answer == {"error": f"unreadable_file: {readers.DAMAGED}"}
    assert job.written() == []


def test_a_conversion_past_its_budget_is_read_timeout(job: Job) -> None:
    """The converter's ``ReadTimeout`` is the answer, with the budget in its words, and ``out/`` stays empty."""
    make_fixtures.make_docx(job.path("memo.docx"))

    answer = job.convert("memo.docx", budget_s=1.5, convert=_raising(readers.ReadTimeout()))

    assert answer == {
        "error": "read_timeout: converting the file took longer than 1.5 s; it is too large or too complex"
    }
    assert job.written() == []


def test_a_spent_budget_is_a_read_timeout_before_any_conversion(job: Job, fake_office: FakeOffice) -> None:
    """A job whose time is up at once answers ``read_timeout`` and converts nothing."""
    make_fixtures.make_deck(job.path("deck.pptx"), 2, chart_on=None)

    answer = job.convert("deck.pptx", budget_s=0)

    assert answer["error"].startswith("read_timeout: converting the file took longer than 0 s")
    assert fake_office.calls == 0
    assert job.written() == []


def test_the_deadline_is_taken_away_afterwards(job: Job, fake_office: FakeOffice) -> None:
    """The readers' deadline is the process's: it is cleared however the runner ends."""
    make_fixtures.make_docx(job.path("memo.docx"))

    job.convert("memo.docx", budget_s=0)
    readers.check()  # would raise ReadTimeout if the spent deadline were still set
    job.convert("memo.docx")
    readers.check()
    job.convert("memo.docx", convert=_raising(readers.ReadTimeout()))
    readers.check()


def test_a_crash_is_unreadable_and_a_missing_libreoffice_is_an_error(job: Job) -> None:
    """``UnreadableFile`` from the converter is the file's; a ``RuntimeError`` is the job's own, and propagates."""
    make_fixtures.make_docx(job.path("memo.docx"))

    crashed = job.convert("memo.docx", convert=_raising(UnreadableFile("the file could not be converted to pages")))
    with pytest.raises(RuntimeError, match="LibreOffice could not start"):
        job.convert("memo.docx", convert=_raising(RuntimeError("LibreOffice could not start")))

    assert crashed == {"error": NOT_CONVERTED}
    assert job.written() == []
    readers.check()


def test_a_converted_file_that_is_no_pdf_is_refused(job: Job) -> None:
    """A converter that wrote text, or nothing, or an empty file, has not made a PDF."""
    make_fixtures.make_docx(job.path("memo.docx"))

    for convert in (_writing(b"not a pdf at all"), _writing(b""), _leaving_nothing):
        assert job.convert("memo.docx", convert=convert) == {"error": NOT_CONVERTED}
    assert job.written() == []


def test_a_converted_file_that_is_a_link_is_refused(job: Job, tmp_path: Path) -> None:
    """Only a regular file is taken: a link to a PDF elsewhere is not followed."""
    make_fixtures.make_docx(job.path("memo.docx"))
    elsewhere = make_fixtures.make_pdf(tmp_path / "elsewhere.pdf", ["one", "two"])

    def link(source: Path, target: Path, *, deck: bool, deadline: float) -> None:
        target.symlink_to(elsewhere)

    assert job.convert("memo.docx", convert=link) == {"error": NOT_CONVERTED}
    assert job.written() == []


def test_a_converted_pdf_with_no_pages_or_a_password_is_unreadable(job: Job, tmp_path: Path) -> None:
    """The PDF is opened to count its pages: a damaged one or one that asks for a password is the file's fault."""
    make_fixtures.make_docx(job.path("memo.docx"))
    locked = make_fixtures.make_pdf(tmp_path / "locked.pdf", ["one"], password="invented")

    for data in (b"%PDF-1.4\nnothing else", locked.read_bytes()):
        answer = job.convert("memo.docx", convert=_writing(data))
        assert answer["error"].startswith("unreadable_file: ")
    assert job.written() == []


def test_the_converter_works_in_a_folder_of_its_own_and_leaves_none(job: Job, fake_office: FakeOffice) -> None:
    """The PDF is made in a new folder of the job's tmp, moved into ``out/`` once it is checked, and the folder goes."""
    make_fixtures.make_docx(job.path("memo.docx"))
    seen: list[Path] = []

    def convert(source: Path, target: Path, *, deck: bool, deadline: float) -> None:
        seen.append(target)
        fake_office(source, target, deck=deck, deadline=deadline)

    for name, run in (("ok", convert), ("refused", _writing(b"not a pdf"))):
        before = _scratch()
        job.convert("memo.docx", convert=run)
        assert _scratch() == before, name

    (target,) = seen
    assert target.name == "converted.pdf"
    assert target.parent.parent == Path(tempfile.gettempdir())
    assert job.ctx.out_dir not in target.parents
    assert not target.parent.exists()


def test_a_half_written_file_never_reaches_out(job: Job) -> None:
    """A converter that writes part of a PDF and then fails leaves ``out/`` as it was."""
    make_fixtures.make_docx(job.path("memo.docx"))

    def half_then_crash(source: Path, target: Path, *, deck: bool, deadline: float) -> None:
        target.write_bytes(b"%PDF-1.4\n1 0 obj")
        raise UnreadableFile("the file could not be converted to pages")

    assert job.convert("memo.docx", convert=half_then_crash) == {"error": NOT_CONVERTED}
    assert job.written() == []


def test_a_conversion_that_cannot_be_moved_into_out_is_unreadable(
    job: Job, fake_office: FakeOffice, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A move that fails (its text names a path) is a short fixed answer, and no half-file stays in ``out/``."""
    make_fixtures.make_docx(job.path("memo.docx"))

    def refuse(source: Any, target: Any) -> None:
        raise OSError(28, "No space left on device", str(target))

    monkeypatch.setattr(os, "replace", refuse)

    answer = job.convert("memo.docx")

    assert answer == {"error": "unreadable_file: the converted file could not be written"}
    assert job.written() == []


# --- what the answer says ------------------------------------------------------------------


def test_the_answer_names_no_path(job: Job, fake_office: FakeOffice) -> None:
    """Neither the virtual ``/workspace`` path, the job folder, the staging folder nor the file's name is in it."""
    make_fixtures.make_deck(job.path("harbour.pptx"), 2, chart_on=None)
    make_fixtures.make_docx(job.path("memo.docx"))

    for name in ("harbour.pptx", "memo.docx"):
        text = json.dumps(job.convert(name))
        assert "/workspace" not in text
        assert str(job.ctx.work_dir) not in text
        assert JOB_ID not in text
        assert name not in text
        assert "tmp" not in text
