"""型号闸门**不许**放宽：把"近似"和"根本不在"分开，但不让近似进门。

``docs/89`` 的猜想是「入口用字符串一刀切，所以像 ``SJ-LS-1300A2`` 这种写法收不
进来」。这一条在隔离库上不成立：

* ``contains_model`` 本来就吃掉分隔符/大小写/空格（``1300 A2``↔``1300A2`` = True、
  ``XKA 2200``↔``XKA-2200`` = True）；它**不**吃掉厂商代码——``SJ-LS-1300A2`` 对
  ``1300A2`` 在型号边界收紧（``docs/185`` §2.1，落地见 ``docs/DRAFT-model-tighten.md``）
  之后不再命中，因为 ``SJ-LS-1300A2`` 是上海上净的整机型号、厂商代码 ``SJ-LS`` 与目标
  数字段粘在一起，与目标不是同一台机器，
* 590 条有正文来源里，白名单内被型号闸门挡住的 135 条，
  **换成"去分隔符后包含"这一档激进放宽，能多救回 0 条**；
* 把放宽口径放到全库，一共只多收 3 条，而且 3 条全错：
  ``Cary 6000i`` 被当成目标 ``Cary 60``，另外两条是聚合站单页行。

所以这里的修法不是"放宽"，是把"压根不在正文里"和"正文里是一个兄弟型号"
分开记录——后者才是用户会追问的那种"是不是漏了"。闸门本身逐字不动。
"""
import pytest

from backend.services.price_rejections import (
    SKIP_MODEL_NOT_IN_BODY, model_match_signal, price_source_candidates,
)
from backend.services.procurement_rules import contains_model


@pytest.mark.parametrize('body,model', [
    # 真实公告写法：分隔符变体，本来就该 exact
    ('1300 A2 生物安全柜', '1300A2'),
    ('XKA 2200 离心机', 'XKA-2200'),
    ('品牌：湘仪\n规格型号：XKA-2200\n数量：1\n单价(元)：17480', 'XKA-2200'),
    ('LEICA DMI1倒置显微镜项目', 'DMi1'),
    ('DW-86L578s 超低温冰箱', 'DW-86L578S'),
])
def test_the_spellings_that_must_be_accepted_are_already_exact(body, model):
    """这些写法**不需要**放宽就已经过闸门——放宽的理由因此不成立。

    这里刻意**不含** ``SJ-LS-1300A2``：厂商代码与目标数字段粘在一起的那种形状
    （``SJ-LS-1300A2``／``X1300A2``）在型号边界收紧之后不再命中，而这是
    ``docs/185`` §2.1 的判定（上海上净的整机型号）。它的断言在
    :func:`test_the_example_docs_89_gave_is_now_refused` 里。
    """
    assert contains_model(body, model) is True
    assert model_match_signal(body, model) == 'exact'


@pytest.mark.parametrize('body,model', [
    ('SPX-150BIII 生化培养箱', 'SPX-150B'),
    ('XKA-2200A 离心机', 'XKA-2200'),
    ('H1850R 离心机', 'H1850'),
    ('中标 | 泰斯特 | SPX-150BIII | 6.0000(台套) | 6,800.0000', 'SPX-150B'),
    ('Varian 的 Cary 6000i', 'Cary 6000'),          # 同一串前缀、尾巴多一位
])
def test_a_near_miss_is_reported_as_near(body, model):
    """``near`` 的用途是回答"这份公告是不是被型号写法挡了"。"""
    assert contains_model(body, model) is False
    assert model_match_signal(body, model) == 'near'


def test_a_vendor_prefixed_sibling_is_not_a_near_miss():
    """``TBSPX150BIII`` 对 ``SPX-150B`` 报 ``None``，是**有意**的。

    厂商代码粘在型号前面时，只看字符串分不清 ``TB`` 是厂商代码还是另一台机器的
    型号号段（``X1300A2`` 就是同一种形状，而它确实是另一台）。隔离库里那两条
    ``tbspx150biii`` 的行本来也被来源类型挡着，不靠这一条救。宁少勿错。
    """
    body = '中标 | 泰斯特 | TBSPX150BIII | 6.0000'
    assert contains_model(body, 'SPX-150B') is False
    assert model_match_signal(body, 'SPX-150B') is None


