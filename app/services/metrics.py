"""Prometheus metrics, computed from Postgres on every scrape.

api, scheduler and workers are separate processes (and replicas); in-process counters
would differ per process. The database is the single source of truth and always current.
"""

from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field

from prometheus_client import CollectorRegistry
from prometheus_client.core import (
    CounterMetricFamily,
    GaugeMetricFamily,
    HistogramMetricFamily,
    Metric,
)
from prometheus_client.registry import Collector
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.services.retention import DURATION_BUCKETS

# Runs of a deleted template keep counting under the name they kept (runs.template_name).
_TEMPLATE = "coalesce(t.name, r.template_name, '(deleted template)')"

_RUNS_BY_STATUS = text(
    f"SELECT {_TEMPLATE} AS template, r.status, count(*) AS n"  # noqa: S608
    " FROM runs r LEFT JOIN templates t ON t.id = r.template_id"
    " GROUP BY 1, r.status"
)

_bucket_cols = ", ".join(f"count(*) FILTER (WHERE d <= {b}) AS le_{b}" for b in DURATION_BUCKETS)
_DURATIONS = text(
    # Only constant bucket bounds in the f-string, no input.
    f"SELECT template, {_bucket_cols}, count(*) AS n, coalesce(sum(d), 0) AS total"  # noqa: S608
    f" FROM (SELECT {_TEMPLATE} AS template,"
    "  extract(epoch FROM r.finished_at - r.started_at) AS d"
    "  FROM runs r LEFT JOIN templates t ON t.id = r.template_id"
    "  WHERE r.started_at IS NOT NULL AND r.finished_at IS NOT NULL) x"
    " GROUP BY template"
)

_QUEUE = text(
    "SELECT count(*) FILTER (WHERE status = 'queued') AS queued,"
    " count(*) FILTER (WHERE status = 'running') AS running FROM runs"
)

# Counts of runs already removed by retention.
_ARCHIVE = text(
    "SELECT template, status, runs, duration_count, duration_sum, duration_buckets"
    " FROM run_stats_archive"
)

_LAST_SUCCESS = text(
    f"SELECT r.schedule_id, {_TEMPLATE} AS template,"  # noqa: S608
    " extract(epoch FROM max(r.finished_at)) AS ts"
    " FROM runs r LEFT JOIN templates t ON t.id = r.template_id"
    " WHERE r.schedule_id IS NOT NULL AND r.status = 'successful'"
    " GROUP BY r.schedule_id, 2"
)


_MAINTENANCE = text(
    "SELECT task, last_status, extract(epoch FROM last_success_at) AS success_ts"
    " FROM maintenance_status"
)


@dataclass
class _Histogram:
    count: int = 0
    total: float = 0.0
    buckets: dict[int, int] = field(default_factory=lambda: dict.fromkeys(DURATION_BUCKETS, 0))


class RunMetricsCollector(Collector):
    def __init__(self, sm: sessionmaker[Session]) -> None:
        self._sm = sm

    def collect(self) -> Iterator[Metric]:
        with self._sm() as session:
            by_status = session.execute(_RUNS_BY_STATUS).all()
            durations = session.execute(_DURATIONS).all()
            queue = session.execute(_QUEUE).one()
            last_success = session.execute(_LAST_SUCCESS).all()
            archive = session.execute(_ARCHIVE).all()
            maint = session.execute(_MAINTENANCE).all()

        # Live runs + archive, so retention does not make the counters drop.
        counts: dict[tuple[str, str], int] = defaultdict(int)
        hists: dict[str, _Histogram] = defaultdict(_Histogram)
        for row in by_status:
            counts[(row.template, row.status)] += row.n
        for row in durations:
            h = hists[row.template]
            h.count += row.n
            h.total += float(row.total)
            for b in DURATION_BUCKETS:
                h.buckets[b] += getattr(row, f"le_{b}")
        for row in archive:
            counts[(row.template, row.status)] += row.runs
            h = hists[row.template]
            h.count += row.duration_count
            h.total += float(row.duration_sum)
            for b in DURATION_BUCKETS:
                h.buckets[b] += int((row.duration_buckets or {}).get(str(b), 0))

        runs = CounterMetricFamily(
            "lamplighter_runs", "Runs per template and status", labels=["template", "status"]
        )
        for (template, status), n in sorted(counts.items()):
            runs.add_metric([template, status], n)
        yield runs

        hist = HistogramMetricFamily(
            "lamplighter_run_duration_seconds",
            "Duration of finished runs",
            labels=["template"],
        )
        for template, h in sorted(hists.items()):
            buckets = [(str(float(b)), h.buckets[b]) for b in DURATION_BUCKETS]
            buckets.append(("+Inf", h.count))
            hist.add_metric([template], buckets, sum_value=h.total)
        yield hist

        depth = GaugeMetricFamily("lamplighter_queue_depth", "Runs waiting in the queue")
        depth.add_metric([], queue.queued)
        yield depth

        running = GaugeMetricFamily("lamplighter_runs_running", "Runs currently running")
        running.add_metric([], queue.running)
        yield running

        last = GaugeMetricFamily(
            "lamplighter_schedule_last_success_timestamp_seconds",
            "Finish time of the last successful run per schedule",
            labels=["schedule_id", "template"],
        )
        for row in last_success:
            last.add_metric([str(row.schedule_id), row.template], float(row.ts))
        yield last

        # Alert on these: e.g. no successful backup for more than 26 hours.
        maint_success = GaugeMetricFamily(
            "lamplighter_maintenance_last_success_timestamp_seconds",
            "Time of the last successful run of a maintenance task (retention, backup)",
            labels=["task"],
        )
        maint_failed = GaugeMetricFamily(
            "lamplighter_maintenance_last_attempt_failed",
            "1 if the last attempt of a maintenance task failed, otherwise 0",
            labels=["task"],
        )
        for row in maint:
            if row.success_ts is not None:
                maint_success.add_metric([row.task], float(row.success_ts))
            maint_failed.add_metric([row.task], 1.0 if row.last_status == "failed" else 0.0)
        yield maint_success
        yield maint_failed


def build_registry(sm: sessionmaker[Session]) -> CollectorRegistry:
    registry = CollectorRegistry(auto_describe=False)
    registry.register(RunMetricsCollector(sm))
    return registry
