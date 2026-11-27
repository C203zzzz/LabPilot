"""Adversarial coverage for the single publisher/authority classifier.

Every "before the fix" assertion in this file describes the concrete failure
the handoff document records: a ``*.com`` bidding agency's award notice was
rewritten from ``tender_document`` to ``unknown`` by a TLD test, and the price
evidence built from it was then rejected downstream.
"""
import pytest

from backend.services.official_search import SourceType, authority, classify_source
from backend.services.price_rejections import (
    SKIP_TYPE_NOT_PRICE_SOURCE,
    price_source_candidates,
)
from backend.services.procurement_rules import contains_model, validate_price
from backend.services.procurement_tables import price_table_rows
from backend.services.source_authority import (
    AGGREGATOR_HOST_MARKERS,
    FORMAL_PUBLICATION,
    LOCAL_AUTHORITY,
    host_of,
    is_marketplace_or_aggregator,
    is_tender,
    publication_tier,
    publisher_tier,
    source_display_authority,
    source_publication_tier,
    source_publisher_tier,
)
from backend.services.source_identity import verify_publisher
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture

# A realistic award notice published by a bidding agency on a ``*.com`` host.
AGENCY_AWARD = (
    '某某招标代理有限公司\n'
    '某某市第一人民医院医疗设备采购项目中标（成交）结果公告\n'
    '项目编号：XYZB-2026-0417\n'
    '采购人：某某市第一人民医院\n'
    '成交供应商：某某医疗器械有限公司\n'
    '中标金额：人民币 236,000.00 元\n'
    '评审专家名单：张三、李四\n'
    '公告日期：2026年09月18日\n'
    '货物名称 | 规格型号 | 数量 | 单价（元）\n'
    '生物安全柜 | 知楚A2 | 2 | 74,800\n'
)

# A solicitation: it announces a purchase, never a concluded price.
AGENCY_SOLICITATION = (
    '某某招标代理有限公司\n'
    '某某市第一人民医院医疗设备采购项目招标公告\n'
    '项目编号：XYZB-2026-0417\n'
    '采购人：某某市第一人民医院\n'
    '招标代理机构：某某招标代理有限公司\n'
    '公告日期：2026年09月01日\n'
    '货物名称 | 规格型号 | 数量 | 单价（元）\n'
    '生物安全柜 | 知楚A2 | 2 | 面议\n'
)


def fetched(url, body):
    return {'url': url, 'body_status': 'fetched', 'body_text': body}


# --------------------------------------------------------------------------
# The historic trusted-transport rule must not move.
# --------------------------------------------------------------------------

def test_local_authority_keeps_the_historic_weak_body_rule():
    # Two structural fields and no formal wording was already enough.
    weak = fetched('https://buyer.example.gov.cn/n', '项目编号：A1\n采购人：某单位')
    assert source_publication_tier(weak) == LOCAL_AUTHORITY
    assert is_tender(weak)

    unrelated = fetched('https://buyer.example.gov.cn/news', '校园运动会通知')
    assert source_publication_tier(unrelated) is None

    # A dealer on an institutional host is still not a procurement publisher.
    assert publication_tier('dealer.example.edu.cn', '成交公告 单价1000元') == LOCAL_AUTHORITY


# --------------------------------------------------------------------------
# The fix: evidence replaces the TLD guess.
# --------------------------------------------------------------------------

@pytest.mark.parametrize('url', [
    'https://www.example-zb.com/notice/20260918-award',       # bidding agency
    'https://www.example-hospital.com.cn/gg/20260918.html',   # hospital
    'https://www.example-cdc.org.cn/tzgg/award.htm',          # CDC
    'https://www.example-institute.cn/zbxx/result',           # research institute
])
def test_non_government_hosts_with_real_award_evidence_are_accepted(url):
    source = fetched(url, AGENCY_AWARD)
    assert source_publication_tier(source) == FORMAL_PUBLICATION
    assert is_tender(source)


def test_award_evidence_outranks_the_domain_in_both_directions():
    """A ``.gov.cn`` host is neither necessary nor sufficient."""
    assert is_tender(fetched('https://bid.example-zb.com/a', AGENCY_AWARD))
    assert not is_tender(fetched('https://www.example.gov.cn/news', '今日天气晴'))


def test_bidding_agency_award_page_becomes_tender_document_end_to_end(research):  # noqa: F811
    """Before the fix this page was downgraded to ``unknown`` and priced out."""
    service, run, provider, _ = research
    url = 'https://www.example-zb.com/notice/20260918-award'
    provider.bodies = {url: AGENCY_AWARD}
    source = service.fetch_source(run, {'url': url, 'source_type': 'unknown'})

    assert source['source_type'] == 'tender_document'
    assert source.get('authority_gap') is None

    row = price_table_rows(source['body_text'], 'A2')[0]
    baseline = {'brand': '知楚', 'model': 'A2'}
    accepted = validate_price({**row, 'kind': 'award'}, source, baseline)
    assert accepted['unit_price'] == 74800
    # The TLD gate's outcome: identical evidence, downgraded type, no price.
    assert validate_price({**row, 'kind': 'award'}, {**source, 'source_type': 'unknown'}, baseline) is None


