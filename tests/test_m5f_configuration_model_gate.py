"""迭代规范：**采用一份配置之前**的型号闸门（设计规范，提交三升级为"配置归属转移"）。

真实例子两件：

* ``BXM-145V-MII``（博迅）被接上的参数页是 ``boxun.com.cn/product-402.html``，而那一页讲的是
  同系列的 ``BXM-170V-MⅡ``——正文里"型号：BXM-170V-MⅡ"出现 3 次，候选型号只在页脚
  "相关产品"里出现过 1 次。选页那一步只检查"这一页全文里出现过候选型号"，被页脚骗过；而
  真正讲这一台的那一页（``product-401.html``）当轮**已经抓到了**，就在候选的
  ``source_pages`` 里（``origin='discovered'``）。
* ``bekomat30`` 的配置来自 ``olaibo.com/ProductDetail/630.html``，那页是 ``BBS-V600``。
  设计规范：「**那不是应该把 BBS-V600 列入候选吗**」——所以配置不该被扔掉，该**归给它
  真正的型号**：本轮候选里已经有 BBS-V600 就归给它，没有就按配置页认出的型号新建一条。

三档固定顺序（``configuration_model`` = X，候选自己的型号 = Y）：

① ``source_pages`` 里有 ``origin='discovered'`` 的页面、且它抽得出**合格**的配置
  （过产出质量闸与型号闸）→ Y 改用那一份；
② 无论①走没走成，**Y 原来拿错的那一份配置都按归属转移还给 X**（已有 → 归给它；
  已有且它本来就有配置 → 一个字不改；没有 → 新建一条候选）；
③ ①走不成时 Y 视为「没读到配置」，交给既有撤除流程。

边界一个字不许松：``candidate_has_configuration`` 不放宽、价格与排序不碰、
``product_configuration.py`` 那一条线不动。
"""
from backend.services import procurement_research
from backend.services.procurement_research import (
    apply_configuration_model_gate, attribute_configuration, candidate_has_configuration,
    configuration_model_conflict, configuration_model_target, gate_candidate_configuration,
    has_named_parameter_row, record_configuration_model,
    refetch_configuration_from_sources, withdraw_unconfigured_candidates)

#: 402 那一页的配置（第一行就写着**别的型号**）。
WRONG_CONFIGURATION = ('技术参数：\n型号：BXM-170V-MⅡ\n灭菌温度范围：105℃~135℃\n'
                       '灭菌时间范围：0~120min\n灭菌器容积：170L\n')
#: 401 那一页的配置（第一行写着候选自己的型号）。
RIGHT_CONFIGURATION = ('技术参数：\n型号：BXM-145V-MII\n灭菌温度范围：105℃~135℃\n'
                       '灭菌时间范围：0~120min\n灭菌器容积：145L\n')

WRONG_URL = 'http://boxun.com.cn/product-402.html'
RIGHT_URL = 'http://www.boxun.com.cn/product-401.html'
#: 配置页上写着型号的那一行（真实形状取自 ``zgjipad.com`` 那张表：
#: ``| 加工定制：是 | | 品牌：上海旌派 | | 型号：SW-CJ-1D``）。
BRAND_LINE = '| 加工定制：是 | | 品牌：上海博迅 | | 型号：BXM-170V-MⅡ'


def _page(url, body, source_type='manufacturer_website'):
    return {'url': url, 'purpose': 'discovery', 'body_status': 'fetched',
            'source_type': source_type, 'body_text': body}


def _wrong_page():
    return _page(WRONG_URL, 'BXM-145V-MII 立式压力蒸汽灭菌器\n' + BRAND_LINE + '\n' + WRONG_CONFIGURATION)


def _right_page():
    return _page(RIGHT_URL, 'BXM-145V-MII 立式压力蒸汽灭菌器 技术参数\n' + RIGHT_CONFIGURATION)


def _candidate(**overrides):
    candidate = {'model': 'BXM-145V-MII', 'brand': '博迅',
                 'configuration': WRONG_CONFIGURATION,
                 'configuration_source': 'product_page_spec',
                 'configuration_url': WRONG_URL}
    candidate.update(overrides)
    return candidate


