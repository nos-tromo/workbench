"""LibreOfficeKit's harness on any host: the forked child, its exit codes and deadline, the profile, the filter data.

Each case of :func:`~workbench.executor.render.office.run_in_child` forks in a fresh interpreter of its own, so that no
thread of pytest's is forked with it. LibreOffice itself runs only in the executor image
(``tests/executor/test_supervisor.py``).
"""

import json
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from tests.conftest import scaled
from workbench.executor.render import office

_HEAD = """\
import json, os, signal, time
from workbench.executor import readers
from workbench.executor.render import office
"""


def _in_a_fresh_interpreter(body: str) -> Any:
    """Run ``body`` after ``_HEAD`` in ``python -I`` and read the JSON it prints."""
    done = subprocess.run(
        [sys.executable, "-I", "-c", _HEAD + textwrap.dedent(body)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_a_child_returns_its_exit_code() -> None:
    """What ``work`` returns is the child's exit code, and the parent's answer."""
    code = _in_a_fresh_interpreter("print(json.dumps(office.run_in_child(lambda: 6, time.monotonic() + 10)))")
    assert code == 6


def test_a_child_that_raises_exits_70() -> None:
    """Whatever ``work`` raises ends the child with ``EXIT_FAILED``; it never returns into the job's code."""
    code = _in_a_fresh_interpreter(
        """
        def work():
            raise ValueError("an invented failure")
        print(json.dumps(office.run_in_child(work, time.monotonic() + 10)))
        """
    )
    assert code == office.EXIT_FAILED == 70


def test_a_child_past_its_deadline_is_killed_and_reaped() -> None:
    """A child still running at the deadline is killed and reaped before ``ReadTimeout`` comes: no child is left."""
    seen = _in_a_fresh_interpreter(
        """
        began = time.monotonic()
        try:
            office.run_in_child(lambda: time.sleep(30) or 0, began + 0.5)
            outcome = "returned"
        except readers.ReadTimeout:
            outcome = "timed out"
        seconds = time.monotonic() - began
        try:
            os.waitpid(-1, os.WNOHANG)
            left = "a child"
        except ChildProcessError:
            left = "none"
        print(json.dumps({"outcome": outcome, "seconds": seconds, "left": left}))
        """
    )
    assert seen["outcome"] == "timed out"
    assert seen["seconds"] < scaled(2)
    assert seen["left"] == "none"


def test_a_child_ended_by_a_signal_reports_it() -> None:
    """A child that a signal ends answers minus that signal's number."""
    code = _in_a_fresh_interpreter(
        """
        def work():
            os.kill(os.getpid(), signal.SIGKILL)
            return 0
        print(json.dumps(office.run_in_child(work, time.monotonic() + 10)))
        """
    )
    assert code == -9


def test_a_profile_is_seeded_into_tmp(tmp_path: Path) -> None:
    """The image's profile is copied into a new folder of the job's tmp; outside the image one holds the settings."""
    prebuilt = tmp_path / "prebuilt"
    (prebuilt / "user").mkdir(parents=True)
    (prebuilt / "user" / "a.xcu").write_text("<invented/>", encoding="utf-8")
    under = tmp_path / "tmp"
    under.mkdir()

    copied = office.seed_profile(under=under, prebuilt=prebuilt)
    made = office.seed_profile(under=under, prebuilt=tmp_path / "not-there")

    assert copied.parent == under
    assert copied.name.startswith("lo-")
    assert (copied / "user" / "a.xcu").read_text(encoding="utf-8") == "<invented/>"
    assert (prebuilt / "user" / "a.xcu").exists()
    assert made.parent == under
    assert made != copied
    settings = made / "user" / "registrymodifications.xcu"
    assert settings.read_text(encoding="utf-8") == office.PROFILE_SETTINGS
    assert len(ET.parse(settings).getroot().findall("item")) == 9


def test_the_filter_data_is_json() -> None:
    """Both filters' data is LibreOffice's JSON; only Impress's draws hidden slides."""
    impress = json.loads(office.IMPRESS_OPTIONS)
    writer = json.loads(office.WRITER_OPTIONS)
    assert impress["ExportHiddenSlides"]["value"] == "true"
    assert "ExportHiddenSlides" not in writer
