"""Build small synthetic report templates at test time.

Every builder writes one file and returns its path. All text is invented. Word templates are made with
python-docx, their content controls and legacy form fields written as WordprocessingML with lxml; workbooks with
openpyxl.
"""

import zipfile
from pathlib import Path

from docx import Document
from docx.document import Document as DocumentObject
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml import parse_xml
from docx.text.paragraph import Paragraph
from openpyxl import Workbook

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
_DECLS = f'xmlns:w="{W}" xmlns:w14="{W14}"'

#: The style a filled control's text must not keep.
PLACEHOLDER_STYLE = "PlaceholderText"

#: The style, header text and protection a filled template keeps.
BODY_STYLE = "Report Body"
HEADER_TEXT = "Invented Harbour Office"


def _xml(text: str) -> object:
    """Parse a piece of WordprocessingML with the namespaces it uses."""
    return parse_xml(text.replace("<w:sdt>", f"<w:sdt {_DECLS}>", 1).replace("<w:r>", f"<w:r {_DECLS}>", 1))


def _placeholder_run(text: str) -> str:
    """A run that shows a control's placeholder text, in the placeholder style."""
    return f'<w:r><w:rPr><w:rStyle w:val="{PLACEHOLDER_STYLE}"/></w:rPr><w:t>{text}</w:t></w:r>'


def text_control(tag: str, *, alias: str | None = None, placeholder: str = "Click here to enter text.") -> str:
    """An inline plain-text content control showing its placeholder."""
    name = f'<w:tag w:val="{tag}"/>' if tag else ""
    title = f'<w:alias w:val="{alias}"/>' if alias else ""
    return (
        f'<w:sdt><w:sdtPr><w:rPr><w:b/></w:rPr>{title}{name}<w:id w:val="1"/><w:showingPlcHdr/><w:text/></w:sdtPr>'
        f"<w:sdtContent>{_placeholder_run(placeholder)}</w:sdtContent></w:sdt>"
    )


def date_control(tag: str, pattern: str = "dd.MM.yyyy", *, bound: bool = False) -> str:
    """An inline date content control in ``pattern``, optionally bound to a custom XML part."""
    binding = (
        '<w:dataBinding w:xpath="/report[1]/date[1]" w:storeItemID="{00000000-0000-0000-0000-000000000001}"/>'
        if bound
        else ""
    )
    return (
        f'<w:sdt><w:sdtPr><w:tag w:val="{tag}"/><w:id w:val="2"/><w:showingPlcHdr/>{binding}'
        f'<w:date><w:dateFormat w:val="{pattern}"/><w:lid w:val="en-GB"/>'
        '<w:storeMappedDataAs w:val="dateTime"/><w:calendar w:val="gregorian"/></w:date></w:sdtPr>'
        f"<w:sdtContent>{_placeholder_run('Click here to enter a date.')}</w:sdtContent></w:sdt>"
    )


def dropdown_control(tag: str, options: list[str]) -> str:
    """An inline drop-down content control with ``options`` as its entries."""
    items = "".join(f'<w:listItem w:displayText="{option}" w:value="{option}-value"/>' for option in options)
    return (
        f'<w:sdt><w:sdtPr><w:tag w:val="{tag}"/><w:id w:val="3"/><w:showingPlcHdr/>'
        f"<w:dropDownList>{items}</w:dropDownList></w:sdtPr>"
        f"<w:sdtContent>{_placeholder_run('Choose an item.')}</w:sdtContent></w:sdt>"
    )


def checkbox_control(tag: str) -> str:
    """An inline check box content control, unchecked."""
    return (
        f'<w:sdt><w:sdtPr><w:tag w:val="{tag}"/><w:id w:val="4"/><w14:checkbox><w14:checked w14:val="0"/>'
        '<w14:checkedState w14:val="2612" w14:font="MS Gothic"/>'
        '<w14:uncheckedState w14:val="2610" w14:font="MS Gothic"/></w14:checkbox></w:sdtPr>'
        '<w:sdtContent><w:r><w:rPr><w:rFonts w:ascii="MS Gothic" w:hAnsi="MS Gothic"/></w:rPr>'
        "<w:t>☐</w:t></w:r></w:sdtContent></w:sdt>"
    )


