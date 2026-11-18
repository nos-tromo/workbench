# write_presentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a `.pptx` from a user's slide master and the model's slide list, in workbench's executor, and let Ghostwriter offer it.

**Architecture:** `inspect_template` learns slide masters (layouts and their places); a new workspace tool `write_presentation` validates the slide list in the api (`workbench/decks.py`, shared with the executor), and the executor builds the deck with python-pptx (`workbench/executor/presentations/`). Ghostwriter adds `.potx`/`.dotx` uploads, a trail label and a prompt sentence.

**Tech Stack:** Python 3.12 (workbench), python-pptx 1.0.2, markdown-it-py, xlsxwriter, pydantic, FastAPI; Ghostwriter Python 3.11 + React/vitest.

**Spec:** `docs/superpowers/specs/2026-11-18-write-presentation-design.md`

## Global Constraints

- workbench tasks run in the worktree `.claude/worktrees/write-presentation` (branch `feat/write-presentation`); Ghostwriter tasks in `ghostwriter/` on a branch `feat/presentations`, after workbench is merged.
- Google docstrings, annotations, pyrefly strict, 120-character lines; `make verify` hands ruff tracked files only: `git add` new files first.
- The executor imports nothing of `workbench.api`; shared rules live in `workbench/decks.py` as `tables.py` does.
- Never a file name, slide text, value or layout name in a log line or an audit event; events name files by id, criteria are counts.
- Every test file is built at test time with invented text (`tests/fixtures/`); no binary fixture is committed.
- Every library call on the master runs through `library()` under the job's time limit; a library failure is `unreadable_file`, never a traceback.
- The bounds are the spec's: slides 1–30, 6 blocks a slide, text 2000, title 300, subtitle 500, notes 5000, layout name 100; table 30 rows with the header, 10 columns, 500 characters a cell; chart 1–50 categories (labels ≤ 100), 1–10 series, a pie one series of values ≥ 0; images 20 a deck, 10 MiB and 40 MP each, 30 MiB together, PNG/JPEG/GIF; sources 20 a slide; layouts listed 100.
- Golden files in `tests/api/golden/` are edited by hand, never regenerated.
- Commit messages: conventional (`feat(executor): …`), no real data.

## Review Focus

1. **A master saved by PowerPoint, not by python-pptx**: footers turned off with `<p:hf ftr="0" sldNum="0"/>` on the master or a layout. Expect no footer shape copied where the master hides it. Test in Task 5.
2. **Two layouts with the same name**, as in masters with two designs. Expect a refusal of the name that asks for the number, and the number works. Test in Task 5.
3. **Example slides carrying a chart and a picture**. Expect their parts gone from the saved package, and part names unique. Test in Task 5.
4. **Blocks that do not match the layout's places** (content on "Title Only", an image in a text place). Expect `invalid_value` naming slide and block and listing the layout's places, so the model can choose again. Test in Task 6.
5. **Text with control characters, and one 300-character word**. Expect the characters replaced with a warning, no crash, and the fit estimate counting the long word as several lines. Test in Task 4.

---

### Task 1: OOXML retyping and `.potx` read and stored

**Files:**
- Create: `workbench/executor/readers/ooxml.py`
- Modify: `workbench/executor/runners/templates.py` (`_as_document` → `ooxml.retyped`), `workbench/executor/readers/pptx.py`, `workbench/executor/readers/__init__.py:75-97`, `workbench/executor/templates/sources.py:33` (`_PAGED` gains `.potx`), `workbench/api/store.py:89`, `workbench/api/tool_table.py` (`read_document` description: `PPTX, POTX`), `tests/api/golden/ghostwriter-{modern,legacy}.json`
- Modify: `tests/fixtures/make_templates.py` (add `as_potx`, `as_macro_pptx`)
- Test: `tests/executor/test_readers.py`, `tests/executor/test_templates.py`, `tests/api/test_store.py`

**Interfaces:**
- Produces:
  - `ooxml.retyped(data: bytes, old: str, new: str) -> bytes` returns the package unchanged when no `Override` has `old`.
  - Constants `ooxml.PML_TEMPLATE_MAIN`, `ooxml.PML_PRESENTATION_MAIN`, `ooxml.PML_MACRO_MAIN`, `ooxml.PML_SLIDESHOW_MAIN`, `ooxml.WML_TEMPLATE_MAIN`, `ooxml.WML_DOCUMENT_MAIN`.
  - `ooxml.main_type(data: bytes) -> str | None` gives the content type of `/ppt/presentation.xml` or `/word/document.xml`.
  - `readers.pptx.open_presentation(path: Path) -> Any` retypes a `.potx`; a macro-enabled or slide-show main part raises `readers.UnsupportedFormat("a macro-enabled presentation or a slide show; save it as .pptx or .potx")`.
  - `make_templates.as_potx(pptx: Path, target: Path) -> Path` and `make_templates.as_macro_pptx(pptx: Path, target: Path) -> Path`, in the same shape as `as_dotx`.

- [ ] **Step 1: Write the failing tests**
  - `test_a_potx_is_read_as_its_slides`: `mk.make_pptx` then `mt.as_potx`. `readers.read_document(path)` has kind `pptx` and the same parts as the `.pptx`.
  - `test_a_macro_enabled_presentation_is_refused`: `read_document` on `as_macro_pptx` raises `UnsupportedFormat`.
  - `test_a_dotx_still_fills`: an existing `.dotx` fill test must still pass. Run it; add nothing.
  - `test_office_templates_have_their_types`: `store._mime("deck.potx") == "application/vnd.openxmlformats-officedocument.presentationml.template"` (`workbench/api/store.py:366`) and `_mime("form.dotx") == "application/vnd.openxmlformats-officedocument.wordprocessingml.template"`.
