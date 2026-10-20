"""Explain why a price candidate was not adopted, instead of showing only zero.

The 「认出」 step already refuses a model-mismatched award (a sibling model priced far from the target
``sibling model is correctly not treated as the
``target model's price). The user-visible problem was that ``prices = []`` came with
no explanation, so a correct refusal was indistinguishable from "the system
found nothing".

This module produces one machine-readable reason per candidate, derived from the
same predicates the acceptance path uses -- it never re-decides, never promotes
a rejected amount, and never invents a price. It exists purely so the report can
say *which* condition failed and *where* the amount was seen.

Design rules:

* Every reason is tied to a literal excerpt and a source URL, so the claim is
  auditable against the frozen snapshot.
* ``reason_code`` is a stable identifier for the UI/tests; ``detail`` is the
  human sentence.
* A candidate that would in fact be accepted is *not* listed here. The caller
  passes only rows that ``validate_price`` already rejected.
"""
from __future__ import annotations

import re

from backend.services.procurement_rules import (
    AMOUNT_NOT_UNIT_PRICE,
    BODY_UNAVAILABLE,
    BRAND_MISMATCH,
    BUDGET_NOT_ACHIEVED,
    EXCERPT_NOT_IN_BODY,
    MODEL_MISMATCH,
    NOT_AWARDED_CANDIDATE,
    NOT_AWARD_EVIDENCE,
    SOURCE_NOT_TENDER,
    TOTAL_NOT_UNIT_PRICE,
    UNKNOWN_REASON,
    UNPARSEABLE_AMOUNT,
    contains_model,
    identity,
    validate_price,
)

#: The reason codes themselves are owned by the acceptance gate
#: (``procurement_rules``) and imported above: each one names the predicate that
#: actually stopped ``validate_price``. Keeping a second, ordered list here is
#: what let a row refused on one ground be explained on another.
#:
#: ``validate_price`` never refuses a row over a missing date: an ungrounded date
#: lowers the comparability level (``model_reference``) and is reported as a gap,
#: but the row still passes. So this code is no longer produced by
#: :func:`explain_rejection`; it stays declared because the UI still maps it and
#: stored runs from earlier rounds carry it.
DATE_UNGROUNDED = 'date_ungrounded'
#: Declared for the UI label table; no predicate currently produces it.
QUANTITY_UNVERIFIED = 'quantity_unverified'
SELLER_SELF_DESCRIPTION = 'seller_self_description'
UNKNOWN = UNKNOWN_REASON

#: A model-looking token: letters+digits, e.g. HC-3016R, H1850R, AS185R.
_MODEL_TOKEN = re.compile(r'(?<![A-Za-z0-9])(?=[A-Za-z0-9-]*[A-Za-z])(?=[A-Za-z0-9-]*\d)'
                          r'[A-Za-z0-9][A-Za-z0-9-]*(?![A-Za-z0-9])')

#: Tokens that merely *look* like a model because a unit meets a number, such as
#: ``6min`` (6 minutes), ``50ml`` (50 millilitres) or ``55dB`` (noise level).
#: Real announcements are full of these and reporting one as "the model did not
#: match" would be wrong.
_SPEC_TOKEN = re.compile(
    r'^\d+(?:\.\d+)?(?:min|max|sec|h|ml|ul|l|kg|g|mg|kw|kv|w|v|ma|a|mm|cm|m'
    r'|hz|khz|rpm|mpa|bar|pa|db|℃|°c|%)$',
    re.I)
#: The same shape with the unit first (``min59``), seen in a real announcement.
_UNIT_FIRST_TOKEN = re.compile(
    r'^(?:min|max|sec|ml|ul|kg|kw|kv|rpm|hz|khz|mm|cm|mpa|db)\d+$', re.I)
#: Fill/dimension formats such as ``6x50ml`` or ``12x15ml`` (rotor capacity).
_DIMENSION_TOKEN = re.compile(r'^\d+(?:\.\d+)?\s*x\s*\d+', re.I)
#: HTML/CSS/JS/library identifiers and standards that survive cleaning as bare
#: words (``dtype1``, ``hot122703``, ``ISO9001``).
_CODE_LIKE_TOKEN = re.compile(
    r'^(?:hot|dtype|gk|var|div|span|img|td|tr|th|class|id|src|href|http|www|iso)', re.I)

