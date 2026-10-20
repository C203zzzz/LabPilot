"""配置比配置：硬约束 + 软排序 + 缺参降级 + 差额。

这一组用例守住四件事：
  1. **缺参数绝不能当成满足**（这是稀疏数据上"稳定找到替代"的关键——把"没查到"
     当成"满足"是撒谎，当成"不满足"则永远找不到替代）；
  2. 硬约束只由"正文里读到数值且确实不满足"触发，缺项不触发；
  3. 一项都没读到时不编相似度（``soft_score`` 是 None，不是 0）；
  4. 读到值但**不满足**的项另外给一句"差多少"（差额，基准是要求值），算不出来就说
     "无法比较"，且差额**不进** ``soft_score``、不改任何既有判定。
"""
import pytest

from backend.services.procurement_rules import evaluate_parameter
from backend.services.research_planning import configuration_alignment, parameter_shortfall


def _source(url, text):
    return {'url': url, 'body_status': 'fetched', 'body_text': text, 'source_type': 'unknown'}


SPEED = [{'name': '最高转速', 'value': 20000, 'unit': 'r/min', 'direction': 'min'}]
URL = 'https://a.example/x20r'


def _running(text, requirements=None):
    # ``[]`` 是"本轮没有确认配置要求"，与"没传（用默认那一条）"必须分开。
    return configuration_alignment({'model': 'X20R', 'url': URL},
                                   [_source(URL, text)], SPEED if requirements is None else requirements)


def test_a_measured_value_that_meets_the_requirement_is_a_match():
    result = _running('X20R 台式高速冷冻离心机 最高转速 21000 r/min')
    assert [item['kind'] for item in result['items']] == ['match']
    assert result['satisfied'][0]['observed']['value'] == 21000
    assert result['hard_failures'] == []
    assert result['confidence'] == 1.0
    # 超出要求越多，软相似度越低：21000 比 20000 高 5%，接近程度 1/1.05。
    assert result['soft_score'] == pytest.approx(1 / 1.05)


def test_a_measured_value_below_the_floor_is_a_hard_failure_and_says_why():
    result = _running('X20R 最高转速 15000 r/min')
    assert [item['kind'] for item in result['items']] == ['different']
    assert result['hard_failures'] == ['最高转速']
    assert result['score'] == 0.0
    assert '不满足' in result['diff_points'][0]


def test_a_missing_measurement_is_unknown_and_never_counts_as_satisfied():
    result = _running('X20R 台式高速冷冻离心机 转速可调')
    assert result['satisfied'] == []
    assert result['violated'] == [], '没读到不等于不满足——不能反过来判死'
    assert [item['kind'] for item in result['items']] == ['unknown']
    assert result['hard_failures'] == []
    # 一项都没读到：不编相似度，但置信度如实降到 0。
    assert result['soft_score'] is None
    assert result['score'] is None
    assert result['confidence'] == 0.0
    assert '没查到' in result['diff_points'][0]


def test_a_non_numeric_requirement_matches_by_wording_and_is_unknown_when_absent():
    requirements = [{'name': '类型', 'value': '高速冷冻离心机', 'direction': 'equal'}]
    hit = _running('X20R 高速冷冻离心机 台式', requirements)
    assert [item['kind'] for item in hit['items']] == ['match']
    miss = _running('X20R 台式离心机', requirements)
    assert [item['kind'] for item in miss['items']] == ['unknown']
    assert miss['violated'] == [], '正文没提同类说法不等于它不具备'


def test_a_condition_that_is_not_in_the_text_is_unknown_not_satisfied():
    requirements = [{'name': '最大相对离心力', 'value': 30000, 'unit': '×g',
                     'direction': 'min', 'conditions': '角转子'}]
    # 整机最大值不能冒充"角转子下"的值：正文没写条件，这一项就是没核到。
    result = _running('X20R 最大离心力 40000 ×g', requirements)
    assert [item['kind'] for item in result['items']] == ['unknown']
    # 正文里写明了条件才算核到。
    written = _running('X20R 角转子 最大离心力 40000 ×g', requirements)
    assert [item['kind'] for item in written['items']] == ['match']


def test_a_unit_of_another_kind_is_unknown_not_a_mismatch():
    result = _running('X20R 最高转速 21000 mL')
    assert [item['kind'] for item in result['items']] == ['unknown']


def test_without_confirmed_requirements_there_is_nothing_to_align():
    result = _running('X20R 最高转速 21000 r/min', [])
    assert result['score'] is None and result['soft_score'] is None
    assert result['confidence'] is None
    assert result['basis'] == 'no_confirmed_configuration'
    assert '没有确认配置要求' in result['diff_points'][0]


def test_another_products_numbers_are_never_attributed_to_this_candidate():
    # 同一页面上另一台仪器的转速，不能算到这个候选头上。
    result = _running('X20R 台式离心机\nY30 最高转速 30000 r/min')
    assert [item['kind'] for item in result['items']] == ['unknown']
    assert result['satisfied'] == []


