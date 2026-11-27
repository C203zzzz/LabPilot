"""厂家产品参数**真的到了页面上**——这次要纠正的失败模式就是这个。

``tests/test_product_configuration.py`` 把判据本身钉得很死，但它钉的是
``prices()`` 的**返回值**。真实系统里那条返回值只是中间产物：页面拿到的是
``GET /{task_id}/research-runs/{run_id}`` 和 ``GET /{task_id}/baseline`` 直接读库
的结果，而库里的 run 可能是在这份推导之前跑出来的，产品参数页也可能是价格通道
结束**之后**才抓回来的（实测 ``task-013dcdcff7e6`` 的 66 个来源里，价格通道只抓了
2 个，后面两段又抓了 35 个）。于是 22 条单元测试全过、页面上却一个
``.price-product-config`` 都没有。

所以这里钉的不是判据，是**接口**：读接口返回的价格记录必须已经带着厂家参数和它的
出处；跑完一轮的研究必须把这一步落进库；报告正文里那一行必须是带出处的截断文本，
包括**已经存下来的旧报告**。
"""
import pytest
from backend.services.product_configuration import (
    MANUFACTURER_SOURCE, REPORT_CHARACTER_LIMIT, hydrate_run, report_configuration)
from backend.services.supplemental_research import report_id
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture
from tests.test_product_configuration import (  # noqa: F401 - 同一批真实夹具
    BRAND, MANUFACTURER_BODY, MANUFACTURER_SPEC, MANUFACTURER_URL, MODEL, NOTICE_URL,
    XKA_EXCERPT, notice, source)


def tender_source():
    return source(NOTICE_URL, notice('ccgp-split-field-notice.txt'), 'tender_document')


def manufacturer_source():
    return source(MANUFACTURER_URL, MANUFACTURER_BODY)


def price_record(configuration='unknown', **overrides):
    """一条已经存进库的价格记录（形状取自真实 run，字段一个不少）。"""
    record = {'brand': BRAND, 'model': MODEL, 'procurement_unit': '', 'configuration': configuration,
              'kind': 'award', 'amount': 17480, 'amount_unit': '元', 'amount_scope': 'unit',
              'quantity': 1, 'currency': 'CNY', 'date': '2026-03-25', 'project_id': '',
              'lot': '', 'item': '', 'tax': 'unknown', 'installation': 'unknown',
              'warranty': 'unknown', 'shipping': 'unknown', 'service_response': 'unknown',
              'service_onsite': 'unknown', 'delivery': 'unknown', 'excerpt': XKA_EXCERPT,
              'unit_price': 17480.0, 'url': NOTICE_URL, 'level': 'historical_configuration',
              'gaps': ['configuration'], 'calculation': '17480.0 元 × 1'}
    record.update(overrides)
    return record


def stored_run_without_configuration(service, run, sources, prices=None):
    """造出「库里那条记录」：价格已算出来、配置还是空的，而来源里已经有产品参数页。

    这正是产品参数页在价格通道结束之后才被抓回来的形状，也是库里所有旧 run 的形状。
    比较基准一并改成 XKA-2200／湘仪：推导读的就是 ``run['baseline']`` 上的型号与品牌。
    """
    run['baseline'] = {**run['baseline'], 'model': MODEL, 'brand': BRAND}
    run['prices'] = [price_record()] if prices is None else prices
    run['sources'] = list(sources)
    service.save(run)
    return run


# ---------------------------------------------------------------------------
# 这一单的断裂本身：读接口必须已经带上出处
# ---------------------------------------------------------------------------

def test_the_run_status_endpoint_returns_the_configuration_with_its_source(research):  # noqa: F811
    """断点就在这条线上：页面读的是这个接口，不是 ``prices()`` 的返回值。"""
    service, run, _, client = research
    stored_run_without_configuration(service, run, [tender_source(), manufacturer_source()])

    body = client.get(f"/api/v1/tasks/{run['task_id']}/research-runs/{run['id']}").json()

    price = body['prices'][0]
    assert price['configuration_source'] == MANUFACTURER_SOURCE
    assert price['configuration_url'] == MANUFACTURER_URL
    assert '最高转速 | 4500r/min' in price['configuration']
    assert '≥4500r/min' not in price['configuration']       # 采购要求仍然一个字都不进来


def test_the_baseline_endpoint_returns_the_configuration_with_its_source(research):  # noqa: F811
    """``ProcurementPanel`` 的 run 来自 ``/baseline``（前端第 74 行 ``setRun(data.run)``）。"""
    service, run, _, client = research
    stored_run_without_configuration(service, run, [tender_source(), manufacturer_source()])

    body = client.get(f"/api/v1/tasks/{run['task_id']}/baseline").json()

    assert body['run']['prices'][0]['configuration_source'] == MANUFACTURER_SOURCE
    assert body['run']['prices'][0]['configuration_url'] == MANUFACTURER_URL


