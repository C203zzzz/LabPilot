# -*- coding: utf-8 -*-
"""一条脏链接不许拖垮整条车道 —— 回归与留痕。

来历（``output/lane-crash``，原始证据只读自 ``D:\\LabPilot\\eval-sample``）：一轮真实
研究以 ``status=failed`` 结束，``lanes={"price":"failed","alternatives":"failed"}``，
4 条查询、26 条来源，价格与替代品都是 0，报告只剩 20 条缺口——整份样本因此无法用于
评估产品质量。

两条 ``failures`` 记的是 ``ValueError`` 且 ``location={"function":"_checknetloc",
"line":436,"file":"parse.py"}``，那是 CPython 3.12 ``urllib/parse.py`` 的
``_checknetloc``（``urlsplit`` 内部调用）：主机名含非 ASCII 字符、且 NFKC 归一化后
冒出 ``/ ? # @ :`` 之一就抛 ``ValueError``。

本工作区独立复现（``output/lane-crash/repro-02-product-code.txt``）把真正那条串抓
出来了——它不是"某个网页上的链接"，而是**公告正文里的一句中文被裸 URL 正则整段吞进
主机名**（句末全角冒号 NFKC 后是 ``:``）：

    http://www.china-tender.com.cn）进行免费注册，……技术支持电话：400-680-8126。

抛出点是 ``research_sources.attachment_leads`` 的 ``urljoin``，而那个
``try: public_url(url) except ValueError`` 兜底就在它的**下一行**——所以永远轮不到。
这个文件钉住的正是这条链：兜底必须落在解析点之内、失败半径必须是这一条链接。
"""
import pytest

from backend.services import research_sources
from backend.services.procurement_research import document_quality_gaps, note_failure
from backend.services.research_sources import attachment_leads, parse_web_url
from backend.infrastructure import page_reader
from backend.infrastructure.page_reader import resolved_download_links
from backend.infrastructure.search_providers import PublicWebSearchProvider
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture

#: 真实那一轮的触发串（``eval-sample/tmp/e2e-blind/run-B02.json`` 的 sources[6]）。
#: 保留原样：全角括号、全角逗号、全角句号、全角冒号，一个字都不改。
B02_BAD_LINK = (
    'http://www.china-tender.com.cn）进行免费注册，注册完成后请按照网上操作流程进行购买。'
    '中国通用招标网技术支持电话：400-680-8126。'
)

#: 同一台机器的另一种写法：全角冒号紧跟在主机名后面（端口位置）。
BAD_LINK_PORT = 'http://www.example.com：8080/notice/1'

#: NFKC 归一化后才冒出 ``/`` 的那一类（U+2100 展开成 ``a/c``）。
BAD_LINK_NFKC_EXPANDING = 'http://\u2100.example.com/x'


# ---------------------------------------------------------------------------
# ① 兜底入口本身：解析不了返回 None，解析得了照旧
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('value', [B02_BAD_LINK, BAD_LINK_PORT, BAD_LINK_NFKC_EXPANDING,
                                   'http://[::1/x', '//www.example.com：8080/x'])
def test_unparseable_urls_are_none_not_an_exception(value):
    assert parse_web_url(value) is None


@pytest.mark.parametrize('value,base,expected', [
    ('files/a.pdf', 'https://h.example.com/notice/1', 'https://h.example.com/notice/files/a.pdf'),
    ('https://h.example.com/a.pdf', '', 'https://h.example.com/a.pdf'),
    ('http://www.china-tender.com.cn线上）', '', 'http://www.china-tender.com.cn线上）'),
    ('/download?uuid=1', 'https://h.example.com/x/y', 'https://h.example.com/download?uuid=1'),
])
def test_parseable_urls_keep_their_exact_result(value, base, expected):
    """非 ASCII 但 NFKC 不变的地址照旧放行——兜底只挡"解析不了"的。"""
    assert parse_web_url(value, base) == expected


def test_parse_web_url_ignores_non_string_input():
    assert parse_web_url(None) is None
    assert parse_web_url(b'http://a.example.com/') is None


# ---------------------------------------------------------------------------
# ② 四个生产解析点：坏链接跳过一个，其余照旧
# ---------------------------------------------------------------------------

