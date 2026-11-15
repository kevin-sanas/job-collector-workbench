import hashlib
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

from jsonschema import Draft7Validator, FormatChecker

from app.discovery import DISCOVERY_SCHEMA_VERSION
from app.discovery.models import (
    ContentCandidate,
    ContentCriteria,
    DiscoveryRun,
    ExtractionIssue,
    JobExtractionStats,
    JobTechMention,
    RepositorySnapshot,
    SourceIssue,
    TechDictionary,
)
from app.io_utils import atomic_destination
from app.models import canonical_url


CONTENT_BATCH_MAX_BYTES = 5 * 1024 * 1024
CONTENT_SCHEMA_PATH = Path(__file__).resolve().parents[2] / 'config' / 'discovery' / 'discovery-content.v0.1.schema.json'
CONTENT_VALIDATOR = Draft7Validator(
    json.loads(CONTENT_SCHEMA_PATH.read_text(encoding='utf-8')),
    format_checker=FormatChecker(),
)


def _validate_content_document(
    document: dict,
    *,
    dictionary: TechDictionary | None = None,
    legacy_preview: bool = False,
    require_dictionary: bool = False,
) -> bool:
    checked = document
    if legacy_preview:
        checked = document | {'dictionary_version': 'legacy-preview-only'}
    error = next(CONTENT_VALIDATOR.iter_errors(checked), None)
    if error is not None:
        location = '.'.join(str(part) for part in error.absolute_path) or '批次根字段'
        raise ValueError(f'内容 JSON 不符合目标协议：{location}（{error.validator}）')
    candidates = document['content_candidates']
    if document['stats']['content_candidates'] != len(candidates):
        raise ValueError('内容运行统计与候选数量不一致')
    keys = [item['candidate_key'] for item in candidates]
    if len(keys) != len(set(keys)):
        raise ValueError('内容候选键不能重复')
    used_keys = {key for item in candidates for key in item['tech_keys']}
    dictionary_unverified = bool(used_keys) and (
        dictionary is None or dictionary.dictionary_version != document['dictionary_version']
    )
    if dictionary_unverified and require_dictionary:
        raise ValueError('内容技术键需要与本次词典版本核对，当前仅支持本地预览')
    if used_keys and not dictionary_unverified:
        known_keys = {term.tech_key for term in dictionary.terms}
        if unknown := used_keys - known_keys:
            raise ValueError(f'内容技术键不在声明词典中：{", ".join(sorted(unknown))}')
    for item in candidates:
        if item['extractor_version'] != document['extractor_version']:
            raise ValueError('候选提取器版本与批次不一致')
        if canonical_url(item['source_url']) != item['source_url']:
            raise ValueError('候选来源链接必须规范化且不含凭据')
        if item['content_type'] == 'open_source_project':
            host = {'github': 'github.com', 'gitee': 'gitee.com'}[item['platform']]
            url = urlsplit(item['source_url'])
            if (
                url.hostname != host or url.query or url.path.strip('/') !=
                f"{item['owner']}/{item['repository_name']}" or
                item['repository_url'] != item['source_url']
            ):
                raise ValueError('仓库来源链接与平台或仓库身份不一致')
            snapshot = item['repository_snapshot']
            if snapshot['candidate_key'] != item['candidate_key'] or snapshot['fetched_at'] != item['fetched_at']:
                raise ValueError('仓库快照与候选不一致')
        elif item['repository_snapshot'] is not None:
            raise ValueError('非仓库候选不应包含仓库快照')
    if any(document['stats'][name] != 0 for name in ('scanned_jobs', 'matched_jobs', 'job_mentions')):
        raise ValueError('纯内容批次不能包含岗位统计')
    return dictionary_unverified


