#!/usr/bin/env sh
# Generates dev secrets in .dev/ (listed in .gitignore). Existing values are kept.
#   .dev/secrets/ssh-target/id_ed25519   SSH key for the ssh-target container
#   .dev/secrets/vault/password          vault password for tests
#   .dev/secrets/keycloak/*              client secret, admin and test passwords
#   .dev/secrets/oidc.env                LAMPLIGHTER_OIDC_CLIENT_SECRET for the api
#   .dev/secrets/keycloak.env            bootstrap admin for the Keycloak console
#   .dev/keycloak/realm-lamplighter.json   realm import, rendered from dev/keycloak/*.tpl
#   .dev/secrets/openbao/*               root token (init + tests only), AppRole ids
#   .dev/secrets/git/token               token for the git-http server
#   .dev/secrets/webhook/hmac            HMAC secret for webhooks
#   .dev/secrets/ansible/value           secret that playbooks read with community.hashi_vault
#   .dev/secrets/metrics.env             LAMPLIGHTER_METRICS_TOKEN for /metrics
#   .dev/secrets/ssh-target/host_*       fixed host key of ssh-target + known_hosts
set -eu
cd "$(dirname "$0")/.."
dir=.dev/secrets
mkdir -p "$dir/ssh-target" "$dir/vault" "$dir/keycloak" "$dir/openbao" "$dir/git" \
    "$dir/webhook" "$dir/ansible" .dev/keycloak

rand() { LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "${1:-32}"; }
secret() {  # secret <file>: create it if it does not exist yet, then read it
    [ -f "$1" ] || rand 32 >"$1"
    cat "$1"
}

if [ ! -f "$dir/ssh-target/id_ed25519" ]; then
    ssh-keygen -q -t ed25519 -N "" -C "lamplighter-dev" -f "$dir/ssh-target/id_ed25519"
fi
# Fixed host key for ssh-target and the matching known_hosts line.
if [ ! -f "$dir/ssh-target/host_ed25519_key" ]; then
    ssh-keygen -q -t ed25519 -N "" -C "ssh-target" -f "$dir/ssh-target/host_ed25519_key"
fi
printf 'ssh-target %s\n' "$(cut -d' ' -f1,2 "$dir/ssh-target/host_ed25519_key.pub")" \
    >"$dir/ssh-target/known_hosts"
secret "$dir/vault/password" >/dev/null

client_secret=$(secret "$dir/keycloak/client-secret")
kc_admin=$(secret "$dir/keycloak/console-admin")
pw_viewer=$(secret "$dir/keycloak/kc-viewer")
pw_operator=$(secret "$dir/keycloak/kc-operator")
pw_admin=$(secret "$dir/keycloak/kc-admin")
pw_norole=$(secret "$dir/keycloak/kc-norole")

sed -e "s/__CLIENT_SECRET__/$client_secret/" \
    -e "s/__PW_VIEWER__/$pw_viewer/" -e "s/__PW_OPERATOR__/$pw_operator/" \
    -e "s/__PW_ADMIN__/$pw_admin/" -e "s/__PW_NOROLE__/$pw_norole/" \
    dev/keycloak/realm-lamplighter.json.tpl >.dev/keycloak/realm-lamplighter.json
printf 'LAMPLIGHTER_OIDC_CLIENT_SECRET=%s\n' "$client_secret" >"$dir/oidc.env"
printf 'KC_BOOTSTRAP_ADMIN_USERNAME=admin\nKC_BOOTSTRAP_ADMIN_PASSWORD=%s\n' "$kc_admin" >"$dir/keycloak.env"

secret "$dir/git/token" >/dev/null
printf 'LAMPLIGHTER_METRICS_TOKEN=%s\n' "$(secret "$dir/metrics-token")" >"$dir/metrics.env"
secret "$dir/webhook/hmac" >/dev/null
secret "$dir/ansible/value" >/dev/null
root_token=$(secret "$dir/openbao/root-token")
worker_role=$(secret "$dir/openbao/worker-role-id")
worker_secret=$(secret "$dir/openbao/worker-secret-id")
sched_role=$(secret "$dir/openbao/scheduler-role-id")
sched_secret=$(secret "$dir/openbao/scheduler-secret-id")
printf 'BAO_DEV_ROOT_TOKEN_ID=%s\n' "$root_token" >"$dir/openbao/server.env"
printf 'BAO_TOKEN=%s\nWORKER_ROLE_ID=%s\nWORKER_SECRET_ID=%s\nSCHEDULER_ROLE_ID=%s\nSCHEDULER_SECRET_ID=%s\n' \
    "$root_token" "$worker_role" "$worker_secret" "$sched_role" "$sched_secret" >"$dir/openbao/init.env"
printf 'LAMPLIGHTER_OPENBAO_ROLE_ID=%s\nLAMPLIGHTER_OPENBAO_SECRET_ID=%s\n' "$worker_role" "$worker_secret" \
    >"$dir/openbao/worker.env"
printf 'LAMPLIGHTER_OPENBAO_ROLE_ID=%s\nLAMPLIGHTER_OPENBAO_SECRET_ID=%s\n' "$sched_role" "$sched_secret" \
    >"$dir/openbao/scheduler.env"

# The containers run under other uids; these are dev secrets only.
chmod -R a+rX .dev
echo "dev secrets in $dir"