def _run(candidate, sources, reference=None, discovered=None):
    reference = list(reference or [])
    return {'sources': sources, 'gaps': [], 'alternatives': [candidate],
            'reference_candidates': reference,
            'discovered_candidates': [candidate] if discovered is None else list(discovered)}


# --------------------------------------------------------- 一、判据本身

def test_a_different_configuration_model_is_a_conflict():
    candidate = _candidate()
    record_configuration_model(candidate, [])
    assert candidate['configuration_model'] == 'BXM-170V-MⅡ'
    assert configuration_model_conflict(candidate)


def test_the_same_model_written_differently_is_not_a_conflict():
    candidate = _candidate(configuration='型号：bxm-145v-mii\n容积：145L\n')
    record_configuration_model(candidate, [])
    assert candidate['configuration_model'] == 'bxm-145v-mii'
    assert not configuration_model_conflict(candidate)


def test_an_unjudged_configuration_model_is_not_a_conflict():
    """判不出（空串）不拦——``设计规范`` 的口径是"判不出就不猜"，不是"判不出就撤"。"""
    candidate = _candidate(configuration='容积：145L\n灭菌温度：105℃\n')
    record_configuration_model(candidate, [])
    assert candidate['configuration_model'] == ''
    assert not configuration_model_conflict(candidate)


def test_a_candidate_without_configuration_is_not_a_conflict():
    assert not configuration_model_conflict({'model': 'BXM-145V-MII'})


# --------------------------------------------------------- 二、产出质量闸

def test_a_reextraction_with_only_values_has_no_parameter_name():
    """``BWS-10`` 那 61 字的真实形状：三行全是值，一个参数名都没有。"""
    values_only = '280×130×150 | 280×220×150\n396×250×260 | 396×330×260\n二孔 | 双列四孔'
    assert has_named_parameter_row(values_only) is False
    assert has_named_parameter_row('280×130×150 | 280×130×150 280×130×150\n396×250×260 | 450×395×260') is False
    assert has_named_parameter_row('') is False
    assert has_named_parameter_row('---\n||\n') is False


def test_a_reextraction_with_parameter_names_passes():
    assert has_named_parameter_row(RIGHT_CONFIGURATION) is True
    assert has_named_parameter_row('技术参数：\n有效容积：145L\n') is True
    assert has_named_parameter_row('型号 技术参数 | BBS-V600\n外形尺寸 | 600×580×1060mm\n') is True
    # 值那一格带 ``×``（``外形尺寸 | 600×580×1060mm``）完全没问题：挡的是**名字那一格**
    # 里的 ``×``（``280×130×150 | 280×220×150``），那说明左格是值、不是参数名。
    assert has_named_parameter_row('内部尺寸 | 580×520×610mm') is True
    assert has_named_parameter_row('580×520×610mm | 600×580×1060mm') is False


def test_a_reextraction_that_fails_the_quality_gate_is_not_adopted(monkeypatch):
    """产出质量闸拦下时：不换页、按"没读到配置"处理，并如实留痕（拦在换页那一步）。"""
    candidate = _candidate(source_pages=[{'url': RIGHT_URL, 'origin': 'discovered', 'primary': False}])
    sources = [_wrong_page(), _right_page()]
    record_configuration_model(candidate, sources)
    run = _run(candidate, sources)
    monkeypatch.setattr(procurement_research, 'has_named_parameter_row', lambda text: False)

    outcome = gate_candidate_configuration(run, candidate)
    assert outcome['action'] == 'dropped'
    assert candidate['configuration'] == ''
    gate = candidate['configuration_model_gate']
    assert gate['relocation_rejected'][0]['url'] == RIGHT_URL
    assert '没有参数名行' in gate['relocation_rejected'][0]['reason']
    # 这一份配置仍然**没有被丢掉**：它归给了它真正的型号（见下一节）。
    assert gate['attributed_to'] == 'BXM-170V-MⅡ'


# --------------------------------------------------------- 三、① 换页

