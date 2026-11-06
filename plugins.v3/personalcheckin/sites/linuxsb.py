"""linux.sb（烧饼社区）签到协议。

- 签到页：``GET https://linux.sb/daily_checkin``，登录态下渲染「今天已签到 / 连续天数 / 累计签到 / 积分」；
- 签到：``POST https://linux.sb/daily_checkin``，表单 ``_csrf=<bbs_csrf cookie 值>``，
  带 ``X-Requested-With: XMLHttpRequest``，成功返回 JSON ``{"ok":1,"message":"..."}``；
- 未登录访问签到页会被 302 到 ``/login``。

站点偶尔也会套 Cloudflare，被挑战时同样降级到隐藏浏览器。
"""
from __future__ import annotations

import html as html_module
import json
import re
from typing import Any, Tuple

from .base import ALREADY, BLOCKED, ERROR, FAIL, NOAUTH, SUCCESS, SiteAdapter, SiteResult

BASE = "https://linux.sb"
CHECKIN_PATH = "/daily_checkin"


def _text(page: str) -> str:
    """去掉脚本/标签，压缩空白，便于中文正则。"""
    body = re.sub(r"<script.*?</script>", " ", page or "", flags=re.S | re.I)
    body = re.sub(r"<style.*?</style>", " ", body, flags=re.S | re.I)
    body = re.sub(r"<[^>]+>", " ", body)
    return re.sub(r"\s+", " ", html_module.unescape(body))


def parse_page_state(page: str) -> Tuple[bool, str, str, str]:
    """解析签到页：返回（今天已签到?, 今日奖励, 连续天数, 余额）。"""
    text = _text(page)
    signed = "今天已签到" in text or "今日已签到" in text
    reward = ""
    match = re.search(r"今天已签到\s*\+?\s*(\d+)", text)
    if match:
        reward = f"+{match.group(1)} 积分"
    streak = ""
    match = re.search(r"连续天数\s*(\d+)", text)
    if match:
        streak = f"连续 {match.group(1)} 天"
    balance = ""
    match = re.search(r"积分\s*(\d+)", text)
    if match:
        balance = f"{match.group(1)} 积分"
    return signed, reward, streak, balance


def parse_checkin_json(data: Any) -> SiteResult:
    """解析签到 POST 的 JSON 返回。"""
    if not isinstance(data, dict):
        return SiteResult(ERROR, "签到接口返回非 JSON")
    message = str(data.get("message") or "").strip()
    ok = data.get("ok")
    if ok in (1, True, "1") or data.get("success") is True:
        reward = ""
        match = re.search(r"(\d+)\s*积[分点]", message)
        if match:
            reward = f"+{match.group(1)} 积分"
        return SiteResult(SUCCESS, message or "签到成功", reward=reward)
    if "已签到" in message or "已经签到" in message:
        return SiteResult(ALREADY, message or "今日已签到")
    return SiteResult(FAIL, message or "签到失败")


