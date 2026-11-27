"""Extracted procurement research helpers (price_helpers)."""
from __future__ import annotations

import copy
import hashlib
import inspect
import json
import logging
import re
import sys
import threading
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import urlsplit

from backend.domain.search import SearchQuery
from backend.schemas.search import source_to_dict
from backend.services.alternative_price_lookup import NOT_ATTEMPTED, NOT_REQUESTED
from backend.services.official_search import OfficialSearchService
from backend.services.procurement_rules import (
    NO_CONFIRMED_PARAMETER,
    brand_from_source_line,
    brand_identity,
    comparison_basis,
    contains_model,
    evaluate_parameter,
    format_money,
    identity,
    model_spellings,
    supported,
    validate_price,
)
from backend.services.report_library import (
    empty_results_all_channels_text,
    empty_results_channel_text,
    search_channel_failed_text,
)
from backend.services.research_sources import is_tender, parse_web_url
from backend.services.source_authority import (
    host_of,
    is_local_authority_host,
    is_marketplace_or_aggregator,
    is_notice_page,
    product_page_rank,
)

from backend.services.research.constants import NO_OFFICIAL_SOURCE, NO_OFFICIAL_SOURCE_TEXT, REJECTION_SOURCE_AGGREGATE_MIN, SNAPSHOT_NOTE, SUSPECTED_SAME_TRANSACTION_REASON, TENDER_BODY_UNVERIFIED, _LOCAL_CHAIN_REASONS, _PRICE_AWARD_WORDS, _SPEC_FIELD, _TRANSACTION_FIELDS, _UNSTATED, _canonical_url, _strip_legacy_gap_note


def price_term_text(record, key):
    """Render one commercial term of a price record, saying why it is missing.

    A bare "待核实" is unreadable: it cannot be told apart from "the announcement
    simply does not say". The record carries the reason the extractor reached
    (``field_notes``, recorded in ``validate_price`` where the source body is
    still in hand), so it is shown next to the term instead of a shrug.
    """
    value = record.get(key)
    if value not in (None, '', 'unknown'):
        return str(value)
    return (record.get('field_notes') or {}).get(key) or '公告里没写'


def reconcile_tender_body_gaps(gaps, sources):
    """Drop the 「正文待核实」 gaps the research pass has since resolved.

    ``tender_body_unverified`` is written while searching, but the body is read in
    a later, separate fetch. Both passes are right about themselves, and the report
    printed them side by side: one line said 「采购公告正文待核实」 about the very
    announcement whose price was listed a few lines further down.

    Two things are wrong with printing that line with a footnote. The footnote
    talks about internal stages ("检索阶段" / "研究阶段") and claims the body was
    *priced from* -- unverifiable, and false whenever no price was accepted. And
    the line itself is no longer true: the body is in hand and listed in the
    source list, so 「正文待核实」 is a statement about the reader's evidence set
    that the evidence set contradicts.

    So a gap whose body has been fetched is not printed at all. Nothing is
    deleted: the search-stage judgement stays in ``run['search_attempts']`` and in
    the stored run's own ``gaps`` (``report()`` saves before reconciling), and a
    gap whose body was never fetched is returned verbatim.
    """
    if not gaps:
        return list(gaps or [])
    fetched = {_canonical_url(str(source.get('url') or ''))
               for source in (sources or [])
               if source.get('body_status') == 'fetched' and source.get('body_text')}
    prefix = TENDER_BODY_UNVERIFIED + ':'
    reconciled = []
    for gap in gaps:
        # Already-stored runs carry the old footnote inside the gap text itself.
        text = _strip_legacy_gap_note(gap)
        # 那句注解只拼给"正文已经读到"的缺口，所以它本身就是"已解决"的记号；
        # 存下来的 run 里正文可能已按当前口径脱敏，URL 对不上，这里按记号撤。
        if text != gap:
            continue
        url = text[len(prefix):].strip() if isinstance(text, str) and text.startswith(prefix) else ''
        if url and _canonical_url(url) in fetched:
            continue
        reconciled.append(text)
    return reconciled


