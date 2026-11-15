from dataclasses import replace
import json
from dataclasses import fields
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QTableWidget, QTableWidgetItem, QScrollArea, QWidget, QFormLayout, QDialogButtonBox,
    QPushButton, QLineEdit, QDoubleSpinBox, QSpinBox, QFileDialog, QMessageBox, QPlainTextEdit)

from app.config import Settings
from app.transfers import FIELD_LABELS, suggested_mapping


class ImportDialog(QDialog):
    def __init__(self, preview, parent=None):
        super().__init__(parent)
        self.setWindowTitle('导入预览与字段映射')
        self.resize(960, 740)
        self.setMinimumSize(760, 580)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f'{preview.path.name}  ·  {len(preview.rows)} 行  ·  预览前 8 行'))
        table = QTableWidget(min(8, len(preview.rows)), len(preview.headers))
        table.setHorizontalHeaderLabels(preview.headers)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for row, values in enumerate(preview.rows[:8]):
            for col, value in enumerate(values[:len(preview.headers)]):
                table.setItem(row, col, QTableWidgetItem(str(value if value is not None else '')))
        table.resizeColumnsToContents()
        layout.addWidget(table, 1)
        layout.addWidget(QLabel('映射岗位字段（岗位名称、公司名称必填；忽略无关列）'))
        area = QScrollArea()
        area.setWidgetResizable(True)
        container = QWidget()
        form = QFormLayout(container)
        self.combos = {}
        initial = suggested_mapping(preview.headers)
        for key, label in FIELD_LABELS.items():
            combo = QComboBox()
            combo.addItem('不导入此字段', -1)
            for index, header in enumerate(preview.headers):
                combo.addItem(f'{index + 1}. {header}', index)
            combo.setCurrentIndex(initial.get(key, -1) + 1)
            self.combos[key] = combo
            form.addRow(label + (' *' if key in ('title', 'company_name') else ''), combo)
        area.setWidget(container)
        layout.addWidget(area, 2)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('校验并继续')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        buttons.accepted.connect(self.validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def mapping(self):
        return {key: combo.currentData() for key, combo in self.combos.items() if combo.currentData() >= 0}

    def validate(self):
        if not all(key in self.mapping() for key in ('title', 'company_name')):
            QMessageBox.warning(self, '缺少字段', '请映射岗位名称和公司名称')
            return
        self.accept()


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle('本项目设置')
        self.resize(710, 360)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.browser = QLineEdit(settings.browser_path)
        self.font = QLineEdit(settings.font_path)
        for label, edit, file_filter in [('浏览器程序', self.browser, '浏览器程序 (*.exe)'), ('中文字体（可选）', self.font, 'TrueType 字体 (*.ttf)')]:
            row = QHBoxLayout()
            row.addWidget(edit)
            button = QPushButton('选择文件')
            button.clicked.connect(lambda checked=False, e=edit, f=file_filter: self.pick(e, f))
            row.addWidget(button)
            form.addRow(label, row)
        self.interval = QDoubleSpinBox()
        self.interval.setRange(3, 120)
        self.interval.setValue(settings.interval_seconds)
        self.interval.setSuffix(' 秒')
        self.pages = QSpinBox()
        self.pages.setRange(1, 20)
        self.pages.setValue(settings.page_limit)
        self.timeout = QSpinBox()
        self.timeout.setRange(1, 120)
        self.timeout.setValue(settings.task_timeout_minutes)
        self.timeout.setSuffix(' 分钟')
        form.addRow('页面访问间隔', self.interval)
        form.addRow('最大列表页数', self.pages)
        form.addRow('任务总时长上限', self.timeout)
        layout.addLayout(form)
        hint = QLabel('选择本机 Chrome / Edge 的 exe。字体留空时使用项目内 Noto Sans SC。\n设置只保存在本项目，不修改 Clash 或 Windows 系统代理。访问间隔不保证避免风控。')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def pick(self, edit, file_filter):
        path, _ = QFileDialog.getOpenFileName(self, '选择文件', edit.text(), file_filter)
        if path:
            edit.setText(path)

    def value(self):
        return replace(self.settings, browser_path=self.browser.text().strip(), font_path=self.font.text().strip(), interval_seconds=self.interval.value(), page_limit=self.pages.value(), task_timeout_minutes=self.timeout.value())


class DiagnosticDialog(QDialog):
    def __init__(self, payload, default_directory, parent=None):
        super().__init__(parent)
        self.setWindowTitle('局部诊断预览（发送前请检查）')
        self.resize(860, 640)
        layout = QVBoxLayout(self)
        label = QLabel('请再次检查姓名、账号信息等。只在需要分析时分享这份局部样本；没有自动上传。')
        label.setWordWrap(True)
        layout.addWidget(label)
        self.text = QPlainTextEdit(json.dumps(payload, ensure_ascii=False, indent=2))
        layout.addWidget(self.text)
        save = QPushButton('另存为 JSON')
        save.clicked.connect(lambda: self.save(default_directory))
        layout.addWidget(save)

    def save(self, directory):
        path, _ = QFileDialog.getSaveFileName(self, '保存脱敏诊断', str(Path(directory) / '岗位局部样本.json'), 'JSON (*.json)')
        if path:
            try:
                Path(path).write_text(self.text.toPlainText(), encoding='utf-8')
                self.accept()
            except OSError as exc:
                QMessageBox.warning(self, '保存失败', str(exc))
