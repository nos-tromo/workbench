"""The bounds and rules of a slide deck the output tools write, shared by the api and the executor.

The api checks a ``write_presentation`` request with these before it sends a job, so a refusal names its place and costs
no job; the runner checks the job's arguments with the same function, since it trusts nothing it is sent. Neither side
copies the other: both import this module, as both import :mod:`workbench.tables`, whose cell rule a table here uses.
The deck is plain JSON, as the api dumps it. Every message names the slide, or the slide and the block, counted from 1,
and the rule that broke; none repeats what the model wrote.
"""

from pathlib import PurePosixPath
from typing import Final, Literal

from workbench import tables

#: Most slides in a deck, most blocks (one a place of the layout) on a slide, and most sources a slide cites.
MAX_SLIDES: Final = 30
MAX_BLOCKS: Final = 6
MAX_SOURCES: Final = 20

#: Most characters of a text block, of a slide's title, of its subtitle and of its speaker notes.
MAX_TEXT: Final = 2000
MAX_TITLE: Final = 300
MAX_SUBTITLE: Final = 500
MAX_NOTES: Final = 5000

#: Most characters of a layout's name, and most layouts ``inspect_template`` lists.
MAX_LAYOUT_NAME: Final = 100
MAX_LAYOUTS: Final = 100

#: Most rows of a table (the header is the first), most columns, and most characters of a cell.
MAX_TABLE_ROWS: Final = 30
MAX_TABLE_COLUMNS: Final = 10
MAX_TABLE_CELL: Final = 500

#: Most categories of a chart, most characters of a category's label or a series' name, and most series.
MAX_CATEGORIES: Final = 50
MAX_CATEGORY: Final = 100
MAX_SERIES_NAME: Final = 100
MAX_SERIES: Final = 10

#: What a chart may be: a column chart has vertical bars, a bar chart horizontal ones.
CHART_TYPES: Final = ("bar", "column", "line", "pie")

#: Most pictures in a deck, most bytes and pixels of one (40 megapixels), most bytes of all together, and most
#: characters of its alternative text. The bytes and pixels are measured by the executor, which holds the files.
MAX_IMAGES: Final = 20
MAX_IMAGE_BYTES: Final = 10 * 1024 * 1024
MAX_IMAGE_PIXELS: Final = 40_000_000
MAX_IMAGES_BYTES: Final = 30 * 1024 * 1024
MAX_ALT: Final = 300

#: The extensions of the picture types a slide takes: PNG, JPEG and GIF.
IMAGE_TYPES: Final = frozenset({".png", ".jpg", ".jpeg", ".gif"})

BlockKind = Literal["text", "table", "chart", "image"]

#: The keys of a block that is not text; a block has exactly one, and a picture may add ``alt``.
_STRUCTURED: Final[tuple[Literal["table", "chart", "image"], ...]] = ("table", "chart", "image")

#: The optional text fields of a slide, each with its limit, in the order they are checked.
_SLIDE_TEXTS: Final = (("title", MAX_TITLE), ("subtitle", MAX_SUBTITLE), ("notes", MAX_NOTES))

_CHART_TYPES_SAID: Final = f"{', '.join(CHART_TYPES[:-1])} or {CHART_TYPES[-1]}"


def block_kind(block: object) -> BlockKind | None:
    """Tell what a block is.

    Args:
        block: One entry of a slide's content.

    Returns:
        ``"text"`` for a string; ``"table"``, ``"chart"`` or ``"image"`` for an object with exactly one of those keys
        (its other keys are not looked at); ``None`` for anything else.
    """
    if isinstance(block, str):
        return "text"
    if isinstance(block, dict):
        found = [kind for kind in _STRUCTURED if kind in block]
        if len(found) == 1:
            return found[0]
    return None