def test_evidence_points_at_the_page_and_the_exact_line():
    result = _running('X20R 最高转速 21000 r/min')
    excerpt = result['satisfied'][0]['observed']['evidence']['excerpt']
    assert excerpt == 'X20R 最高转速 21000 r/min'
    assert result['satisfied'][0]['observed']['evidence']['url'] == URL


# ---------------------------------------------------------------------------
# 差额（"差多少"）：只是附加信息，判定与 soft_score 的口径一个字不改。
# 真实落盘 run（2026-09-28 真跑）里 H1650
# 那一项的原样 required/observed：要求最高转速 ≥20000 r/min、详情页读到 16500 r/min。
# 下面的 17.5% 就是拿这一对实测定下来的（(20000−16500)/20000）。
# ---------------------------------------------------------------------------
REAL_VIOLATED = (
    {'name': '最高转速', 'value': 20000, 'unit': 'r/min', 'direction': 'min'},
    {'value': 16500.0, 'unit': 'r/min',
     'evidence': {'url': 'https://www.yiqi.com/product/detail_1000005.html',
                  'excerpt': '最高转速 16500 r/min'}})


def test_a_measured_value_below_the_floor_also_says_how_far_off_it_is():
    result = _running('X20R 最高转速 16500 r/min')
    assert result['hard_failures'] == ['最高转速'], '不满足的判定本身不许变'
    assert result['shortfalls'][0]['name'] == '最高转速'
    assert result['shortfalls'][0]['percent'] == pytest.approx(17.5)
    assert result['shortfalls'][0]['side'] == 'below'
    # "不满足"那句话照旧，后面多一句差额（报告正文就是拼这些句子）。
    assert '不满足' in result['diff_points'][0]
    assert '17.5%' in result['diff_points'][0]
    # 差额也挂在这一项自己身上，卡片/前端要逐项显示时不必再解析文案。
    assert result['items'][0]['shortfall']['percent'] == pytest.approx(17.5)


def test_the_shortfall_divides_by_the_requirement_not_by_the_measurement():
    # 同一条要求、实读 10000：按"要求值做分母"是 50%；若错用实读值做分母会得到 100%。
    result = _running('X20R 最高转速 10000 r/min')
    assert result['shortfalls'][0]['percent'] == pytest.approx(50.0)
    assert '50.0%' in result['diff_points'][0]


def test_an_upper_bound_shortfall_counts_how_much_it_overshoots():
    # "只有上限没有下限"：要求 ≤50 dB，实读 60 dB ⇒ 实读比要求高 20%。
    requirements = [{'name': '噪声', 'value': 50, 'unit': 'dB', 'direction': 'max'}]
    result = _running('X20R 噪声 60 dB', requirements)
    assert result['hard_failures'] == ['噪声']
    shortfall = result['violated'][0]['shortfall']
    assert shortfall['comparable'] is True
    assert shortfall['side'] == 'above'
    assert shortfall['percent'] == pytest.approx(20.0)
    assert '高 20.0%' in result['diff_points'][0]


def test_another_spelling_of_the_same_unit_still_gets_a_shortfall():
    # 单位写法不一致（要求 r/min、页面写 rpm）在真跑里是常态（live-extract7 的 C1450
    # 与 FC5714 都读出 rpm）：同一量纲照样给差额，不许因为写法不同就退回"无法比较"。
    result = _running('X20R 最高转速 14500 rpm')
    assert result['shortfalls'][0]['percent'] == pytest.approx(27.5)
    assert '27.5%' in result['diff_points'][0]


def test_units_are_converted_before_the_percentage_is_taken():
    # 要求 2 L（下限）、实读 1500 mL：先换算到同一量纲（2000 与 1500）再算，差 25%。
    # 不换算就会算出 749 倍这种荒唐数。
    requirements = [{'name': '容量', 'value': 2, 'unit': 'L', 'direction': 'min'}]
    result = _running('X20R 容量 1500 mL', requirements)
    shortfall = result['violated'][0]['shortfall']
    assert shortfall['required_value'] == pytest.approx(2000.0)
    assert shortfall['observed_value'] == pytest.approx(1500.0)
    assert shortfall['percent'] == pytest.approx(25.0)


def test_a_zero_requirement_has_no_denominator_and_says_so_instead_of_guessing():
    # 除零反例：要求 ≤0、实读 5 ⇒ 确实不满足，但"差了百分之多少"没有分母。
    # 这时必须说"无法比较"，不许拿 0 当分母算出一个数（也不许写无穷大）。
    requirements = [{'name': '最高转速', 'value': 0, 'unit': 'r/min', 'direction': 'max'}]
    result = _running('X20R 最高转速 5 r/min', requirements)
    assert result['hard_failures'] == ['最高转速'], '要求是 0 也不影响"不满足"这个判定'
    assert result['violated'][0]['shortfall']['comparable'] is False
    assert result['violated'][0]['shortfall']['percent'] is None
    assert result['shortfalls'] == [], '算不出来的不进差额清单'
    assert '无法比较' in result['diff_points'][0]
    assert '0' in result['violated'][0]['shortfall']['reason'], '要说清为什么算不出来'


