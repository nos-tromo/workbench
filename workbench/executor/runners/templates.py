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

#: Most names a warning or ``unfilled`` lists one by one.
LISTED: Final = 20

_DEFAULT_BUDGET_S: Final = 60.0
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


def _open_xlsx(path: Path, ctx: JobContext) -> Any:
    """Open a workbook template: the zip guard and the workbook counts of ``read_document``, then openpyxl.

    Args:
        path: The ``.xlsx``.
        ctx: The job's surroundings.

    Returns:
        The openpyxl workbook, formulas kept.

    Raises:
        readers.UnreadableFile: A guard refused it, or it is damaged.
    """
    import openpyxl

    from workbench.executor.readers import sheet

    readers.check_zip(path, ctx.file_mb)
    sheet.check_bounds(path)
    return _library(lambda: openpyxl.load_workbook(path, rich_text=False))


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
    """List names for a sentence, at most :data:`LISTED` of them.

    Args:
        names: The names.

    Returns:
        For example ``a, b and 3 more``.
    """
    shown = ", ".join(names[:LISTED])
    return shown + (f" and {len(names) - LISTED} more" if len(names) > LISTED else "")


def _docx_fields(template: Any) -> tuple[list[Field], list[str]]:
    """Find every field of a Word template.

    Args:
        template: The template, open.

    Returns:
        Every place of every field, controls and form fields first, and what the model should know besides.
    """
    from workbench.executor.templates import docx_controls, docx_jinja

    found = docx_controls.find(template.docx)
    places = [spot.field for spot in found.spots]
    if docx_jinja.has_code(template):
        places += docx_jinja.fields(template, docx_jinja.environment())
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

            places = xlsx_placeholders.fields(_open_xlsx(path, ctx))
        fields = merge(places)
        kind = _kind(fields)
        if kind == "none":
            notes.insert(
                0, f"This template has no content controls, no form fields and no {{{{ }}}} placeholders: {NEEDS}."
            )
        room = FIELDS_ROOM * ctx.max_chars
        listed: list[dict[str, Any]] = []
        for field in fields:
            entry = field.as_dict()
            room -= len(json.dumps(entry)) + 2  # and the ", " before the next
            if room < 0:
                break
            listed.append(entry)
        truncated = len(listed) < len(fields)  # the api says how many of how many it lists
        answer: dict[str, Any] = {"template_kind": kind, "fields": listed, "total": len(fields), "truncated": truncated}
        if notes:
            answer["hint"] = " ".join(notes)
        return answer

    return _answered(work, budget)


def _fill_docx(path: Path, ctx: JobContext, values: Mapping[str, object], warned: list[str]) -> tuple[Any, list[str]]:
    """Fill a Word template: its ``{{ }}`` code first, then its content controls and form fields.

    Args:
        path: The template.
        ctx: The job's surroundings.
        values: The values by field name.
        warned: Where warnings go.

    Returns:
        The filled python-docx document, and the template's field names in order.

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
    if docx_jinja.has_code(template):
        _library(lambda: docx_jinja.render(template, values, env, warned))
    document = template.docx
    docx_controls.fill(document, values, warned)
    return document, names


def _fill_xlsx(path: Path, ctx: JobContext, values: Mapping[str, object], warned: list[str]) -> tuple[Any, list[str]]:
    """Fill a workbook template.

    Args:
        path: The template.
        ctx: The job's surroundings.
        values: The values by field name.
        warned: Where warnings go.

    Returns:
        The filled openpyxl workbook, and the template's field names in order.

    Raises:
        Refusal: A value does not fit its field, a table has no room, or there are no fields.
    """
    from workbench.executor.templates import xlsx_placeholders

    book = _open_xlsx(path, ctx)
    names = [field.name for field in xlsx_placeholders.fields(book)]
    if not names:
        raise Refusal("unsupported_template", _NO_FIELDS)
    xlsx_placeholders.fill(book, values, warned)
    if xlsx_placeholders.unkept_parts(path):
        warned.append(
            "The template has charts, pivot tables, pictures or other drawings: the filled workbook may not keep them "
            "as they were, so check it before handing it on."
        )
    book.template = False
    return book, names


def fill_template(args: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
    """Fill a template's fields with the values given, each with its source, into a new file in ``out/``.

    Args:
        args: ``path`` (the template, relative to the chat folder); ``values`` (by field name, each a ``value`` and
            its ``source``; a file source's ``path`` is relative to the chat folder and ``virtual`` is how the
            appendix names it); ``output_name``; ``include_sources``, whether to add the sources appendix; and
            ``budget_s``, the seconds the work may take.
        ctx: The job's surroundings.

    Returns:
        ``filled`` and ``unfilled`` (field names, in the template's order) and ``warnings``; or an ``error`` result.
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
        filled_file, names = (_fill_docx if family == "docx" else _fill_xlsx)(path, ctx, values, warned)
        filled = [name for name in names if name in values]
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
        unfilled = [name for name in names if name not in values]
        if len(unfilled) > LISTED * 10:
            warned.append(f"{len(unfilled) - LISTED * 10} more fields are unfilled than are listed.")
            unfilled = unfilled[: LISTED * 10]
        if len(warned) > LISTED:
            warned[LISTED - 1 :] = [f"{len(warned) - LISTED + 1} more warnings are not shown."]
        return {"filled": filled, "unfilled": unfilled, "warnings": warned}

    return _answered(work, budget)
