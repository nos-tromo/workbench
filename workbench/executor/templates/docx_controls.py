"""Word content controls and legacy form fields: found and filled in the body, the headers and the footers.

The document is open in python-docx, whose parts are parsed without entities or network; the controls are read and
changed with lxml on those parts. A content control (``w:sdt``) is named by its tag, else its title; a legacy form
field by the name in its form data (``w:ffData/w:name``), which is its bookmark's. Only the innermost controls are
fields: one that holds other controls or form fields groups them and is left alone.

A place is written as the document reader writes it (:mod:`workbench.executor.readers.docx`): ``body, paragraph 3``
over the paragraphs that hold text, ``body, table 2, row 4`` for anything in a table's row, and ``header 1`` or
``footer 2`` for a header or footer, numbered in the order of their parts.
"""

import copy
import itertools
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final

from docx.document import Document
from docx.opc.constants import CONTENT_TYPE as CT
from docx.oxml.ns import qn
from docx.oxml.parser import OxmlElement

from workbench.executor.templates import (
    Field,
    Refusal,
    Type,
    as_bool,
    as_date,
    as_option,
    as_text,
    format_date,
    safe_text,
    xml_safe,
)

_W: Final = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_W14: Final = "{http://schemas.microsoft.com/office/word/2010/wordml}"
_SDT: Final = f"{_W}sdt"
_SDT_PR: Final = f"{_W}sdtPr"
_SDT_CONTENT: Final = f"{_W}sdtContent"
_P: Final = f"{_W}p"
_R: Final = f"{_W}r"
_T: Final = f"{_W}t"
_TAB: Final = f"{_W}tab"
_BR: Final = f"{_W}br"
_CR: Final = f"{_W}cr"
_TBL: Final = f"{_W}tbl"
_TR: Final = f"{_W}tr"
_TC: Final = f"{_W}tc"
_TC_PR: Final = f"{_W}tcPr"
_PPR: Final = f"{_W}pPr"
_RPR: Final = f"{_W}rPr"
_FFDATA: Final = f"{_W}ffData"
_FLDCHAR: Final = f"{_W}fldChar"

#: Elements whose children are body content too, as the reader walks them.
_WRAPPERS: Final = frozenset({_SDT, _SDT_CONTENT, f"{_W}customXml", f"{_W}smartTag"})

#: What a control holds when it sits inside a paragraph.
_INLINE: Final = frozenset({_R, f"{_W}hyperlink", f"{_W}fldSimple", f"{_W}ins", f"{_W}del", f"{_W}smartTag"})

#: What a control holds when it sits where paragraphs go.
_BLOCK_PARENTS: Final = frozenset(
    {f"{_W}body", _TC, f"{_W}hdr", f"{_W}ftr", f"{_W}txbxContent", f"{_W}footnote", f"{_W}endnote", f"{_W}comment"}
)

#: The kinds of control version 1 does not fill: pictures, building blocks, groups, equations, citations.
_NOT_FILLED: Final = frozenset(
    {
        f"{_W}picture",
        f"{_W}docPartObj",
        f"{_W}docPartList",
        f"{_W}group",
        f"{_W}equation",
        f"{_W}citation",
        f"{_W}bibliography",
        "{http://schemas.microsoft.com/office/word/2012/wordml}repeatingSection",
        "{http://schemas.microsoft.com/office/word/2012/wordml}repeatingSectionItem",
    }
)

_PLACEHOLDER_STYLE: Final = "placeholdertext"
_PATTERN_SHOWN: Final = 60
_CHECKED: Final = "2612"
_UNCHECKED: Final = "2610"
_NUMBER: Final = re.compile(r"(\d+)")


