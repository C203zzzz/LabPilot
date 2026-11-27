"""Republished award records must reach the user, and stay visibly second-hand.

Background -- the 2026-09 live run.  For ``湘仪 XKA-2200`` the price lane
finished with ``prices == []`` and the report said "还没有取得同型号的成交单价".
The records were in fact already fetched: ``120shop.cn`` (医价网) publishes a
plain table pairing the model with a buying unit, a date and an amount.

They were invisible for two independent reasons, and both are pinned here:

* ``prices()`` only looks at sources whose ``source_type`` is a formal
  publication, so an aggregator page was dropped before anything read it;
* ``price_table_rows`` needs a recognised header row and an exact column-count
  match, so a headerless record row was skipped even when the source qualified.

The fix is not to relax the first-hand gate -- that gate is right.  It is to read
these rows as what they are: a *republished* transaction, surfaced with its
purchaser, date, amount and a clickable link, and never merged into the
first-hand evidence slot.
"""
import pytest

from backend.repositories.sqlite import SQLiteRepository
from backend.services.procurement_research import ProcurementResearch
from backend.services.procurement_tables import republished_award_rows

TARGET = {'brand': '湘仪', 'model': 'XKA-2200'}

# Verbatim rows from https://www.120shop.cn/i890850380 as fetched by the live run
# (snapshot recorded on the run; body_status='fetched', source_type='unknown').
# Row 0 is deliberately kept in its published form: the page spaces the hyphen
# as 「XKA - 2200」, and an exact-string match on the model silently drops it.
REPUBLISHED_BODY = (
    '血型专用离心机 | (卢)湘仪/湘鑫 | XKA - 2200 | 木垒哈萨克自治县人民医院 | 新疆新丝路惠康医疗科技有限公司 | 2026-05-19 | 1.75万元 | 查看\n'
    '血型血清学离心机 | 湘仪 | XKA-2200 | 峨山彝族自治县医疗与健康服务集团 | 惠而好医疗科技（云南）有限公司 | 2026-03-25 | 1.75万元 | 查看\n'
    '血型血清专用离心机（医用离心机 | 湘仪 | XKA-2200 | 和田地区人民医院 | 新疆贝昇康润生物有限公司 | 2026-03-12 | 1.07万元 | 查看\n'
    '血液离心机 | (卢)湘仪/湘鑫 | XKA-2200 | 吉林市中医院 | 上药科园信海医药吉林市有限公司 | 2024-04-07 | 3.00万元 | 查看\n'
    '免疫血液学用离心机 | 湘仪 | XKA-2200 | 北京天坛医院 | 上药康德乐（北京）医药有限公司 | 2024-03-28 | 4.70万元 | 查看\n'
    '血液离心机 | (卢)湘仪/湘鑫 | XKA-2200 | 吉林市中医院 | 上药科园信海医药吉林市有限公司 | 2023-10-08 | 3.00万元 | 查看\n'
    '免疫血液学离心机（允许进口） | (卢)湘仪/湘鑫 | XKA-2200 | 红河哈尼族彝族自治州中心血站 | 云南维动乐医疗器械有限公司 | 2022-11-14 | 3.98万元 | 查看\n'
    '免疫血清学离心机 | 湖南湘仪 | XKA-2200 | 厦门医学院 | 厦门联信诚有限公司 | 2018-07-27 | 0.88万元 | 查看\n'
    '其他专用仪器仪表 | 湖南湘仪 | XKA-2200 | 厦门医学院 | 厦门联信诚有限公司 | 2018-07-03 | 0.88万元 | 查看\n'
)


def _source(body, **overrides):
    source = {'url': 'https://www.120shop.cn/i890850380', 'body_text': body,
              'body_status': 'fetched', 'source_type': 'unknown',
              'snapshot': {'sha256': 'de2ad2363f94d06c2fa23f804a231c20402c4744a456eccb224804b0c3db2159'}}
    source.update(overrides)
    return source


