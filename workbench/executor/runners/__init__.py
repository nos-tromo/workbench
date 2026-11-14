"""The tool runners, by tool name; each tool task registers its own."""

from collections.abc import Callable
from typing import Any

from workbench.executor.context import JobContext
from workbench.executor.runners.documents import read_document, search_files

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
}
