import json
import secrets
import urllib.error
import pytest

import backend.services.deepseek_analysis as deepseek_analysis
from backend.infrastructure.deepseek import DeepSeekClient, DeepSeekError
from backend.services.deepseek_analysis import CANONICAL_FIELD_NAMES, DeepSeekAnalysisService
from backend.main import create_app
from fastapi.testclient import TestClient


def document():
    return {"id": "doc-1", "source_name": "quote.txt", "pages": [{"number": 2, "source_location": "page:2", "text": "Model X100 price 12000 CNY"}], "tables": []}


def transport(_key, _endpoint, payload, _timeout):
    assert payload["model"] == "deepseek-flash"
    assert payload["max_tokens"] == 8192
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["response_format"] == {"type": "json_object"}
    content = {"candidates": [{"brand": "Acme", "model": "X100", "sources": [{"document_id": "doc-1", "page_or_location": "page:2", "excerpt": "Model X100"}], "fields": [{"field_name": "price_warranty_delivery_and_service", "value": {"price": 12000}, "unit": "CNY", "status": "confirmed", "sources": [{"document_id": "doc-1", "page_or_location": "page:2", "excerpt": "price 12000 CNY"}]}]}]}
    return {"choices": [{"message": {"content": json.dumps(content)}}]}


def test_deepseek_accepts_single_complete_json_fence():
    def fenced_transport(_key, _endpoint, _payload, _timeout):
        return {"choices": [{"message": {"content": '```json\n{"ok": true}\n```'}}]}

    client = DeepSeekClient.from_env(
        env={"DEEPSEEK_API_KEY": secrets.token_urlsafe(24)},
        transport=fenced_transport,
    )
    assert client.analyze(system_prompt="system", user_prompt="user") == {"ok": True}


def test_deepseek_maps_http_402_to_payment_required(monkeypatch):
    def insufficient_balance(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://api.deepseek.com/chat/completions", 402,
            "Payment Required", {}, None,
        )

    monkeypatch.setattr("urllib.request.urlopen", insufficient_balance)
    client = DeepSeekClient.from_env(env={"DEEPSEEK_API_KEY": secrets.token_urlsafe(24)})

    with pytest.raises(DeepSeekError) as error:
        client.analyze(system_prompt="system", user_prompt="user")

    assert error.value.code == "provider_payment_required"
    assert "余额不足" in error.value.message


@pytest.mark.parametrize("content", [
    '说明如下：\n{"ok": true}',
    '```json\n{"ok": true}',
    '{"ok": true}\n{"extra": true}',
])
def test_deepseek_rejects_ambiguous_or_incomplete_json(content):
    def invalid_transport(_key, _endpoint, _payload, _timeout):
        return {"choices": [{"message": {"content": content}}]}

    client = DeepSeekClient.from_env(
        env={"DEEPSEEK_API_KEY": secrets.token_urlsafe(24)},
        transport=invalid_transport,
    )
    with pytest.raises(DeepSeekError) as error:
        client.analyze(system_prompt="system", user_prompt="user")
    assert error.value.code == "invalid_model_json"


def test_deepseek_retries_truncated_json_once_and_succeeds():
    calls = []

    def retry_transport(_key, _endpoint, payload, _timeout):
        calls.append(payload)
        if len(calls) == 1:
            return {"choices": [{"finish_reason": "length", "message": {"content": '{"candidates": ['}}]}
        return {"choices": [{"finish_reason": "stop", "message": {"content": '{"candidates": []}'}}]}

    client = DeepSeekClient.from_env(
        env={"DEEPSEEK_API_KEY": secrets.token_urlsafe(24)},
        transport=retry_transport, json_retries=1,
    )
    assert client.analyze(system_prompt="返回 json", user_prompt="资料") == {"candidates": []}
    assert len(calls) == 2
    assert "上一次输出为空、被截断" in calls[1]["messages"][-1]["content"]


def test_deepseek_reports_truncation_after_retry():
    def truncated_transport(_key, _endpoint, _payload, _timeout):
        return {"choices": [{"finish_reason": "length", "message": {"content": '{"candidates": ['}}]}

    client = DeepSeekClient.from_env(
        env={"DEEPSEEK_API_KEY": secrets.token_urlsafe(24)},
        transport=truncated_transport,
    )
    with pytest.raises(DeepSeekError) as error:
        client.analyze(system_prompt="返回 json", user_prompt="资料")
    assert error.value.code == "model_output_truncated"


def test_analysis_prompt_serializes_documents_as_json():
    observed = {}

    def json_transport(_key, _endpoint, payload, _timeout):
        observed["prompt"] = payload["messages"][1]["content"]
        return {"choices": [{"finish_reason": "stop", "message": {"content": '{"candidates": []}'}}]}

    client = DeepSeekClient.from_env(
        env={"DEEPSEEK_API_KEY": secrets.token_urlsafe(24)},
        transport=json_transport,
    )
    DeepSeekAnalysisService(client).analyze(scenario="采购检测设备", documents=[document()])
    assert '"document_id":"doc-1"' in observed["prompt"]
    assert "'document_id': 'doc-1'" not in observed["prompt"]


def test_analysis_system_prompt_defines_stable_procurement_field_contract():
    observed = {}

    def contract_transport(_key, _endpoint, payload, _timeout):
        system = payload["messages"][0]["content"]
        if "候选清单整理器" in system:
            content = {"candidates": [{
                "model": "X100",
                "sources": [{"document_id": "doc-1", "page_or_location": "page:2", "excerpt": "X100"}],
            }]}
        else:
            observed["system"] = system
            content = {"candidates": [{
                "model": "X100",
                "fields": [{
                    "field_name": name, "value": "unknown", "status": "unknown", "sources": [],
                } for name in CANONICAL_FIELD_NAMES],
            }]}
        return {"choices": [{"message": {"content": json.dumps(content)}}]}

    client = DeepSeekClient.from_env(
        env={"DEEPSEEK_API_KEY": secrets.token_urlsafe(24)},
        transport=contract_transport,
    )
    DeepSeekAnalysisService(client).analyze(scenario="采购", documents=[document()])
    assert all(name in observed["system"] for name in CANONICAL_FIELD_NAMES)


def test_visual_analysis_splits_inventory_and_candidate_details():
    calls = []
    detail_payloads = []

    def visual_transport(_key, _endpoint, payload, _timeout):
        system = payload["messages"][0]["content"]
        calls.append(system)
        if "候选清单整理器" in system:
            content = {"candidates": [{
                "brand": "Acme", "series": "X", "model": "X100", "configuration": "96-well",
                "sources": [{"document_id": "doc-1", "page_or_location": "page:1/rendered", "excerpt": "X100"}],
            }]}
        else:
            detail_payloads.append(json.loads(payload["messages"][1]["content"].split(
                "候选级有界证据 JSON：\n", 1,
            )[1]))
            content = {"candidates": [{
                "brand": "wrong", "model": "wrong",
                "fields": [{
                    "field_name": name, "value": "unknown", "status": "unknown", "sources": [],
                } for name in CANONICAL_FIELD_NAMES],
            }]}
        return {"choices": [{"message": {"content": json.dumps(content)}}]}

    client = DeepSeekClient.from_env(
        env={"DEEPSEEK_API_KEY": secrets.token_urlsafe(24)}, transport=visual_transport,
    )
    visual_document = {
        "id": "doc-1", "source_name": "visual.pdf",
        "pages": [{"number": 1, "source_location": "page:1", "text": "parser text"}],
        "tables": [],
        "model_extraction": {"fields": {"page:1/rendered": {
            "text": "Acme X100", "fields": {"candidate_mentions": {"value": [{"model": "X100"}]}},
        }}},
    }
    result = DeepSeekAnalysisService(client).analyze(scenario="采购", documents=[visual_document])
    assert len(calls) == 2
    assert result["candidates"][0]["model"] == "X100"
    assert result["candidates"][0]["configuration"] == "96-well"
    assert len(result["candidates"][0]["fields"]) == 12
    assert detail_payloads[0]["evidence_selection"]["scope"] == "candidate_bounded_page_subset"
    assert detail_payloads[0]["documents"][0]["pages"][0]["location"] == "page:1/rendered"


