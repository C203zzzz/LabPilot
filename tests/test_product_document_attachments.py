"""M3：候选产品页挂着的产品资料（数据表／规格书／说明书）里的配置。

三件事各自钉住：
1. **发现**——「数据表」「说明书」「Datasheet」这套产品资料语义要认出来，
   而认证证书、驱动、招投标文书不能跟着进来（白读一份 100 秒的资料是真实成本）；
2. **两层地址**——产品页给的经常是 ``showcontent.aspx?id=…`` 这种**文档包装页**，
   真正的 PDF 在它的 iframe 里（实测 OHAUS：2.3 KB HTML → 11.7 MB PDF）；
3. **判据不放宽**——解析出来的文本仍走 ``product_configuration`` 那同一套判据，
   而且**必须出现目标型号**才算这台机器的资料；匹配不上、读不到、额度不够，
   三件事分别留痕（``gaps`` 给人看、``failures`` 给排查用、来源上的
   ``product_documents`` 记全过程）。
"""
import pytest

from backend.services.product_documents import (
    PRODUCT_DOCUMENT_ROUND_LIMIT, document_wrapper_targets, product_document_leads,
    product_document_rank, speaking_model)
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture

PAGE_URL = 'https://www.ohaus.cn/zh-cn/exploreranalytical-5'
WRAPPER_URL = 'https://dmx.ohaus.com/WorkArea/showcontent.aspx?id=28576'
FILE_URL = 'https://dmx.ohaus.com/assets/0/14/69676/69742/69774/29AE3EC6-FBD0-4B69-9FA5-794B56E868AB.pdf'

#: 产品页正文：型号 + 卖点 + 「文档」区。型号写法与真实页面一致（EX224ZH/AD）。
PRODUCT_PAGE = (
    'Explorer™ 分析天平 | OHAUS\n'
    '产品选择 [https://www.ohaus.cn/zh-cn/exploreranalytical-5]\n'
    '技术参数\n'
    'EX224ZH/AD 最大秤量 220 g 可读性 0.1 mg 秤盘尺寸 90 mm\n'
    '文档\n'
    '[产品操作说明书_EX_(CN)](https://dmx.ohaus.com/WorkArea/showcontent.aspx?id=28576)\n'
    '[Laboratory Market Segment Brochure (CN)](https://dmx.ohaus.com/WorkArea/showcontent.aspx?id=28948)\n'
    '[CE 证书](https://www.ohaus.cn/files/ce-certificate.pdf)\n'
    '[驱动下载](https://www.ohaus.cn/files/driver-setup.zip)\n'
)

#: 文档包装页：正文只有标题与文件名，真地址在 iframe 上。这是 ``read_http`` 看到的
#: 那份清洗结果（实测 142 字，``quality()`` 判它没问题，参数却一个都没有）。
#: 真地址在**原始 HTML** 里（真实抓取会把它落盘，见 ``_product_document_targets``）；
#: 离线替身只给文本，所以这里按"清洗后正文里还留着地址"的那种包装页来测，
#: 两种形态走的是同一个判据。
WRAPPER_PAGE = (
    'Instruction Manual Explorer CN 83021170 U\n'
    '[83021170_Instruction Manual_EX_CN_U.pdf](' + FILE_URL + ')\n'
)
WRAPPER_HTML = (
    '<HTML><head><title>Instruction Manual Explorer CN 83021170 U</title></head><body>'
    "<div id='ContentBlock'><title>83021170_Instruction Manual_EX_CN_U.pdf</title>"
    '<iframe class="ektdmsiframe" src="/assets/0/14/69676/69742/69774/29AE3EC6-FBD0-4B69-9FA5-794B56E868AB.pdf">'
    '</iframe></div></body></HTML>')

#: 只写着型号、挂着资料、压不出参数段的候选详情页（``fetch_candidate_detail_pages``
#: 在这条路上要回退去读资料）。
THIN_PAGE = (
    'EX224ZH/AD 分析天平 | OHAUS\n'
    '文档\n'
    '[产品操作说明书_EX_(CN)](https://dmx.ohaus.com/WorkArea/showcontent.aspx?id=28576)\n'
)