- [ ] **Step 2: Run** `uv run pytest tests/executor/test_readers.py tests/api/test_store.py -q -k "potx or macro or types"`. Expected: FAIL.
- [ ] **Step 3: Implement**
  - `ooxml.py`: move `_as_document`'s body there, parameterised by `old` and `new`, using `readers.docx.parser()`.
  - `_open_docx` calls `retyped(data, WML_TEMPLATE_MAIN, WML_DOCUMENT_MAIN)`.
  - `pptx.read` and `pptx.units` open through `open_presentation`.
  - `_FAMILIES[".potx"] = "pptx"`, and `.potx` joins `_ZIPPED`.
  - `_EXTRA_TYPES` gains `.potx` and `.dotx`.
- [ ] **Step 4: Edit the goldens** by hand: `read_document`'s description in both files.
- [ ] **Step 5: Run** `uv run pytest tests/executor tests/api/test_store.py tests/api/test_tools_golden.py -q`. Expected: PASS.
- [ ] **Step 6: Commit** `feat(readers): read .potx presentations; one OOXML retyping for .dotx and .potx`

### Task 2: Shared deck bounds

**Files:**
- Create: `workbench/decks.py`, `tests/test_decks.py`

**Interfaces:**
- Produces:
  - The constants from Global Constraints, named `MAX_SLIDES`, `MAX_BLOCKS`, `MAX_TEXT`, `MAX_TITLE`, `MAX_SUBTITLE`, `MAX_NOTES`, `MAX_LAYOUT_NAME`, `MAX_TABLE_ROWS`, `MAX_TABLE_COLUMNS`, `MAX_TABLE_CELL`, `MAX_CATEGORIES`, `MAX_CATEGORY`, `MAX_SERIES`, `MAX_IMAGES`, `MAX_IMAGE_BYTES`, `MAX_IMAGE_PIXELS`, `MAX_IMAGES_BYTES`, `MAX_SOURCES`, `MAX_LAYOUTS`.
  - `CHART_TYPES: Final = ("bar", "column", "line", "pie")` and `IMAGE_TYPES: Final = frozenset({".png", ".jpg", ".jpeg", ".gif"})`.
  - `block_kind(block: object) -> Literal["text", "table", "chart", "image"] | None`.
  - `deck_problem(slides: object) -> str | None` works on plain JSON. Messages start with `slide N` or `slide N, block M` (counted from 1).
  - Cells use `tables.cell_problem`. Chart values are numbers under the same rule, or `None`.

- [ ] **Step 1: Write the failing tests** (`tests/test_decks.py`), one per rule, each asserting the exact message start:
  - `deck_problem([]) == "a deck has 1 to 30 slides"`.
  - 31 slides are refused.
  - `deck_problem([{"layout": 1}]) is None`; a layout `0`, `""` or a 101-character name is refused with `slide 1: layout …`.
  - A title of 301 characters is refused; so are a subtitle of 501 and notes of 5001.
  - 7 blocks are refused with `slide 1: at most 6 blocks`.
  - A text block of 2001 characters is refused with `slide 1, block 1: …`.
  - A block `{"table": …, "chart": …}` is refused: one of table, chart, image.
  - Tables: 31 rows refused; a row of 11 cells refused; a cell of 501 characters refused; `float("nan")` refused; an empty table refused.
  - Charts: type `area` refused; 0 and 51 categories refused; 11 series refused; a series of the wrong length refused (`a value per category`); a pie with 2 series refused; a pie value `-1` refused; `None` allowed in a line chart.
  - Images: an empty path refused; an alt of 301 characters refused.
  - 21 sources refused.
  - `block_kind("x") == "text"`, `block_kind({"image": "a.png"}) == "image"`, `block_kind(3) is None`.
- [ ] **Step 2: Run** `uv run pytest tests/test_decks.py -q`. Expected: FAIL (no module).
- [ ] **Step 3: Implement `deck_problem`** as one pass over slides and blocks, returning the first problem. Model it on `tables.table_problem`.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `feat(decks): the bounds of a deck, shared by the api and the executor`

### Task 3: The slide master: layouts, places, capacity, inspect

**Files:**
- Create: `workbench/executor/presentations/__init__.py`, `workbench/executor/presentations/master.py`
- Modify: `workbench/executor/runners/templates.py`
  - Make the helpers public: `_answered` → `answered(work, budget, *, unsupported: str = NOT_A_TEMPLATE)`, `_library` → `library`, `_limited` → `limited`, `_input` → `job_input`, `_cut` → `cut`, `_NOT_A_TEMPLATE` → `NOT_A_TEMPLATE`. Update every call site.
  - `TEMPLATES` gains `.pptx` and `.potx` as `pptx`.
  - `NOT_A_TEMPLATE` becomes "templates are .docx and .dotx Word files, .xlsx workbooks and .pptx and .potx slide masters".
- Modify: `tests/fixtures/make_templates.py`: add `make_master`.
- Test: `tests/executor/test_presentations.py` (new)

