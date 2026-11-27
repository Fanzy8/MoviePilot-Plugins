"""OurBits（ourbits.club）签到。

站点签到页 `attendance.php` 带 Cloudflare Turnstile：表单里 `cf-turnstile-response`
由浏览器现场生成，`TurnstileCallback()` 一拿到 token 就自动提交。所以纯 HTTP 过不了，
必须用共享 Chromium（家宽出口 + 真实指纹 + 已登录）打开签到页，剩下的交给页面自己。
"""
from __future__ import annotations

import re
import time
from typing import Any

from .base import ALREADY, BLOCKED, ERROR, NOAUTH, SUCCESS, SiteAdapter, SiteResult
from .cdp import CdpError, open_page, page_text_expr

SIGN_URL = "https://ourbits.club/attendance.php"
WAIT_SECONDS = 75


def _num(text: str) -> str:
    m = re.search(r"魔力值\s*\[使用\]\s*[:：]\s*([0-9,.]+)", text or "")
    return m.group(1) if m else ""


class OurbitsSite(SiteAdapter):
    id = "ourbits"
    name = "OurBits"
    domain = "ourbits.club"
    order = 60
    help_text = ("用共享浏览器（已登录 OurBits）自动打开签到页，Turnstile 由浏览器现场完成并自动提交；"
                 "无需填 Cookie。浏览器没登录时先手动登录一次。")

    def cookie_ok(self, cookie: str) -> bool:
        # 登录态在共享浏览器里，不看插件里的 Cookie
        return True

    def run(self, ctx: Any) -> SiteResult:
        browser = None
        try:
            browser, _ = open_page(SIGN_URL, timeout=float(ctx.timeout or 60))
        except CdpError as exc:
            return SiteResult(ERROR, str(exc))

        try:
            # 等页面把 Turnstile 拉起来并自动提交（成功后会跳到结果页）
            deadline = time.monotonic() + WAIT_SECONDS
            text = ""
            last_err = ""
            while time.monotonic() < deadline:
                time.sleep(2)
                try:
                    text = str(browser.eval(page_text_expr()) or "")
                except CdpError as exc:
                    last_err = str(exc)
                    continue
                if not text:
                    continue
                if "签到成功" in text or "已经签到" in text:
                    break
                if "请先登录" in text or "登录后才能" in text:
                    return SiteResult(NOAUTH, "共享浏览器里 OurBits 未登录，请先登录一次")
                if "兩步驗證" in text or "两步验证" in text:
                    return SiteResult(NOAUTH, "站点要求两步验证，请在浏览器里完成后再试")
            balance = _num(text)

            if "签到成功" in text:
                m = re.search(r"这是您的第\s*(\d+)\s*次签到", text)
                days = m.group(1) if m else ""
                gain = re.search(r"本次签到获得\s*(\d+)\s*个魔力值", text)
                streak = re.search(r"已连续签到\s*(\d+)\s*天", text)
                reward = f"+{gain.group(1)} 魔力值" if gain else ""
                extra = f"连续 {streak.group(1)} 天" if streak else ""
                msg = f"第 {days} 次签到成功" if days else "签到成功"
                return SiteResult(SUCCESS, msg, reward=reward, balance=balance, extra=extra)

            if "已经签到" in text:
                return SiteResult(ALREADY, "今天已经签到过了", balance=balance)

            if "cf-turnstile" in text or "等待签到验证程序" in text or "验证程序" in text:
                return SiteResult(BLOCKED, "Turnstile 没通过（页面停在验证环节），稍后自动重试或手动签一次",
                                  balance=balance)
            return SiteResult(ERROR, f"未识别的签到结果：{text[:120]}（{last_err[:80]}）" if last_err else f"未识别的签到结果：{text[:120]}")
        except CdpError as exc:
            return SiteResult(ERROR, f"浏览器执行失败：{exc}")
        finally:
            if browser is not None:
                browser.close()
