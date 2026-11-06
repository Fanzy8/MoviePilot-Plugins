"""插件契约测试：版本、表单、调度、历史、通知、API。"""
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bootstrap import fresh_plugin, load_package_index, load_plugin, no_network  # noqa: E402


def _walk(component):
    """递归遍历 Vuetify 配置里的所有组件字典。"""
    if isinstance(component, dict):
        yield component
        for value in component.values():
            yield from _walk(value)
    elif isinstance(component, list):
        for item in component:
            yield from _walk(item)


class PluginContractTest(unittest.TestCase):
    """插件元数据与索引一致性。"""

    def test_version_matches_package_index(self):
        module = load_plugin()
        index = load_package_index()
        entry = index.get("PersonalCheckin")
        self.assertIsNotNone(entry, "package.v3.json 缺少 PersonalCheckin 条目")
        self.assertEqual(entry["version"], module.PersonalCheckin.plugin_version)
        self.assertEqual(entry["name"], module.PersonalCheckin.plugin_name)
        self.assertEqual(entry["author"], module.PersonalCheckin.plugin_author)

    def test_metadata(self):
        module = load_plugin()
        plugin = module.PersonalCheckin
        self.assertEqual(plugin.plugin_name, "自用签到")
        self.assertEqual(plugin.plugin_author, "Fanzy8")
        self.assertEqual(plugin.plugin_config_prefix, "personalcheckin_")
        self.assertEqual(plugin.plugin_icon, "signin.png")

    def test_four_sites_registered(self):
        module = load_plugin()
        ids = [site.id for site in module.SITES]
        self.assertEqual(ids, ["nodeseek", "deepflood", "linuxsb", "yzyy"])
        self.assertTrue(module.SITE_MAP["yzyy"].direct_default)
        self.assertFalse(module.SITE_MAP["nodeseek"].direct_default)


class FormContractTest(unittest.TestCase):
    """表单：字段齐全、默认值对齐、不含静态 value。"""

    def test_form_fields_match_defaults(self):
        plugin = fresh_plugin({"enabled": True})
        form, defaults = plugin.get_form()
        models = set()
        for component in _walk(form):
            props = component.get("props") or {}
            if "model" in props:
                models.add(props["model"])
        self.assertEqual(models, set(defaults), "表单字段与默认值字典不一致")
        for site_id in ("nodeseek", "deepflood", "linuxsb", "yzyy"):
            self.assertIn(f"{site_id}_cookie", models)
            self.assertIn(f"{site_id}_enabled", models)
            self.assertIn(f"{site_id}_direct", models)

    def test_form_has_no_static_value(self):
        plugin = fresh_plugin({"enabled": True})
        form, _defaults = plugin.get_form()
        for component in _walk(form):
            props = component.get("props") or {}
            self.assertNotIn("value", props, f"表单字段不应写静态 value：{props}")

    def test_cookie_field_is_textarea(self):
        plugin = fresh_plugin({"enabled": True})
        form, _defaults = plugin.get_form()
        textareas = [c for c in _walk(form) if c.get("component") == "VTextarea"]
        self.assertEqual(len(textareas), 4)


class SchedulerContractTest(unittest.TestCase):
    """调度：停用零任务，启用三个任务，随机分钟持久化。"""

    def test_disabled_registers_nothing(self):
        plugin = fresh_plugin({"enabled": False})
        self.assertEqual(plugin.get_service(), [])

    def test_enabled_registers_jobs(self):
        plugin = fresh_plugin({"enabled": True})
        services = plugin.get_service()
        ids = [item["id"] for item in services]
        self.assertIn("PersonalCheckinTick", ids)
        self.assertIn("PersonalCheckinStartup", ids)
        tick = next(item for item in services if item["id"] == "PersonalCheckinTick")
        self.assertIn("hour='8'", str(tick["trigger"]))

    def test_target_minute_persists(self):
        plugin = fresh_plugin({"enabled": True})
        day = "2026-11-06"
        first = plugin._target_minute(day)
        self.assertGreaterEqual(first, 0)
        self.assertLessEqual(first, 59)
        self.assertEqual(first, plugin._target_minute(day))

    def test_tick_waits_for_target(self):
        module = load_plugin()
        plugin = fresh_plugin({"enabled": True})
        day = "2026-11-06"
        plugin.save_data("targets", {day: 45})
        with patch.object(module, "datetime", SimpleNamespace(
                now=lambda tz=None: datetime(2026, 11, 6, 8, 30, 0))), \
                patch.object(plugin, "run_all") as run_all:
            plugin._job_tick()
            run_all.assert_not_called()
        with patch.object(module, "datetime", SimpleNamespace(
                now=lambda tz=None: datetime(2026, 11, 6, 8, 46, 0))), \
                patch.object(plugin, "run_all") as run_all:
            plugin._job_tick()
            run_all.assert_called_once()

    def test_tick_skips_when_done(self):
        module = load_plugin()
        plugin = fresh_plugin({"enabled": True})
        plugin.save_data("done_day", "2026-11-06")
        with patch.object(module, "datetime", SimpleNamespace(
                now=lambda tz=None: datetime(2026, 11, 6, 8, 59, 0))), \
                patch.object(plugin, "run_all") as run_all:
            plugin._job_tick()
            run_all.assert_not_called()

    def test_startup_catchup_after_window(self):
        module = load_plugin()
        plugin = fresh_plugin({"enabled": True})
        with patch.object(module, "datetime", SimpleNamespace(
                now=lambda tz=None: datetime(2026, 11, 6, 10, 0, 0))), \
                patch.object(plugin, "run_all") as run_all:
            plugin._job_startup()
            run_all.assert_called_once_with(trigger="startup")

    def test_startup_skips_before_window(self):
        module = load_plugin()
        plugin = fresh_plugin({"enabled": True})
        with patch.object(module, "datetime", SimpleNamespace(
                now=lambda tz=None: datetime(2026, 11, 6, 7, 59, 0))), \
                patch.object(plugin, "run_all") as run_all:
            plugin._job_startup()
            run_all.assert_not_called()


