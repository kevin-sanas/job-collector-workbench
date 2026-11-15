import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtWidgets import QApplication
from app.config import Settings
from app.ui.backend_sync_page import BackendSyncPage
from app.ui.main_window import MainWindow


class ContentSyncTests(unittest.TestCase):
    def test_only_completed_content_is_scheduled_when_enabled(self):
        context = SimpleNamespace(
            settings=Settings(backend_auto_discovery=True),
            schedule_backend_sync=MagicMock(),
            submit_content_run=MagicMock(return_value='sent'),
        )
        complete = SimpleNamespace(status='completed')
        MainWindow.schedule_completed_content_sync(context, complete)
        self.assertEqual(context.schedule_backend_sync.call_count, 1)
        operation = context.schedule_backend_sync.call_args.args[0]
        self.assertEqual(operation(), 'sent')
        context.submit_content_run.assert_called_once_with(complete)
        for state in ('failed', 'cancelled', 'running'):
            MainWindow.schedule_completed_content_sync(context, SimpleNamespace(status=state))
        context.settings.backend_auto_discovery = False
        MainWindow.schedule_completed_content_sync(context, complete)
        self.assertEqual(context.schedule_backend_sync.call_count, 1)

    def test_content_submission_uses_run_snapshot_and_validates_before_enqueue(self):
        run = SimpleNamespace(run_id='content-run', dictionary_version='dict-1')
        repo = MagicMock()
        repo.content_candidates.return_value = ['candidate']
        repo.content_snapshots.return_value = ['snapshot']
        repo.content_issues.return_value = ['issue']
        repo.dictionary_snapshot_for_sync.return_value = {'dictionary_version': 'dict-1'}
        client = MagicMock()
        context = SimpleNamespace(discovery_repo=repo, backend_sync=client)
        with patch('app.ui.main_window.build_content_batch', return_value='batch') as build, \
             patch('app.ui.main_window.TechDictionary.from_mapping', return_value='typed-dictionary'), \
             patch('app.ui.main_window.serialize_batch') as validate:
            MainWindow.submit_content_run(context, run)
        build.assert_called_once_with(run, ['candidate'], ['snapshot'], ['issue'])
        validate.assert_called_once_with('batch', dictionary='typed-dictionary')
        client.push_discovery.assert_called_once_with('batch', {'dictionary_version': 'dict-1'})

    def test_desktop_has_explicit_manual_content_sync(self):
        app = QApplication.instance() or QApplication([])
        page = BackendSyncPage(Settings())
        captured = []
        page.content_requested.connect(lambda: captured.append(True))
        page.content_button.click()
        self.assertEqual(captured, [True])
        page.set_busy(True)
        self.assertFalse(page.content_button.isEnabled())
        page.close()
        app.processEvents()

    def test_partial_job_sync_failure_is_visible_in_desktop_status(self):
        context = SimpleNamespace(closing=False, sync_pending=0, backend_sync_page=MagicMock(),
                                  refresh_backend_records=MagicMock(), statusBar=MagicMock(),
                                  async_call=MagicMock())
        MainWindow.schedule_backend_sync(context, MagicMock())
        apply = context.async_call.call_args.args[1]
        apply({'status': 'SENT', 'technology_sync': {'status': 'FAILED', 'error': '连接失败'}})
        message = context.backend_sync_page.message.setText.call_args.args[0]
        self.assertIn('岗位已接收', message)
        self.assertIn('技术证据提交失败', message)
        self.assertIn('发现批次补交', message)


if __name__ == '__main__':
    unittest.main()
