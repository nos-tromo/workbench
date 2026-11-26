"""HTML files, as the text a reader sees: no scripts, styles or markup, table rows on one line each."""

from collections.abc import Iterator
from pathlib import Path
from typing import Final

from bs4 import BeautifulSoup, NavigableString, Tag
from bs4.element import PageElement

from workbench.executor.readers import DocumentText, Measure, Part, tick
from workbench.executor.readers import text as txt

_BLOCKS: Final = frozenset(
    (
        "p div li ul ol dl dt dd h1 h2 h3 h4 h5 h6 table caption section article header footer main nav aside "
        "pre blockquote form fieldset figure figcaption address hr title tbody thead tfoot"
    ).split()
)
_INVISIBLE: Final = frozenset({"script", "style", "noscript", "template"})
_CELLS: Final = frozenset({"td", "th"})


def html_to_lines(data: bytes | str) -> list[str]:
    """Turn a page into lines of text.

    The tree is walked once, with a stack of its own, so the time is in proportion to the size of the page
    however many blocks stand side by side or how deeply they nest: a break at the start and end of each block,
    a row of a table on one line with its cells joined by `` | `` (a break inside a cell is a space).

    Args:
        data: The page, as bytes (its encoding is detected) or text.

    Returns:
        The visible text, one line per block; blank lines are dropped and runs of spaces collapsed.
    """
    soup = BeautifulSoup(data, "lxml")
    out: list[str] = []
    cell_depth = 0
    cells_in_row: list[int] = []

    def split() -> None:
        out.append(" " if cell_depth else "\n")

    stack: list[tuple[Iterator[PageElement], str]] = [(iter(soup.contents), "")]
    while stack:
        tick()
        children, closing = stack[-1]
        node = next(children, None)
        if node is None:
            stack.pop()
            if closing == "tr":
                split()
                if cells_in_row:
                    cells_in_row.pop()
            elif closing in _CELLS:
                cell_depth -= 1
            elif closing in _BLOCKS:
                split()
            continue
        if isinstance(node, Tag):
            name = node.name
            if name in _INVISIBLE:
                continue
            if name == "br":
                split()
                continue
            if name == "tr":
                split()
                cells_in_row.append(0)
            elif name in _CELLS:
                if cells_in_row:
                    if cells_in_row[-1]:
                        out.append(" | ")
                    cells_in_row[-1] += 1
                cell_depth += 1
            elif name in _BLOCKS:
                split()
            stack.append((iter(node.contents), name))
        elif type(node) is NavigableString:
            out.append(str(node))
    lines = (" ".join(line.split()) for line in "".join(out).splitlines())
    return [line for line in lines if line]


def read(path: Path, *, part: str | None, max_chars: int) -> DocumentText:
    """Read a page's text from ``part`` on.

    Args:
        path: The ``.html`` or ``.htm`` file.
        part: ``None``, ``line N`` or ``lines N-M``, counted in the text, not the markup.
        max_chars: The budget in bytes of JSON.

    Returns:
        The lines that fit and where to continue.
    """
    return txt.read_lines("html", html_to_lines(path.read_bytes()), part=part, max_chars=max_chars)


def measure(path: Path, *, chars_cap: int) -> Measure:
    """Count the lines of a page's text and their characters.

    Args:
        path: The file.
        chars_cap: The most characters to count.

    Returns:
        The measure.
    """
    return txt.measure_lines("html", html_to_lines(path.read_bytes()), chars_cap=chars_cap)


def units(path: Path) -> Iterator[Part]:
    """Walk the lines of a page's text.

    Args:
        path: The file.

    Yields:
        Each line as ``line N``.
    """
    yield from txt.line_units(html_to_lines(path.read_bytes()))
