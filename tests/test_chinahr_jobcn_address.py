"""Regression cases for the two sites' distinct job/company/interview locations."""
import json
import unittest
from dataclasses import replace
from pathlib import Path

from lxml import html

from app.adapters import adapter_for
from app.models import Job


ROOT = Path(__file__).resolve().parents[1]


class DetailPage:
    """Keep browser I/O at the boundary; selectors resolve against local HTML."""
    def __init__(self, url, markup):
        self.url = url
        self.root = html.fromstring(markup)

    def run_js(self, script, *args):
        if args:
            elements = self.root.cssselect(args[0])
            return elements[0].text_content().strip() if elements else ''
        return [
            {'label': row.findtext('dt', '').strip(),
             'value': ''.join((row.cssselect('.tab_map_title') or row.cssselect('dd'))[0].xpath('./text()')).strip()}
            for row in self.root.cssselect('.address_pos_body dl')
        ]


class ChinaHRJobcnAddressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sources = {item['id']: item for item in json.loads((ROOT / 'config/sources.json').read_text(encoding='utf-8'))}

    def test_chinahr_uses_job_location_instead_of_company_address(self):
        job = Job(source='chinahr', source_job_id='62d6179327c72706de31d63b',
                  source_url='https://www.chinahr.com/detail/62d6179327c72706de31d63b',
                  title='后台开发工程师（Go）-2023届', company_name='广州希音国际进出口有限公司', city='南京')
        page = DetailPage(job.source_url, '''<main>
            <h1 class="detail-top_left-title-name">后台开发工程师（Go）-2023届</h1>
            <div class="detail-com_info_name_text">广州希音国际进出口有限公司</div>
            <div class="detail-des_lists">岗位职责</div>
            <div class="detail-work_address"><p class="workplace_name">江苏省-南京</p></div>
            <div class="detail-com"><p><span>企业地址：</span>广东省广州市番禺区示例路6号</p></div>
            </main>''')
        result = adapter_for(self.sources['chinahr']).read_detail(page, job)
        self.assertEqual('江苏省-南京', result.work_address)
        self.assertEqual('南京', result.city)
        self.assertEqual('岗位职责', result.description)

    def test_chinahr_does_not_fall_back_to_company_address_when_job_location_is_absent(self):
        job = Job(source='chinahr', source_job_id='62d6179327c72706de31d63b',
                  source_url='https://www.chinahr.com/detail/62d6179327c72706de31d63b', title='开发', company_name='企业')
        page = DetailPage(job.source_url, '''<main><h1 class="detail-top_left-title-name">开发</h1>
            <div class="detail-com_info_name_text">企业</div><div class="detail-des_lists">职责</div>
            <div class="detail-com"><p><span>企业地址：</span>广东省广州市示例路</p></div></main>''')
        result = adapter_for(self.sources['chinahr']).read_detail(page, job)
        self.assertEqual('', result.work_address)

    def test_jobcn_uses_work_address_instead_of_interview_or_company_address(self):
        job = Job(source='jobcn', source_job_id='3751680',
                  source_url='https://www.jobcn.com/position/detail.xhtml?comId=496005&posId=3751680&redirect=0',
                  title='品质经理', company_name='深圳市胜航精密连接器有限公司', city='深圳')
        page = DetailPage(job.source_url, '''<main>
            <div class="base"><h2>深圳市胜航精密连接器有限公司</h2><p>地址：广东深圳凤凰第一工业区B28栋</p></div>
            <div class="name_pos"><h2 class="name"><span>品质经理</span></h2><div class="attributes">广东深圳</div></div>
            <div class="desc_pos_body">岗位职责</div><div class="address_pos_body">
            <dl><dt>面试地址：</dt><dd>广东深圳凤凰第一工业区凤业七路B28栋</dd></dl>
            <dl><dt>工作地址：</dt><dd><div class="tab_map_title">广东深圳凤凰第一工业区凤业七路2号<a>查看地图</a></div></dd></dl>
            </div></main>''')
        result = adapter_for(self.sources['jobcn']).read_detail(page, job)
        self.assertEqual('广东深圳凤凰第一工业区凤业七路2号', result.work_address)
        self.assertEqual('岗位职责', result.description)

    def test_jobcn_missing_work_address_preserves_saved_value_without_using_interview_address(self):
        job = Job(source='jobcn', source_job_id='3751680',
                  source_url='https://www.jobcn.com/position/detail.xhtml?posId=3751680&comId=496005',
                  title='品质经理', company_name='示例企业', city='深圳')
        page = DetailPage(job.source_url, '''<main><div class="base"><h2>示例企业</h2></div>
            <div class="name_pos"><h2 class="name"><span>品质经理</span></h2><div class="attributes">广东深圳</div></div>
            <div class="desc_pos_body">职责</div><div class="address_pos_body">
            <dl><dt>面试地址：</dt><dd>广东深圳面试路B28栋</dd></dl></div></main>''')
        adapter = adapter_for(self.sources['jobcn'])
        self.assertEqual('', adapter.read_detail(page, job).work_address)
        self.assertEqual('已有工作地址', adapter.read_detail(page, replace(job, work_address='已有工作地址')).work_address)


if __name__ == '__main__':
    unittest.main()
