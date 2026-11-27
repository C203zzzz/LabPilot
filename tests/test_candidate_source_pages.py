# -*- coding: utf-8 -*-
"""候选的来源网页（``candidate['source_pages']``）。

设计规范（原话）：「需要提供来源网页 就跟查询成交价一样 且前端页面也要做成相似的效果」。
这条链的三段各自钉在这里：

1. **形状**：``source_pages`` 是**列表**（一条候选可能对应多个来源页），每项
   ``{'url', 'origin', 'primary'}``；``origin`` 说明这条链接是**哪一步**拿到的，
   ``primary`` 全表最多一条、且只能是"发现这条候选的那一页"；
2. **生成端**：``alternatives()`` 跑完，三份名单（``discovered_candidates`` /
   ``alternatives`` / ``reference_candidates``）里的候选都带这个字段；
3. **接口**：读接口（``service.display``，也就是 ``GET /research-runs/{run_id}``
   返回的那一份）原样把它交给前端——候选对象整份返回，没有白名单裁剪。

还有两条反向断言，防的是"为了好看而多加东西"：价格来源**不**进这个列表（它在候选
卡片的价格区已经有自己那一块），以及"只增不减"（重跑一轮只拿到一条来源时，上一轮
那几条一个字都不许丢——``设计规范`` §5 的"内部事实不许被抹掉"）。

模型与检索都是离线替身，一条网络请求、一次真模型调用都不发。
"""
import pytest

from backend.services.procurement_research import (
    CANDIDATE_SOURCE_ORIGINS, candidate_source_pages, record_candidate_source_pages)
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture

BASELINE = {
    'brand': '甲厂', 'model': 'Q10', 'category': '生物安全柜', 'scenario': '微生物检测',
    'parameters': [{'name': '容量', 'value': 100, 'unit': 'ml', 'direction': 'min'}],
}
CATALOG = 'https://catalog.example.com/list'
SHOP = 'https://shop.example.com/product/Q20'
MANUAL = 'https://manual.example.com/Q20.pdf'
PARAM_PAGE = 'https://other.example.com/param'
AWARD = 'https://hospital.example.com/notice/2026-09-11'


def notice(url, body):
    return {'url': url, 'purpose': 'discovery', 'body_status': 'fetched',
            'source_type': 'tender_document', 'title': '实验室设备采购项目中标（成交）结果公告',
            'body_text': body}


def prepare(run, sources):
    """``alternatives()`` 只读它需要的那几个字段；``persist()`` 会读基准版本号，
    所以基准是合并不是替换（与 ``test_alternative_price_lookup._prepare`` 同一条）。"""
    run.update({'baseline': {**(run.get('baseline') or {}), **BASELINE},
                'sources': list(sources), 'alternatives': [],
                'discovered_candidates': [], 'reference_candidates': [],
                'prices': [], 'price_rejections': [], 'price_source_skips': [], 'gaps': []})


def stub(service, monkeypatch, candidates):
    """模型与检索全换替身；发现阶段返回给的那批候选原样交给 ``alternatives()``。"""
    def fake_model(run_, prompt, data):
        if prompt.startswith('从给定正文发现'):
            return {'candidates': [dict(c) for c in candidates]}
        return {'parameters': []}

    monkeypatch.setattr(service, 'model', fake_model)
    monkeypatch.setattr(service, 'query', lambda *args, **kwargs: None)
    monkeypatch.setattr(service, 'verify_publishers', lambda *args, **kwargs: None)
    monkeypatch.setenv('ALTERNATIVE_PRICE_ON_DEMAND', '1')


# ---------------------------------------------------------------------------
# 一、形状：多来源、单主来源、按"哪一步拿到"标注
# ---------------------------------------------------------------------------

def test_the_list_collects_every_step_and_keeps_one_primary():
    """发现页 / 详情页 / 配置证据 / 参数证据各有链接时，列表里一条不少。"""
    candidate = {
        'model': 'Q20', 'url': CATALOG,
        'detail_page': {'url': SHOP, 'status': 'fetched'},
        'brand_evidence': {'url': SHOP, 'excerpt': '品牌 乙厂'},
        'config_match': {'items': [{'observed': {'evidence': {'url': SHOP, 'excerpt': '容量 100 ml'}}}]},
        'matrix': [{'observed': {'evidence': {'url': MANUAL, 'excerpt': '容量 100 ml'}},
                    'raw_evidence': [{'url': PARAM_PAGE, 'excerpt': '容量 100 ml'}]}],
    }
    rows = candidate_source_pages(candidate)

    assert [row['url'] for row in rows] == [CATALOG, SHOP, MANUAL, PARAM_PAGE]
    assert [row['origin'] for row in rows] == ['discovered', 'detail_page', 'matrix', 'matrix']
    # 主来源只有一条，且就是发现这条候选的那一页。
    assert [row['primary'] for row in rows] == [True, False, False, False]
    # 同一个网址既是品牌出处又是配置证据时只留一条（先出现的那步算它的来路）。
    assert sum(1 for row in rows if row['url'] == SHOP) == 1
    assert set(row['origin'] for row in rows) <= set(CANDIDATE_SOURCE_ORIGINS)


