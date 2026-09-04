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

# The generated .env. Compose fills every ${...} in docker-compose.yml from
# it, so without it `up` fails naming the first missing key -- but that is
# after the running container has been stopped, which is what we are here
# to avoid. Read values with grep, never `source`: it is data, not a script.
[ -f .env ] || die ".env is missing. Raven-Fleet writes it from the release JSON."
if [ "$(stat -c '%a' .env)" != "600" ]; then
    die ".env must be 0600, not $(stat -c '%a' .env): it carries the OIDC secret"
fi
envval() { grep -m1 "^$1=" .env | cut -d= -f2- | tr -d '"' ; }

# The secret. Either in .env (the release carries it) or in the store on
# the volume (the app falls back to secret://). Absent from both, sign-in
# fails at the last step -- AFTER the deploy has appeared to succeed.
if [ -n "$(envval OIDC_CLIENT_SECRET)" ]; then
    say "OIDC secret: from .env"
else
    [ -f data/secrets/store.json ] || die "OIDC_CLIENT_SECRET is not in .env and
   data/secrets/store.json is missing. Put it in the release JSON."
    [ "$(stat -c '%a' data/secrets/store.json)" = "600" ] \
        || die "data/secrets/store.json must be 0600"
    grep -q OIDC_CLIENT_SECRET data/secrets/store.json \
        || die "the secret store has no OIDC_CLIENT_SECRET"
    say "OIDC secret: from the store on the volume"
fi

# The certificate. The app refuses to serve without it, so finding out now
# is the difference between a refused deploy and a stopped service. Either
# delivered in .env (base64 PEM, written to data/tls/ by the app at boot)
# or already on the volume; the same checks run on whichever it is.
tlsdir=$(mktemp -d); chmod 700 "$tlsdir"; trap 'rm -rf "$tlsdir"' EXIT
if [ -n "$(envval OPS_TLS_CERT)" ] || [ -n "$(envval OPS_TLS_KEY)" ]; then
    [ -n "$(envval OPS_TLS_CERT)" ] && [ -n "$(envval OPS_TLS_KEY)" ] \
        || die "OPS_TLS_CERT and OPS_TLS_KEY must both be set, or neither"
    envval OPS_TLS_CERT | base64 -d > "$tlsdir/server.crt" 2>/dev/null \
        || die "OPS_TLS_CERT is not valid base64 (base64 -w0 server.crt)"
    envval OPS_TLS_KEY | base64 -d > "$tlsdir/server.key" 2>/dev/null \
        || die "OPS_TLS_KEY is not valid base64 (base64 -w0 server.key)"
    crt="$tlsdir/server.crt"; key="$tlsdir/server.key"
    say "TLS material: from .env"
else
    for f in data/tls/server.crt data/tls/server.key; do
        [ -f "$f" ] || die "$f is missing and OPS_TLS_CERT/OPS_TLS_KEY are not
       in .env. Issue a pair from the internal CA for ops.commsecurity.com.au."
    done
    [ "$(stat -c '%a' data/tls/server.key)" = "600" ] \
        || die "data/tls/server.key must be 0600, not $(stat -c '%a' data/tls/server.key)"
    crt=data/tls/server.crt; key=data/tls/server.key
    say "TLS material: from the volume"
fi

# The pair must go together: two separate issuances is the likely mistake.
# An unreadable file yields an empty key, and two empties compare equal,
# so each side has to be non-empty before they are compared.
crt_pub=$(openssl x509 -in "$crt" -noout -pubkey 2>/dev/null) \
    && [ -n "$crt_pub" ] || die "$crt is not a readable PEM certificate"
key_pub=$(openssl pkey -in "$key" -pubout 2>/dev/null) \
    && [ -n "$key_pub" ] || die "$key is not a readable PEM private key"
[ "$crt_pub" = "$key_pub" ] \
    || die "the TLS certificate and key do not match; reissue both as a pair"

# Certificate expiry. An expired certificate takes the platform down as
# surely as a crash, on a date nobody has in their calendar.
if ! openssl x509 -checkend 1209600 -noout -in "$crt" >/dev/null; then
    say "WARNING: the certificate expires within 14 days"
    openssl x509 -enddate -noout -in "$crt"
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
    # Only a digest can be rolled back to. A tag in the file (the fleet
    # manager pins that itself) is not a place to return to, so do not
    # record one as if it were.
    case "$current" in
        *@sha256:*) echo "${current#*@}" > "$PREVIOUS"
                    note="previous recorded for --rollback" ;;
        *)          note="previous was a tag, nothing recorded for --rollback" ;;
    esac
    sed -i "s|image: .*|image: $target|" docker-compose.yml
    say "docker-compose.yml now pins $target ($note)"
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
