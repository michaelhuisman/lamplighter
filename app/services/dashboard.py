"""Dashboard: an overview of the last 24 hours, the queue, schedules and maintenance."""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models import MaintenanceStatus, Run, RunStatus, Schedule, Template
from app.scheduler.trigger import build_trigger, next_fire_time

WINDOW_HOURS = 24
# A daily task that has not succeeded for longer than this needs attention.
MAINTENANCE_STALE = timedelta(hours=26)
MAINTENANCE_TASKS = ("backup", "retention")
UPCOMING = 5
RECENT_FAILURES = 10

BAD = (RunStatus.FAILED, RunStatus.ERROR, RunStatus.TIMEOUT)
# Groups for the chart: one colour each.
GROUPS: dict[str, tuple[RunStatus, ...]] = {
    "ok": (RunStatus.SUCCESSFUL,),
    "bad": BAD,
    "other": (RunStatus.CANCELED, RunStatus.SKIPPED),
    "active": (RunStatus.QUEUED, RunStatus.RUNNING),
}
_GROUP_OF = {status: group for group, statuses in GROUPS.items() for status in statuses}


@dataclass
class HourBucket:
    start: datetime
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(GROUPS, 0))

    @property
    def total(self) -> int:
        return sum(self.counts.values())


@dataclass(frozen=True)
class RunLink:
    run_id: int
    template: str
    status: str
    at: datetime | None


@dataclass(frozen=True)
class FailingSchedule:
    schedule_id: int
    template: str
    cron: str
    last: RunLink


@dataclass(frozen=True)
class Upcoming:
    schedule_id: int
    template: str
    cron: str
    at: datetime


@dataclass(frozen=True)
class Maintenance:
    task: str
    configured: bool
    last_success_at: datetime | None = None
    last_failed: bool = False
    last_error: str | None = None
    stale: bool = False


@dataclass
class Dashboard:
    now: datetime
    totals: dict[str, int]
    success_rate: float | None
    queued: int
    running: int
    hours: list[HourBucket]
    failing: list[FailingSchedule]
    upcoming: list[Upcoming]
    recent_failures: list[RunLink]
    maintenance: list[Maintenance]

    @property
    def max_per_hour(self) -> int:
        return max((b.total for b in self.hours), default=0)


def hour_buckets(rows: Iterable[tuple[datetime, str, int]], now: datetime) -> list[HourBucket]:
    """WINDOW_HOURS buckets of one hour, the last one being the current hour. Rows are
    (hour, status, count); rows outside the window are ignored."""
    current = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    buckets = [HourBucket(current - timedelta(hours=i)) for i in reversed(range(WINDOW_HOURS))]
    by_start = {b.start: b for b in buckets}
    for hour, status, count in rows:
        bucket = by_start.get(hour.astimezone(UTC))
        group = _GROUP_OF.get(RunStatus(status)) if status in set(RunStatus) else None
        if bucket is not None and group is not None:
            bucket.counts[group] += count
    return buckets


def success_rate(totals: dict[str, int]) -> float | None:
    """Share of successful runs among the finished ones that could succeed (canceled and
    skipped runs don't count). None without such runs."""
    good = totals.get(RunStatus.SUCCESSFUL, 0)
    finished = good + sum(totals.get(s, 0) for s in BAD)
    return good / finished if finished else None


def maintenance_state(
    rows: Sequence[MaintenanceStatus], now: datetime, tasks: Sequence[str] = MAINTENANCE_TASKS
) -> list[Maintenance]:
    by_task = {r.task: r for r in rows}
    result = []
    for task in tasks:
        row = by_task.get(task)
        if row is None:
            result.append(Maintenance(task, configured=False))
            continue
        failed = row.last_status == "failed"
        stale = row.last_success_at is None or now - row.last_success_at > MAINTENANCE_STALE
        result.append(
            Maintenance(
                task,
                configured=True,
                last_success_at=row.last_success_at,
                last_failed=failed,
                last_error=row.last_error if failed else None,
                stale=stale,
            )
        )
    return result


def upcoming_runs(
    schedules: Iterable[Schedule], names: dict[int, str], now: datetime, limit: int = UPCOMING
) -> list[Upcoming]:
    """The next `limit` firings of the given (enabled) schedules, soonest first."""
    upcoming = []
    for schedule in schedules:
        at = next_fire_time(build_trigger(schedule.cron, schedule.timezone), now)
        if at is not None:
            template = names.get(schedule.template_id, f"#{schedule.template_id}")
            upcoming.append(Upcoming(schedule.id, template, schedule.cron, at))
    upcoming.sort(key=lambda u: u.at)
    return upcoming[:limit]


_LATEST_PER_SCHEDULE = text(
    "SELECT DISTINCT ON (r.schedule_id) r.schedule_id, r.id, r.status,"
    " coalesce(r.finished_at, r.created_at) AS at"
    " FROM runs r JOIN schedules s ON s.id = r.schedule_id"
    " WHERE s.enabled AND r.status NOT IN ('queued', 'running', 'skipped')"
    " ORDER BY r.schedule_id, r.created_at DESC"
)


def build(session: Session, now: datetime | None = None) -> Dashboard:
    now = now or datetime.now(UTC)
    since = now - timedelta(hours=WINDOW_HOURS)
    names = dict(session.execute(select(Template.id, Template.name)).tuples().all())

    totals: dict[str, int] = defaultdict(int)
    for status, count in session.execute(
        select(Run.status, func.count()).where(Run.created_at >= since).group_by(Run.status)
    ).tuples():
        totals[status] = count

    hour = func.date_trunc("hour", Run.created_at)
    per_hour = session.execute(
        select(hour, Run.status, func.count())
        .where(Run.created_at >= since)
        .group_by(hour, Run.status)
    ).tuples()

    active = dict(
        session.execute(
            select(Run.status, func.count())
            .where(Run.status.in_([RunStatus.QUEUED, RunStatus.RUNNING]))
            .group_by(Run.status)
        )
        .tuples()
        .all()
    )

    schedules = {s.id: s for s in session.scalars(select(Schedule).where(Schedule.enabled))}
    failing = []
    for schedule_id, run_id, status, at in session.execute(_LATEST_PER_SCHEDULE).tuples():
        schedule = schedules.get(schedule_id)
        if schedule is not None and status in BAD:
            template = names.get(schedule.template_id, f"#{schedule.template_id}")
            failing.append(
                FailingSchedule(
                    schedule_id, template, schedule.cron, RunLink(run_id, template, status, at)
                )
            )

    recent = [
        RunLink(r.id, names.get(r.template_id, f"#{r.template_id}"), r.status, r.finished_at)
        for r in session.scalars(
            select(Run)
            .where(Run.status.in_(BAD))
            .order_by(Run.finished_at.desc().nulls_last(), Run.id.desc())
            .limit(RECENT_FAILURES)
        )
    ]

    return Dashboard(
        now=now,
        totals=dict(totals),
        success_rate=success_rate(totals),
        queued=active.get(RunStatus.QUEUED, 0),
        running=active.get(RunStatus.RUNNING, 0),
        hours=hour_buckets(per_hour, now),
        failing=failing,
        upcoming=upcoming_runs(schedules.values(), names, now),
        recent_failures=recent,
        maintenance=maintenance_state(session.scalars(select(MaintenanceStatus)).all(), now),
    )