def test_reading_does_not_write_the_derivation_back(research):  # noqa: F811
    """读接口只算不写：库里那条记录不被改动（写库只在研究线程那一侧，见下面的用例）。"""
    service, run, _, client = research
    stored_run_without_configuration(service, run, [tender_source(), manufacturer_source()])

    client.get(f"/api/v1/tasks/{run['task_id']}/research-runs/{run['id']}")
    client.get(f"/api/v1/tasks/{run['task_id']}/baseline")

    stored = service.get(run['task_id'], run['id'])['prices'][0]
    assert stored['configuration'] == 'unknown'
    assert 'configuration_source' not in stored


def test_the_finished_run_stores_the_configuration_so_the_database_agrees(research):  # noqa: F811
    """写库的时机在「来源复核与结果汇总」那一段：那时这一轮不会再抓新页面了。

    断点不是「没写库」，而是**写得太早**：``prices()`` 末尾那次跑在价格通道里，之后
    抓回来的产品参数页它看不见。所以这里让价格通道已经完成（模拟"算过了"），来源里
    再放上产品参数页，跑一遍 ``execute``，断言落进库的那条记录带出处。
    """
    service, run, _, _ = research
    stored_run_without_configuration(service, run, [tender_source(), manufacturer_source()],
                                     prices=[price_record()])
    run['lanes'] = {'price': 'completed', 'alternatives': 'completed'}
    run['status'] = 'partial'
    run['active_lane'] = 'all'
    service.save(run)

    service.execute(run['task_id'], run['id'])

    stored = service.get(run['task_id'], run['id'])
    assert stored['prices'][0]['configuration_source'] == MANUFACTURER_SOURCE
    assert stored['prices'][0]['configuration_url'] == MANUFACTURER_URL
    assert stored['prices'][0]['configuration'] == MANUFACTURER_SPEC


def test_a_configuration_written_by_the_award_notice_is_never_overwritten(research):  # noqa: F811
    """公告／合同附件里真写了的配置是这笔成交记录自己的事实，读的时候也不许顶掉。"""
    service, run, _, client = research
    stored_run_without_configuration(service, run, [manufacturer_source()],
                                     prices=[price_record(configuration='含6×50ml角转子')])

    body = client.get(f"/api/v1/tasks/{run['task_id']}/research-runs/{run['id']}").json()

    assert body['prices'][0]['configuration'] == '含6×50ml角转子'
    assert 'configuration_source' not in body['prices'][0]


def test_without_a_product_page_the_configuration_stays_empty_on_the_wire(research):  # noqa: F811
    """没有产品参数页时接口不许编：仍然是 ``unknown``，也不带任何出处字段。"""
    service, run, _, client = research
    stored_run_without_configuration(service, run, [tender_source()])

    body = client.get(f"/api/v1/tasks/{run['task_id']}/research-runs/{run['id']}").json()

    assert body['prices'][0]['configuration'] == 'unknown'
    assert 'configuration_source' not in body['prices'][0]
    assert 'configuration_url' not in body['prices'][0]


def test_a_run_that_is_still_being_written_is_read_without_a_scan(research, monkeypatch):  # noqa: F811
    """还在跑的时候读它也不炸：价格记录还没有，一次来源扫描都不做（前端每 2 秒读一次）。"""
    from backend.services import product_configuration as module
    scans = []
    monkeypatch.setattr(module, 'product_configuration', lambda *args, **kwargs: scans.append(args) or None)
    service, run, _, client = research
    stored_run_without_configuration(service, run, [manufacturer_source()], prices=[])
    run['status'] = 'running'
    service.save(run)

    body = client.get(f"/api/v1/tasks/{run['task_id']}/research-runs/{run['id']}").json()

    assert body['prices'] == []
    assert scans == []


# ---------------------------------------------------------------------------
# ``hydrate_run`` 本身：只填空、每个型号各取各的
# ---------------------------------------------------------------------------

def test_hydrate_run_fills_the_records_that_are_empty_and_skips_the_others():
    run = {'baseline': {'model': MODEL, 'brand': BRAND},
           'sources': [source(MANUFACTURER_URL, MANUFACTURER_BODY)],
           'prices': [price_record(), price_record(configuration='公告里写的配置'),
                      price_record(configuration=''), price_record(configuration=None)],
           'alternatives': []}

    assert hydrate_run(run) == 3

    assert run['prices'][0]['configuration_source'] == MANUFACTURER_SOURCE
    assert run['prices'][1]['configuration'] == '公告里写的配置'
    assert 'configuration_source' not in run['prices'][1]
    for record in (run['prices'][2], run['prices'][3]):
        assert record['configuration'] == MANUFACTURER_SPEC
    # 幂等：第二次没有可填的，一条也不写。
    assert hydrate_run(run) == 0


def test_hydrate_run_leaves_everything_alone_without_a_product_page():
    run = {'baseline': {'model': MODEL, 'brand': BRAND},
           'sources': [source(NOTICE_URL, notice('ccgp-split-field-notice.txt'), 'tender_document')],
           'prices': [price_record()], 'alternatives': []}

    assert hydrate_run(run) == 0
    assert run['prices'][0] == price_record()


