"""聚合站发现渠道的离线测试：不联网、不依赖浏览器。

夹具按 2026-xx 实测到的真实页面结构写（见
``output/procurement-sourcing/probe_aggregators.txt``）：
医价网是 Element UI 表头/表体拆成两个 ``<table>``，采招网详情页关键字段被服务端脱敏。
"""
from __future__ import annotations

from backend.services.aggregator_discovery import (
    AGGREGATOR_120SHOP,
    AGGREGATOR_BIDCENTER,
    AGGREGATOR_CHINAMAE,
    PRICE_EVIDENCE_ELIGIBLE,
    AggregatorDiscovery,
    Lead,
    price_evidence_from,
)
from backend.services.procurement_research import AGGREGATOR_RECHECK_LIMIT
from backend.services.source_authority import (
    LOCAL_AUTHORITY, is_marketplace_or_aggregator, publication_tier, publisher_tier)
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture

SHOP_120_KEYWORD = 'XKA-2200'

#: 医价网结果表：表头与表体是两个 <table>，单元格文本被拆成多个节点。
SHOP_120_RESULT_HTML = """
<html><body><div class="el-table">
  <div class="el-table__header-wrapper"><table class="el-table__header"><thead><tr>
    <th><div class="cell"><label class="el-checkbox"></label></div></th>
    <th><div class="cell">序号</div></th>
    <th><div class="cell">名称</div></th>
    <th><div class="cell">品牌</div></th>
    <th><div class="cell">型号</div></th>
    <th><div class="cell">采购地区</div></th>
    <th><div class="cell">成交时间</div></th>
    <th><div class="cell">成交单位</div></th>
    <th><div class="cell">采购医院</div></th>
    <th><div class="cell">成交价格</div></th>
    <th><div class="cell">操作</div></th>
  </tr></thead></table></div>
  <div class="el-table__body-wrapper"><table class="el-table__body"><tbody>
    <tr>
      <td><label class="el-checkbox"></label></td>
      <td><div class="cell">1</div></td>
      <td><div class="cell">血型血清专用离心机（医用离心机）</div></td>
      <td><div class="cell">湘仪</div></td>
      <td><div class="cell">XKA-2200</div></td>
      <td><div class="cell">新疆/和田</div></td>
      <td><div class="cell">2026-03</div></td>
      <td><div class="cell">新疆贝昇康润生物有限公司<br>7次</div></td>
      <td><div class="cell">和田地区人民医院<br>231次</div></td>
      <td><div class="cell"><div style="display: flex;"><div><div>1</div><span>.07</span><span>万</span></div></div></div></td>
      <td><div class="cell">价格 参数 公告 附件</div></td>
    </tr>
    <tr>
      <td><label class="el-checkbox"></label></td>
      <td><div class="cell">2</div></td>
      <td><div class="cell">221301医用离心机I</div></td>
      <td><div class="cell">三江/湘仪</div></td>
      <td><div class="cell">SJW-I型/XKA-2200</div></td>
      <td><div class="cell">新疆/巴音郭楞</div></td>
      <td><div class="cell">2025-07</div></td>
      <td><div class="cell">新疆生荣医学科技有限公司<br>313次</div></td>
      <td><div class="cell">和静县人民医院<br>95次</div></td>
      <td><div class="cell"><div><div>1</div><span>.60</span><span>万</span></div></div></td>
      <td><div class="cell">价格 参数 公告 附件</div></td>
    </tr>
    <tr>
      <td><label class="el-checkbox"></label></td>
      <td><div class="cell">3</div></td>
      <td><div class="cell">超低温冷冻储存箱</div></td>
      <td><div class="cell">中科美菱</div></td>
      <td><div class="cell">DW-86L578S</div></td>
      <td><div class="cell">北京/丰台</div></td>
      <td><div class="cell">2026-03</div></td>
      <td><div class="cell">某生物科技有限公司<br>3次</div></td>
      <td><div class="cell">某医院<br>5次</div></td>
      <td><div class="cell"><div><div>3</div><span>.20</span><span>万</span></div></div></td>
      <td><div class="cell">价格 参数 公告 附件</div></td>
    </tr>
    <tr>
      <td><label class="el-checkbox"></label></td>
      <td><div class="cell">4</div></td>
      <td><div class="cell">血型专用离心机</div></td>
      <td><div class="cell">(卢)湘仪/湘鑫</div></td>
      <td><div class="cell">XKA - 2200</div></td>
      <td><div class="cell">新疆/昌吉</div></td>
      <td><div class="cell">2026-05</div></td>
      <td><div class="cell">新疆新丝路惠康医疗科技有限公司<br>11次</div></td>
      <td><div class="cell">木垒哈萨克自治县人民医院<br>168次</div></td>
      <td><div class="cell"><div><span>￥411.</span><span>#</span><span> 万元</span></div></div></td>
      <td><div class="cell">价格 参数 公告 附件</div></td>
    </tr>
  </tbody></table></div>
</div></body></html>
"""

#: 医价网详情页的结果表：列名是「设备名称」，而且「采购医院」排在「成交单位」前面。
#: 解析必须按列名定位，不能按列序，否则会把供应商和采购人搞反。
SHOP_120_DETAIL_HTML = """
<html><body><div class="el-table">
  <div class="el-table__header-wrapper"><table class="el-table__header"><thead><tr>
    <th><div class="cell">序号</div></th>
    <th><div class="cell">设备名称</div></th>
    <th><div class="cell">品牌</div></th>
    <th><div class="cell">型号</div></th>
    <th><div class="cell">采购地区</div></th>
    <th><div class="cell">采购医院</div></th>
    <th><div class="cell">成交单位</div></th>
    <th><div class="cell">成交时间</div></th>
    <th><div class="cell">成交价格</div></th>
    <th><div class="cell">操作</div></th>
  </tr></thead></table></div>
  <div class="el-table__body-wrapper"><table class="el-table__body"><tbody>
    <tr>
      <td><div class="cell">1</div></td>
      <td><div class="cell">血型血清专用离心机（医用离心机）</div></td>
      <td><div class="cell">湘仪</div></td>
      <td><div class="cell">XKA-2200</div></td>
      <td><div class="cell">新疆/和田</div></td>
      <td><div class="cell">和田地区人民医院<br>231次</div></td>
      <td><div class="cell">新疆贝昇康润生物有限公司<br>7次</div></td>
      <td><div class="cell">2026-03-12</div></td>
      <td><div class="cell"><div><div>1</div><span>.07</span><span>万</span></div></div></td>
      <td><div class="cell">价格 参数 公告 附件</div></td>
    </tr>
  </tbody></table></div>
</div></body></html>
"""

SHOP_120_DETAIL_URL = 'https://www.120shop.cn/bid/goodsbank/goodsdetail?id=29295'

#: 采招网搜索结果列表：只有标题和详情页链接是可用的。
BIDCENTER_SEARCH_HTML = """
<html><head><title>采招网-专业的招投标信息搜索引擎</title></head><body>
<div class="ssjg-list">
  <div class="ssjg-list_bt"><a href="//www.bidcenter.com.cn/news-441066370-4.html" target="_blank">光学相干断层成像及血管成像仪等一批医疗设备中标成交结果公告</a></div>
  <div class="ssjg-list_nr">四川 中标结果 中标金额：289.63万元</div>
</div>
<div class="ssjg-list">
  <div class="ssjg-list_bt"><a href="//www.bidcenter.com.cn/news-443936412-4.html">光学相干断层成像及血管成像仪等一批医疗设备-签约履行</a></div>
</div>
<div class="recommend"><a href="//www.bidcenter.com.cn/news-1-4.html">推荐位链接，不该被当线索</a></div>
</body></html>
"""

