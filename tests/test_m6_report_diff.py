"""M6报告版本对比：回填销售回复另出一版后，两版差在哪要看得见。

这一组测试要证明的不是"新加了一个接口"，而是：

* 走产品自己的路（``supplier_update`` → 新版报告）能真的产出第二版，且新版报告
  说得清它从哪一版改出来（``parent_report_id``）；
* 两版的差异**分三类**摆出来：价格、配置、缺口，各自看得见；同一处变化只算一类；
* 没变的条目如实报"逐字相同"的条数——读者要能确认"其余零变化"，而不是只能相信那几条；
* 没有上一版、上一版被删这类情况，如实说原因，不编造差异。
"""
import pytest

from backend.services.procurement_research import missing_information_state
from backend.services.report_diff import GAP_STATE_PREFIXES, compare_reports
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture


def _report(sections, summary=''):
    return {'report_id': 'report-x', 'summary': summary, 'sections': sections}


def _category(result, key):
    return next(category for category in result['categories'] if category['key'] == key)


def _labels(changes):
    return [change['label'] for change in changes]


def test_gap_state_prefixes_are_still_what_the_product_writes():
    """分类靠的是这几句"这一项还是空的"文案；产品改了措辞，这里必须先红。"""
    revised = {'supplier_revision': {'not_provided': ['tax'], 'answers': [
        {'question': '交付时间（字段 delivery）', 'answer': '资料里没有提供。'}]}}
    assert missing_information_state(revised, 'tax').startswith(GAP_STATE_PREFIXES[0])
    assert missing_information_state({'supplier_revision': {'changes': {'tax': '含税'}}}, 'delivery').startswith(GAP_STATE_PREFIXES[1])
    assert missing_information_state({}, 'delivery').startswith(GAP_STATE_PREFIXES[2])


def test_entry_alignment_ignores_inserted_sections_and_counts_the_rest():
    """节是插进去的（新版报告会多一整节），按 label 对齐才不会把没变的报成"变了"。"""
    before = _report([{'key': 'baseline', 'title': '比较基准', 'summary': '容量检测', 'items': [
        {'label': '数量', 'text': '1'},
        {'label': '税费', 'text': '本轮没有向销售询问这一项'}]}])
    after = _report([
        {'key': 'decision', 'title': '本次采购判断', 'items': [{'label': '下一步', 'text': '请补齐报价'}]},
        {'key': 'baseline', 'title': '比较基准', 'summary': '容量检测', 'items': [
            {'label': '数量', 'text': '1'},
            {'label': '税费', 'text': '销售已回复：资料中未提供'}]}])

    result = compare_reports(before, after)

    assert [category['key'] for category in result['categories']] == ['price', 'configuration', 'gaps']
    assert _labels(_category(result, 'price')['items']) == ['下一步']
    assert _category(result, 'configuration')['items'] == []
    # 两侧都还在说"这一项没有"：变的是缺口状态，不是配置。
    assert _labels(_category(result, 'gaps')['items']) == ['税费']
    # 「数量」与节摘要逐字未变，如实报出来。
    assert result['unchanged'] == {'items': 2}


def test_a_removed_entry_is_reported_as_removed_not_dropped():
    before = _report([{'key': 'gaps', 'title': '证据缺口', 'items': [
        {'label': '缺口一', 'text': '官方来源未取得'}, {'label': '缺口二', 'text': '正文没读到'}]}])
    after = _report([{'key': 'gaps', 'title': '证据缺口', 'items': [
        {'label': '缺口一', 'text': '官方来源未取得'}]}])

    result = compare_reports(before, after)

    assert [(change['label'], change['change']) for change in _category(result, 'gaps')['items']] == [('缺口二', 'removed')]
    assert result['unchanged'] == {'items': 1}
    assert result['changed_total'] == 1


def _two_versions(service, run):
    """走产品自己的路出两版：先有一版报告，再回填销售回复另出一版。"""
    first = service.report(run['task_id'], run['id'])
    updated = service.supplier_update(run['task_id'], run['id'],
                                     {'supplier_price': 76000, 'warranty': '2 年', 'not_provided': ['tax']})
    second = service.report(run['task_id'], updated['id'])
    return first, updated, second


def test_supplier_reply_produces_a_second_version_linked_to_the_first(research):  # noqa: F811
    service, run, _, client = research

    first, updated, second = _two_versions(service, run)

    assert second['report_id'] != first['report_id'], '回填销售回复必须另出一版，否则"和上一版差在哪"无从谈起'
    assert second.get('parent_report_id') == first['report_id']
    items = {item['id']: item for item in client.get('/api/v1/reports').json()['items']}
    assert items[second['report_id']]['parent_report_id'] == first['report_id']
    assert items[first['report_id']]['parent_report_id'] is None


