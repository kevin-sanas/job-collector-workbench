import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from urllib.parse import urlsplit
from uuid import uuid4

from app.models import Job, canonical_url, now, redact_text


TECH_KEY_PATTERN = re.compile(r'[a-z0-9][a-z0-9-]{1,63}')
RUN_TYPES = {'job_tech', 'content'}
RUN_STATUSES = {'running', 'completed', 'cancelled', 'failed'}
REVIEW_STATES = {'pending', 'approved', 'rejected'}
SOURCE_FIELDS = {'title', 'description'}
MATCH_MODES = {'token', 'phrase', 'review'}


def _required_text(value: object, field_name: str) -> str:
    text = str(value or '').strip()
    if not text:
        raise ValueError(f'{field_name} 不能为空')
    return text


def _timezone_text(value: object, field_name: str, *, allow_empty: bool = False) -> str:
    text = str(value or '').strip()
    if allow_empty and not text:
        return ''
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f'{field_name} 必须为 ISO 8601 时间') from None
    if parsed.tzinfo is None:
        raise ValueError(f'{field_name} 必须包含时区')
    return text


def normalize_alias(value: object) -> str:
    return ' '.join(unicodedata.normalize('NFKC', str(value or '')).casefold().split())


@dataclass(frozen=True)
class TechTerm:
    tech_key: str
    display_name: str
    aliases: tuple[str, ...]
    category: str
    enabled: bool
    match_mode: str

    def __post_init__(self):
        tech_key = _required_text(self.tech_key, 'tech_key')
        if not TECH_KEY_PATTERN.fullmatch(tech_key):
            raise ValueError('tech_key 格式无效')
        object.__setattr__(self, 'tech_key', tech_key)
        object.__setattr__(self, 'display_name', _required_text(self.display_name, 'display_name'))
        object.__setattr__(self, 'category', _required_text(self.category, 'category'))
        if type(self.enabled) is not bool:
            raise ValueError('enabled 必须为布尔值')
        if self.match_mode not in MATCH_MODES:
            raise ValueError('match_mode 不受支持')
        aliases: list[str] = []
        seen: set[str] = set()
        for value in self.aliases:
            alias = _required_text(value, 'alias')
            normalized = normalize_alias(alias)
            if normalized not in seen:
                aliases.append(alias)
                seen.add(normalized)
        if not aliases:
            raise ValueError('aliases 不能为空')
        object.__setattr__(self, 'aliases', tuple(aliases))

    @classmethod
    def from_mapping(cls, values: dict) -> 'TechTerm':
        if not isinstance(values, dict):
            raise ValueError('词典条目必须为对象')
        required = {'tech_key', 'display_name', 'aliases', 'category', 'enabled', 'match_mode'}
        if not required.issubset(values):
            raise ValueError('词典条目字段不完整')
        if not isinstance(values['aliases'], list):
            raise ValueError('aliases 必须为数组')
        for name in ('tech_key', 'display_name', 'category', 'match_mode'):
            if not isinstance(values[name], str):
                raise ValueError(f'{name} 必须为字符串')
        if any(not isinstance(alias, str) for alias in values['aliases']):
            raise ValueError('aliases 元素必须为字符串')
        return cls(
            tech_key=values['tech_key'],
            display_name=values['display_name'],
            aliases=tuple(values['aliases']),
            category=values['category'],
            enabled=values['enabled'],
            match_mode=values['match_mode'],
        )

    def to_dict(self) -> dict:
        return {
            'tech_key': self.tech_key,
            'display_name': self.display_name,
            'aliases': list(self.aliases),
            'category': self.category,
            'enabled': self.enabled,
            'match_mode': self.match_mode,
        }


