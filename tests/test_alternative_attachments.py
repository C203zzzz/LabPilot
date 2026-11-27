# -*- coding: utf-8 -*-
"""跨品牌候选的参数核验要走招标文件附件，但只能走到「待核验」为止。

``alternatives()`` 给每个候选建一张参数矩阵。它的参数来源原先只有厂家官网／
下载与身份未核实的网页：招标文件的附件——**公开材料里唯一写着别的品牌型号的
那一类**——既进不了给模型看的正文，也过不了 ``grounded_parameter`` 的来源判据
（``tender_document`` 原先是两个条件都不满足）。于是附件里的型号清单只当发现
线索用完就丢，候选矩阵永远停在 unknown。

这里钉住打通之后的边界：附件要真的取到正文、要真的出现这个候选型号，落下来的
行只能是 ``provisional``；附件里没有这个型号时一个字都不许借。
"""
from pathlib import Path

from backend.services.procurement_research import ON_DEMAND_ENV
from backend.services.procurement_rules import parameter_row
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture

FIXTURES = Path(__file__).parent / 'fixtures' / 'attachment-parameters'

#: 真实公告页：宁夏大学设备采购，附件挂在 download.jsp 上（hot-run.json 里那一条）。
NXU_NOTICE = 'https://www.nxu.edu.cn/notice/2026/0901.htm'
NXU_FILE = 'https://www.nxu.edu.cn/system/_content/download.jsp?urltype=news.DownloadAttachUrl&owner=1001&wbfileid=12345'
#: 真实公告页：内蒙古 2025 年实验室设备采购清单（cold-run.json 里那一条）。
NMG_NOTICE = 'https://www.ccgp-neimenggu.gov.cn/notice/2025/0606.htm'
NMG_FILE = 'https://www.ccgp-neimenggu.gov.cn/gpx-bid-file/ZF_JGBM_000001/150001/2025/6/6/40.pdf'

BASELINE = {
    'brand': '甲厂', 'model': 'DW-86L578S', 'category': '超低温冰箱', 'scenario': '低温保存',
    'parameters': [{'name': '容积', 'value': 100, 'unit': 'L', 'direction': 'min'}],
}
BROCHURE = {
    'url': 'https://acme-lab.com/DW-86L578S', 'purpose': 'official', 'body_status': 'fetched',
    'source_type': 'manufacturer_website',
    'body_text': '甲厂 DW-86L578S 超低温冰箱 技术参数 容量 580 L',
}


def notice(url, title, file_url):
    return {'url': url, 'purpose': 'discovery', 'body_status': 'fetched',
            'source_type': 'tender_document', 'title': title,
            'body_text': title + '\n采购人：某大学\n附件信息：\n[设备清单](' + file_url + ')\n'}


def attachment(parent, url, body):
    return {'url': url, 'purpose': 'discovery', 'body_status': 'fetched',
            'source_type': 'tender_document', 'parent_url': parent, 'body_text': body}


def fixture(name):
    return (FIXTURES / name).read_text(encoding='utf-8')


def run_alternatives(service, run, candidates, parameters_of, monkeypatch):
    """走一遍 ``alternatives()``：发现模型只回给定候选，核对模型只回给定参数。

    两个 prompt 都提到「其他仪器」（核对那段的末句就是「不用其他仪器或配件
    参数」），所以按开头分派，不按关键词出现与否。

    参数矩阵是 ``verify_and_price_candidate`` 建的，只在**批量查价**那条路上跑
    （开关打开时 ``alternatives()`` 只列候选、不核验）。所以这里显式把开关关掉：
    本文件钉的是核验这一段的行为，开关默认值由
    ``test_alternative_price_on_demand.py`` 钉。
    """
    monkeypatch.setenv(ON_DEMAND_ENV, '0')
    calls = []

    def fake_model(run_, prompt, data):
        if prompt.startswith('从给定正文发现'):
            return {'candidates': [dict(c) for c in candidates]}
        calls.append(data)
        return {'parameters': parameters_of(data)}

    monkeypatch.setattr(service, 'model', fake_model)
    monkeypatch.setattr(service, 'query', lambda *args, **kwargs: None)
    monkeypatch.setattr(service, 'verify_publishers', lambda *args, **kwargs: None)
    monkeypatch.setattr(service, 'alternative_prices', lambda *args, **kwargs: {})
    monkeypatch.setattr(service, 'persist', lambda *args, **kwargs: None)
    service.alternatives(run)
    return calls


def prepare(run, sources, baseline=BASELINE):
    run.update({'baseline': baseline, 'sources': list(sources),
                'alternatives': [], 'discovered_candidates': [], 'reference_candidates': [],
                'prices': [], 'gaps': []})


