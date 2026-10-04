"""Categories of templates and the category filter for templates, schedules and runs.

Runs and schedules have no category of their own: they follow their template's.
"""

from collections.abc import Sequence
from typing import Any, Final, Literal

from sqlalchemy import ColumnElement, Select, select
from sqlalchemy.orm import Session

from app.models import Category, Schedule, Template

UNCATEGORIZED: Final = "none"
# A category id, UNCATEGORIZED (templates without a category) or None (no filter).
CategoryFilter = int | Literal["none"] | None


def parse(value: str | None) -> CategoryFilter:
    """Query parameter -> filter. Unknown values mean no filter."""
    if value == UNCATEGORIZED:
        return UNCATEGORIZED
    if value and value.isdigit():
        return int(value)
    return None


def template_clause(flt: CategoryFilter) -> ColumnElement[bool] | None:
    if flt is None:
        return None
    if flt == UNCATEGORIZED:
        return Template.category_id.is_(None)
    return Template.category_id == flt


def apply[S: Select[Any]](stmt: S, flt: CategoryFilter) -> S:
    """Restrict a statement that already includes (or joins) templates."""
    clause = template_clause(flt)
    return stmt if clause is None else stmt.where(clause)


def list_templates(session: Session, flt: CategoryFilter = None) -> Sequence[Template]:
    return session.scalars(apply(select(Template).order_by(Template.id), flt)).all()


def list_schedules(session: Session, flt: CategoryFilter = None) -> Sequence[Schedule]:
    stmt = select(Schedule).order_by(Schedule.id)
    if flt is not None:
        stmt = apply(stmt.join(Template, Template.id == Schedule.template_id), flt)
    return session.scalars(stmt).all()


def list_categories(session: Session) -> Sequence[Category]:
    return session.scalars(select(Category).order_by(Category.name)).all()
