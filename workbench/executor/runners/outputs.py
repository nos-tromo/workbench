"""The runners of ``write_table`` and ``write_document``, which write one new file from the model's values in the job.

Neither reads a file of the chat: everything comes in the job's arguments, which the api has checked; they are checked
here again, and a value that does not pass is an ``invalid_value`` that names its place and writes nothing. A table is
checked by the very rules the api checks it by (:mod:`workbench.tables`); the length of a document's Markdown and of a
hint are the api's bounds mirrored (the executor imports nothing of the api), and a test ties them. The file goes into
``out/`` under the name the api chose. The answer is a hint at most, never a path of the job.

Every value is data. A workbook cell is written by the value's type, so a string stays a string even if it looks like
a formula, a number or a web address. A CSV cell that a spreadsheet would run (``=``, ``+``, ``-``, ``@``, a tab or a
carriage return first) gets a leading ``'``. Markdown is read without raw HTML, and nothing it names is fetched: a
link is its text and its address, an image its alt text. Characters XML 1.0 cannot hold become U+FFFD in a workbook
and in a Word document, and the hint says so. All of the work runs under the job's time limit.
"""

import csv
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from workbench.executor.context import JobContext
from workbench.executor.runners.documents import error
from workbench.executor.templates import xml_safe
from workbench.executor.timer import TimeLimit, limit_until
from workbench.paths import PathError, check_filename
from workbench.protocol import encodable
from workbench.tables import DELIMITERS, table_problem, word_table_problem

#: The api's bounds (:mod:`workbench.api.tools_output`), mirrored: characters of a document's Markdown and of a hint
#: (``MAX_HINT``). A table's bounds and rules are not mirrored: both sides use :mod:`workbench.tables`.
MAX_MARKDOWN: Final = 200_000
HINT_MAX: Final = 2000

#: The first characters that make a spreadsheet run a CSV cell as a formula.
FORMULA_START: Final = ("=", "+", "-", "@", "\t", "\r")

#: The font of code, in a block and inline.
CODE_FONT: Final = "Consolas"

#: List levels a Word document shows; a list nested deeper takes the deepest.
LIST_LEVELS: Final = 3

#: What a thematic break (``---``) is written as: a paragraph of its own, as plain text.
THEMATIC_BREAK: Final = "—" * 3

_DEFAULT_BUDGET_S: Final = 60.0
_REPLACED: Final = "\ufffd"


class WriteTimeout(TimeLimit):
    """The file took longer to write than the job's time limit allows."""


class _Refused(Exception):
    """A value the runner does not write; the answer is ``invalid_value`` with this message."""


def _answered(work: Callable[[], dict[str, Any]], args: dict[str, Any]) -> dict[str, Any]:
    """Run the work under the job's time limit, turning a refusal or a timeout into the answer the api understands.

    Args:
        work: The runner's work.
        args: The runner's arguments; ``budget_s`` is the seconds the work may take.

    Returns:
        What the work returns, or an ``error`` result.
    """
    budget = float(args.get("budget_s", _DEFAULT_BUDGET_S))
    try:
        with limit_until(time.monotonic() + budget, WriteTimeout):
            return work()
    except _Refused as exc:
        return error("invalid_value", str(exc))
    except WriteTimeout:
        return error("write_timeout", f"writing the file took longer than {budget:g} s")


def _output(args: dict[str, Any], ctx: JobContext) -> Path:
    """Find where the file goes: the name the api chose, in ``out/``.

    Args:
        args: The runner's arguments.
        ctx: The job's surroundings.

    Returns:
        The path to write.

    Raises:
        _Refused: The name is not a plain file name.
    """
    try:
        return ctx.out_dir / check_filename(str(args.get("name", "")))
    except PathError:
        raise _Refused("the name is not a plain file name") from None


def _hint(text: str | None) -> dict[str, Any]:
    """Make the answer: the hint, if there is anything to say.

    Args:
        text: What the model should know, or ``None``.

    Returns:
        ``{"hint": ...}`` cut to :data:`HINT_MAX`, or ``{}``.
    """
    return {"hint": text[:HINT_MAX]} if text else {}


def _plural(count: int, noun: str) -> str:
    """Count a noun.

    Args:
        count: How many.
        noun: The noun, singular; its plural adds an ``s``.

    Returns:
        For example ``1 cell`` or ``3 cells``.
    """
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


# --- write_table --------------------------------------------------------------------------------------------------


