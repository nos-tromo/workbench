"""The workbench api application.

Run it as ``uvicorn --factory workbench.api.main:app_factory``. Authentication
is per route (:mod:`workbench.api.auth`), not middleware, so a path that is not
registered answers 404 whatever the request carries.
"""

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from workbench import __version__
from workbench.api import contract, errors, files, metrics, tools, tools_output, tools_templates  # noqa: F401 (routes)
from workbench.api.audit import AuditLog
from workbench.api.auth import CallerRegistry
from workbench.api.hardening import ViewHardening
from workbench.api.jobs import ExecutorClient, JobQueue
from workbench.api.metrics import Metrics
from workbench.api.retention import Sweeper
from workbench.api.store import DATABASE_NAME, Store
from workbench.api.upload_limit import UploadLimit, body_limit
from workbench.config import Settings, load_settings
from workbench.paths import Workspaces


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


def create_app(
    settings: Settings,
    *,
    store: Store,
    audit: AuditLog,
    jobs: JobQueue | None = None,
    sweeper: Sweeper | None = None,
) -> FastAPI:
    """Build the application over its collaborators.

    Args:
        settings: The api's settings; they must hold the caller key.
        store: Files and job records on the data volume.
        audit: The audit chain; the lifespan starts it and ends it.
        jobs: The job queue that runs tools in the executor, once there is one. ``/metrics`` serves its
            metrics; without one, the app has metrics of its own.
        sweeper: The retention sweep, which the lifespan runs while the app serves; none without one.

    Returns:
        The application, without FastAPI's own spec and documentation routes.

    Raises:
        ConfigError: If the settings hold no caller key.
    """
    callers = CallerRegistry.from_settings(settings)
    app_metrics = jobs.metrics if jobs is not None else Metrics(settings.data_dir)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Open the audit chain, measure the data volume and start the retention sweep; on shutdown, the reverse.

        The sweep starts once the chain is open, and its first run does not wait for a request. On shutdown it is
        cancelled and awaited: the sweep in hand stops before its next record or removal (the files of a batch it
        recorded and did not remove yet are recorded again by the next sweep), and only then does the chain end. A start
        that fails once the chain is open ends the chain too.

        Args:
            _app: The application.

        Yields:
            Once the chain is open and the sweep started.
        """
        audit.start(_audit_config(settings))
        try:
            app_metrics.refresh(files=store.count_files())
            sweeping = None if sweeper is None else asyncio.create_task(sweeper.run_forever())
            try:
                yield
            finally:
                if sweeping is not None:
                    sweeping.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await sweeping
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
    app.state.metrics = app_metrics
    app.state.callers = callers
    errors.install(app)
    app.add_middleware(UploadLimit, max_body=body_limit(settings))
    app.add_middleware(ViewHardening)
    app.include_router(contract.router)
    app.include_router(files.router)
    app.include_router(files.tool_router)
    app.include_router(tools.router)  # the template and output tools' routes are on it too
    app.include_router(metrics.router)
    return app


def app_factory() -> FastAPI:
    """Build the application from the environment, for ``uvicorn --factory``.

    Returns:
        The application, with its database at ``<data dir>/workbench.db`` and its retention sweep.

    Raises:
        ConfigError: If the environment holds a value the api refuses to start with.
    """
    settings = load_settings(os.environ, role="api")
    store = Store(settings.data_dir / DATABASE_NAME, Workspaces(settings.data_dir))
    metrics = Metrics(settings.data_dir)
    jobs = JobQueue(settings, ExecutorClient(settings.executor_socket), store, metrics)
    audit = AuditLog.open(settings, __version__)
    sweeper = Sweeper(store, audit, settings, metrics)
    return create_app(settings, store=store, audit=audit, jobs=jobs, sweeper=sweeper)
