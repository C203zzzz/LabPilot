"""省级政府采购平台「按型号直查成交公告」通道：接口参数、正文、缓存、缺口口径、采信边界。

这些用例**全部离线**：注入的 transport 直接回构造好的响应，不打任何真实站点
（真实站点行为另由端到端验证记录，见 ``设计说明``）。

构造响应的形状是**照真实响应抄的**，不是编的（2026-09-25 实站取证，见
``_probe/p1_fields.py`` / ``p8_pipeline.py``）：

* 响应是 ``{"success":true,"result":{"total":…,"data":[…]}}``；
* 列表行里 ``content`` 直接带正文 HTML（所以一次请求就有型号与单价）；
* **缺 ``district`` 时站点直接拒绝**：``{"success":false,"result":null,
  "error":"区划和实例白名单不能同时为空"}``；
* 安徽的成交标的表**表头比数据行多一列**（``报价明细`` 列在数据行里是空的，
  清洗时尾空格被 ``render_table`` 的 ``rstrip(' |')`` 吃掉），江西没有这一列。
"""
import sqlite3

import pytest

from backend.infrastructure.search_providers import PublicWebSearchProvider, TLS_PROFILES
from backend.repositories.sqlite import SQLiteRepository
from backend.services import procurement_research as pr
from backend.services import province_award_sources as pas
from backend.services.procurement_rules import validate_price
from backend.services.procurement_tables import price_table_rows
from backend.services.research_sources import is_tender
from backend.services.source_authority import publication_tier
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture

MODEL = 'DHG-9240A'

#: 安徽：``报价明细`` 是表头第 8 列，数据行是空的（清洗后只剩 7 格）。
ANHUI_CONTENT = (
    '<p>项目名称: 某学院关于教学仪器的网上超市采购项目</p>'
    '<p>项目编号: 2251451000001337350</p>'
    '<p>本项目采购已经结束，现将采购结果公示如下：</p>'
    '<p>一、项目信息</p>'
    '<p>项目联系人: 张三</p>'
    '<p>二、采购单位信息</p>'
    '<p>采购单位名称: 某学院</p>'
    '<p>三、成交信息</p>'
    '<p>成交日期: 2026年9月24日 总成交金额（元）: 10640 （人民币）</p>'
    '<p>成交供应商名称、联系地址及成交金额:</p>'
    '<table><tr><td>序号</td><td>成交供应商名称</td><td>成交供应商地址</td><td>成交金额（元）</td></tr>'
    '<tr><td>1</td><td>合肥某商贸有限公司</td><td>安徽省合肥市</td><td>10640.0</td></tr></table>'
    '<p>四、成交标的名称、规格型号、数量、单价、成交金额:</p>'
    '<table>'
    '<tr><td>序号</td><td>标的名称</td><td>品牌</td><td>规格型号</td><td>数量</td>'
    '<td>单价(元)</td><td>成交金额（元）</td><td>报价明细</td></tr>'
    '<tr><td>1</td><td>上海一恒 电子电热恒温鼓风干燥箱DHG-9240A</td><td>上海一恒</td>'
    '<td>DHG-9240A</td><td>1</td><td>4660.0</td><td>4660.0</td><td></td></tr>'
    '<tr><td>2</td><td>上海一恒 PH-240A干培两用箱</td><td>上海一恒</td>'
    '<td>PH-240A</td><td>1</td><td>5980.0</td><td>5980.0</td><td></td></tr>'
    '<tr><td>3</td><td>【运费】</td><td></td><td></td><td>1</td><td></td><td>0.0</td><td></td></tr>'
    '</table>'
)

#: 江西：表头与数据行都是 7 列（不需要整形），但**汇总写万元、明细写元**。
JIANGXI_CONTENT = (
    '<p>球磨机、干燥箱、压片机（材料与智能制造学院）竞价成交公告</p>'
    '<p>项目编号： 62025111400482601</p>'
    '<p>本项目采购已经结束，现将采购结果公示如下：</p>'
    '<p>采购单位名称： 某大学材料与智能制造学院</p>'
    '<p>成交日期： 2025年11月14日</p>'
    '<p>总成交金额： 1.05 （万元）</p>'
    '<p>成交供应商名称、联系地址及成交金额：</p>'
    '<table><tr><td>序号</td><td>成交供应商名称</td><td>成交供应商地址</td><td>成交金额（元）</td></tr>'
    '<tr><td>1</td><td>南昌某实业有限公司</td><td>江西省南昌市</td><td>10500.00</td></tr></table>'
    '<p>四、成交标的名称、规格型号、数量、单价、成交金额</p>'
    '<table>'
    '<tr><td>序号</td><td>标的名称</td><td>品牌</td><td>规格型号</td><td>数量</td>'
    '<td>单价(元)</td><td>成交金额（元）</td></tr>'
    '<tr><td>4</td><td>尚仪 电热鼓风干燥箱</td><td>尚仪</td><td>型号:DHG-9240A</td>'
    '<td>5台</td><td>2100.00</td><td>10500.00</td></tr>'
    '</table>'
)


def _response(content, *, total=1, encrypt='Zx9/KQ==', district='349900',
              title='某学院关于教学仪器的网上超市采购项目成交公告',
              project='2251451000001337350'):
    return {
        'success': True,
        'result': {'total': total, 'data': [{
            'content': content, 'encryptId': encrypt, 'district': district,
            'districtName': '安徽省本级', 'title': title, 'projectName': title,
            'projectCode': project, 'announcementType': 8013,
            'announcementTypeName': '电子卖场成交公告(采购成功)',
            'releasedAt': 1758672000000,
        }]},
    }


def anhui_payload(**kwargs):
    return _response(ANHUI_CONTENT, **kwargs)


def jiangxi_payload(**kwargs):
    kwargs.setdefault('district', '369900')
    return _response(JIANGXI_CONTENT, **kwargs)


class Recorder:
    """记录每一次出去的请求原文，并回放构造的响应。"""

    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls = []

    def __call__(self, url, *, method, body, headers, tls_profile, timeout):
        self.calls.append({'url': url, 'method': method, 'body': body,
                           'headers': dict(headers or {}), 'tls_profile': tls_profile})
        if not self.script:
            raise AssertionError(f'没有为第 {len(self.calls)} 次请求准备响应: {url}')
        payload = self.script.pop(0)
        if isinstance(payload, Exception):
            raise payload
        import json
        return json.dumps(payload, ensure_ascii=False).encode('utf-8')

    @property
    def bodies(self):
        import json
        return [json.loads(call['body']) if isinstance(call['body'], (str, bytes))
                else call['body'] for call in self.calls]


@pytest.fixture
def repo(tmp_path):
    return SQLiteRepository(tmp_path / 'cache.sqlite3')


@pytest.fixture
def service(research):  # noqa: F811 - the shared offline app fixture
    service, run, provider, _client = research
    return service, run, provider


# ---------------------------------------------------------------------------
# 接口参数：keyword 搜正文、district 必传
# ---------------------------------------------------------------------------

def test_the_body_uses_keyword_not_projectname_and_always_names_the_district():
    """``projectName`` 只搜项目名，型号写在正文里——用错参数得到的是假否定。

    这是 ``设计规范`` 那份"不能按型号查成交价"结论翻案的地方：同一批型号，
    换 ``keyword`` 就命中了。
    """
    transport = Recorder([anhui_payload()])
    found = pas.lookup(repo=None, target={'model': MODEL}, transport=transport,
                       task_id='t1', platforms=(pas._BY_KEY['anhui'],))
    assert len(transport.calls) == 1
    body = transport.bodies[0]
    assert body['keyword'] == MODEL
    assert 'projectName' not in body, 'projectName 只搜项目名，不能拿它当型号检索'
    assert body['district'] == '349900', 'district 必须显式传，不传会被按 IP 定位接管'
    assert body['announcementTypes'] == [8020, 8012, 8013]
    assert 0 < body['pageSize'] <= 100, '接口 pageSize 上限 100'
    assert found['announcements'], '这次响应里有正文，必须被解析出来'


