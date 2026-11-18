"""A deck built in a user's slide master: the master's example slides removed, then one slide a layout for each asked.

A slide is made from the layout it names (:func:`resolve`): its title and subtitle, block i of its content in place i of
the layout (the reading order ``inspect_template`` lists), the layout's footer and slide number (:func:`copy_footers`),
and its notes with its sources. A place no block fills is removed, so that no slide says "Click to add text"; text that
may not fit its place is shrunk (:func:`fit`). The example slides go first, with what in the package still describes
them (:func:`remove_slides`), and before the deck is written no example slide may be left in it and its part names must
be unique (:func:`check_parts`).

The master is untrusted: its python-pptx objects are worked on under
:func:`~workbench.executor.runners.templates.library`, so whatever the library fails with is the file's damage. What the
deck cannot take is a :class:`~workbench.executor.templates.Refusal`, and what was left out or changed is a warning;
both name the slide, never a path of the job. Nothing here logs: a slide's text and a layout's name are the user's.
"""

import copy
import dataclasses
from collections import Counter
from collections.abc import Mapping, Sequence
from functools import partial
from typing import Any, Final

from pptx.enum.shapes import PP_PLACEHOLDER
from pptx.enum.text import MSO_AUTO_SIZE
from pptx.opc.constants import CONTENT_TYPE as CT
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.opc.oxml import serialize_part_xml
from pptx.oxml import parse_xml
from pptx.oxml.ns import qn
from pptx.oxml.xmlchemy import OxmlElement

from workbench import decks
from workbench.executor.context import JobContext
from workbench.executor.presentations import master, text
from workbench.executor.presentations.master import Layout, Place
from workbench.executor.runners.templates import LISTED, LISTED_ROOM, cut, library
from workbench.executor.templates import Refusal, safe_text, sources

#: ``normAutofit``'s ``fontScale``, in thousandths of a percent: the full size, and the least that text is shrunk to.
FULL_SCALE: Final = 100_000
MIN_SCALE: Final = 60_000

#: How much shrunk text's line spacing is reduced, in thousandths of a percent.
LINE_REDUCTION: Final = "10000"

#: The extension of ``ppt/presentation.xml`` that holds PowerPoint's sections, and the sections' element.
_SECTIONS: Final = "{521415D9-36F7-43E2-AB2F-B90AF26B5E84}"
_SECTION_LIST: Final = "{http://schemas.microsoft.com/office/powerpoint/2010/main}sectionLst"

#: The package's relationships to what describes its slides: the thumbnail, a picture of the first, and the extended
#: properties, which count them and list their titles.
_SLIDE_METADATA: Final = frozenset({RT.THUMBNAIL, RT.EXTENDED_PROPERTIES})
_LINKED: Final = (
    "the master links to its own slides (a link on a layout or the master); remove the links or save it without slides"
)

_TITLES: Final = frozenset({PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE})
_SUBTITLES: Final = frozenset({PP_PLACEHOLDER.SUBTITLE})
_TEXT_ROLES: Final = frozenset({"text", "content"})
_REPLACED: Final = "characters a document cannot hold were replaced with �"


def remove_slides(prs: Any) -> None:
    """Remove the slides a master has, and what in the package points at them or describes them.

    The custom shows and PowerPoint's sections go first: they name slides, and dropping a slide's relationship would
    leave them pointing at nothing (or, for a section, at a new slide that is given the old one's id). The slide show
    then runs no custom show (:func:`_run_no_custom_show`). The thumbnail and the extended properties
    (``docProps/app.xml``) are dropped, as they show or list the master's slides; the core properties are the master's
    own and stay. A part is saved only while a relationship reaches it, and ``[Content_Types].xml`` is written from the
    parts saved, so neither is left there.

    Args:
        prs: The presentation, open.
    """
    package = prs.part.package
    for rid, relationship in list(package._rels.items()):
        if relationship.reltype in _SLIDE_METADATA:
            package.drop_rel(rid)
    _run_no_custom_show(prs)
    root = prs.part._element
    for shows in root.findall(qn("p:custShowLst")):
        root.remove(shows)
    extensions = root.find(qn("p:extLst"))
    if extensions is not None:
        for extension in extensions.findall(qn("p:ext")):
            if extension.get("uri") == _SECTIONS or extension.find(_SECTION_LIST) is not None:
                extensions.remove(extension)
        if len(extensions) == 0:
            root.remove(extensions)
    listed = root.find(qn("p:sldIdLst"))
    if listed is None:
        return
    ids = [entry.get(qn("r:id")) for entry in listed]
    for entry in list(listed):
        listed.remove(entry)
    for rid in dict.fromkeys(ids):
        if rid in prs.part.rels:
            prs.part.drop_rel(rid)


