"""站点协议解析测试：全部用固定样本，不联网。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bootstrap import load_plugin  # noqa: E402

MODULE = load_plugin()
BOARD = sys.modules["personalcheckin.sites.board"]
LINUXSB = sys.modules["personalcheckin.sites.linuxsb"]
YZYY = sys.modules["personalcheckin.sites.yzyy"]
BASE = sys.modules["personalcheckin.sites.base"]
RUNTIME = sys.modules["personalcheckin.runtime"]


class BoardParseTest(unittest.TestCase):
    """NodeSeek / DeepFlood 签到返回解析。"""

    def test_success(self):
        result = BOARD.parse_attendance({"success": True, "message": "签到成功，获得 5 个鸡腿"})
        self.assertEqual(result.status, BASE.SUCCESS)
        self.assertEqual(result.reward, "+5 鸡腿")

    def test_already(self):
        result = BOARD.parse_attendance({"success": False, "message": "你今天已经签到过了"})
        self.assertEqual(result.status, BASE.ALREADY)

    def test_noauth(self):
        result = BOARD.parse_attendance({"success": False, "message": "请先登录", "status": 401})
        self.assertEqual(result.status, BASE.NOAUTH)

    def test_fail(self):
        result = BOARD.parse_attendance({"success": False, "message": "操作过于频繁"})
        self.assertEqual(result.status, BASE.FAIL)

    def test_garbage(self):
        self.assertEqual(BOARD.parse_attendance(None).status, BASE.ERROR)
        self.assertEqual(BOARD.parse_attendance({}).status, BASE.FAIL)

    def test_credit_list(self):
        text = '{"success": true, "data": [[5, 128, "签到收益", "2026-11-06T00:00:00Z"]]}'
        self.assertEqual(BOARD.parse_credit(text), "128 鸡腿")

    def test_credit_dict(self):
        text = '{"success": true, "data": [{"amount": 5, "balance": 99}]}'
        self.assertEqual(BOARD.parse_credit(text), "99 鸡腿")

    def test_credit_bad(self):
        self.assertEqual(BOARD.parse_credit(""), "")
        self.assertEqual(BOARD.parse_credit("<html>"), "")
        self.assertEqual(BOARD.parse_credit('{"success": false}'), "")


class LinuxSbParseTest(unittest.TestCase):
    """linux.sb 签到页与签到返回解析。"""

    PAGE = """
    <html><body><div class="menu">每日签到 今天已签到 3 连续天数 27 累计签到 已完成 Fanzy 积分 3459</div></body></html>
    """

    def test_page_state(self):
        signed, reward, streak, balance = LINUXSB.parse_page_state(self.PAGE)
        self.assertTrue(signed)
        self.assertEqual(reward, "+3 积分")
        self.assertEqual(streak, "连续 27 天")
        self.assertEqual(balance, "3459 积分")

    def test_page_not_signed(self):
        signed, _reward, _streak, balance = LINUXSB.parse_page_state(
            "<div>每日签到 点击签到 余额 积分 100</div>")
        self.assertFalse(signed)
        self.assertEqual(balance, "100 积分")

    def test_checkin_ok(self):
        result = LINUXSB.parse_checkin_json({"ok": 1, "message": "签到成功，获得 5 积分"})
        self.assertEqual(result.status, BASE.SUCCESS)
        self.assertEqual(result.reward, "+5 积分")

    def test_checkin_already(self):
        result = LINUXSB.parse_checkin_json({"ok": 0, "message": "今天已签到"})
        self.assertEqual(result.status, BASE.ALREADY)

    def test_checkin_garbage(self):
        self.assertEqual(LINUXSB.parse_checkin_json("x").status, BASE.ERROR)


class YzyyParseTest(unittest.TestCase):
    """yzyy 签到页与结果页解析。"""

    SIGN_PAGE = """
    <div class="signbtn"><a href="plugin.php?id=zqlj_sign&sign=be2c3c83">点击打卡</a></div>
    """
    DONE_PAGE = """
    <div class="signbtn">今日已打卡</div>
    """

    def test_button_state(self):
        self.assertEqual(YZYY.button_state(self.SIGN_PAGE), "need")
        self.assertEqual(YZYY.button_state(self.DONE_PAGE), "already")

    def test_extract_sign_url(self):
        url = YZYY.extract_sign_url(self.SIGN_PAGE)
        self.assertEqual(url, "https://yzyy.org/plugin.php?id=zqlj_sign&sign=be2c3c83")

    def test_extract_fallback_regex(self):
        url = YZYY.extract_sign_url("<div>sign=02639b80</div>")
        self.assertEqual(url, "https://yzyy.org/plugin.php?id=zqlj_sign&sign=02639b80")

    def test_parse_success(self):
        page = "最近奖励：10.5 影币 连续打卡：3 天 累计打卡：27 天 最近打卡：2026-11-06"
        result = YZYY.parse_result(page)
        self.assertEqual(result.status, BASE.SUCCESS)
        self.assertEqual(result.reward, "+10.5 影币")
        self.assertIn("连续 3 天", result.extra)

    def test_parse_already(self):
        result = YZYY.parse_result("今日已打卡 最近奖励：5 影币")
        self.assertEqual(result.status, BASE.ALREADY)

    def test_parse_fail(self):
        result = YZYY.parse_result("错误：权限不足")
        self.assertEqual(result.status, BASE.FAIL)

    def test_logged_out(self):
        self.assertTrue(YZYY.is_logged_out("<a>请登录</a>"))
        self.assertFalse(YZYY.is_logged_out("<a>签到</a>"))


class CfDetectTest(unittest.TestCase):
    """Cloudflare 挑战识别。"""

    def test_header_marker(self):
        self.assertTrue(RUNTIME.looks_like_cf(403, {"cf-mitigated": "challenge"}, ""))

    def test_body_marker(self):
        self.assertTrue(RUNTIME.looks_like_cf(403, {}, "<title>Just a moment...</title>"))

    def test_normal_page(self):
        self.assertFalse(RUNTIME.looks_like_cf(200, {}, "<html>ok</html>"))
        self.assertFalse(RUNTIME.looks_like_cf(404, {}, "not found"))


class CookieParseTest(unittest.TestCase):
    """Cookie 头解析。"""

    def test_parse_and_header(self):
        raw = " a=1 ; b=2;  broken ; c=3 "
        self.assertEqual(RUNTIME.cookie_parse(raw), {"a": "1", "b": "2", "c": "3"})
        self.assertEqual(RUNTIME.cookie_header(raw), "a=1; b=2; c=3")
        self.assertEqual(RUNTIME.cookie_value(raw, "b"), "2")


if __name__ == "__main__":
    unittest.main()
