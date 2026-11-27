"""M2：品牌值的「斜杠复合写法」，以及判不了时问模型一次的那一层。

背景与逐条证据见 ``backend/services/brand_spelling.py`` 的模块文档；
被救回的真实记录是台账 ``b3-opt-balance-mt-me204e`` 里那条 14,600 元的成交价
（``回归基线`` Q9）。

判据的立场是**只认写法、不认知识**：斜杠复合值拆开逐段全等，任一段对上就算
同一家；不许退化成"包含即相同"。下面每个反例都对应一条真实或既有测试锁定的
行为，改判据时先看它们。
"""
from __future__ import annotations

import json

import pytest

from backend.services.brand_spelling import (
    _reset_for_tests, ask_model, judge_enabled, learned_path, load_learned,
    lookup, may_be_same_brand, same_brand_by_segments, segment_tokens, set_judge)
from backend.services.procurement_rules import BRAND_MISMATCH, brand_identity, brand_name_matches
from backend.services.brand_model_rules import _BRAND_ALIASES


@pytest.fixture(autouse=True)
def _learned_isolated(tmp_path, monkeypatch):
    """落盘台账放到临时目录；每个用例自带一个"从没问过"的进程状态。"""
    monkeypatch.setenv('LABPILOT_BRAND_LEARN_DIR', str(tmp_path))
    monkeypatch.delenv('LABPILOT_BRAND_LEARN', raising=False)
    _reset_for_tests()
    yield
    _reset_for_tests()


# ---------------------------------------------------------------------------
# 1. 逐段拆分：该救回的
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('left,right', [
    ('梅特勒托利多/Mettler-Toledo', '梅特勒-托利多'),   # 14600 那条的两种写法
    ('梅特勒-托利多', '梅特勒托利多/Mettler-Toledo'),
    ('梅特勒托利多/Mettler-Toledo', '梅特勒托利多'),
    ('梅特勒托利多/mettler-toledo', '梅特勒托利多'),
    ('赛多利斯/sartorius', '赛多利斯'),                  # 8980 那条的两种写法
    ('赛多利斯/Sartorius', '赛多利斯'),
    ('佐藤/SATO', '佐藤'),                              # 形态相同的构造例
    ('梅特勒托利多 ／ Mettler-Toledo', '梅特勒 托利多'),  # 全角斜杠 + 词内空格
])
def test_a_slash_compound_spelling_is_one_brand(left, right):
    assert brand_name_matches(left, right) is True


# ---------------------------------------------------------------------------
# 2. 逐段拆分：一条都不许放过的
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('left,right', [
    # 一格并列两个品牌，且**两侧都是斜杠值**——不拆
    ('(卢)湘仪/湘鑫', '湘仪'),
    ('(卢)湘仪/湘鑫', '湘鑫'),
    ('(卢)湘仪/湘鑫', '湖南湘仪'),
    ('(卢)湘仪/湘鑫', '卢湘仪'),
    # 一格并列两个品牌，但**纯中文**（没有"中文/English"这个形状）
    ('梅特勒/托利多', '梅特勒'),
    ('梅特勒/托利多', '托利多'),
    # 字面包含不是依据：整段不相等就不放行
    ('A/赛多利斯', '赛多利斯'),                  # 单字母段不是品牌写法
    ('X/梅特勒托利多', '梅特勒托利多'),
    ('赛多利斯/某小厂', '赛多利斯'),
    ('梅特勒', '梅特勒-托利多'),
    ('梅特勒托利多', '梅特勒'),
    # 型号残渣当品牌段
    ('A/BSC-1604', 'BSC-1604'),
    # 两侧都是英文时不猜（没有中文那一侧）
    ('Mettler/Toledo', 'Mettler'),
    ('Sartorius/BSA224S', 'Sartorius'),
])
def test_a_compound_spelling_never_widens_into_containment(left, right):
    assert brand_name_matches(left, right) is False


def test_the_split_judgement_itself_is_narrow():
    """判据层面的三个反例（不走别名表也必须是 False）。"""
    assert same_brand_by_segments('(卢)湘仪/湘鑫', '湘鑫', aliases=_BRAND_ALIASES) is False
    assert same_brand_by_segments('梅特勒/托利多', '托利多', aliases=_BRAND_ALIASES) is False
    assert same_brand_by_segments('A/BSC-1604', 'BSC-1604', aliases=_BRAND_ALIASES) is False
    assert same_brand_by_segments('梅特勒', '梅特勒-托利多', aliases=_BRAND_ALIASES) is False
    # 两侧都带斜杠 / 都不带斜杠：一律不拆
    assert same_brand_by_segments('A/B', 'A/B', aliases=_BRAND_ALIASES) is False
    assert same_brand_by_segments('赛多利斯', '赛多利斯', aliases=_BRAND_ALIASES) is False