@dataclass
class Story:
    """The body, a header or a footer, with the places of what is in it.

    Attributes:
        name: ``body``, ``header N`` or ``footer N``.
        root: Its root element.
        partname: A header's or footer's part name, such as ``/word/header1.xml``; ``None`` for the body.
        blocks: For the body, each paragraph and table row and the place it is.
    """

    name: str
    root: Any
    partname: str | None = None
    blocks: dict[Any, str] = field(default_factory=dict)

    def place(self, element: Any) -> str:
        """Say where an element is.

        Args:
            element: An element of the story.

        Returns:
            The place of the paragraph or table row it is in, or of the first one it holds; the story alone outside
            the body.
        """
        if not self.blocks:
            return self.name
        for node in itertools.chain([element], element.iterancestors()):
            label = self.blocks.get(node)
            if label is not None:
                return f"{self.name}, {label}"
        for node in element.iter(_P, _TR):
            label = self.blocks.get(node)
            if label is not None:
                return f"{self.name}, {label}"
        return self.name


def paragraph_text(paragraph: Any) -> str:
    """Join the text of one paragraph as the reader does.

    Args:
        paragraph: A ``w:p`` element.

    Returns:
        Its text, with tabs and line breaks, stripped.
    """
    pieces: list[str] = []
    for element in paragraph.iter(_T, _TAB, _BR, _CR):
        if element.tag == _T:
            pieces.append(element.text or "")
        else:
            pieces.append("\t" if element.tag == _TAB else "\n")
    return "".join(pieces).strip()


def _rows(table: Any) -> Iterator[Any]:
    """Walk a table's rows, those inside a control around rows too.

    Args:
        table: A ``w:tbl`` element.

    Yields:
        Each ``w:tr``.
    """
    for child in table:
        if child.tag == _TR:
            yield child
        elif child.tag in _WRAPPERS:
            yield from _rows(child)


def _number(story: Story, parent: Any, counters: list[int]) -> None:
    """Give every paragraph and table row of the body its place, through wrappers such as content controls.

    Args:
        story: The body.
        parent: The body, or a wrapper inside it.
        counters: The paragraphs with text and the tables numbered so far.
    """
    for child in parent:
        if child.tag == _P:
            if paragraph_text(child):
                counters[0] += 1
                story.blocks[child] = f"paragraph {counters[0]}"
            else:
                story.blocks[child] = f"after paragraph {counters[0]}" if counters[0] else "before paragraph 1"
        elif child.tag == _TBL:
            counters[1] += 1
            for number, row in enumerate(_rows(child), 1):
                story.blocks[row] = f"table {counters[1]}, row {number}"
        elif child.tag in _WRAPPERS:
            _number(story, child, counters)


def _natural(partname: str) -> tuple[Any, ...]:
    """Sort part names as people count: ``header2`` before ``header10``.

    Args:
        partname: A part's name.

    Returns:
        A sort key.
    """
    return tuple(int(piece) if piece.isdigit() else piece for piece in _NUMBER.split(partname))


def stories(document: Document) -> list[Story]:
    """List the body, then the headers, then the footers.

    Args:
        document: The document.

    Returns:
        Each with its root element; the body's places numbered.
    """
    body = Story("body", document.element.body)
    _number(body, body.root, [0, 0])
    found = [body]
    package: Any = document.part.package
    parts = list(package.iter_parts())
    for content_type, noun in ((CT.WML_HEADER, "header"), (CT.WML_FOOTER, "footer")):
        chosen = sorted((p for p in parts if p.content_type == content_type), key=lambda p: _natural(str(p.partname)))
        found.extend(Story(f"{noun} {n}", p.element, str(p.partname)) for n, p in enumerate(chosen, 1))
    return found


def _val(element: Any | None, attribute: str = "w:val") -> str | None:
    """Read an attribute of an element that may be missing.

    Args:
        element: The element, or ``None``.
        attribute: The attribute, prefixed.

    Returns:
        Its value, or ``None``.
    """
    return None if element is None else element.get(qn(attribute))


