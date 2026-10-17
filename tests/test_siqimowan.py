#!/usr/bin/env python3
"""思齐搬砖捡破烂炼魔丸（siqimowan）测试。

先写测试、后写实现。stdlib unittest，不依赖 pytest；宿主模块（app.*）与
apscheduler 用桩替代，只按路径加载插件文件本身。

运行：python3 tests/test_siqimowan.py

所有站点协议细节（action 名、参数、返回字段、配方表）都来自用户已登录浏览器
里抓到的真实 mowan.php 页面源码；夹具里的**数字与用户名为假值**，结构照搬。
"""
import importlib.util
import json
import pathlib
import sys
import time
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins.v2" / "siqimowan"
PLUGIN_PATH = PLUGIN_DIR / "__init__.py"

# ---------------------------------------------------------------- 宿主桩
LOG = []


def _logger():
    return types.SimpleNamespace(
        info=lambda *a, **k: LOG.append(("info", a)),
        warn=lambda *a, **k: LOG.append(("warn", a)),
        warning=lambda *a, **k: LOG.append(("warn", a)),
        error=lambda *a, **k: LOG.append(("error", a)),
        debug=lambda *a, **k: LOG.append(("debug", a)),
    )


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


CRON_CALLS = []


def _install_apscheduler_stub():
    try:
        import apscheduler  # noqa: F401
        return
    except ImportError:
        pass

    def _from_crontab(expr):
        CRON_CALLS.append(expr)
        return ("cron", expr)

    cron = types.ModuleType("apscheduler.triggers.cron")
    cron.CronTrigger = type("CronTrigger", (), {"from_crontab": staticmethod(_from_crontab)})
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
    _install_apscheduler_stub()
    spec = importlib.util.spec_from_file_location("siqimowan_plugin", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["siqimowan_plugin"] = module
    spec.loader.exec_module(module)
    return module


PLUGIN_MODULE = _load_plugin()


def setUpModule():
    """任何真实网络请求都视为测试错误（本套测试只允许桩）。"""
    def _blocked(*args, **kwargs):
        raise AssertionError("测试中不得发起真实网络请求")

    for name in ("get", "post", "request"):
        patcher = mock.patch.object(PLUGIN_MODULE.requests, name, side_effect=_blocked)
        patcher.start()
        _MODULE_PATCHERS.append(patcher)


def tearDownModule():
    for patcher in _MODULE_PATCHERS:
        patcher.stop()


_MODULE_PATCHERS = []

# ---------------------------------------------------------------- 页面夹具
# 结构照搬真实页面：gameData 是 JS 对象（键未加引号、inventory 键是 \uXXXX 转义、
# serverTimeOffset 是表达式），recipes 是合法 JSON 数组。数字与用户名均为假值。
PAGE_HTML = """<!doctype html>
<html><body>
<div class="stats">
  <div class="stat-item">魔力: <span id="points">1000000</span></div>
  <div class="stat-item">已兑换魔力: <span id="bonusEarned">20000</span></div>
  <div class="stat-item">当前魔丸数: <span id="magicPills">5</span></div>
  <div class="stat-item">今日搬砖: <span id="dailyBricks">3</span>/50</div>
</div>
<div id="beachStatus">可以进入清理</div>
<div class="beach-tip">
  <span>魔丸出现的<strong>最大次数</strong>由您的<strong>发种等级</strong>（当前：Lv.1）决定，<br>
  <strong>每次出现的概率</strong>由您的<strong>HNR值</strong>（当前：4.5286）决定。</span>
</div>
<script>
    let gameData = {
        dailyBricks: 3,
        points: 1000000,
        lastBrickTime: 1792160130,
        lastDailyReset: "2026-10-16",
        serverTimeOffset: 1792165559 - Math.floor(Date.now() / 1000),
        nextBrickResetTs: 1792166400,
        lastBeachTime: 1792157224,
        currentTrashItems: [],
        brickInterval: 86400,
        beachInterval: 7200,
        isManage: 0,
        inventory: {"\\u7816\\u5757":200,"\\u6728\\u6750":40,"\\u5851\\u6599\\u888b":60,
            "\\u74f6\\u5b50":40,"\\u87ba\\u4e1d":40,"\\u65e7\\u7535\\u6c60":20,
            "\\u7834\\u94dc\\u7247":20,"\\u6728\\u5de5\\u4ef6":0,"\\u5851\\u6599\\u4ef6":0,
            "\\u7b80\\u6613\\u5de5\\u5177":0,"\\u80fd\\u91cf\\u788e\\u7247":0,
            "\\u9b54\\u4e38\\u80da\\u80ce":0,"\\u9b54\\u4e38":5,"\\u86af\\u8693":100}
    };

    function updateRecipes() {
        const recipes = [{"recipe_id":"1","result_item_id":"8","result_quantity":"1","result_name":"\\u6728\\u5de5\\u4ef6","result_icon":"\\ud83e\\ude9a","ingredients":[{"recipe_id":"1","item_id":"1","quantity":"5","name":"\\u7816\\u5757"},{"recipe_id":"1","item_id":"2","quantity":"1","name":"\\u6728\\u6750"},{"recipe_id":"1","item_id":"3","quantity":"1","name":"\\u5851\\u6599\\u888b"}]},{"recipe_id":"2","result_item_id":"9","result_quantity":"1","result_name":"\\u5851\\u6599\\u4ef6","result_icon":"\\ud83e\\udea3","ingredients":[{"recipe_id":"2","item_id":"1","quantity":"5","name":"\\u7816\\u5757"},{"recipe_id":"2","item_id":"3","quantity":"1","name":"\\u5851\\u6599\\u888b"},{"recipe_id":"2","item_id":"4","quantity":"1","name":"\\u74f6\\u5b50"}]},{"recipe_id":"3","result_item_id":"10","result_quantity":"1","result_name":"\\u7b80\\u6613\\u5de5\\u5177","result_icon":"\\ud83d\\udee0\\ufe0f","ingredients":[{"recipe_id":"3","item_id":"5","quantity":"2","name":"\\u87ba\\u4e1d"},{"recipe_id":"3","item_id":"8","quantity":"2","name":"\\u6728\\u5de5\\u4ef6"}]},{"recipe_id":"4","result_item_id":"11","result_quantity":"1","result_name":"\\u80fd\\u91cf\\u788e\\u7247","result_icon":"\\u26a1","ingredients":[{"recipe_id":"4","item_id":"6","quantity":"1","name":"\\u65e7\\u7535\\u6c60"},{"recipe_id":"4","item_id":"9","quantity":"2","name":"\\u5851\\u6599\\u4ef6"}]},{"recipe_id":"5","result_item_id":"12","result_quantity":"1","result_name":"\\u9b54\\u4e38\\u80da\\u80ce","result_icon":"\\ud83e\\udd5a","ingredients":[{"recipe_id":"5","item_id":"7","quantity":"1","name":"\\u7834\\u94dc\\u7247"},{"recipe_id":"5","item_id":"10","quantity":"1","name":"\\u7b80\\u6613\\u5de5\\u5177"},{"recipe_id":"5","item_id":"11","quantity":"1","name":"\\u80fd\\u91cf\\u788e\\u7247"}]},{"recipe_id":"6","result_item_id":"13","result_quantity":"1","result_name":"\\u9b54\\u4e38","result_icon":"\\u2697\\ufe0f","ingredients":[{"recipe_id":"6","item_id":"1","quantity":"10","name":"\\u7816\\u5757"},{"recipe_id":"6","item_id":"12","quantity":"2","name":"\\u9b54\\u4e38\\u80da\\u80ce"}]}];
        const hardMax = 100;
        recipes.forEach(recipe => { recipe.result_name; });
    }
</script>
</body></html>
"""

RESP_MOVE_OK = json.dumps({
    "success": True, "message": "获得1块砖！", "daily_bricks": 4, "bricks_moved": 1,
    "last_daily_reset": "2026-10-16", "server_now": 1792157973,
    "next_brick_reset_ts": 1792166400})
RESP_MOVE_LIMIT = json.dumps({
    "success": False, "message": "今天搬砖已达上限（50块），请明天再来！",
    "daily_bricks": 50, "last_daily_reset": "2026-10-16", "server_now": 1792160131,
    "next_brick_reset_ts": 1792166400})
RESP_ENTER_OK = json.dumps({
    "success": True, "message": "进入沙滩成功",
    "trash_items": [{"name": "瓶子", "x": 10, "y": 20}, {"name": "木材", "x": 30, "y": 40}]})
RESP_ENTER_COOLDOWN = json.dumps({"success": False, "message": "沙滩还在冷却中，请稍后再来"})
RESP_COLLECT_OK = json.dumps({
    "success": True, "message": "收集完成",
    "collected_items": {"瓶子": 2, "木材": 1, "蚯蚓": 3}})
RESP_CRAFT_OK = json.dumps({"success": True, "message": "炼造成功"})
RESP_CRAFT_FAIL = json.dumps({"success": False, "message": "材料不足"})
RESP_EXCHANGE_OK = json.dumps({"success": True, "message": "兑换成功", "points_gained": 2000})

# 材料充裕、砖块 200 的库存（假值）：够炼 4 颗魔丸的破烂量
FULL_INV = {"砖块": 200, "木材": 40, "塑料袋": 60, "瓶子": 40, "螺丝": 40,
            "旧电池": 20, "破铜片": 20, "木工件": 0, "塑料件": 0,
            "简易工具": 0, "能量碎片": 0, "魔丸胚胎": 0, "魔丸": 5, "蚯蚓": 100}


def make_plugin(config=None):
    plugin = PLUGIN_MODULE.SiQiMowan()
    base = {"enabled": True, "cookie": "c_secure_pass=deadbeef; uid=1"}
    base.update(config or {})
    plugin.init_plugin(base)
    return plugin


def priv(plugin, name):
    return getattr(plugin, f"_SiQiMowan__{name}")


def state(**over):
    """构造一份解析后的 gameData 状态。"""
    base = {
        "daily_bricks": 3, "points": 1000000, "bonus_earned": 20000,
        "last_beach_time": 1792157224, "beach_interval": 7200,
        "next_brick_reset_ts": 1792166400, "server_now": 1792165559,
        "trash_items": [], "inventory": dict(FULL_INV),
        "recipes": PLUGIN_MODULE.SiQiMowan.RECIPES,
    }
    base.update(over)
    return base


# ---------------------------------------------------------------- 页面解析
class ParsePageTest(unittest.TestCase):
    def test_game_state_parsed(self):
        got = priv(make_plugin(), "parse_game_state")(PAGE_HTML)
        self.assertIsNotNone(got)
        self.assertEqual(got["daily_bricks"], 3)
        self.assertEqual(got["points"], 1000000)
        self.assertEqual(got["bonus_earned"], 20000)
        self.assertEqual(got["last_beach_time"], 1792157224)
        self.assertEqual(got["beach_interval"], 7200)
        self.assertEqual(got["next_brick_reset_ts"], 1792166400)
        self.assertEqual(got["server_now"], 1792165559)

    def test_inventory_escapes_decoded(self):
        got = priv(make_plugin(), "parse_game_state")(PAGE_HTML)
        self.assertEqual(got["inventory"]["砖块"], 200)
        self.assertEqual(got["inventory"]["魔丸"], 5)
        self.assertEqual(got["inventory"]["蚯蚓"], 100)
        self.assertEqual(len(got["inventory"]), 14)

    def test_recipes_parsed_with_ingredients(self):
        got = priv(make_plugin(), "parse_game_state")(PAGE_HTML)
        recipes = got["recipes"]
        self.assertEqual(len(recipes), 6)
        r6 = recipes[6]
        self.assertEqual(r6["result"], "魔丸")
        self.assertEqual(r6["ingredients"], {"砖块": 10, "魔丸胚胎": 2})

    def test_garbage_page_returns_none(self):
        parse = priv(make_plugin(), "parse_game_state")
        for bad in ("", "<html>请先登录</html>", "{}", "let gameData = not-json;", None):
            self.assertIsNone(parse(bad), bad)

    def test_recipes_optional_but_state_kept(self):
        # 只有 gameData、没有 recipes 时，状态仍可用，配方退回内置表
        html = PAGE_HTML.split("function updateRecipes()")[0] + "</script></body></html>"
        got = priv(make_plugin(), "parse_game_state")(html)
        self.assertIsNotNone(got)
        self.assertEqual(got["inventory"]["砖块"], 200)
        self.assertEqual(len(got["recipes"]), 6)

    def test_hnr_and_level_parsed_through_strong_tags(self):
        got = priv(make_plugin(), "parse_game_state")(PAGE_HTML)
        self.assertEqual(got["hnr"], "4.5286")
        self.assertEqual(got["level"], "1")

    def test_page_numbers_are_optional(self):
        html = PAGE_HTML.replace('id="bonusEarned">20000<', 'id="bonusEarned">?<')
        got = priv(make_plugin(), "parse_game_state")(html)
        self.assertIsNotNone(got)
        self.assertIsNone(got["bonus_earned"])


class BeachStatusTest(unittest.TestCase):
    def test_cooling_down(self):
        plugin = make_plugin()
        ready, left = priv(plugin, "beach_status")(
            state(last_beach_time=1000, beach_interval=7200), now=2000)
        self.assertFalse(ready)
        self.assertEqual(left, 6200)

    def test_ready_when_interval_elapsed(self):
        plugin = make_plugin()
        ready, left = priv(plugin, "beach_status")(
            state(last_beach_time=1000, beach_interval=7200), now=9000)
        self.assertTrue(ready)
        self.assertEqual(left, 0)

    def test_missing_fields_are_treated_as_ready(self):
        plugin = make_plugin()
        ready, _ = priv(plugin, "beach_status")(state(last_beach_time=None), now=9000)
        self.assertTrue(ready)


# ---------------------------------------------------------------- 沙滩 + 炼造
class BeachTaskTest(unittest.TestCase):
    def _patch(self, plugin, state_obj, enter=RESP_ENTER_OK, collect=RESP_COLLECT_OK,
               craft=RESP_CRAFT_OK):
        calls = {"enter": 0, "collect": 0, "craft": []}
        setattr(plugin, "_SiQiMowan__fetch_game_state",
                lambda cookie, force=False: state_obj)

        def fake_enter(cookie):
            calls["enter"] += 1
            return priv(plugin, "parse_action_response")(enter)
        setattr(plugin, "_SiQiMowan__enter_beach", fake_enter)

        def fake_collect(cookie):
            calls["collect"] += 1
            return priv(plugin, "parse_action_response")(collect)
        setattr(plugin, "_SiQiMowan__collect_all_trash", fake_collect)

        def fake_craft(cookie, recipe_id, quantity):
            calls["craft"].append((recipe_id, quantity))
            return priv(plugin, "parse_action_response")(craft)
        setattr(plugin, "_SiQiMowan__craft_item", fake_craft)
        return calls

    def test_cooling_down_makes_no_beach_request(self):
        plugin = make_plugin({"beach_enabled": True})
        st = state(last_beach_time=int(time.time()) - 60, beach_interval=7200)
        calls = self._patch(plugin, st)
        result = plugin.run_beach_task()
        self.assertEqual(calls["enter"], 0, "冷却期不得发 enter_beach")
        self.assertEqual(calls["collect"], 0)
        self.assertEqual(result["status"], "completed")
        self.assertIn("冷却", result["message"])

    def test_ready_enters_then_collects(self):
        plugin = make_plugin({"beach_enabled": True})
        st = state(last_beach_time=int(time.time()) - 8000)
        calls = self._patch(plugin, st)
        result = plugin.run_beach_task()
        self.assertEqual(calls["enter"], 1)
        self.assertEqual(calls["collect"], 1)
        self.assertEqual(result["data"]["collected"]["蚯蚓"], 3)
        self.assertEqual(result["data"]["collected_total"], 6)

    def test_ready_always_runs_both_steps_even_with_leftover_trash(self):
        # 站点规范流程是「清理沙滩 → 一键收集」两步；即使沙滩上还有上次剩的垃圾，
        # 冷却已过就先重新进入（页面 entered 标记是会话级，服务端是否要求先进入未证实）
        plugin = make_plugin({"beach_enabled": True})
        st = state(last_beach_time=int(time.time()) - 8000,
                   trash_items=[{"name": "瓶子", "x": 1, "y": 2}])
        calls = self._patch(plugin, st)
        result = plugin.run_beach_task()
        self.assertEqual(calls["enter"], 1, "冷却已过就该走完整两步")
        self.assertEqual(calls["collect"], 1)
        self.assertTrue(result["data"]["entered"])

    def test_leftover_trash_is_collected_while_cooling_down(self):
        # 用户截图里的状态：清理沙滩置灰（冷却），但一键收集可用（沙滩有垃圾）
        plugin = make_plugin({"beach_enabled": True})
        st = state(last_beach_time=int(time.time()) - 60,
                   trash_items=[{"name": "木材", "x": 1, "y": 2}])
        calls = self._patch(plugin, st)
        result = plugin.run_beach_task()
        self.assertEqual(calls["enter"], 0, "冷却中进不去")
        self.assertEqual(calls["collect"], 1, "沙滩上已有的垃圾仍要收集")
        self.assertIn("冷却", result["message"])
        self.assertEqual(result["data"]["collected_total"], 6)

    def test_server_refuses_enter_but_leftover_trash_still_collected(self):
        plugin = make_plugin({"beach_enabled": True})
        st = state(last_beach_time=int(time.time()) - 8000,
                   trash_items=[{"name": "瓶子", "x": 1, "y": 2}])
        calls = self._patch(plugin, st, enter=RESP_ENTER_COOLDOWN)
        result = plugin.run_beach_task()
        self.assertEqual(calls["enter"], 1)
        self.assertEqual(calls["collect"], 1, "服务端说冷却时退化为只收集")
        self.assertEqual(result["data"]["collected_total"], 6)

    def test_enter_without_new_trash_skips_collect(self):
        # 与页面一致：垃圾数为 0 时「一键收集」是置灰的，不该发请求
        plugin = make_plugin({"beach_enabled": True})
        st = state(last_beach_time=int(time.time()) - 8000)
        enter_empty = json.dumps({"success": True, "message": "进入沙滩成功", "trash_items": []})
        calls = self._patch(plugin, st, enter=enter_empty)
        result = plugin.run_beach_task()
        self.assertEqual(calls["enter"], 1)
        self.assertEqual(calls["collect"], 0)
        self.assertEqual(result["status"], "completed")

    def test_enter_without_trash_field_still_collects(self):
        # 响应里没有 trash_items 字段时，不知道有没有垃圾，按页面惯例收集一次
        plugin = make_plugin({"beach_enabled": True})
        st = state(last_beach_time=int(time.time()) - 8000)
        enter_bare = json.dumps({"success": True, "message": "进入沙滩成功"})
        calls = self._patch(plugin, st, enter=enter_bare)
        plugin.run_beach_task()
        self.assertEqual(calls["enter"], 1)
        self.assertEqual(calls["collect"], 1)

    def test_enter_refused_reports_and_does_not_collect(self):
        plugin = make_plugin({"beach_enabled": True})
        st = state(last_beach_time=int(time.time()) - 8000)
        calls = self._patch(plugin, st, enter=RESP_ENTER_COOLDOWN)
        result = plugin.run_beach_task()
        self.assertEqual(calls["collect"], 0)
        self.assertIn("冷却", result["message"])

    def test_state_fetch_failure_makes_no_request(self):
        plugin = make_plugin({"beach_enabled": True})
        calls = self._patch(plugin, None)
        result = plugin.run_beach_task()
        self.assertEqual(calls["enter"], 0)
        self.assertEqual(calls["collect"], 0)
        self.assertEqual(result["status"], "unknown")

    def test_missing_cookie_makes_no_call(self):
        plugin = make_plugin({"beach_enabled": True, "cookie": ""})
        calls = self._patch(plugin, state())
        result = plugin.run_beach_task()
        self.assertEqual(calls["enter"], 0)
        self.assertEqual(result["status"], "auth_failed")

    def test_craft_runs_in_same_round_when_enabled(self):
        plugin = make_plugin({"beach_enabled": True, "craft_enabled": True,
                              "craft_max_items_per_run": 500})
        st = state(last_beach_time=int(time.time()) - 8000)
        calls = self._patch(plugin, st)
        result = plugin.run_beach_task()
        self.assertTrue(calls["craft"], "炼造开关打开时应与沙滩同轮执行")
        self.assertEqual([c[0] for c in calls["craft"]], [1, 2, 3, 4, 5, 6])
        self.assertIn("craft", result["data"])

    def test_craft_off_stockpiles_only(self):
        plugin = make_plugin({"beach_enabled": True, "craft_enabled": False})
        st = state(last_beach_time=int(time.time()) - 8000)
        calls = self._patch(plugin, st)
        plugin.run_beach_task()
        self.assertEqual(calls["craft"], [], "炼造开关关闭时只囤不炼")

    def test_craft_failure_stops_but_beach_result_kept(self):
        plugin = make_plugin({"beach_enabled": True, "craft_enabled": True})
        st = state(last_beach_time=int(time.time()) - 8000)
        calls = self._patch(plugin, st, craft=RESP_CRAFT_FAIL)
        result = plugin.run_beach_task()
        self.assertEqual(len(calls["craft"]), 1, "第一步失败即停，不再炼后续配方")
        self.assertEqual(result["data"]["collected_total"], 6)
        self.assertEqual(result["data"]["craft"]["status"], "failed")


class BeachRefreshTest(unittest.TestCase):
    def test_inventory_refreshed_after_craft(self):
        """炼造会烧掉大量材料，结束后必须重取状态，否则面板显示旧库存。"""
        plugin = make_plugin({"beach_enabled": True, "craft_enabled": True})
        before = state(last_beach_time=int(time.time()) - 8000,
                       inventory=dict(FULL_INV, **{"砖块": 200}))
        after = state(last_beach_time=int(time.time()) - 8000,
                      inventory=dict(FULL_INV, **{"砖块": 1, "魔丸": 21}))
        calls = {"fetch": 0}

        def fake_fetch(cookie, force=False):
            calls["fetch"] += 1
            return before if calls["fetch"] == 1 else after
        setattr(plugin, "_SiQiMowan__fetch_game_state", fake_fetch)
        setattr(plugin, "_SiQiMowan__enter_beach",
                lambda cookie, force=False: priv(plugin, "parse_action_response")(RESP_ENTER_OK))
        setattr(plugin, "_SiQiMowan__collect_all_trash",
                lambda cookie, force=False: priv(plugin, "parse_action_response")(RESP_COLLECT_OK))
        setattr(plugin, "_SiQiMowan__craft_item",
                lambda cookie, rid, qty: priv(plugin, "parse_action_response")(RESP_CRAFT_OK))

        plugin.run_beach_task()
        self.assertEqual(plugin._inventory["砖块"], 1, "炼造后应刷新为最新库存")
        self.assertEqual(plugin._game_stats["magic_pills"], 21)


class CraftPlanTest(unittest.TestCase):
    def test_plan_is_demand_driven_for_one_mowan(self):
        plugin = make_plugin()
        inv = dict(FULL_INV, **{"砖块": 50, "木材": 4, "塑料袋": 8, "瓶子": 4,
                                "螺丝": 4, "旧电池": 2, "破铜片": 2})
        plan = priv(plugin, "craft_plan")(inv)
        self.assertEqual(plan, {1: 4, 2: 4, 3: 2, 4: 2, 5: 2, 6: 1})

    def test_plan_scales_with_bricks(self):
        plugin = make_plugin()
        inv = dict(FULL_INV, **{"砖块": 100, "木材": 8, "塑料袋": 16, "瓶子": 8,
                                "螺丝": 8, "旧电池": 4, "破铜片": 4})
        plan = priv(plugin, "craft_plan")(inv)
        self.assertEqual(plan, {1: 8, 2: 8, 3: 4, 4: 4, 5: 4, 6: 2})

    def test_plan_uses_stockpiled_intermediates(self):
        plugin = make_plugin()
        inv = dict(FULL_INV, **{"砖块": 50, "木工件": 4, "塑料件": 4,
                                "螺丝": 4, "旧电池": 2, "破铜片": 2})
        plan = priv(plugin, "craft_plan")(inv)
        self.assertNotIn(1, plan, "木工件够用时不该再炼")
        self.assertNotIn(2, plan, "塑料件够用时不该再炼")
        self.assertEqual(plan[6], 1)

    def test_no_plan_when_bricks_below_one_mowan(self):
        plugin = make_plugin()
        plan = priv(plugin, "craft_plan")(dict(FULL_INV, **{"砖块": 49}))
        self.assertEqual(plan, {})

    def test_keep_bricks_reserve_is_respected(self):
        plugin = make_plugin({"craft_keep_bricks": 60})
        inv = dict(FULL_INV, **{"砖块": 110, "木材": 4, "塑料袋": 8, "瓶子": 4,
                                "螺丝": 4, "旧电池": 2, "破铜片": 2})
        plan = priv(plugin, "craft_plan")(inv)
        self.assertEqual(plan, {1: 4, 2: 4, 3: 2, 4: 2, 5: 2, 6: 1})

    def test_missing_materials_yield_no_plan(self):
        plugin = make_plugin()
        plan = priv(plugin, "craft_plan")(dict(FULL_INV, **{"破铜片": 0}))
        self.assertEqual(plan, {})

    def test_max_items_per_run_caps_plan(self):
        plugin = make_plugin({"craft_max_items_per_run": 15})
        plan = priv(plugin, "craft_plan")(dict(FULL_INV))
        total = sum(plan.values())
        self.assertLessEqual(total, 15)
        self.assertEqual(plan.get(6), 1)


class CraftExecutionTest(unittest.TestCase):
    def _patch(self, plugin, state_obj, responses):
        calls = []
        setattr(plugin, "_SiQiMowan__fetch_game_state", lambda cookie, force=False: state_obj)

        def fake_craft(cookie, recipe_id, quantity):
            calls.append((recipe_id, quantity))
            idx = min(len(calls) - 1, len(responses) - 1)
            return priv(plugin, "parse_action_response")(responses[idx])
        setattr(plugin, "_SiQiMowan__craft_item", fake_craft)
        return calls

    def test_craft_switch_off_makes_no_request(self):
        plugin = make_plugin({"craft_enabled": False})
        calls = self._patch(plugin, state(), [RESP_CRAFT_OK])
        result = plugin.run_craft_step(state())
        self.assertEqual(calls, [])
        self.assertEqual(result["status"], "skipped")

    def test_craft_stops_on_refusal(self):
        plugin = make_plugin({"craft_enabled": True})
        calls = self._patch(plugin, state(), [RESP_CRAFT_OK, RESP_CRAFT_FAIL])
        result = plugin.run_craft_step(state())
        self.assertEqual([c[0] for c in calls], [1, 2], "第 2 步被拒后不再往下炼")
        self.assertEqual(result["status"], "partial")

    def test_craft_unknown_result_stops_without_retry(self):
        plugin = make_plugin({"craft_enabled": True})
        calls = []
        setattr(plugin, "_SiQiMowan__fetch_game_state", lambda cookie, force=False: state())

        def fake_craft(cookie, recipe_id, quantity):
            calls.append(recipe_id)
            return None
        setattr(plugin, "_SiQiMowan__craft_item", fake_craft)
        result = plugin.run_craft_step(state())
        self.assertEqual(calls, [1])
        self.assertEqual(result["status"], "unknown")

    def test_craft_chunks_requests_at_site_cap(self):
        plugin = make_plugin({"craft_enabled": True})
        calls = []
        setattr(plugin, "_SiQiMowan__fetch_game_state", lambda cookie, force=False: state())

        def fake_craft(cookie, recipe_id, quantity):
            calls.append((recipe_id, quantity))
            return priv(plugin, "parse_action_response")(RESP_CRAFT_OK)
        setattr(plugin, "_SiQiMowan__craft_item", fake_craft)
        plan = priv(plugin, "craft_plan")(dict(FULL_INV, **{"砖块": 4000, "木材": 1000,
                                                           "塑料袋": 2000, "瓶子": 1000,
                                                           "螺丝": 1000, "旧电池": 500,
                                                           "破铜片": 500}))
        priv(plugin, "execute_craft_plan")(plan)
        self.assertTrue(all(q <= 100 for _, q in calls), calls)


# ---------------------------------------------------------------- 兑换
class ExchangeTaskTest(unittest.TestCase):
    def _patch(self, plugin, state_obj, responses=RESP_EXCHANGE_OK):
        calls = []
        setattr(plugin, "_SiQiMowan__fetch_game_state", lambda cookie, force=False: state_obj)

        def fake_exchange(cookie, quantity):
            calls.append(quantity)
            seq = responses if isinstance(responses, list) else [responses] * 20
            idx = min(len(calls) - 1, len(seq) - 1)
            return priv(plugin, "parse_action_response")(seq[idx])
        setattr(plugin, "_SiQiMowan__exchange_points", fake_exchange)
        return calls

    def test_uses_configured_quantity(self):
        plugin = make_plugin({"exchange_enabled": True, "exchange_quantity": 3})
        calls = self._patch(plugin, state(inventory=dict(FULL_INV, **{"魔丸": 10})))
        result = plugin.run_exchange_task()
        self.assertEqual(calls, [3])
        self.assertEqual(result["data"]["exchanged"], 3)
        self.assertEqual(result["data"]["points_gained"], 2000)

    def test_zero_quantity_means_exchange_all(self):
        plugin = make_plugin({"exchange_enabled": True, "exchange_quantity": 0})
        calls = self._patch(plugin, state(inventory=dict(FULL_INV, **{"魔丸": 7})))
        plugin.run_exchange_task()
        self.assertEqual(calls, [7])

    def test_truncates_to_holding(self):
        plugin = make_plugin({"exchange_enabled": True, "exchange_quantity": 99})
        calls = self._patch(plugin, state(inventory=dict(FULL_INV, **{"魔丸": 4})))
        plugin.run_exchange_task()
        self.assertEqual(calls, [4])

    def test_chunks_at_site_cap(self):
        plugin = make_plugin({"exchange_enabled": True, "exchange_quantity": 250})
        calls = self._patch(plugin, state(inventory=dict(FULL_INV, **{"魔丸": 250})))
        plugin.run_exchange_task()
        self.assertEqual(calls, [100, 100, 50])

    def test_no_mowan_makes_no_request(self):
        plugin = make_plugin({"exchange_enabled": True, "exchange_quantity": 1})
        calls = self._patch(plugin, state(inventory=dict(FULL_INV, **{"魔丸": 0})))
        result = plugin.run_exchange_task()
        self.assertEqual(calls, [])
        self.assertEqual(result["status"], "completed")

    def test_server_refusal_stops_without_retry(self):
        plugin = make_plugin({"exchange_enabled": True, "exchange_quantity": 250})
        calls = self._patch(plugin, state(inventory=dict(FULL_INV, **{"魔丸": 250})),
                            [RESP_EXCHANGE_OK, json.dumps({"success": False, "message": "魔丸数量不足！"})])
        result = plugin.run_exchange_task()
        self.assertEqual(calls, [100, 100])
        self.assertEqual(result["status"], "partial")

    def test_unknown_result_stops_without_retry(self):
        plugin = make_plugin({"exchange_enabled": True, "exchange_quantity": 250})
        calls = []
        setattr(plugin, "_SiQiMowan__fetch_game_state",
                lambda cookie, force=False: state(inventory=dict(FULL_INV, **{"魔丸": 250})))

        def fake_exchange(cookie, quantity):
            calls.append(quantity)
            return None
        setattr(plugin, "_SiQiMowan__exchange_points", fake_exchange)
        result = plugin.run_exchange_task()
        self.assertEqual(calls, [100])
        self.assertEqual(result["status"], "unknown")

    def test_missing_cookie_makes_no_call(self):
        plugin = make_plugin({"exchange_enabled": True, "cookie": ""})
        calls = self._patch(plugin, state())
        result = plugin.run_exchange_task()
        self.assertEqual(calls, [])
        self.assertEqual(result["status"], "auth_failed")


# ---------------------------------------------------------------- 搬砖
class BrickTaskTest(unittest.TestCase):
    def _patch(self, plugin, moves, state_obj=None):
        calls = []

        def fake_move(cookie):
            calls.append(cookie)
            seq = moves[0] if isinstance(moves[0], list) else moves
            idx = min(len(calls) - 1, len(seq) - 1)
            return priv(plugin, "parse_action_response")(seq[idx])
        setattr(plugin, "_SiQiMowan__move_brick", fake_move)
        setattr(plugin, "_SiQiMowan__fetch_game_state",
                lambda cookie, force=False: state_obj if state_obj is not None else state(daily_bricks=0))
        return calls

    def test_stops_at_daily_limit_and_does_not_retry_refusal(self):
        plugin = make_plugin({"brick_enabled": True, "brick_interval_ms": 0})
        seq = [RESP_MOVE_OK, RESP_MOVE_OK, RESP_MOVE_LIMIT]
        calls = self._patch(plugin, [seq], state(daily_bricks=47))
        with mock.patch("time.sleep"):
            result = plugin.run_brick_task()
        self.assertEqual(len(calls), 3, "达到上限后不应再发请求")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["data"]["moved"], 2)

    def test_already_at_limit_makes_no_move_call(self):
        plugin = make_plugin({"brick_enabled": True, "brick_interval_ms": 0})
        calls = self._patch(plugin, [], state(daily_bricks=50))
        with mock.patch("time.sleep"):
            result = plugin.run_brick_task()
        self.assertEqual(calls, [])
        self.assertEqual(result["status"], "completed")

    def test_records_daily_reset_day(self):
        plugin = make_plugin({"brick_enabled": True, "brick_interval_ms": 0,
                              "brick_daily_limit": 2})
        self._patch(plugin, [RESP_MOVE_OK], state(daily_bricks=0))
        with mock.patch("time.sleep"):
            plugin.run_brick_task()
        self.assertEqual(plugin._brick_stats["last_reset"], "2026-10-16")

    def test_unknown_result_stops_without_retry(self):
        plugin = make_plugin({"brick_enabled": True, "brick_interval_ms": 0})
        calls = []
        setattr(plugin, "_SiQiMowan__fetch_game_state", lambda cookie, force=False: state(daily_bricks=0))

        def fake_move(cookie):
            calls.append(cookie)
            return None
        setattr(plugin, "_SiQiMowan__move_brick", fake_move)
        with mock.patch("time.sleep"):
            result = plugin.run_brick_task()
        self.assertEqual(len(calls), 1)
        self.assertIn("未知", result["message"])

    def test_waits_between_calls_with_configured_interval(self):
        plugin = make_plugin({"brick_enabled": True, "brick_interval_ms": 300,
                              "brick_daily_limit": 3})
        seq = [json.dumps({"success": True, "message": "获得1块砖！",
                           "daily_bricks": n, "bricks_moved": 1}) for n in (1, 2, 3)]
        calls = self._patch(plugin, [seq], state(daily_bricks=0))
        with mock.patch("time.sleep") as sleeper:
            plugin.run_brick_task()
        self.assertEqual(len(calls), 3)
        self.assertEqual([c.args[0] for c in sleeper.call_args_list], [0.3, 0.3])

    def test_missing_cookie_makes_no_call(self):
        plugin = make_plugin({"brick_enabled": True, "cookie": ""})
        calls = self._patch(plugin, [])
        result = plugin.run_brick_task()
        self.assertEqual(calls, [])
        self.assertEqual(result["status"], "auth_failed")