def test_asset_register_on_a_bidding_agency_host_is_kept_as_a_lead():
    from backend.services.price_leads import asset_price_leads
    body = ('某某招标代理有限公司 固定资产台账\n'
            '资产编号：ZC-2026-01\n型号：知楚A2\n单价：74800元\n')
    source = fetched('https://www.example-zb.com/assets/2026', body)
    leads = asset_price_leads([source], {'model': 'A2'})
    assert [lead['amount'] for lead in leads] == [74800]


def test_publisher_identity_can_be_vouched_by_a_dot_com_institution():
    candidate = {'url': 'https://www.vendor.example/product', 'body_text': '星海科学仪器有限公司 产品目录'}
    source = fetched('https://www.example-hospital.com.cn/partners',
                     '某某市第一人民医院\n采购结果公告\n项目编号：A1\n采购人：某某市第一人民医院\n'
                     '中标供应商：星海科学仪器有限公司\n中标金额：1000元\n'
                     '合作单位 星海科学仪器有限公司 [https://www.vendor.example]')
    assert verify_publisher(candidate, [source], '星海')


# --------------------------------------------------------------------------
# Negative cases: the widening must not become a hole.
# --------------------------------------------------------------------------

def test_a_solicitation_is_not_a_concluded_publication():
    """``招标公告`` states no award; three structural fields must not mask it."""
    assert publication_tier('www.example-zb.com', AGENCY_SOLICITATION) is None
    assert not is_tender(fetched('https://www.example-zb.com/a', AGENCY_SOLICITATION))


def test_award_wording_alone_is_not_enough_without_structural_fields():
    assert publication_tier('www.example-zb.com', '成交公告') is None
    assert publication_tier('www.example-zb.com', '本公司守合同重信用，产品多次中标。') is None


@pytest.mark.parametrize('host', [
    'www.amazon.com', 'www.alibaba.com', 'www.made-in-china.com', 'www.labx.com',
    'www.fishersci.com', 'en.wikipedia.org', 'www.pdfcoffee.com', 'www.manualslib.com',
    'www.qianlima.com', 'www.yiliaozhaobiao.com', 'www.zhaomingzhaobiao.com',
])
def test_marketplaces_and_aggregators_never_become_publications(host):
    assert publication_tier(host, AGENCY_AWARD) is None
    assert publisher_tier(host, AGENCY_AWARD) is None


def test_a_bid_information_aggregator_listing_page_is_not_a_publication():
    """真实数据：``task-20358119eae4`` 的第 25 条来源。

    ``http://www.qianlima.com/gjxx/232/index_11_3.html`` 是千里马招标网的**分类列表页**
    （正文 22161 字，自己写着「生物安全柜 共有 2577 条相关信息」），标题里有「中标结果」，
    正文里带着几十条公告的链接标题，因此 FORMAL_DECISION 命中了列表项里的「采购合同」，
    结构字段也凑够了 7 个（项目编号/采购人/采购单位/采购机构/成交金额/评审专家/公告日期），
    商户自述词一个都没有。原来的判据于是把这一页判成了 ``formal_publication``。
    下面这段正文是那 22161 字的忠实缩影：保留了同样的信号（列表页头部 + 一条列表项 +
    若干结构字段），因此它证明的是**域名登记**在挡它，而不是正文恰好不合格。
    """
    body = ('黑龙江生物安全柜中标结果\n您好，欢迎您来到千里马招标网\n登录 |\n注册 |\n'
            '[ 资费标准 ]\n[ 付款方式 ]\n分站 广东 北京 江苏 浙江 山东\n'
            '生物安全柜 共有 2577 条相关信息\n【中标结果】\n'
            '兰西县畜牧兽医综合服务中心生猪防疫服务体系建设项目（设备）政府采购合同公告）\n'
            '兰西县\n2026-09-21\n'
            '项目编号：XYZB-2026-0417\n采购人：某县畜牧兽医综合服务中心\n'
            '成交供应商：某某医疗器械有限公司\n成交金额：150000元\n')
    # 同一段正文在没有登记的主机上仍然按「机构自己发布的成交公告」处理。
    assert publication_tier('www.example-zb.com', body) == FORMAL_PUBLICATION
    assert publisher_tier('www.example-zb.com', body) == FORMAL_PUBLICATION
    # 登记之后，这个域名上的页面永远拿不到发布者/公告等级。
    assert publication_tier('www.qianlima.com', body) is None
    assert publisher_tier('www.qianlima.com', body) is None
    assert not is_tender(fetched('http://www.qianlima.com/gjxx/232/index_11_3.html', body))


