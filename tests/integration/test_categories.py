"""Template categories: management, and filtering templates, schedules and runs."""

from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from tests.integration.conftest import Env, LocalUser, login_ui, post, unique


@pytest.fixture
def ui(admin: LocalUser) -> Iterator[httpx.Client]:
    client = login_ui(admin.username, admin.password)
    yield client
    client.close()


def _category(env: Env, color: str = "green") -> dict[str, Any]:
    return post(env.api, "/categories", {"name": unique("Cat"), "color": color})


def _ids(items: list[dict[str, Any]]) -> set[int]:
    return {i["id"] for i in items}


def test_category_names_are_unique_regardless_of_case(env: Env) -> None:
    cat = _category(env)
    dup = env.api.post("/categories", json={"name": cat["name"].upper(), "color": "red"})
    assert dup.status_code == 409
    bad = env.api.post("/categories", json={"name": unique("x"), "color": "pink"})
    assert bad.status_code == 422


def test_filter_templates_schedules_and_runs(env: Env) -> None:
    cat = _category(env)
    other = _category(env)
    in_cat = env.template("ping.yml", category_id=cat["id"])
    in_other = env.template("ping.yml", category_id=other["id"])
    without = env.template("ping.yml")

    templates = env.api.get("/templates", params={"category": cat["id"]}).json()
    assert _ids(templates) == {in_cat["id"]}
    uncategorized = _ids(env.api.get("/templates", params={"category": "none"}).json())
    assert without["id"] in uncategorized
    assert in_cat["id"] not in uncategorized
    assert {in_cat["id"], in_other["id"], without["id"]} <= _ids(env.api.get("/templates").json())

    s1 = post(
        env.api, "/schedules", {"template_id": in_cat["id"], "cron": "0 0 1 1 *", "enabled": False}
    )
    s2 = post(
        env.api,
        "/schedules",
        {"template_id": in_other["id"], "cron": "0 0 1 1 *", "enabled": False},
    )
    schedules = _ids(env.api.get("/schedules", params={"category": cat["id"]}).json())
    assert s1["id"] in schedules
    assert s2["id"] not in schedules

    r1 = env.launch(in_cat)["id"]
    r2 = env.launch(in_other)["id"]
    runs = _ids(env.api.get("/runs", params={"category": cat["id"]}).json())
    assert r1 in runs
    assert r2 not in runs
    env.wait(r1)
    env.wait(r2)
    for s in (s1, s2):
        env.api.delete(f"/schedules/{s['id']}")


def test_a_category_in_use_cannot_be_deleted(env: Env) -> None:
    cat = _category(env)
    template = env.template("ping.yml", category_id=cat["id"])
    assert env.api.delete(f"/categories/{cat['id']}").status_code == 409
    env.api.put(
        f"/templates/{template['id']}",
        json={
            **{k: template[k] for k in template if k not in ("id", "created_at", "updated_at")},
            "category_id": None,
        },
    ).raise_for_status()
    assert env.api.delete(f"/categories/{cat['id']}").status_code == 204


def test_categories_in_the_ui(env: Env, ui: httpx.Client) -> None:
    cat = _category(env, color="purple")
    template = env.template("ping.yml", category_id=cat["id"])
    plain = env.template("ping.yml")

    page = ui.get("/categories").text
    assert cat["name"] in page
    assert "cat-purple" in page

    filtered = ui.get("/templates", params={"category": cat["id"]}).text
    assert template["name"] in filtered
    assert plain["name"] not in filtered
    assert f'<option value="{cat["id"]}" selected>' in filtered

    # The edit form shows the category; a copy keeps it.
    edit = ui.get(f"/templates/{template['id']}/edit").text
    assert f'<option value="{cat["id"]}" selected>' in edit
    copy = ui.get(f"/templates/{template['id']}/copy").text
    assert f'<option value="{cat["id"]}" selected>' in copy

    run = env.wait(env.launch(template)["id"])
    runs_page = ui.get("/runs", params={"category": cat["id"]}).text
    assert f'href="/ui/runs/{run["id"]}"' in runs_page
    assert ui.get("/schedules", params={"category": "none"}).status_code == 200
