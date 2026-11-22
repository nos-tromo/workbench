"""A deck built in a user's slide master: the master's example slides removed, then one slide a layout for each asked.

A slide is made from the layout it names (:func:`resolve`), or, naming none, from the first that takes it
(:func:`fitting`); a title slide's only text block is its subtitle (:func:`subtitle_as_meant`). Its title and subtitle,
block i of its content in place i of the layout (the reading order ``inspect_template`` lists), as :mod:`.blocks` places
text, tables, charts and pictures,
the layout's footer and slide number (:func:`copy_footers`), and its notes with its sources. A place no block fills is
removed, so that no slide says "Click to add text". The example slides go first, with what in the package still
describes them (:func:`remove_slides`), and before the deck is written no example slide may be left in it and its part
names must be unique (:func:`check_parts`).

The master is untrusted: its python-pptx objects are worked on under
:func:`~workbench.executor.runners.templates.library`, so whatever the library fails with is the file's damage. What the
deck cannot take is a :class:`~workbench.executor.templates.Refusal`, and what was left out or changed is a warning;
both name the slide, never a path of the job. Nothing here logs: a slide's text and a layout's name are the user's.
"""

import copy
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, Final

from pptx.enum.shapes import PP_PLACEHOLDER
from pptx.opc.constants import CONTENT_TYPE as CT
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.opc.oxml import serialize_part_xml
from pptx.oxml import parse_xml
from pptx.oxml.ns import qn
from pptx.oxml.xmlchemy import OxmlElement

from workbench import decks
from workbench.executor.context import JobContext
from workbench.executor.presentations import blocks, master
from workbench.executor.presentations.master import Layout
from workbench.executor.runners.templates import LISTED, LISTED_ROOM, cut, library
from workbench.executor.templates import Refusal, safe_text, sources

#: The extension of ``ppt/presentation.xml`` that holds PowerPoint's sections, and the sections' element.
_SECTIONS: Final = "{521415D9-36F7-43E2-AB2F-B90AF26B5E84}"
_SECTION_LIST: Final = "{http://schemas.microsoft.com/office/powerpoint/2010/main}sectionLst"

#: The package's relationships to what describes its slides: the thumbnail, a picture of the first, and the extended
#: properties, which count them and list their titles.
_SLIDE_METADATA: Final = frozenset({RT.THUMBNAIL, RT.EXTENDED_PROPERTIES})

#: The presentation's relationships to the parts that name who edited the master or commented on it: the authors of
#: PowerPoint's comments (``ppt/authors.xml``) and of its older ones (``ppt/commentAuthors.xml``), its changes
#: (``ppt/changesInfos/``) and its revisions (``ppt/revisionInfo.xml``).
_EDITORS: Final = frozenset(
    {
        "http://schemas.microsoft.com/office/2018/10/relationships/authors",
        RT.COMMENT_AUTHORS,
        "http://schemas.microsoft.com/office/2016/11/relationships/changesInfo",
        "http://schemas.microsoft.com/office/2015/10/relationships/revisionInfo",
    }
)

#: The one core property a deck keeps of its master's.
_LANGUAGE: Final = qn("dc:language")
_LINKED: Final = (
    "the master links to its own slides (a link on a layout or the master); remove the links or save it without slides"
)

_TITLES: Final = frozenset({PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE})
_SUBTITLES: Final = frozenset({PP_PLACEHOLDER.SUBTITLE})


def remove_slides(prs: Any) -> None:
    """Remove the slides a master has, and what in the package points at them, describes them or names its people.

    The custom shows and PowerPoint's sections go first: they name slides, and dropping a slide's relationship would
    leave them pointing at nothing (or, for a section, at a new slide that is given the old one's id). The slide show
    then runs the whole deck (:func:`_show_every_slide`), and the outline view collapses none of the master's slides
    (:func:`_forget_outline`), whose links would keep them in the package. The thumbnail and the extended properties
    (``docProps/app.xml``) are dropped, as they show or list the master's slides, and the parts that name who edited
    the master or commented on it (:data:`_EDITORS`); the core properties are the deck's own
    (:func:`_reset_core_properties`). A part is saved only while a relationship reaches it, and ``[Content_Types].xml``
    is written from the parts saved, so none is left there.

    Args:
        prs: The presentation, open.
    """
    package = prs.part.package
    for rid, relationship in list(package._rels.items()):
        if relationship.reltype in _SLIDE_METADATA:
            package.drop_rel(rid)
    for rid, relationship in list(prs.part.rels.items()):
        if relationship.reltype in _EDITORS:
            prs.part.drop_rel(rid)
    _reset_core_properties(prs)
    _show_every_slide(prs)
    _forget_outline(prs)
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


