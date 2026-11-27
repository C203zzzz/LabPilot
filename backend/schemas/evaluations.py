from typing import Any
from backend.domain.evaluations import CandidateEvaluation

def evaluation_to_dict(value: CandidateEvaluation) -> dict[str, Any]:
    return value.to_dict()