# ---------------------------------------------------------------- 调度与服务
class ServiceTest(unittest.TestCase):
    def test_three_services_registered_independently(self):
        plugin = make_plugin({"enabled": True, "brick_enabled": True,
                              "beach_enabled": True, "exchange_enabled": True})
        ids = sorted(s["id"] for s in plugin.get_service())
        self.assertEqual(ids, ["SiQiBeach", "SiQiBrick", "SiQiExchange"])

    def test_services_use_distinct_funcs(self):
        plugin = make_plugin({"enabled": True, "brick_enabled": True,
                              "beach_enabled": True, "exchange_enabled": True})
        funcs = {s["id"]: s["func"] for s in plugin.get_service()}
        self.assertIsNot(funcs["SiQiBeach"], funcs["SiQiBrick"])
        self.assertIsNot(funcs["SiQiExchange"], funcs["SiQiBeach"])
        self.assertEqual(funcs["SiQiBeach"].__name__, "run_beach_task")

    def test_disabled_plugin_registers_nothing(self):
        plugin = make_plugin({"enabled": False, "brick_enabled": True,
                              "beach_enabled": True, "exchange_enabled": True})
        self.assertEqual(plugin.get_service(), [])

    def test_each_task_has_own_switch(self):
        plugin = make_plugin({"enabled": True, "brick_enabled": False,
                              "beach_enabled": True, "exchange_enabled": False})
        self.assertEqual([s["id"] for s in plugin.get_service()], ["SiQiBeach"])

    def test_beach_default_interval_is_121_minutes(self):
        _, defaults = make_plugin().get_form()
        self.assertEqual(defaults["beach_interval_minutes"], 121)

    def test_beach_interval_trigger_uses_configured_minutes(self):
        plugin = make_plugin({"enabled": True, "beach_enabled": True,
                              "beach_interval_minutes": 121})
        svc = [s for s in plugin.get_service() if s["id"] == "SiQiBeach"][0]
        self.assertEqual(getattr(svc["trigger"], "kw", {}).get("minutes"), 121)

    def test_daily_time_mode_schedules_that_time(self):
        CRON_CALLS.clear()
        plugin = make_plugin({"enabled": True, "brick_enabled": True,
                              "brick_daily_time": "03:30"})
        plugin.get_service()
        self.assertIn("30 3 * * *", CRON_CALLS)

    def test_invalid_daily_time_skips_only_that_task(self):
        plugin = make_plugin({"enabled": True, "brick_enabled": True,
                              "beach_enabled": True,
                              "brick_daily_time": "99:99"})
        ids = [s["id"] for s in plugin.get_service()]
        self.assertNotIn("SiQiBrick", ids)
        self.assertIn("SiQiBeach", ids)

    def test_time_to_cron_accepts_and_rejects(self):
        plugin = make_plugin()
        to_cron = priv(plugin, "time_to_cron")
        self.assertEqual(to_cron("00:05"), "5 0 * * *")
        self.assertEqual(to_cron("8:5"), "5 8 * * *")
        self.assertEqual(to_cron(" 07:00 "), "0 7 * * *")
        for bad in ("", "abc", "25:00", "10:99", "10", "10:5:5", None, ":"):
            self.assertIsNone(to_cron(bad), bad)

    def test_run_once_flags_start_the_right_task(self):
        plugin = make_plugin({"enabled": True, "brick_run_once": True,
                              "beach_run_once": True, "exchange_run_once": True})
        started = []
        with mock.patch.object(PLUGIN_MODULE.SiQiMowan, "run_brick_task",
                               lambda self: started.append("brick")), \
                mock.patch.object(PLUGIN_MODULE.SiQiMowan, "run_beach_task",
                                  lambda self: started.append("beach")), \
                mock.patch.object(PLUGIN_MODULE.SiQiMowan, "run_exchange_task",
                                  lambda self: started.append("exchange")), \
                mock.patch("threading.Thread") as thread:
            plugin.init_plugin({"enabled": True, "brick_run_once": True,
                                "beach_run_once": True, "exchange_run_once": True,
                                "cookie": "c_secure_pass=deadbeef"})
        self.assertEqual(thread.call_count, 3)