def test_visual_candidate_evidence_excludes_unowned_adjacent_pages():
    detail_payloads = []

    def visual_transport(_key, _endpoint, payload, _timeout):
        system = payload["messages"][0]["content"]
        if "候选清单整理器" in system:
            content = {"candidates": [{
                "brand": "Acme", "series": "Prime", "model": "X-100",
                "configuration": "96 well",
                "sources": [{
                    "document_id": "doc-catalog",
                    "page_or_location": "page:8",
                    "excerpt": "Acme X-100",
                }],
            }]}
        else:
            detail_payloads.append(json.loads(payload["messages"][1]["content"].split(
                "候选级有界证据 JSON：\n", 1,
            )[1]))
            content = {"candidates": [{
                "model": "X-100",
                "fields": [{
                    "field_name": name, "value": "unknown", "status": "unknown", "sources": [],
                } for name in CANONICAL_FIELD_NAMES],
            }]}
        return {"choices": [{"message": {"content": json.dumps(content)}}]}

    pages = [
        {"number": page, "source_location": f"page:{page}", "text": f"unrelated page {page}"}
        for page in range(1, 11)
    ]
    pages[4]["text"] = "ACME Prime X 100, 96-well configuration"
    pages[7]["text"] = "Acme X-100 catalog continuation page 8"
    visual_document = {
        "id": "doc-catalog", "source_name": "catalog.pdf", "pages": pages, "tables": [],
        "model_extraction": {"fields": {"page:5/rendered": {
            "text": "ACME Prime X 100, 96-well configuration",
            "fields": {"candidate_mentions": {"value": [{"model": "X-100"}]}},
        }}},
    }
    client = DeepSeekClient.from_env(
        env={"DEEPSEEK_API_KEY": secrets.token_urlsafe(24)}, transport=visual_transport,
    )

    result = DeepSeekAnalysisService(client).analyze(scenario="采购", documents=[visual_document])

    evidence = detail_payloads[0]
    selected_pages = evidence["documents"][0]["pages"]
    assert [page["page"] for page in selected_pages] == [5, 8]
    assert len(selected_pages) < len(pages)
    assert evidence["evidence_selection"]["selected_page_count"] == 2
    assert evidence["evidence_selection"]["omitted_page_count"] == 0
    assert evidence["evidence_selection"]["fallback_used"] is False
    selected_page_five = next(
        page for page in evidence["evidence_selection"]["selected_pages"] if page["page"] == 5
    )
    assert selected_page_five["document_id"] == "doc-catalog"
    assert selected_page_five["location"] == "page:5/rendered"
    assert "candidate_model_match" in selected_page_five["selection_reasons"]
    page_five_evidence = next(page for page in selected_pages if page["page"] == 5)
    assert page_five_evidence["provenance"] == {
        "document_id": "doc-catalog",
        "source_name": "catalog.pdf",
        "analysis_input": "rendered_page_vision",
        "page": 5,
        "location": "page:5/rendered",
    }
    selected_page_eight = next(
        page for page in evidence["evidence_selection"]["selected_pages"] if page["page"] == 8
    )
    assert "inventory_source" in selected_page_eight["selection_reasons"]
    assert result["candidates"][0]["model"] == "X-100"
    assert len(result["candidates"][0]["fields"]) == len(CANONICAL_FIELD_NAMES)


def test_visual_candidate_without_model_anchor_does_not_become_product_evidence():
    """无型号归属页的候选：不发参数抽取调用，回退页也绝不成为产品证据。

    这条原来靠检查空载荷（``selected_page_count == 0`` / ``documents == []``）
    来钉住"回退页不算证据"；现在这种候选连调用都不发，钉法换成"没有调用"，
    保护的内容不变。
    """
    detail_payloads = []

    def visual_transport(_key, _endpoint, payload, _timeout):
        system = payload["messages"][0]["content"]
        if "候选清单整理器" in system:
            content = {"candidates": [{
                "brand": "Absent Brand", "model": "NO-MATCH-9000",
                "sources": [{
                    "document_id": "doc-fallback",
                    "page_or_location": "page:99/rendered",
                    "excerpt": "inventory location is unavailable",
                }],
            }]}
        else:
            detail_payloads.append(json.loads(payload["messages"][1]["content"].split(
                "候选级有界证据 JSON：\n", 1,
            )[1]))
            content = {"candidates": [{
                "model": "NO-MATCH-9000",
                "fields": [{
                    "field_name": name, "value": "unknown", "status": "unknown", "sources": [],
                } for name in CANONICAL_FIELD_NAMES],
            }]}
        return {"choices": [{"message": {"content": json.dumps(content)}}]}

    pages = [
        {"number": page, "source_location": f"page:{page}", "text": f"generic catalog page {page}"}
        for page in range(1, 13)
    ]
    visual_document = {
        "id": "doc-fallback", "source_name": "fallback.pdf", "pages": pages, "tables": [],
        "model_extraction": {"fields": {"page:1/rendered": {
            "text": "generic catalog page 1",
            "fields": {"candidate_mentions": {"value": [{"model": "something else"}]}},
        }}},
    }
    client = DeepSeekClient.from_env(
        env={"DEEPSEEK_API_KEY": secrets.token_urlsafe(24)}, transport=visual_transport,
    )

    DeepSeekAnalysisService(client).analyze(scenario="采购", documents=[visual_document])

    # No model anchor: no page owns this candidate, so no parameter call is paid
    # for and broad fallback pages must not become product evidence either.
    assert detail_payloads == []


def test_hybrid_inventory_includes_visual_and_native_pages_with_provenance():
    observed_units = []
    detail_candidates = []

    def hybrid_transport(_key, _endpoint, payload, _timeout):
        system = payload["messages"][0]["content"]
        user = payload["messages"][1]["content"]
        if "候选清单整理器" in system:
            batch = json.loads(user.split("有界资料批次整理 json 候选清单：\n", 1)[1])
            observed_units.extend(batch["evidence_units"])
            content = {"candidates": [{
                "brand": "NativeCo", "model": "N-200",
                "sources": [{
                    "document_id": "doc-hybrid", "page_or_location": "page:2",
                    "excerpt": "NativeCo N-200",
                }],
            }]}
        else:
            detail_candidates.append(json.loads(user.split("\ncandidate：", 1)[1].split(
                "\n候选级有界证据 JSON：", 1,
            )[0]))
            content = {"candidates": [{"model": "ignored", "fields": []}]}
        return {"choices": [{"message": {"content": json.dumps(content)}}]}

    hybrid_document = {
        "id": "doc-hybrid", "source_name": "hybrid.pdf", "tables": [],
        "pages": [
            {"number": 1, "source_location": "page:1", "route": "vision", "text": "bad native"},
            {"number": 2, "source_location": "page:2", "route": "native_text", "text": "NativeCo N-200"},
        ],
        "model_extraction": {"fields": {"page:1/rendered": {
            "text": "VisionCo V-100",
            "fields": {"candidate_mentions": {"value": [{"model": "V-100"}]}},
        }}},
    }
    client = DeepSeekClient.from_env(
        env={"DEEPSEEK_API_KEY": secrets.token_urlsafe(24)}, transport=hybrid_transport,
    )

    result = DeepSeekAnalysisService(client).analyze(scenario="采购", documents=[hybrid_document])

    assert [(unit["location"], unit["analysis_input"]) for unit in observed_units] == [
        ("page:1/rendered", "rendered_page_vision"),
        ("page:2", "parser_text"),
    ]
    assert observed_units[0]["candidate_mentions"] == [{"model": "V-100"}]
    assert observed_units[1]["text"] == "NativeCo N-200"
    assert observed_units[1]["provenance"] == {
        "document_id": "doc-hybrid",
        "source_name": "hybrid.pdf",
        "analysis_input": "parser_text",
        "page": 2,
        "location": "page:2",
    }
    assert detail_candidates[0]["model"] == "N-200"
    assert result["candidates"][0]["model"] == "N-200"
    assert [field["field_name"] for field in result["candidates"][0]["fields"]] == list(CANONICAL_FIELD_NAMES)


