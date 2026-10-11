"""Tests for scripts/sandbox/tls-preflight.py and the sandbox trust-bundle wiring.

Fully local: a throwaway CA and a TLS server on 127.0.0.1, reached through a
minimal in-process CONNECT proxy, exactly as clients inside the sandbox reach a
tunneled host. No network access.
"""
import importlib.util
import os
import pathlib
import select
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PREFLIGHT = REPO_ROOT / "scripts" / "sandbox" / "tls-preflight.py"
STAGE2 = REPO_ROOT / "scripts" / "sandbox" / "stage2-run.sh"
STAGE1 = REPO_ROOT / "scripts" / "dev-sandbox.sh"


def _openssl(*args, cwd):
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


def _make_ca(d, name):
    _openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", f"/CN={name}",
             "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign",
             "-keyout", f"{name}.key", "-out", f"{name}.pem", cwd=d)
    return d / f"{name}.pem", d / f"{name}.key"


def _make_leaf(d, ca_pem, ca_key):
    _openssl("req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=localhost",
             "-keyout", "leaf.key", "-out", "leaf.csr", cwd=d)
    (d / "leaf.ext").write_text("subjectAltName=DNS:localhost\nbasicConstraints=CA:FALSE\n")
    _openssl("x509", "-req", "-in", "leaf.csr", "-CA", str(ca_pem), "-CAkey", str(ca_key),
             "-CAcreateserial", "-days", "1", "-extfile", "leaf.ext", "-out", "leaf.pem", cwd=d)
    return d / "leaf.pem", d / "leaf.key"


def _relay(a, b):
    socks = [a, b]
    try:
        while True:
            r, _, _ = select.select(socks, [], [], 5)
            if not r:
                return
            for s in r:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except OSError:  # a peer aborting a failed handshake is expected in the negative tests
        return
    finally:
        a.close()
        b.close()


