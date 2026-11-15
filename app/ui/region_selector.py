"""Shared province/city controls for collection and local filtering."""
from PySide6.QtCore import QSignalBlocker, Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QWidget

from app.regions import UNKNOWN, catalog


class RegionSelector(QWidget):
    changed = Signal()

    def __init__(self, allow_unknown=False, parent=None):
        super().__init__(parent)
        self.catalog = catalog()
        self.province, self.city = QComboBox(), QComboBox()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        for name, combo in (('省份', self.province), ('城市', self.city)):
            combo.setAccessibleName(name)
            combo.setToolTip(name + '（离线地区目录）')
            combo.setMinimumContentsLength(6)
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMaxVisibleItems(16)
            layout.addWidget(combo, 1)
        self.setMinimumWidth(260)
        self.province.addItem('全国 / 不限省份', '')
        for province in self.catalog.provinces:
            self.province.addItem(province['name'], province['code'])
        if allow_unknown:
            self.province.addItem('未知／未识别地点', UNKNOWN)
        self.province.currentIndexChanged.connect(self.update_cities)
        self.city.currentIndexChanged.connect(self.changed)
        self.update_cities()

    def update_cities(self):
        province_code = self.province.currentData()
        with QSignalBlocker(self.city):
            self.city.clear()
            province = self.catalog.province_by_code.get(province_code)
            if province:
                self.city.addItem('全省 / 不限城市', '')
                for city in province['children']:
                    self.city.addItem(city['name'], city['code'])
                if province_code in ('11', '12', '31', '50'):
                    self.city.setCurrentIndex(1)
            else:
                self.city.addItem('未知地点' if province_code == UNKNOWN else '全国', '')
            self.city.setEnabled(bool(province))
        self.changed.emit()

    def codes(self):
        return self.province.currentData() or '', self.city.currentData() or ''

    def description(self):
        return self.catalog.description(*self.codes())

    def source_city(self, source):
        return self.catalog.source_city(source, *self.codes())

    def reset(self):
        self.province.setCurrentIndex(0)
        self.update_cities()
