# -*- coding: utf-8 -*-
"""思齐搬砖捡破烂炼魔丸（siqimowan，V3 版）。

把思齐站 mowan.php 上的整条游戏链自动化：搬砖 → 沙滩捡破烂 → 炼造魔丸 → 兑换魔力值。

站点协议全部来自用户已登录浏览器里抓到的真实页面源码（mowan.php），未做任何猜测：

- `GET mowan.php` 返回的 HTML 内嵌服务端权威状态：
  `let gameData = {dailyBricks, points, lastBeachTime, beachInterval, nextBrickResetTs,
  currentTrashItems, inventory{名称:数量}}`，以及 `const recipes = [...]` 配方表。
- `POST mowan.php`，body 只有 `action=...`（application/x-www-form-urlencoded）：
  - `move_brick`（无参）→ success, message, daily_bricks, bricks_moved, next_brick_reset_ts
  - `enter_beach`（无参）→ success, message, trash_items[]
  - `collect_all_trash`（无参）→ success, message, collected_items{名称:数量}
  - `craft_item`（recipe_id, quantity）→ success, message
  - `exchange_points`（quantity）→ success, message, points_gained
  - `sync_game_state`（无参）→ success, daily_bricks, last_brick_time, next_brick_reset_ts

配方链（站点给的 recipe_id）：
  1 木工件  ← 砖块5 + 木材1 + 塑料袋1
  2 塑料件  ← 砖块5 + 塑料袋1 + 瓶子1
  3 简易工具 ← 螺丝2 + 木工件2
  4 能量碎片 ← 旧电池1 + 塑料件2
  5 魔丸胚胎 ← 破铜片1 + 简易工具1 + 能量碎片1
  6 魔丸   ← 砖块10 + 魔丸胚胎2

因此 1 颗魔丸 ≈ 50 砖块 + 2 破铜片 + 4 螺丝 + 2 旧电池 + 4 木材 + 8 塑料袋 + 4 瓶子，
而砖块只有搬砖每天 50 块——砖块是瓶颈，炼造按「砖块优先保魔丸链」按需炼，不囤中间件。
"""
import json
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests
import urllib3
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from urllib3.exceptions import InsecureRequestWarning

from app.sdk.config import settings
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase
from app.db.oper.site import SiteOper
from app.schemas.types import EventType, MessageType

urllib3.disable_warnings(InsecureRequestWarning)


