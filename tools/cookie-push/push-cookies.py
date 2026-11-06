#!/usr/bin/env python3
"""把 ego 浏览器里四站的登录 Cookie 推送到 MoviePilot「自用签到」插件。

用法（Windows，双击 更新Cookie.cmd 或命令行）：

    python push-cookies.py            # 只推送 Cookie
    python push-cookies.py --run      # 推送后立刻触发一轮签到
    python push-cookies.py --dry-run  # 只打印将推送哪些站，不落盘

前置条件：
1. ego 浏览器里四个站都已登录；
2. config.json 里填好 MoviePilot 地址与 API 令牌（可从环境变量
   MP_BASE_URL / MP_API_TOKEN 读取）。

脚本通过 `ego-browser nodejs` 读取浏览器 Cookie（含 HttpOnly），再用
MoviePilot 插件 API 写入配置，不需要手工复制粘贴。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_ID = "PersonalCheckin"

# 插件站点 id → 需要匹配的 Cookie 域名后缀
DOMAINS = {
    "nodeseek": "nodeseek.com",
    "deepflood": "deepflood.com",
    "linuxsb": "linux.sb",
    "yzyy": "yzyy.org",
}

NODE_SCRIPT = """
const fs = await import("node:fs/promises");
const task = await taskSpace("cookie-push");
const page = task.page("p1");
const all = await page.cdp("Network.getAllCookies");
const domains = %s;
const out = {};
for (const [site, domain] of Object.entries(domains)) {
  const parts = [];
  for (const c of all.cookies || []) {
    const d = String(c.domain || "").replace(/^\\./, "").toLowerCase();
    if (d === domain || d.endsWith("." + domain)) {
      parts.push(c.name + "=" + c.value);
    }
  }
  if (parts.length) out[site] = parts.join("; ");
}
await fs.writeFile(%s, JSON.stringify(out), "utf8");
console.log(JSON.stringify({sites: Object.keys(out), counts: Object.fromEntries(Object.entries(out).map(([k, v]) => [k, v.split("; ").length]))}));
"""


def load_config() -> tuple[str, str]:
    """读取 MP 地址与令牌：config.json 优先，环境变量兜底。"""
    config_path = HERE / "config.json"
    data = {}
    if config_path.exists():
        data = json.loads(config_path.read_text(encoding="utf-8"))
    base = str(data.get("mp_url") or os.environ.get("MP_BASE_URL") or "").rstrip("/")
    token = str(data.get("api_key") or os.environ.get("MP_API_TOKEN") or "")
    if not base or not token:
        raise SystemExit("缺少 MoviePilot 地址或 API 令牌：请在 config.json 填写 mp_url / api_key")
    return base, token


def read_cookies() -> dict:
    """调用 ego-browser 读四站 Cookie，返回 {站点: cookie 头}。"""
    tmp_dir = Path(tempfile.mkdtemp(prefix="personalcheckin-"))
    out_path = tmp_dir / "cookies.json"
    script = NODE_SCRIPT % (json.dumps(DOMAINS, ensure_ascii=False),
                            json.dumps(str(out_path).replace("\\", "/")))
    exe = os.environ.get("EGO_BROWSER") or "ego-browser"
    try:
        proc = subprocess.run([exe, "nodejs"], input=script.encode("utf-8"),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
    except FileNotFoundError:
        raise SystemExit("找不到 ego-browser 命令：请先安装 ego lite，或设置 EGO_BROWSER 指向可执行文件")
    except subprocess.TimeoutExpired:
        raise SystemExit("ego-browser 读取 Cookie 超时（180 秒）")
    if proc.returncode != 0:
        raise SystemExit("ego-browser 执行失败：\n" + proc.stderr.decode("utf-8", "replace")[-2000:])
    if not out_path.exists():
        raise SystemExit("ego-browser 没有产出 Cookie 文件：\n" + proc.stdout.decode("utf-8", "replace")[-1000:])
    data = json.loads(out_path.read_text(encoding="utf-8"))
    print("读取到：" + "，".join(f"{k}={len(v.split('; '))} 项" for k, v in data.items()))
    return data


def post_json(url: str, token: str, payload: dict | None = None, timeout: int = 60) -> dict:
    """POST 一个 JSON 到 MoviePilot，返回解析后的响应。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b"{}"
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("X-API-KEY", token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise SystemExit(f"HTTP {exc.code}：{detail}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"连不上 MoviePilot：{exc.reason}")


def main() -> int:
    """入口。"""
    parser = argparse.ArgumentParser(description="推送四站 Cookie 到自用签到插件")
    parser.add_argument("--run", action="store_true", help="推送后立即触发一轮签到")
    parser.add_argument("--dry-run", action="store_true", help="只读取并打印站点，不推送")
    args = parser.parse_args()

    base, token = load_config()
    cookies = read_cookies()
    missing = [site for site in DOMAINS if site not in cookies]
    if missing:
        print("注意：这些站没读到 Cookie（可能未登录）：" + ", ".join(missing))
    if args.dry_run:
        print("dry-run：" + json.dumps({k: len(v) for k, v in cookies.items()}, ensure_ascii=False))
        return 0
    if not cookies:
        print("没有任何 Cookie 可推送")
        return 1

    result = post_json(f"{base}/api/v1/plugin/{PLUGIN_ID}/cookies", token, cookies)
    print("推送结果：" + json.dumps(result, ensure_ascii=False))
    if not result.get("success"):
        return 1

    if args.run:
        run = post_json(f"{base}/api/v1/plugin/{PLUGIN_ID}/run", token, {}, timeout=600)
        data = run.get("data") or {}
        for row in data.get("results") or []:
            print(f"  {row.get('name')}: {row.get('status')} {row.get('message')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
