"""Word documents (``.docx``, ``.dotx``): paragraphs and tables, in document order.

The body is read straight from ``word/document.xml`` with lxml, which reads a template as readily as a
document, loads nothing external (no entities, no network, no DTD) and shows only text that is in the
document: deleted text of tracked changes, field codes, headers, footers and comments are left out.
Paragraphs are numbered from 1 over the non-empty ones; a table is ``table N`` and each of its rows
``table N, row R``.
"""

import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Final

from lxml import etree

from workbench.executor.readers import DocumentText, Part
from workbench.executor.readers import paging as pg

_W: Final = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_BODY_PART: Final = "word/document.xml"

#: Elements whose children are body content too: content controls, and the like.
_WRAPPERS: Final = frozenset({f"{_W}sdt", f"{_W}sdtContent", f"{_W}customXml", f"{_W}smartTag"})

#: Word writes a shape twice, as the modern element and as a fallback for old readers; only the first is read.
_FALLBACK: Final = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"

_P: Final = f"{_W}p"
_TBL: Final = f"{_W}tbl"
_TR: Final = f"{_W}tr"
_TC: Final = f"{_W}tc"
_T: Final = f"{_W}t"
_TAB: Final = f"{_W}tab"
_BREAKS: Final = frozenset({f"{_W}br", f"{_W}cr"})


def parser() -> etree.XMLParser:
    """Make an XML parser that loads nothing it was not given.

    Returns:
        A parser without entity resolution, network access or DTD loading.
    """
    return etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)


def _paragraph_text(paragraph: etree._Element) -> str:
    """Join the text of one paragraph.

    Args:
        paragraph: A ``w:p`` element.

    Returns:
        Its text, with tabs and line breaks as such; deleted text and field codes are not in it.
    """
    pieces: list[str] = []
    for element in paragraph.iter(_T, _TAB, *_BREAKS):
        if element.tag == _T:
            pieces.append(element.text or "")
        elif element.tag == _TAB:
            pieces.append("\t")
        else:
            pieces.append("\n")
    return "".join(pieces).strip()


def _cell_text(cell: etree._Element) -> str:
    """Join the paragraphs of a table cell on one line.

    Args:
        cell: A ``w:tc`` element.

    Returns:
        The cell's text, its paragraphs separated by a space.
    """
    paragraphs = (_paragraph_text(p) for p in cell.iter(_P))
    return " ".join(" ".join(text.split()) for text in paragraphs if text)


def _walk(parent: etree._Element, counters: list[int]) -> Iterator[Part]:
    """Walk body content: paragraphs and tables, through wrappers such as content controls.

    Args:
        parent: The body, or a wrapper inside it.
        counters: The paragraphs and tables numbered so far, in a one-item-per-kind list.

    Yields:
        ``paragraph N`` for each non-empty paragraph, ``table T, row R`` for each row of each table.
    """
    for child in parent:
        if child.tag == _P:
            text = _paragraph_text(child)
            if text:
                counters[0] += 1
                yield Part(f"paragraph {counters[0]}", text)
        elif child.tag == _TBL:
            counters[1] += 1
            for number, row in enumerate(child.iterchildren(_TR), 1):
                cells = " | ".join(_cell_text(cell) for cell in row.iterchildren(_TC))
                if cells.replace("|", "").strip():
                    yield Part(f"table {counters[1]}, row {number}", cells)
        elif child.tag in _WRAPPERS:
            yield from _walk(child, counters)


def units(path: Path) -> Iterator[Part]:
    """Walk the paragraphs and table rows.

    Args:
        path: The ``.docx`` or ``.dotx`` file, already checked as a zip.

    Yields:
        ``paragraph N``, and ``table T, row R`` for each row of a table.
    """
    with zipfile.ZipFile(path) as archive, archive.open(_BODY_PART) as stream:
        root = etree.parse(stream, parser()).getroot()
    for copy in list(root.iter(_FALLBACK)):
        parent = copy.getparent()
        if parent is not None:
            parent.remove(copy)
    body = root.find(f"{_W}body")
    if body is not None:
        yield from _walk(body, [0, 0])


def read(path: Path, *, part: str | None, max_chars: int) -> DocumentText:
    """Read paragraphs and tables from ``part`` on.

    Args:
        path: The ``.docx`` or ``.dotx`` file.
        part: ``None``, ``paragraph N`` or ``table N``.
        max_chars: The budget in bytes of JSON.

    Returns:
        The blocks that fit, whole, and where to continue.
    """
    return pg.read_blocks("docx", units(path), part=part, max_chars=max_chars)
