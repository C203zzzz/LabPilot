"""Quantity and unit validation utilities."""
from __future__ import annotations

import re
from typing import Any, Mapping


#: 尺寸三连：``1340×850×2120``（分隔符有 ``×``/``x``/``X``/``*``）。
_DIMENSION_TRIPLE_RE = re.compile(r"^\s*\d[\d.,]*\s*[×xX*]\s*\d[\d.,]*\s*[×xX*]\s*\d[\d.,]*\s*$")

#: 长度单位只收无歧义的多字符写法。单个拉丁字母（``m``）在 ``(EN12469)``、
#: ``(ISO 14664.1 Class3)`` 这种括号里太容易误命中，宁可漏收也不误收。
_LENGTH_UNIT_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:mm|cm|dm|km|μm|um|nm|英寸|毫米|厘米)(?![A-Za-z0-9])"
)

#: 尺寸行必须自己写了轴：只有 ``(mm)`` 而没有轴的三连数字与其他三连数字无法区分。
_DIMENSION_AXES = ("长", "宽", "高", "深")


def label_declares_size_unit(name: str, value: Any) -> bool:
    """子项标签自己声明了「这是个带长度单位的尺寸三连」吗。

    真实数据（``output/bio-instrument-acceptance-20260920``，知楚 A2 生物安全柜
    参数表）里，模型把单位写在键名上：``外形尺寸(长×宽×高mm)(支架高度720)``
    对应 ``1340×850×2120``、``内部尺寸(长×深×高mm)`` 对应 ``1270×580×660``。
    这两种键名里单位（``mm``）与三个轴都写着，值因此**不是**「脱离单位的裸
    数字」——单位原样留在键名上，这里不往值里补任何字符，只决定要不要因为它
    没有字段级 ``unit`` 就把它降级成未结构化。

    三条同时成立才放行：值是三连数字、标签写了至少两个轴、标签里有长度单位。
    表头没有单位的行（``气体交换体积 (风速额定值)`` + ``485``）继续按裸数字拒收。
    """
    text = str(name or "")
    if not _DIMENSION_TRIPLE_RE.match(str(value or "")):
        return False
    if sum(axis in text for axis in _DIMENSION_AXES) < 2:
        return False
    return bool(_LENGTH_UNIT_RE.search(text))


def missing_quantity_unit(name: str, value: Any, field: Mapping[str, Any]) -> bool:
    """Check if a quantitative value is missing its unit.

    Only applies to fields that typically contain measurements.
    Returns True if the value appears to be a bare number without unit.
    """
    if field.get("field_name") not in {
        "capacity_and_throughput",
        "performance_range",
        "accuracy_sensitivity_and_repeatability",
        "temperature_environment_and_safety",
        "physical_dimensions_power_and_installation",
    }:
        return False

    if isinstance(value, Mapping) or isinstance(value, bool):
        return False

    text = " ".join(map(str, value)) if isinstance(value, (list, tuple)) else str(value)
    bare = bool(re.fullmatch(r"[\s\d.,…±+−\-/×xX~:至到—–]+", text))
    if not bare:
        return False

    # Counts and dimensionless measurements are legitimate without a unit
    if re.search(
        r"数量|个数|数目|层数|孔数|相数|比率|(?<![A-Za-z])pH(?![A-Za-z])|\b(?:count|number|phases?|ratio)\b",
        name,
        re.I,
    ):
        return False

    unit = str(field.get("unit") or "").strip()
    # A comma-separated group of units cannot qualify each scalar separately
    if unit and unit.casefold() not in {"unknown", "null", "none"} and not re.search(r"[;,；，]", unit):
        return False

    # 字段自己没有 unit 时，**尺寸三连**的键名可以自带单位，见
    # ``label_declares_size_unit``。除此之外的裸数值照旧算缺单位。
    return not label_declares_size_unit(name, value)
