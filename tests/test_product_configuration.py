"""产品参数页：与采购需求并列、但性质完全不同的第二条证据。

红线只有一条：**采购需求绝不能进 ``configuration``**。招标文件里
「▲5.最高转速：≥4500r/min」是采购方对投标产品开出的门槛，把它印成 XKA-2200 的
标称参数就是凭空捏造一台机器。

这里同时钉住最容易做错的地方：任务书里那条「正文含型号 + ``configuration_regions``
能取到窗口」的判据**单独用是不够的**——实测那条 ccgp 中标公告（4456 字）正文里
既有 XKA-2200，也命中配置类标题「主要标的」，两个条件都成立。所以判据里必须有
「这份来源不是采购公告、不是招标文件」和「窗口自己写着这个型号」这两条。
"""
from pathlib import Path

import pytest

from backend.services.procurement_research import (
    CONFIGURATION_HEADINGS, configuration_regions, price_term_text)
from backend.services.procurement_rules import contains_model
from backend.services.product_configuration import (
    MANUFACTURER_SOURCE, PRODUCT_SOURCE, PRODUCT_SPEC_HEADINGS, REPORT_CHARACTER_LIMIT,
    apply_product_configuration, clean_page_text, product_configuration,
    report_configuration, strip_page_chrome, without_repeated_table_projection)
#: 第 5 条第二种形态的判据本身（私有）：这里直接钉它，是因为要走 ``product_configuration``
#: 就得先过"不是公告也不是招标文件"那一条，测不出结构判据单独的行为。
from backend.services.product_configuration import (  # noqa: E402 - 见上
    _spec_field_rows, _spec_field_table, _spec_headings, _spec_measure_rows)
from backend.services.web_tables import render_table
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture

FIXTURES = Path(__file__).parent / 'fixtures' / 'notice-fields'

MODEL = 'XKA-2200'
BRAND = '湘仪'
MANUFACTURER_URL = 'https://www.xiangyilxj.com/product/info_itemid_952.html'
SHOWCASE_URL = 'http://www.xiangyiyt.com/cp-view/203'
NOTICE_URL = 'http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202603/t20260325_26313581.htm'

#: 招标文件里的一条**采购要求**：带 ▲、带「≥」。参数页上永远不会有这种写法。
CLAUSE = '▲5.最高转速：≥4500r/min。'

XKA_EXCERPT = '品牌：湘仪\n规格型号：XKA-2200\n数量：1\n单价(元)：17480'

MANUFACTURER_BODY = '\n'.join([
    '免疫血液离心机XKA-2200-湘仪集团',
    '网站首页 [http://www.xiangyilxj.com/] 产品中心 [http://www.xiangyilxj.com/product]',
    '技术参数',
    '型号 | XKA-2200',
    '订货号 | 0205202000',
    '最高转速 | 4500r/min',
    '最大相对离心力 | 2000×g',
    '转子容量 | (0.25～1ml)×12',
    '转速精度 | ±30r/min',
    '整机功率 | 100W',
    '外形尺寸 | 456×330×266mm(L×W×H)',
    '重量 | 12kg',
    '转子参数',
    '红血球清洗用转子/SERO | 4 | 500xg | 60s | 血型判定，血球凝集反映观察',
    '淋巴球清洗用转子/HLA | 1 | 2000xg | 180s | 淋巴球分离',
    'Copyright © 湖南湘仪实验室仪器开发有限公司 All Rights Reserved 湘ICP备19023861号-1',
])

#: 现在**写进价格记录**的是压过一遍的正文：``product_configuration`` 写出去的
#: ``text`` 就是判据认的那份（``compressed_configuration``／宽表的按列结果），不再是
#: 窗口原文。比上面那份少三行——产品标题行、导航行（``网站首页 [url] 产品中心 [url]``）
#: 与版权页脚：它们一个字也不是这台机器的参数。
#: 原来写窗口原文，页面的导航与推荐位就跟着进配置；实测 ``yiqi.com`` HVE-50 那页
#: （``product/detail_1867343.html``）两个 ``find_ulbox``（分类导航 + 推荐商品）被拼成
#: 1979 字写进 9 条价格共用的配置，里面是「耐磨擦试验机｜现代环境」这类无关商品。
MANUFACTURER_SPEC = '\n'.join([
    '技术参数',
    '型号 | XKA-2200',
    '订货号 | 0205202000',
    '最高转速 | 4500r/min',
    '最大相对离心力 | 2000×g',
    '转子容量 | (0.25～1ml)×12',
    '转速精度 | ±30r/min',
    '整机功率 | 100W',
    '外形尺寸 | 456×330×266mm(L×W×H)',
    '重量 | 12kg',
    '红血球清洗用转子/SERO | 4 | 500xg | 60s | 血型判定，血球凝集反映观察',
    '淋巴球清洗用转子/HLA | 1 | 2000xg | 180s | 淋巴球分离',
])

#: 第三方展示平台（上海仪天科学仪器有限公司）的同一型号页面：型号相同、公司名里
#: 没有「湘仪」。它的尺寸与重量与厂家官网不一致（330×375×250mm / 16kg），正是
#: 「出处必须一起印出来」的理由。
SHOWCASE_BODY = '\n'.join([
    '湖南湘仪免疫血液离心机XKA-2200/XKA2200',
    '欢迎访问仪天产品展示平台-湘仪离心机',
    '技术参数：',
    '型号 | XKA-2200 | XKA-2200',
    '最高转速 | 4500/min | 4500/min',
    '外形尺寸 | 330×375×250mm(L×W×H) | 330×375×250mm(L×W×H)',
    '重量 | 16kg | 16kg',
    'Copyright © 2012 - 上海仪天科学仪器有限公司 All Rights Reserved',
])

#: 聚合页（仪器网产品详情页）的正文：**分类导航**与**推荐商品**两个
#: ``<ul class="find_ulbox">`` 在正文里的样子。结构照抄快照
#: ``output/e2e-final/tmp-yiqi-structure.txt``（``yiqi.com/product/detail_1867343.html``，
#: HVE-50）。它脏在两处，两处都不是这台机器的参数：
#:   ① 面包屑那一行里带 ``|``（``… > 高压灭菌器（仅限上海销售）||HVE-50|Hirayama``），
#:      按 ``|`` 切开"≥2 格像型号、其中一格正是目标型号"，被当成宽表表头；
#:   ② 表头下面紧跟的是分类导航（``金属浴``／``光学设备``）与推荐商品
#:      （``耐磨擦试验机 | ART | 现代环境 [url]``）。
AGGREGATOR_URL = 'https://www.yiqi.com/product/detail_1867343.html'
AGGREGATOR_PAGE = '\n'.join([
    '「高压灭菌器（仅限上海销售）||HVE-50|Hirayama」价格、参数、图片 - 仪器网',
    '上海来铂生物集团有限公司 [https://www.yiqi.com/zt79008/] > 清洗·净化 '
    '[https://www.yiqi.com/zt79008/sort_92992.html] > 灭菌器 '
    '[https://www.yiqi.com/zt79008/sort_93043.html] > 高压灭菌器（仅限上海销售）||HVE-50|Hirayama',
    '金属浴',
    '光学设备',
    '灭菌器',
    '氮吹仪',
    '高压灭菌器（仅限上海销售） | HVE-50',
    '高压灭菌器（仅限上海销售） | HVA-85',
    '耐磨擦试验机 | ART | 现代环境 [https://www.yiqi.com/product/detail_1875352.html]',
    '硅表维护套件 | 6788305 | Hach/哈希 [https://www.yiqi.com/product/detail_1874650.html]',
    '9. HVE-50上限有效深度：710mm，灭菌腔内可放置三个灭菌提篮以便更好的分类样品。',
    '技术参数：',
    '高压灭菌器（仅限上海销售）||HVE-50|Hirayama多少钱一台？',
    '有高压灭菌器（仅限上海销售）||HVE-50|Hirayama详细技术参数吗？',
])