def test_a_competitor_named_in_a_tender_attachment_yields_provisional_parameters(research, monkeypatch):  # noqa: F811
    """附件正文里有这个候选型号与它的参数：行取到值，且只能是 provisional。

    构造的是真实形状：正文按行写「序号 仪器名称 品牌 规格型号 … 技术参数」，
    中科都菱 MDF-86v58 那一行同时带着型号与「有效容积 520 L」。这份附件是唯一
    写着这个型号的来源，所以行里能有值，就说明附件确实进了参数矩阵。
    """
    service, run, _, _ = research
    body = fixture('nxu-competitor-list.txt')
    attached = attachment(NXU_NOTICE, NXU_FILE, body)
    prepare(run, [BROCHURE, notice(NXU_NOTICE, '实验室设备采购项目中标（成交）结果公告', NXU_FILE), attached])
    line = next(l for l in body.splitlines() if 'MDF-86v58' in l)
    # ``configuration``：2026-10-06 起"找不到配置的候选不展示"，空配置的候选会在
    # ``alternatives()`` 收尾时被撤下候选卡片（``tests/test_m4_pipeline.py``）；
    # 这里测的是核验那一段拿到的参数，所以给一段来源正文里逐字存在的配置。
    calls = run_alternatives(
        service, run,
        [{'brand': '中科都菱', 'model': 'MDF-86v58', 'url': NXU_FILE, 'excerpt': line,
          'configuration': line}],
        lambda data: [{'name': '容积', 'value': 520, 'unit': 'L', 'url': NXU_FILE, 'excerpt': line}],
        monkeypatch)

    assert len(calls) == 1
    assert NXU_FILE in {s['url'] for s in calls[0]['sources']}
    candidate = run['alternatives'][0]
    row = candidate['matrix'][0]
    assert row['status'] == 'satisfied' and row['observed']['value'] == 520
    assert row['evidence_status'] == 'provisional'
    assert row['observed']['evidence_status'] == 'provisional'
    assert row['observed']['evidence']['url'] == NXU_FILE
    assert candidate['status'] == 'needs_review'
    assert candidate['provisional_parameter_count'] == 1


def test_an_attachment_that_never_names_the_candidate_is_not_a_source(research, monkeypatch):  # noqa: F811
    """反向：附件正文里没有这个型号时，不许借它的设备清单凑证据。

    候选只在厂家官网上出现过；同一份附件里写着别的型号与它们的参数。给模型的
    正文里不能有这份附件，参数行仍旧停在 unknown。
    """
    service, run, _, _ = research
    body = fixture('nxu-competitor-list.txt')
    attached = attachment(NXU_NOTICE, NXU_FILE, body)
    mention = '乙厂 SX-500 超声波清洗机 容积 20 L'
    brochure = {**BROCHURE, 'body_text': mention}
    baseline = {**BASELINE, 'parameters': [
        {'name': '容积', 'value': 10, 'unit': 'L', 'direction': 'min'},
    ]}
    prepare(run, [brochure, notice(NXU_NOTICE, '实验室设备采购项目中标（成交）结果公告', NXU_FILE), attached],
            baseline=baseline)
    calls = run_alternatives(
        service, run,
        [{'brand': '乙厂', 'model': 'SX-500', 'url': brochure['url'], 'excerpt': mention,
          'configuration': mention}],
        lambda data: [], monkeypatch)

    assert len(calls) == 1
    assert [s['url'] for s in calls[0]['sources']] == [brochure['url']]
    row = run['alternatives'][0]['matrix'][0]
    assert row['observed'] is None and row['status'] == 'unknown'
    assert row['evidence_status'] == 'unparsed'
    assert not any('SX-500' in gap and '附件' in gap for gap in run['gaps'])


def test_a_competitor_only_in_a_clause_row_keeps_its_own_parameters(research, monkeypatch):  # noqa: F811
    """同一份清单里另一行的参数不能挪过来，逐字引文对不上就不成立。

    内蒙古那份清单里 DHG-9030T 与 PHSJ-3F 各占一行；引文取自后者的行时，
    前者不能凭这份附件拿到值。
    """
    service, run, _, _ = research
    body = fixture('neimenggu-competitor-list.txt')
    attached = attachment(NMG_NOTICE, NMG_FILE, body)
    baseline = {**BASELINE, 'parameters': [
        {'name': '容积', 'value': 10, 'unit': 'L', 'direction': 'min'},
    ]}
    prepare(run, [BROCHURE, notice(NMG_NOTICE, '实验室仪器设备采购项目中标结果公告', NMG_FILE), attached],
            baseline=baseline)
    named = next(l for l in body.splitlines() if 'DHG-9030T' in l)
    other = next(l for l in body.splitlines() if 'PHSJ-3F' in l)
    run_alternatives(
        service, run,
        [{'brand': '泰斯特', 'model': 'DHG-9030T', 'url': NMG_FILE, 'excerpt': named,
          'configuration': named}],
        lambda data: [{'name': '容积', 'value': 30, 'unit': 'L', 'url': NMG_FILE, 'excerpt': other}],
        monkeypatch)

    row = run['alternatives'][0]['matrix'][0]
    assert row['observed'] is None and row['status'] == 'unknown'
    assert row['evidence_status'] == 'unparsed'


def test_a_tender_body_without_a_parent_is_not_an_attachment():
    """招标文件正文本身（没有 parent_url）没有这条通道，附件才有。"""
    req = {'name': '容积', 'value': 100, 'unit': 'L', 'direction': 'min'}
    text = '中科都菱 MDF-86v58 有效容积 520 L'
    observation = {'name': '容积', 'value': 520, 'unit': 'L', 'url': 'https://example.gov.cn/award',
                   'excerpt': text}
    body = {'url': 'https://example.gov.cn/award', 'body_status': 'fetched',
            'source_type': 'tender_document', 'purpose': 'tender', 'body_text': text}
    assert parameter_row(req, [observation], [body], 'MDF-86v58')['observed'] is None
    attached = {**body, 'parent_url': 'https://example.gov.cn/notice'}
    row = parameter_row(req, [observation], [attached], 'MDF-86v58')
    assert row['evidence_status'] == 'provisional'
    assert row['observed']['evidence_status'] == 'provisional'
