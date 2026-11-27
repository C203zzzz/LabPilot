"""二次参数抽取的空配置和空参数保护。"""

from __future__ import annotations

import json
import secrets

from backend.infrastructure.deepseek import DeepSeekClient
from backend.services.deepseek_analysis import (
    CANONICAL_FIELD_NAMES,
    CANDIDATE_INVENTORY_PROMPT,
    PARAMETER_FALLBACK_PROMPT,
    VISUAL_DETAIL_PROMPT,
    DeepSeekAnalysisService,
    _deterministic_parameter_fields,
)


BODY = "Acme X-100 技术参数：通量 100 samples/h；配置 96-well。"


def _document():
    return {
        "id": "doc-fallback",
        "source_name": "fallback.txt",
        "pages": [{
            "number": 1,
            "source_location": "page:1",
            "route": "native_text",
            "text": BODY,
        }],
        "tables": [],
    }


def _field():
    return {
        "field_name": "capacity_and_throughput",
        "value": "100 samples/h",
        "unit": "samples/h",
        "status": "pending_review",
        "sources": [{
            "document_id": "doc-fallback",
            "page_or_location": "page:1",
            "excerpt": "通量 100 samples/h",
        }],
    }


def _client(inventory, detail, fallback, observed):
    def transport(_key, _endpoint, payload, _timeout):
        system = payload["messages"][0]["content"]
        observed.append(payload)
        if system == CANDIDATE_INVENTORY_PROMPT:
            content = {"candidates": [inventory]}
        elif system == PARAMETER_FALLBACK_PROMPT:
            content = fallback
        else:
            content = detail
        return {"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(content, ensure_ascii=False),
        }}]}

    return DeepSeekClient.from_env(
        env={"DEEPSEEK_API_KEY": secrets.token_urlsafe(24)},
        transport=transport,
    )


def test_empty_configuration_reuses_read_body_for_specialized_parameter_retry():
    observed = []
    client = _client(
        {
            "brand": "Acme",
            "model": "X-100",
            "configuration": None,
            "sources": [{
                "document_id": "doc-fallback",
                "page_or_location": "page:1",
                "excerpt": "Acme X-100 技术参数",
            }],
        },
        {"candidates": [{"model": "X-100", "fields": []}]},
        {"candidates": [{
            "model": "X-100",
            "configuration": "96-well",
            "fields": [_field()],
        }]},
        observed,
    )

    result = DeepSeekAnalysisService(client).analyze(
        scenario="采购检测设备", documents=[_document()],
    )

    candidate = result["candidates"][0]
    assert len(observed) == 3
    assert observed[2]["messages"][0]["content"] == PARAMETER_FALLBACK_PROMPT
    assert BODY in observed[2]["messages"][1]["content"]
    assert candidate["configuration"] == "96-well"
    assert candidate["parameters_status"] == "extracted"
    assert any(field["value"] == "100 samples/h" for field in candidate["fields"])
    assert not result["failures"]
    assert not any("本轮没有抽出可比参数" in gap for gap in result["gaps"])


def test_zero_first_parameters_reuses_read_body_even_when_configuration_exists():
    observed = []
    client = _client(
        {
            "brand": "Acme",
            "model": "X-100",
            "configuration": "初始配置",
            "sources": [{
                "document_id": "doc-fallback",
                "page_or_location": "page:1",
                "excerpt": "Acme X-100 技术参数",
            }],
        },
        {"candidates": [{"model": "X-100", "fields": []}]},
        {"candidates": [{"model": "X-100", "fields": [_field()]}]},
        observed,
    )

    result = DeepSeekAnalysisService(client).analyze(
        scenario="采购检测设备", documents=[_document()],
    )

    assert len(observed) == 3
    assert result["candidates"][0]["parameters_status"] == "extracted"
    assert any(field["value"] == "100 samples/h" for field in result["candidates"][0]["fields"])


