"""展示口径的端到端契约：判据只有一个——这一轮有没有官方成交记录。

用户的定位（原文）：「这个产品是面向历史招标公告的比价工具」。由此产生三个必须
同时成立的行为，三处**不许各判一次**：

* ``procurement_research.has_award_record``：唯一的判据，按 ``kind`` 过滤
  （``award`` / ``contract``），不看来源、不看金额。前端 ``transactionPrices`` 是
  同一件事。
* ``mark_source_authority`` 给来源打 ``authority``，前端据此决定来源清单列不列它。
* ``republished_section`` / ``refresh_republished_section``：报告正文的
  「二手成交记录（聚合站转载）」一节，只在没有官方成交记录时存在。

这里的 run 全部用 ``task-013dcdcff7e6`` 与 ``task-0a7f1ff94935`` 的真实形状：
前者有一条 ccgp.gov.cn 的成交记录，后者一条都没有（同一批 7 条商业线索）。
"""
import json

import pytest

from backend.services.procurement_research import (
    ProcurementResearch,
    award_records,
    has_award_record,
    mark_source_authority,
    redact_unlisted_urls,
    republished_section,
    run_brands,
    sanitize_report,
)


def run_with(prices, sources=None, **extra):
    return {
        'task_id': 'task-test', 'id': 'research-test', 'kind': 'research',
        'baseline': {'brand': '湘仪', 'model': 'XKA-2200'},
        'alternatives': [], 'discovered_candidates': [],
        'prices': prices,
        'republished_prices': [
            {'model': 'XKA-2200', 'unit_price': 17500, 'currency': 'CNY', 'date': '2024-03-12',
             'purchaser': '某县人民医院', 'supplier': '某医疗科技公司', 'url': 'https://www.120shop.cn/i890850380',
             'excerpt': '血液专用离心机 | 湘仪 | XKA - 2200 | 1.75万元'},
        ],
        'sources': sources or [],
        **extra,
    }


OFFICIAL = [{'kind': 'award', 'unit_price': 17480, 'url': 'http://www.ccgp.gov.cn/x.htm'}]
LEADS = [{'url': 'https://www.120shop.cn/i890850380', 'body_status': 'fetched', 'body_text': 'XKA-2200'},
         {'url': 'https://www.yiqi.com/product/1.html', 'body_status': 'unavailable'},
         {'url': 'http://www.ccgp.gov.cn/x.htm', 'body_status': 'fetched',
          'body_text': '某某市第一人民医院医疗设备采购项目中标（成交）结果公告\n项目编号：X\n采购人：某院\n'
                       '成交供应商：某公司\n中标金额：17,480.00 元\n公告日期：2026年03月25日\n'
                       '设备名称 | 设备型号 | 单价（元）\n离心机 | XKA-2200 | 17,480\n'}]


def test_the_single_condition_is_the_kind_filter_and_nothing_else():
    assert has_award_record(run_with(OFFICIAL)) is True
    assert has_award_record(run_with([])) is False
    # 挂牌价、报价、投标金额混在 prices 里都不算官方成交记录。
    assert has_award_record(run_with([{'kind': 'listing', 'unit_price': 4500}])) is False
    assert has_award_record(run_with([{'kind': 'bid_amount', 'unit_price': 6400}])) is False
    assert has_award_record(run_with([{'kind': 'contract', 'unit_price': 7100}])) is True
    assert [q['kind'] for q in award_records(run_with(OFFICIAL + [{'kind': 'listing'}]))] == ['award']
    # 畸形 run 不许抛。
    assert has_award_record({}) is False
    assert has_award_record({'prices': [None, 'x', {'kind': 'award'}]}) is True


def test_brands_collect_from_baseline_alternatives_and_discovered_candidates():
    run = run_with(OFFICIAL, alternatives=[{'brand': ' 湖南凯达 '}], discovered_candidates=[{'brand': '湘仪'}, {}])
    assert run_brands(run) == ['湘仪', '湖南凯达']