def rich_block_control(tag: str) -> str:
    """A block-level rich-text content control holding one paragraph of placeholder text."""
    return (
        f'<w:sdt><w:sdtPr><w:tag w:val="{tag}"/><w:id w:val="5"/><w:showingPlcHdr/></w:sdtPr>'
        f'<w:sdtContent><w:p><w:pPr><w:pStyle w:val="ReportBody"/></w:pPr>{_placeholder_run("Summary here.")}</w:p>'
        "</w:sdtContent></w:sdt>"
    )


def _inline(paragraph: Paragraph, control: str) -> None:
    """Append an inline content control to a paragraph."""
    paragraph._p.append(_xml(control))


def _protect(doc: DocumentObject) -> None:
    """Restrict editing to filling in forms, as a form template does."""
    settings = doc.settings.element
    protection = parse_xml(f'<w:documentProtection {_DECLS} w:edit="forms" w:enforcement="1"/>')
    zoom = settings.find(f"{{{W}}}zoom")
    settings.insert(0 if zoom is None else settings.index(zoom) + 1, protection)


def make_controls_docx(path: Path, *, bound: bool = False, date_pattern: str = "dd.MM.yyyy") -> Path:
    """Write a Word form with content controls, a style of its own, a header and protection for filling in forms.

    The controls: ``customer`` (plain text, also in the header), ``report_date`` (date), ``status`` (drop-down of
    ``open`` and ``closed``, in a table), ``approved`` (check box) and ``summary`` (block-level rich text).

    Args:
        path: Where to write it.
        bound: Bind ``report_date`` to a custom XML part.
        date_pattern: The date control's display format.

    Returns:
        ``path``.
    """
    doc = Document()
    doc.styles.add_style(BODY_STYLE, WD_STYLE_TYPE.PARAGRAPH)
    header = doc.sections[0].header.paragraphs[0]
    header.add_run(HEADER_TEXT + ", customer: ")
    _inline(header, text_control("customer"))
    doc.add_paragraph("Quarterly report", style=BODY_STYLE)
    _inline(doc.add_paragraph("Customer: "), text_control("customer", alias="Customer"))
    _inline(doc.add_paragraph("Report date: "), date_control("report_date", date_pattern, bound=bound))
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Item"
    table.cell(0, 1).text = "Status"
    table.cell(1, 0).text = "Dredging"
    _inline(table.cell(1, 1).paragraphs[0], dropdown_control("status", ["open", "closed"]))
    _inline(doc.add_paragraph("Approved: "), checkbox_control("approved"))
    body = doc.element.body
    body.insert(body.index(body[-1]), _xml(rich_block_control("summary")))
    _protect(doc)
    doc.save(str(path))
    return path


def make_controls_body(path: Path, controls: list[str], *, blocks: tuple[str, ...] = ()) -> Path:
    """Write a Word document with one paragraph per control, each control given as WordprocessingML.

    Args:
        path: Where to write it.
        controls: Inline controls, or block-level ones (those start with ``<w:sdt>`` and hold a ``<w:p>``).
        blocks: Further controls to put where paragraphs go, after the others, whatever they hold.

    Returns:
        ``path``.
    """
    doc = Document()
    body = doc.element.body
    for control in controls:
        if "<w:sdtContent><w:p>" in control:
            body.insert(body.index(body[-1]), _xml(control))
        else:
            _inline(doc.add_paragraph("Field: "), control)
    for control in blocks:
        body.insert(body.index(body[-1]), _xml(control))
    doc.save(str(path))
    return path


def _form_field(name: str, data: str, instruction: str, *, result: str | None = "     ") -> str:
    """The runs of one legacy form field: begin with its form data, the instruction, a result, end."""
    runs = [
        f'<w:r><w:fldChar w:fldCharType="begin"><w:ffData><w:name w:val="{name}"/><w:enabled/>'
        f'<w:calcOnExit w:val="0"/>{data}</w:ffData></w:fldChar></w:r>',
        f'<w:r><w:instrText xml:space="preserve"> {instruction} </w:instrText></w:r>',
    ]
    if result is not None:
        runs += [
            '<w:r><w:fldChar w:fldCharType="separate"/></w:r>',
            f'<w:r><w:t xml:space="preserve">{result}</w:t></w:r>',
        ]
    runs.append('<w:r><w:fldChar w:fldCharType="end"/></w:r>')
    return "".join(runs)


