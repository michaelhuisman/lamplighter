#!/usr/bin/env bash
# Secret scan of the container logs (runs on the host; the dev container has no access
# to the container logs). Searches the logs of the app containers for every dev secret in
# .dev/secrets. The OpenBao dev server itself is excluded: it prints its root token.
#
# Run after the integration tests, so the logs contain runs that used all these secrets.
set -euo pipefail
cd "$(dirname "$0")/.."

. scripts/lib.sh
SERVICES="api worker scheduler migrate"
dir=.dev/secrets

needles=()
add() { [ -n "$1" ] && needles+=("$1"); }
add "$(cat "$dir/vault/password")"
add "$(cat "$dir/git/token")"
add "$(cat "$dir/webhook/hmac")"
add "$(cat "$dir/ansible/value")"
add "$(cat "$dir/keycloak/client-secret")"
for f in "$dir"/openbao/*-secret-id "$dir"/keycloak/kc-*; do add "$(cat "$f")"; done
# SSH key: every content line separately (part of a key is a leak too).
while IFS= read -r line; do
    case "$line" in -----*|"") ;; *) add "$line" ;; esac
done <"$dir/ssh-target/id_ed25519"

logs=$($COMPOSE logs --no-color $SERVICES 2>/dev/null)
lines=$(printf '%s\n' "$logs" | wc -l | tr -d ' ')
found=0
for needle in "${needles[@]}"; do
    if printf '%s' "$logs" | grep -qF -- "$needle"; then
        echo "FAIL: secret found in the logs (starts with ${needle:0:4}…)" >&2
        found=1
    fi
done
[ "$found" = 0 ] || exit 1
echo "OK: searched ${#needles[@]} secrets in $lines log lines of: $SERVICES; nothing found"
