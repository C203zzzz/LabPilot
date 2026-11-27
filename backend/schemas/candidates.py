"""JSON-friendly M03 schemas without coupling the domain to a web framework."""
from dataclasses import asdict

from backend.domain.candidates import ProductCandidate


def candidate_to_dict(candidate: ProductCandidate) -> dict:
    return asdict(candidate)
