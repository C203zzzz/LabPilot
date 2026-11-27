"""Integration tests for evidence_validation module after refactoring."""
import pytest

from backend.services.evidence_validation import (
    canonical_number,
    numeric_tokens,
    excerpt_matches_evidence,
    validated_sources,
    source_evidence_index,
    validated_field,
    temperature_metric,
    metric_excerpt_supports,
    missing_quantity_unit,
    label_declares_size_unit,
)


def test_canonical_number_handles_thousands_separator():
    """Test that canonical_number correctly handles thousands separators."""
    assert canonical_number("1,234") == "1234"
    assert canonical_number("1,234,567") == "1234567"
    assert canonical_number("1.5") == "1.5"
    assert canonical_number("1,5") == "1.5"  # European decimal


def test_numeric_tokens_extracts_from_nested_structures():
    """Test numeric token extraction from complex structures."""
    value = {"temperature": "25.5°C", "humidity": "60%"}
    tokens = numeric_tokens(value)
    assert "25.5" in tokens
    assert "60" in tokens


def test_excerpt_matches_evidence_with_location_suffix():
    """Test that location suffixes are properly stripped before matching."""
    excerpt = "型号 X-100 page:1"
    evidence = "产品型号 X-100 是一款..."
    assert excerpt_matches_evidence(excerpt, evidence)


def test_source_evidence_index_builds_correct_mapping():
    """Test that source evidence index is built correctly."""
    documents = [
        {
            "document_id": "doc1",
            "pages": [
                {"location": "page:1", "text": "Test content page 1"},
                {"location": "page:2", "text": "Test content page 2"},
            ],
            "tables": [
                {"source_location": "table:0", "rows": [["A", "B"], ["C", "D"]]},
            ],
        }
    ]
    index = source_evidence_index(documents)
    assert ("doc1", "page:1") in index
    assert "Test content page 1" in index[("doc1", "page:1")]
    assert ("doc1", "table:0") in index
    assert "A\tB" in index[("doc1", "table:0")]


def test_validated_sources_requires_numeric_coverage():
    """Test that validated_sources checks numeric token coverage."""
    sources = [
        {"document_id": "doc1", "page_or_location": "page:1", "excerpt": "环境温度：25°C"}
    ]
    evidence_by_location = {("doc1", "page:1"): "技术参数\n环境温度：25°C\n相对湿度：≤70%"}

    # Should pass when required value is present and excerpt matches evidence
    result = validated_sources(sources, evidence_by_location, required_value="25°C")
    assert len(result) == 1

    # Should fail when required value is missing
    result = validated_sources(sources, evidence_by_location, required_value="30°C")
    assert len(result) == 0


def test_temperature_metric_identification():
    """Test temperature metric type identification."""
    assert temperature_metric("温度均匀性") == "spatial"
    assert temperature_metric("温度波动度") == "temporal"
    assert temperature_metric("temperature uniformity") == "spatial"
    assert temperature_metric("temperature stability") == "temporal"
    assert temperature_metric("其他参数") is None


def test_label_declares_size_unit():
    """Test dimension triple with unit detection."""
    assert label_declares_size_unit("外形尺寸(长×宽×高mm)", "1340×850×2120")
    assert label_declares_size_unit("内部尺寸(长×深×高mm)", "1270×580×660")
    assert not label_declares_size_unit("尺寸", "1340×850×2120")  # Missing axes
    assert not label_declares_size_unit("外形尺寸(长×宽×高)", "1340×850×2120")  # Missing unit


def test_missing_quantity_unit_detects_bare_numbers():
    """Test bare number detection in quantitative fields."""
    field = {
        "field_name": "performance_range",
        "value": "100-200",
        "unit": None,
    }
    assert missing_quantity_unit("量程", "100-200", field)

    # Should allow dimensionless quantities
    field_ph = {
        "field_name": "performance_range",
        "value": "6.5",
        "unit": None,
    }
    assert not missing_quantity_unit("pH范围", "6.5", field_ph)


def test_validated_field_keeps_supported_parameters():
    """Test that validated_field retains only supported values."""
    raw_field = {
        "field_name": "temperature_environment_and_safety",
        "value": {"环境温度": "15-30°C", "相对湿度": "≤70%"},
        "unit": None,
        "sources": [
            {"document_id": "doc1", "page_or_location": "page:1", "excerpt": "环境温度：15-30°C"}
        ],
    }
    evidence = {("doc1", "page:1"): "技术参数\n环境温度：15-30°C\n相对湿度：≤70%"}

    result = validated_field(raw_field, evidence)
    assert result["value"] != "unknown"
    assert "环境温度" in result["value"]


def test_metric_excerpt_supports_chamber_metrics():
    """Test temperature chamber metric validation."""
    sources = [
        {"excerpt": "温度均匀性：±0.5°C\n温度波动度：±0.3°C"}
    ]

    # Spatial metric should match spatial section
    assert metric_excerpt_supports("温度均匀性", "±0.5°C", sources)

    # Temporal metric should match temporal section
    assert metric_excerpt_supports("温度波动度", "±0.3°C", sources)

    # Non-temperature metrics pass through
    assert metric_excerpt_supports("其他参数", "100", sources)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
