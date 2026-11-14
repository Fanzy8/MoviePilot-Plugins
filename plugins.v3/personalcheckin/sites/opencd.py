"""OpenCD（open.cd）签到：plugin_sign-in.php 表单 + 图形验证码。

2026-11-14 实测结论（替代原浏览器方案）：
- 登录态由 ``c_secure_uid`` / ``c_secure_pass`` 决定；首页与签到页都能用
  requests 直接访问（只有静态 JS 会 TLS EOF，不必请求）。
- 首次响应会 ``Set-Cookie: c_csrf``，它和表单里的 ``csrf_token`` 配对。
  必须用**同一个 requests.Session** 保持，否则提交返回
  ``403 CSRF token verification failed``。
- 提交参数是 ``csrf_token`` + ``imagehash`` + ``imagestring`` 三个，原来只发
  后两个，站点必然拒绝。
- 原浏览器方案不可用：``page.set_extra_http_headers({"cookie": ...})`` 注入的
  Cookie 只在首次请求有效，站点回写 ``c_csrf`` 后浏览器 cookie jar 接管，
  第二次导航只带 ``c_csrf``，签到页被 loggedinorreturn() 判未登录跳到
  ``login.php?returnto=plugin_sign-in.php``，于是抓到的是登录页验证码。
"""
from __future__ import annotations

import base64
import re
import time
from typing import Any, Dict, Tuple

import requests

from .base import ALREADY, BLOCKED, ERROR, FAIL, NOAUTH, SUCCESS, SiteAdapter, SiteResult

BASE = "https://open.cd"
HOME = f"{BASE}/index.php"
SIGN_PAGE = f"{BASE}/plugin_sign-in.php"
SIGN_ACTION = f"{BASE}/plugin_sign-in.php?cmd=signin"
CAPTCHA = f"{BASE}/image.php?action=regimage&imagehash=%s"

_ALREADY_MARKS = ("查看签到记录", "签到已得", "今日已签到", "今天已经签到", "您今天已经签到")
_BALANCE_RE = re.compile(r"魔力值[^0-9]{0,40}?([\d,]{3,}(?:\.\d+)?)")
_HASH_RE = re.compile(r'name="imagehash"\s+value="([^"]+)"')
_CSRF_RE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')
_MISSING_RE = re.compile(r'需要登录|未登[錄录]|必須在登[錄录]後才能訪問|必须在登录后才能访问')


def _already(text: str) -> bool:
    return any(mark in (text or "") for mark in _ALREADY_MARKS)


