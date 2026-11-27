"""Parameter extraction module for candidate detail extraction.

This module handles:
- Deterministic parameter extraction from explicit parameter lines
- Parameter validation and status checking
- Field repair and gap detection
- Status recording and failure tracking
"""
from __future__ import annotations

from backend.services.parameter_extraction.deterministic_extractor import (
    deterministic_parameter_fields,
)
from backend.services.parameter_extraction.validation import (
    has_comparable_parameter,
    has_usable_parameter,
)
from backend.services.parameter_extraction.field_repairer import (
    repairable_field_names,
    merge_repaired_fields,
    CANONICAL_FIELD_NAMES,
)
from backend.services.parameter_extraction.status_recorder import (
    PARAMETERS_STATUS_EXTRACTED,
    PARAMETERS_STATUS_FALLBACK_EMPTY,
    PARAMETERS_STATUS_NOT_IN_SOURCE,
    PARAMETERS_STATUS_INCOMPLETE,
    parameter_fallback_gap,
    parameter_fallback_failure,
    parameter_extraction_incomplete_failure,
    parameters_absent_gap,
)

__all__ = [
    "deterministic_parameter_fields",
    "has_comparable_parameter",
    "has_usable_parameter",
    "repairable_field_names",
    "merge_repaired_fields",
    "CANONICAL_FIELD_NAMES",
    "PARAMETERS_STATUS_EXTRACTED",
    "PARAMETERS_STATUS_FALLBACK_EMPTY",
    "PARAMETERS_STATUS_NOT_IN_SOURCE",
    "PARAMETERS_STATUS_INCOMPLETE",
    "parameter_fallback_gap",
    "parameter_fallback_failure",
    "parameter_extraction_incomplete_failure",
    "parameters_absent_gap",
]
