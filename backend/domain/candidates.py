"""Domain objects for M03 candidate identification and confirmation.

The model deliberately keeps product identity separate from configuration and
from the source in which it was observed.  An extracted candidate is never a
confirmed product until a user explicitly changes its status.
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


@dataclass
class ProductCandidate:
    brand: str | None
    series: str | None
    model: str
    configuration: str | None = None
    sources: list[CandidateSource] = field(default_factory=list)
    status: CandidateStatus = CandidateStatus.PENDING
    id: str = field(default_factory=lambda: str(uuid4()))
    note: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def identity_key(self) -> tuple[str | None, str | None, str, str | None]:
        """Return an exact identity key; configurations must not be merged."""
        return tuple(_normalise(x) if x else None for x in (self.brand, self.series, self.model, self.configuration))

    def add_source(self, source: CandidateSource) -> None:
        if source not in self.sources:
            self.sources.append(source)


def _normalise(value: str) -> str:
    return " ".join(value.casefold().split())


def merge_candidates(candidates: Iterable[ProductCandidate]) -> list[ProductCandidate]:
    """Merge duplicate observations while retaining every source and status.

    Status precedence is conservative: excluded and confirmed are never
    silently downgraded, and conflicting decisions return to pending.
    """
    merged: dict[tuple, ProductCandidate] = {}
    for candidate in candidates:
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
        if existing.brand is None and candidate.brand is not None:
            existing.brand = candidate.brand
        if existing.series is None and candidate.series is not None:
            existing.series = candidate.series
        # The representative key may change after enriching an incomplete
        # observation with its brand/series; future observations must still
        # find this representative.
        if key != existing.identity_key():
            merged.pop(key, None)
            merged[existing.identity_key()] = existing
        for source in candidate.sources:
            existing.add_source(source)
        statuses = {existing.status, candidate.status}
        if len(statuses) > 1 and CandidateStatus.PENDING not in statuses:
            existing.status = CandidateStatus.PENDING
            existing.note = "来源记录中的用户决定冲突，需要重新确认"
        elif CandidateStatus.CONFIRMED in statuses:
            existing.status = CandidateStatus.CONFIRMED
        elif CandidateStatus.EXCLUDED in statuses:
            existing.status = CandidateStatus.EXCLUDED
    return list(merged.values())


def _compatible_observation(left: ProductCandidate, right: ProductCandidate) -> bool:
    if _normalise(left.model) != _normalise(right.model):
        return False
    if _normalise(left.configuration or "") != _normalise(right.configuration or ""):
        return False
    for a, b in ((left.brand, right.brand), (left.series, right.series)):
        if a and b and _normalise(a) != _normalise(b):
            return False
    return True
