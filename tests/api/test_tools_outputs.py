"""The ``write_table`` and ``write_document`` tools through the api.

The job runs the real runner in this process (:class:`~tests.api.test_tools_templates.Local`), so a file the api keeps
is a real one; a test that needs another answer scripts it. The runners themselves are tested in
``tests/executor/test_outputs.py``; here the api's part: what it refuses before any job, the name of the new file, the
answer, the audit event and the caps.
"""

import csv
import dataclasses
import io
import json
from typing import Any

import openpyxl
import pytest
from docx import Document
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.api.conftest import KEY
from tests.api.test_files import CHAT, ME, AuditLines, H, put
from tests.api.test_tools_templates import Local
from workbench.api.audit import AuditLog
from workbench.api.jobs import JobQueue
from workbench.api.main import create_app
from workbench.api.metrics import Metrics
from workbench.api.openapi import TOOL_TAG
from workbench.api.store import Store
from workbench.config import Settings
from workbench.models import FileRecord

COLUMNS = ["item", "total"]
ROWS: list[list[Any]] = [["Dredging", 118], ["Piers", 351.5]]


@pytest.fixture
def fake_executor(settings: Settings, tmp_path: Any) -> Local:
    """The in-process executor."""
    return Local(settings.data_dir, tmp_path / "jobs")


@pytest.fixture
def app(settings: Settings, store: Store, audit: AuditLog, fake_executor: Local) -> FastAPI:
    """The real app with a job queue over the in-process executor."""
    queue = JobQueue(settings, fake_executor, store, Metrics(settings.data_dir))
    return create_app(settings, store=store, audit=audit, jobs=queue)


def table(client: TestClient, name: str = "quarterly-figures", fmt: str = "xlsx", **more: Any) -> Any:
    """Call ``write_table`` the way Open WebUI does."""
    body = {"name": name, "format": fmt, "columns": COLUMNS, "rows": ROWS, **more}
    return client.post("/tools/write_table", json=body, headers=H)


def document(client: TestClient, name: str = "report", fmt: str = "docx", markdown: str = "# Report\n") -> Any:
    """Call ``write_document`` the way Open WebUI does."""
    return client.post("/tools/write_document", json={"name": name, "format": fmt, "markdown": markdown}, headers=H)


def events(audit_lines: AuditLines, tool: str) -> list[dict[str, Any]]:
    """The audit events of one tool."""
    return [e for e in audit_lines() if e["operation"] == f"tool.{tool}"]


def outputs(store: Store) -> list[FileRecord]:
    """The files tools made in the chat."""
    return [rec for rec in store.files_in_chat(ME) if rec.kind == "output"]


def content(store: Store, rec: FileRecord) -> bytes:
    """A stored file's bytes."""
    _, stream = store.open_file(ME, rec.virtual_path)
    with stream:
        return stream.read()


# --- the files ----------------------------------------------------------------------------------------------------


def test_a_table_is_written_into_a_new_file_of_the_chat(client: TestClient, store: Store, fake_executor: Local) -> None:
    """The extension is added; the answer names the file by the store's path and counts the rows; no file goes in."""
    r = table(client)
    assert r.status_code == 200, r.text
    assert r.json() == {"path": f"{CHAT}/quarterly-figures.xlsx", "rows": 2}
    (req,) = fake_executor.requests
    assert (req.tool, req.input_files) == ("write_table", [])
    assert req.args == {
        "name": "quarterly-figures.xlsx",
        "format": "xlsx",
        "columns": COLUMNS,
        "rows": ROWS,
        "sheet": "Sheet1",
        "delimiter": ",",
        "budget_s": 90,
    }
    (made,) = outputs(store)
    sheet = openpyxl.load_workbook(io.BytesIO(content(store, made)))["Sheet1"]
    assert [[cell.value for cell in row] for row in sheet.iter_rows()] == [COLUMNS, *ROWS]


def test_a_csv_table_carries_the_runners_hint(client: TestClient, store: Store) -> None:
    """The hint says how many cells were escaped; the file reads back with them."""
    r = table(client, "formulas", "csv", rows=[["=1+1", 2], ["-3", -3]])
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["path"], body["rows"]) == (f"{CHAT}/formulas.csv", 2)
    assert "2 cells" in body["hint"]
    (made,) = outputs(store)
    rows = list(csv.reader(io.StringIO(content(store, made).decode("utf-8-sig"))))
    assert rows == [COLUMNS, ["'=1+1", "2"], ["'-3", "-3"]]