class SiQiMowan(_PluginBase):
    plugin_name = "思齐搬砖捡破烂炼魔丸"
    plugin_desc = ("自动在思齐站点搬砖、沙滩捡破烂、炼造魔丸并兑换魔力值；"
                   "搬砖／捡破烂／兑换各自独立开关与定时，炼造可跟沙滩同步执行。")
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "3.0.0"
    plugin_author = "Fanzy8"
    author_url = "https://github.com/Fanzy8"
    plugin_config_prefix = "siqimowan_"
    plugin_order = 30
    auth_level = 1

    SITE_DOMAIN = "si-qi.xyz"
    BASE_URL = "https://si-qi.xyz"
    MOWAN_URL = f"{BASE_URL}/mowan.php"

    USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0")

    # 站点硬上限（页面写死）
    BRICK_DAILY_LIMIT = 50          # 每天最多搬 50 块
    REQUEST_ITEM_CAP = 100          # 单次炼造／兑换请求的物品上限（页面 hardMax）

    # 配方表（与站点一致）
    RECIPES: Dict[int, Dict[str, Any]] = {
        1: {"result": "木工件", "ingredients": {"砖块": 5, "木材": 1, "塑料袋": 1}},
        2: {"result": "塑料件", "ingredients": {"砖块": 5, "塑料袋": 1, "瓶子": 1}},
        3: {"result": "简易工具", "ingredients": {"螺丝": 2, "木工件": 2}},
        4: {"result": "能量碎片", "ingredients": {"旧电池": 1, "塑料件": 2}},
        5: {"result": "魔丸胚胎", "ingredients": {"破铜片": 1, "简易工具": 1, "能量碎片": 1}},
        6: {"result": "魔丸", "ingredients": {"砖块": 10, "魔丸胚胎": 2}},
    }
    CRAFT_ORDER = (1, 2, 3, 4, 5, 6)   # 自下而上炼造
    STATE_CACHE_TTL = 5                # 秒：并发任务共用同一次页面取数
    MOWAN_RECIPE_ID = 6
    ITEMS_PER_MOWAN = 15               # 4+4+2+2+2+1，用于单次运行总量上限换算

    ITEM_ICONS = {
        "砖块": "🧱", "木材": "🪵", "塑料袋": "🛍️", "瓶子": "🧴", "螺丝": "🔩",
        "旧电池": "🔋", "破铜片": "🪙", "木工件": "🪚", "塑料件": "🪣",
        "简易工具": "🛠️", "能量碎片": "⚡", "魔丸胚胎": "🥚", "魔丸": "⚗️",
        "蚯蚓": "🪱",
    }
    ITEM_COLORS = {
        "砖块": "#c0392b", "木材": "#f39c12", "塑料袋": "#3498db", "瓶子": "#1abc9c",
        "螺丝": "#95a5a6", "旧电池": "#2c3e50", "破铜片": "#e67e22", "木工件": "#d4a574",
        "塑料件": "#5dade2", "简易工具": "#7f8c8d", "能量碎片": "#f1c40f",
        "魔丸胚胎": "#d7bde2", "魔丸": "#8e44ad", "蚯蚓": "#e67e22",
    }

    # ------------------------------------------------------------ 运行期状态
    _enabled = False
    _cookie = ""
    _notify = True

    _brick_enabled = False
    _brick_daily_limit = BRICK_DAILY_LIMIT
    _brick_interval_ms = 300
    _brick_daily_time = "00:05"
    _brick_run_once = False

    _beach_enabled = False
    _beach_interval_minutes = 121
    _beach_run_once = False

    _craft_enabled = False
    _craft_keep_bricks = 0
    _craft_max_items_per_run = 500

    _exchange_enabled = False
    _exchange_quantity = 1
    _exchange_daily_time = "00:30"
    _exchange_run_once = False

    # 自带定时器：三个任务都由插件内部调度，不交给宿主调度器。
    # 原因：MP 重启时宿主只给「加载靠前」的插件注册定时服务，本插件排在后段
    # 会被静默漏掉（到点不执行）；自带调度器随插件初始化建立，与宿主顺序无关。
    _scheduler: Optional[BackgroundScheduler] = None
    JOB_IDS = {"brick": "siqi_brick", "beach": "siqi_beach", "exchange": "siqi_exchange"}

    # 页面状态短缓存：三个任务同时触发时不必各取一次 mowan.php
    _state_cache: Dict[str, Any] = {"ts": 0.0, "cookie": "", "state": None}

    _brick_lock = threading.Lock()
    _beach_lock = threading.Lock()
    _exchange_lock = threading.Lock()

    _brick_stats: Dict[str, Any] = {
        "last_update": None, "last_result": "", "daily": None, "available": None,
        "total_moved": 0, "last_reset": "",
    }
    _beach_stats: Dict[str, Any] = {
        "last_update": None, "last_result": "", "total_collected": 0,
        "next_clean_ts": None, "collected": {},
    }
    _craft_stats: Dict[str, Any] = {
        "last_update": None, "last_result": "", "total_crafted": 0, "crafted": {},
    }
    _exchange_stats: Dict[str, Any] = {
        "last_update": None, "last_result": "", "total_exchanged": 0, "total_points": 0,
    }
    _game_stats: Dict[str, Any] = {
        "last_update": None, "points": None, "bonus_earned": None, "magic_pills": None,
        "daily_bricks": None, "daily_limit": BRICK_DAILY_LIMIT, "last_beach_time": None,
        "beach_interval": None, "next_brick_reset_ts": None, "hnr": None, "level": None,
    }
    _inventory: Dict[str, Any] = {}
    _stats: Dict[str, Any] = {"last_update": None, "last_result": "", "history": []}

    # ------------------------------------------------------------ 生命周期
    def init_plugin(self, config: dict = None):
        config = config or {}
        site_cookie = self.__get_site_cookie()

        self._enabled = bool(config.get("enabled", False))
        self._cookie = (config.get("cookie") or site_cookie or "").strip()
        self._notify = bool(config.get("notify", True))

        # 搬砖
        self._brick_enabled = bool(config.get("brick_enabled", False))
        self._brick_daily_limit = self.__safe_int(
            config.get("brick_daily_limit"), self.BRICK_DAILY_LIMIT, min_value=1)
        self._brick_interval_ms = self.__safe_int(
            config.get("brick_interval_ms"), 300, min_value=0)
        self._brick_daily_time = self.__clean_time(config.get("brick_daily_time"), "00:05")
        self._brick_run_once = bool(config.get("brick_run_once", False))

        # 沙滩捡破烂（+ 可选炼造）
        self._beach_enabled = bool(config.get("beach_enabled", False))
        self._beach_interval_minutes = self.__safe_int(
            config.get("beach_interval_minutes"), 121, min_value=1)
        self._beach_run_once = bool(config.get("beach_run_once", False))

        self._craft_enabled = bool(config.get("craft_enabled", False))
        self._craft_keep_bricks = self.__safe_int(
            config.get("craft_keep_bricks"), 0, min_value=0)
        self._craft_max_items_per_run = self.__safe_int(
            config.get("craft_max_items_per_run"), 500, min_value=1)

        # 兑换魔力
        self._exchange_enabled = bool(config.get("exchange_enabled", False))
        self._exchange_quantity = self.__safe_int(
            config.get("exchange_quantity"), 1, min_value=0)
        self._exchange_daily_time = self.__clean_time(config.get("exchange_daily_time"), "00:30")
        self._exchange_run_once = bool(config.get("exchange_run_once", False))

        for key, target in (("brick_stats", self._brick_stats),
                            ("beach_stats", self._beach_stats),
                            ("craft_stats", self._craft_stats),
                            ("exchange_stats", self._exchange_stats),
                            ("game_stats", self._game_stats),
                            ("stats", self._stats)):
            value = config.get(key)
            if isinstance(value, dict):
                target.update(value)

        inventory = config.get("inventory")
        if isinstance(inventory, dict):
            self._inventory = inventory

        self._state_cache = {"ts": 0.0, "cookie": "", "state": None}

        logger.info(
            f"{self.plugin_name} 初始化完成：enabled={self._enabled}, "
            f"搬砖={self._brick_enabled}(每天{self._brick_daily_time}), "
            f"捡破烂={self._beach_enabled}(每{self._beach_interval_minutes}分钟), "
            f"炼造={self._craft_enabled}, 兑换={self._exchange_enabled}"
        )

        # 建立/重建插件自带的定时器（每次初始化都先停旧的，避免重复任务）
        self.__setup_scheduler()

        pending = [(self._brick_run_once, "run_brick_task", "搬砖"),
                   (self._beach_run_once, "run_beach_task", "捡破烂"),
                   (self._exchange_run_once, "run_exchange_task", "兑换魔力")]
        for flag, method, label in pending:
            if not flag:
                continue
            logger.info(f"收到配置页「立即{label}」请求，后台启动任务")
            threading.Thread(target=getattr(self, method), daemon=True).start()

        if any(flag for flag, _, _ in pending):
            self._brick_run_once = self._beach_run_once = self._exchange_run_once = False
            self._save_config()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [
            {"cmd": "/siqi_brick", "event": EventType.PluginAction,
             "desc": "思齐立即搬砖", "category": "站点",
             "data": {"action": "siqi_brick"}},
            {"cmd": "/siqi_beach", "event": EventType.PluginAction,
             "desc": "思齐立即清理沙滩并收集", "category": "站点",
             "data": {"action": "siqi_beach"}},
            {"cmd": "/siqi_exchange", "event": EventType.PluginAction,
             "desc": "思齐立即兑换魔力值", "category": "站点",
             "data": {"action": "siqi_exchange"}},
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {"path": "/SiQiMowan/brick/run", "endpoint": self.run_brick_api,
             "methods": ["POST"], "auth": "bear",
             "summary": "立即搬砖", "description": "按当前配置立即执行一次搬砖任务。"},
            {"path": "/SiQiMowan/beach/run", "endpoint": self.run_beach_api,
             "methods": ["POST"], "auth": "bear",
             "summary": "立即清理沙滩", "description": "按当前配置立即清理沙滩并一键收集（含炼造）。"},
            {"path": "/SiQiMowan/exchange/run", "endpoint": self.run_exchange_api,
             "methods": ["POST"], "auth": "bear",
             "summary": "立即兑换魔力", "description": "按当前配置立即把魔丸兑换为魔力值。"},
            {"path": "/SiQiMowan/stats", "endpoint": self.get_stats_api,
             "methods": ["GET"], "auth": "bear",
             "summary": "获取游戏数据", "description": "获取思齐游戏面板与各任务统计数据。"},
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """不向宿主注册定时服务：三个任务都由插件自带的 BackgroundScheduler 调度。

        MP 重启时会用「插件加载顺序」逐个注册服务，而调度器单例可能被排在前面
        的插件提前创建，导致后段插件（本插件）的定时任务被静默漏掉——表现就是
        到点不执行。自带调度器在 init_plugin 里建立，不依赖宿主启动顺序。
        """
        return []

    def __setup_scheduler(self):
        """按当前配置建立插件自带的定时器。"""
        self.__stop_scheduler()
        if not self._enabled:
            logger.info(f"{self.plugin_name}：未启用，内部定时器不启动")
            return

        scheduler = BackgroundScheduler(
            timezone=settings.TZ,
            job_defaults={"coalesce": True, "max_instances": 1,
                          "misfire_grace_time": 3600},
        )
        plan = (
            (self._brick_enabled, "brick", "思齐自动搬砖", self.run_brick_task,
             "daily_time", self._brick_daily_time, 0),
            (self._beach_enabled, "beach", "思齐捡破烂炼魔丸", self.run_beach_task,
             "interval", "", self._beach_interval_minutes),
            (self._exchange_enabled, "exchange", "思齐兑换魔力值", self.run_exchange_task,
             "daily_time", self._exchange_daily_time, 0),
        )

        for enabled, kind, name, func, mode, daily_time, minutes in plan:
            if not enabled:
                continue
            trigger = None
            if mode == "interval":
                try:
                    trigger = self.__interval_trigger(minutes)
                except Exception as e:
                    logger.warn(f"{name} 间隔触发器创建失败：{e}")
            else:
                expr = self.__time_to_cron(daily_time)
                if not expr:
                    logger.warn(f"{name} 执行时间无效：{daily_time}（应为 HH:MM），已跳过该任务")
                else:
                    try:
                        trigger = CronTrigger.from_crontab(expr, timezone=settings.TZ)
                    except ValueError as e:
                        logger.warn(f"{name} Cron 配置无效：{e}")
            if trigger is None:
                continue
            try:
                scheduler.add_job(func, trigger, id=self.JOB_IDS[kind],
                                  name=name, replace_existing=True)
            except Exception as e:
                logger.error(f"{name} 注册到内部定时器失败：{e}")

        if not scheduler.get_jobs():
            logger.info(f"{self.plugin_name}：没有启用任何定时任务，内部定时器未启动")
            return

        scheduler.start()
        self._scheduler = scheduler
        plans = " | ".join(
            f"{job.name}：{self.__fmt_next_run(job.next_run_time)}"
            for job in scheduler.get_jobs())
        logger.info(f"[思齐任务] 内部定时器就绪 | {plans}")

    @staticmethod
    def __fmt_next_run(value: Optional[datetime]) -> str:
        """把下次执行时间统一换算成站点本地时区再显示。

        IntervalTrigger 的 start_date 是 UTC，CronTrigger 用 settings.TZ——
        直接格式化会一个显示 UTC、一个显示本地，看着像差 8 小时。
        """
        if not value:
            return "-"
        try:
            return value.astimezone(ZoneInfo(settings.TZ)).strftime("%m-%d %H:%M")
        except Exception:
            return value.strftime("%m-%d %H:%M")

    def __stop_scheduler(self):
        """停止插件自带的定时器（重复初始化、插件停用时都会调用）。"""
        scheduler = self._scheduler
        self._scheduler = None
        if not scheduler:
            return
        try:
            if scheduler.running:
                scheduler.shutdown(wait=False)
            logger.info(f"{self.plugin_name}：内部定时器已停止")
        except Exception as e:
            logger.warn(f"{self.plugin_name}：停止内部定时器失败：{e}")

    def __interval_trigger(self, minutes: int) -> IntervalTrigger:
        """按上次执行时间对齐间隔任务。

        宿主保存配置时会重建定时任务（`PluginConfigCommand.update` → `initialize`
        → `PluginReload` → `update_plugin_job`），若直接 `IntervalTrigger(minutes=N)`，
        倒计时就从保存那一刻重新开始——只改「发送通知」也会把几分钟后该跑的一轮
        推迟整整一个间隔。这里用上次执行时间 + 间隔作为起点，保存配置不再影响节奏。
        """
        interval = max(1, int(minutes)) * 60
        now = time.time()
        next_ts = now + interval
        last = self._beach_stats.get("last_update")
        if isinstance(last, (int, float)) and last > 0:
            candidate = float(last) + interval
            # 已过点（插件停用过、或刚改短间隔）：几秒后补跑一轮，别白等一个间隔
            next_ts = candidate if candidate > now else now + 5
        return IntervalTrigger(minutes=minutes,
                               start_date=datetime.fromtimestamp(next_ts, tz=timezone.utc))

    def stop_service(self):
        self.__stop_scheduler()

    # ------------------------------------------------------------ 配置表单
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        site_cookie = self.__get_site_cookie()
        cookie_value = self._cookie or site_cookie or ""
        return [
            {"component": "VForm", "content": [
                self.__row([
                    self.__col(4, {"component": "VSwitch",
                                   "props": {"model": "enabled", "label": "启用插件"}}),
                    self.__col(4, {"component": "VSwitch",
                                   "props": {"model": "notify", "label": "发送通知"}}),
                ]),
                self.__col(12, {"component": "VTextarea", "props": {
                    "model": "cookie", "label": "思齐 Cookie", "rows": 3,
                    "placeholder": "填写包含 c_secure_pass 的完整 Cookie",
                    "hint": "留空时读取「设定 → 站点」中 si-qi.xyz 的 Cookie"}}),
                {"component": "VDivider", "props": {"class": "my-2"}},
                {"component": "div", "props": {"class": "text-h6 mb-2"},
                 "text": "🧱 搬砖（站点每天上限 50 块，搬满当日自动结束、次日恢复）"},
                self.__row([
                    self.__col(3, {"component": "VSwitch", "props": {
                        "model": "brick_enabled", "label": "启用搬砖"}}),
                    self.__col(3, {"component": "VTextField", "props": {
                        "model": "brick_daily_time", "label": "每天搬砖时间", "type": "time",
                        "hint": "每天这个时刻把 50 块搬完"}}),
                    self.__col(3, {"component": "VSwitch", "props": {
                        "model": "brick_run_once", "label": "立即搬砖一次",
                        "hint": "保存后执行并自动关闭"}}),
                ]),
                {"component": "VDivider", "props": {"class": "my-2"}},
                {"component": "div", "props": {"class": "text-h6 mb-2"},
                 "text": "🏖️ 沙滩捡破烂（站点冷却 2 小时，默认每 121 分钟一次）"},
                self.__row([
                    self.__col(3, {"component": "VSwitch", "props": {
                        "model": "beach_enabled", "label": "启用捡破烂"}}),
                    self.__col(3, {"component": "VTextField", "props": {
                        "model": "beach_interval_minutes", "label": "间隔分钟数", "type": "number",
                        "min": 1,
                        "hint": "站点冷却 120 分钟，121 保证每轮都能清理"}}),
                    self.__col(3, {"component": "VSwitch", "props": {
                        "model": "craft_enabled", "label": "同步炼造魔丸",
                        "hint": "开＝每轮捡破烂后炼造（砖块优先保魔丸链）；关＝只囤不炼"}}),
                    self.__col(3, {"component": "VSwitch", "props": {
                        "model": "beach_run_once", "label": "立即捡破烂一次",
                        "hint": "保存后执行并自动关闭"}}),
                ]),
                self.__row([
                    self.__col(3, {"component": "VTextField", "props": {
                        "model": "craft_keep_bricks", "label": "炼造预留砖块", "type": "number",
                        "min": 0, "hint": "这部分砖块不参与炼造"}}),
                    self.__col(3, {"component": "VTextField", "props": {
                        "model": "craft_max_items_per_run", "label": "单次炼造总量上限", "type": "number",
                        "min": 1, "hint": "一轮最多炼造多少件物品"}}),
                ]),
                {"component": "VDivider", "props": {"class": "my-2"}},
                {"component": "div", "props": {"class": "text-h6 mb-2"},
                 "text": "💰 魔丸兑换魔力（1 魔丸 = 1000 魔力，每天一次）"},
                self.__row([
                    self.__col(3, {"component": "VSwitch", "props": {
                        "model": "exchange_enabled", "label": "启用兑换"}}),
                    self.__col(3, {"component": "VTextField", "props": {
                        "model": "exchange_daily_time", "label": "每天兑换时间", "type": "time",
                        "hint": "建议排在炼造之后"}}),
                    self.__col(3, {"component": "VTextField", "props": {
                        "model": "exchange_quantity", "label": "兑换几颗魔丸", "type": "number",
                        "min": 0,
                        "hint": "自选数量；填 0 = 有多少兑多少（单次上限 100）"}}),
                    self.__col(3, {"component": "VSwitch", "props": {
                        "model": "exchange_run_once", "label": "立即兑换一次",
                        "hint": "保存后执行并自动关闭"}}),
                ]),
            ]},
        ], {
            "enabled": self._enabled,
            "cookie": cookie_value,
            "notify": self._notify,
            "brick_enabled": self._brick_enabled,
            "brick_daily_time": self._brick_daily_time,
            "brick_run_once": False,
            "beach_enabled": self._beach_enabled,
            "beach_interval_minutes": self._beach_interval_minutes,
            "beach_run_once": False,
            "craft_enabled": self._craft_enabled,
            "craft_keep_bricks": self._craft_keep_bricks,
            "craft_max_items_per_run": self._craft_max_items_per_run,
            "exchange_enabled": self._exchange_enabled,
            "exchange_daily_time": self._exchange_daily_time,
            "exchange_quantity": self._exchange_quantity,
            "exchange_run_once": False,
            "brick_stats": self._brick_stats,
            "beach_stats": self._beach_stats,
            "craft_stats": self._craft_stats,
            "exchange_stats": self._exchange_stats,
            "game_stats": self._game_stats,
            "stats": self._stats,
            "inventory": self._inventory,
        }

    @staticmethod
    def __row(content: List[dict]) -> Dict[str, Any]:
        return {"component": "VRow", "content": content}

    @staticmethod
    def __col(cols: int, content: dict) -> Dict[str, Any]:
        return {"component": "VCol", "props": {"cols": 12, "md": cols}, "content": [content]}

    # ------------------------------------------------------------ 数据面板
    def get_page(self) -> List[dict]:
        stats = self._stats or {}
        game = self._game_stats or {}
        brick = self._brick_stats or {}
        beach = self._beach_stats or {}
        craft = self._craft_stats or {}
        exchange = self._exchange_stats or {}

        last_update = self.__format_ts(stats.get("last_update"))
        game_update = self.__format_ts(game.get("last_update"))

        daily_bricks = game.get("daily_bricks")
        if daily_bricks is None:
            daily_bricks = brick.get("daily")
        limit = game.get("daily_limit") or self._brick_daily_limit
        points = game.get("points")
        bonus = game.get("bonus_earned")
        pills = game.get("magic_pills")
        if pills is None:
            pills = (self._inventory or {}).get("魔丸")

        ready, left = self.__beach_status({
            "last_beach_time": game.get("last_beach_time"),
            "beach_interval": game.get("beach_interval")})
        if game.get("last_beach_time") is None:
            beach_state = "未知（尚未取到游戏状态）"
        elif ready:
            beach_state = "✅ 可以进入清理"
        else:
            beach_state = f"⏳ 冷却中，{self.__format_left(left)}后可清理"

        return [
            {"component": "VCard", "props": {"variant": "tonal", "class": "mb-4"},
             "content": [
                 {"component": "VCardTitle", "text": f"⚗️ {self.plugin_name} · 数据面板"},
                 {"component": "VCardText", "content": [
                     {"component": "VRow", "props": {"class": "mb-2"}, "content": [
                         self.__stat_card("💰", "魔力值", f"{points:,}" if points is not None else "-", "", "orange"),
                         self.__stat_card("🎁", "已兑换魔力", f"{bonus:,}" if bonus is not None else "-", "", "purple"),
                         self.__stat_card("⚗️", "当前魔丸", f"{pills:,}" if pills is not None else "-", "颗", "deep-purple"),
                         self.__stat_card("🧱", "今日搬砖", f"{daily_bricks if daily_bricks is not None else '-'}/{limit}", "", "red"),
                     ]},
                     {"component": "VRow", "content": [
                         self.__info_col("🏖️ 沙滩状态", beach_state),
                         self.__info_col("🪱 捡破烂", "已启用" if self._beach_enabled else "未启用"),
                         self.__info_col("🛠️ 炼造", "与沙滩同步" if self._craft_enabled else "只囤不炼"),
                         self.__info_col("💰 兑换", f"每次 {self._exchange_quantity or '全部'}" if self._exchange_enabled else "未启用"),
                         self.__info_col("📈 发种等级", f"Lv.{game.get('level')}" if game.get("level") else "-"),
                         self.__info_col("🌊 HNR", game.get("hnr") or "-"),
                         self.__info_col("🔔 通知", "开启" if self._notify else "关闭"),
                         self.__info_col("🍪 Cookie", "已配置" if self._cookie else "未配置"),
                         self.__info_col("🕐 游戏数据更新", game_update or "-"),
                         self.__info_col("📝 最后结果", (stats.get("last_result") or "-")[:28]),
                     ]},
                 ]},
             ]},
            {"component": "VCard", "props": {"variant": "tonal", "class": "mb-4"}, "content": [
                {"component": "VCardTitle", "text": "🛠️ 炼造工坊（砖块优先保魔丸链）"},
                {"component": "VCardText", "content": [
                    {"component": "VTable", "props": {"hover": True, "dense": True}, "content": [
                        {"component": "thead", "content": [{"component": "tr", "content": [
                            {"component": "th", "text": "产物"}, {"component": "th", "text": "材料（持有/需要）"},
                            {"component": "th", "text": "最多可炼"}]}]},
                        {"component": "tbody", "content": self.__recipe_rows()},
                    ]},
                ]},
            ]},
            {"component": "VCard", "props": {"variant": "tonal", "class": "mb-4"}, "content": [
                {"component": "VCardTitle", "text": "🎒 物品栏"},
                {"component": "VCardText", "content": [
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12}, "content": [
                            {"component": "div", "props": {"style": "display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 16px;"},
                             "content": self.__inventory_items() or [
                                 {"component": "div", "props": {"class": "text-center text-medium-emphasis pa-4"},
                                  "text": "🏝️ 暂无物品数据（等待首次执行）"}]},
                        ]},
                    ]},
                ]},
            ]},
            {"component": "VCard", "props": {"variant": "tonal", "class": "mb-4"}, "content": [
                {"component": "VCardTitle", "text": "📌 任务状态"},
                {"component": "VCardText", "content": [
                    {"component": "VRow", "content": [
                        self.__info_col("🧱 搬砖定时", self.__schedule_text("brick")),
                        self.__info_col("🧱 搬砖结果", str(brick.get("last_result") or "-")[:28]),
                        self.__info_col("🏖️ 捡破烂定时", self.__schedule_text("beach")),
                        self.__info_col("🏖️ 捡破烂结果", str(beach.get("last_result") or "-")[:28]),
                        self.__info_col("🛠️ 炼造结果", str(craft.get("last_result") or "-")[:28]),
                        self.__info_col("🛠️ 累计炼造", f"{craft.get('total_crafted', 0)} 件"),
                        self.__info_col("💰 兑换定时", self.__schedule_text("exchange")),
                        self.__info_col("💰 累计兑换", f"{exchange.get('total_exchanged', 0)} 颗 / {exchange.get('total_points', 0):,} 魔力"),
                        self.__info_col("🧱 累计搬砖", f"{brick.get('total_moved', 0)} 块"),
                        self.__info_col("🪱 累计捡破烂", f"{beach.get('total_collected', 0)} 件"),
                        self.__info_col("🕐 最后执行", last_update or "-"),
                    ]},
                ]},
            ]},
            {"component": "VCard", "props": {"variant": "tonal"}, "content": [
                {"component": "VCardTitle", "text": "📋 执行记录"},
                {"component": "VCardText", "content": [
                    {"component": "VTable", "props": {"hover": True, "dense": True}, "content": [
                        {"component": "thead", "content": [{"component": "tr", "content": [
                            {"component": "th", "text": "时间"}, {"component": "th", "text": "结果"},
                            {"component": "th", "text": "数量"}, {"component": "th", "text": "状态"}]}]},
                        {"component": "tbody", "content": self.__history_rows(stats.get("history", [])[-8:]) or [
                            {"component": "tr", "content": [
                                {"component": "td", "props": {"colspan": 4, "class": "text-center text-medium-emphasis"},
                                 "text": "暂无执行记录"}]}]},
                    ]},
                ]},
            ]},
        ]

    def __recipe_rows(self) -> List[Dict[str, Any]]:
        inventory = self._inventory or {}
        rows = []
        for recipe_id in self.CRAFT_ORDER:
            recipe = self.RECIPES[recipe_id]
            materials = []
            max_craftable = None
            for name, need in recipe["ingredients"].items():
                have = int(inventory.get(name, 0) or 0)
                enough = have >= need
                materials.append(
                    f"{self.ITEM_ICONS.get(name, '📦')}{name}: "
                    f"{have}/{need}")
                step = have // need if need else 0
                max_craftable = step if max_craftable is None else min(max_craftable, step)
                if not enough:
                    pass
            max_craftable = min(max_craftable or 0, self.REQUEST_ITEM_CAP)
            rows.append({"component": "tr", "content": [
                {"component": "td", "text": f"{self.ITEM_ICONS.get(recipe['result'], '📦')} {recipe['result']}"},
                {"component": "td", "text": " + ".join(materials)},
                {"component": "td", "content": [
                    {"component": "VChip", "props": {
                        "color": "success" if max_craftable > 0 else "grey",
                        "size": "small", "variant": "tonal"},
                     "text": f"最多 {max_craftable}" if max_craftable > 0 else "材料不足"}]},
            ]})
        return rows

    def __inventory_items(self) -> List[Dict[str, Any]]:
        items = []
        for name, count in sorted((self._inventory or {}).items(),
                                  key=lambda kv: -int(kv[1] or 0)):
            try:
                count = int(count or 0)
            except (TypeError, ValueError):
                count = 0
            color = self.ITEM_COLORS.get(name, "#3498db")
            dark = color in ("#2c3e50", "#7f8c8d", "#8e44ad", "#d4a574")
            items.append({"component": "div", "props": {"style": f"""
                        background: linear-gradient(145deg, {color}25, {color}08);
                        border-radius: 16px; padding: 20px 16px; text-align: center;
                        border: 1px solid {color}30; box-shadow: 0 2px 8px rgba(0,0,0,0.08);
                    """}, "content": [
                {"component": "div", "props": {"style": "font-size: 40px; margin-bottom: 6px;"},
                 "text": self.ITEM_ICONS.get(name, "📦")},
                {"component": "div", "props": {"style": f"font-size: 14px; font-weight: 600; color: {'#ffffff' if dark else '#2c3e50'};"},
                 "text": name},
                {"component": "div", "props": {"style": f"font-size: 24px; font-weight: bold; color: {color};"},
                 "text": f"{count:,}"},
            ]})
        return items

    @staticmethod
    def __stat_card(icon: str, label: str, value: str, suffix: str, color: str) -> Dict[str, Any]:
        return {"component": "VCol", "props": {"cols": 6, "md": 3}, "content": [
            {"component": "VCard", "props": {"color": color, "variant": "tonal", "class": "pa-3"},
             "content": [{"component": "div", "props": {"class": "d-flex align-center"}, "content": [
                 {"component": "div", "props": {"class": "text-h4 mr-2"}, "text": icon},
                 {"component": "div", "content": [
                     {"component": "div", "props": {"class": "text-caption text-medium-emphasis"}, "text": label},
                     {"component": "div", "props": {"class": "text-h6 font-weight-bold"}, "text": f"{value}{suffix}"},
                 ]},
             ]}]}]}

    @staticmethod
    def __info_col(label: str, value: Any) -> Dict[str, Any]:
        return {"component": "VCol", "props": {"cols": 6, "md": 3}, "content": [
            {"component": "div", "props": {"class": "text-caption text-medium-emphasis"}, "text": label},
            {"component": "div", "props": {"class": "text-body-1"}, "text": str(value if value not in (None, "") else "-")},
        ]}

    def __history_rows(self, history: list) -> List[Dict[str, Any]]:
        rows = []
        for record in reversed(history or []):
            rows.append({"component": "tr", "content": [
                {"component": "td", "text": self.__format_ts(record.get("time"))},
                {"component": "td", "text": str(record.get("result", "-"))[:40]},
                {"component": "td", "text": f"{record.get('total_count', 0):,}"},
                {"component": "td", "content": [{"component": "VChip", "props": {
                    "color": "success" if record.get("status") == "completed" else "warning",
                    "size": "small", "variant": "tonal"},
                    "text": "✅ 成功" if record.get("status") == "completed" else "⚠️ 未完成"}]},
            ]})
        return rows

    def __schedule_text(self, kind: str) -> str:
        """每个任务只有一种定时方式：搬砖/兑换=每天固定时间，捡破烂=固定间隔。

        额外带上内部定时器里的下次执行时间，页面上一眼能看出任务是否真的挂上了。
        """
        if not getattr(self, f"_{kind}_enabled", False):
            return "未启用"
        if kind == "beach":
            base = f"每 {self._beach_interval_minutes} 分钟"
        elif kind == "exchange":
            base = f"每天 {self._exchange_daily_time}"
        else:
            base = f"每天 {self._brick_daily_time}"
        scheduler = self._scheduler
        job = scheduler.get_job(self.JOB_IDS.get(kind, "")) if scheduler else None
        next_run = getattr(job, "next_run_time", None) if job else None
        if next_run:
            return f"{base}（下次 {self.__fmt_next_run(next_run)}）"
        return base

    # ------------------------------------------------------------ 对外接口
    def run_brick_api(self) -> Dict[str, Any]:
        if self._brick_lock.locked():
            return {"success": False, "message": "搬砖任务正在执行"}
        threading.Thread(target=self.run_brick_task, daemon=True).start()
        return {"success": True, "message": "搬砖任务已开始，完成后会发送通知"}

    def run_beach_api(self) -> Dict[str, Any]:
        if self._beach_lock.locked():
            return {"success": False, "message": "捡破烂任务正在执行"}
        threading.Thread(target=self.run_beach_task, daemon=True).start()
        return {"success": True, "message": "捡破烂任务已开始，完成后会发送通知"}

    def run_exchange_api(self) -> Dict[str, Any]:
        if self._exchange_lock.locked():
            return {"success": False, "message": "兑换任务正在执行"}
        threading.Thread(target=self.run_exchange_task, daemon=True).start()
        return {"success": True, "message": "兑换任务已开始，完成后会发送通知"}

    def get_stats_api(self) -> Dict[str, Any]:
        return {"success": True, "data": {
            "game": self._game_stats, "brick": self._brick_stats,
            "beach": self._beach_stats, "craft": self._craft_stats,
            "exchange": self._exchange_stats, "inventory": self._inventory,
            "history": self._stats.get("history", []),
        }}

    @eventmanager.register(EventType.PluginAction)
    def on_plugin_action(self, event: Event = None):
        event_data = event.event_data if event else {}
        action = (event_data or {}).get("action")
        mapping = {
            "siqi_brick": (self._brick_lock, self.run_brick_task, "搬砖"),
            "siqi_beach": (self._beach_lock, self.run_beach_task, "捡破烂"),
            "siqi_exchange": (self._exchange_lock, self.run_exchange_task, "兑换魔力"),
        }
        if action not in mapping:
            return
        lock, func, label = mapping[action]
        channel = event_data.get("channel")
        userid = event_data.get("user")
        if lock.locked():
            self.post_message(channel=channel, userid=userid, mtype=MessageType.Plugin,
                              title=f"【{self.plugin_name}】",
                              text=f"已有{label}任务正在执行，请等待当前任务结束。")
            return
        logger.info(f"收到命令请求，后台启动{label}任务")
        threading.Thread(target=func, daemon=True).start()
        self.post_message(channel=channel, userid=userid, mtype=MessageType.Plugin,
                          title=f"【{self.plugin_name}】",
                          text=f"{label}任务已开始，完成后会发送通知。")

    # ------------------------------------------------------------ 任务：搬砖
    def run_brick_task(self) -> Dict[str, Any]:
        """循环搬砖直到达到上限或被服务端拒绝。

        站点自己按日计数（每天 50 块），因此不做任何盲重试：请求超时或异常时结果
        未知，立即停止并如实报告；服务端已满视为正常结束。
        """
        if not self._brick_lock.acquire(blocking=False):
            return {"status": "running", "message": "已有搬砖任务正在执行"}

        try:
            cookie = self.__cookie()
            if not cookie:
                result = self.__new_result("auth_failed", "缺少包含 c_secure_pass 的思齐 Cookie")
                self.__finish("brick", result)
                return result

            limit = max(1, self._brick_daily_limit)
            interval = max(0, self._brick_interval_ms) / 1000.0

            state = self.__fetch_game_state(cookie)
            if state:
                self.__update_game_stats(state)
                if state.get("daily_bricks") is not None:
                    self._brick_stats["daily"] = state["daily_bricks"]
            before = self._brick_stats.get("daily") or 0

            moved = 0
            status = "completed"
            message = ""
            stopped_by = ""

            if before >= limit:
                message = f"今日搬砖已达上限（{before}/{limit}），无需执行"
                stopped_by = "daily_limit"
            else:
                for _ in range(limit - before):
                    outcome = self.__move_brick(cookie)
                    if outcome is None:
                        message = f"搬砖请求超时或异常，结果未知，已停止（本轮成功 {moved} 次，未重试）"
                        status = "unknown" if moved == 0 else "partial"
                        stopped_by = "unknown"
                        break
                    if not outcome["ok"]:
                        message = outcome["message"] or "服务端拒绝，未知原因"
                        if outcome.get("daily") is not None:
                            self._brick_stats["daily"] = outcome["daily"]
                        at_limit = ("上限" in message) or (
                            (self._brick_stats.get("daily") or 0) >= limit)
                        if at_limit:
                            status = "completed"
                            stopped_by = "limit"
                        else:
                            status = "partial" if moved > 0 else "failed"
                            stopped_by = "refused"
                        break
                    moved += outcome.get("moved") or 1
                    if outcome.get("daily") is not None:
                        self._brick_stats["daily"] = outcome["daily"]
                    reset_day = (outcome.get("data") or {}).get("last_daily_reset")
                    if reset_day:
                        self._brick_stats["last_reset"] = str(reset_day)
                    if (self._brick_stats.get("daily") or 0) >= limit:
                        message = f"今日搬砖已达上限（{self._brick_stats['daily']}/{limit}）"
                        stopped_by = "limit"
                        break
                    time.sleep(interval)

            daily_now = self._brick_stats.get("daily")
            if daily_now is None:
                daily_now = before + moved
                self._brick_stats["daily"] = daily_now
            self._brick_stats["available"] = max(0, self.BRICK_DAILY_LIMIT - int(daily_now))
            if not message:
                message = f"本轮搬砖 {moved} 次，今日 {daily_now}/{limit}"
                stopped_by = stopped_by or "quota_reached"

            self._brick_stats["total_moved"] = (self._brick_stats.get("total_moved") or 0) + moved
            self._brick_stats["last_update"] = time.time()
            self._brick_stats["last_result"] = message[:60]
            self._game_stats["daily_bricks"] = daily_now
            self._game_stats["daily_limit"] = limit

            logger.info(f"搬砖结束：{status}，本轮 {moved} 次，今日 {daily_now}/{limit}，原因 {stopped_by}")
            result = self.__new_result(status, message)
            result["data"] = {"moved": moved, "daily": daily_now, "limit": limit,
                              "available": self._brick_stats["available"],
                              "stopped_by": stopped_by}
            self.__finish("brick", result)
            return result
        except Exception as e:
            logger.error(f"搬砖任务异常：{e}")
            import traceback
            logger.error(traceback.format_exc())
            result = self.__new_result("error", str(e))
            self.__finish("brick", result)
            return result
        finally:
            self._brick_lock.release()

    # ------------------------------------------------------------ 任务：捡破烂 + 炼造
    def run_beach_task(self) -> Dict[str, Any]:
        """清沙滩 → 一键收集 →（开关打开时）同轮炼造。

        站点冷却写在 gameData 里（beachInterval，默认 7200 秒）。冷却未到时**不发
        任何请求**，只记录下次可清理时间；沙滩已有垃圾时直接收集，不再浪费一次进入。
        """
        if not self._beach_lock.acquire(blocking=False):
            return {"status": "running", "message": "已有捡破烂任务正在执行"}

        try:
            cookie = self.__cookie()
            if not cookie:
                result = self.__new_result("auth_failed", "缺少包含 c_secure_pass 的思齐 Cookie")
                self.__finish("beach", result)
                return result

            state = self.__fetch_game_state(cookie)
            if not state:
                result = self.__new_result(
                    "unknown", "无法获取游戏状态（页面取回失败或解析失败），本轮未发任何请求")
                self.__finish("beach", result)
                return result
            self.__update_game_stats(state)

            ready, left = self.__beach_status(state)
            trash_items = state.get("trash_items") or []
            entered = False
            collected: Dict[str, int] = {}
            status = "completed"
            message = ""
            notes: List[str] = []
            should_collect = False

            # 站点规范流程是两步：先「清理沙滩」(enter_beach)，再「一键收集」(collect_all_trash)。
            # 冷却已过就先重新进入，即使沙滩上还留着上次没收集的垃圾。
            if ready:
                enter = self.__enter_beach(cookie)
                if enter is None:
                    result = self.__new_result("unknown", "进入沙滩超时或异常，结果未知，已停止（未重试）")
                    result["data"] = {"entered": False, "collected": {}, "collected_total": 0}
                    self.__finish("beach", result)
                    return result
                if enter["ok"]:
                    entered = True
                    new_trash = (enter.get("data") or {}).get("trash_items")
                    if isinstance(new_trash, list):
                        # 与页面一致：沙滩上没有垃圾时「一键收集」是置灰的，不该发请求
                        should_collect = bool(new_trash)
                        if not new_trash:
                            message = "已清理沙滩，本轮沙滩上没有垃圾"
                    else:
                        should_collect = True
                else:
                    message = enter["message"] or "进入沙滩失败"
                    if trash_items:
                        notes.append(f"清理沙滩未成功（{message}），改为收集沙滩上已有的垃圾")
                        should_collect = True
                    else:
                        status = "completed" if "冷却" in message else "failed"
            elif trash_items:
                # 用户截图里的状态：清理沙滩置灰（冷却中），但一键收集可用
                notes.append(f"沙滩冷却中（{self.__format_left(left)}后可清理），先收集沙滩上已有的垃圾")
                should_collect = True
            else:
                message = f"沙滩冷却中，{self.__format_left(left)}后可清理，本轮未发请求"

            if should_collect:
                outcome = self.__collect_all_trash(cookie)
                if outcome is None:
                    result = self.__new_result(
                        "unknown", "一键收集超时或异常，结果未知，已停止（未重试）")
                    result["data"] = {"entered": entered, "collected": {}, "collected_total": 0}
                    self.__finish("beach", result)
                    return result
                collected = self.__collected_items(outcome)
                message = outcome["message"] or f"收集完成：{sum(collected.values())} 件"
                if not outcome["ok"]:
                    status = "failed"

            if notes:
                message = "；".join(notes + ([message] if message else []))

            total_collected = sum(int(v or 0) for v in collected.values())
            if collected:
                for name, count in collected.items():
                    self._inventory[name] = int(self._inventory.get(name, 0) or 0) + int(count or 0)
            self._beach_stats["total_collected"] = (
                self._beach_stats.get("total_collected") or 0) + total_collected
            self._beach_stats["collected"] = collected
            self._beach_stats["last_update"] = time.time()
            self._beach_stats["last_result"] = message[:60]

            data: Dict[str, Any] = {"entered": entered, "collected": collected,
                                    "collected_total": total_collected,
                                    "next_clean_ts": self.__next_clean_ts(state, entered)}

            # 炼造：与沙滩同轮执行（开关关闭则只囤不炼）。收集后库存变了，重取一次状态。
            if self._craft_enabled:
                fresh = state
                if entered or total_collected > 0:
                    fresh = self.__fetch_game_state(cookie) or state
                    self.__update_game_stats(fresh)
                craft_result = self.run_craft_step(fresh)
                data["craft"] = craft_result.get("data") or {}
                data["craft"]["status"] = craft_result.get("status")
                # 炼造会消耗大量材料：重取一次权威状态，别让面板停留在炼造前的快照
                if (craft_result.get("data") or {}).get("total"):
                    after = self.__fetch_game_state(cookie, force=True)
                    if after:
                        self.__update_game_stats(after)
                if status == "completed" and craft_result.get("status") in ("failed", "partial", "unknown"):
                    status = "partial"
                    message = f"{message}；炼造：{craft_result.get('message')}"

            result = self.__new_result(status, message)
            result["data"] = data
            logger.info(f"捡破烂结束：{status}，{message}")
            self.__finish("beach", result)
            return result
        except Exception as e:
            logger.error(f"捡破烂任务异常：{e}")
            import traceback
            logger.error(traceback.format_exc())
            result = self.__new_result("error", str(e))
            self.__finish("beach", result)
            return result
        finally:
            self._beach_lock.release()

    def run_craft_step(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """按「砖块优先保魔丸链」按需炼造：只炼够凑出魔丸的量，不囤中间件。"""
        if not self._craft_enabled:
            return {"status": "skipped", "message": "炼造开关关闭（只囤不炼）",
                    "data": {"crafted": {}, "total": 0}}
        inventory = (state or {}).get("inventory") or self._inventory or {}
        plan = self.__craft_plan(inventory)
        if not plan:
            return {"status": "completed", "message": "材料不足以炼造，未发请求",
                    "data": {"crafted": {}, "total": 0}}
        return self.__execute_craft_plan(plan)

    def __execute_craft_plan(self, plan: Dict[int, int]) -> Dict[str, Any]:
        cookie = self.__cookie()
        if not cookie:
            return {"status": "auth_failed", "message": "缺少包含 c_secure_pass 的思齐 Cookie",
                    "data": {"crafted": {}, "total": 0}}

        crafted: Dict[str, int] = {}
        total = 0
        for recipe_id in self.CRAFT_ORDER:
            remaining = int(plan.get(recipe_id, 0) or 0)
            if remaining <= 0:
                continue
            name = self.RECIPES[recipe_id]["result"]
            while remaining > 0:
                chunk = min(remaining, self.REQUEST_ITEM_CAP)
                outcome = self.__craft_item(cookie, recipe_id, chunk)
                if outcome is None:
                    message = f"炼造 {name} 时请求超时或异常，结果未知，已停止（未重试）"
                    result = self.__new_result("unknown" if total == 0 else "partial", message)
                    result["data"] = {"crafted": crafted, "total": total}
                    self.__record_craft(result)
                    return result
                if not outcome["ok"]:
                    message = outcome["message"] or f"炼造 {name} 被服务端拒绝"
                    result = self.__new_result("partial" if total > 0 else "failed", message)
                    result["data"] = {"crafted": crafted, "total": total}
                    self.__record_craft(result)
                    return result
                crafted[name] = crafted.get(name, 0) + chunk
                total += chunk
                remaining -= chunk

        message = f"炼造完成：{total} 件（{', '.join(f'{k}×{v}' for k, v in crafted.items())}）"
        result = self.__new_result("completed", message)
        result["data"] = {"crafted": crafted, "total": total}
        self.__record_craft(result)
        return result

    def __record_craft(self, result: Dict[str, Any]):
        data = result.get("data") or {}
        self._craft_stats["crafted"] = data.get("crafted", {})
        self._craft_stats["total_crafted"] = (self._craft_stats.get("total_crafted") or 0) + int(data.get("total", 0))
        self._craft_stats["last_update"] = time.time()
        self._craft_stats["last_result"] = str(result.get("message"))[:60]
        logger.info(f"炼造：{result.get('status')}，{result.get('message')}")

    # ------------------------------------------------------------ 任务：兑换魔力
    def run_exchange_task(self) -> Dict[str, Any]:
        """把魔丸兑换为魔力值。数量自选（0 = 有多少兑多少），单次请求上限 100。

        兑换不可逆：请求超时或异常时结果未知，立即停止且不重试。
        """
        if not self._exchange_lock.acquire(blocking=False):
            return {"status": "running", "message": "已有兑换任务正在执行"}

        try:
            cookie = self.__cookie()
            if not cookie:
                result = self.__new_result("auth_failed", "缺少包含 c_secure_pass 的思齐 Cookie")
                self.__finish("exchange", result)
                return result

            state = self.__fetch_game_state(cookie)
            if not state:
                result = self.__new_result("unknown", "无法获取游戏状态，本轮未发任何请求")
                self.__finish("exchange", result)
                return result
            self.__update_game_stats(state)

            have = int((state.get("inventory") or {}).get("魔丸", 0) or 0)
            want = int(self._exchange_quantity or 0)
            if want <= 0:
                want = have
            want = min(want, have)

            exchanged = 0
            gained = 0
            requests_made = 0
            status = "completed"
            message = ""

            if want <= 0:
                message = f"没有可兑换的魔丸（当前 {have} 颗），本轮未发请求"
            else:
                while want > 0:
                    chunk = min(want, self.REQUEST_ITEM_CAP)
                    outcome = self.__exchange_points(cookie, chunk)
                    requests_made += 1
                    if outcome is None:
                        message = f"兑换请求超时或异常，结果未知，已停止（已兑换 {exchanged} 颗，未重试）"
                        status = "unknown" if exchanged == 0 else "partial"
                        break
                    if not outcome["ok"]:
                        message = outcome["message"] or "兑换被服务端拒绝"
                        status = "partial" if exchanged > 0 else "failed"
                        break
                    gained += int(outcome["data"].get("points_gained") or 0)
                    exchanged += chunk
                    want -= chunk
                if not message:
                    message = f"兑换 {exchanged} 颗魔丸，获得 {gained:,} 魔力值"

            self._exchange_stats["total_exchanged"] = (
                self._exchange_stats.get("total_exchanged") or 0) + exchanged
            self._exchange_stats["total_points"] = (
                self._exchange_stats.get("total_points") or 0) + gained
            self._exchange_stats["last_update"] = time.time()
            self._exchange_stats["last_result"] = message[:60]
            if exchanged:
                self._inventory["魔丸"] = max(0, have - exchanged)
                self._game_stats["magic_pills"] = self._inventory["魔丸"]
                if self._game_stats.get("points") is not None:
                    self._game_stats["points"] = int(self._game_stats["points"]) + gained

            logger.info(f"兑换结束：{status}，{message}")
            result = self.__new_result(status, message)
            result["data"] = {"exchanged": exchanged, "points_gained": gained,
                              "remaining": max(0, have - exchanged), "requests": requests_made}
            self.__finish("exchange", result)
            return result
        except Exception as e:
            logger.error(f"兑换任务异常：{e}")
            import traceback
            logger.error(traceback.format_exc())
            result = self.__new_result("error", str(e))
            self.__finish("exchange", result)
            return result
        finally:
            self._exchange_lock.release()

    # ------------------------------------------------------------ 炼造计划
    def __craft_plan(self, inventory: Dict[str, Any]) -> Dict[int, int]:
        """算出「为了凑出尽可能多的魔丸」每个配方要炼多少次（只炼需要的量）。"""
        inv: Dict[str, int] = {}
        for name, count in (inventory or {}).items():
            try:
                inv[str(name)] = int(count or 0)
            except (TypeError, ValueError):
                inv[str(name)] = 0
        inv["砖块"] = max(0, inv.get("砖块", 0) - max(0, self._craft_keep_bricks))

        max_units = max(1, int(self._craft_max_items_per_run) // self.ITEMS_PER_MOWAN)
        plan: Dict[int, int] = {}
        units = 0
        while units < max_units:
            trial = dict(inv)
            unit_plan: Dict[int, int] = {}
            if not self.__ensure_ingredients(self.MOWAN_RECIPE_ID, 1, trial, unit_plan):
                break
            unit_plan[self.MOWAN_RECIPE_ID] = unit_plan.get(self.MOWAN_RECIPE_ID, 0) + 1
            inv = trial
            for recipe_id, qty in unit_plan.items():
                plan[recipe_id] = plan.get(recipe_id, 0) + qty
            units += 1
        return {rid: qty for rid, qty in plan.items() if qty > 0}

    def __ensure_ingredients(self, recipe_id: int, times: int,
                             inv: Dict[str, int], plan: Dict[int, int]) -> bool:
        recipe = self.RECIPES.get(int(recipe_id))
        if not recipe:
            return False
        for name, per in recipe["ingredients"].items():
            if not self.__ensure_item(name, int(per) * int(times), inv, plan):
                return False
        return True

    def __ensure_item(self, name: str, qty: int, inv: Dict[str, int],
                      plan: Dict[int, int], depth: int = 0) -> bool:
        if qty <= 0:
            return True
        if depth > 8:
            return False
        have = int(inv.get(name, 0) or 0)
        if have >= qty:
            inv[name] = have - qty
            return True
        recipe_id = self.__recipe_of(name)
        if recipe_id is None:
            return False
        missing = qty - have
        if not self.__ensure_ingredients(recipe_id, missing, inv, plan):
            return False
        inv[name] = 0
        plan[recipe_id] = plan.get(recipe_id, 0) + missing
        return True

    def __recipe_of(self, name: str) -> Optional[int]:
        for recipe_id, recipe in self.RECIPES.items():
            if recipe["result"] == name:
                return recipe_id
        return None

    # ------------------------------------------------------------ 站点请求
    def __headers(self, form: bool = True) -> Dict[str, str]:
        headers = {
            "accept": "*/*" if form else "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            "user-agent": self.USER_AGENT,
        }
        if form:
            headers["content-type"] = "application/x-www-form-urlencoded"
            headers["origin"] = self.BASE_URL
            headers["referer"] = self.MOWAN_URL
        else:
            headers["cache-control"] = "max-age=0"
        return headers

    def __post_action(self, cookie: str, data: Dict[str, Any],
                      timeout: int = 10) -> Optional[Dict[str, Any]]:
        """发一次 action 请求。超时/异常返回 None（结果未知，调用方不得重试）。"""
        try:
            response = requests.post(self.MOWAN_URL, headers=self.__headers(True),
                                     cookies=self.__cookie_to_dict(cookie), data=data,
                                     timeout=timeout, verify=False)
            logger.debug(f"action={data.get('action')} 响应状态码：{response.status_code}")
            if response.status_code != 200:
                return {"ok": False, "message": f"HTTP {response.status_code}",
                        "daily": None, "moved": 0, "data": {}}
            return self.__parse_action_response(response.text)
        except requests.Timeout:
            logger.error(f"action={data.get('action')} 请求超时，结果未知")
            return None
        except Exception as e:
            logger.error(f"action={data.get('action')} 请求异常：{e}")
            return None

    def __move_brick(self, cookie: str) -> Optional[Dict[str, Any]]:
        return self.__post_action(cookie, {"action": "move_brick"})

    def __enter_beach(self, cookie: str) -> Optional[Dict[str, Any]]:
        return self.__post_action(cookie, {"action": "enter_beach"})

    def __collect_all_trash(self, cookie: str) -> Optional[Dict[str, Any]]:
        return self.__post_action(cookie, {"action": "collect_all_trash"}, timeout=15)

    def __craft_item(self, cookie: str, recipe_id: int, quantity: int) -> Optional[Dict[str, Any]]:
        return self.__post_action(cookie, {"action": "craft_item",
                                           "recipe_id": int(recipe_id),
                                           "quantity": int(quantity)}, timeout=15)

    def __exchange_points(self, cookie: str, quantity: int) -> Optional[Dict[str, Any]]:
        return self.__post_action(cookie, {"action": "exchange_points",
                                           "quantity": int(quantity)}, timeout=15)

    def __fetch_page(self, cookie: str) -> Optional[str]:
        """取一次 mowan.php，用于读 gameData / recipes 权威状态。"""
        try:
            response = requests.get(self.MOWAN_URL, headers=self.__headers(False),
                                    cookies=self.__cookie_to_dict(cookie),
                                    timeout=15, verify=False)
            logger.debug(f"获取游戏页面响应状态码：{response.status_code}")
            if response.status_code != 200:
                return None
            return response.text
        except requests.Timeout:
            logger.error("获取游戏页面超时")
            return None
        except Exception as e:
            logger.error(f"获取游戏页面异常：{e}")
            return None

    def __fetch_game_state(self, cookie: str,
                           force: bool = False) -> Optional[Dict[str, Any]]:
        """取一次页面状态；force=False 时 5 秒内复用上一次结果。

        三个任务同时被触发时（用户连点三个「立即」）各自取一次页面是浪费，
        而且并发取数会让日志和请求数翻倍；炼造这类会改库存的操作必须 force=True。
        """
        cache = self._state_cache or {}
        now = time.time()
        if (not force and cache.get("state") is not None
                and cache.get("cookie") == cookie
                and now - float(cache.get("ts") or 0.0) < self.STATE_CACHE_TTL):
            logger.debug("复用 5 秒内的游戏状态缓存")
            return cache["state"]

        html = self.__fetch_page(cookie)
        if not html:
            return None
        state = self.__parse_game_state(html)
        if not state:
            return None
        state["fetched_at"] = time.time()
        self._state_cache = {"ts": state["fetched_at"], "cookie": cookie, "state": state}
        return state

    # ------------------------------------------------------------ 解析
    @classmethod
    def __parse_game_state(cls, html: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(html, str) or not html.strip():
            return None
        data = cls.__extract_js_object(html, "gameData")
        if not isinstance(data, dict):
            return None

        inventory: Dict[str, int] = {}
        raw_inventory = data.get("inventory")
        if isinstance(raw_inventory, dict):
            for name, count in raw_inventory.items():
                try:
                    inventory[str(name)] = int(count or 0)
                except (TypeError, ValueError):
                    inventory[str(name)] = 0

        trash = data.get("currentTrashItems")
        state = {
            "daily_bricks": cls.__to_int(data.get("dailyBricks")),
            "points": cls.__to_int(data.get("points")),
            "bonus_earned": cls.__page_number(html, "bonusEarned"),
            "last_beach_time": cls.__to_int(data.get("lastBeachTime")),
            "beach_interval": cls.__to_int(data.get("beachInterval")),
            "next_brick_reset_ts": cls.__to_int(data.get("nextBrickResetTs")),
            "server_now": cls.__extract_server_now(html),
            "trash_items": trash if isinstance(trash, list) else [],
            "inventory": inventory,
            "recipes": cls.__parse_recipes(html) or dict(cls.RECIPES),
            "hnr": cls.__page_text_flat(html, r"HNR值\s*[（(]\s*当前[：:]\s*([\d.]+)\s*[)）]"),
            "level": cls.__page_text_flat(html, r"发种等级\s*[（(]\s*当前[：:]\s*Lv\.?\s*(\d+)\s*[)）]"),
        }
        return state

    @classmethod
    def __extract_js_object(cls, html: str, marker: str) -> Optional[Dict[str, Any]]:
        match = re.search(r"(?:let|var|const)\s+" + re.escape(marker) + r"\s*=\s*", html)
        if not match:
            return None
        text = html[match.end():]
        depth = 0
        start = None
        quote = None
        escaped = False
        for index, char in enumerate(text):
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
                continue
            if char in "\"'":
                quote = char
                continue
            if char == "{":
                if depth == 0:
                    start = index
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0 and start is not None:
                    return cls.__js_object_to_dict(text[start:index + 1])
        return None

    @staticmethod
    def __js_object_to_dict(body: str) -> Optional[Dict[str, Any]]:
        # serverTimeOffset 是 JS 表达式（server_now - Math.floor(Date.now()/1000)），
        # 单独取值，这里先抹平以免破坏 JSON 解析
        body = re.sub(r"(serverTimeOffset\s*:\s*)[^,}\n]+", r"\g<1>0", body)
        body = re.sub(r"([{,]\s*)([A-Za-z_$][\w$]*)\s*:", r'\1"\2":', body)
        body = re.sub(r",\s*([}\]])", r"\1", body)
        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None

    @classmethod
    def __parse_recipes(cls, html: str) -> Optional[Dict[int, Dict[str, Any]]]:
        match = re.search(r"(?:const|let|var)\s+recipes\s*=\s*", html)
        if not match:
            return None
        try:
            raw, _ = json.JSONDecoder().raw_decode(html[match.end():].lstrip())
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(raw, list):
            return None
        recipes: Dict[int, Dict[str, Any]] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                recipe_id = int(item.get("recipe_id"))
            except (TypeError, ValueError):
                continue
            ingredients = {}
            for ingredient in item.get("ingredients") or []:
                if not isinstance(ingredient, dict):
                    continue
                try:
                    ingredients[str(ingredient.get("name"))] = int(ingredient.get("quantity"))
                except (TypeError, ValueError):
                    continue
            recipes[recipe_id] = {"result": str(item.get("result_name") or ""),
                                  "ingredients": ingredients}
        return recipes or None

    @staticmethod
    def __extract_server_now(html: str) -> Optional[int]:
        match = re.search(r"serverTimeOffset\s*:\s*(\d+)\s*-\s*Math\.floor", html)
        return int(match.group(1)) if match else None

    @staticmethod
    def __page_number(html: str, element_id: str) -> Optional[int]:
        match = re.search(r'id="%s"[^>]*>([^<]+)<' % re.escape(element_id), html)
        if not match:
            return None
        text = match.group(1).strip().replace(",", "")
        return int(text) if text.isdigit() else None

    @staticmethod
    def __page_text(html: str, pattern: str) -> Optional[str]:
        match = re.search(pattern, html)
        return match.group(1) if match else None

    @classmethod
    def __page_text_flat(cls, html: str, pattern: str) -> Optional[str]:
        """先剥掉标签再匹配。

        页面把「发种等级」「HNR值」包在 <strong> 里（`HNR值</strong>（当前：4.5286）`），
        直接对 HTML 匹配会被标签打断，实测解析结果为 None。
        """
        text = re.sub(r"<[^>]+>", "", html or "")
        text = re.sub(r"\s+", " ", text)
        return cls.__page_text(text, pattern)

    @staticmethod
    def __parse_action_response(payload: Any) -> Dict[str, Any]:
        result: Dict[str, Any] = {"ok": False, "message": "", "daily": None,
                                  "moved": 0, "data": {}}
        if isinstance(payload, dict):
            data = payload
        else:
            try:
                data = json.loads(payload) if payload else None
            except (json.JSONDecodeError, TypeError, ValueError):
                data = None

        if not isinstance(data, dict):
            text = payload if isinstance(payload, str) else ""
            if "登录" in text or "cookie" in text.lower():
                result["message"] = "Cookie失效，请重新登录"
            else:
                result["message"] = "返回非JSON响应"
            return result

        result["data"] = data
        result["message"] = str(data.get("message") or "")
        result["ok"] = bool(data.get("success") is True or data.get("status") == "success")
        daily = data.get("daily_bricks")
        if isinstance(daily, (int, float)) and not isinstance(daily, bool):
            result["daily"] = int(daily)
        moved = data.get("bricks_moved")
        if isinstance(moved, (int, float)) and not isinstance(moved, bool):
            result["moved"] = int(moved)
        return result

    @staticmethod
    def __collected_items(outcome: Dict[str, Any]) -> Dict[str, int]:
        collected = (outcome.get("data") or {}).get("collected_items")
        if not isinstance(collected, dict):
            return {}
        result = {}
        for name, count in collected.items():
            try:
                value = int(count or 0)
            except (TypeError, ValueError):
                continue
            if value > 0:
                result[str(name)] = value
        return result

    def __beach_status(self, state: Dict[str, Any], now: Optional[float] = None) -> Tuple[bool, int]:
        """返回 (是否可清理, 剩余秒数)。取不到冷却数据时按「可以清理」处理。"""
        last = (state or {}).get("last_beach_time")
        interval = (state or {}).get("beach_interval")
        if last is None or interval is None:
            return True, 0
        if now is None:
            now = self.__server_now(state)
        left = int(interval) - int(now - last)
        return (True, 0) if left <= 0 else (False, left)

    def __server_now(self, state: Dict[str, Any]) -> float:
        server_now = (state or {}).get("server_now")
        fetched_at = (state or {}).get("fetched_at")
        if server_now is None or fetched_at is None:
            return time.time()
        return time.time() + (server_now - fetched_at)

    def __next_clean_ts(self, state: Dict[str, Any], entered: bool) -> Optional[int]:
        interval = (state or {}).get("beach_interval")
        if interval is None:
            return None
        if entered:
            return int(self.__server_now(state) + int(interval))
        last = (state or {}).get("last_beach_time")
        return int(int(last) + int(interval)) if last is not None else None

    # ------------------------------------------------------------ 统计与通知
    def __update_game_stats(self, state: Dict[str, Any]):
        inventory = state.get("inventory") or {}
        self._game_stats.update({
            "last_update": time.time(),
            "points": state.get("points"),
            "bonus_earned": state.get("bonus_earned"),
            "magic_pills": int(inventory.get("魔丸", 0) or 0),
            "daily_bricks": state.get("daily_bricks"),
            "daily_limit": self.BRICK_DAILY_LIMIT,
            "last_beach_time": state.get("last_beach_time"),
            "beach_interval": state.get("beach_interval"),
            "next_brick_reset_ts": state.get("next_brick_reset_ts"),
            "hnr": state.get("hnr"),
            "level": state.get("level"),
        })
        if inventory:
            self._inventory = dict(inventory)
        if state.get("daily_bricks") is not None:
            self._brick_stats["daily"] = state["daily_bricks"]
            self._brick_stats["available"] = max(
                0, self.BRICK_DAILY_LIMIT - int(state["daily_bricks"]))
        if state.get("last_beach_time") is not None and state.get("beach_interval") is not None:
            self._beach_stats["next_clean_ts"] = int(
                state["last_beach_time"] + state["beach_interval"])

    def __finish(self, kind: str, result: Dict[str, Any]):
        try:
            data = result.get("data") or {}
            if kind == "brick":
                row = (f"🧱 搬砖 +{data.get('moved', 0)}"
                       f"（今日 {data.get('daily', 0)}/{data.get('limit', self._brick_daily_limit)}）")
                count = data.get("moved", 0)
            elif kind == "beach":
                row = f"🏖️ 捡破烂 +{data.get('collected_total', 0)}"
                count = data.get("collected_total", 0)
            elif kind == "craft":
                row = f"🛠️ 炼造 +{data.get('total', 0)}"
                count = data.get("total", 0)
            else:
                row = (f"⚗️ 兑换 {data.get('exchanged', 0)} 颗"
                       f"（+{data.get('points_gained', 0)} 魔力）")
                count = data.get("exchanged", 0)
            history = self._stats.get("history", [])
            history.append({"time": time.time(), "result": row, "total_count": count,
                            "status": "completed" if result.get("status") == "completed" else "failed",
                            "type": kind})
            self._stats["history"] = history[-20:]
            self._stats["last_result"] = f"{row} · {result.get('message', '')}"[:60]
            self._stats["last_update"] = time.time()
        except Exception as e:
            logger.error(f"记录执行历史失败：{e}")

        if self._notify:
            self.__send_notification(kind, result)
        self._save_config()

    def __send_notification(self, kind: str, result: Dict[str, Any]):
        icons = {"brick": "🧱", "beach": "🏖️", "craft": "🛠️", "exchange": "💰"}
        labels = {"brick": "搬砖", "beach": "捡破烂", "craft": "炼造", "exchange": "兑换魔力"}
        status_map = {"completed": "✅ 完成", "partial": "⚠️ 部分完成", "failed": "❌ 失败",
                      "unknown": "❓ 结果未知", "auth_failed": "🔐 认证失败",
                      "error": "💥 异常", "skipped": "⏭️ 跳过"}
        status_text = status_map.get(result.get("status"), result.get("status", "未知"))
        text = f"状态：{status_text}\n消息：{result.get('message')}"

        data = result.get("data") or {}
        if kind == "brick" and data:
            text += (f"\n\n🧱 本轮搬砖：{data.get('moved', 0)} 块"
                     f"\n📊 今日进度：{data.get('daily', 0)}/{data.get('limit', self._brick_daily_limit)}")
        elif kind == "beach" and data:
            collected = data.get("collected") or {}
            if collected:
                detail = "、".join(f"{self.ITEM_ICONS.get(k, '📦')}{k}×{v}" for k, v in collected.items())
                text += f"\n\n🗑️ 收集：{detail}"
            craft = data.get("craft") or {}
            if craft.get("total"):
                text += f"\n🛠️ 同轮炼造：{craft['total']} 件"
            elif craft.get("status") == "failed":
                text += "\n🛠️ 同轮炼造：失败"
        elif kind == "exchange" and data:
            text += (f"\n\n⚗️ 兑换魔丸：{data.get('exchanged', 0)} 颗"
                     f"\n💰 获得魔力：{data.get('points_gained', 0):,}"
                     f"\n📦 剩余魔丸：{data.get('remaining', 0)} 颗")

        if self._game_stats.get("points") is not None:
            text += f"\n\n💰 当前魔力值：{self._game_stats['points']:,}"
        if self._inventory:
            text += f"\n🎒 物品：{len(self._inventory)} 种，共 {sum(int(v or 0) for v in self._inventory.values()):,} 件"

        self.post_message(mtype=MessageType.Plugin,
                          title=f"{icons.get(kind, '⚗️')}【{self.plugin_name}·{labels.get(kind, kind)}】",
                          text=text)

    def _save_config(self):
        try:
            self.update_config({
                "enabled": self._enabled, "cookie": self._cookie, "notify": self._notify,
                "brick_enabled": self._brick_enabled,
                "brick_daily_limit": self._brick_daily_limit,
                "brick_interval_ms": self._brick_interval_ms,
                "brick_daily_time": self._brick_daily_time,
                "brick_run_once": False,
                "beach_enabled": self._beach_enabled,
                "beach_interval_minutes": self._beach_interval_minutes,
                "beach_run_once": False,
                "craft_enabled": self._craft_enabled,
                "craft_keep_bricks": self._craft_keep_bricks,
                "craft_max_items_per_run": self._craft_max_items_per_run,
                "exchange_enabled": self._exchange_enabled,
                "exchange_quantity": self._exchange_quantity,
                "exchange_daily_time": self._exchange_daily_time,
                "exchange_run_once": False,
                "brick_stats": self._brick_stats,
                "beach_stats": self._beach_stats,
                "craft_stats": self._craft_stats,
                "exchange_stats": self._exchange_stats,
                "game_stats": self._game_stats,
                "stats": self._stats,
                "inventory": self._inventory,
            })
        except Exception as e:
            logger.error(f"保存插件配置失败：{e}")

    # ------------------------------------------------------------ 小工具
    def __cookie(self) -> str:
        return (self._cookie or self.__get_site_cookie() or "").strip()

    def __get_site_cookie(self) -> str:
        try:
            site = SiteOper().get_by_domain(self.SITE_DOMAIN)
            if site and site.cookie:
                return site.cookie
        except Exception as e:
            logger.error(f"获取站点 Cookie 异常：{e}")
        return ""

    @staticmethod
    def __cookie_to_dict(cookie: str) -> Dict[str, str]:
        cookies = {}
        for item in (cookie or "").split(";"):
            if "=" not in item:
                continue
            key, value = item.split("=", 1)
            key = key.strip()
            if key:
                cookies[key] = value.strip()
        return cookies

    @staticmethod
    def __time_to_cron(value: Any) -> Optional[str]:
        """把「HH:MM」转成每日 cron（`分 时 * * *`）；格式非法返回 None。"""
        if not isinstance(value, str):
            return None
        match = re.fullmatch(r"\s*(\d{1,2})\s*:\s*(\d{1,2})\s*", value)
        if not match:
            return None
        hour, minute = int(match.group(1)), int(match.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return f"{minute} {hour} * * *"

    @staticmethod
    def __clean_time(value: Any, default: str) -> str:
        return default if value in (None, "") else str(value).strip()

    @staticmethod
    def __safe_int(value: Any, default: int, min_value: Optional[int] = None) -> int:
        try:
            result = int(value) if value is not None else default
        except (ValueError, TypeError):
            result = default
        if min_value is not None:
            result = max(result, min_value)
        return result

    @staticmethod
    def __to_int(value: Any) -> Optional[int]:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return int(value)
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def __format_ts(value: Any) -> str:
        if value in (None, ""):
            return ""
        try:
            return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError, OSError):
            return str(value)

    @staticmethod
    def __format_left(seconds: Any) -> str:
        try:
            seconds = int(seconds)
        except (TypeError, ValueError):
            return "未知"
        hours, remainder = divmod(max(0, seconds), 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours} 小时 {minutes} 分"
        if minutes:
            return f"{minutes} 分 {secs} 秒"
        return f"{secs} 秒"

    @staticmethod
    def __new_result(status: str = "running", message: str = "") -> Dict[str, Any]:
        return {"status": status, "message": message, "timestamp": time.time()}
