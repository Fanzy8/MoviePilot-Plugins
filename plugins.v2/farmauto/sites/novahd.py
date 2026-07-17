# sites/novahd.py - NovaHD站点配置（修复价格解析）
import re
from typing import Dict, Optional
from .base import FarmSiteConfig

# 添加 logger 导入
from app.log import logger


class NovaHDConfig(FarmSiteConfig):
    site_id = "novahd"
    site_name = "NovaHD"
    domains = ["pt.novahd.top", "novahd.top"]
    base_url = "https://pt.novahd.top"
    currency = "魔力"
    
    def parse_market_prices(self, html: str) -> Dict[str, int]:
        """
        NovaHD站点价格解析
        表格结构: <tr><td><img...></td><td>物品名</td><td>价格</td></tr>
        需要跳过图片列，正确匹配第二个td（物品名）和第三个td（价格）
        """
        result = {}
        name_to_key = self.get_name_to_key_map()
        
        # 方法1: 查找包含图片的表格行
        # 匹配 <tr><td><img...></td><td>物品名</td><td>价格</td></tr>
        pattern = r'<tr>\s*<td><img[^>]+></td>\s*<td>([^<]+)</td>\s*<td>(\d+)</td>\s*</tr>'
        matches = re.findall(pattern, html, re.DOTALL)
        
        for name, price in matches:
            name = name.strip()
            if name in name_to_key:
                result[name_to_key[name]] = int(price)
                logger.debug(f"NovaHD价格(带图片): {name} = {price}")
        
        # 方法2: 从菜市场区域提取
        if len(result) < 8:
            market_start = html.find('<h2>菜市场</h2>')
            if market_start != -1:
                market_html = html[market_start:market_start+5000]
                
                # 尝试两种模式
                # 模式A: 带图片的
                pattern_a = r'<td><img[^>]+></td>\s*<td>([^<]+)</td>\s*<td>(\d+)</td>'
                for name, price in re.findall(pattern_a, market_html, re.DOTALL):
                    name = name.strip()
                    if name in name_to_key and name not in result:
                        result[name_to_key[name]] = int(price)
                        logger.debug(f"NovaHD价格(菜市场A): {name} = {price}")
                
                # 模式B: 不带图片的（备选）
                pattern_b = r'<td>([^<]+)</td>\s*<td>(\d+)</td>'
                for name, price in re.findall(pattern_b, market_html, re.DOTALL):
                    name = name.strip()
                    if name in name_to_key and name not in result:
                        # 验证价格合理性
                        price_val = int(price)
                        if 100 <= price_val <= 50000:
                            result[name_to_key[name]] = price_val
                            logger.debug(f"NovaHD价格(菜市场B): {name} = {price_val}")
        
        # 方法3: 遍历所有物品名查找
        if len(result) < 8:
            for name, key in name_to_key.items():
                # 查找物品名所在位置，然后找后面的数字
                name_pos = html.find(f'<td>{name}</td>')
                if name_pos == -1:
                    name_pos = html.find(f'<td> {name}</td>')
                if name_pos == -1:
                    continue
                
                # 从物品名位置开始搜索价格（跳过图片列）
                search_start = name_pos + len(f'<td>{name}</td>')
                # 查找下一个td中的数字（可能带属性）
                match = re.search(r'<td[^>]*>(\d+)</td>', html[search_start:search_start+300])
                if match:
                    price = int(match.group(1))
                    if 100 <= price <= 50000:
                        result[key] = price
                        logger.debug(f"NovaHD价格(遍历): {name} = {price}")
        
        # 打印最终结果
        for key, price in result.items():
            crop_name = self.crops.get(key, {}).get("name", key)
            logger.debug(f"NovaHD最终价格: {crop_name} = {price}")
        
        logger.info(f"NovaHD解析到 {len(result)} 种物品价格")
        return result
    
    def parse_crop_status(self, html: str) -> Dict[str, Dict]:
        result = {}
        for key, crop in self.crops.items():
            result[key] = {"can_harvest": rf'action=harvest&type={crop["type"]}&id={crop["id"]}' in html}
        return result
    
    def get_sell_key(self, html: str, item_type: str, item_id: int) -> Optional[str]:
        return f"{item_type}_{item_id}"