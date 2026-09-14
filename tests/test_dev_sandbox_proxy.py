import importlib.util
import pathlib
import select
import socket
import ssl
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PROXY_SCRIPT = REPO_ROOT / "scripts" / "sandbox" / "proxy.py"


class DevSandboxProxyUnitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temp_dir = tempfile.TemporaryDirectory()
        base = pathlib.Path(cls._temp_dir.name)
        cls.fixture_root = base / "http"
        cls.fixture_root.mkdir(parents=True, exist_ok=True)
        (cls.fixture_root / "hermes-agent.nousresearch.com").mkdir(parents=True, exist_ok=True)
        (cls.fixture_root / "hermes-agent.nousresearch.com" / "install.sh").write_text("#!/bin/sh\necho ok\n")

        cls.certs_dir = base / "certs"
        cls.certs_dir.mkdir(parents=True, exist_ok=True)
        cls.real_ca = cls.certs_dir / "ca.pem"
        cls.real_ca.write_text("# dummy ca\n")

        old_argv = sys.argv
        sys.argv = [str(PROXY_SCRIPT), str(cls.fixture_root), str(cls.certs_dir), str(cls.real_ca)]
        try:
            spec = importlib.util.spec_from_file_location("dev_sandbox_proxy", PROXY_SCRIPT)
            cls.proxy_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.proxy_mod)
        finally:
            sys.argv = old_argv

    @classmethod
    def tearDownClass(cls):
        cls._temp_dir.cleanup()

    def test_fixture_host_is_detected(self):
        root = self.proxy_mod.ROOT
        self.assertTrue((root / "hermes-agent.nousresearch.com").is_dir())
        self.assertFalse((root / "registry.npmjs.org").is_dir())
        self.assertFalse((root / "pypi.org").is_dir())

    def test_handle_connect_tunnels_external_host(self):
        client_conn = MagicMock()
        with patch.object(self.proxy_mod, "tunnel") as mock_tunnel:
            self.proxy_mod.handle_connect(client_conn, "registry.npmjs.org:443")
            mock_tunnel.assert_called_once_with(client_conn, "registry.npmjs.org", 443)

    def test_handle_connect_intercepts_fixture_host(self):
        client_conn = MagicMock()
        with patch.object(self.proxy_mod, "tunnel") as mock_tunnel, \
             patch.object(self.proxy_mod, "cert_for", return_value=(self.certs_dir / "test.pem", self.certs_dir / "test.key")), \
             patch("ssl.SSLContext") as mock_ssl_ctx:
            mock_ctx_inst = MagicMock()
            mock_ssl_ctx.return_value = mock_ctx_inst
            mock_tls = MagicMock()
            mock_ctx_inst.wrap_socket.return_value.__enter__.return_value = mock_tls
            mock_tls.split.return_value = [b"GET /install.sh HTTP/1.1"]
            with patch.object(self.proxy_mod, "read_request", return_value=b"GET /install.sh HTTP/1.1\r\n\r\n"), \
                 patch.object(self.proxy_mod, "respond_fixture") as mock_respond:
                self.proxy_mod.handle_connect(client_conn, "hermes-agent.nousresearch.com:443")
                mock_tunnel.assert_not_called()
                client_conn.sendall.assert_called_with(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                mock_respond.assert_called_once()

    def test_bidirectional_tunnel_relay(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(('127.0.0.1', 0))
        server.listen(1)
        port = server.getsockname()[1]

        def handle_upstream():
            conn, _ = server.accept()
            data = conn.recv(1024)
            conn.sendall(b"pong:" + data)
            conn.close()

        t = threading.Thread(target=handle_upstream, daemon=True)
        t.start()

        client_sock, peer_sock = socket.socketpair()

        def run_tunnel():
            self.proxy_mod.tunnel(peer_sock, '127.0.0.1', port)

        tunnel_thread = threading.Thread(target=run_tunnel, daemon=True)
        tunnel_thread.start()

        established = client_sock.recv(1024)
        self.assertIn(b"200 Connection Established", established)

        client_sock.sendall(b"ping")
        response = client_sock.recv(1024)
        self.assertEqual(response, b"pong:ping")

        client_sock.close()
        peer_sock.close()
        server.close()


if __name__ == "__main__":
    unittest.main()
