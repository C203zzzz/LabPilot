/**
 * 候选卡片：``config_match.items`` 的逐项差异（一致／不同／未知三态）。
 *
 * 口径来自 ``docs/HANDOVER-alt-two-stage.md`` 第三节（负责人 2026-09-26）：
 * **一项一行给三态，不给单一百分比；未知要显式写成"正文里没写"**——
 * 后端 ``research_planning.configuration_alignment`` 的 docstring 写得很直白：
 * "把没查到当成满足是撒谎，当成不满足则永远找不到替代"。
 *
 * 下面这些 fixture 逐字照抄真跑产物 ``output/alt-extract/live-extract5.json``
 * （H1650K 那一条：转速 16500 r/min 对要求 20000 r/min 判 violated；
 * 最大相对离心力 26054 xg 对 25000 xg 判 satisfied；类型 判 unknown、observed=null），
 * 只把长正文截短——字段名一个都没改。
 */
import assert from 'node:assert/strict'
import test from 'node:test'

import {
  BRAND_UNRESOLVED,
  candidateBrandLabel,
  candidateTitle,
  configMatchPresentation,
  configMatchRows,
} from '../src/services/alternativePresentation.js'

const DETAIL = 'https://www.yiqi.com/product/detail_1000002.html'

const differentItem = {
  name: '最高转速',
  required: { name: '最高转速', value: 20000, unit: 'r/min', direction: 'min' },
  observed: {
    value: 16500.0, unit: 'r/min', conditions: '',
    evidence: { url: DETAIL, excerpt: '[表格按列展开 表 2 列] H1650K 上限转速 16500r/min' },
  },
  status: 'violated',
  kind: 'different',
}

const matchItem = {
  name: '最大相对离心力',
  required: { name: '最大相对离心力', value: 25000, unit: '×g', direction: 'min' },
  observed: {
    value: 26054.0, unit: 'xg', conditions: '',
    evidence: { url: DETAIL, excerpt: '[表格按列展开 表 2 列] H1650 上限相对离心力 26054xg' },
  },
  status: 'satisfied',
  kind: 'match',
}

const unknownItem = {
  name: '类型',
  required: { name: '类型', value: '高速冷冻离心机', direction: 'equal' },
  observed: null,
  status: 'unknown',
  kind: 'unknown',
}

const candidate = (extra = {}) => ({
  candidate_id: '9a8b7c6d5e4f3021',
  brand: '',
  model: 'H1650K',
  url: DETAIL,
  excerpt: '高速离心机-湘仪H1650K台式微量高速离心机',
  config_match: {
    score: 0.5,
    items: [differentItem, matchItem, unknownItem],
    diff_points: ['最高转速：要求 20000 r/min，正文查得 16500 r/min，不满足。'],
    hard_failures: ['最高转速'],
    soft_score: 0.959545559223152,
    confidence: 0.6666666666666666,
    basis: 'compared',
  },
  price_lookup: { status: 'not_requested', reason_code: 'not_requested' },
  ...extra,
})

test('三态各一例：一致／不同／未知各给一个说法，不是单一百分比', () => {
  const rows = configMatchRows(candidate())
  assert.equal(rows.length, 3)
  assert.equal(rows[0].state, 'different')
  assert.equal(rows[0].stateLabel, '不同')
  assert.equal(rows[1].state, 'match')
  assert.equal(rows[1].stateLabel, '一致')
  assert.equal(rows[2].state, 'unknown')
  assert.equal(rows[2].stateLabel, '未知')
  // 要求与查得两边都印出来（逐项列的意义所在），并要求写法带方向词。
  assert.equal(rows[0].required, '不低于 20000 r/min')
  assert.equal(rows[0].observed, '16500 r/min')
  assert.equal(rows[1].observed, '26054 xg')
})

test('未知不能被显示成满足：字面是"正文里没写"，且任何正向判断都与它无关', () => {
  const rows = configMatchRows(candidate())
  const unknown = rows.find(row => row.state === 'unknown')
  assert.equal(unknown.observed, '正文里没写')
  assert.equal(unknown.stateNote, '正文里没写')
  assert.equal(unknown.satisfied, false)
  assert.equal(unknown.stateLabel, '未知')
  assert.ok(!/满足|一致|符合|达标/.test(unknown.stateLabel + unknown.observed))
  // 三行里只有真正一致的那一行算满足。
  assert.deepEqual(rows.map(row => row.satisfied), [false, true, false])
  const counts = configMatchPresentation(candidate()).counts
  assert.deepEqual(counts, { match: 1, different: 1, unknown: 1 })
})