def _declares_target_model(body, model) -> bool:
    """Whether *body* states the target model in its own 规格型号／型号 field.

    Not a price test -- ``contains_model`` already decided this body may be
    priced. This asks the cheaper question "did the *document* declare this
    model", which is what separates the CCGP notice on ``task-a283e5adf675``
    (``品牌：徕卡`` / ``规格型号：DMi1`` / ``单价(元)：41800``) from the three
    sources that matched the same target through a table cell: two 349-byte
    fragments and a 2 995-byte page whose 规格型号 column holds a car's
    ``2025款DMI1.5L125豪华``.
    """
    if not model:
        return False
    from backend.services.procurement_rules import contains_model
    return any(contains_model(value, model) for value in _SPEC_FIELD.findall(body or ''))


def _local_chain_blocker(source) -> str:
    """One sentence naming why the local PDF chain read nothing, or ``''``.

    Read from the record ``structured_pdf_text`` wrote, never re-derived: if the
    chain never ran, or ran cleanly, there is nothing to say here and the caller
    keeps the weak parser's own message.
    """
    warnings = (source.get('text_provenance') or {}).get('warnings') or []
    reasons = []
    for warning in warnings:
        if not isinstance(warning, dict):
            continue
        phrase = _LOCAL_CHAIN_REASONS.get(warning.get('code'))
        if phrase and phrase not in reasons:
            reasons.append(phrase)
    if not reasons:
        return ''
    return 'PDF正文未能读出：' + '；'.join(reasons) + '，需在本机恢复识别组件或提供可读原件'


def _price_source_rank(source, model=''):
    """Order in which a price budget spends its calls on eligible sources.

    Formal publications first; then the bodies that declare the target model in
    their own 型号 field; then the bodies that talk about an award. Order only:
    what is *accepted* stays ``validate_price``'s decision, and a source ranked
    last is still read when the budget reaches it.

    Measured on ``task-a283e5adf675``: without this rank the per-target allowance
    of 4 was spent on (among others) a car's ``DMI1.5L`` table, while the
    3 902-byte CCGP notice stating ``规格型号：DMi1`` sat past the cut -- and the
    ``price_source_limit`` record then kept it out of the rescan too.
    """
    body = source.get('body_text') or ''
    # The price budget is finite.  Without a deterministic tie-breaker, two
    # sources with the same evidence rank are consumed in provider return
    # order, so a reordered search response can change which prices are read.
    # Keep the evidence priorities above intact, then make equal-ranked
    # sources reproducible using source fields that do not assert evidence.
    raw_url = str(source.get('url') or '')
    return (source.get('source_type') != 'tender_document',
            not _declares_target_model(body, model),
            not _PRICE_AWARD_WORDS.search(body),
            _canonical_url(raw_url),
            str(source.get('title') or ''),
            str(source.get('snippet') or ''),
            str(body))


def is_no_official_source_gap(gap) -> bool:
    """Whether a run-level gap is one of the per-query official-source warnings.

    Matches both the raw marker (new runs, and the stored ones) and the Chinese
    wording those markers are translated into on the way out, so a run already
    read once is not reconciled twice into a different sentence.
    """
    if not isinstance(gap, str):
        return False
    return NO_OFFICIAL_SOURCE in gap or NO_OFFICIAL_SOURCE_TEXT in gap


def official_source_count(sources) -> int:
    """How many of the run's sources pass the document-level official test."""
    from backend.services.source_authority import source_publication_tier
    return sum(1 for source in (sources or [])
               if source_publication_tier(source) is not None)


