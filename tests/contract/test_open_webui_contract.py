"""Open WebUI v0.13.3 with workbench as its terminal server, checked on the running contract stack.

``make test-contract`` brings up ``contract/compose.yaml``: workbench's test stack and the Open WebUI image
open-webui-service deploys, with the connection production uses. The tests sign up synthetic accounts, then call
Open WebUI as its browser does, so every workbench path goes through Open WebUI's proxy, ``/api/v1/terminals/<id>/``.
The browser is emulated, not observed: the tests send the calls the pinned image's frontend makes
(``src/lib/apis/terminal/index.ts``), and no real browser runs here.
What Open WebUI passes on is read back where workbench shows it: ``/files/cwd`` names the chat ``X-Session-Id``
carried, and workbench's audit events name ``X-User-Id`` as their actor and that chat as their folder.

One chat also runs for real, against the stack's stub model (``contract/stub_model.py``): Open WebUI then calls
workbench itself, server side, to load the tools, for the per-chat handshake and for the model's tool call. That call
is seen where workbench records it, in its audit events; what Open WebUI gave the model is seen in the stub's log.
"""

import json
import os
import secrets
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import httpx
import pytest

from tests.fixtures import make_fixtures as mk

pytestmark = pytest.mark.contract

PROJECT = os.environ.get("WORKBENCH_CONTRACT_PROJECT", "workbench-contract")
OPEN_WEBUI = os.environ.get("WORKBENCH_CONTRACT_OPEN_WEBUI", "http://127.0.0.1:18180")

#: The connection's id in ``TERMINAL_SERVER_CONNECTIONS``, and where Open WebUI proxies it.
TERMINAL: Final = "workbench"
PROXY: Final = f"/api/v1/terminals/{TERMINAL}"

#: Every workbench path these tests make Open WebUI call: through its proxy, as its browser does, and the two of the
#: admin's verify call (``/api/v1/policies``, which must not answer 2xx, then ``/api/config``). Every proxied call goes
#: through :meth:`Account.terminal`, which refuses a path not listed here, so an image whose proxy no longer gets to one
#: fails on it, by name. The browser's calls are the test's own, made as the pinned image's frontend makes them; a
#: frontend that changed its paths would not fail here. The two verify paths never go through the helper: Open WebUI
#: calls them itself, and only verify's answer is checked (:func:`test_verify_sees_a_plain_terminal`).
TERMINAL_PATHS: Final = (
    "/api/v1/policies",
    "/api/config",
    "/files/cwd",
    "/files/list",
    "/files/upload",
    "/files/view",
    "/files/read",
    "/files/display",
    "/openapi.json",
    "/system",
    "/tools/list_files",
    "/tools/read_document",
)

#: The paths a chat makes Open WebUI call on workbench itself, server side, in order: loading the tools
#: (``/openapi.json``, key only; ``/api/config``, then ``/system`` for the prompt), the per-chat handshake
#: (``/files/cwd``, then ``/files/read`` of ``<home>/AGENTS.md``), and the model's tool call. The chat test pins
#: ``/openapi.json`` (the model is offered the nine tools), ``/api/config`` and ``/system`` (workbench's prompt reaches
#: the model) and ``/tools/read_document`` (its audit event, its answer back to the model). The handshake leaves no
#: trace it can see: the cwd only ever describes a ``run_command`` tool workbench has none of, and workbench answers
#: 404 for ``<home>/AGENTS.md`` (files live in chat folders), so those two stay unpinned. Documentation only: no test
#: reads this list, so nothing fails on it.
CHAT_PATHS: Final = ("/openapi.json", "/api/config", "/system", "/files/cwd", "/files/read", "/tools/read_document")

#: The stack's one model (contract/stub_model.py) and the text it answers with once a tool has answered.
STUB_MODEL: Final = "stub-model"
STUB_ANSWER: Final = "The stub model read the file."

#: The tools of version 1, by ``operationId``: exactly what the model is offered.
TOOLS: Final = {
    "list_files",
    "read_document",
    "search_files",
    "run_python",
    "inspect_template",
    "fill_template",
    "write_table",
    "write_document",
    "display_file",
}

#: What every answer of ``/files/view`` carries, refusals included (workbench/api/hardening.py).
VIEW_HEADERS: Final = {
    "content-security-policy": "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'",
    "x-content-type-options": "nosniff",
    "cross-origin-resource-policy": "same-origin",
}

