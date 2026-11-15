"""Who may call: the caller registry, the bearer-key check, the doors and the user and chat headers.

The caller is authenticated by its key. The user id and the chat id are the
caller's own claims, trusted only because only a caller holding a key can reach
the api (``workbench-net`` has no other member). Both become folder names, so
both go through :func:`~workbench.paths.check_id`.

The headers are read from the request itself, not declared as parameters, so no
route that depends on them lists them in its OpenAPI operation, where the model
would see them as tool arguments.

The key is checked twice: by :class:`KeyGate`, before anything of the request's
body is read, and again by each route's dependency. Without a valid key a request
answers ``401`` whatever its path, method or body, so a peer without the key can
neither map the routes nor make the api read a body.

Each caller comes in through one door. :class:`KeyGate` checks it right after the key,
still before any body is read: an MCP caller finds :data:`MCP_PATH` alone, a terminal
caller every path but that one, and a path outside the door answers ``404`` with
:data:`~workbench.api.errors.NOT_FOUND`. Only :data:`OPEN_PATHS` stay open to every caller.
"""

import hmac
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Final, Literal, Self

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from workbench.api.errors import NOT_FOUND
from workbench.api.registry import FORM_TOOLS, WORKSPACE_TOOLS
from workbench.config import NO_CALLER_KEY, ConfigError, Settings
from workbench.models import Principal
from workbench.paths import DEFAULT_CHAT, PathError, check_id

_BEARER: Final = "bearer"

#: The detail of every 401.
UNAUTHORIZED: Final = "missing or invalid key"

#: The paths answered without a key: the compose healthcheck and obs-plane's scraper.
OPEN_PATHS: Final = frozenset({"/healthz", "/metrics"})

#: The one path of the MCP door.
MCP_PATH: Final = "/mcp"

#: The texts of the two refusals of the user and chat headers, on both doors.
INVALID_USER: Final = "missing or invalid user"
INVALID_SESSION: Final = "invalid session"

#: How a caller comes in: Open WebUI's terminal-server HTTP, or the MCP door.
Door = Literal["terminal", "mcp"]

#: Where a caller's files live: in its users' workspaces, or only inside the call that brings them.
Files = Literal["workspace", "inline"]


@dataclass(frozen=True)
class Caller:
    """A service that is allowed to call the api.

    Attributes:
        name: The caller's name. It is the first folder level of its users' workspaces and the
            ``caller`` of every audit event, so it is a safe id.
        key: The bearer key the caller presents; left out of ``repr`` so no log line shows it.
        tools: The name of every tool the caller may use, each of its file form.
        door: The door the caller comes in through; every other path is a 404 for it.
        files: Whether the caller's files are kept in workspaces or come with each call.
        wall_s: The most wall-clock seconds one of its jobs may run; ``None`` leaves only the job wall.
        slots: The most jobs of its that may run at once; ``None`` for no cap of its own.
    """

    name: str
    key: str = field(repr=False)
    tools: frozenset[str]
    door: Door = "terminal"
    files: Files = "workspace"
    wall_s: int | None = None
    slots: int | None = None


class CallerRegistry:
    """The callers the api knows, by name and by key: Open WebUI, docint, or both."""

    def __init__(self, callers: Iterable[Caller]) -> None:
        """Remember ``callers``.

        Args:
            callers: Every caller, each with its own key.

        Raises:
            ConfigError: If two callers hold the same key. The message never holds the key.
        """
        self._callers = tuple(callers)
        if len({caller.key for caller in self._callers}) < len(self._callers):
            raise ConfigError("two callers hold the same key")

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        """Build the registry the settings describe: a row for each caller whose key is set.

        Open WebUI comes in through the terminal door, keeps its files in workspaces and may use every workspace tool,
        each job within the job wall. docint comes in through the MCP door, brings its data with each call and may use
        its listed tools, within its own wall cap and slot cap; each of them must be an inline tool.

        Args:
            settings: The api's settings.

        Returns:
            The registry.

        Raises:
            ConfigError: If the settings hold no caller key, two callers hold the same key, or docint's list names a
                tool that is not an inline tool.
        """
        callers: list[Caller] = []
        if settings.open_webui_key is not None:
            callers.append(Caller("open-webui", settings.open_webui_key, WORKSPACE_TOOLS, wall_s=settings.job_wall_s))
        if settings.docint_key is not None:
            docint = Caller(
                "docint",
                settings.docint_key,
                settings.docint_tools,
                door="mcp",
                files="inline",
                wall_s=settings.docint_wall_s,
                slots=settings.docint_slots,
            )
            callers.append(_within_form(docint, "DOCINT_WORKBENCH_TOOLS"))
        if not callers:
            raise ConfigError(NO_CALLER_KEY)
        return cls(callers)

    @property
    def callers(self) -> tuple[Caller, ...]:
        """Every caller, in the order they were given."""
        return self._callers

    def get(self, name: str) -> Caller:
        """Find a caller by its name.

        Args:
            name: The caller's name.

        Returns:
            The caller.

        Raises:
            KeyError: If no caller has that name.
        """
        for caller in self._callers:
            if caller.name == name:
                return caller
        raise KeyError(name)

    def authenticate(self, key: str) -> Caller | None:
        """Find the caller that holds ``key``, comparing in constant time.

        Every caller is compared, so the time taken does not show which one matched.

        Args:
            key: The key a request presented.

        Returns:
            The caller, or ``None`` if no caller holds that key.
        """
        presented = key.encode("utf-8")
        found: Caller | None = None
        for caller in self._callers:
            if hmac.compare_digest(presented, caller.key.encode("utf-8")):
                found = caller
        return found


