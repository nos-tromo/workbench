"""The data volume's layout is one set of names, which the api and the executor share without importing each other."""

import subprocess
import sys

from workbench import layout
from workbench.api import jobs
from workbench.executor import prepare_volume, supervisor


def test_the_api_and_the_executor_find_the_layout_by_the_same_names() -> None:
    """``prepare_volume`` lays out what the store, the queue, retention and the supervisor look for."""
    assert (layout.WORKSPACES, layout.STAGING, layout.JOBS, layout.DATABASE) == (
        "workspaces",
        "staging",
        "jobs",
        "workbench.db",
    )
    assert layout.DERIVED == ".derived"  # the chat's folder of renders; the store reserves the name
    assert jobs.STAGING is layout.STAGING
    assert prepare_volume.WORKSPACES is layout.WORKSPACES
    assert (supervisor.STAGING, supervisor.JOBS) == (layout.STAGING, layout.JOBS)


def test_the_api_imports_nothing_of_the_executor() -> None:
    """The api's image has no executor libraries: none of its modules may load the executor's package."""
    code = (
        "import sys, workbench.api.main, workbench.api.retention, workbench.api.tools; "
        "print(sorted(name for name in sys.modules if name.startswith('workbench.executor')))"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60)
    assert done.stdout.strip() == "[]"
