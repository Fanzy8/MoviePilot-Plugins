"""思齐（si-qi.xyz）签到：attendance.php，验证码由站点 JS 自动填入。

实测（2026-11-10）：签到页 HTML 里 ``imagestring`` 的 value 为空，
但浏览器执行站点 JS 后输入框会被填上 6 位验证码。因此本站走
MoviePilot 自带隐身浏览器：等 JS 填好值，再点「验证并签到」。
"""
from __future__ import annotations

import re
import time
from typing import Any, Optional

from .base import ALREADY, BLOCKED, ERROR, FAIL, NOAUTH, SUCCESS, SiteAdapter, SiteResult

BASE = "https://si-qi.xyz"
HOME = f"{BASE}/index.php"
SIGN_PAGE = f"{BASE}/attendance.php"


def _already(page: str) -> bool:
    """页面是否表明今天已签到。"""
    text = page or ""
    return bool(re.search(r"签到已得\s*\d|今日已签到|今天已经签到|您今天已经签到|签到成功", text))


def _reward(text: str) -> str:
    """从结果页提取奖励文案。"""
    match = re.search(r"本次签到获得\s*([\d.]+)\s*个魔力值", text or "")
    return f"+{match.group(1)} 魔力值" if match else ""


def _extra(text: str) -> str:
    """连续/累计签到天数。"""
    match = re.search(r"已连续签到\s*(\d+)\s*天", text or "")
    return f"连续 {match.group(1)} 天" if match else ""


def _plain(text: str) -> str:
    """把 HTML 去标签压空白：页头的「魔力值 [使用]」中间夹着 <a> 标签。"""
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text)


def _balance(text: str) -> str:
    """页头显示的魔力值余额（如「魔力值 [ 使用 ]: 2,614,296.5」）。

    注意站点渲染的是 ``[ 使用 ]``（方括号内有空格），不能写成 ``[使用]``。
    """
    match = re.search(r"魔力值\s*\[\s*使用\s*\]\s*[：:]\s*([\d,.]+)", _plain(text))
    return f"{match.group(1)} 魔力值" if match else ""


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
            return SiteResult(ALREADY, "今日已签到", balance=_balance(home.text))

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
                # 已签到的签到页没有验证码表单，先把这种正常情况认出来，别报结构变化
                try:
                    body = page.evaluate(
                        "() => document.body ? document.body.innerText : ''"
                    ) or ""
                except Exception:
                    body = ""
                if _already(body):
                    return SiteResult(ALREADY, "今日已签到", balance=_balance(body))
                return SiteResult(FAIL, "未读到站点自动填入的验证码（页面可能已签到或结构变化）")

            clicked = page.evaluate(
                "() => { const b = [...document.querySelectorAll('button,input[type=submit]')]"
                ".find(x => /验证并签到|立即签到|签到/.test((x.innerText||x.value||'')));"
                " if (b) { b.click(); return true; } return false; }"
            )
            if not clicked:
                return SiteResult(FAIL, "未找到「验证并签到」按钮")

            # 提交后页面会重载，重载快慢不定；只读一次常常读到旧页或空页，
            # 所以按秒轮询到出现结果关键字为止（最多约 20 秒）。
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            text = ""
            for _ in range(20):
                try:
                    page.wait_for_timeout(1000)
                except Exception:
                    time.sleep(1)
                try:
                    current = page.evaluate(
                        "() => document.body ? document.body.innerText : ''"
                    ) or ""
                except Exception:
                    current = ""
                if current:
                    text = current
                if re.search(
                        r"签到成功|本次签到获得|已连续签到|今天已经签到|今日已签到|已经签到过"
                        r"|验证码|錯誤|错误|失敗|失败", text):
                    break

        if re.search(r"签到成功|本次签到获得|已连续签到", text):
            return SiteResult(SUCCESS, "签到成功", reward=_reward(text),
                              balance=_balance(text), extra=_extra(text))
        if re.search(r"今天已经签到|今日已签到|已经签到过", text):
            return SiteResult(ALREADY, "今日已签到", balance=_balance(text))
        if re.search(r"验证码|錯誤|错误|失敗|失败", text):
            return SiteResult(FAIL, "站点提示验证码或签到失败")

        # 浏览器没读到结果不代表没签上：HTTP 回读签到页再确认一次
        back = ctx.request("GET", SIGN_PAGE, headers={"Referer": BASE + "/"})
        back_text = _plain(back.text)
        if back.status == 200 and re.search(
                r"签到成功|本次签到获得|已连续签到|今天已经签到|今日已签到|已经签到过",
                back_text):
            return SiteResult(SUCCESS, "签到成功（回读确认）",
                              reward=_reward(back_text), balance=_balance(back_text),
                              extra=_extra(back_text))
        return SiteResult(FAIL, "提交后未识别到签到结果，请到页面查看")
