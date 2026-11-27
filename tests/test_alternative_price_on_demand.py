# -*- coding: utf-8 -*-
"""按需查价（两段式"先展示候选、按需查价"）的后端行为。

设计清单：``设计说明``。本单只做后端，前端卡片与按钮另派单。

这里钉住四件事：

1. **``candidate_id`` 是稳定的定位键**：同一轮两次生成同一个值、候选在列表里的
   顺序变了也不变、不同型号/不同网址不撞号；``url`` 为空时有确定的退化规则，
   既不撞号也不会是 ``None``；
2. **只查被点的那个候选**：点一个候选只花一个人的量，其它候选的价格字段一个字节
   都不动（不许被顺带查）；
3. **同一候选点两次不重复花钱**：第二次命中已有结果；
4. **``candidate_id`` 不存在时 404、且不产生任何付费调用**（``usage`` 不变）；
5. **开关显式关掉时（``ALTERNATIVE_PRICE_ON_DEMAND=0``）``alternatives()`` 与今天逐字一致**
   ——这是本单的回滚保护。默认值 2026-09-29 已由设计规范改成"开"（按需查价）。
6. **点击必须落在"这一轮真的写完了"之后**：``status`` 写成终态比"收尾写完"来得早，
   收尾（阶段账、报告总述）带的是它开始时读到的那份 run 副本，落在点击之后就整份盖回去
   （2026-10-01 ``sample-a-fix1`` 实测：接口 200 带完整结果、库里没落、第二次点又花钱）。

模型与检索都是离线替身，一条网络请求、一次真模型调用都不发。
"""
import copy
import json
import threading

import pytest

from backend.services.alternative_price_lookup import (
    NOT_ATTEMPTED, NOT_REQUESTED, PRICED, REFUSED, SOURCES_NOT_READ, STATUSES)
from backend.services.procurement_research import (
    ON_DEMAND_DEFAULT, ON_DEMAND_ENV, ProcurementResearch, UnknownCandidate, assign_candidate_ids,
    candidate_key, candidate_fallback_salt, alternative_price_on_demand)
from backend.services.procurement_rules import identity
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture
from tests.test_xlyg_price_channel import _build_db, _item_row

BASELINE = {
    'brand': '甲厂', 'model': 'Q10', 'category': '生物安全柜', 'scenario': '微生物检测',
    'parameters': [{'name': '容量', 'value': 100, 'unit': 'ml', 'direction': 'min'}],
}
BROCHURE = {
    'url': 'https://acme-lab.com/Q10', 'purpose': 'official', 'body_status': 'fetched',
    'source_type': 'manufacturer_website',
    'body_text': '甲厂Q10 生物安全柜 技术参数 容量 100 ml 型号 Q10',
}
#: 公告逐行印字段：型号行上面一行就是那条成交单价（``price_regions`` 的窗口）。
NOTICE_BODY_PRICED = ('某某医院实验室设备采购项目中标（成交）结果公告\n'
                      '采购人：某某医院\n'
                      '生物安全柜 乙厂 Q20 数量1台 单价：56,000.00 元\n')
NOTICE_BODY_NO_AMOUNT = ('某某医院实验室设备采购项目中标（成交）结果公告\n'
                         '采购人：某某医院\n'
                         '生物安全柜 乙厂 Q20 数量1台\n')
NOTICE_URL = 'https://hospital-a.example.com/notice/2026-09-11'
NOTICE_URL_B = 'https://hospital-b.example.com/notice/2026-09-11'
Q20_LINE = '生物安全柜 乙厂 Q20 数量1台'
#: 采信判据要的引文是**含金额的那一整行**（``设计规范`` 那条链一个字没动）：
#: 只写型号行、不写单价时，抽出来的行会因为"正文里没有金额证据/金额解析不了"被拒。
Q20_PRICED_LINE = '生物安全柜 乙厂 Q20 数量1台 单价：56,000.00 元'
Q30_LINE = '生物安全柜 丙厂 Q30 数量1台'
#: ``diverse_candidates`` 一个品牌只给一个核验名额（其余落进参考名单），所以
#: "一轮里真的有两个候选"必须**两个品牌**——这是既有判据，本单没动它。
Q30_LINE_OTHER_BRAND = '生物安全柜 丁厂 Q30 数量1台'
CANDIDATE_EXCERPT = Q20_LINE


def notice(url, body, source_type='tender_document'):
    return {'url': url, 'purpose': 'discovery', 'body_status': 'fetched',
            'source_type': source_type, 'title': '实验室设备采购项目中标（成交）结果公告',
            'body_text': body}


def _q20_candidate(url=NOTICE_URL, body=NOTICE_BODY_PRICED):
    #: ``configuration`` 不能空：2026-10-06 起"找不到配置的候选不展示"
    #: （``tests/test_m4_pipeline.py``），空配置的候选会在 ``alternatives()`` 收尾时被撤下，
    #: 而这些用例测的是按需查价那条路。取正文里逐字存在的型号行，保证过 ``supported``。
    line = next(line for line in body.splitlines() if 'Q20' in line)
    return {'brand': '乙厂', 'model': 'Q20', 'url': url, 'excerpt': line,
            'configuration': line}


def prepare(run, sources):
    """``alternatives()`` 只读它需要的那几个字段；``persist()`` 会读基准版本号，
    所以基准是合并不是替换（与 ``test_alternative_price_lookup._prepare`` 同一条）。"""
    run.update({'baseline': {**(run.get('baseline') or {}), **BASELINE},
                'sources': list(sources), 'alternatives': [],
                'discovered_candidates': [], 'reference_candidates': [],
                'prices': [], 'price_rejections': [], 'price_source_skips': [], 'gaps': []})


def stub(service, monkeypatch, candidates, prices=(), persist=True):
    """模型与检索全换替身；返回"被问过什么"的调用账。"""
    calls = []

    def fake_model(run_, prompt, data):
        calls.append({'prompt': prompt, 'data': data})
        if prompt.startswith('从给定正文发现'):
            return {'candidates': [dict(c) for c in candidates]}
        if '只提取正文中属于指定品牌' in prompt:
            return {'prices': [dict(row) for row in prices]}
        return {'parameters': []}

    monkeypatch.setattr(service, 'model', fake_model)
    monkeypatch.setattr(service, 'query', lambda *args, **kwargs: None)
    monkeypatch.setattr(service, 'verify_publishers', lambda *args, **kwargs: None)
    if not persist:
        monkeypatch.setattr(service, 'persist', lambda *args, **kwargs: None)
    return calls


def price_calls(calls):
    """真正花在抽价那一次模型调用上的次数（按 prompt 认，不按状态名推）。"""
    return [call for call in calls if '只提取正文中属于指定品牌' in call['prompt']]


