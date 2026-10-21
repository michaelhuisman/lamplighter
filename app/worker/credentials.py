"""Just-in-time retrieval of secret values based on a credential reference."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from app.core.openbao import OpenBaoClient, OpenBaoError

DEFAULT_GIT_USERNAME = "x-access-token"


class CredentialError(Exception):
    pass


@dataclass(frozen=True)
class CredentialRef:
    id: int
    type: str
    openbao_path: str
    openbao_key: str


@dataclass(frozen=True)
class GitAuth:
    username: str
    token: str

    def __repr__(self) -> str:  # never the token in logs or tracebacks
        return f"GitAuth(username={self.username!r}, token='***')"


class CredentialResolver(Protocol):
    def fields(self, ref: CredentialRef) -> Mapping[str, str]:
        """All fields of the secret the credential refers to."""
        ...


def resolve(resolver: CredentialResolver, ref: CredentialRef) -> str:
    """The value under `openbao_key` (SSH key, vault password)."""
    values = resolver.fields(ref)
    if ref.openbao_key not in values:
        raise CredentialError(f"credential {ref.id}: key {ref.openbao_key!r} not found")
    return values[ref.openbao_key]


def resolve_git(resolver: CredentialResolver, ref: CredentialRef) -> GitAuth:
    """Token under `openbao_key`, username from key `username` (optional)."""
    values = resolver.fields(ref)
    if ref.openbao_key not in values:
        raise CredentialError(f"credential {ref.id}: key {ref.openbao_key!r} not found")
    return GitAuth(values.get("username") or DEFAULT_GIT_USERNAME, values[ref.openbao_key])


class OpenBaoResolver:
    def __init__(self, client: OpenBaoClient) -> None:
        self._client = client

    def fields(self, ref: CredentialRef) -> Mapping[str, str]:
        try:
            return self._client.read(ref.openbao_path)
        except OpenBaoError as exc:
            raise CredentialError(f"credential {ref.id}: {exc}") from exc


class RunTokenIssuer(Protocol):
    def create(self, run_id: int, ttl_s: int) -> str:
        """A short-lived token for one run."""
        ...

    def revoke(self, token: str) -> None: ...


class OpenBaoRunTokens:
    """Run tokens from an OpenBao token role."""

    def __init__(self, client: OpenBaoClient, role: str) -> None:
        self._client = client
        self._role = role

    def create(self, run_id: int, ttl_s: int) -> str:
        try:
            return self._client.create_run_token(role=self._role, ttl_s=ttl_s, run_id=run_id)
        except OpenBaoError as exc:
            raise CredentialError(f"run token: {exc}") from exc

    def revoke(self, token: str) -> None:
        try:
            self._client.revoke_token(token)
        except OpenBaoError as exc:
            raise CredentialError(str(exc)) from exc


class UnconfiguredResolver:
    def fields(self, ref: CredentialRef) -> Mapping[str, str]:
        raise CredentialError(f"credential {ref.id}: no credential backend configured")
