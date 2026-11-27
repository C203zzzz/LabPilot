# -*- coding: utf-8 -*-
"""静态 HTML 里没有地址的下载锚点：识别它，并且只为它渲染一次。

CCGP 把附件列表写成 ``<a class='bizDownload' href='' title='点击下载'>文件名</a>``，
地址由页面脚本填上（实测渲染后为 ``//download.ccgp.gov.cn/oss/download?uuid=<id>``）。
``attachments()`` 从正文里抽不到任何地址，于是报「没有附件」——附件9《招标文件》
就是这样丢的，而整份配置参数都在那份文件里。

夹具是四份真实的 CCGP 详情页快照，覆盖「只有空 href」「空 href 与直链混合」
「只有直链」三种形态。
"""
import re
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.infrastructure.page_reader import (PageReadError, PageResult, PublicPageReader,
                                                resolved_download_links, unresolved_download_anchors)
from backend.infrastructure.search_providers import PublicWebSearchProvider
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture

FIXTURES = Path(__file__).parent / 'fixtures' / 'ccgp-index-study-2026-09-23'
NOTICE = 'http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202603/t20260325_26313581.htm'
#: 真实页面上渲染出来的锚点：地址是协议相对的，正文抽取层要能绝对化。
FILLED_ANCHOR = ("<a class='bizDownload' "
                 "href='//download.ccgp.gov.cn/oss/download?uuid=b695c131-8d60-4109-9735-a09d7f' "
                 "id='b695c131-8d60-4109-9735-a09d7f' title='点击下载'>招标文件（第四批）-定稿.docx</a>")


@pytest.fixture(autouse=True)
def _clear_render_cache():
    """渲染缓存是进程级的，测试之间必须互不影响。"""
    from backend.infrastructure import page_reader
    page_reader._download_render_cache.clear()
    yield
    page_reader._download_render_cache.clear()


def _fixture(name):
    return (FIXTURES / name).read_text(encoding='utf-8', errors='replace')


@pytest.mark.parametrize('name,expected', [
    ('detail-zygg-central-empty-href.html', 2),            # 2 个空 href，页面里没有任何直链
    ('detail-guangdong-qinggong-TBSPX-150BIII.html', 4),   # 4 个空 href + 4 条直链
    ('detail-zhujiang-DW-86L578S.html', 5),                # 5 个空 href（含报价明细附件.zip）
    ('detail-chengde-MD-86L578.html', 0),                  # 只有直链：不该为它渲染
])
def test_pages_are_marked_only_when_a_download_anchor_carries_no_url(name, expected):
    assert unresolved_download_anchors(_fixture(name)) == expected


def test_a_full_href_and_a_bare_file_name_are_not_mistaken_for_a_missing_url():
    """判据是「有下载语义且没有 URL」，不是「出现了下载字样」。"""
    html = ("<a href='/files/a.pdf'>产品手册.pdf</a>"
            "<a href='https://vendor.example/download?id=1'>资料下载</a>"
            "<a href='#top'>下载说明</a>")
    assert unresolved_download_anchors(html) == 0


def test_the_rendered_dom_gives_back_absolute_urls():
    assert resolved_download_links(_fixture('detail-zygg-central-empty-href.html'), NOTICE) == []
    assert resolved_download_links(FILLED_ANCHOR, NOTICE) == [
        'http://download.ccgp.gov.cn/oss/download?uuid=b695c131-8d60-4109-9735-a09d7f']


def test_the_rendered_dom_keeps_one_url_when_the_page_repeats_the_anchor():
    """页面上附件列表出现两次（公告概要表和附件区），去重后仍是一个地址。"""
    assert resolved_download_links(FILLED_ANCHOR + FILLED_ANCHOR, NOTICE) == [
        'http://download.ccgp.gov.cn/oss/download?uuid=b695c131-8d60-4109-9735-a09d7f']


def test_an_http_read_records_how_many_download_anchors_have_no_url(monkeypatch):
    """调用方据此决定要不要渲染，而不是等抽取层报「没有附件」。"""
    reader = PublicPageReader()
    html = _fixture('detail-zygg-central-empty-href.html').encode('utf-8')
    monkeypatch.setattr(reader.http, 'fetch_raw', lambda url, timeout: (html, 'text/html; charset=utf-8'))
    assert reader.read_http('https://www.ccgp.gov.cn/award.htm').metadata['unresolved_downloads'] == 2
    plain = '<p>' + '正文 ' * 400 + '</p>'
    monkeypatch.setattr(reader.http, 'fetch_raw', lambda url, timeout: (plain.encode('utf-8'), 'text/html'))
    assert reader.read_http('https://www.ccgp.gov.cn/award.htm').metadata['unresolved_downloads'] == 0


