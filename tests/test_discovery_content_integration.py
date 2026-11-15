import hashlib
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from app.config import AppPaths, ROOT
from app.discovery.models import (
    ContentCandidate,
    ContentCollectionResult,
    JobExtractionStats,
    RepositorySnapshot,
    ReadmeEvidence,
)
from app.ui.main_window import MainWindow
from app.ui.theme import configure_font


class ContentIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])
        configure_font(cls.application)

    def test_button_to_local_snapshot_and_json_export_without_network(self):
        base = ROOT / 'data' / 'test-tmp'
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as directory:
            paths = AppPaths(Path(directory))
            shutil.copyfile(ROOT / 'config' / 'sources.json', paths.config / 'sources.json')
            for name in ('tech_dictionary.json', 'content_sources.json'):
                shutil.copyfile(
                    ROOT / 'config' / 'discovery' / name,
                    paths.discovery_config / name,
                )
            window = MainWindow(paths)
            try:
                deadline = time.monotonic() + 5
                while window.discovery_page.content_busy and time.monotonic() < deadline:
                    self.application.processEvents()
                    time.sleep(0.01)
                self.assertFalse(window.discovery_page.content_busy)

                url = 'https://github.com/example/sample'
                candidate = ContentCandidate(
                    candidate_key=hashlib.sha256(('open_source_project\n' + url).encode()).hexdigest(),
                    content_type='open_source_project',
                    source_name='GitHub',
                    source_url=url,
                    title='example/sample',
                    source_published_at='',
                    fetched_at='2026-10-26T12:00:00+08:00',
                    source_evidence_excerpt='Python 示例仓库',
                    proposed_summary='',
                    proposed_learning_action='',
                    tech_keys=('python',),
                    extractor_version='github-rest.v2',
                    platform='github',
                    repository_url=url,
                    owner='example',
                    repository_name='sample',
                    readme=ReadmeEvidence(
                        status='fetched',
                        source_url='https://github.com/example/sample/blob/main/README.md',
                        fetched_at='2026-10-26T12:00:00+08:00',
                        sha='a' * 40,
                        content='项目用途与快速开始',
                        size_bytes=256,
                    ),
                )
                result = ContentCollectionResult(
                    candidates=(candidate,),
                    snapshots=(RepositorySnapshot(
                        candidate.candidate_key, candidate.fetched_at, 12, 2, '', False,
                    ),),
                    issues=(),
                    stats=JobExtractionStats(content_candidates=1),
                )
                with patch('app.ui.main_window.GithubSource.collect', return_value=result):
                    window.discovery_page.github_keyword.setText('Python')
                    window.discovery_page.github_limit.setValue(1)
                    window.discovery_page.github_collect_button.click()
                    deadline = time.monotonic() + 5
                    while (
                        window.discovery_page.current_content_run is None
                        and time.monotonic() < deadline
                    ):
                        self.application.processEvents()
                        time.sleep(0.01)
                run = window.discovery_page.current_content_run
                self.assertIsNotNone(run)
                self.assertEqual(run.status, 'completed')
                self.assertEqual(window.discovery_page.candidate_table.rowCount(), 1)
                self.assertIn('项目用途与快速开始', window.discovery_page.readme_preview.toPlainText())
                self.assertEqual(window.discovery_repo.content_candidates(run.run_id), [candidate])

                output = Path(directory) / 'discovery.json'
                with (
                    patch('app.ui.main_window.QFileDialog.getSaveFileName', return_value=(str(output), '')),
                    patch('app.ui.main_window.QMessageBox.information'),
                ):
                    deadline = time.monotonic() + 5
                    while window.discovery_page.content_busy and time.monotonic() < deadline:
                        self.application.processEvents()
                        time.sleep(0.01)
                    window.discovery_page.github_export_button.click()
                    deadline = time.monotonic() + 5
                    while (not output.exists() or window.discovery_page.content_busy) and time.monotonic() < deadline:
                        self.application.processEvents()
                        time.sleep(0.01)
                    self.application.processEvents()
                self.assertTrue(output.exists())
                payload = json.loads(output.read_text(encoding='utf-8'))
                self.assertEqual(payload['run_id'], run.run_id)
                self.assertEqual(payload['job_mentions'], [])
                self.assertEqual(payload['search_criteria'], {
                    'query': 'Python', 'limit': 1, 'sort': 'stars', 'created_after': '',
                })
                self.assertEqual(
                    payload['content_candidates'][0]['repository_snapshot']['stars'], 12,
                )
                self.assertEqual(payload['content_candidates'][0]['readme']['sha'], 'a' * 40)
                self.assertEqual(
                    ContentCandidate.from_mapping(payload['content_candidates'][0]).readme.content,
                    '项目用途与快速开始',
                )
                with patch(
                    'app.ui.main_window.QFileDialog.getOpenFileName',
                    return_value=(str(output), ''),
                ):
                    window.discovery_page.github_import_button.click()
                    deadline = time.monotonic() + 5
                    while window.imported_content_batch is None and time.monotonic() < deadline:
                        self.application.processEvents()
                        time.sleep(0.01)
                self.assertIsNotNone(window.imported_content_batch)
                self.assertIn('未写入本地数据库', window.discovery_page.content_hint.text())
                self.assertIn('项目用途与快速开始', window.discovery_page.readme_preview.toPlainText())
                reexport = Path(directory) / 'reexport.json'
                with (
                    patch('app.ui.main_window.QFileDialog.getSaveFileName', return_value=(str(reexport), '')),
                    patch('app.ui.main_window.QMessageBox.information'),
                ):
                    deadline = time.monotonic() + 5
                    while window.discovery_page.content_busy and time.monotonic() < deadline:
                        self.application.processEvents()
                        time.sleep(0.01)
                    window.discovery_page.github_export_button.click()
                    deadline = time.monotonic() + 5
                    while not reexport.exists() and time.monotonic() < deadline:
                        self.application.processEvents()
                        time.sleep(0.01)
                    while window.discovery_page.content_busy and time.monotonic() < deadline:
                        self.application.processEvents()
                        time.sleep(0.01)
                self.assertEqual(output.read_bytes(), reexport.read_bytes())

                deadline = time.monotonic() + 5
                while window.discovery_page.content_busy and time.monotonic() < deadline:
                    self.application.processEvents()
                    time.sleep(0.01)
                window.discovery_page.github_keyword.clear()
                window.discovery_page.github_sort.setCurrentIndex(1)
                empty_result = ContentCollectionResult(
                    candidates=(), snapshots=(), issues=(),
                    stats=JobExtractionStats(),
                )
                with patch('app.ui.main_window.GithubSource.collect', return_value=empty_result) as mocked_collect:
                    window.discovery_page.github_collect_button.click()
                    deadline = time.monotonic() + 5
                    while (
                        window.discovery_page.current_content_run.run_id == run.run_id
                        and time.monotonic() < deadline
                    ):
                        self.application.processEvents()
                        time.sleep(0.01)
                blank_run = window.discovery_page.current_content_run
                self.assertNotEqual(blank_run.run_id, run.run_id)
                self.assertEqual(blank_run.status, 'completed')
                self.assertEqual(blank_run.search_criteria.query, 'stars:>100000')
                self.assertEqual(blank_run.search_criteria.sort, 'updated')
                self.assertEqual(mocked_collect.call_args.args[0], blank_run.search_criteria)
            finally:
                window.close()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and isValid(window):
                    self.application.processEvents()
                    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                    time.sleep(0.01)
                self.assertFalse(isValid(window))

    def test_gitee_and_manual_market_buttons_save_pending_candidates(self):
        base = ROOT / 'data' / 'test-tmp'
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as directory:
            paths = AppPaths(Path(directory))
            shutil.copyfile(ROOT / 'config' / 'sources.json', paths.config / 'sources.json')
            for name in ('tech_dictionary.json', 'content_sources.json'):
                shutil.copyfile(
                    ROOT / 'config' / 'discovery' / name,
                    paths.discovery_config / name,
                )
            window = MainWindow(paths)
            try:
                deadline = time.monotonic() + 5
                while window.discovery_page.content_busy and time.monotonic() < deadline:
                    self.application.processEvents()
                    time.sleep(0.01)
                gitee_url = 'https://gitee.com/example/sample'
                gitee_key = hashlib.sha256(
                    ('open_source_project\n' + gitee_url).encode('utf-8')
                ).hexdigest()
                gitee_candidate = ContentCandidate(
                    candidate_key=gitee_key, content_type='open_source_project',
                    source_name='Gitee', source_url=gitee_url, title='example/sample',
                    source_published_at='', fetched_at='2026-10-26T12:00:00+08:00',
                    source_evidence_excerpt='Python 示例仓库', proposed_summary='',
                    proposed_learning_action='', tech_keys=('python',),
                    extractor_version='gitee-rest.v1', platform='gitee',
                    repository_url=gitee_url, owner='example', repository_name='sample',
                    readme=ReadmeEvidence(
                        status='fetched',
                        source_url='https://gitee.com/example/sample/blob/master/README.md',
                        fetched_at='2026-10-26T12:00:00+08:00', sha='b' * 40,
                        content='Gitee 项目用途与快速开始', size_bytes=256,
                    ),
                )
                gitee_result = ContentCollectionResult(
                    candidates=(gitee_candidate,),
                    snapshots=(RepositorySnapshot(
                        gitee_key, gitee_candidate.fetched_at, None, None, '', None,
                    ),),
                    issues=(), stats=JobExtractionStats(content_candidates=1),
                )
                with patch('app.ui.main_window.GiteeSource.collect', return_value=gitee_result) as collect:
                    window.discovery_page.gitee_keyword.setText('sample')
                    window.discovery_page.gitee_search_button.click()
                    deadline = time.monotonic() + 5
                    while (
                        window.discovery_page.current_content_run is None
                        and time.monotonic() < deadline
                    ):
                        self.application.processEvents()
                        time.sleep(0.01)
                    self.assertEqual(collect.call_args.args[0].query, 'sample')
                gitee_run = window.discovery_page.current_content_run
                self.assertIsNotNone(gitee_run)
                self.assertEqual(gitee_run.status, 'completed')
                self.assertEqual(gitee_run.extractor_version, 'gitee-rest.v1')
                self.assertEqual(gitee_run.dictionary_version, window.discovery_dictionary.dictionary_version)
                self.assertIn('Gitee 项目用途', window.discovery_page.readme_preview.toPlainText())

                deadline = time.monotonic() + 5
                while window.discovery_page.content_busy and time.monotonic() < deadline:
                    self.application.processEvents()
                    time.sleep(0.01)
                page = window.discovery_page
                def collect_after_manual_verification(criteria, control, notify):
                    notify({'stage': 'waiting_verification'})
                    control.wait_for_user(timeout=5)
                    return gitee_result

                with patch('app.ui.main_window.GiteeSource.collect', side_effect=collect_after_manual_verification) as collect:
                    page.gitee_keyword.clear()
                    page.gitee_mode.setCurrentIndex(1)
                    page.gitee_search_button.click()
                    deadline = time.monotonic() + 5
                    while page.gitee_resume_button.isHidden() and time.monotonic() < deadline:
                        self.application.processEvents()
                        time.sleep(0.01)
                    self.assertFalse(page.gitee_resume_button.isHidden())
                    self.assertIn('自行完成', page.content_hint.text())
                    page.gitee_resume_button.click()
                    while page.current_content_run.run_id == gitee_run.run_id and time.monotonic() < deadline:
                        self.application.processEvents()
                        time.sleep(0.01)
                    self.assertEqual(collect.call_args.args[0], {'mode': 'recommended', 'limit': 3})
                recommendation_run = page.current_content_run
                self.assertNotEqual(recommendation_run.run_id, gitee_run.run_id)
                self.assertEqual(recommendation_run.status, 'completed')
                while page.content_busy and time.monotonic() < deadline:
                    self.application.processEvents()
                    time.sleep(0.01)
                page.market_source_name.setText('合成企业技术博客')
                page.market_source_url.setText('https://example.org/cases/knowledge-assistant')
                page.market_title.setText('合成知识库案例')
                page.market_published_at.setText('2026-10-20T10:00:00+08:00')
                page.market_evidence.setPlainText('团队将文档检索与问答用于内部知识查询。')
                page.market_summary.setPlainText('内部知识查询场景，采用文档检索与问答；原文未披露商业结果。')
                page.market_tech_keys.setText('python')
                page.market_save_button.click()
                deadline = time.monotonic() + 5
                while (
                    page.current_content_run.run_id == recommendation_run.run_id
                    and time.monotonic() < deadline
                ):
                    self.application.processEvents()
                    time.sleep(0.01)
                market_run = page.current_content_run
                self.assertNotEqual(market_run.run_id, gitee_run.run_id)
                self.assertEqual(market_run.status, 'completed')
                self.assertEqual(market_run.extractor_version, 'market-curation.v1')
                self.assertIn('内部知识查询场景', page.readme_preview.toPlainText())
                self.assertEqual(page.candidate_table.item(0, 6).text(), '不适用')
                saved = window.discovery_repo.content_candidates(market_run.run_id)
                self.assertEqual(len(saved), 1)
                self.assertEqual(saved[0].review_state, 'pending')
                self.assertEqual(saved[0].source_url, 'https://example.org/cases/knowledge-assistant')
            finally:
                window.close()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and isValid(window):
                    self.application.processEvents()
                    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                    time.sleep(0.01)
                self.assertFalse(isValid(window))


if __name__ == '__main__':
    unittest.main()
