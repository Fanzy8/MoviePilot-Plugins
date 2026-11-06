"""站点适配器基类：所有站点共用的结果结构与生命周期约定。

新增一个站点只需要在本包的 ``sites/`` 目录放一个模块，继承 ``SiteAdapter``，
实现 ``run(ctx)``，再在 ``sites/__init__.py`` 的 ``SITES`` 里注册一行。
插件表单、页面、调度、通知、历史都按注册表自动生成。
"""
from dataclasses import dataclass
from typing import Any, Dict

# 站点结果状态
SUCCESS = "success"      # 本次签到成功
ALREADY = "already"      # 今日已签到
FAIL = "fail"            # 站点明确拒绝/返回失败
BLOCKED = "blocked"      # Cloudflare 等风控拦截
NOAUTH = "noauth"        # Cookie 缺失或已失效
ERROR = "error"          # 网络/解析异常
SKIPPED = "skipped"      # 未启用或未配置

STATUS_TEXT = {
    SUCCESS: "成功",
    ALREADY: "已签到",
    FAIL: "失败",
    BLOCKED: "CF 拦截",
    NOAUTH: "未登录",
    ERROR: "异常",
    SKIPPED: "跳过",
}
STATUS_ICON = {
    SUCCESS: "✅",
    ALREADY: "🟢",
    FAIL: "❌",
    BLOCKED: "🛡️",
    NOAUTH: "🔑",
    ERROR: "⚠️",
    SKIPPED: "⏭️",
}


@dataclass
class SiteResult:
    """一次站点执行的结果。"""

    status: str = ERROR
    message: str = ""
    reward: str = ""
    balance: str = ""
    extra: str = ""
    attempts: int = 1

    @property
    def ok(self) -> bool:
        """成功或已签到都算“今天这一站没事了”。"""
        return self.status in (SUCCESS, ALREADY)

    @property
    def retryable(self) -> bool:
        """只有网络/解析异常值得重试；站点明确拒绝和拦截不重试。"""
        return self.status == ERROR


class SiteAdapter:
    """站点适配器基类。"""

    id: str = ""
    name: str = ""
    domain: str = ""            # CookieCloud 归组用的根域名
    direct_default: bool = False
    help_text: str = ""
    order: int = 100

    def cookie_ok(self, cookie: str) -> bool:
        """Cookie 是否可用于本站（子类可覆写做更细的校验）。"""
        return bool((cookie or "").strip())

    def run(self, ctx: Any) -> SiteResult:  # pragma: no cover - 抽象方法
        """执行一次签到。"""
        raise NotImplementedError

    def describe(self) -> str:
        """表单里显示的一句话说明。"""
        return self.help_text or f"{self.name} 每日签到"


def result_kwargs(result: SiteResult) -> Dict[str, Any]:
    """把结果整理成历史记录里用的字段。"""
    return {
        "status": result.status,
        "message": result.message,
        "reward": result.reward,
        "balance": result.balance,
        "extra": result.extra,
        "attempts": result.attempts,
    }
