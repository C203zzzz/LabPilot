"""Evidence and comparability gates shared by both procurement lanes."""
from __future__ import annotations

import math
import re
import unicodedata
from datetime import date
from backend.services.brand_model_rules import (
    BRAND_LABELS,
    BRAND_NOISE,
    amount as _hardened_amount,
    brand_from_enterprise_name as _hardened_brand_from_enterprise_name,
    brand_from_source_line as _hardened_brand_from_source_line,
    brand_identity as _hardened_brand_identity,
    brand_name_matches as _hardened_brand_name_matches,
    contains_model as _hardened_contains_model,
    enterprise_names_in_text as _hardened_enterprise_names_in_text,
    identity as _hardened_identity,
    looks_like_brand_value as _hardened_looks_like_brand_value,
    normalize_brand_value as _hardened_normalize_brand_value,
    row_cells as _hardened_row_cells,
    same_model_family as _hardened_same_model_family,
    supported as _hardened_supported,
)

#: How far apart two same-model historical prices may be before the system
#: refuses to print a min-max band. Every comparable carries a ``configuration``
#: gap, so a wide spread is evidence that the two machines are not the same
#: build -- the observed case was 35,200 against 186,000 (5.3x), where the high
#: record turned out to be a losing candidate's bid for a different
#: ("大容量") line item. A band asserts a comparability the evidence lacks.
DISPERSION_REFUSAL_RATIO = 2.0

#: Why a candidate price was refused, as decided by the predicate that actually
#: stopped :func:`validate_price`. These constants live next to the gate rather
#: than in the explainer so there is exactly one source of truth;
#: ``price_rejections`` imports and re-exports the same names.
#:
#: They used to be defined in the explainer, which ran its *own* ordered list of
#: predicates and therefore reported whichever one it happened to check first.
#: The measured consequence: ``validate_price`` returned a quote for the CCGP
#: ``单价(元)：17480`` row while the report explained it as "no verifiable
#: transaction date" -- a predicate that had never executed.
BODY_UNAVAILABLE = 'body_unavailable'
EXCERPT_NOT_IN_BODY = 'excerpt_not_in_source'
NOT_AWARDED_CANDIDATE = 'not_awarded_candidate'
MODEL_MISMATCH = 'model_mismatch'
BRAND_MISMATCH = 'brand_mismatch'
NOT_AWARD_EVIDENCE = 'not_award_evidence'
BUDGET_NOT_ACHIEVED = 'budget_not_achieved'
AMOUNT_NOT_UNIT_PRICE = 'amount_not_unit_price'
UNPARSEABLE_AMOUNT = 'unparsed_amount'
TOTAL_NOT_UNIT_PRICE = 'total_not_unit_price'
SOURCE_NOT_TENDER = 'source_not_tender'
UNKNOWN_REASON = 'unexplained'

#: 本轮一份必须满足的实验要求都没有确认。它不是"还没查到证据"，而是"没有可比的项"：
#: 用户不一定有必须满足的实验要求（图6），而没有指标时"满足 0/0 项"会被算成满分。
#: 逐项参数比对、替代结论和报告都先读这个状态，再决定说不说"满足"。
NO_CONFIRMED_PARAMETER = 'no_confirmed_parameter'
#: 本轮确认了必须满足的实验要求，逐项比对才有意义。
CONFIRMED_PARAMETERS = 'confirmed_parameters'


def _refuse(refusals, code, **evidence):
    """Record which predicate refused a row, then return ``None``.

    ``refusals`` is optional, so the acceptance path behaves exactly as before:
    when it is ``None`` this is a plain ``return None``. When the caller passes
    a dict, the refusal carries the reason code and whatever evidence the
    predicate had in hand (the candidate rank, the offending brand, ...), which
    is what lets the report name the real cause instead of guessing one.
    """
    if refusals is not None:
        refusals['reason_code'] = code
        refusals.update(evidence)
    return None


def validate_price_verdict(raw, source, baseline):
    """``(quote, refusal)`` -- the acceptance result *and* the predicate that decided it.

    ``validate_price`` is the single acceptance gate and stays the only thing
    that decides whether a price is usable. This wrapper adds nothing to that
    decision; it only hands back the reason alongside it, so display code can
    stop re-deriving one.
    """
    refusals = {}
    quote = validate_price(raw, source, baseline, refusals)
    return quote, (None if quote is not None else refusals)


