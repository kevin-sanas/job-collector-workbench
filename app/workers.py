import queue
import threading

from PySide6.QtCore import QThread, Signal

from app.browser import BrowserSession
from app.collector import Collector


class FunctionWorker(QThread):
    result = Signal(object)
    error = Signal(str)

    def __init__(self, function, parent=None):
        super().__init__(parent)
        self.function = function

    def run(self):
        try:
            self.result.emit(self.function())
        except Exception as exc:
            message = str(exc) if isinstance(exc, (ValueError, UnicodeError, PermissionError)) else f'{type(exc).__name__}：操作失败，请检查文件格式、文件占用和配置'
            self.error.emit(message)


class BrowserWorker(QThread):
    state = Signal(object)
    message = Signal(str)
    diagnostic = Signal(object)
    error = Signal(str)
    busy = Signal(bool)

    def __init__(self, paths, settings, repository, parent=None):
        super().__init__(parent)
        self.paths, self.settings, self.repository = paths, settings, repository
        self.commands = queue.Queue()
        self.stopping = threading.Event()
        self.control = None

    def submit(self, action, source=None, criteria=None, control=None):
        if control is not None:
            self.control = control
        self.commands.put((action, source, criteria, control))

    def shutdown(self):
        self.stopping.set()
        if self.control:
            self.control.cancel()
        self.commands.put(None)

    def run(self):
        session = BrowserSession(self.paths, self.settings)
        try:
            while not self.stopping.is_set():
                command = self.commands.get()
                if command is None or self.stopping.is_set():
                    break
                action, source, criteria, control = command
                self.busy.emit(True)
                try:
                    if action == 'open':
                        session.open(source)
                        self.message.emit('专用浏览器已打开，已核对禁用代理启动参数；请在浏览器内自行登录。实际外网连接路径仍需现场确认。')
                    elif action == 'collect':
                        Collector(session, self.repository, self.settings, self.state.emit).run(source, criteria, control)
                    elif action == 'diagnostic':
                        from app.adapters import adapter_for
                        if session.page is None or session.source_id != source['id']:
                            raise ValueError('请先打开所选来源的专用浏览器并进入岗位列表')
                        session.ensure_page(source)
                        self.diagnostic.emit(adapter_for(source).diagnostic(session.page))
                    elif action == 'close':
                        session.close()
                        self.message.emit('本工具创建的浏览器已关闭，专用目录中的会话仍保存在本机')
                except Exception as exc:
                    self.error.emit(str(exc) if isinstance(exc, ValueError) else f'浏览器操作失败（{type(exc).__name__}）。请检查程序路径或按 README 提供脱敏材料。')
                finally:
                    self.busy.emit(False)
        finally:
            session.close()
