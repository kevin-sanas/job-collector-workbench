from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QFrame, QListWidget, QStackedWidget, QPushButton, QLineEdit, QComboBox,
    QSpinBox, QCheckBox, QTableView, QHeaderView, QSplitter, QTextEdit, QAbstractItemView,
    QFileDialog, QMessageBox, QInputDialog, QTableWidget, QTableWidgetItem)

from app.backend_sync import BackendSyncClient
from app.browser import BrowserSession
from app.ui.backend_sync_page import BackendSyncPage
from app.collector import Criteria, TaskControl
from app.discovery import DISCOVERY_SCHEMA_VERSION
from app.discovery.batches import build_content_batch, build_job_tech_batch, export_batch, serialize_batch
from app.discovery.content_service import ContentDiscoveryService
from app.discovery.dictionary import load_dictionary
from app.discovery.http_client import HttpClient
from app.discovery.models import ContentCriteria, TechDictionary
from app.discovery.service import JobDiscoveryService
from app.discovery.sources.github import GithubSource
from app.discovery.sources.gitee import GiteeSource
from app.discovery.sources.market import ManualMarketSource, TencentBrowserClient, TencentMarketSource
from app.discovery.storage import DiscoveryRepository
from app.models import Filters
from app.storage import Repository
from app.transfers import preview_file, import_jobs, export_excel, export_pdf
from app.workers import BrowserWorker, FunctionWorker
from app.ui.dialogs import ImportDialog, SettingsDialog, DiagnosticDialog
from app.ui.discovery_page import DiscoveryPage
from app.ui.theme import STYLE
from app.ui.region_selector import RegionSelector
from app.regions import catalog


STATE_NAMES = {'running': '采集中', 'paused': '已暂停', 'waiting_login': '等待登录',
    'waiting_verification': '等待人工验证', 'waiting_user': '等待人工操作',
    'waiting_sample': '等待页面样本', 'completed': '已完成', 'cancelled': '已取消', 'failed': '已停止'}
WAIT_STATES = {'paused', 'waiting_login', 'waiting_verification', 'waiting_user', 'waiting_sample'}
TERMINAL_STATES = {'completed', 'cancelled', 'failed'}


def label(text, name='', wrap=False):
    widget = QLabel(text)
    widget.setObjectName(name)
    widget.setWordWrap(wrap)
    return widget


def button(text, callback, primary=False):
    widget = QPushButton(text)
    if primary:
        widget.setObjectName('primary')
    widget.clicked.connect(callback)
    return widget


class JobTableModel(QAbstractTableModel):
    COLUMNS = [('岗位名称', 'title'), ('公司名称', 'company_name'), ('城市', 'city'),
        ('原始薪资', 'salary_raw'), ('学历', 'education_raw'), ('经验', 'experience_raw'),
        ('来源', 'source'), ('最近采集', 'last_seen_at')]

    def __init__(self, names, parent=None):
        super().__init__(parent)
        self.rows = []
        self.names = names

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.COLUMNS)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self.rows):
            return None
        value = getattr(self.rows[index.row()], self.COLUMNS[index.column()][1])
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return self.names.get(value, value) if index.column() == 6 else value or '—'
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.COLUMNS[section][0]
        return super().headerData(section, orientation, role)

    def set_rows(self, rows):
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        if 0 <= column < len(self.COLUMNS):
            self.layoutAboutToBeChanged.emit()
            previous = self.persistentIndexList()
            identities = [(self.rows[index.row()].record_key, index.column()) for index in previous]
            key = self.COLUMNS[column][1]
            self.rows.sort(key=lambda job: str(getattr(job, key) or ''), reverse=order == Qt.SortOrder.DescendingOrder)
            positions = {job.record_key: row for row, job in enumerate(self.rows)}
            self.changePersistentIndexList(previous, [self.index(positions[identity], column) for identity, column in identities])
            self.layoutChanged.emit()


