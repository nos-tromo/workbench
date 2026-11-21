"""The MCP door's ``tools/list``, frozen: what each of its callers is told it can call.

Ghostwriter's ten workspace tools and docint's ``run_python`` are written once into ``tests/api/golden/``, on both
revisions the door speaks. A change to a tool's name, description or schema, or to what a caller is offered, fails here
and is made on purpose, by editing the file, never by regenerating it.
"""

import json
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import HD, HG
from tests.api.test_mcp import Headers, result, rpc
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