def _reset_core_properties(prs: Any) -> None:
    """Give the deck core properties of its own: none of the master's texts, names or dates, its language kept.

    A master's core properties name people (its author and last editor) and may hold an example slide's title, so its
    title, subject, keywords, comments, category, author, last editor, identifier, status, version and last printing
    all go; the deck is revision 1, made and modified now (UTC).

    Args:
        prs: The presentation, open.
    """
    core = prs.core_properties
    element = core._element
    for child in list(element):
        if child.tag != _LANGUAGE:
            element.remove(child)
    now = datetime.now(UTC)
    core.revision = 1
    core.created = now
    core.modified = now


def _show_every_slide(prs: Any) -> None:
    """Make the slide show run the whole deck: neither a custom show, as the deck has none, nor a range of slides.

    A range numbers the master's slides, so the show could start at a slide the deck does not have.
    ``ppt/presProps.xml`` is a part python-pptx keeps as bytes; it is parsed, changed and written back.

    Args:
        prs: The presentation, open.
    """
    try:
        properties = prs.part.part_related_by(RT.PRES_PROPS)
    except (KeyError, ValueError):  # none, or more than one
        return
    root = parse_xml(properties.blob)
    chosen = [*root.findall(f"{qn('p:showPr')}/{qn('p:custShow')}"), *root.findall(f"{qn('p:showPr')}/{qn('p:sldRg')}")]
    for choice in chosen:
        choice.getparent().remove(choice)
    if chosen:
        properties.blob = serialize_part_xml(root)


def _forget_outline(prs: Any) -> None:
    """Leave out the outline view's list of the slides it collapses, with its links to them: they are the master's.

    A slide that ``ppt/viewProps.xml`` still relates to would be saved, hidden, in the deck. That part is one
    python-pptx keeps as bytes; it is parsed, changed and written back, and its relationships to slides dropped.

    Args:
        prs: The presentation, open.
    """
    try:
        view = prs.part.part_related_by(RT.VIEW_PROPS)
    except (KeyError, ValueError):  # none, or more than one
        return
    root = parse_xml(view.blob)
    listed = root.findall(f"{qn('p:outlineViewPr')}/{qn('p:sldLst')}")
    for slides in listed:
        slides.getparent().remove(slides)
    if listed:
        view.blob = serialize_part_xml(root)
    for rid, relationship in list(view.rels.items()):
        if relationship.reltype == RT.SLIDE:
            view.rels.pop(rid)


