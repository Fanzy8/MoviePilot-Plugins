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
import time
from typing import Any, Dict, List, Optional, Tuple

from .base import ALREADY, BLOCKED, ERROR, FAIL, NOAUTH, SUCCESS, SiteAdapter, SiteResult

BASE_URL = "https://www.yzyy.org"
SIGN_PAGE = f"{BASE_URL}/plugin.php?id=zqlj_sign"
MY_RECORD_PAGE = f"{BASE_URL}/plugin.php?id=zqlj_sign&tb=my"
PROFILE_PAGE = f"{BASE_URL}/home.php?mod=space&do=profile"


def is_logged_out(page: str) -> bool:
    """页面是否提示未登录。"""
    text = html_module.unescape(page or "")
    return any(key in text for key in ("请登录", "需要先登录", "请先登录", "您还没有登录"))


def button_state(page: str) -> str:
    """返回 ``already`` / ``need`` / ``unknown``。

    只认签到按钮所在的 ``signbtn`` 区块：打卡记录列表里每个人都是
    「今日已打卡」，用整页文本判断会把任何人都误判成已签到。
    """
    text = html_module.unescape(page or "")
    match = re.search(r'<div[^>]*class="[^"]*signbtn[^"]*"[^>]*>(.*?)</div>', text, re.I | re.S)
    if not match:
        return "unknown"
    area = match.group(1)
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
    """解析打卡结果页。

    结果页就是签到页本身（站点打卡后重定向回 index），所以只能看
    ``signbtn`` 按钮：变成「今日已打卡」才算成功。不能用整页关键词，
    因为打卡记录列表里全是「今日已打卡 / X影币」。
    """
    text = html_module.unescape(page or "")
    reward, streak, total = extract_reward(text)
    extra = " / ".join(part for part in (streak, total) if part)
    state = button_state(text)
    if state == "already":
        return SiteResult(SUCCESS, "签到成功", reward=reward, extra=extra)
    if state == "need":
        return SiteResult(FAIL, "打卡后按钮仍是「点击打卡」，请检查 Cookie 是否失效")
    for key in ("签到成功", "签到完成", "打卡成功", "打卡获得"):
        if key in text:
            return SiteResult(SUCCESS, "签到成功", reward=reward, extra=extra)
    for key in ("错误", "失败", "权限不足", "禁止", "无效"):
        if key in text:
            return SiteResult(FAIL, f"签到{key}")
    return SiteResult(FAIL, "无法识别签到结果")


def _plain(cell: str) -> str:
    """去掉表格单元格里的标签与空白。"""
    return html_module.unescape(re.sub(r"<[^>]+>", "", cell or "")).strip()


def parse_my_record(page: str) -> Tuple[str, str]:
    """解析「我的记录」（tb=my）第一行，返回（本次实际到账影币, 打卡时间）。

    表头是：打卡时间 | 打卡固定奖励 | 打卡前N名奖励 | 连续打卡奖励 |
    用户组额外奖励 | 节日额外奖励。每格是「1影币」或「无」，实际到账要把
    一行里的奖励列全部加起来（例如 1 + 节日 10 = 11 影币）。
    """
    text = html_module.unescape(page or "")
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", text, re.I | re.S):
        cells = [_plain(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.I | re.S)]
        if len(cells) < 2 or not re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", cells[0]):
            continue
        total = 0.0
        found = False
        for cell in cells[1:]:
            if "影币" not in cell:
                continue
            found = True
            match = re.search(r"([\d.]+)\s*影币", cell)
            if match:
                total += float(match.group(1))
        if not found:
            continue
        amount = int(total) if total == int(total) else total
        return f"+{amount} 影币", cells[0]
    return "", ""


def extract_balance(page: str) -> str:
    """从个人资料页取影币余额。

    统计信息区块的真实结构是 ``<li><em>影币</em>684</li>``；不能把整页
    转成文本再找「影币」，页面脚本里的 ``creditnotice = '1|影币|,2|贡献|...'``
    会先命中并取到 2。
    """
    text = html_module.unescape(page or "")
    patterns = (
        r"<em>\s*影币\s*</em>\s*([\d,]+)",
        r"影币\s*</span>\s*</div>\s*<div[^>]*>\s*([\d,]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1).replace(",", "")
    return ""


class YzyySite(SiteAdapter):
    """yzyy 论坛签到（直连优先）。"""

    id = "yzyy"
    name = "yzyy 论坛"
    domain = "yzyy.org"
    order = 40
    direct_default = True
    help_text = "登录 yzyy 后复制整段 Cookie；本站直连通常比走代理快，失败会自动改走代理"

    def run(self, ctx: Any) -> SiteResult:
        """签到页 → 打卡链接 → 结果页；影币从「我的记录」与个人资料页取。"""
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
            my_reward, my_balance = self._read_my_credits(ctx, today_only=True)
            return SiteResult(ALREADY, "今日已打卡", reward=my_reward or reward,
                              balance=my_balance, extra=extra)

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

        result = parse_result(resp.text)
        my_reward, my_balance = self._read_my_credits(ctx, today_only=True)
        if my_reward or my_balance:
            result.reward = my_reward or result.reward
            result.balance = my_balance or result.balance
        return result

    def _read_my_credits(self, ctx: Any, today_only: bool = False) -> Tuple[str, str]:
        """读「我的记录」拿本次影币，读个人资料页拿影币余额。

        ``today_only`` 为真时，记录不是今天的就不算本次奖励
        （已打卡分支可能拿到几天前的记录，不能冒充今天）。
        """
        headers = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                   "Referer": SIGN_PAGE}
        reward = ""
        page = ctx.request("GET", MY_RECORD_PAGE, headers=headers)
        if not (page.cf_blocked or page.error or page.status != 200):
            reward, stamp = parse_my_record(page.text)
            if reward and today_only and not stamp.startswith(time.strftime("%Y-%m-%d")):
                reward = ""
        balance = ""
        profile = ctx.request("GET", PROFILE_PAGE, headers=headers)
        if not (profile.cf_blocked or profile.error or profile.status != 200):
            value = extract_balance(profile.text)
            balance = f"{value} 影币" if value else ""
        return reward, balance