def test_segments_are_normalized_the_same_way_as_identity():
    assert segment_tokens('梅特勒托利多/Mettler-Toledo') == ('梅特勒托利多', 'mettlertoledo')
    assert segment_tokens('赛多利斯 ／ sartorius') == ('赛多利斯', 'sartorius')
    assert segment_tokens('梅特勒托利多') == ()


# ---------------------------------------------------------------------------
# 3. 真成交价那条：走完整的拒收解释路径
# ---------------------------------------------------------------------------

#: 逐字取自台账 ``b3-opt-balance-mt-me204e/evidence/16-final-run.json`` 的
#: ``price_rejections[0]``（``bank.anhui...`` 那条）与 ``sources[]`` 里同一 URL 的正文。
_ANHUI_URL = ('https://mall.anhui.zcygov.cn/luban/announcement/detail'
              '?encryptId=b/Dd6w+PcsjCSIJTHfO09ITwhJI3yMqKEmeFXDD+K4I=&district=349900')
_ANHUI_EXCERPT = ('1 | 梅特勒托利多 Me204E 万分之一电子天平 | 梅特勒托利多/Mettler-Toledo | '
                  '梅特勒 托利多ME204E | 1 | 14600.0 | 14600.0')
_ANHUI_BODY = (
    '中标（成交）结果公告\n'
    '四、成交标的名称、规格型号、数量、单价、成交金额:\n'
    '序号 | 标的名称 | 品牌 | 规格型号 | 数量 | 单价(元) | 成交金额（元）\n'
    '1 | 梅特勒托利多 Me204E 万分之一电子天平 | 梅特勒托利多/Mettler-Toledo | '
    '梅特勒 托利多ME204E | 1 | 14600.0 | 14600.0\n'
)


def test_the_14600_balance_price_passes_the_price_gate_itself():
    """直接调 ``validate_price``：这条必须**返回报价**，不能只是换个理由拒。

    台账重放（``output/m2-replay/``）先暴露的坑：品牌闸放行之后，价格闸内部
    还有一道"引文与重新定位到的表行对不对得上"的核对，那里原先用
    ``brand_identity(a) == brand_identity(b)`` 比写法，斜杠复合值在这一步又被
    判成两家，整条变成 ``excerpt_not_in_source``。所以这条断言必须落在
    ``validate_price`` 本身，而不是只看拒收理由。
    """
    from backend.services.procurement_rules import validate_price

    row = {
        'kind': 'award', 'brand': '梅特勒托利多/Mettler-Toledo', 'model': 'ME204E',
        'amount': 14600.0, 'amount_unit': '元', 'amount_scope': 'unit', 'quantity': 1,
        'excerpt': _ANHUI_EXCERPT,
    }
    source = {'url': _ANHUI_URL, 'body_status': 'fetched', 'source_type': 'tender_document',
              'body_text': _ANHUI_BODY}
    refusals: dict = {}
    quote = validate_price(row, source, {'brand': '梅特勒-托利多', 'model': 'ME204E'}, refusals)
    assert quote is not None, f'14600 那条没通过价格闸：{refusals}'


def test_the_citation_cross_check_compares_makers_not_identity_strings():
    """同一性比较用 ``brand_name_matches``：写法不同、同一家时要能对上。"""
    from backend.services.procurement_rules import same_brand_value

    assert same_brand_value('梅特勒托利多/Mettler-Toledo', '梅特勒托利多') is True
    assert same_brand_value('赛多利斯/sartorius', '赛多利斯') is True
    assert same_brand_value('苏净安泰', '江苏富泰') is False
    assert same_brand_value('(卢)湘仪/湘鑫', '湘仪') is False


def test_the_14600_balance_price_is_no_longer_refused_over_spelling():
    """系统原话：引文中的品牌是 梅特勒托利多/Mettler-Toledo，与目标品牌
    梅特勒-托利多 不一致。这条是误拒，本应收下。"""
    from backend.services.price_rejections import explain_rejection

    row = {
        'kind': 'award', 'brand': '梅特勒托利多/Mettler-Toledo', 'model': 'ME204E',
        'amount': 14600.0, 'amount_unit': '元', 'amount_scope': 'unit', 'quantity': 1,
        'excerpt': _ANHUI_EXCERPT,
    }
    source = {'url': _ANHUI_URL, 'body_status': 'fetched', 'source_type': 'tender_document',
              'body_text': _ANHUI_BODY}
    reason = explain_rejection(row, source, {'brand': '梅特勒-托利多', 'model': 'ME204E'})
    assert reason.get('reason_code') != BRAND_MISMATCH, f'14600 那条仍被品牌判据拒：{reason}'


