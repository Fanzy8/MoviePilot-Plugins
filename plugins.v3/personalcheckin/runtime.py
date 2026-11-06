"""请求层：HTTP（含代理/直连）、Cloudflare 识别、CookieCloud 与隐身浏览器。

三种取数方式按顺序降级：
1. ``SiteContext.request`` —— requests，走插件配置的代理或直连；
2. ``SiteContext.browser`` —— MoviePilot 宿主自带的 cloakbrowser（真浏览器），
   用来过 Cloudflare 验证；
3. CookieCloud —— 由宿主已配置的 CookieCloud 服务刷新 Cookie。

所有网络入口都集中在这里，站点模块只描述协议。
"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

import requests

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
CF_TITLE_MARKS = ("Just a moment", "请稍候", "安全验证", "Attention Required")
CF_BODY_MARKS = (
    "cf-mitigated",
    "Just a moment",
    "__cf_chl",
    "cf_chl_opt",
    "正在检查浏览器",
    "正在进行安全验证",
)


def mp_proxy() -> str:
    """读取 MoviePilot 配置里的全局代理，读不到就返回空串。"""
    try:
        from app.sdk.config import settings  # type: ignore

        value = getattr(settings, "PROXY_HOST", "") or ""
        if value:
            return str(value).strip()
    except Exception:
        pass
    try:  # 兼容旧宿主 ABI
        from app.runtime.settings import get_runtime_setting  # type: ignore

        return str(get_runtime_setting("PROXY_HOST") or "").strip()
    except Exception:
        return ""


def cookie_parse(cookie: str) -> Dict[str, str]:
    """把 Cookie 请求头解析成字典。"""
    out: Dict[str, str] = {}
    for chunk in (cookie or "").split(";"):
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        key = key.strip()
        if key:
            out[key] = value.strip()
    return out


def cookie_header(cookie: str) -> str:
    """规范化 Cookie 请求头：去掉换行和多余空格。"""
    return "; ".join(
        f"{k}={v}" for k, v in cookie_parse(cookie).items()
    )


def cookie_value(cookie: str, name: str) -> str:
    """取单个 Cookie 的值。"""
    return cookie_parse(cookie).get(name, "")


def looks_like_cf(status: int, headers: Dict[str, str], body: str) -> bool:
    """判断响应是不是 Cloudflare 的挑战页。"""
    headers = {str(k).lower(): str(v).lower() for k, v in (headers or {}).items()}
    if headers.get("cf-mitigated") in ("challenge", "managed"):
        return True
    text = (body or "")[:4000]
    if status in (403, 429, 503) and any(mark in text for mark in CF_BODY_MARKS):
        return True
    return any(mark in text for mark in CF_TITLE_MARKS) and status >= 400


class HttpResponse:
    """对 requests.Response 的最小封装，便于离线测试替换。"""

    def __init__(self, status: int = 0, text: str = "", headers: Optional[Dict[str, str]] = None,
                 url: str = "", error: str = "") -> None:
        self.status = int(status or 0)
        self.text = text or ""
        self.headers = headers or {}
        self.url = url
        self.error = error

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 400 and not self.error

    @property
    def cf_blocked(self) -> bool:
        return looks_like_cf(self.status, self.headers, self.text)

    def json(self) -> Any:
        """解析 JSON，失败返回 None。"""
        try:
            return json.loads(self.text)
        except (TypeError, ValueError):
            return None

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<HttpResponse {self.status} {self.error or ''}>"


class SiteContext:
    """单个站点一次执行的上下文：Cookie、代理、HTTP 与浏览器。"""

    def __init__(self, site_id: str, name: str, cookie: str = "", direct: bool = False,
                 proxy: str = "", timeout: float = 60, ua: str = DEFAULT_UA,
                 browser: bool = True) -> None:
        self.site_id = site_id
        self.name = name
        self.cookie = cookie_header(cookie)
        self.direct = bool(direct)
        self.proxy = "" if self.direct else (proxy or "").strip()
        self.timeout = float(timeout)
        self.ua = ua
        self.browser_enabled = bool(browser)

    # ---------------------------------------------------------------- HTTP
    def _proxies(self) -> Optional[Dict[str, str]]:
        if not self.proxy:
            return None
        return {"http": self.proxy, "https": self.proxy}

    def request(self, method: str, url: str, headers: Optional[Dict[str, str]] = None,
                data: Any = None, json_body: Any = None, cookie: Optional[str] = None,
                allow_redirects: bool = True, timeout: Optional[float] = None) -> HttpResponse:
        """发一个 HTTP 请求，任何网络异常都变成 ``HttpResponse(error=...)``。"""
        hdrs = {
            "User-Agent": self.ua,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        hdrs.update(headers or {})
        raw_cookie = cookie if cookie is not None else self.cookie
        if raw_cookie and "cookie" not in {k.lower() for k in hdrs}:
            hdrs["Cookie"] = raw_cookie
        try:
            resp = requests.request(
                method.upper(), url, headers=hdrs, data=data, json=json_body,
                proxies=self._proxies(), timeout=timeout or self.timeout,
                allow_redirects=allow_redirects,
            )
            return HttpResponse(
                status=resp.status_code,
                text=resp.text or "",
                headers=dict(resp.headers or {}),
                url=str(resp.url),
            )
        except Exception as exc:  # 网络层异常（含超时、TLS、代理断开）
            return HttpResponse(error=f"{type(exc).__name__}: {exc}", url=url)

    # ------------------------------------------------------------- 浏览器
    @contextmanager
    def browser(self, url: str, cookie: Optional[str] = None,
                timeout: float = 90) -> Iterator[Any]:
        """启动宿主自带隐身浏览器并打开页面，退出时释放上下文。"""
        if not self.browser_enabled:
            raise RuntimeError("浏览器兜底未启用")
        from app.sdk.browser import launch_browser_context  # type: ignore

        kwargs: Dict[str, Any] = {"headless": True}
        if self.proxy:
            kwargs["proxy"] = {"server": self.proxy}
        kwargs["user_agent"] = self.ua
        try:
            context = launch_browser_context(**kwargs)
        except TypeError:
            # 某些宿主版本不接受 proxy/user_agent 形参
            context = launch_browser_context(headless=True)
        try:
            page = context.new_page()
            raw_cookie = cookie_header(cookie if cookie is not None else self.cookie)
            if raw_cookie:
                try:
                    page.set_extra_http_headers({"cookie": raw_cookie})
                except Exception:
                    pass
            page.set_default_timeout(int(timeout * 1000))
            page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
            yield page
        finally:
            try:
                context.close()
            except Exception:
                pass

    @staticmethod
    def wait_cf(page: Any, timeout: float = 75, interval: float = 3) -> bool:
        """等待 Cloudflare 挑战通过；返回 True 表示页面已进入正常内容。"""
        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            try:
                title = page.title() or ""
            except Exception:
                title = ""
            if title and not any(mark in title for mark in CF_TITLE_MARKS):
                return True
            try:
                text = page.evaluate("() => (document.body ? document.body.innerText.slice(0, 400) : '')") or ""
            except Exception:
                text = ""
            if text and not any(mark in text for mark in CF_BODY_MARKS) and "安全验证" not in text:
                return True
            try:
                page.wait_for_timeout(int(interval * 1000))
            except Exception:
                time.sleep(interval)
        return False

    @staticmethod
    def page_fetch(page: Any, expression: str) -> Any:
        """在页面内执行一段返回 Promise 的 JS，拿回可 JSON 化的结果。"""
        return page.evaluate(expression)


def cookiecloud_cookies() -> Dict[str, str]:
    """从宿主 CookieCloud 拉一次域名为键的 Cookie 映射，失败返回空字典。"""
    try:
        from app.adapters.external.cookiecloud import CookieCloudHelper  # type: ignore

        cookies, _error = CookieCloudHelper().download()
        return cookies or {}
    except Exception:
        return {}
