"""猪猪（piggo.me）站内签到。

Vue SPA，签到入口在 `/calendar`：「签到 · +N」按钮 → 弹出阿里云验证码（FeiLin 滑块）→
通过后站点自己发签到请求。请求带 X-Spa-Nonce / X-Spa-Hmac 签名头，外部重放很难，
所以这里同样用共享 Chromium：点按钮、识别拼图缺口、模拟人手拖滑块，让页面自己完成签到。

执行全程拆成多次短 CDP 调用（每次只做一件事），避免单个 await 卡住整轮。
"""
from __future__ import annotations

import random
import time
from typing import Any

from .base import ALREADY, ERROR, FAIL, NOAUTH, SUCCESS, SiteAdapter, SiteResult
from .cdp import CdpError, open_page

CAL_URL = "https://piggo.me/calendar"
READY_TIMEOUT = 25
CAPTCHA_TIMEOUT = 20

STATE_JS = r"""
(() => {
  const t = (document.body.innerText || '').replace(/\s+/g, ' ');
  const win = document.getElementById('aliyunCaptcha-window-float');
  const pill = document.querySelector('.cal-att-pill');
  const logged = !!localStorage.getItem('pig_user') || /退出登录|usercp|个人中心/.test(t);
  return JSON.stringify({
    days: (t.match(/累计签到(\d+)天/) || [])[1] || '',
    streak: (t.match(/连续(\d+)天/) || [])[1] || '',
    pill: pill ? pill.textContent.trim() : null,
    ready: !!document.getElementById('aliyunCaptcha-img') || !!(win && win.getBoundingClientRect().width > 10),
    win: win ? Math.round(win.getBoundingClientRect().width) : 0,
    winText: win ? (win.textContent || '').replace(/\s+/g, ' ').slice(0, 40) : '',
    feed: (t.match(/饲料 \[使用\] \[勋章\]: ([0-9.,]+)/) || [])[1] || '',
    logged,
  });
})()
"""

CLICK_SIGN_JS = r"""
(() => {
  const pill = document.querySelector('.cal-att-pill');
  if (!pill) return 'no-pill';
  if (!/^\s*签到/.test(pill.textContent || '')) return 'already';
  pill.dispatchEvent(new MouseEvent('click', {bubbles: true}));
  return 'clicked';
})()
"""

OPEN_CAPTCHA_JS = r"""
(() => {
  const vis = el => el && el.getBoundingClientRect().width > 10;
  const win = document.getElementById('aliyunCaptcha-window-float');
  if (vis(win)) return 'window';
  const fw = document.getElementById('aliyunCaptcha-float-wrapper');
  if (fw && fw.getBoundingClientRect().height > 10 && !vis(win)) {
    const body = document.getElementById('aliyunCaptcha-captcha-body');
    if (body) {
      const r = body.getBoundingClientRect();
      for (const t of ['mousedown','mouseup','click']) {
        body.dispatchEvent(new MouseEvent(t, {bubbles:true, clientX:r.x+180, clientY:r.y+20}));
      }
      return 'clicked';
    }
  }
  return 'waiting';
})()
"""

