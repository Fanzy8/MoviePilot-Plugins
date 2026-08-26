"""
海胆自动签到插件 - 自动获取Cookie
"""
import re
import json
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, parse_qs

import pytz
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.db.site_oper import SiteOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType
from app.utils.http import RequestUtils


class HaidanSignin(_PluginBase):
    """海胆自动签到插件"""

    # 插件基本信息
    plugin_name = "海胆自动签到"
    plugin_desc = "自动签到海胆PT站点"
    plugin_icon = "statistic.png"
    plugin_version = "1.0.0"
    plugin_author = "bfjy"
    author_url = "https://bfjy2024.github.io/bfjy"
    plugin_config_prefix = "haidansignin_"
    plugin_order = 21
    auth_level = 2

    # 常量配置
    BASE_URL = "https://www.haidan.cc"
    SIGNIN_URL = f"{BASE_URL}/signin.php"
    INDEX_URL = f"{BASE_URL}/index.php"
    SITE_DOMAIN = "haidan.cc"
    MAX_HISTORY = 100
    REQUEST_TIMEOUT = 30

    # 私有属性
    _enabled: bool = False
    _onlyonce: bool = False
    _cron: str = ""
    _notify: bool = False
    _scheduler: Optional[BackgroundScheduler] = None
    _lock: threading.Lock = threading.Lock()

    def init_plugin(self, config: dict = None):
        """初始化插件"""
        self.stop_service()

        if config:
            self._enabled = config.get("enabled", False)
            self._onlyonce = config.get("onlyonce", False)
            self._cron = config.get("cron", "")
            self._notify = config.get("notify", False)

        if self._onlyonce:
            self._onlyonce = False
            self.update_config({
                "enabled": self._enabled,
                "onlyonce": False,
                "cron": self._cron,
                "notify": self._notify,
            })
            logger.info("收到立即运行请求，后台启动签到任务")
            threading.Thread(target=self.__signin, daemon=True).start()

        logger.info(f"海胆自动签到插件初始化完成，启用状态: {self._enabled}")

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [{
            "cmd": "/haidan_sign",
            "event": EventType.PluginAction,
            "desc": "执行海胆签到",
            "category": "站点",
            "data": {"action": "haidan_signin_run"}
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/sign",
            "endpoint": self.__sign_api,
            "methods": ["POST"],
            "auth": "bear",
            "summary": "执行海胆签到",
        }, {
            "path": "/history",
            "endpoint": self.__history_api,
            "methods": ["GET"],
            "auth": "bear",
            "summary": "获取签到历史",
        }]

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            try:
                return [{
                    "id": "HaidanSignin",
                    "name": "海胆自动签到",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.__signin,
                    "kwargs": {}
                }]
            except Exception as e:
                logger.error(f"海胆自动签到服务配置错误: {e}")
        return []

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"停止海胆自动签到服务失败: {e}")

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{
                                    'component': 'VSwitch',
                                    'props': {'model': 'enabled', 'label': '启用插件', 'color': 'success'}
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{
                                    'component': 'VSwitch',
                                    'props': {'model': 'notify', 'label': '发送通知', 'color': 'info'}
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{
                                    'component': 'VSwitch',
                                    'props': {'model': 'onlyonce', 'label': '立即运行一次', 'color': 'warning'}
                                }]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [{
                                    'component': 'VCronField',
                                    'props': {
                                        'model': 'cron',
                                        'label': '执行周期',
                                        'placeholder': '0 8 * * *'
                                    }
                                }]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [{
                            'component': 'VCol',
                            'props': {'cols': 12},
                            'content': [{
                                'component': 'VAlert',
                                'props': {
                                    'type': 'info',
                                    'variant': 'tonal',
                                    'text': '📌 使用说明：\n1. 插件会自动从站点管理中读取海胆的Cookie\n2. 请先在站点管理中配置海胆站点Cookie\n3. 签到成功后会自动记录签到信息'
                                }
                            }]
                        }]
                    }
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "cron": "0 8 * * *",
            "notify": True,
        }

    def get_page(self) -> List[dict]:
        """详情页面"""
        history = self.get_data('history') or []
        if not history:
            return [{
                'component': 'div',
                'props': {
                    'class': 'text-center pa-8',
                    'style': 'font-size: 1.1rem; color: #94a3b8;'
                },
                'text': '📭 暂无签到记录'
            }]

        if not isinstance(history, list):
            history = [history]

        history = sorted(history, key=lambda x: x.get("time") or "0", reverse=True)

        rows = []
        for record in history[:50]:
            is_success = record.get('success', False)

            rows.append({
                'component': 'tr',
                'props': {'style': 'border-bottom: 1px solid #f0f0f0;'},
                'content': [
                    {'component': 'td', 'props': {'class': 'text-caption py-2 px-3'}, 'text': record.get('time', '-')},
                    {'component': 'td', 'props': {'class': f'text-caption py-2 px-3 font-weight-medium text-{"success" if is_success else "error"}'}, 'text': f'{"✅" if is_success else "❌"} {"成功" if is_success else "失败"}'},
                    {'component': 'td', 'props': {'class': 'text-caption py-2 px-3'}, 'text': record.get('message', '-')},
                ]
            })

        return [{
            'component': 'VCard',
            'props': {'variant': 'tonal'},
            'content': [
                {
                    'component': 'VCardTitle',
                    'text': '📊 签到历史'
                },
                {
                    'component': 'VCardText',
                    'content': [{
                        'component': 'VSimpleTable',
                        'props': {'dense': True},
                        'content': [
                            {
                                'component': 'thead',
                                'content': [
                                    {
                                        'component': 'tr',
                                        'props': {'style': 'border-bottom: 2px solid #e0e0e0;'},
                                        'content': [
                                            {'component': 'th', 'props': {'class': 'text-left text-caption font-weight-medium py-1 px-3'}, 'text': '执行时间'},
                                            {'component': 'th', 'props': {'class': 'text-left text-caption font-weight-medium py-1 px-3'}, 'text': '状态'},
                                            {'component': 'th', 'props': {'class': 'text-left text-caption font-weight-medium py-1 px-3'}, 'text': '消息'},
                                        ]
                                    }
                                ]
                            },
                            {
                                'component': 'tbody',
                                'content': rows
                            }
                        ]
                    }]
                }
            ]
        }]

    def __sign_api(self) -> Dict[str, Any]:
        if self._lock.locked():
            return {"success": False, "message": "已有签到任务正在运行"}
        threading.Thread(target=self.__signin, daemon=True).start()
        return {"success": True, "message": "签到任务已启动"}

    def __history_api(self) -> Dict[str, Any]:
        history = self.get_data('history') or []
        return {"success": True, "data": history[:50]}

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

    def __send_notification(self, title: str, text: str):
        if self._notify:
            try:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title=f"【海胆自动签到】{title}",
                    text=text
                )
                logger.info(f"📨 通知已发送: {title}")
            except Exception as e:
                logger.error(f"发送通知失败: {e}")

    def __save_history(self, success: bool, message: str = ""):
        history = self.get_data('history') or []
        if not isinstance(history, list):
            history = []
        history.insert(0, {
            "time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "success": success,
            "message": message
        })
        if len(history) > 100:
            history = history[:100]
        self.save_data('history', history)

    def __get_site_cookie(self) -> str:
        """从站点管理获取Cookie"""
        try:
            site = SiteOper().get_by_domain(self.SITE_DOMAIN)
            if site and site.cookie:
                logger.info(f"✅ 从站点管理获取到海胆Cookie")
                return site.cookie.strip()
            else:
                logger.warning(f"⚠️ 未找到海胆站点配置或Cookie为空")
                return ""
        except Exception as err:
            logger.error(f"读取海胆站点Cookie失败: {err}")
            return ""

    @eventmanager.register(EventType.PluginAction)
    def handle_plugin_action(self, event: Event):
        if not event:
            return
        event_data = event.event_data
        if not event_data or event_data.get("action") != "haidan_signin_run":
            return
        if self._lock.locked():
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title="【海胆自动签到】",
                text="已有签到任务正在运行，请等待完成"
            )
            return
        threading.Thread(target=self.__signin, daemon=True).start()

    def __signin(self):
        """执行签到"""
        if not self._lock.acquire(blocking=False):
            logger.warning("签到任务已在运行，跳过本次执行")
            return

        try:
            logger.info("🔄 开始执行海胆签到任务")

            # 从站点管理获取Cookie
            cookie = self.__get_site_cookie()
            if not cookie:
                logger.error("Cookie未获取到，任务终止")
                self.__save_history(False, "未找到海胆站点Cookie，请先在站点管理中配置")
                self.__send_notification("签到失败", "未找到海胆站点Cookie，请先在站点管理中配置")
                return

            # 执行签到请求
            result = self.__do_signin(cookie)

            if result.get('success'):
                reward = result.get('reward', '')
                logger.info(f"✅ 签到成功: {reward}")
                self.__save_history(True, reward or '签到成功')
                self.__send_notification("签到成功", reward or '签到成功')
            else:
                logger.error(f"❌ 签到失败: {result.get('message', '')}")
                self.__save_history(False, result.get('message', '签到失败'))
                self.__send_notification("签到失败", result.get('message', '签到失败'))

        except Exception as e:
            logger.error(f"签到任务异常: {e}")
            self.__save_history(False, f"异常: {str(e)[:50]}")
            self.__send_notification("签到异常", str(e)[:200])
        finally:
            self._lock.release()

    def __do_signin(self, cookie: str) -> Dict[str, Any]:
        """执行签到请求"""
        try:
            headers = {
                'accept': '*/*',
                'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6',
                'referer': self.INDEX_URL,
                'sec-ch-ua': '"Chromium";v="148", "Microsoft Edge";v="148", "Not/A)Brand";v="99"',
                'sec-ch-ua-mobile': '?0',
                'sec-ch-ua-platform': '"macOS"',
                'sec-fetch-dest': 'empty',
                'sec-fetch-mode': 'cors',
                'sec-fetch-site': 'same-origin',
                'user-agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0',
                'x-requested-with': 'XMLHttpRequest'
            }

            logger.info("📤 发送签到请求...")
            res = RequestUtils(
                cookies=cookie,
                ua=settings.USER_AGENT,
                proxies=settings.PROXY,
                timeout=self.REQUEST_TIMEOUT
            ).post_res(
                url=self.SIGNIN_URL,
                headers=headers,
                data={},
                allow_redirects=True
            )

            if not res:
                return {'success': False, 'message': '签到请求无响应'}

            logger.debug(f"签到响应状态码: {res.status_code}")

            # 海胆使用AJAX请求，响应可能是JSON或HTML
            content_type = res.headers.get('content-type', '')
            response_text = res.text

            # 尝试解析JSON响应
            if 'application/json' in content_type:
                try:
                    json_data = json.loads(response_text)
                    logger.debug(f"JSON响应: {json_data}")
                    
                    # 检查JSON响应中的签到结果
                    if json_data.get('code') == 0 or json_data.get('status') == 'success':
                        return {'success': True, 'reward': json_data.get('message', '签到成功')}
                    else:
                        return {'success': False, 'message': json_data.get('msg', json_data.get('message', '签到失败'))}
                except json.JSONDecodeError:
                    pass

            # 如果是HTML响应，解析HTML内容
            if res.status_code == 200:
                # 检查是否已签到
                if self.__check_already_signed(response_text):
                    reward = self.__extract_reward(response_text)
                    return {'success': True, 'reward': reward or '今日已签到'}

                # 检查是否签到失败
                if self.__check_signin_failed(response_text):
                    error_msg = self.__extract_error(response_text)
                    return {'success': False, 'message': error_msg or '签到失败'}

                # 检查是否需要登录
                if self.__check_need_login(response_text):
                    return {'success': False, 'message': 'Cookie已失效，请重新登录站点管理更新Cookie'}

            # 如果状态码不是200，可能是重定向到登录页
            if res.status_code in [301, 302, 303, 307, 308]:
                return {'success': False, 'message': 'Cookie已失效，请重新登录站点管理更新Cookie'}

            # 无法确定结果，尝试从响应中提取信息
            if '成功' in response_text or '获得' in response_text:
                reward = self.__extract_reward(response_text)
                return {'success': True, 'reward': reward or '签到成功'}

            return {'success': False, 'message': f'签到请求返回异常，状态码: {res.status_code}'}

        except Exception as e:
            logger.error(f"执行签到异常: {e}")
            return {'success': False, 'message': f'签到异常: {str(e)[:50]}'}

    def __check_already_signed(self, html: str) -> bool:
        """检查是否已签到或签到成功"""
        keywords = [
            '恭喜您,获得了',
            '魔力值奖励',
            '明日再打卡',
            '明天再来可获取',
            '每日打卡',
            '已打卡',
        ]
        return any(kw in html for kw in keywords)

    def __check_need_login(self, html: str) -> bool:
        """检查是否需要登录"""
        keywords = [
            '请登录', '请先登录', '需要登录', '未登录',
            '您还没有登录', '登录后签到'
        ]
        return any(kw in html for kw in keywords)

    def __check_signin_failed(self, html: str) -> bool:
        """检查签到是否失败"""
        keywords = [
            '签到失败',
            '今日已签到,请勿重复签到',
            '已签到,请勿重复',
            '重复签到',
        ]
        return any(kw in html for kw in keywords)

    def __extract_reward(self, html: str) -> str:
        """提取签到奖励信息"""
        try:
            import html as html_module
            text = html_module.unescape(html)
            # 移除HTML标签，保留文本
            text = re.sub(r'<[^>]+>', ' ', text)
            text = re.sub(r'\s+', ' ', text)
            text = text.strip()

            logger.debug(f"提取奖励的文本: {text[:300]}")

            rewards = []

            # 1. 匹配 "恭喜您,获得了55魔力值奖励!"
            match = re.search(r'恭喜您[，,]\s*获得了\s*([\d,]+)\s*魔力值奖励', text)
            if match:
                value = match.group(1).replace(',', '')
                rewards.append(f"获得 {value} 魔力值")
            else:
                # 备选匹配 "获得XXX魔力值"
                match2 = re.search(r'获得\s*([\d,]+)\s*魔力值', text)
                if match2:
                    value = match2.group(1).replace(',', '')
                    rewards.append(f"获得 {value} 魔力值")

            # 2. 匹配 "明天再来可获取60魔力值奖励"
            match = re.search(r'明天再来可获取\s*([\d,]+)\s*魔力值奖励', text)
            if match:
                value = match.group(1).replace(',', '')
                rewards.append(f"明天可获 {value} 魔力值")

            # 3. 匹配连续签到信息
            match = re.search(r'已连续签到\s*(\d+)\s*天', text)
            if match:
                days = match.group(1)
                rewards.append(f"连续签到 {days} 天")

            # 4. 匹配总签到信息
            match = re.search(r'累计签到\s*(\d+)\s*天', text)
            if match:
                days = match.group(1)
                rewards.append(f"累计签到 {days} 天")

            return ' | '.join(rewards) if rewards else ''

        except Exception as e:
            logger.error(f"提取奖励异常: {e}")
            return ''

    def __extract_error(self, html: str) -> str:
        """提取错误信息"""
        try:
            import html as html_module
            text = html_module.unescape(html)
            text = re.sub(r'<[^>]+>', ' ', text)
            text = re.sub(r'\s+', ' ', text)
            text = text.strip()

            # 查找错误信息
            patterns = [
                r'签到失败[：:]\s*([^。.!？]*)[。.!？]',
                r'错误[：:]\s*([^。.!？]*)[。.!？]',
                r'提示[：:]\s*([^。.!？]*)[。.!？]',
            ]

            for pattern in patterns:
                match = re.search(pattern, text)
                if match:
                    return match.group(1).strip()

            # 如果包含"重复签到"
            if '重复签到' in text or '今日已签到' in text:
                return '今日已签到，请勿重复签到'

            return ''
        except Exception as e:
            logger.error(f"提取错误异常: {e}")
            return ''


# 导出插件类
__all__ = ['HaidanSignin']