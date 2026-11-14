"""Browser hardening for the one route that sends a user's file to a browser.

A file Open WebUI's browser opens from ``GET /files/view`` must not be able to run script at
Open WebUI's origin, whatever the file holds. ``files.py`` turns active content into a text
attachment; this adds the headers that sandbox the rest. They go on **every** answer of the
route, refusals included, so a middleware sets them on the response start rather than the
route on its own success: a 400, 401, 404, 422 or 500 written by an error handler carries
them too, and so does the 500 :class:`~workbench.api.errors.QuietErrors` writes, inside this
layer, for an error no handler expected.
"""

from typing import Final

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: The one route hardened.
VIEW_PATH: Final = "/files/view"

#: No script, no navigation, no request out, no sniffing, and no other origin may embed the answer.
FILE_HEADERS: Final = {
    "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'",
    "X-Content-Type-Options": "nosniff",
    "Cross-Origin-Resource-Policy": "same-origin",
}


class ViewHardening:
    """Pure ASGI middleware that puts :data:`FILE_HEADERS` on every answer of ``/files/view``."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``.

        Args:
            app: The application.
        """
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass the request on, setting the headers on the answer if it is for ``/files/view``.

        Args:
            scope: The ASGI scope.
            receive: Receives the request body.
            send: Sends the response.
        """
        if scope["type"] != "http" or scope["path"] != VIEW_PATH:
            await self.app(scope, receive, send)
            return

        async def hardened(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in FILE_HEADERS.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, hardened)
