"""表格型号规则抽取：只发现、不否决；只加不减；三种形态各自钉住。

背景（``output/table-model-rule-extract/report.md`` 与 ``impl2-report.md``，已评估验证）：
sample-c 那份扫描件的规格表列头就是**裸编号** ``080/150/250/350/450``，而清单环节的模型
有时把它们命名成 ``SHP-080``（全篇没有这个串）—— 候选每轮都在漂。规则从表格结构里机械地
读出这些型号，作为**新的一路**候选并入池子，把漂移的锚点钉死。

第三形态（形态 C）来自端到端证据：扫描件没有表格结构，产品喂给规则的是**渲染文本**，
里面就是正文行 ``型号：080、150、250、350、450``（``output/table-model-rule-extract/
_tmp_rendered.txt``）。它旁边紧跟着 ``消耗功率：600W、…`` 与
``工作室尺寸(mm)：400x400x500、…`` 两行同样是"标签＋顿号列举"、但绝不是型号——天然反例。

这里钉住四件事：
  1. 形态 A（列头是型号）、B（型号标签行）、C（正文列举行）读得对，
     且不越界读 G/E 类无型号位的表和"标签＋列举"的值行；
  2. 读不到时**什么都不发生** —— 模型原有候选一条不少（历史坑 ``7b1e57b`` 的防线）；
  3. 读到的候选是**并集**，且 ``080`` 与 ``080A`` 不许被并成一条；
  4. 全角写法（``ＳＨＰ－０８０``）按同一台算，而 ``080`` 与 ``80``、``080A`` 不算。
"""
from __future__ import annotations

import pytest

from backend.services import deepseek_analysis as da
from backend.services import table_model_discovery as tmd

SAMPLE_C_PAGE_TWO = "\n".join([
    "三、技术指标",
    "|  | 080 | 150 | 250 | 350 | 450 |",
    "| 电源电压 | 220V 50Hz | 220V 50Hz | 220V 50Hz | 220V 50Hz | 220V 50Hz |",
    "| 控制范围 | 5060℃ | 5060℃ | 5060℃ | 5060℃ | 5060℃ |",
    "| 温度波动 | ±0.5℃ | ±0.5℃ | ±0.5℃ | ±0.5℃ | ±0.5℃ |",
    "| 消耗功率 | 600W | 700W | 800W | 1450 | 1700 |",
])


def _document(page_text: str, *, location: str = "page:2/rendered") -> dict:
    return {
        "document_id": "doc-1", "source_name": "sample-c.pdf", "tables": [],
        "pages": [{"location": location, "text": page_text}],
    }


def _models(items) -> list[str]:
    return [item["model"] for item in items]


# ------------------------------------------------------------------ 形态 A
def test_column_header_models_are_discovered_in_order():
    hits = tmd.discover_in_text(SAMPLE_C_PAGE_TWO)
    assert [(hit["kind"], hit["model"]) for hit in hits] == [
        ("column_header", "080"), ("column_header", "150"), ("column_header", "250"),
        ("column_header", "350"), ("column_header", "450"),
    ]
    assert hits[0]["line"] == "|  | 080 | 150 | 250 | 350 | 450 |"


def test_column_header_accepts_model_label_in_first_cell():
    text = "\n".join([
        "| 型号 | 080 | 150 | 250 |",
        "| 电源电压 | 220V | 220V | 220V |",
        "| 控制范围 | 5~60℃ | 5~60℃ | 5~60℃ |",
    ])
    hits = tmd.discover_in_text(text)
    assert _models(hits) == ["080", "150", "250"]
    # 首格写着"型号"的列头行同时满足两种形态：同一份资料里同型号只留第一次读到的位置。
    assert {hit["kind"] for hit in hits} == {"column_header"}
    assert tmd.table_model_candidates([_document(text)]) != []
    assert len(tmd.table_model_candidates([_document(text)])) == 3


def test_column_header_needs_parameter_labels_below():
    """下方两行都是数值行 → 它不是表头，规则不该读它。"""
    text = "\n".join([
        "|  | 080 | 150 | 250 |",
        "| 220V | 50Hz | 600W |",
        "| 700W | 800W | 1450 |",
    ])
    assert tmd.discover_in_text(text) == []


def test_two_column_spec_table_has_no_model_slot():
    text = "\n".join([
        "| 电源电压 | 220V 50Hz |",
        "| 控制范围 | 5~60℃ |",
    ])
    assert tmd.discover_in_text(text) == []


# ------------------------------------------------------------------ 形态 B
def test_model_label_row_is_discovered():
    text = "| 型号 | H1850R |\n| 最高转速 | 18500rpm |"
    hits = tmd.discover_in_text(text)
    assert [(hit["kind"], hit["model"]) for hit in hits] == [("label_row", "H1850R")]


