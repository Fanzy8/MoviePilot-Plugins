# 自用签到（PersonalCheckin）设计

日期：2026-11-06　宿主：MoviePilot v3.2.3　插件版本：1.0.0

## 1. 目标

一个插件覆盖四个每日签到站点：NodeSeek、DeepFlood、linux.sb、yzyy 论坛。
要求：代理可配、失败重试、记录历史与余额、结束后发一条汇总通知、
以后加站点不需要改插件主体。

## 2. 已验证的站点协议（2026-11-06 实测）

| 站点 | 签到请求 | 结果判定 | 备注 |
|---|---|---|---|
| NodeSeek | `POST https://www.nodeseek.com/api/attendance?random=true` | JSON `success` / `message` 含「鸡腿」/「已完成」 | 余额：`GET /api/account/credit/page-1` → `data[0][1]` |
| DeepFlood | 同上，域名 `www.deepflood.com` | 同上 | 与 NodeSeek 同一套后端 |
| linux.sb | `GET /daily_checkin` 判态；未签则 `POST` 表单 `_csrf=<bbs_csrf cookie>`，带 `X-Requested-With` | 成功 `{"ok":1,...}`；页面含「今天已签到」 | 未登录会 302 到 `/login`；积分从页面正则取 |
| yzyy | `GET /plugin.php?id=zqlj_sign` → 提取 `&sign=<hex>` → GET | 结果页含「今日已打卡」/「最近奖励: X 影币」 | 站点慢（15–40s），直连优先 |

Cloudflare：NodeSeek / DeepFlood 对非浏览器指纹直接 `403 cf-mitigated: challenge`；
linux.sb 目前不挡；yzyy 偶发。故引入浏览器兜底。

## 3. 结构

```
plugins.v3/personalcheckin/
  __init__.py        生命周期、表单、页面、调度、历史、通知、API
  runtime.py         HTTP（代理/直连/超时）、CF 识别、cloakbrowser、CookieCloud
  sites/base.py      SiteAdapter / SiteResult / 状态常量
  sites/board.py     NodeSeek 系共享实现
  sites/{nodeseek,deepflood,linuxsb,yzyy}.py
  sites/__init__.py  SITES 注册表
```

新增站点：写一个 `SiteAdapter` 子类（`id/name/domain/direct_default/run(ctx)`），
在 `sites/__init__.py` 注册；表单、页面、调度、历史、通知自动包含。

## 4. 执行与重试

1. HTTP 直连或走代理（`SiteContext.request`），超时默认 60s；
2. 命中 Cloudflare（`cf-mitigated` 或挑战页特征）→ `SiteContext.browser` 启动
   宿主自带 cloakbrowser，注入 Cookie 头，打开 `/board`（或签到页），
   等待挑战通过后在页面上下文 `fetch` 接口；
3. 单站最多 3 次尝试；只有网络/解析异常（`ERROR`）重试，
   `BLOCKED` / `NOAUTH` / `FAIL` / `ALREADY` 不重试；
4. 直连站点的第 2 次尝试自动改走代理（`yzyy` 默认直连）。

## 5. 调度

- `CronTrigger(hour=8, minute=*)` 每分钟检查；
- 当天随机分钟（0–59）首次抽到即持久化到 `targets`，重启不漂移；
- 过了目标分钟且当天未执行才真正跑；
- 启动补跑：`DateTrigger(now+20s)`，若已过窗口且当天未跑则补一轮；
- 「立即运行一次」开关：持久化 `onlyonce_pending`，注册一次性任务后清除。

## 6. Cookie

优先级：CookieCloud（宿主已配置）→ 手工填写 → 空（记为 skipped）。
NodeSeek/DeepFlood 的 `session` 约 2 天过期，故提供
`tools/cookie-push`：用 `ego-browser nodejs` 读取四站 Cookie（含 HttpOnly），
POST 到 `/api/v1/plugin/PersonalCheckin/cookies`。

## 7. 数据与页面

每次运行、每站一条：`run_id, ts, site, name, status, message, reward, balance,
extra, attempts, trigger`；默认保留 90 天 / 最多 1000 条。
详情页：上次运行摘要、四张站点卡（今日状态/奖励/余额/连续天数）、最近 50 条历史。
旧 `YzyySignin` 的历史在首次加载时一次性导入（`legacy_imported` 标记）。

## 8. 通知

一次运行一条：`【自用签到】3/4 站完成` + 每站一行（状态、消息、奖励、余额、尝试次数）。
成功失败都发；`notify` 开关可关。

## 9. 风险

- Cloudflare 与出口 IP 绑定：`cf_clearance` 在换节点后失效，浏览器兜底也可能被挡，
  此时历史记录 `blocked`，不伪造成功。
- cloakbrowser 启动有内存与时间成本（单站数十秒）。
- yzyy 站点本身慢，直连失败应改走代理（已实现）。
