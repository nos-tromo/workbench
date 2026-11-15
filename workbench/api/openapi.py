"""The OpenAPI documents the terminal door's callers turn into the model's tools.

Open WebUI makes a tool of every operation that has an ``operationId``, so a
document holds the routes tagged :data:`TOOL_TAG` and nothing else: not the
handshake, not the file browser, not the health check. Each caller gets its own
document, of the tools on its list.

``Caller`` is imported for type checking only, since :mod:`workbench.api.auth`
imports the registry, which imports this module.
"""

from typing import TYPE_CHECKING, Any, Final

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute, RouteContext, iter_route_contexts

from workbench import __version__

if TYPE_CHECKING:
    from workbench.api.auth import Caller

#: Tag of every route that is a tool.
TOOL_TAG: Final = "tool"

_TITLE: Final = "workbench"
_DESCRIPTION: Final = "Tools for working with a user's own files: read, search, compute, fill templates, write."


def tool_routes(app: FastAPI) -> list[RouteContext]:
    """List the routes of ``app`` that are tools.

    A route added with ``include_router`` is looked at in its effective form, with the
    router's own tags and ``include_in_schema`` applied.

    Args:
        app: The application.

    Returns:
        The routes tagged :data:`TOOL_TAG` that are in the schema.
    """
    return [
        route
        for route in iter_route_contexts(app.routes)
        if isinstance(route.original_route, APIRoute) and route.include_in_schema and TOOL_TAG in (route.tags or [])
    ]


def tool_openapi(app: FastAPI, caller: "Caller") -> dict[str, Any]:
    """Return a caller's tool document, built on its first use and kept.

    Open WebUI asks for it on every chat request, so it is served from memory, one
    document per caller name in ``app.state.tool_openapi``. Every tool route must
    therefore be registered before the first call. The tool routes are the workspace
    tools, so a caller whose files come with each call finds none of them.

    Args:
        app: The application.
        caller: The authenticated caller.

    Returns:
        The OpenAPI document of the tool routes (:func:`tool_routes`) whose ``operationId``
        is on the caller's list.
    """
    documents: dict[str, dict[str, Any]] | None = getattr(app.state, "tool_openapi", None)
    if documents is None:
        documents = {}
        app.state.tool_openapi = documents
    document = documents.get(caller.name)
    if document is None:
        tools = caller.tools if caller.files == "workspace" else frozenset()
        routes = [route for route in tool_routes(app) if route.operation_id in tools]
        document = get_openapi(title=_TITLE, version=__version__, description=_DESCRIPTION, routes=routes)
        documents[caller.name] = document
    return document