def _table(args: dict[str, Any]) -> tuple[list[str], list[list[Any]], str]:
    """Take the table out of the arguments, every cell checked by the rules the api checks it by.

    Args:
        args: ``columns``, ``rows`` and ``sheet``.

    Returns:
        The columns' names, the rows, and the sheet's name.

    Raises:
        _Refused: A bound or a rule is broken; the message names the field, or the row and the column, from 1.
    """
    columns: Any = args.get("columns")
    rows: Any = args.get("rows")
    sheet: Any = args.get("sheet", "Sheet1")
    problem = table_problem(columns, rows, sheet)
    if problem is not None:
        raise _Refused(problem)
    return columns, rows, sheet


def _write_xlsx(path: Path, columns: list[str], rows: list[list[Any]], sheet: str) -> int:
    """Write the table as a workbook of one sheet: a bold header, the panes frozen below it, every value by its type.

    Args:
        path: Where.
        columns: The columns' names.
        rows: The rows, checked.
        sheet: The sheet's name, checked.

    Returns:
        How many texts held characters XML cannot hold, now U+FFFD.
    """
    import xlsxwriter

    replaced = 0

    def safe(text: str) -> str:
        nonlocal replaced
        written, changed = xml_safe(text)
        replaced += changed
        return written

    options = {"strings_to_formulas": False, "strings_to_numbers": False, "strings_to_urls": False, "in_memory": True}
    with xlsxwriter.Workbook(str(path), options) as book:
        worksheet = book.add_worksheet(safe(sheet))
        bold = book.add_format({"bold": True})
        for column, name in enumerate(columns):
            worksheet.write_string(0, column, safe(name), bold)
        worksheet.freeze_panes(1, 0)
        for row, values in enumerate(rows, 1):
            for column, value in enumerate(values):
                if isinstance(value, bool):
                    worksheet.write_boolean(row, column, value)
                elif value is None:
                    worksheet.write_blank(row, column, None)
                elif isinstance(value, str):
                    worksheet.write_string(row, column, safe(value))
                else:
                    worksheet.write_number(row, column, value)
    return replaced


def _csv_text(value: object) -> tuple[str, bool]:
    """Write a value as a CSV cell, a text a spreadsheet would run behind a leading ``'``.

    Args:
        value: The value, checked.

    Returns:
        The cell's text, and whether it was escaped.
    """
    if value is None:
        return "", False
    if isinstance(value, bool):
        return ("TRUE" if value else "FALSE"), False
    if isinstance(value, str):
        return (f"'{value}", True) if value.startswith(FORMULA_START) else (value, False)
    return str(value), False


def _write_csv(path: Path, columns: list[str], rows: list[list[Any]], delimiter: str) -> int:
    """Write the table as CSV, quoted as RFC 4180 does, UTF-8 with a byte order mark so that Excel reads umlauts right.

    Args:
        path: Where.
        columns: The columns' names.
        rows: The rows, checked; a short one is filled with empty cells.
        delimiter: What separates the cells: a comma, a semicolon or a tab.

    Returns:
        How many cells were escaped.
    """
    escaped = 0
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream, delimiter=delimiter)
        for values in [columns, *rows]:
            cells: list[str] = []
            for value in values:
                text, was_escaped = _csv_text(value)
                escaped += was_escaped
                cells.append(text)
            writer.writerow(cells + [""] * (len(columns) - len(cells)))
    return escaped