CSV: Final = b"item,total\nDredging,118\nPiers,351.5\n"

#: Where in Open WebUI's log an audit record comes from: each is one loguru line, its record as JSON at the end.
AUDIT_WRITER: Final = "| open_webui.utils.audit:write:"


def docker(*args: str) -> str:
    """Run the docker CLI and return what it printed on stdout.

    Args:
        *args: The arguments after ``docker``.

    Returns:
        Its standard output.
    """
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True, timeout=120).stdout


def container_of(service: str) -> str:
    """Find the one container of a service of the contract project.

    Args:
        service: The compose service.

    Returns:
        Its id.
    """
    ids = docker(
        "ps",
        "--quiet",
        "--filter",
        f"label=com.docker.compose.project={PROJECT}",
        "--filter",
        f"label=com.docker.compose.service={service}",
    ).split()
    if len(ids) != 1:
        pytest.fail(
            f"expected one running {service} container of project {PROJECT}, found {len(ids)}: run make test-contract"
        )
    return ids[0]


def workbench_events() -> list[dict[str, Any]]:
    """Read workbench's audit events so far: the JSON lines of the api container's standard output."""
    lines = docker("logs", container_of("api")).splitlines()
    return [event for line in lines if line.startswith("{") and "operation" in (event := json.loads(line))]


def upload_events(chat: str) -> list[dict[str, Any]]:
    """Read workbench's upload events of one chat folder so far."""
    return [
        e for e in workbench_events() if e["operation"] == "file.upload" and e["target"]["container"] == f"chat:{chat}"
    ]


def open_webui_audit_uris() -> list[str]:
    """Read the request URIs of Open WebUI's own audit records so far, in order."""
    lines = docker("logs", container_of("open-webui")).splitlines()
    return [str(json.loads(line[line.index("{") :])["request_uri"]) for line in lines if AUDIT_WRITER in line]


def open_webui_files(listing: Any) -> list[Any]:
    """The files of Open WebUI's own ``GET /api/v1/files/``, a list or a page of one."""
    return listing if isinstance(listing, list) else list(listing.get("items", listing.get("data", [])))


def stub_requests(chat: str) -> list[dict[str, Any]]:
    """Read what Open WebUI sent the stub model for one chat: one JSON line per request, in order."""
    lines = docker("logs", container_of("stub-model")).splitlines()
    return [request for line in lines if line.startswith("{") and (request := json.loads(line))["chat"] == chat]


def eventually(read: Callable[[], list[Any]], count: int, seconds: float = 30.0) -> list[Any]:
    """Read again until ``read`` finds at least ``count`` items, or the time is up; then the last reading.

    A container's log is read through docker, whose copier may not have written a line yet when the HTTP answer that
    caused it has come back; every read of an audit event goes through here.
    """
    deadline = time.monotonic() + seconds
    while len(found := read()) < count and time.monotonic() < deadline:
        time.sleep(0.5)
    return found


@dataclass(frozen=True)
class Account:
    """A synthetic Open WebUI account and a client that calls Open WebUI as it.

    Attributes:
        id: Open WebUI's id of the account, which it sends on as ``X-User-Id``.
        client: A client of Open WebUI holding the account's token.
    """

    id: str
    client: httpx.Client

    def terminal(
        self, method: str, path: str, *, chat: str | None = None, chat_route: str | None = None, **kwargs: Any
    ) -> httpx.Response:
        """Call a workbench path through Open WebUI's proxy, as its browser does.

        Args:
            method: The HTTP method.
            path: A path of :data:`TERMINAL_PATHS`.
            chat: The chat the browser names in ``X-Session-Id``; none if ``None``.
            chat_route: A chat to name in the proxy's own path instead, ``…/chats/<chat>/<path>``.
            **kwargs: What ``httpx`` takes besides.

        Returns:
            The answer, as the browser gets it.
        """
        assert path in TERMINAL_PATHS, f"{path} is not in TERMINAL_PATHS"
        headers = dict(kwargs.pop("headers", {}))
        if chat is not None:
            headers["X-Session-Id"] = chat
        route = PROXY if chat_route is None else f"{PROXY}/chats/{chat_route}"
        return self.client.request(method, f"{route}{path}", headers=headers, **kwargs)

    def upload(self, chat: str, name: str, data: bytes) -> httpx.Response:
        """Upload a file into a chat folder through the proxy, as an attachment of that chat goes."""
        files = {"file": (name, data)}
        r = self.terminal("POST", "/files/upload", chat=chat, params={"directory": f"/workspace/{chat}"}, files=files)
        assert r.status_code == 200, r.text
        return r