def test_native_text_inventory_batches_are_bounded_and_merge_candidate_sources(monkeypatch):
    monkeypatch.setattr(deepseek_analysis, "_INVENTORY_BATCH_CHAR_LIMIT", 2_300)
    inventory_batches = []
    detail_payloads = []

    def native_transport(_key, _endpoint, payload, _timeout):
        system = payload["messages"][0]["content"]
        user = payload["messages"][1]["content"]
        if "候选清单整理器" in system:
            batch = json.loads(user.split("有界资料批次整理 json 候选清单：\n", 1)[1])
            inventory_batches.append(batch)
            location = batch["evidence_units"][0]["location"]
            content = {"candidates": [{
                "brand": "Acme" if len(inventory_batches) == 1 else None,
                "series": "X", "model": "X-100", "configuration": None,
                "sources": [{
                    "document_id": "doc-native", "page_or_location": location,
                    "excerpt": f"X-100 in {location}",
                }],
            }]}
        else:
            detail_payloads.append(json.loads(user.split("候选级有界证据 JSON：\n", 1)[1]))
            content = {"candidates": [{"model": "ignored", "fields": []}]}
        return {"choices": [{"message": {"content": json.dumps(content)}}]}

    pages = [{
        "number": page, "source_location": f"page:{page}", "route": "native_text",
        "text": f"Acme X-100 page {page} " + ("z" * 900),
    } for page in range(1, 7)]
    native_document = {
        "id": "doc-native", "source_name": "native.pdf", "pages": pages, "tables": [],
    }
    client = DeepSeekClient.from_env(
        env={"DEEPSEEK_API_KEY": secrets.token_urlsafe(24)}, transport=native_transport,
    )

    result = DeepSeekAnalysisService(client).analyze(scenario="采购", documents=[native_document])

    assert len(inventory_batches) > 1
    assert all(
        batch["inventory_batch"]["payload_characters"] <= 2_300
        for batch in inventory_batches
    )
    assert all(batch["inventory_batch"]["character_budget"] == 2_300 for batch in inventory_batches)
    assert len(detail_payloads) == 1
    selection = detail_payloads[0]["evidence_selection"]
    assert selection["selected_text_character_count"] <= selection["text_character_budget"]
    assert selection["page_text_character_limit"] == 6_000
    assert result["candidates"][0]["brand"] == "Acme"
    assert result["candidates"][0]["model"] == "X-100"
    assert len(result["candidates"][0]["sources"]) > 1
    assert [field["field_name"] for field in result["candidates"][0]["fields"]] == list(CANONICAL_FIELD_NAMES)


def test_inventory_rejects_explicit_accessory_roles_before_detail_calls():
    evidence = {
        ("doc-products", "page:1"): "MainCo Instrument-100 laboratory instrument",
        ("doc-products", "page:2"): "Injector module Te-inject accessory",
        ("doc-products", "page:3"): "NanoQuant microplate accessory",
    }
    inventory = {"candidates": [
        {
            "product_role": "instrument", "brand": "MainCo", "model": "Instrument-100",
            "sources": [{
                "document_id": "doc-products", "page_or_location": "page:1",
                "excerpt": "Instrument-100 laboratory instrument",
            }],
        },
        {
            "product_role": "module", "brand": "MainCo", "model": "Te-inject",
            "sources": [{
                "document_id": "doc-products", "page_or_location": "page:2",
                "excerpt": "Te-inject accessory",
            }],
        },
        {
            "product_role": "plate", "brand": "MainCo", "model": "NanoQuant",
            "sources": [{
                "document_id": "doc-products", "page_or_location": "page:3",
                "excerpt": "NanoQuant microplate accessory",
            }],
        },
        {
            "product_role": "instrument", "brand": "MainCo", "model": "None",
            "sources": [{
                "document_id": "doc-products", "page_or_location": "page:1",
                "excerpt": "MainCo",
            }],
        },
    ]}

    candidates = deepseek_analysis._merge_inventory_candidates([inventory], evidence)

    assert [candidate["model"] for candidate in candidates] == ["Instrument-100"]


def test_inventory_promotes_product_model_and_merges_ordering_configurations():
    evidence = {
        ("doc-ordering", "page:2/rendered"): (
            "订购信息\n离心机 货号\n"
            "5420 离心机，非冷冻，不含转子，230 V 5420 000 296\n"
            "5420 离心机，非冷冻，含固定角转，230 V 5420 000 091"
        ),
    }
    inventory = {"candidates": [
        {
            "product_role": "instrument_configuration", "brand": "Eppendorf",
            "series": "5420", "model": "5420 000 296",
            "configuration": "非冷冻，不含转子，230 V",
            "sources": [{
                "document_id": "doc-ordering", "page_or_location": "page:2/rendered",
                "excerpt": "5420 离心机，非冷冻，不含转子，230 V 5420 000 296",
            }],
        },
        {
            "product_role": "instrument_configuration", "brand": "Eppendorf",
            "series": "5420", "model": "5420 000 091",
            "configuration": "非冷冻，含固定角转，230 V",
            "sources": [{
                "document_id": "doc-ordering", "page_or_location": "page:2/rendered",
                "excerpt": "5420 离心机，非冷冻，含固定角转，230 V 5420 000 091",
            }],
        },
    ]}

    candidates = deepseek_analysis._merge_inventory_candidates([inventory], evidence)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["brand"] == "Eppendorf"
    assert candidate["model"] == "5420"
    assert "订货号 5420 000 296" in candidate["configuration"]
    assert "订货号 5420 000 091" in candidate["configuration"]
    assert len(candidate["sources"]) == 2


def test_inventory_keeps_real_models_distinct_when_ordering_evidence_is_absent():
    evidence = {
        ("doc-models", "page:1"): "SPX-70BIII and SPX-150BIII are independent models.",
    }
    inventory = {"candidates": [
        {
            "product_role": "instrument", "brand": "Acme", "series": "SPX",
            "model": "SPX-70BIII", "configuration": None,
            "sources": [{
                "document_id": "doc-models", "page_or_location": "page:1",
                "excerpt": "SPX-70BIII",
            }],
        },
        {
            "product_role": "instrument", "brand": "Acme", "series": "SPX",
            "model": "SPX-150BIII", "configuration": None,
            "sources": [{
                "document_id": "doc-models", "page_or_location": "page:1",
                "excerpt": "SPX-150BIII",
            }],
        },
    ]}

    candidates = deepseek_analysis._merge_inventory_candidates([inventory], evidence)

    assert [candidate["model"] for candidate in candidates] == ["SPX-70BIII", "SPX-150BIII"]


def test_field_gap_repair_only_accepts_requested_evidence_backed_values():
    fields = [{
        "field_name": name,
        "value": {"capacity": "24 samples"} if name == "capacity_and_throughput" else "unknown",
        "status": "pending_review" if name == "capacity_and_throughput" else "unknown",
        "sources": ([{
            "document_id": "doc-gap", "page_or_location": "page:2",
            "excerpt": "capacity 24 samples",
        }] if name == "capacity_and_throughput" else []),
    } for name in CANONICAL_FIELD_NAMES]
    evidence_payload = {"evidence_selection": {"field_coverage": {
        "performance_range": {"covered": True},
        "physical_dimensions_power_and_installation": {"covered": False},
    }}}

    requested = deepseek_analysis._repairable_field_names(fields, evidence_payload)
    merged = deepseek_analysis._merge_repaired_fields(
        fields,
        [{
            "field_name": "performance_range", "value": {"speed": "15,060 rpm"},
            "sources": [{
                "document_id": "doc-gap", "page_or_location": "page:2",
                "excerpt": "maximum speed 15,060 rpm",
            }],
        }, {
            "field_name": "physical_dimensions_power_and_installation",
            "value": {"power": "290 W"},
            "sources": [{
                "document_id": "doc-gap", "page_or_location": "page:2",
                "excerpt": "maximum power 290 W",
            }],
        }],
        requested,
        {("doc-gap", "page:2"): "capacity 24 samples; maximum speed 15,060 rpm; maximum power 290 W"},
    )

    assert requested == ["performance_range"]
    by_name = {field["field_name"]: field for field in merged}
    assert by_name["performance_range"]["value"] == {"speed": "15,060 rpm"}
    assert by_name["performance_range"]["status"] == "pending_review"
    assert by_name["physical_dimensions_power_and_installation"]["value"] == "unknown"


