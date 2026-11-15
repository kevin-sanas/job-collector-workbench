import re
import time
from urllib.parse import urlsplit, urlunsplit

from app.adapters import CitySelectionError, DomAdapter, detect_state, source_url_allowed
from app.models import Job


class Jobs51Adapter(DomAdapter):
    """Read visible job fields and use the site's ordinary search/detail controls."""

    def __init__(self, source):
        super().__init__(source)
        self.locators = {}

    @staticmethod
    def _visible(root, selector):
        return [element for element in root.eles('css:' + selector, timeout=0)
                if element.run_js('return this.getClientRects().length>0 && getComputedStyle(this).visibility!=="hidden";')]

    def _city_dialog(self, page):
        for dialog in self._visible(page, '.el-dialog, [role="dialog"], [aria-modal="true"]'):
            labels = [dialog.attr('aria-label') or '']
            labels.extend(node.text.strip() for node in self._visible(
                dialog, '.jbs_cascader_dialog_title, .el-dialog__title, [role="heading"], h1, h2, h3'))
            if any(re.match(r'^(选择城市|城市选择|选择工作城市|工作城市)', label) for label in labels):
                return dialog
        return None

    def _workplace(self, page):
        rows = [row for row in self._visible(page, '.f')
                if any(label.text.strip() == '工作地点' for label in self._visible(row, '.label'))]
        if not rows:
            rows = self._visible(page, '[aria-label="城市筛选"], [aria-label="工作城市"]')
        return rows[0] if len(rows) == 1 else None

    def _selected_cities(self, page):
        workplace = self._workplace(page)
        if workplace is None:
            return []
        return list(dict.fromkeys(element.text.strip() for element in self._visible(workplace,
            'a.ch.on, [aria-label="当前城市"], [aria-selected="true"]')))

    def _keyword_inputs(self, page):
        inputs = self._visible(page, '#keywordInput')
        return inputs or self._visible(page,
            'input[placeholder*="关键词"], input[placeholder*="公司/职位"], input[placeholder="搜索职位"]')

    @staticmethod
    def _submitted_search(page):
        # Observe completed ordinary searches even after the browser's default
        # resource-timing buffer fills. Keep only the latest public conditions.
        return page.run_js('''
            const key='__collector51jobSearchEvidence';
            if(!window[key]) {
                const state={latest:null};
                const record=entries=>{for(const entry of entries) {
                    const url=new URL(entry.name);
                    if(url.pathname==='/api/job/search-pc') state.latest={
                        keyword:url.searchParams.get('keyword'),city:url.searchParams.get('jobArea'),
                        complete:entry.responseEnd>0};
                }};
                record(performance.getEntriesByType('resource'));
                state.observer=new PerformanceObserver(list=>record(list.getEntries()));
                state.observer.observe({type:'resource',buffered:true});
                window[key]=state;
            }
            return window[key].latest;
        ''') or {}

    def validate_city_selection(self, page, city):
        """Require one applied workplace city and its completed search results."""
        if self._city_dialog(page) is not None or self._selected_cities(page) != [city]:
            return False
        submitted = self._submitted_search(page)
        if (submitted.get('complete') is not True
                or submitted.get('city') != self.source.get('cities', {}).get(city)):
            return False
        cards = self._visible(page, '.joblist-item')
        results = self._visible(page, '.joblist, .joblist-empty, '
            '[aria-label="职位列表"], [aria-label="搜索结果"]')
        empty = any(marker in element.text for element in results for marker in (
            '暂无相关职位', '没有找到相关职位', '暂无符合条件的职位'))
        return bool(cards) or empty

    def validate_search_selection(self, page, keyword, city):
        inputs = self._keyword_inputs(page)
        return (len(inputs) == 1 and inputs[0].run_js('return this.value;') == keyword
                and self.validate_city_selection(page, city)
                and self._submitted_search(page).get('keyword') == keyword)

    def search(self, session, keyword, city, gate, check_page, delay, interval):
        if city not in self.source.get('cities', {}):
            raise CitySelectionError('该城市尚未配置，请在浏览器手动筛选后点击继续')

        def wait_for(condition, message):
            deadline = time.monotonic() + 8
            while True:
                gate()
                result = condition()
                if result:
                    return result
                if time.monotonic() >= deadline:
                    # Login, verification and access limits keep their existing gates.
                    if self._city_dialog(session.page) is None:
                        check_page()
                    raise CitySelectionError(message)
                delay(.2)

        gate()
        check_page()
        page = session.page
        try:
            # An old document can have lost its latest search before our observer
            # was installed. Ordinary navigation rebuilds that search context.
            if page.run_js("return !window.__collector51jobSearchEvidence && performance.getEntriesByType('resource').length>=250;"):
                delay(interval)
                gate()
                session.navigate(self.source['search_entry'], self.source)
                check_page()
            wait_for(lambda: len(self._keyword_inputs(session.page)) == 1
                     and self._submitted_search(session.page).get('complete') is True,
                     '51job 初始城市和搜索控件尚未就绪，请页面加载完成后点击继续')
            workplace = wait_for(lambda: self._workplace(session.page),
                '未确认 51job 工作地点筛选区域，请人工选择目标城市后点击继续')
            wait_for(lambda: self._visible(workplace, 'a.ch, .allcity, [aria-haspopup="dialog"], '
                     'button[aria-label="选择城市"], [role="button"][aria-label="选择城市"]'),
                     '未找到 51job 城市选择入口，请人工选择目标城市后点击继续')
            hot = next((item for item in self._visible(workplace, 'a.ch')
                        if item.text.strip() == city), None)
            if hot is not None:
                # Hot cities are multi-select too. Apply the target, then remove
                # every other selected city before reading any result cards.
                if city not in self._selected_cities(session.page):
                    delay(interval)
                    gate()
                    hot.click()
                    wait_for(lambda: city in self._selected_cities(session.page),
                             '热门城市尚未选中，请人工选择目标城市后点击继续')
                others = [name for name in self._selected_cities(session.page) if name != city]
                for name in others:
                    selected = next((item for item in self._visible(self._workplace(session.page), 'a.ch.on')
                                     if item.text.strip() == name), None)
                    if selected is None:
                        raise CitySelectionError('无法清除其他工作地点，请人工只保留目标城市后点击继续')
                    delay(interval)
                    gate()
                    selected.click()
                    wait_for(lambda: name not in self._selected_cities(session.page),
                             '其他工作地点仍处于选中状态，请人工只保留目标城市后点击继续')
                wait_for(lambda: self.validate_city_selection(session.page, city),
                         '目标城市尚未得到列表页面确认，请人工核对城市后点击继续')
            else:
                triggers = self._visible(workplace, '.allcity, [aria-haspopup="dialog"][aria-label*="城市"], '
                    'button[aria-label="选择城市"], [role="button"][aria-label="选择城市"]')
                if not triggers:
                    raise CitySelectionError('未找到 51job 城市选择入口，请人工选择目标城市后点击继续')
                triggers[0].click()
                dialog = wait_for(lambda: self._city_dialog(session.page),
                                  '未确认 51job 城市选择弹窗，请人工选择目标城市后点击继续')
                selected_selector = '.selected_list_wrapper_tag .el-tag, '
                selected_selector += '[aria-label="已选城市"] .el-tag, [role="option"][aria-selected="true"]'
                for tag in self._visible(dialog, selected_selector):
                    gate()
                    if tag.text.strip() != city:
                        if tag.attr('role') == 'option':
                            tag.click()
                        else:
                            closes = self._visible(tag, '.el-tag__close, [aria-label="删除"], [aria-label="移除"]')
                            if not closes:
                                raise CitySelectionError('无法清除其他已选城市，请人工只保留目标城市后点击继续')
                            closes[0].click()
                wait_for(lambda: all(tag.text.strip() == city for tag in self._visible(dialog, selected_selector)),
                         '其他城市仍处于选中状态，请人工只保留目标城市后点击继续')
                selected = [tag.text.strip() for tag in self._visible(dialog, selected_selector)]
                if city not in selected:
                    targets = self._visible(dialog, '.resumeDialog__top-city, .resumeDialog__right-city, [role="option"]')
                    target = next((item for item in targets if item.text.strip() == city), None)
                    if target is None:
                        raise CitySelectionError('51job 城市选择器未找到目标城市，请人工选择后点击继续')
                    gate()
                    target.click()
                wait_for(lambda: [tag.text.strip() for tag in self._visible(dialog, selected_selector)] == [city],
                         '未确认单一城市筛选，请人工只保留目标城市后点击继续')
                confirms = [item for item in self._visible(dialog, '.confirm_button, button, [role="button"]')
                            if 'confirm_button' in (item.attr('class') or '').split()
                            or re.fullmatch(r'(确定|确认)(?:\s*[（(]\d+[）)])?', item.text.strip())]
                if len(confirms) != 1:
                    raise CitySelectionError('未确认城市选择按钮，请人工确认目标城市后点击继续')
                delay(interval)
                gate()
                confirms[0].click()
                wait_for(lambda: self.validate_city_selection(session.page, city),
                         '目标城市尚未得到列表页面确认，请人工核对城市后点击继续')
        except RuntimeError as error:
            raise CitySelectionError('51job 城市选择操作未完成，请人工选择目标城市后点击继续') from error
        delay(interval)
        check_page()
        gate()
        page = session.page
        inputs = self._keyword_inputs(page)
        if len(inputs) != 1:
            raise CitySelectionError('未确认城市搜索的关键词输入框，请人工设置城市和关键词后点击继续')
        inputs[0].input(keyword, clear=True)
        delay(interval)
        gate()
        searches = [item for item in self._visible(page, '.search-btn, button, [role="button"]')
                    if 'search-btn' in (item.attr('class') or '').split()
                    or item.text.strip() in ('搜索', '搜职位', '查找职位')]
        if len(searches) != 1:
            raise CitySelectionError('未确认城市搜索按钮，请人工完成搜索后点击继续')
        searches[0].click()
        delay(interval)
        check_page()
        wait_for(lambda: self.validate_search_selection(session.page, keyword, city),
                 '搜索城市或关键词未得到页面确认，请人工完成搜索后点击继续')

    def read_cards(self, page):
        items = page.run_js('''
            return Array.from(document.querySelectorAll('.joblist-item')).slice(0,200).map((e,index)=>({
                index,title:e.querySelector('.jname')?.innerText.trim()||'',
                company_name:e.querySelector('.cname')?.innerText.trim()||'',
                salary_raw:e.querySelector('.sal')?.innerText.trim()||'',
                location:e.querySelector('.area .shrink-0')?.innerText.trim()||e.querySelector('.area')?.innerText.trim()||''
            }));
        ''') or []
        jobs = []
        self.locators.clear()
        for item in items:
            if not item.get('title') or not item.get('company_name'):
                continue
            location = re.split(r'[·\-]', item.get('location', ''), maxsplit=1)
            # This hint is only a temporary locator. Nothing is stored until an
            # ordinary detail click supplies the canonical site job identifier.
            job = Job.from_mapping({
                'source': self.source['id'], 'title': item['title'],
                'company_name': item['company_name'], 'salary_raw': item.get('salary_raw', ''),
                'city': location[0], 'district': location[1] if len(location) > 1 else '',
                'identity_hint': f'51job-card:{page.tab_id}:{item["index"]}',
            })
            self.locators[job.record_key] = item
            jobs.append(job)
        return jobs

    def read_job_detail(self, session, job, gate, check_page, delay, interval, control):
        original = session.page
        item = self.locators.get(job.record_key)
        if item is None:
            raise ValueError('岗位卡片定位已失效，请重新开始本次任务')
        gate()
        delay(interval)
        check_page()
        before = set()
        opened = set()
        detail = None

        def own_target(target):
            url = target.get('url', '')
            return (target.get('type') == 'page' and target.get('openerId') == original.tab_id
                    and (url == 'about:blank' or source_url_allowed(url, self.source['domains'])))

        def without_user_tabs(function):
            nonlocal before
            revision = control.wait_revision
            try:
                return function()
            finally:
                if control.wait_revision != revision:
                    # Even a cancelled manual wait may have opened user tabs.
                    # Exclude them before either selection or cleanup.
                    try:
                        before = set(original.browser.tab_ids)
                    except Exception:
                        before = set()

        try:
            identifier = None
            for click_attempt in range(2):
                # No pause point between final card verification and the click.
                # A resumed task must never reuse a previously checked ordinal.
                without_user_tabs(gate)
                without_user_tabs(check_page)
                card = original.run_js('''
                    const e=document.querySelectorAll('.joblist-item')[arguments[0]];
                    const location=e?.querySelector('.area .shrink-0')?.innerText.trim()||e?.querySelector('.area')?.innerText.trim()||'';
                    return e && e.querySelector('.jname')?.innerText.trim()===arguments[1]
                        && e.querySelector('.cname')?.innerText.trim()===arguments[2]
                        && location===arguments[3] && (e.querySelector('.sal')?.innerText.trim()||'')===arguments[4] ? e : null;
                ''', item['index'], item['title'], item['company_name'], item.get('location', ''), item.get('salary_raw', ''))
                if card is None:
                    raise ValueError('岗位列表已变化，已停止以避免采集错误的详情')
                before = set(original.browser.tab_ids)
                card.ele('css:.jname').click()
                deadline = time.monotonic() + 8
                handled_by_user = False
                while time.monotonic() < deadline:
                    targets = original.run_cdp('Target.getTargets').get('targetInfos', [])
                    created = {target['targetId'] for target in targets if target['targetId'] not in before and own_target(target)}
                    opened.update(created)
                    if len(created) > 1:
                        raise ValueError('出现多个新窗口，请人工确认后重新开始')
                    if created:
                        identifier = created.pop()
                        break
                    without_user_tabs(gate)
                    state = detect_state(self.snapshot(original, session.status))
                    if state != 'ready' or not source_url_allowed(original.url, self.source['domains']):
                        revision = control.resume_revision
                        without_user_tabs(check_page)
                        handled_by_user = control.resume_revision > revision
                        break
                    delay(.2)
                if identifier:
                    break
                if not handled_by_user or click_attempt:
                    break
                # A second click is allowed only after explicit human resume.
                delay(interval)
            if not identifier:
                raise ValueError('点击岗位后未发现详情窗口，请人工查看页面提示')
            detail = original.browser.get_tab(identifier)
            session.monitor_page(detail, self.source['domains'])
            session.page = detail
            while detail.url == 'about:blank' and time.monotonic() < deadline:
                gate()
                delay(.1)
            navigation_status = detail.run_js('return performance.getEntriesByType("navigation")[0]?.responseStatus || 0;')
            if session.status not in (403, 429) and navigation_status:
                session.status = int(navigation_status)
            check_page()
            parts = urlsplit(detail.url)
            match = re.fullmatch(r'/[^/]+/(\d+)\.html', parts.path)
            if parts.hostname != 'jobs.51job.com' or not match or not source_url_allowed(detail.url, self.source['domains']):
                raise ValueError('未进入可确认岗位 ID 的 51job 详情页，请人工查看提示')
            fields = detail.run_js(r'''
                const visible = e => e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
                const addressArea = document.querySelector('.job-address');
                const fullAddress = Array.from(addressArea?.querySelectorAll('.fp-bottom') || []).find(visible)?.innerText || '';
                const shortAddress = Array.from(addressArea?.querySelectorAll('.fp') || []).find(visible)?.innerText.trim() || '';
                return {work_address:fullAddress.match(/完整地址\s*[:：]\s*([\s\S]*)/)?.[1].trim() || shortAddress,
                    title:document.querySelector('.tHeader h1')?.innerText.trim()||'',
                    company_name:document.querySelector('.com_msg a.com_name')?.innerText.trim()||'',
                    salary_raw:document.querySelector('.tHeader strong')?.innerText.trim()||'',
                    requirements:document.querySelector('.tHeader .msg.ltype')?.innerText||'',
                    description:document.querySelector('.job-detail .job_msg')?.innerText||''};
            ''') or {}
            if fields.get('title') != job.title or fields.get('company_name') != item['company_name']:
                raise ValueError('岗位详情的标题或公司与列表不一致，已停止写入，请人工核对')
            if not fields.get('description', '').strip():
                raise ValueError('详情职责区域为空，请人工确认是否需要登录或页面结构已变化')
            requirements = [text.strip() for text in fields.get('requirements', '').splitlines() if text.strip()]
            detail_location = re.split(r'[·\-]', requirements[0], maxsplit=1) if requirements else []
            if not detail_location or detail_location[0] != job.city or (job.district and (len(detail_location) < 2 or detail_location[1] != job.district)):
                raise ValueError('岗位详情地点与列表不一致，已停止写入，请人工核对')
            education = next((text for text in requirements if text in ('博士', '硕士', '本科', '大专', '高中', '中专', '中技/中专', '学历不限', '初中及以下')), '')
            experience = next((text for text in requirements if re.fullmatch(r'\d+(?:[-~]\d+)?年(?:及以上|以下|以上)?|无需经验|经验不限|在校生/应届生|在校生|应届生', text)), '')
            gate()
            check_page()
            return Job.from_mapping(job.to_dict() | {
                'source_job_id': match.group(1), 'identity_hint': '',
                'source_url': urlunsplit((parts.scheme, parts.netloc, parts.path, '', '')),
                'salary_raw': fields.get('salary_raw') or job.salary_raw,
                'education_raw': education, 'experience_raw': experience,
                'description': fields['description'],
                'work_address': fields.get('work_address') or job.work_address,
            })
        finally:
            session.page = original
            # If cancellation occurs just after the click, identify only tabs
            # opened by this list and belonging to this source. User-created
            # unrelated tabs must not be closed as part of cleanup.
            if before:
                try:
                    candidates = opened | (set(original.browser.tab_ids) - before)
                    targets = original.run_cdp('Target.getTargets').get('targetInfos', [])
                    for target in targets:
                        if target['targetId'] in candidates and target['targetId'] not in before and own_target(target):
                            original.browser.get_tab(target['targetId']).close()
                except Exception:
                    # Keep the original task error if the browser was closed by
                    # the user. The workbench still owns browser-wide shutdown.
                    pass
