"""Bounded body snapshots and model-local excerpts, shared by imports/research."""
import hashlib
import re
from pathlib import Path
from urllib.parse import urlsplit, urljoin, unquote
from backend.services.procurement_rules import contains_model
from backend.services.source_authority import STRUCTURAL_FIELDS, is_tender  # noqa: F401 - is_tender re-exported: one shared publisher test


#: A port is ordinary URL syntax; ``user:pass@`` is a credential. The two used to
#: be judged together through ``urlsplit(...).port``, so every public site that
#: keeps a service on a nonstandard port -- and any page linking to one -- was
#: refused as if it carried a login. Measured on ``gdegp.gds.edu.cn``: its result
#: list links to its own ``:8090`` / ``:8093`` sections, and reading such an href
#: raised ``ValueError`` instead of whatever the connection really did
#: (recorded it as a product defect).
#:
#: What keeps this a boundary rather than an open door: credentials are refused
#: by their own test (an ``@`` anywhere in the authority, or a parsed
#: username/password), and whether the target may be reached is decided
#: downstream by ``network_url``, which resolves the host and refuses anything
#: not global -- an arbitrary port is therefore not a way to reach a private
#: service. ``parsed.port`` is kept only to validate the port *syntax*: a
#: malformed port makes ``urlsplit`` raise ``ValueError``, which callers already
#: treat as "not a public URL".
_CREDENTIALS_IN_AUTHORITY = re.compile(r'@')


def public_url(url):
    import ipaddress
    parsed = urlsplit(url)
    host = parsed.hostname or ''
    try:
        parsed.port  # syntax check only; a malformed port raises ValueError here
    except ValueError as exc:
        raise ValueError('来源必须为不含登录凭据的公开 HTTP/HTTPS 网址') from exc
    if (parsed.scheme not in {'http', 'https'} or not host
            or parsed.username or parsed.password
            or _CREDENTIALS_IN_AUTHORITY.search(parsed.netloc)):
        raise ValueError('来源必须为不含登录凭据的公开 HTTP/HTTPS 网址')
    if host.lower() == 'localhost' or host.lower().endswith(('.local', '.localhost')):
        raise ValueError('不能读取本机或私有网络地址')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return url
    if not address.is_global:
        raise ValueError('不能读取本机或私有网络地址')
    return url


def network_url(url):
    """Validate redirects and resolved addresses before opening untrusted leads."""
    import socket
    import ipaddress
    public_url(url)
    host = urlsplit(url).hostname
    addresses = socket.getaddrinfo(host, None)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError('来源域名解析到私有网络，无法作为公开来源读取')
    return url


#: 「这个字符串根本当不了网址」这一类的异常。标准库 ``urlsplit`` 在主机名含非 ASCII
#: 字符、且 NFKC 归一化后冒出 ``/ ? # @ :`` 之一时抛 ``ValueError``
#: （``urllib/parse.py`` 的 ``_checknetloc``，防 IDN 同形字攻击）；IPv6 括号没闭合
#: 也抛 ``ValueError``；``idna`` 编解码失败抛 ``UnicodeError``（``ValueError`` 的子类）。
#: 逐条枚举见 。
#:
#: 实测的触发串不是构造的，是政府采购公告正文里的一句中文被裸 URL 正则整段吞进主机名：
#: ``http://www.china-tender.com.cn）进行免费注册，……技术支持电话：400-680-8126。``
#: —— 句末的全角冒号 NFKC 归一化后变成 ``:``，于是 ``urlsplit`` 抛错，
#: 整条价格车道与替代品车道在那一轮双双 ``failed``。
#:
#: 这几类异常在这一条链上只有一个语义：**这个串不是可用的网址**。所以一律按
#: ``public_url`` 的 fail-closed 处理——跳过这一条链接，绝不让它升级成
#: 「这条来源失败」，更不许升级成「整条车道失败」。
UNPARSEABLE_URL = (ValueError, UnicodeError)


def parse_web_url(href, base_url=''):
    """把**从网页／正文里抄来的**字符串解析成绝对地址；解析不了返回 ``None``。

    这是全仓唯一允许对"来路不明的字符串"做 ``urljoin`` / ``urlsplit`` 的入口：
    一处兜底，语义与 ``public_url`` 一致（fail-closed，见 ``UNPARSEABLE_URL``）。
    返回 ``None`` 只说明"这一个串不能当网址用"，调用方按"跳过这一条链接"处理，
    不记缺口、不中断循环。

    哪些输入必须走这里：正文里正则捞出来的地址、HTML 的 ``href``、HTTP 重定向的
    ``Location``。哪些不必：配置里写死的端点、产品自己拼出来的地址、以及**已经过
    ``public_url`` 的**地址——那三种的"解析不了"是配置错误，应该响亮地失败。
    """
    if not isinstance(href, str):
        return None
    try:
        url = urljoin(base_url, href) if base_url else href
        urlsplit(url)
    except UNPARSEABLE_URL:
        return None
    return url


#: A publisher's own notice lists several documents -- 采购文件, 明细表, 合同,
#: 清单 -- and the table that carries the unit price is routinely not among the
#: first three. A page that is not a publication keeps the historic small cap:
#: its links are guesses about the target model, not a publisher's file list.
PUBLICATION_ATTACHMENT_LIMIT = 10
ATTACHMENT_LIMIT = 3

#: Document types whose own text decides the type; they need no model in the
#: link's line before their attachments are worth looking at (historic rule).
_SELF_TYPED = {'tender_document', 'manufacturer_website', 'manufacturer_download'}

_FILE_EXTENSION = re.compile(r'\.(?:pdf|docx?|xlsx?)(?:$|[?#&])', re.I)
#: A publisher's line items are routinely published *packed*: ``报价明细附件.zip``
#: holds the xlsx/csv/pdf that the notice body never states. The suffix alone
#: proves an archive on a publication (its own file list); everywhere else it
#: still needs a document-shaped label, because any product page links zips.
_ARCHIVE_EXTENSION = re.compile(r'\.(?:zip|csv)(?:$|[?#&])', re.I)
_FILE_HANDLER_PATH = re.compile(r'/(?:attachments?|annex|(?:file/)?download)(?:[/ .]|$)', re.I)
#: A file-ish query parameter names a download even with no suffix to prove it;
#: ccgp's extensionless endpoint is the shape that matters here.
_FILE_QUERY = re.compile(r'[?&](?:uuid|file_?id|attach(?:ment)?_?id|filename|down(?:load)?|file)=', re.I)
_FILE_LABEL = re.compile(
    r'\.(?:pdf|docx?|xlsx?|zip|csv)\b|附件|明细|清单|压缩包|报价|采购文件|招标文件|采购合同|合同附件|attachment|contract',
    re.I,
)


