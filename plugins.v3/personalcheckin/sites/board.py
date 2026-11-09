"""NodeSeek 系（NodeSeek / DeepFlood）签到协议。

两个站同一套后端，只有域名不同：
- 签到：``POST {base}/api/attendance?random=true``，返回 JSON ``{success, message}``；
- 余额：``GET {base}/api/account/credit/page-1``，返回
  ``{"success": true, "data": [[amount, balance, description, timestamp], ...]}``。

Cloudflare 会按出口 IP 与浏览器指纹判定，HTTP 被挑战时降级到宿主自带隐身浏览器：
先在 ``/board`` 页等待验证通过，再在页面上下文里 ``fetch`` 同一个接口（带着
浏览器自己的 Cookie 与 TLS 指纹），这样能拿到与手工点击一致的响应。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

from .base import ALREADY, BLOCKED, ERROR, FAIL, NOAUTH, SUCCESS, SiteAdapter, SiteResult

ATTENDANCE_PATH = "/api/attendance?random=true"
CREDIT_PATH = "/api/account/credit/page-1"

ATTENDANCE_JS = """
async () => {
  const out = {};
  try {
    const r = await fetch('%s', {method: 'POST', credentials: 'include',
      headers: {'Content-Type': 'application/json'}});
    out.status = r.status;
    out.body = await r.text();
    out.cf = (r.headers.get('cf-mitigated') || '');
  } catch (e) { out.error = String(e); }
  try {
    const c = await fetch('%s', {credentials: 'include'});
    out.credit = await c.text();
  } catch (e) { out.creditError = String(e); }
  return out;
}
""" % (ATTENDANCE_PATH, CREDIT_PATH)


def parse_attendance(data: Any) -> SiteResult:
    """把签到接口的 JSON 解析成结果。"""
    if not isinstance(data, dict):
        return SiteResult(ERROR, "签到接口返回非 JSON")
    message = str(data.get("message") or "").strip()
    status = data.get("status")
    if "已完成" in message or "已经签到" in message or "已签到" in message:
        return SiteResult(ALREADY, message or "今日已签到", reward=_reward(message))
    if data.get("success") is True or "鸡腿" in message:
        return SiteResult(SUCCESS, message or "签到成功", reward=_reward(message))
    if status == 404 or "USER NOT FOUND" in message.upper():
        return SiteResult(NOAUTH, "Cookie 已失效（站点返回 USER NOT FOUND），请重新登录后更新 Cookie")
    if "请先登录" in message or "未登录" in message or "登录" in message:
        return SiteResult(NOAUTH, message or "Cookie 已失效")
    return SiteResult(FAIL, message or "签到失败")


def parse_credit(text: str) -> str:
    """从余额接口解析出当前鸡腿数。"""
    try:
        data = json.loads(text or "")
    except (TypeError, ValueError):
        return ""
    if not isinstance(data, dict) or not data.get("success"):
        return ""
    rows = data.get("data") or []
    if not rows:
        return ""
    first = rows[0]
    value: Any = None
    if isinstance(first, (list, tuple)) and len(first) >= 2:
        value = first[1]
    elif isinstance(first, dict):
        value = first.get("balance")
    if value is None:
        return ""
    return f"{value} 鸡腿"


def _reward(message: str) -> str:
    match = re.search(r"(\d+)\s*个?\s*鸡腿", message or "")
    return f"+{match.group(1)} 鸡腿" if match else ""


def _has_session(cookie: str) -> bool:
    """NodeSeek 系靠 ``session`` 认登录态；只有 cf_clearance 时接口会回 USER NOT FOUND。"""
    for chunk in (cookie or "").split(";"):
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        if key.strip() == "session" and value.strip():
            return True
    return False


class BoardSite(SiteAdapter):
    """NodeSeek 系站点适配器。"""

    base = ""
    direct_default = False
    help_text = "Cookie 直接贴整段（含 session），失效后脚本可一键推送"

    def cookie_ok(self, cookie: str) -> bool:
        """缺 session 的 Cookie 不算可用，否则会把手工 Cookie 顶掉。"""
        return _has_session(cookie)

    def run(self, ctx: Any) -> SiteResult:
        """先走 HTTP，遇到 Cloudflare 再走浏览器。"""
        cookie = ctx.cookie
        http_result = self._http_attempt(ctx, cookie)
        if http_result is not None:
            return http_result
        return self._browser_attempt(ctx)

    # ------------------------------------------------------------ HTTP
    def _http_attempt(self, ctx: Any, cookie: str) -> Optional[SiteResult]:
        """HTTP 签到；返回 None 表示需要浏览器兜底。"""
        resp = ctx.request(
            "POST", f"{self.base}{ATTENDANCE_PATH}",
            headers={
                "Accept": "application/json, text/plain, */*",
                "Origin": self.base,
                "Referer": f"{self.base}/board",
                "Content-Type": "application/json",
            },
        )
        if resp.cf_blocked:
            return None
        if resp.error:
            return SiteResult(ERROR, f"网络异常：{resp.error}")
        if resp.status in (401, 403):
            return SiteResult(NOAUTH, f"HTTP {resp.status}，Cookie 可能已失效")
        if resp.status == 0:
            return SiteResult(ERROR, "请求无响应")
        data = resp.json()
        if data is None:
            return SiteResult(ERROR, f"签到接口返回非 JSON（HTTP {resp.status}）")
        result = parse_attendance(data)
        if result.ok:
            result.balance = self._balance(ctx)
        return result

    def _balance(self, ctx: Any) -> str:
        """读一次余额，失败不影响签到结论。"""
        resp = ctx.request(
            "GET", f"{self.base}{CREDIT_PATH}",
            headers={"Accept": "application/json, text/plain, */*", "Referer": f"{self.base}/board"},
        )
        if resp.error or resp.status != 200:
            return ""
        return parse_credit(resp.text)

    # ---------------------------------------------------------- 浏览器
    def _browser_attempt(self, ctx: Any) -> SiteResult:
        """Cloudflare 拦截后的浏览器兜底。"""
        try:
            with ctx.browser(f"{self.base}/board") as page:
                passed = ctx.wait_cf(page)
                payload = page.evaluate(ATTENDANCE_JS)
        except Exception as exc:
            return SiteResult(BLOCKED, f"浏览器兜底失败：{type(exc).__name__}: {exc}")
        if not isinstance(payload, dict):
            return SiteResult(BLOCKED, "浏览器未返回数据")
        if payload.get("error"):
            return SiteResult(ERROR, f"浏览器内请求失败：{payload['error']}")
        status = payload.get("status")
        body = payload.get("body") or ""
        try:
            data = json.loads(body)
        except (TypeError, ValueError):
            if payload.get("cf") or "Just a moment" in body:
                return SiteResult(BLOCKED, f"Cloudflare 未放行（验证{'通过' if passed else '未通过'}）")
            return SiteResult(ERROR, f"浏览器内接口返回非 JSON（HTTP {status}）")
        result = parse_attendance(data)
        if result.ok:
            result.balance = parse_credit(payload.get("credit") or "")
        result.message = f"{result.message}（浏览器兜底）"
        return result