def test_a_document_is_written_from_markdown(client: TestClient, store: Store, fake_executor: Local) -> None:
    """A Word file, and the Markdown itself as given."""
    r = document(client, markdown="# Quarterly report\n\nGrüße.\n")
    assert r.status_code == 200, r.text
    assert r.json() == {"path": f"{CHAT}/report.docx"}
    assert fake_executor.requests[0].args == {
        "name": "report.docx",
        "format": "docx",
        "markdown": "# Quarterly report\n\nGrüße.\n",
        "budget_s": 90,
    }
    (made,) = outputs(store)
    assert Document(io.BytesIO(content(store, made))).paragraphs[0].text == "Quarterly report"
    assert document(client, "notes", "md", "# Notes\r\n").json() == {"path": f"{CHAT}/notes.md"}
    notes = next(rec for rec in outputs(store) if rec.name == "notes.md")
    assert content(store, notes) == b"# Notes\r\n"


@pytest.mark.parametrize(
    ("name", "fmt", "stored"),
    [
        ("report.docx", "docx", "report.docx"),
        ("Report.DOCX", "docx", "Report.DOCX"),
        ("report.txt", "docx", "report.txt.docx"),
        ("figures.xlsx", "csv", "figures.xlsx.csv"),
        ("notes", "md", "notes.md"),
    ],
)
def test_the_extension_is_added_unless_the_name_has_it(client: TestClient, name: str, fmt: str, stored: str) -> None:
    """Any case counts; another extension stays, with the right one after it."""
    r = table(client, name, fmt) if fmt in {"xlsx", "csv"} else document(client, name, fmt)
    assert r.status_code == 200, r.text
    assert r.json()["path"] == f"{CHAT}/{stored}"


def test_clashing_names_get_a_suffix(client: TestClient, store: Store) -> None:
    """The store adds the number; the answer names the file it stored."""
    put(store, "quarterly-figures.xlsx")
    assert table(client).json()["path"] == f"{CHAT}/quarterly-figures (1).xlsx"
    assert table(client).json()["path"] == f"{CHAT}/quarterly-figures (2).xlsx"
    put(store, "report.docx")
    assert document(client).json()["path"] == f"{CHAT}/report (1).docx"


@pytest.mark.parametrize("name", ["../x", "a/b", "x" * 196, "€" * 84, ".xlsx"])
def test_a_name_that_cannot_be_a_file_name_is_a_422_and_nothing_runs(
    client: TestClient, fake_executor: Local, name: str
) -> None:
    """A plain name of at most 200 characters and 255 bytes, its extension included, with something before it."""
    r = table(client, name)
    assert r.status_code == 422, r.text
    assert r.json()["detail"].startswith("name: ")
    assert fake_executor.requests == []


def test_a_name_of_255_bytes_with_its_extension_is_taken(client: TestClient) -> None:
    """83 euro signs are 249 bytes; with .xlsx 254."""
    assert table(client, "€" * 83).json()["path"] == f"{CHAT}/{'€' * 83}.xlsx"


# --- the audit ----------------------------------------------------------------------------------------------------


def test_routes_record_collect_with_the_output_id(client: TestClient, store: Store, audit_lines: AuditLines) -> None:
    """One collect each, by the new file's id; the table's with its rows and columns; never a name or a value."""
    assert table(client).status_code == 200
    assert document(client, markdown="# Invented Harbour\n").status_code == 200
    made = {rec.name: rec for rec in outputs(store)}
    (written,) = events(audit_lines, "write_table")
    assert (written["action"], written["outcome"]) == ("collect", "ok")
    assert written["target"]["ids"] == [made["quarterly-figures.xlsx"].id]
    assert written["target"]["container"] == "chat:chat-1"
    assert written["criteria"] == {"caller": "open-webui", "rows": 2, "columns": 2}
    (doc,) = events(audit_lines, "write_document")
    assert (doc["action"], doc["target"]["ids"]) == ("collect", [made["report.docx"].id])
    assert doc["criteria"] == {"caller": "open-webui"}
    raw = json.dumps(audit_lines())
    for secret in ("quarterly", "report", "Dredging", "item", "Invented Harbour"):
        assert secret not in raw, secret


def test_a_file_whose_making_cannot_be_recorded_is_removed(
    client: TestClient, store: Store, broken_audit: None
) -> None:
    """As with an upload: a file that could not be recorded does not stay."""
    assert (table(client).status_code, document(client).status_code) == (500, 500)
    assert table(client).json() == {"detail": "audit unavailable"}
    assert outputs(store) == []