# 真实数据：医招网 / 照明招标网。下面这段正文是
# ``https://www.yiliaozhaobiao.com/news-38fc41b1dcc44392891a1324a0acdbb7``
# 那条来源（3392 字正文里搬运的公告主体）的忠实缩影，结构与字段名逐字保留；
# 站点头部的「登录 / 立即查看 / 立即引荐」不是 SELLER_SELF_DESCRIPTION 里的
# 商户自述词（没有面议/现货/购物车），所以**正文判据拦不住它**，
# 原来的代码真的把它判成了 formal_publication / is_tender=True。
YILIAO_AWARD_BODY = (
    '重庆医科大学附属第一医院离心机(CQS26A02375)中标（成交）结果公告\n'
    '您好，欢迎来到医疗招标网!\n咨询热线： 400-688-2000 登录\n'
    '首页 采购意向 招标公告 中标公告 医疗器械 医用耗材 企业名录 甲方通讯录\n'
    '当前位置:\n医疗招标网 [https://www.yiliaozhaobiao.com] >\n中标公告\n'
    '重庆****公司\n企业详情 > 相似采购商 >\n联系人 354个\n立即查看\n'
    '可引荐人脉 507人\n立即引荐\n历史招中标信息 16926条\n立即监控\n'
    '一、项目号：**** 点击查看 采购执行编号：ZC-GZ-**** 点击查看 0875 采购方式：公开招标\n'
    '二、项目名称：**** 点击查看 离心机\n'
    '三、中标（成交）信息：\n包号：1\n供应商名称：**** 点击查看\n'
    '中标（成交）金额： 487,600.00元\n'
    '四、主要标的信息\n包号：1\n小型台式离心机3 | 湘智 | TGL-20MC | 19台 | 14,500.00元\n'
    '五、评审专家名单\n邹正 张萌萌 惠** 肖耀光 张玉洪（采购人代表）\n'
    '代理服务费总计：17344.0元\n'
    '1、采购人信息\n采购人：**** 点击查看\n采购经办人：董政宏\n'
    '2、采购代理机构信息\n代理机构：**** 点击查看\n代理机构经办人：郑菡 罗成\n'
)

ZHAOMING_AWARD_BODY = (
    '广西交投宏冠工程咨询有限公司关于2026年实验室仪器设备、动物疫苗冷库及应急防疫物资'
    '采购项目（GXZC2026-G1-002493-GXJT）中标结果公告\n'
    '您好，欢迎来到照明招标网!\n咨询热线： 400-688-2000 登录\n'
    '首页 采购意向 招标公告 中标公告 照明工程 路灯 企业名录 甲方通讯录\n'
    '当前位置:\n照明招标网 [https://www.zhaomingzhaobiao.com] >\n中标公告\n'
    '中建****公司\n企业详情 > 相似采购商 >\n联系人 133个\n立即查看\n'
    '可引荐人脉 753人\n立即引荐\n历史招中标信息 1667条\n立即监控\n'
    '一、项目编号：**** 点击查看\n'
    '二、项目名称：2026年实验室仪器设备、动物疫苗冷库及应急防疫物资采购项目\n'
    '三、中标（成交）信息\n1.中标结果：\n'
    '1 | 单价②：513600（元），总价③=①×②：513600（元） | ****点击查看 | **市**区新港东路1226号803室\n'
    '四、主要标的信息\n货物类主要标的信息：\n'
    '序号 | 标项名称 | 标的名称 | 品牌 | 规格型号 | 数量 | 单价(元)\n'
    '1 | 实验室仪器设备一 | 二氧化碳培养箱 | 艺斯高 | CLM-170B-8-TC | 1 | 77000\n'
    '2 | 实验室仪器设备一 | 二级生物安全柜 | 赛默飞 | 1574 | 1 | 99000\n'
    '五、评审专家名单\n黄** 韦** 蒙**\n'
    '采购人：某医院\n采购代理机构：广西交投宏冠工程咨询有限公司\n'
)


def test_the_two_named_aggregators_are_registered_and_never_become_publications():
    """把医招网与照明招标网登记进 ``AGGREGATOR_HOST_MARKERS``（``设计规范`` §3 结论 2）。

    实测 (30 个库、3,283 条研究来源，跨库去重键 ``(task_id, url)``)：这两个域名下
    有 7 + 1 条来源**用当前判据仍能自己过** ``is_tender``、拿到 ``tender_document``/85 分
    「已完成招标/采购结果文件，可作为历史成交价证据」。它们没有进最终成交价（``prices``
    里非 gov/edu = 0 条），所以这是**前瞻性**收益：防止以后聚合站转载冒充官方成交证据。
    ``qianlima.com``（``897412d``）是同类先例，登记后实测有效。
    """
    for domain in ('yiliaozhaobiao.com', 'zhaomingzhaobiao.com'):
        assert domain in AGGREGATOR_HOST_MARKERS, domain

    for host, body in (
        ('www.yiliaozhaobiao.com', YILIAO_AWARD_BODY),
        ('www.zhaomingzhaobiao.com', ZHAOMING_AWARD_BODY),
    ):
        assert is_marketplace_or_aggregator(host) is True, host
        assert source_publication_tier(fetched(f'https://{host}/news-x', body)) is None, host
        assert source_publisher_tier(fetched(f'https://{host}/news-x', body)) is None, host
        assert is_tender(fetched(f'https://{host}/news-x', body)) is False, host
        assert classify_source(f'https://{host}/news-x', '中标结果公告') is SourceType.AGGREGATOR

    # 同一段正文落在**未登记**的第三方主机上仍然按机构自己发布的成交公告处理——
    # 这说明挡它的是域名登记，不是正文恰好不合格。
    assert source_publication_tier(
        fetched('https://www.example-zb.com/news-x', YILIAO_AWARD_BODY)) == FORMAL_PUBLICATION
    assert source_publication_tier(
        fetched('https://www.example-zb.com/news-x', ZHAOMING_AWARD_BODY)) == FORMAL_PUBLICATION


