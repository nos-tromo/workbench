"""The runner of ``write_presentation``, which builds a deck in a user's slide master inside the sandboxed job.

The master is a ``.pptx`` or ``.potx`` of the chat; the deck is one new ``.pptx`` in ``out/``, a ``.potx`` giving a
presentation too (:mod:`workbench.executor.presentations.deck`). The runner trusts nothing it is sent: the deck is
checked again with :func:`workbench.decks.deck_problem`, and the name must be a plain file name. The work runs under
the job's time limit, and the answer is the number of slides and the warnings: never a path of the job.
"""

from pathlib import Path
from typing import Any, Final

from workbench import decks
from workbench.executor.context import JobContext
from workbench.executor.runners.templates import LISTED, WARNING_MAX, answered, cut, job_input
from workbench.executor.templates import Refusal
from workbench.paths import PathError, check_filename

#: The extensions of a slide master, and what a file of another kind is told.
MASTERS: Final = frozenset({".pptx", ".potx"})
NOT_A_MASTER: Final = "a deck is built in a .pptx or .potx slide master"

_DEFAULT_BUDGET_S: Final = 60.0


def _output(args: dict[str, Any], ctx: JobContext) -> Path:
    """Find where the deck goes: the name the api chose, in ``out/``.

    Args:
        args: The runner's arguments.
        ctx: The job's surroundings.

    Returns:
        The path to write.

    Raises:
        Refusal: The name is not a plain file name ending in ``.pptx`` (``invalid_value``).
    """
    name = str(args.get("name", ""))
    try:
        check_filename(name)
    except PathError:
        name = ""
    if not name.lower().endswith(".pptx"):
        raise Refusal("invalid_value", "the name is not a plain file name ending in .pptx")
    return ctx.out_dir / name


def _listed(warned: list[str]) -> list[str]:
    """Cut the warnings for the answer: at most :data:`LISTED`, the last saying how many more, each cut too.

    Args:
        warned: Every warning, in order.

    Returns:
        Those the answer lists.
    """
    if len(warned) > LISTED:
        warned = [*warned[: LISTED - 1], f"{len(warned) - LISTED + 1} more warnings are not shown."]
    return [cut(warning, WARNING_MAX) for warning in warned]


def _as_checked(slides: object) -> object:
    """Take the slides as the api checked them: an image block without the ``virtual`` path the api adds for messages.

    Args:
        slides: The runner's ``slides``.

    Returns:
        The slides, each image block without ``virtual``; anything that is not a list of slides as it is.
    """
    if not isinstance(slides, list):
        return slides
    return [
        {**slide, "content": [_without_virtual(block) for block in slide["content"]]}
        if isinstance(slide, dict) and isinstance(slide.get("content"), list)
        else slide
        for slide in slides
    ]


def _without_virtual(block: object) -> object:
    """Leave out an image block's ``virtual``, which the runner does not trust: it names a picture by its own path.

    Args:
        block: A block of a slide.

    Returns:
        The block, or for an image block a copy without ``virtual``.
    """
    if isinstance(block, dict) and decks.block_kind(block) == "image":
        return {key: value for key, value in block.items() if key != "virtual"}
    return block


def write_presentation(args: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
    """Build a deck in a slide master into a new ``.pptx`` in ``out/``.

    Args:
        args: ``path`` (the master, relative to the chat folder); ``name`` (the new file's, with ``.pptx``);
            ``slides`` (each a ``layout`` and, optionally, ``title``, ``subtitle``, ``content``, ``notes`` and
            ``sources``; a file source's ``path`` is relative to the chat folder and ``virtual`` is how the notes
            name it; an image block's ``image`` is relative to the chat folder too, its ``virtual`` left aside);
            ``include_sources``, whether the notes end with the sources; and ``budget_s``, the seconds the work may
            take.
        ctx: The job's surroundings.

    Returns:
        ``slides`` (how many the deck has) and ``warnings``; or an ``error`` result.
    """
    budget = float(args.get("budget_s", _DEFAULT_BUDGET_S))

    def work() -> dict[str, Any]:
        from workbench.executor.presentations import deck, master

        why = decks.deck_problem(_as_checked(args.get("slides")))
        if why is not None:
            raise Refusal("invalid_value", why)
        output = _output(args, ctx)
        path, _ = job_input(args, ctx)
        if path.suffix.lower() not in MASTERS:
            shown = path.suffix.lower() or "a file without an extension"
            raise Refusal("unsupported_template", f"{NOT_A_MASTER}; {shown} is not one")
        prs = master.open_master(path, ctx)
        warned: list[str] = []
        count = deck.build(prs, args, ctx, warned)
        prs.save(str(output))
        return {"slides": count, "warnings": _listed(warned)}

    return answered(work, budget, unsupported=NOT_A_MASTER)
