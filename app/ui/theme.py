def configure_font(application):
    from PySide6.QtGui import QFont, QFontDatabase
    from app.config import ROOT
    font_file = ROOT / 'assets' / 'fonts' / 'NotoSansSC-Regular.ttf'
    identifier = QFontDatabase.addApplicationFont(str(font_file))
    families = QFontDatabase.applicationFontFamilies(identifier) if identifier >= 0 else []
    font = QFont()
    font.setFamilies(list(families) + ['Microsoft YaHei UI', 'Segoe UI'])
    font.setPointSizeF(10)
    application.setFont(font)


STYLE = '''
QWidget { color: #243b40; font-size: 13px; }
QMainWindow { background: #f3f6f5; }
QFrame#rail { background: #163d43; border: none; }
QLabel#brand { color: #ffffff; font-size: 22px; font-weight: 700; }
QLabel#railHint { color: #b9d2d2; font-size: 12px; }
QListWidget#navigation { background: transparent; border: none; outline: none; color: #c9dddd; }
QListWidget#navigation::item { padding: 15px 16px; margin: 4px 0; border-radius: 6px; }
QListWidget#navigation::item:selected { background: #2b555a; color: white; font-weight: 600; }
QListWidget#navigation::item:hover { background: #254d53; }
QLabel#heading { font-size: 25px; font-weight: 700; color: #153e44; }
QLabel#section { font-size: 16px; font-weight: 600; }
QLabel#muted { color: #61777c; }
QLabel#metric { color: #125f5a; font-size: 19px; font-weight: 600; }
QFrame#panel { background: #ffffff; border: 1px solid #dce6e3; border-radius: 9px; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox { background: white; border: 1px solid #c9d7d4; border-radius: 5px; padding: 7px 9px; min-height: 22px; selection-background-color: #c0dfd7; }
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QDoubleSpinBox:focus { border: 2px solid #18786c; padding: 6px 8px; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView { background: white; selection-background-color: #d9ece7; selection-color: #173f44; }
QPushButton { background: white; border: 1px solid #c7d6d1; border-radius: 5px; padding: 9px 14px; min-height: 20px; }
QPushButton:hover { background: #edf5f1; border-color: #709d90; }
QPushButton:pressed { background: #dbeae2; }
QPushButton:focus { border: 2px solid #15776b; padding: 8px 13px; }
QPushButton#primary { background: #176c61; color: white; border-color: #176c61; font-weight: 600; }
QPushButton#primary:hover { background: #11584f; }
QPushButton#danger { color: #9d3932; }
QPushButton:disabled { color: #98a6a3; background: #edf1ef; border-color: #e0e7e4; }
QTableView, QTableWidget { background: white; alternate-background-color: #f7faf8; border: 1px solid #dce6e3; border-radius: 5px; gridline-color: #edf1ef; selection-background-color: #dceee7; selection-color: #163f43; }
QHeaderView::section { background: #eef4f1; color: #45625f; border: none; border-bottom: 1px solid #d7e2dd; padding: 10px 8px; font-weight: 600; }
QTextEdit, QPlainTextEdit { background: white; border: 1px solid #dce6e3; border-radius: 5px; padding: 10px; selection-background-color: #cee9dd; }
QCheckBox { spacing: 7px; padding: 3px 0; }
QCheckBox::indicator { width: 17px; height: 17px; }
QStatusBar { background: #e6eeea; color: #46615d; }
QSplitter::handle { background: #e3ebe6; width: 4px; }
QToolTip { background: #173f43; color: white; border: none; padding: 6px; }
QScrollArea { border: none; background: transparent; }
QDialog { background: #f5f8f6; }
'''
