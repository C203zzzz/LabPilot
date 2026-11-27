# -*- coding: utf-8 -*-
"""2026-10-02 补的 4 条品牌别名：贝克曼系与新华系。

背景（证据与逐笔明细见 ``output/brand-alias-fix/report.md``）：历史落盘记录里
因品牌写法差异被判 ``brand_mismatch`` 的真实价格行共 13 笔，其中 11 笔是误拒；
先前已用别名表救回 6 笔，剩下 5 笔仍被误拒——

* 贝克曼系 3 个来源：品牌列写 ``贝克曼``（陕西公告 HTML／中标确认 PDF）与
  ``Beckman Coulter``（yiliaozhaobiao），制造商列／型号证明它们与目标品牌
  ``贝克曼库尔特`` 是同一家；
* 新华系 2 个来源：品牌列写 ``山东新华``（ccgp.gov.cn）与 ``新华医疗``（sohu 转载），
  同型号 ``TQX-300BH``，与目标品牌 ``新华`` 是同一家（山东新华医疗器械股份
  有限公司，证券简称「新华医疗」）。

本单**没有改判据**：``brand_name_matches`` 的三步逻辑一个字没动，
只往 ``_BRAND_ALIASES`` 那张显式、一对一的表里加了 4 条登记。
本文件钉住三件事：这 5 笔放行、已有的 6 笔不回退、真实反例一条都不许被合并
（最关键的是 ``湘仪`` ↔ ``湘潭湘仪``）。
"""
import pytest

from backend.services.procurement_rules import brand_name_matches
from backend.services.brand_model_rules import _BRAND_ALIASES

#: 这 5 笔的两侧品牌值**逐字**抄自历史落盘记录（未经改写）：
#: ``output/acceptance-57-r1r2/cold-run.json``（贝克曼 3 条）、
#: ``output/reverify/refprice-response.json``（新华 2 条，目标品牌取
#: ``run.alternatives[1].brand``）。
MEASURED_FIVE = [
    ('贝克曼库尔特', '贝克曼', '陕西 CCGP 公告 HTML', 'Avanti J-15R', 110000.0),
    ('贝克曼库尔特', '贝克曼', '陕西 CCGP 中标确认 PDF', 'Avanti J-15R', 110000.0),
    ('贝克曼库尔特', 'Beckman Coulter', 'yiliaozhaobiao.com', 'Avanti J-15R', 90000.0),
    ('新华', '山东新华', 'ccgp.gov.cn 202310', 'TQX-300BH', 26500.0),
    ('新华', '新华医疗', 'sohu 转载', 'TQX-300BH', 25000.0),
]


@pytest.mark.parametrize('target,row_brand,where,model,amount', MEASURED_FIVE)
def test_the_five_measured_rows_are_one_brand_now(target, row_brand, where, model, amount):
    """这 5 笔加别名后必须判同一家（改前实测全部为 False，见 cases-before.txt）。"""
    assert brand_name_matches(row_brand, target) is True, where
    assert brand_name_matches(target, row_brand) is True, where


#: 先前 6 笔已用别名表救回的写法，本单不许回退。
ALREADY_FIXED = [
    ('苏净安泰', '苏州安泰'),
    ('Leica', '徕卡'),
    ('上海申安', '申安'),
    ('SHENAN', '上海申安'),
]


@pytest.mark.parametrize('target,row_brand', ALREADY_FIXED)
def test_the_six_previously_fixed_rows_do_not_regress(target, row_brand):
    assert brand_name_matches(row_brand, target) is True


#: **真实反例**：判成同一家就是把别家的价安到这台机器上，后果比少一条价严重得多。
#: 前两组来自真实语料（``湘仪`` 一族、``洁康``/``深圳捷康`` 那笔 300 元成交），
#: 其余是同类形态的易混写法。
COUNTEREXAMPLES = [
    ('湘仪', '湘潭湘仪'),                       # 湘潭做天平，湖南湘仪做离心机
    ('卢湘仪', '湘潭湘仪'),
    ('湘仪', '(卢)湘仪/湘鑫'),                  # 一格并列写两家
    ('湘仪', 'cence 湘仪'),
    ('湘仪', '湘智'),
    ('湘仪', '湖南凯达'),
    ('洁康', '深圳捷康'),                       # 读音近、字号不同
    ('新华', '新华制药'),                       # 简称撞车
    ('新华', '新华书店'),
    ('新华', '新华招标'),
    ('新华', '新华医疗设备'),                   # 多一个字就不是登记过的写法
    ('新华', '新华医疗器械厂'),
    ('山东新华', '新华制药'),                   # 别名只把写法归到「新华」，不带前缀通配
    ('新华医疗', '新华制药'),
    ('贝克曼', '贝克曼仪器'),                   # 同字号但未登记
    ('Beckman Coulter', 'Beckman Coulter Co.,Ltd'),   # 英文后缀未登记
    ('海尔', '海尔生物'),                       # 同集团不同子品牌/法人
    ('Thermo Fisher', 'Thermo Scientific'),     # 集团名 vs 品牌线
    ('Thermo Fisher', 'Thermo Fisher Scientific'),
    ('申安', '三申'),                           # 上海三申 vs 上海申安
    ('上海精宏实验设备有限公司', '上海精宏实验技术有限公司'),  # 同字号两个登记名
    ('苏净安泰', '江苏苏净'),
    ('艾本德', 'Eppendorf'),
    ('Leica', '徕卡等'),                        # 「等」= 并列多品牌、整包价
]


