"""厂家产品页挂着的**产品资料**（数据表／规格书／说明书）里的配置。

为什么单独一层
--------------
``product_configuration`` 只吃 ``body_text``。产品页上的参数区常常只写一半，
完整的参数表挂在页面自己那份 PDF 里。而现有的附件链认的是**招投标**语义
（``research_sources.attachment_leads`` 的词表：附件／明细／清单／招标文件／采购合同），
产品页上的「数据表」「说明书」「Datasheet」一个都不在其中；OHaus 这类站点的资料地址又是
``https://dmx.ohaus.com/WorkArea/showcontent.aspx?id=28576``——没有后缀、没有 ``download``
字样，点进去还是**一层 HTML 包装页**，真正的 PDF 在里面的 iframe 上。两层都过不去，
用户的感受就是「附件里就有配置，我自己已经看到了，系统说没有」。

这一层只做三件事，**判据一个字不另写**：

1. ``product_document_leads`` 从一条**已经确认讲这台机器**的产品页正文里抽产品资料线索；
2. ``document_wrapper_targets`` 把"文档包装页"里真正的文件地址取出来；
3. ``speaking_model`` 先回答"这份资料讲的是不是这台机器"（型号必须出现），
   过了这一关的文本才交给 ``product_configuration`` 里那**同一套**判据——
   判据不放宽，这里也不替它打包票。

额度与成本
----------
一份 164 页的产品说明书（实测 11.7 MB）走上传资料那条链要 **103 秒**，
所以调用方必须限量：``PRODUCT_DOCUMENT_LIMIT``（一条产品页最多读几份）与
``PRODUCT_DOCUMENT_ROUND_LIMIT``（一轮合计最多读几份）。视觉识别不开：
解析走 ``DocumentIngestionService`` 的默认档（不传 ``vision_adapter``），
付费视觉一次都不会发出去。
"""
import re
from urllib.parse import unquote

PRODUCT_DOCUMENT_LIMIT = 2
#: 一轮合计上限。按"一份说明书 103 秒"算，6 份约 10 分钟——已经是一轮里能接受的上限，
#: 再往上就等于让整个研究去陪附件解析排队。
PRODUCT_DOCUMENT_ROUND_LIMIT = 6
#: 一轮里最多为几个"文档列表由脚本填"的产品页渲染一次（每次约 10 秒）。
PRODUCT_DOCUMENT_RENDER_LIMIT = 4
#: 一页最多收集多少条线索（排序前）。收集得多一点只是为了让"该先读谁"排得准，
#: 真正读几份仍由 ``PRODUCT_DOCUMENT_LIMIT`` 管。
PRODUCT_DOCUMENT_SCAN_LIMIT = 20

#: 产品资料的文件后缀：这几类是**资料**，不是页面。
_DOCUMENT_SUFFIX = re.compile(r'\.(?:pdf|docx?|xlsx?|pptx?)(?:$|[?#&])', re.I)
#: 产品资料的**标签**语义。刻意不含「技术参数」：那个词在产品页上通常是页内锚点
#: （``#tech-specs``）指向页面自己的表格，不是附件；把它收进来会让这一层去抓
#: 已经抓过的页面，而不是用户说的"附件"。
_PRODUCT_DOCUMENT_LABEL = re.compile(
    r'数据表|规格书|技术规格书|参数表|彩页|产品样本|选型手册|产品手册|技术手册'
    r'|说明书|操作手册|使用手册|用户手册|安装手册|维护手册|快速指南|快速入门|手册'
    r'|datasheet|data\s?sheet|brochure|manual|catalog(?:ue)?|leaflet|flyer'
    r'|spec(?:ification)?\s?sheet',
    re.I)
#: 产品页上也挂着与配置无关的文件（认证证书、软件、公司简介）。这些**不是**产品资料：
#: 读它们只花额度，不产生任何参数。排除词全部来自 64 轮真实台账的抽样复核
#: （``output/m3-probe/replay-labels.json``），不是想当然：
#: * 「网站操作手册」（``gdegp.gds.edu.cn/Manual.aspx``）是**平台自己的帮助页**，
#:   每页页脚都挂着，一轮二十几条，把每页两个读取名额占掉一个；
#: * ``static1.tianyancha.com`` 的企业荣誉标签、``max.book118.com`` 的分享页
#:   （标签是一整句 SEO 标题）、结果公告、采购项目文件、测试申请表——都不是产品资料。
_NOT_PRODUCT_DOCUMENT = re.compile(
    r'证书|certificate|声明|declaration|符合性|RoHS|REACH|软件|software|驱动|driver'
    r'|固件|firmware|SDK|公司简介|about\s?us|招标|采购|中标|成交|报价|名单|名录'
    r'|网站|平台操作|系统操作|服务协议|隐私政策|帮助中心|操作指引'
    r'|荣誉|资质|认定|备案|公示|公告|评审|保荐|通知书|营业执照'
    r'|申请表|申报表|投标|项目|标段|响应文件|竞争性|谈判'
    r'|入职|培训|服务权益|权益|通知|承诺函|认证|sertifitseer'
    r'|honou?r|licen[cs]e|agreement|privacy|\bnotice\b|policy',
    re.I)
