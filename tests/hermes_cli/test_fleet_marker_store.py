import os
import pytest
pytestmark = pytest.mark.macos_only
if os.name != "posix":
    pytest.skip("Fleet maintenance adapters require POSIX", allow_module_level=True)
"""Real temporary filesystem and child-process protocol tests; no live profile.
The explicit fixture callbacks model authority/protocol adoption, not prove it."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from hermes_cli.maintenance import hermes_drain_marker_store as sut

class MarkerStoreTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.root=Path(self.temp.name).resolve();self.profile=self.root/'profile';self.profile.mkdir(mode=0o700)
  self.backup=self.root/'receipt';self.backup.mkdir(mode=0o700)
  self.store=sut.MarkerStore(self.profile,self.backup,require_authority=lambda:None,require_protocol=lambda:None)
  self.marker=self.profile/'.drain_request.json';self.data=b'{"action":"drain","principal":"fixture"}\n'
 def create(self):
  tokens=[];self.store.create_marker(self.data,tokens.append);self.assertEqual(len(tokens),1);return tokens[0]
 def receipt(self):return {'version':1,'owner':'fixture','binding':{'profile':str(self.profile)},'state':'prepared'}
 def test_default_refuses_without_lock_receipt_or_marker_creation(self):
  store=sut.MarkerStore(self.profile,self.backup)
  with self.assertRaisesRegex(sut.Refused,'live_marker_authority_unavailable'):store.create_marker(self.data,lambda _:None)
  self.assertEqual(list(self.profile.iterdir()),[]);self.assertEqual(list(self.backup.iterdir()),[])
 def test_protocol_adoption_is_separate_and_required(self):
  store=sut.MarkerStore(self.profile,self.backup,require_authority=lambda:None)
  with self.assertRaisesRegex(sut.Refused,'marker_protocol_unverified'):store.create_marker(self.data,lambda _:None)
  self.assertEqual(list(self.profile.iterdir()),[])
 def test_generation_hash_create_and_owned_remove(self):
  token=self.create();self.assertEqual(self.store.inspect(),{'token':token,'sha256':hashlib.sha256(self.data).hexdigest()})
  self.assertEqual(self.marker.stat().st_mode&0o777,0o600)
  self.assertEqual(self.store.remove_owned(token,hashlib.sha256(self.data).hexdigest()),'removed')
  self.assertIsNone(self.store.inspect());self.assertEqual(self.store.remove_owned(token,hashlib.sha256(self.data).hexdigest()),'absent')
 def test_matching_bytes_from_replacement_are_not_owned(self):
  token=self.create();self.store.replace_marker(self.data)
  with self.assertRaisesRegex(sut.Refused,'marker_replaced'):self.store.remove_owned(token,hashlib.sha256(self.data).hexdigest())
  self.assertEqual(self.marker.read_bytes(),self.data)
 def test_existing_marker_not_clobbered(self):
  self.marker.write_bytes(b'peer')
  with self.assertRaisesRegex(sut.Refused,'marker_exists'):self.create()
  self.assertEqual(self.marker.read_bytes(),b'peer')
 def test_symlink_marker_lock_or_receipt_refused(self):
  outside=self.root/'outside';outside.write_bytes(b'peer')
  for base,name,action in [(self.profile,'.drain_request.json',self.store.inspect),(self.profile,'.drain-control.lock',self.create),(self.backup,'transaction.json',lambda:self.store.write_receipt(self.receipt()))]:
   with self.subTest(name=name):
    p=base/name
    if p.exists():p.unlink()
    p.symlink_to(outside)
    with self.assertRaises(sut.Refused):action()
    self.assertEqual(outside.read_bytes(),b'peer');p.unlink()
 def test_real_other_process_lock_contention_fails_closed(self):
  code="import sys; from pathlib import Path; from hermes_cli.maintenance import hermes_drain_marker_store as s; x=s.MarkerStore(Path(sys.argv[1]),Path(sys.argv[2]),require_authority=lambda:None,require_protocol=lambda:None); g=x.marker_guard(); g.__enter__(); print('held',flush=True); sys.stdin.readline(); g.__exit__(None,None,None)"
  child=subprocess.Popen([sys.executable,'-B','-c',code,str(self.profile),str(self.backup)],cwd=Path(__file__).resolve().parents[2],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True, encoding="utf-8")
  self.addCleanup(lambda:child.kill() if child.poll() is None else None)
  self.assertEqual(child.stdout.readline().strip(),'held')
  with self.assertRaisesRegex(sut.Refused,'marker_lock_busy'):self.create()
  self.assertFalse(self.marker.exists())
  out,err=child.communicate('\n',timeout=5);self.assertEqual(child.returncode,0,err)
  self.create()
 def test_fifo_marker_and_receipt_refuse_without_blocking(self):
  code="import sys; from pathlib import Path; from hermes_cli.maintenance import hermes_drain_marker_store as s; x=s.MarkerStore(Path(sys.argv[1]),Path(sys.argv[2]),require_authority=lambda:None,require_protocol=lambda:None); exec(\"try:\\n getattr(x,sys.argv[3])()\\n print('unexpected_success')\\nexcept s.Refused as e: print(str(e))\")"
  for base,name,method in [(self.profile,sut.MARKER,'inspect'),(self.backup,'transaction.json','read_receipt')]:
   with self.subTest(name=name):
    target=base/name;os.mkfifo(target,0o600)
    child=subprocess.Popen([sys.executable,'-B','-c',code,str(self.profile),str(self.backup),method],cwd=Path(__file__).resolve().parents[2],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True, encoding="utf-8")
    try:
     out,err=child.communicate(timeout=1)
     self.assertEqual(child.returncode,0,err);self.assertEqual(out.strip(),'unsafe_file')
    finally:
     if child.poll() is None:child.kill();child.communicate(timeout=5)
     target.unlink()
 def test_competing_process_cannot_replace_between_compare_and_unlink(self):
  token=self.create()
  code="import sys; from pathlib import Path; from hermes_cli.maintenance import hermes_drain_marker_store as s; x=s.MarkerStore(Path(sys.argv[1]),Path(sys.argv[2]),require_authority=lambda:None,require_protocol=lambda:None); sys.stdin.readline(); exec(\"try:\\n x.replace_marker(b'peer')\\n print('replaced')\\nexcept s.Refused as e: print(str(e))\")"
  child=subprocess.Popen([sys.executable,'-B','-c',code,str(self.profile),str(self.backup)],cwd=Path(__file__).resolve().parents[2],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True, encoding="utf-8")
  self.addCleanup(lambda:child.kill() if child.poll() is None else None)
  native=sut._read_at;results=[]
  def interleave(fd,name,limit):
   result=native(fd,name,limit)
   if name==sut.MARKER and not results:
    out,err=child.communicate('go\n',timeout=5);results.append(out.strip());self.assertEqual(child.returncode,0,err)
   return result
  with patch.object(sut,'_read_at',side_effect=interleave):
   self.assertEqual(self.store.remove_owned(token,hashlib.sha256(self.data).hexdigest()),'removed')
  self.assertEqual(results,['marker_lock_busy']);self.assertFalse(self.marker.exists())
 def test_absent_remove_retries_directory_durability(self):
  with patch.object(sut,'fsync_dir',wraps=sut.fsync_dir) as sync:
   self.assertEqual(self.store.remove_owned('old','0'*64),'absent')
  self.assertEqual(sync.call_count,1)
 def test_publication_callback_precedes_directory_sync_failure(self):
  native=sut.fsync_dir;tokens=[]
  def fail(fd):
   if self.marker.exists():raise OSError('directory sync')
   return native(fd)
  with patch.object(sut,'fsync_dir',side_effect=fail):
   with self.assertRaises(OSError):self.store.create_marker(self.data,tokens.append)
  self.assertEqual(len(tokens),1);self.assertEqual(self.store.inspect()['token'],tokens[0])
  self.store.remove_owned(tokens[0],hashlib.sha256(self.data).hexdigest())
 def test_callback_failure_preserves_published_marker(self):
  def fail(token):raise OSError('journal failed')
  with self.assertRaises(OSError):self.store.create_marker(self.data,fail)
  self.assertEqual(self.marker.read_bytes(),self.data)
 def test_journal_is_durable_and_rejects_other_owner(self):
  self.assertIsNone(self.store.read_receipt());r=self.receipt();self.store.write_receipt(r)
  self.assertEqual(self.store.read_receipt(),r);r['state']='drain_requested';self.store.write_receipt(r)
  self.assertEqual((self.backup/'transaction.json').stat().st_mode&0o777,0o600)
  peer=dict(r,owner='peer')
  with self.assertRaisesRegex(sut.Refused,'receipt_owner_mismatch'):self.store.write_receipt(peer)
  self.assertEqual(self.store.read_receipt(),r)
 def test_invalid_numeric_journal_never_overwrites(self):
  r=self.receipt();self.store.write_receipt(r)
  with self.assertRaises(sut.Refused):self.store.write_receipt(dict(r,value=float('inf')))
  self.assertEqual(self.store.read_receipt(),r)
 def test_controller_roundtrip_with_real_store(self):
  from hermes_cli.maintenance import hermes_maintenance_controller as c
  from tests.hermes_cli import test_fleet_maintenance_controller as fixture
  clock=fixture.Clock();binding=c.Binding(str(self.profile),55959,178904306568,'a'*40,c.REQUIRED_ROUTES)
  session=c.DrainController(binding,fixture.Authority(),self.store,clock)
  session.begin()
  snapshot={'profile':str(self.profile),'pid':binding.pid,'start_time':binding.start_time,'code_sha':binding.code_sha,'observed_at':clock.wall,'gateway_state':'draining','marker_sha256':session.marker_sha256,'admission_routes':{k:'held' for k in c.REQUIRED_ROUTES},'counters':{k:{'valid':True,'count':0} for k in c.COUNTERS}}
  with session.installation_guard(lambda:snapshot) as check:check()
  session.release();snapshot.update(gateway_state='running',marker_sha256=None);session.verify_resume(snapshot)
  self.assertEqual(self.store.read_receipt()['state'],'resumed');self.assertFalse(self.marker.exists())

if __name__=='__main__':unittest.main()
