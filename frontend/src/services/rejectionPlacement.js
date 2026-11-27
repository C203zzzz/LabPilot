/**
 * 拒收记录归位与串链路措辞。
 *
 * 业务口径（``回归基线`` 的 规范项）：「就在报告里，按成交价
 * 和候选品模块划分清楚呗」——一条拒收记录自带 ``model`` 字段，那是**这条核价链路的
 * 目标型号**，不是本次采购目标：
 *
 *   - 成交价链路：``model`` 就是比较基准（本轮采购目标）的型号；
 *   - 候选链路：``model`` 是某个候选的型号——后端核价时 target 用
 *     ``{**baseline, **candidate}``（``procurement_research.py`` 的相似品价格那条路），
 *     所以 target['model'] 是候选型号。
 *
 * 两条链路以前堆在同一节里，于是报告里出现了那句串链路的措辞：「引文中的型号是
 * BSC-1004、MF-52M、HVE-50，与**本次目标型号 LS-50LD** 不一致」——LS-50LD 是候选品，
 * 采购目标是 HVE-50；基线那一轮同一句写的是 LDZH-100KBS，总控真跑的第一轮是 LS-50LD。
 * 逐字同构，说明那是模板问题，不是数据问题。
 *
 * 归位只用记录自带的 ``model``（**不新增字段**）：等于基线型号 → 成交价模块；等于某个
 * 候选型号 → 那个候选块。措辞按归位后的身份改写——历史台账里那句 "本次目标型号" 已经
 * 写死在 ``detail`` 里了，所以这一层必须在展示时改写，不能只靠后端改模板。
 */

/** 型号写法归一：全角、大小写、空白与连字符都不该影响配对（与页面其它配对同一套）。 */
export function normalizeModel(value) {
  return String(value || '').normalize('NFKC').replace(/[\s‐‑–—-]+/g, '').toLowerCase()
}

/** 这条拒收记录属于哪条链路：``price`` / ``candidate`` / ``other`` / ``unattributed``。 */
export function rejectionRole(item, { baselineModel = '', candidateModels = [] } = {}) {
  const owner = normalizeModel(item?.model)
  if (!owner) return 'unattributed'
  if (normalizeModel(baselineModel) === owner) return 'price'
  if ((candidateModels || []).some(model => normalizeModel(model) === owner)) return 'candidate'
  return 'other'
}

/**
 * 按归位分桶：``{price, other, unattributed, byModel}``。
 *
 * ``byModel`` 的键是归一后的型号，值是那条候选的拒收记录。``other`` 是既不等于基线、
 * 也不等于本轮任何候选的型号（早期轮次留下的记录、或候选已从名单里撤掉）——它**不能**
 * 混进成交价那一节当"本次目标"，页面单列一小节并写明它属于哪条核价链路。
 */
export function partitionRejections(rejections, { baselineModel = '', candidateModels = [] } = {}) {
  const price = []
  const other = []
  const unattributed = []
  const byModel = {}
  for (const item of rejections || []) {
    const role = rejectionRole(item, { baselineModel, candidateModels })
    if (role === 'candidate') {
      const key = normalizeModel(item?.model)
      ;(byModel[key] = byModel[key] || []).push(item)
    } else if (role === 'price') {
      price.push(item)
    } else if (role === 'unattributed') {
      unattributed.push(item)
    } else {
      other.push(item)
    }
  }
  return { price, other, unattributed, byModel }
}

//: 型号称呼只有两种（迭代规范 统一称呼，设计规范）：等于比较基准的是
//: 「本次目标型号」，其余（含已撤候选、其它链路）一律「候选型号」。原来同一件事有三种
//: 说法——先是把链路目标写成"本次目标型号"（串链路），迭代规范 又按链路分成两种长称呼，
//: 现在统一成这两种词。
export const TARGET_OWNER_LABEL = '本次目标型号'
export const CANDIDATE_OWNER_LABEL = '候选型号'

//: 每个拒绝原因的共同解释：只在分节里说一次（分组标题下），列表里每条不再重复。
//: 实测洁净台那节连着 12 条都把"同品类的不同型号不能互相认定成交价。"重复一遍。
//: 这份与后端 ``price_rejections.REJECTION_REASON_NOTES`` 逐字相同，
//: ``tests/test_report_claims_and_frontend.py`` 比对两处（同 ``REJECTION_REASON_LABELS``
//: 那套的做法）。
export const REJECTION_REASON_NOTES = {
  model_mismatch: '同品类的不同型号不能互相认定成交价。',
}

