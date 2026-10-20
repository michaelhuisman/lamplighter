"""OpenBao client: AppRole login, token renewal and KV v2 reads.

Secret values are only returned, never logged. Error messages mention at most the
path and the key.
"""

import logging
import threading
import time
from collections.abc import Callable
from functools import lru_cache
from typing import Any

import hvac
import requests
from hvac import exceptions as hvac_exc

from app.core.config import Settings, get_settings

log = logging.getLogger(__name__)

# Renew once less than this fraction of the TTL remains.
RENEW_FRACTION = 1 / 3
TIMEOUT_S = 10


class OpenBaoError(Exception):
    """Secret could not be fetched. The message contains no secret values."""


class OpenBaoClient:
    def __init__(
        self,
        *,
        addr: str,
        role_id: str,
        secret_id: str,
        kv_mount: str = "secret",
        verify: bool | str = True,
        clock: Callable[[], float] = time.monotonic,
        client: Any = None,  # hvac.Client (untyped); injectable for tests
        token_client: Callable[[str], Any] | None = None,  # client for another token
    ) -> None:
        self._client = client or hvac.Client(url=addr, verify=verify, timeout=TIMEOUT_S)
        self._token_client = token_client or (
            lambda token: hvac.Client(url=addr, token=token, verify=verify, timeout=TIMEOUT_S)
        )
        self._role_id = role_id
        self._secret_id = secret_id
        self._mount = kv_mount
        self._clock = clock
        self._lock = threading.Lock()
        self._has_token = False
        self._ttl = 0.0
        self._expires_at = 0.0
        self._renewable = False

    @classmethod
    def from_settings(cls, settings: Settings) -> "OpenBaoClient | None":
        if (
            not settings.openbao_enabled
            or settings.openbao_addr is None
            or settings.openbao_role_id is None
            or settings.openbao_secret_id is None
        ):
            return None
        verify: bool | str = str(settings.openbao_ca_cert) if settings.openbao_ca_cert else True
        return cls(
            addr=settings.openbao_addr,
            role_id=settings.openbao_role_id,
            secret_id=settings.openbao_secret_id.get_secret_value(),
            kv_mount=settings.openbao_kv_mount,
            verify=verify,
        )

    # --- token lifecycle -------------------------------------------------------------

    def _apply_auth(self, auth: dict[str, Any]) -> None:
        self._ttl = float(auth.get("lease_duration") or 0)
        self._expires_at = self._clock() + self._ttl
        self._renewable = bool(auth.get("renewable"))
        self._has_token = True

    def _login(self) -> None:
        try:
            resp = self._client.auth.approle.login(role_id=self._role_id, secret_id=self._secret_id)
        except (hvac_exc.VaultError, requests.RequestException) as exc:
            self._has_token = False
            raise OpenBaoError(f"approle login failed: {type(exc).__name__}") from exc
        self._apply_auth(resp["auth"])
        log.info("openbao login", extra={"ttl_s": self._ttl, "renewable": self._renewable})

    def _renew(self) -> bool:
        remaining = self._expires_at - self._clock()
        try:
            resp = self._client.auth.token.renew_self()
        except (hvac_exc.VaultError, requests.RequestException):
            log.info("openbao token renew failed, logging in again")
            return False
        new_ttl = float(resp["auth"].get("lease_duration") or 0)
        if new_ttl <= remaining:
            # Max TTL reached: renewing gains nothing.
            return False
        self._apply_auth(resp["auth"])
        return True

    def _ensure_token(self) -> None:
        if not self._has_token:
            self._login()
            return
        remaining = self._expires_at - self._clock()
        if remaining > self._ttl * RENEW_FRACTION:
            return
        if remaining > 0 and self._renewable and self._renew():
            return
        self._login()

    # --- reading ---------------------------------------------------------------------

    def read(self, path: str) -> dict[str, str]:
        """All key/values of a KV v2 secret."""
        with self._lock:
            self._ensure_token()
            try:
                data = self._read_once(path)
            except hvac_exc.Forbidden:
                # Token revoked or expired outside our bookkeeping: retry once.
                self._login()
                try:
                    data = self._read_once(path)
                except hvac_exc.Forbidden as exc:
                    raise OpenBaoError(f"permission denied: {self._mount}/{path}") from exc
        return data

    def _read_once(self, path: str) -> dict[str, str]:
        try:
            resp = self._client.secrets.kv.v2.read_secret_version(
                path=path, mount_point=self._mount, raise_on_deleted_version=True
            )
        except hvac_exc.Forbidden:
            raise
        except hvac_exc.InvalidPath as exc:
            raise OpenBaoError(f"secret not found: {self._mount}/{path}") from exc
        except (hvac_exc.VaultError, requests.RequestException) as exc:
            raise OpenBaoError(f"cannot read {self._mount}/{path}: {type(exc).__name__}") from exc
        values = resp.get("data", {}).get("data") or {}
        return {str(k): str(v) for k, v in values.items()}

    # --- run tokens ------------------------------------------------------------------

    def create_run_token(self, *, role: str, ttl_s: int, run_id: int) -> str:
        """A short-lived, non-renewable token for one run, via a token role (orphan, so it
        survives a re-login of our own token). Policies come from the role."""
        with self._lock:
            self._ensure_token()
            try:
                resp = self._create_once(role, ttl_s, run_id)
            except hvac_exc.Forbidden:
                # Our own token revoked or expired outside our bookkeeping: retry once.
                self._login()
                try:
                    resp = self._create_once(role, ttl_s, run_id)
                except hvac_exc.Forbidden as exc:
                    raise OpenBaoError(f"permission denied: token role {role!r}") from exc
        token = (resp.get("auth") or {}).get("client_token")
        if not token:
            raise OpenBaoError(f"token role {role!r} returned no token")
        return str(token)

    def _create_once(self, role: str, ttl_s: int, run_id: int) -> dict[str, Any]:
        try:
            resp: dict[str, Any] = self._client.auth.token.create(
                role_name=role,
                ttl=f"{ttl_s}s",
                renewable=False,
                display_name=f"run-{run_id}",
                meta={"run_id": str(run_id)},
            )
        except hvac_exc.Forbidden:
            raise
        except (hvac_exc.VaultError, requests.RequestException) as exc:
            raise OpenBaoError(
                f"cannot create a token with role {role!r}: {type(exc).__name__}"
            ) from exc
        return resp

    def revoke_token(self, token: str) -> None:
        """Revoke a run token with the token itself (revoke-self)."""
        try:
            self._token_client(token).auth.token.revoke_self()
        except (hvac_exc.VaultError, requests.RequestException) as exc:
            raise OpenBaoError(f"cannot revoke run token: {type(exc).__name__}") from exc

    def read_key(self, path: str, key: str) -> str:
        data = self.read(path)
        if key not in data:
            raise OpenBaoError(f"key {key!r} not found in {self._mount}/{path}")
        return data[key]


@lru_cache
def get_openbao() -> OpenBaoClient | None:
    return OpenBaoClient.from_settings(get_settings())
