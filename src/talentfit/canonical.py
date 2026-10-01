"""规范化序列化与内容哈希。

所有需要跨时间复现的结论都以规范化 JSON 的 SHA-256 作为身份，
键顺序、空白、中文编码均不影响哈希。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_dumps(value: Any) -> str:
    """生成字节稳定的 JSON 字符串。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(value: Any) -> str:
    """计算规范化内容的 SHA-256 十六进制摘要。"""
    return hashlib.sha256(canonical_dumps(value).encode("utf-8")).hexdigest()
