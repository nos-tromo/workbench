"""The runners of ``inspect_template`` and ``fill_template``, which open a report template inside the sandboxed job.

A template is a ``.docx`` or ``.dotx`` (content controls, legacy form fields, ``{{ }}`` placeholders) or an
``.xlsx`` (``{{ }}`` cells and ``{{ table:<name> }}`` anchors); nothing else is one, not ``.docm``, ``.xlsm`` or
``.xltx`` either. The files are untrusted and go through the guards of ``read_document`` first: the zip guard for
both kinds, and for a workbook the counts openpyxl would otherwise load in one call that no clock can stop. All of
the work runs under the job's time limit (:mod:`workbench.executor.timer`), so a long library call is interrupted
too; the libraries parse part XML without entities or network.

A fill writes one file into ``out/``: a ``.dotx`` becomes a ``.docx``. Every value is checked in the field's type, and
every source's page, sheet or cell in the job's copy of its file, before anything is written; a value or source that
does not pass is an error that names the field, and no file is made. The answer is the fields filled and those left
unfilled, and warnings: never a path of the job.
"""

import io
import json
import re
import time
import warnings
import zipfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

from workbench import protocol
from workbench.executor import readers
from workbench.executor.context import JobContext
from workbench.executor.runners.documents import error
from workbench.executor.templates import Field, Refusal, merge
from workbench.executor.timer import limit_until
from workbench.paths import PathError, check_filename

#: The template types, by extension.
TEMPLATES: Final = {".docx": "docx", ".dotx": "docx", ".xlsx": "xlsx"}

#: The answer's fields may take this many times ``max_chars`` as the job writes them; the api cuts them to its limit.
FIELDS_ROOM: Final = 3

#: Most names a warning or ``unfilled`` lists one by one, and most characters a list of names in a warning takes.
LISTED: Final = 20
LISTED_ROOM: Final = 400

#: The api's bounds: the longest name a value is given under, warning and location. A field whose name is longer
#: cannot be filled and is not listed; a warning or a location is cut.
MAX_NAME: Final = 255
WARNING_MAX: Final = 2000
LOCATION_MAX: Final = 300

_DEFAULT_BUDGET_S: Final = 60.0

#: What openpyxl warns of when it loads a workbook and will not write back something of it: a part it does not support,
#: a header or footer it cannot parse, a print area or defined names it cannot place.
_LOST: Final = re.compile(
    r"will be (?:removed|lost|discarded|treated as an error|ignored)|is being dropped|cannot be (?:set|located)"
)
_TYPES: Final = "{http://schemas.openxmlformats.org/package/2006/content-types}"
_TEMPLATE_MAIN: Final = "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml"
_DOCUMENT_MAIN: Final = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
_KINDS: Final = {"content_control": "content_controls", "form_field": "form_fields", "placeholder": "placeholders"}

#: What version 1 fills; the answer for a template with no fields says it.
NEEDS: Final = (
    "version 1 cannot fill it. A template needs one of: content controls (plain or rich text, date, drop-down, check "
    "box) or legacy form fields in a Word .docx or .dotx; {{ field }} placeholders in a Word .docx, with loops and "
    "conditions; {{ field }} cells and a {{ table:name }} cell for a table in an Excel .xlsx. Ask the user to add them"
)
_NOT_A_TEMPLATE: Final = "templates are .docx and .dotx Word files and .xlsx workbooks"
_NO_FIELDS: Final = (
    "the template has no content controls, form fields or {{ }} placeholders, so version 1 cannot fill it; "
    "inspect_template says what it needs"
)


def _family(path: Path) -> str:
    """Name a template's type by its extension.

    Args:
        path: The file.

    Returns:
        ``docx`` or ``xlsx``.

    Raises:
        Refusal: It is not a template (``unsupported_template``).
    """
    ext = path.suffix.lower()
    family = TEMPLATES.get(ext)
    if family is None:
        shown = ext or "a file without an extension"
        raise Refusal("unsupported_template", f"{shown} cannot be filled here; {_NOT_A_TEMPLATE}")
    return family


