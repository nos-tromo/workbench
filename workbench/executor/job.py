"""The job entry point: ``python -I -m workbench.executor.job <spec.json> <result.json>``.

The spec file is the only input; the process reads no environment. The working
directory is the job's working folder. The runner's answer, or its error, is written
to the result path, in the job's own ``home/``: the job folder that holds the spec is
root's, and the job cannot add a name to it. When the spec asks for previews, the
table previews of the outputs are written beside it once the runner has answered
(``workbench.executor.preview``).
"""

import json
import sys
import time
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path

from workbench.executor.preview import PREVIEW_TOTAL_S, PREVIEWS_FILE, previews_of
from workbench.executor.runners import RUNNERS, JobContext
from workbench.executor.timer import TimeLimit


def main(argv: Sequence[str]) -> int:
    """Run the job a spec describes and write its result.

    Args:
        argv: The command line; ``argv[1]`` is the path of the spec, a JSON object with
            ``tool``, ``args``, ``chat_id``, ``max_chars``, ``file_mb`` and ``previews`` (missing is false);
            ``argv[2]`` is where the result goes, next to the spec when it is not given.

    Returns:
        ``0`` once ``result.json`` is written, whether the runner succeeded, raised (``SystemExit`` and
        ``KeyboardInterrupt`` too), returned something other than a JSON object, or let a time limit
        (:class:`~workbench.executor.timer.TimeLimit`, which is not an ``Exception``) escape; ``1`` if no
        result could be written at all. The previews never change it.
    """
    spec_path = Path(argv[1])
    result_path = Path(argv[2]) if len(argv) > 2 else spec_path.with_name("result.json")
    previews_for: Path | None = None
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        work_dir = Path.cwd()
        ctx = JobContext(
            work_dir=work_dir,
            out_dir=work_dir / "out",
            chat_id=spec["chat_id"],
            max_chars=spec["max_chars"],
            file_mb=spec["file_mb"],
        )
        answer = RUNNERS[spec["tool"]](spec["args"], ctx)
        if not isinstance(answer, dict):
            raise TypeError("the runner's answer is not an object")
        payload = json.dumps(answer)
        if spec.get("previews") and set(answer) != {"error"}:  # a failed tool's outputs are not kept
            previews_for = ctx.out_dir
    except TimeLimit:
        # A runner handles its own limits; one that gets this far would otherwise end the process with no result.
        payload = json.dumps({"error": "read_timeout: the work ran past its time limit"})
    except BaseException as exc:  # SystemExit and KeyboardInterrupt too: the job always answers
        payload = json.dumps({"error": f"{type(exc).__name__}: {exc}"})
    status = _write(result_path, payload)
    if status == 0 and previews_for is not None:
        _write_previews(previews_for, result_path.with_name(PREVIEWS_FILE))
    return status


def _write_previews(out_dir: Path, path: Path) -> None:
    """Write the previews of the outputs, or no file at all if anything fails.

    Args:
        out_dir: The job's ``out/``.
        path: Where the previews go, beside the result.
    """
    try:
        found = previews_of(out_dir, time.monotonic() + PREVIEW_TOTAL_S)
        payload = json.dumps({name: preview.model_dump() for name, preview in found.items()}, ensure_ascii=False)
        path.write_text(payload, encoding="utf-8")
    except BaseException:  # the previews never cost the result: a time limit or an exit too
        with suppress(OSError):
            path.unlink(missing_ok=True)


def _write(path: Path, payload: str) -> int:
    """Write the result, or a short error in its place if it cannot be written.

    Code a job runs can fill the volume or lower its own file-size limit; the supervisor would then find a torn
    result, or none.

    Args:
        path: Where the result goes.
        payload: The result, JSON.

    Returns:
        ``0`` once a result is written, ``1`` if not even the short error could be.
    """
    try:
        path.write_text(payload, encoding="utf-8")
    except OSError as exc:
        try:
            path.write_text(json.dumps({"error": f"result_unwritable: {type(exc).__name__}"}), encoding="utf-8")
        except OSError:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