def format_money(value):
    """Render an amount for a human: grouped digits, never scientific notation.

    ``f"{1239000.0:g}"`` prints ``1.239e+06``. That string used to reach the
    report body, where a buyer can read neither the magnitude nor the thousands.
    Every amount here is a CNY figure derived from parsed numerals, so a
    fixed-point rendering is exact and needs no significant-digit guess.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return str(value)
    if number == int(number):
        return f'{int(number):,}'
    return f'{number:,.2f}'


def identity(value):
    # Preserve suffixes and digits: X100 and X100S must never merge.
    if value is None or (isinstance(value, str) and not value.strip()):
        # Existing procurement records use empty optional dimensions as part
        # of comparison keys; preserve that wire-level convention while
        # routing non-empty values through the hardened implementation.
        return ""
    try:
        return _hardened_identity(value)
    except (TypeError, ValueError):
        return ""


#: 型号后面粘着的**中文说明**：``B-260型`` 的「型」、``DK-98-ⅡA双列六孔`` 的「双列六孔」、
#: ``CJ-1DII单人单面`` 的「单人单面」。这类写法在采购台账里是常态（本地成交明细库 5648 个
#: 唯一型号里有 964 个含中文），而厂家产品页只写型号本身——``contains_model`` 要求完整
#: token，于是``亚荣 B-260 旋转蒸发器 技术参数`` 这页认得``B-260`` 却认不出``B-260型``，
#: 配置永远取不到（``readable_page`` 只降到 ``model_only``）。
_MODEL_TRAILING_CJK = re.compile(r'[\u3000-\u303f\u3400-\u9fff\uf900-\ufaff]+[\s]*$')
#: 型号后面粘着的**中文括号说明**：``LS-75HD(原LS-B75L-I数显）``。
_MODEL_TRAILING_PAREN = re.compile(r'[（(][^（()）]*[\u3400-\u9fff][^（()）]*[）)][\s]*$')
#: 判定"去掉说明之后还像不像型号"用的三个字符类（见 ``model_spellings``）。
_CJK = re.compile(r'[\u3400-\u9fff\uf900-\ufaff]')
_ASCII_LETTER = re.compile(r'[A-Za-z]')
_DIGIT = re.compile(r'\d')


def model_spellings(model):
    """这个型号在网页上可能的写法；**第一个永远是原样**，与输入逐字相同。

    只有一个额外写法：**把后面粘着的中文说明去掉**。

    ⚠️ 额外写法必须**长得像型号**，否则宁可不多给——``contains_model`` 认的是"这一页在讲
    这台机器"，认错了就会把别的型号的参数抽到这条候选头上。四条一起用：

    * 去掉之后**不含中文**（``1分钟仰卧起坐测试仪外设TSN200-YW-01`` 只去掉了尾巴，
      前面还压着一串中文，不算型号）；
    * 去掉之后**既有 ASCII 字母、又有数字**（``B-260``／``DK-98-ⅡA``／``CJ-1DII`` 合格；
      ``213``／``1000``／``2020`` 这种纯数字不行——``213型`` 去掉「型」只剩 ``213``，
      它会把任何印着 213 的页面匹进来）；
    * 长度 ≥3；
    * 括号里是**英文**的不动（``梅特勒-托利多(METTLER TOLEDO)`` 是品牌写法，不是中文说明）。

    为什么这条线非划不可：本地成交明细库 5648 个唯一型号里，**401 条（7.1%）**会被"去掉
    中文说明"改写成另一个字符串（``output/e2e-final/tmp-model-spellings-blast.py``），
    其中 ``1000九运``→``1000``、``100发一盒``→``100``、``2020年…采购``→``2020`` 这类
    一眼就是错的；上面四条把它们全部挡掉，真正该救的仪器型号
    （``B-260型``、``DK-98-ⅡA双列六孔``、``CJ-1DII单人单面``、``LS-75HD(原LS-B75L-I数显）``）
    一条不少。

    调用方按顺序试：先原样，再这个写法。命中了要如实记下用的是哪一种写法。
    """
    raw = str(model or '').strip()
    if not raw:
        return ()
    cleaned = _MODEL_TRAILING_PAREN.sub('', raw).strip()
    cleaned = _MODEL_TRAILING_CJK.sub('', cleaned).strip()
    if cleaned == raw or len(cleaned) < 3:
        return (raw,)
    if _CJK.search(cleaned) or not _ASCII_LETTER.search(cleaned) or not _DIGIT.search(cleaned):
        return (raw,)
    return (raw, cleaned)


def brand_identity(value):
    """One canonical spelling per brand, from exactly one alias table.

    The table used to exist twice -- here and in the hardened implementation --
    so any new alias had to be added in two places or the two entry points would
    disagree. It now lives only in ``brand_model_rules._BRAND_ALIASES``
    (table-driven, every entry carrying its evidence); this function keeps its
    old contract, including returning ``''`` for empty input instead of raising.

    Since 2026-10-07 the table can also grow at run time: a pair of spellings the
    judgement cannot decide may be put to the model once
    (``brand_model_rules.brand_name_matches`` step 5,
    ``backend/services/brand_spelling.py``), and a "same maker" answer is stored
    as an alias entry -- so the next run hits the table instead of the network.
    Off by default (``LABPILOT_BRAND_LEARN=1``).
    """
    try:
        return _hardened_brand_identity(value)
    except (TypeError, ValueError):
        return ""


def same_model_family(left, right):
    """Two model spellings naming the same product line, different build.

    Kept behind the same facade as the rest of the hardened rules so callers
    never import the implementation module directly.

    **Never a comparability gate.** :func:`设计规范` measured this widening and
    found 0 true positives among the 23 rows it "rescued"; its only sanctioned
    use is annotating a rejected amount with "same line, different build".
    """
    return _hardened_same_model_family(left, right)


def brand_name_matches(left, right):
    """Whether two brand spellings name the same maker.

    Two narrow shapes are accepted, both decided in one place: one side is the
    other's **registered company name** (``上海知楚仪器设备`` minus ``仪器设备``
    ends with ``知楚``), or both sides reach the same canonical spelling through
    ``_BRAND_ALIASES``. Measured on the frozen acceptance run
    (``task-20358119eae4``, ``http://ztb.hubu.edu.cn/info/1203/8269.htm``): the
    announcement's 「参考品牌及型号」 cell says ``上海知楚仪器设备1300A2``, so the
    extractor read the brand as the company name and the comparison basis brand
    was ``知楚``; ``brand_identity`` called those two different brands and the
    award price was dropped whole.

    Deliberately still refused: any cross-language pair that has no alias entry,
    plus the same-trade-name/different-entity pairs (``湘仪`` vs ``湘潭湘仪``,
    ``洁康`` vs ``深圳捷康``). Fails closed on non-strings.

    Two shapes were added on 2026-10-07, both decided in
    ``backend/services/brand_spelling.py`` and both **about spelling, never about
    knowledge**:

    * a slash-compound value (``梅特勒托利多/Mettler-Toledo``) is split and
      compared segment by segment, but only when exactly one side carries the
      slash and the pair is bilingual (one side CJK, both sides with Latin
      letters). The measured row is the 14,600 元 balance price that was refused
      with 「引文中的品牌是 梅特勒托利多/Mettler-Toledo，与目标品牌 梅特勒-托利多
      不一致」; ``(卢)湘仪/湘鑫`` -- one cell listing two different brands -- is
      still refused, which the existing tests lock down;
    * a pair that looks like the same maker but that no predicate can decide is
      put to the model **once**, and a "same maker" answer is registered as an
      alias entry so the next run hits the table. Off by default
      (``LABPILOT_BRAND_LEARN=1``); with it off this judgement is byte-for-byte
      the one above.

    ``贝克曼`` vs ``贝克曼库尔特`` used to be listed here as refused, and the two
    ``brand_mismatch`` records on ``task-013dcdcff7e6`` were cited as depending
    on that refusal. **That is no longer true, and quoting the judgement's own
    output back at it was circular anyway.** On 2026-10-02 (commit ``98d9eec``)
    both spellings -- plus ``Beckman Coulter`` -- were registered in
    ``brand_model_rules._BRAND_ALIASES`` as one maker, after the
    registration name and the trademark column of the same award rows showed
    them to be the same company; those two records were mis-refusals. The alias
    entry is an **addition to the registry table**, not a widening of this
    judgement: ``brand_name_matches('贝克曼', '贝克曼库尔特')`` returns ``True``
    today and ``False`` for the pairs named above
    (``tests/test_brand_name_matching.py``,
    ``tests/test_brand_alias_beckman_xinhua.py``).
    """
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    try:
        return _hardened_brand_name_matches(left, right)
    except (TypeError, ValueError):
        return False


def same_brand_value(left, right):
    """Same-maker test for two **spelling values** out of tables and citations.

    This is the wrapper the price gate uses when it cross-checks a citation
    against the row it re-reads from the body (``validate_price``). Those two
    values are spelling values, not identity keys, so the test has to be
    :func:`brand_name_matches` and **not** ``brand_identity(a) ==
    brand_identity(b)``: a compound spelling (``梅特勒托利多/Mettler-Toledo``)
    keeps its slash through ``identity()`` and therefore never equalled the
    single spelling on the comparison basis. Measured on the frozen ledger
    (``b3-opt-balance-mt-me204e``): the 14,600 元 award row was refused as
    ``excerpt_not_in_source`` even after the brand gate itself had accepted it,
    purely because this cross-check still compared identity strings.
    """
    return brand_name_matches(left, right)


def enterprise_names_in_text(text):
    """Literal company names written in the document body; ``[]`` when none.

    Deterministic, model-free fallback for candidate ``brand``: a datasheet's
    contact page names the maker (``上海天美科学 仪器 有限公司``) while the
    candidate-scoped evidence handed to the model never contains that page, so
    the LLM inventory leaves ``brand`` null and candidate discovery has nothing
    to require brand agreement against. Empty stays empty on purpose -- a
    manual with no company name must not acquire one.

    Only the **extraction** step reads this. Brand agreement between a
    candidate and a discovered price is still decided solely by
    :func:`brand_identity` / :func:`brand_name_matches`; nothing here widens
    those gates.
    """
    return _hardened_enterprise_names_in_text(text)


def row_cells(line):
    """Value cells of one flattened table line (``名称 | 品牌 | 规格型号 | …``).

    Empty list when the line has no cell separator: a single cell cannot say
    which column is the brand, so that line is not this reader's business.
    """
    if not isinstance(line, str):
        return []
    return _hardened_row_cells(line)


def looks_like_brand_value(value):
    """Whether one cell *looks like* a brand wordmark. Shape only, no name list.

    Deliberately strict (digits are refused, so ``3M`` is missed): a wrong brand
    would be compared against the price rows unchanged, while a missing one only
    costs a candidate.
    """
    if not isinstance(value, str):
        return False
    return _hardened_looks_like_brand_value(value)


def normalize_brand_value(value):
    """品牌串规范化；清掉引导字与句读后不像字号就返回 ``''``（见硬化实现）。

    调用它的是把"品牌"从原文里切出来的那一层（``research_planning.brand_from_detail_page``）：
    ``品牌为喆图。`` 那种解析残渣带着标点也算"有品牌"，会占掉一个验证名额；这里要么
    规范成 ``喆图``，要么返回空、由下游按"品牌不明"如实说。
    """
    try:
        return _hardened_normalize_brand_value(value)
    except (TypeError, ValueError):
        return ''


def brand_from_source_line(line, model='', category=''):
    """Read the brand **this one source line** states; ``''`` when it states none.

    The three routes (header column names in the same line, an inline
    ``品牌：湘智`` label, and shape alignment across the cells) all take a value
    spelled out in that line and never borrow one from elsewhere or from common
    sense. The value that comes back is normalised (:func:`normalize_brand_value`),
    so an inline ``品牌：为喆图。`` yields ``喆图`` instead of a residue that
    ``brand_name_matches`` would refuse against a genuine ``喆图`` price row.
    Alias normalisation stays with :func:`brand_identity`.
    """
    if not isinstance(line, str):
        return ""
    return _hardened_brand_from_source_line(line, model=model, category=category)


def brand_from_enterprise_name(name):
    """Turn a body-text company name into the candidate ``brand`` value.

    Returns the canonical spelling when ``_BRAND_ALIASES`` has one
    (``苏州安泰空气技术有限公司`` -> ``苏净安泰``), otherwise the registered name
    exactly as written in the document. Fails closed (``''``) on non-strings.
    """
    if not isinstance(name, str):
        return ""
    try:
        return _hardened_brand_from_enterprise_name(name)
    except (TypeError, ValueError):
        return ""


def contains_model(text, model, max_depth=3):
    """Return whether *model* occurs as a complete model token.

    Production callers use this predicate as a fail-closed gate.  Invalid
    inputs therefore return ``False`` instead of being coerced to strings;
    ``max_depth`` is accepted for parity with the hardened implementation and
    prevents recursive evidence checks from being unbounded.
    """
    if max_depth <= 0 or not isinstance(text, str) or not isinstance(model, str):
        return False
    try:
        return _hardened_contains_model(text, model, max_depth=max_depth)
    except (TypeError, ValueError, re.error):
        return False



def supported(excerpt, body):
    if not isinstance(excerpt, str) or not isinstance(body, str):
        return False
    try:
        return _hardened_supported(excerpt, body)
    except (TypeError, ValueError):
        return False


def amount(value):
    # bool is a subclass of int, but it is never a valid monetary amount.
    return _hardened_amount(value)


def tender_attachment(source):
    """一份**招标文件的附件**，而不是招标文件正文本身。

    附件是 ``fetch_attachments`` 抓下来的独立来源，它带着父页面的地址
    （``parent_url``），而公告正文没有这个键。两者都是 ``tender_document``，
    「附件里的设备清单能不能当候选参数证据」这件事只对前者成立，所以判据必须
    落在 ``parent_url`` 上，不能只看来源类型。
    """
    return bool(isinstance(source, dict) and source.get('parent_url')
                and source.get('source_type') == 'tender_document')


def provisional_parameter_evidence(source):
    """这份来源只能给出待核验证据，不能算厂家确认。

    未核身份的来源与招标文件附件都属此类：附件里印着的是**采购方写进招标
    文件的技术规格**，不是厂家对自己产品的标称。
    """
    return bool(isinstance(source, dict)
                and ((source.get('source_type') == 'unknown' and source.get('purpose') != 'tender')
                     or tender_attachment(source)))


#: "数值紧跟单位"的写法 —— ``10000rpm``／``18500r``／``50ml``。这是**量出来的值**，
#: 不是型号。厂商页常把整机参数写成一行（``安亭GL-10000C：最高转速：10000rpm (转/分)；
#: 最大相对离心力：15860(×g)``），于是第二个参数标签前面的那个 token 是**上一个参数的
#: 值**，而 ``grounded_parameter`` 的型号锚定闸门原来把它当成"另一个型号"，整行后半段
#: 的参数全被拒、``observed`` 永远是 ``null``；同一条正文在候选卡片那条路上却读得到值。
#: 真跑实测见 ``tests/test_config_matrix_source_agreement.py``。
#: 刻意写成"数字开头 + 单位拼写"两条同时成立：``3H24RI``／``3H20RI`` 这种数字开头的
#: 型号因为后半段不是单位，照旧算型号（``test_research_repair`` 里那两条用例钉着）。
_MEASUREMENT_TOKEN = re.compile(
    r'\d[\d.,]*\s*(?:r/min|rpm|rcf|xg|×g|ml|l|μl|kg|mg|g|mm|cm|℃|°c|db|dba|%|r)$', re.I)


def grounded_parameter(observed, source, model, name, *, allow_unverified=False, max_recursion=5):
    """Bind value, unit and condition to the model's actual parameter excerpt."""
    if max_recursion <= 0:
        return False
    if not isinstance(observed, dict) or not isinstance(source, dict):
        return False
    if not isinstance(model, str) or not model.strip() or not isinstance(name, str) or not name.strip():
        return False
    verified_source = source and source.get('source_type') in {'manufacturer_website', 'manufacturer_download'}
    provisional_source = source and source.get('source_type') == 'unknown' and source.get('purpose') != 'tender'
    # 招标文件的附件只能当**核对该候选参数**的证据。它随附的是采购方列的设备清单
    # 与技术要求，所以这里照旧走 `contains_model` 与逐字引文的同一道闸门，整体
    # 不会因为放行附件而放宽：正文里没有这个型号，附件就一点都不算数。
    if not observed or not source or source.get('body_status') != 'fetched' or not isinstance(source.get('body_text'), str) or not (verified_source or tender_attachment(source) or (allow_unverified and provisional_source)):
        return False
    excerpt = str(observed.get('excerpt') or '')
    if not supported(excerpt, source.get('body_text')) or not contains_model(excerpt, model):
        return False
    condition = str(observed.get('conditions') or '')
    label = name.replace(condition, '').strip() if condition else name
    aliases = {'控温精度': ('温控精度', '温度精度'), '温度设置范围': ('温度范围', '控温范围'),
               '最高转速': ('最大转速',), '最大相对离心力': ('最大离心力',)}
    labels = list(dict.fromkeys([name, label, *aliases.get(label, ())]))
    matched = next((s for s in labels if s and s in excerpt), None)
    if not matched:
        return False
    # If the source has cell geometry, a broad literal citation cannot bypass
    # the target column. Validate against that column, including unit/condition.
    projections = [line for line in source.get('body_text', '').splitlines()
                   if '[表格按列展开' in line and contains_model(line, model) and matched in line]
    if projections and excerpt not in projections:
        return any(grounded_parameter({**observed, 'excerpt': line},
                   {**source, 'body_text': line}, model, name,
                   allow_unverified=allow_unverified,
                   max_recursion=max_recursion - 1) for line in projections)
    excerpt = unicodedata.normalize('NFKC', excerpt)
    # A citation covering multiple products is not a model-specific parameter
    # region. HTML column projections carry the model on each evidence line.
    parameter_line = next((line for line in excerpt.splitlines() if matched in line), excerpt)
    if '[表格按列展开' in parameter_line:
        if not contains_model(parameter_line, model):
            return False
        excerpt = parameter_line
    model_tokens = [token for token in re.findall(r'(?<![A-Za-z0-9])(?=[A-Za-z0-9-]*[A-Za-z])(?=[A-Za-z0-9-]*\d)[A-Za-z0-9][A-Za-z0-9-]*(?![A-Za-z0-9])', excerpt.split(matched, 1)[0])
                    if not _MEASUREMENT_TOKEN.fullmatch(token)]
    if model_tokens and identity(model_tokens[-1]) != identity(model):
        prefix = excerpt.split(matched, 1)[0]
        last_end = prefix.rfind(model_tokens[-1]) + len(model_tokens[-1])
        anchored = r'(?<![A-Za-z0-9])' + r'[\s\-‐‑‒–—―−]*'.join(map(re.escape, identity(model))) + r'$'
        if not re.search(anchored, prefix[:last_end], re.I):
            return False
    # The value must be a whole number token, never 20 borrowed from 120.
    values = observed.get('value') if isinstance(observed.get('value'), list) else [observed.get('value')]
    tail = re.split(r'[，；;\n。]|,(?!\d{3}(?:\D|$))', excerpt.split(matched, 1)[-1], maxsplit=1)[0]
    if condition and condition not in matched:
        if condition in tail:
            tail = tail.split(condition, 1)[-1]
        elif not excerpt.split(matched, 1)[0].rstrip().endswith(condition):
            return False
    numbers = [float(n.replace(',', '')) for n in re.findall(r'(?<![A-Za-z0-9.])-?\d[\d,]*(?:\.\d+)?(?![\d.])', tail)]
    for value in values:
        if isinstance(value, bool):
            return False
        if not isinstance(value, bool) and isinstance(value, (int, float, str)) and re.fullmatch(r'-?\d+(?:\.\d+)?', str(value)):
            number = float(value)
            if not math.isfinite(number):
                return False
            if number not in numbers[:len(values)]:
                return False
        elif value is None or not supported(str(value), tail):
            return False
    unit = unicodedata.normalize('NFKC', str(observed.get('unit') or ''))
    if unit and not re.search(r'(?<![A-Za-z])' + re.escape(str(unit)) + r'(?![A-Za-z])', tail, re.I):
        return False
    return not observed.get('conditions') or supported(observed['conditions'], excerpt)


def _parameter_hint_terms(name):
    """Map requirement labels to text anchors used for raw-evidence fallback."""
    label = unicodedata.normalize('NFKC', str(name or ''))
    if any(token in label for token in ('容积', '容量', '体积')):
        return ('容积', '容量', '内胆', '内箱', '内部尺寸', 'L')
    if '温度' in label or '温区' in label or '控温' in label:
        return ('温度范围', '控温范围', '温区', '温度')
    if any(token in label for token in ('记录', '导出', '数据', '打印', '留档')):
        return ('记录', '导出', '数据', '打印', 'USB', 'RS485', '通讯', '接口')
    return tuple(token for token in re.split(r'[\s、，,;/]+', label) if len(token) > 1)


def _raw_parameter_evidence(required, sources, model, *, limit=3):
    """Return readable source excerpts when structured extraction is incomplete."""
    hints = _parameter_hint_terms(required.get('name'))
    found, seen = [], set()
    for source in sources or []:
        if source.get('body_status') != 'fetched' or not source.get('body_text'):
            continue
        lines = [line.strip() for line in str(source['body_text']).splitlines() if line.strip()]
        model_indexes = [i for i, line in enumerate(lines) if contains_model(line, model)]
        if not model_indexes:
            continue
        source_selected = []
        for index, line in enumerate(lines):
            has_hint = any(hint.casefold() in line.casefold() for hint in hints)
            near_model = any(abs(index - model_index) <= 2 for model_index in model_indexes)
            if not ((contains_model(line, model) and (has_hint or not hints)) or (has_hint and near_model)):
                continue
            source_selected.append(line)
        # Even when the page splits the model table from its parameter rows,
        # retain the exact model context so the initial report is inspectable.
        if not source_selected:
            source_selected = [lines[index] for index in model_indexes[:1]]
        for line in source_selected:
            key = (source.get('url'), line)
            if key in seen:
                continue
            seen.add(key)
            found.append({'url': source.get('url'), 'excerpt': line[:1200],
                          'source_type': source.get('source_type', 'unknown'),
                          'evidence_status': 'provisional' if provisional_parameter_evidence(source) else 'source_text',
                          'evidence_kind': 'model_context' if not any(hint.casefold() in line.casefold() for hint in hints) else 'parameter_context'})
            if len(found) >= limit:
                return found
    return found


def _unparsed_parameter_observation(raw, source, model):
    """Keep a model-cited value visible when strict parsing rejects it.

    This is deliberately a display-only evidence record.  The citation must
    still be present verbatim in a fetched source and contain the target
    model, but the value is not promoted to ``observed`` until the normal
    unit/condition/value checks pass.
    """
    if not isinstance(raw, dict) or not isinstance(source, dict):
        return None
    excerpt = str(raw.get('excerpt') or '').strip()
    body = source.get('body_text')
    if (source.get('body_status') != 'fetched' or not isinstance(body, str)
            or not excerpt or not supported(excerpt, body)
            or not contains_model(excerpt, model)):
        return None
    item = {
        'url': source.get('url') or raw.get('url'),
        'excerpt': excerpt[:1200],
        'source_type': source.get('source_type', 'unknown'),
        'evidence_status': 'unparsed',
        'evidence_kind': 'model_observation',
    }
    if 'value' in raw:
        item['raw_value'] = raw.get('value')
    if raw.get('unit') not in (None, ''):
        item['unit'] = raw.get('unit')
    if raw.get('conditions') not in (None, ''):
        item['conditions'] = raw.get('conditions')
    return item


def parameter_row(required, observations, sources, model, previous=None, *, allow_unverified=False):
    """Keep independently grounded contradictions visible instead of last-wins."""
    verified = []
    unparsed = []
    for raw in observations:
        if not isinstance(raw, dict) or raw.get('name') != required['name']:
            continue
        source = next((s for s in sources if s['url'] == raw.get('url')), None)
        if grounded_parameter(raw, source, model, required['name'], allow_unverified=allow_unverified):
            observed = {k: raw.get(k) for k in ('name', 'value', 'unit', 'conditions', 'url', 'excerpt')}
            observed['evidence'] = {'url': raw['url'], 'excerpt': raw['excerpt']}
            if provisional_parameter_evidence(source):
                # 招标文件附件与未核身份的来源都只到「待核验」：一行里只要有一份
                # 这样的证据，整行就不能标成厂家确认。
                observed['evidence_status'] = 'provisional'
            verified.append(observed)
        else:
            evidence = _unparsed_parameter_observation(raw, source, model)
            if evidence and evidence not in unparsed:
                unparsed.append(evidence)
    if previous:
        verified.extend(previous.get('observations', []) or ([previous['observed']] if previous.get('observed') else []))
    if not verified:
        raw_evidence = unparsed + _raw_parameter_evidence(required, sources, model)
        deduped = []
        seen = set()
        for evidence in raw_evidence:
            key = (evidence.get('url'), evidence.get('excerpt'))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(evidence)
            if len(deduped) >= 3:
                break
        row = {'required': required, 'observed': None, 'status': 'unknown'}
        if deduped:
            row.update({'raw_evidence': deduped, 'evidence_status': 'unparsed'})
        return row
    unique = []
    for obs in verified:
        if obs not in unique:
            unique.append(obs)
    first = unique[0]
    comparable = [o for o in unique if identity(o.get('conditions')) == identity(required.get('conditions'))]
    if comparable:
        first = comparable[0]
    conflict = any(evaluate_parameter({**first, 'direction': 'covers' if isinstance(first.get('value'), list) else 'equal'}, o) != 'satisfied'
                   or evaluate_parameter({**o, 'direction': 'covers' if isinstance(o.get('value'), list) else 'equal'}, first) != 'satisfied'
                   for o in comparable[1:])
    return {'required': required, 'observed': None if conflict else first,
            'status': 'unknown' if conflict else evaluate_parameter(required, first),
            'evidence_status': 'provisional' if any(o.get('evidence_status') == 'provisional' for o in unique) else 'confirmed',
            'observations': unique, 'conflict': conflict,
            'raw_evidence': _raw_parameter_evidence(required, sources, model) if conflict else []}


# ===========================================================================
# 无效 / 废标 / 已终止公告里的价格不是成交价
# ===========================================================================
#
# 背景（实测）：``prices()`` 的**表格分支**会把一份
# ``source_type='tender_document'`` 的废标公告正文里那张
# 「原中标候选人报价如下（已废标，仅供参考）」表格直接交给 ``validate_price``；
# 而后者原先只认识「第 N 中标候选人」这种**带排名的落选报价**，对「本项目已废标」
# 这种**文档级撤销**没有任何判据，于是那个金额以 ``kind='award'`` 被采纳。
# 这条通道在「金额门」那次改动（14c1797）之前就存在——表格分支当时已在
# ``procurement_research.prices()`` 里，那次改动只放宽了「表格解析器读不出的
# 公告也轮到模型抽一次」。所以判据补在**所有价格通道的收口点**上。
#
# 判据全部基于**正文事实**（撤销措辞 + 这个价格与它的相对位置），不看域名：
# 同一条金额在一份真实成交公告里会被采纳，在同一份正文的撤销语境里必须被拒。

#: 采购决定被撤销的措辞。与 ``official_search._extract_tender_metadata`` 里的
#: ``void_markers`` 是同一族证据，那边只用它扫「中标金额」正则周围 80/200 字符的
#: 窗口；这里面对的是所有价格通道，所以多收了几种正文写法。
#: 刻意**不收**裸「终止」：合同终止公告里的金额是已签合同的成交价。
REVOCATION_MARKERS = re.compile(
    r'废标|流标|作废标|中标无效|成交无效|采购活动终止|终止本次采购|本次采购终止'
    r'|采购项目终止|项目终止|采购终止')

#: 撤销之后又重新组织采购——「第一次废标、重新采购后成交」的正常公告。
#: 撤销声明与价格之间这段文字出现它，说明该价格属于新一轮采购的结论。
REPURCHASE_MARKERS = re.compile(
    r'重新(?:组织|开展|招标|采购|询价|竞价|谈判|磋商)|再次(?:组织|招标|采购)'
    r'|二次(?:招标|采购)|第[二2]次(?:招标|采购)')

#: 本次采购已经给出成交结论的措辞。历史值通常带「原」字（「原中标候选人报价」
#: 「原中标金额」），所以用负向断言把它排除在外，否则撤销公告里的历史值会被
#: 误当成「本轮成交结论」而获得豁免。
CONCLUDED_PRICE_MARKERS = re.compile(
    r'(?<!原)(?:中标|成交)(?:供应商|人|金额|价|单价|总价)|合同金额|合同价')

#: 明确写着「不是本次成交结果」的价格措辞。刻意**不收**「投标报价」：正常成交
#: 公告也会写「中标供应商投标报价 74,800 元」。
NOT_FINAL_PRICE_MARKERS = re.compile(
    r'仅供参考|仅作参考|仅供参阅|已废标|原中标候选人|原成交|原中标|作废标处理')

#: 撤销公告的标题形态。只看文档开头——标题一定写在最前面，而价格表可能在很
#: 下面、连引导句里都不再重复「废标」二字。
REVOCATION_TITLE = re.compile(r'(?:废标|流标|采购终止|项目终止|采购活动终止)[^。；\n]{0,8}公告')
TITLE_SCAN_LIMIT = 300

#: 价格引文周围的撤销语境窗口。前小后大，与 ``official_search`` 的 80/200 同源：
#: 撤销声明既可能写在价格表**前面**的引导句里（「原中标候选人报价如下（已废标，
#: 仅供参考）：」），也可能写在金额**后面**（「……报价：836000（元）……根据
#: 某某市财政局……的处理结果，本项目废标」）。
REVOCATION_LOOKBACK = 120
REVOCATION_LOOKAHEAD = 200
#: 「这个价格自己就是本轮成交结论」的判定窗口，只用于给撤销公告标题豁免。
CONCLUDED_LOOKBACK = 120
CONCLUDED_LOOKAHEAD = 120


def _concluded_price_context(text, excerpt):
    """价格引文附近是否写着本次成交结论。引文不在 *text* 里就返回 ``False``。"""
    start = text.find(excerpt)
    if start < 0:
        return False
    window = text[max(0, start - CONCLUDED_LOOKBACK):start + len(excerpt) + CONCLUDED_LOOKAHEAD]
    return bool(CONCLUDED_PRICE_MARKERS.search(window))


def _revocation_near(text, excerpt):
    """价格引文周围是否有生效的撤销声明；有就返回依据，否则 ``None``。

    引文可能在一份正文里出现多次，每一处都查，任一处落在撤销语境里就拒——
    与 ``price_rejections.candidate_rank_of`` 同样 fail closed。
    """
    start = text.find(excerpt)
    while start != -1:
        window_start = max(0, start - REVOCATION_LOOKBACK)
        window = text[window_start:start + len(excerpt) + REVOCATION_LOOKAHEAD]
        for marker in REVOCATION_MARKERS.finditer(window):
            marked_at = window_start + marker.start()
            if marked_at >= start:
                # 撤销写在金额之后：这笔（可能已经成交的）金额已被推翻。
                return f'价格引文之后的撤销声明「{marker.group(0)}」'
            between = text[marked_at:start]
            # 撤销写在金额之前有两种现实，它们的价格都该采纳：
            #   ① 第一次废标后重新组织采购，本次成交；
            #   ② 分包采购里某个包废标、另一个包成交。
            # 两种都在「撤销声明 → 价格」这段文字里留下痕迹。
            if REPURCHASE_MARKERS.search(between) or CONCLUDED_PRICE_MARKERS.search(between):
                continue
            return f'价格引文之前的撤销声明「{marker.group(0)}」'
        start = text.find(excerpt, start + 1)
    return None


def revoked_price_evidence(excerpt, texts):
    """这份价格是否被同一份公告撤销？返回命中的正文依据，否则 ``None``。

    ``texts`` 是这个价格可以引用的**每一段**文本（公告正文，以及它的链接公告
    正文）。分开判定而不是拼成一份：跨文本做方向判断会把「附件里的成交明细 +
    父公告里说明上一轮废标」误判成撤销。

    调用方只关心真值；返回的依据字符串是给测试和排查用的。注意本函数**不写入
    任何东西**，也不改变 ``validate_price`` 的其它谓词。
    """
    excerpt = str(excerpt or '')
    if not excerpt:
        return None
    for text in texts:
        if not isinstance(text, str) or not text:
            continue
        # 1. 整篇就是一份撤销公告。撤销公告不一定在价格表旁边重复「废标」，
        #    但标题一定写着。
        title = REVOCATION_TITLE.search(text[:TITLE_SCAN_LIMIT])
        if title and not _concluded_price_context(text, excerpt):
            return f'文档标题为撤销公告「{title.group(0)}」'
        # 2. 价格引文周围的撤销声明。
        near = _revocation_near(text, excerpt)
        if near:
            return near
        # 3. 价格自己写着「不是本次成交结果」，而这份文档又确实撤销了采购。
        #    先查便宜的局部标记，再决定要不要扫整篇正文——正常成交公告在这里
        #    直接返回，不会为大正文付出一次全文正则。
        start = text.find(excerpt)
        if start >= 0:
            lead_in = text[max(0, start - 80):start] + excerpt
            if NOT_FINAL_PRICE_MARKERS.search(lead_in) and REVOCATION_MARKERS.search(text):
                return '价格被标注为非最终结果，且公告含撤销声明'
    return None


#: 引文/正文的归一化：与 ``supported`` 同一口径（NFKC + 去掉所有空白），
#: 免得两层判据对"什么算同一个字"各有各的答案。
def _flat_text(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value))


