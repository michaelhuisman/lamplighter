"""Deleting a template keeps its run history and deletes its schedules."""

from collections.abc import Iterator

import httpx
import pytest
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models import AuditEntry
from tests.integration.conftest import API_URL, SECRETS_DIR, Env, LocalUser, login_ui, post

METRICS_AUTH = {"Authorization": f"Bearer {(SECRETS_DIR / 'metrics-token').read_text().strip()}"}


@pytest.fixture
def ui(admin: LocalUser) -> Iterator[httpx.Client]:
    client = login_ui(admin.username, admin.password)
    yield client
    client.close()


def test_delete_template_keeps_runs_and_removes_schedules(env: Env, ui: httpx.Client) -> None:
    template = env.template("ping.yml")
    run = env.wait(env.launch(template)["id"])
    schedule = post(
        env.api,
        "/schedules",
        {"template_id": template["id"], "cron": "0 0 1 1 *", "enabled": False},
    )

    listing = ui.get("/templates").text
    assert "Its 1 run(s) stay in the history. Its 1 schedule(s) will be deleted too." in listing

    resp = ui.post(f"/templates/{template['id']}/delete")
    assert resp.status_code == 200, resp.text
    assert env.api.get(f"/templates/{template['id']}").status_code == 404
    assert env.api.get(f"/schedules/{schedule['id']}").status_code == 404

    kept = env.get_run(run["id"])
    assert kept["template_id"] is None
    assert kept["template_name"] == template["name"]
    assert kept["status"] == "successful"

    page = ui.get(f"/runs/{run['id']}").text
    assert f"{template['name']} (deleted)" in page
    assert "it cannot be relaunched" in page
    assert ui.get("/runs").status_code == 200
    assert env.api.post(f"/runs/{run['id']}/relaunch").status_code == 409

    metrics = httpx.get(f"{API_URL}/metrics", headers=METRICS_AUTH, timeout=10).text
    assert f'lamplighter_runs_total{{status="successful",template="{template["name"]}"}}' in metrics

    with get_sessionmaker()() as s:
        entry = s.scalars(
            select(AuditEntry).where(
                AuditEntry.action == "templates.delete",
                AuditEntry.object_id == str(template["id"]),
            )
        ).one()
    assert entry.details == {"name": template["name"], "runs_kept": 1, "schedules_deleted": 1}


def test_api_delete_and_active_runs(env: Env) -> None:
    template = env.template("sleep.yml", extra_vars={"sleep_s": 30})
    run = env.launch(template)
    env.wait(run["id"], {"running"})
    refused = env.api.delete(f"/templates/{template['id']}")
    assert refused.status_code == 409
    assert "queued or running" in refused.text

    env.api.post(f"/runs/{run['id']}/cancel")
    env.wait(run["id"])
    assert env.api.delete(f"/templates/{template['id']}").status_code == 204
    assert env.get_run(run["id"])["template_name"] == template["name"]


def test_unused_template_is_simply_deleted(env: Env) -> None:
    template = env.template("ping.yml")
    assert env.api.delete(f"/templates/{template['id']}").status_code == 204