@dataclass(frozen=True)
class TechDictionary:
    dictionary_version: str
    terms: tuple[TechTerm, ...]

    def __post_init__(self):
        object.__setattr__(self, 'dictionary_version', _required_text(self.dictionary_version, 'dictionary_version'))
        terms = tuple(self.terms)
        if not terms:
            raise ValueError('terms 不能为空')
        keys = [term.tech_key for term in terms]
        if len(keys) != len(set(keys)):
            raise ValueError('tech_key 不能重复')
        object.__setattr__(self, 'terms', terms)

    @classmethod
    def from_mapping(cls, values: dict) -> 'TechDictionary':
        if not isinstance(values, dict) or 'dictionary_version' not in values or 'terms' not in values:
            raise ValueError('词典根字段不完整')
        if not isinstance(values['terms'], list):
            raise ValueError('terms 必须为数组')
        if not isinstance(values['dictionary_version'], str):
            raise ValueError('dictionary_version 必须为字符串')
        return cls(
            dictionary_version=values['dictionary_version'],
            terms=tuple(TechTerm.from_mapping(term) for term in values['terms']),
        )

    def to_dict(self) -> dict:
        return {
            'dictionary_version': self.dictionary_version,
            'terms': [term.to_dict() for term in self.terms],
        }


@dataclass(frozen=True)
class JobIdentity:
    record_key: str
    source: str
    source_job_id: str
    source_url: str
    title: str
    company_name: str
    city: str

    def __post_init__(self):
        object.__setattr__(self, 'record_key', _required_text(self.record_key, 'record_key'))
        object.__setattr__(self, 'source', _required_text(self.source, 'source'))
        object.__setattr__(self, 'title', _required_text(self.title, 'title'))
        object.__setattr__(self, 'company_name', _required_text(self.company_name, 'company_name'))
        for name in ('source_job_id', 'source_url', 'city'):
            object.__setattr__(self, name, str(getattr(self, name) or '').strip())

    @classmethod
    def from_job(cls, job: Job) -> 'JobIdentity':
        return cls(
            record_key=job.record_key,
            source=job.source,
            source_job_id=job.source_job_id,
            source_url=canonical_url(job.source_url),
            title=job.title,
            company_name=job.company_name,
            city=job.city,
        )


@dataclass(frozen=True)
class JobExtractionStats:
    scanned_jobs: int = 0
    matched_jobs: int = 0
    job_mentions: int = 0
    content_candidates: int = 0
    skipped: int = 0
    failed: int = 0

    def __post_init__(self):
        for name in ('scanned_jobs', 'matched_jobs', 'job_mentions', 'content_candidates', 'skipped', 'failed'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f'{name} 必须为非负整数')


@dataclass(frozen=True)
class DiscoveryRun:
    run_id: str
    run_type: str
    status: str
    started_at: str
    completed_at: str
    dictionary_version: str
    extractor_version: str
    record_keys: tuple[str, ...]
    stats: JobExtractionStats = field(default_factory=JobExtractionStats)
    message: str = ''
    search_criteria: 'ContentCriteria | None' = None

    def __post_init__(self):
        object.__setattr__(self, 'run_id', _required_text(self.run_id, 'run_id'))
        if self.run_type not in RUN_TYPES:
            raise ValueError('run_type 不受支持')
        if self.status not in RUN_STATUSES:
            raise ValueError('status 不受支持')
        object.__setattr__(self, 'started_at', _timezone_text(self.started_at, 'started_at'))
        object.__setattr__(self, 'completed_at', _timezone_text(self.completed_at, 'completed_at', allow_empty=True))
        object.__setattr__(self, 'dictionary_version', str(self.dictionary_version or '').strip())
        object.__setattr__(self, 'extractor_version', _required_text(self.extractor_version, 'extractor_version'))
        keys = tuple(sorted({_required_text(key, 'record_key') for key in self.record_keys}))
        object.__setattr__(self, 'record_keys', keys)
        object.__setattr__(self, 'message', str(self.message or '').strip())
        if self.search_criteria is not None and not isinstance(self.search_criteria, ContentCriteria):
            raise ValueError('内容搜索条件无效')

    @classmethod
    def start(
        cls,
        run_type: str,
        dictionary_version: str,
        extractor_version: str,
        record_keys: tuple[str, ...],
        search_criteria: 'ContentCriteria | None' = None,
    ) -> 'DiscoveryRun':
        return cls(
            run_id=str(uuid4()),
            run_type=run_type,
            status='running',
            started_at=now(),
            completed_at='',
            dictionary_version=dictionary_version,
            extractor_version=extractor_version,
            record_keys=record_keys,
            search_criteria=search_criteria,
        )