def test_the_gate_switches_to_the_discovered_page_that_names_the_candidate():
    """``final-check`` 那条：402 换成 401，三格一起换，``configuration_model`` 就地重算。"""
    candidate = _candidate(source_pages=[{'url': RIGHT_URL, 'origin': 'discovered', 'primary': False},
                                         {'url': WRONG_URL, 'origin': 'detail_page', 'primary': True}])
    sources = [_wrong_page(), _right_page()]
    record_configuration_model(candidate, sources)
    run = _run(candidate, sources)

    outcome = gate_candidate_configuration(run, candidate)
    assert outcome['action'] == 'relocated'
    assert candidate['configuration_url'] == RIGHT_URL
    assert candidate['configuration'] == RIGHT_CONFIGURATION.strip()
    assert candidate['configuration_source'] == 'manufacturer_spec'
    # 重算：这一份配置所属的型号就是候选自己的型号，「配置所属型号」那一行不再出现。
    assert candidate['configuration_model'] == 'BXM-145V-MII'
    gate = candidate['configuration_model_gate']
    assert gate['action'] == 'relocated'
    assert gate['rejected_url'] == WRONG_URL and gate['used_url'] == RIGHT_URL
    assert gate['rejected_model'] == 'BXM-170V-MⅡ'
    assert gate['rejected_chars'] == len(WRONG_CONFIGURATION)
    # 换页之后，原来那一份照样归给它的型号。
    assert gate['attribution'] in ('handed', 'already', 'created')
    assert gate['attributed_to'] == 'BXM-170V-MⅡ'


def test_a_discovered_page_that_yields_another_model_is_not_adopted():
    """发现页抽出来的也是别的型号 → 不采用（否则只是换到另一台机器上）。"""
    other = _page(RIGHT_URL, 'BXM-145V-MII 立式压力蒸汽灭菌器\n' + WRONG_CONFIGURATION)
    candidate = _candidate(source_pages=[{'url': RIGHT_URL, 'origin': 'discovered', 'primary': False}])
    sources = [other]
    record_configuration_model(candidate, sources)
    run = _run(candidate, sources)

    assert gate_candidate_configuration(run, candidate)['action'] == 'dropped'
    assert candidate['configuration'] == ''
    assert '属于别的型号' in candidate['configuration_model_gate']['relocation_rejected'][0]['reason']


def test_only_a_discovered_page_is_used():
    """别的来路的页（详情页／品牌出处／配置证据）不拿来顶——口径就是"发现它的那一页"。"""
    candidate = _candidate(source_pages=[{'url': RIGHT_URL, 'origin': 'detail_page', 'primary': True}])
    sources = [_right_page()]
    record_configuration_model(candidate, sources)
    run = _run(candidate, sources)

    assert gate_candidate_configuration(run, candidate)['action'] == 'dropped'
    assert candidate['configuration_url'] == ''


# --------------------------------------------------------- 四、② 配置归属转移

def test_the_configuration_is_handed_to_the_candidate_that_owns_it():
    """X 已经在本轮名单里、而且它自己还没有配置 → 这份配置**归给它**。"""
    owner = {'brand': '', 'model': 'BXM-170V-MⅡ'}
    candidate = _candidate(source_pages=[{'url': WRONG_URL, 'origin': 'detail_page', 'primary': True}])
    sources = [_wrong_page()]
    record_configuration_model(candidate, sources)
    run = _run(candidate, sources, reference=[owner])

    outcome = gate_candidate_configuration(run, candidate)
    assert outcome['action'] == 'dropped'
    assert outcome['attribution'] == 'handed'
    assert outcome['target'] is owner
    assert owner['configuration'] == WRONG_CONFIGURATION
    assert owner['configuration_url'] == WRONG_URL
    assert owner['configuration_model'] == 'BXM-170V-MⅡ'
    assert owner['configuration_attributed_from'] == 'BXM-145V-MII'
    # 配置归给了别人，Y 这条按"没读到配置"处理。
    assert candidate['configuration'] == ''