def test_attachment_leads_survives_the_real_b02_body():
    """修法①的原始回归：这条正文过去让整条车道 failed，现在只丢一条候选。"""
    body = ('中标（成交）结果公告\n'
            '（3）有意向的投标人应先在中国通用招标网（' + B02_BAD_LINK + '\n'
            '[报价明细](files/detail.xlsx)\n')
    source = {'url': 'http://www.ccgp.gov.cn/cggg/zygg/gkzb/202608/t20260828_27222928.htm',
              'source_type': 'tender_document', 'body_text': body}
    skipped = []

    leads = attachment_leads(source, model=None, skipped=skipped)

    # ``files/detail.xlsx`` 是相对地址，按来源页的目录展开（来源页末段是文件名）。
    assert [lead['url'] for lead in leads] == [
        'http://www.ccgp.gov.cn/cggg/zygg/gkzb/202608/files/detail.xlsx']
    # 脏链接必须留痕：静默跳过等于把"这一页原本挂着一条读不出来的地址"抹掉。
    assert skipped == [{'raw': B02_BAD_LINK, 'reason': 'unparseable'}]


def test_attachment_leads_still_drops_non_public_urls_silently():
    """对照组：语法没问题、只是不能当公开来源读的，仍旧静默跳过（既有口径）。"""
    source = {'url': 'https://h.example.com/notice/1', 'source_type': 'tender_document',
              'body_text': '中标结果公告\n[下载](http://127.0.0.1/files/a.pdf)\n'}
    skipped = []
    assert attachment_leads(source, skipped=skipped) == []
    assert skipped == []


def test_resolved_download_links_skips_the_malformed_href():
    html = ('<a href="' + BAD_LINK_PORT + '" download>下载文件</a>'
            '<a href="/files/ok.pdf" download>下载明细</a>')
    skipped = []

    found = resolved_download_links(html, 'https://h.example.com/notice/1', skipped=skipped)

    assert found == ['https://h.example.com/files/ok.pdf']
    assert skipped == [{'raw': BAD_LINK_PORT, 'reason': 'unparseable'}]


def test_clean_html_keeps_the_label_when_the_href_cannot_be_parsed():
    """``_clean_html`` 的 href 重写在 ``re.sub`` 回调里：一个坏 href 过去会
    让**整页正文清洗**失败。现在它退化成"这一条没有地址"，锚文本照旧保留。"""
    html = '<html><body><a href="' + BAD_LINK_PORT + '">某某招标网</a>正文</body></html>'

    text = PublicWebSearchProvider._clean_html(html, 'https://h.example.com/notice/1')

    assert '某某招标网' in text
    assert BAD_LINK_PORT not in text


def test_verify_publisher_skips_a_malformed_url_in_the_body():
    """品牌核验会扫正文里的地址：畸形串只该让这一条匹配落空。"""
    from backend.services.source_identity import verify_publisher

    body = ('某某有限公司 官网 ' + BAD_LINK_PORT + '\n'
            '某某有限公司 官方网站 https://brand.example.com/')
    candidate = {'url': 'https://brand.example.com/', 'brand': '某某'}
    sources = [{'url': 'https://inst.example.edu.cn/list', 'body_status': 'fetched',
                'source_type': 'unknown', 'body_text': body}]

    relation = verify_publisher(candidate, sources, '某某')

    assert relation and relation['domain'] == 'brand.example.com'


def test_aggregator_page_with_a_malformed_href_keeps_the_good_leads():
    from backend.services.aggregator_discovery import _parse_bidcenter_search

    html = ('<div class="ssjg-list_bt"><a href="' + BAD_LINK_PORT + '">坏线索</a></div>'
            '<div class="ssjg-list_bt"><a href="https://www.bidcenter.com.cn/news-1-1.html">好线索</a></div>')

    leads, reason = _parse_bidcenter_search('https://www.bidcenter.com.cn/search', '', html, '分光光度计')

    assert reason == ''
    assert [lead.url for lead in leads] == ['https://www.bidcenter.com.cn/news-1-1.html']


# ---------------------------------------------------------------------------
# ③ 留痕：失败要能定位到具体那一条链接，但不能把凭据一起落库
# ---------------------------------------------------------------------------

