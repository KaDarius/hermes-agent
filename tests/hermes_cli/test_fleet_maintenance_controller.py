import pytest
pytestmark = pytest.mark.macos_only
"""Isolated state-machine tests. The adapter is an inert cooperating store,
not proof of a live filesystem CAS or actual gateway/writer exclusion."""
import contextlib
import copy
import hashlib
import unittest
from hermes_cli.maintenance import hermes_maintenance_controller as sut

class Clock:
 def __init__(self):self.mono=100.;self.wall=1000.
 def monotonic(self):return self.mono
 def time(self):return self.wall
 def advance(self,n):self.mono+=n;self.wall+=n

class Authority:
 def __init__(self):self.allowed=True;self.cleanup=True;self.calls=[]
 @contextlib.contextmanager
 def guard(self,binding,owner,purpose,deadline):
  self.calls.append(purpose)
  if not self.allowed or (purpose=='cleanup' and not self.cleanup):raise sut.Refused('authority_unavailable')
  def require_valid():
   if not self.allowed or (purpose=='cleanup' and not self.cleanup):raise sut.Refused('authority_unavailable')
  yield require_valid

class Store:
 """All marker operations are indivisible under the inert fixture authority.
 Simulates opaque publication generations, even if bytes are identical."""
 def __init__(self):self.marker=None;self.journal=None;self.generation=0;self.fail_journal=None;self.publish_fault=None;self.removes=0
 def inspect(self):return copy.deepcopy(self.marker)
 def read_receipt(self):return copy.deepcopy(self.journal)
 def write_receipt(self,value):
  if self.fail_journal==value['state']:raise OSError('journal failure')
  self.journal=copy.deepcopy(value)
 def create_marker(self,data,published):
  if self.marker is not None:raise sut.Refused('marker_exists')
  self.generation+=1;self.marker={'token':str(self.generation),'sha256':hashlib.sha256(data).hexdigest()}
  if self.publish_fault=='before_callback':raise OSError('ambiguous publication')
  published(self.marker['token'])
  if self.publish_fault=='after_callback':raise OSError('publication sync failure')
 def remove_owned(self,token,sha):
  if self.marker is None:return 'absent'
  if self.marker!={'token':token,'sha256':sha}:raise sut.Refused('marker_replaced')
  self.marker=None;self.removes+=1;return 'removed'

