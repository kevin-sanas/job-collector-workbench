import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from html import unescape
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


def redact_text(value) -> str:
    text = unescape(str(value or '')).replace('\x00', '')
    text = re.sub(r'(?i)[\w.+-]+@[\w.-]+\.[a-z]{2,}', '[邮箱已隐藏]', text)
    text = re.sub(r'(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)', '[电话已隐藏]', text)
    text = re.sub(r'(?<!\d)0\d{2,3}[- ]\d{7,8}(?!\d)', '[电话已隐藏]', text)
    text = re.sub(r'(?i)(微信|微 ?信|wechat|手机号|联系电话)\s*[:：=]\s*[\w+@.-]+', r'\1：[已隐藏]', text)
    return text.strip()



def sanitize_diagnostic_text(value, limit=1000) -> str:
    """Keep useful failure details while removing credentials and contact data."""
    text = unescape(str(value or '')).replace('\x00', '')
    text = re.sub(r'''(?im)["']?\b(?:cookie|set-cookie|authorization|proxy-authorization)["']?\s*[:=]\s*[^\r\n]+''', '[凭据已隐藏]', text)
    text = re.sub(r'(?i)\bbearer\s+[A-Za-z0-9._~+/-]+=*', 'Bearer [已隐藏]', text)
    text = re.sub(r'''(?ix)(["']?\b(?:[\w-]*token|[\w-]*secret|password|passwd|pwd|api[_-]?key|session(?:id)?|credential|access[_-]?key|signature)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;}&]+)''', r'\1[已隐藏]', text)
    def safe_url(match):
        try:
            parts = urlsplit(match.group(0))
            host = parts.hostname or ''
            return urlunsplit((parts.scheme, host, parts.path, '', ''))
        except ValueError:
            return '[链接已隐藏]'
    text = re.sub(r'https?://[^\s<>"\']+', safe_url, text)
    text = redact_text(text)
    return ' '.join(text.split())[:limit]



def diagnostic_error(exception, stage) -> dict:
    message = sanitize_diagnostic_text(exception) or '异常未提供详细说明'
    cause = exception.__cause__
    if cause is not None:
        message += '；原因 ' + type(cause).__name__ + ': ' + (sanitize_diagnostic_text(cause) or '异常未提供详细说明')
    return {'type': type(exception).__name__, 'message': message, 'stage': stage}


def canonical_url(value: str) -> str:
    try:
        parts = urlsplit(str(value).strip())
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
            return ''
        tracking = {'spm', 'from', 'source', 'tracking_id', 'trackid'}
        sensitive = {
            'token', 'accesstoken', 'authtoken', 'idtoken', 'refreshtoken',
            'apikey', 'authorization', 'password', 'passwd', 'secret',
            'session', 'sessionid', 'signature',
        }
        query = []
        for key, item in parse_qsl(parts.query, keep_blank_values=True):
            lowered = key.lower()
            normalized = re.sub(r'[-_.]', '', lowered)
            if lowered.startswith('utm_') or lowered in tracking or normalized in sensitive or normalized.endswith('token'):
                continue
            query.append((key, item))
        return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or '/', urlencode(sorted(query)), ''))
    except ValueError:
        return ''


def parse_salary(value: str) -> tuple:
    text = str(value or '').replace(',', '').replace('，', '').replace(' ', '')
    if not text or any(word in text for word in ('面议', '保密', '不限')):
        return None, None, '', None
    months = re.search(r'(\d{1,2})薪', text)
    period = '年' if re.search(r'[/／]年|年薪', text) else '天' if re.search(r'[/／]天|[/／]日|日薪', text) else '小时' if re.search(r'[/／]小时|时薪', text) else '月'
    match = re.search(r'(\d+(?:\.\d+)?)\s*([kK千万]?)\s*(?:[-~～—至]\s*(\d+(?:\.\d+)?)\s*([kK千万]?))?', text)
    if not match:
        return None, None, '', None
    low, unit1, high, unit2 = match.groups()
    scale = {'k': 1000, 'K': 1000, '千': 1000, '万': 10000, '': 1}
    minimum = float(low) * scale[unit1 or unit2 or '']
    maximum = float(high or low) * scale[unit2 or unit1 or '']
    if minimum > maximum:
        return None, None, '', None
    return minimum, maximum, period, int(months.group(1)) if months else None


@dataclass
class Filters:
    keyword: str = ''
    source: str = ''
    city: str = ''
    education: str = ''
    experience: str = ''
    salary_min: float | None = None
    salary_max: float | None = None
    include_unknown: bool = True
    since: str = ''
    province_code: str = ''
    city_code: str = ''


@dataclass
class Job:
    title: str
    company_name: str
    source: str = 'local_import'
    source_job_id: str = ''
    city: str = ''
    district: str = ''
    salary_raw: str = ''
    salary_min: float | None = None
    salary_max: float | None = None
    salary_period: str = ''
    salary_months: int | None = None
    currency: str = 'CNY'
    experience_raw: str = ''
    education_raw: str = ''
    employment_type: str = ''
    description: str = ''
    published_at: str = ''
    published_raw: str = ''
    source_url: str = ''
    first_seen_at: str = field(default_factory=now)
    last_seen_at: str = field(default_factory=now)
    task_id: str = ''
    import_batch_id: str = ''
    identity_hint: str = ''
    work_address: str = ''

    @classmethod
    def from_mapping(cls, values: dict):
        allowed = {f.name for f in fields(cls)}
        data = {key: value for key, value in values.items() if key in allowed and value is not None}
        structural = {'source', 'source_job_id', 'source_url', 'first_seen_at', 'last_seen_at', 'task_id', 'import_batch_id', 'identity_hint'}
        for key in allowed - {'salary_min', 'salary_max', 'salary_months'}:
            if key in data:
                data[key] = str(data[key]).strip() if key in structural else redact_text(data[key])
        if not data.get('title') or not data.get('company_name'):
            raise ValueError('岗位名称和公司名称不能为空')
        data['source'] = data.get('source') or 'local_import'
        data['source_url'] = canonical_url(data.get('source_url', ''))
        if data.get('salary_raw'):
            lo, hi, period, months = parse_salary(data['salary_raw'])
            data.update(salary_min=lo, salary_max=hi, salary_period=period, salary_months=months)
        else:
            for key in ('salary_min', 'salary_max'):
                if key in data:
                    try:
                        data[key] = float(data[key]) if data[key] != '' else None
                    except (ValueError, TypeError):
                        raise ValueError(f'{key} 必须为数值') from None
        return cls(**data)

    @property
    def record_key(self) -> str:
        identity = ('id', self.source_job_id) if self.source_job_id else ('hint', self.identity_hint) if self.identity_hint else ('url', self.source_url) if self.source_url else (
            'fallback', self.title, self.company_name, self.city, self.description)
        raw = json.dumps((self.source, *identity), ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    @property
    def content_hash(self) -> str:
        data = {k: v for k, v in asdict(self).items() if k not in ('first_seen_at', 'last_seen_at', 'task_id', 'import_batch_id')}
        if not self.work_address:
            data.pop('work_address', None)
        return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def to_dict(self) -> dict:
        return asdict(self)

