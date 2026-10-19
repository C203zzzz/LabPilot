"""相似品候选的查价留痕（`docs/148` 的方案 C）：把「没有价」分成三句说得清的话。

`docs/148` 侦察出来的事实是：相似品与精确型号**共用同一条抽价链**——同一个
``prices()``、同一套三个入口判据、同一个 ``validate_price``、同一套按型号分键的
拒绝留痕；不一样的只是「谁去把公告抓进来」（教育／科研平台按型号查、安徽／江西省
本级按型号直查、迟到正文补扫只有精确型号那条有）。所以这里**不改任何通道、不改
任何判据**，只回答一个记账问题：一个相似品候选没有价，是

* ① **没查**（这一轮根本没给它找来源，或它这一轮压根没跑抽价）；
* ② **查了没有**（来源有了，正文里没有它的成交单价）；
* ③ **有来源但没被采信**（抽到了候选行，被单价核验或候选排名判据挡下）。

三种情况在库里的形状本来就不一样：③ 有 ``run['price_rejections']``（按
``model`` 分键）；② 有 ``run['price_source_skips']``（按 ``target_model`` 分键）
或一次"读了但一行都没抽到"的 pass；① 过去**一个字都没有**——这正是本模块补的那
一环。字段取值口径见 :func:`price_lookup` 的 docstring。

本模块全部是只读推导：它读的是既有的留痕（``price_source_skips`` /
``price_rejections``）与来源自身的通道身份，不重算任何一个判据，也不改变一次
采信决定。通道身份来自两个注册表本身，不是按域名猜的：
``education_platform_sources.PLATFORMS``（``gdegp`` / ``xlyg``）与
``province_award_sources.ALL_PROVINCES``（``anhui`` / ``jiangxi`` / ``zhejiang`` /
``guangxi``：卖场家族 + 政采云内核家族的并集）。
"""
from __future__ import annotations

from functools import lru_cache

from backend.services.price_rejections import (
    SKIP_BODY_NOT_FETCHED,
    SKIP_MODEL_NOT_IN_BODY,
    SKIP_NO_AMOUNT_EVIDENCE,
    SKIP_REASONS,
    SKIP_SOURCE_LIMIT,
    SKIP_TYPE_NOT_PRICE_SOURCE,
    rejection_summary,
)

#: 来源通道（只区分相似品能"沾光"读到的那两条直查通道与其余来源）。
EDUCATION_PLATFORM = 'education_platform'
PROVINCE_AWARD = 'province_award'
OTHER_CHANNEL = 'other'
CHANNELS = (EDUCATION_PLATFORM, PROVINCE_AWARD, OTHER_CHANNEL)

#: ``procurement_research.province_award_record`` 写下的正文来路标记；来源记录上
#: 的 ``platform`` 键缺失时用它兜底认出省级直查通道。
PROVINCE_TEXT_METHOD = 'province_platform_list_content'

#: 一个候选这一轮查价的结论。①②③ 的对应关系见模块开头。
PRICED = 'priced'                          # 抽到并采信了至少一条单价
REFUSED = 'refused'                        # ③ 抽到了候选行，被采信判据挡下
READ_WITHOUT_PRICE = 'read_without_price'  # ② 读了来源，正文里没有它的成交单价
SOURCES_NOT_READ = 'sources_not_read'      # ② 有来源写着型号，但被成本门挡在门外
NO_PRICE_SOURCE = 'no_price_source'        # ② 入口三判据一条都没过：价格通道里没有来源
NOT_ATTEMPTED = 'not_attempted'            # ① 这一轮没查
#: ① 的一个特例：这一轮**按设计**没查——两段式"先展示候选、按需查价"打开时，
#: ``alternatives()`` 不再对候选批量查价，价格等用户点了某一个候选的按钮才去查
#: （``docs/HANDOVER-alt-two-stage.md`` 第二、四节）。它必须与 ``not_attempted``
#: 分开：那一档说的是"本该查却没走到"，这一档说的是"用户还没点"。开关默认**关**，
#: 所以这条在当前默认行为下一行都不会出现。
NOT_REQUESTED = 'not_requested'            # ① 等用户点哪个，这一轮按设计没查
UNRECORDED = 'unrecorded'                  # 抽价被替身接管，没留下扫描记录（只在测试里可能）
STATUSES = (PRICED, REFUSED, READ_WITHOUT_PRICE, SOURCES_NOT_READ,
            NO_PRICE_SOURCE, NOT_ATTEMPTED, NOT_REQUESTED, UNRECORDED)