ANALYZE_JS = r"""
(() => {
  const img = document.getElementById('aliyunCaptcha-img'), pz = document.getElementById('aliyunCaptcha-puzzle');
  const sl = document.getElementById('aliyunCaptcha-sliding-slider');
  if (!img || !pz || !sl || !img.naturalWidth) return JSON.stringify({err: 'no-parts'});
  const c = document.createElement('canvas'); c.width = img.naturalWidth; c.height = img.naturalHeight;
  const g = c.getContext('2d'); g.drawImage(img, 0, 0);
  const aw = c.width, ah = c.height;
  const ad = g.getImageData(0, 0, aw, ah).data;
  const pc = document.createElement('canvas'); pc.width = pz.naturalWidth; pc.height = pz.naturalHeight;
  const pg = pc.getContext('2d'); pg.drawImage(pz, 0, 0);
  const pw = pc.width, ph = pc.height;
  const bd = pg.getImageData(0, 0, pw, ph).data;
  const gray = new Float32Array(aw * ah);
  for (let i = 0; i < aw * ah; i++) gray[i] = (ad[i*4] + ad[i*4+1] + ad[i*4+2]) / 3;
  const grad = new Float32Array(aw * ah);
  for (let y = 1; y < ah - 1; y++) for (let x = 1; x < aw - 1; x++) {
    const i = y*aw + x;
    const gx = -gray[i-aw-1] - 2*gray[i-1] - gray[i+aw-1] + gray[i-aw+1] + 2*gray[i+1] + gray[i+aw+1];
    const gy = -gray[i-aw-1] - 2*gray[i-aw] - gray[i-aw+1] + gray[i+aw-1] + 2*gray[i+aw] + gray[i+aw+1];
    grad[i] = Math.sqrt(gx*gx + gy*gy);
  }
  const alpha = (x, y) => (x < 0 || y < 0 || x >= pw || y >= ph) ? 0 : bd[(y*pw+x)*4+3];
  const edge = [], inner = [];
  for (let y = 0; y < ph; y++) for (let x = 0; x < pw; x++) {
    if (alpha(x,y) <= 150) continue;
    const isEdge = alpha(x-1,y) <= 150 || alpha(x+1,y) <= 150 || alpha(x,y-1) <= 150 || alpha(x,y+1) <= 150;
    (isEdge ? edge : inner).push([x, y]);
  }
  const pzRect = pz.getBoundingClientRect(), imgRect = img.getBoundingClientRect();
  const yShift = Math.round((pzRect.y - imgRect.y) * ah / imgRect.height);
  const rows = [];
  for (let dx = 0; dx <= aw - pw; dx++) {
    let es = 0, en = 0;
    for (const [x, y] of edge) { const yy = y + yShift; if (yy < 1 || yy >= ah-1 || x+dx < 1 || x+dx >= aw-1) continue; es += grad[yy*aw + x + dx]; en++; }
    let bs = 0, bn = 0;
    for (const [x, y] of inner) { const yy = y + yShift; if (yy < 0 || yy >= ah) continue; bs += gray[yy*aw + x + dx]; bn++; }
    rows.push([dx, en ? es/en : 0, bn ? bs/bn : 0]);
  }
  const maxE = Math.max(...rows.map(r => r[1])) || 1;
  const maxB = Math.max(...rows.map(r => r[2])) || 1;
  rows.forEach(r => r.push(r[1]/maxE + r[2]/maxB));
  rows.sort((p, q) => q[3] - p[3]);
  const slr = sl.getBoundingClientRect();
  return JSON.stringify({dx: rows[0][0], top: rows.slice(0,3).map(r=>[r[0], Math.round(r[3]*100)/100]),
    aw, slider: {x: slr.x, y: slr.y, w: slr.width, h: slr.height},
    imgRect: {x: imgRect.x, y: imgRect.y, w: imgRect.width, h: imgRect.height}});
})()
"""


def _drag(browser: Any, sx: float, sy: float, dist: float) -> None:
    """模拟人手拖动：hover → 按下停顿 → 分段变速 → 末端微调 → 松手。"""
    for _ in range(2):
        browser.mouse("mouseMoved", sx + random.uniform(-25, 25), sy + random.uniform(-20, 20))
        time.sleep(0.1 + random.random() * 0.08)
    browser.mouse("mouseMoved", sx, sy)
    time.sleep(0.2 + random.random() * 0.15)
    browser.mouse("mousePressed", sx, sy, buttons=1, click_count=1)
    time.sleep(0.15 + random.random() * 0.2)
    cur = 0.0
    prev = 0.0
    for mark in (0.15, 0.35, 0.6, 0.82, 0.95, 1.0):
        seg = dist * (mark - prev)
        steps = max(2, int(abs(seg) / 4))
        for _ in range(steps):
            cur += seg / steps
            browser.mouse("mouseMoved", sx + cur, sy + random.uniform(-1.2, 1.2), buttons=1)
            time.sleep(0.014 + random.random() * 0.018)
        prev = mark
        if mark in (0.35, 0.6):
            time.sleep(0.05 + random.random() * 0.06)
    for adj, wait in ((-2.0, 0.12), (1.0, 0.1), (0.0, 0.18)):
        browser.mouse("mouseMoved", sx + dist + adj, sy + random.uniform(-0.8, 0.8), buttons=1)
        time.sleep(wait)
    browser.mouse("mouseReleased", sx + dist, sy, click_count=1)