_MONEY = re.compile(r'(?:[￥¥]\s*[\d,]+(?:\.\d+)?|[\d,]+(?:\.\d+)?\s*(?:万元|千元|元))')
#: The wording predicates that used to live here (decision wording, budget,
#: project total, loose date) are gone on purpose: they are the acceptance gate's
#: business, and ``procurement_rules.validate_price`` owns them. A private copy
#: is how the explainer came to report predicates that never ran.


def _is_plausible_model(token: str, target_model: str) -> bool:
    """Whether *token* can honestly be called a model in a rejection message.

    A false positive here is worse than a miss: the user would be told "the model
    is X, not your model" about something that was never a model. Measured on
    real announcements, the naive letters+digits rule yields ``hot122703``,
    ``dtype1``, ``01000100000aa``, ``QDU20260094`` (a project code), ``6min`` and
    ``50ml`` -- all noise.
    """
    if identity(token) == identity(target_model):
        return False
    if not 4 <= len(token) <= 24:
        return False
    if _SPEC_TOKEN.match(token) or _UNIT_FIRST_TOKEN.match(token):
        return False
    if _DIMENSION_TOKEN.match(token):
        return False
    if _CODE_LIKE_TOKEN.match(token):
        return False
    letters = sum(character.isalpha() for character in token)
    digits = sum(character.isdigit() for character in token)
    if letters < 2 or not digits:
        return False
    # A long digit run, or a token that is mostly digits, is a code or a
    # quantity (asset number, project number), not an instrument model.
    if re.search(r'\d{6,}', token) or digits > letters * 2:
        return False
    return True


def _other_models(excerpt: str, target_model: str) -> list[str]:
    """Plausible model tokens in the excerpt that are not the target model."""
    seen: list[str] = []
    for token in _MODEL_TOKEN.findall(excerpt):
        if not _is_plausible_model(token, target_model):
            continue
        if token not in seen:
            seen.append(token)
    return seen


def candidate_rank_of(row: dict, source: dict | None):
    """The candidate rank that disqualifies this row, or ``None`` if none does.

    A losing bid is not a transaction. This is the structural test that the
    keyword test ``kind='award'`` cannot make: some award announcements are titled
    「中标（成交）结果公告」 and contains the words 中标/成交, so every row in it
    passed the award gate -- including a losing bidder's offer for
    the only unit of the target model in the document. The winner never offered that model.

    Every occurrence of the excerpt is checked, not just the first. A result
    notice can list the same item at the same price under more than one
    candidate, and a plain ``body.find`` would attribute such a row to whichever
    section happens to come first -- which may be the winner's. This fails
    closed: if *any* occurrence sits in a non-winner section, the row is refused.
    """
    from backend.services.procurement_tables import candidate_section_ordinal
    body = (source or {}).get('body_text') or ''
    excerpt = str(row.get('excerpt') or '')
    if not excerpt:
        return None
    ranks, start = [], body.find(excerpt)
    while start != -1:
        ranks.append(candidate_section_ordinal(body, start))
        start = body.find(excerpt, start + 1)
    non_winner = [rank for rank in ranks if rank is not None and rank != 1]
    return max(non_winner) if non_winner else None


def _rank_label(ordinal) -> str:
    names = {1: '一', 2: '二', 3: '三', 4: '四', 5: '五',
             6: '六', 7: '七', 8: '八', 9: '九', 10: '十'}
    return names.get(ordinal, str(ordinal))


