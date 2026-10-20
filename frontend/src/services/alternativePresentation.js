/**
 * 「跨品牌比较（相似品分析）」候选卡片的呈现逻辑。
 *
 * 单独放在 service 里，是为了让「这张卡片最后给出什么结论、把哪些参数算作
 * 满足 / 不满足 / 待核对 / 缺证」可以被 node --test 直接断言，不必起浏览器。
 * 组件只负责排版。
 *
 * 事实边界（本模块唯一的铁律）：
 * 结论只有「推荐比较 / 可比较候选 / 已核对的指标满足 / 关键指标不满足 /
 * 参数尚未核全 / 同品牌参考」这几种，**任何时候都不输出「可替代」**。
 * 未知、冲突、临时来源三类证据一律不计入满足，并且各自单独成组展示，
 * 不与「已核实」的结论混在一段文字里。`substitutable` 恒为 false，保留该字段
 * 是为了让这条产品规则能被测试直接断言。
 *
 * 分组的判据与后端 similarity_assessment / parameter_row 一致（backend 不改）：
 *   - 已核实不满足：status=violated 且 evidence_status≠provisional 且无 conflict
 *   - 临时可能不满足：status=violated 且 evidence_status=provisional
 *   - 已核验满足：status=satisfied 且 evidence_status≠provisional 且无 conflict
 *   - 其余（unknown、冲突、临时证据）都算证据不足，按「有没有原文可核对」再分两类
 * 一套口径互斥且完整，所以同一件事不会在两处重复出现。
 */

export const SIMILARITY_BASIS = '相似度按已核验的可比关键参数计算，阈值为 70%；未知、冲突和临时来源不计为满足。'
export const COMPARABLE_BOUNDARY = '相似候选不等于完全替代；采购前仍需确认差异、缺证、实际用途、交付和售后。'
export const INELIGIBLE_BOUNDARY = '已核实的指标存在不满足，不能作为等效替代。'
export const UNRESOLVED_BOUNDARY = '当前不能判断可替代。'
export const PROVISIONAL_VIOLATION_NOTE = '来源身份确认后再作正式判断。'
export const EMPTY_CONCLUSION = '需核对用途、配置和全部关键指标。'
export const PROVISIONAL_NOTE_PREFIX = '已从网页正文提取'

/** 组顺序即页面顺序：先给负向事实，再给已核实满足，最后是证据不足。 */
export const PARAMETER_GROUP_DEFS = [
  { key: 'violated', label: '已核实不满足的指标', tone: 'danger', note: '' },
  { key: 'provisionalViolated', label: '临时正文证据显示可能不满足', tone: 'warning', note: PROVISIONAL_VIOLATION_NOTE },
  { key: 'satisfied', label: '已核验满足', tone: 'success', note: '' },
  { key: 'pendingReview', label: '已有相关原文待核对', tone: 'neutral', note: '' },
  { key: 'provisional', label: '临时正文证据（来源待核实）', tone: 'warning', note: '' },
  { key: 'missing', label: '缺少证据', tone: 'muted', note: '' },
]

/** matrix 缺失时（早期数据）回落到后端已经算好的三类清单。 */
const FALLBACK_LISTS = { violated: 'difference_parameters', satisfied: 'satisfied_parameters', missing: 'missing_parameters' }
const COMPARABLE_DISPOSITIONS = ['comparable_candidate', 'recommended_comparison']
const NEGATIVE_KEYS = ['violated', 'provisionalViolated', 'pendingReview', 'provisional', 'missing']

export function matrixRows(candidate) {
  return Array.isArray(candidate?.matrix) ? candidate.matrix.filter(row => row && typeof row === 'object') : []
}

/**
 * 已核验的可比关键参数比例。
 *
 * 注意 `similarity_score: null`（后端算不出比例时给的值）不能再落进
 * `Number(null) === 0` 这条路径——那会把「没有比例」显示成「0%」。
 */
export function similarityPercent(candidate) {
  const raw = candidate?.similarity_score
  if (raw === null || raw === undefined || raw === '') return null
  const score = Number(raw)
  if (!Number.isFinite(score)) return null
  return `${Math.round(score * 100)}%`
}

export function isComparableDisposition(candidate) {
  return COMPARABLE_DISPOSITIONS.includes(candidate?.comparison_disposition)
}

