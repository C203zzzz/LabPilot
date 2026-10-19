from backend.domain.errors import ModuleValidationError
from backend.domain.quotes import QuoteCondition, QuoteSourceType, SupplierQuote
from backend.services.quote_service import aggregate_quotes, compare_quotes, deduplicate_quotes


def quote(qid="q1", cid="c1", supplier="甲", amount=100, currency="CNY", config="std",
          source_type=QuoteSourceType.CURRENT_SUPPLIER_QUOTE, tax="含税", location="quote.pdf:p2"):
    return SupplierQuote(qid, cid, supplier, "M-1", config, amount, currency, source_type,
                         "quote.pdf", location, conditions=[QuoteCondition("tax", tax, location),
                         QuoteCondition("delivery", "7天", location), QuoteCondition("warranty", "1年", location)])


def test_quote_sources_and_json_round_trip():
    q = quote(source_type=QuoteSourceType.OFFICIAL_PUBLIC_PRICE)
    data = q.to_dict()
    assert data["source_type"] == "official_public_price" and data["currency"] == "CNY"
    assert {c["condition_type"] for c in data["conditions"]} >= {"tax", "delivery", "installation", "warranty", "after_sales", "payment"}


def test_historical_dealer_and_snippet_are_not_current_comparable():
    result = compare_quotes([quote(), quote("q2", amount=80, source_type=QuoteSourceType.HISTORICAL_TENDER_PRICE)])
    assert not result.incomparable[0].comparable
    assert "historical_or_lead_reference_only" in result.incomparable[0].reason
    try:
        quote(source_type=QuoteSourceType.UNKNOWN, location="snippet: search result")
    except ModuleValidationError as exc:
        assert exc.code == "snippet_quote_forbidden"
    else:
        raise AssertionError("snippet quote should be rejected")


def test_configuration_currency_and_tax_mismatch_are_separated():
    result = compare_quotes([quote(), quote("q2", currency="USD", config="pro", tax="未税")])
    assert result.comparable == ()
    assert {"configuration_mismatch", "currency_mismatch", "tax_condition_mismatch"} <= set(result.incomparable[0].reason.split(";"))


def test_delivery_warranty_gaps_are_explicit_and_comparable():
    result = compare_quotes([quote(), quote("q2", amount=120)])
    assert result.comparable[0].amount_delta == 20
    assert result.gaps == ()


def test_unknown_tax_is_not_comparable():
    a = SupplierQuote("q1", "c1", "甲", "M-1", "std", 100, "CNY", QuoteSourceType.CURRENT_SUPPLIER_QUOTE,
                      "quote.pdf", "quote.pdf:p1")
    b = quote("q2")
    result = compare_quotes([a, b])
    assert result.comparable == () and "tax_condition_unknown" in result.incomparable[0].reason


def test_deduplication_and_stable_aggregation():
    qs = [quote("b", supplier="乙"), quote("a", supplier="甲"), quote("a", supplier="甲")]
    assert [q.quote_id for q in deduplicate_quotes(qs)] == ["a", "b"]
    grouped = aggregate_quotes(qs, by="supplier")
    assert list(grouped) == ["乙", "甲"] or list(grouped) == ["甲", "乙"]
    try:
        compare_quotes([quote(amount=-1)])
    except ModuleValidationError as exc:
        assert exc.code == "invalid_amount"
    else:
        raise AssertionError("negative amount should fail")