def test_the_three_kinds_of_change_are_each_visible(research):  # noqa: F811
    service, run, _, client = research
    first, updated, second = _two_versions(service, run)

    payload = client.get(f"/api/v1/reports/{second['report_id']}/diff").json()

    assert payload['available'] is True
    assert payload['previous_report_id'] == first['report_id']

    price = _category(payload, 'price')['items']
    configuration = _category(payload, 'configuration')['items']
    gaps = _category(payload, 'gaps')['items']
    assert price and configuration and gaps, '三类都要看得见（就算某一类恰好没变，也要给出这一类）'

    # 价格：销售单价从"未提供"变成这一版的报价。
    unit_price = next(change for change in price if change['label'] == '销售单价')
    assert unit_price['before'] == '未提供' and unit_price['after'].startswith('76000.0')

    # 配置：保修期限从没说明变成这一版补上的值，并标出它是本轮回复补的。
    warranty = next(change for change in configuration if change['label'] == '保修期限')
    assert '（本轮销售回复补充）' in warranty['after'] and '2 年' in warranty['after']
    # 还缺着的项不算"配置补了什么"：它们进缺口类，不在这里重复一遍。
    assert not [change for change in configuration
                if any((change['before'] or '').startswith(prefix) for prefix in GAP_STATE_PREFIXES)
                and any((change['after'] or '').startswith(prefix) for prefix in GAP_STATE_PREFIXES)]

    # 缺口：头一条就是"还缺哪几项"，并点名这一版补上了什么。
    missing = gaps[0]
    assert missing['label'] == '仍缺的条件'
    assert missing['before'].startswith('上一版仍缺') and missing['after'].startswith('本版仍缺')
    assert '这一版补上了：' in missing['after']
    assert '保修期限' in missing['after'] and '销售单价' in missing['after']

    # 没变的那一大片如实报数，读者才能确认"其余零变化"。
    assert payload['unchanged']['items'] > 0
    assert payload['changed_total'] == len(price) + len(configuration) + len(gaps) + len(payload['other'])


def test_the_supplier_revision_section_is_listed_under_other_not_dropped(research):  # noqa: F811
    """新版报告会多出「销售回复更新」节（这次回复补了什么的原始记账）。
    它归不进三类，但必须列出来——静默丢掉，读者就不知道报告里为什么多了一节。"""
    service, run, _, client = research
    _, _, second = _two_versions(service, run)

    payload = client.get(f"/api/v1/reports/{second['report_id']}/diff").json()

    assert any(change['section'] == 'supplier_revision' for change in payload['other'])


def test_a_first_version_says_so_instead_of_inventing_a_diff(research):  # noqa: F811
    service, run, _, client = research
    first = service.report(run['task_id'], run['id'])

    payload = client.get(f"/api/v1/reports/{first['report_id']}/diff").json()

    assert payload['available'] is False and payload['reason'] == 'first_version'
    assert payload['message']


def test_a_deleted_previous_version_is_reported_as_such(research):  # noqa: F811
    service, run, _, client = research
    first, _, second = _two_versions(service, run)
    assert client.post('/api/v1/reports/delete', json={'report_ids': [first['report_id']]}).status_code == 200

    payload = client.get(f"/api/v1/reports/{second['report_id']}/diff").json()

    assert payload['available'] is False and payload['reason'] == 'previous_missing'
    assert payload['previous_report_id'] == first['report_id']


def test_an_unknown_report_is_not_a_500(research):  # noqa: F811
    _, _, _, client = research

    response = client.get('/api/v1/reports/report-does-not-exist/diff')

    assert response.status_code == 200
    assert response.json()['reason'] == 'report_not_found'


def test_comparing_a_report_with_itself_reports_no_change(research):  # noqa: F811
    """同一份报告比它自己：三类都空、全部条目逐字相同——"零变化"必须是一种能表达的结论。"""
    service, run, _, client = research
    first, _, second = _two_versions(service, run)
    record = service.repo.get('reports', second['report_id'])

    from backend.services.procurement_research import sanitize_report
    report = sanitize_report(record['report'], record.get('snapshot') or {})
    result = compare_reports(report, report)

    assert [category['items'] for category in result['categories']] == [[], [], []]
    assert result['other'] == [] and result['notes'] == [] and result['changed_total'] == 0
    assert result['unchanged']['items'] > 0
    assert first['report_id'] != second['report_id']
