# -*- coding: utf-8 -*-
"""sample-c 候选漂移的四条修复（取证：``output/c-drift/report.md``）。

四条分别是：

1. 引文里没有候选型号时**就地补引文**，补不到才丢**这一条**（过去是整批拒收）；
2. 规则直读的线索与模型提出的候选**始终合并去重**（过去只在模型给空时接管）；
3. 品牌串做**规范化校验**（``为喆图。`` 这种解析残渣不许当品牌）；
4. 参数值写进比较基准前要**在它自己的引文里逐字读到**（对不上就不写这一项、留痕）。

每一条都先有反例（红）：把对应改动回退后，本文件里属于它的用例必须失败。
"""
import copy

from backend.services.procurement_research import (
    ON_DEMAND_ENV,
    excerpt_with_model,
    gate_discovered_candidates,
    merge_discovered_candidates,
    parameters_with_verbatim_evidence,
)
from backend.services.procurement_rules import normalize_brand_value
from backend.services.research_planning import (brand_from_detail_page, discovery_sources,
                                                 diverse_candidates)
from tests.test_reliability_development import research  # noqa: F401 - pytest fixture

CATEGORY = '恒温培养箱'


def _page(url, text, purpose='discovery'):
    return {'url': url, 'purpose': purpose, 'body_status': 'fetched',
            'source_type': 'manufacturer_website', 'body_text': text}


# ---------------------------------------------------------------- 第 1 条：就地补引文

#: c4 那一轮的真实形状：同一页上既有尺寸表行、也有型号表行。模型把**尺寸行**当引文
#: （``report.md`` §2.1 的 5 条），而正文里本来就有含型号的行。
DIMENSION_PAGE = _page('https://a.example/zdp.html',
                       '内胆尺寸W*D*H(mm) | 250*260*250 | 400*400*500\n'
                       '产品型号 | ZDP-9012 | ZDP-9082 | ZDP-9272')
#: 那一页确实没有这台型号的行：这条候选只能丢（报告里 5 条里唯一找不到的那条）。
WITHOUT_MODEL_PAGE = _page('https://b.example/dhp.html',
                           '内胆尺寸(mm)W×D×H | 340×320×320\n'
                           '产品型号 | DHP-9032T | DHP-9272T')
RESIDUE_DIMENSION = '内胆尺寸W*D*H(mm) | 250*260*250 | 400*400*500'
C4_LIKE = [
    {'brand': '喆图', 'model': 'ZDP-9082', 'url': DIMENSION_PAGE['url'], 'excerpt': RESIDUE_DIMENSION},
    {'brand': '一恒', 'model': 'DHP-9082', 'url': WITHOUT_MODEL_PAGE['url'],
     'excerpt': '内胆尺寸(mm)W×D×H | 340×320×320'},
    {'brand': '凭空', 'model': 'X9', 'url': DIMENSION_PAGE['url'], 'excerpt': '这一行在正文里根本不存在'},
]


def test_excerpt_with_model_takes_the_model_row_from_its_own_source():
    line = excerpt_with_model(DIMENSION_PAGE['body_text'], 'ZDP-9082')
    assert line == '产品型号 | ZDP-9012 | ZDP-9082 | ZDP-9272'
    # 配件/转子那一行不是"这台仪器"的证据，补引文时同样跳过。
    body = '转子货号 ZDP-9082 适配器\n电热恒温培养箱 ZDP-9082 技术参数'
    assert excerpt_with_model(body, 'ZDP-9082') == '电热恒温培养箱 ZDP-9082 技术参数'
    # 正文里确实没有这个型号 → 不猜、不借别的来源，返回空串。
    assert excerpt_with_model(WITHOUT_MODEL_PAGE['body_text'], 'DHP-9082') == ''


def test_gate_repairs_the_excerpt_instead_of_rejecting_the_whole_batch():
    discovered, repaired, dropped = gate_discovered_candidates(
        C4_LIKE, [DIMENSION_PAGE, WITHOUT_MODEL_PAGE], 'SHP-080')
    # 补到引文的那一条留下来了，引文换成含型号的原文行（型号与地址一个字没改）。
    assert [c['model'] for c in discovered] == ['ZDP-9082']
    assert discovered[0]['excerpt'] == '产品型号 | ZDP-9012 | ZDP-9082 | ZDP-9272'
    assert discovered[0]['url'] == DIMENSION_PAGE['url']
    assert repaired == ['ZDP-9082']
    # 丢的是"确实找不到"的那一条与"引文在自己来源里都不存在"的那一条，不是整批。
    assert [(item['model'], item['reason']) for item in dropped] == [
        ('DHP-9082', 'excerpt_without_model'), ('X9', 'excerpt_not_in_source')]


