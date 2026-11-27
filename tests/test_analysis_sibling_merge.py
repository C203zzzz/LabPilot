"""无归属候选沿**同编号兄弟候选**归并取数，以及"未取到"与"确认无参数"的分开。

背景（``output/analysis-jitter/report.md``）：sample-c 那份扫描件里，表格列名是**裸编号
``080``**，``SHP`` 只出现在封面的系列名里，全篇没有 ``SHP-080`` 这个串。清单环节的模型
有时把候选命名成 ``080``（``scoped_documents`` 命中 page 2 表头，抽到真字段），有时命名成
``SHP-080``（全篇零关联，被跳过 → ``fields`` 全 unknown）。基准候选是 ``SHP-080``，于是
基准参数在 0/3 之间抖、模块判定跟着翻转。

这里钉住三件事：
  1. 自己一个字都关联不到、但存在**同编号**兄弟候选时，参数沿兄弟候选取回来，并两侧留痕；
  2. 编号不同／前缀不相容的候选**不许**归并；
  3. 能关联到正文的候选行为一字不变（取数走原路、不多花调用），而"未取到"与"确认无参数"
     在 ``parameters_status`` 与 ``gaps`` 上必须不同。
"""
import json
import re
import secrets

from backend.infrastructure.deepseek import DeepSeekClient
from backend.services.deepseek_analysis import DeepSeekAnalysisService

COVER = "SHP 系列 生化培养箱 使用说明书"
SPECS = ["型号：080 150 250 350 450", "控制范围 5060℃", "消耗功率 600W"]
SIBLING_FIELDS = [
    {"field_name": "performance_range", "value": {"控制范围": "5060℃"},
     "status": "pending_review",
     "sources": [{"document_id": "doc-column", "page_or_location": "page:2",
                  "excerpt": "控制范围 5060℃"}]},
    {"field_name": "physical_dimensions_power_and_installation", "value": {"消耗功率": "600W"},
     "status": "pending_review",
     "sources": [{"document_id": "doc-column", "page_or_location": "page:2",
                  "excerpt": "消耗功率 600W"}]},
]


def _document(page_two="\n".join(SPECS), page_five="装箱单 SHP-450 15A"):
    return {
        "id": "doc-column", "source_name": "sample-c.pdf", "tables": [],
        "pages": [
            {"number": 1, "source_location": "page:1", "route": "native_text", "text": COVER},
            {"number": 2, "source_location": "page:2", "route": "native_text", "text": page_two},
            {"number": 5, "source_location": "page:5", "route": "native_text", "text": page_five},
        ],
    }


def _client(inventory, calls):
    """假模型：清单原样返回；详情只对"证据里真有那几个指标"的候选答出参数。"""

    def transport(_key, _endpoint, payload, _timeout):
        system = payload["messages"][0]["content"]
        if "候选清单整理器" in system:
            content = {"candidates": inventory}
        elif "字段补漏器" in system:
            content = {"fields": []}
        else:
            prompt = payload["messages"][1]["content"]
            calls.append(prompt)
            match = re.search(r'"model"\s*:\s*"([^"]*)"',
                              prompt.split("候选级有界证据")[0])
            model = match.group(1) if match else ""
            fields = SIBLING_FIELDS if "消耗功率 600W" in prompt else []
            content = {"candidates": [{"model": model, "fields": fields}]}
        return {"choices": [{"finish_reason": "stop",
                             "message": {"content": json.dumps(content)}}]}

    return DeepSeekClient.from_env(
        env={"DEEPSEEK_API_KEY": secrets.token_urlsafe(24)}, transport=transport,
    )


def _inventory(*entries):
    """``entries`` 形如 ``("SHP-080", "page:2")``：型号 + 清单引用的页码。"""
    return [{"brand": "Acme", "model": model,
             "sources": [{"document_id": "doc-column", "page_or_location": page,
                          "excerpt": COVER if page == "page:1" else SPECS[0]}]}
            for model, page in entries]


def _analyze(inventory, document=None):
    calls = []
    result = DeepSeekAnalysisService(_client(inventory, calls)).analyze(
        scenario="采购一台恒温培养箱", documents=[document or _document()],
    )
    return result, calls


def _by_model(result):
    return {candidate["model"]: candidate for candidate in result["candidates"]}


def _failures(result, code):
    return [failure for failure in result["failures"] if failure.get("code") == code]


def test_unowned_candidate_takes_parameters_from_the_same_number_sibling():
    result, calls = _analyze(_inventory(("SHP-080", "page:2"), ("080", "page:2")))

    # 只给剥编号那条候选发起参数抽取：不往空证据上白花调用（归并是抄字段，不是新调用）。
    assert len(calls) == 2
    candidate = _by_model(result)["SHP-080"]

    # 参数真的救回来了——这正是"基准 0 条"的翻转来源。
    assert candidate["parameters_status"] == "extracted"
    assert candidate["parameters_origin"] == {
        "kind": "sibling_candidate", "model": "080", "number": "080",
    }
    values = [field["value"] for field in candidate["fields"] if field["status"] != "unknown"]
    assert {"控制范围": "5060℃"} in values
    assert {"消耗功率": "600W"} in values

    # 用户侧说人话、不带异常类名；排查侧带类名与原文。
    gap = next(g for g in result["gaps"] if "SHP-080" in g)
    assert "参数取自同编号的另一条候选 080" in gap and "编号 080" in gap
    assert "Error" not in gap and "SiblingCandidateMerge" not in gap
    failure = _failures(result, "sibling_candidate_merge")
    assert len(failure) == 1
    assert failure[0]["type"] == "SiblingCandidateMerge"
    assert failure[0]["models"] == ["SHP-080", "080"]
    assert failure[0]["merged_from"] == "080" and failure[0]["number"] == "080"
    assert "消耗功率 600W" in failure[0]["excerpt"]      # 脱敏后的原文，能回到 page 2
    assert failure[0]["location"]["file"] == "deepseek_analysis.py"


