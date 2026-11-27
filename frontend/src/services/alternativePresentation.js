/**
 * 「跨品牌罗列」候选卡片的呈现逻辑。
 *
 * 单独放在 service 里，是为了让「这张卡片摆出什么、不摆什么」可以被 node --test
 * 直接断言，不必起浏览器。组件只负责排版。
 *
 * 口径（设计规范）：**候选卡片只罗列资料原文，不替用户判断"是否满足需求"**。
 * 这条不是新拍的，回归基线里两句原话就是它——「候选只展示能抓到的关键配置，不替用户
 * 判断是否满足需求」（``回归基线`` 的「业务口径」），
 * 以及 规范项 的「结果里只摆配置原文，不再输出满足 / 差异 / 未知」
 * （``回归基线``）。
 *
 * 于是这一模块**不再产出**：一致／不同／未知三态、已核验比例、数值接近程度、
 * 「已核对的指标满足」「关键指标不满足」这类结论，以及任何"可替代"的说法。留下的
 * 只有事实与证据质量：
 *
 * 1. **这条候选页面上逐字读到的配置**（``configListing``）——指标名 + 页面里写的
 *    写法 + 原文出处。读不到的不铺成一行"未知"（一整排未知会让人觉得"什么都没
 *    查到"），只用一句如实说有几项没写到。
 * 2. **证据本身的问题**（``evidenceNotes``）：原文互相冲突、正文来自尚未核实的
 *    来源。这不是需求判断，是这段话能信几分——``设计规范`` §3.4 要求不确定性在
 *    受影响之前说出来。
 * 3. 型号身份、品类与来源（``candidateTitle`` 与页面上的来源块）。
 *
 * ``substitutable`` 恒为 false：卡片永远不声称某个候选可以替代采购目标（原来那条
 * "任何结论都不称可替代"的产品规则继续由测试钉住）。
 */
import { matchesAnyAnchor } from './candidateRoster.js'

export const BRAND_UNRESOLVED = '品牌未解析'

/** 卡片上配置那一块的口径句。判断满足与否是采购员的事，这里只说边界。 */
export const CANDIDATE_LISTING_BOUNDARY = '以下为该候选页面或资料中写到的配置，不代表其满足本轮实验要求；是否可用请按原文核对。'

export const PROVISIONAL_VIOLATION_NOTE = '来源身份确认后再作正式判断。'
export const PROVISIONAL_NOTE_PREFIX = '已从网页正文提取'
export const PROVISIONAL_PARAMETER_NOTE = '来源身份确认后再作正式判断。'

/** 证据质量的两类提示（不是"满足／不满足"的判断）。 */
export const EVIDENCE_NOTE_DEFS = [
  { key: 'conflict', label: '原文互相冲突的指标', tone: 'warning', note: '同一条指标在不同来源里写法不一致；这一条不替任何一方下结论。' },
  { key: 'provisional', label: '来自尚未核实来源的正文', tone: 'warning', note: PROVISIONAL_VIOLATION_NOTE },
]

const COMPARABLE_DISPOSITIONS = ['comparable_candidate', 'recommended_comparison']

export function matrixRows(candidate) {
  return Array.isArray(candidate?.matrix) ? candidate.matrix.filter(row => row && typeof row === 'object') : []
}

export function isComparableDisposition(candidate) {
  return COMPARABLE_DISPOSITIONS.includes(candidate?.comparison_disposition)
}

/**
 * 卡片顶部只回答"这条线索是怎么来的"，不判满足与否。
 *
 * 原来的标签是「推荐比较 / 可比较候选 / 已核对的指标满足 / 关键指标不满足 /
 * 参数尚未核全」——后三个直接是需求满足判断，前两个也带着"该不该考虑"的暗示，
 * 都随 规范项 一起下线。现在只区分：跨品牌候选 / 同品牌参考 / 只进了线索名单。
 */
export function alternativeVerdictLabel(candidate = {}, { reference = false } = {}) {
  const item = candidate || {}
  if (reference) return '同品牌参考'
  if (isComparableDisposition(item)) return '本轮核验的候选'
  return '本轮线索'
}

export function alternativeVerdict(candidate = {}, { reference = false } = {}) {
  const item = candidate || {}
  const label = alternativeVerdictLabel(item, { reference })
  if (reference) {
    return { key: 'reference', label, tone: 'muted', boundary: '', detail: '', substitutable: false }
  }
  return {
    key: isComparableDisposition(item) ? 'verified' : 'lead',
    label,
    tone: 'neutral',
    boundary: CANDIDATE_LISTING_BOUNDARY,
    detail: '',
    substitutable: false,
  }
}

/**
 * 证据质量：原文冲突与临时来源，各自单独成组。
 *
 * 只有这两类会被列出来。原来那六类（已核实满足／已核实不满足／临时可能不满足／
 * 已有原文待核对／临时正文证据／缺少证据）里有三类是需求判断，另两类是"待核实"
 * 的铺陈（设计规范：「不要给用户太多待核实的信息，能获取到就行」），
 * 一并下线。冲突那一类必须留：同一指标两种写法打架时，读者有权知道。
 */