def test_value_cells_are_not_models():
    assert tmd.identifier_like("220V 50Hz") is False
    assert tmd.identifier_like("600W") is False
    assert tmd.identifier_like("400x400x500") is False
    assert tmd.identifier_like("温度波动") is False
    assert tmd.identifier_like("080") is True
    assert tmd.identifier_like("SHP-080") is True
    # 末尾单字母：带前导零的是型号后缀（080A），不带的是量值（5A/10A/2.5A）。
    assert tmd.identifier_like("080A") is True
    assert tmd.identifier_like("0150A") is True
    for ampere in ("5A", "10A", "2.5A", "0.5A", "100A", "5a"):
        assert tmd.identifier_like(ampere) is False, ampere
    # 未改的既有边界：其他单字母单位仍一律挡住（080M / 080L / 080C）。
    for other in ("080M", "080L", "080C"):
        assert tmd.identifier_like(other) is False, other


def test_ampere_values_are_not_models_in_any_form():
    """真正的安培写法不许被误当型号：单看、整行、列举三处都要挡住。"""
    assert tmd.identifier_like("5A") is False
    assert tmd.identifier_like("10A") is False
    assert tmd.identifier_like("2.5A") is False
    assert tmd.discover_in_prose("额定电流：5A、10A、2.5A") == []
    # 标签是型号标签也不行：列举里全是安培值 → 合格项为 0，整行放弃。
    assert tmd.discover_in_prose("型号：5A、10A") == []
    assert tmd.discover_in_text("型号：5A、10A、2.5A") == []


# ------------------------------------------------------------------ 形态 C：正文列举行
#: ``output/table-model-rule-extract/_tmp_rendered.txt``（sample-c ``page:2/rendered``）
#: 里连着出现的那三行——第一行是型号列举，后面两行是**天然反例**。
SAMPLE_C_RENDERED_LINES = "\n".join([
    "三、技术指标",
    "型号：080、150、250、350、450",
    "电源电压：220V 50Hz",
    "控制范围：5~60℃",
    "温度波动：±0.5℃",
    "消耗功率：600W、700W、800W、1450W、1700W",
    "工作室尺寸(mm)：400x400x500、500x450x650、600x550x750、600x550x1000、600x710x1000",
    "四、结构概述",
])


def test_prose_enumeration_is_discovered():
    hits = tmd.discover_in_text("型号：080、150、250、350、450")
    assert [(hit["kind"], hit["model"]) for hit in hits] == [
        ("prose_enum", "080"), ("prose_enum", "150"), ("prose_enum", "250"),
        ("prose_enum", "350"), ("prose_enum", "450"),
    ]
    assert hits[0]["line"] == "型号：080、150、250、350、450"


def test_rendered_page_yields_only_the_model_line():
    """渲染文本整段过一遍：只有型号那行命中，参数行一行都不许命中。"""
    models = _models(tmd.discover_in_text(SAMPLE_C_RENDERED_LINES))
    assert models == ["080", "150", "250", "350", "450"]
    assert tmd.table_model_candidates([
        {"document_id": "doc-c", "source_name": "sample-c.pdf", "tables": [],
         "pages": [{"location": "page:2/rendered", "text": SAMPLE_C_RENDERED_LINES}]},
    ]) != []
    assert len(tmd.table_model_candidates([
        {"document_id": "doc-c", "source_name": "sample-c.pdf", "tables": [],
         "pages": [{"location": "page:2/rendered", "text": SAMPLE_C_RENDERED_LINES}]},
    ])) == 5


def test_parameter_enumeration_is_not_a_model_list():
    """天然反例：同样是"标签＋顿号列举"，但值是功率/尺寸，绝不该被认成型号。"""
    assert tmd.discover_in_text("消耗功率：600W、700W、800W、1450W、1700W") == []
    assert tmd.discover_in_text(
        "工作室尺寸(mm)：400x400x500、500x450x650、600x550x750") == []
    assert tmd.discover_in_text("电源电压：220V、380V") == []
    assert tmd.discover_in_text("控制范围：5~60℃、10~50℃") == []


@pytest.mark.parametrize("line", [
    "型号：080",                      # 只有一项，不是"一组"
    "型号：080、",                    # 空项
    "型号：080、150（详见附表）",      # 行尾带说明，合格项只剩一项 → 整行放弃
    "参考型号：080、150",             # 标签不是"整格等于型号标签"
    "型号规格如下：080、150",          # 同上
    "型号 080、150",                  # 没有冒号，不是明确列举形态
    "型号：080、80A1B2C3",            # 一项不合格，合格项只剩一项 → 整行放弃
    "型号：080、1",                   # 1 位数字不成型号
    "型号：SHP-080 与 SHP-150",       # 分隔符不是顿号/逗号/分号
])
def test_prose_enumeration_does_not_overreach(line):
    assert tmd.discover_in_text(line) == []