#: ① 没查的原因码。前五个是 ``alternatives()`` 里每一种"没走到抽价那一步"，
#: 第六个是"抽价跑了，但这一轮一条来源都没有"，最后一个是按需查价下"用户还没点"。
RETAINED_FROM_PREVIOUS_ROUND = 'retained_from_previous_round'
LEAD_BELOW_REQUIREMENT = 'lead_below_requirement'
DUPLICATE_MODEL = 'duplicate_model'
SOURCE_NOT_USABLE = 'source_not_usable'
VERIFICATION_LIMIT = 'verification_limit'
NO_SOURCES_FOUND = 'no_sources_found'
SCAN_NOT_RECORDED = 'scan_not_recorded'
#: 又一种留痕，但**不是**"没查"的一种（所以**不进** ``NOT_ATTEMPTED_REASONS``）：候选过了
#: 入口闸门、拿到了核验名额，可这一轮走完却没写出结论（线索低于要求、沿用上一轮、名额
#: 用完，或这一步在本候选上失败被 ``candidate_scope`` 就地跳过）。它同时是**参考候选的
#: 落点原因**：``alternatives()`` 末尾用 ``place_slotted_candidates`` 把这些候选补进
#: ``reference_candidates``，保证"进过名额的候选不会两头都没有"这条不变量。
NO_CONCLUSION = 'no_conclusion'
NOT_ATTEMPTED_REASONS = (RETAINED_FROM_PREVIOUS_ROUND, LEAD_BELOW_REQUIREMENT,
                         DUPLICATE_MODEL, SOURCE_NOT_USABLE, VERIFICATION_LIMIT,
                         NO_SOURCES_FOUND, SCAN_NOT_RECORDED, NOT_REQUESTED)

#: ``prices()`` 的两个阶段与它们的留痕原因码：入口三判据（``price_source_candidates``
#: 判的）与循环里的两个成本门。名字从 ``price_rejections`` 取，不在这里另写一份。
ENTRY_SKIP_REASONS = (SKIP_BODY_NOT_FETCHED, SKIP_TYPE_NOT_PRICE_SOURCE,
                      SKIP_MODEL_NOT_IN_BODY)
COST_SKIP_REASONS = (SKIP_NO_AMOUNT_EVIDENCE, SKIP_SOURCE_LIMIT)


@lru_cache(maxsize=1)
def _registry_keys() -> tuple:
    """两个注册表的键（``gdegp``/``xlyg``、省级通道的 ``ALL_PROVINCES``）。

    **省级这一份读的是全集**（安徽 / 江西 / 浙江 / 广西）：读 ``PROVINCES`` 只会认到
    卖场家族那两省，浙江 / 广西的来源会被判成 ``other``——那是"认不出通道"，
    会让按通道分组的统计把新增两省悄悄记到别的桶里。

    第一次用到时读，之后缓存——注册表是常量元组，而 ``source_channel`` 会被每条
    来源、每个 pass 各调一次。注册表按需 import，模块本身不带任何网络依赖。
    """
    from backend.services.education_platform_sources import PLATFORMS
    from backend.services.province_award_sources import ALL_PROVINCES
    return (frozenset(item.key for item in PLATFORMS),
            frozenset(item.key for item in ALL_PROVINCES))


def source_channel(source) -> str:
    """这条来源是哪条通道放进 ``run['sources']`` 的。

    认身份不认域名拼写：先看来源自己带的 ``platform`` 键在哪个注册表里，再看
    URL 是不是注册过的教育／科研平台（``platform_for`` 按 ``(host, port)`` 比），
    最后看省级通道写下的正文来路标记。认不出来就是 ``other``（绝大多数来源：
    检索句抓回来的公告、附件、厂家资料页）。
    """
    source = source if isinstance(source, dict) else {}
    platform_key = str(source.get('platform') or '')
    education_keys, province_keys = _registry_keys()
    if platform_key and platform_key in education_keys:
        return EDUCATION_PLATFORM
    if platform_key and platform_key in province_keys:
        return PROVINCE_AWARD
    url = str(source.get('url') or '')
    if url:
        from backend.services.education_platform_sources import platform_for
        if platform_for(url) is not None:
            return EDUCATION_PLATFORM
    if (source.get('text_provenance') or {}).get('method') == PROVINCE_TEXT_METHOD:
        return PROVINCE_AWARD
    return OTHER_CHANNEL


def channel_counts(sources) -> dict:
    """这一批来源按通道分组计数；三个键永远在，读的人不用判空。"""
    counts = {channel: 0 for channel in CHANNELS}
    for source in sources or []:
        if not isinstance(source, dict):
            continue
        counts[source_channel(source)] += 1
    return counts