def usage_of(run):
    return {key: (run.get('usage') or {}).get(key, 0) for key in ('search', 'fetch', 'model')}


def row(model, excerpt, amount=56000):
    return {'brand': '乙厂', 'model': model, 'kind': 'award', 'amount': amount,
            'amount_unit': '元', 'amount_scope': 'unit', 'quantity': 1, 'currency': 'CNY',
            'date': '2026-09-11', 'excerpt': excerpt}


# ---------------------------------------------------------------------------
# 开关本身：默认开（2026-09-29 设计规范），显式关掉仍旧有效
# ---------------------------------------------------------------------------

def test_the_switch_is_on_unless_the_environment_says_otherwise():
    """环境变量**没设**就是开（按需查价）；显式设了按设的那个值算，认不出的值一律当关。"""
    assert alternative_price_on_demand({}) is True
    assert alternative_price_on_demand({ON_DEMAND_ENV: ''}) is False
    assert alternative_price_on_demand({ON_DEMAND_ENV: '0'}) is False
    assert alternative_price_on_demand({ON_DEMAND_ENV: 'off'}) is False
    assert alternative_price_on_demand({ON_DEMAND_ENV: 'maybe'}) is False
    for word in ('1', 'true', 'TRUE', ' yes ', 'On'):
        assert alternative_price_on_demand({ON_DEMAND_ENV: word}) is True
    assert ON_DEMAND_ENV == 'ALTERNATIVE_PRICE_ON_DEMAND'
    # 默认值本身必须是"开"的那几个写法之一；以后把默认改回关时这条会跟着响。
    assert alternative_price_on_demand({ON_DEMAND_ENV: ON_DEMAND_DEFAULT}) is True
    assert NOT_REQUESTED in STATUSES


# ---------------------------------------------------------------------------
# 一、candidate_id 的稳定性
# ---------------------------------------------------------------------------

def test_the_id_is_a_sha256_prefix_of_identity_model_plus_url():
    """取值就是设计清单写的那一个：``sha256(identity(model) + url)[:16]``。"""
    import hashlib
    candidate = {'model': 'Q-20', 'url': 'https://hospital-a.example.com/notice'}
    expected = hashlib.sha256((identity('Q-20') + 'https://hospital-a.example.com/notice')
                              .encode()).hexdigest()[:16]
    assert candidate_key(candidate) == expected
    assert len(expected) == 16
    # ``identity`` 是仓库里现成的那个：去除分隔符之后 Q-20 与 Q20 归一，
    # 但后缀与数字保留（Q200 不并入 Q20）。
    assert candidate_key({'model': 'Q20', 'url': candidate['url']}) == expected
    assert candidate_key({'model': 'Q200', 'url': candidate['url']}) != expected


def test_the_id_never_depends_on_the_position_in_the_list():
    """同一批候选换个顺序（甚至从后往前）算出来的 id 与顺序无关。"""
    batch = [{'model': 'Q20', 'url': 'https://a.example/1'},
             {'model': 'Q30', 'url': 'https://a.example/2'},
             {'model': 'Q40', 'url': 'https://a.example/3'}]
    straight = {c['model']: candidate_key(c) for c in batch}
    for candidate in reversed(batch):
        assert candidate_key(candidate) == straight[candidate['model']]
    shuffled = list(reversed(batch))
    assign_candidate_ids(shuffled)
    assert {c['model']: c['candidate_id'] for c in shuffled} == straight


def test_the_id_of_the_same_candidate_is_the_same_on_two_consecutive_rounds(research, monkeypatch):  # noqa: F811
    """真的跑两遍 ``alternatives()``：同一个候选两次算出的 id 逐字相同。

    第二遍故意把候选在列表里的顺序倒过来——id 必须还是那些。
    """
    service, run, _, _ = research
    candidates = [_q20_candidate(),
                  {'brand': '丙厂', 'model': 'Q30', 'url': NOTICE_URL_B, 'excerpt': Q30_LINE,
                   'configuration': Q30_LINE}]
    sources = [BROCHURE, notice(NOTICE_URL, NOTICE_BODY_PRICED),
               notice(NOTICE_URL_B, '某某医院实验室设备采购项目中标（成交）结果公告\n'
                                    '采购人：某某医院\n' + Q30_LINE + '\n')]
    rounds = []
    for order in (candidates, list(reversed(candidates))):
        prepare(run, sources)
        stub(service, monkeypatch, order, persist=False)
        service.alternatives(run)
        rounds.append({c['model']: c['candidate_id'] for c in run['alternatives']})
    assert len(rounds[0]) == 2, '这一轮得真的有两个候选，否则"顺序无关"没被验到'
    assert rounds[0] == rounds[1]
    assert all(value and value != 'None' for value in rounds[0].values())


def test_two_candidates_never_share_an_id(research, monkeypatch):  # noqa: F811
    """同一轮里两个候选的 id 不相同（不同型号、不同网址都不撞）。"""
    service, run, _, _ = research
    candidates = [{'brand': '乙厂', 'model': 'Q20', 'url': NOTICE_URL, 'excerpt': Q20_LINE,
                   'configuration': Q20_LINE},
                  {'brand': '丁厂', 'model': 'Q30', 'url': NOTICE_URL_B,
                   'excerpt': Q30_LINE_OTHER_BRAND, 'configuration': Q30_LINE_OTHER_BRAND}]
    body_b = ('某某医院实验室设备采购项目中标（成交）结果公告\n'
              '采购人：某某医院\n' + Q30_LINE_OTHER_BRAND + '\n')
    prepare(run, [BROCHURE, notice(NOTICE_URL, NOTICE_BODY_PRICED), notice(NOTICE_URL_B, body_b)])
    stub(service, monkeypatch, candidates, persist=False)

    service.alternatives(run)

    ids = sorted(c['candidate_id'] for c in run['alternatives'])
    assert len(ids) == 2, '这一轮得真的有两个候选，否则"不撞号"没被验到'
    assert len(set(ids)) == 2


