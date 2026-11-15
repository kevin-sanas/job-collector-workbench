import tempfile
import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from app.backend_sync import BackendSyncClient
from app.config import Settings
from app.models import Job
from app.storage import Repository


class BackendSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Repository(Path(self.temp.name) / 'jobs.sqlite3')
        self.settings = Settings(backend_url='http://127.0.0.1:8080', backend_token='unit-test-token')
        self.job = Job(source='51job', source_job_id='test-123', title='Java 实习', company_name='示例公司', description='熟悉 Java')
        self.repo.upsert(self.job)
        self.dictionary_path = Path(self.temp.name) / 'dictionary.json'
        self.dictionary_path.write_text(json.dumps({'dictionary_version': 'sync-test.1', 'terms': [
            {'tech_key': 'java', 'display_name': 'Java', 'aliases': ['Java'], 'category': '语言', 'enabled': True, 'match_mode': 'token'},
            {'tech_key': 'python', 'display_name': 'Python', 'aliases': ['Python'], 'category': '语言', 'enabled': True, 'match_mode': 'token'},
            {'tech_key': 'ai', 'display_name': '人工智能', 'aliases': ['AI'], 'category': '方向', 'enabled': True, 'match_mode': 'review'},
        ]}), encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def session(self):
        session = MagicMock()
        session.__enter__.return_value = session
        session.post.return_value.json.return_value = {'code': 200, 'data': {'batchId': 'server-batch'}}
        session.post.return_value.status_code = 200
        return session

    def client(self):
        return BackendSyncClient(self.repo, self.settings, self.dictionary_path)

    def test_success_keeps_stable_keys_and_does_not_submit_same_run_twice(self):
        session = self.session()
        with patch('app.backend_sync.requests.Session', return_value=session):
            client = self.client()
            first = client.push_jobs([self.job], 'task-1')
            second = client.push_jobs([self.job], 'task-1')
        self.assertEqual('SENT', first['status'])
        self.assertEqual('SENT', second['status'])
        self.assertEqual(2, session.post.call_count)
        sent = session.post.call_args_list[0].kwargs['json']['jobs'][0]
        self.assertEqual(self.job.record_key, sent['record_key'])
        self.assertEqual(self.job.content_hash, sent['content_hash'])
        self.assertNotIn('unit-test-token', str(sent))
        self.assertFalse(session.trust_env)
        self.assertEqual('SENT', second['technology_sync']['status'])
        evidence = session.post.call_args_list[1].kwargs['json']
        self.assertEqual(evidence['batch']['stats']['scanned_jobs'], 1)
        self.assertEqual([item['tech_key'] for item in evidence['batch']['job_mentions']], ['java'])
        self.assertTrue(all(item['review_state'] == 'pending' for item in evidence['batch']['job_mentions']))

    def test_connection_failure_is_persisted_and_manual_retry_uses_original_payload(self):
        session = self.session()
        session.post.side_effect = requests.ConnectionError('offline')
        with patch('app.backend_sync.requests.Session', return_value=session):
            client = self.client()
            result = client.push_jobs([self.job], 'task-offline')
        self.assertEqual('FAILED', result['status'])
        self.assertEqual(self.job.record_key, self.repo.query()[0].record_key)
        other = self.client()
        self.assertEqual('FAILED', other._record('JOBS', 'task-offline')['status'])
        original = other._record('JOBS', 'task-offline')['payload_hash']
        success = self.session()
        with patch('app.backend_sync.requests.Session', return_value=success):
            result = other.retry('JOBS', 'task-offline')
        self.assertEqual('SENT', result['status'])
        self.assertEqual(original, other._record('JOBS', 'task-offline')['payload_hash'])
        self.assertEqual(2, success.post.call_count)

    def test_transport_configuration_rejects_credentials_embedded_in_url(self):
        self.settings.backend_url = 'http://user:password@127.0.0.1:8080?token=secret'
        client = self.client()
        with self.assertRaises(ValueError):
            client.push_jobs([self.job], 'bad-url')

    def test_cancelled_or_failed_collection_never_posts(self):
        with patch('app.backend_sync.requests.Session') as session:
            client = self.client()
            self.assertIsNone(client.push_task({'id': 'cancelled', 'status': 'cancelled'}))
            self.assertIsNone(client.push_task({'id': 'failed', 'status': 'failed'}))
            session.assert_not_called()

    def test_automatic_evidence_only_scans_this_batch_and_keeps_review_issues(self):
        self.repo.upsert(Job(source='boss', source_job_id='other', title='Python 工程师', company_name='其他公司'))
        job = Job(source='51job', source_job_id='current', title='Java AI 工程师', company_name='示例公司')
        session = self.session()
        with patch('app.backend_sync.requests.Session', return_value=session):
            self.client().push_jobs([job], 'current-only')
        payload = session.post.call_args_list[1].kwargs['json']['batch']
        self.assertEqual(payload['stats']['scanned_jobs'], 1)
        self.assertEqual([item['tech_key'] for item in payload['job_mentions']], ['java'])
        self.assertEqual(payload['issues'][0]['candidate_tech_keys'], ['ai'])
        self.assertEqual(payload['job_mentions'][0]['record_key'], job.record_key)

    def test_failed_jobs_wait_then_retry_uses_original_evidence_and_dictionary(self):
        client = self.client()
        failed = self.session()
        failed.post.side_effect = requests.ConnectionError('offline')
        with patch('app.backend_sync.requests.Session', return_value=failed):
            result = client.push_jobs([self.job], 'original-payload')
        self.assertEqual(failed.post.call_count, 1)
        child = result['technology_sync']['run_id']
        saved = client._record('DISCOVERY', child)
        self.assertEqual(saved['attempts'], 0)
        changed = Job(source='51job', source_job_id='test-123', title='Python 工程师', company_name='示例公司', description='使用 Python')
        self.repo.upsert(changed)
        self.dictionary_path.write_text('invalid after queueing', encoding='utf-8')
        success = self.session()
        with patch('app.backend_sync.requests.Session', return_value=success):
            retried = self.client().retry('JOBS', 'original-payload')
        self.assertEqual(retried['status'], 'SENT')
        self.assertEqual(success.post.call_count, 2)
        evidence = success.post.call_args_list[1].kwargs['json']
        self.assertEqual(evidence['dictionary']['dictionary_version'], 'sync-test.1')
        self.assertEqual(evidence['batch']['job_mentions'][0]['tech_key'], 'java')
        self.assertEqual(evidence['batch']['job_mentions'][0]['job_content_hash'], hashlib.sha256(json.dumps(
            {'description': self.job.description, 'title': self.job.title}, ensure_ascii=False,
            sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest())
        self.assertEqual(client._record('DISCOVERY', child)['payload'], saved['payload'])

    def test_evidence_failure_has_independent_retry_without_resending_jobs(self):
        client = self.client()
        session = self.session()
        session.post.side_effect = [session.post.return_value, requests.ConnectionError('evidence offline')]
        with patch('app.backend_sync.requests.Session', return_value=session):
            result = client.push_jobs([self.job], 'split-result')
        self.assertEqual(result['status'], 'SENT')
        self.assertEqual(result['technology_sync']['status'], 'FAILED')
        child = result['technology_sync']['run_id']
        original = client._record('DISCOVERY', child)['payload']
        success = self.session()
        with patch('app.backend_sync.requests.Session', return_value=success):
            client.push_jobs([self.job], 'split-result')
            success.post.assert_not_called()
            result = self.client().retry('DISCOVERY', child)
            self.client().retry('DISCOVERY', child)
        self.assertEqual(result['status'], 'SENT')
        self.assertEqual(success.post.call_count, 1)
        self.assertTrue(success.post.call_args.args[0].endswith('/collector/ingest/discovery'))
        self.assertEqual(client._record('DISCOVERY', child)['payload'], original)

    def test_recovery_finishes_staged_evidence_when_jobs_already_succeeded(self):
        client = self.client()
        session = self.session()
        original_retry = client.retry
        def stop_before_evidence(kind, run_id):
            if kind == 'DISCOVERY':
                raise RuntimeError('simulated interruption')
            return original_retry(kind, run_id)
        with patch('app.backend_sync.requests.Session', return_value=session), patch.object(client, 'retry', side_effect=stop_before_evidence):
            with self.assertRaisesRegex(RuntimeError, 'simulated interruption'):
                client.push_jobs([self.job], 'interrupted')
        self.assertEqual(client._record('JOBS', 'interrupted')['status'], 'SENT')
        self.assertEqual(session.post.call_count, 1)
        recovered = self.session()
        with patch('app.backend_sync.requests.Session', return_value=recovered):
            result = self.client().push_jobs([self.job], 'interrupted')
        self.assertEqual(result['technology_sync']['status'], 'SENT')
        self.assertEqual(recovered.post.call_count, 1)
        self.assertTrue(recovered.post.call_args.args[0].endswith('/collector/ingest/discovery'))

    def test_legacy_sent_jobs_generate_evidence_from_saved_payload(self):
        client = self.client()
        payload = {'schema_version': 'collector-jobs.v0.1', 'run_id': 'legacy',
                   'generated_at': self.job.first_seen_at,
                   'jobs': [{**self.job.to_dict(), 'record_key': self.job.record_key, 'content_hash': self.job.content_hash}]}
        data = json.dumps(payload)
        with self.repo.connect() as connection:
            connection.execute('INSERT INTO backend_sync_outbox(kind,run_id,payload,payload_hash,status,updated_at) VALUES (?,?,?,?,?,?)',
                               ('JOBS', 'legacy', data, hashlib.sha256(data.encode()).hexdigest(), 'SENT', self.job.first_seen_at))
        session = self.session()
        with patch('app.backend_sync.requests.Session', return_value=session):
            result = client.retry('JOBS', 'legacy')
        self.assertEqual(session.post.call_count, 1)
        self.assertEqual(result['technology_sync']['status'], 'SENT')
        self.assertEqual(session.post.call_args.kwargs['json']['batch']['job_mentions'][0]['record_key'], self.job.record_key)

    def test_jobs_without_matches_or_source_identity_do_not_send_empty_batches(self):
        session = self.session()
        jobs = [Job(title='Java 工程师', company_name='示例公司'),
                Job(source='boss', source_job_id='unknown-tech', title='文员', company_name='示例公司')]
        with patch('app.backend_sync.requests.Session', return_value=session):
            result = self.client().push_jobs(jobs, 'no-evidence')
        self.assertEqual(result['status'], 'SENT')
        self.assertNotIn('technology_sync', result)
        self.assertEqual(session.post.call_count, 1)


if __name__ == '__main__':
    unittest.main()
