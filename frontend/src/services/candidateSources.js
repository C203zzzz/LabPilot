/**
 * 候选卡尾部的「来源」那一块：**照成交价卡片那份来源块做**，不另造一套样式。
 *
 * 负责人批注（原话）：「需要提供来源网页 就跟查询成交价一样 且前端页面也要做成相似的效果」。
 * "相似"落在三处，都在这里定：
 *
 * 1. **形状相似**：和 ``pricePresentation.priceSourceLinks`` 一样，后端给**列表**、
 *    前端一条一行（一条候选可能对应多个来源页），不是只取第一条；
 * 2. **标注相似**：每条链接有自己的中文标签（那边是「价格来源 / 配置来源」，这边是
 *    「发现来源 / 产品详情页 / 品牌依据 / 配置依据 / 参数依据」——由后端
 *    ``source_pages[].origin`` 决定），链接文字仍是"指向哪里"的简述
 *    （``sourceSummary``：站点名 · 页面类型），页面上不出现 URL；
 * 3. **主来源**：``source_pages[].primary`` 的那一条排在最前面并标「主来源」——
 *    它就是"这条候选是从哪个网页看出来的"的直接答案。主来源由后端唯一决定
 *    （发现这条候选的那一页），前端不猜。
 *
 * 老数据（没有 ``source_pages`` 字段的那批 run）退回候选自己的 ``url``：那时它就是
 * 唯一的来源页，语义与 ``discovered`` 相同。**不因为缺字段就不给来源**——用户口径是
 * "点开一条候选能看到它的来源网页"。
 *
 * 价格来源（``candidate.prices[].url``）**不在这里**：它在候选卡片的价格区已经有
 * 自己那一块（``PriceSources``），同一件事不在两个出口各说一遍。
 */
import { sourceSummary } from './siteNames.js'

/** 与后端 ``procurement_research.CANDIDATE_SOURCE_ORIGINS`` 一一对应。 */
export const CANDIDATE_SOURCE_ORIGINS = ['discovered', 'detail_page', 'brand_evidence',
  'config_match', 'matrix']

//: ``origin`` → 这一行的标签与页面类型（``SourceLink`` 的 ``page``）。
//: 认不出来的 origin 给中性标签，不回显那个内部代号。
const ORIGIN_PRESENTATION = {
  discovered: { label: '发现来源', page: '网页' },
  detail_page: { label: '产品详情页', page: '产品页' },
  brand_evidence: { label: '品牌依据', page: '产品页' },
  config_match: { label: '配置依据', page: '产品页' },
  matrix: { label: '参数依据', page: '产品页' },
}
const UNKNOWN_ORIGIN = { label: '来源', page: '' }

/**
 * 候选卡尾部要渲染的那几行（纯数据，不起浏览器就能断言）。
 *
 * 返回项的字段：``key``（React key，就是网址）、``url``（真实地址，仍在 ``href`` 里）、
 * ``label``（``<dt>`` 上的中文标签）、``page``（页面类型）、``text``（链接文字）、
 * ``primary``（是不是主来源）。空地址与重复网址不产出行；主来源排在最前面。
 */
export function candidateSourceEntries(candidate = {}) {
  const item = candidate || {}
  const stored = Array.isArray(item.source_pages) ? item.source_pages : []
  const rows = stored.length ? stored
    : (String(item.url || '').trim() ? [{ url: item.url, origin: 'discovered', primary: true }] : [])
  const entries = []
  const seen = new Set()
  for (const row of rows) {
    const url = String((row || {}).url || '').trim()
    if (!url || seen.has(url)) continue
    seen.add(url)
    const origin = String((row || {}).origin || '')
    const view = ORIGIN_PRESENTATION[origin] || UNKNOWN_ORIGIN
    entries.push({ key: url, url, label: view.label, page: view.page,
                   text: sourceSummary(url, view.page), primary: Boolean((row || {}).primary) })
  }
  // 主来源在前；其余保持后端给的顺序（发现 → 详情页 → 品牌 → 配置 → 参数），
  // 那个顺序就是"这条候选被一步步看清"的顺序，重排会让读的人失去线索。
  return [...entries.filter(entry => entry.primary), ...entries.filter(entry => !entry.primary)]
}
