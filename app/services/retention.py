"""Retention: purge old events, runs, audit entries and API tokens.

Runs are only deleted after their counts (per template and status, plus the duration
histogram) have been added to `run_stats_archive`, in the same transaction. That keeps
the metrics correct. Running and queued runs are never touched.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session

from app.models import ApiToken, AuditEntry, Run, RunEvent, RunStatsArchive, Template
from app.models.run import FINAL_STATUSES

log = logging.getLogger(__name__)

BATCH = 5000
# Same label as the metrics use for runs whose template name is unknown.
DELETED_TEMPLATE = "(deleted template)"
DURATION_BUCKETS = (1, 5, 10, 30, 60, 120, 300, 600, 1800, 3600)
_FINAL = [s.value for s in FINAL_STATUSES]


@dataclass
class RetentionResult:
    events: int = 0
    runs: int = 0
    audit: int = 0
    tokens: int = 0


def _cutoff(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


def purge_events(session: Session, days: int, batch: int = BATCH) -> int:
    """Events of runs that finished more than `days` days ago."""
    if days <= 0:
        return 0
    cutoff, total = _cutoff(days), 0
    while True:
        ids = session.scalars(
            select(RunEvent.id)
            .join(Run, Run.id == RunEvent.run_id)
            .where(Run.status.in_(_FINAL), Run.finished_at < cutoff)
            .limit(batch)
        ).all()
        if not ids:
            return total
        session.execute(delete(RunEvent).where(RunEvent.id.in_(ids)))
        session.commit()
        total += len(ids)


@dataclass
class _Stats:
    runs: int = 0
    duration_count: int = 0
    duration_sum: float = 0.0
    buckets: dict[str, int] = field(default_factory=lambda: {str(b): 0 for b in DURATION_BUCKETS})


def _archive(session: Session, stats: dict[tuple[str, str], _Stats]) -> None:
    for (template, status), add in stats.items():
        row = session.scalar(
            select(RunStatsArchive)
            .where(RunStatsArchive.template == template, RunStatsArchive.status == status)
            .with_for_update()
        )
        if row is None:
            row = RunStatsArchive(
                template=template,
                status=status,
                runs=0,
                duration_count=0,
                duration_sum=0.0,
                duration_buckets={},
            )
            session.add(row)
        row.runs += add.runs
        row.duration_count += add.duration_count
        row.duration_sum += add.duration_sum
        merged = dict(row.duration_buckets or {})
        for le, n in add.buckets.items():
            merged[le] = int(merged.get(le, 0)) + n
        row.duration_buckets = merged


def purge_runs(session: Session, days: int, batch: int = BATCH) -> int:
    """Finished runs older than `days` days (events and notifications go via cascade)."""
    if days <= 0:
        return 0
    cutoff, total = _cutoff(days), 0
    while True:
        rows = session.execute(
            select(
                Run.id,
                Run.status,
                Run.started_at,
                Run.finished_at,
                # Runs of a deleted template count under the name they kept.
                func.coalesce(Template.name, Run.template_name, DELETED_TEMPLATE).label("name"),
            )
            .outerjoin(Template, Template.id == Run.template_id)
            .where(
                Run.status.in_(_FINAL),
                or_(
                    Run.finished_at < cutoff,
                    and_(Run.finished_at.is_(None), Run.created_at < cutoff),
                ),
            )
            .order_by(Run.id)
            .limit(batch)
            .with_for_update(of=Run, skip_locked=True)
        ).all()
        if not rows:
            return total
        stats: dict[tuple[str, str], _Stats] = defaultdict(_Stats)
        for row in rows:
            entry = stats[(row.name, row.status)]
            entry.runs += 1
            if row.started_at is not None and row.finished_at is not None:
                seconds = (row.finished_at - row.started_at).total_seconds()
                entry.duration_count += 1
                entry.duration_sum += seconds
                for b in DURATION_BUCKETS:
                    if seconds <= b:
                        entry.buckets[str(b)] += 1
        _archive(session, stats)
        session.execute(delete(Run).where(Run.id.in_([r.id for r in rows])))
        session.commit()
        total += len(rows)


def purge_audit(session: Session, days: int) -> int:
    if days <= 0:
        return 0
    removed = session.scalars(
        delete(AuditEntry).where(AuditEntry.at < _cutoff(days)).returning(AuditEntry.id)
    ).all()
    session.commit()
    return len(removed)


def purge_tokens(session: Session, days: int) -> int:
    """Revoked or expired API tokens, `days` days after revocation or expiry."""
    if days <= 0:
        return 0
    cutoff = _cutoff(days)
    removed = session.scalars(
        delete(ApiToken)
        .where(or_(ApiToken.revoked_at < cutoff, ApiToken.expires_at < cutoff))
        .returning(ApiToken.id)
    ).all()
    session.commit()
    return len(removed)


def run_all(
    session: Session, *, events_days: int, runs_days: int, audit_days: int, tokens_days: int
) -> RetentionResult:
    result = RetentionResult(
        events=purge_events(session, events_days),
        runs=purge_runs(session, runs_days),
        audit=purge_audit(session, audit_days),
        tokens=purge_tokens(session, tokens_days),
    )
    log.info(
        "retention done",
        extra={
            "events": result.events,
            "runs": result.runs,
            "audit": result.audit,
            "tokens": result.tokens,
        },
    )
    return result
