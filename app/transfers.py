import csv
import math
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, TableStyle

from app.config import ROOT
from app.io_utils import atomic_destination
from app.models import Job, now


FIELD_LABELS = {
    'title': '岗位名称', 'company_name': '公司名称', 'city': '城市',
    'salary_raw': '原始薪资', 'salary_min': '最低薪资', 'salary_max': '最高薪资',
    'salary_period': '计薪周期', 'salary_months': '年薪月数', 'currency': '币种',
    'education_raw': '学历', 'experience_raw': '经验', 'employment_type': '工作类型',
    'district': '区县', 'work_address': '工作地址', 'description': '岗位描述', 'source': '来源',
    'source_job_id': '站点岗位ID', 'source_url': '来源链接', 'published_raw': '发布时间原文',
    'published_at': '发布时间', 'first_seen_at': '首次采集', 'last_seen_at': '最近采集',
}
ALIASES = {
    'title': ['职位', '职位名称', '岗位', 'job_name'],
    'company_name': ['公司', '企业', '企业名称', 'company'],
    'city': ['工作城市', '工作地点', '城市名称', 'location'],
    'salary_raw': ['薪资', '薪酬', 'salary', '工资'],
    'education_raw': ['学历要求', 'education'], 'experience_raw': ['工作经验', '经验要求', 'experience'],
    'description': ['职责', '任职要求', '职位描述', 'job_description'],
    'source_url': ['岗位链接', '职位链接', 'url', '链接'],
    'work_address': ['详细工作地址', '办公地址', 'work_address'],
}


@dataclass
class FilePreview:
    headers: list[str]
    rows: list[list]
    formula_rows: set[int]
    path: Path
    sheets: list[str]
    restored_fields: dict = field(default_factory=dict)


def suggested_mapping(headers: list[str]) -> dict[str, int]:
    mapping = {}
    normalized = [str(h).strip().lower() for h in headers]
    for key, label in FIELD_LABELS.items():
        for alias in [key, label, *ALIASES.get(key, [])]:
            if alias.lower() in normalized:
                mapping[key] = normalized.index(alias.lower())
                break
    return mapping


def preview_file(path: Path, encoding: str = 'utf-8-sig', sheet: str = '') -> FilePreview:
    path = Path(path)
    formulas = set()
    restored_fields = {}
    sheets = []
    if path.suffix.lower() == '.csv':
        with path.open('r', encoding=encoding, newline='') as file:
            reader = csv.reader(file)
            headers = next(reader, [])
            rows = []
            for index, row in enumerate(reader, 2):
                if index > 50001:
                    raise ValueError('单次导入上限为 50,000 行，请拆分文件')
                rows.append(row)
    elif path.suffix.lower() == '.xlsx':
        wb = load_workbook(path, read_only=True, data_only=False, keep_links=False)
        try:
            sheets = wb.sheetnames
            ws = wb[sheet] if sheet else wb.active
            iterator = ws.iter_rows()
            headers = [str(c.value or '') for c in next(iterator, [])]
            rows = []
            for index, cells in enumerate(iterator, 2):
                if index > 50001:
                    raise ValueError('单次导入上限为 50,000 行，请拆分文件')
                if any(c.data_type == 'f' for c in cells):
                    formulas.add(index)
                rows.append([c.value for c in cells])
            if ws.title == '岗位数据' and '完整描述' in sheets:
                if '导出关联键' not in headers:
                    raise ValueError('该工作簿含旧版长文本附表，无法可靠关联；请使用新版重新导出后导入，避免丢失原文')
                association = headers.index('导出关联键')
                chunks = {}
                for cells in wb['完整描述'].iter_rows(min_row=2):
                    if any(c.data_type == 'f' for c in cells):
                        raise ValueError('完整描述附表包含公式，请先转换为纯值')
                    values = [c.value for c in cells]
                    if len(values) < 4 or values[3] not in FIELD_LABELS:
                        raise ValueError('完整描述附表缺少字段标识，无法完整还原')
                    try:
                        part = int(values[1])
                    except (ValueError, TypeError):
                        raise ValueError('完整描述附表片段编号无效') from None
                    chunks.setdefault((str(values[0]), values[3]), []).append((part, str(values[2] or '')))
                complete = {}
                for key, fragments in chunks.items():
                    fragments.sort()
                    if [part for part, _ in fragments] != list(range(1, len(fragments) + 1)):
                        raise ValueError('完整描述附表片段缺失或重复，无法完整还原')
                    complete[key] = ''.join(text for _, text in fragments)
                for number, row in enumerate(rows, 2):
                    if association < len(row):
                        restored_fields[number] = {key: text for (record, key), text in complete.items() if record == str(row[association])}
                    if any(isinstance(value, str) and '[原文超长，完整内容见' in value for value in row) and not restored_fields.get(number):
                        raise ValueError('主表长文本没有对应的完整片段，导入已停止')
        finally:
            wb.close()
    else:
        raise ValueError('仅支持 .csv 和 .xlsx；请先将 .xls 转为 .xlsx')
    if not headers:
        raise ValueError('文件没有表头或为空')
    return FilePreview([str(h).strip() for h in headers], rows, formulas, path, sheets, restored_fields)