def test_an_empty_url_falls_back_deterministically_and_never_collides():
    """``url`` 为空时的退化规则：仍然确定、仍然两两不同、仍然不是 ``None``。

    规则（见 ``candidate_key``）：地址段换成 ``'\\x00no-url:' + 型号``；同型号撞上时
    再用正文引文（``'\\x00dup:' + excerpt``）重算一次。全程不用位置下标——位置一变
    id 就变，那正是这张卡片不能接受的。真实库里 23 个候选**一个空 url 都没有**
    （2026-09-27 复算，见 ``output/ondemand/identity-replay.txt``），这条是兜底。
    """
    blank = {'model': 'Q20', 'url': '', 'excerpt': ''}
    first = candidate_key(blank)
    assert first and first != 'None'
    assert candidate_key({'model': 'Q20', 'url': '   '}) == first, '空白 url 与缺失同一条路'
    assert candidate_fallback_salt(blank) == '\x00dup:'

    # 同一个型号、两个都没有网址的候选：不撞号。
    pair = [{'model': 'Q20', 'url': '', 'excerpt': '第一行 Q20 单价 5,600 元'},
            {'model': 'Q20', 'url': '', 'excerpt': '第二行 Q20 单价 6,100 元'}]
    assign_candidate_ids(pair)
    assert pair[0]['candidate_id'] != pair[1]['candidate_id']
    assert all(c['candidate_id'] and c['candidate_id'] != 'None' for c in pair)

    # 连引文都没有的那种：仍然两两不同，而且再算一次还是同一批值（确定性）。
    bare = [{'model': 'Q20', 'url': ''}, {'model': 'Q20', 'url': ''}]
    assign_candidate_ids(bare)
    assert bare[0]['candidate_id'] != bare[1]['candidate_id']
    again = [{'model': 'Q20', 'url': ''}, {'model': 'Q20', 'url': ''}]
    assign_candidate_ids(again)
    assert [c['candidate_id'] for c in again] == [c['candidate_id'] for c in bare]

    # 有网址的候选不吃退化分支：与"空网址的同型号"不同号。
    assert candidate_key({'model': 'Q20', 'url': 'https://a.example/x'}) != first


def test_assigning_ids_is_idempotent_and_never_overwrites(research):  # noqa: F811
    """已经写过的 id 不再改（跨轮复用同一个候选字典时不会被换号）。"""
    candidate = {'model': 'Q20', 'url': 'https://a.example/x'}
    assign_candidate_ids([candidate])
    kept = candidate['candidate_id']
    candidate['brand'] = '乙厂'
    candidate['prices'] = [{'amount': 1}]
    assign_candidate_ids([candidate])
    assert candidate['candidate_id'] == kept


# ---------------------------------------------------------------------------
# 二、开关关着时：行为与今天逐字一致（回滚保护）
# ---------------------------------------------------------------------------

def test_switch_off_is_the_old_behaviour(research, monkeypatch):  # noqa: F811
    """显式关掉开关（``ALTERNATIVE_PRICE_ON_DEMAND=0``）时 ``alternatives()`` 跑完就有价、
    且没有任何 ``not_requested``。

    这条是本单最重要的回滚保护：``=0`` 之后，用户看到的东西必须与改动前一模一样。
    默认已经从"关"改成"开"，所以这里**显式**关掉它——它钉的是"关得掉"这条排障路径，
    不是默认值（默认值由上面那条开关用例钉）。
    """
    monkeypatch.setenv(ON_DEMAND_ENV, '0')
    assert alternative_price_on_demand() is False, '显式设成 0 必须还是关'
    service, run, _, _ = research
    body = NOTICE_BODY_PRICED
    excerpt = next(line for line in body.splitlines() if 'Q20' in line)
    prepare(run, [BROCHURE, notice(NOTICE_URL, body)])
    calls = stub(service, monkeypatch, [_q20_candidate()],
                 prices=[row('Q20', excerpt)], persist=False)

    service.alternatives(run)

    card = run['alternatives'][0]
    assert card['price_lookup']['status'] == PRICED
    assert card['price_lookup'].get('reason_code') is None
    assert card['prices'] and card['prices'][0]['unit_price'] == 56000
    assert price_calls(calls), '关着的时候必须真的抽了一次价'
    assert [c['price_lookup']['status'] for c in run['alternatives']] != [NOT_REQUESTED]
    assert all(c['price_lookup']['status'] != NOT_REQUESTED for c in run['alternatives'])
    # 每个候选都有自己的稳定定位键，且已在跑完之后就有（不用等用户点）。
    assert card['candidate_id'] and card['candidate_id'] != 'None'


# ---------------------------------------------------------------------------
# 二·补、默认开只影响相似品：基线目标的成交价查询一个字没动
# ---------------------------------------------------------------------------

def test_the_default_only_defers_alternatives_not_the_baseline_lookup(research, monkeypatch):  # noqa: F811
    """环境变量没设（＝默认开）时：基线目标的成交价**照旧查得到**，相似品一个都不批量查。

    为什么这条必须是端到端的：``alternative_prices()`` 与基线车道调的是**同一个**
    ``self.prices(run, target)``（``alternative_prices()`` 里的三次调用在
    ``procurement_research.py:4421 / 4433 / 4438``，基线车道那一次是 ``_price_lane`` 里的
    ``self.prices(run, b)``，``:5382``）。开关只卡在 ``alternatives()`` 的批量循环那一处
    （``:4975``，``alternative_price_on_demand()`` 全仓**只有这一个调用点**），
    基线车道那一处没有开关。所以这里**在同一个 run、同一份替身上**两头都跑一遍：
    基线查得到 48000，相似品的价格字段一个都不动。
    """
    assert alternative_price_on_demand() is True, '环境变量没设就是开'
    service, run, _, _ = research
    baseline_line = '生物安全柜 甲厂 Q10 数量1台 单价：48,000.00 元'
    baseline_body = ('某某医院实验室设备采购项目中标（成交）结果公告\n'
                     '采购人：某某医院\n' + baseline_line + '\n')
    candidate = {'brand': '乙厂', 'model': 'Q20', 'url': NOTICE_URL_B,
                 'excerpt': Q20_PRICED_LINE, 'configuration': Q20_PRICED_LINE}
    prepare(run, [BROCHURE, notice(NOTICE_URL, baseline_body),
                  notice(NOTICE_URL_B, NOTICE_BODY_PRICED)])
    baseline_row = {**row('Q10', baseline_line, amount=48000), 'brand': '甲厂'}
    calls = stub(service, monkeypatch, [candidate], prices=[baseline_row], persist=False)

    # ① 基线目标：走它自己那条车道用的同一个 ``prices()``，价照旧抽得到。
    baseline_prices = service.prices(run, run['baseline'])
    assert baseline_prices and baseline_prices[0]['unit_price'] == 48000, baseline_prices
    assert price_calls(calls), '基线目标那一次抽价必须真的发生过'

    # ② 相似品：同一个 run 里跑 ``alternatives()``，一次抽价都不许再发生。
    looks_before = len(price_calls(calls))
    service.alternatives(run)

    assert run['alternatives'], '这一轮得真的有一个候选，否则"相似品没被查"没被验到'
    for card in run['alternatives']:
        assert card['price_lookup'] == {'status': NOT_ATTEMPTED,
                                        'reason_code': NOT_REQUESTED, 'price_count': 0}
        assert card['prices'] == []
    assert len(price_calls(calls)) == looks_before, '默认开了之后相似品不许再批量抽价'
    # 相似品的价不进 ``run['prices']``：采信链的隔离规则没被动过。
    # 注意：① 里查出来的基线那一条**本来就该在** ``run['prices']`` 里，所以这里要钉的是
    # "**只有**它、相似品一条都没混进来"。写成 ``assert run['prices'] == []`` 会与 ① 自相矛盾
    # （基线刚查到的 48000 就在里面）。这条断言仍能抓到反例：相似品的价一旦混入，
    # 型号就不止 ````，断言会失败。
    assert [item['model'] for item in run['prices']] == ['Q10'], \
        f'采信结果里只该有基线那一条，实际 {run["prices"]!r}'