def test_the_registry_holds_exactly_the_registered_platforms():
    """两份注册表各自钉死；``registered_hosts`` 认的是**全集**。

    卖场家族（安徽 / 江西）这两条断言一个字没改——加浙江 / 广西**不许动它们**。
    """
    assert [platform.key for platform in pas.PROVINCES] == ['anhui', 'jiangxi']
    assert [platform.key for platform in pas.PORTAL_PROVINCES] == ['zhejiang', 'guangxi']
    assert pas.ALL_PROVINCES == pas.PROVINCES + pas.PORTAL_PROVINCES
    assert set(pas._BY_KEY) == {'anhui', 'jiangxi', 'zhejiang', 'guangxi'}
    assert pas.registered_hosts() == {'mall.anhui.zcygov.cn', 'www.jxemall.com',
                                      'zfcg.czt.zj.gov.cn', 'zfcg.gxzf.gov.cn'}
    for url, key in [('https://mall.anhui.zcygov.cn/luban/announcement/detail?x=1', 'anhui'),
                     ('https://www.jxemall.com/announcement/lobby/queryPage', 'jiangxi'),
                     ('https://zfcg.czt.zj.gov.cn/portal/detail?articleId=x', 'zhejiang'),
                     ('https://zfcg.gxzf.gov.cn/portal/all', 'guangxi')]:
        assert pas.platform_for_url(url).key == key
    # 别的省的政采云不在名单里（zcygov.cn 下有很多省，不能只按根域认）。
    for url in ['https://mall.jiangxi.zcygov.cn/x', 'https://www.example.com/x',
                'https://mall.anhui.zcygov.cn.evil.org/x',
                'https://zfcg.czt.zj.gov.cn.evil.org/x']:
        assert pas.platform_for_url(url) is None


def test_the_default_district_is_only_the_province_level_seat():
    """默认只查省本级那一格；区划是配置项，扩全省是改配置而不是重写。

    ``349900`` 是"安徽省本级"，**不等于全省**；``district`` 是硬过滤，市县公告在
    省本级查询里搜不到（``设计规范`` §6.4）。
    """
    assert [platform.districts for platform in pas.PROVINCES] == [('349900',), ('369900',)]
    assert pas.PROVINCES[0].query_body('349900', MODEL)['district'] == '349900'


def test_jiangxi_declares_the_tls_posture_and_anhui_does_not():
    """江西只接受 legacy renegotiation；姿态写在平台配置上，由产品读取器执行。"""
    assert pas._BY_KEY['jiangxi'].tls_profile == 'legacy-renegotiation'
    assert pas._BY_KEY['anhui'].tls_profile is None
    assert pas._BY_KEY['jiangxi'].tls_profile in TLS_PROFILES


# ---------------------------------------------------------------------------
# 正文：列表响应里的 content 能读出型号与单价
# ---------------------------------------------------------------------------

def test_the_list_content_yields_a_real_unit_price_through_the_existing_gates(repo):
    """一步到位的核心：列表响应 → 正文 → 既有表格判据 → ``validate_price``。"""
    transport = Recorder([jiangxi_payload()])
    found = pas.lookup(repo, {'model': MODEL}, transport=transport, task_id='t1')
    announcement = found['announcements'][0]
    body = pas.body_text(announcement)

    rows = price_table_rows(body, MODEL)
    assert len(rows) == 1, '江西的表头与数据行列数相等，本来就读得出来'
    assert rows[0]['amount'] == 2100.0 and rows[0]['quantity'] == 5.0

    source = {'url': announcement['url'], 'body_status': 'fetched', 'body_text': body,
              'source_type': 'unknown'}
    assert is_tender(source), '成交公告必须由既有的 is_tender 认出来'
    source['source_type'] = 'tender_document'
    accepted = validate_price({**rows[0], 'kind': 'award'}, source,
                              {'brand': '', 'model': MODEL})
    assert accepted['unit_price'] == 2100.0
    # 单价是明细列里的数，不是"总成交金额 1.05（万元）"折出来的 10500。
    assert accepted['amount_unit'] == '元'


def test_the_anhui_extra_trailing_column_is_recovered_without_touching_any_row():
    """安徽的表头多一列 ``报价明细``；整形只动**表头那一行**，数据行逐字保留。"""
    from backend.infrastructure.search_providers import PublicWebSearchProvider
    plain = PublicWebSearchProvider._clean_html(ANHUI_CONTENT, pas.PROVINCES[0].detail_page)
    shaped = pas.shape_award_table(plain)

    plain_lines, shaped_lines = plain.splitlines(), shaped.splitlines()
    assert len(plain_lines) == len(shaped_lines)
    changed = [i for i, (a, b) in enumerate(zip(plain_lines, shaped_lines)) if a != b]
    assert len(changed) == 1, '只有表头那一行该变'
    assert '报价明细' in plain_lines[changed[0]]
    assert '报价明细' not in shaped_lines[changed[0]]
    assert '规格型号' in shaped_lines[changed[0]] and '单价(元)' in shaped_lines[changed[0]]

    assert price_table_rows(plain, MODEL) == [], '前提：不整形这张表一行都读不出来'
    rows = price_table_rows(shaped, MODEL)
    assert [row['amount'] for row in rows] == [4660.0]


def test_a_row_that_is_not_exactly_one_column_short_is_left_alone():
    """有一行列数不对就不整形——宁可这一条读不出来，也不猜列的位置。"""
    broken = ANHUI_CONTENT.replace(
        '<td>PH-240A</td><td>1</td><td>5980.0</td><td>5980.0</td><td></td>',
        '<td>PH-240A</td><td>1</td><td>5980.0</td><td>5980.0</td><td>明细</td>')
    from backend.infrastructure.search_providers import PublicWebSearchProvider
    plain = PublicWebSearchProvider._clean_html(broken, pas.PROVINCES[0].detail_page)
    assert pas.shape_award_table(plain) == plain


def test_a_total_that_does_not_add_up_voids_the_reshaping():
    """自校验：单元格的 ``单价 × 数量`` 加总必须等于**页面自己写的**总成交金额。

    对不上说明列关系没恢复对（例如把整包价当成了单价），此时必须原样返回。
    """
    wrong = ANHUI_CONTENT.replace('总成交金额（元）: 10640', '总成交金额（元）: 99999')
    from backend.infrastructure.search_providers import PublicWebSearchProvider
    plain = PublicWebSearchProvider._clean_html(wrong, pas.PROVINCES[0].detail_page)
    assert pas.shape_award_table(plain) == plain
    assert price_table_rows(plain, MODEL) == []


def test_the_summary_in_ten_thousands_is_not_read_as_the_unit_price():
    """江西同一份公告里明细是元、汇总是万元（``设计规范`` §4.4）：不能差 10000 倍。"""
    body = pas.body_text(pas.parse_response(
        pas._BY_KEY['jiangxi'], jiangxi_payload(), MODEL, '369900')['announcements'][0])
    assert '总成交金额： 1.05 （万元）' in body
    assert pas._total_yuan(body) == 10500.0
    assert price_table_rows(body, MODEL)[0]['amount'] == 2100.0


def test_a_row_whose_body_does_not_really_contain_the_model_is_dropped():
    """接口是全文检索，但落库前**本地再验一遍**：核不到的行丢掉并计数。"""
    payload = _response(ANHUI_CONTENT)
    payload['result']['data'][0]['content'] = '<p>本项目采购已经结束，现将采购结果公示如下：</p><p>采购单位名称: 某学院</p>'
    parsed = pas.parse_response(pas._BY_KEY['anhui'], payload, MODEL, '349900')
    assert parsed['announcements'] == []
    assert parsed['rejected'] == 1


# ---------------------------------------------------------------------------
# 缓存：同一个型号第二次查询不再打网络（跨任务、跨用户）
# ---------------------------------------------------------------------------

