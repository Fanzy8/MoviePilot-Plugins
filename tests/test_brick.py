#!/usr/bin/env python3
"""自动搬砖功能的测试（先写测试，后写实现）。

用 stdlib unittest，不依赖 pytest。宿主模块（app.*）与 apscheduler 用桩替代，
只加载插件文件本身。

运行：python3 tests/test_brick.py
"""
import importlib.util
import json
import pathlib
import sys
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
PLUGIN_PATH = ROOT / "plugins.v2" / "siqicleanbeach" / "__init__.py"

# ---------------------------------------------------------------- 宿主桩
LOG = []


def _logger():
    log = types.SimpleNamespace(
        info=lambda *a, **k: LOG.append(("info", a)),
        warn=lambda *a, **k: LOG.append(("warn", a)),
        warning=lambda *a, **k: LOG.append(("warn", a)),
        error=lambda *a, **k: LOG.append(("error", a)),
        debug=lambda *a, **k: LOG.append(("debug", a)),
    )
    return log


class _PluginBase:
    def __init__(self):
        self._messages = []
        self._saved_configs = []

    def update_config(self, config):
        self._saved_configs.append(config)

    def post_message(self, **kwargs):
        self._messages.append(kwargs)


class _EventManager:
    def register(self, *args, **kwargs):
        def deco(func):
            return func
        return deco


def _install_stubs():
    mods = {
        "app": {},
        "app.core": {},
        "app.core.event": {"Event": object, "eventmanager": _EventManager()},
        "app.db": {},
        "app.db.site_oper": {"SiteOper": object},
        "app.log": {"logger": _logger()},
        "app.plugins": {"_PluginBase": _PluginBase},
        "app.schemas": {"NotificationType": types.SimpleNamespace(Plugin="Plugin")},
        "app.schemas.types": {"EventType": types.SimpleNamespace(PluginAction="plugin.action")},
    }
    for name, attrs in mods.items():
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        sys.modules.setdefault(name, mod)
    if "apscheduler" not in sys.modules:
        try:
            import apscheduler  # noqa: F401
        except ImportError:
            cron = types.ModuleType("apscheduler.triggers.cron")
            cron.CronTrigger = type("CronTrigger", (), {
                "from_crontab": staticmethod(lambda expr: ("cron", expr))})
            interval = types.ModuleType("apscheduler.triggers.interval")
            interval.IntervalTrigger = type("IntervalTrigger", (), {
                "__init__": lambda self, **kw: setattr(self, "kw", kw)})
            pkg = types.ModuleType("apscheduler")
            trig = types.ModuleType("apscheduler.triggers")
            pkg.triggers = trig
            for mod in (pkg, trig, cron, interval):
                sys.modules[mod.__name__] = mod


