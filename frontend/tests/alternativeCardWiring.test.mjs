/**
 * 候选卡片与「查这个的价格」按钮的**接线**契约（源码级）。
 *
 * 判读逻辑都在 services 里、由另外两份测试逐条断言；这里只钉"页面确实按那条路走"：
 * 页面自己不许拼接口路径、不许退回数组下标、不许把 ``soft_score`` 的 null 渲染成 0、
 * 不许再出现旧的"品牌未核实"说法（口径是「品牌未解析」，见 ``docs/217`` 与后端
 * ``alternative_report_item`` 的同一个字面）。
 */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8')
const page = read('../src/pages/ResearchResults.jsx')
const panel = read('../src/pages/ProcurementPanel.jsx')
const service = read('../src/services/alternativePriceLookup.js')

test('品牌解析不出来时印「品牌未解析」，不留空', () => {
  assert.ok(!page.includes('品牌未核实'), '旧说法"品牌未核实"还在页面上')
  assert.match(page, /import \{[^}]*candidateTitle[^}]*\} from '\.\.\/services\/alternativePresentation'/)
  assert.match(page, /<h4>\{candidateTitle\(candidate\)\}<\/h4>/)
  // 参考候选那一栏不再自己拼一行标题，而是走**同一张候选卡片**（同一个
  // ``candidateTitle`` 出口，顺带把按需查价与来源块一起带过去）。
  assert.match(page, /run\.reference_candidates\.map\(\(a,i\) => <div className="reference-candidate"[^>]*>.*<Alternative candidate=\{a\}/)
})

test('卡片上确实有逐项差异与三态判断，未知项印的是"正文里没写"', () => {
  assert.match(page, /import \{[^}]*configMatchPresentation[^}]*\}/)
  assert.match(page, /const config = configMatchPresentation\(candidate\)/)
  assert.match(page, /config\.rows\.map\(row =>/)
  assert.match(page, /<td>\{row\.observed\}<\/td>/)
  assert.match(page, /<StatusTag tone=\{row\.tone === 'muted' \? 'neutral' : row\.tone\}>\{row\.stateLabel\}<\/StatusTag>/)
  // 差额只印后端给的那一句。
  assert.match(page, /\{row\.shortfallText && <small className="config-match-note">\{row\.shortfallText\}<\/small>\}/)
  // 未知那句话的字面来源是 service 里的常量，页面不自己写一遍。
  const presentation = read('../src/services/alternativePresentation.js')
  assert.match(presentation, /export const CONFIG_MATCH_UNKNOWN_NOTE = '正文里没写'/)
})

test('soft_score 为 null 时走"未算出"，页面上没有把它兜成 0 的写法', () => {
  assert.match(page, /config\.softScore\.text \|\| '未算出'/)
  assert.match(page, /config\.confidence\.text \|\| '未算出'/)
  assert.ok(!/\|\| 0%?/.test(page), '页面上出现了把空值兜成 0 的写法')
  assert.ok(!/\{config\.softScore\.text\}/.test(page), 'soft_score 直接渲染了，而它可能为 null')
})

test('按钮的 disabled 与文案来自 priceButtonState（按候选分别判断）', () => {
  assert.match(page, /import \{[^}]*priceButtonState[^}]*\} from '\.\.\/services\/alternativePriceLookup'/)
  assert.match(page, /const button = priceButtonState\(\{ candidateId, pending: pendingLookups,/)
  assert.match(page, /disabled=\{button\.disabled\}/)
  assert.match(page, /\{button\.label\}/)
  assert.match(page, /\{button\.hint && <p className="action-hint">\{button\.hint\}<\/p>\}/)
  // 文案（"查这个的价格"）只有一处出口：service。
  assert.match(service, /'查这个的价格'/)
  // 页面不自己拼接口路径，也不直接 fetch。
  assert.ok(!/alternatives\/\$\{/.test(page), '页面自己拼了查价接口路径')
  assert.ok(!page.includes('fetch('), '页面直接发请求了，应当交给 services/api 那一层')
})

test('查价按 candidate_id 定位，不退回数组下标', () => {
  assert.match(page, /const candidateId = String\(candidate\.candidate_id \|\| ''\)\.trim\(\)/)
  assert.match(page, /onLookupPrice\(candidateId\)/)
  assert.match(page, /applyPriceLookup\(current, candidateId, result\)/)
  assert.match(page, /beginLookup\(current, candidateId\)/)
  assert.match(page, /endLookup\(current, candidateId\)/)
  // 下标不许出现在查价这条路上。
  assert.ok(!/onLookupPrice\(i\)/.test(page))
  assert.ok(!/lookupResults\[i\]/.test(page))
})

test('每张候选卡片都拿到同一份结果表，点一个不会牵动别的卡片', () => {
  const usages = page.match(/\{\.\.\.priceCards\}/g) || []
  // 跨品牌那一组有两处（本组代表 + 「另外 N 个型号」），原品牌一处，品牌不全一处，
  // 参考候选一处（参考候选也走同一张卡片，所以它同样要接上这份结果表）。
  assert.equal(usages.length, 5, '四处候选清单、五个渲染点都要接上同一份结果表')
  assert.match(page, /const priceCards = \{ lookupResults, pendingLookups, onLookupPrice: lookupPrice \}/)
  assert.match(page, /const \[lookupResults, setLookupResults\] = useState\(\{\}\)/)
  assert.match(page, /const \[pendingLookups, setPendingLookups\] = useState\(\{\}\)/)
})

test('ProcurementPanel 只把 candidate_id 交给服务，路径与请求体在服务里拼', () => {
  assert.match(panel, /import \{ lookupCandidatePrice \} from '\.\.\/services\/alternativePriceLookup'/)
  assert.match(panel, /onCandidatePrice=\{candidateId => lookupCandidatePrice\(\{/)
  assert.match(panel, /runId: run\.id,/)
  assert.match(panel, /post: \(path, body\) => api\(taskId, path, body\)/)
  assert.ok(!panel.includes('alternatives/'), '页面外壳自己拼了接口路径，应当只在 service 里拼')
})