def import_jobs(preview: FilePreview, mapping: dict[str, int]) -> tuple[list[Job], list[str]]:
    if 'title' not in mapping or 'company_name' not in mapping:
        raise ValueError('请映射岗位名称和公司名称')
    jobs, errors = [], []
    batch = uuid.uuid4().hex
    for number, row in enumerate(preview.rows, 2):
        if not any(v is not None and str(v).strip() for v in row):
            continue
        try:
            if number in preview.formula_rows:
                raise ValueError('包含公式，请转换为纯值后导入')
            values = {key: row[index] for key, index in mapping.items() if index < len(row)}
            values.update({key: value for key, value in preview.restored_fields.get(number, {}).items() if key in mapping})
            for value in values.values():
                if isinstance(value, str) and value.lstrip().startswith('='):
                    raise ValueError('包含疑似公式，请确认并转换为纯值')
            jobs.append(Job.from_mapping(values | {'import_batch_id': batch}))
        except (ValueError, TypeError) as exc:
            errors.append(f'第 {number} 行：{exc}')
    return jobs, errors


def _text_cell(cell, value):
    if isinstance(value, str):
        # Force OOXML string type: a job title beginning with '=' is data.
        cell.value = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', value)
        cell.data_type = 's'
    else:
        cell.value = value


def export_excel(jobs: list[Job], path: Path, criteria: dict):
    if not jobs:
        raise ValueError('没有可导出的岗位')
    wb = Workbook()
    ws = wb.active
    ws.title = '岗位数据'
    keys = list(FIELD_LABELS)
    ws.append(list(FIELD_LABELS.values()) + ['导出关联键'])
    details = None
    for row_number, job in enumerate(jobs, 2):
        for col, key in enumerate(keys, 1):
            value = getattr(job, key)
            if isinstance(value, str) and len(value) > 32000:
                if details is None:
                    details = wb.create_sheet('完整描述')
                    details.append(['岗位记录键', '片段序号', '原文片段', '字段'])
                for part, offset in enumerate(range(0, len(value), 32000), 1):
                    r = details.max_row + 1
                    for c, item in enumerate((job.record_key, part, value[offset:offset + 32000], key), 1):
                        _text_cell(details.cell(r, c), item)
                value = value[:31000] + '\n[原文超长，完整内容见“完整描述”工作表]'
            cell = ws.cell(row_number, col)
            _text_cell(cell, value)
            cell.alignment = Alignment(vertical='top', wrap_text=True)
            if key == 'source_url' and job.source_url:
                cell.hyperlink = job.source_url
                cell.style = 'Hyperlink'
        _text_cell(ws.cell(row_number, len(keys) + 1), job.record_key)
    ws.freeze_panes = 'A2'
    ws.auto_filter.ref = ws.dimensions
    for cell in ws[1]:
        cell.fill = PatternFill('solid', fgColor='153F4A')
        cell.font = Font(color='FFFFFF', bold=True)
    from openpyxl.utils import get_column_letter
    for index, key in enumerate(keys, 1):
        ws.column_dimensions[get_column_letter(index)].width = 55 if key == 'description' else 26 if key in ('title', 'company_name', 'source_url') else 18
    info = wb.create_sheet('导出说明')
    info.append(['项目', '内容'])
    for key, value in {'导出时间': now(), '岗位数量': len(jobs), **criteria}.items():
        row = info.max_row + 1
        _text_cell(info.cell(row, 1), str(key))
        _text_cell(info.cell(row, 2), str(value))
    info.column_dimensions['A'].width = 25
    info.column_dimensions['B'].width = 80
    try:
        with atomic_destination(path) as destination:
            wb.save(destination)
    finally:
        wb.close()