def _price_key(row):
    """The **strict** identity of a priced row: every stated field, verbatim.

    This is what ``prices()`` uses to drop the same row appearing twice in one
    scan. It is deliberately *not* the identity of a transaction: the extractor
    writes ``''`` / ``unknown`` for a field the notice does not state, so two
    readings of one transaction that filled different fields in produce two
    different keys here. See :func:`_same_transaction` for that question.
    """
    from backend.services.procurement_rules import identity
    return (row.get('project_id') or row.get('url'), row.get('lot'), row.get('item'),
            identity(row.get('model')), row.get('unit_price'), row.get('date'))


def _filter_recalled(run, target):
    """价格记忆里取回的行，剔除当轮已经拿到的那几笔。

    为什么需要这一步：记忆库的行没有 ``project_id``/``lot``/``item``（它只存成交
    事实），所以 :func:`_price_key` 算出来的键与当轮那笔**不相等**，``_merged_prices``
    去不掉重——同一笔价会以"刚查到的"和"记着的"两个身份同时出现在结果里。

    判据用「来源 URL + 单价」：同一来源同一金额就是同一笔成交，这一点不依赖那些
    当轮才有的字段。剔掉之后 ``from_memory`` 就只会落在**真正来自记忆**的行上。
    """
    from backend.services import price_memory
    existing = {(row.get('url'), row.get('unit_price'))
                for row in (run.get('prices') or []) if isinstance(row, dict)}
    recalled = []
    for row in price_memory.recall((target or {}).get('model'), (target or {}).get('brand')):
        if (row.get('url'), row.get('unit_price')) in existing:
            continue
        recalled.append(row)
    return recalled


def _stated(value):
    """``value`` read as evidence: a blank or ``unknown`` field states nothing."""
    if isinstance(value, str):
        text = value.strip()
        if not text or text.casefold() in _UNSTATED:
            return None
        return text
    return value


def _same_transaction(left, right) -> bool:
    """Whether two priced rows are two *readings* of one concluded transaction.

    The measured defect (``设计规范-改后效果评测.md`` §5.1): ``task-271ebe2c1de7``
    and ``task-0a7f1ff94935`` each ended with **two rows for the same URL and the
    same ¥17 480**, because the model was asked twice, once with the notice's own
    files attached and once without. One reading filled ``lot``/``item``/``date``,
    the other left them ``''`` / ``unknown``; ``_price_key`` compared those blanks
    as values, so the pair merged into two transactions and
    ``price_decision.transaction_count`` reported 2 while ``summary`` said
    「查到1条同型号历史成交」.

    The rule is subset agreement, not "same URL": two rows are one transaction
    when they agree on every field **both** of them state, and the page, the model
    and the unit price all match. A row that states *less* is an incomplete
    reading of the same thing; a row that states something *different* is a
    different transaction and is kept:

    * same URL, different model or different unit price -> two rows (the legal
      line-item case: one notice really can award the same model in two lots at
      different prices, and it usually states a different ``lot`` too);
    * same URL, same model and price, ``lot`` stated as ``4包`` on one reading and
      ``5包`` on the other -> two rows;
    * one reading states ``amount_scope='unit'`` and the other
      ``'package_total'`` -> two rows: they are not the same claim about the money
      even when the number matches.
    """
    from backend.services.procurement_rules import identity
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    left_url, right_url = left.get('url'), right.get('url')
    if not left_url or not right_url:
        return False
    if _canonical_url(str(left_url)) != _canonical_url(str(right_url)):
        return False
    if left.get('unit_price') is None or right.get('unit_price') is None:
        return False
    if left.get('unit_price') != right.get('unit_price'):
        return False
    if identity(left.get('model')) != identity(right.get('model')):
        return False
    for field in _TRANSACTION_FIELDS:
        first, second = _stated(left.get(field)), _stated(right.get(field))
        if first is None or second is None:
            continue
        if first != second:
            return False
    return True


def _stated_field_count(row) -> int:
    """How much a reading states about the transaction, to pick the fuller one."""
    return sum(1 for field in _TRANSACTION_FIELDS if _stated(row.get(field)) is not None)