def test_supplier_evidence_is_kept_as_its_own_source_origin():
    candidate = {
        'model': 'SYG-1210', 'url': CATALOG,
        'supplier': '苏州捷美电子有限公司',
        'supplier_evidence': {'url': SHOP, 'excerpt': '苏州捷美电子有限公司 地址:'},
    }
    rows = candidate_source_pages(candidate)
    assert [(row['url'], row['origin']) for row in rows] == [
        (CATALOG, 'discovered'), (SHOP, 'supplier_evidence')]


def test_old_redacted_supplier_evidence_recovers_original_url():
    rows = candidate_source_pages({
        'model': 'SYG-1210', 'url': '#',
        'original_url': CATALOG,
        'supplier_evidence': {'url': '#', 'original_url': SHOP},
    })
    assert [(row['url'], row['origin']) for row in rows] == [
        (CATALOG, 'discovered'), (SHOP, 'supplier_evidence')]


def test_a_candidate_without_a_discovery_page_marks_no_primary():
    """没有"发现页"的候选一条主来源都不标——宁可不标，也不拿别的页冒充。"""
    rows = candidate_source_pages({'model': 'Q20', 'url': '',
                                   'detail_page': {'url': SHOP}})
    assert [row['url'] for row in rows] == [SHOP]
    assert not any(row['primary'] for row in rows)


def test_empty_urls_never_become_rows():
    """"没有地址"与"有地址但不可点"是两件事：空地址不产出行。"""
    rows = candidate_source_pages({'model': 'Q20', 'url': '   ',
                                   'detail_page': {'url': None},
                                   'brand_evidence': {},
                                   'config_match': {'items': [{'observed': None}, 'x']},
                                   'matrix': [None, {'observed': {}}, {'raw_evidence': [{}]}]})
    assert rows == []


def test_prices_are_not_part_of_the_candidate_source_list():
    """价格来源不进这个列表：它在候选卡片的价格区已经有自己那一块（同一件事一个出口）。"""
    rows = candidate_source_pages({'model': 'Q20', 'url': CATALOG,
                                   'prices': [{'url': AWARD, 'unit_price': 56000}]})
    assert [row['url'] for row in rows] == [CATALOG]


def test_recording_only_adds_and_never_drops_an_earlier_source():
    """重跑一轮只拿到一条来源时，上一轮那几条一个字都不许丢（取信息最全的那次）。"""
    candidate = {'model': 'Q20', 'url': CATALOG,
                 'detail_page': {'url': SHOP},
                 'matrix': [{'raw_evidence': [{'url': MANUAL}]}]}
    assert [row['url'] for row in record_candidate_source_pages(candidate)] == [CATALOG, SHOP, MANUAL]

    # 同一个候选对象在后续阶段只剩发现页：既有的三条必须原样保留、顺序也不变。
    candidate.pop('detail_page')
    candidate.pop('matrix')
    again = record_candidate_source_pages(candidate)
    assert [row['url'] for row in again] == [CATALOG, SHOP, MANUAL]
    assert [row['origin'] for row in again] == ['discovered', 'detail_page', 'matrix']

    # 主来源的标记由"发现页"唯一决定，后一次调用不会把它挪到别人头上。
    candidate['url'] = PARAM_PAGE
    moved = record_candidate_source_pages(candidate)
    assert [row['primary'] for row in moved] == [False, False, False, True]
    assert [row['url'] for row in moved] == [CATALOG, SHOP, MANUAL, PARAM_PAGE]


def test_candidates_that_are_not_cards_are_left_untouched():
    """非字典、或没有型号的条目不是卡片：不写字段、不抛异常。"""
    for value in (None, 'Q20', [], {'url': CATALOG}, {'model': ''}):
        before = value.copy() if isinstance(value, dict) else value
        assert record_candidate_source_pages(value) == []
        assert value == before, f'不是卡片的条目被改了：{value!r}'


# ---------------------------------------------------------------------------
# 二、生成端：三份名单都要有
# ---------------------------------------------------------------------------