def _within_form(caller: Caller, variable: str) -> Caller:
    """Check that every tool on a caller's list is one of its file form.

    Args:
        caller: The caller.
        variable: The variable its list comes from, for the message.

    Returns:
        The caller.

    Raises:
        ConfigError: If the list names a tool of another form, or a name no tool has. The message names the variable
            and the tools it may list, never the value.
    """
    allowed = FORM_TOOLS[caller.files]
    if not caller.tools <= allowed:
        raise ConfigError(f"{variable} may list only {caller.files} tools: {', '.join(sorted(allowed))}")
    return caller


def door_allows(caller: Caller, path: str) -> bool:
    """Say whether ``path`` is behind the caller's door.

    Args:
        caller: The authenticated caller.
        path: The request's path, as routing sees it.

    Returns:
        For an MCP caller, whether the path is :data:`MCP_PATH`; for a terminal caller, whether it is any other.
    """
    if caller.door == "mcp":
        return path == MCP_PATH
    return path != MCP_PATH


def _bearer_token(authorization: str | None) -> str | None:
    """Take the key out of an ``Authorization: Bearer <key>`` header value.

    Args:
        authorization: The header value, or ``None`` if the request had none.

    Returns:
        The key, or ``None`` if the header is missing, of another scheme or holds no key.
    """
    if authorization is None:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != _BEARER or not token:
        return None
    return token


def _is_id(value: str) -> bool:
    """Say whether ``value`` is an id that is safe as one folder name.

    Args:
        value: A header value.

    Returns:
        ``True`` if :func:`~workbench.paths.check_id` accepts it.
    """
    try:
        check_id(value, what="id")
    except PathError:
        return False
    return True


async def require_caller(request: Request) -> Caller:
    """Authenticate the request by its bearer key, for a route of the terminal door.

    Every route that depends on it is the terminal door's, so a caller of another door is refused here as
    :class:`KeyGate` refuses it, should a request ever pass the gate.

    Args:
        request: The request.

    Returns:
        The caller that holds the key.

    Raises:
        HTTPException: 401 if the key is missing, malformed or not held by any caller; the answer is the same for all
            three. 404 with :data:`~workbench.api.errors.NOT_FOUND` if the caller does not come in through the terminal
            door.
    """
    registry: CallerRegistry = request.app.state.callers
    token = _bearer_token(request.headers.get("authorization"))
    caller = registry.authenticate(token) if token is not None else None
    if caller is None:
        raise HTTPException(status_code=401, detail=UNAUTHORIZED, headers={"WWW-Authenticate": "Bearer"})
    if caller.door != "terminal":
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    return caller


def require_tool(name: str) -> Callable[..., Awaitable[Caller]]:
    """Make the dependency of the tool route ``name``.

    Like :func:`require_caller`, it reads the request itself and declares no parameter, so the route's
    OpenAPI operation gains none.

    Args:
        name: The tool's ``operationId``.

    Returns:
        A dependency that gives the caller :func:`require_caller` authenticated, and answers 404 with
        :data:`~workbench.api.errors.NOT_FOUND` when ``name`` is not on that caller's list.
    """

    async def tool_on_list(caller: Annotated[Caller, Depends(require_caller)]) -> Caller:
        """Let the caller through if ``name`` is on its list.

        Args:
            caller: The authenticated caller of the terminal door.

        Returns:
            The caller.

        Raises:
            HTTPException: 404 with :data:`~workbench.api.errors.NOT_FOUND` if the tool is not on the caller's list.
        """
        if name not in caller.tools:
            raise HTTPException(status_code=404, detail=NOT_FOUND)
        return caller

    return tool_on_list


