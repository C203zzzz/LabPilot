"""读到一半被停下的附件，必须留在库里。

``fetch_attachments`` 有三条出口，改前只有两条会落库：

* 抓到了（或复用了已抓到的）—— 循环末尾 ``self.persist(run)``；
* 抓取失败（``body_status != 'fetched'``）—— 同样走到循环末尾；
* **读取被 ``ResearchStopped`` 打断** —— 只往内存里的 ``run['gaps']`` 追加一句就
  ``raise``，从不落库。

第三条出口正是「额度用尽」和「抓取途中异常」共同经过的那条，于是这两种原因在
payload 里退化成同一个样子：什么都没有。事后既不能说"这条附件没抓"，也不能说
"抓了但失败"，覆盖率与漏抓率都无从复算。

这个文件钉住修复后的口径：**停下来之前先落一次库**，并且判据仍然是存储的那一份，
而不是调用者手里那个内存对象。
"""
import pytest

from backend.services.procurement_research import ResearchStopped
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture

NOTICE_URL = 'https://www.example-zb.com/notice/award-20260918'
# 相对 href：绝对地址不在正文里，这正是发布方的普通写法。
NOTICE_BODY = (
    '某某招标代理有限公司\n'
    '某某市第一人民医院医疗设备采购项目中标（成交）结果公告\n'
    '项目编号：XYZB-2026-0417\n'
    '采购人：某某市第一人民医院\n'
    '成交供应商：某某医疗器械有限公司\n'
    '中标金额：人民币 236,000.00 元\n'
    '附件信息：\n'
    '[报价明细](files/detail-0417.xlsx)\n'
)


def notice_source():
    return {'url': NOTICE_URL, 'source_type': 'tender_document', 'body_status': 'fetched',
            'body_text': NOTICE_BODY, 'purpose': 'tender'}


def test_an_interrupted_attachment_read_is_stored_as_a_gap(research, monkeypatch):  # noqa: F811
    service, run, _provider, _ = research
    notice = notice_source()
    run['sources'] = [s for s in run['sources'] if s['url'] != NOTICE_URL] + [notice]
    service.persist(run)

    # 附件预取不参与这件事：把网络那一段拿掉，直接让串行读取遇额度停止。
    monkeypatch.setattr(service, 'prefetch_bodies', lambda *args, **kwargs: {})

    def stop(*_args, **_kwargs):
        raise ResearchStopped('本轮调用额度已用完，可保留结果并按需继续。')

    monkeypatch.setattr(service, '_fetch_source', stop)

    with pytest.raises(ResearchStopped):
        service.fetch_attachments(run, notice)

    # 判据读的是存储的那一份，不是内存里的 run：改前它没有这条 gap。
    stored = service.get(run['task_id'], run['id'])
    assert any('附件读取未完成' in gap for gap in stored['gaps'])
    assert stored['unresolved_source_errors'] is True
    # 线索本身也在库里：URL 是「这条附件欠着没读」的唯一身份。
    assert any(NOTICE_URL.replace('/notice/award-20260918', '/notice/files/detail-0417.xlsx') in gap
               or 'detail-0417.xlsx' in gap for gap in stored['gaps'])


def test_a_lease_stop_during_the_write_does_not_replace_the_stop(research, monkeypatch):  # noqa: F811
    """留痕自己也可能被同一个停止条件挡住，那时抛出的仍应是原来的那一个。"""
    service, run, _provider, _ = research
    notice = notice_source()
    run['sources'] = [s for s in run['sources'] if s['url'] != NOTICE_URL] + [notice]
    service.persist(run)

    monkeypatch.setattr(service, 'prefetch_bodies', lambda *args, **kwargs: {})
    monkeypatch.setattr(service, '_fetch_source',
                        lambda *a, **k: (_ for _ in ()).throw(ResearchStopped('本轮调用额度已用完')))

    def lease_lost(_run):
        raise ResearchStopped('执行租约已失效，停止后续调用')

    monkeypatch.setattr(service, 'persist', lease_lost)

    with pytest.raises(ResearchStopped, match='额度已用完'):
        service.fetch_attachments(run, notice)


def test_a_completed_read_still_stores_its_attachment(research, monkeypatch):  # noqa: F811
    """修补不得改变成功路径：读到的附件照样落库，还带着父页面的身份。"""
    service, run, _provider, _ = research
    notice = notice_source()
    run['sources'] = [s for s in run['sources'] if s['url'] != NOTICE_URL] + [notice]
    service.persist(run)

    monkeypatch.setattr(service, 'prefetch_bodies', lambda *args, **kwargs: {})
    attachment = {'url': NOTICE_URL.replace('award-20260918', 'files/detail-0417.xlsx'),
                  'body_status': 'fetched', 'body_text': '生物安全柜 | 甲厂Q10 | 2 | 74,800',
                  'parent_url': NOTICE_URL}
    monkeypatch.setattr(service, '_fetch_source', lambda *a, **k: dict(attachment))

    service.fetch_attachments(run, notice)

    stored = service.get(run['task_id'], run['id'])
    saved = next(s for s in stored['sources'] if s['url'] == attachment['url'])
    assert saved['body_status'] == 'fetched'
    assert saved['parent_url'] == NOTICE_URL
    assert saved['source_type'] == 'tender_document'
