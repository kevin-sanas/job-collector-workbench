import argparse
import logging
import shutil
import sys
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description='本地岗位采集工作台')
    parser.add_argument('--check', action='store_true', help='使用临时本地数据启动窗口并自动退出，不访问招聘网站')
    parser.add_argument('--screenshot', default='', help='与 --check 一起使用，保存窗口截图到指定项目内文件')
    parser.add_argument('--open-source', default='', help='打开指定来源供用户人工登录，例如 boss；不会自动开始采集')
    args = parser.parse_args()
    try:
        from PySide6.QtCore import QTimer, Qt, QLockFile
        from PySide6.QtGui import QFont
        from PySide6.QtWidgets import QApplication
        from app.config import AppPaths, ROOT
        from app.ui.main_window import MainWindow
        from app.ui.theme import configure_font
    except ImportError:
        print('缺少项目依赖。请用项目 .venv 解释器安装 requirements.txt，详见 README.md。')
        return 1
    application = QApplication(sys.argv[:1])
    application.setApplicationName('岗位采集工作台')
    configure_font(application)
    paths = AppPaths()
    scratch = None
    if args.check:
        temporary = paths.data / 'test-tmp'
        temporary.mkdir(exist_ok=True)
        scratch = tempfile.TemporaryDirectory(dir=temporary)
        paths = AppPaths(Path(scratch.name))
        shutil.copyfile(ROOT / 'config' / 'sources.json', paths.config / 'sources.json')
        shutil.copyfile(
            ROOT / 'config' / 'discovery' / 'tech_dictionary.json',
            paths.discovery_config / 'tech_dictionary.json',
        )
        shutil.copyfile(
            ROOT / 'config' / 'discovery' / 'content_sources.json',
            paths.discovery_config / 'content_sources.json',
        )
    lock = QLockFile(str(paths.data / 'application.lock'))
    if not lock.tryLock(0):
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.information(None, '应用已运行', '当前项目已经打开工作台，请使用已有窗口。')
        return 1
    logging.basicConfig(filename=paths.logs / 'app.log', encoding='utf-8', level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        window = MainWindow(paths)
    except Exception as exc:
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.critical(None, '启动失败', f'{type(exc).__name__}：请检查项目配置和数据目录权限。')
        return 1
    screen = application.primaryScreen()
    if screen:
        geometry = screen.availableGeometry()
        window.resize(max(1120, min(1380, geometry.width() - 40)), max(740, min(880, geometry.height() - 40)))
    window.show()
    if args.open_source and not args.check:
        index = window.collect_source.findData(args.open_source)
        if index >= 0:
            window.navigation.setCurrentRow(1)
            window.collect_source.setCurrentIndex(index)
            QTimer.singleShot(300, window.open_browser)
    if args.check:
        def finish_check():
            if args.screenshot:
                screenshot = Path(args.screenshot).resolve()
                if not screenshot.is_relative_to(ROOT):
                    raise ValueError('检查截图须保存在本项目内')
                window.grab().save(str(screenshot))
            print(f'UI_CHECK_OK rows={window.model.rowCount()} browser_started=False', flush=True)
            window.close()
        QTimer.singleShot(1500, finish_check)
    code = application.exec()
    logging.shutdown()
    lock.unlock()
    if scratch:
        scratch.cleanup()
    return code


if __name__ == '__main__':
    raise SystemExit(main())
