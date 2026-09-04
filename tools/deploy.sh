#!/usr/bin/env bash
#
# Bring CS-OP up on the VM, or move it to a new image (CS-OP-RUN-002).
#
# Checks everything it needs BEFORE it stops what is running. A deployment
# that takes the service down and then discovers the certificate is missing
# has turned a five-minute upgrade into an outage.
#
#   ./tools/deploy.sh                     # start, or restart on the pinned digest
#   ./tools/deploy.sh sha256:abc123...    # move to a new digest
#   ./tools/deploy.sh --rollback          # back to the previous digest
set -euo pipefail

cd "$(dirname "$0")/.."
COMPOSE="docker compose"
IMAGE="ghcr.io/commsecurityau/cs-ops"
PREVIOUS=".deploy-previous-digest"

say() { printf '%s  %s\n' "$(date -Is)" "$*"; }
die() { printf '%s  FATAL: %s\n' "$(date -Is)" "$*" >&2; exit 1; }

# ---------------------------------------------------------------- preflight
[ -f docker-compose.yml ] || die "no docker-compose.yml here"

# The certificate. The app refuses to serve without it, so finding out now
# is the difference between a refused deploy and a stopped service.
for f in data/tls/server.crt data/tls/server.key; do
    [ -f "$f" ] || die "$f is missing. Issue one from the internal CA for
       ops.commsecurity.com.au, or the app will refuse to start."
done
if [ "$(stat -c '%a' data/tls/server.key)" != "600" ]; then
    die "data/tls/server.key must be 0600, not $(stat -c '%a' data/tls/server.key)"
fi

# The secret store. Absent, sign-in fails at the last step -- AFTER the
# deploy has appeared to succeed, which is the worst place to find out.
[ -f data/secrets/store.json ] || die "data/secrets/store.json is missing.
   See the header of docker-compose.yml for how to write it."
if [ "$(stat -c '%a' data/secrets/store.json)" != "600" ]; then
    die "data/secrets/store.json must be 0600"
fi
grep -q OIDC_CLIENT_SECRET data/secrets/store.json \
    || die "the secret store has no OIDC_CLIENT_SECRET"

# Certificate expiry. An expired certificate takes the platform down as
# surely as a crash, on a date nobody has in their calendar.
if ! openssl x509 -checkend 1209600 -noout -in data/tls/server.crt >/dev/null; then
    say "WARNING: the certificate expires within 14 days"
    openssl x509 -enddate -noout -in data/tls/server.crt
fi

# ------------------------------------------------------------------ target
current=$(grep -oP 'image:\s*\K\S+' docker-compose.yml)
if [ "${1:-}" = "--rollback" ]; then
    [ -f "$PREVIOUS" ] || die "no previous digest recorded"
    target="$IMAGE@$(cat "$PREVIOUS")"
    say "rolling back to $target"
elif [ -n "${1:-}" ]; then
    case "$1" in
        sha256:*) target="$IMAGE@$1" ;;
        *) die "expected a digest like sha256:abc..., got $1" ;;
    esac
else
    target="$current"
fi

# --------------------------------------------------------------------- pull
say "pulling $target"
docker pull "$target" >/dev/null || die "could not pull $target"

# Prove the image runs before swapping the live one onto it.
fingerprint=$(docker run --rm "$target" python3 -c \
    "import sys; sys.path.insert(0,'/app'); from ops.main import code_fingerprint; print(code_fingerprint())") \
    || die "the image will not start"
say "image fingerprint $fingerprint"

# --------------------------------------------------------------------- swap
if [ "$target" != "$current" ]; then
    echo "${current#*@}" > "$PREVIOUS"
    sed -i "s|image: .*|image: $target|" docker-compose.yml
    say "docker-compose.yml now pins $target (previous recorded for --rollback)"
fi

# A snapshot BEFORE the new code touches the database. Migrations are
# forward-only and nothing rolls the schema back for you, so rolling the
# image back does not undo a migration -- this file is what does.
if docker ps --format '{{.Names}}' | grep -qx cs-ops; then
    say "snapshotting before the swap"
    docker exec cs-ops python3 -c \
        "import sys; sys.path.insert(0,'/app');
from ops.db import Db
from ops import backup
db = Db('/data/ops.db', '/app/ops/migrations')
print(backup.snapshot(db, '/data/backups'))
db.close()" || say "WARNING: could not snapshot; the hourly one still applies"
fi

$COMPOSE up -d

# --------------------------------------------------------------------- prove
say "waiting for it to answer"
for i in $(seq 1 30); do
    if curl -ksf https://localhost:8443/healthz >/dev/null 2>&1; then
        say "healthy after ${i}s"
        docker logs cs-ops 2>&1 | head -1
        exit 0
    fi
    sleep 1
done

say "it did not become healthy in 30s. Logs:"
docker logs --tail 40 cs-ops
die "deployment failed. ./tools/deploy.sh --rollback returns to the previous digest."
