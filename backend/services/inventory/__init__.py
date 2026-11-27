"""Inventory construction and candidate normalization for procurement research."""
from __future__ import annotations

from backend.services.inventory.batch_builder import (
    inventory_units,
    split_inventory_unit,
    inventory_batches,
)
from backend.services.inventory.candidate_normalizer import (
    candidate_identity,
    has_concrete_model,
    normalized_model,
    compound_model_variants,
    is_compound_of_known_models,
    configuration_with_catalog_number,
    probable_catalog_number,
    normalise_inventory_candidate,
)

__all__ = [
    "inventory_units",
    "split_inventory_unit",
    "inventory_batches",
    "candidate_identity",
    "has_concrete_model",
    "normalized_model",
    "compound_model_variants",
    "is_compound_of_known_models",
    "configuration_with_catalog_number",
    "probable_catalog_number",
    "normalise_inventory_candidate",
]