def explain_rejection(row: dict, source: dict | None, target: dict,
                      refusals: dict | None = None) -> dict:
    """Return the single best explanation for why *row* was not adopted.

    ``row`` is a candidate price mapping (as produced by the table projector or
    the model extraction step). ``source`` is the captured source it cites.
    The returned dict always carries ``reason_code`` and ``detail``; evidence
    fields are added only when they genuinely exist in the captured text.

    ``reason_code`` is the predicate that ``validate_price`` *actually* stopped
    on. Callers that already ran the gate pass its ``refusals`` dict to reuse
    that verdict; otherwise the gate is run once here to obtain it. This function
    never re-decides and never promotes an amount.

    Running its own ordered list of predicates is exactly what this used to do,
    and it produced a false explanation the report then repeated: for the CCGP
    ``单价(元)：17480`` row, ``validate_price`` returned a quote while this
    function reported ``date_ungrounded`` -- a predicate that never executed.
    """
    target_model = str(target.get('model') or '')
    if not isinstance(row, dict):
        return {'url': (source or {}).get('url'), 'model': target_model, 'excerpt': '',
                'reason_code': UNKNOWN,
                'detail': '这条候选价格的结构无法解析，未参与单价谓词校验。'}
    excerpt = str(row.get('excerpt') or '')
    url = row.get('url') or (source or {}).get('url')
    base = {'url': url, 'model': target_model, 'excerpt': excerpt[:600]}

    if source is None:
        return {**base, 'reason_code': BODY_UNAVAILABLE,
                'detail': '这条金额所引用的来源不在本轮抓取结果里，无法回原文核对。'}

    if source.get('body_status') != 'fetched':
        return {**base, 'reason_code': BODY_UNAVAILABLE,
                'detail': f"来源正文未抓到（body_status={source.get('body_status')!r}），不能作为证据。"}

    if refusals is None:
        refusals = {}
        if validate_price(dict(row), source, target, refusals) is not None:
            # The gate accepted this row, so no refusal may be invented for it.
            # A row that reaches here is a caller bug, and saying so is the only
            # honest answer: the old explainer instead picked a predicate from
            # its own list and blamed that.
            return {**base, 'reason_code': UNKNOWN,
                    'detail': '这条金额实际通过了单价核验，没有触发任何拒绝判据，不应列入被拒清单。'}
    code = refusals.get('reason_code') or UNKNOWN

    # 1. Model identity: the excerpt names a sibling model, not the target model.
    if code == MODEL_MISMATCH:
        others = _other_models(excerpt, target_model)
        shown = '、'.join(others[:4]) if others else '（引文中未见其他型号）'
        return {**base, 'reason_code': code, 'other_models': others,
                'detail': (f'引文中的型号是 {shown}，与本次目标型号 {target_model} 不一致：'
                           '同品类的不同型号不能互相认定成交价。')}

    # 2. Brand identity.
    if code == BRAND_MISMATCH:
        brand = str(row.get('brand') or refusals.get('other_brand') or '')
        return {**base, 'reason_code': code, 'other_brand': brand,
                'detail': f"引文中的品牌是 {brand}，与目标品牌 {target.get('brand')} 不一致。"}

    # 3. A runner-up's bid is not a transaction. No matter how well-formed the
    # row is, this candidate did not win.
    if code == NOT_AWARDED_CANDIDATE:
        rank = refusals.get('candidate_rank')
        if rank is None:
            rank = candidate_rank_of(row, source)
        if rank is None:
            return {**base, 'reason_code': UNKNOWN,
                    'detail': '这条金额被候选排名判据拒绝，但未能复算候选名次；保留原文供人工核对。'}
        return {**base, 'reason_code': code, 'candidate_rank': rank,
                'detail': (f'这条金额属于“第{_rank_label(rank)}中标候选人”的投标清单，'
                           '该候选人未中标——投标报价不是成交价，不能作为比价基准。')}

    # 4. Budget / ceiling / project total must never become an achieved unit price.
    if code == BUDGET_NOT_ACHIEVED:
        return {**base, 'reason_code': code,
                'detail': '引文中的金额是采购预算或最高限价，不是实际成交单价。'}

    if code == TOTAL_NOT_UNIT_PRICE:
        return {**base, 'reason_code': code,
                'detail': '引文中的金额是项目/标包总金额，不能摊派成单台设备成交单价。'}

    # 5. Formal decision wording, or a notice that withdrew the decision.
    if code == NOT_AWARD_EVIDENCE:
        if refusals.get('revoked'):
            return {**base, 'reason_code': code,
                    'detail': '这份公告声明了废标或采购活动终止，其中的金额不是已成交的价格。'}
        return {**base, 'reason_code': code,
                'detail': '该页面没有出现“中标/成交”等已决标措辞，属于挂牌或介绍页，不是成交证据。'}

    # 6. Amount scale could not be bound to a unit price.
    if code == AMOUNT_NOT_UNIT_PRICE:
        scope = refusals.get('amount_scope', row.get('amount_scope'))
        detail = (f"金额口径无法确认为单台单价（amount_scope={scope!r}）。"
                  if scope not in {'unit', 'single_item_total'} else
                  '引文没有把这条金额写成单价（缺少“单价/每台/每套”等口径词），不能当成交单价。')
        return {**base, 'reason_code': code, 'amount_scope': scope, 'detail': detail}

    if code == UNPARSEABLE_AMOUNT:
        return {**base, 'reason_code': code,
                'detail': '引文里的金额与币种单位无法绑定成一条单价（原文没有可核对的单价写法），'
                          '不能确认单价数值。'}

    if code == EXCERPT_NOT_IN_BODY:
        return {**base, 'reason_code': code,
                'detail': '引文无法在抓取到的正文中逐字定位，或与公告表格的对应行不一致，不能作为证据。'}

    if code == SOURCE_NOT_TENDER:
        return {**base, 'reason_code': code,
                'detail': '这条金额的来源不是已完成的采购公告或合同文件，不能作为历史成交价证据。'}

    if code == BODY_UNAVAILABLE:
        return {**base, 'reason_code': code,
                'detail': '这条金额所引用的来源正文不可用，无法回原文核对。'}

    return {**base, 'reason_code': UNKNOWN,
            'detail': '该行未通过单价谓词校验，但未命中已知的具名原因；保留原文供人工核对。'}


