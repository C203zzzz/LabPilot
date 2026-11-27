"""Step three of the chain: the notice's *attachments* must actually land.

The product promise is that a configuration or unit price published in a
notice's attachment is obtained, and that "未提供" is only said when the notice
*and its attachments* never stated it. Three gaps made that false:

1. ``报价明细附件.zip`` was never discovered at all -- ``.zip`` was not a
   document suffix, so the publisher's own file list entry was dropped before
   anything could fetch it (measured on a real notice: see
   ``test_a_real_notice_publishes_its_line_items_as_a_zip``);
2. a ``.zip`` body was refused by the decoder (``不是可读取的Word或Excel附件``),
   so even a discovered archive read as "unreadable" instead of its contents;
3. a *notice* was grounded on its own body plus its linked page, and nothing
   links a notice to the files it published, so an attachment's text never
   reached the notice's own extraction.

The archive tests use SYNTHETIC fixtures built in this file; the notice text in
the first test is the real published attachment list.
"""
import io
import json
import zipfile
from datetime import datetime, timezone

import pytest

from backend.infrastructure.search_providers import (
    ARCHIVE_ENTRY_BYTES,
    ARCHIVE_ENTRY_LIMIT,
    PublicWebSearchProvider,
)
from backend.repositories.sqlite import SQLiteRepository
from backend.services.official_search import OfficialSearchService
from backend.services.procurement_research import RESEARCH_LIMITS, ProcurementResearch
from backend.services.procurement_tables import price_table_rows
from backend.services.research_sources import attached_documents, attachments, is_document_link

#: The real attachment list of 中国疾病预防控制中心地方病控制中心's 中标（成交）结果公告
#: (ggzyjyw.hlj.gov.cn, 2025-11-21), fetched live by the earlier attachment task.
#: The first line is the file whose line items the notice body does not state.
REAL_ATTACHMENT_LIST = (
    '相关附件：\n'
    '开标记录表.zip [https://hljcg.hlj.gov.cn/gpx-bid-file/ZF_JGBM_000002/230001/2025/11/11/'
    '2c9080129a29a243019a33f3b9191cfa/gpx-bidconfirm/zip/'
    '2c90848b9a32eb2c019aa4e3ce3673dc20251121132944.zip?accessCode=1035419971dfd828d2c4e1883d90ebab]\n'
    '地病中心2025新楼设备报价明细附件.pdf [https://hljcg.hlj.gov.cn/gpx-bid-file/ZF_JGBM_000002/230001/2025/11/11/'
    '2c9080129a29a243019a33f3b9191cfa/gpx-bidconfirm/'
    '2c908ab79a32eb3f019aa4e3d2ee2d5e.pdf?accessCode=f939cf8519fb908bd68cccdde59e229c]\n'
)
REAL_NOTICE_URL = ('https://ggzyjyw.hlj.gov.cn/jyfwdt/003002/003002002/003002002004/20251121/'
                   '2c90835b9a71baac019aa4e3d4b3224d.html')
REAL_ZIP_URL = ('https://hljcg.hlj.gov.cn/gpx-bid-file/ZF_JGBM_000002/230001/2025/11/11/'
                '2c9080129a29a243019a33f3b9191cfa/gpx-bidconfirm/zip/'
                '2c90848b9a32eb2c019aa4e3ce3673dc20251121132944.zip?accessCode=1035419971dfd828d2c4e1883d90ebab')

DETAIL_HEADER = ['货物名称', '规格型号', '品牌', '数量', '单价（元）', '总价（元）']
DETAIL_ROW = ['高速冷冻离心机（生物安全型）', 'H1750R', '湘仪', 2, '43,300.00', '86,600.00']


