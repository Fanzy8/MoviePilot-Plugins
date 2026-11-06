"""测试引导：用最小宿主桩按路径加载 personalcheckin 插件包。

宿主契约来源（MoviePilot v3.2.3）：
- ``app.sdk.plugin._PluginBase``
- ``app.sdk.logging.logger``
- ``app.schemas.types.MessageType``
- ``app.sdk.config.settings``（``PLUGIN_DATA_PATH`` / ``PROXY_HOST``）
- ``app.sdk.browser.launch_browser_context``：测试里不启动，只在需要时打桩。
"""
import importlib.util
import json
import sys
import tempfile
import types
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_DIR = REPO_ROOT / "plugins.v3" / "personalcheckin"
PACKAGE_INDEX = REPO_ROOT / "package.v3.json"
PKG_NAME = "personalcheckin"

_LOADED = None


class StubPluginBase:
    """宿主 ``_PluginBase`` 的最小替身：数据目录、通知、配置存取。"""

    plugin_name = ""
    plugin_desc = ""
    plugin_icon = ""
    plugin_version = ""
    plugin_author = ""
    author_url = ""
    plugin_config_prefix = ""
    plugin_order = 9999
    auth_level = 1

    def __init__(self) -> None:
        """初始化通知记录与按 plugin_id 分区的数据容器。"""
        self.messages = []
        self._data = {}
        self._config = None

    def get_data_path(self, plugin_id=None) -> Path:
        """按宿主规则返回插件数据目录（测试用临时目录）。"""
        base = _settings().PLUGIN_DATA_PATH
        path = Path(base) / (plugin_id or self.__class__.__name__)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def post_message(self, channel=None, mtype=None, title=None, text=None,
                     image=None, link=None, userid=None, username=None, **kwargs) -> None:
        """记录通知，供测试断言。"""
        self.messages.append({"mtype": mtype, "title": title, "text": text, **kwargs})

    def save_data(self, key, value, plugin_id=None) -> None:
        """写入插件持久化数据。"""
        self._data.setdefault(plugin_id or self.__class__.__name__, {})[key] = value

    def get_data(self, key=None, plugin_id=None):
        """读取插件持久化数据。"""
        bucket = self._data.get(plugin_id or self.__class__.__name__, {})
        return bucket.get(key)

    def del_data(self, key, plugin_id=None):
        """删除插件持久化数据。"""
        bucket = self._data.setdefault(plugin_id or self.__class__.__name__, {})
        return bucket.pop(key, None)

    def update_config(self, config, plugin_id=None) -> bool:
        """保存配置（桩）。"""
        self._config = dict(config or {})
        return True

    def get_config(self, plugin_id=None):
        """读取配置（桩）。"""
        return self._config


class _StubMessageType(Enum):
    """宿主 ``MessageType`` 的取值（v3.2.3 无 Error 成员）。"""

    Download = "资源下载"
    Organize = "整理入库"
    Subscribe = "订阅"
    SiteMessage = "站点"
    MediaServer = "媒体服务器"
    Manual = "手动处理"
    Plugin = "插件"
    Agent = "智能体"
    Other = "其它"


class _StubLogger:
    """记录日志调用的桩 logger。"""

    def __init__(self) -> None:
        """初始化日志缓冲。"""
        self.records = []

    def _log(self, level):
        """返回一个记录器函数。"""
        def inner(msg, *args, **kwargs):
            """记录一条日志。"""
            self.records.append((level, str(msg)))
        return inner

    def __getattr__(self, name):
        """任意级别都可用。"""
        return self._log(name)


class _StubBrowserContext:
    """浏览器上下文桩：测试不启动真浏览器。"""

    def new_page(self):
        """返回一个页面桩。"""
        raise AssertionError("测试中不应启动浏览器")


def _settings():
    """返回测试用 settings 单例。"""
    return sys.modules["app.sdk.config"].settings


def _install_host_stubs() -> None:
    """把宿主 API 桩装进 sys.modules（幂等）。"""
    if "app.sdk.plugin" in sys.modules:
        return
    tmp_root = Path(tempfile.mkdtemp(prefix="personalcheckin-test-"))
    settings = types.SimpleNamespace(
        PLUGIN_DATA_PATH=tmp_root,
        TZ="Asia/Shanghai",
        PROXY_HOST="http://127.0.0.1:7897",
        MP_DOMAIN=lambda url=None: url,
    )

    app = types.ModuleType("app")
    app.__path__ = []
    sdk = types.ModuleType("app.sdk")
    sdk.__path__ = []
    plugin_mod = types.ModuleType("app.sdk.plugin")
    plugin_mod._PluginBase = StubPluginBase
    logging_mod = types.ModuleType("app.sdk.logging")
    logging_mod.logger = _StubLogger()
    config_mod = types.ModuleType("app.sdk.config")
    config_mod.settings = settings
    browser_mod = types.ModuleType("app.sdk.browser")
    browser_mod.launch_browser_context = lambda **kwargs: _StubBrowserContext()
    schemas = types.ModuleType("app.schemas")
    schemas.__path__ = []
    types_mod = types.ModuleType("app.schemas.types")
    types_mod.MessageType = _StubMessageType

    sys.modules.update({
        "app": app,
        "app.sdk": sdk,
        "app.sdk.plugin": plugin_mod,
        "app.sdk.logging": logging_mod,
        "app.sdk.config": config_mod,
        "app.sdk.browser": browser_mod,
        "app.schemas": schemas,
        "app.schemas.types": types_mod,
    })
    setattr(app, "sdk", sdk)
    setattr(app, "schemas", schemas)


def load_plugin():
    """按路径加载插件包并返回模块（进程内只加载一次）。"""
    global _LOADED
    _install_host_stubs()
    if _LOADED is not None:
        return _LOADED
    entry = PLUGIN_DIR / "__init__.py"
    if not entry.exists():
        raise ImportError(f"插件入口不存在：{entry}")
    spec = importlib.util.spec_from_file_location(
        PKG_NAME, entry, submodule_search_locations=[str(PLUGIN_DIR)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[PKG_NAME] = module
    spec.loader.exec_module(module)
    _LOADED = module
    return module


def load_package_index() -> dict:
    """读取仓库的 package.v3.json。"""
    if not PACKAGE_INDEX.exists():
        raise FileNotFoundError(f"缺少插件索引：{PACKAGE_INDEX}")
    return json.loads(PACKAGE_INDEX.read_text(encoding="utf-8"))


def fresh_plugin(config: dict | None = None, legacy_history: list | None = None):
    """建一个使用全新数据目录的插件实例并 init_plugin。"""
    _install_host_stubs()
    module = load_plugin()
    settings = _settings()
    settings.PLUGIN_DATA_PATH = Path(tempfile.mkdtemp(prefix="personalcheckin-data-"))
    plugin = module.PersonalCheckin()
    if legacy_history is not None:
        plugin._data["YzyySignin"] = {"history": legacy_history}
    plugin.init_plugin(config or {})
    return plugin


def json_text(value) -> str:
    """把对象序列化成 JSON 文本（测试夹具用）。"""
    return json.dumps(value, ensure_ascii=False)


@contextmanager
def no_network():
    """任何真实 HTTP 请求都直接失败。"""
    def boom(*args, **kwargs):
        """未桩请求的兜底。"""
        raise AssertionError(f"测试中出现真实网络请求：{args!r} {kwargs!r}")

    with patch("requests.Session.request", side_effect=boom):
        yield