def _cookie_pairs(cookie: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for chunk in (cookie or "").split(";"):
        if "=" in chunk:
            key, value = chunk.split("=", 1)
            key = key.strip()
            if key:
                out[key] = value.strip()
    return out


class OpencdSite(SiteAdapter):
    """OpenCD 每日签到（HTTP + Session + OCR）。"""

    id = "opencd"
    name = "OpenCD"
    domain = "open.cd"
    order = 50
    direct_default = False
    help_text = "登录 open.cd 后复制整段 Cookie；签到走 HTTP 表单 + OCR 识别验证码"

    def run(self, ctx: Any) -> SiteResult:
        try:
            return self._run_http(ctx)
        except requests.RequestException as exc:
            return SiteResult(ERROR, f"网络异常：{type(exc).__name__}: {exc}")
        except Exception as exc:  # 任何未预期异常都落成可读结果
            return SiteResult(ERROR, f"签到异常：{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ 主流程
    def _run_http(self, ctx: Any) -> SiteResult:
        session = self._session(ctx)
        timeout = float(getattr(ctx, "timeout", 60) or 60)

        home = session.get(HOME, timeout=timeout)
        if self._cf(home):
            return SiteResult(BLOCKED, "Cloudflare 拦截（挑战页）")
        if home.status_code >= 400:
            return SiteResult(ERROR, f"首页 HTTP {home.status_code}")
        if "login.php" in str(home.url):
            return SiteResult(NOAUTH, "Cookie 已失效（OpenCD 跳登录页）")
        balance = self._balance(home.text)
        if _already(home.text):
            return SiteResult(ALREADY, "今日已签到", balance=balance)

        last_msg = ""
        for attempt in range(1, 4):
            page = session.get(SIGN_PAGE, timeout=timeout)
            if self._cf(page):
                return SiteResult(BLOCKED, "Cloudflare 拦截（签到页）")
            if "login.php" in str(page.url) or _MISSING_RE.search(page.text or ""):
                return SiteResult(NOAUTH, "Cookie 已失效（签到页要求登录）")
            if _already(page.text):
                return SiteResult(ALREADY, "今日已签到", balance=balance)

            csrf = _CSRF_RE.search(page.text or "")
            image_hash = _HASH_RE.search(page.text or "")
            if not (csrf and image_hash):
                return SiteResult(FAIL, "未找到签到表单（页面结构可能已变）")

            image = session.get(CAPTCHA % image_hash.group(1), timeout=timeout)
            if image.status_code != 200 or not image.content:
                last_msg = f"验证码图片 HTTP {image.status_code}"
                time.sleep(2)
                continue
            code = self._ocr(image.content)
            if not code:
                last_msg = "验证码识别失败（OCR 未返回有效值）"
                time.sleep(1)
                continue

            resp = session.post(
                SIGN_ACTION,
                timeout=timeout,
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "Referer": SIGN_PAGE,
                },
                data={
                    "csrf_token": csrf.group(1),
                    "imagehash": image_hash.group(1),
                    "imagestring": code,
                },
            )
            body = resp.text or ""
            result = self._parse(body)
            if result:
                try:
                    again = session.get(HOME, timeout=timeout)
                    result.balance = self._balance(again.text) or balance
                except requests.RequestException:
                    result.balance = balance
                return result

            msg = re.search(r'"msg"\s*:\s*"([^"]+)"', body)
            last_msg = (msg.group(1) if msg else body.strip()[:120]) or f"HTTP {resp.status_code}"
            if "CSRF" in body or "验证码" in body or "imagestring" in body:
                time.sleep(2)
                continue
            break

        # 提交后回读首页兜底确认
        try:
            again = session.get(HOME, timeout=timeout)
            if _already(again.text):
                return SiteResult(SUCCESS, "签到成功（回读已签到）", balance=self._balance(again.text))
            balance = self._balance(again.text) or balance
        except requests.RequestException:
            pass
        return SiteResult(FAIL, f"签到失败：{last_msg}" if last_msg else "签到失败")

    # ------------------------------------------------------------------ 辅助
    @staticmethod
    def _session(ctx: Any) -> requests.Session:
        session = requests.Session()
        session.headers.update({
            "User-Agent": getattr(ctx, "ua", "") or "Mozilla/5.0",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        proxy = (getattr(ctx, "proxy", "") or "").strip()
        if proxy:
            session.proxies = {"http": proxy, "https": proxy}
        for key, value in _cookie_pairs(getattr(ctx, "cookie", "") or "").items():
            session.cookies.set(key, value, domain="open.cd", path="/")
        return session

    @staticmethod
    def _cf(resp: requests.Response) -> bool:
        return resp.status_code in (403, 429, 503) and any(
            mark in (resp.text or "")[:4000]
            for mark in ("Just a moment", "cf_chl_opt", "__cf_chl", "请稍候", "安全验证")
        )

    @staticmethod
    def _balance(text: str) -> str:
        match = _BALANCE_RE.search(text or "")
        return f"{match.group(1)} 魔力值" if match else ""

    @staticmethod
    def _parse(body: str) -> SiteResult | None:
        flat = (body or "").replace(" ", "")
        if '"state":"success"' not in flat:
            return None
        integral = re.search(r'"integral"\s*:\s*"?([\d]+)', body)
        days = re.search(r'"signindays"\s*:\s*"?([\d]+)', body)
        return SiteResult(
            SUCCESS,
            "签到成功",
            reward=f"+{integral.group(1)} 魔力值" if integral else "",
            extra=f"连续 {days.group(1)} 天" if days else "",
        )

    @staticmethod
    def _ocr(content: bytes) -> str:
        """把验证码图交给 MoviePilot 内置 OCR。OCR 失败就换图重来，别拿同图死磕。"""
        try:
            from app.adapters.external.ocr import OcrHelper  # type: ignore
        except Exception:
            return ""
        b64 = base64.b64encode(content).decode()
        for _ in range(2):
            try:
                text = (OcrHelper().get_captcha_text(image_b64=b64) or "").strip()
            except Exception:
                text = ""
            text = re.sub(r"\s+", "", text)
            if len(text) >= 4:
                return text
            time.sleep(1)
        return ""