#: 采招网详情页：项目编号/采购单位/中标单位都是「点击查看」或带「(略)」的脱敏值。
BIDCENTER_DETAIL_HTML = """
<html><head><title>光学相干断层成像及血管成像仪等一批医疗设备中标成交结果公告-采招网</title></head><body>
<table>
  <tr>
    <td class="xinxi_collect_title">发布时间</td><td>2026-09-09</td>
    <td class="xinxi_collect_title">项目编号</td><td><a href="javascript:;">点击查看</a></td>
  </tr>
  <tr><td class="xinxi_collect_title">中标金额</td><td>1.22百万元</td></tr>
  <tr><td class="xinxi_collect_title">采购单位</td><td>德 (略)</td></tr>
  <tr><td class="xinxi_collect_title">中标单位/候选人</td><td>点击查看</td></tr>
  <tr><td class="xinxi_collect_title">采购项目名称</td><td>光学相干断层成像及血管成像仪等一批医疗设备</td></tr>
</table>
</body></html>
"""

BIDCENTER_DETAIL_URL = 'https://www.bidcenter.com.cn/news-441066370-4.html'
OFFICIAL_URL = 'https://www.ccgp.gov.cn/cggg/dfgg/zbgg/202609/t20260909_24123456.htm'
PROJECT_ID = 'N513424202600004820260813001'
PROJECT_NAME = '光学相干断层成像及血管成像仪等一批医疗设备'

#: chinamae 检索结果列表：**逐字取自落盘的真实页面**
#: （``output/aggregator-chain/ac3-fixture-blocks.txt``，只压掉多余空白）。
#: 五条各自代表一种实测形态：普通采购公告、竞价结果公告、**带掩码星号的详情 URL**、
#: ``title`` 属性里夹着 ``<font>``/``<em>`` 高亮标签的行。
CHINAMAE_LIST_URL = 'https://www.chinamae.com/purchases-search/'
CHINAMAE_RESULT_HTML = """
<html><body><div class="list"><ul>
<li><div class="info"><i></i><a href="/purchases/province/1238" target="_blank" class="region"> [江西] </a><a href="/purchases/3b1ae404d47f9c0eb2988b3182d0ffe2.html" class="title" target="_blank" title="江西跃华2026年9月仪器校验询价采购项目 -第2次项目"> 江西跃华2026年9月仪器校验询价采购项目 -第2次项目 (<em style="color: #FF2626;">LDZX</em>...包含在正文中) </a></div><div class="source"><a href="/purchases/type/1" class="type" target="_blank"> 采购公告 </a><span class="time">2026-09-23</span></div></li>
<li><div class="info"><i></i><a href="/purchases/province/0" target="_blank" class="region"> [全国] </a><a href="/purchases/24d184b66c0cfd9f9689de2c82197595.html" class="title" target="_blank" title="华中农业大学－襄阳校区水产养殖基地建设－竞价结果公告"> 华中农业大学－襄阳校区水产养殖基地建设－竞价结果公告 (<em style="color: #FF2626;">LDZX</em>...包含在正文中) </a></div><div class="source"><a href="/purchases/type/3" class="type" target="_blank"> 结果公示 </a><span class="time">2026-08-03</span></div></li>
<li><div class="info"><i></i><a href="/purchases/province/464" target="_blank" class="region"> [辽宁] </a><a href="/purchases/c0119********d42362582fbb1a42b729.html" class="title" target="_blank" title="辽宁省三北重点生态工程区及周边地区林业有害生物防控能力提升项目设备采购结果公告"> 辽宁省三北重点生态工程区及周边地区林业有害生物防控能力提升项目设备采购结果公告 (<em style="color: #FF2626;">TG16</em>...包含在正文中) </a></div><div class="source"><a href="/purchases/type/3" class="type" target="_blank"> 结果公示 </a><span class="time">2025-11-07</span></div></li>
<li><div class="info"><i></i><a href="/purchases/province/3108" target="_blank" class="region"> [新疆] </a><a href="/purchases/b769c83f1dd9c5011d97bb65ee12375d.html" class="title" target="_blank" title="<font color='#0066FF'>[新疆维吾尔自治区·和田地区·民丰县]</font><font color='#CC00FF'>[公开招标]</font><font color='#FF6600'>[材料设备]</font>民丰县防沙治沙实验室建设项目（包一）中标 (成交) 结果公告"> <font color='#0066FF'>[新疆维吾尔自治区·和田地区·民丰县]</font><font color='#CC00FF'>[公开招标]</font><font color='#FF6600'>[材料设备]</font>民丰县防沙治沙实验室建设项目（包一）中标 (成交) 结果公告 (<em style="color: #FF2626;">FA20</em>...包含在正文中) </a></div><div class="source"><a href="/purchases/type/3" class="type" target="_blank"> 结果公示 </a><span class="time">2026-06-29</span></div></li>
<li><div class="info"><i></i><a href="/purchases/province/3108" target="_blank" class="region"> [新疆] </a><a href="/purchases/f4d2514cd0f84bb7f6748087f706b871.html" class="title" target="_blank" title="[政采云]泽普县现代种业提升工程项目（二次）中标(成交)结果公告"> [政采云]泽普县现代种业提升工程项目（二次）中标(成交)结果公告 (<em style="color: #FF2626;">DHG-</em>...包含在正文中) </a></div><div class="source"><a href="/purchases/type/3" class="type" target="_blank"> 结果公示 </a><span class="time">2026-09-22</span></div></li>
</ul></div></body></html>
"""

CHINAMAE_MASKED_URL = 'https://www.chinamae.com/purchases/c0119********d42362582fbb1a42b729.html'


class FakeRenderer:
    """按 URL 片段分派的假渲染器；记录调用，可配置成抛错。"""

    def __init__(self, pages=None, error=None):
        self.pages = pages or {}
        self.error = error
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        for marker, payload in self.pages.items():
            if marker in url:
                return payload
        return None


def _shop120_discovery(pages=None, error=None, enabled=True):
    renderer = FakeRenderer(pages, error=error)
    discovery = AggregatorDiscovery(renderer=renderer, enabled=enabled,
                                    sites=(AGGREGATOR_120SHOP,))
    return discovery, renderer


class FakeFetcher:
    """假静态读取器：记录调用，可配置成抛错或返回固定载荷。"""

    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = []

    def __call__(self, url, fields=None, headers=None):
        self.calls.append((url, dict(fields or {}), dict(headers or {})))
        if self.error is not None:
            raise self.error
        return self.payload


def _chinamae_discovery(payload=CHINAMAE_RESULT_HTML, error=None):
    fetcher = FakeFetcher(payload, error=error)
    discovery = AggregatorDiscovery(fetcher=fetcher, enabled=True,
                                    sites=(AGGREGATOR_CHINAMAE,))
    return discovery, fetcher


def _bidcenter_lead(**overrides):
    base = dict(
        title='光学相干断层成像及血管成像仪等一批医疗设备中标成交结果公告',
        url=BIDCENTER_DETAIL_URL,
        project_id=PROJECT_ID,
        project_name=PROJECT_NAME,
        purchaser='德昌县人民医院',
        supplier='四川国康医疗器械有限公司',
        amount='1.22百万元',
        aggregator=AGGREGATOR_BIDCENTER,
        notice_date='2026-09-09',
    )
    base.update(overrides)
    return Lead(**base)


def _official_hit(url=OFFICIAL_URL):
    return {
        'url': url,
        'project_id': PROJECT_ID,
        'project_name': PROJECT_NAME,
        'body_text': ('德昌县人民医院光学相干断层成像及血管成像仪等一批医疗设备'
                      '中标（成交）结果公告 项目编号：%s' % PROJECT_ID),
    }


# ---------------------------------------------------------------------------
# 1. 降级：没有渲染器 / 渲染失败 / 密文 / 默认关闭，都不能抛异常
# ---------------------------------------------------------------------------


def test_discover_without_renderer_returns_empty_leads_and_partial_reason():
    discovery = AggregatorDiscovery(enabled=True)
    result = discovery.discover(SHOP_120_KEYWORD)
    assert result.leads == []
    assert result.partial is True
    assert '渲染器' in result.partial_reason


