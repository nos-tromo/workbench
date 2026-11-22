"""Values the tests of several callers share."""

#: Ghostwriter's tools by default: every workspace tool.
GHOSTWRITER_TOOLS = frozenset(
    {
        "list_files",
        "read_document",
        "search_files",
        "view_file",
        "run_python",
        "inspect_template",
        "fill_template",
        "write_table",
        "write_document",
        "write_presentation",
    }
)

#: What workbench 0.6 told the model, and still tells a call that takes no images (no ``X-Accept-Images: 1``): the
#: description of ``read_document``, the end of the workspace instructions and the read's hint on pages without text.
TEXT_ONLY_READ_DOCUMENT = (
    "Read one file of this conversation as text, each piece with a reference. Readable: PDF (text layer only, no OCR), "
    "DOCX, DOTX, XLSX, XLSM, CSV, TSV, PPTX, POTX, ODT, ODS, EML, HTML, JSON (shown indented), JSONL, NDJSON, TXT and "
    "MD. Give the file's path from list_files. Reading starts at part (the start if left out) and goes on until the "
    "result limit, so a long file is read in several calls: when truncated is true, call again with part set to "
    "next_part. Parts are pages ('page 3', 'pages 3-9'), slides ('slide 2'), paragraphs ('paragraph 40'), runs of "
    "table rows ('table 2, rows 30-60'; a search location such as 'table 2, row 350' opens there), ranges of up to "
    "200 sheet rows ('Sheet1!A201:F400', each line 'row: cell | cell'; a sheet's name starts at its first row) and "
    "lines ('lines 100-200'). Cite what you read by these references. pages_without_text lists pages or slides with "
    "no text layer (scans, pictures): this tool does no OCR, so tell the user those need OCR elsewhere, and never "
    "treat them as blank. Outlook .msg files cannot be read: ask the user to save them as .eml or PDF."
)
#: What workbench 0.8.0 told the model of ``write_presentation``, and still tells a call that takes no images: it
#: names no view_file.
TEXT_ONLY_WRITE_PRESENTATION = (
    "Build a PowerPoint deck into a new .pptx of this conversation, in a slide master (.pptx or .potx) of it. Without "
    "path, the deck is built in a plain 16:9 master. Call inspect_template on the master first (without path for the "
    "plain one). Then show the user the outline (each slide's title, layout number and name, key points, figures and "
    "sources) and call this tool only in a later message, after they agreed. Each content entry fills one place of the "
    "slide's layout, in the order inspect_template lists its places: Markdown text (bullets with -, up to 3 levels, "
    "**bold**, *italic*), {table: rows} with the first row the header, {chart: {type, categories, series}}, or "
    "{image: path} of a PNG, JPEG or GIF of this conversation (make a chart image with run_python, or prefer a native "
    'chart); "" leaves a place empty. A slide\'s title and subtitle are plain text, not Markdown. At most 30 slides. '
    "Footers and slide numbers come from the master; the master's own slides are left out. Each slide's sources go "
    "into its speaker notes. To change a deck, read it with read_document and write it again. The user sees the new "
    "file as a card in the chat."
)
TEXT_ONLY_INSTRUCTIONS = (
    "workbench works on the files of this chat: it lists, reads and searches them, runs Python over them in a sandbox "
    "with no network, fills report templates and writes tables and documents. Every file a tool writes comes back as "
    "a reference with its path and id, never as bytes."
)
TEXT_ONLY_PAGE_HINT = (
    "page 2 of those looked at have no text layer (scans or pictures). read_document does no OCR: the file needs OCR "
    "elsewhere before they can be read. Tell the user; do not treat them as blank."
)
