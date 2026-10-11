#!/usr/bin/env python3
"""In-sandbox TLS preflight: prove each passthrough host's real chain is trusted.

The proxy TUNNELS every host that has no fixture directory, so clients inside
the sandbox see the host's real certificate chain. Some clients (uv, and
anything else that treats SSL_CERT_FILE as its only trust source) then trust
exactly the bundle in SSL_CERT_FILE and nothing else. If that bundle lacks the
public roots, the failure surfaces much later as an opaque `UnknownIssuer`
from deep inside an install step.

This check fails loudly at sandbox start instead. For each host it opens a
CONNECT tunnel through the sandbox proxy and completes a fully verified TLS
handshake (hostname checked, CERT_REQUIRED) using ONLY the file named by
SSL_CERT_FILE, the same exclusive semantics uv uses. Verification is never
relaxed: a host that does not verify is an error, not a warning.

Usage: tls-preflight.py HOST[:PORT] [HOST[:PORT] ...]
Environment: SSL_CERT_FILE (required), DEV_SANDBOX_PROXY (default 127.0.0.1:8080).
Exit status: 0 when every host verifies, 1 otherwise, 2 on bad usage.
"""
import os
import socket
import ssl
import sys

TIMEOUT = 15


def split_host(spec, default_port=443):
    if ":" in spec:
        host, port = spec.rsplit(":", 1)
        return host, int(port)
    return spec, default_port


def check(host, port, cafile, proxy_host, proxy_port):
    """Return None when HOST verifies against CAFILE only, else a one-line reason."""
    # create_default_context(cafile=...) loads ONLY that file: no system store.
    ctx = ssl.create_default_context(cafile=cafile)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    try:
        raw = socket.create_connection((proxy_host, proxy_port), timeout=TIMEOUT)
    except OSError as exc:
        return f"cannot reach the sandbox proxy at {proxy_host}:{proxy_port}: {exc}"
    with raw:
        raw.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
        reply = b""
        while b"\r\n\r\n" not in reply and len(reply) < 65536:
            chunk = raw.recv(4096)
            if not chunk:
                break
            reply += chunk
        status = reply.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        if " 200" not in status:
            return f"proxy refused CONNECT {host}:{port}: {status or 'no reply'}"
        try:
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                tls.do_handshake()
        except ssl.SSLCertVerificationError as exc:
            return f"certificate chain NOT trusted by SSL_CERT_FILE={cafile}: {exc.verify_message}"
        except (ssl.SSLError, OSError) as exc:
            return f"TLS handshake failed: {exc}"
    return None


def main(argv):
    hosts = argv[1:]
    cafile = os.environ.get("SSL_CERT_FILE", "")
    if not hosts or not cafile:
        print("usage: SSL_CERT_FILE=<bundle> tls-preflight.py HOST[:PORT] ...", file=sys.stderr)
        return 2
    if not os.path.isfile(cafile):
        print(f"error: [tls-preflight] SSL_CERT_FILE={cafile} does not exist", file=sys.stderr)
        return 1
    proxy_host, proxy_port = split_host(os.environ.get("DEV_SANDBOX_PROXY", "127.0.0.1:8080"), 8080)
    failed = 0
    for spec in hosts:
        host, port = split_host(spec)
        reason = check(host, port, cafile, proxy_host, proxy_port)
        if reason:
            failed += 1
            print(f"error: [tls-preflight] {host}:{port}: {reason}", file=sys.stderr)
        else:
            print(f"[tls-preflight] {host}:{port}: chain verified against SSL_CERT_FILE", file=sys.stderr)
    if failed:
        print(f"error: [tls-preflight] {failed} of {len(hosts)} passthrough host(s) untrusted; "
              "clients that trust only SSL_CERT_FILE (uv) will fail with UnknownIssuer", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