def near_miss_rejections(sources, target, limit=12):
    """Explain official sources that carry a *different* model of the same kind.

    Why this exists
    ---------------
    The price lane selects sources with ``source_type == 'tender_document' AND
    contains_model(body, target_model)``. A formal announcement that prices a
    sibling model -- observed on a real award: a sibling model priced far away
    -- is therefore discarded *before* any price predicate runs, and the user
    only sees "0 条" with no reason. That is a correct refusal presented as an
    absence.

    This function looks at the same class of source (formal publications) that do
    NOT name the target model, extracts their priced rows, and reports why each
    was not adopted. It is strictly display-only: it is never merged into
    ``prices`` and cannot affect ``price_decision``.
    """
    from backend.services.procurement_tables import price_table_rows
    from backend.services.source_authority import publication_tier

    target_model = str(target.get('model') or '')
    found: list[dict] = []
    seen: set[tuple] = set()

    for source in sources or []:
        if source.get('body_status') != 'fetched':
            continue
        if source.get('source_type') != 'tender_document':
            continue
        body = source.get('body_text') or ''
        if not body or contains_model(body, target_model):
            # A source naming the target model is handled by the normal path.
            continue
        # Candidate tokens come from table-shaped lines only. Scanning the whole
        # body yielded ~95 "models" per announcement -- nearly all CSS/JS debris
        # -- and cost about a second per run.
        table_lines = [line for line in body.splitlines() if '|' in line]
        candidates_models: list[str] = []
        for line in table_lines:
            for token in _other_models(line, target_model):
                if token not in candidates_models:
                    candidates_models.append(token)

        for other in candidates_models:
            # Column-aware: ``price_table_rows`` only accepts the token when it
            # actually sits in the table's model column, which is what makes it
            # safe to name it in a user-facing message. Parameter-column values
            # such as 55dB or 6x50ml are rejected here.
            rows = price_table_rows(body, other)
            candidates = [{**row, 'kind': 'award', 'url': source.get('url')} for row in rows]
            if not candidates:
                # Headerless amount columns (分项报价 / 成交金额) carry a real
                # amount with no 单价 heading, so the projector finds nothing --
                # observed on a real university site. Fall back to the literal
                # priced row, but still require the model and the amount to share
                # one row so a parameter cannot be mistaken for a model.
                candidates = [
                    {'kind': 'award', 'url': source.get('url'), 'model': other,
                     'excerpt': line.strip()}
                    for line in table_lines
                    if _MONEY.search(line) and contains_model(line, other)
                ]
            for candidate in candidates:
                reason = explain_rejection(candidate, source, target)
                if reason.get('reason_code') != MODEL_MISMATCH:
                    continue
                key = (source.get('url'), (reason.get('excerpt') or '')[:200], reason['reason_code'])
                if key in seen:
                    continue
                seen.add(key)
                reason['publication_tier'] = publication_tier(
                    str(source.get('url') or '').split('/')[2] if '//' in str(source.get('url')) else '',
                    body)
                reason['snapshot_sha256'] = (source.get('snapshot') or {}).get('sha256')
                found.append(reason)
                if len(found) >= limit:
                    return found
    return found