# ---------------------------------------------------------------------------
# 二·补二、成本口径：同一份输入下，批量查价 vs 按需查价各花多少
# ---------------------------------------------------------------------------

def test_the_saving_is_measurable_on_the_same_input(research, monkeypatch):  # noqa: F811
    """同一份替身输入（Q20 有价、Q30 正文里没有金额）跑两遍，给成本一个可复核的数字。

    口径：``alternatives()`` 这一次调用期间
    * **抽价模型调用次数**——按 prompt 认（``price_calls``），不是按状态名推；
    * **被扫描的来源条数**——``alternative_price_lookup.sources_scanned``（车道级总账）。

    怎么是机械可复核的：``pytest tests/test_alternative_price_on_demand.py -q -s
    -k the_saving_is_measurable`` 会把两个口径的三行数字打出来。
    """
    service, run, _, _ = research

    monkeypatch.setenv(ON_DEMAND_ENV, '0')
    calls = _two_candidate_run(service, run, monkeypatch, persist=False)
    service.alternatives(run)
    batched = (len(price_calls(calls)),
               run['alternative_price_lookup']['sources_scanned'],
               len(run['alternatives']))

    monkeypatch.setenv(ON_DEMAND_ENV, '1')
    calls = _two_candidate_run(service, run, monkeypatch, persist=False)
    service.alternatives(run)
    deferred = (len(price_calls(calls)),
                run['alternative_price_lookup']['sources_scanned'],
                len(run['alternatives']))

    print(f'\n[成本口径] 同一份 2 候选输入、同一次 alternatives() 调用期间：'
          f'\n  批量查价(=0)：抽价模型调用 {batched[0]} 次，扫描来源 {batched[1]} 条，'
          f'候选 {batched[2]} 个'
          f'\n  按需查价(默认)：抽价模型调用 {deferred[0]} 次，扫描来源 {deferred[1]} 条，'
          f'候选 {deferred[2]} 个')

    assert batched[2] == deferred[2] == 2, '两遍必须是同一份输入，否则数字不可比'
    assert batched[0] > deferred[0] == 0, f'按需查价期间不许有抽价调用：{batched} / {deferred}'
    # ``sources_scanned`` 的口径是"相似品车道抽价收尾那一刻 ``run['sources']`` 的条数"
    # （``alternative_price_lookup.py:318``），**不是**这一轮实际发出的网络请求数：
    # 候选发现（入口三判据筛选）在两种模式下都必须做，否则连候选都没有，所以这个数
    # 两边相同、且都不为 0。它**不是**能证明省成本的字段。
    assert batched[1] == deferred[1] > 0, f'候选发现两边都跑，来源数应相同且非零：{batched} / {deferred}'
    # 真正被开关省掉的是"**扫描过的候选**"：按需模式下每个候选都停在 ``not_requested``，
    # 既没有候选被抽价扫描，也没有候选拿到价。
    assert run['alternative_price_lookup']['candidates_scanned'] == 0, \
        f'按需模式下不该有候选被抽价扫描：{run["alternative_price_lookup"]!r}'
    assert run['alternative_price_lookup']['candidates_with_price'] == 0


# ---------------------------------------------------------------------------
# 三、开关打开时：只查被点的那个
# ---------------------------------------------------------------------------

def _open_the_switch(monkeypatch):
    monkeypatch.setenv(ON_DEMAND_ENV, '1')


def _two_candidate_run(service, run, monkeypatch, persist=True):
    """一条真实形状的输入：Q20 在 A 公告里有单价、Q30 在 B 公告里没有金额。

    两个候选必须**各有自己的来源网址**：候选的引文要在**自己那条**网址的正文里逐字
    找得到（``supported``），共用一条网址时第二个候选会被发现阶段剔掉，那样就验不到
    "只查被点的那个"。
    """
    body_b = ('某某医院实验室设备采购项目中标（成交）结果公告\n'
              '采购人：某某医院\n' + Q30_LINE_OTHER_BRAND + '\n')
    sources = [BROCHURE, notice(NOTICE_URL, NOTICE_BODY_PRICED), notice(NOTICE_URL_B, body_b)]
    candidates = [{'brand': '乙厂', 'model': 'Q20', 'url': NOTICE_URL, 'excerpt': Q20_PRICED_LINE,
                   'configuration': Q20_PRICED_LINE},
                  {'brand': '丁厂', 'model': 'Q30', 'url': NOTICE_URL_B,
                   'excerpt': Q30_LINE_OTHER_BRAND, 'configuration': Q30_LINE_OTHER_BRAND}]
    prepare(run, sources)
    calls = stub(service, monkeypatch, candidates, prices=[row('Q20', Q20_PRICED_LINE)],
                 persist=persist)
    return calls


def test_switch_on_defers_every_price_and_says_so(research, monkeypatch):  # noqa: F811
    """打开开关：跑完一条价格都不查，每个候选写"等用户点"（按设计没查，不是没走到）。"""
    _open_the_switch(monkeypatch)
    service, run, _, _ = research
    calls = _two_candidate_run(service, run, monkeypatch, persist=False)

    service.alternatives(run)

    assert len(run['alternatives']) == 2
    assert not price_calls(calls), '打开开关之后批量查价必须一次都不跑'
    for card in run['alternatives']:
        assert card['price_lookup'] == {'status': NOT_ATTEMPTED,
                                        'reason_code': NOT_REQUESTED, 'price_count': 0}
        assert card['prices'] == []


