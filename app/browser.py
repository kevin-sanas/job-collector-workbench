import functools
import json
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

from app.adapters import source_url_allowed


def launch_arguments(browser: str, profile: Path, port: int, browser_mode='FOREGROUND') -> list[str]:
    return [browser, '--no-proxy-server', f'--user-data-dir={profile.resolve()}',
            f'--disk-cache-dir={profile.resolve() / "Cache"}', f'--remote-debugging-port={port}',
            '--remote-debugging-address=127.0.0.1', '--no-first-run', '--no-default-browser-check',
            *(['--start-minimized'] if browser_mode == 'BACKGROUND' else []), 'about:blank']


class BrowserSession:
    def __init__(self, paths, settings, runtime_name='browser-runtime.json', browser_mode='FOREGROUND'):
        self.paths, self.settings = paths, settings
        self.browser_mode = browser_mode
        self.page = None
        self.process = None
        self.source_id = ''
        self._page_status = {}
        self.source_domains = []
        self._ws_original = None
        self.runtime_file = self.paths.data / runtime_name

    def open(self, source: dict):
        if self.page is not None and self.source_id == source['id']:
            return self.ensure_page(source)
        self.close()
        browser = Path(self.settings.browser_path)
        if not browser.is_file() or browser.suffix.lower() != '.exe':
            raise ValueError('请在“设置”中选择本机 Chrome 或 Edge 的 .exe 程序；系统代理无需调整')
        profile = self.paths.data / 'browser_profiles' / source['id']
        profile.mkdir(parents=True, exist_ok=True)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        args = launch_arguments(str(browser), profile, port, self.browser_mode)
        self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            import requests
            import psutil
            from DrissionPage import ChromiumPage, ChromiumOptions
            import DrissionPage._base.driver as driver
            # Pin the library's loopback websocket to direct mode, without altering
            # Windows, Clash, or environment variables shared with other apps.
            self._ws_original = driver.create_connection
            original = self._ws_original
            def local_connection(url, **kwargs):
                if urlsplit(url).hostname in ('127.0.0.1', 'localhost', '::1'):
                    kwargs['http_no_proxy'] = ['127.0.0.1', 'localhost', '::1']
                return original(url, **kwargs)
            driver.create_connection = local_connection
            session = requests.Session()
            session.trust_env = False
            try:
                deadline = time.monotonic() + 15
                while True:
                    if self.process.poll() is not None:
                        raise ValueError('浏览器未能启动独立实例，可能专用目录正被占用；请关闭该工具创建的旧实例再试')
                    try:
                        response = session.get(f'http://127.0.0.1:{port}/json/version', timeout=0.5)
                        if response.ok and response.json().get('webSocketDebuggerUrl'):
                            break
                    except (requests.RequestException, ValueError):
                        pass
                    if time.monotonic() > deadline:
                        raise ValueError('等待独立浏览器超时，请确认浏览器路径和本机回环连接可用')
                    time.sleep(0.2)
            finally:
                session.close()
            actual = psutil.Process(self.process.pid).cmdline()
            if '--no-proxy-server' not in actual or f'--user-data-dir={profile.resolve()}' not in actual:
                raise ValueError('浏览器实际启动参数检查失败，已停止连接')
            options = ChromiumOptions(read_file=False).set_address(f'127.0.0.1:{port}').existing_only(True)
            options.set_timeouts(base=2, page_load=15, script=5).set_retry(0, 1)
            self.page = ChromiumPage(options)
            self.source_id = source['id']
            self.source_domains = source['domains']
            self.runtime_file.write_text(json.dumps({'pid': self.process.pid, 'port': port, 'source': source['id'], 'profile': str(profile.resolve())}), encoding='utf-8')
            self.monitor_page(self.page, source['domains'])
            self.navigate(source['home'], source)
            return self.page
        except Exception:
            self.close()
            raise

    @property
    def status(self):
        return self._page_status.get(self.page.tab_id, 0) if self.page else 0

    @status.setter
    def status(self, value):
        if self.page:
            self._page_status[self.page.tab_id] = value

    def ensure_page(self, source, allow_replacement=True):
        """Adopt a single replacement tab after a site's own login redirect."""
        if self.page is None:
            raise ValueError('请先打开专用浏览器')
        if self.page.states.is_alive:
            return self.page
        if not allow_replacement:
            raise ValueError('读取详情的标签页已被关闭或替换；本次任务停止，请回到列表后重新开始，已入库数据保留')
        if self.status in (403, 429):
            raise ValueError('网站返回访问限制，请停止本次任务并按网站提示处理')
        candidates = [tab for tab in self.page.browser.get_tabs()
                      if source_url_allowed(tab.url, source['domains'])]
        if len(candidates) != 1:
            raise ValueError('原标签页已关闭，无法确定继续使用的页面；请仅保留一个所选来源页面后重新开始')
        self.page = candidates[0]
        if self.page.tab_id not in self._page_status:
            self.monitor_page(self.page, source['domains'])
        navigation_status = self.page.run_js('return performance.getEntriesByType("navigation")[0]?.responseStatus || 0;')
        if self.status not in (403, 429) and navigation_status:
            self.status = int(navigation_status)
        return self.page

    def monitor_page(self, page, domains):
        identifier = page.tab_id
        self._page_status[identifier] = 0
        def response(**event):
            data = event.get('response', {})
            if event.get('type') in ('Document', 'XHR', 'Fetch') and source_url_allowed(data.get('url', ''), domains):
                status = int(data.get('status', 0))
                if self._page_status.get(identifier, 0) not in (403, 429) or status in (403, 429):
                    self._page_status[identifier] = status
        page.run_cdp('Network.enable')
        page._driver.set_callback('Network.responseReceived', response)

    def new_detail_tab(self):
        page = self.page.browser.new_tab()
        self.monitor_page(page, self.source_domains)
        return page

    def navigate(self, url: str, source: dict, allow_replacement=True):
        if not source_url_allowed(url, source['domains']):
            raise ValueError('目标链接不属于所选来源，已停止访问')
        self.ensure_page(source, allow_replacement=allow_replacement)
        self.status = 0
        self.page.get(url, timeout=15, retry=0)
        if not source_url_allowed(self.page.url, source['domains']):
            raise ValueError('页面跳转到来源范围以外，请在浏览器人工确认')

    def close(self):
        if self.page is not None:
            try:
                self.page.quit(timeout=3, force=False)
            except Exception:
                pass
            self.page = None
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                pass
        self.process = None
        self.source_id = ''
        if self.runtime_file.exists():
            self.runtime_file.unlink()
        if self._ws_original is not None:
            import DrissionPage._base.driver as driver
            driver.create_connection = self._ws_original
            self._ws_original = None