@contextmanager
def _limited(budget: float) -> Iterator[None]:
    """Run the work under the job's time limit, the readers' deadline the same, and the libraries' warnings unshown.

    What openpyxl warns of as it loads a workbook is not lost: :func:`_open_xlsx` records it for the answer.

    Args:
        budget: Seconds the work may take.

    Yields:
        Inside the limit.

    Raises:
        readers.ReadTimeout: The work ran past the budget.
    """
    deadline = time.monotonic() + budget
    readers.set_deadline(deadline)
    try:
        with limit_until(deadline, readers.ReadTimeout), warnings.catch_warnings():
            warnings.simplefilter("ignore")
            yield
    finally:
        readers.set_deadline(None)


def _answered(work: Callable[[], dict[str, Any]], budget: float) -> dict[str, Any]:
    """Run the work, turning what it refuses or cannot read into the answer the api understands.

    Args:
        work: The runner's work.
        budget: Seconds it may take.

    Returns:
        What the work returns, or an ``error`` result.
    """
    try:
        with _limited(budget):
            return work()
    except Refusal as exc:
        return error(exc.code, exc.message)
    except readers.UnreadableFile as exc:
        return error("unreadable_file", str(exc))
    except readers.UnsupportedFormat as exc:
        return error("unsupported_template", f"{exc}; {_NOT_A_TEMPLATE}")
    except readers.ReadTimeout:
        return error("read_timeout", f"the template took longer than {budget:g} s; it is too large or too complex")


def _library(work: Callable[[], Any]) -> Any:
    """Run a library's work on an untrusted file; whatever it fails with is the file's damage.

    Args:
        work: The work.

    Returns:
        What it returns.

    Raises:
        Refusal: The work refused a value or a source.
        readers.UnreadableFile: The library failed, or ran out of memory.
    """
    try:
        return work()
    except (Refusal, readers.UnreadableFile, readers.UnsupportedFormat):
        raise
    except MemoryError:
        raise readers.UnreadableFile(readers.TOO_BIG) from None
    except Exception:
        raise readers.UnreadableFile(readers.DAMAGED) from None


def _as_document(data: bytes) -> bytes:
    """Give a Word package whose main part is a template the content type of a document.

    Args:
        data: The package.

    Returns:
        The package as it is, or a copy whose ``[Content_Types].xml`` names the main part a document.
    """
    from lxml import etree  # loaded here, like every library a runner needs: a job pays only for its own

    from workbench.executor.readers.docx import parser

    with zipfile.ZipFile(io.BytesIO(data)) as source:
        types = etree.fromstring(source.read("[Content_Types].xml"), parser())
        overrides = [o for o in types.iter(f"{_TYPES}Override") if o.get("ContentType") == _TEMPLATE_MAIN]
        if not overrides:
            return data
        for override in overrides:
            override.set("ContentType", _DOCUMENT_MAIN)
        copied = io.BytesIO()
        with zipfile.ZipFile(copied, "w", zipfile.ZIP_DEFLATED) as target:
            for item in source.infolist():
                content = source.read(item.filename)
                if item.filename == "[Content_Types].xml":
                    content = etree.tostring(types, xml_declaration=True, encoding="UTF-8", standalone=True)
                target.writestr(item, content)
        return copied.getvalue()


def _open_docx(path: Path, ctx: JobContext) -> Any:
    """Open a Word template: the zip guard, then the package, as a document even if it is a template.

    Args:
        path: The ``.docx`` or ``.dotx``.
        ctx: The job's surroundings.

    Returns:
        The docxtpl template, with its python-docx document open.

    Raises:
        readers.UnreadableFile: The zip guard refused it, or it is damaged.
        Refusal: It is a Word file of another kind, such as a macro-enabled one.
    """
    from docxtpl import DocxTemplate

    readers.check_zip(path, ctx.file_mb)

    def opened() -> Any:
        template = DocxTemplate(io.BytesIO(_as_document(path.read_bytes())))
        try:
            template.init_docx()
        except ValueError:
            raise Refusal(
                "unsupported_template", f"it is not a Word document or template (macros?); {_NOT_A_TEMPLATE}"
            ) from None
        return template

    return _library(opened)


