"""Short JSON answers for the errors a path, the store or the audit chain raise on purpose.

The exceptions carry real paths, user ids or file names in their ``__cause__``,
and the model sees every error body, so the answer is a short fixed-text
``{"detail": ...}`` and the handlers log nothing: no traceback, no cause. Every
message of these exceptions is a literal written in ``paths.py`` or ``store.py``.

A failed audit write is a 500 of the same kind: the action is not done, and the cause (the
output stream) is not logged either.
"""

from typing import Final

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from workbench.api.store import Conflict, NotFound, QuotaExceeded, StorageFull, StoreError, TooLarge
from workbench.paths import PathError
from workbench.utils.audit_event import AuditWriteError

#: The detail of every 404.
NOT_FOUND: Final = "not found"

#: Each error, its status, and the detail to send; ``None`` sends the error's own message.
_ANSWERS: Final[tuple[tuple[type[Exception], int, str | None], ...]] = (
    (PathError, 400, None),
    (NotFound, 404, NOT_FOUND),
    (Conflict, 409, None),
    (TooLarge, 413, None),
    (QuotaExceeded, 507, None),
    (StorageFull, 507, None),
    (AuditWriteError, 500, "audit unavailable"),
)


async def _answer(_request: Request, exc: Exception) -> JSONResponse:
    """Turn a path, store or audit error into its short answer, without logging it.

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
