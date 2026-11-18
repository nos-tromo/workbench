"""The bounds of a deck: one rule a test, each message naming its slide and block, counted from 1."""

from typing import Any

import pytest

from workbench import decks


def _slide(**fields: Any) -> dict[str, Any]:
    """Build a slide on layout 1, with the given fields on top.

    Args:
        **fields: What the slide holds besides its layout.

    Returns:
        The slide as the api dumps it.
    """
    return {"layout": 1, **fields}


def _block(block: object) -> str | None:
    """Judge one block, alone on the first slide.

    Args:
        block: The block.

    Returns:
        What :func:`decks.deck_problem` says.
    """
    return decks.deck_problem([_slide(content=[block])])


def _chart(chart_type: object = "column", categories: object = None, series: object = None) -> dict[str, Any]:
    """Build a chart block of two categories and one series, unless told otherwise.

    Args:
        chart_type: The chart's type.
        categories: The categories, or ``None`` for two.
        series: The series, or ``None`` for one of two values.

    Returns:
        The block.
    """
    return {
        "chart": {
            "type": chart_type,
            "categories": ["North", "South"] if categories is None else categories,
            "series": {"Sales": [1, 2]} if series is None else series,
        }
    }


def test_the_bounds_are_the_specs() -> None:
    """The constants are the numbers of the spec, in the units the spec uses."""
    assert (decks.MAX_SLIDES, decks.MAX_BLOCKS, decks.MAX_SOURCES, decks.MAX_LAYOUTS) == (30, 6, 20, 100)
    assert (decks.MAX_TEXT, decks.MAX_TITLE, decks.MAX_SUBTITLE, decks.MAX_NOTES) == (2000, 300, 500, 5000)
    assert (decks.MAX_LAYOUT_NAME, decks.MAX_TABLE_ROWS, decks.MAX_TABLE_COLUMNS) == (100, 30, 10)
    assert (decks.MAX_TABLE_CELL, decks.MAX_CATEGORIES, decks.MAX_CATEGORY, decks.MAX_SERIES) == (500, 50, 100, 10)
    assert (decks.MAX_IMAGES, decks.MAX_IMAGE_PIXELS) == (20, 40_000_000)
    assert (decks.MAX_IMAGE_BYTES, decks.MAX_IMAGES_BYTES) == (10 * 1024 * 1024, 30 * 1024 * 1024)
    assert decks.CHART_TYPES == ("bar", "column", "line", "pie")
    assert decks.IMAGE_TYPES == {".png", ".jpg", ".jpeg", ".gif"}


# --- the deck and its slides


def test_a_deck_has_one_to_thirty_slides() -> None:
    """No slide, or thirty-one, or no list at all, is refused; one and thirty pass."""
    why = "a deck has 1 to 30 slides"
    assert decks.deck_problem([]) == why
    assert decks.deck_problem([_slide()] * 31) == why
    assert decks.deck_problem("slides") == why
    assert decks.deck_problem(None) == why
    assert decks.deck_problem([_slide()]) is None
    assert decks.deck_problem([_slide()] * 30) is None


def test_the_slide_is_named_from_one() -> None:
    """A problem on the third slide says ``slide 3``."""
    why = decks.deck_problem([_slide(), _slide(), _slide(title="x" * 301)])
    assert why is not None
    assert why.startswith("slide 3: ")


def test_a_slide_is_an_object() -> None:
    """A slide that is not an object is refused by its number."""
    why = decks.deck_problem([_slide(), "title"])
    assert why is not None
    assert why.startswith("slide 2: ")


def test_a_slide_of_a_layout_alone_passes() -> None:
    """Only the layout is required."""
    assert decks.deck_problem([{"layout": 1}]) is None


@pytest.mark.parametrize("layout", [1, 12, "Title Slide", "x" * 100])
def test_a_layout_is_a_number_or_a_name(layout: object) -> None:
    """A number from 1, or a name of 1 to 100 characters, passes."""
    assert decks.deck_problem([{"layout": layout}]) is None


@pytest.mark.parametrize("layout", [0, -3, "", "x" * 101, True, False, 1.0, None, ["Title"]])
def test_a_layout_that_is_neither_is_refused(layout: object) -> None:
    """Zero, a negative, an empty or too long name, a bool, a float and the rest are refused."""
    why = decks.deck_problem([{"layout": layout}])
    assert why is not None
    assert why.startswith("slide 1: layout ")


def test_a_slide_without_a_layout_is_refused() -> None:
    """The layout is required."""
    why = decks.deck_problem([{"title": "Hello"}])
    assert why is not None
    assert why.startswith("slide 1: layout ")