def _service(tmp_path):
    return ProcurementResearch(SQLiteRepository(tmp_path / 'republished.sqlite3'), None, None)


# ---------------------------------------------------------------------------
# Reading the row
# ---------------------------------------------------------------------------

def test_a_republished_row_yields_a_dated_amount_for_a_named_buyer():
    rows = republished_award_rows(REPUBLISHED_BODY, 'XKA-2200')
    tiantan = next(r for r in rows if r['purchaser'] == '北京天坛医院')
    assert tiantan['date'] == '2024-03-28'
    assert (tiantan['amount'], tiantan['amount_unit']) == (4.70, '万元')
    assert tiantan['brand'] == '湘仪'
    assert tiantan['excerpt'].startswith('免疫血液学用离心机')


def test_every_published_row_is_read_including_the_brand_variants():
    rows = republished_award_rows(REPUBLISHED_BODY, 'XKA-2200')
    assert len(rows) == 9
    assert {r['purchaser'] for r in rows} == {
        '木垒哈萨克自治县人民医院', '峨山彝族自治县医疗与健康服务集团', '和田地区人民医院',
        '吉林市中医院', '北京天坛医院', '红河哈尼族彝族自治州中心血站', '厦门医学院'}


def test_a_model_written_with_spaces_around_the_hyphen_is_still_matched():
    # The live page publishes 「XKA - 2200」. An exact-string comparison drops the
    # row silently, which is how one of the nine real records went missing while
    # the other eight were read.
    rows = republished_award_rows(REPUBLISHED_BODY, 'XKA-2200')
    assert '木垒哈萨克自治县人民医院' in {r['purchaser'] for r in rows}


def test_the_supplier_is_not_mistaken_for_the_buyer():
    rows = republished_award_rows(REPUBLISHED_BODY, 'XKA-2200')
    tiantan = next(r for r in rows if r['purchaser'] == '北京天坛医院')
    assert tiantan['supplier'] == '上药康德乐（北京）医药有限公司'


def test_a_row_that_only_names_a_seller_is_not_a_purchase():
    body = '医用离心机 | 湘仪 | XKA-2200 | 某某医疗器械有限公司 | 2024-01-01 | 1.00万元\n'
    assert republished_award_rows(body, 'XKA-2200') == []


def test_a_group_that_is_a_company_is_a_seller_not_a_buyer():
    # 「集团」 alone would let a trading group pass as the buying unit. The rule
    # is the complement: a 集团 that is a 公司 is a seller.
    body = '医用离心机 | 湘仪 | XKA-2200 | 某某医疗科技集团有限公司 | 2024-01-01 | 1.00万元\n'
    assert republished_award_rows(body, 'XKA-2200') == []


def test_a_county_health_group_is_read_as_the_buyer():
    # 峨山彝族自治县医疗与健康服务集团 carries no suffix from the institution
    # list, and is a genuine county-level public buyer.
    body = '医用离心机 | 湘仪 | XKA-2200 | 峨山彝族自治县医疗与健康服务集团 | 2024-01-01 | 1.00万元\n'
    rows = republished_award_rows(body, 'XKA-2200')
    assert rows and rows[0]['purchaser'] == '峨山彝族自治县医疗与健康服务集团'


def test_a_row_without_a_date_is_ignored():
    body = '医用离心机 | 湘仪 | XKA-2200 | 北京天坛医院 | 1.00万元\n'
    assert republished_award_rows(body, 'XKA-2200') == []


def test_a_row_without_an_amount_is_ignored():
    body = '医用离心机 | 湘仪 | XKA-2200 | 北京天坛医院 | 2024-01-01\n'
    assert republished_award_rows(body, 'XKA-2200') == []


def test_an_amount_published_before_its_date_is_ignored():
    # The date/amount order is what keeps a stray figure elsewhere in the line
    # from being read as this row's price.
    body = '医用离心机 | 湘仪 | XKA-2200 | 北京天坛医院 | 1.00万元 | 2024-01-01\n'
    assert republished_award_rows(body, 'XKA-2200') == []


