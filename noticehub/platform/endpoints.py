"""外部学习平台的固定入口。

放在这里而不是 `config.py`：`config.py` 与 `bootstrap/settings.py` 互相引用，
常量放这两边任一侧都会成环。本模块不 import 任何项目内模块，谁都能引。
"""

from __future__ import annotations

# 学校 Canvas 实例的默认地址（同济）。别的学校在设置页或 .env 里改。
CANVAS_DEFAULT_BASE_URL = "https://canvas.tongji.edu.cn"
# 课堂派固定入口，不给改：改了只会把 Cookie 发到别处。
KETANGPAI_BASE_URL = "https://www.ketangpai.com"

__all__ = ["CANVAS_DEFAULT_BASE_URL", "KETANGPAI_BASE_URL"]
