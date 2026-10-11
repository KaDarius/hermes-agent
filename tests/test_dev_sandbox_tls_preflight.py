"""Tests for scripts/sandbox/tls-preflight.py and the sandbox trust-bundle wiring.

Every test here executes the shipped code; none reads script source:

* build-trust-bundle.sh is run against fixture CAs and its output is parsed
  by OpenSSL (via the ssl module), not grepped.
* stage2-run.sh is run for real with a fake `bwrap` on PATH that applies
  bwrap's own environment semantics (--clearenv, then --setenv/--unsetenv in
  argument order) and then runs the real in-sandbox payload. The payload's
  final command dumps os.environ, so the assertions see the environment the
  sandboxed client actually gets, after every override.
* tls-preflight.py completes real handshakes against a local TLS server
  reached through a minimal CONNECT proxy, including a wrong-hostname case.

Fully local: throwaway CAs and servers on 127.0.0.1. No network access.
"""
import importlib.util
import json
import os
import pathlib
import re
import select
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PREFLIGHT = REPO_ROOT / "scripts" / "sandbox" / "tls-preflight.py"
STAGE2 = REPO_ROOT / "scripts" / "sandbox" / "stage2-run.sh"
STAGE1 = REPO_ROOT / "scripts" / "dev-sandbox.sh"
BUILD_BUNDLE = REPO_ROOT / "scripts" / "sandbox" / "build-trust-bundle.sh"

# A real hostname the leaf certificate does NOT cover; the test proxy routes it
# to the same TLS server as "localhost".
WRONG_HOST = "wrong-host.test"
ROUTED_HOSTS = {"localhost", WRONG_HOST}


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


def build_bundle(certs_dir):
    return subprocess.run(["bash", str(BUILD_BUNDLE), str(certs_dir)], capture_output=True, text=True,
                          timeout=30)


