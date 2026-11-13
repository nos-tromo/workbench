"""A size guard for uploads that holds while the body is received.

Starlette parses a multipart body completely, spilling it to the temporary directory, before
the route runs; so the store's own per-file check only comes after an oversized body has
already been written to disk. This guard stops it on the way in: a declared ``Content-Length``
over the bound is answered 413 before any body is read, and a body without one (chunked), or
one that lies, is counted as it arrives and stopped at the first chunk that passes the bound.

The bound allows for the multipart framing around the file, so it is a little above the
per-file limit; the store's check stays the exact limit. Only ``POST /files/upload`` is
guarded. Nothing here is logged, so no path, query or name can reach a log through it.
"""

from typing import Final

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from workbench.config import Settings

#: The one route guarded.
UPLOAD_PATH: Final = "/files/upload"

#: The detail of every 413, the same text the store uses for a file over the per-file limit.
TOO_LARGE: Final = "file too large"

_MIB: Final = 1024 * 1024
_FRAMING: Final = _MIB


def body_limit(settings: Settings) -> int:
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


class UploadLimit:
    """Pure ASGI middleware that refuses an upload body over a bound while it is received."""

    def __init__(self, app: ASGIApp, *, max_body: int) -> None:
        """Wrap ``app``.

        Args:
            app: The application.
            max_body: Largest request body, in bytes, that ``POST /files/upload`` may have.
        """
        self.app = app
        self.max_body = max_body

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Answer an oversized upload with 413, and pass everything else on.

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
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != UPLOAD_PATH:
            await self.app(scope, receive, send)
            return
        declared = _declared_length(scope)
        if declared is not None and declared > self.max_body:
            await JSONResponse({"detail": TOO_LARGE}, status_code=413)(scope, receive, send)
            return
        seen = 0

        async def counted() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_body:
                    raise HTTPException(status_code=413, detail=TOO_LARGE)
            return message

        await self.app(scope, counted, send)
