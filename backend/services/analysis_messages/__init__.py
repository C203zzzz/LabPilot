"""Analysis messages module for gap and failure message construction."""
from backend.services.analysis_messages.gap_messages import (
    brand_unresolved_gap,
    no_owned_evidence_gap,
    parameters_absent_gap,
    parameter_fallback_gap,
    sibling_merge_gap,
)
from backend.services.analysis_messages.failure_messages import (
    brand_unresolved_failure,
    no_owned_evidence_failure,
    parameter_fallback_failure,
    parameter_extraction_incomplete_failure,
    sibling_merge_failure,
)
from backend.services.analysis_messages.evidence_helpers import (
    documents_text_excerpt,
    owned_evidence_excerpt,
    candidate_source_excerpt,
    redact_text,
    failure_location,
)

__all__ = [
    "brand_unresolved_gap",
    "no_owned_evidence_gap",
    "parameters_absent_gap",
    "parameter_fallback_gap",
    "sibling_merge_gap",
    "brand_unresolved_failure",
    "no_owned_evidence_failure",
    "parameter_fallback_failure",
    "parameter_extraction_incomplete_failure",
    "sibling_merge_failure",
    "documents_text_excerpt",
    "owned_evidence_excerpt",
    "candidate_source_excerpt",
    "redact_text",
    "failure_location",
]