def test_source_authority_is_tagged_per_host_and_a_host_with_any_authority_stays():
    """同一域名的正文与附件不能一半出现一半消失，所以按主机取最高那一档。

    标签就是三档本身（官方渠道 / 厂家 / 不列），不再折叠成 listed/unlisted：
    前端还要用它把清单分成「采购公告与合同」和「官方渠道与发布机构」。
    """
    run = run_with(OFFICIAL, sources=[
        {'url': 'http://www.ccgp.gov.cn/award.htm', 'body_status': 'fetched', 'body_text': '中标结果公告\n项目编号：X\n采购人：某院\n成交供应商：某公司\n中标金额：1 元'},
        {'url': 'http://www.ccgp.gov.cn/attachment.pdf', 'body_status': 'failed'},
        {'url': 'https://www.120shop.cn/i1', 'body_status': 'fetched', 'body_text': 'XKA-2200 1.75万元'},
        {'url': 'https://www.yiqi.com/p/1', 'body_status': 'unavailable'},
        {'url': 'https://www.xiangyilxj.com/a', 'body_status': 'fetched', 'body_text': '版权所有 © 湖南湘仪实验室仪器开发有限公司'},
    ])
    mark_source_authority(run)
    tags = {source['url']: source['authority'] for source in run['sources']}
    # 官方域名下连"没抓到正文"的那个附件也留下：同一条公告的两半，都算官方渠道。
    assert tags['http://www.ccgp.gov.cn/award.htm'] == 'official_channel'
    assert tags['http://www.ccgp.gov.cn/attachment.pdf'] == 'official_channel'
    assert tags['https://www.120shop.cn/i1'] == 'unlisted'
    assert tags['https://www.yiqi.com/p/1'] == 'unlisted'
    assert tags['https://www.xiangyilxj.com/a'] == 'manufacturer'


def test_marking_is_idempotent_and_survives_a_run_without_sources():
    run = run_with(OFFICIAL, sources=[{'url': 'https://www.120shop.cn/i1', 'body_status': 'fetched', 'body_text': 'x'}])
    mark_source_authority(run)
    first = [source['authority'] for source in run['sources']]
    mark_source_authority(run)
    assert [source['authority'] for source in run['sources']] == first
    empty = run_with(OFFICIAL)
    assert mark_source_authority(empty) is empty, '没有来源时原样返回，不抛'
    mark_source_authority({})  # 连 run 都是空的也不抛


def test_unlisted_urls_are_redacted_everywhere_they_are_rendered():
    """聚合站地址会从很多地方印出来，只在前端拦来源清单是不够的。

    实测 ``task-013dcdcff7e6`` 的替代产品页：来源清单已经干净了，聚合站域名仍然
    从候选卡片的「查看型号来源」和逐项参数表的「打开原文」里印出来。所以脱敏放在
    展示出口，一次换掉。
    """
    run = run_with(OFFICIAL, sources=[
        {'url': 'http://www.ccgp.gov.cn/award.htm', 'body_status': 'fetched',
         'body_text': '中标结果公告\n项目编号：X\n采购人：某院\n成交供应商：某公司\n中标金额：1 元'},
        {'url': 'https://www.yiliaozhaobiao.com/news-1', 'body_status': 'fetched', 'body_text': 'XKA-2200'},
    ])
    run['alternatives'] = [{
        'brand': '某品牌', 'model': 'XKA-2200',
        'url': 'https://www.yiliaozhaobiao.com/news-1',
        'matrix': [{'required': {'name': '转速'}, 'raw_evidence': [
            {'url': 'https://www.yiliaozhaobiao.com/news-1', 'excerpt': '转速 12000'},
            {'url': 'http://www.ccgp.gov.cn/award.htm', 'excerpt': '转速 12000'},
        ]}],
    }]
    run['price_rejections'] = [{'reason_code': 'source_not_tender', 'url': 'https://www.yiliaozhaobiao.com/news-1'}]
    mark_source_authority(run)
    redact_unlisted_urls(run)
    # 来源地址保留，用户可从中文来源说明点击打开原文。
    assert run['sources'][1]['url'] == 'https://www.yiliaozhaobiao.com/news-1'
    assert 'original_url' not in run['sources'][1]
    assert run['alternatives'][0]['url'] == 'https://www.yiliaozhaobiao.com/news-1'
    assert run['alternatives'][0]['matrix'][0]['raw_evidence'][0]['url'] == 'https://www.yiliaozhaobiao.com/news-1'
    assert run['price_rejections'][0]['url'] == 'https://www.yiliaozhaobiao.com/news-1'
    # 权威来源与证据事实一个都不动。
    assert run['alternatives'][0]['matrix'][0]['raw_evidence'][1]['url'] == 'http://www.ccgp.gov.cn/award.htm'
    assert run['alternatives'][0]['matrix'][0]['raw_evidence'][0]['excerpt'] == '转速 12000'
    # 幂等，且没有来源时原样返回。
    redact_unlisted_urls(run)
    assert run['sources'][1]['url'] == 'https://www.yiliaozhaobiao.com/news-1'


def test_historical_masked_url_is_restored_for_display():
    run = {'sources': [{'url': '#', 'original_url': 'https://www.yiliaozhaobiao.com/news-1'}]}
    redact_unlisted_urls(run)
    assert run['sources'][0]['url'] == 'https://www.yiliaozhaobiao.com/news-1'


