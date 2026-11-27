"""教育／科研采购平台通道：骨架、站点差异、额度与采信边界。

这些测试全部用**构造的响应**，不打真实站点：产品通道的行为不该依赖某个站今天
是否在线（真实站点的行为另由端到端验证记录，见 ``设计规范-教育采购平台接入.md``；
2026-09-25 摘除教采云的原因与依据见 ``设计说明``）。
"""
import pytest

from backend.services import education_platform_sources as eps
from backend.services import procurement_research as pr
from backend.services.education_platform_sources import (
    EDUCATION_PLATFORM_LIMIT, QuotaReached, discover, hidden_fields,
    platform_for, redact_detail, registered_hosts, shape_detail)
from backend.services.procurement_tables import price_table_rows
from backend.services.research_sources import has_amount_evidence, price_regions
from backend.services.source_authority import is_local_authority_host, publication_tier


# ---------------------------------------------------------------------------
# 测试替身：回放构造的响应，不做任何网络访问
# ---------------------------------------------------------------------------

class _Recorder:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.transports = []

    def __call__(self, url, *, method='GET', data=None, headers=None,
                 on_request=None, timeout=None, transport=None):
        self.calls.append((method, url, data))
        self.transports.append(transport)
        if on_request is not None:
            on_request()
        if not self.script:
            raise AssertionError(f'没有为第 {len(self.calls)} 次请求准备响应: {url}')
        payload = self.script.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return payload

    def __getitem__(self, index):
        return self.calls[index]

    @property
    def count(self):
        return len(self.calls)

    @property
    def urls(self):
        return [call[1] for call in self.calls]


@pytest.fixture
def replay(monkeypatch):
    def install(responses):
        recorder = _Recorder(responses)
        monkeypatch.setattr(eps, 'request', recorder)
        return recorder
    return install


# ---------------------------------------------------------------------------
# 注册表：站点差异只写在一个地方
# ---------------------------------------------------------------------------

def test_registry_recognizes_its_own_hosts_and_nothing_else():
    # 两个数据集同主机、不同端口，主机名集合因此只多出一条去重后的名字。
    assert registered_hosts() == {'gdegp.gds.edu.cn'}
    for url, key in [('https://gdegp.gds.edu.cn/fpjgContent.aspx?BillGuid=x', 'gdegp'),
                     # ⚠️ 同一台服务器的 :8093 是**另一套数据**（小零易购的商品目录与
                     # 结果公示），不是默认端口那套电子反拍公告。只按主机名认站会把
                     # 两套数据混成一个，所以识别按 (host, port)。
                     ('https://gdegp.gds.edu.cn:8093/Upgrade/Publicity/ResultPublicity/'
                      'OrderResultPublicityView.aspx?BillGuid=x', 'xlyg'),
                     ('https://gdegp.gds.edu.cn:8093/x', 'xlyg')]:
        assert platform_for(url).key == key
    for url in ['https://gdegp.gds.edu.cn.evil.org/x',
                'https://example.edu.cn/fpjgContent.aspx',
                'https://notgdegp.gds.edu.cn/x',
                # 端口不对就不是注册平台：:8443 没有注册过。
                'https://gdegp.gds.edu.cn:8443/x']:
        assert platform_for(url) is None


def test_the_registry_holds_only_the_kept_platforms():
    """注册表就是这几个数据集，没有多余的。

    2026-09-25 摘除教采云是**合规决定**（用户按竞采星同一标准拍板），不是技术决定；
    2026-10-05 摘除在线 ``xlyg``（8093）是**效果决定**：51 轮 209 次调用、成交价 0 条
    （见 ``PLATFORMS`` 上方注释）。两次都写成显式一行的加减，所以这里把名单本身钉住：
    加站或减站都是改这一行的显式动作，不会"悄悄回来一个"。
    """
    assert [platform.key for platform in eps.PLATFORMS] == ['gdegp']
    assert sorted(eps._BY_KEY) == ['gdegp', 'xlyg'], \
        'xlyg 只是不在**在线检索**名单里，识别/脱敏仍要认得它'


def test_a_new_site_costs_only_a_config_object(replay):
    """骨架只读 ``platforms`` 参数，站点差异全来自配置对象。"""
    class Fake(eps.EducationPlatform):
        key, label = 'fake', '假平台'
        list_url = 'https://fake.example/v2/list'
        detail_url = 'https://fake.example/detail/{id}.html'

        def list_items(self, payload):
            return [{'id': '7', 'title': '某成像系统',
                     'url': self.detail_url.format(id=7), 'date': ''}]

    calls = replay(['{}'])
    found = discover({'category': '成像系统'}, platforms=(Fake(),), on_request=lambda: None)
    assert calls.count == 1
    # 详情地址由配置模板拼出、来源身份由配置的 key/label 带出——骨架一行都没为此改动。
    assert calls.urls[0] == 'https://fake.example/v2/list'
    assert found == [{'id': '7', 'title': '某成像系统',
                      'url': 'https://fake.example/detail/7.html', 'date': '',
                      'platform': 'fake', 'platform_label': '假平台'}]


# ---------------------------------------------------------------------------
# 列表：两种分页
# ---------------------------------------------------------------------------

def test_government_platform_postback_pagination_replays_hidden_fields(replay):
    """ASP.NET 回发分页：页号只能通过隐藏字段送回，翻页必须原样带回。"""
    page1 = ('<html><body>'
             '<input type="hidden" name="__VIEWSTATE" value="VS1">'
             '<input type="hidden" name="__EVENTVALIDATION" value="EV1">'
             "ANP_checkInput(&#39;ctl00$ContentPlaceHolder1$UcPagination&#39;,3)"
             '<ul><li><a title=\'华南农业大学采购荧光及化学发光成像系统\' target="_blank" '
             'href="fpjgContent.aspx?BillGuid=G1"></a></li>'
             '<li><a title=\'某学院采购离心机\' target="_blank" '
             'href="fpjgContent.aspx?BillGuid=G2"></a></li></ul>'
             '</body></html>')
    page2 = ('<html><body><ul><li><a title=\'某学院采购酶标仪\' target="_blank" '
             'href="fpjgContent.aspx?BillGuid=G3"></a></li></ul></body></html>')
    calls = replay([page1, page2])
    found = discover({'model': 'OI600', 'category': '成像系统'},
                     platforms=(eps.GuangdongEduGovernmentPlatform(),), on_request=lambda: None)
    assert [item['id'] for item in found] == ['G1', 'G2', 'G3']
    assert calls[0] == ('GET', eps.GuangdongEduGovernmentPlatform.list_url, None)
    method, url, data = calls[1]
    assert method == 'POST' and url == eps.GuangdongEduGovernmentPlatform.list_url
    assert data['__EVENTTARGET'] == 'ctl00$ContentPlaceHolder1$UcPagination'
    assert data['__EVENTARGUMENT'] == '2'
    # 隐藏字段原样带回，否则站点不认这一页。
    assert data['__VIEWSTATE'] == 'VS1' and data['__EVENTVALIDATION'] == 'EV1'
    # 第 2 页没有分页控件，等于"没有下一页"，所以只发两次请求。
    assert calls.count == 2


def test_a_repeated_page_ends_the_platform(replay):
    """站点把第一页重复发回来（回发参数没生效）时，不该白翻到上限。"""
    class ThreePages(eps.GuangdongEduGovernmentPlatform):
        recent_pages = 3

    page = ('<html><body>'
            "ANP_checkInput(&#39;ctl00$ContentPlaceHolder1$UcPagination&#39;,9)"
            '<ul><li><a title=\'某学院采购离心机\' target="_blank" '
            'href="fpjgContent.aspx?BillGuid=G1"></a></li></ul></body></html>')
    calls = replay([page, page, page])
    found = discover({}, platforms=(ThreePages(),), on_request=lambda: None)
    assert calls.count == 2, '第二页没有新候选就该停'
    assert [item['id'] for item in found] == ['G1']


# ---------------------------------------------------------------------------
# 额度：用完就静静收手，且不从既有通道挪用
# ---------------------------------------------------------------------------