export function evidenceNotes(candidate = {}) {
  const buckets = { conflict: [], provisional: [] }
  for (const row of matrixRows(candidate)) {
    const name = String(row.required?.name ?? row.name ?? '').trim()
    if (!name) continue
    if (row.conflict) buckets.conflict.push({ name, note: '原文互相冲突' })
    else if (row.evidence_status === 'provisional') buckets.provisional.push({ name, note: '' })
  }
  return EVIDENCE_NOTE_DEFS
    .map(def => ({ ...def, items: buckets[def.key] }))
    .filter(group => group.items.length > 0)
}

export function provisionalParameterNote(count) {
  return count > 0 ? `${PROVISIONAL_NOTE_PREFIX} ${count} 项临时参数；${PROVISIONAL_PARAMETER_NOTE}` : null
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

// --- 候选卡片的身份与「读到的配置」------------------------------------------
//
// 数据来源是后端 ``research_planning.configuration_alignment`` 写到候选上的
// ``candidate['config_match']``（在 ``procurement_research.alternatives()`` 里
// ``candidate['config_match'] = configuration_alignment(...)``）：
//
//     {'score': 0.5, 'items': [{'name','required':{...},'observed':{...}|null,
//                               'status':'satisfied|violated|unknown',
//                               'kind':'match|different|unknown', ...}, ...],
//      'soft_score': 0.95 | None, 'confidence': 0.67 | None, ...}
//
// 读取层的判据只有一条：**有 ``observed`` 的值或原文，才算"页面上写到了"**。
// 后端那些 ``status`` / ``kind`` / ``shortfall`` 字段这一层一个都不用——判"一致／
// 不同"是采购员按原文自己做的事，不是卡片要做的事。

/** 品牌未从原文里结构化提取出来时的字面（后端报告侧同一口径：``品牌未解析``）。 */
export function candidateBrandLabel(candidate = {}) {
  const brand = String((candidate || {}).brand || '').trim()
  return brand || BRAND_UNRESOLVED
}

/**
 * 候选卡片的对外身份只选一项：明确品牌优先，品牌缺失时退回页面关联供应商。
 * ``supplier`` 不参与品牌分组，也不写回 ``brand``；它只是让已有型号线索有可读的
 * 关联企业，避免页面只剩一个“品牌未解析”。
 */
export function candidateIdentity(candidate = {}) {
  const item = candidate || {}
  const brand = String(item.brand || '').trim()
  if (brand) return { value: brand, kind: 'brand', label: '品牌' }
  const supplier = String(item.supplier || '').trim()
  if (supplier) return { value: supplier, kind: 'supplier', label: '供应商' }
  return { value: BRAND_UNRESOLVED, kind: 'unresolved', label: '品牌' }
}

export function candidateTitle(candidate = {}) {
  const item = candidate || {}
  const model = String(item.model || '').trim()
  const identity = candidateIdentity(item)
  const prefix = identity.kind === 'supplier' ? `${identity.label}：${identity.value}` : identity.value
  return [prefix, model].filter(Boolean).join(' ')
}

function valueText(value) {
  if (value === null || value === undefined || value === '') return ''
  if (Array.isArray(value)) return value.map(item => valueText(item)).filter(Boolean).join(' 至 ')
  return String(value)
}

function observedText(observed = {}) {
  if (!observed || typeof observed !== 'object') return ''
  const shown = [valueText(observed.value), String(observed.unit || '').trim()].filter(Boolean).join(' ')
  const conditions = String(observed.conditions || '').trim()
  if (!shown) return ''
  return conditions ? `${shown}（条件：${conditions}）` : shown
}

/**
 * 三个来源 → 卡片上"这条候选页面上写到的配置"。
 *
 * 一项一行，只收**逐字读到**的东西。三个来源按优先级排（同名项取先出现的那个）：
 *
 * 1. ``config_match.items``：候选详情页正文里逐项读到的（``observed.value`` 或
 *    ``observed.evidence.excerpt`` 有其一就算读到）；
 * 2. ``matrix``：跨品牌核验那条路读到的参数——实测天平那一轮（PTY-124/223）
 *    ``config_match.items`` 四项全是空的，值只落在 ``matrix`` 的 ``raw_evidence``
 *    里；只读第一处会让配置栏显示"什么都没读到"，而页面抓到的原文其实在。
 * 3. ``candidate.configuration``：候选自己的资料正文（M4 的"有配置"判据就是它）。
 *    实测洁净台那一轮（bekomat30）只有这一份原文；它整段摆出来，标成"配置原文"。
 *
 * ``value`` 给结构化写法（"16500 r/min"这种一眼能用的）；只有原文时就印原文；
 * ``excerpt`` 另起一列给原文那一句，``url`` 给这一项的出处。没读到的项由
 * ``configListing().missingCount`` 汇总成一句话，不铺成表。
 *
 * ``anchors`` 是用户关注参数的锚点（``services/candidateRoster.js``）：命中的项在页面上
 * 加粗（``watched``），**只加粗**——不上色、不打勾、不写满足与否。
 */
export function configSourceRows(candidate = {}, { anchors = [] } = {}) {
  const item = candidate || {}
  const rows = []
  const seen = new Set()
  const push = row => {
    const name = String(row.name || '').trim() || '未命名指标'
    if (seen.has(name)) return
    seen.add(name)
    rows.push({
      key: `${name}-${rows.length}`,
      ...row,
      name,
      // 整段资料原文（``block``）不是"一项配置"，不加粗。
      watched: !row.block && matchesAnyAnchor(name, anchors),
    })
  }

  const items = (item.config_match || {}).items
  for (const entry of Array.isArray(items) ? items : []) {
    const row = entry && typeof entry === 'object' ? entry : {}
    const shown = observedText(row.observed)
    const excerpt = String(row.observed?.evidence?.excerpt || '').trim()
    if (!shown && !excerpt) continue
    push({
      name: String(row.name || '').trim(),
      value: shown || excerpt,
      excerpt: shown && excerpt ? excerpt : '',
      url: String(row.observed?.evidence?.url || '').trim(),
    })
  }

  for (const row of matrixRows(item)) {
    const name = String(row.required?.name ?? row.name ?? '').trim()
    if (!name) continue
    const shown = observedText(row.observed)
    const evidence = (Array.isArray(row.raw_evidence) ? row.raw_evidence : []).find(entry => entry && entry.excerpt)
    const excerpt = String(evidence?.excerpt || '').trim()
    if (!shown && !excerpt) continue
    push({
      name,
      value: shown || excerpt,
      excerpt: shown && excerpt && excerpt !== shown ? excerpt : '',
      url: String(row.observed?.url || evidence?.url || '').trim(),
    })
  }

  // 候选自己的资料正文：整段原文，页面上用 <pre> 原样摆（``block`` 标记）。
  const text = cleanConfigText(item.configuration)
  if (text && !['unknown', '未提供'].includes(text.toLowerCase())) {
    push({
      name: '配置原文',
      value: text,
      excerpt: '',
      url: String(item.configuration_url || '').trim(),
      block: true,
    })
  }
  return rows
}

const NOISE_LINE_RE = /^(?:微信扫一扫|扫一扫|分享至?朋友圈|分享到朋友圈|朋友圈|生成海报|海报分享|扫码分享|点击分享|QQ空间|新浪微博|分享给朋友|收藏本页|打印本页|返回顶部|上一篇|下一篇|联系我们|在线咨询)[：:：\s]*$/i

export function cleanConfigText(raw) {
  const lines = String(raw || '').split(/\r?\n/)
  const kept = []
  for (const line of lines) {
    const trimmed = line.trim()
    if (!trimmed) {
      if (kept.length && kept[kept.length - 1] !== '') kept.push('')
      continue
    }
    if (NOISE_LINE_RE.test(trimmed)) continue
    const cleaned = line
      .replace(/(?:微信)?扫一扫(?:分享至?朋友圈)?/g, '')
      .replace(/分享至?朋友圈|生成海报|海报分享|扫码分享/g, '')
      .trimEnd()
    if (cleaned.trim()) kept.push(cleaned)
  }
  while (kept.length && kept[kept.length - 1] === '') kept.pop()
  return kept.join('\n').trim()
}

/** 配置那一块要用的东西一次给全，页面只排版、不判读。 */
export function configListing(candidate = {}, { anchors = [] } = {}) {
  const item = candidate || {}
  const match = item.config_match
  const declared = Array.isArray((match || {}).items) && match.items.length
    ? match.items.length
    : matrixRows(item).length
  const rows = configSourceRows(item, { anchors })
  // "配置原文"那一行是整段资料正文，不是"一项要求"，不参与项数统计。
  const named = rows.filter(row => !row.block)
  return {
    // 只有配置原文（``configuration``）的候选也要摆出来——M4 的"有配置"判据本来就认它。
    present: Boolean((match && typeof match === 'object') || rows.length),
    rows,
    readCount: rows.length,
    // 命中了用户关注参数的项数（候选排序的一级键，与页面加粗同一判据）。
    watchedCount: named.filter(row => row.watched).length,
    // 页面上没写到的项数照实给出去（不静默丢），但不逐项铺开。
    missingCount: Math.max(declared - named.length, 0),
    emptyNote: rows.length ? '' : '该候选页面未读到可引用的配置原文。',
    boundary: CANDIDATE_LISTING_BOUNDARY,
  }
}

export function alternativePresentation(candidate = {}, { baseline = {}, reference = false } = {}) {
  const rows = matrixRows(candidate)
  const verdict = alternativeVerdict(candidate, { reference })
  const provisionalCount = rows.length
    ? rows.filter(row => row.evidence_status === 'provisional').length
    : Number(candidate?.provisional_parameter_count) || 0
  return {
    verdict,
    evidenceNotes: evidenceNotes(candidate),
    provisionalCount,
    provisionalNote: provisionalParameterNote(provisionalCount),
    category: candidate.category || `${baseline?.category || '同类仪器'}（按本轮检索范围，仍需核对用途）`,
    rowCount: rows.length,
  }
}