class LegacyImportTest(unittest.TestCase):
    """旧 yzyy 历史一次性导入。"""

    def test_import_legacy_history(self):
        legacy = [
            {"date": "2026-11-01 09:00:04", "result": "成功", "info": "签到成功"},
            {"date": "2026-11-02 09:00:36", "result": "失败", "info": "签到请求失败"},
        ]
        plugin = fresh_plugin({"enabled": True}, legacy_history=legacy)
        history = plugin._history()
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["site"], "yzyy")
        self.assertEqual(history[0]["status"], "success")
        self.assertEqual(history[1]["status"], "fail")
        self.assertTrue(plugin.get_data("legacy_imported"))

    def test_import_runs_once(self):
        legacy = [{"date": "2026-11-01 09:00:04", "result": "成功", "info": "ok"}]
        plugin = fresh_plugin({"enabled": True}, legacy_history=legacy)
        plugin._import_legacy_history()
        self.assertEqual(len(plugin._history()), 1)


class CookieApiTest(unittest.TestCase):
    """Cookie 推送接口。"""

    def test_cookie_api_merges_and_reports_unknown(self):
        plugin = fresh_plugin({"enabled": True})
        result = plugin._api_cookies({"nodeseek": "a=1; b=2", "nosuch": "x=1"})
        self.assertTrue(result["success"])
        self.assertEqual(result["saved"], ["nodeseek"])
        self.assertEqual(result["unknown"], ["nosuch"])
        self.assertEqual(plugin._config["nodeseek_cookie"], "a=1; b=2")

    def test_cookie_api_rejects_empty(self):
        plugin = fresh_plugin({"enabled": True})
        result = plugin._api_cookies({})
        self.assertFalse(result["success"])

    def test_cookiecloud_preferred_over_manual(self):
        module = load_plugin()
        plugin = fresh_plugin({"enabled": True, "nodeseek_cookie": "session=old"})
        site = module.SITE_MAP["nodeseek"]
        cookie = plugin._cookie_for(site, {"nodeseek.com": "session=new; pjwt=x"})
        self.assertIn("session=new", cookie)
        cookie = plugin._cookie_for(site, {})
        self.assertIn("session=old", cookie)


class NotifyTest(unittest.TestCase):
    """通知：一条汇总，成功失败都发。"""

    def test_notify_sends_single_summary(self):
        plugin = fresh_plugin({"enabled": True, "notify": True})
        rows = [
            {"site": "nodeseek", "name": "NodeSeek", "status": "success", "message": "签到成功",
             "reward": "+5 鸡腿", "balance": "128 鸡腿", "extra": "", "attempts": 1},
            {"site": "linuxsb", "name": "linux.sb", "status": "blocked", "message": "CF 拦截",
             "reward": "", "balance": "", "extra": "", "attempts": 1},
        ]
        plugin._notify(rows, "abc123")
        self.assertEqual(len(plugin.messages), 1)
        message = plugin.messages[0]
        self.assertIn("1/2", message["title"])
        self.assertIn("NodeSeek", message["text"])
        self.assertIn("CF 拦截", message["text"])

    def test_notify_switch_off(self):
        plugin = fresh_plugin({"enabled": True, "notify": False})
        plugin._notify([], "abc")
        self.assertEqual(plugin.messages, [])

    def test_page_renders_without_history(self):
        plugin = fresh_plugin({"enabled": True})
        page = plugin.get_page()
        self.assertIsInstance(page, list)
        self.assertTrue(page)


if __name__ == "__main__":
    unittest.main()
