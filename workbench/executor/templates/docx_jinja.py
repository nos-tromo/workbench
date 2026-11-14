"""``{{ }}`` placeholders in a Word document, filled by docxtpl: fields, loops and conditions.

The template's code runs in Jinja's sandbox, which keeps it from Python's internals, with every value escaped as XML
text, which the template cannot turn off: a value that holds ``</w:t>`` is shown as written, never read as
WordprocessingML. A name the template uses that is given no value renders empty, and stays in the answer's
``unfilled``. The fields are the names the code of the body, the headers and the footers uses and does not set
itself, without Jinja's own (``range``, ``loop``); a name a ``for`` loop runs over takes a table.
"""

import re
from collections.abc import Iterator, Mapping
from typing import Any, Final

from docxtpl import DocxTemplate
from jinja2 import ChainableUndefined, TemplateError, TemplateSyntaxError, meta, nodes
from jinja2.sandbox import SandboxedEnvironment
from markupsafe import escape

from workbench.executor.templates import Field, Refusal, safe_text
from workbench.executor.templates.docx_controls import paragraph_text, stories

_P: Final = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"

#: The tags of Jinja code in a paragraph's text, and the names in them.
_CODE: Final = re.compile(r"\{\{.*?\}\}|\{%.*?%\}")
_NAME: Final = re.compile(r"[^\W\d]\w*")
_TAG: Final = re.compile(r"\{[{%#]")
_LINE_SHOWN: Final = 80


def environment() -> SandboxedEnvironment:
    """Make the environment every template is parsed and rendered in.

    Every value is escaped as it is written out (``finalize``), whatever the template says, and the ``safe`` filter
    is not there: neither ``|safe`` nor ``{% autoescape false %}`` lets a value through raw. A template that uses
    ``|safe`` does not compile.

    Returns:
        A sandbox that escapes every value, where a missing name, or a name below it, is empty.
    """
    env = SandboxedEnvironment(autoescape=True, undefined=ChainableUndefined, finalize=escape)
    del env.filters["safe"]
    return env


def _code(template: DocxTemplate) -> Iterator[tuple[str, str]]:
    """Give the code of the body, the headers and the footers as docxtpl renders it, a paragraph a line.

    Args:
        template: The template, with its document open.

    Yields:
        Each part's name, as :func:`~.docx_controls.stories` names it, and its code.
    """
    document: Any = template.docx
    named = {story.partname: story.name for story in stories(document) if story.partname}
    yield "body", _lines(template.patch_xml(template.get_xml()))
    for uri in (template.HEADER_URI, template.FOOTER_URI):
        for _, part in template.get_headers_footers(uri):
            name = named.get(str(part.partname), "header" if uri == template.HEADER_URI else "footer")
            yield name, _lines(template.patch_xml(template.get_part_xml(part)))


def _lines(xml: str) -> str:
    """Start every paragraph on a line of its own, as docxtpl does before it renders.

    Args:
        xml: A part's XML, patched.

    Returns:
        The XML with a newline before each paragraph.
    """
    return re.sub(r"<w:p([ >])", r"\n<w:p\1", xml)


def has_code(template: DocxTemplate) -> bool:
    """Tell whether a template holds any ``{{ }}``, ``{% %}`` or ``{# #}``.

    Args:
        template: The template, with its document open.

    Returns:
        Whether the body, a header or a footer does.
    """
    return any(_TAG.search(code) for _, code in _code(template))


def parse(template: DocxTemplate, env: SandboxedEnvironment) -> list[tuple[str, nodes.Template]]:
    """Parse the code of the body, the headers and the footers.

    Args:
        template: The template, with its document open.
        env: The environment.

    Returns:
        Each part's name and its code, parsed.

    Raises:
        Refusal: The code of a part does not compile (the message names the part and the line, and quotes it), or
            is nested deeper than the parser goes, or cannot be read otherwise.
    """
    parsed: list[tuple[str, nodes.Template]] = []
    for name, code in _code(template):
        try:
            parsed.append((name, env.parse(code)))
        except MemoryError:
            raise
        except RecursionError:
            raise Refusal("invalid_template", f"the {{{{ }}}} code of the {name} is nested too deeply") from None
        except TemplateSyntaxError as exc:
            line = exc.lineno or 1
            lines = code.splitlines()
            shown = re.sub(r"<[^>]+>", "", lines[line - 1]) if 0 < line <= len(lines) else ""
            shown = shown if len(shown) <= _LINE_SHOWN else shown[: _LINE_SHOWN - 1] + "…"
            raise Refusal(
                "invalid_template",
                f"the {{{{ }}}} code of the {name} does not compile at line {line}, which reads {shown!r}: "
                f"{exc.message}",
            ) from None
        except Exception as exc:
            raise Refusal(
                "invalid_template", f"the {{{{ }}}} code of the {name} cannot be read ({type(exc).__name__})"
            ) from None
    return parsed