@pytest.mark.parametrize('left,right', COUNTEREXAMPLES)
def test_the_counterexamples_are_still_two_brands(left, right):
    """反例一条都不许被合并——尤其 ``湘仪`` ↔ ``湘潭湘仪``。"""
    assert brand_name_matches(left, right) is False


#: **既有**企业名桥（一侧是另一侧的企业登记名、短名是全名子串）放行的对：
#: 改前就是 True，本单没动它，列在这里只是防止有人把它算到本单账上。
EXISTING_ENTERPRISE_BRIDGE = [
    ('新华', '新华医疗器械'),
    ('新华医疗', '新华医疗器械'),
    ('新华', '山东新华医疗器械股份有限公司'),
    ('贝克曼库尔特', '贝克曼库尔特生物科技（苏州）有限公司'),
    ('一恒', '成都一恒科技有限公司'),
    ('湘仪', '长沙高新开发区湘仪天平仪器设备有限公司'),
]


@pytest.mark.parametrize('left,right', EXISTING_ENTERPRISE_BRIDGE)
def test_the_existing_enterprise_name_bridge_is_unchanged(left, right):
    """这条桥是既有判据（``_looks_like_enterprise_name`` + 子串），本单一行没改。

    其中 ``湘仪`` ↔ 长沙那家天平公司是**误放行**，但它改前就是 True，
    归 ``设计规范``「同行登记全名做桥」那一单评估，不在本单范围。
    """
    assert brand_name_matches(left, right) is True


def test_sartorius_is_registered_as_a_same_maker_spelling():
    """``赛多利斯`` ↔ ``Sartorius``：2026-10-07 起判同一家（M2）。

    它本来压在 ``COUNTEREXAMPLES`` 里（"中英对照但没登记"）。M2 把斜杠复合写法
    拆开逐段比之后，台账 ``b1-balance-fix-r1`` 那条
    ``1 | 赛多利斯 赛多利斯电子天平BSA224S | 赛多利斯/sartorius | 型号:BSA224S``
    的第二段正是 ``sartorius``，没有别名条目时这一段落不了地。登记它的依据与表里
    其它条目同一档（准入标准见 ``_BRAND_ALIASES`` 头部）：

    * 厂商自己的官网并列写两种名字（https://www.sartorius.com.cn/ ，
      站名与产品页一律「赛多利斯 Sartorius」）；
    * 语料内自证：上面那行引文同一行的第二列就是这两种写法，型号 ``BSA224S``
      是这家机型。

    仍然不许连带放行的：别的显微镜/天平品牌的英文写法（``Eppendorf`` 下面那条
    反例仍在），以及 ``Sartorius`` 之外未登记的英文变体。
    """
    assert brand_name_matches('赛多利斯', 'Sartorius') is True
    assert brand_name_matches('Sartorius', '赛多利斯') is True
    assert brand_name_matches('赛多利斯/sartorius', '赛多利斯') is True
    assert brand_name_matches('赛多利斯', 'sartorius') is True
    # 没登记的那一类仍然拒
    assert brand_name_matches('赛多利斯', 'Sartorius Scientific') is False
    assert brand_name_matches('赛多利斯', 'Sartorius AG') is False


def test_the_alias_table_stays_an_explicit_enumerated_table():
    """别名表是显式枚举，不是模糊匹配：检查它不是"规则"形态。

    * 键与值都必须是 ``identity()`` 之后的写法（不能带空格/连字符/大写）；
    * 不许出现通配符、不许出现"剥掉某部分再比"的标记；
    * 每条的值必须自己不是键（一对一，不许链式归一）。
    """
    from backend.services.procurement_rules import identity

    for key, value in _BRAND_ALIASES.items():
        assert key == identity(key), f'key {key!r} 不是 identity() 之后的写法'
        assert value == identity(value), f'value {value!r} 不是 identity() 之后的写法'
        assert '*' not in key and '*' not in value, f'别名表里不许有通配符：{key!r} → {value!r}'
        assert value not in _BRAND_ALIASES, f'{value!r} 自身又是键，别名表不许链式归一'


def test_newly_added_entries_are_exactly_the_four_measured_ones():
    """本单新增的 4 条，逐条在此点名（将来再加条目必须同步改这里）。"""
    added = {
        '贝克曼': '贝克曼库尔特',
        'beckmancoulter': '贝克曼库尔特',
        '山东新华': '新华',
        '新华医疗': '新华',
    }
    for key, value in added.items():
        assert _BRAND_ALIASES.get(key) == value, f'{key!r} 的登记被改动或删除'
    # 规范写法自身不许再是键（否则会链式归一）。
    for value in ('贝克曼库尔特', '新华'):
        assert value not in _BRAND_ALIASES