def test_the_gate_does_not_touch_a_candidate_that_already_quotes_the_model():
    page = _page('https://c.example/taisite.html', '电热恒温培养箱 WPL-125D 技术参数 控温范围 RT+5~80℃')
    candidate = {'brand': '泰斯特', 'model': 'WPL-125D', 'url': page['url'],
                 'excerpt': '电热恒温培养箱 WPL-125D 技术参数 控温范围 RT+5~80℃'}
    discovered, repaired, dropped = gate_discovered_candidates([candidate], [page], 'SHP-080')
    assert [c['model'] for c in discovered] == ['WPL-125D']
    assert repaired == [] and dropped == []


def test_alternatives_records_why_a_candidate_was_skipped(research, monkeypatch):  # noqa: F811
    """留痕口径（``设计规范`` §5）：人话进 ``gaps``，原文与原因码进 ``failures``。"""
    monkeypatch.setenv(ON_DEMAND_ENV, '0')
    service, run, _, _ = research
    run.update({'baseline': {'brand': '上海精宏', 'model': 'SHP-080', 'category': CATEGORY,
                             'scenario': '微生物培养', 'parameters': []},
                'sources': [copy.deepcopy(DIMENSION_PAGE), copy.deepcopy(WITHOUT_MODEL_PAGE)],
                'alternatives': [], 'discovered_candidates': [], 'reference_candidates': [],
                'prices': {}, 'gaps': [], 'failures': []})

    def fake_model(run_, prompt, data):
        if '其他仪器' in prompt:
            return {'candidates': copy.deepcopy(C4_LIKE)}
        return {'parameters': []}

    monkeypatch.setattr(service, 'model', fake_model)
    monkeypatch.setattr(service, 'query', lambda *args, **kwargs: None)
    monkeypatch.setattr(service, 'verify_publishers', lambda *args, **kwargs: None)
    monkeypatch.setattr(service, 'alternative_prices', lambda *args, **kwargs: {})
    monkeypatch.setattr(service, 'persist', lambda *args, **kwargs: None)
    service.alternatives(run)

    models = [c['model'] for c in run['discovered_candidates']]
    assert 'ZDP-9082' in models, models                       # 回退成整批拒收时这里为空
    assert 'DHP-9082' not in models and 'X9' not in models    # 找不到 / 编造的那两条仍然丢
    assert any('补上含型号的原文行' in gap for gap in run['gaps']), run['gaps']
    assert any('已跳过这 2 条' in gap for gap in run['gaps']), run['gaps']
    rejects = [f for f in run['failures'] if f['type'] == 'CandidateExcerptRejected']
    assert sorted(f['code'] for f in rejects) == ['excerpt_not_in_source', 'excerpt_without_model']
    assert rejects[0]['location']['file'] == 'candidate_pipeline.py'
    assert rejects[0]['model'] in {'DHP-9082', 'X9'}


# ------------------------------------------------- 第 2 条：兜底与模型结果始终合并

MODEL_PAGE = _page('https://d.example/q30.html',
                   '槽式超声波清洗机 丙厂 Q30 技术参数 容量 100 L')
SCAN_PAGE = _page('https://e.example/q20.html',
                  '槽式超声波清洗机 乙厂 Q20 技术参数 容量 100 L')
WASH_BASELINE = {'brand': '甲厂', 'model': 'Q10', 'category': '超声波清洗机', 'scenario': '清洗器皿',
                 'parameters': []}


def test_merge_keeps_both_sources_and_drops_the_duplicate_model():
    scanned = [{'brand': '乙厂', 'model': 'Q20', 'url': SCAN_PAGE['url'], 'excerpt': SCAN_PAGE['body_text'],
                'proposed_by': 'text_scan'}]
    discovered = [{'brand': '丙厂', 'model': 'Q30', 'url': MODEL_PAGE['url'], 'excerpt': MODEL_PAGE['body_text']}]
    merged, added = merge_discovered_candidates(discovered, scanned)
    assert [c['model'] for c in merged] == ['Q30', 'Q20']     # 模型候选在前
    assert [c['model'] for c in added] == ['Q20']
    # 同一个型号模型已经提过：正文行再读到一次不重复入池，且保留模型那条。
    merged, added = merge_discovered_candidates(discovered, [
        {'brand': '丙厂', 'model': 'Q30', 'url': MODEL_PAGE['url'], 'proposed_by': 'text_scan'}])
    assert [c['model'] for c in merged] == ['Q30'] and added == []