def test_discover_is_disabled_by_default():
    discovery = AggregatorDiscovery()
    assert discovery.enabled is False
    result = discovery.discover(SHOP_120_KEYWORD)
    assert result.leads == []
    assert result.partial is True
    assert '未启用' in result.partial_reason


def test_renderer_timeout_degrades_to_partial_without_raising():
    discovery, _renderer = _shop120_discovery(error=TimeoutError('render timed out'))
    result = discovery.discover(SHOP_120_KEYWORD)
    assert result.leads == []
    assert result.partial is True
    assert '超时' in result.partial_reason
    assert result.channels[AGGREGATOR_120SHOP] == 'renderer_error'


def test_ciphertext_response_degrades_to_partial_without_raising():
    # 医价网的 XHR 接口实测返回的就是一段 hex 密文，没有渲染后 DOM 时不该硬解析。
    cipher = {'text': 'a1b2c3d4' * 32, 'html': ''}
    discovery, _renderer = _shop120_discovery({AGGREGATOR_120SHOP: cipher})
    result = discovery.discover(SHOP_120_KEYWORD)
    assert result.leads == []
    assert result.partial is True
    assert '密文' in result.partial_reason
    assert result.channels[AGGREGATOR_120SHOP] == 'ciphertext'


def test_renderer_returning_none_is_reported_as_empty_page():
    discovery, _renderer = _shop120_discovery({AGGREGATOR_120SHOP: None})
    result = discovery.discover(SHOP_120_KEYWORD)
    assert result.leads == []
    assert result.partial is True


# ---------------------------------------------------------------------------
# 2. 解析：能用真实页面结构解析出线索，字段对得上
# ---------------------------------------------------------------------------


def test_discover_parses_120shop_result_table():
    page = {'text': '', 'html': SHOP_120_RESULT_HTML}
    discovery, renderer = _shop120_discovery({AGGREGATOR_120SHOP: page})

    result = discovery.discover(SHOP_120_KEYWORD)

    assert len(result.leads) == 3
    assert result.partial is False, result.partial_reason
    assert renderer.calls and 'searchContent=XKA-2200' in renderer.calls[0]

    first = result.leads[0]
    assert first.title == '血型血清专用离心机（医用离心机）'
    assert first.supplier == '新疆贝昇康润生物有限公司'   # 行尾的「7次」被去掉
    assert first.purchaser == '和田地区人民医院'
    assert first.notice_date == '2026-03'
    assert first.amount == '1.07万'                      # 展示文本，不是价格证据
    assert first.aggregator == AGGREGATOR_120SHOP
    # 医价网不提供项目编号/项目名称/详情页链接：读不到就空，不猜。
    assert first.project_id == ''
    assert first.project_name == ''
    assert first.url == ''

    # 型号列带空格的真实形态（XKA - 2200 / SJW-I型/XKA-2200）也认得出来。
    titles = [lead.title for lead in result.leads]
    assert '221301医用离心机I' in titles
    assert '血型专用离心机' in titles
    # 型号对不上的行必须被丢掉（窄规则）。
    assert '超低温冷冻储存箱' not in titles


def test_masked_amount_is_dropped_instead_of_guessed():
    page = {'text': '', 'html': SHOP_120_RESULT_HTML}
    discovery, _renderer = _shop120_discovery({AGGREGATOR_120SHOP: page})
    result = discovery.discover(SHOP_120_KEYWORD)

    masked = [lead for lead in result.leads if lead.title == '血型专用离心机']
    assert len(masked) == 1
    assert masked[0].amount == ''          # 「￥411.# 万元」是脱敏值，当空
    assert masked[0].project_id == ''


def test_bidcenter_search_and_detail_pages_are_parsed_separately():
    search_page = {'text': '', 'html': BIDCENTER_SEARCH_HTML}
    discovery = AggregatorDiscovery(renderer=FakeRenderer({AGGREGATOR_BIDCENTER: search_page}),
                                    enabled=True, sites=(AGGREGATOR_BIDCENTER,))

    result = discovery.discover(SHOP_120_KEYWORD)

    assert len(result.leads) == 2
    assert result.leads[0].url == BIDCENTER_DETAIL_URL
    assert result.leads[0].title == '光学相干断层成像及血管成像仪等一批医疗设备中标成交结果公告'
    assert result.leads[0].project_id == ''      # 列表页没有编号，就是空
    # 列表外的推荐位链接不该进线索。
    assert all('news-1-4.html' not in lead.url for lead in result.leads)

    detail = discovery.leads_from_page(BIDCENTER_DETAIL_URL,
                                       {'text': '', 'html': BIDCENTER_DETAIL_HTML})
    assert len(detail) == 1
    lead = detail[0]
    assert lead.title == '光学相干断层成像及血管成像仪等一批医疗设备中标成交结果公告'
    assert lead.project_name == '光学相干断层成像及血管成像仪等一批医疗设备'
    assert lead.amount == '1.22百万元'
    assert lead.notice_date == '2026-09-09'
    # 服务端脱敏的字段一律当空：拿「点击查看」和「德 (略)」去复核只会造出假命中。
    assert lead.project_id == ''
    assert lead.purchaser == ''
    assert lead.supplier == ''


def test_120shop_detail_page_is_parsed_by_column_name_not_column_order():
    discovery = AggregatorDiscovery(enabled=True)
    leads = discovery.leads_from_page(SHOP_120_DETAIL_URL,
                                      {'text': '', 'html': SHOP_120_DETAIL_HTML},
                                      keyword=SHOP_120_KEYWORD)

    assert len(leads) == 1
    lead = leads[0]
    # 详情页的列序与列表页相反：列名定位才拿得到正确的人。
    assert lead.purchaser == '和田地区人民医院'
    assert lead.supplier == '新疆贝昇康润生物有限公司'
    assert lead.amount == '1.07万'
    assert lead.notice_date == '2026-03-12'
    assert lead.url == SHOP_120_DETAIL_URL


def test_leads_from_page_on_failed_render_returns_empty_list():
    discovery = AggregatorDiscovery(enabled=True)
    assert discovery.leads_from_page(BIDCENTER_DETAIL_URL, None) == []
    assert discovery.leads_from_page(BIDCENTER_DETAIL_URL, {'text': 'a1b2' * 40}) == []


# ---------------------------------------------------------------------------
# 2b. chinamae：静态列表页，不需要渲染器
# ---------------------------------------------------------------------------


def test_chinamae_list_is_parsed_from_a_static_response_without_a_renderer():
    discovery, fetcher = _chinamae_discovery()
    result = discovery.discover('LDZX-50L')

    assert result.partial is False, result.partial_reason
    assert result.channels[AGGREGATOR_CHINAMAE] == 'ok'
    assert len(result.leads) == 5
    # 站点要的是 POST 表单字段 scout，来路是站点首页；没有浏览器参与。
    assert fetcher.calls == [(CHINAMAE_LIST_URL, {'scout': 'LDZX-50L'},
                              {'Referer': 'https://www.chinamae.com/'})]

    first = result.leads[0]
    assert first.aggregator == AGGREGATOR_CHINAMAE
    assert first.region == '江西'
    assert first.notice_type == '采购公告'
    assert first.notice_date == '2026-09-23'
    assert first.url == 'https://www.chinamae.com/purchases/3b1ae404d47f9c0eb2988b3182d0ffe2.html'
    # 列表页没有项目编号列：读不到就空，不猜。
    assert first.project_id == ''
    # 项目名由标题推导（剥掉公告类型后缀），站点原文留在 title 里。
    assert first.title == '江西跃华2026年9月仪器校验询价采购项目 -第2次项目'
    assert first.project_name == '江西跃华2026年9月仪器校验询价采购项目 -第2次项目'
    assert '竞价结果公告' in result.leads[1].title
    assert result.leads[1].project_name == '华中农业大学－襄阳校区水产养殖基地建设'


