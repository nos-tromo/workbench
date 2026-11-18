"""The workbench api application.

Run it as ``uvicorn --factory workbench.api.main:app_factory``. A caller key is
checked before any body is read (:class:`~workbench.api.auth.KeyGate`), and again
per route; a path that is not registered answers 404 to a caller with the key and
401 to anyone else.
"""

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from workbench import __version__
from workbench.api import errors, files, health, mcp, metrics
from workbench.api.audit import AuditLog
from workbench.api.auth import CallerRegistry, KeyGate
from workbench.api.body_limit import BodyLimit, upload_limit
from workbench.api.hardening import ViewHardening
from workbench.api.jobs import ExecutorClient, JobQueue
from workbench.api.metrics import Metrics
from workbench.api.registry import Registry
from workbench.api.retention import Sweeper
from workbench.api.store import Store
from workbench.api.tools_inline import INLINE_SPECS
from workbench.api.tools_mcp import workspace_tools
from workbench.config import Settings, load_settings
from workbench.layout import DATABASE
from workbench.paths import Workspaces


def _audit_config(settings: Settings, callers: CallerRegistry) -> dict[str, object]:
    """Pick what the audit chain records of the configuration: limits, periods, the callers and the purpose rule.

    Args:
        settings: The api's settings.
        callers: The callers the api serves.

    Returns:
        The values to record: each caller by its name, its door (every caller's is the MCP door), tools, wall cap and
        slots, in name order, and the actions that need a stated purpose, sorted. It never holds a key or any other
        secret.
    """
    return {
        "retention_days": settings.retention_days,
        "executor_slots": settings.executor_slots,
        "queue_max": settings.queue_max,
        "job_wall_s": settings.job_wall_s,
        "upload_max_mb": settings.upload_max_mb,
        "workspace_max_mb": settings.workspace_max_mb,
        "callers": [
            {
                "name": caller.name,
                "door": "mcp",
                "tools": sorted(caller.tools),
                "wall_s": caller.wall_s,
                "slots": caller.slots,
            }
            for caller in sorted(callers.callers, key=lambda caller: caller.name)
        ],
        "require_purpose": sorted(settings.require_purpose),
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
        settings: The api's settings; they must hold a caller key.
        store: Files and job records on the data volume.
        audit: The audit chain; the lifespan starts it and ends it.
        jobs: The job queue that runs tools in the executor, once there is one. ``/metrics`` serves its
            metrics; without one, the app has metrics of its own.
        sweeper: The retention sweep, which the lifespan runs while the app serves; none without one.

    Returns:
        The application, without FastAPI's own spec and documentation routes, its tools in ``app.state.registry``.

    Raises:
        ConfigError: If the settings hold no caller key, two callers hold one key, or docint's list names a tool that
            is not an inline tool.
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
        audit.start(_audit_config(settings, callers))
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

    # No slash redirects: a trailing slash is a 404, never a 307 that sends a caller on to another path.
    app = FastAPI(
        title="workbench",
        version=__version__,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        redirect_slashes=False,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.audit = audit
    app.state.jobs = jobs
    app.state.metrics = app_metrics
    app.state.callers = callers
    errors.install(app)
    # Each add wraps the ones before it, so a request meets ViewHardening, QuietErrors, KeyGate, BodyLimit, then the
    # app: no body is read before the key is checked, and none past its bound. An error nothing else answers becomes
    # QuietErrors' 500, which still gets the viewing headers.
    app.add_middleware(BodyLimit, upload_max=upload_limit(settings))
    app.add_middleware(KeyGate, callers=callers)
    app.add_middleware(errors.QuietErrors)
    app.add_middleware(ViewHardening)
    app.include_router(health.router)
    app.include_router(mcp.router)
    app.include_router(files.router)
    app.include_router(metrics.router)
    app.state.registry = Registry([*workspace_tools(), *INLINE_SPECS])
    return app


def app_factory() -> FastAPI:
    """Build the application from the environment, for ``uvicorn --factory``.

    Returns:
        The application, with its database at ``<data dir>/workbench.db`` and its retention sweep.

    Raises:
        ConfigError: If the environment holds a value the api refuses to start with.
    """
    settings = load_settings(os.environ, role="api")
    store = Store(settings.data_dir / DATABASE, Workspaces(settings.data_dir))
    metrics = Metrics(settings.data_dir)
    jobs = JobQueue(settings, ExecutorClient(settings.executor_socket), store, metrics)
    audit = AuditLog.open(settings, __version__)
    sweeper = Sweeper(store, audit, settings, metrics)
    return create_app(settings, store=store, audit=audit, jobs=jobs, sweeper=sweeper)