/** 标题级判定，文案与改动前一致，只是不再把分数挤在括号里。 */
export function alternativeVerdictLabel(candidate = {}, { reference = false } = {}) {
  const item = candidate || {}
  if (reference) return '同品牌参考'
  if (item.comparison_disposition === 'recommended_comparison') return '推荐比较'
  if (item.comparison_disposition === 'comparable_candidate') return '可比较候选'
  if (item.status === 'eligible') return '已核对的指标满足'
  if (item.status === 'ineligible') return '关键指标不满足'
  return '参数尚未核全'
}

export function alternativeVerdict(candidate = {}, { reference = false } = {}) {
  const item = candidate || {}
  const label = alternativeVerdictLabel(item, { reference })
  const score = similarityPercent(item)
  if (reference) {
    return { key: 'reference', label, tone: 'muted', scoreTag: null, basis: '', boundary: '', detail: '', substitutable: false }
  }
  const disposition = item.comparison_disposition
  const comparable = COMPARABLE_DISPOSITIONS.includes(disposition)
  const scoreTag = comparable ? `已核验 ${score || '达到阈值'}` : null
  if (disposition === 'recommended_comparison') {
    return { key: 'recommended_comparison', label, tone: 'success', scoreTag, basis: SIMILARITY_BASIS, boundary: COMPARABLE_BOUNDARY, detail: '', substitutable: false }
  }
  if (disposition === 'comparable_candidate') {
    return { key: 'comparable_candidate', label, tone: 'warning', scoreTag, basis: SIMILARITY_BASIS, boundary: COMPARABLE_BOUNDARY, detail: '', substitutable: false }
  }
  if (item.status === 'eligible') {
    return { key: 'eligible', label, tone: 'success', scoreTag: null, basis: '', boundary: COMPARABLE_BOUNDARY, detail: '', substitutable: false }
  }
  if (item.status === 'ineligible') {
    return { key: 'ineligible', label, tone: 'danger', scoreTag: null, basis: '', boundary: INELIGIBLE_BOUNDARY, detail: '', substitutable: false }
  }
  return { key: 'unresolved', label, tone: 'warning', scoreTag: null, basis: '', boundary: UNRESOLVED_BOUNDARY, detail: '', substitutable: false }
}

function parameterNames(values) {
  return (Array.isArray(values) ? values : [])
    .map(item => ({ name: String(item?.name ?? '').trim() || String(item?.required?.name ?? '').trim(), note: '' }))
    .filter(item => item.name)
}

/**
 * 三类（及临时、冲突）参数清单。
 *
 * 没有内容的类别整条不返回——页面上不会出现「缺证：」后面空着，也不写「无」。
 * `includeSatisfied` 为 false 时不列「已核验满足」：比例没到阈值时，列出满足项
 * 会让一个「参数尚未核全」的候选看起来像可行方案。
 */
export function alternativeParameterGroups(candidate = {}, { includeSatisfied = true } = {}) {
  const rows = matrixRows(candidate)
  const buckets = { violated: [], provisionalViolated: [], satisfied: [], pendingReview: [], provisional: [], missing: [] }
  if (!rows.length) {
    buckets.violated = parameterNames(candidate?.[FALLBACK_LISTS.violated])
    buckets.satisfied = parameterNames(candidate?.[FALLBACK_LISTS.satisfied])
    buckets.missing = parameterNames(candidate?.[FALLBACK_LISTS.missing])
  } else {
    for (const row of rows) {
      const name = String(row.required?.name ?? row.name ?? '').trim()
      if (!name) continue
      const provisional = row.evidence_status === 'provisional'
      if (row.conflict) {
        buckets.pendingReview.push({ name, note: '原文互相冲突' })
      } else if (row.status === 'violated') {
        // 差额（"差多少"）由后端算好、写成一句人话放在 ``shortfall_text`` 上：正式候选
        // 卡片原来只列"已核实不满足的指标"，用户看不到差 3% 还是差 50%。后端没给这一句
        // 时（旧数据、或那一对值算不出差额）note 就是空，**不由前端编一句话**。
        // 临时来源那一组不显示差额：那里的结论本身还没定（与 PROVISIONAL_VIOLATION_NOTE 同一口径）。
        ;(provisional ? buckets.provisionalViolated : buckets.violated).push({
          name,
          note: provisional ? '' : String(row.shortfall_text ?? '').trim(),
        })
      } else if (row.status === 'satisfied' && !provisional) {
        buckets.satisfied.push({ name, note: '' })
      } else if (provisional) {
        buckets.provisional.push({ name, note: '' })
      } else if (row.raw_evidence?.length) {
        buckets.pendingReview.push({ name, note: '' })
      } else {
        buckets.missing.push({ name, note: '' })
      }
    }
  }
  return PARAMETER_GROUP_DEFS
    .filter(def => includeSatisfied || def.key !== 'satisfied')
    .map(def => ({ ...def, items: buckets[def.key] }))
    .filter(group => group.items.length > 0)
}

