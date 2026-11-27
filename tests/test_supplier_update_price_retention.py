"""回填销售回复不许丢价、也不许丢原因。

``supplier_update()`` 走的是"用已存证据重算"：它逐条拿 ``price['url']`` 回
``run['sources']`` 里找来源页，找不到就整条丢掉、不留痕。广东教育平台离线成交明细库
（``data/xlyg_prices.sqlite3``）直查出来的价格本来就没有来源页，于是那一整条通道在第二版
报告里成建制消失，同一份报告前后两版自相矛盾（成交 5 条变 1 条、检索尝试 58 次变 0 次、
「未被采用的价格与原因」整段不见）。现象与定位见 ``设计规范``，原始证据见 ``设计规范``
§3.1、§3.2。

这一组用产品自己的入口（``service.supplier_update(...)``）复现那条路径，断言的是三件事：
没有来源页的价格按原判保留、确实被拒的价格留下原因、这一轮没有重新检索所以检索记录原样跟着走。
"""
from backend.services.procurement_rules import EXCERPT_NOT_IN_BODY, MODEL_MISMATCH
from backend.services.procurement_tables import price_table_rows
from backend.services.procurement_rules import validate_price
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture

WEB_URL = 'https://example.edu.cn/award'
OTHER_URL = 'https://example.edu.cn/other-award'
WEB_BODY = ('成交公告 2023-09-14 金额单位：元\n项目总额37799元\n'
            '|品牌|型号|数量|单价|\n|甲厂|Q10|1|2500|')
OTHER_BODY = '成交公告 2023-11-02 金额单位：元\n项目总额2999元\n|品牌|型号|数量|单价|\n|甲厂|Q10|1|2999|'


def _source(url, body):
    return {'url': url, 'body_text': body, 'body_status': 'fetched', 'source_type': 'tender_document'}


def _web_price():
    """上一轮真的采信过的一条网页价（引文逐字在正文里，重算后仍应通过）。"""
    row = price_table_rows(WEB_BODY, 'Q10')[0]
    checked = validate_price({**row, 'kind': 'award', 'date': '2023-09-14'},
                             _source(WEB_URL, WEB_BODY), {'brand': '甲厂', 'model': 'Q10'})
    assert checked, '这个用例的前提是这条网页价上一轮通过了核验'
    return checked


def _refused_web_price():
    """有来源页、但引文与那一页正文对不上的一条：重算时必须被拒且留下原因。"""
    refused = dict(_web_price())
    refused.update({'url': OTHER_URL, 'amount': 2999, 'amount_unit': '元', 'amount_scope': 'unit',
                    'unit_price': 2999.0, 'excerpt': '这一页正文里根本没有的引文：成交单价 2999 元'})
    return refused


def _local_db_price():
    """广东教育平台离线成交明细库直查的一条：形状照 ``xlyg_local_db.local_price_rows``。"""
    return {'brand': '甲厂', 'model': 'Q10', 'procurement_unit': '某高校', 'configuration': 'unknown',
            'kind': 'award', 'amount': 2900, 'amount_unit': '元', 'amount_scope': 'unit',
            'quantity': 1, 'currency': 'CNY', 'date': '2025-03-02', 'project_id': 'GD-2025-0302',
            'supplier': '丙公司', 'lot': '', 'item': 'Q10 教学用恒温槽',
            'tax': 'unknown', 'installation': 'unknown', 'warranty': 'unknown',
            'shipping': 'unknown', 'service_response': 'unknown', 'service_onsite': 'unknown',
            'delivery': 'unknown', 'excerpt': 'Q10 教学用恒温槽 2900.00 1 台',
            # 这一条没有抓取来源页：本地库里的成交明细按型号直查就有价。
            'url': '', 'channel': 'xlyg_local_db', 'price_kind': '',
            'local_db': 'data/xlyg_prices.sqlite3', 'scope_note': '仅该平台口径',
            'unit_price': 2900.0, 'calculation': '2900.0 × 1', 'level': 'historical_configuration',
            'comparison_level': 'historical_configuration', 'date_verified': True,
            'gaps': ['configuration', 'tax', 'installation', 'warranty', 'delivery']}


def _memory_price():
    """价格记忆取回的一条：``price_memory.py:191`` 给记忆行打的 ``from_memory``。

    它记的是**以前轮次**抓到的成交，来源页自然不在本轮 ``run['sources']`` 里。
    ``设计规范`` §3.1：HVE-50 那轮 6 条记忆价（48390×2 / 63000×2 / 99840.5 / 52000）
    就是这样被降级成"未取得正文"的，采信价从 10 条掉到 4 条。
    """
    price = _web_price()
    price.update({'url': 'https://example.edu.cn/recalled', 'amount': 63000, 'amount_unit': '元',
                  'amount_scope': 'unit', 'unit_price': 63000.0, 'date': '2024-05-11',
                  'excerpt': '|甲厂|Q10|2|63000|',
                  'from_memory': True, 'memory_first_seen_at': '2026-10-04T21:27:09+00:00',
                  'memory_times_seen': 2})
    return price


def _price_whose_page_is_gone():
    """有地址、但本轮 ``sources`` 里已经没有这一页：重算不了，但不是被拒。"""
    price = _web_price()
    price.update({'url': 'https://example.edu.cn/dropped', 'amount': 48390, 'amount_unit': '元',
                  'amount_scope': 'unit', 'unit_price': 48390.0, 'date': '2025-09-15',
                  'excerpt': '|甲厂|Q10|1|48390|'})
    return price