def test_an_existing_configuration_is_never_overwritten():
    """X 本来就有配置时一个字不改（同一份配置归给本来就该有它的候选＝去重）。"""
    owner = {'brand': '博迅', 'model': 'BXM-170V-MⅡ',
             'configuration': '型号：BXM-170V-MⅡ\n容积：170L\n',
             'configuration_url': 'http://other.test/owner'}
    candidate = _candidate(source_pages=[{'url': WRONG_URL, 'origin': 'detail_page', 'primary': True}])
    sources = [_wrong_page()]
    record_configuration_model(candidate, sources)
    run = _run(candidate, sources, reference=[owner])

    outcome = gate_candidate_configuration(run, candidate)
    assert outcome['attribution'] == 'already' and outcome['target'] is owner
    assert owner['configuration'] == '型号：BXM-170V-MⅡ\n容积：170L\n'
    assert owner['configuration_url'] == 'http://other.test/owner'


def test_a_model_only_in_discovered_candidates_counts():
    """三份名单都算：X 只在发现记录里，也算"本轮已经有这个型号"。"""
    owner = {'brand': '', 'model': 'BXM-170V-MⅡ', 'candidate_id': ''}
    candidate = _candidate(source_pages=[{'url': WRONG_URL, 'origin': 'detail_page', 'primary': True}])
    sources = [_wrong_page()]
    record_configuration_model(candidate, sources)
    run = _run(candidate, sources, discovered=[candidate, owner])

    assert configuration_model_target(run, 'BXM-170V-MⅡ')[0] is owner
    outcome = gate_candidate_configuration(run, candidate)
    assert outcome['attribution'] == 'handed' and outcome['target'] is owner
    assert owner['configuration'] == WRONG_CONFIGURATION


def test_a_model_that_is_not_in_the_roster_becomes_a_new_candidate():
    """业务口径：「那不是应该把 BBS-V600 列入候选吗」——清单里没有就按配置页认出的型号新建。"""
    candidate = _candidate(source_pages=[{'url': WRONG_URL, 'origin': 'detail_page', 'primary': True}])
    sources = [_wrong_page()]
    record_configuration_model(candidate, sources)
    run = _run(candidate, sources)

    outcome = gate_candidate_configuration(run, candidate)
    assert outcome['action'] == 'dropped' and outcome['attribution'] == 'created'
    created = outcome['target']
    assert created['model'] == 'BXM-170V-MⅡ'
    assert created['brand'] == '上海博迅'          # 只从配置页正文里读
    assert created['configuration'] == WRONG_CONFIGURATION
    assert created['configuration_url'] == WRONG_URL
    assert created['configuration_model'] == 'BXM-170V-MⅡ'
    assert created['configuration_attributed_from'] == 'BXM-145V-MII'
    assert created['source'] == 'configuration_page_model'
    assert created['reference_reason'] == 'configuration_page_model'
    assert '按配置页上写着的型号补出来' in created['reference_note']
    assert created['candidate_id']                # 卡上的查价按钮要有编号
    assert created['source_pages'] == [{'url': WRONG_URL, 'origin': 'discovered', 'primary': True}]
    # 落到用户看得见的那份名单上，而且**不进核验名额**（只往 reference_candidates 里放）。
    assert created in run['reference_candidates']
    assert created not in run['alternatives']


def test_the_same_model_is_only_created_once_per_run():
    """同一轮里多条候选拿错了同一份配置：只新建一条，其余归给它（去重）。"""
    first = _candidate(source_pages=[{'url': WRONG_URL, 'origin': 'detail_page', 'primary': True}])
    second = _candidate(model='BXM-110VF', source_pages=[{'url': WRONG_URL, 'origin': 'detail_page', 'primary': True}])
    sources = [_wrong_page()]
    for item in (first, second):
        record_configuration_model(item, sources)
    run = {'sources': sources, 'gaps': [], 'alternatives': [first, second],
           'reference_candidates': [], 'discovered_candidates': [first, second]}

    result = apply_configuration_model_gate(run, [first, second])
    assert [c['model'] for c in result['created']] == ['BXM-170V-MⅡ']
    assert len(run['reference_candidates']) == 1
    assert [c.get('configuration_model_gate', {}).get('attribution') for c in (first, second)] \
        == ['created', 'already']