def test_chinamae_masked_detail_url_is_kept_and_marked_as_a_lead():
    """详情 URL 带服务端掩码（172 行里实测 1 行）——线索照收，但绝不拿去当正文地址。"""
    discovery, _fetcher = _chinamae_discovery()
    result = discovery.discover('TG16-WS')
    masked = [lead for lead in result.leads if '*' in lead.url]

    assert [lead.url for lead in masked] == [CHINAMAE_MASKED_URL]
    assert masked[0].project_name == ('辽宁省三北重点生态工程区及周边地区林业有害生物防控能力'
                                      '提升项目设备')


def test_chinamae_title_markup_is_stripped_before_deriving_the_project_name():
    """站点往 title 属性里塞高亮标签：不剥掉，项目名与查询词就会带着标签去官方站。"""
    discovery, _fetcher = _chinamae_discovery()
    result = discovery.discover('FA2004')
    titles = {lead.url.split('/')[-1]: lead for lead in result.leads}
    tagged = titles['b769c83f1dd9c5011d97bb65ee12375d.html']

    assert '<' not in tagged.title and 'font' not in tagged.title
    assert tagged.title.startswith('[')
    assert '[公开招标]' in tagged.title and '[材料设备]' in tagged.title
    # ``project_name`` 保留站点原文（前缀照样在里面）……
    assert tagged.project_name.endswith('民丰县防沙治沙实验室建设项目（包一）')
    # ……身份比对时再剥：正文里只要出现"民丰县防沙治沙实验室建设项目"就算同项目。
    body = '民丰县防沙治沙实验室建设项目（包一）中标（成交）结果公告 中标金额：1元'
    outcome = discovery.official_recheck([tagged], searcher=lambda query: [
        {'url': OFFICIAL_URL, 'project_id': '', 'project_name': '', 'body_text': body}])
    assert outcome.verified and outcome.verified[0].official_url == OFFICIAL_URL


def test_chinamae_row_with_a_zhengcaiyun_tag_keeps_the_project_name_usable():
    """``[政采云]…（二次）``：站点自己的标签留在 title 里，推导名要能对上官方正文。

    这一条是 ``设计规范`` 的 idx=103（新疆泽普 DHG-9070A，人工读到 2900 元），
    它的推导名在**主候选名**里带着 ``[政采云]`` 与 ``（二次）``：走严格包含必失手，
    所以身份比对还准备了"剥掉标题装饰"的候选名（见 ``_identity_names``）。
    """
    discovery, _fetcher = _chinamae_discovery()
    lead = [x for x in discovery.discover('DHG-9070A').leads
            if x.url.endswith('f4d2514cd0f84bb7f6748087f706b871.html')][0]
    assert lead.project_name == '[政采云]泽普县现代种业提升工程项目（二次）'

    body = ('泽普县现代种业提升工程项目中标(成交)结果公告 '
            '项目名称：泽普县现代种业提升工程项目 中标金额：2900元')
    outcome = discovery.official_recheck([lead], searcher=lambda query: [
        {'url': OFFICIAL_URL, 'project_id': '', 'project_name': '', 'body_text': body}])

    assert outcome.verified and outcome.verified[0].official_url == OFFICIAL_URL


def test_chinamae_leads_carry_no_price_keys():
    discovery, _fetcher = _chinamae_discovery()
    for lead in discovery.discover('LDZX-50L').leads:
        data = lead.to_dict()
        assert [key for key in data if key.startswith('price')] == []
        assert data['evidence_grade'] == 'secondary'
        assert data['source_kind'] == 'aggregator'


def test_chinamae_fetch_failure_degrades_to_fetcher_error_without_raising():
    discovery, _fetcher = _chinamae_discovery(error=TimeoutError('read timed out'))
    result = discovery.discover('LDZX-50L')

    assert result.leads == []
    assert result.partial is True
    assert '超时' in result.partial_reason
    assert result.channels[AGGREGATOR_CHINAMAE] == 'fetcher_error'


def test_chinamae_without_a_fetcher_says_what_is_missing():
    discovery = AggregatorDiscovery(enabled=True, sites=(AGGREGATOR_CHINAMAE,))
    result = discovery.discover('LDZX-50L')

    assert result.leads == []
    assert result.partial is True
    assert '静态读取器' in result.partial_reason


def test_the_renderer_sites_still_ask_for_a_renderer():
    """新加的静态通道不能把既有两站的降级说明改掉：它们仍然要渲染器。"""
    discovery = AggregatorDiscovery(enabled=True)
    result = discovery.discover('XKA-2200')

    assert result.partial is True
    assert '渲染器' in result.partial_reason
    assert 'renderer' not in result.channels
    assert result.channels[AGGREGATOR_120SHOP] == 'empty'
    assert result.channels[AGGREGATOR_BIDCENTER] == 'empty'
    assert result.channels[AGGREGATOR_CHINAMAE] == 'empty'


def test_chinamae_detail_pages_are_deliberately_not_parsed():
    """详情页在服务端把 7 个字段打成 ******：本模块**有意**不解析它。"""
    discovery, _fetcher = _chinamae_discovery()
    detail = 'https://www.chinamae.com/purchases/3b1ae404d47f9c0eb2988b3182d0ffe2.html'
    page = {'text': '', 'html': '<html><body>规格型号：******</body></html>'}

    assert discovery.leads_from_page(detail, page, keyword='LDZX-50L') == []
    # 列表页仍然照常解析。
    assert discovery.leads_from_page(CHINAMAE_LIST_URL,
                                     {'text': '', 'html': CHINAMAE_RESULT_HTML},
                                     keyword='LDZX-50L')


# ---------------------------------------------------------------------------
# 3. 官方复核：命中才算数，不命中就丢
# ---------------------------------------------------------------------------


def test_official_recheck_marks_verified_and_records_official_url():
    discovery = AggregatorDiscovery(enabled=True)
    queries = []

    def searcher(query):
        queries.append(query)
        return [_official_hit()]

    outcome = discovery.official_recheck([_bidcenter_lead()], searcher=searcher)

    assert queries == [PROJECT_ID]                    # 用项目编号去复核
    assert len(outcome.verified) == 1
    assert outcome.gaps == []
    assert outcome.searched == 1
    verified = outcome.verified[0]
    assert verified.recheck_status == 'verified'
    assert verified.official_url == OFFICIAL_URL
    # 复核命中也不改变非一手标记：官方原文要主链自己去抓。
    assert verified.evidence_grade == 'secondary'
    assert verified.source_kind == 'aggregator'


def test_official_recheck_requires_identity_match_not_just_a_hit():
    discovery = AggregatorDiscovery(enabled=True)

    def searcher(query):
        # 官方搜到的是别的项目，编号对不上。
        return [{'url': OFFICIAL_URL, 'project_id': 'OTHER-PROJECT-ID',
                 'project_name': '另一个项目', 'body_text': '另一个项目 中标（成交）结果公告'}]

    outcome = discovery.official_recheck([_bidcenter_lead()], searcher=searcher)

    assert outcome.verified == []
    assert len(outcome.gaps) == 1
    assert outcome.partial is True


def test_official_recheck_drops_leads_when_searcher_finds_nothing():
    discovery = AggregatorDiscovery(enabled=True)
    outcome = discovery.official_recheck([_bidcenter_lead()], searcher=lambda query: [])

    assert outcome.verified == []
    assert len(outcome.gaps) == 1
    assert PROJECT_ID in outcome.gaps[0]
    assert outcome.partial is True


def test_official_recheck_without_searcher_keeps_leads_out_of_results():
    discovery = AggregatorDiscovery(enabled=True)
    outcome = discovery.official_recheck([_bidcenter_lead()], searcher=None)

    assert outcome.verified == []
    assert len(outcome.gaps) == 1
    assert outcome.searched == 0
    assert outcome.partial is True


