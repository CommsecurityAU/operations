#!/usr/bin/env bash
#
# The internal CA for CS-OP, and the server certificate it issues
# (CS-OP-RUN-002 §2b, [SITE-CA]).
#
#   ./tools/issue_cert.sh                 # CA in ~/cs-ops-ca, cert for ops.commsecurity.com.au
#   ./tools/issue_cert.sh ~/cs-ops-ca ops.commsecurity.com.au 172.16.1.20
#
# WHERE: on your own machine, never on the VM. The CA key signs everything
# staff browsers will trust for this host; the VM only ever sees the server
# pair, delivered in the release. Keep the CA directory in the company
# password manager (it is small) and nowhere else.
#
# WHEN: once for the CA (ten years). Once per server certificate, before
# creating the release that will carry it -- the release JSON needs the two
# base64 values this prints. Again before the certificate expires: the app
# warns at boot and hourly from 30 days out, and a new release with new
# values is the whole renewal.
#
# THE ROOT must reach every staff browser or sign-in shows a certificate
# warning: push ca.crt through Workspace device management (Devices ->
# Networks -> Certificates), or install it by hand on each machine.
#
# A LEAF OVER 825 DAYS is rejected by Apple platforms even from a trusted
# private root, so the default is under that.
set -euo pipefail

CA_DIR="${1:-$HOME/cs-ops-ca}"
HOST="${2:-ops.commsecurity.com.au}"
IP="${3:-}"                 # optional: also valid when reached by IP
LEAF_DAYS="${LEAF_DAYS:-730}"
CA_DAYS="${CA_DAYS:-3650}"

say() { printf '%s\n' "$*" >&2; }
die() { printf 'FATAL: %s\n' "$*" >&2; exit 1; }
command -v openssl >/dev/null || die "openssl is not installed"

umask 077
mkdir -p "$CA_DIR"
cd "$CA_DIR"

# ------------------------------------------------------------------- the CA
if [ ! -f ca.key ]; then
    say "creating the CA in $CA_DIR (valid $CA_DAYS days)"
    openssl genrsa -out ca.key 4096 2>/dev/null
    openssl req -x509 -new -key ca.key -sha256 -days "$CA_DAYS" -out ca.crt \
        -subj "/O=COMMSecurity Pty Ltd/CN=COMMSecurity Internal CA" \
        -addext "basicConstraints=critical,CA:TRUE" \
        -addext "keyUsage=critical,keyCertSign,cRLSign"
    say "CA root written to $CA_DIR/ca.crt -- this is what staff machines must trust"
else
    say "using the existing CA in $CA_DIR"
fi
[ -f ca.crt ] || die "$CA_DIR has ca.key but no ca.crt"

# ------------------------------------------------------------ the server pair
# Each issuance gets its own directory, so a renewal never overwrites the
# pair that is still in production until you choose to switch.
OUT="issued/$HOST-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT"
SAN="DNS:$HOST"
[ -n "$IP" ] && SAN="$SAN,IP:$IP"

openssl genrsa -out "$OUT/server.key" 2048 2>/dev/null
openssl req -new -key "$OUT/server.key" -sha256 -out "$OUT/server.csr" \
    -subj "/O=COMMSecurity Pty Ltd/CN=$HOST"

# Serial from the clock, not a counter file: two people issuing on two
# machines from the same CA must not collide.
cat > "$OUT/ext.cnf" <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=$SAN
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
EOF
openssl x509 -req -in "$OUT/server.csr" -CA ca.crt -CAkey ca.key \
    -set_serial "0x$(openssl rand -hex 16)" -days "$LEAF_DAYS" -sha256 \
    -extfile "$OUT/ext.cnf" -out "$OUT/server.crt" 2>/dev/null
rm -f "$OUT/server.csr" "$OUT/ext.cnf"
chmod 600 "$OUT/server.key"

# Prove the pair before anyone puts it in a release.
openssl verify -CAfile ca.crt "$OUT/server.crt" >/dev/null \
    || die "the issued certificate does not verify against the CA"
[ "$(openssl x509 -in "$OUT/server.crt" -noout -pubkey)" = \
  "$(openssl pkey -in "$OUT/server.key" -pubout)" ] \
    || die "certificate and key do not match"

say ""
say "issued $CA_DIR/$OUT/server.crt for $SAN, valid $LEAF_DAYS days:"
openssl x509 -in "$OUT/server.crt" -noout -enddate >&2
say ""
say "Put these two values in the release's environment JSON. Each is ONE line."
say ""
printf 'OPS_TLS_CERT=%s\n' "$(base64 -w0 < "$OUT/server.crt")"
printf 'OPS_TLS_KEY=%s\n' "$(base64 -w0 < "$OUT/server.key")"
say ""
say "Then distribute $CA_DIR/ca.crt to staff machines (Workspace device management)."