def test_quota_exhaustion_stops_silently_and_keeps_completed_platforms(replay):
    """额度用完安静收手：整轮研究不崩，已经读完的站照样交候选。

    ⚠️ 这条用例 **2026-09-25 之后改过一次**。原来它钉的是当时的坏行为：额度在某个站
    翻页途中耗尽时，这个站本页已经拿到的候选也一起丢掉（``reached_quota`` 那一支直接
    ``break``，走不到 ``found.extend(listed)``），与 docstring 写的"返回已经拿到的
    候选"不符。本单把这个 bug 修了，所以这里反过来钉修复后的行为：**额度耗尽也让
    已经解析出来的候选照常返回**。

    额度 1 的口径：默认端口那个站（``gdegp``，列表只有 1 页）把额度用完，于是 8093
    那个站（``recent_pages = 2``）连第 1 页的请求都发不出去——它一条候选都交不出来，
    也不该因此少掉 ``gdegp`` 那条已经拿到的候选。
    """
    remaining = {'n': 1}

    def on_request():
        if remaining['n'] <= 0:
            raise QuotaReached()
        remaining['n'] -= 1

    # 默认端口那个站的列表只有 1 页（没有分页控件）。8093 那页准备着但用不上。
    gov_page = ('<html><body><ul><li><a title=\'某学院采购离心机\' target="_blank" '
                'href="fpjgContent.aspx?BillGuid=G1"></a></li></ul></body></html>')
    calls = replay([gov_page, XLYG_LIST])
    found = discover({}, limit=1, on_request=on_request)
    assert [item['platform'] for item in found] == ['gdegp']
    assert [item['id'] for item in found] == ['G1']
    # 额度是上网之前扣的：第二份准备好的响应没被消费，说明没有请求真的出门。
    assert len(calls.script) == 1, '额度用尽之后不该再有请求上网'
    assert remaining['n'] == 0


def test_no_form_capable_reader_means_the_channel_sends_nothing(monkeypatch):
    """产品没有可发 POST 的读取器时，这条通道一个请求都不发。

    这里钉的是一个真出过的问题：这几张列表只吃 POST，于是通道曾经自己拿
    ``urllib`` 发请求——那等于在 ``search.providers`` 这道边界**之外**又开了一条
    网络出口。测试注入的离线读取器拦不住它，8 个走完整研究流程的用例因此真的去
    连 ``gdegp.gds.edu.cn``，撞上 fixture 的 10 秒超时（同一批用例在改动前是
    108 passed / 14s，之后全挂）。宁可这一轮不接这个来源，也不能绕开产品联网。
    """
    sent = []

    def explode(*args, **kwargs):
        sent.append(args)
        raise AssertionError('显式禁止时不该有任何请求')

    monkeypatch.setattr(eps, 'request', explode)
    assert discover({'category': '生物安全柜', 'model': 'BSC-1604IIA2'},
                    transport=None) == []
    assert sent == []


def test_product_transport_needs_a_reader_that_can_post():
    """只有能发表单的读取器才能当这条通道的出口；否则返回 ``None``。"""
    import types

    class Offline:
        name = 'offline'

        def search(self, query):
            return []

        def fetch(self, url, timeout):
            return ''

    class Reader(Offline):
        def fetch_form(self, url, **kwargs):
            return b'<html></html>', None

    holder = types.SimpleNamespace(search=types.SimpleNamespace(providers=[Offline()]))
    assert pr.ProcurementResearch.education_platform_transport(holder) is None

    seen = {}

    class Recording(Reader):
        def fetch_form(self, url, **kwargs):
            seen.update({'url': url, **kwargs})
            return b'payload', None

    holder.search = types.SimpleNamespace(providers=[Offline(), Recording()])
    transport = pr.ProcurementResearch.education_platform_transport(holder)
    assert callable(transport)
    assert transport('https://gdegp.gds.edu.cn:8093/x', method='POST',
                     data={'a': 'b'}, headers={'Referer': 'r'}, timeout=7) == b'payload'
    # 表单、方法、来源头与超时原样交给读取器：通道只描述请求，不自己发。
    assert seen == {'url': 'https://gdegp.gds.edu.cn:8093/x', 'fields': {'a': 'b'},
                    'method': 'POST', 'extra_headers': {'Referer': 'r'}, 'timeout': 7}


def test_an_injected_transport_is_what_reaches_the_wire(monkeypatch):
    """给了 transport 就用它，模块自带的 ``urllib`` 一次都不碰。"""
    monkeypatch.setattr(eps.urllib.request, 'urlopen',
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError('不该走 urllib')))
    calls = []

    def transport(url, *, method, data, headers, timeout):
        calls.append((url, method, data))
        return XLYG_LIST

    discover({'category': '生物安全柜'},
             platforms=(eps.GuangdongXiaolingyigouPlatform(),),
             transport=transport, on_request=lambda: None)
    assert calls and calls[0][1] == 'POST'


def test_a_platform_error_does_not_stop_the_next_platform(replay):
    """一个站坏掉只丢它自己的候选；后面的站照旧问。"""
    calls = replay([RuntimeError('站点 502'), XLYG_LIST, XLYG_LIST])
    # 显式传两个平台：在线注册表 2026-10-05 起只剩 gdegp，而这条测试测的是**骨架**
    # 的容错，不该随着名单变化而失效（名单本身由
    # ``test_the_registry_holds_only_the_kept_platforms`` 钉住）。
    found = discover({}, platforms=(eps.GuangdongEduGovernmentPlatform(),
                                    eps.GuangdongXiaolingyigouPlatform()),
                     on_request=lambda: None)
    assert [item['platform'] for item in found] == ['xlyg']
    assert calls.count == 3


def test_quota_is_independent_of_the_existing_channels():
    """这条通道是小额、低频的：额度独立，不从既有通道里挪。"""
    assert EDUCATION_PLATFORM_LIMIT == 24
    assert pr.EDUCATION_PLATFORM_LIMIT == EDUCATION_PLATFORM_LIMIT
    assert 'education_platform' not in pr.RESEARCH_LIMITS, '既有通道的额度不许被挪用'


# ---------------------------------------------------------------------------
# 采信边界：注册平台仍然走既有的 tier 判据，没有一条为它放宽
# ---------------------------------------------------------------------------

#: 8093 详情页导航区里那一行 ``[购物车]``（实测，2026-09-25）：它命中
#: ``SELLER_SELF_DESCRIPTION`` 的 ``购物车``——注册平台自己的页面确实带这种商城字样。
XLYG_CART_LINK = '[购物车] [https://gdegp.gds.edu.cn:8093/Program/Main/Cart.aspx]'

#: 一页足以成为正式成交公告的内容：成交决定 + ≥2 个本地权威字段。
XLYG_DECISION_BODY = ('竞价结果公告 采购单位：华南农业大学 成交供应商：广州康龙生物科技有限公司 '
                      '成交金额：43400.00元 项目编号：DD20260408000007 ' + XLYG_CART_LINK)


def test_the_kept_registry_hosts_all_ride_the_local_authority_branch():
    """剩下的注册主机全在 ``.edu.cn`` 下：它们的 tier 不问"卖家自述"那条判据。

    这是 2026-09-25 摘掉"注册平台不被商城模板页脚一票否决"那次放宽的依据：
    ``publication_tier`` 对 ``.gov.cn`` / ``.edu.cn`` 在**本地权威分支就返回了**，
    ``_unusable_non_local``（``SELLER_SELF_DESCRIPTION`` 在那里）根本不会被走到。
    放宽的唯一受益者是当时那个非 ``.edu.cn`` 的商城模板站，它已整站摘除。
    """
    assert registered_hosts() == {'gdegp.gds.edu.cn'}
    assert all(is_local_authority_host(host) for host in registered_hosts())


def test_a_shopping_cart_link_does_not_void_a_platform_page():
    """8093 页面里真有 ``购物车`` 字样，它照样是本地权威来源——不是靠"注册名单"，是靠 TLD。

    实测（2026-09-25）：8093 的议价订单详情页导航区写着 ``[购物车]``，``购物车``
    正是 ``SELLER_SELF_DESCRIPTION`` 的词。判据不受影响，因为 ``.edu.cn`` 走的是
    本地权威分支（``_LOCAL_FIELDS`` 命中 ≥2 个即成立）。
    """
    assert publication_tier('gdegp.gds.edu.cn', XLYG_DECISION_BODY) == 'local_authority'


def test_a_shop_footer_still_voids_a_non_local_page():
    """非本地域名照旧被"卖家自述"一票否决：域名本身不给任何页面背书。"""
    assert publication_tier('shop.example.com', XLYG_DECISION_BODY) is None


def test_a_tender_without_a_decision_gets_no_tier():
    """有结构字段但没有成交决定（询价公告不是成交公告）同样不给。"""
    tender = ('询价公告 采购单位：某大学 项目编号：X1 公告日期：2024-01-01 '
              + XLYG_CART_LINK)
    assert publication_tier('gdegp.gds.edu.cn', tender) is None


# ---------------------------------------------------------------------------
# 详情页整形：只恢复页面自己印着的列关系，不改一个数
# ---------------------------------------------------------------------------

