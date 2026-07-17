# sites/base.py - 农场站点配置基类
import re
from typing import Any, Dict, List, Optional, Tuple
from abc import ABC, abstractmethod


class FarmSiteConfig(ABC):
    """农场站点配置基类 - 新增站点继承此类"""
    
    # ========== 必填属性 ==========
    site_id: str = ""           # 站点标识（唯一）
    site_name: str = ""         # 站点显示名称
    domains: List[str] = []     # 域名列表（用于Cookie匹配）
    base_url: str = ""          # 基础URL
    currency: str = "魔力"      # 货币名称
    
    # ========== 可选属性 ==========
    farm_path: str = "/magic_fram.php"          # 农场页面路径
    warehouse_path: str = "/magic_fram.php"     # 仓库页面路径
    has_double_harvest: bool = False            # 是否支持双倍收获
    
    # ========== 作物配置 ==========
    crops: Dict[str, Dict] = {
        "crop_1": {"name": "小麦", "cost": 500, "type": "crop", "id": 1},
        "crop_2": {"name": "玉米", "cost": 1000, "type": "crop", "id": 2},
        "crop_3": {"name": "花生", "cost": 1500, "type": "crop", "id": 3},
        "crop_4": {"name": "土豆", "cost": 2000, "type": "crop", "id": 4},
        "animal_1": {"name": "鸡", "cost": 1000, "type": "animal", "id": 1},
        "animal_2": {"name": "猪", "cost": 2000, "type": "animal", "id": 2},
        "animal_3": {"name": "羊", "cost": 5000, "type": "animal", "id": 3},
        "animal_4": {"name": "牛", "cost": 10000, "type": "animal", "id": 4},
    }
    
    # ========== 子类必须实现的方法 ==========
    
    @abstractmethod
    def parse_market_prices(self, html: str) -> Dict[str, int]:
        """
        解析市场价格
        :param html: 农场页面HTML
        :return: {crop_key: price}
        """
        pass
    
    @abstractmethod
    def parse_crop_status(self, html: str) -> Dict[str, Dict]:
        """
        解析作物状态
        :param html: 农场页面HTML
        :return: {crop_key: {"can_harvest": bool}}
        """
        pass
    
    @abstractmethod
    def get_sell_key(self, html: str, item_type: str, item_id: int) -> Optional[str]:
        """
        获取出售key
        :param html: 农场页面HTML
        :param item_type: crop/animal
        :param item_id: 物品ID
        :return: sell_key 或 None
        """
        pass
    
    # ========== 可选重写的方法 ==========
    
    def get_farm_url(self) -> str:
        return f"{self.base_url}{self.farm_path}"
    
    def get_harvest_url(self, item_type: str, item_id: int) -> str:
        return f"{self.base_url}{self.farm_path}?action=harvest&type={item_type}&id={item_id}"
    
    def get_plant_url(self, item_type: str, item_id: int) -> str:
        return f"{self.base_url}{self.farm_path}?action=plant&type={item_type}&id={item_id}"
    
    def get_breed_url(self, item_type: str, item_id: int) -> str:
        return f"{self.base_url}{self.farm_path}?action=breed&type={item_type}&id={item_id}"
    
    def get_sell_url(self, sell_key: str) -> str:
        return f"{self.base_url}{self.farm_path}?action=sell&key={sell_key}"
    
    def get_harvest_all_url(self) -> str:
        return f"{self.base_url}{self.farm_path}?action=harvest_all"
    
    def get_warehouse_url(self) -> str:
        return f"{self.base_url}{self.warehouse_path}?sort=expire_asc"
    
    def check_auth(self, html: str) -> bool:
        return not ("请登录" in html or ("登录" in html and "签到" not in html))
    
    def get_name_to_key_map(self) -> Dict[str, str]:
        return {crop["name"]: key for key, crop in self.crops.items()}
    
    def get_crop_by_name(self, name: str) -> Optional[Tuple[str, Dict]]:
        for key, crop in self.crops.items():
            if crop["name"] == name:
                return key, crop
        return None
    
    def parse_harvest_result(self, html: str) -> Dict[str, Any]:
        double = False
        if self.has_double_harvest:
            double = "双倍" in html or "2倍" in html or "双倍收获" in html
        
        if "成功" in html or "收获" in html:
            return {"success": True, "double": double, "message": "收获成功"}
        elif "已收获" in html or "已经收获" in html:
            return {"success": True, "double": False, "message": "已收获"}
        return {"success": False, "double": False, "message": "收获失败"}
    
    def parse_plant_result(self, html: str, action: str) -> Dict[str, Any]:
        action_name = "种植" if action == "plant" else "养殖"
        if "成功" in html or action_name in html or "完成" in html:
            return {"success": True, "message": f"{action_name}成功"}
        return {"success": False, "message": f"{action_name}失败"}
    
    def parse_sell_result(self, html: str) -> Dict[str, Any]:
        if "成功" in html or "出售" in html or "获得" in html:
            return {"success": True, "message": "出售成功"}
        elif "已出售" in html or "已经出售" in html:
            return {"success": True, "message": "已出售"}
        return {"success": False, "message": "出售失败"}
    
    def parse_warehouse_items(self, html: str) -> List[Dict[str, Any]]:
        """
        解析仓库物品（通用方法，各站点可重写）
        默认格式: <h2>仓库</h2> ... <td>名称</td><td>数量</td><td>过期时间</td><td>sell_key</td>
        """
        items = []
        warehouse_start = html.find('<h2>仓库</h2>')
        if warehouse_start == -1:
            return items
        
        warehouse_html = html[warehouse_start:warehouse_start+8000]
        
        rows = re.findall(
            r'<tr>\s*<td>.*?</td>\s*<td>([^<]+)</td>\s*<td>(\d+)</td>\s*<td>([^<]+)</td>\s*<td>.*?action=sell&key=([^"\']+).*?</td>\s*</tr>',
            warehouse_html,
            re.DOTALL
        )
        
        for name, quantity, expire, sell_key in rows:
            items.append({
                "name": name.strip(),
                "quantity": int(quantity) if quantity.isdigit() else 1,
                "expire": expire.strip(),
                "sell_key": sell_key.strip().split('&')[0]
            })
        
        return items