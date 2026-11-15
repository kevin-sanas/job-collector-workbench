import json
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from app.models import Filters, Job, now
from app.regions import region_matches


class Repository:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS jobs (
                    record_key TEXT PRIMARY KEY, source TEXT NOT NULL,
                    title TEXT NOT NULL, company_name TEXT NOT NULL, city TEXT,
                    salary_min REAL, salary_max REAL, salary_period TEXT,
                    education TEXT, experience TEXT, description TEXT,
                    last_seen_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS jobs_source ON jobs(source);
                CREATE INDEX IF NOT EXISTS jobs_city ON jobs(city);
                CREATE INDEX IF NOT EXISTS jobs_seen ON jobs(last_seen_at);
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, updated_at TEXT, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS job_tasks (
                    task_id TEXT, record_key TEXT,
                    PRIMARY KEY(task_id, record_key)
                );
                CREATE TABLE IF NOT EXISTS source_policies (
                    site_id TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
            ''')

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.create_function('region_matches', 3, region_matches, deterministic=True)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def upsert(self, job: Job, task_id: str = '') -> bool:
        with self.connect() as conn:
            previous = conn.execute('SELECT payload FROM jobs WHERE record_key=?', (job.record_key,)).fetchone()
            if previous:
                old = json.loads(previous[0])
                retained = {key: old.get(key, '') for key in ('description', 'work_address', 'education_raw', 'experience_raw', 'employment_type', 'published_at', 'published_raw') if not getattr(job, key)}
                job = replace(job, first_seen_at=old['first_seen_at'], **retained)
            conn.execute('''INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(record_key) DO UPDATE SET
                source=excluded.source,title=excluded.title,company_name=excluded.company_name,
                city=excluded.city,salary_min=excluded.salary_min,salary_max=excluded.salary_max,
                salary_period=excluded.salary_period,education=excluded.education,
                experience=excluded.experience,description=excluded.description,
                last_seen_at=excluded.last_seen_at,payload=excluded.payload''', (
                    job.record_key, job.source, job.title, job.company_name, job.city,
                    job.salary_min, job.salary_max, job.salary_period, job.education_raw,
                    job.experience_raw, job.description, job.last_seen_at,
                    json.dumps(job.to_dict(), ensure_ascii=False)))
            if task_id or job.import_batch_id:
                conn.execute('INSERT OR IGNORE INTO job_tasks VALUES (?,?)', (task_id or job.import_batch_id, job.record_key))
        return previous is None

    def query(self, filters: Filters | None = None) -> list[Job]:
        filters = filters or Filters()
        clauses, params = [], []
        if filters.keyword.strip():
            clauses.append('(title LIKE ? OR company_name LIKE ? OR description LIKE ?)')
            params.extend([f'%{filters.keyword.strip()}%'] * 3)
        for field, value in (('source', filters.source), ('city', filters.city), ('education', filters.education), ('experience', filters.experience)):
            if field == 'city' and (filters.province_code or filters.city_code):
                continue
            if value.strip():
                clauses.append(f'{field} LIKE ?')
                params.append(f'%{value.strip()}%')
        if filters.province_code or filters.city_code:
            clauses.append('region_matches(city, ?, ?) = 1')
            params.extend((filters.province_code, filters.city_code))
        salary, salary_params = [], []
        if filters.salary_min is not None:
            salary.append('salary_max >= ?')
            salary_params.append(filters.salary_min)
        if filters.salary_max is not None:
            salary.append('salary_min <= ?')
            salary_params.append(filters.salary_max)
        if salary:
            expression = "(salary_period='月' AND " + ' AND '.join(salary) + ')'
            if filters.include_unknown:
                expression = f'({expression} OR salary_min IS NULL)'
            clauses.append(expression)
            params.extend(salary_params)
        if filters.since:
            clauses.append('last_seen_at >= ?')
            params.append(filters.since)
        sql = 'SELECT payload FROM jobs' + (' WHERE ' + ' AND '.join(clauses) if clauses else '') + ' ORDER BY last_seen_at DESC,record_key'
        with self.connect() as conn:
            return [Job(**json.loads(row[0])) for row in conn.execute(sql, params)]

    def count(self) -> int:
        with self.connect() as conn:
            return conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]

    def delete(self, keys: list[str]):
        with self.connect() as conn:
            conn.executemany('DELETE FROM jobs WHERE record_key=?', [(key,) for key in keys])
            conn.executemany('DELETE FROM job_tasks WHERE record_key=?', [(key,) for key in keys])

    def save_task(self, task: dict):
        with self.connect() as conn:
            conn.execute('INSERT OR REPLACE INTO tasks VALUES (?,?,?)', (task['id'], now(), json.dumps(task, ensure_ascii=False)))

    def tasks(self) -> list[dict]:
        with self.connect() as conn:
            return [json.loads(row[0]) for row in conn.execute('SELECT payload FROM tasks ORDER BY updated_at DESC LIMIT 100')]

    def save_sources(self, sources: list[dict]):
        with self.connect() as conn:
            conn.executemany('INSERT OR REPLACE INTO source_policies VALUES (?,?)', [(s['id'], json.dumps(s, ensure_ascii=False)) for s in sources])

