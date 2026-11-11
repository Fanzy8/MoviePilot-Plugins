"""PlayLet（playlet.cc）签到：attendance.php + 图形验证码。

requests 的 TLS 指纹会被站点/CDN 重置（实测 SSL EOF），整条链路走
MoviePilot 自带隐身浏览器：页面内 fetch 取验证码图片、OCR、页面内 fetch 提交。
"""
from __future__ import annotations

import re
import time
from typing import Any

from .base import ALREADY, BLOCKED, ERROR, FAIL, NOAUTH, SUCCESS, SiteAdapter, SiteResult

BASE = "https://playlet.cc"
HOME = f"{BASE}/index.php"
SIGN_PAGE = f"{BASE}/attendance.php"


def _already(text: str) -> bool:
    return bool(re.search(r"签到已得\s*\d|今日已签到|今天已经签到|您今天已经签到|已簽到", text or ""))


class PlayletSite(SiteAdapter):
    """PlayLet 每日签到（浏览器 + OCR）。"""

    id = "playlet"
    name = "PlayLet"
    domain = "playlet.cc"
    order = 55
    direct_default = False
    help_text = "登录 playlet.cc 后复制整段 Cookie；签到走隐身浏览器 + OCR 识别验证码"

    def run(self, ctx: Any) -> SiteResult:
        try:
            return self._run_browser(ctx)
        except Exception as exc:
            return SiteResult(ERROR, f"浏览器签到失败：{type(exc).__name__}: {exc}")

    def _run_browser(self, ctx: Any) -> SiteResult:
        with ctx.browser(HOME, timeout=90) as page:
            ctx.wait_cf(page, timeout=30)
            if "login.php" in (page.url or ""):
                return SiteResult(NOAUTH, "Cookie 已失效（PlayLet 跳登录页）")
            if self._already_now(page):
                return SiteResult(ALREADY, "今日已签到", balance=self._read_balance(page))

            page.goto(SIGN_PAGE, wait_until="domcontentloaded", timeout=60000)
            ctx.wait_cf(page, timeout=20)
            image_hash = page.evaluate(
                "() => (document.querySelector('[name=imagehash]') || {}).value || ''"
            ) or ""
            image_src = page.evaluate(
                "() => (document.querySelector('img[src*=regimage]') || {}).src || ''"
            ) or ""
            if not image_hash or not image_src:
                if self._already_now(page):
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
                        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                        body: body.toString(),
                    });
                    return await r.text();
                }""",
                {"action": SIGN_PAGE, "hash": image_hash, "code": code},
            ) or ""

            text = self._body(page)
            body = result + "\n" + text
            if re.search(r"签到成功|簽到成功|本次签到获得|您今天已经签到|今天已经签到", body):
                reward = re.search(r"本次签到获得\s*([\d.]+)\s*个魔力值", body)
                return SiteResult(SUCCESS, "签到成功",
                                  reward=f"+{reward.group(1)} 魔力值" if reward else "",
                                  balance=self._read_balance(page))

            # 回读首页确认
            page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
            if self._already_now(page):
                return SiteResult(SUCCESS, "签到成功（回读已签到）",
                                  balance=self._read_balance(page))
            if re.search(r"验证码|錯誤|错误|失敗|失败", body):
                return SiteResult(FAIL, "站点提示验证码或签到失败")
            return SiteResult(FAIL, "无法确认签到结果，请到页面查看")

    def _read_balance(self, page: Any) -> str:
        """从签到页页头读魔力值余额（「魔力值 [ 使用 ]: 102,745.1」，方括号内有空格）。

        首页模板只有 ``title="魔力值"`` + 数字，签到页才有完整的「使用」链接。
        """
        try:
            page.goto(SIGN_PAGE, wait_until="domcontentloaded", timeout=60000)
            html = page.content() or ""
        except Exception:
            return ""
        plain = re.sub(r"<[^>]+>", " ", html)
        plain = re.sub(r"\s+", " ", plain)
        match = re.search(r"魔力值\s*\[\s*使用\s*\]\s*[：:]\s*([\d,.]+)", plain)
        return f"{match.group(1)} 魔力值" if match else ""

    def _already_now(self, page: Any) -> bool:
        """已签到判定要看 HTML：页头的「签到已得」在 innerText 里被样式隐藏。"""
        if _already(self._body(page)):
            return True
        try:
            return _already(page.content() or "")
        except Exception:
            return False

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