def test_prose_only_first_result_retries_even_when_fields_are_nonempty():
    """说明文字和来源齐全，也不能冒充已经抽到可比较参数。"""
    observed = []
    client = _client(
        {
            "brand": "Acme",
            "model": "X-100",
            "configuration": "三孔电热恒温水槽",
            "sources": [{
                "document_id": "doc-fallback",
                "page_or_location": "page:1",
                "excerpt": "Acme X-100 技术参数",
            }],
        },
        {"candidates": [{"model": "X-100", "fields": [{
            "field_name": "temperature_environment_and_safety",
            "value": "三孔水槽各自单独加热并分别控温",
            "status": "pending_review",
            "sources": [{"document_id": "doc-fallback", "page_or_location": "page:1",
                         "excerpt": "三孔水槽各自单独加热并分别控温"}],
        }]}]},
        {"candidates": [{"model": "X-100", "configuration": "96-well",
                         "fields": [_field()]}]},
        observed,
    )

    result = DeepSeekAnalysisService(client).analyze(
        scenario="采购恒温水浴锅", documents=[_document()],
    )

    assert len(observed) == 3
    assert observed[2]["messages"][0]["content"] == PARAMETER_FALLBACK_PROMPT
    assert result["candidates"][0]["configuration"] == "96-well"
    assert result["candidates"][0]["parameters_status"] == "extracted"


def test_second_parameter_attempt_keeps_raw_empty_response_and_user_gap():
    observed = []
    client = _client(
        {
            "brand": "Acme",
            "model": "X-100",
            "configuration": None,
            "sources": [{
                "document_id": "doc-fallback",
                "page_or_location": "page:1",
                "excerpt": "Acme X-100 技术参数",
            }],
        },
        {"candidates": [{"model": "X-100", "fields": []}]},
        {
            "candidates": [],
            "raw_debug": {"reason": "MODEL_NO_CONFIGURATION", "api_key": "secret-token"},
        },
        observed,
    )

    result = DeepSeekAnalysisService(client).analyze(
        scenario="采购检测设备", documents=[_document()],
    )

    candidate = result["candidates"][0]
    assert len(observed) == 3
    assert candidate["parameters_status"] == "fallback_empty"
    assert all(field["field_name"] in CANONICAL_FIELD_NAMES for field in candidate["fields"])
    assert all(field["status"] == "unknown" for field in candidate["fields"])
    gap = next(gap for gap in result["gaps"] if "X-100" in gap)
    assert "资料正文已读到，但本轮没有抽出可比参数" in gap
    assert "资料里没有可用的参数数值" not in gap

    failure = next(item for item in result["failures"] if item["code"] == "parameter_fallback_empty")
    assert failure["type"] == "ModelReturnedMissingStructuredConfiguration"
    assert failure["location"]["file"] == "deepseek_analysis.py"
    assert failure["raw_response_type"] == "dict"
    assert "MODEL_NO_CONFIGURATION" in failure["raw_response"]
    assert "secret-token" not in json.dumps(failure, ensure_ascii=False)


def test_deterministic_fallback_extracts_waterbath_units_and_verbatim_sources():
    documents = [{
        "document_id": "doc-waterbath",
        "pages": [{
            "location": "page:6",
            "text": (
                "目录：技术指标 - 3\n"
                "2.1 电源电压：～(220±22) V (50±1)Hz；\n"
                "2.2 输入功率：750W（250W×3）；\n"
                "2.3 控温范围：（室温+5）℃～99℃；\n"
                "2.6 工作室尺寸（mm）：150×125×110\n"
                "外形尺寸（mm）：490×245×310\n"
            ),
        }],
        "tables": [],
    }]

    fields = _deterministic_parameter_fields(documents)

    assert len(fields) == 5
    values = {field["value"] for field in fields}
    assert {"～(220±22) V (50±1)Hz", "750W（250W×3）",
            "（室温+5）℃～99℃", "150×125×110", "490×245×310"} <= values
    assert all(field["sources"][0]["excerpt"] in documents[0]["pages"][0]["text"]
               for field in fields)
    assert all(field["status"] == "pending_review" for field in fields)


