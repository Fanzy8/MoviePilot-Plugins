#!/usr/bin/env python3
"""把 plugins.v3/personalcheckin 登记进 MoviePilot 本地插件仓库。

在 NAS 上执行：

    python install_local.py /config/local_plugin_repo

做的事：
1. 校验插件目录与入口文件存在；
2. 读取（或新建）package.v3.json，写入/更新 PersonalCheckin 条目；
3. 打印条目供核对。

不会动其它插件条目，也不重启 MoviePilot——装/启用仍走宿主接口。
"""
from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

ENTRY = collections.OrderedDict([
    ("name", "自用签到"),
    ("version", "1.0.1"),
    ("description", "NodeSeek / DeepFlood / linux.sb / yzyy 每日签到：08–09 点随机分钟执行，"
                    "单站失败重试两次，记录余额与签到历史，结束发一条汇总通知；"
                    "支持 CookieCloud 自动刷新与 Windows 一键推送 Cookie。"),
    ("author", "Fanzy8"),
    ("icon", "signin.png"),
    ("level", 1),
    ("release", True),
    ("system_version", ">=3.2.3"),
    ("history", collections.OrderedDict([
        ("v1.0.1", "表单回显已保存配置；通知发送后写日志。"),
        ("v1.0.0", "首个版本：四站签到（NodeSeek/DeepFlood/linux.sb/yzyy），Cloudflare 浏览器兜底，"
                   "随机 8–9 点调度，历史与余额记录，CookieCloud 与一键推送 Cookie，"
                   "导入旧 yzyy 插件历史。"),
    ])),
])


def main() -> int:
    """入口。"""
    if len(sys.argv) < 2:
        print("用法：python install_local.py <本地插件仓库根目录>")
        return 2
    root = Path(sys.argv[1]).resolve()
    plugin_dir = root / "plugins.v3" / "personalcheckin"
    if not (plugin_dir / "__init__.py").exists():
        print(f"插件目录不存在或缺少入口：{plugin_dir}")
        return 1
    pkg = root / "package.v3.json"
    data = collections.OrderedDict()
    if pkg.exists():
        data = json.loads(pkg.read_text(encoding="utf-8"),
                          object_pairs_hook=collections.OrderedDict)
    data["PersonalCheckin"] = ENTRY
    pkg.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("已登记 PersonalCheckin，当前 key：" + ", ".join(data.keys()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
