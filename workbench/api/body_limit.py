"""A size guard for every request body that holds while the body is received.

Nothing reads more of a request than its bound. Starlette parses a multipart body completely, spilling it to the
temporary directory, before the route runs, and FastAPI reads a JSON body whole before it parses it; so a route's own
checks come only after an oversized body is already in memory or on disk. This guard stops it on the way in: a
declared ``Content-Length`` over the bound is answered 413 before any body is read, and a body without one (chunked),
or one that lies, is counted as it arrives and stopped at the first chunk that passes the bound.

There are three bounds. ``POST /files/upload`` may carry one file of the per-file limit, plus room for the multipart
framing; the store's own check stays the exact limit (``file too large``). ``POST /files/derived`` carries one
artifact of a clip as its raw body, so it has the bound of the largest artifact, the zip's (:data:`DERIVED_BODY_MAX`),
and the same ``file too large``; the route's own bound for a JSON Lines artifact is the smaller one. Every other request
is a tool call, which the executor's protocol line (1 MiB) bounds anyway, or has no body of its own, so it may have at
most :data:`REQUEST_MAX` (``request too large``). The caller key is checked before this guard runs
(:class:`~workbench.api.auth.KeyGate`), so a request without a valid key never has its body read at all. Nothing here
is logged, so no path, query or name can reach a log through it.

On the MCP door the refusal of a declared length is JSON-RPC (:func:`~workbench.api.mcp_wire.too_large`); a body that
grows past the bound there is answered so by the door itself.
"""

from typing import Final

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from workbench.api.auth import DERIVED_PATH, MCP_PATH
from workbench.api.mcp_wire import too_large
from workbench.config import Settings
from workbench.renders import ZIP_MAX_BYTES

#: The route that takes a file of the user's, in a multipart body.
UPLOAD_PATH: Final = "/files/upload"

#: The largest body of ``POST /files/derived``: the larger of the two artifacts of a clip, Nextext's keyframes zip.
DERIVED_BODY_MAX: Final = ZIP_MAX_BYTES

#: The detail of a 413 for an upload, the same text the store uses for a file over the per-file limit.
FILE_TOO_LARGE: Final = "file too large"

#: The detail of a 413 for any other request.
REQUEST_TOO_LARGE: Final = "request too large"

#: The largest body of any request but an upload: twice the executor's protocol line.
REQUEST_MAX: Final = 2 * 1024 * 1024

_MIB: Final = 1024 * 1024
_FRAMING: Final = _MIB


def upload_limit(settings: Settings) -> int:
    """Return the largest request body an upload may have: the per-file limit plus room for framing.

    Args:
        settings: The api's settings.

    Returns:
        The bound in bytes.
    """
    return settings.upload_max_mb * _MIB + _FRAMING


def _declared_length(scope: Scope) -> int | None:
    """Read the ``Content-Length`` a request declared.

    Args:
        scope: The ASGI scope of an HTTP request.

    Returns:
        The declared length, or ``None`` if there is none or it is not a number.
    """
    for name, value in scope["headers"]:
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


class BodyLimit:
    """Pure ASGI middleware that refuses a request body over its bound while it is received."""

    def __init__(self, app: ASGIApp, *, upload_max: int, request_max: int = REQUEST_MAX) -> None:
        """Wrap ``app``.

        Args:
            app: The application.
            upload_max: Largest request body, in bytes, that ``POST /files/upload`` may have.
            request_max: Largest request body, in bytes, of any other request.
        """
        self.app = app
        self.upload_max = upload_max
        self.request_max = request_max

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Answer an oversized body with 413, and pass everything else on.

        The route's own answer is not replaced: a body that grows past the bound while it is
        read raises an ``HTTPException``, which FastAPI re-raises out of the body parser and
        the application answers as it does any other (short JSON, nothing logged).

        Args:
            scope: The ASGI scope.
            receive: Receives the request body.
            send: Sends the response.

        Raises:
            HTTPException: 413, from ``receive``, once the body passes the bound.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["method"] == "POST" and scope["path"] == UPLOAD_PATH:
            bound, detail = self.upload_max, FILE_TOO_LARGE
        elif scope["method"] == "POST" and scope["path"] == DERIVED_PATH:
            bound, detail = DERIVED_BODY_MAX, FILE_TOO_LARGE
        else:
            bound, detail = self.request_max, REQUEST_TOO_LARGE
        declared = _declared_length(scope)
        if declared is not None and declared > bound:
            refusal = too_large() if scope["path"] == MCP_PATH else JSONResponse({"detail": detail}, status_code=413)
            await refusal(scope, receive, send)
            return
        seen = 0

        async def counted() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > bound:
                    raise HTTPException(status_code=413, detail=detail)
            return message

        await self.app(scope, counted, send)
