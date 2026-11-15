"""Report templates, inspected and filled inside the sandboxed job: what the three kinds share.

A template holds *fields*: Word content controls and legacy form fields (:mod:`.docx_controls`), ``{{ }}``
placeholders in a Word document (:mod:`.docx_jinja`) or in a workbook (:mod:`.xlsx_placeholders`). Each is named,
typed and placed, so the model can plan a fill and the user can confirm it; a value that a field cannot take is a
:class:`Refusal` that names the field. Every value comes with its source (:mod:`.sources`).
"""

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Final, Literal

Kind = Literal["content_control", "form_field", "placeholder", "table"]
Type = Literal["text", "rich_text", "date", "dropdown", "checkbox", "table"]

#: What an appendix shows of a list or a table value, at the most, in characters of compact JSON.
SHOWN_MAX: Final = 200

#: A character XML 1.0 cannot hold.
_NOT_XML: Final = re.compile("[^\t\n\r\x20-퟿-�\U00010000-\U0010ffff]")

#: The date format tokens filled as such; any other run of letters keeps a date in ISO form.
_DATE_TOKENS: Final = {"d", "dd", "M", "MM", "yy", "yyyy"}
_ISO_DATE: Final = re.compile(r"\d{4}-\d{2}-\d{2}")


class Refusal(Exception):
    """A template, a value or a source that cannot be used; the job answers ``{"error": "<code>: <message>"}``.

    Attributes:
        code: ``unsupported_template``, ``invalid_template``, ``invalid_value`` or ``invalid_source``.
        message: What the model should know; it names the field, never a path of the job.
    """

    def __init__(self, code: str, message: str) -> None:
        """Keep the code and the message.

        Args:
            code: The short name the api maps to a status.
            message: The text for the model.
        """
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Field:
    """One fillable place of a template, as ``inspect_template`` lists it.

    Attributes:
        name: What a value is given under: a control's tag (else its title), a form field's bookmark name, a
            placeholder's name.
        kind: ``content_control``, ``form_field``, ``placeholder`` or ``table``.
        type: What it takes: ``text`` and ``rich_text`` a string or a number, ``date`` an ISO date, ``dropdown``
            one of ``options``, ``checkbox`` a bool, ``table`` a list of rows.
        options: A drop-down's entries, as shown; ``None`` for any other type.
        location: Where it sits, such as ``body, table 2, row 3`` or ``Summary!B4``.
    """

    name: str
    kind: Kind
    type: Type
    options: list[str] | None
    location: str

    def as_dict(self) -> dict[str, Any]:
        """Write the field as the job answers it.

        Returns:
            Its attributes by name.
        """
        return {
            "name": self.name,
            "kind": self.kind,
            "type": self.type,
            "options": self.options,
            "location": self.location,
        }


def merge(found: list[Field]) -> list[Field]:
    """List each name once, in the order first seen, with how many more places it has.

    Args:
        found: Every place, in document order.

    Returns:
        The first place of each name; its location ends in ``(and N more)`` when the name occurs again.
    """
    first: dict[str, Field] = {}
    more: dict[str, int] = {}
    for field in found:
        if field.name in first:
            more[field.name] = more.get(field.name, 0) + 1
        else:
            first[field.name] = field
    merged: list[Field] = []
    for name, field in first.items():
        extra = more.get(name, 0)
        if extra:
            field = Field(field.name, field.kind, field.type, field.options, f"{field.location} (and {extra} more)")
        merged.append(field)
    return merged


def xml_safe(text: str) -> tuple[str, bool]:
    """Replace the characters XML 1.0 cannot hold (controls, lone surrogates) with U+FFFD.

    Args:
        text: A value's text.

    Returns:
        The text that can be written, and whether anything was replaced.
    """
    safe = _NOT_XML.sub("�", text)
    return safe, safe != text


def safe_text(name: str, text: str, warnings: list[str]) -> str:
    """Make a value's text writable, with a warning that names the field if a character had to go.

    Args:
        name: The field.
        text: Its text.
        warnings: Where the warning goes.

    Returns:
        The text with U+FFFD for each character XML 1.0 cannot hold.
    """
    safe, changed = xml_safe(text)
    if changed:
        warning = f"{name}: characters a document cannot hold were replaced with �"
        if warning not in warnings:
            warnings.append(warning)
    return safe


def as_text(name: str, value: object) -> str:
    """Take a text field's value: a string, or a number written as Python writes it.

    Args:
        name: The field.
        value: The value.

    Returns:
        The text.

    Raises:
        Refusal: It is neither a string nor a number.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    raise Refusal("invalid_value", f"{name} takes text or a number")


def as_date(name: str, value: object) -> date:
    """Take a date field's value: an ISO date.

    Args:
        name: The field.
        value: The value.

    Returns:
        The date.

    Raises:
        Refusal: It is not a date written ``YYYY-MM-DD``.
    """
    if isinstance(value, str) and _ISO_DATE.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise Refusal("invalid_value", f"{name} takes a date written YYYY-MM-DD")


def as_bool(name: str, value: object) -> bool:
    """Take a check box's value: ``true`` or ``false``.

    Args:
        name: The field.
        value: The value.

    Returns:
        Whether it is checked.

    Raises:
        Refusal: It is not a bool.
    """
    if isinstance(value, bool):
        return value
    raise Refusal("invalid_value", f"{name} is a check box and takes true or false")


def as_option(name: str, value: object, options: list[str]) -> str:
    """Take a drop-down's value: one of its entries, exactly as shown.

    Args:
        name: The field.
        value: The value.
        options: Its entries.

    Returns:
        The entry.

    Raises:
        Refusal: It is not one of them; the message lists them.
    """
    if isinstance(value, str) and value in options:
        return value
    listed = ", ".join(json.dumps(option, ensure_ascii=False) for option in options[:20])
    if len(options) > 20:
        listed += f" and {len(options) - 20} more"
    raise Refusal("invalid_value", f"{name} is a drop-down and takes one of: {listed}")


def format_date(day: date, pattern: str) -> str | None:
    """Write a date the way a Word date format says, for the tokens ``d``, ``dd``, ``M``, ``MM``, ``yy``, ``yyyy``.

    Args:
        day: The date.
        pattern: The format, such as ``dd.MM.yyyy``.

    Returns:
        The date as the format writes it, or ``None`` if the format has any other token (a month's name, a
        weekday, a time, quoted text).
    """
    out: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        end = index
        while end < len(pattern) and pattern[end] == char:
            end += 1
        run = pattern[index:end]
        if char.isalpha() or char == "'":
            if run not in _DATE_TOKENS:
                return None
            values = {
                "d": str(day.day),
                "dd": f"{day.day:02d}",
                "M": str(day.month),
                "MM": f"{day.month:02d}",
                "yy": f"{day.year % 100:02d}",
                "yyyy": f"{day.year:04d}",
            }
            out.append(values[run])
        else:
            out.append(run)
        index = end
    return "".join(out)


def shown(value: object) -> str:
    """Show a value in a sources appendix: a string as it is, anything else as compact JSON, cut to 200 characters.

    Args:
        value: The value.

    Returns:
        Its text.
    """
    if isinstance(value, str):
        return value
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= SHOWN_MAX else text[: SHOWN_MAX - 1] + "…"
