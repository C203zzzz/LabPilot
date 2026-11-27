# -*- coding: utf-8 -*-
"""本地成交明细库接进**候选发现**的回归（2026-10-05）。

钉住五件事：品类只认商品名（不拿型号列匹配）、尺寸当型号的行挡掉、品牌列粘着的品目
去掉、排除基线品牌与已在列型号、一个品牌只出一条；库不在／品类词认不出要如实说。
外加一条端到端：``alternatives()`` 真把它接上了，且线索走的是同一套下游。
"""
import copy
import sqlite3

import pytest

from backend.services import xlyg_local_db as xdb
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture


def _row(model, brand, *, goods='立式压力蒸汽灭菌器', product='其他教学用仪器',
         price=49300.0, kind='negotiated', date='2026-09-21',
         order='DD20260921000001', line=1, verified=1, url=None):
    """一行 ``xlyg_item``，列顺序与 ``test_xlyg_price_channel`` 保持一致。"""
    column = '协议价格（元）' if kind == 'negotiated' else '中标价'
    return (order, line, goods, product, brand, model, 1.0, price, price,
            column, kind, '灭菌器', date, '华南农业大学', '某某仪器有限公司',
            url or f'https://gdegp.gds.edu.cn:8093/Upgrade/Publicity/ResultPublicity/x{order}.aspx',
            f'{goods} | {product} | {brand} | {model} | {price:.2f} | 1.00',
            f'商品名称 | 产品名称 | 品牌 | 型号规格 | {column} | 数量',
            f'{model} | {price:.2f} | 1.00 | {brand}', verified)


def _build_db(tmp_path, rows):
    path = tmp_path / 'xlyg_prices.sqlite3'
    conn = sqlite3.connect(path)
    conn.executescript(xdb.schema_sql())
    conn.executemany(
        'INSERT INTO xlyg_item (order_no, line_no, goods_name, product_name, brand, model, '
        'quantity, unit_price, amount, price_column, price_kind, category, deal_date, '
        'purchaser, supplier, detail_url, excerpt, table_header, price_excerpt, verified) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', rows)
    conn.commit()
    conn.close()
    return path


# --------------------------------------------------------------- 取材口径

def test_category_is_matched_on_the_goods_name_not_the_model_column(tmp_path):
    """``goods_name=旋光管``、``model=长20cm,带水浴夹套`` 不许因为型号里带品类词被捞进来。

    实测依据：库里 11 个品类词下，"只写在型号列里"的行一条也没有，而拿型号匹配会把
    这条整行捞出来（另有 ``水槽/三联水龙头 | 1000*1000*850`` 这类）。
    """
    path = _build_db(tmp_path, [
        _row('SJ511C', '雅马拓', goods='立式压力蒸汽灭菌器'),
        _row('长20cm,带水浴夹套', '新航仪器', goods='旋光管', product='旋光管',
             order='DD2', line=1),
    ])
    out = xdb.local_candidate_leads(['灭菌器'], database_path=path)
    assert [lead['model'] for lead in out['leads']] == ['SJ511C']


def test_dimension_shaped_models_are_refused(tmp_path):
    """型号列写的是外形尺寸（``1600×800×850/钢木结构``）→ 不是型号，不拿来搜产品页。"""
    path = _build_db(tmp_path, [
        _row('1600×800×850/钢木结构', '实验室家具(广州赛亚)', goods='洁净工作台',
             product='实验室家具', order='DD1'),
        _row('1000*1000*850', '煜鸿', goods='洁净工作台', order='DD2'),
        _row('2000×600×850/带箱体/钢木结构', '广州赛亚', goods='洁净工作台', order='DD3'),
        _row('CA-1400', '上海上净', goods='垂直层流洁净工作台', order='DD4'),
    ])
    out = xdb.local_candidate_leads(['洁净工作台'], database_path=path)
    assert [lead['model'] for lead in out['leads']] == ['CA-1400']


