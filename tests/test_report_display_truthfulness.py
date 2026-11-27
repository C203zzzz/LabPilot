"""What the report says about a number or a gap must match what the run knows.

Three measured presentation defects, all of the same kind -- the sentence is
about a different thing than the data:

* a notice the *search* pass could not verify was still printed as
  「采购公告正文没读到」 in the same report that listed a price read from it in
  the *research* pass -- and the footnote an earlier version appended to it
  ("这是检索阶段的判定；研究阶段已取得正文并据其核价") named two internal stages
  and claimed a pricing that a zero-price run never did;
* ``f"{1239000.0:g}"`` printed ``1.239e+06`` into the report body, so a
  1,239,000 元 award was unreadable;
* a second-hand row read off an aggregator repost said 「公告未提供」 about its
  configuration column, blaming the announcement for a column the page it was
  read from does not have.
"""
from backend.services.procurement_research import reconcile_tender_body_gaps
from backend.services.procurement_rules import format_money, price_assessment
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture

BODY = '中标结果公告 2026年03月25日 名称：血型血清学离心机 规格型号：XKA-2200 单价(元)：17480\n'


def test_grouped_digits_never_scientific_notation():
    assert format_money(1239000.0) == '1,239,000'
    assert 'e+' not in format_money(1239000.0)
    assert format_money(17480.0) == '17,480'
    assert format_money(1234.5) == '1,234.50'
    assert format_money('47000') == '47,000'


def test_the_price_summary_prints_a_readable_amount():
    prices = [{'kind': 'award', 'level': 'historical_configuration', 'unit_price': 1239000.0,
               'date': '2025-12-09', 'gaps': ['configuration']}]

    summary = price_assessment(prices, {'supplier_price': 150000, 'currency': 'CNY'})

    assert '1,239,000' in summary
    assert 'e+' not in summary


def test_a_notice_read_by_the_research_pass_is_no_longer_called_unverified():
    url = 'https://ggzy.example/notice/1'
    gaps = [f'tender_body_unverified: {url}', 'tender_body_unverified: https://ggzy.example/never-read']
    sources = [{'url': url, 'body_status': 'fetched', 'body_text': BODY}]

    reconciled = reconcile_tender_body_gaps(gaps, sources)

    # 正文已经在手的那条对读者不成立：报告不再印它（存下来的 run 一字未动，
    # 检索阶段的判定留在 ``run['search_attempts']``）。
    assert reconciled == [gaps[1]]


def test_the_internal_footnote_an_older_version_appended_is_never_printed():
    """旧版给"已经读到正文"的缺口拼过一句内部说明，库里已存下来了（164 条）。

    那句话点了两个内部阶段，还写着"据其核价"——0 条成交价的那一轮里也照样印。
    它连同它标记的那条缺口一起撤掉，一个字都不再进报告。
    """
    url = 'https://ggzy.example/notice/legacy'
    legacy = (f'tender_body_unverified: {url}'
              '（这是检索阶段的判定；研究阶段已取得正文并据其核价，见来源清单）')

    reconciled = reconcile_tender_body_gaps([legacy], [])

    assert reconciled == []
    assert not any('这是检索阶段的判定' in text for text in reconciled)


def test_reconciliation_leaves_every_other_gap_untouched():
    gaps = ['本轮未取得可核实的同型号成交单价。', 'empty_results']
    assert reconcile_tender_body_gaps(gaps, [{'url': 'https://a.example', 'body_status': 'fetched',
                                              'body_text': 'x'}]) == gaps


def test_reconciliation_is_idempotent():
    """``report()`` may run more than once for the same run."""
    url = 'https://ggzy.example/notice/2'
    gaps = [f'tender_body_unverified: {url}']
    sources = [{'url': url, 'body_status': 'fetched', 'body_text': BODY}]

    once = reconcile_tender_body_gaps(gaps, sources)
    twice = reconcile_tender_body_gaps(once, sources)

    assert twice == once == []


def test_a_url_that_differs_only_by_normalisation_still_matches():
    gaps = ['tender_body_unverified: https://ggzy.example/notice/3']
    sources = [{'url': 'https://ggzy.example/notice/3/', 'body_status': 'fetched',
                'body_text': BODY}]

    assert reconcile_tender_body_gaps(gaps, sources) == []


def test_the_report_renders_a_read_notice_as_read(research):  # noqa: F811
    service, run, _, _ = research
    url = 'https://ggzy.example/notice/4'
    run.update(status='partial', round=77, prices=[], republished_prices=[],
               lanes={'price': 'completed', 'alternatives': 'completed'},
               sources=[{'url': url, 'body_status': 'fetched', 'body_text': BODY,
                         'source_type': 'tender_document'}],
               gaps=[f'tender_body_unverified: {url}',
                     'tender_body_unverified: https://ggzy.example/never-read'])
    service.save(run)

    report = service.report(run['task_id'], run['id'])
    texts = [item['text'] for section in report['sections'] if section['key'] == 'gaps'
             for item in section['items']]

    assert not any(url in text for text in texts), '正文已经读到的那条不许再印成待核实'
    unread_line = next(text for text in texts if 'never-read' in text)
    assert '这是检索阶段的判定' not in unread_line
    # 页面读的同一份 run 也必须是同一批缺口，不能页面 2 条、报告 1 条。
    displayed = service.display(run['task_id'], run['id'])
    assert [gap for gap in displayed['gaps'] if 'never-read' in gap] == [unread_line]


def test_the_second_hand_section_does_not_blame_the_announcement(research):  # noqa: F811
    """The aggregator page has no configuration column at all.

    Real header of the measured source (https://www.120shop.cn/i890850380):
    设备名称 | 设备品牌 | 设备型号 | 采购单位 | 成交单位 | 成交时间 | 成交价格 | 操作
    """
    service, run, _, _ = research
    run.update(status='partial', round=78, prices=[], gaps=[],
               lanes={'price': 'completed', 'alternatives': 'completed'},
               republished_prices=[{
                   'model': 'XKA-2200', 'brand': '湘仪', 'amount': 4.70, 'amount_unit': '万元',
                   'quantity': None, 'amount_scope': 'unit', 'currency': 'CNY',
                   'date': '2024-03-28', 'configuration': 'unknown', 'purchaser': '北京天坛医院',
                   'supplier': '上药康德乐（北京）医药有限公司', 'evidence_grade': 'secondary',
                   'unit_price': 47000.0, 'url': 'https://www.120shop.cn/i890850380',
                   'source_type': 'unknown', 'excerpt': '免疫血液学用离心机 | 湘仪 | XKA-2200',
               }])
    service.save(run)

    report = service.report(run['task_id'], run['id'])
    section = next(item for item in report['sections'] if item['key'] == 'republished')
    text = section['items'][0]['text']

    assert '来源页未提供配置' in text
    assert '公告未提供' not in text
    assert '47,000' in text
