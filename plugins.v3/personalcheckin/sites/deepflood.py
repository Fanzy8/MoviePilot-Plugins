"""DeepFlood 站适配器（与 NodeSeek 同一套后端，仅域名不同）。"""
from .board import BoardSite


class DeepFloodSite(BoardSite):
    """DeepFlood 签到：POST /api/attendance?random=true。"""

    id = "deepflood"
    name = "DeepFlood"
    domain = "deepflood.com"
    base = "https://www.deepflood.com"
    order = 20
    help_text = "登录 www.deepflood.com 后复制整段 Cookie（必须含 session）"