**Interfaces:**
- Consumes: `readers.pptx.open_presentation` (Task 1), `decks.MAX_LAYOUTS`, `decks.MAX_LAYOUT_NAME` (Task 2).
- Produces:
  - `@dataclass(frozen=True) class Place: role: Literal["text", "content", "picture", "table", "chart"]; lines: int; chars: int; shape: Any`.
  - `@dataclass(frozen=True) class Layout: number: int; name: str; layout: Any; title: bool; subtitle: bool; places: tuple[Place, ...]; footer: bool`.
  - `open_master(path: Path, ctx: JobContext) -> Any` runs `readers.check_zip`, then `open_presentation` under `library()`. `UnsupportedFormat` becomes `Refusal("unsupported_template", …)`.
  - `layouts(prs: Any) -> list[Layout]` numbers 1..N over `prs.slide_masters`, then each master's `slide_layouts`. Places are in reading order (top, then left).
  - `capacity(shape: Any, layout: Any, master: Any) -> tuple[int, int]` returns (lines, chars). The font size is looked up in this order:
    1. the layout placeholder's `a:lstStyle/a:lvl1pPr/a:defRPr@sz`;
    2. the master placeholder of the same type;
    3. the master's `p:txStyles` (`p:titleStyle` for a title, `p:bodyStyle` otherwise);
    4. 18 pt.
    Then chars = (width − lIns − rIns) / (0.5 × size) and lines = (height − tIns − bIns) / (1.2 × size), with the insets defaulting to 91440 / 45720 EMU. Each is at least 1.
  - `inspect(prs: Any, room: int) -> dict[str, Any]` returns `{"template_kind": "slide_master", "layouts": [entry…], "slides": <existing slides>, "total": <layouts>, "truncated": bool}`. An entry is `{"number", "name" (cut to 100), "title", "subtitle", "places": [{"role", "lines", "chars"}], "footer"}`. Entries stop at `decks.MAX_LAYOUTS` or when `room` JSON characters are used.
  - `make_master(path: Path, *, footer: str = "Synthetic Ltd · internal", examples: int = 2, hidden_footer: bool = False, duplicate_name: bool = False) -> Path` starts from python-pptx's default `Presentation()`:
    - layout 2 is renamed "Titel und Inhalt";
    - the footer text goes into each layout's footer placeholder;
    - `examples` slides are added, the first with notes, a picture (`make_fixtures._png`) and a chart;
    - a `p:custShowLst` naming slide 1 and a `p14:sectionLst` extension are added;
    - `hidden_footer` writes `<p:hf ftr="0" sldNum="0" dt="0"/>` on the master;
    - `duplicate_name` names layouts 3 and 4 "Zwei Inhalte".

- [ ] **Step 1: Write the failing tests** (in `tests/executor/test_presentations.py`; reuse the `Job` fixture shape of `tests/executor/test_templates.py`):
  - `test_inspect_lists_the_layouts_of_a_master`: `answer["template_kind"] == "slide_master"`, `len(answer["layouts"]) == 11`, `answer["slides"] == 2`. Layout 1 has `title` and `subtitle` true, `places == []` and `footer` true. Layout 2 is named "Titel und Inhalt" with one place of role `content`.
  - `test_places_are_in_reading_order`: the "Comparison" layout (number 5) has roles `["text", "text", "content", "content"]`: both headings sit above both contents. This is reading order, not placeholder-number order.
  - `test_capacity_follows_the_inherited_font_size`: the title-and-content place gives `lines` and `chars` within ±1 of the formula. The test reads the size from the fixture's `p:bodyStyle/a:lvl1pPr/a:defRPr@sz` rather than hard-coding it. Setting `sz="2000"` in the layout's `lstStyle` changes the answer to the 20-pt figures. A shape with no size anywhere uses 18 pt.
  - `test_a_potx_is_inspected_like_a_pptx`: same layouts as the `.pptx`.
  - `test_a_macro_enabled_master_is_refused`: `error.code == "unsupported_template"`.
  - `test_a_long_layout_list_is_cut`: with `max_chars` 512, `truncated` is true and `total == 11`.
  - `test_fill_template_refuses_a_slide_master`: `RUNNERS["fill_template"]` on a `.pptx` gives `unsupported_template`, with `write_presentation` in the message.
- [ ] **Step 2: Run** `uv run pytest tests/executor/test_presentations.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement `master.py`** and hook it in:
  - `inspect_template`'s `work()`: for family `pptx`, return `master.inspect(master.open_master(path, ctx), FIELDS_ROOM * ctx.max_chars)`.
  - `fill_template`: family `pptx` raises `Refusal("unsupported_template", "a slide master is filled with write_presentation")`.
  - Role mapping (`pptx.enum.shapes.PP_PLACEHOLDER`):
    - TITLE and CENTER_TITLE set `title`; SUBTITLE sets `subtitle`;
    - BODY → `text`, OBJECT → `content`, PICTURE and BITMAP → `picture`, TABLE → `table`, CHART → `chart`;
    - DATE, FOOTER and SLIDE_NUMBER set `footer`;
    - everything else is skipped.
- [ ] **Step 4: Run** `uv run pytest tests/executor -q`. Expected: PASS, the template tests included.
- [ ] **Step 5: Commit** `feat(executor): inspect a slide master's layouts and places`

### Task 4: Markdown into a text frame

**Files:**
- Create: `workbench/executor/presentations/text.py`
- Test: `tests/executor/test_presentations.py`

