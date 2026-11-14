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
"""

from typing import Final

from fastapi import FastAPI, Request
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
