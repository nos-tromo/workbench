"""The job entry point: ``python -I -m workbench.executor.job <spec.json>``.

The spec file is the only input; the process reads no environment. The working
directory is the job directory. The runner's answer, or its error, is written to
``result.json`` next to the spec.
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
            ``tool``, ``args``, ``chat_id``, ``max_chars`` and ``file_mb``.

    Returns:
        ``0`` once ``result.json`` is written, whether the runner succeeded, raised, or let a time limit
        (:class:`~workbench.executor.timer.TimeLimit`, which is not an ``Exception``) escape.
    """
    spec_path = Path(argv[1])
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
        payload = json.dumps(RUNNERS[spec["tool"]](spec["args"], ctx))
    except TimeLimit:
        # A runner handles its own limits; one that gets this far would otherwise end the process with no result.
        payload = json.dumps({"error": "read_timeout: the work ran past its time limit"})
    except Exception as exc:
        payload = json.dumps({"error": f"{type(exc).__name__}: {exc}"})
    spec_path.with_name("result.json").write_text(payload, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