def _session(answer: httpx.Response) -> Account:
    """Hold the token Open WebUI answered a sign-up or a sign-in with."""
    assert answer.status_code == 200, answer.text
    signed = answer.json()
    client = httpx.Client(base_url=OPEN_WEBUI, timeout=60, headers={"Authorization": f"Bearer {signed['token']}"})
    return Account(id=signed["id"], client=client)


@pytest.fixture(scope="module")
def admin() -> Iterator[Account]:
    """The first account, which Open WebUI makes an admin: ``admin@example.invalid``, a password of this run's.

    Its sign-up closes sign-up (``ui.enable_signup``); later accounts are the admin's to make.
    """
    form = {"name": "Synthetic Admin", "email": "admin@example.invalid", "password": secrets.token_urlsafe(24)}
    account = _session(httpx.post(f"{OPEN_WEBUI}/api/v1/auths/signup", json=form, timeout=60))
    try:
        assert account.client.get("/api/v1/auths/").json()["role"] == "admin"
        yield account
    finally:
        account.client.close()


@pytest.fixture(scope="module")
def user_b(admin: Account) -> Iterator[Account]:
    """A second account, a plain user the admin makes: ``user-b@example.invalid``, signed in with its own password."""
    form = {"name": "Synthetic User B", "email": "user-b@example.invalid", "password": secrets.token_urlsafe(24)}
    made = admin.client.post("/api/v1/auths/add", json={**form, "role": "user"})
    assert made.status_code == 200, made.text
    signin = {"email": form["email"], "password": form["password"]}
    account = _session(httpx.post(f"{OPEN_WEBUI}/api/v1/auths/signin", json=signin, timeout=60))
    try:
        assert account.client.get("/api/v1/auths/").json()["role"] == "user"
        yield account
    finally:
        account.client.close()


@pytest.fixture
def chat() -> str:
    """A chat id as Open WebUI makes them; the proxy forwards it without looking it up."""
    return str(uuid.uuid4())


@pytest.fixture(scope="module")
def key() -> str:
    """The caller key this run made, from the api's environment."""
    info = json.loads(docker("inspect", container_of("api")))[0]
    env = dict(entry.partition("=")[::2] for entry in info["Config"]["Env"])
    return env["OPEN_WEBUI_WORKBENCH_KEY"]


# --- the connection -----------------------------------------------------------------------------------------------


def test_verify_sees_a_plain_terminal(admin: Account, key: str) -> None:
    """The admin's check of the connection: ``/api/v1/policies`` is not found, ``/api/config`` answers; a terminal.

    Open WebUI calls workbench itself here, not through its proxy, with the key alone.
    """
    body = {"url": "http://workbench-api:8000", "key": key, "auth_type": "bearer"}
    r = admin.client.post("/api/v1/configs/terminal_servers/verify", json=body)
    assert (r.status_code, r.json()) == (200, {"status": True, "type": "terminal"})


def test_every_user_sees_the_connection_with_filesystem_uploads(user_b: Account) -> None:
    """The grant for ``*`` reaches a plain user, and chat attachments go to workbench."""
    r = user_b.client.get("/api/v1/terminals/")
    assert r.status_code == 200, r.text
    (connection,) = r.json()
    assert (connection["id"], connection["name"]) == (TERMINAL, "Workbench")
    assert connection["config"] == {"chat_uploads": "filesystem"}
    assert connection["contexts"] == {}


def test_spec_through_the_proxy_lists_exactly_the_tools(admin: Account) -> None:
    """The model is offered the operations of ``/openapi.json``: these nine, and no other."""
    r = admin.terminal("GET", "/openapi.json")
    assert r.status_code == 200, r.text
    operations = {
        op["operationId"] for item in r.json()["paths"].values() for op in item.values() if "operationId" in op
    }
    assert operations == TOOLS


# --- files and tools through the proxy ----------------------------------------------------------------------------


