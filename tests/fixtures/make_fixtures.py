"""Build small synthetic documents at test time.

Every builder writes one file and returns its path. All text is invented. The libraries that
write the files are the ones the executor image carries (python-docx, openpyxl, python-pptx,
odfpy) plus reportlab, a dev dependency, for PDFs.
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

from docx import Document
from docx.table import _Cell
from odf.opendocument import OpenDocumentSpreadsheet, OpenDocumentText
from odf.table import Table, TableCell, TableRow
from odf.text import H, P
from openpyxl import Workbook
from pptx import Presentation
from pptx.util import Inches
from pypdf import PdfWriter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

OLE_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")
_MIB = 1024 * 1024


def _png(size: int = 8) -> bytes:
    """A small grey PNG, written by hand so no imaging library is needed."""
    rows = b"".join(b"\x00" + b"\x80" * size for _ in range(size))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", size, size, 8, 0, 0, 0, 0)
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


def make_sparse_xlsx(path: Path, *, row: int = 2_000_000_000, column: str = "B") -> Path:
    """Write a workbook whose second row of data is numbered ``row`` and whose last cell is in ``column``.

    Args:
        path: Where to write it.
        row: The number of the second row; no spreadsheet has more than 1 048 576.
        column: The column of the second row's cells; none goes past ``XFD``.

    Returns:
        ``path``.
    """
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
            if info.filename == "xl/worksheets/sheet1.xml":
                xml = data.decode()
                xml = xml.replace('<row r="2"', f'<row r="{row}"').replace('r="A2"', f'r="A{row}"')
                xml = xml.replace('r="B2"', f'r="{column}{row}"')
                data = xml.encode()
            out.writestr(info, data)
    return path


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
