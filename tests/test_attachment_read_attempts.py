"""附件正文**读失败**也是一种记账状态：次数有上限，用完就不再自动重发。

``fetch_attachments`` 的窗口过滤原来只看 ``body_status == 'fetched'``，所以读失败
（``failed``／``unavailable``：403、空正文、格式不支持）的附件在任何后续调用点都会
再发一次请求，永远如此。公告进入研究的时机不止一个（正文到手那一刻、已完成查询的
重入、补抓），同一条永久失效的附件于是被反复请求。

实测 ``research-19c4d6a7926f4e55b0f79cc07bd58117``（``task-e2edocbkp01``，
``usage.fetch`` 48/72）：七条附件全部失败（两条 ``Error``、五条 ``http_403``），
之后的每一轮都被整批重发。

上限是 2 而不是 1，因为「第二次机会」是既有契约：
``tests/test_delivery_fixes.py::test_failed_attachment_remains_recoverable`` 要求
第一次读失败之后、下一次走到这条公告时仍能读回来。本改动收的是「无限重发」，
并让补抓层与这一层**共用同一份计数**，而不是各记各的「我试过了」。
"""
import pytest

from backend.services.procurement_research import (
    ATTACHMENT_READ_ATTEMPT_LIMIT, ResearchStopped,
)
from tests.test_attachment_backfill import (  # noqa: F401 - 复用同一份正文与 fixture
    ATTACHMENT_URL, NOTICE_BODY, NOTICE_URL, notice, put, research,
)


def _reads(provider):
    return len([url for url in provider.reads if url == ATTACHMENT_URL])


def test_the_budget_is_a_count_not_a_flag():
    assert ATTACHMENT_READ_ATTEMPT_LIMIT == 2, '首读 + 一次重试；恢复契约要求第二次可用'


def test_attempts_are_capped_and_counted(research):  # noqa: F811
    service, run, provider, _ = research
    provider.bodies = {NOTICE_URL: NOTICE_BODY}      # 附件永远读不到
    put(run, notice())

    service.fetch_attachments(run, notice())
    assert _reads(provider) == 1
    service.fetch_attachments(run, notice())
    assert _reads(provider) == ATTACHMENT_READ_ATTEMPT_LIMIT, '第二次仍在预算内'
    service.fetch_attachments(run, notice())
    assert _reads(provider) == ATTACHMENT_READ_ATTEMPT_LIMIT, '第三次不再发请求'

    assert run['attachment_read_failures'][ATTACHMENT_URL] == ATTACHMENT_READ_ATTEMPT_LIMIT
    stored = service.get(run['task_id'], run['id'])
    assert stored['attachment_read_failures'][ATTACHMENT_URL] == ATTACHMENT_READ_ATTEMPT_LIMIT
    assert any('不再自动重发' in gap for gap in run['gaps'])


def test_the_second_attempt_still_recovers_the_body(research):  # noqa: F811
    """既有契约：第一次读失败不算终局，下一次走到这条公告时还能读回来。"""
    service, run, provider, _ = research
    provider.bodies = {NOTICE_URL: NOTICE_BODY}
    put(run, notice())
    service.fetch_attachments(run, notice())
    assert next(s for s in run['sources'] if s['url'] == ATTACHMENT_URL)['body_status'] != 'fetched'

    provider.bodies[ATTACHMENT_URL] = '生物安全柜 | Q10 | 2 | 74,800'
    service.fetch_attachments(run, notice())

    attachment = next(s for s in run['sources'] if s['url'] == ATTACHMENT_URL)
    assert attachment['body_status'] == 'fetched'
    assert run['attachment_read_failures'][ATTACHMENT_URL] == 1


def test_the_backfill_layer_and_this_layer_spend_one_budget(research):  # noqa: F811
    """补抓与正文路径共用同一份计数：合起来不超过上限，谁都不会单独多试一次。"""
    service, run, provider, _ = research
    provider.bodies = {NOTICE_URL: NOTICE_BODY}
    put(run, notice())

    assert service.backfill_publication_attachments(run) == [NOTICE_URL]
    assert _reads(provider) == 1
    service.fetch_attachments(run, notice())          # 第二个调用点：用掉第二次
    assert _reads(provider) == ATTACHMENT_READ_ATTEMPT_LIMIT
    service.fetch_attachments(run, notice())          # 预算用尽
    assert _reads(provider) == ATTACHMENT_READ_ATTEMPT_LIMIT


def test_a_read_that_stopped_for_budget_is_still_retryable(research):  # noqa: F811
    """额度用尽不是「读失败」：那时不能记账，额度恢复后仍要再试。"""
    service, run, provider, _ = research
    provider.bodies = {NOTICE_URL: NOTICE_BODY}
    put(run, notice())
    run['usage']['fetch'] = run['limits']['fetch']

    with pytest.raises(ResearchStopped):
        service.fetch_attachments(run, notice())

    assert not run.get('attachment_read_failures')
    run['usage']['fetch'] = 0
    service.fetch_attachments(run, notice())
    assert any(url == ATTACHMENT_URL for url in provider.reads)


def test_a_readable_attachment_is_unaffected(research):  # noqa: F811
    service, run, provider, _ = research
    provider.bodies = {NOTICE_URL: NOTICE_BODY, ATTACHMENT_URL: '生物安全柜 | Q10 | 2 | 74,800'}
    put(run, notice())

    service.fetch_attachments(run, notice())
    reads = len(provider.reads)

    service.fetch_attachments(run, notice())

    assert len(provider.reads) == reads
    assert not run.get('attachment_read_failures')
    assert next(s for s in run['sources'] if s['url'] == ATTACHMENT_URL)['body_status'] == 'fetched'
