"""Domain objects for M03 candidate identification and confirmation.

The model deliberately keeps product identity separate from configuration and
from the source in which it was observed.  An extracted candidate is never a
confirmed product until a user explicitly changes its status.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
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
    """Immutable product candidate; mutations return a new instance."""
    brand: str | None
    series: str | None
    model: str
    configuration: str | None = None
    sources: tuple[CandidateSource, ...] = ()
    status: CandidateStatus = CandidateStatus.PENDING
    id: str = field(default_factory=lambda: str(uuid4()))
    note: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("model cannot be empty")
        if self.brand is not None and not isinstance(self.brand, str):
            raise ValueError("brand must be string or None")
        if self.series is not None and not isinstance(self.series, str):
            raise ValueError("series must be string or None")
        if not isinstance(self.status, CandidateStatus):
            object.__setattr__(self, "status", CandidateStatus(self.status))
        if isinstance(self.sources, list):
            object.__setattr__(self, "sources", tuple(self.sources))

    def identity_key(self) -> tuple[str | None, str | None, str, str | None]:
        """Return an exact identity key; configurations must not be merged."""
        return tuple(_normalise(x) if x else None for x in (self.brand, self.series, self.model, self.configuration))

    def with_source(self, source: CandidateSource) -> ProductCandidate:
        if source in self.sources:
            return self
        return replace(self, sources=self.sources + (source,))

    def with_status(self, status: CandidateStatus | str, note: str | None = None) -> ProductCandidate:
        resolved = status if isinstance(status, CandidateStatus) else CandidateStatus(status)
        return replace(self, status=resolved, note=note if note is not None else self.note)


def _normalise(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"value must be string, got {type(value)}")
    return " ".join(value.casefold().split())


def merge_candidates(candidates: Iterable[ProductCandidate]) -> list[ProductCandidate]:
    """Merge duplicate observations while retaining every source and status.

    Status precedence is conservative: excluded and confirmed are never
    silently downgraded, and conflicting decisions return to pending.
    """
    merged: dict[tuple, ProductCandidate] = {}

    for candidate in candidates:
        if not isinstance(candidate, ProductCandidate):
            raise ValueError(f"expected ProductCandidate, got {type(candidate)}")

        key = candidate.identity_key()
        existing = merged.get(key)
        if existing is None:
            for probe in merged.values():
                if _compatible_observation(probe, candidate):
                    existing = probe
                    break
        if existing is None:
            merged[key] = candidate
            continue

        new_sources = existing.sources
        for source in candidate.sources:
            if source not in new_sources:
                new_sources = new_sources + (source,)

        new_brand = existing.brand if existing.brand else candidate.brand
        new_series = existing.series if existing.series else candidate.series

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

        merged_candidate = replace(
            existing,
            brand=new_brand,
            series=new_series,
            sources=new_sources,
            status=new_status,
            note=new_note,
        )

        old_key = existing.identity_key()
        new_key = merged_candidate.identity_key()
        merged.pop(old_key, None)
        merged[new_key] = merged_candidate

    return list(merged.values())


def _compatible_observation(left: ProductCandidate, right: ProductCandidate) -> bool:
    try:
        if _normalise(left.model) != _normalise(right.model):
            return False
    except ValueError:
        return False

    left_config = _normalise(left.configuration) if left.configuration else ""
    right_config = _normalise(right.configuration) if right.configuration else ""
    if left_config != right_config:
        return False

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