def test_registering_the_two_aggregators_does_not_touch_official_sources():
    """反向检查：``设计规范`` §2.3 里官方公告的政采云 OSS 附件与 gov/edu 来源一条都不能动。

    按域名一刀切设天花板会确定误杀 86 条官方附件；本单只扩黑名单，
    公开的 host 判据 ``is_marketplace_or_aggregator`` 对它们必须仍为 ``False``，
    等级与 ``classify_source`` 结果都不许改。下面用的两个 OSS host 与正文规模
    取自真实附件记录（``anhui-gov-open-doc`` 那条自己的正文就过了判据）。
    """
    award_table = ('采购合同\n项目编号：AH-2026-0001\n采购人：某某市第一人民医院\n'
                   '中标供应商：某某医疗器械有限公司\n中标金额：1500000元\n'
                   '评审专家：张三、李四\n合同金额：1500000元\n'
                   '公告日期：2026年09月18日\n')
    for host in (
        'sh-gov-open-doc.oss-cn-shanghai.aliyuncs.com',
        'zcy-gov-open-doc.oss-cn-north-2-gov-1.aliyuncs.com',
        'anhui-gov-open-doc.oss-cn-hangzhou.aliyuncs.com',
        'sx2gov2open2doc.uos.sxzfcg.zcygov.cn',
    ):
        assert is_marketplace_or_aggregator(host) is False, host
        assert source_publication_tier(fetched(f'https://{host}/f.pdf', award_table)) \
            == FORMAL_PUBLICATION, host
        assert is_tender(fetched(f'https://{host}/f.pdf', award_table)), host

    for url in (
        'http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202408/t20240801_22782834.htm',
        'https://gzc.wmu.edu.cn/info/1234/5678.htm',
        'https://cgzx.hust.edu.cn/award/1.htm',
        'https://www7.zzu.edu.cn/zb/2.htm',
    ):
        host = host_of(url)
        assert is_marketplace_or_aggregator(host) is False, url
        assert source_publication_tier(fetched(url, award_table)) == LOCAL_AUTHORITY, url
        assert source_publisher_tier(fetched(url, award_table)) == LOCAL_AUTHORITY, url
        assert classify_source(url, '中标结果公告') is SourceType.TENDER_DOCUMENT, url


# 竞采星 / 全国高校竞价网与广东教采云的成交详情：字段形状取自实站记录
# （``设计规范`` §2 结论表一、``设计规范`` §5.5），正文干净到能在**未登记**的
# 主机上自己通过 ``publication_tier`` —— 这正是需要域名登记的原因。
EASYJCX_AWARD_BODY = (
    '全国高校竞价网 竞采星\n'
    '某某大学生物安全柜采购项目成交结果公告\n'
    '项目编号：JCX-2026-0812\n'
    '采购单位：某某大学\n'
    '成交供应商：某某生物科技有限公司\n'
    '成交金额：人民币 99,000.00 元\n'
    '公告日期：2026年09月18日\n'
    '设备名称 | 品牌 | 型号 | 数量 | 单价（元）\n'
    '生物安全柜 | 赛默飞 | 1574 | 1 | 99000\n'
)
GDEDULSCG_AWARD_BODY = (
    '广东教采云零散采购竞价平台\n'
    '某某大学离心机采购项目成交结果公告\n'
    '项目编号：JCY-2026-60413\n'
    '采购人：某某大学\n'
    '成交供应商：某某仪器有限公司\n'
    '成交金额：人民币 39,780.00 元\n'
    '公告日期：2026年09月18日\n'
    '报价情况\n'
    '生产厂商/品牌 | 型号规格 | 数量 | 单价（元）\n'
    '湖南湘仪 | TG16-WS | 1 | 39780\n'
)

#: 登记依据的域名 → 实站 URL 形状（``设计规范`` 记的列表/详情路径）。
NEWLY_REGISTERED_AGGREGATORS = (
    ('easyjcx.com', 'www.easyjcx.com', 'https://www.easyjcx.com/#/purchase/detail/812',
     'api.easyjcx.com', EASYJCX_AWARD_BODY),
    ('gdedulscg.cn', 'www.gdedulscg.cn',
     'https://www.gdedulscg.cn/home/bill/billdetails/billGuid/60413.html',
     'gdedulscg.cn', GDEDULSCG_AWARD_BODY),
)


def test_the_two_compliance_aggregators_are_registered_and_never_become_publications():
    """竞采星与教采云登记进 ``AGGREGATOR_HOST_MARKERS``（用户 2026-09-25 合规决定）。

    两站页脚都有禁止使用其数据的条款（教采云「未经本平台授权，严禁以任何形式转载或
    使用本平台数据」，竞采星同类，见 ``设计规范`` / ``设计规范`` / ``设计规范``），
    因此都不做直连数据源。用户的处置不是拉黑而是**当聚合站用**——页面照旧可以被搜到，
    用来发现项目名称，价格回到官方原文读。所以登记的判据是"拿不到 publisher/
    publication tier"，而不是"搜不到"。
    """
    for marker, host, url, bare_host, body in NEWLY_REGISTERED_AGGREGATORS:
        assert marker in AGGREGATOR_HOST_MARKERS, marker
        # 子串匹配：裸域、www. 与任何子域都要覆盖（教采云实站只有带 www. 的证书是对的，
        # 裸域同样是这台主机；竞采星的接口在 api. 子域上）。
        for spelling in (bare_host, host):
            assert is_marketplace_or_aggregator(spelling) is True, spelling

        source = fetched(url, body)
        assert source_publication_tier(source) is None, url
        assert source_publisher_tier(source) is None, url
        assert is_tender(source) is False, url
        # 仍会被搜到，只是被认成聚合站：85 分/历史成交 → 25 分/商业线索，排序下移。
        assert classify_source(url, '成交结果公告') is SourceType.AGGREGATOR, url
        assert authority(SourceType.AGGREGATOR)[0] == 25, url

    # 同一段正文落在**未登记**的第三方主机上仍然按机构自己发布的成交公告处理——
    # 挡它的是域名登记，不是正文恰好不合格（否则这两条登记就是假生效）。
    for _, _, _, _, body in NEWLY_REGISTERED_AGGREGATORS:
        assert source_publication_tier(
            fetched('https://www.example-zb.com/news-x', body)) == FORMAL_PUBLICATION
        assert source_publisher_tier(
            fetched('https://www.example-zb.com/news-x', body)) == FORMAL_PUBLICATION


