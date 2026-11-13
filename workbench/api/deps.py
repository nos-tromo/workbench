"""Accessors for what the application was built with, for routes to depend on."""

from typing import Annotated

from fastapi import Depends, Request

from workbench.api.audit import AuditLog
from workbench.api.auth import require_principal
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


#: Route parameter types: the authenticated user and chat, and the collaborators the app was built with.
PrincipalDep = Annotated[Principal, Depends(require_principal)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
StoreDep = Annotated[Store, Depends(get_store)]
AuditDep = Annotated[AuditLog, Depends(get_audit)]
