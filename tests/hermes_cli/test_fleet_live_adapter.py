import pytest
pytestmark = pytest.mark.macos_only
"""Temporary native-store tests; fixture capabilities are not live authority."""
import hashlib
import copy
from pathlib import Path
import tempfile
import json
import socket
import threading
import time
from unittest.mock import patch
import unittest

from gateway import drain_control
from hermes_cli.maintenance.hermes_job_install import Refused
from hermes_cli.maintenance.hermes_live_adapter import NativeMarkerStore
from hermes_cli.maintenance.hermes_maintenance_controller import Binding, REQUIRED_ROUTES


class NativeStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.profile = Path(self.temp.name).resolve()
        self.receipts = self.profile / 'receipts'
        self.receipts.mkdir(mode=0o700)
        self.raw = b'{"action":"drain","maintenance_owner":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'

    def test_default_refuses_before_native_lock_creation(self):
        store = NativeMarkerStore(self.profile, self.receipts)
        with self.assertRaises(Refused):
            store.create_marker(self.raw, lambda _: None)
        self.assertFalse((self.profile / '.drain-control.lock').exists())

    def test_missing_profile_has_refused_error_contract(self):
        store=NativeMarkerStore(self.profile/'missing',self.receipts,require_authority=lambda:None,require_protocol=lambda:None)
        with self.assertRaises(Refused):store.create_marker(self.raw,lambda _:None)

    def test_real_native_store_and_legacy_writer_share_lock_and_ownership(self):
        store = NativeMarkerStore(self.profile, self.receipts,
                                  require_authority=lambda: None,
                                  require_protocol=lambda: None)
        tokens = []
        store.create_marker(self.raw, tokens.append)
        self.assertEqual(store.inspect(), {'token': tokens[0], 'sha256': hashlib.sha256(self.raw).hexdigest()})
        with self.assertRaises(drain_control.DrainControlConflict):
            drain_control.clear_drain_request(home=self.profile)
        self.assertEqual(store.remove_owned(tokens[0], hashlib.sha256(self.raw).hexdigest()), 'removed')

    def test_protocol_revocation_before_native_publication_preserves_absence(self):
        calls = []
        def protocol():
            calls.append(True)
            if len(calls) >= 3:
                raise Refused('protocol_revoked')
        store = NativeMarkerStore(self.profile, self.receipts,
                                  require_authority=lambda: None,
                                  require_protocol=protocol)
        with self.assertRaises(Refused):
            store.create_marker(self.raw, lambda _: None)
        self.assertFalse((self.profile / '.drain_request.json').exists())

    def test_adapter_never_exposes_unconditional_replacement(self):
        store = NativeMarkerStore(self.profile,self.receipts,require_authority=lambda:None,require_protocol=lambda:None)
        store.create_marker(self.raw,lambda _:None)
        with self.assertRaises(Refused): store.replace_marker(b'peer')
        self.assertEqual((self.profile/'.drain_request.json').read_bytes(),self.raw)

    def test_profile_swap_before_native_entry_cannot_write_peer(self):
        from gateway import drain_marker_protocol as native
        store=NativeMarkerStore(self.profile,self.receipts,require_authority=lambda:None,require_protocol=lambda:None)
        with tempfile.TemporaryDirectory() as other:
            peer=Path(other).resolve(); original=self.profile.with_name(self.profile.name+'-original')
            real=native.create_owned_marker
            def swap(*args,**kwargs):
                self.profile.rename(original); self.profile.symlink_to(peer,target_is_directory=True)
                return real(*args,**kwargs)
            try:
                with patch.object(native,'create_owned_marker',side_effect=swap):
                    with self.assertRaises(Refused):store.create_marker(self.raw,lambda _:None)
                self.assertFalse((peer/'.drain_request.json').exists())
            finally:
                if self.profile.is_symlink():self.profile.unlink()
                if original.exists():original.rename(self.profile)


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.binding = Binding('/fixture/hermes', 123, 456, 'a' * 40, REQUIRED_ROUTES)
        self.identity = {'pid': 123, 'start_time': 456, 'hermes_home': '/fixture/hermes', 'code_sha': 'a' * 40}
        self.marker = {'token': '1:2:3', 'sha256': 'b' * 64}
        self.status = {'pid': 123, 'answering_pid': 123, 'answered_at': 101., 'gateway_state': 'draining',
                       'maintenance_counters': {'writer_pid': 123, 'writer_start_time': 456,
                        'profile': '/fixture/hermes', 'code_sha': 'a' * 40, 'observed_at': 100.,
                        'counters': {k: {'valid': True, 'count': 0} for k in ('messaging','cron','api')},
                        'drain': {'valid': True, 'present': True, 'requested': True,
                                  'gateway_state': 'draining', 'token': '1:2:3', 'sha256': 'b' * 64,
                                  'observed_at': 99.}}}

    def adapt(self, **kwargs):
        from hermes_cli.maintenance.hermes_live_adapter import adapt_native_observation
        return adapt_native_observation(self.identity, self.status, self.identity,
                                         self.binding, now=101., not_before=98.,
                                         expected_marker=self.marker, **kwargs)

    def test_distinct_sample_times_preserved_without_claiming_route_authority(self):
        result = self.adapt()
        self.assertEqual(result['observed_at'], 99.)
        self.assertNotIn('admission_routes', result)

    def test_live_answer_cannot_refresh_stale_drain_or_counters(self):
        for key in ('observed_at', 'drain'):
            original = copy.deepcopy(self.status)
            if key == 'drain': self.status['maintenance_counters']['drain']['observed_at'] = 90.
            else: self.status['maintenance_counters']['observed_at'] = 90.
            with self.assertRaises(Refused): self.adapt()
            self.status = original

    def test_same_hash_different_marker_generation_is_refused(self):
        self.status['maintenance_counters']['drain']['token'] = '1:2:4'
        with self.assertRaises(Refused): self.adapt()

    def test_sample_from_another_writer_is_refused(self):
        self.status['maintenance_counters']['writer_pid'] = 999
        with self.assertRaises(Refused): self.adapt()

    def test_wrong_nested_identity_fields_are_refused(self):
        sample = self.status['maintenance_counters']
        for key, value in [('writer_pid', True), ('writer_start_time', 999),
                           ('profile', '/other'), ('code_sha', 'c'*40)]:
            old = sample[key]; sample[key] = value
            with self.assertRaises(Refused): self.adapt()
            sample[key] = old

    def test_identity_change_across_status_request_is_refused(self):
        from hermes_cli.maintenance.hermes_live_adapter import adapt_native_observation
        after = dict(self.identity, start_time=457)
        with self.assertRaises(Refused):
            adapt_native_observation(self.identity,self.status,after,self.binding,now=101.,not_before=98.,expected_marker=self.marker)

    def test_unreadable_counter_is_not_zero(self):
        self.status['maintenance_counters']['counters']['api'] = {'valid': False, 'count': None}
        with self.assertRaises(Refused): self.adapt()

    def test_resume_requires_explicit_absence(self):
        self.marker = None
        self.status['gateway_state'] = 'running'
        drain = self.status['maintenance_counters']['drain']
        drain.update(gateway_state='running', token=None, sha256=None)
        with self.assertRaises(Refused): self.adapt(resuming=True)
        drain.update(present=False, requested=False)
        self.assertIsNone(self.adapt(resuming=True)['marker_sha256'])

    def test_resume_requires_null_keys_to_be_present(self):
        self.marker=None;self.status['gateway_state']='running'
        drain=self.status['maintenance_counters']['drain']
        drain.update(present=False,requested=False,gateway_state='running',token=None,sha256=None)
        for key in ('token','sha256'):
            del drain[key]
            with self.assertRaises(Refused):self.adapt(resuming=True)
            drain[key]=None


