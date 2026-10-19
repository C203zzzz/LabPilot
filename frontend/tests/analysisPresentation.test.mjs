import test from 'node:test'
import assert from 'node:assert/strict'

import {
  evidencePageLabel,
  focusCandidates,
  matchesUserModel,
  mergeEvidence,
  modelTokens,
  structuredUnparsedRows,
  userModelText,
} from '../src/services/analysisPresentation.js'

/**
 * 样例数据（匿名化）：task-<run-8> 的 deepseek_analysis 报告
 * (analysis-1e4c86c5f278477ea8d4d1dd26d930c7) 从一份知楚 A2Ⅱ级生物安全柜
 * 参数表里识别出 7 个型号，而用户原话只点了 1300A2。
 */
const TASK = { scenario: '采购一台知楚安全柜 1300A2，用于细菌分离，报价22800，数量一台，质保5年，售后响应48小时' }
const REQUIREMENT = { scenario: TASK.scenario, hard_constraints: [], soft_constraints: [] }

const candidate = model => ({ candidate_id: `analysis-${model}`, brand: '知楚', model })
const ALL_MODELS = ['900A2', '1300A2', '1500A2', '1800A2', '1306A2', '1506A2', 'A2 II级']

test('型号归一：空格、连字符、下划线、大小写不算不同型号', () => {
  assert.deepEqual(modelTokens('型号：1300a2'), ['1300A2'])
  assert.deepEqual(modelTokens('型号 M-1300_A2'), ['1300'])
  assert.ok(matchesUserModel('bsc-1300a2', modelTokens('采购 1300A2 一台')))
  assert.ok(matchesUserModel('1300A2', modelTokens('采购 BSC-1300A2 一台')))
  assert.ok(!matchesUserModel('A2', modelTokens('采购 1300A2 一台')))
})

test('用户原话只切出真正的型号样子，不切出预算和数量', () => {
  const tokens = modelTokens(TASK.scenario)
  assert.ok(tokens.includes('1300A2'))
  assert.ok(!tokens.includes('A2'), '两字符的 A2 不该单独成为型号')
})

test('只展示用户点名的型号，其他型号收进 others 而不是消失', () => {
  const text = userModelText(REQUIREMENT, TASK)
  const focus = focusCandidates(ALL_MODELS.map(candidate), text)
  assert.deepEqual(focus.candidates.map(item => item.model), ['1300A2'])
  assert.equal(focus.total, 7)
  assert.equal(focus.narrowed, true)
  assert.deepEqual(focus.models, ['1300A2'])
  assert.equal(focus.others.length, 6)
  assert.deepEqual(focus.others.map(item => item.model), ['900A2', '1500A2', '1800A2', '1306A2', '1506A2', 'A2 II级'])
})

test('一个都匹配不上时照旧展示全部候选，绝不返回空列表', () => {
  const others = ['900A2', '1500A2', '1800A2'].map(candidate)
  const focus = focusCandidates(others, '采购一台生物安全柜，用于细菌分离')
  assert.equal(focus.narrowed, false)
  assert.deepEqual(focus.candidates, others)
  assert.deepEqual(focus.others, [])
})

test('候选型号为空或用户没写型号时同样不收敛', () => {
  assert.equal(focusCandidates([candidate('')], '要一台 1300A2').candidates.length, 1)
  assert.equal(focusCandidates(undefined, TASK.scenario).candidates.length, 0)
  const noText = focusCandidates(['A2'].map(candidate), '')
  assert.equal(noText.candidates.length, 1)
  assert.equal(noText.narrowed, false)
})

test('A2 这类通用片段不会命中 1300A2', () => {
  const focus = focusCandidates([candidate('A2')], TASK.scenario)
  assert.deepEqual(focus.candidates.map(item => item.model), ['A2'], '匹配不上就保持全量')
  assert.equal(focus.narrowed, false)
})

test('page:9/rendered 说成人话「第 9 页」', () => {
  assert.equal(evidencePageLabel('page:1/rendered'), '第 1 页')
  assert.equal(evidencePageLabel('page:9'), '第 9 页')
  assert.equal(evidencePageLabel(''), '页码未定位')
  assert.equal(evidencePageLabel('region:2/region:2:25'), 'region:2/region:2:25')
})