def test_official_recheck_error_becomes_gap_and_stops_querying():
    discovery = AggregatorDiscovery(enabled=True)
    calls = []

    def searcher(query):
        calls.append(query)
        raise RuntimeError('CCGP rate limited')

    leads = [_bidcenter_lead(), _bidcenter_lead(project_id='N513424202600004820269999999')]
    outcome = discovery.official_recheck(leads, searcher=searcher)

    # 报错时不再继续查剩余线索，避免在限流窗口里雪上加霜。
    assert len(calls) == 1
    assert outcome.verified == []
    assert len(outcome.gaps) == 2
    assert all('不再查询' in gap for gap in outcome.gaps)
    assert outcome.searched == 0


def test_120shop_lead_rechecks_by_equipment_title_when_no_project_fields():
    discovery = AggregatorDiscovery(enabled=True)
    lead = Lead(title='血型血清专用离心机（医用离心机）',
                supplier='新疆贝昇康润生物有限公司',
                amount='1.07万', aggregator=AGGREGATOR_120SHOP)
    queries = []

    def searcher(query):
        queries.append(query)
        return [{'url': OFFICIAL_URL, 'project_id': '', 'project_name': '',
                 'body_text': ('和田地区人民医院医用离心机采购项目中标（成交）结果公告 '
                               '采购标的：血型血清专用离心机（医用离心机）')}]

    outcome = discovery.official_recheck([lead], searcher=searcher)

    assert queries == ['血型血清专用离心机（医用离心机）']
    assert len(outcome.verified) == 1
    assert outcome.verified[0].official_url == OFFICIAL_URL


def test_official_recheck_prefers_award_leads_and_still_queries_the_rest():
    """结果类线索先复核（``设计规范`` ③：17/20 对 0/2），但其余线索**不丢**。"""
    discovery = AggregatorDiscovery(enabled=True)
    purchase = Lead(title='某某单位设备采购公告', project_name='某某单位设备采购项目',
                    aggregator=AGGREGATOR_CHINAMAE, notice_type='采购公告')
    award = Lead(title='某某单位设备中标（成交）结果公告', project_name='某某单位设备采购项目',
                 aggregator=AGGREGATOR_CHINAMAE, notice_type='结果公示')
    seen = []

    def searcher(query):
        seen.append(query)
        return [{'url': OFFICIAL_URL, 'project_id': '', 'project_name': '',
                 'body_text': '某某单位设备采购项目中标（成交）结果公告'}]

    outcome = discovery.official_recheck([purchase, award], searcher=searcher)

    # 两条线索的查询词相同：第二条被合并，只打了一次官方接口（见下一个测试）。
    assert seen == ['某某单位设备采购项目']
    assert outcome.searched == 1
    assert len(outcome.gaps) == 1 and '不重复请求' in outcome.gaps[0]
    assert len(outcome.verified) == 1
    # 留下来的是结果类那一条——顺序只影响"谁先被复核"，判据一个字没改。
    assert '结果公告' in outcome.verified[0].title


def test_official_recheck_sends_award_leads_first_even_when_listed_last():
    discovery = AggregatorDiscovery(enabled=True)
    leads = [
        Lead(title='项目甲采购公告', project_name='项目甲', aggregator=AGGREGATOR_CHINAMAE,
             notice_type='采购公告'),
        Lead(title='项目乙中标结果公告', project_name='项目乙', aggregator=AGGREGATOR_CHINAMAE,
             notice_type='结果公示'),
        Lead(title='项目丙竞价公告', project_name='项目丙', aggregator=AGGREGATOR_CHINAMAE,
             notice_type='采购公告'),
    ]
    seen = []

    outcome = discovery.official_recheck(leads, searcher=lambda q: seen.append(q) or [], limit=2)

    assert seen == ['项目乙', '项目甲']
    assert len([gap for gap in outcome.gaps if '上限' in gap]) == 1
    assert any('项目丙' in gap and '上限' in gap for gap in outcome.gaps)


def test_official_recheck_limit_records_every_skipped_lead_as_a_gap():
    """被上限挡下的线索必须记成"没去查"，不能和"查了没有"混成一句话。"""
    discovery = AggregatorDiscovery(enabled=True)
    leads = [Lead(title='项目%s中标结果公告' % name, project_name='项目%s' % name,
                  aggregator=AGGREGATOR_CHINAMAE, notice_type='结果公示')
             for name in ('甲', '乙', '丙')]
    calls = []

    outcome = discovery.official_recheck(leads, searcher=lambda q: calls.append(q) or [], limit=1)

    assert calls == ['项目甲']
    assert outcome.searched == 1
    assert len(outcome.gaps) == 3          # 未命中的 1 条 + 没去查的 2 条
    capped = [gap for gap in outcome.gaps if '上限' in gap]
    assert len(capped) == 2
    assert all('没有**去官方查' in gap or '没有' in gap for gap in capped)


def test_official_recheck_prefers_an_award_result_when_several_rows_match():
    """官方侧返回多条都为真时，先挑带成交字样的那条看（身份判据不变）。"""
    discovery = AggregatorDiscovery(enabled=True)
    lead = Lead(title='某某单位设备中标（成交）结果公告', project_name='某某单位设备采购项目',
                aggregator=AGGREGATOR_CHINAMAE, notice_type='结果公示')
    tender_url = OFFICIAL_URL + '?tender'
    award_url = OFFICIAL_URL + '?award'

    outcome = discovery.official_recheck([lead], searcher=lambda query: [
        {'url': tender_url, 'title': '某某单位设备采购项目招标公告', 'project_id': '',
         'project_name': '', 'body_text': '某某单位设备采购项目招标公告 预算 100 万元'},
        {'url': award_url, 'title': '某某单位设备采购项目中标（成交）结果公告', 'project_id': '',
         'project_name': '', 'body_text': '某某单位设备采购项目中标（成交）结果公告 中标金额 1 元'},
    ])

    assert outcome.verified[0].official_url == award_url


def test_official_recheck_does_not_confirm_on_a_short_generic_overlap():
    """重合兜底那把尺子是 ≥10 个连续字：共 9 个字不算同项目。"""
    discovery = AggregatorDiscovery(enabled=True)
    lead = Lead(title='某某县人民医院医疗设备采购项目中标结果公告',
                project_name='某某县人民医院医疗设备采购项目',
                aggregator=AGGREGATOR_CHINAMAE, notice_type='结果公示')
    # 正文只与项目名重合 9 个连续字（"县人民医院医疗设备"），第 10 个字就岔开了。
    body = '另一个县人民医院医疗设备公告 中标（成交）结果公告'

    outcome = discovery.official_recheck([lead], searcher=lambda query: [
        {'url': OFFICIAL_URL, 'project_id': '', 'project_name': '', 'body_text': body}])

    assert outcome.verified == []
    assert len(outcome.gaps) == 1


def test_official_recheck_keeps_unmatched_chinamae_leads_out_of_the_result():
    """复核不上就是缺口，不是"官方没有明细"——两种话必须分得开。"""
    discovery, _fetcher = _chinamae_discovery()
    leads = discovery.discover('LDZX-50L').leads
    outcome = discovery.official_recheck(leads, searcher=lambda query: [], limit=2)

    assert outcome.verified == []
    assert outcome.searched == 2
    assert len(outcome.gaps) == 2 + len(leads) - 2
    assert all('官方侧没有复核到' in gap or '上限' in gap or '不重复请求' in gap
               for gap in outcome.gaps)


# ---------------------------------------------------------------------------
# 4. 核心约束：聚合站的金额永远进不了价格通道
# ---------------------------------------------------------------------------


