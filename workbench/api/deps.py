"""Accessors for what the application was built with, for routes to depend on."""

from typing import Annotated

from fastapi import Depends, Request

from workbench.api.audit import AuditLog
from workbench.api.auth import require_principal
from workbench.api.jobs import Busy, JobQueue
from workbench.api.store import Store
from workbench.config import Settings
from workbench.models import Principal


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


#: Route parameter types: the authenticated user and chat, and the collaborators the app was built with.
PrincipalDep = Annotated[Principal, Depends(require_principal)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
StoreDep = Annotated[Store, Depends(get_store)]
AuditDep = Annotated[AuditLog, Depends(get_audit)]
JobsDep = Annotated[JobQueue, Depends(get_jobs)]