def test_shaping_a_page_without_a_known_table_is_a_no_op():
    body = '这一页没有可识别的商品表。' * 10
    assert shape_detail('xlyg', body, '<table><tr><td>a</td><td>b</td></tr></table>') == body
    assert shape_detail('gdegp', body, None) == body
    # 未注册的站点不整形。
    assert shape_detail('unknown', body, '<table><tr><td>a</td><td>b</td></tr></table>') == body


def test_government_detail_page_needs_no_shaping():
    """政府平台详情页是 ``字段: 值`` 成对行，现成清洗链就能读。"""
    body = ('采购项目编号: SQ26072200001 商品名称: 荧光及化学发光成像系统 '
            '品牌: 某某 型号规格: OI600 数量: 1 中标价: 95000 元')
    assert shape_detail('gdegp', body, None) == body
    assert price_table_rows(body, 'OI600') == []


def test_hidden_fields_reads_aspnet_state():
    html = ('<input type="hidden" name="__VIEWSTATE" value="a&amp;b">'
            '<input type="hidden" name="__EVENTVALIDATION" value="c">'
            '<input type="text" name="q" value="x">')
    assert hidden_fields(html) == {'__VIEWSTATE': 'a&b', '__EVENTVALIDATION': 'c'}


# ---------------------------------------------------------------------------
# 政府平台（gdegp，默认端口）的脱敏：人名／手机／地址／税号在落盘前消失
#
# 下面的样本全是**合成的**：人名、号码、地址、税号都是编的。真实的联系人信息
# 一个字节都不进这个文件。
#
# 为什么要盯这一站：``设计规范``（待归档那份「8093 修后重测」）§8 实测发现
# ``gdegp.gds.edu.cn/fpjgContent.aspx`` 的正文与快照里留着真手机号（10 个单、
# 24 处），原因是 ``GuangdongEduGovernmentPlatform`` 当时没有实现脱敏钩子，
# 基类默认原样返回。这里把修完之后的边界钉住。
# ---------------------------------------------------------------------------

#: 详情页的**合成**样本。真实页面的 HTML 结构本地没有（离线环境不再为脱敏打站点），
#: 所以几种可能的形状都放进去：整表格行、字段名与值分两格、值同格、div 里的文本行。
GDEGP_DETAIL = (
    '<html><body>'
    '<div class="title">广东省教育部门政府采购管理平台 结果公告</div>'
    '<table>'
    '<tr><td>采购项目编号：</td><td>FP20260101000001</td></tr>'
    '<tr><td>商品名称：</td><td>生物安全柜</td></tr>'
    '<tr><td>品牌：</td><td>某某</td></tr>'
    '<tr><td>型号规格：</td><td>BSC-0000IIA2</td></tr>'
    '<tr><td>数量：</td><td>1</td></tr>'
    '<tr><td>最高限价：</td><td>12000.00</td></tr>'
    '<tr><td>中标价：</td><td>10000.00</td></tr>'
    '</table>'
    '<table>'
    '<tr><td>供应商联系人：</td><td>张三</td></tr>'
    '<tr><td>供应商联系电话：</td><td>13800000000</td></tr>'
    '<tr><td>供应商联系地址：</td><td>假地址一号楼二单元303</td></tr>'
    '<tr><td>采购人收货人：</td><td>李四</td></tr>'
    '<tr><td>收货人手机：</td><td>13900000000</td></tr>'
    '<tr><td>纳税人识别号：</td><td>123456789012345678</td></tr>'
    '</table>'
    '<div>供应商联系人： 张三</div>'
    '<div>联系电话： 13800000000</div>'
    '</body></html>')

GDEGP_GONE = ('张三', '李四', '13800000000', '13900000000',
              '假地址一号楼二单元303', '123456789012345678',
              '供应商联系人', '联系电话', '纳税人识别号')
GDEGP_KEPT = ('FP20260101000001', '生物安全柜', 'BSC-0000IIA2', '10000.00',
              '中标价', '采购项目编号', '广东省教育部门政府采购管理平台')


def test_government_personal_contact_details_never_reach_the_body_or_snapshot():
    """硬边界：政府平台详情页公开的个人信息，落盘前必须消失。

    ``redact_detail`` 在 ``fetch_source`` 里的位置就是这条约束：它在存快照**之前**
    被调用（``procurement_research.py`` 里两处 ``snapshot_path`` 之前），因为文件一
    写下去就收不回来。这里同时钉住两端：个人信息与它的字段名不见了，而商品与价格
    字段、以及权威性判据要读的平台名一个不少。
    """
    from backend.infrastructure.search_providers import PublicWebSearchProvider

    clean = redact_detail('gdegp', GDEGP_DETAIL)
    assert clean != GDEGP_DETAIL
    for gone in GDEGP_GONE:
        assert gone not in clean, gone
    for kept in GDEGP_KEPT:
        assert kept in clean, kept
    body = PublicWebSearchProvider()._clean_html(clean, 'https://gdegp.gds.edu.cn/x')
    assert '13800000000' not in body and '张三' not in body
    assert 'BSC-0000IIA2' in body and '10000.00' in body
    assert publication_tier('gdegp.gds.edu.cn', body) is not None


def test_government_redaction_masks_numbers_when_the_shape_is_not_recognised():
    """形状全认不出来（没有表格、没有字段名）时，号码也必须掩码。

    这一道是兜底：真号一旦落盘就收不回来，所以"结构没读懂"不能等于"放它过去"。
    """
    payload = '本页没有个人字段标签，正文里有一串 13800000000 与 123456789012345678。'
    clean = redact_detail('gdegp', payload)
    assert '13800000000' not in clean and '138****0000' in clean
    assert '123456789012345678' not in clean


def test_government_redaction_blanks_label_cells_outside_a_whole_table_row():
    """``<tr>`` 形状没认出来时，"字段名格 + 值格"里的值也要清掉。

    政府平台详情页的 HTML 样本本地没有，所以脱敏不能只靠"字段一定在整 ``<tr>`` 里"
    这一条假设：这里给的是没有 ``</tr>`` 的残缺表格，整行那条正则不命中，值仍必须消失。
    """
    payload = ('<div><td>供应商联系人：</td><td>张三</td>'
               '<td>供应商联系电话：13800000000</td></div>')
    clean = redact_detail('gdegp', payload)
    assert '张三' not in clean and '13800000000' not in clean


def test_redaction_leaves_binary_attachments_alone():
    """二进制（PDF／Office）原样返回：改坏文件并不等于删掉里面的数字。

    ``fetch_source`` 把详情页和附件的原始字节交给同一个 ``redact_detail``。钩子内部
    是 decode → 正则 → encode，这对 HTML 没问题，对二进制会把文件改坏（实测最小 PDF
    379 → 635 字节），而手机号是 ASCII、改坏之后照样读得出来。所以这一层挡在前面：
    二进制不动。**这不是"已经脱敏了"**——PDF 文本层里的个人信息是已知缺口。
    """
    pdf = b'%PDF-1.4\nstream\n' + bytes(range(256)) + b'\nendstream\n%%EOF'
    assert redact_detail('gdegp', pdf) == pdf
    assert redact_detail('xlyg', pdf) == pdf
    office = b'PK\x03\x04' + bytes(range(256)) + b'xl/sharedStrings.xml'
    assert redact_detail('gdegp', office) == office
    assert eps.is_binary_payload(pdf) and eps.is_binary_payload(office)
    # 文本页不能被这道守卫放过：同一形状的 HTML 里手机号照样消失。
    html = ('<td>供应商联系电话：</td><td>13800000000</td>').encode('utf-8')
    assert not eps.is_binary_payload(html)
    assert b'13800000000' not in redact_detail('gdegp', html)


def test_both_platforms_share_one_personal_field_baseline():
    """两个站的字段表只有一份底表：给 gdegp 加宽不许悄悄改动 8093 的口径。

    8093 的字段表就是下面这 7 项（它那套整行／整区块删除是按这 7 项写的）；
    gdegp 的表是在它之上加宽，不是替换。``_XLYG_PERSONAL_FIELDS`` 一旦被改动，
    8093 的脱敏输出会跟着变，而那是既有行为、不该被这一单带着改。
    """
    assert eps._XLYG_PERSONAL_FIELDS == ('联系人', '联系电话', '联系地址', '手机',
                                         '邮箱', '收货人', '开票')
    assert eps._XLYG_PERSONAL_FIELDS is eps._PERSONAL_FIELDS
    assert set(eps._XLYG_PERSONAL_FIELDS) < set(eps._GDEGP_PERSONAL_FIELDS)