class KeyGate:
    """Pure ASGI middleware: no request gets past it without a caller's key, or outside that caller's door.

    It reads no body. Every path but :data:`OPEN_PATHS` needs the key, a path no route serves included, so a peer
    without it learns nothing of the routes: neither a 404, nor a 405, nor a 422 that names a body's fields. The key
    is the ``Authorization`` header's, read as the routes read it. A caller with a key finds only its door's paths
    (:func:`door_allows`); any other answers ``404`` with :data:`~workbench.api.errors.NOT_FOUND`.
    """

    def __init__(self, app: ASGIApp, *, callers: CallerRegistry) -> None:
        """Wrap ``app``.

        Args:
            app: The application.
            callers: The callers whose keys open the gate.
        """
        self.app = app
        self.callers = callers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Answer 401 without a valid key and 404 outside the caller's door, before the body is read; pass the rest on.

        Args:
            scope: The ASGI scope.
            receive: Receives the request body; never called here.
            send: Sends the response.
        """
        if scope["type"] != "http" or scope["path"] in OPEN_PATHS:
            await self.app(scope, receive, send)
            return
        authorization = next((value for name, value in scope["headers"] if name == b"authorization"), None)
        token = _bearer_token(None if authorization is None else authorization.decode("latin-1"))
        caller = self.callers.authenticate(token) if token is not None else None
        if caller is None:
            refusal = JSONResponse({"detail": UNAUTHORIZED}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
            await refusal(scope, receive, send)
            return
        if not door_allows(caller, scope["path"]):
            await JSONResponse({"detail": NOT_FOUND}, status_code=404)(scope, receive, send)
            return
        await self.app(scope, receive, send)


class InvalidPrincipal(ValueError):
    """The user or chat header is missing or not a plain id.

    The message is :data:`INVALID_USER` or :data:`INVALID_SESSION`, never the value sent.
    """


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Read the first header called ``name``, whatever the case of its name.

    Args:
        headers: The request's headers, a Starlette ``Headers`` or a plain mapping.
        name: The header's name, in lower case.

    Returns:
        Its value, or ``None`` if there is none.
    """
    return next((value for key, value in headers.items() if key.lower() == name), None)


def principal_from_headers(caller: Caller, headers: Mapping[str, str]) -> Principal:
    """Say whose files a request is about, from the headers both doors read.

    Args:
        caller: The authenticated caller; the key, not a header, names it.
        headers: The request's headers.

    Returns:
        The caller, the user from ``X-User-Id`` and the chat from ``X-Session-Id``
        (``default`` when the request has none).

    Raises:
        InvalidPrincipal: If ``X-User-Id`` is missing or not a plain id, or
            ``X-Session-Id`` is present but not a plain id.
    """
    user_id = _header(headers, "x-user-id")
    if user_id is None or not _is_id(user_id):
        raise InvalidPrincipal(INVALID_USER)
    chat_id = _header(headers, "x-session-id")
    if chat_id is None:
        chat_id = DEFAULT_CHAT
    if not _is_id(chat_id):
        raise InvalidPrincipal(INVALID_SESSION)
    return Principal(caller=caller.name, user_id=user_id, chat_id=chat_id)


async def require_principal(request: Request, caller: Annotated[Caller, Depends(require_caller)]) -> Principal:
    """Authenticate the request and say whose files it is about.

    Args:
        request: The request.
        caller: The authenticated caller; the key is checked before any other header.

    Returns:
        The caller, the user from ``X-User-Id`` and the chat from ``X-Session-Id``
        (``default`` when the request has none).

    Raises:
        HTTPException: 400 with the text of :class:`InvalidPrincipal` if ``X-User-Id`` is missing or not a plain id, or
            ``X-Session-Id`` is present but not a plain id.
    """
    try:
        return principal_from_headers(caller, request.headers)
    except InvalidPrincipal as refused:
        raise HTTPException(status_code=400, detail=str(refused)) from None
