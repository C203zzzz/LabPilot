/**
 * 「查这个的价格」——两段式（先展示候选、用户点哪个才查哪个）的前端一半。
 *
 * 后端接口：::
 *
 *     POST /api/v1/tasks/{task_id}/research-runs/{run_id}/alternatives/{candidate_id}/price
 *
 * 请求体可以为空 ``{}``；只有 ``force`` 一个可选键，**默认 false**
 * （``backend/api/procurement.py`` 的 ``lookup_alternative_price``，服务侧
 * ``procurement_research.lookup_candidate_price``：已经有结果的候选再点一次直接
 * 端回已有结果，``spent`` 三个零，不重复花钱）。
 *
 * **"一次只查一个候选"在前端这一侧有三重保证，本模块是其中两重的落点**：
 *
 * 1. **接口形状**（后端，这次不动）：要查哪个由**路径里的 candidate_id** 决定，
 *    一次请求只可能命中一个候选；不存在"传一批下标"的用法。
 * 2. **请求由本模块构造**：``candidatePricePath`` 只接受**一个** ``candidateId``，
 *    ``candidatePriceBody`` 恒为 ``{}`` —— 请求体里连候选标识都不出现，更没有
 *    候选数组；因此无论点多少次，也不可能一次提交两个候选。
 * 3. **回写只认那一个键**：``applyPriceLookup`` 只改 ``candidateId`` 这一个键，
 *    其它候选的条目**按引用原样返回**（组件因此只会重画这一张卡片的价格区）；
 *    响应里的 ``candidate_id`` 与所点的候选不一致时 ``priceLookupResult`` 返回
 *    ``null``，卡片宁可什么都不更新，也不会把别人的价格贴到这张卡上。
 *
 * 另一条硬要求是**说清楚"没查"和"查了没有"**（后端 ``alternative_price_lookup``
 * 的三种形状：① 没查 ② 查了没有 ③ 有来源但没被采信）。所以这里把所有状态码翻译成
 * 带**族名**的人话（``没查：``／``查了没有：``／``查到了但没有采用``），
 * 页面上一个内部字段名、一个状态码都不出现（见 tests/alternativePriceLookup.test.mjs
 * 里那条"不许出现 snake_case 与网址"的断言）。
 *
 * 本模块是纯函数 + 一次可注入的 ``post``，不起浏览器就能断言请求形状与文案。
 */

/** 后端 ``alternative_price_lookup.STATUSES`` 里会出现在候选卡片上的那几个。 */
export const PRICE_LOOKUP_FAMILY = {
  priced: '查到了',
  refused: '查到了但没有采用',
  read_without_price: '查了没有',
  sources_not_read: '查了没有',
  no_price_source: '查了没有',
  not_attempted: '没查',
  not_requested: '没查',
  unrecorded: '',
}

const STATUS_LABELS = {
  priced: '已查到这个候选的成交价',
  refused: '抽到的价格行没通过核价判据，不能当作成交价',
  read_without_price: '来源正文里没有这个型号的成交单价',
  sources_not_read: '有来源写着这个型号，但这一轮没读到来源正文',
  no_price_source: '价格通道里没有能用于这个候选比价的来源',
  not_attempted: '这一轮没有为这个候选查价',
  not_requested: '这个候选的价格要点了“查这个的价格”才会去查',
  unrecorded: '这一轮这个候选的查价结论没有记录下来',
}

const STATUS_TONES = {
  priced: 'success',
  refused: 'warning',
  read_without_price: 'warning',
  sources_not_read: 'warning',
  no_price_source: 'warning',
  not_attempted: 'muted',
  not_requested: 'muted',
  unrecorded: 'muted',
}

/** ① 没查的原因（``alternative_price_lookup.NOT_ATTEMPTED_REASONS``），逐条说人话。 */
const NOT_ATTEMPTED_NOTES = {
  retained_from_previous_round: '这一轮沿用了上一轮的结果，没有重新查。',
  lead_below_requirement: '线索里的参数可能低于要求，没有进入查价。',
  duplicate_model: '和另一个候选是同一个型号，本轮只查一次。',
  source_not_usable: '找到的来源不可用，没有进入查价。',
  verification_limit: '本轮核验名额已用完，没有进入查价。',
  no_sources_found: '这一轮没有为它找到价格来源。',
  scan_not_recorded: '没有留下这次查价的记录。',
  not_requested: '等你点了“查这个的价格”才会去查。',
}