def test_the_rendered_request_does_not_announce_a_headless_browser(monkeypatch):
    """无头 UA 里带 HeadlessChrome，政府采购站点据此直接 403（实测 CCGP）。

    这里断言的是渲染请求用的是与 HTTP 那一遍同一个常规浏览器标识。
    """
    monkeypatch.delenv('LAB_BROWSER_USER_AGENT', raising=False)
    seen = {}

    class Page:
        url = 'https://www.ccgp.gov.cn/award.htm'

        def goto(self, *args, **kwargs):
            return SimpleNamespace(status=200)

        def content(self):
            return '<p>' + '正文 ' * 400 + '</p>'

        def wait_for_timeout(self, delay):
            return None

    page = Page()
    context = SimpleNamespace(route=lambda *a: None, new_page=lambda: page)

    def new_context(**kwargs):
        seen.update(kwargs)
        return context

    browser = SimpleNamespace(new_context=new_context, close=lambda: None)
    runtime = SimpleNamespace(chromium=SimpleNamespace(launch=lambda **k: browser))
    monkeypatch.setattr('playwright.sync_api.sync_playwright', lambda: nullcontext(runtime))
    monkeypatch.setattr('backend.infrastructure.page_reader.network_url', lambda url: url)
    PublicPageReader(browser=True).read_browser(page.url)
    assert 'Headless' not in seen['user_agent']
    assert seen['user_agent'] == PublicWebSearchProvider()._request_headers(page.url)['User-Agent']


