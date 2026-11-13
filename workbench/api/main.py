"""The workbench api application.

Run it as ``uvicorn --factory workbench.api.main:app_factory``. Authentication
is per route (:mod:`workbench.api.auth`), not middleware, so a path that is not
registered answers 404 whatever the request carries.
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from workbench import __version__
from workbench.api import contract, errors, files, tools
from workbench.api.audit import AuditLog
from workbench.api.auth import CallerRegistry
from workbench.api.store import Store
from workbench.config import Settings, load_settings
from workbench.paths import Workspaces

_DB_NAME = "workbench.db"


def _audit_config(settings: Settings) -> dict[str, object]:
    """Pick what the audit chain records of the configuration: limits and periods only.

    Args:
        settings: The api's settings.

    Returns:
        The values to record. It never holds a key or any other secret.
    """
    return {
        "retention_days": settings.retention_days,
        "executor_slots": settings.executor_slots,
        "queue_max": settings.queue_max,
        "job_wall_s": settings.job_wall_s,
        "upload_max_mb": settings.upload_max_mb,
        "workspace_max_mb": settings.workspace_max_mb,
    }


def create_app(settings: Settings, *, store: Store, audit: AuditLog, jobs: object | None = None) -> FastAPI:
    """Build the application over its collaborators.

    Args:
        settings: The api's settings; they must hold the caller key.
        store: Files and job records on the data volume.
        audit: The audit chain; the lifespan starts it and ends it.
        jobs: The job queue that runs tools in the executor, once there is one.

    Returns:
        The application, without FastAPI's own spec and documentation routes.

    Raises:
        ConfigError: If the settings hold no caller key.
    """
    callers = CallerRegistry.from_settings(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Open the audit chain on start and end it on shutdown.

        Args:
            _app: The application.

        Yields:
            Once the chain is open.
        """
        audit.start(_audit_config(settings))
        try:
            yield
        finally:
            audit.close()

    app = FastAPI(
        title="workbench",
        version=__version__,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.audit = audit
    app.state.jobs = jobs
    app.state.callers = callers
    errors.install(app)
    app.include_router(contract.router)
    app.include_router(files.router)
    app.include_router(files.tool_router)
    app.include_router(tools.router)
    return app


def app_factory() -> FastAPI:
    """Build the application from the environment, for ``uvicorn --factory``.

    Returns:
        The application, with its database at ``<data dir>/workbench.db``.

    Raises:
        ConfigError: If the environment holds a value the api refuses to start with.
    """
    settings = load_settings(os.environ, role="api")
    store = Store(settings.data_dir / _DB_NAME, Workspaces(settings.data_dir))
    return create_app(settings, store=store, audit=AuditLog.open(settings, __version__))
