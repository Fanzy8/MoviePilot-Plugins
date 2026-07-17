# sites/playlet.py - PlayLet站点配置
import re
from typing import Dict, Optional
from .base import FarmSiteConfig


class PlayLetConfig(FarmSiteConfig):
    site_id = "playlet"
    site_name = "PlayLet"
    domains = ["playlet.cc", "www.playlet.cc"]
    base_url = "https://playlet.cc"
    currency = "魔力"
    
    def parse_market_prices(self, html: str) -> Dict[str, int]:
        result = {}
        name_to_key = self.get_name_to_key_map()
        
        market_start = html.find('<h2>菜市场</h2>')
        if market_start != -1:
            market_html = html[market_start:market_start+6000]
            
            crop_section_start = market_html.find('农作物种植区')
            if crop_section_start != -1:
                crop_html = market_html[crop_section_start:crop_section_start+2000]
                for name, price in re.findall(r'<td>([^<]+)</td>\s*<td>(\d+)</td>', crop_html):
                    name = name.strip()
                    if name in name_to_key:
                        result[name_to_key[name]] = int(price)
            
            animal_section_start = market_html.find('动物养殖区')
            if animal_section_start != -1:
                animal_html = market_html[animal_section_start:animal_section_start+2000]
                for name, price in re.findall(r'<td>([^<]+)</td>\s*<td>(\d+)</td>', animal_html):
                    name = name.strip()
                    if name in name_to_key and name not in result:
                        result[name_to_key[name]] = int(price)
                
                if "animal_4" not in result:
                    cow_match = re.search(r'<td>牛</td>\s*<td>(\d+)</td>', animal_html)
                    if cow_match:
                        result["animal_4"] = int(cow_match.group(1))
        
        if len(result) < 8:
            market_pos = html.find('菜市场')
            if market_pos != -1:
                for name, price in re.findall(
                    r'<tr>\s*<td>.*?</td>\s*<td>([^<]+)</td>\s*<td>(\d+)</td>\s*</tr>',
                    html[market_pos:], re.DOTALL
                ):
                    name = name.strip()
                    if name in name_to_key and name not in result:
                        result[name_to_key[name]] = int(price)
        
        if "animal_4" not in result:
            for pattern in [r'<td>牛</td>\s*<td>(\d+)</td>', r'牛</td>\s*<td>(\d+)</td>', r'牛.*?<td>(\d+)</td>']:
                match = re.search(pattern, html)
                if match:
                    price = int(match.group(1))
                    if 5000 <= price <= 20000:
                        result["animal_4"] = price
                        break
        
        return result
    
    def parse_crop_status(self, html: str) -> Dict[str, Dict]:
        result = {}
        for key, crop in self.crops.items():
            if rf'action=harvest&type={crop["type"]}&id={crop["id"]}' in html:
                result[key] = {"can_harvest": True}
            else:
                result[key] = {"can_harvest": False}
        return result
    
    def get_sell_key(self, html: str, item_type: str, item_id: int) -> Optional[str]:
        return f"{item_type}_{item_id}"