def test_hydrate_run_gives_each_candidate_its_own_models_parameters():
    """候选用的是**它自己**的型号：把主型号的参数挂到候选上就是把两台机器搞混。"""
    other_body = MANUFACTURER_BODY.replace('XKA-2200', 'TDZ4K').replace('XKA2200', 'TDZ4K')
    run = {'baseline': {'model': MODEL, 'brand': BRAND},
           'sources': [source(MANUFACTURER_URL, MANUFACTURER_BODY),
                       source('https://www.xiangyilxj.com/product/info_itemid_1701.html', other_body)],
           'prices': [price_record()],
           'alternatives': [{'model': 'TDZ4K', 'brand': BRAND, 'prices': [price_record(model='TDZ4K')]}]}

    assert hydrate_run(run) == 2

    assert 'XKA-2200' in run['prices'][0]['configuration']
    assert 'TDZ4K' in run['alternatives'][0]['prices'][0]['configuration']
    assert 'XKA-2200' not in run['alternatives'][0]['prices'][0]['configuration']


def test_hydrate_run_skips_empty_price_lists_without_scanning(monkeypatch):
    """空的价格列表不触发扫描：候选大多没有价格，为此把 66 个来源扫一遍是白花钱。"""
    from backend.services import product_configuration as module
    calls = []
    monkeypatch.setattr(module, 'product_configuration',
                        lambda *args, **kwargs: calls.append(args) or None)
    run = {'baseline': {'model': MODEL, 'brand': BRAND},
           'sources': [source(MANUFACTURER_URL, MANUFACTURER_BODY)], 'prices': [],
           'alternatives': [{'model': 'TDZ4K', 'brand': BRAND, 'prices': []}]}

    assert hydrate_run(run) == 0
    assert calls == []


# ---------------------------------------------------------------------------
# 报告正文：出处要真的走到正文里，旧报告也要
# ---------------------------------------------------------------------------

def test_the_report_body_says_the_parameters_are_not_the_award_notices(research):  # noqa: F811
    service, run, _, _ = research
    stored_run_without_configuration(service, run, [tender_source(), manufacturer_source()])

    report = service.report(run['task_id'], run['id'])

    section = next(s for s in report['sections'] if s['key'] == 'prices')
    text = section['items'][0]['text']
    assert '厂家资料标称参数（非公告配置）：' in text
    assert '最高转速 | 4500r/min' in text
    assert len(text) < 600                                        # 200 字上限 + 前后缀
    assert {'url': MANUFACTURER_URL} in section['items'][0]['evidence']


def test_an_already_stored_report_gets_its_configuration_line_refreshed(research):  # noqa: F811
    """旧报告是**建它那一刻**算出来的：正文写着「未提供」，而价格记录现在有出处了。

    实测那份存下来的报告（``report-3a8c466dd61ba24e3b798184``）正文就是
    「配置：公告正文及已取得的附件未提供该项」。价格记录修好之后，报告必须跟着改口，
    否则同一件事在两个地方各说一套。
    """
    service, run, _, _ = research
    stored_run_without_configuration(service, run, [tender_source()])

    first = service.report(run['task_id'], run['id'])
    old_text = next(s for s in first['sections'] if s['key'] == 'prices')['items'][0]['text']
    assert '厂家资料标称参数' not in old_text
    # 这一份已经存下来了（报告有总述，所以下一次读走的是快速返回那一支）。
    stored = service.repo.get('reports', report_id(run))['report']
    assert next(s for s in stored['sections'] if s['key'] == 'prices')['items'][0]['text'] == old_text

    # 产品参数页是之后才抓回来的（真实时序就是这样），再读一次报告。
    run = service.get(run['task_id'], run['id'])
    run['sources'] = [tender_source(), manufacturer_source()]
    service.save(run)
    second = service.report(run['task_id'], run['id'])
    new_text = next(s for s in second['sections'] if s['key'] == 'prices')['items'][0]['text']
    assert '厂家资料标称参数（非公告配置）：' in new_text
    assert new_text != old_text
    # 刷新过的那一份被写回去了：库里那一节现在就是新文本，不是每次读都改一遍。
    assert next(s for s in service.repo.get('reports', report_id(run))['report']['sections']
                if s['key'] == 'prices')['items'][0]['text'] == new_text


def test_a_stored_report_without_a_prices_section_is_left_alone(research):  # noqa: F811
    """更早版式的报告没有这一节，不许给它凭空加一节。"""
    service, run, _, _ = research
    service.repo.put('reports', report_id(run),
                     {'report': {'summary': '旧版式的总述', 'sections': [{'key': 'candidates', 'items': []}]},
                      'research_run_id': run['id']}, task_id=run['task_id'])

    report = service.report(run['task_id'], run['id'])

    assert [s['key'] for s in report['sections']] == ['candidates']
    assert report['summary'] == '旧版式的总述'


@pytest.mark.parametrize('source_code', [MANUFACTURER_SOURCE, 'product_page_spec'])
def test_the_report_line_is_truncated_and_labelled_for_both_sources(source_code):
    line = report_configuration({'configuration': '指标 | ' * 400, 'configuration_source': source_code})
    assert line.startswith('产品资料' if source_code == 'product_page_spec' else '厂家资料')
    assert line.endswith('…') and len(line) <= REPORT_CHARACTER_LIMIT + 40