def test_excerpt_validation_accepts_only_fully_supported_multi_clause_quotes():
    evidence = (
        "最大相对离心力 (rcf)：21,300 × g\n"
        "最高转速：15,060 rpm\n"
        "噪音水平：< 56 dB(A)"
    )

    assert deepseek_analysis._excerpt_matches_evidence(
        "最大相对离心力 (rcf)：21,300 × g；最高转速：15,060 rpm；噪音水平：< 56 dB(A)",
        evidence,
    )
    assert not deepseek_analysis._excerpt_matches_evidence(
        "最大相对离心力 (rcf)：21,300 × g；最高转速：99,999 rpm",
        evidence,
    )


def test_inventory_merges_same_brand_model_despite_series_or_configuration_labels():
    evidence = {
        ("doc-duplicate", "page:6"): "Spark Cyto live-cell imaging instrument",
        ("doc-duplicate", "page:6/table:1"): "Spark Cyto supports 6-384 well plates",
    }
    inventory = {"candidates": [{
        "brand": "Tecan", "series": "Spark Cyto", "model": "Spark Cyto",
        "configuration": "live-cell imaging", "sources": [{
            "document_id": "doc-duplicate", "page_or_location": "page:6",
            "excerpt": "Spark Cyto live-cell imaging instrument",
        }],
    }, {
        "brand": "Tecan", "series": "Spark", "model": "Spark Cyto",
        "configuration": None, "sources": [{
            "document_id": "doc-duplicate", "page_or_location": "page:6/table:1",
            "excerpt": "Spark Cyto supports 6-384 well plates",
        }],
    }]}

    candidates = deepseek_analysis._merge_inventory_candidates([inventory], evidence)

    assert len(candidates) == 1
    assert candidates[0]["model"] == "Spark Cyto"
    assert candidates[0]["configuration"] == "live-cell imaging"
    assert len(candidates[0]["sources"]) == 2


def test_inventory_rejects_model_mentioned_only_in_accessory_compatibility_context():
    evidence = {
        ("doc-accessory", "page:5"): (
            "Linear pipette stand is suitable for TopPette, MicroPette Plus and Hipette pipettes"
        ),
    }
    inventory = {"candidates": [{
        "product_role": "instrument", "brand": "DLAB", "series": "Hipette", "model": "Hipette",
        "sources": [{
            "document_id": "doc-accessory", "page_or_location": "page:5",
            "excerpt": "pipette stand is suitable for TopPette, MicroPette Plus and Hipette pipettes",
        }],
    }]}

    assert deepseek_analysis._merge_inventory_candidates([inventory], evidence) == []


def test_inventory_filters_role_confusion_without_hiding_main_instrument_on_same_page():
    evidence = {
        ("doc-role", "page:1/rendered"): "Main-100 laboratory instrument with full specifications",
        ("doc-role", "page:2/rendered"): (
            "AnalysisSuite software provides data analysis and export."
        ),
        ("doc-role", "page:3/rendered"): (
            "Consumables. Count Chips are individually packaged, 50 chips/box. "
            "Auto Lid patch discs, 50 discs/box."
        ),
        ("doc-role", "page:4/rendered"): (
            "Name Assay600 Measurement wavelength 600 Reference well A1 Bandwidth 3.5 Input data"
        ),
        ("doc-role", "page:5/rendered"): (
            "Besides Main-100, we also offer FlowStation automation. "
            "More information at example.test/flowstation."
        ),
    }
    inventory = {"candidates": [
        {
            "product_role": "instrument", "model": "Main-100",
            "sources": [{
                "document_id": "doc-role", "page_or_location": "page:1/rendered",
                "excerpt": "Main-100 laboratory instrument with full specifications",
            }],
        },
        {
            "product_role": "instrument", "model": "AnalysisSuite",
            "sources": [{
                "document_id": "doc-role", "page_or_location": "page:2/rendered",
                "excerpt": "AnalysisSuite software provides data analysis and export",
            }],
        },
        {
            "product_role": "instrument", "model": "Count Chips",
            "sources": [{
                "document_id": "doc-role", "page_or_location": "page:3/rendered",
                "excerpt": "Count Chips are individually packaged, 50 chips/box",
            }],
        },
        {
            "product_role": "instrument", "model": "Auto Lid",
            "sources": [{
                "document_id": "doc-role", "page_or_location": "page:3/rendered",
                "excerpt": "Auto Lid patch discs, 50 discs/box",
            }],
        },
        {
            "product_role": "instrument", "model": "Assay600",
            "sources": [{
                "document_id": "doc-role", "page_or_location": "page:4/rendered",
                "excerpt": "Name Assay600",
            }],
        },
        {
            "product_role": "instrument", "model": "FlowStation",
            "sources": [{
                "document_id": "doc-role", "page_or_location": "page:5/rendered",
                "excerpt": "FlowStation automation",
            }],
        },
    ]}

    candidates = deepseek_analysis._merge_inventory_candidates([inventory], evidence)

    assert [candidate["model"] for candidate in candidates] == ["Main-100"]


def test_inventory_rejects_explicit_non_instrument_visual_role():
    evidence = {("doc-role", "page:1/rendered"): "DataSuite product name"}
    inventory = {"candidates": [{
        "product_role": "software", "model": "DataSuite",
        "sources": [{
            "document_id": "doc-role", "page_or_location": "page:1/rendered",
            "excerpt": "DataSuite product name",
        }],
    }]}

    assert deepseek_analysis._merge_inventory_candidates([inventory], evidence) == []


def test_structured_visual_mentions_seed_inventory_without_generic_unquoted_series():
    prompt_docs = [{
        "document_id": "doc-visual-seed", "pages": [{
            "location": "page:4/rendered", "visual_fields": {
                "candidate_mentions": {"value": [{
                    "brand": "TopPette", "series": "TopPette", "model": None,
                    "configuration": "manual pipette",
                    "evidence_quote": "TopPette manual pipette",
                }, {
                    "brand": None, "series": "Liquid Handling", "model": None,
                    "configuration": "fixed volume",
                    "evidence_quote": "fixed volume pipette",
                }]},
            },
        }, {
            "location": "page:6/rendered", "visual_fields": {
                "candidate_mentions": {"value": [{
                    "brand": None, "series": "MicroPette Plus", "model": None,
                    "configuration": "variable and fixed volume",
                    "evidence_quote": "MicroPette Plus series pipettes",
                }, {
                    "brand": None, "series": "TOP", "model": None,
                    "configuration": "variable and fixed volume",
                    "evidence_quote": "TOP and MicroPette Plus series pipettes",
                }]},
            },
        }],
    }]

    seeded = deepseek_analysis._structured_mention_inventory(prompt_docs)

    assert [candidate["model"] for candidate in seeded["candidates"]] == [
        "TopPette", "MicroPette Plus", "TopPette",
    ]
    assert seeded["candidates"][0]["brand"] is None


def test_inventory_merges_unambiguous_short_series_alias_from_any_inventory_pass():
    evidence = {
        ("doc-series", "page:4/rendered"): "TopPette manual pipette",
        ("doc-series", "page:6/rendered"): "TOP and MicroPette Plus series pipettes",
    }
    inventories = [{"candidates": [{
        "brand": "DLAB", "series": "TopPette", "model": "TopPette",
        "sources": [{
            "document_id": "doc-series", "page_or_location": "page:4/rendered",
            "excerpt": "TopPette manual pipette",
        }],
    }]}, {"candidates": [{
        "series": "TOP", "model": "TOP", "configuration": "variable and fixed volume",
        "sources": [{
            "document_id": "doc-series", "page_or_location": "page:6/rendered",
            "excerpt": "TOP and MicroPette Plus series pipettes",
        }],
    }]}]

    candidates = deepseek_analysis._merge_inventory_candidates(inventories, evidence)

    assert [candidate["model"] for candidate in candidates] == ["TopPette"]
    assert candidates[0]["configuration"] == "variable and fixed volume"
    assert len(candidates[0]["sources"]) == 2