def test_the_sartorius_row_is_no_longer_refused_over_spelling():
    """台账 ``b1-balance-fix-r1``：目标品牌 ``赛多利斯``，引文品牌写
    ``赛多利斯/sartorius``（同一行第二列），被拒理由同样是品牌不一致。

    这里的行用产品自己的表格解析器从正文重建（与真实管线一致）：
    ``8台`` 这种数量写法只有那一条路径解析得出，手写 ``quantity=8`` 会给出一条
    产品永远不会喂进去的输入。
    """
    from backend.services.price_rejections import explain_rejection
    from backend.services.procurement_rules import validate_price
    from backend.services.procurement_tables import price_table_rows

    excerpt = ('1 | 赛多利斯 赛多利斯电子天平BSA224S | 赛多利斯/sartorius | '
               '型号:BSA224S | 8台 | 8980.00 | 71840.00 | -')
    # 正文照台账里那一段的关键部分重建：表头（产品自己的表格解析器靠它认列）
    # 加数据行；``8台`` 这种数量写法只有走那条路径才解析得出。
    body = ('中标（成交）结果公告\n'
            '三、成交信息\n'
            '成交日期： 2022年11月24日\n'
            '总成交金额： 7.184 （万元）\n'
            '成交供应商名称、联系地址及成交金额：\n'
            '序号 | 成交供应商名称 | 成交供应商地址 | 成交金额（元）\n'
            '1 | 杭州科穹自动化科技有限公司 | 浙江省杭州市余杭区五常街道后山路18号 | 71840.0\n'
            '四、成交标的名称、规格型号、数量、单价、成交金额\n'
            '序号 | 标的名称 | 品牌 | 规格型号 | 数量 | 单价(元) | 成交金额（元） | 报价明细\n'
            + excerpt + '\n')
    target = {'brand': '赛多利斯', 'model': 'BSA224S'}
    row = next(({'kind': 'award', **parsed} for parsed in price_table_rows(body, 'BSA224S')
                if excerpt in (parsed.get('excerpt') or '')), None)
    assert row is not None, '产品自己的表格解析器没能把这一行读出来'
    assert row['brand'] == '赛多利斯/sartorius', row
    source = {'url': 'https://www.ccgp.gov.cn/example.htm', 'body_status': 'fetched',
              'source_type': 'tender_document', 'body_text': body}
    reason = explain_rejection(row, source, target)
    assert reason.get('reason_code') != BRAND_MISMATCH, f'8980 那条仍被品牌判据拒：{reason}'
    refusals: dict = {}
    assert validate_price(row, source, target, refusals) is not None, \
        f'8980 那条没通过价格闸：{refusals}'


def test_the_two_brand_cell_still_trips_the_brand_gate():
    """既有测试锁定的行为不许被 M2 改掉：``(卢)湘仪/湘鑫`` 是"一格并列两家"。"""
    from backend.services.price_rejections import explain_rejection

    excerpt = ('血型专用离心机 | (卢)湘仪/湘鑫 | XKA - 2200 | 木垒哈萨克自治县人民医院 | '
               '新疆新丝路惠康医疗科技有限公司 | 2026-05-19 | 1.75万元 | 查看')
    row = {'kind': 'award', 'brand': '(卢)湘仪/湘鑫', 'model': 'XKA-2200',
           'amount': 17500.0, 'amount_unit': '元', 'amount_scope': 'unit', 'quantity': 1,
           'excerpt': excerpt}
    source = {'url': 'https://www.120shop.cn/i890850380', 'body_status': 'fetched',
              'source_type': 'tender_document', 'body_text': excerpt + '\n'}
    reason = explain_rejection(row, source, {'brand': '湘仪', 'model': 'XKA-2200'})
    assert reason.get('reason_code') == BRAND_MISMATCH


# ---------------------------------------------------------------------------
# 4. 判不了的一对：问模型一次，结论连证据落盘
# ---------------------------------------------------------------------------

def test_learning_is_off_by_default_so_nothing_is_ever_asked(monkeypatch):
    calls = []
    set_judge(lambda left, right: calls.append((left, right)) or {'same': True})
    assert judge_enabled() is False
    assert brand_name_matches('梅特勒', '梅特勒-托利多') is False
    assert calls == []
    assert not learned_path().exists()