test('认不出来的项默认落"未知"，不落"满足"', () => {
  const rows = configMatchRows({ config_match: { items: [
    { name: '奇怪的项', required: { name: '奇怪的项', value: 'x' }, observed: { value: 'x', unit: '' } },
    { name: '状态认不出', required: { name: '状态认不出', value: 1 }, observed: { value: 1 }, status: 'whatever', kind: 'nope' },
  ] } })
  assert.deepEqual(rows.map(row => row.state), ['unknown', 'unknown'])
  assert.deepEqual(rows.map(row => row.satisfied), [false, false])
})

test('差额只印后端给的 shortfall_text；没给就留空，前端不按百分比自己编', () => {
  const withText = configMatchRows(candidate({ config_match: {
    ...candidate().config_match,
    items: [{ ...differentItem, shortfall_text: '差额：实读比要求低 17.5%（以要求值为基准）。' }, matchItem, unknownItem],
  } }))
  assert.equal(withText[0].shortfallText, '差额：实读比要求低 17.5%（以要求值为基准）。')

  // 后端只给了结构化的 shortfall（``{comparable, percent, side}``）、没挂那一句话时：
  // 卡片上留空，**不许前端按 percent 拼一句**出来。
  const structuredOnly = candidate({ config_match: {
    ...candidate().config_match,
    items: [{ ...differentItem, shortfall: { comparable: true, percent: 17.5, side: 'below', basis: 'requirement_value' } }, matchItem, unknownItem],
  } })
  const rows = configMatchRows(structuredOnly)
  assert.equal(rows[0].shortfallText, '')
  assert.ok(!JSON.stringify(rows).includes('17.5'), '前端自己算差额了')
  assert.ok(!JSON.stringify(rows).includes('差额'))
  // 一致项与未知项上永远不出现差额。
  assert.deepEqual(rows.map(row => row.shortfallText), ['', '', ''])
})

test('来源链接与原文行逐项给出来（不印网址本身）', () => {
  const rows = configMatchRows(candidate())
  assert.equal(rows[0].evidenceUrl, DETAIL)
  assert.match(rows[0].excerpt, /16500r\/min/)
  assert.equal(rows[2].evidenceUrl, '')
  assert.equal(rows[2].excerpt, '')
  // 卡片自己那条发现原话也要有（``candidate.excerpt``）。
  assert.match(candidate().excerpt, /H1650K/)
})

test('置信度与接近程度：null 时不显示 0，只说明为什么算不出', () => {
  const full = configMatchPresentation(candidate())
  assert.equal(full.confidence.text, '67%')
  assert.equal(full.confidence.note, '2 / 3 项有可核验的值。')
  assert.equal(full.softScore.text, '96%')

  const nothingComparable = configMatchPresentation(candidate({ config_match: {
    ...candidate().config_match, confidence: null, soft_score: null, basis: 'no_confirmed_configuration',
  } }))
  assert.equal(nothingComparable.confidence.text, '')
  assert.equal(nothingComparable.softScore.text, '')
  assert.ok(!nothingComparable.confidence.note.includes('0%'))
  assert.match(nothingComparable.confidence.note, /没有确认配置要求/)
  assert.match(nothingComparable.softScore.note, /算不出接近程度/)
  // 真正的 0 仍然照实显示（0 与"没有"不是一回事）。
  assert.equal(configMatchPresentation(candidate({ config_match: { ...candidate().config_match, confidence: 0, soft_score: 0 } })).confidence.text, '0%')
})

test('置信度分母只数有值的项，不用矩阵或相似度顶替', () => {
  const one = configMatchPresentation(candidate({ config_match: {
    ...candidate().config_match, items: [differentItem, unknownItem, unknownItem], confidence: 1 / 3,
  } }))
  assert.equal(one.confidence.note, '1 / 3 项有可核验的值。')
})

test('品牌解析不出来时印「品牌未解析」，不留空', () => {
  assert.equal(BRAND_UNRESOLVED, '品牌未解析')
  assert.equal(candidateBrandLabel({ model: 'H1650K' }), '品牌未解析')
  assert.equal(candidateBrandLabel({ brand: '  ' }), '品牌未解析')
  assert.equal(candidateTitle({ model: 'H1650K' }), '品牌未解析 H1650K')
  assert.equal(candidateTitle({ brand: '湘仪', model: 'H1650K' }), '湘仪 H1650K')
})

test('没有 config_match 或字段形状不对时给空结构，不抛异常、也不假装已对齐', () => {
  const missing = configMatchPresentation({ model: 'X' })
  assert.equal(missing.present, false)
  assert.deepEqual(missing.rows, [])
  assert.match(missing.emptyNote, /没有可与已确认要求逐项对齐的内容/)
  assert.deepEqual(configMatchRows({ config_match: { items: 'not-a-list' } }), [])
  assert.deepEqual(configMatchRows(null), [])
  assert.equal(configMatchPresentation({ config_match: { items: [] } }).present, true)
})
