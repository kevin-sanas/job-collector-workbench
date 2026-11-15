import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from lxml import html

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.adapter_job168 import Job168Adapter
from app.adapter_public import PublicPageAdapter
from app.models import Job


class PublicWorkLocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sources = {item['id']: item for item in json.loads((ROOT / 'config/sources.json').read_text(encoding='utf-8'))}

    def detail(self, source, location, description='岗位职责', saved_address='', reference_address=''):
        identifier = '123456' if source == 'job168' else 'Example123'
        url = f'https://www.job168.com/person/detail_{identifier}.html' if source == 'job168' else f'https://www.ncss.cn/student/jobs/{identifier}/detail.html'
        job = Job(source=source, source_job_id=identifier, source_url=url, title='工程师', company_name='示例公司', work_address=saved_address)
        fields = {'工作地区': location, '联系地址': reference_address} if source == 'job168' else {'location': location, 'company_address': reference_address}
        page = SimpleNamespace(url=url, run_js=MagicMock(side_effect=[job.title, job.company_name, description, fields]))
        adapter = Job168Adapter(self.sources[source]) if source == 'job168' else PublicPageAdapter(self.sources[source])
        return adapter.read_detail(page, job)

    def test_ncss_description_selector_supports_pre_and_div_templates(self):
        selector = self.sources['ncss']['detail_selector']
        for tag in ('pre', 'div'):
            with self.subTest(tag=tag):
                root = html.fromstring(f'<div class="jobdetail-box"><{tag} class="mainContent mainContent-geshi">岗位职责</{tag}></div>')
                self.assertEqual(['岗位职责'], [node.text_content() for node in root.cssselect(selector)])

    def test_job168_keeps_district_when_city_is_not_provided(self):
        job = self.detail('job168', '示例区', '联系地址：广州市天河区示例路1号')
        self.assertEqual(('', '示例区', ''), (job.city, job.district, job.work_address))

    def test_job168_exact_site_districts_resolve_their_official_parent_city(self):
        for district, city in [('黄埔区', '广州'), ('禅城区', '佛山')]:
            with self.subTest(district=district):
                job = self.detail('job168', district, reference_address='上海市浦东新区公司路88号')
                self.assertEqual((city, district, ''), (job.city, job.district, job.work_address))

    def test_job168_does_not_match_multiple_or_partial_district_names(self):
        for location in ('黄埔区/禅城区', '黄埔区附近'):
            with self.subTest(location=location):
                self.assertEqual('', self.detail('job168', location).city)

    def test_job168_gets_city_and_single_address_from_explicit_work_location(self):
        address = '广东省广州市黄埔区科学大道18号'
        job = self.detail('job168', '黄埔区', '岗位职责\n工作地点：' + address + '。\n联系地址：广州市天河区示例路1号')
        self.assertEqual(('广州', '黄埔区', address), (job.city, job.district, job.work_address))

    def test_job168_multiple_offices_do_not_become_a_single_map_address(self):
        job = self.detail('job168', '越秀区', '工作地点：广东省广州市越秀区白云路28号、广东省广州市越秀区东华南路5号（地点二选一）。')
        self.assertEqual(('广州', '越秀区', ''), (job.city, job.district, job.work_address))

    def test_ncss_administrative_region_is_not_an_office_address(self):
        job = self.detail('ncss', '北京市海淀区', '岗位职责\n公司地址：北京市海淀区公司路88号')
        self.assertEqual(('北京', '海淀区', ''), (job.city, job.district, job.work_address))

    def test_ncss_uses_explicit_work_address_without_copying_company_address(self):
        address = '北京市海淀区学院路18号A座3楼'
        job = self.detail('ncss', '北京市海淀区', '岗位职责\n工作地址：' + address + '\n公司地址：北京市朝阳区公司路88号')
        self.assertEqual(('北京', '海淀区', address), (job.city, job.district, job.work_address))

    def test_ncss_full_job_location_can_supply_the_address(self):
        address = '北京市海淀区学院路18号A座3楼'
        job = self.detail('ncss', address)
        self.assertEqual(address, job.work_address)

    def test_detail_address_conflicting_with_job_region_is_not_used(self):
        job = self.detail('ncss', '北京市海淀区', '工作地址：上海市浦东新区示例路18号')
        self.assertEqual(('北京', '海淀区', ''), (job.city, job.district, job.work_address))

    def test_broad_or_variable_work_locations_are_not_office_addresses(self):
        for source, region, description in [('job168', '广州市', '工作地点：广州市区内（具体地点根据项目安排）'), ('ncss', '北京市海淀区', '工作地址：北京市海淀区（待定）')]:
            with self.subTest(source=source):
                self.assertEqual('', self.detail(source, region, description).work_address)

    def test_separate_explicit_offices_do_not_choose_one_address(self):
        job = self.detail('ncss', '北京市海淀区', '工作地址：北京市海淀区学院路18号\n办公地址：北京市海淀区中关村大街28号')
        self.assertEqual('', job.work_address)

    def test_address_can_follow_its_label_on_the_next_line(self):
        address = '北京市海淀区学院路18号A座3楼'
        self.assertEqual(address, self.detail('ncss', '北京市海淀区', '工作地点：\n' + address).work_address)

    def test_complete_job168_city_and_district_remain_available(self):
        job = self.detail('job168', '广东省广州市黄埔区')
        self.assertEqual(('广州', '黄埔区', ''), (job.city, job.district, job.work_address))

    def test_job168_contact_address_remains_reference_text_only(self):
        reference = '北京市海淀区公司路88号'
        job = self.detail('job168', '北京市海淀区', reference_address=reference)
        self.assertEqual('', job.work_address)
        self.assertIn('联系地址（仅供参考）：' + reference, job.description)

    def test_ncss_company_address_is_an_explicitly_labelled_fallback(self):
        reference = '北京市海淀区公司路88号'
        job = self.detail('ncss', '全国', reference_address=reference)
        self.assertEqual(('', '', reference), (job.city, job.district, job.work_address))
        self.assertIn('公司所在地（仅供参考）：' + reference, job.description)
        self.assertIn('地址来源：国家平台所在地址（公司信息，待核对）', job.description)

    def test_missing_address_preserves_previous_saved_address(self):
        address = '北京市海淀区学院路18号A座3楼'
        self.assertEqual(address, self.detail('ncss', '北京市海淀区', saved_address=address).work_address)


if __name__ == '__main__':
    unittest.main()