/**
 * 后端写好的那句结论（例如「推荐比较：全部已列关键指标均有可核验证据；仍需
 * 单独核对价格、配置和售后」）在卡片上作为补充说明，别把判定再念一遍。
 */
export function verdictDetail(candidate = {}, label = '') {
  const conclusion = String(candidate?.conclusion || '').trim()
  if (!conclusion) return ''
  for (const prefix of [label, '推荐比较', '可比较候选', '已核对的指标满足', '关键指标不满足', '参数尚未核全'].filter(Boolean)) {
    if (!conclusion.startsWith(prefix)) continue
    const rest = conclusion.slice(prefix.length)
      .replace(/^[（(][^）)]*[）)]/, '')
      .replace(/^[：:；;、\s]+/, '')
      .trim()
    return rest
  }
  return conclusion
}

export function provisionalParameterNote(count) {
  return count > 0 ? `${PROVISIONAL_NOTE_PREFIX} ${count} 项临时参数；来源身份尚未核实，不计入合格替代结论。` : null
}

/**
 * 「这个候选项算不算原品牌」的分组键与判定。
 *
 * 判据**在后端**：读接口（``procurement_research.annotate_candidate_brands``）给出
 * ``brand_key``（规范写法）与 ``same_brand_as_baseline``（算不算原品牌），
 * 因为别名表的唯一来源在后端（``procurement_rules_improved._BRAND_ALIASES``）。
 * 页面上原来硬编码过一份只有 ``天津泰斯特``／``上海一恒`` 两条的别名表，缺 ``徕卡``
 * 与两条苏州安泰，界面上就把同一家分成两组——那正是"同一张表存在两处"的老病根，
 * 所以这里不再保留任何别名条目。
 *
 * 保留**退化**：老 payload 没有这两个字段时，按写法本身（NFKC 归一、去空白与连字符、
 * 小写）相等分组。那种数据里 ``天津泰斯特`` 与 ``泰斯特`` 会分成两组，是旧数据的已知
 * 限制；``alternatives`` 为空时这一步不产生任何分组，也不会出错。
 */
const brandToken = value => String(value || '').normalize('NFKC').replace(/[\s‐‑–—-]+/g, '').toLowerCase()

export function brandGroupKey(candidate = {}) {
  const item = candidate || {}
  return item.brand_key || brandToken(item.brand)
}

export function sameBrandAsBaseline(candidate = {}, baseline = {}) {
  const item = candidate || {}
  if (typeof item.same_brand_as_baseline === 'boolean') return item.same_brand_as_baseline
  const baselineKey = brandToken((baseline || {}).brand)
  return Boolean(baselineKey) && brandToken(item.brand) === baselineKey
}

// --- 候选卡片的身份、逐项差异、置信度（两段式「先展示候选、按需查价」那一半）----
//
// 数据来源是后端 ``research_planning.configuration_alignment`` 写到候选上的
// ``candidate['config_match']``（在 ``procurement_research.alternatives()`` 里
// ``candidate['config_match'] = configuration_alignment(...)``）：
//
//     {'score': 0.5, 'items': [{'name','required':{...},'observed':{...}|null,
//                               'status':'satisfied|violated|unknown',
//                               'kind':'match|different|unknown',
//                               'shortfall': {...}}, ...],
//      'satisfied': [...], 'violated': [...], 'unknown': [...],
//      'diff_points': [...], 'hard_failures': [...], 'shortfalls': [...],
//      'soft_score': 0.95 | None, 'confidence': 0.67 | None, 'basis': 'compared' | ...}
//
// 三条铁律（对应 ``docs/HANDOVER-alt-two-stage.md`` 第三节）：
//
// 1. **逐项三态、不给单一百分比**：一项一行，判"一致／不同／未知"，取代过去那种
//    一个相似度数字盖住所有差异的读法（Octopart cross-reference 的读法）。
// 2. **未知就是未知，字面写成"正文里没写"**：后端 docstring 说得清楚——"把没查到
//    当成满足是撒谎"。所以 ``unknown`` 的行既不进 ``satisfied`` 那一类，也不给
//    任何正向色调，显示的字面就是"正文里没写"。本模块的 ``satisfied`` 布尔只对
//    ``match`` 为真，测试直接钉这一条（unknown 的行 ``satisfied === false``）。
// 3. **差额只印后端那句话**：``configuration_alignment`` 只在**逐项核验那条路**
//    （``candidate['matrix']`` 的行）上挂了 ``shortfall_text``；``config_match.items``
//    上目前只有结构化的 ``shortfall``（``{comparable, percent, side, ...}``）。
//    所以这里**只印 ``item.shortfall_text``**，后端没给就留空——**不自己按 percent
//    拼一句话**（同一句文案的唯一来源是后端 ``research_planning.shortfall_text``）。
//    这是后端的一处缺口，这次不动后端。

