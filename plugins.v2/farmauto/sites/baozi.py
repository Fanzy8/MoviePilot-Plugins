# sites/baozi.py - 包子站点配置
import re
from typing import Dict, Optional, List, Any
from .base import FarmSiteConfig


class BaoziConfig(FarmSiteConfig):
    site_id = "baozi"
    site_name = "包子"
    domains = ["p.t-baozi.cc", "t-baozi.cc", "baozi.cc"]
    base_url = "https://p.t-baozi.cc"
    currency = "魔力"
    
    def parse_market_prices(self, html: str) -> Dict[str, int]:
        result = {}
        name_to_key = self.get_name_to_key_map()
        
        for name, price in re.findall(
            r'<tr>\s*<td>([^<]+)</td>\s*<td>(\d+)</td>\s*</tr>',
            html, re.DOTALL
        ):
            name = name.strip()
            if name in name_to_key:
                result[name_to_key[name]] = int(price)
        
        if not result:
            for name, price in re.findall(
                r'<td><img[^>]+></td>\s*<td>([^<]+)</td>\s*<td>(\d+)</td>',
                html, re.DOTALL
            ):
                name = name.strip()
                if name in name_to_key:
                    result[name_to_key[name]] = int(price)
        
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
        包子站点仓库解析
        结构: <tr><td>名称</td><td>数量</td><td>过期时间</td><td><a href="?action=sell&key=xxx">出售</a></td></tr>
        注意: 无 <h2>仓库</h2> 标签，无收获时间列
        """
        items = []
        
        # 查找仓库区域（通过注释 <!-- 仓库 --> 或直接找表格）
        warehouse_start = html.find('<!-- 仓库 -->')
        if warehouse_start == -1:
            warehouse_start = html.find('<h2>仓库</h2>')
        if warehouse_start == -1:
            # 尝试直接找仓库表格
            warehouse_start = html.find('<div class="farm-section">')
            # 找包含"仓库"的section
            sections = re.findall(r'<div class="farm-section">(.*?)</div>', html, re.DOTALL)
            for section in sections:
                if '仓库' in section:
                    warehouse_html = section
                    break
            else:
                return items
        else:
            warehouse_html = html[warehouse_start:warehouse_start+5000]
        
        # 匹配仓库表格行
        # 包子格式: <td>名称</td><td>数量</td><td>过期时间</td><td><a href="?action=sell&key=xxx">出售</a></td>
        # 注意：没有收获时间列
        pattern = r'<tr>\s*<td>([^<]+)</td>\s*<td>(\d+)</td>\s*<td>([^<]+)</td>\s*<td>.*?action=sell&key=([^"\']+).*?</td>\s*</tr>'
        matches = re.findall(pattern, warehouse_html, re.DOTALL)
        
        for name, quantity, expire, sell_key in matches:
            name = name.strip()
            # 清理sell_key（去掉可能的 &page=1 等参数）
            sell_key = sell_key.strip().split('&')[0]
            items.append({
                "name": name,
                "quantity": int(quantity) if quantity.isdigit() else 1,
                "expire": expire.strip(),
                "sell_key": sell_key
            })
        
        return items
    
    def get_sell_key(self, html: str, item_type: str, item_id: int) -> Optional[str]:
        return f"{item_type}_{item_id}"