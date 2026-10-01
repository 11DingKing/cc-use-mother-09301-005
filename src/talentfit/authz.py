"""授权裁剪。

主体（使用者或批次）只持有按资源类型与业务键授予的范围。汇合输入时
逐资源判定可见性：无授权的资源既不参与计算，也不出现在谱系中。
授权本身按 ``(subject, scope_kind, scope_key)`` 维护，缺失即不可见，
永不做"默认可读"。
"""
from __future__ import annotations

from typing import Any


def is_visible(scopes: set[tuple[str, str]], kind: str, key: str) -> bool:
    """该主体是否被授权访问某资源的某个业务键。"""
    return (kind, key) in scopes


def filter_visible(scopes: set[tuple[str, str]], resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """裁掉主体未被授权的资源，保持输入顺序。"""
    return [r for r in resources if is_visible(scopes, r["kind"], r["key"])]
