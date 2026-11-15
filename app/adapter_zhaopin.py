"""Visible split-list controls used by the current Zhaopin desktop site."""
import re
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit, urlunsplit

from app.adapters import CitySelectionError, DomAdapter, source_url_allowed
from app.models import Job, sanitize_diagnostic_text


CARD_FIELDS = '''
    const field = s => e.querySelector(s)?.innerText.trim() || '';
    return {title:e.querySelector('.vue-clamp__text')?.getAttribute('aria-label') || field('.job-card__title-clamp'),
        company_name:field('.job-card__company-name'),salary_raw:field('.job-card__salary'),
        location:field('.job-card__location'),tags:field('.job-card__skill-tags')};
'''


class ZhaopinAdapter(DomAdapter):
    def __init__(self, source):
        super().__init__(source)
        self.locators = {}

    def snapshot(self, page, status=0):
        snapshot = super().snapshot(page, status)
        masked = page.run_js(r'''return Array.from(document.querySelectorAll('.job-card__salary'))
            .some(e=>e.innerText.includes('*') && !/\d/.test(e.innerText));''')
        if masked:
            snapshot['login'] = True
        return snapshot

    def validate_search_selection(self, page, keyword, city):
        code = self.source.get('cities', {}).get(city)
        if not code:
            return False
        parts = urlsplit(page.url)
        query = parse_qs(parts.query)
        path_city = re.search(r'/sou/jl(\d+)(?:/|$)', parts.path)
        submitted = query.get('jl') if 'jl' in query else [path_city.group(1)] if path_city else []
        fields = self._search_evidence(page, {
            'keyword': '.query-sug__input',
            'city': '.filter-region-box .filter-select-box__label'})
        return (submitted == [code] and fields.get('city') == [city] and fields.get('loaded') is True
                and (keyword is None or fields.get('keyword') == [keyword]))

    def search(self, session, keyword, city, gate, check_page, delay, interval):
        code = self.source.get('cities', {}).get(city)
        if not code:
            raise CitySelectionError('该城市尚未确认，请在浏览器手动筛选后使用“采集当前页”')
        check_page()
        gate()
        entry = session.page.ele('css:.search-wrapper__input', timeout=2)
        if not entry:
            raise ValueError('未找到智联首页搜索框，请回到首页后重试')
        entry.input(keyword, clear=True)
        delay(.4)
        href = session.page.run_js('return document.querySelector(".search-wrapper__button")?.href || "";')
        if not source_url_allowed(href, self.source['domains']) or not re.search(r'/sou/jl\d+/kw[^/]+$', urlsplit(href).path):
            raise ValueError('未取得页面生成的关键词搜索链接，已停止')
        # Preserve the keyword encoding produced by the visible search control.
        href = re.sub(r'/sou/jl\d+/', f'/sou/jl{code}/', href, count=1)
        delay(interval)
        gate()
        session.navigate(href, self.source)
        check_page()
        for attempt in range(13):
            check_page()
            verified = session.page.run_js('''return {keyword:document.querySelector('.query-sug__input')?.value,
                city:document.querySelector('.filter-region-box .filter-select-box__label')?.innerText.trim()};''') or {}
            if (verified.get('keyword') == keyword and verified.get('city') == city
                    and parse_qs(urlsplit(session.page.url).query).get('jl') == [code]):
                return
            if attempt < 12:
                delay(.5)
        else:
            raise CitySelectionError('智联搜索条件未得到页面确认，请人工核对后采集当前页')

    def read_cards(self, page):
        items = page.run_js('return Array.from(document.querySelectorAll(".job-card")).slice(0,200).map((e,index)=>({'
                            'index,fields:(()=>{' + CARD_FIELDS + '})()}));') or []
        self.locators.clear()
        jobs = []
        for item in items:
            fields = item['fields']
            if not fields.get('title') or not fields.get('company_name'):
                continue
            location = fields.get('location', '').split()
            tags = fields.get('tags', '')
            job = Job.from_mapping({**fields, 'source':self.source['id'], 'city':location[0] if location else '',
                'district':' '.join(location[1:]), 'identity_hint':f'{page.tab_id}:{item["index"]}',
                'education_raw':next((word for word in ('博士','硕士','本科','大专','高中','中专','学历不限') if word in tags),'')})
            self.locators[job.record_key] = (item['index'], fields)
            jobs.append(job)
        return jobs

    def read_work_address(self, session, job, gate, check_page, delay, interval):
        """Read the same verified job's public workplace card in a temporary tab."""
        original, detail = session.page, None
        try:
            delay(interval)
            gate()
            detail = session.new_detail_tab()
            session.page = detail
            session.navigate(job.source_url, self.source)
            evidence = {}
            for attempt in range(13):
                check_page()
                gate()
                evidence = session.page.run_js(r'''
                    const visible = e => !!e && e.getClientRects().length > 0
                        && getComputedStyle(e).visibility !== 'hidden';
                    const locations = Array.from(document.querySelectorAll('.seo-card')).filter(card =>
                        visible(card) && /^(工作地点|工作地址)$/.test(card.querySelector('.seo-card__title')?.innerText.trim() || ''));
                    const content = locations.length === 1 ? locations[0].querySelector('.seo-card__content') : null;
                    // SEO titles can abbreviate employers; use the visible job and company names.
                    const jobTitles = Array.from(document.querySelectorAll('.summary-planes__title')).filter(visible)
                        .map(e => e.innerText.trim());
                    const companies = Array.from(new Set(Array.from(document.querySelectorAll(
                        '.company-info__name, .company-summary__name-link')).filter(visible).map(e => e.innerText.trim())));
                    return {url:location.href, job_titles:jobTitles, companies,
                        address:visible(content) ? content.innerText.trim() : ''};
                ''') or {}
                if (evidence.get('job_titles') == [job.title] and evidence.get('companies') == [job.company_name]
                        and evidence.get('address')):
                    break
                if attempt < 12:
                    delay(.5)
            current = urlsplit(evidence.get('url', ''))
            if not source_url_allowed(evidence.get('url', ''), self.source['domains']):
                raise ValueError('智联独立详情来源不符，未读取工作地点')
            if current.path != '/jobdetail/' + job.source_job_id + '.htm':
                raise ValueError('智联独立详情岗位 ID 与已确认岗位不一致，未读取工作地点')
            if evidence.get('job_titles') != [job.title]:
                raise ValueError('智联独立详情可见岗位名称缺失、冲突或与已确认岗位不一致，未读取工作地点')
            if evidence.get('companies') != [job.company_name]:
                raise ValueError('智联独立详情可见公司名称缺失、冲突或与已确认公司不一致，未读取工作地点')
            return evidence.get('address') or job.work_address
        finally:
            session.page = original
            if detail is not None and detail.states.is_alive:
                detail.close()

    def read_job_detail(self, session, job, gate, check_page, delay, interval, control):
        index, expected = self.locators[job.record_key]
        delay(interval)
        check_page()
        gate()
        page = session.page
        before = page.run_js('return document.querySelector(arguments[0])?.href||"";',
                             '.job-detail-panel a[href*="/jobdetail/"]')
        data = {}
        for click_attempt in range(2):
            if click_attempt:
                delay(interval)
                check_page()
                gate()
            cards = page.eles('css:.job-card')
            if index >= len(cards):
                raise ValueError('智联列表发生变化，请重新采集当前页')
            card = cards[index]
            if card.run_js('const e=this;' + CARD_FIELDS) != expected:
                raise ValueError('智联卡片字段发生变化，已停止，避免匹配错误岗位')
            if not click_attempt:
                # Keep this first transition requirement when re-clicking an already active card.
                changed = not card.run_js('return this.classList.contains("job-card--active");')
            card.click()
            for attempt in range(13):
                check_page()
                gate()
                data = session.page.run_js('''
                    const active=document.querySelector('.job-card--active');
                    const e=active;
                    return {active:e ? (()=>{''' + CARD_FIELDS + '''})() : null,
                        title:document.querySelector('.job-detail-summary__title-text')?.innerText.trim(),
                        company:document.querySelector('.job-company-info__name')?.innerText.trim(),
                        href:document.querySelector('.job-detail-panel a[href*="/jobdetail/"]')?.href,
                        salary:document.querySelector('.job-detail-summary__salary')?.innerText.trim(),
                        tags:Array.from(document.querySelectorAll('.job-detail-summary__tag')).map(e=>e.innerText.trim()),
                        description:document.querySelector('.job-description__content')?.innerText};
                ''') or {}
                ready = (data.get('active') == expected and data.get('title') == job.title
                         and data.get('company') == job.company_name and data.get('description')
                         and data.get('href') and (not changed or data['href'] != before))
                if ready:
                    break
                if attempt < 12:
                    delay(.5)
            if ready:
                break
        else:
            mismatches = []
            if data.get('active') != expected:
                active = data.get('active') or {}
                fields = [name for name in expected if active.get(name) != expected[name]]
                mismatches.append('选中卡片字段[' + ','.join(fields) + ']')
            if data.get('title') != job.title:
                mismatches.append('岗位名称')
            if data.get('company') != job.company_name:
                mismatches.append('公司名称')
            if not data.get('href'):
                mismatches.append('岗位链接未加载')
            elif changed and data['href'] == before:
                mismatches.append('岗位链接未切换')
            if not data.get('description'):
                mismatches.append('职位描述未加载')
            identifiers = []
            for href in (before, data.get('href') or ''):
                match = re.fullmatch(r'/jobdetail/([A-Za-z0-9]+)\.htm', urlsplit(href).path)
                identifiers.append(match.group(1) if match else '缺失或不可确认')
            raise ValueError(sanitize_diagnostic_text(
                f'智联详情两次点击后仍未就绪：卡片 {index + 1}；不一致={"、".join(mismatches)}；'
                f'期望岗位={job.title} / {job.company_name}；'
                f'实际岗位={data.get("title") or "缺失"} / {data.get("company") or "缺失"}；'
                f'切换前岗位 ID={identifiers[0]}；当前岗位 ID={identifiers[1]}；'
                f'描述已加载={bool(data.get("description"))}'))
        parts = urlsplit(data['href'])
        match = re.fullmatch(r'/jobdetail/([A-Za-z0-9]+)\.htm', parts.path)
        if not match or not source_url_allowed(data['href'], self.source['domains']):
            raise ValueError('智联未提供可确认的岗位链接和 ID，已停止')
        tags = data.get('tags') or []
        location = re.split(r'[·\s]+', tags[0]) if tags else []
        if not location or location[0] != job.city:
            raise ValueError('智联详情与列表城市不符，已停止')
        if job.district:
            normalize = lambda value: re.sub(r'[区县市]$', '', value.strip())
            expected_district = job.district.split()[0]
            if len(location) < 2 or normalize(location[1]) != normalize(expected_district):
                raise ValueError('智联详情与列表行政区不符，已停止')
        experience = next((tag for tag in tags if re.search(r'年|经验|应届|在校',tag)), '')
        education = next((tag for tag in tags if re.search(r'博士|硕士|本科|大专|高中|中专|学历',tag)),job.education_raw)
        values = job.to_dict()
        values.update(source_job_id=match.group(1),source_url=urlunsplit((parts.scheme,parts.netloc,parts.path,'','')),
            identity_hint='',description=data['description'],salary_raw=data.get('salary') or job.salary_raw,
            district=' '.join(location[1:]) or job.district,experience_raw=experience,education_raw=education)
        identified = replace(job, source_job_id=values['source_job_id'], source_url=values['source_url'])
        values['work_address'] = self.read_work_address(session, identified, gate, check_page, delay, interval)
        return Job.from_mapping(values)