**Interfaces:**
- Produces:
  - `write_markdown(frame: Any, markdown: str) -> Written`, where `@dataclass(frozen=True) class Written: lines: tuple[tuple[int, int], ...]; replaced: int`. `lines` holds (level, characters) per paragraph; `replaced` counts characters that `templates.xml_safe` replaced.
  - `estimate_lines(written: Written, chars: int) -> int`: the sum over paragraphs of ceil(characters / (chars − 4 × level)), with at least 1 for each paragraph.

- [ ] **Step 1: Write the failing tests** (on a text frame of a slide added to `Presentation()`):
  - `test_bullets_keep_their_levels`: `"- a\n  - b\n    - c\n      - d"` gives paragraph levels `[0, 1, 2, 2]`.
  - `test_a_plain_paragraph_has_no_bullet`: `"Intro\n\n- a"` gives the first paragraph an `a:pPr/a:buNone` and the second none.
  - `test_a_numbered_list_counts_from_its_start`: `"3. x\n4. y"` gives `a:buAutoNum` with `startAt="3"` on the first item.
  - `test_bold_and_italic_are_runs`: `"**Fix** it *now*"` gives the runs `[("Fix", True, None), (" it ", None, None), ("now", None, True)]`.
  - `test_a_link_is_its_text_and_address`: `"[docs](https://example.invalid)"` gives the text `"docs (https://example.invalid)"`.
  - `test_characters_xml_cannot_hold_are_replaced`: `"a\x01b"` gives `replaced == 1` and the text `"a�b"`.
  - `test_a_long_word_counts_as_several_lines`: one 300-character word with `chars=60` estimates 5 lines.
- [ ] **Step 2: Run** `uv run pytest tests/executor/test_presentations.py -q -k "bullet or paragraph or numbered or bold or link or characters or long_word"`. Expected: FAIL.
- [ ] **Step 3: Implement**
  - Parse with `MarkdownIt("commonmark", {"html": False})`, as `runners/outputs.py:639` does. Walk `bullet_list_open` / `ordered_list_open` depth for the level (clamped to 2) and `inline` children for runs (`strong_open` / `em_open` / `link_open` / `code_inline` / `softbreak`).
  - The first paragraph reuses `frame.paragraphs[0]`; the frame is cleared first.
  - Set `buNone` and `buAutoNum` through lxml on `paragraph._p.get_or_add_pPr()`.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `feat(executor): write Markdown into a slide's text frame`

### Task 5: The deck: slides, text, footers, notes, the runner

**Files:**
- Create: `workbench/executor/presentations/deck.py`, `workbench/executor/runners/presentations.py`
- Modify: `workbench/executor/runners/__init__.py` (`RUNNERS["write_presentation"]`), `workbench/api/jobs.py:233` (`TOOLS` gains it)
- Test: `tests/executor/test_presentations.py`

**Interfaces:**
- Consumes: `master.open_master`, `master.layouts`, `Layout`, `Place` (Task 3); `text.write_markdown`, `text.estimate_lines` (Task 4); `decks.deck_problem` (Task 2); `templates.sources.check` and `templates.sources.reference`; `runners.templates.answered`, `library`, `job_input`, `cut`.
- Produces:
  - `deck.remove_slides(prs: Any) -> None`: drops `p:custShowLst`, the `p14:sectionLst` extension, then every `sldId` and its relationship.
  - `deck.resolve(layouts: list[Layout], wanted: int | str, slide: int) -> Layout` raises `Refusal("invalid_value", …)`:
    - an unknown layout: `"slide 3: no layout 'X'; the layouts are 1 Title Slide, 2 Titel und Inhalt, …"`;
    - a shared name: `"slide 3: two layouts are named 'Zwei Inhalte'; give its number: 3 or 4"`;
    - a string of digits that matches no name is a number.
  - `deck.copy_footers(slide: Any, layout: Layout, number: int) -> None`:
    - deep-copies the layout's footer and slide-number `p:sp`, empties `p:spPr` and takes a new id from `slide.shapes._next_shape_id`;
    - writes the cached text of `a:fld type="slidenum"` as `str(number)`;
    - copies a date only when it holds no `a:fld`;
    - copies nothing that the layout's or master's `p:hf` turns off (`ftr="0"`, `sldNum="0"`, `dt="0"`; missing means on);
    - skips a footer with empty text.
  - `deck.fit(frame: Any, needed: int, place: Place) -> bool`: when `needed > place.lines`, sets `normAutofit fontScale = max(60000, 100000 × place.lines / needed)` and `lnSpcReduction="10000"`. Returns False when the scale hit 60 %.
  - `deck.check_parts(prs: Any) -> None`: duplicate part names raise `Refusal("invalid_template", "the master cannot be written as a new deck")`.
  - `deck.build(prs: Any, args: Mapping[str, Any], ctx: JobContext, warnings: list[str]) -> int` builds every slide and returns how many.
  - Runner `write_presentation(args: dict[str, Any], ctx: JobContext) -> dict[str, Any]`:
    - args are `path`, `name` (with `.pptx`), `slides` (image blocks carry `image` as the path below the chat folder plus `virtual`), `include_sources`, `budget_s`;
    - it answers `{"slides": n, "warnings": [...]}`, with at most `LISTED` warnings, each cut to `WARNING_MAX`;
    - it re-checks with `deck_problem`, which gives `invalid_value`.
  - Notes: the slide's `notes`, then, if `include_sources` and there are sources, a blank line, `Sources:` and one line per source, `"<kind>: <reference>"` from `sources.reference(source)`. Sources go through `sources.check({f"slide {n}, source {m}": {"source": s}}, ctx)` first.

