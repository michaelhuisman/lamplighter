"""CRUD routers for configuration objects."""

from collections.abc import Callable, Sequence
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api import schemas
from app.api.deps import ActorDep, SessionDep, require
from app.core.auth import Action
from app.models import Category, Credential, Entity, Inventory, Project, Schedule, Template
from app.services import categories, crud, schedules, templates

READ = [Depends(require(Action.READ))]
CONFIGURE = [Depends(require(Action.CONFIGURE))]


def crud_router[In: BaseModel, Out: BaseModel](
    prefix: str,
    model: type[Entity],
    schema_in: type[In],
    schema_out: type[Out],
    on_change: Callable[[Session], None] | None = None,
    by_category: Callable[[Session, categories.CategoryFilter], Sequence[Entity]] | None = None,
    deleter: Callable[..., object] | None = None,
) -> APIRouter:
    router = APIRouter(prefix=f"/{prefix}", tags=[prefix])

    def changed(session: Session) -> None:
        if on_change is not None:
            on_change(session)

    if by_category is None:

        @router.get("", response_model=list[schema_out], dependencies=READ)  # type: ignore[valid-type]
        def list_items(session: SessionDep) -> list[Out]:
            return [schema_out.model_validate(o) for o in crud.list_all(session, model)]

    else:
        lister = by_category

        @router.get("", response_model=list[schema_out], dependencies=READ)  # type: ignore[valid-type]
        def list_items_by_category(
            session: SessionDep,
            category: Annotated[
                str | None,
                Query(description='Category id, or "none" for templates without a category'),
            ] = None,
        ) -> list[Out]:
            items = lister(session, categories.parse(category))
            return [schema_out.model_validate(o) for o in items]

    @router.post(
        "", response_model=schema_out, status_code=status.HTTP_201_CREATED, dependencies=CONFIGURE
    )
    def create_item(body: schema_in, session: SessionDep, actor: ActorDep) -> Out:  # type: ignore[valid-type]
        obj = crud.create(session, model, body.model_dump(), actor=actor)  # type: ignore[attr-defined]
        changed(session)
        return schema_out.model_validate(obj)

    @router.get("/{obj_id}", response_model=schema_out, dependencies=READ)
    def get_item(obj_id: int, session: SessionDep) -> Out:
        return schema_out.model_validate(crud.get(session, model, obj_id))

    @router.put("/{obj_id}", response_model=schema_out, dependencies=CONFIGURE)
    def replace_item(
        obj_id: int,
        body: schema_in,  # type: ignore[valid-type]
        session: SessionDep,
        actor: ActorDep,
    ) -> Out:
        obj = crud.update(session, model, obj_id, body.model_dump(), actor=actor)  # type: ignore[attr-defined]
        changed(session)
        return schema_out.model_validate(obj)

    @router.delete("/{obj_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=CONFIGURE)
    def delete_item(obj_id: int, session: SessionDep, actor: ActorDep) -> None:
        if deleter is not None:
            deleter(session, obj_id, actor=actor)
        else:
            crud.delete(session, model, obj_id, actor=actor)
        changed(session)

    return router


routers = [
    crud_router("projects", Project, schemas.ProjectIn, schemas.ProjectOut),
    crud_router("inventories", Inventory, schemas.InventoryIn, schemas.InventoryOut),
    crud_router("credentials", Credential, schemas.CredentialIn, schemas.CredentialOut),
    crud_router("categories", Category, schemas.CategoryIn, schemas.CategoryOut),
    crud_router(
        "templates",
        Template,
        schemas.TemplateIn,
        schemas.TemplateOut,
        by_category=categories.list_templates,
        # Keeps the run history and deletes the template's schedules too.
        deleter=templates.delete,
    ),
    crud_router(
        "schedules",
        Schedule,
        schemas.ScheduleIn,
        schemas.ScheduleOut,
        on_change=schedules.notify_changed,
        by_category=categories.list_schedules,
    ),
]
