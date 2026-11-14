"""Short JSON answers for the errors a path, the store, the audit chain or the job queue raise on purpose.

The exceptions carry real paths, user ids or file names in their ``__cause__``,
and the model sees every error body, so the answer is a short fixed-text
``{"detail": ...}`` and the handlers log nothing: no traceback, no cause. Every
message of these exceptions is a literal written in ``paths.py`` or ``store.py``.

A failed audit write is a 500 of the same kind: the action is not done, and the cause (the
output stream) is not logged either.

A job the queue refuses (``Busy``) is a 503 with one fixed text, whatever the reason: the model is
told to retry later, and which limit was hit is for the metrics, not for the model. A request too
large for the executor's protocol (``RequestTooLarge``) is a 422: retrying it cannot help.

A request that does not validate is FastAPI's 422, but smaller: each error keeps its ``type``, ``loc`` and ``msg``,
never the ``input`` that failed or its ``ctx``, so a 200 000-character document or a 100 000-row table never goes back
into the model's context. The text a location part or a message quotes from the request (a key, a tag) is cut, and at
most :data:`MAX_ERRORS` are listed, the rest counted in ``more_errors``.
"""

from collections.abc import Sequence
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from workbench.api.jobs import Busy, RequestTooLarge
from workbench.api.store import Conflict, NotFound, QuotaExceeded, StorageFull, StoreError, TooLarge
from workbench.paths import PathError
from workbench.utils.audit_event import AuditWriteError

#: The detail of every 404.
NOT_FOUND: Final = "not found"

#: The detail of every 503.
BUSY: Final = "busy, retry later"

#: The detail of every 422 for a request the executor's protocol cannot carry.
TOO_LARGE: Final = "request too large"

#: The most validation errors one 422 lists.
MAX_ERRORS: Final = 20

#: The most characters of a location part or a message a 422 keeps.
_ERROR_TEXT: Final = 300
_CUT_MARK: Final = " [cut]"

#: Each error, its status, and the detail to send; ``None`` sends the error's own message.
_ANSWERS: Final[tuple[tuple[type[Exception], int, str | None], ...]] = (
    (PathError, 400, None),
    (NotFound, 404, NOT_FOUND),
    (Conflict, 409, None),
    (TooLarge, 413, None),
    (QuotaExceeded, 507, None),
    (StorageFull, 507, None),
    (AuditWriteError, 500, "audit unavailable"),
    (Busy, 503, BUSY),
    (RequestTooLarge, 422, TOO_LARGE),
)


async def _answer(_request: Request, exc: Exception) -> JSONResponse:
    """Turn a path, store, audit, queue or request-size error into its short answer, without logging it.

    Args:
        _request: The request that failed.
        exc: The error.

    Returns:
        The JSON response.
    """
    for kind, status, detail in _ANSWERS:
        if isinstance(exc, kind):
            return JSONResponse({"detail": detail or str(exc)}, status_code=status)
    return JSONResponse({"detail": "storage error"}, status_code=500)


def _cut(text: str) -> str:
    """Keep the first :data:`_ERROR_TEXT` characters of a text, marked where it was cut.

    Args:
        text: A location part or a message.

    Returns:
        The text, or its start and the mark.
    """
    return text if len(text) <= _ERROR_TEXT else text[:_ERROR_TEXT] + _CUT_MARK


async def _invalid(_request: Request, exc: Exception) -> JSONResponse:
    """Answer a request that does not validate: where and why, never the value.

    Args:
        _request: The request that failed.
        exc: The :class:`RequestValidationError`.

    Returns:
        The 422: ``detail`` lists the first :data:`MAX_ERRORS` errors as ``type``, ``loc`` and ``msg``;
        ``more_errors`` counts the rest, when there are more.
    """
    errors: Sequence[Any] = exc.errors() if isinstance(exc, RequestValidationError) else ()
    detail = [
        {
            "type": str(error.get("type", "")),
            "loc": [_cut(part) if isinstance(part, str) else part for part in error.get("loc", ())],
            "msg": _cut(str(error.get("msg", ""))),
        }
        for error in errors[:MAX_ERRORS]
    ]
    body: dict[str, object] = {"detail": detail}
    if len(errors) > MAX_ERRORS:
        body["more_errors"] = len(errors) - MAX_ERRORS
    return JSONResponse(body, status_code=422)


def install(app: FastAPI) -> None:
    """Register the handlers on ``app``.

    Args:
        app: The application.
    """
    app.add_exception_handler(PathError, _answer)
    app.add_exception_handler(StoreError, _answer)
    app.add_exception_handler(AuditWriteError, _answer)
    app.add_exception_handler(Busy, _answer)
    app.add_exception_handler(RequestTooLarge, _answer)
    app.add_exception_handler(RequestValidationError, _invalid)