def zip_bytes(members):
    """A synthetic archive; no publisher ever served these bytes."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def xlsx_bytes(rows):
    import openpyxl
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = '报价明细'
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def decode(payload, content_type='application/zip', url='https://files.example.gov.cn/a.zip'):
    return PublicWebSearchProvider.decode_body_with_report(payload, content_type, url)[0]


# --------------------------------------------------------------------------
# 1. Discovery: a publisher's own .zip is a document, without losing the pdf.
# --------------------------------------------------------------------------

def test_a_real_notice_publishes_its_line_items_as_a_zip():
    source = {'url': REAL_NOTICE_URL, 'source_type': 'tender_document',
              'body_text': '中标（成交）结果公告\n' + REAL_ATTACHMENT_LIST}
    found = attachments(source, model='H1750R')

    assert REAL_ZIP_URL in found
    assert any(url.endswith('.pdf?accessCode=f939cf8519fb908bd68cccdde59e229c') for url in found)
    # The zip is evidence of the publisher's file list; nothing more is needed.
    assert is_document_link(REAL_ZIP_URL, '开标记录表.zip', formal=True)


def test_an_untrusted_page_still_has_to_name_the_archive():
    """Any product page links zips; only a publication's file list is evidence."""
    assert is_document_link('https://h.example.com/files/a.zip', '', formal=False) is False
    assert is_document_link('https://h.example.com/files/a.zip', '报价明细', formal=False) is True
    assert is_document_link('https://h.example.com/files/a.zip', '驱动下载', formal=False) is False
    assert is_document_link('https://h.example.com/files/a.csv', '', formal=False) is False


# --------------------------------------------------------------------------
# 2. Unpacking: an archive of line items becomes a body the readers can use.
# --------------------------------------------------------------------------

def test_an_archive_of_line_items_becomes_a_readable_body():
    payload = zip_bytes({
        '报价明细/明细表.xlsx': xlsx_bytes([DETAIL_HEADER, DETAIL_ROW]),
        '报价明细/分项报价.csv': '货物名称,规格型号,数量,单价（元）\n台式微量离心机,HT165,2,9000\n',
    })

    text = decode(payload)

    # Each member is labelled, so a citation can say which file it came from.
    assert '【附件包内文件】报价明细/明细表.xlsx' in text
    assert '【附件包内文件】报价明细/分项报价.csv' in text
    rows = price_table_rows(text, 'H1750R')
    assert [(row['model'], row['amount'], row['quantity'], row['brand']) for row in rows] == [
        ('H1750R', 43300.0, 2.0, '湘仪')]
    # A comma-separated member is normalised to the pipe rows the readers expect.
    assert [row['amount'] for row in price_table_rows(text, 'HT165')] == [9000.0]