def test_dimension_filter_keeps_real_models_that_look_numeric(tmp_path):
    """``5425``（艾本德离心机）是数字型号，没有分隔符，不能被当成尺寸挡掉。"""
    assert xdb._is_dimension_model('5425') is False
    assert xdb._is_dimension_model('5425R') is False
    assert xdb._is_dimension_model('DK-98-Ⅱ单列两孔') is False
    assert xdb._is_dimension_model('LS-75HD(原LS-B75L-I数显）') is False
    assert xdb._is_dimension_model('1000*1000*850') is True
    assert xdb._is_dimension_model('1500*1400*1200mm') is True
    assert xdb._is_dimension_model('1200*700*1600 /全钢标准型') is True


def test_category_suffix_on_the_brand_column_is_removed(tmp_path):
    """库里的品牌列会把**品目**印在后面：``海尔(其他教学用仪器)``。

    判据只认"括号里那句恰好等于这一行的 ``product_name``"：``华志（福建）``、
    ``梅特勒-托利多(METTLER TOLEDO)`` 这类写法原样保留。
    """
    assert xdb._strip_category_suffix('海尔(其他教学用仪器)', '其他教学用仪器') == '海尔'
    assert xdb._strip_category_suffix('知楚(其他教学用仪器)', '其他教学用仪器') == '知楚'
    assert xdb._strip_category_suffix('华志（福建）', '电子天平') == '华志（福建）'
    assert (xdb._strip_category_suffix('梅特勒-托利多(METTLER TOLEDO)', '电子天平')
            == '梅特勒-托利多(METTLER TOLEDO)')
    assert (xdb._strip_category_suffix('实验室家具(广州赛亚)', '实验室家具')
            == '实验室家具(广州赛亚)')
    path = _build_db(tmp_path, [
        _row('LX-165T2R', '海尔(其他教学用仪器)', goods='冷冻离心机', order='DD1'),
    ])
    assert [lead['brand'] for lead in xdb.local_candidate_leads(
        ['冷冻离心机'], database_path=path)['leads']] == ['海尔']


def test_only_deal_prices_and_verified_rows_are_used(tmp_path):
    """挂牌价（``list_price_direct``）与没过闸门（``verified=0``）的行都不是"成交过的机器"。"""
    path = _build_db(tmp_path, [
        _row('SJ511C', '雅马拓', kind='negotiated'),
        _row('SQ810C', '雅马拓', kind='list_price_direct', order='DD2'),
        _row('GI29', '致微', verified=0, order='DD3'),
    ])
    out = xdb.local_candidate_leads(['灭菌器'], database_path=path)
    assert [lead['model'] for lead in out['leads']] == ['SJ511C']


def test_each_brand_appears_once_with_its_most_recent_model(tmp_path):
    """一个品牌只出一条：候选要的是"**别的品牌**能替代的机器"。

    实测依据：灭菌器按（品牌，型号）取前 6 条只覆盖 4 个品牌（申安两条、滨江医疗两条）。
    """
    path = _build_db(tmp_path, [
        _row('LS-28HD', '滨江医疗', date='2026-04-22', order='DD1'),
        _row('LS-75HD', '滨江医疗', date='2026-07-15', order='DD2'),
        _row('SJ511C', '雅马拓', date='2026-09-21', order='DD3'),
        # 同一个品牌的另一种写法（``上海一恒`` / ``一恒`` 在库里都有）也只是一个品牌。
        _row('HWS-26', '上海一恒', goods='电热恒温水浴锅', date='2026-07-14', order='DD4'),
        _row('HWS-28', '一恒', goods='电热恒温水浴锅', date='2026-08-01', order='DD5'),
    ])
    out = xdb.local_candidate_leads(['灭菌器', '恒温水浴'], database_path=path)
    assert [lead['model'] for lead in out['leads']] == ['SJ511C', 'LS-75HD', 'HWS-28']