def _present(value) -> bool:
    """Whether a field carries something: blanks, ``unknown`` and empty containers do not."""
    if _stated(value) is None:
        return False
    if isinstance(value, (list, tuple, dict, set)) and not value:
        return False
    return True


def _fill_blanks(winner, loser):
    """One reading that states everything either reading stated.

    Two rows reach here only after ``_same_transaction`` proved they are one
    purchase, so nothing either of them stated is contested: the fuller reading
    wins on the fields they both state, and whatever only the other one carries is
    filled in. Measured on ``task-a283e5adf675``: the model reading of the
    ``t20260812_27118698.htm`` line and the table reading of the same line agree
    on url/model/unit price/quantity and differ only in their extras -- the model
    reading has ``item``/``lot``, the table reading has ``table_evidence`` -- and
    the merged row now keeps both instead of dropping a legal table citation.
    """
    filled = dict(winner)
    for key, value in loser.items():
        if not _present(filled.get(key)) and _present(value):
            filled[key] = value
    return filled


def _merge_price_rows(rows):
    """One row per concluded transaction, as the union of its readings.

    The first reading of a transaction keeps its position; a later reading is
    merged into it, the fuller of the two winning every field they both state and
    the other one filling only the blanks (see ``_fill_blanks``). Nothing that
    either reading stated is discarded.

    Rows that stay apart but look like one purchase get the "疑似同一笔" note on
    the way out (see :func:`annotate_suspected_same_transaction`): the note is
    added, never merged, so the adopted price set is unchanged.
    """
    merged = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for index, kept in enumerate(merged):
            if _same_transaction(kept, row):
                winner, loser = ((row, kept) if _stated_field_count(row) > _stated_field_count(kept)
                                 else (kept, row))
                merged[index] = _fill_blanks(winner, loser)
                break
        else:
            merged.append(row)
    annotate_suspected_same_transaction(merged)
    return merged


def annotate_suspected_same_transaction(rows):
    """Mark rows that look like one purchase recorded twice by two sites.

    The case the owner named (``output/e2e-final/runs/b3-r8-median``, the nine
    ``HVE-50`` rows): 48 390 元 on 2025-09-15 appears on ``zbcg.wit.edu.cn`` and
    again on ``ccgp-hubei.gov.cn``; 63 000 元 on 2024-05-11 appears on
    ``www.ccgp.gov.cn`` and again on ``www.zsjypt.cn``. Same amount, same date,
    two hosts -- most likely one award republished by a second site, possibly two
    real purchases of the same machine at the same price on the same day.

    Why this only annotates and never merges: ``_same_transaction`` calls two
    rows one transaction only when they are the *same URL* (one page, two
    readings). These pairs are different URLs, and two different sites may
    legitimately carry two different awards. The predicate layer has no evidence
    to fuse them, so **no accepted-price decision changes** -- the row count, the
    amount and the date all stay exactly as they were. Each row simply carries a
    sentence saying another record states the same amount on the same date from a
    different site, and the person decides.

    Returns how many rows were annotated. Running it twice is idempotent: the
    note is recomputed from the rows themselves, never accumulated.
    """
    groups = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        url = str(row.get('url') or '').strip()
        price = row.get('unit_price')
        date = _stated(row.get('date'))
        if not url or price is None or date is None:
            continue
        try:
            amount = round(float(price), 2)
        except (TypeError, ValueError):
            continue
        groups.setdefault((identity(row.get('model')), amount, date), []).append(row)
    flagged = 0
    for (model, amount, date), items in groups.items():
        if len({_canonical_url(str(row.get('url') or '')) for row in items}) < 2:
            continue
        for row in items:
            own = _canonical_url(str(row.get('url') or ''))
            row['suspected_same_transaction'] = {
                'reason': SUSPECTED_SAME_TRANSACTION_REASON,
                'unit_price': amount,
                'date': date,
                'counterparts': [{'url': other.get('url'), 'model': other.get('model')}
                                 for other in items
                                 if _canonical_url(str(other.get('url') or '')) != own][:5],
            }
            flagged += 1
    return flagged


