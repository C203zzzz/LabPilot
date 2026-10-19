"""第三方聚合站「发现渠道」适配器：只产出线索，永远不产出价格证据。

设计边界（都是硬约束，改动前先读 ``output/procurement-sourcing/probe_aggregators.txt``
里的实测记录）：

* 医价网（``www.120shop.cn``）、采招网（``bidcenter.com.cn``）与中国机电设备采购
  平台（``www.chinamae.com``）只用来**发现**：知道「某个型号在某地某项目里被某供应商
  中标了」这件事，然后拿项目编号或项目名称回 CCGP 这类官方源复核。官方复核不通过的
  线索一律丢弃或记缺口。
* 三个站的读取方式**不一样，这是实测事实而不是偏好**：医价网与采招网的列表由脚本
  渲染／服务端脱敏，只能靠浏览器渲染读 DOM；chinamae 的检索是
  ``POST https://www.chinamae.com/purchases-search/``（表单字段 ``scout``）直接返回
  **静态 HTML**，普通 HTTP 就能读，不需要渲染器。所以本模块有两种注入的读取能力：
  ``renderer``（前两个站用）与 ``fetcher``（chinamae 用）。只注入其中一种时，另一种
  站点降级成「空线索 + 一句人话原因」，不影响已经注入的那一种。
* chinamae **详情页有意不解析**：详情页有 7 个字段被服务端打成 ``******``
  （``docs/164`` §第 1 级、``docs/169`` ① 都实测过），列表页已经给足了本模块需要的
  四样东西——项目名（``title`` 属性）、地区、日期、详情 URL。不碰掩码字段是边界。
* 聚合站正文**永远不能**成为价格证据。本模块产出的每条记录都带
  ``evidence_grade='secondary'`` 与 ``source_kind='aggregator'``，
  ``price_evidence_from()`` 恒返回 ``None``，而且模块**不产生**任何能被塞进
  ``run['prices']`` 的字段（没有 ``price_amount`` / ``price_quote`` 之类的键）。
  ``amount`` 字段只是聚合站页面上印的展示文本，供人工判断哪条线索值得复核，
  不是价格证据，价格通道不得读它。
* 两个站点的响应/正文都由服务端生成：浏览器渲染只能解决「JS 渲染」，
  解决不了「服务端脱敏」。**不在 Python 里复刻任何解密逻辑**——渲染能力由调用方
  以 ``renderer(url) -> {'text': str, 'html': str} | None`` 注入。
* 默认关闭（``enabled=False``）。不读环境变量、不引入新的全局配置，调用方显式打开。
* 任何失败（没有渲染器／没有静态读取器、超时/报错、返回密文、站点结构改版、人机验证）
  一律降级成「空线索 + 一句人能读懂的原因」，**绝不抛异常**给调用方：
  主链必须在发现渠道整个挂掉的情况下照常工作。
* 官方复核与限流解耦：本模块**不**自己搜 CCGP、**不**管 5 分钟冷却。
  调用方注入 ``searcher(query)``，「5 分钟冷却」与「同一时刻只允许一个任务用搜索
  接口」由那一层统一管，本模块不越权。
* 官方复核**优先落到「中标/成交结果类」公告上**：复核线索时先挑结果类的，
  同一批官方结果里也先看结果类的（``docs/169`` ③ 实测：最终落到结果类公告的
  型号单价率是 17/20，落到招标/采购类的是 0/2——招标公告结构上就没有成交价）。
  这是**排序**，不是放宽判据：身份比对那几条判据一个字没动，排在前面的对不上
  就继续往后看，全对不上仍然是缺口。

刻意**不** import 主链类型（``backend.domain.*`` / ``source_authority``）：
主链正在并行改动，这里保持自足，接入时由整合方把
``evidence_grade`` / ``source_kind`` 映射到主链的 ``SourceType`` / ``PriceEvidenceLevel``。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from html import unescape as _unescape
from html.parser import HTMLParser
import re
from urllib.parse import quote, urlsplit

from backend.services.research_sources import parse_web_url

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

AGGREGATOR_120SHOP = '120shop'
AGGREGATOR_BIDCENTER = 'bidcenter'
#: 中国机电设备采购平台（机电设备采购网）。与上面两个不同：**不需要浏览器渲染**。
AGGREGATOR_CHINAMAE = 'chinamae'

#: 非一手标记。每条线索都必须带上，调用方据此拒绝把它当价格证据。
EVIDENCE_GRADE_SECONDARY = 'secondary'
SOURCE_KIND_AGGREGATOR = 'aggregator'

RECHECK_VERIFIED = 'verified'
RECHECK_NOT_FOUND = 'not_found'
RECHECK_ERROR = 'error'
RECHECK_NOT_ATTEMPTED = 'not_attempted'

#: 恒为 False：聚合站输出在任何情况下都不能进价格通道。
#: 这个常量存在的意义是让「只当发现渠道」成为可以被断言的写法，而不是口头约定。
PRICE_EVIDENCE_ELIGIBLE = False

#: 站点主机名后缀。刻意写窄：只认这三个域名。
_SITE_HOST_SUFFIXES = {
    AGGREGATOR_120SHOP: ('120shop.cn',),
    AGGREGATOR_BIDCENTER: ('bidcenter.com.cn',),
    AGGREGATOR_CHINAMAE: ('chinamae.com',),
}

#: 站点代号 → 人看得懂的名字（带域名，读者能自己打开核对）。
#:
#: 上面那三个常量是**内部代号**，它们原来被直接拼进缺口文字，于是报告里出现
#: 「120shop 遇到人机验证」「bidcenter 渲染器报错」这类句子——不懂这个系统的人
#: 读到只会疑惑"这是什么"。名字取自本模块开头登记的站点（医价网 www.120shop.cn、
#: 采招网 bidcenter.com.cn、中国机电设备采购平台 chinamae.com）。
SITE_LABELS = {
    AGGREGATOR_120SHOP: '医价网（120shop.cn）',
    AGGREGATOR_BIDCENTER: '采招网（bidcenter.com.cn）',
    AGGREGATOR_CHINAMAE: '中国机电设备采购平台（chinamae.com）',
}


def site_label(site) -> str:
    """面向用户的站点名；认不出的代号只说「该站点」，绝不把代号印出去。"""
    return SITE_LABELS.get(str(site or ''), '该站点')

#: 搜索入口 URL 形态，实测于 2026-xx（见 probe 记录）。
_SEARCH_URLS = {
    AGGREGATOR_120SHOP: 'https://www.120shop.cn/bid/index?searchContent={kw}&fromStart=fromStart',
    AGGREGATOR_BIDCENTER: 'https://search.bidcenter.com.cn/search?keywords={kw}&mod=0',
    # 这个 URL 不带查询串：chinamae 的检索是**表单 POST**（见 _STATIC_SEARCH_FORMS）。
    AGGREGATOR_CHINAMAE: 'https://www.chinamae.com/purchases-search/',
}

#: 需要浏览器渲染的站点：响应是脚本渲染的表格式结果或服务端密文。
_RENDERED_SITES = (AGGREGATOR_120SHOP, AGGREGATOR_BIDCENTER)

#: 普通 HTTP 就能读的站点：服务端直接回静态 HTML。
_STATIC_SITES = (AGGREGATOR_CHINAMAE,)

#: 静态站点的检索表单。chinamae 实测只需要一个字段 ``scout``（不需要 ``_token``），
#: 带上来路是因为浏览器提交这个表单时本来就会带。
_STATIC_SEARCH_FORMS = {
    AGGREGATOR_CHINAMAE: {'fields': ('scout',), 'referer': 'https://www.chinamae.com/'},
}

#: 详情页路径形态。站点一改版就匹配不上，于是退化成空——这正是想要的降级。
_120SHOP_DETAIL_PATH = re.compile(r'^/bid/goodsbank/goodsdetail$', re.I)
_BIDCENTER_DETAIL_PATH = re.compile(r'^/news-\d+-\d+\.html$', re.I)
#: chinamae 的结果列表页（POST 目标）与详情页。
_CHINAMAE_LIST_PATH = re.compile(r'^/purchases-search/?$', re.I)
_CHINAMAE_DETAIL_PATH = re.compile(r'^/purchases/[^/]+\.html$', re.I)

#: 医价网结果表的列名（按列名定位，不按列序：列表页与详情页同表不同序）。
_120SHOP_TITLE_COLUMNS = ('名称', '设备名称')
_120SHOP_AMOUNT_COLUMNS = ('成交价格', '中标价格')

#: 采招网详情页里可作线索字段的中文标签。
_BIDCENTER_LABELS = ('项目编号', '采购项目名称', '采购单位', '中标单位/候选人',
                     '中标金额', '总中标金额', '发布时间', '公告时间', '代理单位')

#: 官方正文里必须出现的采购语义标记，避免把无关页面当复核命中。
_OFFICIAL_MARKERS = ('项目编号', '中标', '成交', '采购', '公告', '招标')

#: 「中标/成交结果类」公告的措辞。**只用来排序**（先复核结果类的），不作为判据：
#: 排在前面的身份对不上就继续往后看，全对不上仍然是缺口。
_AWARD_NOTICE_MARKERS = ('中标', '成交', '结果公告', '结果公示', '中选', '比选结果',
                         '评标结果', '定标')

#: chinamae 列标题里「公告类型」后缀词。项目名 = 列表标题剥掉这些后缀
#: （口径照搬 ``docs/169`` 探针里那把已验证的尺子）。剥不干净不是错误：复核身份
#: 比对只是更难命中，并因此记成缺口——宁可不中，不放松判据。
_NOTICE_SUFFIX = re.compile(
    r'(?:[-－—–\s]*(?:中标|成交|采购|招标|竞争性|询价|公开|邀请|结果|评标|定标|'
    r'候选人|合同|更正|变更|终止|废标|流标|验收|公示|公告|竞价|比价|议价|比选|直接)'
    r'[^\u4e00-\u9fff]*)+$')
#: 代理机构前缀「<公司>关于」。``docs/169`` 实测必须剥：不剥的话同一家代理公司的
#: **另一个项目**会靠代理机构名本身凑够标题重合，被当成同一项目（idx=167 的错法）。
_AGENCY_PREFIX = re.compile(r'^.{2,40}?关于')

#: 服务端脱敏痕迹。带这些痕迹的值一律当空——拿它去复核只会制造假命中。
_MASKED_MARKERS = re.compile(r'#|[（(]\s*略\s*[）)]|点击查看|登录后查看|会员查看|\*{2,}')

#: 人机验证痕迹（实测医价网页面里挂着滑块验证组件）。
_CHALLENGE_MARKERS = ('请完成安全验证', '向右滑动完成验证', '滑动验证', '人机验证', 'captcha')

#: 密文形态：一长串 hex、没有汉字。医价网的 XHR 接口实测就是这个样子。
_HEX_ONLY = re.compile(r'\A[0-9a-fA-F]+\Z')
_WHITESPACE = re.compile(r'[\s\u00a0\u3000]+')
_CJK = re.compile(r'[\u4e00-\u9fff]')
#: 站点往 ``title`` 属性里塞的高亮标签（``<em style=…>TG16</em>``、
#: ``<font color=…>[新疆维吾尔自治区本级]</font>``）。172 行实测 4 行带这种东西，
#: 而且刚好落在型号上（``<em …>DHG-9070</em>A``），不剥掉的话项目名与查询词都会带着
#: 标签去官方站碰运气。
_TITLE_MARKUP = re.compile(r'<[^>]{0,200}>')


def unavailable_renderer(url):  # noqa: ARG001 - 签名必须与注入契约一致
    """生产环境真实渲染器的**占位**：本模块不内置浏览器。

    真实实现由调用方注入——例如接一个浏览器渲染服务、复用项目已有的浏览器通道，
    或者单独跑一个 Playwright 工作进程。此占位永远返回 ``None``，因此不做任何
    网络访问，也绝不在 Python 里复刻聚合站的解密逻辑。
    """
    return None


def unavailable_fetcher(url, fields=None, headers=None):  # noqa: ARG001 - 签名固定
    """静态读取器的**占位**：本模块不自己开连接。

    与 ``unavailable_renderer`` 并列的第二样注入能力，给那些**普通 HTTP 就能读**的
    站点用（今天只有 chinamae）。契约与渲染器同形：``fetcher(url, fields, headers)``
    返回页面载荷（``bytes``／``str``／``{'text':…, 'html':…}``）或 ``None``；
    ``fields`` 是表单 POST 的字段（GET 站点给 ``None``），``headers`` 是附加请求头。

    真实实现由调用方注入——产品里走的是既有公开网页读取通道
    （``PublicWebSearchProvider.fetch_form``，带公网地址校验、重定向逐跳校验与
    16MB 上限）。占位永远返回 ``None``，所以不做任何网络访问。
    """
    return None


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Lead:
    """一条聚合站线索。读不到的字段就是空串——不猜、不跨页面拼。"""

    title: str = ''
    url: str = ''
    project_id: str = ''
    #: 项目名。chinamae 的列表页没有单独的「项目名称」字段，这里是**列表标题剥掉
    #: 公告类型后缀与代理机构前缀后的推导值**（口径来自 ``docs/169``），用途只有
    #: 两个：当官方复核的查询词、当身份比对的字符串。医价网/采招网的列表页没有项目
    #: 名称时这里是空串——不把设备名硬塞进来。
    project_name: str = ''
    purchaser: str = ''
    supplier: str = ''
    amount: str = ''
    aggregator: str = ''
    notice_date: str = ''
    #: 聚合站自己标的地区标签。**只作展示与留痕**：``docs/164`` §四 实测有错标
    #: （标 ``[安徽]`` 的其实是新疆的项目），所以任何路由都不得读它。
    region: str = ''
    #: 聚合站自己标的公告类型（chinamae 的列表页有这一列，如「结果公示」）。
    #: 只用于挑复核顺序与展示，不参与任何价格判定。
    notice_type: str = ''
    #: 非一手标记，固定值；调用方据此拒绝把线索当价格证据。
    evidence_grade: str = EVIDENCE_GRADE_SECONDARY
    source_kind: str = SOURCE_KIND_AGGREGATOR
    #: 官方复核命中后填官方原文 URL；复核不通过则保持空串。
    official_url: str = ''
    recheck_status: str = RECHECK_NOT_ATTEMPTED

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DiscoveryResult:
    keyword: str
    leads: list = field(default_factory=list)
    partial: bool = False
    partial_reason: str = ''
    #: 每个站点这一轮的结果（ok / empty / renderer_error / fetcher_error /
    #: ciphertext / challenge），供主链打日志，不参与业务判断。
    channels: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            'keyword': self.keyword,
            'leads': [lead.to_dict() for lead in self.leads],
            'partial': self.partial,
            'partial_reason': self.partial_reason,
            'channels': dict(self.channels),
        }


@dataclass
class RecheckOutcome:
    """官方复核结果：``verified`` 是复核通过的线索，``gaps`` 是被丢弃的缺口。"""

    verified: list = field(default_factory=list)
    gaps: list = field(default_factory=list)
    searched: int = 0
    partial: bool = False
    partial_reason: str = ''

    def to_dict(self) -> dict:
        return {
            'verified': [lead.to_dict() for lead in self.verified],
            'gaps': list(self.gaps),
            'searched': self.searched,
            'partial': self.partial,
            'partial_reason': self.partial_reason,
        }


# ---------------------------------------------------------------------------
# 小组件
# ---------------------------------------------------------------------------


def _clean(value) -> str:
    if not isinstance(value, str):
        return ''
    return _WHITESPACE.sub(' ', value).strip()


def _norm(value) -> str:
    """归一化：去掉所有空白并 casefold，用于编号/名称的精确比较。"""
    if not isinstance(value, str):
        return ''
    return _WHITESPACE.sub('', value).casefold()


def _looks_masked(value) -> bool:
    return bool(_MASKED_MARKERS.search(value or ''))


def _usable(value) -> str:
    """脱敏过的值当空——它既不能当编号，也不能拿去复核。"""
    cleaned = _clean(value)
    if not cleaned or _looks_masked(cleaned):
        return ''
    return cleaned


def _drop_trailing_stat(value) -> str:
    """医价网把「中标次数」拼在同一格里（``公司名 7次``），去掉这个尾巴。"""
    return re.sub(r'\s*\d+\s*次\s*\Z', '', _clean(value)).strip()


def _amount_text(value) -> str:
    """保留金额的展示原文（如 ``1.07万``），但脱敏值、长文本一律丢弃。

    返回值只用于人工判断线索价值，**不是**价格证据。
    """
    cleaned = _clean(value)
    if not cleaned or len(cleaned) > 24 or _looks_masked(cleaned):
        return ''
    if not re.search(r'\d', cleaned):
        return ''
    return cleaned


def _looks_like_ciphertext(text) -> bool:
    """识别「只有一段密文、没有渲染后 DOM」的响应。"""
    if not isinstance(text, str):
        return False
    stripped = text.strip().strip('"').strip()
    if len(stripped) < 80:
        return False
    if _CJK.search(stripped):
        return False
    return bool(_HEX_ONLY.match(stripped))


def _looks_like_challenge(text) -> bool:
    low = (text or '').casefold()
    return any(marker.casefold() in low for marker in _CHALLENGE_MARKERS)


def _rendered_parts(rendered):
    """把注入的渲染结果拆成 ``(text, html)``；不可用时给一句人话原因。"""
    if rendered is None:
        return None, '渲染器没有返回页面内容'
    if isinstance(rendered, dict):
        text = rendered.get('text')
        html = rendered.get('html')
    else:
        text = getattr(rendered, 'text', None)
        html = getattr(rendered, 'html', None)
    text = text if isinstance(text, str) else ''
    html = html if isinstance(html, str) else ''
    if not text.strip() and not html.strip():
        return None, '渲染器返回了空页面'
    return (text, html), ''


def _render_failure_reason(exc: BaseException) -> str:
    """渲染失败的给人看的说法。

    原来把 ``type(exc).__name__`` 拼进句子（``渲染器报错（PageReadError）``），
    它随缺口印进报告——真站那一轮的报告里就是 1 处 ``PageReadError``。
    异常类名对采购人员没有信息量（同 ``procurement_research.note_failure`` /
    ``failure_kind_text`` 的取舍）：细节留在 ``run['failures']`` 里给排查用。
    """
    name = exc.__class__.__name__
    if 'Timeout' in name:
        return '渲染超时'
    return '渲染器报错'


def _static_parts(fetched):
    """把注入的静态读取结果拆成 ``(text, html)``；不可用时给一句人话原因。

    契约与 ``_rendered_parts`` 同形，只是多认 ``bytes``（普通 HTTP 读回来的是字节）：
    静态页面的 HTML 本身就是可读的，所以 text 与 html 都给同一份内容——本模块的解析
    只吃 HTML，把文本也给出去是为了让人机验证/密文那两个判据照旧生效。
    """
    if fetched is None:
        return None, '静态读取器没有返回页面内容'
    if isinstance(fetched, dict):
        text = fetched.get('text')
        html = fetched.get('html')
    elif isinstance(fetched, (bytes, bytearray)):
        html = bytes(fetched).decode('utf-8', 'replace')
        text = html
    elif isinstance(fetched, str):
        html = text = fetched
    else:
        text = getattr(fetched, 'text', None)
        html = getattr(fetched, 'html', text)
    text = text if isinstance(text, str) else ''
    html = html if isinstance(html, str) else ''
    if not text.strip() and not html.strip():
        return None, '静态读取器返回了空页面'
    return (text, html), ''


def _fetch_failure_reason(exc: BaseException) -> str:
    """静态读取失败的给人看的说法；异常类名不印给读者（同 ``_render_failure_reason``）。"""
    name = exc.__class__.__name__
    if 'Timeout' in name:
        return '静态读取超时'
    return '静态读取报错'


def _site_for_url(url: str) -> str:
    host = (urlsplit(url).hostname or '').lower()
    for site, suffixes in _SITE_HOST_SUFFIXES.items():
        if any(host == suffix or host.endswith('.' + suffix) for suffix in suffixes):
            return site
    return ''


class _HtmlScan(HTMLParser):
    """把一个 HTML 片段扫成「表格行」与「链接」两样结构化事实。

    只认结构、不认站点：具体到某个站的窄规则写在下面的解析函数里。
    """

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.tables = []   # list[list[list[str]]]：表格 -> 行 -> 单元格文本
        self.anchors = []  # list[(href, text, 祖先 class 串)]
        self._open_tables = []
        self._row = None
        self._cell = None
        self._stack = []
        self._href = None
        self._anchor_text = None
        self.feed(html or '')
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'table':
            self._open_tables.append([])
        elif tag == 'tr' and self._open_tables:
            self._row = []
        elif tag in ('td', 'th') and self._row is not None:
            self._cell = []
        elif tag == 'br' and self._cell is not None:
            self._cell.append(' ')
        elif tag == 'a':
            self._href = attrs.get('href') or ''
            self._anchor_text = []
        self._stack.append((tag, attrs.get('class') or ''))

    def handle_endtag(self, tag):
        if self._stack:
            self._stack.pop()
        if tag == 'table' and self._open_tables:
            self.tables.append(self._open_tables.pop())
        elif tag == 'tr' and self._row is not None:
            if self._open_tables:
                self._open_tables[-1].append(self._row)
            self._row = None
        elif tag in ('td', 'th') and self._cell is not None:
            if self._row is not None:
                self._row.append(_clean(''.join(self._cell)))
            self._cell = None
        elif tag == 'a' and self._href is not None:
            context = ' '.join(cls for _, cls in self._stack if cls)
            self.anchors.append((self._href, _clean(''.join(self._anchor_text or [])), context))
            self._href = None
            self._anchor_text = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)
        if self._anchor_text is not None:
            self._anchor_text.append(data)


def _all_rows(scan: _HtmlScan):
    for table in scan.tables:
        for row in table:
            yield row


def _find_120shop_columns(scan: _HtmlScan):
    """找到医价网结果表的表头，返回 ``列名 -> 索引``。

    Element UI 的表头和表体是两个 ``<table>``，所以先全局找表头，再扫所有行取数据。
    """
    for row in _all_rows(scan):
        cells = [_clean(cell) for cell in row]
        if '型号' not in cells:
            continue
        if not any(name in cells for name in _120SHOP_AMOUNT_COLUMNS):
            continue
        if not any(name in cells for name in _120SHOP_TITLE_COLUMNS):
            continue
        return {name: index for index, name in enumerate(cells) if name}
    return None


def _cell_at(cells, columns, *names) -> str:
    for name in names:
        index = columns.get(name)
        if index is None or index >= len(cells):
            continue
        value = cells[index]
        if value:
            return value
    return ''


def _parse_120shop(page_url: str, text: str, html: str, keyword: str):
    """解析医价网的结果表。解析不到就返回空 + 原因，绝不猜。"""
    scan = _HtmlScan(html)
    columns = _find_120shop_columns(scan)
    if columns is None:
        return [], '页面上没有出现医价网结果表格的表头（站点可能已改版）'

    want = _norm(keyword)
    leads = []
    seen = set()
    for row in _all_rows(scan):
        cells = [_clean(cell) for cell in row]
        model = _cell_at(cells, columns, '型号')
        # 窄规则：型号列必须命中关键词，否则这一行不是我们要的线索。
        if not model or not want or want not in _norm(model):
            continue
        title = _cell_at(cells, columns, *_120SHOP_TITLE_COLUMNS)
        if not title:
            continue
        lead = Lead(
            title=title,
            # 医价网结果行不提供详情页链接（「价格」是 JS 按钮，没有 href），保持空串。
            url='',
            project_id='',
            # 医价网只有设备名称，没有项目名称；不把设备名硬塞进项目名字段。
            project_name='',
            purchaser=_drop_trailing_stat(_cell_at(cells, columns, '采购医院')),
            supplier=_drop_trailing_stat(_cell_at(cells, columns, '成交单位')),
            amount=_amount_text(_cell_at(cells, columns, *_120SHOP_AMOUNT_COLUMNS)),
            aggregator=AGGREGATOR_120SHOP,
            notice_date=_cell_at(cells, columns, '成交时间', '中标时间'),
        )
        key = (_norm(lead.title), _norm(lead.supplier), _norm(lead.notice_date), _norm(lead.amount))
        if key in seen:
            continue
        seen.add(key)
        leads.append(lead)
    return leads, ''


def _parse_bidcenter_search(page_url: str, text: str, html: str, keyword: str):
    """解析采招网搜索结果列表，只取标题与详情页链接。"""
    scan = _HtmlScan(html)
    leads = []
    seen = set()
    for href, anchor_text, context in scan.anchors:
        if 'ssjg-list_bt' not in context:
            continue
        if not anchor_text:
            continue
        absolute = ('https:' + href) if href.startswith('//') else href
        # ``href`` 是聚合站页面上的任意字符串：畸形串只说明"这一条线索用不了"，
        # 不能让它把整页结果解析打断（见 ``research_sources.parse_web_url``）。
        absolute = parse_web_url(absolute, page_url)
        if absolute is None:
            continue
        if not _BIDCENTER_DETAIL_PATH.match(urlsplit(absolute).path):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        leads.append(Lead(title=anchor_text, url=absolute, aggregator=AGGREGATOR_BIDCENTER))
    if not leads:
        return [], '页面上没有出现采招网结果列表（站点可能已改版）'
    return leads, ''


#: chinamae 的结果列表行。实测结构（2026-xx，`docs/164` 那批原始页）：
#: ``<li><div class="info"><i></i><a href="/purchases/province/464" …class="region">[辽宁]</a>
#: <a href="/purchases/<32位>.html" class="title" title="…项目名…">…</a>…</div>
#: <div class="source"><a href="/purchases/type/2" …class="type">结果公示</a>…<span class="time">2025-11-07</span>``。
#: **两处与站点真实形态对齐的细节**：① 详情 URL 里**可能带掩码星号**
#: （实测 ``/purchases/c0119********d42362582fbb1a42b729.html``，172 行里 1 行），
#: 所以路径段不能限死成 hex；② 项目名取 ``title`` 属性而不是链接文字：链接文字里夹着
#: 站点自己加的高亮标签与「(…包含在正文中)」尾巴（``docs/164`` §第 1 级）。
_CHINAMAE_ROW = re.compile(
    r'<li>\s*<div class="info">.*?'
    r'<a href="/purchases/province/\d+"[^>]*class="region"[^>]*>\s*'
    r'\[(?P<region>[^\]]+)\]\s*</a>.*?'
    r'<a href="(?P<url>/purchases/[^"]+\.html)"[^>]*class="title"[^>]*'
    r'title="(?P<title>[^"]*)">.*?'
    r'<div class="source">\s*(?:<a href="/purchases/type/\d+"[^>]*class="type"[^>]*>\s*'
    r'(?P<type>[^<]*?)\s*</a>)?.*?'
    r'<span class="time">(?P<date>[\d-]+)</span>',
    re.S)


def _project_name_from_title(title: str) -> str:
    """chinamae 列表标题 → 项目名（剥公告类型后缀与代理机构前缀）。

    判据与实现照搬 ``docs/169`` 探针里那把已经在 26 条样本上用过的尺子，
    ``output/detail-rate/b-labels.json`` 就是它的判据来源。剥不出来时返回空串——
    宁可这一条复核不上，也不拿一个猜出来的名字去官方站碰运气。
    """
    base = _NOTICE_SUFFIX.sub('', _clean(title)).strip(' -－—')
    match = _AGENCY_PREFIX.match(base)
    if match and len(base) - match.end() >= 6:
        base = base[match.end():]
    return base.strip(' -－—')


def _parse_chinamae_list(page_url: str, text: str, html: str, keyword: str):
    """解析 chinamae 的检索结果列表：项目名 + 地区 + 日期 + 详情 URL。

    **刻意不按关键词过滤**（与医价网那条窄规则相反）：chinamae 的检索本身就是按型号
    做的，命中可以落在正文里，所以列表标题里**经常没有型号**——再按「标题必须含型号」
    过一道会把真实结果全丢掉。医价网那条规则的场景不同：它是在一张结果表里按型号列
    挑行，站点给的型号列是可判定的字段。

    解析不到就返回空 + 原因，绝不猜。返回的 ``project_name`` 是推导值（见
    ``_project_name_from_title``），``title`` 保持站点原文。
    """
    leads = []
    seen = set()
    for match in _CHINAMAE_ROW.finditer(html or ''):
        # ``title`` 属性是站点给的纯文本字段，但站内高亮会往里面塞标签（见
        # ``_TITLE_MARKUP``），先剥标签再归一化空白。
        title = _clean(_TITLE_MARKUP.sub('', _unescape(match.group('title'))))
        if not title:
            continue
        path = match.group('url')
        if not _CHINAMAE_DETAIL_PATH.match(path):
            continue
        url = 'https://www.chinamae.com' + path
        key = (_norm(title), url)
        if key in seen:
            continue
        seen.add(key)
        leads.append(Lead(
            title=title,
            url=url,
            project_id='',
            project_name=_project_name_from_title(title),
            aggregator=AGGREGATOR_CHINAMAE,
            notice_date=_clean(match.group('date')),
            region=_clean(match.group('region')),
            notice_type=_clean(match.group('type')),
        ))
    if not leads:
        return [], '页面上没有出现 chinamae 结果列表（站点可能已改版）'
    return leads, ''


def _page_title(html: str) -> str:
    match = re.search(r'<title[^>]*>(.*?)</title>', html or '', re.S | re.I)
    if not match:
        return ''
    title = _clean(match.group(1))
    for suffix in ('-采招网', '—采招网', '_采招网'):
        if title.endswith(suffix):
            title = title[: -len(suffix)].strip()
    return title


def _parse_bidcenter_detail(page_url: str, text: str, html: str, keyword: str):
    """解析采招网详情页。脱敏值一律当空，字段读不到就空。"""
    if not _BIDCENTER_DETAIL_PATH.match(urlsplit(page_url).path):
        return None
    scan = _HtmlScan(html)
    values = {}
    for row in _all_rows(scan):
        cells = [_clean(cell) for cell in row]
        for index, cell in enumerate(cells):
            if cell in _BIDCENTER_LABELS and cell not in values and index + 1 < len(cells):
                values[cell] = cells[index + 1]
    lead = Lead(
        title=_page_title(html),
        url=page_url,
        # 项目编号在未登录时显示成「点击查看」，_usable 会把它清成空串。
        project_id=_usable(values.get('项目编号')),
        project_name=_usable(values.get('采购项目名称')),
        purchaser=_usable(values.get('采购单位')),
        supplier=_usable(values.get('中标单位/候选人')),
        amount=_amount_text(_usable(values.get('中标金额'))),
        aggregator=AGGREGATOR_BIDCENTER,
        # 详情页字段区用「发布时间」，公告概要区用「公告时间」，两个都认。
        notice_date=_usable(values.get('发布时间') or values.get('公告时间')),
    )
    if not lead.title and not lead.project_id and not lead.project_name:
        return None
    return lead


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------


class AggregatorDiscovery:
    """只做发现的聚合站适配器。

    用法::

        discovery = AggregatorDiscovery(renderer=my_browser_renderer,
                                        fetcher=my_static_fetcher, enabled=True)
        result = discovery.discover('XKA-2200')
        outcome = discovery.official_recheck(result.leads, searcher=ccgp_searcher)
        # outcome.verified 里的线索仍然带 evidence_grade='secondary'：
        # 复核命中只说明「这条线索指向一份官方公告」，主链要自己去抓那份原文，
        # 抓到的原文才有资格进价格通道。

    ``renderer`` 给需要浏览器渲染的站点（医价网、采招网），``fetcher`` 给普通 HTTP
    就能读的站点（chinamae）。两者都是可选的：只注入其中一种时，另一种站点自己降级。
    """

    def __init__(self, renderer=None, fetcher=None, enabled: bool = False,
                 sites=(AGGREGATOR_120SHOP, AGGREGATOR_BIDCENTER, AGGREGATOR_CHINAMAE)):
        # 默认关闭：调用方显式打开。不读环境变量、不引入全局配置。
        self.enabled = bool(enabled)
        self._renderer = renderer
        self._fetcher = fetcher
        self._sites = tuple(sites)

    # -- 发现 ---------------------------------------------------------------

    def discover(self, keyword: str) -> DiscoveryResult:
        """在聚合站上搜一个型号/关键词，返回线索。任何失败都降级，不抛异常。"""
        keyword = _clean(keyword)
        if not self.enabled:
            return DiscoveryResult(
                keyword=keyword, leads=[], partial=True,
                partial_reason='第三方聚合站发现渠道未启用（默认关闭，需要调用方显式打开）')
        if not keyword:
            return DiscoveryResult(
                keyword=keyword, leads=[], partial=True,
                partial_reason='没有给出型号或关键词，无法在聚合站上搜索')

        leads = []
        channels = {}
        reasons = []
        for site in self._sites:
            site_leads, channel, reason = self._search_one(site, keyword)
            channels[site] = channel
            if reason:
                reasons.append(reason)
            leads.extend(site_leads)

        # 去重：同一台设备可能同时出现在列表页和详情页解析结果里。
        # 详情 URL 也在键里：chinamae 实测同一个项目会挂 2–6 条**不同 URL** 的列表行
        # （同一标题、同一日期），它们指向的公告条目不是同一条，合并掉会把能复核的
        # 行数从 172 掉到 157（本单离线核对实测）。复核那一步按查询词合并重复请求。
        deduped = []
        seen = set()
        for lead in leads:
            key = (_norm(lead.title), _norm(lead.url), _norm(lead.project_id),
                   _norm(lead.supplier), _norm(lead.notice_date), _norm(lead.amount),
                   lead.aggregator)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(lead)

        return DiscoveryResult(
            keyword=keyword,
            leads=deduped,
            partial=bool(reasons),
            partial_reason='；'.join(reasons),
            channels=channels,
        )

    def _search_one(self, site: str, keyword: str):
        if site in _STATIC_SITES:
            return self._search_static(site, keyword)
        return self._search_rendered(site, keyword)

    def _search_rendered(self, site: str, keyword: str):
        """需要浏览器渲染的站点：把检索 URL 交给注入的渲染器。"""
        if site not in _SEARCH_URLS:
            return [], 'empty', '未知的聚合站：%s' % site_label(site)
        if self._renderer is None:
            return [], 'empty', ('%s 没有注入渲染器：这个站的响应要浏览器渲染后才能读'
                                 '（本模块不内置浏览器，也不在 Python 里解密）' % site_label(site))
        url = _SEARCH_URLS[site].format(kw=quote(keyword))
        try:
            rendered = self._renderer(url)
        except Exception as exc:  # 渲染器超时/崩溃都走这里，绝不外抛
            return [], 'renderer_error', '%s %s' % (site_label(site), _render_failure_reason(exc))
        parts, reason = _rendered_parts(rendered)
        if parts is None:
            return [], 'empty', '%s %s' % (site_label(site), reason)
        text, html = parts
        if _looks_like_ciphertext(text) or _looks_like_ciphertext(html):
            return [], 'ciphertext', ('%s 返回的是密文，没有渲染后的 DOM 可读'
                                      '（需要浏览器渲染，不在 Python 里解密）' % site_label(site))
        return self._finish_search(site, url, text, html, keyword)

    def _search_static(self, site: str, keyword: str):
        """静态页面就能读的站点：把检索表单交给注入的普通 HTTP 读取器。"""
        if site not in _SEARCH_URLS:
            return [], 'empty', '未知的聚合站：%s' % site_label(site)
        if self._fetcher is None:
            return [], 'empty', ('%s 没有注入静态读取器：这个站的列表页要普通 HTTP 读'
                                 '（不需要浏览器渲染）' % site_label(site))
        url = _SEARCH_URLS[site]
        form = _STATIC_SEARCH_FORMS.get(site) or {}
        fields = {name: keyword for name in form.get('fields') or ()}
        headers = {'Referer': form['referer']} if form.get('referer') else None
        try:
            fetched = self._fetcher(url, fields=fields or None, headers=headers)
        except Exception as exc:  # 超时/网络错误都走这里，绝不外抛
            return [], 'fetcher_error', '%s %s' % (site_label(site), _fetch_failure_reason(exc))
        parts, reason = _static_parts(fetched)
        if parts is None:
            return [], 'empty', '%s %s' % (site_label(site), reason)
        text, html = parts
        if _looks_like_ciphertext(text) or _looks_like_ciphertext(html):
            return [], 'ciphertext', ('%s 返回的是密文，没有可读的页面内容'
                                      '（不在 Python 里解密）' % site_label(site))
        return self._finish_search(site, url, text, html, keyword)

    def _finish_search(self, site: str, url: str, text: str, html: str, keyword: str):
        leads, parse_reason = self._parse(site, url, text, html, keyword)
        if leads:
            return leads, 'ok', ''
        if _looks_like_challenge(text) or _looks_like_challenge(html):
            return [], 'challenge', '%s 遇到人机验证，本轮放弃该站点' % site_label(site)
        return [], 'empty', '%s %s' % (site_label(site), parse_reason or '没有解析出线索')

    def _parse(self, site: str, page_url: str, text: str, html: str, keyword: str):
        if site == AGGREGATOR_120SHOP:
            return _parse_120shop(page_url, text, html, keyword)
        if site == AGGREGATOR_BIDCENTER:
            return _parse_bidcenter_search(page_url, text, html, keyword)
        if site == AGGREGATOR_CHINAMAE:
            return _parse_chinamae_list(page_url, text, html, keyword)
        return [], '未知的聚合站：%s' % site_label(site)

    # -- 二次解析（主链自己渲染了详情页时用） --------------------------------

    def leads_from_page(self, page_url: str, rendered, keyword: str = ''):
        """解析一个**已经渲染好**的页面，返回线索列表。失败返回空列表，不抛异常。"""
        parts, _reason = _rendered_parts(rendered)
        if parts is None:
            return []
        text, html = parts
        if _looks_like_ciphertext(text) or _looks_like_ciphertext(html):
            return []
        site = _site_for_url(page_url)
        if site == AGGREGATOR_120SHOP:
            # 详情页与列表页同表不同序，按列名定位的解析器已经覆盖，直接复用。
            leads = _parse_120shop(page_url, text, html, keyword)[0]
            if _120SHOP_DETAIL_PATH.match(urlsplit(page_url).path):
                # 详情页解析出来的线索知道自己在哪一页：列表页的行没有链接，
                # 但详情页有，把 URL 带上对后续人工复核有用。
                return [replace(lead, url=page_url) for lead in leads]
            return leads
        if site == AGGREGATOR_BIDCENTER:
            if _BIDCENTER_DETAIL_PATH.match(urlsplit(page_url).path):
                lead = _parse_bidcenter_detail(page_url, text, html, keyword)
                return [replace(lead, url=page_url)] if lead else []
            return _parse_bidcenter_search(page_url, text, html, keyword)[0]
        if site == AGGREGATOR_CHINAMAE:
            if _CHINAMAE_LIST_PATH.match(urlsplit(page_url).path):
                return _parse_chinamae_list(page_url, text, html, keyword)[0]
            # 详情页**有意不解析**：站点在服务端把 7 个字段打成 ``******``
            # （``docs/164`` §第 1 级实测），本模块需要的东西列表页已经全给了。
            # 不在这里做"看到星号就跳过"一类的猜测——那是判断，不是解析。
            return []
        return []

    # -- 官方复核（限流由注入的 searcher 那一层管） --------------------------

    def official_recheck(self, leads, searcher=None, limit=None) -> RecheckOutcome:
        """用项目编号或项目名称把线索拿到官方侧复核。

        * 命中 -> 标 ``recheck_status='verified'`` 并记下官方原文 URL；
        * 没命中、没给 searcher、复核报错 -> 记成缺口，**不**作为可用结果返回。

        本模块不搜 CCGP、不管 5 分钟冷却：冷却与「同时只有一个任务用搜索接口」
        由 ``searcher`` 那一层统一管。复核报错时不再继续查剩余的线索，避免在
        限流窗口里雪上加霜。

        **优先复核「结果类」的线索**（见 ``_recheck_priority``），同一个查询词只打
        一次官方接口。``limit`` 是这一轮最多复核几条，``None`` = 不限（既有调用方的
        行为逐字不变）；被上限挡下的线索**记成缺口**——「没去查」和「查了没有」必须
        分得开，这个项目已经栽过两次。
        """
        leads = list(leads or [])
        if searcher is None:
            gaps = ['%s：没有注入官方复核用的 searcher，线索不采信' % _lead_label(lead)
                    for lead in leads]
            return RecheckOutcome(
                verified=[], gaps=gaps, searched=0, partial=bool(leads),
                partial_reason='没有注入 searcher，官方复核这一步没有执行；'
                               '聚合站线索一律不采信')
        if not callable(searcher):
            return RecheckOutcome(
                verified=[],
                gaps=['%s：searcher 不可调用，线索不采信' % _lead_label(lead) for lead in leads],
                searched=0, partial=bool(leads),
                partial_reason='searcher 不是可调用的复核入口，聚合站线索一律不采信')

        verified = []
        gaps = []
        searched = 0
        searched_queries = set()
        stop_reason = ''
        # 先复核「结果类」的线索：``docs/169`` ③ 实测最后落到成交公告上的比例远高于
        # 落到招标/采购公告上的（17/20 对 0/2）。排序用稳定排序，同一优先级内保持
        # 调用方给的顺序——只改先后，不改判据、不改去留。
        ordered = sorted(leads, key=_recheck_priority)
        if limit is not None and limit >= 0 and len(ordered) > limit:
            for lead in ordered[limit:]:
                gaps.append('%s：本轮复核条数已到上限（%d 条），这一条**没有**去官方查'
                            % (_lead_label(lead), limit))
            ordered = ordered[:limit]
        for lead in ordered:
            if stop_reason:
                gaps.append('%s：%s' % (_lead_label(lead), stop_reason))
                continue
            query = _recheck_query(lead)
            if not query:
                gaps.append('%s：线索里既没有项目编号也没有项目名称，无法到官方复核'
                            % _lead_label(lead))
                continue
            if _norm(query) in searched_queries:
                # 同一批里同一个项目会有多条列表行（chinamae 实测同一标题 2–6 行）。
                # 同一个查询词再打一次官方接口等于白花一次请求，**合并**成一次复核；
                # 后到的那条记成缺口而不是"没有明细"，人能看出它是被合并掉的。
                gaps.append('%s：与已复核的线索查询词相同（%s），本轮不重复请求官方接口'
                            % (_lead_label(lead), query))
                continue
            try:
                results = searcher(query)
                searched += 1
                searched_queries.add(_norm(query))
            except Exception:
                # 可能是 CCGP 限流/网络故障。记缺口，并且不再继续打接口。
                # 异常类名不进这一句（它随缺口印进报告，真站那一轮印了 4 处
                # ``TimeoutError``）：读者要知道的是"接口没答复、本轮不再试"。
                stop_reason = ('官方复核接口没有返回结果（读取失败），为避免在限流窗口里继续请求，'
                               '剩余线索不再查询')
                gaps.append('%s：%s' % (_lead_label(lead), stop_reason))
                continue
            official_url = _match_official(lead, results)
            if official_url:
                verified.append(replace(
                    lead,
                    official_url=official_url,
                    # 复核命中也不改变非一手标记：官方原文要主链自己去抓。
                    recheck_status=RECHECK_VERIFIED,
                ))
            else:
                gaps.append('%s：官方侧没有复核到对应的采购原文（查询词：%s）'
                            % (_lead_label(lead), query))

        return RecheckOutcome(
            verified=verified,
            gaps=gaps,
            searched=searched,
            partial=bool(gaps),
            partial_reason=('有 %d 条线索没有通过官方复核，已丢弃或记为缺口' % len(gaps))
                           if gaps else '',
        )


# ---------------------------------------------------------------------------
# 复核用的纯函数
# ---------------------------------------------------------------------------


def price_evidence_from(lead) -> None:  # noqa: ARG001 - 签名固定，故意忽略入参
    """恒为 ``None``：聚合站线索不能变成价格证据。

    这个函数存在的唯一意义，是把「只当发现渠道」变成可以被测试断言的事实：
    任何想从线索里造价格记录的调用方，都会在这里拿到 ``None``。
    """
    return None


def _lead_label(lead) -> str:
    if isinstance(lead, dict):
        label = lead.get('title') or lead.get('project_name') or lead.get('url')
    else:
        label = getattr(lead, 'title', '') or getattr(lead, 'project_name', '') \
            or getattr(lead, 'url', '')
    return _clean(label) or '(无标题线索)'


def _lead_value(lead, name: str) -> str:
    if isinstance(lead, dict):
        return _clean(lead.get(name) or '')
    return _clean(getattr(lead, name, '') or '')


def looks_like_award_notice(value) -> bool:
    """标题/公告类型里带不带"结果类"字样（中标、成交、结果公示…）。

    这个判据**只用来排序**（先看结果类），从来不用来筛除：``docs/169`` ③ 实测
    结果类 17/20、招标采购类 0/2，先看结果类是省额度，不是宣告后者没有价格。
    公开出来是因为复核那一路（``procurement_research.aggregator_ccgp_search``）
    要用同一把尺子决定先读哪几篇正文，两处各写一份迟早会走偏。
    """
    text = str(value or '')
    return any(marker in text for marker in _AWARD_NOTICE_MARKERS)


#: 「同一项目」的最长连续汉字重合门槛。沿用 ``docs/164`` / ``docs/169`` 两单用过的
#: 同一把尺子（≥10 字）：``docs/164`` 用它把 8 个"官方域名命中"砍到 4 个真命中，
#: 方向是收紧而不是放松。这里只当**最后一档兜底**——严格的"名字整体出现在正文里"
#: 已经先试过了，这一档是为推导出来的项目名（标题剥类型后缀、难免留个把残字）准备的。
SAME_PROJECT_OVERLAP = 10

#: 兜底那一档要求名字本身至少有这么长，避免短名字靠通用词凑够重合。
SAME_PROJECT_MIN_NAME = 12

#: 标题自带的装饰：开头的方括号/圆括号标签（``[政采云]``、``【初始合同】``）。
#: 上限 24 字是给 chinamae 自己那串地区标签留的余量：
#: ``[新疆维吾尔自治区·和田地区·民丰县]`` 实测 20 字，按 12 字卡会一个都剥不掉。
_TITLE_LEAD_TAG = re.compile(r'^\s*[\[【（(][^\]】）)]{1,24}[\]】）)]\s*')
#: 标题自带的装饰：结尾的括号注记（``（采购包1）``、``（二次）``）。**只用于生成
#: 身份比对的候选名**，不写回 ``Lead.project_name``，也不改查询词。
_TITLE_TAIL_NOTE = re.compile(r'\s*[（(][^（()）]{1,16}[)）]\s*$')
#: 残留的连接字（``…设备采购的成交公告`` 剥掉后缀后剩一个「的」）。
_TITLE_TAIL_PARTICLE = re.compile(r'[的\s]+$')


def _strip_title_decorations(title: str) -> str:
    """剥掉标题自己带的方括号标签与结尾括号注记，得到身份比对的**候选名**。

    比对的判据是"包含"（``name in body``），所以剥短只可能让本来对得上的对上；
    唯一的风险是剥到过短、拿通用词去凑，所以调用方还要求长度不低于
    :data:`SAME_PROJECT_MIN_NAME`，且这一档排在严格判据之后。
    """
    name = _clean(title)
    for _ in range(3):
        stripped = _TITLE_LEAD_TAG.sub('', name)
        stripped = _TITLE_TAIL_NOTE.sub('', stripped)
        if stripped == name:
            break
        name = stripped
    # 去掉括号注记之后可能又露出了公告类型后缀（``…采购及服务（2026电子卖场87）需求``），
    # 再剥一次；留下尾巴只会让"包含"判据失手，不会造成假命中。
    name = _NOTICE_SUFFIX.sub('', name)
    return _TITLE_TAIL_PARTICLE.sub('', name).strip(' -－—–、,，')


def _identity_names(project_name: str) -> list:
    """身份比对用的候选名：先原文，再剥掉装饰的候选名。

    剥装饰那个候选名**要够长才算**（:data:`SAME_PROJECT_MIN_NAME`）：它是"包含"
    判据，剥到只剩 ``采购项目`` 这样的通用词就会到处命中——那正是这个项目栽过的
    "看着像就采信"。
    """
    names = []
    for candidate in (_clean(project_name), _strip_title_decorations(project_name)):
        token = _norm(candidate)
        if token and token not in names and (candidate == project_name
                                            or len(token) >= SAME_PROJECT_MIN_NAME):
            names.append(token)
    return names


def _longest_common_run(left: str, right: str, floor: int = SAME_PROJECT_OVERLAP) -> int:
    """两个归一化串的最长连续公共子串长度（只判有没有达到 ``floor``）。"""
    if not left or not right:
        return 0
    if len(left) > len(right):
        left, right = right, left
    for size in range(len(left), floor - 1, -1):
        for start in range(len(left) - size + 1):
            if left[start:start + size] in right:
                return size
    return 0


def _recheck_priority(lead):
    """复核顺序：结果类线索排前面（0），其余排后面（1）。

    **只排序，不筛除**：结果类的一个都没命中时，招标/采购类的线索照样会被复核——
    它们是拿不到成交价，但拿得到项目编号、采购人、预算，仍然是有用的线索。
    ``docs/169`` ③ 给出的只是"先看哪一类"的理由：结果类是 17/20，招标采购类是 0/2。
    """
    if looks_like_award_notice(_lead_value(lead, 'notice_type')):
        return 0
    if looks_like_award_notice(_lead_value(lead, 'title')):
        return 0
    return 1


def _recheck_query(lead) -> str:
    """复核查询词：优先项目编号，其次项目名称，最后退化成设备名称/型号。

    退化成设备名称时命中判定更严（必须出现在官方正文里），见 ``_match_official``。
    """
    return (_lead_value(lead, 'project_id')
            or _lead_value(lead, 'project_name')
            or _lead_value(lead, 'title'))


def _official_body_ok(body) -> bool:
    if not isinstance(body, str) or not body.strip():
        return False
    return any(marker in body for marker in _OFFICIAL_MARKERS)


def _official_result_rank(item) -> int:
    """官方候选公告的查看顺序：结果类排前面（0），其余排后面（1）。

    ``docs/169`` ③ 那组 85% 对 0% 的差距就是这一步的理由：同一批官方结果里，
    先看「中标/成交结果公告」，别把额度花在结构上就没有成交价的招标公告上。
    """
    if not isinstance(item, dict):
        return 1
    title = str(item.get('title') or '')
    if looks_like_award_notice(title):
        return 0
    # 没有标题的结果（通用通道那条 searcher 只回正文）就看正文开头：
    # 公告的落款/标题通常在靠前的位置。
    body = item.get('body_text')
    if isinstance(body, str) and looks_like_award_notice(body[:400]):
        return 0
    return 1


def _match_official(lead, results) -> str:
    """在一批官方结果里找身份对得上的那一条，返回官方 URL；找不到返回空串。"""
    if isinstance(results, dict):
        results = [results]
    if not isinstance(results, (list, tuple)):
        return ''

    want_id = _norm(_lead_value(lead, 'project_id'))
    want_name = _norm(_lead_value(lead, 'project_name'))
    want_title = _norm(_lead_value(lead, 'title'))
    identity_names = _identity_names(_lead_value(lead, 'project_name'))

    # 结果类优先，稳定排序：身份判据一个字不动，只是先看结果类的那几条。
    for item in sorted(results, key=_official_result_rank):
        if not isinstance(item, dict):
            continue
        url = _clean(str(item.get('url') or ''))
        if not url:
            continue
        body = item.get('body_text')
        body = body if isinstance(body, str) else ''
        norm_body = _norm(body)
        got_id = _norm(str(item.get('project_id') or ''))
        got_name = _norm(str(item.get('project_name') or ''))

        # 项目编号精确相等：身份已经锁死，不再要求正文。
        if want_id and got_id and want_id == got_id:
            return url
        if want_id and want_id in norm_body and _official_body_ok(body):
            return url
        if want_name and got_name and want_name == got_name and _official_body_ok(body):
            return url
        # 项目名（含"剥掉标题装饰"的候选名）整体出现在官方正文里。
        for candidate in identity_names:
            if candidate and candidate in norm_body and _official_body_ok(body):
                return url
        # 最后一档：推导名的**最长连续汉字重合 ≥10 字**（``docs/164``/``docs/169``
        # 那把尺子）。只在严格判据都没命中时才走；候选名的长度下限在
        # ``_identity_names`` 里把关。
        for candidate in identity_names:
            if _official_body_ok(body) and (
                    _longest_common_run(candidate, norm_body) >= SAME_PROJECT_OVERLAP
                    or _longest_common_run(candidate, _norm(str(item.get('title') or '')))
                    >= SAME_PROJECT_OVERLAP):
                return url
        # 没有编号也没有项目名时，只有设备名称/型号出现在官方正文里才算命中。
        if not want_id and not want_name and want_title \
                and want_title in norm_body and _official_body_ok(body):
            return url
    return ''


__all__ = [
    'AGGREGATOR_120SHOP',
    'AGGREGATOR_BIDCENTER',
    'AGGREGATOR_CHINAMAE',
    'AggregatorDiscovery',
    'DiscoveryResult',
    'EVIDENCE_GRADE_SECONDARY',
    'Lead',
    'PRICE_EVIDENCE_ELIGIBLE',
    'RECHECK_ERROR',
    'RECHECK_NOT_ATTEMPTED',
    'RECHECK_NOT_FOUND',
    'RECHECK_VERIFIED',
    'RecheckOutcome',
    'SOURCE_KIND_AGGREGATOR',
    'price_evidence_from',
    'looks_like_award_notice',
    'unavailable_fetcher',
    'unavailable_renderer',
]