def test_the_second_lookup_of_the_same_model_sends_nothing(repo):
    first = Recorder([anhui_payload(), jiangxi_payload()])
    pas.lookup(repo, {'model': MODEL}, transport=first, task_id='task-a')
    assert len(first.calls) == 2, '两个平台各一次'
    second = Recorder([])
    found = pas.lookup(repo, {'model': MODEL}, transport=second, task_id='task-b')
    assert second.calls == [], '第二次查询一个请求都不该发'
    assert [q['source'] for q in found['queries']] == ['cache', 'cache']
    assert found['announcements'], '缓存命中也要把公告交出来'


def test_the_cache_is_cross_task_and_survives_a_new_service_instance(tmp_path):
    path = tmp_path / 'shared.sqlite3'
    writer = SQLiteRepository(path)
    pas.lookup(writer, {'model': MODEL}, transport=Recorder([anhui_payload(), jiangxi_payload()]),
               task_id='task-a')
    writer.close()
    reader = SQLiteRepository(path)
    silent = Recorder([])
    found = pas.lookup(reader, {'model': MODEL}, transport=silent, task_id='task-b')
    assert silent.calls == []
    assert {item['platform'] for item in found['announcements']} == {'anhui', 'jiangxi'}
    reader.close()


def test_the_cache_key_carries_the_district_and_the_exact_model_string():
    """省本级与市县的结果互斥，写法不同的型号也不是一回事——键里两个都要有。"""
    base = pas.cache_key('anhui', '349900', MODEL)
    assert base != pas.cache_key('anhui', '349901', MODEL)
    assert base != pas.cache_key('jiangxi', '349900', MODEL)
    assert base != pas.cache_key('anhui', '349900', 'DHG9240A')
    assert base == pas.cache_key('anhui', '349900', MODEL)


def test_an_expired_entry_is_queried_again(repo):
    from datetime import datetime, timedelta, timezone
    stale = datetime.now(timezone.utc) - timedelta(seconds=pas.CACHE_TTL_SECONDS + 60)
    pas.lookup(repo, {'model': MODEL}, transport=Recorder([anhui_payload(), jiangxi_payload()]),
               task_id='t1', now=stale)
    again = Recorder([anhui_payload(), jiangxi_payload()])
    found = pas.lookup(repo, {'model': MODEL}, transport=again, task_id='t2')
    assert len(again.calls) == 2, '过期就要重新查'
    assert [q['source'] for q in found['queries']] == ['network', 'network']


def test_the_cache_stores_the_evidence_never_a_price_or_a_verdict(repo):
    """缓存里只有平台给的正文与元数据：价格每次重算，所以缓存不可能绕过判据。

    号码掩码（``设计规范`` §4 第 7 行）就落在这一步：缓存是**跨任务**可读的，
    写脏一次等于把真号散到后面每一个任务里。这份构造正文里没有号码，所以掩码是
    恒等变换——这里同时钉住"掩码不许顺手改动正文"（价格与项目编号一个都不许动）。
    """
    pas.lookup(repo, {'model': MODEL}, transport=Recorder([anhui_payload(), jiangxi_payload()]),
               task_id='t1')
    record = repo.get(pas.CACHE_TABLE, pas.cache_key('anhui', '349900', MODEL))
    assert record['model'] == MODEL and record['district'] == '349900'
    assert record['platform'] == 'anhui'
    assert record['announcements'][0]['content'] == ANHUI_CONTENT, '原文照存（这份正文里没有号码，掩码是恒等）'
    for kept in ('10640.0', '4660.0', '5980.0', '2251451000001337350', 'DHG-9240A'):
        assert kept in record['announcements'][0]['content'], kept
    flat = repr(record)
    for forbidden in ('unit_price', 'validate_price', 'amount', 'source_type'):
        assert forbidden not in flat, forbidden


def test_the_cached_content_is_masked_before_it_lands(repo):
    """跨任务缓存落盘前过号码掩码：手机号与 18 位号不能被缓存抄一份出去。

    缓存与 ``run['sources']`` 看到的是同一份内容，掩在写缓存这一步，两边才不会
    一个有掩一个没有。反例同时钉住：金额与项目编号一位不动。
    """
    content = (ANHUI_CONTENT.replace('<p>项目联系人: 张三</p>',
                                     '<p>项目联系人: 张三，联系电话 13800000000</p>')
               .replace('<p>采购单位名称: 某学院</p>',
                        '<p>采购单位名称: 某学院，统一社会信用代码 913401001234567890</p>'))
    assert '13800000000' in content and '913401001234567890' in content
    pas.lookup(repo, {'model': MODEL}, transport=Recorder([_response(content)]), task_id='t1')
    record = repo.get(pas.CACHE_TABLE, pas.cache_key('anhui', '349900', MODEL))
    stored = record['announcements'][0]['content']
    assert '13800000000' not in stored and '913401001234567890' not in stored
    assert '***' in stored
    for kept in ('10640.0', '4660.0', '2251451000001337350', 'DHG-9240A', '某学院'):
        assert kept in stored, kept
    # 缓存命中那一次也不能把真号发回给调用方（读的是同一行缓存）。
    second = pas.lookup(repo, {'model': MODEL}, transport=Recorder([]), task_id='t2')
    assert second['announcements'][0]['cache_hit'] is True
    assert '13800000000' not in second['announcements'][0]['content']


def test_a_zero_hit_result_is_cached_too(repo):
    """查过了、没有——也要进缓存，否则没人查过的长尾型号会每次任务都重打一遍网络。"""
    empty = {'success': True, 'result': {'total': None, 'data': None}}
    first = Recorder([empty, empty])
    found = pas.lookup(repo, {'model': 'NO-SUCH-MODEL-999'}, transport=first, task_id='t1')
    assert found['announcements'] == [] and found['failures'] == []
    assert all(q['hits'] == 0 for q in found['queries'])
    second = Recorder([])
    found = pas.lookup(repo, {'model': 'NO-SUCH-MODEL-999'}, transport=second, task_id='t2')
    assert second.calls == []
    assert all(q['source'] == 'cache' and q['hits'] == 0 for q in found['queries'])


# ---------------------------------------------------------------------------
# 「查过了没有」与「根本没查成」必须分开
# ---------------------------------------------------------------------------

def test_a_rejected_request_is_a_failure_not_a_zero_hit(repo):
    """带 ``district`` 是硬要求：站点拒绝时**绝不能**当成"0 命中"。

    ``{"success":false,"result":null,"error":"区划和实例白名单不能同时为空"}``
    是实站对缺 ``district`` 的回应。把它读成空结果，就等于把"没查成"说成"没有"——
    ``设计规范`` 的假否定就是这么来的。
    """
    rejected = {'success': False, 'result': None, 'error': '区划和实例白名单不能同时为空'}
    found = pas.lookup(repo, {'model': MODEL},
                       transport=Recorder([rejected, rejected]), task_id='t1')
    assert found['announcements'] == []
    assert found['queries'] == []
    assert len(found['failures']) == 2
    assert '区划和实例白名单' in found['failures'][0]['message']
    assert repo.get(pas.CACHE_TABLE, pas.cache_key('anhui', '349900', MODEL)) is None, \
        '没查成不该写缓存'


def test_a_network_error_is_a_failure_not_a_zero_hit(repo):
    found = pas.lookup(repo, {'model': MODEL},
                       transport=Recorder([RuntimeError('TLS handshake failed'),
                                           TimeoutError('timed out')]), task_id='t1')
    assert found['announcements'] == []
    assert {f['platform'] for f in found['failures']} == {'anhui', 'jiangxi'}
    assert 'TLS handshake failed' in found['failures'][0]['message']