/** 品牌未从原文里结构化提取出来时的字面（后端报告侧同一口径：``品牌未解析``）。 */
export const BRAND_UNRESOLVED = '品牌未解析'

export function candidateBrandLabel(candidate = {}) {
  const brand = String((candidate || {}).brand || '').trim()
  return brand || BRAND_UNRESOLVED
}

export function candidateTitle(candidate = {}) {
  const item = candidate || {}
  const model = String(item.model || '').trim()
  return [candidateBrandLabel(item), model].filter(Boolean).join(' ')
}

/** 逐项差异的三种状态：标签、色调，以及"未知"必须带上的那句字面。 */
export const CONFIG_MATCH_UNKNOWN_NOTE = '正文里没写'
export const CONFIG_MATCH_STATE_DEFS = {
  match: { key: 'match', label: '一致', tone: 'success', note: '' },
  different: { key: 'different', label: '不同', tone: 'danger', note: '' },
  unknown: { key: 'unknown', label: '未知', tone: 'muted', note: CONFIG_MATCH_UNKNOWN_NOTE },
}

/** 要求的写法：``不低于 20000 r/min``。方向词来自后端 ``required.direction``。 */
const DIRECTION_WORDS = { min: '不低于', max: '不高于', equal: '要求', covers: '覆盖范围' }

function valueText(value) {
  if (value === null || value === undefined || value === '') return ''
  if (Array.isArray(value)) return value.map(item => valueText(item)).filter(Boolean).join(' 至 ')
  return String(value)
}

function requirementText(required = {}) {
  const word = DIRECTION_WORDS[required?.direction] || '要求'
  const shown = [valueText(required?.value), String(required?.unit || '').trim()].filter(Boolean).join(' ')
  return shown ? `${word} ${shown}` : word
}

function observedText(observed = {}) {
  if (!observed || typeof observed !== 'object') return ''
  const shown = [valueText(observed.value), String(observed.unit || '').trim()].filter(Boolean).join(' ')
  const conditions = String(observed.conditions || '').trim()
  if (!shown) return ''
  return conditions ? `${shown}（条件：${conditions}）` : shown
}

/**
 * ``config_match.items`` → 卡片上一行一项的三态差异。
 *
 * ``state`` 优先读后端写好的 ``kind``（``match``/``different``/``unknown``，就是给
 * 卡片用的那个字段），缺失时按 ``status`` 退化映射；两者都认不出来算 ``unknown``
 * ——**默认落到"未知"这一档，不落"满足"**：认不出来的东西一律不算满足。
 */
export function configMatchRows(candidate = {}) {
  const items = ((candidate || {}).config_match || {}).items
  if (!Array.isArray(items)) return []
  return items.map((item, index) => {
    const row = item && typeof item === 'object' ? item : {}
    const kind = String(row.kind || '').trim()
    const status = String(row.status || '').trim()
    const key = CONFIG_MATCH_STATE_DEFS[kind] ? kind
      : status === 'satisfied' ? 'match' : status === 'violated' ? 'different' : 'unknown'
    const def = CONFIG_MATCH_STATE_DEFS[key]
    const observed = observedText(row.observed)
    return {
      // key 只用于 React 列表，取"状态＋指标名＋位次"；页面上不出现下标。
      key: `${def.key}-${String(row.name || '').trim() || '未命名指标'}-${index}`,
      name: String(row.name || '').trim() || '未命名指标',
      state: def.key,
      stateLabel: def.label,
      stateNote: def.note,
      tone: def.tone,
      satisfied: def.key === 'match',
      required: requirementText(row.required),
      // 未知项在页面上印的就是"正文里没写"：不能只留一个空格，也不能印成满足。
      observed: def.key === 'unknown' ? CONFIG_MATCH_UNKNOWN_NOTE : (observed || '未读到'),
      // 差额：后端在 ``items`` 上挂了 ``shortfall_text`` 才印；没挂就留空，前端不编。
      shortfallText: def.key === 'different' ? String(row.shortfall_text || '').trim() : '',
      evidenceUrl: String(row.observed?.evidence?.url || '').trim(),
      excerpt: String(row.observed?.evidence?.excerpt || '').trim(),
    }
  })
}