class FormTest(unittest.TestCase):
    KEYS = ("enabled", "cookie", "notify",
            "brick_enabled", "brick_daily_time", "brick_run_once",
            "beach_enabled", "beach_interval_minutes", "beach_run_once",
            "craft_enabled", "craft_keep_bricks", "craft_max_items_per_run",
            "exchange_enabled", "exchange_daily_time", "exchange_quantity",
            "exchange_run_once")

    def test_all_config_keys_in_form_and_defaults(self):
        forms, defaults = make_plugin().get_form()
        blob = json.dumps(forms, ensure_ascii=False)
        for key in self.KEYS:
            self.assertIn(key, blob, f"表单缺少 {key}")
            self.assertIn(key, defaults, f"默认值缺少 {key}")

    def test_form_drops_confusing_schedule_fields(self):
        """定时模式下拉、Cron 输入框、搬砖上限/间隔都不该再出现在界面上。"""
        forms, defaults = make_plugin().get_form()
        blob = json.dumps(forms, ensure_ascii=False)
        for gone in ("schedule_mode", "VCronField", "brick_daily_limit",
                     "brick_interval_ms", "beach_cron", "exchange_cron"):
            self.assertNotIn(gone, blob, f"表单不该再出现 {gone}")
            self.assertNotIn(gone, defaults, f"默认值不该再出现 {gone}")

    def test_form_fields_carry_no_static_value_prop(self):
        """默认值只能走 get_form() 的第二个返回值。

        实测教训：在 props 里塞 "value": 0 会和 v-model 打架，用户输入被顶回去，
        最后只存进 "01" 这种脏值——「炼造预留砖块改不了」就是这么来的。
        """
        forms, _ = make_plugin().get_form()
        blob = json.dumps(forms, ensure_ascii=False)
        self.assertNotIn('"value":', blob, "表单字段不应携带静态 value")

    def test_form_offers_daily_time_control(self):
        forms, _ = make_plugin().get_form()
        blob = json.dumps(forms, ensure_ascii=False)
        self.assertIn('"time"', blob, "需要 type=time 的每天固定时间控件")
        self.assertIn("daily_time", blob)

    def test_invalid_time_configured_keeps_value_for_display(self):
        plugin = make_plugin({"brick_schedule_mode": "daily_time",
                              "brick_daily_time": "not-a-time"})
        _, defaults = plugin.get_form()
        self.assertEqual(defaults["brick_daily_time"], "not-a-time")


