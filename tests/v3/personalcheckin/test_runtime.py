"""执行层测试：重试、直连/代理切换、缺 Cookie、CookieCloud 优先级。"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bootstrap import fresh_plugin, load_plugin, no_network  # noqa: E402

MODULE = load_plugin()
BASE = sys.modules["personalcheckin.sites.base"]
RUNTIME = sys.modules["personalcheckin.runtime"]


class DummySite(BASE.SiteAdapter):
    """可编排结果的假站点。"""

    id = "dummy"
    name = "Dummy"
    domain = "dummy.com"

    def __init__(self):
        """记录每次调用看到的上下文。"""
        self.calls = []
        self.script = []

    def run(self, ctx):
        """按脚本依次返回结果。"""
        self.calls.append({"direct": ctx.direct, "proxy": ctx.proxy})
        return self.script.pop(0) if self.script else BASE.SiteResult(BASE.SUCCESS, "ok")


class RunRetryTest(unittest.TestCase):
    """单站重试与直连/代理切换。"""

    def _plugin(self, config=None):
        base = {"enabled": True, "cookiecloud": False, "notify": False, "dummy_cookie": "a=1"}
        base.update(config or {})
        plugin = fresh_plugin(base)
        site = DummySite()
        plugin._sites = [site]
        plugin._proxy = "http://proxy:7897"
        plugin._config["dummy_enabled"] = True
        plugin._config.setdefault("dummy_direct", True)
        return plugin, site

    def test_retry_twice_then_success(self):
        plugin, site = self._plugin()
        site.script = [
            BASE.SiteResult(BASE.ERROR, "超时"),
            BASE.SiteResult(BASE.ERROR, "超时"),
            BASE.SiteResult(BASE.SUCCESS, "签到成功", reward="+5"),
        ]
        with patch.object(MODULE.time, "sleep"), no_network():
            summary = plugin.run_all(trigger="manual")
        row = summary["results"][0]
        self.assertEqual(row["status"], BASE.SUCCESS)
        self.assertEqual(row["attempts"], 3)
        self.assertEqual(len(site.calls), 3)

    def test_stops_after_three_attempts(self):
        plugin, site = self._plugin()
        site.script = [BASE.SiteResult(BASE.ERROR, "超时")] * 3
        with patch.object(MODULE.time, "sleep"), no_network():
            summary = plugin.run_all(trigger="manual")
        row = summary["results"][0]
        self.assertEqual(row["status"], BASE.ERROR)
        self.assertEqual(row["attempts"], 3)
        self.assertEqual(len(site.calls), 3)

    def test_blocked_is_not_retried(self):
        plugin, site = self._plugin()
        site.script = [BASE.SiteResult(BASE.BLOCKED, "CF 拦截")]
        with patch.object(MODULE.time, "sleep"), no_network():
            summary = plugin.run_all(trigger="manual")
        self.assertEqual(summary["results"][0]["attempts"], 1)
        self.assertEqual(len(site.calls), 1)

    def test_already_is_not_retried(self):
        plugin, site = self._plugin()
        site.script = [BASE.SiteResult(BASE.ALREADY, "今天已签到")]
        with patch.object(MODULE.time, "sleep"), no_network():
            summary = plugin.run_all(trigger="manual")
        self.assertEqual(summary["results"][0]["status"], BASE.ALREADY)
        self.assertEqual(len(site.calls), 1)

    def test_second_attempt_switches_to_proxy(self):
        plugin, site = self._plugin({"dummy_direct": True})
        site.script = [
            BASE.SiteResult(BASE.ERROR, "直连超时"),
            BASE.SiteResult(BASE.SUCCESS, "走代理成功"),
        ]
        with patch.object(MODULE.time, "sleep"), no_network():
            plugin.run_all(trigger="manual")
        self.assertTrue(site.calls[0]["direct"])
        self.assertFalse(site.calls[1]["direct"])

    def test_direct_site_uses_proxy_when_no_proxy_configured(self):
        plugin, site = self._plugin({"dummy_direct": True})
        plugin._proxy = ""
        site.script = [BASE.SiteResult(BASE.ERROR, "x"), BASE.SiteResult(BASE.SUCCESS, "ok")]
        with patch.object(MODULE.time, "sleep"), no_network():
            plugin.run_all(trigger="manual")
        self.assertTrue(all(call["direct"] for call in site.calls))

    def test_missing_cookie_skips_without_request(self):
        plugin, site = self._plugin({"dummy_cookie": ""})
        with no_network():
            summary = plugin.run_all(trigger="manual")
        self.assertEqual(summary["results"][0]["status"], BASE.SKIPPED)
        self.assertEqual(site.calls, [])

    def test_cookiecloud_cookie_used(self):
        plugin, site = self._plugin({"dummy_cookie": "", "cookiecloud": True, "dummy_direct": False})
        with patch.object(MODULE, "cookiecloud_cookies",
                          return_value={"dummy.com": "session=fromcloud"}), no_network():
            plugin.run_all(trigger="manual")
        self.assertEqual(site.calls, [{"direct": False, "proxy": "http://proxy:7897"}])

    def test_history_record_shape(self):
        plugin, site = self._plugin()
        site.script = [BASE.SiteResult(BASE.SUCCESS, "签到成功", reward="+5", balance="100 鸡腿")]
        with patch.object(MODULE.time, "sleep"), no_network():
            plugin.run_all(trigger="manual")
        history = plugin._history()
        self.assertEqual(len(history), 1)
        row = history[-1]
        for key in ("run_id", "ts", "site", "name", "status", "message", "reward",
                    "balance", "extra", "attempts", "trigger"):
            self.assertIn(key, row)
        self.assertEqual(row["trigger"], "manual")

    def test_scheduled_marks_done(self):
        plugin, site = self._plugin()
        with patch.object(MODULE.time, "sleep"), no_network():
            plugin.run_all(trigger="scheduled")
        self.assertTrue(plugin.get_data("done_day"))


class RequestLayerTest(unittest.TestCase):
    """HTTP 层：代理选择与异常包装。"""

    def test_direct_uses_no_proxy(self):
        ctx = RUNTIME.SiteContext("s", "S", cookie="a=1", direct=True, proxy="http://p:1")
        captured = {}

        def fake_request(method, url, **kwargs):
            captured.update(kwargs)
            return type("R", (), {"status_code": 200, "text": "ok", "headers": {}, "url": url})()

        with patch.object(RUNTIME.requests, "request", side_effect=fake_request):
            resp = ctx.request("GET", "https://example.com")
        self.assertTrue(resp.ok)
        self.assertIsNone(captured["proxies"])
        self.assertEqual(captured["headers"]["Cookie"], "a=1")

    def test_proxy_is_used(self):
        ctx = RUNTIME.SiteContext("s", "S", cookie="", direct=False, proxy="http://p:1")
        captured = {}

        def fake_request(method, url, **kwargs):
            captured.update(kwargs)
            return type("R", (), {"status_code": 200, "text": "ok", "headers": {}, "url": url})()

        with patch.object(RUNTIME.requests, "request", side_effect=fake_request):
            ctx.request("GET", "https://example.com")
        self.assertEqual(captured["proxies"], {"http": "http://p:1", "https": "http://p:1"})

    def test_network_error_is_wrapped(self):
        ctx = RUNTIME.SiteContext("s", "S", direct=True)

        def boom(*args, **kwargs):
            raise ConnectionError("read timed out")

        with patch.object(RUNTIME.requests, "request", side_effect=boom):
            resp = ctx.request("GET", "https://example.com")
        self.assertFalse(resp.ok)
        self.assertIn("ConnectionError", resp.error)


if __name__ == "__main__":
    unittest.main()
