"""The documentation keeps up with the code: every setting, every tool and every audit event is in it.

Each name is looked for in backticks, as the documents write code, so a word in a sentence does not count.
"""

import io
import re
from pathlib import Path

from workbench import __version__
from workbench.api.audit import CONTINUATION, OPERATIONS, SERVICE, AuditLog
from workbench.api.main import create_app
from workbench.api.openapi import tool_openapi
from workbench.api.store import Store
from workbench.config import load_settings
from workbench.paths import Workspaces
from workbench.utils.audit_event import AuditEmitter

ROOT = Path(__file__).resolve().parents[1]

_SETTING = re.compile(r"(?:# )?([A-Z][A-Z0-9_]*)=")

#: A literal system event in the api's code: ``record_system("<action>", "<operation>"`` or
#: ``emit("<action>", "<operation>"``, the call's first two arguments, spread over lines or not.
_SYSTEM_EVENT = re.compile(r"(?:record_system|emit)\(\s*\"[a-z_]+\",\s*\"([a-z_]+(?:\.[a-z_]+)+)\"")


def doc(name: str) -> str:
    """Read a document of ``docs/``; a missing one fails the test by its name."""
    path = ROOT / "docs" / name
    assert path.is_file(), f"docs/{name} is missing"
    return path.read_text(encoding="utf-8")


def test_every_env_variable_is_documented() -> None:
    """Each setting of ``.env.example``, set or shown commented out, is in ``docs/configuration.md``."""
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    names = [found.group(1) for line in lines if (found := _SETTING.match(line))]
    assert "OPEN_WEBUI_WORKBENCH_KEY" in names and "WORKBENCH_RETENTION_DAYS" in names
    text = doc("configuration.md")
    assert [name for name in names if f"`{name}`" not in text] == []


def test_every_tool_is_in_the_permission_matrix(tmp_path: Path) -> None:
    """Each ``operationId`` the model is offered has its row in the matrix of ``docs/permissions.md``."""
    settings = load_settings({"OPEN_WEBUI_WORKBENCH_KEY": "k" * 32, "WORKBENCH_DATA_DIR": str(tmp_path)}, role="api")
    store = Store(tmp_path / "workbench.db", Workspaces(tmp_path))
    audit = AuditLog(AuditEmitter(SERVICE, __version__, stream=io.StringIO(), heartbeat_interval_s=0))
    app = create_app(settings, store=store, audit=audit)
    spec = tool_openapi(app, app.state.callers.get("open-webui"))
    tools = [op["operationId"] for item in spec["paths"].values() for op in item.values() if "operationId" in op]
    assert len(tools) == 9
    rows = {line.split("|")[1].strip() for line in doc("permissions.md").splitlines() if line.startswith("| `")}
    assert [tool for tool in tools if f"`{tool}`" not in rows] == []


def system_operations() -> set[str]:
    """The operations the api records as the system, read from its code: every literal one it emits or records."""
    found: set[str] = set()
    for path in (ROOT / "workbench" / "api").glob("*.py"):
        found.update(_SYSTEM_EVENT.findall(path.read_text(encoding="utf-8")))
    return found


def test_every_audit_key_is_documented() -> None:
    """Each operation an event key or the system is recorded under is in ``docs/audit.md``, and the continuations.

    The system's operations are found in the api's code, so a new one fails here until it is documented.
    """
    text = doc("audit.md")
    assert len(OPERATIONS) >= 12
    assert [operation for _, operation in OPERATIONS.values() if f"`{operation}`" not in text] == []
    system = system_operations()
    assert {"retention.sweep", "config.audit"} <= system
    assert [operation for operation in sorted(system) if f"`{operation}`" not in text] == []
    assert f"`<operation>{CONTINUATION}`" in text
