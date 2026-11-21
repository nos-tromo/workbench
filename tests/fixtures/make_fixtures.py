"""Build small synthetic documents at test time.

Every builder writes one file and returns its path. All text is invented. The libraries that
write the files are the ones the executor image carries (python-docx, openpyxl, python-pptx,
odfpy, and Pillow for the pictures of noise, through ``make_images``) plus reportlab, a dev
dependency, for PDFs.
"""

import io
import re
import struct
import zipfile
import zlib
from collections.abc import Callable, Sequence
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from docx import Document
from docx import shared as docx_shared
from docx.table import _Cell
from odf.opendocument import OpenDocumentSpreadsheet, OpenDocumentText
from odf.table import Table, TableCell, TableRow
from odf.text import H, P
from openpyxl import Workbook
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.util import Inches
from pypdf import PdfWriter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

from tests.fixtures import make_images

OLE_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")
_MIB = 1024 * 1024


def _png(size: int = 8, height: int | None = None) -> bytes:
    """A small grey PNG, written by hand so no imaging library is needed: ``size`` wide, square unless ``height``."""
    tall = size if height is None else height
    rows = b"".join(b"\x00" + b"\x80" * size for _ in range(tall))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", size, tall, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def make_pdf(path: Path, pages: Sequence[str | None], *, password: str | None = None) -> Path:
    """Write a PDF; a page given as ``None`` holds only a picture, as a scan does, and no text layer.

    Args:
        path: Where to write it.
        pages: The text of each page, or ``None`` for a page with an image and no text.
        password: If given, the PDF is encrypted with it as the user password.

    Returns:
        ``path``.
    """
    canvas = Canvas(str(path), encrypt=password)
    for text in pages:
        if text is None:
            canvas.drawImage(ImageReader(io.BytesIO(_png())), 72, 600, width=200, height=200)
        else:
            y = 760
            for line in text.splitlines():
                canvas.drawString(72, y, line)
                y -= 16
        canvas.showPage()
    canvas.save()
    return path


def make_docx(path: Path) -> Path:
    """Write a Word document: a title, two paragraphs, a table, then a closing paragraph.

    Args:
        path: Where to write it.

    Returns:
        ``path``.
    """
    doc = Document()
    doc.add_heading("Quarterly figures review", level=1)
    doc.add_paragraph("The harbour project is on schedule.")
    doc.add_paragraph("")
    doc.add_paragraph("Budget notes follow in the table below.")
    table = doc.add_table(rows=3, cols=3)
    for r, row in enumerate([("Item", "Planned", "Actual"), ("Dredging", "120", "118"), ("Piers", "340", "351")]):
        for c, value in enumerate(row):
            table.cell(r, c).text = value
    doc.add_paragraph("Contact the invented office for questions.")
    doc.save(str(path))
    return path


def as_template(docx: Path, target: Path) -> Path:
    """Copy a Word document as a ``.dotx`` template: the same parts under the template content type.

    Args:
        docx: A document made by :func:`make_docx`.
        target: Where to write the template.

    Returns:
        ``target``.
    """
    with zipfile.ZipFile(docx) as src, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as out:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "[Content_Types].xml":
                data = data.replace(b"wordprocessingml.document.main+xml", b"wordprocessingml.template.main+xml")
            out.writestr(item, data)
    return target


def make_xlsx(path: Path, *, rows: int = 5, formula: bool = True) -> Path:
    """Write a workbook: ``Sheet1`` with a header and ``rows`` data rows, and a second small sheet.

    Column F of every data row is ``=C<n>*2`` when ``formula`` is set; openpyxl stores no cached value.

    Args:
        path: Where to write it.
        rows: Data rows below the header; large counts use a write-only workbook.
        formula: Whether column F holds a formula.

    Returns:
        ``path``.
    """
    book = Workbook(write_only=True)
    sheet = book.create_sheet("Sheet1")
    sheet.append(["id", "name", "amount", "note", "region", "doubled"])
    for n in range(2, rows + 2):
        sheet.append([n, f"item {n}", n * 1.5, "synthetic", "north", f"=C{n}*2" if formula else None])
    other = book.create_sheet("Summary")
    other.append(["total", "harbour budget"])
    other.append([42, "approved"])
    book.save(str(path))
    return _declare_sizes(path, {"sheet1": f"A1:F{rows + 1}", "sheet2": "A1:B2"})