test('同一文档同一页的多个证据合并成一个入口并记下处数', () => {
  const documentId = '00000000-0000-4000-8000-000000000003'
  // 样例数据（匿名化）：1300A2 的 physical_dimensions_power_and_installation 有 7 条 sources，全部 page:1/rendered
  const sources = [
    { document_id: documentId, page_or_location: 'page:1/rendered', excerpt: '外形尺寸(长×宽×高mm) (支架高度720) 1340×850×2120' },
    { document_id: documentId, page_or_location: 'page:1/rendered', excerpt: '内部尺寸(长×深×高mm) 1270×580×660' },
    { document_id: documentId, page_or_location: 'page:1/rendered', excerpt: '电源/电压 220-240V/50 Hz，单相' },
    { document_id: documentId, page_or_location: 'page:1/rendered', excerpt: '额定功率/电流 371W/1.68A' },
    { document_id: documentId, page_or_location: 'page:1/rendered', excerpt: '产品净重 210 kg' },
    { document_id: documentId, page_or_location: 'page:1/rendered', excerpt: '装运重量 260 kg' },
    { document_id: documentId, page_or_location: 'page:1/rendered', excerpt: '装载体积 2立方米' },
  ]
  const merged = mergeEvidence(sources, [])
  assert.equal(merged.entries.length, 1)
  assert.equal(merged.entries[0].count, 7)
  assert.equal(merged.entries[0].page, 1)
  assert.equal(evidencePageLabel(merged.entries[0].location), '第 1 页')
  assert.equal(merged.unlocated, 0)
})

test('不同页不合并，没有位置的证据单独计数而不是静默丢弃', () => {
  const documentId = 'doc-1'
  const merged = mergeEvidence([
    { document_id: documentId, page_or_location: 'page:1/rendered', excerpt: 'a' },
    { document_id: documentId, page_or_location: 'page:2/rendered', excerpt: 'b' },
    { document_id: documentId, page_or_location: 'page:2/rendered', excerpt: 'c' },
    { excerpt: '只有正文没有位置' },
  ], [])
  assert.deepEqual(merged.entries.map(entry => entry.count), [1, 2])
  assert.equal(merged.unlocated, 1)
})

test('网址证据按 url 合并，unparsed 状态取并集', () => {
  const merged = mergeEvidence([
    { url: 'https://example.com/a', evidence_status: 'unparsed' },
    { url: 'https://example.com/a' },
  ], [])
  assert.equal(merged.entries.length, 1)
  assert.equal(merged.entries[0].count, 2)
  assert.equal(merged.entries[0].unparsed, true)
})

test('unparsed_values 是对象时一行一条（真实数据 13 处全是对象）', () => {
  const rows = structuredUnparsedRows({
    '外形尺寸(长×宽×高mm)(支架高度720)': '1340×850×2120',
    '内部尺寸(长×深×高mm)': '1270×580×660',
    '核箱尺寸(长×宽×高mm)': '1400×860×1650',
  })
  assert.equal(rows.dropped, 0)
  assert.deepEqual(rows.rows, [
    { label: '外形尺寸(长×宽×高mm)(支架高度720)', value: '1340×850×2120' },
    { label: '内部尺寸(长×深×高mm)', value: '1270×580×660' },
    { label: '核箱尺寸(长×宽×高mm)', value: '1400×860×1650' },
  ])
})

test('值里带 | 的表格原文不展示，也不当成参数值', () => {
  const rows = structuredUnparsedRows({
    '品名': 'A2Ⅱ级生物安全柜',
    '整段表格': '品名 | 品名 | A2Ⅱ级生物安全柜\n型号 | 型号 | 900A2',
  })
  assert.deepEqual(rows.rows, [{ label: '品名', value: 'A2Ⅱ级生物安全柜' }])
  assert.equal(rows.dropped, 1)
})

test('拆不干净就整段不展示，不留半截', () => {
  const rows = structuredUnparsedRows('品名 | 品名 | A2Ⅱ级生物安全柜\n型号 | 型号 | 900A2')
  assert.deepEqual(rows.rows, [])
  assert.equal(rows.dropped, 1)
})

test('字符串形态的「键：值；键：值」按行拆开', () => {
  const rows = structuredUnparsedRows('产品净重：210 kg；装运重量：260 kg')
  assert.equal(rows.rows.length, 2)
  assert.deepEqual(rows.rows[1], { label: '装运重量', value: '260 kg' })
})

test('空值不产生任何行', () => {
  assert.deepEqual(structuredUnparsedRows(null), { rows: [], dropped: 0 })
  assert.deepEqual(structuredUnparsedRows(undefined), { rows: [], dropped: 0 })
  assert.deepEqual(structuredUnparsedRows({}), { rows: [], dropped: 0 })
})