def resolve(layouts: Sequence[Layout], wanted: int | str, slide: int) -> Layout:
    """Find the layout a slide names: by number, or by name ignoring case and outer spaces.

    A name of digits that names no layout is a number.

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
        key = wanted.strip().casefold()
        named = [layout for layout in layouts if layout.name.strip().casefold() == key]
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


def _takes(place_role: str, block: object) -> bool:
    """Tell whether a place takes a block, as :func:`~.blocks.check_places` would: an empty text goes anywhere.

    Args:
        place_role: The place's role.
        block: The block.

    Returns:
        Whether the block may go into the place.
    """
    if isinstance(block, str) and not block.strip():
        return True
    kind = decks.block_kind(block)
    return kind is not None and place_role in blocks.ACCEPTS[kind]


def fitting(layouts: Sequence[Layout], slide: Mapping[str, Any], number: int) -> Layout:
    """Find the layout for a slide that names none: the first, in the master's order, that takes it as it is.

    A layout takes a slide when it has a title if the slide has one, a subtitle if it has one, and exactly one place a
    block, each taking its block. One whose title and subtitle are exactly the slide's goes first, so a slide with a
    title and nothing else is put on a title-only layout rather than a title slide when the master has one.

    Args:
        layouts: The master's layouts.
        slide: The slide, without a layout.
        number: Its number.

    Returns:
        The layout.

    Raises:
        Refusal: No layout takes the slide (``invalid_value``), listing the layouts (:func:`_listed`).
    """
    content = slide.get("content", [])
    title, subtitle = bool(slide.get("title")), bool(slide.get("subtitle"))
    takes = [
        layout
        for layout in layouts
        if (layout.title or not title)
        and (layout.subtitle or not subtitle)
        and len(layout.places) == len(content)
        and all(_takes(place.role, block) for place, block in zip(layout.places, content, strict=True))
    ]
    exact = [layout for layout in takes if (layout.title, layout.subtitle) == (title, subtitle)]
    if exact or takes:
        return (exact or takes)[0]
    said = f"slide {number}: it names no layout, and none takes it as it is"
    if not layouts:
        raise Refusal("invalid_value", f"{said}; the master has no layouts")
    raise Refusal("invalid_value", f"{said}; give its layout: {_listed(layouts)}")


def subtitle_as_meant(layout: Layout, slide: Mapping[str, Any]) -> Mapping[str, Any]:
    """Read a title slide's only text block as its subtitle, as a model writes it by mistake; any other slide as asked.

    That is a slide with no subtitle on a layout that has a subtitle and no places, whose content is one text block
    within a subtitle's bounds (:data:`workbench.decks.MAX_SUBTITLE`); a longer one is refused as before.

    Args:
        layout: The slide's layout.
        slide: The slide as asked.

    Returns:
        The slide meant.
    """
    content = slide.get("content", [])
    if layout.places or not layout.subtitle or slide.get("subtitle") or len(content) != 1:
        return slide
    (block,) = content
    if not isinstance(block, str) or not block.strip() or len(block) > decks.MAX_SUBTITLE:
        return slide
    return {**slide, "subtitle": block, "content": []}


def _unknown(layouts: Sequence[Layout], said: str, slide: int) -> Refusal:
    """Refuse a layout that is not there, listing those that are.

    Args:
        layouts: The master's layouts.
        said: The layout as the slide named it.
        slide: The slide's number.

    Returns:
        The refusal, with the layouts as :func:`_listed` lists them.
    """
    if not layouts:
        return Refusal("invalid_value", f"slide {slide}: no layout {said}; the master has no layouts")
    return Refusal("invalid_value", f"slide {slide}: no layout {said}; the layouts are {_listed(layouts)}")


def _listed(layouts: Sequence[Layout]) -> str:
    """List a master's layouts for a refusal.

    Args:
        layouts: The master's layouts, at least one.

    Returns:
        At most :data:`~workbench.executor.runners.templates.LISTED` layouts by number and name, in at most
        :data:`~workbench.executor.runners.templates.LISTED_ROOM` characters, and how many more there are.
    """
    shown: list[str] = []
    used = 0
    for layout in layouts[:LISTED]:
        entry = f"{layout.number} {cut(layout.name, decks.MAX_LAYOUT_NAME)}".rstrip()
        if shown and used + len(entry) + 2 > LISTED_ROOM:
            break
        shown.append(entry)
        used += len(entry) + 2
    more = len(layouts) - len(shown)
    return ", ".join(shown) + (f" and {more} more" if more else "")


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

    Every layout, every block's place, every picture and every source is checked before a slide is made.

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
    found = master.layouts(prs)
    chosen: list[Layout] = []
    slides: list[Mapping[str, Any]] = []
    for number, asked in enumerate(args["slides"], 1):
        named = asked.get("layout")
        layout = fitting(found, asked, number) if named is None else resolve(found, named, number)
        chosen.append(layout)
        slides.append(subtitle_as_meant(layout, asked))
    for number, (slide, layout) in enumerate(zip(slides, chosen, strict=True), 1):
        blocks.check_places(layout, slide.get("content", []), number)
    images = blocks.find_images(slides, ctx)
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
        library(partial(_slide, prs, layout, slide, number, notes, images, warnings))
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


def _slide(
    prs: Any,
    layout: Layout,
    spec: Mapping[str, Any],
    number: int,
    notes: str,
    images: Mapping[str, Path],
    warnings: list[str],
) -> None:
    """Add one slide: its title and subtitle, its blocks, its footers and its notes; the places left empty removed.

    The places that hold something are known by ``idx``, which a placeholder keeps when a table or chart takes its
    place, or a picture a picture place's.

    Args:
        prs: The presentation.
        layout: The slide's layout.
        spec: The slide as asked.
        number: Its number.
        notes: Its speaker notes (:func:`_notes`).
        images: The deck's pictures (:func:`~.blocks.find_images`).
        warnings: Where warnings go.
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
    blocks.fill(slide, layout, spec.get("content", []), number, images, filled, warnings)
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
