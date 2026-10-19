"""差额必须出现在**用户看得见的出口**上：报告正文里正式候选那一段。

上一单把差额写进了 ``config_match['diff_points']``，而它全仓唯一的消费点是
``alternative_report_item(verified=False)`` —— 报告里那一句**只有在这一轮一条正式候选
都没有时**才印得出来（``:5576-5578`` 是 ``or``：有 ``run['alternatives']`` 时只列正式
候选）。正式候选（用户最关心的那批）走 ``matrix`` 那条路，逐项只印"要求 X，查得 Y"，
一个字的差额都没有。

这一组用例钉住三件事：
  1. 正式候选的逐项那一行，判"不满足"时也带上差额（报告文案级断言）；
  2. 算不出来时写"差额：无法比较（原因）"，**不是省略、也不是编一个数**；
  3. 给 ``matrix`` 行挂差额**不改变** ``similarity_assessment`` 的任何输出；
  4. 同一对（要求, 实读）在 **``config_match.items[].shortfall_text``（前端卡片读它）**
     与 **核验矩阵行的 ``shortfall_text``** 上逐字同一句，且 ``soft_score`` 不受影响。
"""
import copy

import pytest

from backend.services.procurement_research import alternative_report_item, _matrix_line_text
from backend.services.procurement_rules import similarity_assessment
from backend.services.research_planning import (configuration_alignment, parameter_shortfall,
                                                shortfall_text)

SPEED = {'name': '最高转速', 'value': 20000, 'unit': 'r/min', 'direction': 'min'}
RCF = {'name': '最大相对离心力', 'value': 25000, 'unit': '×g', 'direction': 'min'}
DETAIL_URL = 'https://www.yiqi.com/product/detail_1000005.html'
EXCERPT = '最高转速 16500 r/min'


def _observed(value=16500.0, unit='r/min', name='最高转速', excerpt=EXCERPT):
    return {'name': name, 'value': value, 'unit': unit, 'conditions': '', 'url': DETAIL_URL,
            'excerpt': excerpt, 'evidence': {'url': DETAIL_URL, 'excerpt': excerpt}}


def _row(required, observed, status):
    row = {'required': required, 'observed': observed, 'status': status}
    if observed:
        row['evidence_status'] = 'confirmed'
    return row


def _candidate(matrix, config_match=None, status='ineligible'):
    return {'brand': '湘仪', 'model': 'H1650',
            'url': 'https://www.yiqi.com/product/detail_1000006.html',
            'excerpt': '高速离心机-湘仪H1650台式高速离心机',
            'matrix': matrix, 'status': status, 'config_match': config_match or {}}


def test_the_report_line_for_a_formal_candidate_now_says_how_far_off_it_is():
    row = _row(SPEED, _observed(), 'violated')
    line = _matrix_line_text(row)
    # 老句子还在（要求/查得/不满足），后面多一句差额。
    assert '要求 20000 r/min，查得 16500.0；不满足' in line
    assert '差额：实读比要求低 17.5%（以要求值为基准）。' in line


def test_a_formal_candidate_that_meets_the_requirement_gets_no_shortfall_sentence():
    row = _row(SPEED, _observed(value=21000.0), 'satisfied')
    line = _matrix_line_text(row)
    assert line.endswith('满足')
    assert '差额' not in line


def test_a_shortfall_that_cannot_be_computed_is_spelled_out_not_omitted():
    # 要求值为 0（没有可作分母的基准）：这一项照样判"不满足"，差额必须写"无法比较"
    # 并给出原因——省略掉，读者就分不清"没差多少这回事"还是"没算"。
    zero = {'name': '残留量', 'value': 0, 'unit': 'mL', 'direction': 'max'}
    row = _row(zero, _observed(value=5.0, unit='mL', name='残留量', excerpt='残留量 5 mL'),
               'violated')
    line = _matrix_line_text(row)
    assert '不满足' in line
    assert '差额：无法比较' in line
    assert '没有可作分母的基准' in line