def test_aggregator_amount_never_becomes_price_evidence():
    page = {'text': '', 'html': SHOP_120_RESULT_HTML}
    discovery, _renderer = _shop120_discovery({AGGREGATOR_120SHOP: page})
    result = discovery.discover(SHOP_120_KEYWORD)
    lead = result.leads[0]

    assert PRICE_EVIDENCE_ELIGIBLE is False

    # 聚合站页面上印的金额只作为展示文本保留……
    assert lead.amount == '1.07万'
    # ……但记录里没有任何能进 run['prices'] 的价格字段。
    data = lead.to_dict()
    assert [key for key in data if key.startswith('price')] == []
    assert 'price_amount' not in data
    assert 'price_quote' not in data
    assert data['evidence_grade'] == 'secondary'
    assert data['source_kind'] == 'aggregator'
    # 把线索换成价格证据的唯一入口恒为 None。
    assert price_evidence_from(lead) is None

    # 就算官方复核命中了，线索本身仍然不是价格证据。
    def searcher(query):
        return [_official_hit()]

    outcome = discovery.official_recheck([_bidcenter_lead()], searcher=searcher)
    verified = outcome.verified[0]
    assert verified.official_url == OFFICIAL_URL
    assert price_evidence_from(verified) is None
    assert [key for key in verified.to_dict() if key.startswith('price')] == []
    assert verified.evidence_grade == 'secondary'


def test_discovery_result_serialises_without_price_channel():
    page = {'text': '', 'html': SHOP_120_RESULT_HTML}
    discovery, _renderer = _shop120_discovery({AGGREGATOR_120SHOP: page})
    payload = discovery.discover(SHOP_120_KEYWORD).to_dict()

    assert payload['keyword'] == SHOP_120_KEYWORD
    assert payload['leads']
    for lead in payload['leads']:
        assert not [key for key in lead if key.startswith('price')]
        assert lead['evidence_grade'] == 'secondary'


def test_the_two_aggregator_hosts_can_never_be_official_publishers():
    """把这两个域名登记进 source_authority，它们的页面就永远拿不到官方 tier。

    这是「报告里不得出现聚合站转载作为价格来源」的第一道保险：不靠调用方自觉，
    而是让 tier 判定本身把它们挡在外面。采招网的脱敏在服务端（项目编号写成
    「点击查看」、采购单位写成「德 （略） 」、正文数字变 ``N#8``），渲染读 DOM
    也救不回来，所以这道理必须硬。官方站不受影响。
    """
    body = ('中标结果公告\n项目编号：ABC-2026-001\n采购人：某某医院\n'
            '中标供应商：某某公司\n中标金额：150000元\n评审专家：张三')

    for host in ('www.120shop.cn', 'search.bidcenter.com.cn'):
        assert is_marketplace_or_aggregator(host) is True
        assert publisher_tier(host, body) is None
        assert publication_tier(host, body) is None

    # 同一段正文落在官方站上仍然算数——登记不能误伤。
    assert is_marketplace_or_aggregator('www.ccgp.gov.cn') is False
    assert publisher_tier('www.ccgp.gov.cn', body) == LOCAL_AUTHORITY


# ---------------------------------------------------------------------------
# 接进采购主链：聚合站只当发现渠道，页面永不进 run['sources']
# ---------------------------------------------------------------------------

class _RenderedPage:
    """形状与 ``infrastructure.page_reader.PageResult`` 一致的假渲染结果。"""

    def __init__(self, text, html):
        self.text = text
        self.payload = html.encode('utf-8')


class _BrowserReader:
    """冒充 ``PublicPageReader``：只有它才该被当成渲染器。"""

    def __init__(self, html, text=''):
        self.html = html
        self.text = text
        self.reads = []

    def read_browser(self, url, timeout=25):
        self.reads.append(url)
        return _RenderedPage(self.text, self.html)


def _fallback_state(run):
    """把 run 摆成「价格车道跑完、价格确实是空的」，并清掉夹具那一轮的兜底留痕。

    夹具（``tests/test_reliability_development.py::research``）走的是**真**研究流程：
    离线通道什么都搜不到，于是这一轮本来就会触发一次兜底并把留痕写进 run。要再测
    「触发」这件事，就必须先把那份留痕清掉——它同时是"同一轮最多花一次钱"的闸门。
    """
    run['prices'] = []
    run['lanes']['price'] = 'completed'
    run.pop('aggregator_discovery', None)
    return run


def test_the_aggregator_lane_stays_shut_when_the_run_already_has_a_price(research):  # noqa: F811
    """产品默认路径是兜底：直查拿到了价格，这条通道一次都不该走（连渲染器都不碰）。

    这是「成本只花在真正需要它的任务上」那一条的落点：CCGP 按型号直查本来就弱，
    对所有任务默认打开等于每个任务都多打一轮 CCGP 站内搜索，而那条线有 300 秒冷却。
    """
    service, run, _provider, _client = research
    reader = _BrowserReader(SHOP_120_RESULT_HTML)
    service.search.reader = reader
    run['prices'] = [{'kind': 'award', 'model': run['baseline']['model'], 'unit_price': 1200}]
    run['lanes']['price'] = 'completed'
    run.pop('aggregator_discovery', None)
    before = [dict(source) for source in run['sources']]

    service.aggregator_leads(run, price_lane_finished=True)

    assert 'aggregator_discovery' not in run
    assert reader.reads == []
    assert run['sources'] == before


def test_the_aggregator_lane_runs_as_a_fallback_when_the_direct_query_priced_nothing(research):  # noqa: F811
    """直查跑完（``lanes['price'] == 'completed'``）而 ``run['prices']`` 仍为空 → 兜底。

    判据读的是**落地结果**，不是"某一次检索没命中"；这条用例同时把触发原因与
    "触发那一刻有几条价格"钉在任务数据里（留痕不是隐形的）。
    """
    service, run, _provider, _client = research
    # 夹具页面的型号换成这一轮基准的型号，否则解析阶段就按关键词滤掉了。
    reader = _BrowserReader(SHOP_120_RESULT_HTML.replace('XKA-2200', 'Q10'))
    service.search.reader = reader
    service.verify_publishers = lambda *args, **kwargs: None
    _fallback_state(run)

    service.aggregator_leads(run)

    trace = run['aggregator_discovery']
    assert trace['trigger'] == 'no_price_fallback'
    assert trace['prices_at_trigger'] == 0
    assert trace['lanes_at_trigger']['price'] == 'completed'
    assert trace['channels'][AGGREGATOR_120SHOP] == 'ok'
    assert trace['leads'] == 2      # 夹具页面里带这个型号的两行（另一行是空格写法）
    assert reader.reads, '兜底被触发了，就应该真的去发现渠道读过页面'
    # 同一轮不重复花钱：第二次调用连页面都不再读。
    reads = list(reader.reads)
    service.aggregator_leads(run)
    assert reader.reads == reads


def test_without_a_renderer_the_lane_degrades_without_touching_the_run(research):  # noqa: F811
    """发现渠道整个失败时只记一条缺口：主链照常，来源一条不多一条不少。

    ``run['aggregator_lead_discovery']`` 是显式旁路，触发原因照实记成 ``forced``——
    "这一轮是被人手动打开的"和"这一轮是直查没价兜底的"必须分得开。
    """
    service, run, _provider, _client = research
    run['aggregator_lead_discovery'] = True
    run.pop('aggregator_discovery', None)
    service.search.reader = object()          # 没有 read_browser，就不是渲染器
    before = [dict(source) for source in run['sources']]

    service.aggregator_leads(run)

    assert run['aggregator_leads'] == []
    assert run['sources'] == before
    assert run['aggregator_discovery']['trigger'] == 'forced'
    assert any('聚合站发现渠道未完成' in gap for gap in run['gaps'])


def test_a_new_round_may_spend_again_and_the_old_trace_moves_to_history(research):  # noqa: F811
    """闸门按**轮**算：用户点「继续」是新的一轮、额度也重算，上一轮不挡这一轮。

    上一轮的留痕不能被覆盖掉——"哪一轮因为直查没价走了兜底、拿回什么"是逐轮的账。
    """
    service, run, _provider, _client = research
    reader = _BrowserReader(SHOP_120_RESULT_HTML.replace('XKA-2200', 'Q10'))
    service.search.reader = reader
    service.verify_publishers = lambda *args, **kwargs: None
    run['prices'] = []
    run['lanes']['price'] = 'completed'
    run['aggregator_discovery'] = {'trigger': 'no_price_fallback', 'round': 1,
                                   'official_bodies_added': 3}
    run['round'] = 2

    service.aggregator_leads(run)

    assert reader.reads, '新一轮该重新走一次'
    assert run['aggregator_discovery']['round'] == 2
    assert [entry['round'] for entry in run['aggregator_discovery_history']] == [1]