def test_baseline_brand_and_listed_models_are_excluded(tmp_path):
    """排除基线品牌（含库里的另一种写法）与已经在列的型号。"""
    path = _build_db(tmp_path, [
        _row('HVE-50', 'HIRAYAMA', date='2026-02-04', order='DD1'),
        _row('SJ511C', '雅马拓Yamato', date='2026-09-21', order='DD2'),
        _row('DSX-24L-I', '申安', date='2026-07-16', order='DD3'),
        _row('GMJ-80L-I', '上海申安', date='2026-06-18', order='DD4'),
    ])
    out = xdb.local_candidate_leads(
        ['灭菌器'], exclude_models=['DSX-24L-I'], exclude_brands=['HIRAYAMA', '申安'],
        database_path=path)
    # HIRAYAMA 是基线品牌；申安/上海申安 是同一个品牌，连它的第二个型号也一并让开。
    assert [lead['model'] for lead in out['leads']] == ['SJ511C']


def test_leads_carry_their_library_provenance(tmp_path):
    """线索要带着"从库里哪一行来"的原样信息，下游合并与展示都靠它。"""
    path = _build_db(tmp_path, [_row('SJ511C', '雅马拓')])
    lead = xdb.local_candidate_leads(['灭菌器'], database_path=path)['leads'][0]
    assert lead['proposed_by'] == 'local_db'
    assert lead['configuration'] == ''          # 库里没有配置列，不编
    assert lead['url'].startswith('https://gdegp.gds.edu.cn:8093/')
    assert 'SJ511C' in lead['excerpt']          # 引文是那一行的原文
    assert lead['library']['channel'] == 'xlyg_local_db'
    assert lead['library']['unit_price'] == 49300.0
    assert lead['library']['deal_date'] == '2026-09-21'
    assert '单一省份' in lead['library']['scope_note']


# --------------------------------------------------------------- 缺口口径

def test_missing_library_is_reported_not_silent(tmp_path):
    out = xdb.local_candidate_leads(['灭菌器'], database_path=tmp_path / 'nope.sqlite3')
    assert out['leads'] == []
    assert '未生效' in out['note']


@pytest.mark.parametrize('words', [[], [''], ['器'], ['  ']])
def test_unusable_category_word_is_reported_not_silent(words):
    """一个字（"柜""台"）会把整库捞成一锅，认不出品类就说这一路没生效。"""
    out = xdb.local_candidate_leads(words)
    assert out['leads'] == []
    assert '未生效' in out['note']


def test_limit_caps_the_leads(tmp_path):
    path = _build_db(tmp_path, [
        _row(f'M{i}', f'厂{i}', order=f'DD{i}') for i in range(5)
    ])
    out = xdb.local_candidate_leads(['灭菌器'], limit=2, database_path=path)
    assert len(out['leads']) == 2


# ------------------------------------------------------- 端到端：车道真的接上了

STERILIZER_BASELINE = {'brand': 'HIRAYAMA', 'model': 'HVE-50', 'category': '灭菌器',
                       'scenario': '采购一台立式压力蒸汽灭菌器', 'parameters': []}


def _page(url, text, purpose='discovery'):
    return {'url': url, 'purpose': purpose, 'body_status': 'fetched',
            'source_type': 'manufacturer_website', 'body_text': text}


#: 发现页地址**故意取一个字典序排在 ``https://gdegp`` 之后**的域名。库线索的地址是
#: 8093 的成交页（``https://gdegp.gds.edu.cn:8093/...``），而选名额的排序键里含地址字符串
#: ——地址 alphabet 靠前的候选会先被选中。真跑 ``b3-r2-local-leads`` 就是这么翻的：
#: 库线索把 3 个核验名额全占了，能核验的候选一条没轮到，``alternatives`` 空掉。这里把
#: alphabet 摆成对它不利的方向，才能测出"名额不许给过不了闸门的候选"这条要求本身。
DISCOVERY_PAGE = _page('https://www.zhili-example.com/lm.html',
                       '立式压力蒸汽灭菌器 乙厂 LM-100 技术参数')


