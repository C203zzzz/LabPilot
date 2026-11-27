# -*- coding: utf-8 -*-
"""CCGP 的附件下载主机拒绝「空 Referer」的请求：抓取层把公告页带成 Referer。

公告页里的附件锚点是 ``href=''`` + ``id=<uuid>``，真实地址
（``http://download.ccgp.gov.cn/oss/download?uuid=<id>``）由页面脚本拼出来，
所以本项目的正文读取是从**搜索结果**直接跳到附件，而不是从公告页点进去。
``设计规范`` 里那 7 条 403 就落在这种请求上。

实测（同一批 URL、同一 UA、间隔 3s、不重试）：

* 不带 Referer：同一 URL 一次 403（``Server: nginx``）、下一次 200；
* 带 ``http://www.ccgp.gov.cn/``：200（``Server: openresty``，带真实
  ``Content-Disposition``）；
* 带真实公告页地址：同样 200。

本模块钉住的是抓取层的那一步：附件记录里的 ``parent_url`` 必须变成请求的
Referer，未知父页时回落成站点自己的公开首页；其它主机的请求头一个字节不变。
"""
import pytest

from backend.infrastructure.page_reader import PageResult, PublicPageReader
from backend.infrastructure.search_providers import (CCGP_DOWNLOAD_REFERER,
                                                      PublicWebSearchProvider)
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture

ATTACHMENT_URL = 'http://download.ccgp.gov.cn/oss/download?uuid=BB888F8D0A8BEC9FC787F24187448E'
NOTICE_URL = 'http://www.ccgp.gov.cn/cggg/zygg/zbgg/202608/t20260827_27219263.htm'
DETAIL_BODY = ('货物名称 | 规格型号 | 数量 | 单价（元）\n'
               '生物安全柜 | 甲厂Q10 | 2 | 74,800\n')
LONG_HTML = ('<html><body><p>' + '公告正文占位，用于通过读者层的空壳判据。' * 8 + '</p></body></html>')


def test_the_download_host_gets_a_referer_even_when_no_parent_is_known():
    headers = PublicWebSearchProvider()._request_headers(ATTACHMENT_URL)
    assert headers['Referer'] == CCGP_DOWNLOAD_REFERER


def test_the_notice_page_wins_over_the_fallback():
    provider = PublicWebSearchProvider().for_referer(NOTICE_URL)
    assert provider._request_headers(ATTACHMENT_URL)['Referer'] == NOTICE_URL


def test_reads_that_have_no_parent_keep_their_old_headers():
    provider = PublicWebSearchProvider()
    assert provider.for_referer(None) is provider
    assert provider.for_referer('   ') is provider
    for url in ('https://www.ccgp.gov.cn/cggg/zygg/zbgg/202608/t20260827_27219263.htm',
                'https://example.edu.cn/award'):
        assert 'Referer' not in PublicWebSearchProvider()._request_headers(url)


def test_read_http_sends_the_referer_it_was_given():
    reader = PublicPageReader(browser=False)
    seen = []

    class Recorder(PublicWebSearchProvider):
        def fetch_raw(self, url, timeout):
            seen.append(self._request_headers(url).get('Referer'))
            return LONG_HTML.encode('utf-8'), 'text/html; charset=utf-8'

    reader.http = Recorder()
    reader.read_http(ATTACHMENT_URL, 5, referer=NOTICE_URL)
    assert seen == [NOTICE_URL]
    reader.read_http(ATTACHMENT_URL, 5)
    assert seen[-1] == CCGP_DOWNLOAD_REFERER      # 没有父页时仍是同站公开首页


class RecordingReader:
    """正文读取的最小替身，记下每次调用带的 referer。"""

    def __init__(self, body=DETAIL_BODY):
        self.calls = []
        self.body = body

    def read(self, url, timeout=15, on_attempt=None, referer=None):
        self.calls.append((url, referer))
        if on_attempt:
            on_attempt()
        return PageResult(LONG_HTML.encode('utf-8'), 'text/html; charset=utf-8',
                          self.body, {'reader': 'http', 'snapshot_kind': 'response_bytes'})


def test_fetch_source_hands_the_parent_page_to_the_reader(research):  # noqa: F811
    service, run, _, _ = research
    reader = RecordingReader()
    service.search.reader = reader

    source = service.fetch_source(run, {'url': ATTACHMENT_URL, 'source_type': 'unknown',
                                        'parent_url': NOTICE_URL, 'purpose': 'price'})

    assert source['body_status'] == 'fetched'
    assert reader.calls == [(ATTACHMENT_URL, NOTICE_URL)]


def test_source_without_a_parent_still_reads_with_no_referer(research):  # noqa: F811
    service, run, _, _ = research
    reader = RecordingReader()
    service.search.reader = reader

    service.fetch_source(run, {'url': 'https://example-zb.com/notice/award', 'source_type': 'unknown'})

    assert reader.calls == [('https://example-zb.com/notice/award', None)]


def test_prefetching_an_attachment_carries_the_parent_page_too(research):  # noqa: F811
    """并行预读走的是另一条分支，同样不能丢掉父页。"""
    service, run, _, _ = research
    reader = RecordingReader()
    service.search.reader = reader

    service.prefetch_bodies(run, [{'url': ATTACHMENT_URL, 'source_type': 'unknown',
                                   'parent_url': NOTICE_URL, 'purpose': 'price'}])

    assert reader.calls == [(ATTACHMENT_URL, NOTICE_URL)]
