"""The test process gets the thread limits every job has, before any test module is imported; and one time factor.

Most runner tests run their code in this process, and importing numpy (which openpyxl does whenever it is installed)
would otherwise start a pool of BLAS threads, one per CPU, none of which blocks a signal. A job never has that pool:
the sandbox gives it ``OPENBLAS_NUM_THREADS=1`` and ``OMP_NUM_THREADS=1`` (``CLEAN_ENV``). The timer tests need it
gone too: a signal one thread holds back is handed to any other thread (``alarm_held`` in
``tests/executor/test_run_python.py``).

``WORKBENCH_TEST_TIME_SCALE`` (1 by default; CI sets 3) says how much slower than a developer's machine the host may
be. Every upper bound a test puts on the time of work it runs, where the bound is within about four times what that
work takes on a developer's machine, is ``scaled``. A deadline the product itself keeps is never scaled: a test that
needs more room for one gives the work a larger budget or a smaller input, and a lower bound (a deadline must not fire
early) stays as it is.

``fake_office`` puts a converter that writes invented PDF pages in place of LibreOfficeKit's (``office.convert``), which
runs only in the executor image.
"""

import os
from pathlib import Path
from typing import Final

import pytest

from workbench.executor.sandbox import CLEAN_ENV

for _name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS"):
    os.environ[_name] = CLEAN_ENV[_name]


def _time_scale(raw: str | None) -> float:
    """Read the time factor.

    Args:
        raw: ``WORKBENCH_TEST_TIME_SCALE``, or ``None``.

    Returns:
        The factor: 1 when unset or blank.

    Raises:
        ValueError: It is not a number of at least 1.
    """
    if raw is None or not raw.strip():
        return 1.0
    value = float(raw)
    if not value >= 1:
        raise ValueError("WORKBENCH_TEST_TIME_SCALE must be a number of at least 1")
    return value


#: How much slower than a developer's machine the host may be.
TIME_SCALE: Final = _time_scale(os.environ.get("WORKBENCH_TEST_TIME_SCALE"))


def scaled(seconds: float) -> float:
    """Widen an upper bound on the time of work a test runs by the host's factor.

    Args:
        seconds: The bound on a developer's machine.

    Returns:
        The bound on this host.
    """
    return seconds * TIME_SCALE


class FakeOffice:
    """A stand-in for ``office.convert``, for tests on any host: it writes a PDF of invented pages, no LibreOffice.

    A deck gives one page per slide, the text of its text frames, or ``None`` (a page of a picture and no text) for a
    slide without a text frame, and leaves out the slides in ``drop`` (a build of LibreOffice that does not draw a
    hidden slide). A document gives ``pages`` pages of invented text. It never reads the file's words besides. A page
    range draws those pages alone; past ``ceiling`` pages, a conversion writes its PDF (unless ``saves_at_ceiling`` is
    off) and then comes to the ceiling of its address space (``office.AtCeiling``), as LibreOffice does when it has
    left pictures out.

    Attributes:
        drop: The slide numbers, counted from 1, a deck's PDF leaves out.
        pages: How many pages a document's PDF has.
        ceiling: The most pages one conversion draws without coming to its ceiling; ``None`` for no ceiling.
        saves_at_ceiling: Whether a conversion that comes to its ceiling writes its PDF first, as Writer does;
            Impress may fail the save instead.
        calls: How many conversions were asked for.
        decks: The ``deck`` argument of each of them, in order.
        ranges: The ``pages`` argument of each of them, in order.
        headrooms: The ``headroom`` argument of each of them, in order.
    """

    def __init__(self) -> None:
        """Start with no slide left out, three pages for a document, no ceiling and no call."""
        self.drop: set[int] = set()
        self.pages: int = 3
        self.ceiling: int | None = None
        self.saves_at_ceiling: bool = True
        self.calls: int = 0
        self.decks: list[bool] = []
        self.ranges: list[tuple[int, int] | None] = []
        self.headrooms: list[int] = []

    def __call__(
        self,
        source: Path,
        target: Path,
        *,
        deck: bool,
        deadline: float,
        headroom: int,
        pages: tuple[int, int] | None = None,
    ) -> None:
        """Write the PDF of ``source`` at ``target``.

        Args:
            source: The Office file; read only when it is a deck.
            target: Where the PDF goes.
            deck: Whether ``source`` is a presentation.
            deadline: Ignored; the fake never waits.
            headroom: Recorded; the fake's ceiling is ``ceiling``.
            pages: The first and last page to draw, or ``None`` for all.

        Raises:
            AtCeiling: More than ``ceiling`` pages were drawn; the PDF is written all the same, or not at all.
        """
        from tests.fixtures import make_fixtures
        from workbench.executor.readers import pptx
        from workbench.executor.render import office

        self.calls += 1
        self.decks.append(deck)
        self.ranges.append(pages)
        self.headrooms.append(headroom)
        drawn: list[str | None] = []
        if deck:
            for number, slide in enumerate(pptx.open_presentation(source).slides, 1):
                if number in self.drop:
                    continue
                frames = [shape.text_frame.text for shape in slide.shapes if shape.has_text_frame]
                drawn.append("\n".join(frames) if frames else None)
        else:
            drawn = [f"Invented page {number}" for number in range(1, self.pages + 1)]
        if pages is not None:
            drawn = drawn[pages[0] - 1 : pages[1]]
        at_ceiling = self.ceiling is not None and len(drawn) > self.ceiling
        if self.saves_at_ceiling or not at_ceiling:
            make_fixtures.make_pdf(target, drawn)
        if at_ceiling:
            raise office.AtCeiling


@pytest.fixture
def fake_office(monkeypatch: pytest.MonkeyPatch) -> FakeOffice:
    """``office.convert`` replaced by a :class:`FakeOffice`, which the test may set (``drop``, ``pages``) and ask.

    Returns:
        The fake, which is the converter in place.
    """
    from workbench.executor.render import office

    fake = FakeOffice()
    monkeypatch.setattr(office, "convert", fake)
    return fake
