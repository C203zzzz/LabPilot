"""JSON serialization helpers for M08."""
from typing import Any
from backend.domain.recommendations import RankingResult

def ranking_to_dict(value: RankingResult) -> dict[str, Any]:
    return value.to_dict()