class PiggoSite(SiteAdapter):
    id = "piggo"
    name = "猪猪"
    domain = "piggo.me"
    order = 70
    help_text = ("用共享浏览器（已登录猪猪）打开日历页点签到；阿里云滑块会自动尝试拖过，"
                 "过不去时会提醒你手动点一次。无需填 Cookie。")

    def cookie_ok(self, cookie: str) -> bool:
        return True

    def run(self, ctx: Any) -> SiteResult:
        browser = None
        try:
            browser, _ = open_page(CAL_URL, timeout=float(ctx.timeout or 60))
        except CdpError as exc:
            return SiteResult(FAIL, f"连不上共享浏览器：{exc}")

        try:
            # 1) 等页面把签到按钮渲染出来
            state = {}
            deadline = time.monotonic() + READY_TIMEOUT
            while time.monotonic() < deadline:
                time.sleep(1.5)
                state = browser.eval_json(STATE_JS) or {}
                if state.get("pill") is not None:
                    break
            days = str(state.get("days") or "")
            streak = str(state.get("streak") or "")
            pill = state.get("pill")

            if not state.get("logged"):
                return SiteResult(NOAUTH, "共享浏览器里猪猪未登录，请先登录一次")
            if pill is None:
                return SiteResult(ERROR, "日历页没找到签到按钮（站点可能改版）")
            if not str(pill).startswith("签到"):
                return SiteResult(ALREADY, "今天已经签到过了",
                                  extra=f"累计 {days} 天" if days else "")

            # 2) 点签到，等验证码浮层
            clicked = browser.eval(CLICK_SIGN_JS)
            if clicked != "clicked":
                state = browser.eval_json(STATE_JS) or {}
                return SiteResult(ALREADY if clicked == "already" else ERROR,
                                  "今天已经签到过了" if clicked == "already" else "没点到签到按钮")

            opened = False
            deadline = time.monotonic() + CAPTCHA_TIMEOUT
            while time.monotonic() < deadline:
                time.sleep(1.2)
                step = browser.eval(OPEN_CAPTCHA_JS)
                if step == "window":
                    opened = True
                    break
                if step == "waiting":
                    continue
            if not opened:
                # 没弹验证码，可能是无感验证直接过了
                time.sleep(3)
                now = browser.eval_json(STATE_JS) or {}
                return self._judge(days, streak, now)

            # 3) 识别缺口 + 拖滑块（只试一次，避免连续触发风控）
            info = browser.eval_json(ANALYZE_JS) or {}
            if info.get("err") or not info.get("aw"):
                return SiteResult(FAIL, "验证码组件结构变化，没能自动完成")
            scale = float(info["imgRect"]["w"]) / float(info["aw"])
            dist = max(8.0, min(float(info["dx"]) * scale, 258.0))
            sx = float(info["slider"]["x"]) + float(info["slider"]["w"]) / 2
            sy = float(info["slider"]["y"]) + float(info["slider"]["h"]) / 2
            _drag(browser, sx, sy, dist)
            time.sleep(4)
            now = browser.eval_json(STATE_JS) or {}
            result = self._judge(days, streak, now)
            if result.status in (SUCCESS, ALREADY):
                return result
            hint = "（页面提示：验证失败）" if "失败" in str(now.get("winText") or "") else ""
            return SiteResult(FAIL, f"阿里云滑块没拖过{hint}，请在共享浏览器手动点一次签到")
        except CdpError as exc:
            return SiteResult(FAIL, f"浏览器执行失败：{exc}")
        finally:
            if browser is not None:
                browser.close()

    @staticmethod
    def _judge(days: str, streak: str, now: dict) -> SiteResult:
        new_days = str(now.get("days") or "")
        pill = now.get("pill")
        gained = bool(days and new_days and new_days != days)
        if gained:
            return SiteResult(SUCCESS, f"签到成功，累计 {new_days} 天",
                              extra=f"连续 {now.get('streak') or streak} 天",
                              balance=str(now.get("feed") or ""))
        if pill is not None and not str(pill).startswith("签到"):
            return SiteResult(SUCCESS, "签到完成（按钮已变为已签状态）",
                              balance=str(now.get("feed") or ""))
        return SiteResult(FAIL, "没看到签到结果")
