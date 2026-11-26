"""时间口径：看护日固定为 2026-12-02（UTC+08:00），文本时刻按分钟比较。

交换约定规定“时段右端不计入”，因此所有区间统一按半开区间 ``[start, end)``
处理。``qualification_to`` 所在时点本身不在资质有效期内。
"""
from __future__ import annotations

from typing import Optional, Union

CARE_DATE = "2026-12-02"
TimeValue = Union[str, int, None]


def parse_minutes(value: TimeValue) -> Optional[int]:
    """把 ``"HH:MM"``、ISO 日期时间或分钟数解析为当日分钟数。

    无法解析或为 ``None`` 时返回 ``None``。
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    # 去掉 ISO 日期部分
    if "T" in text:
        text = text.split("T", 1)[1]
    elif " " in text:
        tail = text.split(" ", 1)[1]
        if ":" in tail:
            text = tail
    # 去掉时区
    if text.endswith("Z"):
        text = text[:-1]
    if "+" in text:
        text = text.split("+", 1)[0]
    if "-" in text[2:]:  # 可能是 HH:MM-... 不会出现；保留安全处理
        pass
    parts = text.split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return None
    return hour * 60 + minute


def format_minutes(value: TimeValue) -> Optional[str]:
    m = parse_minutes(value) if not isinstance(value, int) else value
    if m is None:
        return None
    return f"{m // 60:02d}:{m % 60:02d}"


def overlaps(start_a: int, end_a: int, start_b: int, end_b: int) -> bool:
    """两个半开区间是否重叠（首尾相接不算重叠）。"""
    return start_a < end_b and start_b < end_a


def gap_minutes(end_a: int, start_b: int) -> int:
    return start_b - end_a