def test_alternatives_does_not_pull_candidates_from_the_library(
        research, monkeypatch, tmp_path):  # noqa: F811 - pytest fixture
    """本地成交明细库**不接候选发现**（设计规范决定）。

    库里只有成交明细、没有任何配置列，所以补进来的线索配置只能靠出去检索产品页；真跑
    ``b3-r3/r4-local-leads`` 两轮里 6 条线索**全部**停在 ``url_missing``、配置一格没填上
    （给了 2 次保底检索、命中率 0），而且它们的地址是 8093 的成交页、不在车道开头那份
    ``sources`` 快照里，**结构上进不了核验名额**——只能"被看到"、不能"被比价"。

    这条用例钉住"撤掉了"：库里明明有 3 条同品类他牌型号（SJ511C／DSX-24L-I／FE5401），
    候选列表里也不许出现它们；同时**能核验的候选照样进得了名额**（撤掉之后
    ``alternatives`` 不该再被过不了闸门的线索占空）。
    """
    from backend.services.procurement_research import ON_DEMAND_ENV

    monkeypatch.setenv(ON_DEMAND_ENV, '0')
    library = _build_db(tmp_path, [
        _row('SJ511C', '雅马拓Yamato', date='2026-09-21'),
        _row('DSX-24L-I', '申安', date='2026-07-16', order='DD2'),
        _row('FE5401', '福之鼎', date='2026-04-28', order='DD4'),
        _row('HVE-50', 'HIRAYAMA', date='2026-02-04', order='DD3'),
    ])
    monkeypatch.setenv('LAB_XLYG_DB', str(library))

    service, run, _, _ = research
    run.update({'baseline': dict(STERILIZER_BASELINE),
                'sources': [copy.deepcopy(DISCOVERY_PAGE)],
                'alternatives': [], 'discovered_candidates': [], 'reference_candidates': [],
                'prices': {}, 'gaps': [], 'failures': []})

    def fake_model(run_, prompt, data):
        if '其他仪器' in prompt:
            # ``configuration`` 不能空：2026-10-06 起"找不到配置的候选不展示"
            # （``tests/test_m4_pipeline.py``），空配置的候选会在 ``alternatives()`` 收尾时被
            # 撤下展示名单，于是下面"能核验的候选照样进得了名额"那条断言拿到的是空名单——
            # 那会让 ``not any(proposed_by == 'local_db')`` 这类守卫在空名单上永远为真。
            # 这一段测的是"候选不从本地成交明细库来"，所以给一段**来源正文里逐字存在**的
            # 配置（``DISCOVERY_PAGE['body_text']`` 里就有），测试要测的机制一个字不改。
            return {'candidates': [{'brand': '乙厂', 'model': 'LM-100',
                                    'url': DISCOVERY_PAGE['url'],
                                    'configuration': DISCOVERY_PAGE['body_text'],
                                    'excerpt': DISCOVERY_PAGE['body_text']}]}
        return {'parameters': []}

    monkeypatch.setattr(service, 'model', fake_model)
    monkeypatch.setattr(service, 'query', lambda *args, **kwargs: None)
    monkeypatch.setattr(service, 'verify_publishers', lambda *args, **kwargs: None)
    monkeypatch.setattr(service, 'alternative_prices', lambda *args, **kwargs: {})
    monkeypatch.setattr(service, 'persist', lambda *args, **kwargs: None)
    monkeypatch.setattr(service, 'fetch_candidate_detail_pages', lambda *args, **kwargs: 0)
    monkeypatch.setattr(service, 'search_candidate_product_pages', lambda *args, **kwargs: 0)
    service.alternatives(run)

    models = [c['model'] for c in run['discovered_candidates']]
    assert models == ['LM-100'], models
    assert not any(c.get('proposed_by') == 'local_db' for c in run['discovered_candidates'])
    assert not any('本地成交明细库' in gap for gap in run['gaps']), run['gaps']
    # 撤掉之后名额不再被过不了闸门的线索占住：能核验的那条候选进得了核验。
    assert [c.get('model') for c in run['alternatives']] == ['LM-100'], run['alternatives']

    models = [c['model'] for c in run['discovered_candidates']]
    assert models == ['LM-100'], models