const UNKNOWN_STATUS = { label: '这个候选的价格情况没有记录清楚', tone: 'muted', family: '' }

/**
 * 候选自己带的查价留痕（``candidate.price_lookup``，后端
 * ``alternative_price_lookup.price_lookup`` 的形状）说成人话。
 *
 * ``status`` 认不出来时**不回显那个码**，也不猜成"没查"或"查了没有"——那两句话
 * 是有事实含义的（一个说没花钱、一个说花了钱没结果），猜错就是把用户往错的方向带。
 */
export function candidatePriceView(candidate = {}) {
  const item = candidate || {}
  const lookup = item.price_lookup || {}
  const status = String(lookup.status || '').trim()
  const reason = String(lookup.reason_code || '').trim()
  const prices = Array.isArray(item.prices) ? item.prices : []
  if (!status && prices.length) {
    // 老数据/开关关着时批量查到价、没写 price_lookup 的那种：有价就是有价。
    return { family: PRICE_LOOKUP_FAMILY.priced, label: STATUS_LABELS.priced,
             tone: 'success', note: `共 ${prices.length} 条价格记录。`, prices }
  }
  if (!status) {
    return { family: PRICE_LOOKUP_FAMILY.not_attempted,
             label: `没查：${STATUS_LABELS.not_requested}`,
             tone: 'muted',
             note: '（较早一轮的数据可能没有这条留痕；点“查这个的价格”现在去查。）',
             prices }
  }
  const known = Boolean(STATUS_LABELS[status])
  const family = PRICE_LOOKUP_FAMILY[status] || ''
  const base = known ? STATUS_LABELS[status] : UNKNOWN_STATUS.label
  const label = known ? (family ? `${family}：${base}` : base) : UNKNOWN_STATUS.label
  const note = status === 'not_attempted' || status === 'not_requested'
    ? (NOT_ATTEMPTED_NOTES[reason] || '')
    : (status === 'priced' ? `共 ${prices.length || Number(lookup.price_count) || 0} 条价格记录。` : '')
  return { family, label, tone: STATUS_TONES[status] || 'muted', note,
           prices, priceCount: Number(lookup.price_count) || prices.length }
}

/** 价格来源地址：只取记录里的 ``url``（与 pricePresentation.priceSourceLinks 同一口径）。 */
export function priceRecordLinks(price = {}) {
  const url = String((price || {}).url || '').trim()
  return url ? [url] : []
}

function spentNote(spent) {
  if (!spent || typeof spent !== 'object') return ''
  const parts = []
  if (Number(spent.search)) parts.push(`检索 ${Number(spent.search)} 次`)
  if (Number(spent.fetch)) parts.push(`读取来源 ${Number(spent.fetch)} 个`)
  if (Number(spent.model)) parts.push(`模型调用 ${Number(spent.model)} 次`)
  return parts.length ? `本次新增：${parts.join('、')}。` : '本次没有新增检索。'
}

/**
 * 卡片价格区要显示的那一条：优先用**这次点按钮**拿回来的结果，没有就显示候选自己
 * 的留痕。``prices`` 也按同一优先级取——点完之后这张卡的价格区立刻是新结果，
 * 不需要用户刷新整页。
 */
export function priceLookupView(candidate = {}, result = null) {
  const stored = candidatePriceView(candidate)
  if (!result) return { ...stored, cached: null, spent: null }
  if (result.status === 'pending') {
    return { family: '', label: '这个候选还没核验出结论', tone: 'muted', prices: stored.prices,
             cached: null, spent: null,
             note: '等这一轮给出了结论，再单独查这个候选的价格。' }
  }
  if (result.status === 'failed') {
    return { family: '', label: '这次查价没有完成', tone: 'warning', prices: stored.prices,
             cached: null, spent: null, note: '可以稍后重试；没有取到价格时不会显示任何金额。' }
  }
  const after = candidatePriceView({ ...candidate, prices: result.prices, price_lookup: result.detail })
  const notes = [after.note]
  if (result.cached) notes.push('这个候选之前已经查过，这次直接用了已有结果，没有新增检索。')
  else notes.push(spentNote(result.spent))
  return { ...after, prices: result.prices, sources: result.sources,
           cached: result.cached, spent: result.spent,
           note: notes.filter(Boolean).join(' ') }
}

