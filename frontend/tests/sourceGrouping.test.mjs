/**
 * Q13b「来源按权威性分组」与 Q11「进展不编百分比」两条展示契约。
 *
 * 样例数据（匿名化）：`GET /api/v1/tasks/task-<run-8>/research-runs/research-<id>`
 * 的真实返回体（本地隔离库 output/bio-instrument-acceptance-20260920），
 * 30 条来源 / stage_timings 5 条 / round 2 / usage {search:13,fetch:51,model:3}。
 * 下面的 sourceTypeCensus 就是那份返回体里 source_type 的全量计数，不是抽样。
 */
import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import test from 'node:test'

import {AUTHORITY_GROUPS, SOURCE_GROUPS, groupSources} from '../src/services/sourceGrouping.js'
import {progressFacts} from '../src/services/researchProgress.js'

const listPage = readFileSync(new URL('../src/pages/ResearchResults.jsx', import.meta.url), 'utf8')
// 注释里会解释「原来每行都挂一个『查看已抓取正文』」，断言只看真正跑起来的代码
const listCode = listPage.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')

// 后端 run.sources 里出现过的全部 source_type（sourceLabels 词典就是这几个键）
const ALL_SOURCE_TYPES = ['manufacturer_website', 'manufacturer_download', 'tender_document', 'dealer', 'aggregator', 'unknown']
const sourceTypeCensus = {tender_document: 16, unknown: 13, aggregator: 1}

const source = (source_type, extra = {}) => ({url: `https://example.test/${source_type}/${Math.random()}`, source_type, ...extra})

test('每一个后端 source_type 都会被分到某一组，一个都不丢', () => {
  for (const type of ALL_SOURCE_TYPES) {
    const {classified, total} = groupSources([source(type)])
    assert.equal(classified, 1, `${type} 没有落进任何一组`)
    assert.equal(total, 1)
  }
})

test('将来多出的未知 source_type 落进「非官方来源」组，而不是消失', () => {
  const rows = [source('brand_new_type_from_backend')]
  const {groups, classified} = groupSources(rows)
  assert.equal(classified, 1)
  const trade = groups.find(group => group.key === 'trade')
  assert.equal(trade.rows.length, 1)
  assert.match(trade.title, /非官方来源/)
})

test('分组计数必须等于筛选后的条数（复现任务那次 run 的真实分布）', () => {
  const rows = Object.entries(sourceTypeCensus).flatMap(([type, count]) => Array.from({length: count}, () => source(type)))
  assert.equal(rows.length, 30)
  const {groups, classified, total} = groupSources(rows)
  assert.equal(total, 30)
  assert.equal(classified, 30)
  assert.equal(groups.reduce((sum, group) => sum + group.rows.length, 0), 30)
  assert.equal(groups.find(g => g.key === 'tender').rows.length, 16)
  assert.equal(groups.find(g => g.key === 'manufacturer').rows.length, 0)
  assert.equal(groups.find(g => g.key === 'trade').rows.length, 14)
})

test('普通仪器商 / 门户页面只能进第三组，且标题写明非官方', () => {
  const trade = SOURCE_GROUPS.find(group => group.key === 'trade')
  // 复现任务那次 run 里排在最前面的几条：仪器信息网、忆言医疗、苏州生物安全实验室设备供应商
  // 后端给的 source_type 都是 unknown，阿里云 oss 那份 pdf 是 tender_document。
  for (const s of [source('unknown'), source('aggregator'), source('dealer')]) {
    assert.ok(trade.match(s), `${s.source_type} 不该进采购公告组`)
    assert.ok(!SOURCE_GROUPS.find(g => g.key === 'tender').match(s))
  }
  assert.match(trade.title, /非官方/)
  assert.match(trade.note, /不是厂家确认/)
  assert.match(trade.note, /不能作为成交价/)
})

test('采购公告组只收 tender_document，线索不会被藏起来', () => {
  const tender = SOURCE_GROUPS.find(group => group.key === 'tender')
  assert.ok(tender.match(source('tender_document')))
  assert.ok(!tender.match(source('unknown', {purpose: 'tender'})))
  // purpose=tender 但身份未核实的来源仍然会出现在页面上（在第三组里带「公告身份尚未核实」）
  const {classified} = groupSources([source('unknown', {purpose: 'tender'})])
  assert.equal(classified, 1)
})

