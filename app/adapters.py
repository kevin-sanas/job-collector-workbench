import json
import hashlib
import re
from dataclasses import replace
from urllib.parse import parse_qs, quote, urljoin, urlsplit, urlunsplit

from lxml import html

from app.models import Job, canonical_url, redact_text
from app.fonts import DisplayFontDecoder


class CitySelectionError(ValueError):
    """A city control could not be verified; human correction may resume it."""


def source_url_allowed(url: str, domains: list[str]) -> bool:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or '').lower()
        return (parts.scheme in ('http', 'https') and not parts.username and not parts.password
                and any(host == domain or host.endswith('.' + domain) for domain in domains))
    except ValueError:
        return False


def detect_state(snapshot: dict) -> str:
    text = snapshot.get('text', '')
    if snapshot.get('status') in (403, 429) or any(word in text for word in ('访问过于频繁', '访问频繁，请', '请求过于频繁', '访问已被限制', '账号已被封禁', 'Access Denied')):
        return 'blocked'
    if snapshot.get('verification'):
        return 'waiting_verification'
    if snapshot.get('login'):
        return 'waiting_login'
    if snapshot.get('overlay'):
        return 'waiting_user'
    return 'ready'


class DomAdapter:
    def __init__(self, source: dict):
        self.source = source
        self.selectors = source.get('selectors', {})
        self.font_decoder = DisplayFontDecoder(source.get('font_selector', ''))

    def search_url(self, keyword: str, city: str) -> str:
        if not self.source.get('search_url'):
            raise ValueError('该来源尚未适配搜索，请手动进入列表后使用“采集当前页”')
        cities = self.source.get('cities', {})
        if city not in cities:
            raise CitySelectionError('该城市尚无可靠的站点编码，请先在浏览器手动筛选，再使用“采集当前页”')
        return self.source['search_url'].format(keyword=quote(keyword), city=cities[city])

    def _search_evidence(self, page, selectors):
        """Read visible controls and loaded results together at the DOM boundary."""
        return page.run_js('''
            const selectors=arguments[0];
            const visible=e=>!!e && e.getClientRects().length>0 && getComputedStyle(e).visibility!=='hidden';
            const result={};
            for(const [name,selector] of Object.entries(selectors))
                result[name]=Array.from(document.querySelectorAll(selector)).filter(visible)
                    .map(e=>'value' in e ? e.value : e.innerText.trim());
            result.loaded=Array.from(arguments[1] ? document.querySelectorAll(arguments[1]) : []).some(visible)
                || ['暂无相关职位','没有找到相关职位','暂无符合条件的职位'].some(
                    marker=>(document.body?.innerText||'').includes(marker));
            return result;
        ''', selectors, self.selectors.get('card', '')) or {}

    def validate_city_selection(self, page, city):
        return self.validate_search_selection(page, None, city)

    def validate_search_selection(self, page, keyword, city):
        # URL searches keep the site's submitted city/keyword parameters visible.
        route = {'liepin': ('/zhaopin/', 'dq', 'key'),
                 'boss': ('/web/geek/jobs', 'city', 'query')}.get(self.source.get('id'))
        if route is None or city not in self.source.get('cities', {}):
            return False
        path, city_key, keyword_key = route
        parts = urlsplit(page.url)
        query = parse_qs(parts.query, keep_blank_values=True)
        return (parts.path == path and query.get(city_key) == [self.source['cities'][city]]
                and (keyword is None or query.get(keyword_key) == [keyword])
                and self._search_evidence(page, {}).get('loaded') is True)

    def parse_html(self, markup: str, base_url: str) -> list[Job]:
        if not source_url_allowed(base_url, self.source['domains']):
            raise ValueError('当前页面不属于所选来源')
        root = html.fromstring(markup or '<div/>')
        jobs = []
        card_selector = self.selectors.get('card', '')
        cards = root.cssselect(card_selector) if card_selector else []

        def text(node, field):
            selector = self.selectors.get(field, '')
            nodes = node.cssselect(selector) if selector else []
            return ' '.join(' '.join(n.itertext()).strip() for n in nodes).strip()

        for card in cards:
            title, company = text(card, 'title'), text(card, 'company')
            if not title or not company:
                continue
            anchors = card.cssselect(self.selectors.get('link') or 'a[href]')
            url = canonical_url(urljoin(base_url, anchors[0].get('href', ''))) if anchors else ''
            if url and not source_url_allowed(url, self.source['domains']):
                continue
            identifier = re.search(self.source.get('job_id_pattern', r'/job_detail/([^/?]+?)(?:\.html)?$'), urlsplit(url).path)
            if self.source.get('job_id_pattern') and not identifier:
                continue
            if identifier and self.source.get('canonical_job_path'):
                parts = urlsplit(url)
                url = urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))
            tags = text(card, 'tags')
            education = next((word for word in ('博士', '硕士', '本科', '大专', '专科', '高中', '中专', '学历不限') if word in tags), '')
            experience = re.search(r'\d+(?:[-~]\d+)?年(?:以上|以下)?|经验不限|应届(?:毕业)?生|在校生', tags)
            salary = text(card, 'salary')
            if any(0xE000 <= ord(char) <= 0xF8FF for char in salary):
                salary = '未解析（页面自定义字体，需人工核对）'
            location = re.split(self.source.get('location_separator', '·'), text(card, 'city'), maxsplit=1)
            jobs.append(Job.from_mapping({'source': self.source['id'], 'source_job_id': identifier.group(1) if identifier else '',
                'title': title, 'company_name': company, 'city': location[0], 'district': location[1] if len(location)>1 else '',
                'salary_raw': salary, 'education_raw': education,
                'experience_raw': text(card,'experience') or (experience.group(0) if experience else ''),
                'employment_type':text(card,'employment_type'), 'description': text(card, 'description'), 'source_url': url}))
        if jobs:
            return jobs
        # Structured JobPosting is useful on employer career pages and some details.
        for script in root.cssselect('script[type="application/ld+json"]'):
            try:
                data = json.loads(script.text or '{}')
            except (ValueError, TypeError):
                continue
            entries = data if isinstance(data, list) else data.get('@graph', [data]) if isinstance(data, dict) else []
            for entry in entries:
                if not isinstance(entry, dict) or entry.get('@type') != 'JobPosting':
                    continue
                company = entry.get('hiringOrganization', {})
                company = company.get('name', '') if isinstance(company, dict) else str(company)
                location = entry.get('jobLocation', {})
                if isinstance(location, list):
                    location = location[0] if location else {}
                address = location.get('address', {}) if isinstance(location, dict) else {}
                description = html.fromstring('<div>' + str(entry.get('description', '')) + '</div>').text_content()
                try:
                    entry_url = entry.get('url', '')
                    entry_url = canonical_url(urljoin(base_url, entry_url)) if isinstance(entry_url, str) and entry_url else ''
                    if entry_url and not source_url_allowed(entry_url, self.source['domains']):
                        continue
                    identifier = entry.get('identifier', '')
                    identifier = identifier.get('value', '') if isinstance(identifier, dict) else identifier
                    hint = hashlib.sha256(json.dumps([entry.get('title'), company, address, description], ensure_ascii=False, sort_keys=True).encode()).hexdigest() if not entry_url and not identifier and len(entries) > 1 else ''
                    jobs.append(Job.from_mapping({'source': self.source['id'], 'title': entry.get('title', ''),
                        'company_name': company, 'city': address.get('addressLocality', '') if isinstance(address, dict) else '',
                        'description': description, 'source_url': entry_url or base_url, 'source_job_id': identifier,
                        'identity_hint': hint, 'published_raw': entry.get('datePosted', '')}))
                except ValueError:
                    continue
        return jobs

    def read_detail(self, page, job):
        pattern = self.source.get('job_id_pattern')
        if pattern:
            match = re.search(pattern, urlsplit(page.url).path)
            if not match or match.group(1) != job.source_job_id:
                raise ValueError('详情页岗位 ID 与列表不符，已停止写入')
        title_selector = self.source.get('detail_title_selector')
        if title_selector:
            title = page.run_js('return document.querySelector(arguments[0])?.innerText.trim()||"";', title_selector)
            if title != job.title:
                raise ValueError('详情页标题与列表不符，已停止写入，请检查页面是否变化')
        company_selector = self.source.get('detail_company_selector')
        if company_selector:
            company = page.run_js('return document.querySelector(arguments[0])?.innerText.trim()||"";',company_selector)
            if company != job.company_name:
                raise ValueError('详情页公司与列表不符，已停止写入')
        description = page.run_js('return document.querySelector(arguments[0])?.innerText || "";', self.source['detail_selector'])
        description = self.font_decoder.decode(page, str(description or ''))
        if self.source.get('detail_title_selector') and not description.strip():
            raise ValueError('未读取到职责区域，已停止，请人工检查详情结构')
        work_address = ''
        address_label = self.source.get('detail_work_address_label')
        address_selector = self.source.get('detail_work_address_selector')
        if address_label:
            visible_text = page.run_js('return document.body?.innerText || "";') or ''
            lines = [line.strip() for line in str(visible_text).splitlines() if line.strip()]
            for index, line in enumerate(lines):
                match = re.fullmatch(re.escape(address_label) + r'\s*[:：]\s*(.*)', line)
                if not match:
                    continue
                value = match.group(1).strip()
                if not value and index + 1 < len(lines):
                    value = lines[index + 1]
                # An empty address must not consume the next company-info field.
                if not re.match(r'^[^:：]{1,20}[:：]', value):
                    work_address = value
                break
        elif address_selector:
            work_address = page.run_js('''
                const element = Array.from(document.querySelectorAll(arguments[0])).find(
                    e => e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden');
                return element?.innerText.trim() || '';
            ''', address_selector)
        return replace(job, description=redact_text(description) or job.description,
                       work_address=redact_text(work_address) or job.work_address)

    def read_cards(self, page) -> list[Job]:
        selector = self.selectors.get('card', '')
        if selector:
            markup = page.run_js('return Array.from(document.querySelectorAll(arguments[0])).slice(0, 200).map(e => e.outerHTML).join("\\n");', selector)
        else:
            markup = page.run_js('return Array.from(document.querySelectorAll(\'script[type="application/ld+json"]\')).map(e => e.outerHTML).join("\\n");')
        markup = self.font_decoder.decode(page, str(markup or ''))
        return self.parse_html('<div>' + markup + '</div>', page.url)

    def snapshot(self, page, status: int = 0) -> dict:
        snapshot = page.run_js('''
            const visible = e => !!e && e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
            const any = selector => Array.from(document.querySelectorAll(selector)).some(visible);
            return {
                text: (document.body?.innerText || '').slice(0, 6000),
                verification: any(arguments[0]),
                login: any(arguments[1]),
                overlay: any('[role="dialog"], .dialog-wrap, .modal-mask')
            };
        ''', self.source.get('verification_selector', 'iframe[src*="captcha"], iframe[src*="verify"], .geetest_panel, .nc-container, #captcha, .verify-wrap'),
             self.source.get('login_selector', '.login-dialog, .login-register-modal, .sign-wrap, .login-container input[type="password"]')) or {}
        if '/web/user/' in page.url or '/login' in urlsplit(page.url).path or '/passport/' in page.url:
            snapshot['login'] = True
        if '/verify' in urlsplit(page.url).path or any(t in snapshot.get('text', '')[:500] for t in ('请完成安全验证', '请拖动滑块', '完成验证后继续')):
            snapshot['verification'] = True
        snapshot['status'] = status
        return snapshot

    def diagnostic(self, page) -> dict:
        if not source_url_allowed(page.url, self.source['domains']):
            raise ValueError('请先回到所选来源的岗位页面')
        selector = self.selectors.get('card') or 'main, [role="main"]'
        sample = page.run_js('return Array.from(document.querySelectorAll(arguments[0])).slice(0, 2).map(e => e.outerHTML).join("\\n");', selector) or ''
        root = html.fromstring('<div>' + sample[:200000] + '</div>')
        for node in root.xpath('.//script|.//style|.//input|.//textarea|.//iframe|.//img|.//svg'):
            node.drop_tree()
        for node in root.iter():
            for attr in list(node.attrib):
                if attr not in ('class', 'id', 'role'):
                    del node.attrib[attr]
        parts = urlsplit(page.url)
        return {'source': self.source['name'], 'page_path': parts.scheme + '://' + parts.netloc + parts.path,
            'state': detect_state(self.snapshot(page)), 'selector': selector,
            'sample_html': redact_text(html.tostring(root, encoding='unicode'))[:24000],
            'note': '仅包含至多两个局部节点，已删除链接、输入、脚本及非必要属性。发送前请在本机再次检查姓名等个人信息。若样本为空，请按 README 手动复制单张岗位卡片。'}


def adapter_for(source):
    if source.get('adapter') == 'job168':
        from app.adapter_job168 import Job168Adapter
        return Job168Adapter(source)
    if source.get('adapter') == 'public_fields':
        from app.adapter_public import PublicPageAdapter
        return PublicPageAdapter(source)
    if source.get('adapter') == '51job':
        from app.adapter_51job import Jobs51Adapter
        return Jobs51Adapter(source)
    if source.get('adapter') == 'zhaopin':
        from app.adapter_zhaopin import ZhaopinAdapter
        return ZhaopinAdapter(source)
    if source.get('adapter') == 'chinahr':
        from app.adapter_chinahr import ChinaHRAdapter
        return ChinaHRAdapter(source)
    return DomAdapter(source)
