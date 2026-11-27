"""线索对齐闸门（设计规范 三个硬验收案例 + 型号归一化反例）。

本文件先于实现写成：三个案例的数据全部来自真实记录，不是编的。

- 案例①湖北大学：线索（买方=湖北大学、卖方=武汉坎德尼、3.25 万、知楚 1300A2、品类=生物安全柜）
  命中的是 `tests/fixtures/lead-alignment/hubu-12240.body.txt`（供应商优尚和越、96.2 万、新华牌灭菌器）。
  线索侧出处：`设计规范` §1 事实 #6；命中公告的原文 2026-09-24 从
  https://xxgk.hubu.edu.cn/info/1269/12240.htm 实抓（见夹具 README）。
- 案例②库车市农业农村局：线索 知楚 1500A2；命中公告
  http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202502/t20250218_24183413.htm
  正文第 32 行「生物安全柜 | 上海知楚 | 1 | 32000 | 1500A2」。
- 案例③杭锦旗疾控：线索 泰斯特生化培养箱 SPX-150BIII；命中公告
  http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202609/t20260921_27374817.htm
  （`source_type=tender_document`、`authority_score=85`），正文第 79-80 行是福玛 DPX-9182B-2 / SPX-160B。
  正文取自隔离库 `output/bio-instrument-acceptance-20260920/browser-runtime/acceptance.sqlite3`
  （`body-cache-ec9e8e66…`，task-58a395f8b52f 的同名公告），与 wt-metric-optional 库一致。
"""
from pathlib import Path

import pytest

