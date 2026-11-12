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
import re
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

from .runtime import (SiteContext, cookie_parse, cookiecloud_cookies, cookie_header,
                      mp_proxy)
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
    plugin_version = "1.1.3"
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
        self._proxy_alt = ""
        self._sites = list(SITES)
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 生命周期
    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        """加载配置；首次加载时导入旧 yzyy 历史。"""
        config = dict(config or {})
        self._config = config or self._default_config()
        self._enabled = bool(self._config.get("enabled"))
        self._proxy = str(self._config.get("proxy") or mp_proxy() or "").strip()
        self._proxy_alt = str(self._config.get("proxy_alt") or "").strip()
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
            "proxy_alt": "",
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
        """CookieCloud 优先，但只采用「能通过站点校验」的那一份。

        CookieCloud 同步不到 httpOnly 的 session（NodeSeek 系必须带），只有 cf_clearance
        的 Cookie 会让服务端当未登录，返回 USER NOT FOUND；此时以手工 Cookie 为身份基底，
        只借用 Cloud 里较新的 cf_clearance 等字段。
        """
        manual = self._manual_cookie(site.id)
        cloud_cookie = ""
        if cloud and site.domain:
            for domain, cookie in cloud.items():
                key = str(domain or "").lstrip(".").lower()
                if key == site.domain or key.endswith("." + site.domain):
                    candidate = cookie_header(cookie)
                    if not candidate:
                        continue
                    if site.cookie_ok(candidate):
                        return candidate
                    cloud_cookie = cloud_cookie or candidate
        if cloud_cookie and manual:
            merged = cookie_parse(manual)
            for key, value in cookie_parse(cloud_cookie).items():
                # 身份字段以手工 Cookie 为准，其余（cf_clearance 等短期票据）允许 Cloud 刷新
                if key in ("session", "pjwt", "smac", "auth", "bbs_auth", "ANASID"):
                    continue
                merged[key] = value
            candidate = cookie_header("; ".join(f"{k}={v}" for k, v in merged.items()))
            if site.cookie_ok(candidate):
                return candidate
        return manual

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
    def run_all(self, trigger: str = "manual", site_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        """跑一轮签到。

        ``site_ids`` 为空时跑全部启用站点；传了站点 id 就只跑这些站点
        （手动单站运行、失败重试都走这条路，不会把已成功的站点再跑一遍）。
        """
        if not self._lock.acquire(blocking=False):
            logger.info("自用签到：上一轮还没结束，本次跳过")
            return {"run_id": "", "results": [], "skipped": True,
                    "message": "上一轮还没结束"}
        try:
            return self._run_all_locked(trigger, site_ids=site_ids)
        finally:
            self._lock.release()

    def _run_all_locked(self, trigger: str, site_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        """持锁执行一轮；``site_ids`` 限定本次要跑的站点。"""
        run_id = uuid.uuid4().hex[:8]
        now = datetime.now(TZ)
        wanted = [str(item).strip() for item in (site_ids or []) if str(item).strip()]
        known = {site.id: site for site in self._sites}
        if wanted:
            unknown = [item for item in wanted if item not in known]
            if unknown:
                return {"run_id": "", "results": [], "skipped": True,
                        "message": f"未知站点：{', '.join(unknown)}"}
            selected = [known[item] for item in dict.fromkeys(wanted)]
        else:
            selected = list(self._sites)

        cloud: Dict[str, str] = {}
        if self._config.get("cookiecloud", True):
            cloud = cookiecloud_cookies()
            if cloud:
                logger.info(f"自用签到：CookieCloud 返回 {len(cloud)} 个域名的 Cookie")
        results: List[Dict[str, Any]] = []
        skipped_disabled: List[str] = []
        for site in selected:
            if not self._site_enabled(site.id):
                skipped_disabled.append(site.name)
                continue
            cookie = self._cookie_for(site, cloud)
            if not site.cookie_ok(cookie):
                result = SiteResult(SKIPPED, "未配置 Cookie")
                results.append(self._record(run_id, site, result, trigger, now))
                continue
            result = self._run_site(site, cookie)
            results.append(self._record(run_id, site, result, trigger, now))
        if not results:
            message = (f"已跳过：{', '.join(skipped_disabled)} 未启用"
                       if skipped_disabled else "没有可运行的站点")
            logger.info(f"自用签到：{message}")
            return {"run_id": "", "results": [], "skipped": True, "message": message}

        full_run = not wanted
        self.save_data("last_run", {"run_id": run_id, "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
                                    "trigger": trigger})
        if trigger != "manual" and full_run:
            self._mark_done(now)
        self._notify(results, run_id)
        scope = "全部站点" if full_run else f"{len(results)} 个指定站点"
        logger.info(f"自用签到：run {run_id} 完成 {len(results)} 站（{trigger}，{scope}）")
        return {"run_id": run_id, "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
                "results": results, "scope": scope}

    def failed_sites(self, statuses: Optional[Tuple[str, ...]] = None) -> List[str]:
        """今天最近一次运行里失败的站点 id（用于只重试失败站点）。

        ``statuses`` 可限定只挑某些状态：自动重试只挑可恢复的
        ERROR/BLOCKED，Cookie 失效（NOAUTH）和站点明确拒绝（FAIL）重试也没用。
        """
        history = self._history()
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        today_rows = [row for row in history if str(row.get("ts") or "").startswith(today)]
        if not today_rows:
            return []
        last_run_id = today_rows[-1].get("run_id")
        bad = set(statuses or (FAIL, ERROR, BLOCKED, NOAUTH))
        ids: List[str] = []
        for row in today_rows:
            if row.get("run_id") != last_run_id:
                continue
            site_id = str(row.get("site") or "")
            if row.get("status") in bad and site_id and site_id not in ids:
                ids.append(site_id)
        return ids

    def retry_failed(self, trigger: str = "manual") -> Dict[str, Any]:
        """只重跑今天最近一轮里失败的站点，不碰已成功的站点。"""
        ids = self.failed_sites()
        if not ids:
            return {"run_id": "", "results": [], "skipped": True,
                    "message": "今天没有失败的站点，无需重试"}
        logger.info(f"自用签到：只重试失败站点 {', '.join(ids)}")
        return self.run_all(trigger=trigger, site_ids=ids)

    def _run_site(self, site: Any, cookie: str) -> SiteResult:
        """按站点执行，失败按配置重试。"""
        timeout = float(self._config.get("timeout") or 60)
        direct = self._site_direct(site.id)
        # 优先用备用代理的站点（如 yzyy）：分流代理会把它判成直连而失败
        proxy = self._proxy_alt if (getattr(site, "prefer_alt_proxy", False) and self._proxy_alt) else self._proxy
        last: Optional[SiteResult] = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            # 直连站点第 2 次自动改走代理；没有配代理就一直直连
            use_direct = direct and not (attempt == 2 and proxy)
            ctx = SiteContext(site.id, site.name, cookie=cookie, direct=use_direct,
                              proxy=proxy, timeout=timeout)
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

    def _job_retry_failed(self) -> None:
        """自动重试：只跑今天失败且可恢复的站点，每天最多一次。

        Cookie 失效/站点明确拒绝不自动重试（重试也无效，只会多一条失败通知），
        需要时用详情页的「只跑失败站点」按钮手动触发。
        """
        now = datetime.now(TZ)
        day = now.strftime("%Y-%m-%d")
        last_run = self.get_data("last_run") or {}
        if not str(last_run.get("ts") or "").startswith(day):
            return
        retried = self.get_data("retry_day") or ""
        if retried == day:
            return
        ids = self.failed_sites(statuses=(ERROR, BLOCKED))
        if not ids:
            return
        self.save_data("retry_day", day)
        logger.info(f"自用签到：自动重试失败站点 {', '.join(ids)}")
        self.run_all(trigger="retry", site_ids=ids)

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
            {
                "id": "PersonalCheckinRetry",
                "name": "自用签到 · 失败站点重试",
                # 主任务跑完后每 30 分钟看一次：只重跑可恢复的失败站点，每天最多一次
                # 首次重试推迟到 10:00：主任务 8 点跑，8-10 点之间保活会做多次择优，
                # 节点可能已经换过；出口 IP 被 Cloudflare 拦的情况要靠这个时间差规避。
                "trigger": CronTrigger(hour="10-13", minute="*/30", timezone=TZ),
                "func": self._job_retry_failed,
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
             "auth": "bear", "summary": "立即执行一次签到（全部站点）"},
            {"path": "/run/site/{site}", "endpoint": self._api_run_site, "methods": ["POST"],
             "auth": "bear", "summary": "只运行指定站点（多个用逗号分隔）"},
            {"path": "/run/failed", "endpoint": self._api_retry_failed, "methods": ["POST"],
             "auth": "bear", "summary": "只重试今天失败的站点"},
            {"path": "/cookies", "endpoint": self._api_cookies, "methods": ["POST"],
             "auth": "bear", "summary": "推送四站 Cookie"},
            {"path": "/history", "endpoint": self._api_history, "methods": ["GET"],
             "auth": "bear", "summary": "读取签到历史"},
        ]

    def _api_run(self) -> Dict[str, Any]:
        """手动跑一轮（全部站点）。"""
        return {"success": True, "data": self.run_all(trigger="manual")}

    def _api_run_site(self, site: str = "") -> Dict[str, Any]:
        """只跑指定站点：``/run/site/yzyy`` 或 ``/run/site/yzyy,nodeseek``。"""
        ids = [item.strip() for item in re.split(r"[,，\s]+", str(site or "")) if item.strip()]
        if not ids:
            return {"success": False, "message": "需要站点 id，例如 yzyy",
                    "data": {"site_ids": [item.id for item in SITES]}}
        data = self.run_all(trigger="manual", site_ids=ids)
        if data.get("skipped"):
            return {"success": False, "message": str(data.get("message") or "未执行"), "data": data}
        return {"success": True, "data": data}

    def _api_retry_failed(self) -> Dict[str, Any]:
        """只重试今天最近一轮失败的站点。"""
        data = self.retry_failed(trigger="manual")
        if data.get("skipped"):
            return {"success": False, "message": str(data.get("message") or "无需重试"), "data": data}
        return {"success": True, "data": data}

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
                self._col(12, 6, self._text("proxy_alt", "备用代理（无分流）",
                                            hint="给分流规则误判的站点用（如 yzyy）。填普通 HTTP 代理端口，不要填带分流的端口")),
            ]),
            self._row([
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
        failed_count = len(self.failed_sites())

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
                    }, {
                        "component": "VBtn",
                        "props": {"variant": "text", "size": "small", "class": "mt-1 px-0",
                                  "prepend-icon": "mdi-play-circle-outline"},
                        "text": "运行",
                        "events": {"click": {"api": f"plugin/PersonalCheckin/run/site/{site.id}",
                                             "method": "post"}},
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
                        "text": (f"代理：{self._proxy or '未配置'}"
                                 + (f" · 备用：{self._proxy_alt}" if self._proxy_alt else "")
                                 + f" · CookieCloud：{'开' if self._config.get('cookiecloud', True) else '关'} · "
                                 f"已记录 {len(history)} 条"),
                    }, {
                        "component": "div",
                        "props": {"class": "d-flex flex-wrap ga-2 mt-2"},
                        "content": [{
                            "component": "VBtn",
                            "props": {"variant": "tonal", "size": "small",
                                      "prepend-icon": "mdi-play"},
                            "text": "全部运行",
                            "events": {"click": {"api": "plugin/PersonalCheckin/run",
                                                 "method": "post"}},
                        }, {
                            "component": "VBtn",
                            "props": {"variant": "tonal", "color": "warning", "size": "small",
                                      "prepend-icon": "mdi-restart"},
                            "text": (f"只跑失败站点（{failed_count}）" if failed_count
                                     else "只跑失败站点"),
                            "events": {"click": {"api": "plugin/PersonalCheckin/run/failed",
                                                 "method": "post"}},
                        }],
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