class NamingTest(unittest.TestCase):
    def test_plugin_identity(self):
        cls = PLUGIN_MODULE.SiQiMowan
        self.assertEqual(cls.plugin_name, "思齐搬砖捡破烂炼魔丸")
        self.assertEqual(cls.plugin_config_prefix, "siqimowan_")
        index = json.loads((ROOT / "package.v2.json").read_text())
        self.assertEqual(cls.plugin_version, index["SiQiMowan"]["version"],
                         "plugin_version 必须与 package.v2.json 一致，否则市场不提示更新")
        self.assertEqual(cls.plugin_author, "Fanzy8")

    def test_directory_is_plugin_id(self):
        self.assertTrue(PLUGIN_PATH.exists())
        self.assertEqual(PLUGIN_PATH.parent.name, "siqimowan")

    def test_old_plugin_dir_is_gone(self):
        self.assertFalse((ROOT / "plugins.v2" / "siqicleanbeach").exists(),
                         "旧目录应已改名迁移")


class ProtocolTest(unittest.TestCase):
    """把从真实页面读到的 action 名与参数固化成断言，防止被猜改。"""

    def test_action_names_present_in_source(self):
        src = PLUGIN_PATH.read_text()
        for action in ("move_brick", "enter_beach", "collect_all_trash",
                       "craft_item", "exchange_points", "sync_game_state"):
            self.assertIn(action, src, action)

    def test_craft_request_uses_recipe_id_and_quantity(self):
        plugin = make_plugin()
        seen = {}

        class FakeResp:
            status_code = 200
            text = RESP_CRAFT_OK

        def fake_post(url, headers=None, cookies=None, data=None, timeout=None, verify=None):
            seen.update(data or {})
            return FakeResp()

        with mock.patch.object(PLUGIN_MODULE.requests, "post", fake_post):
            priv(plugin, "craft_item")("c_secure_pass=x", 3, 2)
        self.assertEqual(seen, {"action": "craft_item", "recipe_id": 3, "quantity": 2})

    def test_exchange_request_uses_quantity(self):
        plugin = make_plugin()
        seen = {}

        class FakeResp:
            status_code = 200
            text = RESP_EXCHANGE_OK

        def fake_post(url, headers=None, cookies=None, data=None, timeout=None, verify=None):
            seen.update(data or {})
            return FakeResp()

        with mock.patch.object(PLUGIN_MODULE.requests, "post", fake_post):
            priv(plugin, "exchange_points")("c_secure_pass=x", 7)
        self.assertEqual(seen, {"action": "exchange_points", "quantity": 7})

    def test_collect_all_trash_body_has_no_params(self):
        plugin = make_plugin()
        seen = {}

        class FakeResp:
            status_code = 200
            text = RESP_COLLECT_OK

        def fake_post(url, headers=None, cookies=None, data=None, timeout=None, verify=None):
            seen.update(data or {})
            return FakeResp()

        with mock.patch.object(PLUGIN_MODULE.requests, "post", fake_post):
            priv(plugin, "collect_all_trash")("c_secure_pass=x")
        self.assertEqual(seen, {"action": "collect_all_trash"})