def test_a_row_for_another_model_is_ignored():
    body = '医用离心机 | 湘仪 | H1850R | 北京天坛医院 | 2024-01-01 | 1.00万元\n'
    assert republished_award_rows(body, 'XKA-2200') == []


def test_budget_and_limit_wording_never_becomes_a_republished_record():
    # Republished rows carry no announcement wording at all, so the only shapes
    # that reach here are dated award rows. A budget line has no purchase date.
    body = '采购预算 | ￥35,000.00 | 最高限价 | 35000\n'
    assert republished_award_rows(body, 'XKA-2200') == []


# ---------------------------------------------------------------------------
# Grade and provenance -- the properties the user asked to keep
# ---------------------------------------------------------------------------

def test_the_record_is_graded_secondary(tmp_path):
    service = _service(tmp_path)
    run = {'sources': [_source(REPUBLISHED_BODY)]}
    records = service.republished_prices(run, TARGET)
    assert records and all(r['evidence_grade'] == 'secondary' for r in records)


def test_the_record_carries_a_clickable_source_link(tmp_path):
    service = _service(tmp_path)
    records = service.republished_prices({'sources': [_source(REPUBLISHED_BODY)]}, TARGET)
    assert all(r['url'] == 'https://www.120shop.cn/i890850380' for r in records)
    assert all(r['excerpt'] for r in records)


def test_the_unit_price_is_normalised_from_the_published_unit(tmp_path):
    service = _service(tmp_path)
    records = service.republished_prices({'sources': [_source(REPUBLISHED_BODY)]}, TARGET)
    tiantan = next(r for r in records if r['purchaser'] == '北京天坛医院')
    assert tiantan['unit_price'] == 47000.0


def test_a_formal_publication_is_never_read_as_a_secondary_record(tmp_path):
    # First-hand announcements keep going through ``prices()`` and its stricter
    # predicate stack; reading them here too would open a weaker second route in.
    service = _service(tmp_path)
    run = {'sources': [_source(REPUBLISHED_BODY, source_type='tender_document')]}
    assert service.republished_prices(run, TARGET) == []


def test_an_unfetched_source_is_not_read(tmp_path):
    service = _service(tmp_path)
    run = {'sources': [_source(REPUBLISHED_BODY, body_status='failed')]}
    assert service.republished_prices(run, TARGET) == []


def test_identical_records_are_not_counted_twice(tmp_path):
    service = _service(tmp_path)
    run = {'sources': [_source(REPUBLISHED_BODY), _source(REPUBLISHED_BODY)]}
    assert len(service.republished_prices(run, TARGET)) == 9


# ---------------------------------------------------------------------------
# Configuration: taken from the row when it states one, otherwise reported as
# not provided by the announcement -- never silently invented.
# ---------------------------------------------------------------------------

def test_configuration_is_reported_as_not_stated_when_the_row_omits_it():
    rows = republished_award_rows(REPUBLISHED_BODY, 'XKA-2200')
    assert all(r['configuration'] == 'unknown' for r in rows)


def test_configuration_is_read_when_the_row_states_it():
    body = '医用离心机 | 湘仪 | XKA-2200 | 标准配置 | 北京天坛医院 | 2024-01-01 | 1.00万元\n'
    rows = republished_award_rows(body, 'XKA-2200')
    assert rows and rows[0]['configuration'] == '标准配置'


def test_the_real_rows_span_a_wide_but_publishable_range(tmp_path):
    # A 5x spread is exactly why these rows go to the user rather than into a
    # single "historical range": the caller shows each one with its own date.
    service = _service(tmp_path)
    records = service.republished_prices({'sources': [_source(REPUBLISHED_BODY)]}, TARGET)
    prices = sorted(r['unit_price'] for r in records)
    assert prices[0] == 8800.0
    assert prices[-1] == 47000.0
    assert [r['date'] for r in records] == sorted((r['date'] for r in records), reverse=True)