def test_the_clicked_candidate_also_sees_the_local_deal_library(research, monkeypatch,
                                                               tmp_path):  # noqa: F811
    """候选品查价与成交价查价**共用同一条链路**，所以本地成交明细库对候选品也生效。

    为什么钉这一条（设计规范：两条通路必须同步，不许改了成交价那条、
    候选品那条没跟上）：两条入口本来就是一条链——
    ``lookup_candidate_price`` → ``verify_and_price_candidate`` → ``alternative_prices``
    → ``prices()``，而"查本地库"那一段（``local_price_rows``）就在 ``prices()`` 里，
    用的是传进来的 target 的型号与品牌。**所以以后谁要只改一处，这条用例就会红。**

    测法：库里放一条 Q20 的成交价，公告那边**一分钱都不给**（``prices=[]``），于是候选
    卡片上唯一可能出现的价格来源就是本地库；然后从**用户点按钮的那个入口**真的点一次。
    """
    _open_the_switch(monkeypatch)
    library = _build_db(tmp_path, [_item_row('Q20', '乙厂', 8800.0, 'negotiated')])
    monkeypatch.setenv('LAB_XLYG_DB', str(library))

    service, run, _, _ = research
    body_b = ('某某医院实验室设备采购项目中标（成交）结果公告\n'
              '采购人：某某医院\n' + Q30_LINE_OTHER_BRAND + '\n')
    sources = [BROCHURE, notice(NOTICE_URL, NOTICE_BODY_PRICED), notice(NOTICE_URL_B, body_b)]
    candidates = [{'brand': '乙厂', 'model': 'Q20', 'url': NOTICE_URL, 'excerpt': Q20_PRICED_LINE,
                   'configuration': Q20_PRICED_LINE},
                  {'brand': '丁厂', 'model': 'Q30', 'url': NOTICE_URL_B,
                   'excerpt': Q30_LINE_OTHER_BRAND, 'configuration': Q30_LINE_OTHER_BRAND}]
    prepare(run, sources)
    stub(service, monkeypatch, candidates, prices=[], persist=False)
    service.alternatives(run)
    cards = {c['model']: c for c in run['alternatives']}
    # 开关打开时批量不查价：点之前候选卡片上一条价格都没有。
    assert cards['Q20']['prices'] == []

    result = service.lookup_candidate_price(run, cards['Q20']['candidate_id'])

    rows = result['price_lookup']['prices']
    from_library = [row for row in rows if row.get('channel') == 'xlyg_local_db']
    assert from_library, rows
    assert from_library[0]['model'] == 'Q20'
    assert from_library[0]['unit_price'] == 8800.0
    # 库里那条要带着它的范围说明与出处，不能裸着一条金额。
    assert from_library[0]['url'].startswith('https://gdegp.gds.edu.cn:8093/')
    assert '单一省份' in from_library[0]['scope_note']


def test_only_the_clicked_candidate_is_priced(research, monkeypatch):  # noqa: F811
    """点一个候选：只有它的价格字段变，另一个候选一个字节都不动，额度只花一个人的量。

    "只花一个人的量"在这里用**被问过几次抽价**来钉（``price_calls``）：离线替身不发
    网络请求、也不走 ``consume``，所以 ``spent`` 三个零是测试环境的正常值，不是
    "没查"。真实花费的门槛由"另一个候选的抽价次数不增加、价格字段逐字不变"来保证。
    """
    _open_the_switch(monkeypatch)
    service, run, _, _ = research
    calls = _two_candidate_run(service, run, monkeypatch)
    service.alternatives(run)
    run = service.get(run['task_id'], run['id'])
    cards = {c['model']: c for c in run['alternatives']}
    other = copy.deepcopy({k: v for k, v in cards['Q30'].items() if k != 'price_lookup'})
    other_lookup = copy.deepcopy(cards['Q30']['price_lookup'])
    looks_before = len(price_calls(calls))

    result = service.lookup_candidate_price(run, cards['Q20']['candidate_id'])

    assert result['candidate_id'] == cards['Q20']['candidate_id']
    assert result['price_lookup']['status'] == 'completed'
    assert result['price_lookup']['cached'] is False
    assert result['price_lookup']['prices'][0]['unit_price'] == 56000
    assert result['price_lookup']['sources'] == [
        {'url': NOTICE_URL, 'excerpt': cards['Q20']['excerpt']}]
    # 只问了一次抽价（被点的那一个），没有顺带再问第二个候选。
    assert len(price_calls(calls)) == looks_before + 1
    # 离线替身不花额度：spent 三个零是这条测法的正常值，接口形状仍然完整。
    assert set(result['price_lookup']['spent']) == {'search', 'fetch', 'model'}

    stored = service.get(run['task_id'], run['id'])
    after = {c['model']: c for c in stored['alternatives']}
    assert after['Q20']['candidate_id'] == cards['Q20']['candidate_id']
    assert after['Q20']['price_lookup']['status'] == PRICED
    assert after['Q20']['prices'][0]['unit_price'] == 56000
    # 另一个候选：除了它自己那条"等用户点"，什么都没动。
    assert after['Q30']['price_lookup'] == other_lookup
    assert after['Q30']['price_lookup']['reason_code'] == NOT_REQUESTED
    assert {k: v for k, v in after['Q30'].items() if k != 'price_lookup'} == other
    # 被点的那个与没点的那个，价钱这件事形状不同。
    assert after['Q30']['prices'] == []


def test_clicking_the_same_candidate_twice_does_not_pay_twice(research, monkeypatch):  # noqa: F811
    """同一个候选点两次：第二次命中已有结果，抽价一次都不再问。"""
    _open_the_switch(monkeypatch)
    service, run, _, _ = research
    calls = _two_candidate_run(service, run, monkeypatch)
    service.alternatives(run)
    run = service.get(run['task_id'], run['id'])
    card = next(c for c in run['alternatives'] if c['model'] == 'Q20')

    first = service.lookup_candidate_price(run, card['candidate_id'])
    assert first['price_lookup']['cached'] is False
    assert first['price_lookup']['prices'], '第一次点必须真的查到价'
    looks = len(price_calls(calls))

    run = service.get(run['task_id'], run['id'])
    before = usage_of(run)
    second = service.lookup_candidate_price(run, card['candidate_id'])

    assert second['price_lookup']['cached'] is True
    assert second['price_lookup']['spent'] == {'search': 0, 'fetch': 0, 'model': 0}
    assert usage_of(run) == before, '第二次点不许再花任何额度'
    assert len(price_calls(calls)) == looks, '第二次点不许再问一次抽价'
    assert second['price_lookup']['prices'] == first['price_lookup']['prices']


def test_force_re_looks_up_a_candidate_that_already_has_a_price(research, monkeypatch):  # noqa: F811
    """``force=True`` 时才重查——默认那条路不重查。"""
    _open_the_switch(monkeypatch)
    service, run, _, _ = research
    calls = _two_candidate_run(service, run, monkeypatch)
    service.alternatives(run)
    run = service.get(run['task_id'], run['id'])
    card = next(c for c in run['alternatives'] if c['model'] == 'Q20')
    service.lookup_candidate_price(run, card['candidate_id'])
    before = len(price_calls(calls))

    run = service.get(run['task_id'], run['id'])
    forced = service.lookup_candidate_price(run, card['candidate_id'], force=True)

    assert forced['price_lookup']['cached'] is False
    assert len(price_calls(calls)) > before, '重查必须真的又抽了一次'