@dataclass(frozen=True)
class JobTechMention:
    run_id: str
    record_key: str
    tech_key: str
    source_fields: tuple[str, ...]
    evidence_excerpt: str
    job_content_hash: str
    extractor_version: str
    dictionary_version: str
    review_state: str
    job_identity: JobIdentity

    def __post_init__(self):
        object.__setattr__(self, 'run_id', _required_text(self.run_id, 'run_id'))
        object.__setattr__(self, 'record_key', _required_text(self.record_key, 'record_key'))
        tech_key = _required_text(self.tech_key, 'tech_key')
        if not TECH_KEY_PATTERN.fullmatch(tech_key):
            raise ValueError('tech_key 格式无效')
        object.__setattr__(self, 'tech_key', tech_key)
        source_fields = tuple(dict.fromkeys(self.source_fields))
        if not source_fields or any(field not in SOURCE_FIELDS for field in source_fields):
            raise ValueError('source_fields 必须由 title 或 description 组成')
        object.__setattr__(self, 'source_fields', source_fields)
        object.__setattr__(self, 'evidence_excerpt', str(self.evidence_excerpt or '').strip())
        object.__setattr__(self, 'job_content_hash', _required_text(self.job_content_hash, 'job_content_hash'))
        object.__setattr__(self, 'extractor_version', _required_text(self.extractor_version, 'extractor_version'))
        object.__setattr__(self, 'dictionary_version', _required_text(self.dictionary_version, 'dictionary_version'))
        if self.review_state not in REVIEW_STATES:
            raise ValueError('review_state 不受支持')
        if self.job_identity.record_key != self.record_key:
            raise ValueError('job_identity 与 record_key 不一致')


@dataclass(frozen=True)
class ExtractionIssue:
    run_id: str
    record_key: str
    issue_code: str
    matched_alias: str
    candidate_tech_keys: tuple[str, ...]
    evidence_excerpt: str

    def __post_init__(self):
        object.__setattr__(self, 'run_id', _required_text(self.run_id, 'run_id'))
        object.__setattr__(self, 'record_key', _required_text(self.record_key, 'record_key'))
        object.__setattr__(self, 'issue_code', _required_text(self.issue_code, 'issue_code'))
        object.__setattr__(self, 'matched_alias', _required_text(self.matched_alias, 'matched_alias'))
        keys = tuple(dict.fromkeys(_required_text(key, 'candidate_tech_key') for key in self.candidate_tech_keys))
        if not keys or any(not TECH_KEY_PATTERN.fullmatch(key) for key in keys):
            raise ValueError('candidate_tech_keys 格式无效')
        object.__setattr__(self, 'candidate_tech_keys', keys)
        object.__setattr__(self, 'evidence_excerpt', str(self.evidence_excerpt or '').strip())


@dataclass(frozen=True)
class JobExtractionResult:
    mentions: tuple[JobTechMention, ...]
    issues: tuple[ExtractionIssue, ...]
    stats: JobExtractionStats

    def __post_init__(self):
        object.__setattr__(self, 'mentions', tuple(self.mentions))
        object.__setattr__(self, 'issues', tuple(self.issues))


CONTENT_TYPES = {'open_source_project', 'ai_resource', 'market_case'}
README_STATUSES = {'fetched', 'not_found', 'failed', 'too_large', 'rate_limited'}