def _names(story: str, tree: nodes.Template) -> tuple[set[str], set[str]]:
    """Find the names a part's code uses without setting them, and those a ``for`` loop runs over.

    Args:
        story: The part's name.
        tree: Its code, parsed.

    Returns:
        The names it does not set, and the names a loop runs over.

    Raises:
        Refusal: The code is nested too deeply to walk, or uses a filter or a test there is not (``|safe``).
    """
    try:
        loops = {loop.iter.name for loop in tree.find_all(nodes.For) if isinstance(loop.iter, nodes.Name)}
        return set(meta.find_undeclared_variables(tree)), loops
    except RecursionError:
        raise Refusal("invalid_template", f"the {{{{ }}}} code of the {story} is nested too deeply") from None
    except TemplateSyntaxError as exc:
        raise Refusal(
            "invalid_template",
            f"the {{{{ }}}} code of the {story} does not compile at line {exc.lineno}: {exc.message}",
        ) from None


def fields(template: DocxTemplate, env: SandboxedEnvironment) -> list[Field]:
    """List the places of the names the template's code uses and does not set itself.

    Args:
        template: The template, with its document open.
        env: The environment.

    Returns:
        A field for each paragraph or table row a name is used in, in document order (:func:`~.merge` lists each
        name once); a name whose code spans paragraphs is placed in its part.

    Raises:
        Refusal: The code does not compile.
    """
    names: dict[str, str] = {}
    tables: set[str] = set()
    for story, tree in parse(template, env):
        undeclared, loops = _names(story, tree)
        tables |= loops
        for name in sorted(undeclared):
            if name not in env.globals:
                names.setdefault(name, story)
    found: list[Field] = []
    seen: set[tuple[str, str]] = set()
    document: Any = template.docx
    for story in stories(document):
        for paragraph in story.root.iter(_P):
            words = [word for code in _CODE.findall(paragraph_text(paragraph)) for word in _NAME.findall(code)]
            for name in dict.fromkeys(word for word in words if word in names):
                place = story.place(paragraph)
                if (name, place) not in seen:
                    seen.add((name, place))
                    found.append(Field(name, "placeholder", "table" if name in tables else "text", None, place))
    located = {f.name for f in found}
    for name, story in names.items():
        if name not in located:
            found.append(Field(name, "placeholder", "table" if name in tables else "text", None, story))
    return found


def _safe(name: str, value: object, warnings: list[str]) -> object:
    """Make every string of a value writable, with a warning that names the field if a character had to go.

    Args:
        name: The field.
        value: Its value.
        warnings: Where the warning goes.

    Returns:
        The value, its strings without characters XML 1.0 cannot hold.
    """
    if isinstance(value, str):
        return safe_text(name, value.replace("\r\n", "\n").replace("\r", "\n"), warnings)
    if isinstance(value, list):
        return [_safe(name, item, warnings) for item in value]
    if isinstance(value, dict):
        return {key: _safe(name, item, warnings) for key, item in value.items()}
    return value


def render(
    template: DocxTemplate, values: Mapping[str, object], env: SandboxedEnvironment, warnings: list[str]
) -> None:
    """Render the template's code with the values.

    Args:
        template: The template, with its document open; its document becomes the rendered one.
        values: The values by name.
        env: The environment.
        warnings: Where warnings go.

    Raises:
        Refusal: The code does not compile, or rendering it failed: the sandbox refused something, or the code
            raised.
    """
    parse(template, env)
    context: dict[str, Any] = {name: _safe(name, value, warnings) for name, value in values.items()}
    try:
        template.render(context, env)
    except TemplateError as exc:
        raise Refusal("invalid_template", f"the {{{{ }}}} code could not be filled: {exc.message}") from None
    except MemoryError:
        raise
    except Exception as exc:
        raise Refusal("invalid_template", f"the {{{{ }}}} code could not be filled ({type(exc).__name__})") from None