# --------------------------------------------------------- 五、③ 按"没读到配置"处理

def test_without_a_replacement_the_configuration_is_dropped_with_a_trace():
    candidate = _candidate(source_pages=[{'url': WRONG_URL, 'origin': 'detail_page', 'primary': True}])
    sources = [_wrong_page()]
    record_configuration_model(candidate, sources)
    run = _run(candidate, sources)

    gate_candidate_configuration(run, candidate)
    # 三格清空：既有撤除判据（``candidate_has_configuration``）读的就是这三个字段里的
    # 第一个，判据本身一个字没改。
    assert candidate['configuration'] == ''
    assert candidate['configuration_source'] == ''
    assert candidate['configuration_url'] == ''
    assert 'configuration_model' not in candidate
    assert 'configuration_model_basis' not in candidate
    # 跳过 ≠ 丢数据：判断依据留在候选上，技术细节进 failures。
    gate = candidate['configuration_model_gate']
    assert gate['action'] == 'dropped' and gate['rejected_url'] == WRONG_URL
    assert gate['rejected_model'] == 'BXM-170V-MⅡ'
    failure = run['failures'][-1]
    assert failure['code'] == 'candidate_configuration_model_mismatch'
    assert 'BXM-170V-MⅡ' in failure['error'] and 'BXM-145V-MII' in failure['error']
    # 候选自己的型号一个字不改（那是查价的键）。
    assert candidate['model'] == 'BXM-145V-MII'


# --------------------------------------------------------- 六、gaps 人话，各写一次

def test_the_gate_writes_one_human_sentence_per_action_and_only_once():
    moved = _candidate(source_pages=[{'url': RIGHT_URL, 'origin': 'discovered', 'primary': False}])
    lost = _candidate(model='BXM-110VF', configuration='型号：BXM-110VE\n容积：110L\n',
                      source_pages=[{'url': WRONG_URL, 'origin': 'detail_page', 'primary': True}])
    owner = {'brand': '', 'model': 'BXM-110VE'}
    sources = [_wrong_page(), _right_page()]
    run = {'sources': sources, 'gaps': [], 'discovered_candidates': [moved, lost, owner],
           'alternatives': [moved, lost], 'reference_candidates': [owner]}

    result = apply_configuration_model_gate(run, [moved, lost, owner, moved])
    assert [c['model'] for c in result['relocated']] == ['BXM-145V-MII']
    assert [c['model'] for c in result['dropped']] == ['BXM-110VF']
    assert [target['model'] for _source, target in result['handed']] == ['BXM-110VE']
    switch = [gap for gap in run['gaps'] if gap.startswith('配置页型号闸门：')]
    move = [gap for gap in run['gaps'] if gap.startswith('配置页型号闸门（配置归属转移）：')]
    created = [gap for gap in run['gaps'] if gap.startswith('配置页型号闸门（新建候选）：')]
    assert len(switch) == 1 and len(move) == 1 and len(created) == 1
    assert 'BXM-145V-MII' in switch[0] and 'BXM-110VF' in move[0]
    # ``moved`` 原来那一份配置属于 ``BXM-170V-MⅡ``，本轮名单里没有它 → 它被新建出来，
    # 所以第三句话点名的是新候选（而不是 ``owner``：``owner`` 是 ``BXM-110VE``）。
    assert 'BXM-170V-MⅡ' in created[0]
    # 跑第二遍是幂等的：四份清单都空，也不再多写一句话。
    again = apply_configuration_model_gate(run, [moved, lost, owner])
    assert again == {'relocated': [], 'dropped': [], 'handed': [], 'created': []}
    assert len(run['gaps']) == 3


# --------------------------------------------------------- 七、接上既有的撤除流程

def test_a_dropped_candidate_goes_through_the_existing_withdrawal():
    """③ 之后没有配置 → 被既有流程撤下；判据（``candidate_has_configuration``）不放宽。"""
    candidate = _candidate(source_pages=[{'url': WRONG_URL, 'origin': 'detail_page', 'primary': True}])
    sources = [_wrong_page()]
    record_configuration_model(candidate, sources)
    run = _run(candidate, sources)

    apply_configuration_model_gate(run, [candidate])
    assert not candidate_has_configuration(candidate)
    assert withdraw_unconfigured_candidates(run) == [candidate]
    assert run['alternatives'] == []
    # 配置归给的那条新候选还在（它有配置，不被撤）。
    assert [item['model'] for item in run['reference_candidates']] == ['BXM-170V-MⅡ']


