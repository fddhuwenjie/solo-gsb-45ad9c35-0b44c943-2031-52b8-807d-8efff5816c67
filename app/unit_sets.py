"""已发布的可复用自定义单位定义集（unit set）。

``POST /unit-sets`` 把 1～6 项与请求级 ``custom_units`` 相同格式的定义发布为
具名集合：先按现有规则校验并展开全部条目（
:class:`app.custom_units.UnitScope` 构造时即完成全量解析），任一项非法则
整次返回 422——不产生标识、不落盘，不会留下可引用的定义集。

- 发布成功后返回服务生成的标识；已发布内容不可改（不提供更新/删除操作，
  重复发布相同内容会得到新的标识）；
- 集合持久化到 JSON 文件（先写临时文件再 ``os.replace`` 原子替换），
  服务/容器重启后重新载入，已发布集合仍可按标识使用；
- 不同集合允许同名符号有不同含义；每次调用按标识现场构建独立的
  :class:`UnitScope`，并发调用互不影响，全局单位注册表保持只读。
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from .config import UNIT_SETS_PATH
from .custom_units import CustomUnitSpec, UnitScope
from .errors import unknown_unit_set

if TYPE_CHECKING:
    from .schemas import CustomUnitItem

#: 持久化文件格式版本
_FORMAT_VERSION = 1

#: 标识形式：``us_`` + 16 位随机十六进制
_ID_PREFIX = "us_"
_ID_HEX_LEN = 16


def _specs_of(items: Iterable[dict[str, str]]) -> tuple[CustomUnitSpec, ...]:
    """把持久化条目还原为自定义单位声明（factor 为十进制字符串）。"""

    return tuple(
        CustomUnitSpec(
            symbol=item["symbol"],
            factor=Fraction(item["factor"]),
            base_unit=item["base_unit"],
        )
        for item in items
    )


class UnitSetStore:
    """定义集存储：内存索引 + JSON 文件持久化，线程安全。

    内存中的条目即持久化格式（``factor`` 保持十进制字符串，与提交内容一致）；
    读取返回不可变元组，全部修改在锁内完成并原子落盘。
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._sets: dict[str, tuple[dict[str, str], ...]] = {}
        self._load()

    def __len__(self) -> int:
        with self._lock:
            return len(self._sets)

    # -- 发布与取用 --------------------------------------------------------

    def publish(
        self, items: Iterable[CustomUnitItem]
    ) -> tuple[str, list[dict[str, str]]]:
        """校验并发布一组定义，返回 ``(标识, 已持久化的条目列表)``。

        任一条目非法（重名、与内置单位/词头冲突、循环引用、未知引用、
        禁引温标、非法表达式等）都抛出 :class:`ConversionError`（HTTP 422），
        此时不产生标识也不落盘。
        """

        stored = [
            {
                "symbol": item.symbol,
                "factor": format(item.factor, "f"),
                "base_unit": item.base_unit,
            }
            for item in items
        ]
        # 先按现有规则校验并展开全部条目；通过后才生成标识、允许被引用
        UnitScope(_specs_of(stored))
        with self._lock:
            set_id = self._new_id_locked()
            self._sets[set_id] = tuple(stored)
            self._save_locked()
        return set_id, stored

    def get(self, set_id: str) -> tuple[dict[str, str], ...] | None:
        """按标识取已发布条目；不存在时返回 ``None``。"""

        with self._lock:
            return self._sets.get(set_id)

    def build_scope(self, set_id: str) -> UnitScope:
        """按标识为本次调用构建独立的解析上下文；标识不存在时抛出 422。"""

        items = self.get(set_id)
        if items is None:
            raise unknown_unit_set(set_id)
        return UnitScope(_specs_of(items))

    # -- 标识 --------------------------------------------------------------

    def _new_id_locked(self) -> str:
        while True:
            set_id = _ID_PREFIX + uuid.uuid4().hex[:_ID_HEX_LEN]
            if set_id not in self._sets:
                return set_id

    # -- 持久化 ------------------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            return
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("sets"), dict):
            raise RuntimeError(f"定义集持久化文件格式非法: {self._path}")
        for set_id, items in raw["sets"].items():
            # 载入时重新校验并展开：文件内容必须仍满足发布规则
            UnitScope(_specs_of(items))
            self._sets[set_id] = tuple(items)

    def _save_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": _FORMAT_VERSION,
            "sets": {
                set_id: list(items) for set_id, items in sorted(self._sets.items())
            },
        }
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self._path)


#: 进程级默认存储（路径由 ``UNIT_API_UNIT_SETS_PATH`` 配置，默认 ./data/unit_sets.json）
STORE = UnitSetStore(UNIT_SETS_PATH)