def is_document_link(url, label='', formal=False):
    """Whether *url* plausibly names a document rather than a navigation page.

    ``formal`` marks a page already established as a publisher's publication:
    there the publisher's own file handler is itself evidence, so a link no
    longer has to prove what it is. Everywhere else the historic stricter rule
    stands, because those links are guesses about an untrusted page.

    这个函数也会被"还没过 ``public_url`` 的候选地址"调用，所以它自己必须先
    fail-closed：解析不了的串一律不是文档链接（返回 ``False``），不许把标准库的
    ``ValueError`` 漏给调用方。
    """
    parsed = parse_web_url(url)
    if parsed is None:
        return False
    if _FILE_EXTENSION.search(unquote(parsed)):
        return True
    if _ARCHIVE_EXTENSION.search(unquote(parsed)):
        # ``报价明细附件.zip`` is a publisher's own file list entry. On an
        # untrusted page the same suffix proves nothing by itself, so it keeps
        # needing the document label every other strict-rule link needs.
        return bool(formal or _FILE_LABEL.search(label))
    parts = urlsplit(parsed)
    handler = _FILE_HANDLER_PATH.search(parts.path)
    if not formal:
        return bool(handler and _FILE_LABEL.search(label))
    return bool(handler or _FILE_QUERY.search(parts.query))


#: 附件按「文件名有没有说明它装着关键信息」分级。实测那条 CCGP 中标公告挂了
#: 9 个附件，7 个是中小企业／符合本国产品标准的声明函，只有附件9《招标文件》
#: 有配置——按顺序读等于把 8 次读取花在格式文本上，而配置参数本来就在第 9 个。
#: 分级只决定**先读谁、值不值得读**，不决定内容取舍：一个附件最后有没有用，
#: 仍然由它自己的字节说了算（正文判据见下面的 ``attachment_document_type``）。
#:
#: 这一级只能看 label 与 URL——``attachment_leads`` 是**抓取之前**调用的，那时手里
#: 没有附件正文。这个边界是实测的：600 份真实公告正文上跑 ``attachment_leads``，
#: 112 条非噪声线索里有 5 条 label 是 ``附件 1``（政采云生成的开标／资格审查记录表，
#: 地址里也只有 UUID），抓取前无论怎么加词都判不掉它们——那几条只能交给抓取后的
#: 正文判据。
_ATTACHMENT_KEY_LABEL = re.compile(
    r'招标文件|采购文件|磋商|谈判|询价|招标需求|采购需求|采购要求|需求'
    r'|技术参数|技术规格|定稿|清单|明细|分项报价|报价单|报价表'
    # 「成交结果类」的承诺函要读：某高校那份《主要中标标的承诺函》正文里带着一条
    # 真实成交单价 1239000.00 元；而「中小企业声明函」「售后服务承诺函」是投标人的
    # 格式函件。两者的区别就在名字里有没有「中标标的／成交标的」。
    r'|中标标的|成交标的',
    re.I)
#: 文件名本身就说明「里面既不是采购需求、也不是成交价」的附件。三类：
#:
#: * 投标人的格式化函件——中小企业声明函、承诺函、承诺书；
#: * 平台／评标事务文书——操作说明、使用手册、专家抽取规则、开标（一览）记录表、
#:   资格审查记录表、评标（评审）报告、综合评分表（只记分数）；
#: * 名录——企业名单、咨询电话。
#:
#: **故意不包含「示范文本」**：实测那份名字带「示范文本」的安徽招标文件
#: （``00000000-0000-4000-8000-000000000001.pdf``，47590 字）恰恰含配置
#: （命中「配置清单／技术参数／采购需求／标的名称」），装的是某高校该项目的真实
#: 需求。按名字把它降级就是丢掉唯一写着需求的那份文件。
#: 另外 ``中标结果公告`` 也不升级为关键：它就是这条公告自己，已经在读了。
#:
#: 已知盲区（有意保留，方向都是「宁可多读」）：
#: 1. 关键判据先于本表判定，所以 ``中小企业声明函-定稿.pdf`` 这种「噪声词＋关键词」
#:    的名字会被读一次。反过来做（噪声优先）会把 ``磋商文件及声明函格式.docx``
#:    这种真招标文件判成不读，代价更大。
#: 2. 只写项目名加「-文件集.zip」的名字（陕西那条公告的第二个附件）两边都不命中，
#:    留在 PLAIN 按顺序读。
_ATTACHMENT_FORM_LABEL = re.compile(
    r'声明函|承诺函|承诺书|操作说明|使用说明|操作手册|使用手册|用户手册|服务指南'
    r'|用户指南|专家抽取|开标记录|开标一览|资格审查|符合性审查|评标报告|评审报告'
    r'|评分表|综合评分|名单|名录|咨询电话',
    re.I)
#: 归档容器（zip/rar/7z/tar/gz）不按文件名降级：名字说的是**容器**，不是容器里的东西，
#: 而发布方正是把整套文件打成一个包挂上来的。实测两处都是反例——
#: 黑龙江那条公告的 ``开标记录表.zip`` 解开是 ``报价明细/明细表.xlsx``，是该公告里
#: **唯一**写着分项报价的地方（``tests/test_attachment_archive_landing.py`` 钉着它）；
#: 陕西那条的 ``…（241121002）-文件集.zip`` 名字里连一个类型词都没有。
#: 代价是归档里真装着声明函时也会读一次，额度由 ``PUBLICATION_ATTACHMENT_LIMIT`` 兜着。
_ATTACHMENT_ARCHIVE_SUFFIX = re.compile(r'\.(?:zip|rar|7z|tar|gz)(?:$|[?#])', re.I)

ATTACHMENT_KEY, ATTACHMENT_PLAIN, ATTACHMENT_FORM = 0, 1, 2


def attachment_rank(label, url=''):
    """附件名分级：``0`` 优先读，``1`` 一般，``2`` 不值得读（格式函件／事务文书）。

    **只看文件名与地址，不看正文**——调用点在抓取之前，手里没有附件正文。
    所以这个接口不许改成需要正文；正文判据是另一个纯函数，见
    ``attachment_document_type``。``url`` 只用来认归档后缀（有些页面不写扩展名），
    不参与别的判断。
    """
    text = label or ''
    if _ATTACHMENT_KEY_LABEL.search(text):
        return ATTACHMENT_KEY
    if (_ATTACHMENT_FORM_LABEL.search(text)
            and not _ATTACHMENT_ARCHIVE_SUFFIX.search(text + '\n' + (url or ''))):
        return ATTACHMENT_FORM
    return ATTACHMENT_PLAIN


