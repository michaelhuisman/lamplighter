"""Server-rendered UI (Jinja2 + htmx) under /ui.

Thin like the API routers: business logic in app/services, validation via the same
pydantic schemas as the API.
"""

import html
import json
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, Response

from app.api.deps import SessionDep
from app.api.schemas import LaunchIn, ScheduleIn, TemplateIn
from app.core.auth import Principal
from app.models import Credential, Inventory, Project, Schedule, Template
from app.models.run import RunStatus
from app.scheduler.trigger import build_trigger, next_fire_time
from app.services import crud, dashboard, runs, schedules
from app.services.errors import ServiceError
from app.ui.common import (
    CanCancel,
    CanConfigure,
    CanLaunch,
    CanRead,
    FormData,
    actor,
    clean,
    parse_json,
    redirect,
    render,
    validate,
)

router = APIRouter(prefix="/ui", include_in_schema=False)


# --- runs ------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
def index() -> Response:
    return redirect("/ui/dashboard")


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request, session: SessionDep, user: CanRead) -> HTMLResponse:
    data = dashboard.build(session)
    # htmx refreshes only the panels.
    partial = request.headers.get("HX-Request") == "true"
    return render(request, "_dashboard.html" if partial else "dashboard.html", user, d=data)


@router.get("/runs", response_class=HTMLResponse)
def runs_page(
    request: Request,
    session: SessionDep,
    user: CanRead,
    template_id: Annotated[str | None, Query()] = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> HTMLResponse:
    tid = int(template_id) if template_id and template_id.isdigit() else None
    st = RunStatus(status_filter) if status_filter in set(RunStatus) else None
    rows = runs.list_runs(session, runs.RunFilter(template_id=tid, status=st, limit=100))
    names = {t.id: t.name for t in crud.list_all(session, Template)}
    ctx: dict[str, Any] = {"runs": rows, "template_names": names, "template_id": tid, "status": st}
    # htmx only refreshes the table.
    partial = request.headers.get("HX-Request") == "true"
    return render(request, "_runs_table.html" if partial else "runs.html", user, **ctx)


def _run_context(session: SessionDep, run_id: int) -> dict[str, Any]:
    run = runs.get(session, run_id)
    template = crud.get(session, Template, run.template_id)
    schedule = session.get(Schedule, run.schedule_id) if run.schedule_id else None
    return {
        "run": run,
        "template": template,
        "schedule": schedule,
        "failed_hosts": runs.failed_hosts(run),
    }


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: int, session: SessionDep, user: CanRead) -> HTMLResponse:
    return render(request, "run_detail.html", user, **_run_context(session, run_id))


@router.get("/runs/{run_id}/meta", response_class=HTMLResponse)
def run_meta(request: Request, run_id: int, session: SessionDep, user: CanRead) -> HTMLResponse:
    return render(request, "_run_meta.html", user, **_run_context(session, run_id))


@router.post("/runs/{run_id}/cancel", response_class=HTMLResponse)
def run_cancel(request: Request, run_id: int, session: SessionDep, user: CanCancel) -> HTMLResponse:
    error = None
    try:
        runs.cancel(session, run_id, actor=actor(request, user))
    except ServiceError as exc:
        error = str(exc)
    ctx = _run_context(session, run_id)
    return render(request, "_run_meta.html", user, error=error, **ctx)


@router.post("/runs/{run_id}/relaunch", response_class=HTMLResponse)
async def run_relaunch(
    request: Request, run_id: int, session: SessionDep, user: CanLaunch
) -> Response:
    form = await request.form()
    try:
        new = runs.relaunch(
            session,
            run_id,
            triggered_by=user.triggered_by,
            failed_hosts_only=form.get("failed_hosts_only") == "1",
            actor=actor(request, user),
        )
    except ServiceError as exc:
        ctx = _run_context(session, run_id)
        return render(request, "run_detail.html", user, code=409, error=str(exc), **ctx)
    return redirect(f"/ui/runs/{new.id}")


# --- templates -------------------------------------------------------------


def _template_choices(session: SessionDep) -> dict[str, Any]:
    creds = crud.list_all(session, Credential)
    return {
        "projects": crud.list_all(session, Project),
        "inventories": crud.list_all(session, Inventory),
        "ssh_credentials": [c for c in creds if c.type == "ssh_key"],
        "vault_credentials": [c for c in creds if c.type == "vault_password"],
        "known_hosts_credentials": [c for c in creds if c.type == "known_hosts"],
    }


