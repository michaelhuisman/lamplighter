"""Worker side of the queue: claiming, updating status, storing events."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import func, insert, or_, select, text, update
from sqlalchemy.orm import Session

from app.core.locks import NS_TEMPLATE
from app.models import Notification, Run, RunEvent, RunStatus

CLAIM_BATCH = 20
TEMPLATE_DELETED = "template was deleted"
# Outbox row that the scheduler still has to expand to the current webhook targets.
ALL_TARGETS = "*"
NOTIFY_STATUSES = frozenset({RunStatus.FAILED, RunStatus.ERROR, RunStatus.TIMEOUT})


class TemplateLocker(Protocol):
    """Overlap lock per template; in the worker a session lock on a dedicated connection."""

    def try_lock(self, template_id: int) -> bool: ...

    def unlock(self, template_id: int) -> None: ...


@dataclass(frozen=True)
class Claimed:
    run_id: int
    template_id: int


# Templates that have a run going somewhere right now. Only a hint to keep waiting
# 'queue' runs from filling the batch; try_lock remains the real decision.
_BUSY_TEMPLATES = text(
    "runs.template_id NOT IN (SELECT objid::bigint FROM pg_locks"
    " WHERE locktype = 'advisory' AND classid = :ns AND objsubid = 2 AND granted)"
).bindparams(ns=NS_TEMPLATE)


def claim(session: Session, worker_id: str, locker: TemplateLocker) -> Claimed | None:
    """Claim the oldest run whose template is free.

    - Competing workers skip each other's locked rows (SKIP LOCKED).
    - If the template is busy: 'skip' runs become `skipped` immediately, 'queue' runs
      stay put and the next candidate is tried.
    """
    candidates = (
        select(Run.id, Run.template_id, Run.overlap_policy)
        .where(Run.status == RunStatus.QUEUED)
        .where(or_(Run.overlap_policy == "skip", _BUSY_TEMPLATES))
        .order_by(Run.created_at, Run.id)
        .with_for_update(skip_locked=True)
        .limit(CLAIM_BATCH)
    )
    locked: int | None = None
    try:
        with session.begin():
            for run_id, template_id, policy in session.execute(candidates).all():
                if template_id is None:
                    # The template was deleted while this run waited (deleting refuses
                    # templates with queued runs, so only in a race).
                    session.execute(
                        update(Run)
                        .where(Run.id == run_id)
                        .values(
                            status=RunStatus.ERROR,
                            status_reason=TEMPLATE_DELETED,
                            finished_at=func.now(),
                        )
                    )
                    continue
                if locker.try_lock(template_id):
                    locked = template_id
                    session.execute(
                        update(Run)
                        .where(Run.id == run_id)
                        .values(
                            status=RunStatus.RUNNING, worker_id=worker_id, started_at=func.now()
                        )
                    )
                    return Claimed(run_id, template_id)
                if policy == "skip":
                    session.execute(
                        update(Run)
                        .where(Run.id == run_id)
                        .values(
                            status=RunStatus.SKIPPED,
                            status_reason="previous run still active",
                            finished_at=func.now(),
                        )
                    )
        return None
    except BaseException:
        if locked is not None:
            locker.unlock(locked)
        raise


def set_commit(session: Session, run_id: int, commit_sha: str) -> None:
    with session.begin():
        session.execute(update(Run).where(Run.id == run_id).values(commit_sha=commit_sha))


def is_cancel_requested(session: Session, run_id: int) -> bool:
    with session.begin():
        return session.scalar(select(Run.cancel_requested_at).where(Run.id == run_id)) is not None


def finish(
    session: Session,
    run_id: int,
    *,
    status: RunStatus,
    rc: int | None = None,
    stats: dict[str, Any] | None = None,  # ansible-runner stats: JSON
    reason: str | None = None,
) -> None:
    """Finish the run. On an error status a notification goes into the outbox in the same
    transaction, so it is still sent after a crash. The worker does not know the
    webhook targets: the scheduler expands the `*` row (see notifications)."""
    with session.begin():
        finished = session.scalar(
            update(Run)
            .where(Run.id == run_id, Run.status == RunStatus.RUNNING)
            .values(
                status=status,
                rc=rc,
                stats=stats,
                status_reason=reason,
                finished_at=func.now(),
            )
            .returning(Run.id)
        )
        if finished is not None and status in NOTIFY_STATUSES:
            session.execute(
                insert(Notification).values(
                    run_id=run_id, target=ALL_TARGETS, event=f"run.{status}"
                )
            )


# Rows for run_events; the keys match the columns.
def add_events(session: Session, rows: Sequence[dict[str, Any]]) -> None:
    if not rows:
        return
    with session.begin():
        session.execute(insert(RunEvent), list(rows))


def fail_orphaned(session: Session, worker_id: str) -> list[int]:
    """Runs still in 'running' for this worker got stuck in a crash."""
    with session.begin():
        result = session.execute(
            update(Run)
            .where(Run.status == RunStatus.RUNNING, Run.worker_id == worker_id)
            .values(
                status=RunStatus.ERROR, status_reason="worker restarted", finished_at=func.now()
            )
            .returning(Run.id)
        )
        return list(result.scalars())


def active_elsewhere(session: Session, run_ids: Sequence[int], worker_id: str) -> set[int]:
    """Which of these runs are currently running on another worker."""
    if not run_ids:
        return set()
    with session.begin():
        return set(
            session.scalars(
                select(Run.id).where(
                    Run.id.in_(run_ids),
                    Run.status == RunStatus.RUNNING,
                    Run.worker_id != worker_id,
                )
            )
        )