def attachment_leads(source, model=None, skipped=None):
    """附件的线索：地址、文件名标签、分级；按分级稳定排序后截断到上限。

    排序在截断之前：一个页面挂二十个附件时，先到先得的截断会把「招标文件」
    排在十个声明函后面切掉，而按分级排序保证额度先花在有信息的那几个上。

    ``skipped`` 是可选的留痕列表：正文里那些**根本没法当网址解析**的候选串会
    原样记进去（``{'raw': …, 'reason': 'unparseable'}``）。这一步是必须的——
    脏链接过去会让整条车道失败、失败记录里还看不到是哪一条；修好之后它被静默
    跳过，如果不留痕，就再也没人知道这一页上原本挂着一条读不出来的地址。
    """
    leads = []
    body = source.get('body_text', '')
    formal = source.get('source_type') == 'tender_document'
    limit = PUBLICATION_ATTACHMENT_LIMIT if formal else ATTACHMENT_LIMIT
    # Markdown destinations, cleaned HTML's "label [url]", and bare URLs.
    # Consume labeled links first so closing Markdown punctuation never enters
    # the request URL, and labels remain available for extensionless downloads.
    links = re.compile(
        r'\[(?P<label>[^\]\n]+)\]\(<?(?P<target>[^\s<>]+?)>?(?:\s+"[^"\n]*")?\)'
        r'|(?P<bare>https?://[^\s<>\[\]"\']+)', re.I)
    for match in links.finditer(body):
        raw_url = match.group('target') or match.group('bare')
        # 裸 URL 正则会把紧跟其后的整句中文一起吞进来（实测：
        # ``http://www.china-tender.com.cn）进行免费注册，……电话：400-680-8126。``），
        # 而句末的全角冒号 NFKC 后是 ``:``，``urljoin`` 于是抛 ValueError。
        # 兜底必须在 ``urljoin`` 之前——过去那个 ``try/except`` 包在 ``public_url``
        # 外面、晚了一行，所以永远轮不到。
        url = parse_web_url(raw_url, source.get('url') or '')
        if url is None:
            if skipped is not None:
                skipped.append({'raw': raw_url, 'reason': 'unparseable'})
            continue
        try:
            public_url(url)
        except ValueError:
            continue
        if url.split('#', 1)[0] == (source.get('url') or '').split('#', 1)[0]:
            continue  # an in-page anchor is not a second document
        start = body.rfind('\n', 0, match.start()) + 1
        end = body.find('\n', match.end())
        context = body[start:end if end >= 0 else len(body)]
        if model and source.get('source_type') not in _SELF_TYPED:
            if not contains_model(context, model) and not re.search(r'招标文件|采购文件|采购合同|中标(?:明细|表)|成交(?:明细|表)|报价明细|合同附件', context):
                continue
        label = match.group('label') or body[start:match.start()].strip().rstrip('[')
        if is_document_link(url, label, formal=formal) and not any(lead['url'] == url for lead in leads):
            leads.append({'url': url, 'label': label,
                          'rank': attachment_rank(label, url)})
    leads.sort(key=lambda lead: lead['rank'])
    return leads[:limit]


def attachments(source, model=None):
    """Discover bounded leads; the fetched bytes determine the document type.

    只有文件名说明它装着关键信息的附件才会被读：发布方一次挂十个附件是常态，
    而每个附件都是一次正文读取（额度是有限的）。分级见 ``attachment_rank``，
    其中归档容器（``.zip``）不按名字降级——容器名说明不了容器里的东西。
    """
    return [lead['url'] for lead in attachment_leads(source, model)
            if lead['rank'] != ATTACHMENT_FORM]


def attached_documents(sources, source):
    """The fetched files *source* itself published, in the reverse direction.

    ``linked_publication`` walks attachment -> parent: it is the file that
    records the notice. Nothing records the notice's *own* files, so grounding a
    notice on "its body plus its linked page" silently left out every attachment
    it published -- and the notice's attachments are exactly where its line
    items and buying configuration usually live. Membership is read from the
    same recorded ``parent_url`` identity the other direction uses, so no page
    can claim a file it did not publish, and a *file* is never treated as the
    parent of the notice that published it.
    """
    parent = (source or {}).get('url')
    if not parent:
        return []
    return [other for other in sources
            if other is not source
            and other.get('parent_url') == parent
            and other.get('url') != parent
            and other.get('body_status') == 'fetched'
            and (other.get('body_text') or '').strip()]


# ---------------------------------------------------------------------------
# 抓取之后：按附件**自己的正文**判它是什么文书
# ---------------------------------------------------------------------------
# 附件名靠不住。实测 41 条真实附件里，最有价值的那几份名字是 UUID
# （``00000000-0000-4000-8000-000000000001.pdf`` 是含配置的安徽招标文件、
# ``00000000-0000-4000-8000-000000000002.pdf`` 是带真单价的中标标的表），
# 或者拼音首字母（``xqyesmh.pdf`` 就是「中小企业声明函」）。所以「这份文件算什么」
# 只能等它自己的字节到手之后再问它。
#
# 下面三个都是**纯函数**：不联网、不看调用顺序、不依赖 label/URL。
# ``attachment_rank``（抓取前，只能看 label）与它们各管一半，不要互相替代。

#: 文书类型名一般就印在封面，所以只看**最前面两行非空文字**。
#: 窗口刻意定得这么窄，有真实反例：某高校那份 585 字的《主要中标标的承诺函》
#: （``a38f7c6d-…pdf``）从**第三行**起才出现「承诺函」三个字，而它的表体里带着一条
#: 真实成交单价（药理评价系统 1239000.00 元/套）。窗口放宽到三行，这份真价格表就会
#: 被当成格式函件丢掉。
_ATTACHMENT_TITLE_LINES = 2
#: 招标文件要更宽的窗口：它的封面会把类型名拆成竖排单字
#: （``公``/``开``/``招``/``标``/``文``/``件``），连成词的 ``竞争性磋商文件`` 落在
#: 第 4 行（海口那份 55722 字）、``招标编号`` 落在第 8 行（绍兴那份 55405 字）。
#: 这个宽窗口只用于**认定**招标文件，认定之后不再被任何噪声判据推翻，所以放宽没有
#: 误杀风险；噪声判据仍然只用最窄的两行。
_TENDER_TITLE_LINES = 8
#: 政采云生成的《开标记录表》《资格审查记录表》把类型名印在**页脚**
#: （``政府采购开标记录表``、``开标记录表(结果公告)``），开头只有项目名和字段名。
_ATTACHMENT_FOOTER_CHARS = 300

