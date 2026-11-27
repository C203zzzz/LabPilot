"""Main orchestrator class for procurement research."""
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

from backend.services.research.constants import facade_attr
from backend.services.research.constants import EDUCATION_PLATFORM_LIMIT, LANES_TOGETHER, LANES_TOGETHER_STAGE, LANE_RESERVE_FLAG, LaneBudgetReserved, NOT_PROVIDED_ONLY, OPERATION_TIMING_LIMIT, PARALLEL_ALTERNATIVES_RESERVE, PROVINCE_AWARD_LIMIT, RESEARCH_LIMITS, ResearchStopped, SEARCH_STRATEGY_VERSION, STAGE_TIMING_LIMIT, now
from backend.services.research.price_helpers import _filter_recalled, _merged_prices, aggregate_rejections_by_source, has_award_record, reconcile_official_source_gaps, reconcile_tender_body_gaps
from backend.services.research.report_helpers import annotate_candidate_brands, answer_field, document_quality_notice, failure_location, mark_source_authority, no_comparable_candidates_gap, note_failure, redact_error_text, redact_unlisted_urls, sanitize_gap_text, uncovered_intake_fields
from backend.services.research.candidate_gates import parameters_with_verbatim_evidence
from backend.services.research.candidate_pipeline import CandidatePipelineMixin
from backend.services.research.content_fetcher import ContentFetcherMixin
from backend.services.research.price_pipeline import PricePipelineMixin
from backend.services.research.reporting import ReportingMixin
from backend.services.research.search_lane import SearchLaneMixin