class MainWindow(QMainWindow):
    gitee_verification_waiting = Signal()

    def __init__(self, paths, parent=None):
        super().__init__(parent)
        self.paths = paths
        self.settings = paths.load_settings()
        self.sources = paths.sources()
        self.repo = Repository(paths.database)
        self.repo.save_sources(self.sources)
        self.backend_sync = BackendSyncClient(self.repo, self.settings)
        self.sync_pending = 0
        self.auto_submitted_tasks = set()
        self.discovery_repo = DiscoveryRepository(paths.discovery_database)
        self.discovery_dictionary_path = paths.discovery_config / 'tech_dictionary.json'
        self.discovery_dictionary = load_dictionary(self.discovery_dictionary_path)
        self.github_content_config = paths.github_content_source()
        self.gitee_content_config = paths.gitee_content_source()
        self.tencent_market_config = paths.tencent_market_source()
        self.discovery_service = JobDiscoveryService(
            self.repo, self.discovery_repo, self.discovery_dictionary_path
        )
        self.jobs = []
        self.current_filters = Filters()
        self.query_generation = 0
        self.io_workers = []
        self.control = None
        self.discovery_control = None
        self.content_control = None
        self.imported_content_batch = None
        self.task_state = ''
        self.browser_busy = False
        self.browser_pending = False
        self.closing = False
        # Destroy the native window while QApplication still exists. Deferring
        # it to Python's shutdown can invert Qt's native object lifetime.
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle('岗位采集工作台 · 本地学习版')
        self.setMinimumSize(1120, 740)
        self.resize(1380, 880)
        self.setStyleSheet(STYLE)
        self.build_ui()
        self.browser_worker = BrowserWorker(paths, self.settings, self.repo, self)
        self.browser_worker.state.connect(self.on_task_state)
        self.browser_worker.message.connect(self.on_browser_message)
        self.browser_worker.error.connect(self.show_error)
        self.browser_worker.diagnostic.connect(self.show_diagnostic)
        self.browser_worker.busy.connect(self.on_browser_busy)
        self.browser_worker.start()
        self.refresh_jobs()
        self.refresh_tasks()
        self.refresh_discovery()
        self.refresh_content()
        self.refresh_backend_records()
        self.statusBar().showMessage('就绪 · 本地数据保留，后台提交按连接设置执行')

    def build_ui(self):
        root = QWidget()
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        rail = QFrame()
        rail.setObjectName('rail')
        rail.setFixedWidth(205)
        rail_layout = QVBoxLayout(rail)
        rail_layout.setContentsMargins(20, 30, 20, 22)
        rail_layout.addWidget(label('岗位工作台', 'brand'))
        rail_layout.addWidget(label('LOCAL JOB WORKSPACE', 'railHint'))
        rail_layout.addSpacing(30)
        self.navigation = QListWidget()
        self.navigation.setObjectName('navigation')
        self.navigation.addItems(['岗位数据', '采集任务', '发现数据', '来源与设置', '后台对接'])
        rail_layout.addWidget(self.navigation, 1)
        rail_layout.addWidget(label('本地存储\nExcel / PDF 导出\n专用浏览器直连配置', 'railHint'))
        outer.addWidget(rail)
        content = QVBoxLayout()
        content.setContentsMargins(27, 24, 27, 18)
        content.setSpacing(18)
        header = QHBoxLayout()
        header.addWidget(label('采集、整理，留在本地。', 'muted'))
        header.addStretch()
        header.addWidget(label('WINDOWS  /  PYTHON', 'muted'))
        content.addLayout(header)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.build_data_page())
        self.stack.addWidget(self.build_collection_page())
        self.discovery_page = DiscoveryPage(self.discovery_dictionary.dictionary_version)
        network_label = {
            'system': '使用 Python 可读取的系统代理',
            'direct': '直连',
            'local_proxy': f"使用本机代理 {self.github_content_config['proxy_url']}",
        }[self.github_content_config['network_mode']]
        self.discovery_page.content_description.setText(
            self.discovery_page.content_description.text()
            + f' GitHub 网络模式：{network_label}；Gitee 直连。'
        )
        self.discovery_page.scan_requested.connect(self.start_discovery_scan)
        self.discovery_page.cancel_requested.connect(self.cancel_discovery_scan)
        self.discovery_page.export_requested.connect(self.export_discovery_batch)
        self.discovery_page.refresh_requested.connect(self.refresh_discovery)
        self.discovery_page.github_collect_requested.connect(self.start_github_collection)
        self.discovery_page.gitee_search_requested.connect(self.start_gitee_search)
        self.discovery_page.gitee_recommend_requested.connect(self.start_gitee_recommendation)
        self.discovery_page.gitee_resume_requested.connect(self.resume_gitee_recommendation)
        self.gitee_verification_waiting.connect(self.show_gitee_verification_waiting)
        self.discovery_page.market_save_requested.connect(self.start_market_case)
        self.discovery_page.market_collect_requested.connect(self.start_tencent_market_collection)
        self.discovery_page.github_cancel_requested.connect(self.cancel_github_collection)
        self.discovery_page.github_export_requested.connect(self.export_content_batch)
        self.discovery_page.github_import_requested.connect(self.import_content_batch)
        self.discovery_page.github_refresh_requested.connect(self.refresh_content)
        self.stack.addWidget(self.discovery_page)
        self.stack.addWidget(self.build_sources_page())
        self.backend_sync_page = BackendSyncPage(self.settings)
        self.backend_sync_page.save_requested.connect(self.save_backend_settings)
        self.backend_sync_page.check_requested.connect(self.check_backend_connection)
        self.backend_sync_page.history_requested.connect(self.sync_history_jobs)
        self.backend_sync_page.discovery_requested.connect(self.sync_latest_discovery)
        self.backend_sync_page.content_requested.connect(self.sync_latest_content)
        self.backend_sync_page.retry_requested.connect(self.retry_backend_batch)
        self.backend_sync_page.refresh_requested.connect(self.refresh_backend_records)
        self.stack.addWidget(self.backend_sync_page)
        content.addWidget(self.stack, 1)
        outer.addLayout(content, 1)
        self.navigation.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.navigation.setCurrentRow(0)
        self.setCentralWidget(root)

    def build_data_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        heading = QHBoxLayout()
        heading.addWidget(label('岗位数据', 'heading'))
        heading.addStretch()
        self.metrics = label('正在读取…', 'metric')
        heading.addWidget(self.metrics)
        layout.addLayout(heading)
        layout.addWidget(label('从文件导入或从采集任务获取岗位，筛选后导出你需要的结果。', 'muted'))
        filters = QFrame()
        filters.setObjectName('panel')
        grid = QGridLayout(filters)
        grid.setContentsMargins(16, 14, 16, 14)
        self.filter_keyword = QLineEdit()
        self.filter_keyword.setPlaceholderText('岗位、公司或描述关键词')
        self.filter_region = RegionSelector(allow_unknown=True)
        self.filter_source = QComboBox()
        self.filter_source.addItem('全部来源', '')
        for source in self.sources:
            self.filter_source.addItem(source['name'], source['id'])
        self.filter_source.addItem('本地导入', 'local_import')
        for col, (name, widget) in enumerate([('关键词', self.filter_keyword), ('省份 / 城市', self.filter_region), ('来源', self.filter_source)]):
            grid.addWidget(label(name), 0, col)
            grid.addWidget(widget, 1, col)
        self.filter_salary_min, self.filter_salary_max = QSpinBox(), QSpinBox()
        for spin in (self.filter_salary_min, self.filter_salary_max):
            spin.setRange(0, 1000000)
            spin.setSingleStep(1000)
            spin.setSpecialValueText('不限')
        salary_row = QHBoxLayout()
        salary_row.addWidget(self.filter_salary_min)
        salary_row.addWidget(label('至'))
        salary_row.addWidget(self.filter_salary_max)
        grid.addWidget(label('月薪区间（元）'), 2, 0)
        grid.addLayout(salary_row, 3, 0)
        self.filter_education, self.filter_experience = QLineEdit(), QLineEdit()
        self.filter_education.setPlaceholderText('如：本科')
        self.filter_experience.setPlaceholderText('如：3-5年')
        grid.addWidget(label('学历 / 经验'), 2, 1)
        education_row = QHBoxLayout()
        education_row.addWidget(self.filter_education)
        education_row.addWidget(self.filter_experience)
        grid.addLayout(education_row, 3, 1)
        self.filter_days = QComboBox()
        for text, days in [('采集时间不限', 0), ('最近 7 天', 7), ('最近 30 天', 30), ('最近 90 天', 90)]:
            self.filter_days.addItem(text, days)
        grid.addWidget(label('采集时间'), 2, 2)
        grid.addWidget(self.filter_days, 3, 2)
        self.filter_unknown = QCheckBox('薪资筛选时包含未知薪资')
        self.filter_unknown.setChecked(True)
        grid.addWidget(self.filter_unknown, 4, 0, 1, 2)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(button('重置', self.reset_filters))
        actions.addWidget(button('应用筛选', self.refresh_jobs, True))
        grid.addLayout(actions, 4, 2)
        self.filter_keyword.returnPressed.connect(self.refresh_jobs)
        layout.addWidget(filters)
        toolbar = QHBoxLayout()
        self.import_button = button('导入 CSV / Excel', self.choose_import)
        toolbar.addWidget(self.import_button)
        toolbar.addWidget(button('导出 Excel', lambda: self.export('xlsx')))
        toolbar.addWidget(button('导出 PDF', lambda: self.export('pdf')))
        self.export_selected = QCheckBox('仅导出选中项')
        toolbar.addWidget(self.export_selected)
        self.selection_count = label('已选 0 条', 'muted')
        self.selection_count.setToolTip('单击行可累加选择或取消；勾选“仅导出选中项”后导出全部选中岗位。')
        toolbar.addWidget(self.selection_count)
        toolbar.addWidget(button('全选', lambda: self.table.selectAll()))
        toolbar.addWidget(button('清空', lambda: self.table.clearSelection()))
        toolbar.addStretch()
        toolbar.addWidget(button('刷新', self.refresh_jobs))
        remove = button('删除选中', self.delete_selected)
        remove.setObjectName('danger')
        toolbar.addWidget(remove)
        layout.addLayout(toolbar)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.table = QTableView()
        self.model = JobTableModel({s['id']: s['name'] for s in self.sources} | {'local_import': '本地导入'}, self)
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        self.table.setToolTip('单击行可选中或取消，连续单击可多选；Ctrl+A 全选当前筛选结果。')
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(43)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for index, width in enumerate((190, 185, 85, 115, 75, 80, 120, 190)):
            self.table.setColumnWidth(index, width)
        self.table.selectionModel().selectionChanged.connect(self.show_detail)
        self.model.layoutChanged.connect(self.show_detail)
        left_layout.addWidget(self.table, 1)
        self.empty = label('还没有岗位数据。导入一份 CSV / Excel，或前往采集任务开始。', 'muted', True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        left_layout.addWidget(self.empty)
        splitter.addWidget(left)
        right = QWidget()
        right.setMinimumWidth(260)
        detail_layout = QVBoxLayout(right)
        detail_layout.setContentsMargins(12, 0, 0, 0)
        detail_layout.addWidget(label('岗位详情', 'section'))
        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setPlaceholderText('选择一条岗位，查看完整描述、来源和采集时间。')
        detail_layout.addWidget(self.detail)
        detail_layout.addWidget(button('复制来源链接', self.copy_source))
        splitter.addWidget(right)
        splitter.setSizes([790, 310])
        layout.addWidget(splitter, 1)
        return page

    def build_collection_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(15)
        layout.addWidget(label('采集任务', 'heading'))
        layout.addWidget(label('先打开专用浏览器并自行登录，再启动小批量采集。需要人工操作时会暂停。', 'muted', True))
        panel = QFrame()
        panel.setObjectName('panel')
        grid = QGridLayout(panel)
        grid.setContentsMargins(20, 18, 20, 18)
        self.collect_source = QComboBox()
        for source in self.sources:
            self.collect_source.addItem(source['name'] + (' · 试验规则' if source['status'] == 'trial' else ' · 待适配'), source['id'])
        self.collect_keyword = QLineEdit('Python')
        self.collect_region = RegionSelector()
        self.collect_limit = QSpinBox()
        self.collect_limit.setRange(1, 200)
        self.collect_limit.setValue(3)
        for col, (name, widget) in enumerate([('国内来源', self.collect_source), ('关键词', self.collect_keyword), ('省份 / 城市', self.collect_region), ('最多采集（条）', self.collect_limit)]):
            grid.addWidget(label(name), 0, col)
            grid.addWidget(widget, 1, col)
        grid.setColumnStretch(1, 1)
        self.collect_current = QCheckBox('采集当前页（在浏览器手动设置关键词和城市）')
        self.collect_details = QCheckBox('读取岗位详情（增加页面访问次数）')
        self.collect_details.setChecked(True)
        self.collect_current.toggled.connect(self.update_collection_buttons)
        grid.addWidget(self.collect_current, 2, 0, 1, 2)
        grid.addWidget(self.collect_details, 2, 2, 1, 2)
        self.source_note = label('', 'muted', True)
        self.source_note.setTextFormat(Qt.TextFormat.PlainText)
        grid.addWidget(self.source_note, 3, 0, 1, 4)
        layout.addWidget(panel)
        row = QHBoxLayout()
        self.open_browser_button = button('打开浏览器并登录', self.open_browser)
        self.start_button = button('开始采集', self.start_collection, True)
        self.pause_button = button('暂停', self.pause_collection)
        self.resume_button = button('人工处理完成，继续', self.resume_collection)
        self.cancel_button = button('停止采集', self.cancel_collection)
        for widget in (self.open_browser_button, self.start_button, self.pause_button, self.resume_button, self.cancel_button):
            row.addWidget(widget)
        row.addStretch()
        layout.addLayout(row)
        state_panel = QFrame()
        state_panel.setObjectName('panel')
        state_layout = QVBoxLayout(state_panel)
        state_layout.setContentsMargins(20, 17, 20, 17)
        self.state_heading = label('准备开始', 'section')
        self.state_message = label('首次使用请在来源与设置中选择浏览器程序。各来源的适配范围见上方备注。', 'muted', True)
        self.state_message.setTextFormat(Qt.TextFormat.PlainText)
        state_layout.addWidget(self.state_heading)
        state_layout.addWidget(self.state_message)
        layout.addWidget(state_panel)
        extra = QHBoxLayout()
        self.diagnostic_button = button('预览局部诊断样本', self.preview_diagnostic)
        self.close_browser_button = button('关闭专用浏览器', self.close_browser)
        extra.addWidget(self.diagnostic_button)
        extra.addWidget(self.close_browser_button)
        extra.addWidget(label('需要诊断时先停止任务；不会自动上传页面或账号数据。', 'muted'), 1)
        layout.addLayout(extra)
        layout.addWidget(label('最近任务', 'section'))
        self.task_table = QTableWidget(0, 6)
        self.task_table.setHorizontalHeaderLabels(['来源', '关键词 / 模式', '状态', '已处理', '开始时间', '说明'])
        self.task_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.task_table.verticalHeader().setVisible(False)
        self.task_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.task_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.task_table, 1)
        self.collect_source.currentIndexChanged.connect(self.update_source_note)
        self.collect_region.changed.connect(self.update_collection_buttons)
        self.update_source_note()
        self.update_collection_buttons()
        return page

    def build_sources_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        layout.addWidget(label('来源与设置', 'heading'))
        layout.addWidget(label('配置本工具使用的浏览器、访问间隔和字体，查看各来源的实际适配状态。', 'muted', True))
        row = QHBoxLayout()
        self.settings_button = button('配置浏览器与采集参数', self.edit_settings, True)
        row.addWidget(self.settings_button)
        row.addStretch()
        layout.addLayout(row)
        self.settings_summary = label('', 'muted', True)
        self.settings_summary.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.settings_summary)
        table = QTableWidget(len(self.sources), 4)
        table.setHorizontalHeaderLabels(['国内来源', '技术状态', '协议记录', '备注'])
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setWordWrap(True)
        table.verticalHeader().setVisible(False)
        for row_number, source in enumerate(self.sources):
            values = [source['name'], '试验规则 / 覆盖范围见备注' if source['status'] == 'trial' else '待适配', source.get('protocol_version', '待核对'), source['note']]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(source.get('protocol_url', '') if col == 2 else value)
                table.setItem(row_number, col, item)
            table.setRowHeight(row_number, 83)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(table, 1)
        layout.addWidget(label('协议限制与技术状态分别记录。完成登录或技术适配不代表平台授权，也不保证不会触发风控。\n来源配置：config/sources.json；个人设置：config/settings.json；数据：data/jobs.sqlite3。', 'muted', True))
        self.update_settings_summary()
        return page

    def async_call(self, function, on_result, description='处理中…', on_finished=None):
        worker = FunctionWorker(function, self)
        self.io_workers.append(worker)
        self.statusBar().showMessage(description)
        worker.result.connect(on_result)
        worker.error.connect(self.show_error)
        if on_finished:
            worker.finished.connect(on_finished)
        worker.finished.connect(lambda w=worker: self.finish_worker(w))
        worker.start()

    def refresh_discovery(self):
        if self.closing or self.discovery_page.busy:
            return
        self.discovery_page.set_busy(True)

        def read_latest():
            run = self.discovery_repo.latest_completed_run('job_tech')
            if run is None:
                return None, [], []
            return run, self.discovery_repo.mentions(run.run_id), self.discovery_repo.issues(run.run_id)

        def apply(result):
            if not self.closing:
                self.discovery_page.show_run(*result)

        self.async_call(
            read_latest,
            apply,
            '正在读取本地发现数据…',
            lambda: self.discovery_page.set_busy(False) if not self.closing else None,
        )

    def start_discovery_scan(self):
        if self.discovery_page.busy:
            return
        self.discovery_control = TaskControl()
        self.discovery_page.set_busy(True, cancellable=True)

        def scan():
            run = self.discovery_service.scan_all(self.discovery_control)
            return run, self.discovery_repo.mentions(run.run_id), self.discovery_repo.issues(run.run_id)

        def apply(result):
            if not self.closing:
                self.discovery_page.show_run(*result)
                if self.settings.backend_auto_discovery:
                    self.schedule_backend_sync(lambda: self.submit_discovery_run(result[0]))
                self.statusBar().showMessage('岗位技术扫描完成，结果仅保存在本机')

        def finished():
            self.discovery_control = None
            if not self.closing:
                self.discovery_page.set_busy(False)

        self.async_call(scan, apply, '正在扫描全部当前本地岗位…', finished)

    def cancel_discovery_scan(self):
        if self.discovery_control:
            self.discovery_control.cancel()
            self.discovery_page.cancel_button.setEnabled(False)
            self.statusBar().showMessage('正在停止本地岗位技术扫描…')

    def export_discovery_batch(self):
        run = self.discovery_page.current_run
        if self.discovery_page.busy or run is None or run.status != 'completed':
            return
        default_path = self.paths.discovery_exports / f'{run.run_id}.json'
        chosen, _ = QFileDialog.getSaveFileName(
            self,
            '导出发现候选批次',
            str(default_path),
            'JSON 批次 (*.json)',
        )
        if not chosen:
            return
        path = Path(chosen)
        if not path.suffix:
            path = path.with_suffix('.json')
        self.discovery_page.set_busy(True)

        def write():
            batch = build_job_tech_batch(
                run,
                self.discovery_repo.mentions(run.run_id),
                self.discovery_repo.issues(run.run_id),
            )
            digest = export_batch(batch, path)
            self.discovery_repo.record_export(
                run.run_id,
                DISCOVERY_SCHEMA_VERSION,
                path,
                digest,
                batch.generated_at,
            )
            return path

        def complete(exported_path):
            if not self.closing:
                QMessageBox.information(self, '导出完成', f'本地候选批次已保存到：\n{exported_path}')

        self.async_call(
            write,
            complete,
            '正在导出本地发现候选…',
            lambda: self.discovery_page.set_busy(False) if not self.closing else None,
        )

    def refresh_content(self):
        if self.closing or self.discovery_page.content_busy:
            return
        self.discovery_page.set_content_busy(True)

        def read_latest():
            run = self.discovery_repo.latest_completed_run('content')
            if run is None:
                return None, [], [], False, False
            candidates = self.discovery_repo.content_candidates(run.run_id)
            legacy = not run.dictionary_version and any(item.tech_keys for item in candidates)
            unverified = False
            if not legacy and any(item.tech_keys for item in candidates):
                try:
                    snapshot = TechDictionary.from_mapping(
                        self.discovery_repo.dictionary_snapshot_for_sync(run.dictionary_version)
                    )
                except ValueError:
                    unverified = True
                else:
                    known = {term.tech_key for term in snapshot.terms}
                    unverified = any(key not in known for item in candidates for key in item.tech_keys)
            return run, candidates, self.discovery_repo.content_issues(run.run_id), legacy, unverified

        def apply(result):
            if not self.closing:
                self.imported_content_batch = None
                run, candidates, issues, legacy, unverified = result
                self.discovery_page.show_content_run(
                    run, candidates, issues,
                    legacy_preview=legacy, dictionary_unverified=unverified,
                )

        self.async_call(
            read_latest,
            apply,
            '正在读取本地内容候选…',
            lambda: self.discovery_page.set_content_busy(False) if not self.closing else None,
        )

    def start_github_collection(self, keyword: str, limit: int, sort: str, created_after: str):
        if self.closing or self.discovery_page.content_busy:
            return
        if limit > self.github_content_config['max_items']:
            self.show_error(f"GitHub 单次上限为 {self.github_content_config['max_items']} 条")
            return
        try:
            # GitHub's global qualifier-only search can return incomplete, misleading rankings.
            # This verified high-star pool keeps both blank-keyword sort modes reproducible.
            criteria = ContentCriteria(keyword.strip() or 'stars:>100000', limit, sort, created_after)
        except ValueError as exc:
            self.show_error(str(exc))
            return
        self.content_control = TaskControl()
        self.discovery_page.set_content_busy(True)
        self.discovery_page.content_hint.setText(
            '正在读取 GitHub 官方公开仓库元数据…'
            if keyword.strip() else '正在读取 Star 超过 10 万的公开仓库并按所选方式排序…'
        )

        def collect():
            config = self.github_content_config
            client = HttpClient(
                config['base_url'],
                tuple(config['allowed_domains']),
                config['network_mode'],
                proxy_url=config.get('proxy_url', ''),
            )
            try:
                source = GithubSource(client, self.discovery_dictionary)
                run = ContentDiscoveryService(self.discovery_repo, source).collect(
                    criteria, self.content_control,
                )
                return (
                    run,
                    self.discovery_repo.content_candidates(run.run_id),
                    self.discovery_repo.content_issues(run.run_id),
                )
            finally:
                client.close()

        def apply(result):
            if not self.closing:
                run, candidates, issues = result
                self.imported_content_batch = None
                self.discovery_page.show_content_run(run, candidates, issues)
                self.schedule_completed_content_sync(run)
                self.statusBar().showMessage(
                    f'GitHub 本次{run.status}：候选 {len(candidates)} 条，问题 {len(issues)} 条'
                )

        def finished():
            self.content_control = None
            if not self.closing:
                self.discovery_page.set_content_busy(False)

        self.async_call(collect, apply, '正在采集 GitHub 公开仓库候选…', finished)

    def start_gitee_search(self, keyword: str, limit: int, sort: str):
        if self.closing or self.discovery_page.content_busy:
            return
        if limit > self.gitee_content_config['max_items']:
            self.show_error(f"Gitee 单次上限为 {self.gitee_content_config['max_items']} 条")
            return
        try:
            criteria = ContentCriteria(keyword, limit, sort)
        except ValueError as exc:
            self.show_error(str(exc))
            return
        self.start_gitee_collection(criteria)

    def start_gitee_recommendation(self, limit: int):
        if self.closing or self.discovery_page.content_busy:
            return
        if limit > self.gitee_content_config['max_items']:
            self.show_error(f"Gitee 单次上限为 {self.gitee_content_config['max_items']} 条")
            return
        self.start_gitee_collection({'mode': 'recommended', 'limit': limit})

    def start_gitee_collection(self, repository_url: ContentCriteria | dict):
        if self.closing or self.discovery_page.content_busy:
            return
        self.content_control = TaskControl()
        self.discovery_page.set_content_busy(True)
        self.discovery_page.content_hint.setText(
            '正在搜索 Gitee 官方公开仓库并读取 README；排序仅限本次返回池…'
            if isinstance(repository_url, ContentCriteria)
            else '正在读取 Gitee 推荐项目页面及仓库 README…'
        )

        def collect():
            config = self.gitee_content_config
            client = HttpClient(
                config['base_url'], tuple(config['allowed_domains']),
                config['network_mode'], proxy_url=config.get('proxy_url', ''),
            )
            browser = (
                BrowserSession(self.paths, self.settings, runtime_name='gitee-recommended-runtime.json')
                if isinstance(repository_url, dict) else None
            )
            try:
                source = GiteeSource(
                    client, self.discovery_dictionary, recommendation_browser=browser,
                    verification_timeout=self.settings.task_timeout_minutes * 60,
                )
                def notify(update):
                    if update.get('stage') == 'waiting_verification':
                        self.gitee_verification_waiting.emit()
                run = ContentDiscoveryService(self.discovery_repo, source).collect(
                    repository_url, self.content_control, notify,
                )
                return (
                    run,
                    self.discovery_repo.content_candidates(run.run_id),
                    self.discovery_repo.content_issues(run.run_id),
                )
            finally:
                if browser is not None:
                    browser.close()
                client.close()

        def apply(result):
            if not self.closing:
                run, candidates, issues = result
                self.imported_content_batch = None
                self.discovery_page.show_content_run(run, candidates, issues)
                self.schedule_completed_content_sync(run)
                self.statusBar().showMessage(
                    f'Gitee 本次{run.status}：候选 {len(candidates)} 条，问题 {len(issues)} 条'
                )

        def finished():
            self.content_control = None
            if not self.closing:
                self.discovery_page.set_content_busy(False)

        self.async_call(collect, apply, '正在采集 Gitee 公开仓库候选…', finished)

    def show_gitee_verification_waiting(self):
        if self.closing or self.content_control is None or self.content_control.cancel_event.is_set():
            return
        self.discovery_page.gitee_resume_button.setVisible(True)
        self.discovery_page.gitee_resume_button.setEnabled(True)
        self.discovery_page.content_hint.setText(
            '请在专用浏览器自行完成 Gitee 页面验证，再点击“验证完成，继续”；'
            '等待期间不会自动请求推荐页。'
        )

    def resume_gitee_recommendation(self):
        if self.content_control:
            self.discovery_page.gitee_resume_button.setVisible(False)
            self.discovery_page.content_hint.setText('正在检查人工处理后的推荐页…')
            self.content_control.resume()

    def start_market_case(self, values: dict):
        if self.closing or self.discovery_page.content_busy:
            return
        self.content_control = TaskControl()
        self.discovery_page.set_content_busy(True)
        self.discovery_page.content_hint.setText('正在将人工核对的案例材料保存为本地待复核候选…')

        def collect():
            source = ManualMarketSource(self.discovery_dictionary)
            run = ContentDiscoveryService(self.discovery_repo, source).collect(
                values, self.content_control,
            )
            return (
                run,
                self.discovery_repo.content_candidates(run.run_id),
                self.discovery_repo.content_issues(run.run_id),
            )

        def apply(result):
            if not self.closing:
                run, candidates, issues = result
                self.imported_content_batch = None
                self.discovery_page.show_content_run(run, candidates, issues)
                self.schedule_completed_content_sync(run)
                self.statusBar().showMessage(
                    f'人工案例本次{run.status}：候选 {len(candidates)} 条，问题 {len(issues)} 条'
                )

        def finished():
            self.content_control = None
            if not self.closing:
                self.discovery_page.set_content_busy(False)

        self.async_call(collect, apply, '正在保存人工整理案例…', finished)

    def start_tencent_market_collection(self, mode: str, limit: int):
        if self.closing or self.discovery_page.content_busy:
            return
        if limit > self.tencent_market_config['max_items']:
            self.show_error(f"案例单次上限为 {self.tencent_market_config['max_items']} 条")
            return
        self.content_control = TaskControl()
        self.discovery_page.set_content_busy(True)
        self.discovery_page.content_hint.setText('正在读取腾讯云公开案例专栏首屏与有限详情…')

        def collect():
            browser = BrowserSession(
                self.paths, self.settings, runtime_name='tencent-market-browser-runtime.json',
            )
            try:
                client = TencentBrowserClient(browser)
                source = TencentMarketSource(client, self.discovery_dictionary)
                run = ContentDiscoveryService(self.discovery_repo, source).collect(
                    {'mode': mode, 'limit': limit}, self.content_control,
                )
                return (
                    run,
                    self.discovery_repo.content_candidates(run.run_id),
                    self.discovery_repo.content_issues(run.run_id),
                )
            finally:
                browser.close()

        def apply(result):
            if not self.closing:
                run, candidates, issues = result
                self.imported_content_batch = None
                self.discovery_page.show_content_run(run, candidates, issues)
                self.schedule_completed_content_sync(run)
                self.statusBar().showMessage(
                    f'腾讯云案例本次{run.status}：候选 {len(candidates)} 条，问题 {len(issues)} 条'
                )

        def finished():
            self.content_control = None
            if not self.closing:
                self.discovery_page.set_content_busy(False)

        self.async_call(collect, apply, '正在采集腾讯云公开案例…', finished)

    def cancel_github_collection(self):
        if self.content_control:
            self.content_control.cancel()
            self.discovery_page.github_cancel_button.setEnabled(False)
            self.discovery_page.gitee_resume_button.setVisible(False)
            self.statusBar().showMessage('正在停止内容采集，已取得的结果会保留在本机…')

    def import_content_batch(self):
        if self.closing or self.discovery_page.content_busy:
            return
        chosen, _ = QFileDialog.getOpenFileName(
            self, '导入内容候选 JSON 预览', str(self.paths.discovery_exports), 'JSON 批次 (*.json)',
        )
        if not chosen:
            return
        path = Path(chosen)
        self.discovery_page.set_content_busy(True)

        def read():
            if path.stat().st_size > 5 * 1024 * 1024:
                raise ValueError('候选 JSON 超过 5 MiB 上限')
            from app.discovery.batches import parse_content_batch
            return parse_content_batch(path.read_bytes(), dictionary=self.discovery_dictionary)

        def apply(result):
            if not self.closing:
                run, candidates, _snapshots, issues, batch = result
                self.imported_content_batch = batch
                self.discovery_page.show_content_run(
                    run, candidates, issues, imported=True,
                    legacy_preview=batch.legacy_preview,
                    dictionary_unverified=batch.dictionary_unverified,
                )
                self.statusBar().showMessage(f'已导入预览 {len(candidates)} 条候选；本地数据库未修改')

        self.async_call(
            read, apply, '正在导入候选 JSON 预览…',
            lambda: self.discovery_page.set_content_busy(False) if not self.closing else None,
        )

    def export_content_batch(self):
        run = self.discovery_page.current_content_run
        if (
            self.discovery_page.content_busy or run is None or run.status != 'completed'
            or self.discovery_page.content_export_blocked
            or (
                self.imported_content_batch is not None
                and (self.imported_content_batch.legacy_preview
                     or self.imported_content_batch.dictionary_unverified)
            )
        ):
            return
        default_path = self.paths.discovery_exports / f'{run.run_id}.json'
        chosen, _ = QFileDialog.getSaveFileName(
            self, '导出内容候选批次', str(default_path), 'JSON 批次 (*.json)',
        )
        if not chosen:
            return
        path = Path(chosen)
        if not path.suffix:
            path = path.with_suffix('.json')
        self.discovery_page.set_content_busy(True)
        imported_batch = self.imported_content_batch

        def write():
            batch = imported_batch or build_content_batch(
                run,
                self.discovery_repo.content_candidates(run.run_id),
                self.discovery_repo.content_snapshots(run.run_id),
                self.discovery_repo.content_issues(run.run_id),
            )
            dictionary = self.discovery_dictionary if imported_batch is not None else None
            if imported_batch is None and run.dictionary_version:
                dictionary = TechDictionary.from_mapping(
                    self.discovery_repo.dictionary_snapshot_for_sync(run.dictionary_version)
                )
            digest = export_batch(batch, path, dictionary=dictionary)
            if imported_batch is None:
                self.discovery_repo.record_export(
                    run.run_id, DISCOVERY_SCHEMA_VERSION, path, digest, batch.generated_at,
                )
            return path

        def complete(exported_path):
            if not self.closing:
                QMessageBox.information(
                    self, '导出完成',
                    f'本地候选批次已保存到：\n{exported_path}\n'
                    '当前后台尚未接收内容候选；此文件仅是目标协议格式。',
                )

        self.async_call(
            write, complete, '正在导出本地内容候选…',
            lambda: self.discovery_page.set_content_busy(False) if not self.closing else None,
        )

    def finish_worker(self, worker):
        if worker in self.io_workers:
            self.io_workers.remove(worker)
        worker.deleteLater()

    def selected_jobs(self):
        indexes = sorted(self.table.selectionModel().selectedRows(), key=lambda index: index.row())
        return [self.model.rows[index.row()] for index in indexes]

    def read_filters(self):
        days = self.filter_days.currentData()
        lo, hi = self.filter_salary_min.value(), self.filter_salary_max.value()
        if lo and hi and lo > hi:
            raise ValueError('月薪下限不能高于上限')
        province_code, city_code = self.filter_region.codes()
        return Filters(keyword=self.filter_keyword.text(), province_code=province_code, city_code=city_code, source=self.filter_source.currentData(),
            education=self.filter_education.text(), experience=self.filter_experience.text(), salary_min=lo or None, salary_max=hi or None,
            include_unknown=self.filter_unknown.isChecked(), since=(datetime.now().astimezone() - timedelta(days=days)).isoformat(timespec='seconds') if days else '')

    def refresh_jobs(self):
        if self.closing:
            return
        try:
            filters = self.read_filters()
        except ValueError as exc:
            self.show_error(str(exc))
            return
        self.query_generation += 1
        generation = self.query_generation
        def apply(result):
            if generation != self.query_generation or self.closing:
                return
            rows, total = result
            self.jobs, self.current_filters = rows, filters
            self.model.set_rows(list(rows))
            self.selection_count.setText('已选 0 条')
            self.empty.setVisible(not rows)
            self.metrics.setText(f'{len(rows):,} 条匹配  /  共 {total:,} 条')
            self.detail.clear()
            self.statusBar().showMessage(f'已读取 {len(rows)} 条筛选结果')
        self.async_call(lambda: (self.repo.query(filters), self.repo.count()), apply, '正在读取岗位…')

    def reset_filters(self):
        for edit in (self.filter_keyword, self.filter_education, self.filter_experience):
            edit.clear()
        self.filter_region.reset()
        self.filter_source.setCurrentIndex(0)
        self.filter_days.setCurrentIndex(0)
        self.filter_salary_min.setValue(0)
        self.filter_salary_max.setValue(0)
        self.filter_unknown.setChecked(True)
        self.refresh_jobs()

    def show_detail(self):
        jobs = self.selected_jobs()
        self.selection_count.setText(f'已选 {len(jobs)} 条')
        if not jobs:
            self.detail.clear()
            return
        current = self.table.currentIndex()
        current_job = self.model.rows[current.row()] if current.isValid() else None
        job = next((job for job in jobs if current_job and job.record_key == current_job.record_key), jobs[0])
        self.detail.setPlainText(f'{job.title}\n{job.company_name}\n\n城市：{job.city or "未提供"} {job.district}\n工作地址：{job.work_address or "未提供"}\n薪资：{job.salary_raw or "未提供"}\n学历：{job.education_raw or "未提供"}\n经验：{job.experience_raw or "未提供"}\n来源：{job.source}\n发布时间：{job.published_raw or job.published_at or "未提供"}\n首次采集：{job.first_seen_at}\n最近采集：{job.last_seen_at}\n\n{job.description or "此条记录未提供岗位详情"}\n\n{job.source_url}')

    def copy_source(self):
        selected = self.selected_jobs()
        if selected and selected[0].source_url:
            QGuiApplication.clipboard().setText(selected[0].source_url)
            self.statusBar().showMessage('来源链接已复制')

    def choose_import(self):
        path, _ = QFileDialog.getOpenFileName(self, '选择岗位文件', str(self.paths.root), '岗位数据 (*.csv *.xlsx)')
        if not path:
            return
        encoding, sheet = 'utf-8-sig', ''
        if Path(path).suffix.lower() == '.csv':
            encoding, accepted = QInputDialog.getItem(self, 'CSV 编码', '选择文件编码（乱码时可换编码重新导入）', ['utf-8-sig', 'gb18030', 'utf-8'], 0, False)
            if not accepted:
                return
        else:
            from openpyxl import load_workbook
            try:
                book = load_workbook(path, read_only=True, keep_links=False)
                sheets = book.sheetnames
                book.close()
            except Exception:
                self.show_error('Excel 文件无法读取，请确认文件格式和文件是否完整')
                return
            if len(sheets) > 1:
                sheet, accepted = QInputDialog.getItem(self, '选择工作表', '导入哪个工作表？', sheets, 0, False)
                if not accepted:
                    return
        self.async_call(lambda: preview_file(Path(path), encoding, sheet), self.map_import, '正在读取导入预览…')

    def map_import(self, preview):
        dialog = ImportDialog(preview, self)
        if dialog.exec() == dialog.DialogCode.Accepted:
            mapping = dialog.mapping()
            self.async_call(lambda: import_jobs(preview, mapping), self.confirm_import, '正在校验字段与公式…')

    def confirm_import(self, result):
        jobs, errors = result
        message = f'有效岗位 {len(jobs)} 条，错误行 {len(errors)} 条。\n同来源同岗位将更新，其他来源保留独立记录。'
        if errors:
            message += '\n\n' + '\n'.join(errors[:8]) + ('\n…更多错误行未列出' if len(errors) > 8 else '')
        if not jobs:
            QMessageBox.information(self, '没有可导入记录', message)
            return
        if QMessageBox.question(self, '确认导入有效记录', message + '\n\n写入以上有效岗位？') != QMessageBox.StandardButton.Yes:
            return
        def write():
            inserted = sum(self.repo.upsert(job) for job in jobs)
            return len(jobs), inserted
        def complete(counts):
            self.refresh_jobs()
            QMessageBox.information(self, '导入完成', f'处理 {counts[0]} 条：新增 {counts[1]} 条，更新 {counts[0] - counts[1]} 条。')
        self.async_call(write, complete, '正在写入本地数据库…')

    def export(self, kind):
        jobs = self.selected_jobs() if self.export_selected.isChecked() else list(self.jobs)
        if not jobs:
            self.show_error('没有可导出的岗位；仅导出选中项时请先选择记录')
            return
        path, _ = QFileDialog.getSaveFileName(self, '保存岗位报告', str(self.paths.exports / f'岗位报告_{datetime.now():%Y%m%d_%H%M%S}.{kind}'), f'{"Excel" if kind == "xlsx" else "PDF"} (*.{kind})')
        if not path:
            return
        if not Path(path).suffix:
            path += '.' + kind
        criteria = {'关键词': self.current_filters.keyword or '不限', '省份 / 城市': catalog().description(self.current_filters.province_code, self.current_filters.city_code), '来源': self.current_filters.source or '全部', '范围': '选中记录' if self.export_selected.isChecked() else '当前全部筛选结果'}
        function = (lambda: export_excel(jobs, Path(path), criteria)) if kind == 'xlsx' else (lambda: export_pdf(jobs, Path(path), criteria, self.settings.font_path))
        self.async_call(function, lambda _: QMessageBox.information(self, '导出完成', f'已导出 {len(jobs)} 条岗位到：\n{path}'), '正在生成报告…')

    def delete_selected(self):
        jobs = self.selected_jobs()
        if jobs and QMessageBox.question(self, '删除本地记录', f'删除选中的 {len(jobs)} 条本地岗位记录？') == QMessageBox.StandardButton.Yes:
            keys = [job.record_key for job in jobs]
            self.async_call(lambda: self.repo.delete(keys), lambda _: self.refresh_jobs(), '正在删除本地记录…')

    def source(self):
        return next(source for source in self.sources if source['id'] == self.collect_source.currentData())

    def update_source_note(self):
        source = self.source()
        required = bool(source.get('requires_details') or source.get('detail_fields_required'))
        if required:
            self.collect_details.setChecked(True)
        self.collect_details.setEnabled(not required)
        self.collect_details.setText('读取岗位详情（此来源必需）' if required else '读取岗位详情（增加页面访问次数）')
        self.update_collection_buttons()

    def update_region_hint(self):
        source = self.source()
        if self.collect_current.isChecked():
            hint = '当前页模式：以浏览器中的关键词和地区为准，表单省市不参与筛选。'
        elif self.collect_region.source_city(source) is None:
            hint = f'所选地区：{self.collect_region.description()}。此来源尚未支持该地区自动搜索；请在浏览器人工筛选后勾选“采集当前页”。'
        else:
            hint = f'所选地区：{self.collect_region.description()}。将使用本来源已确认的地区搜索条件。'
        self.source_note.setText(source['note'] + '\n' + hint + '\n首次联调建议 1—3 条；普通弹窗与验证码需根据实际页面识别。')

    def submit_browser(self, action, **kwargs):
        self.browser_pending = True
        self.update_collection_buttons()
        self.browser_worker.submit(action, self.source(), **kwargs)

    def open_browser(self):
        if not self.settings.browser_path:
            self.edit_settings()
            if not self.settings.browser_path:
                return
        self.submit_browser('open')
        self.state_heading.setText('正在打开专用浏览器')
        self.state_message.setText('使用独立用户目录与禁用代理参数；请在出现的网站窗口中自行登录或注册。')

    def start_collection(self):
        if not self.settings.browser_path:
            self.show_error('请先配置浏览器程序并打开浏览器完成登录')
            return
        current = self.collect_current.isChecked()
        city = '' if current else self.collect_region.source_city(self.source())
        if city is None:
            self.show_error('此来源尚未支持所选省市自动搜索，请在浏览器人工筛选后勾选“采集当前页”')
            return
        criteria = Criteria(keyword='' if current else self.collect_keyword.text().strip(), city=city, limit=self.collect_limit.value(), current_page=current, details=self.collect_details.isChecked())
        if not current and not criteria.keyword:
            self.show_error('请输入关键词，或选择采集当前页')
            return
        self.control = TaskControl()
        self.task_state = 'running'
        self.submit_browser('collect', criteria=criteria, control=self.control)

    def pause_collection(self):
        if self.control:
            self.control.pause()
            self.state_message.setText('已请求暂停，当前页面动作结束后暂停。')

    def resume_collection(self):
        if self.control:
            self.control.resume()
            self.task_state = 'running'
            self.update_collection_buttons()

    def cancel_collection(self):
        if self.control:
            self.control.cancel()
            self.state_message.setText('正在停止，已经完成的岗位会保留；等待当前页面动作返回。')

    def close_browser(self):
        self.submit_browser('close')

    def preview_diagnostic(self):
        self.submit_browser('diagnostic')

    def show_diagnostic(self, payload):
        DiagnosticDialog(payload, self.paths.data, self).exec()

    def on_browser_message(self, message):
        self.state_heading.setText('浏览器就绪')
        self.state_message.setText(message)
        self.statusBar().showMessage(message)

    def on_browser_busy(self, busy):
        self.browser_pending = False
        self.browser_busy = busy
        self.update_collection_buttons()
        if not busy:
            self.refresh_tasks()
            self.refresh_jobs()

    def on_task_state(self, task):
        self.task_state = task['status']
        self.state_heading.setText(STATE_NAMES.get(task['status'], task['status']))
        self.state_message.setText(task['message'])
        self.statusBar().showMessage(task['message'])
        self.update_collection_buttons()
        if task['status'] in TERMINAL_STATES:
            self.refresh_tasks()
            self.refresh_jobs()
        if task['status'] == 'completed' and self.settings.backend_auto_jobs and task['id'] not in self.auto_submitted_tasks:
            self.auto_submitted_tasks.add(task['id'])
            self.schedule_backend_sync(lambda current=dict(task): self.backend_sync.push_task(current))

    def update_collection_buttons(self):
        if not hasattr(self, 'start_button'):
            return
        busy = self.browser_busy or self.browser_pending
        active = busy and self.task_state not in TERMINAL_STATES and self.control is not None
        current = self.collect_current.isChecked()
        supported = current or self.collect_region.source_city(self.source()) is not None
        self.update_region_hint()
        self.open_browser_button.setEnabled(not busy)
        self.start_button.setEnabled(not busy and supported and bool(self.source().get('selectors')))
        self.pause_button.setEnabled(active and self.task_state == 'running')
        self.resume_button.setEnabled(active and self.task_state in WAIT_STATES)
        self.cancel_button.setEnabled(active)
        self.diagnostic_button.setEnabled(not busy)
        self.close_browser_button.setEnabled(not busy)
        self.collect_source.setEnabled(not busy)
        self.collect_current.setEnabled(not busy)
        self.collect_keyword.setEnabled(not busy and not current)
        self.collect_region.setEnabled(not busy and not current)
        self.collect_limit.setEnabled(not busy)
        self.collect_details.setEnabled(not busy and not (self.source().get('requires_details') or self.source().get('detail_fields_required')))
        if hasattr(self, 'settings_button'):
            self.settings_button.setEnabled(not busy)

    def refresh_tasks(self):
        rows = self.repo.tasks()
        self.task_table.setRowCount(len(rows))
        for row, task in enumerate(rows):
            values = [task['source'], task.get('keyword') or '当前页', STATE_NAMES.get(task['status'], task['status']), str(task.get('count', 0)), task.get('started_at', ''), task.get('message', '')]
            for col, value in enumerate(values):
                self.task_table.setItem(row, col, QTableWidgetItem(value))

    def save_backend_settings(self):
        page = self.backend_sync_page
        candidate = replace(self.settings, backend_url=page.url.text().strip(),
                            backend_token=page.token.text().strip() or self.settings.backend_token,
                            backend_auto_jobs=page.auto_jobs.isChecked(),
                            backend_auto_discovery=page.auto_discovery.isChecked())
        try:
            BackendSyncClient.validate_configuration(candidate)
        except ValueError as exception:
            QMessageBox.warning(self, '连接配置', str(exception))
            return
        self.paths.save_settings(candidate)
        self.settings.__dict__.update(candidate.__dict__)
        page.show_settings(self.settings)
        self.statusBar().showMessage('后台连接设置已保存，凭据不回显')

    def refresh_backend_records(self):
        if not self.closing:
            self.backend_sync_page.show_records(self.backend_sync.records())

    def schedule_backend_sync(self, function):
        if self.closing:
            return
        self.sync_pending += 1
        self.backend_sync_page.set_busy(True)
        def apply(result):
            if self.closing:
                return
            if result is None:
                message = '本次没有需要提交的数据'
            elif result.get('status') == 'FAILED':
                message = '后台提交失败，已保留待补交记录：' + result['error']
            elif result.get('technology_sync', {}).get('status') == 'FAILED':
                message = '岗位已接收；技术证据提交失败，请在发送记录中选择发现批次补交：' + result['technology_sync']['error']
            elif 'name' in result:
                message = '连接成功：' + result['name']
            else:
                message = '后台已接收；新增岗位保持下架，内容和技术处理结果请在后台查看'
            self.backend_sync_page.message.setText(message)
            self.statusBar().showMessage(message)
            self.refresh_backend_records()
        def finished():
            self.sync_pending -= 1
            if not self.closing:
                self.backend_sync_page.set_busy(self.sync_pending > 0)
                self.refresh_backend_records()
        self.async_call(function, apply, '正在对接光合后台…', finished)

    def check_backend_connection(self):
        self.schedule_backend_sync(self.backend_sync.check_connection)

    def sync_history_jobs(self):
        self.schedule_backend_sync(lambda: self.backend_sync.push_jobs(self.repo.query(Filters())))

    def submit_discovery_run(self, run):
        batch = build_job_tech_batch(run, self.discovery_repo.mentions(run.run_id), self.discovery_repo.issues(run.run_id))
        dictionary = self.discovery_repo.dictionary_snapshot_for_sync(run.dictionary_version)
        return self.backend_sync.push_discovery(batch, dictionary)

    def schedule_completed_content_sync(self, run):
        if run.status == 'completed' and self.settings.backend_auto_discovery:
            self.schedule_backend_sync(lambda: self.submit_content_run(run))

    def submit_content_run(self, run):
        candidates = self.discovery_repo.content_candidates(run.run_id)
        if not candidates:
            return None
        batch = build_content_batch(run, candidates, self.discovery_repo.content_snapshots(run.run_id),
                                    self.discovery_repo.content_issues(run.run_id))
        dictionary = (self.discovery_repo.dictionary_snapshot_for_sync(run.dictionary_version)
                      if run.dictionary_version else None)
        typed_dictionary = TechDictionary.from_mapping(dictionary) if dictionary is not None else None
        serialize_batch(batch, dictionary=typed_dictionary)
        return self.backend_sync.push_discovery(batch, dictionary)

    def sync_latest_content(self):
        def submit():
            run = self.discovery_repo.latest_completed_run('content')
            if run is None:
                raise ValueError('还没有成功的内容采集运行，请先完成一次采集')
            return self.submit_content_run(run)
        self.schedule_backend_sync(submit)

    def sync_latest_discovery(self):
        def submit():
            run = self.discovery_repo.latest_completed_run('job_tech')
            if run is None:
                raise ValueError('还没有成功的发现运行，请先手动扫描岗位')
            return self.submit_discovery_run(run)
        self.schedule_backend_sync(submit)

    def retry_backend_batch(self, kind, run_id):
        self.schedule_backend_sync(lambda: self.backend_sync.retry(kind, run_id))

    def edit_settings(self):
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec() == dialog.DialogCode.Accepted:
            new_settings = dialog.value()
            if new_settings.browser_path and not Path(new_settings.browser_path).is_file():
                self.show_error('浏览器程序路径不存在，设置未保存')
                return
            self.paths.save_settings(new_settings)
            # Worker and browser share this object; mutate only while idle.
            self.settings.__dict__.update(new_settings.__dict__)
            self.update_settings_summary()

    def update_settings_summary(self):
        self.settings_summary.setText(f'浏览器：{self.settings.browser_path or "尚未选择"}\n访问间隔：{self.settings.interval_seconds:g} 秒　列表上限：{self.settings.page_limit} 页　任务时长：{self.settings.task_timeout_minutes} 分钟\n字体：{self.settings.font_path or "项目自带 Noto Sans SC（OFL）"}\n数据库：{self.paths.database}')

    def show_error(self, message):
        self.statusBar().showMessage(message)
        if not self.closing:
            QMessageBox.warning(self, '操作未完成', message)

    def closeEvent(self, event):
        if not self.closing:
            self.closing = True
            if self.discovery_control:
                self.discovery_control.cancel()
            if self.content_control:
                self.content_control.cancel()
            self.browser_worker.shutdown()
            self.statusBar().showMessage('正在停止任务并关闭本工具的浏览器，请稍候…')
            self.setEnabled(False)
            QTimer.singleShot(100, self.poll_close)
        if self.browser_worker.isRunning() or any(worker.isRunning() for worker in self.io_workers):
            event.ignore()
        else:
            event.accept()

    def poll_close(self):
        if self.browser_worker.isRunning() or any(worker.isRunning() for worker in self.io_workers):
            QTimer.singleShot(100, self.poll_close)
        else:
            self.close()
