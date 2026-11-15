"""Read configured public job fields without copying complete card markup."""
import re
from dataclasses import replace
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from app.adapters import CitySelectionError, DomAdapter, detect_state, source_url_allowed
from app.models import Job, redact_text


def explicit_city(location):
    """Extract only cities and districts explicitly labelled by administrative suffixes."""
    text = re.sub(r'\s+', '', location)
    municipality = re.match(r'^(北京|上海|天津|重庆)市', text)
    if municipality:
        city, rest = municipality.group(1), text[municipality.end():]
    else:
        province = re.match(r'^(?:[^省]+省|内蒙古自治区|广西壮族自治区|西藏自治区|宁夏回族自治区|新疆维吾尔自治区)',text)
        rest = text[province.end():] if province else text
        match = re.match(r'([^市省区县]+)市',rest)
        if not match:
            return '', ''
        city_name = match.group(1)
        if any(token in city_name for token in ('盟','自治','地区')) or '州' in city_name[:-1]:
            return '', ''
        city, rest = city_name, rest[match.end():]
    district = re.match(r'([^区县]+[区县])',rest)
    return city, district.group(1) if district else ''


def work_location_details(location, description, allow_broad_address=False):
    """Use job-labelled locations, keeping broad or multiple places off the map."""
    compact = re.sub(r'\s+', '', location)
    city, district = explicit_city(compact)
    multiple_region = bool(re.search(r'[,，、;；/|]', compact))
    if multiple_region:
        city, district = '', ''
    elif not city and re.fullmatch(r'[^省市区县\d,，、;；/|]+[区县]', compact):
        district = compact
    locations = []
    for match in re.finditer(r'(?:^|[\n。；;])\s*(?:\d+[、.．]\s*)?(?:工作地点|工作地址|办公地址|上班地址)\s*[:：]\s*([^\n。；;]+)', description):
        value = re.split(r'(?:工作时间|工作制度|工作作息|岗位要求|任职要求|备注|薪酬|福利|公司简介|公司地址|联系地址|联系方式)\s*[:：]', match.group(1), maxsplit=1)[0].strip()
        if value:
            locations.append(value)
    # A district alone is retained; its city needs an explicit matching job
    # location. Company names, contact addresses and the selected search city
    # are not evidence of where this particular job is performed.
    regions = {explicit_city(part.strip()) for value in locations for part in re.split(r'[,，、;；/|]', value)}
    if len(regions) == 1:
        location_city, location_district = next(iter(regions))
        if district and location_district == district and location_city:
            city = city or location_city
        elif city and location_city == city and not district:
            district = location_district
    addresses = set()
    multiple_address = multiple_region
    for value in [location, *locations]:
        value = re.sub(r'\s+', ' ', value).strip()
        if not re.search(r'路|街|道|巷|弄|号|大厦|大楼|园区|工业园|科技园|产业园|广场|中心|基地|栋|幢|楼|室|大院', value):
            continue
        if re.search(r'[,，、;；/|]|二选一|多地|就近|根据.*安排|待定|另行|面议', value):
            multiple_address = True
        else:
            addresses.add(value)
    work_address = ''
    if (city or allow_broad_address) and len(addresses) == 1 and not multiple_address:
        address = next(iter(addresses))
        address_city, address_district = explicit_city(address)
        if (not city or not address_city or address_city == city) and (not address_district or not district or address_district == district):
            work_address = address
    return city, district, work_address


