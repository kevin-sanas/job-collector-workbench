import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable
from collections.abc import Callable

from app.discovery.dictionary import CompiledAlias
from app.discovery.models import (
    DiscoveryRun,
    ExtractionIssue,
    JobExtractionResult,
    JobExtractionStats,
    JobIdentity,
    JobTechMention,
    TechDictionary,
)
from app.models import Job, redact_text


def _normalized_text(value: str) -> str:
    return unicodedata.normalize('NFKC', str(value or '')).casefold()


def _match_position(value: str, alias: CompiledAlias) -> int:
    text = _normalized_text(value)
    needle = alias.normalized_alias
    if alias.match_mode == 'token':
        match = re.search(rf'(?<![A-Za-z0-9_]){re.escape(needle)}(?![A-Za-z0-9_])', text)
        return match.start() if match else -1
    phrase = r'\s+'.join(re.escape(part) for part in needle.split(' '))
    match = re.search(phrase, text)
    return match.start() if match else -1


def _evidence(value: str, position: int, *, whole_value: bool) -> str:
    text = str(value or '')
    if whole_value:
        excerpt = text
    else:
        start = max(0, position - 100)
        excerpt = text[start:position + 140]
    return redact_text(excerpt)[:240]


def extract_job_technologies(
    jobs: Iterable[Job],
    dictionary: TechDictionary,
    aliases: tuple[CompiledAlias, ...],
    run: DiscoveryRun,
    check_cancel: Callable[[], None] | None = None,
) -> JobExtractionResult:
    mentions: list[JobTechMention] = []
    issues: dict[tuple[str, str], ExtractionIssue] = {}
    scanned_jobs = 0
    matched_jobs = 0

    for job in jobs:
        if check_cancel:
            check_cancel()
        scanned_jobs += 1
        identity = JobIdentity.from_job(job)
        content_hash = hashlib.sha256(json.dumps(
            {'description': job.description, 'title': job.title},
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        ).encode('utf-8')).hexdigest()
        matched: dict[str, dict] = {}

        for alias in aliases:
            title_position = _match_position(job.title, alias)
            description_position = _match_position(job.description, alias) if job.description else -1
            if title_position < 0 and description_position < 0:
                continue
            evidence = (
                _evidence(job.title, title_position, whole_value=True)
                if title_position >= 0
                else _evidence(job.description, description_position, whole_value=False)
            )
            if alias.review_required:
                issue_key = (identity.record_key, alias.tech_key)
                issues.setdefault(issue_key, ExtractionIssue(
                    run_id=run.run_id,
                    record_key=identity.record_key,
                    issue_code='review_required',
                    matched_alias=alias.alias,
                    candidate_tech_keys=(alias.tech_key,),
                    evidence_excerpt=evidence,
                ))
                continue
            state = matched.setdefault(alias.tech_key, {'fields': set(), 'evidence': evidence})
            if title_position >= 0:
                state['fields'].add('title')
                state['evidence'] = _evidence(job.title, title_position, whole_value=True)
            if description_position >= 0:
                state['fields'].add('description')

        if matched:
            matched_jobs += 1
        for tech_key, state in matched.items():
            source_fields = tuple(field for field in ('title', 'description') if field in state['fields'])
            mentions.append(JobTechMention(
                run_id=run.run_id,
                record_key=identity.record_key,
                tech_key=tech_key,
                source_fields=source_fields,
                evidence_excerpt=state['evidence'],
                job_content_hash=content_hash,
                extractor_version=run.extractor_version,
                dictionary_version=dictionary.dictionary_version,
                review_state='pending',
                job_identity=identity,
            ))

    mentions.sort(key=lambda item: (item.record_key, item.tech_key))
    sorted_issues = tuple(sorted(issues.values(), key=lambda item: (item.record_key, item.issue_code, item.matched_alias)))
    return JobExtractionResult(
        mentions=tuple(mentions),
        issues=sorted_issues,
        stats=JobExtractionStats(
            scanned_jobs=scanned_jobs,
            matched_jobs=matched_jobs,
            job_mentions=len(mentions),
        ),
    )

