import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from hvac import exceptions as hvac_exc

from app.core.openbao import OpenBaoClient, OpenBaoError
from app.worker.credentials import GitAuth
from app.worker.git import askpass_config


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class Hvac:
    """The part of hvac.Client that OpenBaoClient uses."""

    def __init__(self) -> None:
        self.logins = 0
        self.renews = 0
        self.login_ttl = 60
        self.renew_ttl: int | None = 60
        self.renewable = True
        self.login_error: Exception | None = None
        self.forbidden_reads = 0
        self.data: dict[str, dict[str, Any]] = {"ssh/a": {"id_ed25519": "KEY", "n": 1}}
        self.created: list[dict[str, Any]] = []
        self.forbidden_creates = 0
        self.create_error: Exception | None = None
        self.auth = SimpleNamespace(
            approle=SimpleNamespace(login=self._login),
            token=SimpleNamespace(renew_self=self._renew, create=self._create),
        )
        self.secrets = SimpleNamespace(
            kv=SimpleNamespace(v2=SimpleNamespace(read_secret_version=self._read))
        )

    def _login(self, role_id: str, secret_id: str) -> dict[str, Any]:
        if self.login_error:
            raise self.login_error
        self.logins += 1
        return {"auth": {"lease_duration": self.login_ttl, "renewable": self.renewable}}

    def _renew(self) -> dict[str, Any]:
        if self.renew_ttl is None:
            raise hvac_exc.Forbidden("renew denied")
        self.renews += 1
        return {"auth": {"lease_duration": self.renew_ttl, "renewable": True}}

    def _create(self, **kwargs: Any) -> dict[str, Any]:
        if self.forbidden_creates:
            self.forbidden_creates -= 1
            raise hvac_exc.Forbidden("permission denied")
        if self.create_error:
            raise self.create_error
        self.created.append(kwargs)
        return {"auth": {"client_token": "s.CHILD-TOKEN", "lease_duration": kwargs["ttl"]}}

    def _read(self, path: str, mount_point: str, raise_on_deleted_version: bool) -> dict[str, Any]:
        if self.forbidden_reads:
            self.forbidden_reads -= 1
            raise hvac_exc.Forbidden("permission denied")
        if path not in self.data:
            raise hvac_exc.InvalidPath("not found")
        return {"data": {"data": self.data[path]}}


@pytest.fixture
def fake() -> Hvac:
    return Hvac()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def make(fake: Hvac, clock: FakeClock) -> OpenBaoClient:
    return OpenBaoClient(
        addr="http://x", role_id="r", secret_id="SECRET-ID-XYZ", clock=clock, client=fake
    )


def test_first_read_logs_in(fake: Hvac, clock: FakeClock) -> None:
    client = make(fake, clock)
    assert client.read("ssh/a") == {"id_ed25519": "KEY", "n": "1"}
    assert client.read_key("ssh/a", "id_ed25519") == "KEY"
    assert fake.logins == 1


def test_no_renew_while_plenty_of_ttl(fake: Hvac, clock: FakeClock) -> None:
    client = make(fake, clock)
    client.read("ssh/a")
    clock.t += 30  # 30 of 60s left (> 1/3)
    client.read("ssh/a")
    assert (fake.logins, fake.renews) == (1, 0)


def test_renews_when_ttl_runs_low(fake: Hvac, clock: FakeClock) -> None:
    client = make(fake, clock)
    client.read("ssh/a")
    clock.t += 45  # 15s over (< 1/3)
    client.read("ssh/a")
    assert (fake.logins, fake.renews) == (1, 1)


def test_relogin_when_max_ttl_reached(fake: Hvac, clock: FakeClock) -> None:
    client = make(fake, clock)
    client.read("ssh/a")
    clock.t += 50  # 10s left
    fake.renew_ttl = 5  # max TTL: renew yields less than what is left
    client.read("ssh/a")
    assert fake.logins == 2


def test_relogin_when_renew_fails(fake: Hvac, clock: FakeClock) -> None:
    client = make(fake, clock)
    client.read("ssh/a")
    clock.t += 50
    fake.renew_ttl = None
    client.read("ssh/a")
    assert fake.logins == 2


def test_relogin_when_expired_or_not_renewable(fake: Hvac, clock: FakeClock) -> None:
    fake.renewable = False
    client = make(fake, clock)
    client.read("ssh/a")
    clock.t += 50
    client.read("ssh/a")
    clock.t += 500
    client.read("ssh/a")
    assert (fake.logins, fake.renews) == (3, 0)