def test_inventory_does_not_merge_ambiguous_short_series_prefix():
    evidence = {
        ("doc-series", "page:1"): "SPX series: SPX-70 and SPX-150",
    }
    inventory = {"candidates": [{
        "series": "SPX", "model": model,
        "sources": [{
            "document_id": "doc-series", "page_or_location": "page:1",
            "excerpt": "SPX series: SPX-70 and SPX-150",
        }],
    } for model in ("SPX", "SPX-70", "SPX-150")]}

    candidates = deepseek_analysis._merge_inventory_candidates([inventory], evidence)

    assert [candidate["model"] for candidate in candidates] == ["SPX", "SPX-70", "SPX-150"]


def test_long_native_page_keeps_tail_candidate_and_centers_detail_window():
    inventory_texts = []
    detail_payloads = []

    def long_page_transport(_key, _endpoint, payload, _timeout):
        system = payload["messages"][0]["content"]
        user = payload["messages"][1]["content"]
        if "候选清单整理器" in system:
            batch = json.loads(user.split("有界资料批次整理 json 候选清单：\n", 1)[1])
            batch_text = "\n".join(unit["text"] for unit in batch["evidence_units"])
            inventory_texts.append(batch_text)
            candidates = []
            if "T-900" in batch_text:
                candidates.append({
                    "brand": "TailCo", "model": "T-900",
                    "sources": [{
                        "document_id": "doc-long", "page_or_location": "page:1",
                        "excerpt": "TailCo T-900",
                    }],
                })
            content = {"candidates": candidates}
        else:
            detail_payloads.append(json.loads(user.split("候选级有界证据 JSON：\n", 1)[1]))
            content = {"candidates": [{"model": "ignored", "fields": []}]}
        return {"choices": [{"message": {"content": json.dumps(content)}}]}

    long_text = ("front matter " * 1_700) + " TailCo T-900 throughput 900 samples/h"
    long_document = {
        "id": "doc-long", "source_name": "long.pdf", "tables": [],
        "pages": [{
            "number": 1, "source_location": "page:1", "route": "native_text", "text": long_text,
        }],
    }
    client = DeepSeekClient.from_env(
        env={"DEEPSEEK_API_KEY": secrets.token_urlsafe(24)}, transport=long_page_transport,
    )

    result = DeepSeekAnalysisService(client).analyze(scenario="采购", documents=[long_document])

    assert "T-900" in "".join(inventory_texts)
    detail_page = detail_payloads[0]["documents"][0]["pages"][0]
    assert detail_page["text_truncated"] is True
    assert detail_page["text_char_start"] > 0
    assert "T-900" in detail_page["text"]
    assert detail_payloads[0]["evidence_selection"]["omitted_selected_page_text_characters"] > 0
    assert result["candidates"][0]["model"] == "T-900"


def test_explicit_native_route_does_not_use_stale_rendered_page_cache():
    inventory_units = []

    def route_transport(_key, _endpoint, payload, _timeout):
        system = payload["messages"][0]["content"]
        user = payload["messages"][1]["content"]
        if "候选清单整理器" in system:
            batch = json.loads(user.split("有界资料批次整理 json 候选清单：\n", 1)[1])
            inventory_units.extend(batch["evidence_units"])
            content = {"candidates": []}
        else:
            pytest.fail("No detail call is expected when inventory is empty")
        return {"choices": [{"message": {"content": json.dumps(content)}}]}

    routed_document = {
        "id": "doc-route", "source_name": "route.pdf", "tables": [],
        "pages": [{
            "number": 1, "source_location": "page:1", "route": "native_text",
            "text": "CURRENT NATIVE TEXT",
        }],
        "model_extraction": {"fields": {"page:1/rendered": {
            "text": "STALE VISUAL CACHE", "fields": {"candidate_mentions": [{"model": "STALE"}]},
        }}},
    }
    client = DeepSeekClient.from_env(
        env={"DEEPSEEK_API_KEY": secrets.token_urlsafe(24)}, transport=route_transport,
    )

    DeepSeekAnalysisService(client).analyze(scenario="采购", documents=[routed_document])

    assert inventory_units[0]["text"] == "CURRENT NATIVE TEXT"
    assert inventory_units[0]["location"] == "page:1"
    assert inventory_units[0]["analysis_input"] == "parser_text"
    assert inventory_units[0]["candidate_mentions"] == []


def test_normalize_enforces_canonical_field_order_and_conflicts():
    service = DeepSeekAnalysisService()
    source_one = {"document_id": "doc", "page_or_location": "page:1", "excerpt": "100 rpm"}
    source_two = {"document_id": "doc", "page_or_location": "page:2", "excerpt": "200 rpm"}

    result = service._normalize({"candidates": [{
        "model": "X-100",
        "fields": [
            {
                "field_name": "capacity_and_throughput", "value": {"speed": "100 rpm"},
                "status": "pending_review", "sources": [source_one],
            },
            {"field_name": "hallucinated_extra", "value": "must not leak", "sources": [source_one]},
            {
                "field_name": "capacity_and_throughput", "value": {"speed": "200 rpm"},
                "status": "pending_review", "sources": [source_two],
            },
        ],
    }]})

    fields = result["candidates"][0]["fields"]
    assert [field["field_name"] for field in fields] == list(CANONICAL_FIELD_NAMES)
    assert len(fields) == 12
    conflict = fields[CANONICAL_FIELD_NAMES.index("capacity_and_throughput")]
    assert conflict["status"] == "conflicted"
    assert conflict["value"] == [{"speed": "100 rpm"}, {"speed": "200 rpm"}]
    assert len(conflict["sources"]) == 2
    assert all(field["field_name"] != "hallucinated_extra" for field in fields)
    missing = fields[CANONICAL_FIELD_NAMES.index("software_data_and_compliance")]
    assert missing == {
        "field_name": "software_data_and_compliance",
        "value": "unknown", "unit": None, "status": "unknown", "sources": [],
    }


def test_pipeline_rejects_inventory_and_detail_sources_outside_real_locations():
    calls = []

    def source_transport(_key, _endpoint, payload, _timeout):
        system = payload["messages"][0]["content"]
        calls.append(system)
        if "候选清单整理器" in system:
            content = {"candidates": [{
                "model": "X-100",
                "sources": [
                    {"document_id": "doc-source", "page_or_location": "page:1", "excerpt": "X-100"},
                    {"document_id": "doc-source", "page_or_location": "page:99", "excerpt": "invented"},
                ],
            }]}
        else:
            content = {"candidates": [{
                "model": "X-100",
                "sources": [
                    {"document_id": "doc-source", "page_or_location": "page:1", "excerpt": "X-100"},
                    {"document_id": "doc-source", "page_or_location": "page:99", "excerpt": "invented"},
                ],
                "fields": [{
                    "field_name": "capacity_and_throughput", "value": "100 samples/h",
                    "status": "pending_review",
                    "sources": [
                        {"document_id": "doc-source", "page_or_location": "page:1", "excerpt": "100 samples/h"},
                        {"document_id": "doc-source", "page_or_location": "page:99", "excerpt": "invented"},
                    ],
                }],
            }]}
        return {"choices": [{"message": {"content": json.dumps(content)}}]}

    source_document = {
        "id": "doc-source", "source_name": "source.pdf", "tables": [],
        "pages": [{
            "number": 1, "source_location": "page:1", "route": "native_text",
            "text": "Acme X-100 throughput 100 samples/h",
        }],
    }
    client = DeepSeekClient.from_env(
        env={"DEEPSEEK_API_KEY": secrets.token_urlsafe(24)}, transport=source_transport,
    )

    result = DeepSeekAnalysisService(client).analyze(scenario="采购", documents=[source_document])

    candidate = result["candidates"][0]
    assert [source["page_or_location"] for source in candidate["sources"]] == ["page:1"]
    field = next(field for field in candidate["fields"] if field["field_name"] == "capacity_and_throughput")
    assert field["status"] == "pending_review"
    assert [source["page_or_location"] for source in field["sources"]] == ["page:1"]
    assert len(calls) == 2


def _table_column_document():
    """型号只作为表格列出现：正文里没有一处逐字写着 ``SHP-080``。"""
    return {
        "id": "doc-column", "source_name": "manual.pdf", "tables": [],
        "pages": [
            {"number": 1, "source_location": "page:1", "route": "native_text",
             "text": "SHP 系列 生化培养箱 使用说明书"},
            {"number": 2, "source_location": "page:2", "route": "native_text",
             "text": "型号 080 150 250 350 450"},
        ],
    }


