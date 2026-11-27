"""共享 Chromium 的 CDP 客户端：给需要真浏览器的站点用（Turnstile / 阿里云滑块）。

共享 Chromium 容器（jlesage/chromium）暴露 CDP 9222，登录态跨重启保留。
插件在 MP 容器里直连它，后台开标签页执行，不打扰用户当前页面。
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Any, Optional, Tuple

try:
    import websocket  # type: ignore  # websocket-client，MP 镜像自带
except Exception:  # pragma: no cover
    websocket = None

DEFAULT_CDP = os.environ.get("PERSONALCHECKIN_CDP_URL", "http://192.168.31.211:9222")


class CdpError(RuntimeError):
    pass


class CdpBrowser:
    """一次会话：连上浏览器，按需开标签页、执行 JS、发鼠标事件。"""

    def __init__(self, endpoint: str = "", timeout: float = 60) -> None:
        if websocket is None:
            raise CdpError("缺少 websocket-client，无法连接共享浏览器")
        self.endpoint = (endpoint or DEFAULT_CDP).rstrip("/")
        self.timeout = timeout
        self.ws = None
        self._mid = 0
        self.session_id = ""
        self.target_id = ""

    # ---------------------------------------------------------------- 连接
    def connect(self) -> "CdpBrowser":
        try:
            ver = json.load(urllib.request.urlopen(self.endpoint + "/json/version", timeout=10))
        except Exception as exc:
            raise CdpError(f"连不上共享浏览器 {self.endpoint}：{type(exc).__name__}: {exc}")
        ws_url = ver.get("webSocketDebuggerUrl")
        if not ws_url:
            raise CdpError("共享浏览器没返回 webSocketDebuggerUrl")
        self.ws = websocket.create_connection(ws_url, timeout=self.timeout, max_size=None)
        return self

    def send(self, method: str, params: Optional[dict] = None, session_id: Optional[str] = None) -> dict:
        if self.ws is None:
            raise CdpError("CDP 未连接")
        self._mid += 1
        msg: dict = {"id": self._mid, "method": method, "params": params or {}}
        sid = self.session_id if session_id is None else session_id
        if sid:
            msg["sessionId"] = sid
        self.ws.send(json.dumps(msg))
        while True:
            raw = self.ws.recv()
            if not raw:
                raise CdpError("CDP 连接被关闭")
            data = json.loads(raw)
            if data.get("id") == self._mid:
                if "error" in data:
                    raise CdpError(f"{method} 失败：{data['error']}")
                return data.get("result", {})

    def open_tab(self, url: str, activate: bool = False) -> str:
        res = self.send("Target.createTarget", {"url": url, "background": not activate}, "")
        self.target_id = res["targetId"]
        att = self.send("Target.attachToTarget", {"targetId": self.target_id, "flatten": True}, "")
        self.session_id = att["sessionId"]
        return self.target_id

    def close_tab(self) -> None:
        try:
            if self.target_id:
                self.send("Target.closeTarget", {"targetId": self.target_id}, "")
        except Exception:
            pass
        self.target_id = ""
        self.session_id = ""

    def close(self) -> None:
        self.close_tab()
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        self.ws = None

    # ------------------------------------------------------------- 执行 JS
    def eval(self, expression: str, await_promise: bool = True) -> Any:
        res = self.send("Runtime.evaluate", {
            "expression": expression,
            "awaitPromise": await_promise,
            "returnByValue": True,
            "userGesture": True,
        })
        if res.get("exceptionDetails"):
            detail = res["exceptionDetails"]
            raise CdpError("页面脚本出错：" + str(detail.get("text") or detail)[:200])
        return res.get("result", {}).get("value")

    def eval_json(self, expression: str) -> Any:
        value = self.eval(expression)
        if value is None:
            return None
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value

    def wait_for(self, expression: str, timeout: float = 30, interval: float = 1.0) -> bool:
        """轮询一段返回布尔值的 JS，直到为真或超时。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.eval(expression):
                    return True
            except Exception:
                pass
            time.sleep(interval)
        return False

    # ------------------------------------------------------------- 鼠标
    def mouse(self, kind: str, x: float, y: float, buttons: int = 0, click_count: int = 0) -> None:
        params: dict = {"type": kind, "x": round(float(x), 1), "y": round(float(y), 1)}
        if kind != "mouseMoved" or buttons:
            params["button"] = "left"
        if buttons:
            params["buttons"] = buttons
        if click_count:
            params["clickCount"] = click_count
        self.send("Input.dispatchMouseEvent", params)


def open_page(url: str, endpoint: str = "", timeout: float = 60, activate: bool = False) -> Tuple[CdpBrowser, str]:
    """连上共享浏览器并开一个后台标签页，返回 (browser, target_id)。"""
    browser = CdpBrowser(endpoint, timeout=timeout).connect()
    try:
        browser.open_tab(url, activate=activate)
    except Exception:
        browser.close()
        raise
    return browser, browser.target_id


def page_text_expr(limit: int = 4000) -> str:
    return f"(document.body ? document.body.innerText : '').replace(/\\s+/g, ' ').slice(0, {int(limit)})"