@router.get("/templates", response_class=HTMLResponse)
def templates_page(request: Request, session: SessionDep, user: CanRead) -> HTMLResponse:
    items = crud.list_all(session, Template)
    projects = {p.id: p.name for p in crud.list_all(session, Project)}
    return render(request, "templates.html", user, templates=items, projects=projects)


@router.get("/templates/new", response_class=HTMLResponse)
def template_new(request: Request, session: SessionDep, user: CanConfigure) -> HTMLResponse:
    return render(
        request,
        "template_form.html",
        user,
        item=None,
        form={},
        errors={},
        **_template_choices(session),
    )


@router.get("/templates/{template_id}/edit", response_class=HTMLResponse)
def template_edit(
    request: Request, template_id: int, session: SessionDep, user: CanConfigure
) -> HTMLResponse:
    item = crud.get(session, Template, template_id)
    form = TemplateIn.model_validate(item, from_attributes=True).model_dump()
    form["extra_vars"] = json.dumps(form["extra_vars"], indent=2) if form["extra_vars"] else ""
    return render(
        request,
        "template_form.html",
        user,
        item=item,
        form=form,
        errors={},
        **_template_choices(session),
    )


def _save_template(
    request: Request, session: SessionDep, user: Principal, form: FormData, template_id: int | None
) -> Response:
    errors: dict[str, str] = {}
    data = clean(form)
    data["extra_vars"] = parse_json(data.get("extra_vars"), "extra_vars", errors)
    data["verbosity"] = data.get("verbosity") or 0
    parsed = validate(TemplateIn, data, errors)
    if parsed is not None and not errors:
        try:
            if template_id is None:
                crud.create(session, Template, parsed.model_dump(), actor=actor(request, user))
            else:
                crud.update(
                    session, Template, template_id, parsed.model_dump(), actor=actor(request, user)
                )
        except ServiceError as exc:
            errors["__all__"] = str(exc)
        else:
            return redirect("/ui/templates")
    item = crud.get(session, Template, template_id) if template_id else None
    return render(
        request,
        "template_form.html",
        user,
        code=422,
        item=item,
        form=form,
        errors=errors,
        **_template_choices(session),
    )


@router.post("/templates", response_class=HTMLResponse)
async def template_create(request: Request, session: SessionDep, user: CanConfigure) -> Response:
    form = dict(await request.form())
    return _save_template(request, session, user, form, None)


@router.post("/templates/{template_id}", response_class=HTMLResponse)
async def template_update(
    request: Request, template_id: int, session: SessionDep, user: CanConfigure
) -> Response:
    form = dict(await request.form())
    return _save_template(request, session, user, form, template_id)


@router.post("/templates/{template_id}/delete", response_class=HTMLResponse)
def template_delete(
    request: Request, template_id: int, session: SessionDep, user: CanConfigure
) -> Response:
    try:
        crud.delete(session, Template, template_id, actor=actor(request, user))
    except ServiceError as exc:
        return HTMLResponse(f'<span class="error">{html.escape(str(exc))}</span>', status_code=409)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.get("/templates/{template_id}/launch", response_class=HTMLResponse)
def template_launch_form(
    request: Request, template_id: int, session: SessionDep, user: CanLaunch
) -> HTMLResponse:
    item = crud.get(session, Template, template_id)
    return render(request, "launch_form.html", user, item=item, form={}, errors={})


@router.post("/templates/{template_id}/launch", response_class=HTMLResponse)
def template_launch(
    request: Request,
    template_id: int,
    session: SessionDep,
    user: CanLaunch,
    extra_vars: Annotated[str, Form()] = "",
    limit: Annotated[str, Form()] = "",
) -> Response:
    errors: dict[str, str] = {}
    parsed_vars = parse_json(extra_vars.strip(), "extra_vars", errors)
    body = validate(LaunchIn, {"extra_vars": parsed_vars, "limit": limit.strip() or None}, errors)
    if body is None or errors:
        item = crud.get(session, Template, template_id)
        form = {"extra_vars": extra_vars, "limit": limit}
        return render(
            request, "launch_form.html", user, code=422, item=item, form=form, errors=errors
        )
    run = runs.launch(
        session,
        template_id,
        triggered_by=user.triggered_by,
        extra_vars=body.extra_vars,
        limit=body.limit,
        actor=actor(request, user),
    )
    return redirect(f"/ui/runs/{run.id}")


# --- schedules -------------------------------------------------------------