_TENDER_TITLE = re.compile(
    r'招标文件|采购文件|磋商文件|谈判文件|询价文件|询价通知书|招标编号|采购编号|示范文本')
_ATTACHMENT_RECORD_TITLE = re.compile(
    r'开标记录|开标一览|资格审查|符合性审查|专家抽取|评标报告|评审报告'
    r'|综合得分|得分统计|评分表')
#: 文书类型 → 判据。顺序即优先级：先认封面上的类型名。
_ATTACHMENT_TYPE_RULES = (
    ('form', re.compile(r'声明函|承诺函|承诺书')),
    ('record', _ATTACHMENT_RECORD_TITLE),
    ('help', re.compile(r'操作说明|使用说明|操作手册|使用手册|服务指南|用户指南|网站地图')),
    ('roster', re.compile(r'名单|名录|通讯录|咨询电话')),
    ('award_reason', re.compile(r'推荐理由')),
)
_AWARD_TABLE_MODEL_COLUMN = re.compile(r'规格型号|型号')
_AWARD_TABLE_PRICE_COLUMN = re.compile(r'单价|总价|金额|报价')

#: 这几种文书永远不是采购需求、配置或成交价的证据：投标人的格式化函件、
#: 评标事务记录、企业／联系人名录。
_NOT_EVIDENCE_TYPES = frozenset({'form', 'record', 'roster'})
#: 平台操作说明／使用手册要分上下文：公告自己会写需求，附件里的操作说明是噪声；
#: 厂家页面／厂家下载页挂出来的说明书却是参数的第一手证据
#: （``tests/test_attachment_selection.py::test_a_brand_manual_stays_readable_on_an_untrusted_page``
#: 钉的就是这条）。
_CONTEXT_DEPENDENT_TYPES = frozenset({'help'})
_MANUFACTURER_SOURCE_TYPES = frozenset({'manufacturer_website', 'manufacturer_download'})


def _attachment_title_lines(body, count):
    return [line.strip() for line in body.splitlines() if line.strip()][:count]


def attachment_document_type(body):
    """这份附件**自己的正文**说它是什么文书。

    返回下列之一：

    ``'tender'``
        招标／采购／磋商／谈判／询价文件（含名字带「示范文本」的安徽公开招标文件）。
    ``'award_table'``
        带型号列与价格列的中标明细表（``品目号 | … | 规格型号 | 品牌 | 单价（元）``）。
    ``'award_reason'``
        中标（成交）推荐理由。
    ``'form'``
        投标人的声明函／承诺函／承诺书。
    ``'record'``
        开标记录表、资格审查记录表、专家抽取规则、评分统计表这类评标事务记录。
    ``'help'``
        网站／平台操作说明、使用手册、服务指南。
    ``'roster'``
        企业名单、名录、咨询电话。
    ``'unknown'``
        没认出来。**认不出来按「保留」处理**，不按噪声丢。

    只读文书自己的开头（窗口与理由见 ``_ATTACHMENT_TITLE_LINES``）。这不是偷懒：
    一份招标文件的目录里会提到声明函、开标记录表等每一种文书，按全文搜索会把招标
    文件本身判成噪声；而正文开口的**声明函**（``eaebb2bc-…pdf``）第 6 行就写着
    「采购文件中明确的所属行业」，按全文找「采购文件」又会把声明函判成招标文件。
    """
    if not isinstance(body, str) or not body.strip():
        return 'unknown'
    title = '\n'.join(_attachment_title_lines(body, _ATTACHMENT_TITLE_LINES))
    if _TENDER_TITLE.search('\n'.join(_attachment_title_lines(body, _TENDER_TITLE_LINES))):
        return 'tender'
    for kind, pattern in _ATTACHMENT_TYPE_RULES:
        if pattern.search(title):
            return kind
    if (_AWARD_TABLE_MODEL_COLUMN.search(title)
            and _AWARD_TABLE_PRICE_COLUMN.search(title)):
        return 'award_table'
    if _ATTACHMENT_RECORD_TITLE.search(body[-_ATTACHMENT_FOOTER_CHARS:]):
        return 'record'
    return 'unknown'


def is_evidence_attachment(body, parent_type=None):
    """这份正文够不够格当采购需求／配置／成交价的证据。

    ``parent_type`` 是发布这份附件那一页自己的来源类型。
    ``操作说明``／``使用手册`` 这类文书在**公告**的附件里是噪声（公告自己会写需求），
    在**厂家页面／厂家下载页**上却是参数的第一手证据，所以这一类的取舍要看上下文：
    传 ``manufacturer_website`` / ``manufacturer_download`` 时留着，其余情况丢掉。
    不传就按公告附件的上下文处理。
    """
    kind = attachment_document_type(body)
    if kind in _NOT_EVIDENCE_TYPES:
        return False
    if kind in _CONTEXT_DEPENDENT_TYPES:
        return parent_type in _MANUFACTURER_SOURCE_TYPES
    return True


def key_attachment_documents(documents, parent_type=None):
    """已抓取的附件里，按**自己的正文**判为可能有用证据的那些，保持原顺序。

    ``documents``：可迭代的已抓取来源（``fetch_source`` 落下来的那种 dict，
    只要有 ``body_text``；正文为空的直接不要）。
    ``parent_type``：发布这些附件那一页自己的来源类型，语义见 ``is_evidence_attachment``。
    返回值：同一个 dict 对象组成的新列表，调用方自己再截断额度——本函数不管额度，
    上限仍然由 ``PUBLICATION_ATTACHMENT_LIMIT`` / ``ATTACHMENT_LIMIT`` 决定。

    实测 41 条真实附件的 24 条「带父页且自己是 tender_document」里，这个判据滤掉
    11 条（4 份中小企业声明函、2 份开标记录表、2 份资格审查记录表、2 份专家抽取规则、
    1 份综合得分统计表，合计 12664 字），留下 6 份招标文件、4 份中标明细表、
    1 份中标推荐理由表和 2 份短页面。所有真价格表都留在里面。
    """
    return [document for document in documents
            if isinstance(document, dict)
            and (document.get('body_text') or '').strip()
            and is_evidence_attachment(document.get('body_text'), parent_type)]