def export_pdf(jobs: list[Job], path: Path, criteria: dict, font_path: str = ''):
    if not jobs:
        raise ValueError('没有可导出的岗位')
    font_file = Path(font_path) if font_path else ROOT / 'assets' / 'fonts' / 'NotoSansSC-Regular.ttf'
    if not font_file.is_file():
        raise ValueError('缺少中文字体，请在设置中选择许可允许嵌入的 TrueType 字体')
    font_name = 'JobsCJK_' + str(abs(hash(str(font_file.resolve()))))
    if font_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(font_name, str(font_file)))
    style = ParagraphStyle('body', fontName=font_name, fontSize=10, leading=17, wordWrap='CJK', alignment=TA_LEFT, textColor=colors.HexColor('#24383C'))
    heading = ParagraphStyle('heading', parent=style, fontSize=17, leading=25, spaceAfter=12)
    small = ParagraphStyle('small', parent=style, fontSize=8, leading=13, textColor=colors.HexColor('#586B70'))

    def paragraph(text, chosen=style):
        return Paragraph(escape(str(text or '未提供')).replace('\n', '<br/>'), chosen)

    story = [paragraph('岗位采集报告', heading), paragraph(f'导出时间：{now()}　岗位数量：{len(jobs)}', small), Spacer(1, 14)]
    for key, value in criteria.items():
        story.append(paragraph(f'{key}：{value}', small))
    story.extend([Spacer(1, 16), paragraph('岗位概览', heading)])
    rows = [[paragraph(v, small) for v in ('岗位 / 公司', '城市', '原始薪资')]]
    rows += [[paragraph(job.title[:100] + '\n' + job.company_name[:100], small), paragraph(job.city[:40], small), paragraph(job.salary_raw[:80], small)] for job in jobs]
    table = Table(rows, colWidths=[290, 75, 140], repeatRows=1, hAlign='LEFT')
    table.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#E6F1F0')), ('VALIGN', (0, 0), (-1, -1), 'TOP'), ('BOTTOMPADDING', (0, 0), (-1, -1), 8), ('LINEBELOW', (0, 0), (-1, -1), 0.3, colors.HexColor('#D3DFDF'))]))
    story.append(table)
    for index, job in enumerate(jobs, 1):
        story.extend([PageBreak(), paragraph(f'{index:02d}  {job.title}', heading)])
        for value in (job.company_name, f'{job.city} {job.district}　{job.salary_raw}', f'经验：{job.experience_raw or "未提供"}　学历：{job.education_raw or "未提供"}', f'来源：{job.source}　采集：{job.last_seen_at}'):
            story.append(paragraph(value))
        if job.work_address:
            story.append(paragraph(f'工作地址：{job.work_address}'))
        if job.source_url:
            story.append(Paragraph('<link href="' + escape(job.source_url, {'"': '&quot;'}) + '">查看原岗位</link>', small))
        story.append(Spacer(1, 14))
        # Bound each paragraph, allowing long descriptions to split over pages.
        for offset in range(0, len(job.description), 1200):
            story.append(paragraph(job.description[offset:offset + 1200]))

    def footer(canvas, document):
        canvas.saveState()
        canvas.setFont(font_name, 8)
        canvas.setFillColor(colors.HexColor('#586B70'))
        canvas.drawString(42, 24, '本地岗位报告 · 信息以来源页面为准')
        canvas.drawRightString(A4[0] - 42, 24, str(document.page))
        canvas.restoreState()

    with atomic_destination(path) as destination:
        SimpleDocTemplate(str(destination), pagesize=A4, leftMargin=42, rightMargin=42, topMargin=42, bottomMargin=45, title='岗位采集报告', author='本地岗位工作台').build(story, onFirstPage=footer, onLaterPages=footer)