#: 招标文件正文：既写着这个型号，又写着那些采购要求。类型为 ``tender_document``
#: 的来源就是这样一份文书。
TENDER_BODY = '\n'.join([
    '某某医院医疗设备采购项目 公开招标文件',
    '项目编号：TEST-2026-001',
    '采购人：某某医院',
    '第四章 采购需求',
    '4包 技术参数要求',
    '一、血型血清学离心机',
    '▲1.专用红血球清洗/SERO 转子，微机控制，液晶显示。',
    CLAUSE,
    '参考型号：XKA-2200',
    '二、采购设备清单',
    '序号 | 设备名称 | 数量 | 预算单价',
    '1 | 血型血清学离心机 | 1 | 20000',
])


#: 真实页面窗口（2400 字 / 74 行）——隔离库
#: ``output/bio-instrument-acceptance-20260920/browser-runtime/acceptance.sqlite3`` 里
#: ``task-013dcdcff7e6`` 那条研究记录 ``prices[0].configuration`` 的**全文**，
#: 与前端 ``frontend/tests/fixtures/xka-2200-manufacturer-spec.txt`` 逐字相同。
#: 它是 ``configuration_regions`` 从 ``xiangyilxj.com/product/info_itemid_952.html``
#: （16091 字 / 414 行）里取出的那一段，脏在两处，而且两处都不在参数本身：
#:   ① 第 14-23 行是同一张表的第二种写法（``web_tables.render_table`` 追加的
#:      ``[表格按列展开 …]`` 投影），与第 3-13 行逐格重复；
#:   ② 第 33 行起是页面的留言表单与「相关推荐」轮播（产品名 + 裸链接）。
#: 第 70-73 行是抓取正文里的窗口尾部（正文本身在 414 行处结束，所以这里被截断）。
REAL_PAGE_WINDOW = '\n'.join([
    '技术参数',
    '型号 | XKA-2200',
    '订货号 | 0205202000',
    '最高转速 | 4500r/min',
    '最大相对离心力 | 2000×g',
    '转子容量 | (0.25～1ml)×12',
    '转速精度 | ±30r/min',
    '定时范围 | 1min~99min',
    '整机噪声 | 最低 ≤58dB(A)',
    '电源 | AC220V ±22V 50/60Hz',
    '整机功率 | 100W',
    '外形尺寸 | 456×330×266mm(L×W×H)',
    '包装尺寸 | 568×423×300mm(L×W×H)',
    '重量 | 12kg',
    '[表格按列展开 行3列2] XKA-2200 最高转速 4500r/min',
    '[表格按列展开 行4列2] XKA-2200 最大相对离心力 2000×g',
    '[表格按列展开 行6列2] XKA-2200 转速精度 ±30r/min',
    '[表格按列展开 行7列2] XKA-2200 定时范围 1min~99min',
    '[表格按列展开 行8列2] XKA-2200 整机噪声 最低 ≤58dB(A)',
    '[表格按列展开 行9列2] XKA-2200 电源 AC220V ±22V 50/60Hz',
    '[表格按列展开 行10列2] XKA-2200 整机功率 100W',
    '[表格按列展开 行11列2] XKA-2200 外形尺寸 456×330×266mm(L×W×H)',
    '[表格按列展开 行12列2] XKA-2200 包装尺寸 568×423×300mm(L×W×H)',
    '[表格按列展开 行13列2] XKA-2200 重量 12kg',
    '转子参数',
    '转子名称 | 转子号 | 离心力(xg) | 离心时间(s) | 用途',
    '淋巴球清洗用转子/HLA | 1 | 2000xg | 180s | 淋巴球分离,培养细胞分离',
    '淋巴球清洗用转子/HLA | 2 | 1000xg | 3s | 血小板的去除(凝血酶处理)',
    '淋巴球清洗用转子/HLA | 3 | 1000xg | 60s | 淋巴球的清洗',
    '红血球清洗用转子/SERO | 4 | 500xg | 60s | 血型判定，血球凝集反映观察',
    '红血球清洗用转子/SERO | 5 | 1000xg | 15s | 交叉适合性试验，抗球素试验',
    '红血球清洗用转子/SERO | 6 | 1000xg | 60s | 血球清洗、血清、血浆的提取',
    '*所有产品都可根据客户要求定制、设计、生产。',
    '留言板',
    '发 送',
    '相关推荐',
    'H300R大容量高速冷冻离心机',
    '[https://www.xiangyilxj.com/product/info_itemid_2261.html]',
    'CHT200R大容量高速台式冷冻离心机',
    '[https://www.xiangyilxj.com/product/info_itemid_2260.html]',
    '大容量高速台式冷冻离心机CHT230R',
    '[https://www.xiangyilxj.com/product/info_itemid_2258.html]',
    'RM32R 低速冷冻离心机',
    '[https://www.xiangyilxj.com/product/info_itemid_2241.html]',
    'RM32 低速台式离心机',
    '[https://www.xiangyilxj.com/product/info_itemid_2240.html]',
    '低速台式离心机PRP200（牙科便携式）',
    '[https://www.xiangyilxj.com/product/info_itemid_2239.html]',
    'HT200高速台式离心机',
    '[https://www.xiangyilxj.com/product/info_itemid_2219.html]',
    'HT120高速台式离心机',
    '[https://www.xiangyilxj.com/product/info_itemid_2218.html]',
    '大容量高速台式冷冻离心机CHT220R',
    '[https://www.xiangyilxj.com/product/info_itemid_2215.html]',
    'LT65低速台式离心机',
    '[https://www.xiangyilxj.com/product/info_itemid_2214.html]',
    'HT300R高速台式冷冻离心机',
    '[https://www.xiangyilxj.com/product/info_itemid_2200.html]',
    'HT230R高速台式冷冻离心机',
    '[https://www.xiangyilxj.com/product/info_itemid_2199.html]',
    'CHT160R大容量高速台式冷冻离心机',
    '[https://www.xiangyilxj.com/product/info_itemid_2195.html]',
    'LT60低速台式离心机',
    '[https://www.xiangyilxj.com/product/info_itemid_2194.html]',
    'CH300R大容量高速冷冻离心机',
    '[https://www.xiangyilxj.com/product/info_itemid_2182.html]',
    '大容量高速离心机CH210',
    '[https://www.xiangyilxj.com/product/info_itemid_2140.html]',
    'CL55R/CL55大容量低速冷冻/常温离心机',
    '[https://www.xiangyilxj.com/product/info_itemid_2139.html]',
    '大容量高速冷冻离心机CH100R',
    '[https://www.xiangyilxj.com/product/info_itemid_2136.html]',
    'HT200高速台式离心机',
    '[https://www.xian',
])

