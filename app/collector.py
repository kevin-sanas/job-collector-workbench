import threading
import time
import uuid
import logging
from dataclasses import dataclass, replace

from app.adapters import adapter_for, detect_state, source_url_allowed, CitySelectionError
from app.models import now, sanitize_diagnostic_text, diagnostic_error, canonical_url


class Cancelled(Exception):
    pass


class TaskControl:
    def __init__(self):
        self.cancel_event = threading.Event()
        self.resume_event = threading.Event()
        self.pause_event = threading.Event()
        self.wait_revision = 0
        self.resume_revision = 0

    def check(self):
        if self.cancel_event.is_set():
            raise Cancelled()

    def cancel(self):
        self.cancel_event.set()
        self.resume_event.set()

    def resume(self):
        self.pause_event.clear()
        self.resume_event.set()

    def pause(self):
        self.pause_event.set()

    def wait_for_user(self, timeout=None):
        self.check()
        self.wait_revision += 1
        resumed = self.resume_event.wait(timeout)
        self.resume_event.clear()
        self.check()
        if not resumed:
            raise ValueError('已达到任务总时长上限，人工等待结束；已完成岗位已保留')
        self.resume_revision += 1

    def delay(self, seconds):
        if self.cancel_event.wait(seconds):
            raise Cancelled()


@dataclass
class Criteria:
    keyword: str = 'Python'
    city: str = '全国'
    limit: int = 20
    current_page: bool = False
    details: bool = True


