import json
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import requests


@dataclass(frozen=True)
class HttpResult:
    data: object
    headers: dict[str, str]
    url: str
    status_code: int


class HttpStatusError(RuntimeError):
    def __init__(self, status_code: int, headers: dict[str, str]):
        self.status_code = status_code
        self.headers = headers
        super().__init__(f'来源接口返回 HTTP {status_code}')


class HttpClient:
    def __init__(
        self,
        base_url: str,
        allowed_domains: tuple[str, ...],
        network_mode: str = 'system',
        *,
        proxy_url: str = '',
        session: requests.Session | None = None,
        max_bytes: int = 2_000_000,
        timeout: tuple[int, int] = (5, 15),
    ):
        if network_mode not in {'system', 'direct', 'local_proxy'}:
            raise ValueError('网络模式必须为 system、direct 或 local_proxy')
        self.proxies = None
        if network_mode == 'local_proxy':
            if not isinstance(proxy_url, str):
                raise ValueError('本地代理地址格式无效')
            proxy_url = proxy_url.strip()
            try:
                proxy = urlsplit(proxy_url)
                port = proxy.port
            except ValueError:
                raise ValueError('本地代理地址格式无效') from None
            if (
                proxy.scheme != 'http'
                or proxy.hostname not in {'127.0.0.1', 'localhost', '::1'}
                or proxy.username is not None
                or proxy.password is not None
                or port is None
                or not 1 <= port <= 65535
                or proxy.path not in {'', '/'}
                or proxy.query
                or proxy.fragment
            ):
                raise ValueError('本地代理必须是无凭据的本机 HTTP 地址和有效端口')
            normalized_proxy = proxy_url.rstrip('/')
            self.proxies = {'http': normalized_proxy, 'https': normalized_proxy}
        if not allowed_domains or any(not domain or '/' in domain for domain in allowed_domains):
            raise ValueError('允许域名配置无效')
        if max_bytes < 1:
            raise ValueError('响应字节上限必须为正整数')
        self.allowed_domains = tuple(domain.casefold().strip('.') for domain in allowed_domains)
        self.base_url = self._validated_url(base_url)
        self.session = session or requests.Session()
        self.session.trust_env = network_mode == 'system'
        self.max_bytes = max_bytes
        self.timeout = timeout

    def _validated_url(self, url: str) -> str:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').casefold()
        if (
            parsed.scheme != 'https'
            or parsed.username is not None
            or parsed.password is not None
            or not any(host == domain or host.endswith('.' + domain) for domain in self.allowed_domains)
        ):
            raise ValueError('来源 URL 必须是允许域名下的 HTTPS 地址')
        return url

    def get_json(
        self,
        path_or_url: str,
        params: dict | None = None,
        *,
        accept: str = 'application/json',
    ) -> HttpResult:
        if accept not in {'application/json', 'application/vnd.github+json'}:
            raise ValueError('来源 JSON 媒体类型无效')
        url = self._validated_url(urljoin(self.base_url.rstrip('/') + '/', path_or_url))
        for redirect_count in range(4):
            response = self.session.get(
                url,
                params=params,
                headers={'Accept': accept, 'User-Agent': 'ZhaoPing-sprider-discovery/0.1'},
                auth=lambda prepared_request: prepared_request,
                proxies=self.proxies,
                timeout=self.timeout,
                stream=True,
                allow_redirects=False,
            )
            try:
                self._validated_url(response.url)
                headers = dict(response.headers)
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get('Location')
                    if not location or redirect_count == 3:
                        raise ValueError('来源重定向次数过多或缺少目标')
                    url = self._validated_url(urljoin(response.url, location))
                    params = None
                    continue
                if response.status_code >= 400:
                    raise HttpStatusError(response.status_code, headers)
                declared = response.headers.get('Content-Length')
                if declared and declared.isdecimal() and int(declared) > self.max_bytes:
                    raise ValueError('来源响应超过大小限制')
                chunks = []
                total = 0
                for chunk in response.iter_content(chunk_size=8192):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > self.max_bytes:
                        raise ValueError('来源响应超过大小限制')
                    chunks.append(chunk)
                try:
                    data = json.loads(b''.join(chunks))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise ValueError('来源响应不是有效 JSON') from None
                return HttpResult(data=data, headers=headers, url=response.url, status_code=response.status_code)
            finally:
                response.close()
        raise ValueError('来源重定向次数过多')

    def close(self):
        self.session.close()
