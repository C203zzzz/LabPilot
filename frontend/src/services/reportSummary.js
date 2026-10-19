/**
 * 报告总述的取数与判空。
 *
 * 总述由后端在报告里额外给出一份，契约是：
 *   {headline, facts:[{label,value}], blockers:[...], narrative}
 * 它有三个不可假设的地方：整块可能不存在（老报告里没有这个字段）、
 * narrative 可能不存在（模型不可用时只少这一段）、单条 fact 的 value 可能为空。
 *
 * 所以这里只做一件事：把「真有内容的」挑出来，其余当没有。渲染层拿到的
 * 一定是可以直接印出来的文字，永远不会因为空值印出「未提供」「原文未提取」
 * 这类占位 —— 那正是这次要修的毛病。
 *
 * 沿用 tenderPresentation.js / pricePresentation.js 的做法：判空复用
 * hasDisplayValue，逻辑留在 service 里，好让 node --test 直接测，组件只管排版。
 */
// 这里带扩展名：本文件要能被 node --test 直接加载，Node 的 ESM 解析不做无扩展名补全。
import { hasDisplayValue } from './pricePresentation.js'

/** 总述对象上认得的字段。一个对象至少要有其中一个，才当它是总述。 */
const SUMMARY_KEYS = ['headline', 'facts', 'blockers', 'narrative', 'gaps']

/** 取字符串。空白串、null、undefined、'unknown' 一律算「没有」。 */
function text(value) {
  if (!hasDisplayValue(value)) return ''
  if (typeof value === 'object') return ''
  return String(value).trim()
}

/**
 * 总述可能挂在报告本身，也可能挂在存放报告的记录上。
 * 字段挂在哪一层由后端定，这里逐个试；都不像就当没有。
 */
export function summaryObject(payload) {
  if (!payload || typeof payload !== 'object') return null
  const candidates = [
    payload.summary,
    payload.report && payload.report.summary,
    payload.report_summary,
    payload.snapshot && payload.snapshot.summary,
  ]
  return candidates.find(item => isSummaryShape(item)) || null
}

function isSummaryShape(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  return SUMMARY_KEYS.some(key => key in value)
}

/** facts 里能印出来的行：label 和 value 都得有内容。 */
export function summaryFacts(facts) {
  if (!Array.isArray(facts)) return []
  const rows = []
  for (const fact of facts) {
    if (!fact || typeof fact !== 'object') continue
    const label = text(fact.label)
    const value = text(fact.value)
    if (!label || !value) continue
    rows.push({ label, value })
  }
  return rows
}

/** blockers（缺口的同义字段）：空行、重复行、非文本项全部去掉。 */
export function summaryBlockers(blockers) {
  if (!Array.isArray(blockers)) return []
  const lines = blockers.map(text).filter(Boolean)
  return [...new Set(lines)]
}

/**
 * 把后端给的总述归一化。整块没有一句话可印时返回 null，调用方据此不渲染。
 * 返回对象里的字段一定非空，渲染层不需要再判一次。
 */
export function normalizeSummary(raw) {
  if (!isSummaryShape(raw)) return null
  const headline = text(raw.headline)
  const narrative = text(raw.narrative)
  const facts = summaryFacts(raw.facts)
  const blockers = summaryBlockers(Array.isArray(raw.blockers) ? raw.blockers : raw.gaps)
  if (!headline && !narrative && !facts.length && !blockers.length) return null
  return { headline, narrative, facts, blockers }
}

/**
 * 总述里真正有内容、该渲染的块，顺序即页面顺序：结论 → 简述 → 事实 → 缺口。
 * 测试用它断言「没有的块不会留下空壳」。
 */
export function summaryBlocks(summary) {
  if (!summary) return []
  return ['headline', 'narrative', 'facts', 'blockers'].filter(key => {
    const value = summary[key]
    return Array.isArray(value) ? value.length > 0 : Boolean(value)
  })
}

export function reportSummary(payload) {
  return normalizeSummary(summaryObject(payload))
}

/**
 * 真正交给页面渲染的文字：每一段都先过一遍公开翻译层。
 *
 * translate 由调用方注入（组件传的是 publicText + taskState），这样一来这段
 * 逻辑不依赖 React，而「模型写进句子里的 unknown、run 数据里的内部 ID 都不会
 * 漏给用户」这件事可以被 node --test 直接验证。
 * （publicText 的词典没有覆盖任意 snake_case 字段名，例如 evidence_grade；
 *   那一类词属于后端不该写进给人看的文字里，见 docs/66。）
 */
export function reportSummaryView(summary, translate = value => value) {
  const blocks = summaryBlocks(summary)
  if (!blocks.length) return null
  return {
    blocks,
    headline: translate(summary.headline),
    narrative: translate(summary.narrative),
    facts: summary.facts.map(fact => ({ label: translate(fact.label), value: translate(fact.value) })),
    blockers: summary.blockers.map(item => translate(item)),
  }
}
