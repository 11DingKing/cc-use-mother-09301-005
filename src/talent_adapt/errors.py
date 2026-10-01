"""服务层异常与 HTTP 状态码的对应。"""
from __future__ import annotations


class DomainError(Exception):
    """领域规则错误，映射为 400。"""


class NotFoundError(KeyError):
    """资源不存在，映射为 404。"""


class ConflictError(Exception):
    """状态或版本冲突，映射为 409。"""
