"""Prometheus metrics: counts and durations only, never who asked.

The metrics live in their own registry, so nothing the process registers elsewhere leaks into
the scrape. ``GET /metrics`` is unauthenticated: ``workbench-net`` has no member that is not the
caller or the scraper, and the page holds no user id, chat id, file name or other content.
Labels are the tool's name and the reason a job was refused, nothing else.
"""

import logging
import shutil
from pathlib import Path
from typing import Final

from fastapi import APIRouter, Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest

from workbench.api.routing import EncodableRoute

_log = logging.getLogger(__name__)

#: Seconds a job waited for a slot; most wait none, the longest may wait ``MCP_QUEUE_WAIT_S``.
_WAIT_BUCKETS: Final = (0.005, 0.05, 0.25, 0.5, 1, 2.5, 5, 10)

#: Seconds a job ran in the executor, up to its wall time plus the executor's cleanup.
_DURATION_BUCKETS: Final = (0.1, 0.5, 1, 2.5, 5, 10, 20, 30, 60, 120, 240, 480)


class Metrics:
    """The api's metrics, in a registry of their own.

    Attributes:
        registry: The registry every metric below is in.
        queue_length: Jobs that wait for a slot now.
        queue_wait_seconds: How long jobs waited for a slot, those refused after the wait included.
        job_duration_seconds: How long jobs ran in the executor, by ``tool``.
        job_refusals_total: Jobs refused, by ``reason``.
        disk_used_bytes: Bytes used on the data volume's filesystem.
        disk_capacity_bytes: Size of the data volume's filesystem.
        files_total: Files in the store.
    """

    def __init__(self, data_dir: Path) -> None:
        """Create the metrics, all at zero; call :meth:`refresh` to measure the volume.

        Args:
            data_dir: Root of the data volume, measured for the disk gauges.
        """
        self._data_dir = data_dir
        self.registry = CollectorRegistry()
        self.queue_length = Gauge("workbench_queue_length", "Jobs waiting for a free slot.", registry=self.registry)
        self.queue_wait_seconds = Histogram(
            "workbench_queue_wait_seconds",
            "Seconds jobs waited for a free slot.",
            registry=self.registry,
            buckets=_WAIT_BUCKETS,
        )
        self.job_duration_seconds = Histogram(
            "workbench_job_duration_seconds",
            "Seconds jobs ran in the executor.",
            ["tool"],
            registry=self.registry,
            buckets=_DURATION_BUCKETS,
        )
        self.job_refusals_total = Counter(
            "workbench_job_refusals_total",
            "Jobs refused: queue_full, wait_timeout or executor_busy.",
            ["reason"],
            registry=self.registry,
        )
        self.disk_used_bytes = Gauge(
            "workbench_disk_used_bytes", "Bytes used on the data volume's filesystem.", registry=self.registry
        )
        self.disk_capacity_bytes = Gauge(
            "workbench_disk_capacity_bytes", "Size of the data volume's filesystem in bytes.", registry=self.registry
        )
        self.files_total = Gauge("workbench_files_total", "Files in the workspaces.", registry=self.registry)

    def refresh(self, files: int) -> None:
        """Set the gauges that are measured, not counted as they happen: disk use and the file count.

        Called when the api starts and by each retention sweep. A volume that cannot be measured
        leaves the disk gauges as they were; the failure is logged without the path.

        Args:
            files: How many files the store holds now.
        """
        self.files_total.set(files)
        try:
            usage = shutil.disk_usage(self._data_dir)
        except OSError as exc:
            _log.warning("could not measure the data volume (%s)", type(exc).__name__)
            return
        self.disk_used_bytes.set(usage.used)
        self.disk_capacity_bytes.set(usage.total)

    def render(self) -> bytes:
        """Return the registry in the Prometheus text format.

        Returns:
            The text, UTF-8 encoded.
        """
        return generate_latest(self.registry)


router = APIRouter(include_in_schema=False, route_class=EncodableRoute)


@router.get("/metrics")
async def scrape(request: Request) -> Response:
    """Answer a Prometheus scrape; no key, nothing the model can call.

    Args:
        request: The request.

    Returns:
        The metrics as Prometheus text.
    """
    metrics: Metrics = request.app.state.metrics
    return Response(metrics.render(), media_type=CONTENT_TYPE_LATEST)
