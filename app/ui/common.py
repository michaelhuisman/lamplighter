"""Shared parts of the UI: Jinja environment, rendering and form helpers."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ValidationError

from app.api.deps import require
from app.core.auth import CSRF_FIELD, Action, Principal, client_ip
from app.core.config import get_settings
from app.models.run import FINAL_STATUSES
from app.services.audit import Actor
from app.services.runs import template_label

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=TEMPLATES_DIR)
templates.env.globals["Action"] = Action
templates.env.globals["FINAL_STATUSES"] = {s.value for s in FINAL_STATUSES}
templates.env.globals["CSRF_FIELD"] = CSRF_FIELD


def _fmt_dt(value: datetime | None) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S") if value else ""


def _fmt_duration(start: datetime | None, end: datetime | None) -> str:
    if start is None:
        return ""
    total = int(((end or datetime.now(UTC)) - start) / timedelta(seconds=1))
    minutes, seconds = divmod(max(total, 0), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m{seconds:02d}s"


@lru_cache(maxsize=64)
def _static_version(path: str) -> str:
    file = (STATIC_DIR / path).resolve()
    if not file.is_relative_to(STATIC_DIR.resolve()) or not file.is_file():
        return "0"
    return hashlib.sha256(file.read_bytes()).hexdigest()[:10]


def static(path: str) -> str:
    """URL of a static file with a content version, so browsers don't keep loading from
    their cache after an update."""
    return f"/ui/static/{path}?v={_static_version(path)}"


templates.env.filters["dt"] = _fmt_dt
templates.env.globals["static"] = static
templates.env.globals["duration"] = _fmt_duration
templates.env.globals["template_label"] = template_label

CanLaunch = Annotated[Principal, Depends(require(Action.LAUNCH))]
CanCancel = Annotated[Principal, Depends(require(Action.CANCEL))]
CanConfigure = Annotated[Principal, Depends(require(Action.CONFIGURE))]
CanRead = Annotated[Principal, Depends(require(Action.READ))]
CanManageUsers = Annotated[Principal, Depends(require(Action.MANAGE_USERS))]

# Forms send everything as strings; empty fields become None.
FormData = dict[str, Any]


def render(
    request: Request, name: str, user: Principal | None, code: int = 200, **ctx: Any
) -> HTMLResponse:
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        name,
        {
            "user": user,
            "now": datetime.now(UTC),
            "local_login": settings.auth_local_enabled,
            "oidc_login": settings.oidc_enabled,
            **ctx,
        },
        status_code=code,
    )


def redirect(url: str) -> Response:
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


def actor(request: Request, user: Principal) -> Actor:
    return Actor(user.triggered_by, client_ip(request))


def clean(form: FormData) -> FormData:
    return {
        k: (v.strip() if isinstance(v, str) else v) or None
        for k, v in form.items()
        if k != CSRF_FIELD
    }


def parse_json(value: str | None, field: str, errors: dict[str, str]) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        errors[field] = f"invalid JSON: {exc.msg}"
        return {}
    if not isinstance(parsed, dict):
        errors[field] = "must be a JSON object"
        return {}
    return parsed


def validate[S: BaseModel](schema: type[S], data: FormData, errors: dict[str, str]) -> S | None:
    try:
        return schema.model_validate(data)
    except ValidationError as exc:
        for err in exc.errors():
            field = str(err["loc"][0]) if err["loc"] else "__all__"
            errors.setdefault(field, err["msg"].removeprefix("Value error, "))
        return None
