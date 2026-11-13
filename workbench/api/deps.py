"""Accessors for what the application was built with, for routes to depend on."""

from fastapi import Request

from workbench.api.store import Store
from workbench.config import Settings


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