def test_upload_list_view_through_the_proxy(admin: Account, chat: str) -> None:
    """A synthetic CSV goes in, is listed and comes back byte for byte; workbench saw Open WebUI's user and the chat."""
    r = admin.upload(chat, "quarterly-figures.csv", CSV)
    assert r.status_code == 200, r.text
    path = f"/workspace/{chat}/quarterly-figures.csv"
    assert r.json() == {"path": path, "size": len(CSV)}
    cwd = admin.terminal("GET", "/files/cwd", chat=chat)
    assert cwd.json() == {"cwd": f"/workspace/{chat}", "home": "/workspace"}
    listed = admin.terminal("GET", "/files/list", chat=chat, params={"directory": f"/workspace/{chat}"})
    assert [(e["name"], e["type"], e["size"]) for e in listed.json()["entries"]] == [
        ("quarterly-figures.csv", "file", len(CSV))
    ]
    viewed = admin.terminal("GET", "/files/view", chat=chat, params={"path": path})
    assert (viewed.status_code, viewed.content) == (200, CSV)
    read = admin.terminal("GET", "/files/read", chat=chat, params={"path": path})
    assert read.json() == {"content": CSV.decode()}
    (upload,) = eventually(lambda: upload_events(chat), 1)
    assert upload["actor"] == {"user": admin.id, "via": "service"}
    stored = admin.client.get("/api/v1/files/")
    assert stored.status_code == 200, stored.text
    assert open_webui_files(stored.json()) == []  # R2 M9: Open WebUI keeps nothing of the upload itself


def test_tool_call_through_the_proxy(admin: Account, chat: str) -> None:
    """``read_document`` through the proxy answers the CSV's text, as the job in the executor read it."""
    admin.upload(chat, "quarterly-figures.csv", CSV)
    path = f"/workspace/{chat}/quarterly-figures.csv"
    r = admin.terminal("POST", "/tools/read_document", chat=chat, json={"path": path})
    assert r.status_code == 200, r.text
    read = r.json()
    assert (read["path"], read["kind"], read["truncated"]) == (path, "csv", False)
    assert "Dredging" in " ".join(part["text"] for part in read["parts"])


def test_display_file_through_the_proxy(admin: Account, chat: str) -> None:
    """The card Open WebUI shows: the path, the name and the type, then the bytes from ``/files/view``."""
    admin.upload(chat, "quarterly-figures.csv", CSV)
    path = f"/workspace/{chat}/quarterly-figures.csv"
    r = admin.terminal("GET", "/files/display", chat=chat, params={"path": path})
    assert r.json() == {"path": path, "exists": True, "name": "quarterly-figures.csv", "mime_type": "text/csv"}
    missing = admin.terminal("GET", "/files/display", chat=chat, params={"path": f"/workspace/{chat}/absent.csv"})
    assert missing.json()["exists"] is False


def test_the_proxy_keeps_the_viewing_headers(admin: Account, chat: str) -> None:
    """The sandboxing headers reach the browser through the proxy; an HTML file still goes out as a text attachment."""
    admin.upload(chat, "quarterly-figures.csv", CSV)
    viewed = admin.terminal(
        "GET", "/files/view", chat=chat, params={"path": f"/workspace/{chat}/quarterly-figures.csv"}
    )
    assert {name: viewed.headers.get(name) for name in VIEW_HEADERS} == VIEW_HEADERS
    admin.upload(chat, "harbour-notes.html", b"<html><body><script>alert(1)</script></body></html>")
    active = admin.terminal("GET", "/files/view", chat=chat, params={"path": f"/workspace/{chat}/harbour-notes.html"})
    assert active.status_code == 200
    assert active.headers["content-type"] == "text/plain; charset=utf-8"
    assert active.headers["content-disposition"].startswith("attachment;")
    assert {name: active.headers.get(name) for name in VIEW_HEADERS} == VIEW_HEADERS
    refused = admin.terminal("GET", "/files/view", chat=chat, params={"path": f"/workspace/{chat}/absent.csv"})
    assert refused.status_code == 404
    assert {name: refused.headers.get(name) for name in VIEW_HEADERS} == VIEW_HEADERS


def test_a_pdf_preview_is_a_sandboxed_pdf(admin: Account, chat: str, tmp_path: Path) -> None:
    """``display_file`` of a PDF names ``application/pdf``; its bytes come with the type and the sandbox.

    Open WebUI's card fetches them and draws the PDF in its own page; whether a browser's built-in viewer renders a
    PDF opened straight under ``sandbox`` cannot be told here, and the end-to-end check decides it.
    """
    pdf = mk.make_pdf(tmp_path / "harbour-report.pdf", ["Synthetic harbour report"]).read_bytes()
    admin.upload(chat, "harbour-report.pdf", pdf)
    path = f"/workspace/{chat}/harbour-report.pdf"
    shown = admin.terminal("GET", "/files/display", chat=chat, params={"path": path})
    assert shown.json()["mime_type"] == "application/pdf"
    viewed = admin.terminal("GET", "/files/view", chat=chat, params={"path": path})
    assert (viewed.status_code, viewed.content) == (200, pdf)
    assert viewed.headers["content-type"] == "application/pdf"
    assert viewed.headers["content-disposition"] == "inline; filename*=UTF-8''harbour-report.pdf"
    assert {name: viewed.headers.get(name) for name in VIEW_HEADERS} == VIEW_HEADERS


