"""Deleting templates while keeping the run history."""

from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import Run, RunStatus, Schedule, Template
from app.services import audit, schedules
from app.services.errors import ConflictError, NotFoundError

ACTIVE = (RunStatus.QUEUED, RunStatus.RUNNING)


@dataclass(frozen=True)
class Usage:
    runs: int = 0
    schedules: int = 0


def usage(session: Session) -> dict[int, Usage]:
    """Runs and schedules per template (for the confirmation when deleting)."""
    run_counts = dict(
        session.execute(
            select(Run.template_id, func.count())
            .where(Run.template_id.is_not(None))
            .group_by(Run.template_id)
        )
        .tuples()
        .all()
    )
    schedule_counts = dict(
        session.execute(select(Schedule.template_id, func.count()).group_by(Schedule.template_id))
        .tuples()
        .all()
    )
    return {
        tid: Usage(run_counts.get(tid, 0), schedule_counts.get(tid, 0))
        for tid in set(run_counts) | set(schedule_counts)
        if tid is not None
    }


def delete(session: Session, template_id: int, *, actor: audit.Actor | None = None) -> Usage:
    """Delete a template and its schedules. Its runs stay: they keep the template's name
    (runs.template_name) and lose the reference (ON DELETE SET NULL). Refused while runs
    of the template are queued or running."""
    template = session.scalars(
        select(Template).where(Template.id == template_id).with_for_update()
    ).one_or_none()
    if template is None:
        raise NotFoundError(f"templates {template_id} not found")
    active = session.scalar(
        select(func.count()).where(Run.template_id == template_id, Run.status.in_(ACTIVE))
    )
    if active:
        session.rollback()
        raise ConflictError(
            f"template '{template.name}' has {active} queued or running run(s);"
            " wait for them or cancel them first"
        )
    runs_kept = session.scalar(select(func.count()).where(Run.template_id == template_id)) or 0
    session.execute(
        update(Run).where(Run.template_id == template_id).values(template_name=template.name)
    )
    to_delete = session.scalars(select(Schedule).where(Schedule.template_id == template_id)).all()
    for schedule in to_delete:
        session.delete(schedule)
        audit.record(
            session,
            actor,
            "schedules.delete",
            "schedules",
            schedule.id,
            {"reason": "template deleted", "template": template.name},
        )
    session.delete(template)
    session.flush()
    audit.record(
        session,
        actor,
        "templates.delete",
        "templates",
        template_id,
        {"name": template.name, "runs_kept": runs_kept, "schedules_deleted": len(to_delete)},
    )
    if to_delete:
        schedules.notify_changed(session)
    session.commit()
    return Usage(runs_kept, len(to_delete))
