# workbench 0.6.0 — presentations in the user's slide master

Status: approved by the owner · 2026-11-18

A Ghostwriter user attaches source documents and a PowerPoint slide master and asks for a
presentation. The model reads the sources, shows an outline, and after the user agrees builds a
`.pptx` that uses the master's layouts, theme, footers and slide numbers and stays editable in
PowerPoint. Decisions are marked **(owner)** when the project owner made them and **(design)**
when this document makes them; the builder may revisit a **(design)** call with a stated
reason, never an **(owner)** one.

## 1. Decisions

1. Typed tools in workbench, not `run_python` alone and no rendering in this version (owner).
2. The outline always comes first: the model shows it in the chat and builds only in a later
   message, after the user agreed (owner).
3. Decks of up to about 15 slides, one call builds the whole deck; no appending (owner).
4. Slide content: title, subtitle, Markdown text with bullets, native tables, native charts in
   the theme's colours, images of the chat, and per-slide sources in the speaker notes (owner).
5. Ghostwriter allows no image uploads; images in slides come from `run_python` outputs. The
   tool itself takes any PNG, JPEG or GIF of the chat (owner).
6. `inspect_template` is extended for slide masters rather than adding a ninth inspect tool:
   Ghostwriter sends every tool's input schema on every round, and the answer costs nothing
   there (design).
7. `write_presentation` is recorded as a `modify`, as `fill_template` is: it reads files of the
   chat (the master, images, sources) (design).

## 2. The flow

1. `inspect_template` on a `.pptx` or `.potx` answers `template_kind: "slide_master"`, its
   layouts and the number of slides the file already has (they are left out of a deck).
2. The model reads the sources and shows the user an outline: for each slide its title, the
   layout's number and name, the key points, figures and sources. It stops there.
3. In a later message `write_presentation` builds the deck: one new `.pptx` of the chat (a
   `.potx` master gives a `.pptx`). The answer names the file, the number of slides and
   warnings.

Ghostwriter replays an earlier answer in full but earlier tool results cut to 2000 characters,
so the outline carries the layout numbers and the figures itself.

## 3. `inspect_template` on a slide master

Each layout, numbered 1..N across all slide masters, has its name and its places in reading
order (top, then left). A place has a role and a capacity:

| Placeholder type | Role |
|---|---|
| title, centre title | `title` |
| subtitle | `subtitle` |
| body | `text` |
| object | `content` |
| picture, bitmap | `picture` |
| table | `table` |
| chart | `chart` |

Title and subtitle are reported as flags of the layout, not as places. Date, footer and slide
number set the layout's `footer` flag. SmartArt and media placeholders are not listed. The
capacity is lines and characters a line, from the placeholder's size less its insets and the
inherited font size (the layout's list style, then the master placeholder of that type, then
the master's text styles, else 18 pt): characters ≈ width / (0.5 × size), lines ≈ height /
(1.2 × size). At most 100 layouts are listed; more set `truncated`. `fill_template` refuses a
slide master and names `write_presentation`.

## 4. `write_presentation`

```
path: str                 the master (.pptx or .potx) as list_files gives it
name: str                 the new file's name; .pptx is added if missing
include_sources: bool     true by default
slides: 1..30 of
  layout: int | str       number or name as inspect_template lists it
  title, subtitle: str    at most 300 and 500 characters
  content: 0..6 of        one entry per place, in the listed order
      str                                Markdown, at most 2000 characters
    | {table: [[cell]]}                  the first row is the header
    | {chart: {type: bar|column|line|pie, categories: [...], series: {name: [number|null]}}}
    | {image: path, alt: str}
  notes: str              at most 5000 characters
  sources: 0..20 of fill_template's file, job, user and assistant sources
```

A realistic 15-slide deck measures about 6500 characters, about 2200 tokens by Ghostwriter's
estimate, within its 4000-token answer cap (design).

### Bounds (`workbench/decks.py`, shared by both sides as `tables.py` is)

| Item | Bound |
|---|---|
| Slides | 1 to 30, at most 6 blocks each |
| Table | at most 30 rows with the header, 10 columns, 500 characters a cell; `tables.cell_problem` |
| Chart | 1 to 50 categories, 1 to 10 series, a value per category, finite or null; a pie has one series of values ≥ 0 |
| Images | at most 20 a deck, each 10 MiB and 40 megapixels, 30 MiB together; PNG, JPEG, GIF |
| Sources | at most 20 a slide |

The api checks every bound before a job (a 422 that names slide and block, no job, no event);
the runner checks them again.

### Building (python-pptx 1.0.2)

- A `.potx` is opened by retyping its main part in `[Content_Types].xml`, the helper `.dotx`
  already uses, moved to `workbench/executor/readers/ooxml.py`. A macro-enabled main part or a
  slide show is refused.
- The existing slides go first: `p:custShowLst` and the `p14:sectionLst` extension are removed
  (they hold references `drop_rel` would leave), then each `sldId` and its relationship. After
  building, part names must be unique, or the master is refused as `invalid_template`.
- A layout is found by number, or by name ignoring case; a name of digits that matches no
  layout's name is a number. An unknown layout, or a name two layouts share, is refused.
- Block i fills place i. Text goes into a text or content place, a table into a table or
  content place, a chart into a chart or content place, an image into a picture or content
  place. A table and a chart in a content place use `insert_table` / `insert_chart` on the
  placeholder's element (the `ph idx` link stays); an image in a picture place is cropped to
  fill it, in a content place it is fitted within it.
- Text: markdown-it (`commonmark`, no HTML). List items get levels 0 to 2 and the master's
  bullet; a plain paragraph gets `a:buNone`; a numbered list gets `a:buAutoNum`; bold and
  italic are runs; a link is its text with its address in parentheses.
- Fit: the text's lines are estimated against the place's capacity; when they pass it,
  `normAutofit` gets a `fontScale` (and `lnSpcReduction`) down to 60 %, and a warning names the
  slide when even that does not fit. A bare `normAutofit` would shrink nothing until the text
  is edited in PowerPoint.
- Footers: python-pptx clones date, footer and slide-number placeholders without a text body.
  The layout's footer and slide-number shapes are copied with their text instead, their shape
  properties emptied so the position is inherited, a new id, and the slide's number as the
  field's text. A date is copied only as fixed text, never a stale date field. `p:hf` flags
  that turn a footer off are kept.
- Charts: column, bar (category axis reversed), line with markers, pie; a legend at the bottom
  for several series or a pie; no colours set, so the theme's accents apply.
- Empty places left over are removed. Notes get the slide's notes, then `Sources:` and one line
  a source as `fill_template`'s Sources table words them, unless `include_sources` is false.

## 5. Recording

`write_presentation` is a `modify` (`tool.write_presentation`): the event names the master,
each image, each source file and the new file by id; its criteria are counts only (slides,
tables, charts, images, sources by kind). No slide text, name or value is ever recorded or
logged.

## 6. Ghostwriter 0.2.0

- `.potx` and `.dotx` become attachment types; no image types (owner).
- A trail label for `write_presentation`, in English and German.
- A prompt sentence, only when the tool is offered: inspect the master, read the sources, show
  the outline and stop; build in a later message after the user agreed; make images for
  slides with `run_python`, prefer native charts.
- A tool call cut at the answer cap is reported to the model as cut, not as bad arguments.

## 7. Not in this version

Slide previews and rendering checks (LibreOffice), appending to a deck, images out of source
documents, image uploads, `.odp`, `.ppt` and `.pot`.
