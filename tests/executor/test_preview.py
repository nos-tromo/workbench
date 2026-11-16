"""The table preview a job reads of its csv and xlsx outputs: the header and five rows, cut plainly."""

import csv
import time
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from openpyxl import Workbook

from tests.conftest import scaled
from workbench.executor import preview
from workbench.executor.preview import (
    PREVIEW_COUNT_MAX,
    PREVIEW_SUFFIXES,
    PREVIEW_TOTAL_S,
    PREVIEWS_FILE,
    previews_of,
    table_preview,
)
from workbench.protocol import TablePreview


def _soon() -> float:
    """A deadline no preview here reaches."""
    return time.monotonic() + scaled(30)


def _xlsx(path: Path, sheets: dict[str, list[list[Any]]]) -> Path:
    """Write a workbook of these sheets, in order, each a list of rows."""
    book = Workbook()
    for index, (name, rows) in enumerate(sheets.items()):
        sheet = book.worksheets[0] if index == 0 else book.create_sheet()
        sheet.title = name
        for row in rows:
            sheet.append(row)
    book.save(path)
    return path


def _table(path: Path, rows: list[list[Any]]) -> Path:
    """Write a table as csv or, by the suffix, as a workbook of one sheet."""
    if path.suffix == ".xlsx":
        return _xlsx(path, {"Sheet1": rows})
    with path.open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream).writerows(rows)
    return path


def test_the_preview_bounds() -> None:
    """Two kinds of file, 2 s for a job's previews, a count of at most 100 000 rows, one file beside the result."""
    assert PREVIEW_SUFFIXES == (".csv", ".xlsx")
    assert PREVIEW_TOTAL_S == 2.0
    assert PREVIEW_COUNT_MAX == 100_000
    assert PREVIEWS_FILE == "previews.json"


def test_a_csv_preview_is_its_header_and_five_rows(tmp_path: Path) -> None:
    """The first record is the header, the next five are the rows, and every data row is counted."""
    path = tmp_path / "counts.csv"
    path.write_text("author,posts\n" + "".join(f"user-{n},{n}\n" for n in range(1, 8)), encoding="utf-8")

    assert table_preview(path, _soon()) == TablePreview(
        columns=["author", "posts"],
        rows=[[f"user-{n}", str(n)] for n in range(1, 6)],
        row_count=7,
        truncated=True,
    )


@pytest.mark.parametrize("suffix", [".csv", ".xlsx"])
def test_a_short_table_comes_whole(suffix: str, tmp_path: Path) -> None:
    """Three rows are all shown and counted, and nothing is cut."""
    rows = [["author", "posts"], *([f"user-{n}", n] for n in range(1, 4))]
    path = _table(tmp_path / f"counts{suffix}", rows)

    assert table_preview(path, _soon()) == TablePreview(
        columns=["author", "posts"],
        rows=[[f"user-{n}", str(n)] for n in range(1, 4)],
        row_count=3,
        truncated=False,
    )


@pytest.mark.parametrize("suffix", [".csv", ".xlsx"])
def test_columns_stop_at_20_and_cells_at_100(suffix: str, tmp_path: Path) -> None:
    """A wide row and a long cell are cut plainly, with no "…", alike in both formats; at the limits nothing is."""
    header = [f"column-{n}" for n in range(1, 26)]
    wide = _table(tmp_path / f"wide{suffix}", [header, ["x" * 150, *(f"v{n}" for n in range(2, 26))]])

    got = table_preview(wide, _soon())

    assert got is not None
    assert got.columns == header[:20]
    assert all(len(row) <= 20 for row in got.rows)
    assert got.rows[0][0] == "x" * 100
    assert got.truncated is True

    exact = _table(tmp_path / f"exact{suffix}", [header[:20], ["y" * 100, *(f"v{n}" for n in range(2, 21))]])

    assert table_preview(exact, _soon()) == TablePreview(
        columns=header[:20], rows=[["y" * 100, *(f"v{n}" for n in range(2, 21))]], row_count=1, truncated=False
    )


def test_a_semicolon_csv_with_a_bom_and_a_latin1_one_preview(tmp_path: Path) -> None:
    """CSV as German Excel writes it: semicolons and a BOM, or tabs, or Latin-1, whose byte becomes U+FFFD."""
    semicolons = tmp_path / "semicolons.csv"
    semicolons.write_bytes(b"\xef\xbb\xbfname;city\nM\xc3\xbcller;Kiel\n")
    tabs = tmp_path / "tabs.csv"
    tabs.write_bytes(b"name\tcity\nM\xc3\xbcller\tKiel\n")
    latin1 = tmp_path / "latin1.csv"
    latin1.write_bytes(b"name,city\nM\xfcller,Kiel\n")

    whole = TablePreview(columns=["name", "city"], rows=[["Müller", "Kiel"]], row_count=1, truncated=False)
    assert table_preview(semicolons, _soon()) == whole
    assert table_preview(tabs, _soon()) == whole
    assert table_preview(latin1, _soon()) == whole.model_copy(update={"rows": [["M�ller", "Kiel"]]})