def _declare_sizes(xlsx: Path, sizes: dict[str, str]) -> Path:
    """Add the ``dimension`` element Excel writes and openpyxl's write-only mode leaves out.

    Without it, openpyxl has to read a whole sheet to open it, which makes a 100 000-row fixture slow to open.

    Args:
        xlsx: A workbook, rewritten in place.
        sizes: The range of each sheet part, by the part's file name without its extension.

    Returns:
        ``xlsx``.
    """
    with zipfile.ZipFile(xlsx) as src:
        items = [(info, src.read(info.filename)) for info in src.infolist()]
    with zipfile.ZipFile(xlsx, "w", zipfile.ZIP_DEFLATED) as out:
        for info, data in items:
            name = info.filename.removeprefix("xl/worksheets/").removesuffix(".xml")
            if info.filename.startswith("xl/worksheets/") and name in sizes:
                data = data.replace(b"<sheetViews>", f'<dimension ref="{sizes[name]}"/><sheetViews>'.encode(), 1)
            out.writestr(info, data)
    return xlsx


def with_cached_value(xlsx: Path, cell: str, value: str) -> Path:
    """Give one formula cell a cached value, as Excel does when it saves; openpyxl cannot.

    Args:
        xlsx: A workbook made by :func:`make_xlsx`, rewritten in place.
        cell: The formula cell's address on ``Sheet1``, for example ``F2``.
        value: The cached result.

    Returns:
        ``xlsx``.
    """
    pattern = re.compile(rf'(<c r="{cell}"[^>]*><f>[^<]*</f>)<v></v>'.encode())
    with zipfile.ZipFile(xlsx) as src:
        items = [(info, src.read(info.filename)) for info in src.infolist()]
    with zipfile.ZipFile(xlsx, "w", zipfile.ZIP_DEFLATED) as out:
        for info, data in items:
            if info.filename.startswith("xl/worksheets/sheet1"):
                data = pattern.sub(rb"\1<v>" + value.encode() + rb"</v>", data)
            out.writestr(info, data)
    return xlsx


def make_pptx(path: Path) -> Path:
    """Write a presentation: two slides, the first with a table and speaker notes.

    Args:
        path: Where to write it.

    Returns:
        ``path``.
    """
    deck: Any = Presentation()
    first = deck.slides.add_slide(deck.slide_layouts[5])
    first.shapes.title.text = "Harbour project status"
    shape = first.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(4), Inches(1))
    for r, row in enumerate([("Phase", "State"), ("Dredging", "done")]):
        for c, value in enumerate(row):
            shape.table.cell(r, c).text = value
    first.notes_slide.notes_text_frame.text = "Mention the invented budget."
    second = deck.slides.add_slide(deck.slide_layouts[1])
    second.shapes.title.text = "Next steps"
    second.placeholders[1].text = "Build the northern pier"
    deck.save(str(path))
    return path


def make_odt(path: Path) -> Path:
    """Write an OpenDocument text: a heading, two paragraphs and a small table.

    Args:
        path: Where to write it.

    Returns:
        ``path``.
    """
    doc: Any = OpenDocumentText()
    doc.text.addElement(H(outlinelevel=1, text="Survey summary"))
    doc.text.addElement(P(text="The invented survey covers three harbours."))
    table = Table(name="Results")
    for row in (("Harbour", "Depth"), ("North", "12")):
        tr = TableRow()
        for value in row:
            cell = TableCell()
            cell.addElement(P(text=value))
            tr.addElement(cell)
        table.addElement(tr)
    doc.text.addElement(table)
    doc.text.addElement(P(text="End of the synthetic survey."))
    doc.save(str(path))
    return path


def make_ods(path: Path) -> Path:
    """Write an OpenDocument spreadsheet: one sheet of numbers and words, then a long run of blank rows.

    Args:
        path: Where to write it.

    Returns:
        ``path``.
    """
    doc: Any = OpenDocumentSpreadsheet()
    table = Table(name="Depths")
    for name, depth in (("North", 12), ("South", 9)):
        row = TableRow()
        word = TableCell(valuetype="string")
        word.addElement(P(text=name))
        number = TableCell(valuetype="float", value=depth)
        number.addElement(P(text=str(depth)))
        row.addElement(word)
        row.addElement(number)
        table.addElement(row)
    table.addElement(TableRow(numberrowsrepeated=1000000))
    doc.spreadsheet.addElement(table)
    doc.save(str(path))
    return path


def make_eml(path: Path) -> Path:
    """Write a message with a text body and one attachment.

    Args:
        path: Where to write it.

    Returns:
        ``path``.
    """
    msg = EmailMessage()
    msg["From"] = "sender@example.invalid"
    msg["To"] = "reader@example.invalid"
    msg["Subject"] = "Pier inspection"
    msg["Date"] = "Mon, 02 Mar 2026 09:30:00 +0000"
    msg.set_content("Hello,\nthe inspection of the northern pier is done.\nRegards")
    msg.add_attachment(b"%PDF-1.4 synthetic", maintype="application", subtype="pdf", filename="inspection-report.pdf")
    path.write_bytes(msg.as_bytes())
    return path


