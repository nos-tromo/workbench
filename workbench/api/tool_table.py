"""The workspace tools, one row each: what the MCP door tells the model of every tool, and what runs it.

A row names the tool, its title, its description written for the model (and, for a tool told otherwise to a call that
takes no images, that text too), the model of its arguments and of its answer, and the function that runs it (in
:mod:`workbench.api.tools`, :mod:`workbench.api.tools_templates`, :mod:`workbench.api.tools_output`,
:mod:`workbench.api.tools_presentations` and :mod:`workbench.api.tools_views`).
:func:`~workbench.api.tools_mcp.workspace_tools` makes each row a spec of the registry. A new workspace tool is a row
here.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from pydantic import BaseModel

from workbench.api import tools, tools_output, tools_presentations, tools_templates, tools_views
from workbench.tables import MAX_WORD_COLUMNS


@dataclass(frozen=True)
class WorkspaceTool:
    """A workspace tool as the table describes it.

    Attributes:
        name: Its name.
        title: A short title.
        description: What it does, written for the model.
        input_model: The model of its arguments, or ``None`` when it takes none (``list_files``).
        output_model: The model of its answer, before the door adds ``outputs``.
        function: Runs it; it takes, by name, any of ``body``, ``p``, ``settings``, ``store``, ``audit``, ``jobs`` and
            ``images`` (whether the call takes images).
        images_only: Whether it only shows images, and so is offered only to a call that takes them.
        text_description: What a call that takes no images is told of it instead, or ``None`` for ``description``.
    """

    name: str
    title: str
    description: str
    input_model: type[BaseModel] | None
    output_model: type[BaseModel]
    function: Callable[..., Any]
    images_only: bool = False
    text_description: str | None = None


#: The workspace tools, in name order.
WORKSPACE_TABLE: Final[tuple[WorkspaceTool, ...]] = (
    WorkspaceTool(
        name="fill_template",
        title="Fill a report template into a new file",
        description=(
            "Fill a report template of this conversation into a new file; the template itself is not changed, and a "
            ".dotx becomes a .docx. A slide master (.pptx or .potx) is not filled with this: build a deck in it with "
            "write_presentation. Call inspect_template first and agree the values with the user. values maps each "
            "field's name to its value and its source, and every value needs a source: kind file (the path of a file "
            "of this conversation as list_files gives it, with page for a PDF page or PPTX slide, or sheet and cell "
            "such as B7 for a workbook), job (the job_id of the run_python run that computed it: every computed "
            "figure cites its job_id), user (the user said it; note says where) or assistant (you wrote it). Types: "
            "text and rich_text take a string or a number, date YYYY-MM-DD, dropdown one of its options, checkbox "
            "true or false, table a list of rows (each a list of values in a workbook). Fields without a value are "
            "listed in unfilled. The new file ends with a Sources table (a Sources sheet in a workbook) naming each "
            "value's source; include_sources false leaves it out. The user sees the new file as a card in the chat."
        ),
        input_model=tools_templates.FillTemplateRequest,
        output_model=tools_templates.FilledTemplate,
        function=tools_templates.fill_template,
    ),
    WorkspaceTool(
        name="inspect_template",
        title="List the fields of a template or the layouts of a slide master",
        description=(
            "List the fields of a report template in this conversation, so you can plan the fill with the user before "
            "you call fill_template. Templates are Word .docx and .dotx files with content controls or legacy form "
            "fields, Word .docx files with {{ field }} placeholders (loops and conditions too), and Excel .xlsx files "
            "with {{ field }} cells and {{ table:name }} cells where a table grows downwards. Each field has its name "
            "(give its value under it), kind, type (text, rich_text, date, dropdown with its options, checkbox, "
            "table) and location. template_kind none means version 1 cannot fill the file: tell the user what it "
            "needs, as hint says. PowerPoint .pptx and .potx files are slide masters: their layouts are listed, "
            "numbered, with each layout's places in reading order (role, about how many lines and characters fit) and "
            "whether it has a footer; build a deck in one with write_presentation. Without path, it lists the layouts "
            "of the plain 16:9 master write_presentation uses when the user gave none. A very long list is cut: "
            "truncated is then true."
        ),
        input_model=tools_templates.InspectTemplateRequest,
        output_model=tools_templates.TemplateInfo,
        function=tools_templates.inspect_template,
    ),
    WorkspaceTool(
        name="list_files",
        title="List the files of this conversation",
        description=(
            "List every file in this conversation's workspace, the user's uploads and the files tools made, newest "
            "first, with path, size, date and kind. Takes no arguments. Call it first to see what there is to work "
            "with. A very long list is cut: truncated is then true, and hint says how many older files are not listed."
        ),
        input_model=None,
        output_model=tools.FileList,
        function=tools.list_files,
    ),
    WorkspaceTool(
        name="read_document",
        title="Read a file of this conversation as text",
        description=(
            "Read one file of this conversation as text, each piece with a reference. Readable: PDF, DOCX, DOTX, XLSX, "
            "XLSM, CSV, TSV, PPTX, POTX, ODT, ODS, EML, HTML, JSON (shown indented), JSONL, NDJSON, TXT and MD, and "
            "pictures (PNG, JPEG, WebP, GIF, BMP, TIFF, HEIC), which come back as the image. Give the file's path "
            "from list_files. Reading starts at part (the start if left out) and goes on until the result limit, so "
            "a long file is read in several calls: when truncated is true, call again with part set to next_part. "
            "Parts are pages ('page 3', 'pages 3-9'), slides ('slide 2'), "
            "paragraphs ('paragraph 40'), runs of table rows ('table 2, rows 30-60'; a search location such as "
            "'table 2, row 350' opens there), ranges of up to 200 sheet rows ('Sheet1!A201:F400', each line 'row: "
            "cell | cell'; a sheet's name starts at its first row) and lines ('lines 100-200'). Cite what you read by "
            "these references. pages_without_text lists pages or slides with no text layer (scans, pictures); never "
            "treat them as blank. Such PDF pages and PowerPoint slides come back as images in the same answer (shown "
            "names them): read them from the images. To look at any page or slide, use view_file. Outlook .msg files "
            "cannot be read: ask the user to save them as .eml or PDF."
        ),
        input_model=tools.ReadDocumentRequest,
        output_model=tools.DocumentRead,
        function=tools_views.read_document,
        # workbench 0.6's words, for a call that takes no images: they promise none.
        text_description=(
            "Read one file of this conversation as text, each piece with a reference. Readable: PDF (text layer only, "
            "no OCR), DOCX, DOTX, XLSX, XLSM, CSV, TSV, PPTX, POTX, ODT, ODS, EML, HTML, JSON (shown indented), JSONL, "
            "NDJSON, TXT and MD. Give the file's path from list_files. Reading starts at part (the start if left out) "
            "and goes on until the result limit, so a long file is read in several calls: when truncated is true, "
            "call again with part set to next_part. Parts are pages ('page 3', 'pages 3-9'), slides ('slide 2'), "
            "paragraphs ('paragraph 40'), runs of table rows ('table 2, rows 30-60'; a search location such as "
            "'table 2, row 350' opens there), ranges of up to 200 sheet rows ('Sheet1!A201:F400', each line 'row: "
            "cell | cell'; a sheet's name starts at its first row) and lines ('lines 100-200'). Cite what you read by "
            "these references. pages_without_text lists pages or slides with no text layer (scans, pictures): this "
            "tool does no OCR, so tell the user those need OCR elsewhere, and never treat them as blank. Outlook .msg "
            "files cannot be read: ask the user to save them as .eml or PDF."
        ),
    ),
    WorkspaceTool(
        name="run_python",
        title="Run Python on the files of this conversation",
        description=(
            "Run Python code on this conversation's files and get back what it printed. The files are in the current "
            "folder, under their names as list_files gives them (also at their /workspace/<chat>/<name> paths), "
            "read-only. Write every result file directly into the out/ folder, not into a subfolder: each becomes a "
            "new file of the workspace, and files lists their paths (at most 20 files a run). Save charts as image "
            "files there, for example plt.savefig('out/chart.png'); nothing is shown inline, so the user sees each "
            "file in out/ as a card in the chat. Installed: pandas, numpy, scipy, matplotlib, openpyxl, xlsxwriter, "
            "python-docx, docxtpl, python-pptx, pypdf, odfpy, lxml, beautifulsoup4, pillow, pypdfium2. To turn a "
            "data file (JSON, CSV, XLSX) into a table file, load it in code (json, pandas.json_normalize) and write "
            "it to out/ as .xlsx or .csv; do not copy its rows into write_table. There is no network and nothing can "
            "be installed. Use threads, not multiprocessing: process pools and multiprocessing do not work here. The "
            "code runs as a script with a time limit; an error or timeout is in stderr. stdout and stderr are cut in "
            "the middle when long (truncated is then true), so print summaries, not whole tables. Cite job_id as the "
            "source of figures the code computed."
        ),
        input_model=tools.RunPythonRequest,
        output_model=tools.PythonRun,
        function=tools.run_python,
    ),
    WorkspaceTool(
        name="search_files",
        title="Search all files of this conversation",
        description=(
            "Search every file of this conversation for a text or, with regex true, a regular expression; case is "
            "ignored. Each hit has the file's path, the location in it (page 3, Sheet1!B7, paragraph 12, line 40) and "
            "a snippet of at most 200 characters; read the place with read_document. max_hits is 50 by default and "
            "at most 200. Files that cannot be searched are named in skipped (no text layer, encrypted, damaged) or "
            "counted in unsupported_files (images and other types): never tell the user something is not in the "
            "files without saying what could not be searched."
        ),
        input_model=tools.SearchFilesRequest,
        output_model=tools.SearchResult,
        function=tools.search_files,
    ),
    WorkspaceTool(
        name="view_file",
        title="Look at a picture, or the pages or slides of a file",
        description=(
            "Show a picture, or the pages or slides of a file of this conversation, as images for you to see, with "
            "their metadata removed. Shown: pictures (PNG, JPEG, WebP, GIF, BMP, TIFF, HEIC), the pages of a PDF, "
            "Word (DOCX, DOTX) or OpenDocument text (ODT) file and the slides of a PowerPoint (PPTX, POTX) file, "
            "drawn as they would print. part chooses them: 'page 3' or 'pages 2-5', for a deck 'slide 3' or "
            "'slides 2-5'; left out, a picture is shown, or the first four pages or slides. Use it for what text "
            "cannot tell you: layout, design, charts, handwriting, photos, scans; and to look at a chart or picture "
            "a tool made, such as one run_python wrote. At most 8 images come back at once: when truncated is true, "
            "call again with part set to next_part. Pages or slides that could not be shown are named in hint. "
            "Spreadsheets are not shown: read them with read_document. Never describe an image you have not looked "
            "at."
        ),
        input_model=tools_views.ViewFileRequest,
        output_model=tools_views.ViewedFile,
        function=tools_views.view_file,
        images_only=True,
    ),
    WorkspaceTool(
        name="write_document",
        title="Write a document into a new Word or Markdown file",
        description=(
            "Write a document into a new file of this conversation from Markdown: a Word document (format docx) or "
            "the Markdown itself (format md). A Word document takes headings # to #### (##### and ###### become level "
            "4), paragraphs, **bold** and *italic*, bullet and numbered lists nested up to three levels (each numbered "
            f"list counts from its own first number), tables of up to {MAX_WORD_COLUMNS} columns (| a | b | over a "
            "|---|---| line; Word holds no wider one, so write that with write_table) and code, in blocks or "
            "`inline`, in a monospace font. A link is written as its text with the address in parentheses, an image "
            "as its alt text: nothing is fetched. Raw HTML and other syntax stay plain text. At most 200000 "
            "characters. The user sees the new file as a card in the chat."
        ),
        input_model=tools_output.WriteDocumentRequest,
        output_model=tools_output.WrittenDocument,
        function=tools_output.write_document,
    ),
    WorkspaceTool(
        name="write_presentation",
        title="Build a slide deck in a slide master",
        description=(
            "Build a PowerPoint deck into a new .pptx of this conversation, in a slide master (.pptx or .potx) of it. "
            "Without path, the deck is built in a plain 16:9 master. Call inspect_template on the master first "
            "(without path for the plain one). Then show the user the outline (each slide's title, layout "
            "number and name, key points, figures and sources) and call this tool only in a later message, after they "
            "agreed. Each content entry fills one place of the slide's layout, in the order inspect_template lists its "
            "places: Markdown text (bullets with -, up to 3 levels, **bold**, *italic*), {table: rows} with the first "
            "row the header, {chart: {type, categories, series}}, or {image: path} of a PNG, JPEG or GIF of this "
            'conversation (make a chart image with run_python, or prefer a native chart); "" leaves a place empty. '
            "A slide's title and subtitle are plain text, not Markdown. At most 30 slides. Footers and slide numbers "
            "come from the master; the master's own slides are left out. Each slide's sources go into its speaker "
            "notes. View the new deck with view_file. To change it, read it with read_document and write it again, or "
            "restyle it in run_python. The user sees the new file as a card in the chat."
        ),
        input_model=tools_presentations.WritePresentationRequest,
        output_model=tools_presentations.BuiltPresentation,
        function=tools_presentations.write_presentation,
        # 0.8.0's words, for a call that takes no images: they name no view_file.
        text_description=(
            "Build a PowerPoint deck into a new .pptx of this conversation, in a slide master (.pptx or .potx) of it. "
            "Without path, the deck is built in a plain 16:9 master. Call inspect_template on the master first "
            "(without path for the plain one). Then show the user the outline (each slide's title, layout "
            "number and name, key points, figures and sources) and call this tool only in a later message, after they "
            "agreed. Each content entry fills one place of the slide's layout, in the order inspect_template lists its "
            "places: Markdown text (bullets with -, up to 3 levels, **bold**, *italic*), {table: rows} with the first "
            "row the header, {chart: {type, categories, series}}, or {image: path} of a PNG, JPEG or GIF of this "
            'conversation (make a chart image with run_python, or prefer a native chart); "" leaves a place empty. '
            "A slide's title and subtitle are plain text, not Markdown. At most 30 slides. Footers and slide numbers "
            "come from the master; the master's own slides are left out. Each slide's sources go into its speaker "
            "notes. To change a deck, read it with read_document and write it again. The user sees the new file as a "
            "card in the chat."
        ),
    ),
    WorkspaceTool(
        name="write_table",
        title="Write a table into a new Excel or CSV file",
        description=(
            "Write a table into a new file of this conversation: an Excel workbook (format xlsx, a bold header row "
            "that stays in view) or a CSV file (format csv, UTF-8, comma-separated unless delimiter is ; or a tab). "
            "For people who will open the file in Excel, use xlsx: Excel reads a CSV file by the computer's regional "
            "settings (German Excel splits it on ; and may read 1.5 as text or a date), so CSV is for exchanging data "
            "with other programs. columns are the header's names; rows are lists of values in the columns' order, "
            "each a string, a number, true, false or null; a shorter row is filled with empty cells. At most 100000 "
            "rows, 16384 columns and 32767 characters a cell. Values are data, never formulas: =SUM(A1:A3), 0042 or "
            "a web address stay text; a number keeps 15 significant digits in Excel, so write long identifiers as "
            "strings. In a CSV a text that starts with =, +, -, @, a tab or a carriage return gets a leading ' so "
            "that a spreadsheet does not run it, and hint says how many. sheet names the workbook's sheet. One "
            "request is at most 1 MB: for a large table, write the file with run_python into out/ instead. The user "
            "sees the new file as a card in the chat."
        ),
        input_model=tools_output.WriteTableRequest,
        output_model=tools_output.WrittenTable,
        function=tools_output.write_table,
    ),
)