@dataclass(frozen=True)
class ContentCriteria:
    query: str
    limit: int
    sort: str
    created_after: str = ''

    def __post_init__(self):
        if not isinstance(self.query, str) or not self.query.strip() or len(self.query.strip()) > 100:
            raise ValueError('查询关键词长度必须为 1–100')
        object.__setattr__(self, 'query', self.query.strip())
        if type(self.limit) is not int or not 1 <= self.limit <= 20:
            raise ValueError('单次候选上限必须为 1–20')
        if self.sort not in {'stars', 'updated'}:
            raise ValueError('排序只支持 stars 或 updated')
        if self.created_after:
            if (
                not isinstance(self.created_after, str)
                or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', self.created_after)
            ):
                raise ValueError('创建日期必须为 YYYY-MM-DD')
            try:
                date.fromisoformat(self.created_after)
            except ValueError:
                raise ValueError('创建日期必须为 YYYY-MM-DD') from None


@dataclass(frozen=True)
class ReadmeEvidence:
    status: str
    source_url: str
    fetched_at: str
    sha: str = ''
    content: str = ''
    size_bytes: int | None = None
    truncated: bool = False
    reason: str = ''

    def __post_init__(self):
        if self.status not in README_STATUSES:
            raise ValueError('README 获取状态无效')
        if not isinstance(self.source_url, str):
            raise ValueError('README 来源链接无效')
        url = canonical_url(self.source_url)
        parts = urlsplit(url)
        if parts.scheme != 'https' or parts.hostname not in {
            'github.com', 'api.github.com', 'gitee.com',
        } or parts.query:
            raise ValueError('README 来源链接必须是对应仓库平台的 HTTPS 地址')
        object.__setattr__(self, 'source_url', url)
        object.__setattr__(self, 'fetched_at', _timezone_text(self.fetched_at, 'README 采集时间'))
        if not isinstance(self.sha, str) or (self.sha and not re.fullmatch(r'(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})', self.sha)):
            raise ValueError('README 文件 SHA 无效')
        if self.status == 'fetched' and not self.sha:
            raise ValueError('已获取的 README 必须保留文件 SHA')
        if not isinstance(self.content, str) or len(self.content) > 12000:
            raise ValueError('README 保留正文超过长度上限')
        if self.status != 'fetched' and self.content:
            raise ValueError('未获取的 README 不应保存正文')
        if type(self.truncated) is not bool:
            raise ValueError('README 截断标记必须为布尔值')
        cleaned = redact_text(self.content)
        if len(cleaned) > 12000:
            marker = '[内容已截断]'
            cleaned = cleaned[:12000 - len(marker) - 1].rstrip() + '\n' + marker
            object.__setattr__(self, 'truncated', True)
        object.__setattr__(self, 'content', cleaned)
        if self.size_bytes is not None and (type(self.size_bytes) is not int or self.size_bytes < 0):
            raise ValueError('README 文件大小无效')
        if not isinstance(self.reason, str) or len(self.reason) > 300:
            raise ValueError('README 获取原因过长')
        object.__setattr__(self, 'reason', redact_text(self.reason))

    @classmethod
    def from_mapping(cls, values: dict) -> 'ReadmeEvidence':
        if not isinstance(values, dict):
            raise ValueError('README 记录必须为对象')
        return cls(**values)


