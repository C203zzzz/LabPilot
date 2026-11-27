import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import test from 'node:test'

import {displayTexts, findLeaks, splitTenderClauses, tenderDeviceForModel, tenderSource} from '../src/services/tenderPresentation.js'

const data = JSON.parse(readFileSync(new URL('./fixtures/tender-requirements.json', import.meta.url), 'utf8'))

test('the fixture still carries the three 4th-package instruments', () => {
  assert.equal(data.packages.length, 1)
  assert.equal(data.packages[0].package, '4包')
  assert.deepEqual(data.packages[0].devices.map(device => device.name),
    ['凝血全血凝固时间监测仪', '干式溶浆机', '血型血清学离心机'])
})

test('an instrument is found by the model that won its package', () => {
  const centrifuge = tenderDeviceForModel(data, 'XKA-2200')
  assert.equal(centrifuge.name, '血型血清学离心机')
  assert.equal(centrifuge.award.brand, '湘仪')
  assert.equal(centrifuge.requirements.length, 13)
  assert.equal(tenderDeviceForModel(data, 'WGH-II').name, '干式溶浆机')
  assert.equal(tenderDeviceForModel(data, 'OCG-102').name, '凝血全血凝固时间监测仪')
})

test('an unrelated or missing model matches nothing', () => {
  assert.equal(tenderDeviceForModel(data, 'MD-2300S'), null) // 1 包的设备，不在这份 fixture 里
  assert.equal(tenderDeviceForModel(data, ''), null)
  assert.equal(tenderDeviceForModel(data, null), null)
  assert.equal(tenderDeviceForModel(data, undefined), null)
  assert.equal(tenderDeviceForModel(null, 'XKA-2200'), null)
})

test('model matching ignores case, spacing, dashes and full-width forms', () => {
  assert.equal(tenderDeviceForModel(data, 'xka-2200').name, '血型血清学离心机')
  assert.equal(tenderDeviceForModel(data, ' XKA - 2200 ').name, '血型血清学离心机')
  assert.equal(tenderDeviceForModel(data, 'ＸＫＡ－２２００').name, '血型血清学离心机')
  assert.equal(tenderDeviceForModel(data, 'XKA-2200S'), null)
})

test('key clauses keep the ▲ marker and ordinary ones do not', () => {
  const {key, plain} = splitTenderClauses(tenderDeviceForModel(data, 'XKA-2200').requirements)
  assert.deepEqual(key.map(row => row.clause), ['▲1', '▲5', '▲12'])
  assert.equal(key.length + plain.length, 13)
  assert.ok(key.every(row => row.clause.startsWith('▲')))
  assert.ok(plain.every(row => !row.clause.startsWith('▲')))
})

test('the fixed wording is still in the data, but the bare text carries no caveat', () => {
  const top = tenderDeviceForModel(data, 'XKA-2200').requirements.find(row => row.clause === '▲5')
  assert.ok(top.statement.startsWith('本项目要求：'))
  assert.ok(top.statement.endsWith('中标人须满足。这不等于该型号的产品标称参数。'))
  assert.equal(top.statement, `本项目要求：血型血清学离心机 ${top.text} 中标人须满足。这不等于该型号的产品标称参数。`)
  assert.ok(!top.text.includes('本项目要求'), '裸文本不该再重复那句说明')
  assert.ok(!top.text.includes('中标人须满足'), '裸文本不该再重复那句说明')
})

test('the source says where the requirements come from', () => {
  const source = tenderSource(data)
  assert.match(source.title, /招标文件/)
  assert.match(source.chapter, /采购需求/)
  assert.ok(source.url.startsWith('http'))
  assert.equal(source.nature, data.nature)
})

test('nothing internal reaches the displayed text', () => {
  const leaks = displayTexts(data).flatMap(findLeaks)
  assert.deepEqual([...new Set(leaks)], [])
})

test('findLeaks still catches what it is meant to catch', () => {
  assert.deepEqual(findLeaks('evidence_type: tender_requirement'), ['evidence_type', 'tender_requirement'])
  assert.deepEqual(findLeaks('本项目要求：最高转速 ≥4500r/min'), [])
  assert.deepEqual(findLeaks(null), [])
})

test('missing data renders nothing instead of throwing', () => {
  assert.deepEqual(splitTenderClauses(undefined), {key: [], plain: []})
  assert.deepEqual(displayTexts(null), [])
  assert.equal(tenderSource(null).title, '')
  assert.equal(tenderDeviceForModel(undefined, 'XKA-2200'), null)
})
