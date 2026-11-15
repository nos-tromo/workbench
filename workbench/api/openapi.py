"""The OpenAPI document Open WebUI turns into the model's tools.

Open WebUI makes a tool of every operation that has an ``operationId``, so the
document holds the routes tagged :data:`TOOL_TAG` and nothing else: not the
handshake, not the file browser, not the health check.
"""

from typing import Any, Final

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute, iter_route_contexts

from workbench import __version__

#: Tag of every route that is a tool.
TOOL_TAG: Final = "tool"

_TITLE: Final = "workbench"
_DESCRIPTION: Final = "Tools for working with a user's own files: read, search, compute, fill templates, write."


def tool_openapi(app: FastAPI) -> dict[str, Any]:
    """Return the tool document of ``app``, built on first use and kept.

    Open WebUI asks for it on every chat request, so it is served from memory.
    Every tool route must therefore be registered before the first call. A route added
    with ``include_router`` is looked at in its effective form, with the router's own
    tags and ``include_in_schema`` applied.

    Args:
        app: The application.

    Returns:
        The OpenAPI document of the routes tagged :data:`TOOL_TAG` that are in the schema.
    """
    cached: dict[str, Any] | None = getattr(app.state, "tool_openapi", None)
    if cached is None:
        routes = [
            route
            for route in iter_route_contexts(app.routes)
            if isinstance(route.original_route, APIRoute) and route.include_in_schema and TOOL_TAG in (route.tags or [])
        ]
        cached = get_openapi(title=_TITLE, version=__version__, description=_DESCRIPTION, routes=routes)
        app.state.tool_openapi = cached
    return cached
