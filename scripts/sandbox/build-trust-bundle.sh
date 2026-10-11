#!/usr/bin/env bash
# Build CERTS_DIR/trust.pem = CERTS_DIR/ca.pem + CERTS_DIR/real-ca.pem.
#
# Clients inside the sandbox trust trust.pem: the sandbox CA (for MITM-ed
# fixture hosts) plus the real system roots (for hosts the proxy tunnels). uv
# treats SSL_CERT_FILE as its ONLY trust source, so pointing it at ca.pem alone
# made every tunneled host fail with UnknownIssuer. ca.pem stays the proxy's
# signing CA and real-ca.pem its upstream trust store; nothing here relaxes
# verification. dev-sandbox.sh runs this every start so a regenerated ca.pem is
# never stale. Written to a temp file and renamed, so a failed build never
# leaves a truncated bundle behind.
#
# Usage: build-trust-bundle.sh CERTS_DIR

set -euo pipefail

certs_dir="${1:?usage: build-trust-bundle.sh CERTS_DIR}"
for input in ca.pem real-ca.pem; do
  if [ ! -s "$certs_dir/$input" ]; then
    echo "error: cannot build the sandbox trust bundle: $certs_dir/$input is missing or empty" >&2
    exit 1
  fi
done
trust_tmp="$certs_dir/trust.pem.tmp"
# Each input on its own line: a PEM file without a trailing newline would
# otherwise glue its END line to the next file's BEGIN line.
{ cat "$certs_dir/ca.pem"; echo; cat "$certs_dir/real-ca.pem"; } > "$trust_tmp"
mv -f "$trust_tmp" "$certs_dir/trust.pem"
