"""Accessors for what the application was built with, for routes to depend on."""

from typing import Annotated

from fastapi import Depends, HTTPException, Request

from workbench.api.audit import AuditLog
from workbench.api.auth import (
    Caller,
    InvalidPurpose,
    purpose_from_headers,
    require_file_caller,
    require_file_principal,
    require_principal,
)
from workbench.api.errors import INVALID_PURPOSE
from workbench.api.jobs import Busy, JobQueue
from workbench.api.store import Store
from workbench.config import Settings
from workbench.models import Principal
from workbench.utils.audit_runtime import clean_request_id


async def get_settings(request: Request) -> Settings:
    """Return the settings the application was created with.

    Args:
        request: The request.

    Returns:
        The settings.
    """
    settings: Settings = request.app.state.settings
    return settings


async def get_store(request: Request) -> Store:
    """Return the store the application was created with.

    Args:
        request: The request.

    Returns:
        The store.
    """
    store: Store = request.app.state.store
    return store


async def get_audit(request: Request) -> AuditLog:
    """Return the audit log the application was created with.

    Args:
        request: The request.

    Returns:
        The audit log.
    """
    audit: AuditLog = request.app.state.audit
    return audit


async def get_jobs(request: Request) -> JobQueue:
    """Return the job queue the application was created with.

    Args:
        request: The request.

    Returns:
        The queue.

    Raises:
        Busy: The application has no queue, so no tool can run; answered as "busy, retry later".
    """
    jobs: JobQueue | None = request.app.state.jobs
    if jobs is None:
        raise Busy("executor_busy")
    return jobs


async def call_audit(
    request: Request,
    caller: Annotated[Caller, Depends(require_file_caller)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AuditLog:
    """Return the log a file route records through: the app's, bound to the call for a confined caller.

    Args:
        request: The request; ``X-Audit-Purpose`` and ``X-Request-ID`` are read from it.
        caller: The caller :func:`~workbench.api.auth.require_file_caller` let through.
        settings: The api's settings; ``require_purpose`` names the actions that need a purpose.

    Returns:
        The app's log for a terminal caller, whose calls state no purpose. For a confined caller, that log
        :meth:`~workbench.api.audit.AuditLog.bound` to the stated purpose, the request id and ``require_purpose``.

    Raises:
        HTTPException: 400 with :data:`~workbench.api.errors.INVALID_PURPOSE` if a confined caller's
            ``X-Audit-Purpose`` is malformed.
    """
    audit: AuditLog = request.app.state.audit
    if not caller.confined:
        return audit
    try:
        purpose = purpose_from_headers(request.headers)
    except InvalidPurpose:
        raise HTTPException(status_code=400, detail=INVALID_PURPOSE) from None
    request_id = clean_request_id(request.headers.get("x-request-id"))
    return audit.bound(purpose, request_id, required=settings.require_purpose)


#: Route parameter types: the authenticated user and chat, and the collaborators the app was built with.
PrincipalDep = Annotated[Principal, Depends(require_principal)]
#: The user and chat on a file route a confined caller shares with the terminal door, and the log it records through.
FilePrincipalDep = Annotated[Principal, Depends(require_file_principal)]
CallAuditDep = Annotated[AuditLog, Depends(call_audit)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
StoreDep = Annotated[Store, Depends(get_store)]
AuditDep = Annotated[AuditLog, Depends(get_audit)]
JobsDep = Annotated[JobQueue, Depends(get_jobs)]