#: chinamae 详情页的正文形状取自 ``设计规范`` §第 1 级实测：站点在服务端把 7 个字段
#: 打成 ``******``，但公告标题里的成交字样是完整的——不登记的话，这段文字在
#: ``.com`` 主机上能自己拿到 ``publication_tier``（``设计规范`` 就是这么记的）。
CHINAMAE_DETAIL_BODY = (
    '巴彦县质量技术监督检验检测中心农产品质量安全检测项目仪器设备购置中标（成交）结果公告\n'
    '项目编号：******\n'
    '采购人：******\n'
    '中标供应商：******\n'
    '中标金额：******\n'
    '规格型号：******\n'
    '单价：******\n'
    '公告日期：2026年07月28日\n'
)


def test_chinamae_is_registered_as_an_aggregator_so_it_can_never_be_a_publication():
    """接成发现渠道的同一步，把 chinamae 登记成聚合站。

    为什么用它做发现：列表页是静态 HTML，能拿到**干净的项目名**（``设计规范`` §第 1 级），
    所以本单把它接成了 ``AGGREGATOR_CHINAMAE``。为什么必须同时登记成聚合站：它仍然是
    转载聚合站——详情页 7 个字段被服务端打成 ``******``。登记的后果与 120shop / 采招网
    完全一样（拿不到 publisher/publication tier、``classify_source`` 认成 AGGREGATOR、
    排序下移），**不丢弃**：页面照旧可以被搜到，价格一律回到官方原文读。
    """
    assert 'chinamae.com' in AGGREGATOR_HOST_MARKERS
    # 子串匹配：裸域、www. 与任何子域都要覆盖。
    for spelling in ('chinamae.com', 'www.chinamae.com'):
        assert is_marketplace_or_aggregator(spelling) is True, spelling

    url = 'https://www.chinamae.com/purchases/f009785bc9c14ed1b18be98ec2bba623.html'
    source = fetched(url, CHINAMAE_DETAIL_BODY)
    assert source_publication_tier(source) is None, url
    assert source_publisher_tier(source) is None, url
    assert is_tender(source) is False, url
    assert classify_source(url, '中标（成交）结果公告') is SourceType.AGGREGATOR, url
    assert authority(SourceType.AGGREGATOR)[0] == 25, url

    # 同一段正文落在**未登记**的主机上仍然按机构自己发布的成交公告处理——
    # 挡它的是域名登记，不是正文恰好不合格（否则这条登记就是假生效）。
    assert source_publication_tier(
        fetched('https://www.example-zb.com/news-x', CHINAMAE_DETAIL_BODY)) == FORMAL_PUBLICATION


def test_a_registered_aggregator_body_never_reaches_the_price_lane():
    """不联网把链路走通：登记 → 无 tier → ``prices()`` 入口判据拒收。

    ``prices()`` 的三条入口判据由 ``price_source_candidates`` 复现（fetched /
    价格来源类型 / 正文含目标型号）。三个都构造成立，只有**类型**这一条因为域名登记
    变成 ``aggregator``，所以拒收码必须是 ``source_type_not_price_source``——
    这样"聚合站进不了价格通道"才是数据结构的结论，而不是调用方的自觉。
    """
    url, body = ('https://www.gdedulscg.cn/home/bill/billdetails/billGuid/60413.html',
                 GDEDULSCG_AWARD_BODY)
    target = {'brand': '湘仪', 'model': 'TG16-WS'}
    source = fetched(url, body)
    source['source_type'] = classify_source(url, '成交结果公告').value
    assert source['source_type'] == 'aggregator'
    assert source_publication_tier(source) is None
    # 目标型号确实在正文里（否则拒收理由会变成型号不匹配，这条用例就证明不了登记）。
    assert contains_model(body, 'TG16-WS') is True

    selected, trace = price_source_candidates([source], target)
    assert selected == []
    assert [item['skip_reason'] for item in trace] == [SKIP_TYPE_NOT_PRICE_SOURCE]

    # 同一份正文 + 同一型号，只把主机换成未登记的一方，就重新进得了价格通道——
    # 这是登记生效的反证，也说明拒收不是"这份正文本来就过不了"。
    control = dict(source, url='https://www.example-zb.com/60413.html', source_type='tender_document')
    selected, trace = price_source_candidates([control], target)
    assert trace == []
    assert [item['url'] for item in selected] == ['https://www.example-zb.com/60413.html']