class Collector:
    def __init__(self, session, repository, settings, notify, adapter_factory=adapter_for, accept_job=None):
        self.session, self.repository, self.settings, self.notify = session, repository, settings, notify
        self.adapter_factory = adapter_factory
        self.accept_job = accept_job

    def run(self, source, criteria: Criteria, control: TaskControl):
        adapter = self.adapter_factory(source)
        task = {'id': uuid.uuid4().hex, 'source': source['name'], 'keyword': criteria.keyword, 'city': criteria.city,
                'status': 'running', 'count': 0, 'inserted': 0, 'updated': 0, 'page': 1, 'started_at': now(), 'message': ''}
        seen = set()
        deadline = time.monotonic() + self.settings.task_timeout_minutes * 60
        interval = max(self.settings.interval_seconds, source.get('min_interval_seconds', 0))
        reading_detail = False
        active_job = None

        def persist(state, message, item=None, item_error=None):
            task.update(status=state, message=sanitize_diagnostic_text(message))
            if state != 'waiting_manual':
                task.pop('waitingReason', None)
            self.repository.save_task(task)
            self.notify({**task, **({'item': item} if item is not None else {}),
                         **({'itemError': item_error} if item_error is not None else {})})

        def step(stage, message, state='RUNNING', error=None):
            task['stage'] = stage
            self.notify({**task, 'message': sanitize_diagnostic_text(message),
                         'step': {'stage': stage, 'status': state, **({'error': error} if error else {})}})

        def gate():
            control.check()
            if time.monotonic() > deadline:
                raise ValueError('已达到任务时长上限，已完成岗位已保留')
            if control.pause_event.is_set():
                persist('paused', '已暂停，点击继续后恢复；等待期间不会翻页或访问详情')
                control.wait_for_user(timeout=max(0, deadline - time.monotonic()))
            control.check()
            if time.monotonic() > deadline:
                raise ValueError('已达到任务时长上限，已完成岗位已保留')

        def check_page():
            while True:
                gate()
                if hasattr(self.session, 'ensure_page'):
                    self.session.ensure_page(source, allow_replacement=not reading_detail)
                if not source_url_allowed(self.session.page.url, source['domains']):
                    persist('waiting_user', '当前页面已跳出来源范围，请人工返回岗位列表或详情后继续')
                    control.wait_for_user(timeout=max(0, deadline - time.monotonic()))
                    continue
                state = detect_state(adapter.snapshot(self.session.page, self.session.status))
                if state == 'blocked':
                    raise ValueError('网站提示访问限制或返回 403/429；本次任务停止，请按网站提示稍后处理')
                if state == 'ready':
                    return
                messages = {'waiting_login': '请在专用浏览器自行登录或注册，完成后点击继续',
                            'waiting_verification': '请在专用浏览器自行完成滑块或验证码，完成后点击继续',
                            'waiting_user': '发现需要人工处理的弹窗，请在浏览器处理后点击继续'}
                persist(state, messages[state])
                control.wait_for_user(timeout=max(0, deadline - time.monotonic()))
                persist('running', '检查人工操作后的页面状态')

        def navigate(url):
            gate()
            control.delay(interval)
            gate()
            if hasattr(self.session, 'ensure_page'):
                self.session.navigate(url, source, allow_replacement=not reading_detail)
            else:
                self.session.navigate(url, source)
            check_page()

        try:
            step('browser_open', '准备专用直连浏览器')
            persist('running', '准备专用直连浏览器')
            control.check()
            self.session.open(source)
            check_page()
            step('browser_open', '专用浏览器已就绪', 'SUCCEEDED')
            if not criteria.current_page:
                step('search', '按关键词和城市准备来源列表')
                try:
                    if source.get('search_mode') == 'controls':
                        entry = source['search_entry']
                        if canonical_url(self.session.page.url) != canonical_url(entry):
                            navigate(entry)
                        adapter.search(self.session, criteria.keyword, criteria.city, gate, check_page, control.delay, interval)
                    else:
                        search_url = adapter.search_url(criteria.keyword, criteria.city)
                        if canonical_url(self.session.page.url) != canonical_url(search_url):
                            navigate(search_url)
                except ValueError as exc:
                    if not isinstance(exc, CitySelectionError) and not any(word in str(exc) for word in ('城市', '地区', '选城')):
                        raise
                    error = diagnostic_error(exc, 'city_select')
                    task['error'] = error
                    while True:
                        task['waitingReason'] = 'CITY_SELECTION'
                        step('city_select', error['message'], 'WAITING', error)
                        persist('waiting_manual', error['message'] + '；请在专用浏览器手动选择目标城市并搜索，完成后点击继续')
                        control.wait_for_user(timeout=max(0, deadline - time.monotonic()))
                        check_page()
                        validate_search = getattr(adapter, 'validate_search_selection', None)
                        validate_city = getattr(adapter, 'validate_city_selection', None)
                        if (validate_search(self.session.page, criteria.keyword, criteria.city) is True if callable(validate_search)
                                else validate_city(self.session.page, criteria.city) is True if callable(validate_city) else False):
                            break
                        error = {'type': 'CitySelectionError', 'message': '人工选择后的目标城市、关键词或岗位列表仍未确认', 'stage': 'city_select'}
                        task['error'] = error
                    step('city_select', '人工城市和列表已确认', 'SUCCEEDED')
                    persist('running', '人工城市和列表已确认，继续读取原任务')
                step('search', '来源搜索已完成', 'SUCCEEDED')
            for page_number in range(1, self.settings.page_limit + 1):
                step('list_read', '读取岗位列表')
                task['page'] = page_number
                check_page()
                cards = []
                for attempt in range(9):
                    gate()
                    cards = adapter.read_cards(self.session.page)
                    if cards:
                        break
                    text = adapter.snapshot(self.session.page).get('text', '')
                    if any(marker in text for marker in ('暂无相关职位', '没有找到相关职位', '暂无符合条件的职位')):
                        persist('completed', f'网站显示没有更多匹配岗位，共处理 {task["count"]} 条')
                        return task
                    if attempt < 8:
                        control.delay(.75)
                        check_page()
                if not cards and hasattr(adapter, 'empty_message'):
                    gate()
                    check_page()
                    message = adapter.empty_message(self.session.page)
                    if message:
                        persist('completed', f'{message}；共处理 {task["count"]} 条')
                        return task
                while not cards:
                    persist('waiting_sample', '没有匹配到岗位结构；请确认已进入列表。可人工返回列表后继续，或停止任务后预览诊断样本供适配')
                    control.wait_for_user(timeout=max(0, deadline - time.monotonic()))
                    check_page()
                    cards = adapter.read_cards(self.session.page)
                step('list_read', f'岗位列表已读取，共 {len(cards)} 条候选', 'SUCCEEDED')
                list_url = self.session.page.url
                for job in cards:
                    active_job = job
                    gate()
                    if job.record_key in seen:
                        if self.accept_job is not None:
                            self.accept_job(job)
                        active_job = None
                        continue
                    if source.get('requires_details'):
                        step('detail_read', '读取岗位详情')
                        reading_detail = True
                        try:
                            job = adapter.read_job_detail(self.session, job, gate, check_page, control.delay, interval, control)
                        finally:
                            reading_detail = False
                        if job.record_key in seen:
                            if self.accept_job is not None:
                                self.accept_job(job)
                            active_job = None
                            continue
                    elif (criteria.details or source.get('detail_fields_required')) and job.source_url and source.get('detail_selector'):
                        step('detail_read', '读取岗位详情')
                        original = self.session.page
                        detail_page = None
                        reading_detail = True
                        try:
                            gate()
                            detail_page = self.session.new_detail_tab()
                            self.session.page = detail_page
                            navigate(job.source_url)
                            gate()
                            if source.get('detail_title_selector'):
                                for attempt in range(13):
                                    check_page()
                                    ready = self.session.page.run_js('''return !!document.querySelector(arguments[0])?.innerText.trim()
                                        && !!document.querySelector(arguments[1])?.innerText.trim();''',
                                        source['detail_title_selector'],source['detail_selector'])
                                    if ready:
                                        break
                                    if attempt < 12:
                                        control.delay(.5)
                            job = adapter.read_detail(self.session.page, job)
                        finally:
                            reading_detail = False
                            self.session.page = original
                            if detail_page is not None and detail_page.states.is_alive:
                                detail_page.close()
                    if task.get('stage') == 'detail_read':
                        step('detail_read', '岗位详情已读取', 'SUCCEEDED')
                    job = replace(job, task_id=task['id'])
                    if self.accept_job is not None and not self.accept_job(job):
                        seen.add(job.record_key)
                        active_job = None
                        continue
                    step('save_local', '保存已读取岗位到本地')
                    inserted = self.repository.upsert(job, task['id'])
                    step('save_local', '岗位已保存到本地', 'SUCCEEDED')
                    seen.add(job.record_key)
                    task['count'] += 1
                    task['inserted' if inserted else 'updated'] += 1
                    task['last_key'] = job.record_key
                    persist('running', f'已处理 {task["count"]} / {criteria.limit} 条（新增 {task["inserted"]}，更新 {task["updated"]}）', item={'recordKey': job.record_key, 'title': job.title, 'company': job.company_name, 'inserted': inserted})
                    active_job = None
                    if task['count'] >= criteria.limit:
                        break
                if task['count'] >= criteria.limit or criteria.current_page:
                    break
                # Experimental pagination is intentionally opt-in only when an
                # explicit next-page link exists, never click arbitrary anchors.
                gate()
                selector = source.get('next_selector', '')
                next_url = self.session.page.run_js('return document.querySelector(arguments[0])?.href || "";', selector) if selector else ''
                if not next_url or next_url == list_url:
                    task['message'] = '当前列表结束或尚未验证翻页规则，已保留本页结果'
                    break
                step('pagination', '读取下一页岗位列表')
                navigate(next_url)
                step('pagination', '下一页已打开', 'SUCCEEDED')
            persist('completed', task['message'] if task['message'].startswith('当前列表') else f'任务结束，共处理 {task["count"]} 条；可在岗位数据中查看')
        except Cancelled:
            persist('cancelled', '任务已取消，已经入库的岗位已保留')
        except Exception as exc:
            error = diagnostic_error(exc, task.get('stage', 'browser_open'))
            task['error'] = error
            message = f"{error['type']}: {error['message']}"
            logging.getLogger(__name__).error('采集异常 stage=%s %s', error['stage'], message)
            step(error['stage'], message, 'FAILED', error)
            persist('failed', message, item_error={
                'recordKey': active_job.record_key, 'title': active_job.title, 'company': active_job.company_name
            } if self.accept_job is not None and active_job is not None else None)
        return task