def test_pipeline_keeps_word_checked_sources_when_no_page_owns_the_model():
    """归属页为空时，已经逐字校验过的 sources 不许被抹掉（只有证据不成立才清）。"""
    detail_calls = []

    def column_transport(_key, _endpoint, payload, _timeout):
        system = payload["messages"][0]["content"]
        if "候选清单整理器" in system:
            content = {"candidates": [{
                "brand": "Acme", "model": "SHP-080",
                "sources": [
                    {"document_id": "doc-column", "page_or_location": "page:1",
                     "excerpt": "SHP 系列 生化培养箱 使用说明书"},
                    {"document_id": "doc-column", "page_or_location": "page:2",
                     "excerpt": "型号 080 150 250 350 450"},
                ],
            }]}
        else:
            detail_calls.append(system)
            content = {"candidates": [{"model": "SHP-080", "fields": []}]}
        return {"choices": [{"message": {"content": json.dumps(content)}}]}

    client = DeepSeekClient.from_env(
        env={"DEEPSEEK_API_KEY": secrets.token_urlsafe(24)}, transport=column_transport,
    )

    result = DeepSeekAnalysisService(client).analyze(
        scenario="采购", documents=[_table_column_document()],
    )

    candidate = result["candidates"][0]
    assert candidate["model"] == "SHP-080"
    assert [source["page_or_location"] for source in candidate["sources"]] == ["page:1", "page:2"]
    assert any("SHP-080" in gap for gap in result["gaps"])


def test_pipeline_does_not_call_parameter_extraction_without_owned_evidence():
    """归属页为空 → 不发起参数抽取调用（省掉打在空证据上的白花调用）；有归属页的照旧。"""
    detail_prompts = []

    def mixed_transport(_key, _endpoint, payload, _timeout):
        system = payload["messages"][0]["content"]
        if "候选清单整理器" in system:
            content = {"candidates": [
                {
                    "brand": "Acme", "model": "X-100",
                    "sources": [{"document_id": "doc-column", "page_or_location": "page:1",
                                 "excerpt": "Acme X-100 使用说明书"}],
                },
                {
                    "brand": "Acme", "model": "SHP-080",
                    "sources": [{"document_id": "doc-column", "page_or_location": "page:2",
                                 "excerpt": "型号 080 150 250 350 450"}],
                },
            ]}
        else:
            detail_prompts.append(payload["messages"][1]["content"])
            content = {"candidates": [{"model": "X-100", "fields": []}]}
        return {"choices": [{"message": {"content": json.dumps(content)}}]}

    mixed_document = {
        "id": "doc-column", "source_name": "manual.pdf", "tables": [],
        "pages": [
            {"number": 1, "source_location": "page:1", "route": "native_text",
             "text": "Acme X-100 使用说明书"},
            {"number": 2, "source_location": "page:2", "route": "native_text",
             "text": "型号 080 150 250 350 450"},
        ],
    }
    client = DeepSeekClient.from_env(
        env={"DEEPSEEK_API_KEY": secrets.token_urlsafe(24)}, transport=mixed_transport,
    )

    result = DeepSeekAnalysisService(client).analyze(
        scenario="采购", documents=[mixed_document],
    )

    assert len(detail_prompts) == 1
    assert "SHP-080" not in detail_prompts[0]
    assert sorted(candidate["model"] for candidate in result["candidates"]) == ["SHP-080", "X-100"]


def test_pipeline_keeps_existing_sources_when_detail_call_returns_none():
    """详情调用返回空 sources 时，候选已有的（全文校验过的）sources 不许被清空。"""
    detail_calls = []

    def empty_detail_transport(_key, _endpoint, payload, _timeout):
        system = payload["messages"][0]["content"]
        if "候选清单整理器" in system:
            content = {"candidates": [{
                "brand": "Acme", "model": "X-100",
                "sources": [{"document_id": "doc-cover", "page_or_location": "page:1",
                             "excerpt": "Acme 生化培养箱 使用说明书"}],
            }]}
        else:
            detail_calls.append(system)
            content = {"candidates": [{"model": "X-100", "sources": [], "fields": []}]}
        return {"choices": [{"message": {"content": json.dumps(content)}}]}

    cover_document = {
        "id": "doc-cover", "source_name": "cover.pdf", "tables": [],
        "pages": [
            {"number": 1, "source_location": "page:1", "route": "native_text",
             "text": "Acme 生化培养箱 使用说明书"},
            {"number": 2, "source_location": "page:2", "route": "native_text",
             "text": "Acme X-100 throughput 100 samples/h"},
        ],
    }
    client = DeepSeekClient.from_env(
        env={"DEEPSEEK_API_KEY": secrets.token_urlsafe(24)}, transport=empty_detail_transport,
    )

    result = DeepSeekAnalysisService(client).analyze(scenario="采购", documents=[cover_document])

    assert detail_calls, "详情阶段仍应照常发起调用"
    candidate = result["candidates"][0]
    assert candidate["sources"] == [{
        "document_id": "doc-cover", "page_or_location": "page:1",
        "excerpt": "Acme 生化培养箱 使用说明书",
    }]


def test_pipeline_records_each_remaining_candidate_when_call_budget_runs_out():
    """预算断掉后，每个还没做的候选都要有自己的 gaps，不许只留一条批量说明。"""
    models = [f"X-{index}" for index in range(100, 115)]

    def budget_transport(_key, _endpoint, payload, _timeout):
        system = payload["messages"][0]["content"]
        if "候选清单整理器" in system:
            content = {"candidates": [{
                "brand": "Acme", "model": model,
                "sources": [{"document_id": "doc-many", "page_or_location": "page:1",
                             "excerpt": "Acme models " + " ".join(models)}],
            } for model in models]}
        else:
            content = {"candidates": [{"model": "ignored", "fields": []}]}
        return {"choices": [{"message": {"content": json.dumps(content)}}]}

    many_document = {
        "id": "doc-many", "source_name": "many.pdf", "tables": [],
        "pages": [{
            "number": 1, "source_location": "page:1", "route": "native_text",
            "text": "Acme models " + " ".join(models) + " specifications",
        }],
    }
    client = DeepSeekClient.from_env(
        env={"DEEPSEEK_API_KEY": secrets.token_urlsafe(24)}, transport=budget_transport,
    )

    result = DeepSeekAnalysisService(client).analyze(scenario="采购", documents=[many_document])

    assert len(result["candidates"]) == len(models)
    unfinished = [gap for gap in result["gaps"] if "参数抽取未完成" in gap]
    assert len(unfinished) == 4
    assert any("X-111" in gap for gap in unfinished)
    assert any("X-114" in gap for gap in unfinished)


