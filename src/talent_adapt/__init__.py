"""地方产业人才适配服务端。

四个不变量对应实现：
- 能力映射：engine 按能力码汇合岗位要求、培养目标、课程证据与毕业去向；
- 授权裁剪：authz 按角色与范围控制登记/查看，谱系隐藏越权载荷；
- 来源谱系：lineage 给出 manifest → run → conclusion → 精确版本数据的链；
- 批次续跑：运行冻结 manifest，逐能力写检查点，resume 可分批续算，recompute 复现。
"""
from __future__ import annotations

from .canonical import canonical_json, sha256_hex
from .clock import Clock
from .errors import ConflictError, DomainError, NotFoundError
from .service import Service
from .storage import Storage

__all__ = [
    "Service",
    "Storage",
    "Clock",
    "DomainError",
    "ConflictError",
    "NotFoundError",
    "canonical_json",
    "sha256_hex",
]