def _open_xlsx(path: Path, ctx: JobContext) -> tuple[Any, list[str]]:
    """Open a workbook template: the zip guard and the workbook counts of ``read_document``, then openpyxl.

    Args:
        path: The ``.xlsx``.
        ctx: The job's surroundings.

    Returns:
        The openpyxl workbook, formulas kept and rich text flattened, and a warning for each kind of thing the filled
        workbook will not keep: what openpyxl says it removes, and the rich text.

    Raises:
        readers.UnreadableFile: A guard refused it, or it is damaged.
    """
    import openpyxl

    from workbench.executor.readers import sheet
    from workbench.executor.templates import xlsx_placeholders

    readers.check_zip(path, ctx.file_mb)
    sheet.check_bounds(path)

    def opened() -> tuple[Any, list[str]]:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            book = openpyxl.load_workbook(path, rich_text=True)
        said = [str(warning.message) for warning in caught]
        return book, [
            _cut(f"Not kept in the filled workbook: {text}.", WARNING_MAX) for text in said if _LOST.search(text)
        ]

    book, losses = _library(opened)
    flattened = _library(lambda: xlsx_placeholders.flatten_rich_text(book))
    if flattened:
        cells = "1 cell holds" if flattened == 1 else f"{flattened} cells hold"
        losses.append(
            f"{cells} rich text (words in another font or colour within the cell); the filled workbook keeps "
            f"{'its' if flattened == 1 else 'their'} text alone."
        )
    return book, list(dict.fromkeys(losses))


def _input(args: Mapping[str, Any], ctx: JobContext) -> tuple[Path, str]:
    """Find the template among the job's inputs.

    Args:
        args: The runner's arguments; ``path`` is relative to the chat folder.
        ctx: The job's surroundings.

    Returns:
        The job's copy and the virtual path.

    Raises:
        Refusal: It is not there (``file_not_found``).
    """
    name = protocol.check_input_path(str(args["path"]))
    real = ctx.work_dir / name
    virtual = f"/workspace/{ctx.chat_id}/{name}"
    if name.split("/")[0] == "out" or not real.is_file():
        raise Refusal("file_not_found", f"{virtual} is not in this conversation's workspace")
    return real, virtual


def _listed(names: list[str]) -> str:
    """List names for a sentence: at most :data:`LISTED` of them, in at most :data:`LISTED_ROOM` characters.

    Args:
        names: The names.

    Returns:
        For example ``a, b and 3 more``; the first name always, however long.
    """
    shown: list[str] = []
    used = 0
    for name in names[:LISTED]:
        if shown and used + len(name) + 2 > LISTED_ROOM:
            break
        shown.append(name)
        used += len(name) + 2
    more = len(names) - len(shown)
    return ", ".join(shown) + (f" and {more} more" if more else "")


def _nameable(fields: list[Field]) -> tuple[list[Field], str | None]:
    """Leave out the fields whose names are longer than a value's name may be: they cannot be filled.

    Args:
        fields: The template's fields, each name once.

    Returns:
        The others, and a sentence on those left out, if any.
    """
    kept = [field for field in fields if len(field.name) <= MAX_NAME]
    left = len(fields) - len(kept)
    if not left:
        return kept, None
    names = "field has a name" if left == 1 else "fields have names"
    return kept, (
        f"{left} {names} longer than {MAX_NAME} characters, which no value can be given under, and "
        f"{'is' if left == 1 else 'are'} not listed: rename {'it' if left == 1 else 'them'} in the template."
    )


