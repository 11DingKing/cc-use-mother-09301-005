"""可替换时钟。

业务生效时间（valid_from）与“取数时点”（as_of）都由调用方显式给出，
计算过程绝不读取墙上时钟；这里的时钟只用于登记入库流水等元数据，
测试中可固定，保证演示与回归结果稳定。
"""
from __future__ import annotations


class Clock:
    def __init__(self, now: str = "2026-01-01T00:00:00Z") -> None:
        self._now = now

    def now(self) -> str:
        return self._now

    def set(self, value: str) -> None:
        self._now = value