def test_forbidden_read_retries_once_after_login(fake: Hvac, clock: FakeClock) -> None:
    client = make(fake, clock)
    client.read("ssh/a")
    fake.forbidden_reads = 1  # e.g. token revoked
    assert client.read("ssh/a")["id_ed25519"] == "KEY"
    assert fake.logins == 2
    fake.forbidden_reads = 2
    with pytest.raises(OpenBaoError, match="permission denied"):
        client.read("ssh/a")


def test_missing_secret_and_key(fake: Hvac, clock: FakeClock) -> None:
    client = make(fake, clock)
    with pytest.raises(OpenBaoError, match="secret not found: secret/ssh/nope"):
        client.read("ssh/nope")
    with pytest.raises(OpenBaoError, match="key 'other' not found"):
        client.read_key("ssh/a", "other")


@pytest.mark.parametrize(
    "error", [hvac_exc.InvalidRequest("bad secret_id"), requests.ConnectionError("down")]
)
def test_login_failure_is_openbao_error(fake: Hvac, clock: FakeClock, error: Exception) -> None:
    fake.login_error = error
    with pytest.raises(OpenBaoError, match="approle login failed") as exc:
        make(fake, clock).read("ssh/a")
    assert "SECRET-ID-XYZ" not in str(exc.value)


def test_create_run_token(fake: Hvac, clock: FakeClock) -> None:
    token = make(fake, clock).create_run_token(role="lamplighter-run", ttl_s=300, run_id=7)
    assert token == "s.CHILD-TOKEN"
    assert fake.logins == 1
    assert fake.created == [
        {
            "role_name": "lamplighter-run",
            "ttl": "300s",
            "renewable": False,
            "display_name": "run-7",
            "meta": {"run_id": "7"},
        }
    ]


def test_create_run_token_retries_once_after_login(fake: Hvac, clock: FakeClock) -> None:
    client = make(fake, clock)
    fake.forbidden_creates = 1
    assert client.create_run_token(role="r", ttl_s=60, run_id=1) == "s.CHILD-TOKEN"
    assert fake.logins == 2
    fake.forbidden_creates = 2
    with pytest.raises(OpenBaoError, match="permission denied: token role 'r'"):
        client.create_run_token(role="r", ttl_s=60, run_id=1)


def test_create_run_token_error(fake: Hvac, clock: FakeClock) -> None:
    fake.create_error = hvac_exc.InvalidRequest("unknown role")
    with pytest.raises(OpenBaoError, match="cannot create a token with role 'r': InvalidRequest"):
        make(fake, clock).create_run_token(role="r", ttl_s=60, run_id=1)


def test_revoke_token_uses_the_token_itself(fake: Hvac, clock: FakeClock) -> None:
    used: list[str] = []

    def token_client(token: str) -> SimpleNamespace:
        used.append(token)
        return SimpleNamespace(
            auth=SimpleNamespace(token=SimpleNamespace(revoke_self=lambda: None))
        )

    client = OpenBaoClient(
        addr="http://x",
        role_id="r",
        secret_id="s",
        clock=clock,
        client=fake,
        token_client=token_client,
    )
    client.revoke_token("s.CHILD-TOKEN")
    assert used == ["s.CHILD-TOKEN"]
    assert fake.logins == 0  # our own token is not needed


def test_revoke_token_error_has_no_token(fake: Hvac, clock: FakeClock) -> None:
    def revoke_self() -> None:
        raise requests.ConnectionError("s.CHILD-TOKEN unreachable")

    client = OpenBaoClient(
        addr="http://x",
        role_id="r",
        secret_id="s",
        clock=clock,
        client=fake,
        token_client=lambda _t: SimpleNamespace(
            auth=SimpleNamespace(token=SimpleNamespace(revoke_self=revoke_self))
        ),
    )
    with pytest.raises(OpenBaoError) as exc_info:
        client.revoke_token("s.CHILD-TOKEN")
    assert "s.CHILD-TOKEN" not in str(exc_info.value)


def test_askpass_script(tmp_path: Path) -> None:
    auth_dir = tmp_path / "git-auth"
    opts = askpass_config(GitAuth("deploy", "tok'en $x"), auth_dir)
    assert opts[0] == "-c"
    script = Path(opts[1].split("=", 1)[1])
    assert opts[2:] == ["-c", "credential.helper="]
    assert stat.S_IMODE(auth_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((auth_dir / "token").stat().st_mode) == 0o600
    assert "tok'en" not in script.read_text()  # the token is not in the script itself
    assert os.access(script, os.X_OK)

    def ask(prompt: str) -> str:
        return subprocess.run(
            [str(script), prompt], capture_output=True, text=True, check=True
        ).stdout

    assert ask("Username for 'http://git-http:8080': ") == "deploy"
    assert ask("Password for 'http://deploy@git-http:8080': ") == "tok'en $x"