#: 跨列拼接引文切段用的分隔符：``名称； 品牌； 型号； 数量； 金额``。
_TABLE_EXCERPT_SEPARATOR = re.compile(r'[；;|]+')


def excerpt_in_table_row(excerpt, body, model):
    """把"跨列拼接的表格行引文"映射回正文里那一行的逐字原文；做不到就 ``None``。

    实测（``设计说明`` §5.2 第 2 条，
    ``http://www.ccgp.gov.cn/cggg/zygg/cjgg/202608/t20260812_27118698.htm``）：
    一份列式成交表的正文把每一列各自用 ``；`` 串起来——

        1 | 供应商 | A2型生物安全柜； 倒置显微镜； … | 贝晶； 徕卡； 徕卡； … |
        Mars1500； DMi1； … | 11台； 2台； … | 48000； 55000； …

    抽取层读某个条目时会把**不同列的同序号单元格**拼成一行：
    ``倒置显微镜； 徕卡； DMi1； 2台； 55000``。这五段在正文里都在，但从不连续
    （``倒置显微镜`` 与 ``徕卡`` 之间还隔着名称列的其它条目），于是 ``supported``
    的逐字子串判定必然失败，一条真实的 55000 元/台成交被记成"引文无法逐字定位"。
    同一个来源、同一份正文，由表格解析器给出的引文是**逐字整行**，一次就过——
    差别不在空白/全角半角/换行（``supported`` 本来就折叠掉这些），而在
    **表格列拼接 vs 整行复制**。

    放行条件，三条同时成立：

    1. 引文按 ``；``/``;``/``|`` 切出**至少两段**（否则这就是一次普通子串判定，
       原样交给 ``supported``）；
    2. ``price_table_rows`` 认得的某一行里，这些段**按顺序**逐字出现；
    3. 返回的是那一行的**逐字原文**，调用方拿它替换引文。

    第 3 条是关键：校验没有被绕过，落库的引文反而回到了原文上。序号对齐不在
    这一层判——引文过门之后仍要过 ``price_table_rows`` 投影出的金额/数量/品牌
    一致性（``validate_price`` 的 ``projected`` 分支），跨序号拼出来的组合
    （第 1 项的名称配第 2 项的金额）在那里被拒。
    """
    if not isinstance(excerpt, str) or not isinstance(body, str) or not body:
        return None
    if not _TABLE_EXCERPT_SEPARATOR.search(excerpt):
        return None
    fragments = [part for part in _TABLE_EXCERPT_SEPARATOR.split(excerpt) if part.strip()]
    if len(fragments) < 2:
        return None
    flattened = [_flat_text(fragment) for fragment in fragments]
    from backend.services.procurement_tables import price_table_rows
    for row in price_table_rows(body, model):
        line = row.get('excerpt') or ''
        if not line:
            continue
        position, matched = 0, True
        flat_line = _flat_text(line)
        for fragment in flattened:
            found = flat_line.find(fragment, position)
            if found < 0:
                matched = False
                break
            position = found + len(fragment)
        if matched:
            return line
    return None