@dataclass
class _Spot:
    """One place of a field in a story: a content control or a form field, and what it takes.

    Attributes:
        field: The field as listed.
        element: The ``w:sdt``, or the run that begins the form field.
        options: A drop-down's entries as shown, with the value each stands for.
    """

    field: Field
    element: Any
    options: list[tuple[str, str]]

    def check(self, value: object) -> object:
        """Take the value in the field's type.

        Args:
            value: The value.

        Returns:
            The value as the field will write it: text, a date, a bool or the chosen entry.

        Raises:
            Refusal: The field does not take it.
        """
        name, kind = self.field.name, self.field.type
        if kind == "checkbox":
            return as_bool(name, value)
        if kind == "date":
            return as_date(name, value)
        if kind == "dropdown":
            return as_option(name, value, [shown for shown, _ in self.options])
        text = as_text(name, value)
        if self.field.kind == "form_field":
            limit = _val(self.element.find(f".//{_FFDATA}/{_W}textInput/{_W}maxLength"))
            if limit is not None and limit.isdigit() and int(limit) > 0 and len(text) > int(limit):
                raise Refusal("invalid_value", f"{name} takes at most {limit} characters")
        return text


def _control_type(properties: Any) -> tuple[Type | None, list[tuple[str, str]]]:
    """Tell what a content control takes from its properties.

    Args:
        properties: Its ``w:sdtPr``.

    Returns:
        Its type (``None`` for a kind version 1 does not fill) and, for a drop-down, its entries as shown with
        their values.
    """
    tags = {child.tag for child in properties if isinstance(child.tag, str)}
    if tags & _NOT_FILLED:
        return None, []
    if f"{_W14}checkbox" in tags:
        return "checkbox", []
    if f"{_W}date" in tags:
        return "date", []
    for name in ("dropDownList", "comboBox"):
        listed = properties.find(f"{_W}{name}")
        if listed is not None:
            items = [
                (item.get(qn("w:displayText")) or item.get(qn("w:value")) or "", item.get(qn("w:value")) or "")
                for item in listed.iter(f"{_W}listItem")
            ]
            return "dropdown", items
    if f"{_W}text" in tags:
        return "text", []
    return "rich_text", []


def _form_type(data: Any) -> tuple[Type | None, list[tuple[str, str]]]:
    """Tell what a legacy form field takes from its form data.

    Args:
        data: Its ``w:ffData``.

    Returns:
        Its type (``None`` for a calculated or current date or time field, which fills itself) and, for a
        drop-down, its entries.
    """
    if data.find(f"{_W}checkBox") is not None:
        return "checkbox", []
    listed = data.find(f"{_W}ddList")
    if listed is not None:
        entries = [entry.get(qn("w:val")) or "" for entry in listed.iter(f"{_W}listEntry")]
        return "dropdown", [(entry, entry) for entry in entries]
    text = data.find(f"{_W}textInput")
    if text is None:
        return None, []
    kind = _val(text.find(f"{_W}type")) or "regular"
    if kind == "date":
        return "date", []
    if kind in {"regular", "number"}:
        return "text", []
    return None, []


@dataclass
class Found:
    """What a document holds.

    Attributes:
        spots: Every place of every field, in document order, body first.
        unnamed_controls: Content controls with neither a tag nor a title, which cannot be filled.
        unnamed_fields: Form fields without a name, which cannot be filled.
        other: Content controls of a kind version 1 does not fill, and form fields that fill themselves.
    """

    spots: list[_Spot]
    unnamed_controls: int
    unnamed_fields: int
    other: int


def find(document: Document) -> Found:
    """Find the content controls and legacy form fields of the body, the headers and the footers.

    Args:
        document: The document.

    Returns:
        Their places, and how many could not be listed.
    """
    spots: list[_Spot] = []
    unnamed_controls = unnamed_fields = other = 0
    for story in stories(document):
        for element in story.root.iter(_SDT, _FFDATA):
            if element.tag == _FFDATA:
                kind, options = _form_type(element)
                name = _val(element.find(f"{_W}name"))
                holder = element.getparent()
                run = holder.getparent() if holder is not None else None
                if run is None or run.tag != _R:
                    continue
                if kind is None:
                    other += 1
                elif not name:
                    unnamed_fields += 1
                else:
                    field = Field(name, "form_field", kind, [s for s, _ in options] or None, story.place(run))
                    spots.append(_Spot(field, run, options))
                continue
            properties = element.find(_SDT_PR)
            if (
                properties is None
                or element.find(f".//{_SDT}") is not None
                or element.find(f".//{_FFDATA}") is not None
            ):
                continue
            kind, options = _control_type(properties)
            name = _val(properties.find(f"{_W}tag")) or _val(properties.find(f"{_W}alias"))
            if kind is None:
                other += 1
            elif not name:
                unnamed_controls += 1
            else:
                field = Field(name, "content_control", kind, [s for s, _ in options] or None, story.place(element))
                spots.append(_Spot(field, element, options))
    return Found(spots, unnamed_controls, unnamed_fields, other)