def test_pipeline_rejects_fabricated_excerpt_and_unsupported_field_numbers():
    def evidence_transport(_key, _endpoint, payload, _timeout):
        system = payload["messages"][0]["content"]
        if "候选清单整理器" in system:
            content = {"candidates": [{
                "brand": "Acme", "model": "X-100",
                "sources": [{
                    "document_id": "doc-evidence", "page_or_location": "page:1",
                    "excerpt": "Acme X-100",
                }],
            }]}
        else:
            content = {"candidates": [{
                "model": "X-100",
                "sources": [{
                    "document_id": "doc-evidence", "page_or_location": "page:1",
                    "excerpt": "Acme X-100 fictional flagship",
                }],
                "fields": [
                    {
                        "field_name": "capacity_and_throughput", "value": "100 samples/h",
                        "status": "pending_review", "sources": [{
                            "document_id": "doc-evidence", "page_or_location": "page:1",
                            "excerpt": "throughput 100 samples/h",
                        }],
                    },
                    {
                        "field_name": "performance_range", "value": "999 samples/h",
                        "status": "pending_review", "sources": [{
                            "document_id": "doc-evidence", "page_or_location": "page:1",
                            "excerpt": "throughput 100 samples/h",
                        }],
                    },
                    {
                        "field_name": "core_function_and_applications", "value": "fictional laser mode",
                        "status": "pending_review", "sources": [{
                            "document_id": "doc-evidence", "page_or_location": "page:1",
                            "excerpt": "Acme X-100 fictional laser mode",
                        }],
                    },
                ],
            }]}
        return {"choices": [{"message": {"content": json.dumps(content)}}]}

    evidence_document = {
        "id": "doc-evidence", "source_name": "evidence.pdf", "tables": [],
        "pages": [{
            "number": 1, "source_location": "page:1", "route": "native_text",
            "text": "Acme X-100 throughput 100 samples/h",
        }],
    }
    client = DeepSeekClient.from_env(
        env={"DEEPSEEK_API_KEY": secrets.token_urlsafe(24)}, transport=evidence_transport,
    )

    result = DeepSeekAnalysisService(client).analyze(scenario="采购", documents=[evidence_document])

    candidate = result["candidates"][0]
    assert candidate["sources"] == [{
        "document_id": "doc-evidence", "page_or_location": "page:1", "excerpt": "Acme X-100",
    }]
    fields = {field["field_name"]: field for field in candidate["fields"]}
    assert fields["capacity_and_throughput"]["status"] == "pending_review"
    assert fields["capacity_and_throughput"]["value"] == "100 samples/h"
    performance = fields["performance_range"]
    assert performance["value"] == "unknown"
    assert performance["status"] == "unknown"
    assert performance["sources"] == []
    assert performance["evidence_status"] == "unparsed"
    assert performance["raw_evidence"][0]["excerpt"] == "Acme X-100 throughput 100 samples/h"
    core = fields["core_function_and_applications"]
    assert core["value"] == "unknown"
    assert core["status"] == "unknown"
    assert core["sources"] == []
    assert core["evidence_status"] == "unparsed"
    assert core["raw_evidence"][0]["excerpt"] == "Acme X-100 throughput 100 samples/h"


def test_candidate_page_and_table_text_share_one_bounded_budget():
    pages = [{
        "page": page, "location": f"page:{page}",
        "text": f"Acme X-100 page {page} " + ("p" * 8_000),
        "analysis_input": "parser_text", "visual_fields": {},
    } for page in range(1, 9)]
    rows = [["model", "parameter", "value"]] + [
        ["X-100", f"parameter-{row}", "v" * 500] for row in range(1, 61)
    ]
    prompt_docs = [{
        "document_id": "doc-budget", "source_name": "budget.pdf",
        "analysis_input": "parser_text", "pages": pages,
        "tables": [{"index": 1, "source_location": "page:1/table:1", "rows": rows}],
    }]
    candidate = {
        "brand": "Acme", "model": "X-100",
        "sources": [{
            "document_id": "doc-budget", "page_or_location": "page:1",
            "excerpt": "Acme X-100",
        }],
    }

    payload = deepseek_analysis._candidate_evidence_payload(candidate, prompt_docs)

    selection = payload["evidence_selection"]
    page_characters = sum(
        len(page["text"])
        for doc in payload["documents"] for page in doc["pages"]
    )
    table_characters = sum(
        len("\n".join("\t".join(str(cell) for cell in row) for row in table["rows"]))
        for doc in payload["documents"] for table in doc["tables"]
    )
    assert selection["selected_text_character_count"] <= selection["text_character_budget"] == 48_000
    assert selection["selected_text_character_count"] == page_characters + table_characters
    assert selection["selected_page_text_character_count"] == page_characters
    assert selection["selected_table_text_character_count"] == table_characters
    assert selection["selected_original_table_text_character_count"] > table_characters
    assert selection["omitted_selected_table_text_characters"] > 0
    assert selection["selected_table_count"] == 1
    assert selection["truncated_selected_table_count"] == 1
    selected_table = payload["documents"][0]["tables"][0]
    assert selected_table["text_truncated"] is True
    assert selected_table["selected_row_count"] <= deepseek_analysis._CANDIDATE_EVIDENCE_TABLE_ROW_LIMIT
    assert selected_table["selected_text_characters"] <= deepseek_analysis._CANDIDATE_EVIDENCE_TABLE_CHAR_LIMIT


def test_candidate_field_retrieval_covers_distributed_specs_without_repeated_model_name():
    page_texts = {
        1: "Product model: Acme X-100 laboratory analyzer",
        2: "Welcome to our company. Innovation for every laboratory.",
        3: "Measurement range 0.1 to 1000 units. Accuracy 0.5% and repeatability CV 1%.",
        4: "Corporate milestones and global office locations.",
        5: "外形尺寸 500 x 400 x 300 mm，重量 25 kg，电源 220 V，功率 800 W。",
        6: "Award-winning industrial design and premium materials.",
        7: "Software supports data export through USB interface and network connectivity.",
        8: "Customer stories from laboratories around the world.",
        9: "质保 2 年，交付周期 30 天，提供售后服务和操作培训。",
        10: "Our vision is to make science easier for everyone.",
        11: "Throughput 96 samples per hour with batch capacity of 24 samples.",
        12: "Company address and general contact information.",
        13: "A decorative catalog divider without specifications.",
        14: "Thank you for reading this product brochure.",
    }
    prompt_docs = [{
        "document_id": "doc-distributed", "source_name": "distributed.pdf",
        "analysis_input": "parser_text",
        "pages": [{
            "page": page, "location": f"page:{page}", "text": text,
            "analysis_input": "parser_text", "visual_fields": {},
        } for page, text in page_texts.items()],
        "tables": [{
            "index": 1, "source_location": "page:11/table:1",
            "rows": [["Parameter", "Value"], ["Sample type", "Serum and plasma"]],
        }],
    }]
    candidate = {
        "brand": "Acme", "model": "X-100",
        "sources": [{
            "document_id": "doc-distributed", "page_or_location": "page:1",
            "excerpt": "Acme X-100",
        }],
    }

    payload = deepseek_analysis._candidate_evidence_payload(candidate, prompt_docs)

    selection = payload["evidence_selection"]
    selected_pages = {
        page["page"]
        for document in payload["documents"] for page in document["pages"]
    }
    assert {1, 3, 5, 7, 9, 11}.issubset(selected_pages)
    assert not ({10, 12, 13, 14} & selected_pages)
    assert selection["selected_page_count"] <= 8
    assert selection["selected_text_character_count"] <= 48_000
    assert selection["selected_table_text_character_count"] <= 12_000
    assert selection["retrieval_strategy"] == "canonical_field_top_k_with_identity_anchors"

    expected_fields = {
        "performance_range",
        "accuracy_sensitivity_and_repeatability",
        "capacity_and_throughput",
        "sample_and_format_compatibility",
        "software_data_and_compliance",
        "physical_dimensions_power_and_installation",
        "price_warranty_delivery_and_service",
    }
    assert expected_fields.issubset(set(selection["covered_canonical_fields"]))
    for field_name in expected_fields:
        audit = selection["field_coverage"][field_name]
        assert audit["covered"] is True
        assert audit["selected_evidence_count"] >= 1
        assert 1 <= len(audit["top_k"]) <= 2
        assert all(item["score"] > 0 and item["matched_terms"] for item in audit["top_k"])

    page_audit = {item["page"]: item for item in selection["selected_pages"]}
    for page in (3, 5, 7, 9, 11):
        assert "canonical_field_top_k" in page_audit[page]["selection_reasons"]
        assert page_audit[page]["retrieval_score"] > 0
        assert page_audit[page]["selection_field_scores"]
    assert selection["selected_tables"][0]["field_scores"]["sample_and_format_compatibility"] > 0


def test_deepseek_normalizes_to_review_and_preserves_evidence():
    runtime_key = secrets.token_urlsafe(24)
    client = DeepSeekClient.from_env(env={"DEEPSEEK_API_KEY": runtime_key}, transport=transport)
    result = DeepSeekAnalysisService(client).analyze(scenario="采购检测设备", documents=[document()])
    assert result["status"] == "pending_review"
    field = next(
        field for field in result["candidates"][0]["fields"]
        if field["field_name"] == "price_warranty_delivery_and_service"
    )
    assert field["status"] == "pending_review"
    assert field["sources"][0]["document_id"] == "doc-1"
    assert runtime_key not in json.dumps(result)


