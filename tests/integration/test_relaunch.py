"""Relaunch of a finished run, via the API and the UI."""

from collections.abc import Iterator

import httpx
import pytest
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models import AuditEntry
from tests.integration.conftest import Env, LocalUser, ensure_local_user, login_ui, unique


@pytest.fixture
def ui(admin: LocalUser) -> Iterator[httpx.Client]:
    client = login_ui(admin.username, admin.password)
    yield client
    client.close()


def test_relaunch_reuses_the_effective_parameters(env: Env) -> None:
    template = env.template("ping.yml")
    original = env.wait(
        env.launch(template, extra_vars={"color": "blue"}, limit="ssh-target")["id"]
    )
    assert original["status"] == "successful"

    resp = env.api.post(f"/runs/{original['id']}/relaunch")
    assert resp.status_code == 201, resp.text
    new = resp.json()
    assert new["relaunch_of"] == original["id"]
    assert new["template_id"] == template["id"]
    assert new["extra_vars"] == original["extra_vars"]
    assert new["limit"] == "ssh-target"
    assert new["triggered_by"].startswith("user:")
    assert env.wait(new["id"])["status"] == "successful"

    with get_sessionmaker()() as s:
        entry = s.scalars(
            select(AuditEntry).where(
                AuditEntry.action == "run.relaunch", AuditEntry.object_id == str(new["id"])
            )
        ).one()
    assert entry.details["relaunched_from"] == original["id"]


def test_relaunch_failed_hosts_only(env: Env) -> None:
    failed = env.wait(env.launch(env.template("fail.yml"))["id"])
    assert failed["status"] == "failed"
    assert failed["stats"]["failed"] == {"ssh-target": 1}

    resp = env.api.post(f"/runs/{failed['id']}/relaunch", json={"failed_hosts_only": True})
    assert resp.status_code == 201, resp.text
    assert resp.json()["limit"] == "ssh-target"

    ok = env.wait(env.launch(env.template("ping.yml"))["id"])
    refused = env.api.post(f"/runs/{ok['id']}/relaunch", json={"failed_hosts_only": True})
    assert refused.status_code == 409
    assert "no failed or unreachable hosts" in refused.text


def test_active_runs_cannot_be_relaunched(env: Env) -> None:
    run = env.launch(env.template("sleep.yml", extra_vars={"sleep_s": 30}))
    env.wait(run["id"], {"running"})
    resp = env.api.post(f"/runs/{run['id']}/relaunch")
    assert resp.status_code == 409
    env.api.post(f"/runs/{run['id']}/cancel")
    env.wait(run["id"])


def test_relaunch_needs_launch_rights(env: Env) -> None:
    run = env.wait(env.launch(env.template("ping.yml"))["id"])
    viewer = ensure_local_user(unique("viewer"), ["viewer"])
    resp = httpx.post(
        f"{str(env.api.base_url).rstrip('/')}/runs/{run['id']}/relaunch",
        headers=viewer.headers,
        timeout=10,
    )
    assert resp.status_code == 403
    page = login_ui(viewer.username, viewer.password).get(f"/runs/{run['id']}").text
    assert "Relaunch" not in page


def test_relaunch_buttons_in_the_ui(env: Env, ui: httpx.Client) -> None:
    failed = env.wait(env.launch(env.template("fail.yml"))["id"])
    page = ui.get(f"/runs/{failed['id']}").text
    assert f'action="/ui/runs/{failed["id"]}/relaunch"' in page
    assert "Relaunch failed hosts (1)" in page

    resp = ui.post(f"/runs/{failed['id']}/relaunch", data={"failed_hosts_only": "1"})
    assert resp.status_code == 303
    new_id = int(resp.headers["location"].rsplit("/", 1)[1])
    new_page = ui.get(f"/runs/{new_id}").text
    assert "Relaunch of" in new_page
    assert f'href="/ui/runs/{failed["id"]}"' in new_page
    env.wait(new_id)