def _without_placeholder_style(properties: Any | None) -> Any | None:
    """Copy run properties for new text, without the placeholder style.

    Args:
        properties: A ``w:rPr``, or ``None``.

    Returns:
        The copy, or ``None``.
    """
    if properties is None:
        return None
    kept = copy.deepcopy(properties)
    for style in kept.findall(f"{_W}rStyle"):
        if (style.get(qn("w:val")) or "").lower() == _PLACEHOLDER_STYLE:
            kept.remove(style)
    return kept


def _runs(lines: list[str], properties: Any | None) -> list[Any]:
    """Make the runs that show some lines, a line break before each line after the first.

    Args:
        lines: The lines.
        properties: The runs' properties, or ``None``.

    Returns:
        One run per line.
    """
    runs: list[Any] = []
    for index, line in enumerate(lines):
        run = OxmlElement("w:r")
        if properties is not None:
            run.append(copy.deepcopy(properties))
        if index:
            run.append(OxmlElement("w:br"))
        text = OxmlElement("w:t")
        text.text = line
        text.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        run.append(text)
        runs.append(run)
    return runs


def _level(control: Any, content: Any) -> str:
    """Tell where a content control sits: among paragraphs, in a table row as cells, or inside a paragraph.

    Args:
        control: The ``w:sdt``.
        content: Its ``w:sdtContent``.

    Returns:
        ``block``, ``cell`` or ``inline``: by what it holds, or, when that tells nothing (it is empty, or holds only
        bookmarks), by where it is.
    """
    tags = {child.tag for child in content if isinstance(child.tag, str)}
    if tags & {_P, _TBL}:
        return "block"
    if _TC in tags:
        return "cell"
    if tags & _INLINE:
        return "inline"
    parent = control.getparent()
    while parent is not None and parent.tag in _WRAPPERS:
        parent = parent.getparent()
    if parent is not None and parent.tag in _BLOCK_PARENTS:
        return "block"
    return "cell" if parent is not None and parent.tag == _TR else "inline"


def _show(control: Any, text: str, *, rich: bool, font: str | None = None) -> None:
    """Replace what a content control shows with a text, in the control's run formatting.

    A newline starts a new paragraph in rich text that sits among paragraphs, and is a line break otherwise.

    Args:
        control: The ``w:sdt``.
        text: The text.
        rich: Whether the control is rich text.
        font: A font the text must be shown in, as a check box's glyph is.
    """
    properties = control.find(_SDT_PR)
    content = control.find(_SDT_CONTENT)
    if content is None:
        content = OxmlElement("w:sdtContent")
        control.append(content)
    first = content.find(f".//{_RPR}")
    own = properties.find(_RPR) if properties is not None else None
    run_properties = _without_placeholder_style(own if own is not None else first)
    if font is not None:
        run_properties = run_properties if run_properties is not None else OxmlElement("w:rPr")
        for old in run_properties.findall(f"{_W}rFonts"):
            run_properties.remove(old)
        fonts = OxmlElement("w:rFonts", {qn("w:ascii"): font, qn("w:hAnsi"): font, qn("w:eastAsia"): font})
        style = run_properties.find(f"{_W}rStyle")
        run_properties.insert(0 if style is None else run_properties.index(style) + 1, fonts)  # the schema's order
    lines = text.split("\n")
    level = _level(control, content)
    if level == "inline":
        for child in list(content):
            content.remove(child)
        for run in _runs(lines, run_properties):
            content.append(run)
        return
    holder = content
    if level == "cell":
        holder = content.find(_TC)
        if holder is None:
            holder = OxmlElement("w:tc")
            content.append(holder)
    paragraph_properties = holder.find(f".//{_PPR}")
    kept = copy.deepcopy(paragraph_properties) if paragraph_properties is not None else None
    for child in list(holder):
        if child.tag != _TC_PR:
            holder.remove(child)
    for group in [[line] for line in lines] if rich else [lines]:
        paragraph = OxmlElement("w:p")
        if kept is not None:
            paragraph.append(copy.deepcopy(kept))
        for run in _runs(group, run_properties):
            paragraph.append(run)
        holder.append(paragraph)


