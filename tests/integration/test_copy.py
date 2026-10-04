"""Copying templates and schedules in the UI: a prefilled "new" form, nothing is created
until it is saved."""

import json
import re
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models import AuditEntry
from tests.integration.conftest import Env, LocalUser, ensure_local_user, login_ui, post, unique


@pytest.fixture
def ui(admin: LocalUser) -> Iterator[httpx.Client]:
    client = login_ui(admin.username, admin.password)
    yield client
    client.close()


def _input(page: str, name: str) -> str | None:
    match = re.search(rf'name="{name}"[^>]*?value="([^"]*)"', page)
    return match.group(1) if match else None


def _audit_details(action: str, object_id: int) -> dict[str, Any]:
    with get_sessionmaker()() as s:
        entry = s.scalars(
            select(AuditEntry).where(
                AuditEntry.action == action, AuditEntry.object_id == str(object_id)
            )
        ).one()
    return dict(entry.details)


def _template_form(t: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    form = {
        "name": t["name"],
        "project_id": t["project_id"],
        "playbook_path": t["playbook_path"],
        "inventory_id": t["inventory_id"],
        "machine_credential_id": t["machine_credential_id"],
        "extra_vars": json.dumps(t["extra_vars"]),
        "limit": t["limit"] or "",
        "verbosity": t["verbosity"],
    }
    form.update(overrides)
    return form


def test_copy_template(env: Env, ui: httpx.Client) -> None:
    original = env.template("ping.yml", extra_vars={"color": "blue"}, limit="ssh-target")
    page = ui.get(f"/templates/{original['id']}/copy")
    assert page.status_code == 200
    assert f"Copy of {original['name']}" in page.text
    assert _input(page.text, "name") == f"{original['name']} (copy)"
    assert _input(page.text, "playbook_path") == "ping.yml"
    assert _input(page.text, "limit") == "ssh-target"
    assert "&#34;color&#34;" in page.text or '"color"' in page.text
    # Only the form: nothing is created by opening it.
    names = [t["name"] for t in env.api.get("/templates").json()]
    assert f"{original['name']} (copy)" not in names

    saved = ui.post(
        "/templates",
        data=_template_form(
            original, name=f"{original['name']} (copy)", copied_from=original["id"]
        ),
    )
    assert saved.status_code == 303, saved.text[:300]
    copies = [
        t for t in env.api.get("/templates").json() if t["name"] == f"{original['name']} (copy)"
    ]
    assert len(copies) == 1
    copy = copies[0]
    assert copy["id"] != original["id"]
    assert (copy["extra_vars"], copy["limit"]) == ({"color": "blue"}, "ssh-target")
    assert _audit_details("templates.create", copy["id"]) == {
        "name": copy["name"],
        "copied_from": original["id"],
    }
    # The next copy gets a free name.
    again = ui.get(f"/templates/{original['id']}/copy")
    assert _input(again.text, "name") == f"{original['name']} (copy 2)"


def test_copy_schedule_keeps_the_enabled_state(env: Env, ui: httpx.Client) -> None:
    template = env.template("ping.yml")
    original = post(
        env.api,
        "/schedules",
        {
            "template_id": template["id"],
            "cron": "15 3 * * 1",
            "timezone": "Europe/Amsterdam",
            "enabled": True,
            "overlap_policy": "queue",
        },
    )
    page = ui.get(f"/schedules/{original['id']}/copy")
    assert page.status_code == 200
    assert f"Copy of schedule {original['id']}" in page.text
    assert _input(page.text, "cron") == "15 3 * * 1"
    assert re.search(r'name="enabled"\s+checked', page.text)

    saved = ui.post(
        "/schedules",
        data={
            "template_id": template["id"],
            "cron": "15 3 * * 1",
            "timezone": "Europe/Amsterdam",
            "enabled": "on",
            "overlap_policy": "queue",
            "misfire_grace_s": 60,
            "extra_vars_override": "",
            "copied_from": original["id"],
        },
    )
    assert saved.status_code == 303, saved.text[:300]
    mine = [s for s in env.api.get("/schedules").json() if s["template_id"] == template["id"]]
    assert len(mine) == 2
    copy = next(s for s in mine if s["id"] != original["id"])
    assert (copy["cron"], copy["timezone"], copy["enabled"], copy["overlap_policy"]) == (
        "15 3 * * 1",
        "Europe/Amsterdam",
        True,
        "queue",
    )
    assert _audit_details("schedules.create", copy["id"])["copied_from"] == original["id"]
    env.api.delete(f"/schedules/{copy['id']}")
    env.api.delete(f"/schedules/{original['id']}")


def test_copy_needs_configure_rights(env: Env) -> None:
    template = env.template("ping.yml")
    viewer = ensure_local_user(unique("viewer"), ["operator"])
    ui = login_ui(viewer.username, viewer.password)
    assert ui.get(f"/templates/{template['id']}/copy").status_code == 403
    listing = ui.get("/templates").text
    assert f"/templates/{template['id']}/copy" not in listing