#: 多型号表：``render_table`` 会把每一格投影一条。值不同（4500/min 对 4000/min），
#: 因此这 6 条**一条都不该删**——删了就真丢了另一列的值。
MULTI_MODEL_TABLE_HTML = (
    '<table>'
    '<tr><th>型号</th><th>XKA-2200</th><th>XKA-2200</th></tr>'
    '<tr><td>最高转速</td><td>4500/min</td><td>4000/min</td></tr>'
    '<tr><td>最大相对离心力</td><td>2000xg</td><td>1800xg</td></tr>'
    '<tr><td>外形尺寸</td><td>330x375x250mm</td><td>300x350x240mm</td></tr>'
    '</table>')


def source(url, body, source_type='unknown', **extra):
    return {'url': url, 'body_status': 'fetched', 'source_type': source_type,
            'body_text': body, **extra}


def notice(name):
    return (FIXTURES / name).read_text(encoding='utf-8')


def _extractor(service, extraction):
    class Client:
        endpoint = 'test://product-configuration'
        model = 'offline'
        last_usage = None

        def analyze(self, **kwargs):
            return {'prices': [] if extraction is None else [dict(extraction)]}

    service.client_factory = Client


def _extraction(**overrides):
    row = {'brand': BRAND, 'model': MODEL, 'procurement_unit': '', 'configuration': 'unknown',
           'kind': 'award', 'amount': 17480, 'amount_unit': '元', 'amount_scope': 'unit',
           'quantity': 1, 'currency': 'CNY', 'date': '2026-03-25', 'project_id': '',
           'lot': '', 'item': '', 'tax': 'unknown', 'installation': 'unknown',
           'warranty': 'unknown', 'shipping': 'unknown', 'service_response': 'unknown',
           'service_onsite': 'unknown', 'delivery': 'unknown', 'excerpt': XKA_EXCERPT}
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# 判据本身
# ---------------------------------------------------------------------------

def test_a_brand_own_product_page_gives_the_parameters_and_its_provenance():
    spec = product_configuration([source(MANUFACTURER_URL, MANUFACTURER_BODY)], MODEL, BRAND)
    assert spec['url'] == MANUFACTURER_URL
    assert spec['source'] == MANUFACTURER_SOURCE
    assert spec['manufacturer_owned'] is True
    assert spec['headings'] == ['技术参数']
    assert '最高转速 | 4500r/min' in spec['text']
    assert '红血球清洗用转子/SERO' in spec['text']
    # 采购要求一个字都不在取到的参数里。
    assert CLAUSE not in spec['text']
    assert '≥4500r/min' not in spec['text']


def test_a_third_party_product_page_is_used_but_not_called_a_manufacturer():
    spec = product_configuration([source(SHOWCASE_URL, SHOWCASE_BODY)], MODEL, BRAND)
    assert spec['source'] == PRODUCT_SOURCE
    assert spec['manufacturer_owned'] is False


def test_the_brands_own_page_wins_over_a_page_that_names_the_model_more_often():
    """展示平台整页的型号密度更高（6.20 对 4.58），但厂家自有页优先。"""
    spec = product_configuration([source(SHOWCASE_URL, SHOWCASE_BODY),
                                  source(MANUFACTURER_URL, MANUFACTURER_BODY)], MODEL, BRAND)
    assert spec['url'] == MANUFACTURER_URL
    assert spec['source'] == MANUFACTURER_SOURCE


def test_the_spec_heading_list_is_a_subset_of_the_shared_one():
    """产品参数标题只有这一族，且必须来自共享词表——不许长出第二套标题表。"""
    assert set(PRODUCT_SPEC_HEADINGS) <= set(CONFIGURATION_HEADINGS)
    assert PRODUCT_SPEC_HEADINGS == ('主要技术参数', '技术参数', '技术规格', '规格参数')
    assert '主要标的' in CONFIGURATION_HEADINGS and '主要标的' not in PRODUCT_SPEC_HEADINGS
    assert '采购需求' in CONFIGURATION_HEADINGS and '采购需求' not in PRODUCT_SPEC_HEADINGS


# ---------------------------------------------------------------------------
# 红线：采购需求、中标公告、招标文件都不许当参数
# ---------------------------------------------------------------------------

def test_the_award_notice_passes_the_naive_criterion_but_is_still_refused():
    """这条是这轮改动里最容易做错的地方，单独钉住。

    实测那份 ccgp 中标公告正文 4456 字：里面有 ``规格型号：XKA-2200``，也命中配置
    类标题「主要标的」。所以「正文含型号 + 能取到窗口」两个条件在它身上**都成立**
    ——只按这两条实现，这段公告正文就会被当成 XKA-2200 的参数印出去。
    """
    body = notice('ccgp-split-field-notice.txt')
    regions = configuration_regions(body, MODEL)
    assert any(contains_model(region['text'], MODEL) for region in regions)
    assert product_configuration([source(NOTICE_URL, body, 'tender_document')], MODEL, BRAND) is None


def test_a_tender_document_never_becomes_the_configuration():
    """招标文件里也写着这个型号（参考型号），但它是采购方口径的文书。"""
    assert product_configuration([source('http://ggzy.example.gov.cn/zb/1', TENDER_BODY,
                                        'tender_document')], MODEL, BRAND) is None


def test_a_document_that_says_it_is_a_tender_is_refused_even_when_untyped():
    """抓取阶段没认出来的招标文件，靠它自己开头那行「公开招标文件」也要挡住。"""
    spec = product_configuration([source('https://www.example-bid.com/notice/1', TENDER_BODY)],
                                 MODEL, BRAND)
    assert spec is None


def test_a_page_that_only_has_the_spec_heading_is_not_used_as_the_configuration():
    # 批3 实测：21 条成交价行里 18 条印出来只有一句"技术参数："——页面有参数标题、
    # 正文里却没有参数行（问答型产品页）。这种页不采纳，那一格照实说"来源页未提供配置"，
    # 用户在报告里看到的就不是一句空壳。
    shell = '\n'.join(['技术参数：',
                       '介绍下 XKA-2200 高速离心机的工作原理',
                       'XKA-2200 的使用寿命和质保时间是多少'])
    assert product_configuration([source(MANUFACTURER_URL, shell)], MODEL, BRAND) is None


def test_a_tender_style_heading_alone_is_not_a_product_spec_heading():
    """「技术参数要求」是招标文件的章节标题，不是产品参数页的标题。

    按子串匹配它命中「技术参数」；按「标题独占一行」这条结构判据它不算。
    """
    body = '\n'.join(['某某公司产品页', '1包 技术参数要求',
                      'XKA-2200 的最高转速要求为 ≥4500r/min', '投标人须逐条响应'])
    assert product_configuration([source('https://www.example-shop.com/p/1', body)], MODEL, BRAND) is None


