"""The bounds and rules of a table the output tools write, shared by the api and the executor.

The api checks a ``write_table`` request with these before it sends a job, so a refusal names its place and costs no
job; the runner checks the job's arguments with the same functions, since it trusts nothing it is sent. Neither side
copies the other: both import this module, as both import :func:`workbench.paths.check_filename`. Every message names
the field, or the row and the column counted from 1.
"""

from collections.abc import Sequence
from typing import Final, Literal, get_args

#: Most rows and columns of a table (Excel's columns), and most characters of a cell (Excel's limit).
MAX_ROWS: Final = 100_000
MAX_COLUMNS: Final = 16_384
MAX_CELL: Final = 32_767

#: The largest number Excel holds, either sign.
MAX_NUMBER: Final = 9.99999999999999e307

#: Most characters of a sheet's name, what it may not hold, and the one name Excel keeps for itself.
MAX_SHEET_NAME: Final = 31
SHEET_FORBIDDEN: Final = frozenset("[]:*?/\\")
SHEET_RESERVED: Final = "history"

#: Most columns of a table in a Word document, Word's own limit.
MAX_WORD_COLUMNS: Final = 63

#: What may separate a CSV file's cells: a comma by default (RFC 4180), a semicolon or a tab.
Delimiter = Literal[",", ";", "\t"]
DELIMITERS: Final = get_args(Delimiter)


def sheet_problem(sheet: object) -> str | None:
    """Tell what is wrong with a sheet's name, by Excel's rules.

    Args:
        sheet: The name.

    Returns:
        Why Excel would refuse it, or ``None``.
    """
    if not isinstance(sheet, str):
        return "a sheet's name is a string"
    if not 1 <= len(sheet) <= MAX_SHEET_NAME:
        return f"a sheet's name has 1 to {MAX_SHEET_NAME} characters"
    if SHEET_FORBIDDEN & set(sheet):
        return "a sheet's name holds none of [ ] : * ? / \\"
    if sheet.startswith("'") or sheet.endswith("'"):
        return "a sheet's name does not begin or end with '"
    if sheet.casefold() == SHEET_RESERVED:
        return "History is a name Excel keeps for itself"
    return None


def cell_problem(value: object) -> str | None:
    """Tell what is wrong with a cell.

    Args:
        value: The cell's value.

    Returns:
        Why the file cannot hold it, or ``None``: it is not a string, a number, a bool or ``None``; a string is longer
        than :data:`MAX_CELL`; a number is not finite (NaN, an infinity, an integer too large for a float) or is
        larger in size than :data:`MAX_NUMBER`.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        return f"a cell holds at most {MAX_CELL} characters" if len(value) > MAX_CELL else None
    if isinstance(value, int | float):
        try:
            size = abs(float(value))
        except OverflowError:
            size = float("inf")
        return (
            None if size <= MAX_NUMBER else "a number must be finite and no larger in size than 9.99999999999999E+307"
        )
    return "a cell takes a string, a number, true, false or null"


def table_problem(columns: object, rows: object, sheet: object) -> str | None:
    """Tell what is wrong with a table, naming the first place: the sheet's name, a column, or a row and a column.

    Args:
        columns: The header's names.
        rows: The rows, each a list of values in the columns' order; a shorter row is filled with empty cells.
        sheet: The sheet's name.

    Returns:
        Why the table cannot be written, or ``None``.
    """
    why = sheet_problem(sheet)
    if why is not None:
        return f"sheet: {why}"
    if not isinstance(columns, list) or not 1 <= len(columns) <= MAX_COLUMNS:
        return f"columns: a table has 1 to {MAX_COLUMNS} columns"
    for number, name in enumerate(columns, 1):
        why = cell_problem(name) if isinstance(name, str) else "a column's name is a string"
        if why is not None:
            return f"columns, column {number}: {why}"
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        return f"rows: a table has at most {MAX_ROWS} rows"
    for row_number, row in enumerate(rows, 1):
        if not isinstance(row, list):
            return f"row {row_number}: a row is a list of values"
        if len(row) > len(columns):
            return f"row {row_number} has {len(row)} cells, but the table has {len(columns)} columns"
        for column_number, value in enumerate(row, 1):
            why = cell_problem(value)
            if why is not None:
                return f"row {row_number}, column {column_number}: {why}"
    return None


def word_table_problem(widths: Sequence[int]) -> str | None:
    """Tell whether a document's tables fit in Word, naming the first that does not.

    Args:
        widths: Each table's columns, in the document's order.

    Returns:
        Why the document cannot be written, or ``None``.
    """
    for number, width in enumerate(widths, 1):
        if width > MAX_WORD_COLUMNS:
            return (
                f"markdown: table {number} has {width} columns, and a Word table holds at most {MAX_WORD_COLUMNS}; "
                "write a table that wide with write_table"
            )
    return None