def test_a_zero_hit_run_leaves_no_user_facing_text_and_no_failure(service, monkeypatch):
    """命中 0 条是**正常结果**：不写面向用户的文案、不报错、不算缺口。

    产品设计规范（2026-09-25）：免责式文案会让产品显得不可靠。但"查过了"
    这件事仍旧要留痕——否则第二次查询就分不清"查过没有"与"从没查过"。
    """
    service, run, provider = service
    empty = {'success': True, 'result': {'total': None, 'data': None}}
    provider.fetch_form = lambda url, **kwargs: (
        __import__('json').dumps(empty, ensure_ascii=False).encode(), None)
    gaps_before = list(run['gaps'])
    run.pop('province_award_queries', None)
    service.province_platforms = pas.PROVINCES
    added = service.province_award_sources(run, {'model': MODEL})
    assert added == 0
    assert run['gaps'] == gaps_before, '命中 0 条不许产出任何面向用户的文案'
    assert run['province_award_queries'] == [
        {'platform': 'anhui', 'district': '349900', 'source': 'network', 'hits': 0,
         'total': None, 'rows': 0, 'rejected': 0, 'status': 'queried'},
        {'platform': 'jiangxi', 'district': '369900', 'source': 'network', 'hits': 0,
         'total': None, 'rows': 0, 'rejected': 0, 'status': 'queried'},
    ]


def test_a_failed_query_does_leave_a_gap_because_it_is_not_a_zero_hit(service):
    """没查成**绝不静默**：用户必须能看出这一轮没读成、该重试。"""
    service, run, provider = service
    provider.fetch_form = lambda url, **kwargs: (
        (_ for _ in ()).throw(RuntimeError('TLS handshake failed')))
    gaps_before = list(run['gaps'])
    run.pop('province_award_queries', None)
    service.province_platforms = pas.PROVINCES
    added = service.province_award_sources(run, {'model': MODEL})
    assert added == 0
    new = [gap for gap in run['gaps'] if gap not in gaps_before]
    assert len(new) == 2, '两个平台各一条'
    assert all('按型号直查未完成' in gap and 'RuntimeError' in gap for gap in new)
    assert all(platform in ' '.join(new) for platform in ('安徽', '江西'))


def test_no_form_capable_reader_sends_nothing_and_writes_no_gap(service):
    """没有能发 POST 的读取器 = 环境里没有出口，不是一次失败的查询。

    它**一个请求都不发**（``设计规范``：通道自己开连接会让离线用例真去连站点），
    也不写面向用户的文案；区分留在 ``province_award_queries`` 里。
    """
    service, run, _provider = service
    assert not hasattr(_provider, 'fetch_form'), '前提：这个离线读取器不会发表单'
    gaps_before = list(run['gaps'])
    run.pop('province_award_queries', None)
    assert service.province_award_sources(run, {'model': MODEL}) == 0
    assert run['gaps'] == gaps_before
    assert run['province_award_queries'] == [
        {'status': 'not_attempted', 'reason': 'no_form_capable_reader'}]


def test_quota_exhaustion_stops_quietly(repo):
    """额度用完安静收手：不是错误，也不该把后面的平台算成"没有"。"""
    from backend.services.education_platform_sources import QuotaReached

    class Budget:
        def __call__(self, *args, **kwargs):
            raise QuotaReached()

    found = pas.lookup(repo, {'model': MODEL}, transport=Recorder([]), task_id='t1',
                       on_request=Budget())
    assert found['announcements'] == [] and found['failures'] == []
    assert found['queries'] == [{'platform': 'anhui', 'district': '349900',
                                'source': 'quota', 'hits': 0}]


def test_the_channel_has_its_own_budget_that_covers_a_cold_run():
    """额度必须罩住"四省两族冷启动一轮"，同时仍旧与既有通道分账。

    一轮 = 一个型号（``_execute`` 里 ``b = run['baseline']``，这条通道只被调一次）：

    * 每个平台一次列表查询（4 次；卖场家族不需要详情）；
    * 政采云内核家族每个平台最多 ``detail_limit`` 条详情（浙江 4 + 广西 4 = 8 次）。

    最坏一轮 = 4 + 8 = 12。原来的 8 **不够**：光是两个政采云站各回读 4 条详情就已经
    8 次，列表查询还没算进去。命中缓存时仍然是 0 次。
    """
    worst_case = len(pas.ALL_PROVINCES) + sum(platform.detail_limit
                                              for platform in pas.ALL_PROVINCES)
    assert worst_case == 12, '平台数或 detail_limit 改了，这条不等式要重算'
    assert pr.PROVINCE_AWARD_LIMIT == 16 and pr.PROVINCE_AWARD_LIMIT >= worst_case
    assert 'province_award' not in pr.RESEARCH_LIMITS, '既有通道的额度不许被挪用'
    assert pr.PROVINCE_AWARD_LIMIT >= len(pas.ALL_PROVINCES), '每个平台各一次请求是底线'


# ---------------------------------------------------------------------------
# 采信边界：一条判据都没有为这两个省放开
# ---------------------------------------------------------------------------

def test_the_source_type_is_judged_by_is_tender_and_never_asserted(service):
    """标题写着"成交公告"不算——`source_type` 由正文过既有的 ``is_tender`` 判。"""
    service, run, _provider = service
    announcement = {'platform': 'anhui', 'platform_label': '安徽',
                    'url': 'https://mall.anhui.zcygov.cn/luban/announcement/detail?encryptId=x&district=349900',
                    'title': '某项目成交公告', 'content': '<p>某学院采购一批器材，欢迎报价。</p>',
                    'project_code': 'P1', 'district': '349900'}
    source = service.province_award_record(run, announcement)
    assert source['body_status'] == 'fetched'
    assert source['source_type'] == 'unknown', '正文里没有结论性措辞就不能当成交公告'
    assert not is_tender(source)


def test_the_two_new_decision_phrases_are_the_only_vocabulary_change():
    """``采购结果公示`` / ``成交结果公示`` 是与 ``采购结果公告`` 同层的结论性措辞。

    这两个词是安徽／江西成交公告正文的**首句**（"本项目采购已经结束，现将采购结果
    公示如下："）。爆炸半径量过：本机库里 230 条已抓正文的来源，加词后新拿到 tier
    的是 **0 条**（``_probe/p6_blast_radius.py``）。
    """
    host = 'mall.anhui.zcygov.cn'
    award = ('项目编号：P1\n采购单位：某学院\n成交供应商：某公司\n成交金额：10640元\n'
             '本项目采购已经结束，现将采购结果公示如下：\n'
             '货物名称 | 规格型号 | 数量 | 单价（元）\n烘干箱 | DHG-9240A | 1 | 4660\n')
    assert publication_tier(host, award) == 'formal_publication'
    solicitation = award.replace('现将采购结果公示如下：', '现邀请合格供应商参加报价：')
    assert publication_tier(host, solicitation) is None, '询价邀请不是成交公告'
    cart = award.replace('采购单位：某学院', '本店现货 加入购物车')
    assert publication_tier(host, cart) is None, '商城话术仍然一票否决'


def test_the_record_still_needs_the_real_model_and_a_real_amount(service):
    """整形只改列关系，改不了判据：型号不对、钱不是单价，照样过不去。"""
    service, run, _provider = service
    announcement = {'platform': 'jiangxi', 'platform_label': '江西',
                    'url': 'https://www.jxemall.com/luban/announcement/detail?encryptId=x&district=369900',
                    'title': '球磨机、干燥箱、压片机竞价成交公告', 'content': JIANGXI_CONTENT,
                    'project_code': 'P1', 'district': '369900'}
    source = service.province_award_record(run, announcement)
    assert source['source_type'] == 'tender_document'
    row = price_table_rows(source['body_text'], MODEL)[0]
    assert validate_price({**row, 'kind': 'award'}, source,
                          {'brand': '', 'model': 'OTHER-MODEL-1'}) is None
    # 汇总行的 10500 不是单价：拿它当 amount 会被采信链拒掉。
    assert validate_price({**row, 'kind': 'award', 'amount': 10500.0,
                           'amount_scope': 'package_total'}, source,
                          {'brand': '', 'model': MODEL}) is None