#: 产品资料的名字是**文件名**，不会是一句 SEO 标题。实测 ``max.book118.com`` 把整句
#: 标题当标签（「Huaan Securities发行保荐书 说明书用户手册.pdf」），这个上限挡掉它。
_LABEL_MAX = 60

#: 「先读谁」：一份资料越像**这台机器的参数表**，越该先读。顺序与理由：
#: * ``0`` 数据表／规格书——直接就是参数表，含型号与全部规格；
#: * ``1`` 说明书／手册——有「技术规格」章节（实测 OHAUS 那份 164 页说明书里
#:   ``EX224ZH/AD`` 出现在技术规格表上）；
#: * ``2`` 彩页／样本／目录——偏营销，可能只讲系列；
#: * ``3`` 其余（只有后缀能证明是文件的）；
#: * ``4`` **配件／耗材／打印说明**——它讲的是配件，不是整机。实测同一页四份资料里
#:   「Manual_Density Kit」「产品操作手册_Label Printing」占满了前两个名额，而含
#:   ``EX224ZH/AD`` 的那份整机说明书排在第三个、一次都没被读到。
#: 这条顺序**只决定先读谁**，不决定算不算配置——那个仍由 ``speaking_model`` 与
#: ``product_configuration`` 说了算。
_ACCESSORY_DOCUMENT = re.compile(
    r'density\s?kit|\bkits?\b|accessor(?:y|ies)|选件|配件|附件|耗材|label\s?printing'
    r'|标签打印|打印说明|砝码|校准件',
    re.I)
_PRODUCT_DOCUMENT_RANKS = (
    (0, re.compile(r'数据表|规格书|技术规格书|参数表|datasheet|data\s?sheet'
                   r'|spec(?:ification)?\s?sheet', re.I)),
    (1, re.compile(r'说明书|操作手册|使用手册|用户手册|安装手册|维护手册|技术手册'
                   r'|产品手册|手册|manual', re.I)),
    (2, re.compile(r'彩页|产品样本|选型手册|样本|brochure|catalog(?:ue)?|leaflet|flyer', re.I)),
)


def product_document_rank(label, url=''):
    """产品资料的分级：``0`` 最像参数表，``4`` 是配件类（最后读），``3`` 只是"一个文件"。"""
    text = label or ''
    if _ACCESSORY_DOCUMENT.search(text):
        return 4
    for rank, pattern in _PRODUCT_DOCUMENT_RANKS:
        if pattern.search(text):
            return rank
    return 3


def product_document_leads(source, limit=PRODUCT_DOCUMENT_LIMIT, skipped=None):
    """这条产品页正文里挂着的产品资料线索：``[{'url', 'label', 'rank'}]``，保序去重。

    扫描口径与 ``research_sources.attachment_leads`` 一致（Markdown 目标、
    清洗后的 ``标签 [地址]``、裸 URL 三种），因为产品页正文是同一套清洗结果；
    分级的词表不同——**这里只认产品资料**，招投标那套词表一个字不动。

    排序在截断之前（与 ``attachment_leads`` 同一个道理）：一页挂十来份资料时，
    先到先得的截断会把「数据表」排在「彩页」和配件手册后面切掉——实测
    ``ohaus.cn`` 的 Explorer 页四份资料里，含型号的那份整机说明书并不排在第一个。

    ``skipped`` 是可选留痕：正文里读不出地址的候选串原样记下来
    （``{'raw': …, 'reason': 'unparseable'}``）。"跳过"与"丢数据"必须分得开。
    """
    from backend.services.research_sources import parse_web_url, public_url
    leads = []
    body = source.get('body_text') or ''
    links = re.compile(
        r'\[(?P<label>[^\]\n]+)\]\(<?(?P<target>[^\s<>]+?)>?(?:\s+"[^"\n]*")?\)'
        r'|(?P<bare>https?://[^\s<>\[\]"\']+)', re.I)
    for match in links.finditer(body):
        raw_url = match.group('target') or match.group('bare')
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
            continue  # 页内锚点不是第二份文件
        # 清洗后的正文把链接写成 ``标签 [地址]``（不是 Markdown），所以标签要往前
        # 找同一行的前缀——``attachment_leads`` 也是这么取的，两处口径一致。
        start = body.rfind('\n', 0, match.start()) + 1
        label = match.group('label') or body[start:match.start()].strip().rstrip('[')
        if not _is_product_document(url, label):
            continue
        if any(lead['url'] == url for lead in leads):
            continue
        leads.append({'url': url, 'label': label or url.rsplit('/', 1)[-1],
                      'rank': product_document_rank(label, url)})
        if len(leads) >= PRODUCT_DOCUMENT_SCAN_LIMIT:
            break
    leads.sort(key=lambda lead: lead['rank'])
    return leads[:limit]


