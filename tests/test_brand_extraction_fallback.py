"""正文厂名兜底：资料写明厂名时候选带品牌，没写就保持为空。

根因（``sample-d``，紫外可见分光光度计 UV1100 的 111 页文字层 PDF）：厂名印在
**最后一页联系方式**的独立行里（``上海天美科学 仪器 有限公司``），而那一页不含
型号，``scoped_documents`` 不会把它算进候选级证据——模型看不到它，候选清单的
``brand`` 于是是 null，候选发现没有可要求"品牌一致"的对象，整条车道查不到东西。

对照（``sample-b``，数码超声波清洗机 CE-5600A 使用说明书）：正文里**根本没有
厂名**，只有 ``使用厂家未曾建议的配件`` 这种普通词，这份的 ``brand: null`` 是
正确的——兜底不许给它凭空造一个品牌。

素材是真实样本的正文片段，不改写、不合成。
"""
import json
import secrets

from backend.infrastructure.deepseek import DeepSeekClient
from backend.services.deepseek_analysis import CANONICAL_FIELD_NAMES, DeepSeekAnalysisService
from backend.services.procurement_rules import (
    brand_from_enterprise_name,
    brand_identity,
    brand_name_matches,
    enterprise_names_in_text,
)


#: sample-d 第 111 页（最后一页）原文片段：厂名与地址、邮编、电话同块。
SAMPLE_D_LAST_PAGE = (
    " 111 \n \n亲爱的顾客：  \n"
    "如果您在仪器的使用过程中遇到问题需要我们提供协助，请与我\n们联系。  \n \n \n"
    "上海天美科学 仪器 有限公司  \n"
    "上海市闵行区春东 路 555 号  \n"
    "邮编： 201108 \n"
    "电 话： 021-54422858 \n"
    "传真： 021-34073265 \n \n \n "
)

#: sample-b 正文片段（使用说明书开头 + 唯一含"厂家"两字的那句）。
SAMPLE_B_BODY = (
    "数码超声波清洗机 数码超声波清洗机\n"
    "CE-5600A CE-5600A\n"
    "使用说明书 使用说明书\n"
    "每秒42，000次的超声波，只需在不锈钢 电子高频振荡线路，经能量转换器产生 "
    "槽中加入普通的水，将需清洗的物品放 看到污垢离开物品进入水中。\n"
    "《使用说明书》正确使用产品，切勿私自使用配件或 使用厂家未曾建议的配件。\n"
    "在清洗过程中，不牢固的镶嵌及固 定件可能会松动，请及时检查。\n"
    "此机相比其他小型超声波清洗机而言， 最大的亮点是带【消气】功能。\n"
)


def test_contact_page_yields_the_registered_maker():
    assert enterprise_names_in_text(SAMPLE_D_LAST_PAGE) == ["上海天美科学仪器有限公司"]


def test_manual_without_a_maker_yields_nothing():
    # 反例：正文里没有厂名（只有"使用厂家"这种普通词），不许造品牌。
    assert enterprise_names_in_text(SAMPLE_B_BODY) == []
    assert "厂家" in SAMPLE_B_BODY


def test_a_sentence_glued_to_the_name_is_trimmed():
    # PDF 文字层常把厂名接在句子后面（没有空格），地名用来把句子裁掉。
    assert enterprise_names_in_text("更多信息请访问上海天美科学仪器有限公司官网") == [
        "上海天美科学仪器有限公司"
    ]


def test_an_address_page_still_yields_exactly_one_name():
    assert enterprise_names_in_text(
        "地址：上海市闵行区春东路555号\n上海天美科学仪器有限公司\n邮编：201108"
    ) == ["上海天美科学仪器有限公司"]


def test_a_bare_company_word_is_not_a_name():
    # 反例：裸的“公司/厂”不当锚点。
    assert enterprise_names_in_text("请与本公司联系，或联系厂家售后") == []