def test_every_announcement_carries_a_snapshot_and_a_provenance_note(service):
    """正文不是"我们转述的"：快照按内容寻址落盘，并写清它来自列表响应的哪个字段。"""
    service, run, _provider = service
    announcement = {'platform': 'anhui', 'platform_label': '安徽',
                    'url': 'https://mall.anhui.zcygov.cn/luban/announcement/detail?encryptId=x&district=349900',
                    'title': '某项目成交公告', 'content': ANHUI_CONTENT,
                    'project_code': 'P1', 'district': '349900', 'published_at': '2026-09-24T00:00:00+00:00'}
    source = service.province_award_record(run, announcement)
    assert source['snapshot']['size'] == len(ANHUI_CONTENT.encode())
    from pathlib import Path
    payload = Path(source['snapshot']['path']).read_bytes()
    assert payload == ANHUI_CONTENT.encode()
    assert source['text_provenance']['method'] == 'province_platform_list_content'
    assert source['text_provenance']['project_code'] == 'P1'


def test_the_province_body_and_snapshot_land_without_numbers(service):
    """省级通道的正文与快照：号码落盘前就被掩掉，价格判据要用的数字一个不少。

    这条通道**自己拼正文**（正文来自列表响应的 ``content`` 字段），不进
    ``fetch_source``，所以共享落盘点覆盖不到它（``设计规范`` §4 第 6 行）。
    掩的是 ``content`` 原文：``snapshot`` 与 ``body_text`` 都由它派生，先掩原文
    两处才同时干净——只掩正文会把真号留在按内容寻址的 ``.bin`` 快照里。
    """
    service, run, _provider = service
    from pathlib import Path
    content = ANHUI_CONTENT.replace(
        '<p>项目联系人: 张三</p>',
        '<p>项目联系人: 张三，联系电话 138-0000-0000</p>'
        '<p>统一社会信用代码 913401001234567890</p>')
    announcement = {'platform': 'anhui', 'platform_label': '安徽',
                    'url': 'https://mall.anhui.zcygov.cn/luban/announcement/detail?encryptId=x&district=349900',
                    'title': '某项目成交公告', 'content': content,
                    'project_code': '2251451000001337350', 'district': '349900',
                    'published_at': '2026-09-24T00:00:00+00:00'}
    source = service.province_award_record(run, announcement)
    assert source['body_status'] == 'fetched'
    assert '138-0000-0000' not in source['body_text']
    assert '913401001234567890' not in source['body_text']
    assert '***' in source['body_text']
    payload = Path(source['snapshot']['path']).read_bytes()
    assert b'138-0000-0000' not in payload and b'913401001234567890' not in payload
    for kept in (b'10640.0', b'4660.0', b'2251451000001337350', b'DHG-9240A'):
        assert kept in payload, kept
    # 判据照旧：型号与单价仍旧从这份正文里读得出来。
    rows = price_table_rows(source['body_text'], MODEL)
    assert rows and rows[0]['amount'] == 4660.0
    assert '2251451000001337350' in source['body_text']


def test_the_platform_rotates_so_one_platform_cannot_take_every_slot():
    found = {'announcements': [{'platform': 'anhui', 'url': f'a{i}'} for i in range(5)] +
                             [{'platform': 'jiangxi', 'url': f'j{i}'} for i in range(5)]}
    chosen = pas.source_records(found, limit=4)
    assert [item['platform'] for item in chosen] == ['anhui', 'jiangxi', 'anhui', 'jiangxi']


# ---------------------------------------------------------------------------
# 读取器：JSON body 与 TLS 姿态走产品自己的出口
# ---------------------------------------------------------------------------

def test_the_transport_hands_the_json_body_and_the_tls_posture_to_the_reader():
    import types

    class Offline:
        name = 'offline'

        def search(self, query):
            return []

    holder = types.SimpleNamespace(search=types.SimpleNamespace(providers=[Offline()]))
    assert pr.ProcurementResearch.province_award_transport(holder) is None

    seen = {}

    class Recording(Offline):
        def fetch_form(self, url, **kwargs):
            seen.update({'url': url, **kwargs})
            return b'payload', None

    holder.search = types.SimpleNamespace(providers=[Offline(), Recording()])
    transport = pr.ProcurementResearch.province_award_transport(holder)
    assert transport('https://www.jxemall.com/x', method='POST', body=b'{"a":1}',
                     headers={'Content-Type': 'application/json;charset=UTF-8'},
                     tls_profile='legacy-renegotiation', timeout=9) == b'payload'
    assert seen == {'url': 'https://www.jxemall.com/x', 'body': b'{"a":1}',
                    'content_type': 'application/json;charset=UTF-8',
                    'tls_profile': 'legacy-renegotiation', 'method': 'POST',
                    'extra_headers': {'Content-Type': 'application/json;charset=UTF-8'},
                    'timeout': 9}


def test_fetch_form_sends_a_json_body_and_refuses_an_unknown_tls_profile(monkeypatch):
    seen = {}

    def fake_request(url, timeout, **kwargs):
        seen.update({'url': url, 'timeout': timeout, **kwargs})
        return b'{}', 'application/json'

    provider = PublicWebSearchProvider()
    monkeypatch.setattr(provider, '_request', fake_request)
    provider.fetch_form('https://mall.anhui.zcygov.cn/q', body='{"district":"349900"}',
                        content_type='application/json;charset=UTF-8')
    assert seen['data'] == b'{"district":"349900"}'
    assert seen['extra_headers']['Content-Type'] == 'application/json;charset=UTF-8'
    assert seen['method'] == 'POST' and seen['tls_profile'] is None

    seen.clear()
    provider.fetch_form('https://www.jxemall.com/q', fields={'a': 'b'},
                        extra_headers={'Referer': 'r'})
    assert seen['data'] == b'a=b'
    assert seen['extra_headers']['Content-Type'] == 'application/x-www-form-urlencoded'
    assert seen['tls_profile'] is None, '没声明姿态就必须走默认上下文'


def test_an_unknown_tls_posture_is_rejected_loudly():
    """写错的姿态必须当场报错，不静默降级成默认上下文（失败会被误读成站点坏了）。"""
    from backend.infrastructure.search_providers import _tls_handlers
    assert TLS_PROFILES == frozenset({'legacy-renegotiation'})
    assert _tls_handlers(None) == ()
    with pytest.raises(ValueError):
        _tls_handlers('whatever')
    (handler,) = _tls_handlers('legacy-renegotiation')
    import ssl
    assert handler._context.options & getattr(ssl, 'OP_LEGACY_SERVER_CONNECT', 0x4), \
        '这个姿态就是站点要的 legacy renegotiation'
    assert not handler._context.options & getattr(ssl, 'OP_NO_RENEGOTIATION', 0)


def test_fetch_form_refuses_two_bodies_at_once():
    provider = PublicWebSearchProvider()
    with pytest.raises(ValueError):
        provider.fetch_form('https://mall.anhui.zcygov.cn/q', fields={'a': 'b'}, body=b'x')


def test_the_stored_cache_table_is_a_plain_key_value_row(tmp_path):
    """缓存表沿用既有 ``(id, task_id, payload)`` 形态，读取只按 id——所以跨任务。"""
    path = tmp_path / 'app.sqlite3'
    SQLiteRepository(path).close()
    connection = sqlite3.connect(path)
    columns = {row[1]: row[2] for row in connection.execute(
        'PRAGMA table_info(province_award_cache)')}
    assert columns == {'id': 'TEXT', 'task_id': 'TEXT', 'payload': 'TEXT'}
    connection.close()


# ---------------------------------------------------------------------------
# 政采云内核家族（浙江 / 广西）：请求形状、假命中、详情回读、标项块整形
#
# 构造响应的形状**照真实响应抄的**（2026-09-26 实站取证，原始响应在
# ``output/province-direct/raw/``，型号命中数与靶子值见 ``设计规范`` §2.1）：
# 响应是 ``{"success":true,"result":{"data":{"total":…,"data":[…]}}}``；
# 列表行的 ``content`` 只是**命中片段**（116–121 字符，带 ``<em>`` 高亮）；
# 正文在详情响应 ``result.data.content`` 里，且**一个 ``<table>`` 都没有**。
# ---------------------------------------------------------------------------

