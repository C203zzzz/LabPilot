// 资料分析页的展示口径：型号收敛、证据入口合并、未结构化文本的取舍。
//
// 这一层只做「怎么把后端已经给出的东西说清楚」，不改后端已经做出的判断：
// 候选是不是候选、字段是不是 confirmed，都由 deepseek_analysis 的结果决定。

import { modelKey } from './procurementIdentity.js'

const MODEL_TOKEN = /[A-Za-z0-9]+/g

/**
 * 从用户原话里取出「型号样子」的词。
 *
 * 只看连续 ASCII 字母数字，并要求含数字、长度 >= 3：这样 `1300A2`、`BSC-1300A2`
 * 会被取出来，而 `A2`、`5` 这类容易误命中的片段不会。CJK 文字天然是分隔符，
 * 所以「采购一台知楚安全柜 1300A2，用于细菌分离」只会切出 `1300A2`、`22800`、`48`、`5`。
 */
export function modelTokens(text) {
  const tokens = String(text ?? '').match(MODEL_TOKEN) || []
  return [...new Set(tokens.map(modelKey).filter(token => /\d/.test(token) && token.length >= 3))]
}

/**
 * 本任务里用户自己提供的文字（需求原话 + 已填写的条件），用来判断「用户要的是哪个型号」。
 * 不读资料、不读模型输出——那是被比较的一方。
 */
export function userModelText(requirement, task) {
  const parts = [task?.scenario, requirement?.scenario]
  for (const group of ['hard_constraints', 'soft_constraints']) {
    for (const item of requirement?.[group] || []) {
      parts.push(item?.source_text, typeof item?.value === 'string' ? item.value : '')
    }
  }
  return parts.filter(value => typeof value === 'string' && value.trim()).join('\n')
}

/**
 * 资料的型号是否存在用户原话里的同一个写法。
 *
 * 主规则是「归一化后整词相等」，与后端 procurement_intake.model_identity 的归一一致；
 * 只有型号本身足够长（>= 4 位且含数字）时才退一步允许包含关系，避免 `A2` 匹配上 `1300A2`。
 */
export function matchesUserModel(model, tokens) {
  const key = modelKey(model)
  if (!key || !tokens?.length) return false
  if (tokens.includes(key)) return true
  // 资料写成 `BSC-1300A2`、用户只写 `1300A2`（或反过来）时按包含关系收敛；
  // 两侧都要求 >= 4 位且含数字，避免 `A2`、`130` 这类通用片段误命中 `1300A2`。
  if (key.length < 4 || !/\d/.test(key)) return false
  return tokens.some(token => token.length >= 4 && (token.includes(key) || key.includes(token)))
}

/**
 * 只展示用户点名的型号；一个都匹配不上时原样返回全部候选。
 *
 * 「收敛」不是「过滤到没有」：资料里识别出来的东西必须仍然能被看到，
 * 匹配不上的兄弟型号收进 `others`，由界面折叠一行说明，不静默丢弃。
 */
export function focusCandidates(candidates, userText) {
  const list = Array.isArray(candidates) ? candidates : []
  const empty = { candidates: list, others: [], narrowed: false, models: [], total: list.length }
  const tokens = modelTokens(userText)
  if (!tokens.length) return empty
  const matched = []
  const others = []
  for (const candidate of list) (matchesUserModel(candidate?.model, tokens) ? matched : others).push(candidate)
  if (!matched.length) return empty
  return {
    candidates: matched,
    others,
    narrowed: others.length > 0,
    models: matched.map(candidate => String(candidate?.model ?? '').trim()).filter(Boolean),
    total: list.length,
  }
}

const pageOf = location => {
  const matched = String(location ?? '').match(/page:(\d+)/)
  return matched ? Number(matched[1]) : null
}

/** `page:9/rendered` 是人可读的「第 9 页」，不是「第 9/rendered」。 */
export function evidencePageLabel(location) {
  const page = pageOf(location)
  if (page) return `第 ${page} 页`
  const text = String(location ?? '').trim()
  return text || '页码未定位'
}