def test_alternatives_merges_the_rule_leads_even_when_the_model_proposed_some(research, monkeypatch):  # noqa: F811
    monkeypatch.setenv(ON_DEMAND_ENV, '0')
    service, run, _, _ = research
    run.update({'baseline': dict(WASH_BASELINE),
                'sources': [copy.deepcopy(MODEL_PAGE), copy.deepcopy(SCAN_PAGE)],
                'alternatives': [], 'discovered_candidates': [], 'reference_candidates': [],
                'prices': {}, 'gaps': [], 'failures': []})

    def fake_model(run_, prompt, data):
        if '其他仪器' in prompt:
            return {'candidates': [{'brand': '丙厂', 'model': 'Q30', 'url': MODEL_PAGE['url'],
                                    'excerpt': MODEL_PAGE['body_text']}]}
        return {'parameters': []}

    monkeypatch.setattr(service, 'model', fake_model)
    monkeypatch.setattr(service, 'query', lambda *args, **kwargs: None)
    monkeypatch.setattr(service, 'verify_publishers', lambda *args, **kwargs: None)
    monkeypatch.setattr(service, 'alternative_prices', lambda *args, **kwargs: {})
    monkeypatch.setattr(service, 'persist', lambda *args, **kwargs: None)
    service.alternatives(run)

    models = [c['model'] for c in run['discovered_candidates']]
    assert models == ['Q30', 'Q20'], models                   # 回退成"只在模型给空时接管"时 Q20 不会出现
    assert any(c.get('proposed_by') == 'text_scan' for c in run['discovered_candidates'])
    assert any('又按正文原文行补了 1 条线索' in gap for gap in run['gaps']), run['gaps']
    # 模型这一轮给空时的老口径一个字没改。
    run['discovered_candidates'] = []
    run['gaps'] = []
    # 上一轮进过核验名单的候选会被当成"沿用上一轮"重新喂回闸门，这里清干净再跑。
    run['alternatives'] = []
    run['reference_candidates'] = []

    def empty_model(run_, prompt, data):
        return {'candidates': []}

    monkeypatch.setattr(service, 'model', empty_model)
    service.alternatives(run)
    assert 'Q20' in [c['model'] for c in run['discovered_candidates']]
    assert all(c.get('proposed_by') == 'text_scan' for c in run['discovered_candidates'])
    assert any(gap.startswith('发现模型这一轮没有提出候选，已按正文原文行直接列出以下线索')
               for gap in run['gaps']), run['gaps']


def test_valid_empty_candidate_response_is_recorded_without_becoming_a_failure(research):  # noqa: F811
    service, run, _, _ = research

    class EmptyCandidateModel:
        endpoint = 'offline-empty-candidate'
        model = 'candidate-discovery-test'
        last_usage = None

        def analyze(self, **kwargs):
            return {'candidates': []}

    service.client_factory = EmptyCandidateModel
    result = service.model(run, 'valid_empty_candidates_stability_probe', {'probe': 'candidate'})

    assert result['response_kind'] == 'valid_empty_candidates'
    assert any(saved.get('response_kind') == 'valid_empty_candidates'
               for saved in run['model_results'].values())
    assert not any(f.get('type') == 'ProviderFailure' for f in run.get('failures', []))


def test_blank_candidate_placeholder_is_recorded_as_empty_without_changing_candidates(research):  # noqa: F811
    service, run, _, _ = research

    class BlankCandidateModel:
        endpoint = 'offline-blank-candidate'
        model = 'candidate-discovery-blank-test'
        last_usage = None

        def analyze(self, **kwargs):
            return {'candidates': [{'model': '  ', 'brand': '博科'}]}

    service.client_factory = BlankCandidateModel
    result = service.model(run, 'blank_candidate_placeholder_probe', {'probe': 'candidate'})

    assert result['candidates'] == [{'model': '  ', 'brand': '博科'}]
    assert result['response_kind'] == 'valid_empty_candidates'
    assert not any(f.get('type') == 'ProviderFailure' for f in run.get('failures', []))