def test_a_formal_publication_on_a_plain_host_is_refused():
    """不是 .gov.cn 的转载公告：正文写着中标结果 + 三个结构字段，发布者等级就成立。"""
    body = '\n'.join(['某某项目 中标结果公告', '项目编号：TEST-1', '采购人：某某医院',
                      '成交供应商：某某公司', '技术参数', 'XKA-2200 | 最高转速 | 4500r/min'])
    assert product_configuration([source('https://www.example-news.com/a/1', body)], MODEL, BRAND) is None


def test_an_aggregator_page_is_usable_for_the_configuration():
    """聚合站域名不再整站挡住配置：那道门是**价格通道**的门（第 4 条已撤销）。

    ``source_authority.py:75-79`` 写的是这些域名"只当**发现渠道**（拿线索），采信一律
    回到官方原文……也就没资格进**价格通道**"——问的是"这笔成交价是不是真的"。配置问的
    是"这段技术参数是谁写的、准不准"，用户 2026-09-26 的口径是「**如果是取配置的来源
    可以算 如果是取价格的来源 不能算**」。

    ``shihuantong.com``（世环通）是 ``AGGREGATOR_HOST_MARKERS`` 里**真实存在**的
    marker（``source_authority.py:129``），这里就用它当那张第三方参数页的主机。
    """
    from backend.services.source_authority import is_marketplace_or_aggregator

    assert is_marketplace_or_aggregator('www.shihuantong.com') is True

    spec = product_configuration(
        [source('https://www.shihuantong.com/tech/xka-2200.html', SHOWCASE_BODY)], MODEL, BRAND)
    assert spec is not None
    assert spec['host'] == 'shihuantong.com'
    assert '最高转速 | 4500/min' in spec['text']
    # 两档标签的判定逻辑本单没动：第三方平台的页面照样不许印成「厂家资料」。
    assert spec['source'] == PRODUCT_SOURCE
    assert spec['manufacturer_owned'] is False


def test_a_tender_document_on_an_aggregator_host_is_still_refused():
    """撤销第 4 条不许把红线一起放行：同一份正文是招标文件时，聚合站也进不来。

    这是上一条的**反证**：域名与其余判据完全一样，只把正文换成招标文件。带类型与
    不带类型（抓取阶段没认出来）两种都要挡住——挡它的是第 3 条那三种写法，与域名无关。
    """
    assert product_configuration([source('https://www.shihuantong.com/zb/1.html', TENDER_BODY,
                                        'tender_document')], MODEL, BRAND) is None
    assert product_configuration([source('https://www.shihuantong.com/zb/2.html',
                                         TENDER_BODY)], MODEL, BRAND) is None


def test_a_page_without_this_model_is_refused():
    """同一家的另一个型号：正文里没有 XKA-2200 就不是它的参数页。"""
    other = MANUFACTURER_BODY.replace('XKA-2200', 'TDZ4K')
    assert product_configuration([source(MANUFACTURER_URL, other)], MODEL, BRAND) is None


def test_a_window_that_does_not_name_the_model_is_refused():
    """分类页并列好几个型号：标题窗口里写的不是这个型号，就不取。

    实测量级：窗口 2400 字，所以把目标型号放到第 12000 字之后。
    """
    filler = '\n'.join(f'XZ21K-T | 指标{i} | {i}00r/min' for i in range(700))
    body = '某厂离心机产品页\n技术参数\n' + filler + '\nXKA-2200 相关页面链接'
    assert len(body) > 12000
    assert contains_model(body, MODEL)
    assert product_configuration([source('https://www.example-centrifuge.com/p/1', body)],
                                 MODEL, BRAND) is None


def test_nothing_to_use_returns_none_instead_of_an_empty_string():
    assert product_configuration([], MODEL, BRAND) is None
    assert product_configuration(None, MODEL, BRAND) is None
    assert product_configuration([source(MANUFACTURER_URL, MANUFACTURER_BODY)], '', BRAND) is None
    assert product_configuration([source(MANUFACTURER_URL, MANUFACTURER_BODY)], None, BRAND) is None
    assert product_configuration([{'url': MANUFACTURER_URL, 'body_status': 'unavailable',
                                   'source_type': 'unknown', 'body_text': ''}], MODEL, BRAND) is None
    # 正文里只有型号、没有任何参数标题：这不是参数页（也正是中标公告的形状）。
    assert product_configuration([source('https://x.example.com/p', '规格型号：XKA-2200 数量：1')],
                                 MODEL, BRAND) is None


# ---------------------------------------------------------------------------
# 落地：写进价格记录、报告里怎么说
# ---------------------------------------------------------------------------

def test_only_the_records_without_a_configuration_are_filled():
    records = [{'configuration': '含6×50ml角转子'}, {'configuration': 'unknown'},
               {'configuration': None}, {'configuration': ''}]
    written = apply_product_configuration(records, [source(MANUFACTURER_URL, MANUFACTURER_BODY)],
                                          MODEL, BRAND)
    assert written == 3
    # 公告／合同附件自己写的配置是这笔成交记录的事实，不被产品参数顶掉。
    assert records[0] == {'configuration': '含6×50ml角转子'}
    for record in records[1:]:
        assert record['configuration_source'] == MANUFACTURER_SOURCE
        assert record['configuration_url'] == MANUFACTURER_URL
        assert record['configuration'] == MANUFACTURER_SPEC
    # 写出去的这份里没有页面框架：导航行与版权页脚一条都不剩，参数一条都没少。
    assert '网站首页' not in MANUFACTURER_SPEC and 'Copyright' not in MANUFACTURER_SPEC
    assert '最高转速 | 4500r/min' in MANUFACTURER_SPEC


def test_nothing_is_written_when_there_is_no_product_page():
    records = [{'configuration': 'unknown'}]
    assert apply_product_configuration(records, [source(NOTICE_URL, notice('ccgp-split-field-notice.txt'),
                                                       'tender_document')], MODEL, BRAND) == 0
    assert records[0] == {'configuration': 'unknown'}


def test_the_report_line_says_where_the_parameters_came_from():
    record = {'configuration': '最高转速 | 4500r/min\n重量 | 12kg',
              'configuration_source': MANUFACTURER_SOURCE, 'configuration_url': MANUFACTURER_URL}
    line = report_configuration(record)
    assert line.startswith('厂家资料标称参数（非公告配置）：')
    assert '4500r/min' in line and '\n' not in line
    assert report_configuration({**record, 'configuration_source': PRODUCT_SOURCE}).startswith('产品资料')
    # 公告里没写配置时，报告仍然说清是哪一段正文没写（既有行为不变）。
    assert report_configuration({'configuration': 'unknown', 'field_notes': {}}) == price_term_text(
        {'configuration': 'unknown', 'field_notes': {}}, 'configuration')
    assert report_configuration(None) == '待核实'


