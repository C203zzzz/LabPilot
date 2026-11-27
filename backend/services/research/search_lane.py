"""ProcurementResearch mixin: SearchLaneMixin."""
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

from backend.services.research.constants import (
    CHALLENGE_COOLDOWN_SECONDS,
    CHALLENGE_QUARANTINE_MARKER,
    CHALLENGE_QUARANTINE_THRESHOLD,
    EDUCATION_PLATFORM_LIMIT,
    PROVINCE_AWARD_LIMIT,
    QUARANTINE_AFTER_FAILURES,
    ResearchStopped,
    SEARCH_RESULT_LIMIT,
    SEARCH_STRATEGY_VERSION,
    is_low_value_social_url,
    now,
)
from backend.services.research.report_helpers import extend_gaps_once, note_failure, note_provider_result
from backend.services.research.candidate_gates import body_candidate_limit, engine_query_for


class SearchLaneMixin:
    def education_platform_budget(self, run):
        """这个通道还剩几次请求；上一条老运行记录没有这一项时补上初值。

        旧记录（这一单之前开的 run）没有 ``education_platform`` 计数位。不补的
        话 ``run['limits'][key]`` 会 KeyError，而那是别人的研究记录，不该因为
        我们多了一个通道就报错。
        """
        run.setdefault('usage', {}).setdefault('education_platform', 0)
        run.setdefault('limits', {}).setdefault('education_platform', EDUCATION_PLATFORM_LIMIT)
        return max(0, run['limits']['education_platform'] - run['usage']['education_platform'])

    def note_education_platform_request(self, run):
        """记一次平台请求；额度不足时抛 ``QuotaReached``（安静收手，不是错误）。"""
        from backend.services.education_platform_sources import QuotaReached
        if self.education_platform_budget(run) <= 0:
            raise QuotaReached()
        self.consume(run, 'education_platform')

    def education_platform_transport(self):
        """这条通道的网络出口：**产品自己的公开网页读取器**。

        为什么不让通道自己开连接：产品的全部公开读取都经过
        ``PublicWebSearchProvider``，那里有 ``network_url``（公网地址校验）、重定向
        逐跳校验、16MB 正文上限，以及一个可供测试注入的 ``transport``。平台模块
        自己拿 ``urllib`` 发请求就等于**在测试的拦截边界之外**又开了一条网络通道：
        测试注入的离线读取器拦不住它，于是那些本该毫秒级完成的用例会真的去连
        ``gdegp.gds.edu.cn`` 并超时（实测 8 个用例因 fixture 的 10 秒超时而失败）。

        返回 ``None`` 表示"这一轮没有能发 POST 的读取器"（离线读取器、或配置缺失），
        此时通道不发任何请求——宁可这一轮不接这个来源，也不绕开产品自己联网。
        """
        for provider in getattr(self.search, 'providers', ()) or ():
            if hasattr(provider, 'fetch_form'):
                def transport(url, *, method, data, headers, timeout):
                    payload, _content_type = provider.fetch_form(
                        url, fields=data, method=method, extra_headers=headers,
                        timeout=timeout)
                    return payload
                return transport
        return None

    def education_platform_leads(self, run, target):
        """教育／科研交易平台：平台只当**来源**，抓回来的正文走既有的价格判据。

        和 ``aggregator_leads`` 的区别是本质的：聚合站（医价网、采招网）是转载，
        所以它们的页面永不进 ``run['sources']``；这两个平台是**采购人自己的成交
        结果公告发布方**，详情页就是一手原文，所以它按普通来源进
        ``run['sources']``，``source_type`` 由既有的 ``is_tender`` 判。

        价格一个字都没为它放宽：``prices()`` 读的是 ``run['sources']``，这里只是
        往里面多放了几条来源；过不过 ``validate_price`` 仍旧只看正文。

        额度用完、站点改版、被限流都只记一条缺口并返回——这一类渠道整个失败不
        该让整轮研究崩掉。返回这一轮新增的来源条数。
        """
        from backend.services import education_platform_sources as platforms
        budget = self.education_platform_budget(run)
        if budget <= 0:
            return 0
        transport = self.education_platform_transport()
        if transport is None:
            # 没有能发 POST 的公开读取器（离线/测试注入的那一个）。这一轮不接这条
            # 通道，而且要说清楚是"没读"而不是"没有数据"。
            run['education_platform_leads'] = []
            run['education_platform_sources'] = run.get('education_platform_sources') or 0
            run['gaps'].append('教育／科研交易平台本轮未接入：没有可用的公开网页读取器（不影响其它来源）。')
            return 0
        queries = {'model': target.get('model') or '',
                   'brand': target.get('brand') or '',
                   'category': target.get('category') or ''}
        try:
            leads = platforms.discover(queries, on_request=lambda: self.note_education_platform_request(run),
                                       limit=min(platforms.EDUCATION_PLATFORM_CANDIDATES, budget),
                                       transport=transport)
        except Exception as exc:
            note_failure(run, '教育平台检索', 'education_platform_discovery_failed', exc)
            run['gaps'].append('教育／科研交易平台检索未完成；保留已有来源待重试。')
            return 0
        added = 0
        new_sources = []
        with self.stage_lock:
            known = {source['url'] for source in run['sources']}
        for lead in leads:
            if self.education_platform_budget(run) <= 0:
                break
            if lead['url'] in known:
                continue
            try:
                source = self.fetch_source(run, {
                    'url': lead['url'], 'source_type': 'unknown', 'purpose': 'tender',
                    'platform': lead['platform'], 'platform_label': lead['platform_label'],
                    'min_interval': platforms.MIN_INTERVAL,
                })
            except ResearchStopped:
                run['unresolved_source_errors'] = True
                raise
            except Exception as exc:
                with self.stage_lock:
                    note_failure(run, '教育平台详情复读', 'education_platform_detail_failed', exc)
                    run['gaps'].append('教育／科研交易平台详情页未读到，保留线索可稍后重试。')
                continue
            known.add(lead['url'])
            new_sources.append(source)
            added += 1
        with self.stage_lock:
            existing = {s['url'] for s in run['sources']}
            for source in new_sources:
                if source['url'] not in existing:
                    run['sources'].append(source)
                    existing.add(source['url'])
            run['education_platform_leads'] = [dict(lead) for lead in leads]
            run['education_platform_sources'] = (run.get('education_platform_sources') or 0) + added
        self.persist(run)
        return added

    def province_award_budget(self, run):
        """这个通道还剩几次请求；上一条老运行记录没有这一项时补上初值。"""
        run.setdefault('usage', {}).setdefault('province_award', 0)
        run.setdefault('limits', {}).setdefault('province_award', PROVINCE_AWARD_LIMIT)
        return max(0, run['limits']['province_award'] - run['usage']['province_award'])

    def note_province_award_request(self, run):
        """记一次按型号查询；额度不足时抛 ``QuotaReached``（安静收手，不是错误）。"""
        from backend.services.education_platform_sources import QuotaReached
        if self.province_award_budget(run) <= 0:
            raise QuotaReached()
        self.consume(run, 'province_award')

    def province_award_transport(self):
        """这条通道的网络出口：**产品自己的公开网页读取器**（与教育平台通道同一个出口）。

        它比教育平台那条多两样东西，都是站点事实而不是新开的通道：

        * **JSON body**：省级政采平台的查询接口吃 ``application/json``，``fields`` 只会
          被 urlencode 成表单编码，所以走 ``fetch_form(body=…)``；
        * **TLS 姿态**：江西站只接受 legacy renegotiation，姿态写在平台配置上、
          由读取器执行（``search_providers.TLS_PROFILES``）。

        返回 ``None`` 表示"这一轮没有能发 POST 的读取器"（离线读取器、或配置缺失）。
        此时通道**一个请求都不发**——宁可这一轮不接这个来源，也不绕开产品自己联网。
        """
        for provider in getattr(self.search, 'providers', ()) or ():
            if hasattr(provider, 'fetch_form'):
                def transport(url, *, method, body, headers, tls_profile, timeout):
                    payload, _content_type = provider.fetch_form(
                        url, body=body, content_type=(headers or {}).get('Content-Type'),
                        tls_profile=tls_profile, method=method,
                        extra_headers=headers, timeout=timeout)
                    return payload
                return transport
        return None

    def province_award_sources(self, run, target):
        """按型号直查省级平台的成交公告，把命中并入 ``run['sources']``。

        四个省、两个家族（注册表与差异见 ``province_award_sources`` 的模块 docstring）：

        * **卖场家族**（安徽 / 江西）：**列表响应里直接带正文 HTML**（``content`` 字段），
          所以公告一到手就是 `fetched` 状态的完整来源，**不再发任何请求**、也不占共享
          ``fetch`` 额度。安徽详情页服务端只给 JS 空壳，回读详情反而读不到正文——
          正文随列表回来这一点就是这条通道的全部价值。
        * **政采云内核家族**（浙江 / 广西）：列表行只给一段 ~120 字符的**命中片段**
          （实测 5 个型号 16 条行，``price_table_rows`` 一行都读不出来），正文必须
          回读 ``GET /portal/detail?articleId=``，所以这个家族**会多发请求**，
          次数上限写在 ``province_award_sources.PORTAL_DETAIL_LIMIT``，
          正文来路在来源的 ``text_provenance.method`` 里与列表正文区分开。

        两件事在这一步落地：

        * **"查过了没有" 与 "根本没查成" 必须分开。** 命中 0 条是正常结果，
          **不写任何面向用户的说明文案、不报错、不算缺口**（产品设计规范：
          免责式文案会让产品显得不可靠）；而网络失败 / TLS 失败 / 被挡 / 解析报错 /
          详情页没读到**照旧写缺口 + ``note_failure``**——把"没查"说成"没有"才是真骗人，
          而且用户永远不知道自己该重试。两者的区分留在 ``run['province_award_queries']``。
        * **价格判据一个字没改。** 来源的 ``source_type`` 由既有的 ``is_tender`` 判，
          金额仍旧要过 ``price_table_rows`` / ``validate_price``。这里只是多放了几条
          一手来源进 ``run['sources']``。

        这个通道整个失败不该让整轮研究崩掉：任何异常都只记一条缺口并返回。
        返回这一轮新增的来源条数。
        """
        from backend.services import province_award_sources as provinces
        budget = self.province_award_budget(run)
        if budget <= 0:
            return 0
        if not isinstance(run.get('province_award_queries'), list):
            run['province_award_queries'] = []
        transport = self.province_award_transport()
        if transport is None:
            # 这一轮**没有能发 POST 的读取器**：一个请求都不发，也**不写面向用户的文案**。
            # 与"查询失败"分开记（区分留在 ``province_award_queries`` 里）：这不是
            # "这个型号在省本级也没有"，但也不是一次失败的查询——它是运行环境里没有出口
            # （离线测试、或部署缺读取器），此时研究链上别的通道会各自如实报自己的问题。
            run['province_award_queries'].append(
                {'status': 'not_attempted', 'reason': 'no_form_capable_reader'})
            return 0
        try:
            found = provinces.lookup(
                self.repo, target, transport=transport, task_id=run['task_id'],
                platforms=self.province_award_platforms(),
                on_request=lambda: self.note_province_award_request(run),
                parallel=True)
        except Exception as exc:
            with self.stage_lock:
                note_failure(run, '省级平台按型号直查', 'province_award_lookup_failed', exc)
                run['gaps'].append('省级平台按型号直查未完成；保留已有来源待重试。')
            return 0
        added = 0
        with self.stage_lock:
            for query in found['queries']:
                run['province_award_queries'].append({**query, 'status': 'queried'})
            for failure in found['failures']:
                platform_label = self.province_award_label(failure['platform'])
                note_failure(run, '省级平台按型号直查',
                             'province_award_' + str(failure['code']),
                             RuntimeError(f"{failure['platform']}: {failure['message']}"))
                run['gaps'].append(
                    f'{platform_label}按型号直查未完成（{failure["code"]}）；保留已有来源待重试。')
            known = {source['url'] for source in run['sources']}
            for announcement in provinces.source_records(found):
                if announcement['url'] in known:
                    continue
                run['sources'].append(self.province_award_record(run, announcement))
                known.add(announcement['url'])
                added += 1
            run['province_award_sources'] = (run.get('province_award_sources') or 0) + added
        self.persist(run)
        return added

    def province_award_platforms(self):
        """本轮要查哪些平台（测试注入用；默认是注册表全集：安徽 / 江西 / 浙江 / 广西）。

        **为什么读 ``ALL_PROVINCES`` 而不是 ``PROVINCES``**：``PROVINCES`` 只是卖场家族
        （安徽 / 江西）那一份注册表，政采云内核家族（浙江 / 广西）在
        ``PORTAL_PROVINCES`` 里。产品路径要的是**两族的并集**——只读前一份，新加的两省
        永远查不到，而且不会有任何报错。
        """
        from backend.services.province_award_sources import ALL_PROVINCES
        return getattr(self, 'province_platforms', None) or ALL_PROVINCES

    @staticmethod
    def province_award_label(platform_key):
        from backend.services.province_award_sources import _BY_KEY
        platform = _BY_KEY.get(platform_key)
        return platform.label if platform else str(platform_key)

    def province_award_record(self, run, announcement):
        """把一条查到的公告变成 ``run['sources']`` 里的一条来源（**不发请求**）。

        它和 ``fetch_source`` 的产出同形，只是正文来自平台自己的列表响应而不是一次
        正文读取：``body_status='fetched'``、``snapshot`` 按正文内容寻址落盘、
        ``repair_source_text`` 走同一条全角/坏字符修法。``source_type`` 由既有的
        ``is_tender`` 判——**判不出来就是 ``unknown``**，绝不会因为"它是成交公告"就
        直接写成 ``tender_document``。

        落盘前的号码掩码（``设计规范`` §4 第 6、7 行）：这条通道的正文**不进**
        ``fetch_source``（它是从平台列表响应的 ``content`` 字段自己拼出来的），所以
        共享落盘点覆盖不到它，必须在这里单独接一次。掩的是公告 ``content`` 原文——
        ``snapshot`` 与 ``body_text`` 都由它派生，先掩原文两处才同时干净；只掩正文
        会把真号留在按内容寻址的 ``.bin`` 快照里。
        """
        from backend.services.research_sources import public_url, snapshot_path
        from backend.services.education_platform_sources import mask_landed_text
        from backend.services.province_award_sources import body_text as province_body_text
        announcement = dict(announcement)
        if isinstance(announcement.get('content'), str):
            announcement['content'] = mask_landed_text(announcement['content'])
        source = {
            'url': announcement['url'],
            'source_type': 'unknown',
            'platform': announcement.get('platform'),
            'platform_label': announcement.get('platform_label'),
            'fetched_at': now(),
        }
        try:
            public_url(source['url'])
        except ValueError:
            source.update(body_status='unavailable', failure_kind='unsupported_source_url',
                          error_reason='来源地址不符合公开网页读取条件')
            return source
        content = announcement.get('content') or ''
        payload = content.encode('utf-8', 'replace')
        source['snapshot'] = {**snapshot_path(self.repo, payload),
                              'content_type': 'text/html; charset=utf-8'}
        source['body_text'] = province_body_text(announcement)
        self.repair_source_text(source)
        source['body_status'] = 'fetched'
        # 正文来路必须**逐条如实写**：卖场家族（安徽 / 江西）的正文随列表响应一起回来，
        # 政采云内核家族（浙江 / 广西）的列表行只是命中片段、正文是回读详情页拿到的。
        # 两者都在这条通道里，但"我们到底读了哪一份"不是同一件事，混成一句话就是
        # 把"读完一条公告"和"读了一段检索摘要"说成一样。
        if announcement.get('detail_fetched'):
            source['text_provenance'] = {
                'method': 'province_platform_detail_content',
                'note': '正文取自该平台详情接口（GET /portal/detail?articleId=）返回的正文，'
                        '与列表响应里的命中片段不是同一份内容。',
                'announcement': announcement.get('title') or '',
                'project_code': announcement.get('project_code') or '',
                'published_at': announcement.get('published_at') or '',
                'district': announcement.get('district') or '',
            }
        else:
            source['text_provenance'] = {
                'method': 'province_platform_list_content',
                'note': '正文取自该平台成交公告查询接口列表响应里的 content 字段（同一份公告的正文），未回读详情页。',
                'announcement': announcement.get('title') or '',
                'project_code': announcement.get('project_code') or '',
                'published_at': announcement.get('published_at') or '',
                'district': announcement.get('district') or '',
            }
        if announcement.get('cache_hit'):
            source['cache_hit'] = True
        source['source_type'] = ('tender_document' if is_tender(source)
                                 else 'unknown' if source.get('source_type') == 'tender_document'
                                 else source.get('source_type', 'unknown'))
        return source

    def note_candidate_window(self, run, text, purpose, recalled, admitted, origin, first_attempt):
        """把一次查询的候选窗口写进它自己的 ``search_attempts`` 记录。

        ``recalled`` 是这条查询去重之后（并已过 ``relevant_source`` 判据）的候选数，
        ``admitted`` 是真正交给正文抓取循环的条数——两者的差正是被
        ``body_candidate_limit(purpose)`` 截掉、过去既不抓也不记录的那批
        （设计规范、设计规范）。有了它，「这个窗口丢了多少」才能用 run 里的
        数字回答，而不是靠推断（设计规范 的对照实验就是这么读出来的）。

        窗口是**每查询一个决定**，所以字段写在本次 ``query`` 调用产生的行上
        （``first_attempt`` 之后、``query`` 文本相同的那些）。

        缓存命中时不消耗任何通道调用、``search_attempts`` 里本来一行都没有，这里为它
        补一行 ``status='search_cache_hit'``：**缓存路径同样会截断**，这件事必须也看得见。
        真实检索路径不会走这一支——``successful`` 为真就说明至少有一次通道调用成功，
        而那一行已经在循环里写下了。
        """
        window = {'candidates_recalled': recalled,
                  'candidates_in_body': admitted,
                  'candidates_dropped': max(recalled - admitted, 0),
                  'candidate_limit': body_candidate_limit(purpose),
                  'candidate_origin': origin}
        attempts = run.setdefault('search_attempts', [])
        rows = [row for row in attempts[first_attempt:] if row.get('query') == text]
        if not rows:
            if origin != 'search_cache':
                # 不该发生：宁可少一行，也不写一行假的来源。
                return
            rows = [{'query': text, 'provider': 'search_cache',
                     'status': 'search_cache_hit', 'results': recalled,
                     'relevant_results': recalled, 'procurement_leads': bool(recalled),
                     'official_results': 0, 'official_results_total': 0,
                     'provider_role': 'cache', 'fallback_reason': None, 'warnings': []}]
            attempts.extend(rows)
        for row in rows:
            row.update(window)

    def query(self, run, text, purpose, target=None):
        from backend.infrastructure.search_providers import (FATAL_CHANNEL_QUARANTINE_MARKER,
                                                             channel_quarantine_reason)
        from backend.services.research_planning import procurement_relevant, procurement_relevance_reason, discovery_relevant
        def relevant_source(source):
            if is_low_value_social_url(source.get('url', '')):
                return False
            return ((not target or procurement_relevant(source, target))
                    and (purpose != 'discovery' or discovery_relevant(source, run['baseline'])))
        if text in run['completed_queries']:
            for source in list(run['sources']):
                if not source.get('parent_url'):
                    self.fetch_attachments(run, source)
            return
        multi = bool(target) or purpose == 'discovery'
        # 本次调用新产生的 attempt 行从这里开始；候选窗口只写这些行，既往查询的
        # 记录（同文本重试时会存在）不被改写。
        attempt_first = len(run.get('search_attempts') or [])
        cache_scope = f'|procurement-search-v{SEARCH_STRATEGY_VERSION}'
        cache_id = 'search-cache-' + hashlib.sha256((text + '|' + purpose + cache_scope).encode()).hexdigest()
        with self.operation(run, 'search_cache_lookup', purpose=purpose, cache='current'):
            cached = self.repo.get('procurement_records', cache_id)
        # Keep completed v4 cache entries readable after the parser strategy
        # upgrade. Fresh searches use the new key and never overwrite history.
        if not cached:
            old_scope = '|procurement-multisource-v4' if multi else ''
            old_id = 'search-cache-' + hashlib.sha256((text + '|' + purpose + old_scope).encode()).hexdigest()
            with self.operation(run, 'search_cache_lookup', purpose=purpose, cache='legacy'):
                cached = self.repo.get('procurement_records', old_id)
            # Legacy empty results may actually be undetected captcha pages.
            if cached and not cached.get('sources'):
                cached = None
        if cached and (datetime.now(timezone.utc) - datetime.fromisoformat(cached['created_at'])).total_seconds() < 86400:
            known = {item['url'] for item in run['sources']}
            window = cached['sources'][:body_candidate_limit(purpose)]
            self.note_candidate_window(run, text, purpose, recalled=len(cached['sources']),
                                       admitted=len(window), origin='search_cache',
                                       first_attempt=attempt_first)
            # 立刻落库：下面的正文循环一旦顶到 fetch 额度就会抛 ResearchStopped，
            # 那时这条留痕必须是已经存下来的，否则「额度恰好被这次查询顶到」这件事
            # 会连带丢掉最该看的那条记录。
            self.persist(run)
            # Bodies that this window will have to read are read ahead of time,
            # in parallel. The candidate list is exactly the set the loop below
            # calls ``fetch_source`` for -- a source the search cache already
            # carries a usable body for is not in it, so no request is added.
            prefetched = self.prefetch_bodies(run, [
                {'url': item['url'], 'source_type': item.get('source_type', 'unknown'),
                 'purpose': purpose}
                for item in window
                if not is_low_value_social_url(item.get('url', ''))
                and not (target and not procurement_relevant(item, target))
                and self._search_cache_body_missing(cached.get('cache_version'), item)])
            for item in window:
                if is_low_value_social_url(item.get('url', '')):
                    run.setdefault('skipped_search_results', []).append({'query': text, 'url': item['url'], 'reason': '低质社交媒体域名，跳过抓取'})
                    continue
                if target and not procurement_relevant(item, target):
                    run.setdefault('skipped_search_results', []).append({'query': text, 'url': item['url'], 'reason': '与目标型号及采购主题不相关'})
                    continue
                source = copy.deepcopy(item)
                source['purpose'] = purpose
                if self._search_cache_body_missing(cached.get('cache_version'), source):
                    source = self._fetch_source(run, {'url': source['url'], 'source_type': source.get('source_type','unknown'), 'purpose': purpose},
                                                prefetched.get(source['url']))
                if source.get('source_type') == 'tender_document' and not is_tender(source):
                    source['source_type'] = 'unknown'
                if source.get('body_status') != 'fetched':
                    run['unresolved_source_errors'] = True
                if source['url'] not in known:
                    run['sources'].append(source)
                    known.add(source['url'])
                else:
                    previous = next(s for s in run['sources'] if s['url'] == source['url'])
                    if previous.get('purpose') == 'discovery':
                        source['purpose'] = 'discovery'
                    run['sources'] = [s for s in run['sources'] if s['url'] != source['url']] + [source]
                self.fetch_attachments(run, source)
            run['completed_queries'].append(text)
            self.persist(run)
            return
        # One budget reservation corresponds to one provider call, never an
        # unbounded provider fan-out hidden behind a single search counter.
        providers = [p for p in self.search.providers if p is not None]
        from types import SimpleNamespace
        collected, warnings, seen_urls = [], [], set()
        successful = False
        degraded = bool(run.get('unavailable_search_providers'))
        useful_collected = False
        official_results = 0
        official_urls = set()
        attempted = 0
        # The confirmed baseline travels with the query so an adapter that has
        # to drop the model and the brand before searching (CCGP indexes notice
        # titles, which name a category) can tell them apart from the category.
        baseline = run.get('baseline') or {}
        search_query = SearchQuery(text, purpose=purpose, limit=SEARCH_RESULT_LIMIT,
                                   brand=str(baseline.get('brand') or '').strip() or None,
                                   model=str(baseline.get('model') or '').strip() or None)
        failure_counts = run.setdefault('provider_failure_counts', {})
        challenge_counts = run.setdefault('provider_challenge_counts', {})
        cooldowns = run.setdefault('provider_cooldown_until', {})
        runnable = []
        for provider in providers:
            if failure_counts.get(provider.name, 0) >= QUARANTINE_AFTER_FAILURES:
                unavailable = run.setdefault('unavailable_search_providers', [])
                if provider.name not in unavailable:
                    unavailable.append(provider.name)
                run.setdefault('search_attempts', []).append({
                    'query': text, 'provider': provider.name, 'status': 'quarantined_skipped',
                    'results': 0, 'warnings': ['channel_quarantined_in_run'],
                })
                continue
            cooldown_until = float(cooldowns.get(provider.name) or 0)
            if cooldown_until and time.time() < cooldown_until:
                run.setdefault('search_attempts', []).append({
                    'query': text, 'provider': provider.name, 'status': 'cooldown_skipped',
                    'results': 0, 'cooldown_remaining_s': round(cooldown_until - time.time(), 1),
                    'warnings': ['challenge_cooldown_active'],
                })
                continue
            # Health-check reconfiguration can intentionally leave a
            # fetch-only adapter in the shared chain.  It must not consume a
            # discovery call or turn "no provider ran" into a successful
            # empty search.
            if hasattr(provider, 'supports') and not provider.supports(search_query):
                quarantined = channel_quarantine_reason(provider.name)
                if quarantined:
                    unavailable = run.setdefault('unavailable_search_providers', [])
                    if provider.name not in unavailable:
                        unavailable.append(provider.name)
                    run.setdefault('search_attempts', []).append({
                        'query': text, 'provider': provider.name, 'status': 'channel_quarantined',
                        'results': 0, 'warnings': [f'{FATAL_CHANNEL_QUARANTINE_MARKER}: {quarantined}'],
                        'quarantine_reason': quarantined})
                    self.persist(run)
                continue
            if provider.name == 'deepseek_web' and run['usage']['model'] >= run['limits']['model']:
                run.setdefault('search_attempts', []).append({'query': text, 'provider': provider.name, 'status': 'budget_exhausted'})
                continue
            if run['usage']['search'] >= run['limits']['search']:
                break
            self.consume(run, 'search')
            attempted += 1
            runnable.append(provider)

        lane_claim = getattr(self.execution, 'claim', None) or self.round_claim
        lane_name = getattr(self.spending_lane, 'name', None)

        def _execute_provider(provider):
            self.execution.claim = lane_claim
            if lane_name:
                self.spending_lane.name = lane_name
            adapter = (SimpleNamespace(name=provider.name, supports=provider.supports, search=provider.search_once)
                       if hasattr(provider, 'search_once') else provider)
            if getattr(provider, 'resolves_result_links', False):
                adapter.search = lambda query, p=provider: p.search_once(query, on_resolve=lambda: self.consume(run, 'fetch'))
            service = OfficialSearchService(providers=[adapter], fetch_body=False, timeout=self.search.timeout)
            from backend.infrastructure.call_budget import research_reservation
            token = research_reservation.set(lambda: self.consume(run, 'model'))
            search_started, search_clock = now(), time.perf_counter()
            try:
                response = service.run(search_query)
                return provider, response, search_started, search_clock, None
            except Exception as exc:
                return provider, None, search_started, search_clock, exc
            finally:
                research_reservation.reset(token)

        if len(runnable) > 1:
            with ThreadPoolExecutor(max_workers=min(len(runnable), 5), thread_name_prefix='search-provider') as pool:
                provider_outcomes = list(pool.map(_execute_provider, runnable))
        else:
            provider_outcomes = [_execute_provider(p) for p in runnable]

        for provider, response, search_started, search_clock, uncaught in provider_outcomes:
            if uncaught is not None:
                self.record_operation(
                    run, 'search_provider', search_started, search_clock, 'failed',
                    purpose=purpose, provider=provider.name, cache='miss', attempts=1,
                    result_urls=0, new_result_urls=0, repeated_result_urls=0,
                    error_code=str(getattr(uncaught, 'code', '') or type(uncaught).__name__))
                raise uncaught
            provider_name = getattr(provider, 'name', '')
            failed = any(w.startswith('provider_failed:') for w in response.warnings)
            self.record_operation(
                run, 'search_provider', search_started, search_clock,
                'failed' if failed else 'completed', purpose=purpose,
                provider=provider_name, cache='miss', attempts=1,
                result_urls=len(response.sources),
                new_result_urls=sum(source.url not in seen_urls for source in response.sources),
                repeated_result_urls=sum(source.url in seen_urls for source in response.sources),
                error_code='provider_failed' if failed else None,
            )
            successful = successful or not failed
            challenge_hit = False
            if failed:
                degraded = True
                if any(FATAL_CHANNEL_QUARANTINE_MARKER in warning for warning in response.warnings):
                    failure_counts[provider_name] = QUARANTINE_AFTER_FAILURES
                elif any(CHALLENGE_QUARANTINE_MARKER in warning for warning in response.warnings):
                    challenge_hit = True
                    challenge_counts[provider_name] = challenge_counts.get(provider_name, 0) + 1
                    failure_counts[provider_name] = failure_counts.get(provider_name, 0) + 1
                    if challenge_counts[provider_name] >= CHALLENGE_QUARANTINE_THRESHOLD:
                        failure_counts[provider_name] = QUARANTINE_AFTER_FAILURES
                        cooldowns.pop(provider_name, None)
                else:
                    failure_counts[provider_name] = failure_counts.get(provider_name, 0) + 1
                if failure_counts[provider_name] >= QUARANTINE_AFTER_FAILURES:
                    unavailable = run.setdefault('unavailable_search_providers', [])
                    if provider_name not in unavailable:
                        unavailable.append(provider_name)
                elif challenge_hit:
                    cooldowns[provider_name] = time.time() + CHALLENGE_COOLDOWN_SECONDS
            else:
                failure_counts[provider_name] = 0
                challenge_counts.pop(provider_name, None)
                cooldowns.pop(provider_name, None)
                if provider_name in run.get('unavailable_search_providers', []):
                    run['unavailable_search_providers'].remove(provider_name)
            relevant = [s for s in response.sources if relevant_source(source_to_dict(s))]
            official_in_response = 0
            for item in relevant:
                data = source_to_dict(item)
                host = (urlsplit(data.get('url', '')).hostname or '').casefold()
                source_type = data.get('source_type')
                procurement_title = re.search(r'中标|成交|采购|招标|tender|procurement|award',
                                               f"{data.get('title', '')} {data.get('snippet', '')}", re.I)
                if (source_type in {'tender_document', 'manufacturer_website', 'manufacturer_download'} or (
                        (is_local_authority_host(host) or host.endswith('.gov')) and procurement_title)):
                    if data.get('url') not in official_urls:
                        official_urls.add(data.get('url'))
                        official_in_response += 1
            official_results += official_in_response
            useful = any(re.search(r'中标|成交|采购|招标|tender|procurement|award', s.title + ' ' + s.snippet, re.I) for s in relevant) if target and purpose != 'discovery' else bool(relevant)
            useful_collected = useful_collected or useful
            attempt_warnings = list(response.warnings)
            if challenge_hit and failure_counts.get(provider_name, 0) < QUARANTINE_AFTER_FAILURES:
                attempt_warnings.append('challenge_cooldown')
            run.setdefault('search_attempts', []).append({'query': text, 'provider': provider_name,
                'engine_query': engine_query_for(provider, search_query),
                'status': 'failed' if failed else 'ok', 'results': len(response.sources), 'relevant_results': len(relevant),
                'procurement_leads': useful, 'official_results': official_in_response,
                'official_results_total': official_results,
                'provider_role': 'parallel_discovery',
                'warnings': attempt_warnings})
            empty_gap = note_provider_result(
                run, provider_name, status='failed' if failed else 'ok',
                results=len(response.sources))
            if empty_gap:
                run['search_attempts'][-1].setdefault('warnings', []).append('provider_returned_no_results')
                warnings.append(empty_gap)
            warnings.extend(w for w in response.warnings if not w.startswith('provider_failed:'))
            for source in relevant:
                if source.url not in seen_urls:
                    collected.append(source)
                    seen_urls.add(source.url)
            for source in response.sources:
                if source not in relevant:
                    skip_reason = '低质社交媒体域名，跳过抓取' if is_low_value_social_url(source.url) else '与目标型号及采购主题不相关'
                    run.setdefault('skipped_search_results', []).append({'query': text, 'url': source.url, 'reason': skip_reason})
            if getattr(provider, 'client', None) and getattr(provider.client, 'last_usage', None):
                run['search_attempts'][-1]['model_usage'] = copy.deepcopy(provider.client.last_usage)
        if provider_outcomes:
            self.persist(run)
        if not attempted:
            if run['usage']['search'] >= run['limits']['search']:
                raise ResearchStopped('本轮搜索额度已用完，保留结果待继续。')
        if target:
            collected.sort(key=lambda s: (not bool(re.search(r'中标|成交|采购|招标|tender|procurement|award', s.title + ' ' + s.snippet, re.I)), not contains_model(s.title + ' ' + s.snippet, target['model'])))
        result = SimpleNamespace(sources=collected[:body_candidate_limit(purpose)], warnings=warnings)
        if not successful:
            run['unresolved_source_errors'] = True
            self.persist(run)
            raise ValueError('搜索接口失败，请先恢复服务')
        # 候选窗口留痕：这条查询召回了多少、进正文多少、丢了多少。写在成功之后，
        # 所以「全部通道失败」的那种查询不会多出误导性的空窗口记录。
        self.note_candidate_window(run, text, purpose, recalled=len(collected),
                                   admitted=len(result.sources), origin='provider',
                                   first_attempt=attempt_first)
        # 立刻落库：下面的正文循环一旦顶到 fetch 额度就会抛 ResearchStopped，
        # 那时这条留痕必须是已经存下来的（见缓存路径同处注释）。
        self.persist(run)
        run['gaps'] = extend_gaps_once(run, result.warnings)
        known = {s['url'] for s in run['sources'] if s.get('body_status') == 'fetched'}
        query_sources = []
        # The sources this loop will fetch are known before it runs: a url the
        # run already holds a body for is reused, and a result the discovery
        # gate rejects never gets read. Those bodies are therefore read ahead
        # of time in parallel; the loop below still fetches them one at a time,
        # in order, and finds each read already done.
        candidates = []
        for src in result.sources:
            if src.url in known:
                continue
            data = source_to_dict(src)
            if target and not procurement_relevant(data, target):
                continue
            candidates.append(data)
        prefetched = self.prefetch_bodies(run, candidates)
        for src in result.sources:
            if target and not procurement_relevant(source_to_dict(src), target):
                run.setdefault('skipped_search_results', []).append({'query': text, 'url': src.url,
                    'reason': '与目标型号及采购主题不相关', 'relevance_signal': None})
                continue
            if src.url in known:
                existing = next(s for s in run['sources'] if s['url'] == src.url)
                query_sources.append(existing)
                self.fetch_attachments(run, existing)
                continue
            if run['usage']['fetch'] >= run['limits']['fetch']:
                run['gaps'].append('正文读取额度已用完，保留已取得的来源继续分析')
            source = source_to_dict(src)
            source = self._fetch_source(run, source, prefetched.get(source['url']))
            source['purpose'] = purpose
            if target:
                relevance_signal = procurement_relevance_reason(source, target)
                if not relevance_signal:
                    run.setdefault('skipped_search_results', []).append({
                        'query': text, 'url': source['url'], 'reason': '正文仍未出现目标型号、品类或实验仪器采购语境',
                        'relevance_signal': None,
                    })
                    continue
                source['relevance_signal'] = relevance_signal
            host = (urlsplit(source['url']).hostname or '').lower()
            if is_tender(source):
                source['source_type'] = 'tender_document'
            # A title containing “中标” is not proof of an official publisher.
            if source['source_type'] == 'tender_document' and not is_tender(source):
                source['source_type'] = 'unknown'
                source['authority_gap'] = '采购发布方身份尚未核实'
            run['sources'] = [s for s in run['sources'] if s['url'] != source['url']] + [source]
            query_sources.append(source)
            self.persist(run)
            self.fetch_attachments(run, source)
        retryable = degraded and not useful_collected
        pending = run.setdefault('retryable_queries', [])
        if retryable:
            if text not in pending:
                pending.append(text)
            run['gaps'].append('部分搜索来源访问受阻，当前查询尚无有效线索；可稍后继续补查。')
        elif text in pending:
            pending.remove(text)
        if successful and not retryable:
            self.save_cache(run, {'id': cache_id, 'task_id': run['task_id'], 'kind': 'search_cache', 'created_at': now(), 'sources': query_sources, 'cache_version': SEARCH_STRATEGY_VERSION})
        if retryable or any(s.get('body_status') not in {'fetched', 'unavailable'} for s in query_sources):
            run['unresolved_source_errors'] = True
        else:
            run['completed_queries'].append(text)
        self.persist(run)