def _merged_prices(existing, found):
    """Union of two priced-row lists, **one row per concluded transaction**.

    ``prices()`` used to write ``run['prices'] = list(result)`` -- an assignment,
    not an append -- and it runs once per target. A price that had already passed
    every predicate could therefore be erased by a later call that found nothing:
    measured on the frozen acceptance run, ``task-0a7f1ff94935`` still had the
    correct row in ``model_results`` (``XKA-2200``/17480, which ``validate_price``
    replays as a PASS) while the run ended with ``prices == []``.

    Merging by ``_price_key`` fixed that and exposed the mirror-image defect: two
    *readings* of one transaction whose blanks differ are not the same key, so the
    same URL and the same amount were stored twice. Identity is therefore decided
    by :func:`_same_transaction`, and ``_price_key`` is left to do what it is good
    at (dropping a verbatim repeat inside one scan).
    """
    return _merge_price_rows(list(existing or []) + list(found or []))


def reconcile_official_source_gaps(gaps, sources):
    """Make the official-source warning a **task-level** statement, once.

    The per-query warning is neither deleted nor weakened -- it stays verbatim in
    ``run['search_attempts'][]['warnings']``. What changes is the run-level gap:
    when the run *did* obtain official sources, one line says so, so the report
    can no longer claim there was no official source while listing one. When it
    did not, the wording says exactly that, once, rather than once per query.

    The output is a finished Chinese sentence with no marker left in it: this is
    the text a person reads, and it must not depend on ``GAP_TOKENS`` rewriting it
    a second time.
    """
    items = [gap for gap in (gaps or []) if gap is not None]
    if not any(is_no_official_source_gap(gap) for gap in items):
        return items
    others = [gap for gap in items if not is_no_official_source_gap(gap)]
    count = official_source_count(sources)
    if count:
        summary = f'部分检索未取得官方来源；本任务已取得 {count} 个官方来源，见来源清单'
    else:
        summary = '本任务未取得已核实的官方来源'
    return others + [summary]


def _date_range(dates):
    if not dates:
        return ''
    return dates[0] + ' 至 ' + dates[-1] if len(dates) > 1 else dates[0]


def quoted_model_note(baseline):
    """报价挂在口述型号上、比较基准是资料型号时，说明这笔报价到底属于谁。

    没有这层说明，报告会把「资料型号」的成交记录直接和「口述型号」的报价比，
    等于把一台机器的报价安到另一台头上。
    """
    quoted = str((baseline or {}).get('quote_model') or '').strip()
    basis = str((baseline or {}).get('model') or '').strip()
    if not quoted or quoted == basis:
        return ''
    return f'（对应销售推荐的 {quoted}，不是比较基准 {basis} 的报价）'


def award_records(run):
    """这一轮真正采信的成交记录（``award`` / ``contract``）。

    全仓库只有这一个判据回答「这个任务有没有官方成交记录」。前端的
    ``transactionPrices`` 是它的同一件事：按 ``kind`` 过滤，不看来源、不看金额。
    报告的分支（二手成交记录那一节产不产出）和展示层的分支（来源清单过不过滤、
    聚合站线索渲不渲染）必须都问这一个函数，否则两个地方会给出两种说法。
    """
    return [q for q in (run.get('prices') or []) if isinstance(q, dict)
            and q.get('kind') in {'award', 'contract'}]


def has_award_record(run) -> bool:
    return bool(award_records(run))