def test_owned_candidate_is_never_touched_by_the_merge():
    """能关联到正文的候选走原路：自己的字段、不算归并、origin 为空、不多花调用。"""
    result, calls = _analyze(
        _inventory(("SHP-080", "page:2"), ("080", "page:2"), ("SHP-450", "page:5")),
    )

    # 只有两条有归属的候选发起参数抽取（080 与 SHP-450）；SHP-080 不额外调用。
    assert len(calls) == 4
    owned = _by_model(result)["SHP-450"]
    assert owned["parameters_origin"] is None
    assert owned["parameters_status"] == "fallback_empty"
    assert all("SHP-450" not in failure["models"]
               for failure in _failures(result, "sibling_candidate_merge"))
    assert _by_model(result)["080"]["parameters_status"] == "extracted"


def test_a_different_number_is_never_merged():
    """编号不同不许归并：``SHP-150`` 身边只有编号 ``080`` 的兄弟候选。"""
    result, _calls = _analyze(_inventory(("SHP-150", "page:2"), ("080", "page:2")))

    candidate = _by_model(result)["SHP-150"]
    assert candidate["parameters_status"] == "not_located"
    assert candidate["parameters_origin"] is None
    assert all(field["status"] == "unknown" for field in candidate["fields"])
    assert _by_model(result)["080"]["parameters_status"] == "extracted"

    gap = next(g for g in result["gaps"] if "SHP-150" in g)
    assert "未做参数抽取" in gap and "资料里没有" not in gap
    failure = _failures(result, "no_owned_evidence")
    assert [item["models"] for item in failure] == [["SHP-150"]]
    assert failure[0]["type"] == "ModelWithoutOwnedEvidence"
    assert "SHP-150" in failure[0]["error"]
    assert failure[0]["excerpt"]                          # 候选自己引用的原文
    assert not _failures(result, "sibling_candidate_merge")


def test_an_incompatible_prefix_is_never_merged():
    """前缀不同且都不为空 → 不是同一台机器的两种写法，不归并（两边都有参数可抄也不抄）。"""
    document = _document(page_two="型号：SHP-080 SHP-150\n控制范围 5060℃\n消耗功率 600W")
    result, _calls = _analyze(
        _inventory(("DHP-080", "page:2"), ("SHP-080", "page:2")), document=document,
    )

    assert _by_model(result)["SHP-080"]["parameters_status"] == "extracted"
    candidate = _by_model(result)["DHP-080"]
    assert candidate["parameters_status"] == "not_located"
    assert candidate["parameters_origin"] is None
    assert not _failures(result, "sibling_candidate_merge")


def test_a_model_with_a_trailing_letter_has_no_number_to_share():
    """``080A`` 结尾不是数字（是另一个型号），不参与归并。"""
    result, _calls = _analyze(_inventory(("080A", "page:2"), ("080", "page:2")))

    candidate = _by_model(result)["080A"]
    assert candidate["parameters_status"] == "not_located"
    assert candidate["parameters_origin"] is None
    assert not _failures(result, "sibling_candidate_merge")


def test_not_located_and_not_available_in_source_are_two_different_facts():
    """"未取到"与"确认无参数"必须能分开：状态不同、对外那句话不同、只有前者留 failures。"""
    missing_result, _calls = _analyze(_inventory(("SHP-150", "page:2"), ("080", "page:2")))
    assert _by_model(missing_result)["SHP-150"]["parameters_status"] == "not_located"
    missing_gap = next(g for g in missing_result["gaps"] if "SHP-150" in g)
    assert _failures(missing_result, "no_owned_evidence")

    # 有归属、但正文里确实没有可用参数 → 另一种事实（不是未取到），也不进 failures。
    absent_document = _document(page_two="型号：X-100\n本机小巧，适合台面使用。")
    absent_result, calls = _analyze(_inventory(("X-100", "page:2")), document=absent_document)
    assert calls, "有归属就该照常发起参数抽取"
    absent = _by_model(absent_result)["X-100"]
    assert absent["parameters_status"] == "fallback_empty"
    assert absent["parameters_origin"] is None
    absent_gap = next(g for g in absent_result["gaps"] if "X-100" in g)
    assert "资料正文已读到，但本轮没有抽出可比参数" in absent_gap
    assert not _failures(absent_result, "no_owned_evidence")
    assert _failures(absent_result, "parameter_fallback_empty")

    # 两句话是两种事实，不能互换。
    assert missing_gap.replace("SHP-150", "") != absent_gap.replace("X-100", "")
    assert "未做参数抽取" in missing_gap
    assert "未做参数抽取" not in absent_gap
