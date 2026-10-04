from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    computed_field,
    model_validator,
)

from app.models.config import CATEGORY_COLORS
from app.models.run import RunStatus
from app.scheduler.trigger import InvalidScheduleError, build_trigger, next_fire_time


def _relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ValueError("must be a relative path without '..'")
    return value


RelPath = Annotated[str, AfterValidator(_relative_path)]
Name = Annotated[str, Field(min_length=1, max_length=200)]
NonEmpty = Annotated[str, Field(min_length=1)]
# Extra vars are free-form JSON for Ansible.
ExtraVars = dict[str, Any]


class OrmModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- credentials -----------------------------------------------------------


class CredentialIn(BaseModel):
    name: Name
    type: Literal["ssh_key", "vault_password", "git_token", "known_hosts"]
    openbao_path: NonEmpty
    openbao_key: NonEmpty


class CredentialOut(CredentialIn, OrmModel):
    id: int
    created_at: datetime


# --- categories ------------------------------------------------------------


class CategoryIn(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
    color: Literal[CATEGORY_COLORS] = "blue"  # type: ignore[valid-type]


class CategoryOut(CategoryIn, OrmModel):
    id: int
    created_at: datetime
    updated_at: datetime


# --- projects --------------------------------------------------------------


class ProjectIn(BaseModel):
    name: Name
    git_url: NonEmpty
    branch: NonEmpty = "main"
    credential_id: int | None = None


class ProjectOut(ProjectIn, OrmModel):
    id: int
    created_at: datetime
    updated_at: datetime


# --- inventories -----------------------------------------------------------


class InventoryIn(BaseModel):
    name: Name
    project_id: int | None = None
    source_type: Literal["project_file", "inline"]
    path: RelPath | None = None
    content: str | None = None

    @model_validator(mode="after")
    def _check_source(self) -> Self:
        if self.source_type == "project_file":
            if self.path is None or self.project_id is None:
                raise ValueError("project_file requires path and project_id")
            if self.content is not None:
                raise ValueError("project_file does not take content")
        else:
            if self.content is None:
                raise ValueError("inline requires content")
            if self.path is not None:
                raise ValueError("inline does not take path")
        return self


class InventoryOut(InventoryIn, OrmModel):
    id: int
    created_at: datetime
    updated_at: datetime


# --- templates -------------------------------------------------------------


class TemplateIn(BaseModel):
    name: Name
    project_id: int
    playbook_path: RelPath
    inventory_id: int
    extra_vars: ExtraVars = Field(default_factory=dict)
    limit: str | None = None
    tags: str | None = None
    skip_tags: str | None = None
    verbosity: int = Field(default=0, ge=0, le=5)
    machine_credential_id: int
    vault_credential_id: int | None = None
    known_hosts_credential_id: int | None = None
    category_id: int | None = None
    timeout_s: int | None = Field(default=None, gt=0)


class TemplateOut(TemplateIn, OrmModel):
    id: int
    created_at: datetime
    updated_at: datetime


# --- schedules -------------------------------------------------------------


class ScheduleIn(BaseModel):
    template_id: int
    cron: str = Field(description="5 fields: minute hour day month weekday")
    timezone: str = "UTC"
    enabled: bool = True
    overlap_policy: Literal["skip", "queue"] = "skip"
    misfire_grace_s: int = Field(default=60, gt=0)
    extra_vars_override: ExtraVars = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_cron(self) -> Self:
        try:
            build_trigger(self.cron, self.timezone)
        except InvalidScheduleError as exc:
            raise ValueError(str(exc)) from exc
        return self


class ScheduleOut(ScheduleIn, OrmModel):
    id: int
    created_at: datetime
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def next_run_at(self) -> datetime | None:
        if not self.enabled:
            return None
        return next_fire_time(build_trigger(self.cron, self.timezone), datetime.now(UTC))


# --- runs ------------------------------------------------------------------


class LaunchIn(BaseModel):
    extra_vars: ExtraVars = Field(default_factory=dict)
    limit: str | None = None


class RelaunchIn(BaseModel):
    # Only the hosts that failed or were unreachable in the original run.
    failed_hosts_only: bool = False


class RunOut(OrmModel):
    id: int
    # None once the template is deleted; template_name is then the name the run kept.
    template_id: int | None
    template_name: str | None
    schedule_id: int | None
    scheduled_for: datetime | None
    relaunch_of: int | None
    overlap_policy: Literal["skip", "queue"]
    triggered_by: str
    status: RunStatus
    extra_vars: ExtraVars
    limit: str | None
    commit_sha: str | None
    worker_id: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    cancel_requested_at: datetime | None
    rc: int | None
    status_reason: str | None
    stats: dict[str, Any] | None


class RunEventOut(OrmModel):
    seq: int
    event: str
    host: str | None
    task: str | None
    created_at: datetime
    stdout: str | None
    data: dict[str, Any]


class RunEventsPage(BaseModel):
    items: list[RunEventOut]
    next_after_seq: int | None


# --- auth ------------------------------------------------------------------

Role = Literal["viewer", "operator", "admin"]


class MeOut(BaseModel):
    subject: str
    display_name: str | None
    source: str
    roles: list[str]
    via: str


class UserOut(OrmModel):
    id: int
    source: Literal["local", "oidc"]
    username: str
    display_name: str | None
    email: str | None
    roles: list[str]
    disabled: bool
    created_at: datetime
    last_login_at: datetime | None


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=12, max_length=1024)
    roles: list[Role] = Field(default_factory=list)
    display_name: str | None = None


class UserUpdate(BaseModel):
    roles: list[Role] | None = None
    disabled: bool | None = None
    display_name: str | None = None


class PasswordIn(BaseModel):
    password: str = Field(min_length=12, max_length=1024)


class TokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    expires_days: int | None = Field(default=90, ge=1, le=365)


class TokenOut(OrmModel):
    id: int
    name: str
    prefix: str
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None


class TokenCreated(TokenOut):
    token: str = Field(description="Only shown now")


class AuditOut(OrmModel):
    id: int
    at: datetime
    actor: str
    action: str
    object_type: str | None
    object_id: str | None
    details: dict[str, Any]
    ip: str | None
