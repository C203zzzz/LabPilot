/**
 * 招标文件要求的取数逻辑。
 *
 * 单独放在 service 里，是为了让「哪几条算关键条款」「某个型号对应哪台设备」
 * 以及「页面上会不会出现内部字段名或诊断码」这几件事能被 node --test 直接测，
 * 不必起浏览器。组件只负责排版。
 */
export const INTERNAL_TERMS = [
  'evidence_type', 'evidence_level', 'inferred_compliance', 'tender_requirement',
  'table_rows', 'heading_line', 'binding_chain', 'project_level', 'requirement_source',
  'line_start', 'line_end', 'tender_requirements',
  'no_official_source', 'empty_results', 'tender_body_unverified', 'tls_', 'provider_',
]

export function findLeaks(text) {
  const haystack = String(text ?? '')
  return INTERNAL_TERMS.filter(term => haystack.includes(term))
}

/** ▲ 关键条款与一般条款分开：前者直接列出，后者折叠。 */
export function splitTenderClauses(requirements) {
  const rows = requirements || []
  return {key: rows.filter(row => row.key), plain: rows.filter(row => !row.key)}
}

/** 与价格区同一套归一化：全角、大小写、分隔符差异不该影响型号配对。 */
export function identity(value) {
  return String(value || '').normalize('NFKC').replace(/[\s‐‑–—－-]+/g, '').toLowerCase()
}

/**
 * 招标文件里与这个型号对应的那台设备。
 *
 * 判据只有一条：设备的中标型号与给定型号归一化后相等。这是数据里唯一能把
 * 「招标文件里的某台设备」和「用户手上这台产品」硬对上的字段。对不上就不返回，
 * 宁可什么都不显示，也不把别的产品的配置要求挂到这张卡片上。
 */
export function tenderDeviceForModel(data, model) {
  const wanted = identity(model)
  if (!wanted) return null
  for (const item of data?.packages || []) {
    for (const device of item.devices || []) {
      if (identity(device.award?.model) === wanted) return device
    }
  }
  return null
}

/** 这份招标文件的出处，用于在卡片上注明要求来自哪里。 */
export function tenderSource(data) {
  const document = (data?.documents || [])[0] || {}
  return {
    title: document.title || '',
    chapter: document.chapter || '',
    url: document.url || '',
    parentUrl: document.parent_url || '',
    nature: data?.nature || '',
  }
}

/** 组件会印到页面上的全部数据字符串，供泄漏检查直接扫描。 */
export function displayTexts(data) {
  if (!data) return []
  const source = tenderSource(data)
  const texts = [data.nature, source.title, source.chapter, ...(data.notes || [])]
  for (const item of data.packages || []) {
    texts.push(item.package, item.budget?.text)
    for (const device of item.devices || []) {
      texts.push(device.name, device.title, device.award?.brand, device.award?.model, device.award?.quantity)
      for (const row of device.requirements || []) texts.push(row.clause, row.statement, row.text)
      for (const step of device.binding_chain || []) texts.push(step.evidence)
    }
  }
  for (const item of data.project_level || []) {
    texts.push(item.name, item.title, item.reason)
    for (const row of item.requirements || []) texts.push(row.clause, row.statement, row.text)
  }
  return texts.filter(value => value !== null && value !== undefined).map(String)
}
