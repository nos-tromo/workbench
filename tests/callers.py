"""Values the tests of several callers share."""

#: Ghostwriter's tools by default: the workspace tools without ``display_file``.
EIGHT = frozenset(
    {
        "list_files",
        "read_document",
        "search_files",
        "run_python",
        "inspect_template",
        "fill_template",
        "write_table",
        "write_document",
    }
)
