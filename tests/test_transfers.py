import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from openpyxl import Workbook, load_workbook

from app.models import Job
from app.transfers import preview_file, import_jobs, export_excel, export_pdf, suggested_mapping


class TransferTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parent.parent / 'data' / 'test-tmp'
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_csv_bom_mapping_and_bad_row(self):
        path = self.root / 'jobs.csv'
        path.write_text('职位名称,公司,城市\nPython开发,示例公司,北京\n缺公司,,上海\n', encoding='utf-8-sig')
        preview = preview_file(path)
        jobs, errors = import_jobs(preview, suggested_mapping(preview.headers))
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].city, '北京')
        self.assertIn('3', errors[0])

    def test_formula_is_rejected(self):
        path = self.root / 'formula.xlsx'
        wb = Workbook()
        wb.active.append(['岗位名称', '公司'])
        wb.active.append(['=1+1', '示例公司'])
        wb.save(path)
        preview = preview_file(path)
        jobs, errors = import_jobs(preview, suggested_mapping(preview.headers))
        self.assertFalse(jobs)
        self.assertTrue(errors)

    def test_export_preserves_formula_like_text_and_long_description(self):
        job = Job.from_mapping({'title': '=开发', 'company_name': '示例', 'description': '长描述' * 12000})
        path = self.root / 'jobs.xlsx'
        export_excel([job], path, {'城市': '北京'})
        wb = load_workbook(path)
        self.addCleanup(wb.close)
        self.assertEqual(wb['岗位数据']['A2'].value, '=开发')
        self.assertEqual(wb['岗位数据']['A2'].data_type, 's')
        parts = [row[2] for row in wb['完整描述'].iter_rows(min_row=2, values_only=True)]
        self.assertEqual(''.join(parts), job.description)

    def test_pdf_is_generated_with_chinese_and_special_text(self):
        job = Job.from_mapping({'title': 'Python 开发', 'company_name': '学习示例公司', 'description': '<研发> & 数据分析\n' * 200})
        path = self.root / 'jobs.pdf'
        export_pdf([job], path, {'城市': '北京'})
        self.assertTrue(path.read_bytes().startswith(b'%PDF-'))
        self.assertGreater(path.stat().st_size, 2000)

    def test_failed_export_preserves_existing_file(self):
        path = self.root / 'existing.xlsx'
        path.write_bytes(b'previous report')
        def broken_save(workbook, destination):
            Path(destination).write_bytes(b'incomplete new report')
            raise OSError('simulated disk failure')
        with patch.object(Workbook, 'save', broken_save), self.assertRaises(OSError):
            export_excel([Job.from_mapping({'title': '开发', 'company_name': '虚构企业'})], path, {})
        self.assertEqual(path.read_bytes(), b'previous report')

    def test_long_description_roundtrip_is_lossless(self):
        job = Job.from_mapping({'title': '开发', 'company_name': '企业', 'description': '完整内容' * 12000})
        path = self.root / 'roundtrip.xlsx'
        export_excel([job], path, {})
        preview = preview_file(path, sheet='岗位数据')
        jobs, errors = import_jobs(preview, suggested_mapping(preview.headers))
        self.assertFalse(errors)
        self.assertEqual(jobs[0].description, job.description)


if __name__ == '__main__':
    unittest.main()
