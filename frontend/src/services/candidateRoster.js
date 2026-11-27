/**
 * 候选清单：合并、排序、关注参数加粗（设计规范）。
 *
 * 设计规范：**不要区分正式候选和备用候选，全部都归为候选；不要替用户做判断，而是
 * 提供清楚信息；把用户关注的配置参数加粗（比如有效容积），其他配置正常展示。**
 *
 * 因此这一层做三件事，**一件判据都不改**：
 *
 * 1. ``rosterEntries``：把 ``alternatives`` 与 ``reference_candidates`` 拼成一份清单
 *    （底层两份名单的结构、``candidate_id``、撤除逻辑都不动，价格查询仍按原键走）。
 * 2. 排序：一级「与用户关注参数对上的项数」降序，二级「配置项总数」降序，同分**稳定**
 *    保持原顺序。不写"顺序不代表推荐"这类说明——排序规则本身不替用户下结论，只是把
 *    用户关心的东西排到前面。
 * 3. ``parameterHintTerms``：把关注参数名展开成锚点，命中锚点的配置项在页面上加粗。
 *    锚点规则**逐字照抄**后端 ``backend/services/procurement_rules.py`` 的
 *    ``_parameter_hint_terms``（那份函数是后端做"原文兜底"时用的）。后端没有把它透传给
 *    前端，所以这里是一份镜像实现，``tests/parameterAnchors.test.mjs`` 读后端源码比对，
 *    任何一边改字都会红。**只加粗，不上色、不打勾、不写满足与否。**
 *
 * 短锚点（``L``、``USB``、``RS485``）按词边界匹配：``L`` 不许命中 ``LCD``。
 */

/** 纯 ASCII 字母数字的锚点走词边界，其余（中文等）走子串包含。 */
const ASCII_ANCHOR = /^[A-Za-z0-9]+$/

/**
 * 关注参数名 → 文本锚点。与后端 ``_parameter_hint_terms`` 一一对应：
 *
 * ```python
 * if any(token in label for token in ('容积', '容量', '体积')):
 *     return ('容积', '容量', '内胆', '内箱', '内部尺寸', 'L')
 * if '温度' in label or '温区' in label or '控温' in label:
 *     return ('温度范围', '控温范围', '温区', '温度')
 * if any(token in label for token in ('记录', '导出', '数据', '打印', '留档')):
 *     return ('记录', '导出', '数据', '打印', 'USB', 'RS485', '通讯', '接口')
 * return tuple(token for token in re.split(r'[\s、，,;/]+', label) if len(token) > 1)
 * ```
 */
export function parameterHintTerms(name) {
  const label = String(name || '').normalize('NFKC')
  if (['容积', '容量', '体积'].some(token => label.includes(token))) {
    return ['容积', '容量', '内胆', '内箱', '内部尺寸', 'L']
  }
  if (['温度', '温区', '控温'].some(token => label.includes(token))) {
    return ['温度范围', '控温范围', '温区', '温度']
  }
  if (['记录', '导出', '数据', '打印', '留档'].some(token => label.includes(token))) {
    return ['记录', '导出', '数据', '打印', 'USB', 'RS485', '通讯', '接口']
  }
  return label.split(/[\s、，,;/]+/).filter(token => token.length > 1)
}

/**
 * 一个文本命中一个锚点吗。
 *
 * 纯 ASCII 锚点（``L`` / ``USB`` / ``RS485``）按**字母边界**匹配：两侧不许再是字母。
 * 数字不算边界（``50L``、``RS485`` 里的单位与型号都要命中），但 ``LCD`` 里的 ``L``
 * 和 ``USBX`` 里的 ``USB`` 不算——那正是用户点名要防的误命中。
 */
export function anchorMatches(text, anchor) {
  const haystack = String(text || '').normalize('NFKC')
  const needle = String(anchor || '').normalize('NFKC').trim()
  if (!needle) return false
  if (ASCII_ANCHOR.test(needle)) {
    const escaped = needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    return new RegExp(`(^|[^A-Za-z])${escaped}([^A-Za-z]|$)`, 'i').test(haystack)
  }
  return haystack.toLowerCase().includes(needle.toLowerCase())
}

