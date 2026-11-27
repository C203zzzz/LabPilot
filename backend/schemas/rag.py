"""JSON conversion helpers for M05 contracts."""
from dataclasses import asdict
from enum import Enum
from typing import Any

from backend.domain.rag import DocumentChunk, RAGContext, RetrievedEvidence, RetrievalQuery


def _json(value: Any) -> Any:
    if isinstance(value, Enum): return value.value
    if isinstance(value, list): return [_json(x) for x in value]
    if isinstance(value, tuple): return [_json(x) for x in value]
    if isinstance(value, dict): return {k: _json(v) for k, v in value.items()}
    return value


def to_dict(value: Any) -> dict:
    return _json(asdict(value))


__all__ = ["to_dict"]