#: The price lane's source whitelist, owned here so ``prices`` and its trace
#: can never disagree. See ``procurement_research.prices``.
PRICE_SOURCE_TYPES = ('manufacturer_website', 'manufacturer_download', 'tender_document')

#: One code per way a fetched-looking source is dropped *before* any price
#: predicate sees it. ``prices`` used to drop all of them silently, which made
#: "this announcement was never read" indistinguishable from "this announcement
#: priced nothing".
SKIP_BODY_NOT_FETCHED = 'body_not_fetched'
SKIP_TYPE_NOT_PRICE_SOURCE = 'source_type_not_price_source'
SKIP_MODEL_NOT_IN_BODY = 'target_model_not_in_body'
SKIP_NO_AMOUNT_EVIDENCE = 'no_amount_evidence'
SKIP_SOURCE_LIMIT = 'price_source_limit'
SKIP_BUDGET_NOT_AWARD = 'budget_or_ceiling_only'
SKIP_REASONS = (SKIP_BODY_NOT_FETCHED, SKIP_TYPE_NOT_PRICE_SOURCE,
                SKIP_MODEL_NOT_IN_BODY, SKIP_NO_AMOUNT_EVIDENCE,
                SKIP_SOURCE_LIMIT, SKIP_BUDGET_NOT_AWARD)


def price_source_skip_record(source, target, reason) -> dict:
    """One trace record for a source the price lane will not read, and why."""
    url = str((source or {}).get('url') or '')
    return {
        'url': (source or {}).get('url'),
        'host': url.split('/')[2] if '//' in url else '',
        'source_type': (source or {}).get('source_type'),
        'body_status': (source or {}).get('body_status'),
        'body_len': len((source or {}).get('body_text') or ''),
        'target_model': (target or {}).get('model'),
        'skip_reason': reason,
    }


#: Everything that is not an ASCII letter or digit: spaces, the dash family, CJK
#: punctuation (``（``/``～``), table pipes (``|``, which is how a real notice
#: writes ``| SPX-150BIII |``) -- and Chinese text, which matters just as much:
#: in ``Varian 的 Cary 6000i`` the Chinese word is what keeps ``varian`` from
#: fusing with ``cary6000i``, and in ``SPX-150B 生化培养箱`` it is what keeps
#: ``spx150b`` a token instead of the start of a sentence-long run.
_SEPARATOR_RUN = re.compile(r'[^0-9a-zA-Z]+')

#: A model-shaped run inside **one field** of a document: letters and digits only,
#: at least one of each. ``spx150biii`` and ``xka2200a`` match; a bare number, a
#: Chinese word or a whole sentence does not.
_ALNUM_RUN = re.compile(r'[0-9a-z]{3,32}')

#: Field boundaries for :func:`_model_shaped_tokens`: anything that is not an
#: ASCII letter, digit or **inside-model** separator (space, dash, ``/``, ``_``).
#: Chinese text is a boundary (a model never contains Chinese, and without this
#: ``SPX-150B 生化培养箱`` would flatten to one sentence-long run). Table pipes are
#: boundaries too: in ``…| SPX-150BIII | 6.0000`` joining across the pipe would
#: produce ``spx150biii6`` and the quantity digit would hide the sibling match.
_FIELD_BOUNDARY = re.compile(r'[^0-9a-zA-Z\s\-_/]+')
_MIN_RUN_DIGITS = 1
_MIN_RUN_LETTERS = 1


def _model_shaped_tokens(text: str):
    """Model-shaped runs in *text*, compared field by field.

    Within a field, separators are removed so ``SPX-150BIII`` is one token rather
    than ``spx`` and ``biii``. Across fields nothing is joined, so
    ``Varian 的 Cary 6000i`` yields ``cary6000i`` (the field as written) rather
    than ``variancary6000i``.
    """
    for field in _FIELD_BOUNDARY.split(text.casefold()):
        if not field:
            continue
        for token in _ALNUM_RUN.findall(_flat(field)):
            if sum(character.isalpha() for character in token) < _MIN_RUN_LETTERS:
                continue
            if sum(character.isdigit() for character in token) < _MIN_RUN_DIGITS:
                continue
            yield token


