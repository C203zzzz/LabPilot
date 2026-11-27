import pytest
from urllib.error import HTTPError
from backend.infrastructure.page_reader import PublicPageReader, PageResult, quality
from backend.services.research_sources import public_url, network_url


def test_port_is_ordinary_syntax_while_credentials_and_private_hosts_stay_refused(monkeypatch):
    """端口不再被误判成凭据；凭据与私网这两条边界一条都没放松。

    旧版把 ``urlsplit(...).port`` 和登录凭据放在同一个判断里，于是任何把服务开在
    非标准端口上的公开站点（以及链接到它的页面）都被当成"带登录凭据"拒绝。实测
    广东教育部门政府采购平台的列表页就链到它自己的 ``:8090`` / ``:8093``，
    读这种 href 直接抛 ``ValueError``（设计规范 记为产品缺陷）。
    """
    for url in ['https://ecaigou.swu.edu.cn:30910/bid/detail?id=10559',
                'https://gdegp.gds.edu.cn:8093/x',
                'https://gdegp.gds.edu.cn:8090/Main/Login2.aspx',
                'http://example.com:8080/a',
                # ``@`` 出现在查询串里不是凭据，只有出现在 authority 里才是。
                'https://example.com:8080/a?next=user@example.com']:
        assert public_url(url) == url

    for url in ['https://user:pw@example.com/a',
                'https://user@example.com/a',
                'https://user:pw@example.com:30910/a',
                'ftp://example.com:30910/a',
                'https://:30910/a',
                # 畸形端口：``urlsplit`` 自己在这里抛，调用方照旧当成"不是公开网址"。
                'https://example.com:not-a-port/a',
                'http://127.0.0.1:30910/a',
                'http://192.168.1.10:30910/a',
                'http://localhost:30910/a',
                'http://printer.local:30910/a']:
        with pytest.raises(ValueError):
            public_url(url)

    # 端口自由不等于能读到私网：真正放行与否由 ``network_url`` 解析主机后定。
    monkeypatch.setattr('socket.getaddrinfo', lambda *args: [(2, 1, 6, '', ('127.0.0.1', 0))])
    for url in ['https://ecaigou.swu.edu.cn:30910/a', 'https://gdegp.gds.edu.cn:8093/x']:
        with pytest.raises(ValueError):
            network_url(url)


def test_not_found_is_not_retried_but_http_failure_can_render(monkeypatch):
    reader = PublicPageReader(browser=True)
    counts = []
    monkeypatch.setattr(reader, 'read_http', lambda *args: (_ for _ in ()).throw(HTTPError('https://example.org', 404, 'missing', {}, None)))
    monkeypatch.setattr(reader, 'read_browser', lambda *args: PageResult(b'html', 'text/html', 'body'))
    with pytest.raises(HTTPError):
        reader.read('https://example.org', on_attempt=lambda: counts.append(1))
    assert len(counts) == 1
    monkeypatch.setattr(reader, 'read_http', lambda *args: (_ for _ in ()).throw(TimeoutError()))
    assert reader.read('https://example.org', on_attempt=lambda: counts.append(1)).metadata['http_failure'] == 'TimeoutError'
    assert len(counts) == 3


def test_challenge_is_not_success_even_with_http_200():
    assert quality('请完成人机验证' + ' 页面载入' * 80) == 'verification_required'
    assert quality('<div></div>') == 'empty_shell'
    assert quality('404 Not Found') == 'not_found'
    assert quality('请登录后查看') == 'login_required'


def test_browser_waits_for_delayed_table_and_preserves_model_column(monkeypatch):
    from types import SimpleNamespace
    from contextlib import nullcontext
    from backend.infrastructure.page_reader import PageReadError
    html = '<h1>产品参数</h1><p>' + '产品说明 ' * 30 + '</p><table><tr><th>型号</th><th>Q10</th><th>Q20</th></tr><tr><td>容积(L)</td><td>150</td><td>250</td></tr></table>'
    class Page:
        url = 'https://vendor.example/product'
        tick = 0
        def goto(self, *args, **kwargs): return SimpleNamespace(status=200)
        def content(self): return '<p>加载中</p>' if self.tick < 4 else html
        def wait_for_timeout(self, delay): self.tick += 1
    page = Page()
    context = SimpleNamespace(route=lambda *a: None, new_page=lambda: page)
    browser = SimpleNamespace(new_context=lambda **k: context, close=lambda: None)
    runtime = SimpleNamespace(chromium=SimpleNamespace(launch=lambda **k: browser))
    monkeypatch.setattr('playwright.sync_api.sync_playwright', lambda: nullcontext(runtime))
    monkeypatch.setattr('backend.infrastructure.page_reader.network_url', lambda url: url)
    result = PublicPageReader(browser=True).read_browser(page.url)
    assert 'Q10 容积(L) 150' in result.text and 'Q20 容积(L) 250' in result.text
    assert b'<table>' in result.payload and result.metadata['snapshot_kind'] == 'rendered_dom'
    page.content = lambda: '<p>请登录后查看</p>'
    with pytest.raises(PageReadError, match='login_required'):
        PublicPageReader(browser=True).read_browser(page.url)