def test_the_formal_candidate_section_prints_the_report_text_with_the_shortfall():
    candidate = _candidate([_row(SPEED, _observed(), 'violated'),
                            _row(RCF, _observed(value=26054.0, unit='×g', name='最大相对离心力'),
                                 'satisfied'),
                            _row({'name': '类型', 'value': '高速冷冻离心机', 'direction': 'equal'},
                                 None, 'unknown')],
                           status='ineligible')
    item = alternative_report_item(candidate, verified=True)
    assert item['label'] == '湘仪 H1650'
    assert '关键参数不满足' in item['text']
    assert '差额：实读比要求低 17.5%（以要求值为基准）。' in item['text']


def test_the_formal_candidate_section_falls_back_to_what_the_detail_page_said():
    # 2026-09-28 真跑实测的形状：正式候选的 matrix 三行全是"待核实"、observed 全是 None，
    # 而发现阶段从详情页正文里读到了值并算出了差额。核验那条路一条可比结论都没有时，
    # 把发现阶段读到的逐项结果（含差额）附上——否则正式候选这一段对用户是空的。
    points = ['最高转速：要求 20000 r/min，正文查得 16500 r/min，不满足。'
              '差额：实读比要求低 17.5%（以要求值为基准）。']
    candidate = _candidate([_row(SPEED, None, 'unknown'), _row(RCF, None, 'unknown'),
                            _row({'name': '类型', 'value': '高速冷冻离心机', 'direction': 'equal'},
                                 None, 'unknown')],
                           config_match={'diff_points': points}, status='needs_review')
    text = alternative_report_item(candidate, verified=True)['text']
    assert '核验阶段没读到可判定的参数值' in text
    assert '详情页正文里读到的配置对齐' in text
    assert '差额：实读比要求低 17.5%（以要求值为基准）。' in text


def test_the_fallback_is_not_added_when_the_verification_did_produce_a_verdict():
    # 核验已经给出结论时不再并排印一套发现阶段的数字：两条路的来源与判据不同，
    # 并排会变成同一份报告两套数字，读者不知道信哪个。
    candidate = _candidate([_row(SPEED, _observed(), 'violated')],
                           config_match={'diff_points': ['最高转速：…不满足。差额：…']},
                           status='ineligible')
    text = alternative_report_item(candidate, verified=True)['text']
    assert '详情页正文里读到的配置对齐' not in text


def test_the_fallback_is_not_invented_when_there_is_nothing_to_attach():
    candidate = _candidate([_row(SPEED, None, 'unknown')], status='needs_review')
    text = alternative_report_item(candidate, verified=True)['text']
    assert '配置对齐' not in text
    assert '差额' not in text, '没有可比的一对值时不许编一句差额，也不许留个空壳'


def test_the_reference_candidate_section_still_prints_the_diff_points():
    # 既有出口不许被这次改动碰坏：没进名额的参考候选照旧印 config_match.diff_points。
    candidate = {'brand': '', 'model': 'FC5718', 'url': 'https://example.com/fc5718',
                 'excerpt': 'FC5718高速离心机', 'reference_reason': 'brand_unresolved',
                 'config_match': {'diff_points': ['最高转速：要求 20000 r/min，正文查得 18000 r/min，'
                                                  '不满足。差额：实读比要求低 10.0%（以要求值为基准）。']}}
    text = alternative_report_item(candidate, verified=False)['text']
    assert '本条未进入本轮跨品牌核验名额' in text
    assert '正文里读到的配置对齐' in text
    assert '差额：实读比要求低 10.0%（以要求值为基准）。' in text


def test_attaching_the_shortfall_to_matrix_rows_does_not_change_the_verdict():
    # 生产代码在 similarity_assessment 之后才给不满足的行挂 shortfall/shortfall_text。
    # 这里正反各算一遍：挂键前后的输出必须逐字相同（差额不参与任何判定）。
    rows = [_row(SPEED, _observed(), 'violated'),
            _row(RCF, _observed(value=26054.0, unit='×g', name='最大相对离心力'), 'satisfied'),
            _row({'name': '类型', 'value': '高速冷冻离心机', 'direction': 'equal'}, None, 'unknown')]
    before = similarity_assessment(copy.deepcopy(rows), confirmed_parameter_count=3)
    for row in rows:
        if row['status'] == 'violated':
            info = parameter_shortfall(row['required'], row['observed'])
            row['shortfall'] = info
            row['shortfall_text'] = shortfall_text(info)
    after = similarity_assessment(rows, confirmed_parameter_count=3)
    assert before == after
    assert rows[0]['status'] == 'violated', '挂差额不许改状态'