def test_record_failure_keeps_the_exception_text(research):  # noqa: F811
    service, run, _, _ = research
    error = ValueError("netloc 'www.china-tender.com.cn）进行免费注册' contains invalid "
                       "characters under NFKC normalization")

    service.record_failure(run, '价格核查', error)

    record = run['failures'][-1]
    assert record['stage'] == '价格核查' and record['type'] == 'ValueError'
    # 过去只记 type/code/location，于是"是哪一条链接"永远查不回来。
    assert 'www.china-tender.com.cn' in record['error']
    assert 'NFKC normalization' in record['error']


def test_failure_text_is_redacted_before_it_is_stored(research):  # noqa: F811
    service, run, _, _ = research
    error = ValueError('读取失败 https://hljcg.hlj.gov.cn/gpx-bid-file/a.pdf'
                       '?accessCode=ed5d1f238fcbcbf14a1a086a66eaa154&uuid=2c9089be&token=xyz')

    service.record_failure(run, '价格核查', error)

    stored = run['failures'][-1]['error']
    assert 'hljcg.hlj.gov.cn' in stored            # 定位信息留着
    assert 'ed5d1f238fcbcbf14a1a086a66eaa154' not in stored
    assert 'accessCode=***' in stored and 'uuid=***' in stored and 'token=***' in stored


def test_redaction_also_covers_the_spellings_the_first_list_missed(research):  # noqa: F811
    """两份脱敏合并后钉住的这一组载荷：两边的漏处一条都不许原样落库。

    ``209`` 侧的名字表里有 ``key``，但前面挂着"不许是字母数字下划线"的负向断言
    （防 ``monkey=``／``keyword=`` 这类误伤），而 ``api_key`` 的 ``key`` 前面正是一个
    下划线 —— 于是 ``api_key=``、``apikey=`` 整条漏掉；``password`` 根本不在名单里；
    分隔符只认 ``=``，``api_key: sk-…`` 一个都不认；``authorization=`` 因为 ``auth``
    后面紧跟的不是 ``=`` 也漏。``210`` 侧认这些名字，却漏"没有 scheme 的裸参数"，
    也不掩号码。合并后取并集，这个用例把两边的漏都钉住（比较过程见
    ``设计规范-合并210到master.md``）。
    """
    service, run, _, _ = research
    error = ValueError('拉取失败 https://api.example.com/v1/items?sk=sk-unknown-777'
                       ' api_key=sk-live-9f8a apikey=sk-plain-001 password: hunter2'
                       ' authorization=Bearer%20abc accessCode=XYZ789&signature=deadbeef'
                       ' 联系 13800000000。')

    service.record_failure(run, '价格核查', error)

    stored = run['failures'][-1]['error']
    for secret in ('sk-unknown-777', 'sk-live-9f8a', 'sk-plain-001', 'hunter2',
                   'Bearer%20abc', 'XYZ789', 'deadbeef', '13800000000'):
        assert secret not in stored, secret + ' 原样落库了'
    # 该留的定位信息一条不少：主机名、路径、参数名。要证明的只是"值没落库"。
    assert 'api.example.com/v1/items' in stored
    assert 'accessCode=***' in stored and 'api_key=***' in stored and 'sk=***' in stored


def test_both_failure_exits_share_one_field_name_and_one_redaction(research):  # noqa: F811
    """两个出口（``note_failure`` / ``record_failure``）必须同名字段、同一套脱敏。

    合并前两单各写了一份（字段 ``error`` 与 ``detail``）：字段名一分叉，读留痕的人
    就得先猜是哪条路写的；脱敏一分叉，其中一条路就可能把凭据写进库。这里逐字段比对。
    """
    service, run, _, _ = research
    payload = '炸了 https://h.example.com/a?accessCode=once-only-token'

    note_failure(run, '价格核查', 'source_skipped', ValueError(payload))
    service.record_failure(run, '价格核查', ValueError(payload))

    first, second = run['failures'][-2], run['failures'][-1]
    assert set(first) == set(second) == {'stage', 'type', 'code', 'location', 'error'}
    assert first['error'] == second['error']
    assert 'once-only-token' not in first['error']
    assert 'h.example.com/a' in first['error']


# ---------------------------------------------------------------------------
# ④ 单条来源的失败降级：坏来源记缺口，其余来源照跑
# ---------------------------------------------------------------------------

