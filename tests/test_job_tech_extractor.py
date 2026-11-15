import unittest

from app.discovery.dictionary import build_alias_index
from app.discovery.job_extractor import extract_job_technologies
from app.discovery.models import DiscoveryRun, TechDictionary, TechTerm
from app.models import Job


class JobTechExtractorTests(unittest.TestCase):
    def setUp(self):
        self.dictionary = TechDictionary(
            dictionary_version='tech-dict.test.1',
            terms=(
                TechTerm('java', 'Java', ('Java',), '语言', True, 'token'),
                TechTerm('javascript', 'JavaScript', ('JavaScript', 'JS'), '语言', True, 'token'),
                TechTerm('python', 'Python', ('Python',), '语言', True, 'token'),
                TechTerm('spring-boot', 'Spring Boot', ('Spring Boot', 'SpringBoot'), '框架', True, 'phrase'),
                TechTerm('artificial-intelligence', '人工智能', ('AI',), '方向', True, 'review'),
            ),
        )
        self.aliases = build_alias_index(self.dictionary)

    def job(self, title: str, description: str = '', source_job_id: str = '1') -> Job:
        return Job.from_mapping({
            'title': title,
            'company_name': '示例公司',
            'source': 'test-source',
            'source_job_id': source_job_id,
            'source_url': f'https://example.com/jobs/{source_job_id}',
            'city': '北京',
            'description': description,
        })

    def run_for(self, *jobs: Job) -> DiscoveryRun:
        return DiscoveryRun.start(
            'job_tech', self.dictionary.dictionary_version, 'job-tech-rule.v1',
            tuple(job.record_key for job in jobs),
        )

    def test_token_boundaries_are_case_insensitive_without_java_javascript_collision(self):
        jobs = (
            self.job('JavaScript 工程师', '熟悉 JAVASCRIPT 与 js', 'js'),
            self.job('JAVA 后端工程师', '', 'java'),
        )

        result = extract_job_technologies(jobs, self.dictionary, self.aliases, self.run_for(*jobs))

        self.assertEqual(
            {(item.job_identity.source_job_id, item.tech_key) for item in result.mentions},
            {('java', 'java'), ('js', 'javascript')},
        )

    def test_merges_aliases_and_source_fields_into_one_mention(self):
        job = self.job(
            'Python / Spring Boot 开发',
            '负责 PYTHON 服务，使用 SpringBoot。',
        )

        result = extract_job_technologies((job,), self.dictionary, self.aliases, self.run_for(job))
        mentions = {item.tech_key: item for item in result.mentions}

        self.assertEqual(set(mentions), {'python', 'spring-boot'})
        self.assertEqual(mentions['python'].source_fields, ('title', 'description'))
        self.assertEqual(mentions['spring-boot'].source_fields, ('title', 'description'))
        self.assertEqual(result.stats.scanned_jobs, 1)
        self.assertEqual(result.stats.matched_jobs, 1)
        self.assertEqual(result.stats.job_mentions, 2)

    def test_phrase_matches_multiple_spaces_and_line_breaks(self):
        jobs = (
            self.job('Spring  Boot 工程师', source_job_id='spaces'),
            self.job('后端工程师', '使用 Spring\nBoot 开发服务', 'newline'),
        )

        result = extract_job_technologies(jobs, self.dictionary, self.aliases, self.run_for(*jobs))

        self.assertEqual(
            {(item.job_identity.source_job_id, item.tech_key) for item in result.mentions},
            {('spaces', 'spring-boot'), ('newline', 'spring-boot')},
        )

    def test_empty_description_scans_title_and_evidence_is_redacted_and_bounded(self):
        title_only = self.job('Python 工程师', '', 'title-only')
        long_description = ('负责系统开发 ' * 30) + 'Python 联系电话：13800138000 ' + ('维护服务 ' * 30)
        detailed = self.job('后端工程师', long_description, 'details')

        result = extract_job_technologies(
            (detailed, title_only), self.dictionary, self.aliases, self.run_for(detailed, title_only)
        )
        by_id = {item.job_identity.source_job_id: item for item in result.mentions}

        self.assertEqual(by_id['title-only'].source_fields, ('title',))
        self.assertEqual(by_id['title-only'].evidence_excerpt, 'Python 工程师')
        self.assertLessEqual(len(by_id['details'].evidence_excerpt), 240)
        self.assertNotIn('13800138000', by_id['details'].evidence_excerpt)
        self.assertIn('已隐藏', by_id['details'].evidence_excerpt)

    def test_review_alias_creates_issue_but_not_mention(self):
        job = self.job('AI 应用开发工程师')

        result = extract_job_technologies((job,), self.dictionary, self.aliases, self.run_for(job))

        self.assertEqual(result.mentions, ())
        self.assertEqual(len(result.issues), 1)
        self.assertEqual(result.issues[0].candidate_tech_keys, ('artificial-intelligence',))
        self.assertEqual(result.stats.matched_jobs, 0)

    def test_output_is_stably_sorted_by_record_key_and_tech_key(self):
        jobs = (
            self.job('Python Java 工程师', source_job_id='z'),
            self.job('Spring Boot 工程师', source_job_id='a'),
        )

        result = extract_job_technologies(jobs, self.dictionary, self.aliases, self.run_for(*jobs))

        keys = [(item.record_key, item.tech_key) for item in result.mentions]
        self.assertEqual(keys, sorted(keys))
        self.assertTrue(all(len(item.job_content_hash) == 64 for item in result.mentions))


if __name__ == '__main__':
    unittest.main()
