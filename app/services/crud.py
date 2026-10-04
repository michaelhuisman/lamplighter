"""Generic CRUD for the configuration objects (projects, inventories, ...)."""

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from psycopg import errors as pg_errors
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Entity, Template
from app.services import audit
from app.services.errors import ConflictError, InvalidReferenceError, NotFoundError


@contextmanager
def _translate_integrity_errors(session: Session) -> Iterator[None]:
    try:
        yield
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        orig = exc.orig
        if isinstance(orig, pg_errors.UniqueViolation):
            raise ConflictError("an object with this name already exists") from exc
        if isinstance(orig, pg_errors.ForeignKeyViolation):
            # On insert/update: the reference does not exist. On delete: the object is in use.
            if "is still referenced" in str(orig):
                raise ConflictError("object is still in use") from exc
            raise InvalidReferenceError("referenced object does not exist") from exc
        if isinstance(orig, pg_errors.CheckViolation):
            raise InvalidReferenceError(
                f"constraint violated: {orig.diag.constraint_name}"
            ) from exc
        raise


def list_all[M: Entity](session: Session, model: type[M]) -> Sequence[M]:
    return session.scalars(select(model).order_by(model.id)).all()


def get[M: Entity](session: Session, model: type[M], obj_id: int) -> M:
    obj = session.get(model, obj_id)
    if obj is None:
        raise NotFoundError(f"{model.__tablename__} {obj_id} not found")
    return obj


def _label(obj: Entity) -> dict[str, Any]:
    name = getattr(obj, "name", None)
    return {"name": name} if name is not None else {}


# Values come from validated pydantic schemas (model_dump), hence Any.
def create[M: Entity](
    session: Session,
    model: type[M],
    values: Mapping[str, Any],
    *,
    actor: audit.Actor | None = None,
    audit_details: Mapping[str, Any] | None = None,
) -> M:
    obj = model(**values)
    with _translate_integrity_errors(session):
        session.add(obj)
        session.flush()
        audit.record(
            session,
            actor,
            f"{model.__tablename__}.create",
            model.__tablename__,
            obj.id,
            {**_label(obj), **(audit_details or {})},
        )
    session.refresh(obj)
    return obj


def update[M: Entity](
    session: Session,
    model: type[M],
    obj_id: int,
    values: Mapping[str, Any],
    *,
    actor: audit.Actor | None = None,
) -> M:
    obj = get(session, model, obj_id)
    with _translate_integrity_errors(session):
        # Only field names in the audit log, no values.
        changed = sorted(k for k, v in values.items() if getattr(obj, k) != v)
        for key, value in values.items():
            setattr(obj, key, value)
        session.flush()
        audit.record(
            session,
            actor,
            f"{model.__tablename__}.update",
            model.__tablename__,
            obj.id,
            {**_label(obj), "changed": changed},
        )
    session.refresh(obj)
    return obj


def delete[M: Entity](
    session: Session, model: type[M], obj_id: int, *, actor: audit.Actor | None = None
) -> None:
    obj = get(session, model, obj_id)
    label = _label(obj)
    with _translate_integrity_errors(session):
        session.delete(obj)
        session.flush()
        audit.record(
            session, actor, f"{model.__tablename__}.delete", model.__tablename__, obj_id, label
        )


def template_copy_name(session: Session, name: str) -> str:
    """A free name for a copied template: "<name> (copy)", then "(copy 2)", "(copy 3)", ..."""
    taken = set(session.scalars(select(Template.name).where(Template.name.startswith(name))))
    candidate = f"{name} (copy)"
    n = 2
    while candidate in taken:
        candidate = f"{name} (copy {n})"
        n += 1
    return candidate