def test_an_archive_with_nothing_readable_says_so_instead_of_returning_empty():
    with pytest.raises(ValueError) as failure:
        decode(zip_bytes({'setup.exe': b'MZ\x90\x00binary'}))
    assert '没有可读取的明细文件' in str(failure.value)
    assert 'setup.exe' in str(failure.value)

    with pytest.raises(ValueError) as legacy:
        decode(zip_bytes({'明细.xls': b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1legacy'}))
    assert '旧版Word/Excel' in str(legacy.value)


# --------------------------------------------------------------------------
# 3. Safety: a bomb, an over-long member, an over-full archive, a traversal.
# Every one of these refuses the whole archive rather than truncating it.
# --------------------------------------------------------------------------

def test_a_decompression_bomb_is_refused():
    payload = zip_bytes({'明细.csv': b'A' * (ARCHIVE_ENTRY_BYTES + 1024)})
    with pytest.raises(ValueError) as failure:
        decode(payload)
    assert '单个文件解压后过大' in str(failure.value)


def test_a_forged_small_header_does_not_get_past_the_bomb_guard():
    """The cap counts bytes that came out, not the size the header declares."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo('明细.csv')
        archive.writestr(info, b'A' * (ARCHIVE_ENTRY_BYTES + 1024))
    payload = bytearray(buffer.getvalue())
    # Rewrite the local header's declared uncompressed size to 1 KiB.
    payload[22:26] = (1024).to_bytes(4, 'little')
    with pytest.raises(ValueError) as failure:
        decode(bytes(payload))
    assert '单个文件解压后过大' in str(failure.value)


def test_an_archive_with_too_many_entries_is_refused():
    payload = zip_bytes({f'明细{i}.csv': 'a,b\n1,2\n' for i in range(ARCHIVE_ENTRY_LIMIT + 1)})
    with pytest.raises(ValueError) as failure:
        decode(payload)
    assert '超过' in str(failure.value) and '个文件' in str(failure.value)


@pytest.mark.parametrize('name', ['../evil.csv', '..\\evil.csv', '/etc/passwd.csv', 'C:/evil.csv',
                                  '明细/../../evil.csv'])
def test_a_member_that_escapes_the_archive_is_refused(name):
    with pytest.raises(ValueError) as failure:
        decode(zip_bytes({name: 'a,b\n1,2\n', '明细.csv': 'a,b\n1,2\n'}))
    assert '越界路径' in str(failure.value)


def test_a_plain_zip_that_is_not_a_zip_is_refused_not_crashed():
    with pytest.raises(ValueError) as failure:
        decode(b'PK\x03\x04 not actually an archive')
    assert '不是可读取的压缩包' in str(failure.value)


# --------------------------------------------------------------------------
# 4. Grounding: a notice sees the files it published, and only those.
# --------------------------------------------------------------------------

def test_a_notice_reads_the_files_it_itself_published():
    notice = {'url': 'https://ggzy.example.gov.cn/notice/1', 'body_status': 'fetched',
              'source_type': 'tender_document'}
    attachment = {'url': 'https://files.example.gov.cn/1.zip', 'body_status': 'fetched',
                  'parent_url': notice['url'], 'body_text': '明细'}
    unrelated = {'url': 'https://files.example.gov.cn/2.zip', 'body_status': 'fetched',
                 'parent_url': 'https://ggzy.example.gov.cn/notice/2', 'body_text': '别人家的明细'}

    found = attached_documents([notice, attachment, unrelated], notice)

    assert found == [attachment]


def test_a_file_is_never_the_parent_of_the_notice_that_published_it():
    """The reverse edge exists only where a file recorded its notice."""
    notice = {'url': 'https://ggzy.example.gov.cn/notice/1', 'body_status': 'fetched',
              'source_type': 'tender_document'}
    attachment = {'url': 'https://files.example.gov.cn/1.zip', 'body_status': 'fetched',
                  'parent_url': notice['url'], 'body_text': '明细'}
    # An unfetched or empty file lends no text, and a notice never becomes its
    # own attachment.
    assert attached_documents([notice, attachment], attachment) == []
    assert attached_documents([notice, {**attachment, 'body_status': 'failed'}], notice) == []
    assert attached_documents([notice, {**attachment, 'body_text': '  '}], notice) == []


# --------------------------------------------------------------------------
# 4. Cells that repeat the header's unit, and cells that contradict it.
# --------------------------------------------------------------------------

def test_a_price_cell_that_repeats_the_header_unit_is_read():
    """``单价（元）`` + ``43,300.00元`` is an ordinary 报价明细表 shape."""
    body = ('货物名称 | 规格型号 | 品牌 | 数量 | 单价（元） | 总价（元）\n'
            '高速冷冻离心机 | H1750R | 湘仪 | 2 | 43,300.00元 | 86,600.00元\n')

    rows = price_table_rows(body, 'H1750R')

    assert [(row['amount'], row['amount_unit']) for row in rows] == [(43300.0, '元')]
    # The synthesised evidence states the unit once, not twice.
    assert rows[0]['money_excerpt'] == 'H1750R 单价 43300.00元 数量 2'


def test_a_price_cell_unit_that_contradicts_the_header_is_refused():
    """Two conflicting statements must not be resolved by picking one."""
    body = ('货物名称 | 规格型号 | 品牌 | 数量 | 单价（元） | 总价（元）\n'
            '高速冷冻离心机 | H1750R | 湘仪 | 2 | 43,300.00万元 | 86,600.00万元\n')

    assert price_table_rows(body, 'H1750R') == []


# --------------------------------------------------------------------------
# 5. End to end: a line item that exists only in the notice's own .zip lands.
# --------------------------------------------------------------------------

NOTICE_BODY = (
    '某某市第一人民医院医疗设备采购项目中标（成交）结果公告\n'
    '项目编号：XYZB-2026-0417\n'
    '采购人：某某市第一人民医院\n'
    '中标（成交）供应商：某某医疗器械有限公司\n'
    '中标（成交）金额：人民币 236,000.00 元\n'
    '公告日期：2026年09月18日\n'
    '四、主要标的信息\n'
    '品目号 | 品目名称 | 采购标的 | 品牌 | 规格型号 | 数量（单位）\n'
    '1-1 | 其他仪器仪表 | 高速冷冻离心机（生物安全型） | 湘仪 | H1750R | 2.00(台)\n'
    '相关附件：\n'
    '报价明细附件.zip [https://files.example.gov.cn/notice/20260918/detail.zip]\n'
)
NOTICE_URL = 'https://ggzy.example.gov.cn/notice/20260918/award'
ZIP_URL = 'https://files.example.gov.cn/notice/20260918/detail.zip'
CONFIGURATION = '标配24×1.5ml角转子'


class _RecordingModel:
    """Quotes the line it was shown; invents nothing (no API key needed)."""

    def __init__(self):
        self.calls = []

    def analyze(self, system_prompt=None, user_prompt=None, **kwargs):
        data = json.loads(user_prompt)
        self.calls.append(data)
        line = next((l for l in (data.get('body') or '').splitlines()
                     if 'H1750R' in l and '43,300' in l), '')
        if not line:
            return {'prices': []}
        return {'prices': [{
            'brand': '湘仪', 'model': 'H1750R', 'kind': 'award', 'configuration': CONFIGURATION,
            'amount': 43300.0, 'amount_unit': '元', 'amount_scope': 'unit', 'quantity': 2,
            'currency': 'CNY', 'date': '2026-09-18', 'excerpt': line}]}


@pytest.fixture
def landing(tmp_path):
    bodies = {
        NOTICE_URL: (NOTICE_BODY.encode('utf-8'), 'text/html; charset=utf-8'),
        ZIP_URL: (zip_bytes({'报价明细/明细表.xlsx': xlsx_bytes(
            [DETAIL_HEADER + ['配置'], DETAIL_ROW + [CONFIGURATION]])}), 'application/zip'),
    }
    provider = PublicWebSearchProvider(transport=lambda url, timeout: bodies[url])
    search = OfficialSearchService(providers=[provider])
    repo = SQLiteRepository(tmp_path / 'app.db')
    model = _RecordingModel()
    service = ProcurementResearch(repo=repo, search=search, client_factory=lambda: model)
    service.query = lambda *args, **kwargs: None  # no search traffic in this test
    stamp = datetime.now(timezone.utc).isoformat()
    run = {
        'id': 'research-landing', 'task_id': 'task-landing', 'kind': 'research',
        'created_at': stamp, 'updated_at': stamp, 'baseline_id': 'baseline-landing',
        'baseline': {'id': 'baseline-landing', 'version': 1, 'brand': '湘仪', 'model': 'H1750R',
                     'scenario': '容量检测', 'parameters': [], 'currency': 'CNY'},
        'status': 'running', 'stage': '准备检索',
        'usage': {'search': 0, 'fetch': 0, 'model': 0}, 'limits': dict(RESEARCH_LIMITS),
        'sources': [], 'completed_queries': [], 'prices': [], 'republished_prices': [],
        'alternatives': [], 'gaps': [], 'search_strategy_version': 0,
        'lanes': {'price': 'pending', 'alternatives': 'pending'}, 'round': 1, 'model_results': {},
    }
    repo.put('tasks', 'task-landing', {'id': 'task-landing', 'procurement_status': 'running'})
    service.save(run)
    notice = service.fetch_source(run, {'url': NOTICE_URL, 'source_type': 'unknown', 'purpose': 'price'})
    run['sources'].append(notice)
    service.fetch_attachments(run, notice)
    service.save(run)
    return service, service.get('task-landing', 'research-landing'), model


def test_a_zip_attachment_is_fetched_and_becomes_a_priced_source(landing):
    service, run, _ = landing
    archive = next(s for s in run['sources'] if s['url'] == ZIP_URL)

    assert archive['body_status'] == 'fetched'
    assert archive['parent_url'] == NOTICE_URL
    assert 'H1750R' in archive['body_text'] and '43,300.00' in archive['body_text']
    assert price_table_rows(archive['body_text'], 'H1750R')[0]['amount'] == 43300.0

    prices = service.prices(run, run['baseline'])
    assert {price['url'] for price in prices} == {NOTICE_URL, ZIP_URL}
    assert {price['unit_price'] for price in prices} == {43300.0}


def test_a_configuration_that_exists_only_in_the_attachment_is_not_reported_missing(landing):
    service, run, model = landing
    notice, archive = run['sources'][0], run['sources'][1]

    # The premise: the notice body never states the configuration, and the file
    # the notice published is the only place it appears.
    assert CONFIGURATION not in notice['body_text']
    assert CONFIGURATION in archive['body_text']

    prices = service.prices(run, run['baseline'])
    from_notice = next(price for price in prices if price['url'] == NOTICE_URL)

    # Grounding the notice on its own body alone rewrote this to "unknown" and
    # reported "公告正文未提供该项" -- the exact claim the promise forbids.
    assert from_notice['configuration'] == CONFIGURATION
    assert 'configuration' not in from_notice['gaps']
    assert 'configuration' not in from_notice['field_notes']
    # The notice's own attachment text reached the extractor as attachment text.
    assert any(CONFIGURATION in json.dumps(call, ensure_ascii=False) for call in model.calls)


def _csv_archive(body: str, compression=zipfile.ZIP_STORED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', compression) as archive:
        archive.writestr('明细.csv', body)
    return buf.getvalue()


def test_a_corrupt_member_is_refused_with_a_reason_not_a_raw_exception():
    """The decoder's contract is ValueError, and a CRC failure is not one.

    A truncated fetch leaves a readable central directory whose member data no
    longer matches its CRC, which surfaces as BadZipFile. The caller above
    catches only ValueError, so without this it would report an unexplained
    failure instead of an unreadable attachment -- and skip the structured-PDF
    fallback on the way out.
    """
    body = '序号 | 品目 | 单价（元）\n1 | 生物安全柜 | 43300.00\n'
    payload = bytearray(_csv_archive(body))
    at = payload.index(body.encode('utf-8')[:12])
    payload[at] ^= 0x01                        # stored data no longer matches its CRC
    with pytest.raises(ValueError) as raised:
        PublicWebSearchProvider.decode_body_with_report(
            bytes(payload), 'application/zip', 'https://x.gov.cn/a.zip')
    assert '损坏' in str(raised.value) or '不是可读取' in str(raised.value)


def test_an_over_long_csv_field_is_refused_with_a_reason():
    """csv's default field cap is 128 KiB and csv.Error is not a ValueError."""
    payload = _csv_archive('A' * 200_000)      # one field, no line break anywhere
    with pytest.raises(ValueError) as raised:
        PublicWebSearchProvider.decode_body_with_report(
            payload, 'application/zip', 'https://x.gov.cn/a.zip')
    assert '损坏' in str(raised.value) or '格式' in str(raised.value)