def test_an_xlsx_preview_reads_the_first_sheet(tmp_path: Path) -> None:
    """Only the first sheet, its cells as the sheet reader shows them; a formula is shown as written, never run."""
    path = _xlsx(
        tmp_path / "quarterly-figures.xlsx",
        {
            "Figures": [
                ["quarter", "total", "day", "double"],
                ["Q1", 12.5, date(2026, 11, 16), "=B2*2"],
                ["Q2", 3, date(2026, 11, 17), "=B3*2"],
            ],
            "Other": [["elsewhere"], ["not shown"]],
        },
    )

    assert table_preview(path, _soon()) == TablePreview(
        columns=["quarter", "total", "day", "double"],
        rows=[["Q1", "12.5", "2026-11-16", "=B2*2"], ["Q2", "3", "2026-11-17", "=B3*2"]],
        row_count=2,
        truncated=False,
    )


def test_an_empty_first_sheet_is_an_empty_preview(tmp_path: Path) -> None:
    """The next sheet's rows are never taken for the first sheet's."""
    path = _xlsx(tmp_path / "t.xlsx", {"Empty": [], "Other": [["elsewhere"], ["not shown"]]})

    assert table_preview(path, _soon()) == TablePreview(columns=[], rows=[], row_count=0, truncated=False)


def test_an_empty_csv(tmp_path: Path) -> None:
    """A file with nothing in it is a preview with nothing in it, counted as whole."""
    path = tmp_path / "t.csv"
    path.write_bytes(b"")

    assert table_preview(path, _soon()) == TablePreview(columns=[], rows=[], row_count=0, truncated=False)


def test_an_unreadable_xlsx_has_no_preview(tmp_path: Path) -> None:
    """A file that is not the workbook its name says has no preview, and raises nothing."""
    path = tmp_path / "t.xlsx"
    path.write_bytes(b"PK\x03\x04garbage")

    assert table_preview(path, _soon()) is None


def test_counting_that_runs_out_of_time_keeps_the_rows_with_no_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deadline passes while the rest is counted: the header and five rows stay, the count is open."""
    path = tmp_path / "t.csv"
    path.write_text("author,posts\n" + "".join(f"user-{n},{n}\n" for n in range(1, 200_001)), encoding="utf-8")
    monkeypatch.setattr(preview, "PREVIEW_COUNT_MAX", 1_000_000)  # so that only the deadline can stop the count
    real = csv.reader

    def slow(*args: Any, **kwargs: Any) -> Iterator[list[str]]:
        """Read as csv does, but stall in the middle of the count, past any deadline."""
        for number, record in enumerate(real(*args, **kwargs)):
            if number == 100:
                time.sleep(scaled(30))
            yield record

    monkeypatch.setattr(csv, "reader", slow)
    started = time.monotonic()

    got = table_preview(path, time.monotonic() + scaled(1))

    assert got == TablePreview(
        columns=["author", "posts"],
        rows=[[f"user-{n}", str(n)] for n in range(1, 6)],
        row_count=None,
        truncated=True,
    )
    assert time.monotonic() - started < scaled(10)


def test_a_count_past_its_cap_is_left_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rows are counted up to the cap: the cap itself is a count, one row past it is none."""
    monkeypatch.setattr(preview, "PREVIEW_COUNT_MAX", 10)
    at_cap = tmp_path / "ten.csv"
    at_cap.write_text("n\n" + "".join(f"{n}\n" for n in range(10)), encoding="utf-8")
    past = tmp_path / "eleven.csv"
    past.write_text("n\n" + "".join(f"{n}\n" for n in range(11)), encoding="utf-8")

    counted = table_preview(at_cap, _soon())
    open_ended = table_preview(past, _soon())

    assert counted is not None
    assert (counted.row_count, counted.truncated) == (10, True)
    assert open_ended is not None
    assert (open_ended.rows, open_ended.row_count, open_ended.truncated) == (counted.rows, None, True)


def test_previews_of_reads_csv_and_xlsx_directly_in_out_only(tmp_path: Path) -> None:
    """Regular files directly in ``out/``, by suffix in any case; folders, links and other kinds are passed over."""
    out = tmp_path / "out"
    (out / "sub").mkdir(parents=True)
    _table(out / "a.csv", [["author", "posts"], ["user-a", 12]])
    _xlsx(out / "B.XLSX", {"Sheet1": [["author", "posts"], ["user-b", 3]]})
    (out / "c.docx").write_bytes(b"PK\x03\x04not a table")
    (out / "chart.png").write_bytes(b"\x89PNG\r\n")
    _table(out / "sub" / "d.csv", [["author"], ["user-d"]])
    (out / "e.csv").symlink_to("a.csv")

    got = previews_of(out, _soon())

    assert sorted(got) == ["B.XLSX", "a.csv"]
    assert got["a.csv"].rows == [["user-a", "12"]]
    assert got["B.XLSX"].rows == [["user-b", "3"]]


def test_previews_stop_at_the_deadline(tmp_path: Path) -> None:
    """A deadline already past gives no preview and raises nothing."""
    out = tmp_path / "out"
    out.mkdir()
    _table(out / "a.csv", [["author", "posts"], ["user-a", 12]])

    assert previews_of(out, time.monotonic() - 1) == {}
    assert table_preview(out / "a.csv", time.monotonic() - 1) is None