def test_a_dropped_candidate_with_readings_keeps_its_card():
    """逐项读数仍然是"有配置"（既有判据原样）——闸门只拿掉它自己那一份配置文本。"""
    candidate = _candidate(config_match={'items': [{'name': '灭菌温度',
                                                    'observed': {'value': '105℃~135℃'}}]},
                           source_pages=[{'url': WRONG_URL, 'origin': 'detail_page', 'primary': True}])
    sources = [_wrong_page()]
    record_configuration_model(candidate, sources)
    run = _run(candidate, sources)

    apply_configuration_model_gate(run, [candidate])
    assert candidate_has_configuration(candidate)
    assert withdraw_unconfigured_candidates(run) == []
    assert run['alternatives'] == [candidate]


def test_the_refetch_does_not_put_the_rejected_page_back():
    """被闸门拦下的那一页，回捞这一步不再试——否则闸门的判断会被反过来推翻。"""
    candidate = _candidate(source_pages=[{'url': WRONG_URL, 'origin': 'detail_page', 'primary': True}])
    sources = [_wrong_page()]
    record_configuration_model(candidate, sources)
    run = _run(candidate, sources)
    apply_configuration_model_gate(run, [candidate])

    assert candidate['configuration_model_gate']['rejected_url'] == WRONG_URL
    spec, attempted = refetch_configuration_from_sources(run, candidate)
    assert spec is None and attempted == []


def test_the_refetched_configuration_also_passes_the_gate():
    """回捞到的这一份如果也不属于候选自己的型号，同样按"没读到配置"处理。"""
    page = _page(RIGHT_URL, 'BXM-145V-MII 立式压力蒸汽灭菌器\n' + WRONG_CONFIGURATION)
    candidate = {'model': 'BXM-145V-MII', 'brand': '博迅',
                 'url': 'https://example.test/notice',
                 'detail_page': {'url': 'https://example.test/notice', 'coverage': 'model_only'},
                 'source_pages': [{'url': 'https://example.test/notice', 'origin': 'discovered',
                                   'primary': True}]}
    run = {'sources': [page], 'gaps': [], 'alternatives': [candidate],
           'reference_candidates': [], 'discovered_candidates': [candidate]}

    assert withdraw_unconfigured_candidates(run) == [candidate]
    assert run['alternatives'] == []
    assert candidate['configuration_model_gate']['action'] == 'dropped'
    # 回捞本身确实试过这一页（留痕留着），只是它抽出来的配置不是这个型号的。
    assert candidate['configuration_refetch']['attempted'] == [RIGHT_URL]
    # 这一份配置同样没有被丢掉：它归给了 BXM-170V-MⅡ（新建的那一条）。
    assert candidate['configuration_model_gate']['attributed_to'] == 'BXM-170V-MⅡ'


def test_attribute_configuration_is_idempotent_for_an_owner_that_already_has_it():
    """直接调归属转移那一层：同一条配置归两次，第二次是 ``already``，配置不动。"""
    owner = {'brand': '', 'model': 'BXM-170V-MⅡ'}
    candidate = _candidate()
    held = {'configuration': WRONG_CONFIGURATION, 'configuration_source': 'product_page_spec',
            'configuration_url': WRONG_URL, 'configuration_model_basis': {}}
    run = {'sources': [_wrong_page()], 'alternatives': [], 'reference_candidates': [owner],
           'discovered_candidates': [owner]}

    assert attribute_configuration(run, candidate, 'BXM-170V-MⅡ', held) == ('handed', owner)
    assert attribute_configuration(run, candidate, 'BXM-170V-MⅡ', held) == ('already', owner)
    assert owner['configuration'] == WRONG_CONFIGURATION
    assert run['reference_candidates'] == [owner]