@dataclass(frozen=True)
class ContentCandidate:
    candidate_key: str
    content_type: str
    source_name: str
    source_url: str
    title: str
    source_published_at: str
    fetched_at: str
    source_evidence_excerpt: str
    proposed_summary: str
    proposed_learning_action: str
    tech_keys: tuple[str, ...]
    extractor_version: str
    review_state: str = 'pending'
    platform: str = ''
    repository_url: str = ''
    owner: str = ''
    repository_name: str = ''
    description: str = ''
    primary_language: str = ''
    topics: tuple[str, ...] = ()
    license_spdx: str = ''
    readme: ReadmeEvidence | None = None

    def __post_init__(self):
        if self.content_type not in CONTENT_TYPES:
            raise ValueError('内容类型不受支持')
        for name, limit in (('source_name', 80), ('title', 100), ('extractor_version', 80)):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
                raise ValueError(f'{name} 必须为 1–{limit} 字符的字符串')
            object.__setattr__(self, name, value.strip())
        if not isinstance(self.source_url, str):
            raise ValueError('来源链接必须为 HTTPS')
        url = canonical_url(self.source_url)
        if not url.startswith('https://'):
            raise ValueError('来源链接必须为 HTTPS')
        object.__setattr__(self, 'source_url', url)
        expected = hashlib.sha256((self.content_type + '\n' + url).encode('utf-8')).hexdigest()
        if self.candidate_key != expected:
            raise ValueError('候选键与来源不一致')
        object.__setattr__(self, 'fetched_at', _timezone_text(self.fetched_at, 'fetched_at'))
        object.__setattr__(
            self, 'source_published_at',
            _timezone_text(self.source_published_at, 'source_published_at', allow_empty=True),
        )
        for name, limit in (
            ('source_evidence_excerpt', 240),
            ('proposed_summary', 600),
            ('proposed_learning_action', 200),
            ('description', 500),
        ):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise ValueError(f'{name} 必须为字符串')
            cleaned = redact_text(value)
            if len(cleaned) > limit:
                raise ValueError(f'{name} 不能超过 {limit} 字符')
            object.__setattr__(self, name, cleaned)
        if self.review_state != 'pending':
            raise ValueError('桌面候选必须为待复核状态')
        if not isinstance(self.tech_keys, (list, tuple)):
            raise ValueError('tech_keys 必须为数组')
        keys = tuple(dict.fromkeys(self.tech_keys))
        if any(not isinstance(key, str) or not TECH_KEY_PATTERN.fullmatch(key) for key in keys):
            raise ValueError('tech_keys 格式无效')
        object.__setattr__(self, 'tech_keys', keys)
        if not isinstance(self.topics, (list, tuple)) or any(not isinstance(topic, str) for topic in self.topics):
            raise ValueError('topics 必须为字符串数组')
        object.__setattr__(self, 'topics', tuple(dict.fromkeys(
            topic.strip() for topic in self.topics if topic.strip()
        )))
        for name in ('platform', 'owner', 'repository_name', 'primary_language', 'license_spdx'):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise ValueError(f'{name} 必须为字符串')
            object.__setattr__(self, name, value.strip())
        if self.readme is not None and not isinstance(self.readme, ReadmeEvidence):
            raise ValueError('README 记录无效')
        if self.content_type == 'open_source_project':
            if self.platform not in {'github', 'gitee'} or not self.owner or not self.repository_name:
                raise ValueError('仓库平台及名称不能为空')
            if canonical_url(self.repository_url) != url:
                raise ValueError('仓库主页与来源链接不一致')
            repository_url = urlsplit(url)
            expected_host = 'github.com' if self.platform == 'github' else 'gitee.com'
            if (
                repository_url.hostname != expected_host
                or repository_url.query
                or repository_url.path.strip('/').casefold()
                != f'{self.owner}/{self.repository_name}'.casefold()
            ):
                raise ValueError('仓库平台、拥有者或仓库名与来源链接不一致')
            object.__setattr__(self, 'repository_url', url)
            if self.readme is not None:
                readme_url = urlsplit(self.readme.source_url)
                path = [part.casefold() for part in readme_url.path.strip('/').split('/')]
                owner, repository = self.owner.casefold(), self.repository_name.casefold()
                html_path = (
                    readme_url.hostname == expected_host
                    and len(path) >= 5
                    and path[:3] == [owner, repository, 'blob']
                )
                if self.platform == 'github':
                    api_path = (
                        readme_url.hostname == 'api.github.com'
                        and path == ['repos', owner, repository, 'readme']
                    )
                else:
                    api_path = (
                        readme_url.hostname == 'gitee.com'
                        and path == ['api', 'v5', 'repos', owner, repository, 'readme']
                    )
                if not (html_path or api_path):
                    raise ValueError('README 来源与仓库不一致')
        elif self.content_type == 'market_case':
            if not self.proposed_summary or not keys or not self.source_published_at or not self.source_evidence_excerpt:
                raise ValueError('市场案例需要摘要、关联技术、原文证据和可核对日期')
        if self.content_type != 'open_source_project' and (
            self.platform or self.repository_url or self.owner or self.repository_name or self.readme is not None
        ):
            raise ValueError('非仓库内容不得携带仓库身份或 README')

    @classmethod
    def from_mapping(cls, values: dict) -> 'ContentCandidate':
        if not isinstance(values, dict):
            raise ValueError('内容候选必须为对象')
        required = {
            'candidate_key', 'content_type', 'source_name', 'source_url', 'title',
            'source_published_at', 'fetched_at', 'source_evidence_excerpt',
            'proposed_summary', 'proposed_learning_action', 'tech_keys', 'extractor_version',
        }
        if not required.issubset(values):
            raise ValueError('内容候选字段不完整')
        data = {key: values[key] for key in cls.__dataclass_fields__ if key in values}
        data['source_published_at'] = data['source_published_at'] or ''
        if isinstance(data.get('readme'), dict):
            data['readme'] = ReadmeEvidence.from_mapping(data['readme'])
        return cls(**data)