def test_an_unknown_candidate_id_costs_nothing_and_is_reported_as_unknown(research, monkeypatch):  # noqa: F811
    """不存在的 ``candidate_id``：抛 ``UnknownCandidate``，而且 ``usage`` 一个字不变。

    接口层把这个异常翻成 404（见 ``api/procurement.py``）。付费调用必须一次都没发生：
    这里用"抽价模型调用次数不变"与"usage 不变"两头一起钉。
    """
    _open_the_switch(monkeypatch)
    service, run, _, _ = research
    calls = _two_candidate_run(service, run, monkeypatch)
    service.alternatives(run)
    run = service.get(run['task_id'], run['id'])
    before_usage = usage_of(run)
    before_calls = len(price_calls(calls))

    with pytest.raises(UnknownCandidate):
        service.lookup_candidate_price(run, 'deadbeefdeadbeef')

    assert usage_of(run) == before_usage
    assert len(price_calls(calls)) == before_calls
    stored = service.get(run['task_id'], run['id'])
    assert usage_of(stored) == before_usage
    assert all(c['price_lookup']['status'] == NOT_ATTEMPTED for c in stored['alternatives'])


def test_a_candidate_without_a_price_records_why_rather_than_faking_one(research, monkeypatch):  # noqa: F811
    """点了、但正文里没有它的成交单价：记成"查了没有"，不假装查到、也不写"没查"。

    这里**不**替换 ``persist``：结论要真的落盘，第二次读回来的才是 ``candidate_lookup``
    算出的那一档（替身接管时扫描账不在 run 上，会退化成 ``not_attempted``）。
    """
    _open_the_switch(monkeypatch)
    service, run, _, _ = research
    body = NOTICE_BODY_NO_AMOUNT
    prepare(run, [BROCHURE, notice(NOTICE_URL, body)])
    stub(service, monkeypatch, [_q20_candidate(body=body)])
    service.alternatives(run)
    card = run['alternatives'][0]
    assert card['price_lookup']['reason_code'] == NOT_REQUESTED
    assert card['prices'] == []

    result = service.lookup_candidate_price(run, card['candidate_id'])

    assert result['price_lookup']['prices'] == []
    assert result['price_lookup']['detail']['status'] == SOURCES_NOT_READ
    assert result['price_lookup']['detail']['status'] != NOT_ATTEMPTED
    assert result['price_lookup']['detail']['reason_code'] is None
    assert result['price_lookup']['sources'] == []
    # 落盘之后再读一次：卡片上的结论与接口给的是同一档。
    stored = next(c for c in service.get(run['task_id'], run['id'])['alternatives']
                  if c['candidate_id'] == card['candidate_id'])
    assert stored['price_lookup']['status'] == SOURCES_NOT_READ


# ---------------------------------------------------------------------------
# 四、接口形状（用 TestClient 走真路由）
# ---------------------------------------------------------------------------

def test_the_route_prices_one_candidate_and_404s_an_unknown_id(research, monkeypatch):  # noqa: F811
    """路由层：响应形状按设计清单第二节，未知 id 回 404 且不花钱。"""
    _open_the_switch(monkeypatch)
    service, run, provider, client = research
    tid, rid = run['task_id'], run['id']
    _two_candidate_run(service, run, monkeypatch)
    service.alternatives(run)
    stored = service.get(tid, rid)
    card = next(c for c in stored['alternatives'] if c['model'] == 'Q20')

    missing = client.post(f'/api/v1/tasks/{tid}/research-runs/{rid}/alternatives/'
                          f'deadbeefdeadbeef/price', json={})
    assert missing.status_code == 404
    after_404 = usage_of(service.get(tid, rid))
    assert after_404 == usage_of(stored)

    response = client.post(f'/api/v1/tasks/{tid}/research-runs/{rid}/alternatives/'
                           f'{card["candidate_id"]}/price', json={})
    assert response.status_code == 200
    body = response.json()
    assert body['run_id'] == rid or body['run']['id'] == rid
    assert body['candidate_id'] == card['candidate_id']
    assert set(body['price_lookup']) >= {'status', 'prices', 'sources', 'spent'}
    assert body['price_lookup']['status'] in {'pending', 'completed', 'failed'}
    assert set(body['price_lookup']['spent']) == {'search', 'fetch', 'model'}
    assert body['run']['id'] == rid
    assert isinstance(body['run']['alternatives'], list)

    # 没有请求体也算合法（设计清单：空对象即可）。
    response = client.post(f'/api/v1/tasks/{tid}/research-runs/{rid}/alternatives/'
                           f'{card["candidate_id"]}/price')
    assert response.status_code == 200
    assert response.json()['price_lookup']['cached'] is True


def test_a_false_force_is_still_a_cache_hit(research, monkeypatch):  # noqa: F811
    """``{'force': False}`` 与空对象同一条路：命中已有结果，不再花钱。

    请求体是可选键，只有 ``force`` 为真才重查——这条把"默认不重复花钱"钉在接口层。
    """
    _open_the_switch(monkeypatch)
    service, run, _, client = research
    tid, rid = run['task_id'], run['id']
    _two_candidate_run(service, run, monkeypatch)
    service.alternatives(run)
    card = service.get(tid, rid)['alternatives'][0]

    first = client.post(f'/api/v1/tasks/{tid}/research-runs/{rid}/alternatives/'
                        f'{card["candidate_id"]}/price', json={'force': False})
    assert first.status_code == 200
    second = client.post(f'/api/v1/tasks/{tid}/research-runs/{rid}/alternatives/'
                         f'{card["candidate_id"]}/price', json={'force': False})
    assert second.status_code == 200
    assert second.json()['price_lookup']['cached'] is True
    assert second.json()['price_lookup']['spent'] == {'search': 0, 'fetch': 0, 'model': 0}


def test_the_switch_off_shape_is_untouched_by_the_new_route(research, monkeypatch):  # noqa: F811
    """开关显式关着时点这个接口：候选本来就有价 → 命中已有结果，不再花钱。"""
    monkeypatch.setenv(ON_DEMAND_ENV, '0')
    service, run, _, client = research
    tid, rid = run['task_id'], run['id']
    body = NOTICE_BODY_PRICED
    excerpt = next(line for line in body.splitlines() if 'Q20' in line)
    prepare(run, [BROCHURE, notice(NOTICE_URL, body)])
    calls = stub(service, monkeypatch, [_q20_candidate()], prices=[row('Q20', excerpt)])
    service.alternatives(run)
    card = service.get(tid, rid)['alternatives'][0]
    before = len(price_calls(calls))

    response = client.post(f'/api/v1/tasks/{tid}/research-runs/{rid}/alternatives/'
                           f'{card["candidate_id"]}/price', json={})

    assert response.status_code == 200
    assert response.json()['price_lookup']['cached'] is True
    assert len(price_calls(calls)) == before