#: 「批 / 宗 / 包」是整包口径。数量落在这些单位上时，那一格印的是整包金额，
#: 不是一台设备的成交价：黑龙江 2022-09-07 的合同包写着「3,651,000.00 | 1.00 | 批」，
#: 型号列是「BJ-2CD等其他设备型号」——整包里有多种设备，这笔钱既不是一台的价，
#: 也不是 BJ-2CD 这一个型号的价。金额与口径绑错会把整包价写成单台成交价。
_PACKAGE_UNIT_RE = re.compile(
    r'\|\s*(?:批|宗|包)\s*\|'
    r'|[\d.,]+\s*(?:批|宗|包)(?![\u4e00-\u9fff])'
    r'|数量\s*[:：]?\s*[\d.,]+\s*(?:批|宗|包)'
)

#: 单位声明写成紧凑括号形状：表头「货物单价（元）」「单价(万元)」。
_UNIT_DECLARATION_RE = re.compile(
    r'(?:单价|金额|价格|报价|中标价|成交价|合同价)\s*[（(]\s*(万元|千元|元)\s*[）)]'
)
#: 总价句：「中标（成交）金额： 115.880000 (万元)」。
_TOTAL_DECLARATION_RE = re.compile(
    r'(?:中标|成交|合同)[^\n。；]{0,16}?金额\s*[:：]?\s*([\d,]+(?:\.\d+)?)\s*[（(]\s*(万元|千元|元)\s*[）)]'
)
#: 逐条明细里的「单价：4.8390」，用来把明细求和与总价对上账。
_LABELED_UNIT_PRICE_RE = re.compile(r'单价\s*[:：]?\s*([\d,]+(?:\.\d+)?)')
#: 一格并列多台设备时，值之间用顿号/逗号分隔（``split_cells`` 只切分号，切不开这种）。
_SIBLING_SEPARATOR = re.compile(r'[、,，;；]')


def _has_number(text, value):
    """这一格里有没有等于 ``value`` 的数字。"""
    return any(amount(number) == value for number in re.findall(r'\d[\d,]*(?:\.\d+)?', text or ''))


def _sold_as_package(text):
    """这一行是不是按「批/宗/包」整体成交。"""
    if not isinstance(text, str):
        return False
    return bool(_PACKAGE_UNIT_RE.search(text))


