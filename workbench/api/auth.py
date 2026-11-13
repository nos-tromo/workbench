"""Who may call: the caller registry, the bearer-key check and the user and chat headers.

The caller is authenticated by its key. The user id and the chat id are the
caller's own claims, trusted only because only a caller holding a key can reach
the api (``workbench-net`` has no other member). Both become folder names, so
both go through :func:`~workbench.paths.check_id`.

The headers are read from the request itself, not declared as parameters, so no
route that depends on them lists them in its OpenAPI operation, where the model
would see them as tool arguments.
"""

import hmac
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Annotated, Final, Self

from fastapi import Depends, HTTPException, Request

from workbench.config import ConfigError, Settings
from workbench.models import Principal
from workbench.paths import DEFAULT_CHAT, PathError, check_id

#: Every tool of version 1, by ``operationId``.
ALL_TOOLS: Final = frozenset(
    {
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
)

_BEARER: Final = "bearer"


@dataclass(frozen=True)
class Caller:
    """A service that is allowed to call the api.

    Attributes:
        name: The caller's name. It is the first folder level of its users' workspaces and the
            ``caller`` of every audit event, so it is a safe id.
        key: The bearer key the caller presents; left out of ``repr`` so no log line shows it.
        tools: The ``operationId`` of every tool the caller may use.
    """

    name: str
    key: str = field(repr=False)
    tools: frozenset[str]


class CallerRegistry:
    """The callers the api knows. Version 1 has one; docint joins in version 2."""

    def __init__(self, callers: Iterable[Caller]) -> None:
        """Remember ``callers``.

        Args:
            callers: Every caller, each with its own key.
        """
        self._callers = tuple(callers)

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        """Build the registry the settings describe: Open WebUI, with every tool.

        Args:
            settings: The api's settings.

        Returns:
            The registry.

        Raises:
            ConfigError: If the settings hold no caller key.
        """
        if settings.open_webui_key is None:
            raise ConfigError("OPEN_WEBUI_WORKBENCH_KEY must be set to at least 32 characters")
        return cls([Caller("open-webui", settings.open_webui_key, ALL_TOOLS)])

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
    """Authenticate the request by its bearer key.

    Args:
        request: The request.

    Returns:
        The caller that holds the key.

    Raises:
        HTTPException: 401 if the key is missing, malformed or not held by any caller.
            The answer is the same for all three.
    """
    registry: CallerRegistry = request.app.state.callers
    token = _bearer_token(request.headers.get("authorization"))
    caller = registry.authenticate(token) if token is not None else None
    if caller is None:
        raise HTTPException(status_code=401, detail="missing or invalid key", headers={"WWW-Authenticate": "Bearer"})
    return caller


async def require_principal(request: Request, caller: Annotated[Caller, Depends(require_caller)]) -> Principal:
    """Authenticate the request and say whose files it is about.

    Args:
        request: The request.
        caller: The authenticated caller; the key is checked before any other header.

    Returns:
        The caller, the user from ``X-User-Id`` and the chat from ``X-Session-Id``
        (``default`` when the request has none).

    Raises:
        HTTPException: 400 if ``X-User-Id`` is missing or not a plain id, or
            ``X-Session-Id`` is present but not a plain id.
    """
    user_id = request.headers.get("x-user-id")
    if user_id is None or not _is_id(user_id):
        raise HTTPException(status_code=400, detail="missing or invalid user")
    chat_id = request.headers.get("x-session-id", DEFAULT_CHAT)
    if not _is_id(chat_id):
        raise HTTPException(status_code=400, detail="invalid session")
    return Principal(caller=caller.name, user_id=user_id, chat_id=chat_id)