#: 真 PDF 解析出来的正文（真实说明书里 9.1 技术规格那一段的形状）。
MANUAL_TEXT = (
    '9. 技术参数\n9.1 技术规格\n环境条件：仅限室内使用\n海拔高度：2000米\n'
    '称盘：304不锈钢\nEX224ZH/AD 最大秤量 220 g 可读性 0.1 mg\n'
    '内部校准 全自动内校 稳定时间 2 s\n专业型分析天平 Explorer 系列\n')


def product_source(url=PAGE_URL, body=PRODUCT_PAGE):
    return {'url': url, 'body_status': 'fetched', 'body_text': body,
            'source_type': 'product_detail', 'purpose': 'alternatives_detail'}


# --------------------------------------------------------------------------
# 1. 线索判据（纯函数）
# --------------------------------------------------------------------------

def test_product_page_documents_are_leads_and_certificates_are_not():
    leads = product_document_leads(product_source())
    urls = [lead['url'] for lead in leads]
    assert WRAPPER_URL in urls                      # 说明书（没有后缀，靠标签认）
    assert 'https://dmx.ohaus.com/WorkArea/showcontent.aspx?id=28948' in urls
    # 证书与驱动也是 ``.pdf``／``.zip``，但读它们只会白花一次解析。
    assert all('ce-certificate' not in url for url in urls)
    assert all('driver-setup' not in url for url in urls)
    # 页内锚点／这一页自己不算第二份文件。
    assert all(url != PAGE_URL for url in urls)
    assert leads[0]['label'] == '产品操作说明书_EX_(CN)'


def test_only_product_document_labels_or_document_suffixes_are_leads():
    body = (
        '[技术参数](https://vendor.example/product/spec)\n'      # 页内参数页，不是附件
        '[Explorer 数据表](https://vendor.example/files/ex-datasheet)\n'
        '[整机手册](https://vendor.example/files/manual)\n'
        '[招标文件](https://vendor.example/files/tender.pdf)\n'
    )
    urls = [lead['url'] for lead in product_document_leads(product_source(body=body))]
    assert 'https://vendor.example/files/ex-datasheet' in urls
    assert 'https://vendor.example/files/manual' in urls
    assert 'https://vendor.example/product/spec' not in urls
    assert 'https://vendor.example/files/tender.pdf' not in urls


def test_unparseable_document_links_are_recorded_not_silently_dropped():
    skipped = []
    leads = product_document_leads(
        product_source(body='[数据表](http://[::1/数据表.pdf)\n'), skipped=skipped)
    assert leads == []
    assert skipped and skipped[0]['reason'] == 'unparseable'


def test_document_wrapper_targets_reads_iframe_and_cleaned_text():
    assert document_wrapper_targets(WRAPPER_HTML, WRAPPER_URL) == [FILE_URL]
    # 没有原始 HTML 时，清洗后的正文里那条 ``标签 [地址]`` 是同一份文件。
    assert document_wrapper_targets('[数据表](https://vendor.example/files/a.pdf)') == [
        'https://vendor.example/files/a.pdf']
    # 不是文件就不认：这一层不做"凡是 iframe 就再抓一次"的放大器。
    assert document_wrapper_targets('<iframe src="https://vendor.example/player.html">') == []


def test_datasheets_are_read_before_brochures():
    """一页挂十来份资料时先读谁：数据表 → 说明书 → 彩页（排序在截断之前）。"""
    body = (
        '[产品彩页](https://vendor.example/files/brochure.pdf)\n'
        '[整机说明书](https://vendor.example/files/manual.pdf)\n'
        '[技术数据表](https://vendor.example/files/datasheet.pdf)\n'
    )
    leads = product_document_leads(product_source(body=body))
    assert [lead['label'] for lead in leads] == ['技术数据表', '整机说明书']
    assert [lead['rank'] for lead in leads] == [0, 1]


