"""The test process gets the thread limits every job has, before any test module is imported; and one time factor.

Most runner tests run their code in this process, and importing numpy (which openpyxl does whenever it is installed)
would otherwise start a pool of BLAS threads, one per CPU, none of which blocks a signal. A job never has that pool:
the sandbox gives it ``OPENBLAS_NUM_THREADS=1`` and ``OMP_NUM_THREADS=1`` (``CLEAN_ENV``). The timer tests need it
gone too: a signal one thread holds back is handed to any other thread (``alarm_held`` in
``tests/executor/test_run_python.py``).

``WORKBENCH_TEST_TIME_SCALE`` (1 by default; CI sets 3) says how much slower than a developer's machine the host may
be. Every upper bound a test puts on the time of work it runs, where the bound is within about four times what that
work takes on a developer's machine, is ``scaled``. A deadline the product itself keeps is never scaled: a test that
needs more room for one gives the work a larger budget or a smaller input, and a lower bound (a deadline must not fire
early) stays as it is.
"""

import os
from typing import Final

from workbench.executor.sandbox import CLEAN_ENV

for _name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS"):
    os.environ[_name] = CLEAN_ENV[_name]


def _time_scale(raw: str | None) -> float:
    """Read the time factor.

    Args:
        raw: ``WORKBENCH_TEST_TIME_SCALE``, or ``None``.

    Returns:
        The factor: 1 when unset or blank.

    Raises:
        ValueError: It is not a number of at least 1.
    """
    if raw is None or not raw.strip():
        return 1.0
    value = float(raw)
    if not value >= 1:
        raise ValueError("WORKBENCH_TEST_TIME_SCALE must be a number of at least 1")
    return value


#: How much slower than a developer's machine the host may be.
TIME_SCALE: Final = _time_scale(os.environ.get("WORKBENCH_TEST_TIME_SCALE"))


def scaled(seconds: float) -> float:
    """Widen an upper bound on the time of work a test runs by the host's factor.

    Args:
        seconds: The bound on a developer's machine.

    Returns:
        The bound on this host.
    """
    return seconds * TIME_SCALE