def test_registering_the_two_aggregators_does_not_touch_8093_or_official_hosts():
    """反向检查：8093（小零易购）与中国教育装备网、ccgp 一个都不能被误伤。

    用户这次的重点是"不能误伤 8093 和官方站"。``gdegp.gds.edu.cn`` 与
    ``gdedulscg.cn`` 是**两个不同**的广东平台（``设计规范`` 明确「"教采云" 与
    "小零易购" 是两个系统」），子串登记必须证明没有把它们混在一起；``ceiea.com``
    是机构自建站，正文本就干净，也不该被这次登记连带降级。
    """
    award_table = ('采购合同\n项目编号：AH-2026-0001\n采购人：某某市第一人民医院\n'
                   '中标供应商：某某医疗器械有限公司\n中标金额：1500000元\n'
                   '评审专家：张三、李四\n合同金额：1500000元\n'
                   '公告日期：2026年09月18日\n'
                   '离心机 | 湘仪 | TG16-WS | 1 | 39780\n')
    for host in ('gdegp.gds.edu.cn', 'www.gdegp.gds.edu.cn',
                 'www.ceiea.com', 'www.ccgp.gov.cn'):
        assert is_marketplace_or_aggregator(host) is False, host

    # 8093 是 .edu.cn：本地权威分支先返回，登记与否都不影响它。
    for url in ('https://gdegp.gds.edu.cn:8093/orderNotice/detail?id=1',
                'https://gdegp.gds.edu.cn/sci/notice/award.htm'):
        source = fetched(url, award_table)
        assert source_publication_tier(source) == LOCAL_AUTHORITY, url
        assert source_publisher_tier(source) == LOCAL_AUTHORITY, url
        assert classify_source(url, '中标结果公告') is SourceType.TENDER_DOCUMENT, url
        selected, trace = price_source_candidates(
            [dict(source, source_type='tender_document')], {'model': 'TG16-WS'})
        assert trace == [], url

    # 中国教育装备网：商业域 + 干净成交正文，登记这两个域名**不**改变它的等级。
    ceiea = fetched('https://www.ceiea.com/html/202609/award-1.shtml', award_table)
    assert source_publication_tier(ceiea) == FORMAL_PUBLICATION
    assert source_publisher_tier(ceiea) == FORMAL_PUBLICATION

    # ccgp 是官方渠道。
    ccgp = fetched('http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202609/t1.htm', award_table)
    assert source_publication_tier(ccgp) == LOCAL_AUTHORITY
    assert classify_source('http://www.ccgp.gov.cn/cggg/dfgg/qtgg/202609/t1.htm',
                           '其他公告') is SourceType.TENDER_DOCUMENT


def test_the_two_registered_aggregators_are_unlisted_in_every_consumer():
    """把 ``is_marketplace_or_aggregator`` 的其余三个调用点一次钉住。

    登记一个域名会同时改三处判定，本用例逐处确认方向都是"当商业线索、不当证据"：
    ``source_display_authority``（有官方成交记录时不列入来源清单 → ``unlisted``）、
    ``lead_alignment.authority_component``（权威分量 0.05 而不是 0.35）、
    ``procurement_research._mentions_unlisted``（报告正文里的这类地址列为"不列出"）。
    登记时还有第四个调用点——``product_configuration`` 的配置来源闸门——**已于
    2026-09-26 按业务口径撤销**（「如果是取配置的来源 可以算 如果是取价格的来源
    不能算」，理由见 ``product_configuration.py`` 模块 docstring「第 4 条为什么
    撤销」），所以本用例不再覆盖它，下面的断言只直接钉 ``is_marketplace_or_aggregator``
    本身仍为真（价格通道还在用它）。**没有一处是"从结果里丢弃"**。
    """
    from backend.services.lead_alignment import AUTHORITY_MARKETPLACE, authority_component
    from backend.services.procurement_research import _mentions_unlisted

    for _marker, host, url, _, body in NEWLY_REGISTERED_AGGREGATORS:
        source = fetched(url, body)
        assert source_display_authority(source, brands=('湘仪',)) == 'unlisted', url
        assert authority_component(source) == AUTHORITY_MARKETPLACE, url
        assert _mentions_unlisted(f'参考来源：{url}', set()) is True, url
        assert is_marketplace_or_aggregator(host_of(url)) is True, url

    # 对照：同一个函数对官方站仍是"官方渠道"，不是"不列出"。
    official = fetched('https://gzc.wmu.edu.cn/info/1234/5678.htm', '采购合同\n项目编号：X\n')
    assert source_display_authority(official) == 'official_channel'
    assert authority_component(official) != AUTHORITY_MARKETPLACE


def test_a_dealer_price_page_never_becomes_price_evidence():
    body = ('某某仪器经销有限公司\n'
            '知楚A2 生物安全柜 现货\n'
            '价格：面议\n'
            '成交公告参考价仅供参考\n'
            '项目编号：D1\n采购人：某公司\n成交供应商：某公司\n')
    assert not is_tender(fetched('https://www.example-dealer.com/goods/a2', body))
    # The same quoted notice on a shop page, with the seller voice only.
    shop = AGENCY_AWARD.replace('某某招标代理有限公司', '某某仪器商城') + '\n现货 加入购物车\n'
    assert not is_tender(fetched('https://www.example-shop.com/goods/a2', shop))


