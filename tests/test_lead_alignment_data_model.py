"""设计规范 对话 1：线索对齐数据模型（SearchQuery / SourceRecord 的字段扩展）。

本模块只验证数据结构约定，不涉及任何运行逻辑：

1. 新增字段全部可选，老调用点一行不改也能构造（向后兼容）；
2. 唯一真源 ``backend/domain/search.py`` 保持不可变、tuple 契约与完整字段清单；
3. 既有位置参数构造方式不被破坏——``backend/services/official_search.py:235`` 就是位置参数；
4. 新增字段的默认值、显式赋值、归一化与校验行为。

字段命名刻意不对称，不要"统一"掉：
- ``SearchQuery`` 用 ``supplier_name`` / ``buyer_name``
- ``SourceRecord`` 用 ``supplier`` / ``buyer``
"""
import dataclasses
from datetime import datetime, timezone

import pytest

from backend.domain import search as legacy
from backend.domain import search as hardened
from backend.schemas.search import query_to_dict, source_to_dict


QUERY_MODULES = pytest.mark.parametrize(
    "module", [legacy], ids=["search"]
)

#: 设计规范 对话 1 要求新增的字段，两文件必须一致
NEW_QUERY_FIELDS = ("buyer_name", "supplier_name", "project_id", "window_start", "window_end")
NEW_SOURCE_FIELDS = ("supplier", "buyer", "project_id", "quantity")


def _query(module, **kwargs):
    return module.SearchQuery("生物安全柜 中标", **kwargs)


#: 固定时间戳，用来验证位置参数之后的关键字参数仍然映射到正确的字段
RETRIEVED_AT = datetime(2024, 6, 1, tzinfo=timezone.utc)


def _source(module, **kwargs):
    """按 backend/services/official_search.py:235 的位置参数写法构造。"""
    return module.SourceRecord(
        "https://example.com/tender/1", "某医院检验设备成交公告",
        module.SourceType.TENDER_DOCUMENT, 85, "官方域名", "snippet",
        provider="ccgp", provider_provenance=["ccgp"], retrieved_at=RETRIEVED_AT, **kwargs,
    )


class TestFieldInventory:
    """字段清单与默认值：这是其它三路要照抄的约定。"""

    def test_new_fields_are_declared_with_none_default_in_both_modules(self):
        for module in (legacy, hardened):
            pairs = (
                (module.SearchQuery, NEW_QUERY_FIELDS),
                (module.SourceRecord, NEW_SOURCE_FIELDS),
            )
            for cls, names in pairs:
                declared = {f.name: f for f in dataclasses.fields(cls)}
                for name in names:
                    assert name in declared, f"{cls.__module__}.{cls.__name__} 缺字段 {name}"
                    assert declared[name].default is None, (
                        f"{cls.__module__}.{cls.__name__}.{name} 默认值必须是 None（保证老调用点不改也能构造）"
                    )

    def test_query_fields_match_between_modules_in_name_and_order(self):
        a = tuple(f.name for f in dataclasses.fields(legacy.SearchQuery))
        b = tuple(f.name for f in dataclasses.fields(hardened.SearchQuery))
        assert a == b

    def test_source_fields_match_between_modules_in_name_and_order(self):
        a = tuple(f.name for f in dataclasses.fields(legacy.SourceRecord))
        b = tuple(f.name for f in dataclasses.fields(hardened.SourceRecord))
        assert a == b

    def test_new_field_defaults_match_between_modules(self):
        for cls_name, names in (("SearchQuery", NEW_QUERY_FIELDS), ("SourceRecord", NEW_SOURCE_FIELDS)):
            def defaults(module):
                declared = {f.name: f for f in dataclasses.fields(getattr(module, cls_name))}
                return {n: declared[n].default for n in names}
            assert defaults(legacy) == defaults(hardened)

    def test_new_fields_are_appended_after_existing_ones(self):
        """新字段必须加在字段表末尾，否则 official_search.py 的位置参数会错位。"""
        for module in (legacy, hardened):
            query_names = [f.name for f in dataclasses.fields(module.SearchQuery)]
            assert query_names[7:] == list(NEW_QUERY_FIELDS)
            assert query_names[:7] == [
                "text", "candidate_id", "brand", "model", "limit", "purpose", "target_source_types",
            ]
            source_names = [f.name for f in dataclasses.fields(module.SourceRecord)]
            assert source_names[-4:] == list(NEW_SOURCE_FIELDS)