def test_prose_enumeration_drops_dirty_item_but_keeps_the_rest():
    """一项被弄脏不该把整行的好型号一起拖没；丢掉的那项本来也读不出来。"""
    hits = tmd.discover_in_text("型号：080、700W、250")
    assert [(hit["kind"], hit["model"]) for hit in hits] == [
        ("prose_enum", "080"), ("prose_enum", "250"),
    ]


def test_prose_enumeration_reads_080a_but_keeps_it_apart_from_080():
    """``080A`` 是合法型号，必须读出来；读出来之后**仍然不是同一台**。

    修复前 ``identifier_like`` 把末尾的 ``A`` 一律当安培单位，``080A`` 整条丢掉；
    现在数字带前导零时算型号后缀（``_AMPERE_UNIT_RE``），真正的安培写法照旧挡住。
    """
    hits = tmd.discover_in_text("型号：080、080A、150")
    assert [(hit["kind"], hit["model"]) for hit in hits] == [
        ("prose_enum", "080"), ("prose_enum", "080A"), ("prose_enum", "150"),
    ]
    assert _models(hits) == ["080", "080A", "150"]
    assert da.model_number_agrees("080", "080A") is False
    assert da.model_number_agrees("080A", "080") is False
    # 合并层也不许把两台并成一条。
    merged, _ = da._merge_inventory_candidates([{"candidates": [
        {"product_role": "instrument", "brand": None, "series": None, "model": "080",
         "configuration": None,
         "sources": [{"document_id": "d1", "page_or_location": "page:2/rendered",
                      "excerpt": "型号：080、080A、150"}]},
        {"product_role": "instrument", "brand": None, "series": None, "model": "080A",
         "configuration": None,
         "sources": [{"document_id": "d1", "page_or_location": "page:2/rendered",
                      "excerpt": "型号：080、080A、150"}]},
    ]}], {})
    assert [c["model"] for c in merged] == ["080", "080A"]


# --------------------------------------------------- 形态 A/B 共用一个判据：一起重验
def test_label_row_reads_080a_after_the_fix():
    """形态 B（型号标签行）同享 :func:`identifier_like`，修复后也该读出 ``080A``。"""
    hits = tmd.discover_in_text("| 型号 | 080A |\n| 电源电压 | 220V |")
    assert [(hit["kind"], hit["model"]) for hit in hits] == [("label_row", "080A")]
    # 但"额定电流"这种标签不是型号标签，安培值仍旧进不来。
    assert tmd.discover_in_text("| 额定电流 | 5A |\n| 电源电压 | 220V |") == []
    assert tmd.discover_in_text("| 额定电流 | 10A |\n| 电源电压 | 220V |") == []


def test_column_header_reads_080a_after_the_fix():
    """形态 A（列头是型号）也同享这个判据。"""
    text = "\n".join([
        "|  | 080 | 080A | 150 |",
        "| 电源电压 | 220V | 220V | 220V |",
        "| 控制范围 | 5~60℃ | 5~60℃ | 5~60℃ |",
    ])
    hits = tmd.discover_in_text(text)
    assert _models(hits) == ["080", "080A", "150"]
    assert {hit["kind"] for hit in hits} == {"column_header"}


# ------------------------------------------------------------------ 输入边界
def test_prose_enumeration_does_not_touch_pipe_cells():
    """单元格里碰巧写着同一个串，也不该被正文形态重复读出来（表格形态自己管）。"""
    hits = tmd.discover_in_text("| 型号 | H1850R |\n| 最高转速 | 18500rpm |")
    assert [(hit["kind"], hit["model"]) for hit in hits] == [("label_row", "H1850R")]


def test_degenerate_single_column_table_is_not_read():
    text = "\n".join(["| 5420 |", "| 5420 |", "| 5420 |"])
    assert tmd.discover_in_text(text) == []


def test_docling_table_rows_are_read_with_row_excerpt():
    document = {
        "document_id": "doc-2", "source_name": "x.pdf",
        "pages": [],
        "tables": [{"index": 0, "source_location": "page:5",
                    "rows": [["型号", "H1850R"], ["最高转速", "18500rpm"]]}],
    }
    candidates = tmd.table_model_candidates([document])
    assert [c["model"] for c in candidates] == ["H1850R"]
    assert candidates[0]["sources"][0]["excerpt"] == "型号 | H1850R"
    assert candidates[0]["sources"][0]["page_or_location"] == "page:5"


def test_candidate_cap_keeps_the_pipeline_alive():
    cells = " | ".join(str(1000 + index) for index in range(40))
    text = "\n".join([
        f"|  | {cells} |",
        "| 电源电压 | " + " | ".join(["220V"] * 40) + " |",
        "| 控制范围 | " + " | ".join(["5~60℃"] * 40) + " |",
    ])
    candidates = tmd.table_model_candidates([_document(text)])
    assert len(candidates) == tmd._MAX_RULE_CANDIDATES
    assert len(candidates) < 50