#: 浙江的列表命中片段（原文照抄，把项目编号等长串截短，价格与型号一个字没改）。
ZHEJIANG_EXCERPT = (
    ';&nbsp;&nbsp;数量：1.00&nbsp;&nbsp;&nbsp;&nbsp;单价（元）：12000.00'
    '&nbsp;&nbsp;&nbsp;规格型号（或服务要求）：品牌:上海申安规格型号:<em>LDZX-50L</em>'
)
ZHEJIANG_ARTICLE = 'F9vXJ+ZAylEIRdNeetiChQ=='

#: 浙江的详情正文（标项块，原文结构照抄；这里只留 3 个标项）。
ZHEJIANG_DETAIL_CONTENT = (
    '<p>一、合同编号 ： 11N73016478720225202</p>'
    '<p>二、合同名称 ： 浙大宁波理工学院2022年生化学院实验教学设备采购项目的合同</p>'
    '<p>六、合同主体信息</p>'
    '<p>1.主要标的信息：</p>'
    '<p>标项一</p><p>主要标的名称： 台式离心机</p><p>数量： 2.00</p>'
    '<p>单价（元）： 30000.00</p>'
    '<p>规格型号（或服务要求）： 品牌:艾本德</p><p>规格型号:Centrifuge5425</p>'
    '<p>标项二</p><p>主要标的名称： 立式压力蒸汽灭菌器</p><p>数量： 1.00</p>'
    '<p>单价（元）： 12000.00</p>'
    '<p>规格型号（或服务要求）： 品牌:上海申安</p><p>规格型号:LDZX-50L</p>'
    '<p>标项三</p><p>主要标的名称： 气动升降椅</p><p>数量： 80.00</p>'
    '<p>单价（元）： 800.00</p>'
    '<p>规格型号（或服务要求）： 品牌:国产圆形</p><p>规格型号:/</p>'
    '<p>2.合同金额（元）： 45600.00</p>'
    '<p>七、合同签订日期 ： 2022年08月05日</p>'
)

#: 广西的列表命中片段：**型号被服务端高亮截短**（``LDZX`` 而不是 ``LDZX-50L``），
#: 但 ``total=1``、片段里就有 ``单价（元）：13000.00``。这就是 ``设计规范`` §2.1
#: 那条"只看 total 会把广西误记成能搜到"的假命中形状（原文照抄）。
GUANGXI_TRUNCATED_EXCERPT = (
    'nbsp;&nbsp;&nbsp;数量：2.00&nbsp;&nbsp;&nbsp;&nbsp;单价（元）：13000.00'
    '&nbsp;&nbsp;&nbsp;规格型号（或服务要求）：品牌:申安规格型号:<em>LDZX</em>'
)


def _portal_response(rows, *, total=None):
    return {'success': True, 'error': None,
            'result': {'data': {'total': len(rows) if total is None else total,
                                'data': rows, 'empty': not rows}}}


def _portal_row(article, title, content, *, announcement_type=3010,
                publish=1660535358000, district_name='宁波市本级', project='ZJZC-223193-C'):
    return {'articleId': article, 'annId': 12681691, 'siteId': 110,
            'firstCode': 'ZcyAnnouncement', 'secondCode': '110-606633',
            'pathName': '采购公告 | 政府采购公告 | 采购合同公告 | 采购合同公告',
            'title': title, 'content': content, 'publishDate': publish,
            'districtCode': '330299', 'districtName': district_name,
            'projectCode': project, 'projectName': title,
            'announcementType': announcement_type, 'encryptId': None}


def _portal_detail(content):
    return {'success': True, 'error': None,
            'result': {'data': {'title': '详情', 'articleId': ZHEJIANG_ARTICLE,
                                'content': content}}}


def zhejiang_list():
    return _portal_response([_portal_row(
        ZHEJIANG_ARTICLE,
        '浙大宁波理工学院2022年生化学院实验教学设备采购项目的合同公告',
        ZHEJIANG_EXCERPT)])


def guangxi_list():
    return _portal_response([_portal_row(
        'suSOnrCyImKQJBVyxXAY+w==',
        '桂林医科大学2025年基础医学、护理学、医学检验学教学设备采购项目合同公告',
        GUANGXI_TRUNCATED_EXCERPT, publish=1756821568000,
        district_name='广西壮族自治区本级', project='GXZC2025-G1-002022-JDZB')])


class UrlRecorder:
    """按 URL 回放响应（详情是 GET、列表是 POST，同一个出口）。"""

    def __init__(self, answers):
        self.answers = dict(answers)
        self.calls = []

    def __call__(self, url, *, method, body, headers, tls_profile, timeout):
        self.calls.append({'url': url, 'method': method, 'body': body,
                           'headers': dict(headers or {}), 'tls_profile': tls_profile})
        for prefix, payload in self.answers.items():
            if url.startswith(prefix):
                if isinstance(payload, Exception):
                    raise payload
                import json
                return json.dumps(payload, ensure_ascii=False).encode('utf-8')
        raise AssertionError(f'没有为这次请求准备响应: {url}')


def test_the_portal_body_carries_no_district_and_no_borrowed_type_filter():
    """政采云内核的 body 只有实测可用的三个字段。

    **不抄卖场家族的 ``announcementTypes``**：那是另一个接口的参数，没有证据说这个站
    认它（照抄一个不认的参数，最坏情况是悄悄把结果筛空）；**不写 ``district``**：
    这个接口没有区划参数，检索是站内全量；**不写 ``isTitleSearch``**：``设计规范``
    记着传布尔会 422，而省略它时默认就是搜正文。
    """
    platform = pas._BY_KEY['zhejiang']
    body = platform.query_body(pas.PORTAL_SCOPE, MODEL)
    assert body == {'keyword': MODEL, 'pageNo': 1, 'pageSize': 15}
    assert 'district' not in body and 'announcementTypes' not in body
    assert 'projectName' not in body, 'projectName 只搜项目名，不能拿它当型号检索'
    assert platform.districts == (pas.PORTAL_SCOPE,), '这一格不是区划码，是"全站"作用域'
    assert platform.detail_limit == pas.PORTAL_DETAIL_LIMIT > 0
    assert _portal_row('a', 't', 'c')['articleId'] == 'a'
    # 详情地址必须整体百分号编码：articleId 里的 ``+`` 在 query 里会被读成空格。
    url = platform.detail_url({'articleId': ZHEJIANG_ARTICLE}, pas.PORTAL_SCOPE)
    assert url == ('https://zfcg.czt.zj.gov.cn/portal/detail?articleId='
                   'F9vXJ%2BZAylEIRdNeetiChQ%3D%3D')


def test_a_truncated_model_in_the_returned_row_is_not_a_hit():
    """⭐ ``设计规范`` §2.1 那条假命中形状，钉成一条判据。

    广西这条：``total=1``、片段里连 ``单价（元）：13000.00`` 都有，但**返回行里的型号
    被服务端高亮截短成 ``LDZX``**（不是 ``LDZX-50L``）。判定必须要求型号**逐字**出现在
    返回行里，所以这条是 **未命中** —— "只看 ``total>0``" 会把广西误记成"能按型号搜到"。

    同时钉住：被拒的行**不会**去回读详情页（这一格一个请求都不多发），拒了几条留在
    ``queries.rejected`` 里，看得见。
    """
    transport = UrlRecorder({'https://zfcg.gxzf.gov.cn/portal/all': guangxi_list()})
    found = pas.lookup(repo=None, target={'model': 'LDZX-50L'}, transport=transport,
                       task_id='t1', platforms=(pas._BY_KEY['guangxi'],))
    assert found['announcements'] == [], '返回行里没有完整型号 → 不是命中'
    assert len(transport.calls) == 1, '被拒的行不该去回读详情页'
    query = found['queries'][0]
    assert query['total'] == 1 and query['rows'] == 1 and query['rejected'] == 1
    assert query['hits'] == 0
    assert query['detail_fetched'] == 0 and query['detail_failed'] == 0


