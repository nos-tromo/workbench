"""HTML files, as the text a reader sees: no scripts, styles or markup, table rows on one line each."""

from collections.abc import Iterator
from pathlib import Path
from typing import Final

from bs4 import BeautifulSoup, NavigableString

from workbench.executor.readers import DocumentText, Part
from workbench.executor.readers import text as txt

_BLOCKS: Final = (
    "p div li ul ol dl dt dd h1 h2 h3 h4 h5 h6 table caption section article header footer main nav aside "
    "pre blockquote form fieldset figure figcaption address hr title"
).split()
_INVISIBLE: Final = ("script", "style", "noscript", "template")


def html_to_lines(data: bytes | str) -> list[str]:
    """Turn a page into lines of text.

    Args:
        data: The page, as bytes (its encoding is detected) or text.

    Returns:
        The visible text, one line per block, a table row on one line with its cells joined by `` | ``;
        blank lines are dropped and runs of spaces collapsed.
    """
    soup = BeautifulSoup(data, "lxml")
    for tag in soup(_INVISIBLE):
        tag.decompose()
    for row in reversed(soup.find_all("tr")):
        cells = [" ".join(cell.get_text(" ").split()) for cell in row.find_all(["td", "th"])]
        row.replace_with(NavigableString("\n" + " | ".join(cells) + "\n"))
    for tag in soup.find_all("br"):
        tag.replace_with(NavigableString("\n"))
    for tag in soup.find_all(_BLOCKS):
        tag.insert_before(NavigableString("\n"))
        tag.insert_after(NavigableString("\n"))
    lines = (" ".join(line.split()) for line in soup.get_text().splitlines())
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


def units(path: Path) -> Iterator[Part]:
    """Walk the lines of a page's text.

    Args:
        path: The file.

    Yields:
        Each line as ``line N``.
    """
    yield from txt.line_units(html_to_lines(path.read_bytes()))