def test_a_long_parameter_block_is_truncated_in_the_report():
    record = {'configuration': '指标 | ' * 400, 'configuration_source': MANUFACTURER_SOURCE}
    line = report_configuration(record)
    assert line.endswith('…')
    assert len(line) <= REPORT_CHARACTER_LIMIT + 40


# ---------------------------------------------------------------------------
# 端到端：价格记录上落地的是什么
# ---------------------------------------------------------------------------

def test_the_price_record_carries_the_manufacturers_parameters_and_their_source(research):  # noqa: F811
    service, run, _, _ = research
    service.verify_publishers = lambda *args, **kwargs: None
    run['sources'] = [source(NOTICE_URL, notice('ccgp-split-field-notice.txt'), 'tender_document'),
                      source(MANUFACTURER_URL, MANUFACTURER_BODY)]
    _extractor(service, _extraction())

    prices = service.prices(run, {'brand': BRAND, 'model': MODEL})

    assert len(prices) == 1
    price = prices[0]
    assert price['unit_price'] == 17480                      # 价格判据没变
    assert price['configuration'] == MANUFACTURER_SPEC
    assert price['configuration_source'] == MANUFACTURER_SOURCE
    assert price['configuration_url'] == MANUFACTURER_URL
    assert '最高转速 | 4500r/min' in price['configuration']   # 这台机器有什么
    assert CLAUSE not in price['configuration']              # 采购方要什么，一个字都没进来
    assert '≥4500r/min' not in price['configuration']
    # 成交记录自己的缺口照旧：公告还是没写这笔交易的配置。
    assert 'configuration' in price['gaps']


def test_without_a_product_page_the_configuration_stays_empty(research):  # noqa: F811
    """只有采购需求时：配置留空，绝不拿采购要求兜底。"""
    service, run, _, _ = research
    service.verify_publishers = lambda *args, **kwargs: None
    run['sources'] = [source(NOTICE_URL, notice('ccgp-split-field-notice.txt'), 'tender_document')]
    _extractor(service, _extraction())

    prices = service.prices(run, {'brand': BRAND, 'model': MODEL})

    assert len(prices) == 1
    assert prices[0]['configuration'] == 'unknown'
    assert 'configuration_source' not in prices[0]
    assert 'configuration_url' not in prices[0]


def test_a_tender_document_that_names_the_model_still_does_not_fill_the_configuration(research):  # noqa: F811
    """招标文件正文里写着参考型号 XKA-2200，这是同一批来源里最容易误用的一份。"""
    service, run, _, _ = research
    service.verify_publishers = lambda *args, **kwargs: None
    run['sources'] = [source(NOTICE_URL, notice('ccgp-split-field-notice.txt'), 'tender_document'),
                      source('http://ggzy.example.gov.cn/zb/1', TENDER_BODY, 'tender_document')]
    _extractor(service, _extraction())

    prices = service.prices(run, {'brand': BRAND, 'model': MODEL})

    assert len(prices) == 1
    assert prices[0]['configuration'] == 'unknown'
    assert CLAUSE not in str(prices[0].get('configuration'))


def test_neither_a_product_page_nor_a_tender_device_leaves_the_configuration_empty(research):  # noqa: F811
    """两样都没有：留空，不编造。"""
    service, run, _, _ = research
    service.verify_publishers = lambda *args, **kwargs: None
    # 同一批来源里只有**别的型号**的产品页：它既不是本型号的参数页，也不是采购需求。
    other = SHOWCASE_BODY.replace('XKA-2200', 'TDZ4K').replace('XKA2200', 'TDZ4K')
    run['sources'] = [source(NOTICE_URL, notice('ccgp-split-field-notice.txt'), 'tender_document'),
                      source('https://www.example-shop.com/p/9', other)]
    _extractor(service, _extraction())

    prices = service.prices(run, {'brand': BRAND, 'model': MODEL})

    assert len(prices) == 1
    assert prices[0]['configuration'] == 'unknown'
    assert 'configuration_source' not in prices[0]


def test_the_configuration_is_not_re_extracted_for_other_models(research):  # noqa: F811
    """同一个 run 里有别的型号的产品页时，不会把它的参数挂到本型号上。"""
    service, run, _, _ = research
    service.verify_publishers = lambda *args, **kwargs: None
    run['sources'] = [source(NOTICE_URL, notice('ccgp-split-field-notice.txt'), 'tender_document'),
                      source(MANUFACTURER_URL,
                             MANUFACTURER_BODY.replace('XKA-2200', 'TDZ4K').replace('XKA2200', 'TDZ4K'))]
    _extractor(service, _extraction())

    prices = service.prices(run, {'brand': BRAND, 'model': MODEL})

    assert prices[0]['configuration'] == 'unknown'
    assert 'TDZ4K' not in str(prices[0].get('configuration'))


# ---------------------------------------------------------------------------
# 清洗：先把"脏"钉死，再钉"参数一条都没少"，最后钉"判据不认词表"
# ---------------------------------------------------------------------------

def test_the_real_window_is_dirty_in_exactly_the_two_ways_reported():
    """先钉输入：真实窗口 2400 字 / 74 行，脏在两处。

    这条要是红了，说明样本被换过，下面几条断言的前提就不成立。
    """
    assert len(REAL_PAGE_WINDOW) == 2400
    assert len(REAL_PAGE_WINDOW.splitlines()) == 74
    # ① 同一张表解析两遍：直行 13 行 + 「表格按列展开」10 行。
    assert REAL_PAGE_WINDOW.count('最高转速') == 2
    assert len([line for line in REAL_PAGE_WINDOW.splitlines()
                if line.startswith('[表格按列展开')]) == 10
    # ② 页面框架内容：留言表单 + 相关推荐轮播。
    assert '留言板' in REAL_PAGE_WINDOW and '发 送' in REAL_PAGE_WINDOW
    assert '相关推荐' in REAL_PAGE_WINDOW
    assert len([line for line in REAL_PAGE_WINDOW.splitlines()
                if line.startswith('[http')]) == 19
    # 最后一条链接在库里就是断的（窗口取到 2400 字正好切在它中间，闭括号没进来）。
    # 这条样本限制下面会用到：它认不出"纯链接"，所以清洗后可能残留这一行。
    assert REAL_PAGE_WINDOW.splitlines()[-1] == '[https://www.xian'


