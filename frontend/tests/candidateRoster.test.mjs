/**
 * 候选清单：合并、排序、关注参数加粗（迭代规范，设计规范）。
 *
 * 设计规范：**不要区分正式候选和备用候选，全部都归为候选；不要替用户做判断，而是提供
 * 清楚信息；把用户关注的配置参数加粗（比如有效容积），其他配置正常展示。**
 *
 * 这一份测试钉四件事：
 * 1. 关注参数的锚点规则与后端 ``_parameter_hint_terms`` 逐字一致（读后端源码比对——
 *    后端没有把那个函数透传给前端，这里是一份镜像，任何一边改字都会红）；
 * 2. 短锚点按词边界（``L`` 不命中 ``LCD``，``USB`` 不命中 ``USBX``）；
 * 3. 排序：一级「对上关注参数的项数」降序、二级「配置项总数」降序、同分**稳定**；
 * 4. 页面上：一份清单、标题写明台数、没有"正式候选／参考候选／未进入深入核验"字样、
 *    事实标记行在、配置项命中才加粗（``is-watched``）。
 */
import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import test from 'node:test'

import {configListing} from '../src/services/alternativePresentation.js'
import {
  anchorMatches,
  candidateFacts,
  candidateLists,
  matchesAnyAnchor,
  parameterHintTerms,
  rosterEntries,
  watchedAnchors,
} from '../src/services/candidateRoster.js'

const page = readFileSync(new URL('../src/pages/ResearchResults.jsx', import.meta.url), 'utf8')
const backend = readFileSync(new URL('../../backend/services/procurement_rules.py', import.meta.url), 'utf8')

const configCountOf = (candidate, anchors) => configListing(candidate, {anchors}).rows.filter(row => !row.block).length
const watchedCountOf = (candidate, anchors) => configListing(candidate, {anchors}).watchedCount

test('关注参数的锚点规则与后端 _parameter_hint_terms 逐字一致', () => {
  const start = backend.indexOf('def _parameter_hint_terms')
  assert.ok(start > 0, '后端那个函数找不到了，这条镜像测试要跟着改')
  const source = backend.slice(start, backend.indexOf('def _raw_parameter_evidence', start))
  const tuples = [...source.matchAll(/return \(([^)]*)\)/g)]
    .map(match => [...match[1].matchAll(/'([^']+)'/g)].map(item => item[1]))
  assert.equal(tuples.length, 3, '后端那三组关键词的形状变了')
  assert.deepEqual(parameterHintTerms('有效容积'), tuples[0])
  assert.deepEqual(parameterHintTerms('灭菌温度范围'), tuples[1])
  assert.deepEqual(parameterHintTerms('数据导出'), tuples[2])
  // 兜底分支：按分隔符切、长度 > 1（与后端的 ``re.split`` 同一套分隔符）。
  assert.deepEqual(parameterHintTerms('灭菌时间范围'), ['灭菌时间范围'])
  assert.deepEqual(parameterHintTerms('转速/压力'), ['转速', '压力'])
  assert.deepEqual(parameterHintTerms('a、b'), [])
  assert.deepEqual(parameterHintTerms(''), [])
})

test('短锚点按词边界匹配：L 不命中 LCD，USB 不命中 USBX', () => {
  assert.ok(anchorMatches('有效容积 50L', 'L'), '50L 里的 L 要算命中')
  assert.ok(anchorMatches('50 L', 'L'))
  assert.ok(!anchorMatches('LCD 显示屏', 'L'), 'L 不许命中 LCD')
  assert.ok(!anchorMatches('LIGHT', 'L'))
  assert.ok(anchorMatches('USB 接口', 'USB'))
  assert.ok(!anchorMatches('USBX 接口', 'USB'))
  assert.ok(anchorMatches('RS485 通讯', 'RS485'))
  // 中文锚点仍是子串包含（不是词边界）。
  assert.ok(anchorMatches('有效容积', '容积'))
  assert.ok(anchorMatches('灭菌腔容量', '容量'))
  assert.ok(matchesAnyAnchor('内部尺寸 500mm', ['容积', '内箱', '内部尺寸']))
  assert.ok(!matchesAnyAnchor('重量 10kg', ['容积', '容量', 'L']))
  assert.ok(!anchorMatches('有效容积', ''))
})