def test_a_model_answer_is_registered_and_only_ever_asked_once(monkeypatch):
    monkeypatch.setenv('LABPILOT_BRAND_LEARN', '1')
    calls = []

    def judge(left, right):
        calls.append((left, right))
        return {'same': True, 'canonical': '梅特勒托利多',
                'basis': 'METTLER TOLEDO 的中文名就是梅特勒-托利多',
                'evidence': ['https://www.mt.com/cn/zh/home.html']}

    set_judge(judge)
    assert brand_name_matches('梅特勒', '梅特勒-托利多') is True
    assert calls == [('梅特勒', '梅特勒-托利多')]

    # 结论连证据落进了台账
    records = load_learned()
    assert len(records) == 1
    record = next(iter(records.values()))
    assert record['same'] is True
    assert record['canonical'] == '梅特勒托利多'
    assert record['evidence'] == ['https://www.mt.com/cn/zh/home.html']

    # 同一条不再问第二遍，而且别名表已经能直接命中（"下次直接命中"）
    assert brand_name_matches('梅特勒', '梅特勒-托利多') is True
    assert brand_name_matches('梅特勒-托利多', '梅特勒') is True
    assert calls == [('梅特勒', '梅特勒-托利多')]
    assert brand_identity('梅特勒') == brand_identity('梅特勒托利多')


def test_a_different_maker_answer_is_remembered_but_never_registered(monkeypatch):
    monkeypatch.setenv('LABPILOT_BRAND_LEARN', '1')
    calls = []
    set_judge(lambda left, right: calls.append((left, right)) or {
        'same': False, 'canonical': None, 'basis': '两家公司', 'evidence': []})
    assert brand_name_matches('梅特勒', '梅特勒-托利多') is False
    assert lookup('梅特勒托利多', '梅特勒')['same'] is False     # 顺序无关，同一条
    assert brand_name_matches('梅特勒', '梅特勒-托利多') is False
    assert calls == [('梅特勒', '梅特勒-托利多')]                 # 没问第二遍
    # 没登记进别名表：两个写法仍然各是各的
    assert brand_identity('梅特勒') != brand_identity('梅特勒托利多')


def test_shapes_that_do_not_look_like_one_maker_are_never_asked(monkeypatch):
    """没有形态关联的一对（一个词都不共用）不值得花一次模型调用。"""
    monkeypatch.setenv('LABPILOT_BRAND_LEARN', '1')
    calls = []
    set_judge(lambda left, right: calls.append((left, right)) or {'same': True})
    assert brand_name_matches('苏净安泰', '江苏富泰') is False
    assert brand_name_matches('申安', '知楚') is False
    assert brand_name_matches('赛多利斯', '赛多利斯科学仪器（北京）') is False   # 企业登记名形态
    assert calls == []
    assert not learned_path().exists()


def test_a_failed_ask_keeps_the_original_verdict(monkeypatch):
    """问不成（没凭据/网络/额度）时，判定与今天逐字相同，且不落"不同一家"。"""
    monkeypatch.setenv('LABPILOT_BRAND_LEARN', '1')
    assert may_be_same_brand('梅特勒', '梅特勒-托利多') is True   # 形态上确实像
    assert judge_enabled() is True
    assert ask_model('梅特勒', '梅特勒-托利多') is None            # 没注入判官、也没凭据
    assert brand_name_matches('梅特勒', '梅特勒-托利多') is False
    assert load_learned() == {}


def test_the_learned_table_is_read_as_aliases_and_survives_a_restart(monkeypatch):
    """落盘之后，**新进程**（这里用清空进程内状态模拟）也直接命中，不再问。"""
    monkeypatch.setenv('LABPILOT_BRAND_LEARN', '1')
    set_judge(lambda left, right: {'same': True, 'canonical': '艾本德',
                                   'basis': 'Eppendorf 中文名艾本德', 'evidence': []})
    assert brand_name_matches('艾本德', 'Eppendorf') is True
    _reset_for_tests()                       # 进程内状态清空，只留落盘
    assert brand_name_matches('艾本德', 'Eppendorf') is True
    assert brand_name_matches('Eppendorf', '艾本德') is True
    assert brand_identity('Eppendorf') == brand_identity('艾本德')


def test_the_learned_file_stays_out_of_the_repository():
    """台账落在进程临时目录，不写仓库、不写共享的 ``output/``。"""
    path = learned_path()
    assert path.name == 'labpilot-brand-learned.json'
    assert str(path).startswith(str(path.parent))
    assert 'output' not in [part.lower() for part in path.parts]
