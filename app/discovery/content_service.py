from app.collector import Cancelled, TaskControl
from app.discovery.models import (
    ContentCollectionResult,
    ContentCriteria,
    DiscoveryRun,
    JobExtractionStats,
    TechDictionary,
)
from app.discovery.storage import DiscoveryRepository
from app.models import redact_text


class ContentDiscoveryService:
    def __init__(self, repository: DiscoveryRepository, source):
        self.repository = repository
        self.source = source
        self.dictionary = source.dictionary
        if not isinstance(self.dictionary, TechDictionary):
            raise ValueError('内容来源没有提供实际使用的技术词典')
        self.extractor_version = source.extractor_version

    def collect(
        self,
        criteria: ContentCriteria | str | dict,
        control: TaskControl | None = None,
        notify=None,
    ) -> DiscoveryRun:
        run = DiscoveryRun.start(
            'content', self.dictionary.dictionary_version, self.extractor_version, (),
            search_criteria=criteria if isinstance(criteria, ContentCriteria) else None,
        )
        self.repository.start_run(run)
        try:
            result = self.source.collect(criteria, control, notify)
        except Cancelled:
            result = ContentCollectionResult(
                candidates=(), snapshots=(), issues=(),
                stats=JobExtractionStats(),
                status='cancelled',
                message='用户取消',
            )
        except Exception as exc:
            detail = redact_text(str(exc)) if isinstance(exc, (ValueError, UnicodeError)) else '内容来源发生未预期错误'
            self.repository.fail_run(
                run.run_id, 'failed', f'{type(exc).__name__}：{detail}'[:500],
            )
            raise
        try:
            known_keys = {term.tech_key for term in self.dictionary.terms}
            if any(
                candidate.extractor_version != run.extractor_version
                or any(key not in known_keys for key in candidate.tech_keys)
                for candidate in result.candidates
            ):
                raise ValueError('内容候选的提取器版本或技术键与本次词典不一致')
            self.repository.finish_content_run(
                run, result, self.dictionary, status=result.status, message=result.message,
            )
        except Exception as exc:
            detail = redact_text(str(exc)) if isinstance(exc, (ValueError, UnicodeError)) else '内容结果保存失败'
            self.repository.fail_run(
                run.run_id, 'failed', f'{type(exc).__name__}：{detail}'[:500],
            )
            raise
        finished = self.repository.content_run(run.run_id)
        if finished is None:
            raise RuntimeError('内容运行结束后未找到记录')
        return finished