test('关注参数展开成锚点：去重、保序、跳过空名', () => {
  const anchors = watchedAnchors([{name: '有效容积'}, {name: '  '}, {name: '容量'}, {name: '灭菌温度范围'}])
  assert.deepEqual(anchors.slice(0, 6), ['容积', '容量', '内胆', '内箱', '内部尺寸', 'L'])
  assert.ok(anchors.includes('温度'))
  assert.equal(new Set(anchors).size, anchors.length, '锚点里有重复')
  assert.deepEqual(watchedAnchors([]), [])
  assert.deepEqual(watchedAnchors(null), [])
})

test('排序：先按对上关注参数的项数，再按配置项总数，同分保持原顺序', () => {
  const anchors = watchedAnchors([{name: '有效容积'}])
  const candidate = (model, names) => ({model, config_match: {items: names.map(name => ({name, observed: {value: 'x'}}))}})
  const a = candidate('A', ['容积'])
  const b = candidate('B', ['容积', '温度范围'])
  const c = candidate('C', ['重量', '功率', '电流'])
  const d = candidate('D', ['容积', '内胆'])
  const e = candidate('E', ['重量'])
  const f = candidate('F', ['长度'])
  const entries = rosterEntries([a, b, c, d, e, f], [], {
    anchors,
    configCount: item => configCountOf(item, anchors),
    watchedCount: item => watchedCountOf(item, anchors),
  })
  assert.deepEqual(entries.map(entry => entry.candidate.model), ['D', 'B', 'A', 'C', 'E', 'F'])
  assert.deepEqual(entries.map(entry => entry.watched), [2, 1, 1, 0, 0, 0])
  assert.deepEqual(entries.map(entry => entry.configCount), [2, 2, 1, 3, 1, 1])
})

test('清单就是两份名单拼起来，原顺序不打乱（两份名单的结构一个字没动）', () => {
  const anchors = watchedAnchors([])
  const alternatives = [{model: 'A1'}, {model: 'A2'}]
  const references = [{model: 'R1'}, {model: 'R2'}]
  const entries = rosterEntries(alternatives, references, {
    anchors,
    isReference: candidate => String(candidate.model).startsWith('R'),
    configCount: () => 0,
    watchedCount: () => 0,
  })
  assert.deepEqual(entries.map(entry => entry.candidate.model), ['A1', 'A2', 'R1', 'R2'])
  assert.deepEqual(entries.map(entry => entry.reference), [false, false, true, true])
  // 一份清单就是两份名单的并集，不丢不重。
  assert.equal(entries.length, alternatives.length + references.length)
})

test('事实标记：查没查价、做没做逐项比对（事实，不是判断）', () => {
  const compared = {config_match: {items: [{name: '容积'}, {name: '温度'}]}}
  assert.equal(candidateFacts({...compared, price_lookup: {status: 'not_requested'}}).line, '未查价 · 已逐项比对 2 项')
  assert.equal(candidateFacts({...compared, price_lookup: {status: 'not_attempted'}}).line, '未查价 · 已逐项比对 2 项')
  assert.equal(candidateFacts({...compared, price_lookup: {status: 'priced'}, prices: [{unit_price: 1}]}).line,
    '已查价 · 1 条价格依据 · 已逐项比对 2 项')
  // 这次点击查出来的结果优先于候选自带的那一份。
  const fresh = candidateFacts({...compared, price_lookup: {status: 'not_requested'}}, {lookup: {status: 'priced', prices: [{}, {}]}})
  assert.equal(fresh.line, '已查价 · 2 条价格依据 · 已逐项比对 2 项')
  assert.equal(candidateFacts({}).line, '未查价 · 未做逐项比对')
  assert.equal(candidateFacts({price_lookup: {status: 'read_without_price'}}).line, '已查价 · 未做逐项比对')
})

test('配置栏：命中关注参数的项加粗，其它项保持正文字重', () => {
  const anchors = watchedAnchors([{name: '有效容积'}])
  const listing = configListing({
    config_match: {items: [
      {name: '容积', observed: {value: '100L'}},
      {name: '额定工作压力', observed: {value: '0.165MPa'}},
      {name: '内胆', observed: {value: '不锈钢'}},
    ]},
  }, {anchors})
  assert.deepEqual(listing.rows.map(row => [row.name, row.watched]), [
    ['容积', true], ['额定工作压力', false], ['内胆', true],
  ])
  assert.equal(listing.watchedCount, 2)
  // 整段资料原文（``block``）不是"一项配置"，永不参与加粗。
  const withText = configListing({configuration: '型号 | BBS-V600\n容积 100L'}, {anchors})
  assert.equal(withText.rows[0].block, true)
  assert.equal(withText.rows[0].watched, false)
  // 没有关注参数时一项都不加粗（页面与默认状态一致）。
  assert.equal(configListing({config_match: {items: [{name: '容积', observed: {value: '1'}}]}}).watchedCount, 0)
})

