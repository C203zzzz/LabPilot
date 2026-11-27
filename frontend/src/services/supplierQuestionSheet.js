/**
 * 缺口 → 可直接发给销售的话术。
 *
 * 业务口径：「**缺口 → 向销售索要清单**：把缺口变成可直接发出去的话术」，点名要
 * 配置、税运、安装培训、保修、交付时间（``回归基线``）。
 *
 * 这一层只做两件事：把**本轮真实缺的条件**挑出来（销售报价里没说 + 成交记录里也缺的
 * 那几项），再拼成一段能直接复制、发给销售的文字。已经说清的项不进问句——同一句话
 * 问两遍，销售会以为系统没读他的回复。数据来源：
 * ``run.baseline`` 的商务字段（``configuration`` / ``tax`` / ``shipping`` /
 * ``installation`` / ``warranty`` / ``service_response`` / ``service_onsite`` /
 * ``delivery``，空或 ``unknown`` 都算没说明）与 ``run.prices[].gaps``。
 */

/** 问句清单：用户点名的五类（配置、税运、安装培训、保修、交付时间）＋售后响应两小项。 */
export const QUESTION_DEFS = [
  { key: 'configuration', label: '配置', question: '主机与选件清单（型号、数量、是否含软件与附件），并注明厂家选配项是否已含在本次报价里' },
  { key: 'tax', label: '税费与运费', question: '报价是否含税（含则写明税率与发票类型）；运费、运输方式与上楼就位费用由谁承担', covers: ['tax', 'shipping'] },
  { key: 'installation', label: '安装培训', question: '是否含安装、调试与操作培训，含几次、多少人' },
  { key: 'warranty', label: '保修', question: '主机与选件的质保期分别是多久，从什么时间起算' },
  { key: 'service_response', label: '报修响应与上门', question: '报修后的响应时间、上门时限，以及人工与差旅费用由谁承担', covers: ['service_response', 'service_onsite'] },
  { key: 'delivery', label: '交付时间', question: '合同生效后的交货期与安装完成时间' },
]

const UNSTATED = new Set(['', 'unknown', '未知', '未说明', 'none', 'null'])

function unstated(value) {
  if (value === null || value === undefined) return true
  return UNSTATED.has(String(value).trim().toLowerCase())
}

function formatAmount(baseline = {}) {
  const amount = baseline.supplier_price
  if (amount === null || amount === undefined || amount === '') return ''
  const number = Number(amount)
  if (!Number.isFinite(number)) return String(amount)
  const currency = baseline.currency === 'CNY' || !baseline.currency ? '元' : String(baseline.currency)
  return `${number.toLocaleString('zh-CN')} ${currency}`
}

/** 本轮缺哪些条件：销售报价里没说明的字段 ∪ 成交记录 ``gaps`` 里点到的字段。 */
export function missingConditionKeys({ baseline = {}, prices = [] } = {}) {
  const keys = new Set()
  for (const def of QUESTION_DEFS) {
    const fields = def.covers || [def.key]
    if (fields.some(field => unstated(baseline?.[field]))) keys.add(def.key)
  }
  const gapFields = new Set((prices || []).flatMap(price => price?.gaps || []))
  for (const def of QUESTION_DEFS) {
    const fields = def.covers || [def.key]
    if (fields.some(field => gapFields.has(field))) keys.add(def.key)
  }
  return keys
}

/**
 * 生成话术。``body`` 是**可直接复制**的整段文字（页面用 ``<pre>`` 原样展示 + 复制按钮）；
 * ``missing`` 是缺项的标签（页面上单独标出来，读者不用读完整段也知道缺哪几样）。
 */
export function supplierQuestionSheet({ baseline = {}, prices = [] } = {}) {
  const model = [baseline?.brand, baseline?.model].filter(Boolean).join(' ').trim() || '这台设备'
  const amount = formatAmount(baseline)
  const quantity = unstated(baseline?.quantity) ? '' : String(baseline.quantity)
  const missingKeys = missingConditionKeys({ baseline, prices })
  const missing = QUESTION_DEFS.filter(def => missingKeys.has(def.key))
  // 一条都不缺时也要给话术：那不是"没有缺口"，而是"本轮销售报价把条件都写了"，
  // 这时该做的是把口径落到合同里（业务口径：销售承诺仍需核实）。
  const asks = missing.length ? missing : QUESTION_DEFS

  const head = `您好，关于${model}${amount ? `（销售报价 ${amount}）` : ''}的供货条件，麻烦按下面几条逐条书面回复，我们好按同一口径核对：`
  const lines = asks.map((def, index) => `${index + 1}. ${def.label}：${def.question}。`)
  const tail = missing.length
    ? '以上几条在本轮资料（销售报价与已检索到的成交记录）里都没有写清，麻烦一并确认。'
    : '本轮销售报价里这些条件已经提到过，麻烦按上面口径再书面确认一遍，以便写进合同。'
  const body = [head, '', ...lines, '', tail].join('\n')

  return {
    model,
    amount,
    quantity,
    missing,
    missingLabels: missing.map(def => def.label),
    body,
  }
}
