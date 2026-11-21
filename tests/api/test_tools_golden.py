"""The MCP door's ``tools/list``, frozen: what each of its callers is told it can call.

Ghostwriter's ten workspace tools and docint's ``run_python`` are written once into ``tests/api/golden/``, on both
revisions the door speaks. A change to a tool's name, description or schema, or to what a caller is offered, fails here
and is made on purpose, by editing the file, never by regenerating it. Ghostwriter's files are its list on a call that
takes images (``X-Accept-Images: 1``); without the header it is that list less ``view_file``, ``read_document`` told as
in 0.6.
"""

import json
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import HD, HG, HG_TEXT
from tests.api.test_mcp import Headers, result, rpc
from tests.callers import TEXT_ONLY_READ_DOCUMENT
from workbench import __version__
from workbench.api.mcp_wire import LEGACY_DEFAULT, MODERN, SERVER_INFO

#: Where the frozen answers are.
GOLDEN: Final = Path(__file__).parent / "golden"
#: What stands for the server's own version in the files: it moves with every release, the tool list does not.
VERSION: Final = "<version>"


def frozen(answer: dict[str, Any]) -> str:
    """Write an answer as the files hold it: indented JSON, the modern revision's server version left out.

    Args:
        answer: The ``result`` of a ``tools/list`` answer.

    Returns:
        The text of the file.
    """
    meta = answer.get("_meta")
    if meta is not None:
        assert meta[SERVER_INFO]["version"] == __version__
        answer = {**answer, "_meta": {**meta, SERVER_INFO: {**meta[SERVER_INFO], "version": VERSION}}}
    return json.dumps(answer, indent=2, ensure_ascii=False) + "\n"


@pytest.mark.parametrize(("caller", "headers"), [("ghostwriter", HG), ("docint", HD)])
@pytest.mark.parametrize("era", ["modern", "legacy"])
def test_tools_list_is_frozen(client: TestClient, caller: str, headers: Headers, era: str) -> None:
    """The MCP door lists each caller's tools exactly as the frozen files hold them."""
    version = MODERN if era == "modern" else LEGACY_DEFAULT
    served = frozen(result(rpc(client, "tools/list", version=version, headers=headers)))
    assert served == (GOLDEN / f"{caller}-{era}.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("era", ["modern", "legacy"])
def test_a_ghostwriter_that_takes_no_images_is_listed_what_0_6_listed(client: TestClient, era: str) -> None:
    """Final review I1: without ``X-Accept-Images`` (a Ghostwriter before 0.3.0), nine tools, read_document as in 0.6.

    The schemas stay those of the frozen file, ``shown`` and ``strips`` included: they promise no image.
    """
    version = MODERN if era == "modern" else LEGACY_DEFAULT
    served = json.loads(frozen(result(rpc(client, "tools/list", version=version, headers=HG_TEXT))))
    listed = json.loads((GOLDEN / f"ghostwriter-{era}.json").read_text(encoding="utf-8"))
    tools = [
        {**tool, "description": TEXT_ONLY_READ_DOCUMENT} if tool["name"] == "read_document" else tool
        for tool in listed["tools"]
        if tool["name"] != "view_file"
    ]
    assert served == {**listed, "tools": tools}
    assert len(tools) == 9
