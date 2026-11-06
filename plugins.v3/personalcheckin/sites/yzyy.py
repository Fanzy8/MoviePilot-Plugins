"""yzyy 论坛（Discuz zqlj_sign 插件）签到协议。

移植自旧插件 ``YzyySignin`` 的可用逻辑，修掉三处导致长期失败的问题：
1. 超时 30 秒 → 60 秒（站点响应常在 15–40 秒）；
2. 没有重试 → 由插件层重试两次；
3. 异常只记 ``str(e)`` → 记录异常类型，便于定位。

协议：
- 签到页 ``GET /plugin.php?id=zqlj_sign``，按钮文案「今日已打卡 / 点击打卡」；
- 打卡链接 ``/plugin.php?id=zqlj_sign&sign=<8位hex>``；
- 结果页含「最近奖励: X 影币 / 连续打卡: N 天 / 累计打卡: N 天」。
"""
from __future__ import annotations

import html as html_module
import re
from typing import Any, Optional, Tuple

from .base import ALREADY, BLOCKED, ERROR, FAIL, NOAUTH, SUCCESS, SiteAdapter, SiteResult

BASE_URL = "https://yzyy.org"
SIGN_PAGE = f"{BASE_URL}/plugin.php?id=zqlj_sign"


def is_logged_out(page: str) -> bool:
    """页面是否提示未登录。"""
    text = html_module.unescape(page or "")
    return any(key in text for key in ("请登录", "需要先登录", "请先登录", "您还没有登录"))


def button_state(page: str) -> str:
    """返回 ``already`` / ``need`` / ``unknown``。"""
    text = html_module.unescape(page or "")
    match = re.search(r'<div[^>]*class="[^"]*signbtn[^"]*"[^>]*>(.*?)</div>', text, re.I | re.S)
    area = match.group(1) if match else text
    if "今日已打卡" in area:
        return "already"
    if "点击打卡" in area or "打卡" in area or "签到" in area:
        return "need"
    return "unknown"


def extract_sign_url(page: str) -> Optional[str]:
    """从签到页提取打卡链接。"""
    text = html_module.unescape(page or "")
    patterns = (
        r'<div[^>]*class="[^"]*signbtn[^"]*"[^>]*>.*?<a[^>]*href="([^"]*plugin\.php\?id=zqlj_sign&sign=[^"]+)"',
        r"<div[^>]*class='[^']*signbtn[^']*'[^>]*>.*?<a[^>]*href='([^']*plugin\.php\?id=zqlj_sign&sign=[^']+)'",
        r'<a[^>]*href="([^"]*plugin\.php\?id=zqlj_sign&sign=[^"]+)"',
        r"<a[^>]*href='([^']*plugin\.php\?id=zqlj_sign&sign=[^']+)'",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I | re.S)
        if match:
            url = match.group(1).strip()
            if url.startswith("http"):
                return url
            return f"{BASE_URL}/{url.lstrip('/')}"
    match = re.search(r"sign=([a-f0-9]{8})", text)
    if match:
        return f"{BASE_URL}/plugin.php?id=zqlj_sign&sign={match.group(1)}"
    return None


def extract_reward(page: str) -> Tuple[str, str, str]:
    """提取（本次奖励, 连续天数, 累计天数）。"""
    text = html_module.unescape(page or "")
    reward = ""
    match = re.search(r"最近奖励[：:]\s*([\d.]+)\s*影币", text)
    if match:
        reward = f"+{match.group(1)} 影币"
    streak = ""
    match = re.search(r"连续打卡[：:]\s*([\d.]+)\s*天", text)
    if match:
        streak = f"连续 {match.group(1)} 天"
    total = ""
    match = re.search(r"累计打卡[：:]\s*([\d.]+)\s*天", text)
    if match:
        total = f"累计 {match.group(1)} 天"
    return reward, streak, total


def parse_result(page: str) -> SiteResult:
    """解析打卡结果页。"""
    text = html_module.unescape(page or "")
    reward, streak, total = extract_reward(text)
    extra = " / ".join(part for part in (streak, total) if part)
    if "今日已打卡" in text or "今日已签到" in text:
        return SiteResult(ALREADY, "今日已打卡", reward=reward, extra=extra)
    if "最近打卡" in text and "累计打卡" in text:
        return SiteResult(SUCCESS, "签到成功", reward=reward, extra=extra)
    for key in ("签到成功", "签到完成", "打卡成功", "打卡获得", "影币"):
        if key in text:
            return SiteResult(SUCCESS, "签到成功", reward=reward, extra=extra)
    for key in ("错误", "失败", "权限不足", "禁止", "无效"):
        if key in text:
            return SiteResult(FAIL, f"签到{key}")
    return SiteResult(FAIL, "无法识别签到结果")


class YzyySite(SiteAdapter):
    """yzyy 论坛签到（直连优先）。"""

    id = "yzyy"
    name = "yzyy 论坛"
    domain = "yzyy.org"
    order = 40
    direct_default = True
    help_text = "登录 yzyy 后复制整段 Cookie；本站直连通常比走代理快，失败会自动改走代理"

    def run(self, ctx: Any) -> SiteResult:
        """签到页 → 打卡链接 → 结果页。"""
        page = ctx.request(
            "GET", SIGN_PAGE,
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                     "Referer": BASE_URL + "/"},
        )
        if page.cf_blocked:
            return SiteResult(BLOCKED, "Cloudflare 拦截（yzyy）")
        if page.error:
            return SiteResult(ERROR, f"网络异常：{page.error}")
        if page.status != 200:
            return SiteResult(ERROR, f"签到页 HTTP {page.status}")
        if is_logged_out(page.text):
            return SiteResult(NOAUTH, "Cookie 已失效（签到页提示登录）")

        state = button_state(page.text)
        reward, streak, total = extract_reward(page.text)
        extra = " / ".join(part for part in (streak, total) if part)
        if state == "already":
            return SiteResult(ALREADY, "今日已打卡", reward=reward, extra=extra)

        sign_url = extract_sign_url(page.text)
        if not sign_url:
            return SiteResult(FAIL, "未找到打卡链接（按钮不可用或页面结构变了）")

        resp = ctx.request(
            "GET", sign_url,
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                     "Referer": SIGN_PAGE},
        )
        if resp.cf_blocked:
            return SiteResult(BLOCKED, "Cloudflare 拦截（打卡请求）")
        if resp.error:
            return SiteResult(ERROR, f"打卡请求异常：{resp.error}")
        if resp.status != 200:
            return SiteResult(ERROR, f"打卡请求 HTTP {resp.status}")
        if is_logged_out(resp.text):
            return SiteResult(NOAUTH, "Cookie 已失效（打卡请求提示登录）")
        return parse_result(resp.text)
