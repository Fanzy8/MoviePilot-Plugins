# __init__.py - 农场自动化通用插件（支持多站点）
import json
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime

import requests
import urllib3
from apscheduler.triggers.interval import IntervalTrigger
from urllib3.exceptions import InsecureRequestWarning

from app.core.event import Event, eventmanager
from app.db.site_oper import SiteOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.schemas.types import EventType

from .sites import SITE_CONFIGS, SITE_OPTIONS, get_site_config, get_all_site_configs, FarmSiteConfig

urllib3.disable_warnings(InsecureRequestWarning)


class FarmAuto(_PluginBase):
    """农场自动化插件 - 支持多站点"""
    
    plugin_name = "农场自动化"
    plugin_desc = "适配多个站点农场，建议选智能模式，否则后果自负"
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "1.0"
    plugin_author = "bfjy"
    author_url = "https://bfjy2024.github.io/bfjy"
    plugin_config_prefix = "farmauto_"
    plugin_order = 30
    auth_level = 2

    # 模式常量
    MODE_SMART = "smart"
    MODE_HARVEST = "harvest"

    _enabled = False
    _site_ids = []  # 选中的站点ID列表
    _mode = MODE_SMART
    _interval_minutes = 61
    _harvest_interval_minutes = 61
    _expire_threshold_minutes = 120
    _notify = True
    _run_once = False
    _task_lock = threading.Lock()
    
    _stats = {
        "last_run": None,
        "last_result": "",
        "total_profit": 0,
        "total_trades": 0,
        "history": []
    }
    _market_prices = {}  # {site_id: {crop_key: price}}
    
    def init_plugin(self, config: dict = None):
        config = config or {}

        self._enabled = bool(config.get("enabled", False))
        self._site_ids = config.get("site_ids", ["playlet"])
        if isinstance(self._site_ids, str):
            self._site_ids = [self._site_ids]
        self._mode = config.get("mode", self.MODE_SMART)
        self._interval_minutes = self.__safe_int(config.get("interval_minutes"), 15, min_value=1)
        self._harvest_interval_minutes = self.__safe_int(config.get("harvest_interval_minutes"), 60, min_value=5)
        self._expire_threshold_minutes = self.__safe_int(config.get("expire_threshold_minutes"), 120, min_value=10)
        self._notify = bool(config.get("notify", True))
        self._run_once = bool(config.get("run_once", False))

        stats = config.get("stats", {})
        if stats:
            self._stats.update(stats)
        
        market_prices = config.get("market_prices", {})
        if market_prices:
            self._market_prices = market_prices

        site_names = self.__get_site_names()
        mode_name = "智能交易" if self._mode == self.MODE_SMART else "自动收获"
        
        logger.info(
            f"农场自动化插件初始化完成：站点={site_names}, 模式={mode_name}, 启用={self._enabled}"
        )

        if self._run_once:
            self._run_once = False
            self.update_config({
                "enabled": self._enabled,
                "site_ids": self._site_ids,
                "mode": self._mode,
                "interval_minutes": self._interval_minutes,
                "harvest_interval_minutes": self._harvest_interval_minutes,
                "expire_threshold_minutes": self._expire_threshold_minutes,
                "notify": self._notify,
                "run_once": False,
                "stats": self._stats,
                "market_prices": self._market_prices
            })
            logger.info("收到配置页立即运行请求，后台启动农场任务")
            threading.Thread(target=self.run_farm_task, daemon=True).start()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [
            {
                "cmd": "/farmauto_run",
                "event": EventType.PluginAction,
                "desc": "立即运行农场自动化",
                "category": "站点",
                "data": {"action": "farmauto_run"}
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/FarmAuto/run",
                "endpoint": self.run_once_api,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即运行农场自动化",
            },
            {
                "path": "/FarmAuto/stats",
                "endpoint": self.get_stats_api,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取农场统计数据",
            },
            {
                "path": "/FarmAuto/prices",
                "endpoint": self.get_prices_api,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取市场价格",
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled or not self._site_ids:
            return []

        try:
            interval = self._harvest_interval_minutes if self._mode == self.MODE_HARVEST else self._interval_minutes
            trigger = IntervalTrigger(minutes=interval)
            mode_name = "自动收获" if self._mode == self.MODE_HARVEST else "智能交易"
            site_names = self.__get_site_names()
            logger.info(f"{mode_name}模式：每{interval}分钟执行一次，目标站点：{site_names}")
            return [{
                "id": "FarmAuto",
                "name": "农场自动化",
                "trigger": trigger,
                "func": self.run_farm_task,
                "kwargs": {}
            }]
        except Exception as e:
            logger.warn(f"间隔触发器创建失败：{e}")
            return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        selected_sites = self._site_ids if self._site_ids else ["playlet"]
        
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
                                        "props": {"model": "enabled", "label": "启用插件"}
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "notify", "label": "发送通知"}
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
                                            "model": "run_once",
                                            "label": "立即运行一次",
                                            "hint": "保存配置后执行农场任务"
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
                                        "component": "VSelect",
                                        "props": {
                                            "model": "site_ids",
                                            "label": "目标站点（多选）",
                                            "items": SITE_OPTIONS,
                                            "multiple": True,
                                            "chips": True,
                                            "hint": "选择要自动化的站点，支持多选"
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "mode",
                                            "label": "运行模式",
                                            "items": [
                                                {"title": "🧠 智能交易模式", "value": "smart"},
                                                {"title": "🌾 自动收获模式", "value": "harvest"},
                                            ]
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
                                        "component": "VTextField",
                                        "props": {
                                            "model": "interval_minutes",
                                            "label": "智能交易间隔（分钟）",
                                            "type": "number",
                                            "min": 1,
                                            "hint": "智能交易模式生效"
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
                                            "model": "harvest_interval_minutes",
                                            "label": "自动收获间隔（分钟）",
                                            "type": "number",
                                            "min": 5,
                                            "hint": "自动收获模式生效"
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
                                        "component": "VTextField",
                                        "props": {
                                            "model": "expire_threshold_minutes",
                                            "label": "过期预警阈值（分钟）",
                                            "type": "number",
                                            "min": 10,
                                            "hint": "剩余时间少于该值时强制出售"
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
                                            "text": "📌 支持站点：PlayLet、NovaHD、好学、包子\n"
                                                   "🧠 智能交易：价格高于成本时自动收获→种植/养殖→出售\n"
                                                   "🌾 自动收获：一键收获 → 自动种植/养殖 → 检查仓库按价格出售\n"
                                                   "💡 多站点模式：按顺序依次处理每个选中的站点\n"
                                                   "🔑 Cookie 自动从站点管理读取，无需手动配置"
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
            "site_ids": selected_sites,
            "mode": self._mode,
            "interval_minutes": self._interval_minutes,
            "harvest_interval_minutes": self._harvest_interval_minutes,
            "expire_threshold_minutes": self._expire_threshold_minutes,
            "notify": self._notify,
            "run_once": False,
            "stats": self._stats,
            "market_prices": self._market_prices
        }

    def get_page(self) -> List[dict]:
        stats = self._stats
        market_prices = self._market_prices
        site_configs = self.__get_selected_site_configs()
        
        last_run = stats.get("last_run", "")
        if last_run:
            try:
                last_run = datetime.fromtimestamp(last_run).strftime("%Y-%m-%d %H:%M:%S")
            except:
                last_run = str(last_run)

        history = stats.get("history", [])[-10:]
        site_names = self.__get_site_names()
        currency = self.__get_site_currency()

        price_sections = []
        for site_config in site_configs:
            site_prices = market_prices.get(site_config.site_id, {})
            price_items = []
            for key, crop in site_config.crops.items():
                market_price = site_prices.get(key, 0)
                cost = crop["cost"]
                profit = market_price - cost if market_price > 0 else 0
                status = "🟢 可交易" if profit > 0 else "🔴 观望"
                animal_tag = " 🐾" if key.startswith("animal_") else ""
                double_tag = " 🌟双倍" if site_config.has_double_harvest and profit > 0 else ""
                price_items.append({
                    "component": "VCol",
                    "props": {"cols": 6, "md": 3},
                    "content": [
                        {
                            "component": "div",
                            "props": {"class": "pa-2", "style": "background: #f5f5f5; border-radius: 8px; text-align: center;"},
                            "content": [
                                {"component": "div", "props": {"class": "text-h6"}, "text": f"{crop['name']}{double_tag}{animal_tag}"},
                                {"component": "div", "props": {"class": "text-caption"}, "text": f"成本: {cost}"},
                                {"component": "div", "props": {"class": "text-h6", "style": f"color: {'#4caf50' if profit > 0 else '#f44336'};"}, "text": f"{market_price}"},
                                {"component": "div", "props": {"class": "text-caption"}, "text": f"利润: {profit} ({status})"}
                            ]
                        }
                    ]
                })
            
            price_sections.append({
                "component": "div",
                "content": [
                    {
                        "component": "div",
                        "props": {"class": "text-subtitle-1 mt-3 mb-2"},
                        "text": f"📊 {site_config.site_name} 市场价格"
                    },
                    {
                        "component": "VRow",
                        "content": price_items if price_items else [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "div",
                                        "props": {"class": "text-center text-medium-emphasis pa-4"},
                                        "text": "暂无市场价格数据"
                                    }
                                ]
                            }
                        ]
                    }
                ]
            })

        mode_name = "智能交易模式" if self._mode == self.MODE_SMART else "自动收获模式"
        
        return [
            {
                "component": "VCard",
                "props": {"variant": "tonal", "class": "mb-4"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "text": f"🌾 农场自动化数据面板"
                    },
                    {
                        "component": "VCardText",
                        "content": [
                            {
                                "component": "VRow",
                                "props": {"class": "mb-4"},
                                "content": [
                                    self.__stat_card("📋", "运行模式", mode_name, "", "primary"),
                                    self.__stat_card("🌐", "目标站点", site_names, "", "info"),
                                    self.__stat_card("💰", "总利润", f"{stats.get('total_profit', 0):,}", currency, "success"),
                                    self.__stat_card("🔄", "总交易", f"{stats.get('total_trades', 0)}", "次", "warning"),
                                    self.__stat_card("📈", "监控站点", f"{len(site_configs)}", "个", "secondary"),
                                    self.__stat_card("🕐", "最后运行", last_run or "-", "", "grey"),
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VCardText",
                        "content": price_sections if price_sections else [
                            {
                                "component": "div",
                                "props": {"class": "text-center text-medium-emphasis pa-4"},
                                "text": "请先在配置中选择目标站点"
                            }
                        ]
                    }
                ]
            },
            {
                "component": "VCard",
                "props": {"variant": "tonal"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "text": "📋 执行记录"
                    },
                    {
                        "component": "VCardText",
                        "content": [
                            {
                                "component": "VTable",
                                "props": {"hover": True, "dense": True},
                                "content": [
                                    {
                                        "component": "thead",
                                        "content": [
                                            {
                                                "component": "tr",
                                                "content": [
                                                    {"component": "th", "text": "时间"},
                                                    {"component": "th", "text": "站点"},
                                                    {"component": "th", "text": "操作"},
                                                    {"component": "th", "text": "利润"},
                                                    {"component": "th", "text": "状态"},
                                                ]
                                            }
                                        ]
                                    },
                                    {
                                        "component": "tbody",
                                        "content": self.__history_rows(history) if history else [
                                            {
                                                "component": "tr",
                                                "content": [
                                                    {
                                                        "component": "td",
                                                        "props": {"colspan": 5, "class": "text-center text-medium-emphasis"},
                                                        "text": "暂无执行记录"
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
            }
        ]

    def stop_service(self):
        pass

    # ==================== 辅助方法 ====================
    
    @staticmethod
    def __stat_card(icon: str, label: str, value: str, suffix: str, color: str) -> Dict[str, Any]:
        return {
            "component": "VCol",
            "props": {"cols": 6, "md": 4, "lg": 2},
            "content": [
                {
                    "component": "VCard",
                    "props": {"color": color, "variant": "tonal", "class": "pa-3"},
                    "content": [
                        {
                            "component": "div",
                            "props": {"class": "d-flex align-center"},
                            "content": [
                                {"component": "div", "props": {"class": "text-h4 mr-2"}, "text": icon},
                                {
                                    "component": "div",
                                    "content": [
                                        {"component": "div", "props": {"class": "text-caption text-medium-emphasis"}, "text": label},
                                        {"component": "div", "props": {"class": "text-h6 font-weight-bold"}, "text": f"{value}{suffix}"}
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ]
        }

    def __history_rows(self, history: list) -> List[Dict[str, Any]]:
        rows = []
        for record in reversed(history):
            time_str = record.get("time", "")
            if isinstance(time_str, (int, float)):
                try:
                    time_str = datetime.fromtimestamp(time_str).strftime("%Y-%m-%d %H:%M:%S")
                except:
                    time_str = str(time_str)
            
            status_color = "success" if record.get("status") == "completed" else "warning"
            status_text = "✅ 成功" if record.get("status") == "completed" else "⚠️ 部分成功"
            
            rows.append({
                "component": "tr",
                "content": [
                    {"component": "td", "text": time_str},
                    {"component": "td", "text": record.get("site", "-")},
                    {"component": "td", "text": record.get("action", "-")},
                    {"component": "td", "text": f"{record.get('profit', 0):,}" if record.get('profit', 0) > 0 else "-"},
                    {
                        "component": "td",
                        "content": [
                            {
                                "component": "VChip",
                                "props": {"color": status_color, "size": "small", "variant": "tonal"},
                                "text": status_text
                            }
                        ]
                    }
                ]
            })
        return rows

    def __get_site_names(self) -> str:
        configs = self.__get_selected_site_configs()
        return ", ".join([c.site_name for c in configs]) if configs else "未选择"

    def __get_site_currency(self) -> str:
        configs = self.__get_selected_site_configs()
        return configs[0].currency if configs else "魔力"

    def __get_selected_site_configs(self) -> List[FarmSiteConfig]:
        configs = []
        for site_id in self._site_ids:
            config = get_site_config(site_id)
            if config:
                configs.append(config)
        return configs

    # ==================== API方法 ====================
    
    def run_once_api(self) -> Dict[str, Any]:
        if self._task_lock.locked():
            return {"success": False, "message": "已有任务正在执行"}
        logger.info("收到API立即执行请求")
        threading.Thread(target=self.run_farm_task, daemon=True).start()
        return {"success": True, "message": "任务已开始"}

    def get_stats_api(self) -> Dict[str, Any]:
        return {"success": True, "data": {"stats": self._stats, "market_prices": self._market_prices}}

    def get_prices_api(self) -> Dict[str, Any]:
        return {"success": True, "data": {"market_prices": self._market_prices}}

    @eventmanager.register(EventType.PluginAction)
    def run_once_command(self, event: Event = None):
        event_data = event.event_data if event else {}
        if not event_data:
            return
        
        action = event_data.get("action")
        if action == "farmauto_run":
            if self._task_lock.locked():
                self.post_message(
                    channel=event_data.get("channel"),
                    userid=event_data.get("user"),
                    mtype=NotificationType.Plugin,
                    title="【农场自动化】",
                    text="已有任务正在执行"
                )
                return
            logger.info("收到命令立即执行请求")
            threading.Thread(target=self.run_farm_task, daemon=True).start()
            self.post_message(
                channel=event_data.get("channel"),
                userid=event_data.get("user"),
                mtype=NotificationType.Plugin,
                title="【农场自动化】",
                text="任务已开始"
            )

    # ==================== 核心任务 ====================
    
    def __get_headers(self) -> Dict[str, str]:
        return {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        }

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

    @staticmethod
    def __safe_int(value: Any, default: int, min_value: int = None) -> int:
        try:
            result = int(value) if value is not None else default
        except (ValueError, TypeError):
            result = default
        if min_value is not None:
            result = max(result, min_value)
        return result

    def __parse_expire_time(self, expire_str: str) -> Optional[int]:
        if not expire_str or expire_str == "已过期":
            return 0
        
        try:
            days = hours = minutes = 0
            day_match = re.search(r'(\d+)天', expire_str)
            if day_match:
                days = int(day_match.group(1))
            hour_match = re.search(r'(\d+)小时', expire_str)
            if hour_match:
                hours = int(hour_match.group(1))
            minute_match = re.search(r'(\d+)分钟', expire_str)
            if minute_match:
                minutes = int(minute_match.group(1))
            return days * 24 * 60 + hours * 60 + minutes
        except Exception:
            return None

    # ==================== Cookie获取 ====================
    
    def __get_site_cookie(self, site_config: FarmSiteConfig) -> str:
        try:
            site_oper = SiteOper()
            
            for domain in site_config.domains:
                try:
                    site = site_oper.get_by_domain(domain)
                    if site and site.cookie:
                        logger.info(f"✅ 从站点管理获取到 {site_config.site_name} Cookie (域名: {domain})")
                        return site.cookie.strip()
                except Exception as e:
                    logger.debug(f"查询域名 {domain} 失败: {e}")
            
            try:
                sites = site_oper.list()
                for site in sites:
                    if not site or not site.cookie:
                        continue
                    site_domain = getattr(site, 'domain', '') or getattr(site, 'url', '')
                    if not site_domain:
                        continue
                    clean_domain = site_domain.replace('https://', '').replace('http://', '').split('/')[0]
                    for target_domain in site_config.domains:
                        target_clean = target_domain.replace('https://', '').replace('http://', '').split('/')[0]
                        if target_clean in clean_domain or clean_domain in target_clean:
                            logger.info(f"✅ 从站点列表匹配到 {site_config.site_name} Cookie (站点: {clean_domain})")
                            return site.cookie.strip()
            except Exception as e:
                logger.debug(f"遍历站点列表失败: {e}")
            
            logger.warning(f"⚠️ 未找到 {site_config.site_name} 站点Cookie")
            return ""
            
        except Exception as err:
            logger.error(f"读取 {site_config.site_name} Cookie失败: {err}")
            return ""

    # ==================== 页面获取 ====================
    
    def __fetch_farm_page(self, cookie: str, site_config: FarmSiteConfig) -> Optional[Dict[str, Any]]:
        try:
            response = requests.get(
                site_config.get_farm_url(),
                headers=self.__get_headers(),
                cookies=self.__cookie_to_dict(cookie),
                timeout=15,
                verify=False
            )

            if response.status_code != 200:
                logger.warn(f"获取 {site_config.site_name} 农场页面失败：HTTP {response.status_code}")
                return None

            html = response.text
            
            if not site_config.check_auth(html):
                logger.warn(f"{site_config.site_name} Cookie已失效，需要重新登录")
                return None

            result = {
                "market_prices": site_config.parse_market_prices(html),
                "crop_status": site_config.parse_crop_status(html)
            }
            
            logger.info(f"✅ {site_config.site_name} 解析到 {len(result['market_prices'])} 种物品价格")
            return result

        except requests.Timeout:
            logger.error(f"{site_config.site_name} 获取农场页面超时")
            return None
        except Exception as e:
            logger.error(f"{site_config.site_name} 获取农场页面异常：{e}")
            return None

    def __fetch_farm_page_html(self, cookie: str, site_config: FarmSiteConfig) -> Optional[str]:
        try:
            response = requests.get(
                site_config.get_farm_url(),
                headers=self.__get_headers(),
                cookies=self.__cookie_to_dict(cookie),
                timeout=15,
                verify=False
            )
            if response.status_code == 200:
                return response.text
            return None
        except Exception:
            return None

    def __fetch_warehouse(self, cookie: str, site_config: FarmSiteConfig) -> List[Dict[str, Any]]:
        try:
            response = requests.get(
                site_config.get_warehouse_url(),
                headers=self.__get_headers(),
                cookies=self.__cookie_to_dict(cookie),
                timeout=15,
                verify=False
            )

            if response.status_code != 200:
                return []

            return site_config.parse_warehouse_items(response.text)

        except Exception as e:
            logger.error(f"{site_config.site_name} 获取仓库信息异常：{e}")
            return []

    # ==================== 执行操作 ====================
    
    def __do_harvest(self, cookie: str, site_config: FarmSiteConfig, item_type: str, item_id: int) -> Dict[str, Any]:
        try:
            response = requests.get(
                site_config.get_harvest_url(item_type, item_id),
                headers=self.__get_headers(),
                cookies=self.__cookie_to_dict(cookie),
                timeout=15,
                verify=False,
                allow_redirects=True
            )

            if response.status_code != 200:
                return {"success": False, "message": f"HTTP {response.status_code}"}

            return site_config.parse_harvest_result(response.text)

        except Exception as e:
            return {"success": False, "message": str(e)}

    def __do_plant(self, cookie: str, site_config: FarmSiteConfig, item_type: str, item_id: int, action: str = "plant") -> Dict[str, Any]:
        try:
            if action == "breed":
                url = site_config.get_breed_url(item_type, item_id)
            else:
                url = site_config.get_plant_url(item_type, item_id)
            
            response = requests.get(
                url,
                headers=self.__get_headers(),
                cookies=self.__cookie_to_dict(cookie),
                timeout=15,
                verify=False,
                allow_redirects=True
            )

            if response.status_code != 200:
                return {"success": False, "message": f"HTTP {response.status_code}"}

            return site_config.parse_plant_result(response.text, action)

        except Exception as e:
            return {"success": False, "message": str(e)}

    def __do_sell(self, cookie: str, site_config: FarmSiteConfig, sell_key: str) -> Dict[str, Any]:
        try:
            response = requests.get(
                site_config.get_sell_url(sell_key),
                headers=self.__get_headers(),
                cookies=self.__cookie_to_dict(cookie),
                timeout=15,
                verify=False,
                allow_redirects=True
            )

            if response.status_code != 200:
                return {"success": False, "message": f"HTTP {response.status_code}"}

            return site_config.parse_sell_result(response.text)

        except Exception as e:
            return {"success": False, "message": str(e)}

    def __do_harvest_all(self, cookie: str, site_config: FarmSiteConfig) -> Dict[str, Any]:
        try:
            response = requests.get(
                site_config.get_harvest_all_url(),
                headers=self.__get_headers(),
                cookies=self.__cookie_to_dict(cookie),
                timeout=15,
                verify=False,
                allow_redirects=True
            )

            if response.status_code != 200:
                return {"success": False, "message": f"HTTP {response.status_code}"}

            html = response.text
            if "成功" in html or "收获" in html:
                return {"success": True, "message": "一键收获成功"}
            return {"success": False, "message": "一键收获失败"}

        except Exception as e:
            return {"success": False, "message": str(e)}

    # ==================== 模式运行 ====================
    
    def run_farm_task(self) -> Dict[str, Any]:
        if not self._task_lock.acquire(blocking=False):
            return {"status": "running", "message": "已有任务正在执行"}

        try:
            site_configs = self.__get_selected_site_configs()
            if not site_configs:
                result = {"status": "failed", "message": "未选择任何站点"}
                self.__finish_task(result)
                return result

            logger.info(f"=== 开始执行农场任务，目标站点：{len(site_configs)} 个 ===")

            all_results = []
            total_profit = 0
            total_trades = 0

            for site_config in site_configs:
                logger.info(f"--- 处理站点：{site_config.site_name} ---")
                
                cookie = self.__get_site_cookie(site_config)
                if not cookie:
                    logger.warn(f"跳过 {site_config.site_name}：未获取到Cookie")
                    all_results.append({
                        "site": site_config.site_name,
                        "result": {"status": "auth_failed", "message": "未获取到Cookie，请检查站点管理配置"}
                    })
                    continue

                try:
                    if self._mode == self.MODE_SMART:
                        result = self.__run_smart_mode(cookie, site_config)
                    else:
                        result = self.__run_harvest_mode(cookie, site_config)
                    
                    all_results.append({
                        "site": site_config.site_name,
                        "result": result
                    })
                    
                    if result.get("data", {}).get("total_profit"):
                        total_profit += result["data"]["total_profit"]
                    if result.get("data", {}).get("trades"):
                        total_trades += len(result["data"]["trades"])
                        
                except Exception as e:
                    logger.error(f"{site_config.site_name} 任务执行异常：{e}")
                    all_results.append({
                        "site": site_config.site_name,
                        "result": {"status": "error", "message": str(e)}
                    })
                
                time.sleep(2)

            self._stats["total_profit"] = self._stats.get("total_profit", 0) + total_profit
            self._stats["total_trades"] = self._stats.get("total_trades", 0) + total_trades
            self._stats["last_run"] = time.time()
            
            history = self._stats.get("history", [])
            history.append({
                "time": time.time(),
                "site": "多站点",
                "action": f"执行 {len(all_results)} 个站点",
                "profit": total_profit,
                "status": "completed"
            })
            if len(history) > 20:
                history = history[-20:]
            self._stats["history"] = history
            self._stats["last_result"] = f"完成 {len(all_results)} 个站点，总利润 {total_profit}"

            result = {
                "status": "completed",
                "message": f"完成 {len(all_results)} 个站点，总利润 {total_profit}",
                "data": {
                    "total_profit": total_profit,
                    "total_trades": total_trades,
                    "details": all_results
                }
            }
            
            self.__finish_task(result)
            logger.info(f"=== 农场任务完成，总利润 {total_profit} ===")
            return result

        except Exception as e:
            logger.error(f"农场任务异常：{e}")
            import traceback
            logger.error(traceback.format_exc())
            result = {"status": "error", "message": str(e)}
            self.__finish_task(result)
            return result
        finally:
            self._task_lock.release()

    def __run_smart_mode(self, cookie: str, site_config: FarmSiteConfig) -> Dict[str, Any]:
        """
        智能交易模式
        逻辑：
        1. 检查市场价格是否大于种植成本
        2. 查询仓库是否有该物品，有则按数量全部出售
        3. 检查是否可收获该物品，可收获则收获→种植→出售
        """
        logger.info(f"🧠 {site_config.site_name} 智能交易模式")

        # ========== 步骤1: 获取农场页面，获取市场价格和作物状态 ==========
        page_data = self.__fetch_farm_page(cookie, site_config)
        if not page_data:
            return {"status": "failed", "message": "获取农场页面失败"}

        market_prices = page_data.get("market_prices", {})
        crop_status = page_data.get("crop_status", {})
        
        if site_config.site_id not in self._market_prices:
            self._market_prices[site_config.site_id] = {}
        self._market_prices[site_config.site_id].update(market_prices)
        
        logger.info(f"📊 {site_config.site_name} 获取到 {len(market_prices)} 种物品的市场价格")

        # ========== 步骤2: 获取仓库物品 ==========
        warehouse_items = self.__fetch_warehouse(cookie, site_config)
        warehouse_map = {}  # {物品名称: {quantity, sell_key, crop_key, cost}}
        for item in warehouse_items:
            name = item.get("name", "")
            crop = site_config.get_crop_by_name(name)
            if crop:
                key, crop_data = crop
                warehouse_map[name] = {
                    "quantity": item.get("quantity", 1),
                    "sell_key": item.get("sell_key", ""),
                    "crop_key": key,
                    "cost": crop_data.get("cost", 0)
                }
        
        if warehouse_map:
            logger.info(f"📦 {site_config.site_name} 仓库有 {len(warehouse_map)} 种物品")

        # ========== 步骤3: 遍历每种作物/动物 ==========
        total_profit = 0
        trades = []
        action_summary = []

        for key, crop in site_config.crops.items():
            crop_name = crop["name"]
            cost = crop["cost"]
            market_price = market_prices.get(key, 0)
            
            # 判断是否有利可图：市场价格 > 成本
            if market_price <= cost:
                continue
            
            logger.info(f"💰 {site_config.site_name} {crop_name} 市场价格 {market_price} > 成本 {cost}")
            
            item_profit = 0
            item_trades = []
            
            # ========== 步骤3a: 出售仓库中的该物品 ==========
            if crop_name in warehouse_map:
                warehouse_info = warehouse_map[crop_name]
                quantity = warehouse_info["quantity"]
                sell_key = warehouse_info["sell_key"]
                
                if sell_key:
                    sold_count = 0
                    for i in range(quantity):
                        sell_result = self.__do_sell(cookie, site_config, sell_key)
                        if sell_result["success"]:
                            sold_count += 1
                            profit = market_price - cost
                            item_profit += profit
                            logger.info(f"✅ {site_config.site_name} 出售仓库 {crop_name} ({i+1}/{quantity})")
                        else:
                            logger.warn(f"❌ {site_config.site_name} 出售仓库 {crop_name} 失败：{sell_result.get('message')}")
                            break
                        time.sleep(0.3)
                    
                    if sold_count > 0:
                        item_trades.append({
                            "name": crop_name,
                            "source": "仓库",
                            "quantity": sold_count,
                            "profit": item_profit
                        })
                        action_summary.append(f"售出仓库{crop_name}x{sold_count}")
            
            # ========== 步骤3b: 检查是否可收获并处理 ==========
            can_harvest = crop_status.get(key, {}).get("can_harvest", False)
            
            if can_harvest:
                logger.info(f"🌾 {site_config.site_name} {crop_name} 可收获")
                
                # 收获
                harvest_result = self.__do_harvest(cookie, site_config, crop["type"], crop["id"])
                if harvest_result["success"]:
                    double_tag = "🌟" if harvest_result.get("double") else ""
                    logger.info(f"✅ {site_config.site_name} 收获 {crop_name} 成功{double_tag}")
                    
                    # 种植/养殖
                    action = crop.get("action", "plant")
                    action_name = "种植" if action == "plant" else "养殖"
                    plant_result = self.__do_plant(cookie, site_config, crop["type"], crop["id"], action)
                    
                    if plant_result["success"]:
                        logger.info(f"✅ {site_config.site_name} {action_name} {crop_name} 成功")
                        
                        # 出售收获的物品
                        farm_html = self.__fetch_farm_page_html(cookie, site_config)
                        sell_key = site_config.get_sell_key(farm_html or "", crop["type"], crop["id"])
                        if not sell_key:
                            sell_key = f"{crop['type']}_{crop['id']}"
                        
                        sell_result = self.__do_sell(cookie, site_config, sell_key)
                        if sell_result["success"]:
                            profit = market_price - cost
                            item_profit += profit
                            logger.info(f"✅ {site_config.site_name} 出售 {crop_name}，利润 {profit}")
                            
                            item_trades.append({
                                "name": crop_name,
                                "source": "收获",
                                "quantity": 1,
                                "double": harvest_result.get("double", False),
                                "profit": profit,
                                "action": action
                            })
                            double_info = "双倍" if harvest_result.get("double") else ""
                            action_summary.append(f"{action_name}{crop_name}，售出{double_info}")
                        else:
                            logger.warn(f"❌ {site_config.site_name} 出售 {crop_name} 失败：{sell_result.get('message')}")
                    else:
                        logger.debug(f"{site_config.site_name} {action_name} {crop_name} 失败（可能已种植）")
                else:
                    logger.warn(f"❌ {site_config.site_name} 收获 {crop_name} 失败：{harvest_result.get('message')}")
                
                time.sleep(1)
            
            # 汇总该物品的操作
            if item_trades:
                trades.extend(item_trades)
                total_profit += item_profit

        # ========== 更新统计 ==========
        if total_profit > 0:
            self._stats["total_profit"] = self._stats.get("total_profit", 0) + total_profit
            self._stats["total_trades"] = self._stats.get("total_trades", 0) + len(trades)
        
        self._stats["last_run"] = time.time()
        
        history = self._stats.get("history", [])
        history.append({
            "time": time.time(),
            "site": site_config.site_name,
            "mode": "智能交易",
            "action": f"交易 {len(trades)} 笔" if trades else "无交易",
            "profit": total_profit,
            "status": "completed" if trades else "partial"
        })
        if len(history) > 20:
            history = history[-20:]
        self._stats["history"] = history
        self._stats["last_result"] = f"智能交易完成，利润 {total_profit} {site_config.currency}"

        if not action_summary:
            action_summary.append("无可交易物品")

        logger.info(f"✅ {site_config.site_name} 智能交易完成，总利润 {total_profit} {site_config.currency}")
        
        return {
            "status": "completed" if trades else "partial",
            "message": f"智能交易完成 {len(trades)} 笔交易，总利润 {total_profit} {site_config.currency}",
            "data": {
                "trades": trades,
                "total_profit": total_profit,
                "market_prices": market_prices,
                "action_summary": action_summary,
                "currency": site_config.currency,
                "site_name": site_config.site_name,
                "mode": "智能交易"
            }
        }

    def __run_harvest_mode(self, cookie: str, site_config: FarmSiteConfig) -> Dict[str, Any]:
        """
        自动收获模式
        逻辑：
        1. 一键收获所有成熟作物/动物
        2. 检查每种作物/动物是否可种植/养殖，如果可以则种植/养殖
        3. 检查仓库物品，如果市场价格高于成本则出售，数量为n则出售n次
        """
        logger.info(f"🌾 {site_config.site_name} 自动收获模式")

        action_summary = []
        total_profit = 0
        trades = []

        # ========== 步骤1: 一键收获 ==========
        harvest_all_result = self.__do_harvest_all(cookie, site_config)
        if harvest_all_result["success"]:
            logger.info(f"✅ {site_config.site_name} 一键收获完成")
            # 不添加到 action_summary，避免通知中出现
        else:
            logger.warn(f"{site_config.site_name} 一键收获失败：{harvest_all_result.get('message')}")

        # ========== 步骤2: 获取农场页面，检查可种植/养殖状态 ==========
        page_data = self.__fetch_farm_page(cookie, site_config)
        if page_data:
            market_prices = page_data.get("market_prices", {})
            crop_status = page_data.get("crop_status", {})
            
            if site_config.site_id not in self._market_prices:
                self._market_prices[site_config.site_id] = {}
            self._market_prices[site_config.site_id].update(market_prices)
            
            logger.info(f"📊 {site_config.site_name} 获取到 {len(market_prices)} 种物品的市场价格")
            
            # ========== 步骤3: 检查每种作物/动物是否可种植/养殖 ==========
            plant_actions = []
            for key, crop in site_config.crops.items():
                status = crop_status.get(key, {})
                can_harvest = status.get("can_harvest", False)
                
                if not can_harvest:
                    action = crop.get("action", "plant")
                    action_name = "种植" if action == "plant" else "养殖"
                    
                    logger.info(f"🌱 {site_config.site_name} 尝试{action_name} {crop['name']}...")
                    plant_result = self.__do_plant(cookie, site_config, crop["type"], crop["id"], action)
                    if plant_result["success"]:
                        logger.info(f"✅ {site_config.site_name} {action_name} {crop['name']} 成功")
                        plant_actions.append(f"{action_name}{crop['name']}")
                    else:
                        logger.debug(f"{site_config.site_name} {action_name} {crop['name']} 失败（可能已种植）")
                    time.sleep(0.5)
            
            if plant_actions:
                action_summary.extend(plant_actions)
        else:
            market_prices = self._market_prices.get(site_config.site_id, {})
            logger.warn(f"{site_config.site_name} 获取农场页面失败，跳过种植检查")

        # ========== 步骤4: 检查仓库，按价格出售 ==========
        warehouse_items = self.__fetch_warehouse(cookie, site_config)
        if warehouse_items:
            logger.info(f"📦 {site_config.site_name} 仓库有 {len(warehouse_items)} 种物品")
            
            for item in warehouse_items:
                name = item.get("name", "")
                crop = site_config.get_crop_by_name(name)
                if not crop:
                    continue

                key, crop_data = crop
                cost = crop_data.get("cost", 0)
                market_price = market_prices.get(key, 0)
                quantity = item.get("quantity", 1)
                sell_key = item.get("sell_key", "")

                if market_price > cost and sell_key:
                    sold_count = 0
                    profit = 0
                    for i in range(quantity):
                        sell_result = self.__do_sell(cookie, site_config, sell_key)
                        if sell_result["success"]:
                            sold_count += 1
                            profit += (market_price - cost)
                            logger.info(f"✅ {site_config.site_name} 出售 {name} ({i+1}/{quantity})")
                        else:
                            logger.warn(f"❌ {site_config.site_name} 出售 {name} 失败：{sell_result.get('message')}")
                            break
                        time.sleep(0.3)
                    
                    if sold_count > 0:
                        total_profit += profit
                        trades.append({
                            "name": name,
                            "quantity": sold_count,
                            "profit": profit,
                            "source": "仓库"
                        })
                        action_summary.append(f"售出{name}x{sold_count}")
                        logger.info(f"✅ {site_config.site_name} 仓库 {name} 售出 {sold_count} 个，利润 {profit}")
        else:
            logger.info(f"ℹ️ {site_config.site_name} 仓库为空")

        # ========== 更新统计 ==========
        if total_profit > 0:
            self._stats["total_profit"] = self._stats.get("total_profit", 0) + total_profit
            self._stats["total_trades"] = self._stats.get("total_trades", 0) + len(trades)
        
        self._stats["last_run"] = time.time()
        
        history = self._stats.get("history", [])
        history.append({
            "time": time.time(),
            "site": site_config.site_name,
            "mode": "自动收获",
            "action": f"出售 {len(trades)} 种物品" if trades else "无交易",
            "profit": total_profit,
            "status": "completed"
        })
        if len(history) > 20:
            history = history[-20:]
        self._stats["history"] = history
        self._stats["last_result"] = f"自动收获完成，利润 {total_profit} {site_config.currency}"

        if not action_summary:
            action_summary.append("无操作")

        logger.info(f"✅ {site_config.site_name} 自动收获完成，总利润 {total_profit} {site_config.currency}")
        
        return {
            "status": "completed",
            "message": f"自动收获完成，总利润 {total_profit} {site_config.currency}",
            "data": {
                "trades": trades,
                "total_profit": total_profit,
                "market_prices": market_prices,
                "action_summary": action_summary,
                "currency": site_config.currency,
                "site_name": site_config.site_name,
                "mode": "自动收获"
            }
        }

    # ==================== 任务完成 ====================
    
    def __finish_task(self, result: Dict[str, Any]):
        if self._notify:
            self.__send_notification(result)
        self._update_stats()

    def _update_stats(self):
        try:
            self.update_config({
                "enabled": self._enabled,
                "site_ids": self._site_ids,
                "mode": self._mode,
                "interval_minutes": self._interval_minutes,
                "harvest_interval_minutes": self._harvest_interval_minutes,
                "expire_threshold_minutes": self._expire_threshold_minutes,
                "notify": self._notify,
                "run_once": False,
                "stats": self._stats,
                "market_prices": self._market_prices
            })
        except Exception as e:
            logger.error(f"更新统计数据失败：{e}")

    def __send_notification(self, result: Dict[str, Any]):
        site_names = self.__get_site_names()
        currency = self.__get_site_currency()
        
        title = f"🌾【农场自动化】"
        
        # 构建通知内容
        lines = []
        lines.append(f"🌐 目标站点：{site_names}")
        
        data = result.get("data", {})
        total_profit = data.get("total_profit", 0)
        
        if total_profit > 0:
            lines.append(f"💰 总利润：{total_profit} {currency}")
        else:
            lines.append("ℹ️ 无可交易物品")
        
        # 详细结果
        details = data.get("details", [])
        if details:
            lines.append("")
            for d in details:
                site_name = d.get("site", "未知")
                detail_result = d.get("result", {})
                
                status = detail_result.get("status")
                if status == "auth_failed":
                    lines.append(f"  • {site_name}：🔐 认证失败")
                    continue
                elif status in ["failed", "error"]:
                    lines.append(f"  • {site_name}：❌ {detail_result.get('message', '失败')}")
                    continue
                
                detail_data = detail_result.get("data", {})
                profit = detail_data.get("total_profit", 0)
                trades = detail_data.get("trades", [])
                site_currency = detail_data.get("currency", "魔力")
                
                # 合并相同物品的售出数量（包括仓库和收获）
                sell_map = {}  # {物品名称: 总数量}
                for trade in trades:
                    name = trade.get("name", "")
                    quantity = trade.get("quantity", 1)
                    if name:
                        sell_map[name] = sell_map.get(name, 0) + quantity
                
                # 构建售出信息
                if sell_map:
                    sell_info = [f"售出{name}x{count}" for name, count in sell_map.items()]
                    msg_parts = []
                    if profit > 0:
                        msg_parts.append(f"利润{profit}{site_currency}")
                    msg_parts.append(f"本次{', '.join(sell_info)}")
                    lines.append(f"  • {site_name}：{', '.join(msg_parts)}")
                else:
                    if profit > 0:
                        lines.append(f"  • {site_name}：利润{profit}{site_currency}")
                    else:
                        lines.append(f"  • {site_name}：无可交易物品")

        # 累计统计
        lines.append("")
        lines.append(f"📊 累计利润：{self._stats.get('total_profit', 0):,} {currency}")
        lines.append(f"🔄 累计交易：{self._stats.get('total_trades', 0)} 次")

        text = "\n".join(lines)

        self.post_message(
            mtype=NotificationType.Plugin,
            title=title,
            text=text
        )