# ---------------------------------------------------------------------------
# 小零易购（gdegp.gds.edu.cn:8093）：结果公示 + 商品价格目录
#
# 下面的 HTML 全是**合成的**：结构与真实页面一致，人名／电话／地址都是编的。
# 真实的联系人信息一个字节都不进这个文件。
# ---------------------------------------------------------------------------

XLYG_ORDER_GUID = '02ebbf39-9501-447f-b22f-9f3b5adcc9eb'

XLYG_LIST = (
    '<html><body><form>'
    '<input type="text" name="ctl00$ContentPlaceHolder1$orderName" value="生物安全柜" />'
    '<div class="list-title"><div style="float:left;width:18%;">订单编号</div></div>'
    "<div class=\"list-content\" onclick=\"DoView(this)\" code='" + XLYG_ORDER_GUID + "'"
    " codeType='0' code1='100' code2 ='0'>"
    '<div style="float:left;width:18%;"> DD20260408000007 </div>'
    '<div style="float:left;width:18%;" title=" 生物安全柜"> 生物安全柜 </div>'
    '<div style="float:left;width:19%;"> 华南农业大学 </div>'
    '<div style="float:left;width:19%;"> 广州康龙生物科技有限公司 </div>'
    '<div style="float:left;width:16%;"> 2026/4/8 16:21:59 </div>'
    '<div style="float:left;width:10%;"> 交易成功 </div>'
    '</div></div>'
    # code2='1' 是**服务类**单据（``DoView`` 里 isService 那一维），它的两套 FW* 模板
    # 没有验过形状：不猜模板、不发这一条请求，直接跳过（宁可少一条候选）。
    "<div class=\"list-content\" onclick=\"DoView(this)\" code='ffffffff-0000-0000-0000-000000000000'"
    " codeType='1' code1='100' code2 ='1'>"
    '<div style="float:left;width:18%;"> DD20260101000001 </div>'
    '<div style="float:left;width:18%;" title=" 别的单据"> 别的单据 </div>'
    '</div></div>'
    '</form></body></html>')


def _xlyg_page(*rows):
    """合成一页 8093 结果公示：``rows`` 是 ``(订单编号, 项目名称)``。

    GUID 由订单编号末两位拼出来，保证同一页内不重复、不同页之间也不重复
    （``discover`` 按 URL 去重，重了会走"这一页没有新候选"那一支，就不是在测翻页了）。
    属性顺序照 ``_XLYG_ROW``：``code`` / ``codeType`` / ``code1`` / ``code2``。
    """
    html = ['<html><body><form>',
            '<input type="text" name="ctl00$ContentPlaceHolder1$orderName" value="生物安全柜" />']
    for index, (order_no, title) in enumerate(rows, start=1):
        suffix = order_no[-2:]
        html.append(f"<div class=\"list-content\" onclick=\"DoView(this)\" "
                    f"code='ffffffff-0000-0000-0000-00000000{suffix}'"
                    f" codeType='0' code1='100' code2 ='0'>"
                    f'<div style="float:left;width:18%;"> {order_no} </div>'
                    f'<div style="float:left;width:18%;" title=" {title}"> {title} </div>'
                    '<div style="float:left;width:19%;"> 华南农业大学 </div>'
                    '<div style="float:left;width:19%;"> 广州康龙生物科技有限公司 </div>'
                    f'<div style="float:left;width:16%;"> 2026/4/8 16:2{index}:59 </div>'
                    '<div style="float:left;width:10%;"> 交易成功 </div>'
                    '</div></div>')
    html.append('</form></body></html>')
    return ''.join(html)


def test_a_quota_stop_on_page_two_still_returns_what_page_one_found(replay):
    """额度在第 2 页耗尽时，第 1 页那 2 条候选必须照旧返回。

    这是 8093 会真的碰上的路径（``GuangdongXiaolingyigouPlatform.recent_pages = 2``）：
    额度是在上网之前扣的（``request`` 先调 ``on_request``），所以第 2 页那次请求根本
    没出门、也没有内容，但第 1 页的候选是**已经解析好的**。修之前 ``reached_quota``
    那一支直接 break，走不到 ``found.extend(listed)``，这 2 条被整批丢掉。
    """
    platform = eps.GuangdongXiaolingyigouPlatform()
    page1 = _xlyg_page(('DD20260408000001', '生物安全柜甲'),
                       ('DD20260408000002', '生物安全柜乙'))
    assert len(platform.list_items(page1)) == 2, '第 1 页得真解析出 2 条，否则测不到点子上'
    calls = replay([page1])
    remaining = {'n': 1}  # 只够第 1 页那一次；第 2 页的请求在 on_request 处被拦下

    def on_request():
        if remaining['n'] <= 0:
            raise QuotaReached()
        remaining['n'] -= 1

    found = discover({'category': '生物安全柜'}, platforms=(platform,),
                     on_request=on_request)
    assert [item['title'] for item in found] == ['生物安全柜甲', '生物安全柜乙']
    # ⚠️ 不能用 ``calls.count``：``_Recorder`` 是**先记下这次调用再调 on_request**，
    # 所以"额度拦下的那一次"也会被记进去（count == 2）。真正出门的只有第 1 页，用
    # 准备好的响应剩下几份来判：只消费了 1 份。
    assert len(calls.script) == 0, '只准备了第 1 页的响应，第 2 页只能被额度拦下'
    assert calls.count == 2 and calls.urls[0] == calls.urls[1], (
        '第 2 次调用是"已经到 request 了、被 on_request 拦回"，没真的上网')
    # 小零易购按型号直查商品目录那条路已撤（用户 2026-09-25 拍板），所以这个平台的
    # 候选只有结果公示详情这一种。
    assert found and all('ResultPublicityView.aspx' in item['url'] for item in found)


def test_a_quota_stop_on_page_one_returns_nothing_and_asks_nothing(replay):
    """第 1 页就被额度拦下时：返回空、零请求。

    额度耗尽就安静收手：这一轮不该再产出任何候选，也不该再为这条通道拼候选地址。
    """
    platform = eps.GuangdongXiaolingyigouPlatform()
    replay([])

    def on_request():
        raise QuotaReached()

    assert discover({'category': '生物安全柜'}, platforms=(platform,),
                    on_request=on_request) == []


def test_a_quota_free_two_page_walk_still_returns_every_page_in_order(replay):
    """正常的翻页路径不受这次改动影响：两页的候选按页序出现、各一次。"""
    platform = eps.GuangdongXiaolingyigouPlatform()
    page1 = _xlyg_page(('DD20260408000001', '生物安全柜甲'))
    page2 = _xlyg_page(('DD20260408000002', '生物安全柜乙'))
    calls = replay([page1, page2])
    found = discover({'category': '生物安全柜'}, platforms=(platform,),
                     on_request=lambda: None)
    assert [item['title'] for item in found] == ['生物安全柜甲', '生物安全柜乙']
    assert calls.count == 2
    assert calls[1][2]['__EVENTARGUMENT'] == '2', '第 2 页仍旧走分页回发'


XLYG_DETAIL = (
    '<html><body>'
    '<div class="title">议价 订单详情</div>'
    '订单名称： 生物安全柜'
    '订单编号： DD20260408000007'
    '成交总金额(元)： 43400.00'
    '<div class="title">一、商品信息</div>'
    '<table><tbody><tr>'
    '<th>商品名称</th><th>产品名称</th><th>品牌</th><th>型号规格</th>'
    '<th>金额（元）</th><th>协议价格（元）</th><th>数量</th><th>是否进口</th>'
    '<th>制造商</th><th>产地</th></tr><tr>'
    '<td>生物安全柜</td><td>其他教学用仪器</td><td>海尔</td><td>HR1780-IIA2（KY）</td>'
    '<td>44800.00</td><td>43400.00</td><td>1.00</td><td>否</td>'
    '<td>青岛海尔生物医疗科技有限公司</td><td>青岛</td>'
    '</tr></tbody></table>'
    '<div class="title">二、确定成交供应商</div>'
    '<table><tr><td>供应商：</td><td>广州康龙生物科技有限公司</td></tr>'
    '<tr><td>成交总金额（元）：</td><td>43400.00</td></tr>'
    '<tr><td>供应商联系人：</td><td>假联系人</td></tr>'
    '<tr><td>供应商联系电话：</td><td>17600000000</td></tr>'
    '<tr><td>供应商联系地址：</td><td>假地址一号楼二单元303</td></tr></table>'
    '<div id="ctl00_ContentPlaceHolder1_three" class="info_details">'
    '<div id="ctl00_ContentPlaceHolder1_shcoolTitle" class="title">三、采购人的收货人信息和开票信息</div>'
    '<table><tr><td>收货人：</td><td>假收货人</td></tr>'
    '<tr><td>联系电话：</td><td>13900000000</td></tr>'
    '<tr><td>收货地址：</td><td>假地址四号楼五单元606</td></tr>'
    '<tr><td>纳税人识别号：</td><td>123456789012345678</td></tr></table>'
    '</div>'
    '</body></html>')

