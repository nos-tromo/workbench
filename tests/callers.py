"""Values the tests of several callers share."""

#: Ghostwriter's tools by default: every workspace tool.
GHOSTWRITER_TOOLS = frozenset(
    {
        "list_files",
        "read_document",
        "search_files",
        "run_python",
        "inspect_template",
        "fill_template",
        "write_table",
        "write_document",
        "write_presentation",
    }
)