def test_deterministic_fallback_does_not_cross_model_blocks_on_one_page():
    documents = [{
        "document_id": "doc-multi-model",
        "pages": [{
            "location": "page:1",
            "text": (
                "A10\n"
                "输入功率：100W\n"
                "A20\n"
                "输入功率：200W\n"
            ),
        }],
        "tables": [],
    }]

    fields = _deterministic_parameter_fields(
        documents, model="A10", other_models=["A20"],
    )

    assert [field["value"] for field in fields] == ["100W"]


def test_service_keeps_same_page_models_in_separate_deterministic_fallback_blocks():
    document = {
        "id": "doc-multi-model",
        "source_name": "multi-model.pdf",
        "pages": [{
            "number": 1,
            "source_location": "page:1",
            "route": "native_text",
            "text": "A10\n输入功率：100W\nA20\n输入功率：200W\n",
        }],
        "tables": [],
    }

    def transport(_key, _endpoint, payload, _timeout):
        system = payload["messages"][0]["content"]
        user_prompt = payload["messages"][1]["content"]
        if system == CANDIDATE_INVENTORY_PROMPT:
            content = {"candidates": [
                {"brand": "Acme", "model": "A10", "sources": [{
                    "document_id": "doc-multi-model", "page_or_location": "page:1",
                    "excerpt": "A10",
                }]},
                {"brand": "Acme", "model": "A20", "sources": [{
                    "document_id": "doc-multi-model", "page_or_location": "page:1",
                    "excerpt": "A20",
                }]},
            ]}
        elif system == VISUAL_DETAIL_PROMPT:
            model = "A10" if '"model":"A10"' in user_prompt else "A20"
            content = {"candidates": [{"model": model, "fields": []}]}
        else:
            content = {"candidates": []}
        return {"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(content, ensure_ascii=False),
        }}]}

    client = DeepSeekClient.from_env(
        env={"DEEPSEEK_API_KEY": secrets.token_urlsafe(24)},
        transport=transport,
    )

    result = DeepSeekAnalysisService(client).analyze(
        scenario="采购电热设备", documents=[document],
    )

    by_model = {candidate["model"]: candidate for candidate in result["candidates"]}
    assert [field["value"] for field in by_model["A10"]["fields"]
            if field.get("value") != "unknown"] == ["100W"]
    assert [field["value"] for field in by_model["A20"]["fields"]
            if field.get("value") != "unknown"] == ["200W"]
    assert all(candidate["parameters_status"] == "extracted"
               for candidate in by_model.values())


def test_model_explanation_only_uses_deterministic_body_fallback():
    observed = []
    client = _client(
        {
            "brand": "上海一恒",
            "model": "DK-8D",
            "configuration": None,
            "sources": [{
                "document_id": "doc-waterbath",
                "page_or_location": "page:6",
                "excerpt": "DK-8D 输入功率：750W（250W×3）",
            }],
        },
        {"candidates": [{"model": "DK-8D", "fields": [{
            "field_name": "temperature_environment_and_safety",
            "value": "三孔水槽采用智能温控仪",
            "unit": None,
            "status": "pending_review",
            "sources": [{"document_id": "doc-waterbath", "page_or_location": "page:6",
                         "excerpt": "三孔水槽采用智能温控仪"}],
        }]}]},
        {"candidates": []},
        observed,
    )
    document = {
        "id": "doc-waterbath",
        "source_name": "waterbath.pdf",
        "pages": [{
            "number": 6,
            "source_location": "page:6",
            "route": "native_text",
                "text": "DK-8D 输入功率：750W（250W×3）；",
        }],
        "tables": [],
    }

    result = DeepSeekAnalysisService(client).analyze(
        scenario="采购恒温水浴锅", documents=[document],
    )

    candidate = result["candidates"][0]
    assert candidate["parameters_status"] == "extracted"
    assert any(field["value"] == "750W（250W×3）" for field in candidate["fields"])
    assert candidate["parameters_origin"]["kind"] == "deterministic_text_fallback"
    assert any("二次参数抽取未返回结构化参数" in gap for gap in result["gaps"])
    failure = next(item for item in result["failures"]
                   if item["code"] == "parameter_fallback_empty")
    assert failure["recovered_by"] == "deterministic_text_fallback"
