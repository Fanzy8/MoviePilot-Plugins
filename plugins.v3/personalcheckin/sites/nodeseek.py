"""NodeSeek 站适配器（/board 页面签到，奖励鸡腿）。"""
from .board import BoardSite


class NodeSeekSite(BoardSite):
    """NodeSeek 签到：POST /api/attendance?random=true。"""

    id = "nodeseek"
    name = "NodeSeek"
    domain = "nodeseek.com"
    base = "https://www.nodeseek.com"
    order = 10
    help_text = "登录 www.nodeseek.com 后复制整段 Cookie（必须含 session）"