def test_accessory_manuals_are_read_after_the_instrument_manual():
    """实测：同一页四份资料里，前两个名额会被配件手册占掉，整机说明书反而读不到。"""
    body = (
        '[Manual_Density Kit_(EN)](https://dmx.ohaus.com/WorkArea/showcontent.aspx?id=25404)\n'
        '[产品操作手册_Label Printing_(EN FR DE IT ES PL ZH JP RU)]'
        '(https://dmx.ohaus.com/WorkArea/showcontent.aspx?id=4295006693)\n'
        '[产品操作说明书_EX_(CN)](https://dmx.ohaus.com/WorkArea/showcontent.aspx?id=28576)\n'
    )
    leads = product_document_leads(product_source(body=body))
    assert leads[0]['label'] == '产品操作说明书_EX_(CN)'
    assert product_document_rank('Manual_Density Kit_(EN)') == 4


def test_documents_that_were_not_read_are_still_recorded(research):  # noqa: F811
    service, run, provider, _ = research
    page_body = (
        'EX224ZH/AD 分析天平 | OHAUS\n'
        '文档\n'
        '[技术数据表](https://vendor.example/files/a-datasheet.pdf)\n'
        '[整机说明书](https://vendor.example/files/b-manual.pdf)\n'
        '[产品彩页](https://vendor.example/files/c-brochure.pdf)\n'
    )
    provider.bodies = {PAGE_URL: page_body}
    page = service.fetch_source(run, {'url': PAGE_URL, 'source_type': 'product_detail'})

    service.fetch_product_attachments(run, page, 'EX224ZH/AD', '奥豪斯')
    not_read = page['product_documents_not_read']
    assert [entry['label'] for entry in not_read] == ['产品彩页']


def test_speaking_model_requires_the_model_itself_not_the_family():
    assert speaking_model(MANUAL_TEXT, 'EX224ZH/AD') == 'EX224ZH/AD'
    # 候选那一行写的是系列名（回归基线里 OHAUS Explorer™ 分析天平 这一条就是），
    # 说明书里没有这个串——如实说不匹配，而不是硬塞进配置。
    assert speaking_model(MANUAL_TEXT, 'Explorer™ 分析天平') == ''
    assert speaking_model(MANUAL_TEXT, 'EX224') == ''   # 严格 token：EX224 ≠ EX224ZH/AD


# --------------------------------------------------------------------------
# 2. 接线：产品页 → 资料 → 同一套判据
# --------------------------------------------------------------------------

def test_datasheet_in_attachment_becomes_candidate_configuration(research):  # noqa: F811
    service, run, provider, _ = research
    provider.bodies = {
        PAGE_URL: PRODUCT_PAGE,
        WRAPPER_URL: WRAPPER_PAGE,
        FILE_URL: MANUAL_TEXT,
    }
    page = service.fetch_source(run, {'url': PAGE_URL, 'source_type': 'product_detail'})
    run['sources'].append(page)

    usable = service.fetch_product_attachments(run, page, 'EX224ZH/AD', '奥豪斯')
    assert [source['url'] for source in usable] == [FILE_URL]
    # 两层地址都留在痕上：产品页挂的是包装页，参数在包装页后面的 PDF 里。
    entry = page['product_documents'][0]
    assert entry['wrapper_url'] == WRAPPER_URL and entry['resolved_url'] == FILE_URL
    assert entry['status'] == 'read' and entry['model_spelling'] == 'EX224ZH/AD'
    assert entry['body_chars'] == len(MANUAL_TEXT)

    spec, spelling = service._configuration_with_attachments(page, usable, 'EX224ZH/AD', '奥豪斯')
    assert spelling == 'EX224ZH/AD'
    assert spec and '技术规格' in spec['text'] and spec['url'] == FILE_URL
    assert any('产品资料配置' in gap for gap in run['gaps'])


def test_attachment_about_another_machine_is_not_used_as_configuration(research):  # noqa: F811
    service, run, provider, _ = research
    provider.bodies = {PAGE_URL: PRODUCT_PAGE, WRAPPER_URL: WRAPPER_PAGE,
                       FILE_URL: '9.1 技术规格\nXPR 分析天平 最大秤量 220 g\n'}
    page = service.fetch_source(run, {'url': PAGE_URL, 'source_type': 'product_detail'})

    assert service.fetch_product_attachments(run, page, 'EX224ZH/AD', '奥豪斯') == []
    entry = page['product_documents'][0]
    assert entry['status'] == 'other_model' and 'EX224ZH/AD' in entry['reason']
    # 资料正文与出处留着（人工可复核），但**没有**当成本机配置。
    assert any('没有出现型号 EX224ZH/AD' in gap for gap in run['gaps'])