def write_table(args: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
    """Write a table into a new workbook or CSV file in ``out/``.

    Args:
        args: ``name`` (the file's name, with its extension), ``format`` (``xlsx`` or ``csv``), ``columns`` (the
            header's names), ``rows`` (lists of strings, numbers, bools and ``None``, none longer than ``columns``),
            ``sheet`` (the workbook's sheet), ``delimiter`` (a CSV's separator, ``,`` by default; a workbook has
            none) and ``budget_s`` (the seconds the work may take).
        ctx: The job's surroundings.

    Returns:
        ``{}``, or a ``hint`` on what was escaped or replaced; or an ``error`` result.
    """

    def work() -> dict[str, Any]:
        path = _output(args, ctx)
        if args.get("format") not in {"xlsx", "csv"}:
            raise _Refused("format: a table is written as xlsx or csv")
        columns, rows, sheet = _table(args)
        if args.get("format") == "csv":
            delimiter = args.get("delimiter", ",")
            if delimiter not in DELIMITERS:
                raise _Refused("delimiter: a CSV's cells are separated by a comma, a semicolon or a tab")
            escaped = _write_csv(path, columns, rows, delimiter)
            one = escaped == 1
            return _hint(
                f"{_plural(escaped, 'cell')} began with =, +, -, @, a tab or a carriage return and "
                f"{'was' if one else 'were'} written with a leading ' so that a spreadsheet shows "
                f"{'it' if one else 'them'} as text and does not run {'it' if one else 'them'}."
                if escaped
                else None
            )
        replaced = _write_xlsx(path, columns, rows, sheet)
        return _hint(
            f"{_plural(replaced, 'text')} (cells, column names or the sheet's name) held characters a workbook cannot "
            f"hold, such as control characters; each such character was replaced with {_REPLACED}."
            if replaced
            else None
        )

    return _answered(work, args)


# --- write_document -----------------------------------------------------------------------------------------------


@dataclass
class _List:
    """A list that is open while the Markdown is walked.

    Attributes:
        ordered: Whether it is numbered.
        start: The number of its first item.
        fresh: Whether its current item has had no paragraph yet: the first takes the list's style.
        num_id: The numbering this list's paragraphs use, made at its first item; ``None`` for bullets.
    """

    ordered: bool
    start: int
    fresh: bool = False
    num_id: int | None = None


# python-docx internals this writer relies on, below the library's public api, to stay fast (a test names each, so an
# upgrade that drops one fails clearly): ``Paragraph._p``, ``Document._body``, ``CT_Num.new``,
# ``CT_Numbering._insert_num`` and ``num_having_numId``, ``CT_Tbl.new_tbl``, ``CT_Tbl.tblGrid``, ``CT_Tbl.add_tr``,
# ``CT_Row.add_tc`` and ``docx.table._Cell``.
class _Word:
    """Writes Markdown's tokens into a Word document.

    python-docx's own ``add_paragraph`` and ``add_table`` look for the body's last element, its style setters look
    through every style, ``add_num`` through every numbering, and ``add_row`` through every row for the table's grid,
    each time: a document of tens of thousands of short paragraphs, lists or rows would take minutes. So every block is
    put before an empty paragraph at the end (the anchor, removed once the document is written), each style is looked up
    once, the text's width and a table's grid are measured once, each table row is added directly, and each numbered
    list's numbering gets the next free id without a search.

    Attributes:
        document: The python-docx document.
        replaced: Characters XML cannot hold that were replaced with U+FFFD.
        lists: The lists open, innermost last.
    """

    def __init__(self, document: Any) -> None:
        """Start writing at the end of the document.

        Args:
            document: A python-docx document.
        """
        self.document = document
        self.replaced = 0
        self.lists: list[_List] = []
        self._anchor = document.add_paragraph()
        self._styles: dict[str, str] = {}
        self._numbering = document.part.numbering_part.element
        self._next_num = max((int(num.numId) for num in self._numbering.num_lst), default=0) + 1
        self._abstract: dict[str, int] = {}
        section = document.sections[-1]
        self._width = section.page_width - section.left_margin - section.right_margin

    def close(self) -> None:
        """Remove the anchor; nothing more is written."""
        anchor = self._anchor._p
        anchor.getparent().remove(anchor)

    def _style(self, name: str) -> str:
        """Find a style's id by its name, once.

        Args:
            name: The style's name, such as ``List Bullet 2``.

        Returns:
            Its id.
        """
        if name not in self._styles:
            self._styles[name] = self.document.styles[name].style_id
        return self._styles[name]

    def new_paragraph(self, style: str | None = None) -> Any:
        """Add a paragraph at the end of the document.

        Args:
            style: Its style's name, or ``None`` for the document's default.

        Returns:
            The paragraph.
        """
        paragraph = self._anchor.insert_paragraph_before()
        if style is not None:
            paragraph._p.style = self._style(style)
        return paragraph

    def text(self, text: str) -> str:
        """Make a text writable, counting what had to go.

        Args:
            text: The text.

        Returns:
            The text with U+FFFD for each character XML 1.0 cannot hold.
        """
        written, _ = xml_safe(text)
        self.replaced += sum(1 for old, new in zip(text, written, strict=True) if old != new)
        return written

    def run(self, paragraph: Any, text: str, *, bold: bool = False, italic: bool = False, code: bool = False) -> None:
        """Add a run of text to a paragraph.

        Args:
            paragraph: The paragraph.
            text: The text; a newline in it is a line break.
            bold: Whether it is bold.
            italic: Whether it is italic.
            code: Whether it is code, in :data:`CODE_FONT`.
        """
        if not text:
            return
        run = paragraph.add_run(self.text(text))
        run.bold = bold or None
        run.italic = italic or None
        if code:
            run.font.name = CODE_FONT

    def inline(self, paragraph: Any, children: Sequence[Any], *, bold: bool = False) -> None:
        """Write a block's inline tokens into a paragraph: bold, italic, code, links and images as text.

        Args:
            paragraph: The paragraph.
            children: The inline tokens.
            bold: Whether everything is bold, as in a table's header.
        """
        strong = emphasis = 0
        links: list[tuple[str, list[str]]] = []

        def add(text: str, *, code: bool = False) -> None:
            self.run(paragraph, text, bold=bold or strong > 0, italic=emphasis > 0, code=code)
            for _, shown in links:
                shown.append(text)

        for token in children:
            kind = token.type
            if kind == "strong_open":
                strong += 1
            elif kind == "strong_close":
                strong -= 1
            elif kind == "em_open":
                emphasis += 1
            elif kind == "em_close":
                emphasis -= 1
            elif kind == "code_inline":
                add(token.content, code=True)
            elif kind == "softbreak":
                add(" ")
            elif kind == "hardbreak":
                paragraph.add_run().add_break()
            elif kind == "link_open":
                links.append((str(token.attrs.get("href", "")), []))
            elif kind == "link_close" and links:
                href, shown = links.pop()
                text = "".join(shown)
                if href and href not in {text, f"mailto:{text}"}:
                    add(f" ({href})")
            elif kind == "image":
                alt = "".join(child.content for child in token.children or [] if child.type in {"text", "code_inline"})
                add(alt or token.content)
            else:
                add(token.content)

    def paragraph(self) -> Any:
        """Start a paragraph of body text: plain, or in the list that is open.

        Returns:
            The paragraph.
        """
        if not self.lists:
            return self.new_paragraph()
        current = self.lists[-1]
        level = min(len(self.lists), LIST_LEVELS)
        suffix = "" if level == 1 else f" {level}"
        if not current.fresh:
            return self.new_paragraph(f"List Continue{suffix}")
        current.fresh = False
        style = f"{'List Number' if current.ordered else 'List Bullet'}{suffix}"
        paragraph = self.new_paragraph(style)
        if current.ordered:
            self.number(paragraph, current, style)
        return paragraph

    def number(self, paragraph: Any, numbered: _List, style: str) -> None:
        """Give a numbered list's paragraph its list's own numbering, which starts at the list's first number.

        Every paragraph of a style such as ``List Number`` shares one numbering, which would count on from one list
        to the next; each list gets a numbering of its own over the same definition instead.

        Args:
            paragraph: The paragraph.
            numbered: The list.
            style: The paragraph's style.
        """
        if numbered.num_id is None:
            from docx.oxml.numbering import CT_Num

            if style not in self._abstract:
                shared = self.document.styles[style].element.pPr.numPr.numId.val
                self._abstract[style] = self._numbering.num_having_numId(shared).abstractNumId.val
            num = CT_Num.new(self._next_num, self._abstract[style])
            num.add_lvlOverride(ilvl=0).add_startOverride(numbered.start)
            self._numbering._insert_num(num)
            numbered.num_id = self._next_num
            self._next_num += 1
        properties = paragraph._p.get_or_add_pPr().get_or_add_numPr()
        properties.get_or_add_ilvl().val = 0
        properties.get_or_add_numId().val = numbered.num_id

    def code(self, text: str) -> None:
        """Write a code block: one paragraph in :data:`CODE_FONT`, its lines kept.

        Args:
            text: The block's text.
        """
        self.run(self.new_paragraph(), text.removesuffix("\n"), code=True)

    def table(self, tokens: Sequence[Any]) -> None:
        """Write a table: a header row in bold, then its rows.

        Args:
            tokens: The tokens between ``table_open`` and ``table_close``.
        """
        rows: list[list[tuple[bool, Sequence[Any]]]] = []
        header = False
        for token in tokens:
            if token.type == "tr_open":
                rows.append([])
            elif token.type in {"th_open", "td_open"}:
                header = token.type == "th_open"
            elif token.type == "inline" and rows:
                rows[-1].append((header, token.children or []))
        width = max((len(row) for row in rows), default=0)
        if not width:
            return
        from docx.oxml.table import CT_Tbl
        from docx.table import Table, _Cell

        element = CT_Tbl.new_tbl(0, width, self._width)
        element.tblPr.style = self._style("Table Grid")
        self._anchor._p.addprevious(element)
        table = Table(element, self.document._body)
        widths = [column.w for column in element.tblGrid.gridCol_lst]
        for row in rows:
            tr = element.add_tr()  # as add_row does, with the grid read once above
            cells = [tr.add_tc() for _ in widths]
            for tc, column_width in zip(cells, widths, strict=True):
                if column_width is not None:
                    tc.width = column_width
            for tc, (bold, children) in zip(cells, row, strict=False):
                self.inline(_Cell(tc, table).paragraphs[0], children, bold=bold)

    def write(self, tokens: Sequence[Any]) -> None:
        """Write the document's tokens.

        Args:
            tokens: What markdown-it made of the Markdown.
        """
        heading: int | None = None
        index = 0
        while index < len(tokens):
            token = tokens[index]
            kind = token.type
            if kind == "heading_open":
                heading = min(int(token.tag[1:]), 4)
            elif kind == "heading_close":
                heading = None
            elif kind == "inline":
                if heading is not None:
                    self.inline(self.new_paragraph(f"Heading {heading}"), token.children or [])
                else:
                    self.inline(self.paragraph(), token.children or [])
            elif kind in {"bullet_list_open", "ordered_list_open"}:
                start = token.attrs.get("start", 1) if kind == "ordered_list_open" else 1
                self.lists.append(_List(ordered=kind == "ordered_list_open", start=int(start)))
            elif kind in {"bullet_list_close", "ordered_list_close"}:
                self.lists.pop()
            elif kind == "list_item_open" and self.lists:
                self.lists[-1].fresh = True
            elif kind == "list_item_close" and self.lists and self.lists[-1].fresh:
                # An item without a paragraph of its own (empty, or a heading or code first) keeps its bullet or
                # number, so the numbers after it hold.
                self.paragraph()
            elif kind == "hr":
                self.run(self.paragraph(), THEMATIC_BREAK)
            elif kind in {"fence", "code_block"}:
                self.code(token.content)
            elif kind == "table_open":
                end = next(i for i in range(index, len(tokens)) if tokens[i].type == "table_close")
                self.table(tokens[index + 1 : end])
                index = end
            index += 1


def _table_widths(tokens: Sequence[Any]) -> list[int]:
    """Count the columns of each table, by its header row.

    Args:
        tokens: What markdown-it made of the Markdown.

    Returns:
        Each table's columns, in the document's order.
    """
    widths: list[int] = []
    header = False
    for token in tokens:
        if token.type == "table_open":
            widths.append(0)
        elif token.type in {"thead_open", "thead_close"}:
            header = token.type == "thead_open"
        elif token.type == "th_open" and header and widths:
            widths[-1] += 1
    return widths


def write_document(args: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
    """Write a document into a new Word or Markdown file in ``out/``, from Markdown.

    A Word document has headings 1 to 4 (5 and 6 become 4), paragraphs, bold and italic, bullet and numbered lists to
    three levels, tables of up to Word's 63 columns (a wider one is refused before anything is written) and code;
    other syntax is its text.

    Args:
        args: ``name`` (the file's name, with its extension), ``format`` (``docx`` or ``md``), ``markdown`` and
            ``budget_s`` (the seconds the work may take).
        ctx: The job's surroundings.

    Returns:
        ``{}``, or a ``hint`` on characters that were replaced; or an ``error`` result.
    """

    def work() -> dict[str, Any]:
        path = _output(args, ctx)
        if args.get("format") not in {"docx", "md"}:
            raise _Refused("format: a document is written as docx or md")
        markdown = args.get("markdown")
        if not isinstance(markdown, str) or len(markdown) > MAX_MARKDOWN:
            raise _Refused(f"markdown: a text of at most {MAX_MARKDOWN} characters")
        if args.get("format") == "md":
            text: str = encodable(markdown)
            replaced = text.count(_REPLACED) - markdown.count(_REPLACED)
            with path.open("w", encoding="utf-8", newline="") as stream:
                stream.write(text)
            return _hint(
                f"{_plural(replaced, 'character')} UTF-8 cannot hold (half of a surrogate pair) "
                f"{'was' if replaced == 1 else 'were'} replaced with {_REPLACED}."
                if replaced
                else None
            )
        from docx import Document
        from markdown_it import MarkdownIt

        tokens = MarkdownIt("commonmark", {"html": False}).enable("table").parse(markdown)
        problem = word_table_problem(_table_widths(tokens))
        if problem is not None:
            raise _Refused(problem)
        word = _Word(Document())
        word.write(tokens)
        word.close()
        word.document.save(str(path))
        return _hint(
            f"{_plural(word.replaced, 'character')} a document cannot hold, such as control characters, "
            f"{'was' if word.replaced == 1 else 'were'} replaced with {_REPLACED}."
            if word.replaced
            else None
        )

    return _answered(work, args)