#: 电子反拍／竞价的详情页（``ResultPublicityView.aspx``）。列名是「最高限价」「中标价」，
#: 整单金额印成 ``中标价：``。**「最高限价」不是成交价**：它是采购人自设的上限。
XLYG_BILL_DETAIL = (
    '<html><body>'
    '<div class="title">结果公示</div>'
    '订单名称： 天平离心机采购'
    '订单编号： CM20180508000133'
    '<div class="title">一、商品信息</div>'
    '<table><tbody><tr>'
    '<th>商品名称</th><th>产品名称</th><th>品牌</th><th>型号规格</th><th>最高限价</th>'
    '<th>数量</th><th>是否进口</th><th>中标价</th><th>制造商</th><th>产地</th></tr><tr>'
    '<td>台式高速冷冻离心机</td><td></td><td>湘仪</td><td>H1850</td><td>28656.00</td>'
    '<td>1.00</td><td>否</td><td>15000.00</td><td>湖南湘仪</td><td>中国</td></tr><tr>'
    '<td>精密天平</td><td></td><td>梅特勒-托利多</td><td>TLE104E</td><td>14060.00</td>'
    '<td>1.00</td><td>否</td><td>10000.00</td><td>梅特勒-托利多</td><td>中国</td></tr>'
    '</tbody></table>'
    '<div class="title">二、确定成交供应商</div>'
    '<table><tr><td>天平离心机采购(反拍) 电子竞价项目（编号：CM20180508000133）'
    '于 2018-05-11 15:00:09 确定成交供应商。</td></tr>'
    '<tr><td>中标供应商：</td><td>某某贸易有限公司</td></tr>'
    '<tr><td>中标价：</td><td>25000.00</td></tr></table>'
    '</body></html>')

XLYG_GOODS = (
    '<html><body>'
    '<div id="serachResult">商品筛选共： <i><span id="ctl00_ContentPlaceHolder1_lblSum">6</span></i>条</div>'
    '<div id="proShow" class="proLists"><ul>'
    "<li id='f6a95e51-be5f-4ce9-9324-4a199f469c6d'>"
    "<a onclick=\"OpenGoods('f6a95e51-be5f-4ce9-9324-4a199f469c6d','生物安全柜','0')\""
    " title='生物安全柜' code='f6a95e51-be5f-4ce9-9324-4a199f469c6d' href=\"javascript:void(0)\">"
    '<img src="../../Images/noimg.gif" alt="生物安全柜" /></a>'
    '<div class="inforBg"><span><a title=\'生物安全柜\' href="javascript:void(0)"> 生物安全柜<br />'
    '<em> BSC-1604IIA2</em></a></span>'
    '<p class="price"> <b>￥</b> 44800.00 </p>'
    '<p class="other"> <b>苏州安泰</b> </p>'
    '<p class="other"> <b> <a href="DeptGoods_List.aspx?DeptGuid=x"> 广州市深华生物技术有限公司 </b> </a> </p>'
    '</div></li>'
    '</ul></div></body></html>')


def test_xiaolingyigou_filter_is_a_bare_postback_and_lists_orders_only(replay):
    """列表吃表单字段、只产出结果公示详情这一类候选。

    商品价格目录（``Goods_List.aspx?searchStr=``，按型号直查挂牌价）那条**在线检索**
    已按用户 2026-09-25 的裁定撤掉，所以这里一个目录候选都不该再出现——这一条钉住
    "撤干净了"，防止哪天又被补回来。
    """
    calls = replay([XLYG_LIST])
    found = discover({'category': '生物安全柜', 'model': 'BSC-1604IIA2'},
                     platforms=(eps.GuangdongXiaolingyigouPlatform(),),
                     on_request=lambda: None)
    method, url, data = calls[0]
    assert method == 'POST'
    # 关键词是**表单字段**，不是查询串参数。
    assert data['ctl00$ContentPlaceHolder1$orderName'] == '生物安全柜'
    assert data['__EVENTTARGET'] == 'ctl00$ContentPlaceHolder1$btn_Find'
    assert [item['id'] for item in found] == [XLYG_ORDER_GUID]
    order = found[0]
    assert order['title'] == '生物安全柜'
    assert order['url'].endswith(f'BillGuid={XLYG_ORDER_GUID}&RequestCodeType=0&RequestGuid=100')
    assert not any('Goods_List.aspx' in item['url'] for item in found)
    # 目录检索那条路整个不存在了：类里没有这个方法，框架里也没有这个钩子。
    assert not hasattr(eps.GuangdongXiaolingyigouPlatform, 'extra_items')
    assert not hasattr(eps.EducationPlatform, 'extra_items')
    assert not hasattr(eps.EducationPlatform, '_catalogue_section')


def test_candidates_are_shared_round_robin_so_no_single_site_takes_every_slot(replay):
    """名额按平台轮转，不能被一个站独占。

    全局排序取前 N 的写法实测让排在后面的站一个名额都拿不到，于是"新接的站没有
    贡献"——那是名额分配造成的假象。这里 1 个名额也必须有后面那个站的一份。
    """
    class Loud(eps.EducationPlatform):
        key, label = 'loud', '占满名额的站'
        list_url = 'https://loud.example/list'

        def list_items(self, payload):
            return [{'id': str(n), 'title': '生物安全柜', 'url': f'https://loud.example/{n}',
                     'date': ''} for n in range(9)]

    class Quiet(eps.EducationPlatform):
        key, label = 'quiet', '排在后面的站'
        list_url = 'https://quiet.example/list'

        def list_items(self, payload):
            return [{'id': 'q', 'title': '生物安全柜', 'url': 'https://quiet.example/q',
                     'date': ''}]

    replay(['{}', '{}'])
    found = discover({'category': '生物安全柜'}, platforms=(Loud(), Quiet()), limit=4,
                     on_request=lambda: None)
    assert [item['platform'] for item in found] == ['loud', 'quiet', 'loud', 'loud']


def test_xiaolingyigou_paging_repeats_the_filter(replay):
    """翻页回发必须带上筛选词，否则第 2 页会变成全站最新订单。"""
    platform = eps.GuangdongXiaolingyigouPlatform()
    calls = replay([XLYG_LIST, XLYG_LIST])
    discover({'category': '生物安全柜'}, platforms=(platform,), on_request=lambda: None)
    assert calls.count == 2
    paged = calls[1]
    assert paged[0] == 'POST'
    assert paged[2]['__EVENTTARGET'] == 'ctl00$ContentPlaceHolder1$UcPagination'
    assert paged[2]['__EVENTARGUMENT'] == '2'
    assert paged[2]['ctl00$ContentPlaceHolder1$orderName'] == '生物安全柜'


def test_xiaolingyigou_personal_contact_details_never_reach_the_body_or_snapshot():
    """硬边界：详情页公开的联系人手机号与收货信息，落盘前就被删掉。

    个人信息是**不可逆**的：快照一旦写下去就收不回来，所以脱敏必须在存快照之前
    （``redact_detail`` 在 ``fetch_source`` 里的位置就是这条约束）。这里同时钉住
    两端：个人信息不见了，而商品与价格字段、以及权威性判据要用的词还在。
    """
    from backend.infrastructure.search_providers import PublicWebSearchProvider

    clean = redact_detail('xlyg', XLYG_DETAIL)
    assert '17600000000' not in clean and '13900000000' not in clean
    assert '假联系人' not in clean and '假收货人' not in clean
    assert '假地址一号楼二单元303' not in clean and '假地址四号楼五单元606' not in clean
    assert '纳税人识别号' not in clean
    # 商品与价格字段一个不少。
    for kept in ('HR1780-IIA2（KY）', '43400.00', '44800.00', '广州康龙生物科技有限公司',
                 '青岛海尔生物医疗科技有限公司', '成交总金额'):
        assert kept in clean, kept
    # 权威性判据读的两个词（标题，不是内容）留着——删标题会把这条来源一起删掉。
    assert '成交供应商' in clean and '采购人' in clean
    body = PublicWebSearchProvider()._clean_html(clean, 'https://gdegp.gds.edu.cn:8093/x')
    assert '17600000000' not in body and '假地址四号楼五单元606' not in body
    assert publication_tier('gdegp.gds.edu.cn', body) is not None