def test_an_inquiry_result_notice_is_still_a_concluded_decision():
    """``询价结果公告`` is institutional wording, not a seller signal."""
    body = AGENCY_AWARD.replace('中标（成交）结果公告', '询价结果公告')
    assert is_tender(fetched('https://www.example-zb.com/x', body))


@pytest.mark.parametrize('heading', [
    '中标（成交）结果公告', '中标结果公告', '成交结果公告', '采购结果公告',
    '询价结果公告', '竞争性谈判结果公告', '竞争性磋商结果公告', '单一来源采购结果公告',
    '比选结果公告', '竞价结果公告', '中标候选人公示', '评标结果公示',
])
def test_every_result_notice_family_is_recognised(heading):
    body = AGENCY_AWARD.replace('中标（成交）结果公告', heading)
    assert is_tender(fetched('https://www.example-zb.com/x', body)), heading


@pytest.mark.parametrize('heading', [
    '招标公告', '询价公告', '竞争性谈判公告', '竞争性磋商公告', '单一来源采购公示',
    '采购意向公开', '资格预审公告', '更正公告',
])
def test_every_solicitation_family_is_rejected(heading):
    """A solicitation states no concluded price, whatever its structure."""
    body = AGENCY_AWARD.replace('中标（成交）结果公告', heading)
    assert not is_tender(fetched('https://www.example-zb.com/x', body)), heading


def test_unfetched_bodies_never_count_regardless_of_host():
    for status in (None, 'failed', 'unavailable', 'skeleton'):
        source = {'url': 'https://www.example-zb.com/a', 'body_status': status, 'body_text': AGENCY_AWARD}
        assert source_publication_tier(source) is None
        assert source_publisher_tier(source) is None
        assert not is_tender(source)


def test_malformed_input_is_rejected_without_raising():
    assert publication_tier(None, AGENCY_AWARD) is None
    assert publication_tier('www.example-zb.com', None) is None
    assert publication_tier('www.example-zb.com', '   ') is None
    assert not is_tender({'url': 'https://[bad', 'body_status': 'fetched', 'body_text': AGENCY_AWARD})
    assert not is_tender({'body_status': 'fetched', 'body_text': AGENCY_AWARD})
    assert not is_tender(None)
    assert source_publication_tier('not-a-dict') is None


# --------------------------------------------------------------------------
# 展示权威性：有官方成交记录时，来源清单里留哪些
# --------------------------------------------------------------------------
#
# 这一组用例的域名与正文不是编的：``task-013dcdcff7e6`` 的真实来源清单里就有
# 下面每一个域名，正文状态也照实（``caizhaowang.com`` / ``dlnyzb.com`` /
# ``shihuantong.com`` 三个聚合站一条正文都没抓到，所以只能靠域名登记）。
# 反向的对照同样来自真实交付：``xiangyilxj.com`` 的版权方是「湖南湘仪实验室仪器
# 开发有限公司」（品牌在用的「湘仪」），``xiangyiyt.com`` 是「上海仪天科学仪器
# 有限公司」——两家都卖同类仪器，只有前者是品牌方自己的站点。

CENTRIFUGE_AWARD = (
    '某某招标代理有限公司\n'
    '某某市第一人民医院医疗设备采购项目中标（成交）结果公告\n'
    '项目编号：XYZB-2026-0417\n'
    '采购人：某某市第一人民医院\n'
    '成交供应商：某某医疗器械有限公司\n'
    '中标金额：人民币 236,000.00 元\n'
    '公告日期：2026年09月18日\n'
    '设备名称 | 设备品牌 | 设备型号 | 数量 | 单价（元）\n'
    '离心机 | 湘仪 | XKA-2200 | 2 | 17,480\n'
)


@pytest.mark.parametrize('url', [
    'http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202603/t20260325_26313581.htm',
    'http://www.ccgp-shaanxi.gov.cn/x/a.htm',
    'http://ztb.usx.edu.cn/a.htm',
    'http://ggzy.haikou.gov.cn/a.htm',
])
def test_official_channels_stay_in_the_source_list(url):
    assert source_display_authority(fetched(url, CENTRIFUGE_AWARD)) == 'official_channel', url


def test_a_government_procurement_platform_without_the_gov_suffix_is_claimed_by_its_body():
    """``gpx.zfcg.scsczt.cn``（四川政府采购一体化平台）没有 ``gov.cn`` 后缀。

    域名判据不认业务缩写（见下面那条用例），所以它靠**正文**被认领：这一轮抓到的
    是一份正式成交公告。真实数据上它确实留在来源清单里（``task-013dcdcff7e6``，
    27 个保留域名之一）；没抓到正文时它就得按未核实处理，不能只凭域名放行。
    """
    assert source_display_authority(fetched('https://gpx.zfcg.scsczt.cn/a', CENTRIFUGE_AWARD)) == 'official_channel'
    assert source_display_authority({'url': 'https://gpx.zfcg.scsczt.cn/a', 'body_status': 'unavailable'}) == 'unlisted'


