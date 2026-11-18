"""Markdown in a slide's text frame, and how many lines of a place it takes.

The model writes the text of a slide as Markdown: bullets, numbered items, bold and italic, links.
:func:`write_markdown` puts it into a python-pptx text frame, and :func:`estimate_lines` says how many lines it takes
in a place of a given width, so a block that is too long can be shrunk before it is written.

A list item's bullet is the master's own: a bullet item carries no bullet property, so the frame's list style gives it
its bullet, indent and size at its level. A paragraph that is not an item says ``a:buNone``, and a numbered item
``a:buAutoNum``. The levels are the lists' depth, at most :data:`MAX_LEVEL`. Everything else the Markdown can say is
text: a heading is a bold paragraph, a code block its lines as paragraphs, a quote its text, an image its alternative
text, a link its text and address in parentheses, inline code its text, and a table or HTML what was typed. Nothing here
logs or names a path: the text is the user's content.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from workbench.executor.templates import xml_safe

#: The deepest level of a paragraph, as a slide's text has three.
MAX_LEVEL: Final = 2

#: A thematic break, written as a paragraph of dashes, as a Word document's is.
RULE: Final = "—" * 3

#: The characters of a line that each level of indent takes.
_INDENT_CHARS: Final = 4

_NUMBERING: Final = "arabicPeriod"


@dataclass(frozen=True)
class Written:
    """What :func:`write_markdown` wrote, as much as :func:`estimate_lines` needs.

    Attributes:
        lines: Each paragraph's level and characters, in order; a line break inside a paragraph starts another entry
            at the same level, as it starts a line.
        replaced: How many characters XML cannot hold were replaced with U+FFFD.
    """

    lines: tuple[tuple[int, int], ...]
    replaced: int


@dataclass
class _List:
    """A list that is open while the Markdown is walked.

    Attributes:
        ordered: Whether it is numbered.
        start: The number of its first item.
        fresh: Whether its current item has had no paragraph yet: the first one takes the item's bullet or number.
    """

    ordered: bool
    start: int
    fresh: bool = False


class _Writer:
    """Writes Markdown's tokens into a text frame.

    Attributes:
        frame: The python-pptx text frame, cleared.
        replaced: Characters XML cannot hold that were replaced with U+FFFD.
        lines: The level and characters of each line closed so far.
        lists: The lists open, innermost last.
    """

    def __init__(self, frame: Any) -> None:
        """Clear the frame and start at its first paragraph.

        Args:
            frame: A python-pptx text frame.
        """
        frame.clear()
        first = frame.paragraphs[0]._p
        properties = first.pPr
        if properties is not None:  # what the paragraph said before is no longer true of what is written
            first.remove(properties)
        self.frame = frame
        self.replaced = 0
        self.lines: list[tuple[int, int]] = []
        self.lists: list[_List] = []
        self._used = False
        self._open = False
        self._level = 0
        self._chars = 0

    def close(self) -> Written:
        """End the line that is open.

        Returns:
            What was written.
        """
        self._close()
        return Written(tuple(self.lines), self.replaced)

    def _close(self) -> None:
        """Count the line that is open, if one is."""
        if self._open:
            self.lines.append((self._level, self._chars))
            self._open = False

    def _begin(self, level: int) -> None:
        """Start counting a line.

        Args:
            level: The level of the paragraph it is in.
        """
        self._close()
        self._open = True
        self._level = level
        self._chars = 0

    def paragraph(self) -> Any:
        """Start a paragraph: the first of a list item takes the item's bullet or number, any other has none.

        Returns:
            The paragraph.
        """
        if self._used:
            paragraph = self.frame.add_paragraph()
        else:
            paragraph = self.frame.paragraphs[0]
            self._used = True
        current = self.lists[-1] if self.lists else None
        level = min(len(self.lists) - 1, MAX_LEVEL) if current is not None else 0
        paragraph.level = level
        if current is not None and current.fresh:
            current.fresh = False
            if current.ordered:
                _bullet(paragraph, "buAutoNum", start=current.start)
        else:
            _bullet(paragraph, "buNone")
        self._begin(level)
        return paragraph

    def run(self, paragraph: Any, text: str, *, bold: bool = False, italic: bool = False) -> None:
        """Add a run of text to a paragraph, counting what had to go.

        Args:
            paragraph: The paragraph.
            text: The text, without a line break.
            bold: Whether it is bold.
            italic: Whether it is italic.
        """
        if not text:
            return
        written, _ = xml_safe(text)
        self.replaced += sum(1 for old, new in zip(text, written, strict=True) if old != new)
        run = paragraph.add_run()
        run.text = written
        run.font.bold = True if bold else None
        run.font.italic = True if italic else None
        self._chars += len(written)

    def inline(self, paragraph: Any, children: Sequence[Any], *, bold: bool = False) -> None:
        """Write a block's inline tokens into a paragraph: bold, italic, code, links and images as text.

        Args:
            paragraph: The paragraph.
            children: The inline tokens.
            bold: Whether everything is bold, as in a heading.
        """
        strong = emphasis = 0
        links: list[tuple[str, list[str]]] = []

        def add(text: str) -> None:
            self.run(paragraph, text, bold=bold or strong > 0, italic=emphasis > 0)
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
            elif kind == "softbreak":
                add(" ")
            elif kind == "hardbreak":
                paragraph.add_line_break()
                self._begin(self._level)
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
            else:  # text and code_inline; with html off, no other token carries a content
                add(token.content)

    def code(self, text: str) -> None:
        """Write a code block: each line a paragraph of its own, the blank ones kept.

        Args:
            text: The block's text.
        """
        if not text:
            return
        for line in text.removesuffix("\n").split("\n"):
            self.run(self.paragraph(), line)

    def write(self, tokens: Sequence[Any]) -> None:
        """Write the text's tokens.

        Args:
            tokens: What markdown-it made of the Markdown.
        """
        heading = False
        for token in tokens:
            kind = token.type
            if kind == "heading_open":
                heading = True
            elif kind == "heading_close":
                heading = False
            elif kind == "inline":
                paragraph = self.paragraph()
                self.inline(paragraph, token.children or [], bold=heading)
            elif kind in {"bullet_list_open", "ordered_list_open"}:
                if self.lists:  # an item that goes straight into a list has no text of its own to bullet
                    self.lists[-1].fresh = False
                start = token.attrs.get("start", 1) if kind == "ordered_list_open" else 1
                self.lists.append(_List(ordered=kind == "ordered_list_open", start=int(start)))
            elif kind in {"bullet_list_close", "ordered_list_close"}:
                self.lists.pop()
            elif kind == "list_item_open" and self.lists:
                self.lists[-1].fresh = True
            elif kind == "hr":
                self.run(self.paragraph(), RULE)
            elif kind in {"fence", "code_block"}:
                self.code(token.content)


def _bullet(paragraph: Any, kind: str, *, start: int = 1) -> None:
    """Set a paragraph's bullet: none, or an automatic number.

    Args:
        paragraph: The python-pptx paragraph, whose ``a:pPr`` has no other child yet.
        kind: ``buNone`` or ``buAutoNum``.
        start: The number a numbered list starts at; said only when it is not 1.
    """
    from pptx.oxml.xmlchemy import OxmlElement

    element = OxmlElement(f"a:{kind}")
    if kind == "buAutoNum":
        element.set("type", _NUMBERING)
        if start != 1:
            element.set("startAt", str(start))
    paragraph._p.get_or_add_pPr().append(element)


def write_markdown(frame: Any, markdown: str) -> Written:
    """Write Markdown into a text frame, which is cleared first.

    Bullets and numbered items keep their depth as the paragraph's level (at most :data:`MAX_LEVEL`), a paragraph that
    is not an item has no bullet, and bold and italic are runs. Nothing else of Markdown's formatting is kept; see the
    module's docstring.

    Args:
        frame: A python-pptx text frame.
        markdown: The text.

    Returns:
        The level and characters of each paragraph, and how many characters XML cannot hold were replaced with U+FFFD.
    """
    from markdown_it import MarkdownIt

    writer = _Writer(frame)
    writer.write(MarkdownIt("commonmark", {"html": False}).parse(markdown))
    return writer.close()


def estimate_lines(written: Written, chars: int) -> int:
    """Estimate the lines that written text takes in a place whose lines hold ``chars`` characters.

    Each level of indent takes four characters of a line. A paragraph is at least one line, however short, and a word
    longer than a line is counted as the lines it would be cut into.

    Args:
        written: What :func:`write_markdown` wrote.
        chars: How many characters a line of the place holds.

    Returns:
        The sum, over the paragraphs, of the lines each takes.
    """
    return sum(
        max(1, math.ceil(characters / max(1, chars - _INDENT_CHARS * level))) for level, characters in written.lines
    )