def price_evidence_summary(run):
    """``prices`` 这一节的汇总口径：只报本轮读到了什么，判断留给「本次采购判断」那一节。"""
    prices = [q for q in (run.get('prices') or []) if q.get('kind') in {'award', 'contract'}]
    if not prices:
        # 原文这里写「被拒收的原文与原因见「证据缺口」」：机械核对过，那一轮
        # rejected_records=12、rejection_excerpts_printed=0——报告指的地方一条都没有。
        # 拒绝明细现在真的印出来（``price_rejection_section``），这一句只说自己。
        return '本轮没有留下可采信的同型号成交记录。'
    dates = sorted({str(q['date']) for q in prices if q.get('date')})
    suppliers = list(dict.fromkeys(str(q['supplier']) for q in prices if q.get('supplier')))
    parts = [f'本轮列出 {len(prices)} 条同型号成交记录']
    if dates:
        parts.append('日期 ' + _date_range(dates))
    if suppliers:
        parts.append('成交供应商 ' + '、'.join(suppliers[:3]) + (' 等' if len(suppliers) > 3 else ''))
    return '；'.join(parts) + '。逐条的配置与商务条件缺口见各自标注。'


def aggregate_rejections_by_source(rejections, baseline_model=None):
    """Fold one source's amount-level refusals into a single source-level record.

    The owner's verdict (``回归基线``
    §Q2, 2026-10-06): the refusals themselves are right, the *granularity* is
    wrong. The internal fact is "this announcement is about other instruments,
    not the target model"; what reached the report was a dozen-plus amount-level
    refusals, which reads like dozens of separately considered rows. That is
    exactly what this repository forbids for itself (``设计规范`` §3.4: one
    outlet, one sentence, per fact).

    Three boundaries, so nothing meaningful gets folded away:

    * **Only ``model_mismatch``** is folded: its reason *is* "the model quoted is
      not the target". Brand mismatch, amount scope and "not award evidence" each
      say something different and are never merged.
    * **Every record from that source must carry that reason.** A source that
      also refused rows on other grounds keeps its records verbatim -- those
      grounds are facts about this run, not noise.
    * **At least ``REJECTION_SOURCE_AGGREGATE_MIN`` records.** One or two do not
      establish that the whole announcement is off-topic.

    Nothing is deleted: the folded record keeps ``reason_code``, its first
    ``excerpt`` and its ``detail`` fields, states how many records it stands for
    in ``aggregated_count``, and carries the union of the sibling models in
    ``other_models`` -- those model names are the evidence that this notice is
    about other instruments. Returns ``(records, folded_away)``.
    """
    from backend.services.procurement_rules import MODEL_MISMATCH
    groups, order = {}, []
    for record in rejections or []:
        if not isinstance(record, dict):
            groups.setdefault(None, []).append(record)
            if None not in order:
                order.append(None)
            continue
        url = str(record.get('url') or '').strip() or None
        if url not in groups:
            order.append(url)
        groups.setdefault(url, []).append(record)
    kept, folded = [], 0
    for url in order:
        rows = groups[url]
        codes = {str(row.get('reason_code') or '') for row in rows if isinstance(row, dict)}
        if (url and len(rows) >= REJECTION_SOURCE_AGGREGATE_MIN
                and codes == {MODEL_MISMATCH}):
            first = rows[0]
            others = []
            for row in rows:
                for name in (row.get('other_models') or []):
                    name = str(name or '').strip()
                    if name and name not in others:
                        others.append(name)
            # 型号称呼只有两种（迭代规范 统一称呼）：等于比较基准的是「本次目标型号」，其余
            # 一律「候选型号」（实测洁净台那轮 12 条属于已撤候选 BCV-3S1）。这句里不带
            # 称呼——它由记录的 ``model`` 字段承载（报告 label 上印一次）；共同解释
            # （"同品类的不同型号不能互相认定成交价。"）也不写在这里，见
            # ``price_rejections.REJECTION_REASON_NOTES``。
            detail = ('这条来源上共有 %d 条金额，引文型号：%s；'
                      '该公告讲的是别的仪器，已按来源聚合为一条。'
                      % (len(rows), '、'.join(others[:6]) or '（正文里未见其他型号）'))
            siblings = [name for row in rows for name in (row.get('same_family_models') or [])]
            if siblings:
                detail += ('其中 %s 属于同一系列的不同规格，那个价只作同系列参照。'
                           % '、'.join(dict.fromkeys(siblings)))
            kept.append({**first, 'detail': detail, 'aggregated_count': len(rows),
                         'other_models': others[:6]})
            folded += len(rows) - 1
        else:
            kept.extend(rows)
    return kept, folded


