"""What Open WebUI calls on a terminal server, besides the tools themselves.

Open WebUI connects a "terminal server" by calling, in this order: a policy
probe that must *not* succeed (a 2xx makes it treat the server as an
orchestrator, so no route exists for it), ``/api/config``, ``/system``,
``/openapi.json``, ``/files/cwd``, ``/files/read`` for ``AGENTS.md`` and
``/skills``. They all answer from memory or the metadata store, never from a
queue and never by parsing a file. None of them is in the tool document.

Everything not registered answers 404; that allow-list is workbench's own,
because Open WebUI's proxy forwards every method and path the browser sends.

The routes that need nothing from disk are ``async`` and so are their dependencies, so a
burst of uploads that holds every worker thread cannot delay the handshake or the
compose healthcheck.
"""

import posixpath
from functools import cache
from importlib import resources
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.requests import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from workbench.api.auth import require_caller, require_principal
from workbench.api.deps import get_settings, get_store
from workbench.api.errors import NOT_FOUND
from workbench.api.openapi import tool_openapi
from workbench.api.store import Store
from workbench.config import Settings
from workbench.models import VIRTUAL_ROOT, Principal

router = APIRouter(include_in_schema=False)

_PROMPT_FILE = "system_prompt.md"


@cache
def _prompt_template() -> str:
    """Read the system prompt from the package.

    Returns:
        The text, with ``{retention_days}`` still in it.
    """
    return resources.files("workbench.api").joinpath(_PROMPT_FILE).read_text(encoding="utf-8")


def system_prompt(settings: Settings) -> str:
    """Return the assistant's system prompt, with the retention period filled in.

    The one placeholder is replaced by name, not by ``str.format``, because the prompt
    itself talks about ``{{ field }}`` template placeholders.

    Args:
        settings: The api's settings.

    Returns:
        The prompt Open WebUI hands the model.
    """
    return _prompt_template().replace("{retention_days}", str(settings.retention_days))


class CwdRequest(BaseModel):
    """Body of ``POST /files/cwd``.

    Attributes:
        path: The folder to change to, as a virtual path.
    """

    path: str


@router.get("/healthz")
async def healthz() -> dict[str, bool]:
    """Answer the compose healthcheck. It needs no key and is not in the schema.

    Returns:
        ``{"ok": true}``.
    """
    return {"ok": True}


@router.get("/api/config", dependencies=[Depends(require_caller)])
async def config() -> dict[str, Any]:
    """Tell Open WebUI the server has a system prompt to fetch.

    Returns:
        The feature flags.
    """
    return {"features": {"system": True}}


@router.get("/system", dependencies=[Depends(require_caller)])
async def system(settings: Annotated[Settings, Depends(get_settings)]) -> dict[str, str]:
    """Serve the assistant's system prompt.

    Args:
        settings: The api's settings.

    Returns:
        ``{"prompt": ...}``.
    """
    return {"prompt": system_prompt(settings)}


@router.get("/openapi.json", dependencies=[Depends(require_caller)])
async def openapi(request: Request) -> JSONResponse:
    """Serve the tool document, from memory.

    Args:
        request: The request.

    Returns:
        The OpenAPI document of the tools. It needs the key only; Open WebUI sends no user headers here.
    """
    return JSONResponse(tool_openapi(request.app))


@router.get("/files/cwd")
async def get_cwd(p: Annotated[Principal, Depends(require_principal)]) -> dict[str, str]:
    """Say where the caller's chat starts.

    The folder is not made here; the first file makes it.

    Args:
        p: The caller, user and chat.

    Returns:
        The chat's folder, and the user's home above all chats.
    """
    return {"cwd": f"{VIRTUAL_ROOT}/{p.chat_id}", "home": VIRTUAL_ROOT}


@router.post("/files/cwd")
def set_cwd(
    body: CwdRequest,
    p: Annotated[Principal, Depends(require_principal)],
    store: Annotated[Store, Depends(get_store)],
) -> dict[str, str]:
    """Check that a folder is the caller's own, and give it back in its plain form.

    The server keeps no working folder between requests: the chat comes from every request's
    header. So this only validates.

    Args:
        body: The folder.
        p: The caller, user and chat.
        store: The file store.

    Returns:
        The normalised path.
    """
    store.list_dir(p, body.path)
    return {"cwd": posixpath.normpath(body.path)}


@router.get("/files/read")
def read_file(
    path: str,
    p: Annotated[Principal, Depends(require_principal)],
    store: Annotated[Store, Depends(get_store)],
) -> dict[str, str]:
    """Read a file as text. Open WebUI probes ``<home>/AGENTS.md`` for every chat.

    Files live only inside chat folders, so a file directly under home is absent by definition.

    Args:
        path: The file's virtual path.
        p: The caller, user and chat.
        store: The file store.

    Returns:
        Nothing yet: reading stored files as text arrives with the file browser.

    Raises:
        HTTPException: 404 if there is no such file, 415 if there is one.
    """
    if posixpath.dirname(posixpath.normpath(path)) == VIRTUAL_ROOT:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    store.get(p, path)
    raise HTTPException(status_code=415, detail="not readable as text")


@router.get("/skills", dependencies=[Depends(require_caller)])
async def skills() -> list[str]:
    """List the skills; there are none.

    Returns:
        An empty list.
    """
    return []


@router.get("/skills/read", dependencies=[Depends(require_caller)])
async def read_skill(name: str) -> None:
    """Read a skill; there are none.

    Args:
        name: The skill's name.

    Raises:
        HTTPException: 404, always.
    """
    raise HTTPException(status_code=404, detail=NOT_FOUND)