@pytest.mark.parametrize(
    ("field", "limit"),
    [("title", decks.MAX_TITLE), ("subtitle", decks.MAX_SUBTITLE), ("notes", decks.MAX_NOTES)],
)
def test_a_slide_text_has_its_own_limit(field: str, limit: int) -> None:
    """The limit passes, one character more is refused naming the field, and a non-string is refused."""
    assert decks.deck_problem([_slide(**{field: "x" * limit})]) is None
    assert decks.deck_problem([_slide(**{field: "x" * (limit + 1)})]) == (
        f"slide 1: {field} has at most {limit} characters"
    )
    assert decks.deck_problem([_slide(**{field: 7})]) == f"slide 1: {field} is a string"


def test_a_slide_holds_six_blocks() -> None:
    """Six blocks pass, seven are refused, and content that is not a list is refused."""
    assert decks.deck_problem([_slide(content=["a"] * 6)]) is None
    assert decks.deck_problem([_slide(content=["a"] * 7)]) == "slide 1: at most 6 blocks"
    why = decks.deck_problem([_slide(content="a")])
    assert why is not None
    assert why.startswith("slide 1: ")


def test_a_slide_holds_twenty_sources() -> None:
    """Twenty sources are counted and pass; twenty-one are refused; a non-list is refused."""
    assert decks.deck_problem([_slide(sources=[{"file": "a"}] * 20)]) is None
    assert decks.deck_problem([_slide(sources=[{"file": "a"}] * 21)]) == "slide 1: at most 20 sources"
    why = decks.deck_problem([_slide(sources={"file": "a"})])
    assert why is not None
    assert why.startswith("slide 1: ")


def test_the_first_problem_is_the_one_told() -> None:
    """A slide with two problems tells the first, so the model fixes one at a time."""
    assert decks.deck_problem([_slide(title="x" * 301, notes="y" * 5001)]) == (
        "slide 1: title has at most 300 characters"
    )


# --- blocks


def test_block_kind_tells_the_four_kinds() -> None:
    """A string is text; a dict with one of table, chart, image is that; the rest is none."""
    assert decks.block_kind("x") == "text"
    assert decks.block_kind("") == "text"
    assert decks.block_kind({"table": [["a"]]}) == "table"
    assert decks.block_kind({"chart": {}}) == "chart"
    assert decks.block_kind({"image": "a.png"}) == "image"
    assert decks.block_kind({"image": "a.png", "alt": "A logo"}) == "image"
    assert decks.block_kind(3) is None
    assert decks.block_kind(None) is None
    assert decks.block_kind(["x"]) is None
    assert decks.block_kind({}) is None
    assert decks.block_kind({"table": [["a"]], "chart": {}}) is None
    assert decks.block_kind({"alt": "A logo"}) is None


def test_a_text_block_holds_two_thousand_characters() -> None:
    """The limit passes, one more is refused naming slide and block."""
    assert _block("x" * 2000) is None
    assert decks.deck_problem([_slide(content=["ok", "x" * 2001])]) == (
        "slide 1, block 2: text has at most 2000 characters"
    )


def test_a_block_that_is_nothing_is_refused() -> None:
    """A number, a null and a list are not blocks."""
    for block in (3, None, ["x"]):
        why = _block(block)
        assert why is not None
        assert why.startswith("slide 1, block 1: a block is "), block


def test_a_block_is_one_of_table_chart_image() -> None:
    """A block with two kinds, with none, or with no key at all is refused."""
    no_key: dict[str, Any] = {}
    for block in (
        {"table": [["a"]], "chart": _chart()["chart"]},
        {"image": "a.png", "table": [["a"]]},
        {"alt": "x"},
        no_key,
    ):
        assert _block(block) == "slide 1, block 1: a block has one of table, chart, image", block


def test_a_block_has_no_other_keys() -> None:
    """Besides the kind, an image has ``alt``; nothing else has anything."""
    assert _block({"image": "a.png", "alt": "A logo"}) is None
    for block in ({"table": [["a"]], "alt": "x"}, {"image": "a.png", "caption": "x"}):
        why = _block(block)
        assert why is not None
        assert why.startswith("slide 1, block 1: a block has "), block


def test_a_block_is_named_by_its_place_in_its_slide() -> None:
    """The second block of the fourth slide is ``slide 4, block 2``."""
    slides = [_slide(), _slide(), _slide(), _slide(content=["fine", {"table": []}])]
    why = decks.deck_problem(slides)
    assert why is not None
    assert why.startswith("slide 4, block 2: ")


# --- tables


def test_a_table_has_thirty_rows_with_its_header() -> None:
    """Thirty rows pass; thirty-one are refused; an empty table and a non-list are refused."""
    assert _block({"table": [["a"]] * 30}) is None
    assert _block({"table": [["a"]] * 31}) == "slide 1, block 1: a table has 1 to 30 rows with its header"
    assert _block({"table": []}) == "slide 1, block 1: a table has 1 to 30 rows with its header"
    assert _block({"table": "a"}) == "slide 1, block 1: a table has 1 to 30 rows with its header"