def test_registered_name_keeps_its_spelling_and_aliases_are_canonicalized():
    assert brand_from_enterprise_name("上海天美科学仪器有限公司") == "上海天美科学仪器有限公司"
    # 别名表里有的写法归到规范写法：与候选发现用的是同一个 key。
    assert brand_from_enterprise_name("苏州安泰空气技术有限公司") == "苏净安泰"
    assert brand_from_enterprise_name("苏州安泰空气技术有限公司") == brand_identity(
        "苏州安泰空气技术有限公司"
    )


def test_brand_agreement_gate_is_not_widened():
    # 兜底填的是登记名；能不能与网页上的字号对上，仍由既有判据决定。
    assert brand_name_matches("上海天美科学仪器有限公司", "天美") is True
    # 两家不同实体必须继续判成不匹配。
    assert brand_name_matches("上海精宏实验技术有限公司", "上海天美科学仪器有限公司") is False


def _client(transport):
    return DeepSeekClient.from_env(
        env={"DEEPSEEK_API_KEY": secrets.token_urlsafe(24)}, transport=transport,
    )


def _transport(*, inventory_brand, detail_prompts, model="UV1100", document_id="doc-uv1100"):
    """候选清单返回 ``inventory_brand``；候选详情的 user prompt 记进列表。"""

    def transport(_key, _endpoint, payload, _timeout):
        system = payload["messages"][0]["content"]
        if "候选清单整理器" in system:
            content = {"candidates": [{
                "brand": inventory_brand,
                "model": model,
                "sources": [{
                    "document_id": document_id, "page_or_location": "page:1", "excerpt": model,
                }],
            }]}
        elif "字段补漏器" in system:
            content = {"fields": []}
        else:
            detail_prompts.append(payload["messages"][1]["content"].split(
                "候选级有界证据 JSON：\n", 1,
            )[-1])
            content = {"candidates": [{"model": model, "fields": [
                {"field_name": name, "value": "unknown", "status": "unknown", "sources": []}
                for name in CANONICAL_FIELD_NAMES
            ]}]}
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}]}

    return transport


def _sample_d_document():
    """正文含型号的页 + 只有厂名的最后一页（那一页没有型号，正是缺陷的形态）。"""
    return {
        "id": "doc-uv1100", "source_name": "UV1100.pdf",
        "pages": [
            {"number": 1, "source_location": "page:1", "text": "UV1100 紫外可见分光光度计 使用说明书"},
            {"number": 111, "source_location": "page:111", "text": SAMPLE_D_LAST_PAGE},
        ],
        "tables": [],
    }


def test_brand_is_filled_from_a_page_the_model_never_saw():
    detail_prompts = []
    client = _client(_transport(inventory_brand=None, detail_prompts=detail_prompts))

    result = DeepSeekAnalysisService(client).analyze(
        scenario="采购一台紫外可见分光光度计", documents=[_sample_d_document()],
    )

    assert result["candidates"][0]["brand"] == "上海天美科学仪器有限公司"
    # 关键前提：模型真的没看到厂名那一页——兜底不是"模型已经看到"。
    assert detail_prompts
    assert "天美" not in detail_prompts[0]


def test_brand_stays_null_when_the_body_names_no_maker():
    detail_prompts = []
    client = _client(_transport(inventory_brand=None, detail_prompts=detail_prompts,
                                model="CE-5600A", document_id="doc-ce5600a"))
    document = {
        "id": "doc-ce5600a", "source_name": "CE-5600A.pdf",
        "pages": [{"number": 1, "source_location": "page:1",
                   "text": "CE-5600A 数码超声波清洗机 使用说明书\n" + SAMPLE_B_BODY}],
        "tables": [],
    }

    result = DeepSeekAnalysisService(client).analyze(
        scenario="采购一台数码超声波清洗机", documents=[document],
    )

    # 品牌仍然是空：资料里没有厂名，不许猜（这一条没变）。
    assert result["candidates"][0]["brand"] is None
    # 但"品牌没读出来"这件事必须留痕：它是资料的事实，却决定了下游能不能判
    # 跨品牌相似品（``docs/209`` §3.4：静默跳过等于把事实抹掉）。
    gap = next(g for g in result["gaps"] if "厂名" in g)
    assert "CE-5600A" in gap and "跨品牌相似品" in gap
    # 给用户看的那句是人话：不带异常类名、不带内部代号。
    assert "Error" not in gap and "brand_unresolved" not in gap
    failure = next(f for f in result["failures"] if f["code"] == "brand_unresolved")
    assert failure["stage"] == "brand_extraction"
    assert failure["models"] == ["CE-5600A"]
    assert failure["location"]["function"] and failure["location"]["file"] == "deepseek_analysis.py"
    assert "厂名" in failure["error"] and "CE-5600A" in failure["error"]
    assert failure["excerpt"]                      # 脱敏后的原文片段，能回到那几页
    # 不是失败、不阻断：候选照常进入待复核。
    assert result["candidates"][0]["status"] == "pending_review"