#: 公告级结构字段的锚定写法，**直接取自** ``source_authority.STRUCTURAL_FIELDS``
#: ——发布者等级判定用的就是那份词表，不允许这里悄悄长出第二套。后面的元组是同一
#: 字段在真实公告里的常见写法（``代理机构名称``、``公告时间``），只做补充。
#: 会随标包逐行变化的供应商不在这里，见 ``lot_suppliers``。
_NOTICE_ANCHORS = (
    ('project_id', '项目编号', ('采购项目编号', '项目编码', '招标编号')),
    ('procurement_unit', '采购单位', ('采购人', '采购人名称', '采购单位名称',
                                      '招标人', '招标人名称', '建设单位')),
    ('agency', '代理机构', ('代理机构名称', '代理机构全称', '采购代理机构',
                            '采购代理机构名称', '招标代理机构')),
    ('notice_date', '公告日期', ('公告时间', '公告发布时间', '发布日期', '发布时间')),
    ('award_date', '中标日期', ('成交日期', '中标时间', '成交时间', '定标日期')),
)
_MISSING_ANCHORS = [anchor for _, anchor, _ in _NOTICE_ANCHORS if anchor not in STRUCTURAL_FIELDS]
if _MISSING_ANCHORS:  # pragma: no cover - 防漂移：共享词表改了就得同步改这里
    raise RuntimeError(f'STRUCTURAL_FIELDS 里没有这些锚定写法：{_MISSING_ANCHORS}')

NOTICE_LEVEL_LABELS = tuple((key, (anchor,) + tuple(extra))
                            for key, anchor, extra in _NOTICE_ANCHORS)

#: 「标包」与「供应商」的就地标签。一份公告的几个标包可以给不同供应商，所以
#: 供应商必须逐包取；把公告级的那一个套到每条价格上就是张冠李戴。
LOT_LABELS = ('标段名称', '标段（包）', '标段(包)', '标段', '合同包', '合同包号',
              '分包编号', '包号', '包别', '标项')
SUPPLIER_LABELS = ('成交供应商', '供应商名称', '中标供应商名称', '成交供应商名称',
                   '中标供应商', '中标人', '成交人', '中标单位', '成交单位')
if '成交供应商' not in STRUCTURAL_FIELDS:  # pragma: no cover - 防漂移，同上
    raise RuntimeError('STRUCTURAL_FIELDS 里没有「成交供应商」')


_ALL_LABELS = frozenset(label for _, labels in NOTICE_LEVEL_LABELS for label in labels)

#: 表头行里跟在真标签后面的兄弟列名。``序号 | 供应商名称 | 供应商地址 | 金额`` 是
#: 一张表，不是「供应商名称：供应商地址」；照后者读就会把列名当供应商。
#: 这张表是**历史判据**，不是完整清单——兄弟列名是开放集合，见下面两条新判据。
_HEADER_NEIGHBOURS = frozenset({
    '供应商地址', '供应商联系方式', '供应商联系人', '供应商统一社会信用代码',
    '地址', '地 址', '联系方式', '联系电话', '联系人', '序号', '备注', '标项号',
    '其他事项',
    '中标（成交）金额', '中标(成交)金额', '中标成交金额', '中标金额', '成交金额',
    '金额', '单价', '数量', '品牌', '规格型号', '名称', '采购项目名称', '品目',
})

#: 兄弟列名补不完，所以换判据。实测大连公共资源交易网那条公告
#: （某高校教学实验室建设项目，正文第 19 行）是表头
#: ``序号 | 中标（成交）金额(元) | 中标供应商名称 | 中标供应商地址 | 推荐理由``：
#: ``中标供应商名称`` 精确命中标签后，``_value_cell`` 取到的是**兄弟列名**
#: ``中标供应商地址``，而它不在上面那张表里，于是 ``lot_suppliers`` 返回
#: ``{'': '中标供应商地址'}``——列名被当成供应商，而且会真的落库（``apply_notice_fields``
#: 在价格行自己没给供应商时就用这个值）。第 22 行
#: ``标项号 | 供应商名称 | 资格审查结果 | 总得分 | 排名 | 未通过资格审查原因``
#: 同理会抽出 ``资格审查结果``。
#:
#: 两条新判据都不依赖穷举：
#:
#: * **值级**（``_is_header_word``）：取到的值以真实列名词尾结尾、且很短，就是列名。
#: * **行级**（``_is_header_row``）：一行里至少两个列名，且每个列名后面跟的还是列名，
#:   这一整行就是表头，不取任何值。
#:
#: 词尾全部取自真实列名，并且**只收不可能出现在值里的词尾**。这条规则是被实测逼出来的：
#: ``包``／``标段``／``标项`` 本身就是标包名（``第1包``、``第一标段``、``标项1``），
#: ``号`` 结尾的短地址在政采公告里很常见（``大连市经济技术开发区学府大街10号``，6 个字），
#: 一旦它们成为合法词尾，``_value_cell`` 就会把这些**真值**判成兄弟列名丢掉。
#: 它们的列名写法（``标项号``、``其他事项``）因此走 ``_HEADER_NEIGHBOURS`` 精确词表，
#: 不进这张词尾表。误杀比漏判危险得多：漏判最多这一行不认表头。
_COLUMN_NAME_TAILS = (
    '地址', '名称', '编号', '金额', '数量', '型号', '品牌', '结果', '得分', '排名',
    '理由', '原因', '备注', '时间', '日期', '电话', '联系方式', '单位', '单价', '总价',
    '报价', '合价', '价款', '品目', '标的', '产地', '制造商', '供应商', '采购人',
    '联系人', '规格', '说明',
)
#: 值级判据的长度上限取 10：真实列名 ``未通过资格审查原因`` 有 9 个字，而真实值都比
#: 它长或词尾不在表里——``云南和昂医疗器械有限责任公司``（14 字，尾「公司」）、
#: ``云南速霖商贸有限公司``、``惠而好医疗科技（云南）有限公司``、``山西凯力晟达科技有限公司``、
#: ``河南缔恩地美仪器设备有限公司``、``大连中远招标代理有限公司``、``公诚管理咨询有限公司``。
_COLUMN_NAME_MAX_LENGTH = 10
#: 表头行至少要有几个列名（``_is_header_row``）。
_HEADER_ROW_MIN_CELLS = 2
#: 列名括号里常写着这一列的计量单位（``单价（元）``、``中标（成交）金额(元)``、
#: ``报价(总价，元)``）。判定前先摘掉，否则同一列会分裂成好几个写法。括号里的字数
#: 不穷举，只要求它以货币单位收尾。
_COLUMN_UNIT_SUFFIX = re.compile(r'[（(][^）)]{0,8}?(?:万元|千元|元|万)[）)]$')
_COLUMN_SPACES = re.compile(r'\s+')
#: 平台生成的评审专家评分表列头：``专家1``…``专家5``（浙江／政采云公告模板固定的那一行
#: ``标项 | 供应商名称 | 专家1 | … | 商务技术得分 | 报价得分 | 总分``）。
#: 数字结尾的词不算列名（``标项1``、``第1包`` 是标包名，是值），所以这一条只认
#: 「专家」这个固定词头加可选序号，不是「任何词＋数字」。
_EXPERT_COLUMN_NAME = re.compile(r'^专家\s*\d*$')