def test_an_office_preview_is_the_file_itself(admin: Account, chat: str, tmp_path: Path) -> None:
    """For a DOCX the card asks ``preview=true`` and uses the answer only if it is a PDF; workbench sends the DOCX.

    The card then draws the DOCX itself (spec section 5): no converter runs anywhere.
    """
    docx = mk.make_docx(tmp_path / "harbour-memo.docx").read_bytes()
    admin.upload(chat, "harbour-memo.docx", docx)
    path = f"/workspace/{chat}/harbour-memo.docx"
    preview = admin.terminal("GET", "/files/view", chat=chat, params={"path": path, "preview": "true"})
    assert (preview.status_code, preview.content) == (200, docx)
    assert "application/pdf" not in preview.headers["content-type"]
    assert {name: preview.headers.get(name) for name in VIEW_HEADERS} == VIEW_HEADERS


# --- what Open WebUI sends on -------------------------------------------------------------------------------------


def test_the_chat_open_webui_sends_on(admin: Account, chat: str) -> None:
    """``X-Session-Id`` is the chat the browser names, or the one in the proxy's ``/chats/<id>/`` path; never empty.

    A request that names no chat, or an empty one, reaches workbench without the header (Open WebUI forwards it only
    when it is not empty) and lands in ``default``. The chat in the path wins over the header.
    """

    def cwd(**where: str) -> str:
        r = admin.terminal("GET", "/files/cwd", **where)
        assert r.status_code == 200, r.text
        return str(r.json()["cwd"])

    assert cwd(chat=chat) == f"/workspace/{chat}"
    assert cwd() == "/workspace/default"
    assert cwd(chat="") == "/workspace/default"
    assert cwd(chat_route=chat) == f"/workspace/{chat}"
    assert cwd(chat_route=chat, chat="another-chat") == f"/workspace/{chat}"


def test_each_open_webui_user_has_a_workspace_of_their_own(admin: Account, user_b: Account, chat: str) -> None:
    """Two accounts, two ids, two workspaces: in the same chat folder user B sees nothing of the admin's.

    The browser cannot forge the user (R1 I-5): user B's browser sends the admin's id as ``X-User-Id``, once in that
    case and once in lower case, and Open WebUI's proxy still names B. B sees nothing of the admin's, and B's upload
    is recorded with B's account id as its actor.
    """
    assert admin.id != user_b.id
    admin.upload(chat, "quarterly-figures.csv", CSV)
    path = f"/workspace/{chat}/quarterly-figures.csv"
    honest: dict[str, str] = {}
    for forged in (honest, {"X-User-Id": admin.id}, {"x-user-id": admin.id}):
        listed = user_b.terminal(
            "GET", "/files/list", chat=chat, params={"directory": f"/workspace/{chat}"}, headers=forged
        )
        assert listed.json()["entries"] == [], forged
        tool = user_b.terminal("POST", "/tools/list_files", chat=chat, json={}, headers=forged)
        assert tool.json() == {"files": [], "truncated": False}, forged
        assert (
            user_b.terminal("GET", "/files/view", chat=chat, params={"path": path}, headers=forged).status_code == 404
        )
    forged_upload = {"file": ("quarterly-figures.csv", b"item,total\nOwn,1\n")}
    r = user_b.terminal(
        "POST",
        "/files/upload",
        chat=chat,
        params={"directory": f"/workspace/{chat}"},
        files=forged_upload,
        headers={"X-User-Id": admin.id},
    )
    assert r.status_code == 200, r.text
    assert r.json()["path"] == path  # B's own folder, where the name is still free
    eventually(lambda: upload_events(chat), 2)
    actors = [e["actor"]["user"] for e in upload_events(chat)]
    assert sorted(actors) == sorted([admin.id, user_b.id])


