"""M05 task-material retrieval contracts (provider independent)."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, Sequence


class EvidenceSource(str, Enum):
    USER_DOCUMENT = "user_document"
    OFFICIAL_SOURCE = "official_source"
    DEALER = "dealer"
    UNKNOWN = "unknown"


class EvidenceStatus(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class DocumentChunk:
    chunk_id: str
    document_id: str
    text: str
    source_type: EvidenceSource = EvidenceSource.USER_DOCUMENT
    page: int | None = None
    location: str | None = None
    source_url: str | None = None
    authority_score: int | None = None
    evidence_status: EvidenceStatus = EvidenceStatus.AVAILABLE
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.document_id or not self.document_id.strip():
            raise ValueError("document_id is required")
        if not self.chunk_id or not self.chunk_id.strip():
            raise ValueError("chunk_id is required")


@dataclass(frozen=True)
class RetrievalQuery:
    text: str
    top_k: int = 5
    candidate_model: str | None = None
    parameter_terms: tuple[str, ...] = ()
    requirement_terms: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.top_k < 1:
            raise ValueError("top_k must be positive")


@dataclass(frozen=True)
class RetrievedEvidence:
    chunk: DocumentChunk
    score: float
    match_reasons: tuple[str, ...] = ()


@dataclass
class RAGContext:
    query: RetrievalQuery
    evidence: list[RetrievedEvidence] = field(default_factory=list)
    status: str = "ok"
    answerable: bool = True

    @property
    def context_text(self) -> str:
        return "\n\n".join(e.chunk.text for e in self.evidence if e.chunk.text)

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


class Retriever(Protocol):
    def retrieve(self, query: RetrievalQuery) -> list[RetrievedEvidence]: ...


__all__ = ["DocumentChunk", "RetrievalQuery", "RetrievedEvidence", "RAGContext", "Retriever", "EvidenceSource", "EvidenceStatus"]
