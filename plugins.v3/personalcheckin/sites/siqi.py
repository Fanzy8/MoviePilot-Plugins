"""思齐（si-qi.xyz）签到：attendance.php，验证码由站点 JS 自动填入。

实测（2026-11-10）：签到页 HTML 里 ``imagestring`` 的 value 为空，
但浏览器执行站点 JS 后输入框会被填上 6 位验证码。因此本站走
MoviePilot 自带隐身浏览器：等 JS 填好值，再点「验证并签到」。
"""
from __future__ import annotations

import re
from typing import Any, Optional

from .base import ALREADY, BLOCKED, ERROR, FAIL, NOAUTH, SUCCESS, SiteAdapter, SiteResult

BASE = "https://si-qi.xyz"
HOME = f"{BASE}/index.php"
SIGN_PAGE = f"{BASE}/attendance.php"


def _already(page: str) -> bool:
    """页面是否表明今天已签到。"""
    text = page or ""
    return bool(re.search(r"签到已得|今日已签到|今天已经签到|您今天已经签到|签到成功", text))


def _reward(text: str) -> str:
    """从结果页提取奖励文案。"""
    match = re.search(r"本次签到获得\s*([\d.]+)\s*个魔力值", text or "")
    return f"+{match.group(1)} 魔力值" if match else ""


def _extra(text: str) -> str:
    """连续/累计签到天数。"""
    match = re.search(r"已连续签到\s*(\d+)\s*天", text or "")
    return f"连续 {match.group(1)} 天" if match else ""


class SiqiSite(SiteAdapter):
    """思齐每日签到（站点 JS 自动填验证码）。"""

    id = "siqi"
    name = "思齐"
    domain = "si-qi.xyz"
    order = 58
    direct_default = False
    help_text = "登录 si-qi.xyz 后复制整段 Cookie；验证码由站点自动填入，不需要 OCR"

    def run(self, ctx: Any) -> SiteResult:
        home = ctx.request("GET", HOME, headers={"Referer": BASE + "/"})
        if home.cf_blocked:
            return SiteResult(BLOCKED, "Cloudflare 拦截（思齐）")
        if home.error:
            return SiteResult(ERROR, f"网络异常：{home.error}")
        if home.status != 200:
            return SiteResult(ERROR, f"首页 HTTP {home.status}")
        if "login.php" in home.url or re.search(r"name=[\"']username[\"']", home.text):
            return SiteResult(NOAUTH, "Cookie 已失效（思齐跳登录页）")
        if _already(home.text):
            return SiteResult(ALREADY, "今日已签到")

        try:
            return self._run_browser(ctx)
        except Exception as exc:  # 浏览器启动/执行失败
            return SiteResult(ERROR, f"浏览器签到失败：{type(exc).__name__}: {exc}")

    def _run_browser(self, ctx: Any) -> SiteResult:
        """用隐身浏览器打开签到页，等 JS 填值后提交。"""
        with ctx.browser(SIGN_PAGE, timeout=90) as page:
            ctx.wait_cf(page, timeout=30)
            code: Optional[str] = ""
            for _ in range(20):
                try:
                    code = page.evaluate(
                        "() => { const i = document.querySelector('#captcha-input')"
                        " || document.querySelector('[name=imagestring]'); return i ? i.value : ''; }"
                    ) or ""
                except Exception:
                    code = ""
                if code:
                    break
                page.wait_for_timeout(1000)
            if not code:
                return SiteResult(FAIL, "未读到站点自动填入的验证码（页面可能已签到或结构变化）")

            clicked = page.evaluate(
                "() => { const b = [...document.querySelectorAll('button,input[type=submit]')]"
                ".find(x => /验证并签到|立即签到|签到/.test((x.innerText||x.value||'')));"
                " if (b) { b.click(); return true; } return false; }"
            )
            if not clicked:
                return SiteResult(FAIL, "未找到「验证并签到」按钮")

            # 等提交后的页面（跳转或重载）
            page.wait_for_timeout(4000)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            text = ""
            try:
                text = page.evaluate(
                    "() => document.body ? document.body.innerText : ''"
                ) or ""
            except Exception:
                text = ""

        if re.search(r"签到成功|本次签到获得|已连续签到", text):
            return SiteResult(SUCCESS, "签到成功", reward=_reward(text), extra=_extra(text))
        if re.search(r"今天已经签到|今日已签到|已经签到过", text):
            return SiteResult(ALREADY, "今日已签到")
        if re.search(r"验证码|錯誤|错误|失敗|失败", text):
            return SiteResult(FAIL, "站点提示验证码或签到失败")
        return SiteResult(FAIL, "提交后未识别到签到结果，请到页面查看")
