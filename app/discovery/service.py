from pathlib import Path

from app.collector import Cancelled, TaskControl
from app.discovery import JOB_TECH_EXTRACTOR_VERSION
from app.discovery.dictionary import build_alias_index, load_dictionary
from app.discovery.job_extractor import extract_job_technologies
from app.discovery.models import DiscoveryRun
from app.discovery.storage import DiscoveryRepository
from app.models import Filters, redact_text
from app.storage import Repository


class JobDiscoveryService:
    def __init__(
        self,
        job_repository: Repository,
        discovery_repository: DiscoveryRepository,
        dictionary_path: Path,
    ):
        self.job_repository = job_repository
        self.discovery_repository = discovery_repository
        self.dictionary_path = Path(dictionary_path)

    def scan_all(self, control: TaskControl | None = None) -> DiscoveryRun:
        dictionary = load_dictionary(self.dictionary_path)
        aliases = build_alias_index(dictionary)
        jobs = tuple(self.job_repository.query(Filters()))
        run = DiscoveryRun.start(
            'job_tech',
            dictionary.dictionary_version,
            JOB_TECH_EXTRACTOR_VERSION,
            tuple(job.record_key for job in jobs),
        )
        self.discovery_repository.start_run(run)
        try:
            if control:
                control.check()
            result = extract_job_technologies(
                jobs,
                dictionary,
                aliases,
                run,
                control.check if control else None,
            )
            if control:
                control.check()
            self.discovery_repository.finish_job_run(run, dictionary, result)
        except Cancelled:
            self.discovery_repository.fail_run(run.run_id, 'cancelled', '用户取消')
            raise
        except Exception as exc:
            detail = redact_text(str(exc)) if isinstance(exc, (ValueError, UnicodeError, PermissionError)) else '本地提取发生未预期错误'
            self.discovery_repository.fail_run(
                run.run_id,
                'failed',
                f'{type(exc).__name__}：{detail}'[:500],
            )
            raise
        completed = self.discovery_repository.latest_completed_run('job_tech')
        if completed is None:
            raise RuntimeError('岗位技术运行完成后未找到结果')
        return completed