from backend.domain.search import BodyStatus, SourceRecord, SourceType
from backend.services import source_authority
from backend.services.lead_alignment import (
    AUTHORITY_BY_TIER,
    STATE_REJECTED,
    STATE_UNRESOLVED,
    STATE_VERIFIED,
    AlignmentReport,
    LeadProfile,
    align_sources,
    coerce_lead,
    is_same_model,
    model_present,
    model_tokens,
    normalize_model,
    verify_against_lead,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "lead-alignment"

HUBEI_URL = "https://xxgk.hubu.edu.cn/info/1269/12240.htm"
KUCHA_URL = "http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202502/t20250218_24183413.htm"
HANGJIN_URL = "http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202609/t20260921_27374817.htm"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def notice(url: str, title: str, body: str, *, date: str, snippet: str = "",
           authority_score: int = 85, source_type: SourceType = SourceType.TENDER_DOCUMENT,
           with_body: bool = True) -> SourceRecord:
    """按 `official_search.py:235` 的位置参数写法构造，避免与生产构造方式脱节。"""
    from dataclasses import replace
    record = SourceRecord(url, title, source_type, authority_score,
                          "已完成招标/采购结果文件，可作为历史成交价证据", snippet)
    if with_body:
        record = record.with_body(body)
    return replace(record, publication_date=date)


# ---------------------------------------------------------------------------
# 案例① 湖北大学：买方相符、权威来源、金额巨大 —— 但卖方不是线索里那家
# ---------------------------------------------------------------------------


def test_case_hubei_is_rejected_on_supplier_mismatch():
    lead = LeadProfile(
        buyer="湖北大学",
        supplier="武汉坎德尼科技有限公司",
        amount=32500.0,
        amount_text="3.25万",
        model="1300A2",
        brand="知楚",
        category="生物安全柜",
        date="2024-11-19",
    )
    source = notice(
        HUBEI_URL,
        "湖北大学病毒学和疫苗研究中心动物房建设配套设备货物采购项目成交结果公告",
        fixture_text("hubu-12240.body.txt"),
        date="2024-12-10",
    )

    verdict = verify_against_lead(
        source, lead,
        attempted_queries=("生物安全柜", "1300A2", "湖北大学"),
        windows=(("2023-11-20", "2024-11-19"),),
    )

    assert verdict.state == STATE_REJECTED
    assert "供应商不匹配" in verdict.reason
    # 理由必须点名两边的卖方，用户才能判断
    assert "武汉坎德尼科技有限公司" in verdict.reason
    assert "优尚和越环境科技有限公司" in verdict.reason
    assert verdict.declared_supplier == "优尚和越环境科技有限公司"
    # 权威性满分也压不住硬冲突（这张截图的核心教训）
    assert verdict.authority == AUTHORITY_BY_TIER[source_authority.LOCAL_AUTHORITY]
    assert verdict.combined_score > 0.0, "分数不低却仍被拒，正是要证明的事"
    # 买方和来源都吻合，说明拒绝不是因为检索失败
    assert verdict.buyer_matched is True
    assert verdict.amount == pytest.approx(962000.0)
    assert verdict.amount_basis == "整包价"
    assert verdict.url == HUBEI_URL


def test_case_hubei_reason_also_reports_amount_gap():
    lead = LeadProfile(buyer="湖北大学", supplier="武汉坎德尼科技有限公司",
                       amount=32500.0, model="1300A2", category="生物安全柜")
    source = notice(HUBEI_URL, "湖北大学病毒学和疫苗研究中心动物房建设配套设备货物采购项目成交结果公告",
                    fixture_text("hubu-12240.body.txt"), date="2024-12-10")
    verdict = verify_against_lead(source, lead)
    assert "金额" in verdict.reason, verdict.reason
    assert verdict.field_match < 0.5


# ---------------------------------------------------------------------------
# 案例② 库车市农业农村局：同型号命中 → verified，带原文链接和金额
# ---------------------------------------------------------------------------


def test_case_kuche_is_verified_with_url_and_unit_price():
    lead = LeadProfile(
        buyer="库车市农业农村局",
        brand="知楚",
        model="1500A2",
        category="生物安全柜",
        date="2025-02-18",
    )
    source = notice(
        KUCHA_URL,
        "库车市现代农作物种子科研及加工基地建设项目设备采购中标(成交)结果公告",
        fixture_text("kucha-24183413.body.txt"),
        date="2025-02-18",
    )

    verdict = verify_against_lead(source, lead)

    assert verdict.state == STATE_VERIFIED
    assert verdict.url == KUCHA_URL
    assert verdict.amount == pytest.approx(32000.0), "公告正文第 32 行的单台单价"
    assert verdict.amount_basis == "单台单价"
    assert verdict.quantity == pytest.approx(1.0)
    assert normalize_model("1500A2") in verdict.matched_models
    assert "上海知楚" in verdict.reason or "知楚" in verdict.reason
    # 同一条公告的整包价另记，别把整包价当单台价
    assert verdict.notice_amount == pytest.approx(4915000.0)


def test_case_kuche_verified_requires_the_model_not_the_category():
    """同一条公告，换一个品类词相同的型号就不该 verified。"""
    lead = LeadProfile(buyer="库车市农业农村局", brand="知楚", model="1306A2", category="生物安全柜")
    source = notice(KUCHA_URL, "库车市现代农作物种子科研及加工基地建设项目设备采购中标(成交)结果公告",
                    fixture_text("kucha-24183413.body.txt"), date="2025-02-18")
    verdict = verify_against_lead(source, lead)
    assert verdict.state == STATE_REJECTED


def test_case_kuche_declared_supplier_comes_from_the_right_column():
    """库车公告的表头第一列是序号，供应商名在第 2 列（踩过的坑）。"""
    source = notice(KUCHA_URL, "库车市现代农作物种子科研及加工基地建设项目设备采购中标(成交)结果公告",
                    fixture_text("kucha-24183413.body.txt"), date="2025-02-18")
    verdict = verify_against_lead(source, LeadProfile(model="1500A2", category="生物安全柜"))
    assert verdict.declared_supplier == "新疆鑫华力科析科技有限公司"


def test_notice_amount_does_not_leak_from_a_table_header_row():
    """表头里的「中标（成交）金额」后面紧跟的是下一行的序号，不能当成金额。"""
    body = ("某单位仪器设备采购中标(成交)结果公告\n"
            "序号 | 供应商名称 | 供应商地址 | 中标（成交）金额 | 评审总得分\n"
            "1 | 某某科技有限公司 | 某地 | 报价：5000（元） | 90\n")
    verdict = verify_against_lead(
        notice("http://www.ccgp.gov.cn/hdr.htm", "某单位仪器设备采购中标(成交)结果公告", body, date="2026-01-01"),
        LeadProfile(model="AAA-1"))
    assert verdict.notice_amount is None


# ---------------------------------------------------------------------------
# 案例③ 杭锦旗疾控：.gov.cn + 85 分 + 决标措辞齐全，但与目标型号无关
# ---------------------------------------------------------------------------


def test_case_hangjin_is_rejected_on_model_and_brand():
    lead = LeadProfile(
        buyer="杭锦旗疾病预防控制中心",
        brand="泰斯特",
        model="SPX-150BIII",
        category="生化培养箱",
        date="2026-09-21",
    )
    source = notice(
        HANGJIN_URL,
        "杭锦旗疾病预防控制中心仪器设备采购项目中标（成交）结果公告",
        fixture_text("hjj-27374817.body.txt"),
        date="2026-09-21",
        snippet="表电热恒温培养箱福玛DPX-9182B-25.00(个)5,400.000027,000.00001-32A02109900其他仪器仪表生化培养箱福玛SPX-160B2.00(个)",
    )

    verdict = verify_against_lead(source, lead)

    assert verdict.state == STATE_REJECTED
    assert "型号/品牌不匹配" in verdict.reason
    assert "公告内是" in verdict.reason
    assert "福玛" in verdict.reason
    assert "DPX-9182B" in verdict.reason
    # 权威性满分 + 时效满分，光靠权威判据一条都筛不掉（设计规范 第三个案例的意义）
    assert verdict.authority == pytest.approx(1.0)
    assert verdict.timeliness == pytest.approx(1.0)
    assert verdict.combined_score >= 0.5
    assert verdict.declared_supplier == "北京九州通医疗科技有限公司"


def test_case_hangjin_other_models_are_not_confused_with_the_target():
    """公告里同品类的其它型号（SPX-160B / DPX-9182B-2 / MP-160B）都不等于 SPX-150BIII。"""
    body = fixture_text("hjj-27374817.body.txt")
    tokens = model_tokens(body)
    assert normalize_model("SPX-150BIII") not in tokens
    for other in ("SPX-160B", "DPX-9182B-2", "MP-160B"):
        assert normalize_model(other) in tokens


# ---------------------------------------------------------------------------
# 反例一：型号归一化不能把相邻型号并成同一台
# ---------------------------------------------------------------------------


def test_1300a2_and_1306a2_stay_different():
    """设计规范：这两台的尺寸数值完全相同，是最容易被误判的一对。"""
    assert normalize_model("1300A2") != normalize_model("1306A2")
    assert not is_same_model("1300A2", "1306A2")
    assert not is_same_model(" 1300 A2 ", "1306 A2")


def test_spx150b_is_not_spx150biii_despite_substring():
    """`spx150b` 是 `spx150biii` 的子串 —— 用包含判定就会把绿博 SPX-150B 认成泰斯特 SPX-150BIII。"""
    assert "spx150b" in "spx150biii"  # 子串陷阱真实存在
    assert not is_same_model("SPX-150B", "SPX-150BIII")
    assert not is_same_model("SPX-150B-Z", "SPX-150BIII")
    assert not is_same_model("SPX-160B", "SPX-150BIII")


def test_dpx9182b_suffix_is_not_the_same_model():
    assert not is_same_model("DPX-9182B", "DPX-9182B-2")


def test_model_written_with_space_or_roman_suffix_is_the_same_model():
    """设计规范 要求：1300A2 / "1300 A2" / "1300A2 Ⅱ级" 视为同一型号。"""
    assert is_same_model("1300A2", "1300 A2")
    assert is_same_model("1300A2", "1300A2 Ⅱ级")
    assert normalize_model("1300A2 Ⅱ级") == "1300a2"
    assert model_present("生物安全柜 | 知楚 | 1300 A2 | 1 | 32500", "1300A2")
    assert model_present("知楚 1300A2 Ⅱ级 生物安全柜", "1300A2")
    assert model_present("知楚 1300-A2 生物安全柜", "1300A2")


def test_spaced_model_written_in_a_table_row_is_verified_not_rejected():
    """带空格的型号写法如果认不出来，就会把一条**正确**的公告误判成 rejected。"""
    body = ("某单位实验室设备采购中标(成交)结果公告\n"
            "1 | 某单位实验室设备采购 | 生物安全柜 | 知楚 | 1 | 32500 | 1300 A2\n")
    verdict = verify_against_lead(
        notice("http://www.ccgp.gov.cn/spaced.htm", "某单位实验室设备采购中标(成交)结果公告", body, date="2026-01-01"),
        LeadProfile(buyer="某单位", brand="知楚", model="1300A2", category="生物安全柜", date="2026-01-01"))
    assert verdict.state == STATE_VERIFIED
    assert verdict.amount == pytest.approx(32500.0)


def test_model_boundaries_are_not_crossed_by_adjacent_digits_or_letters():
    assert not model_present("生物安全柜 知楚 1300A21 台", "1300A2")
    assert not model_present("生化培养箱 绿博 SPX-150B", "SPX-150BIII")
    assert not model_present("生物安全柜 知楚 1306A2", "1300A2")
    assert model_present("生化培养箱 泰斯特 SPX 150BIII", "SPX-150BIII")


def test_spx150b_row_does_not_verify_a_spx150biii_lead():
    """绿博 SPX-150B 那类行（案例③检索摘录里真实出现过）不能把线索验成 verified。"""
    body = (
        "江格斯乡宠物饲料生产设备购置项目（二次）中标(成交)结果公告\n"
        "23 | 江格斯乡宠物饲料生产设备购置项目（二次） | 生化培养箱 | 绿博 | SPX-150B | 1 | 6800\n"
    )
    lead = LeadProfile(brand="泰斯特", model="SPX-150BIII", category="生化培养箱", buyer="江格斯乡")
    verdict = verify_against_lead(notice("http://www.ccgp.gov.cn/award.htm", "中标(成交)结果公告", body, date="2026-09-21"), lead)
    assert verdict.state == STATE_REJECTED
    assert "SPX-150B" in verdict.reason


# ---------------------------------------------------------------------------
# 反例二：型号只在摘要里 / 只在附件里
# ---------------------------------------------------------------------------


def test_model_in_snippet_only_still_matches():
    """摘要是检索摘录，属于可判断信号；正文取不到时不能把命中当没命中。"""
    body = "某单位实验室设备采购中标(成交)结果公告\n成交供应商：某某科技有限公司\n"
    lead = LeadProfile(buyer="某单位", model="SPX-150BIII", brand="泰斯特", category="生化培养箱")
    source = notice("http://www.ccgp.gov.cn/award.htm", "实验室设备中标(成交)结果公告", body,
                    date="2026-09-21", snippet="生化培养箱 泰斯特 SPX-150BIII 1台 6800")
    verdict = verify_against_lead(source, lead)
    assert verdict.state == STATE_VERIFIED
    assert normalize_model("SPX-150BIII") in verdict.matched_models


def test_model_only_in_attachment_is_unresolved_not_rejected():
    """正文里既没有目标型号、也没有同品类其它型号可对照时，只能 unresolved。

    这是已知缺口（型号只出现在附件 PDF 里），闸门不许把它误判成 rejected。
    """
    body = ("某疾控中心仪器设备采购中标(成交)结果公告\n"
            "成交供应商：某某科技有限公司\n中标（成交）金额：120.00万元\n"
            "四、主要标的信息\n详见附件：报价明细附件.pdf\n")
    lead = LeadProfile(buyer="某疾控中心", model="SPX-150BIII", brand="泰斯特", category="生化培养箱",
                       date="2026-09-21")
    source = notice("http://www.ccgp.gov.cn/award.htm", "某疾控中心仪器设备采购中标(成交)结果公告", body,
                    date="2026-09-21")
    verdict = verify_against_lead(source, lead)
    assert verdict.state == STATE_UNRESOLVED
    assert "证据不足" in verdict.reason


# ---------------------------------------------------------------------------
# 权威性直接来自 source_authority，不许另写一套分级
# ---------------------------------------------------------------------------


def test_authority_component_delegates_to_source_authority():
    body = ("某某医院医疗设备采购中标（成交）结果公告\n项目编号：ABC-1\n采购人：某某医院\n"
            "成交供应商：某某医疗器械有限公司\n中标金额：1,200,000.00元\n")
    host = "yycg.some-hospital.com"
    assert source_authority.publisher_tier(host, body) == source_authority.FORMAL_PUBLICATION
    lead = LeadProfile(buyer="某某医院", model="ZZZ-999", category="培养箱")
    verdict = verify_against_lead(notice(f"http://{host}/award.htm", "某某医院医疗设备采购中标（成交）结果公告", body,
                                         date="2026-01-01"), lead)
    assert verdict.authority == AUTHORITY_BY_TIER[source_authority.FORMAL_PUBLICATION]

    aggregator = verify_against_lead(
        notice("https://www.120shop.cn/i773711056", "报价线索", "面议 现货 加入购物车", date="2026-01-01",
               source_type=SourceType.AGGREGATOR, authority_score=25), lead)
    assert aggregator.authority < 0.2
    assert aggregator.authority < verdict.authority


def test_combined_score_formula_is_the_agreed_weights():
    lead = LeadProfile(buyer="杭锦旗疾病预防控制中心", brand="泰斯特", model="SPX-150BIII",
                       category="生化培养箱", date="2026-09-21")
    source = notice(HANGJIN_URL, "杭锦旗疾病预防控制中心仪器设备采购项目中标（成交）结果公告",
                    fixture_text("hjj-27374817.body.txt"), date="2026-09-21")
    verdict = verify_against_lead(source, lead)
    expected = 0.5 * verdict.field_match + 0.3 * verdict.authority + 0.2 * verdict.timeliness
    assert verdict.combined_score == pytest.approx(expected)


# ---------------------------------------------------------------------------
# unresolved 必须列出尝试过的查询式和窗口
# ---------------------------------------------------------------------------


def test_unresolved_must_list_attempted_queries_and_windows():
    lead = LeadProfile(buyer="某个没搜到的单位", model="AAA-1", category="培养箱")
    source = notice("http://www.ccgp.gov.cn/other.htm", "无关项目中标公告",
                    "无关项目中标(成交)结果公告\n成交供应商：某某公司\n", date="2026-01-01")
    queries = ("某个没搜到的单位", "AAA-1 培养箱")
    windows = (("2025-01-01", "2026-01-01"),)
    verdict = verify_against_lead(source, lead, attempted_queries=queries, windows=windows)
    assert verdict.state == STATE_UNRESOLVED
    assert verdict.attempted_queries == queries
    assert verdict.windows == windows
    assert "证据不足" in verdict.reason


def test_unresolved_through_align_sources_keeps_the_tried_queries():
    lead = LeadProfile(buyer="某个没搜到的单位", model="AAA-1", category="培养箱")
    sources = [notice("http://www.ccgp.gov.cn/other.htm", "无关项目中标公告",
                      "无关项目中标(成交)结果公告\n成交供应商：某某公司\n", date="2026-01-01")]
    report = align_sources(lead, sources, attempted_queries=("AAA-1 培养箱",),
                           windows=(("2025-01-01", "2026-01-01"),))
    assert isinstance(report, AlignmentReport)
    assert len(report.unresolved) == 1
    assert report.unresolved[0].attempted_queries == ("AAA-1 培养箱",)
    assert report.attempted_queries == ("AAA-1 培养箱",)
    assert report.to_dict()["unresolved"][0]["state"] == STATE_UNRESOLVED


# ---------------------------------------------------------------------------
# 三堆编排：真实公告各归各位
# ---------------------------------------------------------------------------


def test_align_sources_buckets_the_real_notices():
    hubei_lead = LeadProfile(buyer="湖北大学", supplier="武汉坎德尼科技有限公司", amount=32500.0,
                             model="1300A2", brand="知楚", category="生物安全柜", date="2024-11-19")
    kuche_lead = LeadProfile(buyer="库车市农业农村局", brand="知楚", model="1500A2",
                             category="生物安全柜", date="2025-02-18")
    hangjin_lead = LeadProfile(buyer="杭锦旗疾病预防控制中心", brand="泰斯特", model="SPX-150BIII",
                               category="生化培养箱", date="2026-09-21")

    hubei = notice(HUBEI_URL, "湖北大学病毒学和疫苗研究中心动物房建设配套设备货物采购项目成交结果公告",
                   fixture_text("hubu-12240.body.txt"), date="2024-12-10")
    kuche = notice(KUCHA_URL, "库车市现代农作物种子科研及加工基地建设项目设备采购中标(成交)结果公告",
                   fixture_text("kucha-24183413.body.txt"), date="2025-02-18")
    hangjin = notice(HANGJIN_URL, "杭锦旗疾病预防控制中心仪器设备采购项目中标（成交）结果公告",
                     fixture_text("hjj-27374817.body.txt"), date="2026-09-21")

    report = align_sources(hubei_lead, [hubei], attempted_queries=("生物安全柜",),
                           windows=(("2023-11-20", "2024-11-19"),))
    assert [v.url for v in report.rejected] == [HUBEI_URL]

    report = align_sources(kuche_lead, [kuche], attempted_queries=("1500A2",),
                           windows=(("2024-02-19", "2025-02-18"),))
    assert [v.url for v in report.verified] == [KUCHA_URL]

    report = align_sources(hangjin_lead, [hangjin], attempted_queries=("SPX-150BIII 生化培养箱",),
                           windows=(("2025-09-21", "2026-09-21"),))
    assert [v.url for v in report.rejected] == [HANGJIN_URL]


# ---------------------------------------------------------------------------
# 线索输入适配：脏值不炸链路，字段名两种写法都认
# ---------------------------------------------------------------------------


def test_coerce_lead_accepts_mapping_alias_and_text_amount():
    lead = coerce_lead({
        "purchaser": "  湖北大学 ",
        "supplier_name": "武汉坎德尼科技有限公司",
        "amount": "3.25万",
        "model": "1300A2",
        "brand": "知楚",
        "category": "生物安全柜",
        "notice_date": "2024-11-19",
    })
    assert lead.buyer == "湖北大学"
    assert lead.supplier == "武汉坎德尼科技有限公司"
    assert lead.amount == pytest.approx(32500.0)
    assert lead.date == "2024-11-19"


def test_coerce_lead_survives_junk():
    lead = coerce_lead({"purchaser": None, "amount": "", "model": "  ", "date": "2024/11/19"})
    assert lead.buyer is None and lead.model is None
    assert lead.amount is None
    # 非 ISO 日期不猜：保留原文但不参与时效打分
    verdict = verify_against_lead(
        notice("http://www.ccgp.gov.cn/a.htm", "标题", "正文 中标公告", date="2024-11-19"), lead)
    assert 0.0 <= verdict.timeliness <= 1.0