def test_a_shortfall_too_small_to_round_to_one_decimal_is_not_reported_as_zero():
    # 0.01% 也是差。四舍五入成 "0.0%" 读起来像"没差"，宁可写"不到 0.1%"。
    result = _running('X20R 最高转速 19998 r/min')
    assert result['shortfalls'][0]['percent'] == pytest.approx(0.01)
    assert '不到 0.1%' in result['diff_points'][0]
    assert '0.0%' not in result['diff_points'][0]


def test_the_same_input_gives_the_same_shortfall_every_time():
    first = _running('X20R 最高转速 16500 r/min')
    second = _running('X20R 最高转速 16500 r/min')
    assert first['shortfalls'] == second['shortfalls']
    assert first['diff_points'] == second['diff_points']


def test_a_satisfied_item_carries_no_shortfall():
    result = _running('X20R 最高转速 21000 r/min')
    assert result['shortfalls'] == []
    assert 'shortfall' not in result['items'][0]
    # 满足项照旧算软分（超出要求 5% ⇒ 1/1.05），差额一个字都没插进来。
    assert result['soft_score'] == pytest.approx(1 / 1.05)


def test_a_quantity_of_another_kind_is_unknown_and_gets_no_shortfall():
    # 无法判定项：单位是另一量纲 ⇒ 判 unknown（既不算满足也不算不满足），自然也没有差额。
    result = _running('X20R 最高转速 21000 mL')
    assert [item['kind'] for item in result['items']] == ['unknown']
    assert result['shortfalls'] == []
    assert 'shortfall' not in result['items'][0]
    assert '无法比较' in result['diff_points'][0]


def test_the_shortfall_never_enters_soft_score_or_the_verdict():
    # 一满足（21000 ≥ 20000）一不满足（25000 < 30000）：soft_score 只由满足那一项决定，
    # score/hard_failures 与改动前逐字相同，不满足那一项只是多出一句差额。
    requirements = [{'name': '最高转速', 'value': 20000, 'unit': 'r/min', 'direction': 'min'},
                    {'name': '最大相对离心力', 'value': 30000, 'unit': '×g', 'direction': 'min'}]
    result = _running('X20R 最高转速 21000 r/min\nX20R 最大相对离心力 25000 ×g', requirements)
    assert result['soft_score'] == pytest.approx(1 / 1.05)
    assert result['score'] == pytest.approx(0.5)
    assert result['hard_failures'] == ['最大相对离心力']
    assert [item['kind'] for item in result['items']] == ['match', 'different']
    assert len(result['shortfalls']) == 1
    assert result['shortfalls'][0]['name'] == '最大相对离心力'
    assert result['shortfalls'][0]['percent'] == pytest.approx(100.0 / 6)


def test_the_shortfall_function_refuses_to_compare_what_it_cannot_compare():
    finite = {'name': 'x', 'value': 10, 'unit': 'r/min', 'direction': 'min'}
    for required, observed in (
            (finite, None),                                            # 实读值缺失
            ({**finite, 'value': [10, 20], 'direction': 'covers'},
             {'value': 5, 'unit': 'r/min'}),                           # 区间型要求
            ({**finite, 'direction': None}, {'value': 5, 'unit': 'r/min'}),   # 没有方向
            (finite, {'value': 5, 'unit': 'mL'}),                      # 量纲不同
            (finite, {'value': 5, 'unit': '只'}),                      # 单位不认识
            ({**finite, 'value': -5}, {'value': 5, 'unit': 'r/min'}),   # 参考值不是正数
    ):
        info = parameter_shortfall(required, observed)
        assert info['comparable'] is False, (required, observed)
        assert info['percent'] is None
        assert info['reason'], '说"无法比较"必须给出原因，不能只说一句不行'


def test_the_shortfall_function_is_stable_on_the_real_measured_pair():
    # 用真实落盘 run 里 H1650 那一对的原始 required/observed 复算：判定仍由
    # evaluate_parameter 说了算（'violated'），差额是 17.5%，重复调用逐字相同。
    required, observed = REAL_VIOLATED
    assert evaluate_parameter(required, observed) == 'violated'
    first = parameter_shortfall(required, observed)
    second = parameter_shortfall(required, observed)
    assert first == second
    assert first['percent'] == pytest.approx(17.5)
    assert first['side'] == 'below'
    assert first['basis'] == 'requirement_value'

