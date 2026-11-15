from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel,
    QLineEdit, QCheckBox, QPushButton, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView)


class BackendSyncPage(QWidget):
    save_requested = Signal()
    check_requested = Signal()
    history_requested = Signal()
    discovery_requested = Signal()
    content_requested = Signal()
    retry_requested = Signal(str, str)
    refresh_requested = Signal()

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.busy = False
        self.rows = []
        layout = QVBoxLayout(self)
        heading = QLabel('光合后台对接')
        heading.setObjectName('heading')
        layout.addWidget(heading)
        hint = QLabel('岗位采集和发现扫描仍由你手动触发；成功后可自动提交后台。同步岗位时自动提取技术证据，处理结果请在后台查看，新增岗位默认下架。\n岗位与技术证据分别保留发送记录；连接失败不影响本地保存，可选择失败记录单独重发。每批最多 1000 条岗位。')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        form = QFormLayout()
        self.url = QLineEdit()
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText('留空保留已保存的对接凭据；不使用管理员密码')
        self.auto_jobs = QCheckBox('岗位任务成功后自动提交')
        self.auto_discovery = QCheckBox('岗位技术扫描及内容采集成功后自动提交')
        form.addRow('后台服务地址', self.url)
        form.addRow('专用对接凭据', self.token)
        form.addRow(self.auto_jobs)
        form.addRow(self.auto_discovery)
        layout.addLayout(form)
        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.message)
        actions = QHBoxLayout()
        self.save_button = QPushButton('保存连接设置')
        self.check_button = QPushButton('测试连接')
        self.history_button = QPushButton('同步全部历史岗位')
        self.discovery_button = QPushButton('提交最近岗位技术批次')
        self.content_button = QPushButton('提交最近内容批次')
        self.refresh_button = QPushButton('刷新发送记录')
        self.buttons = [self.save_button, self.check_button, self.history_button, self.discovery_button, self.content_button, self.refresh_button]
        for widget, signal in zip(self.buttons, [self.save_requested,self.check_requested,self.history_requested,self.discovery_requested,self.content_requested,self.refresh_requested]):
            widget.clicked.connect(signal.emit)
            actions.addWidget(widget)
        layout.addLayout(actions)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(['类型 / 批次', '提交状态', '尝试次数', '更新时间', '后台批次', '失败说明'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self.update_retry)
        layout.addWidget(self.table, 1)
        footer = QHBoxLayout()
        self.retry_button = QPushButton('重发选中失败批次')
        self.retry_button.clicked.connect(self.retry_selected)
        footer.addStretch()
        footer.addWidget(self.retry_button)
        layout.addLayout(footer)
        self.show_settings(settings)
        self.update_retry()

    def show_settings(self, settings):
        self.url.setText(settings.backend_url)
        self.token.clear()
        self.auto_jobs.setChecked(settings.backend_auto_jobs)
        self.auto_discovery.setChecked(settings.backend_auto_discovery)
        self.message.setText('专用凭据已配置，页面不回显；可测试连接。' if settings.backend_token else '尚未配置专用凭据，自动提交保持关闭。')

    def set_busy(self, busy):
        self.busy = busy
        for widget in [*self.buttons,self.url,self.token,self.auto_jobs,self.auto_discovery]:
            widget.setEnabled(not busy)
        self.update_retry()

    def show_records(self, rows):
        import json
        self.rows = rows
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            receipt = json.loads(row['receipt'])
            values = [f'{"岗位" if row["kind"] == "JOBS" else "发现"} · {row["run_id"]}',
                      {'SENT':'已提交','FAILED':'待补交','PENDING':'待发送'}.get(row['status'],row['status']),
                      str(row['attempts']), row['updated_at'], str(receipt.get('batchId','—')), row['error']]
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                self.table.setItem(index,column,item)
        self.update_retry()

    def update_retry(self):
        selected = self.table.currentRow()
        self.retry_button.setEnabled(not self.busy and 0 <= selected < len(self.rows) and self.rows[selected]['status'] != 'SENT')

    def retry_selected(self):
        selected = self.table.currentRow()
        if 0 <= selected < len(self.rows) and self.rows[selected]['status'] != 'SENT':
            row = self.rows[selected]
            self.retry_requested.emit(row['kind'],row['run_id'])
