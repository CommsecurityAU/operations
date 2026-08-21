# syntax=docker/dockerfile:1
#
# ONE image, ZERO pip packages, ONE /data volume (CS-OP-ARCH-002 §2, §13).
#
# The base MUST be digest-pinned. Everything downstream of this build is
# digest-pinned and signed by the fleet manager -- "a release means exactly
# those bytes forever" -- but an unpinned base makes that true only AFTER
# the build, so two CI runs of the same commit can ship different bytes and,
# specifically, different SQLite versions. STRICT tables need >= 3.37.
#
# Refresh the digest with:
#   docker pull python:3.12-alpine
#   docker inspect --format='{{index .RepoDigests 0}}' python:3.12-alpine
#
# A bump is an ordinary PR: new digest, full suite, merged like anything
# else. A pin nobody moves is an unpatched base.
#
# Pinned 21 Aug 2026. Bumping is an ordinary PR: new digest, full suite.
FROM python:3.12-alpine@sha256:d09d15e60962ca365d1cd544a48773bac9d33f2fb1b00f2aa0deec78ade7dc31

# ca-certificates is not decoration. auth.py accepts an UNSIGNED id_token
# payload on the strength of TLS alone (OIDC Core §3.1.3.7), so certificate
# validation is the entire trust boundary for sign-in.
RUN apk add --no-cache ca-certificates && update-ca-certificates

# Fail the BUILD, not the first migration, if the base ships an old SQLite.
RUN python3 -c "import sqlite3,sys; \
    v=tuple(int(p) for p in sqlite3.sqlite_version.split('.')); \
    sys.exit(0) if v>=(3,37,0) else sys.exit('SQLite %s < 3.37: STRICT tables unavailable' % sqlite3.sqlite_version)"

WORKDIR /app
COPY ops/ /app/ops/

# Non-root. The volume is chowned so the app can write its own store, key
# and snapshots without ever running as root.
RUN addgroup -S ops && adduser -S -G ops ops \
    && mkdir -p /data && chown -R ops:ops /data /app
USER ops

VOLUME ["/data"]
EXPOSE 8443
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 OPS_DATA=/data

# No shell form: PID 1 is python itself, so it receives SIGTERM directly and
# the container stops in milliseconds rather than waiting out the 10 s kill
# timeout on every deploy.
CMD ["python3", "-m", "ops.main"]