def _load_plugin():
    _install_stubs()
    spec = importlib.util.spec_from_file_location("siqicleanbeach_plugin", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["siqicleanbeach_plugin"] = module
    spec.loader.exec_module(module)
    return module


PLUGIN_MODULE = _load_plugin()

# 真实抓到的两种响应（来自站点，未改动）
RESP_OK = json.dumps({
    "success": True,
    "message": "获得1块砖！",
    "daily_bricks": 7,
    "bricks_moved": 1,
    "last_daily_reset": "2026-10-16",
    "server_now": 1792157973,
    "next_brick_reset_ts": 1792166400,
})
RESP_LIMIT = json.dumps({
    "success": False,
    "message": "今天搬砖已达上限（50块），请明天再来！",
    "daily_bricks": 50,
    "last_daily_reset": "2026-10-16",
    "server_now": 1792160131,
    "next_brick_reset_ts": 1792166400,
})

PAGE_HTML = """
<html><body>
<div class="stats">魔力: 2542967 已兑换魔力: 20000 当前魔丸数: 61 今日搬砖: 11/50</div>
<div class="workshop"><div class="drag-instruction">搬砖到口袋</div>
<span class="label">可搬：39块</span><span class="label">口袋：511块</span></div>
</body></html>
"""


def make_plugin(config=None):
    plugin = PLUGIN_MODULE.SiQiCleanBeach()
    base = {"enabled": True, "cookie": "c_secure_pass=deadbeef; uid=1"}
    base.update(config or {})
    plugin.init_plugin(base)
    return plugin


def names(plugin):
    return f"_SiQiCleanBeach__%s"


class ParseResponseTest(unittest.TestCase):
    def test_success_payload(self):
        got = getattr(make_plugin(), names(None) % "parse_brick_response")(RESP_OK)
        self.assertTrue(got["ok"])
        self.assertEqual(got["daily"], 7)
        self.assertEqual(got["moved"], 1)
        self.assertIn("获得1块砖", got["message"])

    def test_daily_limit_payload_is_not_ok_but_has_daily(self):
        got = getattr(make_plugin(), names(None) % "parse_brick_response")(RESP_LIMIT)
        self.assertFalse(got["ok"])
        self.assertEqual(got["daily"], 50)
        self.assertIn("上限", got["message"])

    def test_garbage_payload_is_not_ok_and_has_no_daily(self):
        parse = getattr(make_plugin(), names(None) % "parse_brick_response")
        for payload in ("<html>请先登录</html>", "{}", "", "null", "[1,2]"):
            got = parse(payload)
            self.assertFalse(got["ok"], payload)
            self.assertIsNone(got["daily"], payload)

    def test_bricks_moved_above_one_is_kept(self):
        parse = getattr(make_plugin(), names(None) % "parse_brick_response")
        got = parse(json.dumps({"success": True, "message": "获得3块砖！",
                                "daily_bricks": 9, "bricks_moved": 3}))
        self.assertEqual(got["moved"], 3)
        self.assertEqual(got["daily"], 9)


class ParsePageTest(unittest.TestCase):
    def test_reads_daily_and_available(self):
        got = getattr(make_plugin(), names(None) % "parse_brick_page")(PAGE_HTML)
        self.assertEqual(got["daily"], 11)
        self.assertEqual(got["available"], 39)

    def test_missing_numbers_are_none(self):
        got = getattr(make_plugin(), names(None) % "parse_brick_page")("<html>登录</html>")
        self.assertIsNone(got["daily"])
        self.assertIsNone(got["available"])

    def test_tolerates_half_width_colon_and_spaces(self):
        got = getattr(make_plugin(), names(None) % "parse_brick_page")(
            "<div>今日搬砖: 3 / 50 可搬: 47 块</div>")
        self.assertEqual(got["daily"], 3)
        self.assertEqual(got["available"], 47)


class BrickLoopTest(unittest.TestCase):
    def _patch(self, plugin, moves, page=PAGE_HTML):
        calls = []

        def fake_move(cookie):
            calls.append(cookie)
            if isinstance(moves[0], list):
                seq = moves[0]
            else:
                seq = moves
            index = min(len(calls) - 1, len(seq) - 1)
            return seq[index]

        def fake_page(cookie):
            return page

        setattr(plugin, names(None) % "move_brick", fake_move)
        setattr(plugin, names(None) % "fetch_brick_page", fake_page)
        return calls

    def test_stops_at_daily_limit_and_does_not_retry_refusal(self):
        plugin = make_plugin({"brick_enabled": True, "brick_interval_ms": 0})
        seq = [
            {"ok": True, "message": "获得1块砖！", "daily": 48, "moved": 1},
            {"ok": True, "message": "获得1块砖！", "daily": 49, "moved": 1},
            {"ok": True, "message": "获得1块砖！", "daily": 50, "moved": 1},
            {"ok": False, "message": "今天搬砖已达上限（50块），请明天再来！", "daily": 50, "moved": 0},
        ]
        calls = self._patch(plugin, [seq])
        with mock.patch("time.sleep"):
            result = plugin.run_brick_task()
        self.assertEqual(len(calls), 3, "达到上限后不应再发请求")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["data"]["moved"], 3)
        self.assertEqual(plugin._brick_stats["daily"], 50)

    def test_respects_configured_daily_limit(self):
        plugin = make_plugin({"brick_enabled": True, "brick_interval_ms": 0,
                              "brick_daily_limit": 2})
        seq = [{"ok": True, "message": "获得1块砖！", "daily": n, "moved": 1}
               for n in (1, 2, 3)]
        calls = self._patch(plugin, [seq], page="<div>今日搬砖: 0/50</div>")
        with mock.patch("time.sleep"):
            result = plugin.run_brick_task()
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["data"]["moved"], 2)

    def test_server_refusal_stops_without_retry(self):
        plugin = make_plugin({"brick_enabled": True, "brick_interval_ms": 0})
        seq = [
            {"ok": True, "message": "获得1块砖！", "daily": 1, "moved": 1},
            {"ok": False, "message": "工坊里没有砖块了", "daily": 1, "moved": 0},
        ]
        calls = self._patch(plugin, [seq])
        with mock.patch("time.sleep"):
            result = plugin.run_brick_task()
        self.assertEqual(len(calls), 2)
        self.assertIn("没有砖块", result["message"])
        self.assertEqual(result["data"]["moved"], 1)

    def test_unknown_result_stops_without_retry(self):
        plugin = make_plugin({"brick_enabled": True, "brick_interval_ms": 0})
        seq = [{"ok": True, "message": "获得1块砖！", "daily": 1, "moved": 1}, None]
        calls = self._patch(plugin, [seq])
        with mock.patch("time.sleep"):
            result = plugin.run_brick_task()
        self.assertEqual(len(calls), 2, "结果未知时不得重试")
        self.assertEqual(result["data"]["moved"], 1)
        self.assertIn("未知", result["message"])

    def test_already_at_limit_makes_no_move_call(self):
        plugin = make_plugin({"brick_enabled": True, "brick_interval_ms": 0})
        page = "<div>今日搬砖: 50/50 可搬：0块</div>"
        calls = self._patch(plugin, [], page=page)
        with mock.patch("time.sleep"):
            result = plugin.run_brick_task()
        self.assertEqual(calls, [])
        self.assertEqual(result["status"], "completed")

    def test_missing_cookie_makes_no_call(self):
        plugin = make_plugin({"brick_enabled": True, "cookie": ""})
        calls = self._patch(plugin, [])
        result = plugin.run_brick_task()
        self.assertEqual(calls, [])
        self.assertEqual(result["status"], "auth_failed")

    def test_waits_between_calls_with_configured_interval(self):
        plugin = make_plugin({"brick_enabled": True, "brick_interval_ms": 300,
                              "brick_daily_limit": 3})
        seq = [{"ok": True, "message": "获得1块砖！", "daily": n, "moved": 1}
               for n in (1, 2, 3)]
        calls = self._patch(plugin, [seq], page="<div>今日搬砖: 0/50</div>")
        with mock.patch("time.sleep") as sleeper:
            plugin.run_brick_task()
        self.assertEqual(len(calls), 3)
        self.assertEqual([c.args[0] for c in sleeper.call_args_list], [0.3, 0.3])

    def test_history_and_stats_are_recorded(self):
        plugin = make_plugin({"brick_enabled": True, "brick_interval_ms": 0})
        seq = [{"ok": True, "message": "获得1块砖！", "daily": 50, "moved": 1}]
        calls = self._patch(plugin, [seq])
        with mock.patch("time.sleep"):
            plugin.run_brick_task()
        history = plugin._stats["history"]
        self.assertTrue(any("搬砖" in str(row.get("result", "")) for row in history))
        self.assertEqual(plugin._brick_stats["available"], 39)
        self.assertTrue(plugin._messages, "应发送通知")