- [ ] **Step 1: Write the failing tests**
  - `test_a_deck_has_the_slides_asked_for`: 3 slides (layout 1 by number, `"titel und inhalt"` by name in another case, `"6"` as a string) give `len(Presentation(out).slides) == 3`, the titles in order, and layouts named in order.
  - `test_the_example_slides_are_gone`: no slide title of the examples appears. The zip has no part of the examples' chart or picture. `len(names) == len(set(names))`. Slide part names are `slide1.xml`… in order.
  - `test_a_potx_master_gives_a_pptx`: the output's `[Content_Types].xml` names `/ppt/presentation.xml` `presentation.main+xml`.
  - `test_footer_and_slide_number_are_copied`: on slide 2 the footer shape's text is "Synthetic Ltd · internal", the slide-number field's text is "2", and there is no date shape.
  - `test_a_hidden_footer_stays_hidden`: with `make_master(hidden_footer=True)`, no footer or slide-number shape is on the slide.
  - `test_unknown_and_shared_layout_names_are_refused`: the error codes are `invalid_value` and the messages are as in Interfaces; `duplicate_name=True` with layout `3` works.
  - `test_notes_end_with_the_sources`:
    - a file source `{"kind": "file", "path": <a pdf of the job>, "page": 2}` and a user source give notes ending `"Sources:\nfile: /workspace/chat-1/harbour-report.pdf, page 2\nuser: "`;
    - `include_sources: false` gives the notes alone;
    - page 9 of a 2-page PDF is `invalid_source`.
  - `test_empty_places_are_removed`: a "Titel und Inhalt" slide with a title only has no placeholder left except the title, footer and slide number.
  - `test_overflowing_text_is_scaled_and_warned`: 40 bullets of 80 characters in one place set `fontScale="60000"` and give a warning starting `"slide 1: the text may not fit"`; 3 bullets set no `normAutofit` `fontScale`.
  - `test_the_answer_names_no_path`: `json.dumps(answer)` contains no `/workspace` and no job folder.
- [ ] **Step 2: Run** `uv run pytest tests/executor/test_presentations.py -q`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement** `deck.py` and the runner, in the order `build` works: `remove_slides`; per slide `resolve`, `add_slide`, title and subtitle (warning on a value the layout has no place for); text blocks via `write_markdown` and `fit`; `copy_footers`; remove the placeholders left empty; notes; then `check_parts`; save to `ctx.out_dir / name`.
- [ ] **Step 4: Run** `uv run pytest tests/executor tests/api/test_jobs.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(executor): build a deck from a slide master: text, footers, notes and sources`

### Task 6: Tables, charts and images in places

**Files:**
- Create: `workbench/executor/presentations/blocks.py`
- Modify: `workbench/executor/presentations/deck.py` (it dispatches on `decks.block_kind`)
- Test: `tests/executor/test_presentations.py`

**Interfaces:**
- Consumes: `Place` (Task 3), `decks.block_kind` (Task 2).
- Produces:
  - `ACCEPTS: Final = {"text": {"text", "content"}, "table": {"table", "content"}, "chart": {"chart", "content"}, "image": {"picture", "content"}}`.
  - `place_table(place: Place, rows: list[list[Any]]) -> None`:
    - `TablePlaceholder(place.shape._element, place.shape._parent).insert_table(len(rows), width)`;
    - rows shorter than the widest are padded;
    - the first row is the header;
    - row heights become the place's height / rows;
    - cells take 12 pt when there are more than 8 rows.
  - `place_chart(place: Place, chart: Mapping[str, Any]) -> None`:
    - `CategoryChartData`;
    - column → `COLUMN_CLUSTERED`; bar → `BAR_CLUSTERED` with `category_axis.reverse_order = True`; line → `LINE_MARKERS`; pie → `PIE`;
    - a legend at the bottom (`include_in_layout = False`) for several series or a pie;
    - no colours.
  - `place_image(place: Place, path: Path) -> None`: a picture place uses `insert_picture` (cropped to fill). A content place uses `slide.shapes.add_picture`, fitted within the place with its aspect ratio and centred, moved before the placeholder (`addprevious`); the placeholder is then removed.
  - `check_image(path: Path, virtual: str, label: str) -> None` reads the header with `pptx.parts.image.Image.from_blob`. Anything that is not PNG/JPEG/GIF, or is over `decks.MAX_IMAGE_PIXELS`, raises `Refusal("invalid_value", f"{label}: {virtual} …")`.
  - Mismatch: `Refusal("invalid_value", "slide 2, block 1: a table goes into a table or content place; layout 'Title Only' has: none")`. More blocks than places is refused the same way.

- [ ] **Step 1: Write the failing tests**
  - `test_a_table_fills_a_content_place`: a `graphicFrame` with an `a:tbl` of 3×2 whose `p:nvPr/p:ph@idx` equals the place's idx; the header text is in row 1; a ragged row is padded with empty cells.
  - `test_a_chart_is_native_and_editable`: the output has a `ppt/charts/chart*.xml` and an embedded `ppt/embeddings/*.xlsx`; `chart.chart_type` is as mapped for each of the four types; a pie with one series has a legend.
  - `test_a_chart_keeps_its_gaps`: a line series `[1, None, 3]` has no `c:v` for the middle point.
  - `test_an_image_is_cropped_in_a_picture_place_and_fitted_in_a_content_place`:
    - "Picture with Caption" gives a picture with crop values set;
    - "Titel und Inhalt" with a 2:1 PNG gives a picture inside the place's box, with width / height = 2 and no crop, and no placeholder left with that idx.
  - `test_a_bad_image_is_refused`: a `.png` that holds text gives `invalid_value` naming slide and block.
  - `test_blocks_that_do_not_fit_the_layout_are_refused`: content on "Title Only", an image in a text place, and 3 blocks on a one-place layout each give `invalid_value` with the listed places.