def test_xiaolingyigou_detail_shaping_reads_the_settled_unit_price():
    """``协议价格`` 是成交单价；整形后既有价格判据能读出它。"""
    from backend.infrastructure.search_providers import PublicWebSearchProvider

    clean = redact_detail('xlyg', XLYG_DETAIL)
    body = PublicWebSearchProvider()._clean_html(clean, 'https://gdegp.gds.edu.cn:8093/x')
    shaped = shape_detail('xlyg', body, clean)
    rows = price_table_rows(shaped, 'HR1780-IIA2（KY）')
    assert len(rows) == 1
    # 44800 是挂牌金额，43400 才是这一单的成交单价；取错列就是张冠李戴。
    assert rows[0]['amount'] == 43400.0
    assert rows[0]['quantity'] == 1.0
    assert rows[0]['brand'] == '海尔'
    assert '协议价格' not in shaped.split('型号规格 | 单价（元）')[1].split('\n')[0]


def test_xiaolingyigou_shaped_body_passes_the_amount_evidence_gate():
    """整形之后金额闸门能过——否则整条模型通道根本不会启动。"""
    from backend.infrastructure.search_providers import PublicWebSearchProvider

    clean = redact_detail('xlyg', XLYG_DETAIL)
    body = PublicWebSearchProvider()._clean_html(clean, 'https://gdegp.gds.edu.cn:8093/x')
    shaped = shape_detail('xlyg', body, clean)
    regions = price_regions(shaped, 'HR1780-IIA2（KY）')
    assert any(has_amount_evidence(region['text']) for region in regions)


def test_xiaolingyigou_shaping_refuses_a_row_whose_numbers_do_not_add_up():
    """单价 × 数量 ≠ 成交总金额时整行不要：宁可少采信，不许张冠李戴。"""
    platform = eps.GuangdongXiaolingyigouPlatform()
    body = ('订单编号： DD20260408000007\n'
            '成交总金额(元)： 999999.00\n'
            '商品名称 | 品牌 | 型号规格 | 金额（元） | 协议价格（元） | 数量\n'
            '生物安全柜 | 海尔 | HR1780-IIA2（KY） | 44800.00 | 43400.00 | 1.00')
    shaped = platform.shape_text(body, None)
    assert price_table_rows(shaped, 'HR1780-IIA2（KY）') == []
    # 数字对得上时才产出。
    body = body.replace('999999.00', '43400.00')
    shaped = platform.shape_text(body, None)
    assert len(price_table_rows(shaped, 'HR1780-IIA2（KY）')) == 1


def test_xiaolingyigou_zero_agreement_price_falls_back_to_the_amount_column():
    """协议价格为 0 时成交单价落在「金额（元）」列（实测 DYCZ-30C 那一单）。

    只有"金额 × 数量 = 成交总金额"成立时才回退，仍然每步自校验。
    """
    body = ('成交总金额(元)： 48000.00\n'
            '商品名称 | 品牌 | 型号规格 | 金额（元） | 协议价格（元） | 数量\n'
            '电泳仪 | 北京六一 | DYCZ-30C | 2400.00 | 0.00 | 20.00')
    shaped = eps.GuangdongXiaolingyigouPlatform().shape_text(body, None)
    rows = price_table_rows(shaped, 'DYCZ-30C')
    assert len(rows) == 1
    assert rows[0]['amount'] == 2400.0
    assert rows[0]['quantity'] == 20.0


def test_xiaolingyigou_catalogue_page_is_no_longer_shaped_or_reachable():
    """撤掉在线检索之后：商品目录页不再是任何候选，也不再被整形。

    ``XLYG_GOODS`` 是那份目录页的合成样本（结构与真实页面一致）。撤之前它会被
    ``_catalogue_section`` 整形成一张能被挂牌线索通道读到的表；撤之后这个整形分支
    不存在了，所以整形的结果就是"什么都没加"。
    """
    assert shape_detail('xlyg', '', XLYG_GOODS) == ''
    assert not hasattr(eps.GuangdongXiaolingyigouPlatform(), '_catalogue_section')


def test_price_leads_still_reads_other_sources_after_the_catalogue_is_withdrawn():
    """撤掉小零易购商品目录的在线检索，不许动到 ``price_leads`` 这条通用通道。

    它扫的是**所有来源**正文里的表格行，与小零易购没有依赖关系：这里给一个普通商城
    来源（既不是 8093、也不是 xlyg 整形的产物），挂牌线索照旧读得到，而且照旧只能
    是线索（``kind='listing'``、note 写明不能当成交价）。
    """
    from backend.services.price_leads import price_leads

    body = ('商品名称 | 型号 | 价格 | 品牌 | 供货商\n'
            '生物安全柜 | BSC-1604IIA2 | ￥44800.00 | 苏州安泰 | 广州市深华生物技术有限公司')
    source = {'url': 'https://shop.example/goods?q=BSC-1604IIA2', 'body_status': 'fetched',
              'body_text': body, 'source_type': 'unknown'}
    leads = price_leads([source], {'model': 'BSC-1604IIA2'})
    assert len(leads) == 1
    assert leads[0]['amount'] == 44800.0
    assert leads[0]['kind'] == 'listing'
    assert '不能作为成交价' in leads[0]['note']


# ---------------------------------------------------------------------------
# 8093 的详情页有四套模板：选错就取回空壳页（实测 97/265 单中招）
# ---------------------------------------------------------------------------

def test_xiaolingyigou_detail_template_follows_the_pages_own_doView_branches():
    """模板按 ``codeType`` / ``code2`` 选，分支照抄列表页自己的 ``DoView(this)``。

    以前这里写死 ``OrderResultPublicityView.aspx``，于是 ``codeType`` 3/4 的
    反拍／竞价单取回来是**表头在、数据空的空壳页**——下游看起来和"这单没有价格"
    一模一样，是最坏的一种错。四条分支与两种"认不出来"的情况都在这里钉住。
    """
    platform = eps.GuangdongXiaolingyigouPlatform()
    assert platform._detail_template('0', '0') == 'OrderResultPublicityView.aspx'
    assert platform._detail_template('1', '0') == 'OrderResultPublicityView.aspx'
    assert platform._detail_template('3', '0') == 'ResultPublicityView.aspx'
    assert platform._detail_template('4', '0') == 'ResultPublicityView.aspx'
    # 服务类（code2=1）的两套 FW* 模板**没有验证过**（265 条抽样里 code2 全是 0）：
    # 不猜模板，也不为它发请求。
    assert platform._detail_template('0', '1') is None
    assert platform._detail_template('3', '1') is None
    # 没见过的 codeType 同样不猜。
    assert platform._detail_template('9', '0') is None


def test_xiaolingyigou_bill_rows_become_bill_detail_candidates():
    """反拍／竞价行真的会拼出 ``ResultPublicityView`` 的地址（不是只在模板函数里对）。"""
    guid = 'aaaaaaaa-1111-2222-3333-444444444444'
    html = (
        '<html><body>'
        "<div class=\"list-content\" onclick=\"DoView(this)\" code='" + guid + "'"
        " codeType='4' code1='90' code2 ='0'>"
        '<div style="float:left;width:18%;"> CM20180528000206 </div>'
        '<div style="float:left;width:18%;" title=" 天平离心机采购"> 天平离心机采购 </div>'
        '</div></div></body></html>')
    items = eps.GuangdongXiaolingyigouPlatform().list_items(html)
    assert len(items) == 1
    assert items[0]['url'].endswith(
        f'ResultPublicityView.aspx?BillGuid={guid}&RequestCodeType=4&RequestGuid=90')


def test_xiaolingyigou_bill_detail_reads_the_award_price_not_the_ceiling():
    """反拍页的成交单价是「中标价」，「最高限价」不许当成交价。

    拿限价当成交价会系统性高估（实测 H1850：限价 28656、中标价 15000）。
    整单金额取"二、确定成交供应商"里的 ``中标价：``，而它同时是那张表的列名——
    两者同名，判据是"带冒号的那一处才是整单金额"。
    """
    from backend.infrastructure.search_providers import PublicWebSearchProvider

    clean = redact_detail('xlyg', XLYG_BILL_DETAIL)
    body = PublicWebSearchProvider()._clean_html(clean, 'https://gdegp.gds.edu.cn:8093/x')
    shaped = shape_detail('xlyg', body, clean)
    rows = {row['model']: row for row in price_table_rows(shaped, 'H1850')}
    assert rows, shaped
    assert rows['H1850']['amount'] == 15000.0, '读成 28656 就是把最高限价当了成交价'
    assert rows['H1850']['quantity'] == 1.0
    assert rows['H1850']['brand'] == '湘仪'
    # 整单两件加起来必须等于页面印的 25000，否则整单不要。
    assert {row['amount'] for row in price_table_rows(shaped, 'H1850')} == {15000.0}
    assert '最高限价' not in shaped.split('型号规格 | 单价（元）')[1].split('\n')[0]


