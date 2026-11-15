import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from app.discovery.models import (
    ContentCandidate,
    ContentCollectionResult,
    ContentCriteria,
    DiscoveryRun,
    ExtractionIssue,
    JobExtractionResult,
    JobExtractionStats,
    JobIdentity,
    JobTechMention,
    RepositorySnapshot,
    SourceIssue,
    TechDictionary,
)
from app.models import now


class DiscoveryRepository:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS discovery_runs (
                    run_id TEXT PRIMARY KEY,
                    run_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    dictionary_version TEXT NOT NULL,
                    extractor_version TEXT NOT NULL,
                    record_keys_json TEXT NOT NULL,
                    stats_json TEXT NOT NULL,
                    message TEXT NOT NULL,
                    search_criteria_json TEXT NOT NULL DEFAULT 'null'
                );
                CREATE INDEX IF NOT EXISTS discovery_runs_latest
                    ON discovery_runs(run_type, status, completed_at);
                CREATE TABLE IF NOT EXISTS dictionary_snapshots (
                    dictionary_version TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS job_tech_mentions (
                    run_id TEXT NOT NULL,
                    record_key TEXT NOT NULL,
                    tech_key TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY(run_id, record_key, tech_key),
                    FOREIGN KEY(run_id) REFERENCES discovery_runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS extraction_issues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    record_key TEXT NOT NULL,
                    issue_code TEXT NOT NULL,
                    matched_alias TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES discovery_runs(run_id)
                );
                CREATE INDEX IF NOT EXISTS extraction_issues_run
                    ON extraction_issues(run_id, record_key, issue_code, matched_alias);
                CREATE TABLE IF NOT EXISTS batch_exports (
                    run_id TEXT PRIMARY KEY,
                    schema_version TEXT NOT NULL,
                    path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES discovery_runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS content_candidates (
                    candidate_key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS run_content_candidates (
                    run_id TEXT NOT NULL,
                    candidate_key TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY(run_id, candidate_key),
                    FOREIGN KEY(run_id) REFERENCES discovery_runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS content_run_issues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES discovery_runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS candidate_snapshots (
                    candidate_key TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY(candidate_key, fetched_at)
                );
            ''')
            columns = {row[1] for row in connection.execute('PRAGMA table_info(discovery_runs)')}
            if 'search_criteria_json' not in columns:
                connection.execute(
                    "ALTER TABLE discovery_runs ADD COLUMN search_criteria_json TEXT NOT NULL DEFAULT 'null'"
                )

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute('PRAGMA foreign_keys=ON')
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _json(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

    @staticmethod
    def _run_from_row(row: tuple) -> DiscoveryRun:
        return DiscoveryRun(
            run_id=row[0],
            run_type=row[1],
            status=row[2],
            started_at=row[3],
            completed_at=row[4],
            dictionary_version=row[5],
            extractor_version=row[6],
            record_keys=tuple(json.loads(row[7])),
            stats=JobExtractionStats(**json.loads(row[8])),
            message=row[9],
            search_criteria=(
                ContentCriteria(**json.loads(row[10])) if row[10] != 'null' else None
            ),
        )

    def start_run(self, run: DiscoveryRun) -> None:
        if run.status != 'running':
            raise ValueError('只能创建 running 状态的运行')
        try:
            with self.connect() as connection:
                connection.execute(
                    '''INSERT INTO discovery_runs
                       (run_id,run_type,status,started_at,completed_at,dictionary_version,
                        extractor_version,record_keys_json,stats_json,message,search_criteria_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                    (
                        run.run_id, run.run_type, run.status, run.started_at, run.completed_at,
                        run.dictionary_version, run.extractor_version,
                        self._json(list(run.record_keys)), self._json(asdict(run.stats)), run.message,
                        self._json(asdict(run.search_criteria)) if run.search_criteria else 'null',
                    ),
                )
        except sqlite3.IntegrityError:
            raise ValueError(f'运行 {run.run_id} 已存在') from None

    def finish_job_run(
        self,
        run: DiscoveryRun,
        dictionary: TechDictionary,
        result: JobExtractionResult,
    ) -> None:
        if run.dictionary_version != dictionary.dictionary_version:
            raise ValueError('运行与词典版本不一致')
        with self.connect() as connection:
            row = connection.execute(
                'SELECT status FROM discovery_runs WHERE run_id=?', (run.run_id,)
            ).fetchone()
            if not row:
                raise ValueError('运行不存在')
            if row[0] != 'running':
                raise ValueError('终态运行不可修改')

            dictionary_payload = self._json(dictionary.to_dict())
            existing = connection.execute(
                'SELECT payload FROM dictionary_snapshots WHERE dictionary_version=?',
                (dictionary.dictionary_version,),
            ).fetchone()
            if existing and existing[0] != dictionary_payload:
                raise ValueError('同一词典版本不能对应不同内容')
            connection.execute(
                'INSERT OR IGNORE INTO dictionary_snapshots VALUES (?,?,?)',
                (dictionary.dictionary_version, dictionary_payload, now()),
            )
            connection.executemany(
                'INSERT INTO job_tech_mentions VALUES (?,?,?,?)',
                [
                    (mention.run_id, mention.record_key, mention.tech_key, self._json(asdict(mention)))
                    for mention in result.mentions
                ],
            )
            connection.executemany(
                'INSERT INTO extraction_issues(run_id,record_key,issue_code,matched_alias,payload) VALUES (?,?,?,?,?)',
                [
                    (
                        issue.run_id, issue.record_key, issue.issue_code, issue.matched_alias,
                        self._json(asdict(issue)),
                    )
                    for issue in result.issues
                ],
            )
            connection.execute(
                '''UPDATE discovery_runs
                   SET status='completed',completed_at=?,stats_json=?,message=''
                   WHERE run_id=?''',
                (now(), self._json(asdict(result.stats)), run.run_id),
            )

    def fail_run(self, run_id: str, status: str, message: str) -> None:
        if status not in {'failed', 'cancelled'}:
            raise ValueError('失败运行状态只能是 failed 或 cancelled')
        with self.connect() as connection:
            cursor = connection.execute(
                '''UPDATE discovery_runs SET status=?,completed_at=?,message=?
                   WHERE run_id=? AND status='running' ''',
                (status, now(), str(message or '').strip(), str(run_id).strip()),
            )
            if cursor.rowcount != 1:
                raise ValueError('运行不存在或已经结束')

    def finish_content_run(
        self,
        run: DiscoveryRun,
        result: ContentCollectionResult,
        dictionary: TechDictionary | None = None,
        *,
        status: str = 'completed',
        message: str = '',
    ) -> None:
        if run.run_type != 'content' or status not in {'completed', 'cancelled', 'failed'}:
            raise ValueError('内容运行类型或终态无效')
        if dictionary is not None and run.dictionary_version != dictionary.dictionary_version:
            raise ValueError('内容运行与技术词典版本不一致')
        candidates = {candidate.candidate_key: candidate for candidate in result.candidates}
        snapshots = {snapshot.candidate_key: snapshot for snapshot in result.snapshots}
        if len(candidates) != len(result.candidates) or len(snapshots) != len(result.snapshots):
            raise ValueError('本次候选或指标快照重复')
        if not set(snapshots).issubset(candidates):
            raise ValueError('指标快照没有对应的本次候选')
        if any(snapshot.fetched_at != candidates[key].fetched_at for key, snapshot in snapshots.items()):
            raise ValueError('指标快照时间与候选不一致')
        with self.connect() as connection:
            row = connection.execute(
                'SELECT status,run_type FROM discovery_runs WHERE run_id=?', (run.run_id,)
            ).fetchone()
            if not row or row != ('running', 'content'):
                raise ValueError('内容运行不存在或已经结束')
            if dictionary is not None:
                dictionary_payload = self._json(dictionary.to_dict())
                existing = connection.execute(
                    'SELECT payload FROM dictionary_snapshots WHERE dictionary_version=?',
                    (dictionary.dictionary_version,),
                ).fetchone()
                if existing and existing[0] != dictionary_payload:
                    raise ValueError('同一词典版本不能对应不同内容')
                connection.execute(
                    'INSERT OR IGNORE INTO dictionary_snapshots VALUES (?,?,?)',
                    (dictionary.dictionary_version, dictionary_payload, now()),
                )
            connection.executemany(
                'INSERT INTO run_content_candidates VALUES (?,?,?)',
                [
                    (
                        run.run_id, candidate.candidate_key,
                        self._json({
                            'candidate': asdict(candidate),
                            'snapshot': asdict(snapshots[candidate.candidate_key])
                            if candidate.candidate_key in snapshots else None,
                        }),
                    )
                    for candidate in result.candidates
                ],
            )
            connection.executemany(
                'INSERT INTO content_run_issues(run_id,payload) VALUES (?,?)',
                [(run.run_id, self._json(asdict(issue))) for issue in result.issues],
            )
            if status == 'completed':
                connection.executemany(
                    '''INSERT INTO content_candidates VALUES (?,?)
                       ON CONFLICT(candidate_key) DO UPDATE SET payload=excluded.payload''',
                    [
                        (candidate.candidate_key, self._json(asdict(candidate)))
                        for candidate in result.candidates
                    ],
                )
                for snapshot in result.snapshots:
                    payload = self._json(asdict(snapshot))
                    cursor = connection.execute(
                        'INSERT OR IGNORE INTO candidate_snapshots VALUES (?,?,?)',
                        (snapshot.candidate_key, snapshot.fetched_at, payload),
                    )
                    if cursor.rowcount == 0:
                        existing = connection.execute(
                            'SELECT payload FROM candidate_snapshots WHERE candidate_key=? AND fetched_at=?',
                            (snapshot.candidate_key, snapshot.fetched_at),
                        ).fetchone()[0]
                        if existing != payload:
                            raise ValueError('同一时间的指标快照内容不一致')
            connection.execute(
                '''UPDATE discovery_runs SET status=?,completed_at=?,stats_json=?,message=?
                   WHERE run_id=?''',
                (status, now(), self._json(asdict(result.stats)), message[:500], run.run_id),
            )

    def content_run(self, run_id: str) -> DiscoveryRun | None:
        with self.connect() as connection:
            row = connection.execute(
                '''SELECT run_id,run_type,status,started_at,completed_at,dictionary_version,
                          extractor_version,record_keys_json,stats_json,message,search_criteria_json
                   FROM discovery_runs WHERE run_id=? AND run_type='content' ''',
                (run_id,),
            ).fetchone()
        return self._run_from_row(row) if row else None

    def content_candidates(self, run_id: str | None = None) -> list[ContentCandidate]:
        with self.connect() as connection:
            if run_id is None:
                rows = connection.execute(
                    'SELECT payload FROM content_candidates ORDER BY candidate_key'
                ).fetchall()
                payloads = [json.loads(row[0]) for row in rows]
            else:
                rows = connection.execute(
                    'SELECT payload FROM run_content_candidates WHERE run_id=? ORDER BY candidate_key',
                    (run_id,),
                ).fetchall()
                payloads = [json.loads(row[0])['candidate'] for row in rows]
        return [ContentCandidate.from_mapping(payload) for payload in payloads]

    def content_snapshots(self, run_id: str) -> list[RepositorySnapshot]:
        with self.connect() as connection:
            rows = connection.execute(
                'SELECT payload FROM run_content_candidates WHERE run_id=? ORDER BY candidate_key',
                (run_id,),
            ).fetchall()
        return [
            RepositorySnapshot.from_mapping(payload['snapshot'])
            for row in rows
            if (payload := json.loads(row[0]))['snapshot'] is not None
        ]

    def candidate_snapshots(self, candidate_key: str) -> list[RepositorySnapshot]:
        with self.connect() as connection:
            rows = connection.execute(
                '''SELECT payload FROM candidate_snapshots WHERE candidate_key=?
                   ORDER BY fetched_at''',
                (candidate_key,),
            ).fetchall()
        return [RepositorySnapshot.from_mapping(json.loads(row[0])) for row in rows]

    def content_issues(self, run_id: str) -> list[SourceIssue]:
        with self.connect() as connection:
            rows = connection.execute(
                'SELECT payload FROM content_run_issues WHERE run_id=? ORDER BY id',
                (run_id,),
            ).fetchall()
        return [SourceIssue(**json.loads(row[0])) for row in rows]

    def latest_completed_run(self, run_type: str) -> DiscoveryRun | None:
        with self.connect() as connection:
            row = connection.execute(
                '''SELECT run_id,run_type,status,started_at,completed_at,dictionary_version,
                          extractor_version,record_keys_json,stats_json,message,search_criteria_json
                   FROM discovery_runs
                   WHERE run_type=? AND status='completed'
                   ORDER BY completed_at DESC,rowid DESC LIMIT 1''',
                (run_type,),
            ).fetchone()
        return self._run_from_row(row) if row else None

    def dictionary_snapshot_for_sync(self, version: str) -> dict:
        with self.connect() as conn:
            row = conn.execute('SELECT payload FROM dictionary_snapshots WHERE dictionary_version=?', (version,)).fetchone()
        if row is None:
            raise ValueError('此运行的词典快照不存在，不能用当前词典冒充历史版本')
        return json.loads(row[0])

    def mentions(self, run_id: str) -> list[JobTechMention]:
        with self.connect() as connection:
            rows = connection.execute(
                'SELECT payload FROM job_tech_mentions WHERE run_id=? ORDER BY record_key,tech_key',
                (run_id,),
            ).fetchall()
        values: list[JobTechMention] = []
        for row in rows:
            payload = json.loads(row[0])
            payload['source_fields'] = tuple(payload['source_fields'])
            payload['job_identity'] = JobIdentity(**payload['job_identity'])
            values.append(JobTechMention(**payload))
        return values

    def issues(self, run_id: str) -> list[ExtractionIssue]:
        with self.connect() as connection:
            rows = connection.execute(
                '''SELECT payload FROM extraction_issues WHERE run_id=?
                   ORDER BY record_key,issue_code,matched_alias,id''',
                (run_id,),
            ).fetchall()
        values: list[ExtractionIssue] = []
        for row in rows:
            payload = json.loads(row[0])
            payload['candidate_tech_keys'] = tuple(payload['candidate_tech_keys'])
            values.append(ExtractionIssue(**payload))
        return values

    def record_export(
        self,
        run_id: str,
        schema_version: str,
        path: Path,
        sha256: str,
        generated_at: str,
    ) -> None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT status FROM discovery_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if not row or row[0] != 'completed':
                raise ValueError('只能记录已完成运行的导出')
            connection.execute(
                '''INSERT INTO batch_exports VALUES (?,?,?,?,?)
                   ON CONFLICT(run_id) DO UPDATE SET
                   schema_version=excluded.schema_version,path=excluded.path,
                   sha256=excluded.sha256,generated_at=excluded.generated_at''',
                (run_id, schema_version, str(path), sha256, generated_at),
            )