def scan_record(sources) -> dict:
    """一次候选抽价 pass 看到的来源账（只读、不花额度、不发请求）。

    ``sources_scanned`` 是这次 pass 可读的 ``run['sources']`` 条数——**不是**网络
    请求数，也不是"有几条真的被读了"（那个由 ``price_source_skips`` 与本模块推出的
    ``sources_eligible`` 回答）。
    """
    rows = [s for s in (sources or []) if isinstance(s, dict)]
    return {'sources_scanned': len(rows), 'sources_by_channel': channel_counts(rows)}


def not_attempted(reason_code: str, prices=None) -> dict:
    """① 没查的结论。没有扫描记录可写，所以只有状态、原因与已有的价格条数。

    这条记录存在的理由：``run['price_source_skips']`` 只在"查了但被挡"时才有行，
    一个这一轮压根没跑的候选过去连一行都没有，于是"没查"和"查了没有"在库里长得
    一样。``reason_code`` 说清是哪一种没查（见 ``NOT_ATTEMPTED_REASONS``）。
    """
    return {'status': NOT_ATTEMPTED, 'reason_code': reason_code,
            'price_count': len(prices or [])}


def _same_model(left, right) -> bool:
    """留痕的键与被查的型号逐字比：``prices()`` 写 ``target_model`` 用的是原字符串。

    故意不用 ``identity()``——它按既有语义把分隔符去掉，``Q20`` 与 ``Q-20`` 会合并
    成同一个键，而 ``prices()`` 的留痕键并没有合并。
    """
    return str(left or '') == str(right or '')


def _distinct_sources(skips, reasons) -> int:
    """这几道门各挡下了**几条来源**（同一个 URL 被同一道门挡多次只算一条）。

    ``prices()`` 一个候选要跑很多次（先一次、每条采购方检索句之后各一次），成本门
    那两条留痕每次都往 ``run['price_source_skips']`` 追加一行、run 级不去重（入口那
    三条会被下一次 pass 覆盖，成本门这两条不会）。所以按记录数会虚报——卡片上要说的
    是"几条来源没读到"，不是"几次留痕"。
    """
    return len({record.get('url') for record in skips
                if record.get('skip_reason') in reasons})


def skip_summary(skips) -> list:
    """按来源去重的逐原因计数，顺序沿用 ``price_rejections.SKIP_REASONS``。"""
    counts = {}
    for record in skips:
        reason = record.get('skip_reason')
        counts.setdefault(reason, set()).add(record.get('url'))
    return [{'skip_reason': reason, 'count': len(counts[reason])}
            for reason in SKIP_REASONS if reason in counts]


def price_lookup(run, model, prices=None, scan=None, not_attempted_reason=None) -> dict:
    """一个相似品候选这一轮查价的结论。只读留痕，不重算判据。

    口径（每个数都写清分子分母）：

    * ``sources_scanned`` —— 这个候选**最后一次**抽价 pass 看到的 ``run['sources']``
      条数（``alternative_prices`` 每调用一次 ``prices()`` 前记一次，取最后一次）。
    * ``sources_entry_dropped`` —— 上面那批来源里，被入口三判据挡下的**条数**（按
      ``target_model`` 从 ``run['price_source_skips']`` 数出来的入口记录，同一条
      来源只算一次）。
    * ``sources_eligible`` —— ``sources_scanned - sources_entry_dropped``：过了三判据、
      真正进了抽价循环的条数。这是减法，不是把判据再跑一遍（入口只可能两种结局：
      入选、或留一条记录）。
    * ``sources_cost_gated`` —— 合格但被成本门挡下的**条数**（正文没有金额证据／超出
      ``PRICE_SOURCE_LIMIT`` 条来源每目标）。
    * ``skip_reasons`` —— 本 target 的入口／成本门留痕按原因汇总（按来源去重）；
      ``rejection_reasons`` —— 被拒的行按原因汇总（``price_rejections.rejection_summary``
      的形状，那里的记录本来就是每个 pass 覆盖式的，不会跨 pass 重复）。

    状态判定顺序（先到的先算，所以"有价"不会被"被拒的行"盖掉）：

    1. 有采信的价格 → ``priced``；
    2. 有被拒的行 → ``refused``（③）；
    3. 一条来源都没看到（``sources_scanned == 0``）→ ``not_attempted`` +
       ``no_sources_found``（①：这一轮根本没给它找到来源）；
    4. 没有一条来源过得了入口三判据（正文没抓到／类型不是价格来源／正文里没有这个
       型号）→ ``no_price_source``（②）；是哪一条判据把它们全挡下的，看
       ``skip_reasons``；
    5. 合格的来源全被成本门挡下（``sources_cost_gated >= sources_eligible``）→
       ``sources_not_read``（②，可重试）；
    6. 其余（有条目真的被读了，就是没抽出价）→ ``read_without_price``（②）。
    """
    prices = list(prices or [])
    if not_attempted_reason:
        return not_attempted(not_attempted_reason, prices)
    if scan is None:
        return {'status': UNRECORDED, 'reason_code': SCAN_NOT_RECORDED,
                'price_count': len(prices)}
    skips = [record for record in (run.get('price_source_skips') or [])
             if _same_model(record.get('target_model'), model)]
    rejections = [record for record in (run.get('price_rejections') or [])
                  if _same_model(record.get('model'), model)]
    scanned = int(scan.get('sources_scanned') or 0)
    entry_dropped = _distinct_sources(skips, ENTRY_SKIP_REASONS)
    cost_gated = _distinct_sources(skips, COST_SKIP_REASONS)
    eligible = max(0, scanned - entry_dropped)
    if prices:
        status = PRICED
    elif rejections:
        status = REFUSED
    elif scanned == 0:
        return not_attempted(NO_SOURCES_FOUND, prices)
    elif eligible == 0:
        status = NO_PRICE_SOURCE
    elif cost_gated >= eligible:
        status = SOURCES_NOT_READ
    else:
        status = READ_WITHOUT_PRICE
    return {
        'status': status,
        'reason_code': None,
        'sources_scanned': scanned,
        'sources_entry_dropped': entry_dropped,
        'sources_eligible': eligible,
        'sources_cost_gated': cost_gated,
        'sources_by_channel': dict(scan.get('sources_by_channel') or channel_counts([])),
        'skip_reasons': skip_summary(skips),
        'rejection_reasons': rejection_summary(rejections),
        'price_count': len(prices),
    }