def test_the_replaceable_stub_shape_is_still_a_python_dict(research):  # noqa: F811
    """JSON 里跑一圈还是同一份东西：候选与 id 都能序列化（接口要原样返回 run）。"""
    candidate = {'model': 'Q20', 'url': NOTICE_URL}
    assign_candidate_ids([candidate])
    assert json.loads(json.dumps(candidate))['candidate_id'] == candidate['candidate_id']
    assert ProcurementResearch is not None


# ---------------------------------------------------------------------------
# 六、点击不许落在"收尾的旧快照"前面（2026-10-01 ``sample-a-fix1`` 那一处）
# ---------------------------------------------------------------------------

def test_a_click_that_lands_while_the_round_is_finishing_is_not_erased(research, monkeypatch):  # noqa: F811
    """车道跑完、整轮已写 ``partial``、**收尾还在跑**时点一次：查到的结论必须留在库里。

    端到端实测（``sample-a-fix1``）的真实顺序是：收尾的写（阶段账／报告总述）带着
    "收尾开始时读到的那份 run 副本"排在点击后面落库，把用户刚花过钱查到的候选整份盖回
    ``not_attempted``——接口 200 带完整结果，库里却没有，第二次点击又花一次钱
    （证据：``stage_timings`` 6→7、用量与候选原样退回点击前）。

    这里把那个顺序钉死、把不确定性去掉：报告线程先读完副本、停在总述那一次模型调用上，
    点击整条路走完，再放它写回。接口回了什么，库里就必须是什么（``设计规范`` §3：
    不许把"没保存成功"变成"接口返回成功"）。
    """
    _open_the_switch(monkeypatch)
    service, run, _, client = research
    tid, rid = run['task_id'], run['id']
    _two_candidate_run(service, run, monkeypatch)
    service.alternatives(run)
    card = next(c for c in service.get(tid, rid)['alternatives'] if c['model'] == 'Q20')
    assert card['price_lookup']['status'] == NOT_ATTEMPTED

    # 收尾线程：与 ``execute()`` 一样先占住这一轮的执行租约，再跑报告（收尾的最后一段）。
    # 轮次与额度都按 ``start_lane`` 那一套给：报告 id 由 (run_id, round) 决定，新一轮才有
    # 一份**新报告**要走"取总述"那段（老报告走的是不调模型的快路径）；额度留出来，
    # 报告总述才不会因为"模型额度用完"直接降级。这样收尾真的走到"读完副本 → 调模型 →
    # 写回阶段账"，也就是实测里排在点击后面的那一次写。
    live = service.get(tid, rid)
    live['round'] += 1
    live.setdefault('limits', {})['model'] = (live.get('usage') or {}).get('model', 0) + 5
    service.save(live)
    claim = 'research:' + rid
    owner = service.repo.claim_analysis(claim)
    assert owner
    tail, entered, release = {}, threading.Event(), threading.Event()
    base_model = service.model

    def gated_model(run_, prompt, data):
        if threading.current_thread() is tail['thread']:
            entered.set()
            assert release.wait(20), '收尾线程没有被放行'
        return base_model(run_, prompt, data)

    def finishing():
        service.execution.claim = (claim, owner)
        try:
            service.report(tid, rid)
        finally:
            service.execution.claim = None
            service.repo.release_analysis(claim, owner)

    monkeypatch.setattr(service, 'model', gated_model)
    tail['thread'] = threading.Thread(target=finishing, daemon=True)
    tail['thread'].start()
    assert entered.wait(20), '收尾线程没有走到总述那一次模型调用'
    threading.Timer(0.5, release.set).start()

    response = client.post(f'/api/v1/tasks/{tid}/research-runs/{rid}/alternatives/'
                           f'{card["candidate_id"]}/price', json={})
    release.set()
    tail['thread'].join(20)

    assert response.status_code == 200, response.text
    answered = response.json()['price_lookup']['detail']['status']
    stored = next(c for c in service.get(tid, rid)['alternatives']
                  if c['candidate_id'] == card['candidate_id'])
    assert answered != NOT_ATTEMPTED, '这一次点击本来应该真的问到结论'
    assert stored['price_lookup']['status'] == answered, '接口说查到了，库里必须也有'


def test_a_click_while_the_round_is_still_running_is_refused_and_costs_nothing(research, monkeypatch):  # noqa: F811
    """这一轮还在跑（``running`` 且租约未释放）时点一次：响亮拒绝，一次付费调用都不发生。

    与收尾那一处同一个原因：这一轮的写带着它自己那份 run 副本，随时可能盖掉点击结果。
    拒绝的粒度按内部事实说——"还在进行"与"正在收尾"是两件事，不许合成一句。
    """
    _open_the_switch(monkeypatch)
    service, run, _, client = research
    tid, rid = run['task_id'], run['id']
    calls = _two_candidate_run(service, run, monkeypatch)
    service.alternatives(run)
    card = next(c for c in service.get(tid, rid)['alternatives'] if c['model'] == 'Q20')

    live = service.get(tid, rid)
    live['status'] = 'running'
    service.save(live)
    owner = service.repo.claim_analysis('research:' + rid)
    assert owner
    try:
        before_usage, before_calls = usage_of(service.get(tid, rid)), len(price_calls(calls))
        response = client.post(f'/api/v1/tasks/{tid}/research-runs/{rid}/alternatives/'
                               f'{card["candidate_id"]}/price', json={})
    finally:
        service.repo.release_analysis('research:' + rid, owner)

    assert response.status_code == 409
    assert response.json()['detail']
    assert usage_of(service.get(tid, rid)) == before_usage, '被拒的那一次不许花任何额度'
    assert len(price_calls(calls)) == before_calls, '被拒的那一次不许问模型'


# ---------------------------------------------------------------------------
# 五、参考候选也能查价（设计规范「定这个」）
# ---------------------------------------------------------------------------

#: 四个品牌：``diverse_candidates`` 一个品牌只给一个核验名额、上限 3（既有判据，
#: 本单没动它），所以第四个候选**必然**落进参考候选名单——要验"参考候选也能查价"，
#: 就得先真的有一条参考候选。
FOUR_BRANDS = (('乙厂', 'Q20', 52000), ('丁厂', 'Q30', 51000),
               ('戊厂', 'Q40', 54000), ('己厂', 'Q50', 55000))


