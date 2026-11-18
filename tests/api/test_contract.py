"""The terminal handshake Open WebUI runs, and the key and header checks."""

import dataclasses
import io

import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import KEY, KEY_ONLY, H, unrecorded
from workbench.api.auth import CallerRegistry
from workbench.api.contract import system_prompt
from workbench.api.registry import WORKSPACE_TOOLS
from workbench.api.store import Store
from workbench.config import ConfigError, Settings
from workbench.models import Principal


def _without(headers: dict[str, str], name: str) -> dict[str, str]:
    """Copy ``headers`` without ``name``."""
    return {key: value for key, value in headers.items() if key != name}


def test_policies_is_not_found_so_open_webui_sees_a_plain_terminal(client: TestClient) -> None:
    """A 2xx there makes Open WebUI treat workbench as an orchestrator."""
    assert client.get("/api/v1/policies", headers=H).status_code == 404


def test_config_announces_a_system_prompt(client: TestClient) -> None:
    """Open WebUI fetches ``/system`` only when the config says the server has one."""
    r = client.get("/api/config", headers=H)
    assert r.status_code == 200
    assert r.json()["features"]["system"] is True


def test_config_and_system_need_only_the_key(client: TestClient) -> None:
    """The connection check sends no user headers."""
    assert client.get("/api/config", headers=KEY_ONLY).status_code == 200
    assert client.get("/system", headers=KEY_ONLY).status_code == 200


def test_system_prompt_names_the_retention_period(client: TestClient) -> None:
    """The assistant is told how long files live, and that projects belong in docint."""
    prompt = client.get("/system", headers=H).json()["prompt"]
    assert "7 days" in prompt
    assert "docint" in prompt
    assert "{retention_days}" not in prompt


def test_system_prompt_follows_the_configured_period(settings: Settings) -> None:
    """The period is filled in from the settings, and a template placeholder is left alone."""
    prompt = system_prompt(dataclasses.replace(settings, retention_days=3))
    assert "3 days" in prompt
    assert "7 days" not in prompt
    assert "{{ field }}" in prompt


def test_system_prompt_carries_the_behaviour_the_spec_asks_for(settings: Settings) -> None:
    """Plan and confirm, cite, never invent, OCR, templates and where Python finds its files."""
    prompt = system_prompt(settings).lower()
    for phrase in ("plan", "confirm", "cite", "never invent", "ocr", "docint", "template", "/workspace/"):
        assert phrase in prompt, phrase


def test_system_prompt_tells_python_where_results_go_and_to_use_threads(settings: Settings) -> None:
    """Results in ``out/``, plots as files, threads instead of processes (R28), and the job id as a source."""
    prompt = system_prompt(settings)
    for phrase in ("`out/`", "savefig", "display_file", "threads", "multiprocessing", "job_id"):
        assert phrase in prompt, phrase


def test_system_prompt_says_how_new_tables_and_documents_are_made(settings: Settings) -> None:
    """With the two output tools, and with run_python for a large table."""
    prompt = system_prompt(settings)
    for phrase in ("`write_table`", "`write_document`", "large table"):
        assert phrase in prompt, phrase


def test_system_prompt_says_who_sees_the_files_without_promising_nobody(settings: Settings) -> None:
    """R2 M5: only this user reaches the workspace; tool results go into the chat and to the configured model."""
    prompt = system_prompt(settings)
    assert "shared with nobody" not in prompt
    for phrase in ("only this user", "part of this chat", "the operator configured"):
        assert phrase in prompt, phrase


@pytest.mark.parametrize("user", ["../x", "", " ", "a b", "x/y", ".hidden", "a" * 129])
def test_user_header_is_checked(client: TestClient, user: str) -> None:
    """The user id becomes a folder name, so anything that is not a plain id is refused."""
    r = client.get("/files/cwd", headers={**H, "X-User-Id": user})
    assert r.status_code == 400
    assert r.json() == {"detail": "missing or invalid user"}


def test_user_header_is_required(client: TestClient) -> None:
    """Every route but the handshake ones needs a user."""
    r = client.get("/files/cwd", headers=_without(H, "X-User-Id"))
    assert r.status_code == 400
    assert r.json() == {"detail": "missing or invalid user"}


@pytest.mark.parametrize("session", ["../x", "", "a b", "a/b", "a" * 129])
def test_session_header_is_checked(client: TestClient, session: str) -> None:
    """The chat id becomes a folder name too."""
    r = client.get("/files/cwd", headers={**H, "X-Session-Id": session})
    assert r.status_code == 400
    assert r.json() == {"detail": "invalid session"}


def test_cwd_is_the_chats_folder(client: TestClient) -> None:
    """Open WebUI starts the file browser in the chat's folder, below the user's home."""
    r = client.get("/files/cwd", headers=H)
    assert r.status_code == 200
    assert r.json() == {"cwd": "/workspace/chat-1", "home": "/workspace"}


def test_requests_without_a_session_use_the_default_folder(client: TestClient) -> None:
    """A request outside any chat still has a folder."""
    r = client.get("/files/cwd", headers=_without(H, "X-Session-Id"))
    assert r.json() == {"cwd": "/workspace/default", "home": "/workspace"}