def _cut(text: str, limit: int) -> str:
    """Cut a text to at most ``limit`` characters, marking the cut.

    Args:
        text: The text.
        limit: The most characters.

    Returns:
        The text, or its start and an ellipsis.
    """
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _docx_fields(template: Any) -> tuple[list[Field], list[str]]:
    """Find every field of a Word template.

    Args:
        template: The template, open.

    Returns:
        Every place of every field, controls and form fields first, and what the model should know besides.
    """
    from workbench.executor.templates import docx_controls, docx_jinja

    found = _library(lambda: docx_controls.find(template.docx))
    places = [spot.field for spot in found.spots]
    if _library(lambda: docx_jinja.has_code(template)):
        places += _library(lambda: docx_jinja.fields(template, docx_jinja.environment()))
    notes: list[str] = []
    if found.unnamed_controls:
        count = found.unnamed_controls
        notes.append(
            f"{count} content control{' has' if count == 1 else 's have'} neither a tag nor a title and cannot be "
            "filled: give each a tag in Word (Developer, Properties)."
        )
    if found.unnamed_fields:
        count = found.unnamed_fields
        notes.append(f"{count} form field{' has' if count == 1 else 's have'} no bookmark name and cannot be filled.")
    if found.other:
        count = found.other
        notes.append(
            f"{count} control{' is' if count == 1 else 's are'} of a kind version 1 does not fill (pictures, building "
            "blocks, groups, calculated or current date fields) and not listed."
        )
    return places, notes


def _kind(fields: list[Field]) -> str:
    """Tell what kind of template the fields make.

    Args:
        fields: Its fields.

    Returns:
        ``content_controls``, ``form_fields``, ``placeholders``, ``mixed`` or ``none``.
    """
    kinds = {_KINDS.get(field.kind, "placeholders") for field in fields}
    if not kinds:
        return "none"
    return kinds.pop() if len(kinds) == 1 else "mixed"