def test_a_stored_report_loses_the_transshipment_wording_when_an_official_record_exists():
    """报告是存下来的，措辞固化；读的时候要按当前口径清一遍。

    真实那一份（``task-013dcdcff7e6``）就是这样：``sections`` 里那一节已经被删掉，
    ``summary`` 里还留着「二手成交记录（聚合站转载）9 条」这条事实、总述里还写着
    「另有 9 条二手成交记录来自聚合站转载」、未完成事项里还列着那些地址。
    """
    run = run_with(OFFICIAL, sources=[
        {'url': 'http://www.ccgp.gov.cn/award.htm', 'body_status': 'fetched',
         'body_text': '中标结果公告\n项目编号：X\n采购人：某院\n成交供应商：某公司\n中标金额：1 元'},
        {'url': 'https://www.yiliaozhaobiao.com/news-1', 'body_status': 'fetched', 'body_text': 'XKA-2200'},
    ])
    report = {
        'summary': {
            'headline': '只有 1 条可采信的同型号成交记录，不足以支撑比价。',
            'facts': [
                {'label': '可采信的同型号成交记录', 'value': '1 条'},
                {'label': '二手成交记录（聚合站转载）', 'value': '9 条'},
            ],
            'blockers': [
                '附件正文读取失败，需恢复后核验：http://www.ccgp.gov.cn/x.pdf',
                '来源限制自动访问，保留URL及缺口：https://www.yiliaozhaobiao.com/news-1',
            ],
            'narrative': '查到可采信的同型号成交记录 1 条，另有 9 条二手成交记录来自聚合站转载，同档替代候选为 0 款。',
        },
        'sections': [
            {'key': 'prices', 'title': '成交项目核价', 'items': [{'label': '中标金额', 'text': '1 元'}]},
            {'key': 'republished', 'title': '二手成交记录（聚合站转载）',
             'items': [{'label': 'XKA-2200', 'text': '1.75 万元'}]},
            {'key': 'alternatives', 'title': '同档替代核查', 'items': [
                {'label': 'XKA-2200', 'text': '参数来源一致', 'evidence': [
                    {'url': 'https://www.yiliaozhaobiao.com/news-1'},
                    {'url': 'http://www.ccgp.gov.cn/award.htm'}]}]},
        ],
    }
    cleaned = sanitize_report(report, run)
    # 那条"聚合站"事实、那一整节、那条缺口，连同地址，全部消失。
    assert [fact['label'] for fact in cleaned['summary']['facts']] == ['可采信的同型号成交记录']
    assert 'republished' not in [section['key'] for section in cleaned['sections']]
    assert cleaned['summary']['blockers'] == ['附件正文读取失败，需恢复后核验：http://www.ccgp.gov.cn/x.pdf']
    assert '聚合站' not in cleaned['summary']['narrative']
    assert cleaned['summary']['narrative'] == '查到可采信的同型号成交记录 1 条，同档替代候选为 0 款。'
    # 报告层仍按“有官方成交记录时不展示聚合站证据”清洗；候选卡片来源链接走 run 展示路径。
    evidences = next(s for s in cleaned['sections'] if s['key'] == 'alternatives')['items'][0]['evidence']
    assert [entry['url'] for entry in evidences] == ['', 'http://www.ccgp.gov.cn/award.htm']
    assert cleaned['sections'][0]['items'][0]['text'] == '1 元'
    # 幂等，且原报告不被就地改坏（库里那份要留证据）。
    assert '聚合站' in json.dumps(report, ensure_ascii=False)
    assert '聚合站' not in json.dumps(sanitize_report(cleaned, run), ensure_ascii=False)


def test_a_report_address_that_never_entered_the_source_list_is_still_recognised():
    """报告里会有"检索阶段看到过、研究阶段没纳入来源清单"的地址。

    实测 ``task-013dcdcff7e6`` 的报告里就有一条 ``biaoxun.shuidi.cn``：它不在本轮
    ``sources`` 里，拿不到 ``authority`` 标记，只靠"域名在不在 hidden 里"清不掉。
    所以兜底认后端**已注册的聚合站表**。

    兜底刻意不写成"未知域名一律排除"：只凭 URL 判定会把厂家官网也判成不列出
    （``beckman.com``、``xiangyilxj.com`` 没有正文证据时正是如此），而那些厂家页面
    恰恰是参数依据，必须留下。
    """
    run = run_with(OFFICIAL, sources=[
        {'url': 'http://www.ccgp.gov.cn/award.htm', 'body_status': 'fetched',
         'body_text': '中标结果公告\n项目编号：X\n采购人：某院\n成交供应商：某公司\n中标金额：1 元'},
    ])
    report = {'summary': {'blockers': [
        '采购公告正文没读到：https://biaoxun.shuidi.cn/detail/3ed59f29b30bd989d77cabadad13d01e',
        '厂家参数依据：https://www.beckman.com/centrifuges/general-purpose/avanti-j-15r',
        '厂家参数依据：https://www.xiangyilxj.com/product/info_itemid_952.html',
        '附件正文读取失败，需恢复后核验：http://ztb.usx.edu.cn/system/download.jsp',
    ]}}
    cleaned = sanitize_report(report, run)
    assert cleaned['summary']['blockers'] == [
        '厂家参数依据：https://www.beckman.com/centrifuges/general-purpose/avanti-j-15r',
        '厂家参数依据：https://www.xiangyilxj.com/product/info_itemid_952.html',
        '附件正文读取失败，需恢复后核验：http://ztb.usx.edu.cn/system/download.jsp',
    ]