def test_the_detail_page_body_is_read_back_through_the_existing_record_path(repo):
    """列表片段 → 详情正文 → 既有价格判据，一路拿到页面自己印的单价。

    这一步就是"浙江能不能出价"的全部：列表片段 116 字符、``price_table_rows`` 读出
    **0 行**（``test_the_excerpt_alone_yields_no_price_row`` 钉住这一点），
    详情正文里的标项块经 ``shape_lot_blocks`` 整形后才读出 12000.00。
    """
    transport = UrlRecorder({
        'https://zfcg.czt.zj.gov.cn/portal/all': zhejiang_list(),
        'https://zfcg.czt.zj.gov.cn/portal/detail':
            _portal_detail(ZHEJIANG_DETAIL_CONTENT),
    })
    found = pas.lookup(repo=repo, target={'model': 'LDZX-50L'}, transport=transport,
                       task_id='t1', platforms=(pas._BY_KEY['zhejiang'],))
    assert [call['method'] for call in transport.calls] == ['POST', 'GET']
    assert 'articleId=F9vXJ%2BZAylEIRdNeetiChQ%3D%3D' in transport.calls[1]['url']
    announcement = found['announcements'][0]
    assert announcement['detail_fetched'] is True
    assert announcement['platform'] == 'zhejiang'
    assert announcement['district_name'] == '宁波市本级'
    assert '主要标的名称' in announcement['content'], '缓存的是详情正文，不是片段'

    body = pas.body_text(announcement)
    rows = price_table_rows(body, 'LDZX-50L')
    assert len(rows) == 1
    assert rows[0]['amount'] == 12000.0 and rows[0]['quantity'] == 1.0
    assert rows[0]['brand'] == '上海申安'
    assert validate_price({**rows[0], 'kind': 'contract'},
                          {'url': announcement['url'], 'body_text': body,
                           'body_status': 'fetched', 'source_type': 'tender_document'},
                          {'brand': '', 'model': 'LDZX-50L'}), '成交/合同证据齐了就该采信'


def test_the_excerpt_alone_yields_no_price_row():
    """反面：只有命中片段时，既有表格判据一行都读不出来（所以要回读详情页）。"""
    body = pas.clean_content(ZHEJIANG_EXCERPT, '')
    assert 'LDZX-50L' in body and '单价（元）：12000.00' in body
    assert price_table_rows(body, 'LDZX-50L') == []


def test_a_failed_detail_read_is_a_failure_and_is_not_cached(repo):
    """详情读不到 = **没查成**，不是"这个型号没有"：记一条失败，而且**不写缓存**。

    不写缓存这一条是刻意的：把"没读成"缓存 7 天，等于把一次网络抖动固化成一周的
    "平台上没有这个型号"。
    """
    transport = UrlRecorder({
        'https://zfcg.czt.zj.gov.cn/portal/all': zhejiang_list(),
        'https://zfcg.czt.zj.gov.cn/portal/detail': RuntimeError('boom'),
    })
    found = pas.lookup(repo=repo, target={'model': 'LDZX-50L'}, transport=transport,
                       task_id='t1', platforms=(pas._BY_KEY['zhejiang'],))
    assert found['announcements'] == []
    assert [failure['code'] for failure in found['failures']] == ['detail_failed']
    assert found['queries'][0]['detail_failed'] == 1
    assert found['queries'][0]['hits'] == 0

    # 没写缓存：第二次查询仍旧打网络（列表 + 详情各一次）。
    again = UrlRecorder({
        'https://zfcg.czt.zj.gov.cn/portal/all': zhejiang_list(),
        'https://zfcg.czt.zj.gov.cn/portal/detail': _portal_detail(ZHEJIANG_DETAIL_CONTENT),
    })
    second = pas.lookup(repo=repo, target={'model': 'LDZX-50L'}, transport=again,
                        task_id='t1', platforms=(pas._BY_KEY['zhejiang'],))
    assert [call['method'] for call in again.calls] == ['POST', 'GET']
    assert second['queries'][0]['source'] == 'network'
    assert second['announcements'][0]['detail_fetched'] is True


def test_the_detail_read_can_be_bounded_by_the_request_budget():
    """额度在详情回读途中用完：安静收手、记一条 ``quota``、**不写缓存**。"""
    from backend.services.education_platform_sources import QuotaReached

    calls = {'n': 0}

    def budget():
        calls['n'] += 1
        if calls['n'] > 1:          # 列表查询放行，第一条详情就撞额度
            raise QuotaReached()

    transport = UrlRecorder({
        'https://zfcg.czt.zj.gov.cn/portal/all': zhejiang_list(),
        'https://zfcg.czt.zj.gov.cn/portal/detail':
            _portal_detail(ZHEJIANG_DETAIL_CONTENT),
    })
    found = pas.lookup(repo=None, target={'model': 'LDZX-50L'}, transport=transport,
                       task_id='t1', platforms=(pas._BY_KEY['zhejiang'],),
                       on_request=budget)
    assert found['queries'][-1] == {'platform': 'zhejiang', 'district': pas.PORTAL_SCOPE,
                                    'source': 'quota', 'hits': 0}
    assert len(transport.calls) == 1, '额度用完就不再发请求'


def test_the_lot_block_shaper_reads_the_pages_own_price_and_never_does_arithmetic():
    """标项块 → 表：每个格子都是页面原文，正文原件只在末尾追加。"""
    body = pas.clean_content(ZHEJIANG_DETAIL_CONTENT, '')
    shaped = pas.shape_lot_blocks(body)
    assert shaped.startswith(body.rstrip('\n')), '原件一字不动，整形只追加'
    lines = shaped.splitlines()
    header_at = lines.index('主要标的名称 | 品牌 | 规格型号 | 数量 | 单价（元）')
    assert lines[header_at + 2] == '立式压力蒸汽灭菌器 | 上海申安 | LDZX-50L | 1.00 | 12000.00'
    rows = price_table_rows(shaped, 'LDZX-50L')
    assert [row['amount'] for row in rows] == [12000.0]
    assert price_table_rows(shaped, 'Centrifuge5425')[0]['amount'] == 30000.0


def test_the_shaper_also_reads_a_block_whose_brand_and_model_share_one_line():
    """站点把品牌与型号印在同一行时，型号不许被读丢。

    广西的命中片段就是 ``规格型号（或服务要求）：品牌:申安规格型号:LDZX`` 这种挤在一行
    的写法。一行只认一个标签的实现会把型号丢掉、再把整块判成"不完整"，最后整份不整形
    ——那是我们自己造成的读不出来，不是页面没写。
    """
    body = pas.clean_content(
        '<p>标项一</p><p>主要标的名称： 立式压力蒸汽灭菌器</p><p>数量： 1.00</p>'
        '<p>单价（元）： 12000.00</p>'
        '<p>规格型号（或服务要求）：品牌:上海申安规格型号:LDZX-50L</p>'
        '<p>标项二</p><p>主要标的名称： 台式离心机</p><p>数量： 2.00</p>'
        '<p>单价（元）： 30000.00</p>'
        '<p>规格型号（或服务要求）：品牌:艾本德规格型号:Centrifuge5425</p>', '')
    shaped = pas.shape_lot_blocks(body)
    assert shaped != body, '这种写法也必须读得出来'
    rows = price_table_rows(shaped, 'LDZX-50L')
    assert [(row['brand'], row['amount']) for row in rows] == [('上海申安', 12000.0)]