def test_an_old_cached_empty_response_is_backfilled_and_persisted(research):  # noqa: F811
    service, run, _, _ = research

    class EmptyCandidateModel:
        endpoint = 'offline-empty-cache'
        model = 'candidate-discovery-cache-test'
        last_usage = None
        calls = 0

        def analyze(self, **kwargs):
            self.calls += 1
            if self.calls > 1:
                raise AssertionError('the existing run cache should be used')
            return {'candidates': []}

    client = EmptyCandidateModel()
    service.client_factory = lambda: client
    before_keys = set(run.get('model_results') or {})
    first = service.model(run, 'cached_empty_candidates_probe', {'probe': 'candidate'})
    assert first['response_kind'] == 'valid_empty_candidates'
    key = (set(run['model_results']) - before_keys).pop()
    run['model_results'][key].pop('response_kind')
    service.persist(run)

    result = service.model(run, 'cached_empty_candidates_probe', {'probe': 'candidate'})
    saved = service.get(run['task_id'], run['id'])['model_results'][key]
    assert result['response_kind'] == 'valid_empty_candidates'
    assert saved['response_kind'] == 'valid_empty_candidates'
    assert client.calls == 1


def test_merge_order_is_independent_of_model_and_scan_input_order():
    model_candidates = [
        {'brand': '丙厂', 'model': 'Q30', 'url': 'https://z.example/item', 'excerpt': 'Q30'},
        {'brand': '丁厂', 'model': 'Q31', 'url': 'https://a.example/item', 'excerpt': 'Q31'},
    ]
    scanned_candidates = [
        {'brand': '乙厂', 'model': 'Q20', 'url': 'https://m.example/item', 'excerpt': 'Q20',
         'proposed_by': 'text_scan'},
    ]

    first, _ = merge_discovered_candidates(model_candidates, scanned_candidates)
    second, _ = merge_discovered_candidates(list(reversed(model_candidates)),
                                             list(reversed(scanned_candidates)))

    assert [(c['brand'], c['model'], c['url']) for c in first] == [
        ('丁厂', 'Q31', 'https://a.example/item'),
        ('丙厂', 'Q30', 'https://z.example/item'),
        ('乙厂', 'Q20', 'https://m.example/item'),
    ]
    assert [(c['brand'], c['model'], c['url']) for c in first] == [
        (c['brand'], c['model'], c['url']) for c in second]


def test_merge_deduplicates_duplicate_model_candidates_deterministically():
    discovered = [
        {'brand': '乙厂', 'model': 'Q20', 'url': 'https://z.example/item', 'excerpt': 'Q20'},
        {'brand': '乙厂', 'model': 'Q20', 'url': 'https://a.example/item', 'excerpt': 'Q20'},
    ]

    merged, added = merge_discovered_candidates(list(reversed(discovered)), [])

    assert [(c['model'], c['url']) for c in merged] == [('Q20', 'https://a.example/item')]
    assert added == []


def test_alternatives_keeps_unknown_category_as_a_user_visible_gap(research, monkeypatch):  # noqa: F811
    monkeypatch.setenv(ON_DEMAND_ENV, '0')
    service, run, _, _ = research
    run.update({'baseline': {'brand': '甲厂', 'model': 'Q10', 'category': None,
                             'scenario': '采购一台仪器', 'parameters': []},
                'sources': [_page('https://unknown.example/page',
                                  '某型号 ABC123 超声波清洗机')],
                'alternatives': [], 'discovered_candidates': [], 'reference_candidates': [],
                'prices': {}, 'gaps': [], 'failures': []})

    monkeypatch.setattr(service, 'model', lambda *args, **kwargs: {'candidates': []})
    monkeypatch.setattr(service, 'query', lambda *args, **kwargs: None)
    monkeypatch.setattr(service, 'verify_publishers', lambda *args, **kwargs: None)
    monkeypatch.setattr(service, 'alternative_prices', lambda *args, **kwargs: {})
    monkeypatch.setattr(service, 'persist', lambda *args, **kwargs: None)

    service.alternatives(run)

    assert run['discovered_candidates'] == []
    assert '候选发现：无法从已确认品类或已取得正文中的原型号行确认仪器品类，因此未造候选。' in run['gaps']


# ------------------------------------------------------- 第 3 条：品牌串规范化校验

DETAIL_PAGE = ('喆图ZDP-9272电热恒温培养箱_报价\n'
               '具体型号是ZDP-9082，品牌为喆图。')