def declared_amount_unit(raw, source, excerpt, money_excerpt):
    """单位不在引文里时，从同一份来源的声明里取刻度；取不到就返回 ``None``。

    两种实测形状（批3，2026-10-03）：

    * 表头「货物单价（元）」——上海农林职业技术学院 2022-06-15 成交公告，一行里
      并列五台设备，目标型号 HVE-50 的价是 99,840.50 元；引文带数字、「元」印在
      表头，于是整条被 ``unparsed_amount`` 拒掉。
    * 公告总价句「中标（成交）金额： 115.880000 (万元)」——武汉工程大学
      2025-09-15 中标公告，逐条写「名称：高压灭菌锅 … 数量：1 单价：4.8390」，
      单位在总价句里（4.8390 万元 = 48,390 元）。这一条能对上账：同一页八条
      明细的单价求和正好 115.88，等于中标总价。

    换算型单位（万元/千元）必须用「明细求和 ≈ 总价」对上账才认，避免拿一个版面
    单位给随便一个数字加刻度。引文本身的数字仍由调用方核对（``quoted_numbers``）。
    """
    unit = raw.get('amount_unit')
    if unit not in {'元', '万元', '千元'}:
        return None
    body = source.get('body_text') or ''
    snippet = source.get('snippet') or ''
    texts = [body, snippet] + [text for text in (excerpt, money_excerpt) if isinstance(text, str)]
    declarations = [match for text in texts for match in _UNIT_DECLARATION_RE.finditer(text)]
    declared = {match.group(1) for match in declarations}
    if unit == '元' and declared == {'元'}:
        return unit, declarations[0].group(0)
    total = _TOTAL_DECLARATION_RE.search(body) or _TOTAL_DECLARATION_RE.search(snippet)
    if total is None or total.group(2) != unit:
        return None
    stated = float(total.group(1).replace(',', ''))
    labeled = [float(value.replace(',', '')) for value in _LABELED_UNIT_PRICE_RE.findall(body)]
    if len(labeled) < 2:
        return None
    if abs(sum(labeled) - stated) > max(stated * 0.01, 0.02):
        return None
    return unit, total.group(0)


def _sibling_alignment_conflicts(raw, excerpt, model, value):
    """一格并列多台设备时，目标型号的序号必须与金额的序号一致。

    上海农林职业技术学院那份公告把五台设备写在同一格：型号
    「HVE-50、SZX7、BCD-520WDPD、MGC-350HP、Master Touch-RUV等」对金额
    「99840.50、84720.00、3600.00、21800.00、37500.00」。目标型号排第 1，
    只有第 1 个数是它的价；取第 3 个（3600.00，冰箱的价）同样"有数字、有单位"，
    却是另一台设备的成交价。型号列与金额列项数不一致时不下结论——那由后面的
    数字与单位核对去拒。
    """
    if not isinstance(excerpt, str) or not model:
        return False
    for line in excerpt.splitlines():
        if '|' not in line:
            continue
        cells = [cell.strip() for cell in line.split('|')]
        model_cells = [cell for cell in cells if contains_model(cell, model)]
        amount_cells = [cell for cell in cells if _has_number(cell, value)]
        if not model_cells or not amount_cells or model_cells[0] == amount_cells[0]:
            continue
        models = [part for part in _SIBLING_SEPARATOR.split(model_cells[0]) if part.strip()]
        amounts = [part for part in _SIBLING_SEPARATOR.split(amount_cells[0]) if part.strip()]
        if len(models) < 2 or len(models) != len(amounts):
            continue
        wanted = next((index for index, name in enumerate(models) if contains_model(name, model)), None)
        got = next((index for index, text in enumerate(amounts) if amount(text) == value), None)
        if wanted is not None and got is not None and wanted != got:
            return True
    return False