def records_for(run, key, field, model) -> list:
    """把这个 target 自己的逐条留痕从 run 级留痕里筛出来。

    ``price_source_skips`` 按 ``target_model`` 分键、``price_rejections`` 按 ``model``
    分键，都是 ``prices()`` 写的（见它末尾那两段注释）；这里只筛不改，顺序就是
    ``prices()`` 写下的顺序。
    """
    return [record for record in (run.get(key) or [])
            if isinstance(record, dict) and _same_model(record.get(field), model)]


def candidate_lookup(run, candidate) -> dict:
    """把一个候选这一轮的查价结论与逐条留痕写到候选自己身上，返回那条结论。

    在 ``alternatives()`` 里、``alternative_prices()`` 返回之后调用：那时
    ``candidate['price_scan']``（``alternative_prices`` 记的扫描账）与 ``prices()``
    写下的 run 级留痕都已到位。``price_scan`` 是中间账，落进 ``price_lookup`` 之后
    就删掉，卡片上只留一个字段说这件事。
    """
    model = str(candidate.get('model') or '')
    scan = candidate.pop('price_scan', None)
    candidate['price_source_skips'] = records_for(run, 'price_source_skips',
                                                  'target_model', model)
    candidate['price_rejections'] = records_for(run, 'price_rejections', 'model', model)
    candidate['price_lookup'] = price_lookup(run, model, candidate.get('prices'), scan)
    return candidate['price_lookup']


def alternative_lookup(run) -> dict:
    """相似品车道这一轮的查价总账，写到 ``run['alternative_price_lookup']``。

    ``sources_scanned`` 的口径：**相似品车道抽价收尾那一刻** ``run['sources']`` 的
    条数。它是这一轮所有候选 pass 扫描范围的并集（来源只增不减，最后一个候选的
    最后一次 pass 看得到此前全部来源），但**不含**收尾段之后才抓回来的来源
    （``recover_sources`` 与第二次附件补抓在 ``_execute`` 里跑在它后面，那些来源
    对相似品候选的抽价是"不存在"的——这正是 ``docs/148`` 里那个顺序细节）。

    ``candidates`` 每行是一个候选的结论摘要，``candidates_with_price`` 就是
    ``docs/148`` 要的"命中了几条候选"。
    """
    sources = [source for source in (run.get('sources') or []) if isinstance(source, dict)]
    candidates = []
    for candidate in run.get('alternatives') or []:
        lookup = candidate.get('price_lookup') or {}
        candidates.append({'model': candidate.get('model'),
                           'status': lookup.get('status'),
                           'reason_code': lookup.get('reason_code'),
                           'price_count': len(candidate.get('prices') or [])})
    return {
        'sources_scanned': len(sources),
        'sources_by_channel': channel_counts(sources),
        'candidates': candidates,
        'candidates_scanned': sum(1 for row in candidates
                                  if row['status'] not in (None, NOT_ATTEMPTED, NOT_REQUESTED)),
        'candidates_with_price': sum(1 for row in candidates if row['price_count']),
    }
