"""The tool runners, by tool name; each tool task registers its own."""

from collections.abc import Callable
from typing import Any

from workbench.executor.context import JobContext
from workbench.executor.runners.documents import read_document, search_files
from workbench.executor.runners.outputs import write_document, write_table
from workbench.executor.runners.presentations import write_presentation
from workbench.executor.runners.python import run_python
from workbench.executor.runners.render import prepare_file
from workbench.executor.runners.templates import fill_template, inspect_template

__all__ = ["RUNNERS", "JobContext", "echo"]


def echo(args: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
    """Return the arguments unchanged; proves the protocol and the sandbox end to end.

    Args:
        args: The runner's arguments.
        ctx: The job's surroundings; unused.

    Returns:
        ``args``.
    """
    return args


RUNNERS: dict[str, Callable[[dict[str, Any], JobContext], dict[str, Any]]] = {
    "echo": echo,
    "read_document": read_document,
    "search_files": search_files,
    "run_python": run_python,
    "inspect_template": inspect_template,
    "fill_template": fill_template,
    "write_table": write_table,
    "write_document": write_document,
    "write_presentation": write_presentation,
    "prepare_file": prepare_file,
}
