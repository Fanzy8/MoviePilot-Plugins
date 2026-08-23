"""
藏宝阁大转盘抽奖插件 - 完整版
"""
import json
import random
import re
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests
import urllib3
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from urllib3.exceptions import InsecureRequestWarning

from app.core.event import eventmanager, Event
from app.db.site_oper import SiteOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType

urllib3.disable_warnings(InsecureRequestWarning)


class CbgSpin(_PluginBase):
    """藏宝阁大转盘抽奖插件"""

    plugin_name = "藏宝阁大转盘抽奖"
    plugin_desc = "自动执行藏宝阁(CBG)站点大转盘抽奖，智能使用10连抽，支持随机冷却时间"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/statistic.png"
    plugin_version = "1.3"
    plugin_author = "bfjy"
    author_url = "https://bfjy2024.github.io/bfjy"
    plugin_config_prefix = "cbgspin_"
    plugin_order = 26
    auth_level = 2

    BASE_URL = "https://cangbao.ge"
    SPIN_URL = f"{BASE_URL}/api/api_luckywheel.php"
    REFERER = f"{BASE_URL}/luckywheel.php"
    INDEX_URL = f"{BASE_URL}/index.php"
    SITE_DOMAIN = "cangbao.ge"
    MAX_HISTORY = 100
    REQUEST_TIMEOUT = 30
    COST_MP = 2000  # 每次抽奖消耗魔力值
    BATCH_SIZE = 10  # 批量抽奖大小

    # 藏宝阁大转盘奖品映射（从官方源代码提取）
    PRIZE_MAP = {
        1: {"name": "魔力值1000", "type": "magic", "value": 1000},
        2: {"name": "上传量1G", "type": "upload", "value": 1},
        3: {"name": "补签卡1张", "type": "item", "value": 1},
        4: {"name": "彩虹ID(7天)", "type": "item", "value": 0},
        5: {"name": "VIP(7天)", "type": "vip", "value": 0},
        6: {"name": "邀请码(7天)", "type": "item", "value": 0},
        7: {"name": "谢谢参与", "type": "none", "value": 0},
        8: {"name": "魔力值2000", "type": "magic", "value": 2000},
        9: {"name": "改名卡1张", "type": "item", "value": 1},
        10: {"name": "上传量2G", "type": "upload", "value": 2},
    }

    _enabled: bool = False
    _onlyonce: bool = False
    _cron: str = ""
    _cookie: str = ""
    _notify: bool = False
    _max_spins: int = 10
    _scheduler: Optional[BackgroundScheduler] = None
    _lock: threading.Lock = threading.Lock()
    _last_bonus: float = 0  # 记录上次魔力值，用于计算差值

    def init_plugin(self, config: dict = None):
        self.stop_service()

        if config:
            self._enabled = config.get("enabled", False)
            self._onlyonce = config.get("onlyonce", False)
            self._cron = config.get("cron", "")
            self._cookie = config.get("cookie", "")
            self._notify = config.get("notify", False)
            try:
                self._max_spins = int(config.get("max_spins", 10))
            except (TypeError, ValueError):
                self._max_spins = 10

        if self._onlyonce:
            self._onlyonce = False
            self.update_config({
                "enabled": self._enabled,
                "onlyonce": False,
                "cron": self._cron,
                "cookie": self._cookie,
                "notify": self._notify,
                "max_spins": self._max_spins,
            })
            logger.info("收到立即运行请求，后台启动抽奖任务")
            threading.Thread(target=self.__run_spin_task, daemon=True).start()

        logger.info(f"藏宝阁抽奖插件初始化完成，启用状态: {self._enabled}")

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [{
            "cmd": "/cbg_spin",
            "event": EventType.PluginAction,
            "desc": "执行藏宝阁抽奖",
            "category": "站点",
            "data": {"action": "cbg_spin_run"}
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/spin",
            "endpoint": self.__spin_api,
            "methods": ["POST"],
            "auth": "bear",
            "summary": "执行藏宝阁抽奖",
        }, {
            "path": "/history",
            "endpoint": self.__history_api,
            "methods": ["GET"],
            "auth": "bear",
            "summary": "获取抽奖历史",
        }]

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            try:
                return [{
                    "id": "CbgSpin",
                    "name": "藏宝阁大转盘抽奖",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.__run_spin_task,
                    "kwargs": {}
                }]
            except Exception as e:
                logger.error(f"藏宝阁抽奖服务配置错误: {e}")
        return []

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"停止藏宝阁抽奖服务失败: {e}")

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        site_cookie = self.__get_site_cookie()
        cookie_value = self._cookie or site_cookie or ""
        
        try:
            max_spins = int(self._max_spins) if self._max_spins else 10
        except (TypeError, ValueError):
            max_spins = 10
        
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                            "color": "success"
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "notify",
                                            "label": "发送通知",
                                            "color": "info"
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "onlyonce",
                                            "label": "立即运行一次",
                                            "color": "warning",
                                            "hint": "保存配置后立即执行一次"
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VCronField",
                                        "props": {
                                            "model": "cron",
                                            "label": "执行周期",
                                            "placeholder": "0 8 * * *",
                                            "hint": "Cron 表达式，例如 0 8 * * * 表示每天8点执行"
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "max_spins",
                                            "label": "每次抽奖次数",
                                            "type": "number",
                                            "min": 1,
                                            "max": 100,
                                            "hint": f"每次任务执行抽奖次数，每次消耗 {self.COST_MP} 魔力值，>=10次时自动使用10连抽"
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "cookie",
                                            "label": "🔑 藏宝阁 Cookie",
                                            "rows": 2,
                                            "placeholder": "SITE_TOTAL_ID=xxx; c_secure_pass=xxx",
                                            "hint": "从浏览器复制完整的 Cookie 字符串，留空则使用站点管理中保存的 Cookie"
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": f"📌 使用说明：\n"
                                                   f"1. 每次抽奖消耗 {self.COST_MP} 魔力值\n"
                                                   f"2. 当抽奖次数 >= 10 时，自动使用10连抽提高效率\n"
                                                   "3. 每次抽奖之间冷却 1-3 秒随机\n"
                                                   "4. 奖品包括：魔力值、上传量、补签卡、VIP、邀请码、改名卡等\n"
                                                   "5. 需要配置 Cookie 才能正常运行\n"
                                                   "6. 抽奖结果会自动记录并可选发送通知"
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": self._enabled,
            "onlyonce": False,
            "cron": self._cron or "0 8 * * *",
            "cookie": cookie_value,
            "notify": self._notify,
            "max_spins": max_spins,
        }

    def get_page(self) -> List[dict]:
        """获取详情页面"""
        records = self.__get_records()
        
        # 获取用户信息
        user_info = self.__fetch_user_info()
        
        # 构建统计
        today_summary, yesterday_summary = self.__build_recent_prize_summary(records)
        
        return [
            # 我的信息卡片
            {
                "component": "VCard",
                "props": {"variant": "tonal", "class": "mb-4"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "text": "👤 我的信息"
                    },
                    {
                        "component": "VCardText",
                        "content": [
                            {
                                "component": "VRow",
                                "content": [
                                    self.__info_col("用户名", user_info.get("username")),
                                    self.__info_col("💎 魔力值", user_info.get("bonus")),
                                    self.__info_col("📤 上传量", user_info.get("upload")),
                                    self.__info_col("📥 下载量", user_info.get("download")),
                                ]
                            },
                            {
                                "component": "VRow",
                                "content": [
                                    self.__info_col("📊 分享率", user_info.get("ratio")),
                                    self.__info_col("🎫 邀请数", user_info.get("invite")),
                                    self.__info_col("📋 补签卡", user_info.get("repair_card")),
                                ]
                            },
                            {
                                "component": "div",
                                "props": {"class": "text-caption text-medium-emphasis mt-2"},
                                "text": f"更新时间：{user_info.get('updated_at')} | {user_info.get('message')}"
                            }
                        ]
                    }
                ]
            },
            # 统计卡片
            {
                "component": "VCard",
                "props": {"variant": "tonal", "class": "mb-4"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "text": "📊 抽奖统计"
                    },
                    {
                        "component": "VCardText",
                        "content": [
                            {
                                "component": "VRow",
                                "content": self.__build_statistics(records)
                            }
                        ]
                    }
                ]
            },
            # 奖品分布 - 今日/昨日对比
            {
                "component": "VDivider",
                "props": {"class": "my-4"}
            },
            {
                "component": "div",
                "props": {"class": "text-h6 mb-3"},
                "text": "🏆 奖品分布"
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "tonal", "class": "h-100"},
                                "content": [
                                    {
                                        "component": "VCardTitle",
                                        "text": "今日奖品"
                                    },
                                    {
                                        "component": "VCardText",
                                        "content": [
                                            self.__summary_chart("今日奖品分布", today_summary),
                                            {
                                                "component": "VRow",
                                                "props": {"dense": True},
                                                "content": self.__summary_grid(today_summary)
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "tonal", "class": "h-100"},
                                "content": [
                                    {
                                        "component": "VCardTitle",
                                        "text": "昨日奖品"
                                    },
                                    {
                                        "component": "VCardText",
                                        "content": [
                                            self.__summary_chart("昨日奖品分布", yesterday_summary),
                                            {
                                                "component": "VRow",
                                                "props": {"dense": True},
                                                "content": self.__summary_grid(yesterday_summary)
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ]

    def __info_col(self, label: str, value: Any) -> Dict[str, Any]:
        """信息列"""
        return {
            "component": "VCol",
            "props": {"cols": 6, "md": 3},
            "content": [
                {
                    "component": "div",
                    "props": {"class": "text-caption text-medium-emphasis"},
                    "text": label
                },
                {
                    "component": "div",
                    "props": {"class": "text-h6"},
                    "text": str(value or "-")
                }
            ]
        }

    def __build_statistics(self, records: List[Dict]) -> List[Dict]:
        """构建统计信息"""
        total = len(records)
        success_count = sum(1 for h in records if h.get('success', False))
        win_count = sum(1 for h in records if h.get('is_win', False))
        total_bonus = sum(h.get('bonus', 0) for h in records if h.get('bonus', 0) > 0)
        
        stats = [
            self.__info_col("✅ 成功", str(success_count)),
            self.__info_col("🏆 中奖", str(win_count)),
            self.__info_col("💰 总魔力", f"{total_bonus:,.0f}" if total_bonus > 0 else "0"),
        ]
        return stats

    def __summary_grid(self, items: List[dict]) -> List[Dict[str, Any]]:
        """汇总网格"""
        if not items or (len(items) == 1 and items[0].get("name") == "无记录"):
            return [{
                "component": "VCol",
                "props": {"cols": 12},
                "content": [{
                    "component": "div",
                    "props": {"class": "text-caption text-medium-emphasis"},
                    "text": "暂无数据"
                }]
            }]
        
        return [
            {
                "component": "VCol",
                "props": {"cols": 12, "sm": 6, "md": 4},
                "content": [
                    {
                        "component": "VChip",
                        "props": {
                            "variant": "tonal",
                            "color": "primary",
                            "class": "ma-1"
                        },
                        "text": f"{item.get('name')} x {item.get('count')}" if item.get('count') else item.get('name')
                    }
                ]
            }
            for item in items[:12]
        ]

    def __summary_chart(self, title: str, items: List[dict]) -> Dict[str, Any]:
        """汇总图表"""
        chart_items = [
            item for item in items
            if isinstance(item.get("count"), (int, float)) and item.get("count") > 0
        ]
        
        if not chart_items:
            return {
                "component": "div",
                "props": {"class": "text-center pa-4 text-medium-emphasis"},
                "text": "暂无数据"
            }
        
        return {
            "component": "VApexChart",
            "props": {
                "height": 260,
                "options": {
                    "chart": {
                        "type": "pie"
                    },
                    "labels": [item.get("name") for item in chart_items],
                    "title": {
                        "text": title
                    },
                    "legend": {
                        "show": True,
                        "position": "bottom"
                    },
                    "plotOptions": {
                        "pie": {
                            "expandOnClick": False
                        }
                    },
                    "noData": {
                        "text": "暂无数据"
                    }
                },
                "series": [item.get("count") for item in chart_items]
            }
        }

    def __build_recent_prize_summary(self, records: List[Dict]) -> Tuple[List[dict], List[dict]]:
        """构建今日/昨日奖品汇总"""
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        
        today_counts = Counter()
        yesterday_counts = Counter()
        
        for record in records:
            record_date = str(record.get("time") or "")[:10]
            prize = record.get("prize", "")
            if not prize:
                continue
            if record_date == today:
                today_counts[prize] += 1
            elif record_date == yesterday:
                yesterday_counts[prize] += 1
        
        return (
            self.__counter_to_items(today_counts),
            self.__counter_to_items(yesterday_counts)
        )

    @staticmethod
    def __counter_to_items(counter: Counter) -> List[dict]:
        if not counter:
            return [{"name": "无记录", "count": 0}]
        return [
            {"name": name, "count": count}
            for name, count in sorted(counter.items(), key=lambda x: x[1], reverse=True)
        ]

    def __spin_api(self) -> Dict[str, Any]:
        if self._lock.locked():
            return {"success": False, "message": "已有抽奖任务正在运行"}
        threading.Thread(target=self.__run_spin_task, daemon=True).start()
        return {"success": True, "message": "抽奖任务已启动"}

    def __history_api(self) -> Dict[str, Any]:
        history = self.get_data('history') or []
        return {"success": True, "data": history[:50]}

    def __get_site_cookie(self) -> str:
        try:
            site = SiteOper().get_by_domain(self.SITE_DOMAIN)
            if site and site.cookie:
                return site.cookie.strip()
            return ""
        except Exception as err:
            logger.error(f"读取藏宝阁站点Cookie失败: {err}")
            return ""

    def __get_records(self) -> List[Dict[str, Any]]:
        """获取历史记录"""
        records = self.get_data('history') or []
        if not isinstance(records, list):
            return []
        formatted = []
        for r in records[:30]:
            formatted.append({
                "time": r.get("time", ""),
                "success": r.get("success", False),
                "is_win": r.get("is_win", False),
                "prize": r.get("prize", ""),
                "bonus": r.get("bonus", 0),
                "message": r.get("message", ""),
            })
        return formatted

    def __fetch_user_info(self) -> Dict[str, Any]:
        """从首页解析用户信息（魔力值、上传、下载等）"""
        info = {
            "bonus": "-",
            "upload": "-",
            "download": "-",
            "username": "-",
            "ratio": "-",
            "invite": "-",
            "repair_card": "-",
            "updated_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "message": ""
        }
        
        cookie = self._cookie or self.__get_site_cookie()
        if not cookie:
            info["message"] = "缺少 Cookie，无法获取用户信息"
            logger.warn("获取用户信息失败：缺少 Cookie")
            return info

        headers = {
            'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6',
            'referer': self.REFERER,
            'sec-ch-ua': '"Microsoft Edge";v="151", "Chromium";v="151", "Not=A?Brand";v="99"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"macOS"',
            'sec-fetch-dest': 'document',
            'sec-fetch-mode': 'navigate',
            'sec-fetch-site': 'same-origin',
            'sec-fetch-user': '?1',
            'upgrade-insecure-requests': '1',
            'user-agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0'
        }

        try:
            response = requests.get(
                self.INDEX_URL,
                headers=headers,
                cookies=self.__cookie_to_dict(cookie),
                timeout=self.REQUEST_TIMEOUT,
                verify=False
            )
        except Exception as err:
            info["message"] = f"获取用户信息失败：{err}"
            logger.error(f"获取用户信息异常：{err}")
            return info

        if response.status_code != 200:
            info["message"] = f"获取用户信息失败：HTTP {response.status_code}"
            return info

        try:
            html_content = response.text
            
            # 提取用户名
            username_match = re.search(r'<a\s+href="[^"]*userdetails\.php\?id=\d+"[^>]*><b>([^<]+)</b></a>', html_content)
            if username_match:
                info["username"] = username_match.group(1)
            
            # 提取魔力值 - 格式: 魔力值 [使用]: 1,893,248.9
            bonus_match = re.search(r'魔力值\s*</font>\s*\[<a[^>]*>使用</a>\]:\s*([\d,]+\.?\d*)', html_content)
            if not bonus_match:
                bonus_match = re.search(r'魔力值[^:]*:\s*([\d,]+\.?\d*)', html_content)
            if bonus_match:
                info["bonus"] = bonus_match.group(1).strip()
                # 更新上次魔力值
                try:
                    self._last_bonus = float(bonus_match.group(1).replace(',', ''))
                except:
                    pass
            
            # 提取上传量 - 格式: 上传量: 7.119 TB
            upload_match = re.search(r'上传量:</font>\s*([\d.]+\s*(?:TB|GB|MB))', html_content)
            if upload_match:
                info["upload"] = upload_match.group(1).strip()
            
            # 提取下载量 - 格式: 下载量: 800.00 GB
            download_match = re.search(r'下载量:</font>\s*([\d.]+\s*(?:TB|GB|MB))', html_content)
            if download_match:
                info["download"] = download_match.group(1).strip()
            
            # 提取分享率 - 格式: 分享率: 9.113
            ratio_match = re.search(r'分享率:</font>\s*([\d.]+)', html_content)
            if ratio_match:
                info["ratio"] = ratio_match.group(1).strip()
            
            # 提取邀请数 - 格式: 邀请 [发送]: 1(200)
            invite_match = re.search(r'邀请\s*</font>\s*\[<a[^>]*>发送</a>\]:\s*(\d+)\((\d+)\)', html_content)
            if invite_match:
                info["invite"] = f"{invite_match.group(1)}({invite_match.group(2)})"
            
            # 提取补签卡 - 格式: 补签卡: 1173
            repair_match = re.search(r'补签卡[^:]*:\s*([\d,]+)', html_content)
            if repair_match:
                info["repair_card"] = repair_match.group(1).strip()
            
            info["message"] = "用户信息获取成功"
            logger.info(f"用户信息解析成功: 魔力={info['bonus']}, 上传={info['upload']}, 下载={info['download']}")
                
        except Exception as e:
            info["message"] = f"解析用户信息失败：{e}"
            logger.error(f"解析用户信息异常：{e}")

        return info

    def __send_notification(self, title: str, text: str):
        if self._notify:
            try:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title=f"【藏宝阁抽奖】{title}",
                    text=text
                )
                logger.info(f"📨 通知已发送: {title}")
            except Exception as e:
                logger.error(f"发送通知失败: {e}")

    def __save_history(self, record: Dict[str, Any]):
        history = self.get_data('history') or []
        if not isinstance(history, list):
            history = []
        history.insert(0, {
            "time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "success": record.get('success', False),
            "is_win": record.get('is_win', False),
            "prize": record.get('prize', ''),
            "bonus": record.get('bonus', 0),
            "message": record.get('message', ''),
        })
        if len(history) > self.MAX_HISTORY:
            history = history[:self.MAX_HISTORY]
        self.save_data('history', history)

    def __save_batch_history(self, batch_results: List[Dict[str, Any]]):
        """批量保存历史记录"""
        for result in batch_results:
            self.__save_history(result)

    @eventmanager.register(EventType.PluginAction)
    def handle_plugin_action(self, event: Event):
        if not event:
            return
        event_data = event.event_data
        if not event_data or event_data.get("action") != "cbg_spin_run":
            return
        if self._lock.locked():
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title="【藏宝阁抽奖】",
                text="已有抽奖任务正在运行，请等待完成"
            )
            return
        threading.Thread(target=self.__run_spin_task, daemon=True).start()

    def __run_spin_task(self):
        if not self._lock.acquire(blocking=False):
            logger.warning("抽奖任务已在运行，跳过本次执行")
            return

        try:
            logger.info("🔄 开始执行藏宝阁抽奖任务")

            cookie = self._cookie or self.__get_site_cookie()
            if not cookie:
                logger.error("Cookie未获取到，任务终止")
                self.__send_notification("任务失败", "未找到藏宝阁站点Cookie")
                return

            try:
                max_spins = int(self._max_spins)
            except (TypeError, ValueError):
                max_spins = 10
                logger.warning(f"抽奖次数配置无效，使用默认值: {max_spins}")

            # 检查魔力值是否足够
            user_info = self.__fetch_user_info()
            try:
                current_bonus = float(user_info.get('bonus', '0').replace(',', ''))
                total_cost = max_spins * self.COST_MP
                if current_bonus < total_cost:
                    error_msg = f"魔力值不足！需要 {total_cost:,.0f}，当前 {current_bonus:,.0f}"
                    logger.error(error_msg)
                    self.__send_notification("任务失败", error_msg)
                    return
                logger.info(f"💰 当前魔力值: {current_bonus:,.1f}，预计消耗: {total_cost:,.0f}")
            except:
                pass

            success_count = 0
            fail_count = 0
            total_bonus = 0
            results = []
            prize_summary = defaultdict(int)
            win_count = 0
            total_spins_done = 0

            remaining = max_spins
            batch_num = 0
            
            while remaining > 0:
                # 决定本次抽奖次数：剩余>=10时用10连，否则单抽
                if remaining >= self.BATCH_SIZE:
                    batch_size = self.BATCH_SIZE
                else:
                    batch_size = 1
                
                batch_num += 1
                logger.info(f"  🎰 第 {batch_num} 批：执行 {batch_size} 连抽 (剩余 {remaining} 次)...")

                batch_result = self.__do_spin_batch(cookie, batch_size)
                
                if batch_result.get('success'):
                    batch_results = batch_result.get('results', [])
                    batch_success = len(batch_results)
                    success_count += batch_success
                    total_spins_done += batch_success
                    
                    # 保存批量结果到历史
                    self.__save_batch_history(batch_results)
                    
                    for prize in batch_results:
                        bonus = prize.get('bonus', 0)
                        if bonus > 0:
                            total_bonus += bonus
                        prize_name = prize.get('prize', '未知')
                        prize_summary[prize_name] += 1
                        
                        if prize.get('is_win', False):
                            win_count += 1
                        
                        logger.info(f"    ✅ {prize.get('message')}")
                        results.append(f"✅ {prize.get('message')}")
                    
                    logger.info(f"    📊 本批 {batch_size} 连抽完成，成功 {batch_success} 次")
                else:
                    error_msg = batch_result.get('message', '未知错误')
                    # 如果是批量失败，记录失败次数
                    fail_count += batch_size
                    logger.warning(f"    ❌ {batch_size} 连抽失败: {error_msg}")
                    results.append(f"❌ {batch_size} 连抽失败: {error_msg}")

                remaining -= batch_size
                
                if remaining > 0:
                    delay = random.randint(1, 3)
                    logger.info(f"    ⏱️ 冷却 {delay} 秒...")
                    time.sleep(delay)

            # 重新获取用户信息计算实际收益
            user_info = self.__fetch_user_info()
            try:
                current_bonus = float(user_info.get('bonus', '0').replace(',', ''))
                if self._last_bonus > 0 and current_bonus > 0:
                    actual_gain = current_bonus - self._last_bonus
                    if actual_gain > 0 and total_bonus == 0:
                        total_bonus = actual_gain
            except:
                pass

            prize_text = "\n".join([f"  {p}: {c}次" for p, c in sorted(prize_summary.items(), key=lambda x: x[1], reverse=True)]) if prize_summary else "  无"

            summary = f"🎯 共抽奖 {max_spins} 次\n"
            summary += f"✅ 成功 {success_count} 次，❌ 失败 {fail_count} 次\n"
            summary += f"🏆 中奖 {win_count} 次\n"
            summary += f"💰 累计获得魔力 {total_bonus:,.0f}\n"
            summary += f"📊 奖品统计：\n{prize_text}"

            self.__send_notification(
                "任务完成" if fail_count == 0 else "任务部分完成",
                summary
            )

            logger.info(f"📊 任务完成: 成功 {success_count}，失败 {fail_count}，总魔力 {total_bonus}")

        except Exception as e:
            logger.error(f"抽奖任务异常: {e}")
            self.__send_notification("任务异常", str(e)[:200])
        finally:
            self._lock.release()

    def __do_spin_batch(self, cookie: str, count: int) -> Dict[str, Any]:
        """执行批量抽奖（1次或10次）"""
        try:
            headers = {
                'accept': '*/*',
                'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6',
                'content-type': 'application/json',
                'origin': self.BASE_URL,
                'referer': self.REFERER,
                'sec-ch-ua': '"Not;A=Brand";v="8", "Chromium";v="150", "Microsoft Edge";v="150"',
                'sec-ch-ua-mobile': '?0',
                'sec-ch-ua-platform': '"macOS"',
                'sec-fetch-dest': 'empty',
                'sec-fetch-mode': 'cors',
                'sec-fetch-site': 'same-origin',
                'user-agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0'
            }

            payload = json.dumps({"count": count})

            response = requests.post(
                self.SPIN_URL,
                headers=headers,
                cookies=self.__cookie_to_dict(cookie),
                data=payload,
                timeout=self.REQUEST_TIMEOUT,
                verify=False
            )

            if response.status_code != 200:
                return {'success': False, 'message': f'HTTP {response.status_code}'}

            data = response.json()
            logger.info(f"API响应: {json.dumps(data, ensure_ascii=False)}")

            status = data.get('status')
            if status == 'success':
                prize_ids = data.get('prize_ids', [])
                if not prize_ids:
                    return {'success': False, 'message': '未返回奖品ID'}
                
                # 解析每个奖品ID
                results = []
                for prize_id in prize_ids:
                    prize_info = self.PRIZE_MAP.get(prize_id, {"name": f"未知奖品(ID:{prize_id})", "type": "unknown", "value": 0})
                    
                    prize_name = prize_info.get('name', '未知奖品')
                    prize_type = prize_info.get('type', 'unknown')
                    bonus = prize_info.get('value', 0)
                    
                    is_win = prize_type != 'none'
                    
                    if not is_win:
                        message = '谢谢参与 🍀'
                    elif prize_type == 'magic':
                        message = f'获得 {prize_name} 💎'
                    elif prize_type == 'upload':
                        message = f'获得 {prize_name} ⬆️'
                    elif prize_type == 'item':
                        message = f'获得 {prize_name} 🎁'
                    elif prize_type == 'vip':
                        message = f'获得 {prize_name} 👑'
                    else:
                        message = f'获得 {prize_name}'
                    
                    results.append({
                        'success': True,
                        'is_win': is_win,
                        'prize': prize_name,
                        'bonus': bonus,
                        'message': message
                    })
                
                return {
                    'success': True,
                    'results': results
                }
            else:
                message = data.get('message', data.get('msg', f'状态: {status}'))
                return {'success': False, 'message': message}

        except requests.exceptions.Timeout:
            logger.error("请求超时")
            return {'success': False, 'message': '请求超时'}
        except requests.exceptions.ConnectionError:
            logger.error("连接失败")
            return {'success': False, 'message': '连接失败'}
        except Exception as e:
            logger.error(f"抽奖异常: {e}")
            return {'success': False, 'message': str(e)[:50]}

    def __cookie_to_dict(self, cookie: str) -> Dict[str, str]:
        cookies = {}
        if not cookie:
            return cookies
        for item in cookie.split(';'):
            item = item.strip()
            if '=' in item:
                key, value = item.split('=', 1)
                cookies[key.strip()] = value.strip()
        return cookies