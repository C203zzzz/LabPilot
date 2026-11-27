/**
 * 报告版本对比在页面上"摆成什么样"的口径。
 *
 * 这里盯的是三件事：
 * - 价格、配置、缺口三类**永远都画**，某一类没变时画成"这一类没有变化"，
 *   而不是让这一类消失（消失了就分不清"没变"和"没比"）；
 * - 新增的条目不印一行空的"上一版：（）"，被删掉的条目要写明"不再出现"——
 *   否则读者会把新增读成"原来是空的"；
 * - "其余 N 条逐字相同"里的 N 用后端给的数字，前端不自己数第二遍。
 */
import assert from 'node:assert/strict'
import test from 'node:test'

import { changeLines, diffGroups, unchangedSummary } from '../src/services/reportDiff.js'

const payload = {
  available: true,
  categories: [
    { key: 'price', title: '价格', items: [{ section: 'baseline', label: '销售单价', change: 'changed', before: '未提供', after: '76000.0 元' }] },
    { key: 'configuration', title: '配置', items: [] },
    { key: 'gaps', title: '缺口', items: [{ section: 'gaps', label: '缺口二', change: 'removed', before: '正文没读到', after: null }] },
  ],
  unchanged: { items: 21 },
}

test('the three categories are always drawn, even the one that did not change', () => {
  const groups = diffGroups(payload)
  assert.deepEqual(groups.map(group => group.key), ['price', 'configuration', 'gaps'])
  assert.deepEqual(groups.map(group => group.title), ['价格', '配置', '缺口'])
  assert.deepEqual(groups.map(group => group.empty), [false, true, false])
})

test('a missing payload draws nothing instead of throwing', () => {
  assert.deepEqual(diffGroups(null), [])
  assert.equal(unchangedSummary(null), '其余 0 条条目两版逐字相同。')
})

test('an added entry never shows an empty previous value', () => {
  const lines = changeLines({ change: 'added', before: null, after: '2 年（本轮销售回复补充）' })
  assert.deepEqual(lines, [{ tone: 'after', text: '本版新增：2 年（本轮销售回复补充）' }])
})

test('a removed entry says so instead of showing an empty new value', () => {
  const lines = changeLines({ change: 'removed', before: '正文没读到', after: null })
  assert.deepEqual(lines, [
    { tone: 'before', text: '上一版：正文没读到' },
    { tone: 'removed', text: '这一条本版不再出现' },
  ])
})

test('a changed entry shows both sides', () => {
  const lines = changeLines({ change: 'changed', before: '未提供', after: '76000.0 元' })
  assert.deepEqual(lines, [
    { tone: 'before', text: '上一版：未提供' },
    { tone: 'after', text: '本版：76000.0 元' },
  ])
})

test('the unchanged count comes from the backend number', () => {
  assert.equal(unchangedSummary(payload), '其余 21 条条目两版逐字相同。')
})