# --- Open WebUI's own audit log -----------------------------------------------------------------------------------


def test_open_webui_audit_skips_the_terminal_proxy(admin: Account, chat: str) -> None:
    """With ``AUDIT_EXCLUDED_PATHS=/terminals`` no record names an uploaded file: its name is in the proxy's URLs.

    Other requests are still recorded, so the log is on and the exclusion is what keeps the names out. The last request
    carries a marker of this run; once its record is in the log, so is every record of the requests before it.
    """
    name = f"harbour-ledger-{secrets.token_hex(4)}.csv"
    marker = secrets.token_hex(8)
    admin.upload(chat, name, CSV)
    admin.terminal("GET", "/files/view", chat=chat, params={"path": f"/workspace/{chat}/{name}"})
    admin.client.get("/api/v1/auths/", params={"marker": marker})
    marked = eventually(lambda: [uri for uri in open_webui_audit_uris() if marker in uri], 1)
    assert [uri.split("?", 1)[0] for uri in marked] == [f"{OPEN_WEBUI}/api/v1/auths/"], marked
    uris = open_webui_audit_uris()
    assert [uri for uri in uris if name in uri or "/terminals" in uri] == []


# --- a chat's own tool call ---------------------------------------------------------------------------------------


def test_a_chats_own_tool_call(admin: Account) -> None:
    """A saved chat with the workbench terminal and native function calling, as the browser starts one.

    The stub model asks for ``read_document`` on a file of the chat; Open WebUI makes that call on workbench itself.
    workbench records exactly that tool event, its actor the account's id and its folder the saved chat (``X-User-Id``
    and ``X-Session-Id`` on the server-side call). The model was offered the nine tools (``/openapi.json``) and
    workbench's prompt (``/api/config``, ``/system``), and got the tool's answer back. Its own answer is in the message
    Open WebUI streamed: a saved chat's turn runs as a task of Open WebUI's, whose stream goes to the browser over its
    socket (the HTTP answer is ``null``), and the message it streamed is saved in the chat. The tool event is counted
    again once the model has answered, so a late second call fails too.
    :data:`CHAT_PATHS` says what stays unpinned.
    """
    new: dict[str, Any] = {
        "title": "Synthetic chat",
        "models": [STUB_MODEL],
        "messages": [],
        "history": {"messages": {}},
    }
    made = admin.client.post("/api/v1/chats/new", json={"chat": new})
    assert made.status_code == 200, made.text
    chat = made.json()["id"]
    path = admin.upload(chat, "quarterly-figures.csv", CSV).json()["path"]
    (upload,) = eventually(lambda: upload_events(chat), 1)
    prompt = admin.terminal("GET", "/system").json()["prompt"]
    message = str(uuid.uuid4())
    body = {
        "model": STUB_MODEL,
        "messages": [{"role": "user", "content": f"What is in {path}?"}],
        "chat_id": chat,
        "id": message,
        "terminal_id": TERMINAL,
        "stream": True,
        "params": {"function_calling": "native"},
    }
    with admin.client.stream("POST", "/api/chat/completions", json=body) as r:
        assert r.status_code == 200, r.read()
        r.read()

    def streamed() -> list[str]:
        saved = admin.client.get(f"/api/v1/chats/{chat}").json()["chat"]["history"]["messages"].get(message, {})
        return [str(saved.get("content", ""))] if saved.get("done") else []

    def tool_events() -> list[dict[str, Any]]:
        return [
            e
            for e in workbench_events()
            if e["operation"].startswith("tool.") and e.get("target", {}).get("container") == f"chat:{chat}"
        ]

    (event,) = eventually(tool_events, 1)
    assert event["operation"] == "tool.read_document"
    assert event["actor"] == {"user": admin.id, "via": "service"}
    assert event["target"]["container"] == f"chat:{chat}"
    assert event["target"]["ids"] == upload["target"]["ids"]
    asked, answered = eventually(lambda: stub_requests(chat), 2)[:2]
    assert set(asked["tools"]) == TOOLS
    assert prompt.strip()
    assert prompt.strip() in asked["system"]
    assert asked["tool_answers"] == []
    (tool_answer,) = answered["tool_answers"]
    assert "Dredging" in tool_answer
    assert len(stub_requests(chat)) == 2
    assert [e["operation"] for e in tool_events()] == ["tool.read_document"]  # still one, once the model has answered
    (answer,) = eventually(streamed, 1)
    assert STUB_ANSWER in answer