/** 文本命中任一锚点吗。 */
export function matchesAnyAnchor(text, anchors = []) {
  return (anchors || []).some(anchor => anchorMatches(text, anchor))
}

/**
 * ``run['baseline']['parameters']`` 的每一个参数名 → 全部锚点（去重、保序）。
 *
 * 参数结构就是比较基准那一份：``[{'name': '有效容积', 'value': 50, 'unit': '升'}, …]``。
 * 名字缺失的项跳过（不猜）。
 */
export function watchedAnchors(parameters = []) {
  const anchors = []
  const seen = new Set()
  for (const parameter of Array.isArray(parameters) ? parameters : []) {
    const name = String((parameter || {}).name || '').trim()
    if (!name) continue
    for (const anchor of parameterHintTerms(name)) {
      const key = anchor.toLowerCase()
      if (seen.has(key)) continue
      seen.add(key)
      anchors.push(anchor)
    }
  }
  return anchors
}

/**
 * 候选清单的**数据来源**（迭代规范 提交四，设计规范：「后端代码也别再区分了」的第一步）。
 *
 * 优先读后端给的合并清单 ``run.candidates``（``alternatives`` 在前、
 * ``reference_candidates`` 在后，与今天的顺序逐字一致）；读不到时**回退**到"两份名单拼接"
 * ——历史 run 与旧台账里没有这个字段，回退之后页面看到的东西一个字不变。
 *
 * 两条路给的都是同一批候选对象、同一个顺序，所以**展示内容与顺序不许有差别**：这一步只是
 * 换数据来源。每个条目上多一个后端写的 ``verified``（走过逐项对齐或查过价），页面今天还
 * 不读它（那行事实标记仍由 ``candidateFacts`` 按同一口径现算）。
 */
export function candidateLists(run = {}) {
  const source = run || {}
  if (Array.isArray(source.candidates)) return [source.candidates, []]
  return [source.alternatives || [], source.reference_candidates || []]
}

/**
 * 合并后的候选清单（``alternatives`` 在前、``reference_candidates`` 在后，与原顺序一致）。
 *
 * ``reference`` 标记沿用页面原有的品牌判据（同品牌才算"原品牌参考"），它只影响卡片上
 * 那一行标签，不影响排序、不影响名单。
 */
export function rosterEntries(alternatives = [], referenceCandidates = [], {
  isReference = () => false,
  parameters = [],
  anchors = null,
  configCount = () => 0,
  watchedCount = () => 0,
} = {}) {
  const list = [...(alternatives || []), ...(referenceCandidates || [])]
  const resolvedAnchors = anchors || watchedAnchors(parameters)
  return list
    .map((candidate, index) => ({
      candidate,
      reference: Boolean(isReference(candidate)),
      index,
      watched: watchedCount(candidate, resolvedAnchors),
      configCount: configCount(candidate),
    }))
    .sort((left, right) => (right.watched - left.watched)
      || (right.configCount - left.configCount)
      || (left.index - right.index))
}

/**
 * 卡片上那行事实标记：查没查过价、做没做过逐项比对。
 *
 * 这是**事实**不是判断（设计规范：不许替用户做判断，但也不许把事实抹平）。
 * ``lookup`` 是这次点击查价的结果（没有就用候选自带的 ``price_lookup``）。
 */
export function candidateFacts(candidate = {}, { lookup = null } = {}) {
  const item = candidate || {}
  const prices = Array.isArray(lookup?.prices) ? lookup.prices : (Array.isArray(item.prices) ? item.prices : [])
  const status = String((lookup?.status ?? item.price_lookup?.status) || '').trim()
  // "没查"只有两种字面：not_requested（等用户点按钮）与 not_attempted（本轮没查）。
  // 空 status 且没有价格，同样算没查——老数据不给结论，也不假装查过。
  const queried = Boolean(prices.length) || (status !== '' && status !== 'not_requested' && status !== 'not_attempted')
  const items = (item.config_match || {}).items
  const compared = Array.isArray(items) ? items.length : 0
  const parts = [
    queried ? (prices.length ? `已查价 · ${prices.length} 条价格依据` : '已查价') : '未查价',
    compared ? `已逐项比对 ${compared} 项` : '未做逐项比对',
  ]
  return { queried, priceCount: prices.length, compared, comparedCount: compared, line: parts.join(' · ') }
}
