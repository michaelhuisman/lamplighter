"""Dashboard calculations (app/services/dashboard.py) without a database."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.services.dashboard import (
    MAINTENANCE_STALE,
    WINDOW_HOURS,
    hour_buckets,
    maintenance_state,
    success_rate,
    upcoming_runs,
)

NOW = datetime(2026, 10, 1, 14, 37, tzinfo=UTC)


def test_hour_buckets_cover_the_window_and_group_statuses() -> None:
    this_hour = NOW.replace(minute=0)
    rows = [
        (this_hour, "successful", 3),
        (this_hour, "failed", 1),
        (this_hour, "timeout", 1),
        (this_hour - timedelta(hours=2), "canceled", 2),
        (this_hour - timedelta(hours=5), "running", 1),
        (this_hour - timedelta(hours=WINDOW_HOURS), "successful", 9),  # outside the window
        (this_hour, "unknown-status", 4),  # ignored
    ]
    buckets = hour_buckets(rows, NOW)
    assert len(buckets) == WINDOW_HOURS
    assert buckets[-1].start == this_hour
    assert buckets[0].start == this_hour - timedelta(hours=WINDOW_HOURS - 1)
    assert buckets[-1].counts == {"ok": 3, "bad": 2, "other": 0, "active": 0}
    assert buckets[-3].counts["other"] == 2
    assert buckets[-6].counts["active"] == 1
    assert sum(b.total for b in buckets) == 3 + 2 + 2 + 1


def test_hour_buckets_accept_other_time_zones() -> None:
    local = NOW.replace(minute=0).astimezone(ZoneInfo("Europe/Amsterdam"))
    buckets = hour_buckets([(local, "successful", 1)], NOW)
    assert buckets[-1].counts["ok"] == 1


def test_success_rate() -> None:
    assert success_rate({}) is None
    assert success_rate({"canceled": 4, "skipped": 1}) is None
    assert success_rate({"successful": 3, "failed": 1}) == 0.75
    assert success_rate({"successful": 1, "error": 1, "timeout": 2, "canceled": 9}) == 0.25


def _row(
    task: str, status: str, success: datetime | None, error: str | None = None
) -> SimpleNamespace:
    return SimpleNamespace(task=task, last_status=status, last_success_at=success, last_error=error)


def test_maintenance_state() -> None:
    rows = [
        _row("backup", "failed", NOW - timedelta(hours=3), "pg_dump failed"),
        _row("retention", "ok", NOW - MAINTENANCE_STALE - timedelta(minutes=1)),
    ]
    backup, retention, other = maintenance_state(rows, NOW, ("backup", "retention", "other"))  # type: ignore[arg-type]
    assert backup.configured
    assert backup.last_failed
    assert backup.last_error == "pg_dump failed"
    assert not backup.stale
    assert retention.stale
    assert not retention.last_failed
    assert retention.last_error is None
    assert not other.configured


def test_upcoming_runs_soonest_first() -> None:
    schedules = [
        SimpleNamespace(id=1, template_id=10, cron="0 0 1 1 *", timezone="UTC"),
        SimpleNamespace(id=2, template_id=11, cron="0 15 * * *", timezone="UTC"),
        SimpleNamespace(id=3, template_id=12, cron="45 14 * * *", timezone="Europe/Amsterdam"),
        SimpleNamespace(id=4, template_id=99, cron="*/5 * * * *", timezone="UTC"),
    ]
    result = upcoming_runs(schedules, {10: "yearly", 11: "daily", 12: "local"}, NOW, limit=3)  # type: ignore[arg-type]
    assert [u.schedule_id for u in result] == [4, 2, 3]
    assert result[0].at == NOW.replace(minute=40)
    assert result[0].template == "#99"  # unknown template id
    assert result[1].at == NOW.replace(hour=15, minute=0)
    # 14:45 in Amsterdam (UTC+2 in October) is 12:45 UTC tomorrow.
    assert result[2].at == (NOW + timedelta(days=1)).replace(hour=12, minute=45)
