/**
 * 候选卡片与「查这个的价格」按钮的**接线**契约（源码级）。
 *
 * 判读逻辑都在 services 里、由另外两份测试逐条断言；这里只钉"页面确实按那条路走"：
 * 页面自己不许拼接口路径、不许退回数组下标、不许把 ``soft_score`` 的 null 渲染成 0、
 * 不许再出现旧的"品牌未核实"说法（口径是「品牌未解析」，见 ``设计规范`` 与后端
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
  // 候选清单合并成一份（迭代规范，设计规范）：参考候选不再是另一栏，而是与
  // ``alternatives`` 拼成同一份 ``roster``，走**同一张候选卡片**（同一个
  // ``candidateTitle`` 出口，顺带把按需查价与来源块一起带过去）。
  assert.match(page, /roster\.map\(\(entry, index\) => <Alternative[^>]*candidate=\{entry\.candidate\}/)
})

test('卡片上的配置栏只罗列读到的原文，不再输出满足／差异／未知三态', () => {
  // 规范项（设计规范）：口径变更写在 src/services/alternativePresentation.js 开头。
  // 这一条钉"页面确实按新口径走"：只摆配置原文与出处，判定与分数一个都不印。
  assert.match(page, /import \{[^}]*configListing[^}]*\}/)
  assert.match(page, /const listing = configListing\(candidate, \{ anchors \}\)/)
  assert.match(page, /listing\.rows\.map\(row =>/)
  assert.match(page, /\{row\.block \? <pre className="config-source-text">\{row\.value\}<\/pre> : row\.value\}/)
  assert.ok(!page.includes('stateLabel'), '页面上还在印"一致／不同／未知"判定标签')
  assert.ok(!page.includes('configMatchPresentation'), '旧的三态差异表还在页面上')
  assert.ok(!page.includes('已核验比例'), '已核验比例还在印')
  assert.ok(!page.includes('数值接近程度'), '数值接近程度还在印')
  assert.ok(!page.includes('compareLabels'), '满足／不满足／缺少证据的映射还在页面上')
  assert.ok(!page.includes('shortfallText'), '差额那句话（判定的一部分）还在印')
  assert.ok(!page.includes('逐项比较参数'), '旧的「逐项比较参数」表还在页面上（值已经并进配置栏）')
  const presentation = read('../src/services/alternativePresentation.js')
  assert.match(presentation, /export function configListing/)
  assert.ok(!presentation.includes('CONFIG_MATCH_UNKNOWN_NOTE'), '三态那句"正文里没写"还留着')
})

test('配置栏的空说明与项数走 service 的同一个出口，页面不自己编百分比', () => {
  assert.match(page, /listing\.missingCount/)
  assert.match(page, /listing\.emptyNote/)
  assert.match(page, /config-match-legend">\{listing\.boundary\}/)
  // 相似度与接近程度这两个百分比字段不再被任何调用点读。
  assert.ok(!page.includes('softScore') && !page.includes('confidence'), '相似度/接近程度还在渲染')
  assert.ok(!/\|\| 0%?/.test(page), '页面上出现了把空值兜成 0 的写法')
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
  // 迭代规范：候选清单合并成一份之后只剩一个渲染点（``roster.map``）。这一份表仍然是
  // 同一份 ``priceCards``：点 A 卡片只让 A 的状态变，B 卡片的 props 一个字没动。
  assert.equal(usages.length, 1, '合并后只有一个渲染点，它要接上同一份结果表')
  assert.match(page, /const priceCards = \{ lookupResults, pendingLookups, onLookupPrice: lookupPrice, rejectionsByModel: rejections\.byModel \}/)
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