@dataclass(frozen=True)
class DiscoveryBatch:
    schema_version: str
    run_id: str
    trigger: str
    generated_at: str
    dictionary_version: str
    extractor_version: str
    stats: JobExtractionStats
    job_mentions: tuple[JobTechMention, ...]
    content_candidates: tuple[dict, ...]
    issues: tuple[ExtractionIssue | SourceIssue, ...]
    search_criteria: ContentCriteria | None = None
    run_type: str = 'job_tech'
    legacy_preview: bool = False
    dictionary_unverified: bool = False

    def to_dict(self) -> dict:
        payload = {
            'schema_version': self.schema_version,
            'run_id': self.run_id,
            'trigger': self.trigger,
            'generated_at': self.generated_at,
            'dictionary_version': self.dictionary_version,
            'extractor_version': self.extractor_version,
            'stats': asdict(self.stats),
            'job_mentions': [asdict(mention) for mention in self.job_mentions],
            'content_candidates': list(self.content_candidates),
            'issues': [asdict(issue) for issue in self.issues],
        }
        if self.search_criteria is not None:
            payload['search_criteria'] = asdict(self.search_criteria)
        return payload


def build_job_tech_batch(
    run: DiscoveryRun,
    mentions: list[JobTechMention],
    issues: list[ExtractionIssue],
) -> DiscoveryBatch:
    if run.status != 'completed' or not run.completed_at:
        raise ValueError('只能为已完成运行生成批次')
    if any(mention.run_id != run.run_id for mention in mentions):
        raise ValueError('技术关联不属于指定运行')
    if any(issue.run_id != run.run_id for issue in issues):
        raise ValueError('提取问题不属于指定运行')
    if any(
        mention.dictionary_version != run.dictionary_version
        or mention.extractor_version != run.extractor_version
        for mention in mentions
    ):
        raise ValueError('技术关联版本与运行不一致')
    return DiscoveryBatch(
        schema_version=DISCOVERY_SCHEMA_VERSION,
        run_id=run.run_id,
        trigger='manual_desktop',
        generated_at=run.completed_at,
        dictionary_version=run.dictionary_version,
        extractor_version=run.extractor_version,
        stats=run.stats,
        job_mentions=tuple(sorted(mentions, key=lambda item: (item.record_key, item.tech_key))),
        content_candidates=(),
        issues=tuple(sorted(
            issues,
            key=lambda item: (item.record_key, item.issue_code, item.matched_alias),
        )),
    )


def build_content_batch(
    run: DiscoveryRun,
    candidates: list[ContentCandidate],
    snapshots: list[RepositorySnapshot],
    issues: list[SourceIssue],
) -> DiscoveryBatch:
    if run.run_type != 'content' or run.status != 'completed' or not run.completed_at:
        raise ValueError('只能为已完成内容运行生成批次')
    if run.stats.content_candidates != len(candidates):
        raise ValueError('内容运行统计与候选数量不一致')
    if any(getattr(run.stats, name) != 0 for name in ('scanned_jobs', 'matched_jobs', 'job_mentions')):
        raise ValueError('纯内容运行不能包含岗位统计')
    if len({candidate.candidate_key for candidate in candidates}) != len(candidates):
        raise ValueError('本次候选键不能重复')
    if any(candidate.extractor_version != run.extractor_version for candidate in candidates):
        raise ValueError('候选版本与运行不一致')
    candidate_by_key = {candidate.candidate_key: candidate for candidate in candidates}
    if len({snapshot.candidate_key for snapshot in snapshots}) != len(snapshots):
        raise ValueError('本次指标快照不能重复')
    for snapshot in snapshots:
        candidate = candidate_by_key.get(snapshot.candidate_key)
        if candidate is None or snapshot.fetched_at != candidate.fetched_at:
            raise ValueError('指标快照与候选不一致')
    snapshot_by_key = {snapshot.candidate_key: snapshot for snapshot in snapshots}
    contents = []
    for candidate in sorted(candidates, key=lambda item: item.candidate_key):
        payload = asdict(candidate)
        payload['tech_keys'] = list(candidate.tech_keys)
        payload['topics'] = list(candidate.topics)
        snapshot = snapshot_by_key.get(candidate.candidate_key)
        if candidate.content_type == 'open_source_project' and snapshot is None:
            raise ValueError('开源项目必须包含仓库快照')
        if candidate.content_type != 'open_source_project' and snapshot is not None:
            raise ValueError('非仓库候选不应包含仓库快照')
        payload['repository_snapshot'] = asdict(snapshot) if snapshot else None
        contents.append(payload)
    return DiscoveryBatch(
        schema_version=DISCOVERY_SCHEMA_VERSION,
        run_id=run.run_id,
        trigger='manual_desktop',
        generated_at=run.completed_at,
        dictionary_version=run.dictionary_version,
        extractor_version=run.extractor_version,
        stats=run.stats,
        job_mentions=(),
        content_candidates=tuple(contents),
        issues=tuple(sorted(
            issues,
            key=lambda item: (item.source_id, item.issue_code, item.candidate_key, item.message),
        )),
        search_criteria=run.search_criteria,
        run_type='content',
    )


