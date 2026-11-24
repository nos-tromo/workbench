"""E-mail messages (``.eml``): the main headers, the body, and the names of the attachments.

Attachments are listed by name and type, never opened. An HTML-only body is read as the text it shows.
Outlook's ``.msg`` is not supported (its parsers are GPL): the answer says to save it as ``.eml`` or PDF.
"""

from collections.abc import Iterator
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path
from typing import Final

from workbench.executor.readers import DocumentText, Measure, Part
from workbench.executor.readers import html as html_reader
from workbench.executor.readers import paging as pg
from workbench.executor.readers import text as txt

_HEADERS: Final = ("From", "To", "Cc", "Date", "Subject")
_NAME_CAP: Final = 120


def _body_lines(message: EmailMessage) -> list[str]:
    """Give the message's body as lines.

    Args:
        message: The parsed message.

    Returns:
        The plain-text body, or the text of the HTML one; empty if there is none.
    """
    body = message.get_body(preferencelist=("plain", "html"))
    if body is None:
        return []
    try:
        content = str(body.get_content())
    except Exception:
        payload = body.get_payload(decode=True)
        content = txt.decode(payload) if isinstance(payload, bytes) else ""
    if body.get_content_type() == "text/html":
        return html_reader.html_to_lines(content)
    return txt.split_lines(content)


def lines_of(path: Path) -> list[str]:
    """Render a message as lines: headers, a blank line, the body, then the attachments.

    Args:
        path: The ``.eml`` file.

    Returns:
        The lines.
    """
    parsed = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    message = parsed if isinstance(parsed, EmailMessage) else EmailMessage()
    lines: list[str] = []
    for name in _HEADERS:
        try:
            value = " ".join(str(message[name] or "").split())
        except Exception:
            value = ""
        if value:
            lines.append(f"{name}: {value}")
    lines.append("")
    lines.extend(_body_lines(message))
    names: list[str] = []
    for attachment in message.iter_attachments():
        filename = " ".join((attachment.get_filename() or "(unnamed)").split())
        names.append(f"{pg.clip(filename, _NAME_CAP)} ({attachment.get_content_type()})")
    if names:
        lines.extend(["", "Attachments: " + ", ".join(names)])
    return lines


def read(path: Path, *, part: str | None, max_chars: int) -> DocumentText:
    """Read a message from ``part`` on.

    Args:
        path: The ``.eml`` file.
        part: ``None``, ``line N`` or ``lines N-M`` of the rendered message.
        max_chars: The budget in bytes of JSON.

    Returns:
        The lines that fit and where to continue.
    """
    return txt.read_lines("eml", lines_of(path), part=part, max_chars=max_chars)


def measure(path: Path, *, chars_cap: int) -> Measure:
    """Count the lines of the rendered message, the blank one included, and their characters.

    Args:
        path: The ``.eml`` file.
        chars_cap: The most characters to count.

    Returns:
        The measure.
    """
    return txt.measure_lines("eml", lines_of(path), chars_cap=chars_cap)


def units(path: Path) -> Iterator[Part]:
    """Walk the lines of the rendered message.

    Args:
        path: The file.

    Yields:
        Each non-blank line as ``line N``.
    """
    yield from txt.line_units(lines_of(path))