def test_the_report_item_does_not_mutate_the_candidate_it_is_given():
    # 报告是纯读路径：印一遍报告不许改 run 里的候选（否则前端/库里的数据会被顺手改掉）。
    candidate = _candidate([_row(SPEED, _observed(), 'violated')], status='ineligible')
    snapshot = copy.deepcopy(candidate)
    alternative_report_item(candidate, verified=True)
    assert candidate == snapshot


# ---------------------------------------------------------------------------
# 两条路同一句话：``config_match.items[].shortfall_text``（前端卡片读它）与
# ``matrix`` 行的 ``shortfall_text``（核验那条路）必须逐字相同。
# ---------------------------------------------------------------------------
DELTA_URL = 'https://www.yiqi.com/product/detail_1000005.html'


def _alignment(requirements, text='H1650 最高转速 16500 r/min', model='H1650'):
    source = {'url': DELTA_URL, 'body_status': 'fetched', 'body_text': text,
              'source_type': 'unknown'}
    return configuration_alignment({'model': model, 'url': DELTA_URL}, [source], requirements)


def test_the_item_and_the_matrix_row_carry_the_same_sentence():
    result = _alignment([SPEED])
    item = result['violated'][0]
    # 这一对值在核验那条路上会挂到 matrix 行上的，正是 shortfall_text(info)：
    # 报告行的末尾那一句就是它（``_matrix_line_text`` 是核验段的排版）。
    sentence = shortfall_text(parameter_shortfall(SPEED, item['observed']))
    assert item['shortfall_text'] == sentence
    assert sentence == '差额：实读比要求低 17.5%（以要求值为基准）。'
    row = _row(SPEED, item['observed'], 'violated')
    assert _matrix_line_text(row).endswith(sentence)


def test_the_item_still_says_it_cannot_compare_instead_of_leaving_it_blank():
    # 算不出来时（要求值为 0，没有分母）那一格写"无法比较"并给原因，
    # **不是空串、也不是编一个数**——前端拿到空串就什么都不显示，等于没修。
    zero = {'name': '最高转速', 'value': 0, 'unit': 'r/min', 'direction': 'max'}
    result = _alignment([zero], 'H1650 最高转速 5 r/min')
    item = result['violated'][0]
    assert item['shortfall']['comparable'] is False
    expected = '差额：无法比较（要求值是 0 或负数，没有可作分母的基准，无法比较）。'
    assert item['shortfall_text'] == expected


def test_the_shortfall_text_on_items_does_not_change_soft_score_or_the_verdict():
    # 一满足（21000 ≥ 20000）一不满足（20000 < 25000）：soft_score 仍只由满足项决定，
    # score / hard_failures 与改动前逐字相同，不满足那一项只是多一句差额。
    result = _alignment([SPEED, RCF],
                        'H1650 最高转速 21000 r/min\nH1650 最大相对离心力 20000 ×g')
    assert result['soft_score'] == pytest.approx(1 / 1.05)
    assert result['score'] == pytest.approx(0.5)
    assert result['hard_failures'] == ['最大相对离心力']
    assert result['items'][0].get('shortfall_text') is None, '满足项不带差额'
    assert result['items'][1]['shortfall_text'] == '差额：实读比要求低 20.0%（以要求值为基准）。'


def test_a_candidate_that_only_fails_keeps_soft_score_none_with_a_sentence_attached():
    # 只有不满足项时 soft_score 照旧是 None（不把不满足揉进相似度），差额只是附加信息。
    result = _alignment([SPEED])
    assert result['soft_score'] is None
    assert result['score'] == 0.0
    assert result['hard_failures'] == ['最高转速']
    assert result['items'][0]['shortfall_text'].startswith('差额：实读比要求低 17.5%')
    assert result['shortfalls'][0]['percent'] == pytest.approx(17.5)