export function ownerLabelFor(role) {
  return role === 'price' ? TARGET_OWNER_LABEL : CANDIDATE_OWNER_LABEL
}

/**
 * 列表里那一条短信息（只有引文型号）。
 *
 * 目标型号的称呼不在这里写：行首那句（``rejectionOwnerLabel``）已经在每条记录上印了一次
 * 「候选型号 X」/「本次目标型号 X」，``detail`` 再写一遍就是同一件事两遍（迭代规范 去重复）。
 * 新台账写下的就是短句；历史台账里那句长句在这里归一成同一形状——共同解释交给分组
 * 说明说一次。后端 ``price_rejections.short_rejection_detail`` 是同一处口径的镜像实现
 * （报告正文用）。``role`` 参数保留：调用点按归位传它，这一层不再用它决定文本。
 */
export function rejectionDetail(item, { role = 'other' } = {}) {
  const text = String(item?.detail || '').trim()
  if (!text) return ''
  const mismatch = text.match(/^引文中的型号是\s*(.+?)，与[^，。；]*不一致：(.*)$/)
  if (mismatch) {
    const rest = mismatch[2]
      .replace('同品类的不同型号不能互相认定成交价。', '')
      .replace(/那个价只作同系列参照，不能当成本机（[^）]*）的成交价。/, '该价只作同系列参照。')
      .trim()
    return `引文型号：${mismatch[1].trim()}。${rest}`
  }
  const aggregated = text.match(/^这条来源上共有\s*(\d+)\s*条金额，引文里的型号（([^）]*)）都与[^，。；]*不一致：(.*)$/)
  if (aggregated) {
    const rest = aggregated[3].replace('已按来源聚合为这一条，不再逐条列出。', '已按来源聚合为一条。').trim()
    return `这条来源上共有 ${aggregated[1]} 条金额，引文型号：${aggregated[2]}；${rest}`
  }
  return text
}

/** 记录行首的型号称呼：归位到哪一节，就按那一节的身份称呼这个型号。 */
export function rejectionOwnerLabel(item, { role = 'other' } = {}) {
  const model = String(item?.model || '').trim()
  if (!model) return '型号未标注'
  return `${ownerLabelFor(role)} ${model}`
}

/**
 * 一个原因（分组）的共同解释，只在组标题下印一次。
 *
 * ``model_mismatch`` 这一组额外带上"与谁不一致"——那是每条记录都需要的对照，
 * 而节级说明说不准（同一节里可能混着品牌不符等其它原因）。称呼仍只有两种。
 */
export function rejectionGroupNote(reason, { role = 'other', model = '' } = {}) {
  const code = String(reason || '').trim()
  const note = REJECTION_REASON_NOTES[code] || ''
  if (code !== 'model_mismatch') return note
  const ownerModel = String(model || '').trim()
  const subject = ownerModel ? `${ownerLabelFor(role)} ${ownerModel}` : ownerLabelFor(role)
  const gap = ownerModel ? ' ' : ''
  return `引文写的是别的型号，与${subject}${gap}不一致；${note}`
}

/** 一节要用的标题与说明；分节之后三处的读者预期不一样（成交价 / 候选品 / 其它链路）。 */
export function rejectionSectionCopy(role, { model = '' } = {}) {
  if (role === 'candidate') {
    return {
      title: `该候选品核价时被拒的金额${model ? `（${model}）` : ''}`,
      lead: '这些金额不是本次采购目标的成交价，也不是该候选品的价格依据。',
    }
  }
  if (role === 'other') {
    // 既不是本次采购目标、也不是本轮展示的候选（实测：洁净台那轮 12 条记录属于
    // BCV-3S1，该候选已被 M4 按"没读到配置"撤除）。它不能混进"本次目标"那一节，
    // 也不该和它同名。型号称呼仍然统一成「候选型号」（迭代规范）。
    return {
      title: '其它型号核价时被拒的金额',
      lead: '该型号既不是本次采购目标，也不是本轮展示的候选品（可能已按「没有配置就不展示」撤下）。',
    }
  }
  return {
    title: '为什么这些价格没有被采用',
    lead: '本轮抓到、但没有通过单价核验的金额；它们没有进入成交价比较。',
  }
}