# --- what is refused before any job -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("columns", "rows", "said"),
    [
        (["a", "b"], [[1, 2], [1, 2, 3]], "row 2 has 3 cells, but the table has 2 columns"),
        (["a", "b"], [[1, "x" * 32_768]], "row 1, column 2: a cell holds at most 32767 characters"),
        (["a", "x" * 32_768], [], "columns, column 2: a cell holds at most 32767 characters"),
    ],
)
def test_a_cell_out_of_bounds_is_a_422_naming_its_place(
    client: TestClient, fake_executor: Local, columns: list[str], rows: list[list[Any]], said: str
) -> None:
    """Rows and columns counted from 1; nothing runs."""
    r = client.post(
        "/tools/write_table", json={"name": "t", "format": "xlsx", "columns": columns, "rows": rows}, headers=H
    )
    assert (r.status_code, r.json()) == (422, {"detail": said})
    assert fake_executor.requests == []


def test_a_csv_may_be_separated_by_a_semicolon(client: TestClient, store: Store, fake_executor: Local) -> None:
    """A semicolon instead of a comma: the byte order mark and the escaping stay."""
    r = table(client, "semicolons", "csv", rows=[["=1+1", 1.5]], delimiter=";")
    assert r.status_code == 200, r.text
    assert fake_executor.requests[0].args["delimiter"] == ";"
    (made,) = outputs(store)
    raw = content(store, made)
    assert raw.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline=""), delimiter=";"))
    assert rows == [COLUMNS, ["'=1+1", "1.5"]]


def test_a_delimiter_given_with_a_workbook_is_not_used(client: TestClient, store: Store) -> None:
    """It applies to csv alone; a workbook is written as without it."""
    assert table(client, delimiter="\t").status_code == 200
    (made,) = outputs(store)
    sheet = openpyxl.load_workbook(io.BytesIO(content(store, made)))["Sheet1"]
    assert [[cell.value for cell in row] for row in sheet.iter_rows()] == [COLUMNS, *ROWS]


@pytest.mark.parametrize("delimiter", ["|", ",,", ""])
def test_a_delimiter_csv_does_not_take_is_a_422(client: TestClient, fake_executor: Local, delimiter: str) -> None:
    """A comma, a semicolon or a tab."""
    assert table(client, fmt="csv", delimiter=delimiter).status_code == 422
    assert fake_executor.requests == []


def test_a_document_with_a_table_wider_than_word_holds_is_a_422(client: TestClient, store: Store) -> None:
    """Word's 63 columns: the answer names the table and points to write_table; no file is made."""
    wide = "|" + "c|" * 64 + "\n|" + "-|" * 64 + "\n"
    r = document(client, markdown=f"Intro.\n\n{wide}")
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail.startswith("invalid_value: markdown: table 1 has 64 columns")
    assert "write_table" in detail
    assert outputs(store) == []


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e400", "1" + "0" * 400, "1.7e308", "-1e308"])
def test_a_number_that_is_not_finite_is_a_422_naming_its_place(
    client: TestClient, fake_executor: Local, number: str
) -> None:
    """NaN and infinities parse but would reach the job as null; an integer too large for a float, or past Excel's."""
    body = f'{{"name": "t", "format": "xlsx", "columns": ["a", "b"], "rows": [[1, 2], [3, {number}]]}}'
    r = client.post("/tools/write_table", content=body.encode(), headers={**H, "Content-Type": "application/json"})
    assert (r.status_code, r.json()) == (
        422,
        {"detail": "row 2, column 2: a number must be finite and no larger in size than 9.99999999999999E+307"},
    )
    assert fake_executor.requests == []


@pytest.mark.parametrize("sheet", ["a[b", "a:b", "a/b", "a\\b", "a?b", "a*b", "'a", "a'", "History", "HISTORY"])
def test_a_sheet_name_excel_refuses_is_a_422(client: TestClient, fake_executor: Local, sheet: str) -> None:
    """Excel's rules for a sheet's name; the answer names the field."""
    r = table(client, sheet=sheet)
    assert r.status_code == 422, r.text
    assert r.json()["detail"].startswith("sheet: ")
    assert fake_executor.requests == []


