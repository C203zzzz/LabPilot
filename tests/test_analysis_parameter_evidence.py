"""Regression for grouped parameters with incomplete citations across layouts."""
import pytest

from backend.services import deepseek_analysis as analysis


@pytest.mark.parametrize("text,value,retained,missing", [
    ("Volume 75 L; Dimensions 400 x 500 x 600 mm",
     {"volume": "75 L", "dimensions": "400 x 500 x 600 mm", "weight": "38 kg"},
     {"volume": "75 L", "dimensions": "400 x 500 x 600 mm"}, ["weight"]),
    ("通量 96 样品/小时；电源 220 V",
     {"通量": "96 样品/小时", "电源": "220 V", "噪声": "45 dB"},
     {"通量": "96 样品/小时", "电源": "220 V"}, ["噪声"]),
    ("Accuracy 0.1 K; Volume 20 L",
     {"accuracy": {"value": 0.1, "unit": "K", "condition": "at 37 C"}, "volume": "20 L"},
     {"volume": "20 L"}, ["accuracy"]),
])
def test_unsupported_parameter_does_not_erase_supported_siblings(text, value, retained, missing):
    source = {"document_id": "d", "page_or_location": "page:2", "excerpt": text}
    result = analysis._validated_field({
        "field_name": "physical_dimensions_power_and_installation", "value": value,
        "sources": [source], "status": "pending_review",
    }, {("d", "page:2"): text})
    assert result["value"] == retained
    assert result["sources"] == [source]
    assert result["_unsupported_parts"] == missing
    assert result["status"] == "pending_review"


def test_atomic_string_cannot_lose_unsupported_condition_to_pass():
    result = analysis._validated_field({"value": "0.1 K at 37 C", "sources": [{
        "document_id": "d", "page_or_location": "page:1", "excerpt": "Accuracy 0.1 K",
    }]}, {("d", "page:1"): "Accuracy 0.1 K"})
    assert result["value"] == "unknown"
    assert result["sources"] == []
    assert result["_unsupported_parts"]


def test_fabricated_excerpt_cannot_salvage_even_nonnumeric_parameters():
    result = analysis._validated_field({"value": {"mode": "natural convection"}, "sources": [{
        "document_id": "d", "page_or_location": "page:1", "excerpt": "natural convection",
    }]}, {("d", "page:1"): "Forced air circulation"})
    assert result["value"] == "unknown"
    assert result["sources"] == []


def test_uncited_nonnumeric_claim_does_not_hitchhike_on_supported_sibling():
    result = analysis._validated_field({"value": {
        "volume": "20 L", "weight": "88 kg", "shipping": "transport upright",
    }, "sources": [{"document_id": "d", "page_or_location": "page:1", "excerpt": "Volume 20 L"}]},
        {("d", "page:1"): "Volume 20 L; transport upright"})
    assert result["value"] == {"volume": "20 L"}
    assert result["_unsupported_parts"] == ["weight", "shipping"]


def test_normalization_preserves_every_validated_parameter_citation():
    sources = [{"document_id": "d", "page_or_location": f"page:{i}", "excerpt": text}
               for i, text in enumerate(["Volume 20 L", "Power 220 V", "Weight 44 kg"], 1)]
    field = {"field_name": "physical_dimensions_power_and_installation",
             "value": {"volume": "20 L", "power": "220 V", "weight": "44 kg"},
             "sources": sources}
    result = analysis.DeepSeekAnalysisService()._normalize({"candidates": [{"model": "A20", "fields": [field]}]})
    output = next(f for f in result["candidates"][0]["fields"] if f["field_name"] == field["field_name"])
    assert output["sources"] == sources
    assert output["value"] == field["value"]


@pytest.mark.parametrize("text", ["Interior volume 20 L", "容积 53 升"])
def test_volume_vocabulary_triggers_capacity_gap_retrieval(text):
    assert "capacity_and_throughput" in analysis._field_retrieval_profile(text)["field_scores"]


def test_partial_repair_retains_previous_supported_values_and_missing_notice():
    evidence = {("d", "page:1"): "Dimensions 400 mm; Voltage 230 V"}
    field = {"field_name": "physical_dimensions_power_and_installation",
             "value": {"dimensions": "400 mm"}, "_unsupported_parts": ["weight"],
             "sources": [{"document_id": "d", "page_or_location": "page:1", "excerpt": "Dimensions 400 mm"}]}
    repair = {"field_name": field["field_name"], "value": {"voltage": "230 V"},
              "sources": [{"document_id": "d", "page_or_location": "page:1", "excerpt": "Voltage 230 V"}]}
    output = analysis._merge_repaired_fields([field], [repair], [field["field_name"]], evidence)[0]
    assert output["value"] == {"dimensions": "400 mm", "voltage": "230 V"}
    assert output["_unsupported_parts"] == ["weight"]