class StateCacheTest(unittest.TestCase):
    """三个任务同时触发时只该取一次页面（实测日志里连取 3 次）。"""

    def _patch_page(self, plugin):
        calls = {"n": 0}

        def fake_page(cookie):
            calls["n"] += 1
            return PAGE_HTML
        setattr(plugin, "_SiQiMowan__fetch_page", fake_page)
        return calls

    def test_rapid_calls_share_one_fetch(self):
        plugin = make_plugin()
        calls = self._patch_page(plugin)
        first = priv(plugin, "fetch_game_state")("c_secure_pass=x")
        second = priv(plugin, "fetch_game_state")("c_secure_pass=x")
        self.assertEqual(calls["n"], 1, "5 秒内应复用缓存")
        self.assertIs(first, second)
        priv(plugin, "fetch_game_state")("c_secure_pass=x", True)
        self.assertEqual(calls["n"], 2, "force=True 应绕过缓存")

    def test_cache_keyed_by_cookie(self):
        plugin = make_plugin()
        calls = self._patch_page(plugin)
        priv(plugin, "fetch_game_state")("c_secure_pass=a")
        priv(plugin, "fetch_game_state")("c_secure_pass=b")
        self.assertEqual(calls["n"], 2, "换了 Cookie 不能复用")

    def test_cache_expires_after_ttl(self):
        plugin = make_plugin()
        calls = self._patch_page(plugin)
        priv(plugin, "fetch_game_state")("c_secure_pass=x")
        with mock.patch.object(PLUGIN_MODULE.time, "time",
                               return_value=time.time() + PLUGIN_MODULE.SiQiMowan.STATE_CACHE_TTL + 1):
            priv(plugin, "fetch_game_state")("c_secure_pass=x")
        self.assertEqual(calls["n"], 2, "超过 TTL 应重新取")


