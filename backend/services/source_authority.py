"""One place that decides which fetched pages count as official sources.

Why this module exists
----------------------
The price lane only accepts evidence published by a public procurement
publisher: a government body, a school, a hospital, a CDC or a bidding
agency.  Four call sites used to answer that with
``host.endswith(('.gov.cn', '.edu.cn'))``.  A TLD guess is not evidence, and
it failed in both directions:

* hospitals, CDCs and bidding agencies publish on ``*.com`` / ``*.cn`` /
  ``*.org.cn`` hosts, so their award notices were silently rewritten from
  ``tender_document`` back to ``unknown`` and every price record built from
  them was rejected downstream;
* a ``.gov.cn`` host says nothing about whether a page is an award notice.

Two related but different questions were previously conflated by that one
suffix test, and they are answered separately here:

``source_publication_tier`` -- "is the *document* a formal publication?"
    Used to promote a fetched page to ``tender_document``.
``source_publisher_tier`` -- "is the *publisher* one we accept?"
    Used when a page only has to be an institutional page: vouching for a
    manufacturer domain, or an asset register, or a linked announcement.

Both share one classifier and one host list, so the two can never drift.

Tiers
-----
``LOCAL_AUTHORITY``
    ``.gov.cn`` / ``.edu.cn``.  Trusted transport: the historic body test is
    preserved byte for byte, and for the publisher question no body wording
    is required at all.  Sources already accepted must not regress.
``FORMAL_PUBLICATION``
    Any other host whose fetched body shows a *concluded* award or contract
    decision plus several structural announcement fields.

Everything else -- dealer pages, marketplaces, aggregators, solicitations
without a decision, and pages whose body was never fetched -- returns
``None``.  Callers decide whether ``None`` is disqualifying.

Display authority
-----------------
``source_display_authority`` answers a third, narrower question: *may this
source stay in the source list when the task already has a trustworthy award
record?*  The rule is that the product is a comparison tool for historical
award notices, so ``unlisted`` covers the whole commercial lead lane --
aggregators, e-commerce listings, industry portals, product databases -- while
``official_channel`` and ``manufacturer`` keep the three families that are
evidence: official award notices and the institutions that publish them, and
manufacturer documentation used for parameters.

Nothing with grown-up evidence reaches ``unlisted``: local-authority hosts,
official procurement channels, any fetched body passing ``publication_tier`` /
``publisher_tier``, and manufacturer domains proved by ``publisher_evidence``
or by the page's own copyright owner matching a brand in play.

Brand names arrive already normalised, so this module never imports
``procurement_rules`` and the two cannot drift into an import cycle.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

LOCAL_AUTHORITY = "local_authority"
FORMAL_PUBLICATION = "formal_publication"

#: Suffixes that carry an implicit publisher guarantee.
LOCAL_AUTHORITY_SUFFIXES = ('.gov.cn', '.edu.cn')

#: Marketplaces and re-hosting aggregators. Owned here so
#: ``official_search.classify_source`` and the authority tiers cannot drift.
MARKETPLACE_HOST_MARKERS = ('amazon.', 'alibaba.', 'made-in-china.', 'labx.', 'fishersci.')
#: 医价网与采招网：只当**发现渠道**（拿线索），采信一律回到官方原文。登记在这里，
#: 它们的页面就永远拿不到 publisher/publication tier，也就没资格进价格通道——
#: 「报告里不得出现聚合站转载作为价格来源」这条由 ``is_marketplace_or_aggregator``
#: 一处兜住，不依赖调用方自觉。采招网的脱敏在服务端（项目编号「点击查看」、
#: 采购单位「德 (略)」、正文数字 ``N#8``），渲染读 DOM 也救不回来，所以更不能信它。
#: ``qianlima.com``（千里马招标网）是**订阅制**招标信息聚合站，页面是分类列表
#: （真实来源 ``/gjxx/232/index_11_3.html``，正文 22161 字里写着「共有 2577 条相关信息」），
#: 一页里塞着几十条公告的链接标题与摘录，因此天然带着「采购合同」「项目编号」
#: 「成交金额」等结构字段。它不在商户自述词表里（没有面议/购物车），于是原来的
#: 判据（非本地域名 + 有成交措辞 + ≥3 个结构字段）真的把它判成了 formal_publication
#: ——列表页冒充单份公告。前端 ``frontend/src/services/pricePresentation.js:125``
#: 早就把它标成「招标信息平台」，这里补上同一件事。
#: ``yiliaozhaobiao.com``（医招网）与 ``zhaomingzhaobiao.com``（照明招标网）是同一类：
#: 转载其它单位公告的招标信息聚合站。实测（``docs/83-证据等级天花板验证.md`` §2.3/§2.4a，
#: 见 ``docs/待归档-聚合站黑名单扩充.md`` 的逐条复核）这两个域名下有 8 个 (task_id, url)
#: 条目（5 个不同 URL）**用当前判据仍能自己通过** ``is_tender``、拿到 85 分
#: 「已完成招标/采购结果文件，可作为历史成交价证据」——正文够干净（没有面议/现货/购物车
#: 这类商户自述词），成交措辞与结构字段都齐，所以正文判据拦不住它们，只能靠域名登记。
#: 登记后这 8 条全部退回 ``None``；``qianlima.com``（``897412d``）是同类先例，实测有效。
#: 作用面有界：全库 3,283 条研究来源里只有落在这两个域名上的 20 条 (task_id, url) 会变，
#: 988 条 ``.gov.cn``/``.edu.cn`` 来源与 90 条官方公告 OSS 附件来源一条都不动。
#: ``caizhaowang.com``（中国采招网）、``dlnyzb.com``（电力能源招标网）、
#: ``shihuantong.com``（世环通，环保/仪器行业信息平台）是同一类的另外三个，
#: 实测于 ``task-<run-6>`` 的来源清单：三个域名**正文一条都没抓到**
#: （``unavailable`` / ``failed``），那时任何读正文的判据都失效，只能靠域名登记；
#: 登记同时让它们在后端就被认出是聚合站，而不是等展示层去猜。
#: 入库与否只改一件事：这三个域名下与目标型号有关的页面今后拿不到
#: publisher/publication tier，也就进不了价格通道。全库作用面同样有界。
#: ``easyjcx.com``（竞采星 / 全国高校竞价网）与 ``gdedulscg.cn``（广东教采云零散采购
#: 竞价平台）是**用户 2026-09-25 拍板的合规决定**，不是技术决定。依据是两站页脚都有
#: **禁止性条款**：教采云写着「未经本平台授权，严禁以任何形式转载或使用本平台数据，
#: 一经发现，依法追究法律责任」（``docs/135`` 摘录页脚原文、``docs/137``），竞采星
#: 属同一类条款（``docs/129``）。按同一标准，两站**都不做直连数据源**——教采云已从
#: 教育采购平台通道整站摘除（``docs/137``，``gdedulscg`` 在 backend/tests/scripts/frontend
#: 里 0 命中），竞采星从未接入（``docs/131`` 明确"本单不给接入方案"）。
#: 用户的原话是：「不用（拉黑），就把他们当作聚合站处理，借助他们获取项目名称来获取公告」
#: ——所以处置**不是拉黑、不是丢弃**：它们的页面照旧**可以被搜到**（用来发现项目名称/编号），
#: 拿到的线索回官方复核，价格一律从官方公告正文读。这与 ``aggregator_leads()`` 的既有
#: 设计同一条：聚合站 URL 根本没机会变成 ``run['sources']`` 里的一条价格来源。
#: 登记的后果**有界**，只有两件事，且都只落在两个域名自己的页面上：
#: ① ``_unusable_non_local`` 让它们拿不到 publisher/publication tier，因此进不了
#: ``prices()`` 的入口判据（``PRICE_SOURCE_TYPES``）；② ``classify_source`` 把它们认成
#: ``AGGREGATOR``（25 分、``DEALER_LEAD``）而不是 ``TENDER_DOCUMENT``（85 分），
#: 于是排序位次下移——**但不丢弃**，页面仍然会出现在搜索结果里。
#: 不改变的是：搜索引擎照常返回它们（这正是"借助他们获取项目名称"的前提），
#: ``is_marketplace_or_aggregator`` 的**子串匹配**也不碰别的主机——实测本机
#: ``data/app.sqlite3``（17 张表全部字符串字段）扫出 618 个不同的 http host，
#: 新的两个 marker 各自命中 0 个 host、命中非目标域名 0 个（无子串碰撞）；
#: ``gdegp.gds.edu.cn``（含 8093 小零易购）与 ``.gov.cn``/``.edu.cn`` 官方站不受影响。
#: 线索发现端（真去抓这两站、解析项目名称/编号再回官方查）**本单没有实现**，
#: ``run['aggregator_lead_discovery']`` 的默认开关也没有打开。
AGGREGATOR_HOST_MARKERS = ('wikipedia.', 'pdfcoffee.', 'manualslib.',
                           '120shop.cn', 'bidcenter.com.cn', 'qianlima.com',
                           'yiliaozhaobiao.com', 'zhaomingzhaobiao.com',
                           'caizhaowang.com', 'dlnyzb.com', 'shihuantong.com',
                           'shuidi.cn', 'easyjcx.com', 'gdedulscg.cn',
                           # chinamae（原"中国招标与采购网"系的医疗器械/招标聚合站）：
                           # 列表页是静态 HTML、能拿到**干净的项目名**，所以本单把它接成了
                           # 发现渠道（``AGGREGATOR_CHINAMAE``）。它的详情页在服务端把 7 个
                           # 字段打成 ``******``（``docs/164``），载体本身绝不该被当成官方
                           # 公告，所以照样登记在这里——它同 120shop/采招网一样受两条有界后果。
                           'chinamae.com')

#: Historic weak test for local-authority hosts; intentionally unchanged.
_LOCAL_FORMAL = re.compile(
    r'中标(?:（成交）)?(?:结果)?公告|成交(?:结果)?公告|采购(?:结果|合同|公告)|招标公告|最高限价'
    r'|(?:中标|成交)(?:明细|单价\s*[:：]?\s*\d)')
_LOCAL_FIELDS = ('项目编号', '采购人', '成交供应商', '中标金额', '成交金额')

#: Strong test for every other host. It must read as a *concluded* decision:
#: a solicitation ("招标公告", "询价公告") states no award and must not
#: qualify, while every result-notice family must.
FORMAL_DECISION = re.compile(
    r'中标(?:（成交）)?(?:结果)?公告|成交(?:结果)?公告|采购结果公告|中标结果公告|成交结果公告'
    r'|询价结果公告|竞争性谈判结果公告|竞争性磋商结果公告|单一来源(?:采购)?结果公告'
    r'|比选结果公告|竞价结果公告|(?:中标|成交)候选人公示|评标结果(?:公示|公告)|定标(?:结果|公告)'
    r'|采购合同|合同公告|合同明细|合同协议书'
    #: ``采购结果公示`` / ``成交结果公示``：省级政府采购平台（安徽政采云、江西电子卖场）
    #: 的成交公告正文首句就是「本项目采购已经结束，现将采购结果公示如下：」——
    #: 它是**结论性**措辞（采购已结束、结果已定），与 ``采购结果公告`` 同义同层，
    #: 只是这两站的模板用「公示」而不用「公告」。``_LOCAL_FORMAL``（本地权威主机那条
    #: 分支）早就认 ``采购(?:结果|合同|公告)``，所以这是**把非本地分支的措辞表补齐到
    #: 同一层次**，不是新开一条路。
    #:
    #: 爆炸半径是量过的，不是估的（``_probe/p6_blast_radius.py``，只读扫本机
    #: ``data/app.sqlite3``）：**230 条已抓正文的来源里，加这两个词之后新拿到
    #: publication tier 的是 0 条**。而且这一步只是
    #: "这份文件算不算正式成交公告"，金额仍要过 ``parse_amount``/``validate_price``，
    #: 型号仍要逐字出现在数据行里。
    r'|采购结果公示|成交结果公示')
STRUCTURAL_FIELDS = ('项目编号', '采购人', '采购单位', '招标人', '采购机构', '代理机构',
                     '成交供应商', '中标供应商', '成交人', '中标人',
                     '中标金额', '成交金额', '合同金额', '中标价', '成交价',
                     '评审专家', '评标委员会', '中标日期', '成交日期', '公告日期')
MIN_STRUCTURAL_FIELDS = 3

#: Institutional registers (asset books, contract ledgers) are not award
#: notices, but they are still first-party institutional publications. They
#: qualify the *publisher* question only, never the document question, and
#: only together with an institution's own name.
REGISTER_FORM = re.compile(r'资产编号|资产登记|固定资产|资产清单|设备台账|合同编号|验收单|采购合同')
INSTITUTIONAL_ENTITY = re.compile(
    r'[\u4e00-\u9fff]{2,}(?:医院|疾病预防控制中心|疾控中心|卫生健康委|大学|学院|研究院|研究所'
    r'|招标|采购中心|代理机构|公共资源交易)')

#: A seller writes prices on request; an institution publishes a decision.
#: Without a government TLD this is the difference between "the publisher's
#: own award record" and "a shop quoting someone else's award". Deliberately
#: narrow: a bare ``询价`` or ``代理`` also occurs in institutional notices.
SELLER_SELF_DESCRIPTION = re.compile(
    r'面议|现货|经销|代理商|授权代理|旗舰店|专卖店|购物车|加入购物车|在线咨询|在线客服'
    r'|包邮|促销|热卖|库存|在线询价|下单|购买须知|product\s*description')


def host_of(url) -> str:
    """Return the bare hostname of *url*; never raise on untrusted input."""
    if not isinstance(url, str) or not url.strip():
        return ''
    try:
        host = urlsplit(url).hostname or ''
    except ValueError:
        return ''
    return host.lower().removeprefix('www.')


def is_local_authority_host(host) -> bool:
    return isinstance(host, str) and host.lower().endswith(LOCAL_AUTHORITY_SUFFIXES)


def is_marketplace_or_aggregator(host) -> bool:
    if not isinstance(host, str):
        return False
    host = host.lower()
    return any(marker in host for marker in MARKETPLACE_HOST_MARKERS + AGGREGATOR_HOST_MARKERS)


def has_formal_decision(body) -> bool:
    return isinstance(body, str) and bool(FORMAL_DECISION.search(body))


def structural_field_count(body) -> int:
    if not isinstance(body, str):
        return 0
    return sum(field in body for field in STRUCTURAL_FIELDS)


def _has_text(body) -> bool:
    return isinstance(body, str) and bool(body.strip())


def _unusable_non_local(host, body) -> bool:
    """Reasons a non-local host can never be an official publisher.

    Fails closed on an unusable host: an unverifiable publisher must not be
    upgraded by body wording alone.
    """
    if not _has_text(body) or not isinstance(host, str) or not host.strip():
        return True
    if is_marketplace_or_aggregator(host):
        return True
    return bool(SELLER_SELF_DESCRIPTION.search(body))


def _formal_tier(host, body):
    """Tier for a non-local host: requires concluded-decision evidence."""
    if _unusable_non_local(host, body):
        return None
    if has_formal_decision(body) and structural_field_count(body) >= MIN_STRUCTURAL_FIELDS:
        return FORMAL_PUBLICATION
    return None


def publication_tier(host, body):
    """Is this *document* a formal procurement publication?  Tier or ``None``.

    ``body`` must be the fetched text: a title, a snippet or a URL is not
    evidence about what the page says.
    """
    if not _has_text(body):
        return None
    if is_local_authority_host(host):
        if _LOCAL_FORMAL.search(body) or sum(field in body for field in _LOCAL_FIELDS) >= 2:
            return LOCAL_AUTHORITY
        return None
    return _formal_tier(host, body)


def publisher_tier(host, body):
    """Is this page published by a source we accept?  Tier or ``None``.

    Local-authority hosts need no body wording (their historic behaviour);
    every other host must show either formal-decision evidence or a
    first-party institutional register.
    """
    if is_local_authority_host(host):
        return LOCAL_AUTHORITY
    formal = _formal_tier(host, body)
    if formal is not None:
        return formal
    if _unusable_non_local(host, body):
        return None
    if REGISTER_FORM.search(body) and INSTITUTIONAL_ENTITY.search(body):
        return FORMAL_PUBLICATION
    return None


def source_publication_tier(source):
    """Document tier of a fetched source record; ``None`` when not fetched."""
    if not isinstance(source, dict) or source.get('body_status') != 'fetched':
        return None
    return publication_tier(host_of(source.get('url')), source.get('body_text'))


def source_publisher_tier(source):
    """Publisher tier of a fetched source record; ``None`` when not fetched."""
    if not isinstance(source, dict) or source.get('body_status') != 'fetched':
        return None
    return publisher_tier(host_of(source.get('url')), source.get('body_text'))


def is_tender(source) -> bool:
    """Whether the fetched body itself is a formal procurement publication."""
    return source_publication_tier(source) is not None


def linked_publication(source):
    """The parent publication that lends its award wording to *source*, if any.

    A price table attached to an award notice rarely repeats the words "中标"
    or "成交" on its own; the decision lives in the notice that published it.
    Linkage is decided by the *recorded* parent identity, never by searching
    the parent's text for the attachment URL: sites publish relative hrefs, so
    the absolute attachment URL is normally absent from the parent body even
    though the publisher did link the file. The substring test failed exactly
    on the ordinary case and passed only on fixtures that inline absolute URLs.

    Returns ``None`` unless the parent is itself an acceptable publisher, so a
    page that merely claims to be a publication cannot lend its wording.
    """
    if not isinstance(source, dict):
        return None
    publication = source.get('publication_context')
    if not isinstance(publication, dict):
        return None
    if publication.get('linked_url') != source.get('url'):
        return None
    parent = publication.get('url')
    if not parent or source.get('parent_url') != parent:
        return None
    if publisher_tier(host_of(parent), publication.get('body_text')) is None:
        return None
    return publication


# --- 展示权威性：有官方成交记录时，哪些来源才允许留在来源清单里 -----------------
#
# 用户的定位（原话）：「这个产品是面向历史招标公告的比价工具」，所以有官方成交
# 记录时，来源清单只承载三类证据：官方历史招标公告及其发布机构、发布机构自有
# 官网、厂家官网资料。其余（聚合站、电商报价、行业门户、产品库）一律不列。
#
# 这不是"来源可信度排序"，是"这条来源属不属于比价工具的论据"。没有官方成交记录
# 时这个判据不生效——那时商业线索是唯一信号，藏起来用户会以为市场上没有记录。

#: 域名层级里表示"这是官方发布渠道"的标签。``is_local_authority_host`` 只认
#: ``.gov.cn`` / ``.edu.cn`` 后缀，但官方渠道还有 ``*.gob.cn`` 这一类同义后缀，
#: 所以这里按**整段标签**再判一次（``gov`` / ``gob`` / ``edu``，以及带数字的变体
#: ``gov2`` / ``edu3``）。
#:
#: 这里**故意不登记 ``zfcg`` / ``ggzy`` 这类业务缩写**。实测 ``gpx.zfcg.scsczt.cn``
#: 是四川政府采购一体化平台，但"含 ``zfcg`` 标签"不是证据：``zfcg.example.com``
#: 同样含这个标签。缩写表一旦登记，任何把 ``zfcg`` 放到三级域名上的站点都能自称
#: 官方渠道；而真正的官方平台页面由正文判据（``source_publication_tier``）负责认领
#: ——那条路读的是"这份文件是不是正式的成交公告"，比域名缩写更接近事实。实测
#: ``gpx.zfcg.scsczt.cn`` 上的那份公告正是这样被判成 ``official_channel`` 的。
OFFICIAL_CHANNEL_LABELS = frozenset({'gov', 'gob', 'edu'})

#: 已知品牌到域名词的映射，只覆盖"品牌名在页面上是中文、官网域名是英文"这一类。
#: 判据不是这张表本身：没有映射时仍然按同语言比较（``湘仪`` ⊂ ``xiangyilxj`` 走
#: 版权方公司名那条路），表里只补这一步查不到的情况。
MANUFACTURER_BRAND_TOKENS = {'贝克曼库尔特': ('beckman', 'beckmancoulter')}

#: 版权行里的权利方公司名。与 ``product_configuration`` 读产品参数时用的是同一
#: 个形状：版权方公司名里带着品牌，说明这一页是品牌方自己发的（实测
#: ``xiangyilxj.com`` = 湖南湘仪实验室仪器开发有限公司，``xiangyiyt.com`` =
#: 上海仪天科学仪器有限公司 → 第三方展示平台）。
_COPYRIGHT_LINE = re.compile(r'(?:Copyright|版权所有|©)[^\n]{0,200}', re.I)
_COMPANY_NAME = re.compile(r'[\u4e00-\u9fff]{2,35}(?:有限公司|研究所)')

#: 域名核心词里要忽略的前缀/子域标签。
_HOST_NOISE = frozenset({'www', 'm', 'zh', 'en', 'wap'})
#: 两级后缀：``example.com.cn`` 的核心词是 ``example``。
_COMPOUND_SUFFIXES = frozenset({'com', 'net', 'org', 'gov', 'edu'})


def host_label_list(host) -> list:
    """域名按 ``.`` 切出来的整段标签；非字符串一律返回空。"""
    if not isinstance(host, str):
        return []
    return [part for part in host.lower().split('.') if part]


def domain_core(host) -> str:
    """域名的核心词（``www.xiangyilxj.com`` → ``xiangyilxj``）。"""
    parts = [part for part in host_label_list(host) if part not in _HOST_NOISE]
    if not parts:
        return ''
    if len(parts) >= 3 and parts[-1] in {'cn', 'uk', 'jp'} and parts[-2] in _COMPOUND_SUFFIXES:
        return parts[-3]
    if len(parts) >= 2:
        return parts[-2]
    return parts[0]


def is_official_channel_host(host) -> bool:
    """这条来源是不是官方发布渠道（政府/高校/政府采购与公共资源交易平台）。"""
    if is_local_authority_host(host):
        return True
    return any(label in OFFICIAL_CHANNEL_LABELS or re.fullmatch(r'gov\d*|edu\d*', label)
               for label in host_label_list(host))


def copyright_owner(body) -> str:
    """版权行里写的公司名;读不到返回空串。"""
    if not isinstance(body, str):
        return ''
    for match in _COPYRIGHT_LINE.finditer(body):
        found = _COMPANY_NAME.search(match.group())
        if found:
            return found.group()
    return ''


def _is_play_brand(name, brands):
    """``name`` 是不是本轮在用的某个品牌；品牌已归一化，这里只做包含。"""
    if not name:
        return False
    return any(brand and brand in name for brand in brands)


def source_display_authority(source, brands=()):
    """这条来源在「有官方成交记录」时的展示身份。

    ``official_channel``  官方发布渠道（含发布机构自有官网）
    ``manufacturer``      厂家官网 / 品牌方自有页（参数依据）
    ``unlisted``          品牌与机构之外的商业线索，不列入来源清单

    ``brands`` 必须是调用方归一化后的品牌名（``procurement_rules.identity``），
    因为归一化规则属于那个模块，这里不重复实现一套。
    """
    if not isinstance(source, dict):
        return 'unlisted'
    host = host_of(source.get('url'))
    if is_marketplace_or_aggregator(host):
        return 'unlisted'
    if is_official_channel_host(host):
        return 'official_channel'
    if source_publication_tier(source) is not None or source_publisher_tier(source) is not None:
        return 'official_channel'
    if source.get('source_type') in {'manufacturer_website', 'manufacturer_download'}:
        return 'manufacturer'
    if (source.get('publisher_evidence') or {}).get('brand'):
        return 'manufacturer'
    owner = copyright_owner(source.get('body_text'))
    if _is_play_brand(owner, brands):
        return 'manufacturer'
    core = domain_core(host)
    for brand in brands:
        tokens = MANUFACTURER_BRAND_TOKENS.get(brand) or ()
        if any(token and token in core for token in tokens):
            return 'manufacturer'
    return 'unlisted'

