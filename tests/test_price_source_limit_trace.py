"""上限（``PRICE_SOURCE_LIMIT``）挡掉的公告：必须留痕，而且要有一次被读的机会。

实测（``设计规范-改后效果评测.md`` §5.2）：``task-a283e5adf675`` 的
``t20260423_26446217.htm`` 正文写着 ``规格型号：DMi1``、``单价(元)：41800``，
前置判据全过，却因为四条来源的额度用完而 **从未被模型读过**；留痕只落在
``price_source_skips`` 里，而那个字段在展示路径上没有任何读者。更糟的是
``_rescan_late_price_sources`` 把「有 skip 记录」当成「已经考虑过」，于是被额度挡掉的
来源连补扫那一轮也被排除——同一条记录既没被读、又被记为处理过。

本文件钉住三件事：

* 额度挡掉的来源在超预算时写进 trace，并**在 run 的 gaps 里说出来**（用户可见）；
* 补扫轮不被该记录排除，而且**额度挡掉的来源排在补扫队列最前**，否则第二次机会
  仍然可能轮不到它；
* 排序改动只按「正文自己声明了目标型号」这一条证据走，不许把同档来源重排。
"""
import json

from backend.services.procurement_research import (
    LATE_PRICE_SOURCE_LIMIT, PRICE_SOURCE_LIMIT, _declares_target_model, _price_source_rank,
)
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture

MODEL = 'XKA-2200'
NOTICE = ('中标（成交）结果公告\n项目编号：P-2026-0001\n采购人：某县人民医院\n'
          '成交供应商：某医疗器械有限公司\n品牌：湘仪\n规格型号：XKA-2200\n'
          '数量：1\n单价(元)：17480\n')
#: 表格形态：型号只出现在单元格里，正文没有一句「规格型号：XKA-2200」。
TABLE = ('品目号 | 品目名称 | 品牌 | 规格型号 | 数量（单位） | 单价(元) | 总价(元)\n'
         '1-1 | 其他仪器仪表 | 高速冷冻离心机 | 湘仪 | XKA-2200 | 1.00(台) | 17,480.00 | 17,480.00\n')


def _source(url, body=NOTICE):
    return {'url': url, 'source_type': 'tender_document', 'body_status': 'fetched',
            'body_text': body, 'provider': 'ccgp'}


def _client(service, calls):
    class Client:
        endpoint = 'test://source-limit'
        model = 'offline'
        last_usage = None

        def analyze(self, system_prompt, user_prompt):
            calls.append(json.loads(user_prompt).get('url'))
            return {'prices': []}

    service.client_factory = Client
    return calls


def _prepare(service, run, sources):
    service.verify_publishers = lambda *args, **kwargs: None
    run['baseline'] = {**run['baseline'], 'brand': '湘仪', 'model': MODEL}
    run['sources'] = sources
    run['prices'] = []
    run['price_source_skips'] = []
    run['price_rejections'] = []
    run['gaps'] = []
    run['model_results'] = {}
    run['limits'] = {**run.get('limits', {}), 'model': 40}
    run['usage'] = {key: 0 for key in run['limits']}
    return run['baseline']


# ---------------------------------------------------------------------------
# 留痕：被额度挡掉要看得见
# ---------------------------------------------------------------------------

def test_the_two_budgets_are_the_same_number():
    """上限度与补扫额度必须是同一个数，否则会出现两套口径。"""
    assert LATE_PRICE_SOURCE_LIMIT == PRICE_SOURCE_LIMIT


def test_a_body_that_declares_the_model_is_ranked_first():
    declared = _source('u-declared', NOTICE)
    table = _source('u-table', TABLE)
    assert _declares_target_model(NOTICE, MODEL)
    assert not _declares_target_model(TABLE, MODEL)
    ordered = sorted([table, declared], key=lambda item: _price_source_rank(item, MODEL))
    assert [item['url'] for item in ordered] == ['u-declared', 'u-table']


def test_bodies_in_the_same_rank_have_stable_order_across_provider_order():
    """同档来源按稳定键排序，不能把额度交给搜索返回顺序。"""
    first = _source('u-first', TABLE)
    second = _source('u-second', TABLE)
    forward = sorted([first, second], key=lambda item: _price_source_rank(item, MODEL))
    reverse = sorted([second, first], key=lambda item: _price_source_rank(item, MODEL))
    assert [item['url'] for item in forward] == ['u-first', 'u-second']
    assert [item['url'] for item in reverse] == ['u-first', 'u-second']


def test_a_dropped_source_is_recorded_and_said_out_loud(research):  # noqa: F811
    service, run, _, _ = research
    sources = [_source('http://example.gov.cn/notice-%d.htm' % index)
               for index in range(PRICE_SOURCE_LIMIT + 1)]
    baseline = _prepare(service, run, sources)
    calls = _client(service, [])

    service.prices(run, baseline)

    assert len(calls) == PRICE_SOURCE_LIMIT
    dropped = [item for item in run['price_source_skips']
               if item.get('skip_reason') == 'price_source_limit']
    assert len(dropped) == 1
    assert dropped[0]['url'] == sources[-1]['url']
    # 记录里要说清这条正文是不是在讲成交——只看 body_len 分不出真公告与 349 字节的表格片段。
    assert dropped[0]['body_has_award_words'] is True
    assert any('超出本轮核价额度' in gap for gap in run['gaps'])


def test_the_gap_is_written_once_per_target(research):  # noqa: F811
    service, run, _, _ = research
    sources = [_source('http://example.gov.cn/notice-%d.htm' % index)
               for index in range(PRICE_SOURCE_LIMIT + 1)]
    baseline = _prepare(service, run, sources)
    _client(service, [])

    service.prices(run, baseline)
    service.prices(run, baseline)

    assert len([gap for gap in run['gaps'] if '超出本轮核价额度' in gap]) == 1


# ---------------------------------------------------------------------------
# 第二次机会：补扫必须能读到被额度挡掉的来源
# ---------------------------------------------------------------------------

def test_the_rescan_reads_the_source_the_first_budget_could_not_afford(research):  # noqa: F811
    service, run, _, _ = research
    sources = [_source('http://example.gov.cn/notice-%d.htm' % index)
               for index in range(PRICE_SOURCE_LIMIT + 1)]
    baseline = _prepare(service, run, sources)
    first_pass = _client(service, [])

    service.prices(run, baseline)
    dropped_url = sources[-1]['url']
    assert dropped_url not in first_pass, '前置：它确实没被读过'

    second_pass = _client(service, [])
    added = service._rescan_late_price_sources(run, baseline)

    assert dropped_url in second_pass, '被额度挡掉的来源必须在补扫轮被读一次'
    assert added == []


def test_the_rescan_still_skips_reasons_that_were_real_readings(research):  # noqa: F811
    """只有「额度」不是一次阅读；别的 skip 理由仍然是已经考虑过。"""
    service, run, _, _ = research
    sources = [_source('http://example.gov.cn/notice-%d.htm' % index)
               for index in range(PRICE_SOURCE_LIMIT + 1)]
    baseline = _prepare(service, run, sources)
    _client(service, [])
    service.prices(run, baseline)
    # 手工把最后一条的理由改成「正文里没有目标型号」：它不该再被补扫。
    for item in run['price_source_skips']:
        if item.get('url') == sources[-1]['url']:
            item['skip_reason'] = 'target_model_not_in_body'

    calls = _client(service, [])
    service._rescan_late_price_sources(run, baseline)

    assert sources[-1]['url'] not in calls