def _fill_control(spot: _Spot, value: object, warnings: list[str]) -> None:
    """Fill one content control with a value its type takes.

    Args:
        spot: The control.
        value: The value, already checked.
        warnings: Where a warning goes.
    """
    control, name, kind = spot.element, spot.field.name, spot.field.type
    properties = control.find(_SDT_PR)
    for marker in properties.findall(f"{_W}showingPlcHdr"):
        properties.remove(marker)
    for binding in properties.findall(f"{_W}dataBinding"):
        properties.remove(binding)
        warning = (
            f"{name}: the control was bound to data in the document; the binding is removed, so that Word shows the "
            "filled value"
        )
        if warning not in warnings:
            warnings.append(warning)
    if kind == "checkbox":
        box = properties.find(f"{_W14}checkbox")
        checked = box.find(f"{_W14}checked")
        if checked is None:
            checked = OxmlElement("w14:checked")
            box.insert(0, checked)
        checked.set(f"{_W14}val", "1" if value else "0")
        state = box.find(f"{_W14}checkedState" if value else f"{_W14}uncheckedState")
        usual = chr(int(_CHECKED if value else _UNCHECKED, 16))
        code = state.get(f"{_W14}val") if state is not None else None
        font = state.get(f"{_W14}font") if state is not None else None
        try:
            glyph, unwritable = xml_safe(chr(int(code or "", 16)))
        except (ValueError, OverflowError):
            glyph, unwritable = usual, False
        if unwritable:
            glyph, font = usual, None  # a state XML cannot hold (a control character, a surrogate): the usual box
        _show(control, glyph, rich=False, font=font)
        return
    if kind == "date":
        assert isinstance(value, date)
        settings = properties.find(f"{_W}date")
        settings.set(qn("w:fullDate"), f"{value.isoformat()}T00:00:00Z")
        pattern = _val(settings.find(f"{_W}dateFormat"))
        text = format_date(value, pattern) if pattern else value.isoformat()
        if text is None:
            text = value.isoformat()
            warnings.append(
                f"{name}: the control's date format {_quoted(pattern)} is not one version 1 writes; the date is shown "
                f"as {text}"
            )
        _show(control, text, rich=False)
        return
    if kind == "dropdown":
        chosen = next(stored for shown, stored in spot.options if shown == value)
        for listed in (properties.find(f"{_W}dropDownList"), properties.find(f"{_W}comboBox")):
            if listed is not None:
                listed.set(qn("w:lastValue"), chosen)
        _show(control, str(value), rich=False)
        return
    _show(
        control,
        safe_text(name, str(value), warnings).replace("\r\n", "\n").replace("\r", "\n"),
        rich=kind == "rich_text",
    )


def _quoted(pattern: str | None) -> str:
    """Quote a date format for a warning, its start only if it is long.

    Args:
        pattern: The format.

    Returns:
        At most about 60 characters.
    """
    text = pattern or ""
    return repr(text if len(text) <= _PATTERN_SHOWN else text[: _PATTERN_SHOWN - 1] + "…")