def test_normalization_does_not_modify_saved_model_node():
    raw = {"value": {"volume": "20 L", "weight": "88 kg"}, "sources": [{
        "document_id": "d", "page_or_location": "page:1", "excerpt": "Volume 20 L",
    }]}
    analysis._validated_field(raw, {("d", "page:1"): "Volume 20 L"})
    assert raw["value"] == {"volume": "20 L", "weight": "88 kg"}
    assert "_unsupported_parts" not in raw


def test_repaired_identical_value_under_more_precise_name_clears_old_gap():
    evidence = {("d", "page:1"): "Volume 20 L; Gross weight 88 kg"}
    first = analysis._validated_field({"field_name": "physical_dimensions_power_and_installation",
        "value": {"volume": "20 L", "weight": "88 kg"}, "sources": [{
            "document_id": "d", "page_or_location": "page:1", "excerpt": "Volume 20 L"}]}, evidence)
    repair = {"field_name": first["field_name"], "value": {"gross weight": "88 kg"}, "sources": [{
        "document_id": "d", "page_or_location": "page:1", "excerpt": "Gross weight 88 kg"}]}
    result = analysis._merge_repaired_fields([first], [repair], [first["field_name"]], evidence)[0]
    assert result["value"] == {"volume": "20 L", "gross weight": "88 kg"}
    assert not result["_unsupported_parts"]


def test_repair_citations_are_not_truncated_behind_first_pass_sources():
    sources = [{"document_id": "d", "page_or_location": "page:1", "excerpt": f"parameter {i} value {i} mm"}
               for i in range(1, 10)]
    evidence = {("d", "page:1"): '; '.join(s['excerpt'] for s in sources)}
    field = {"field_name": "physical_dimensions_power_and_installation", "value": {f"p{i}": f"{i} mm" for i in range(1, 9)},
             "sources": sources[:8], "_unsupported_parts": ["p9"], "_unsupported_values": {"p9": "9 mm"}}
    repair = {"field_name": field["field_name"], "value": {"p9": "9 mm"}, "sources": [sources[8]]}
    result = analysis._merge_repaired_fields([field], [repair], [field["field_name"]], evidence)[0]
    assert result["value"]["p9"] == "9 mm"
    assert result["sources"] == sources
    assert not result["_unsupported_parts"]


def test_different_signed_value_cannot_clear_missing_parameter():
    evidence = {("d", "page:1"): "Volume 20 L; operating limit +5 C"}
    field = {"field_name": "performance_range", "value": {"volume": "20 L"},
             "sources": [{"document_id": "d", "page_or_location": "page:1", "excerpt": "Volume 20 L"}],
             "_unsupported_parts": ["temperature"], "_unsupported_values": {"temperature": "-5 C"}}
    repair = {"field_name": field["field_name"], "value": {"operating limit": "+5 C"},
              "sources": [{"document_id": "d", "page_or_location": "page:1", "excerpt": "operating limit +5 C"}]}
    result = analysis._merge_repaired_fields([field], [repair], [field["field_name"]], evidence)[0]
    assert result["_unsupported_parts"] == ["temperature"]


@pytest.mark.parametrize('uniformity,fluctuation', [(0.3, 0.1), (0.8, 0.2)])
@pytest.mark.parametrize('layout', ['separate', 'combined'])
def test_temperature_metrics_cannot_borrow_each_others_numbers(uniformity, fluctuation, layout):
    rows = [f'Temperature variation at 37 °C [± K] {uniformity}',
            f'Temperature fluctuation at 37 °C [± K] {fluctuation}']
    excerpts = rows if layout == 'separate' else ['; '.join(rows)]
    sources = [{'document_id': 'd', 'page_or_location': 'page:1', 'excerpt': text} for text in excerpts]
    field = {'value': {'温度波动度（37 °C）': f'±{uniformity} K',
                       '温度均匀度（37 °C）': f'±{fluctuation} K'}, 'sources': sources}
    evidence = {('d', 'page:1'): '; '.join(rows)}
    rejected = analysis._validated_field(field, evidence)
    assert rejected['value'] == 'unknown'
    assert len(rejected['_unsupported_parts']) == 2
    corrected = analysis._validated_field({**field, 'value': {
        '温度波动度（37 °C）': f'±{fluctuation} K',
        '温度均匀度（37 °C）': f'±{uniformity} K'}}, evidence)
    assert len(corrected['value']) == 2
    assert not corrected['_unsupported_parts']


def test_temperature_condition_in_label_must_match_same_row():
    text = 'Temperature fluctuation at 37 °C [± K] 0.1; Temperature variation at 25 °C [± K] 0.3'
    result = analysis._validated_field({'value': {'25 °C 温度波动度': '±0.1 K'},
        'sources': [{'document_id': 'd', 'page_or_location': 'page:1', 'excerpt': text}]}, {('d', 'page:1'): text})
    assert result['value'] == 'unknown'