def _run_no_custom_show(prs: Any) -> None:
    """Make the slide show run the whole deck: the deck has no custom show, so one the master's show names is gone.

    ``ppt/presProps.xml`` is a part python-pptx keeps as bytes; it is parsed, changed and written back.

    Args:
        prs: The presentation, open.
    """
    try:
        properties = prs.part.part_related_by(RT.PRES_PROPS)
    except (KeyError, ValueError):  # none, or more than one
        return
    root = parse_xml(properties.blob)
    shows = root.findall(f"{qn('p:showPr')}/{qn('p:custShow')}")
    for show in shows:
        show.getparent().remove(show)
    if shows:
        properties.blob = serialize_part_xml(root)


def resolve(layouts: Sequence[Layout], wanted: int | str, slide: int) -> Layout:
    """Find the layout a slide names: by number, or by name ignoring case; a name of digits that names none is a number.

    Args:
        layouts: The master's layouts, numbered from 1.
        wanted: The slide's ``layout``.
        slide: The slide's number, for the message.

    Returns:
        The layout.

    Raises:
        Refusal: No layout is so named or numbered, or two or more share the name (``invalid_value``).
    """
    if isinstance(wanted, str):
        named = [layout for layout in layouts if layout.name.casefold() == wanted.casefold()]
        if len(named) == 1:
            return named[0]
        if named:
            count = "two" if len(named) == 2 else str(len(named))
            name = cut(named[0].name, decks.MAX_LAYOUT_NAME)
            numbers = ", ".join(str(layout.number) for layout in named[:-1]) + f" or {named[-1].number}"
            raise Refusal(
                "invalid_value", f"slide {slide}: {count} layouts are named {name!r}; give its number: {numbers}"
            )
        if not (wanted.isascii() and wanted.isdigit()):
            raise _unknown(layouts, repr(wanted), slide)
        wanted = int(wanted)
    if 1 <= wanted <= len(layouts):
        return layouts[wanted - 1]
    raise _unknown(layouts, str(wanted), slide)


def _unknown(layouts: Sequence[Layout], said: str, slide: int) -> Refusal:
    """Refuse a layout that is not there, listing those that are.

    Args:
        layouts: The master's layouts.
        said: The layout as the slide named it.
        slide: The slide's number.

    Returns:
        The refusal: at most :data:`~workbench.executor.runners.templates.LISTED` layouts by number and name, in at
        most :data:`~workbench.executor.runners.templates.LISTED_ROOM` characters, and how many more there are.
    """
    if not layouts:
        return Refusal("invalid_value", f"slide {slide}: no layout {said}; the master has no layouts")
    shown: list[str] = []
    used = 0
    for layout in layouts[:LISTED]:
        entry = f"{layout.number} {cut(layout.name, decks.MAX_LAYOUT_NAME)}".rstrip()
        if shown and used + len(entry) + 2 > LISTED_ROOM:
            break
        shown.append(entry)
        used += len(entry) + 2
    more = len(layouts) - len(shown)
    listed = ", ".join(shown) + (f" and {more} more" if more else "")
    return Refusal("invalid_value", f"slide {slide}: no layout {said}; the layouts are {listed}")


def copy_footers(slide: Any, layout: Layout, number: int) -> None:
    """Copy the layout's date, footer and slide number onto a slide, with their text, as PowerPoint puts them there.

    python-pptx gives a slide none of them, and a copy it would make has no text, so none would show. Each is the
    layout's shape with its text, its shape properties emptied (the layout's position is inherited), a new id and the
    slide's number as the slide-number field's text. A kind ``p:hf`` turns off is not copied
    (:func:`~workbench.executor.presentations.master.shown_footers`); nor is a date that holds a field, whose text is
    the day the master was saved, nor one whose text is empty. A link in the copy is left out, its text kept: the
    relationship it names is the layout's.

    Args:
        slide: The python-pptx slide, made from the layout.
        layout: The layout.
        number: The slide's number.
    """
    shown = master.shown_footers(layout.layout, layout.layout.slide_master)
    tree = slide.shapes._spTree
    for placeholder in layout.layout.placeholders:
        kind = placeholder.placeholder_format.type
        if kind not in shown:
            continue
        element = copy.deepcopy(placeholder._element)
        fields = list(element.iter(qn("a:fld")))
        if kind == PP_PLACEHOLDER.DATE and fields:
            continue
        for field in fields:
            if field.get("type") == "slidenum":
                cached = field.find(qn("a:t"))
                if cached is None:
                    cached = OxmlElement("a:t")
                    field.append(cached)
                cached.text = str(number)
        properties = element.find(qn("p:spPr"))
        names = element.find(f"{qn('p:nvSpPr')}/{qn('p:cNvPr')}")
        if properties is None or names is None or not "".join(element.itertext()).strip():
            continue
        for linked in element.xpath(".//*[@r:*]"):
            linked.getparent().remove(linked)
        element.replace(properties, OxmlElement("p:spPr"))
        names.set("id", str(slide.shapes._next_shape_id))
        tree.insert_element_before(element, "p:extLst")