class PanelTest(unittest.TestCase):
    def _rich_plugin(self):
        plugin = make_plugin({"enabled": True})
        plugin._inventory = dict(FULL_INV)
        plugin._game_stats.update({
            "points": 1000000, "bonus_earned": 20000, "magic_pills": 5, "daily_bricks": 50,
            "daily_limit": 50, "last_beach_time": 1792157224, "beach_interval": 7200,
            "hnr": "4.5286", "level": "1", "last_update": 1792165559})
        return plugin

    def test_page_renders_and_is_json_serializable(self):
        page = self._rich_plugin().get_page()
        blob = json.dumps(page, ensure_ascii=False)
        for text in ("数据面板", "炼造工坊", "物品栏", "任务状态", "执行记录", "魔力值", "当前魔丸"):
            self.assertIn(text, blob, text)

    def test_page_shows_max_craftable_from_inventory(self):
        blob = json.dumps(self._rich_plugin().get_page(), ensure_ascii=False)
        # 砖块 200/5=40，木材 40/1=40，塑料袋 60/1=60 → 木工件最多 40
        self.assertIn("最多 40", blob)
        # 魔丸胚胎为 0 → 魔丸配方显示材料不足
        self.assertIn("材料不足", blob)

    def test_page_survives_empty_stats(self):
        page = make_plugin().get_page()
        blob = json.dumps(page, ensure_ascii=False)
        self.assertIn("数据面板", blob)
        self.assertIn("暂无物品数据", blob)


if __name__ == "__main__":
    unittest.main(verbosity=2)