def test_missing_key_is_configuration_error():
    with pytest.raises(DeepSeekError) as error:
        DeepSeekClient.from_env(env={})
    assert error.value.code == "provider_not_configured"


def test_missing_source_becomes_unknown():
    def no_source(_key, _endpoint, _payload, _timeout):
        content = {"candidates": [{"model": "X", "fields": [{
            "field_name": "price_warranty_delivery_and_service", "value": 1,
        }]}]}
        return {"choices": [{"message": {"content": json.dumps(content)}}]}
    client = DeepSeekClient.from_env(env={"DEEPSEEK_API_KEY": secrets.token_urlsafe(24)}, transport=no_source)
    result = DeepSeekAnalysisService(client).analyze(scenario="采购", documents=[document()])
    assert result["candidates"][0]["fields"][0]["status"] == "unknown"


def test_api_returns_stable_configuration_error_without_secret(monkeypatch, tmp_path):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        task_id = client.post("/api/v1/tasks", json={"scenario": "采购设备"}).json()["task"]["id"]
        client.post(f"/api/v1/tasks/{task_id}/documents", files={"file": ("spec.txt", "Model X", "text/plain")})
        response = client.post(f"/api/v1/tasks/{task_id}/analysis/deepseek")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "provider_not_configured"
    assert "DEEPSEEK_API_KEY" not in response.text


def test_analysis_read_and_confirmation_gate_unknown_fields(tmp_path):
    runtime_source = {}

    def api_transport(_key, _endpoint, payload, _timeout):
        system = payload["messages"][0]["content"]
        user = payload["messages"][1]["content"]
        if "候选清单整理器" in system:
            batch = json.loads(user.split("有界资料批次整理 json 候选清单：\n", 1)[1])
            unit = batch["evidence_units"][0]
            runtime_source.update(
                document_id=unit["document_id"], page_or_location=unit["location"],
            )
            content = {"candidates": [{
                "brand": "Acme", "model": "X100",
                "sources": [{**runtime_source, "excerpt": "Model X100"}],
            }]}
        else:
            content = {"candidates": [{
                "brand": "Acme", "model": "X100",
                "sources": [{**runtime_source, "excerpt": "Model X100"}],
                "fields": [{
                    "field_name": "price_warranty_delivery_and_service",
                    "value": {"price": 12000}, "unit": "CNY", "status": "pending_review",
                    "sources": [{**runtime_source, "excerpt": "price 12000 CNY"}],
                }],
            }]}
        return {"choices": [{"message": {"content": json.dumps(content)}}]}

    with TestClient(create_app(database_path=tmp_path / "app.sqlite3", storage_dir=tmp_path / "storage")) as client:
        task_id = client.post("/api/v1/tasks", json={"scenario": "采购设备"}).json()["task"]["id"]
        client.post(
            f"/api/v1/tasks/{task_id}/documents",
            files={"file": ("spec.txt", "Model X100 price 12000 CNY", "text/plain")},
        )
        # Inject a deterministic client through the route boundary by setting the
        # runtime key; the test only verifies the review contract, not live HTTP.
        import backend.api.routes as routes
        original = routes.DeepSeekClient.from_env
        routes.DeepSeekClient.from_env = classmethod(lambda cls, **kwargs: original(
            env={"DEEPSEEK_API_KEY": "runtime-test"}, transport=api_transport,
        ))
        try:
            generated = client.post(f"/api/v1/tasks/{task_id}/analysis/deepseek")
        finally:
            routes.DeepSeekClient.from_env = original
        assert generated.status_code == 200
        analysis = generated.json()
        assert client.get(f"/api/v1/tasks/{task_id}/analysis/deepseek").json()["analysis_id"] == analysis["analysis_id"]
        candidate = analysis["candidates"][0]
        field_statuses = {name: "rejected" for name in CANONICAL_FIELD_NAMES}
        field_statuses["price_warranty_delivery_and_service"] = "confirmed"
        decision = client.post(f"/api/v1/tasks/{task_id}/analysis/deepseek/decisions", json=[{
            "analysis_id": analysis["analysis_id"], "candidate_id": candidate["candidate_id"], "status": "confirmed",
            "field_statuses": field_statuses,
        }])
        assert decision.status_code == 200
        assert client.get(f"/api/v1/tasks/{task_id}/candidates/confirmed").json()["items"][0]["id"] == candidate["candidate_id"]
        facts = client.get(f"/api/v1/tasks/{task_id}/facts").json()["items"]
        assert len(facts) == 1
        assert facts[0]["field_name"] == "price_warranty_delivery_and_service"
        assert facts[0]["status"] == "confirmed"
        assert facts[0]["evidence"][0]["document_id"] == runtime_source["document_id"]
        assert facts[0]["evidence"][0]["page"] == 1


def test_latest_analysis_read_uses_created_at_not_random_analysis_id(tmp_path):
    app = create_app(database_path=tmp_path / "latest.sqlite3", storage_dir=tmp_path / "storage")
    with TestClient(app) as client:
        task_id = client.post("/api/v1/tasks", json={"scenario": "采购设备"}).json()["task"]["id"]
        app.state.repository.put("reports", "analysis-z-old", {
            "kind": "deepseek_analysis", "analysis_id": "analysis-z-old",
            "created_at": "2026-09-19T01:00:00+00:00", "status": "pending_review",
            "candidates": [{"candidate_id": "old", "model": "OLD", "fields": []}],
        }, task_id=task_id)
        app.state.repository.put("reports", "analysis-a-new", {
            "kind": "deepseek_analysis", "analysis_id": "analysis-a-new",
            "created_at": "2026-09-19T02:00:00+00:00", "status": "pending_review",
            "candidates": [{"candidate_id": "new", "model": "NEW", "fields": []}],
        }, task_id=task_id)

        latest = client.get(f"/api/v1/tasks/{task_id}/analysis/deepseek")

    assert latest.status_code == 200
    assert latest.json()["analysis_id"] == "analysis-a-new"


@pytest.mark.parametrize(("audit_fields", "expected_reviewer", "expected_note"), [
    ({}, "analysis-decision:unspecified", None),
    ({"reviewed_by": "reviewer-7", "note": "逐页核对原始资料"}, "reviewer-7", "逐页核对原始资料"),
])
def test_analysis_decision_records_caller_reviewer_without_impersonating_user(
    tmp_path, audit_fields, expected_reviewer, expected_note,
):
    with TestClient(create_app(database_path=tmp_path / "audit.sqlite3", storage_dir=tmp_path / "storage")) as client:
        task_id = client.post("/api/v1/tasks", json={"scenario": "采购设备"}).json()["task"]["id"]
        analysis_id = "analysis-audit"
        candidate_id = "candidate-audit"
        client.app.state.repository.put("reports", analysis_id, {
            "kind": "deepseek_analysis", "analysis_id": analysis_id, "status": "pending_review",
            "candidates": [{
                "candidate_id": candidate_id, "model": "X100", "status": "pending_review",
                "sources": [{"document_id": "doc-audit", "page_or_location": "page:3", "excerpt": "X100"}],
                "fields": [{
                    "field_name": "price_warranty_delivery_and_service",
                    "value": {"price": 12000}, "unit": "CNY", "status": "pending_review",
                    "sources": [{"document_id": "doc-audit", "page_or_location": "page:3", "excerpt": "12000 CNY"}],
                }],
            }],
        }, task_id=task_id)

        response = client.post(f"/api/v1/tasks/{task_id}/analysis/deepseek/decisions", json=[{
            "analysis_id": analysis_id, "candidate_id": candidate_id, "status": "confirmed",
            "field_statuses": {"price_warranty_delivery_and_service": "confirmed"},
            **audit_fields,
        }])

        assert response.status_code == 200
        item = response.json()["items"][0]
        assert item["reviewed_by"] == expected_reviewer
        assert item["fields"][0]["reviewed_by"] == expected_reviewer
        candidate = client.app.state.repository.get("candidates", candidate_id)
        assert candidate["reviewed_by"] == expected_reviewer
        fact = client.get(f"/api/v1/tasks/{task_id}/facts").json()["items"][0]
        assert fact["reviewed_by"] == expected_reviewer
        assert fact["note"] == expected_note
        assert fact["reviewed_by"] != "current-user"