def test_a_single_bad_source_is_skipped_not_the_whole_lane(research):  # noqa: F811
    service, run, _, _ = research
    source = {'url': 'https://h.example.com/notice/bad', 'source_type': 'tender_document',
              'body_status': 'fetched', 'body_text': '正文'}

    with service.source_scope(run, source, '附件线索'):
        raise ValueError('这一条来源自己的事')

    assert any('附件线索' in gap and source['url'] in gap for gap in run['gaps'])
    stored = run['failures'][-1]
    assert stored['code'] == 'source_skipped' and stored['error'] == '这一条来源自己的事'


def test_source_scope_does_not_swallow_the_stop_signal(research):  # noqa: F811
    """``ResearchStopped`` 是控流（额度用尽），不是失败，必须原样上抛。"""
    from backend.services.procurement_research import ResearchStopped

    service, run, _, _ = research
    before = list(run['gaps'])
    with pytest.raises(ResearchStopped):
        with service.source_scope(run, {'url': 'https://h.example.com/a'}, '附件线索'):
            raise ResearchStopped('额度用尽')
    assert run['gaps'] == before
    assert not run.get('failures')


def test_candidate_scope_degrades_one_candidate_not_the_lane(research):  # noqa: F811
    """替代品车道是**一个候选一份工作**：它的失败半径同样只该是那个候选。"""
    service, run, _, _ = research

    with service.candidate_scope(run, {'model': 'Q20'}, '候选核验'):
        raise ValueError('这一个候选自己的事')

    assert any('候选核验' in gap and 'Q20' in gap for gap in run['gaps'])
    assert run['failures'][-1]['code'] == 'candidate_skipped'


def test_fetch_attachments_degrades_instead_of_raising(research, monkeypatch):  # noqa: F811
    """修法②的落点：附件处理整段包在 ``source_scope`` 里，脏链接不再上抛到车道。"""
    service, run, _, _ = research
    source = {'url': 'https://h.example.com/notice/1', 'source_type': 'tender_document',
              'body_status': 'fetched', 'body_text': '正文'}

    def explode(run_, source_):
        raise ValueError('坏链接')

    monkeypatch.setattr(service, '_fetch_attachments', explode)

    service.fetch_attachments(run, source)      # 不抛

    assert any(source['url'] in gap for gap in run['gaps'])


# ---------------------------------------------------------------------------
# ⑤ OCR 失败但仍有正文：不许静默
# ---------------------------------------------------------------------------

def test_ocr_failed_with_text_is_reported_as_a_gap():
    documents = [{'source_name': '需求.pdf', 'ocr_status': 'failed', 'extracted_text': 'x' * 40021},
                 {'source_name': '对照.pdf', 'ocr_status': 'not_requested', 'extracted_text': 'y' * 10}]

    gaps = document_quality_gaps(documents)

    assert len(gaps) == 1
    assert '需求.pdf' in gaps[0] and '40021' in gaps[0] and 'OCR' in gaps[0]
    assert '对照.pdf' not in gaps[0]


def test_ocr_failed_without_any_text_stays_silent():
    """一个字都没抽出来的那份由"资料没有正文"那条既有链路说话，不在这里重复。"""
    assert document_quality_gaps([{'source_name': 'a.pdf', 'ocr_status': 'failed', 'extracted_text': ''}]) == []
    assert document_quality_gaps([{'source_name': 'a.pdf', 'ocr_status': 'partial', 'extracted_text': '  '}]) == []


def test_report_tells_the_reader_that_a_failed_ocr_still_produced_text(research):  # noqa: F811
    service, run, _, _ = research
    service.repo.put('documents', 'doc-ocr', {'id': 'doc-ocr', 'source_name': '实验室需求.pdf',
                                              'ocr_status': 'failed', 'extracted_text': 'x' * 40021},
                     task_id=run['task_id'])

    report = service.report(run['task_id'], run['id'])
    gaps = next(section for section in report['sections'] if section['key'] == 'gaps')

    assert any('实验室需求.pdf' in item['text'] and '40021' in item['text'] for item in gaps['items'])

    # 幂等：再读一次不会多出一条一样的。
    again = service.report(run['task_id'], run['id'])
    again_gaps = next(section for section in again['sections'] if section['key'] == 'gaps')
    matching = [item for item in again_gaps['items'] if '实验室需求.pdf' in item['text']]
    assert len(matching) == 1