def test_the_explicit_switch_still_means_run_it_now(research):  # noqa: F811
    """显式旁路（``run['aggregator_lead_discovery']``）判在闸门**前面**。

    它不能被一条自动兜底的留痕悄悄挡掉——挡掉就是一次看不见的空操作。代价是显式：
    同一轮里调用两次就是两次，这正是"显式"与"自动"的分界（自动兜底仍然一轮一次）。
    """
    service, run, _provider, _client = research
    reader = _BrowserReader(SHOP_120_RESULT_HTML.replace('XKA-2200', 'Q10'))
    service.search.reader = reader
    service.verify_publishers = lambda *args, **kwargs: None
    run['aggregator_lead_discovery'] = True
    run['prices'] = []
    run['lanes']['price'] = 'completed'
    run['aggregator_discovery'] = {'trigger': 'no_price_fallback', 'round': 1, 'leads': 0}

    service.aggregator_leads(run)

    assert reader.reads, '显式旁路该跑'
    assert run['aggregator_discovery']['trigger'] == 'forced'
    assert [entry['trigger'] for entry in run['aggregator_discovery_history']] == ['no_price_fallback']


def test_aggregator_pages_never_become_sources(research):  # noqa: F811
    """线索指向的官方原文可以进来源，聚合站自己的页面绝对不行。

    这是「报告里不得出现聚合站转载作为价格来源」在数据结构上的落点：聚合站 URL
    根本没有机会变成一条 source，也就没有机会走到价格通道。复核没过的线索直接丢。
    """
    service, run, _provider, _client = research
    run['aggregator_lead_discovery'] = True
    # 夹具页面的型号换成这一轮基准的型号，否则解析阶段就按关键词滤掉了。
    service.search.reader = _BrowserReader(SHOP_120_RESULT_HTML.replace('XKA-2200', 'Q10'))
    service.verify_publishers = lambda *args, **kwargs: None

    service.aggregator_leads(run)

    assert not [source for source in run['sources']
                if '120shop' in source['url'] or 'bidcenter' in source['url']]
    assert run['aggregator_leads'] == []
    assert any('聚合站' in gap for gap in run['gaps'])


# ---------------------------------------------------------------------------
# 5. 走通整条路：chinamae 发现 → CCGP 站内复核 → 官方原文进来源清单
# ---------------------------------------------------------------------------


class _OfficialPage:
    """形状与 ``infrastructure.page_reader.PageResult`` 一致的假正文读取结果。"""

    def __init__(self, html):
        self.payload = html.encode('utf-8')
        self.content_type = 'text/html; charset=utf-8'
        self.metadata = {'reader': 'http', 'snapshot_kind': 'response_bytes'}


class _HttpFormStub:
    """形状与 ``PublicWebSearchProvider.fetch_form`` 一致的假静态读取通道。"""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def fetch_form(self, url, fields=None, body=None, content_type=None, tls_profile=None,
                   method='POST', extra_headers=None, timeout=None):
        self.calls.append((url, dict(fields or {}), dict(extra_headers or {})))
        return self.payload, 'text/html; charset=utf-8'


class _StaticReader:
    """冒充 ``PublicPageReader``：既有共享正文通道（``read``），又有静态表单通道。

    **没有** ``read_browser``：这条夹具路线证明 chinamae 那一路不需要浏览器渲染。
    """

    def __init__(self, list_payload, pages):
        self.http = _HttpFormStub(list_payload)
        self.pages = pages
        self.reads = []

    def read(self, url, timeout=25, on_attempt=None, referer=None):
        self.reads.append(url)
        if on_attempt is not None:
            on_attempt()
        return _OfficialPage(self.pages[url])


class _CcgpProvider:
    """冒充 ``CCGPSearchProvider``：按查询词里的标记决定回不回结果，并记录查询。"""

    name = 'ccgp'

    def __init__(self, markers, rows):
        self.markers = tuple(markers)
        self.rows = rows
        self.queries = []

    def supports(self, query):
        return True

    def search(self, query):
        self.queries.append(query)
        if not any(marker in str(getattr(query, 'text', query)) for marker in self.markers):
            return []
        from backend.domain.search import SearchResult
        return [SearchResult(url=row['url'], title=row['title'], snippet='') for row in self.rows]


def test_chinamae_lead_found_on_ccgp_enters_the_source_list_for_the_price_lane(research):  # noqa: F811
    """``设计规范`` 那句"借助聚合站拿项目名、回官方拿正文"要真走通，就差这一步。

    走过：chinamae 静态列表拿线索（**不需要渲染器**）→ 用项目名去 **CCGP 站内搜索**
    → 挑成交类公告 → 读正文 → 身份判据命中 → **官方原文进 ``run['sources']``**。
    最后这一步是价格通道唯一看得见的地方：记录只进正文缓存的话，这条路走到最后
    一步会断在那里（正文抓到了、价格通道永远看不到它）。

    用的就是 ``设计规范`` 的 idx=103 那条（新疆泽普 DHG-9070A，人工读到 2900 元）：
    列表标题带 ``[政采云]`` 前缀与 ``（二次）``，严格包含判据必失手。
    """
    service, run, _provider, _client = research
    run['aggregator_lead_discovery'] = True
    model = run['baseline']['model']

    official = 'https://www.ccgp.gov.cn/cggg/dfgg/zbgg/202609/t20260909_24000001.htm'
    notice = ('泽普县现代种业提升工程项目（二次）中标（成交）结果公告 '
              '规格型号：%s 单价（元）：2900' % model)
    reader = _StaticReader(CHINAMAE_RESULT_HTML, {official: '<html><body>%s</body></html>' % notice})
    service.search.reader = reader
    ccgp = _CcgpProvider(['泽普'], [
        {'url': official + '?tender', 'title': '泽普县现代种业提升工程项目（二次）招标公告'},
        {'url': official, 'title': '泽普县现代种业提升工程项目（二次）中标（成交）结果公告'},
    ])
    service.search.providers.append(ccgp)
    service.verify_publishers = lambda *args, **kwargs: None

    service.aggregator_leads(run)

    # 静态列表解析出来的线索都在（型号对不上没关系：chinamae 是把命中落在正文里的）。
    assert run['aggregator_channels'][AGGREGATOR_CHINAMAE] == 'ok'
    assert len(run['aggregator_leads']) == 1
    assert run['aggregator_leads'][0]['official_url'] == official
    assert [key for key in run['aggregator_leads'][0] if key.startswith('price')] == []

    # 只在正文里出现过的那份公告进了来源清单，而且带的是官方地址。
    landed = [source for source in run['sources'] if source['url'] == official]
    assert len(landed) == 1
    assert landed[0]['body_status'] == 'fetched'
    assert '泽普县现代种业提升工程项目' in landed[0]['body_text']
    assert landed[0]['source_type'] == 'tender_document'
    assert landed[0]['purpose'] == 'discovery'
    # 正文只读一次：复核阶段读过，落来源时走的是正文缓存。
    assert reader.reads.count(official) == 1
    # 聚合站自己的地址一条都没有进来（复核用的是官方地址）。
    assert not [source for source in run['sources'] if 'chinamae' in source['url']]
    # CCGP 站内搜索确实被用上了，查询词是推导出来的项目名。
    assert any('泽普' in str(getattr(query, 'text', query)) for query in ccgp.queries)
    # 前三条线索（华中农业大学…）在 CCGP 上查不到，退化到既有通用通道，如实记缺口。
    assert any('官方侧没有复核到' in gap for gap in run['gaps'])