def _four_brand_run(service, run, monkeypatch):
    """四个不同品牌的候选：三个进核验名额（``alternatives``）、第四个落参考候选。

    ``diverse_candidates`` 一个品牌只给一个名额、上限 3（既有判据，本单没动它），
    所以"参考候选也有这个字段"必须造第四条。
    """
    brands = [('乙厂', 'Q20'), ('丁厂', 'Q30'), ('戊厂', 'Q40'), ('己厂', 'Q50')]
    candidates, sources = [], []
    for index, (brand, model) in enumerate(brands):
        url = f'https://hospital-{index}.example.com/notice/2026-09-11'
        line = f'生物安全柜 {brand} {model} 数量1台 单价：5{index}0,000.00 元'
        # 设计规范「找不到配置就不展示给用户」在
        # ``alternatives()`` 收尾时撤掉配置为空的候选（见
        # ``tests/test_m4_pipeline.py``）。这里测的是来源清单随候选落三份名单，
        # 所以给每条候选一段**正文里逐字存在**的配置，否则它们会被撤下候选卡片，
        # 断言测的就不是来源清单了。
        spec_line = f'主舱尺寸 1000×600×{index}00mm'
        body = ('某某医院实验室设备采购项目中标（成交）结果公告\n采购人：某某医院\n'
                + line + '\n' + spec_line + '\n')
        sources.append(notice(url, body))
        candidates.append({'brand': brand, 'model': model, 'url': url, 'excerpt': line,
                           'configuration': spec_line})
    prepare(run, sources)
    stub(service, monkeypatch, candidates)
    service.alternatives(run)
    return {model: url for (_, model), url in zip(brands, [c['url'] for c in candidates])}


def test_alternatives_writes_the_source_list_on_all_three_rosters(research, monkeypatch):  # noqa: F811
    """生成端跑完：发现名单、正式候选、参考候选里的候选都带 ``source_pages``。"""
    service, run, _, _ = research
    urls = _four_brand_run(service, run, monkeypatch)

    assert len(run['alternatives']) == 3, run['alternatives']
    assert len(run['reference_candidates']) == 1, run['reference_candidates']
    assert len(run['discovered_candidates']) == 4

    for roster in ('discovered_candidates', 'alternatives', 'reference_candidates'):
        for candidate in run[roster]:
            rows = candidate.get('source_pages') or []
            assert rows, f'{roster} 里的候选没有来源清单：{candidate.get("model")}'
            assert [row['primary'] for row in rows] == [True] + [False] * (len(rows) - 1), rows
            assert rows[0]['url'] == urls[candidate['model']], rows
            assert rows[0]['origin'] == 'discovered'


def test_brandless_candidate_with_page_supplier_is_selected_and_keeps_brand_empty(research, monkeypatch):  # noqa: F811
    """有页面关联企业的候选可以展示和占名额，但供应商不能冒充品牌。"""
    service, run, _, _ = research
    url = 'https://supplier.example.com/syg-1210'
    line = '恒温水浴锅 SYG-1210 控温范围 室温+5~99℃'
    source = notice(url, '恒温水浴锅产品页\n' + line + '\n苏州捷美电子有限公司 地址: 苏州')
    prepare(run, [source])
    # 配置同上一条：没有配置的候选会在 ``alternatives()`` 收尾时被撤下（2026-10-06 口径）。
    stub(service, monkeypatch, [{'brand': '', 'model': 'SYG-1210', 'url': url, 'excerpt': line,
                                 'configuration': line}])

    service.alternatives(run)

    assert len(run['alternatives']) == 1, run['alternatives']
    candidate = run['alternatives'][0]
    assert candidate['brand'] == ''
    assert candidate['supplier'] == '苏州捷美电子有限公司'
    assert candidate['supplier_evidence']['excerpt'].startswith('苏州捷美电子有限公司')


def test_the_run_read_api_returns_the_source_list(research, monkeypatch):  # noqa: F811
    """读接口那一份（``service.display``）原样带出字段——接口没有白名单裁剪。"""
    service, run, _, client = research
    urls = _four_brand_run(service, run, monkeypatch)

    payload = client.get(f"/api/v1/tasks/{run['task_id']}/research-runs/{run['id']}").json()
    cards = payload['alternatives'] + payload['reference_candidates']
    assert len(cards) == 4, cards
    for candidate in cards:
        rows = candidate.get('source_pages') or []
        assert rows, candidate.get('model')
        assert rows[0] == {'url': urls[candidate['model']], 'origin': 'discovered', 'primary': True}


def test_the_required_field_shape_does_not_break_existing_candidate_fields(research, monkeypatch):  # noqa: F811
    """只加字段：既有字段（引文、型号、id、价格留痕）一个都不因这一步被改写。"""
    service, run, _, _ = research
    _four_brand_run(service, run, monkeypatch)

    for candidate in run['alternatives'] + run['reference_candidates']:
        assert candidate['model'] and candidate['excerpt']
        assert candidate['candidate_id']
        assert 'price_lookup' in candidate
