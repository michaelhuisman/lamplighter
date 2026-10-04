"""Starting, fetching and canceling runs (API side)."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Run, RunEvent, RunStatus, Template
from app.models.run import FINAL_STATUSES
from app.services import audit, crud
from app.services.errors import ConflictError, NotFoundError

# Event sequence numbers start at 0 (lamplighter's setup notes); ansible-runner's at 1.
BEFORE_FIRST = -1


def launch(
    session: Session,
    template_id: int,
    *,
    triggered_by: str,
    extra_vars: dict[str, Any] | None = None,  # free-form JSON from the user
    limit: str | None = None,
    actor: audit.Actor | None = None,
) -> Run:
    template = crud.get(session, Template, template_id)
    run = Run(
        template_id=template.id,
        triggered_by=triggered_by,
        status=RunStatus.QUEUED,
        # Manual runs wait for a running run of the same template.
        overlap_policy="queue",
        extra_vars={**template.extra_vars, **(extra_vars or {})},
        limit=limit if limit is not None else template.limit,
    )
    session.add(run)
    session.flush()
    audit.record(
        session,
        actor,
        "run.launch",
        "run",
        run.id,
        {"template_id": template.id, "template": template.name, "limit": run.limit},
    )
    session.commit()
    session.refresh(run)
    return run


def failed_hosts(run: Run) -> list[str]:
    """Hosts that failed or were unreachable, from the run's stats (sorted)."""
    stats = run.stats or {}
    hosts = {
        host
        for key in ("failed", "unreachable")
        for host, count in (stats.get(key) or {}).items()
        if count
    }
    return sorted(hosts)


def relaunch(
    session: Session,
    run_id: int,
    *,
    triggered_by: str,
    failed_hosts_only: bool = False,
    actor: audit.Actor | None = None,
) -> Run:
    """A new manual run of the same template with the original run's effective extra vars
    and limit; with `failed_hosts_only` limited to the hosts that failed or were
    unreachable. The template's current settings apply (playbook, inventory, ...)."""
    original = get(session, run_id)
    if original.status not in FINAL_STATUSES:
        raise ConflictError(f"run {run_id} is still {original.status}")
    limit = original.limit
    if failed_hosts_only:
        hosts = failed_hosts(original)
        if not hosts:
            raise ConflictError(f"run {run_id} has no failed or unreachable hosts")
        limit = ",".join(hosts)
    crud.get(session, Template, original.template_id)
    run = Run(
        template_id=original.template_id,
        triggered_by=triggered_by,
        status=RunStatus.QUEUED,
        overlap_policy="queue",
        extra_vars=dict(original.extra_vars),
        limit=limit,
        relaunch_of=original.id,
    )
    session.add(run)
    session.flush()
    audit.record(
        session,
        actor,
        "run.relaunch",
        "run",
        run.id,
        {"relaunched_from": original.id, "failed_hosts_only": failed_hosts_only, "limit": limit},
    )
    session.commit()
    session.refresh(run)
    return run


def get(session: Session, run_id: int) -> Run:
    return crud.get(session, Run, run_id)


@dataclass(frozen=True)
class RunFilter:
    template_id: int | None = None
    status: RunStatus | None = None
    since: datetime | None = None
    limit: int = 100
    offset: int = 0


def list_runs(session: Session, flt: RunFilter) -> Sequence[Run]:
    stmt = select(Run).order_by(Run.created_at.desc(), Run.id.desc())
    if flt.template_id is not None:
        stmt = stmt.where(Run.template_id == flt.template_id)
    if flt.status is not None:
        stmt = stmt.where(Run.status == flt.status)
    if flt.since is not None:
        stmt = stmt.where(Run.created_at >= flt.since)
    return session.scalars(stmt.limit(flt.limit).offset(flt.offset)).all()


def list_events(
    session: Session, run_id: int, *, after_seq: int = BEFORE_FIRST, limit: int = 500
) -> Sequence[RunEvent]:
    crud.get(session, Run, run_id)
    stmt = (
        select(RunEvent)
        .where(RunEvent.run_id == run_id, RunEvent.seq > after_seq)
        .order_by(RunEvent.seq)
        .limit(limit)
    )
    return session.scalars(stmt).all()


def cancel(session: Session, run_id: int, *, actor: audit.Actor | None = None) -> Run:
    """A queued run is canceled immediately; a running run gets a request that the worker
    picks up via its cancel_callback."""
    run = session.scalars(select(Run).where(Run.id == run_id).with_for_update()).one_or_none()
    if run is None:
        raise NotFoundError(f"runs {run_id} not found")
    now = datetime.now(UTC)
    if run.status == RunStatus.QUEUED:
        run.status = RunStatus.CANCELED
        run.status_reason = "canceled before start"
        run.finished_at = now
        run.cancel_requested_at = now
    elif run.status == RunStatus.RUNNING:
        if run.cancel_requested_at is None:
            run.cancel_requested_at = now
    else:
        session.rollback()
        raise ConflictError(f"run {run_id} is already {run.status}")
    audit.record(session, actor, "run.cancel", "run", run.id, {"status": run.status})
    session.commit()
    session.refresh(run)
    return run