def test_one_lead_absent_from_ccgp_does_not_abandon_the_rest_of_the_batch(research, monkeypatch):  # noqa: F811
    """``设计规范`` 里那 23% 的一类：高校竞价网项目在 CCGP 站内**一条公告都没有**。

    2026-09-26 真跑暴露的失效方式不是"这条复核不到"，而是**把整批线索一起放弃**：
    这类线索退回通用通道时，``query`` 会因为"没有任何可用通道"抛
    ``ValueError('搜索接口失败，请先恢复服务')``，``official_recheck`` 把它读成
    "官方接口报错 = 限流停止信号"，于是同一批里**本来能在 CCGP 找到成交公告**的线索
    连试都不试。LDZX-50L 那一轮正是这样：巴彦县那条在离线冒烟里已经定位到
    ``t20260728_27019504``，真跑里整个型号一条都没复核到。

    该留住的性质有两条，本用例各断言一条：退回通用通道失败**只记这一条的缺口**、
    其余线索照旧去 CCGP；真正的限流信号（``aggregator_ccgp_search`` 里上抛的
    ``ProviderConfigurationError``）仍然是停止信号（另有用例覆盖）。
    """
    service, run, _provider, _client = research
    run['aggregator_lead_discovery'] = True
    model = run['baseline']['model']

    official = 'https://www.ccgp.gov.cn/cggg/dfgg/zbgg/202609/t20260909_24000001.htm'
    notice = ('泽普县现代种业提升工程项目（二次）中标（成交）结果公告 '
              '规格型号：%s 单价（元）：2900' % model)
    reader = _StaticReader(CHINAMAE_RESULT_HTML, {official: '<html><body>%s</body></html>' % notice})
    service.search.reader = reader
    # 五条线索里只有泽普那条能在 CCGP 站内查到，其余一律 0 条结果。
    ccgp = _CcgpProvider(['泽普'], [
        {'url': official, 'title': '泽普县现代种业提升工程项目（二次）中标（成交）结果公告'}])
    service.search.providers.append(ccgp)
    service.verify_publishers = lambda *args, **kwargs: None

    # 通用通道这一轮整个不可用：这就是真跑里那句 ValueError 的来源。
    def broken_query(*args, **kwargs):
        raise ValueError('搜索接口失败，请先恢复服务')
    monkeypatch.setattr(service, 'query', broken_query)

    # 记下这一轮到底有几条线索、查询词分别是什么（口径与 ``_recheck_query`` 一致）。
    seen = {}
    real_discover = AggregatorDiscovery.discover

    def spy_discover(self, keyword):
        found = real_discover(self, keyword)
        seen['queries'] = {lead.project_id or lead.project_name or lead.title
                           for lead in found.leads}
        return found

    monkeypatch.setattr(AggregatorDiscovery, 'discover', spy_discover)

    service.aggregator_leads(run)

    # 每一条线索都真的去过 CCGP：一条线索的通用通道失败没有把剩下的带走。
    assert seen['queries']
    assert len(ccgp.queries) == min(len(seen['queries']), AGGREGATOR_RECHECK_LIMIT)
    # 站内 0 条的那几条如实记缺口（不是记成"没有明细"）。
    assert any('通用通道这次也没走通' in gap for gap in run['gaps'])
    # 站内查得到的那条照旧走完整条路：官方原文进来源清单。
    landed = [source for source in run['sources'] if source['url'] == official]
    assert len(landed) == 1
    assert landed[0]['source_type'] == 'tender_document'


# ---------------------------------------------------------------------------
# 6. 兜底触发之后：官方原文进来源清单，还要**来得及**被价格车道再读一遍
# ---------------------------------------------------------------------------


def _ccgp_chain(service, run, official, notice, markers=('泽普',)):
    """把 chinamae 的静态列表 + 一份官方成交公告接到这一轮上。"""
    reader = _StaticReader(CHINAMAE_RESULT_HTML,
                           {official: '<html><body>%s</body></html>' % notice})
    service.search.reader = reader
    ccgp = _CcgpProvider(list(markers),
                         [{'url': official, 'title': '泽普县现代种业提升工程项目（二次）中标（成交）结果公告'}])
    service.search.providers.append(ccgp)
    service.verify_publishers = lambda *args, **kwargs: None
    return reader, ccgp


def test_a_price_that_arrives_through_the_fallback_is_read_by_the_late_rescan(research):  # noqa: F811
    """兜底进来的官方原文**来得及**被价格车道再读一遍——这就是那条接线。

    ``_execute`` 里的顺序是：价格车道的 ``prices()`` → 本车道的兜底（复核回来的
    官方原文进 ``run['sources']``）→「来源复核与结果汇总」里的
    ``_rescan_late_price_sources``。价格车道那次 ``prices()`` 只认它被调用那一刻的
    来源，所以兜底正文对它是"不存在"的；补读用的第二份额度只排除"这一轮真进过价格
    通道"的来源（``price_source_skips`` / ``price_source_cache_hits`` 里有记录的
    那些），兜底正文从没进过价格通道，按构造必然入选。这条用例把这条链子钉住：
    正文进来源清单 → 补扫真的读了它 → 价格落进 ``run['prices']``。
    """
    service, run, _provider, _client = research
    model = run['baseline']['model']
    official = 'https://www.ccgp.gov.cn/cggg/dfgg/zbgg/202609/t20260909_24000002.htm'
    excerpt = '规格型号：%s 数量：1 单价(元)：2900' % model
    notice = ('泽普县现代种业提升工程项目（二次）中标（成交）结果公告 ' + excerpt)
    _reader, _ccgp = _ccgp_chain(service, run, official, notice)
    _fallback_state(run)
    run['price_source_skips'] = []
    run['price_source_cache_hits'] = []

    service.aggregator_leads(run, price_lane_finished=True)

    assert run['aggregator_discovery']['official_bodies_added'] == 1
    assert official in [source['url'] for source in run['sources']]
    assert not [source for source in run['sources'] if 'chinamae' in source['url']]
    # 模型这一格换成桩：这条用例验的是"补扫有没有去读它、读到的价落不落进
    # ``run['prices']``"，而不是表格读取器的解析能力（那条在别处）。
    service.model = lambda run, prompt, data: {'prices': [
        {'brand': '', 'model': model, 'kind': 'award', 'amount': 2900, 'amount_unit': '元',
         'amount_scope': 'unit', 'quantity': 1, 'currency': 'CNY', 'date': '2026-09-09',
         'configuration': 'unknown', 'excerpt': excerpt}]}

    added = service._rescan_late_price_sources(run, run['baseline'])

    assert [row['unit_price'] for row in added] == [2900]
    assert [row['unit_price'] for row in run['prices']] == [2900]
    # 兜底正文不是"被额度挡掉的那种来源"，所以它也不该留一条 price_source_skips。
    assert [item for item in run['price_source_skips'] if item.get('url') == official] == []


def test_the_fallback_keeps_the_run_alive_when_the_search_budget_is_gone(research):  # noqa: F811
    """检索额度见底不是限流信号：记一条"没查"，不许把整轮从价格车道里抛出去。

    兜底触发点在价格车道末尾，在这里上抛 ``ResearchStopped`` 的代价是"这一轮没有
    价格"的任务停在半路——连替代品那一车道都跑不到。``query()`` 对同一件事的处理
    是 break + 留痕，CCGP 站内搜索这里必须一致。
    """
    service, run, _provider, _client = research
    model = run['baseline']['model']
    official = 'https://www.ccgp.gov.cn/cggg/dfgg/zbgg/202609/t20260909_24000003.htm'
    excerpt = '规格型号：%s 数量：1 单价(元)：2900' % model
    notice = '泽普县现代种业提升工程项目（二次）中标（成交）结果公告 ' + excerpt
    _reader, ccgp = _ccgp_chain(service, run, official, notice)
    _fallback_state(run)
    run['usage']['search'] = run['limits']['search']

    service.aggregator_leads(run, price_lane_finished=True)

    assert ccgp.queries == [], '额度已经见底，一次都不该发'
    assert any('额度已用完' in gap for gap in run['gaps'])
    assert official not in [source['url'] for source in run['sources']]
    assert run['aggregator_discovery']['trigger'] == 'no_price_fallback'
