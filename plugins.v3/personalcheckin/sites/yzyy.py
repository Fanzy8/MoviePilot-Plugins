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

BASE_URL = "https://yzyy.org"
SIGN_PAGE = f"{BASE_URL}/plugin.php?id=zqlj_sign"
MY_RECORD_PAGE = f"{BASE_URL}/plugin.php?id=zqlj_sign&tb=my"
INDEX_PAGE = f"{BASE_URL}/plugin.php?id=zqlj_sign&tb=index"


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


def extract_credit_rows(page: str) -> List[Dict[str, str]]:
    """从打卡记录表格里提取每行的（时间, 上次奖励, 总奖励）。

    站点改版后不再有「最近奖励：X 影币」这段文案，影币只出现在打卡记录表：
    用户名 | 打卡等级 | 总天数 | 月天数 | 上次打卡时间 | 上次奖励 | 总奖励 | 今日状态。
    「我的记录」（tb=my）页是同一套表格，但只有自己的行。
    """
    text = html_module.unescape(page or "")
    rows: List[Dict[str, str]] = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", text, re.I | re.S):
        cells = [_plain(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.I | re.S)]
        credits = [c for c in cells if "影币" in c]
        stamps = [c for c in cells if re.search(r"\d{4}-\d{2}-\d{2}", c)]
        if not credits or not stamps:
            continue
        rows.append({
            "ts": stamps[-1],
            "reward": credits[0] if len(credits) > 1 else "",
            "total": credits[-1],
        })
    return rows


def credit_value(text: str) -> str:
    """从「1影币 / 319影币」里取数字。"""
    match = re.search(r"([\d,]+(?:\.\d+)?)\s*影币", text or "")
    return match.group(1).replace(",", "") if match else ""


class YzyySite(SiteAdapter):
    """yzyy 论坛签到（直连优先）。"""

    id = "yzyy"
    name = "yzyy 论坛"
    domain = "yzyy.org"
    order = 40
    direct_default = True
    help_text = "登录 yzyy 后复制整段 Cookie；本站直连通常比走代理快，失败会自动改走代理"

    def run(self, ctx: Any) -> SiteResult:
        """签到页 → 打卡链接 → 结果页；影币从打卡记录表里取。"""
        started = time.time()
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
            my_reward, my_balance = self._read_my_credits(ctx)
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
        # 打卡成功后结果页就是打卡记录列表：第一行（时间最新）就是自己刚写进去的记录
        my_reward, my_balance = self._credits_from_result(resp.text, started)
        if not (my_reward or my_balance):
            my_reward, my_balance = self._read_my_credits(ctx)
        if my_reward or my_balance:
            result.reward = my_reward or result.reward
            result.balance = my_balance or result.balance
        return result

    @staticmethod
    def _format_credits(row: Dict[str, str]) -> Tuple[str, str]:
        """把一行打卡记录格式化成（+本次影币, 累计影币）。"""
        reward = credit_value(row.get("reward") or "")
        total = credit_value(row.get("total") or "")
        return (f"+{reward} 影币" if reward else "",
                f"{total} 影币" if total else "")

    def _read_my_credits(self, ctx: Any) -> Tuple[str, str]:
        """读「我的记录」（tb=my）：只有自己的行，取最近一次打卡的影币。"""
        page = ctx.request(
            "GET", MY_RECORD_PAGE,
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                     "Referer": SIGN_PAGE},
        )
        if page.cf_blocked or page.error or page.status != 200:
            return "", ""
        rows = extract_credit_rows(page.text)
        if not rows:
            return "", ""
        return self._format_credits(rows[0])

    @staticmethod
    def _credits_from_result(page: str, started: float) -> Tuple[str, str]:
        """从打卡记录列表里找本次运行写入的那一行（按时间倒序，取前 3 分钟内的第一条）。"""
        rows = extract_credit_rows(page)
        if not rows:
            return "", ""
        cutoff = time.strftime("%Y-%m-%d %H:%M", time.localtime(started - 180))
        for row in rows:
            if str(row.get("ts") or "")[:16] >= cutoff:
                return YzyySite._format_credits(row)
        return "", ""