def test_unreadable_attachment_leaves_both_kinds_of_trace(research):  # noqa: F811
    service, run, provider, _ = research
    provider.bodies = {PAGE_URL: PRODUCT_PAGE}          # 资料地址一条都读不到
    page = service.fetch_source(run, {'url': PAGE_URL, 'source_type': 'product_detail'})

    assert service.fetch_product_attachments(run, page, 'EX224ZH/AD', '奥豪斯') == []
    entry = page['product_documents'][0]
    assert entry['status'] in {'failed', 'unavailable'}
    assert any('产品资料读取失败' in gap for gap in run['gaps'])
    # 给用户看的话里不带异常类名；给排查用的那条带得出来。
    failure = next(item for item in run['failures'] if item['stage'] == '产品资料')
    assert failure['location'] == WRAPPER_URL and failure['type'] != '失败'


def test_document_reads_stop_at_the_budget_and_say_so(research):  # noqa: F811
    service, run, provider, _ = research
    provider.bodies = {PAGE_URL: PRODUCT_PAGE, WRAPPER_URL: WRAPPER_PAGE, FILE_URL: MANUAL_TEXT}
    page = service.fetch_source(run, {'url': PAGE_URL, 'source_type': 'product_detail'})
    run['usage']['fetch'] = run['limits']['fetch']       # 额度已经用完

    assert service.fetch_product_attachments(run, page, 'EX224ZH/AD', '奥豪斯') == []
    entry = page['product_documents'][0]
    assert entry['status'] == 'budget_exhausted' and entry['url'] == WRAPPER_URL
    assert any('产品资料未读' in gap and WRAPPER_URL in gap for gap in run['gaps'])


def test_round_limit_stops_the_ninth_document(research):  # noqa: F811
    service, run, provider, _ = research
    provider.bodies = {PAGE_URL: PRODUCT_PAGE, WRAPPER_URL: WRAPPER_PAGE, FILE_URL: MANUAL_TEXT}
    page = service.fetch_source(run, {'url': PAGE_URL, 'source_type': 'product_detail'})
    run['product_document_reads'] = PRODUCT_DOCUMENT_ROUND_LIMIT

    assert service.fetch_product_attachments(run, page, 'EX224ZH/AD', '奥豪斯') == []
    assert page['product_documents'][0]['status'] == 'round_limit'


def test_candidate_detail_page_falls_back_to_its_own_datasheet(research):  # noqa: F811
    """候选详情页只写着型号时，配置从它挂着的资料里来（`fetch_candidate_detail_pages`）。"""
    service, run, provider, _ = research
    detail_url = 'https://vendor.example/product/ex224zh-ad'
    provider.bodies = {detail_url: THIN_PAGE, WRAPPER_URL: WRAPPER_PAGE, FILE_URL: MANUAL_TEXT}
    run.setdefault('detail_page_sources', [])
    candidate = {'model': 'EX224ZH/AD', 'brand': '奥豪斯',
                 'excerpt': 'EX224ZH/AD 分析天平 [' + detail_url + ']'}
    service.fetch_candidate_detail_pages(run, [candidate], [])

    assert candidate['detail_page'].get('coverage') == 'parameters', candidate['detail_page']
    entry = candidate['detail_page']['product_documents'][0]
    assert entry['status'] == 'read' and entry['resolved_url'] == FILE_URL
    assert entry['wrapper_url'] == WRAPPER_URL
    # 候选那一格配置由后面"为候选补足配置"那一步从 ``run['sources']`` 里取——
    # 取的就是同一套 ``product_configuration`` 判据，附件来源已经在里面了。
    from backend.services.product_configuration import product_configuration
    spec = product_configuration(run['sources'], 'EX224ZH/AD', '奥豪斯')
    assert spec and '技术规格' in spec['text'] and spec['url'] == FILE_URL