class PublicPageAdapter(DomAdapter):
    def identity(self, url):
        if not source_url_allowed(url, self.source['domains']):
            return '', ''
        parts = urlsplit(url)
        path_pattern = self.source.get('job_path_pattern')
        if path_pattern and not re.fullmatch(path_pattern, parts.path):
            return '', ''
        query_name = self.source.get('job_id_query')
        if query_name:
            values = parse_qs(parts.query).get(query_name, [])
            identifier = values[0] if len(values) == 1 and re.fullmatch(r'\d+', values[0]) else ''
        else:
            match = re.fullmatch(self.source['job_id_pattern'], parts.path)
            identifier = match.group(1) if match else ''
        if not identifier:
            return '', ''
        keep = self.source.get('job_query_keys', [])
        query = [(key, value) for key, value in parse_qsl(parts.query) if key in keep]
        return identifier, urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(sorted(query)), ''))

    def fields(self, page, limit=200):
        selectors = {key:value for key,value in self.selectors.items() if key not in ('card','link')}
        return page.run_js('''
            const selectors=arguments[1];
            return Array.from(document.querySelectorAll(arguments[0])).filter(e=>e.getClientRects().length)
                .slice(0,arguments[3]).map(e=>{
                    const result={};
                    for (const [field,selector] of Object.entries(selectors))
                        result[field]=Array.from(e.querySelectorAll(selector)).map(n=>n.innerText.trim()).join(' ').trim();
                    result.href=e.querySelector(arguments[2])?.href || '';
                    return result;
                });
        ''', self.selectors['card'], selectors, self.selectors['link'], limit) or []

    def read_cards(self, page):
        if not source_url_allowed(page.url, self.source['domains']):
            raise ValueError('当前页面不属于所选来源')
        jobs = []
        for item in self.fields(page):
            identifier, url = self.identity(item.get('href',''))
            if not identifier or not item.get('title') or not item.get('company'):
                continue
            values = {'source':self.source['id'],'source_job_id':identifier,'source_url':url,
                'title':item['title'],'company_name':item['company'],'salary_raw':item.get('salary',''),
                'description':item.get('description','')}
            if self.source.get('list_format') == 'jobcn':
                requirements = [part.strip() for part in item.get('requirements','').split('|')]
                if len(requirements) < 3:
                    continue
                location = re.split(r'[-·]',requirements[0],maxsplit=1)
                values.update(city=location[0],district=location[1] if len(location)>1 else '',
                    education_raw=requirements[1],experience_raw=requirements[2],
                    published_raw=requirements[3] if len(requirements)>3 else '')
                if re.search(r'\.\.\.|…', requirements[0]):
                    # The site truncates multi-location labels such as 深圳... .
                    # A search filter cannot supply the missing job location.
                    values.update(city='', district='', description='列表工作地区（显示已截断）：' + requirements[0])
            else:
                values.update(education_raw=item.get('education','').split('|')[0].strip(),
                    experience_raw=item.get('experience',''),employment_type=item.get('employment_type',''))
                region = item.get('region','').split('|')[0].strip()
                # NCSS list locations are province-level; do not label them cities.
                if region:
                    values['description'] = f'列表地区：{region}（省级；未从列表确认城市）'
            jobs.append(Job.from_mapping(values))
        return jobs

    def read_detail(self, page, job):
        identifier, _ = self.identity(page.url)
        if identifier != job.source_job_id:
            raise ValueError('详情岗位 ID 与列表不符，已停止写入')
        result = super().read_detail(page, job)
        selector = self.source.get('detail_location_selector')
        location = ''
        if selector:
            location = page.run_js('return document.querySelector(arguments[0])?.innerText.trim()||"";',selector)
            if job.city and (not location or job.city not in location):
                raise ValueError('详情工作地点与列表不符，已停止写入')
        if self.source.get('list_format') == 'jobcn':
            addresses = page.run_js("""
                const selector = '.address_pos_body dl' +
                    (document.querySelector('.pos_primary .pos_base > .pos_name') ? ', .pos_detail_company_base dl' : '');
                return Array.from(document.querySelectorAll(selector))
                    .filter(e => e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden')
                    .map(e => {
                        const address = e.querySelector('.tab_map_title') || e.querySelector('dd');
                        return {label: e.querySelector('dt')?.innerText.trim() || '',
                            company: !!e.closest('.pos_detail_company_base'),
                            value: Array.from(address?.childNodes || [])
                                .filter(n => n.nodeType === Node.TEXT_NODE)
                                .map(n => n.textContent).join(' ').trim()};
                    });
            """) or []
            work_address = next((item.get('value', '') for item in addresses
                                 if not item.get('company') and item.get('label', '').rstrip('：:').strip() == '工作地址'), '')
            description = result.description
            if not work_address:
                work_address = next((item.get('value', '') for item in addresses
                                     if item.get('company') and item.get('label', '').rstrip('：:').strip() == '地址'), '')
                if work_address:
                    description += '\n\n地址来源：卓博公司信息地址（待核对）'
            if job.description:
                description += '\n\n' + job.description
                if location:
                    description += '\n详情职位属性：' + location
            return replace(result, work_address=redact_text(work_address) or result.work_address,
                           description=description)
        if self.source.get('list_format') == 'ncss':
            fields = page.run_js('''const result={};for(const [field,selector] of Object.entries(arguments[0]))
                result[field]=document.querySelector(selector)?.innerText.trim()||'';return result;''',self.source['detail_fields']) or {}
            location = fields.get('location','')
            if not location:
                raise ValueError('详情工作地点区域为空，已停止，请人工确认页面结构')
            original_region = re.match(r'列表地区：(.+?)（省级',job.description)
            if original_region and original_region.group(1) not in ('全国','不限') and not location.startswith(original_region.group(1)):
                raise ValueError('详情工作地点与列表地区不符，已停止写入')
            city, district, work_address = work_location_details(location, result.description,
                allow_broad_address=location in ('全国', '不限'))
            work_address = work_address or result.work_address
            description = f'工作地点：{location}\n\n{result.description}'
            company_address = fields.get('company_address', '')
            if company_address:
                description += f'\n\n公司所在地（仅供参考）：{company_address}'
                # An unresolved explicit job location must not be replaced by a company reference.
                if not work_address and not re.search(r'(?:工作地点|工作地址|办公地址|上班地址)\s*[:：]', result.description):
                    _, _, reference_address = work_location_details(company_address, '')
                    if reference_address:
                        work_address = company_address
                        description += '\n\n地址来源：国家平台所在地址（公司信息，待核对）'
            values = result.to_dict()
            values.update(city=city,district=district,work_address=work_address,
                salary_raw=fields.get('salary') or job.salary_raw,
                education_raw=fields.get('education') or job.education_raw,
                employment_type=fields.get('employment_type','').strip('[]【】'),
                published_raw=fields.get('published_raw',''),
                description=description)
            return Job.from_mapping(values)
        return result

    def validate_search_selection(self, page, keyword, city):
        if city not in self.source.get('cities', {}):
            return False
        site = self.source.get('id')
        option = self.source.get('city_options', {}).get(city, {})
        if site == 'jobcn':
            fields = self._search_evidence(page, {
                'keyword': '#search-form #search-keyword', 'city': '#search-form #search-work-location'})
            site_name = option.get('site_name', '' if city == '全国' else city)
            return (urlsplit(page.url).path == '/search/result.xhtml' and fields.get('city') == [site_name]
                    and fields.get('loaded') is True
                    and (keyword is None or fields.get('keyword') == [keyword]))
        if site == 'ncss':
            fields = self._search_evidence(page, {
                'keyword': '.searchKeywords', 'city': '.cascader-dl .cascader-input'})
            submitted = page.run_js('''return {
                code:document.querySelector('.cascader-dl .areacode-input')?.value,
                requestedCode:typeof parameter==='undefined' ? null : parameter.areaCode,
                requestedKeyword:typeof parameter==='undefined' ? null : parameter.jobName};''') or {}
            selected = fields.get('city') or []
            code = option.get('code', self.source['cities'][city])
            return (urlsplit(page.url).path == '/student/jobs/index.html' and len(selected) == 1
                    and selected[0].split('/')[-1].strip() == option.get('site_name', city)
                    and submitted.get('code') == code and submitted.get('requestedCode') == code
                    and fields.get('loaded') is True
                    and (keyword is None or (fields.get('keyword') == [keyword]
                                             and submitted.get('requestedKeyword') == keyword)))
        return super().validate_search_selection(page, keyword, city)

    def search(self, session, keyword, city, gate, check_page, delay, interval):
        if self.source.get('search_kind') != 'ncss' or city != '全国':
            raise CitySelectionError('该地区请在浏览器人工筛选，然后使用“采集当前页”')
        check_page()
        page = session.page
        region = page.run_js('return document.querySelector(".cascader-input")?.value||"";')
        if region != '全国':
            raise CitySelectionError('请先在国家平台将地区恢复为全国，或使用“采集当前页”保留人工筛选')
        gate()
        page.ele('css:.searchKeywords',timeout=2).input(keyword,clear=True)
        delay(interval)
        check_page()
        gate()
        page.ele('css:#jobSearch',timeout=2).click()
        delay(interval)
        check_page()
        verified = page.run_js('''return {keyword:document.querySelector('.searchKeywords')?.value,
            region:document.querySelector('.cascader-input')?.value};''') or {}
        if verified.get('keyword') != keyword or verified.get('region') != '全国':
            raise CitySelectionError('搜索条件未得到页面确认，请人工检查')

    def diagnostic(self, page):
        if not source_url_allowed(page.url,self.source['domains']):
            raise ValueError('请先返回所选来源的列表页')
        fields = self.fields(page,limit=2)
        return {'source':self.source['name'],'state':detect_state(self.snapshot(page)),
            'sample_fields':[{key:redact_text(value) for key,value in item.items() if key!='href'} for item in fields],
            'note':'仅选定的公开岗位字段，未读取整张卡片 HTML；发送前请再次检查描述中的个人信息。'}