def test_repair_without_units_cannot_overwrite_previously_supported_quantities():
    text = 'Voltage 230 V; Weight 44 kg; Number of doors 1'
    sources = [{'document_id': 'd', 'page_or_location': 'page:1', 'excerpt': text}]
    field = {'field_name': 'physical_dimensions_power_and_installation',
             'value': {'电压': '230 V', '重量': '44 kg'}, 'sources': sources}
    repair = {**field, 'value': {'电压': '230', '重量': '44', '门数量': '1'}}
    merged = analysis._merge_repaired_fields([field], [repair], [field['field_name']], {('d', 'page:1'): text})[0]
    assert merged['value'] == {'电压': '230 V', '重量': '44 kg', '门数量': '1'}
    assert not merged['_unsupported_parts']


def test_mixed_unit_group_cannot_justify_bare_scalars():
    text = 'Voltage 230 V; Weight 44 kg'
    result = analysis._validated_field({'field_name': 'physical_dimensions_power_and_installation',
        'value': {'电压': '230', '重量': '44'}, 'unit': 'V; kg',
        'sources': [{'document_id': 'd', 'page_or_location': 'page:1', 'excerpt': text}]}, {('d', 'page:1'): text})
    assert result['value'] == 'unknown'


# --- 键名自带长度单位的尺寸三连 ------------------------------------------------
# 样例数据：知楚 A2 Ⅱ级生物安全柜参数表（task-<run-8>，PDF 第 1 页）
#   | 外形尺寸(长×宽×高mm) (支架高度720) | ... | 1050×850×2120 | 1340×850×2120 | ...
#   | 内部尺寸(长×深×高mm) | ... | 980×580×660 | 1270×580×660 | ...
# 模型把单位写在键名上，字段自己没有 unit。这些值原文里就是「裸三连数字」，
# 但它们并不缺单位——单位在键名上。此前一律按「缺单位」拒收，于是整屏
# 「识别到但未结构化」。判据只放行「三连数字 + 键名写了 ≥2 个轴 + 键名有长度单位」
# 三条同时成立的情形，且**不往值里补任何字符**。

_REAL_TABLE_TEXT = (
    '| 外形尺寸(长×宽×高mm) (支架高度720) | 外形尺寸(长×宽×高mm) (支架高度720) | '
    '1050×850×2120 | 1340×850×2120 | 1645×850×2120 |\n'
    '| 内部尺寸(长×深×高mm) | 内部尺寸(长×深×高mm) | 980×580×660 | 1270×580×660 | 1575×580×660\n'
)


def _dimension_field(value):
    return {'field_name': 'physical_dimensions_power_and_installation', 'value': value,
            'sources': [{'document_id': 'd', 'page_or_location': 'page:1', 'excerpt': _REAL_TABLE_TEXT}]}


def test_label_declared_length_unit_structures_the_dimension_triple():
    result = analysis._validated_field(_dimension_field({
        '外形尺寸(长×宽×高mm) (支架高度720)': '1340×850×2120',
        '内部尺寸(长×深×高mm)': '1270×580×660',
    }), {('d', 'page:1'): _REAL_TABLE_TEXT})
    assert result['value'] == {
        '外形尺寸(长×宽×高mm) (支架高度720)': '1340×850×2120',
        '内部尺寸(长×深×高mm)': '1270×580×660',
    }
    assert not result['_unsupported_parts']
    # 单位留在键名上，值必须逐字等于原文，不许补 mm 也不许换算。
    assert 'mm' not in result['value']['外形尺寸(长×宽×高mm) (支架高度720)']


@pytest.mark.parametrize('label,value', [
    ('气体交换体积(风速额定值)', '485'),          # 裸数字，键名没有长度单位
    ('产品净重', '160'),                          # 同上
    ('装载体积', '1.57'),                         # 同上
    ('内部尺寸(长×宽×高)', '1270×580×660'),      # 有轴但键名没写长度单位
    ('外形尺寸(长mm)', '1050×850×2120'),         # 只有 1 个轴
    ('外形尺寸(长×宽×高mm)', '1050×850'),        # 不是三连
    ('外形尺寸(长×宽×max)', '1050×850×2120'),    # 单个拉丁字母 m 不算长度单位（max 不是米）
    ('外形尺寸(长×宽×高m)', '1.34×0.85×2.12'),   # 单字母单位一律不放行：宁可漏收也不误收
])
def test_dimension_triple_without_a_declared_length_unit_stays_unparsed(label, value):
    text = f'{label} {value}'
    result = analysis._validated_field({
        'field_name': 'physical_dimensions_power_and_installation', 'value': {label: value},
        'sources': [{'document_id': 'd', 'page_or_location': 'page:1', 'excerpt': text}],
    }, {('d', 'page:1'): text})
    assert result['value'] == 'unknown'
    assert result['_unsupported_parts'] == [label]
    assert result['_unsupported_values'] == {label: value}
