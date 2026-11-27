"""Evidence selection utilities for candidate detail extraction.

This module provides evidence collection, page selection, scoring, and budget
allocation for selecting relevant pages and tables from documents.
"""
from __future__ import annotations

from backend.services.evidence_selection.candidate_scorer import (
    candidate_terms,
    matched_candidate_terms,
)
from backend.services.evidence_selection.field_scorer import (
    field_retrieval_profile,
)
from backend.services.evidence_selection.budget_allocator import (
    bounded_candidate_page_text,
    bounded_candidate_table,
    bounded_table_row,
)
from backend.services.evidence_selection.evidence_collector import (
    collect_evidence_units,
)
from backend.services.evidence_selection.page_selector import (
    select_pages,
)
from backend.services.evidence_selection.audit_builder import (
    build_audit_info,
)
from backend.services.evidence_selection.budget_assembler import (
    allocate_table_budget,
    allocate_page_budget,
    assemble_documents,
)


__all__ = [
    "candidate_terms",
    "matched_candidate_terms",
    "field_retrieval_profile",
    "bounded_candidate_page_text",
    "bounded_candidate_table",
    "bounded_table_row",
    "collect_evidence_units",
    "select_pages",
    "build_audit_info",
    "allocate_table_budget",
    "allocate_page_budget",
    "assemble_documents",
]
