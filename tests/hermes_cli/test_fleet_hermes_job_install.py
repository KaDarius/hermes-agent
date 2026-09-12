import pytest
pytestmark = pytest.mark.macos_only
import contextlib
import copy
import fcntl
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from hermes_cli.maintenance import hermes_job_install as sut


class Backend:
    """Inert supported-writer fixture; transaction must guard its save boundary."""
    def __init__(self, profile):
        self.profile = profile
        self._jobs_lock = contextlib.nullcontext
        self.load_jobs = lambda: json.loads((profile/'cron/jobs.json').read_text(encoding="utf-8"))['jobs']
        self.save_jobs = self._save
        self.calls = 0
        self.drift = False
    def _save(self, rows):
        (self.profile/'cron/jobs.json').write_text(json.dumps({'jobs':rows,'updated_at':'writer-time'}), encoding="utf-8")
    def update_job(self, job_id, updates):
        with self._jobs_lock():
            rows = self.load_jobs()
            row = next(r for r in rows if r['id']==job_id)
            row.update(updates)
            row['provider_snapshot'] = None
            row['model_snapshot'] = None
            if self.drift: rows[1]['prompt']='clobbered'
            self.calls += 1
            self.save_jobs(rows)
            return copy.deepcopy(row)


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.profile=self.root/'profile';self.cron=self.profile/'cron';self.cron.mkdir(parents=True)
        self.scripts=self.profile/'scripts';self.scripts.mkdir()
        self.target={'id':'8417d6710834','name':'daily-command-hub','script':None,'no_agent':False,'enabled':True,'state':'scheduled','next_run_at':'2099-09-12T09:00:00-05:00','schedule':{'kind':'cron','expr':'0 9 * * *'},'provider':'nous','model':'deepseek/deepseek-chat','provider_snapshot':None,'model_snapshot':None}
        self.other={'id':'other','prompt':'must survive','enabled':True}
        self.store=self.cron/'jobs.json';self.store.write_text(json.dumps({'jobs':[self.target,self.other],'updated_at':'before'}), encoding="utf-8");self.store.chmod(0o600)
        self.backend=Backend(self.profile)
        self.expected=sut.digest(self.target)
        self.payloads={'hermes_daily_command_hub.py':b'print("fixture only")\n','daily-command-hub-config.json':b'{"fixture":true}\n'}
        self.backup=self.root/'backup'
    def apply(self, **kw):
        return sut.apply(self.profile,self.backend,self.expected,self.payloads,self.backup,lambda:None,**kw)
    def rows(self):return json.loads(self.store.read_text(encoding="utf-8"))['jobs']
    def test_nonfinite_peer_number_refused_before_mutation(self):
        raw=json.dumps({'jobs':[self.target,self.other]}).replace('"must survive"','1e999')
        self.store.write_text(raw, encoding="utf-8")
        with self.assertRaisesRegex(sut.Refused,'nonfinite_json_number'):self.apply()
        self.assertEqual(self.store.read_text(encoding="utf-8"),raw);self.assertFalse(self.backup.exists())
    def test_directory_sync_failure_cleans_published_files(self):
        native=sut._fsync_dir
        def fail_scripts(path):
            if path==self.scripts:raise OSError('directory sync failure')
            return native(path)
        with patch.object(sut,'_fsync_dir',side_effect=fail_scripts):
            with self.assertRaises(OSError):self.apply()
        self.assertEqual(self.rows()[0],self.target)
        self.assertFalse((self.scripts/'hermes_daily_command_hub.py').exists())
    def test_rollback_cleanup_can_resume_after_unlink_failure(self):
        self.apply();native=Path.unlink;dest=self.scripts/'daily-command-hub-config.json'
        def fail_one(path,*args,**kw):
            if path==dest:raise OSError('unlink failed')
            return native(path,*args,**kw)
        with patch.object(Path,'unlink',fail_one):
            with self.assertRaises(OSError):sut.rollback(self.profile,self.backend,self.backup,lambda:None)
        self.assertFalse(self.rows()[0]['no_agent'])
        sut.rollback(self.profile,self.backend,self.backup,lambda:None)
        self.assertFalse(dest.exists())
    def test_whitespace_provider_pin_refuses(self):
        row=dict(self.target,provider='  ');self.backend._save([row,self.other]);self.expected=sut.digest(row)
        with self.assertRaisesRegex(sut.Refused,'unpinned_provider'):self.apply()
        self.assertEqual(self.backend.calls,0)

    def test_post_link_temporary_cleanup_failure_tracks_owned_destination(self):
        native=sut.os.unlink
        def fail_temp(path,*args,**kwargs):
            if Path(path).name.startswith('.hermes-install-'):raise OSError('temporary cleanup failed')
            return native(path,*args,**kwargs)
        with patch.object(sut.os,'unlink',side_effect=fail_temp):
            with self.assertRaises(OSError):self.apply()
        self.assertEqual(self.backend.calls,0)
        self.assertFalse((self.scripts/'hermes_daily_command_hub.py').exists())
    def test_peer_destination_created_at_publication_is_preserved(self):
        native=sut.os.link
        def peer_wins(src,dst,**kw):
            Path(dst).write_bytes(b'peer');return native(src,dst,**kw)
        with patch.object(sut.os,'link',side_effect=peer_wins):
            with self.assertRaises(FileExistsError):self.apply()
        self.assertEqual((self.scripts/'hermes_daily_command_hub.py').read_bytes(),b'peer')
        self.assertEqual(self.backend.calls,0)
    def test_reverse_native_post_write_failure_can_resume(self):
        self.apply();native=self.backend.save_jobs
        def fail(rows):native(rows);raise OSError('after reverse write')
        self.backend.save_jobs=fail
        with self.assertRaises(OSError):sut.rollback(self.profile,self.backend,self.backup,lambda:None)
        self.backend.save_jobs=native
        self.assertEqual(json.loads((self.backup/'transaction.json').read_text(encoding="utf-8"))['status'],'rollback_started')
        sut.rollback(self.profile,self.backend,self.backup,lambda:None)
        self.assertFalse((self.scripts/'daily-command-hub-config.json').exists())
    def test_reverse_directory_or_final_receipt_failure_can_resume(self):
        for failure in ('directory','receipt'):
            with self.subTest(failure=failure):
                if self.backup.exists():
                    import shutil
                    shutil.rmtree(self.backup)
                self.backend._save([self.target,self.other]);self.apply()
                native_sync=sut._fsync_dir;native_journal=sut._journal
                def sync(path):
                    if failure=='directory' and path==self.scripts:raise OSError('sync failed')
                    return native_sync(path)
                def journal(path,value):
                    if failure=='receipt' and value['status']=='rolled_back':raise OSError('receipt failed')
                    return native_journal(path,value)
                with patch.object(sut,'_fsync_dir',side_effect=sync),patch.object(sut,'_journal',side_effect=journal):
                    with self.assertRaises(OSError):sut.rollback(self.profile,self.backend,self.backup,lambda:None)
                sut.rollback(self.profile,self.backend,self.backup,lambda:None)
                self.assertEqual(json.loads((self.backup/'transaction.json').read_text(encoding="utf-8"))['status'],'rolled_back')

    def test_final_receipt_post_replace_sync_failure_can_resume(self):
        self.apply();native=sut._fsync_dir
        def sync(path):
            if path==self.backup and json.loads((self.backup/'transaction.json').read_text(encoding="utf-8"))['status']=='rolled_back':raise OSError('post replace sync')
            return native(path)
        with patch.object(sut,'_fsync_dir',side_effect=sync):
            with self.assertRaises(OSError):sut.rollback(self.profile,self.backend,self.backup,lambda:None)
        calls=self.backend.calls
        result=sut.rollback(self.profile,self.backend,self.backup,lambda:None)
        self.assertEqual(result['status'],'rolled_back');self.assertEqual(self.backend.calls,calls)

    def test_real_execution_store_requires_known_idle_state(self):
        import sqlite3
        with self.assertRaisesRegex(sut.Refused,'execution_store_unavailable'):
            sut.require_idle_execution_store(self.profile)
        db=self.cron/'executions.db'
        with sqlite3.connect(db) as c:
            c.execute('CREATE TABLE executions (job_id TEXT, status TEXT)')
            c.execute('INSERT INTO executions VALUES (?,?)',(sut.JOB_ID,'completed'))
            c.execute('INSERT INTO executions VALUES (?,?)',('other','running'))
        before=db.read_bytes();sut.require_idle_execution_store(self.profile);self.assertEqual(db.read_bytes(),before)
        for status in ['claimed','running']:
            with self.subTest(status=status):
                with sqlite3.connect(db) as c:c.execute('UPDATE executions SET status=? WHERE job_id=?',(status,sut.JOB_ID))
                with self.assertRaisesRegex(sut.Refused,'active_execution'):
                    sut.require_idle_execution_store(self.profile)

    def test_forward_and_reverse_preserve_other_jobs_and_runtime_progress(self):
        result=self.apply();self.assertEqual(result['status'],'installed');self.assertEqual(self.rows()[1],self.other)
        self.assertTrue(self.rows()[0]['no_agent']);self.assertEqual(self.rows()[0]['script'],'hermes_daily_command_hub.py')
        self.assertEqual(self.backup.stat().st_mode&0o777,0o700)
        rows=self.rows();rows[1]['new_note']='later peer work';rows[0]['last_run_at']='later';self.backend._save(rows)
        sut.rollback(self.profile,self.backend,self.backup,lambda:None)
        rows=self.rows();self.assertFalse(rows[0]['no_agent']);self.assertEqual(rows[0]['last_run_at'],'later');self.assertEqual(rows[1]['new_note'],'later peer work')
        self.assertFalse((self.scripts/'hermes_daily_command_hub.py').exists())
    def test_busy_real_filesystem_lock_refuses_without_any_change(self):
        lock=self.cron/'.jobs.lock'
        with lock.open('w') as f:
            fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaisesRegex(sut.Refused,'lock_busy'):self.apply()
        self.assertEqual(self.rows()[0],self.target);self.assertFalse(self.backup.exists());self.assertEqual(self.backend.calls,0)
    def test_any_existing_claim_refuses_even_if_old(self):
        for name in ['fire_claim','run_claim']:
            with self.subTest(name=name):
                row=dict(self.target,**{name:{'at':'2000-01-01T00:00:00Z','by':'unknown'}})
                self.store.write_text(json.dumps({'jobs':[row,self.other]}), encoding="utf-8");self.expected=sut.digest(row)
                with self.assertRaisesRegex(sut.Refused,'in_flight_claim'):self.apply()
        self.assertEqual(self.backend.calls,0)
    def test_active_execution_refuses(self):
        def busy():raise sut.Refused('active_execution')
        with self.assertRaisesRegex(sut.Refused,'active_execution'):
            sut.apply(self.profile,self.backend,self.expected,self.payloads,self.backup,busy)
        self.assertEqual(self.backend.calls,0);self.assertFalse(self.backup.exists())
    def test_duplicate_json_and_wrong_shape_never_repair(self):
        for raw in ['{"jobs":[],"jobs":[]}','[]','{"jobs":[{"id":"x"},{"id":"x"}]}','{"jobs":[],"new_field":1}']:
            self.store.write_text(raw, encoding="utf-8")
            with self.assertRaises(sut.Refused):self.apply()
            self.assertEqual(self.store.read_text(encoding="utf-8"),raw)
        self.assertEqual(self.backend.calls,0)
    def test_target_drift_refuses(self):
        rows=self.rows();rows[0]['no_agent']=True;self.backend._save(rows)
        with self.assertRaisesRegex(sut.Refused,'target_drift'):self.apply()
        self.assertEqual(self.backend.calls,0)
    def test_existing_install_file_is_not_overwritten(self):
        p=self.scripts/'hermes_daily_command_hub.py';p.write_bytes(b'peer')
        with self.assertRaisesRegex(sut.Refused,'destination_exists'):self.apply()
        self.assertEqual(p.read_bytes(),b'peer');self.assertEqual(self.backend.calls,0)
    def test_symlink_destination_and_store_refused(self):
        p=self.scripts/'hermes_daily_command_hub.py';p.symlink_to(self.root/'absent')
        with self.assertRaisesRegex(sut.Refused,'destination_exists'):self.apply()
        p.unlink();raw=self.store.read_bytes();self.store.unlink();outside=self.root/'outside';outside.write_bytes(raw);self.store.symlink_to(outside)
        with self.assertRaisesRegex(sut.Refused,'unsafe_path'):self.apply()
        self.assertEqual(self.backend.calls,0)
    def test_writer_unexpected_delta_is_refused_before_persistence(self):
        self.backend.drift=True
        with self.assertRaisesRegex(sut.Refused,'unexpected_writer_delta'):self.apply()
        self.assertEqual(self.rows(),[self.target,self.other]);self.assertFalse((self.scripts/'hermes_daily_command_hub.py').exists())
    def test_write_failure_restores_own_files_without_calling_writer(self):
        original=sut.install_new
        calls=[]
        def fail_second(p,data,on_publish):
            calls.append(p)
            if len(calls)==2:raise OSError('simulated')
            original(p,data,on_publish)
        with patch.object(sut,'install_new',side_effect=fail_second):
            with self.assertRaises(OSError):self.apply()
        self.assertEqual(self.backend.calls,0);self.assertFalse((self.scripts/'hermes_daily_command_hub.py').exists())
    def test_rollback_refuses_later_configuration_edit(self):
        self.apply();rows=self.rows();rows[0]['workdir']='/a/new/owner/path';self.backend._save(rows)
        with self.assertRaisesRegex(sut.Refused,'configuration_drift'):
            sut.rollback(self.profile,self.backend,self.backup,lambda:None)
        self.assertTrue(self.rows()[0]['no_agent'])

    def test_rollback_refuses_changed_installed_file_or_configuration(self):
        self.apply();p=self.scripts/'hermes_daily_command_hub.py';p.write_bytes(b'peer edited')
        with self.assertRaisesRegex(sut.Refused,'installed_file_drift'):sut.rollback(self.profile,self.backend,self.backup,lambda:None)
        self.assertTrue(self.rows()[0]['no_agent']);self.assertEqual(p.read_bytes(),b'peer edited')
    def test_uncertain_writer_failure_does_not_erase_installed_dependencies(self):
        native=self.backend.save_jobs
        def write_then_fail(rows):native(rows);raise OSError('after replace')
        self.backend.save_jobs=write_then_fail
        with self.assertRaisesRegex(sut.Refused,'write_outcome_requires_reconciliation'):self.apply()
        self.assertTrue(self.rows()[0]['no_agent']);self.assertTrue((self.scripts/'hermes_daily_command_hub.py').exists())

if __name__=='__main__':unittest.main()
