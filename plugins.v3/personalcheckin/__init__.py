"""自用签到：NodeSeek / DeepFlood / linux.sb / yzyy 每日签到。

设计要点：
- 站点按 ``sites/`` 注册表组织，加新站只写一个适配器模块并注册一行；
  表单、页面、调度、历史、通知都从注册表生成。
- 每天 08:00–08:59 之间的随机分钟执行一次（当天抽到的分钟持久化，重启不漂移）。
- 单站失败重试两次（共 3 次）；直连站点第 2 次自动改走代理。
- 每次运行按站点各写一条历史，同一 run_id 归组，便于看出重试和一天多次运行。
- Cookie 支持手工填写 + CookieCloud 自动刷新；Windows 端脚本可一键推送四站 Cookie。
- yzyy 旧插件（YzyySignin）的历史在首次加载时一次性导入。
"""
from __future__ import annotations

import random
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from app.schemas.types import MessageType
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase

from .runtime import SiteContext, cookiecloud_cookies, cookie_header, mp_proxy
from .sites import (ALREADY, BLOCKED, ERROR, FAIL, NOAUTH, SITE_MAP, SITES, SKIPPED,
                    STATUS_ICON, STATUS_TEXT, SUCCESS, SiteResult)

TZ = ZoneInfo("Asia/Shanghai")
START_HOUR = 8
MAX_ATTEMPTS = 3
RETRY_DELAYS = (10, 30)
HISTORY_MAX = 1000
HISTORY_PAGE_ROWS = 50
LEGACY_PLUGIN_ID = "YzyySignin"