#: 「这一页有文档／资料区块」的痕迹。产品页的文档列表**经常整块由脚本填**：
#: 实测 ``ohaus.cn`` 的 Explorer 页静态 HTML 里连一个锚点都没有
#: （``download_links=[]``、``unresolved_downloads=0``），只有导航里一个
#: ``#model-documentation`` 的「文档」标题；渲染一次就能拿到 4 条资料。
#: ``research_sources.unresolved_download_anchors`` 看不到这种页面，所以这里另立一条
#: **只用来决定"值不值得渲染一次"** 的弱信号——它不生成任何线索，也不参与采信。
_DOCUMENT_SECTION = re.compile(
    r'文档|资料下载|下载中心|产品资料|技术资料|相关资料|说明书|Downloads?|Documents?'
    r'|Documentation|Resources|Data\s?Sheets?|Brochures?|Manuals?',
    re.I)


def document_section_hint(text):
    """这段内容里有没有"文档／资料"区块的痕迹（有痕迹而没线索 → 值得渲染一次）。

    刻意做得**很松**：它只回答"要不要为这一页花一次渲染"，不回答"有没有附件"。
    真正的线索仍由 ``product_document_leads`` 从渲染后的正文里抽，抽不到就什么都不做。
    """
    return bool(_DOCUMENT_SECTION.search(text or ''))


def _is_product_document(url, label):
    """地址或标签说明它是**产品资料**，且标签没说它是证书／软件／招投标文书。

    地址带文档后缀的，标签仍要过一遍排除词：``CE证书.pdf`` 也是 ``.pdf``，
    但读它只会白花一次解析。标签什么都没说（空）时按地址判定。

    过长的标签**不当文件名用**（`_LABEL_MAX`），但排除词仍看**原标签**：
    分享页的整句 SEO 标题（实测 ``max.book118.com``）与"正文里一行话 + 裸地址"
    （实测公告页上的「项目联系人：… 电话：***」，地址带 ``.pdf``）都要在这一步被挡；
    而真正的"一行长描述 + 裸 PDF 地址"不会含那些词，仍按地址认下来。
    """
    raw = label or ''
    text = '%s %s' % (raw, unquote(url or ''))
    if _NOT_PRODUCT_DOCUMENT.search(text):
        return False
    if len(raw) > _LABEL_MAX:
        raw = ''
    if _PRODUCT_DOCUMENT_LABEL.search(raw):
        return True
    return bool(_DOCUMENT_SUFFIX.search(unquote(url or '')))


#: 文档包装页里真正文件的落点：iframe／embed／object 的 src/data，下载按钮的 href，
#: 以及清洗后的正文里剩下的裸地址（``clean_html`` 会把 iframe 的 src 丢掉、只留
#: ``标签 [地址]`` 这种形式，所以两条都要吃）。
_FILE_REFERENCE = re.compile(
    r'<(?:iframe|embed|object)\b[^>]*?\b(?:src|data)\s*=\s*["\']([^"\']+)["\']'
    r'|<a\b[^>]*?\bhref\s*=\s*["\']([^"\']+)["\']'
    r'|(?P<bare>https?://[^\s<>"\'\])]+?\.(?:pdf|docx?|xlsx?|pptx?))(?=$|[\s\]\)）>])',
    re.I)


def document_wrapper_targets(html, base_url=''):
    """这段内容里指向**文件本身**的地址（绝对化、去重、保序）。

    这是"文档包装页"那一层的解法：产品页给的
    ``showcontent.aspx?id=28576`` 回来的是 2.3 KB 的 HTML，正文只有一个 iframe
    ``/assets/…/29AE3EC6-….pdf`` 和一个 ``downloadasset.aspx?id=28576`` 下载按钮；
    直接按普通页面读，它只压出 142 字的标题与文件名——**参数一个都没有**，
    真正的 11.7 MB PDF 就在那一层后面。

    只认**带文档后缀**的地址：这一层不想变成"凡是 iframe 就再抓一次"的放大器。
    ``base_url`` 是这段内容的出处，相对地址按它绝对化。
    """
    from backend.services.research_sources import parse_web_url
    found = []
    for match in _FILE_REFERENCE.finditer(html or ''):
        raw = match.group(1) or match.group(2) or match.group(3)
        if not raw or not _DOCUMENT_SUFFIX.search(unquote(raw)):
            continue
        url = parse_web_url(raw, base_url)
        if url is None or url in found:
            continue
        found.append(url)
    return found


def speaking_model(text, model):
    """这份资料讲的是不是这个型号——命中就返回**用的那种写法**，否则返回 ``''``。

    用 ``model_spellings`` 的顺序试（原样 → 去掉粘着的中文说明），判据是
    ``contains_model`` 本身：这就是 ``product_configuration`` 内部认型号的那把尺子，
    这里不替它放宽。候选那一行写的可能是系列名而不是型号（实测回归基线里
    ``OHAUS Explorer™ 分析天平`` 这一条），那它在这份资料里就**匹配不上**——
    如实返回 ``''``，由调用方记成"这份资料讲的是别的写法"，而不是硬塞进配置。
    """
    from backend.services.procurement_rules import contains_model, model_spellings
    body = text or ''
    if not body.strip() or not model:
        return ''
    for spelling in model_spellings(model):
        if contains_model(body, spelling):
            return spelling
    return ''
