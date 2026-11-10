"""OpenCD（open.cd）签到：plugin_sign-in.php + 图形验证码。

requests 的 TLS 指纹会被站点/CDN 重置（实测 SSL EOF），所以整条链路走
MoviePilot 自带隐身浏览器：页面内 fetch 取验证码图片、OCR、页面内 fetch 提交。
"""
from __future__ import annotations

import re
import time
from typing import Any, Optional, Tuple

from .base import ALREADY, BLOCKED, ERROR, FAIL, NOAUTH, SUCCESS, SiteAdapter, SiteResult

BASE = "https://open.cd"
HOME = f"{BASE}/index.php"
SIGN_PAGE = f"{BASE}/plugin_sign-in.php"
SIGN_ACTION = f"{BASE}/plugin_sign-in.php?cmd=signin"


def _already(text: str) -> bool:
    # OpenCD 已签到后首页的 [签到] 链接会变成 [查看签到记录]，
    # 页面里没有「签到已得」这类文案，必须把「查看签到记录」也当作已签到。
    return bool(re.search(r"签到已得|今日已签到|今天已经签到|您今天已经签到|查看签到记录", text or ""))


class OpencdSite(SiteAdapter):
    """OpenCD 每日签到（浏览器 + OCR）。"""

    id = "opencd"
    name = "OpenCD"
    domain = "open.cd"
    order = 50
    direct_default = False
    help_text = "登录 open.cd 后复制整段 Cookie；签到走隐身浏览器 + OCR 识别验证码"

    def run(self, ctx: Any) -> SiteResult:
        try:
            return self._run_browser(ctx)
        except Exception as exc:
            return SiteResult(ERROR, f"浏览器签到失败：{type(exc).__name__}: {exc}")

    def _run_browser(self, ctx: Any) -> SiteResult:
        with ctx.browser(HOME, timeout=90) as page:
            ctx.wait_cf(page, timeout=30)
            text = self._body(page)
            if "login.php" in (page.url or ""):
                return SiteResult(NOAUTH, "Cookie 已失效（OpenCD 跳登录页）")
            if _already(text):
                return SiteResult(ALREADY, "今日已签到")

            page.goto(SIGN_PAGE, wait_until="domcontentloaded", timeout=60000)
            ctx.wait_cf(page, timeout=20)
            image_hash = page.evaluate(
                "() => (document.querySelector('[name=imagehash]') || {}).value || ''"
            ) or ""
            image_src = page.evaluate(
                "() => (document.querySelector('img[src*=regimage]') || {}).src || ''"
            ) or ""
            if not image_hash or not image_src:
                # 没有表单：可能已签到
                if _already(self._body(page)):
                    return SiteResult(ALREADY, "今日已签到")
                return SiteResult(FAIL, "未找到签到表单（页面结构可能已变）")

            code = self._ocr(page, image_src)
            if not code:
                return SiteResult(FAIL, "验证码识别失败（OCR 未返回有效值）")

            result = page.evaluate(
                """async ({action, hash, code}) => {
                    const body = new URLSearchParams({imagehash: hash, imagestring: code});
                    const r = await fetch(action, {
                        method: 'POST',
                        credentials: 'include',
                        headers: {
                            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                            'X-Requested-With': 'XMLHttpRequest',
                        },
                        body: body.toString(),
                    });
                    return await r.text();
                }""",
                {"action": SIGN_ACTION, "hash": image_hash, "code": code},
            ) or ""

            if '"state":"success"' in result.replace(" ", ""):
                days = re.search(r'"signindays":"?(\d+)', result)
                integral = re.search(r'"integral":"?(\d+)', result)
                reward = f"+{integral.group(1)} 魔力值" if integral else ""
                extra = f"连续 {days.group(1)} 天" if days else ""
                return SiteResult(SUCCESS, "签到成功", reward=reward, extra=extra)

            msg = re.search(r'"msg":"([^"]+)"', result)
            if msg:
                return SiteResult(FAIL, f"站点提示：{msg.group(1)}")

            # 回读首页确认
            page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
            if _already(self._body(page)):
                return SiteResult(SUCCESS, "签到成功（回读已签到）")
            return SiteResult(FAIL, "无法确认签到结果，请到页面查看")

    @staticmethod
    def _body(page: Any) -> str:
        try:
            return page.evaluate("() => document.body ? document.body.innerText : ''") or ""
        except Exception:
            return ""

    @staticmethod
    def _ocr(page: Any, image_src: str) -> str:
        """在页面内取图片 base64，再交给 MoviePilot OCR。"""
        try:
            from app.adapters.external.ocr import OcrHelper  # type: ignore
        except Exception:
            return ""
        b64 = ""
        try:
            b64 = page.evaluate(
                """async (url) => {
                    const r = await fetch(url, {credentials: 'include'});
                    const buf = await r.arrayBuffer();
                    const bytes = new Uint8Array(buf);
                    let binary = '';
                    for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
                    return btoa(binary);
                }""",
                image_src,
            ) or ""
        except Exception:
            b64 = ""
        if not b64:
            return ""
        for _ in range(3):
            try:
                text = OcrHelper().get_captcha_text(image_b64=b64) or ""
            except Exception:
                text = ""
            text = text.strip()
            if len(text) >= 4:
                return text
            time.sleep(1)
        return ""
