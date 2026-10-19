"""第一性原理优化：domain层不可变数据结构

修复P0.1：所有领域对象改为frozen，容器改为不可变类型
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Iterable
from uuid import uuid4


class CandidateStatus(StrEnum):
    PENDING = "pending_confirmation"
    CONFIRMED = "confirmed"
    EXCLUDED = "excluded"


@dataclass(frozen=True)
class CandidateSource:
    document_id: str
    location: str
    excerpt: str
    source_type: str = "uploaded_document"


@dataclass(frozen=True)
class ProductCandidate:
    """不可变的产品候选对象

    所有修改操作返回新对象而非修改自身
    """
    brand: str | None
    series: str | None
    model: str
    configuration: str | None = None
    sources: tuple[CandidateSource, ...] = ()  # 改为tuple不可变
    status: CandidateStatus = CandidateStatus.PENDING
    id: str = field(default_factory=lambda: str(uuid4()))
    note: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __post_init__(self):
        """输入校验"""
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("model cannot be empty")
        if self.brand is not None and not isinstance(self.brand, str):
            raise ValueError("brand must be string or None")
        if self.series is not None and not isinstance(self.series, str):
            raise ValueError("series must be string or None")
        # 转换为真正的不可变元组
        if isinstance(self.sources, list):
            object.__setattr__(self, 'sources', tuple(self.sources))

    def identity_key(self) -> tuple[str | None, str | None, str, str | None]:
        """Return an exact identity key; configurations must not be merged."""
        return tuple(_normalise(x) if x else None for x in (self.brand, self.series, self.model, self.configuration))

    def with_source(self, source: CandidateSource) -> ProductCandidate:
        """返回新对象，添加source"""
        if source in self.sources:
            return self
        from dataclasses import replace
        return replace(self, sources=self.sources + (source,))

    def with_status(self, status: CandidateStatus, note: str | None = None) -> ProductCandidate:
        """安全的状态转换"""
        from dataclasses import replace
        return replace(self, status=status, note=note if note is not None else self.note)


def _normalise(value: str) -> str:
    """标准化字符串用于比较"""
    if not isinstance(value, str):
        raise ValueError(f"value must be string, got {type(value)}")
    return " ".join(value.casefold().split())


def merge_candidates(candidates: Iterable[ProductCandidate]) -> list[ProductCandidate]:
    """Merge duplicate observations while retaining every source and status.

    Status precedence is conservative: excluded and confirmed are never
    silently downgraded, and conflicting decisions return to pending.

    完全使用不可变操作，不修改任何输入对象
    """
    merged: dict[tuple, ProductCandidate] = {}

    for candidate in candidates:
        if not isinstance(candidate, ProductCandidate):
            raise ValueError(f"expected ProductCandidate, got {type(candidate)}")

        key = candidate.identity_key()
        existing = merged.get(key)

        if existing is None:
            # 查找兼容的观察
            for probe in merged.values():
                if _compatible_observation(probe, candidate):
                    existing = probe
                    break

        if existing is None:
            # 新候选，直接添加
            merged[key] = candidate
            continue

        # 合并sources
        new_sources = existing.sources
        for source in candidate.sources:
            if source not in new_sources:
                new_sources = new_sources + (source,)

        # 合并brand/series（填充空值）
        new_brand = existing.brand if existing.brand else candidate.brand
        new_series = existing.series if existing.series else candidate.series

        # 状态合并逻辑
        statuses = {existing.status, candidate.status}
        if len(statuses) > 1 and CandidateStatus.PENDING not in statuses:
            new_status = CandidateStatus.PENDING
            new_note = "来源记录中的用户决定冲突，需要重新确认"
        elif CandidateStatus.CONFIRMED in statuses:
            new_status = CandidateStatus.CONFIRMED
            new_note = existing.note
        elif CandidateStatus.EXCLUDED in statuses:
            new_status = CandidateStatus.EXCLUDED
            new_note = existing.note
        else:
            new_status = existing.status
            new_note = existing.note

        # 创建新对象
        from dataclasses import replace
        merged_candidate = replace(
            existing,
            brand=new_brand,
            series=new_series,
            sources=new_sources,
            status=new_status,
            note=new_note
        )

        # 更新映射
        old_key = existing.identity_key()
        new_key = merged_candidate.identity_key()

        if old_key in merged:
            del merged[old_key]
        merged[new_key] = merged_candidate

    return list(merged.values())


def _compatible_observation(left: ProductCandidate, right: ProductCandidate) -> bool:
    """检查两个观察是否兼容（可合并）"""
    try:
        if _normalise(left.model) != _normalise(right.model):
            return False
    except ValueError:
        return False

    # 配置必须完全一致
    left_config = _normalise(left.configuration) if left.configuration else ""
    right_config = _normalise(right.configuration) if right.configuration else ""
    if left_config != right_config:
        return False

    # brand/series只要有一个匹配就兼容
    for a, b in ((left.brand, right.brand), (left.series, right.series)):
        if a and b:
            try:
                if _normalise(a) != _normalise(b):
                    return False
            except ValueError:
                return False

    return True


__all__ = [
    "CandidateStatus",
    "CandidateSource",
    "ProductCandidate",
    "merge_candidates",
]