@pytest.mark.parametrize('url', [
    'https://www.120shop.cn/i890850380',
    'https://www.yiliaozhaobiao.com/news-38fc41b1dcc44392891a1324a0acdbb7',
    'https://www.caizhaowang.com/a',
    'http://www.dlnyzb.com/a',
    'http://www.shihuantong.com/a',
    'https://www.yiqi.com/product/detail_1690891.html',
    'https://www.antpedia.com/a',
    'https://www.instrument.com.cn/a',
    'https://www.ixbang.com/a',
    'https://zh.kindle-tech.com/a',
    'https://www.ccen.net/a',
])
def test_commercial_lead_lanes_are_unlisted(url):
    # 没抓到正文的商业线索页：判据只能落到域名登记与官方渠道上。
    assert source_display_authority({'url': url, 'body_status': 'unavailable'}) == 'unlisted', url


def test_an_aggregator_is_unlisted_no_matter_how_clean_its_body_is():
    """正文判据拦不住聚合站：它的列表页带着成交措辞与结构字段。

    所以判定顺序是"先域名、后正文"：同一段正文放在一个未登记的 ``*.com`` 机构站上
    会被当成正式公告，放在聚合站域名上就一条都不是。反过来（先看正文）会让一个抓干
    净的转载页冒充官方渠道。
    """
    body = CENTRIFUGE_AWARD
    unregistered_institution = fetched('https://www.example-zb.com/a', body)
    aggregator = fetched('https://www.yiliaozhaobiao.com/news-x', body)
    assert source_publication_tier(unregistered_institution) == FORMAL_PUBLICATION, '前提：这段正文本身够格'
    assert source_display_authority(unregistered_institution) == 'official_channel'
    assert source_publication_tier(aggregator) is None, '登记的聚合站拿不到 publication tier'
    assert source_display_authority(aggregator) == 'unlisted'


def test_official_channel_judgement_is_a_label_match_not_a_substring_guess():
    """判据是整段域名标签，不是"域名里含 gov 就算官方"。

    真实数据的分类不变（``gpx.zfcg.scsczt.cn`` 仍留），但留下它的**理由**不是
    "含 ``zfcg`` 标签"——那个理由对任何站点都成立。这里钉住的是：整段标签
    ``gov`` / ``gob`` / ``edu`` 成立，把它们当子串用不成立。
    """
    assert source_display_authority({'url': 'https://gov.example.com/a'}) == 'official_channel'
    assert source_display_authority({'url': 'https://www.gov2.example.cn/a'}) == 'official_channel'
    assert source_display_authority({'url': 'https://mygov.example.com/a'}) == 'unlisted', '子串不算'
    assert source_display_authority({'url': 'https://government.example.com/a'}) == 'unlisted', '子串不算'
    # ``zfcg`` / ``ggzy`` 这类业务缩写**不是**证据，正式公告靠正文判据认领。
    assert source_display_authority({'url': 'https://www.zfcg.example.com/a'}) == 'unlisted'
    assert source_display_authority({'url': 'https://ggzy.example.com/a'}) == 'unlisted'


def test_a_manufacturer_domain_is_kept_for_parameters():
    assert source_display_authority({'url': 'https://www.beckman.com/x', 'source_type': 'manufacturer_website'}) == 'manufacturer'
    assert source_display_authority({'url': 'https://www.example.com/x', 'source_type': 'manufacturer_download'}) == 'manufacturer'
    assert source_display_authority({'url': 'https://www.example.com/x', 'publisher_evidence': {'brand': '知楚'}}) == 'manufacturer'


def test_the_copyright_owner_decides_between_two_lookalike_dealer_domains():
    """同一批交付里两个最像的域名：一个有版权方公司名，一个没有。"""
    brands = ['湘仪']
    own = fetched('https://www.xiangyilxj.com/a', '版权所有 © 湖南湘仪实验室仪器开发有限公司')
    third_party = fetched('https://www.xiangyiyt.com/a', '版权所有 © 上海仪天科学仪器有限公司')
    assert source_display_authority(own, brands) == 'manufacturer'
    assert source_display_authority(third_party, brands) == 'unlisted'
    # 换个品牌名，结论跟着换——判据读的是本轮在用的品牌，不是域名像不像。
    assert source_display_authority(third_party, ['仪天']) == 'manufacturer'
    assert source_display_authority(own, ['仪天']) == 'unlisted'


def test_a_known_brand_alias_maps_a_pinyin_domain():
    """品牌名是中文、官网域名是英文时的那一步：品牌→域名词的已知映射。"""
    assert source_display_authority({'url': 'https://www.beckman.com/x'}, ['贝克曼库尔特']) == 'manufacturer'
    assert source_display_authority({'url': 'https://www.mybeckman.cn/x'}, ['贝克曼库尔特']) == 'manufacturer'
    # 没有映射、域名里也没有品牌词 → 不猜。
    assert source_display_authority({'url': 'https://www.kaidasy.com/x'}, ['湖南凯达']) == 'unlisted'
    assert source_display_authority(fetched('https://www.kaidalab.com/x', '版权所有 © 湖南凯达科学仪器有限公司'), ['湖南凯达']) == 'manufacturer'


def test_display_authority_never_raises_on_junk():
    for junk in (None, 'not-a-dict', {}, {'url': None}, {'url': 'https://[bad'}, {'url': 'https://x.com/a', 'body_text': 42}):
        assert source_display_authority(junk, ['湘仪']) == 'unlisted'