@pytest.mark.parametrize(
    "body",
    [
        {"name": "t", "format": "xls", "columns": ["a"], "rows": []},
        {"name": "t", "format": "xlsx", "columns": [], "rows": []},
        {"name": "t", "format": "xlsx", "columns": ["a"] * 16_385, "rows": []},
        {"name": "t", "format": "xlsx", "columns": ["a"], "rows": [[1]] * 100_001},
        {"name": "t", "format": "xlsx", "columns": ["a"], "rows": [[{"x": 1}]]},
        {"name": "t", "format": "xlsx", "columns": ["a"], "rows": [], "sheet": ""},
        {"name": "t", "format": "xlsx", "columns": ["a"], "rows": [], "sheet": "x" * 32},
        {"name": "", "format": "xlsx", "columns": ["a"], "rows": []},
        {"name": "x" * 256, "format": "xlsx", "columns": ["a"], "rows": []},
        {"format": "xlsx", "columns": ["a"], "rows": []},
    ],
    ids=[
        "format",
        "no columns",
        "16385 columns",
        "100001 rows",
        "not a value",
        "empty sheet",
        "long sheet",
        "empty name",
        "long name",
        "no name",
    ],
)
def test_a_table_out_of_bounds_is_a_422_and_nothing_runs(
    client: TestClient, fake_executor: Local, body: dict[str, Any]
) -> None:
    """The request's own bounds."""
    assert client.post("/tools/write_table", json=body, headers=H).status_code == 422
    assert fake_executor.requests == []


def test_a_table_of_100000_rows_and_16384_columns_is_within_the_bounds(client: TestClient) -> None:
    """The largest counts pass the api; a table that large goes by run_python, as the description says."""
    assert table(client, "tall", "csv", columns=["a"], rows=[[1]] * 100_000).json()["rows"] == 100_000
    assert table(client, "wide", "csv", columns=["a"] * 16_384, rows=[]).status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {"name": "r", "format": "pdf", "markdown": "x"},
        {"name": "r", "format": "docx", "markdown": "x" * 200_001},
        {"name": "r", "format": "docx"},
        {"name": "r", "format": "docx", "markdown": 1},
    ],
    ids=["format", "200001 characters", "no markdown", "not text"],
)
def test_a_document_out_of_bounds_is_a_422_and_nothing_runs(
    client: TestClient, fake_executor: Local, body: dict[str, Any]
) -> None:
    """At most 200000 characters of Markdown, as docx or md."""
    assert client.post("/tools/write_document", json=body, headers=H).status_code == 422
    assert fake_executor.requests == []


def test_a_request_too_large_for_the_executor_is_a_422(client: TestClient) -> None:
    """The protocol's line bounds the rest; the answer says so."""
    rows = [["x" * 1000, "y"] for _ in range(1100)]
    assert table(client, rows=rows).json() == {"detail": "request too large"}


def test_a_lone_surrogate_in_a_value_is_written_as_a_replacement_character(client: TestClient, store: Store) -> None:
    """The escape of half a pair reaches the file as U+FFFD, in a cell and in a document."""
    body = b'{"name": "s", "format": "csv", "columns": ["a"], "rows": [["x\\ud800y"]]}'
    r = client.post("/tools/write_table", content=body, headers={**H, "Content-Type": "application/json"})
    assert r.status_code == 200, r.text
    (made,) = outputs(store)
    assert "x\ufffdy" in content(store, made).decode("utf-8-sig")


# --- the job's answer ---------------------------------------------------------------------------------------------


def test_an_ok_answer_that_cannot_be_read_keeps_no_output_and_records_nothing(
    client: TestClient, store: Store, fake_executor: Local, audit_lines: AuditLines
) -> None:
    """The model is told the tool failed and never learns of the file, so it does not stay."""
    fake_executor.files = {"quarterly-figures.xlsx": b"PK"}
    fake_executor.payload = {"hint": 1}
    r = table(client)
    assert (r.status_code, r.json()) == (500, {"detail": "the tool returned an answer that could not be read"})
    assert outputs(store) == []
    assert events(audit_lines, "write_table") == []


def test_an_answer_whose_file_was_not_kept_says_so(
    client: TestClient, fake_executor: Local, audit_lines: AuditLines
) -> None:
    """An ok answer with no file kept is not a success, and nothing is recorded."""
    fake_executor.payload = {"hint": None}
    fake_executor.message = "dropped 1 output: not the job's own file"
    r = document(client)
    assert r.status_code == 500
    assert "dropped 1 output" in r.json()["detail"]
    assert events(audit_lines, "write_document") == []