def test_the_real_window_keeps_every_parameter_after_cleaning():
    """**参数一条都不能少**：清洗后逐个键检查，并逐字对比值。"""
    cleaned, dropped = clean_page_text(REAL_PAGE_WINDOW)
    lines = cleaned.splitlines()

    parameters = {
        '型号': 'XKA-2200',
        '订货号': '0205202000',
        '最高转速': '4500r/min',
        '最大相对离心力': '2000×g',
        '转子容量': '(0.25～1ml)×12',
        '转速精度': '±30r/min',
        '定时范围': '1min~99min',
        '整机噪声': '最低 ≤58dB(A)',
        '电源': 'AC220V ±22V 50/60Hz',
        '整机功率': '100W',
        '外形尺寸': '456×330×266mm(L×W×H)',
        '包装尺寸': '568×423×300mm(L×W×H)',
        '重量': '12kg',
    }
    kept = dict(line.split(' | ', 1) for line in lines if ' | ' in line)
    for key, value in parameters.items():
        assert key in kept, f'{key} 被当成页面框架丢掉了'
        assert kept[key] == value, f'{key} 的值变了：{kept[key]!r} != {value!r}'
    # 分组标题与六行转子表原样保留。
    assert '技术参数' in lines and '转子参数' in lines
    rotor = [line for line in lines if line.startswith(('淋巴球清洗用转子', '红血球清洗用转子'))]
    assert len(rotor) == 6
    assert '*所有产品都可根据客户要求定制、设计、生产。' in lines
    # 两处脏内容都不在了：重复的投影一条不剩，页面框架的产品名与完整链接一条不剩。
    assert '[表格按列展开' not in cleaned
    assert '留言板' not in cleaned and '发 送' not in cleaned
    assert '相关推荐' not in cleaned
    assert 'H300R大容量高速冷冻离心机' not in cleaned
    assert 'xian' not in cleaned.replace('www.xian', '')
    assert cleaned.count('[http') == 1, '只剩样本尾部那条断掉的链接'
    assert '4500r/min' in cleaned and cleaned.count('4500r/min') == 1
    # 2400 字 / 74 行 → 25 行；删掉的 49 行全部有据：
    # 10 条重复投影 + 36 行推荐位（18 条完整链接 + 18 个产品名）+ 3 行模板短行
    # （留言板 / 发 送 / 相关推荐）。第 74 行（那条断链接）留着——这是**样本的**限制，
    # 不是判据的漏洞：真窗口里它是完整链接，属于"纯链接行"。
    assert len(lines) == 25
    assert dropped == 49


def test_the_real_window_via_the_product_page_path(research):  # noqa: F811
    """走 ``product_configuration`` 这条路拿到的 ``configuration`` 就是清洗后的。"""
    spec = product_configuration([source(MANUFACTURER_URL, MANUFACTURER_BODY)], MODEL, BRAND)
    assert '[表格按列展开' not in spec['text']
    # 型号密度仍按**清洗前**的窗口算，不受清洗影响（``_rank`` 的档内比较要用同一把尺子）。
    assert spec['model_mentions'] == 2


def test_an_aggregator_pages_navigation_and_recommendations_never_enter_the_configuration():
    """聚合页的分类导航与推荐商品不进配置 ★M1 要修的那件事。

    实测 ``yiqi.com/product/detail_1867343.html``（HVE-50）：面包屑那一行带 ``|``，
    被当成宽表表头，下面两个 ``find_ulbox``（分类导航 + 推荐商品）于是被拼成一张
    1979 字的假参数表，写进 9 条成交价共用的配置——里面是「耐磨擦试验机｜现代环境」
    「硅表维护套件｜Hach/哈希」这些与这台机器无关的商品。

    两条断言必须同时成立，缺一个都是"用少给参数换干净"：
    **脏的一条都不在**，**真正写着这台机器的那条还在**。
    """
    spec = product_configuration([source(AGGREGATOR_URL, AGGREGATOR_PAGE)], 'HVE-50', 'HIRAYAMA')

    assert spec is not None and spec['url'] == AGGREGATOR_URL
    for junk in ('耐磨擦试验机', '硅表维护套件', '现代环境', 'Hach/哈希', '金属浴', '光学设备'):
        assert junk not in spec['text'], '页面框架进了配置：%s' % junk
    assert 'HVE-50上限有效深度：710mm' in spec['text']
    # 写出去的每一行都能在页面原文里逐字对上（压缩只删行、不改字）。
    for line in spec['text'].splitlines():
        assert line in AGGREGATOR_PAGE.splitlines()


def test_a_lone_nav_link_and_a_copyright_line_are_still_kept_for_now():
    """**没清掉的**两类页面框架内容（如实记录，不是判据失效的证据）。

    这两行只在合成样本里出现，判据处理不了它们，而且都不属于界面实测里报的两类：

    * ``网站首页 [url] 产品中心 [url]``——一行里两个导航项、不是"整行只有一个链接"，
      也不成片，所以按结构认不出来（真页面上它们成片出现，见上面那条测试）；
    * 版权页脚——长行、带公司名，与"参数页的说明性文字"形状相同，没有可靠的结构信号。

    保守留着它们：删错一行参数（例如把参数值里的链接或说明性文字删掉）比留下两行
    导航更贵。写在这里是为了让下一个人知道这是**已知的**，别再当新 bug 报一次。
    """
    cleaned, dropped = strip_page_chrome(MANUFACTURER_BODY)
    assert dropped == 0
    assert '网站首页 [http://www.xiangyilxj.com/] 产品中心 [http://www.xiangyilxj.com/product]' in cleaned
    assert 'Copyright © 湖南湘仪实验室仪器开发有限公司 All Rights Reserved' in cleaned
    # 参数一行不少。
    for key in ('型号 | XKA-2200', '最高转速 | 4500r/min', '重量 | 12kg'):
        assert key in cleaned


def test_page_chrome_is_dropped_by_shape_not_by_a_word_list():
    """判据不是词表：把词换掉，同一形状照样删；形状不成立，同一个词照样留。"""
    # 同一个词的两种用法：成片的推荐轮播删掉，而参数行里的「相关推荐」保留。
    # 版式照真实那页排：参数行 → 留言表单（两行）→ 轮播标题 → 产品名 + 裸链接。
    carousel = '\n'.join([
        '技术参数',
        '型号 | XKA-2200',
        '最高转速 | 4500r/min',
        '留言板',
        '发 送',
        '相关推荐',
        *[line for pair in ((f'品牌{index}型号{index}', f'[https://example.com/p/{index}]')
                            for index in range(4)) for line in pair],
    ])
    cleaned, dropped = strip_page_chrome(carousel)
    assert '相关推荐' not in cleaned
    assert '留言板' not in cleaned and '发 送' not in cleaned
    assert dropped == 11
    assert '最高转速 | 4500r/min' in cleaned

    # 换两个词、换成别的厂家的导航，形状不变 → 一样删。
    renamed = carousel.replace('相关推荐', '热门机型').replace('品牌', '甲公司')
    renamed_cleaned, renamed_dropped = strip_page_chrome(renamed)
    assert '热门机型' not in renamed_cleaned and renamed_dropped == 11


def test_a_key_that_merely_sounds_like_navigation_is_a_real_parameter():
    """反例：带「推荐／留言／发送」字样的行确实可能是参数，必须留下。

    这是"按词表硬删"会踩的坑——``相关推荐 | 见厂家选型手册`` 是一个键值对，
    ``发 送方式 | 厂家直发`` 也是，它们与那两个按钮文字只差一个分隔符。
    """
    body = '\n'.join([
        '技术参数',
        '型号 | XKA-2200',
        '最高转速 | 4500r/min',
        '推荐转速 | 4000r/min',
        '相关推荐 | 见厂家选型手册',
        '留言板说明 | 免费安装调试',
        '发 送方式 | 厂家直发',
    ])
    cleaned, dropped = strip_page_chrome(body)
    assert dropped == 0
    assert cleaned == body