def test_a_table_has_ten_columns() -> None:
    """Ten cells in the header pass; eleven are refused; an empty header is refused."""
    assert _block({"table": [["a"] * 10]}) is None
    assert _block({"table": [["a"] * 11]}) == "slide 1, block 1: a table has 1 to 10 columns"
    assert _block({"table": [[]]}) == "slide 1, block 1: a table has 1 to 10 columns"


def test_a_row_longer_than_the_header_is_refused() -> None:
    """A row may be shorter, filled with empty cells, but not longer than the header."""
    assert _block({"table": [["a", "b", "c"], ["x", "y"]]}) is None
    assert _block({"table": [["a", "b"], ["x", "y"], ["x", "y", "z"]]}) == (
        "slide 1, block 1: row 3 has 3 cells, but the table has 2 columns"
    )


def test_a_row_is_a_list() -> None:
    """A row that is not a list is refused by its number."""
    assert _block({"table": [["a"], "b"]}) == "slide 1, block 1: row 2 is a list of cells"


def test_a_cell_holds_five_hundred_characters() -> None:
    """The limit passes; one more is refused naming row and column."""
    assert _block({"table": [["x" * 500]]}) is None
    assert _block({"table": [["a", "b"], ["c", "x" * 501]]}) == (
        "slide 1, block 1: row 2, column 2: a cell holds at most 500 characters"
    )


def test_a_cell_takes_what_a_spreadsheet_cell_takes() -> None:
    """Numbers, bools and null pass; NaN, an infinity and an object are refused by the shared cell rule."""
    assert _block({"table": [["a", 1, 2.5, True, None]]}) is None
    for cell in (float("nan"), float("inf"), -float("inf"), 10**400, {"a": 1}, ["a"]):
        why = _block({"table": [["a"], [cell]]})
        assert why is not None, cell
        assert why.startswith("slide 1, block 1: row 2, column 1: "), cell


# --- charts


@pytest.mark.parametrize("chart_type", ["bar", "column", "line", "pie"])
def test_every_chart_type_passes(chart_type: str) -> None:
    """The four types are accepted."""
    assert _block(_chart(chart_type)) is None


def test_a_chart_type_is_one_of_four() -> None:
    """``area`` and the like are refused naming the types."""
    why = "slide 1, block 1: a chart's type is bar, column, line or pie"
    for chart_type in ("area", "", "Column", 3):
        assert _block(_chart(chart_type)) == why, chart_type


def test_a_chart_is_an_object() -> None:
    """A chart that is no object is refused; a chart without its parts is refused."""
    assert _block({"chart": "bar"}) == "slide 1, block 1: a chart is an object with type, categories and series"
    assert _block({"chart": {"type": "bar"}}) == "slide 1, block 1: a chart's categories are a list"


def test_a_chart_has_one_to_fifty_categories() -> None:
    """Fifty pass; none and fifty-one are refused."""
    fifty = [f"c{n}" for n in range(50)]
    assert _block(_chart(categories=fifty, series={"S": [1] * 50})) is None
    why = "slide 1, block 1: a chart has 1 to 50 categories"
    assert _block(_chart(categories=[], series={"S": []})) == why
    assert _block(_chart(categories=[*fifty, "x"], series={"S": [1] * 51})) == why
    assert _block(_chart(categories="ab")) == "slide 1, block 1: a chart's categories are a list"


def test_a_category_is_a_short_label_or_a_number() -> None:
    """A string of 100 characters or a number passes; 101 characters, a bool, null and NaN are refused."""
    assert _block(_chart(categories=["x" * 100, 2026], series={"S": [1, 2]})) is None
    assert _block(_chart(categories=["x" * 101, "b"])) == (
        "slide 1, block 1: category 1: a label holds at most 100 characters"
    )
    for category in (True, None, float("nan"), ["a"]):
        why = _block(_chart(categories=["a", category]))
        assert why is not None, category
        assert why.startswith("slide 1, block 1: category 2: "), category


def test_a_chart_has_one_to_ten_series() -> None:
    """Ten pass; none and eleven are refused."""
    ten = {f"s{n}": [1, 2] for n in range(10)}
    assert _block(_chart(series=ten)) is None
    why = "slide 1, block 1: a chart has 1 to 10 series"
    assert _block(_chart(series={})) == why
    assert _block(_chart(series={**ten, "s10": [1, 2]})) == why
    assert _block(_chart(series=[1, 2])) == "slide 1, block 1: a chart's series are an object of name to values"


def test_a_series_name_is_a_short_string() -> None:
    """A name of 100 characters passes; 101, or a non-string, is refused by its number."""
    assert _block(_chart(series={"x" * 100: [1, 2]})) is None
    assert _block(_chart(series={"ok": [1, 2], "x" * 101: [1, 2]})) == (
        "slide 1, block 1: series 2: a name holds at most 100 characters"
    )
    assert _block(_chart(series={3: [1, 2]})) == "slide 1, block 1: series 1: a name is a string"


