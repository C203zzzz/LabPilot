"""候选品结论：取到该型号的配置就说"配置与样品相当"，不再印"待核实"。

设计规范：「尽量让正确的候选品的配置通过闸口，确保正确，但不用注明
未核实等字样，容易给用户一种不可信的感觉；只要抓到候选品型号、且候选品相关网页有
对应型号的配置就可以通过——因为前面就是根据配置定位到该型号的，所以网页中大概率
是有相关配置的」，以及「候选品的配置就是在用户提供的样品配置的左右即可」。

两条边界写在测试里：
* 说"配置与样品相当"，**不说"满足"**——网页上有配置只证明型号对得上，参数值达不
  达标要看参数表自己，写成满足等于替用户下了他没下的结论；
* 没取到配置时也不印"待核实"，只陈述"尚未取到该型号的产品参数"。
"""
from backend.services.procurement_rules import alternative_conclusion

CONFIGURATION = '技术参数 浴槽容积 3.5L 工作温度 室温~100℃ 温度稳定度 ±0.5℃'


def candidate(**overrides):
    return {'status': 'unknown', 'model': 'BW-10H', **overrides}


def test_a_candidate_with_its_own_configuration_is_called_comparable():
    text = alternative_conclusion({}, [], candidate(configuration=CONFIGURATION))

    assert text == '同档候选：配置与样品相当，产品参数见下'
    assert '待核实' not in text
    assert '满足' not in text


def test_a_candidate_without_configuration_states_the_absence_plainly():
    text = alternative_conclusion({}, [], candidate())

    assert text == '同档候选：尚未取到该型号的产品参数'
    assert '核实' not in text


def test_an_eligible_candidate_without_a_current_price_says_so_plainly():
    text = alternative_conclusion({}, [], candidate(status='eligible'))

    assert text == '同档；暂无成交价记录'
    assert '待核实' not in text


def test_a_historical_price_line_no_longer_says_pending_verification():
    candidate_row = candidate(status='eligible', prices=[{'level': 'historical_configuration'}])
    text = alternative_conclusion({}, [], candidate_row)

    assert text == '同档；已有历史价格参考，暂无当前成交价'
    assert '待核实' not in text