@unittest.skipUnless(shutil.which("openssl"), "openssl CLI required")
class TlsPreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        d = pathlib.Path(cls._tmp.name)
        cls.public_ca, public_key = _make_ca(d, "public-root")   # stands in for real-ca.pem
        cls.sandbox_ca, _ = _make_ca(d, "sandbox-ca")              # stands in for ca.pem
        leaf_pem, leaf_key = _make_leaf(d, cls.public_ca, public_key)
        cls.trust = d / "trust.pem"                                # what dev-sandbox.sh builds
        cls.trust.write_bytes(cls.sandbox_ca.read_bytes() + cls.public_ca.read_bytes())

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(leaf_pem, leaf_key)
        cls.tls_srv = socket.create_server(("127.0.0.1", 0))
        cls.tls_port = cls.tls_srv.getsockname()[1]

        def serve_tls():
            while True:
                try:
                    conn, _ = cls.tls_srv.accept()
                except OSError:
                    return
                try:
                    with ctx.wrap_socket(conn, server_side=True) as t:
                        t.recv(1)
                except (ssl.SSLError, OSError):
                    pass

        cls.proxy = socket.create_server(("127.0.0.1", 0))
        cls.proxy_port = cls.proxy.getsockname()[1]

        def serve_proxy():
            while True:
                try:
                    c, _ = cls.proxy.accept()
                except OSError:
                    return
                head = b""
                while b"\r\n\r\n" not in head:
                    chunk = c.recv(4096)
                    if not chunk:
                        break
                    head += chunk
                target = head.split(b" ")[1].decode()
                host, port = target.rsplit(":", 1)
                if host != "localhost":
                    c.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                    c.close()
                    continue
                up = socket.create_connection(("127.0.0.1", int(port)))
                c.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                threading.Thread(target=_relay, args=(c, up), daemon=True).start()

        threading.Thread(target=serve_tls, daemon=True).start()
        threading.Thread(target=serve_proxy, daemon=True).start()

        spec = importlib.util.spec_from_file_location("tls_preflight", PREFLIGHT)
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    @classmethod
    def tearDownClass(cls):
        cls.tls_srv.close()
        cls.proxy.close()
        cls._tmp.cleanup()

    def run_preflight(self, cafile, *hosts):
        env = dict(os.environ, SSL_CERT_FILE=str(cafile), DEV_SANDBOX_PROXY=f"127.0.0.1:{self.proxy_port}")
        return subprocess.run(["python3", str(PREFLIGHT), *hosts], env=env, capture_output=True,
                              text=True, timeout=60)

    def test_combined_trust_bundle_verifies_tunneled_host(self):
        """Pass case: trust.pem (sandbox CA + public roots) verifies a host with a public chain."""
        r = self.run_preflight(self.trust, f"localhost:{self.tls_port}")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("chain verified against SSL_CERT_FILE", r.stderr)

    def test_sandbox_ca_alone_fails_loudly(self):
        """The 056493bb configuration: SSL_CERT_FILE = ca.pem only -> untrusted, exit 1."""
        r = self.run_preflight(self.sandbox_ca, f"localhost:{self.tls_port}")
        self.assertEqual(r.returncode, 1)
        self.assertIn("NOT trusted by SSL_CERT_FILE", r.stderr)
        self.assertIn("UnknownIssuer", r.stderr)

    def test_any_untrusted_host_fails_the_whole_preflight(self):
        r = self.run_preflight(self.trust, f"localhost:{self.tls_port}", "blocked.example:443")
        self.assertEqual(r.returncode, 1)
        self.assertIn("1 of 2 passthrough host(s) untrusted", r.stderr)

    def test_missing_bundle_and_usage_errors(self):
        self.assertEqual(self.run_preflight(pathlib.Path(self._tmp.name) / "absent.pem", "localhost").returncode, 1)
        self.assertEqual(self.run_preflight(self.trust).returncode, 2)

    def test_verification_is_never_relaxed(self):
        src = PREFLIGHT.read_text()
        self.assertIn("ssl.CERT_REQUIRED", src)
        self.assertIn("check_hostname = True", src)
        for weak in ("CERT_NONE", "check_hostname = False", "_create_unverified_context"):
            self.assertNotIn(weak, src)

    def test_split_host(self):
        self.assertEqual(self.mod.split_host("github.com"), ("github.com", 443))
        self.assertEqual(self.mod.split_host("example.org:8443"), ("example.org", 8443))


class TrustBundleWiringTests(unittest.TestCase):
    """The clients that replace their roots with SSL_CERT_FILE get trust.pem; the proxy keeps ca.pem."""

    def test_exclusive_trust_clients_point_at_trust_bundle(self):
        s = STAGE2.read_text()
        for var in ("SSL_CERT_FILE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO", "npm_config_cafile"):
            self.assertIn(f"--setenv {var} /work/certs/trust.pem", s, var)
            self.assertNotIn(f"--setenv {var} /work/certs/ca.pem", s, var)
        self.assertIn("--setenv NODE_EXTRA_CA_CERTS /work/certs/ca.pem", s)  # additive, unchanged
        self.assertIn("python3 /work/proxy.py /work/http /work/certs /work/certs/real-ca.pem", s)

    def test_stage1_builds_trust_bundle_from_both_cas(self):
        s = STAGE1.read_text()
        self.assertIn('cat "$SANDBOX_ROOT/root/certs/ca.pem" "$SANDBOX_ROOT/root/certs/real-ca.pem"', s)
        self.assertIn('"$SANDBOX_ROOT/root/certs/trust.pem"', s)
        self.assertIn("tls-preflight.py", s)

    def test_scripts_parse(self):
        for script in (STAGE1, STAGE2, REPO_ROOT / "tests" / "install" / "install-update-e2e.sh"):
            r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"{script}: {r.stderr}")


if __name__ == "__main__":
    unittest.main()
