from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Entity, JsonDict, TimestampMixin

CREDENTIAL_TYPES = ("ssh_key", "vault_password", "git_token", "known_hosts")
INVENTORY_SOURCES = ("project_file", "inline")
OVERLAP_POLICIES = ("skip", "queue")
# Fixed palette for category badges (CSS classes cat-<colour>).
CATEGORY_COLORS = ("blue", "green", "teal", "purple", "orange", "red", "yellow", "gray")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Credential(Entity):
    """Only a reference to a secret, never the value itself."""

    __tablename__ = "credentials"
    __table_args__ = (CheckConstraint(_in("type", CREDENTIAL_TYPES), name="type"),)

    name: Mapped[str] = mapped_column(Text, unique=True)
    type: Mapped[str] = mapped_column(Text)
    openbao_path: Mapped[str] = mapped_column(Text)
    openbao_key: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Project(TimestampMixin, Entity):
    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(Text, unique=True)
    git_url: Mapped[str] = mapped_column(Text)
    branch: Mapped[str] = mapped_column(Text)
    credential_id: Mapped[int | None] = mapped_column(ForeignKey("credentials.id"))


class Inventory(TimestampMixin, Entity):
    __tablename__ = "inventories"
    __table_args__ = (
        CheckConstraint(_in("source_type", INVENTORY_SOURCES), name="source_type"),
        CheckConstraint(
            "(source_type = 'project_file' AND path IS NOT NULL AND project_id IS NOT NULL)"
            " OR (source_type = 'inline' AND content IS NOT NULL)",
            name="source_fields",
        ),
    )

    name: Mapped[str] = mapped_column(Text, unique=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"))
    source_type: Mapped[str] = mapped_column(Text)
    path: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)


class Category(TimestampMixin, Entity):
    """A label to group templates; runs and schedules follow their template's category."""

    __tablename__ = "categories"
    __table_args__ = (
        CheckConstraint(_in("color", CATEGORY_COLORS), name="color"),
        # Unique regardless of case: no "Linux" next to "linux".
        Index("uq_categories_name_lower", func.lower(text("name")), unique=True),
    )

    name: Mapped[str] = mapped_column(Text)
    color: Mapped[str] = mapped_column(Text, server_default="blue")


class Template(TimestampMixin, Entity):
    __tablename__ = "templates"
    __table_args__ = (
        CheckConstraint("verbosity BETWEEN 0 AND 5", name="verbosity"),
        CheckConstraint("timeout_s IS NULL OR timeout_s > 0", name="timeout_s"),
    )

    name: Mapped[str] = mapped_column(Text, unique=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    playbook_path: Mapped[str] = mapped_column(Text)
    inventory_id: Mapped[int] = mapped_column(ForeignKey("inventories.id"))
    extra_vars: Mapped[JsonDict] = mapped_column(server_default="{}")
    limit: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[str | None] = mapped_column(Text)
    skip_tags: Mapped[str | None] = mapped_column(Text)
    verbosity: Mapped[int] = mapped_column(server_default="0")
    machine_credential_id: Mapped[int] = mapped_column(ForeignKey("credentials.id"))
    vault_credential_id: Mapped[int | None] = mapped_column(ForeignKey("credentials.id"))
    # Set: strict host key checking with these known_hosts, regardless of the global setting.
    known_hosts_credential_id: Mapped[int | None] = mapped_column(ForeignKey("credentials.id"))
    timeout_s: Mapped[int | None]
    # A category in use cannot be deleted (no ON DELETE).
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), index=True)


class Schedule(TimestampMixin, Entity):
    __tablename__ = "schedules"
    __table_args__ = (
        CheckConstraint(_in("overlap_policy", OVERLAP_POLICIES), name="overlap_policy"),
        CheckConstraint("misfire_grace_s > 0", name="misfire_grace_s"),
    )

    template_id: Mapped[int] = mapped_column(ForeignKey("templates.id"))
    cron: Mapped[str] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(server_default="true")
    overlap_policy: Mapped[str] = mapped_column(Text, server_default="skip")
    misfire_grace_s: Mapped[int] = mapped_column(server_default="60")
    extra_vars_override: Mapped[JsonDict] = mapped_column(server_default="{}")