def inspect_template(args: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
    """List the fields of a template, with their types, a drop-down's entries and where they sit.

    Args:
        args: ``path``, relative to the chat folder; ``budget_s``, the seconds the work may take.
        ctx: The job's surroundings.

    Returns:
        ``template_kind``, ``fields``, ``total`` (fields there are), ``truncated`` and, when it applies, ``hint``;
        or an ``error`` result.
    """
    budget = float(args.get("budget_s", _DEFAULT_BUDGET_S))

    def work() -> dict[str, Any]:
        path, _ = _input(args, ctx)
        notes: list[str] = []
        if _family(path) == "docx":
            places, notes = _docx_fields(_open_docx(path, ctx))
        else:
            from workbench.executor.templates import xlsx_placeholders

            book, _ = _open_xlsx(path, ctx)
            places = _library(lambda: xlsx_placeholders.fields(book))
        merged = merge(places)
        fields, long_names = _nameable(merged)
        if long_names is not None:
            notes.append(long_names)
        kind = _kind(merged)
        if kind == "none":
            notes.insert(
                0, f"This template has no content controls, no form fields and no {{{{ }}}} placeholders: {NEEDS}."
            )
        room = FIELDS_ROOM * ctx.max_chars
        listed: list[dict[str, Any]] = []
        for field in fields:
            entry = {**field.as_dict(), "location": _cut(field.location, LOCATION_MAX)}
            room -= len(json.dumps(entry)) + 2  # and the ", " before the next
            if room < 0:
                break
            listed.append(entry)
        truncated = len(listed) < len(fields)  # the api says how many of how many it lists
        answer: dict[str, Any] = {"template_kind": kind, "fields": listed, "total": len(fields), "truncated": truncated}
        if notes:
            answer["hint"] = _cut(" ".join(notes), WARNING_MAX)
        return answer

    return _answered(work, budget)


def _fill_docx(
    path: Path, ctx: JobContext, values: Mapping[str, object], warned: list[str]
) -> tuple[Any, list[str], set[str]]:
    """Fill a Word template: its ``{{ }}`` code first, then its content controls and form fields.

    Args:
        path: The template.
        ctx: The job's surroundings.
        values: The values by field name.
        warned: Where warnings go.

    Returns:
        The filled python-docx document, the template's field names in order, and the names given a value that
        could be filled nowhere (each with a warning).

    Raises:
        Refusal: A value does not fit its field, the code does not compile, or there are no fields.
    """
    from workbench.executor.templates import docx_controls, docx_jinja

    template = _open_docx(path, ctx)
    places, _ = _docx_fields(template)
    names = [field.name for field in merge(places)]
    if not names:
        raise Refusal("unsupported_template", _NO_FIELDS)
    env = docx_jinja.environment()
    if _library(lambda: docx_jinja.has_code(template)):
        _library(lambda: docx_jinja.render(template, values, env, warned))
    document = template.docx
    placeholders = {field.name for field in places if field.kind == "placeholder"}
    not_filled = set(_library(lambda: docx_controls.fill(document, values, warned))) - placeholders
    return document, names, not_filled


def _fill_xlsx(
    path: Path, ctx: JobContext, values: Mapping[str, object], warned: list[str]
) -> tuple[Any, list[str], set[str]]:
    """Fill a workbook template.

    Args:
        path: The template.
        ctx: The job's surroundings.
        values: The values by field name.
        warned: Where warnings go.

    Returns:
        The filled openpyxl workbook, the template's field names in order, and no name: a cell is always filled.

    Raises:
        Refusal: A value does not fit its field, a table has no room, or there are no fields.
    """
    from workbench.executor.templates import xlsx_placeholders

    book, losses = _open_xlsx(path, ctx)
    names = [field.name for field in _library(lambda: xlsx_placeholders.fields(book))]
    if not names:
        raise Refusal("unsupported_template", _NO_FIELDS)
    warned.extend(losses)
    _library(lambda: xlsx_placeholders.fill(book, values, warned))
    if xlsx_placeholders.unkept_parts(path):
        warned.append(
            "The template has charts, pivot tables, pictures or other drawings: the filled workbook may not keep them "
            "as they were, so check it before handing it on."
        )
    book.template = False
    return book, names, set()


def fill_template(args: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
    """Fill a template's fields with the values given, each with its source, into a new file in ``out/``.

    Args:
        args: ``path`` (the template, relative to the chat folder); ``values`` (by field name, each a ``value`` and
            its ``source``; a file source's ``path`` is relative to the chat folder and ``virtual`` is how the
            appendix names it); ``output_name``; ``include_sources``, whether to add the sources appendix; and
            ``budget_s``, the seconds the work may take.
        ctx: The job's surroundings.

    Returns:
        ``filled`` and ``unfilled`` (field names, in the template's order; a field given a value it could be filled
        with nowhere is unfilled, with a warning) and ``warnings``; or an ``error`` result.
    """
    from workbench.executor.templates import sources

    budget = float(args.get("budget_s", _DEFAULT_BUDGET_S))

    def work() -> dict[str, Any]:
        path, _ = _input(args, ctx)
        family = _family(path)
        try:
            output = check_filename(str(args["output_name"]))
        except PathError:
            raise Refusal("invalid_value", "the output name is not a plain file name") from None
        entries: Mapping[str, Mapping[str, Any]] = args["values"]
        values = {name: entry.get("value") for name, entry in entries.items()}
        sheets = sources.check(entries, ctx)
        warned: list[str] = []
        filled_file, every, not_filled = (_fill_docx if family == "docx" else _fill_xlsx)(path, ctx, values, warned)
        names = [name for name in every if len(name) <= MAX_NAME]
        if len(names) < len(every):
            left = len(every) - len(names)
            warned.append(
                f"{left} {'field has a name' if left == 1 else 'fields have names'} longer than {MAX_NAME} characters "
                "and cannot be filled."
            )
        filled = [name for name in names if name in values and name not in not_filled]
        known = set(names)
        unused = [name for name in values if name not in known]
        if unused:
            warned.append(
                f"No field of the template is named {_listed(unused)}: "
                f"{'its value was' if len(unused) == 1 else 'their values were'} not used."
            )
        if args.get("include_sources", True):
            table = sources.rows(entries, filled, sheets, warned)
            if family == "docx":
                sources.to_document(filled_file, table)
            else:
                sources.to_workbook(filled_file, table)
        filled_file.save(str(ctx.out_dir / output))
        unfilled = [name for name in names if name not in values or name in not_filled]
        if len(unfilled) > LISTED * 10:
            warned.append(f"{len(unfilled) - LISTED * 10} more fields are unfilled than are listed.")
            unfilled = unfilled[: LISTED * 10]
        if len(warned) > LISTED:
            warned[LISTED - 1 :] = [f"{len(warned) - LISTED + 1} more warnings are not shown."]
        return {"filled": filled, "unfilled": unfilled, "warnings": [_cut(warning, WARNING_MAX) for warning in warned]}

    return _answered(work, budget)
