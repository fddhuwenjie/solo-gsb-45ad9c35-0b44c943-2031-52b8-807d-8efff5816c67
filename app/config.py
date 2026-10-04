"""全局配置。"""

from __future__ import annotations

import os

#: 十进制精度上限（有效数字位数）。输入值与输出结果均不得超过该精度，
#: 否则判定为“精度溢出”并返回 422。可通过环境变量覆盖。
MAX_PRECISION: int = int(os.environ.get("UNIT_API_MAX_PRECISION", "50"))

#: 单位幂指数允许的最大绝对值，超出视为“非法幂”。
MAX_POWER: int = int(os.environ.get("UNIT_API_MAX_POWER", "99"))

#: 已发布定义集（unit set）的持久化文件路径（JSON）。Compose 部署时指向
#: 命名卷，容器重建后已发布集合仍可使用。可通过环境变量覆盖。
UNIT_SETS_PATH: str = os.environ.get("UNIT_API_UNIT_SETS_PATH", "data/unit_sets.json")
