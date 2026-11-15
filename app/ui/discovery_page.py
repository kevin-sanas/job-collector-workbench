from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.discovery.models import DiscoveryRun, ExtractionIssue, JobTechMention


README_STATUS_LABELS = {
    'fetched': '已获取',
    'not_found': '未找到',
    'failed': '获取失败',
    'too_large': '超过大小上限',
    'rate_limited': '访问限流',
}


class DiscoveryPage(QWidget):
    scan_requested = Signal()
    cancel_requested = Signal()
    export_requested = Signal()
    refresh_requested = Signal()
    github_collect_requested = Signal(str, int, str, str)
    github_cancel_requested = Signal()
    github_export_requested = Signal()
    github_import_requested = Signal()
    github_refresh_requested = Signal()
    gitee_search_requested = Signal(str, int, str)
    gitee_recommend_requested = Signal(int)
    gitee_resume_requested = Signal()
    market_save_requested = Signal(dict)
    market_collect_requested = Signal(str, int)

    def __init__(self, dictionary_version: str, parent=None):
        super().__init__(parent)
        self.current_run: DiscoveryRun | None = None
        self.busy = False
        self.current_content_run: DiscoveryRun | None = None
        self.content_busy = False
        self.content_export_blocked = False
        self.content_candidates = []
        self.content_detail_focused = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        heading = QHBoxLayout()
        title = QLabel('发现数据')
        title.setObjectName('heading')
        heading.addWidget(title)
        heading.addStretch()
        self.dictionary_label = QLabel(f'词典：{dictionary_version}')
        self.dictionary_label.setObjectName('metric')
        heading.addWidget(self.dictionary_label)
        layout.addLayout(heading)

        self.scope_note = QLabel('结果仅保存在本机，均为仅本地候选，尚未发布。')
        self.scope_note.setObjectName('muted')
        layout.addWidget(self.scope_note)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        job_tab = QWidget()
        job_layout = QVBoxLayout(job_tab)
        job_layout.setContentsMargins(0, 8, 0, 0)
        job_layout.setSpacing(14)

        summary = QFrame()
        summary.setObjectName('panel')
        summary_layout = QVBoxLayout(summary)
        summary_layout.setContentsMargins(16, 14, 16, 14)
        summary_layout.setSpacing(9)
        summary_layout.addWidget(QLabel('岗位技术提取'))
        description = QLabel('扫描范围：全部当前本地岗位。使用确定性草案词典提取技术及原文证据，不访问招聘网站。')
        description.setObjectName('muted')
        description.setWordWrap(True)
        summary_layout.addWidget(description)
        self.metrics_label = QLabel('尚无成功运行')
        self.metrics_label.setObjectName('metric')
        summary_layout.addWidget(self.metrics_label)
        self.run_label = QLabel('点击“扫描全部岗位”生成第一个不可变本地快照。')
        self.run_label.setObjectName('muted')
        self.run_label.setWordWrap(True)
        summary_layout.addWidget(self.run_label)
        actions = QHBoxLayout()
        self.scan_button = QPushButton('扫描全部岗位')
        self.scan_button.setObjectName('primary')
        self.cancel_button = QPushButton('停止扫描')
        self.export_button = QPushButton('导出当前 JSON')
        self.refresh_button = QPushButton('刷新')
        actions.addWidget(self.scan_button)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.export_button)
        actions.addWidget(self.refresh_button)
        actions.addStretch()
        summary_layout.addLayout(actions)
        job_layout.addWidget(summary)

        splitter = QSplitter()
        self.mention_table = QTableWidget(0, 5)
        self.mention_table.setHorizontalHeaderLabels(['技术', '岗位', '公司', '来源字段', '原文证据'])
        self.mention_table.setAlternatingRowColors(True)
        self.mention_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.mention_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.mention_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.mention_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        splitter.addWidget(self.mention_table)

        self.issue_table = QTableWidget(0, 5)
        self.issue_table.setHorizontalHeaderLabels(['岗位键', '问题', '命中别名', '候选技术', '证据'])
        self.issue_table.setAlternatingRowColors(True)
        self.issue_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.issue_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.issue_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.issue_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        splitter.addWidget(self.issue_table)
        splitter.setSizes([390, 210])
        job_layout.addWidget(splitter, 1)
        self.tabs.addTab(job_tab, '岗位技术')

        content_tab = QWidget()
        content_layout = QVBoxLayout(content_tab)
        content_layout.setContentsMargins(0, 8, 0, 0)
        content_layout.setSpacing(14)
        self.content_mode_tabs = QTabWidget()
        content_layout.addWidget(self.content_mode_tabs, 1)
        self.content_summary = QFrame()
        self.content_summary.setObjectName('panel')
        content_summary_layout = QVBoxLayout(self.content_summary)
        content_summary_layout.setContentsMargins(16, 14, 16, 14)
        content_summary_layout.setSpacing(9)
        content_summary_layout.addWidget(QLabel('开源项目与 AI×市场案例采集'))
        self.content_description = QLabel(
            'GitHub 支持关键词或高星仓库池，Gitee 支持关键词或推荐项目；'
            'AI×市场案例可从腾讯云公开专栏一键采集，也可人工核对来源后填写。'
            '所有结果仅保存于本机，尚未进入管理端审核。'
        )
        self.content_description.setObjectName('muted')
        self.content_description.setWordWrap(True)
        content_summary_layout.addWidget(self.content_description)

        self.content_source_tabs = QTabWidget()
        self.content_source_tabs.setMinimumHeight(150)
        github_source_tab = QWidget()
        github_source_layout = QVBoxLayout(github_source_tab)
        github_source_layout.setContentsMargins(0, 6, 0, 0)
        content_filters = QHBoxLayout()
        self.github_keyword = QLineEdit()
        self.github_keyword.setPlaceholderText('关键词可留空；留空时从高星仓库中采集')
        self.github_keyword.setAccessibleName('GitHub 关键词')
        self.github_sort = QComboBox()
        self.github_sort.addItem('按累计 Star', 'stars')
        self.github_sort.addItem('按最近更新', 'updated')
        self.github_sort.setAccessibleName('GitHub 排序')
        self.github_created_after = QLineEdit()
        self.github_created_after.setPlaceholderText('创建日期起 YYYY-MM-DD（可选）')
        self.github_created_after.setAccessibleName('GitHub 最早创建日期')
        self.github_created_after.setFixedWidth(200)
        self.github_limit = QSpinBox()
        self.github_limit.setRange(1, 20)
        self.github_limit.setValue(3)
        self.github_limit.setSuffix(' 条')
        self.github_limit.setAccessibleName('GitHub 结果上限')
        content_filters.addWidget(self.github_keyword, 1)
        content_filters.addWidget(self.github_created_after)
        content_filters.addWidget(self.github_sort)
        content_filters.addWidget(self.github_limit)
        github_source_layout.addLayout(content_filters)
        self.github_scope_note = QLabel('不填关键词时，从 Star 超过 10 万的公开仓库中按所选方式排序；“最近更新”仅指这个高星范围。')
        self.github_scope_note.setObjectName('muted')
        self.github_scope_note.setWordWrap(True)
        github_source_layout.addWidget(self.github_scope_note)
        self.github_collect_button = QPushButton('采集 GitHub 候选')
        self.github_collect_button.setObjectName('primary')
        github_source_layout.addWidget(self.github_collect_button)
        github_scroll = QScrollArea()
        github_scroll.setWidgetResizable(True)
        github_scroll.setWidget(github_source_tab)
        self.content_source_tabs.addTab(github_scroll, 'GitHub')

        gitee_source_tab = QWidget()
        gitee_source_layout = QVBoxLayout(gitee_source_tab)
        gitee_source_layout.setContentsMargins(0, 6, 0, 0)
        self.gitee_mode = QComboBox()
        self.gitee_mode.addItem('关键词搜索', 'keyword')
        self.gitee_mode.addItem('推荐项目', 'recommended')
        self.gitee_mode.setAccessibleName('Gitee 采集方式')
        gitee_note = QLabel(
            '关键词模式在接口返回的前 100 条内排序；推荐项目按推荐页当前顺序采集，'
            '遇到验证时请在专用浏览器手动完成。'
        )
        gitee_note.setWordWrap(True)
        gitee_source_layout.addWidget(gitee_note)
        gitee_search_fields = QHBoxLayout()
        self.gitee_keyword = QLineEdit()
        self.gitee_keyword.setPlaceholderText('Gitee 项目或技术关键词（必填）')
        self.gitee_keyword.setAccessibleName('Gitee 搜索关键词')
        self.gitee_sort = QComboBox()
        self.gitee_sort.addItem('返回池按 Star', 'stars')
        self.gitee_sort.addItem('返回池按更新时间', 'updated')
        self.gitee_sort.setAccessibleName('Gitee 返回池排序')
        self.gitee_limit = QSpinBox()
        self.gitee_limit.setRange(1, 20)
        self.gitee_limit.setValue(3)
        self.gitee_limit.setSuffix(' 条')
        self.gitee_limit.setAccessibleName('Gitee 结果上限')
        gitee_search_fields.addWidget(self.gitee_mode)
        gitee_search_fields.addWidget(self.gitee_keyword, 1)
        gitee_search_fields.addWidget(self.gitee_sort)
        gitee_search_fields.addWidget(self.gitee_limit)
        gitee_source_layout.addLayout(gitee_search_fields)
        self.gitee_search_button = QPushButton('开始采集 Gitee')
        self.gitee_search_button.setObjectName('primary')
        gitee_search_fields.addWidget(self.gitee_search_button)
        gitee_source_layout.addStretch()
        gitee_scroll = QScrollArea()
        gitee_scroll.setWidgetResizable(True)
        gitee_scroll.setWidget(gitee_source_tab)
        self.content_source_tabs.addTab(gitee_scroll, 'Gitee')

        self.market_mode_tabs = QTabWidget()
        market_auto_tab = QWidget()
        market_auto_layout = QVBoxLayout(market_auto_tab)
        market_auto_layout.setContentsMargins(0, 6, 0, 0)
        market_auto_note = QLabel(
            '一键采集腾讯云认证作者案例专栏首屏：仅收原文注明客户、业务痛点和 AI 方案的案例。'
            '“阅读量”仅指该站当前显示的阅读量，内容为来源方自述，仍需复核。'
        )
        market_auto_note.setWordWrap(True)
        market_auto_layout.addWidget(market_auto_note)
        market_automatic = QHBoxLayout()
        self.market_collect_mode = QComboBox()
        self.market_collect_mode.addItem('首屏最新发布', 'latest')
        self.market_collect_mode.addItem('首屏站内阅读最多', 'views')
        self.market_collect_mode.setAccessibleName('案例自动采集排序')
        self.market_collect_limit = QSpinBox()
        self.market_collect_limit.setRange(1, 5)
        self.market_collect_limit.setValue(3)
        self.market_collect_limit.setSuffix(' 条')
        self.market_collect_limit.setAccessibleName('案例自动采集上限')
        self.market_collect_button = QPushButton('一键采集案例')
        self.market_collect_button.setObjectName('primary')
        market_automatic.addWidget(self.market_collect_mode, 1)
        market_automatic.addWidget(self.market_collect_limit)
        market_automatic.addWidget(self.market_collect_button)
        market_auto_layout.addLayout(market_automatic)
        market_auto_layout.addStretch()
        self.market_mode_tabs.addTab(market_auto_tab, '一键采集')
        market_manual_tab = QWidget()
        market_manual_layout = QVBoxLayout(market_manual_tab)
        market_manual_layout.setContentsMargins(0, 6, 0, 0)
        market_note = QLabel(
            '人工核对国内外企业技术博客或业务案例后填写来源材料；外文来源的摘要和实践方向须人工译为中文。'
            '本表单不联网、不调用 AI，也不发布。'
        )
        market_note.setWordWrap(True)
        market_manual_layout.addWidget(market_note)
        market_grid = QGridLayout()
        self.market_source_name = QLineEdit()
        self.market_source_name.setPlaceholderText('原始发布方名称')
        self.market_source_url = QLineEdit()
        self.market_source_url.setPlaceholderText('https://... 原文链接')
        self.market_title = QLineEdit()
        self.market_title.setPlaceholderText('案例标题')
        self.market_published_at = QLineEdit()
        self.market_published_at.setPlaceholderText('原文发布时间，须含时区，如 2026-10-26T12:00:00+08:00')
        self.market_tech_keys = QLineEdit()
        self.market_tech_keys.setPlaceholderText('词典技术键，逗号分隔，如 python, fastapi')
        self.market_description = QLineEdit()
        self.market_description.setPlaceholderText('原始介绍（可选）')
        self.market_evidence = QTextEdit()
        self.market_evidence.setPlaceholderText('原文证据：业务场景、问题、AI 采用方式、结果或限制')
        self.market_evidence.setFixedHeight(62)
        self.market_summary = QTextEdit()
        self.market_summary.setPlaceholderText('拟审核摘要：只整理来源支持的业务场景、问题、AI 采用方式与结果或限制')
        self.market_summary.setFixedHeight(62)
        self.market_learning_action = QLineEdit()
        self.market_learning_action.setPlaceholderText('学生可实践的最小练习（无法确认可留空）')
        for row, fields in enumerate((
            (('发布方', self.market_source_name), ('原文链接', self.market_source_url)),
            (('案例标题', self.market_title), ('原文时间', self.market_published_at)),
            (('技术键', self.market_tech_keys), ('原始介绍', self.market_description)),
            (('原文证据', self.market_evidence), ('拟审核摘要', self.market_summary)),
        )):
            for column, (name, widget) in enumerate(fields):
                market_grid.addWidget(QLabel(name), row, column * 2)
                market_grid.addWidget(widget, row, column * 2 + 1)
        market_grid.addWidget(QLabel('实践方向'), 4, 0)
        market_grid.addWidget(self.market_learning_action, 4, 1, 1, 3)
        market_grid.setColumnStretch(1, 1)
        market_grid.setColumnStretch(3, 1)
        market_manual_layout.addLayout(market_grid)
        self.market_save_button = QPushButton('保存人工整理的待复核案例')
        self.market_save_button.setObjectName('primary')
        market_manual_layout.addWidget(self.market_save_button)
        market_scroll = QScrollArea()
        market_scroll.setWidgetResizable(True)
        market_scroll.setWidget(market_manual_tab)
        self.market_mode_tabs.addTab(market_scroll, '人工整理')
        self.content_source_tabs.addTab(self.market_mode_tabs, 'AI×市场案例')
        content_summary_layout.addWidget(self.content_source_tabs, 1)
        self.content_mode_tabs.addTab(self.content_summary, '采集设置')

        content_status = QFrame()
        content_status.setObjectName('panel')
        content_status_layout = QVBoxLayout(content_status)
        content_status_layout.setContentsMargins(16, 10, 16, 10)
        content_status_layout.setSpacing(6)
        self.content_metrics_label = QLabel('尚无本地内容候选')
        self.content_metrics_label.setObjectName('metric')
        content_status_layout.addWidget(self.content_metrics_label)
        self.content_hint = QLabel('选择来源后采集或整理；README 仅作来源材料，候选介绍和学习建议仍需复核。')
        self.content_hint.setObjectName('muted')
        self.content_hint.setWordWrap(True)
        content_status_layout.addWidget(self.content_hint)
        content_actions = QHBoxLayout()
        self.github_cancel_button = QPushButton('停止内容采集')
        self.gitee_resume_button = QPushButton('验证完成，继续')
        self.gitee_resume_button.setVisible(False)
        self.github_export_button = QPushButton('导出候选 JSON')
        self.github_import_button = QPushButton('导入 JSON 预览')
        self.github_refresh_button = QPushButton('刷新最近成功')
        content_actions.addWidget(self.github_cancel_button)
        content_actions.addWidget(self.gitee_resume_button)
        content_actions.addWidget(self.github_export_button)
        content_actions.addWidget(self.github_import_button)
        content_actions.addWidget(self.github_refresh_button)
        content_actions.addStretch()
        content_status_layout.addLayout(content_actions)
        content_layout.insertWidget(0, content_status)

        self.content_workspace = QSplitter(Qt.Orientation.Vertical)
        self.content_workspace.setChildrenCollapsible(False)
        self.content_workspace.setHandleWidth(7)
        self.content_mode_tabs.addTab(self.content_workspace, '查看结果')
        self.content_results_tabs = QTabWidget()
        self.content_results_tabs.setMinimumHeight(140)
        self.candidate_table = QTableWidget(0, 7)
        self.candidate_table.setHorizontalHeaderLabels(
            ['标题', '来源', '采集时间', '许可证', '状态', '来源链接', 'README']
        )
        self.candidate_table.setAlternatingRowColors(True)
        self.candidate_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.candidate_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.candidate_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column, width in enumerate((280, 190, 180, 100, 80, 220, 110)):
            self.candidate_table.setColumnWidth(column, width)
        self.content_results_tabs.addTab(self.candidate_table, '内容候选 (0)')
        self.content_issue_table = QTableWidget(0, 3)
        self.content_issue_table.setHorizontalHeaderLabels(['来源', '问题', '说明'])
        self.content_issue_table.setAlternatingRowColors(True)
        self.content_issue_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.content_issue_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.content_issue_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.content_issue_table.setColumnWidth(0, 150)
        self.content_issue_table.setColumnWidth(1, 220)
        self.content_issue_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.content_results_tabs.addTab(self.content_issue_table, '采集问题 (0)')
        self.content_workspace.addWidget(self.content_results_tabs)
        detail_panel = QWidget()
        detail_layout = QVBoxLayout(detail_panel)
        detail_layout.setContentsMargins(0, 6, 0, 0)
        detail_layout.setSpacing(6)
        self.readme_info_label = QLabel('选择一条候选，查看来源材料与 README 状态。')
        self.readme_info_label.setObjectName('muted')
        self.readme_info_label.setWordWrap(True)
        self.readme_info_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        detail_header = QHBoxLayout()
        detail_header.addWidget(self.readme_info_label, 1)
        self.content_detail_button = QPushButton('放大详情')
        self.content_detail_button.setToolTip('也可以拖动列表与详情之间的分隔线调整高度')
        detail_header.addWidget(self.content_detail_button, 0, Qt.AlignmentFlag.AlignTop)
        detail_layout.addLayout(detail_header)
        self.readme_preview = QTextEdit()
        self.readme_preview.setReadOnly(True)
        self.readme_preview.setPlaceholderText('仓库 README 或案例来源材料将在这里预览；内容仍待人工复核。')
        detail_layout.addWidget(self.readme_preview, 1)
        self.content_workspace.addWidget(detail_panel)
        self.content_workspace.setStretchFactor(0, 1)
        self.content_workspace.setStretchFactor(1, 1)
        self.content_workspace.setSizes([240, 200])
        self.tabs.addTab(content_tab, '内容候选')

        self.scan_button.clicked.connect(self.scan_requested.emit)
        self.cancel_button.clicked.connect(self.cancel_requested.emit)
        self.export_button.clicked.connect(self.export_requested.emit)
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        self.github_collect_button.clicked.connect(self.request_github_collection)
        self.gitee_search_button.clicked.connect(self.request_gitee_search)
        self.gitee_mode.currentIndexChanged.connect(self.update_gitee_controls)
        self.market_collect_button.clicked.connect(self.request_market_collection)
        self.market_save_button.clicked.connect(self.request_market_case)
        self.github_cancel_button.clicked.connect(self.github_cancel_requested.emit)
        self.gitee_resume_button.clicked.connect(self.gitee_resume_requested.emit)
        self.github_export_button.clicked.connect(self.github_export_requested.emit)
        self.github_import_button.clicked.connect(self.github_import_requested.emit)
        self.github_refresh_button.clicked.connect(self.github_refresh_requested.emit)
        self.content_detail_button.clicked.connect(self.toggle_content_detail)
        self.candidate_table.itemSelectionChanged.connect(self.show_readme_preview)
        self.set_busy(False)
        self.set_content_busy(False)

    def set_busy(self, busy: bool, cancellable: bool = False):
        self.busy = bool(busy)
        self.scan_button.setEnabled(not self.busy)
        self.refresh_button.setEnabled(not self.busy)
        self.cancel_button.setEnabled(self.busy and bool(cancellable))
        self.export_button.setEnabled(
            not self.busy and self.current_run is not None and self.current_run.status == 'completed'
        )

    def toggle_content_detail(self):
        self.content_detail_focused = not self.content_detail_focused
        self.content_results_tabs.setVisible(not self.content_detail_focused)
        self.content_detail_button.setText('返回列表' if self.content_detail_focused else '放大详情')
        if not self.content_detail_focused:
            self.content_workspace.setSizes([240, 200])

    def request_github_collection(self):
        keyword = self.github_keyword.text().strip()
        self.github_collect_requested.emit(
            keyword, self.github_limit.value(), self.github_sort.currentData(),
            self.github_created_after.text().strip(),
        )

    def request_gitee_search(self):
        if self.gitee_mode.currentData() == 'recommended':
            self.gitee_recommend_requested.emit(self.gitee_limit.value())
            return
        keyword = self.gitee_keyword.text().strip()
        if not keyword:
            self.content_hint.setText('请填写 Gitee 关键词，或选择“推荐项目”。')
            self.gitee_keyword.setFocus()
            return
        self.gitee_search_requested.emit(
            keyword, self.gitee_limit.value(), self.gitee_sort.currentData(),
        )

    def update_gitee_controls(self):
        keyword_mode = self.gitee_mode.currentData() == 'keyword'
        self.gitee_keyword.setVisible(keyword_mode)
        self.gitee_sort.setVisible(keyword_mode)
        self.gitee_keyword.setEnabled(not self.content_busy and keyword_mode)
        self.gitee_sort.setEnabled(not self.content_busy and keyword_mode)

    def request_market_case(self):
        self.market_save_requested.emit({
            'source_name': self.market_source_name.text().strip(),
            'source_url': self.market_source_url.text().strip(),
            'title': self.market_title.text().strip(),
            'source_published_at': self.market_published_at.text().strip(),
            'source_evidence_excerpt': self.market_evidence.toPlainText().strip(),
            'proposed_summary': self.market_summary.toPlainText().strip(),
            'proposed_learning_action': self.market_learning_action.text().strip(),
            'tech_keys': tuple(
                key.strip() for key in self.market_tech_keys.text().replace('，', ',').split(',')
                if key.strip()
            ),
            'description': self.market_description.text().strip(),
        })

    def request_market_collection(self):
        self.market_collect_requested.emit(
            self.market_collect_mode.currentData(), self.market_collect_limit.value(),
        )

    def set_content_busy(self, busy: bool):
        self.content_busy = bool(busy)
        self.github_keyword.setEnabled(not self.content_busy)
        self.github_created_after.setEnabled(not self.content_busy)
        self.github_sort.setEnabled(not self.content_busy)
        self.github_limit.setEnabled(not self.content_busy)
        self.github_collect_button.setEnabled(not self.content_busy)
        self.gitee_mode.setEnabled(not self.content_busy)
        self.update_gitee_controls()
        self.gitee_limit.setEnabled(not self.content_busy)
        self.gitee_search_button.setEnabled(not self.content_busy)
        self.market_collect_mode.setEnabled(not self.content_busy)
        self.market_collect_limit.setEnabled(not self.content_busy)
        self.market_collect_button.setEnabled(not self.content_busy)
        for field in (
            self.market_source_name, self.market_source_url, self.market_title,
            self.market_published_at, self.market_tech_keys, self.market_description,
            self.market_evidence, self.market_summary, self.market_learning_action,
        ):
            field.setEnabled(not self.content_busy)
        self.market_save_button.setEnabled(not self.content_busy)
        self.github_cancel_button.setEnabled(self.content_busy)
        if not self.content_busy:
            self.gitee_resume_button.setVisible(False)
        self.github_refresh_button.setEnabled(not self.content_busy)
        self.github_import_button.setEnabled(not self.content_busy)
        self.github_export_button.setEnabled(
            not self.content_busy
            and not self.content_export_blocked
            and self.current_content_run is not None
            and self.current_content_run.status == 'completed'
        )

    def show_content_run(
        self, run, candidates, issues, *, imported=False,
        legacy_preview=False, dictionary_unverified=False,
    ):
        if run is not None and run.status == 'running':
            return
        self.current_content_run = run
        self.content_export_blocked = legacy_preview or dictionary_unverified
        self.content_candidates = list(candidates)
        self.candidate_table.setRowCount(0)
        self.content_issue_table.setRowCount(0)
        self.content_results_tabs.setTabText(0, f'内容候选 ({len(candidates)})')
        self.content_results_tabs.setTabText(1, f'采集问题 ({len(issues)})')
        self.readme_info_label.setText('选择一条候选，查看来源材料与 README 状态。')
        self.readme_preview.clear()
        if run is None:
            self.content_metrics_label.setText('尚无本地内容候选')
            self.content_hint.setText('选择来源后采集或整理；README 仅作来源材料，候选介绍和学习建议仍需复核。')
            self.set_content_busy(self.content_busy)
            return

        if self.content_detail_focused:
            self.toggle_content_detail()
        self.content_mode_tabs.setCurrentIndex(1)
        partial = run.status in {'cancelled', 'failed'}
        self.content_metrics_label.setText(
            f'{"局部" if partial else "本次"}候选 {len(candidates)} 条　'
            f'待核对问题 {len(issues)} 条'
        )
        criteria = run.search_criteria
        criteria_note = (
            f'查询：{criteria.query}　排序：{criteria.sort}　上限：{criteria.limit} 条　'
            f'创建日期起：{criteria.created_after or "不限"}'
            if criteria else (
                f'来源：腾讯云认证作者案例专栏首屏；{run.message}'
                if run.extractor_version == 'market-tencent-column.v1'
                else '来源：Gitee 推荐项目页；按页面顺序采集'
                if run.extractor_version == 'gitee-rest.v1' and (
                    run.message.startswith('Gitee 推荐')
                    or any(issue.issue_code == 'recommended_listing' for issue in issues)
                )
                else '来源：手动仓库链接或人工案例；无关键词搜索条件'
            )
        )
        self.content_hint.setText(
            f'运行：{run.run_id}　结束：{run.completed_at}\n{criteria_note}\n'
            + (
                '采集已取消或失败；以上是本地部分结果，不可导出。点击“刷新最近成功”可查看上次完成的批次。'
                if partial else
                '历史批次缺少词典版本，仅支持本地预览，不能按目标协议重新导出。'
                if legacy_preview else
                '本机词典无法核对批次中的技术键，仅支持本地预览，不能重新导出。'
                if dictionary_unverified else
                '已从 JSON 导入预览，未写入本地数据库；可重新导出或刷新最近成功记录。'
                if imported else '候选均为本地待复核结果，尚未进入管理端审核。'
            )
        )
        for candidate in candidates:
            row = self.candidate_table.rowCount()
            self.candidate_table.insertRow(row)
            values = (
                candidate.title,
                candidate.source_name,
                candidate.fetched_at,
                (candidate.license_spdx or '待核对') if candidate.content_type == 'open_source_project' else '不适用',
                '待复核',
                candidate.source_url,
                README_STATUS_LABELS.get(
                    getattr(getattr(candidate, 'readme', None), 'status', ''),
                    '不适用' if candidate.content_type != 'open_source_project' else '旧记录未采集',
                ),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.candidate_table.setItem(row, column, item)
        for issue in issues:
            row = self.content_issue_table.rowCount()
            self.content_issue_table.insertRow(row)
            values = (issue.source_id, issue.issue_code, issue.message)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.content_issue_table.setItem(row, column, item)
        if candidates:
            self.content_results_tabs.setCurrentIndex(0)
            self.candidate_table.selectRow(0)
        self.set_content_busy(self.content_busy)

    def show_readme_preview(self):
        row = self.candidate_table.currentRow()
        if row < 0 or row >= len(self.content_candidates):
            self.readme_info_label.setText('选择一条仓库候选，查看 README 来源和采集状态。')
            self.readme_preview.clear()
            return
        candidate = self.content_candidates[row]
        if candidate.content_type != 'open_source_project':
            self.readme_info_label.setText(
                f'原文：{candidate.source_url}\n原文时间：{candidate.source_published_at or "未提供"}　'
                f'技术键：{", ".join(candidate.tech_keys) or "待补充"}　状态：待复核'
            )
            self.readme_preview.setPlainText(
                f'原文证据：{candidate.source_evidence_excerpt}\n\n'
                f'拟审核摘要：{candidate.proposed_summary}\n\n'
                f'实践方向：{candidate.proposed_learning_action or "待补充"}'
            )
            return
        readme = candidate.readme
        if readme is None:
            self.readme_info_label.setText('旧记录未采集 README；可重新采集该来源仓库。')
            self.readme_preview.clear()
            return
        self.readme_info_label.setText(
            f'README 状态：{README_STATUS_LABELS[readme.status]}　来源：{readme.source_url}\n'
            f'SHA：{readme.sha or "未提供"}　采集：{readme.fetched_at}　'
            f'原始大小：{readme.size_bytes if readme.size_bytes is not None else "未知"} 字节　'
            f'截断：{"是" if readme.truncated else "否"}'
            + (f'\n原因：{readme.reason}' if readme.reason else '')
        )
        self.readme_preview.setPlainText(readme.content)

    def show_run(
        self,
        run: DiscoveryRun | None,
        mentions: list[JobTechMention],
        issues: list[ExtractionIssue],
    ):
        if run is not None and run.status != 'completed':
            return
        self.current_run = run
        self.mention_table.setRowCount(0)
        self.issue_table.setRowCount(0)
        if run is None:
            self.metrics_label.setText('尚无成功运行')
            self.run_label.setText('点击“扫描全部岗位”生成第一个不可变本地快照。')
            self.set_busy(self.busy)
            return

        self.metrics_label.setText(
            f'扫描 {run.stats.scanned_jobs}　命中岗位 {run.stats.matched_jobs}　'
            f'技术关联 {run.stats.job_mentions}　待复核 {len(issues)}'
        )
        self.run_label.setText(
            f'运行：{run.run_id}　完成：{run.completed_at}\n'
            f'词典：{run.dictionary_version}　提取器：{run.extractor_version}'
        )
        for mention in mentions:
            row = self.mention_table.rowCount()
            self.mention_table.insertRow(row)
            values = (
                mention.tech_key,
                mention.job_identity.title,
                mention.job_identity.company_name,
                ' / '.join(mention.source_fields),
                mention.evidence_excerpt,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.mention_table.setItem(row, column, item)
        for issue in issues:
            row = self.issue_table.rowCount()
            self.issue_table.insertRow(row)
            values = (
                issue.record_key,
                issue.issue_code,
                issue.matched_alias,
                ' / '.join(issue.candidate_tech_keys),
                issue.evidence_excerpt,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.issue_table.setItem(row, column, item)
        self.set_busy(self.busy)

