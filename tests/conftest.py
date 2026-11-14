"""The test process gets the thread limits every job has, before any test module is imported.

Most runner tests run their code in this process, and importing numpy (which openpyxl does whenever it is installed)
would otherwise start a pool of BLAS threads, one per CPU, none of which blocks a signal. A job never has that pool:
the sandbox gives it ``OPENBLAS_NUM_THREADS=1`` and ``OMP_NUM_THREADS=1`` (``CLEAN_ENV``). The timer tests need it
gone too: a signal one thread holds back is handed to any other thread (``alarm_held`` in
``tests/executor/test_run_python.py``).
"""

import os

from workbench.executor.sandbox import CLEAN_ENV

for _name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS"):
    os.environ[_name] = CLEAN_ENV[_name]