def test_local_chat_ids_are_accepted(client: TestClient) -> None:
    """Open WebUI names a chat that is not saved yet ``local:<id>``."""
    r = client.get("/files/cwd", headers={**H, "X-Session-Id": "local:abc123"})
    assert r.status_code == 200
    assert r.json()["cwd"] == "/workspace/local:abc123"


def test_cwd_does_not_create_the_folder(client: TestClient, settings: Settings) -> None:
    """Looking is not writing: the folder appears with the first file."""
    client.get("/files/cwd", headers=H)
    assert not (settings.data_dir / "workspaces").exists()


def test_post_cwd_stays_inside_the_root(client: TestClient) -> None:
    """Moving to another folder of one's own works; leaving the root does not."""
    ok = client.post("/files/cwd", json={"path": "/workspace/chat-2"}, headers=H)
    assert ok.status_code == 200
    assert ok.json() == {"cwd": "/workspace/chat-2"}
    assert client.post("/files/cwd", json={"path": "/workspace/chat-1/../chat-2"}, headers=H).json() == {
        "cwd": "/workspace/chat-2"
    }
    for outside in ("/etc", "/workspace/../etc", "/workspace/chat-1/../../x", "relative", "/workspace/a\x00b"):
        r = client.post("/files/cwd", json={"path": outside}, headers=H)
        assert r.status_code == 400, outside


def test_post_cwd_needs_a_folder_that_exists_below_the_chat_folder(client: TestClient) -> None:
    """A folder inside a chat that was never made is not a place to be."""
    r = client.post("/files/cwd", json={"path": "/workspace/chat-2/reports"}, headers=H)
    assert r.status_code == 404
    assert r.json() == {"detail": "not found"}


def test_agents_md_is_absent(client: TestClient) -> None:
    """Open WebUI probes ``<home>/AGENTS.md`` for every chat; it does not exist."""
    r = client.get("/files/read", params={"path": "/workspace/AGENTS.md"}, headers=H)
    assert r.status_code == 404
    assert r.json() == {"detail": "not found"}


def test_a_file_that_is_not_stored_is_not_found(client: TestClient) -> None:
    """The same answer inside a chat folder."""
    r = client.get("/files/read", params={"path": "/workspace/chat-1/AGENTS.md"}, headers=H)
    assert r.status_code == 404
    assert r.json() == {"detail": "not found"}


def test_read_refuses_paths_outside_the_root(client: TestClient) -> None:
    """Only a file at home is simply absent; a path that leaves the root is an error."""
    r = client.get("/files/read", params={"path": "/workspace/../etc/passwd"}, headers=H)
    assert r.status_code == 400


def test_read_of_a_stored_text_file_returns_its_content(client: TestClient, store: Store) -> None:
    """The file browser serves ``/files/read``: a text file answers its content, any other type 415."""
    me = Principal("open-webui", "user-a", "chat-1")
    store.add_upload(
        me, "/workspace/chat-1", "notes.txt", io.BytesIO(b"hello"), max_bytes=100, quota_bytes=1000, record=unrecorded
    )
    store.add_upload(
        me, "/workspace/chat-1", "q.xlsx", io.BytesIO(b"PK"), max_bytes=100, quota_bytes=1000, record=unrecorded
    )
    r = client.get("/files/read", params={"path": "/workspace/chat-1/notes.txt"}, headers=H)
    assert (r.status_code, r.json()) == (200, {"content": "hello"})
    assert client.get("/files/read", params={"path": "/workspace/chat-1/q.xlsx"}, headers=H).status_code == 415


def test_skills_answer_empty(client: TestClient) -> None:
    """Open WebUI does not catch a connection error here, so the routes must answer."""
    assert client.get("/skills", headers=H).json() == []
    r = client.get("/skills/read", params={"name": "x"}, headers=H)
    assert r.status_code == 404
    assert r.json() == {"detail": "not found"}


def test_callers_are_a_registry_with_open_webui_first(settings: Settings) -> None:
    """Version 1 has one caller that may use every tool; version 2 adds docint without restructuring."""
    registry = CallerRegistry.from_settings(settings)
    caller = registry.authenticate(KEY)
    assert caller is not None
    assert (caller.name, caller.key, caller.tools) == ("open-webui", KEY, WORKSPACE_TOOLS)
    assert {"list_files", "read_document", "run_python", "fill_template"} <= WORKSPACE_TOOLS
    assert registry.authenticate("wrong") is None
    assert registry.authenticate("") is None
    assert KEY not in repr(caller)


def test_the_registry_needs_a_key(settings: Settings) -> None:
    """The executor's settings carry no key, and the api refuses to build a registry from them."""
    with pytest.raises(
        ConfigError, match="OPEN_WEBUI_WORKBENCH_KEY, DOCINT_WORKBENCH_KEY or GHOSTWRITER_WORKBENCH_KEY"
    ):
        CallerRegistry.from_settings(
            dataclasses.replace(settings, open_webui_key=None, docint_key=None, ghostwriter_key=None)
        )
