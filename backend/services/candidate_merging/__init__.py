"""Candidate merging module for inventory deduplication and normalization.

This module handles:
- Five-layer identity matching (exact, same_model, compatible, same_instrument, new)
- Short series alias resolution (e.g., "ABC" → "ABC-100")
- Vision mention promotion into inventory seeds
- Filtering of non-instrument roles (accessories, consumables, software)
- Source deduplication and merging
"""
from __future__ import annotations

from backend.services.candidate_merging.identity_matcher import (
    candidate_identity,
    compatible_candidate_identity,
)
from backend.services.candidate_merging.series_alias_resolver import (
    merge_short_series_aliases,
)
from backend.services.candidate_merging.vision_mention_promoter import (
    structured_mention_inventory,
)
from backend.services.candidate_merging.inventory_merger import (
    merge_inventory_candidates,
    DeepSeekError,
)

__all__ = [
    "candidate_identity",
    "compatible_candidate_identity",
    "merge_short_series_aliases",
    "structured_mention_inventory",
    "merge_inventory_candidates",
    "DeepSeekError",
]