def _flat(text: str) -> str:
    return _SEPARATOR_RUN.sub('', text).casefold()


def model_match_signal(text, model) -> str | None:
    """``'exact'`` / ``'near'`` / ``None`` -- how *model* appears in *text*.

    ``'exact'`` is the acceptance gate (``procurement_rules.contains_model``):
    normalized identity, separator-tolerant, but a suffix letter or digit still
    refuses. This function **does not widen it**. It only separates the two
    reasons a body can fail that gate, because they call for opposite actions:

    * ``None``   -- the model is not in this body in any spelling. Nothing to read.
    * ``'near'`` -- one spelling is the other plus glued characters, i.e. almost
      certainly a **sibling model** (``SPX-150BIII`` for a target ``SPX-150B``,
      ``XKA-2200A`` for ``XKA-2200``). This is the case a reader will ask about:
      "you had the announcement and did not use it".

    The two real bodies in the frozen acceptance run that a *relaxed* gate would
    have admitted are both refused here on purpose: a different **number**
    (``Cary 6000i`` against ``Cary 60``, ``X1300A2`` against ``1300A2``) is a
    different instrument and stays ``None``. Relaxing the gate itself saves
    **zero** of the 135 in-whitelist bodies it drops, and the loose test admits
    exactly 3 bodies corpus-wide, all 3 wrong --
    so the gate stays as it is, and ``tests/test_model_match_signal.py`` pins the
    sibling pairs a loose test would merge.
    """
    if not isinstance(text, str) or not isinstance(model, str) or not model.strip():
        return None
    if contains_model(text, model):
        return 'exact'
    token = _flat(model)
    if len(token) < 3:
        return None
    # What this reports is narrow on purpose: some model-shaped run in the body
    # writes the same model with a letter or digit **glued to an end of it**. That
    # is the sibling-model shape -- ``SPX-150BIII`` for ``SPX-150B``,
    # ``XKA-2200A`` for ``XKA-2200``, ``H1850R`` for ``H1850``.
    #
    # Left out deliberately, because the alternative admits wrong instruments:
    #   * a different number in the middle (``Cary 6000i`` for ``Cary 60``,
    #     ``1306A2`` for ``1300A2``) is a different machine;
    #   * a different number glued with no separator (``X1300A2`` for ``1300A2``)
    #     is indistinguishable from a vendor prefix without guessing which digits
    #     are the model;
    #   * a **shorter** body spelling (target ``SPX-150BIII``, body ``SPX-150B``)
    #     reports ``None``: a reader looking at such a body sees a different model
    #     and the announcement would still have to be refused.
    #
    # Compared run by run rather than against the whole flattened document,
    # because a real notice never *is* just a model number. The glued character
    # must not be a digit: ``X1300A2`` and ``Cary 6000i`` differ from their
    # targets by a *number*, so they are ``None`` (different instruments).
    for found in _model_shaped_tokens(text):
        if len(found) <= len(token) or not found.startswith(token):
            continue
        # The extra characters must not be a digit: ``X1300A2`` differs from
        # ``1300A2`` by a number, not by a suffix. They must also not be a bare
        # run of digits, which ``set`` covers for the separator-free tail.
        extra = found[len(token):]
        if any(character.isdigit() for character in extra):
            continue
        return 'near'
    return None