class ServiceTest(unittest.TestCase):
    def test_brick_service_only_when_brick_enabled(self):
        off = make_plugin({"enabled": True, "brick_enabled": False})
        on = make_plugin({"enabled": True, "brick_enabled": True})
        self.assertEqual([s["id"] for s in off.get_service()], ["SiQiCleanBeach"])
        self.assertEqual(sorted(s["id"] for s in on.get_service()),
                         ["SiQiBrick", "SiQiCleanBeach"])

    def test_brick_service_is_separate_from_beach(self):
        on = make_plugin({"enabled": True, "brick_enabled": True,
                          "schedule_mode": "interval", "interval_minutes": 121})
        ids = {s["id"]: s for s in on.get_service()}
        self.assertIn("SiQiBrick", ids)
        self.assertIsNot(ids["SiQiBrick"]["func"], ids["SiQiCleanBeach"]["func"])

    def test_disabled_plugin_registers_nothing(self):
        plugin = make_plugin({"enabled": False, "brick_enabled": True})
        self.assertEqual(plugin.get_service(), [])

    def test_form_exposes_new_config_keys(self):
        forms, defaults = make_plugin().get_form()
        blob = json.dumps(forms, ensure_ascii=False)
        for key in ("brick_enabled", "brick_daily_limit", "brick_interval_ms",
                    "brick_cron", "brick_run_once"):
            self.assertIn(key, blob, key)
            self.assertIn(key, defaults, key)

    def test_run_once_spawns_brick_task(self):
        plugin = make_plugin({"brick_enabled": True, "brick_run_once": True})
        with mock.patch.object(plugin, "run_brick_task") as runner, \
                mock.patch("threading.Thread") as thread:
            plugin.init_plugin({"enabled": True, "brick_enabled": True,
                                "brick_run_once": True,
                                "cookie": "c_secure_pass=deadbeef"})
            thread.assert_called()
            self.assertTrue(runner is not None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