- [ ] **Step 2: Run** `uv run pytest tests/executor/test_presentations.py -q -k "table or chart or image or blocks"`. Expected: FAIL.
- [ ] **Step 3: Implement** `blocks.py` and the dispatch in `deck.build`.
- [ ] **Step 4: Run** `uv run pytest tests/executor -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(executor): native tables, charts and images in a slide's places`

### Task 7: `inspect_template` answers slide masters

**Files:**
- Modify: `workbench/api/tools_templates.py`, `workbench/api/tool_table.py` (`inspect_template` and `fill_template` descriptions), goldens
- Test: `tests/api/test_tools_templates.py`

**Interfaces:**
- Produces:
  - `TEMPLATE_FILES: Final = {".docx": ".docx", ".dotx": ".docx", ".xlsx": ".xlsx"}` (renamed from `OUTPUTS`) and `SLIDE_MASTERS: Final = frozenset({".pptx", ".potx"})`.
  - `class LayoutPlace(BaseModel): role: Literal["text", "content", "picture", "table", "chart"]; lines: int; chars: int`.
  - `class SlideLayout(BaseModel): number: int; name: str (max 100); title: bool; subtitle: bool; places: list[LayoutPlace]; footer: bool`.
  - `TemplateInfo.template_kind` gains `"slide_master"`. New fields: `layouts: list[SlideLayout] | None = None` and `slides: int | None = None` ("example slides the file already has; a deck leaves them out"). `_Inspected` matches.
  - `_fit_inspection` cuts `layouts` as it cuts `fields`.
  - `fill_template` on a slide master: 422 `unsupported_template: a slide master is filled with write_presentation`.
  - New `inspect_template` description: the current text plus "PowerPoint .pptx and .potx files are slide masters: their layouts are listed, numbered, with each layout's places in reading order (role, about how many lines and characters fit) and whether it has a footer; build a deck in one with write_presentation." Keep it to about 300 characters more.

- [ ] **Step 1: Write the failing tests**
  - `test_inspect_answers_a_slide_masters_layouts`: stored `mt.make_master`; `structuredContent["template_kind"] == "slide_master"`, `len(layouts) == 11`, `slides == 2`; one `query` event by the master's id.
  - `test_inspect_answers_a_potx_too`.
  - `test_a_long_layout_list_is_cut_to_the_result_limit`: with `small(...)` (1 KB), `truncated` is true and a hint says how many of 11 are listed.
  - `test_fill_template_refuses_a_slide_master`: a 422, no job sent, no event.
- [ ] **Step 2: Run** `uv run pytest tests/api/test_tools_templates.py -q -k "slide_master or potx or layout_list or refuses_a_slide"`. Expected: FAIL.
- [ ] **Step 3: Implement**, then edit both goldens by hand: the `inspect_template` and `fill_template` descriptions. `inspect_template` has no outputSchema in the goldens unless they already carry one; check and follow.
- [ ] **Step 4: Run** `uv run pytest tests/api -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(api): inspect_template lists a slide master's layouts`

### Task 8: The `write_presentation` tool

**Files:**
- Create: `workbench/api/tools_presentations.py`, `tests/api/test_tools_presentations.py`
- Modify:
  - `workbench/api/tools_templates.py`: `_sources` keeps its signature, built on a new `_source_spec`.
  - `workbench/api/tool_table.py` (new row between `write_document` and `write_table`), `workbench/api/registry.py` (`WORKSPACE_TOOLS`), `workbench/api/audit.py` (`OPERATIONS`).
  - `workbench/config.py:32`, `docker/compose.yaml:111`, `.env.example:81`.
  - `tests/callers.py`: `EIGHT` → `GHOSTWRITER_TOOLS`, nine names; update every importer.
  - `tests/test_compose_defaults.py` (`EIGHT_LIST` → `TOOLS_LIST`), `tests/test_docs.py:40` (`== 9`), `tests/api/test_tools_golden.py:3` (docstring), goldens.