class SocketTests(unittest.TestCase):
    def exchange(self, response):
        from hermes_cli.maintenance.hermes_live_adapter import DirectControlReader
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp).resolve()
            path = profile / 'gateway.sock'
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(path)); path.chmod(0o600); listener.listen(1)
            def serve():
                with listener:
                    connection, _ = listener.accept()
                    with connection:
                        connection.recv(4096)
                        try: connection.sendall(response)
                        except BrokenPipeError: pass
            worker = threading.Thread(target=serve, daemon=True); worker.start()
            try: return DirectControlReader(profile).query('identify')
            finally: worker.join(timeout=3)

    def test_real_socket_valid_reply(self):
        result = self.exchange(b'{"protocol":1,"id":1,"ok":true,"result":{"pid":123}}\n')
        self.assertEqual(result, {'pid':123})

    def test_duplicate_protocol_and_wrong_request_id_refused(self):
        for reply in [b'{"protocol":1,"protocol":2,"id":1,"ok":true,"result":{}}\n',
                      b'{"protocol":1,"id":2,"ok":true,"result":{}}\n']:
            with self.assertRaises(Refused): self.exchange(reply)

    def test_newline_does_not_bypass_size_cap(self):
        with self.assertRaises(Refused):
            self.exchange(json.dumps({'protocol':1,'id':1,'ok':True,'result':{'padding':'x'*524288}}).encode()+b'\n')

    def test_nonfinite_or_boolean_protocol_is_refused(self):
        for response in [b'{"protocol":true,"id":1,"ok":true,"result":{}}\n',
                         b'{"protocol":1,"id":1,"ok":true,"result":{"x":1e999}}\n']:
            with self.assertRaises(Refused): self.exchange(response)

    def test_socket_replacement_during_response_is_refused(self):
        from hermes_cli.maintenance.hermes_live_adapter import DirectControlReader
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp).resolve(); path = profile/'gateway.sock'
            listener = socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
            listener.bind(str(path)); path.chmod(0o600); listener.listen(1)
            replacement = socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
            self.addCleanup(replacement.close)
            def serve():
                with listener:
                    client,_ = listener.accept()
                    with client:
                        client.recv(4096); path.unlink(); replacement.bind(str(path)); path.chmod(0o600)
                        client.sendall(b'{"protocol":1,"id":1,"ok":true,"result":{}}\n')
            worker = threading.Thread(target=serve,daemon=True); worker.start()
            with self.assertRaisesRegex(Refused,'replaced'):
                DirectControlReader(profile).query('status')
            worker.join(timeout=3)

    def test_silent_peer_times_out(self):
        from hermes_cli.maintenance.hermes_live_adapter import DirectControlReader
        with tempfile.TemporaryDirectory() as tmp:
            profile=Path(tmp).resolve(); path=profile/'gateway.sock'
            listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
            listener.bind(str(path)); path.chmod(0o600); listener.listen(1)
            def serve():
                with listener:
                    client,_=listener.accept()
                    with client: client.recv(4096); time.sleep(.15)
            worker=threading.Thread(target=serve,daemon=True); worker.start()
            with self.assertRaises(Refused): DirectControlReader(profile,timeout=.05).query('status')
            worker.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
