#!/bin/sh
# Sets up the dev OpenBao (in-memory; runs again on every start of the stack):
# AppRoles with least-privilege policies and the dev secrets from .dev/secrets.
set -eu
export BAO_ADDR=http://openbao:8200   # BAO_TOKEN (root, dev only) comes from init.env

until bao status >/dev/null 2>&1; do sleep 1; done

bao auth list -format=json | grep -q '"approle/"' || bao auth enable approle >/dev/null
# KV mount for secrets that playbooks read themselves (community.hashi_vault).
bao secrets list -format=json | grep -q '"ansible/"' || bao secrets enable -path=ansible kv-v2 >/dev/null

bao policy write lamplighter-worker - >/dev/null <<'POLICY'
path "secret/data/ssh/*"   { capabilities = ["read"] }
path "secret/data/vault/*" { capabilities = ["read"] }
path "secret/data/git/*"   { capabilities = ["read"] }
path "auth/token/create/lamplighter-run" { capabilities = ["update"] }
POLICY
bao policy write lamplighter-scheduler - >/dev/null <<'POLICY'
path "secret/data/webhooks/*" { capabilities = ["read"] }
POLICY
bao policy write ansible-read - >/dev/null <<'POLICY'
path "ansible/data/*"     { capabilities = ["read"] }
path "ansible/metadata/*" { capabilities = ["read", "list"] }
POLICY

# Per-run tokens for playbooks: orphans (they survive a re-login of the worker's own
# token), only ansible-read (+ default, for revoke-self), not renewable.
bao write auth/token/roles/lamplighter-run allowed_policies=ansible-read orphan=true \
    renewable=false token_explicit_max_ttl=24h >/dev/null

# Short TTLs in dev, so token renewal and re-login run during normal use.
role() {
    bao write "auth/approle/role/$1" token_policies="$1" token_ttl=60s token_max_ttl=300s \
        secret_id_ttl=0 secret_id_num_uses=0 >/dev/null
    bao write "auth/approle/role/$1/role-id" role_id="$2" >/dev/null
    bao write "auth/approle/role/$1/custom-secret-id" secret_id="$3" >/dev/null 2>&1 || true
}
role lamplighter-worker "$WORKER_ROLE_ID" "$WORKER_SECRET_ID"
role lamplighter-scheduler "$SCHEDULER_ROLE_ID" "$SCHEDULER_SECRET_ID"

bao kv put -mount=secret ssh/ssh-target id_ed25519=@/secrets/ssh-target/id_ed25519 >/dev/null
bao kv put -mount=secret ssh/ssh-target-known-hosts known_hosts=@/secrets/ssh-target/known_hosts >/dev/null
bao kv put -mount=secret vault/dev password=@/secrets/vault/password >/dev/null
bao kv put -mount=secret git/fixtures token=@/secrets/git/token username=scheduler >/dev/null
bao kv put -mount=secret webhooks/default urls='["http://webhook-sink:8080/hook"]' \
    hmac_secret=@/secrets/webhook/hmac >/dev/null
bao kv put -mount=ansible dev/lamplighter value=@/secrets/ansible/value >/dev/null
echo "openbao initialised"