def _run_with_two_unreachable_prices(service, run):
    """两类"证据不依赖本轮来源页"的价格，外加一条正常的网页价作对照。"""
    run['prices'] = [_web_price(), _memory_price(), _price_whose_page_is_gone()]
    run['sources'] = [_source(WEB_URL, WEB_BODY)]
    run['search_attempts'] = [{'query': 'Q10 成交公告', 'provider': 'offline', 'status': 'ok'}]
    service.save(run)
    return run


def _run_with_prices(service, run):
    """把三种价格记录与检索记录放进这一轮，模拟"已经出过一版报告"的 run。"""
    run['prices'] = [_web_price(), _refused_web_price(), _local_db_price()]
    run['sources'] = [_source(WEB_URL, WEB_BODY), _source(OTHER_URL, OTHER_BODY)]
    run['search_attempts'] = [{'query': f'Q10 成交公告 {index}', 'provider': 'offline',
                               'status': 'ok'} for index in range(3)]
    # 上一轮已经写下的拒收原因：回填没有重新推导来源级原因，它必须原样留着。
    run['price_rejections'] = [{'url': 'https://example.edu.cn/legacy', 'model': 'H1850R',
                                'excerpt': 'H1850R 成交单价 36000 元',
                                'reason_code': MODEL_MISMATCH, 'detail': '引文型号：H1850R。'}]
    service.save(run)
    return run


def test_a_price_without_a_source_page_is_kept_verbatim(research):  # noqa: F811
    service, run, _, _ = research
    previous = _run_with_prices(service, run)

    updated = service.supplier_update(run['task_id'], run['id'], {'supplier_price': 7500})

    assert updated['id'] != previous['id'], '比较基准变了就该另出一版，否则这个测试没走到重算路径'
    assert updated['evidence_reused'] is True
    amounts = sorted(float(price['unit_price']) for price in updated['prices'])
    # 网页价（2500）与本地库直查价（2900）都还在；被拒的 2999 不在。
    assert amounts == [2500.0, 2900.0]
    local = next(price for price in updated['prices'] if price.get('channel') == 'xlyg_local_db')
    assert local['url'] == '' and local['local_db'] == 'data/xlyg_prices.sqlite3'


def test_a_refused_price_is_recorded_instead_of_disappearing(research):  # noqa: F811
    service, run, _, _ = research
    _run_with_prices(service, run)

    updated = service.supplier_update(run['task_id'], run['id'], {'supplier_price': 7500})

    rejections = updated['price_rejections']
    refused = next(item for item in rejections if item.get('url') == OTHER_URL)
    assert refused['reason_code'] == EXCERPT_NOT_IN_BODY
    assert refused.get('detail'), '拒收记录必须带一句给人看的原因'
    assert not any(price.get('url') == OTHER_URL for price in updated['prices'])
    # 上一轮的原因不被这一轮覆盖（回填没有重新推导来源级拒收）。
    assert any(item.get('url') == 'https://example.edu.cn/legacy' for item in rejections)


def test_reused_evidence_keeps_the_search_attempts_of_the_first_version(research):  # noqa: F811
    service, run, _, _ = research
    previous = _run_with_prices(service, run)

    updated = service.supplier_update(run['task_id'], run['id'], {'supplier_price': 7500})

    assert updated['search_attempts'] == previous['search_attempts']
    assert len(updated['search_attempts']) == 3, '重算没有重新检索，第二版的检索次数不该归零'
    report = service.report(run['task_id'], updated['id'])
    gaps = next(section for section in report['sections'] if section['key'] == 'gaps')
    assert any('3 次检索尝试' in str(item.get('text') or '') for item in gaps['items']), \
        '第二版报告里"本轮做过什么"的账要和第一版一致'
    rejection_section = next(section for section in report['sections'] if section['key'] == 'price_rejections')
    assert rejection_section['items'], '被拒的价格要在报告里列出来，不能整段消失'


def test_a_price_recalled_from_memory_is_kept(research):  # noqa: F811
    """价格记忆取回的成交价，来源页不在本轮 sources 里 —— 不许因此在回填时降级。"""
    service, run, _, _ = research
    previous = _run_with_two_unreachable_prices(service, run)

    updated = service.supplier_update(run['task_id'], run['id'], {'supplier_price': 7500})

    assert updated['id'] != previous['id'], '比较基准变了就该另出一版，否则这个测试没走到重算路径'
    amounts = sorted(float(price['unit_price']) for price in updated['prices'])
    assert amounts == [2500.0, 48390.0, 63000.0], \
        '记忆取回的 63000 与回不了原文的 48390 都该按原判保留，一条都不能掉'
    recalled = next(price for price in updated['prices'] if price.get('from_memory'))
    assert recalled['memory_times_seen'] == 2 and recalled['unit_price'] == 63000.0
    rejected_urls = {item.get('url') for item in (updated.get('price_rejections') or [])}
    assert 'https://example.edu.cn/recalled' not in rejected_urls, '保留，不是拒收'
    assert 'https://example.edu.cn/dropped' not in rejected_urls, '回不了原文也不是拒收'


def test_missing_source_pages_are_disclosed_as_unverified(research):  # noqa: F811
    """保留不等于不吭声：第二版报告要说清这几条没重新核对过。"""
    service, run, _, _ = research
    _run_with_two_unreachable_prices(service, run)

    updated = service.supplier_update(run['task_id'], run['id'], {'supplier_price': 7500})

    assert any('沿用上一轮判定' in str(gap) for gap in updated['gaps']), \
        '来源页不在本轮结果里这件事要写给人看，不能静默地少几条'
    report = service.report(run['task_id'], updated['id'])
    gaps = next(section for section in report['sections'] if section['key'] == 'gaps')
    assert any('沿用上一轮判定' in str(item.get('text') or '') for item in gaps['items'])