def make_html(path: Path) -> Path:
    """Write a page with a title, text, a table, and a script and style block that must not be read.

    Args:
        path: Where to write it.

    Returns:
        ``path``.
    """
    path.write_text(
        "<html><head><title>Harbour page</title><style>p { color: red }</style></head><body>"
        "<script>var hidden = 'script text';</script>"
        "<h1>Harbour notes</h1><p>The invented quay is open.</p>"
        "<table><tr><td>Berth</td><td>Free</td></tr><tr><td>7</td><td>yes</td></tr></table>"
        "</body></html>",
        encoding="utf-8",
    )
    return path


def make_latin1_txt(path: Path) -> Path:
    """Write a text file in Latin-1, with accented words, that is not valid UTF-8.

    Args:
        path: Where to write it.

    Returns:
        ``path``.
    """
    path.write_bytes("Café crème brûlée, façade und Größe\nzweite Zeile mit Müller\n".encode("latin-1"))
    return path


def make_ole(path: Path) -> Path:
    """Write what a password-protected Office file is: an old-style compound file, not a zip.

    Args:
        path: Where to write it, with an Office extension.

    Returns:
        ``path``.
    """
    path.write_bytes(OLE_MAGIC + bytes(1024))
    return path


def make_truncated(source: Path, target: Path) -> Path:
    """Copy the first half of a file, as an interrupted download leaves it.

    Args:
        source: A complete file.
        target: Where to write the half.

    Returns:
        ``target``.
    """
    data = source.read_bytes()
    target.write_bytes(data[: len(data) // 2])
    return target


def make_zip_bomb(path: Path, *, mb: int = 25) -> Path:
    """Write a Word file of a few kilobytes whose main part expands to ``mb`` megabytes of spaces.

    Args:
        path: Where to write it, with a ``.docx`` extension.
        mb: Size of the expanded part, in megabytes.

    Returns:
        ``path``.
    """
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        )
        with zf.open("word/document.xml", "w", force_zip64=True) as member:
            member.write(b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>')
            block = b" " * _MIB
            for _ in range(mb):
                member.write(block)
            member.write(b"</w:body></w:document>")
    return path


_ENTITY_BOMB = "".join(f'<!ENTITY l{n} "{f"&l{n - 1};" * 10}">' for n in range(1, 8))
_ENTITY_USES = {"none": None, "external": "before &x; after", "bomb": "before &l7; after"}


def with_entities(path: Path, member: str, old_text: str, secret: Path, *, use: str) -> Path:
    """Declare entities in one XML part of a zip file, and use one of them in its text.

    The declared entities are ``x``, a local file (a ``SYSTEM`` entity), and ``l7``, a billion-laughs chain
    (ten million times ``ha``). The text ``old_text`` of the part is replaced by the entity references that
    ``use`` names; with ``none`` the declarations are there and no reference is.

    Args:
        path: An Office or OpenDocument file, rewritten in place.
        member: The XML part to change, such as ``word/document.xml``.
        old_text: Text that is in that part.
        secret: A local file whose content must never appear in what is read.
        use: ``none``, ``external`` (``before &x; after``) or ``bomb`` (``before &l7; after``).

    Returns:
        ``path``.
    """
    with zipfile.ZipFile(path) as src:
        items = [(info, src.read(info.filename)) for info in src.infolist()]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as out:
        for info, data in items:
            if info.filename == member:
                xml = data.decode("utf-8")
                assert old_text in xml, member
                replacement = _ENTITY_USES[use]
                if replacement is not None:
                    xml = xml.replace(old_text, replacement, 1)
                head = xml.index("?>") + 2 if xml.startswith("<?xml") else 0
                root = re.search(r"<([A-Za-z_][\w:.-]*)", xml[head:])
                assert root is not None
                doctype = (
                    f'<!DOCTYPE {root[1]} [<!ENTITY l0 "ha">{_ENTITY_BOMB}<!ENTITY x SYSTEM "{secret.as_uri()}">]>'
                )
                data = (xml[:head] + doctype + xml[head:]).encode("utf-8")
            out.writestr(info, data)
    return path


def make_restricted_pdf(
    path: Path, pages: Sequence[str | None], *, user_password: str = "", algorithm: str = "AES-128"
) -> Path:
    """Write a PDF that is encrypted, with an owner password and a user password that may be empty.

    With an empty user password any reader opens it, as a browser does for a PDF that only restricts printing or
    copying; with another one it asks for the password.

    Args:
        path: Where to write it.
        pages: The text of each page, or ``None`` for a page without a text layer.
        user_password: The password needed to open it; empty for none.
        algorithm: ``RC4-128``, ``AES-128`` or ``AES-256``.

    Returns:
        ``path``.
    """
    plain = make_pdf(path.with_name(path.name + ".plain"), pages)
    writer = PdfWriter(clone_from=str(plain))
    writer.encrypt(user_password=user_password, owner_password="synthetic-owner-password", algorithm=algorithm)
    with path.open("wb") as handle:
        writer.write(handle)
    plain.unlink()
    return path


def make_sparse_xlsx(
    path: Path, *, row: int = 2_000_000_000, column: str = "B", moved: bool = False, part: str | None = None
) -> Path:
    """Write a workbook whose second row of data is numbered ``row`` and whose last cell is in ``column``.

    Args:
        path: Where to write it.
        row: The number of the second row; no spreadsheet has more than 1 048 576.
        column: The column of the second row's cells; none goes past ``XFD``.
        moved: Whether the worksheet part is ``xl/ws/s1.xml``, named so in the workbook's relationships.
        part: Another name for the worksheet part, below ``xl/`` (``ws/s1.bin``), named so in the relationships.

    Returns:
        ``path``.
    """
    target = part or ("ws/s1.xml" if moved else None)
    book = Workbook()
    sheet: Any = book.active
    sheet.title = "Sheet1"
    sheet.append(["a", "b"])
    sheet.append(["c", "d"])
    book.save(str(path))
    with zipfile.ZipFile(path) as src:
        items = [(info, src.read(info.filename)) for info in src.infolist()]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as out:
        for info, data in items:
            name = info.filename
            if name == "xl/worksheets/sheet1.xml":
                xml = data.decode()
                xml = xml.replace('<row r="2"', f'<row r="{row}"').replace('r="A2"', f'r="A{row}"')
                xml = xml.replace('r="B2"', f'r="{column}{row}"')
                data = xml.encode()
                name = f"xl/{target}" if target else name
            elif target and name in {"[Content_Types].xml", "xl/_rels/workbook.xml.rels"}:
                data = data.replace(b"worksheets/sheet1.xml", target.encode())
            out.writestr(name, data)
    return path


def make_tag_storm_xlsx(path: Path, tag: str, count: int) -> Path:
    """Write a small workbook whose worksheet has a comment of ``count`` unclosed tags, ``<row<row<row...``.

    A pattern that runs from each ``<row`` to the next ``>`` reads the rest of the run from every one of them.

    Args:
        path: Where to write it.
        tag: ``row`` or ``c``.
        count: How many times ``<tag`` is repeated.

    Returns:
        ``path``.
    """
    make_sparse_xlsx(path, row=2)
    storm = b"<!--" + f"<{tag}".encode() * count + b"-->"
    return _rewrite_xlsx(
        path, {"xl/worksheets/sheet1.xml": lambda data: data.replace(b"<sheetData>", storm + b"<sheetData>")}
    )


def make_orphan_part_xlsx(path: Path) -> Path:
    """Write a small workbook with a stray worksheet part that no relationship names, with a row past the end.

    openpyxl never opens a part that the workbook's relationships do not name.

    Args:
        path: Where to write it.

    Returns:
        ``path``.
    """
    make_sparse_xlsx(path, row=2)
    stray = b'<worksheet><sheetData><row r="2000000000"><c r="A2000000000"/></row></sheetData></worksheet>'
    return _rewrite_xlsx(path, {}, {"xl/worksheets/sheet9.xml": stray})


def make_many_relationships_xlsx(path: Path, count: int) -> Path:
    """Write a small workbook whose relationships part has ``count`` entries in all, the extra ones naming its styles.

    Args:
        path: Where to write it.
        count: How many relationships; the workbook has three of its own.

    Returns:
        ``path``.
    """
    make_sparse_xlsx(path, row=2)
    extra = "".join(
        f'<Relationship Type="urn:synthetic:extra" Target="styles.xml" Id="rIdX{n}"/>' for n in range(count - 3)
    )
    return _rewrite_xlsx(
        path,
        {
            "xl/_rels/workbook.xml.rels": lambda data: data.replace(
                b"</Relationships>", f"{extra}</Relationships>".encode()
            )
        },
    )


def make_many_sheets_xlsx(path: Path, sheets: int) -> Path:
    """Write a workbook of ``sheets`` sheets, each with one cell.

    Args:
        path: Where to write it.
        sheets: How many sheets.

    Returns:
        ``path``.
    """
    book = Workbook(write_only=True)
    for n in range(sheets):
        book.create_sheet(f"s{n}").append([n])
    book.save(str(path))
    return path


def make_many_blocks_html(path: Path, count: int) -> Path:
    """Write a page of ``count`` paragraphs side by side.

    Args:
        path: Where to write it.
        count: How many paragraphs.

    Returns:
        ``path``.
    """
    path.write_text("<html><body>" + "<p>x</p>" * count + "</body></html>", encoding="utf-8")
    return path


def make_many_sheets_ods(path: Path, sheets: int, *, data_in_last: bool = False) -> Path:
    """Write a spreadsheet of ``sheets`` sheets, empty but for a cell in the last one if asked.

    Args:
        path: Where to write it.
        sheets: How many sheets.
        data_in_last: Whether the last sheet holds one cell.

    Returns:
        ``path``.
    """
    ns = (
        'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"'
    )
    tables = "".join(f'<table:table table:name="s{n}"/>' for n in range(sheets - 1))
    last = (
        '<table:table table:name="last"><table:table-row><table:table-cell office:value-type="string">'
        "<text:p>only data</text:p></table:table-cell></table:table-row></table:table>"
        if data_in_last
        else f'<table:table table:name="s{sheets - 1}"/>'
    )
    content = (
        f'<?xml version="1.0"?><office:document-content {ns}><office:body><office:spreadsheet>'
        f"{tables}{last}</office:spreadsheet></office:body></office:document-content>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("content.xml", content)
    return path


def make_big_table_docx(path: Path, rows: int) -> Path:
    """Write a Word document with a paragraph, a table of ``rows`` rows and a closing paragraph.

    Args:
        path: Where to write it.
        rows: How many rows the table has.

    Returns:
        ``path``.
    """
    doc = Document()
    doc.add_paragraph("Before the table.")
    table: Any = doc.add_table(rows=rows, cols=3)
    # table.cell(r, c) builds the whole grid on each call, which makes a long table quadratic; go by the rows.
    for r, tr in enumerate(table._tbl.tr_lst):
        for c, tc in enumerate(tr.tc_lst):
            _Cell(tc, table).text = (f"item {r + 1}", f"synthetic value {r + 1}", str((r + 1) * 3))[c]
    doc.add_paragraph("After the table.")
    doc.save(str(path))
    return path


def make_big_table_odt(path: Path, rows: int) -> Path:
    """Write an OpenDocument text with a table of ``rows`` rows between two paragraphs.

    Args:
        path: Where to write it.
        rows: How many rows the table has.

    Returns:
        ``path``.
    """
    doc: Any = OpenDocumentText()
    doc.text.addElement(P(text="Before the table."))
    table = Table(name="Big")
    for r in range(rows):
        tr = TableRow()
        for value in (f"item {r + 1}", f"synthetic value {r + 1}", str((r + 1) * 3)):
            cell = TableCell()
            cell.addElement(P(text=value))
            tr.addElement(cell)
        table.addElement(tr)
    doc.text.addElement(table)
    doc.text.addElement(P(text="After the table."))
    doc.save(str(path))
    return path


def _rewrite_xlsx(
    path: Path, changes: dict[str, Callable[[bytes], bytes]], extra: dict[str, bytes] | None = None
) -> Path:
    """Rewrite parts of a workbook in place.

    Args:
        path: A workbook made by :func:`make_xlsx`.
        changes: For each part name, a function from its content to its new content.
        extra: Parts to add, by name.

    Returns:
        ``path``.
    """
    with zipfile.ZipFile(path) as src:
        items = [(info, src.read(info.filename)) for info in src.infolist()]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as out:
        for info, data in items:
            out.writestr(info, changes[info.filename](data) if info.filename in changes else data)
        for name, data in (extra or {}).items():
            out.writestr(name, data)
    return path


def make_many_entries_xlsx(path: Path, entries: int, *, moved_workbook: bool = False) -> Path:
    """Write a workbook whose ``workbook.xml`` lists ``entries`` sheets that all name the one worksheet part.

    openpyxl opens a worksheet for each entry, however few parts there are.

    Args:
        path: Where to write it.
        entries: How many ``sheet`` entries.
        moved_workbook: Whether the workbook part is called ``xl/wb.xml``, found through the content types as
            openpyxl finds it, instead of ``xl/workbook.xml``.

    Returns:
        ``path``.
    """
    make_xlsx(path, rows=2)

    def many(data: bytes) -> bytes:
        text = data.decode()
        match = re.search(r"<sheet [^>]*/>", text)
        assert match is not None
        one = re.sub(r'name="[^"]*"', 'name="s{i}"', re.sub(r'sheetId="\d+"', 'sheetId="{i}"', match[0]))
        return text.replace(match[0], "".join(one.format(i=i + 1) for i in range(entries))).encode()

    if not moved_workbook:
        return _rewrite_xlsx(path, {"xl/workbook.xml": many})
    with zipfile.ZipFile(path) as src:
        items = [(info, src.read(info.filename)) for info in src.infolist()]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as out:
        for info, data in items:
            name = info.filename
            if name == "xl/workbook.xml":
                name, data = "xl/wb.xml", many(data)
            elif name == "xl/_rels/workbook.xml.rels":
                name = "xl/_rels/wb.xml.rels"
            elif name in {"[Content_Types].xml", "_rels/.rels"}:
                data = data.replace(b"/xl/workbook.xml", b"/xl/wb.xml").replace(b"xl/workbook.xml", b"xl/wb.xml")
            out.writestr(name, data)
    return path


def make_shared_strings_xlsx(path: Path, strings: int) -> Path:
    """Write a workbook with ``strings`` tiny shared strings, which openpyxl reads whole when it opens the file.

    Args:
        path: Where to write it.
        strings: How many ``si`` elements.

    Returns:
        ``path``.
    """
    make_xlsx(path, rows=2)
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    override = (
        b'<Override PartName="/xl/sharedStrings.xml" '
        b'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/></Types>'
    )
    _rewrite_xlsx(path, {"[Content_Types].xml": lambda data: data.replace(b"</Types>", override)})
    with zipfile.ZipFile(path, "a", zipfile.ZIP_DEFLATED) as archive:
        with archive.open("xl/sharedStrings.xml", "w", force_zip64=True) as member:
            member.write(f'<?xml version="1.0"?><sst xmlns="{ns}" count="{strings}" uniqueCount="{strings}">'.encode())
            block = b"<si><t>a</t></si>" * 10_000
            for _ in range(strings // 10_000):
                member.write(block)
            member.write(b"<si><t>a</t></si>" * (strings % 10_000))
            member.write(b"</sst>")
    return path


def make_defined_names_xlsx(path: Path, names: int) -> Path:
    """Write a workbook that defines ``names`` names.

    Args:
        path: Where to write it.
        names: How many ``definedName`` elements.

    Returns:
        ``path``.
    """
    make_xlsx(path, rows=2)
    defined = "<definedNames>" + "".join(f'<definedName name="n{i}">Sheet1!$A$1</definedName>' for i in range(names))
    return _rewrite_xlsx(
        path,
        {
            "xl/workbook.xml": lambda data: data.replace(
                b"</sheets>", b"</sheets>" + (defined + "</definedNames>").encode()
            )
        },
    )


_WORKBOOK_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
_STRINGS_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"


def make_bypass_xlsx(path: Path, kind: str, count: int) -> Path:
    """Write a workbook that opens ``count`` sheets or reads ``count`` shared strings through a loophole.

    Each kind is a way the part or the entries are named that openpyxl 3.1.5 understands and a count that looks
    for the usual names does not:

    - ``tag``: the children of ``sheets`` are called ``x``, not ``sheet``; openpyxl makes a sheet of each;
    - ``default``: there is no ``Override`` for the workbook, ``Default Extension="xml"`` has its content type,
      and openpyxl falls back to ``xl/workbook.xml``;
    - ``noslash``: the workbook is ``xl/wb.xml`` and its part name is ``Xxl/wb.xml``, of which openpyxl drops
      exactly one character;
    - ``dslash``: the part name is ``//xl/wb.xml`` and the zip member ``/xl/wb.xml``;
    - ``views``: ``count`` ``workbookView`` entries in ``bookViews``, which openpyxl also turns into objects;
    - ``strings-noslash``: ``count`` shared strings in ``xl/sharedStrings.xml``, declared as ``Xxl/sharedStrings.xml``.

    Args:
        path: Where to write it.
        kind: One of the above.
        count: How many sheet entries, or shared strings for ``strings-noslash``.

    Returns:
        ``path``.
    """
    base = Workbook()
    sheet: Any = base.active
    sheet.title = "Sheet1"
    sheet.append(["a", "b"])
    base.save(str(path))
    namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

    def many(text: str) -> str:
        if kind == "views":
            return text.replace("</bookViews>", "<workbookView/>" * count + "</bookViews>")
        match = re.search(r"<sheet [^>]*/>", text)
        assert match is not None
        one = re.sub(r'name="[^"]*"', 'name="s{i}"', re.sub(r'sheetId="\d+"', 'sheetId="{i}"', match[0]))
        if kind == "tag":
            one = one.replace("<sheet ", "<x ", 1)
        return text.replace(match[0], "".join(one.format(i=i + 1) for i in range(count)))

    with zipfile.ZipFile(path) as src:
        items = [(info, src.read(info.filename)) for info in src.infolist()]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as out:
        for info, data in items:
            name = info.filename
            if name == "xl/workbook.xml" and kind != "strings-noslash":
                data = many(data.decode()).encode()
                if kind in {"noslash", "dslash"}:
                    name = "xl/wb.xml" if kind == "noslash" else "/xl/wb.xml"
            elif name == "xl/_rels/workbook.xml.rels" and kind in {"noslash", "dslash"}:
                name = "xl/_rels/wb.xml.rels" if kind == "noslash" else "/xl/_rels/wb.xml.rels"
            elif name == "[Content_Types].xml":
                text = data.decode()
                if kind == "default":
                    text = text.replace(f'<Override PartName="/xl/workbook.xml" ContentType="{_WORKBOOK_TYPE}"/>', "")
                    text = text.replace(
                        '<Default Extension="xml" ContentType="application/xml"/>',
                        f'<Default Extension="xml" ContentType="{_WORKBOOK_TYPE}"/>',
                    )
                elif kind == "noslash":
                    text = text.replace('PartName="/xl/workbook.xml"', 'PartName="Xxl/wb.xml"')
                elif kind == "dslash":
                    text = text.replace('PartName="/xl/workbook.xml"', 'PartName="//xl/wb.xml"')
                elif kind == "strings-noslash":
                    override = f'<Override PartName="Xxl/sharedStrings.xml" ContentType="{_STRINGS_TYPE}"/></Types>'
                    text = text.replace("</Types>", override)
                data = text.encode()
            out.writestr(name, data)
        if kind == "strings-noslash":
            with out.open("xl/sharedStrings.xml", "w", force_zip64=True) as member:
                member.write(
                    f'<?xml version="1.0"?><sst xmlns="{namespace}" count="{count}" uniqueCount="{count}">'.encode()
                )
                block = b"<si><t>a</t></si>" * 10_000
                for _ in range(count // 10_000):
                    member.write(block)
                member.write(b"<si><t>a</t></si>" * (count % 10_000))
                member.write(b"</sst>")
    return path


def make_junk_strings_xlsx(path: Path, elements: int) -> Path:
    """Write a workbook whose shared strings part holds ``elements`` empty elements that are not strings.

    openpyxl's reader walks all of them and keeps a tree of them; a counting pass that builds a tree does too.

    Args:
        path: Where to write it.
        elements: How many ``x`` elements.

    Returns:
        ``path``.
    """
    make_xlsx(path, rows=2)
    namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    override = (
        b'<Override PartName="/xl/sharedStrings.xml" '
        b'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/></Types>'
    )
    _rewrite_xlsx(path, {"[Content_Types].xml": lambda data: data.replace(b"</Types>", override)})
    with zipfile.ZipFile(path, "a", zipfile.ZIP_DEFLATED) as archive:
        with archive.open("xl/sharedStrings.xml", "w", force_zip64=True) as member:
            member.write(f'<?xml version="1.0"?><sst xmlns="{namespace}"><si><t>a</t></si>'.encode())
            block = b"<x/>" * 100_000
            for _ in range(elements // 100_000):
                member.write(block)
            member.write(b"</sst>")
    return path


def _noise_beside(path: Path, suffix: str, size: tuple[int, int], fmt: str) -> Path:
    """Write a picture of noise next to ``path``, for a builder to put into it and remove afterwards.

    Args:
        path: The file being built.
        suffix: What the picture's name adds to the file's.
        size: Width and height.
        fmt: The Pillow format name.

    Returns:
        The picture's path.
    """
    return make_images.make_noise(path.with_name(f"{path.name}.{suffix}"), size, fmt)


def make_deck(
    path: Path,
    slides: int = 30,
    *,
    chart_on: int | None = 1,
    picture_only: Sequence[int] = (),
    hidden: Sequence[int] = (),
    photo: tuple[int, int] | None = None,
) -> Path:
    """Write a deck of ``slides`` slides, each titled ``Invented slide N`` with one bullet, for LibreOffice to draw.

    Args:
        path: Where to write it.
        slides: How many slides.
        chart_on: The slide that also holds a clustered column chart of invented figures, or ``None``.
        picture_only: Slides that hold one picture of noise (a PNG) and no text frame instead.
        hidden: Slides hidden in the slide show (``show="0"`` on the slide).
        photo: The size of a noise JPEG put on every slide, or ``None``.

    Returns:
        ``path``.
    """
    deck: Any = Presentation()
    noise = _noise_beside(path, "noise.png", (320, 240), "PNG") if picture_only else None
    picture = _noise_beside(path, "photo.jpg", photo, "JPEG") if photo else None
    try:
        for number in range(1, slides + 1):
            if noise is not None and number in picture_only:
                slide = deck.slides.add_slide(deck.slide_layouts[6])
                slide.shapes.add_picture(str(noise), Inches(2), Inches(1.5), Inches(6))
            else:
                slide = deck.slides.add_slide(deck.slide_layouts[1])
                slide.shapes.title.text = f"Invented slide {number}"
                slide.placeholders[1].text = f"Invented point {number}"
            if number == chart_on:
                data = CategoryChartData()
                data.categories = ["North pier", "South pier", "East pier"]
                data.add_series("Invented tonnes", (12, 7, 9))
                slide.shapes.add_chart(
                    XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(3.5), Inches(8), Inches(3.5), data
                )
            if picture is not None:
                slide.shapes.add_picture(str(picture), Inches(5.5), Inches(4.5), Inches(4))
            if number in hidden:
                slide._element.set("show", "0")
        deck.save(str(path))
    finally:
        for made in (noise, picture):
            if made is not None:
                made.unlink()
    return path


def make_long_docx(path: Path, pages: int = 24, *, picture_on: int | None = None) -> Path:
    """Write a Word document of ``pages`` pages: a heading ``Invented section N`` and two paragraphs on each.

    A page break ends every page but the last, which would otherwise leave an empty page after it.

    Args:
        path: Where to write it.
        pages: How many pages.
        picture_on: The page that holds a picture of noise (a PNG) alone instead, or ``None``.

    Returns:
        ``path``.
    """
    doc = Document()
    noise = _noise_beside(path, "noise.png", (320, 240), "PNG") if picture_on else None
    try:
        for number in range(1, pages + 1):
            if noise is not None and number == picture_on:
                doc.add_picture(str(noise), width=docx_shared.Inches(4))
            else:
                doc.add_heading(f"Invented section {number}", level=1)
                doc.add_paragraph(f"The invented survey of section {number} counts the synthetic cranes of a harbour.")
                doc.add_paragraph("A second invented paragraph closes the section.")
            if number < pages:
                doc.add_page_break()
        doc.save(str(path))
    finally:
        if noise is not None:
            noise.unlink()
    return path


_ODF_OFFICE = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
_ODF_MANIFEST = "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"
_OOO_LIBRARY = "http://openoffice.org/2000/library"
_ODT_BASIC = ("Basic/script-lc.xml", "Basic/Standard/script-lb.xml", "Basic/Standard/Module1.xml")
_QUOT = {'"': "&quot;"}


def make_macro_odt(path: Path, marker: str) -> Path:
    """Write an OpenDocument text whose Basic macro, run when the document loads, writes ``$HOME/<marker>``.

    The macro is ``Standard.Module1.Main`` in the document's own Basic library (``Basic/``, every part listed in the
    manifest), bound to the document's ``dom:load`` event in ``content.xml``'s ``office:event-listeners``, as an
    office suite saves a document macro on "open document".

    Args:
        path: Where to write it.
        marker: The name of the file the macro would write in ``HOME``.

    Returns:
        ``path``.
    """
    target = marker.replace('"', '""')
    code = (
        "Sub Main\n"
        "    Dim n As Integer\n"
        "    n = FreeFile()\n"
        f'    Open Environ("HOME") & "/" & "{target}" For Output As #n\n'
        '    Print #n, "an invented line"\n'
        "    Close #n\n"
        "End Sub\n"
    )
    module = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<script:module xmlns:script="http://openoffice.org/2000/script" script:name="Module1" '
        f'script:language="StarBasic" script:moduleType="normal">{escape(code, _QUOT)}</script:module>\n'
    )
    library = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<library:library xmlns:library="{_OOO_LIBRARY}" library:name="Standard" library:readonly="false" '
        'library:passwordprotected="false"><library:element library:name="Module1"/></library:library>\n'
    )
    libraries = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<library:libraries xmlns:library="{_OOO_LIBRARY}" xmlns:xlink="http://www.w3.org/1999/xlink">'
        '<library:library library:name="Standard" library:link="false"/></library:libraries>\n'
    )
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<office:document-content xmlns:office="{_ODF_OFFICE}" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
        'xmlns:script="urn:oasis:names:tc:opendocument:xmlns:script:1.0" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:dom="http://www.w3.org/2001/xml-events" '
        'xmlns:ooo="http://openoffice.org/2004/office" office:version="1.3">'
        "<office:scripts><office:event-listeners>"
        '<script:event-listener script:language="ooo:script" script:event-name="dom:load" '
        'xlink:href="vnd.sun.star.script:Standard.Module1.Main?language=Basic&amp;location=document" '
        'xlink:type="simple"/>'
        "</office:event-listeners></office:scripts>"
        "<office:body><office:text><text:p>An invented paragraph of a document with a macro.</text:p>"
        "</office:text></office:body></office:document-content>\n"
    )
    entries = "".join(
        f'<manifest:file-entry manifest:full-path="{name}" manifest:media-type="text/xml"/>'
        for name in ("content.xml", *_ODT_BASIC)
    )
    manifest = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<manifest:manifest xmlns:manifest="{_ODF_MANIFEST}" manifest:version="1.3">'
        '<manifest:file-entry manifest:full-path="/" manifest:version="1.3" '
        'manifest:media-type="application/vnd.oasis.opendocument.text"/>'
        f"{entries}</manifest:manifest>\n"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/vnd.oasis.opendocument.text", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/manifest.xml", manifest)
        archive.writestr("content.xml", content)
        for name, part in zip(_ODT_BASIC, (libraries, library, module), strict=True):
            archive.writestr(name, part)
    return path