def test_the_example_docs_89_gave_is_now_refused():
    """``SJ-LS-1300A2`` 对 ``1300A2``：``docs/89`` 当它是 exact，``docs/185`` §2.1 判它错配。

    以 ``docs/185`` 为准（本题目的权威量测）：``SJ-LS-1300A2`` 是上海上净的整机型号，
    ``SJ-LS`` 是厂商代码、与目标数字段粘在一起，``BSC-1300A2``／``BHC-1300A2``／
    ``ZSC-1300A2``／``CBHC-1300A2`` 同形（都是别家机器）。型号边界收紧后它不再命中，
    信号报 ``None``——与 ``X1300A2`` 同样是"前缀粘连、字符串分不清是厂商代码还是
    别台机器"的形状，所以不进 ``near``。
    """
    assert contains_model('SJ-LS-1300A2 生物安全柜', '1300A2') is False
    assert model_match_signal('SJ-LS-1300A2 生物安全柜', '1300A2') is None


@pytest.mark.parametrize('body,model', [
    ('1306A2 生物安全柜', '1300A2'),               # 中间数字不同
    ('X1300A2 生物安全柜', '1300A2'),              # 直接拼接不同编号
    ('SPX-150B 生化培养箱', 'SPX-150BIII'),        # 正文写短、目标写长：见下方说明
    ('Varian 的 Cary 6000i(175～1800nm)', 'Cary 60'),   # 全库里被放宽误收的那条
    ('这是一页与仪器无关的页面', 'XKA-2200'),
    ('', 'XKA-2200'),
])
def test_a_different_number_is_not_called_a_near_miss(body, model):
    """**关键断言**：``near`` 不许把这些说成"差一点就是你的型号"。

    ``1306A2`` 与 ``X1300A2`` 是不同型号；``Cary 6000i`` 对 ``Cary 60``
    是全库里唯一一条"放宽口径会误收"的真实例子（``output/q2_relax_probe.txt``），
    必须仍然是"不在正文里"。
    """
    assert contains_model(body, model) is False
    assert model_match_signal(body, model) is None


def test_a_body_naming_the_shorter_sibling_is_not_a_near_miss():
    """目标写长、正文写短：``None``，是**有意**的。

    目标 ``SPX-150BIII``、正文只写 ``SPX-150B`` 时，读公告的人看到的是另一个型号，
    就算报出来这份公告也仍然要拒；而把它认成 near 会让 ``Cary 60`` 对
    ``Cary 6000i`` 也成立。宁少勿错。
    """
    assert contains_model('SPX-150B 生化培养箱', 'SPX-150BIII') is False
    assert model_match_signal('SPX-150B 生化培养箱', 'SPX-150BIII') is None


def test_a_table_row_naming_a_sibling_is_a_near_miss():
    """真实公告形状：``… | 泰斯特 | SPX-150BIII | 6.0000(台套) |``。

    这是 ``task-<run-7>`` 在 ``zgggzy.com`` 上的那一行。目标 ``某型号``
    被闸门拒（正文写的是带后缀的兄弟型号），``near`` 就是为这种行准备的。
    """
    body = '中标 | 其他仪器仪表 | 生化培养箱 | 泰斯特 | SPX-150BIII | 6.0000(台套) | 6,800.0000'
    assert contains_model(body, 'SPX-150B') is False
    assert model_match_signal(body, 'SPX-150B') == 'near'


def test_the_gate_itself_is_unchanged():
    """闸门的判据必须还是那一个函数，不许被本模块换掉。"""
    assert contains_model('SPX-150BIII 培养箱', 'SPX-150B') is False
    assert contains_model('SPX-150B 培养箱', 'SPX-150BIII') is False
    assert contains_model('1306A2 柜', '1300A2') is False


def test_non_strings_fail_closed():
    assert model_match_signal(None, 'XKA-2200') is None
    assert model_match_signal('XKA-2200', None) is None
    assert model_match_signal(123, 'XKA-2200') is None


def test_the_trace_says_which_kind_of_model_miss_it_was():
    sources = [
        {'url': 'http://a.com/sibling', 'body_status': 'fetched',
         'source_type': 'tender_document', 'body_text': 'SPX-150BIII'},
        {'url': 'http://b.com/unrelated', 'body_status': 'fetched',
         'source_type': 'tender_document', 'body_text': '与仪器无关的页面'},
    ]
    selected, trace = price_source_candidates(sources, {'model': 'SPX-150B'})

    assert selected == []
    assert [item['skip_reason'] for item in trace] == [SKIP_MODEL_NOT_IN_BODY] * 2
    assert [item['model_signal'] for item in trace] == ['near', None]
