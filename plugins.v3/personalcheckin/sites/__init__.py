"""站点注册表：加新站 = 新增一个模块 + 这里加一行。"""
from typing import Dict, Tuple

from .base import (ALREADY, BLOCKED, ERROR, FAIL, NOAUTH, SKIPPED, STATUS_ICON,
                   STATUS_TEXT, SUCCESS, SiteAdapter, SiteResult)
from .deepflood import DeepFloodSite
from .linuxsb import LinuxSbSite
from .nodeseek import NodeSeekSite
from .opencd import OpencdSite
from .playlet import PlayletSite
from .siqi import SiqiSite
from .yzyy import YzyySite

SITES: Tuple[SiteAdapter, ...] = tuple(
    sorted(
        (
            NodeSeekSite(), DeepFloodSite(), LinuxSbSite(), YzyySite(),
            OpencdSite(), PlayletSite(), SiqiSite(),
        ),
        key=lambda item: item.order,
    )
)
SITE_MAP: Dict[str, SiteAdapter] = {site.id: site for site in SITES}

__all__ = [
    "SITES", "SITE_MAP", "SiteAdapter", "SiteResult",
    "SUCCESS", "ALREADY", "FAIL", "BLOCKED", "NOAUTH", "ERROR", "SKIPPED",
    "STATUS_ICON", "STATUS_TEXT",
]
