"""Regression tests for hardening that is active on the production path."""

from backend.services.procurement_rules import (
    amount,
    contains_model,
    grounded_parameter,
    identity,
    supported,
)


def test_amount_rejects_boolean_values():
    assert amount(True) is None
    assert amount(False) is None


def test_text_gates_fail_closed_for_non_strings():
    assert not contains_model(123, "X100")
    assert not contains_model("X100", None)
    assert not supported("X100", None)
    assert not supported(None, "X100")


def test_contains_model_accepts_unicode_hyphen_variants_without_merging_suffixes():
    assert identity("X−100") == identity("X-100")
    assert contains_model("型号 X―100", "X-100")
    assert not contains_model("型号 X−100S", "X-100")


def test_grounded_parameter_has_recursion_and_type_guards():
    assert not grounded_parameter([], {}, "X100", "容量")
    assert not grounded_parameter({}, [], "X100", "容量")
    assert not grounded_parameter({}, {}, "", "容量")
    assert not grounded_parameter({}, {}, "X100", "")
    assert not grounded_parameter({}, {}, "X100", "容量", max_recursion=0)


def test_grounded_parameter_rejects_boolean_as_numeric_evidence():
    excerpt = "X100 容量 1 L"
    source = {
        "source_type": "manufacturer_website",
        "body_status": "fetched",
        "body_text": excerpt,
    }
    observed = {"value": True, "unit": "L", "excerpt": excerpt}
    assert not grounded_parameter(observed, source, "X100", "容量")