def test_a_long_hint_from_the_runner_is_cut_never_a_500(client: TestClient, fake_executor: Local) -> None:
    """The backstop behind the runner's own bound."""
    fake_executor.files = {"quarterly-figures.xlsx": b"PK"}
    fake_executor.payload = {"hint": "h" * 5000}
    r = table(client)
    assert r.status_code == 200, r.text
    assert r.json()["hint"] == "h" * 2000


def test_a_runner_refusal_is_its_422(client: TestClient, fake_executor: Local) -> None:
    """The runner's code and text, as for every tool."""
    fake_executor.status = "error"
    fake_executor.payload = {"error": "invalid_value: row 1, column 1: a cell takes a string"}
    r = table(client)
    assert (r.status_code, r.json()) == (422, {"detail": "invalid_value: row 1, column 1: a cell takes a string"})
    fake_executor.payload = {"error": "write_timeout: writing the file took longer than 90 s"}
    assert document(client).status_code == 422


def test_a_busy_executor_is_a_503(client: TestClient, fake_executor: Local) -> None:
    """Nothing ran; retry later."""
    fake_executor.status = "busy"
    assert table(client).json() == {"detail": "busy, retry later"}
    assert document(client).status_code == 503


def test_answers_are_cut_at_the_result_limit(
    settings: Settings, store: Store, audit: AuditLog, fake_executor: Local
) -> None:
    """A path and a long hint within 1 KB."""
    limited = dataclasses.replace(settings, result_max_kb=1)
    queue = JobQueue(limited, fake_executor, store, Metrics(settings.data_dir))
    fake_executor.files = {"quarterly-figures.xlsx": b"PK"}
    fake_executor.payload = {"hint": "h" * 2000}
    with TestClient(create_app(limited, store=store, audit=audit, jobs=queue)) as client:
        r = table(client)
    assert r.status_code == 200
    assert len(r.content) <= 1024
    assert r.json()["path"] == f"{CHAT}/quarterly-figures.xlsx"


# --- the schema ---------------------------------------------------------------------------------------------------


def test_the_tools_are_in_the_schema_with_typed_parameters(client: TestClient) -> None:
    """Both carry the tool tag and an operationId; every field of every body has a type and a description."""
    doc = client.get("/openapi.json", headers={"Authorization": f"Bearer {KEY}"}).json()
    schemas = doc["components"]["schemas"]
    for name in ("write_table", "write_document"):
        operation = doc["paths"][f"/tools/{name}"]["post"]
        assert operation["operationId"] == name
        assert operation["tags"] == [TOOL_TAG]
        assert "Args:" not in operation["description"]
    assert "run_python" in doc["paths"]["/tools/write_table"]["post"]["description"]
    for title in ("WriteTableRequest", "WriteDocumentRequest"):
        for field, spec in schemas[title]["properties"].items():
            assert "type" in spec, (title, field)
            assert spec.get("description", "").strip(), (title, field)
    table_schema = schemas["WriteTableRequest"]
    assert table_schema["required"] == ["name", "format", "columns", "rows"]
    assert table_schema["properties"]["name"]["maxLength"] == 255
    assert table_schema["properties"]["rows"]["maxItems"] == 100_000
    assert table_schema["properties"]["columns"]["maxItems"] == 16_384
    assert schemas["WriteDocumentRequest"]["properties"]["markdown"]["maxLength"] == 200_000


def test_the_tools_need_the_callers_key(client: TestClient) -> None:
    """No key, no tool."""
    body: dict[str, Any] = {"name": "t", "format": "csv", "columns": ["a"], "rows": []}
    assert client.post("/tools/write_table", json=body).status_code == 401
    assert client.post("/tools/write_document", json={"name": "r", "format": "md", "markdown": "x"}).status_code == 401


def test_the_runners_mirrored_bounds_are_the_apis() -> None:
    """The runner checks the api's bounds again without importing the api; a change on one side must reach the other."""
    from workbench.api import tools, tools_output
    from workbench.executor.runners import outputs as runner

    assert runner.MAX_MARKDOWN == tools_output.MAX_MARKDOWN
    assert runner.HINT_MAX == tools.MAX_HINT


def test_a_tables_rules_are_one_module_both_sides_call() -> None:
    """The table's bounds and checks are not copied: the api and the runner use the shared module's."""
    from workbench import tables
    from workbench.api import tools_output
    from workbench.executor.runners import outputs as runner

    assert tools_output.table_problem is tables.table_problem
    assert runner.table_problem is tables.table_problem
    assert runner.word_table_problem is tables.word_table_problem
