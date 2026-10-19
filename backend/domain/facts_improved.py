"""第一性原理优化：facts领域模型

修复P0.1：不可变性
修复P0.2：输入校验
修复P0.3：状态转换保护
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class FactStatus(StrEnum):
    EXTRACTED = "extracted"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    CONFLICTED = "conflicted"


class FactValueType(StrEnum):
    TEXT = "text"
    NUMBER = "number"
    BOOLEAN = "boolean"
    RANGE = "range"
    STRUCTURED = "structured"


class FactEvidenceLevel(StrEnum):
    PRIMARY = "primary"  # 制造商官方
    SECONDARY = "secondary"  # 经销商等
    USER_PROVIDED = "user_provided"  # 用户上传
    HISTORICAL = "historical"  # 历史采购


@dataclass(frozen=True)
class FactEvidence:
    """不可变的事实证据对象"""
    evidence_id: str
    candidate_id: str
    source_type: str
    authority_score: int
    evidence_level: FactEvidenceLevel
    extracted_at: datetime
    document_id: str | None = None
    page: int | None = None
    location: str | None = None
    url: str | None = None
    body_location: str | None = None
    quote: str | None = None
    price_evidence_level: str | None = None
    tender_title: str | None = None
    publication_date: str | None = None
    currency: str | None = None
    tax_condition: str | None = None
    price_amount: float | None = None
    price_unit: str | None = None
    price_quote: str | None = None

    def __post_init__(self):
        """输入校验"""
        if not self.evidence_id.strip():
            raise ValueError("evidence_id cannot be empty")
        if not self.candidate_id.strip():
            raise ValueError("candidate_id cannot be empty")
        if not isinstance(self.authority_score, int) or not 0 <= self.authority_score <= 100:
            raise ValueError(f"authority_score must be 0-100, got {self.authority_score}")
        if not isinstance(self.evidence_level, FactEvidenceLevel):
            raise ValueError(f"evidence_level must be FactEvidenceLevel, got {type(self.evidence_level)}")
        if self.price_amount is not None:
            if not isinstance(self.price_amount, (int, float)) or self.price_amount < 0:
                raise ValueError(f"price_amount must be non-negative, got {self.price_amount}")


@dataclass(frozen=True)
class ProductFact:
    """不可变的产品事实对象

    状态机：
    EXTRACTED -> CONFIRMED (confirm)
    EXTRACTED -> REJECTED (reject)
    EXTRACTED -> CONFLICTED (mark_conflicted)
    CONFLICTED -> CONFIRMED (resolve_conflict)

    不允许：
    REJECTED -> CONFIRMED
    CONFIRMED -> REJECTED
    """
    fact_id: str
    candidate_id: str
    field_name: str
    value: str | int | float | bool | list | dict
    value_type: FactValueType
    unit: str | None
    evidence: tuple[FactEvidence, ...]  # 不可变tuple
    status: FactStatus
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    note: str | None = None
    conflict_ids: tuple[str, ...] = ()  # 不可变tuple

    def __post_init__(self):
        """输入校验"""
        if not self.fact_id.strip():
            raise ValueError("fact_id cannot be empty")
        if not self.candidate_id.strip():
            raise ValueError("candidate_id cannot be empty")
        if not self.field_name.strip():
            raise ValueError("field_name cannot be empty")
        if not self.evidence:
            raise ValueError("fact must have at least one evidence")
        if not isinstance(self.status, FactStatus):
            raise ValueError(f"status must be FactStatus, got {type(self.status)}")
        if not isinstance(self.value_type, FactValueType):
            raise ValueError(f"value_type must be FactValueType, got {type(self.value_type)}")

        # 转换为不可变类型
        if isinstance(self.evidence, list):
            object.__setattr__(self, 'evidence', tuple(self.evidence))
        if isinstance(self.conflict_ids, list):
            object.__setattr__(self, 'conflict_ids', tuple(self.conflict_ids))

        # 校验evidence中的candidate_id一致性
        for e in self.evidence:
            if e.candidate_id != self.candidate_id:
                raise ValueError(f"evidence candidate_id mismatch: {e.candidate_id} != {self.candidate_id}")

    def confirm(self, reviewer_id: str, note: str | None = None) -> ProductFact:
        """状态转换：-> CONFIRMED

        只能从EXTRACTED转换，REJECTED/CONFLICTED不能直接确认
        """
        if not reviewer_id.strip():
            raise ValueError("reviewer_id cannot be empty")

        if self.status == FactStatus.REJECTED:
            raise ValueError("cannot confirm rejected fact, must create new fact")
        if self.status == FactStatus.CONFLICTED:
            raise ValueError("cannot confirm conflicted fact, must resolve conflict first")
        if self.status == FactStatus.CONFIRMED:
            raise ValueError("fact is already confirmed")

        return replace(
            self,
            status=FactStatus.CONFIRMED,
            reviewed_by=reviewer_id,
            reviewed_at=datetime.now(timezone.utc),
            note=note if note is not None else self.note
        )

    def reject(self, reviewer_id: str, note: str | None = None) -> ProductFact:
        """状态转换：-> REJECTED

        只能从EXTRACTED转换
        """
        if not reviewer_id.strip():
            raise ValueError("reviewer_id cannot be empty")

        if self.status == FactStatus.CONFIRMED:
            raise ValueError("cannot reject confirmed fact, must create new revision")
        if self.status == FactStatus.CONFLICTED:
            raise ValueError("cannot reject conflicted fact, must resolve conflict first")
        if self.status == FactStatus.REJECTED:
            raise ValueError("fact is already rejected")

        return replace(
            self,
            status=FactStatus.REJECTED,
            reviewed_by=reviewer_id,
            reviewed_at=datetime.now(timezone.utc),
            note=note if note is not None else self.note
        )

    def mark_conflicted(self, conflict_ids: tuple[str, ...] | list[str]) -> ProductFact:
        """标记为冲突状态"""
        if not conflict_ids:
            raise ValueError("conflict_ids cannot be empty")

        if isinstance(conflict_ids, list):
            conflict_ids = tuple(conflict_ids)

        if self.fact_id in conflict_ids:
            raise ValueError("conflict_ids cannot contain self")

        return replace(
            self,
            status=FactStatus.CONFLICTED,
            conflict_ids=conflict_ids
        )

    def resolve_conflict(self, reviewer_id: str, note: str | None = None) -> ProductFact:
        """解决冲突：CONFLICTED -> CONFIRMED"""
        if not reviewer_id.strip():
            raise ValueError("reviewer_id cannot be empty")

        if self.status != FactStatus.CONFLICTED:
            raise ValueError("fact is not conflicted")

        return replace(
            self,
            status=FactStatus.CONFIRMED,
            reviewed_by=reviewer_id,
            reviewed_at=datetime.now(timezone.utc),
            note=note if note is not None else self.note,
            conflict_ids=()  # 清空冲突ID
        )


def reconcile_facts(facts: list[ProductFact]) -> list[ProductFact]:
    """识别并标记冲突的事实

    同一候选的同一字段，如果有不同的值，标记为冲突
    """
    if not isinstance(facts, list):
        raise ValueError("facts must be a list")

    groups: dict[tuple[str, str], list[ProductFact]] = {}
    for fact in facts:
        if not isinstance(fact, ProductFact):
            raise ValueError(f"expected ProductFact, got {type(fact)}")
        key = (fact.candidate_id, fact.field_name)
        groups.setdefault(key, []).append(fact)

    result = []
    for members in groups.values():
        # 检查是否有不同的值
        distinct_values = {
            repr((m.value, m.unit, m.value_type.value))
            for m in members
        }

        if len(distinct_values) > 1:
            # 有冲突，标记所有成员
            fact_ids = tuple(m.fact_id for m in members)
            for fact in members:
                conflict_ids = tuple(fid for fid in fact_ids if fid != fact.fact_id)
                if fact.status != FactStatus.CONFLICTED or fact.conflict_ids != conflict_ids:
                    result.append(fact.mark_conflicted(conflict_ids))
                else:
                    result.append(fact)
        else:
            # 无冲突
            result.extend(members)

    return result


__all__ = [
    "FactStatus",
    "FactValueType",
    "FactEvidenceLevel",
    "FactEvidence",
    "ProductFact",
    "reconcile_facts",
]