test('页面用共享的分组函数，不再各自写一份 match', () => {
  assert.match(listCode, /import \{ groupSources \} from '\.\.\/services\/sourceGrouping'/)
  assert.match(listCode, /groupSources\(filtered\)/)
  assert.ok(!/const sourceGroups = \[/.test(listCode), '页面里又长出一份本地分组表了')
  assert.match(listCode, /grouped\.classified !== grouped\.total/, '少了「分组没吃掉来源」的保护')
})

// ---------------------------------------------------------------------------
// 后端给了 authority 之后：分组必须跟着权威身份走，不能按 source_type 猜
// ---------------------------------------------------------------------------
//
// 真实分布是 ``task-<run-6>`` 的：43 条列出来源里 ``tender_document`` 只占 17，
// 其余 49 条 ``unknown`` 里混着政府采购平台、高校实验室与厂家官网。按 source_type 分
// 会把它们全塞进「产品页与门户线索（非官方来源）」，标题与内容正好相反。

const certified = (authority, source_type, extra = {}) => (
  {url: `https://example.test/${authority}/${source_type}`, authority, source_type, ...extra})

test('权威模式：官方公告与机构站分开，厂家资料单列，最后才是商业线索', () => {
  const rows = [
    certified('official_channel', 'tender_document'),
    certified('official_channel', 'unknown'),
    certified('manufacturer', 'unknown'),
    certified('unlisted', 'unknown'),
    certified('unlisted', 'tender_document'),
  ]
  const {groups, classified, total} = groupSources(rows)
  assert.equal(total, 5)
  assert.equal(classified, 5, '一个都不能丢')
  assert.equal(groups.find(g => g.key === 'tender').rows.length, 1)
  assert.equal(groups.find(g => g.key === 'official').rows.length, 1)
  assert.equal(groups.find(g => g.key === 'manufacturer').rows.length, 1)
  assert.equal(groups.find(g => g.key === 'trade').rows.length, 2)
  assert.equal(groups.reduce((sum, group) => sum + group.rows.length, 0), 5)
})

test('权威模式：机构官网不算"非官方来源"，标题里也不许出现这句话', () => {
  const official = AUTHORITY_GROUPS.find(group => group.key === 'official')
  assert.ok(official.match(certified('official_channel', 'unknown')))
  assert.ok(!official.match(certified('manufacturer', 'unknown')))
  assert.ok(!official.match(certified('unlisted', 'unknown')))
  assert.ok(!/非官方/.test(official.title))
  // 有官方成交记录时，来源清单里这一组的说明也不许出现「转载 / 聚合 / 镜像」这类字眼
  // （用户验收口径：那种情况下页面上聚合站相关内容零出现）。
  for (const word of ['转载', '聚合', '镜像']) {
    assert.ok(!official.note.includes(word), `机构组的说明里出现了「${word}」`)
  }
  // 商业线索那一组仍然是唯一写「非官方来源」的地方。
  const trade = AUTHORITY_GROUPS.find(group => group.key === 'trade')
  assert.match(trade.title, /非官方来源/)
  for (const authority of ['official_channel', 'manufacturer']) {
    assert.ok(!trade.match(certified(authority, 'unknown')), `${authority} 不该进商业线索组`)
  }
})

test('两种分组口径的切换只看"这一批来源有没有 authority"', () => {
  // 老数据（库里已有的 run）没有 authority → 沿用 source_type 口径，行为不变。
  const legacy = groupSources([source('unknown'), source('tender_document')])
  assert.equal(legacy.groups.find(g => g.key === 'trade').rows.length, 1)
  assert.equal(legacy.groups.find(g => g.key === 'manufacturer').rows.length, 0)
  // 新数据全都有 authority → 换权威口径。
  const current = groupSources([certified('manufacturer', 'unknown'), certified('official_channel', 'tender_document')])
  assert.equal(current.groups.find(g => g.key === 'manufacturer').rows.length, 1)
  assert.equal(current.groups.find(g => g.key === 'manufacturer').title, '厂家资料')
})

test('Q13a：每行来源只留一个正文入口，不再写「查看已抓取正文」', () => {
  assert.ok(!listCode.includes('查看已抓取正文'), '「查看已抓取正文」又回到了页面上')
  assert.equal(listCode.split('正文原文').length - 1, 1, '正文入口应该只有一处定义')
  assert.equal(listCode.split('<SourceBody source={s} />').length - 1, 1, '一行来源只能挂一个正文入口')
})

test('Q11：进展只读后端真实字段，进度条读的是检索线条数', () => {
  const run = {
    status: 'running', stage: '核价与复核', round: 2, started_at: '2026-09-24T08:53:00+00:00',
    lanes: {price: 'running', alternatives: 'pending'},
    stage_timings: [
      {name: '第 1 轮', stage: '旧轮次', round: 1, status: 'completed', duration_ms: 17},
      {name: '正文补核', stage: '核价与复核', round: 2, status: 'completed', duration_ms: 303836},
      {name: '同价位复核', stage: '核价与复核', round: 2, status: 'completed', duration_ms: 85900},
    ],
    usage: {search: 13, fetch: 51, model: 3}, limits: {search: 73, fetch: 111, model: 23},
    completed_queries: ['a', 'b', 'c'],
  }
  const facts = progressFacts(run)
  // 只显示本轮：第 1 轮那条不能混进来
  assert.equal(facts.shown.length, 2)
  assert.ok(!facts.shown.some(entry => entry.round === 1))
  assert.equal(facts.done, 0)
  assert.equal(facts.total, 2)
  assert.equal(facts.currentStage, '核价与复核')
  assert.equal(facts.queries, 3)
  assert.equal(facts.counts.find(c => c.key === 'search').used, 13)
  assert.equal(facts.counts.find(c => c.key === 'search').limit, 73)
  assert.deepEqual(facts.lanes.map(l => l.text), ['正在检索', '尚未开始'])
})

test('Q11：没有阶段记录时也不编分母，进度条只按检索线算', () => {
  const facts = progressFacts({status: 'queued', lanes: {price: 'pending', alternatives: 'completed'}})
  assert.equal(facts.shown.length, 0)
  assert.equal(facts.earlierCount, 0)
  assert.equal(facts.done, 1)
  assert.equal(facts.total, 2)
  // 阶段清单读不到就空着，不拿 usage 或检索式条数凑进度
  assert.equal(facts.queries, 0)
  assert.deepEqual(facts.counts.map(c => c.used), [0, 0, 0])
})

test('Q11：没有 limit 时不显示「已用 / 额度」这种假分数', () => {
  const facts = progressFacts({status: 'running', usage: {search: 4}})
  assert.equal(facts.counts.find(c => c.key === 'search').limit, 0)
})
