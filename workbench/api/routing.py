"""The route class of every router of the app: a JSON body is read with every lone surrogate replaced by U+FFFD.

JSON may carry the escape of a surrogate half alone, as code a model writes with one backslash too few does. Without
this, FastAPI's 422, which quotes the body back, could not be encoded, and the answer would be a 500.
"""

from collections.abc import Callable, Coroutine
from typing import Any, override

from fastapi import Request, Response
from fastapi.routing import APIRoute

from workbench.protocol import encodable


class EncodableRequest(Request):
    """A request whose JSON body is read with every lone surrogate replaced by U+FFFD."""

    @override
    async def json(self) -> Any:
        """Read the body as JSON, every string of it encodable as UTF-8.

        Returns:
            The body's value.
        """
        return encodable(await super().json())


class EncodableRoute(APIRoute):
    """A route that reads its body as :class:`EncodableRequest` does."""

    @override
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """Wrap the route's handler so that it is given an :class:`EncodableRequest`.

        Returns:
            The handler.
        """
        handler = super().get_route_handler()

        async def read_encodable(request: Request) -> Response:
            return await handler(EncodableRequest(request.scope, request.receive))

        return read_encodable