# ------------------------------------------------------------------ 并集：只加不减
def test_rule_candidates_only_add_to_model_candidates():
    model_candidates = [
        {"product_role": "instrument", "brand": "上海精宏", "series": None,
         "model": "SHP-080", "configuration": None,
         "sources": [{"document_id": "doc-1", "page_or_location": "page:1/rendered",
                      "excerpt": "SHP 系列 生化培养箱 使用说明书"}]},
    ]
    prompt_docs = [_document(SAMPLE_C_PAGE_TWO)]
    evidence = da._source_evidence_index(prompt_docs)
    before, _ = da._merge_inventory_candidates([{"candidates": model_candidates}], evidence)
    after, _ = da._merge_inventory_candidates(
        [{"candidates": model_candidates}, tmd.table_model_inventory(prompt_docs)], evidence)
    before_keys = {da._normalized_model(c.get("model")) for c in before}
    after_keys = {da._normalized_model(c.get("model")) for c in after}
    assert before_keys <= after_keys
    assert [c["model"] for c in after][:1] == ["SHP-080"]
    assert {"080", "150", "250", "350", "450"} <= {c["model"] for c in after}


def test_unreadable_table_leaves_model_candidates_untouched():
    """反向：表格形态怪、规则读不出 → 模型候选完全不受影响。"""
    model_candidates = [
        {"product_role": "instrument", "brand": "赛默飞", "series": None,
         "model": "QuantStudio 5", "configuration": None,
         "sources": [{"document_id": "doc-1", "page_or_location": "page:1",
                      "excerpt": "参考型号及技术参数"}]},
    ]
    weird = _document("\n".join(["| 序号 | 参数 | 值 |", "| 1 | 通道 | 4 |"]), location="page:1")
    prompt_docs = [weird]
    evidence = da._source_evidence_index(prompt_docs)
    before, _ = da._merge_inventory_candidates([{"candidates": model_candidates}], evidence)
    assert tmd.table_model_candidates(prompt_docs) == []
    after, _ = da._merge_inventory_candidates(
        [{"candidates": model_candidates}, tmd.table_model_inventory(prompt_docs)], evidence)
    assert [c["model"] for c in before] == [c["model"] for c in after] == ["QuantStudio 5"]


# ------------------------------------------------------------------ 同一台判据
@pytest.mark.parametrize("left,right,expected", [
    ("080", "080A", False),
    ("080A", "080", False),
    ("080", "080", True),
    ("080", "SHP-080", True),
    ("SHP-080", "SHP 080", True),
    ("080", "SHP.080", True),
    ("080", "150", False),
    ("080", "80", False),
    ("SHP-080", "DHP-080", False),
    ("080", "ＳＨＰ－０８０", True),
    ("080", "０８０", True),
    ("ＳＨＰ－０８０", "SHP-080", True),
    ("080", "０８０Ａ", False),
    ("０８０", "080A", False),
    ("０８０", "８０", False),
])
def test_model_number_agrees_matrix(left, right, expected):
    assert da.model_number_agrees(left, right) is expected


def test_fullwidth_variant_is_one_machine_but_080a_is_not():
    probe = [
        {"product_role": "instrument", "brand": None, "series": None, "model": "080",
         "configuration": None,
         "sources": [{"document_id": "d1", "page_or_location": "page:2/rendered",
                      "excerpt": "|  | 080 | 150 | 250 | 350 | 450 |"}]},
        {"product_role": "instrument", "brand": None, "series": None, "model": "080A",
         "configuration": None,
         "sources": [{"document_id": "d1", "page_or_location": "page:2/rendered",
                      "excerpt": "|  | 080 | 150 | 250 | 350 | 450 |"}]},
        {"product_role": "instrument", "brand": None, "series": None, "model": "ＳＨＰ－０８０",
         "configuration": None,
         "sources": [{"document_id": "d1", "page_or_location": "page:1/rendered",
                      "excerpt": "SHP 系列 生化培养箱 使用说明书"}]},
    ]
    merged, _ = da._merge_inventory_candidates([{"candidates": probe}], {})
    models = [c["model"] for c in merged]
    # 候选清单层按完整型号字符串判同（既有设计），三条各自保留。
    assert models == ["080", "080A", "ＳＨＰ－０８０"], models
    # "同一台"判据用于无归属候选沿兄弟取数：全角算同一台，080A 不算。
    assert da.model_number_agrees("080", "ＳＨＰ－０８０") is True
    assert da.model_number_agrees("080", "080A") is False
    assert da.model_number_agrees("ＳＨＰ－０８０", "SHP-080") is True