class TestBackwardCompatibility:
    """老调用点不传新字段也必须能跑 —— 本单最重要的验收点。"""

    @QUERY_MODULES
    def test_old_style_query_constructions_still_work(self, module):
        # 以下形态逐个取自现网调用点
        assert _query(module).text == "生物安全柜 中标"                     # tests/ 大量这样写
        assert module.SearchQuery("实验室仪器 官方资料", limit=1, purpose="official").limit == 1  # routes.py:525
        built = module.SearchQuery(text="泵", candidate_id="c1", brand="知楚", model="1500A2", purpose="price")
        assert built.candidate_id == "c1" and built.brand == "知楚"        # official_search.py:306
        # procurement_research.py:1032 的形态：把空值折叠成 None 再传进来
        assert module.SearchQuery("x", purpose="price", limit=10, brand=None, model=None).model is None

    @QUERY_MODULES
    def test_old_style_source_construction_with_positional_args_still_works(self, module):
        s = _source(module)
        # 前六个位置参数必须仍然落在原来的字段上
        assert s.url == "https://example.com/tender/1"
        assert s.title == "某医院检验设备成交公告"
        assert s.source_type is module.SourceType.TENDER_DOCUMENT
        assert s.authority_score == 85
        assert s.authority_reason == "官方域名"
        assert s.snippet == "snippet"
        assert s.retrieved_at == RETRIEVED_AT
        # 新字段全部取默认值
        for name in NEW_SOURCE_FIELDS:
            assert getattr(s, name) is None

    @QUERY_MODULES
    def test_new_query_fields_default_to_none(self, module):
        q = _query(module)
        for name in NEW_QUERY_FIELDS:
            assert getattr(q, name) is None

    def test_limit_and_purpose_defaults_unchanged(self):
        for module in (legacy, hardened):
            q = module.SearchQuery("x")
            assert q.limit == 10
            assert q.purpose == "general"
            assert q.target_source_types == ()


class TestFieldSemantics:
    """显式赋值与两文件行为一致性。"""

    @QUERY_MODULES
    def test_query_keeps_explicit_lead_values(self, module):
        q = _query(
            module, buyer_name="库车市农业农村局", supplier_name="知楚",
            project_id="2921-20250429-667", window_start="2024-01-01", window_end="2024-12-31",
        )
        assert q.buyer_name == "库车市农业农村局"
        assert q.supplier_name == "知楚"
        assert q.project_id == "2921-20250429-667"
        assert q.window_start == "2024-01-01"
        assert q.window_end == "2024-12-31"

    @QUERY_MODULES
    def test_source_keeps_explicit_lead_values(self, module):
        s = _source(
            module, supplier="武汉坎德尼", buyer="湖北大学",
            project_id="2921-20250429-667", quantity=2,
        )
        assert s.supplier == "武汉坎德尼"
        assert s.buyer == "湖北大学"
        assert s.project_id == "2921-20250429-667"
        assert s.quantity == 2

    def test_both_modules_agree_on_the_same_input(self):
        kwargs = dict(
            buyer_name="湖北大学", supplier_name="武汉坎德尼",
            project_id="PRJ-1", window_start="2024-01-01", window_end="2024-12-31",
        )
        a, b = _query(legacy, **kwargs), _query(hardened, **kwargs)
        assert [getattr(a, n) for n in NEW_QUERY_FIELDS] == [getattr(b, n) for n in NEW_QUERY_FIELDS]

        skwargs = dict(supplier="武汉坎德尼", buyer="湖北大学", project_id="PRJ-1", quantity=2.5)
        sa, sb = _source(legacy, **skwargs), _source(hardened, **skwargs)
        assert [getattr(sa, n) for n in NEW_SOURCE_FIELDS] == [getattr(sb, n) for n in NEW_SOURCE_FIELDS]

    def test_naming_is_intentionally_asymmetric(self):
        """SearchQuery 用 supplier_name/buyer_name，SourceRecord 用 supplier/buyer。"""
        query_names = {f.name for f in dataclasses.fields(legacy.SearchQuery)}
        source_names = {f.name for f in dataclasses.fields(legacy.SourceRecord)}
        assert {"supplier_name", "buyer_name"} <= query_names
        assert "supplier" not in query_names and "buyer" not in query_names
        assert {"supplier", "buyer"} <= source_names
        assert "supplier_name" not in source_names and "buyer_name" not in source_names

    def test_project_id_exists_on_both_structures(self):
        assert "project_id" in {f.name for f in dataclasses.fields(legacy.SearchQuery)}
        assert "project_id" in {f.name for f in dataclasses.fields(legacy.SourceRecord)}