class MaintenanceTests(unittest.TestCase):
 def setUp(self):
  self.clock=Clock();self.auth=Authority();self.store=Store()
  self.binding=sut.Binding('/fixture/hermes',55959,178904306568,'a'*40,frozenset(sut.REQUIRED_ROUTES))
  self.session=sut.DrainController(self.binding,self.auth,self.store,self.clock,window=600)
 def snapshot(self,**updates):
  r={'profile':self.binding.profile,'pid':self.binding.pid,'start_time':self.binding.start_time,'code_sha':self.binding.code_sha,'observed_at':self.clock.wall,'gateway_state':'draining','marker_sha256':self.session.marker_sha256,'admission_routes':{k:'held' for k in sut.REQUIRED_ROUTES},'counters':{k:{'valid':True,'count':0} for k in sut.COUNTERS}}
  r.update(updates);return r
 def test_counter_collector_never_turns_read_failure_into_zero(self):
  def fail():raise RuntimeError('private runtime details')
  readers={'messaging':lambda:0,'cron':fail,'api':lambda:False}
  counts=sut.capture_counters(readers)
  self.assertEqual(counts['messaging'],{'valid':True,'count':0})
  self.assertEqual(counts['cron'],{'valid':False,'count':None})
  self.assertEqual(counts['api'],{'valid':False,'count':None})
 def test_installation_context_holds_authority_and_rechecks_at_write(self):
  self.session.begin();held=[];native=self.auth.guard
  @contextlib.contextmanager
  def guard(*args):
   with native(*args) as capability:
    held.append(args[2])
    try:yield capability
    finally:held.pop()
  self.auth.guard=guard
  with self.assertRaisesRegex(sut.Refused,'window_expired'):
   with self.session.installation_guard(self.snapshot) as check:
    self.assertEqual(held,['installation'])
    check();self.clock.advance(601);check()
 def test_invalid_cleanup_clock_refuses_before_removal(self):
  self.session.begin();self.clock.mono=float('nan')
  with self.assertRaisesRegex(sut.Refused,'invalid_clock'):self.session.release()
  self.assertIsNotNone(self.store.marker)
 def test_revoked_authority_after_prepared_receipt_prevents_marker(self):
  native=self.store.write_receipt
  def revoke(value):
   native(value)
   if value['state']=='prepared':self.auth.allowed=False
  self.store.write_receipt=revoke
  with self.assertRaisesRegex(sut.Refused,'authority_unavailable'):self.session.begin()
  self.assertIsNone(self.store.marker)
 def test_revoked_authority_inside_installation_refuses_recheck(self):
  self.session.begin()
  with self.assertRaisesRegex(sut.Refused,'authority_unavailable'):
   with self.session.installation_guard(self.snapshot) as check:
    self.auth.allowed=False;check()
 def test_mutable_route_binding_refused(self):
  with self.assertRaisesRegex(sut.Refused,'incomplete_route_set'):
   sut.Binding('/fixture/hermes',55959,178904306568,'a'*40,set(sut.REQUIRED_ROUTES))
 def test_install_check_cannot_escape_its_capability_context(self):
  self.session.begin()
  with self.session.installation_guard(self.snapshot) as check:check()
  with self.assertRaisesRegex(sut.Refused,'installation_context_closed'):check()
 def test_failed_publication_journal_never_accepts_ready(self):
  self.store.fail_journal='drain_requested'
  with self.assertRaises(OSError):self.session.begin()
  self.assertEqual(self.store.journal['state'],'prepared')
  with self.assertRaisesRegex(sut.Refused,'drain_not_requested'):self.session.require_ready(self.snapshot())
 def test_post_publication_sync_failure_never_accepts_ready(self):
  self.store.publish_fault='after_callback'
  with self.assertRaises(OSError):self.session.begin()
  with self.assertRaisesRegex(sut.Refused,'drain_not_requested'):self.session.require_ready(self.snapshot())
 def test_wall_clock_rollback_does_not_refresh_cached_status(self):
  self.session.begin();cached=self.snapshot()
  self.clock.advance(8);self.clock.wall-=8
  with self.assertRaisesRegex(sut.Refused,'clock_discontinuity'):self.session.require_ready(cached)
 def test_default_live_authority_refuses_before_marker_or_receipt(self):
  session=sut.DrainController(self.binding,sut.LiveAuthority(),self.store,self.clock)
  with self.assertRaisesRegex(sut.Refused,'live_authority_unavailable'):session.begin()
  self.assertIsNone(self.store.marker);self.assertIsNone(self.store.journal)
 def test_begin_ready_release_requires_resume_readback(self):
  self.session.begin();self.session.require_ready(self.snapshot());self.session.release()
  self.assertEqual(self.store.journal['state'],'marker_removed_resume_unverified')
  self.session.verify_resume(self.snapshot(gateway_state='running',marker_sha256=None))
  self.assertEqual(self.store.journal['state'],'resumed');self.assertEqual(self.store.removes,1)
 def test_wrong_generation_unacknowledged_stale_future_and_missing_counter_refuse(self):
  self.session.begin()
  for change,reason in [({'pid':2},'process_identity'),({'start_time':1},'process_identity'),({'code_sha':'b'*40},'process_identity'),({'marker_sha256':'0'*64},'marker_not_acknowledged'),({'observed_at':self.clock.wall-6},'stale_status'),({'observed_at':self.clock.wall+2},'future_status'),({'counters':{'cron':{'valid':True,'count':0}}},'invalid_counters')]:
   with self.subTest(change=change),self.assertRaisesRegex(sut.Refused,reason):self.session.require_ready(self.snapshot(**change))
 def test_counter_failure_bool_negative_and_busy_refuse(self):
  self.session.begin()
  for valid,count,reason in [(False,0,'unreadable_counter'),(True,False,'invalid_counter'),(True,-1,'invalid_counter'),(True,1,'active_work')]:
   snap=self.snapshot();snap['counters']['cron']={'valid':valid,'count':count}
   with self.subTest(count=count,valid=valid),self.assertRaisesRegex(sut.Refused,reason):self.session.require_ready(snap)
 def test_internal_or_external_route_not_held_refuses_even_with_zero_counts(self):
  self.session.begin()
  for route in ('internal_recovery','external_cron','queued_workers','drain_marker_writers'):
   snap=self.snapshot();snap['admission_routes'][route]='unknown'
   with self.subTest(route=route),self.assertRaisesRegex(sut.Refused,'admission_not_held'):self.session.require_ready(snap)
 def test_readiness_revalidates_authority_and_owned_generation(self):
  self.session.begin();self.auth.allowed=False
  with self.assertRaisesRegex(sut.Refused,'authority_unavailable'):self.session.require_ready(self.snapshot())
  self.auth.allowed=True;self.store.marker['token']='peer'
  with self.assertRaisesRegex(sut.Refused,'marker_replaced'):self.session.require_ready(self.snapshot())
 def test_existing_marker_refused_and_preserved(self):
  self.store.marker={'token':'peer','sha256':'f'*64};before=copy.deepcopy(self.store.marker)
  with self.assertRaisesRegex(sut.Refused,'marker_exists'):self.session.begin()
  self.assertEqual(self.store.marker,before)
 def test_replaced_marker_never_removed_even_when_bytes_equal(self):
  self.session.begin();self.store.marker['token']='peer'
  with self.assertRaisesRegex(sut.Refused,'marker_replaced'):self.session.release()
  self.assertIsNotNone(self.store.marker);self.assertEqual(self.store.removes,0)
 def test_post_publication_failure_has_recoverable_owned_receipt(self):
  self.store.publish_fault='after_callback'
  with self.assertRaises(OSError):self.session.begin()
  self.assertEqual(self.store.journal['state'],'drain_requested')
  sut.DrainController.recover_release(self.binding,self.auth,self.store,self.clock)
  self.assertIsNone(self.store.marker)
 def test_publication_before_ownership_callback_stays_ambiguous(self):
  self.store.publish_fault='before_callback'
  with self.assertRaises(OSError):self.session.begin()
  with self.assertRaisesRegex(sut.Refused,'publication_ambiguous'):sut.DrainController.recover_release(self.binding,self.auth,self.store,self.clock)
  self.assertIsNotNone(self.store.marker)
 def test_final_journal_failure_can_resume_cleanup_without_second_delete(self):
  self.session.begin();self.store.fail_journal='marker_removed_resume_unverified'
  with self.assertRaises(OSError):self.session.release()
  self.assertIsNone(self.store.marker);self.store.fail_journal=None
  sut.DrainController.recover_release(self.binding,self.auth,self.store,self.clock)
  self.assertEqual(self.store.removes,1);self.assertEqual(self.store.journal['state'],'marker_removed_resume_unverified')
 def test_expired_operation_cannot_install_and_needs_separate_cleanup_authority(self):
  self.session.begin();self.clock.advance(601)
  with self.assertRaisesRegex(sut.Refused,'window_expired'):self.session.require_ready(self.snapshot())
  self.auth.cleanup=False
  with self.assertRaisesRegex(sut.Refused,'authority_unavailable'):self.session.release()
  self.assertIsNotNone(self.store.marker);self.auth.cleanup=True;self.session.release();self.assertIsNone(self.store.marker)
 def test_prepared_receipt_failure_prevents_publication(self):
  self.store.fail_journal='prepared'
  with self.assertRaises(OSError):self.session.begin()
  self.assertIsNone(self.store.marker)
 def test_recovery_rejects_other_profile_or_process(self):
  self.session.begin()
  other=sut.Binding('/fixture/other',self.binding.pid,self.binding.start_time,self.binding.code_sha,self.binding.routes)
  with self.assertRaisesRegex(sut.Refused,'receipt_binding'):sut.DrainController.recover_release(other,self.auth,self.store,self.clock)
  self.assertIsNotNone(self.store.marker)

if __name__=='__main__':unittest.main()