def price_source_candidates(run_sources, target):
    """Split the run's sources into the ones the price lane will read and a trace.

    Returns ``(selected, trace)``. ``selected`` is, in order, exactly what
    ``procurement_research.prices()`` iterates over -- the same three predicates
    in the same order, so the trace can never describe a decision the lane did
    not make. ``trace`` is one record per source that was *dropped by an entry
    predicate*, each naming the predicate that dropped it.

    This is a pure function: it reads nothing, writes nothing, and returns the
    same answer the inline comprehension returned before it existed.
    """
    model = str((target or {}).get('model') or '')
    selected: list[dict] = []
    trace: list[dict] = []
    for source in run_sources or []:
        if not isinstance(source, dict):
            continue
        if source.get('body_status') != 'fetched':
            trace.append(price_source_skip_record(source, target, SKIP_BODY_NOT_FETCHED))
            continue
        if source.get('source_type') not in PRICE_SOURCE_TYPES:
            trace.append(price_source_skip_record(source, target, SKIP_TYPE_NOT_PRICE_SOURCE))
            continue
        if not contains_model(source.get('body_text', ''), model):
            record = price_source_skip_record(source, target, SKIP_MODEL_NOT_IN_BODY)
            # Distinguish "not in this body in any spelling" from "a sibling
            # model's spelling is glued to ours". The second is a nearly-missed
            # document and is what a reader will ask about; the gate still
            # refuses both (see ``model_match_signal``).
            record['model_signal'] = model_match_signal(source.get('body_text', ''), model)
            trace.append(record)
            continue
        selected.append(source)
    return selected, trace


def price_source_trace_summary(trace) -> list[dict]:
    """Counts per skip reason, for a report line and for tests."""
    counts: dict[str, int] = {}
    for item in trace or []:
        code = item.get('skip_reason') or UNKNOWN
        counts[code] = counts.get(code, 0) + 1
    return [{'skip_reason': code, 'count': counts[code]}
            for code in SKIP_REASONS if code in counts]


#: 拒绝代号 → 采购人员看得懂的说法。
#:
#: 前端核价页那张表（``frontend/src/pages/ResearchResults.jsx`` 的 ``rejectionLabels``）
#: 是同一套词的另一个副本；``tests/test_report_frontend_consistency.py`` 逐条比对两处，
#: 因为「前端有一句、报告没有」正是这次要修的那类缺陷——同一批事实两个说法。
#: 报告侧的取值走 :func:`rejection_label`，**永远不回落成代号本身**：认不出的代号
#: 对采购人员是内部话（同 ``procurement_research.failure_kind_text`` 的取舍）。
REJECTION_REASON_LABELS = {
    MODEL_MISMATCH: '型号不一致',
    BRAND_MISMATCH: '品牌不一致',
    BUDGET_NOT_ACHIEVED: '只是预算或限价',
    TOTAL_NOT_UNIT_PRICE: '只是项目总金额',
    NOT_AWARD_EVIDENCE: '不是成交证据',
    DATE_UNGROUNDED: '成交日期无法追溯',
    AMOUNT_NOT_UNIT_PRICE: '金额口径不是单价',
    UNPARSEABLE_AMOUNT: '金额写法无法解析',
    SELLER_SELF_DESCRIPTION: '卖家自述页面',
    SOURCE_NOT_TENDER: '来源不是采购公告',
    BODY_UNAVAILABLE: '未取得正文',
    EXCERPT_NOT_IN_BODY: '引文无法回原文',
    NOT_AWARDED_CANDIDATE: '不是中标候选人清单',
    QUANTITY_UNVERIFIED: '采购数量未核实',
    UNKNOWN: '未命中已知原因',
}


def rejection_label(code) -> str:
    """一个拒绝代号的人话说法；认不出来的一律说「未命中已知原因」。"""
    return REJECTION_REASON_LABELS.get(str(code or ''), REJECTION_REASON_LABELS[UNKNOWN])


def rejection_summary(rejections: list[dict]) -> list[dict]:
    """Group rejections by code, newest evidence first, for the report table."""
    order = [MODEL_MISMATCH, BRAND_MISMATCH, BUDGET_NOT_ACHIEVED, TOTAL_NOT_UNIT_PRICE,
             NOT_AWARD_EVIDENCE, DATE_UNGROUNDED, AMOUNT_NOT_UNIT_PRICE,
             UNPARSEABLE_AMOUNT, SELLER_SELF_DESCRIPTION, SOURCE_NOT_TENDER,
             BODY_UNAVAILABLE, EXCERPT_NOT_IN_BODY, NOT_AWARDED_CANDIDATE,
             QUANTITY_UNVERIFIED, UNKNOWN]
    counts: dict[str, int] = {}
    for item in rejections:
        code = item.get('reason_code') or UNKNOWN
        counts[code] = counts.get(code, 0) + 1
    return [{'reason_code': code, 'count': counts[code]} for code in order if code in counts]