def _is_header_word(cell):
    """这个单元格是列名而不是值。

    先与已知标签／列名词精确比对（历史判据），再退到「短词以真实列名词尾结尾」。
    后者不是补充而是必需：``中标供应商地址``、``资格审查结果``、``未通过资格审查原因``
    不在任何词表里，而这类兄弟列名是开放集合。
    """
    stripped = cell.strip()
    if not stripped:
        return False
    for candidate in (stripped, _COLUMN_UNIT_SUFFIX.sub('', stripped).strip()):
        if (candidate in _ALL_LABELS or candidate in LOT_LABELS
                or candidate in SUPPLIER_LABELS or candidate in _HEADER_NEIGHBOURS):
            return True
        if _EXPERT_COLUMN_NAME.match(candidate):
            return True
        compact = _COLUMN_SPACES.sub('', candidate)
        if (len(compact) <= _COLUMN_NAME_MAX_LENGTH
                and compact.endswith(_COLUMN_NAME_TAILS)):
            return True
    return False


def _next_cell(cells, index):
    for following in cells[index + 1:]:
        if following:
            return following
    return ''


def _is_header_row(cells):
    """整行都是列名，而不是「标签 | 值」并排写在一行里。

    两个条件缺一不可：至少两个列名，且每个列名后面跟的还是列名。
    ``采购人 | 某高校 | 代理机构 | 某公司`` 这种一列标签一列值的行，第一个标签
    后面就是真值，所以走不过第二条，真值不会被当成表头丢掉。
    """
    columns = [index for index, cell in enumerate(cells) if _is_header_word(cell)]
    if len(columns) < _HEADER_ROW_MIN_CELLS:
        return False
    return all(not _next_cell(cells, index) or _is_header_word(_next_cell(cells, index))
               for index in columns)


def _value_cell(cells, index):
    """标签后面紧跟的那个值；紧跟的是另一个列名就说明这是表头行，不取值。"""
    following = _next_cell(cells, index)
    return '' if not following or _is_header_word(following) else following


def _label_value(line, labels):
    """取一行里标签后面的值：``标签：值`` 或 ``标签 | 值 | 值``。

    只认就地写明的标签，不做近似推测：``采购单位地址`` 不是 ``采购单位``，
    ``采购人代表`` 也不是 ``采购人``——那种相似度匹配正是张冠李戴的来源。
    另外认 ``包别1 | 供应商名`` 这种带编号的包标签。

    整行是表头时一个值都不取：``中标供应商名称`` 后面跟着的 ``中标供应商地址``
    是同一张表里的兄弟列，不是这个标签的值。
    """
    cells = [cell.strip() for cell in re.split(r'[|｜]', line)]
    if _is_header_row(cells):
        return ''
    for index, cell in enumerate(cells):
        if not cell:
            continue
        for label in labels:
            if cell == label:
                return _value_cell(cells, index)
            numbered = re.match(rf'^{re.escape(label)}\s*[（(]?\s*\d+\s*[）)]?$', cell)
            if numbered:
                return _value_cell(cells, index)
            matched = re.match(rf'^.{{0,12}}?{re.escape(label)}\s*[:：]\s*(.+)$', cell)
            if matched:
                return matched.group(1).strip()
    return ''


def _iso_date(value):
    matched = re.search(r'(\d{4})\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})', value or '')
    if not matched:
        return ''
    return f'{int(matched.group(1)):04d}-{int(matched.group(2)):02d}-{int(matched.group(3)):02d}'


#: 项目编号后面常再跟一句括号注释：``09-02-04A-2026-D-F-E15648 （招标文件编号：09-02-04A-…）``。
#: 那不是编号的一部分。这条只用在编号上——采购单位不能这么切，
#: ``惠而好医疗科技（云南）有限公司`` 的括号就是名字本身。
_TRAILING_NOTE = re.compile(r'\s*[（(][^）)]*(?:编号|文件|项目|标段|包)[^）)]*[）)]\s*$')


#: 标签可能和值分成两行（CCGP 正文常见 ``一、项目编号`` / ``FS34000120257981号001``），
#: 也可能缩在 ``1.采购人信息`` 这类小节里只写 ``名 称：``。这两种形态都要认，
#: 否则实测 123 份真实公告里只有 34% 抽得到采购单位。
_NUMBERED_HEAD = re.compile(r'^[\s\d一二三四五六七八九十]+[、.．)）]')
_PURCHASER_SECTION = re.compile(
    r'^[\s\d一二三四五六七八九十.、]*\s*(?:采购人|采购单位|招标人|采购机构)信息\s*[:：]?$')
_AGENCY_SECTION = re.compile(
    r'^[\s\d一二三四五六七八九十.、]*\s*(?:采购代理机构|代理机构|招标代理机构)信息\s*[:：]?$')
_NAMED = re.compile(r'^名\s*称\s*[:：]\s*(.+)$')


def _bare_label(line):
    """``一、项目编号`` / ``项目编号：`` → ``项目编号``；不是标签就原样返回。"""
    stripped = _NUMBERED_HEAD.sub('', line.strip()).strip()
    return stripped.strip('：:').strip()


def _looks_like_label(line):
    """下一行到底是值，还是又一个标签（``一、项目编号`` 紧跟 ``二、项目名称``）。"""
    stripped = line.strip()
    if not stripped:
        return False
    return _bare_label(stripped) in _ALL_LABELS or bool(_NUMBERED_HEAD.match(stripped))


def _next_line_value(lines, labels):
    """标签独占一行时，值在下一行。"""
    for index, line in enumerate(lines):
        if _bare_label(line) not in labels:
            continue
        for following in lines[index + 1:index + 3]:
            candidate = following.strip()
            if not candidate:
                continue
            if _looks_like_label(candidate):
                break
            return candidate
    return ''


def _named_section_value(lines, key):
    """``1.采购人信息`` 小节里的 ``名 称：``。

    小节是会串的：真实公告的第三个小节是 ``3.同级政府采购监督管理部门``，那里的
    ``名 称：绍兴市财政局`` 既不是采购人也不是代理机构。所以任何**别的**编号小节
    都要把小节状态清掉——留着它就会把监管部门的名称写成代理机构。
    """
    if key not in {'procurement_unit', 'agency'}:
        return ''
    section = ''
    for line in lines:
        stripped = line.strip()
        if _PURCHASER_SECTION.match(stripped):
            section = 'procurement_unit'
            continue
        if _AGENCY_SECTION.match(stripped):
            section = 'agency'
            continue
        if _NUMBERED_HEAD.match(stripped):
            section = ''
            continue
        matched = _NAMED.match(stripped)
        if matched and section == key:
            return matched.group(1).strip()
    return ''


