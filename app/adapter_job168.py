"""Public job168 nationwide search and job details through visible page controls."""
from urllib.parse import urlsplit

from app.adapter_public import PublicPageAdapter, work_location_details
from app.adapters import CitySelectionError, source_url_allowed
from app.models import Job


class Job168Adapter(PublicPageAdapter):
    def criteria_fields(self, page):
        return page.run_js('''return Object.fromEntries(
            ['keyword','work_loc1','industry','apply_func','pub_date'].map(name=>
            [name,document.querySelector('input[name="'+name+'"]')?.value??null]));''') or {}

    def matches_criteria(self, fields, keyword, code):
        # The server echoes a cleared national search as its exact placeholder.
        region_ok = fields.get('work_loc1') == code if code else fields.get('work_loc1') in ('', '请选择地区')
        return (fields.get('keyword') == keyword and region_ok
                and fields.get('industry') in ('', '请选择行业')
                and fields.get('apply_func') in ('', '请选择职位类别')
                and fields.get('pub_date') == '30')

    def validate_search_selection(self, page, keyword, city):
        if city not in self.source.get('cities', {}) or urlsplit(page.url).path != '/person/search_result.jsp':
            return False
        fields = self.criteria_fields(page)
        evidence = self._search_evidence(page, {'keyword': '#keyword'})
        expected_keyword = fields.get('keyword') if keyword is None else keyword
        return (evidence.get('keyword') == [expected_keyword]
                and self.matches_criteria(fields, expected_keyword, self.source['cities'][city])
                and (evidence.get('loaded') is True or bool(self.empty_message(page))))

    def search(self, session, keyword, city, gate, check_page, delay, interval):
        if city not in self.source.get('cities', {}):
            raise CitySelectionError('该地区尚未确认，请在浏览器手动筛选后采集当前页')
        check_page()
        page = session.page

        def picker(trigger, title):
            gate()
            check_page()
            page.ele('css:' + trigger, timeout=2).click()
            delay(.2)
            heading = page.ele('css:#SwinMain .SwinMaint1', timeout=2)
            if not heading or not heading.text.strip().startswith(title):
                if title == '工作地区':
                    raise CitySelectionError('未确认工作地区选择窗口，请人工选择城市并搜索后点击继续')
                raise ValueError('筛选窗口结构发生变化，请人工确认后使用采集当前页')
            gate()

        # The application's normal search has no industry or occupation filter.
        # Keep manually selected criteria only in current-page mode.
        fields = self.criteria_fields(page)
        for field, trigger, title, placeholder in (
                ('industry', '#buttonSelIndustry', '行业', '请选择行业'),
                ('apply_func', '#buttonSelJobType', '职位', '请选择职位类别')):
            if fields.get(field) not in ('', placeholder):
                picker(trigger, title)
                page.ele('css:#dicClear').click()
                delay(.15)
                gate()
                page.ele('css:#dicConfirm').click()
                delay(.15)

        picker('#buttonSelCity', '工作地区')
        page.ele('css:#dicClear').click()
        delay(.15)
        code = self.source['cities'][city]
        if code:
            # Codes are recorded from this site's visible radio options, not
            # from the application's independent administrative catalog.
            target = page.ele('css:#dicRegion #code' + code, timeout=2)
            label = page.ele('css:#dicRegion label[for="code' + code + '"]', timeout=0)
            if not target or not label or label.text.strip() != city:
                raise CitySelectionError('地区选项与来源配置不符，已停止搜索')
            gate()
            target.click()
            delay(.15)
        gate()
        # A single radio selection can apply itself and remove the picker.
        confirm = page.ele('css:#dicConfirm', timeout=0)
        if confirm and confirm.run_js('return this.getClientRects().length>0;'):
            confirm.click()
        delay(.2)
        check_page()
        gate()
        page.ele('css:#keyword', timeout=2).input(keyword, clear=True)
        fields = self.criteria_fields(page)
        if not self.matches_criteria(fields, keyword, code):
            raise CitySelectionError('搜索条件未确认：自动搜索使用不限行业／职位类别和网站默认一个月内；其他条件请人工筛选后采集当前页')
        delay(interval)
        gate()
        page.ele('css:.idoSearch', timeout=2).click()
        delay(interval)
        check_page()
        verified = self.criteria_fields(session.page)
        if not self.matches_criteria(verified, keyword, code):
            raise CitySelectionError('搜索后的条件与提交条件不符，请人工检查页面')

    def read_cards(self, page):
        if not source_url_allowed(page.url, self.source['domains']):
            raise ValueError('当前页面不属于南方人才网')
        jobs = []
        for item in self.fields(page):
            identifier, url = self.identity(item.get('href', ''))
            if not identifier or not item.get('title') or not item.get('company'):
                continue
            jobs.append(Job.from_mapping({'source': self.source['id'], 'source_job_id': identifier,
                'source_url': url, 'title': item['title'], 'company_name': item['company'],
                'salary_raw': item.get('salary', ''), 'education_raw': item.get('education', ''),
                'experience_raw': item.get('experience', ''), 'published_raw': item.get('published', '')}))
        return jobs

    def read_detail(self, page, job):
        result = super().read_detail(page, job)
        fields = page.run_js('''const result={};const allowed=new Set(
            ['工作性质','最低学历要求','工作年限','工作地区']);
            for(const dt of document.querySelectorAll('.jthum dl dt')) {
                const text=dt.innerText.trim();const at=text.indexOf('：');
                const key=text.slice(0,at).trim();
                if(at>=0 && allowed.has(key))result[key]=text.slice(at+1).trim();
            }
            const contact=Array.from(document.querySelectorAll('.jhc_1 .jmcont p')).find(
                e=>e.getClientRects().length>0 && /^联系地址[：:]/.test(e.innerText.trim()));
            if(contact)result['联系地址']=contact.innerText.trim().replace(/^联系地址[：:]/, '').trim();
            return result;''') or {}
        location = fields.get('工作地区', '')
        if not location:
            raise ValueError('详情工作地区为空，已停止，请检查页面结构')
        # Exact, unique district labels and parent codes were verified in the
        # site's /js/CDict2.js arr_region dictionary on 2026-10-28.
        region_city = self.source.get('district_cities', {}).get(''.join(location.split()), '')
        resolved_location = f'{region_city}市{location}' if region_city else location
        city, district, work_address = work_location_details(resolved_location, result.description)
        description = f'工作地点：{location}\n\n{result.description}'
        if fields.get('联系地址'):
            description += f"\n\n联系地址（仅供参考）：{fields['联系地址']}"
        values = result.to_dict()
        values.update(city=city, district=district, work_address=work_address or result.work_address,
            education_raw=fields.get('最低学历要求') or job.education_raw,
            experience_raw=fields.get('工作年限') or job.experience_raw,
            employment_type=fields.get('工作性质', ''),
            description=description)
        return Job.from_mapping(values)

    def empty_message(self, page):
        if (not source_url_allowed(page.url, self.source['domains'])
                or urlsplit(page.url).path != '/person/search_result.jsp'):
            return ''
        # This server-rendered search page leaves its known list empty without
        # displaying a separate "no results" message. Check the complete shell.
        empty = page.run_js('''return document.readyState==='complete'
            && !!document.querySelector('#keyword') && !!document.querySelector('.sresult_tit')
            && !!document.querySelector('.sjoblist') && !!document.querySelector('.joblistctl')
            && !document.querySelector('.sjoblist .sjob');''')
        return '当前搜索页未显示岗位条目，可调整关键词或地区后重新采集' if empty else ''
