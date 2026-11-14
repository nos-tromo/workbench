"""The route class of every router of the app: a JSON body is read within a depth limit, lone surrogates replaced.

Every lone surrogate in the body becomes U+FFFD. JSON may carry the escape of a surrogate half alone, as code a model
writes with one backslash too few does; a route that used the string could not encode it.

A body that nests objects and arrays deeper than :data:`MAX_DEPTH` is a 422 with one fixed text, whichever route it
was sent to: past that no tool's arguments go, and a job that deep could not be written to the executor (pydantic
serializes 255 levels) or read there (the protocol's reader takes 200). Past Python's own recursion limit the parser
itself gives up; that is the same 422, not FastAPI's 400.
"""

from collections.abc import Callable, Coroutine
from typing import Any, Final, override

from fastapi import HTTPException, Request, Response
from fastapi.routing import APIRoute

from workbench.protocol import encodable

#: The deepest a request's JSON body may nest, objects and arrays together; the body itself is the first level.
MAX_DEPTH: Final = 64

#: The detail of the 422 for a body nested deeper.
TOO_DEEP: Final = "request nested too deeply"


def nested_deeper_than(value: Any, limit: int) -> bool:
    """Tell whether a JSON value nests objects and arrays deeper than ``limit``, one level at a time, never recursing.

    Args:
        value: A value as :func:`json.loads` returns it.
        limit: The most levels allowed.

    Returns:
        ``True`` if some object or array lies more than ``limit`` levels down; a value that is neither is level 0.
    """
    level = [value]
    depth = 0
    while containers := [item for item in level if isinstance(item, (dict, list))]:
        depth += 1
        if depth > limit:
            return True
        level = [child for item in containers for child in (item.values() if isinstance(item, dict) else item)]
    return False


class EncodableRequest(Request):
    """A request whose JSON body is read within :data:`MAX_DEPTH`, every lone surrogate replaced with U+FFFD."""

    @override
    async def json(self) -> Any:
        """Read the body as JSON, at most :data:`MAX_DEPTH` levels deep, every string of it encodable as UTF-8.

        Returns:
            The body's value.

        Raises:
            HTTPException: 422 with :data:`TOO_DEEP` if the body nests deeper, or too deep for the parser at all.
        """
        try:
            body = await super().json()
        except RecursionError:
            raise HTTPException(status_code=422, detail=TOO_DEEP) from None
        if nested_deeper_than(body, MAX_DEPTH):
            raise HTTPException(status_code=422, detail=TOO_DEEP)
        return encodable(body)


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