class LinuxSbSite(SiteAdapter):
    """linux.sb 每日签到。"""

    id = "linuxsb"
    name = "linux.sb"
    domain = "linux.sb"
    order = 30
    help_text = "登录 linux.sb 后复制整段 Cookie（必须含 bbs_auth 与 bbs_csrf）"

    def cookie_ok(self, cookie: str) -> bool:
        """linux.sb 需要 bbs_auth 才可能处于登录态。"""
        return "bbs_auth" in (cookie or "")

    def run(self, ctx: Any) -> SiteResult:
        """读签到页 → 未签到则提交 CSRF 表单。"""
        page_resp = ctx.request(
            "GET", f"{BASE}{CHECKIN_PATH}",
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                     "Referer": f"{BASE}/"},
            allow_redirects=False,
        )
        if page_resp.cf_blocked:
            return self._browser_attempt(ctx)
        if page_resp.error:
            return SiteResult(ERROR, f"网络异常：{page_resp.error}")
        if page_resp.status in (301, 302, 303, 307, 308):
            location = str(page_resp.headers.get("location") or "")
            if "/login" in location:
                return SiteResult(NOAUTH, "Cookie 已失效（签到页跳转到登录页）")
            return SiteResult(ERROR, f"签到页跳转异常：{location or page_resp.status}")
        if page_resp.status != 200:
            return SiteResult(ERROR, f"签到页 HTTP {page_resp.status}")

        signed, reward, streak, balance = parse_page_state(page_resp.text)
        if signed:
            return SiteResult(ALREADY, "今天已签到", reward=reward, balance=balance, extra=streak)
        if 'name="password"' in (page_resp.text or "")[:4000]:
            return SiteResult(NOAUTH, "Cookie 已失效（返回登录表单）")

        csrf = ""
        for chunk in (ctx.cookie or "").split(";"):
            key, _, value = chunk.partition("=")
            if key.strip() == "bbs_csrf":
                csrf = value.strip()
                break
        if not csrf:
            return SiteResult(ERROR, "Cookie 里缺少 bbs_csrf，无法提交签到")

        resp = ctx.request(
            "POST", f"{BASE}{CHECKIN_PATH}",
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": BASE,
                "Referer": f"{BASE}{CHECKIN_PATH}",
            },
            data={"_csrf": csrf},
            allow_redirects=False,
        )
        if resp.cf_blocked:
            return self._browser_attempt(ctx)
        if resp.error:
            return SiteResult(ERROR, f"签到请求异常：{resp.error}")
        if resp.status in (301, 302, 303, 307, 308):
            return SiteResult(NOAUTH, "Cookie 已失效（签到请求被重定向到登录）")
        data = resp.json()
        if data is None:
            if "已签到" in (resp.text or ""):
                return SiteResult(ALREADY, "今天已签到")
            return SiteResult(ERROR, f"签到接口返回非 JSON（HTTP {resp.status}）")
        result = parse_checkin_json(data)
        if result.ok:
            result.extra = streak
            result.balance = balance or self._refresh_balance(ctx)
        return result

    def _refresh_balance(self, ctx: Any) -> str:
        """签到后重读一次页面拿最新积分。"""
        resp = ctx.request("GET", f"{BASE}{CHECKIN_PATH}",
                           headers={"Accept": "text/html"}, allow_redirects=True)
        if resp.error or resp.status != 200:
            return ""
        _signed, _reward, _streak, balance = parse_page_state(resp.text)
        return balance

    def _browser_attempt(self, ctx: Any) -> SiteResult:
        """Cloudflare 拦截时的浏览器兜底。"""
        try:
            with ctx.browser(f"{BASE}{CHECKIN_PATH}") as page:
                passed = ctx.wait_cf(page)
                payload = page.evaluate(
                    """async () => {
                        const out = {};
                        const csrf = (document.cookie.match(/(?:^|;\\s*)bbs_csrf=([^;]+)/) || [])[1] || '';
                        try {
                            const r = await fetch('/daily_checkin', {method: 'POST', credentials: 'include',
                                headers: {'Content-Type': 'application/x-www-form-urlencoded',
                                          'X-Requested-With': 'XMLHttpRequest'},
                                body: '_csrf=' + encodeURIComponent(csrf)});
                            out.status = r.status;
                            out.body = await r.text();
                        } catch (e) { out.error = String(e); }
                        out.text = document.body ? document.body.innerText.slice(0, 400) : '';
                        return out;
                    }"""
                )
        except Exception as exc:
            return SiteResult(BLOCKED, f"浏览器兜底失败：{type(exc).__name__}: {exc}")
        if not isinstance(payload, dict):
            return SiteResult(BLOCKED, "浏览器未返回数据")
        if payload.get("error"):
            return SiteResult(ERROR, f"浏览器内请求失败：{payload['error']}")
        body = payload.get("body") or ""
        try:
            data = json.loads(body)
        except (TypeError, ValueError):
            text = _text(payload.get("text") or "")
            if "今天已签到" in text or "今日已签到" in text:
                return SiteResult(ALREADY, "今天已签到（浏览器兜底）")
            return SiteResult(BLOCKED, f"Cloudflare 未放行（验证{'通过' if passed else '未通过'}）")
        result = parse_checkin_json(data)
        result.message = f"{result.message}（浏览器兜底）"
        return result