def test_a_document_that_names_its_maker_leaves_no_brand_gap():
    detail_prompts = []
    client = _client(_transport(inventory_brand=None, detail_prompts=detail_prompts))

    result = DeepSeekAnalysisService(client).analyze(
        scenario="采购一台紫外可见分光光度计", documents=[_sample_d_document()],
    )

    assert result["candidates"][0]["brand"] == "上海天美科学仪器有限公司"
    assert not any("资料里没有厂名" in gap for gap in result["gaps"])
    assert not result["failures"]


def _multi_transport(inventory):
    """候选清单返回 ``inventory``（多条）；详情返回全 unknown 的 12 个字段。"""

    def transport(_key, _endpoint, payload, _timeout):
        system = payload["messages"][0]["content"]
        if "候选清单整理器" in system:
            content = {"candidates": inventory}
        elif "字段补漏器" in system:
            content = {"fields": []}
        else:
            content = {"candidates": [{"model": inventory[0]["model"], "fields": [
                {"field_name": name, "value": "unknown", "status": "unknown", "sources": []}
                for name in CANONICAL_FIELD_NAMES
            ]}]}
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}]}

    return transport


def test_a_filtered_out_candidate_is_not_named_in_the_brand_gap():
    """型号筛选之后只剩带品牌的候选时，不写"资料里没有厂名"。

    对外粒度与结果一致（``docs/209`` §2）：被筛掉的候选不在结果里，缺口就不该点它的名。
    """
    inventory = [
        {"brand": "天美", "model": "UV1100",
         "sources": [{"document_id": "d", "page_or_location": "page:1", "excerpt": "UV1100"}]},
        {"brand": None, "model": "UV1200",
         "sources": [{"document_id": "d", "page_or_location": "page:1", "excerpt": "UV1200"}]},
    ]
    document = {"id": "d", "source_name": "a.pdf",
                "pages": [{"number": 1, "source_location": "page:1", "text": "UV1100 UV1200"}],
                "tables": []}

    result = DeepSeekAnalysisService(_client(_multi_transport(inventory))).analyze(
        scenario="采购一台紫外可见分光光度计", documents=[document], target_model="UV1100",
    )

    assert [candidate["model"] for candidate in result["candidates"]] == ["UV1100"]
    assert not any("资料里没有厂名" in gap for gap in result["gaps"])
    assert not result["failures"]


def test_an_extracted_brand_is_never_overwritten():
    detail_prompts = []
    client = _client(_transport(inventory_brand="天美", detail_prompts=detail_prompts))

    result = DeepSeekAnalysisService(client).analyze(
        scenario="采购一台紫外可见分光光度计", documents=[_sample_d_document()],
    )

    assert result["candidates"][0]["brand"] == "天美"


def test_two_makers_in_one_document_are_not_guessed():
    detail_prompts = []
    client = _client(_transport(inventory_brand=None, detail_prompts=detail_prompts))
    document = _sample_d_document()
    document["pages"].append({
        "number": 112, "source_location": "page:112",
        "text": "代理商：上海精宏实验技术有限公司",
    })

    result = DeepSeekAnalysisService(client).analyze(
        scenario="采购一台紫外可见分光光度计", documents=[document],
    )

    assert result["candidates"][0]["brand"] is None
    assert any("多个厂名" in gap for gap in result["gaps"])
