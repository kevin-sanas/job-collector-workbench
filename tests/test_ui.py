import os
import shutil
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QRawFont
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRect
from shiboken6 import isValid

from app.config import AppPaths, ROOT
from app.discovery.models import (
    ContentCriteria,
    DiscoveryRun,
    ExtractionIssue,
    JobExtractionStats,
    JobIdentity,
    JobTechMention,
    ReadmeEvidence,
)
from app.models import Job
from app.ui.discovery_page import DiscoveryPage
from app.ui.theme import configure_font
from app.ui.main_window import MainWindow


class UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])
        configure_font(cls.application)

    def test_bundled_ui_font_contains_chinese_glyphs(self):
        raw = QRawFont.fromFont(self.application.font())
        self.assertTrue(raw.supportsCharacter('岗'))
        self.assertTrue(raw.supportsCharacter('位'))

    def test_discovery_page_shows_local_scope_busy_state_and_completed_metrics(self):
        page = DiscoveryPage('tech-dict.draft-test')
        self.addCleanup(page.deleteLater)

        self.assertIn('仅本地候选', page.scope_note.text())
        self.assertIn('tech-dict.draft-test', page.dictionary_label.text())
        self.assertTrue(page.scan_button.isEnabled())
        self.assertFalse(page.export_button.isEnabled())
        page.set_busy(True)
        self.assertFalse(page.scan_button.isEnabled())
        self.assertFalse(page.export_button.isEnabled())
        self.assertFalse(page.refresh_button.isEnabled())
        self.assertFalse(page.cancel_button.isEnabled())

        page.set_busy(True, cancellable=True)
        self.assertTrue(page.cancel_button.isEnabled())

        page.set_busy(False)
        started = DiscoveryRun.start('job_tech', 'tech-dict.draft-test', 'job-tech-rule.v1', ('job-a', 'job-b'))
        completed = replace(
            started,
            status='completed',
            completed_at='2026-10-26T12:00:00+08:00',
            stats=JobExtractionStats(scanned_jobs=2, matched_jobs=1, job_mentions=1),
        )
        identity = JobIdentity(
            'job-a', 'test', 'a', 'https://example.com/a',
            'Python 工程师', '示例公司', '北京',
        )
        mention = JobTechMention(
            completed.run_id, 'job-a', 'python', ('title',), 'Python 工程师',
            'a' * 64, completed.extractor_version, completed.dictionary_version,
            'pending', identity,
        )
        issue = ExtractionIssue(
            completed.run_id, 'job-b', 'review_required', 'AI',
            ('artificial-intelligence',), 'AI 应用',
        )

        page.show_run(completed, [mention], [issue])

        self.assertIs(page.current_run, completed)
        self.assertEqual(page.mention_table.rowCount(), 1)
        self.assertEqual(page.issue_table.rowCount(), 1)
        self.assertIn('扫描 2', page.metrics_label.text())
        self.assertIn('命中岗位 1', page.metrics_label.text())
        self.assertIn('技术关联 1', page.metrics_label.text())
        self.assertIn('待复核 1', page.metrics_label.text())
        self.assertTrue(page.export_button.isEnabled())

        failed = replace(completed, run_id='failed-run', status='failed')
        page.show_run(failed, [], [])
        self.assertIs(page.current_run, completed)
        self.assertEqual(page.mention_table.rowCount(), 1)

    def test_discovery_page_collects_and_shows_github_candidates(self):
        page = DiscoveryPage('tech-dict.draft-test')
        self.addCleanup(page.deleteLater)
        self.assertEqual(page.tabs.count(), 2)
        self.assertIn('Gitee 支持关键词或推荐项目', page.content_description.text())
        self.assertEqual(page.content_source_tabs.count(), 3)
        self.assertFalse(page.github_export_button.isEnabled())
        self.assertTrue(page.github_import_button.isEnabled())
        self.assertFalse(page.github_cancel_button.isEnabled())
        refreshed = []
        page.github_refresh_requested.connect(lambda: refreshed.append(True))
        page.github_refresh_button.click()
        self.assertEqual(refreshed, [True])

        submitted = []
        page.github_collect_requested.connect(lambda keyword, limit, sort, created_after: submitted.append(
            (keyword, limit, sort, created_after)
        ))
        page.github_collect_button.click()
        self.assertEqual(submitted, [('', 3, 'stars', '')])
        self.assertIn('Star 超过 10 万', page.github_scope_note.text())
        page.github_keyword.setText(' RAG ')
        page.github_limit.setValue(2)
        page.github_sort.setCurrentIndex(1)
        page.github_created_after.setText(' 2026-10-01 ')
        page.github_collect_button.click()
        self.assertEqual(submitted, [('', 3, 'stars', ''), ('RAG', 2, 'updated', '2026-10-01')])
        gitee_searches = []
        page.gitee_search_requested.connect(
            lambda keyword, limit, sort: gitee_searches.append((keyword, limit, sort))
        )
        page.gitee_search_button.click()
        self.assertEqual(gitee_searches, [])
        self.assertIn('请填写 Gitee 关键词', page.content_hint.text())
        page.gitee_keyword.setText(' Python ')
        page.gitee_sort.setCurrentIndex(1)
        page.gitee_limit.setValue(2)
        page.gitee_search_button.click()
        self.assertEqual(gitee_searches, [('Python', 2, 'updated')])
        recommendations = []
        page.gitee_recommend_requested.connect(recommendations.append)
        page.gitee_keyword.clear()
        page.gitee_mode.setCurrentIndex(1)
        self.assertFalse(page.gitee_keyword.isEnabled())
        self.assertFalse(page.gitee_sort.isEnabled())
        page.gitee_search_button.click()
        self.assertEqual(recommendations, [2])
        market_submitted = []
        page.market_save_requested.connect(market_submitted.append)
        page.market_source_name.setText('示例企业技术博客')
        page.market_source_url.setText('https://example.org/case')
        page.market_title.setText('知识库问答案例')
        page.market_published_at.setText('2026-10-20T10:00:00+08:00')
        page.market_tech_keys.setText('python，fastapi')
        page.market_evidence.setPlainText('原文证据')
        page.market_summary.setPlainText('经人工整理的摘要')
        page.market_save_button.click()
        self.assertEqual(len(market_submitted), 1)
        self.assertEqual(market_submitted[0]['tech_keys'], ('python', 'fastapi'))
        self.assertEqual(market_submitted[0]['source_evidence_excerpt'], '原文证据')
        market_automatic = []
        page.market_collect_requested.connect(
            lambda mode, limit: market_automatic.append((mode, limit))
        )
        page.market_collect_mode.setCurrentIndex(1)
        page.market_collect_limit.setValue(2)
        page.market_collect_button.click()
        self.assertEqual(market_automatic, [('views', 2)])

        page.set_content_busy(True)
        self.assertFalse(page.github_collect_button.isEnabled())
        self.assertFalse(page.gitee_search_button.isEnabled())
        self.assertFalse(page.market_save_button.isEnabled())
        self.assertFalse(page.market_collect_button.isEnabled())
        self.assertFalse(page.github_created_after.isEnabled())
        self.assertTrue(page.github_cancel_button.isEnabled())
        self.assertFalse(page.github_export_button.isEnabled())
        self.assertFalse(page.github_refresh_button.isEnabled())
        self.assertFalse(page.github_import_button.isEnabled())

        started = DiscoveryRun.start(
            'content', '', 'github-rest.v1', (),
            search_criteria=ContentCriteria('RAG', 2, 'updated', '2026-10-01'),
        )
        completed = replace(
            started,
            status='completed',
            completed_at='2026-10-26T16:00:00+08:00',
            stats=JobExtractionStats(content_candidates=1),
        )
        candidate = SimpleNamespace(
            title='RAG 示例', source_name='GitHub', content_type='open_source_project',
            fetched_at='2026-10-26T16:00:00+08:00',
            license_spdx='', source_url='https://github.com/example/rag',
            readme=ReadmeEvidence(
                status='fetched',
                source_url='https://github.com/example/rag/blob/main/README.md',
                fetched_at='2026-10-26T16:00:00+08:00',
                sha='a' * 40,
                content='项目用途与快速开始',
                size_bytes=256,
            ),
        )
        issue = SimpleNamespace(
            source_id='github', issue_code='missing_license', message='许可证待核对',
        )
        page.set_content_busy(False)
        page.show_content_run(completed, [candidate], [issue])
        self.assertIs(page.current_content_run, completed)
        self.assertEqual(page.content_mode_tabs.currentIndex(), 1)
        self.assertEqual(page.content_results_tabs.tabText(0), '内容候选 (1)')
        self.assertEqual(page.content_results_tabs.tabText(1), '采集问题 (1)')
        self.assertIn('创建日期起：2026-10-01', page.content_hint.text())
        self.assertEqual(page.candidate_table.rowCount(), 1)
        self.assertEqual(page.candidate_table.item(0, 6).text(), '已获取')
        self.assertIn('项目用途与快速开始', page.readme_preview.toPlainText())
        self.assertIn('SHA：' + 'a' * 40, page.readme_info_label.text())
        self.assertEqual(page.candidate_table.item(0, 3).text(), '待核对')
        self.assertEqual(
            page.candidate_table.item(0, 5).text(),
            'https://github.com/example/rag',
        )
        self.assertEqual(page.content_issue_table.rowCount(), 1)
        self.assertIn('本次候选 1 条', page.content_metrics_label.text())
        self.assertTrue(page.github_export_button.isEnabled())
        old_candidate = SimpleNamespace(**(vars(candidate) | {'readme': None}))
        page.show_content_run(completed, [old_candidate], [])
        self.assertEqual(page.candidate_table.item(0, 6).text(), '旧记录未采集')
        self.assertIn('旧记录未采集 README', page.readme_info_label.text())
        market_candidate = SimpleNamespace(
            content_type='market_case', title='知识库问答案例',
            source_name='示例企业技术博客', fetched_at='2026-10-26T16:00:00+08:00',
            license_spdx='', source_url='https://example.org/case', readme=None,
            source_published_at='2026-10-20T10:00:00+08:00', tech_keys=('python', 'fastapi'),
            source_evidence_excerpt='原文证据', proposed_summary='经人工整理的摘要',
            proposed_learning_action='',
        )
        page.show_content_run(completed, [market_candidate], [])
        self.assertEqual(page.candidate_table.item(0, 6).text(), '不适用')
        self.assertIn('原文证据', page.readme_preview.toPlainText())
        self.assertIn('实践方向：待补充', page.readme_preview.toPlainText())
        page.show_content_run(completed, [candidate], [], imported=True, legacy_preview=True)
        self.assertFalse(page.github_export_button.isEnabled())
        self.assertIn('仅支持本地预览', page.content_hint.text())
        page.set_content_busy(True)
        page.set_content_busy(False)
        self.assertFalse(page.github_export_button.isEnabled())
        page.show_content_run(completed, [candidate], [], imported=True, dictionary_unverified=True)
        self.assertFalse(page.github_export_button.isEnabled())
        self.assertIn('无法核对', page.content_hint.text())

        failed = replace(completed, run_id='failed-run', status='failed')
        page.show_content_run(failed, [candidate], [issue])
        self.assertIs(page.current_content_run, failed)
        self.assertEqual(page.candidate_table.rowCount(), 1)
        self.assertFalse(page.github_export_button.isEnabled())
        self.assertIn('部分结果，不可导出', page.content_hint.text())

        cancelled = replace(completed, run_id='cancelled-run', status='cancelled')
        page.show_content_run(cancelled, [], [])
        self.assertIs(page.current_content_run, cancelled)
        self.assertEqual(page.candidate_table.rowCount(), 0)
        self.assertFalse(page.github_export_button.isEnabled())
        self.assertIn('部分结果，不可导出', page.content_hint.text())

        running = replace(completed, run_id='running-run', status='running', completed_at='')
        page.show_content_run(running, [], [])
        self.assertIs(page.current_content_run, cancelled)

    def test_content_controls_and_results_fit_or_scroll_at_compact_size(self):
        page = DiscoveryPage('tech-dict.draft-test')
        self.addCleanup(page.deleteLater)
        page.resize(900, 620)
        page.tabs.setCurrentIndex(1)
        page.content_source_tabs.setCurrentIndex(1)
        page.show()
        self.application.processEvents()

        self.assertEqual(page.content_source_tabs.widget(1).verticalScrollBar().maximum(), 0)
        self.assertFalse(page.gitee_mode.geometry().intersects(page.gitee_keyword.geometry()))
        self.assertFalse(page.gitee_keyword.geometry().intersects(page.gitee_search_button.geometry()))
        page.gitee_keyword.setText('保留的筛选条件')
        page.content_source_tabs.setCurrentIndex(2)
        page.market_mode_tabs.setCurrentIndex(1)
        self.application.processEvents()
        manual_scroll = page.market_mode_tabs.widget(1)
        manual_scroll.ensureWidgetVisible(page.market_save_button)
        self.application.processEvents()
        button_rect = QRect(
            page.market_save_button.mapTo(manual_scroll.viewport(), QPoint(0, 0)),
            page.market_save_button.size(),
        )
        self.assertTrue(manual_scroll.viewport().rect().contains(button_rect))
        page.content_mode_tabs.setCurrentIndex(1)
        page.content_detail_button.click()
        self.application.processEvents()
        self.assertGreater(page.readme_preview.height(), 130)
        self.assertTrue(page.content_results_tabs.isHidden())
        self.assertEqual(page.content_detail_button.text(), '返回列表')
        page.content_detail_button.click()
        self.assertFalse(page.content_results_tabs.isHidden())
        self.assertEqual(page.content_detail_button.text(), '放大详情')
        page.content_mode_tabs.setCurrentIndex(0)
        page.content_source_tabs.setCurrentIndex(1)
        self.assertEqual(page.gitee_keyword.text(), '保留的筛选条件')
        page.close()

    def test_window_reads_real_data_and_closes_workers(self):
        base = ROOT / 'data' / 'test-tmp'
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as directory:
            paths = AppPaths(Path(directory))
            shutil.copyfile(ROOT / 'config' / 'sources.json', paths.config / 'sources.json')
            shutil.copyfile(
                ROOT / 'config' / 'discovery' / 'tech_dictionary.json',
                paths.discovery_config / 'tech_dictionary.json',
            )
            shutil.copyfile(
                ROOT / 'config' / 'discovery' / 'content_sources.json',
                paths.discovery_config / 'content_sources.json',
            )
            window = MainWindow(paths)
            navigation = [window.navigation.item(index).text() for index in range(window.navigation.count())]
            self.assertIn('发现数据', navigation)
            self.assertEqual(window.discovery_page.scope_note.text(), '结果仅保存在本机，均为仅本地候选，尚未发布。')
            window.repo.upsert(Job.from_mapping({'title': '界面测试', 'company_name': '虚构公司'}))
            window.refresh_jobs()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and window.model.rowCount() != 1:
                self.application.processEvents()
                time.sleep(0.01)
            self.assertEqual(window.model.rowCount(), 1)
            self.assertEqual(window.model.rows[0].title, '界面测试')
            self.assertTrue(window.start_button.isEnabled())
            # Exercise an unconfigured source without depending on a real site.
            window.source()['selectors'] = {}
            window.update_source_note()
            self.assertFalse(window.start_button.isEnabled())
            window.close()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and isValid(window):
                self.application.processEvents()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                time.sleep(0.01)
            self.assertFalse(isValid(window), '关闭后须在 QApplication 仍存在时释放主窗口及其工作线程对象')


if __name__ == '__main__':
    unittest.main()