def test_a_parsed_residue_is_normalized_instead_of_becoming_a_brand():
    assert normalize_brand_value('为喆图。') == '喆图'
    assert normalize_brand_value('喆图') == '喆图'
    assert normalize_brand_value('（喆图）') == '喆图'
    # 规范不出来的判为"品牌不明"：带数字的、噪声词的、不是一句话里切出来的。
    assert normalize_brand_value('为3M。') == ''
    assert normalize_brand_value('详见附件') == ''
    # 详情页那一处（``品牌为喆图。`` 被正则整段吃下）读出来的不再是残渣。
    assert brand_from_detail_page(DETAIL_PAGE, 'ZDP-9272') == '喆图'


def test_the_empty_brand_still_reads_as_brand_unresolved():
    selected, references = diverse_candidates(
        [{'brand': '', 'model': 'ZDP-9082'}], '上海精宏')
    assert selected == []
    assert references[0]['reference_reason'] == 'brand_unresolved'


def test_a_real_brand_spelling_is_left_alone():
    assert brand_from_detail_page('FC5816 离心机\n品牌 奥豪斯 [https://x]', 'FC5816') == '奥豪斯'
    assert normalize_brand_value('上海精宏实验设备有限公司') == '上海精宏实验设备有限公司'


# ------------------------------------------- 第 4 条：参数值写进基准前做逐字校验

def test_parameter_value_must_be_readable_in_its_own_citation():
    kept, gaps, failures = parameters_with_verbatim_evidence([
        {'name': '容量', 'value': 100, 'unit': 'ml', 'direction': 'min',
         'sources': [{'excerpt': '容量 | 100 ml'}]},                       # 逐字支持 → 保留
        {'name': '工作室尺寸', 'value': '400x400x500', 'unit': 'mm', 'direction': 'equal',
         'sources': [{'excerpt': '工作室尺寸(mm) | 600*550*750'}]},        # 对不上 → 不写
        {'name': '电源电压', 'value': 220, 'unit': 'V', 'direction': 'equal'},  # 没有引文 → 原样保留
        {'name': '控制范围', 'value': [5, 60], 'unit': '℃', 'direction': 'covers',
         'sources': [{'excerpt': '控制范围 5~60 ℃'}]},                    # 两端都在引文里
    ])
    assert [p['name'] for p in kept] == ['容量', '电源电压', '控制范围']
    assert gaps and '400x400x500' in gaps[0] and '未写进比较基准' in gaps[0]
    assert failures[0]['code'] == 'parameter_value_unverified'
    assert failures[0]['type'] == 'ParameterValueNotVerbatim'
    assert failures[0]['name'] == '工作室尺寸'


def test_size_separator_spelling_is_not_a_difference():
    # 400*400*500（原文）与 400x400x500 是同一件事；只有这一处差异被允许。
    kept, gaps, failures = parameters_with_verbatim_evidence([
        {'name': '工作室尺寸', 'value': '400x400x500', 'unit': 'mm', 'direction': 'equal',
         'sources': [{'excerpt': '工作室尺寸(mm) | 400*400*500'}]}])
    assert len(kept) == 1 and not gaps and not failures


def test_confirm_refuses_to_write_an_unchecked_parameter_value(research):  # noqa: F811
    """写进比较基准那一步的第二道闸门（``confirm``）：不通过就不落成确定值，并留痕。"""
    service, run, _, client = research
    response = client.post(
        f"/api/v1/tasks/{run['task_id']}/baseline/save",
        json={'brand': '上海精宏', 'model': 'SHP-080', 'scenario': '微生物培养', 'parameters': [
            {'name': '容量', 'value': 100, 'unit': 'ml', 'direction': 'min',
             'sources': [{'document_id': 'd1', 'excerpt': '容量 | 100 ml'}]},
            {'name': '工作室尺寸', 'value': '400x400x500', 'unit': 'mm', 'direction': 'equal',
             'sources': [{'document_id': 'd1', 'excerpt': '工作室尺寸(mm) | 600*550*750'}]},
        ]})
    assert response.status_code == 200, response.text
    saved = response.json()
    assert [p['name'] for p in saved['baseline']['parameters']] == ['容量']
    assert any('工作室尺寸' in gap and '未写进比较基准' in gap for gap in saved['gaps']), saved['gaps']
    assert saved['failures'][0]['code'] == 'parameter_value_unverified'
    assert '600*550*750' in saved['failures'][0]['error']