def test_reading_a_stored_report_adds_the_notice_to_the_saved_gaps_section(research):  # noqa: F811
    """库里已有的报告不会自己长出这条提示，读的时候要补上（幂等）。"""
    service, run, _, _ = research
    report = service.report(run['task_id'], run['id'])
    gaps = next(section for section in report['sections'] if section['key'] == 'gaps')
    assert not any('实验室需求.pdf' in item['text'] for item in gaps['items'])

    service.repo.put('documents', 'doc-ocr', {'id': 'doc-ocr', 'source_name': '实验室需求.pdf',
                                              'ocr_status': 'failed', 'extracted_text': 'x' * 40021},
                     task_id=run['task_id'])

    reread = service.report(run['task_id'], run['id'])
    reread_gaps = next(section for section in reread['sections'] if section['key'] == 'gaps')
    assert any('实验室需求.pdf' in item['text'] for item in reread_gaps['items'])


# ---------------------------------------------------------------------------
# ⑥ 模块级兜底：这四个模块的解析点必须都走同一个入口
# ---------------------------------------------------------------------------

def test_page_reader_does_not_reach_for_the_stdlib_parse_directly():
    """``page_reader`` 里除了 ``urlsplit`` 的 scheme 复读，不许再有裸 ``urljoin``。"""
    import inspect
    source = inspect.getsource(page_reader)
    assert 'urllib.parse.urljoin' not in source


def test_research_sources_exposes_one_choke_point():
    assert research_sources.UNPARSEABLE_URL == (ValueError, UnicodeError)
    assert callable(research_sources.parse_web_url)


def test_an_old_report_without_a_summary_also_gets_the_notice(research):  # noqa: F811
    """补这条提示**两条路都要走到**：旧报告第一次打开就得补上，不能等第二次。

    上面那条走的是"报告已经有总述"的快路径。库里早期建的报告没有顶层 ``summary``，
    走的是"补一次总述再写回"那条路——那条路上原来漏了这一步，于是同一份报告**第一次
    打开不补提示、第二次才补**。这里按"报告先建好、资料后上传"的顺序造出来钉住它。
    """
    service, run, _, _ = research
    stored = service.report(run['task_id'], run['id'])
    # 装作这是改动前存下来的报告：整份都在，只是没有顶层 summary
    service.repo.put('reports', stored['report_id'],
                     {**service.repo.get('reports', stored['report_id']),
                      'report': {k: v for k, v in stored.items() if k != 'summary'}},
                     task_id=run['task_id'])
    # 资料是在那份报告建好之后才上传、才识别完的
    service.repo.put('documents', 'doc-ocr', {'id': 'doc-ocr', 'source_name': '实验室需求.pdf',
                                              'ocr_status': 'failed', 'extracted_text': 'x' * 40021},
                     task_id=run['task_id'])

    first = service.report(run['task_id'], run['id'])
    gaps = next(section for section in first['sections'] if section['key'] == 'gaps')
    assert any('实验室需求.pdf' in item['text'] and '40021' in item['text'] for item in gaps['items']), \
        '旧报告第一次读到就要补上识别失败提示，不能等第二次'

    # 幂等：再读一次（这次走的是"已有总述"的快路径）不会多出一条一样的
    again = service.report(run['task_id'], run['id'])
    again_gaps = next(section for section in again['sections'] if section['key'] == 'gaps')
    assert len([item for item in again_gaps['items'] if '实验室需求.pdf' in item['text']]) == 1


# ---------------------------------------------------------------------------
# ⑥ 模块级兜底：这四个模块的解析点必须都走同一个入口
# ---------------------------------------------------------------------------

def test_page_reader_does_not_reach_for_the_stdlib_parse_directly():
    """``page_reader`` 里除了 ``urlsplit`` 的 scheme 复读，不许再有裸 ``urljoin``。"""
    import inspect
    source = inspect.getsource(page_reader)
    assert 'urllib.parse.urljoin' not in source


def test_research_sources_exposes_one_choke_point():
    assert research_sources.UNPARSEABLE_URL == (ValueError, UnicodeError)
    assert callable(research_sources.parse_web_url)
