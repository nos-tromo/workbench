"""The job entry point: ``python -I -m workbench.executor.job <spec.json> <result.json>``.

The spec file is the only input; the process reads no environment. The working
directory is the job's working folder. The runner's answer, or its error, is written
to the result path, in the job's own ``home/``: the job folder that holds the spec is
root's, and the job cannot add a name to it.
"""

import json
import sys
from collections.abc import Sequence
from pathlib import Path

from workbench.executor.runners import RUNNERS, JobContext
from workbench.executor.timer import TimeLimit


def main(argv: Sequence[str]) -> int:
    """Run the job a spec describes and write its result.

    Args:
        argv: The command line; ``argv[1]`` is the path of the spec, a JSON object with
            ``tool``, ``args``, ``chat_id``, ``max_chars`` and ``file_mb``; ``argv[2]`` is where the result goes,
            next to the spec when it is not given.

    Returns:
        ``0`` once ``result.json`` is written, whether the runner succeeded, raised (``SystemExit`` and
        ``KeyboardInterrupt`` too), returned something other than a JSON object, or let a time limit
        (:class:`~workbench.executor.timer.TimeLimit`, which is not an ``Exception``) escape; ``1`` if no
        result could be written at all.
    """
    spec_path = Path(argv[1])
    result_path = Path(argv[2]) if len(argv) > 2 else spec_path.with_name("result.json")
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
    except TimeLimit:
        # A runner handles its own limits; one that gets this far would otherwise end the process with no result.
        payload = json.dumps({"error": "read_timeout: the work ran past its time limit"})
    except BaseException as exc:  # SystemExit and KeyboardInterrupt too: the job always answers
        payload = json.dumps({"error": f"{type(exc).__name__}: {exc}"})
    return _write(result_path, payload)


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