def test_a_lot_block_without_a_brand_line_is_read_not_dropped():
    """实测形状①：型号写在 ``规格型号（或服务要求）：`` 的值里，**整块不印品牌**。

    浙江 ``FA2004`` 有两条真实正文就是这样（``规格型号（或服务要求）： FA2004``，既没有
    ``品牌:``，也没有另起一行的 ``规格型号:``），它们的单价是 6200.00 与 4500.00 元。
    按"每块必须齐备品牌和型号"的口径，这两个真实标项会**整条丢掉**——那是我们自己造成的
    读不出来，不是页面没写。现在品牌格留空、型号照读，能不能采信仍旧交给 ``validate_price``。
    """
    body = pas.clean_content(
        '<p>一、合同编号： 11N1</p>'
        '<p>标项一</p><p>主要标的名称： 电子分析天平</p><p>数量： 1.00</p>'
        '<p>单价（元）： 6200.00</p><p>规格型号（或服务要求）： FA2004</p>'
        '<p>标项二</p><p>主要标的名称： 天平（万分之一）</p><p>数量： 1.00</p>'
        '<p>单价（元）： 4500.00</p><p>规格型号（或服务要求）： FA2004</p>', '')
    shaped = pas.shape_lot_blocks(body)
    assert shaped != body
    rows = price_table_rows(shaped, 'FA2004')
    assert [(row['amount'], row['brand']) for row in rows] == [(6200.0, ''), (4500.0, '')]


def test_the_guangxi_shape_has_no_section_marker_and_a_last_block_without_a_model():
    """实测形状②（广西）：**不印 ``标项N``**，而且最后一个标项连型号行都没有。

    真实正文的最后一块是 ``主要标的名称： 其他设备 / 数量： 1.00 /
    单价（元）： 174918.00 / 规格型号（或服务要求）： 详见采购文件``。按"整份只要有一块
    不完整就不整形"的口径，同一份公告里 FA2004 的 1750.00 元也读不出来（实测踩到过）。
    """
    body = pas.clean_content(
        '<p>主要标的名称： 电子天平</p><p>数量： 20.00</p><p>单价（元）： 1750.00</p>'
        '<p>规格型号（或服务要求）： 品牌:佑科</p><p>规格型号:FA2004</p>'
        '<p>主要标的名称： 其他设备</p><p>数量： 1.00</p><p>单价（元）： 174918.00</p>'
        '<p>规格型号（或服务要求）： 详见采购文件</p>'
        '<p>2.合同金额（元）： 258418.00</p>', '')
    shaped = pas.shape_lot_blocks(body)
    rows = price_table_rows(shaped, 'FA2004')
    assert [(row['amount'], row['quantity'], row['brand']) for row in rows] == \
        [(1750.0, 20.0, '佑科')]


def test_a_broken_block_is_dropped_alone_instead_of_voiding_the_whole_body():
    """一块读不完整 → **只丢那一条**；分块被读错 → 整份不铺。

    为什么放开"一块不完整就整份作废"：每个格子的值都只从**它自己那个块**里读，块与块
    之间不共享任何值，所以"块不完整"最坏只会**丢掉那一条**，不会把 A 标项的价配到 B 标项
    的型号上。这条边界是实测逼出来的（广西最后一块没有型号、浙江 FA2004 两块没有品牌），
    不放开就等于把这些真实公告的单价一起丢掉。仍然挡着的是"分块读错了"：印了 ``标项N``
    的页面序号重复 → 一条都不铺。
    """
    body = pas.clean_content(ZHEJIANG_DETAIL_CONTENT, '')
    # ① 少一个"数量"：只有那一块丢掉，其它块照铺。
    missing = body.replace('数量： 2.00\n', '')
    shaped = pas.shape_lot_blocks(missing)
    assert shaped != missing
    assert not price_table_rows(shaped, 'Centrifuge5425'), '缺数量的那一块必须丢掉'
    assert price_table_rows(shaped, 'LDZX-50L'), '别的块照读'
    # ② 序号重复：说明分块读错了 → 整份不铺，一条都不许猜。
    duplicate = body.replace('标项二', '标项一')
    assert pas.shape_lot_blocks(duplicate) == duplicate
    # ③ 块中间插了别的话：插话之后的标签不属于这一块 → 那一块丢掉，别的块照读。
    interrupted = body.replace('单价（元）： 12000.00',
                               '单价（元）： 12000.00\n备注：以合同为准')
    shaped = pas.shape_lot_blocks(interrupted)
    assert not price_table_rows(shaped, 'LDZX-50L'), '型号行落在插话之后，这一块读不完整'
    assert price_table_rows(shaped, 'Centrifuge5425'), '其它块不受影响'
    # ④ 没有任何标项块（卖场家族的正文就是这样）→ 原样返回。
    anhui_body = pas.clean_content(ANHUI_CONTENT, '')
    assert pas.shape_lot_blocks(anhui_body) == anhui_body


def test_the_mall_family_is_untouched_by_the_new_shaper_and_the_detail_step():
    """既有两省：不回读详情、整形是恒等、正文路径与缓存键都与原来一样。"""
    for key in ('anhui', 'jiangxi'):
        platform = pas._BY_KEY[key]
        assert platform.detail_limit == 0, '卖场家族的正文随列表响应回来，不需要详情'
        assert platform.trailing_columns == ('报价明细',)
        assert platform.shape_text('任意正文\n一字不动') == '任意正文\n一字不动'
    assert pas._BY_KEY['zhejiang'].trailing_columns == (), \
        '空元组保证 shape_award_table 的循环认不出这两站，改不到安徽 / 江西的结果'
    transport = Recorder([anhui_payload()])
    found = pas.lookup(repo=None, target={'model': MODEL}, transport=transport,
                       task_id='t1', platforms=(pas._BY_KEY['anhui'],))
    assert [call['method'] for call in transport.calls] == ['POST'], '不发详情请求'
    assert 'detail_fetched' not in found['announcements'][0]


def test_the_detail_body_is_declared_as_a_different_provenance(service):
    """来源必须说清正文是哪一份：详情正文与列表正文不是同一件事。"""
    service, run, _provider = service
    detail = service.province_award_record(run, {
        'platform': 'zhejiang', 'platform_label': '浙江',
        'url': 'https://zfcg.czt.zj.gov.cn/portal/detail?articleId=x',
        'title': '某项目合同公告', 'content': ZHEJIANG_DETAIL_CONTENT,
        'project_code': 'P1', 'district': pas.PORTAL_SCOPE, 'detail_fetched': True})
    listed = service.province_award_record(run, {
        'platform': 'anhui', 'platform_label': '安徽',
        'url': 'https://mall.anhui.zcygov.cn/luban/announcement/detail?encryptId=x&district=349900',
        'title': '某项目成交公告', 'content': ANHUI_CONTENT,
        'project_code': 'P1', 'district': '349900'})
    assert detail['text_provenance']['method'] == 'province_platform_detail_content'
    assert listed['text_provenance']['method'] == 'province_platform_list_content'


def test_the_channel_identity_covers_the_portal_family():
    """按通道分组统计必须认出新增两省，否则它们的来源会被记进 ``other``。"""
    from backend.services.alternative_price_lookup import PROVINCE_AWARD, source_channel
    assert source_channel({'platform': 'zhejiang',
                           'url': 'https://zfcg.czt.zj.gov.cn/portal/detail?articleId=x'}) == PROVINCE_AWARD
    assert source_channel({'platform': 'guangxi',
                           'url': 'https://zfcg.gxzf.gov.cn/portal/detail?articleId=x'}) == PROVINCE_AWARD


def test_the_product_queries_the_union_of_both_families(service):
    """产品路径要的是**并集**：只读 ``PROVINCES`` 会让新增两省永远查不到，还不报错。"""
    service, run, _provider = service
    queried = [platform.key for platform in service.province_award_platforms()]
    assert queried == ['anhui', 'jiangxi', 'zhejiang', 'guangxi']
    assert 'province_award' not in pr.RESEARCH_LIMITS
    assert run['limits']['province_award'] == pr.PROVINCE_AWARD_LIMIT