class TestNormalizationAndValidation:
    """归一化与校验：空值折叠成 None，脏值不炸链路。"""

    @QUERY_MODULES
    def test_blank_identifiers_fold_to_none(self, module):
        q = _query(module, buyer_name="", supplier_name="   ", project_id="\t\n")
        assert q.buyer_name is None and q.supplier_name is None and q.project_id is None
        s = _source(module, supplier="", buyer="  ")
        assert s.supplier is None and s.buyer is None

    @QUERY_MODULES
    def test_identifiers_are_stripped(self, module):
        q = _query(module, buyer_name="  湖北大学 ", supplier_name=" 武汉坎德尼")
        assert q.buyer_name == "湖北大学"
        assert q.supplier_name == "武汉坎德尼"

    @QUERY_MODULES
    def test_blank_window_folds_to_none_and_valid_iso_is_kept(self, module):
        q = _query(module, window_start="", window_end=" 2024-12-31 ")
        assert q.window_start is None
        assert q.window_end == "2024-12-31"

    @QUERY_MODULES
    def test_non_iso_window_is_rejected(self, module):
        for bad in ("2024-1-1", "2024/01/01", "20240101", "2024-13-01", "2024-02-30", "去年"):
            with pytest.raises(ValueError):
                _query(module, window_start=bad)

    @QUERY_MODULES
    def test_window_start_must_not_be_after_end(self, module):
        with pytest.raises(ValueError):
            _query(module, window_start="2025-01-01", window_end="2024-01-01")
        # 相等和单边界都是合法的
        assert _query(module, window_start="2024-01-01", window_end="2024-01-01").window_start == "2024-01-01"
        assert _query(module, window_start="2024-01-01").window_end is None

    @QUERY_MODULES
    def test_window_accepts_the_real_ccgp_style_window(self, module):
        """实测的 CCGP 分段窗口（设计规范 事实 #5）必须能构造。"""
        q = _query(module, window_start="2024-01-01", window_end="2024-12-31")
        assert (q.window_start, q.window_end) == ("2024-01-01", "2024-12-31")

    @QUERY_MODULES
    def test_quantity_rejects_negative_and_non_numeric(self, module):
        with pytest.raises(ValueError):
            _source(module, quantity=-1)
        with pytest.raises(ValueError):
            _source(module, quantity="两台")

    @QUERY_MODULES
    def test_quantity_accepts_int_float_and_zero(self, module):
        assert _source(module, quantity=0).quantity == 0
        assert _source(module, quantity=2).quantity == 2
        assert _source(module, quantity=2.5).quantity == 2.5

    @QUERY_MODULES
    def test_overlong_identifier_rejected(self, module):
        with pytest.raises(ValueError):
            _query(module, buyer_name="x" * 201)
        assert _query(module, buyer_name="x" * 200).buyer_name == "x" * 200

    @QUERY_MODULES
    def test_non_string_identifier_rejected(self, module):
        with pytest.raises(ValueError):
            _query(module, project_id=20250429)


class TestSerialization:
    """backend/schemas/search.py 走 asdict，新增键会出现在 API 响应里。"""

    def test_source_to_dict_exposes_new_keys(self):
        d = source_to_dict(_source(legacy, supplier="知楚", buyer="库车市农业农村局",
                                   project_id="PRJ-1", quantity=1))
        assert d["supplier"] == "知楚"
        assert d["buyer"] == "库车市农业农村局"
        assert d["project_id"] == "PRJ-1"
        assert d["quantity"] == 1

    def test_query_to_dict_exposes_new_keys(self):
        d = query_to_dict(_query(legacy, buyer_name="湖北大学", window_start="2024-01-01"))
        assert d["buyer_name"] == "湖北大学"
        assert d["window_start"] == "2024-01-01"
        for name in NEW_QUERY_FIELDS:
            assert name in d

    def test_unset_lead_fields_serialize_as_none(self):
        d = source_to_dict(_source(legacy))
        for name in NEW_SOURCE_FIELDS:
            assert d[name] is None


class TestImmutableSearchModelContract:
    """合并后的唯一搜索领域模型保持不可变与 tuple 契约。"""

    def test_query_is_frozen(self):
        q = _query(legacy)
        with pytest.raises(dataclasses.FrozenInstanceError):
            q.buyer_name = "湖北大学"

    def test_source_is_frozen(self):
        source = _source(legacy)
        with pytest.raises(dataclasses.FrozenInstanceError):
            source.supplier = "知楚"

    def test_provider_provenance_is_tuple(self):
        assert isinstance(_source(legacy).provider_provenance, tuple)

    def test_replace_helpers_preserve_lead_fields(self):
        """with_body/with_error 用 replace，新增字段必须被带上。"""
        s = _source(legacy, supplier="知楚", buyer="库车市农业农村局", project_id="PRJ-1", quantity=3)
        assert s.with_body("正文").supplier == "知楚"
        marked = s.with_error("boom")
        assert (marked.buyer, marked.project_id, marked.quantity) == ("库车市农业农村局", "PRJ-1", 3)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
