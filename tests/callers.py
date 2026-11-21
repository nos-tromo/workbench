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
TEXT_ONLY_INSTRUCTIONS = (
    "workbench works on the files of this chat: it lists, reads and searches them, runs Python over them in a sandbox "
    "with no network, fills report templates and writes tables and documents. Every file a tool writes comes back as "
    "a reference with its path and id, never as bytes."
)
TEXT_ONLY_PAGE_HINT = (
    "page 2 of those looked at have no text layer (scans or pictures). read_document does no OCR: the file needs OCR "
    "elsewhere before they can be read. Tell the user; do not treat them as blank."
)
