import json
from dataclasses import asdict, dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    browser_path: str = ''
    font_path: str = ''
    interval_seconds: float = 5.0
    page_limit: int = 5
    detail_limit: int = 20
    task_timeout_minutes: int = 20
    backend_url: str = 'http://127.0.0.1:8080'
    backend_token: str = ''
    backend_auto_jobs: bool = False
    backend_auto_discovery: bool = False


class AppPaths:
    def __init__(self, root: Path = ROOT):
        self.root = Path(root).resolve()
        self.data = self.root / 'data'
        self.exports = self.root / 'exports'
        self.logs = self.root / 'logs'
        self.config = self.root / 'config'
        for path in (self.data, self.exports, self.logs, self.config):
            path.mkdir(parents=True, exist_ok=True)
        self.database = self.data / 'jobs.sqlite3'
        self.discovery_database = self.data / 'discovery.sqlite3'
        self.discovery_config = self.config / 'discovery'
        self.discovery_exports = self.exports / 'discovery'
        self.discovery_config.mkdir(parents=True, exist_ok=True)
        self.discovery_exports.mkdir(parents=True, exist_ok=True)
        self.settings_file = self.config / 'settings.json'

    def load_settings(self) -> Settings:
        if not self.settings_file.exists():
            return Settings()
        try:
            data = json.loads(self.settings_file.read_text(encoding='utf-8'))
            settings = Settings(**{k: v for k, v in data.items() if k in Settings.__dataclass_fields__})
            settings.interval_seconds = max(3.0, float(settings.interval_seconds))
            settings.page_limit = max(1, min(20, int(settings.page_limit)))
            return settings
        except (ValueError, TypeError):
            raise ValueError('config/settings.json 无法读取，请检查 JSON 格式或在设置中重新保存') from None

    def save_settings(self, settings: Settings):
        temp = self.settings_file.with_suffix('.tmp')
        temp.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(self.settings_file)

    def sources(self) -> list[dict]:
        return json.loads((self.config / 'sources.json').read_text(encoding='utf-8'))

    def github_content_source(self) -> dict:
        values = json.loads((self.discovery_config / 'content_sources.json').read_text(encoding='utf-8'))
        if not isinstance(values, dict) or not isinstance(values.get('sources'), list):
            raise ValueError('发现内容来源配置格式无效')
        matches = [source for source in values['sources'] if isinstance(source, dict) and source.get('id') == 'github']
        if len(matches) != 1:
            raise ValueError('发现内容来源须登记一个 GitHub 配置')
        source = dict(matches[0])
        local_config = self.discovery_config / 'content_sources.local.json'
        if local_config.exists():
            override = json.loads(local_config.read_text(encoding='utf-8'))
            if not isinstance(override, dict) or set(override) - {'network_mode', 'proxy_url'}:
                raise ValueError('GitHub 本地网络配置格式无效')
            source.update(override)
        if (
            source.get('status') != 'trial'
            or source.get('kind') != 'repository'
            or source.get('base_url') != 'https://api.github.com'
            or source.get('allowed_domains') != ['api.github.com']
            or source.get('network_mode') not in {'system', 'direct', 'local_proxy'}
            or (
                source.get('network_mode') == 'local_proxy'
                and not isinstance(source.get('proxy_url'), str)
            )
            or type(source.get('max_items')) is not int
            or not 1 <= source['max_items'] <= 20
        ):
            raise ValueError('GitHub 内容来源配置无效')
        return source

    def gitee_content_source(self) -> dict:
        values = json.loads((self.discovery_config / 'content_sources.json').read_text(encoding='utf-8'))
        if not isinstance(values, dict) or not isinstance(values.get('sources'), list):
            raise ValueError('发现内容来源配置格式无效')
        matches = [source for source in values['sources'] if isinstance(source, dict) and source.get('id') == 'gitee']
        if len(matches) != 1:
            raise ValueError('发现内容来源须登记一个 Gitee 配置')
        source = matches[0]
        if (
            source.get('status') != 'trial'
            or source.get('kind') != 'repository'
            or source.get('base_url') != 'https://gitee.com/api/v5'
            or source.get('allowed_domains') != ['gitee.com']
            or source.get('network_mode') != 'direct'
            or source.get('proxy_url') not in ('', None)
            or type(source.get('max_items')) is not int
            or not 1 <= source['max_items'] <= 20
        ):
            raise ValueError('Gitee 内容来源配置无效，必须直连且单次不超过 20 个仓库')
        return source

    def tencent_market_source(self) -> dict:
        values = json.loads((self.discovery_config / 'content_sources.json').read_text(encoding='utf-8'))
        if not isinstance(values, dict) or not isinstance(values.get('sources'), list):
            raise ValueError('发现内容来源配置格式无效')
        matches = [source for source in values['sources'] if isinstance(source, dict) and source.get('id') == 'tencent_market']
        if len(matches) != 1:
            raise ValueError('发现内容来源须登记一个腾讯云案例配置')
        source = matches[0]
        if (
            source.get('status') != 'trial'
            or source.get('kind') != 'market_case'
            or source.get('base_url') != 'https://cloud.tencent.com'
            or source.get('allowed_domains') != ['cloud.tencent.com']
            or source.get('network_mode') != 'direct'
            or source.get('proxy_url') not in ('', None)
            or type(source.get('max_items')) is not int
            or not 1 <= source['max_items'] <= 5
        ):
            raise ValueError('腾讯云案例来源配置无效，必须直连且单次不超过 5 条')
        return source