test('页面：一份候选清单、标题写明台数、没有分层字样、事实行与加粗都接上', () => {
  assert.match(page, /<h3>候选（\{roster\.length\} 台）<\/h3>/)
  // 数据来源（迭代规范 提交四）：后端有合并清单就用它，没有就回退两份名单拼接——
  // 接线钉在这里，退回"只读 alternatives"或"只读 reference_candidates"都会红。
  assert.match(page, /const roster = rosterEntries\(\.\.\.candidateLists\(run\), \{/)
  assert.match(page, /const candidateModels = candidateLists\(run\)\.flat\(\)/)
  assert.match(page, /<p className="alternative-facts-line">\{facts\.line\}<\/p>/)
  assert.match(page, /className=\{row\.watched \? 'is-watched' : undefined\}/)
  assert.match(page, /const anchors = watchedAnchors\(baseline\?\.parameters\)/)
  // 「该型号产品资料的标称参数（非成交配置）」那一块在**候选卡片上下线了**
  // （2026-10-07 设计规范：「没有配置就不会作为候选展示了，直接保留候选配置即可」）。
  // 它要么是空壳、要么和下面「该候选页面写到的配置」重复：59 轮台账里 182 条候选渲染过
  // 这一块，182 条全都有 ``configListing`` 的内容，删掉没有一张卡片变空。
  // **价格记录卡那一处不在这次口径里**（那是这条成交配置的唯一出口），所以组件本身还在，
  // 这条同时钉两头：候选那处没了、价格那处还在。
  assert.ok(!page.includes('ProductSpecCard spec={spec} anchors={anchors}'),
            '候选卡上的「标称参数」块没有下线')
  assert.match(page, /\{spec && <ProductSpecCard spec=\{spec\} \/>\}/)
  // 候选卡片那一处原先靠 ``anchors`` 在参数表里分字重；它下线之后 ``ProductSpecCard``
  // 不再需要 ``anchors``（价格记录卡本来就不传），死参数一并去掉。
  assert.ok(!page.includes('matchesAnyAnchor(row.key, anchors)'),
            '只剩价格记录卡在用这个组件，锚点参数应当已经拆掉')
  // 分层字样与那两个分区一个都不许回来。
  for (const word of ['正式候选', '参考候选', '未进入深入核验', '跨品牌罗列', 'reference-candidate', 'referenceRoster', 'REFERENCE_REASON_LABELS']) {
    assert.ok(!page.includes(word), `页面上还有分层／分区字样：${word}`)
  }
})

test('候选清单的数据来源：优先后端合并清单，读不到回退两份名单拼接', () => {
  const a = {model: 'A'}
  const b = {model: 'B'}
  // 有合并清单就用它（``alternatives`` + ``reference_candidates`` 由后端拼好）。
  assert.deepEqual(candidateLists({candidates: [a, b], alternatives: [], reference_candidates: []}), [[a, b], []])
  // 老数据／旧台账没有这个字段 → 回退到两份名单拼接，顺序与内容一模一样。
  assert.deepEqual(candidateLists({alternatives: [a], reference_candidates: [b]}), [[a], [b]])
  // 后端给空数组也算"有"（那时两份名单同样是空的），不能掉进回退分支。
  assert.deepEqual(candidateLists({candidates: [], alternatives: [a], reference_candidates: []}), [[], []])
  assert.deepEqual(candidateLists({}), [[], []])
  assert.deepEqual(candidateLists(), [[], []])
})

test('两条来源拼出来的清单逐字相同（换数据来源不许改变内容与顺序）', () => {
  const alternatives = [{model: 'A'}, {model: 'B'}]
  const references = [{model: 'C'}]
  const fromLists = rosterEntries(...candidateLists({alternatives, reference_candidates: references}))
  const fromMerged = rosterEntries(...candidateLists({candidates: [...alternatives, ...references]}))
  assert.deepEqual(fromLists.map(row => row.candidate), fromMerged.map(row => row.candidate))
  assert.deepEqual(fromLists.map(row => row.index), fromMerged.map(row => row.index))
})
