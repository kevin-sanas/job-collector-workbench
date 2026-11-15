"""Offline location choices; these codes are never website search codes."""
import json
import re
from functools import lru_cache
from pathlib import Path


UNKNOWN = '__unknown__'


def aliases(name):
    values = {name}
    if name.endswith(('省', '市')) and len(name) > 2:
        values.add(name[:-1])
    if name.endswith(('地区', '自治州')):
        values.add(name[:-2] if name.endswith('地区') else name[:-3])
    if name.endswith('盟'):
        values.add(name[:-1])
    short = {'内蒙古自治区': '内蒙古', '广西壮族自治区': '广西', '西藏自治区': '西藏',
             '宁夏回族自治区': '宁夏', '新疆维吾尔自治区': '新疆'}
    if name in short:
        values.add(short[name])
    return values


class RegionCatalog:
    def __init__(self):
        path = Path(__file__).resolve().parent.parent / 'assets/regions/province-city.json'
        self.provinces = json.loads(path.read_text(encoding='utf-8'))
        self.province_by_code = {p['code']: p for p in self.provinces}
        self.cities = {c['code']: dict(c, province_code=p['code']) for p in self.provinces for c in p['children']}
        self.city_aliases = {}
        for code, city in self.cities.items():
            for name in aliases(city['name']):
                self.city_aliases.setdefault(name, set()).add(code)
        self.province_prefixes = sorted(((name, p['code']) for p in self.provinces for name in aliases(p['name'])),
                                        key=lambda item: len(item[0]), reverse=True)

    @lru_cache(maxsize=4096)
    def resolve_city(self, value):
        """Accept exact city aliases or explicit province + city; leave ambiguous text unknown."""
        text = re.sub(r'[\s·/\-]+', '', str(value or ''))
        found = self.city_aliases.get(text, set())
        if len(found) == 1:
            return next(iter(found))
        for prefix, province_code in self.province_prefixes:
            if not text.startswith(prefix):
                continue
            found = {code for code in self.city_aliases.get(text[len(prefix):], set())
                     if self.cities[code]['province_code'] == province_code}
            if len(found) == 1:
                return next(iter(found))
        return ''

    def matches(self, value, province_code='', city_code=''):
        resolved = self.resolve_city(value)
        if province_code == UNKNOWN:
            return not resolved
        if city_code and resolved != city_code:
            return False
        if province_code:
            return bool(resolved and self.cities[resolved]['province_code'] == province_code)
        return True

    def description(self, province_code='', city_code=''):
        if province_code == UNKNOWN:
            return '未知／未识别地点'
        if not province_code:
            return '全国'
        province = self.province_by_code[province_code]['name']
        city = self.cities.get(city_code)
        return f'{province} / {city["name"]}' if city and city['name'] != province else province + (' / 全省' if not city else '')

    def source_city(self, source, province_code='', city_code=''):
        """Return the site's verified city name, or None when that selection is unsupported."""
        choices = source.get('cities', {})
        if not province_code and not city_code:
            return '全国' if '全国' in choices else None
        if not city_code or self.cities.get(city_code, {}).get('province_code') != province_code:
            return None
        return next((name for name in choices if self.resolve_city(name) == city_code), None)


@lru_cache(maxsize=1)
def catalog():
    return RegionCatalog()


def region_matches(value, province_code, city_code):
    return int(catalog().matches(value, province_code or '', city_code or ''))