def notice_fields(body):
    """公告级结构字段：正文里写一次、同一公告每条价格共用一份的那几个。

    正文里的公告有项目编号、采购单位、代理机构，而 ``price_regions`` 只切型号行
    上下三行；实测那条公告的窗口只有 37 个字符，抽取 prompt 却要求模型返回
    ``procurement_unit`` 与 ``project_id``，于是每条价格都揣着空串走到前端，已存
    运行记录里被采信的价格没有一条填上过。这里在**来源级**读一次正文补齐。

    三种就地形态，按可靠度依次退让：同行 ``标签：值`` / ``标签 | 值``、标签独占
    一行而值在下一行、``采购人信息`` 小节里的 ``名 称：``。读不到就不放进结果，
    调用方据此如实记缺口——不拿品类常识或别的公告来补。
    """
    if not isinstance(body, str) or not body.strip():
        return {}
    lines = body.splitlines()
    fields = {}
    for key, labels in NOTICE_LEVEL_LABELS:
        value = ''
        for line in lines:
            value = _label_value(line, labels)
            if value:
                break
        value = value or _next_line_value(lines, labels) or _named_section_value(lines, key)
        if not value:
            continue
        if key in {'notice_date', 'award_date'}:
            value = _iso_date(value)
        elif key == 'project_id':
            value = _TRAILING_NOTE.sub('', value).strip()
        if value:
            fields[key] = value
    return fields


def lot_key(text):
    """把包名压成可比较的编号：``…第1包`` / ``合同包1`` / ``1包`` 都归到 ``1``。"""
    matched = (re.search(r'第\s*(\d+)\s*(?:包|标段|标项)', text or '')
               or re.search(r'(?:包|标段|标项)\s*[（(]?\s*第?\s*(\d+)', text or ''))
    return matched.group(1) if matched else ''


def lot_suppliers(body):
    """包编号 → 成交/中标供应商，逐包记录，包与包互不覆盖。

    真实公告的形状是「中标信息」段里一段一个标包：``标段名称：…第3包`` 后面跟着
    ``供应商名称：云南速霖商贸有限公司``。同一份公告的三个包给三家不同的公司，
    所以这个映射只能逐包建，给价格用的时候也只能逐包取。
    """
    lots = {}
    current = ''
    for line in body.splitlines():
        lot = _label_value(line, LOT_LABELS)
        if lot:
            current = lot_key(lot) or lot
        supplier = _label_value(line, SUPPLIER_LABELS)
        if supplier:
            lots.setdefault(current, supplier)
    return lots


def supplier_for(body, excerpt, lots, lookback=80):
    """这条引文所属标包的供应商：按引文在正文里的位置向上找最近的包标签。

    分不清就不给：引文定位不到、或者公告有多个标包而上游没有包标签时返回空串，
    宁可这一行为空，也不套用公告里别的标包的那一家。
    """
    if not lots or not isinstance(body, str) or not excerpt:
        return ''
    position = body.find(excerpt)
    if position < 0:
        return ''
    for line in reversed(body[:position].splitlines()[-lookback:]):
        lot = _label_value(line, LOT_LABELS)
        if lot:
            return lots.get(lot_key(lot) or lot, '')
    return lots[''] if len(lots) == 1 and '' in lots else ''


#: 注入价格记录的公告级键。一份公告只有一份，同一来源的每条价格共用。
NOTICE_RECORD_KEYS = ('project_id', 'procurement_unit', 'agency', 'notice_date', 'award_date')
#: 其中缺了要如实记进 ``gaps`` 的那几个。公告日期/成交日期不是每条价格都必须有，
#: 报了只会把缺口列表灌满噪声，所以只注入、不记缺口。
NOTICE_GAP_KEYS = ('project_id', 'procurement_unit', 'agency')


def apply_notice_fields(record, fields, lots, body):
    """把来源级结构字段注入一条价格记录，原样返回它。

    公告级字段一份公告只有一份，同一来源的每条价格共用同一份；供应商必须按这条
    价格**自己的引文位置**逐包取——一份公告的几个标包可以给不同供应商，套公告级的
    那一个就是张冠李戴。抽不到的键名进 ``gaps`` 如实说明，不拿别的公告或品类常识补。
    """
    for key in NOTICE_RECORD_KEYS:
        value = (fields or {}).get(key)
        if value:
            record[key] = value
    # 供应商：表格通道逐行读出来的那个最贴近这一行，模型从窗口里读到的同理；
    # 只有它们都没给，才用「按引文位置回溯标包」的结果补。
    if not record.get('supplier'):
        supplier = supplier_for(body, record.get('excerpt') or '', lots or {})
        if supplier:
            record['supplier'] = supplier
    gaps = record.get('gaps')
    if isinstance(gaps, list):
        for key in (*NOTICE_GAP_KEYS, 'supplier'):
            if not record.get(key) and key not in gaps:
                gaps.append(key)
    return record


def price_regions(body, model, maximum=24000):
    """Retain row boundaries and offsets; prioritize exact-model neighborhoods.

    The window is one line above the model line and two below it. Two below,
    not one: a Chinese award notice often prints the buying block one field per
    line (``名称`` / ``品牌`` / ``规格型号`` / ``数量`` / ``单价(元)``), so the
    amount sits on the line *after* the quantity -- outside a one-line window.
    The region then carried the model but no money at all, the amount gate in
    ``prices()`` dropped the source, the extractor was never called, and the
    report said "还没有取得同型号的成交单价" about a notice whose body already
    held ``规格型号：〔型号〕 数量：1 单价(元)：〔金额〕``.
    """
    lines = body.splitlines(keepends=True)
    regions, offset = [], 0
    for index, line in enumerate(lines):
        if contains_model(line, model):
            start = max(0, index - 1)
            excerpt = ''.join(lines[start:min(len(lines), index + 3)])
            regions.append({'start': sum(map(len, lines[:start])), 'text': excerpt[:maximum]})
        offset += len(line)
    if not regions:
        return []
    retained, used = [], 0
    for region in regions:
        if used >= maximum:
            break
        region['text'] = region['text'][:maximum-used]
        retained.append(region)
        used += len(region['text'])
    return retained


