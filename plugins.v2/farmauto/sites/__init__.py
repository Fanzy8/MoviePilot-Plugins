# sites/__init__.py - 站点配置注册
from typing import Dict, Optional, List
from .base import FarmSiteConfig
from .playlet import PlayLetConfig
from .novahd import NovaHDConfig
from .haoxue import HaoxueConfig
from .baozi import BaoziConfig

# 所有支持的站点配置
SITE_CONFIGS: Dict[str, FarmSiteConfig] = {
    "playlet": PlayLetConfig(),
    "novahd": NovaHDConfig(),
    "haoxue": HaoxueConfig(),
    "baozi": BaoziConfig(),
}

# 站点选项（用于UI多选）
SITE_OPTIONS = [
    {"title": config.site_name, "value": site_id}
    for site_id, config in SITE_CONFIGS.items()
]


def get_site_config(site_id: str) -> Optional[FarmSiteConfig]:
    """获取站点配置"""
    return SITE_CONFIGS.get(site_id)


def get_all_site_configs() -> List[FarmSiteConfig]:
    """获取所有站点配置"""
    return list(SITE_CONFIGS.values())


def register_site_config(site_id: str, config: FarmSiteConfig) -> None:
    """注册新站点配置"""
    SITE_CONFIGS[site_id] = config