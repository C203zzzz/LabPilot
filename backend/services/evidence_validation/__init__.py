"""Evidence validation module for verifying citations against source documents."""
from backend.services.evidence_validation.text_normalization import (
    search_text,
    normalized_model,
    value_identity,
)
from backend.services.evidence_validation.numeric_matching import (
    canonical_number,
    numeric_tokens,
)
from backend.services.evidence_validation.excerpt_validator import (
    excerpt_matches_evidence,
    validated_sources,
    source_evidence_index,
)
from backend.services.evidence_validation.quantity_unit_checker import (
    missing_quantity_unit,
    label_declares_size_unit,
)
from backend.services.evidence_validation.domain_validators import (
    temperature_metric,
    metric_excerpt_supports,
)
from backend.services.evidence_validation.field_validator import validated_field

__all__ = [
    "search_text",
    "normalized_model",
    "value_identity",
    "canonical_number",
    "numeric_tokens",
    "excerpt_matches_evidence",
    "validated_sources",
    "source_evidence_index",
    "missing_quantity_unit",
    "label_declares_size_unit",
    "temperature_metric",
    "metric_excerpt_supports",
    "validated_field",
]
