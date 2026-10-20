import socket
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LAMPLIGHTER_", extra="ignore")

    database_url: str
    runtime_dir: Path = Path("/run/lamplighter")
    repo_cache_dir: Path = Path("/var/cache/lamplighter/repos")
    # Collections from a project's collections/requirements.yml, cached per requirements.
    collections_cache_dir: Path = Path("/var/cache/lamplighter/collections")
    collections_install_timeout_s: int = Field(default=600, ge=10)
    # Cache entries unused for this many days are removed at worker start (0 = never).
    collections_cache_days: int = Field(default=30, ge=0)
    worker_id: str = Field(default_factory=socket.gethostname)
    poll_interval_s: float = 2.0
    log_level: str = "INFO"

    api_host: str = "0.0.0.0"  # noqa: S104 - listens inside the container
    api_port: int = 8000
    # X-Forwarded-For and X-Forwarded-Proto are only accepted from these proxies
    # (comma-separated IPs/CIDRs).
    trusted_proxies: str = "127.0.0.1"
    # Set: /metrics requires `Authorization: Bearer <token>`. Empty: /metrics is open.
    metrics_token: SecretStr | None = None

    # --- retention (days; 0 = never purge) ---
    retention_events_days: int = Field(default=30, ge=0)
    retention_runs_days: int = Field(default=180, ge=0)
    retention_audit_days: int = Field(default=365, ge=0)
    retention_tokens_days: int = Field(default=30, ge=0)
    retention_hour_utc: int = Field(default=3, ge=0, le=23)

    # --- reaper: runs in 'running' without a live worker ---
    reaper_grace_s: int = Field(default=120, ge=10)
    reaper_interval_s: int = Field(default=60, ge=5)

    ansible_host_key_checking: bool = True

    scheduler_lock_retry_s: float = 10.0
    # Maximum time between two reconciles of schedules -> jobs (NOTIFY is faster).
    scheduler_sync_interval_s: float = 5.0

    # Base URL for links in notifications, e.g. https://scheduler.example.org
    public_url: str = "http://localhost:8000"
    # Webhook URLs and the HMAC secret live in OpenBao (keys `urls` and `hmac_secret`).
    # Empty = no webhooks. Only the scheduler reads this path.
    webhook_openbao_path: str | None = None
    webhook_cache_s: float = 60.0

    # --- OpenBao (AppRole) ---
    openbao_addr: str | None = None
    openbao_role_id: str | None = None
    openbao_secret_id: SecretStr | None = None
    openbao_kv_mount: str = "secret"
    openbao_ca_cert: Path | None = None  # CA bundle for TLS; empty = system CAs
    # Token role for the per-run token that playbooks get as ANSIBLE_HASHI_VAULT_TOKEN
    # (community.hashi_vault). Empty = no run token.
    openbao_run_token_role: str | None = "lamplighter-run"  # noqa: S105 - a role name
    # TTL of the run token for templates without a timeout (otherwise: the timeout).
    openbao_run_token_ttl_s: int = Field(default=3600, ge=60)

    # --- auth ---
    auth_local_enabled: bool = True
    session_cookie_secure: bool = True  # only turn off in dev over http
    session_idle_s: int = 8 * 3600
    session_max_s: int = 24 * 3600
    login_max_failures: int = 5
    login_lockout_s: int = 15 * 60

    # OIDC (Keycloak). Empty = local users only.
    oidc_issuer: str | None = None  # as in the `iss` claim, e.g. https://kc/realms/x
    # Discovery via an internal address (e.g. http://keycloak:8080/...); default: from issuer.
    oidc_discovery_url: str | None = None
    oidc_client_id: str = "lamplighter"
    oidc_client_secret: SecretStr | None = None
    # Expected `aud` in access tokens; defaults to the client id.
    oidc_audience: str | None = None

    @property
    def openbao_enabled(self) -> bool:
        return bool(self.openbao_addr and self.openbao_role_id and self.openbao_secret_id)

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.oidc_issuer and self.oidc_client_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # database_url comes from the environment