def _next_run(schedule: Schedule) -> datetime | None:
    if not schedule.enabled:
        return None
    return next_fire_time(build_trigger(schedule.cron, schedule.timezone), datetime.now(UTC))


@router.get("/schedules", response_class=HTMLResponse)
def schedules_page(request: Request, session: SessionDep, user: CanRead) -> HTMLResponse:
    items = crud.list_all(session, Schedule)
    names = {t.id: t.name for t in crud.list_all(session, Template)}
    next_runs = {s.id: _next_run(s) for s in items}
    return render(
        request, "schedules.html", user, schedules=items, template_names=names, next_runs=next_runs
    )


@router.get("/schedules/new", response_class=HTMLResponse)
def schedule_new(request: Request, session: SessionDep, user: CanConfigure) -> HTMLResponse:
    defaults = ScheduleIn(template_id=0, cron="0 * * * *").model_dump()
    defaults["template_id"] = None
    defaults["extra_vars_override"] = ""
    return render(
        request,
        "schedule_form.html",
        user,
        item=None,
        form=defaults,
        errors={},
        templates=crud.list_all(session, Template),
    )


@router.get("/schedules/{schedule_id}/edit", response_class=HTMLResponse)
def schedule_edit(
    request: Request, schedule_id: int, session: SessionDep, user: CanConfigure
) -> HTMLResponse:
    item = crud.get(session, Schedule, schedule_id)
    form = ScheduleIn.model_validate(item, from_attributes=True).model_dump()
    override = form["extra_vars_override"]
    form["extra_vars_override"] = json.dumps(override, indent=2) if override else ""
    return render(
        request,
        "schedule_form.html",
        user,
        item=item,
        form=form,
        errors={},
        templates=crud.list_all(session, Template),
    )


def _save_schedule(
    request: Request, session: SessionDep, user: Principal, form: FormData, schedule_id: int | None
) -> Response:
    errors: dict[str, str] = {}
    data = clean(form)
    data["enabled"] = form.get("enabled") == "on"
    data["extra_vars_override"] = parse_json(
        data.get("extra_vars_override"), "extra_vars_override", errors
    )
    data["misfire_grace_s"] = data.get("misfire_grace_s") or 60
    data["timezone"] = data.get("timezone") or "UTC"
    parsed = validate(ScheduleIn, data, errors)
    if parsed is not None and not errors:
        try:
            if schedule_id is None:
                crud.create(session, Schedule, parsed.model_dump(), actor=actor(request, user))
            else:
                crud.update(
                    session, Schedule, schedule_id, parsed.model_dump(), actor=actor(request, user)
                )
            schedules.notify_changed(session)
        except ServiceError as exc:
            errors["__all__"] = str(exc)
        else:
            return redirect("/ui/schedules")
    item = crud.get(session, Schedule, schedule_id) if schedule_id else None
    return render(
        request,
        "schedule_form.html",
        user,
        code=422,
        item=item,
        form=form,
        errors=errors,
        templates=crud.list_all(session, Template),
    )


@router.post("/schedules", response_class=HTMLResponse)
async def schedule_create(request: Request, session: SessionDep, user: CanConfigure) -> Response:
    form = dict(await request.form())
    return _save_schedule(request, session, user, form, None)


@router.post("/schedules/{schedule_id}", response_class=HTMLResponse)
async def schedule_update(
    request: Request, schedule_id: int, session: SessionDep, user: CanConfigure
) -> Response:
    form = dict(await request.form())
    return _save_schedule(request, session, user, form, schedule_id)


@router.post("/schedules/{schedule_id}/toggle", response_class=HTMLResponse)
def schedule_toggle(
    request: Request, schedule_id: int, session: SessionDep, user: CanConfigure
) -> HTMLResponse:
    item = crud.get(session, Schedule, schedule_id)
    item = crud.update(
        session, Schedule, schedule_id, {"enabled": not item.enabled}, actor=actor(request, user)
    )
    schedules.notify_changed(session)
    names = {t.id: t.name for t in crud.list_all(session, Template)}
    return render(
        request,
        "_schedule_row.html",
        user,
        s=item,
        template_names=names,
        next_runs={item.id: _next_run(item)},
    )


@router.post("/schedules/{schedule_id}/delete", response_class=HTMLResponse)
def schedule_delete(
    request: Request, schedule_id: int, session: SessionDep, user: CanConfigure
) -> HTMLResponse:
    crud.delete(session, Schedule, schedule_id, actor=actor(request, user))
    schedules.notify_changed(session)
    return HTMLResponse("")