#: 金额和单位写在同一段文本里：``成交价 43,300.00 元``。
_AMOUNT_WITH_UNIT = re.compile(r'\d[\d,.]*\s*(?:万元|千元|元|人民币|CNY|RMB|USD|EUR)', re.I)
#: 价格列名本身，就是"这一格是成交价"的那几个字。两条判据共用同一组词，
#: 免得"标签后直接跟数字"与"标签做表头"两处慢慢漂成两套口径。
_PRICE_WORD = r'单价|成交价|中标价'
#: 货币符号或价格标签紧接数字：``￥43,300`` / ``单价 1200``。
#: 中间允许插一个括号里的货币单位：公告明细常写成 ``单价(元)：17480``，单位在
#: 列名里、数值在冒号后。原先这条正则要求 ``单价`` 直接接数字，于是这种行既过不了
#: "数字+单位"那条（单位在前），也过不了列式表那条（``_MONEY_SHAPED`` 只认带千分位
#: 或两位小数的金额，裸整数 ``17480`` 不算），抽取模型一次都没被调用。
_PRICE_LABEL_WITH_NUMBER = re.compile(
    r'(?:[¥￥$€]|' + _PRICE_WORD + r')\s*'
    r'(?:[（(]\s*(?:万元|千元|元|人民币)\s*[）)])?\s*'
    r'[:：]?\s*\d')

#: 列式表的金额列表头词根（``单价``/``合价``/``金额``/``报价``…）。
_PRICE_COLUMN_WORD = re.compile(r'单价|合价|总价|金额|报价|价款|价税合计')
#: 该列自己声明的货币单位：``单价（元）``/``金额(万元)``。
_PRICE_COLUMN_UNIT = re.compile(r'万元|千元|元|人民币')
#: 上限声明，连它修饰的列名一起：``预算``、``预算金额``、``最高限价``、``预算单价``。
#: 它说的是上限，不是成交价——判定时要整个挖掉；但**同一行**里并排的真实成交单价列
#: （``最高限价（元） | 单价（元）``）必须留下，所以不能拿它作废整行。
_BUDGET_LABEL = re.compile(r'(?:预算|最高限价|限价|控制价|概算|估价|金额上限)(?:金额|单价|总价|合价|价款|价|款)?')

#: 金额形态：带千分位（``43,300.00``/``2,606,600``）或两位小数（``43300.00``/``4.33``）。
#: 后面两个负向前瞻把金额和计量数字分开：``2.0000`` 这种四位小数数量因 ``(?!\d)``
#: 不成立；``2.00(台)``、``2.0000 | 台`` 这类紧跟计量单位的数字也不成立。
_MONEY_SHAPED = re.compile(
    r'(?:\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+\.\d{2})(?!\d)'
    r'(?!\s*\|?\s*[（(]?\s*(?:台|个|套|件|支|只|把|份|批|张|块|组|箱|瓶|袋|管|枚|辆|人'
    r'|年|月|天|次|项|根|盒|卷|桶|对|副|架|米|升|公斤|千克|吨|ml|l|kg|g))',
    re.I)

#: **独占一个单元格**的裸整数（无千分位、无小数点）：``… | 1 | 1239000``。
#:
#: 4–10 位：真实设备的成交价不会是 1–999 元，而序号、包号、数量恰好是 1–3 位；
#: 11 位以上是电话号与编号。要求数字两侧就是单元格边界（``|`` 或行首／行尾），
#: 于是 ``JH25-210000-33918``（编号）、``2025-12-09``（日期）、
#: ``联系电话：010-12345678``（电话）里的数字串**都不算**——它们不是独占单元格，
#: 而且前两个的长度／边界都不满足。这一层认定的只有"这一格里是一个四位数以上的
#: 整数"，够不够当成交价由抽取模型和 ``validate_price`` 去判。
_BARE_INTEGER_CELL = re.compile(r'(?:^|\|)[ \t]*(\d{4,10})[ \t]*(?=\||$)', re.M)


def has_amount_evidence(text):
    """这个 region 是否带着值得交给抽取模型的金额证据。

    **这是成本门，不是采信门**：它只决定"值不值得花一次模型调用"。通过它
    不等于这条价被收下——抽取结果仍要过 ``validate_price``（含型号、品牌、
    逐字引文与金额口径）。反过来，不通过它连"被问一次"的机会都没有。

    三类判据，满足任一即可：

    1. **金额和单位写在一起**（``成交价 43,300.00 元``）。这是原来的判据。
    2. **列式表，表头声明了单位**：货币单位只写在表头（``单价（元）``），数据行
       是裸数字（``43,300.00``）。中国政府采购的「报价明细表」几乎都是这个形状，
       而 ``price_regions`` 取的是型号行上下各一行，表头一般就在 region 里。
       只认第 1 类，这类表就恰好拿不到兜底抽取：表格解析器认不出它，记录型
       模型也不被调用，价格明细直接归零。
    3. **列式表，表头是裸的价格列名**（``… | 数量 | 单价``），数据行是独占单元格
       的裸整数（``… | 1 | 1239000``）。单位写在表外或标题里，列名就是「单价」
       两个字——线上实测形态
       （``task-<run-12>`` 的 ``one real notice page``：整表只有一列价格，
       列名裸「单价」，金额裸整数 ``1239000``）。第 2 类要求表头里出现货币单位，
       第 3 类不要求，但要求数字**独占单元格**且 ≥4 位，把页码、数量、日期、
       电话、编号这类无关数字挡在外面。

    第 2、3 类都要求表头那一行（``_BUDGET_LABEL`` 挖掉上限声明之后）出现价格
    列词根，且判定按行读**原文**，不折叠、不改写：引用仍然逐字落在原文上。

    预算/最高限价被单独处理：``_BUDGET_LABEL`` 把 ``预算金额``、``最高限价``、
    ``预算单价`` 这类上限声明从行里挖掉，剩下的部分还要有价格列词根才算数。
    于是 ``包号 | 预算金额（元） | 最高限价（元）`` 不通过，而
    ``最高限价（元） | 单价（元）`` 里那个真实的成交单价列仍然算数。
    """
    if _AMOUNT_WITH_UNIT.search(text) or _PRICE_LABEL_WITH_NUMBER.search(text):
        return True
    bare_integer = bool(_BARE_INTEGER_CELL.search(text))
    if not (_MONEY_SHAPED.search(text) or bare_integer):
        return False
    for line in text.splitlines():
        declared = _BUDGET_LABEL.sub(' ', line)
        if _PRICE_COLUMN_WORD.search(declared) and _PRICE_COLUMN_UNIT.search(line):
            return True
        if bare_integer and re.search(_PRICE_WORD, declared):
            return True
    return False


def snapshot_path(repo, payload):
    digest = hashlib.sha256(payload).hexdigest()
    root = Path(repo.path).parent / 'research-snapshots'
    root.mkdir(parents=True, exist_ok=True)
    path = root / (digest + '.bin')
    if not path.exists():
        path.write_bytes(payload)
    return {'sha256': digest, 'path': str(path.resolve()), 'size': len(payload)}