def test_a_short_group_heading_is_not_mistaken_for_a_button():
    """「技术参数」也只有 4 个字，而且窗口里可能就它最短——它不能当按钮删。"""
    body = '\n'.join(['技术参数', '型号 | XKA-2200', '最高转速 | 4500r/min'])
    cleaned, _ = strip_page_chrome(body)
    assert '技术参数' in cleaned


def test_a_lone_name_and_link_is_not_a_carousel():
    """孤立的「名字 + 链接」不是轮播（例如「查看厂家资料原文 [url]」），不动。"""
    body = '\n'.join(['技术参数', '型号 | XKA-2200', '查看厂家资料原文',
                      '[https://www.xiangyilxj.com/product/info_itemid_952.html]'])
    cleaned, dropped = strip_page_chrome(body)
    assert dropped == 0 and cleaned == body


def test_a_projection_is_dropped_only_when_the_grid_row_repeats_it():
    """重复的投影删掉；网格里对不上的投影一条都不删（宁可留重复，不可丢信息）。"""
    plain = '\n'.join(['技术参数', '型号 | XKA-2200', '最高转速 | 4500r/min', '重量 | 12kg'])
    repeated = plain + '\n' + '\n'.join([
        '[表格按列展开 行3列2] XKA-2200 最高转速 4500r/min',
        '[表格按列展开 行4列2] XKA-2200 重量 12kg',
    ])
    cleaned, dropped = without_repeated_table_projection(repeated)
    assert dropped == 2 and '[表格按列展开' not in cleaned
    assert cleaned == plain

    # 网格里没有这一格的值（另一列的 4000/min）→ 留着。
    partial = plain + '\n[表格按列展开 行3列3] XKA-2200 最高转速 4000/min'
    kept, dropped = without_repeated_table_projection(partial)
    assert dropped == 0 and '4000/min' in kept


def test_a_multi_model_table_keeps_every_value_because_the_columns_differ():
    """真实的多型号表（``web_tables.render_table`` 的原始输出）：两条投影都只对自己那一列成立。

    网格行 ``最高转速 | 4500/min | 4000/min`` 已经把两列的值都印出来了，所以两条投影都
    是重复；判据必须按**列**对齐——只比"值相同"会让 4000/min 那条挂到 4500/min 的那一列上
    （那是错的），只按行比又可能整条留下。这里钉的是"两列的值一个不少"。
    """
    body = render_table(MULTI_MODEL_TABLE_HTML).replace('</p><p>', '\n').replace('<p>', '').replace('</p>', '')
    projections = [line for line in body.splitlines() if line.startswith('[表格按列展开')]
    assert len(projections) == 6
    cleaned, dropped = without_repeated_table_projection(body)
    assert dropped == 6
    # 值一条都没少，而且每个值只剩一份。
    for value in ('4500/min', '4000/min', '2000xg', '1800xg',
                  '330x375x250mm', '300x350x240mm'):
        assert value in cleaned, value
        assert cleaned.count(value) == 1, value
    assert '最高转速 | 4500/min | 4000/min' in cleaned
    assert '外形尺寸 | 330x375x250mm | 300x350x240mm' in cleaned


def test_a_body_without_any_grid_keeps_every_projection():
    """整段没有表网格（没有 ``|``）时，投影就是唯一一份，一条都不删。"""
    body = '\n'.join(['技术参数', '型号：XKA-2200',
                      '[表格按列展开 行3列2] XKA-2200 最高转速 4500r/min'])
    cleaned, dropped = without_repeated_table_projection(body)
    assert dropped == 0 and cleaned == body


def test_cleaning_is_idempotent_and_survives_empty_input():
    once, _ = clean_page_text(REAL_PAGE_WINDOW)
    twice, second = clean_page_text(once)
    assert twice == once and second == 0
    for value in ('', '   ', None):
        assert clean_page_text(value) == (value, 0)
    assert strip_page_chrome('') == ('', 0)
    assert without_repeated_table_projection('') == ('', 0)


# ---------------------------------------------------------------------------
# 第 5 条的第二种形态：参数区没有「独占一行的标题」时，看窗口自己是不是一张表
# （``设计规范`` §四 的 4 个 KH20A 组合就是这个形状；这里是同形状的离线正文）
# ---------------------------------------------------------------------------

#: 真实厂家页（``kaidalab.com/lixinji1238.html``，8525 字）的形状：参数区在页面上是
#: **标签导航**，不是标题——那一行写成 ``产品参数 [http://…/lixinji1238.html]``，与
#: 「产品介绍」「转子附表」「应用案例」排在一起、指向同一个 URL。所以标题这条路
#: 取不到它（``_spec_headings`` 为空），但页面上确实有一张 10 行的参数表。
KH20A_URL = 'http://www.kaidalab.com/lixinji1238.html'
KH20A_BODY = '\n'.join([
    '凯达牌高性能高速台式离心机-KH20A-湖南凯达科学仪器有限公司',
    '[http://www.kaidalab.com/]',
    '网站首页 [http://www.kaidalab.com/]',
    '产品中心 [http://www.kaidalab.com/tylxj122.html]',
    '产品介绍 [http://www.kaidalab.com/lixinji1238.html]',
    '产品参数 [http://www.kaidalab.com/lixinji1238.html]',
    '转子附表 [http://www.kaidalab.com/lixinji1238.html]',
    '应用案例 [http://www.kaidalab.com/lixinji1238.html]',
    '联系凯达 [http://www.kaidalab.com/lxwm19.html]',
    '型号 Model | KH20A',
    '最高转速 Max Speed | 18600r/min',
    '最大相对离心力 Max RCF | 23950×g',
    '最大容量 Max Capacity | 400ml',
    '转速精度 Speed Accuracy | ±20r/min',
    '定时范围 Time Range | 0-999min/999s',
    '电源 Power Supply | AC 220V 50Hz 15A',
    '整机噪声 Noise | ≤60dB',
    '外形尺寸 Dimension | 460×350×350mm',
    '重量 Weight | 20kg',
    'Copyright © 湖南凯达科学仪器有限公司',
])


def test_a_parameters_table_without_a_heading_is_still_read():
    """修前不算、修后算：标题这条路取不到（``_spec_headings`` 为空），表本身在。

    ``设计规范`` §四 的 4 个 KH20A 组合就卡在这条上：来源抓到了、型号在里面、
    10 行「参数名 | 值」也在，只因为参数区那行是带链接的标签导航而不是标题。
    """
    assert _spec_headings(KH20A_BODY) == []
    spec = product_configuration([source(KH20A_URL, KH20A_BODY)], 'KH20A', '湖南凯达')
    assert spec is not None
    assert spec['url'] == KH20A_URL
    assert spec['headings'] == []
    assert '型号 Model | KH20A' in spec['text']
    assert '最高转速 Max Speed | 18600r/min' in spec['text']
    assert '最大相对离心力 Max RCF | 23950×g' in spec['text']


