"""What a runner is given besides its arguments."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class JobContext:
    """The job's surroundings, read from its spec and its working directory.

    Attributes:
        work_dir: The job directory: the input files are in it, and it is the process's cwd.
        out_dir: Where the job writes its outputs, ``out/`` below ``work_dir``.
        chat_id: Chat folder the job works on.
        max_chars: Largest result the runner may return, in characters.
        file_mb: Largest single file the job may write, in megabytes; also the base of the
            zip-bomb guard (20 times this).
    """

    work_dir: Path
    out_dir: Path
    chat_id: str
    max_chars: int
    file_mb: int
