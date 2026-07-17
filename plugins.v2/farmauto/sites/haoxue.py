# sites/haoxue.py - 好学站点配置
import re
from typing import Dict, Optional, List, Any
from .base import FarmSiteConfig


class HaoxueConfig(FarmSiteConfig):
    site_id = "haoxue"
    site_name = "好学"
    domains = ["www.hxpt.org", "hxpt.org"]
    base_url = "https://www.hxpt.org"
    currency = "火花"
    has_double_harvest = False
    
    def parse_market_prices(self, html: str) -> Dict[str, int]:
        result = {}
        name_to_key = self.get_name_to_key_map()
        
        pattern = r'<td[^>]*>([^<]+)</td>\s*(?:<!--.*?-->)?\s*<td[^>]*>(\d+)</td>'
        matches = re.findall(pattern, html, re.DOTALL)
        
        for name, price in matches:
            name = name.strip()
            if name in name_to_key and name not in result:
                price_val = int(price)
                if 100 <= price_val <= 20000:
                    result[name_to_key[name]] = price_val
        
        return result
    
    def parse_crop_status(self, html: str) -> Dict[str, Dict]:
        result = {}
        for key, crop in self.crops.items():
            if rf'action=harvest&type={crop["type"]}&id={crop["id"]}' in html:
                result[key] = {"can_harvest": True}
            else:
                result[key] = {"can_harvest": False}
        return result
    
    def parse_warehouse_items(self, html: str) -> List[Dict[str, Any]]:
        """
        好学站点仓库解析
        结构: <tr><td>名称</td><td>数量</td><td>收获时间</td><td>过期时间</td><td><a href="?action=sell&key=xxx">出售</a></td></tr>
        """
        items = []
        
        warehouse_start = html.find('<h2>仓库</h2>')
        if warehouse_start == -1:
            return items
        
        warehouse_html = html[warehouse_start:warehouse_start+8000]
        
        # 好学格式: 名称, 数量, 收获时间, 过期时间, sell_key
        # 匹配: <td>名称</td><td>数量</td><td>收获时间</td><td>过期时间</td><td><a href="?action=sell&key=xxx">出售</a></td>
        pattern = r'<tr>\s*<td>([^<]+)</td>\s*<td>(\d+)</td>\s*<td>[^<]+</td>\s*<td>([^<]+)</td>\s*<td>.*?action=sell&key=([^"\']+).*?</td>\s*</tr>'
        matches = re.findall(pattern, warehouse_html, re.DOTALL)
        
        for name, quantity, expire, sell_key in matches:
            name = name.strip()
            sell_key = sell_key.strip().split('&')[0]
            items.append({
                "name": name,
                "quantity": int(quantity) if quantity.isdigit() else 1,
                "expire": expire.strip(),
                "sell_key": sell_key
            })
        
        return items
    
    def get_sell_key(self, html: str, item_type: str, item_id: int) -> Optional[str]:
        pattern = rf'action=sell&key={item_type}_{item_id}_\d+'
        match = re.search(pattern, html)
        if match:
            return match.group(0).replace('action=sell&key=', '')
        
        match2 = re.search(rf'{item_type}_{item_id}_\d+', html)
        if match2:
            return match2.group(0)
        
        return None