def test_xiaolingyigou_bill_detail_without_an_award_total_yields_no_price():
    """没成交的反拍单（页面上没有 ``中标价：``）一行价格都不产出。

    实测取消竞价／重新竞价的那几单，中标价列是空的。这里宁可什么都不给，
    也不许拿最高限价顶上。
    """
    from backend.infrastructure.search_providers import PublicWebSearchProvider

    body = ('结果公示\n'
            '商品名称 | 产品名称 | 品牌 | 型号规格 | 最高限价 | 数量 | 是否进口 | 中标价 | 制造商\n'
            '离心机 | | eppendorf | 5804R | 88000.00 | 1.00 | 否')
    shaped = eps.GuangdongXiaolingyigouPlatform().shape_text(body, None)
    assert price_table_rows(shaped, '5804R') == []


def test_xiaolingyigou_bill_detail_refuses_a_table_that_does_not_add_up():
    """反拍页也走同一道订单级自校验：中标价 × 数量 ≠ 页面印的总额时整单不要。"""
    body = ('中标价： 99999.00\n'
            '商品名称 | 产品名称 | 品牌 | 型号规格 | 最高限价 | 数量 | 是否进口 | 中标价 | 制造商\n'
            '台式高速冷冻离心机 | | 湘仪 | H1850 | 28656.00 | 1.00 | 否 | 15000.00 | 湖南湘仪')
    shaped = eps.GuangdongXiaolingyigouPlatform().shape_text(body, None)
    assert price_table_rows(shaped, 'H1850') == []
    body = body.replace('99999.00', '15000.00')
    shaped = eps.GuangdongXiaolingyigouPlatform().shape_text(body, None)
    assert len(price_table_rows(shaped, 'H1850')) == 1


# ---------------------------------------------------------------------------
# 共享落盘层的号码掩码（``设计规范`` §4 第 3 行的共同根因）
#
# 用户 2026-09-25 拍板的口径：**只掩手机号／18 位号，不做"删个人字段行"**。
# 掩码只有一个实现（``eps.mask_landed_text``），下面这一组钉两件事：
#   1. 号码真的被掩掉（含分隔写法、全角写法）；
#   2. 价格／型号／品牌／日期／金额／长编号**一个都没被误伤**（反例）。
#
# 反例是这一单的硬要求：判据要用的数字被掩掉，比不掩更糟——那会让价格读不出来
# 还不报警。所以下面的反例与正例同等重要，改掩码规则必须两条一起看。
# ---------------------------------------------------------------------------

#: 判据要读的东西，掩码**不许碰**。每条都写了它为什么在这里。
_LANDED_MUST_KEEP = (
    '1450.00',            # 金额带小数：绝不能被当成号码
    '10000.00',           # 同上
    '74480.0',            # 同上（报价表里的单价）
    '1,450.00',           # 带千分位
    '440000',             # 数量／规格里的短数字
    '20260914',           # 8 位日期（紧凑写法）
    '2026-09-14',         # 破折号日期：分隔符形状与手机号分隔写法像，必须区分
    '20260408000007',     # 14 位订单号
    '2251451000001337350',  # 19 位项目编号（多一位，不能被子串规则咬掉）
    '62025111400482601',  # 17 位项目编号（少一位）
    'DHG-9240A',          # 型号
    'BSC-0000IIA2',       # 型号
    'HR1780-IIA2',        # 型号
    '上海一恒',            # 品牌
    '生物安全柜',          # 品名
    '0755-12345678',      # 座机号（明知不掩，见函数 docstring）
    '518000',             # 邮编
)


def test_landed_mask_masks_mobile_numbers_in_every_shape_we_have_seen():
    """手机号：连续、``-`` 分隔、空格分隔、全角数字——四种写法都必须变 ``***``。"""
    for spoken in ('13800000000', '138-0000-0000', '138 0000 0000',
                   '138　0000　0000', '１３８００００００００'):
        masked = eps.mask_landed_text(f'联系电话 {spoken} 结束')
        assert '***' in masked, spoken
        for digit in '13800000000':
            assert masked.count(digit) == 0, (spoken, masked)


def test_landed_mask_masks_an_eighteen_digit_number_and_leaves_other_lengths():
    """18 位号：正好 18 位才掩；17／19／20 位是项目编号或流水号，一位不动。

    这一条是"号码形状"与"长编号"的分界线。身份证与统一社会信用代码都是 18 位，
    而采购公告里的项目编号常见 17／19／20 位——按"至少若干位数字"去掩，就会把
    项目编号打成 ``***``，摘录与正文的对应关系也跟着错位。
    """
    masked = eps.mask_landed_text('身份证 110105194912310021 税号 91340100123456789X')
    assert '110105194912310021' not in masked and '***' in masked
    for keep in ('2251451000001337350', '62025111400482601', '12345678901234567890'):
        assert eps.mask_landed_text(f'编号 {keep}') == f'编号 {keep}', keep


def test_landed_mask_never_touches_the_numbers_the_price_gates_read():
    """反例（这一单的硬要求）：价格、金额、型号、品牌、日期、长编号一个不许动。

    ``validate_price`` 要求"摘录能在正文里逐字对上"。掩码万一咬到金额或型号，
    轻则价格读不出来，重则摘录与正文对不上而被判成伪造——比不掩更糟。
    """
    text = ('供应商：上海一恒科学仪器有限公司\n'
            '规格型号 DHG-9240A，数量 440000，单价 4660.0 元，成交金额 74480.0 元\n'
            '单价（元） 1,450.00 与 1450.00 与 10000.00\n'
            '成交日期 2026年9月14日 / 20260914 / 2026-09-14\n'
            '项目编号 2251451000001337350、62025111400482601，订单号 20260408000007\n'
            '型号 BSC-0000IIA2、HR1780-IIA2\n')
    assert eps.mask_landed_text(text) == text
    for kept in _LANDED_MUST_KEEP:
        assert eps.mask_landed_text(f'值 {kept} 值') == f'值 {kept} 值', kept


def test_landed_mask_keeps_a_wrong_length_number_untouched():
    """10／12 位数字不是手机号：不许按"像号码"就掩（订单号、流水号都在这个长度）。"""
    for keep in ('1380000000', '138000000000', '075512345678'):
        assert eps.mask_landed_text(f'编号 {keep}') == f'编号 {keep}', keep


def test_landed_mask_leaves_full_width_money_alone():
    """全角金额（``７４，８００``）一个字符都不许动。

    这条是**回归钉**：本单第一版为了掩全角号码，把整段正文的全角数字都折成了半角，
    于是"全角金额"这件既有行为被顺手改掉——`tests/test_fullwidth_money.py` 里两个
    用例在那一版全量上失败（网页原文被改写，摘录与快照的逐字对应也跟着变）。
    现在的做法是**先按全角形状认出号码那一小段、只折那一小段**，所以：
    全角号码照掩，全角金额照旧是原样。
    """
    for money in ('７４，８００', '１，４５０．００', '１２３４５６７８９'):
        assert eps.mask_landed_text(f'金额 {money} 元') == f'金额 {money} 元', money
    assert eps.mask_landed_text('电话 １３８００００００００ 金额 ７４，８００') == '电话 *** 金额 ７４，８００'


def test_landed_mask_is_idempotent_and_type_preserving():
    """幂等与类型：同一个落盘点会被走两次（补抓／缓存复用），掩两次结果必须一样。

    类型也要跟着入参走：调用方后面还要按字节判 PDF、按字节写快照。
    """
    once = eps.mask_landed_text('电话 13800000000，税号 110105194912310021，价 1450.00')
    assert once == eps.mask_landed_text(once)
    assert '***' in once and '1450.00' in once
    raw = '电话 138-0000-0000'.encode('utf-8')
    assert isinstance(eps.mask_landed_text(raw), bytes)
    assert b'***' in eps.mask_landed_text(raw)
    assert eps.mask_landed_text(None) is None
    assert eps.mask_landed_text('') == ''


