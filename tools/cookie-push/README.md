# 一键推送 Cookie（Windows）

把 ego 浏览器里 NodeSeek / DeepFlood / linux.sb / yzyy 的登录 Cookie
推送到 MoviePilot 的「自用签到」插件，免去手工复制。

## 准备

1. `config.example.json` 复制成 `config.json`，填写：
   - `mp_url`：MoviePilot 地址（如 `http://100.107.236.19:13000`）
   - `api_key`：MoviePilot「设定 → 系统 → API 令牌」
2. 确认 `ego-browser` 在 PATH 里（`ego-browser --version` 能跑）。
3. 四个站在 ego 浏览器里处于登录状态。

## 使用

- 双击 `更新Cookie.cmd`：只推送 Cookie。
- 命令行 `python push-cookies.py --run`：推送后立刻跑一轮签到。
- `python push-cookies.py --dry-run`：只读 Cookie，不推送。

## 说明

- 读取的是浏览器当前 Cookie（含 HttpOnly 的 `session` / `bbs_auth`），
  不经过截图或剪贴板。
- NodeSeek / DeepFlood 的 `session` 只活两天左右，建议在 Windows
  计划任务里每天跑一次本脚本；插件本身也会优先使用 CookieCloud。
- 站点开了 Cloudflare 时，插件会降级到宿主自带隐身浏览器；此时
  Cookie 里的 `cf_clearance` 只在同出口 IP 下有效，换节点后失败属正常，
  插件会在历史里记录「CF 拦截」。