def parse_content_batch(
    payload: bytes,
    dictionary: TechDictionary | None = None,
) -> tuple[DiscoveryRun, list[ContentCandidate], list[RepositorySnapshot], list[SourceIssue], DiscoveryBatch]:
    """Load a completed content batch for local preview without changing SQLite."""
    if not isinstance(payload, bytes) or len(payload) > CONTENT_BATCH_MAX_BYTES:
        raise ValueError('内容 JSON 文件无效或超过 5 MiB')
    try:
        document = json.loads(payload.decode('utf-8'))
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError('内容 JSON 不是有效的 UTF-8 JSON') from None
    if not isinstance(document, dict):
        raise ValueError('内容 JSON 批次根节点必须为对象')
    legacy_preview = (
        document.get('dictionary_version') == ''
        and isinstance(document.get('content_candidates'), list)
        and any(isinstance(item, dict) and item.get('tech_keys') for item in document['content_candidates'])
    )
    dictionary_unverified = _validate_content_document(
        document, dictionary=dictionary, legacy_preview=legacy_preview,
    )
    criteria_payload = document.get('search_criteria')
    try:
        criteria = ContentCriteria(**criteria_payload) if criteria_payload is not None else None
        stats = JobExtractionStats(**document['stats'])
        run = DiscoveryRun(
            run_id=document['run_id'],
            run_type='content',
            status='completed',
            started_at=document['generated_at'],
            completed_at=document['generated_at'],
            dictionary_version=document['dictionary_version'],
            extractor_version=document['extractor_version'],
            record_keys=(),
            stats=stats,
            search_criteria=criteria,
        )
        candidates = []
        snapshots = []
        for item in document['content_candidates']:
            candidates.append(ContentCandidate.from_mapping(item))
            if candidates[-1].source_url != item['source_url']:
                raise ValueError('候选来源链接必须规范化且不含凭据')
            if item.get('readme') is not None and candidates[-1].readme.source_url != item['readme']['source_url']:
                raise ValueError('README 来源链接必须规范化且不含凭据')
            if item['repository_snapshot'] is not None:
                snapshots.append(RepositorySnapshot.from_mapping(item['repository_snapshot']))
        issues = [SourceIssue(**issue) for issue in document['issues']]
        batch = build_content_batch(run, candidates, snapshots, issues)
        if legacy_preview or dictionary_unverified:
            batch = replace(
                batch, legacy_preview=legacy_preview,
                dictionary_unverified=dictionary_unverified,
            )
    except (KeyError, TypeError):
        raise ValueError('内容 JSON 批次字段无效') from None
    return run, candidates, snapshots, issues, batch


def serialize_batch(batch: DiscoveryBatch, dictionary: TechDictionary | None = None) -> bytes:
    if batch.run_type == 'content':
        if batch.legacy_preview:
            raise ValueError('历史批次缺少词典版本，仅支持本地预览')
        _validate_content_document(batch.to_dict(), dictionary=dictionary, require_dictionary=True)
    text = json.dumps(
        batch.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ) + '\n'
    payload = text.encode('utf-8')
    if batch.run_type == 'content' and len(payload) > CONTENT_BATCH_MAX_BYTES:
        raise ValueError('内容 JSON 文件超过 5 MiB')
    return payload


def export_batch(batch: DiscoveryBatch, path: Path, dictionary: TechDictionary | None = None) -> str:
    payload = serialize_batch(batch, dictionary=dictionary)
    with atomic_destination(path) as destination:
        destination.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()