def test_a_missing_library_no_longer_shows_up_in_the_candidate_lane(research, monkeypatch,
                                                                    tmp_path):  # noqa: F811
    """库不在时，**候选车道**不再提它——那条接线已经撤了，库只服务价格车道。

    （价格车道那边"库不在"仍要如实记缺口，见 ``local_price_rows`` 自己的用例。）
    """
    from backend.services.procurement_research import ON_DEMAND_ENV

    monkeypatch.setenv(ON_DEMAND_ENV, '0')
    monkeypatch.setenv('LAB_XLYG_DB', str(tmp_path / 'nope.sqlite3'))
    service, run, _, _ = research
    run.update({'baseline': dict(STERILIZER_BASELINE), 'sources': [],
                'alternatives': [], 'discovered_candidates': [], 'reference_candidates': [],
                'prices': {}, 'gaps': [], 'failures': []})
    monkeypatch.setattr(service, 'model', lambda *args, **kwargs: {'candidates': []})
    monkeypatch.setattr(service, 'query', lambda *args, **kwargs: None)
    monkeypatch.setattr(service, 'verify_publishers', lambda *args, **kwargs: None)
    monkeypatch.setattr(service, 'alternative_prices', lambda *args, **kwargs: {})
    monkeypatch.setattr(service, 'persist', lambda *args, **kwargs: None)
    monkeypatch.setattr(service, 'fetch_candidate_detail_pages', lambda *args, **kwargs: 0)
    monkeypatch.setattr(service, 'search_candidate_product_pages', lambda *args, **kwargs: 0)
    service.alternatives(run)
    assert not any('本地成交明细库' in gap for gap in run['gaps']), run['gaps']


def test_alternatives_does_not_pad_the_candidate_list_when_the_library_has_no_match(
        research, monkeypatch, tmp_path):  # noqa: F811
    """库在、但品类对不上：一条都不补，也不许写"补了 0 条"这种话。"""
    from backend.services.procurement_research import ON_DEMAND_ENV

    monkeypatch.setenv(ON_DEMAND_ENV, '0')
    library = _build_db(tmp_path, [_row('SJ511C', '雅马拓Yamato')])
    monkeypatch.setenv('LAB_XLYG_DB', str(library))
    service, run, _, _ = research
    run.update({'baseline': {**STERILIZER_BASELINE, 'category': '超声波清洗机'},
                'sources': [], 'alternatives': [], 'discovered_candidates': [],
                'reference_candidates': [], 'prices': {}, 'gaps': [], 'failures': []})
    monkeypatch.setattr(service, 'model', lambda *args, **kwargs: {'candidates': []})
    monkeypatch.setattr(service, 'query', lambda *args, **kwargs: None)
    monkeypatch.setattr(service, 'verify_publishers', lambda *args, **kwargs: None)
    monkeypatch.setattr(service, 'alternative_prices', lambda *args, **kwargs: {})
    monkeypatch.setattr(service, 'persist', lambda *args, **kwargs: None)
    monkeypatch.setattr(service, 'fetch_candidate_detail_pages', lambda *args, **kwargs: 0)
    monkeypatch.setattr(service, 'search_candidate_product_pages', lambda *args, **kwargs: 0)
    service.alternatives(run)
    assert run['discovered_candidates'] == []
    assert not any('补了' in gap and '本地成交明细库' in gap for gap in run['gaps']), run['gaps']