def _four_brand_run(service, run, monkeypatch, persist=True):
    """四个品牌各一条候选，每条有自己的公告与自己的原文行（第四条落参考候选）。

    公告里都带单价行：参考候选查价时抽的就是它。
    """
    candidates, sources = [], []
    for index, (brand, model, amount) in enumerate(FOUR_BRANDS):
        url = f'https://hospital-{index}.example.com/notice/2026-09-11'
        line = f'生物安全柜 {brand} {model} 数量1台 单价：{amount:,}.00 元'
        body = ('某某医院实验室设备采购项目中标（成交）结果公告\n'
                '采购人：某某医院\n' + line + '\n')
        sources.append(notice(url, body))
        # ``configuration``：2026-10-06 起空配置的候选会被撤下候选卡片
        # （``tests/test_m4_pipeline.py``），而这里的断言要真的落出候选卡片。
        candidates.append({'brand': brand, 'model': model, 'url': url, 'excerpt': line,
                           'configuration': line})
    prepare(run, sources)
    calls = stub(service, monkeypatch, candidates, persist=persist)
    service.alternatives(run)
    return calls, {candidate['model']: candidate for candidate in candidates}


def _price_row_for(candidate, brand, amount):
    return {**row(candidate['model'], candidate['excerpt'], amount=amount), 'brand': brand}


def test_a_reference_candidate_can_be_priced_without_changing_its_roster(research, monkeypatch):  # noqa: F811
    """参考候选点查价：不再 404，价照查照写，但**名单不变**（不许被搬进正式候选）。

    走过去那条唯一的路（真路由 → ``run_exclusive`` → ``lookup_candidate_price``）；
    参考候选过去在这条路上是 404（查找范围只有 ``run['alternatives']``）。
    """
    _open_the_switch(monkeypatch)
    service, run, _, client = research
    tid, rid = run['task_id'], run['id']
    calls, candidates = _four_brand_run(service, run, monkeypatch)
    stored = service.get(tid, rid)
    assert len(stored['alternatives']) == 3, stored['alternatives']
    assert len(stored['reference_candidates']) == 1, stored['reference_candidates']
    card = stored['reference_candidates'][0]
    assert card['candidate_id'], '参考候选也得有候选编号，否则卡片上连按钮都不给'
    assert card['price_lookup']['reason_code'] == NOT_REQUESTED
    # 抽价时模型要能给出这条候选的单价行（公告正文里逐字写着）。
    stub(service, monkeypatch, [], prices=[_price_row_for(candidates[card['model']], card['brand'], 55000)])

    response = client.post(f'/api/v1/tasks/{tid}/research-runs/{rid}/alternatives/'
                           f'{card["candidate_id"]}/price', json={})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body['candidate_id'] == card['candidate_id']
    assert body['price_lookup']['cached'] is False
    assert body['price_lookup']['status'] == 'completed'
    assert [item['unit_price'] for item in body['price_lookup']['prices']] == [55000]
    assert body['price_lookup']['sources'] == [{'url': candidates[card['model']]['url'],
                                                'excerpt': card['excerpt']}]

    after = service.get(tid, rid)
    ids = [candidate['candidate_id'] for candidate in after['alternatives']]
    assert card['candidate_id'] not in ids, '参考候选被搬进正式候选名单了'
    assert len(after['alternatives']) == 3
    assert [candidate['candidate_id'] for candidate in after['reference_candidates']] == [card['candidate_id']]
    saved = after['reference_candidates'][0]
    assert saved['price_lookup']['status'] == PRICED
    assert [item['unit_price'] for item in saved['prices']] == [55000]

    # 再点一次：命中已有结果，不再花钱（与正式候选同一条默认行为）。
    looks = len(price_calls(calls))
    again = client.post(f'/api/v1/tasks/{tid}/research-runs/{rid}/alternatives/'
                        f'{card["candidate_id"]}/price', json={})
    assert again.status_code == 200
    assert again.json()['price_lookup']['cached'] is True
    assert again.json()['price_lookup']['spent'] == {'search': 0, 'fetch': 0, 'model': 0}
    assert len(price_calls(calls)) == looks, '第二次点不许再问一次抽价'


def test_the_reference_route_does_not_relax_the_price_rules(research, monkeypatch):  # noqa: F811
    """参考候选与正式候选**同一套核价判据**：价格行上的品牌对不上，照样不采信。

    这是本单要回答的那个问题的落地断言（设计规范问：参考候选本身带疑点时，查价校验该不该
    和正式候选一致）。答案是**一致**：``validate_price`` 的每一条判据读的都是"目标
    （这条候选）"和"这一行价格"，与候选落在哪张名单里毫无关系
    （型号与品牌两条在 ``procurement_rules.py:750-753``）。这条测试给参考候选一条
    **别的品牌**的价格行——它不会被采信，卡片记成"查到了但没有采用"。
    """
    _open_the_switch(monkeypatch)
    service, run, _, client = research
    tid, rid = run['task_id'], run['id']
    _, candidates = _four_brand_run(service, run, monkeypatch)
    card = service.get(tid, rid)['reference_candidates'][0]
    stub(service, monkeypatch, [],
         prices=[_price_row_for(candidates[card['model']], '庚厂', 55000)])

    response = client.post(f'/api/v1/tasks/{tid}/research-runs/{rid}/alternatives/'
                           f'{card["candidate_id"]}/price', json={})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body['price_lookup']['prices'] == [], '跨品牌的价格被采信了'
    assert body['price_lookup']['detail']['status'] == REFUSED
    assert 'brand_mismatch' in json.dumps(body['price_lookup']['detail'], ensure_ascii=False)
    stored = service.get(tid, rid)['reference_candidates'][0]
    assert stored['price_lookup']['status'] == REFUSED
    assert stored['prices'] == []


def test_a_slotted_candidate_placed_into_the_reference_roster_gets_an_id(research):  # noqa: F811
    """兜底补进参考候选的那批也要有候选编号，否则卡片上的查价按钮还是点不动。

    ``place_slotted_candidates`` 补的是 ``deepcopy`` 出来的新对象，而
    ``alternatives()`` 里那次 ``assign_candidate_ids`` 只跑过原始对象；前端按编号定位
    候选，没编号的卡片按钮是禁用的（见 ``priceButtonState``）。
    """
    service, run, _, _ = research
    prepare(run, [])
    candidate = {'brand': '乙厂', 'model': 'Q20', 'url': NOTICE_URL, 'excerpt': Q20_LINE}
    # 名额名单的键与实现同一套归一（``identity`` 会把型号折成小写：``Q20`` → ``q20``）。
    key = (identity('乙厂'), identity('Q20'))

    service.place_slotted_candidates(run, {key}, [candidate])

    assert len(run['reference_candidates']) == 1
    placed = run['reference_candidates'][0]
    assert placed['reference_reason'] == 'no_conclusion'
    assert placed['candidate_id'] == candidate_key(candidate), '兜底补的候选没有候选编号'

    # 再补一次是幂等的：编号不变、也不会多出一张卡片。
    again = {'brand': '乙厂', 'model': 'Q20', 'url': NOTICE_URL, 'excerpt': Q20_LINE}
    service.place_slotted_candidates(run, {key}, [again])
    assert len(run['reference_candidates']) == 1
    assert run['reference_candidates'][0]['candidate_id'] == placed['candidate_id']