def test_a_series_has_a_value_per_category() -> None:
    """A series with more or fewer values than categories is refused naming the series."""
    no_values: list[int] = []
    for values in ([1], [1, 2, 3], no_values):
        why = _block(_chart(series={"A": [1, 2], "B": values}))
        assert why is not None, values
        assert why.startswith("slide 1, block 1: series 2 has "), values
        assert "a value per category" in why, values
    why = _block(_chart(series={"A": "12"}))
    assert why is not None
    assert "a value per category" in why


def test_a_chart_value_is_a_finite_number_or_null() -> None:
    """A float, an integer, a negative and null pass; NaN, an infinity, a bool and a string are refused."""
    assert _block(_chart("line", series={"S": [None, 1.5]})) is None
    assert _block(_chart("column", series={"S": [-4, 10**6]})) is None
    for value in (float("nan"), float("inf"), True, "12", [1], 10**400):
        why = _block(_chart("column", series={"S": [1, value]}))
        assert why is not None, value
        assert why.startswith("slide 1, block 1: series 1, value 2: "), value


def test_a_pie_has_one_series_of_values_from_zero() -> None:
    """A pie of one series of non-negative values passes; two series or a negative value is refused."""
    assert _block(_chart("pie", series={"Share": [0, 2.5]})) is None
    assert _block(_chart("pie", series={"A": [1, 2], "B": [3, 4]})) == "slide 1, block 1: a pie chart has one series"
    assert _block(_chart("pie", series={"Share": [3, -1]})) == (
        "slide 1, block 1: series 1, value 2: a pie value is 0 or more"
    )


def test_a_negative_value_passes_outside_a_pie() -> None:
    """Only a pie refuses a negative value."""
    for chart_type in ("bar", "column", "line"):
        assert _block(_chart(chart_type, series={"S": [-1, -2]})) is None


# --- images


def test_an_image_is_a_path_with_an_alt() -> None:
    """A path alone passes; a path with an alt of 300 characters passes."""
    assert _block({"image": "/workspace/chat/logo.png"}) is None
    assert _block({"image": "/workspace/chat/logo.png", "alt": "x" * 300}) is None
    assert _block({"image": "logo.png", "alt": ""}) is None


def test_an_image_path_is_not_empty() -> None:
    """An empty path and a non-string are refused."""
    for path in ("", 3, None, ["a.png"]):
        assert _block({"image": path}) == "slide 1, block 1: an image is the path of a file", path


def test_an_alt_holds_three_hundred_characters() -> None:
    """One more is refused, and so is a non-string."""
    assert _block({"image": "a.png", "alt": "x" * 301}) == "slide 1, block 1: alt has at most 300 characters"
    assert _block({"image": "a.png", "alt": 3}) == "slide 1, block 1: alt is a string"


@pytest.mark.parametrize("path", ["a.png", "A.PNG", "a.jpg", "a.jpeg", "a.JPG", "a.gif", "/workspace/c/pics/a.b.png"])
def test_an_image_is_a_png_jpeg_or_gif(path: str) -> None:
    """The extensions of the three types pass, in any case."""
    assert _block({"image": path}) is None


@pytest.mark.parametrize("path", ["a.svg", "a.bmp", "a.webp", "a.pdf", "a", "a.png.txt", ".png"])
def test_another_image_type_is_refused(path: str) -> None:
    """Another type, or none, is refused."""
    assert _block({"image": path}) == "slide 1, block 1: an image is a PNG, JPEG or GIF file"


def test_a_deck_holds_twenty_images() -> None:
    """Twenty images across the slides pass; the twenty-first is named."""
    image = {"image": "a.png"}
    ten = [_slide(content=[image, image]) for _ in range(10)]
    assert decks.deck_problem(ten) is None
    why = decks.deck_problem([*ten, _slide(content=["text", image])])
    assert why == "slide 11, block 2: a deck holds at most 20 images"


# --- shape of the problem


def test_every_problem_names_its_place_and_holds_no_content() -> None:
    """A message says where and what rule, never the text that broke it."""
    secret = "Quarterly secret plan " * 200
    for slide in (
        _slide(title=secret),
        _slide(content=[secret]),
        _slide(content=[{"table": [[secret]]}]),
        _slide(content=[_chart(categories=[secret, "b"])]),
        _slide(content=[{"image": "a.png", "alt": secret}]),
        _slide(layout=secret),
    ):
        why = decks.deck_problem([slide])
        assert why is not None
        assert "secret" not in why
    why = decks.deck_problem([_slide(content=[_chart(series={secret[:101]: [1, 2]})])])
    assert why is not None
    assert "secret" not in why