def deck_problem(slides: object) -> str | None:
    """Tell what is wrong with a deck, naming the first place: a slide, or a block of a slide.

    The sizes of the picture files are not here: the executor, which holds them, measures them against
    :data:`MAX_IMAGE_BYTES`, :data:`MAX_IMAGE_PIXELS` and :data:`MAX_IMAGES_BYTES`. The sources of a slide are only
    counted: what each holds is checked where the sources are read.

    Args:
        slides: The slides, each an object with, optionally, a ``layout`` (a number from 1 or a name), ``title``,
            ``subtitle``, ``notes``, ``content`` (a list of blocks) and ``sources`` (a list).

    Returns:
        Why the deck cannot be written, or ``None``.
    """
    if not isinstance(slides, list) or not 1 <= len(slides) <= MAX_SLIDES:
        return f"a deck has 1 to {MAX_SLIDES} slides"
    images = 0
    for number, slide in enumerate(slides, 1):
        why = _slide_problem(number, slide, images)
        if why is not None:
            return why
        images += sum(1 for block in slide.get("content", []) if block_kind(block) == "image")
    return None


def _slide_problem(number: int, slide: object, images: int) -> str | None:
    """Tell what is wrong with a slide.

    Args:
        number: The slide's place in the deck, from 1.
        slide: The slide.
        images: How many pictures the slides before this one hold.

    Returns:
        Why the slide cannot be written, or ``None``.
    """
    where = f"slide {number}"
    if not isinstance(slide, dict):
        return f"{where}: a slide is an object"
    if slide.get("layout") is not None and not _layout_is_valid(slide["layout"]):
        return f"{where}: layout is a number from 1, or a name of 1 to {MAX_LAYOUT_NAME} characters"
    for field, limit in _SLIDE_TEXTS:
        if field in slide:
            if not isinstance(slide[field], str):
                return f"{where}: {field} is a string"
            if len(slide[field]) > limit:
                return f"{where}: {field} has at most {limit} characters"
    if "sources" in slide:
        if not isinstance(slide["sources"], list):
            return f"{where}: sources are a list"
        if len(slide["sources"]) > MAX_SOURCES:
            return f"{where}: at most {MAX_SOURCES} sources"
    content = slide.get("content", [])
    if not isinstance(content, list):
        return f"{where}: content is a list of blocks"
    if len(content) > MAX_BLOCKS:
        return f"{where}: at most {MAX_BLOCKS} blocks"
    for block_number, block in enumerate(content, 1):
        place = f"{where}, block {block_number}"
        why = _block_problem(block)
        if why is not None:
            return f"{place}: {why}"
        if block_kind(block) == "image":
            images += 1
            if images > MAX_IMAGES:
                return f"{place}: a deck holds at most {MAX_IMAGES} images"
    return None


def _layout_is_valid(layout: object) -> bool:
    """Tell whether a layout is a number from 1 or a name of 1 to :data:`MAX_LAYOUT_NAME` characters.

    Args:
        layout: The slide's ``layout``.

    Returns:
        Whether it is; a bool is not a number.
    """
    if isinstance(layout, bool):
        return False
    if isinstance(layout, int):
        return layout >= 1
    return isinstance(layout, str) and 1 <= len(layout) <= MAX_LAYOUT_NAME


def _block_problem(block: object) -> str | None:
    """Tell what is wrong with a block.

    Args:
        block: One entry of a slide's content.

    Returns:
        Why the block cannot be written, without its place, or ``None``.
    """
    if isinstance(block, str):
        return f"text has at most {MAX_TEXT} characters" if len(block) > MAX_TEXT else None
    if not isinstance(block, dict):
        return "a block is text, a table, a chart or an image"
    kind = block_kind(block)
    if kind is None:
        return "a block has one of table, chart, image"
    if set(block) - {kind, "alt"} or ("alt" in block and kind != "image"):
        return "a block has nothing besides its kind, and alt with an image"
    if kind == "table":
        return _table_problem(block["table"])
    if kind == "chart":
        return _chart_problem(block["chart"])
    return _image_problem(block)


def _table_problem(rows: object) -> str | None:
    """Tell what is wrong with a table: its first row is the header, which sets the columns.

    Args:
        rows: The rows, each a list of cells; a row shorter than the header is filled with empty cells.

    Returns:
        Why the table cannot be written, or ``None``.
    """
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_TABLE_ROWS:
        return f"a table has 1 to {MAX_TABLE_ROWS} rows with its header"
    columns = 0
    for row_number, row in enumerate(rows, 1):
        if not isinstance(row, list):
            return f"row {row_number} is a list of cells"
        if row_number == 1:
            columns = len(row)
            if not 1 <= columns <= MAX_TABLE_COLUMNS:
                return f"a table has 1 to {MAX_TABLE_COLUMNS} columns"
        elif len(row) > columns:
            return f"row {row_number} has {len(row)} cells, but the table has {columns} columns"
        for column_number, cell in enumerate(row, 1):
            if isinstance(cell, str) and len(cell) > MAX_TABLE_CELL:
                why = f"a cell holds at most {MAX_TABLE_CELL} characters"
            else:
                why = tables.cell_problem(cell)
            if why is not None:
                return f"row {row_number}, column {column_number}: {why}"
    return None