def test_a_navigation_tab_named_product_parameters_is_not_a_spec_table():
    """反例一：光有「产品参数」这个**链接标签**不算。

    真实页面里这一行是标签导航条（``产品参数 [http://…]`` 与「产品介绍」「转子附表」
    「应用案例」「合作客户」排在一起）。剥掉 ``[http://…]`` 尾巴再比词表就能让它命中
    「产品参数」——但全库 1137 个来源、36 万行里这样新命中的 15 行**全部**是这种
    导航条，没有一行是真标题。所以这里不剥尾巴、也不把「产品参数」收进白名单。
    """
    body = '\n'.join([
        '某某高速离心机 KH20A 产品页',
        '产品介绍 [http://www.example-shop.com/p/1]',
        '产品参数 [http://www.example-shop.com/p/1]',
        '转子附表 [http://www.example-shop.com/p/1]',
        '应用案例 [http://www.example-shop.com/p/1]',
        '合作客户 [http://www.example-shop.com/p/1]',
        '咨询电话 400-000-0000',
    ])
    assert _spec_headings(body) == []
    assert _spec_field_table(body, 'KH20A') is False


def test_a_shared_instrument_register_is_not_a_spec_table():
    """反例二（本轮真实数据换来的）：高校共享仪器平台的**设备台账页**不是参数表。

    上海交大那个 ``aemd.sjtu.edu.cn/platform/750.html``：``型号： | Lambda 750S``
    ``功能： | 190nm~2500nm波长范围内…测试`` ``工程师： | 付老师 / 电话``
    ``设备地点： | 东区测试III区`` ``设备编号： | ETE3UVV01``。这是资产信息，
    认下来会把工程师姓名和电话写进「配置」。所以值必须是**量**（数字／``±``／
    ``≤``／负号／``×`` 开头），而设备台账里只有「型号」那一行是量值。
    """
    body = '\n'.join([
        '紫外可见分光光度计',
        '预约登录 [https://aemd-lims.sjtu.edu.cn/lims_shjd]',
        '平台介绍 [https://aemd.sjtu.edu.cn/about]',
        '型号： | Lambda 750S',
        '功能： | 190nm~2500nm波长范围内薄膜样品吸光度(A)/反射率(R%)/透过率(T%)测试',
        '工程师： | 付老师 / (021)34206126-6010',
        '设备地点： | 东区测试III区',
        '设备编号： | ETE3UVV01',
    ])
    assert _spec_field_table(body, 'Lambda 750S') is False
    assert product_configuration(
        [source('https://aemd.sjtu.edu.cn/platform/750.html', body)],
        'Lambda 750S', 'PerkinElmer') is None


def test_an_award_item_table_is_not_a_spec_table():
    """反例三：成交标的表是多格表，第 1 格是序号不是字段名。"""
    body = '\n'.join([
        '品目号 | 品目名称 | 采购标的 | 品牌 | 规格型号 | 数量（单位） | 单价(元) | 总价(元)',
        '1-1 | 教学仪器 | 高性能台式高速离心机 | 湖南凯达 | KH20A | 1.00(台) | 9,200.0000 | 9,200.0000',
        '1-2 | 教学仪器 | 超声波清洗机 | 昆山舒美 | KQ5200E | 1.00(台) | 2,400.0000 | 2,400.0000',
        '1-3 | 教学仪器 | 精密数显台式酸度计 | 青岛溯源 | PHS-3DW | 3.00(台) | 390.0000 | 1,170.0000',
    ])
    assert _spec_field_table(body, 'KH20A') is False


def test_a_long_sentence_value_is_not_a_parameter_row():
    """值这一格超过 ``SPEC_VALUE_MAX`` 的是说明句，不是参数。"""
    text = '\n'.join([
        '品牌 | 湖南凯达',
        '功能 | ' + '很长的设备说明' * 8,
        '转速 | 18600r/min',
        '容量 | 600ml',
    ])
    assert _spec_field_rows(text) == [('品牌', '湖南凯达'), ('转速', '18600r/min'), ('容量', '600ml')]
    assert _spec_measure_rows(text, 'KH20A') == [('转速', '18600r/min'), ('容量', '600ml')]


def test_the_report_line_starts_at_the_parameters_not_at_the_page_navigation():
    """报告那一行从**参数区**开始截，不从配置全文的开头截。

    配置文本是配置窗口的原文（实测那一页 8525 字），开头是页面导航；从开头截
    200 字，用户看到的是「…网站首页 [http://…] 产品中心 [http://…]」，参数一个字
    都进不来（``设计规范`` §三 报告快照 ``report-626444eed7ef027ff4d11976``）。
    """
    record = {'configuration': KH20A_BODY, 'configuration_source': MANUFACTURER_SOURCE,
              'configuration_url': KH20A_URL, 'model': 'KH20A'}
    line = report_configuration(record)
    assert line.startswith('厂家资料标称参数（非公告配置）：型号 Model | KH20A')
    assert '网站首页' not in line and 'http://' not in line
    assert '18600r/min' in line


def test_the_report_line_leaves_a_headed_block_at_its_heading():
    """有标题的那一段仍然从标题开始（既有行为不变）。"""
    record = {'configuration': MANUFACTURER_BODY, 'configuration_source': MANUFACTURER_SOURCE,
              'configuration_url': MANUFACTURER_URL, 'model': MODEL}
    line = report_configuration(record)
    assert line.startswith('厂家资料标称参数（非公告配置）：技术参数')


def test_the_report_line_stays_at_the_top_when_there_is_no_parameter_region():
    """找不到参数区时不猜：照旧从头截。"""
    record = {'configuration': '这是一段没有参数表的说明文字。' * 40,
              'configuration_source': MANUFACTURER_SOURCE}
    line = report_configuration(record)
    assert line.startswith('厂家资料标称参数（非公告配置）：这是一段没有参数表的说明文字。')


def test_a_page_with_the_heading_still_wins_over_one_with_only_the_table():
    """新通路不许顶掉原来带标题的候选：证据强度是排序的第二维。

    实测回归：``task-efd631dee1b2``（``H1850R``）原本取 ``yiqi.com`` 那个带
    「技术参数」的页面（4414 字），修后一度被另一个没有标题、但窗口短所以型号密度
    更高的页面（2045 字）顶掉。
    """
    headed = '\n'.join(['技术参数', '型号 | XKA-2200', '最高转速 | 4500r/min',
                        '最大相对离心力 | 2000×g', '外形尺寸 | 456×330×266mm',
                        '本页说明 ' * 200])
    tabled = '\n'.join(['型号 | XKA-2200', '最高转速 | 4500r/min',
                        '最大相对离心力 | 2000×g', '外形尺寸 | 456×330×266mm'])
    spec = product_configuration([source('https://www.example-shop.com/p/2', tabled),
                                 source('https://www.example-shop.com/p/1', headed)],
                                 'XKA-2200')
    assert spec['url'] == 'https://www.example-shop.com/p/1'
    assert spec['headings'] == ['技术参数']