/**
 * 同一文档同一页的多个证据合并成一个入口，并记住它代表几处。
 *
 * 合并后仍然按 `document_id` + 页号跳转，所以点开还是能定位到原文；
 * `excerpt` 只是同一页的不同引用片段，不构成第二个入口。
 */
export function mergeEvidence(sources, rawEvidence) {
  const entries = new Map()
  let unlocated = 0
  for (const source of [...(sources || []), ...(rawEvidence || [])]) {
    if (!source) continue
    const unparsed = source.evidence_status === 'unparsed'
    if (source.document_id) {
      const page = pageOf(source.page_or_location)
      const key = `doc:${source.document_id}:${page ?? source.page_or_location ?? ''}`
      const entry = entries.get(key) || { key, kind: 'document', document_id: source.document_id, url: '', location: source.page_or_location, page, count: 0, unparsed: false }
      entry.count += 1
      entry.unparsed = entry.unparsed || unparsed
      entries.set(key, entry)
    } else if (source.url) {
      const key = `url:${source.url}`
      const entry = entries.get(key) || { key, kind: 'url', document_id: '', url: source.url, location: source.page_or_location, page: null, count: 0, unparsed: false }
      entry.count += 1
      entry.unparsed = entry.unparsed || unparsed
      entries.set(key, entry)
    } else if (source.excerpt) {
      // 有正文却没有文档或网址：不能变成可点入口，但也不能当作没有证据。
      unlocated += 1
    }
  }
  return { entries: [...entries.values()], unlocated }
}

/** 一眼看出这段值是「表格里扒出来的整段原文」而不是一个参数值。 */
const looksLikeRawTable = value => value.includes('|') || value.includes('\n')

const splitPairs = text => {
  const segments = String(text).split(/[；;\n]+/).map(segment => segment.trim()).filter(Boolean)
  const rows = []
  for (const segment of segments) {
    const at = segment.search(/[：:]/)
    if (at <= 0) return null
    rows.push({ label: segment.slice(0, at).trim(), value: segment.slice(at + 1).trim() })
  }
  return rows.length ? rows : null
}

/**
 * 把 `unparsed_values` 拆成一行一条；拆不干净的部分不展示。
 *
 * `unparsed_values` 是「已取得原文但没完成结构化」的值，按硬约束不能当成已确认参数。
 * 真实数据里它是 `{键: 值}` 对象（本任务 13 处全是对象），可以确定性地一行一条；
 * 值里带 `|` 或换行说明它是一整段表格文本，归不到某一个参数名下，就丢弃不展示，
 * 用户仍可通过同一字段的「查看原始资料」回到原文。
 */
export function structuredUnparsedRows(unparsed) {
  const rows = []
  let dropped = 0
  const push = (label, value) => {
    const cleanLabel = String(label ?? '').trim()
    const cleanValue = ['string', 'number'].includes(typeof value) ? String(value).trim() : ''
    if (!cleanLabel || !cleanValue || looksLikeRawTable(cleanValue)) { dropped += 1; return }
    rows.push({ label: cleanLabel, value: cleanValue })
  }
  if (unparsed && typeof unparsed === 'object' && !Array.isArray(unparsed)) {
    for (const [label, value] of Object.entries(unparsed)) {
      if (value && typeof value === 'object' && !Array.isArray(value)) {
        const inner = Object.entries(value)
        if (!inner.length) { dropped += 1; continue }
        for (const [nested, nestedValue] of inner) push(`${label}·${nested}`, nestedValue)
      } else if (Array.isArray(value)) {
        push(label, value.filter(item => ['string', 'number'].includes(typeof item)).join('、'))
      } else {
        push(label, value)
      }
    }
    return { rows, dropped }
  }
  if (typeof unparsed === 'string' && unparsed.trim()) {
    const pairs = splitPairs(unparsed)
    if (!pairs) return { rows: [], dropped: 1 }
    for (const pair of pairs) push(pair.label, pair.value)
    return { rows, dropped }
  }
  return { rows: [], dropped: unparsed ? 1 : 0 }
}