def pem_blocks_der(path):
    """Every certificate in a PEM file, as DER bytes, in file order."""
    text = pathlib.Path(path).read_text()
    blocks = re.findall(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", text, re.S)
    return [ssl.PEM_cert_to_DER_cert(b) for b in blocks]


def trusted_subject_cns(path):
    """CNs of the CA certs OpenSSL actually loads from PATH as trust anchors."""
    ctx = ssl.create_default_context(cafile=str(path))
    cns = set()
    for cert in ctx.get_ca_certs():
        for rdn in cert["subject"]:
            for key, value in rdn:
                if key == "commonName":
                    cns.add(value)
    return cns


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
        certs = d / "certs"                                         # built by the shipped script
        certs.mkdir()
        shutil.copy(cls.sandbox_ca, certs / "ca.pem")
        shutil.copy(cls.public_ca, certs / "real-ca.pem")
        r = build_bundle(certs)
        assert r.returncode == 0, r.stderr
        cls.trust = certs / "trust.pem"

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
                if host not in ROUTED_HOSTS:
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

    def test_wrong_hostname_is_rejected_even_with_trusted_chain(self):
        """Same server, same trusted chain: only the requested name differs."""
        ok = self.run_preflight(self.trust, f"localhost:{self.tls_port}")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        r = self.run_preflight(self.trust, f"{WRONG_HOST}:{self.tls_port}")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("NOT trusted by SSL_CERT_FILE", r.stderr)
        self.assertIn("Hostname mismatch", r.stderr)
        self.assertIn(WRONG_HOST, r.stderr)

    def test_missing_bundle_and_usage_errors(self):
        self.assertEqual(self.run_preflight(pathlib.Path(self._tmp.name) / "absent.pem", "localhost").returncode, 1)
        self.assertEqual(self.run_preflight(self.trust).returncode, 2)

    def test_split_host(self):
        self.assertEqual(self.mod.split_host("github.com"), ("github.com", 443))
        self.assertEqual(self.mod.split_host("example.org:8443"), ("example.org", 8443))




@unittest.skipUnless(shutil.which("openssl"), "openssl CLI required")
class TrustBundleBuildTests(unittest.TestCase):
    """Runs scripts/sandbox/build-trust-bundle.sh, the step dev-sandbox.sh executes every start."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        d = pathlib.Path(self._tmp.name)
        self.sandbox_ca, _ = _make_ca(d, "sandbox-ca")
        self.public_ca, _ = _make_ca(d, "public-root")
        self.certs = d / "certs"
        self.certs.mkdir()

    def stage(self, ca=True, real=True):
        if ca:
            shutil.copy(self.sandbox_ca, self.certs / "ca.pem")
        if real:
            shutil.copy(self.public_ca, self.certs / "real-ca.pem")

    def test_bundle_holds_both_cas_as_trust_anchors(self):
        self.stage()
        r = build_bundle(self.certs)
        self.assertEqual(r.returncode, 0, r.stderr)
        trust = self.certs / "trust.pem"
        self.assertEqual(pem_blocks_der(trust),
                         pem_blocks_der(self.sandbox_ca) + pem_blocks_der(self.public_ca))
        self.assertEqual(trusted_subject_cns(trust), {"sandbox-ca", "public-root"})
        self.assertFalse((self.certs / "trust.pem.tmp").exists())

    def test_rebuild_replaces_a_stale_bundle(self):
        self.stage()
        (self.certs / "trust.pem").write_text("stale\n")
        self.assertEqual(build_bundle(self.certs).returncode, 0)
        self.assertEqual(trusted_subject_cns(self.certs / "trust.pem"), {"sandbox-ca", "public-root"})

    def test_input_without_trailing_newline_still_yields_two_certs(self):
        self.stage()
        ca = self.certs / "ca.pem"
        ca.write_text(ca.read_text().rstrip("\n"))
        self.assertEqual(build_bundle(self.certs).returncode, 0)
        self.assertEqual(trusted_subject_cns(self.certs / "trust.pem"), {"sandbox-ca", "public-root"})

    def test_missing_input_fails_and_writes_nothing(self):
        for ca, real in ((False, True), (True, False)):
            for f in self.certs.iterdir():
                f.unlink()
            self.stage(ca=ca, real=real)
            r = build_bundle(self.certs)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("missing or empty", r.stderr)
            self.assertFalse((self.certs / "trust.pem").exists())


# A stand-in for bwrap: applies bwrap's environment semantics in argument order,
# records the bind mounts, ignores the other namespace/mount options, then runs
# the command. An option it does not know fails loudly rather than being
# mis-parsed (bwrap options take a fixed number of arguments).
FAKE_BWRAP = r'''
import json, os, sys
ARITY = {"--clearenv": 0, "--unshare-pid": 0, "--unshare-all": 0, "--unshare-net": 0,
         "--unshare-user": 0, "--unshare-ipc": 0, "--unshare-uts": 0, "--unshare-cgroup": 0,
         "--share-net": 0, "--die-with-parent": 0, "--new-session": 0, "--as-pid-1": 0,
         "--proc": 1, "--tmpfs": 1, "--dev": 1, "--dir": 1, "--chdir": 1, "--unsetenv": 1,
         "--hostname": 1, "--remount-ro": 1, "--mqueue": 1,
         "--bind": 2, "--ro-bind": 2, "--dev-bind": 2, "--bind-try": 2, "--ro-bind-try": 2,
         "--dev-bind-try": 2, "--symlink": 2, "--setenv": 2}
record = os.environ["FAKE_BWRAP_RECORD"]
args = sys.argv[1:]
env = dict(os.environ)
binds = []
i = 0
while i < len(args) and args[i] != "--":
    opt = args[i]
    if opt not in ARITY:
        sys.exit(f"fake bwrap: unknown option {opt!r}; teach the test its arity")
    vals = args[i + 1:i + 1 + ARITY[opt]]
    if opt == "--clearenv":
        env = {}
    elif opt == "--setenv":
        env[vals[0]] = vals[1]
    elif opt == "--unsetenv":
        env.pop(vals[0], None)
    elif opt in ("--bind", "--ro-bind", "--dev-bind", "--bind-try", "--ro-bind-try", "--dev-bind-try"):
        binds.append(vals)
    i += 1 + ARITY[opt]
with open(record, "w") as f:
    json.dump({"binds": binds}, f)
cmd = args[i + 1:]
os.execvpe(cmd[0], cmd, env)
'''

# Stands in for python3 inside the sandbox (first on the sandbox PATH): the
# payload starts `python3 /work/proxy.py ...` in the background. Anything else
# would be an unexpected call and fails loudly.
FAKE_PYTHON3 = '''#!/bin/sh
case "$1" in
  /work/proxy.py) exit 0 ;;
esac
echo "fake python3: unexpected call: $*" >&2
exit 97
'''

DUMP_ENV = "import json, os; print('ENVDUMP=' + json.dumps(dict(os.environ)))"
EXCLUSIVE_TRUST_VARS = ("SSL_CERT_FILE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO", "npm_config_cafile")


def _modern_bash():
    """stage2-run.sh is Linux-only and expands empty arrays under `set -u`, which needs bash >= 4.4."""
    bash = shutil.which("bash")
    if not bash:
        return None
    r = subprocess.run([bash, "-c", 'echo "${BASH_VERSINFO[0]} ${BASH_VERSINFO[1]}"'],
                       capture_output=True, text=True)
    major, minor = (int(x) for x in r.stdout.split())
    return bash if (major, minor) >= (4, 4) else None


MODERN_BASH = _modern_bash()


@unittest.skipUnless(shutil.which("openssl"), "openssl CLI required")
@unittest.skipUnless(MODERN_BASH, "bash >= 4.4 required (stage2-run.sh targets Linux bash)")
class SandboxEffectiveEnvTests(unittest.TestCase):
    """Runs the real stage2-run.sh payload and inspects the env its command receives."""

    @classmethod
    def setUpClass(cls):
        # The payload waits for its proxy on 127.0.0.1:8080. Our fake proxy.py
        # does not listen, so the test does; if the port is already taken by
        # something listening, the probe succeeds against that instead.
        cls.listener = None
        try:
            cls.listener = socket.create_server(("127.0.0.1", 8080))
        except OSError:
            try:
                socket.create_connection(("127.0.0.1", 8080), timeout=2).close()
            except OSError as exc:
                raise unittest.SkipTest(f"127.0.0.1:8080 neither bindable nor listening: {exc}")
        if cls.listener is not None:
            def accept_forever():
                while True:
                    try:
                        conn, _ = cls.listener.accept()
                    except OSError:
                        return
                    conn.close()
            threading.Thread(target=accept_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        if cls.listener is not None:
            cls.listener.close()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        d = pathlib.Path(self._tmp.name)
        self.root = d / "sandbox"
        for sub in ("root/logs", "root/certs", "etc", "home"):
            (self.root / sub).mkdir(parents=True)
        (self.root / "root" / "logs" / "slirp.ready").write_text("ready\n")
        # Certs exactly as dev-sandbox.sh leaves them: CAs, then the built bundle.
        sandbox_ca, _ = _make_ca(d, "sandbox-ca")
        public_ca, _ = _make_ca(d, "public-root")
        certs = self.root / "root" / "certs"
        shutil.copy(sandbox_ca, certs / "ca.pem")
        shutil.copy(public_ca, certs / "real-ca.pem")
        r = build_bundle(certs)
        self.assertEqual(r.returncode, 0, r.stderr)

        self.home = d / "home" / "tester"
        bindir = self.home / ".local" / "bin"
        bindir.mkdir(parents=True)
        (bindir / "python3").write_text(FAKE_PYTHON3)
        (bindir / "python3").chmod(0o755)
        self.fakebin = d / "fakebin"
        self.fakebin.mkdir()
        (self.fakebin / "bwrap").write_text(f"#!{sys.executable}\n{FAKE_BWRAP}")
        (self.fakebin / "bwrap").chmod(0o755)
        self.record = d / "bwrap-record.json"

    def run_stage2(self, **extra_env):
        env = {
            "PATH": f"{self.fakebin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "DEV_SANDBOX_ROOT": str(self.root),
            "DEV_SANDBOX_BASH": MODERN_BASH,
            "DEV_SANDBOX_INTERACTIVE": "false",
            "DEV_SANDBOX_USER": "tester",
            "DEV_SANDBOX_HOME": str(self.home),
            "FAKE_BWRAP_RECORD": str(self.record),
            # Host values that must NOT leak through: bwrap --clearenv drops them.
            "SSL_CERT_FILE": "/host/leaked.pem",
            "CURL_CA_BUNDLE": "/host/leaked.pem",
            **extra_env,
        }
        r = subprocess.run([MODERN_BASH, str(STAGE2), sys.executable, "-c", DUMP_ENV], env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, f"stdout={r.stdout}\nstderr={r.stderr}")
        lines = [ln for ln in r.stdout.splitlines() if ln.startswith("ENVDUMP=")]
        self.assertEqual(len(lines), 1, r.stdout)
        binds = json.loads(self.record.read_text())["binds"]
        return json.loads(lines[0][len("ENVDUMP="):]), binds

    @staticmethod
    def resolve(sandbox_path, binds):
        """Map an in-sandbox path to the host file through the recorded bind mounts (last match wins)."""
        best = None
        for src, dest in binds:
            if sandbox_path == dest or sandbox_path.startswith(dest.rstrip("/") + "/"):
                if best is None or len(dest) >= len(best[1]):
                    best = (src, dest)
        assert best is not None, f"{sandbox_path} is not under any bind mount: {binds}"
        return pathlib.Path(best[0] + sandbox_path[len(best[1]):])

    def test_exclusive_trust_clients_get_the_combined_bundle(self):
        env, binds = self.run_stage2()
        for var in EXCLUSIVE_TRUST_VARS:
            with self.subTest(var=var):
                self.assertEqual(env.get(var), "/work/certs/trust.pem")
                host_file = self.resolve(env[var], binds)
                self.assertEqual(host_file, self.root / "root" / "certs" / "trust.pem")
                self.assertEqual(trusted_subject_cns(host_file), {"sandbox-ca", "public-root"})

    def test_node_extra_ca_stays_additive_sandbox_ca(self):
        env, binds = self.run_stage2()
        host_file = self.resolve(env["NODE_EXTRA_CA_CERTS"], binds)
        self.assertEqual(trusted_subject_cns(host_file), {"sandbox-ca"})

    def test_preflight_opt_in_is_off_by_default(self):
        env, _ = self.run_stage2()
        self.assertEqual(env.get("DEV_SANDBOX_TLS_PREFLIGHT"), "")


class ScriptSyntaxTests(unittest.TestCase):
    def test_scripts_parse(self):
        for script in (STAGE1, STAGE2, BUILD_BUNDLE, REPO_ROOT / "tests" / "install" / "install-update-e2e.sh"):
            r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"{script}: {r.stderr}")


if __name__ == "__main__":
    unittest.main()