def fit(frame: Any, needed: int, place: Place) -> bool:
    """Shrink a text frame's text when it needs more lines than its place has, as PowerPoint's autofit would.

    The font is scaled to the place's lines over the lines needed, but not below :data:`MIN_SCALE`, and the line
    spacing is reduced by :data:`LINE_REDUCTION`. A bare ``normAutofit`` would shrink nothing until the text is edited.

    Args:
        frame: The python-pptx text frame, written.
        needed: The lines its text takes at full size (:func:`~workbench.executor.presentations.text.estimate_lines`).
        place: The place it fills.

    Returns:
        Whether the text fits at the scale written; ``False`` when even :data:`MIN_SCALE` is too large.
    """
    if needed <= place.lines:
        return True
    frame.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
    autofit = frame._bodyPr.normAutofit
    autofit.set("fontScale", str(max(MIN_SCALE, FULL_SCALE * place.lines // needed)))
    autofit.set("lnSpcReduction", LINE_REDUCTION)
    return FULL_SCALE * place.lines >= MIN_SCALE * needed


def check_parts(prs: Any) -> None:
    """Make sure the deck holds no slide but its own, and each part a name of its own, as a package's names ignore case.

    An example slide that another part still relates to (a link on a layout or the master) stays in the package,
    hidden, under its old name, which a new slide may be given too.

    Args:
        prs: The presentation, built.

    Raises:
        Refusal: A slide the deck does not list is in it, or two parts share a name (``invalid_template``).
    """
    parts = list(prs.part.package.iter_parts())
    entries = prs.part._element.find(qn("p:sldIdLst"))
    listed = {prs.part.related_part(entry.get(qn("r:id"))) for entry in ([] if entries is None else entries)}
    if any(part.content_type == CT.PML_SLIDE and part not in listed for part in parts):
        raise Refusal("invalid_template", _LINKED)
    names = Counter(str(part.partname).lower() for part in parts)
    if any(count > 1 for count in names.values()):
        raise Refusal("invalid_template", "the master cannot be written as a new deck")


def build(prs: Any, args: Mapping[str, Any], ctx: JobContext, warnings: list[str]) -> int:
    """Build the deck in the master: its example slides removed, then every slide asked for, in order.

    Every layout and every source is checked before a slide is made.

    Args:
        prs: The master, open.
        args: The runner's arguments: ``slides`` (checked with :func:`workbench.decks.deck_problem`) and
            ``include_sources``.
        ctx: The job's surroundings.
        warnings: Where warnings go.

    Returns:
        How many slides the deck has.

    Raises:
        Refusal: A layout, block or source cannot be used, or the master cannot be written as a new deck.
        readers.UnreadableFile: The master is damaged.
        readers.ReadTimeout: The work ran past the deadline.
    """
    slides: Sequence[Mapping[str, Any]] = args["slides"]
    found = master.layouts(prs)
    chosen = [resolve(found, slide["layout"], number) for number, slide in enumerate(slides, 1)]
    cited = sources.check(
        {
            _label(number, index): {"source": source}
            for number, slide in enumerate(slides, 1)
            for index, source in enumerate(slide.get("sources", []), 1)
        },
        ctx,
    )
    include = bool(args.get("include_sources", True))
    library(lambda: remove_slides(prs))
    for number, (slide, layout) in enumerate(zip(slides, chosen, strict=True), 1):
        notes = _notes(slide, number, cited if include else None)
        library(partial(_slide, prs, layout, slide, number, notes, warnings))
    library(lambda: check_parts(prs))
    return len(slides)


def _label(number: int, index: int) -> str:
    """Name a source of a slide, as its refusal names it.

    Args:
        number: The slide's number.
        index: The source's place among the slide's, from 1.

    Returns:
        ``slide <number>, source <index>``.
    """
    return f"slide {number}, source {index}"


def _notes(slide: Mapping[str, Any], number: int, cited: Mapping[str, str] | None) -> str:
    """Write a slide's speaker notes: its notes, then a blank line and its sources, one a line as kind and reference.

    Args:
        slide: The slide.
        number: Its number.
        cited: The sheets its sources name, as :func:`workbench.executor.templates.sources.check` found them; ``None``
            to leave the sources out.

    Returns:
        The notes, empty when there is nothing to say.
    """
    parts: list[str] = [str(slide["notes"])] if slide.get("notes") else []
    if cited is not None and slide.get("sources"):
        lines = [f"{sources.TITLE}:"]
        for index, source in enumerate(slide["sources"], 1):
            kind = str(source.get("kind", ""))
            where = sources.reference(source, cited.get(_label(number, index)))
            lines.append(f"{kind}: {where}" if where else kind)
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _slide(prs: Any, layout: Layout, spec: Mapping[str, Any], number: int, notes: str, warnings: list[str]) -> None:
    """Add one slide: its title and subtitle, its blocks, its footers and its notes; the places left empty removed.

    The places that hold something are known by ``idx``, which a placeholder keeps when a table, chart or picture
    takes its place.

    Args:
        prs: The presentation.
        layout: The slide's layout.
        spec: The slide as asked.
        number: Its number.
        notes: Its speaker notes (:func:`_notes`).
        warnings: Where warnings go.

    Raises:
        Refusal: A block cannot go into its place.
    """
    slide = prs.slides.add_slide(layout.layout)
    filled: set[int] = set()
    for field, kinds in (("title", _TITLES), ("subtitle", _SUBTITLES)):
        value = spec.get(field)
        if not value:
            continue
        shape = next((shape for shape in slide.placeholders if shape.placeholder_format.type in kinds), None)
        if shape is None:
            warnings.append(f"slide {number}: the layout has no place for a {field}; it was left out")
            continue
        shape.text_frame.text = safe_text(f"slide {number}, {field}", str(value), warnings)
        filled.add(shape.placeholder_format.idx)
    _content(slide, layout, spec.get("content", []), number, filled, warnings)
    for shape in list(slide.placeholders):
        if shape.placeholder_format.idx not in filled:
            shape._element.getparent().remove(shape._element)
    copy_footers(slide, layout, number)
    if notes:
        frame = slide.notes_slide.notes_text_frame
        if frame is None:
            warnings.append(f"slide {number}: the master's notes page has no place for notes; they were left out")
        else:
            frame.text = safe_text(f"slide {number}, notes", notes, warnings)


def _content(
    slide: Any, layout: Layout, blocks: Sequence[Any], number: int, filled: set[int], warnings: list[str]
) -> None:
    """Fill the slide's places with its blocks, block i in place i.

    Args:
        slide: The slide.
        layout: Its layout.
        blocks: The slide's content.
        number: Its number.
        filled: The ``idx`` of each place that holds something, added to.
        warnings: Where warnings go.

    Raises:
        Refusal: There are more blocks than places, or a block cannot go into its place (``invalid_value``).
    """
    if len(blocks) > len(layout.places):
        places = len(layout.places)
        room = "no place for one" if not places else f"{places} place{'' if places == 1 else 's'}"
        count = f"{len(blocks)} block{'' if len(blocks) == 1 else 's'}"
        raise Refusal("invalid_value", f"slide {number}: {count}, but layout {layout.number} has {room}")
    for index, (block, place) in enumerate(zip(blocks, layout.places[: len(blocks)], strict=True), 1):
        where = f"slide {number}, block {index}"
        kind = decks.block_kind(block)
        if kind != "text":
            raise Refusal("invalid_value", f"{where}: a {kind} block is not written in this version")
        if place.role not in _TEXT_ROLES:
            raise Refusal(
                "invalid_value",
                f"{where}: text goes into a text or content place, and place {index} takes a {place.role}",
            )
        if not block.strip():
            continue
        try:
            on_slide = dataclasses.replace(place, shape=slide.placeholders[place.idx])
        except KeyError:
            warnings.append(f"{where}: the layout's place {index} is not on the slide; the block was left out")
            continue
        frame = on_slide.shape.text_frame
        written = text.write_markdown(frame, block)
        if written.replaced:
            warnings.append(f"{where}: {_REPLACED}")
        if not fit(frame, text.estimate_lines(written, on_slide.chars), on_slide):
            warnings.append(
                f"slide {number}: the text may not fit, even at {MIN_SCALE // 1000} % of its size (block {index}); "
                "shorten it or move some of it to another slide"
            )
        filled.add(place.idx)
