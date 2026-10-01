"""确定性编码与内容寻址哈希。

所有需要复现的结论都以规范 JSON（键排序、无空白）的 SHA-256 为准，
保证同一批输入在任何时间、任何机器上算出同一结果。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> bytes:
    """返回规范 JSON 字节串：键有序、无多余分隔符、UTF-8。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(value: Any) -> str:
    """计算任意可 JSON 化对象的内容哈希。"""
    return hashlib.sha256(canonical_json(value)).hexdigest()


def short_id(prefix: str, value: Any) -> str:
    """生成带前缀的短标识（运行、结论等）。"""
    return f"{prefix}-{sha256_hex(value)[:16]}"