class PersonalCheckin(_PluginBase):
    """自用签到插件。"""

    plugin_name = "自用签到"
    plugin_desc = "NodeSeek / DeepFlood / linux.sb / yzyy 每日签到：8–9 点随机执行，记录余额与历史，MP 通知"
    plugin_icon = "signin.png"
    plugin_version = "1.0.3"
    plugin_author = "Fanzy8"
    author_url = "https://github.com/Fanzy8"
    plugin_config_prefix = "personalcheckin_"
    plugin_order = 25
    auth_level = 1

    def __init__(self) -> None:
        """初始化运行状态。"""
        super().__init__()
        self._enabled = False
        self._config: Dict[str, Any] = {}
        self._proxy = ""
        self._sites = list(SITES)
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 生命周期
    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        """加载配置；首次加载时导入旧 yzyy 历史。"""
        config = dict(config or {})
        self._config = config or self._default_config()
        self._enabled = bool(self._config.get("enabled"))
        self._proxy = str(self._config.get("proxy") or mp_proxy() or "").strip()
        if not self._config.get("proxy"):
            # 代理留空时跟随宿主全局配置，表单回显真实值
            self._config["proxy"] = self._proxy
        self._import_legacy_history()
        if self._config.get("onlyonce"):
            # 一次性开关改为持久化标记：保存配置会重建插件实例，内存标志留不住
            self.save_data("onlyonce_pending", datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"))
            merged = dict(self._config)
            merged["onlyonce"] = False
            self._config["onlyonce"] = False
            try:
                self.update_config(merged)
            except Exception as exc:  # pragma: no cover - 宿主侧异常
                logger.warn(f"自用签到：重置 onlyonce 失败 {exc!r}")

    def stop_service(self) -> None:
        """停用插件：无需释放额外资源（浏览器上下文在每次调用内关闭）。"""
        self._enabled = False

    def get_state(self) -> bool:
        """插件启用状态。"""
        return self._enabled

    # ------------------------------------------------------------ 配置读取
    @staticmethod
    def _default_config() -> Dict[str, Any]:
        """表单默认值；proxies 为空时运行时再取宿主全局代理。"""
        config: Dict[str, Any] = {
            "enabled": False,
            "notify": True,
            "cookiecloud": True,
            "proxy": "",
            "timeout": 60,
            "history_days": 90,
            "onlyonce": False,
        }
        for site in SITES:
            config[f"{site.id}_enabled"] = True
            config[f"{site.id}_cookie"] = ""
            config[f"{site.id}_direct"] = site.direct_default
        return config

    def _site_enabled(self, site_id: str) -> bool:
        value = self._config.get(f"{site_id}_enabled")
        return True if value is None else bool(value)

    def _site_direct(self, site_id: str) -> bool:
        site = SITE_MAP.get(site_id)
        default = bool(site.direct_default) if site else False
        value = self._config.get(f"{site_id}_direct")
        return default if value is None else bool(value)

    def _manual_cookie(self, site_id: str) -> str:
        return cookie_header(str(self._config.get(f"{site_id}_cookie") or ""))

    def _cookie_for(self, site: Any, cloud: Dict[str, str]) -> str:
        """CookieCloud 优先，手工填写兜底。"""
        if cloud and site.domain:
            for domain, cookie in cloud.items():
                key = str(domain or "").lstrip(".").lower()
                if key == site.domain or key.endswith("." + site.domain):
                    if cookie and cookie.strip():
                        return cookie_header(cookie)
        return self._manual_cookie(site.id)

    # ------------------------------------------------------------ 历史数据
    def _history(self) -> List[Dict[str, Any]]:
        value = self.get_data("history") or []
        return value if isinstance(value, list) else []

    def _import_legacy_history(self) -> None:
        """把旧 YzyySignin 的历史一次性并进来。"""
        if self.get_data("legacy_imported"):
            return
        try:
            legacy = self.get_data("history", plugin_id=LEGACY_PLUGIN_ID) or []
        except Exception:
            legacy = []
        self.save_data("legacy_imported", True)
        if not isinstance(legacy, list) or not legacy:
            return
        rows: List[Dict[str, Any]] = []
        for item in legacy:
            if not isinstance(item, dict):
                continue
            result = str(item.get("result") or "")
            status = SUCCESS if result == "成功" else FAIL
            rows.append({
                "run_id": "legacy",
                "ts": str(item.get("date") or ""),
                "site": "yzyy",
                "name": "yzyy 论坛",
                "status": status,
                "message": f"旧插件导入：{item.get('info') or result}",
                "reward": "",
                "balance": "",
                "extra": "",
                "attempts": 1,
                "trigger": "import",
            })
        if rows:
            merged = rows + self._history()
            merged.sort(key=lambda row: str(row.get("ts") or ""))
            self.save_data("history", self._trim_history(merged))
            logger.info(f"自用签到：已导入旧 yzyy 历史 {len(rows)} 条")

    def _trim_history(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        days = int(self._config.get("history_days") or 90)
        if days > 0:
            cutoff = (datetime.now(TZ) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            rows = [row for row in rows if str(row.get("ts") or "") >= cutoff]
        return rows[-HISTORY_MAX:]

    def _append_history(self, row: Dict[str, Any]) -> None:
        rows = self._history()
        rows.append(row)
        self.save_data("history", self._trim_history(rows))

    # ------------------------------------------------------------ 执行
    def run_all(self, trigger: str = "manual") -> Dict[str, Any]:
        """跑一轮全部启用站点，返回本次运行摘要。"""
        if not self._lock.acquire(blocking=False):
            logger.info("自用签到：上一轮还没结束，本次跳过")
            return {"run_id": "", "results": [], "skipped": True,
                    "message": "上一轮还没结束"}
        try:
            return self._run_all_locked(trigger)
        finally:
            self._lock.release()

    def _run_all_locked(self, trigger: str) -> Dict[str, Any]:
        """持锁执行一轮。"""
        run_id = uuid.uuid4().hex[:8]
        now = datetime.now(TZ)
        cloud: Dict[str, str] = {}
        if self._config.get("cookiecloud", True):
            cloud = cookiecloud_cookies()
            if cloud:
                logger.info(f"自用签到：CookieCloud 返回 {len(cloud)} 个域名的 Cookie")
        results: List[Dict[str, Any]] = []
        for site in self._sites:
            if not self._site_enabled(site.id):
                continue
            cookie = self._cookie_for(site, cloud)
            if not site.cookie_ok(cookie):
                result = SiteResult(SKIPPED, "未配置 Cookie")
                results.append(self._record(run_id, site, result, trigger, now))
                continue
            result = self._run_site(site, cookie)
            results.append(self._record(run_id, site, result, trigger, now))
        self.save_data("last_run", {"run_id": run_id, "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
                                    "trigger": trigger})
        if trigger != "manual":
            self._mark_done(now)
        if results:
            self._notify(results, run_id)
        logger.info(f"自用签到：run {run_id} 完成 {len(results)} 站（{trigger}）")
        return {"run_id": run_id, "ts": now.strftime("%Y-%m-%d %H:%M:%S"), "results": results}

    def _run_site(self, site: Any, cookie: str) -> SiteResult:
        """按站点执行，失败按配置重试。"""
        timeout = float(self._config.get("timeout") or 60)
        direct = self._site_direct(site.id)
        last: Optional[SiteResult] = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            # 直连站点第 2 次自动改走代理；没有配代理就一直直连
            use_direct = direct and not (attempt == 2 and self._proxy)
            ctx = SiteContext(site.id, site.name, cookie=cookie, direct=use_direct,
                              proxy=self._proxy, timeout=timeout)
            try:
                result = site.run(ctx)
            except Exception as exc:  # 站点适配器兜住不到的地方
                result = SiteResult(ERROR, f"未捕获异常：{type(exc).__name__}: {exc}")
            result.attempts = attempt
            last = result
            if not result.retryable or attempt >= MAX_ATTEMPTS:
                break
            delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
            logger.info(f"自用签到：{site.name} 第 {attempt} 次失败（{result.message}），{delay}s 后重试")
            time.sleep(delay)
        return last or SiteResult(ERROR, "未执行")

    def _record(self, run_id: str, site: Any, result: SiteResult, trigger: str,
                now: datetime) -> Dict[str, Any]:
        """写一条历史并返回同结构摘要。"""
        row = {
            "run_id": run_id,
            "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
            "site": site.id,
            "name": site.name,
            "status": result.status,
            "message": result.message,
            "reward": result.reward,
            "balance": result.balance,
            "extra": result.extra,
            "attempts": result.attempts,
            "trigger": trigger,
        }
        self._append_history(row)
        return row

    # ------------------------------------------------------------ 通知
    def _notify(self, rows: List[Dict[str, Any]], run_id: str) -> None:
        """一次运行发一条汇总通知，成功失败都发。"""
        if not self._config.get("notify", True):
            return
        ok = sum(1 for row in rows if row.get("status") in (SUCCESS, ALREADY))
        lines = []
        for row in rows:
            icon = STATUS_ICON.get(str(row.get("status")), "•")
            detail = row.get("message") or STATUS_TEXT.get(str(row.get("status")), "")
            extra = []
            if row.get("reward"):
                extra.append(str(row["reward"]))
            if row.get("balance"):
                extra.append(f"余额 {row['balance']}")
            if row.get("extra"):
                extra.append(str(row["extra"]))
            if row.get("attempts", 1) > 1:
                extra.append(f"{row['attempts']} 次尝试")
            tail = f"（{'，'.join(extra)}）" if extra else ""
            lines.append(f"{icon} {row.get('name')}：{detail}{tail}")
        title = f"【自用签到】{ok}/{len(rows)} 站完成"
        text = "\n".join(lines) + f"\n\n运行编号：{run_id}"
        try:
            self.post_message(mtype=MessageType.Plugin, title=title, text=text)
            logger.info(f"自用签到：通知已发送（{title}）")
        except Exception as exc:  # 通知失败不影响签到结论
            logger.warn(f"自用签到：通知发送失败 {exc!r}")

    # ------------------------------------------------------------ 调度
    def _target_minute(self, day: str) -> int:
        """当天 08 点内的随机分钟，首次抽到后持久化。"""
        targets = self.get_data("targets") or {}
        if not isinstance(targets, dict):
            targets = {}
        if day not in targets:
            targets[day] = random.randint(0, 59)
            # 只保留最近 14 天
            keep = sorted(targets)[-14:]
            targets = {key: targets[key] for key in keep}
            self.save_data("targets", targets)
        try:
            return int(targets[day])
        except (TypeError, ValueError):
            return 0

    def _mark_done(self, now: datetime) -> None:
        self.save_data("done_day", now.strftime("%Y-%m-%d"))

    def _done_today(self, now: datetime) -> bool:
        return self.get_data("done_day") == now.strftime("%Y-%m-%d")

    def _job_tick(self) -> None:
        """08:00–08:59 每分钟检查一次：过了当天随机分钟就跑一轮。"""
        now = datetime.now(TZ)
        if self._done_today(now):
            return
        target = self._target_minute(now.strftime("%Y-%m-%d"))
        if now.minute < target:
            return
        self.run_all(trigger="scheduled")

    def _job_startup(self) -> None:
        """启动补跑：错过窗口（≥09:00）且今天没跑过时补一轮。"""
        now = datetime.now(TZ)
        if self._done_today(now) or now.hour < START_HOUR:
            return
        target = self._target_minute(now.strftime("%Y-%m-%d"))
        if now.hour == START_HOUR and now.minute < target:
            return
        self.run_all(trigger="startup")

    def _job_once(self) -> None:
        """「立即运行一次」开关触发的一次性运行。"""
        self.del_data("onlyonce_pending")
        self.run_all(trigger="manual")

    def get_service(self) -> List[Dict[str, Any]]:
        """注册 8 点随机分钟任务 + 启动补跑 + 可选的一次性运行。"""
        if not self._enabled:
            return []
        services: List[Dict[str, Any]] = [
            {
                "id": "PersonalCheckinTick",
                "name": "自用签到 · 8 点随机分钟",
                "trigger": CronTrigger(hour=str(START_HOUR), minute="*", timezone=TZ),
                "func": self._job_tick,
                "kwargs": {},
            },
            {
                "id": "PersonalCheckinStartup",
                "name": "自用签到 · 启动补跑",
                # 立即执行会被调度器判为「已错过」，留余量
                "trigger": DateTrigger(run_date=datetime.now(TZ) + timedelta(seconds=20)),
                "func": self._job_startup,
                "kwargs": {},
            },
        ]
        if self.get_data("onlyonce_pending"):
            services.append({
                "id": "PersonalCheckinOnce",
                "name": "自用签到 · 立即运行一次",
                "trigger": DateTrigger(run_date=datetime.now(TZ) + timedelta(seconds=15)),
                "func": self._job_once,
                "kwargs": {},
            })
        return services

    # ------------------------------------------------------------ API / 命令
    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """聊天命令：/checkin。"""
        return [{
            "cmd": "/checkin",
            "event": "PluginAction",
            "desc": "立即执行自用签到（四站）",
            "category": "站点",
            "data": {"action": "personalcheckin_run"},
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        """插件 API：手动运行、推送 Cookie、读历史。"""
        return [
            {"path": "/run", "endpoint": self._api_run, "methods": ["POST"],
             "auth": "bear", "summary": "立即执行一次签到"},
            {"path": "/cookies", "endpoint": self._api_cookies, "methods": ["POST"],
             "auth": "bear", "summary": "推送四站 Cookie"},
            {"path": "/history", "endpoint": self._api_history, "methods": ["GET"],
             "auth": "bear", "summary": "读取签到历史"},
        ]

    def _api_run(self) -> Dict[str, Any]:
        """手动跑一轮。"""
        return {"success": True, "data": self.run_all(trigger="manual")}

    def _api_cookies(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """接收 ``{站点id: cookie}`` 并写入配置。"""
        if not isinstance(payload, dict) or not payload:
            return {"success": False, "message": "请求体需要 {站点id: cookie}"}
        merged = dict(self._config)
        saved, unknown = [], []
        for key, value in payload.items():
            site_id = str(key).strip()
            if site_id not in SITE_MAP:
                unknown.append(site_id)
                continue
            cookie = cookie_header(str(value or ""))
            if not cookie:
                continue
            merged[f"{site_id}_cookie"] = cookie
            saved.append(site_id)
        if not saved:
            return {"success": False, "message": "没有可写入的 Cookie", "unknown": unknown}
        self._config = merged
        self.update_config(merged)
        logger.info(f"自用签到：已更新 Cookie {saved}")
        return {"success": True, "saved": saved, "unknown": unknown}

    def _api_history(self) -> Dict[str, Any]:
        """最近 100 条历史。"""
        return {"success": True, "data": self._history()[-100:][::-1]}

    # ------------------------------------------------------------ 表单
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """按注册表生成表单：全局设置 + 每站一块。"""
        rows: List[dict] = [
            self._row([
                self._col(12, 3, self._switch("enabled", "启用插件", "success")),
                self._col(12, 3, self._switch("notify", "发送通知", "info")),
                self._col(12, 3, self._switch("cookiecloud", "使用 CookieCloud", "primary",
                                              hint="开启后优先使用 CookieCloud 里的 Cookie")),
                self._col(12, 3, self._switch("onlyonce", "立即运行一次", "warning",
                                              hint="保存后约 15 秒执行一轮")),
            ]),
            self._row([
                self._col(12, 6, self._text("proxy", "代理地址",
                                            hint="留空则跟随 MoviePilot 的全局代理；直连站点不受影响")),
                self._col(12, 3, self._text("timeout", "单次请求超时（秒）", "number")),
                self._col(12, 3, self._text("history_days", "历史保留天数", "number")),
            ]),
        ]
        for site in SITES:
            rows.append(self._row([
                self._col(12, 4, self._switch(f"{site.id}_enabled", f"启用 {site.name}", "success")),
                self._col(12, 8, self._switch(f"{site.id}_direct", "直连（失败自动改走代理）",
                                              "info", hint=site.describe())),
            ]))
            rows.append(self._row([
                self._col(12, 12, {
                    "component": "VTextarea",
                    "props": {
                        "model": f"{site.id}_cookie",
                        "label": f"🔑 {site.name} Cookie",
                        "rows": 2,
                        "placeholder": f"{site.name} 登录后整段复制 Cookie",
                        "hint": site.help_text,
                    },
                }),
            ]))
        rows.append(self._row([
            self._col(12, 12, {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "text": ("每天 08:00–08:59 之间随机分钟执行一次；单站失败重试 2 次。"
                             "Windows 端可用仓库 tools/cookie-push 一键推送四站 Cookie。"),
                },
            }),
        ]))
        return [{"component": "VForm", "content": rows}], self._form_defaults()

    def _form_defaults(self) -> Dict[str, Any]:
        """表单默认值：内置默认 + 已保存配置（代理回显真实值）。"""
        defaults = self._default_config()
        for key in defaults:
            if key in self._config:
                defaults[key] = self._config[key]
        if not defaults.get("proxy"):
            defaults["proxy"] = self._proxy
        return defaults

    @staticmethod
    def _row(content: List[dict]) -> dict:
        return {"component": "VRow", "content": content}

    @staticmethod
    def _col(cols: int, md: int, content: dict) -> dict:
        return {"component": "VCol", "props": {"cols": cols, "md": md}, "content": [content]}

    @staticmethod
    def _switch(model: str, label: str, color: str = "success", hint: str = "") -> dict:
        props = {"model": model, "label": label, "color": color}
        if hint:
            props["hint"] = hint
        return {"component": "VSwitch", "props": props}

    @staticmethod
    def _text(model: str, label: str, kind: str = "text", hint: str = "") -> dict:
        props = {"model": model, "label": label, "type": kind}
        if hint:
            props["hint"] = hint
        return {"component": "VTextField", "props": props}

    # ------------------------------------------------------------ 详情页
    def get_page(self) -> Optional[List[dict]]:
        """详情页：上次运行 + 各站最近一次结果 + 历史表。"""
        history = self._history()
        last_run = self.get_data("last_run") or {}
        today = datetime.now(TZ).strftime("%Y-%m-%d")

        headers = []
        for site in SITES:
            rows = [row for row in history if row.get("site") == site.id]
            latest = rows[-1] if rows else None
            today_row = next((row for row in reversed(rows)
                              if str(row.get("ts") or "").startswith(today)), None)
            status = (today_row or latest or {}).get("status")
            icon = STATUS_ICON.get(str(status), "…")
            headers.append(self._col(12, 3, {
                "component": "VCard",
                "props": {"variant": "tonal", "class": "h-100"},
                "content": [{
                    "component": "VCardText",
                    "props": {"class": "pa-3"},
                    "content": [{
                        "component": "div",
                        "props": {"class": "text-subtitle-2 font-weight-medium"},
                        "text": f"{icon} {site.name}",
                    }, {
                        "component": "div",
                        "props": {"class": "text-caption text-medium-emphasis mt-1"},
                        "text": (today_row or {}).get("reward") or STATUS_TEXT.get(str(status), "暂无记录"),
                    }, {
                        "component": "div",
                        "props": {"class": "text-caption text-medium-emphasis"},
                        "text": (today_row or {}).get("balance") or "",
                    }, {
                        "component": "div",
                        "props": {"class": "text-caption text-medium-emphasis"},
                        "text": (today_row or {}).get("extra") or (today_row or {}).get("message") or "",
                    }],
                }],
            }))

        table_rows = []
        for row in history[-HISTORY_PAGE_ROWS:][::-1]:
            icon = STATUS_ICON.get(str(row.get("status")), "•")
            detail = str(row.get("message") or "")
            if row.get("reward"):
                detail = f"{detail} {row['reward']}"
            if row.get("balance"):
                detail = f"{detail}（余额 {row['balance']}）"
            table_rows.append({
                "component": "tr",
                "content": [
                    self._cell(str(row.get("ts") or ""), "text-caption"),
                    self._cell(str(row.get("name") or row.get("site") or ""), "text-caption"),
                    self._cell(f"{icon} {STATUS_TEXT.get(str(row.get('status')), row.get('status'))}", "text-caption"),
                    self._cell(str(row.get("attempts") or 1), "text-caption"),
                    self._cell(detail[:160], "text-caption"),
                ],
            })

        target = self._target_minute(today) if self._enabled else None
        summary = (f"上次运行：{last_run.get('ts') or '暂无'}（{last_run.get('trigger') or '-'}）"
                   f" · 今天的随机分钟：08:{target:02d}" if target is not None
                   else f"上次运行：{last_run.get('ts') or '暂无'}")
        return [
            {
                "component": "VCard",
                "props": {"variant": "tonal", "class": "mb-3"},
                "content": [{
                    "component": "VCardText",
                    "props": {"class": "pa-3"},
                    "content": [{
                        "component": "div",
                        "props": {"class": "text-body-2"},
                        "text": summary,
                    }, {
                        "component": "div",
                        "props": {"class": "text-caption text-medium-emphasis"},
                        "text": (f"代理：{self._proxy or '未配置'} · "
                                 f"CookieCloud：{'开' if self._config.get('cookiecloud', True) else '关'} · "
                                 f"已记录 {len(history)} 条"),
                    }],
                }],
            },
            {"component": "VRow", "props": {"dense": True}, "content": headers},
            {
                "component": "VCard",
                "props": {"variant": "elevated", "elevation": 1, "class": "mt-3"},
                "content": [{
                    "component": "VCardText",
                    "props": {"class": "pa-3"},
                    "content": [{
                        "component": "VSimpleTable",
                        "props": {"dense": True, "class": "elevation-0"},
                        "content": [
                            {"component": "thead", "content": [{
                                "component": "tr",
                                "content": [
                                    self._th("时间"), self._th("站点"), self._th("结果"),
                                    self._th("尝试"), self._th("详情"),
                                ],
                            }]},
                            {"component": "tbody", "content": table_rows or [{
                                "component": "tr",
                                "content": [self._cell("暂无签到记录", "text-caption")],
                            }]},
                        ],
                    }],
                }],
            },
        ]

    @staticmethod
    def _th(text: str) -> dict:
        return {"component": "th", "props": {"class": "text-left text-caption font-weight-medium"}, "text": text}

    @staticmethod
    def _cell(text: str, cls: str) -> dict:
        return {"component": "td", "props": {"class": cls}, "text": text}