def make_form_fields_docx(path: Path) -> Path:
    """Write a Word form with legacy form fields, one a paragraph.

    The fields: ``name`` (text, at most 40 characters), ``agreed`` (check box), ``region`` (drop-down of ``north``
    and ``south``) and ``signed_on`` (a date, ``dd.MM.yyyy``).

    Args:
        path: Where to write it.

    Returns:
        ``path``.
    """
    doc = Document()
    fields = [
        ("Name: ", _form_field("name", '<w:textInput><w:maxLength w:val="40"/></w:textInput>', "FORMTEXT")),
        (
            "Agreed: ",
            _form_field(
                "agreed", '<w:checkBox><w:sizeAuto/><w:default w:val="0"/></w:checkBox>', "FORMCHECKBOX", result=None
            ),
        ),
        (
            "Region: ",
            _form_field(
                "region",
                '<w:ddList><w:listEntry w:val="north"/><w:listEntry w:val="south"/></w:ddList>',
                "FORMDROPDOWN",
                result=None,
            ),
        ),
        (
            "Signed on: ",
            _form_field(
                "signed_on",
                '<w:textInput><w:type w:val="date"/><w:format w:val="dd.MM.yyyy"/></w:textInput>',
                "FORMTEXT",
            ),
        ),
    ]
    for label, runs in fields:
        paragraph = doc.add_paragraph(label)
        holder = parse_xml(f"<w:p {_DECLS}>{runs}</w:p>")
        for run in list(holder):
            paragraph._p.append(run)
    doc.save(str(path))
    return path


def as_dotx(docx: Path, target: Path) -> Path:
    """Copy a Word document as a ``.dotx`` template: the same parts under the template content type.

    Args:
        docx: A Word document.
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


def make_jinja_docx(path: Path, *, extra: str | None = None) -> Path:
    """Write a Word document with ``{{ }}`` placeholders.

    ``customer`` (also in the header), a table whose rows loop over ``rows`` (``item`` and ``total`` of each), and a
    sentence shown if ``approved``.

    Args:
        path: Where to write it.
        extra: One more paragraph of template text, such as code that does not compile.

    Returns:
        ``path``.
    """
    doc = Document()
    doc.sections[0].header.paragraphs[0].text = "Report for {{ customer }}"
    doc.add_paragraph("Customer: {{ customer }}")
    table = doc.add_table(rows=4, cols=2)
    table.cell(0, 0).text = "Item"
    table.cell(0, 1).text = "Total"
    table.cell(1, 0).text = "{%tr for row in rows %}"
    table.cell(2, 0).text = "{{ row.item }}"
    table.cell(2, 1).text = "{{ row.total }}"
    table.cell(3, 0).text = "{%tr endfor %}"
    doc.add_paragraph("{% if approved %}Approved by the invented board.{% endif %}")
    if extra is not None:
        doc.add_paragraph(extra)
    doc.save(str(path))
    return path


def make_placeholder_xlsx(path: Path, *, blocker: str | None = None) -> Path:
    """Write a workbook with ``{{ }}`` placeholders on the sheet ``Summary``.

    ``{{ customer }}`` (a cell of its own and inside a note), ``{{ total }}`` and a ``{{ table:rows }}`` anchor below
    a header row.

    Args:
        path: Where to write it.
        blocker: A cell below the anchor to fill with text, which a table must not overwrite.

    Returns:
        ``path``.
    """
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "Summary"
    sheet["A1"] = "Customer"
    sheet["B1"] = "{{ customer }}"
    sheet["A2"] = "Total"
    sheet["B2"] = "{{ total }}"
    sheet["A3"] = "Checked for {{ customer }} by the invented office"
    sheet["A5"] = "Item"
    sheet["B5"] = "Amount"
    sheet["A6"] = "{{ table:rows }}"
    if blocker is not None:
        sheet[blocker] = "kept"
    book.save(str(path))
    return path