def validate_price(raw, source, baseline, refusals=None):
    """Require a model-specific quotation; never promote project totals.

    This is the one predicate stack every price lane runs through, so the
    structural disqualifiers live here rather than at each call site. A losing
    bid is the most fundamental of them: no matter how well-formed the row is,
    a runner-up's bid is not a transaction.

    Passing ``refusals`` (a dict) makes every refusal record *which* predicate
    fired, for display. The decision is identical whether or not it is passed.
    """
    excerpt = str(raw.get("excerpt") or "")
    if source.get("body_status") != "fetched":
        return _refuse(refusals, BODY_UNAVAILABLE)
    if not supported(excerpt, source.get("body_text")):
        # 跨列拼接的表格行引文映射回那一行的逐字原文（见 excerpt_in_table_row）。
        # 映射不出来才拒：这道门仍然是防模型编造引文的闸门，不是被关掉。
        row_excerpt = excerpt_in_table_row(excerpt, source.get("body_text"), baseline.get("model"))
        if row_excerpt is None:
            return _refuse(refusals, EXCERPT_NOT_IN_BODY)
        excerpt = row_excerpt
        # 落库的引文也换成逐字原文：库里不留"定位不到"的引用。
        raw = {**raw, "excerpt": row_excerpt}
    # Refuse a row that sits in a non-winning candidate's list. The table lane
    # already refuses these before calling this function (procurement_research
    # ``prices()``); the model-extraction lane did not, and its only remaining
    # guard was the ``kind == 'award'`` wording test, which the word 中标 inside
    # 「中标候选人公示」 satisfies on its own. Observed on 芜湖市第一人民医院
    # 「心电监护、输液泵双泵采购（二包）中标候选人公示」
    # (https://whfph.com/714/15595.html): the 第二中标候选人's 路迈 LS-1 unit
    # price of 1,780 元 was accepted as an award price, while the same project's
    # result announcement (https://whfph.com/714/15599.html) awards the package
    # to the 第一名 at 中标金额 104,750.00 元 -- the loser's pump was never
    # bought. ``None`` means "this document states no candidate ranking" and must
    # keep allowing ordinary award notices; rank 1 is allowed because the first
    # candidate's bid *is* the award amount in the ordinary case (verified on the
    # same notice: 第一中标候选人 投标报价 104,750.00 元 == the result page's
    # 中标金额 104,750.00 元).
    from backend.services.price_rejections import candidate_rank_of
    candidate_rank = candidate_rank_of(raw, source)
    if candidate_rank is not None and candidate_rank != 1:
        return _refuse(refusals, NOT_AWARDED_CANDIDATE, candidate_rank=candidate_rank)
    if identity(raw.get("model")) != identity(baseline.get("model")) or not contains_model(excerpt, baseline.get("model")):
        return _refuse(refusals, MODEL_MISMATCH)
    if raw.get("brand") and baseline.get("brand") and not brand_name_matches(raw["brand"], baseline["brand"]):
        return _refuse(refusals, BRAND_MISMATCH, other_brand=raw.get("brand"))
    table_rows = [line for line in excerpt.splitlines() if '|' in line and contains_model(line, baseline['model'])]
    if table_rows:
        # Metadata dates may surround a row, but another row's amount cannot
        # be borrowed. Preserve the complete citation for audit/export below.
        money_excerpt = '\n'.join(table_rows)
    else:
        money_excerpt = excerpt
    from backend.services.procurement_tables import price_table_rows
    projected = [p for p in price_table_rows(source.get('body_text') or '', baseline['model'])
                 if supported(p['excerpt'], excerpt)]
    if projected:
        match = next((p for p in projected if p['amount'] == amount(raw.get('amount')) and p['amount_unit'] == raw.get('amount_unit')
                      and p['quantity'] == amount(raw.get('quantity')) and raw.get('amount_scope') == 'unit'
                      and all(not brand or not p['brand'] or same_brand_value(brand, p['brand'])
                              for brand in (raw.get('brand'), baseline.get('brand')))), None)
        if not match:
            return _refuse(refusals, EXCERPT_NOT_IN_BODY)
        money_excerpt = match['money_excerpt']
    kind = raw.get("kind")
    publisher_text = source.get('body_text', '')
    from backend.services.source_authority import linked_publication
    # The linked publication page may lend its award wording, but only when it
    # is itself an institutional publication, and only through the parent
    # identity recorded when the file was discovered. Which host counts is
    # decided in one place; a hospital or bidding agency on ``*.com`` qualifies.
    linked = linked_publication(source)
    if linked is not None:
        publisher_text += '\n' + linked['body_text']
    # 无效 / 废标 / 已终止公告里的价格不是成交价。判据只看正文事实：一份写着
    # 「本项目废标，采购活动终止」的公告，其「原中标候选人报价」无论多么完整、
    # 日期多么可核实，都不是一笔交易。附件价格一并受它约束——附件的
    # ``publication_context`` 会把父公告正文带进这里，所以挂在废标公告下的
    # 报价明细同样被拒。
    if revoked_price_evidence(excerpt, [source.get('body_text') or '',
                                        (linked or {}).get('body_text') or '']):
        return _refuse(refusals, NOT_AWARD_EVIDENCE, revoked=True)
    if kind not in {"award", "contract"}:
        return _refuse(refusals, NOT_AWARD_EVIDENCE, unsupported_kind=kind)
    if kind == 'award' and not re.search(r'中标|成交|award', publisher_text, re.I):
        return _refuse(refusals, NOT_AWARD_EVIDENCE)
    if kind == 'contract' and not re.search(r'合同|contract', publisher_text, re.I):
        return _refuse(refusals, NOT_AWARD_EVIDENCE)
    if kind in {'award', 'contract', 'official_list'} and re.search(r'预算|最高限价', excerpt) and not re.search(r'成交单价|中标单价|合同单价', excerpt):
        return _refuse(refusals, BUDGET_NOT_ACHIEVED)
    scope = raw.get("amount_scope")
    value, quantity = amount(raw.get("amount")), amount(raw.get("quantity"))
    if value is None or scope not in {"unit", "single_item_total"}:
        return _refuse(refusals, AMOUNT_NOT_UNIT_PRICE, amount_scope=scope)
    # 「1 批 / 1 宗 / 1 包」印的是整包金额，不是单台成交价：黑龙江 2022-09-07 那笔
    # 3,651,000 元的型号列写着「BJ-2CD等其他设备型号」，整包里装着多种设备。
    if _sold_as_package(excerpt) or _sold_as_package(money_excerpt):
        return _refuse(refusals, AMOUNT_NOT_UNIT_PRICE, amount_scope='package_total')
    # Every numeral used in a computed price must occur in the cited line.
    quoted_numbers = {float(x.replace(',', '')) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", money_excerpt)}
    if value not in quoted_numbers or (scope == 'single_item_total' and (not quantity or quantity not in quoted_numbers)):
        return _refuse(refusals, UNPARSEABLE_AMOUNT)
    # 一格并列多台设备：型号的序号与金额的序号必须一致，否则那是别的设备的价。
    if _sibling_alignment_conflicts(raw, excerpt, baseline.get('model'), value):
        return _refuse(refusals, UNPARSEABLE_AMOUNT)
    scale = {"元": 1, "万元": 10000, "千元": 1000}.get(raw.get("amount_unit"))
    if scale is None:
        return _refuse(refusals, UNPARSEABLE_AMOUNT)
    currency_prefix = raw.get('amount_unit') == '元' and bool(re.search(r'[￥¥]\s*\d', money_excerpt))
    # 单位可以印在表头或公告总价句里（引文只剩裸数字）；换算型刻度还要对得上账。
    declared_unit = None
    declared_text = ''
    if raw.get("amount_unit") not in money_excerpt and not currency_prefix:
        declared = declared_amount_unit(raw, source, excerpt, money_excerpt)
        if declared is None or declared[0] != raw.get("amount_unit"):
            return _refuse(refusals, UNPARSEABLE_AMOUNT)
        declared_unit, declared_text = declared
    # Bind the amount to its currency scale, not an unrelated model/quantity.
    money = re.findall(r'(\d[\d,]*(?:\.\d+)?)\s*(万元|千元|元)', money_excerpt)
    money.extend((number, '元') for number in re.findall(r'[￥¥]\s*(\d[\d,]*(?:\.\d+)?)(?![\d.]|\s*[万千])', money_excerpt))
    # 列名带单位、数值在冒号后：``单价(元)：17480``。单位在数字**前面**，上面两条都不认
    # （一条要单位在后，一条要货币符号），于是 money 为空、下一行直接拒收。CCGP 的中标
    # 公告明细恰好是这个形状——「规格型号：XKA-2200 数量：1 单价(元)：17480」——一条
    # 抓到了正文、模型也读对了的成交单价，就因为这里绑不上币种刻度而进不了 prices[]。
    # 只认「价格标签 + 括号里的货币单位 + 冒号 + 数字」这个窄形状，不放开裸数字，
    # 免得把「数量 1」和一个别处的「元」绑成一条金额。
    money.extend((number, {'人民币': '元'}.get(declared, declared)) for declared, number in re.findall(
        r'(?:单价|成交价|中标价|合同单价)\s*[（(]\s*(万元|千元|元|人民币)\s*[）)]\s*[:：]\s*(\d[\d,]*(?:\.\d+)?)',
        money_excerpt))
    if not any(float(number.replace(',', '')) == value and unit == raw['amount_unit'] for number, unit in money):
        # 单位来自表头/总价声明时引文里只有裸数字；数字本身仍必须在引文里。
        if declared_unit != raw['amount_unit'] or value not in quoted_numbers:
            return _refuse(refusals, UNPARSEABLE_AMOUNT)
    if scope == 'unit' and not re.search(r'单价|每台|每套|售价|价格|price', money_excerpt + ' ' + declared_text, re.I):
        return _refuse(refusals, AMOUNT_NOT_UNIT_PRICE, amount_scope=scope)
    if scope == 'single_item_total' and not re.search(r"数量|台|套|件", money_excerpt):
        return _refuse(refusals, AMOUNT_NOT_UNIT_PRICE, amount_scope=scope)
    if re.search(r'项目总|总中标金额|总成交金额|采购包总|标包总', excerpt):
        return _refuse(refusals, TOTAL_NOT_UNIT_PRICE)
    if raw.get('currency', 'CNY') != 'CNY':
        return _refuse(refusals, UNPARSEABLE_AMOUNT)  # foreign currencies require an explicit currency parser
    unit_price = value * scale / (quantity if scope == 'single_item_total' else 1)
    official = source.get("source_type") in {"manufacturer_website", "manufacturer_download", "tender_document"}
    if not official:
        return _refuse(refusals, SOURCE_NOT_TENDER)
    if kind == 'official_list' and source.get('source_type') == 'tender_document':
        return _refuse(refusals, SOURCE_NOT_TENDER)
    if kind in {'award', 'contract'} and source.get('source_type') != 'tender_document':
        return _refuse(refusals, SOURCE_NOT_TENDER)
    raw = dict(raw)
    # Table extraction often has no date field of its own. Carry a date from
    # the source metadata or the cited announcement text into the row, while
    # retaining whether it was actually located in the public evidence.
    if raw.get('date') in (None, '') and source.get('publication_date'):
        raw['date'] = source['publication_date']
    # Business conditions must be literally grounded too, not just the amount.
    business_keys = ('configuration', 'tax', 'installation', 'warranty', 'delivery')
    additional_keys = ('shipping', 'service_response', 'service_onsite')
    # Reporting "未提供" on its own hides *why*, and the two causes read very
    # differently to a buyer: the source may simply never state the term, or the
    # extractor may have read something that cannot be tied back to its own
    # words -- worth a look rather than silent erasure.
    #
    # The reason must not claim more than was searched. This used to key off
    # ``parent_url``, because an attachment arrived as its own source and a
    # *page's* grounding text did not include the files it published. The
    # caller now hands in a source whose body already merges the notice with
    # its own attachments, so that discriminator no longer describes the text
    # in hand and neither old branch is right to print. "已取得的" is the part
    # that keeps the claim honest: it names the files we opened, not every file
    # the announcement may have published.
    searched = '公告正文及已取得的附件'
    field_notes = {}
    for key in (*business_keys, *additional_keys):
        # Ground against the announcement *and* its linked page -- for an
        # attachment that is the notice it was discovered from, which is where
        # the award wording lives. A buying unit's configuration usually lives
        # in the attached award/contract file rather than the notice body;
        # grounding on the notice alone discarded every configuration that was
        # correctly read from an attachment.
        if raw.get(key) and raw[key] != 'unknown' and not supported(raw[key], publisher_text):
            raw[key] = 'unknown'
            field_notes[key] = f'抽取结果无法与{searched}的原文逐字对应，已按未提供处理'
        elif not raw.get(key) or raw[key] == 'unknown':
            field_notes[key] = f'{searched}未提供该项'
    raw['field_notes'] = field_notes
    missing = []
    comparison_keys = (*business_keys, *(k for k in additional_keys if baseline.get(k) not in (None, '', 'unknown')))
    for key in comparison_keys:
        raw_value = raw.get(key)
        baseline_value = baseline.get(key)
        if not raw_value or raw_value == 'unknown':
            missing.append(key)
        elif baseline_value not in (None, '', 'unknown') and identity(raw_value) != identity(baseline_value):
            missing.append(key)
    # Brand mismatches were rejected above. Missing brand is retained as an
    # explicit gap and keeps the row as a visible model reference, but it does
    # not pass the same-model historical-comparability gate.
    brand_known = raw.get('brand') not in (None, '', 'unknown') and baseline.get('brand') not in (None, '', 'unknown')
    if not brand_known:
        missing.append('brand')
    # Brand + complete model + unit price + grounded date is sufficient to
    # retain a comparable historical price. Commercial terms and purchasing
    # volume are deliberately reported as gaps rather than hard gates.
    matching = brand_known
    if quantity is None or (amount(baseline.get('quantity')) is not None and quantity != amount(baseline.get('quantity'))):
        missing.append('quantity')
    date_grounded = False
    stamp = None
    try:
        if raw.get('date') in (None, ''):
            found = re.search(r'(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?', publisher_text)
            if found:
                raw['date'] = f'{int(found.group(1)):04d}-{int(found.group(2)):02d}-{int(found.group(3)):02d}'
        stamp = date.fromisoformat(raw.get('date', ''))
        date_pattern = rf'{stamp.year}\s*[-/.年]\s*0?{stamp.month}\s*[-/.月]\s*0?{stamp.day}(?:日)?'
        grounded_text = publisher_text + '\n' + str(source.get('publication_date') or '')
        if not re.search(date_pattern, grounded_text):
            raw['date'] = None
            raise ValueError('ungrounded date')
        today = date.today()
        date_grounded = True
        recent = stamp <= today and (today - stamp).days <= 1096
    except (TypeError, ValueError):
        recent = False
        missing.append('date')
    historical_identity = kind in {'award', 'contract'} and date_grounded and stamp <= today and brand_known
    level = ('budget_reference' if kind in {'budget', 'ceiling'} else
             'current_comparable' if kind == 'official_list' and matching and recent and (today - stamp).days <= 1 else
             'historical_configuration' if historical_identity and matching else
             'model_reference')
    quote = {**raw, 'unit_price': unit_price, 'currency': raw.get('currency', 'CNY'), 'url': source['url'],
             'level': level, 'gaps': missing, 'recent': recent, 'date_verified': date_grounded,
             'comparison_level': ('historical_configuration' if historical_identity and matching else 'model_reference'),
             'calculation': f'{value} × {scale}' + (f' ÷ {quantity}' if scope == 'single_item_total' else '')}
    supplier = amount(baseline.get('supplier_price'))
    quote['delta'] = supplier - unit_price if supplier is not None and level == 'historical_configuration' and quote['currency'] == baseline.get('currency', 'CNY') else None
    return quote


#: ``gaps`` 里报价条件之外还可能出现的字段名 → 中文。报价条件（tax／delivery／
#: installation／warranty…）的中文名复用 ``question_service.CONDITION_LABELS``，
#: 见 ``missing_condition_label``——同一个字段不许在两处印出两个名字。
_MISSING_CONDITION_LABELS = {
    'configuration': '配置',
    'shipping': '运费',
    'service_response': '报修响应',
    'service_onsite': '上门处理',
    'brand': '品牌',
    'quantity': '数量',
    'date': '公告日期',
}


def missing_condition_label(gap):
    """把 ``gaps`` 里的内部字段名翻成印给用户看的中文。

    迭代规范（2026-10）：缺口提醒原来直接把 ``delivery``、``installation`` 这类内部字段名
    join 给用户看，界面上是一串英文 key。这里按「先复用已有映射、再补人话」的顺序翻；
    两边都不认识的原样返回（宁可露出字段名，也不要猜一个错的中文上去）。

    延迟导入 ``question_service``：那个模块在 import 期会拉 ``backend.domain.*``，
    这里在调用期取，避免给判定链路的模块加载顺序加一条没必要的边。
    """
    from backend.services.question_service import CONDITION_LABELS
    return CONDITION_LABELS.get(gap) or _MISSING_CONDITION_LABELS.get(gap) or gap


def price_assessment(prices, baseline):
    """Describe transaction evidence without inventing a current fair-price band."""
    prices = [q for q in prices if q.get('kind') in {'award', 'contract'}]
    supplier = amount(baseline.get('supplier_price'))
    if supplier is None:
        return '尚未提供销售报价。可以先查历史成交，补上销售单价后再比较。'
    # 报价挂在销售口述的型号上、比较基准是产品资料里的型号时，底下这些成交记录
    # 查的是比较基准那一台。把它们和这份报价放在一起比，等于把一台机器的报价
    # 安到另一台头上；先说明这件事，不生成任何比价结论。
    quoted = str(baseline.get('quote_model') or '').strip()
    basis = str(baseline.get('model') or '').strip()
    if quoted and quoted != basis:
        return (f'销售报价 {format_money(supplier)} 元对应的是 {quoted}，'
                f'本次比较基准是产品资料里的 {basis}，不是同一台机器。'
                f'因此这份报价不与 {basis} 的成交记录直接比较；'
                f'要判断 {basis} 的价格，需要销售按 {basis} 重新报价。')
    if baseline.get('currency') != 'CNY':
        return '销售报价币种尚未确认为人民币，暂不与人民币成交金额直接比较。'
    if not prices:
        return '还没有取得同型号的成交单价，暂不能判断这份报价是否合理。'
    comparable = [q for q in prices if q.get('level') == 'historical_configuration']
    if not comparable:
        return f'找到{len(prices)}条同型号历史成交，可作为线索参考；公告日期或商务条件尚未完整核实，不能据此认定报价偏贵或便宜。'
    gap_keys = sorted({gap for q in comparable for gap in q.get('gaps', [])})
    low, high = min(q['unit_price'] for q in comparable), max(q['unit_price'] for q in comparable)
    spread = high / low if low > 0 else None
    too_wide = spread is not None and spread >= DISPERSION_REFUSAL_RATIO
    # ``configuration`` is never verifiable by the predicate stack: it can prove
    # brand, model, unit price and date, but never that two machines are the same
    # build. So when it is missing it is the reason for refusing a band, and
    # repeating it in the trailing reminder would just be noise.
    missing_rest = [gap for gap in gap_keys if gap != 'configuration']
    reminder = (f'；仍需核对缺失条件：{"、".join(missing_condition_label(gap) for gap in missing_rest)}' if missing_rest
                else '；仍需重新确认当前供货及服务承诺')

    # Two conditions stop a min-max band from being printed. Both come from the
    # same principle: a band asserts that the records are one comparable
    # population, which the evidence has not established.
    if 'configuration' in gap_keys:
        listed = '、'.join(f'{format_money(q["unit_price"])}元（{q.get("date") or "日期未核实"}）' for q in comparable)
        extra = (f'两者相差 {spread:.1f} 倍，这个差距本身也说明它们很可能不是同一配置，'
                 '更不能合并成一个区间。' if too_wide else '')
        return (f'查到{len(comparable)}条同型号历史成交：{listed}。'
                f'这些记录的配置口径都未核实，不能合并成一个价格区间。{extra}{reminder.lstrip("；")}。')
    if too_wide:
        return (f'查到{len(comparable)}条同型号历史成交，但高低相差 {spread:.1f} 倍'
                f'（{format_money(low)}–{format_money(high)}元）。差距过大说明口径不一致，不能给出一个价格区间{reminder}。')

    relation = '高于' if supplier > high else '低于' if supplier < low else '处于'
    return f'销售单价{format_money(supplier)}元，{relation}本次查到的同型号历史成交范围{format_money(low)}–{format_money(high)}元（{len(comparable)}条）。这是历史比较，不是当前市场公允价{reminder}。'


def price_decision(prices, baseline, lane='completed'):
    """Persist the decision and its exact inputs; reports never reinterpret it."""
    accepted = [q for q in prices if q.get('kind') in {'award', 'contract'}]
    comparable = [q for q in accepted if q.get('level') == 'historical_configuration']
    state = ('not_researched' if (lane == 'pending' and not accepted) else 'no_transaction' if not accepted
             else 'historical_comparable' if comparable else 'historical_reference')
    summary = ('尚未启动成交项目核价，当前不能判断销售报价。' if state == 'not_researched'
               else price_assessment(accepted, baseline))
    return {'schema_version': 1, 'state': state, 'summary': summary,
            'fair_market_price_established': False, 'supplier_price': baseline.get('supplier_price'),
            'currency': baseline.get('currency'), 'model': baseline.get('model'),
            'transaction_count': len(accepted), 'comparable_count': len(comparable),
            'references': [{'url': q['url'], 'unit_price': q['unit_price'],
                            'date': q.get('date'), 'level': q.get('level'), 'comparison_level': q.get('comparison_level'),
                            'date_verified': q.get('date_verified', False),
                            'gaps': q.get('gaps', [])} for q in accepted],
            'next_action': '请销售按实际用途补齐配置、税运安装及服务承诺，提供完整方案总价。'}


UNITS = {'ml': ('volume', 1), 'l': ('volume', 1000), 'μl': ('volume', .001),
         'mm': ('length', 1), 'cm': ('length', 10), 'm': ('length', 1000),
         'g': ('mass', 1), 'kg': ('mass', 1000), 'mg': ('mass', .001),
         '℃': ('temperature', 1), '°c': ('temperature', 1), 'rpm': ('rpm', 1),
         'r/min': ('rpm', 1), 'xg': ('rcf', 1), '×g': ('rcf', 1), 'db': ('noise', 1), 'dba': ('noise_a', 1),
         '%': ('percent', 1), '': ('unitless', 1)}

#: 单位写法 → ``UNITS`` 认得的键。**只有这一份**：读正式候选配置对比矩阵的那条路
#: （``parameter_row`` → ``evaluate_parameter``）与读候选卡片的那条路
#: （``research_planning.configuration_alignment``）都调 ``canonical_unit``。
#: 两份各写各的时候，同一份正文里同一个数会被两条路判出两个结论（见下面那个函数）。
_UNIT_ALIASES = {'xg': '×g', '×g': '×g', 'rcf': '×g',
                 'rpm': 'r/min', 'r/min': 'r/min', '转/分': 'r/min', '转每分': 'r/min'}


def canonical_unit(unit):
    """单位写法归一化：``× g``／``xg``／``rcf`` → ``×g``，``rpm``／``转/分`` → ``r/min``。

    括号里那一层也算候选写法。厂商页把参数写成一行时，单位常印成
    ``10000rpm (转/分)`` —— 整串拿去查 ``UNITS`` 查不到，于是**读到了值却因为
    写法不同被判成"未知"**。真跑实测（``08-run-off.json`` 的 ``GL-10000C``）：
    正文原文是 ``安亭GL-10000C：最高转速：10000rpm (转/分)；…``，矩阵行
    ``observed`` 拿到了 ``10000`` 但 ``status`` 是 ``unknown``（单位 ``rpm (转/分)``
    查不到），而候选卡片那条路把同一个数归一到 ``r/min`` 后判 ``violated`` ——
    同一条正文、两条路、两个结论。

    认不出来的写法**原样返回**，不猜、不补：``根毛细管`` 这种量纲本来就不在
    ``UNITS`` 里，归一化也不会把它变成一个能比较的单位（那一项该是"未知"）。
    """
    text = str(unit or '').strip()
    if not text:
        return ''
    candidates = [text, re.split(r'[（(\[【]', text, 1)[0]]
    candidates += re.findall(r'[（(\[【]([^）)\]】]*)[）)\]】]', text)
    for part in candidates:
        try:
            flat = identity(part)
        except (TypeError, ValueError):
            continue
        if flat in _UNIT_ALIASES:
            return _UNIT_ALIASES[flat]
    return text


#: 从一个可能带尾巴的单位写法里认单位时用的词表：**长的先试**，免得"米"先把"毫米"抢走。
_UNIT_TOKENS = tuple(sorted(
    [(key, value) for key, value in UNITS.items() if key] +
    [('升', ('volume', 1000)), ('毫升', ('volume', 1)), ('微升', ('volume', .001)),
     ('千克', ('mass', 1000)), ('公斤', ('mass', 1000)), ('克', ('mass', 1)),
     ('毫克', ('mass', .001)), ('厘米', ('length', 10)), ('毫米', ('length', 1)),
     ('分钟', ('time', 60)), ('小时', ('time', 3600)), ('秒', ('time', 1)),
     ('千瓦', ('power', 1000)), ('瓦', ('power', 1)), ('赫兹', ('frequency', 1)),
     ('千帕', ('pressure', 1)), ('兆帕', ('pressure', 1000))],
    key=lambda item: -len(item[0])))

#: ``unit`` 字段里捎带的范围尾巴：``-135℃``、``～60℃``、``至 135 ℃``。
_RANGE_TAIL_RE = re.compile(r'^\s*(?:[-~～—]|至)\s*(\d+(?:\.\d+)?)')

#: 展示用写法（键是 ``UNITS`` / 词表里的小写键）。判决只看量纲，这里只影响报告上印什么。
_UNIT_DISPLAY = {'l': 'L', 'ml': 'mL', 'μl': 'μL', '°c': '℃', '℃': '℃',
                 'rpm': 'r/min', 'db': 'dB', 'dba': 'dBA', 'kw': 'kW', 'w': 'W', 'hz': 'Hz',
                 'xg': '×g', '×g': '×g'}


def unit_scale(unit):
    """单位 → ``(量纲, 换算系数)``；整串认不出来时，在串里找第一个认识的单位词。

    真跑实测（2026-10-05 批3 灭菌器）：模型的单位字段会捎带范围与说明——
    ``-135℃（可调``、``直径*71深 cm``、``分钟-1周延迟``。原先整串查 ``UNITS`` 查不到
    就判 ``unknown``，于是"要求 50 升、正文读到 50 L"这种明明有值的项一条都算不出来
    （24 项里读到值的 5 项全部 unknown）。整串先查、再找单位词，两步都只是查表，不猜。
    """
    text = canonical_unit(unit)
    direct = UNITS.get(str(text).lower())
    if direct:
        return direct
    flat = identity(text)
    if not flat:
        return None
    for token, scale in _UNIT_TOKENS:
        if identity(token) in flat:
            return scale
    return None


def unit_label(unit):
    """单位串里第一个认识的单位词（用于展示），认不出来时原样返回。"""
    text = str(unit or '').strip()
    flat = identity(text)
    for token, _scale in _UNIT_TOKENS:
        if identity(token) and identity(token) in flat:
            return _UNIT_DISPLAY.get(str(token).lower(), str(token))
    return text


def split_requirement_range(required):
    """把模型塞在 ``unit`` 里的范围尾巴搬回 ``value``，并标成区间要求。

    实测写法：``{'value': 105, 'unit': '-135℃（可调'}`` —— 它表达的是"105~135℃"，
    上界被写进了单位字段。不搬回来的话，这一项要么被当成"必须等于 105"（观测 120
    会被判成不满足，是**错判**），要么整项读不出来。搬回来之后它就是一条正常的
    区间要求，判定仍交回 ``evaluate_parameter``。

    只在"value 是单值、unit 以范围符＋数字开头"时动手；读不出上下界就原样返回。
    """
    if not isinstance(required, dict):
        return required
    unit = str(required.get('unit') or '')
    if isinstance(required.get('value'), (list, tuple)):
        return required
    match = _RANGE_TAIL_RE.match(unit)
    if not match:
        return required
    try:
        head = float(required['value'])
        tail = float(match.group(1))
    except (TypeError, ValueError, KeyError):
        return required
    low, high = (head, tail) if head <= tail else (tail, head)
    rest = unit[match.end():]
    label = unit_label(rest) or unit_label(unit)
    return {**required, 'value': [low, high], 'direction': 'covers', 'unit': label}


def evaluate_parameter(required, observed):
    if not observed or not observed.get('evidence'):
        return 'unknown'
    required = split_requirement_range(required)
    if identity(required.get('conditions')) != identity(observed.get('conditions')):
        return 'unknown'
    direction = required.get('direction', 'equal')
    if direction == 'equal':
        expected, actual = amount(required.get('value')), amount(observed.get('value'))
        if expected is not None and actual is not None:
            ru = unit_scale(required.get('unit'))
            ou = unit_scale(observed.get('unit'))
            if not ru or not ou or ru[0] != ou[0]:
                return 'unknown'
            return 'satisfied' if math.isclose(expected * ru[1], actual * ou[1], rel_tol=1e-9) else 'violated'
        if identity(required.get('unit')) != identity(observed.get('unit')):
            return 'unknown'
        # Different prose is not proof that a functional requirement is violated.
        # Qualitative requests need clarified criteria or equivalent evidence.
        return 'satisfied' if identity(required.get('value')) == identity(observed.get('value')) else 'unknown'
    ru = unit_scale(required.get('unit'))
    ou = unit_scale(observed.get('unit'))
    if not ru or not ou or ru[0] != ou[0]:
        return 'unknown'
    try:
        if direction == 'covers':
            low, high = [float(x) * ru[1] for x in required['value']]
            observed_value = observed['value']
            if isinstance(observed_value, (list, tuple)) and len(observed_value) == 2:
                actual_low, actual_high = [float(x) * ou[1] for x in observed_value]
                if not all(math.isfinite(v) for v in (low, high, actual_low, actual_high)) or low > high or actual_low > actual_high:
                    return 'unknown'
                ok = actual_low <= low <= high <= actual_high
            else:
                # 观测只有一个点（页面上就印了一个数）：证明不了"覆盖整个范围"，
                # 但能证明这个点落没落在范围内。落进去记 satisfied、落在外面记
                # violated——两种都是读到数值后的确定结论，不拿"没读全"冒充"不满足"。
                actual = float(observed_value) * ou[1]
                if not math.isfinite(actual) or low > high:
                    return 'unknown'
                ok = low <= actual <= high
        else:
            expected = float(required['value']) * ru[1]
            actual = float(observed['value']) * ou[1]
            if not math.isfinite(expected) or not math.isfinite(actual):
                return 'unknown'
            if direction not in {'min', 'max'}:
                return 'unknown'
            ok = actual >= expected if direction == 'min' else actual <= expected
        return 'satisfied' if ok else 'violated'
    except (ValueError, TypeError, KeyError):
        return 'unknown'


def historical_summary(prices):
    from statistics import median
    groups = {}
    for q in prices:
        project = q.get('project_id')
        if q.get('level') != 'historical_configuration' or not project or not supported(project, q.get('excerpt', '')):
            continue
        key = tuple(identity(q.get(k)) for k in ('brand', 'model', 'currency'))
        groups.setdefault(key, {})[identity(project)] = q['unit_price']
    return [{'count': len(values), 'median': median(values.values()), 'currency': key[2].upper(), 'note': '同品牌同型号历史成交汇总，不是当前报价；配置和商务条件仍需逐条核对'} for key, values in groups.items() if len(values) >= 3]


def alternative_conclusion(base, original_prices, candidate):
    disposition = candidate.get('comparison_disposition')
    # 本轮一份必须满足的指标都没有确认过时，不能出现"满足全部关键指标"这类结论：
    # 没有任何约束被核验过，那句话在这里没有依据（见 ``comparison_basis``）。
    if candidate.get('comparison_basis') == NO_CONFIRMED_PARAMETER:
        return '本轮没有确认必须满足的实验要求，未做逐项参数比对；只能按价格与来源比较，不能判定关键参数满足或不满足'
    if disposition == 'comparable_candidate':
        score = candidate.get('similarity_score')
        percent = f"{score:.0%}" if isinstance(score, (int, float)) else '达到阈值'
        differences = [row.get('name') for row in candidate.get('difference_parameters', []) if row.get('name')]
        missing = [row.get('name') for row in candidate.get('missing_parameters', []) if row.get('name')]
        detail = []
        if differences:
            detail.append('已核实差异：' + '、'.join(differences))
        if missing:
            detail.append(f'仍缺证：另有 {len(missing)} 项')
        return f'可比较候选（已核验满足比例 {percent}）' + ('；' + '；'.join(detail) if detail else '')
    if disposition == 'recommended_comparison':
        return '推荐比较：全部已列关键指标均有可核验证据；仍需单独核对价格、配置和售后'
    if candidate['status'] == 'ineligible':
        return '存在取舍：关键指标不满足'
    if candidate['status'] != 'eligible':
        # 候选品是**按配置定位**出来的：它自己的网页上写着该型号的参数，说明这台对得上、
        # 配置与用户给的样品在同一个档位上（设计规范：「候选品的配置就是
        # 在用户提供的样品配置的左右即可」）。这时不再印"待核实"——那句话表达的只是
        # "没做跨品牌核验"，却会让用户觉得这份结果不可信。
        # **也不写"满足"**：网页上有配置只证明型号对得上，参数值达不达标要看参数表自己，
        # 写成满足等于替用户下了他没下的结论。
        if candidate.get('configuration'):
            return '同档候选：配置与样品相当，产品参数见下'
        return '同档候选：尚未取到该型号的产品参数'
    comparable = [q for q in candidate.get('prices', []) if q['level'] == 'current_comparable']
    originals = [q for q in original_prices if q['level'] == 'current_comparable']
    for q in comparable:
        for original in originals:
            if q['currency'] == original['currency'] and all(identity(q.get(k)) == identity(original.get(k)) for k in ('tax', 'installation', 'warranty', 'delivery', 'quantity')) and q['unit_price'] < original['unit_price']:
                return '同档且可比价格更低'
    if any(q['level'] == 'historical_configuration' for q in candidate.get('prices', [])):
        return '同档；已有历史价格参考，暂无当前成交价'
    return '同档；暂无成交价记录'


def comparison_details(baseline, candidate, sources):
    """Describe measured margins and preference evidence without relaxing gates."""
    stronger = []
    for row in candidate.get('matrix', []):
        req, obs = row['required'], row.get('observed')
        if row['status'] != 'satisfied' or not obs or req.get('direction') not in {'min', 'max', 'covers'}:
            continue
        reverse = {**req, 'direction': {'min': 'max', 'max': 'min', 'covers': 'covers'}[req['direction']]}
        if req['direction'] == 'covers':
            reverse = {**obs, 'direction': 'covers'}
            same = evaluate_parameter(reverse, {**req, 'evidence': True})
        else:
            same = evaluate_parameter(reverse, obs)
        if same == 'violated':
            stronger.append({'name': req['name'], 'required': req, 'observed': obs})
    preferences = []
    for term in dict.fromkeys(t.strip() for t in re.split(r'[，,；;\n、]+', str(baseline.get('preferences') or '')) if t.strip()):
        evidence = []
        for source in sources:
            if source.get('body_status') != 'fetched' or source.get('source_type') not in {'manufacturer_website', 'manufacturer_download'}:
                continue
            for line in source.get('body_text', '').splitlines():
                if contains_model(line, candidate['model']) and term in line:
                    evidence.append({'url': source['url'], 'excerpt': line})
        preferences.append({'preference': term, 'status': 'needs_review' if evidence else 'unknown', 'evidence': evidence,
                            'note': '找到同型号原文，功能、选配及适用条件仍需确认' if evidence else '尚无同型号官方原文，不推断具备或不具备'})
    return {'stronger_parameters': stronger, 'preference_checks': preferences}


def comparison_basis(parameters):
    """说明逐项参数比对这一轮**有没有可比的东西**。

    用户没有必须满足的实验要求时，基准里的 ``parameters`` 就是空的；这时
    "满足 0/0 项"在数学上会算成满分，在采购结论里却是无依据的——一款候选
    没有被任何指标约束过，既不能叫"满足全部要求"，也不能叫"不满足"。
    所以比对之前先问"有没有确认过指标"，把这个状态一路带到结论里，
    而不是让下游从"候选状态"或"条数"往回猜。
    """
    confirmed = [p for p in (parameters or []) if isinstance(p, dict) and p.get('name')]
    return {'comparison_basis': CONFIRMED_PARAMETERS if confirmed else NO_CONFIRMED_PARAMETER,
            'confirmed_parameter_count': len(confirmed)}


def similarity_assessment(matrix, threshold: float = 0.70, confirmed_parameter_count=None):
    """Classify a candidate against confirmed, comparable key parameters.

    Only non-provisional ``satisfied``/``violated`` rows enter the score.
    Unknown, conflicted, and provisional observations are reported as gaps and
    never become implicit matches. The legacy candidate status remains owned by
    the stricter all-parameter gate; this separate disposition allows a 70%
    candidate into comparison without calling it fully compliant.

    ``confirmed_parameter_count`` 是这一轮确认过的指标条数（见
    ``comparison_basis``）。不传时按矩阵行数推断，保持既有调用的行为不变；
    显式传 0 时结论一律是"本轮没有确认可比指标"，不产出任何满足比例。
    """
    rows = [row for row in (matrix or []) if isinstance(row, dict)]
    # 不传就沿用旧口径（矩阵里有行就算比过），既有调用点的行为不变；显式传这一轮
    # 确认过的指标条数（``len(baseline['parameters'])``）时才用得到"没有指标"这一态。
    if confirmed_parameter_count is None:
        comparison_state = {'comparison_basis': CONFIRMED_PARAMETERS if rows else NO_CONFIRMED_PARAMETER,
                            'confirmed_parameter_count': len(rows)}
    else:
        comparison_state = {'comparison_basis': CONFIRMED_PARAMETERS if int(confirmed_parameter_count) else NO_CONFIRMED_PARAMETER,
                            'confirmed_parameter_count': int(confirmed_parameter_count)}
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        threshold = 0.70
    if not math.isfinite(threshold):
        threshold = 0.70
    threshold = min(1.0, max(0.0, threshold))

    confirmed_satisfied = [
        row for row in rows
        if row.get('status') == 'satisfied' and row.get('evidence_status') != 'provisional' and not row.get('conflict')
    ]
    confirmed_violated = [
        row for row in rows
        if row.get('status') == 'violated' and row.get('evidence_status') != 'provisional' and not row.get('conflict')
    ]
    missing = [
        row for row in rows
        if row.get('status') not in {'satisfied', 'violated'}
        or row.get('evidence_status') == 'provisional'
        or row.get('conflict')
    ]
    comparable_count = len(confirmed_satisfied) + len(confirmed_violated)
    # The score describes only parameters with confirmed, comparable evidence.
    # Missing evidence is never a match and is surfaced separately below.
    # 本轮没确认过指标时连比例都不给：没有可比项，"全部满足"是空话。
    no_basis = comparison_state['comparison_basis'] == NO_CONFIRMED_PARAMETER
    score = None if no_basis else ((len(confirmed_satisfied) / comparable_count) if comparable_count else None)
    threshold_met = score is not None and score >= threshold
    if not threshold_met:
        disposition = 'needs_review'
    elif confirmed_violated or missing:
        disposition = 'comparable_candidate'
    else:
        disposition = 'recommended_comparison'
    return {
        **comparison_state,
        'parameter_comparison_performed': not no_basis,
        'similarity_score': score,
        'similarity_threshold': threshold,
        'similarity_counts': {
            'satisfied': len(confirmed_satisfied),
            'different': len(confirmed_violated),
            'missing': len(missing),
            'comparable': comparable_count,
            'total': len(rows),
        },
        'comparison_disposition': disposition,
        'satisfied_parameters': [row['required'] for row in confirmed_satisfied if row.get('required')],
        'difference_parameters': [row['required'] for row in confirmed_violated if row.get('required')],
        'missing_parameters': [row['required'] for row in missing if row.get('required')],
        'unknown_parameters': [row['required'] for row in missing if row.get('required')],
        'difference_summary': '；'.join(filter(None, [
            ('本轮没有确认必须满足的实验要求，未做逐项参数比对' if no_basis else
             f'满足 {len(confirmed_satisfied)}/{comparable_count} 项已核验指标' if comparable_count else '没有可核验的可比指标'),
            f'明确差异 {len(confirmed_violated)} 项' if confirmed_violated else '',
            f'缺少证据 {len(missing)} 项' if missing else '',
        ])),
    }