**Interfaces:**
- Consumes: `decks.deck_problem`, `decks.block_kind`, the image bounds (Task 2); `tools_output._output_name` (`tools_output.py:145`); `tools.OutputKeeper`, `judged`, `_readable`, `_parse`, `_payload`, `_capped`, `_clean`, `_within`; `tools_templates.FileSource`, `JobSource`, `UserSource`, `AssistantSource`, `Source`, `SOURCE_KINDS`.
- Produces:
  - `_source_spec(store: Store, p: Principal, chat_id: str, source: Source, label: str) -> tuple[dict[str, Any], FileRecord | None]`, shared by `_sources`.
  - Request models (short descriptions; `json_schema_extra` for optional fields, as `FileSource` does):
    - `class TableBlock(BaseModel): table: list[list[Cell]]`.
    - `class ChartSpec(BaseModel): type: Literal["bar", "column", "line", "pie"]; categories: list[str | float]; series: dict[str, list[float | None]]`.
    - `class ChartBlock(BaseModel): chart: ChartSpec`.
    - `class ImageBlock(BaseModel): image: str; alt: str | None`.
    - `Block = str | TableBlock | ChartBlock | ImageBlock`.
    - `class SlideSpec(BaseModel): layout: int | str; title: str | None; subtitle: str | None; content: list[Block] = []; notes: str | None; sources: list[Annotated[Source, Field(discriminator="kind")]] = []`.
    - `class WritePresentationRequest(BaseModel): path: str; name: str; slides: list[SlideSpec]; include_sources: bool = True`.
  - `class BuiltPresentation(BaseModel): path: str; slides: int; warnings: list[str]`.
  - `async def write_presentation(body, p, settings, store, audit, jobs) -> BuiltPresentation`. The order:
    1. master by `store.get`, whose extension must be in `SLIDE_MASTERS`;
    2. `_output_name(body.name, "pptx")`;
    3. `deck_problem(body.model_dump(mode="json", exclude_none=True)["slides"])`;
    4. images: `store.get` each, extension in `IMAGE_TYPES`, `size ≤ MAX_IMAGE_BYTES`, sum ≤ `MAX_IMAGES_BYTES`, count ≤ `MAX_IMAGES`. A 422 names `slide N, block M`, never 404;
    5. sources via `_source_spec` with label `slide N, source M`;
    6. inputs: the master, the images, the source files (≤ `MAX_INPUT_FILES`);
    7. submit with an `OutputKeeper(audit, p, "write_presentation", used_ids, decide)`, where `decide` keeps exactly one planned file and records criteria `{"slides", "tables", "charts", "images", "sources_by_kind"}` (or `{"outputs": 0, …}` from `judged`);
    8. answer cut with `_fit_built`, modelled on `_fit_fill`.
  - `OPERATIONS["write_presentation"] = ("modify", "tool.write_presentation")`.
  - The tool's description, at most about 1200 characters. It says:
    - call `inspect_template` on the master first;
    - show the user the outline (each slide's title, layout number and name, key points, figures, sources) and call this tool only in a later message, after they agreed;
    - each content entry fills one place of the layout, in the listed order: Markdown text (bullets with `-`, up to 3 levels, **bold**, *italic*), `{table: rows}` with the first row the header, `{chart: …}`, or `{image: path}` of a PNG, JPEG or GIF of this conversation (make charts as images with `run_python`, or prefer a native chart);
    - at most 30 slides;
    - footers and slide numbers come from the master; the master's own slides are left out;
    - sources go into the speaker notes;
    - to change a deck, read it with `read_document` and write it again;
    - the user sees the new file as a card in the chat.

- [ ] **Step 1: Write the failing tests** (`tests/api/test_tools_presentations.py`, `Local` and helpers imported from `tests/api/test_tools_templates.py`):
  - `test_a_deck_is_written_and_recorded_as_a_modify`:
    - a stored master, a PDF source and a PNG output of a prior `write_table`-style put give one new output `deck.pptx`;
    - the answer has `slides` and `warnings`;
    - one event `tool.write_presentation`, action `modify`, whose target ids are the master, the PNG, the PDF and the new file, with criteria `{"slides": 2, "tables": 1, "charts": 1, "images": 1, "sources_by_kind": {"file": 1, "job": 0, "user": 0, "assistant": 1}}`;
    - no slide text in `audit_lines()` as a whole.
  - `test_refusals_cost_no_job`, parametrised over a `.docx` as master, a name `../x`, 31 slides, a table of 31 rows, an image that is no file of the chat, a `.svg` image, an 11 MiB image, and a source file of another chat: each is a 422 naming the slide and block where it applies, `fake_executor.requests == []`, and no event.
  - `test_a_job_that_makes_two_files_keeps_none`: `Scripted`-style, gives a 500, `outputs: 0` in an error event, no file kept.
  - `test_a_failed_audit_write_keeps_no_file`: follow the existing `fill_template` test of this case.
  - `test_the_runner_refusal_is_recorded_by_its_code`: unknown layout gives `invalid_value` in the event criteria and no file.
  - `test_ghostwriter_is_offered_write_presentation` and the goldens: `tools/list` has nine names, sorted.
  - `test_the_schema_stays_small`: `len(json.dumps(input_schema)) + len(description) <= 6500` for `write_presentation`, so it does not quietly grow in Ghostwriter's prompt.
- [ ] **Step 2: Run** `uv run pytest tests/api/test_tools_presentations.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the module and the registrations; add the row to both goldens by hand (name, title, description, inputSchema as `tools/list` serves it; copy the served JSON once after reviewing it line by line).
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: PASS (all of workbench).
- [ ] **Step 5: Commit** `feat(api): write_presentation builds a deck in the user's slide master`

### Task 9: Docs, isolation, release

**Files:**
- Modify:
  - `CLAUDE.md`: "eight" becomes "nine"; the new modules in the module lists and in "Adding a tool touches", plus the compose and config tool list.
  - `README.md:5`.
  - `docs/permissions.md`: the matrix row, "eight" at 16 and 56, the bounds.
  - `docs/audit.md`: the `tool.write_presentation` row.
  - `docs/configuration.md:34`: the default list.
  - `docs/data-flow.md`: slide content becomes file content; sources go into the notes; images are copied into the deck; the master's media and core properties are carried into the new file.
  - `docs/README.md` if it indexes the tools.
- Modify: `tests/isolation/test_isolation.py`: one case, `write_presentation` on a real stack with a master, a chart, a PNG and notes.
- Modify: `pyproject.toml` (version `0.6.0`), `uv.lock` (`uv lock`).

- [ ] **Step 1: Write the isolation case** `test_a_deck_is_built_in_the_sandbox`. Follow the file's existing template case: upload a master, call the tool, then download the output and open it with python-pptx in the test process.
- [ ] **Step 2: Docs.** Run `uv run pytest tests/test_docs.py -q`. Expected: PASS.
- [ ] **Step 3: Version.** `uv lock`, then `uv run pytest tests/test_version.py -q`. Expected: PASS.
- [ ] **Step 4: Gates.** `make verify && make test`, then `make test-executor-root` and `make test-isolation`. Expected: each green. Paste the summary lines into the PR.
- [ ] **Step 5: Commit** `docs: write_presentation in the design record, permissions, audit and data flow` and `chore(release): 0.6.0`.
- [ ] **Step 6: Push and PR.** `git push -u origin feat/write-presentation`, PR to `main`; confirm CI ran on the last commit.

### Task 10: Ghostwriter: `.potx` and `.dotx` attachments

**Files (repo `ghostwriter`):**
- Modify: `ghostwriter/upload.py:30`, `tests/test_api_meta.py:21-40`

- [ ] **Step 1: Test.** `test_api_meta`'s expected `attachment_types` gains `.dotx` and `.potx` in sorted order. Add `test_a_potx_is_sent_on_as_a_template`: the MIME type forwarded for `deck.potx` is `application/vnd.openxmlformats-officedocument.presentationml.template`. Follow the existing upload test that checks a forwarded MIME type.
- [ ] **Step 2: Run** `uv run pytest tests/test_api_meta.py tests/test_api_files.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the two entries.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `feat(upload): .potx slide masters and .dotx templates as attachments`

### Task 11: Ghostwriter: the prompt and the cut call

**Files:**
- Modify: `ghostwriter/agent/prompt.py`, `ghostwriter/agent/loop.py:322` (and `_prepare`), `ghostwriter/agent/tools.py:23` (a message constant)
- Test: `tests/test_agent_context.py`, `tests/test_agent_loop.py`

**Interfaces:**
- Produces:
  - `system_prompt(..., presentations: bool = False)`, a new keyword on the existing function.
  - `CUT_ARGUMENTS: Final = "the call to {tool} was cut off at the answer's length limit; make the arguments shorter (fewer or shorter slides, rows or text) and call it again."`
- Prompt sentence (only when `presentations`): "To build a presentation in a slide master (.pptx or .potx): call inspect_template on it and read the sources, then show the user an outline (each slide's title, the layout's number and name, key points, figures and sources) and stop. Call write_presentation only in a later message, after the user has agreed. Images for slides are made with run_python; prefer native charts."

- [ ] **Step 1: Write the failing tests**
  - `test_the_presentation_sentence_comes_with_the_tool`: the prompt with `presentations=True` contains "write_presentation only in a later message"; without the flag it does not.
  - `test_the_loop_sets_presentations_from_the_offered_tools`: a `FakeDoor` offering `write_presentation` gives a first system message containing the sentence.
  - `test_a_call_cut_at_the_length_limit_says_so`: a scripted round ending `finish_reason="length"` with half-written arguments gives a tool message equal to `CUT_ARGUMENTS.format(tool=…)`, and no workbench call.
- [ ] **Step 2: Run** `uv run pytest tests/test_agent_context.py tests/test_agent_loop.py -q -k "presentation or cut_at"`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(agent): the outline-first rule for presentations; a call cut at the length limit is named`

### Task 12: Ghostwriter: trail label, contract tools, release

**Files:**
- Modify: `frontend/src/components/Trail.tsx:13-22`, `frontend/src/i18n/en.ts`, `frontend/src/i18n/de.ts`, `frontend/src/components/Trail.test.tsx`
- Modify: `tests/fakes.py` (`CONTRACT_TOOLS`), the lists in `tests/test_workbench.py` and `tests/test_agent_loop.py`
- Modify: `pyproject.toml` (0.2.0), `uv.lock`, `tests/test_version.py:10-12`; `docs/configuration.md` or `docs/data-flow.md` wherever attachment types are named.

- [ ] **Step 1: Test.** `Trail.test.tsx`: a `write_presentation` step with `detail` `master.potx` reads "Built a presentation from master.potx" in English and "Präsentation aus master.potx erstellt" in German; without detail, "Built a presentation" and "Eine Präsentation erstellt".
- [ ] **Step 2: Run** `cd frontend && pnpm test src/components/Trail.test.tsx`. Expected: FAIL.
- [ ] **Step 3: Implement** the `TOOL_LABELS` row and the keys `trail.write_presentation` and `trail.write_presentation_unnamed` in both locales.
- [ ] **Step 4: Gates.** `make verify && make test`. Expected: green, with the `i18n` key-parity test included.
- [ ] **Step 5: End to end.**
  - workbench (from the worktree) and Ghostwriter with `make dev`.
  - Upload a master built by `make_master` and saved locally, plus two invented sources.
  - Message 1 shows an outline and no `write_presentation` step.
  - Message 2, "passt, bitte erstellen", gives the trail label and a download pill.
  - Open the `.pptx` (Keynote or LibreOffice) and check layouts, footer, slide numbers, table, chart, image and notes.
  - Repeat with a `.potx`.
  - Check the trail with Playwright in light and dark.
- [ ] **Step 6: Commit and PR.** `feat(ui): trail label for write_presentation` and `chore(release): 0.2.0`; branch `feat/presentations`; confirm CI ran.
