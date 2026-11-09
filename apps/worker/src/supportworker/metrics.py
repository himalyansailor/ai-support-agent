"""Prometheus endpoint of the worker (settings.worker_metrics_port, default 9101).

Celery's prefork pool runs jobs in several child processes, each with its own counters. When
PROMETHEUS_MULTIPROC_DIR is set (the Makefile's dev-worker does it) prometheus_client keeps the
values in files there and the endpoint aggregates every child. The first child to bind the port
serves it; siblings find it taken and rely on that one (a replacement child re-binds if it dies).
Without the directory, only the child that bound the port is exported (a warning says so).
"""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from prometheus_client.core import GaugeMetricFamily

from supportcore.observability.logging import get_logger

logger = get_logger(__name__)

# Celery's default queue: the app routes every task there (no task_routes). Bounded label values.
QUEUE_NAMES = ("celery",)
QUEUE_DEPTH_TIMEOUT_SECONDS = 2


class QueueDepthCollector:
    """support_job_queue_depth{queue}: the Redis list length of each queue, read when Prometheus scrapes.
    A Redis error or timeout (2 s) yields no sample, never a stale or zero value."""

    def __init__(self, redis_factory: Callable[[], Any], queues: tuple[str, ...] = QUEUE_NAMES) -> None:
        self._redis_factory = redis_factory
        self._queues = queues

    def collect(self):
        family = GaugeMetricFamily("support_job_queue_depth", "Jobs waiting in each Celery queue", labels=["queue"])
        try:
            client = self._redis_factory()
            for queue in self._queues:
                family.add_metric([queue], float(client.llen(queue)))
        except Exception as exc:  # timeout, connection refused...: skip the sample
            logger.debug("queue_depth_unavailable", error_type=type(exc).__name__)
            family = GaugeMetricFamily(
                "support_job_queue_depth", "Jobs waiting in each Celery queue", labels=["queue"]
            )
        yield family


def register_queue_depth(registry, broker_url: str) -> None:
    import redis

    client = redis.Redis.from_url(
        broker_url,
        socket_timeout=QUEUE_DEPTH_TIMEOUT_SECONDS,
        socket_connect_timeout=QUEUE_DEPTH_TIMEOUT_SECONDS,
    )
    registry.register(QueueDepthCollector(lambda: client))


def multiprocess_dir() -> str | None:
    return os.environ.get("PROMETHEUS_MULTIPROC_DIR") or None


def wipe_multiprocess_dir() -> None:
    """Values from a previous worker run would be added to this one's; called once, before forking."""
    directory = multiprocess_dir()
    if directory is None:
        return
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    for file in path.glob("*.db"):
        file.unlink(missing_ok=True)


def start_metrics_server(port: int, broker_url: str | None = None) -> bool:
    """Called in each pool process at worker_process_init; True if this process now serves /metrics."""
    from prometheus_client import REGISTRY, CollectorRegistry, multiprocess, start_http_server

    registry = REGISTRY
    if multiprocess_dir() is not None:
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
    try:
        start_http_server(port, registry=registry)
    except OSError:
        if multiprocess_dir() is not None:
            logger.debug("worker_metrics_served_by_sibling", port=port)
        else:
            logger.warning(
                "worker_metrics_port_in_use",
                port=port,
                detail="set PROMETHEUS_MULTIPROC_DIR to export every pool process",
            )
        return False
    if broker_url:  # read at scrape time, by the process that serves /metrics
        register_queue_depth(registry, broker_url)
    logger.info("worker_metrics_server_started", port=port)
    return True


def mark_process_dead() -> None:
    if multiprocess_dir() is not None:
        from prometheus_client import multiprocess

        multiprocess.mark_process_dead(os.getpid())