/** 要查哪个由路径决定：路径里**只有这一个** candidate_id。 */
export function candidatePricePath(runId, candidateId) {
  return `/research-runs/${encodeURIComponent(String(runId ?? ''))}`
    + `/alternatives/${encodeURIComponent(String(candidateId ?? ''))}/price`
}

/**
 * 请求体恒为空对象。
 *
 * 刻意**不传** ``force``：默认 false 就是"已经查过就直接命中已有结果"，
 * 所以用户点第二次不会重复花钱。候选标识一个字都不放进请求体——要查哪个由路径
 * 决定，请求体里再多一个身份字段就会出现"路径说 A、体里说 B"这种能打架的形状。
 */
export function candidatePriceBody() {
  return {}
}

/**
 * 发一次请求、只发一次（每次调用一个 ``post``），并把响应收成一个结果对象。
 *
 * ``post(path, body)`` 由调用方注入（页面里是 ProcurementPanel 的那个 ``api``），
 * 测试里换成记账用的替身，就能断言"点一次只发一个请求、路径只带一个 candidate_id"。
 */
export async function lookupCandidatePrice({ runId, candidateId, post }) {
  const id = String(candidateId ?? '').trim()
  if (!id) throw new Error('这一条候选没有编号，不能按需查价。')
  if (typeof post !== 'function') throw new Error('查价入口没有接上。')
  const response = await post(candidatePricePath(runId, id), candidatePriceBody())
  const result = priceLookupResult(response, id)
  if (!result) throw new Error('接口返回的不是这个候选的价格，已忽略这一次结果。')
  return result
}

/** 响应 → 结果对象；响应的 ``candidate_id`` 与所点的候选不一致时返回 ``null``。 */
export function priceLookupResult(response, candidateId) {
  const payload = response || {}
  if (String(payload.candidate_id ?? '') !== String(candidateId ?? '')) return null
  const lookup = payload.price_lookup || {}
  return {
    candidateId: String(candidateId),
    status: String(lookup.status || '').trim(),
    cached: Boolean(lookup.cached),
    reasonCode: lookup.reason_code || null,
    detail: lookup.detail && typeof lookup.detail === 'object' ? lookup.detail : {},
    prices: Array.isArray(lookup.prices) ? lookup.prices : [],
    sources: Array.isArray(lookup.sources) ? lookup.sources : [],
    spent: lookup.spent || null,
  }
}

/** 某个候选正在查（``{[candidateId]: true}``）。 */
export function beginLookup(pending = {}, candidateId) {
  const id = String(candidateId ?? '').trim()
  if (!id) return pending
  return { ...pending, [id]: true }
}

export function endLookup(pending = {}, candidateId) {
  const id = String(candidateId ?? '').trim()
  if (!id || !(id in pending)) return pending
  const next = { ...pending }
  delete next[id]
  return next
}

/** 只改这一个候选的条目，其它候选**按引用原样保留**（别的卡片不会跟着变）。 */
export function applyPriceLookup(results = {}, candidateId, result) {
  const id = String(candidateId ?? '').trim()
  if (!id) return results
  return { ...results, [id]: result }
}

/**
 * 「查这个的价格」按钮该是什么状态。
 *
 * 三条：``disabled`` 在**这一个**候选的查价还在进行时、或后端说它的结论还是
 * ``pending`` 时为真；**其它候选的按钮一律不受影响**（用户改主意可以查另一个，
 * 每次只查一个）；没有 ``candidate_id`` 的老数据不给按钮——**不退回数组下标**，
 * 下标会在候选列表变化后指错人。
 */
export function priceButtonState({ candidateId = '', pending = {}, status = '' } = {}) {
  const id = String(candidateId ?? '').trim()
  if (!id) return { disabled: true, label: '查这个的价格',
                    hint: '这一轮数据里没有候选编号，不能按需查价；请重新检索后再查。' }
  if (pending && pending[id]) return { disabled: true, label: '正在查这个候选的价格', hint: '' }
  if (String(status || '') === 'pending') {
    return { disabled: true, label: '查这个的价格',
             hint: '这个候选还没核验出结论，等这一轮出结论后再查。' }
  }
  return { disabled: false, label: '查这个的价格', hint: '' }
}