class ProcurementResearch(
    SearchLaneMixin,
    ContentFetcherMixin,
    PricePipelineMixin,
    CandidatePipelineMixin,
    ReportingMixin,
):
    def __init__(self, repo, search, client_factory):
        self.repo, self.search, self.client_factory = repo, search, client_factory
        self.lock = threading.RLock()
        # 两条车道同时跑（``_lanes_together``）时新增的三把锁。它们保护的都是
        # **读-改-写**，不是单次赋值（``list.append``／``dict.__setitem__`` 在 GIL 下
        # 本身就是原子的，再上一把锁只会白等）：
        #   * ``budget_lock``：``consume`` 是"先比较再自增"，两条车道同时看到
        #     ``usage < limits`` 就会一起花出去，把额度记账冲破上限；
        #   * ``stage_lock``：``record_stage`` 与 ``record_operation`` 都是整份读出、
        #     追加、再写回，并发时后写的那份会盖掉前一份，丢掉的正是操作耗时；
        #   * ``aggregator_lock``：``aggregator_leads`` 的闸门（``aggregator_discovery``
        #     留痕）是"先查留痕再花钱"，两条车道同时通过就是同一轮花两次聚合站的钱。
        self.budget_lock = threading.RLock()
        self.stage_lock = threading.RLock()
        self.aggregator_lock = threading.RLock()
        # 当前**这一条车道**在花钱。``consume`` 靠它决定"价格车道是否已经花到保底线"。
        # 用 thread-local 而不是 run 上的字段：run 是两条车道共享的，写进去会被对方
        # 覆盖。拿不到值的线程（恢复流程、补充研究、报告）一律按"没有保底线"处理。
        self.spending_lane = threading.local()
        # 本轮的租约所有权，供车道线程装到自己的 ``execution`` 上（见 ``_lane_thread``）。
        self.round_claim = None
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='procurement')
        self.futures = {}
        self.execution = threading.local()
        # The body a prefetch already read for the ``fetch_source`` call in
        # flight (see ``_fetch_source``). It is thread-local and cleared the
        # moment it is taken, so it cannot leak into another call, another
        # batch or another thread.
        self.prefetch_slot = threading.local()
        for task in repo.list_tasks():
            for run in self.records(task['id'], 'research'):
                if run['status'] in {'queued', 'running'}:
                    if repo.active_analysis('research:' + run['id']):
                        continue
                    run.update(status='interrupted', message='服务已重启，请继续未完成研究。')
                    self.save(run)

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)

    def records(self, task_id, kind):
        return sorted([r for r in self.repo.list('procurement_records', task_id) if r.get('kind') == kind], key=lambda r: r['created_at'])

    def save(self, record):
        record['updated_at'] = now()
        self.repo.put('procurement_records', record['id'], record, task_id=record['task_id'])
        if record.get('kind') == 'research':
            task = self.repo.get('tasks', record['task_id'])
            if task:
                task['procurement_status'] = record['status']
                task['baseline_version'] = record['baseline']['version']
                self.repo.put('tasks', record['task_id'], task)

    def save_cache(self, run, record):
        with self.operation(run, 'cache_save', cache=record['kind']):
            self.save(record)

    def get(self, task_id, run_id):
        run = self.repo.get('procurement_records', run_id)
        if not run or run['task_id'] != task_id or run['kind'] != 'research':
            raise ValueError('研究记录不存在')
        future = self.futures.get(run_id)
        if run['status'] in {'queued', 'running'} and future is not None and future.done():
            run = self.repo.interrupt_orphaned_research(run_id) or run
        return run

    def display(self, task_id, run_id):
        """读给页面看的 run：把厂家产品参数按**当前**来源补出来，只改内存不落库。

        为什么不把它放进 ``get()``：``persist()`` 每存一次就要调一次 ``get()``
        （见 ``persist`` 里那句 ``stored = self.get(...)``，它只为读一眼状态），
        而一轮研究里 ``persist`` 要跑几十次；推导实测约 96 ms／次
        （66 个来源的真实 run），放进 ``get()`` 等于每次落库都白算一遍。
        读接口只有三个（run 状态、baseline、报告），所以在这三处显式补。

        为什么读的时候不落库：这里是纯读路径，写库有两个真实代价——一是前端
        跑起来时每 2 秒轮询一次 run 状态，二是研究线程可能正在同一份 run 上
        落库（``persist`` 带执行租约，读路径没有，写回旧快照会把它盖掉）。
        需要落库的时机只有一个，见 ``_execute`` 里「来源复核与结果汇总」那一段。

        研究还在跑的时候也照补：那时价格记录多半还没算出来，``hydrate_run`` 对空
        价格列表直接跳过、一次来源扫描都不做；真有价格可补时，补出来的东西与这一轮
        结束时的结果逐字相同（同一个纯函数），不存在"中途值"被缓存下来。
        """
        from backend.services.product_configuration import hydrate_run
        run = self.get(task_id, run_id)
        hydrate_run(run)
        # 页面上的缺口与报告正文必须是同一份：报告会把"研究阶段已经读到正文"的
        # 检索阶段缺口撤掉（见 ``reconcile_tender_body_gaps``），这里不撤的话，
        # 页面上还挂着 42 条、报告里只剩 34 条，同一份 run 两个说法。
        # ``sanitize_gap_text`` 同样两边都过一遍：展示副本说人话，库里的原文不动。
        run['gaps'] = [sanitize_gap_text(gap)
                       for gap in reconcile_tender_body_gaps(run.get('gaps'), run.get('sources'))]
        mark_source_authority(run)
        # 「这个候选项算不算原品牌」由后端算好（唯一一份别名表的所在地），前端不再
        # 自己维护第三份表；见 ``annotate_candidate_brands``。
        annotate_candidate_brands(run)
        # 脱敏只在"有官方成交记录"这一档生效，判据与来源清单过滤、报告那一节同一个：
        # 那种情况下聚合站属于"历史招标公告之外"的内容，前端一个地址都不承载。
        # 没有官方成交记录时它们就是页面上的正常内容，地址保持可点击（用户要靠它核对）。
        if has_award_record(run):
            redact_unlisted_urls(run)
        return run

    def document_candidates(self, task_id):
        """最新一次资料分析识别出的候选。

        页面上「型号与参数」页读的就是这份记录；报告要引用上传资料里的厂家参数时
        也走这里，不另开一条读取路径（``baseline()`` 同样用它）。
        """
        analyses = [x for x in self.repo.list('reports', task_id) if x.get('kind') == 'deepseek_analysis']
        if not analyses:
            return []
        return sorted(analyses, key=lambda x: x.get('created_at', ''))[-1].get('candidates', [])

    def baseline(self, task_id):
        previous = self.records(task_id, 'baseline')
        runs = self.records(task_id, 'research')
        candidates = self.document_candidates(task_id)
        if not candidates:
            candidates = self.repo.list('candidates', task_id)
        task = self.repo.get('tasks', task_id) or {}
        from backend.services.procurement_intake import read_intake
        intake = read_intake(self.repo, task_id)
        draft = intake.get('draft') or {'scenario': task.get('scenario', ''), 'parameters': [], 'currency': 'CNY'}
        document = intake.get('document_identity')
        previous_baseline = previous[-1] if previous else None
        # 已确认的比较基准不是在资料分析之前确认的，就是那条要改的：这里只标记出来，
        # 不偷偷改写用户已经确认过的记录和已经出的报告，让界面提示用户重新确认。
        stale = bool(previous_baseline and document and document.get('model')
                     and identity(previous_baseline.get('model')) != identity(document['model']))
        return {'baseline': previous_baseline or draft,
                'intake_draft': draft if intake.get('revision') else None,
                'candidates': candidates, 'run': self.display(task_id, runs[-1]['id']) if runs else None,
                'document_identity': document, 'document_candidates': intake.get('document_candidates'),
                'baseline_stale_by_document': stale}

    def confirm(self, task_id, payload, start=True, reuse_supplier=True, require_intake=True):
        if not str(payload.get('model') or '').strip() or not str(payload.get('scenario') or '').strip():
            raise ValueError('请填写型号与实际用途')
        parameters = payload.get('parameters') or []
        if not isinstance(parameters, list):
            raise ValueError('关键指标必须是列表')
        # 用户不一定有必须满足的实验要求（图6）。没有指标是一条正常输入：这里不再拦，
        # 但也不能静默——后面的参数比对、替代结论和报告都按"本轮没有确认可比指标"
        # 如实说（见 procurement_rules.comparison_basis），不产出任何满足结论。
        for p in parameters:
            if not p.get('name') or p.get('value') in (None, '') or p.get('direction', 'equal') not in {'equal', 'min', 'max', 'covers'}:
                raise ValueError('请完整填写关键指标及比较方式')
            if p.get('direction') == 'covers':
                import math
                value = p['value']
                if not isinstance(value, list) or len(value) != 2 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in value) or value[0] > value[1]:
                    raise ValueError('范围需包含从小到大的两个有效数值')
        # 写进比较基准之前的最后一道：**参数值必须能在它自己的引文里逐字读到**。抽出来的值
        # 自带引文（分析那一层写的 ``sources[].excerpt``）时校验它；对不上就**不写这一项**、
        # 按 ``设计规范`` §5 留下人话（``gaps``）与原文（``failures``），其余项照常。
        # 没有引文的项（用户自己填的、或外部脚本直接提交的）在这一层无从校验，原样保留。
        parameters, parameter_gaps, parameter_failures = parameters_with_verbatim_evidence(parameters)
        from backend.services.procurement_rules import amount
        if payload.get('supplier_price') not in (None, '') and amount(payload['supplier_price']) is None:
            raise ValueError('销售报价必须是有效的非负金额')
        quantity = amount(payload.get('quantity'))
        if payload.get('quantity') not in (None, '') and (quantity is None or quantity <= 0):
            raise ValueError('采购数量必须大于零')
        if require_intake:
            # A baseline that drops a condition the interview recorded is not a
            # silent "the supplier never said" -- it is missing input. Refuse it
            # while a genuinely unanswered condition (no recorded value) still
            # saves and keeps reading as 用户未填写 in the report.
            from backend.services.procurement_intake import FIELDS, read_intake
            missing = uncovered_intake_fields(read_intake(self.repo, task_id), payload)
            if missing:
                names = '、'.join(FIELDS.get(key, key) for key in missing)
                # 迭代规范（2026-10，总控定）：这一格要说的是"你这次漏提交了"，不是"销售没
                # 说"——它是用户可见的报错，跟界面（ProcurementPanel）统一成「未填写」，
                # 不写「销售未说明」，否则用户会以为在怪销售。
                raise ValueError(f'对话中已采集{names}，本次提交没有带入，不能按未填写保存。'
                                 '请刷新核对页把这些条件一并确认；确实不适用的项目，请在对话中更正后再保存。')
        allowed = ('brand', 'model', 'configuration', 'scenario', 'supplier_price', 'quantity', 'preferences', 'currency', 'tax', 'shipping', 'installation', 'warranty', 'service_response', 'service_onsite', 'delivery', 'parameters', 'category', 'intake_revision',
                   # 报价属于哪个型号：资料型号成为比较基准后，这笔报价仍要挂回口述型号。
                   'quote_model', 'quote_brand')
        values = {k: payload.get(k) for k in allowed}
        # 用的是上面筛过的那一份：`values` 里必须只有真的写进比较基准的参数，
        # 否则 digest 与 `reuse_supplier` 的比较都还按"含未通过校验的那一项"算。
        values['parameters'] = parameters
        if reuse_supplier and not start:
            previous = self.records(task_id, 'research')
            if previous and (values.get('intake_revision') or 0) > (previous[-1]['baseline'].get('intake_revision') or 0):
                old = previous[-1]['baseline']
                def comparison(value):
                    if isinstance(value, list):
                        return [{k: p.get(k) or '' for k in ('name', 'value', 'unit', 'direction', 'conditions')} for p in value]
                    if isinstance(value, str):
                        parts = set(filter(None, (p.strip() for p in re.split('[；;]', value))))
                        return sorted(p for p in parts if not any(p != other and p in other for other in parts))
                    return value or None
                fixed = ('brand', 'model', 'scenario', 'quantity', 'currency', 'preferences', 'parameters', 'category')
                if all(comparison(values[k]) == comparison(old.get(k)) for k in fixed):
                    return self.supplier_update(task_id, previous[-1]['id'], values)
        digest = hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        # 本轮到底有没有"必须满足的实验要求"，在确认这一刻定下来并写进 run：报告和替代
        # 结论都要如实说出这个状态，不能靠"候选状态"或"条数"往回猜——没有指标时
        # 0/0 会被算成满分（见 procurement_rules.comparison_basis）。
        basis = comparison_basis(values.get('parameters'))
        with self.lock:
            baseline = {**values, 'id': 'baseline-' + uuid.uuid4().hex, 'task_id': task_id, 'kind': 'baseline',
                        'created_at': now(), 'version': 1, 'digest': digest}
            run = {'id': 'research-' + uuid.uuid4().hex, 'task_id': task_id, 'kind': 'research', 'created_at': now(),
                   'baseline_id': baseline['id'], 'baseline': baseline, 'status': 'queued' if start else 'ready', 'stage': '准备检索' if start else '采购条件已确认',
                   **basis,
                   'usage': {'search': 0, 'fetch': 0, 'model': 0, 'education_platform': 0,
                             'province_award': 0},
                   'limits': {**RESEARCH_LIMITS, 'education_platform': EDUCATION_PLATFORM_LIMIT,
                              'province_award': PROVINCE_AWARD_LIMIT},
                   'sources': [], 'completed_queries': [], 'prices': [], 'republished_prices': [],
                   'alternatives': [], 'gaps': list(parameter_gaps),
                   'search_strategy_version': SEARCH_STRATEGY_VERSION,
                   'lanes': {'price': 'pending', 'alternatives': 'pending'}, 'round': 1}
            if parameter_failures:
                # 只有真的丢过参数时才出现这个键：没有它的时候 run 的形状与改动前逐字一致。
                run['failures'] = list(parameter_failures)
            run, created = self.repo.create_procurement_run(baseline, run)
            if not created:
                return run
            self.save(run)
            if start:
                self.futures[run['id']] = self.pool.submit(self.execute, task_id, run['id'])
            return run

    def _prepare_lane_resume(self, run, lane, *, force=False):
        """Clear provider quarantine and replenish exhausted quotas while keeping prior progress."""
        run.pop('unavailable_search_providers', None)
        run.pop('provider_failure_counts', None)
        run.pop('provider_challenge_counts', None)
        run.pop('provider_cooldown_until', None)
        run.pop('fetch_host_403_counts', None)
        run.pop('finished_at', None)
        usage = run.setdefault('usage', {})
        limits = run.setdefault('limits', {})
        for key, amount in RESEARCH_LIMITS.items():
            used = int(usage.get(key) or 0)
            current_limit = int(limits.get(key) or 0)
            if current_limit <= used:
                limits[key] = used + amount
        prev_status = (run.get('lanes') or {}).get(lane)
        run.setdefault('lane_retries', []).append({
            'lane': lane,
            'round': int(run.get('round') or 0) + 1,
            'previous_status': prev_status,
            'force': bool(force),
            'preserved_sources': len(run.get('sources') or []),
            'requested_at': now(),
        })

    def start_lane(self, task_id, run_id, lane):
        if lane not in {'price', 'alternatives'}:
            raise ValueError('请选择成交核价或其他品牌比较')
        with self.lock:
            run = self.get(task_id, run_id)
            from backend.services.procurement_intake import read_intake
            if read_intake(self.repo, task_id)['revision'] > (run['baseline'].get('intake_revision') or 0):
                raise ValueError('对话中补充了新条件，请先回到材料与报价确认最新比较基准。')
            if self.records(task_id, 'baseline')[-1]['id'] != run['baseline_id']:
                raise ValueError('采购条件已更新，请使用最新结果')
            if run['status'] in {'queued', 'running'} or self.repo.active_analysis('research:' + run_id):
                raise ValueError('正在处理本轮研究，请完成或停止后再开始')
            if run_id in self.futures and not self.futures[run_id].done():
                raise ValueError('上一轮正在保存，请稍后再开始')
            if run['lanes'].get(lane) == 'completed':
                return run
            if run['lanes'].get(lane) == 'failed' or run['status'] in {'failed', 'interrupted', 'cancelled', 'partial'}:
                self._prepare_lane_resume(run, lane, force=False)
            run['round'] += 1
            run.update(status='queued', active_lane=lane, stage='准备检索')
            run['lanes'][lane] = 'pending'
            run.pop('message', None)
            self.save(run)
            self.futures[run_id] = self.pool.submit(self.execute, task_id, run_id)
            return run

    def retry_lane(self, task_id, run_id, lane, *, force=False):
        """Retry a single lane from its last saved checkpoint without discarding gathered sources."""
        if lane not in {'price', 'alternatives'}:
            raise ValueError('请选择成交核价或其他品牌比较')
        with self.lock:
            run = self.get(task_id, run_id)
            from backend.services.procurement_intake import read_intake
            if read_intake(self.repo, task_id)['revision'] > (run['baseline'].get('intake_revision') or 0):
                raise ValueError('对话中补充了新条件，请先回到材料与报价确认最新比较基准。')
            if self.records(task_id, 'baseline')[-1]['id'] != run['baseline_id']:
                raise ValueError('采购条件已更新，请使用最新结果')
            if run['status'] in {'queued', 'running'} or self.repo.active_analysis('research:' + run_id):
                raise ValueError('正在处理本轮研究，请完成或停止后再重试')
            if run_id in self.futures and not self.futures[run_id].done():
                raise ValueError('上一轮正在保存，请稍后再重试')
            if run['lanes'].get(lane) == 'completed' and not force:
                return run
            self._prepare_lane_resume(run, lane, force=force)
            run['round'] += 1
            stage_label = '准备重试核查同款价格' if lane == 'price' else '准备重试核查其他品牌'
            run.update(status='queued', active_lane=lane, stage=stage_label)
            run['lanes'][lane] = 'pending'
            run.pop('message', None)
            self.save(run)
            self.futures[run_id] = self.pool.submit(self.execute, task_id, run_id)
            return run

    def _is_run_cancelled(self, run) -> bool:
        if not isinstance(run, dict):
            return False
        if run.get('status') == 'cancelled':
            return True
        run_id = run.get('id')
        if not run_id:
            return False
        stored = self.repo.get('procurement_records', run_id)
        return bool(stored and stored.get('status') == 'cancelled')

    def cancel(self, task_id, run_id):
        with self.lock:
            run = self.get(task_id, run_id)
            if run['status'] in {'queued', 'running'}:
                run.update(status='cancelled', message='已停止后续调用，现有结果已保存。', finished_at=now())
                # 同上：``finished_at`` 一写这一轮就是终态，``active_lane`` 不再有意义
                # （继续研究走 ``resume``，它会重新写入 ``all``）。
                run.pop('active_lane', None)
                self.save(run)
        from backend.services.structured_pdf import terminate_active_subprocesses
        terminate_active_subprocesses(owner_id=run_id)
        return run

    def supplier_update(self, task_id, run_id, payload):
        """Reprice a supplier revision using saved evidence, without paid calls."""
        with self.lock:
            previous = self.get(task_id, run_id)
            if previous['status'] in {'running', 'queued'}:
                raise ValueError('请等待当前研究保存，再更新销售回复。')
            allowed = {'configuration', 'supplier_price', 'tax', 'shipping', 'installation',
                       'warranty', 'service_response', 'service_onsite', 'delivery',
                       # 销售改口推荐了别的型号时，报价的归属也要跟着改，否则这笔报价
                       # 会一直挂在上一轮的型号上。
                       'quote_model', 'quote_brand',
                       # 功能偏好：比较基准本来就有这个字段，报告里也有落点（逐项偏好核查）。
                       # 它原来不在白名单里，于是被**静默丢掉**——盲测实测「提交 7 个字段、
                       # 生效 6 个」，接口 200、没有任何提示（B01 第 8b 条）。
                       'preferences'}
            # 这次回复的"依据""未提供""开放式答复"各自有专门的键，不是字段：
            # 销售答了「资料中未提供」和长文本答复，原来都无处存放（B01 第 8b 条）。
            meta = {'intake_revision', 'field_evidence', 'not_provided', 'answers', 'reply_notes', 'notes'}
            # 调用方（前端「回填销售回复」表单）会把整份比较基准一起提交：brand / model /
            # scenario / parameters / quantity / category / currency 这些键不是"销售的新
            # 回复"，但必须接受——原来它们被白名单**静默过滤**掉了，看不出是被丢了还是
            # 本来就没发。接受之后也不进 ``changes``：它们进不去"这次回复补了什么"。
            passthrough = {'brand', 'model', 'scenario', 'parameters', 'quantity',
                           'category', 'currency'}
            unknown = sorted(set(payload) - allowed - meta - passthrough)
            if unknown:
                # 不许静默丢弃：认不出的键要说出来是哪个，调用方才知道自己发错了什么。
                raise ValueError('这些字段当前不接受，请去掉或改名后再提交：' + '、'.join(unknown))
            not_provided = []
            for name in payload.get('not_provided') or []:
                name = str(name).strip()
                if name not in allowed:
                    raise ValueError('这些字段当前不接受，不能列入"资料中未提供"：%s' % name)
                not_provided.append(name)
            changes = {}
            for key, value in payload.items():
                if key not in allowed or value in (None, ''):
                    continue
                if isinstance(value, str) and NOT_PROVIDED_ONLY.match(value):
                    # 整句就是"资料里没有"：这是一次有效的回复，但它不是一个新值——
                    # 写进比较基准会把「税费：资料中未提供」当成税率。
                    not_provided.append(key)
                    continue
                changes[key] = value
            answer_entries = []
            for item in payload.get('answers') or []:
                if isinstance(item, str):
                    item = {'answer': item}
                if not isinstance(item, dict):
                    raise ValueError('answers 里的每一项要么是文本，要么是含 answer 的对象。')
                text = str(item.get('answer') or '').strip()
                if not text:
                    continue
                entry = {'question': str(item.get('question') or '').strip(), 'answer': text}
                for key in ('quote', 'location'):
                    if str(item.get(key) or '').strip():
                        entry[key] = str(item[key]).strip()
                field = answer_field(entry['question'])
                if field:
                    entry['field'] = field
                    # 追问里点了字段名、答复整句是"没有" → 就是这一项"问了、对方说没有"。
                    if NOT_PROVIDED_ONLY.match(text) and field not in not_provided:
                        not_provided.append(field)
                answer_entries.append(entry)
            reply_notes = str(payload.get('reply_notes') or payload.get('notes') or '').strip()
            if not (changes or not_provided or answer_entries or reply_notes):
                raise ValueError('请至少填写一项销售的新回复，或说明哪些项目资料中未提供。')
            from backend.services.procurement_rules import amount, price_decision
            if 'supplier_price' in changes:
                value = amount(changes['supplier_price'])
                if value is None:
                    raise ValueError('请填写有效的非负销售单价。')
                changes['supplier_price'] = value
            revision = {'changes': changes, 'origin': 'user_supplied_supplier_statement',
                        'previous_run_id': run_id, 'created_at': now()}
            if not_provided:
                revision['not_provided'] = sorted(set(not_provided))
            evidence = {k: v for k, v in (payload.get('field_evidence') or {}).items()
                        if k in allowed and isinstance(v, dict)}
            if evidence:
                revision['field_evidence'] = evidence
            if answer_entries:
                revision['answers'] = answer_entries
            if reply_notes:
                revision['reply_notes'] = reply_notes
            existing_reports = sorted(
                (r for r in self.repo.list('reports', task_id)
                 if isinstance(r, dict) and r.get('research_run_id') == run_id and isinstance(r.get('report'), dict)),
                key=lambda r: (str(r.get('report', {}).get('created_at') or ''), str(r.get('report', {}).get('report_id') or '')),
            )
            old_report = existing_reports[-1]['report'] if existing_reports else self.report(task_id, run_id)
            baseline = {**previous['baseline'], **changes}
            if payload.get('intake_revision') is not None:
                baseline['intake_revision'] = payload['intake_revision']
            # This path reprices an already-confirmed baseline with the
            # supplier's latest reply; which interview conditions it carries was
            # decided when that baseline was confirmed, so it must not be
            # re-litigated here (that would block a reply on old drafts).
            updated = self.confirm(task_id, baseline, start=False, reuse_supplier=False, require_intake=False)
            if updated['id'] == run_id:
                # 比较基准一个字都没变（典型：销售这次只说"资料中未提供"）。不另开版本，
                # 但这次回复本身必须记在 run 上——否则报告里还是分不清"没问"和"问了
                # 说没有"，而供应商答复的原文也就白留了。
                updated = copy.deepcopy(previous)
                updated['supplier_revision'] = revision
                updated['stage'] = '销售回复已记录'
                self.save(updated)
                self.report(task_id, run_id)
                return updated
            for key in ('sources', 'completed_queries', 'alternatives', 'gaps', 'lanes', 'price_leads'):
                updated[key] = copy.deepcopy(previous.get(key, []))
            # Reused evidence keeps its actual execution provenance and errors.
            for key in ('started_at', 'finished_at', 'unresolved_source_errors', 'unavailable_sources',
                        'retryable_queries', 'search_strategy_version', 'product_source_checked',
                        # 重算没有重新检索：逐次检索的原始记录（报告按它数"检索尝试"，
                        # 见 ``report`` 里的 ``count(run.get('search_attempts'))``）必须
                        # 跟着走，否则第二版报告会写"0 次检索尝试"（见 ``设计规范`` §3.2）。
                        'search_attempts'):
                if key in previous:
                    updated[key] = copy.deepcopy(previous[key])
            if 'search_strategy_version' not in previous:
                updated.pop('search_strategy_version', None)
            # 这一轮是"用已存证据重算"，没有重新检索、也没有重新推导来源级原因，
            # 所以上一轮已经写下的拒收记录原样带过来，只把**本次重算真的拒掉**的补进去。
            # 覆盖它等于丢数据（``设计规范`` §3.3：跳过与丢数据必须分开）。
            from backend.services.price_rejections import explain_rejection
            rejections = copy.deepcopy(previous.get('price_rejections') or [])
            recomputed = []
            unverified = []
            updated['prices'] = []
            for price in previous['prices']:
                url = str(price.get('url') or '').strip()
                # 证据不依赖本轮抓取来源页的价格，一律按原判保留——"核不了"和"否了"是两件事：
                #   * 本地成交明细库直查（``channel == 'xlyg_local_db'``，记录里带
                #     ``local_db`` 库路径）按型号直查就有价，本来就没有可对来源页的 url，
                #     它的判据在入库时已经由 ``validate_price_row`` 走过一遍（``设计规范`` §4.1）；
                #   * 价格记忆取回的历史成交价（``from_memory``，见 ``price_memory.py:191``）
                #     记的是**以前轮次**抓到的成交，来源页自然不在本轮的 ``sources`` 里。
                # 2026-10-08 回归重跑实测（``设计规范`` §3.1）：只认前一类时，HVE-50 那轮
                # 6 条记忆价（48390×2 / 63000×2 / 99840.5 / 52000）被降级成"未取得正文"，
                # 采信价从 10 条掉到 4 条。
                if (not url or price.get('local_db') or price.get('channel') == 'xlyg_local_db'
                        or price.get('from_memory')):
                    updated['prices'].append(copy.deepcopy(price))
                    continue
                source = next((s for s in updated['sources']
                               if str(s.get('url') or '').strip() == url), None)
                if source is None:
                    # 有地址、但这一轮的 ``sources`` 里没有它：回不了原文，重算不了。
                    # **"核不了"不等于这条价格无效**，所以不降级成拒收——回填只改销售
                    # 回复，``changes`` 的白名单里没有 model/brand（它们走 ``passthrough``，
                    # 见本函数上方），这些价格与当前比较基准的型号/品牌必然一致，按原判
                    # 保留；同时留一句给人看的说明，别让第二版报告静默地少几条
                    # （``设计规范`` §3：跳过与丢数据必须分开）。
                    updated['prices'].append(copy.deepcopy(price))
                    unverified.append(price)
                    continue
                refusals = {}
                checked = validate_price(price, source, updated['baseline'], refusals)
                if checked:
                    updated['prices'].append(checked)
                else:
                    recomputed.append(explain_rejection(price, source, updated['baseline'], refusals))
            if recomputed:
                # 落地口径与正常路径逐条对齐（``prices()`` 尾部）：先按
                # (url, 引文前 200 字, 原因码) 去重，再按来源折叠同因的金额级拒收
                # （``aggregate_rejections_by_source``），最后与上一轮已有的记录
                # 合并去重后写回。
                target_model = str(updated['baseline'].get('model') or '')
                deduped = {}
                for item in recomputed:
                    deduped.setdefault((item.get('url'), (item.get('excerpt') or '')[:200],
                                        item.get('reason_code')), item)
                target_rejections, folded = aggregate_rejections_by_source(
                    list(deduped.values()), baseline_model=target_model)
                if folded:
                    note = (f'目标型号「{target_model}」有 {folded} 条金额级拒收与同一条来源上、'
                            '原因相同的记录合并成来源级的一条，不再逐条列出'
                            '（每条聚合记录上写着它代表多少条金额）；拒收判据与采信结果没有改动。')
                    if note not in updated['gaps']:
                        updated['gaps'].append(note)
                known = {(item.get('url'), (item.get('excerpt') or '')[:200], item.get('reason_code'))
                         for item in rejections}
                for item in target_rejections:
                    key = (item.get('url'), (item.get('excerpt') or '')[:200], item.get('reason_code'))
                    if key not in known:
                        known.add(key)
                        rejections.append(item)
            if unverified:
                # 给人看的话：这一轮没有重新检索，这几条的回不了原文，金额沿用上一轮。
                note = (f'有 {len(unverified)} 条价格的来源页不在本轮结果里（回填销售回复是复用'
                        '上一轮的证据重算，没有重新抓取），它们的金额沿用上一轮判定、未经重新核对。')
                if note not in updated['gaps']:
                    updated['gaps'].append(note)
            if rejections:
                updated['price_rejections'] = rejections
            updated.update(status='partial', stage='销售回复已更新', parent_report_id=old_report['report_id'],
                           supplier_revision=revision, evidence_reused=True)
            if updated['lanes'].get('price') == 'pending' and (
                updated['prices']
                or (previous.get('price_decision') or {}).get('state') not in {None, 'not_researched'}
                or previous.get('search_attempts')
            ):
                updated['lanes']['price'] = 'completed'
            updated['price_decision'] = price_decision(updated['prices'], updated['baseline'], updated['lanes']['price'])
            updated['price_assessment'] = updated['price_decision']['summary']
            self.save(updated)
            self.report(task_id, updated['id'])
            return updated

    def resume(self, task_id, run_id):
        with self.lock:
            run = self.get(task_id, run_id)
            if run.get('parent_run_id'):
                from backend.services.supplemental_research import budget_root
                root = budget_root(self, run)
                if self.repo.active_analysis('research:' + root['id']):
                    raise ValueError('关联研究正在处理，请稍后继续')
                # An explicit paid research round becomes independent of the
                # import budget; parent report linkage remains immutable.
                run.pop('parent_run_id', None)
            if self.repo.active_analysis('research:' + run_id):
                raise ValueError('本轮正在处理或核验补证，请稍后再继续')
            if run['status'] in {'queued', 'running'} and (run_id not in self.futures or self.futures[run_id].done()):
                run['status'] = 'interrupted'
            if run['status'] in {'queued', 'running', 'completed'}:
                return run
            if run_id in self.futures and not self.futures[run_id].done():
                raise ValueError('正在结束上一轮调用，请稍后继续')
            latest = self.records(task_id, 'baseline')[-1]
            if latest['id'] != run['baseline_id']:
                raise ValueError('比较基准已更新，请使用最新研究')
            # Provider quarantine is scoped to the completed research round.
            # Reset both the visible quarantine list and its failure streak so
            # a recovered Exa/Tavily/public provider gets one fresh probe in
            # the explicitly requested next round. Keeping the streak here
            # would permanently skip providers after three transient errors.
            run.pop('unavailable_search_providers', None)
            run.pop('provider_failure_counts', None)
            run.pop('provider_challenge_counts', None)
            run.pop('provider_cooldown_until', None)
            run.pop('fetch_host_403_counts', None)
            if run.get('search_strategy_version') != SEARCH_STRATEGY_VERSION:
                # Upgrade discovery only; body/model caches and saved reports stay intact.
                run['completed_queries'] = []
                run.pop('product_source_checked', None)
                run['search_strategy_version'] = SEARCH_STRATEGY_VERSION
            run['limits'] = {k: run['usage'][k] + v for k, v in RESEARCH_LIMITS.items()}
            run.update(status='queued', stage='准备继续研究', round=run['round'] + 1, message='继续未完成部分')
            run['active_lane'] = 'all'
            run.pop('finished_at', None)
            if not run['prices']:
                run['lanes']['price'] = 'pending'
            if not any(a['status'] == 'eligible' for a in run['alternatives']):
                run['lanes']['alternatives'] = 'pending'
            if run.get('unresolved_source_errors'):
                run['lanes'] = {'price': 'pending', 'alternatives': 'pending'}
            self.save(run)
            self.futures[run_id] = self.pool.submit(self.execute, task_id, run_id)
            return run

    @contextmanager
    def stage(self, run, name, stage_label=None):
        """Time one stage of a round and count the calls it spent.

        A finished round recorded only ``started_at``/``finished_at``, so which
        step of a twenty-minute chain took the time could only be answered with
        an external stopwatch. Each stage now appends one entry carrying its own
        start, end, duration and the search/fetch/model calls that happened
        inside it. ``usage`` stays the authority for the counters, so a cached
        model answer counts zero here exactly as it does for the budget.

        The entry lives on the run, which means the ordinary save points persist
        it and the run GET endpoint returns it. ``stage`` itself is only
        snapshotted into the entry; its user-visible meaning is unchanged.

        ``stage_label`` 只给**并行车道**用（``_lanes_together``）：那时
        ``run['stage']`` 是两条车道合起来的那一个词，快照到条目里就不再等于
        ``name``，而阶段清单与状态行是同一屏上的两处说法。车道把自己的名字传进来，
        两处就还是同一个词；不传时行为与从前一致。

        并行时两条车道的阶段会**重叠**，于是 ``duration_ms`` 相加会大于本轮墙钟
        时间——那是事实，不是记账错。口径按出现区间取并集（见
        ``tests/test_research_stage_timings.py``）。
        """
        started, clock = now(), time.perf_counter()
        before = dict(run.get('usage') or {})
        try:
            yield
        except BaseException:
            self.record_stage(run, name, started, clock, before, 'stopped', stage_label)
            raise
        self.record_stage(run, name, started, clock, before, 'completed', stage_label)
        # Publish the finished stage before the next one begins, so a run that
        # is cancelled or interrupted still shows what it already spent. This is
        # the same cancellation/lease check the next stage would have made.
        self.persist(run)

    def record_stage(self, run, name, started, clock, before, outcome, stage_label=None):
        usage = run.get('usage') or {}
        calls = {key: max(0, usage.get(key, 0) - before.get(key, 0))
                 for key in ('search', 'fetch', 'model', 'education_platform', 'province_award')}
        entry = {
            'name': name,
            # 并行时 ``run['stage']`` 是两条车道合起来的那一个词，快照进条目里就不再
            # 等于 ``name``，而阶段清单与状态行是同一屏上的两处说法。车道把自己的名字
            # 传进来（``stage_label``），两处就还是同一个词；不传时与从前逐字相同。
            'stage': run.get('stage') if stage_label is None else stage_label,
            'round': run.get('round', 1),
            'status': outcome,
            'started_at': started,
            'finished_at': now(),
            'duration_ms': int(round((time.perf_counter() - clock) * 1000)),
            'calls': calls,
        }
        # 读出—追加—写回必须原子：两条车道同时结束一个阶段时，后写的那份会盖掉前一份。
        with self.stage_lock:
            entries = list(run.get('stage_timings') or [])
            entries.append(entry)
            run['stage_timings'] = entries[-STAGE_TIMING_LIMIT:]

    def record_operation(self, run, operation, started, clock, outcome, **metadata):
        if isinstance(started, (int, float)):
            started = datetime.fromtimestamp(started, timezone.utc).isoformat()
        entry = {
            'operation': operation,
            'status': outcome,
            'started_at': started,
            'finished_at': now(),
            'duration_ms': max(0, int(round((time.perf_counter() - clock) * 1000))),
            'round': run.get('round', 1),
            **metadata,
        }
        with self.stage_lock:
            entries = list(run.get('operation_timings') or [])
            entries.append(entry)
            run['operation_timings'] = entries[-facade_attr('OPERATION_TIMING_LIMIT', OPERATION_TIMING_LIMIT):]

    def import_operation(self, run, entry, **overrides):
        with self.stage_lock:
            entries = list(run.get('operation_timings') or [])
            entries.append({**entry, **overrides, 'round': run.get('round', 1)})
            run['operation_timings'] = entries[-facade_attr('OPERATION_TIMING_LIMIT', OPERATION_TIMING_LIMIT):]

    @contextmanager
    def operation(self, run, operation, **metadata):
        started, clock = now(), time.perf_counter()
        try:
            yield
        except BaseException:
            self.record_operation(run, operation, started, clock, 'stopped', **metadata)
            raise
        self.record_operation(run, operation, started, clock, 'completed', **metadata)

    def persist(self, run):
        started, clock = now(), time.perf_counter()
        with self.lock:
            claim = getattr(self.execution, 'claim', None)
            if claim and self.repo.active_analysis(claim[0]) != claim[1]:
                raise ResearchStopped('执行租约已失效，停止后续调用')
            stored = self.get(run['task_id'], run['id'])
            if stored['status'] == 'cancelled':
                raise ResearchStopped('已取消')
            self.record_operation(run, 'persist', started, clock, 'completed')
            self.save(run)

    def consume(self, run, key):
        # 先比较额度再自增，两步必须原子：两条车道同时跑时，两个线程都可能在对方
        # 自增之前看到 ``usage < limits``，于是同一份额度被花两次。``persist`` 故意
        # 留在锁**外面**——它自己要拿 ``self.lock``，套进来就形成锁序，而两条车道
        # 抢这一把锁的时间比一次落库小得多。
        with self.budget_lock:
            if run['usage'][key] >= run['limits'][key]:
                raise ResearchStopped('本轮调用额度已用完，可保留结果并按需继续。')
            reserve = PARALLEL_ALTERNATIVES_RESERVE.get(key, 0)
            if (reserve and run.get(LANE_RESERVE_FLAG)
                    and getattr(self.spending_lane, 'name', None) == 'price'
                    and run['usage'][key] + reserve >= run['limits'][key]):
                raise LaneBudgetReserved(
                    '价格核查已花到为相似品核查预留的额度线（%s 还剩 %d 次），先停下。'
                    % (key, run['limits'][key] - run['usage'][key]))
            run['usage'][key] += 1
        self.persist(run)
        if run.get('parent_run_id'):
            parent = self.get(run['task_id'], run['parent_run_id'])
            seen = set()
            while parent['id'] not in seen:
                seen.add(parent['id'])
                parent['usage'][key] = max(parent['usage'][key], run['usage'][key])
                self.save(parent)
                if not parent.get('parent_run_id'):
                    break
                parent = self.get(run['task_id'], parent['parent_run_id'])

    def record_failure(self, run, stage, error):
        from backend.infrastructure.deepseek import DeepSeekError
        message = error.message if isinstance(error, DeepSeekError) else '处理数据时发生异常，已有结果已保留'
        run['gaps'].append(stage + '：' + message)
        # 车道级的失败也带上异常原文（脱敏后）。口径与 ``note_failure`` 一处：同一个
        # 字段名 ``error``、同一套脱敏。只记 type/location 时，"哪一条链接、哪一个
        # 主机名"这类定位信息全丢了——``210`` 那一单当时正是被这一条挡住，连着两轮
        # ``alternatives=failed`` 拿不到任何原因；而 ``str(error)`` 可能带着整条请求
        # URL（含 key／token），所以先脱敏再落库：key 不落盘是红线，留痕不能靠破这条
        # 红线来换。
        run.setdefault('failures', []).append({'stage': stage, 'type': type(error).__name__,
                                               'code': getattr(error, 'code', 'processing_error'),
                                               'location': failure_location(error),
                                               'error': redact_error_text(error)})

    @contextmanager
    def unit_scope(self, run, stage, unit, ident, code):
        """处理**一个单元**（一条来源／一个候选）时的一层兜底：失败半径就是这个单元。

        为什么要有这一层：车道上那一层 ``except Exception``（价格车道与替代品车道
        各一处）只能回答"这一步整体做不了"，它一接住就把整条车道写成 ``failed``。
        于是"某个页面正文里抄出来的一条畸形链接解析不了"这种单点故障，升级成了
        "整轮研究没有价格、没有替代品"——实测 ``lanes={"price":"failed",
        "alternatives":"failed"}``、26 条来源只剩 20 条缺口，整份样本作废。

        层次从此定死：**车道级只接"这一步整体做不了"；单条来源、单个候选、单条链接
        的失败在更内层就地降级成缺口**。``ResearchStopped`` 是控流（额度用尽），
        原样上抛，它不是失败。
        """
        try:
            yield
        except ResearchStopped:
            raise
        except Exception as exc:
            note_failure(run, stage, code, exc)
            if code == 'source_skipped':
                run['unresolved_source_errors'] = True
            gap = f'{stage}：这一{unit}处理失败，已跳过，其余不受影响：{ident}'
            if gap not in run['gaps']:
                run['gaps'].append(gap)

    @contextmanager
    def source_scope(self, run, source, stage):
        """``unit_scope`` 的来源版：识别名用来源地址。"""
        with self.unit_scope(run, stage, '条来源', (source or {}).get('url') or '', 'source_skipped'):
            yield

    @contextmanager
    def candidate_scope(self, run, candidate, stage):
        """``unit_scope`` 的候选版：识别名用型号。

        替代品车道的循环体是**一个候选**一份工作（检索、厂家核验、抽价、比对），
        它与"一条来源"是同一个道理：一个候选上的单点故障不该让整条替代品车道
        ``failed``，那正是 B02 那一轮发生的事。
        """
        with self.unit_scope(run, stage, '个候选', (candidate or {}).get('model') or '', 'candidate_skipped'):
            yield

    def execute(self, task_id, run_id):
        claim = 'research:' + run_id
        owner = self.repo.claim_analysis(claim)
        if not owner:
            return
        from backend.infrastructure.lease_heartbeat import LeaseHeartbeat
        heartbeat = LeaseHeartbeat(self.repo, claim, owner)
        try:
            self.execution.claim = (claim, owner)
            # 车道线程要把同一份租约装到**自己的** ``execution`` 上，见 ``_lane_thread``。
            self.round_claim = (claim, owner)
            self._execute(task_id, run_id)
        finally:
            self.round_claim = None
            self.execution.claim = None
            heartbeat.close()
            self.repo.release_analysis(claim, owner)

    def _price_lane(self, run, b):
        """价格车道：成交金额检索（原 ``_execute`` 里那一段，逐字搬过来）。

        动作与顺序一个字没改，改的只是"谁在什么线程上跑它"：并行时它由
        ``_lanes_together`` 起一条线程执行，串行时照旧在调度线程上顺序执行。
        ``self.spending_lane`` 记的是"这条车道在花钱"，只给并行的保底线看
        （见 ``PARALLEL_ALTERNATIVES_RESERVE``）。
        """
        self.spending_lane.name = 'price'
        try:
            run['lanes']['price'] = 'running'
            self.persist(run)
            # 阶段名与这一段的 ``run['stage']`` 逐字相同（``_execute`` 开头设的
            # 「核查同款价格」）：进度面板把 ``run.stage`` 印在状态行、把
            # ``stage_timings[].name`` 印在已完成阶段清单里，两个词序一旦不同，
            # 同一步就在同一屏上说了两遍、还像两步。这里改成 ``run['stage']``
            # 已经印着的那个词序，用户看到的阶段名不变。
            # 缺口文案也一起用同一个词序（见下面 ``run['gaps'].append``）——它自己
            # 不直接给用户看，是 ``frontend/src/services/researchCopy.js`` 认出来换成人话；
            # 那个识别正则同时认新旧两种词序，所以库里已经存下来的老缺口文案照旧换。
            with self.stage(run, '核查同款价格', stage_label='核查同款价格'):
                from backend.services.research_planning import procurement_queries
                for query, purpose in procurement_queries(b):
                    self.query(run, query, purpose, target=b)
                self.trace_procurement(run)
                # 价格通道入口：正文给了附件线索、名下却没有附件正文的公告在这里
                # 补抓一次。它只花 fetch 额度，抓回来的正文照旧过全部既有判据
                # （``prices()`` 一个字没改）。
                self.backfill_publication_attachments(run)
                # 教育／科研交易平台通道（省级教育部门自己的小额竞价平台）。
                # 位置必须在 ``prices()`` **之前**：``prices()`` 读的是它被
                # 调用那一刻的 ``run['sources']``，这一步新增的来源要到场才
                # 有意义。额度是它自己的一份（``education_platform``），不占
                # ``fetch``；价格判据一个字没改，它只是多放几条来源进去。
                # **独立容错**：该通道不依赖模型，即使上游步骤失败也应该执行。
                def _run_edu():
                    self.execution.claim = self.round_claim
                    self.spending_lane.name = 'price'
                    try:
                        self.education_platform_leads(run, b)
                    except Exception as exc:
                        with self.stage_lock:
                            run['gaps'].append(f'教育平台查询失败：{exc}')

                def _run_prov():
                    self.execution.claim = self.round_claim
                    self.spending_lane.name = 'price'
                    try:
                        self.province_award_sources(run, b)
                    except Exception as exc:
                        with self.stage_lock:
                            run['gaps'].append(f'省级平台查询失败：{exc}')

                if self.education_platform_budget(run) > 0 and self.province_award_budget(run) > 0:
                    with ThreadPoolExecutor(max_workers=2, thread_name_prefix='price-platforms') as platform_pool:
                        f_edu = platform_pool.submit(_run_edu)
                        f_prov = platform_pool.submit(_run_prov)
                        f_edu.result()
                        f_prov.result()
                else:
                    _run_edu()
                    _run_prov()
                # 合并而不是覆盖：``prices()`` 只读它被调用的那一刻的
                # ``run['sources']``，而 ``recover_sources``（最后一个会新增来源的
                # 步骤）在两个通道之后才跑。到这里做赋值，会把"另一条 target 的
                # 通道已经落库、按当前判据仍然有效"的价格整条抹掉——实测
                # ``task-0a7f1ff94935`` 的 ``XKA-2200``/17480 就是这样消失的
                # （``model_results`` 里还在，``validate_price`` 复算是 PASS）。
                run['prices'] = _merged_prices(run.get('prices'), self.prices(run, b))
                # 价格记忆（``backend/services/price_memory.py``）：本轮采信的价先记账，
                # 再把**往轮**采信过的价并进来。顺序是"先记后并"——反过来的话，本轮刚
                # 采信的行会在同一轮里以"记忆"的身份再并一次，``from_memory`` 就落到了
                # 刚查到的行上，"记着的"和"刚查到的"从此分不开。
                #
                # 记忆里的价**不改判据**：它们当初就是按同一套判据采信的，这里原样带回。
                # 记忆默认开启，``LAB_PRICE_MEMORY=off`` 可整体关掉做对照跑。
                from backend.services import price_memory
                price_memory.record(run, b)
                _remembered = _filter_recalled(run, b)
                if _remembered:
                    run['prices'] = _merged_prices(run.get('prices'), _remembered)
                    run.setdefault('price_memory_hits', []).append({
                        'model': b.get('model'), 'brand': b.get('brand'),
                        'count': len(_remembered),
                        'note': '这几条价格来自以前核过的成交记录，不是本轮新查到的。',
                    })
                run['republished_prices'] = self.republished_prices(run, b)
                # 聚合站发现渠道的**兜底触发点**：官方直查——本车道上这一串
                # 检索、教育/科研平台通道、省级按型号直查，直到 ``prices()``
                # 与 ``republished_prices()``——跑完仍然一条可采信的成交单价都
                # 没有，才花聚合站这笔钱。触发判据在 ``aggregator_trigger``
                # 里（读的是落地结果，不是"某一次检索没命中"）。
                #
                # 位置的两处讲究：
                # ① 在 ``prices()`` **之后**：判据是"价格确实没产出"；
                # ② 在本车道之内、``alternatives`` **之前**：复核回来的官方
                #    原文因此同时喂给替代品的发现检索（``discovery_sources``
                #    读的就是 ``run['sources']``）。
                #
                # 它这次进来的正文**来得及**被价格车道再读一遍：本车道的
                # ``prices()`` 只认它被调用那一刻的来源，而这些正文是之后才到
                # 的，由「来源复核与结果汇总」里的 ``_rescan_late_price_sources``
                # 用第二份额度补读——那一步只排除"这一轮真进过价格通道"的来源
                # （``price_source_skips`` / ``price_source_cache_hits`` 里有的），
                # 兜底正文从没进过价格通道，按构造必然入选。同一个 ``b``，
                # 判据栈一个字不改。
                self.aggregator_leads(run, price_lane_finished=True)
                run['lanes']['price'] = 'completed'
        except LaneBudgetReserved as exc:
            # 撞上为相似品车道留的额度线：本车道停在原地，而**不**让整轮走
            # "已停止"那条路。车道标回 ``pending``（与"额度用完后重新开始"同一个
            # 可继续状态），另一条车道不受影响，它还在花自己那份。
            run['lanes']['price'] = 'pending'
            run['gaps'].append('核查同款价格在预留额度前停下：' + str(exc))
            self.persist(run)
        except ResearchStopped:
            raise
        except Exception as exc:
            self.record_failure(run, '价格核查', exc)
            run['lanes']['price'] = 'failed'
            run['gaps'].append('核查同款价格未完成，请检查模型或搜索服务后继续。')

    def _alternatives_lane(self, run, b, parallel=False):
        """相似品车道：同档替代核查（原 ``_execute`` 里那一段，逐字搬过来）。"""
        self.spending_lane.name = 'alternatives'
        try:
            if not parallel:
                # 串行时状态行归这条车道；并行时它由 ``_execute`` 设成两条车道合起来的
                # 那一个词，这里再写一次就是两条车道互相盖。
                run['stage'] = '核查其他品牌'
            run['lanes']['alternatives'] = 'running'
            self.persist(run)
            # 同一条规矩（见上面价格通道那段）：``run['stage']`` 印在状态行上的是
            # 「核查其他品牌」，这里的阶段名就跟它逐字相同。缺口文案也一起统一成
            # 这个词序（前端 ``researchCopy.js:16`` 的正则同时认新旧两种词序，库里
            # 已存的老缺口照旧换成人话）；``record_failure`` 的内部标签『替代产品核查』
            # 不动——它不是用户看到的阶段名，也不进前端文案。
            with self.stage(run, '核查其他品牌', stage_label='核查其他品牌'):
                from backend.services.research_planning import discovery_queries
                for query in discovery_queries(b):
                    self.query(run, query, 'discovery')
                # 这一次调用只在**价格车道这一轮没跑**（比如只重跑替代品那一
                # 车道）而 ``run['prices']`` 仍为空时才会真的走：价格车道里已经
                # 触发过兜底的话，``run['aggregator_discovery']`` 就是闸门，这里
                # 直接返回，同一轮不会花第二次钱（见 ``aggregator_trigger``）。
                self.aggregator_leads(run)
                self.alternatives(run)
                run['lanes']['alternatives'] = 'completed'
        except ResearchStopped:
            raise
        except Exception as exc:
            self.record_failure(run, '替代产品核查', exc)
            run['lanes']['alternatives'] = 'failed'
            run['gaps'].append('核查其他品牌未完成，请检查服务后继续。')

    def _lane_thread(self, lane, run, b):
        """在车道线程上装好本轮租约，再跑这条车道。

        ``self.execution`` 是 ``threading.local()``：``execute()`` 把租约装在**调度
        线程**上，新起的车道线程读到的会是 ``None``，而 ``persist`` 的取消/租约检查
        读的正是它——不装的话，一轮里最长的两个阶段恰好成了唯一不检查取消的两个阶段。
        """
        self.execution.claim = self.round_claim
        return lane(run, b)

    def _lanes_together(self, run, b):
        """价格车道与相似品车道**同时**跑。

        为什么是线程不是 ``asyncio``：这一层全是同步调用（``self.pool`` 是
        ``ThreadPoolExecutor``，正文抓取本来就另有一个 ``ThreadPoolExecutor``，
        模型与搜索客户端也都是阻塞式），没有可以把这些调用挂上去的事件循环，
        改 asyncio 等于把整条链重写成异步。

        为什么不用 ``self.pool``：它是**整轮研究的调度器**，``max_workers=1``，
        把车道提交到它上面就是死锁。这里另起一个两线程的池，用完即关。

        两条车道汇合后一起把保底线关掉（``LANE_RESERVE_FLAG``）：保底是给并行的
        相似品车道留的，后面的「来源复核与结果汇总」是全轮的收尾，不该再受它限制。

        停止语义：任一条车道抛 ``ResearchStopped``（真额度用完或租约失效）时不立刻
        往外抛，先等另一条也停下来，否则另一条线程还在花钱，而外层已经把这轮记成
        "已停止"并把 ``running`` 的车道改回 ``pending``。

        异常处理：任意线程抛出异常时，不立刻终止整个后端进程，而是记录异常信息并标记
        该车道为失败状态，让另一条车道继续完成。
        """
        run[LANE_RESERVE_FLAG] = True
        stopped = None
        crashed: dict[str, BaseException] = {}
        try:
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix='research-lane') as pool:
                futures = {name: pool.submit(self._lane_thread, lane, run, b)
                           for name, lane in (('price', self._price_lane),
                                              ('alternatives', self._alternatives_lane))}
                for name, future in futures.items():
                    try:
                        future.result()
                    except ResearchStopped as exc:
                        if stopped is None:
                            stopped = exc
                    except Exception as exc:
                        # 一条车道崩了不该把另一条也拖死：先把它标成失败、让另一条跑完，
                        # 汇合后再照旧往上抛（``finally`` 之后的语义一个字没改）。
                        crashed[name] = exc
                        if name in run.get('lanes', {}):
                            run['lanes'][name] = 'failed'
        finally:
            run.pop(LANE_RESERVE_FLAG, None)
            for lane_name, error in crashed.items():
                # 同一件事的两个出口（``设计规范`` §3.4）：给读者的一句**不带异常类名**，
                # 给排查的类名、出错行与脱敏原文进 ``run['failures']``。异常原文不能写进
                # ``gaps``——那里是随报告渲染给用户看的话。
                run.setdefault('gaps', []).append(
                    '%s车道这一轮没有跑完，已标记为失败，结果按已取得的部分给出。'
                    % {'price': '价格', 'alternatives': '相似品'}.get(lane_name, lane_name))
                note_failure(run, '%s车道' % lane_name, 'lane_thread_failed', error)
            self.persist(run)
        if stopped is not None:
            raise stopped

    def _execute(self, task_id, run_id):
        run = self.get(task_id, run_id)
        try:
            run.update(status='running', stage='核查其他品牌' if run.get('active_lane') == 'alternatives' else '核查同款价格', started_at=now())
            run.pop('finished_at', None)
            run['unresolved_source_errors'] = False
            self.persist(run)
            b = run['baseline']
            if not run.get('product_source_checked'):
                with self.stage(run, '厂家资料核查'):
                    self.query(run, f"{b.get('brand') or ''} {b['model']} 官网 技术参数", 'official')
                    self.verify_publishers(run, b.get('brand'))
                    run['product_source_checked'] = True
                    self.persist(run)
            # 两条车道是否到期：判据与从前逐字相同，只是从"顺序执行的两个 if"
            # 变成"先算清楚，再决定串行还是同时跑"。``active_lane`` 缺省时两条都到期
            # ——新建一轮研究走的就是这条路（``create`` 不写 ``active_lane``）。
            price_due = (run.get('active_lane', 'price') in {'price', 'all'}
                         and run['lanes']['price'] != 'completed')
            alternatives_due = (run.get('active_lane', 'alternatives') in {'alternatives', 'all'}
                                and run['lanes']['alternatives'] != 'completed')
            if price_due and alternatives_due and facade_attr('LANES_TOGETHER', LANES_TOGETHER):
                run['stage'] = LANES_TOGETHER_STAGE
                # 留痕：这一轮的两条车道是同时跑的还是顺序跑的。阶段耗时在并行时
                # 会重叠，读报告的人需要一个字段能区分"重叠"和"记账错"。
                run['lanes_execution'] = 'together'
                self.persist(run)
                self._lanes_together(run, b)
            else:
                run['lanes_execution'] = 'serial'
                if price_due:
                    self._price_lane(run, b)
                if alternatives_due:
                    self._alternatives_lane(run, b)
            with self.stage(run, '来源复核与结果汇总'):
                self.recover_sources(run)
                # 上传资料自己的解析质量也要如实说：OCR 失败（或只完成一部分）却仍有
                # 文字被用进来的那一份，必须在缺口里点名，不能让报告静默地把它当成
                # 完整资料（``document_quality_gaps`` 的判据与实测见那里）。
                document_quality_notice(run, self.repo.list('documents', run['task_id']))
                # 这时这一轮不会再抓新页面了（``recover_sources`` 是最后一个会新增来源的
                # 步骤），所以在这里补厂家产品参数、由本段末尾那次 ``persist`` 落库：
                # ``prices()`` 末尾那次跑在价格通道里，而实测一轮 66 个来源里有 35 个是
                # 价格通道结束之后才抓回来的（见 ``hydrate_run``）。补到就落库、补不到
                # 就什么都不写，而且只填空配置，已有的配置不被顶掉。
                from backend.services.product_configuration import hydrate_run
                hydrate_run(run)
                from backend.services.price_leads import price_leads
                run['price_leads'] = price_leads(run['sources'], b)
                for candidate in run['alternatives']:
                    candidate['price_leads'] = price_leads(run['sources'], candidate)
                # 价格通道跑完之后才抓回来的正文，对它是**不存在**的：入口只读它跑的
                # 那一刻的 ``run['sources']``（见 ``prices()`` 开头）。实测一轮 66 个来源
                # 里有 35 个是价格通道结束之后才到货的，所以现在补读一次。
                # 只读"这一轮从没进过价格通道、且现在四条件都满足"的来源——判据读的是
                # 修法① 写下的留痕，不是猜的。判据栈一个字不改（仍然走 ``prices()``）。
                # ``aggregator_leads``／``recover_sources`` 抓回来的正文是价格通道
                # 结束之后才到的，它们的附件同样没人抓过；补一次再进补读，判据栈不动。
                self.backfill_publication_attachments(run)
                late_priced = self._rescan_late_price_sources(run, b)
                # ``no_official_source`` 是**每一次检索**各写一次，一个任务能写 20 多次。
                # 到这里来源已经取齐（``recover_sources`` 是最后一个会新增来源的步骤），
                # 所以现在是把它变成任务级事实的唯一正确时机：这一轮到底有没有官方来源，
                # 只有此刻知道。逐次检索的原始告警留在 ``run['search_attempts']`` 不动。
                run['gaps'] = reconcile_official_source_gaps(run.get('gaps'), run.get('sources'))
                # A 70% candidate is useful for comparison, but does not by itself
                # make the overall procurement run a completed recommendation.
                useful = bool(run['prices']) and any(a.get('status') == 'eligible' for a in run['alternatives']) and not run.get('unresolved_source_errors')
                run['status'] = 'completed' if useful and all(v == 'completed' for v in run['lanes'].values()) else 'partial'
                run['stage'] = '结果已保存'
                run['finished_at'] = now()
                # 这一轮到此收尾：下面是只读的取数与落库，不会再开任何车道，所以
                # ``active_lane`` 必须在这里清掉。它的唯一含义是"这一轮从哪条车道起步"
                # （``start_lane``／``resume`` 写入），留着就会被读成"还在跑那条车道"——
                # 实测一轮结束 22 分钟后它仍是 ``price``（设计规范）。就绪信号是
                # ``status`` + ``lanes`` + ``finished_at``，前端也只读 ``lanes``。
                run.pop('active_lane', None)
                from backend.services.procurement_rules import historical_summary
                run['historical_summary'] = historical_summary(run['prices'])
                from backend.services.procurement_rules import price_decision
                run['price_decision'] = price_decision(run['prices'], b, run['lanes']['price'])
                run['price_assessment'] = run['price_decision']['summary']
                if run['lanes']['price'] != 'pending' and not run['prices']:
                    # 前端在同样情形下印的是「本轮检索没有取得可采信的成交记录……
                    # 也不能据此说这台机器在别处没有成交记录」（``ResearchResults.jsx``）；
                    # 导出报告从前只有前半句，读起来像"市场上查不到这台机器的成交"。
                    # 查不到是"这一轮的检索没命中"，不是"不存在成交记录"——两件事，
                    # 报告与页面说同一件。
                    run['gaps'].append('本轮未取得可核实的同型号成交单价；'
                                       '这不等于这台机器在别处没有成交记录，'
                                       '只说明本轮检索没有取到可采信的记录。')
                comparable_alternatives = [a for a in run['alternatives'] if a.get('comparison_disposition') in {'comparable_candidate', 'recommended_comparison'}]
                if run['lanes']['alternatives'] != 'pending' and not comparable_alternatives:
                    # 没有确认过指标时不能报"没有达到70%相似度的候选"：这一轮根本没做
                    # 参数比对，相似度无从谈起，如实说没做比对。
                    #
                    # 但**不能**把"候选为零"归因给"没有实验要求"（2026-09-26 实测
                    # ``task-03d477711afc``）：发现阶段不看 ``parameters``，它是无条件
                    # 跑完的——那一轮 3 条发现查询照发、7 篇发现正文照抓、发现模型照调
                    # （结果就在 ``model_results`` 里，返回 ``{"candidates": []}``）。
                    # 候选有没有，是发现阶段的事实；有没有指标，只决定候选怎么比。
                    # 从前那一句把两件事写成因果，读报告的人会以为"补上实验要求就会有
                    # 候选"，而补上之后发现阶段一个字都不会变。所以这里按**真实原因**分：
                    # 一条候选都没发现，和发现了候选但没有可比依据，是两件事。
                    # 2026-10-08 再分细一层：从前"有指标"这一支无条件说"没有达到70%已核验
                    # 相似度"，而 69 条带这句话的记录里没有一条真有候选算过那个分数（见
                    # ``no_comparable_candidates_gap``）；拦住候选的是身份、名额或证据。
                    if run.get('comparison_basis') != NO_CONFIRMED_PARAMETER:
                        gap = no_comparable_candidates_gap(run)
                        if gap and gap not in run['gaps']:
                            run['gaps'].append(gap)
                    elif not (run.get('discovered_candidates') or run.get('alternatives')
                              or run.get('reference_candidates')):
                        run['gaps'].append('本轮没有从已取得的正文中识别出其他品牌的候选；'
                                           '本轮没有确认必须满足的实验要求，也未做逐项参数比对。'
                                           '这不等于市场上没有同类替代品——'
                                           '本轮窗口里没有取到可用的同类产品页时，'
                                           '不代表这类产品不存在。')
                    else:
                        run['gaps'].append('本轮没有确认必须满足的实验要求，未做逐项参数比对；'
                                           '识别到的候选没有进入可比较名单，因此没有可比对的跨品牌候选。')
                elif comparable_alternatives and not any(a['status'] == 'eligible' for a in run['alternatives']):
                    # 同上：没有指标时"没有候选被证明为全部关键参数满足"只是 0/0 的空话。
                    if run.get('comparison_basis') != NO_CONFIRMED_PARAMETER:
                        run['gaps'].append('已有达到70%已核验相似度的可比较候选，但没有候选被证明为全部关键参数满足。')
                self.persist(run)
            self.report(task_id, run_id)
        except ResearchStopped as exc:
            claim = getattr(self.execution, 'claim', None)
            if claim and self.repo.active_analysis(claim[0]) != claim[1]:
                self.repo.interrupt_orphaned_research(run_id)
                return
            if self.get(task_id, run_id)['status'] != 'cancelled':
                # A budget/lease stop can occur after a lane is marked
                # ``running`` but before its result is persisted.  Leaving
                # that marker behind makes a partial run look permanently
                # active and prevents a clean resume. Only the interrupted
                # lane is reopened; completed lanes remain completed.
                for lane in ('price', 'alternatives'):
                    if run.get('lanes', {}).get(lane) == 'running':
                        run['lanes'][lane] = 'pending'
                run.update(status='partial', stage='结果已保存', message=str(exc), finished_at=now())
                # 同上：``finished_at`` 一写这一轮就是终态，``active_lane`` 不再有意义
                # （继续研究走 ``resume``，它会重新写入 ``all``）。
                run.pop('active_lane', None)
                self.save(run)
                self.report(task_id, run_id)
        except Exception as exc:
            if self.get(task_id, run_id)['status'] == 'cancelled':
                return
            run.setdefault('gaps', [])
            lanes_complete = all(run.get('lanes', {}).get(lane) == 'completed'
                                 for lane in ('price', 'alternatives'))
            self.record_failure(run, '研究收尾' if lanes_complete else '研究执行', exc)
            if lanes_complete:
                run['unresolved_source_errors'] = True
                run['gaps'].append('研究收尾未完全完成，已有候选和价格结果已保存；请检查失败记录后继续。')
                run.update(status='partial', stage='结果已保存',
                           message='研究结果已保存，但收尾阶段未完全完成，请检查失败记录后继续。',
                           finished_at=now())
            else:
                run.update(status='failed', message='研究中断，已有结果已保存，请检查服务后继续。', finished_at=now())
            # 同上：终态不再声称还在跑某条车道。
            run.pop('active_lane', None)
            self.save(run)
        # 一轮结束（不管收尾是完成、部分完成还是失败）把这一轮的成果写进沉淀知识库
        # （K1~K5），并把以前轮次沉淀的**条数**取回来留痕。写库失败不影响研究结果。
        #
        # 但**取消的轮次没有终态成果**，不写沉淀、也不再落库：上面
        # ``except ResearchStopped`` 判定到 ``cancelled`` 时既不改状态也不 ``return``，
        # 会一路落到这里；而内存里的 ``run`` 还停在 ``running``，任何一次写回都会把
        # 库里刚写好的 ``cancelled`` 盖回去（``设计规范``）。判断口径与
        # ``except Exception`` 分支里的那条逐字一致。
        if self.get(task_id, run_id)['status'] == 'cancelled':
            return
        self.remember_knowledge(task_id, run)