def _field_runs(begin: Any) -> tuple[Any | None, Any] | None:
    """Find the separator and the end of the field a run begins, among the runs after it.

    Args:
        begin: The run with the field's ``begin``.

    Returns:
        The run with ``separate`` (``None`` if there is none) and the run with ``end``; ``None`` if the end is not
        in the same paragraph.
    """
    depth = 0
    separate = None
    for sibling in begin.itersiblings():
        for char in sibling.iter(_FLDCHAR):
            kind = char.get(qn("w:fldCharType"))
            if kind == "begin":
                depth += 1
            elif kind == "separate" and depth == 0:
                separate = sibling
            elif kind == "end":
                if depth == 0:
                    return separate, sibling
                depth -= 1
    return None


def _fill_form(spot: _Spot, value: object, warnings: list[str]) -> bool:
    """Fill one legacy form field with a value its type takes.

    Args:
        spot: The form field.
        value: The value, already checked.
        warnings: Where a warning goes.

    Returns:
        Whether it was filled; a field whose end is not in its paragraph is not, with a warning.
    """
    begin, name, kind = spot.element, spot.field.name, spot.field.type
    data = begin.find(f".//{_FFDATA}")
    if kind == "checkbox":
        box = data.find(f"{_W}checkBox")
        checked = box.find(f"{_W}checked")
        if checked is None:
            checked = OxmlElement("w:checked")
            box.append(checked)
        checked.set(qn("w:val"), "1" if value else "0")
        return True
    if kind == "dropdown":
        listed = data.find(f"{_W}ddList")
        result = listed.find(f"{_W}result")
        if result is None:
            result = OxmlElement("w:result")
            listed.insert(0, result)
        result.set(qn("w:val"), str([shown for shown, _ in spot.options].index(str(value))))
        return True
    if kind == "date":
        assert isinstance(value, date)
        pattern = _val(data.find(f"{_W}textInput/{_W}format"))
        text = format_date(value, pattern) if pattern else value.isoformat()
        if text is None:
            text = value.isoformat()
            warnings.append(
                f"{name}: the field's date format {_quoted(pattern)} is not one version 1 writes; the date is shown as "
                f"{text}"
            )
    else:
        text = safe_text(name, str(value), warnings).replace("\r\n", "\n").replace("\r", "\n")
    ends = _field_runs(begin)
    if ends is None:
        warnings.append(f"{name}: one place of this form field runs over more than one paragraph and was not filled")
        return False
    separate, end = ends
    if separate is None:
        separate = OxmlElement("w:r")
        separate.append(OxmlElement("w:fldChar", {qn("w:fldCharType"): "separate"}))
        end.addprevious(separate)
    old = list(itertools.takewhile(lambda run: run is not end, separate.itersiblings()))
    first = next((run.find(_RPR) for run in old if run.find(_RPR) is not None), begin.find(_RPR))
    for run in old:
        run.getparent().remove(run)
    for run in _runs(text.split("\n"), _without_placeholder_style(first)):
        end.addprevious(run)
    return True


def fill(document: Document, values: Mapping[str, object], warnings: list[str]) -> list[str]:
    """Fill every place of every field a value is given for; every value is checked before anything changes.

    Args:
        document: The document; it is changed.
        values: The values by field name.
        warnings: Where warnings go.

    Returns:
        The names of the fields that were given a value and filled in none of their places: form fields that run
        over more than one paragraph, each with a warning. A field filled in some of its places is filled.

    Raises:
        Refusal: A field does not take its value.
    """
    places: dict[str, list[_Spot]] = {}
    for spot in find(document).spots:
        places.setdefault(spot.field.name, []).append(spot)
    checked = {name: [spot.check(values[name]) for spot in spots] for name, spots in places.items() if name in values}
    not_filled: list[str] = []
    for name, spots in places.items():
        if name not in checked:
            continue
        done = False
        for spot, value in zip(spots, checked[name], strict=True):
            if spot.field.kind == "form_field":
                done = _fill_form(spot, value, warnings) or done
            else:
                _fill_control(spot, value, warnings)
                done = True
        if not done:
            not_filled.append(name)
    return not_filled
