"""ProcurementResearch mixin: ReportingMixin."""
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

from backend.services.research.constants import NO_ALTERNATIVES_NOTE, OVERVIEW_FORBIDDEN_ENGLISH, OVERVIEW_FORBIDDEN_MARKERS, OVERVIEW_FORBIDDEN_PATTERNS, REPORT_OVERVIEW_PROMPT, ResearchStopped, logger, now
from backend.services.research.price_helpers import has_award_record, price_evidence_summary, price_rejection_section, reconcile_official_source_gaps, reconcile_tender_body_gaps
from backend.services.research.report_helpers import alternative_report_item, apply_empty_section_notes, baseline_section, document_quality_gaps, document_quality_notice, gap_action_summary, gap_label, mark_source_authority, redact_error_text, report_blockers, report_overview_facts, report_overview_headline, republished_section, sanitize_gap_text, sanitize_report, supplier_revision_section


class ReportingMixin:
    def remember_knowledge(self, task_id, run):
        """每轮结束写一次沉淀知识库（K1~K5），并把旧沉淀的取回结果挂到 ``run`` 上。

        三件事，任何一步出问题都不许影响这一轮已经得到的结果
        （``设计规范`` §5：一条失败不能放大成整轮失败）：

        * **写**：``knowledge_memory.record_run`` 按型号／品类存档；它自己会把
          "这一条为什么没存"逐条留痕（``ingest_notes``），这里不再复述；
        * **读**：按同一型号与品类把**以前**的沉淀取回来，只记条数——取回的行不并进
          ``run['prices']``、不参与任何判定（那是 ``price_memory`` 那条线上复用通道的
          事，规矩不同，见 ``knowledge_memory`` 的模块注释）；
        * **留痕**：把这次写入与取回的结论写回 run（``knowledge_memory`` /
          ``knowledge_memory_recall``）。这一步**只更新这两个键**，不走 ``save``
          ——内存里的 ``run`` 可能已经落后于库里的终态（见 ``_trace_knowledge``）。

        整个方法外面还兜了一层：连"记失败"这件事本身都不许把研究收尾带崩。
        """
        try:
            self._remember_knowledge(task_id, run)
        except Exception as exc:  # noqa: BLE001 - 存档绝不能打断研究收尾
            try:
                self._trace_knowledge(run, knowledge_memory={
                    'status': 'failed', 'error': redact_error_text(str(exc)),
                    'note': '沉淀存档失败，本轮研究结果不受影响。'})
            except Exception as trace_exc:  # noqa: BLE001 - 连留痕都写不动时也不抛
                # 写库与留痕**同时**失败时，原来的这里是 `return`：库没有记录、日志 0 条，
                # 痕迹只在内存 ``run`` 上、进程结束即消失——与项目自己的对外口径
                # （``设计规范`` §5「失败要留脱敏后的 error 原文」）在半截上冲突
                # （``设计规范`` §2d 第三条）。落不了库，至少**落到日志**：两条 error 都过
                # ``redact_error_text``，不带密钥、不带 cookie（与留痕同一把尺子）。
                # 仍然不抛——把研究收尾带崩比留痕缺失更糟。
                logger.warning(
                    '沉淀存档与留痕同时失败，这一格只能留在日志里：写库失败=%s；留痕失败=%s',
                    redact_error_text(str(exc)), redact_error_text(str(trace_exc)))

    def _trace_knowledge(self, run, **fields):
        """把沉淀留痕写回库：只更新给定字段，其余字段以库里的当前值为准。

        为什么不能用 ``self.save(run)``：``run`` 是内存里的那一份，取消／中断
        路径上它可能还停在 ``running``（甚至带着 ``finished_at`` 之前的状态），
        而库里已经是别处刚写好的终态；整行写回就会把那个终态盖回去
        （``设计规范`` 那条回归就是这么来的）。``repo.merge_procurement_record``
        在同一个事务里读回当前行、只合并这几个键。

        ``updated_at`` 跟着刷一次，与从前 ``save`` 的时间戳行为一致；除它之外
        一个终态字段都不动。
        """
        stamp = now()
        run.update(fields)
        run['updated_at'] = stamp
        self.repo.merge_procurement_record(run['id'], {**fields, 'updated_at': stamp})

    def _remember_knowledge(self, task_id, run):
        from backend.services import knowledge_memory
        if not knowledge_memory.enabled():
            return
        baseline = run.get('baseline') if isinstance(run.get('baseline'), dict) else {}
        category = ''
        try:
            from backend.services.task_title import task_identity
            documents = self.repo.list('documents', task_id) or []
            task = self.repo.get('tasks', task_id) or {}
            identity = task_identity(task, list(run.get('alternatives') or []), documents, baseline)
            category = str(identity.get('category') or '').strip()
        except Exception:
            # 品类只是 K5 的键；推断不出来就按"没有品类"处理（K5 会如实记一条跳过）。
            category = str(baseline.get('category') or '').strip()
        summary = knowledge_memory.record_run(run, category=category, run_dir='')
        trace = {'knowledge_memory': {
            'status': 'stored', 'category': category, 'database': summary['database'],
            'written': summary['written'], 'updated': summary['updated'],
            'skipped': summary['skipped'],
            'failed': summary['failed'], 'already_stored': summary['already_stored'],
            'note': '沉淀只作存档，不参与本轮任何采信判定。',
        }}
        try:
            recalled = knowledge_memory.recall({'model': baseline.get('model'),
                                                'brand': baseline.get('brand'),
                                                'category': category})
        except Exception as exc:  # noqa: BLE001 - 取回同样只作留痕
            trace['knowledge_memory_recall'] = {'status': 'failed',
                                                'error': redact_error_text(str(exc))}
            self._trace_knowledge(run, **trace)
            return
        model_rows = recalled.get('model') or {}
        category_rows = recalled.get('category') or {}
        trace['knowledge_memory_recall'] = {
            'status': 'read',
            'model': baseline.get('model') or '', 'category': category,
            'price_evidence': len(model_rows.get('price_evidence') or []),
            'configuration_evidence': len(model_rows.get('configuration_evidence') or []),
            'model_spellings': len(model_rows.get('model_spellings') or []),
            'bad_sources': len(model_rows.get('bad_sources') or []),
            'category_parameters': len(category_rows.get('parameters') or []),
            'note': '这些是以前轮次沉淀下来的条数；明细走 knowledge_memory.recall()，不并进本轮结果。',
        }
        self._trace_knowledge(run, **trace)

    def report_overview(self, run, decision):
        """报告总述：结构部分机械取数，文字部分交给模型，模型不可用就降级。

        这里只多花一次模型调用，走 ``self.model`` 既有的额度、缓存与失败语义。
        额度用完、租约失效或模型报错都不往上抛：报告骨架不能因为少了一段文字而消失，
        而 ``report()`` 在 ``_execute`` 末尾也被调用，抛出会连带整份报告缺失。
        降级时 ``headline`` 由已保存的数据直接拼出，一个数都不是新编的；它也**不重复**
        各 section 自己的 summary（``prices``/``decision`` 已经各有一句），而是汇总口径。
        """
        facts = report_overview_facts(run, decision)
        blockers = report_blockers(run)
        summary = {'headline': report_overview_headline(run, decision),
                   'facts': facts, 'blockers': blockers, 'narrative': '',
                   'narrative_status': 'unavailable',
                   'narrative_note': '模型总述未生成，结论取自已保存的价格判断。'}
        limits, usage = run.get('limits') or {}, run.get('usage') or {}
        if 'model' not in limits or usage.get('model', 0) >= limits['model']:
            summary['narrative_note'] = '本轮模型调用额度已用完，未生成模型总述；结论取自已保存的价格判断。'
            return summary
        # 交给模型的只有已保存的数据：facts 机械取数，缺口用已换成中文的那一份，
        # 价格判断用规则引擎自己的那句话（模型可以概括，但没有新事实可用）。
        # 状态枚举不进去——它已经在 facts 的「比价依据」里用中文写着了。
        packet = {'baseline': {key: (run.get('baseline') or {}).get(key) for key in
                               ('brand', 'model', 'category', 'scenario', 'supplier_price', 'currency',
                                'quantity', 'configuration')},
                  'price_decision': (decision or {}).get('summary'),
                  'facts': facts, 'gaps': blockers}
        try:
            # 这一笔是报告自己的付费调用，用 ``stage`` 记账：一轮研究要求「每一笔付费调用
            # 都落在某个计时阶段里」（见 tests/test_research_stage_timings.py），
            # 而 report() 是在最后一个阶段之后才被调用的。
            with self.stage(run, '报告总述'):
                raw = self.model(run, REPORT_OVERVIEW_PROMPT, packet)
        except ResearchStopped:
            summary['narrative_note'] = '模型调用已停止，未生成模型总述；结论取自已保存的价格判断。'
            return summary
        except Exception:
            summary['narrative_note'] = '模型暂时不可用，未生成模型总述；结论取自已保存的价格判断。'
            return summary
        raw = raw if isinstance(raw, dict) else {}
        headline, narrative = str(raw.get('headline') or '').strip(), str(raw.get('narrative') or '').strip()
        if not headline or not narrative:
            summary['narrative_note'] = '模型没有按要求给出总述（缺 headline 或 narrative），结论取自已保存的价格判断。'
            return summary
        rejected = any(marker in headline or marker in narrative for marker in OVERVIEW_FORBIDDEN_MARKERS)
        rejected = rejected or any(pattern.search(headline) or pattern.search(narrative)
                                   for pattern in OVERVIEW_FORBIDDEN_PATTERNS)
        rejected = rejected or bool(OVERVIEW_FORBIDDEN_ENGLISH.search(headline + ' ' + narrative))
        if rejected:
            summary['narrative_note'] = '模型给出的总述不符合本报告的写法要求（去 AI 腔、只用中文），已弃用；结论取自已保存的价格判断。'
            return summary
        summary.update(headline=headline[:200], narrative=narrative[:1200],
                       narrative_status='model', narrative_note='')
        return summary

    def price_report_section(self, run, price_gap_labels):
        """报告「成交项目核价」那一节的每一项。

        抽出来是因为它有两处调用，而且必须是同一份实现：新建报告时建这一节，
        以及读一份**已经存下来**的旧报告时就地刷新这一节（见 ``report``）。
        两边各写一遍的话，刷新出来的那一份迟早与新建的那一份说法不一致。
        """
        from backend.services.product_configuration import report_configuration
        return [{'label': q['model'],
                 'text': f"{format_money(q['unit_price'])} {q['currency']}；日期：{q.get('date') or '未知'}；配置：{report_configuration(q)}；缺口：{'、'.join(price_gap_labels.get(gap, gap) for gap in q.get('gaps', [])) or '无'}",
                 # 配置来自厂家资料时，出处在别处：把它一并列出来，报告里的每一段
                 # 参数都点得开。见 ``product_configuration``。
                 'evidence': [{'url': q['url']}] + ([{'url': q['configuration_url']}] if q.get('configuration_url') else [])}
                for q in run['prices']]

    def refresh_price_section(self, report, run, price_gap_labels):
        """就地把已存报告的「成交项目核价」一节刷成当前价格记录的样子。

        返回是否真的改了（没改就不必写回）。找不到这一节（更早的报告版式）就返回
        ``False``：不认识的报告不动它。
        """
        section = next((s for s in report.get('sections') or [] if s.get('key') == 'prices'), None)
        if section is None:
            return False
        items = self.price_report_section(run, price_gap_labels)
        if section.get('items') == items:
            return False
        section['items'] = items
        return True

    def refresh_document_notice(self, report, run):
        """把上传资料的解析质量提示补进**已存报告**的缺口一节（幂等）。

        报告是建它那一刻写死的，而 ``document_quality_notice`` 读的是 ``documents``
        的当前状态：那份资料可能在报告建好之后才上传、或才被识别完。不补这一步，
        库里已有的报告就会继续对"OCR 失败但仍有正文"只字不提。
        """
        section = next((s for s in report.get('sections') or [] if s.get('key') == 'gaps'), None)
        if section is None:
            return False
        wanted = document_quality_gaps(self.repo.list('documents', run['task_id']))
        if not wanted:
            return False
        items = section.setdefault('items', [])
        present = {item.get('text') for item in items if isinstance(item, dict)}
        changed = False
        for gap in wanted:
            if gap not in present:
                items.append({'label': gap_label(gap), 'text': gap})
                changed = True
        return changed

    def refresh_republished_section(self, report, run):
        """按 ``has_award_record`` 把已存报告里的「二手成交记录」一节增删到位。

        报告是**存下来**的：库里在本次口径之前建的报告，正文里那一节还在。
        读报告时顺手改掉，旧任务的页面才不会继续印出与新报告两种说法。
        只在真的增删了那一节时返回 ``True``（幂等）；更早版式的报告（没有 ``key``
        的 sections，连「成交项目核价」那一节都认不出来）原样不动。
        """
        sections = report.get('sections') or []
        if not any(section.get('key') == 'prices' for section in sections):
            return False
        republished = republished_section(run)
        kept = has_award_record(run)
        position = next((i for i, s in enumerate(sections) if s.get('key') == 'republished'), None)
        if kept:
            if position is None:
                return False
            sections.pop(position)
            return True
        if position is not None:
            # 已经有这一节：正文按当前数据重刷，避免旧快照与新口径两种说法。
            changed = sections[position] != republished
            sections[position] = republished
            return changed
        index = next((i for i, s in enumerate(sections) if s.get('key') == 'alternatives'), len(sections))
        sections.insert(index, republished)
        return True

    def refresh_gap_section(self, report):
        """把已存报告里那些带内部注解的缺口条目去掉。

        那句注解（「这是检索阶段的判定；研究阶段已取得正文并据其核价，见来源清单」）
        是旧版 ``reconcile_tender_body_gaps`` 拼上去的，**只拼给"正文已经读到"的那些
        缺口**（已测 164 条，``设计规范``），所以它正好是"这条对读者已经不成立"的记号。
        库里的报告是存下来的，旧任务读一次就按当前口径清一遍；那句内部话一条都不再印。
        保留其余条目原样（含后面按逐项比较追加的冲突条目）。
        """
        section = next((s for s in report.get('sections') or [] if s.get('key') == 'gaps'), None)
        if section is None:
            return False
        items = section.get('items') or []
        changed = False
        for item in items:
            text = sanitize_gap_text(item.get('text'))
            if text != item.get('text'):
                item['text'] = text
                changed = True
        kept = [item for item in items if '这是检索阶段的判定' not in str(item.get('text') or '')]
        if len(kept) != len(items):
            section['items'] = kept
            changed = True
        return changed

    def refresh_evidence_state_sections(self, report, run):
        """按当前 run 的口径重刷「比较基准」「销售回复更新」与缺口节的查询账（幂等）。

        库里的报告是**建它那一刻**的措辞：那时缺的销售字段一律写「用户未填写」，
        读者分不出"没问"和"问了、对方说没有"；补全销售回复的旧报告里那节还是个
        只有引导语的空节。读的时候按当前 run 重刷，页面与导出才是一个说法。
        """
        sections = report.get('sections') or []
        changed = False
        fresh = baseline_section(run)
        position = next((i for i, s in enumerate(sections) if s.get('key') == 'baseline'), None)
        if position is not None and sections[position] != fresh:
            sections[position] = fresh
            changed = True
        revised = supplier_revision_section(run)
        position = next((i for i, s in enumerate(sections) if s.get('key') == 'supplier_revision'), None)
        if revised is None:
            if position is not None:
                sections.pop(position)
                changed = True
        elif position is None:
            sections.append(revised)
            changed = True
        elif sections[position] != revised:
            sections[position] = revised
            changed = True
        gaps = next((s for s in sections if s.get('key') == 'gaps'), None)
        if gaps is not None:
            label = '本轮为这些缺口做过的查询'
            item = gap_action_summary(run)
            items = [entry for entry in (gaps.get('items') or []) if str(entry.get('label')) != label]
            if item:
                items.insert(0, item)
            if items != (gaps.get('items') or []):
                gaps['items'] = items
                changed = True
        return changed

    def refresh_price_rejection_section(self, report, run):
        """按当前 ``price_rejections`` 把「未被采用的价格与原因」这一节增删到位。

        与 ``refresh_price_section`` / ``refresh_republished_section`` 同一套规矩：
        报告是**存下来**的，库里在本次口径之前建的报告正文里没有这一节（或还带着
        「见「证据缺口」」那句承诺），读的时候按当前记录重刷一遍，旧任务的页面才
        不会继续印出与新报告两种说法。找不到「成交项目核价」那一节的更早版式不动。
        """
        sections = report.get('sections') or []
        prices = next((i for i, s in enumerate(sections) if s.get('key') == 'prices'), None)
        if prices is None:
            return False
        fresh = price_rejection_section(run)
        position = next((i for i, s in enumerate(sections) if s.get('key') == 'price_rejections'), None)
        if fresh is None:
            if position is None:
                return False
            sections.pop(position)
            return True
        if position is None:
            sections.insert(prices + 1, fresh)
            return True
        if sections[position] == fresh:
            return False
        sections[position] = fresh
        return True

    def report(self, task_id, run_id):
        run = self.get(task_id, run_id)
        if run['status'] in {'running', 'queued'}:
            raise ValueError('研究仍在进行，请等待或停止后导出')
        b = run['baseline']
        # Old runs may contain website list prices. New reports apply the current policy.
        run = copy.deepcopy(run)
        # 报告正文里的「配置：」写什么由 ``report_configuration`` 决定，而它读的是价格
        # 记录上的 ``configuration_source``。库里的 run 可能是在这份推导之前跑出来的
        # （或者产品参数页是价格通道结束之后才抓回来的），所以先按当前来源补一遍——
        # 用的是页面读的同一个纯函数，报告与页面不会给出两种说法。
        from backend.services.product_configuration import hydrate_run
        hydrate_run(run)
        # 同一个道理，同一份报告里也不能一边说"未找到已核实的官方来源"、一边列着官方来源。
        # 历史 run 的 ``gaps`` 里存的是逐次检索写的 ``no_official_source``，改不掉了，
        # 所以也在读的时候按任务级收敛一次。
        run['gaps'] = reconcile_official_source_gaps(run.get('gaps'), run.get('sources'))
        # 报告是**存下来**的：库里已有的 run 是在这条提示之前跑出来的，读报告时
        # 按当前口径再补一次，旧任务才不会继续对"OCR 失败但仍有正文"只字不提。
        # 幂等，与上面两条 reconcile 同一个位置、同一个道理。
        document_quality_notice(run, self.repo.list('documents', run['task_id']))
        run['prices'] = [q for q in run['prices'] if q.get('kind') in {'award', 'contract'}]
        # 报告的取材与页面同一口径：有官方成交记录时，这一轮判定为不列出的来源及其
        # 转载口径的文字，在正文里也不出现（判据与 ``display()`` 同一个）。
        mark_source_authority(run)
        for candidate in run['alternatives']:
            candidate['prices'] = [q for q in candidate.get('prices', []) if q.get('kind') in {'award', 'contract'}]
        from backend.services.procurement_rules import price_decision
        from backend.services.product_configuration import report_configuration
        decision = run.get('price_decision') or price_decision(run['prices'], b, run.get('lanes', {}).get('price', 'completed'))
        run['price_decision'] = decision
        run['price_assessment'] = decision['summary']
        # 这一路本来就要写回 run（把上面两个字段存下来），推导因此**顺带落一次库**：
        # 库里已有的旧 run 在有人读过一次报告之后就带上出处了，与页面读到的完全一致。
        # 这是幂等的，不是"每次读都写新东西"；而报告在读的时候本来就拒绝正在跑的 run
        # （见本方法开头），所以不存在把研究线程的新状态盖掉的窗口。
        self.save(run)
        # 检索阶段写过「采购公告正文待核实」、而研究阶段已经把那份正文抓回来的缺口，
        # **只在展示副本上**撤掉（``display()`` 走同一个函数，页面与报告同一份）。
        # 落库放在它前面：库里的缺口是这一轮检索的原始记账，读一次报告不该把它删掉。
        # 顺手过一遍 ``sanitize_gap_text``：库里的旧缺口还写着 ``120shop``、
        # ``（TimeoutError）``，报告这一层不许再印这些（库里那份不动）。
        #
        # 净化结果**只放在局部变量里**，绝不写回 ``run['gaps']``：``report_overview``
        # 那一句模型调用的前后会 ``persist(run)``，写回就等于把净化后的文字落库，
        # 库里那份原始记账（``tender_body_unverified: …``）就没了。上一版正是这样漏的，
        # 被 ``test_a_stored_gap_reaches_the_report_as_a_human_sentence`` 抓住。
        display_gaps = [sanitize_gap_text(gap)
                        for gap in reconcile_tender_body_gaps(run.get('gaps'), run.get('sources'))]
        price_gap_labels = {'brand': '品牌', 'configuration': '配置', 'quantity': '数量', 'tax': '税费',
                            'shipping': '运费', 'installation': '安装培训', 'warranty': '保修',
                            'service_response': '报修响应', 'service_onsite': '上门处理', 'delivery': '交付',
                            'date': '公告日期'}
        # 资料型号和销售口述的型号可能是两台机器。报告必须分开写清楚，
        # 否则要么把厂家资料里的型号说成「销售推荐」，要么把口述型号的报价
        # 安到资料型号头上——这正是本仓库最忌讳的错。节体本身在
        # ``baseline_section`` 里（库里已存的报告也要按同一套口径重刷）。
        # 本轮确认过指标没有？全篇报告共用这一个判定，避免同一件事在两节里说法不一。
        # 老 run 没有这个字段时按它自己的 baseline 现算（口径与 confirm 时一致）。
        no_metric = (run.get('comparison_basis')
                     or comparison_basis(b.get('parameters'))['comparison_basis']) == NO_CONFIRMED_PARAMETER
        sections = [baseline_section(run), {'key': 'prices', 'title': '成交项目核价', 'summary': price_evidence_summary(run),
                'items': self.price_report_section(run, price_gap_labels)},
            # 合并说明（211 并 master@3f0e9059）：这一节两边各改写了一遍——``master`` 侧
            # （210 那单）把条目抽成 ``alternative_report_item``、补了「零候选」那句与
            # ``reference_candidates`` 线索；本单（211）没动这一节，所以整节取 ``master`` 侧。
            # 「证据缺口」那一行两边**都**改了：``master`` 侧改用 ``report_blockers(run)``
            # （``_human_gap`` ＋ ``humanize_gap_text``），本单改用 ``gap_action_summary(run)``
            # ＋ ``display_gaps``（``reconcile_tender_body_gaps`` ＋ ``sanitize_gap_text``）。
            # 取本单那行：两行都走 ``humanize_gap_text``，本单多一层旧版阶段注记的清理与
            # 「这一轮做过什么」的账；``master`` 的 ``report_blockers`` 仍用在总述的 blockers 上，
            # 没被丢掉。**这一处是语义合并，已报主管道确认。**
            {'key': 'alternatives', 'title': '同档替代核查', 'summary': (
                # 零候选单独一句。有候选时那两句（"只按价格与来源比" / "达到 70% 已核验
                # 相似度的候选可进入比较"）在零候选时都兑现不了：前者让读者以为下面本该
                # 有几条被省略，后者是空话。零候选那半句直接用 ``NO_ALTERNATIVES_NOTE``：
                # 前端在同样情形下印的正是这句（``ResearchResults.jsx``），而这个常量同时
                # 是空节引导语的唯一出口（``apply_empty_section_notes``，新建的报告与读回来
                # 的旧报告都走它）——两边各写一句同义的话，同一份报告就会两条路两种说法，
                # 而且这一处写的那句永远被那一处覆盖、到不了读者眼前。
                (NO_ALTERNATIVES_NOTE
                 + ('本轮没有确认必须满足的实验要求，未做逐项参数比对。' if no_metric
                    else '也没有候选达到70%已核验相似度。')
                 + ('下面列出的是本轮从正文原文行里直接读到的型号线索（未进入跨品牌核验名额，'
                    '原因见每一条），供人工判断。' if run.get('reference_candidates') else ''))
                if not run['alternatives'] else
                ('本轮没有确认必须满足的实验要求，未做逐项参数比对；这里只列价格与来源，不判定关键参数是否满足。'
                 if no_metric else
                 '达到70%已核验相似度的候选可进入比较；满足、差异和缺证分别列出，未知不算满足。')),
              'items': ([alternative_report_item(a, verified=True) for a in run['alternatives']]
                        or [alternative_report_item(a, verified=False)
                            for a in (run.get('reference_candidates') or [])])},
            {'key': 'gaps', 'title': '证据缺口', 'summary': run.get('message', ''), 'items': (
                [gap_action_summary(run)] if gap_action_summary(run) else []) + [
                {'label': gap_label(g), 'text': g} for g in dict.fromkeys(display_gaps)]}]
        # 「二手成交记录（聚合站转载）」只在**这一轮一条官方成交记录都没有**时产出：
        # 那时它是唯一的参考，藏起来读者会以为市场上没有任何记录。一旦有官方记录，
        # 报告正文里连这一节的标题都不出现（业务口径：有官方记录时聚合站相关内容
        # 一个字都不出现）。判据是 ``has_award_record``，与来源清单、页面上的聚合站
        # 线索区块用的是同一个事实，三处不各判一次。正文用 ``republished_section``
        # 一处生成——这份报告存下来之后再读（``refresh_republished_section``）也是它。
        if not has_award_record(run):
            sections.insert(2, republished_section(run))
        # 被拒收的金额明细与原因：前端核价页一直列着（12 条，含引文与快照指纹），
        # 导出的报告一条都没有过。这一节的全部字段都取自 ``run['price_rejections']``，
        # 与前端读的是同一份记录，不重算任何判据。
        rejections = price_rejection_section(run)
        if rejections:
            sections.insert(next(i for i, s in enumerate(sections) if s['key'] == 'prices') + 1, rejections)
        # 「本次采购判断」保留规则引擎自己写的那句话（``price_decision['summary']``），
        # 「成交项目核价」改报本轮读到了什么。两节原本逐字相同，同一句话在报告里出现两遍；
        # 现在只剩这里一处判断，另一处是证据汇总。
        # 「其中 N 款全部关键指标满足」在**没有确认过指标**时会退化成"全部 0 项都满足"——
        # 那是没有依据的结论。这轮没有指标就如实说没有指标，不说满足。
        basis_note = ('本轮没有确认必须满足的实验要求，未做逐项参数比对：下面的候选只按价格与来源比较，'
                      '不能判定关键指标满足或不满足。' if no_metric else
                      f"{sum(a.get('comparison_disposition') in {'comparable_candidate', 'recommended_comparison'} for a in run['alternatives'])}款达到70%已核验相似度，"
                      f"其中{sum(a.get('status') == 'eligible' for a in run['alternatives'])}款全部关键指标满足。相似候选不等于完全替代，差异和缺证见逐项比较。")
        sections.insert(0, {'key': 'decision', 'title': '本次采购判断',
                            'summary': decision['summary'],
                            'items': [{'label': '其他品牌', 'text': basis_note},
                                      {'label': '下一步', 'text': ('已收到销售回复；请将主机、记录模块、导出功能及服务承诺写入完整报价或合同，并提供功能演示与验收方式。销售陈述尚不等于厂家核验。' if run.get('supplier_revision') else '请销售补齐报价配置、税运费用、保修期限、报修响应和上门处理时限。将功能选件费用计入总价，再比较完整方案。')}]})
        report_id = 'report-' + hashlib.sha256((run_id + str(run['round'])).encode()).hexdigest()[:24]
        existing = self.repo.get('reports', report_id)
        if existing:
            report = existing['report']
            if report.get('summary'):
                # 报告是存下来的，正文里的文字是**建它那一刻**算出来的。库里那份
                # 「配置：公告正文及已取得的附件未提供该项」就是这么来的：建它的时候
                # 这份推导还不存在。价格记录现在有厂家参数了，正文却还说没有——同一
                # 件事在两个地方各说一套。这里只按当前价格记录重刷那一节，不重建整份
                # 报告、更不调模型取总述（``report_overview`` 要花一次调用）。重刷是
                # 幂等的：第二次读时文本已经一样，什么都不写。
                changed = self.refresh_price_section(report, run, price_gap_labels)
                changed = self.refresh_republished_section(report, run) or changed
                # 这一排取并集：``master`` 侧的「读到旧报告时补一次上传资料提示」与
                # 本单的四节重刷，改的是同一排调用不是二选一（见 设计说明）。
                changed = self.refresh_document_notice(report, run) or changed
                changed = self.refresh_price_rejection_section(report, run) or changed
                changed = self.refresh_gap_section(report) or changed
                changed = self.refresh_evidence_state_sections(report, run) or changed
                changed = apply_empty_section_notes(report.get('sections')) or changed
                if changed:
                    self.repo.put('reports', report_id, {**existing, 'report': report}, task_id=task_id)
                # 库里那份报告是**建它那一刻**的取材与措辞，读的时候按当前口径清一遍；
                # 落库的那一份不动（原始证据留在库里，页面拿到的是展示副本）。
                return sanitize_report(report, run)
            # 这份报告存下来时还没有总述字段。补一次并写回，之后走上面的快速返回，
            # 不会每次读报告都调模型；补的时候同样不许抛（否则读旧报告就报错）。
            report['summary'] = self.report_overview(run, decision)
            self.refresh_price_section(report, run, price_gap_labels)
            self.refresh_republished_section(report, run)
            # 补上传资料提示这件事**两条路都要走到**。这条「这份报告存下来时还没有总述
            # 字段」的旧路以前漏了它：库里早期建的旧报告**第一次打开不补提示、第二次才
            # 补**（上面那条快路径里有它）。``refresh_document_notice`` 自己是幂等的
            # ——按文本去重后才 append，重复调用不会写第二遍。
            self.refresh_document_notice(report, run)
            self.refresh_price_rejection_section(report, run)
            self.refresh_gap_section(report)
            self.refresh_evidence_state_sections(report, run)
            apply_empty_section_notes(report.get('sections'))
            self.repo.put('reports', report_id, {**existing, 'report': report}, task_id=task_id)
            return sanitize_report(report, run)
        sections.append({'key': 'alternative_prices', 'title': '替代候选价格依据',
                         'summary': '有历史单价不等于关键指标合格；配置和交易条件未齐时不能判更划算。',
                         'items': [{'label': a['model'], 'text': f"{format_money(q['unit_price'])} {q['currency']}；数量：{q.get('quantity') or '未知'}；日期：{q.get('date') or '待核实'}",
                                    'evidence': [{'url': q['url']}]} for a in run['alternatives'] for q in a.get('prices', [])]})
        sections.append({'key': 'comparison_details', 'title': '指标余量与功能偏好',
                         'summary': '数值余量仅对应已核实的同条件指标，不等于整体更优；功能偏好不抵消关键指标不足。',
                         'items': [entry for a in run['alternatives'] for entry in (
                             [{'label': a['model'] + ' · ' + r['name'], 'text': f"核实值 {r['observed']['value']} {r['observed'].get('unit', '')}；基准要求 {r['required']['value']} {r['required'].get('unit', '')}", 'evidence': [r['observed']['evidence']]} for r in a.get('stronger_parameters', [])]
                             + [{'label': a['model'] + ' · ' + p['preference'], 'text': p['note'], 'evidence': p['evidence']} for p in a.get('preference_checks', [])])]})
        for a in run['alternatives']:
            for row in a.get('matrix', []):
                if row.get('conflict'):
                    next(s for s in sections if s['key'] == 'gaps')['items'].append({'label': a['model'] + ' · ' + row['required']['name'], 'text': '官方原文存在冲突，不能判为满足', 'evidence': [o['evidence'] for o in row.get('observations', [])]})
        for section in sections:
            if section['key'] == 'decision':
                section['summary'] = decision['summary']
        sections.append({'key': 'supplier_questions', 'title': '向销售确认的三组问题', 'items': [
            {'label': '配置与完整价格', 'text': '请按上述用途逐项确认主机与选件清单、功能实现方式和演示材料，给出含税、运费、安装培训和软件费用的完整报价。厂家选配不等于本次报价已包含。'},
            {'label': '性能与验收', 'text': '请提供本次交付完整型号的参数表，说明关键性能在实际使用条件下的承诺值和验收方式。需求未量化的稳定性不能直接判为达标。'},
            {'label': '售后与交期', 'text': '请分别说明主机及选件的质保期、报修响应时间、上门时间、人工差旅费用及交货安装时间；历史项目承诺不自动适用于本次报价。'}]})
        from backend.services.product_facts import (table_facts, dimension_conflicts, PRODUCT_FIT_BOUNDARY,
                                                    document_facts, model_form_ambiguity)
        facts = table_facts(run['sources'], b['model'])
        # 本任务上传资料里由资料分析提取、带页码的那一组（页面上「型号与参数」页展示的
        # 就是它）。报告原来只读联网来源，于是上传资料里的厂家参数一条都进不来。
        documents = {record.get('id'): record.get('source_name')
                     for record in self.repo.list('documents', task_id) if record.get('id')}
        candidates = self.document_candidates(task_id)
        uploaded = document_facts(candidates, b.get('brand'), b['model'], documents)
        # 同一台机器在原文里有多种写法（带后缀的那种往往对应另一个配置版本）时，报告
        # 不许替读者挑一种就当没这回事：并列出来，并写明正文用的是哪一种口径。
        spelling = model_form_ambiguity(run['sources'], candidates, b['model'])
        # 上传的资料常是外文原件：参数按原文照抄（采购员要拿它跟资料核对），所以报告里
        # 会出现整句外文。这不是内部话，但先说清楚，免得读者以为报告串了语言。
        if uploaded and facts:
            product_summary = ('以下参数来自公开来源与本任务上传的产品资料，两处都标了出处；'
                               '上传资料是外文原件时按原文照抄。资料里的标称值不等于实验要求已满足。'
                               + PRODUCT_FIT_BOUNDARY)
        elif uploaded:
            product_summary = ('本轮没有从公开来源取得本型号的厂家参数表；以下参数取自「本任务上传的产品资料」'
                               '（资料分析已提取，逐条带页码入口；外文原件按原文照抄）。'
                               '资料里的标称值不等于实验要求已满足。' + PRODUCT_FIT_BOUNDARY)
        elif facts:
            product_summary = ('以下参数绑定本型号的厂家表格列。选配清单不证明报价已含，接口不证明完整记录方案。'
                               + PRODUCT_FIT_BOUNDARY)
        else:
            product_summary = ('本任务上传的产品资料没有给出本型号的参数，本轮也没有从公开来源取得厂家参数表；'
                               '这一节没有可罗列的资料原文。' + PRODUCT_FIT_BOUNDARY)
        # 口径（2026-10-07，规范项）：这一节只摆资料原文，不再产出「本轮要求 · X：比较基准
        # 要求 A；资料里对应写法 B」那种把采购要求和产品参数印在同一句里的条目——读者
        # 会把两者当成同一件事。本轮要求本身在报告的「比较基准」一节（``PRODUCT_FIT_BOUNDARY``
        # 那句话把这条边界对读者说清楚）。原来的 ``requirement_notes`` 是那些条目的唯一来源，
        # 随这次口径变更一起下线。
        sections.insert(2, {'key': 'product_fit', 'title': '产品事实与用途适配',
            'summary': product_summary,
            'items': ([spelling] if spelling else [])
                     + dimension_conflicts(run['sources'], b['model'])
                     + [{'label': fact['model'], 'text': fact['text'], 'evidence': [fact['evidence']]} for fact in facts]
                     + uploaded})
        # 总述另开一个顶层键 ``summary``，前端在报告折叠时也能一处直读。
        # 结构部分（headline、facts、blockers）全部机械取数，所以模型不可用、额度用尽
        # 时报告仍然有骨架，降级少的只是一段文字。
        # 空节的引导语由 ``apply_empty_section_notes`` 一处落地：新建的报告与读旧报告
        # 时重刷的那一份走同一个函数，不会一边说「已列出」一边 0 条。
        apply_empty_section_notes(sections)
        report = {'report_id': report_id, 'baseline_version': b['version'], 'completion': run['status'], 'created_at': now(), 'status': 'recommendations' if run['status'] == 'completed' else 'needs_review', 'summary': self.report_overview(run, decision), 'sections': sections}
        if run.get('parent_report_id'):
            report['parent_report_id'] = run['parent_report_id']
            report['supplement_results'] = run.get('supplement_results', [])
            if run.get('supplier_revision'):
                report['supplier_revision'] = run['supplier_revision']
                # 逐项写出这次回复补了什么、依据在哪、哪些项对方说资料里没有；
                # 只写一句引导语、items=0，等于让读者自己去猜（盲测 B01 第 8 条）。
                revised = supplier_revision_section(run)
                if revised:
                    sections.append(revised)
                questions = next(s for s in sections if s['key'] == 'supplier_questions')['items']
                questions[0]['text'] = '请将已回复的主机与选件清单、各项费用和功能实现方式落实为完整书面报价，并提供记录及导出功能演示；销售承诺仍需核实。'
                questions[2]['text'] = '请将已回复的质保、响应、上门和交货时间写入合同，明确主机及选件的责任方、人工差旅费用和逾期处理方式。'
            else:
                sections.append({'key': 'supplement', 'title': '外部补证核验', 'summary': '原报告保留；外部结论仅为线索',
                                 'items': [{'label': r.get('model', ''), 'text': r.get('reason', '')} for r in run.get('supplement_results', [])]})
        self.repo.put('reports', report_id, {'report': report, 'research_run_id': run_id, 'baseline_id': b['id'], 'snapshot': run}, task_id=task_id)
        return sanitize_report(report, run)
