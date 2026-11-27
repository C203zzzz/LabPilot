"""Integration tests for candidate_merging module after refactoring."""
import pytest

from backend.services.candidate_merging import (
    candidate_identity,
    compatible_candidate_identity,
    merge_short_series_aliases,
    structured_mention_inventory,
    merge_inventory_candidates,
)


def test_candidate_identity_extraction():
    """Test that candidate_identity extracts the four-part tuple correctly.

    身份串按拆分前的语义做归一（NFKC + casefold + 去非词字符），大小写与连字符
    不参与判同——``VIOS 160i`` 与 ``vios160i`` 是同一个身份。
    """
    candidate = {
        "brand": "Thermo Fisher",
        "series": "Heracell",
        "model": "VIOS 160i",
        "configuration": "CO2",
    }
    identity = candidate_identity(candidate)
    assert identity == ("thermofisher", "heracell", "vios160i", "co2")


def test_candidate_identity_handles_missing_fields():
    """Test that missing fields are treated as empty strings."""
    candidate = {"model": "ABC-100"}
    identity = candidate_identity(candidate)
    assert identity == ("", "", "abc100", "")


def test_compatible_candidate_identity_same_model():
    """Test that compatible_candidate_identity matches same model."""
    left = ("Thermo", "Heracell", "VIOS 160i", "")
    right = ("Thermo", "Heracell", "VIOS 160i", "CO2")
    assert compatible_candidate_identity(left, right)


def test_compatible_candidate_identity_rejects_different_brand():
    """Test that different brands are not compatible."""
    left = ("Brand A", "", "Model X", "")
    right = ("Brand B", "", "Model X", "")
    assert not compatible_candidate_identity(left, right)


def _merge_probe(candidates_by_doc):
    """把 {文档ID: 候选} 组装成 merge_inventory_candidates 的输入。"""
    results = []
    evidence = {}
    for document_id, candidate in candidates_by_doc.items():
        candidate = {**candidate, "product_role": "instrument"}
        candidate.setdefault("sources", [{
            "document_id": document_id,
            "page_or_location": "page:1",
            "excerpt": str(candidate.get("model") or ""),
        }])
        results.append({"candidates": [candidate]})
        evidence[(document_id, "page:1")] = str(candidate.get("model") or "")
    return results, evidence


def test_merge_inventory_candidates_keeps_different_brands_apart():
    """同型号但品牌不同：不合并（五层匹配要求品牌相容）。"""
    results, evidence = _merge_probe({
        "doc1": {"brand": "Brand A", "model": "Model X"},
        "doc2": {"brand": "Brand B", "model": "Model X"},
    })
    merged, _ = merge_inventory_candidates(results, evidence)
    assert len(merged) == 2


def test_merge_inventory_candidates_fills_missing_fields():
    """一条缺 configuration、另一条有：合并成一条并补齐（兼容匹配层）。"""
    results, evidence = _merge_probe({
        "doc1": {"brand": "Thermo", "series": "Heracell", "model": "VIOS 160i"},
        "doc2": {"brand": "Thermo", "series": "Heracell", "model": "VIOS 160i",
                 "configuration": "CO2"},
    })
    merged, _ = merge_inventory_candidates(results, evidence)
    assert len(merged) == 1
    assert merged[0]["configuration"] == "CO2"


def test_merge_inventory_candidates_keeps_unrelated_models_apart():
    """型号不同的两条候选各自保留（不匹配层）。"""
    results, evidence = _merge_probe({
        "doc1": {"brand": "Brand A", "model": "Model A"},
        "doc2": {"brand": "Brand B", "model": "Model B"},
    })
    merged, _ = merge_inventory_candidates(results, evidence)
    assert len(merged) == 2


def test_merge_short_series_aliases_resolves_short_label():
    """Test that 3-4 char uppercase labels are resolved to full names."""
    candidates = [
        {
            "model": "ABC",
            "series": "",
            "brand": "",
            "configuration": None,
            "sources": [{
                "document_id": "doc1",
                "page_or_location": "page:1",
                "excerpt": "ABC series specifications",
            }],
        },
        {
            "model": "ABC-100",
            "series": "ABC Series",
            "brand": "Vendor",
            "configuration": None,
            "sources": [{
                "document_id": "doc1",
                "page_or_location": "page:2",
                "excerpt": "ABC-100 technical details",
            }],
        },
    ]

    merged = merge_short_series_aliases(candidates)

    # Short alias should be merged into full model
    assert len(merged) == 1
    assert merged[0]["model"] == "ABC-100"
    assert merged[0]["series"] == "ABC Series"
    assert len(merged[0]["sources"]) == 2