@dataclass(frozen=True)
class RepositorySnapshot:
    candidate_key: str
    fetched_at: str
    stars: int | None
    forks: int | None
    pushed_at: str
    archived: bool | None

    def __post_init__(self):
        if not re.fullmatch(r'[0-9a-f]{64}', self.candidate_key):
            raise ValueError('指标快照候选键无效')
        object.__setattr__(self, 'fetched_at', _timezone_text(self.fetched_at, 'fetched_at'))
        object.__setattr__(self, 'pushed_at', _timezone_text(self.pushed_at, 'pushed_at', allow_empty=True))
        for name in ('stars', 'forks'):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f'{name} 必须为非负整数或空值')
        if self.archived is not None and type(self.archived) is not bool:
            raise ValueError('archived 必须为布尔值或空值')

    @classmethod
    def from_mapping(cls, values: dict) -> 'RepositorySnapshot':
        if not isinstance(values, dict):
            raise ValueError('仓库指标必须为对象')
        return cls(**values)


@dataclass(frozen=True)
class SourceIssue:
    source_id: str
    issue_code: str
    message: str
    candidate_key: str = ''

    def __post_init__(self):
        object.__setattr__(self, 'source_id', _required_text(self.source_id, 'source_id'))
        object.__setattr__(self, 'issue_code', _required_text(self.issue_code, 'issue_code'))
        if not isinstance(self.message, str):
            raise ValueError('问题说明必须为字符串')
        object.__setattr__(self, 'message', redact_text(self.message)[:300])
        if self.candidate_key and not re.fullmatch(r'[0-9a-f]{64}', self.candidate_key):
            raise ValueError('问题记录候选键无效')


@dataclass(frozen=True)
class ContentCollectionResult:
    candidates: tuple[ContentCandidate, ...]
    snapshots: tuple[RepositorySnapshot, ...]
    issues: tuple[SourceIssue, ...]
    stats: JobExtractionStats
    status: str = 'completed'
    message: str = ''

    def __post_init__(self):
        object.__setattr__(self, 'candidates', tuple(self.candidates))
        object.__setattr__(self, 'snapshots', tuple(self.snapshots))
        object.__setattr__(self, 'issues', tuple(self.issues))
        if self.stats.content_candidates != len(self.candidates):
            raise ValueError('候选数量与统计不一致')
        if self.status not in {'completed', 'cancelled', 'failed'}:
            raise ValueError('内容采集结果状态无效')
        if not isinstance(self.message, str):
            raise ValueError('内容采集结果说明必须为字符串')
        object.__setattr__(self, 'message', redact_text(self.message)[:500])

