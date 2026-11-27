# -*- coding: utf-8 -*-
"""2026-10-03 补的 2 条品牌别名：博迅系。

背景（证据 = 批2 水槽 run 的落盘记录
``output/e2e-final/runs/b2-waterbath-ssw2s-r4/evidence/16-final-run.json``
里 ``price_rejections`` 中 ``reason_code='brand_mismatch'`` 的那一条）：

    引文 1 | 恒温电热水槽 | 博讯SSW-420-2S | 台 | 3 | \\ | 2981.98
    来源 https://zfcg.czt.zj.gov.cn/portal/detail?articleId=2Nk41SpuPyOoozr28CFzEg%3D%3D
    other_brand = 博迅，目标品牌 = 上海博迅
    detail = 引文中的品牌是 博迅，与目标品牌 上海博迅 不一致。

型号闸已经确认型号就是目标型号 ``SSW-420-2S``，整条真成交单价卡住的只有品牌写法。
本单**没有改判据**：``brand_name_matches`` 的三步逻辑一个字没动，只往
``_BRAND_ALIASES`` 那张显式、一对一的表里加了 2 条登记。

本文件钉住三件事：这条实测记录放行；既有别名一条都不回退；没有登记的写法照样判两家。
"""
import pytest

from backend.services.procurement_rules import brand_name_matches
from backend.services.brand_model_rules import _BRAND_ALIASES

#: 这条的两侧品牌值**逐字**抄自批2 落盘记录（未经改写）。
MEASURED = [
    ('上海博迅', '博迅', '浙江政府采购 zfcg.czt.zj.gov.cn', 'SSW-420-2S', 2981.98),
]


@pytest.mark.parametrize('target,row_brand,where,model,amount', MEASURED)
def test_the_measured_row_is_one_brand_now(target, row_brand, where, model, amount):
    """这条加别名后必须判同一家（改前实测两侧都是 False）。"""
    assert brand_name_matches(row_brand, target) is True, where
    assert brand_name_matches(target, row_brand) is True, where


def test_the_misspelling_dropped_by_the_extractor_is_registered_too():
    """引文自身写的是「博讯」，抽取层落下来的是「博迅」。

    两种写法都会出现在来源里：只登记一个，另一处照样丢。
    """
    assert brand_name_matches('博讯', '上海博迅') is True
    assert brand_name_matches('博讯', '博迅') is True
    assert brand_name_matches('博讯', '上海博迅医疗生物仪器股份有限公司') is True


def test_the_alias_table_did_not_grow_a_prefix_rule():
    """本单是**加登记**，不是放宽判据：没登记的写法仍然判两家。

    ``上海博迅仪器`` 是经销商式写法，``_ENTERPRISE_SUFFIX`` 刻意不写裸的
    ``仪器``（见该表注释），所以它剥不出登记名后缀、也没有别名条目，
    必须继续判成两个品牌——否则任何「上海博迅*」都会被吃进来。
    """
    assert brand_name_matches('上海博迅仪器', '博迅') is False
    assert brand_name_matches('上海博迅仪器', '上海博迅') is False


def test_existing_aliases_do_not_regress():
    """既有的别名两向都必须还成立（防止后续改动把表改坏）。"""
    for spelled, canonical in (('天津泰斯特', '泰斯特'), ('上海一恒', '一恒'),
                               ('湖南湘仪', '湘仪'), ('苏州安泰', '苏净安泰'),
                               ('贝克曼', '贝克曼库尔特'), ('山东新华', '新华')):
        assert brand_name_matches(spelled, canonical) is True, spelled
        assert brand_name_matches(canonical, spelled) is True, spelled


def test_the_two_new_entries_are_in_the_table():
    """登记必须落在 ``_BRAND_ALIASES`` 里，而不是靠判据分支猜出来的。"""
    assert _BRAND_ALIASES['上海博迅'] == '博迅'
    assert _BRAND_ALIASES['博讯'] == '博迅'
