"""本地数据成功保存后提交后台；发送失败只改变同步记录，不改变采集结果。"""
import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import requests

from app.discovery.batches import DiscoveryBatch
from app.config import ROOT
from app.models import Job, now
from app.storage import Repository


class BackendSyncClient:
    def __init__(self, repository: Repository, settings, dictionary_path=None):
        self.repository = repository
        self.settings = settings
        self.dictionary_path = Path(dictionary_path) if dictionary_path is not None else ROOT / 'config' / 'discovery' / 'tech_dictionary.json'
        with repository.connect() as connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS backend_sync_outbox (
                    kind TEXT NOT NULL, run_id TEXT NOT NULL, payload TEXT NOT NULL,
                    payload_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING',
                    attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
                    receipt TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL,
                    PRIMARY KEY(kind, run_id)
                );
            ''')

    @staticmethod
    def validate_configuration(settings):
        base = settings.backend_url.strip().rstrip('/')
        parsed = urlparse(base)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ValueError('后台地址必须是 HTTP/HTTPS 服务地址，不能包含账号、凭据或查询参数')
        if parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
            raise ValueError('远程后台请使用 HTTPS；本机可使用 http://127.0.0.1:8080')
        if not settings.backend_token.strip():
            raise ValueError('请先配置后台对接凭据')
        return base, settings.backend_token.strip()

    def _request(self, path, payload=None):
        base, token = self.validate_configuration(self.settings)
        try:
            with requests.Session() as session:
                session.trust_env = False
                session.headers.update({'X-Collector-Token': token})
                if payload is None:
                    response = session.get(base + path, timeout=(3, 20))
                else:
                    response = session.post(base + path, json=payload, timeout=(3, 30))
                response.raise_for_status()
                body = response.json()
        except requests.RequestException:
            raise ValueError('后台连接或请求失败，请确认本地服务已启动；待提交数据仍保存在本机') from None
        except ValueError:
            raise ValueError('后台返回了无法识别的数据，请核对服务地址') from None
        if not isinstance(body, dict) or body.get('code') != 200 or not isinstance(body.get('data'), dict):
            message = body.get('msg', '') if isinstance(body, dict) else ''
            raise ValueError(str(message)[:300] or '后台未接受此批次，请检查版本和对接凭据')
        return body['data']

    def check_connection(self):
        data = self._request('/collector/ingest/status')
        if not {'collector-jobs.v0.1', 'discovery-batch.v0.1'}.issubset(data.get('schemas', [])):
            raise ValueError('后台版本尚未支持当前岗位与发现批次')
        return data

    def push_jobs(self, jobs: list[Job], run_id: str | None = None):
        run_id = run_id or 'history-' + str(uuid4())
        existing = self._record('JOBS', run_id)
        if existing is not None:
            return self.retry('JOBS', run_id)
        if not jobs or len(jobs) > 1000:
            raise ValueError('每批提交 1 至 1000 条岗位；当前没有岗位或数量超过单批限制')
        payload = {
            'schema_version': 'collector-jobs.v0.1', 'run_id': run_id, 'generated_at': now(),
            'jobs': [{**job.to_dict(), 'record_key': job.record_key, 'content_hash': job.content_hash} for job in jobs],
        }
        return self.submit('JOBS', run_id, payload)

    def push_task(self, task: dict):
        if task.get('status') != 'completed':
            return None
        with self.repository.connect() as connection:
            values = connection.execute('SELECT j.payload FROM jobs j JOIN job_tasks t ON t.record_key=j.record_key WHERE t.task_id=? ORDER BY j.record_key', (task['id'],)).fetchall()
        if not values:
            return None
        return self.push_jobs([Job(**json.loads(row[0])) for row in values], 'task-' + task['id'])

    def push_discovery(self, batch: DiscoveryBatch, dictionary: dict | None = None):
        payload = {'batch': batch.to_dict()}
        if dictionary is not None:
            if dictionary.get('dictionary_version') != batch.dictionary_version:
                raise ValueError('提交的词典快照与发现运行版本不一致')
            payload['dictionary'] = dictionary
        return self.submit('DISCOVERY', batch.run_id, payload)

    def submit(self, kind, run_id, payload):
        self.validate_configuration(self.settings)
        if kind not in ('JOBS', 'DISCOVERY'):
            raise ValueError('不支持的提交类型')
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        digest = hashlib.sha256(data.encode('utf-8')).hexdigest()
        with self.repository.connect() as connection:
            existing = connection.execute('SELECT payload_hash FROM backend_sync_outbox WHERE kind=? AND run_id=?', (kind, run_id)).fetchone()
            if existing and existing[0] != digest:
                raise ValueError('同一批次内容已变化，请生成新批次；原发送记录保留')
            connection.execute('INSERT OR IGNORE INTO backend_sync_outbox(kind,run_id,payload,payload_hash,updated_at) VALUES (?,?,?,?,?)', (kind, run_id, data, digest, now()))
            if kind == 'JOBS':
                self._stage_job_technologies(connection, run_id, payload)
        return self.retry(kind, run_id)

    def _stage_job_technologies(self, connection, run_id, payload):
        """Persist evidence beside its immutable jobs payload before either request."""
        technology_run_id = 'jobs-tech-' + hashlib.sha256(run_id.encode('utf-8')).hexdigest()
        if connection.execute('SELECT 1 FROM backend_sync_outbox WHERE kind=? AND run_id=?', ('DISCOVERY', technology_run_id)).fetchone():
            return
        from app.discovery import JOB_TECH_EXTRACTOR_VERSION
        from app.discovery.batches import build_job_tech_batch
        from app.discovery.dictionary import build_alias_index, load_dictionary
        from app.discovery.job_extractor import extract_job_technologies
        from app.discovery.models import DiscoveryRun
        # The existing discovery contract requires a public source identity.
        jobs = [Job.from_mapping(value) for value in payload['jobs'] if value.get('source_job_id') or value.get('source_url')]
        if not jobs:
            return
        dictionary = load_dictionary(self.dictionary_path)
        run = DiscoveryRun(technology_run_id, 'job_tech', 'running', payload['generated_at'], '',
                           dictionary.dictionary_version, JOB_TECH_EXTRACTOR_VERSION,
                           tuple(job.record_key for job in jobs))
        result = extract_job_technologies(jobs, dictionary, build_alias_index(dictionary), run)
        if not result.mentions and not result.issues:
            return
        run = replace(run, status='completed', completed_at=payload['generated_at'], stats=result.stats)
        batch = build_job_tech_batch(run, list(result.mentions), list(result.issues))
        data = json.dumps({'batch': batch.to_dict(), 'dictionary': dictionary.to_dict()},
                          ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        connection.execute('''INSERT INTO backend_sync_outbox(kind,run_id,payload,payload_hash,status,error,updated_at)
            VALUES (?,?,?,?,?,?,?)''', ('DISCOVERY', technology_run_id, data,
            hashlib.sha256(data.encode('utf-8')).hexdigest(), 'FAILED', '等待原始岗位成功提交后发送技术证据', now()))

    def retry(self, kind, run_id):
        record = self._record(kind, run_id)
        if record is None:
            raise ValueError('本地发送记录不存在')
        if kind == 'JOBS':
            # Older outbox rows also use their saved payload, never today's jobs.
            with self.repository.connect() as connection:
                self._stage_job_technologies(connection, run_id, json.loads(record['payload']))
        if record['status'] != 'SENT':
            status, error, receipt = 'SENT', '', {}
            try:
                receipt = self._request('/collector/ingest/' + ('jobs' if kind == 'JOBS' else 'discovery'), json.loads(record['payload']))
            except ValueError as exception:
                status, error = 'FAILED', str(exception)
            with self.repository.connect() as connection:
                connection.execute('UPDATE backend_sync_outbox SET status=?,error=?,receipt=?,attempts=attempts+1,updated_at=? WHERE kind=? AND run_id=?', (status, error, json.dumps(receipt, ensure_ascii=False), now(), kind, run_id))
            record = self._record(kind, run_id)
        if kind == 'JOBS':
            technology_run_id = 'jobs-tech-' + hashlib.sha256(run_id.encode('utf-8')).hexdigest()
            evidence = self._record('DISCOVERY', technology_run_id)
            if evidence is not None:
                if record['status'] == 'SENT' and evidence['attempts'] == 0:
                    evidence = self.retry('DISCOVERY', technology_run_id)
                record['technology_sync'] = {key: evidence[key] for key in ('run_id', 'status', 'error', 'attempts')}
        return record

    def _record(self, kind, run_id):
        with self.repository.connect() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute('SELECT * FROM backend_sync_outbox WHERE kind=? AND run_id=?', (kind, run_id)).fetchone()
        return dict(row) if row is not None else None

    def records(self):
        with self.repository.connect() as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute('SELECT kind,run_id,payload_hash,status,attempts,error,receipt,updated_at FROM backend_sync_outbox ORDER BY updated_at DESC LIMIT 100')]