def price_rejection_section(run):
    """「未被采用的价格与原因」：前端核价页一直列着，导出的报告一条都没有过。

    每一条都照着前端那张表说同样的事实：被拒原因、模型给的引文、
    引文里出现的型号、抓取快照是否留存、来源地址。这些字段在 ``price_rejections``
    记录里本来就有（``price_rejections.explain_rejection`` / ``near_miss_rejections``
    写下），这里一个都不新造。

    迭代规范（设计规范："太啰嗦重复了"）：逐条只留短信息
    （``short_rejection_detail``），共同解释（"同品类的不同型号不能互相认定成交价。"）
    由节首说一次；型号称呼只有「本次目标型号」与「候选型号」两种
    （``rejection_owner_label``）。逐条的原因、引文与快照一个字没少，只是不再每条
    重复同一句解释。
    """
    from backend.services.price_rejections import (MODEL_MISMATCH, rejection_label,
                                                   rejection_owner_label,
                                                   rejection_reason_note,
                                                   short_rejection_detail)
    records = list(run.get('price_rejections') or [])
    if not records:
        return None
    baseline_model = str((run.get('baseline') or {}).get('model') or '').strip()
    items = []
    for record in records:
        who = rejection_owner_label(record, baseline_model)
        parts = [short_rejection_detail(record)]
        excerpt = str(record.get('excerpt') or '').strip()
        if excerpt:
            parts.append(f'引文：{excerpt}')
        # 原来这里还有一行「引文中出现的型号：…」——``short_rejection_detail`` 已经把它
        # 写在「引文型号：X」里了，同一批型号不再印两遍（迭代规范 去重复）。
        if str(record.get('snapshot_sha256') or '').strip():
            # 指纹本身是内部标识（业务口径：任何一层都不印哈希），但它要能核对得到：
            # 快照留在库里，校验值随报告导出在「来源清单」里。这句话与前端逐字相同
            # （``frontend/src/pages/ResearchResults.jsx`` 的 ``SNAPSHOT_NOTE``）。
            parts.append(SNAPSHOT_NOTE)
        item = {'label': f"{rejection_label(record.get('reason_code'))} · {who}",
                'text': '；'.join(part for part in parts if part)}
        if record.get('url'):
            item['evidence'] = [{'url': record['url']}]
        items.append(item)
    codes = {str(record.get('reason_code') or '') for record in records if isinstance(record, dict)}
    summary = (f'本轮抓到 {len(items)} 条金额，都没有通过单价核验，'
               '因此没有进入成交价比较；逐条的原因、引文与快照留存情况列在下面。')
    if MODEL_MISMATCH in codes:
        # 共同解释在报告里只说一次（原来每条都重复一遍）。
        summary += '共同口径：' + rejection_reason_note(MODEL_MISMATCH)
    return {'key': 'price_rejections', 'title': '未被采用的价格与原因',
            'summary': summary,
            'items': items}


def _price_sources(prices):
    """``price_lookup.sources``：从采信的价格行里取出来源（接口响应用，只读不改）。

    一行价格本来就是"一条来源上的一个金额"，所以这里逐行给 ``url`` 与 ``excerpt``，
    不另外去 ``run['sources']`` 里找一遍——那样会把没有价格的行也带出来，与
    ``prices`` 对不上。同一网址出现多次时按出现顺序都留着：两条不同的成交记录值得
    分别看，去重会丢掉金额不同的事实。
    """
    rows = []
    for price in prices or []:
        if not isinstance(price, dict):
            continue
        url = str(price.get('url') or '').strip()
        if not url:
            continue
        rows.append({'url': url, 'excerpt': str(price.get('excerpt') or '').strip()})
    return rows