def test_landed_payload_mask_skips_binary_and_masks_text():
    """载荷层的二进制守卫与平台钩子共用同一个判断。

    二进制不动（改坏文件不等于删掉数字），文本载荷一律掩；两道判断要是各写一份，
    就会出现"共享层改了二进制、站点层没改"这种一半一半的坏结果。
    """
    pdf = b'%PDF-1.4 13800000000'
    assert eps.mask_landed_payload(pdf) == pdf
    assert eps.is_text_payload(b'<p>13800000000</p>')
    assert not eps.is_text_payload(pdf)
    assert eps.mask_landed_payload(b'<p>13800000000</p>') == b'<p>***</p>'
    assert eps.is_text_payload('13800000000')


#: 非注册主机（第三方校／院网站）的公告页。它**不在**教育平台注册表里，正是
#: ``设计规范`` §4 第 3 行说的那个缺口：修前这种主机一个钩子都没有。
NON_REGISTRY_URL = 'https://lab-supplier.example.edu.cn/award/2026-0001.html'
NON_REGISTRY_PAYLOAD = (
    '<html><body>'
    '<p>项目名称：某学院实验室仪器采购项目成交公告</p>'
    '<table><tr><td>货物名称</td><td>规格型号</td><td>数量</td><td>单价（元）</td></tr>'
    '<tr><td>生物安全柜</td><td>甲厂Q10</td><td>1</td><td>74480.0</td></tr></table>'
    '<p>供应商联系人：张三，联系电话 13800000000</p>'
    '<p>供应商联系电话：138-0000-0000，备用 139 0000 0000</p>'
    '<p>统一社会信用代码 913401001234567890，单价 1450.00 元，中标价 44800.00 元</p>'
    '<p>项目编号 2251451000001337350，成交日期 2026年9月14日</p>'
    '</body></html>')


def _non_registry_body():
    """把上面那份 HTML 压成正文文本（走产品自己那条清洗链，不另写剥壳器）。"""
    from backend.infrastructure.search_providers import PublicWebSearchProvider
    return PublicWebSearchProvider._clean_html(NON_REGISTRY_PAYLOAD, NON_REGISTRY_URL)


def test_a_non_registry_host_lands_without_numbers_in_body_snapshot_or_database(tmp_path):
    """**根因的那一条**：非注册来源的正文／快照／库三处都不能留号码。

    修前的行为是"只认注册平台（按 host+port）"，所以这台主机一个钩子都没有；
    这里钉住修后：正文（`body_text`）、按内容寻址的快照 `.bin`、以及落库的 run
    记录三处都干净，而商品／价格／型号／日期一个不少。
    """
    from pathlib import Path
    from backend.infrastructure.page_reader import PageResult
    from backend.repositories.sqlite import SQLiteRepository
    from backend.services.official_search import OfficialSearchService

    assert eps.platform_for(NON_REGISTRY_URL) is None, '这台主机不在注册平台表里，才是要测的那条路'

    class Reader:
        def read(self, url, timeout=15, on_attempt=None, referer=None):
            if on_attempt:
                on_attempt()
            return PageResult(NON_REGISTRY_PAYLOAD.encode('utf-8'), 'text/html; charset=utf-8',
                              _non_registry_body(), {'reader': 'http', 'snapshot_kind': 'response_bytes'})

    repo = SQLiteRepository(tmp_path / 'app.sqlite3')
    service = pr.ProcurementResearch(repo, OfficialSearchService(providers=[], reader=Reader()), None)
    repo.put('tasks', 'task', {'id': 'task'})
    run = {'id': 'run', 'kind': 'research', 'task_id': 'task', 'status': 'running',
           'baseline': {'version': 1, 'model': 'Q10'}, 'usage': {'fetch': 0},
           'limits': {'fetch': 5}, 'sources': [], 'gaps': []}
    repo.put('procurement_records', 'run', run, task_id='task')
    try:
        source = service.fetch_source(run, {'url': NON_REGISTRY_URL, 'source_type': 'unknown',
                                            'purpose': 'price'})
        assert source['body_status'] == 'fetched'
        for gone in ('13800000000', '138-0000-0000', '139 0000 0000', '913401001234567890'):
            assert gone not in source['body_text'], (gone, source['body_text'])
        for kept in ('74480.0', '1450.00', '44800.00', '2251451000001337350', '生物安全柜'):
            assert kept in source['body_text'], kept
        snapshot = Path(source['snapshot']['path'])
        assert snapshot.is_file()
        raw = snapshot.read_bytes()
        assert b'13800000000' not in raw and b'913401001234567890' not in raw
        assert b'74480.0' in raw and b'1450.00' in raw, '快照里价格不许被动'
        run['sources'] = [source]
        service.persist(run)
        stored = service.get('task', 'run')['sources'][0]['body_text']
        assert '13800000000' not in stored and '74480.0' in stored
    finally:
        service.close()


#: 父页与附件。附件主机同样不在注册平台表里，附件记录的正文走
#: ``fetch_attachments`` → ``_fetch_source`` → ``fetch_source`` 这条链路
#: （``设计规范`` §4 第 4 行）。
ATTACHMENT_PARENT_URL = 'https://lab-supplier.example.edu.cn/notice/2026-0002.html'
ATTACHMENT_FILE_URL = 'https://download.lab-supplier.example.edu.cn/files/spec-2026.pdf'


def test_an_attachment_lands_without_numbers_through_the_attachment_chain(tmp_path):
    """附件链路也必须过掩码：它的正文与快照过去一个钩子都没有。

    ``fetch_attachments`` 里的附件 URL 一般不在注册平台表里，所以修前这条链路
    （父页 → 附件记录 → ``_fetch_source`` → ``fetch_source``）是全程不脱敏的。
    这里钉住附件自己的正文与快照：号码消失，而型号与价格照旧。
    """
    from pathlib import Path
    from backend.infrastructure.page_reader import PageResult
    from backend.repositories.sqlite import SQLiteRepository
    from backend.services.official_search import OfficialSearchService

    # 正文是从**载荷**（原始字节）解出来的，所以附件锚点必须写在载荷里；
    # ``page.text`` 只是读者层自己那份文本，正文并不取它。
    parent_body = ('<html><body>'
                   '<p>货物名称 | 规格型号 | 数量 | 单价（元）</p>'
                   '<p>生物安全柜 | 甲厂Q10 | 1 | 74480.0</p>'
                   '<p>供应商联系人：张三，联系电话 13800000000</p>'
                   f'<p>[采购文件]({ATTACHMENT_FILE_URL}) 甲厂Q10</p>'
                   + '<p>' + ('公告正文占位。' * 20) + '</p>'
                   '</body></html>')
    attachment_body = ('采购文件正文\n规格型号 甲厂Q10\n单价 74480.0 元\n'
                       '项目负责人电话 139-0000-0000\n税号 913401001234567890')

    class Reader:
        def read(self, url, timeout=15, on_attempt=None, referer=None):
            if on_attempt:
                on_attempt()
            if url == ATTACHMENT_FILE_URL:
                return PageResult(attachment_body.encode('utf-8'), 'text/plain; charset=utf-8',
                                  attachment_body, {'reader': 'http', 'snapshot_kind': 'response_bytes'})
            return PageResult(parent_body.encode('utf-8'), 'text/html; charset=utf-8',
                              parent_body, {'reader': 'http', 'snapshot_kind': 'response_bytes'})

    repo = SQLiteRepository(tmp_path / 'app.sqlite3')
    service = pr.ProcurementResearch(repo, OfficialSearchService(providers=[], reader=Reader()), None)
    repo.put('tasks', 'task', {'id': 'task'})
    run = {'id': 'run', 'kind': 'research', 'task_id': 'task', 'status': 'running',
           'baseline': {'version': 1, 'model': 'Q10'}, 'usage': {'fetch': 0},
           'limits': {'fetch': 8}, 'sources': [], 'gaps': [],
           'attachment_read_failures': {}}
    repo.put('procurement_records', 'run', run, task_id='task')
    try:
        parent = service.fetch_source(run, {'url': ATTACHMENT_PARENT_URL,
                                            'source_type': 'tender_document', 'purpose': 'price'})
        assert parent['body_status'] == 'fetched'
        run['sources'] = [parent]
        service.fetch_attachments(run, parent)
        attachment = next((s for s in run['sources'] if s['url'] == ATTACHMENT_FILE_URL), None)
        assert attachment is not None, [s['url'] for s in run['sources']]
        assert attachment['body_status'] == 'fetched'
        assert '139-0000-0000' not in attachment['body_text']
        assert '913401001234567890' not in attachment['body_text']
        for kept in ('74480.0', '甲厂Q10'):
            assert kept in attachment['body_text'], kept
        raw = Path(attachment['snapshot']['path']).read_bytes()
        assert b'139-0000-0000' not in raw and b'913401001234567890' not in raw
        assert b'74480.0' in raw
    finally:
        service.close()