def test_a_configured_user_agent_is_used_and_a_challenge_page_still_fails(monkeypatch):
    """UA 可配置，但换 UA 不是反爬绕过：验证码页照旧判失败。"""
    monkeypatch.setenv('LAB_BROWSER_USER_AGENT', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Vendor/1.0')
    seen = {}

    class Page:
        url = 'https://www.ccgp.gov.cn/award.htm'

        def goto(self, *args, **kwargs):
            return SimpleNamespace(status=200)

        def content(self):
            return '<p>请完成人机验证</p><p>' + '正文 ' * 400 + '</p>'

        def wait_for_timeout(self, delay):
            return None

    page = Page()
    context = SimpleNamespace(route=lambda *a: None, new_page=lambda: page)

    def new_context(**kwargs):
        seen.update(kwargs)
        return context

    browser = SimpleNamespace(new_context=new_context, close=lambda: None)
    runtime = SimpleNamespace(chromium=SimpleNamespace(launch=lambda **k: browser))
    monkeypatch.setattr('playwright.sync_api.sync_playwright', lambda: nullcontext(runtime))
    monkeypatch.setattr('backend.infrastructure.page_reader.network_url', lambda url: url)
    with pytest.raises(PageReadError, match='verification_required'):
        PublicPageReader(browser=True).read_browser(page.url)
    assert seen['user_agent'] == 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Vendor/1.0'


def test_the_same_page_is_rendered_at_most_once(monkeypatch):
    reader = PublicPageReader(browser=True)
    calls = []
    rendered = PageResult(b'<html></html>', 'text/html', '正文', {'resolved_downloads': ['https://d.example/a']})
    monkeypatch.setattr(reader, 'read_browser', lambda *args: calls.append(1) or rendered)
    first = reader.read_resolved_downloads('https://www.ccgp.gov.cn/once-1.htm')
    second = reader.read_resolved_downloads('https://www.ccgp.gov.cn/once-1.htm')
    assert calls == [1] and first is second is rendered


def test_a_render_that_failed_is_not_retried_for_the_same_page(monkeypatch):
    """一次会话里同一个页面最多渲染一次，失败也一样：重试只会再拖十几秒。"""
    reader = PublicPageReader(browser=True)
    calls = []

    def denied(*args):
        calls.append(1)
        raise PageReadError('http_403', 403)

    monkeypatch.setattr(reader, 'read_browser', denied)
    for _ in range(2):
        with pytest.raises(PageReadError):
            reader.read_resolved_downloads('https://www.ccgp.gov.cn/denied-1.htm')
    assert calls == [1]


#: 渲染后正文里的附件行，形状与实测的渲染 DOM 文本化一致。
RENDERED_ATTACHMENTS = [
    ('中小企业声明函（1包）.pdf', 'http://download.ccgp.gov.cn/oss/download?uuid=1'),
    ('关于符合本国产品标准的声明函.pdf', 'http://download.ccgp.gov.cn/oss/download?uuid=2'),
    ('中小企业声明函（3包）.pdf', 'http://download.ccgp.gov.cn/oss/download?uuid=4'),
    ('中标结果公告.pdf', 'http://download.ccgp.gov.cn/oss/download?uuid=7'),
    ('招标文件（第四批）-定稿.docx', 'http://download.ccgp.gov.cn/oss/download?uuid=9'),
]
RENDERED_BODY = '\n'.join('附件%d | %s [%s] | %s [%s]' % (number, name, url, name, url)
                          for number, (name, url) in enumerate(RENDERED_ATTACHMENTS, 1))
TENDER_URL = [url for name, url in RENDERED_ATTACHMENTS if '招标文件' in name][0]
DECLARATION_URLS = [url for name, url in RENDERED_ATTACHMENTS if '声明函' in name]


def _render_capable_reader(monkeypatch, calls, bodies, body=RENDERED_BODY):
    """一个已经通过相关性的公告：HTTP 那一遍读到若干没有地址的下载锚点。"""
    reader = PublicPageReader(browser=True)

    def render(url, timeout=25, on_attempt=None):
        calls.append(url)
        if on_attempt:
            on_attempt()
        return PageResult(b'<html></html>', 'text/html', body,
                          {'reader': 'browser', 'snapshot_kind': 'rendered_dom', 'status_code': 200})

    def read(url, timeout=15, on_attempt=None):
        if on_attempt:
            on_attempt()
        return PageResult(bodies[url].encode('utf-8'), 'text/html; charset=utf-8', bodies[url],
                          {'reader': 'http', 'snapshot_kind': 'response_bytes', 'status_code': 200})

    monkeypatch.setattr(reader, 'read_resolved_downloads', render)
    monkeypatch.setattr(reader, 'read', read)
    return reader


def test_a_relevant_notice_without_attachment_urls_is_rendered_and_useful_files_read(research, monkeypatch):
    service, run, provider, _ = research
    calls = []
    bodies = {url: '附件正文 ' + url for _, url in RENDERED_ATTACHMENTS}
    service.search.reader = _render_capable_reader(monkeypatch, calls, bodies)
    notice = {'url': NOTICE, 'source_type': 'tender_document', 'body_status': 'fetched',
              'body_text': '峨山彝族自治县第四批医疗设备采购项目中标公告 规格型号：XKA-2200 单价(元)：17480',
              'read_metadata': {'reader': 'http', 'unresolved_downloads': 9}}
    run['sources'].append(notice)
    service.fetch_attachments(run, notice)
    assert calls == [NOTICE]                                    # 渲染恰好一次
    fetched = {source['url'] for source in run['sources']}
    assert TENDER_URL in fetched                                # 招标文件读到了
    assert not set(DECLARATION_URLS) & fetched                  # 声明函一次都没读
    assert run['download_renders'][0]['resolved'] == 2
    assert len(notice['attachment_skipped']) == len(DECLARATION_URLS)


def test_a_page_whose_download_anchors_already_have_urls_is_not_rendered(research, monkeypatch):
    service, run, provider, _ = research
    calls = []
    service.search.reader = _render_capable_reader(monkeypatch, calls, {})
    notice = {'url': NOTICE, 'source_type': 'tender_document', 'body_status': 'fetched',
              'body_text': '中标公告 [招标文件.docx](https://files.example/tender.docx)',
              'read_metadata': {'reader': 'http', 'unresolved_downloads': 0}}
    run['sources'].append(notice)
    provider.bodies = {'https://files.example/tender.docx': '正文'}
    service.fetch_attachments(run, notice)
    assert calls == []
    assert not run.get('download_renders')


def test_the_render_is_skipped_when_the_attachment_budget_is_gone(research, monkeypatch):
    service, run, provider, _ = research
    calls = []
    service.search.reader = _render_capable_reader(monkeypatch, calls, {})
    run['usage']['fetch'] = run['limits']['fetch']
    notice = {'url': NOTICE, 'source_type': 'tender_document', 'body_status': 'fetched',
              'body_text': '中标公告 规格型号：XKA-2200',
              'read_metadata': {'reader': 'http', 'unresolved_downloads': 9}}
    run['sources'].append(notice)
    service.fetch_attachments(run, notice)
    assert calls == []
    assert any('额度已用完' in gap for gap in run['gaps'])


def test_a_source_that_never_passed_relevance_is_not_examined(research, monkeypatch):
    """空 href 不是触发条件之一：不相关来源连附件都不看。"""
    service, run, provider, _ = research
    calls = []
    service.search.reader = _render_capable_reader(monkeypatch, calls, {})
    unrelated = {'url': 'https://vendor.example/about', 'source_type': 'unknown', 'body_status': 'fetched',
                 'body_text': '公司简介 ' * 50,
                 'read_metadata': {'reader': 'http', 'unresolved_downloads': 9}}
    run['sources'].append(unrelated)
    service.fetch_attachments(run, unrelated)
    assert calls == []