/**
 * 置信度（有值项占全部要求的比例）说成人话。
 *
 * ``confidence`` 为 ``None`` 是后端"没有可比的东西"，**不能显示成 0%**（这与
 * ``similarityPercent`` 那条同源：``Number(null) === 0`` 会把"没有比例"印成 0%）。
 */
export function configMatchConfidence(candidate = {}) {
  const match = ((candidate || {}).config_match) || {}
  const rows = configMatchRows(candidate)
  const comparable = rows.filter(row => row.state === 'match' || row.state === 'different').length
  const total = rows.length
  const raw = match.confidence
  const basis = String(match.basis || '')
  if (raw === null || raw === undefined || raw === '') {
    return basis === 'no_confirmed_configuration'
      ? { text: '', note: '本轮没有确认配置要求，没有可比项，算不出已核验比例。' }
      : { text: '', note: '没有读到可核验的值，算不出已核验比例。' }
  }
  const score = Number(raw)
  if (!Number.isFinite(score)) return { text: '', note: '置信度字段读不出来，不显示百分比。' }
  return { text: `${Math.round(score * 100)}%`, note: total ? `${comparable} / ${total} 项有可核验的值。` : '' }
}

/**
 * 数值接近程度（``soft_score``）说成人话；``null`` 时**不显示 0**，只说明为什么没有。
 */
export function configMatchSoftScore(candidate = {}) {
  const raw = ((candidate || {}).config_match || {}).soft_score
  if (raw === null || raw === undefined || raw === '') {
    return { text: '', note: '没有读到可比较的数值型指标，算不出接近程度。' }
  }
  const score = Number(raw)
  if (!Number.isFinite(score)) return { text: '', note: '接近程度字段读不出来，不显示百分比。' }
  return { text: `${Math.round(score * 100)}%`, note: '只统计读到数值的那几项。' }
}

/** 整块差异区要用的东西一次给全，页面只排版、不判读。 */
export function configMatchPresentation(candidate = {}) {
  const match = (candidate || {}).config_match
  const rows = configMatchRows(candidate)
  const present = Boolean(match && typeof match === 'object')
  return {
    present,
    rows,
    counts: {
      match: rows.filter(row => row.state === 'match').length,
      different: rows.filter(row => row.state === 'different').length,
      unknown: rows.filter(row => row.state === 'unknown').length,
    },
    confidence: configMatchConfidence(candidate),
    softScore: configMatchSoftScore(candidate),
    // 一项都没有时的说法：两种原因分开说，不含糊（"没有可比内容"不等于"都满足"）。
    emptyNote: rows.length ? '' : '这一条候选没有可与已确认要求逐项对齐的内容：正文里没读到，或本轮没有确认配置要求。',
  }
}

export function alternativePresentation(candidate = {}, { baseline = {}, reference = false } = {}) {
  const rows = matrixRows(candidate)
  const verdict = alternativeVerdict(candidate, { reference })
  const groups = alternativeParameterGroups(candidate, { includeSatisfied: isComparableDisposition(candidate) })
  // 有负向事实时不再补后端结论：那几句原本就只说正向结论，混在一起又会变成一段墙。
  const detail = groups.some(group => NEGATIVE_KEYS.includes(group.key))
    ? ''
    : verdictDetail(candidate, verdict.label) || EMPTY_CONCLUSION
  const provisionalCount = rows.length
    ? rows.filter(row => row.evidence_status === 'provisional').length
    : Number(candidate?.provisional_parameter_count) || 0
  return {
    verdict: { ...verdict, detail },
    groups,
    provisionalCount,
    provisionalNote: provisionalParameterNote(provisionalCount),
    category: candidate.category || `${baseline?.category || '同类仪器'}（按本轮检索范围，仍需核对用途）`,
    rowCount: rows.length,
  }
}