def _chart_problem(chart: object) -> str | None:
    """Tell what is wrong with a chart: its type, categories and series, a value per category in each series.

    Args:
        chart: The chart, an object with ``type``, ``categories`` and ``series`` (a name to its values).

    Returns:
        Why the chart cannot be written, or ``None``.
    """
    if not isinstance(chart, dict):
        return "a chart is an object with type, categories and series"
    chart_type = chart.get("type")
    if chart_type not in CHART_TYPES:
        return f"a chart's type is {_CHART_TYPES_SAID}"
    categories = chart.get("categories")
    if not isinstance(categories, list):
        return "a chart's categories are a list"
    if not 1 <= len(categories) <= MAX_CATEGORIES:
        return f"a chart has 1 to {MAX_CATEGORIES} categories"
    for number, category in enumerate(categories, 1):
        why = _category_problem(category)
        if why is not None:
            return f"category {number}: {why}"
    series = chart.get("series")
    if not isinstance(series, dict):
        return "a chart's series are an object of name to values"
    if not 1 <= len(series) <= MAX_SERIES:
        return f"a chart has 1 to {MAX_SERIES} series"
    if chart_type == "pie" and len(series) != 1:
        return "a pie chart has one series"
    for number, (name, values) in enumerate(series.items(), 1):
        if not isinstance(name, str):
            return f"series {number}: a name is a string"
        if len(name) > MAX_SERIES_NAME:
            return f"series {number}: a name holds at most {MAX_SERIES_NAME} characters"
        if not isinstance(values, list):
            return f"series {number} is a list with a value per category"
        if len(values) != len(categories):
            return f"series {number} has {len(values)} values, not a value per category ({len(categories)})"
        for value_number, value in enumerate(values, 1):
            why = _value_problem(value, non_negative=chart_type == "pie")
            if why is not None:
                return f"series {number}, value {value_number}: {why}"
    return None


def _category_problem(category: object) -> str | None:
    """Tell what is wrong with a category: a label of at most :data:`MAX_CATEGORY` characters, or a number.

    Args:
        category: The category.

    Returns:
        Why a chart cannot take it, or ``None``.
    """
    if isinstance(category, str):
        return f"a label holds at most {MAX_CATEGORY} characters" if len(category) > MAX_CATEGORY else None
    if isinstance(category, bool) or not isinstance(category, int | float):
        return "a category is a string or a number"
    return tables.cell_problem(category)


def _value_problem(value: object, *, non_negative: bool) -> str | None:
    """Tell what is wrong with a value of a series: a number under the cell rule of a table, or ``None``.

    Args:
        value: The value; ``None`` is a gap.
        non_negative: Whether a negative number is refused, as in a pie.

    Returns:
        Why a chart cannot take it, or ``None``.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return "a value is a number or null"
    why = tables.cell_problem(value)
    if why is None and non_negative and value < 0:
        return "a pie value is 0 or more"
    return why


def _image_problem(block: dict[str, object]) -> str | None:
    """Tell what is wrong with a picture block: a path of a PNG, JPEG or GIF file, and an optional alternative text.

    Args:
        block: The block, an object with ``image`` and, optionally, ``alt``.

    Returns:
        Why the picture cannot be placed, or ``None``.
    """
    path = block["image"]
    if not isinstance(path, str) or not path:
        return "an image is the path of a file"
    if PurePosixPath(path).suffix.lower() not in IMAGE_TYPES:
        return "an image is a PNG, JPEG or GIF file"
    if "alt" in block:
        if not isinstance(block["alt"], str):
            return "alt is a string"
        if len(block["alt"]) > MAX_ALT:
            return f"alt has at most {MAX_ALT} characters"
    return None