def test_a_stored_report_without_an_official_record_keeps_everything():
    """没有官方成交记录时，聚合记录正是要给用户看的东西，不许清。"""
    run = run_with([], sources=[
        {'url': 'https://www.yiliaozhaobiao.com/news-1', 'body_status': 'fetched', 'body_text': 'XKA-2200'},
    ])
    report = {
        'summary': {'facts': [{'label': '二手成交记录（聚合站转载）', 'value': '9 条'}]},
        'sections': [{'key': 'republished', 'title': '二手成交记录（聚合站转载）',
                      'items': [{'label': 'XKA-2200', 'text': '1.75 万元',
                                 'evidence': [{'url': 'https://www.yiliaozhaobiao.com/news-1'}]}]}],
    }
    cleaned = sanitize_report(report, run)
    assert cleaned['summary']['facts'] == [{'label': '二手成交记录（聚合站转载）', 'value': '9 条'}]
    assert cleaned['sections'][0]['items'][0]['evidence'][0]['url'] == 'https://www.yiliaozhaobiao.com/news-1'


def test_the_republished_section_exists_only_without_an_official_record():
    with_official = republished_section(run_with(OFFICIAL))
    without = republished_section(run_with([]))
    assert with_official['title'] == without['title'] == '二手成交记录（聚合站转载）'
    assert len(without['items']) == 1
    assert '不是一手公告' in without['summary']


def stored_report(*, has_republished):
    sections = [{'key': 'baseline'}, {'key': 'prices'}]
    if has_republished:
        sections.append({'key': 'republished', 'title': '二手成交记录（聚合站转载）', 'items': [{'label': '旧'}]})
    sections += [{'key': 'alternatives'}, {'key': 'gaps'}]
    return {'sections': sections}


@pytest.mark.parametrize('has_official,kept,expected', [
    (True, True, ['baseline', 'prices', 'alternatives', 'gaps']),          # 有官方记录 → 删掉旧的那一节
    (True, False, ['baseline', 'prices', 'alternatives', 'gaps']),         # 本来就没有 → 不动
    (False, True, ['baseline', 'prices', 'republished', 'alternatives', 'gaps']),  # 留着并按当前数据重刷
    (False, False, ['baseline', 'prices', 'republished', 'alternatives', 'gaps']),  # 补回来
])
def test_a_stored_report_is_repaired_to_the_same_rule(has_official, kept, expected):
    """报告是存下来的：库里在本次口径之前建的那一节必须被就地增删。

    四格里只有「有官方记录 + 库里还留着那一节」会删；「没有官方记录」的两格都要有
    这一节，其中库里已有的那一份还要按当前数据重刷（否则旧快照与新口径两种说法）。
    """
    service = object.__new__(ProcurementResearch)  # 只测这一个纯函数，不碰 repo/search
    report = stored_report(has_republished=kept)
    run = run_with(OFFICIAL if has_official else [])
    changed = service.refresh_republished_section(report, run)
    assert [section['key'] for section in report['sections']] == expected
    if not has_official:
        section = next(s for s in report['sections'] if s['key'] == 'republished')
        assert section == republished_section(run), '库里那一节必须与当前数据一致'
    # 幂等：第二次读时不该再改，也不该写回。
    assert service.refresh_republished_section(report, run) is False
    # 返回值就是"要不要写回库"：库里状态与当前口径不一致时才动。四格里只有
    # 「有官方记录 + 库里还留着那一节」是删除（也算改动）。
    assert changed is (kept if has_official else True)


def test_an_unrecognised_report_is_left_alone():
    """更早版式的报告（没有 ``key``）不动它，也不许抛。"""
    service = object.__new__(ProcurementResearch)
    report = stored_report(has_republished=False)
    report['sections'] = [{'title': '比较基准'}, {'title': '成交项目核价'}]
    service.refresh_republished_section(report, run_with(OFFICIAL))
    service.refresh_republished_section(report, run_with([]))
    assert report['sections'] == [{'title': '比较基准'}, {'title': '成交项目核价'}]