def test_merge_short_series_aliases_preserves_non_short():
    """Test that non-short model names are not affected."""
    candidates = [
        {"model": "Full-Model-Name-123", "sources": []},
        {"model": "Another-Long-Model", "sources": []},
    ]

    merged = merge_short_series_aliases(candidates)
    assert len(merged) == 2


def test_structured_mention_inventory_promotes_vision_mentions():
    """Test that vision mentions are converted to inventory seeds."""
    prompt_docs = [
        {
            "document_id": "doc1",
            "pages": [
                {
                    "location": "page:1",
                    "visual_fields": {
                        "candidate_mentions": [
                            {
                                "product_role": "instrument",
                                "brand": "Thermo",
                                "series": "Heracell",
                                "model": "VIOS 160i",
                                "configuration": None,
                                "evidence_quote": "Thermo Heracell VIOS 160i CO2 incubator",
                            }
                        ]
                    },
                }
            ],
        }
    ]

    result = structured_mention_inventory(prompt_docs)

    assert "candidates" in result
    candidates = result["candidates"]
    assert len(candidates) == 1
    assert candidates[0]["model"] == "VIOS 160i"
    assert candidates[0]["brand"] == "Thermo"
    assert candidates[0]["sources"][0]["document_id"] == "doc1"


def test_structured_mention_inventory_rejects_non_concrete():
    """Test that mentions without concrete identifiers are rejected."""
    prompt_docs = [
        {
            "document_id": "doc1",
            "pages": [
                {
                    "location": "page:1",
                    "visual_fields": {
                        "candidate_mentions": [
                            {
                                "model": None,
                                "series": "unknown",
                                "evidence_quote": "Some text without model",
                            }
                        ]
                    },
                }
            ],
        }
    ]

    result = structured_mention_inventory(prompt_docs)
    assert len(result["candidates"]) == 0


def test_merge_inventory_candidates_deduplicates():
    """Test that identical candidates from different sources are merged."""
    results = [
        {
            "candidates": [
                {
                    "brand": "Thermo",
                    "series": "Heracell",
                    "model": "VIOS 160i",
                    "configuration": None,
                    "product_role": "instrument",
                    "sources": [{
                        "document_id": "doc1",
                        "page_or_location": "page:1",
                        "excerpt": "VIOS 160i specifications",
                    }],
                }
            ]
        },
        {
            "candidates": [
                {
                    "brand": "Thermo",
                    "series": "Heracell",
                    "model": "VIOS 160i",
                    "configuration": "CO2",
                    "product_role": "instrument",
                    "sources": [{
                        "document_id": "doc2",
                        "page_or_location": "page:1",
                        "excerpt": "VIOS 160i CO2 version",
                    }],
                }
            ]
        },
    ]

    evidence_by_location = {
        ("doc1", "page:1"): "Technical specifications\nVIOS 160i specifications\nChamber volume: 165L",
        ("doc2", "page:1"): "Product line\nVIOS 160i CO2 version\nCO2 control system",
    }

    merged, gap = merge_inventory_candidates(results, evidence_by_location)

    # Should merge into one candidate with combined sources and configuration
    assert len(merged) == 1
    assert merged[0]["model"] == "VIOS 160i"
    assert merged[0]["configuration"] == "CO2"
    assert len(merged[0]["sources"]) == 2


def test_merge_inventory_candidates_filters_accessories():
    """Test that accessories are filtered out."""
    results = [
        {
            "candidates": [
                {
                    "model": "Rotor-123",
                    "product_role": "accessory",
                    "sources": [{
                        "document_id": "doc1",
                        "page_or_location": "page:1",
                        "excerpt": "Rotor accessory",
                    }],
                }
            ]
        }
    ]

    evidence_by_location = {
        ("doc1", "page:1"): "Rotor accessory for centrifuge",
    }

    merged, gap = merge_inventory_candidates(results, evidence_by_location)
    assert len(merged) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
